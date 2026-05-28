"""Train an IRIS-style Atari actor on reconstructed world-model dreams.

This is the mainline policy recipe for the Atari SLS experiments.  The actor
reads decoded 64x64 RGB observations, keeps an LSTM state, and is trained with
lambda-return actor-critic loss over imagined rollouts.  The world model and
tokenizer stay frozen.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
import time
from pathlib import Path

import ale_py
import gymnasium as gym
import numpy as np
import torch
import torch.nn.functional as F

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atari.controller import AtariIrisPolicy
from atari.predictor import AtariPredictorWithHeads, split_atari_predictor_state
from atari.replay_buffer import load_metadata, load_replay_arrays
from deepdash.config import apply_config, load_config
from deepdash.fsq import FSQVAE
from deepdash.wandb_utils import wandb_finish, wandb_init, wandb_log
from deepdash.world_model import WorldModel
from scripts.train_atari_actor_real import (
    amp_dtype,
    load_clean_state,
    parse_action_subset,
    resize_frame_to_64,
)
from scripts.train_atari_predictor import encode_replay_obs, load_matching_state_dict

gym.register_envs(ale_py)
ACTION_NAMES = ["NOOP", "FIRE", "RIGHT", "LEFT", "RIGHTFIRE", "LEFTFIRE"]


def valid_context_starts(replay, context_frames: int, horizon: int):
    episode_ids = replay.episode_ids
    dones = replay.dones
    limit = len(episode_ids) - int(context_frames) - max(int(horizon), 1)
    starts = []
    for i in range(max(limit, 0)):
        end = i + int(context_frames)
        if np.all(episode_ids[i:end] == episode_ids[i]) and not np.any(dones[i:end - 1]):
            starts.append(i)
    if not starts:
        raise RuntimeError("no valid actor contexts found in replay")
    return np.asarray(starts, dtype=np.int64)


def reward_event_context_starts(replay, context_frames: int,
                                min_gap: int, max_gap: int):
    """Context starts whose first trainable action is close to a reward event."""
    if min_gap < 0 or max_gap < min_gap:
        raise ValueError("reward event gaps must satisfy 0 <= min_gap <= max_gap")
    episode_ids = replay.episode_ids
    dones = replay.dones
    rewards = replay.rewards
    by_sign = {1: [], -1: []}
    for reward_idx in np.flatnonzero(rewards != 0):
        sign = 1 if rewards[reward_idx] > 0 else -1
        for gap in range(min_gap, max_gap + 1):
            start = int(reward_idx) - (context_frames - 1) - gap
            end = start + context_frames
            if start < 0 or end > len(episode_ids):
                continue
            episode_id = episode_ids[start]
            if episode_ids[reward_idx] != episode_id:
                continue
            if not np.all(episode_ids[start:reward_idx + 1] == episode_id):
                continue
            if np.any(dones[start:reward_idx]):
                continue
            by_sign[sign].append(start)
    return {
        sign: np.asarray(sorted(set(values)), dtype=np.int64)
        for sign, values in by_sign.items()
    }


def _choice(values, n: int, rng: np.random.Generator):
    if n <= 0 or len(values) == 0:
        return np.empty(0, dtype=np.int64)
    return rng.choice(values, size=n, replace=len(values) < n)


def sample_context_indices(starts, n: int, rng: np.random.Generator,
                           event_starts=None, event_sample_frac: float = 0.0,
                           event_pos_frac: float = 0.5):
    n_event = int(round(n * float(event_sample_frac)))
    event_starts = event_starts or {}
    pos_starts = event_starts.get(1, np.empty(0, dtype=np.int64))
    neg_starts = event_starts.get(-1, np.empty(0, dtype=np.int64))
    if len(pos_starts) == 0 and len(neg_starts) == 0:
        n_event = 0

    n_pos = int(round(n_event * float(event_pos_frac)))
    n_neg = n_event - n_pos
    if len(pos_starts) == 0:
        n_neg += n_pos
        n_pos = 0
    if len(neg_starts) == 0:
        n_pos += n_neg
        n_neg = 0

    parts = [
        _choice(pos_starts, n_pos, rng),
        _choice(neg_starts, n_neg, rng),
        _choice(starts, n - n_pos - n_neg, rng),
    ]
    idx = np.concatenate([part for part in parts if len(part) > 0])
    rng.shuffle(idx)
    return idx, {"pos": int(n_pos), "neg": int(n_neg),
                 "uniform": int(n - n_pos - n_neg)}


def decode_flat_tokens(fsq: FSQVAE, tokens: torch.Tensor, grid_size: int) -> torch.Tensor:
    shape = tokens.shape
    flat = tokens.reshape(-1, grid_size, grid_size).long()
    recon = fsq.decode_indices(flat).clamp(0.0, 1.0)
    return recon.reshape(*shape[:-1], recon.size(1), 64, 64)


def reconstruct_obs(fsq: FSQVAE, frame: np.ndarray, device: torch.device) -> torch.Tensor:
    x = torch.from_numpy(frame).float().permute(2, 0, 1).unsqueeze(0).to(device) / 255.0
    recon, _, _ = fsq(x)
    return recon.clamp(0.0, 1.0)


def action_window_with_last_action(ctx_actions: torch.Tensor,
                                   action: torch.Tensor) -> torch.Tensor:
    return torch.cat([ctx_actions[:, :-1], action.reshape(-1, 1).long()], dim=1)


def reward_from_outputs(reward: torch.Tensor, event_logits: torch.Tensor | None,
                        args) -> torch.Tensor:
    mode = str(args.reward_mode)
    if event_logits is not None and mode in {"event_sample", "event_argmax", "event_threshold"}:
        logits = event_logits.float()
        if mode == "event_threshold":
            probs = torch.softmax(logits, dim=-1)
            neg = probs[..., 0]
            zero = probs[..., 1]
            pos = probs[..., 2]
            event_conf = torch.maximum(neg, pos)
            sign = torch.where(pos >= neg, torch.ones_like(pos), -torch.ones_like(neg))
            return torch.where(
                (event_conf >= float(args.reward_discrete_threshold)) & (event_conf > zero),
                sign,
                torch.zeros_like(sign),
            )
        cls = (
            torch.distributions.Categorical(logits=logits).sample()
            if mode == "event_sample" else logits.argmax(dim=-1)
        )
        return (cls.float() - 1.0).clamp(-1.0, 1.0)
    reward = reward.float().clamp(float(args.reward_clip_min), float(args.reward_clip_max))
    if mode == "scalar_threshold":
        return torch.where(
            reward.abs() >= float(args.reward_discrete_threshold),
            reward.sign(),
            torch.zeros_like(reward),
        )
    return reward


def done_from_prob(done_prob: torch.Tensor, args) -> torch.Tensor:
    mode = str(args.done_mode)
    prob = done_prob.float().clamp(0.0, 1.0)
    if mode == "sample":
        return torch.bernoulli(prob).bool()
    if mode == "threshold":
        return prob >= float(args.done_threshold)
    return torch.zeros_like(prob, dtype=torch.bool)


def compute_iris_lambda_returns(rewards: torch.Tensor, values: torch.Tensor,
                                dones: torch.Tensor, gamma: float,
                                lambda_: float) -> torch.Tensor:
    """IRIS lambda returns over tensors shaped (B, T)."""
    returns = torch.empty_like(values)
    returns[:, -1] = values[:, -1]
    not_done = (~dones).float()
    returns[:, :-1] = (
        rewards[:, :-1]
        + not_done[:, :-1] * float(gamma) * (1.0 - float(lambda_)) * values[:, 1:]
    )
    last = values[:, -1]
    for idx in range(values.size(1) - 2, -1, -1):
        returns[:, idx] += not_done[:, idx] * float(gamma) * float(lambda_) * last
        last = returns[:, idx]
    return returns


def load_predictor(args, model_cfg, pred_cfg, n_actions, device):
    world_model = WorldModel(
        vocab_size=int(model_cfg.get("vocab_size", 1000)),
        n_actions=int(n_actions),
        embed_dim=int(model_cfg.get("embed_dim", 384)),
        n_heads=int(model_cfg.get("n_heads", 8)),
        n_layers=int(model_cfg.get("n_layers", 8)),
        context_frames=int(model_cfg.get("context_frames", 4)),
        dropout=float(model_cfg.get("dropout", 0.1)),
        tokens_per_frame=int(model_cfg.get("tokens_per_frame", 256)),
        adaln=bool(model_cfg.get("adaln", False)),
        use_status_token=False,
        use_cpc=False,
    ).to(device)
    predictor = AtariPredictorWithHeads(
        world_model,
        hidden_dim=int(model_cfg.get("embed_dim", 384)),
        reward_head_type=str(pred_cfg.get("reward_head_type", "scalar")),
        reward_bins=int(pred_cfg.get("reward_twohot_bins", 255)),
        reward_low=float(pred_cfg.get("reward_twohot_low", -25.0)),
        reward_high=float(pred_cfg.get("reward_twohot_high", 25.0)),
        reward_event_head=bool(pred_cfg.get("reward_event_head", False)),
    ).to(device)
    state = load_clean_state(args.predictor_checkpoint, device)
    if "world_model.head.weight" in state:
        load_matching_state_dict(predictor, state, strict=False)
    else:
        wm_state, aux_state = split_atari_predictor_state(state)
        predictor.world_model.load_state_dict(wm_state)
        load_matching_state_dict(predictor, aux_state, strict=False)
    predictor.eval()
    for param in predictor.parameters():
        param.requires_grad_(False)
    return predictor


def imagine(policy: AtariIrisPolicy, predictor, fsq: FSQVAE, ctx_tokens: torch.Tensor,
            ctx_actions: torch.Tensor, action_subset: torch.Tensor | None,
            args, device, amp):
    grid_size = int(args.latent_grid)
    ctx_tokens = ctx_tokens.to(device).long()
    ctx_actions = ctx_actions.to(device).long()
    bsz = ctx_tokens.size(0)

    with torch.no_grad():
        obs_prefix = decode_flat_tokens(fsq, ctx_tokens, grid_size)
    if obs_prefix.size(1) > 1:
        policy.burn_in(obs_prefix[:, :-1])
    else:
        policy.reset(bsz, device)
    obs = obs_prefix[:, -1].detach()

    logits_seq = []
    actions_seq = []
    values_seq = []
    rewards_seq = []
    dones_seq = []
    entropy_seq = []
    active_seq = []
    alive = torch.ones(bsz, dtype=torch.bool, device=device)

    for step in range(int(args.imagine_horizon)):
        active_seq.append(alive.clone())
        with torch.amp.autocast(
            "cuda",
            enabled=amp is not None and device.type == "cuda",
            dtype=amp,
        ):
            action, _, entropy, value, logits = policy.act(
                obs, temperature=float(args.train_temperature), sample=True)
        env_action = action_subset[action] if action_subset is not None else action
        pred_actions = action_window_with_last_action(ctx_actions, env_action)
        with torch.no_grad(), torch.amp.autocast(
            "cuda",
            enabled=amp is not None and device.type == "cuda",
            dtype=amp,
        ):
            pred_tokens, pred_reward, done_prob, event_logits = predictor.predict_next_frame(
                ctx_tokens,
                pred_actions,
                temperature=float(args.world_model_temperature),
                return_aux=True,
                return_event_logits=True,
            )
            reward = reward_from_outputs(pred_reward, event_logits, args)
            done = done_from_prob(done_prob, args)
            reward_event = reward.abs() > float(args.reward_done_epsilon)
            terminal = done | (
                reward_event if bool(args.stop_on_reward) else torch.zeros_like(done)
            )
            masked_reward = reward * alive.float()
            masked_done = terminal & alive
            if step < int(args.imagine_horizon) - 1:
                next_obs = decode_flat_tokens(fsq, pred_tokens, grid_size).detach()
        logits_seq.append(logits)
        actions_seq.append(action)
        values_seq.append(value)
        rewards_seq.append(masked_reward)
        dones_seq.append(masked_done)
        entropy_seq.append(entropy)

        alive = alive & ~masked_done
        ctx_tokens = torch.cat([ctx_tokens[:, 1:], pred_tokens.unsqueeze(1)], dim=1)
        ctx_actions = torch.cat([pred_actions[:, 1:], env_action.unsqueeze(1)], dim=1)
        if step < int(args.imagine_horizon) - 1:
            obs = next_obs

    return {
        "logits": torch.stack(logits_seq, dim=1),
        "actions": torch.stack(actions_seq, dim=1),
        "values": torch.stack(values_seq, dim=1),
        "rewards": torch.stack(rewards_seq, dim=1),
        "dones": torch.stack(dones_seq, dim=1),
        "entropy": torch.stack(entropy_seq, dim=1),
        "active": torch.stack(active_seq, dim=1),
    }


def actor_critic_loss(rollout: dict[str, torch.Tensor], args):
    values = rollout["values"].float()
    rewards = rollout["rewards"].float()
    dones = rollout["dones"].bool()
    lambda_returns = compute_iris_lambda_returns(
        rewards, values.detach(), dones, float(args.gamma), float(args.lambda_))
    train_slice = slice(None, -1) if values.size(1) > 1 else slice(None)
    logits = rollout["logits"][:, train_slice]
    actions = rollout["actions"][:, train_slice]
    values_train = values[:, train_slice]
    returns_train = lambda_returns[:, train_slice]
    entropy = rollout["entropy"][:, train_slice]
    active = rollout["active"][:, train_slice].float()
    active_count = active.sum().clamp_min(1.0)
    dist = torch.distributions.Categorical(logits=logits)
    log_prob = dist.log_prob(actions)
    advantage = returns_train - values_train.detach()
    raw_advantage = advantage
    valid_advantage = raw_advantage[active.bool()]
    if valid_advantage.numel() > 1:
        advantage = (advantage - valid_advantage.mean()) / (
            valid_advantage.std(unbiased=False) + 1e-8)
    else:
        advantage = advantage - advantage.mean()
    loss_actions = -((log_prob * advantage) * active).sum() / active_count
    loss_values = ((values_train - returns_train).pow(2) * active).sum() / active_count
    loss_entropy = -float(args.entropy_weight) * (
        entropy * active).sum() / active_count
    return {
        "total": loss_actions + loss_values + loss_entropy,
        "actions": loss_actions,
        "values": loss_values,
        "entropy": loss_entropy,
        "mean_return": rewards.sum(dim=1).mean(),
        "mean_length": rollout["active"].float().sum(dim=1).mean(),
        "mean_reward": rewards.mean(),
        "mean_value": values.mean(),
        "policy_entropy": entropy.mean(),
        "advantage_mean": raw_advantage.mean(),
        "advantage_std": raw_advantage.std(unbiased=False),
    }


@torch.no_grad()
def evaluate_real_env(policy: AtariIrisPolicy, fsq: FSQVAE, atari_cfg: dict,
                      args, device: torch.device, action_subset: list[int]):
    env = gym.make(
        f"ALE/{atari_cfg.get('game', 'Pong')}-v5",
        frameskip=int(atari_cfg.get("frame_skip", 4)),
        repeat_action_probability=float(atari_cfg.get("repeat_action_probability", 0.0)),
    )
    rng = np.random.default_rng(int(args.eval_seed))
    returns = []
    lengths = []
    action_counts = collections.Counter()
    was_training = policy.training
    policy.eval()
    for _ in range(int(args.eval_episodes)):
        obs, _ = env.reset(seed=int(rng.integers(0, 2**31 - 1)))
        policy.reset(1, device)
        total = 0.0
        steps = 0
        for _ in range(int(args.eval_max_steps)):
            frame = resize_frame_to_64(obs)
            recon = reconstruct_obs(fsq, frame, device)
            action, _, _, _, logits = policy.act(
                recon,
                temperature=float(args.eval_temperature),
                sample=bool(args.eval_sample),
            )
            if not bool(args.eval_sample):
                action = logits.argmax(dim=-1)
            env_action = int(action_subset[int(action.item())])
            action_counts[env_action] += 1
            obs, reward, terminated, truncated, _ = env.step(env_action)
            total += float(reward)
            steps += 1
            if terminated or truncated:
                break
        returns.append(total)
        lengths.append(steps)
    env.close()
    if was_training:
        policy.train()
    return {
        "returns": returns,
        "lengths": lengths,
        "mean_return": float(np.mean(returns)) if returns else 0.0,
        "mean_length": float(np.mean(lengths)) if lengths else 0.0,
        "action_counts": {
            ACTION_NAMES[a] if a < len(ACTION_NAMES) else str(a): int(action_counts.get(a, 0))
            for a in range(max(action_subset) + 1)
            if action_counts.get(a, 0) > 0
        },
    }


def clean_policy_state(policy: AtariIrisPolicy):
    return {k.removeprefix("_orig_mod."): v for k, v in policy.state_dict().items()
            if not k.endswith("hx") and not k.endswith("cx")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/atari/atari_pong_h200_realwarmup.yaml")
    parser.add_argument("--config-section", default="actor_iris")
    parser.add_argument("--replay-dir", default=None)
    parser.add_argument("--checkpoint-dir", default=None)
    parser.add_argument("--fsq-checkpoint", default=None)
    parser.add_argument("--predictor-checkpoint", default=None)
    parser.add_argument("--policy-action-subset", default=None)
    parser.add_argument("--n-iterations", type=int, default=None)
    parser.add_argument("--batch-num-samples", type=int, default=None)
    parser.add_argument("--imagine-horizon", type=int, default=None)
    parser.add_argument("--lr", type=float, default=None)
    parser.add_argument("--gamma", type=float, default=None)
    parser.add_argument("--lambda_", type=float, default=None)
    parser.add_argument("--entropy-weight", type=float, default=None)
    parser.add_argument("--max-grad-norm", type=float, default=None)
    parser.add_argument("--train-temperature", type=float, default=None)
    parser.add_argument("--world-model-temperature", type=float, default=None)
    parser.add_argument("--reward-mode", choices=["event_sample", "event_argmax", "event_threshold", "scalar_threshold", "scalar"], default=None)
    parser.add_argument("--done-mode", choices=["sample", "threshold", "never"], default=None)
    parser.add_argument("--reward-clip-min", type=float, default=None)
    parser.add_argument("--reward-clip-max", type=float, default=None)
    parser.add_argument("--reward-discrete-threshold", type=float, default=None)
    parser.add_argument("--done-threshold", type=float, default=None)
    parser.add_argument("--reward-event-sample-frac", type=float, default=None)
    parser.add_argument("--reward-event-pos-frac", type=float, default=None)
    parser.add_argument("--reward-event-min-gap", type=int, default=None)
    parser.add_argument("--reward-event-max-gap", type=int, default=None)
    parser.add_argument("--stop-on-reward", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--reward-done-epsilon", type=float, default=None)
    parser.add_argument("--eval-interval", type=int, default=None)
    parser.add_argument("--eval-episodes", type=int, default=None)
    parser.add_argument("--eval-max-steps", type=int, default=None)
    parser.add_argument("--eval-temperature", type=float, default=None)
    parser.add_argument("--eval-sample", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--eval-seed", type=int, default=None)
    parser.add_argument("--amp-dtype", choices=["none", "float16", "bfloat16"], default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--init-from", default=None)
    parser.add_argument("--wandb-project", default=None)
    parser.add_argument("--wandb-name", default=None)
    args = parser.parse_args()
    apply_config(args, section=args.config_section)

    atari_cfg = load_config(args.config, section="atari")
    fsq_cfg = load_config(args.config, section="fsq")
    model_cfg = load_config(args.config, section="model")
    pred_cfg = load_config(args.config, section="predictor_sls")
    metadata = load_metadata(args.replay_dir or atari_cfg.get("replay_dir"))

    args.replay_dir = args.replay_dir or atari_cfg.get("replay_dir")
    args.checkpoint_dir = args.checkpoint_dir or "checkpoints_atari_actor_iris"
    args.fsq_checkpoint = args.fsq_checkpoint or pred_cfg.get("fsq_checkpoint")
    args.predictor_checkpoint = args.predictor_checkpoint or str(
        Path(pred_cfg.get("checkpoint_dir")) / "predictor_best.pt")
    args.n_iterations = args.n_iterations or 2000
    args.batch_num_samples = args.batch_num_samples or 64
    args.imagine_horizon = args.imagine_horizon or int(model_cfg.get("context_frames", 4))
    args.lr = args.lr if args.lr is not None else 1e-4
    args.gamma = args.gamma if args.gamma is not None else 0.995
    args.lambda_ = args.lambda_ if args.lambda_ is not None else 0.95
    args.entropy_weight = args.entropy_weight if args.entropy_weight is not None else 0.001
    args.max_grad_norm = args.max_grad_norm if args.max_grad_norm is not None else 10.0
    args.train_temperature = args.train_temperature if args.train_temperature is not None else 1.0
    args.world_model_temperature = (
        args.world_model_temperature if args.world_model_temperature is not None else 1.0)
    args.reward_mode = args.reward_mode or "event_threshold"
    args.done_mode = args.done_mode or "threshold"
    args.reward_clip_min = args.reward_clip_min if args.reward_clip_min is not None else -1.0
    args.reward_clip_max = args.reward_clip_max if args.reward_clip_max is not None else 1.0
    args.reward_discrete_threshold = (
        args.reward_discrete_threshold if args.reward_discrete_threshold is not None else 0.5)
    args.done_threshold = args.done_threshold if args.done_threshold is not None else 0.5
    args.reward_event_sample_frac = (
        args.reward_event_sample_frac if args.reward_event_sample_frac is not None else 0.9)
    args.reward_event_pos_frac = (
        args.reward_event_pos_frac if args.reward_event_pos_frac is not None else 0.5)
    args.reward_event_min_gap = (
        args.reward_event_min_gap if args.reward_event_min_gap is not None else 3)
    args.reward_event_max_gap = (
        args.reward_event_max_gap if args.reward_event_max_gap is not None else 13)
    args.stop_on_reward = bool(args.stop_on_reward) if args.stop_on_reward is not None else True
    args.reward_done_epsilon = (
        args.reward_done_epsilon if args.reward_done_epsilon is not None else 0.5)
    args.eval_interval = args.eval_interval if args.eval_interval is not None else 50
    args.eval_episodes = args.eval_episodes if args.eval_episodes is not None else 16
    args.eval_max_steps = args.eval_max_steps if args.eval_max_steps is not None else 27000
    args.eval_temperature = args.eval_temperature if args.eval_temperature is not None else 0.5
    args.eval_sample = bool(args.eval_sample) if args.eval_sample is not None else True
    args.eval_seed = args.eval_seed if args.eval_seed is not None else int(atari_cfg.get("seed", 42)) + 20000
    args.amp_dtype = args.amp_dtype or "bfloat16"
    args.seed = args.seed if args.seed is not None else int(atari_cfg.get("seed", 42))
    args.wandb_project = args.wandb_project or "sls-wm-atari"
    args.wandb_name = args.wandb_name or f"actor-iris-{Path(args.checkpoint_dir).name}"
    args.latent_grid = int(fsq_cfg.get("latent_grid", 16))

    torch.manual_seed(int(args.seed))
    np.random.seed(int(args.seed))
    rng = np.random.default_rng(int(args.seed))
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    amp = amp_dtype(args.amp_dtype)
    ckpt_dir = Path(args.checkpoint_dir)
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    replay = load_replay_arrays(args.replay_dir)
    n_actions = int((metadata or {}).get("n_actions", pred_cfg.get("n_actions", 6)))
    action_subset = parse_action_subset(args.policy_action_subset, n_actions)
    action_subset_t = (
        torch.tensor(action_subset, dtype=torch.long, device=device)
        if len(action_subset) != n_actions or action_subset != list(range(n_actions)) else None
    )
    print(f"Replay: {args.replay_dir} steps={len(replay.obs)} n_actions={n_actions}")
    print(f"Policy actions={len(action_subset)} subset={action_subset}")

    fsq = FSQVAE(
        img_channels=int(fsq_cfg.get("img_channels", 3)),
        levels=fsq_cfg.get("levels", [8, 5, 5, 5]),
        norm_type=fsq_cfg.get("norm_type", "group"),
        latent_grid=args.latent_grid,
    ).to(device)
    fsq.load_state_dict(load_clean_state(args.fsq_checkpoint, device))
    fsq.eval()
    for param in fsq.parameters():
        param.requires_grad_(False)

    tokens = encode_replay_obs(fsq, replay.obs, batch_size=256, device=device).cpu()
    context_frames = int(model_cfg.get("context_frames", 4))
    starts = valid_context_starts(replay, context_frames, int(args.imagine_horizon))
    event_starts = reward_event_context_starts(
        replay, context_frames, int(args.reward_event_min_gap),
        int(args.reward_event_max_gap))
    print(
        "Actor contexts: "
        f"uniform={len(starts)} reward_pos={len(event_starts[1])} "
        f"reward_neg={len(event_starts[-1])} "
        f"event_frac={float(args.reward_event_sample_frac):.2f}"
    )
    predictor = load_predictor(args, model_cfg, pred_cfg, n_actions, device)

    policy = AtariIrisPolicy(
        n_actions=len(action_subset),
        input_channels=int(fsq_cfg.get("img_channels", 3)),
    ).to(device)
    latest = ckpt_dir / "actor_iris_latest.pt"
    best = ckpt_dir / "actor_iris_best.pt"
    start_iter = 1
    best_return = -float("inf")
    if args.init_from:
        policy.load_state_dict(load_clean_state(args.init_from, device), strict=False)
        print(f"Initialized actor from {args.init_from}")
    elif args.resume and latest.exists():
        state = torch.load(latest, map_location=device, weights_only=False)
        policy.load_state_dict(state["actor"], strict=False)
        start_iter = int(state.get("iteration", 0)) + 1
        best_return = float(state.get("best_return", best_return))
        rng.bit_generator.state = state.get("rng_state", rng.bit_generator.state)
        print(f"Resumed actor from {latest} at iteration {start_iter - 1}")

    optimizer = torch.optim.Adam(policy.parameters(), lr=float(args.lr))
    if args.resume and latest.exists() and not args.init_from:
        state = torch.load(latest, map_location=device, weights_only=False)
        if "optimizer" in state:
            optimizer.load_state_dict(state["optimizer"])

    wandb_init(
        project=args.wandb_project,
        name=args.wandb_name,
        config={
            **vars(args),
            "replay_steps": int(len(replay.obs)),
            "n_actions": int(n_actions),
            "policy_action_subset": action_subset,
        },
    )
    log_path = ckpt_dir / "actor_iris_log.csv"
    with open(log_path, "a" if args.resume and log_path.exists() else "w", newline="") as f:
        log = csv.writer(f)
        if f.tell() == 0:
            log.writerow([
                "iteration", "loss", "loss_actions", "loss_values", "loss_entropy",
                "dream_return", "dream_length", "dream_reward", "value_mean",
                "entropy", "eval_return", "eval_length", "eval_actions", "time_s",
            ])
        try:
            for iteration in range(start_iter, int(args.n_iterations) + 1):
                t0 = time.time()
                idx, sample_info = sample_context_indices(
                    starts, int(args.batch_num_samples), rng, event_starts,
                    float(args.reward_event_sample_frac),
                    float(args.reward_event_pos_frac))
                ctx_tokens = torch.stack([
                    tokens[int(i):int(i) + context_frames] for i in idx
                ], dim=0)
                ctx_actions = torch.from_numpy(np.stack([
                    replay.actions[int(i):int(i) + context_frames] for i in idx
                ]).astype(np.int64))

                policy.train()
                rollout = imagine(
                    policy, predictor, fsq, ctx_tokens, ctx_actions,
                    action_subset_t, args, device, amp)
                losses = actor_critic_loss(rollout, args)
                optimizer.zero_grad(set_to_none=True)
                losses["total"].backward()
                grad_norm = torch.nn.utils.clip_grad_norm_(
                    policy.parameters(), float(args.max_grad_norm))
                optimizer.step()
                policy.clear()

                eval_metrics = None
                if int(args.eval_interval) > 0 and (
                    iteration == 1
                    or iteration % int(args.eval_interval) == 0
                    or iteration == int(args.n_iterations)
                ):
                    eval_metrics = evaluate_real_env(
                        policy, fsq, atari_cfg, args, device, action_subset)
                    if eval_metrics["mean_return"] > best_return:
                        best_return = float(eval_metrics["mean_return"])
                        torch.save(clean_policy_state(policy), best)

                clean = clean_policy_state(policy)
                torch.save({
                    "actor": clean,
                    "optimizer": optimizer.state_dict(),
                    "iteration": int(iteration),
                    "best_return": float(best_return),
                    "rng_state": rng.bit_generator.state,
                    "args": vars(args),
                }, latest)
                elapsed = time.time() - t0
                payload = {
                    "actor_iris/iteration": int(iteration),
                    "actor_iris/loss": float(losses["total"].item()),
                    "actor_iris/loss_actions": float(losses["actions"].item()),
                    "actor_iris/loss_values": float(losses["values"].item()),
                    "actor_iris/loss_entropy": float(losses["entropy"].item()),
                    "actor_iris/dream_return": float(losses["mean_return"].item()),
                    "actor_iris/dream_length": float(losses["mean_length"].item()),
                    "actor_iris/dream_reward": float(losses["mean_reward"].item()),
                    "actor_iris/value_mean": float(losses["mean_value"].item()),
                    "actor_iris/entropy": float(losses["policy_entropy"].item()),
                    "actor_iris/advantage_mean": float(losses["advantage_mean"].item()),
                    "actor_iris/advantage_std": float(losses["advantage_std"].item()),
                    "actor_iris/sample_pos": int(sample_info["pos"]),
                    "actor_iris/sample_neg": int(sample_info["neg"]),
                    "actor_iris/sample_uniform": int(sample_info["uniform"]),
                    "actor_iris/grad_norm": float(grad_norm),
                    "actor_iris/best_return": float(best_return),
                    "actor_iris/time_s": elapsed,
                }
                eval_return = ""
                eval_length = ""
                eval_actions = ""
                if eval_metrics is not None:
                    eval_return = float(eval_metrics["mean_return"])
                    eval_length = float(eval_metrics["mean_length"])
                    eval_actions = eval_metrics["action_counts"]
                    payload["actor_iris/eval_return"] = eval_return
                    payload["actor_iris/eval_length"] = eval_length
                    for action_name, count in eval_metrics["action_counts"].items():
                        payload[f"actor_iris/eval_actions/{action_name}"] = int(count)
                    print(
                        f"iter={iteration} eval_return={eval_return:+.3f} "
                        f"len={eval_length:.1f} actions={eval_actions}")
                print(
                    f"iter={iteration} loss={payload['actor_iris/loss']:.4f} "
                    f"dream_return={payload['actor_iris/dream_return']:+.3f} "
                    f"entropy={payload['actor_iris/entropy']:.3f} "
                    f"grad={payload['actor_iris/grad_norm']:.3f} time={elapsed:.1f}s")
                log.writerow([
                    iteration,
                    f"{payload['actor_iris/loss']:.6f}",
                    f"{payload['actor_iris/loss_actions']:.6f}",
                    f"{payload['actor_iris/loss_values']:.6f}",
                    f"{payload['actor_iris/loss_entropy']:.6f}",
                    f"{payload['actor_iris/dream_return']:.6f}",
                    f"{payload['actor_iris/dream_length']:.6f}",
                    f"{payload['actor_iris/dream_reward']:.6f}",
                    f"{payload['actor_iris/value_mean']:.6f}",
                    f"{payload['actor_iris/entropy']:.6f}",
                    "" if eval_return == "" else f"{eval_return:.6f}",
                    "" if eval_length == "" else f"{eval_length:.1f}",
                    eval_actions,
                    f"{elapsed:.1f}",
                ])
                f.flush()
                wandb_log(payload)
        finally:
            torch.save(clean_policy_state(policy), ckpt_dir / "actor_iris_final.pt")
            summary = {
                "checkpoint": str(ckpt_dir / "actor_iris_final.pt"),
                "best_checkpoint": str(best),
                "best_return": best_return,
                "latest": str(latest),
            }
            (ckpt_dir / "actor_iris_summary.json").write_text(json.dumps(summary, indent=2))
            wandb_finish()


if __name__ == "__main__":
    main()

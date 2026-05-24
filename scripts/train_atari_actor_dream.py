"""Train an Atari actor with PPO inside the learned world model."""

from __future__ import annotations

import argparse
import collections
import copy
import csv
import json
import sys
import time
from pathlib import Path

import gymnasium as gym
import numpy as np
import torch
import torch.nn.functional as F
import ale_py

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atari.controller import AtariCNNPolicy
from atari.actor_critic import compute_lambda_returns, ppo_update as shared_ppo_update
from atari.predictor import AtariPredictorWithHeads, split_atari_predictor_state
from atari.rl_targets import PercentileNormalizer
from atari.replay_buffer import load_metadata, load_replay_arrays
from deepdash.config import apply_config, load_config
from deepdash.fsq import FSQVAE
from deepdash.wandb_utils import wandb_finish, wandb_init, wandb_log
from deepdash.world_model import WorldModel
from scripts.train_atari_actor_real import (
    amp_dtype,
    load_clean_state,
    load_module_state_matching,
    load_policy_state_flexible,
)
from scripts.train_atari_actor_real import encode_frame, resize_frame_to_64
from scripts.train_atari_predictor import encode_replay_obs, load_matching_state_dict

gym.register_envs(ale_py)
ACTION_NAMES = ["NOOP", "FIRE", "RIGHT", "LEFT", "RIGHTFIRE", "LEFTFIRE"]


def valid_context_starts(replay, context_frames: int):
    episode_ids = replay.episode_ids
    dones = replay.dones
    starts = []
    for i in range(0, len(episode_ids) - context_frames):
        end = i + context_frames
        if np.all(episode_ids[i:end] == episode_ids[i]) and not np.any(dones[i:end - 1]):
            starts.append(i)
    if not starts:
        raise RuntimeError("no valid dream contexts found in replay")
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
    reward_indices = np.flatnonzero(rewards != 0)
    for reward_idx in reward_indices:
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


def _choice(values, n, rng):
    if n <= 0 or len(values) == 0:
        return np.empty(0, dtype=np.int64)
    return rng.choice(values, size=n, replace=len(values) < n)


def sample_contexts(starts, tokens, actions, context_frames, n, rng,
                    event_starts=None, event_sample_frac=0.0,
                    event_pos_frac=0.5):
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

    idx_parts = [
        _choice(pos_starts, n_pos, rng),
        _choice(neg_starts, n_neg, rng),
        _choice(starts, n - n_pos - n_neg, rng),
    ]
    idx = np.concatenate([part for part in idx_parts if len(part) > 0])
    rng.shuffle(idx)
    ctx_tokens = torch.stack([tokens[i:i + context_frames] for i in idx], dim=0)
    ctx_actions = torch.from_numpy(np.stack([
        actions[i:i + context_frames] for i in idx
    ]).astype(np.int64))
    return ctx_tokens, ctx_actions, {
        "pos": int(n_pos),
        "neg": int(n_neg),
        "uniform": int(n - n_pos - n_neg),
    }


def ppo_update(policy, optimizer, batch, args, device, amp):
    tokens = torch.cat(batch["tokens"], dim=0).to(device)
    h = torch.cat(batch["h"], dim=0).to(device)
    actions = torch.stack(batch["actions"]).reshape(-1).long().to(device)
    old_logp = torch.stack(batch["logp"]).reshape(-1).float().to(device)
    adv = batch["advantages"].to(device)
    returns = batch["returns"].to(device)
    adv = (adv - adv.mean()) / (adv.std(unbiased=False) + 1e-8)

    n = actions.numel()
    idx = torch.arange(n, device=device)
    total_loss = total_entropy = 0.0
    updates = 0
    for _ in range(args.ppo_epochs):
        perm = idx[torch.randperm(n, device=device)]
        for start in range(0, n, args.minibatch_size):
            mb = perm[start:start + args.minibatch_size]
            with torch.amp.autocast("cuda", enabled=amp is not None and device.type == "cuda", dtype=amp):
                logits, value = policy(tokens[mb], h[mb])
                dist = torch.distributions.Categorical(logits=logits)
                logp = dist.log_prob(actions[mb])
                ratio = (logp - old_logp[mb]).exp()
                pg1 = -adv[mb] * ratio
                pg2 = -adv[mb] * torch.clamp(ratio, 1.0 - args.clip_eps, 1.0 + args.clip_eps)
                actor_loss = torch.max(pg1, pg2).mean()
                critic_loss = F.mse_loss(value, returns[mb])
                entropy = dist.entropy().mean()
                loss = actor_loss + args.critic_coeff * critic_loss - args.entropy_coeff * entropy
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(policy.parameters(), args.max_grad_norm)
            optimizer.step()
            total_loss += float(loss.item())
            total_entropy += float(entropy.item())
            updates += 1
    return total_loss / max(updates, 1), total_entropy / max(updates, 1)


def compute_gae_sequence(rewards, values, dones, bootstrap, gamma, lam):
    """GAE over a (T, B) imagined rollout."""
    advantages = torch.zeros_like(rewards)
    gae = torch.zeros(rewards.size(1), device=rewards.device)
    for t in reversed(range(rewards.size(0))):
        if t == rewards.size(0) - 1:
            next_value = bootstrap
            next_nonterminal = 1.0 - dones[t].float()
        else:
            next_value = values[t + 1]
            next_nonterminal = 1.0 - dones[t].float()
        delta = rewards[t] + gamma * next_value * next_nonterminal - values[t]
        gae = delta + gamma * lam * next_nonterminal * gae
        advantages[t] = gae
    return advantages.reshape(-1), (advantages + values).reshape(-1)


def atari_event_reward(reward, threshold: float):
    """Map scalar reward-head output to Atari's sparse -1/0/+1 event reward."""
    return torch.where(
        reward.abs() >= threshold,
        reward.sign(),
        torch.zeros_like(reward),
    )


def action_window_with_last_action(ctx_actions: torch.Tensor,
                                   action: torch.Tensor) -> torch.Tensor:
    """Align a chosen action with the last context frame for next-step prediction."""
    return torch.cat([ctx_actions[:, :-1], action.reshape(-1, 1).long()], dim=1)


@torch.no_grad()
def calibrate_dream_rewards(predictor, starts, tokens, actions, rewards,
                            context_frames: int, args, device, amp, rng):
    if args.reward_calibration_samples <= 0:
        return None
    n = min(int(args.reward_calibration_samples), len(starts))
    idx = rng.choice(starts, size=n, replace=len(starts) < n)
    horizon = int(args.reward_calibration_horizon)
    rows = []
    sign_correct = timing_errors = false_positive = missed = 0
    event_cases = neutral_cases = 0
    for start in idx:
        start = int(start)
        end = start + context_frames
        if end - 1 + horizon > len(actions):
            continue
        ctx_tokens = tokens[start:end].unsqueeze(0).to(device)
        ctx_actions = torch.from_numpy(
            actions[start:end].astype(np.int64)
        ).unsqueeze(0).to(device)
        true_window = rewards[end - 1:end - 1 + horizon]
        true_events = np.flatnonzero(true_window != 0)
        true_gap = int(true_events[0]) if len(true_events) else None
        true_sign = int(np.sign(true_window[true_gap])) if true_gap is not None else 0
        pred_gap = None
        pred_sign = 0
        for step in range(horizon):
            action_idx = end - 1 + step
            action = int(actions[action_idx])
            pred_actions = action_window_with_last_action(
                ctx_actions, torch.tensor([action], dtype=torch.long, device=device))
            with torch.amp.autocast("cuda", enabled=amp is not None and device.type == "cuda", dtype=amp):
                pred_tokens, pred_reward, _ = predictor.predict_next_frame(
                    ctx_tokens, pred_actions, temperature=args.temperature,
                    return_aux=True)
            pred_event = atari_event_reward(
                pred_reward.float().clamp(args.reward_clip_min, args.reward_clip_max),
                args.reward_discrete_threshold,
            )
            if pred_gap is None and bool((pred_event != 0).item()):
                pred_gap = step
                pred_sign = int(pred_event.item())
            ctx_tokens = torch.cat([ctx_tokens[:, 1:], pred_tokens.unsqueeze(1)], dim=1)
            ctx_actions = torch.cat([pred_actions[:, 1:], pred_actions[:, -1:]], dim=1)
        if true_gap is None:
            neutral_cases += 1
            false_positive += int(pred_gap is not None)
        else:
            event_cases += 1
            if pred_gap is None:
                missed += 1
            else:
                sign_correct += int(pred_sign == true_sign)
                timing_errors += abs(pred_gap - true_gap)
        rows.append({
            "start": start,
            "true_gap": "" if true_gap is None else true_gap,
            "true_sign": true_sign,
            "pred_gap": "" if pred_gap is None else pred_gap,
            "pred_sign": pred_sign,
        })
    sign_acc = sign_correct / max(event_cases - missed, 1)
    miss_rate = missed / max(event_cases, 1)
    false_positive_rate = false_positive / max(neutral_cases, 1)
    mean_timing_error = timing_errors / max(event_cases - missed, 1)
    return {
        "rows": rows,
        "event_cases": event_cases,
        "neutral_cases": neutral_cases,
        "sign_accuracy": sign_acc,
        "miss_rate": miss_rate,
        "false_positive_rate": false_positive_rate,
        "mean_timing_error": mean_timing_error,
    }


@torch.no_grad()
def evaluate_real_env(policy, fsq, predictor, atari_cfg, model_cfg, args, device, amp,
                      n_actions: int):
    """Deterministic real-env eval used only for checkpoint selection."""
    env = gym.make(
        f"ALE/{atari_cfg.get('game', 'Pong')}-v5",
        frameskip=int(atari_cfg.get("frame_skip", 4)),
        repeat_action_probability=float(atari_cfg.get("repeat_action_probability", 0.0)),
    )
    k = int(model_cfg.get("context_frames", 4))
    rng = np.random.default_rng(args.real_eval_seed)
    returns, lengths = [], []
    action_counts = collections.Counter()
    reward_true_events = 0
    reward_pred_events = 0
    reward_sign_correct = 0
    reward_false_positive = 0
    reward_missed = 0
    was_training = policy.training
    policy.eval()
    predictor.eval()
    fsq.eval()
    for _ in range(args.real_eval_episodes):
        obs, _ = env.reset(seed=int(rng.integers(0, 2**31 - 1)))
        frame = resize_frame_to_64(obs)
        token = encode_frame(fsq, frame, device).cpu()
        ctx_tokens = [token.clone() for _ in range(k)]
        ctx_actions = [0 for _ in range(k)]
        ep_return = 0.0
        steps = 0
        for _ in range(args.real_eval_max_steps):
            ctx_t = torch.stack(ctx_tokens[-k:], dim=1).to(device)
            ctx_a = torch.tensor([ctx_actions[-k:]], dtype=torch.long, device=device)
            with torch.amp.autocast("cuda", enabled=amp is not None and device.type == "cuda", dtype=amp):
                h_t = predictor.encode_controller_context(ctx_t, ctx_a)
                logits, _ = policy(ctx_t[:, -1], h_t.float())
            action = int(logits.argmax(dim=-1).item())
            if action < 0 or action >= n_actions:
                raise RuntimeError(f"policy emitted invalid action {action} for n_actions={n_actions}")
            pred_actions = action_window_with_last_action(
                ctx_a, torch.tensor([action], dtype=torch.long, device=device))
            with torch.amp.autocast("cuda", enabled=amp is not None and device.type == "cuda", dtype=amp):
                _, pred_reward, _ = predictor.predict_next_frame(
                    ctx_t, pred_actions, temperature=args.temperature,
                    return_aux=True)
            pred_event = atari_event_reward(
                pred_reward.float().clamp(args.reward_clip_min, args.reward_clip_max),
                args.reward_discrete_threshold,
            )
            action_counts[action] += 1
            obs, reward, terminated, truncated, _ = env.step(action)
            true_event = int(np.sign(reward)) if reward != 0 else 0
            pred_event_int = int(pred_event.item())
            if true_event != 0:
                reward_true_events += 1
                if pred_event_int == 0:
                    reward_missed += 1
                elif pred_event_int == true_event:
                    reward_sign_correct += 1
            if pred_event_int != 0:
                reward_pred_events += 1
                if true_event == 0:
                    reward_false_positive += 1
            frame = resize_frame_to_64(obs)
            ctx_tokens.append(encode_frame(fsq, frame, device).cpu())
            ctx_actions.append(action)
            ep_return += float(reward)
            steps += 1
            if terminated or truncated:
                break
        returns.append(ep_return)
        lengths.append(steps)
    env.close()
    if was_training:
        policy.train()
    return {
        "mean_return": float(np.mean(returns)) if returns else 0.0,
        "returns": returns,
        "mean_length": float(np.mean(lengths)) if lengths else 0.0,
        "lengths": lengths,
        "action_counts": {
            ACTION_NAMES[a] if a < len(ACTION_NAMES) else str(a): int(action_counts.get(a, 0))
            for a in range(n_actions)
        },
        "reward_true_events": reward_true_events,
        "reward_pred_events": reward_pred_events,
        "reward_sign_accuracy": reward_sign_correct / max(reward_true_events - reward_missed, 1),
        "reward_miss_rate": reward_missed / max(reward_true_events, 1),
        "reward_false_positive_rate": reward_false_positive / max(reward_pred_events, 1),
    }


@torch.no_grad()
def dream_rollout(predictor, policy, ctx_tokens, ctx_actions, args, device, amp,
                  value_policy=None):
    ctx_tokens = ctx_tokens.to(device)
    ctx_actions = ctx_actions.to(device)
    bsz = ctx_tokens.size(0)
    alive = torch.ones(bsz, dtype=torch.bool, device=device)
    episode_return = torch.zeros(bsz, dtype=torch.float32, device=device)
    active_steps = torch.zeros(bsz, dtype=torch.float32, device=device)

    rollout = {"tokens": [], "h": [], "actions": [], "logp": [], "values": [],
               "rewards": [], "dones": []}

    for _ in range(args.max_dream_steps):
        if not alive.any():
            break
        with torch.amp.autocast("cuda", enabled=amp is not None and device.type == "cuda", dtype=amp):
            h_t = predictor.encode_controller_context(ctx_tokens, ctx_actions)
            token_t = ctx_tokens[:, -1]
            action_t, logp_t, _, value_t = policy.act(token_t, h_t.float())

            pred_actions = action_window_with_last_action(ctx_actions, action_t)
            pred_tokens, reward, done_prob = predictor.predict_next_frame(
                ctx_tokens, pred_actions, temperature=args.temperature,
                return_aux=True)

        reward = reward.float().clamp(args.reward_clip_min, args.reward_clip_max)
        if args.reward_mode == "discrete":
            reward = atari_event_reward(reward, args.reward_discrete_threshold)
        reward_event = reward.abs() > args.reward_done_epsilon
        done = done_prob >= args.done_threshold
        terminal = done | (reward_event if args.stop_on_reward else torch.zeros_like(done))
        active = alive.float()
        masked_reward = reward * active
        masked_done = terminal & alive
        episode_return += masked_reward
        active_steps += active

        # Store the reward-bearing transition before applying terminal masks.
        # This lets GAE propagate +1/-1 back to all earlier actions in the
        # dream, while still stopping immediately after the point is scored.
        rollout["tokens"].append(token_t.detach().cpu())
        rollout["h"].append(h_t.detach().cpu().float())
        rollout["actions"].append(action_t.detach().cpu())
        rollout["logp"].append(logp_t.detach().cpu())
        rollout["values"].append(value_t.detach().cpu())
        rollout["rewards"].append(masked_reward.detach().cpu())
        rollout["dones"].append(masked_done.detach().cpu())

        alive &= ~terminal
        ctx_tokens = torch.cat([ctx_tokens[:, 1:], pred_tokens.unsqueeze(1)], dim=1)
        ctx_actions = torch.cat([pred_actions[:, 1:], action_t.unsqueeze(1)], dim=1)

    if not rollout["rewards"]:
        return None, episode_return

    if alive.any():
        with torch.amp.autocast("cuda", enabled=amp is not None and device.type == "cuda", dtype=amp):
            h_t = predictor.encode_controller_context(ctx_tokens, ctx_actions)
            target_policy = value_policy if value_policy is not None else policy
            _, bootstrap = target_policy(ctx_tokens[:, -1], h_t.float())
        bootstrap = bootstrap * alive.float()
    else:
        bootstrap = torch.zeros(bsz, device=device)

    rewards = torch.stack(rollout["rewards"]).to(device)
    values = torch.stack(rollout["values"]).to(device)
    dones = torch.stack(rollout["dones"]).to(device)
    adv, returns = compute_lambda_returns(
        rewards, values, dones, bootstrap, args.gamma, args.lam)
    rollout["advantages"] = adv.cpu()
    rollout["returns"] = returns.cpu()
    rollout["mean_active_steps"] = float(active_steps.mean().item())
    return rollout, episode_return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/atari/atari_pong_h200.yaml")
    parser.add_argument("--config-section", default="actor_dream")
    parser.add_argument("--replay-dir", default=None)
    parser.add_argument("--checkpoint-dir", default=None)
    parser.add_argument("--fsq-checkpoint", default=None)
    parser.add_argument("--predictor-checkpoint", default=None)
    parser.add_argument("--pretrained", default=None)
    parser.add_argument("--n-iterations", type=int, default=None)
    parser.add_argument("--n-episodes", type=int, default=None)
    parser.add_argument("--max-dream-steps", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--done-threshold", type=float, default=None)
    parser.add_argument("--reward-clip-min", type=float, default=None)
    parser.add_argument("--reward-clip-max", type=float, default=None)
    parser.add_argument("--reward-mode", choices=["raw", "discrete"], default=None,
                        help="Use raw clipped reward-head output or Atari -1/0/+1 thresholded rewards.")
    parser.add_argument("--reward-discrete-threshold", type=float, default=None)
    parser.add_argument("--reward-event-sample-frac", type=float, default=None)
    parser.add_argument("--reward-event-pos-frac", type=float, default=None)
    parser.add_argument("--reward-event-min-gap", type=int, default=None,
                        help="Minimum trainable dream steps from PPO start to a replay reward.")
    parser.add_argument("--reward-event-max-gap", type=int, default=None,
                        help="Maximum trainable dream steps from PPO start to a replay reward.")
    parser.add_argument("--stop-on-reward", action=argparse.BooleanOptionalAction,
                        default=None,
                        help="Treat non-zero predicted reward as terminal for dream PPO.")
    parser.add_argument("--reward-done-epsilon", type=float, default=None)
    parser.add_argument("--lr", type=float, default=None)
    parser.add_argument("--gamma", type=float, default=None)
    parser.add_argument("--lam", type=float, default=None)
    parser.add_argument("--clip-eps", type=float, default=None)
    parser.add_argument("--ppo-epochs", type=int, default=None)
    parser.add_argument("--minibatch-size", type=int, default=None)
    parser.add_argument("--entropy-coeff", type=float, default=None)
    parser.add_argument("--critic-coeff", type=float, default=None)
    parser.add_argument("--max-grad-norm", type=float, default=None)
    parser.add_argument("--value-head-type", choices=["scalar", "twohot"], default=None)
    parser.add_argument("--value-twohot-bins", type=int, default=None)
    parser.add_argument("--value-twohot-low", type=float, default=None)
    parser.add_argument("--value-twohot-high", type=float, default=None)
    parser.add_argument("--return-normalizer-momentum", type=float, default=None)
    parser.add_argument("--ema-decay", type=float, default=None)
    parser.add_argument("--real-eval-interval", type=int, default=None,
                        help="Run deterministic real-env eval every N dream PPO iterations; 0 disables.")
    parser.add_argument("--real-eval-episodes", type=int, default=None)
    parser.add_argument("--real-eval-max-steps", type=int, default=None)
    parser.add_argument("--real-eval-seed", type=int, default=None)
    parser.add_argument("--global-best-real-checkpoint", default=None,
                        help="Checkpoint updated only when real eval beats the full run's prior best.")
    parser.add_argument("--global-best-real-metadata", default=None,
                        help="JSON metadata storing the full-run best real eval score.")
    parser.add_argument("--reward-calibration-samples", type=int, default=None)
    parser.add_argument("--reward-calibration-horizon", type=int, default=None)
    parser.add_argument("--dream-gate", action=argparse.BooleanOptionalAction,
                        default=None,
                        help="Skip dream PPO when reward-head calibration is not reliable enough.")
    parser.add_argument("--dream-gate-miss-rate", type=float, default=None)
    parser.add_argument("--dream-gate-false-positive-rate", type=float, default=None)
    parser.add_argument("--dream-gate-sign-accuracy", type=float, default=None)
    parser.add_argument("--dream-gate-real-eval-episodes", type=int, default=None)
    parser.add_argument("--wandb-project", default=None)
    parser.add_argument("--wandb-name", default=None)
    parser.add_argument("--amp-dtype", choices=["none", "float16", "bfloat16"], default=None)
    parser.add_argument("--compile-mode", choices=["none", "default", "reduce-overhead"], default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--init-from", default=None,
                        help="Warm-start actor weights from a checkpoint, but reset optimizer/iteration/RNG state.")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    apply_config(args, section=args.config_section)

    atari_cfg = load_config(args.config, section="atari")
    fsq_cfg = load_config(args.config, section="fsq")
    model_cfg = load_config(args.config, section="model")
    pred_cfg = load_config(args.config, section="predictor_sls")

    args.replay_dir = args.replay_dir or atari_cfg.get("replay_dir", "data/atari/Pong/replay")
    args.checkpoint_dir = args.checkpoint_dir or "checkpoints_atari_actor_dream"
    args.fsq_checkpoint = args.fsq_checkpoint or pred_cfg.get("fsq_checkpoint")
    args.predictor_checkpoint = args.predictor_checkpoint or str(Path(pred_cfg.get("checkpoint_dir")) / "predictor_best.pt")
    args.n_iterations = args.n_iterations or 1000
    args.n_episodes = args.n_episodes or 32
    args.max_dream_steps = args.max_dream_steps or 15
    args.temperature = args.temperature if args.temperature is not None else 0.0
    args.done_threshold = args.done_threshold if args.done_threshold is not None else 0.5
    args.reward_clip_min = args.reward_clip_min if args.reward_clip_min is not None else -1.0
    args.reward_clip_max = args.reward_clip_max if args.reward_clip_max is not None else 1.0
    args.reward_mode = args.reward_mode or "raw"
    args.reward_discrete_threshold = (
        args.reward_discrete_threshold if args.reward_discrete_threshold is not None else 0.5)
    args.reward_event_sample_frac = (
        args.reward_event_sample_frac if args.reward_event_sample_frac is not None else 0.0)
    args.reward_event_pos_frac = (
        args.reward_event_pos_frac if args.reward_event_pos_frac is not None else 0.5)
    args.reward_event_min_gap = (
        args.reward_event_min_gap if args.reward_event_min_gap is not None else 3)
    args.reward_event_max_gap = (
        args.reward_event_max_gap if args.reward_event_max_gap is not None else 13)
    args.stop_on_reward = bool(args.stop_on_reward) if args.stop_on_reward is not None else False
    args.reward_done_epsilon = (
        args.reward_done_epsilon if args.reward_done_epsilon is not None else 0.5)
    args.lr = args.lr or 2.5e-4
    args.gamma = args.gamma if args.gamma is not None else 0.997
    args.lam = args.lam if args.lam is not None else 0.95
    args.clip_eps = args.clip_eps if args.clip_eps is not None else 0.2
    args.ppo_epochs = args.ppo_epochs or 4
    args.minibatch_size = args.minibatch_size or 256
    args.entropy_coeff = args.entropy_coeff if args.entropy_coeff is not None else 0.01
    args.critic_coeff = args.critic_coeff if args.critic_coeff is not None else 0.5
    args.max_grad_norm = args.max_grad_norm if args.max_grad_norm is not None else 0.5
    args.value_head_type = args.value_head_type or "scalar"
    args.value_twohot_bins = args.value_twohot_bins or 255
    args.value_twohot_low = args.value_twohot_low if args.value_twohot_low is not None else -25.0
    args.value_twohot_high = args.value_twohot_high if args.value_twohot_high is not None else 25.0
    args.return_normalizer_momentum = (
        args.return_normalizer_momentum if args.return_normalizer_momentum is not None else 0.99)
    args.ema_decay = args.ema_decay if args.ema_decay is not None else 0.995
    args.seed = args.seed if args.seed is not None else int(atari_cfg.get("seed", 42))
    args.real_eval_interval = args.real_eval_interval if args.real_eval_interval is not None else 0
    args.real_eval_episodes = args.real_eval_episodes or 5
    args.real_eval_max_steps = args.real_eval_max_steps or 27000
    args.real_eval_seed = args.real_eval_seed if args.real_eval_seed is not None else args.seed + 20000
    args.reward_calibration_samples = (
        args.reward_calibration_samples if args.reward_calibration_samples is not None else 0)
    args.reward_calibration_horizon = (
        args.reward_calibration_horizon if args.reward_calibration_horizon is not None
        else args.max_dream_steps)
    args.dream_gate = bool(args.dream_gate) if args.dream_gate is not None else False
    args.dream_gate_miss_rate = (
        args.dream_gate_miss_rate if args.dream_gate_miss_rate is not None else 0.25)
    args.dream_gate_false_positive_rate = (
        args.dream_gate_false_positive_rate
        if args.dream_gate_false_positive_rate is not None else 0.20)
    args.dream_gate_sign_accuracy = (
        args.dream_gate_sign_accuracy if args.dream_gate_sign_accuracy is not None else 0.80)
    args.dream_gate_real_eval_episodes = (
        args.dream_gate_real_eval_episodes
        if args.dream_gate_real_eval_episodes is not None else 0)
    args.wandb_project = args.wandb_project or "sls-wm-atari"
    args.wandb_name = args.wandb_name or f"actor-dream-{Path(args.checkpoint_dir).name}"
    args.amp_dtype = args.amp_dtype or "bfloat16"
    args.compile_mode = args.compile_mode or "reduce-overhead"

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    rng = np.random.default_rng(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    amp = amp_dtype(args.amp_dtype)
    ckpt_dir = Path(args.checkpoint_dir)
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    replay = load_replay_arrays(args.replay_dir)
    metadata = load_metadata(args.replay_dir) or {}
    n_actions = int(metadata.get("n_actions", pred_cfg.get("n_actions", 6)))
    print(f"Replay: {args.replay_dir} steps={len(replay.obs)} n_actions={n_actions}")

    fsq = FSQVAE(
        img_channels=int(fsq_cfg.get("img_channels", 3)),
        levels=fsq_cfg.get("levels", [8, 5, 5, 5]),
        norm_type=fsq_cfg.get("norm_type", "group"),
        latent_grid=int(fsq_cfg.get("latent_grid", 16)),
    ).to(device)
    fsq.load_state_dict(load_clean_state(args.fsq_checkpoint, device))
    fsq.eval()
    tokens = encode_replay_obs(fsq, replay.obs, batch_size=256, device=device)
    context_frames = int(model_cfg.get("context_frames", 4))
    starts = valid_context_starts(replay, context_frames)
    event_starts = reward_event_context_starts(
        replay, context_frames, args.reward_event_min_gap,
        args.reward_event_max_gap)
    print(
        "Dream starts: "
        f"uniform={len(starts)} reward_pos={len(event_starts[1])} "
        f"reward_neg={len(event_starts[-1])} "
        f"event_frac={args.reward_event_sample_frac:.2f} "
        f"gap=[K+{args.reward_event_min_gap}, K+{args.reward_event_max_gap}]"
    )
    wandb_init(
        project=args.wandb_project,
        name=args.wandb_name,
        config={**vars(args), "replay_steps": len(replay.obs), "n_actions": n_actions},
    )

    world_model = WorldModel(
        vocab_size=int(model_cfg.get("vocab_size", 1000)),
        n_actions=n_actions,
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
    ).to(device)
    state = load_clean_state(args.predictor_checkpoint, device)
    if "world_model.head.weight" in state:
        load_matching_state_dict(predictor, state, strict=False)
    else:
        wm_state, aux_state = split_atari_predictor_state(state)
        predictor.world_model.load_state_dict(wm_state)
        load_matching_state_dict(predictor, aux_state, strict=False)
    predictor.eval()
    for p in predictor.parameters():
        p.requires_grad_(False)

    policy = AtariCNNPolicy(
        vocab_size=int(model_cfg.get("vocab_size", 1000)),
        n_actions=n_actions,
        grid_size=int(fsq_cfg.get("latent_grid", 16)),
        h_dim=int(model_cfg.get("embed_dim", 384)),
        value_head_type=args.value_head_type,
        value_bins=args.value_twohot_bins,
        value_low=args.value_twohot_low,
        value_high=args.value_twohot_high,
    ).to(device)
    ema_policy = copy.deepcopy(policy).eval() if args.ema_decay > 0 else None
    if args.pretrained and Path(args.pretrained).exists():
        load_policy_state_flexible(policy, args.pretrained, device)
        if ema_policy is not None:
            ema_policy.load_state_dict(policy.state_dict())
        print(f"Loaded pretrained actor from {args.pretrained}")
    elif args.pretrained:
        print(f"Pretrained actor not found at {args.pretrained}; starting cold")

    latest = ckpt_dir / "actor_dream_latest.pt"
    best_real_path = ckpt_dir / "actor_dream_best_real.pt"
    best_real_metric = -float("inf")
    global_best_real_path = Path(args.global_best_real_checkpoint) if args.global_best_real_checkpoint else (
        ckpt_dir / "actor_dream_global_best_real.pt")
    global_best_real_meta = Path(args.global_best_real_metadata) if args.global_best_real_metadata else (
        ckpt_dir / "actor_dream_global_best_real.json")
    global_best_real_metric = -float("inf")
    if global_best_real_meta.exists():
        try:
            global_best_real_metric = float(json.loads(
                global_best_real_meta.read_text()).get("mean_return", global_best_real_metric))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            print(f"Could not read global best metadata at {global_best_real_meta}; resetting metric")
    optimizer = torch.optim.Adam(policy.parameters(), lr=args.lr)
    start_iter = 1
    if args.init_from:
        load_policy_state_flexible(policy, args.init_from, device)
        if ema_policy is not None:
            ema_policy.load_state_dict(policy.state_dict())
        print(f"Warm-started dream actor weights from {args.init_from}; "
              "reset optimizer/iteration/RNG state")
    elif args.resume and latest.exists():
        ckpt = torch.load(latest, map_location=device, weights_only=False)
        load_module_state_matching(policy, ckpt["controller"], label="actor")
        if ema_policy is not None:
            load_module_state_matching(
                ema_policy, ckpt.get("ema_controller", policy.state_dict()),
                label="ema_actor")
        optimizer.load_state_dict(ckpt["optimizer"])
        start_iter = int(ckpt["iteration"]) + 1
        rng.bit_generator.state = ckpt["rng_state"]
        best_real_metric = float(ckpt.get("best_real_mean_return", best_real_metric))
        global_best_real_metric = float(ckpt.get(
            "global_best_real_mean_return", global_best_real_metric))
        print(f"Resumed dream actor from iteration {start_iter - 1}")
    return_normalizer = PercentileNormalizer(momentum=args.return_normalizer_momentum)
    if args.resume and latest.exists() and not args.init_from:
        return_normalizer.load_state_dict(ckpt.get("return_normalizer"))

    if args.compile_mode != "none" and device.type == "cuda":
        predictor = torch.compile(predictor, mode=args.compile_mode)
        policy = torch.compile(policy, mode=args.compile_mode)
        print(f"torch.compile enabled (mode={args.compile_mode})")

    calibration_starts = np.concatenate([
        event_starts[1],
        event_starts[-1],
        starts,
    ])
    calibration = calibrate_dream_rewards(
        predictor, calibration_starts, tokens, replay.actions, replay.rewards, context_frames,
        args, device, amp, rng)
    if calibration is not None:
        cal_path = ckpt_dir / "reward_calibration.csv"
        with open(cal_path, "w", newline="") as f:
            writer = csv.DictWriter(
                f, fieldnames=["start", "true_gap", "true_sign", "pred_gap", "pred_sign"])
            writer.writeheader()
            writer.writerows(calibration["rows"])
        print(
            "Reward calibration: "
            f"events={calibration['event_cases']} neutral={calibration['neutral_cases']} "
            f"sign_acc={calibration['sign_accuracy']:.3f} "
            f"miss_rate={calibration['miss_rate']:.3f} "
            f"false_pos={calibration['false_positive_rate']:.3f} "
            f"timing_err={calibration['mean_timing_error']:.2f} "
            f"path={cal_path}"
        )
        wandb_log({
            "actor_dream/reward_calibration_event_cases": calibration["event_cases"],
            "actor_dream/reward_calibration_neutral_cases": calibration["neutral_cases"],
            "actor_dream/reward_calibration_sign_accuracy": calibration["sign_accuracy"],
            "actor_dream/reward_calibration_miss_rate": calibration["miss_rate"],
            "actor_dream/reward_calibration_false_positive_rate": calibration["false_positive_rate"],
            "actor_dream/reward_calibration_mean_timing_error": calibration["mean_timing_error"],
        })

    if args.dream_gate:
        gate = {
            "dream_ran": True,
            "passed": True,
            "reason": "",
            "thresholds": {
                "miss_rate": args.dream_gate_miss_rate,
                "false_positive_rate": args.dream_gate_false_positive_rate,
                "sign_accuracy": args.dream_gate_sign_accuracy,
            },
            "replay_reward_calibration": calibration,
            "actor_action_reward_calibration": None,
        }
        if calibration is None or calibration.get("event_cases", 0) <= 0:
            gate.update({"passed": False, "reason": "missing_replay_reward_calibration"})
        elif (
            calibration["miss_rate"] > args.dream_gate_miss_rate
            or calibration["false_positive_rate"] > args.dream_gate_false_positive_rate
            or calibration["sign_accuracy"] < args.dream_gate_sign_accuracy
        ):
            gate.update({"passed": False, "reason": "replay_reward_calibration_failed"})
        if gate["passed"] and args.dream_gate_real_eval_episodes > 0:
            prev_episodes = args.real_eval_episodes
            args.real_eval_episodes = int(args.dream_gate_real_eval_episodes)
            metrics = evaluate_real_env(
                policy, fsq, predictor, atari_cfg, model_cfg, args, device, amp,
                n_actions)
            args.real_eval_episodes = prev_episodes
            gate["actor_action_reward_calibration"] = metrics
            if (
                metrics["reward_miss_rate"] > args.dream_gate_miss_rate
                or metrics["reward_false_positive_rate"] > args.dream_gate_false_positive_rate
                or metrics["reward_sign_accuracy"] < args.dream_gate_sign_accuracy
            ):
                gate.update({"passed": False, "reason": "actor_action_reward_calibration_failed"})
        gate_path = ckpt_dir / "actor_dream_gate.json"
        gate_path.write_text(json.dumps(gate, indent=2))
        wandb_log({
            "actor_dream/gate_passed": int(gate["passed"]),
            "actor_dream/gate_dream_ran": int(gate["dream_ran"] and gate["passed"]),
        })
        if not gate["passed"]:
            clean_policy = {k.removeprefix("_orig_mod."): v for k, v in policy.state_dict().items()}
            torch.save(clean_policy, ckpt_dir / "actor_dream_final.pt")
            gate["dream_ran"] = False
            gate_path.write_text(json.dumps(gate, indent=2))
            print(f"Dream gate failed ({gate['reason']}); skipped dream PPO. path={gate_path}")
            wandb_finish()
            return

    log_path = ckpt_dir / "actor_dream_log.csv"
    log_file = open(log_path, "a" if (args.resume or args.init_from) and log_path.exists() else "w", newline="")
    log = csv.writer(log_file)
    if log_file.tell() == 0:
        log.writerow(["iteration", "mean_return", "mean_dream_steps",
                      "ppo_loss", "entropy",
                      "real_eval_mean_return", "real_eval_mean_length",
                      "real_eval_action_counts",
                      "real_eval_reward_true_events",
                      "real_eval_reward_pred_events",
                      "real_eval_reward_miss_rate",
                      "real_eval_reward_false_positive_rate",
                      "time_s"])

    try:
        for iteration in range(start_iter, args.n_iterations + 1):
            t0 = time.time()
            ctx_tokens, ctx_actions, sample_info = sample_contexts(
                starts, tokens, replay.actions, context_frames,
                args.n_episodes, rng, event_starts,
                args.reward_event_sample_frac, args.reward_event_pos_frac)
            policy.eval()
            rollout, dream_return = dream_rollout(
                predictor, policy, ctx_tokens, ctx_actions, args, device, amp,
                value_policy=ema_policy)
            if rollout is None:
                print(f"iteration={iteration} empty dream rollout; skipping")
                continue
            policy.train()
            ppo_metrics = shared_ppo_update(
                policy, optimizer, rollout, args, device, amp,
                normalizer=return_normalizer, ema_policy=ema_policy)
            loss = ppo_metrics["loss"]
            entropy = ppo_metrics["entropy"]
            mean_return = float(dream_return.mean().item())
            mean_dream_steps = float(rollout.get("mean_active_steps", 0.0))
            clean_policy = {k.removeprefix("_orig_mod."): v for k, v in policy.state_dict().items()}
            real_eval_mean = ""
            real_eval_length = ""
            real_eval_actions = ""
            if args.real_eval_interval and (
                iteration == 1 or iteration % int(args.real_eval_interval) == 0
                or iteration == args.n_iterations
            ):
                metrics = evaluate_real_env(
                    policy, fsq, predictor, atari_cfg, model_cfg, args, device, amp,
                    n_actions)
                real_eval_mean = metrics["mean_return"]
                real_eval_length = metrics["mean_length"]
                real_eval_actions = metrics["action_counts"]
                if real_eval_mean > best_real_metric:
                    best_real_metric = real_eval_mean
                    torch.save(clean_policy, best_real_path)
                    print(f"  new best real eval: return={real_eval_mean:+.3f} "
                          f"checkpoint={best_real_path}")
                if real_eval_mean > global_best_real_metric:
                    global_best_real_metric = real_eval_mean
                    global_best_real_path.parent.mkdir(parents=True, exist_ok=True)
                    global_best_real_meta.parent.mkdir(parents=True, exist_ok=True)
                    torch.save(clean_policy, global_best_real_path)
                    global_best_real_meta.write_text(json.dumps({
                        "mean_return": float(real_eval_mean),
                        "iteration": int(iteration),
                        "checkpoint": str(global_best_real_path),
                        "local_checkpoint": str(best_real_path),
                        "source_checkpoint_dir": str(ckpt_dir),
                        "mean_length": float(real_eval_length),
                        "action_counts": real_eval_actions,
                    }, indent=2))
                    print(f"  new global best real eval: return={real_eval_mean:+.3f} "
                          f"checkpoint={global_best_real_path}")
                print(f"  real_eval return={real_eval_mean:+.3f} "
                      f"len={real_eval_length:.1f} actions={real_eval_actions} "
                      f"reward_head={{'true': {metrics['reward_true_events']}, "
                      f"'pred': {metrics['reward_pred_events']}, "
                      f"'miss': {metrics['reward_miss_rate']:.3f}, "
                      f"'false_pos': {metrics['reward_false_positive_rate']:.3f}}}")
            elapsed = time.time() - t0
            print(f"iter={iteration} return={mean_return:+.3f} loss={loss:.4f} "
                  f"entropy={entropy:.3f} dream_steps={mean_dream_steps:.1f} "
                  f"samples={sample_info} time={elapsed:.1f}s")
            log.writerow([
                iteration, f"{mean_return:.6f}", f"{mean_dream_steps:.3f}",
                f"{loss:.6f}", f"{entropy:.6f}",
                "" if real_eval_mean == "" else f"{real_eval_mean:.6f}",
                "" if real_eval_length == "" else f"{real_eval_length:.1f}",
                real_eval_actions,
                "" if real_eval_mean == "" else metrics["reward_true_events"],
                "" if real_eval_mean == "" else metrics["reward_pred_events"],
                "" if real_eval_mean == "" else f"{metrics['reward_miss_rate']:.6f}",
                "" if real_eval_mean == "" else f"{metrics['reward_false_positive_rate']:.6f}",
                f"{elapsed:.1f}",
            ])
            log_file.flush()
            payload = {
                "actor_dream/iteration": iteration,
                "actor_dream/mean_return": mean_return,
                "actor_dream/mean_dream_steps": mean_dream_steps,
                "actor_dream/ppo_loss": loss,
                "actor_dream/actor_loss": ppo_metrics["actor_loss"],
                "actor_dream/value_loss": ppo_metrics["critic_loss"],
                "actor_dream/value_mean": ppo_metrics["value_mean"],
                "actor_dream/return_normalizer_scale": return_normalizer.scale,
                "actor_dream/entropy": entropy,
                "actor_dream/sample_pos": sample_info["pos"],
                "actor_dream/sample_neg": sample_info["neg"],
                "actor_dream/sample_uniform": sample_info["uniform"],
                "actor_dream/time_s": elapsed,
            }
            if real_eval_mean != "":
                payload["actor_dream/real_eval_mean_return"] = float(real_eval_mean)
                payload["actor_dream/real_eval_mean_length"] = float(real_eval_length)
                payload["actor_dream/real_eval_reward_true_events"] = int(metrics["reward_true_events"])
                payload["actor_dream/real_eval_reward_pred_events"] = int(metrics["reward_pred_events"])
                payload["actor_dream/real_eval_reward_sign_accuracy"] = float(metrics["reward_sign_accuracy"])
                payload["actor_dream/real_eval_reward_miss_rate"] = float(metrics["reward_miss_rate"])
                payload["actor_dream/real_eval_reward_false_positive_rate"] = float(metrics["reward_false_positive_rate"])
                payload["actor_dream/global_best_real_mean_return"] = float(global_best_real_metric)
                for action_name, count in real_eval_actions.items():
                    payload[f"actor_dream/real_eval_actions/{action_name}"] = int(count)
            wandb_log(payload)

            torch.save({
                "iteration": iteration,
                "controller": clean_policy,
                "optimizer": optimizer.state_dict(),
                "ema_controller": None if ema_policy is None else {
                    k.removeprefix("_orig_mod."): v for k, v in ema_policy.state_dict().items()},
                "return_normalizer": return_normalizer.state_dict(),
                "rng_state": rng.bit_generator.state,
                "best_real_mean_return": best_real_metric,
                "global_best_real_mean_return": global_best_real_metric,
            }, latest)
            if iteration == 1 or iteration % 10 == 0:
                torch.save(clean_policy, ckpt_dir / "actor_dream_best_effort.pt")

        clean_policy = {k.removeprefix("_orig_mod."): v for k, v in policy.state_dict().items()}
        torch.save(clean_policy, ckpt_dir / "actor_dream_final.pt")
        print(f"Done. Checkpoints saved to {ckpt_dir}")
    finally:
        log_file.close()
        wandb_finish()


if __name__ == "__main__":
    main()

"""Train/deploy an Atari actor in the real env while appending replay.

This is the real-environment half of the Atari100K cycle. It loads a frozen
FSQ tokenizer and frozen Atari predictor, uses their token grid + hidden state
as policy features, updates a categorical actor-critic from real PPO rollouts,
and appends the same transitions to replay.
"""

from __future__ import annotations

import argparse
import copy
import csv
import sys
import time
from pathlib import Path

import gymnasium as gym
import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image

import ale_py

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atari.predictor import split_atari_predictor_state
from atari.actor_critic import compute_lambda_returns, ppo_update as shared_ppo_update
from atari.controller import AtariCNNPolicy
from atari.rl_targets import PercentileNormalizer
from atari.replay_buffer import ReplayShardWriter, load_metadata
from deepdash.config import apply_config, load_config
from deepdash.fsq import FSQVAE
from deepdash.wandb_utils import wandb_finish, wandb_init, wandb_log
from deepdash.world_model import WorldModel

gym.register_envs(ale_py)
RESAMPLE_BILINEAR = Image.Resampling.BILINEAR if hasattr(Image, "Resampling") else Image.BILINEAR


def resize_frame_to_64(obs):
    return np.asarray(Image.fromarray(obs).resize((64, 64), RESAMPLE_BILINEAR), dtype=np.uint8)


def amp_dtype(name):
    if name == "bfloat16":
        return torch.bfloat16
    if name == "float16":
        return torch.float16
    return None


@torch.no_grad()
def encode_frame(fsq, frame, device):
    x = torch.from_numpy(frame).float().permute(2, 0, 1).unsqueeze(0).to(device) / 255.0
    return fsq.encode(x).reshape(1, -1)


def load_clean_state(path, device):
    state = torch.load(path, map_location=device, weights_only=False)
    if isinstance(state, dict) and "model" in state:
        state = state["model"]
    if isinstance(state, dict) and "controller" in state:
        state = state["controller"]
    return {k.removeprefix("_orig_mod."): v for k, v in state.items()}


def load_policy_state_flexible(policy, path, device):
    state = load_clean_state(path, device)
    load_module_state_matching(policy, state, label="actor")


def load_module_state_matching(module, state, label="module"):
    if state is None:
        return
    own = module.state_dict()
    matched = {k: v for k, v in state.items()
               if k in own and tuple(own[k].shape) == tuple(v.shape)}
    skipped = sorted(k for k, v in state.items()
                     if k in own and tuple(own[k].shape) != tuple(v.shape))
    module.load_state_dict(matched, strict=False)
    if skipped:
        print(f"Skipped resized {label} tensors: {skipped}")


def parse_action_subset(value, n_actions: int) -> list[int]:
    if value is None or str(value).strip() == "":
        return list(range(int(n_actions)))
    subset = [int(part.strip()) for part in str(value).split(",") if part.strip()]
    if not subset:
        raise ValueError("policy action subset is empty")
    if len(set(subset)) != len(subset):
        raise ValueError(f"policy action subset has duplicates: {subset}")
    invalid = [action for action in subset if action < 0 or action >= int(n_actions)]
    if invalid:
        raise ValueError(
            f"policy action subset contains invalid env actions {invalid} "
            f"for n_actions={n_actions}")
    return subset


def compute_gae(rewards, values, dones, bootstrap_value, gamma, lam, device):
    rewards = torch.tensor(rewards, dtype=torch.float32, device=device)
    values = torch.tensor(values, dtype=torch.float32, device=device)
    dones = torch.tensor(dones, dtype=torch.float32, device=device)
    adv = torch.zeros_like(rewards)
    gae = torch.zeros((), device=device)
    next_value = bootstrap_value
    for t in reversed(range(len(rewards))):
        next_nonterminal = 1.0 - dones[t]
        delta = rewards[t] + gamma * next_value * next_nonterminal - values[t]
        gae = delta + gamma * lam * next_nonterminal * gae
        adv[t] = gae
        next_value = values[t]
    returns = adv + values
    return adv, returns


def ppo_update(policy, optimizer, batch, args, device, amp):
    tokens = torch.cat(batch["tokens"], dim=0).to(device)
    h = torch.cat(batch["h"], dim=0).to(device)
    actions = torch.tensor(batch["actions"], dtype=torch.long, device=device)
    old_logp = torch.tensor(batch["logp"], dtype=torch.float32, device=device)
    adv = batch["advantages"].to(device)
    returns = batch["returns"].to(device)
    adv = (adv - adv.mean()) / (adv.std(unbiased=False) + 1e-8)

    n = actions.numel()
    idx = torch.arange(n, device=device)
    total_loss = 0.0
    total_entropy = 0.0
    updates = 0
    for _ in range(args.ppo_epochs):
        perm = idx[torch.randperm(n, device=device)]
        for start in range(0, n, args.minibatch_size):
            mb = perm[start:start + args.minibatch_size]
            with torch.amp.autocast("cuda", enabled=amp is not None, dtype=amp):
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


def scalar_stats(values, prefix: str) -> dict[str, float]:
    values = torch.as_tensor(values, dtype=torch.float32)
    if values.numel() == 0:
        return {
            f"{prefix}_mean": 0.0,
            f"{prefix}_std": 0.0,
            f"{prefix}_min": 0.0,
            f"{prefix}_max": 0.0,
        }
    return {
        f"{prefix}_mean": float(values.mean().item()),
        f"{prefix}_std": float(values.std(unbiased=False).item()),
        f"{prefix}_min": float(values.min().item()),
        f"{prefix}_max": float(values.max().item()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/atari/atari_pong_h200.yaml")
    parser.add_argument("--config-section", default="actor_real")
    parser.add_argument("--game", default=None)
    parser.add_argument("--replay-dir", default=None)
    parser.add_argument("--checkpoint-dir", default=None)
    parser.add_argument("--fsq-checkpoint", default=None)
    parser.add_argument("--predictor-checkpoint", default=None)
    parser.add_argument("--n-steps", type=int, default=None)
    parser.add_argument("--target-replay-steps", type=int, default=None,
                        help="Collect only until replay metadata reaches this global step count.")
    parser.add_argument("--rollout-steps", type=int, default=None)
    parser.add_argument("--lr", type=float, default=None)
    parser.add_argument("--gamma", type=float, default=None)
    parser.add_argument("--lam", type=float, default=None)
    parser.add_argument("--clip-eps", type=float, default=None)
    parser.add_argument("--ppo-epochs", type=int, default=None)
    parser.add_argument("--minibatch-size", type=int, default=None)
    parser.add_argument("--actor-update", choices=["ppo", "iris_pg"], default=None)
    parser.add_argument("--entropy-coeff", type=float, default=None)
    parser.add_argument("--critic-coeff", type=float, default=None)
    parser.add_argument("--max-grad-norm", type=float, default=None)
    parser.add_argument("--value-head-type", choices=["scalar", "twohot"], default=None)
    parser.add_argument("--value-twohot-bins", type=int, default=None)
    parser.add_argument("--value-twohot-low", type=float, default=None)
    parser.add_argument("--value-twohot-high", type=float, default=None)
    parser.add_argument("--return-normalizer-momentum", type=float, default=None)
    parser.add_argument("--ema-decay", type=float, default=None)
    parser.add_argument("--amp-dtype", choices=["none", "float16", "bfloat16"], default=None)
    parser.add_argument("--compile-mode", choices=["none", "default", "reduce-overhead"], default=None)
    parser.add_argument("--wandb-project", default=None)
    parser.add_argument("--wandb-name", default=None)
    parser.add_argument("--policy-action-subset", default=None,
                        help="Comma-separated ALE env action ids exposed to the policy. "
                             "Replay and predictor context still use original env ids.")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--init-from", default=None,
                        help="Warm-start actor weights from a checkpoint, but reset optimizer/update state.")
    parser.add_argument("--collect-only", action="store_true",
                        help="Append real replay with the current policy without actor updates.")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    apply_config(args, section=args.config_section)

    atari_cfg = load_config(args.config, section="atari")
    fsq_cfg = load_config(args.config, section="fsq")
    model_cfg = load_config(args.config, section="model")
    pred_cfg = load_config(args.config, section="predictor")

    args.game = args.game or atari_cfg.get("game", "Pong")
    args.replay_dir = args.replay_dir or atari_cfg.get("replay_dir", f"data/atari/{args.game}/replay")
    args.checkpoint_dir = args.checkpoint_dir or f"checkpoints_atari_{args.game.lower()}_actor_real"
    args.fsq_checkpoint = args.fsq_checkpoint or pred_cfg.get("fsq_checkpoint")
    args.predictor_checkpoint = args.predictor_checkpoint or str(Path(pred_cfg.get("checkpoint_dir")) / "predictor_best.pt")
    args.n_steps = args.n_steps or int(atari_cfg.get("cycle_steps", 10000))
    args.rollout_steps = args.rollout_steps or 256
    args.lr = args.lr or 2.5e-4
    args.gamma = args.gamma if args.gamma is not None else 0.997
    args.lam = args.lam if args.lam is not None else 0.95
    args.clip_eps = args.clip_eps if args.clip_eps is not None else 0.2
    args.ppo_epochs = args.ppo_epochs or 4
    args.minibatch_size = args.minibatch_size or 256
    args.actor_update = args.actor_update or "ppo"
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
    args.amp_dtype = args.amp_dtype or "bfloat16"
    args.compile_mode = args.compile_mode or "reduce-overhead"
    args.wandb_project = args.wandb_project or "sls-wm-atari"
    args.wandb_name = args.wandb_name or f"actor-real-{Path(args.checkpoint_dir).name}"
    args.seed = args.seed if args.seed is not None else int(atari_cfg.get("seed", 42))

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    amp = amp_dtype(args.amp_dtype)
    ckpt_dir = Path(args.checkpoint_dir)
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    env = gym.make(
        f"ALE/{args.game}-v5",
        frameskip=int(atari_cfg.get("frame_skip", 4)),
        repeat_action_probability=float(atari_cfg.get("repeat_action_probability", 0.0)),
    )
    n_actions = int(env.action_space.n)
    action_subset = parse_action_subset(args.policy_action_subset, n_actions)
    policy_n_actions = len(action_subset)
    print(f"Env actions={n_actions} policy_actions={policy_n_actions} subset={action_subset}")
    metadata = load_metadata(args.replay_dir) or {}
    before_replay_steps = int((metadata or {}).get("total_steps", 0))
    if args.target_replay_steps is not None:
        remaining = int(args.target_replay_steps) - before_replay_steps
        if remaining <= 0:
            print(f"Replay already at {before_replay_steps} steps; target={args.target_replay_steps}. Nothing to collect.")
            env.close()
            return
        args.n_steps = min(int(args.n_steps), remaining)
        print(f"Global replay target enabled: before={before_replay_steps} "
              f"target={args.target_replay_steps} local_n_steps={args.n_steps}")

    writer = ReplayShardWriter(
        args.replay_dir,
        shard_size=int(atari_cfg.get("shard_size", metadata.get("shard_size", 8192))),
        metadata={
            "game": args.game,
            "env_id": f"ALE/{args.game}-v5",
            "n_actions": n_actions,
            "policy_action_subset": action_subset,
            "frame_skip": int(atari_cfg.get("frame_skip", 4)),
            "repeat_action_probability": float(atari_cfg.get("repeat_action_probability", 0.0)),
            "obs_shape": [64, 64, 3],
            "obs_dtype": "uint8",
            "seed": args.seed,
        },
    )

    fsq = FSQVAE(
        img_channels=int(fsq_cfg.get("img_channels", 3)),
        levels=fsq_cfg.get("levels", [8, 5, 5, 5]),
        norm_type=fsq_cfg.get("norm_type", "group"),
        latent_grid=int(fsq_cfg.get("latent_grid", 16)),
    ).to(device)
    fsq.load_state_dict(load_clean_state(args.fsq_checkpoint, device))
    fsq.eval()

    predictor = WorldModel(
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
    pred_state = load_clean_state(args.predictor_checkpoint, device)
    pred_state, _ = split_atari_predictor_state(pred_state)
    predictor.load_state_dict(pred_state)
    predictor.eval()

    policy = AtariCNNPolicy(
        vocab_size=int(model_cfg.get("vocab_size", 1000)),
        n_actions=policy_n_actions,
        grid_size=int(fsq_cfg.get("latent_grid", 16)),
        h_dim=int(model_cfg.get("embed_dim", 384)),
        value_head_type=args.value_head_type,
        value_bins=args.value_twohot_bins,
        value_low=args.value_twohot_low,
        value_high=args.value_twohot_high,
    ).to(device)
    ema_policy = copy.deepcopy(policy).eval() if args.ema_decay > 0 else None
    latest = ckpt_dir / "actor_real_latest.pt"
    start_real_step = int(writer.metadata.get("total_steps", 0))
    if args.init_from:
        load_policy_state_flexible(policy, args.init_from, device)
        if ema_policy is not None:
            ema_policy.load_state_dict(policy.state_dict())
        print(f"Warm-started actor weights from {args.init_from}; reset optimizer/update state")
    elif args.resume and latest.exists():
        state = torch.load(latest, map_location=device, weights_only=False)
        load_policy_state_flexible(policy, latest, device)
        if ema_policy is not None:
            load_module_state_matching(
                ema_policy, state.get("ema_controller", policy.state_dict()), label="ema_actor")
        start_real_step = int(state.get("real_steps", start_real_step))
        print(f"Resumed actor from {latest} real_steps={start_real_step}")

    if args.compile_mode != "none" and device.type == "cuda":
        fsq.encode = torch.compile(fsq.encode, mode=args.compile_mode)
        predictor = torch.compile(predictor, mode=args.compile_mode)
        policy = torch.compile(policy, mode=args.compile_mode)
        print(f"torch.compile enabled (mode={args.compile_mode})")

    optimizer = torch.optim.Adam(policy.parameters(), lr=args.lr)
    if args.resume and not args.init_from and latest.exists():
        state = torch.load(latest, map_location=device, weights_only=False)
        if "optimizer" in state:
            optimizer.load_state_dict(state["optimizer"])
    return_normalizer = PercentileNormalizer(momentum=args.return_normalizer_momentum)
    if args.resume and not args.init_from and latest.exists():
        return_normalizer.load_state_dict(state.get("return_normalizer"))

    log_path = ckpt_dir / "actor_real_log.csv"
    log_file = open(log_path, "a" if (args.resume or args.init_from) and log_path.exists() else "w", newline="")
    log = csv.writer(log_file)
    if log_file.tell() == 0:
        log.writerow([
            "update", "real_steps", "episode_return", "rollout_reward_sum",
            "nonzero_rewards", "adv_mean", "adv_std", "adv_min", "adv_max",
            "return_mean", "return_std", "value_mean_before", "value_std_before",
            "ppo_loss", "actor_loss", "value_loss", "entropy", "approx_kl",
            "clipfrac", "ratio_mean", "ratio_min", "ratio_max", "time_s",
        ])
    wandb_init(
        project=args.wandb_project,
        name=args.wandb_name,
        config={**vars(args), "before_replay_steps": before_replay_steps, "n_actions": n_actions},
    )

    obs, _ = env.reset(seed=args.seed)
    frame = resize_frame_to_64(obs)
    k = int(model_cfg.get("context_frames", 4))
    def reset_context(frame64):
        token = encode_frame(fsq, frame64, device).cpu()
        return [token.clone() for _ in range(k)], [0 for _ in range(k)]

    ctx_tokens, ctx_actions = reset_context(frame)
    episode_frames, episode_actions, episode_rewards, episode_dones = [], [], [], []
    episode_return = 0.0
    rng = np.random.default_rng(args.seed)

    real_steps = 0
    update_idx = 0
    t0 = time.time()
    rollout = {"tokens": [], "h": [], "actions": [], "logp": [], "values": [],
               "rewards": [], "dones": []}

    try:
      while real_steps < args.n_steps:
        ctx_t = torch.stack(ctx_tokens[-k:], dim=1).to(device)
        ctx_a = torch.tensor([ctx_actions[-k:]], dtype=torch.long, device=device)
        with torch.no_grad(), torch.amp.autocast("cuda", enabled=amp is not None, dtype=amp):
            h_t = predictor.encode_context(
                ctx_t, ctx_a, return_action_hidden=False)
            token_t = ctx_t[:, -1]
            policy_action_t, logp_t, _, value_t = policy.act(token_t, h_t.float())
        action = int(action_subset[int(policy_action_t.item())])

        obs, reward, terminated, truncated, _ = env.step(action)
        done = bool(terminated or truncated)
        next_frame = resize_frame_to_64(obs)

        episode_frames.append(frame)
        episode_actions.append(action)
        episode_rewards.append(float(reward))
        episode_dones.append(done)
        episode_return += float(reward)

        rollout["tokens"].append(token_t.detach().cpu())
        rollout["h"].append(h_t.detach().cpu().float())
        rollout["actions"].append(int(policy_action_t.item()))
        rollout["logp"].append(float(logp_t.item()))
        rollout["values"].append(float(value_t.item()))
        rollout["rewards"].append(float(reward))
        rollout["dones"].append(done)

        next_token = encode_frame(fsq, next_frame, device).cpu()
        ctx_tokens.append(next_token)
        ctx_actions.append(action)
        frame = next_frame
        real_steps += 1

        if done:
            writer.append_episode(episode_frames, episode_actions, episode_rewards, episode_dones)
            print(f"episode return={episode_return:+.1f} replay_steps={writer.total_steps}")
            wandb_log({
                "actor_real/episode_return": episode_return,
                "actor_real/replay_steps": writer.total_steps,
                "actor_real/local_real_steps": real_steps,
            })
            episode_frames, episode_actions, episode_rewards, episode_dones = [], [], [], []
            episode_return = 0.0
            obs, _ = env.reset(seed=int(rng.integers(0, 2**31 - 1)))
            frame = resize_frame_to_64(obs)
            ctx_tokens, ctx_actions = reset_context(frame)

        if args.collect_only:
            rollout = {"tokens": [], "h": [], "actions": [], "logp": [], "values": [],
                       "rewards": [], "dones": []}
            continue

        if len(rollout["rewards"]) >= args.rollout_steps or real_steps >= args.n_steps:
            if len(rollout["rewards"]) < args.rollout_steps and real_steps >= args.n_steps:
                # Avoid a final short batch under torch.compile reduce-overhead:
                # cudagraph capture expects the same rollout shape as prior PPO updates.
                print(
                    f"Skipping final partial PPO rollout len={len(rollout['rewards'])} "
                    f"rollout_steps={args.rollout_steps}")
                rollout = {"tokens": [], "h": [], "actions": [], "logp": [], "values": [],
                           "rewards": [], "dones": []}
                continue
            with torch.no_grad():
                if rollout["dones"] and rollout["dones"][-1]:
                    bootstrap = torch.zeros((), device=device)
                else:
                    ctx_t = torch.stack(ctx_tokens[-k:], dim=1).to(device)
                    ctx_a = torch.tensor([ctx_actions[-k:]], dtype=torch.long, device=device)
                    h_t = predictor.encode_context(
                        ctx_t, ctx_a, return_action_hidden=False)
                    value_policy = ema_policy if ema_policy is not None else policy
                    _, bootstrap_value = value_policy(ctx_t[:, -1], h_t.float())
                    bootstrap = bootstrap_value.squeeze(0)
            rewards_t = torch.tensor(rollout["rewards"], dtype=torch.float32, device=device)
            values_t = torch.tensor(rollout["values"], dtype=torch.float32, device=device)
            dones_t = torch.tensor(rollout["dones"], dtype=torch.float32, device=device)
            adv, returns = compute_lambda_returns(
                rewards_t, values_t, dones_t, bootstrap, args.gamma, args.lam)
            rollout["advantages"] = adv.cpu()
            rollout["returns"] = returns.cpu()
            reward_sum = float(rewards_t.sum().item())
            nonzero_rewards = int((rewards_t != 0).sum().item())
            diag = {
                **scalar_stats(adv.cpu(), "adv"),
                **scalar_stats(returns.cpu(), "return"),
                **scalar_stats(values_t.cpu(), "value_before"),
                "rollout_reward_sum": reward_sum,
                "nonzero_rewards": nonzero_rewards,
            }
            ppo_metrics = shared_ppo_update(
                policy, optimizer, rollout, args, device, amp,
                normalizer=return_normalizer, ema_policy=ema_policy)
            loss = ppo_metrics["loss"]
            entropy = ppo_metrics["entropy"]
            update_idx += 1
            elapsed = time.time() - t0
            print(f"update={update_idx} real_steps={real_steps}/{args.n_steps} "
                  f"loss={loss:.4f} entropy={entropy:.3f} kl={ppo_metrics['approx_kl']:.5f} "
                  f"clip={ppo_metrics['clipfrac']:.3f} adv={diag['adv_mean']:.3f}/{diag['adv_std']:.3f} "
                  f"r_sum={reward_sum:+.1f} nz_r={nonzero_rewards} replay_steps={writer.total_steps}")
            log.writerow([
                update_idx, writer.total_steps, f"{episode_return:.3f}",
                f"{reward_sum:.3f}", nonzero_rewards,
                f"{diag['adv_mean']:.6f}", f"{diag['adv_std']:.6f}",
                f"{diag['adv_min']:.6f}", f"{diag['adv_max']:.6f}",
                f"{diag['return_mean']:.6f}", f"{diag['return_std']:.6f}",
                f"{diag['value_before_mean']:.6f}", f"{diag['value_before_std']:.6f}",
                f"{loss:.6f}", f"{ppo_metrics['actor_loss']:.6f}",
                f"{ppo_metrics['critic_loss']:.6f}", f"{entropy:.6f}",
                f"{ppo_metrics['approx_kl']:.6f}", f"{ppo_metrics['clipfrac']:.6f}",
                f"{ppo_metrics['ratio_mean']:.6f}", f"{ppo_metrics['ratio_min']:.6f}",
                f"{ppo_metrics['ratio_max']:.6f}", f"{elapsed:.1f}",
            ])
            log_file.flush()
            wandb_log({
                "actor_real/update": update_idx,
                "actor_real/replay_steps": writer.total_steps,
                "actor_real/local_real_steps": real_steps,
                "actor_real/episode_return_open": episode_return,
                "actor_real/ppo_loss": loss,
                "actor_real/actor_loss": ppo_metrics["actor_loss"],
                "actor_real/value_loss": ppo_metrics["critic_loss"],
                "actor_real/entropy": entropy,
                "actor_real/approx_kl": ppo_metrics["approx_kl"],
                "actor_real/clipfrac": ppo_metrics["clipfrac"],
                "actor_real/ratio_mean": ppo_metrics["ratio_mean"],
                "actor_real/ratio_min": ppo_metrics["ratio_min"],
                "actor_real/ratio_max": ppo_metrics["ratio_max"],
                "actor_real/value_mean": ppo_metrics["value_mean"],
                "actor_real/rollout_reward_sum": reward_sum,
                "actor_real/nonzero_rewards": nonzero_rewards,
                "actor_real/adv_mean": diag["adv_mean"],
                "actor_real/adv_std": diag["adv_std"],
                "actor_real/adv_min": diag["adv_min"],
                "actor_real/adv_max": diag["adv_max"],
                "actor_real/return_mean": diag["return_mean"],
                "actor_real/return_std": diag["return_std"],
                "actor_real/value_mean_before": diag["value_before_mean"],
                "actor_real/value_std_before": diag["value_before_std"],
                "actor_real/return_normalizer_scale": return_normalizer.scale,
                "actor_real/time_s": elapsed,
            })
            clean_policy = {k.removeprefix("_orig_mod."): v for k, v in policy.state_dict().items()}
            torch.save({
                "controller": clean_policy,
                "optimizer": optimizer.state_dict(),
                "ema_controller": None if ema_policy is None else {
                    k.removeprefix("_orig_mod."): v for k, v in ema_policy.state_dict().items()},
                "return_normalizer": return_normalizer.state_dict(),
                "real_steps": writer.total_steps,
                "update": update_idx,
            }, latest)
            torch.save(clean_policy, ckpt_dir / "actor_real_best_effort.pt")
            rollout = {"tokens": [], "h": [], "actions": [], "logp": [], "values": [],
                       "rewards": [], "dones": []}

      if episode_frames:
          writer.append_episode(episode_frames, episode_actions, episode_rewards, episode_dones)
      clean_policy = {k.removeprefix("_orig_mod."): v for k, v in policy.state_dict().items()}
      torch.save(clean_policy, ckpt_dir / "actor_real_final.pt")
      wandb_log({"actor_real/final_replay_steps": writer.metadata["total_steps"]})
      print(f"Done. Replay steps={writer.metadata['total_steps']} checkpoints={ckpt_dir}")
    finally:
      writer.close()
      env.close()
      log_file.close()
      wandb_finish()


if __name__ == "__main__":
    main()

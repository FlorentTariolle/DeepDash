"""Diagnose why the Atari controller is not learning.

This script intentionally does not train. It probes the real environment,
reward/done heads, action sensitivity, dream rewards, and actor policy
distribution using the same FSQ/predictor/controller checkpoints as the full
cycle.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import random
import sys
from pathlib import Path

import gymnasium as gym
import numpy as np
import torch
import ale_py

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atari.controller import AtariCNNPolicy
from atari.predictor import AtariPredictorWithHeads, split_atari_predictor_state
from atari.replay_buffer import load_replay_arrays
from deepdash.config import load_config
from deepdash.fsq import FSQVAE
from deepdash.world_model import WorldModel
from scripts.train_atari_actor_real import encode_frame, load_clean_state, resize_frame_to_64
from scripts.train_atari_actor_dream import valid_context_starts
from scripts.train_atari_predictor import encode_replay_obs

gym.register_envs(ale_py)

ACTION_NAMES = ["NOOP", "FIRE", "RIGHT", "LEFT", "RIGHTFIRE", "LEFTFIRE"]


def finite(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def stats(xs):
    arr = np.asarray(xs, dtype=np.float64)
    if arr.size == 0:
        return {"n": 0}
    return {
        "n": int(arr.size),
        "mean": finite(float(arr.mean())),
        "std": finite(float(arr.std())),
        "min": finite(float(arr.min())),
        "p50": finite(float(np.percentile(arr, 50))),
        "p90": finite(float(np.percentile(arr, 90))),
        "max": finite(float(arr.max())),
    }


def deep_get(cfg, dotted, default=None):
    cur = cfg
    for key in dotted.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def make_env(game, atari_cfg):
    return gym.make(
        f"ALE/{game}-v5",
        frameskip=int(atari_cfg.get("frame_skip", 4)),
        repeat_action_probability=float(atari_cfg.get("repeat_action_probability", 0.0)),
    )


def eval_policy_fn(env, policy_fn, n_episodes, max_steps, seed):
    rng = np.random.default_rng(seed)
    returns, lengths = [], []
    counts = collections.Counter()
    for _ in range(n_episodes):
        obs, _ = env.reset(seed=int(rng.integers(0, 2**31 - 1)))
        total = 0.0
        steps = 0
        for _ in range(max_steps):
            action = int(policy_fn(obs, steps))
            counts[action] += 1
            obs, reward, terminated, truncated, _ = env.step(action)
            total += float(reward)
            steps += 1
            if terminated or truncated:
                break
        returns.append(total)
        lengths.append(steps)
    return {
        "returns": returns,
        "lengths": lengths,
        "mean_return": float(np.mean(returns)) if returns else 0.0,
        "action_counts": {
            ACTION_NAMES[a] if a < len(ACTION_NAMES) else str(a): int(counts[a])
            for a in range(env.action_space.n)
        },
    }


def real_env_baselines(game, atari_cfg, args):
    env = make_env(game, atari_cfg)
    out = {"action_meanings": env.unwrapped.get_action_meanings()}
    n_actions = int(env.action_space.n)
    for action in range(n_actions):
        out[f"constant_{ACTION_NAMES[action] if action < len(ACTION_NAMES) else action}"] = eval_policy_fn(
            env, lambda _obs, _step, a=action: a,
            args.baseline_episodes, args.max_steps_per_episode, args.seed + 1000 + action)
    rng = np.random.default_rng(args.seed + 2000)
    out["random"] = eval_policy_fn(
        env, lambda _obs, _step: int(rng.integers(0, n_actions)),
        args.random_episodes, args.max_steps_per_episode, args.seed + 3000)
    env.close()
    return out


def build_models(args, device):
    atari_cfg = load_config(args.config, section="atari")
    fsq_cfg = load_config(args.config, section="fsq")
    model_cfg = load_config(args.config, section="model")
    pred_cfg = load_config(args.config, section=args.predictor_section)
    actor_cfg = load_config(args.config, section="actor_dream")
    game = atari_cfg.get("game", "Pong")

    fsq_ckpt = args.fsq_checkpoint or pred_cfg.get("fsq_checkpoint")
    predictor_ckpt = args.predictor_checkpoint or str(
        Path(pred_cfg.get("checkpoint_dir")) / "predictor_best.pt")
    actor_ckpt = args.actor_checkpoint or actor_cfg.get("actor_checkpoint") or str(
        Path(actor_cfg.get("checkpoint_dir", "checkpoints_atari_actor_dream")) / "actor_dream_best_real.pt")

    replay = load_replay_arrays(atari_cfg.get("replay_dir", f"data/atari/{game}/replay"))
    n_actions = int(max(replay.actions.max() + 1, int(pred_cfg.get("n_actions", 6))))

    fsq = FSQVAE(
        img_channels=int(fsq_cfg.get("img_channels", 3)),
        levels=fsq_cfg.get("levels", [8, 5, 5, 5]),
        norm_type=fsq_cfg.get("norm_type", "group"),
        latent_grid=int(fsq_cfg.get("latent_grid", 16)),
    ).to(device)
    fsq.load_state_dict(load_clean_state(fsq_ckpt, device))
    fsq.eval()

    wm = WorldModel(
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
    predictor = AtariPredictorWithHeads(wm, hidden_dim=int(model_cfg.get("embed_dim", 384))).to(device)
    state = load_clean_state(predictor_ckpt, device)
    if "world_model.head.weight" in state:
        predictor.load_state_dict(state)
    else:
        wm_state, aux_state = split_atari_predictor_state(state)
        predictor.world_model.load_state_dict(wm_state)
        predictor.load_state_dict(aux_state, strict=False)
    predictor.eval()

    policy = None
    if actor_ckpt and Path(actor_ckpt).exists():
        policy = AtariCNNPolicy(
            vocab_size=int(model_cfg.get("vocab_size", 1000)),
            n_actions=n_actions,
            grid_size=int(fsq_cfg.get("latent_grid", 16)),
            h_dim=int(model_cfg.get("embed_dim", 384)),
        ).to(device)
        policy.load_state_dict(load_clean_state(actor_ckpt, device))
        policy.eval()

    return {
        "atari_cfg": atari_cfg,
        "fsq_cfg": fsq_cfg,
        "model_cfg": model_cfg,
        "game": game,
        "replay": replay,
        "n_actions": n_actions,
        "fsq": fsq,
        "predictor": predictor,
        "policy": policy,
        "checkpoints": {
            "fsq": str(fsq_ckpt),
            "predictor": str(predictor_ckpt),
            "actor": str(actor_ckpt),
        },
    }


@torch.no_grad()
def reward_head_audit(bundle, tokens, starts, args, device):
    replay = bundle["replay"]
    predictor = bundle["predictor"]
    k = int(bundle["model_cfg"].get("context_frames", 4))
    rng = np.random.default_rng(args.seed + 10)
    if len(starts) > args.audit_samples:
        starts = rng.choice(starts, size=args.audit_samples, replace=False)

    rows = []
    batch = int(args.batch_size)
    for lo in range(0, len(starts), batch):
        chunk = starts[lo:lo + batch]
        frame_tokens = torch.stack([tokens[i:i + k + 1] for i in chunk]).to(device)
        actions = torch.from_numpy(np.stack([replay.actions[i:i + k] for i in chunk]).astype(np.int64)).to(device)
        _, pred_reward, done_logit = predictor(frame_tokens, actions, return_aux=True)
        target_idx = chunk + k - 1
        true_reward = replay.rewards[target_idx]
        true_done = replay.dones[target_idx].astype(np.float32)
        for tr, td, pr, dl in zip(
            true_reward.tolist(), true_done.tolist(),
            pred_reward.detach().float().cpu().tolist(),
            done_logit.detach().float().cpu().tolist(),
        ):
            rows.append((float(tr), float(td), float(pr), float(torch.sigmoid(torch.tensor(dl)).item())))

    out = {"overall_reward_mae": float(np.mean([abs(r[2] - r[0]) for r in rows])) if rows else None}
    for label, value in [("neg", -1.0), ("zero", 0.0), ("pos", 1.0)]:
        subset = [r for r in rows if r[0] == value]
        out[label] = {
            "count": len(subset),
            "pred_reward": stats([r[2] for r in subset]),
            "abs_error": stats([abs(r[2] - r[0]) for r in subset]),
            "done_prob": stats([r[3] for r in subset]),
        }
    done_subset = [r for r in rows if r[1] > 0.5]
    not_done_subset = [r for r in rows if r[1] <= 0.5]
    out["done"] = {
        "true_done_count": len(done_subset),
        "done_prob_true_done": stats([r[3] for r in done_subset]),
        "done_prob_true_not_done": stats([r[3] for r in not_done_subset]),
    }
    return out


@torch.no_grad()
def action_sensitivity(bundle, tokens, starts, args, device):
    replay = bundle["replay"]
    predictor = bundle["predictor"]
    k = int(bundle["model_cfg"].get("context_frames", 4))
    n_actions = bundle["n_actions"]
    rng = np.random.default_rng(args.seed + 20)
    starts = rng.choice(starts, size=min(args.sensitivity_samples, len(starts)), replace=False)

    reward_by_action = {a: [] for a in range(n_actions)}
    done_by_action = {a: [] for a in range(n_actions)}
    token_delta_by_action = {a: [] for a in range(n_actions)}
    reward_ranges = []

    for i in starts:
        ctx_tokens = tokens[i:i + k].unsqueeze(0).to(device)
        base_actions = replay.actions[i:i + k].astype(np.int64)
        per_action_rewards = []
        baseline_pred = None
        for a in range(n_actions):
            cand = base_actions.copy()
            cand[-1] = a
            actions = torch.from_numpy(cand[None]).to(device)
            pred_tokens, reward, done_prob = predictor.predict_next_frame(
                ctx_tokens, actions, temperature=0.0, return_aux=True)
            if baseline_pred is None:
                baseline_pred = pred_tokens
            reward_val = float(reward.item())
            reward_by_action[a].append(reward_val)
            done_by_action[a].append(float(done_prob.item()))
            token_delta_by_action[a].append(float((pred_tokens != baseline_pred).float().mean().item()))
            per_action_rewards.append(reward_val)
        reward_ranges.append(max(per_action_rewards) - min(per_action_rewards))

    return {
        "reward_by_action": {
            ACTION_NAMES[a] if a < len(ACTION_NAMES) else str(a): stats(vals)
            for a, vals in reward_by_action.items()
        },
        "done_by_action": {
            ACTION_NAMES[a] if a < len(ACTION_NAMES) else str(a): stats(vals)
            for a, vals in done_by_action.items()
        },
        "token_delta_vs_action0": {
            ACTION_NAMES[a] if a < len(ACTION_NAMES) else str(a): stats(vals)
            for a, vals in token_delta_by_action.items()
        },
        "per_context_reward_range": stats(reward_ranges),
    }


@torch.no_grad()
def policy_probe(bundle, tokens, starts, args, device):
    policy = bundle["policy"]
    if policy is None:
        return {"available": False}
    replay = bundle["replay"]
    predictor = bundle["predictor"]
    k = int(bundle["model_cfg"].get("context_frames", 4))
    rng = np.random.default_rng(args.seed + 30)
    starts = rng.choice(starts, size=min(args.policy_samples, len(starts)), replace=False)
    probs = []
    argmax_counts = collections.Counter()
    true_action_counts = collections.Counter()
    for lo in range(0, len(starts), args.batch_size):
        chunk = starts[lo:lo + args.batch_size]
        ctx_tokens = torch.stack([tokens[i:i + k] for i in chunk]).to(device)
        ctx_actions = torch.from_numpy(np.stack([replay.actions[i:i + k] for i in chunk]).astype(np.int64)).to(device)
        h = predictor.encode_controller_context(ctx_tokens, ctx_actions)
        logits, values = policy(ctx_tokens[:, -1], h.float())
        p = logits.softmax(dim=-1).detach().cpu().numpy()
        probs.append(p)
        for a in p.argmax(axis=1).tolist():
            argmax_counts[int(a)] += 1
        for a in replay.actions[chunk + k - 1].tolist():
            true_action_counts[int(a)] += 1
    probs = np.concatenate(probs, axis=0)
    return {
        "available": True,
        "mean_probs": {
            ACTION_NAMES[a] if a < len(ACTION_NAMES) else str(a): float(probs[:, a].mean())
            for a in range(probs.shape[1])
        },
        "argmax_counts": {
            ACTION_NAMES[a] if a < len(ACTION_NAMES) else str(a): int(argmax_counts[a])
            for a in range(probs.shape[1])
        },
        "replay_target_action_counts": {
            ACTION_NAMES[a] if a < len(ACTION_NAMES) else str(a): int(true_action_counts[a])
            for a in range(probs.shape[1])
        },
        "entropy": stats((-probs * np.log(np.clip(probs, 1e-9, 1.0))).sum(axis=1)),
    }


@torch.no_grad()
def dream_reward_probe(bundle, tokens, starts, args, device):
    replay = bundle["replay"]
    predictor = bundle["predictor"]
    policy = bundle["policy"]
    k = int(bundle["model_cfg"].get("context_frames", 4))
    n_actions = bundle["n_actions"]
    rng = np.random.default_rng(args.seed + 40)
    starts = rng.choice(starts, size=min(args.dream_contexts, len(starts)), replace=False)

    def rollout(mode):
        returns = []
        reward_values = []
        for i in starts:
            ctx_tokens = tokens[i:i + k].unsqueeze(0).to(device)
            ctx_actions = torch.from_numpy(replay.actions[i:i + k][None].astype(np.int64)).to(device)
            total = 0.0
            for _ in range(args.dream_horizon):
                h = predictor.encode_controller_context(ctx_tokens, ctx_actions)
                if mode == "random" or policy is None:
                    action = torch.tensor([int(rng.integers(0, n_actions))], device=device)
                else:
                    logits, _ = policy(ctx_tokens[:, -1], h.float())
                    action = logits.argmax(dim=-1)
                pred_actions = torch.cat([ctx_actions[:, 1:], action.unsqueeze(1)], dim=1)
                pred_tokens, reward, done_prob = predictor.predict_next_frame(
                    ctx_tokens, pred_actions, temperature=0.0, return_aux=True)
                rv = float(reward.item())
                reward_values.append(rv)
                total += rv
                ctx_tokens = torch.cat([ctx_tokens[:, 1:], pred_tokens.unsqueeze(1)], dim=1)
                ctx_actions = pred_actions
                if float(done_prob.item()) >= args.done_threshold:
                    break
            returns.append(total)
        return {"return": stats(returns), "step_reward": stats(reward_values)}

    return {"random": rollout("random"), "policy_argmax": rollout("policy")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/atari/atari_pong_h200.yaml")
    parser.add_argument("--predictor-section", default="predictor_sls")
    parser.add_argument("--fsq-checkpoint", default=None)
    parser.add_argument("--predictor-checkpoint", default=None)
    parser.add_argument("--actor-checkpoint", default=None)
    parser.add_argument("--output", default="runs/atari_pong_h200_full/controller_diagnostics.json")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--baseline-episodes", type=int, default=3)
    parser.add_argument("--random-episodes", type=int, default=10)
    parser.add_argument("--max-steps-per-episode", type=int, default=27000)
    parser.add_argument("--audit-samples", type=int, default=20000)
    parser.add_argument("--sensitivity-samples", type=int, default=512)
    parser.add_argument("--policy-samples", type=int, default=4096)
    parser.add_argument("--dream-contexts", type=int, default=128)
    parser.add_argument("--dream-horizon", type=int, default=50)
    parser.add_argument("--done-threshold", type=float, default=0.5)
    parser.add_argument("--batch-size", type=int, default=256)
    args = parser.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    bundle = build_models(args, device)
    replay = bundle["replay"]
    starts = valid_context_starts(replay, int(bundle["model_cfg"].get("context_frames", 4)))

    report = {
        "device": str(device),
        "checkpoints": bundle["checkpoints"],
        "replay": {
            "steps": int(len(replay.obs)),
            "reward_counts": {str(float(v)): int((replay.rewards == v).sum()) for v in np.unique(replay.rewards)},
            "action_counts": {
                ACTION_NAMES[a] if a < len(ACTION_NAMES) else str(a): int((replay.actions == a).sum())
                for a in range(bundle["n_actions"])
            },
            "valid_contexts": int(len(starts)),
        },
    }

    report["real_env_baselines"] = real_env_baselines(bundle["game"], bundle["atari_cfg"], args)
    tokens = encode_replay_obs(bundle["fsq"], replay.obs, batch_size=args.batch_size, device=device)
    report["reward_head_audit"] = reward_head_audit(bundle, tokens, starts, args, device)
    report["action_sensitivity"] = action_sensitivity(bundle, tokens, starts, args, device)
    report["policy_probe"] = policy_probe(bundle, tokens, starts, args, device)
    report["dream_reward_probe"] = dream_reward_probe(bundle, tokens, starts, args, device)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

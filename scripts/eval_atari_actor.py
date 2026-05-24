"""Evaluate an Atari actor in the real environment without writing replay."""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

import gymnasium as gym
import numpy as np
import torch

import ale_py

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atari.controller import AtariCNNPolicy
from atari.predictor import split_atari_predictor_state
from deepdash.config import apply_config, load_config
from deepdash.fsq import FSQVAE
from deepdash.world_model import WorldModel
from scripts.train_atari_actor_real import (
    encode_frame,
    load_clean_state,
    parse_action_subset,
    resize_frame_to_64,
)

gym.register_envs(ale_py)

ACTION_NAMES = ["NOOP", "FIRE", "RIGHT", "LEFT", "RIGHTFIRE", "LEFTFIRE"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/atari/atari_pong_h200.yaml")
    parser.add_argument("--config-section", default="evaluation")
    parser.add_argument("--actor-checkpoint", default=None)
    parser.add_argument("--fsq-checkpoint", default=None)
    parser.add_argument("--predictor-checkpoint", default=None)
    parser.add_argument("--n-episodes", type=int, default=None)
    parser.add_argument("--max-steps-per-episode", type=int, default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--policy-action-subset", default=None,
                        help="Comma-separated ALE env action ids exposed to the policy.")
    parser.add_argument("--stochastic", action="store_true",
                        help="Sample from the policy instead of using argmax.")
    args = parser.parse_args()
    apply_config(args, section=args.config_section)

    atari_cfg = load_config(args.config, section="atari")
    fsq_cfg = load_config(args.config, section="fsq")
    model_cfg = load_config(args.config, section="model")
    pred_cfg = load_config(args.config, section="predictor_sls")
    actor_cfg = load_config(args.config, section="actor_dream")

    game = atari_cfg.get("game", "Pong")
    args.actor_checkpoint = args.actor_checkpoint or actor_cfg.get("pretrained") or str(
        Path(actor_cfg.get("checkpoint_dir", "checkpoints_atari_actor_dream")) / "actor_dream_final.pt")
    args.fsq_checkpoint = args.fsq_checkpoint or pred_cfg.get("fsq_checkpoint")
    args.predictor_checkpoint = args.predictor_checkpoint or str(Path(pred_cfg.get("checkpoint_dir")) / "predictor_best.pt")
    args.n_episodes = args.n_episodes or 10
    args.max_steps_per_episode = args.max_steps_per_episode or 27000
    args.seed = args.seed if args.seed is not None else int(atari_cfg.get("seed", 42)) + 10000

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    env = gym.make(
        f"ALE/{game}-v5",
        frameskip=int(atari_cfg.get("frame_skip", 4)),
        repeat_action_probability=float(atari_cfg.get("repeat_action_probability", 0.0)),
    )
    n_actions = int(env.action_space.n)
    action_subset = parse_action_subset(args.policy_action_subset, n_actions)
    policy_n_actions = len(action_subset)
    print(f"Env actions={n_actions} policy_actions={policy_n_actions} subset={action_subset}")

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
    pred_state, _ = split_atari_predictor_state(load_clean_state(args.predictor_checkpoint, device))
    predictor.load_state_dict(pred_state)
    predictor.eval()

    policy = AtariCNNPolicy(
        vocab_size=int(model_cfg.get("vocab_size", 1000)),
        n_actions=policy_n_actions,
        grid_size=int(fsq_cfg.get("latent_grid", 16)),
        h_dim=int(model_cfg.get("embed_dim", 384)),
        value_head_type=str(actor_cfg.get("value_head_type", "scalar")),
        value_bins=int(actor_cfg.get("value_twohot_bins", 255)),
        value_low=float(actor_cfg.get("value_twohot_low", -25.0)),
        value_high=float(actor_cfg.get("value_twohot_high", 25.0)),
    ).to(device)
    policy.load_state_dict(load_clean_state(args.actor_checkpoint, device))
    policy.eval()

    k = int(model_cfg.get("context_frames", 4))
    rng = np.random.default_rng(args.seed)
    returns, lengths = [], []
    action_counts = collections.Counter()
    with torch.no_grad():
        for ep in range(args.n_episodes):
            ep_action_counts = collections.Counter()
            obs, _ = env.reset(seed=int(rng.integers(0, 2**31 - 1)))
            frame = resize_frame_to_64(obs)
            token = encode_frame(fsq, frame, device).cpu()
            ctx_tokens = [token.clone() for _ in range(k)]
            ctx_actions = [0 for _ in range(k)]
            ep_return = 0.0
            steps = 0
            for _ in range(args.max_steps_per_episode):
                ctx_t = torch.stack(ctx_tokens[-k:], dim=1).to(device)
                ctx_a = torch.tensor([ctx_actions[-k:]], dtype=torch.long, device=device)
                h_t = predictor.encode_context(
                    ctx_t, ctx_a, return_action_hidden=False)
                logits, _ = policy(ctx_t[:, -1], h_t.float())
                if args.stochastic:
                    dist = torch.distributions.Categorical(logits=logits)
                    policy_action = int(dist.sample().item())
                else:
                    policy_action = int(logits.argmax(dim=-1).item())
                action = int(action_subset[policy_action])
                action_counts[action] += 1
                ep_action_counts[action] += 1
                obs, reward, terminated, truncated, _ = env.step(action)
                frame = resize_frame_to_64(obs)
                ctx_tokens.append(encode_frame(fsq, frame, device).cpu())
                ctx_actions.append(action)
                ep_return += float(reward)
                steps += 1
                if terminated or truncated:
                    break
            returns.append(ep_return)
            lengths.append(steps)
            ep_actions = {
                ACTION_NAMES[a] if a < len(ACTION_NAMES) else str(a): int(n)
                for a, n in sorted(ep_action_counts.items())
            }
            print(f"eval_ep={ep + 1} return={ep_return:+.1f} "
                  f"steps={steps} actions={ep_actions}")

    env.close()
    summary = {
        "actor_checkpoint": str(args.actor_checkpoint),
        "n_episodes": int(args.n_episodes),
        "returns": returns,
        "lengths": lengths,
        "mean_return": float(np.mean(returns)) if returns else 0.0,
        "episode_count": len(returns),
        "stochastic": bool(args.stochastic),
        "eval_env_steps": int(sum(lengths)),
        "action_counts": {
            ACTION_NAMES[a] if a < len(ACTION_NAMES) else str(a): int(n)
            for a, n in sorted(action_counts.items())
        },
    }
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

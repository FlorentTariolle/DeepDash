"""Collect Atari episodes for SLS-WM training.

Can write either the legacy per-episode dirs or an appendable replay buffer
made of compressed NumPy shards. The replay format is the preferred path for
Atari100K cycles because it tracks the real env-step budget explicitly.

Atari 100k convention (Kaiser et al. 2020):
  - Frame-skip = 4 (action repeated for 4 game frames; obs returned at the end).
  - No sticky actions (``repeat_action_probability=0.0``).
  - Minimal action space per game (gymnasium default for ``ALE/{game}-v5``).

Frames are resized from native (210, 160, 3) to (64, 64, 3) RGB uint8 via
PIL bilinear resizing, matching the released IRIS / delta-IRIS Atari wrappers.

Usage::

    python scripts/collect_atari_episodes.py --game Pong --n-episodes 50

Output layout::

    data/atari/{game}/episodes/ep_0000/
        frames.npy   (T, 64, 64, 3) uint8 RGB
        actions.npy  (T,) int32 action indices
        dones.npy    (T,) bool, dones[-1] = True for natural episode ends
        rewards.npy  (T,) float32 env rewards

    data/atari/{game}/replay/
        metadata.json
        shard_000000.npz  obs/actions/rewards/dones/episode_ids
"""

import argparse
import sys
import time
from pathlib import Path

import gymnasium as gym
import numpy as np
from PIL import Image

import ale_py
from atari.replay_buffer import ReplayShardWriter

gym.register_envs(ale_py)

RESAMPLE_BILINEAR = Image.Resampling.BILINEAR if hasattr(Image, "Resampling") else Image.BILINEAR


def resize_frame_to_64(obs):
    """Match the IRIS/delta-IRIS Atari preprocessing: RGB PIL bilinear to 64x64."""
    return np.asarray(Image.fromarray(obs).resize((64, 64), RESAMPLE_BILINEAR), dtype=np.uint8)


def collect_episode(env, max_steps, rng):
    """Run one episode under a uniform-random policy. Returns numpy arrays."""
    obs, _ = env.reset(seed=int(rng.integers(0, 2**31 - 1)))
    frames, actions, dones, rewards = [], [], [], []
    for _ in range(max_steps):
        action = int(env.action_space.sample())
        obs, reward, terminated, truncated, _ = env.step(action)
        # Resize 210x160 -> 64x64, keep RGB channels-last uint8.
        frame64 = resize_frame_to_64(obs)
        frames.append(frame64)
        actions.append(action)
        rewards.append(float(reward))
        done = bool(terminated or truncated)
        dones.append(done)
        if done:
            break
    return (
        np.stack(frames).astype(np.uint8),
        np.asarray(actions, dtype=np.int32),
        np.asarray(dones, dtype=bool),
        np.asarray(rewards, dtype=np.float32),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--game", default="Pong",
                        help="ALE game name; will be used as 'ALE/{game}-v5'.")
    parser.add_argument("--out-dir", default=None,
                        help="Output dir (default: data/atari/{game})")
    parser.add_argument("--storage", choices=["episodes", "replay", "both"],
                        default="replay",
                        help="Storage format. Replay is preferred for Atari100K cycles.")
    parser.add_argument("--replay-dir", default=None,
                        help="Replay output dir (default: {out_dir}/replay).")
    parser.add_argument("--shard-size", type=int, default=8192,
                        help="Transitions per replay shard.")
    parser.add_argument("--max-env-steps", type=int, default=None,
                        help="Optional global collection cap in real env steps.")
    parser.add_argument("--n-episodes", type=int, default=50)
    parser.add_argument("--max-steps-per-episode", type=int, default=27000,
                        help="Atari standard cap (108k game frames at frame-skip 4).")
    parser.add_argument("--frame-skip", type=int, default=4,
                        help="Atari 100k convention (Kaiser 2020).")
    parser.add_argument("--repeat-action-probability", type=float, default=0.0,
                        help="0.0 = no sticky actions (Atari 100k convention).")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--start-idx", type=int, default=0,
                        help="First episode index, in case of resume.")
    args = parser.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else Path("data/atari") / args.game
    episodes_dir = out_dir / "episodes"
    if args.storage in ("episodes", "both"):
        episodes_dir.mkdir(parents=True, exist_ok=True)

    env_id = f"ALE/{args.game}-v5"
    env = gym.make(env_id,
                    frameskip=args.frame_skip,
                    repeat_action_probability=args.repeat_action_probability)
    n_actions = env.action_space.n
    print(f"Env: {env_id}  frame_skip={args.frame_skip}  "
          f"sticky={args.repeat_action_probability}  n_actions={n_actions}")

    writer = None
    if args.storage in ("replay", "both"):
        replay_dir = Path(args.replay_dir) if args.replay_dir else out_dir / "replay"
        writer = ReplayShardWriter(
            replay_dir,
            shard_size=args.shard_size,
            metadata={
                "game": args.game,
                "env_id": env_id,
                "n_actions": int(n_actions),
                "frame_skip": int(args.frame_skip),
                "repeat_action_probability": float(args.repeat_action_probability),
                "obs_shape": [64, 64, 3],
                "obs_dtype": "uint8",
                "seed": int(args.seed),
            },
        )
        print(f"Replay: {replay_dir}  existing_steps={writer.metadata['total_steps']}  "
              f"next_episode_id={writer.next_episode_id}")

    rng = np.random.default_rng(args.seed)
    total_steps = 0
    t0 = time.time()
    for i in range(args.n_episodes):
        ep_idx = args.start_idx + i
        ep_dir = episodes_dir / f"ep_{ep_idx:04d}"
        if args.storage == "episodes" and ep_dir.exists() and (ep_dir / "frames.npy").exists():
            print(f"  skip ep_{ep_idx:04d} (exists)")
            continue
        if args.max_env_steps is not None and total_steps >= args.max_env_steps:
            break
        remaining = args.max_steps_per_episode
        if args.max_env_steps is not None:
            remaining = min(remaining, args.max_env_steps - total_steps)
        if remaining <= 0:
            break

        frames, actions, dones, rewards = collect_episode(env, remaining, rng)
        if args.storage in ("episodes", "both"):
            ep_dir.mkdir(parents=True, exist_ok=True)
            np.save(ep_dir / "frames.npy", frames)
            np.save(ep_dir / "actions.npy", actions)
            np.save(ep_dir / "dones.npy", dones)
            np.save(ep_dir / "rewards.npy", rewards)
        if writer is not None:
            replay_ep_id = writer.next_episode_id
            writer.append_episode(frames, actions, rewards, dones, replay_ep_id)
        T = len(frames)
        total_steps += T
        ret = float(rewards.sum())
        print(f"  ep_{ep_idx:04d}: T={T:5d}  return={ret:+.1f}")

    env.close()
    if writer is not None:
        writer.close()
    dt = time.time() - t0
    print(f"\nCollected up to {args.n_episodes} episodes, {total_steps} env steps total "
          f"in {dt:.1f}s ({total_steps / max(dt, 1e-3):.0f} steps/s).")
    if args.storage in ("episodes", "both"):
        print(f"Episodes output: {episodes_dir}")
    if writer is not None:
        print(f"Replay output: {writer.replay_dir}  total_steps={writer.metadata['total_steps']}")


if __name__ == "__main__":
    main()

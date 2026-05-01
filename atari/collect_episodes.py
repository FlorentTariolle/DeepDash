"""Collect Atari episodes for SLS-WM training.

Produces per-episode dirs in the same layout the existing training scripts
already consume (frames.npy + actions.npy), with ``dones.npy`` and
``rewards.npy`` added for env-driven termination and PPO reward shaping.

Atari 100k convention (Kaiser et al. 2020):
  - Frame-skip = 4 (action repeated for 4 game frames; obs returned at the end).
  - No sticky actions (``repeat_action_probability=0.0``).
  - Minimal action space per game (gymnasium default for ``ALE/{game}-v5``).

Frames are resized from native (210, 160, 3) to (64, 64, 3) RGB uint8 via
``cv2.INTER_AREA``, matching IRIS / delta-IRIS / TWISTER's input resolution
(verified from each paper's appendix).

Usage::

    python scripts/collect_atari_episodes.py --game Pong --n-episodes 50

Output layout::

    data/atari/{game}/episodes/ep_0000/
        frames.npy   (T, 64, 64, 3) uint8 RGB
        actions.npy  (T,) int32 action indices
        dones.npy    (T,) bool, dones[-1] = True for natural episode ends
        rewards.npy  (T,) float32 env rewards
"""

import argparse
import sys
import time
from pathlib import Path

import cv2
import gymnasium as gym
import numpy as np

import ale_py
gym.register_envs(ale_py)


def collect_episode(env, max_steps, rng):
    """Run one episode under a uniform-random policy. Returns numpy arrays."""
    obs, _ = env.reset(seed=int(rng.integers(0, 2**31 - 1)))
    frames, actions, dones, rewards = [], [], [], []
    for _ in range(max_steps):
        action = int(env.action_space.sample())
        obs, reward, terminated, truncated, _ = env.step(action)
        # Resize 210x160 -> 64x64, keep RGB channels-last uint8.
        frame64 = cv2.resize(obs, (64, 64), interpolation=cv2.INTER_AREA)
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
    episodes_dir.mkdir(parents=True, exist_ok=True)

    env_id = f"ALE/{args.game}-v5"
    env = gym.make(env_id,
                    frameskip=args.frame_skip,
                    repeat_action_probability=args.repeat_action_probability)
    n_actions = env.action_space.n
    print(f"Env: {env_id}  frame_skip={args.frame_skip}  "
          f"sticky={args.repeat_action_probability}  n_actions={n_actions}")

    rng = np.random.default_rng(args.seed)
    total_steps = 0
    t0 = time.time()
    for i in range(args.n_episodes):
        ep_idx = args.start_idx + i
        ep_dir = episodes_dir / f"ep_{ep_idx:04d}"
        if ep_dir.exists() and (ep_dir / "frames.npy").exists():
            print(f"  skip ep_{ep_idx:04d} (exists)")
            continue
        ep_dir.mkdir(parents=True, exist_ok=True)
        frames, actions, dones, rewards = collect_episode(env, args.max_steps_per_episode, rng)
        np.save(ep_dir / "frames.npy", frames)
        np.save(ep_dir / "actions.npy", actions)
        np.save(ep_dir / "dones.npy", dones)
        np.save(ep_dir / "rewards.npy", rewards)
        T = len(frames)
        total_steps += T
        ret = float(rewards.sum())
        print(f"  ep_{ep_idx:04d}: T={T:5d}  return={ret:+.1f}")

    env.close()
    dt = time.time() - t0
    print(f"\nCollected {args.n_episodes} episodes, {total_steps} env steps total "
          f"in {dt:.1f}s ({total_steps / max(dt, 1e-3):.0f} steps/s).")
    print(f"Output: {episodes_dir}")


if __name__ == "__main__":
    main()

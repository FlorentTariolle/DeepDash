"""Play an Atari ALE environment manually.

This is a local inspection tool only. It does not save trajectories, so human
play cannot accidentally enter Atari100K training replay.
"""

import argparse

import cv2
import gymnasium as gym
import numpy as np
from PIL import Image

import ale_py

gym.register_envs(ale_py)

RESAMPLE_BILINEAR = Image.Resampling.BILINEAR if hasattr(Image, "Resampling") else Image.BILINEAR


KEY_ESC = 27
KEY_UP = 2490368
KEY_DOWN = 2621440
KEY_LEFT = 2424832
KEY_RIGHT = 2555904


def action_lookup(meanings):
    return {name: idx for idx, name in enumerate(meanings)}


def first_available(actions, *names, default=0):
    for name in names:
        if name in actions:
            return actions[name]
    return default


def key_to_action(key, actions):
    noop = actions.get("NOOP", 0)
    if key < 0:
        return noop

    if ord("0") <= key <= ord("9"):
        return key - ord("0")

    key_char = chr(key).lower() if 0 <= key < 256 else ""
    if key in (KEY_UP,) or key_char == "w":
        return first_available(actions, "UP", "RIGHT", "UPFIRE", "RIGHTFIRE", default=noop)
    if key in (KEY_DOWN,) or key_char == "s":
        return first_available(actions, "DOWN", "LEFT", "DOWNFIRE", "LEFTFIRE", default=noop)
    if key in (KEY_LEFT,) or key_char == "a":
        return first_available(actions, "LEFT", "LEFTFIRE", default=noop)
    if key in (KEY_RIGHT,) or key_char == "d":
        return first_available(actions, "RIGHT", "RIGHTFIRE", default=noop)
    if key == ord(" "):
        return actions.get("FIRE", noop)
    return noop


def render_frame(obs, view, scale, status):
    if view == "train":
        frame = np.asarray(Image.fromarray(obs).resize((64, 64), RESAMPLE_BILINEAR), dtype=np.uint8)
    else:
        frame = obs
    frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    h, w = frame.shape[:2]
    frame = cv2.resize(frame, (w * scale, h * scale), interpolation=cv2.INTER_NEAREST)

    panel = np.zeros((48, frame.shape[1], 3), dtype=np.uint8)
    cv2.putText(panel, status, (8, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (230, 230, 230), 1, cv2.LINE_AA)
    return np.concatenate([frame, panel], axis=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", default="Pong")
    parser.add_argument("--frame-skip", type=int, default=4)
    parser.add_argument("--repeat-action-probability", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--view", choices=["native", "train"], default="native",
                        help="native shows ALE frames; train shows 64x64 preprocessing.")
    parser.add_argument("--scale", type=int, default=3)
    parser.add_argument("--delay-ms", type=int, default=35)
    args = parser.parse_args()

    env_id = f"ALE/{args.game}-v5"
    env = gym.make(
        env_id,
        frameskip=args.frame_skip,
        repeat_action_probability=args.repeat_action_probability,
    )
    meanings = env.unwrapped.get_action_meanings()
    actions = action_lookup(meanings)

    print(f"Env: {env_id}")
    print(f"Action meanings: {list(enumerate(meanings))}")
    print("Controls:")
    print("  W/Up and S/Down: paddle movement for Pong-like games")
    print("  A/Left and D/Right: left/right actions")
    print("  Space: FIRE")
    print("  0-9: send exact action id")
    print("  P: pause/resume, R: reset, Q/Esc: quit")
    print("Note: this script does not save human play.")

    obs, _ = env.reset(seed=args.seed)
    episode = 1
    episode_return = 0.0
    episode_steps = 0
    last_action = actions.get("NOOP", 0)
    paused = False

    while True:
        action_name = meanings[last_action] if last_action < len(meanings) else str(last_action)
        status = (
            f"ep {episode} step {episode_steps} return {episode_return:+.1f} "
            f"action {last_action}:{action_name}"
        )
        cv2.imshow("Atari manual play", render_frame(obs, args.view, args.scale, status))
        key = cv2.waitKeyEx(50 if paused else args.delay_ms)

        if key in (KEY_ESC, ord("q"), ord("Q")):
            break
        if key in (ord("p"), ord("P")):
            paused = not paused
            continue
        if key in (ord("r"), ord("R")):
            obs, _ = env.reset()
            episode += 1
            episode_return = 0.0
            episode_steps = 0
            last_action = actions.get("NOOP", 0)
            continue
        if paused:
            continue

        action = key_to_action(key, actions)
        if action >= env.action_space.n:
            action = actions.get("NOOP", 0)
        obs, reward, terminated, truncated, _ = env.step(action)
        episode_return += float(reward)
        episode_steps += 1
        last_action = action

        if terminated or truncated:
            print(f"Episode {episode}: return={episode_return:+.1f}, steps={episode_steps}")
            obs, _ = env.reset()
            episode += 1
            episode_return = 0.0
            episode_steps = 0
            last_action = actions.get("NOOP", 0)

    env.close()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()

"""Generate a real-prefix -> sampled-continuation panel for DashVMC.

The figure seeds the V7 world model with real recorded frames/actions, then
samples future token grids under the recorded future action sequence and decodes
them with the FSQ decoder. The result is a paper-ready static panel.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import cv2
import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deepdash.fsq import FSQVAE
from deepdash.world_model import WorldModel


def load_clean_state(path: str, device: torch.device) -> dict[str, torch.Tensor]:
    state = torch.load(path, map_location=device, weights_only=True)
    return {k.removeprefix("_orig_mod."): v for k, v in state.items()}


def iter_episode_dirs(*roots: str):
    shift_re = re.compile(r"_s[+-]\d+_[+-]\d+$")
    for root in roots:
        for ep in sorted(Path(root).glob("*")):
            if shift_re.search(ep.name):
                continue
            if (ep / "frames.npy").exists() and (ep / "actions.npy").exists():
                yield ep


def encode_frames(vae: FSQVAE, frames: np.ndarray, device: torch.device) -> np.ndarray:
    with torch.no_grad():
        x = torch.from_numpy(frames).float().unsqueeze(1).to(device) / 255.0
        indices = vae.encode(x)
    return indices.reshape(indices.size(0), -1).cpu().numpy().astype(np.int64)


def decode_tokens(vae: FSQVAE, tokens_np: np.ndarray, device: torch.device) -> np.ndarray:
    indices = torch.from_numpy(tokens_np.astype(np.int64)).reshape(1, 8, 8).to(device)
    with torch.no_grad():
        img = vae.decode_indices(indices)
    return np.clip(img[0, 0].cpu().numpy() * 255.0, 0, 255).astype(np.uint8)


def pick_episode(args, min_len):
    candidates = []
    for ep in iter_episode_dirs(args.episodes_dir, args.expert_episodes_dir):
        frames = np.load(ep / "frames.npy", mmap_mode="r")
        if len(frames) >= min_len:
            candidates.append(ep)
    if not candidates:
        raise SystemExit(f"No episode with at least {min_len} frames found.")
    rng = np.random.default_rng(args.seed)
    if args.episode:
        for ep in candidates:
            if ep.name == args.episode:
                return ep
        raise SystemExit(f"Episode not found or too short: {args.episode}")
    return candidates[int(rng.integers(len(candidates)))]


def render_panel(prefix_frames, continuation_frames, actions, output, scale=5):
    tile = 64 * scale
    label_w = 190
    label_h = 24
    action_h = 20
    gap = 8
    pad = 14
    n_cols = max(len(prefix_frames), len(continuation_frames))
    width = pad * 2 + label_w + n_cols * tile + (n_cols - 1) * gap
    height = pad * 2 + 2 * (label_h + tile + action_h) + gap
    canvas = np.full((height, width, 3), 248, dtype=np.uint8)

    font = cv2.FONT_HERSHEY_SIMPLEX

    def draw_row(row_idx, row_label, frames, row_actions, color):
        row_y = pad + row_idx * (label_h + tile + action_h + gap)
        cv2.putText(canvas, row_label, (pad, row_y + label_h + tile // 2),
                    font, 0.62, color, 1, cv2.LINE_AA)
        if row_idx == 0:
            cv2.putText(canvas, "observed", (pad, row_y + label_h + tile // 2 + 22),
                        font, 0.45, (80, 80, 80), 1, cv2.LINE_AA)
        else:
            cv2.putText(canvas, "sampled", (pad, row_y + label_h + tile // 2 + 22),
                        font, 0.45, (80, 80, 80), 1, cv2.LINE_AA)
        for i, frame in enumerate(frames):
            x = pad + label_w + i * (tile + gap)
            y = row_y + label_h
            rgb = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            rgb = cv2.resize(rgb, (tile, tile), interpolation=cv2.INTER_NEAREST)
            canvas[y:y + tile, x:x + tile] = rgb
            cv2.rectangle(canvas, (x, y), (x + tile - 1, y + tile - 1), color, 2)
            act = row_actions[i] if i < len(row_actions) else 0
            act_label = "jump" if int(act) else "idle"
            cv2.putText(canvas, act_label, (x + 4, y + tile + 15), font, 0.4,
                        (70, 70, 70), 1, cv2.LINE_AA)

    draw_row(0, "real prefix", prefix_frames, actions[:len(prefix_frames)], (40, 70, 170))
    draw_row(
        1,
        "continuation",
        continuation_frames,
        actions[len(prefix_frames):len(prefix_frames) + len(continuation_frames)],
        (35, 125, 70),
    )
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output), canvas)


def main():
    parser = argparse.ArgumentParser(description="Generate continuation figure")
    parser.add_argument("--config", default="configs/deepdash/v7-phase0.yaml")
    parser.add_argument("--vae-checkpoint", default="checkpoints_v7/fsq_best.pt")
    parser.add_argument("--transformer-checkpoint", default="checkpoints_v7/transformer_best.pt")
    parser.add_argument("--episodes-dir", default="data/deepdash/death_episodes")
    parser.add_argument("--expert-episodes-dir", default="data/deepdash/expert_episodes")
    parser.add_argument("--output", default="paper/figures/continuation_v7.png")
    parser.add_argument("--prefix-frames", type=int, default=4)
    parser.add_argument("--continuation-steps", type=int, default=8)
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--episode", default=None)
    parser.add_argument("--start", type=int, default=None)
    parser.add_argument("--scale", type=int, default=5)
    parser.add_argument("--levels", type=int, nargs="+", default=None)
    parser.add_argument("--vocab-size", type=int, default=None)
    parser.add_argument("--embed-dim", type=int, default=None)
    parser.add_argument("--n-heads", type=int, default=None)
    parser.add_argument("--n-layers", type=int, default=None)
    parser.add_argument("--tokens-per-frame", type=int, default=None)
    parser.add_argument("--context-frames", type=int, default=None)
    parser.add_argument("--dropout", type=float, default=None)
    args = parser.parse_args()

    from deepdash.config import apply_config
    apply_config(args, section="controller_ppo")
    if args.prefix_frames != args.context_frames:
        raise SystemExit("--prefix-frames must match config context_frames.")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    vae = FSQVAE(levels=args.levels).to(device)
    vae.load_state_dict(load_clean_state(args.vae_checkpoint, device))
    vae.eval()

    wm = WorldModel(
        vocab_size=args.vocab_size,
        embed_dim=args.embed_dim,
        n_heads=args.n_heads,
        n_layers=args.n_layers,
        context_frames=args.context_frames,
        dropout=args.dropout,
        tokens_per_frame=args.tokens_per_frame,
        adaln=getattr(args, "adaln", False),
        fsq_dim=len(args.levels) if getattr(args, "levels", None) else None,
    ).to(device)
    wm.load_state_dict(load_clean_state(args.transformer_checkpoint, device), strict=False)
    wm.eval()

    total_needed = args.prefix_frames + args.continuation_steps + 1
    ep = pick_episode(args, total_needed)
    frames = np.load(ep / "frames.npy")
    actions = np.load(ep / "actions.npy").astype(np.int64)
    latest_start = len(frames) - total_needed
    rng = np.random.default_rng(args.seed)
    start = args.start if args.start is not None else int(rng.integers(latest_start + 1))
    if start < 0 or start > latest_start:
        raise SystemExit(f"--start must be in [0, {latest_start}] for {ep.name}.")

    window_frames = frames[start:start + total_needed]
    window_actions = actions[start:start + total_needed]
    tokens = encode_frames(vae, window_frames, device)

    K = args.context_frames
    status = np.full((K, 1), wm.ALIVE_TOKEN, dtype=np.int64)
    ctx_t = torch.from_numpy(np.concatenate([tokens[:K], status], axis=1)[None]).to(device)
    ctx_a = torch.from_numpy(window_actions[:K][None]).to(device)

    generated = []
    generated_actions = []
    with torch.no_grad():
        for step in range(args.continuation_steps):
            pred_tokens, _death_prob = wm.predict_next_frame(
                ctx_t, ctx_a, temperature=args.temperature)
            pred_np = pred_tokens[0].cpu().numpy()
            generated.append(decode_tokens(vae, pred_np, device))

            act = int(window_actions[K + step])
            generated_actions.append(act)
            new_status = torch.full((1, 1), wm.ALIVE_TOKEN, dtype=torch.long, device=device)
            new_frame = torch.cat([pred_tokens, new_status], dim=1).unsqueeze(1)
            ctx_t = torch.cat([ctx_t[:, 1:], new_frame], dim=1)
            ctx_a = torch.cat([
                ctx_a[:, 1:],
                torch.tensor([[act]], dtype=torch.long, device=device),
            ], dim=1)

    prefix = [frame.astype(np.uint8) for frame in window_frames[:K]]
    action_labels = list(window_actions[:K]) + generated_actions
    render_panel(prefix, generated, action_labels, args.output, scale=args.scale)
    print(f"Saved {args.output}")
    print(f"source_episode={ep.name} start={start} seed={args.seed} temperature={args.temperature}")


if __name__ == "__main__":
    main()

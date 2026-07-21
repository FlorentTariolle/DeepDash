"""Generate matched jump/idle world-model branches from recorded dream frames.

The source PNGs produced by play_dream.py contain a display-only HUD. This
script recovers the 64x64 decoded frame, blanks the HUD-covered top-left region,
re-encodes the four-frame context, and rolls out two greedy branches that differ
only in the final context action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deepdash.fsq import FSQVAE
from deepdash.world_model import WorldModel


def load_clean_state(path: Path, device: torch.device) -> dict[str, torch.Tensor]:
    state = torch.load(path, map_location=device, weights_only=True)
    return {key.removeprefix("_orig_mod."): value for key, value in state.items()}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def recover_frame(path: Path, hud_rows: int, hud_cols: int) -> np.ndarray:
    screenshot = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if screenshot is None:
        raise SystemExit(f"Could not read {path}")
    height, width = screenshot.shape
    if height != width or height % 64:
        raise SystemExit(f"Expected a square integer-scaled 64x64 image, got {width}x{height}: {path}")

    scale = height // 64
    # pygame.transform.scale duplicates pixels. Sampling each cell centre
    # recovers the displayed decoder pixel without interpolation.
    frame = screenshot[scale // 2::scale, scale // 2::scale].copy()
    frame[:hud_rows, :hud_cols] = 0
    return frame


def encode_frames(vae: FSQVAE, frames: np.ndarray, device: torch.device) -> torch.Tensor:
    tensor = torch.from_numpy(frames).float().unsqueeze(1).to(device) / 255.0
    with torch.no_grad():
        indices = vae.encode(tensor)
    return indices.reshape(len(frames), -1).long()


def decode_tokens(vae: FSQVAE, tokens: torch.Tensor, device: torch.device) -> np.ndarray:
    indices = tokens.reshape(1, 8, 8).to(device)
    with torch.no_grad():
        image = vae.decode_indices(indices)
    return np.clip(image[0, 0].cpu().numpy() * 255.0, 0, 255).astype(np.uint8)


def rollout(
    wm: WorldModel,
    vae: FSQVAE,
    context_tokens: torch.Tensor,
    context_actions: list[int],
    steps: int,
    device: torch.device,
    stop_on_death: bool = True,
) -> tuple[list[np.ndarray], list[float]]:
    status = torch.full(
        (1, wm.context_frames, 1), wm.ALIVE_TOKEN, dtype=torch.long, device=device
    )
    ctx_t = torch.cat([context_tokens.unsqueeze(0), status], dim=2)
    ctx_a = torch.tensor([context_actions], dtype=torch.long, device=device)
    frames: list[np.ndarray] = []
    death_probs: list[float] = []

    with torch.no_grad():
        for _ in range(steps):
            predicted, death_prob = wm.predict_next_frame(ctx_t, ctx_a, temperature=0.0)
            frames.append(decode_tokens(vae, predicted[0], device))
            death_probs.append(float(death_prob[0].item()))

            if stop_on_death and death_probs[-1] > 0.5:
                break

            alive = torch.full((1, 1), wm.ALIVE_TOKEN, dtype=torch.long, device=device)
            next_frame = torch.cat([predicted, alive], dim=1).unsqueeze(1)
            ctx_t = torch.cat([ctx_t[:, 1:], next_frame], dim=1)
            # Both branches are idle after the single intervention action.
            ctx_a = torch.cat(
                [ctx_a[:, 1:], torch.zeros((1, 1), dtype=torch.long, device=device)],
                dim=1,
            )

    return frames, death_probs


def save_frames(directory: Path, frames: list[np.ndarray]) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("frame_*.png"):
        stale.unlink()
    paths = []
    for index, frame in enumerate(frames):
        path = directory / f"frame_{index:06d}.png"
        cv2.imwrite(str(path), frame)
        paths.append(path.as_posix())
    return paths


def render_figure(
    prefix: list[np.ndarray],
    jump: list[np.ndarray],
    idle: list[np.ndarray],
    jump_probs: list[float],
    idle_probs: list[float],
    output: Path,
    stride: int,
    scale: int,
) -> list[int]:
    selected = list(range(0, len(jump), stride))
    if selected[-1] != len(jump) - 1:
        selected.append(len(jump) - 1)
    idle_selected = [index for index in selected if index < len(idle)]
    death_index = len(idle) - 1
    if death_index not in idle_selected:
        idle_selected.append(death_index)
        idle_selected.sort()

    tile = 64 * scale
    footer_h = 26
    gap_x = 10
    gap_y = 30
    pad = 18
    branch_w = 190
    columns = len(selected)
    grid_w = columns * tile + (columns - 1) * gap_x
    width = 2 * pad + tile + branch_w + grid_w
    row_h = tile + footer_h
    height = 2 * pad + 2 * row_h + gap_y + 28
    canvas = np.full((height, width, 3), 248, dtype=np.uint8)
    font = cv2.FONT_HERSHEY_SIMPLEX

    top_y = pad + 28
    bottom_y = top_y + row_h + gap_y
    shared_x = pad
    shared_y = (top_y + bottom_y + tile) // 2 - tile // 2
    grid_x = shared_x + tile + branch_w

    def draw_frame(frame: np.ndarray, x: int, y: int, color: tuple[int, int, int], footer: str) -> None:
        image = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        image = cv2.resize(image, (tile, tile), interpolation=cv2.INTER_NEAREST)
        canvas[y:y + tile, x:x + tile] = image
        cv2.rectangle(canvas, (x, y), (x + tile - 1, y + tile - 1), color, 2)
        cv2.putText(canvas, footer, (x + 3, y + tile + 17), font, 0.34, (55, 55, 55), 1, cv2.LINE_AA)

    draw_frame(prefix[-1], shared_x, shared_y, (85, 85, 85), "shared context  t=174")

    idle_color = (45, 80, 180)
    jump_color = (35, 125, 70)
    arrow_start = (shared_x + tile + 8, shared_y + tile // 2)
    idle_end = (grid_x - 10, top_y + tile // 2)
    jump_end = (grid_x - 10, bottom_y + tile // 2)
    cv2.arrowedLine(canvas, arrow_start, idle_end, idle_color, 3, cv2.LINE_AA, tipLength=0.08)
    cv2.arrowedLine(canvas, arrow_start, jump_end, jump_color, 3, cv2.LINE_AA, tipLength=0.08)
    cv2.putText(canvas, "IDLE", (shared_x + tile + 38, top_y + tile // 2 + 2), font, 0.62, idle_color, 2, cv2.LINE_AA)
    cv2.putText(canvas, "JUMP", (shared_x + tile + 28, bottom_y + tile // 2 + 2), font, 0.62, jump_color, 2, cv2.LINE_AA)

    for column, index in enumerate(selected):
        x = grid_x + column * (tile + gap_x)
        footer = f"t+{index + 1}  p(death)={jump_probs[index]:.2f}"
        draw_frame(jump[index], x, bottom_y, jump_color, footer)

    for column, index in enumerate(idle_selected):
        x = grid_x + column * (tile + gap_x)
        is_death = index == death_index
        footer = f"t+{index + 1}  " + (
            f"DEATH  p={idle_probs[index]:.2f}"
            if is_death else f"p(death)={idle_probs[index]:.2f}"
        )
        draw_frame(idle[index], x, top_y, (30, 30, 210) if is_death else idle_color, footer)

    dead_start = len(idle_selected)
    for offset, letter in enumerate("DEAD"):
        column = dead_start + offset
        if column >= columns:
            break
        x = grid_x + column * (tile + gap_x)
        text_size = cv2.getTextSize(letter, font, 2.3, 5)[0]
        text_x = x + (tile - text_size[0]) // 2
        text_y = top_y + (tile + text_size[1]) // 2
        cv2.putText(canvas, letter, (text_x, text_y), font, 2.3, (20, 20, 20), 5, cv2.LINE_AA)

    note = "Identical generated context; only the intervention action changes"
    cv2.putText(canvas, note, (grid_x, 20), font, 0.42, (55, 55, 55), 1, cv2.LINE_AA)
    output.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output), canvas)
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the spike counterfactual for issue #30")
    parser.add_argument("--config", default="configs/deepdash/v7-phase0.yaml")
    parser.add_argument("--vae-checkpoint", default="checkpoints_v7/fsq_best.pt")
    parser.add_argument("--transformer-checkpoint", default="checkpoints_v7/transformer_best.pt")
    parser.add_argument(
        "--frames-dir",
        default="analysis/2026-07-21_counterfactual_spike/source_display",
    )
    parser.add_argument("--start-frame", type=int, default=171)
    parser.add_argument("--context-frames", type=int, default=4)
    parser.add_argument("--steps", type=int, default=13)
    parser.add_argument("--hud-rows", type=int, default=4)
    parser.add_argument("--hud-cols", type=int, default=32)
    parser.add_argument("--figure-stride", type=int, default=2)
    parser.add_argument("--figure-scale", type=int, default=3)
    parser.add_argument("--output-dir", default="analysis/2026-07-21_counterfactual_spike")
    parser.add_argument("--levels", type=int, nargs="+", default=None)
    parser.add_argument("--vocab-size", type=int, default=None)
    parser.add_argument("--embed-dim", type=int, default=None)
    parser.add_argument("--n-heads", type=int, default=None)
    parser.add_argument("--n-layers", type=int, default=None)
    parser.add_argument("--tokens-per-frame", type=int, default=None)
    parser.add_argument("--dropout", type=float, default=None)
    args = parser.parse_args()

    from deepdash.config import apply_config
    apply_config(args, section="controller_ppo")
    if args.context_frames != 4:
        raise SystemExit("This selected counterfactual requires exactly four context frames.")

    torch.manual_seed(0)
    np.random.seed(0)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    vae_path = Path(args.vae_checkpoint)
    transformer_path = Path(args.transformer_checkpoint)

    vae = FSQVAE(levels=args.levels).to(device)
    vae.load_state_dict(load_clean_state(vae_path, device))
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
        fsq_dim=len(args.levels),
    ).to(device)
    wm.load_state_dict(load_clean_state(transformer_path, device), strict=False)
    wm.eval()

    frames_dir = Path(args.frames_dir)
    source_paths = [frames_dir / f"frame_{i:06d}.png" for i in range(args.start_frame, args.start_frame + 4)]
    recovered = np.stack([recover_frame(path, args.hud_rows, args.hud_cols) for path in source_paths])
    tokens = encode_frames(vae, recovered, device)
    roundtrip = [decode_tokens(vae, token, device) for token in tokens]

    jump_actions = [0, 0, 0, 1]
    idle_actions = [0, 0, 0, 0]
    jump_frames, jump_probs = rollout(wm, vae, tokens, jump_actions, args.steps, device)
    idle_frames, idle_probs = rollout(wm, vae, tokens, idle_actions, args.steps, device)

    output_dir = Path(args.output_dir)
    source_output = save_frames(output_dir / "shared_context", roundtrip)
    jump_output = save_frames(output_dir / "jump", jump_frames)
    idle_output = save_frames(output_dir / "idle", idle_frames)
    figure_path = output_dir / "counterfactual_spike.png"
    selected = render_figure(
        roundtrip, jump_frames, idle_frames, jump_probs, idle_probs,
        figure_path, args.figure_stride, args.figure_scale,
    )

    metadata = {
        "description": "Matched greedy world-model branches differing only in the 174->175 action.",
        "device": str(device),
        "source_frames": [path.as_posix() for path in source_paths],
        "source_sha256": {path.name: sha256(path) for path in source_paths},
        "hud_removal_64px": {"rows": args.hud_rows, "cols": args.hud_cols},
        "context_note": "Display screenshots were cleaned, re-encoded, and decoded; both branches use the resulting identical token context.",
        "vae_checkpoint": {"path": vae_path.as_posix(), "sha256": sha256(vae_path)},
        "transformer_checkpoint": {"path": transformer_path.as_posix(), "sha256": sha256(transformer_path)},
        "decoding": {"temperature": 0.0, "seed": 0, "steps": args.steps},
        "jump_context_actions": jump_actions,
        "idle_context_actions": idle_actions,
        "post_intervention_actions": {
            "jump": [0] * (len(jump_frames) - 1),
            "idle": [0] * (len(idle_frames) - 1),
        },
        "termination_rule": "Stop each branch immediately after the first prediction with p(death) > 0.5.",
        "jump_death_probabilities": jump_probs,
        "idle_death_probabilities": idle_probs,
        "figure_selected_generated_offsets": selected,
        "outputs": {"shared_context": source_output, "jump": jump_output, "idle": idle_output, "figure": figure_path.as_posix()},
    }
    metadata_path = output_dir / "metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    print(f"Device: {device}")
    print(f"Saved figure: {figure_path}")
    print(f"Saved metadata: {metadata_path}")
    print(f"Jump p(death): {[round(value, 3) for value in jump_probs]}")
    print(f"Idle p(death): {[round(value, 3) for value in idle_probs]}")


if __name__ == "__main__":
    main()

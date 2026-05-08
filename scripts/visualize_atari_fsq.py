"""Create an Atari FSQ reconstruction contact sheet.

Usage:
    python scripts/visualize_atari_fsq.py --config configs/atari/atari_pong_v0.yaml
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import torch
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deepdash.config import load_config
from deepdash.fsq import FSQVAE


def load_sample_frames(episodes_dir, n_frames):
    episode_dirs = sorted(
        ep for ep in Path(episodes_dir).glob("*")
        if (ep / "frames.npy").exists()
    )
    if not episode_dirs:
        raise FileNotFoundError(f"no episodes with frames.npy found in {episodes_dir}")

    per_episode = max(1, int(np.ceil(n_frames / len(episode_dirs))))
    frames = []
    for ep in episode_dirs:
        arr = np.load(ep / "frames.npy", mmap_mode="r")
        if len(arr) == 0:
            continue
        idx = np.linspace(0, len(arr) - 1, min(per_episode, len(arr)), dtype=int)
        frames.extend(np.asarray(arr[idx]))
        if len(frames) >= n_frames:
            break
    return np.stack(frames[:n_frames]).astype(np.uint8)


def load_sample_frames_from_replay(replay_dir, n_frames):
    from atari.replay_buffer import load_replay_arrays

    replay = load_replay_arrays(replay_dir)
    if len(replay.obs) == 0:
        raise ValueError(f"replay {replay_dir} is empty")
    idx = np.linspace(0, len(replay.obs) - 1, min(n_frames, len(replay.obs)), dtype=int)
    return np.asarray(replay.obs[idx], dtype=np.uint8)


@torch.no_grad()
def reconstruct(model, frames, batch_size, device):
    outs = []
    for i in range(0, len(frames), batch_size):
        batch = frames[i:i + batch_size]
        x = torch.from_numpy(batch).float().permute(0, 3, 1, 2).to(device) / 255.0
        recon, _, _ = model(x)
        y = (recon.permute(0, 2, 3, 1).cpu().numpy() * 255.0).clip(0, 255)
        outs.append(y.astype(np.uint8))
    return np.concatenate(outs, axis=0)


def make_sheet(originals, reconstructions, columns):
    tile_h, tile_w = originals.shape[1:3]
    rows = int(np.ceil(len(originals) / columns))
    label_h = 18
    pad = 4
    sheet_h = rows * (2 * tile_h + label_h + pad) + pad
    sheet_w = columns * (tile_w + pad) + pad
    sheet = np.full((sheet_h, sheet_w, 3), 24, dtype=np.uint8)

    for i, (orig, recon) in enumerate(zip(originals, reconstructions)):
        r, c = divmod(i, columns)
        x0 = pad + c * (tile_w + pad)
        y0 = pad + r * (2 * tile_h + label_h + pad)
        sheet[y0:y0 + tile_h, x0:x0 + tile_w] = orig
        sheet[y0 + tile_h:y0 + 2 * tile_h, x0:x0 + tile_w] = recon
        pil = Image.fromarray(sheet)
        draw = ImageDraw.Draw(pil)
        draw.text((x0, y0 + 2 * tile_h + 4), "orig/recon", fill=(220, 220, 220))
        sheet = np.array(pil)
    return sheet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/atari/atari_pong_v0.yaml")
    parser.add_argument("--checkpoint", default=None)
    parser.add_argument("--episodes-dir", default=None)
    parser.add_argument("--replay-dir", default=None)
    parser.add_argument("--out", default="outputs/atari/pong_fsq_recon.png")
    parser.add_argument("--n-frames", type=int, default=32)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--columns", type=int, default=8)
    args = parser.parse_args()

    cfg = load_config(args.config, section="fsq")
    episodes_dir = args.episodes_dir or cfg["episodes_dir"]
    replay_dir = args.replay_dir or cfg.get("replay_dir")
    checkpoint = args.checkpoint or str(Path(cfg["checkpoint_dir"]) / "fsq_best.pt")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = FSQVAE(
        img_channels=int(cfg.get("img_channels", 3)),
        levels=cfg.get("levels", [8, 5, 5, 5]),
        norm_type=cfg.get("norm_type", "batch"),
        latent_grid=int(cfg.get("latent_grid", 8)),
    ).to(device)
    state = torch.load(checkpoint, map_location=device, weights_only=True)
    state = {k.removeprefix("_orig_mod."): v for k, v in state.items()}
    model.load_state_dict(state)
    model.eval()

    if replay_dir and any(Path(replay_dir).glob("shard_*.npz")):
        frames = load_sample_frames_from_replay(replay_dir, args.n_frames)
    else:
        frames = load_sample_frames(episodes_dir, args.n_frames)
    recon = reconstruct(model, frames, args.batch_size, device)
    sheet = make_sheet(frames, recon, args.columns)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(sheet).save(out_path)
    print(f"Saved {out_path}")


if __name__ == "__main__":
    main()

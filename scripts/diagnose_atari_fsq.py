"""Diagnose Atari FSQ token and reconstruction diversity.

This is meant to catch background-only tokenizers where flat code usage looks
nonzero but each frame is mapped to nearly the same 8x8 token template.
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atari.replay_buffer import load_replay_arrays
from deepdash.config import load_config
from deepdash.fsq import FSQVAE


def sample_frames(replay_dir, n_frames):
    replay = load_replay_arrays(replay_dir)
    if len(replay.obs) == 0:
        raise ValueError(f"empty replay: {replay_dir}")
    idx = np.linspace(0, len(replay.obs) - 1, min(n_frames, len(replay.obs)), dtype=int)
    return np.asarray(replay.obs[idx], dtype=np.uint8)


@torch.no_grad()
def encode_reconstruct(model, frames, batch_size, device):
    all_indices, all_recon = [], []
    for i in range(0, len(frames), batch_size):
        batch = frames[i:i + batch_size]
        x = torch.from_numpy(batch).float().permute(0, 3, 1, 2).to(device) / 255.0
        recon, _, indices = model(x)
        all_indices.append(indices.cpu())
        all_recon.append(recon.cpu())
    return torch.cat(all_indices, dim=0), torch.cat(all_recon, dim=0)


def mean_pairwise_hamming(tokens, max_pairs=4096):
    flat = tokens.reshape(tokens.shape[0], -1)
    n = flat.shape[0]
    if n < 2:
        return 0.0
    rng = np.random.default_rng(0)
    i = rng.integers(0, n, size=max_pairs)
    j = rng.integers(0, n, size=max_pairs)
    mask = i != j
    i, j = i[mask], j[mask]
    if len(i) == 0:
        return 0.0
    diff = (flat[i] != flat[j]).float().mean(dim=1)
    return float(diff.mean().item())


def saliency_split_errors(frames, recon):
    x = torch.from_numpy(frames).float().permute(0, 3, 1, 2) / 255.0
    gray = x.mean(dim=1, keepdim=True)
    dx = torch.nn.functional.pad((gray[:, :, :, 1:] - gray[:, :, :, :-1]).abs(), (0, 1, 0, 0))
    dy = torch.nn.functional.pad((gray[:, :, 1:, :] - gray[:, :, :-1, :]).abs(), (0, 0, 0, 1))
    sal = (dx + dy).flatten(1)
    err = (recon - x).abs().mean(dim=1).flatten(1)
    k = max(1, int(0.05 * sal.shape[1]))
    top_idx = sal.topk(k, dim=1).indices
    fg_err = err.gather(1, top_idx).mean()
    bg_mask = torch.ones_like(err, dtype=torch.bool)
    bg_mask.scatter_(1, top_idx, False)
    bg_err = err[bg_mask].mean()
    return float(fg_err.item()), float(bg_err.item())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/atari/atari_pong_v0.yaml")
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--replay-dir", default=None)
    parser.add_argument("--n-frames", type=int, default=1024)
    parser.add_argument("--batch-size", type=int, default=256)
    args = parser.parse_args()

    cfg = load_config(args.config, section="fsq")
    replay_dir = args.replay_dir or cfg["replay_dir"]
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    model = FSQVAE(
        img_channels=int(cfg.get("img_channels", 3)),
        levels=cfg.get("levels", [8, 5, 5, 5]),
        norm_type=cfg.get("norm_type", "batch"),
        latent_grid=int(cfg.get("latent_grid", 8)),
    ).to(device)
    state = torch.load(args.checkpoint, map_location=device, weights_only=True)
    state = {k.removeprefix("_orig_mod."): v for k, v in state.items()}
    model.load_state_dict(state)
    model.eval()

    frames = sample_frames(replay_dir, args.n_frames)
    indices, recon = encode_reconstruct(model, frames, args.batch_size, device)
    flat = indices.reshape(indices.shape[0], -1)
    unique_codes = int(indices.unique().numel())
    unique_templates = int(torch.unique(flat, dim=0).shape[0])
    position_unique = torch.stack([
        indices[:, r, c].unique().numel().float()
        for r in range(indices.shape[1])
        for c in range(indices.shape[2])
    ])
    fg_err, bg_err = saliency_split_errors(frames, recon)

    x = torch.from_numpy(frames).float().permute(0, 3, 1, 2) / 255.0
    print(f"frames: {len(frames)}")
    print(f"flat code usage: {unique_codes}/{model.codebook_size} ({100 * unique_codes / model.codebook_size:.2f}%)")
    print(f"unique frame token maps: {unique_templates}/{len(frames)} ({100 * unique_templates / len(frames):.2f}%)")
    print(f"mean pairwise token-map hamming: {mean_pairwise_hamming(indices):.4f}")
    print(f"mean unique codes per spatial position: {float(position_unique.mean().item()):.2f}")
    print(f"input pixel std: {float(x.std(dim=0).mean().item()):.5f}")
    print(f"recon pixel std: {float(recon.std(dim=0).mean().item()):.5f}")
    print(f"top-5% edge pixel L1: {fg_err:.5f}")
    print(f"remaining pixel L1: {bg_err:.5f}")


if __name__ == "__main__":
    main()

"""Train the FSQ-VAE on raw Geometry Dash frame pairs.

V7 Phase 0 reproduction of V3-deploy's FSQ recipe (commit 66d5e2c) with
two corrections relative to the original:

  1. Globbing filter excludes shift-augmented episode dirs
     (``_s[+-]\\d+_[+-]\\d+`` suffix). The V3-deploy code accidentally
     globbed those as if they were independent episodes, multiplying the
     epoch length 5-15x. See ``project_v3_fsq_aug_bug.md``.

  2. ``--steps-per-epoch`` knob caps gradient steps per epoch. Use this
     to restore the lost compute by training longer (e.g. ``--epochs 1000``
     or via the cap), without resurrecting the duplication bug.

YAML config support via ``apply_config(section="fsq")`` — same convention
as ``train_world_model.py``.
"""

import argparse
import csv
import math
import re
import signal
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deepdash.fsq import FSQVAE, fsqvae_loss, fsq_marginal_uniform_reg, grwm_slowness
from deepdash.wandb_utils import wandb_init, wandb_log, wandb_finish


SHIFT_AUG_RE = re.compile(r"_s[+-]\d+_[+-]\d+$")


def codebook_stats(code_counts):
    """Usage % and normalized perplexity from a (codebook_size,) histogram.

    Mirrors scripts/train_world_model.py::_codebook_stats so panels stay
    comparable across joint and sequential FSQ runs.
        usage_pct: fraction of codes observed at least once, in percent.
        ppl_pct: exp(H) / codebook_size, in percent. 100% = uniform usage,
                 ~0% = fully collapsed. Cross-codebook-size comparable.
    """
    vocab_size = int(code_counts.numel())
    total = int(code_counts.sum().item())
    if total == 0:
        return 0.0, 0.0
    n_used = int((code_counts > 0).sum().item())
    usage_pct = 100.0 * n_used / vocab_size
    p = code_counts.to(torch.float64) / float(total)
    p_nz = p[p > 0]
    entropy = float(-(p_nz * p_nz.log()).sum().item())
    ppl_pct = 100.0 * math.exp(entropy) / vocab_size
    return usage_pct, ppl_pct


class FramePairDataset(Dataset):
    """Loads consecutive frame pairs from episodes for GRWM temporal slowness.

    Each sample is (frame_t, frame_t+1). Split by episode, not by frame.
    """

    def __init__(self, episode_dirs, device=None):
        pairs_t, pairs_t1 = [], []
        for ep_dir in episode_dirs:
            frames = np.load(ep_dir / "frames.npy")  # (T, H, W) grayscale OR (T, H, W, C) RGB
            for i in range(len(frames) - 1):
                pairs_t.append(frames[i])
                pairs_t1.append(frames[i + 1])
        t_data = np.stack(pairs_t)
        t1_data = np.stack(pairs_t1)
        # Grayscale (T, H, W) -> add channel dim. RGB (T, H, W, C) -> permute to channels-first.
        if t_data.ndim == 3:
            self.frames_t = torch.from_numpy(t_data).float().unsqueeze(1) / 255.0
            self.frames_t1 = torch.from_numpy(t1_data).float().unsqueeze(1) / 255.0
        elif t_data.ndim == 4:
            self.frames_t = torch.from_numpy(t_data).float().permute(0, 3, 1, 2) / 255.0
            self.frames_t1 = torch.from_numpy(t1_data).float().permute(0, 3, 1, 2) / 255.0
        else:
            raise ValueError(f"frames.npy must be (T, H, W) or (T, H, W, C); got shape {t_data.shape}")
        if device and device.type == "cuda":
            self.frames_t = self.frames_t.to(device)
            self.frames_t1 = self.frames_t1.to(device)

    def __len__(self):
        return len(self.frames_t)

    def __getitem__(self, idx):
        return self.frames_t[idx], self.frames_t1[idx]


class TensorFramePairDataset(Dataset):
    """Frame-pair dataset built from already materialized NumPy arrays."""

    def __init__(self, frames_t, frames_t1, device=None):
        self.frames_t = frames_to_tensor(frames_t)
        self.frames_t1 = frames_to_tensor(frames_t1)
        if device and device.type == "cuda":
            self.frames_t = self.frames_t.to(device)
            self.frames_t1 = self.frames_t1.to(device)

    def __len__(self):
        return len(self.frames_t)

    def __getitem__(self, idx):
        return self.frames_t[idx], self.frames_t1[idx]


def frames_to_tensor(frames):
    """Convert uint8 frames to NCHW float tensors."""
    data = np.asarray(frames)
    if data.ndim == 3:
        return torch.from_numpy(data).float().unsqueeze(1) / 255.0
    if data.ndim == 4:
        return torch.from_numpy(data).float().permute(0, 3, 1, 2) / 255.0
    raise ValueError(f"frames must be (T, H, W) or (T, H, W, C); got {data.shape}")


def build_replay_datasets(replay_dir, val_ratio, seed, device=None):
    """Load replay shards and split frame pairs by episode id."""
    from atari.replay_buffer import load_replay_arrays

    replay = load_replay_arrays(replay_dir)
    if len(replay.obs) < 2:
        raise ValueError(f"replay {replay_dir} has fewer than two transitions")

    pair_mask = (
        (replay.episode_ids[:-1] == replay.episode_ids[1:])
        & (~replay.dones[:-1])
    )
    pair_starts = np.nonzero(pair_mask)[0]
    if len(pair_starts) == 0:
        raise ValueError(f"replay {replay_dir} has no consecutive frame pairs")

    episode_ids = replay.episode_ids[pair_starts]
    unique_eps = np.unique(episode_ids)
    rng = np.random.default_rng(seed)
    rng.shuffle(unique_eps)
    n_val = max(1, int(len(unique_eps) * val_ratio)) if len(unique_eps) > 1 else 1
    val_eps = set(unique_eps[:n_val].tolist())
    is_val = np.asarray([ep in val_eps for ep in episode_ids], dtype=bool)

    train_idx = pair_starts[~is_val]
    val_idx = pair_starts[is_val]
    if len(train_idx) == 0:
        train_idx = val_idx
    if len(val_idx) == 0:
        val_idx = train_idx

    train_dataset = TensorFramePairDataset(
        replay.obs[train_idx], replay.obs[train_idx + 1], device=device)
    val_dataset = TensorFramePairDataset(
        replay.obs[val_idx], replay.obs[val_idx + 1], device=device)
    return train_dataset, val_dataset, len(unique_eps), len(val_eps)


def augment_batch(ft, ft1, pad=4, size=64):
    """Per-sample random shift augmentation via grid_sample with edge padding."""
    B = ft.size(0)
    di = torch.randint(0, 2 * pad + 1, (B,), device=ft.device, dtype=ft.dtype)
    dj = torch.randint(0, 2 * pad + 1, (B,), device=ft.device, dtype=ft.dtype)
    shift_i = (di - pad) / (size / 2)
    shift_j = (dj - pad) / (size / 2)
    grid_y = torch.linspace(-1, 1, size, device=ft.device)
    grid_x = torch.linspace(-1, 1, size, device=ft.device)
    gy, gx = torch.meshgrid(grid_y, grid_x, indexing="ij")
    grid = torch.stack([gx, gy], dim=-1).unsqueeze(0).expand(B, -1, -1, -1)
    grid = grid.clone()
    grid[..., 0] += shift_j.view(B, 1, 1)
    grid[..., 1] += shift_i.view(B, 1, 1)
    out_t = F.grid_sample(ft, grid, mode="nearest", padding_mode="border", align_corners=True)
    out_t1 = F.grid_sample(ft1, grid, mode="nearest", padding_mode="border", align_corners=True)
    return out_t, out_t1


def frame_saliency(target, other=None, mode="edge_temporal"):
    """Return a normalized per-pixel importance map for small Atari objects."""
    if mode == "none":
        return None

    gray = target.mean(dim=1, keepdim=True)
    saliency = torch.zeros_like(gray)
    if mode in ("edge", "edge_temporal"):
        dx = F.pad((gray[:, :, :, 1:] - gray[:, :, :, :-1]).abs(), (0, 1, 0, 0))
        dy = F.pad((gray[:, :, 1:, :] - gray[:, :, :-1, :]).abs(), (0, 0, 0, 1))
        saliency = saliency + dx + dy
    if mode in ("temporal", "edge_temporal"):
        if other is None:
            raise ValueError(f"{mode} saliency requires the adjacent frame")
        saliency = saliency + (target - other).abs().mean(dim=1, keepdim=True)
    if mode not in ("edge", "temporal", "edge_temporal"):
        raise ValueError(f"unknown recon_weight_mode: {mode}")

    saliency_mean = saliency.flatten(1).mean(dim=1).view(-1, 1, 1, 1)
    return saliency / (saliency_mean + 1e-6)


def reconstruction_loss(recon, target, loss_type, reduction,
                        weight_mode="none", foreground_weight=0.0,
                        foreground_weight_max=25.0, other=None):
    """Pixel loss with optional normalized foreground/edge weighting."""
    if loss_type == "l1":
        per_pixel = (recon - target).abs()
    elif loss_type == "mse":
        per_pixel = (recon - target).pow(2)
    else:
        raise ValueError(f"unknown loss_type: {loss_type}")

    if weight_mode != "none" and foreground_weight > 0.0:
        saliency = frame_saliency(target, other=other, mode=weight_mode)
        weights = 1.0 + foreground_weight * saliency
        weights = weights.clamp(max=foreground_weight_max)
        per_pixel = per_pixel * weights

    if reduction == "mean":
        return per_pixel.mean()
    if reduction == "sum":
        return per_pixel.sum() / target.size(0)
    raise ValueError(f"unknown reduction: {reduction}")


def train_epoch(model, loader, optimizer, alpha_slow, alpha_uniform,
                recon_loss_type='mse', recon_reduction='sum',
                recon_weight_mode='none', foreground_weight=0.0,
                foreground_weight_max=25.0, perceptual_model=None,
                perceptual_weight=0.0, amp_dtype=None,
                scaler=None, augment=True, max_steps=0):
    model.train()
    total_recon, total_perceptual, total_slow, total_uniform, n = 0.0, 0.0, 0.0, 0.0, 0
    step = 0
    for ft, ft1 in loader:
        if augment:
            ft, ft1 = augment_batch(ft, ft1)
        with torch.amp.autocast("cuda", enabled=amp_dtype is not None, dtype=amp_dtype):
            recon_t, z_e_t, _ = model(ft)
            recon_t1, z_e_t1, _ = model(ft1)
            recon_loss = (
                reconstruction_loss(
                    recon_t, ft, recon_loss_type, recon_reduction,
                    recon_weight_mode, foreground_weight, foreground_weight_max,
                    other=ft1)
                + reconstruction_loss(
                    recon_t1, ft1, recon_loss_type, recon_reduction,
                    recon_weight_mode, foreground_weight, foreground_weight_max,
                    other=ft)
            ) / 2
            if perceptual_model is not None and perceptual_weight > 0.0:
                perceptual_loss = (
                    perceptual_model(recon_t, ft)
                    + perceptual_model(recon_t1, ft1)
                ) / 2
            else:
                perceptual_loss = torch.zeros((), device=ft.device, dtype=ft.dtype)
            slow_loss = grwm_slowness(z_e_t, z_e_t1)
            if alpha_uniform > 0.0:
                underlying = model._orig_mod if hasattr(model, "_orig_mod") else model
                z_bounded = torch.stack([
                    underlying.fsq.bound(z_e_t),
                    underlying.fsq.bound(z_e_t1),
                ], dim=1)
                uniform_loss = fsq_marginal_uniform_reg(
                    z_bounded, underlying.fsq.half_levels)
            else:
                uniform_loss = torch.zeros((), device=ft.device, dtype=ft.dtype)
            loss = (recon_loss
                    + perceptual_weight * perceptual_loss
                    + alpha_slow * slow_loss
                    + alpha_uniform * uniform_loss)
        optimizer.zero_grad(set_to_none=True)
        if scaler is not None and scaler.is_enabled():
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()
        else:
            loss.backward()
            optimizer.step()
        bs = ft.size(0)
        total_recon += recon_loss.item() * bs
        total_perceptual += perceptual_loss.item() * bs
        total_slow += slow_loss.item() * bs
        total_uniform += uniform_loss.item() * bs
        n += bs
        step += 1
        if max_steps and step >= max_steps:
            break
    return total_recon / n, total_perceptual / n, total_slow / n, total_uniform / n


@torch.no_grad()
def val_epoch(model, loader, codebook_size, recon_loss_type='mse',
              recon_reduction='sum', recon_weight_mode='none',
              foreground_weight=0.0, foreground_weight_max=25.0,
              perceptual_model=None, perceptual_weight=0.0,
              amp_dtype=None, device=None):
    """Returns (val_recon, usage_pct, ppl_pct) over the val set."""
    model.eval()
    total_recon, total_perceptual, n = 0.0, 0.0, 0
    code_counts = torch.zeros(codebook_size, device=device, dtype=torch.long)
    for ft, ft1 in loader:
        with torch.amp.autocast("cuda", enabled=amp_dtype is not None, dtype=amp_dtype):
            recon_t, _, indices = model(ft)
            recon_loss = reconstruction_loss(
                recon_t, ft, recon_loss_type, recon_reduction,
                recon_weight_mode, foreground_weight, foreground_weight_max,
                other=ft1)
            if perceptual_model is not None and perceptual_weight > 0.0:
                perceptual_loss = perceptual_model(recon_t, ft)
            else:
                perceptual_loss = torch.zeros((), device=ft.device, dtype=ft.dtype)
        code_counts.scatter_add_(
            0, indices.reshape(-1).long(),
            torch.ones_like(indices.reshape(-1), dtype=torch.long))
        bs = ft.size(0)
        total_recon += recon_loss.item() * bs
        total_perceptual += perceptual_loss.item() * bs
        n += bs
    usage_pct, ppl_pct = codebook_stats(code_counts)
    return total_recon / n, total_perceptual / n, usage_pct, ppl_pct


def main():
    parser = argparse.ArgumentParser(description="Train FSQ-VAE on Geometry Dash frames")
    parser.add_argument("--config", default=None, help="YAML config path")
    parser.add_argument("--episodes-dir", default=None)
    parser.add_argument("--expert-episodes-dir", default=None)
    parser.add_argument("--replay-dir", default=None,
                        help="Atari replay shard dir. If it contains shards, it overrides episode dirs.")
    parser.add_argument("--epochs", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--lr", type=float, default=None)
    parser.add_argument("--lr-min", type=float, default=None)
    parser.add_argument("--checkpoint-dir", default=None)
    parser.add_argument("--resume", default=None, help="Path to checkpoint to resume from")
    parser.add_argument("--levels", type=int, nargs="+", default=None)
    parser.add_argument("--latent-grid", type=int, choices=[8, 16], default=None,
                        help="FSQ spatial token grid for 64x64 frames.")
    parser.add_argument("--img-channels", type=int, default=None,
                        help="1 for grayscale (V7 / GD default), 3 for RGB (Atari).")
    parser.add_argument("--norm-type", choices=["batch", "group"], default=None,
                        help="Normalization in FSQ ResBlocks. GroupNorm avoids BatchNorm eval-stat collapse.")
    parser.add_argument("--alpha-slow", type=float, default=None)
    parser.add_argument("--alpha-uniform", type=float, default=None)
    parser.add_argument("--recon-loss-type", choices=["mse", "l1"], default=None,
                        help="Tokenizer reconstruction loss. L1 avoids L2 average-blur attractors.")
    parser.add_argument("--recon-reduction", choices=["sum", "mean"], default=None,
                        help="sum is legacy per-sample pixel sum; mean makes regularizers comparable.")
    parser.add_argument("--augment-shift", action=argparse.BooleanOptionalAction,
                        default=None,
                        help="Apply DeepDash-style random pixel shifts during FSQ training.")
    parser.add_argument("--recon-weight-mode",
                        choices=["none", "edge", "temporal", "edge_temporal"],
                        default=None,
                        help="Optional foreground weighting for Atari-style small objects.")
    parser.add_argument("--foreground-weight", type=float, default=None,
                        help="Multiplier for normalized foreground/edge saliency.")
    parser.add_argument("--foreground-weight-max", type=float, default=None,
                        help="Clamp for per-pixel foreground weights.")
    parser.add_argument("--perceptual-loss", choices=["none", "vgg16", "lpips"],
                        default=None,
                        help="Differentiable perceptual tokenizer loss.")
    parser.add_argument("--perceptual-weight", type=float, default=None,
                        help="Weight for perceptual loss.")
    parser.add_argument("--val-ratio", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--steps-per-epoch", type=int, default=None,
                        help="Cap gradient steps per epoch (0/None = full loader). "
                             "Useful for restoring V3-deploy's accidental 5-15x "
                             "compute via longer training without aug-dir duplication.")
    parser.add_argument("--val-interval", type=int, default=None,
                        help="Run val + best-checkpoint selection every N "
                             "epochs (default 10). At epochs=1000 a per-"
                             "epoch val burns ~10%% of training time on a "
                             "frozen-distribution metric; default keeps the "
                             "per-step LR shape and best-selection signal "
                             "while spending much less compute on val.")
    parser.add_argument("--amp-dtype", choices=["bfloat16", "float16", "none"],
                        default=None,
                        help="Use float16 locally on RTX 2060; bfloat16 on BF16-capable supercomputer GPUs.")
    parser.add_argument("--compile-mode",
                        choices=["reduce-overhead", "default", "none"],
                        default=None, help="torch.compile mode. Default reduce-overhead.")
    args = parser.parse_args()

    from deepdash.config import apply_config
    apply_config(args, section="fsq")

    # Defaults if config missing them
    args.epochs = args.epochs or 200
    args.batch_size = args.batch_size or 2048
    args.lr = args.lr or 1e-3
    args.lr_min = args.lr_min if args.lr_min is not None else 1e-5
    args.checkpoint_dir = args.checkpoint_dir or "checkpoints"
    args.levels = args.levels or [8, 5, 5, 5]
    args.latent_grid = args.latent_grid or 8
    args.img_channels = args.img_channels if args.img_channels is not None else 1
    args.norm_type = args.norm_type or "batch"
    # Backward compat: GD defaults if neither YAML nor CLI provided paths.
    args.episodes_dir = args.episodes_dir or "data/deepdash/death_episodes"
    args.expert_episodes_dir = args.expert_episodes_dir or "data/deepdash/expert_episodes"
    args.alpha_slow = args.alpha_slow if args.alpha_slow is not None else 0.1
    args.alpha_uniform = args.alpha_uniform if args.alpha_uniform is not None else 0.01
    args.recon_loss_type = args.recon_loss_type or "mse"
    args.recon_reduction = args.recon_reduction or "sum"
    args.augment_shift = True if args.augment_shift is None else args.augment_shift
    args.recon_weight_mode = args.recon_weight_mode or "none"
    args.foreground_weight = args.foreground_weight if args.foreground_weight is not None else 0.0
    args.foreground_weight_max = (
        args.foreground_weight_max if args.foreground_weight_max is not None else 25.0)
    args.perceptual_loss = args.perceptual_loss or "none"
    args.perceptual_weight = args.perceptual_weight if args.perceptual_weight is not None else 0.0
    args.amp_dtype = args.amp_dtype or "float16"
    args.compile_mode = args.compile_mode or "reduce-overhead"
    args.val_interval = args.val_interval if args.val_interval is not None else 10

    # W&B (graceful no-op if not installed / not logged in).
    wandb_init(project="deepdash",
               name=f"fsq-{args.latent_grid}x{args.latent_grid}-{'-'.join(str(x) for x in args.levels)}",
               config=vars(args))

    def _sigterm_handler(sig, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _sigterm_handler)

    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)
        torch.backends.cudnn.benchmark = True

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")

    from deepdash.perceptual import build_perceptual_loss
    perceptual_model = build_perceptual_loss(args.perceptual_loss, device)
    if perceptual_model is not None:
        print(f"Perceptual loss: {args.perceptual_loss} weight={args.perceptual_weight}")

    replay_dir = Path(args.replay_dir) if args.replay_dir else None
    if replay_dir and any(replay_dir.glob("shard_*.npz")):
        train_dataset, val_dataset, n_eps, n_val_eps = build_replay_datasets(
            replay_dir, args.val_ratio, args.seed, device=device)
        print(f"Replay: {replay_dir}  episodes={n_eps}  val_episodes={n_val_eps}")
    else:
        from deepdash.data_split import get_val_episodes, is_val_episode
        val_set = get_val_episodes(args.episodes_dir, args.expert_episodes_dir)

        all_episodes = []
        for ep_dir in [args.episodes_dir, args.expert_episodes_dir]:
            p = Path(ep_dir)
            if p.exists():
                all_episodes.extend(
                    ep for ep in sorted(p.glob("*"))
                    if (ep / "frames.npy").exists()
                    and not SHIFT_AUG_RE.search(ep.name)  # bug fix: skip aug_dirs
                )

        train_eps = [ep for ep in all_episodes if not is_val_episode(ep.name, val_set)]
        val_eps = [ep for ep in all_episodes if is_val_episode(ep.name, val_set)]
        print(f"Episodes: {len(all_episodes)} total, {len(train_eps)} train, {len(val_eps)} val "
              f"(shift-aug dirs filtered out)")

        train_dataset = FramePairDataset(train_eps, device=device)
        val_dataset = FramePairDataset(val_eps, device=device)
    print(f"Frame pairs: {len(train_dataset)} train, {len(val_dataset)} val")

    train_loader = DataLoader(train_dataset, batch_size=args.batch_size,
                              shuffle=True, num_workers=0, pin_memory=False)
    val_loader = DataLoader(val_dataset, batch_size=args.batch_size,
                            shuffle=False, num_workers=0, pin_memory=False)

    model = FSQVAE(img_channels=args.img_channels, levels=args.levels,
                   norm_type=args.norm_type,
                   latent_grid=args.latent_grid).to(device)
    if args.resume:
        state = torch.load(args.resume, map_location=device, weights_only=True)
        state = {k.removeprefix("_orig_mod."): v for k, v in state.items()}
        model.load_state_dict(state)
        print(f"Resumed from {args.resume}")

    param_count = sum(p.numel() for p in model.parameters())
    print(f"Model parameters: {param_count:,}")
    print(f"FSQ levels: {args.levels} -> {model.codebook_size} implicit codes")
    print(f"FSQ latent grid: {args.latent_grid}x{args.latent_grid} "
          f"({args.latent_grid * args.latent_grid} tokens/frame)")

    if args.compile_mode != "none":
        try:
            model = torch.compile(model, mode=args.compile_mode)
            print(f"torch.compile enabled (mode={args.compile_mode})")
        except Exception as e:
            print(f"torch.compile failed ({e}), continuing without it")

    amp_dtype = None
    if args.amp_dtype == "bfloat16":
        amp_dtype = torch.bfloat16
    elif args.amp_dtype == "float16":
        amp_dtype = torch.float16
    if amp_dtype is not None:
        print(f"AMP enabled with {amp_dtype}")
    scaler = torch.amp.GradScaler("cuda", enabled=(amp_dtype == torch.float16 and device.type == "cuda"))
    if scaler.is_enabled():
        print("GradScaler enabled for float16 AMP")

    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=args.epochs, eta_min=args.lr_min)

    ckpt_dir = Path(args.checkpoint_dir)
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    best_val_loss = float("inf")

    log_path = ckpt_dir / "fsq_log.csv"
    log_file = open(log_path, "w", newline="")
    log_writer = csv.writer(log_file)
    log_writer.writerow(["epoch", "train_recon", "train_perceptual",
                         "train_slow", "train_uniform",
                         "val_recon", "val_perceptual",
                         "val_usage_pct", "val_ppl_pct",
                         "lr", "time_s"])

    # Codebook size (for histogram pre-allocation in val_epoch).
    underlying = model._orig_mod if hasattr(model, "_orig_mod") else model
    codebook_size = underlying.codebook_size

    max_steps = args.steps_per_epoch or 0
    val_interval = max(1, int(args.val_interval))
    print(f"Val interval: every {val_interval} epoch(s)")
    try:
        for epoch in range(1, args.epochs + 1):
            t0 = time.time()
            train_recon, train_perceptual, train_slow, train_uniform = train_epoch(
                model, train_loader, optimizer, args.alpha_slow, args.alpha_uniform,
                recon_loss_type=args.recon_loss_type,
                recon_reduction=args.recon_reduction,
                recon_weight_mode=args.recon_weight_mode,
                foreground_weight=args.foreground_weight,
                foreground_weight_max=args.foreground_weight_max,
                perceptual_model=perceptual_model,
                perceptual_weight=args.perceptual_weight,
                amp_dtype=amp_dtype,
                scaler=scaler,
                augment=args.augment_shift, max_steps=max_steps)
            scheduler.step()

            # Val every val_interval epochs, plus the final epoch.
            do_val = (epoch % val_interval == 0) or (epoch == args.epochs)
            if do_val:
                val_recon, val_perceptual, val_usage, val_ppl = val_epoch(
                    model, val_loader, codebook_size,
                    recon_loss_type=args.recon_loss_type,
                    recon_reduction=args.recon_reduction,
                    recon_weight_mode=args.recon_weight_mode,
                    foreground_weight=args.foreground_weight,
                    foreground_weight_max=args.foreground_weight_max,
                    perceptual_model=perceptual_model,
                    perceptual_weight=args.perceptual_weight,
                    amp_dtype=amp_dtype, device=device)
            else:
                val_recon = val_perceptual = val_usage = val_ppl = None
            dt = time.time() - t0
            lr = optimizer.param_groups[0]["lr"]

            val_str = (f"recon={val_recon:.4f} perc={val_perceptual:.4f} "
                       f"usage={val_usage:.1f}% ppl={val_ppl:.1f}%"
                       if do_val else "skipped")
            print(
                f"Epoch {epoch:4d}/{args.epochs} ({dt:.1f}s) | "
                f"Train: recon={train_recon:.4f} perc={train_perceptual:.4f} "
                f"slow={train_slow:.4f} unif={train_uniform:.4f} | "
                f"Val: {val_str} | LR: {lr:.1e}"
            )
            log_writer.writerow([
                epoch, f"{train_recon:.6f}", f"{train_perceptual:.6f}",
                f"{train_slow:.6f}", f"{train_uniform:.6f}",
                f"{val_recon:.6f}" if do_val else "",
                f"{val_perceptual:.6f}" if do_val else "",
                f"{val_usage:.4f}" if do_val else "",
                f"{val_ppl:.4f}" if do_val else "",
                f"{lr:.1e}", f"{dt:.1f}"
            ])
            log_file.flush()

            wandb_payload = {
                "epoch": epoch,
                "fsq/train/recon": train_recon,
                "fsq/train/perceptual": train_perceptual,
                "fsq/train/slow": train_slow,
                "fsq/train/unif": train_uniform,
                "fsq/lr": lr,
                "fsq/epoch_time_s": dt,
            }
            if do_val:
                wandb_payload["fsq/val/recon"] = val_recon
                wandb_payload["fsq/val/perceptual"] = val_perceptual
                wandb_payload["fsq/val/usage_pct"] = val_usage
                wandb_payload["fsq/val/ppl_pct"] = val_ppl
            wandb_log(wandb_payload)

            if do_val:
                val_loss_for_selection = val_recon + args.perceptual_weight * val_perceptual
            if do_val and val_loss_for_selection < best_val_loss:
                best_val_loss = val_loss_for_selection
                clean_state = {k.removeprefix("_orig_mod."): v
                               for k, v in model.state_dict().items()}
                torch.save(clean_state, ckpt_dir / "fsq_best.pt")
    except KeyboardInterrupt:
        print("\nInterrupted - saving final checkpoint...")

    log_file.close()
    wandb_finish()
    clean_state = {k.removeprefix("_orig_mod."): v
                   for k, v in model.state_dict().items()}
    torch.save(clean_state, ckpt_dir / "fsq_final.pt")
    print(f"\nTraining complete. Best val objective: {best_val_loss:.4f}")
    print(f"Checkpoints saved to {ckpt_dir}/")


if __name__ == "__main__":
    main()

"""Train/evaluate an Atari token predictor from replay shards.

This is the local-first Atari predictor path used before controller training.
It compares CE vs SLS on frozen FSQ tokens and reports rollout metrics at
controller-relevant horizons, especially H=15.

Examples:
    python scripts/train_atari_predictor.py --config configs/atari/atari_pong_v0.yaml
    python scripts/train_atari_predictor.py --config configs/atari/atari_pong_v0.yaml --label-smoothing 0.1 --checkpoint-dir checkpoints_atari_pong_v0_predictor_sls
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atari.predictor import AtariPredictorWithHeads, split_atari_predictor_state
from atari.replay_buffer import load_metadata, load_replay_arrays
from deepdash.config import apply_config, load_config
from deepdash.fsq import FSQVAE
from deepdash.world_model import WorldModel
from scripts.train_world_model import build_structured_smooth_targets, focal_cross_entropy


def _amp_dtype(name: str | None):
    if name == "float16":
        return torch.float16
    if name == "bfloat16":
        return torch.bfloat16
    return None


def _fsq_coords(levels: list[int], device: torch.device) -> torch.Tensor:
    vocab_size = math.prod(levels)
    divisors = []
    acc = 1
    for level in reversed(levels):
        divisors.append(acc)
        acc *= level
    divisors.reverse()
    coords = torch.zeros(vocab_size, len(levels), device=device)
    idx = torch.arange(vocab_size, device=device)
    remainder = idx.clone()
    for dim, div in enumerate(divisors):
        coords[:, dim] = torch.div(remainder, div, rounding_mode="floor")
        remainder = torch.remainder(remainder, div)
    return coords


@torch.no_grad()
def encode_replay_obs(fsq: FSQVAE, obs: np.ndarray, batch_size: int,
                      device: torch.device) -> torch.Tensor:
    """Encode uint8 NHWC replay observations to flattened FSQ tokens."""
    fsq.eval()
    out = []
    for i in range(0, len(obs), batch_size):
        batch = obs[i:i + batch_size]
        x = torch.from_numpy(batch).float().permute(0, 3, 1, 2).to(device) / 255.0
        tokens = fsq.encode(x).reshape(x.size(0), -1)
        out.append(tokens.cpu())
    return torch.cat(out, dim=0)


class AtariTokenDataset(Dataset):
    def __init__(self, starts: np.ndarray, tokens: torch.Tensor,
                 actions: np.ndarray, rewards: np.ndarray, dones: np.ndarray,
                 context_frames: int):
        self.starts = np.asarray(starts, dtype=np.int64)
        self.tokens = tokens
        self.actions = torch.from_numpy(actions.astype(np.int64))
        self.rewards = torch.from_numpy(rewards.astype(np.float32))
        self.dones = torch.from_numpy(dones.astype(np.float32))
        self.context_frames = int(context_frames)

    def __len__(self):
        return len(self.starts)

    def __getitem__(self, idx):
        i = int(self.starts[idx])
        k = self.context_frames
        target_transition = i + k - 1
        return (
            self.tokens[i:i + k + 1],
            self.actions[i:i + k],
            self.rewards[target_transition],
            self.dones[target_transition],
        )


class AtariRolloutDataset(Dataset):
    def __init__(self, starts: np.ndarray, tokens: torch.Tensor,
                 actions: np.ndarray, obs: np.ndarray, context_frames: int,
                 max_horizon: int):
        self.starts = np.asarray(starts, dtype=np.int64)
        self.tokens = tokens
        self.actions = torch.from_numpy(actions.astype(np.int64))
        self.obs = obs
        self.context_frames = int(context_frames)
        self.max_horizon = int(max_horizon)

    def __len__(self):
        return len(self.starts)

    def __getitem__(self, idx):
        i = int(self.starts[idx])
        k = self.context_frames
        h = self.max_horizon
        return (
            self.tokens[i:i + k],
            self.actions[i:i + k + h],
            torch.from_numpy(self.obs[i + k:i + k + h]).float().permute(0, 3, 1, 2) / 255.0,
        )


def split_starts(replay, context_frames: int, max_horizon: int,
                 val_ratio: float, seed: int):
    episode_ids = replay.episode_ids
    dones = replay.dones
    n = len(episode_ids)

    next_starts = []
    rollout_starts = []
    for i in range(0, n - context_frames):
        end = i + context_frames
        if end >= n:
            break
        same_ep = np.all(episode_ids[i:end + 1] == episode_ids[i])
        no_done = not np.any(dones[i:end])
        if same_ep and no_done:
            next_starts.append(i)
        rh_end = i + context_frames + max_horizon
        if rh_end <= n:
            same_rollout = np.all(episode_ids[i:rh_end] == episode_ids[i])
            no_rollout_done = not np.any(dones[i:rh_end - 1])
            if same_rollout and no_rollout_done:
                rollout_starts.append(i)

    next_starts = np.asarray(next_starts, dtype=np.int64)
    rollout_starts = np.asarray(rollout_starts, dtype=np.int64)
    eps = np.unique(episode_ids[next_starts]) if len(next_starts) else np.unique(episode_ids)
    rng = np.random.default_rng(seed)
    rng.shuffle(eps)
    n_val = max(1, int(len(eps) * val_ratio)) if len(eps) > 1 else 1
    val_eps = set(eps[:n_val].tolist())

    def by_split(starts):
        is_val = np.asarray([episode_ids[i] in val_eps for i in starts], dtype=bool)
        train = starts[~is_val]
        val = starts[is_val]
        if len(train) == 0:
            train = val
        if len(val) == 0:
            val = train
        return train, val

    return (*by_split(next_starts), *by_split(rollout_starts), len(eps), len(val_eps))


def compute_loss(logits, targets, vocab_size, soft_target_matrix,
                 label_smoothing, focal_gamma):
    visual_logits = logits[:, :, :vocab_size].reshape(-1, vocab_size)
    visual_targets = targets.reshape(-1)
    return focal_cross_entropy(
        visual_logits,
        visual_targets,
        gamma=focal_gamma,
        soft_target_matrix=soft_target_matrix,
        label_smoothing=label_smoothing if soft_target_matrix is None else 0.0,
    )


def clean_state_dict(module: nn.Module):
    return {k.removeprefix("_orig_mod."): v for k, v in module.state_dict().items()}


def predictor_state_for_world_model(predictor: nn.Module):
    state = clean_state_dict(predictor)
    return {
        k.removeprefix("world_model."): v
        for k, v in state.items()
        if k.startswith("world_model.")
    }


def load_predictor_weights(predictor: nn.Module, path: str | Path,
                           device: torch.device) -> None:
    """Load predictor weights from either a raw state dict or latest payload.

    This intentionally ignores optimizer, scheduler, epoch, and best metric
    state. It is used when the FSQ tokenizer has changed and we want a warm
    start from previous predictor weights, not continuation of the previous
    training run.
    """
    payload = torch.load(path, map_location=device, weights_only=False)
    state = payload.get("model", payload) if isinstance(payload, dict) else payload
    state = {k.removeprefix("_orig_mod."): v for k, v in state.items()}
    if "world_model.head.weight" in state:
        predictor.load_state_dict(state)
    else:
        wm_state, aux_state = split_atari_predictor_state(state)
        predictor.world_model.load_state_dict(wm_state)
        predictor.load_state_dict(aux_state, strict=False)


@torch.no_grad()
def eval_next_step(model, loader, device, vocab_size, soft_target_matrix,
                   label_smoothing, focal_gamma, coords, amp_dtype,
                   max_batches=0):
    model.eval()
    total_loss = total_acc = total_dist = total_tokens = 0.0
    total_reward_mae = total_done_correct = total_aux = 0.0
    for batch_idx, (frame_tokens, actions, rewards, dones) in enumerate(loader, start=1):
        frame_tokens = frame_tokens.to(device)
        actions = actions.to(device)
        rewards = rewards.to(device)
        dones = dones.to(device)
        with torch.amp.autocast("cuda", enabled=amp_dtype is not None, dtype=amp_dtype):
            logits, pred_reward, done_logit = model(frame_tokens, actions, return_aux=True)
            loss = compute_loss(logits, frame_tokens[:, -1], vocab_size, soft_target_matrix,
                                label_smoothing, focal_gamma)
        pred = logits[:, :, :vocab_size].argmax(dim=-1)
        tgt = frame_tokens[:, -1]
        n_tok = tgt.numel()
        total_loss += float(loss.item()) * n_tok
        total_acc += float((pred == tgt).sum().item())
        total_dist += float((coords[pred.reshape(-1)] - coords[tgt.reshape(-1)]).abs().sum(dim=-1).sum().item())
        total_tokens += n_tok
        total_reward_mae += float((pred_reward.float() - rewards).abs().sum().item())
        total_done_correct += float(((done_logit.float().sigmoid() >= 0.5) == (dones >= 0.5)).sum().item())
        total_aux += float(rewards.numel())
        if max_batches and batch_idx >= max_batches:
            break
    return {
        "nll": total_loss / max(total_tokens, 1),
        "acc": total_acc / max(total_tokens, 1),
        "fsq_l1_dist": total_dist / max(total_tokens, 1),
        "reward_mae": total_reward_mae / max(total_aux, 1),
        "done_acc": total_done_correct / max(total_aux, 1),
    }


@torch.no_grad()
def eval_rollouts(model, fsq, loader, horizons, device, vocab_size, amp_dtype,
                  max_batches=0):
    model.eval()
    fsq.eval()
    totals = {h: {"l1": 0.0, "acc": 0.0, "n": 0} for h in horizons}
    max_h = max(horizons)
    for batch_idx, (ctx_tokens, actions, target_frames) in enumerate(loader, start=1):
        ctx_tokens = ctx_tokens.to(device)
        actions = actions.to(device)
        target_frames = target_frames.to(device)
        pred_context = ctx_tokens.clone()
        for step in range(max_h):
            act_window = actions[:, step:step + model.context_frames]
            with torch.amp.autocast("cuda", enabled=amp_dtype is not None, dtype=amp_dtype):
                logits = model(pred_context, act_window)
            pred_next = logits[:, :, :vocab_size].argmax(dim=-1)
            if step + 1 in horizons:
                recon = fsq.decode_indices(pred_next.view(
                    pred_next.size(0), fsq.latent_grid, fsq.latent_grid))
                target = target_frames[:, step]
                l1 = F.l1_loss(recon, target, reduction="none").mean(dim=(1, 2, 3))
                tgt_tokens = ctx_tokens.new_empty(pred_next.shape)
                # The rollout dataset target frame index for this horizon is
                # context_start + K + step, so re-encode-free token accuracy
                # uses the ground-truth tokens already present after context.
                # These are reconstructed from the global token tensor by the
                # caller through ctx alignment, so for rollout eval we report
                # pixel L1 as the primary model-use metric.
                totals[step + 1]["l1"] += float(l1.sum().item())
                totals[step + 1]["n"] += int(pred_next.size(0))
            pred_context = torch.cat([pred_context[:, 1:], pred_next.unsqueeze(1)], dim=1)
        if max_batches and batch_idx >= max_batches:
            break
    return {
        f"rollout_{h}_l1": totals[h]["l1"] / max(totals[h]["n"], 1)
        for h in horizons
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/atari/atari_pong_v0.yaml")
    parser.add_argument("--config-section", default="predictor",
                        help="YAML section to read; use predictor_sls for the SLS condition.")
    parser.add_argument("--replay-dir", default=None)
    parser.add_argument("--fsq-checkpoint", default=None)
    parser.add_argument("--checkpoint-dir", default=None)
    parser.add_argument("--epochs", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--lr", type=float, default=None)
    parser.add_argument("--lr-min", type=float, default=None)
    parser.add_argument("--weight-decay", type=float, default=None)
    parser.add_argument("--label-smoothing", type=float, default=None,
                        help="0 = CE; >0 = SLS with fsq_kernel/fsq_sigma.")
    parser.add_argument("--fsq-kernel", default=None)
    parser.add_argument("--fsq-sigma", type=float, default=None)
    parser.add_argument("--focal-gamma", type=float, default=None)
    parser.add_argument("--reward-loss-weight", type=float, default=None)
    parser.add_argument("--done-loss-weight", type=float, default=None)
    parser.add_argument("--compile-mode", choices=["none", "default", "reduce-overhead"], default=None)
    parser.add_argument("--amp-dtype", choices=["none", "float16", "bfloat16"], default=None)
    parser.add_argument("--val-interval", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--steps-per-epoch", type=int, default=None)
    parser.add_argument("--max-val-batches", type=int, default=None,
                        help="Debug cap for next-step validation batches; 0 = full val set.")
    parser.add_argument("--max-rollout-batches", type=int, default=None,
                        help="Debug cap for rollout validation batches; 0 = full val set.")
    parser.add_argument("--init-from", default=None,
                        help="Warm-start model weights from a predictor checkpoint, "
                             "but reset optimizer/scheduler/epoch/best metric.")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from predictor_latest.pt in checkpoint-dir.")
    args = parser.parse_args()
    apply_config(args, section=args.config_section)

    args.epochs = args.epochs or 50
    args.batch_size = args.batch_size or 32
    args.lr = args.lr or 2e-4
    args.lr_min = args.lr_min if args.lr_min is not None else 1e-5
    args.weight_decay = args.weight_decay if args.weight_decay is not None else 0.01
    args.label_smoothing = args.label_smoothing if args.label_smoothing is not None else 0.0
    args.fsq_kernel = args.fsq_kernel or "gaussian"
    args.fsq_sigma = args.fsq_sigma if args.fsq_sigma is not None else 0.9
    args.focal_gamma = args.focal_gamma if args.focal_gamma is not None else 0.0
    args.reward_loss_weight = args.reward_loss_weight if args.reward_loss_weight is not None else 1.0
    args.done_loss_weight = args.done_loss_weight if args.done_loss_weight is not None else 1.0
    args.compile_mode = args.compile_mode or "reduce-overhead"
    args.amp_dtype = args.amp_dtype or "float16"
    args.val_interval = args.val_interval if args.val_interval is not None else 1
    args.seed = args.seed if args.seed is not None else 42
    args.max_val_batches = args.max_val_batches if args.max_val_batches is not None else 0
    args.max_rollout_batches = (
        args.max_rollout_batches if args.max_rollout_batches is not None else 0)

    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)
        torch.backends.cudnn.benchmark = True
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    amp_dtype = _amp_dtype(args.amp_dtype)
    print(f"Device: {device}")
    if amp_dtype is not None:
        print(f"AMP enabled with {amp_dtype}")

    cfg = load_config(args.config, section=args.config_section)
    fsq_cfg = load_config(args.config, section="fsq")
    model_cfg = load_config(args.config, section="model")
    horizons = [int(h) for h in cfg.get("eval_horizons", [1, 5, 10, 15])]
    max_horizon = max(horizons)

    replay = load_replay_arrays(args.replay_dir)
    metadata = load_metadata(args.replay_dir) or {}
    n_actions = int(getattr(args, "n_actions", None) or metadata.get("n_actions", 0) or cfg.get("n_actions", 6))
    print(f"Replay: {args.replay_dir}  steps={len(replay.obs)}  n_actions={n_actions}")

    fsq = FSQVAE(
        img_channels=int(fsq_cfg.get("img_channels", 3)),
        levels=fsq_cfg.get("levels", [8, 5, 5, 5]),
        norm_type=fsq_cfg.get("norm_type", "group"),
        latent_grid=int(fsq_cfg.get("latent_grid", 16)),
    ).to(device)
    state = torch.load(args.fsq_checkpoint, map_location=device, weights_only=True)
    state = {k.removeprefix("_orig_mod."): v for k, v in state.items()}
    fsq.load_state_dict(state)
    fsq.eval()
    print(f"Loaded tokenizer: {args.fsq_checkpoint}")

    tokens = encode_replay_obs(fsq, replay.obs, batch_size=256, device=device)
    print(f"Encoded replay tokens: {tuple(tokens.shape)}")

    k = int(model_cfg.get("context_frames", 4))
    train_starts, val_starts, rollout_train, rollout_val, n_eps, n_val_eps = split_starts(
        replay, k, max_horizon, float(cfg.get("val_ratio", 0.2)), args.seed)
    print(f"Windows: train={len(train_starts)} val={len(val_starts)} "
          f"rollout_val={len(rollout_val)} episodes={n_eps} val_episodes={n_val_eps}")

    train_ds = AtariTokenDataset(
        train_starts, tokens, replay.actions, replay.rewards, replay.dones, k)
    val_ds = AtariTokenDataset(
        val_starts, tokens, replay.actions, replay.rewards, replay.dones, k)
    rollout_ds = AtariRolloutDataset(rollout_val, tokens, replay.actions, replay.obs, k, max_horizon)
    train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True, num_workers=0)
    val_loader = DataLoader(val_ds, batch_size=args.batch_size, shuffle=False, num_workers=0)
    rollout_loader = DataLoader(rollout_ds, batch_size=args.batch_size, shuffle=False, num_workers=0)

    levels = list(model_cfg.get("levels", [8, 5, 5, 5]))
    vocab_size = int(model_cfg.get("vocab_size", math.prod(levels)))
    world_model = WorldModel(
        vocab_size=vocab_size,
        n_actions=n_actions,
        embed_dim=int(model_cfg.get("embed_dim", 384)),
        n_heads=int(model_cfg.get("n_heads", 8)),
        n_layers=int(model_cfg.get("n_layers", 8)),
        context_frames=k,
        dropout=float(model_cfg.get("dropout", 0.1)),
        tokens_per_frame=int(model_cfg.get("tokens_per_frame", fsq.latent_grid ** 2)),
        adaln=bool(model_cfg.get("adaln", False)),
        use_status_token=False,
        use_cpc=False,
    ).to(device)
    predictor = AtariPredictorWithHeads(
        world_model, hidden_dim=int(model_cfg.get("embed_dim", 384))).to(device)
    print(f"Predictor parameters: {sum(p.numel() for p in predictor.parameters()):,}")

    full_vocab_size = vocab_size
    soft_target_matrix = None
    if args.label_smoothing > 0:
        soft_target_matrix = build_structured_smooth_targets(
            levels, full_vocab_size, sigma=args.fsq_sigma,
            smoothing=args.label_smoothing, kernel=args.fsq_kernel,
        ).to(device)
        print(f"SLS enabled: kernel={args.fsq_kernel} sigma={args.fsq_sigma} "
              f"epsilon={args.label_smoothing}")
    else:
        print("CE baseline: no SLS target smoothing")

    optimizer = torch.optim.AdamW(
        predictor.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=args.epochs, eta_min=args.lr_min)
    scaler = torch.amp.GradScaler("cuda", enabled=(amp_dtype == torch.float16 and device.type == "cuda"))
    if scaler.is_enabled():
        print("GradScaler enabled for float16 AMP")
    coords = _fsq_coords(levels, device)

    ckpt_dir = Path(args.checkpoint_dir)
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    latest_path = ckpt_dir / "predictor_latest.pt"
    start_epoch = 1
    best_rollout = float("inf")
    if args.init_from:
        load_predictor_weights(predictor, args.init_from, device)
        print(f"Warm-started predictor weights from {args.init_from}; "
              "reset optimizer/scheduler/epoch/best metric")
    elif args.resume and latest_path.exists():
        resume_state = torch.load(latest_path, map_location=device, weights_only=False)
        model_state = resume_state.get("model", resume_state)
        model_state = {k.removeprefix("_orig_mod."): v for k, v in model_state.items()}
        if "world_model.head.weight" in model_state:
            predictor.load_state_dict(model_state)
        else:
            wm_state, aux_state = split_atari_predictor_state(model_state)
            predictor.world_model.load_state_dict(wm_state)
            predictor.load_state_dict(aux_state, strict=False)
        if "optimizer" in resume_state:
            try:
                optimizer.load_state_dict(resume_state["optimizer"])
            except ValueError as exc:
                print(f"Skipping optimizer resume after predictor head change: {exc}")
        if "scheduler" in resume_state:
            scheduler.load_state_dict(resume_state["scheduler"])
        if "scaler" in resume_state and scaler.is_enabled():
            scaler.load_state_dict(resume_state["scaler"])
        start_epoch = int(resume_state.get("epoch", 0)) + 1
        best_rollout = float(resume_state.get("best_rollout", best_rollout))
        print(f"Resumed predictor from epoch {start_epoch - 1} "
              f"(best rollout={best_rollout:.6f})")
    elif args.resume:
        print(f"--resume requested but {latest_path} does not exist; starting fresh")

    if args.compile_mode != "none":
        predictor = torch.compile(predictor, mode=args.compile_mode)
        print(f"torch.compile enabled (mode={args.compile_mode})")

    log_mode = "a" if args.resume and start_epoch > 1 and (ckpt_dir / "predictor_log.csv").exists() else "w"
    log_file = open(ckpt_dir / "predictor_log.csv", log_mode, newline="")
    log = csv.writer(log_file)
    if log_mode == "w":
        log.writerow(["epoch", "train_nll", "train_reward_loss", "train_done_loss",
                      "val_nll", "val_acc", "val_fsq_l1_dist",
                      "val_reward_mae", "val_done_acc",
                      *[f"rollout_{h}_l1" for h in horizons], "lr", "time_s"])

    max_steps = int(args.steps_per_epoch or 0)
    for epoch in range(start_epoch, args.epochs + 1):
        t0 = time.time()
        predictor.train()
        total_loss = total_tokens = total_reward_loss = total_done_loss = total_aux = 0
        for step, (frame_tokens, actions, rewards, dones) in enumerate(train_loader, start=1):
            frame_tokens = frame_tokens.to(device)
            actions = actions.to(device)
            rewards = rewards.to(device)
            dones = dones.to(device)
            with torch.amp.autocast("cuda", enabled=amp_dtype is not None, dtype=amp_dtype):
                logits, pred_reward, done_logit = predictor(
                    frame_tokens, actions, return_aux=True)
                token_loss = compute_loss(
                    logits, frame_tokens[:, -1], vocab_size,
                    soft_target_matrix, args.label_smoothing, args.focal_gamma)
                reward_loss = F.mse_loss(pred_reward.float(), rewards)
                done_loss = F.binary_cross_entropy_with_logits(done_logit.float(), dones)
                loss = token_loss + float(args.reward_loss_weight) * reward_loss + \
                    float(args.done_loss_weight) * done_loss
            optimizer.zero_grad(set_to_none=True)
            if scaler.is_enabled():
                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()
            else:
                loss.backward()
                optimizer.step()
            total_loss += float(token_loss.item()) * frame_tokens[:, -1].numel()
            total_tokens += frame_tokens[:, -1].numel()
            total_reward_loss += float(reward_loss.item()) * rewards.numel()
            total_done_loss += float(done_loss.item()) * dones.numel()
            total_aux += rewards.numel()
            if max_steps and step >= max_steps:
                break
        scheduler.step()

        do_val = (epoch % int(args.val_interval) == 0) or epoch == args.epochs
        rollout_metrics = {f"rollout_{h}_l1": float("nan") for h in horizons}
        if do_val:
            val_metrics = eval_next_step(
                predictor, val_loader, device, vocab_size, soft_target_matrix,
                args.label_smoothing, args.focal_gamma, coords, amp_dtype,
                max_batches=args.max_val_batches)
            rollout_metrics = eval_rollouts(
                predictor, fsq, rollout_loader, horizons, device, vocab_size,
                amp_dtype, max_batches=args.max_rollout_batches)
            headline = rollout_metrics[f"rollout_{max_horizon}_l1"]
            if headline < best_rollout:
                best_rollout = headline
                torch.save(clean_state_dict(predictor), ckpt_dir / "predictor_best.pt")
                torch.save(predictor_state_for_world_model(predictor),
                           ckpt_dir / "predictor_best_world_model.pt")
        else:
            val_metrics = {
                "nll": float("nan"), "acc": float("nan"),
                "fsq_l1_dist": float("nan"),
                "reward_mae": float("nan"), "done_acc": float("nan"),
            }

        dt = time.time() - t0
        train_nll = total_loss / max(total_tokens, 1)
        train_reward_loss = total_reward_loss / max(total_aux, 1)
        train_done_loss = total_done_loss / max(total_aux, 1)
        lr = optimizer.param_groups[0]["lr"]
        print(
            f"Epoch {epoch:3d}/{args.epochs} ({dt:.1f}s) | "
            f"train_nll={train_nll:.4f} rloss={train_reward_loss:.4f} "
            f"dloss={train_done_loss:.4f} | val_nll={val_metrics['nll']:.4f} "
            f"acc={100*val_metrics['acc']:.2f}% fsq_l1={val_metrics['fsq_l1_dist']:.3f} "
            f"rmae={val_metrics['reward_mae']:.3f} done={100*val_metrics['done_acc']:.1f}% | "
            + " ".join(f"r{h}={rollout_metrics[f'rollout_{h}_l1']:.4f}" for h in horizons)
            + f" | LR={lr:.1e}"
        )
        log.writerow([
            epoch, f"{train_nll:.6f}", f"{train_reward_loss:.6f}",
            f"{train_done_loss:.6f}", f"{val_metrics['nll']:.6f}",
            f"{val_metrics['acc']:.6f}", f"{val_metrics['fsq_l1_dist']:.6f}",
            f"{val_metrics['reward_mae']:.6f}", f"{val_metrics['done_acc']:.6f}",
            *[f"{rollout_metrics[f'rollout_{h}_l1']:.6f}" for h in horizons],
            f"{lr:.1e}", f"{dt:.1f}",
        ])
        log_file.flush()
        latest_payload = {
            "epoch": epoch,
            "model": clean_state_dict(predictor),
            "optimizer": optimizer.state_dict(),
            "scheduler": scheduler.state_dict(),
            "best_rollout": best_rollout,
        }
        if scaler.is_enabled():
            latest_payload["scaler"] = scaler.state_dict()
        torch.save(latest_payload, latest_path)

    torch.save(clean_state_dict(predictor), ckpt_dir / "predictor_final.pt")
    torch.save(predictor_state_for_world_model(predictor),
               ckpt_dir / "predictor_final_world_model.pt")
    log_file.close()
    print(f"Training complete. Best rollout_{max_horizon}_l1={best_rollout:.6f}")
    print(f"Checkpoints saved to {ckpt_dir}")


if __name__ == "__main__":
    main()

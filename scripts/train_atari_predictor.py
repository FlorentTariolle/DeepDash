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
from torch.utils.data import DataLoader, Dataset, Sampler, WeightedRandomSampler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atari.predictor import AtariPredictorWithHeads, split_atari_predictor_state
from atari.rl_targets import twohot_cross_entropy, twohot_symlog_targets
from atari.replay_buffer import load_metadata, load_replay_arrays
from deepdash.config import apply_config, load_config
from deepdash.fsq import FSQVAE
from deepdash.wandb_utils import wandb_finish, wandb_init, wandb_log
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


def predictor_lr_lambda(epoch_idx: int, total_epochs: int, warmup_epochs: int,
                        min_lr_ratio: float) -> float:
    """Linear warmup followed by cosine decay.

    ``epoch_idx`` is LambdaLR's zero-based scheduler step index. LambdaLR sets
    the epoch-1 LR at construction, then the loop advances it after each epoch.
    """
    total_epochs = max(1, int(total_epochs))
    warmup_epochs = max(0, min(int(warmup_epochs), total_epochs))
    min_lr_ratio = float(min_lr_ratio)

    if warmup_epochs > 0 and epoch_idx < warmup_epochs:
        return max(min_lr_ratio, float(epoch_idx + 1) / float(warmup_epochs))

    decay_epochs = max(1, total_epochs - warmup_epochs)
    progress = min(1.0, max(0.0, float(epoch_idx - warmup_epochs + 1) / decay_epochs))
    cosine = 0.5 * (1.0 + math.cos(math.pi * progress))
    return min_lr_ratio + (1.0 - min_lr_ratio) * cosine


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


class AtariRolloutConsistencyDataset(Dataset):
    def __init__(self, starts: np.ndarray, tokens: torch.Tensor,
                 actions: np.ndarray, rewards: np.ndarray, dones: np.ndarray,
                 context_frames: int, horizon: int):
        self.starts = np.asarray(starts, dtype=np.int64)
        self.tokens = tokens
        self.actions = torch.from_numpy(actions.astype(np.int64))
        self.rewards = torch.from_numpy(rewards.astype(np.float32))
        self.dones = torch.from_numpy(dones.astype(np.float32))
        self.context_frames = int(context_frames)
        self.horizon = int(horizon)

    def __len__(self):
        return len(self.starts)

    def __getitem__(self, idx):
        i = int(self.starts[idx])
        k = self.context_frames
        h = self.horizon
        reward_start = i + k - 1
        return (
            self.tokens[i:i + k],
            self.actions[i:i + k + h],
            self.tokens[i + k:i + k + h],
            self.rewards[reward_start:reward_start + h],
            self.dones[reward_start:reward_start + h],
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


def reward_balanced_sampler(starts: np.ndarray, rewards: np.ndarray,
                            context_frames: int, zero_weight: float,
                            neg_weight: float, pos_weight: float):
    """Build a replacement sampler that oversamples rare reward events."""
    if len(starts) == 0:
        return None
    target_idx = starts + int(context_frames) - 1
    target_rewards = rewards[target_idx]
    weights = np.full(len(starts), float(zero_weight), dtype=np.float64)
    weights[target_rewards < 0] = float(neg_weight)
    weights[target_rewards > 0] = float(pos_weight)
    counts = {
        "neg": int((target_rewards < 0).sum()),
        "zero": int((target_rewards == 0).sum()),
        "pos": int((target_rewards > 0).sum()),
    }
    print(
        "Reward-balanced sampler: "
        f"counts={counts} weights={{neg:{neg_weight}, zero:{zero_weight}, pos:{pos_weight}}}"
    )
    return WeightedRandomSampler(
        torch.as_tensor(weights, dtype=torch.double),
        num_samples=len(starts),
        replacement=True,
    )


def reward_window_balanced_sampler(starts: np.ndarray, rewards: np.ndarray,
                                   context_frames: int, horizon: int,
                                   min_gap: int, max_gap: int,
                                   zero_weight: float, neg_weight: float,
                                   pos_weight: float):
    """Oversample rollout windows whose future contains a reward event."""
    if len(starts) == 0:
        return None
    k = int(context_frames)
    horizon = int(horizon)
    min_gap = max(0, int(min_gap))
    max_gap = min(horizon - 1, int(max_gap))
    weights = np.full(len(starts), float(zero_weight), dtype=np.float64)
    counts = {"neg": 0, "zero": 0, "pos": 0, "other_event": 0}
    for row, start in enumerate(starts):
        reward_start = int(start) + k - 1
        window = rewards[reward_start:reward_start + horizon]
        events = np.flatnonzero(window != 0)
        if len(events) == 0:
            counts["zero"] += 1
            continue
        event_gap = int(events[0])
        event_sign = float(np.sign(window[event_gap]))
        if min_gap <= event_gap <= max_gap:
            if event_sign < 0:
                weights[row] = float(neg_weight)
                counts["neg"] += 1
            else:
                weights[row] = float(pos_weight)
                counts["pos"] += 1
        else:
            counts["other_event"] += 1
    print(
        "Reward-window rollout sampler: "
        f"counts={counts} gap=[{min_gap},{max_gap}] "
        f"weights={{neg:{neg_weight}, zero:{zero_weight}, pos:{pos_weight}}}"
    )
    return WeightedRandomSampler(
        torch.as_tensor(weights, dtype=torch.double),
        num_samples=len(starts),
        replacement=True,
    )


def _reward_window_event_labels(starts: np.ndarray, rewards: np.ndarray,
                                context_frames: int, horizon: int,
                                min_gap: int, max_gap: int) -> tuple[np.ndarray, dict[str, int]]:
    k = int(context_frames)
    horizon = int(horizon)
    min_gap = max(0, int(min_gap))
    max_gap = min(horizon - 1, int(max_gap))
    labels = np.zeros(len(starts), dtype=np.int8)
    counts = {"neg": 0, "zero": 0, "pos": 0, "other_event": 0}
    for row, start in enumerate(starts):
        reward_start = int(start) + k - 1
        window = rewards[reward_start:reward_start + horizon]
        events = np.flatnonzero(window != 0)
        if len(events) == 0:
            counts["zero"] += 1
            continue
        event_gap = int(events[0])
        event_sign = float(np.sign(window[event_gap]))
        if min_gap <= event_gap <= max_gap:
            if event_sign < 0:
                labels[row] = -1
                counts["neg"] += 1
            else:
                labels[row] = 1
                counts["pos"] += 1
        else:
            counts["other_event"] += 1
    return labels, counts


class RewardWindowQuotaBatchSampler(Sampler[list[int]]):
    """Build rollout batches with fixed positive/negative reward-event quotas."""

    def __init__(self, starts: np.ndarray, rewards: np.ndarray,
                 context_frames: int, horizon: int, min_gap: int, max_gap: int,
                 batch_size: int, event_fraction: float, pos_fraction: float,
                 num_batches: int | None = None, seed: int = 42):
        self.starts = np.asarray(starts, dtype=np.int64)
        self.batch_size = max(1, int(batch_size))
        self.event_fraction = min(1.0, max(0.0, float(event_fraction)))
        self.pos_fraction = min(1.0, max(0.0, float(pos_fraction)))
        self.num_batches = (
            max(1, int(num_batches))
            if num_batches is not None and int(num_batches) > 0
            else max(1, math.ceil(len(self.starts) / self.batch_size))
        )
        self.seed = int(seed)
        self.epoch = 0
        labels, counts = _reward_window_event_labels(
            self.starts, rewards, context_frames, horizon, min_gap, max_gap)
        self.pos_indices = np.flatnonzero(labels > 0).astype(np.int64)
        self.neg_indices = np.flatnonzero(labels < 0).astype(np.int64)
        self.zero_indices = np.flatnonzero(labels == 0).astype(np.int64)
        self.all_indices = np.arange(len(self.starts), dtype=np.int64)
        print(
            "Reward-window quota batch sampler: "
            f"counts={counts} gap=[{max(0, int(min_gap))},{min(int(horizon) - 1, int(max_gap))}] "
            f"batch={self.batch_size} event_fraction={self.event_fraction:.2f} "
            f"pos_fraction={self.pos_fraction:.2f} batches={self.num_batches}"
        )

    def __len__(self):
        return self.num_batches

    @staticmethod
    def _draw(rng: np.random.Generator, pool: np.ndarray, n: int) -> list[int]:
        if n <= 0 or len(pool) == 0:
            return []
        replace = len(pool) < n
        return rng.choice(pool, size=n, replace=replace).astype(np.int64).tolist()

    def __iter__(self):
        rng = np.random.default_rng(self.seed + self.epoch)
        self.epoch += 1
        for _ in range(self.num_batches):
            n_event = int(round(self.batch_size * self.event_fraction))
            n_pos = int(round(n_event * self.pos_fraction))
            n_neg = n_event - n_pos
            n_zero = self.batch_size - n_event

            pos_pool = self.pos_indices if len(self.pos_indices) else self.neg_indices
            neg_pool = self.neg_indices if len(self.neg_indices) else self.pos_indices
            zero_pool = self.zero_indices if len(self.zero_indices) else self.all_indices
            if len(pos_pool) == 0:
                pos_pool = self.all_indices
            if len(neg_pool) == 0:
                neg_pool = self.all_indices

            batch = []
            batch.extend(self._draw(rng, pos_pool, n_pos))
            batch.extend(self._draw(rng, neg_pool, n_neg))
            batch.extend(self._draw(rng, zero_pool, n_zero))
            if len(batch) < self.batch_size:
                batch.extend(self._draw(rng, self.all_indices, self.batch_size - len(batch)))
            rng.shuffle(batch)
            yield batch


def reward_event_targets(rewards: torch.Tensor) -> torch.Tensor:
    """Class indices for Pong-style sparse rewards: 0=-1, 1=0, 2=+1."""
    return torch.where(
        rewards < 0,
        torch.zeros_like(rewards, dtype=torch.long),
        torch.where(
            rewards > 0,
            torch.full_like(rewards, 2, dtype=torch.long),
            torch.ones_like(rewards, dtype=torch.long),
        ),
    )


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


def compute_rollout_consistency_loss(model, ctx_tokens, actions, target_tokens,
                                     rewards, dones, vocab_size,
                                     soft_target_matrix, label_smoothing,
                                     focal_gamma, args, event_weights):
    pred_context = ctx_tokens
    k = int(model.context_frames)
    horizon = int(target_tokens.size(1))
    total_token = rewards.new_zeros(())
    total_reward = rewards.new_zeros(())
    total_event = rewards.new_zeros(())
    total_done = rewards.new_zeros(())
    for step in range(horizon):
        act_window = actions[:, step:step + k]
        if args.reward_head_type == "twohot":
            outputs = model(
                pred_context, act_window, return_aux=True,
                return_reward_logits=True,
                return_event_logits=args.reward_event_head)
            logits, pred_reward, done_logit, reward_logits = outputs[:4]
            event_logits = outputs[4] if args.reward_event_head else None
            reward_targets = twohot_symlog_targets(
                rewards[:, step], args.reward_twohot_bins,
                args.reward_twohot_low, args.reward_twohot_high)
            reward_loss = twohot_cross_entropy(reward_logits, reward_targets)
        else:
            outputs = model(
                pred_context, act_window, return_aux=True,
                return_event_logits=args.reward_event_head)
            logits, pred_reward, done_logit = outputs[:3]
            event_logits = outputs[3] if args.reward_event_head else None
            reward_loss = F.mse_loss(pred_reward.float(), rewards[:, step])
        token_loss = compute_loss(
            logits, target_tokens[:, step], vocab_size,
            soft_target_matrix, label_smoothing, focal_gamma)
        if event_logits is not None:
            event_loss = F.cross_entropy(
                event_logits.float(), reward_event_targets(rewards[:, step]),
                weight=event_weights)
        else:
            event_loss = rewards.new_zeros(())
        done_loss = F.binary_cross_entropy_with_logits(
            done_logit.float(), dones[:, step])
        total_token = total_token + token_loss
        total_reward = total_reward + reward_loss
        total_event = total_event + event_loss
        total_done = total_done + done_loss
        pred_next = logits[:, :, :vocab_size].argmax(dim=-1).detach()
        pred_context = torch.cat(
            [pred_context[:, 1:], pred_next.unsqueeze(1)], dim=1)
    inv_horizon = 1.0 / max(horizon, 1)
    total_token = total_token * inv_horizon
    total_reward = total_reward * inv_horizon
    total_event = total_event * inv_horizon
    total_done = total_done * inv_horizon
    loss = (
        float(args.rollout_consistency_token_loss_weight) * total_token
        + float(args.rollout_consistency_reward_loss_weight) * total_reward
        + float(args.rollout_consistency_event_loss_weight) * total_event
        + float(args.rollout_consistency_done_loss_weight) * total_done
    )
    return loss, {
        "token": total_token.detach(),
        "reward": total_reward.detach(),
        "event": total_event.detach(),
        "done": total_done.detach(),
    }


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
        load_matching_state_dict(predictor, state)
    else:
        wm_state, aux_state = split_atari_predictor_state(state)
        predictor.world_model.load_state_dict(wm_state)
        load_matching_state_dict(predictor, aux_state, strict=False)


def load_matching_state_dict(module: nn.Module, state: dict, strict: bool = False):
    """Load matching tensors and skip resized heads for scalar/two-hot upgrades."""
    own = module.state_dict()
    matched = {
        k: v for k, v in state.items()
        if k in own and tuple(own[k].shape) == tuple(v.shape)
    }
    skipped = sorted(k for k, v in state.items()
                     if k in own and tuple(own[k].shape) != tuple(v.shape))
    result = module.load_state_dict(matched, strict=strict)
    if skipped:
        print(f"Skipped resized predictor tensors: {skipped}")
    return result


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
    parser.add_argument("--warmup-epochs", type=int, default=None)
    parser.add_argument("--weight-decay", type=float, default=None)
    parser.add_argument("--label-smoothing", type=float, default=None,
                        help="0 = CE; >0 = SLS with fsq_kernel/fsq_sigma.")
    parser.add_argument("--fsq-kernel", default=None)
    parser.add_argument("--fsq-sigma", type=float, default=None)
    parser.add_argument("--focal-gamma", type=float, default=None)
    parser.add_argument("--reward-loss-weight", type=float, default=None)
    parser.add_argument("--done-loss-weight", type=float, default=None)
    parser.add_argument("--reward-head-type", choices=["scalar", "twohot"], default=None)
    parser.add_argument("--reward-twohot-bins", type=int, default=None)
    parser.add_argument("--reward-twohot-low", type=float, default=None)
    parser.add_argument("--reward-twohot-high", type=float, default=None)
    parser.add_argument("--reward-event-head", action=argparse.BooleanOptionalAction,
                        default=None)
    parser.add_argument("--reward-event-loss-weight", type=float, default=None)
    parser.add_argument("--reward-event-zero-weight", type=float, default=None)
    parser.add_argument("--reward-event-neg-weight", type=float, default=None)
    parser.add_argument("--reward-event-pos-weight", type=float, default=None)
    parser.add_argument("--reward-balanced-sampler", action="store_true",
                        help="Oversample windows by target reward sign.")
    parser.add_argument("--reward-sample-zero-weight", type=float, default=None)
    parser.add_argument("--reward-sample-neg-weight", type=float, default=None)
    parser.add_argument("--reward-sample-pos-weight", type=float, default=None)
    parser.add_argument("--rollout-consistency-loss-weight", type=float, default=None)
    parser.add_argument("--rollout-consistency-token-loss-weight", type=float, default=None)
    parser.add_argument("--rollout-consistency-reward-loss-weight", type=float, default=None)
    parser.add_argument("--rollout-consistency-event-loss-weight", type=float, default=None)
    parser.add_argument("--rollout-consistency-done-loss-weight", type=float, default=None)
    parser.add_argument("--rollout-consistency-batch-size", type=int, default=None)
    parser.add_argument("--rollout-consistency-horizon", type=int, default=None)
    parser.add_argument("--rollout-consistency-min-gap", type=int, default=None)
    parser.add_argument("--rollout-consistency-max-gap", type=int, default=None)
    parser.add_argument("--rollout-consistency-zero-weight", type=float, default=None)
    parser.add_argument("--rollout-consistency-neg-weight", type=float, default=None)
    parser.add_argument("--rollout-consistency-pos-weight", type=float, default=None)
    parser.add_argument("--rollout-consistency-event-quota-sampler",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="Build rollout-consistency batches with fixed near-reward quotas.")
    parser.add_argument("--rollout-consistency-event-fraction", type=float, default=None,
                        help="Fraction of each rollout-consistency batch drawn near reward events.")
    parser.add_argument("--rollout-consistency-pos-fraction", type=float, default=None,
                        help="Positive-event fraction inside the reward-event quota.")
    parser.add_argument("--rollout-consistency-batches-per-epoch", type=int, default=None,
                        help="Override number of rollout-consistency quota batches per epoch.")
    parser.add_argument("--rollout-consistency-event-zero-weight", type=float, default=None)
    parser.add_argument("--rollout-consistency-event-neg-weight", type=float, default=None)
    parser.add_argument("--rollout-consistency-event-pos-weight", type=float, default=None)
    parser.add_argument("--use-cpc", action=argparse.BooleanOptionalAction,
                        default=None)
    parser.add_argument("--cpc-weight", type=float, default=None)
    parser.add_argument("--cpc-dim", type=int, default=None)
    parser.add_argument("--compile-mode", choices=["none", "default", "reduce-overhead"], default=None)
    parser.add_argument("--amp-dtype", choices=["none", "float16", "bfloat16"], default=None)
    parser.add_argument("--val-interval", type=int, default=None)
    parser.add_argument("--wandb-project", default=None)
    parser.add_argument("--wandb-name", default=None)
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
    args.warmup_epochs = args.warmup_epochs if args.warmup_epochs is not None else 0
    args.weight_decay = args.weight_decay if args.weight_decay is not None else 0.01
    args.label_smoothing = args.label_smoothing if args.label_smoothing is not None else 0.0
    args.fsq_kernel = args.fsq_kernel or "gaussian"
    args.fsq_sigma = args.fsq_sigma if args.fsq_sigma is not None else 0.9
    args.focal_gamma = args.focal_gamma if args.focal_gamma is not None else 0.0
    args.reward_loss_weight = args.reward_loss_weight if args.reward_loss_weight is not None else 1.0
    args.done_loss_weight = args.done_loss_weight if args.done_loss_weight is not None else 1.0
    args.reward_head_type = args.reward_head_type or "scalar"
    args.reward_twohot_bins = args.reward_twohot_bins or 255
    args.reward_twohot_low = args.reward_twohot_low if args.reward_twohot_low is not None else -25.0
    args.reward_twohot_high = args.reward_twohot_high if args.reward_twohot_high is not None else 25.0
    args.reward_event_head = bool(args.reward_event_head) if args.reward_event_head is not None else False
    args.reward_event_loss_weight = (
        args.reward_event_loss_weight if args.reward_event_loss_weight is not None else 0.0)
    args.reward_event_zero_weight = (
        args.reward_event_zero_weight if args.reward_event_zero_weight is not None else 1.0)
    args.reward_event_neg_weight = (
        args.reward_event_neg_weight if args.reward_event_neg_weight is not None else 10.0)
    args.reward_event_pos_weight = (
        args.reward_event_pos_weight if args.reward_event_pos_weight is not None else 100.0)
    args.reward_sample_zero_weight = (
        args.reward_sample_zero_weight if args.reward_sample_zero_weight is not None else 1.0)
    args.reward_sample_neg_weight = (
        args.reward_sample_neg_weight if args.reward_sample_neg_weight is not None else 10.0)
    args.reward_sample_pos_weight = (
        args.reward_sample_pos_weight if args.reward_sample_pos_weight is not None else 500.0)
    args.rollout_consistency_loss_weight = (
        args.rollout_consistency_loss_weight
        if args.rollout_consistency_loss_weight is not None else 0.0)
    args.rollout_consistency_token_loss_weight = (
        args.rollout_consistency_token_loss_weight
        if args.rollout_consistency_token_loss_weight is not None else 1.0)
    args.rollout_consistency_reward_loss_weight = (
        args.rollout_consistency_reward_loss_weight
        if args.rollout_consistency_reward_loss_weight is not None else 1.0)
    args.rollout_consistency_event_loss_weight = (
        args.rollout_consistency_event_loss_weight
        if args.rollout_consistency_event_loss_weight is not None else 1.0)
    args.rollout_consistency_done_loss_weight = (
        args.rollout_consistency_done_loss_weight
        if args.rollout_consistency_done_loss_weight is not None else 0.25)
    args.rollout_consistency_horizon = (
        args.rollout_consistency_horizon if args.rollout_consistency_horizon is not None else 15)
    args.rollout_consistency_batch_size = (
        args.rollout_consistency_batch_size
        if args.rollout_consistency_batch_size is not None else max(1, min(args.batch_size, 32)))
    args.rollout_consistency_min_gap = (
        args.rollout_consistency_min_gap if args.rollout_consistency_min_gap is not None else 3)
    args.rollout_consistency_max_gap = (
        args.rollout_consistency_max_gap if args.rollout_consistency_max_gap is not None else 13)
    args.rollout_consistency_zero_weight = (
        args.rollout_consistency_zero_weight
        if args.rollout_consistency_zero_weight is not None else 1.0)
    args.rollout_consistency_neg_weight = (
        args.rollout_consistency_neg_weight
        if args.rollout_consistency_neg_weight is not None else 20.0)
    args.rollout_consistency_pos_weight = (
        args.rollout_consistency_pos_weight
        if args.rollout_consistency_pos_weight is not None else 200.0)
    args.rollout_consistency_event_quota_sampler = (
        bool(args.rollout_consistency_event_quota_sampler)
        if args.rollout_consistency_event_quota_sampler is not None else False)
    args.rollout_consistency_event_fraction = (
        args.rollout_consistency_event_fraction
        if args.rollout_consistency_event_fraction is not None else 0.9)
    args.rollout_consistency_pos_fraction = (
        args.rollout_consistency_pos_fraction
        if args.rollout_consistency_pos_fraction is not None else 0.5)
    args.rollout_consistency_event_zero_weight = (
        args.rollout_consistency_event_zero_weight
        if args.rollout_consistency_event_zero_weight is not None
        else args.reward_event_zero_weight)
    args.rollout_consistency_event_neg_weight = (
        args.rollout_consistency_event_neg_weight
        if args.rollout_consistency_event_neg_weight is not None
        else args.reward_event_neg_weight)
    args.rollout_consistency_event_pos_weight = (
        args.rollout_consistency_event_pos_weight
        if args.rollout_consistency_event_pos_weight is not None
        else args.reward_event_pos_weight)
    args.use_cpc = bool(args.use_cpc) if args.use_cpc is not None else False
    args.cpc_weight = args.cpc_weight if args.cpc_weight is not None else 0.0
    args.cpc_dim = args.cpc_dim if args.cpc_dim is not None else 64
    args.compile_mode = args.compile_mode or "reduce-overhead"
    args.amp_dtype = args.amp_dtype or "float16"
    args.val_interval = args.val_interval if args.val_interval is not None else 1
    args.wandb_project = args.wandb_project or "sls-wm-atari"
    args.wandb_name = args.wandb_name or f"predictor-{args.config_section}-{Path(args.checkpoint_dir).name}"
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
    max_horizon = max(max(horizons), int(args.rollout_consistency_horizon))

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
    eval_max_horizon = max(horizons)
    rollout_ds = AtariRolloutDataset(
        rollout_val, tokens, replay.actions, replay.obs, k, eval_max_horizon)
    consistency_loader = None
    if args.rollout_consistency_loss_weight > 0:
        consistency_ds = AtariRolloutConsistencyDataset(
            rollout_train, tokens, replay.actions, replay.rewards, replay.dones,
            k, int(args.rollout_consistency_horizon))
        if args.rollout_consistency_event_quota_sampler:
            consistency_batch_sampler = RewardWindowQuotaBatchSampler(
                rollout_train, replay.rewards, k, int(args.rollout_consistency_horizon),
                args.rollout_consistency_min_gap,
                args.rollout_consistency_max_gap,
                int(args.rollout_consistency_batch_size),
                args.rollout_consistency_event_fraction,
                args.rollout_consistency_pos_fraction,
                args.rollout_consistency_batches_per_epoch,
                args.seed,
            )
            consistency_loader = DataLoader(
                consistency_ds,
                batch_sampler=consistency_batch_sampler,
                num_workers=0,
            )
        else:
            consistency_sampler = reward_window_balanced_sampler(
                rollout_train, replay.rewards, k, int(args.rollout_consistency_horizon),
                args.rollout_consistency_min_gap,
                args.rollout_consistency_max_gap,
                args.rollout_consistency_zero_weight,
                args.rollout_consistency_neg_weight,
                args.rollout_consistency_pos_weight,
            )
            consistency_loader = DataLoader(
                consistency_ds,
                batch_size=int(args.rollout_consistency_batch_size),
                shuffle=consistency_sampler is None,
                sampler=consistency_sampler,
                num_workers=0,
            )
        print(
            "Rollout consistency enabled: "
            f"horizon={args.rollout_consistency_horizon} "
            f"batch={args.rollout_consistency_batch_size} "
            f"weight={args.rollout_consistency_loss_weight} "
            f"quota_sampler={args.rollout_consistency_event_quota_sampler}"
        )
    train_sampler = None
    if args.reward_balanced_sampler:
        train_sampler = reward_balanced_sampler(
            train_starts, replay.rewards, k,
            args.reward_sample_zero_weight,
            args.reward_sample_neg_weight,
            args.reward_sample_pos_weight,
        )
    train_loader = DataLoader(
        train_ds, batch_size=args.batch_size,
        shuffle=train_sampler is None, sampler=train_sampler, num_workers=0)
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
        use_cpc=bool(args.use_cpc),
        cpc_dim=int(args.cpc_dim),
    ).to(device)
    predictor = AtariPredictorWithHeads(
        world_model,
        hidden_dim=int(model_cfg.get("embed_dim", 384)),
        reward_head_type=args.reward_head_type,
        reward_bins=args.reward_twohot_bins,
        reward_low=args.reward_twohot_low,
        reward_high=args.reward_twohot_high,
        reward_event_head=args.reward_event_head,
    ).to(device)
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
    if args.use_cpc and args.cpc_weight > 0:
        print(f"AC-CPC enabled: weight={args.cpc_weight} dim={args.cpc_dim}")
    elif args.use_cpc:
        print("AC-CPC module enabled but cpc_weight=0; loss is inactive")

    optimizer = torch.optim.AdamW(
        predictor.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    min_lr_ratio = float(args.lr_min) / float(args.lr) if args.lr > 0 else 0.0
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer,
        lr_lambda=lambda epoch_idx: predictor_lr_lambda(
            epoch_idx, args.epochs, args.warmup_epochs, min_lr_ratio),
    )
    if args.warmup_epochs > 0:
        print(f"LR schedule: linear warmup {args.warmup_epochs} epoch(s), "
              f"then cosine decay to {args.lr_min:.1e}")
    else:
        print(f"LR schedule: cosine decay to {args.lr_min:.1e}")
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
            load_matching_state_dict(predictor, model_state, strict=False)
        else:
            wm_state, aux_state = split_atari_predictor_state(model_state)
            predictor.world_model.load_state_dict(wm_state)
            load_matching_state_dict(predictor, aux_state, strict=False)
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

    rollout_consistency_model = predictor
    if args.compile_mode != "none":
        predictor = torch.compile(predictor, mode=args.compile_mode)
        print(f"torch.compile enabled (mode={args.compile_mode})")

    log_mode = "a" if args.resume and start_epoch > 1 and (ckpt_dir / "predictor_log.csv").exists() else "w"
    log_file = open(ckpt_dir / "predictor_log.csv", log_mode, newline="")
    log = csv.writer(log_file)
    if log_mode == "w":
        log.writerow(["epoch", "train_nll", "train_reward_loss",
                      "train_reward_event_loss", "train_done_loss",
                      "train_cpc_loss",
                      "train_rollout_consistency_loss",
                      "train_rollout_consistency_token_loss",
                      "train_rollout_consistency_reward_loss",
                      "train_rollout_consistency_event_loss",
                      "train_rollout_consistency_done_loss",
                      "val_nll", "val_acc", "val_fsq_l1_dist",
                      "val_reward_mae", "val_done_acc",
                      *[f"rollout_{h}_l1" for h in horizons], "lr", "time_s"])
    wandb_init(
        project=args.wandb_project,
        name=args.wandb_name,
        config={**vars(args), "replay_steps": len(replay.obs), "n_actions": n_actions},
    )

    max_steps = int(args.steps_per_epoch or 0)
    try:
      for epoch in range(start_epoch, args.epochs + 1):
        t0 = time.time()
        predictor.train()
        total_loss = total_tokens = total_reward_loss = 0
        total_reward_event_loss = total_done_loss = total_aux = 0
        total_cpc_loss = total_cpc_batches = 0
        total_consistency_loss = total_consistency_batches = 0
        total_consistency_token_loss = total_consistency_reward_loss = 0
        total_consistency_event_loss = total_consistency_done_loss = 0
        event_weights = torch.tensor(
            [args.reward_event_neg_weight, args.reward_event_zero_weight,
             args.reward_event_pos_weight],
            dtype=torch.float32,
            device=device,
        )
        consistency_event_weights = torch.tensor(
            [args.rollout_consistency_event_neg_weight,
             args.rollout_consistency_event_zero_weight,
             args.rollout_consistency_event_pos_weight],
            dtype=torch.float32,
            device=device,
        )
        consistency_iter = iter(consistency_loader) if consistency_loader is not None else None
        for step, (frame_tokens, actions, rewards, dones) in enumerate(train_loader, start=1):
            frame_tokens = frame_tokens.to(device)
            actions = actions.to(device)
            rewards = rewards.to(device)
            dones = dones.to(device)
            with torch.amp.autocast("cuda", enabled=amp_dtype is not None, dtype=amp_dtype):
                if args.reward_head_type == "twohot":
                    outputs = predictor(
                        frame_tokens, actions, return_aux=True,
                        return_reward_logits=True,
                        return_event_logits=args.reward_event_head,
                        return_cpc_loss=bool(args.use_cpc and args.cpc_weight > 0))
                    logits, pred_reward, done_logit = outputs[:3]
                    out_idx = 3
                    cpc_loss = (
                        outputs[out_idx]
                        if args.use_cpc and args.cpc_weight > 0
                        else rewards.new_zeros(())
                    )
                    out_idx += int(bool(args.use_cpc and args.cpc_weight > 0))
                    reward_logits = outputs[out_idx]
                    out_idx += 1
                    event_logits = outputs[out_idx] if args.reward_event_head else None
                    reward_targets = twohot_symlog_targets(
                        rewards, args.reward_twohot_bins,
                        args.reward_twohot_low, args.reward_twohot_high)
                    reward_loss = twohot_cross_entropy(reward_logits, reward_targets)
                else:
                    outputs = predictor(
                        frame_tokens, actions, return_aux=True,
                        return_event_logits=args.reward_event_head,
                        return_cpc_loss=bool(args.use_cpc and args.cpc_weight > 0))
                    logits, pred_reward, done_logit = outputs[:3]
                    out_idx = 3
                    cpc_loss = (
                        outputs[out_idx]
                        if args.use_cpc and args.cpc_weight > 0
                        else rewards.new_zeros(())
                    )
                    out_idx += int(bool(args.use_cpc and args.cpc_weight > 0))
                    event_logits = outputs[out_idx] if args.reward_event_head else None
                    reward_loss = F.mse_loss(pred_reward.float(), rewards)
                if event_logits is not None:
                    event_loss = F.cross_entropy(
                        event_logits.float(), reward_event_targets(rewards),
                        weight=event_weights)
                else:
                    event_loss = rewards.new_zeros(())
                token_loss = compute_loss(
                    logits, frame_tokens[:, -1], vocab_size,
                    soft_target_matrix, args.label_smoothing, args.focal_gamma)
                done_loss = F.binary_cross_entropy_with_logits(done_logit.float(), dones)
                loss = token_loss + float(args.reward_loss_weight) * reward_loss + \
                    float(args.reward_event_loss_weight) * event_loss + \
                    float(args.done_loss_weight) * done_loss + \
                    float(args.cpc_weight) * cpc_loss
                consistency_loss = rewards.new_zeros(())
                consistency_parts = None
                if consistency_iter is not None:
                    try:
                        c_batch = next(consistency_iter)
                    except StopIteration:
                        consistency_iter = iter(consistency_loader)
                        c_batch = next(consistency_iter)
                    c_ctx, c_actions, c_targets, c_rewards, c_dones = [
                        item.to(device) for item in c_batch
                    ]
                    consistency_loss, consistency_parts = compute_rollout_consistency_loss(
                        rollout_consistency_model,
                        c_ctx,
                        c_actions,
                        c_targets,
                        c_rewards,
                        c_dones,
                        vocab_size,
                        soft_target_matrix,
                        args.label_smoothing,
                        args.focal_gamma,
                        args,
                        consistency_event_weights,
                    )
                    loss = loss + float(args.rollout_consistency_loss_weight) * consistency_loss
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
            total_reward_event_loss += float(event_loss.item()) * rewards.numel()
            total_done_loss += float(done_loss.item()) * dones.numel()
            total_aux += rewards.numel()
            if args.use_cpc and args.cpc_weight > 0:
                total_cpc_loss += float(cpc_loss.item())
                total_cpc_batches += 1
            if consistency_parts is not None:
                total_consistency_loss += float(consistency_loss.item())
                total_consistency_token_loss += float(consistency_parts["token"].item())
                total_consistency_reward_loss += float(consistency_parts["reward"].item())
                total_consistency_event_loss += float(consistency_parts["event"].item())
                total_consistency_done_loss += float(consistency_parts["done"].item())
                total_consistency_batches += 1
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
            headline = rollout_metrics[f"rollout_{max(horizons)}_l1"]
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
        train_reward_event_loss = total_reward_event_loss / max(total_aux, 1)
        train_done_loss = total_done_loss / max(total_aux, 1)
        train_cpc_loss = total_cpc_loss / max(total_cpc_batches, 1)
        train_consistency_loss = total_consistency_loss / max(total_consistency_batches, 1)
        train_consistency_token_loss = (
            total_consistency_token_loss / max(total_consistency_batches, 1))
        train_consistency_reward_loss = (
            total_consistency_reward_loss / max(total_consistency_batches, 1))
        train_consistency_event_loss = (
            total_consistency_event_loss / max(total_consistency_batches, 1))
        train_consistency_done_loss = (
            total_consistency_done_loss / max(total_consistency_batches, 1))
        lr = optimizer.param_groups[0]["lr"]
        print(
            f"Epoch {epoch:3d}/{args.epochs} ({dt:.1f}s) | "
            f"train_nll={train_nll:.4f} rloss={train_reward_loss:.4f} "
            f"revent={train_reward_event_loss:.4f} "
            f"dloss={train_done_loss:.4f} "
            f"cpc={train_cpc_loss:.4f} "
            f"rcons={train_consistency_loss:.4f} "
            f"rc_tok={train_consistency_token_loss:.4f} "
            f"rc_rew={train_consistency_reward_loss:.4f} "
            f"rc_evt={train_consistency_event_loss:.4f} | "
            f"val_nll={val_metrics['nll']:.4f} "
            f"acc={100*val_metrics['acc']:.2f}% fsq_l1={val_metrics['fsq_l1_dist']:.3f} "
            f"rmae={val_metrics['reward_mae']:.3f} done={100*val_metrics['done_acc']:.1f}% | "
            + " ".join(f"r{h}={rollout_metrics[f'rollout_{h}_l1']:.4f}" for h in horizons)
            + f" | LR={lr:.1e}"
        )
        log.writerow([
            epoch, f"{train_nll:.6f}", f"{train_reward_loss:.6f}",
            f"{train_reward_event_loss:.6f}",
            f"{train_done_loss:.6f}",
            f"{train_cpc_loss:.6f}",
            f"{train_consistency_loss:.6f}",
            f"{train_consistency_token_loss:.6f}",
            f"{train_consistency_reward_loss:.6f}",
            f"{train_consistency_event_loss:.6f}",
            f"{train_consistency_done_loss:.6f}",
            f"{val_metrics['nll']:.6f}",
            f"{val_metrics['acc']:.6f}", f"{val_metrics['fsq_l1_dist']:.6f}",
            f"{val_metrics['reward_mae']:.6f}", f"{val_metrics['done_acc']:.6f}",
            *[f"{rollout_metrics[f'rollout_{h}_l1']:.6f}" for h in horizons],
            f"{lr:.1e}", f"{dt:.1f}",
        ])
        log_file.flush()
        payload = {
            f"{args.config_section}/epoch": epoch,
            f"{args.config_section}/train_nll": train_nll,
            f"{args.config_section}/train_reward_loss": train_reward_loss,
            f"{args.config_section}/train_reward_event_loss": train_reward_event_loss,
            f"{args.config_section}/train_done_loss": train_done_loss,
            f"{args.config_section}/train_cpc_loss": train_cpc_loss,
            f"{args.config_section}/train_rollout_consistency_loss": train_consistency_loss,
            f"{args.config_section}/train_rollout_consistency_token_loss": train_consistency_token_loss,
            f"{args.config_section}/train_rollout_consistency_reward_loss": train_consistency_reward_loss,
            f"{args.config_section}/train_rollout_consistency_event_loss": train_consistency_event_loss,
            f"{args.config_section}/train_rollout_consistency_done_loss": train_consistency_done_loss,
            f"{args.config_section}/lr": lr,
            f"{args.config_section}/time_s": dt,
        }
        if do_val:
            payload.update({
                f"{args.config_section}/val_nll": val_metrics["nll"],
                f"{args.config_section}/val_acc": val_metrics["acc"],
                f"{args.config_section}/val_fsq_l1_dist": val_metrics["fsq_l1_dist"],
                f"{args.config_section}/val_reward_mae": val_metrics["reward_mae"],
                f"{args.config_section}/val_done_acc": val_metrics["done_acc"],
            })
            for h in horizons:
                payload[f"{args.config_section}/rollout_{h}_l1"] = rollout_metrics[f"rollout_{h}_l1"]
        wandb_log(payload)
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
      print(f"Training complete. Best rollout_{max(horizons)}_l1={best_rollout:.6f}")
      print(f"Checkpoints saved to {ckpt_dir}")
    finally:
      log_file.close()
      wandb_finish()


if __name__ == "__main__":
    main()

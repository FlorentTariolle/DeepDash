"""Quantitative held-out diagnostics for the frozen DashVMC world model.

This evaluation does not train or select a model.  It uses the global
episode-level validation split and reports:

1. one-step visual-token likelihood and accuracy;
2. death/status discrimination and calibration;
3. a paired factual-vs-flipped final-action intervention; and
4. autoregressive fidelity under the recorded future action sequence.

The action intervention is paired at the *same recorded context*: only the
last action in the K-action conditioning window is flipped.  Its confidence
interval resamples validation episodes (not individual, correlated windows).

Example
-------
python scripts/eval_world_model_diagnostics.py \
    --config configs/deepdash/v7-phase0.yaml \
    --transformer-checkpoint checkpoints_v7/transformer_best.pt \
    --fsq-checkpoint checkpoints_v7/fsq_best.pt \
    --output-dir analysis/world_model_diagnostics_20260728
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import torch
import torch.nn.functional as F

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deepdash.config import load_config
from deepdash.data_split import get_val_episodes, is_val_episode
from deepdash.fsq import FSQVAE
from deepdash.world_model import WorldModel


SHIFT_RE = re.compile(r"_s[+-]\d+_[+-]\d+$")


@dataclass
class Episode:
    name: str
    source: str
    frames_path: Path
    actions: np.ndarray
    tokens: np.ndarray

    @property
    def length(self) -> int:
        return int(len(self.tokens))


@dataclass(frozen=True)
class WindowRef:
    episode_index: int
    start: int


def _json_value(value: Any) -> Any:
    """Convert NumPy values and non-finite floats into JSON-safe values."""
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if isinstance(value, np.generic):
        return _json_value(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _autocast(device: torch.device, dtype: torch.dtype):
    return torch.autocast(
        device_type=device.type,
        dtype=dtype,
        enabled=device.type == "cuda",
    )


def _load_checkpoint(model: torch.nn.Module, path: Path) -> None:
    state = torch.load(path, map_location="cpu", weights_only=True)
    state = {key.removeprefix("_orig_mod."): value for key, value in state.items()}
    model.load_state_dict(state, strict=True)


@torch.inference_mode()
def _encode_frames(
    vae: FSQVAE,
    frames_path: Path,
    device: torch.device,
    batch_size: int,
    tokens_per_frame: int,
) -> np.ndarray:
    frames = np.load(frames_path, mmap_mode="r")
    encoded: list[np.ndarray] = []
    for start in range(0, len(frames), batch_size):
        # np.load(..., mmap_mode="r") returns a read-only view.  Copy before
        # torch.from_numpy so PyTorch never receives a non-writable array.
        batch_np = np.array(frames[start:start + batch_size], copy=True)
        batch = torch.from_numpy(batch_np).to(
            device=device, dtype=torch.float32, non_blocking=True
        )
        indices = vae.encode(batch.unsqueeze(1).div_(255.0))
        encoded.append(
            indices.reshape(indices.size(0), tokens_per_frame)
            .to(torch.int32)
            .cpu()
            .numpy()
        )
    return np.concatenate(encoded, axis=0).astype(np.uint16, copy=False)


def _load_validation_episodes(
    *,
    death_dir: Path,
    expert_dir: Path,
    val_set: set[str],
    vae: FSQVAE,
    device: torch.device,
    encode_batch_size: int,
    tokens_per_frame: int,
    context_frames: int,
    cache_dir: Path,
    cache_key: str,
    max_val_episodes: int,
) -> tuple[list[Episode], list[dict[str, Any]]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    episodes: list[Episode] = []
    skipped: list[dict[str, Any]] = []

    candidates: list[tuple[str, Path]] = []
    for source, root in (("failure", death_dir), ("expert", expert_dir)):
        if not root.exists():
            continue
        for ep_dir in sorted(root.iterdir()):
            if (
                ep_dir.is_dir()
                and not SHIFT_RE.search(ep_dir.name)
                and is_val_episode(ep_dir.name, val_set)
            ):
                candidates.append((source, ep_dir))

    if max_val_episodes > 0:
        # Keep both strata represented in smoke tests.
        limited: list[tuple[str, Path]] = []
        for source in ("failure", "expert"):
            source_eps = [item for item in candidates if item[0] == source]
            limited.extend(source_eps[:max_val_episodes])
        candidates = limited

    print(f"Validation candidate directories: {len(candidates)}", flush=True)
    for index, (source, ep_dir) in enumerate(candidates, start=1):
        frames_path = ep_dir / "frames.npy"
        actions_path = ep_dir / "actions.npy"
        if not frames_path.exists() or not actions_path.exists():
            skipped.append(
                {"source": source, "episode": ep_dir.name, "reason": "missing_file"}
            )
            continue
        if frames_path.stat().st_size == 0 or actions_path.stat().st_size == 0:
            skipped.append(
                {"source": source, "episode": ep_dir.name, "reason": "empty_file"}
            )
            continue

        try:
            frame_count = int(np.load(frames_path, mmap_mode="r").shape[0])
            actions = np.load(actions_path).astype(np.int64, copy=False).reshape(-1)
        except (EOFError, OSError, ValueError) as exc:
            skipped.append(
                {
                    "source": source,
                    "episode": ep_dir.name,
                    "reason": f"load_error:{type(exc).__name__}",
                }
            )
            continue

        usable_length = min(frame_count, len(actions) + 1)
        if usable_length < context_frames + 1:
            skipped.append(
                {"source": source, "episode": ep_dir.name, "reason": "too_short"}
            )
            continue

        cache_path = cache_dir / f"{source}__{ep_dir.name}.npy"
        metadata_path = cache_path.with_suffix(".json")
        tokens: np.ndarray | None = None
        if cache_path.exists() and metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                cached = np.load(cache_path, mmap_mode="r")
                if (
                    metadata.get("cache_key") == cache_key
                    and cached.shape == (frame_count, tokens_per_frame)
                ):
                    tokens = np.asarray(cached[:usable_length])
            except (OSError, ValueError, json.JSONDecodeError):
                tokens = None

        if tokens is None:
            print(
                f"  Encoding {index}/{len(candidates)}: "
                f"{source}/{ep_dir.name} ({frame_count} frames)",
                flush=True,
            )
            tokens = _encode_frames(
                vae,
                frames_path,
                device,
                encode_batch_size,
                tokens_per_frame,
            )
            np.save(cache_path, tokens)
            metadata_path.write_text(
                json.dumps(
                    {
                        "cache_key": cache_key,
                        "source": source,
                        "episode": ep_dir.name,
                        "frame_count": frame_count,
                        "tokens_per_frame": tokens_per_frame,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            tokens = tokens[:usable_length]
        else:
            print(
                f"  Cached {index}/{len(candidates)}: "
                f"{source}/{ep_dir.name} ({usable_length} frames)",
                flush=True,
            )

        episodes.append(
            Episode(
                name=ep_dir.name,
                source=source,
                frames_path=frames_path,
                actions=actions[: max(0, usable_length - 1)],
                tokens=np.asarray(tokens[:usable_length]),
            )
        )

    return episodes, skipped


def _append_status(
    visual_tokens: np.ndarray,
    alive_token: int,
) -> np.ndarray:
    status = np.full(
        visual_tokens.shape[:-1] + (1,), alive_token, dtype=np.int64
    )
    return np.concatenate(
        [visual_tokens.astype(np.int64, copy=False), status], axis=-1
    )


def _forward_logits(
    model: WorldModel,
    contexts: torch.Tensor,
    actions: torch.Tensor,
    alive_token: int,
    amp_dtype: torch.dtype,
) -> torch.Tensor:
    batch = contexts.size(0)
    dummy_target = torch.full(
        (batch, 1, contexts.size(2)),
        alive_token,
        dtype=torch.long,
        device=contexts.device,
    )
    frame_tokens = torch.cat([contexts, dummy_target], dim=1)
    with _autocast(contexts.device, amp_dtype):
        output = model(frame_tokens, actions)
    if isinstance(output, tuple):
        return output[0]
    return output


def _binary_auroc(labels: np.ndarray, scores: np.ndarray) -> float:
    labels = labels.astype(np.int64, copy=False)
    positives = int(labels.sum())
    negatives = int(len(labels) - positives)
    if positives == 0 or negatives == 0:
        return float("nan")
    order = np.argsort(-scores, kind="mergesort")
    sorted_y = labels[order]
    sorted_s = scores[order]
    tp = fp = 0
    previous_tp = previous_fp = 0
    area = 0.0
    index = 0
    while index < len(sorted_y):
        end = index + 1
        while end < len(sorted_y) and sorted_s[end] == sorted_s[index]:
            end += 1
        group = sorted_y[index:end]
        tp += int(group.sum())
        fp += int(len(group) - group.sum())
        area += (fp - previous_fp) * (tp + previous_tp) / 2.0
        previous_tp, previous_fp = tp, fp
        index = end
    return area / (positives * negatives)


def _average_precision(labels: np.ndarray, scores: np.ndarray) -> float:
    labels = labels.astype(np.int64, copy=False)
    positives = int(labels.sum())
    if positives == 0:
        return float("nan")
    order = np.argsort(-scores, kind="mergesort")
    sorted_y = labels[order]
    cumulative_tp = np.cumsum(sorted_y)
    precision = cumulative_tp / np.arange(1, len(sorted_y) + 1)
    return float(precision[sorted_y == 1].sum() / positives)


def _ece(labels: np.ndarray, probabilities: np.ndarray, bins: int = 15) -> float:
    edges = np.linspace(0.0, 1.0, bins + 1)
    total = len(labels)
    value = 0.0
    for index in range(bins):
        if index == bins - 1:
            mask = (
                (probabilities >= edges[index])
                & (probabilities <= edges[index + 1])
            )
        else:
            mask = (
                (probabilities >= edges[index])
                & (probabilities < edges[index + 1])
            )
        count = int(mask.sum())
        if count:
            value += (count / total) * abs(
                float(probabilities[mask].mean()) - float(labels[mask].mean())
            )
    return value


def _binary_metrics(labels: np.ndarray, probabilities: np.ndarray) -> dict[str, Any]:
    labels = labels.astype(np.int64, copy=False)
    probabilities = np.clip(probabilities.astype(np.float64, copy=False), 1e-8, 1 - 1e-8)
    predictions = probabilities >= 0.5
    positives = labels == 1
    negatives = ~positives
    tp = int((predictions & positives).sum())
    fp = int((predictions & negatives).sum())
    fn = int((~predictions & positives).sum())
    tn = int((~predictions & negatives).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    binary_nll = -np.mean(
        labels * np.log(probabilities)
        + (1 - labels) * np.log(1 - probabilities)
    )
    positive_scores = probabilities[positives]
    negative_scores = probabilities[negatives]
    return {
        "n": int(len(labels)),
        "positives": int(positives.sum()),
        "prevalence": float(labels.mean()) if len(labels) else float("nan"),
        "auroc": _binary_auroc(labels, probabilities),
        "average_precision": _average_precision(labels, probabilities),
        "precision_at_0.5": precision,
        "recall_at_0.5": recall,
        "f1_at_0.5": f1,
        "specificity_at_0.5": tn / (tn + fp) if tn + fp else float("nan"),
        "false_positive_rate_at_0.5": fp / (fp + tn) if fp + tn else float("nan"),
        "binary_nll": float(binary_nll),
        "brier": float(np.mean((probabilities - labels) ** 2)),
        "ece_15_bins": _ece(labels, probabilities, bins=15),
        "positive_probability_mean": (
            float(positive_scores.mean()) if len(positive_scores) else float("nan")
        ),
        "positive_probability_median": (
            float(np.median(positive_scores)) if len(positive_scores) else float("nan")
        ),
        "negative_probability_mean": (
            float(negative_scores.mean()) if len(negative_scores) else float("nan")
        ),
        "negative_probability_p99": (
            float(np.quantile(negative_scores, 0.99))
            if len(negative_scores)
            else float("nan")
        ),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
    }


def _visual_metrics(
    nll: np.ndarray,
    correct_fraction: np.ndarray,
    exact: np.ndarray,
) -> dict[str, Any]:
    mean_nll = float(nll.mean())
    return {
        "n_windows": int(len(nll)),
        "visual_nll_nats_per_token": mean_nll,
        "visual_perplexity": float(math.exp(min(mean_nll, 50.0))),
        "visual_token_accuracy": float(correct_fraction.mean()),
        "exact_grid_accuracy": float(exact.mean()),
    }


def _stratified_episode_bootstrap(
    episode_values: dict[int, float],
    episode_sources: dict[int, str],
    reps: int,
    rng: np.random.Generator,
) -> tuple[float, float]:
    by_source: dict[str, np.ndarray] = {}
    for source in sorted(set(episode_sources.values())):
        by_source[source] = np.asarray(
            [
                episode_values[index]
                for index in sorted(episode_values)
                if episode_sources[index] == source
            ],
            dtype=np.float64,
        )
    draws = np.empty(reps, dtype=np.float64)
    for rep in range(reps):
        sampled: list[np.ndarray] = []
        for values in by_source.values():
            sampled.append(values[rng.integers(0, len(values), size=len(values))])
        draws[rep] = float(np.concatenate(sampled).mean())
    return float(np.quantile(draws, 0.025)), float(np.quantile(draws, 0.975))


def _action_metrics(
    *,
    mask: np.ndarray,
    factual_nll: np.ndarray,
    flipped_nll: np.ndarray,
    factual_accuracy: np.ndarray,
    flipped_accuracy: np.ndarray,
    changed_fraction: np.ndarray,
    episode_indices: np.ndarray,
    episode_sources: dict[int, str],
    bootstrap_reps: int,
    rng: np.random.Generator,
) -> dict[str, Any]:
    selected = np.flatnonzero(mask)
    advantage = flipped_nll[selected] - factual_nll[selected]
    episode_values: dict[int, float] = {}
    for episode_index in np.unique(episode_indices[selected]):
        ep_mask = episode_indices[selected] == episode_index
        episode_values[int(episode_index)] = float(advantage[ep_mask].mean())
    episode_mean = float(np.mean(list(episode_values.values())))
    ci_low, ci_high = _stratified_episode_bootstrap(
        episode_values,
        {idx: episode_sources[idx] for idx in episode_values},
        bootstrap_reps,
        rng,
    )
    return {
        "n_windows": int(len(selected)),
        "n_episodes": int(len(episode_values)),
        "factual_visual_nll": float(factual_nll[selected].mean()),
        "flipped_visual_nll": float(flipped_nll[selected].mean()),
        "window_weighted_nll_advantage": float(advantage.mean()),
        "episode_mean_nll_advantage": episode_mean,
        "episode_bootstrap_95_ci": [ci_low, ci_high],
        "window_fraction_factual_nll_better": float((advantage > 0).mean()),
        "episode_fraction_positive_advantage": float(
            np.mean(np.asarray(list(episode_values.values())) > 0)
        ),
        "factual_visual_token_accuracy": float(factual_accuracy[selected].mean()),
        "flipped_visual_token_accuracy": float(flipped_accuracy[selected].mean()),
        "prediction_changed_fraction": float(changed_fraction[selected].mean()),
    }


@torch.inference_mode()
def evaluate_one_step(
    *,
    model: WorldModel,
    episodes: list[Episode],
    device: torch.device,
    batch_size: int,
    context_frames: int,
    tokens_per_frame: int,
    vocab_size: int,
    alive_token: int,
    death_token: int,
    amp_dtype: torch.dtype,
    bootstrap_reps: int,
    rng: np.random.Generator,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    references: list[WindowRef] = []
    for episode_index, episode in enumerate(episodes):
        for start in range(episode.length - context_frames):
            references.append(WindowRef(episode_index, start))
    if not references:
        raise RuntimeError("No one-step validation windows are available")

    factual_nll_parts: list[np.ndarray] = []
    flipped_nll_parts: list[np.ndarray] = []
    factual_acc_parts: list[np.ndarray] = []
    flipped_acc_parts: list[np.ndarray] = []
    exact_parts: list[np.ndarray] = []
    changed_parts: list[np.ndarray] = []
    death_probability_parts: list[np.ndarray] = []
    death_label_parts: list[np.ndarray] = []
    action_parts: list[np.ndarray] = []
    source_parts: list[np.ndarray] = []
    episode_index_parts: list[np.ndarray] = []

    completed = 0
    for chunk in _chunks(references, batch_size):
        contexts_np = np.stack(
            [
                episodes[ref.episode_index].tokens[
                    ref.start:ref.start + context_frames
                ]
                for ref in chunk
            ]
        )
        targets_np = np.stack(
            [
                episodes[ref.episode_index].tokens[ref.start + context_frames]
                for ref in chunk
            ]
        ).astype(np.int64, copy=False)
        actions_np = np.stack(
            [
                episodes[ref.episode_index].actions[
                    ref.start:ref.start + context_frames
                ]
                for ref in chunk
            ]
        ).astype(np.int64, copy=False)
        if actions_np.shape[1] != context_frames:
            raise RuntimeError("An episode has too few actions for its frame window")

        death_labels_np = np.asarray(
            [
                int(
                    episodes[ref.episode_index].source == "failure"
                    and ref.start + context_frames
                    == episodes[ref.episode_index].length - 1
                )
                for ref in chunk
            ],
            dtype=np.int64,
        )
        source_np = np.asarray(
            [episodes[ref.episode_index].source for ref in chunk]
        )
        episode_indices_np = np.asarray(
            [ref.episode_index for ref in chunk], dtype=np.int64
        )

        contexts = torch.from_numpy(
            _append_status(contexts_np, alive_token)
        ).to(device, non_blocking=True)
        actions = torch.from_numpy(actions_np).to(device, non_blocking=True)
        targets = torch.from_numpy(targets_np).to(device, non_blocking=True)

        factual_logits = _forward_logits(
            model, contexts, actions, alive_token, amp_dtype
        )
        factual_visual_logits = factual_logits[:, :tokens_per_frame, :vocab_size].float()
        factual_log_probs = F.log_softmax(factual_visual_logits, dim=-1)
        factual_nll = -factual_log_probs.gather(
            -1, targets.unsqueeze(-1)
        ).squeeze(-1).mean(dim=-1)
        factual_prediction = factual_visual_logits.argmax(dim=-1)
        factual_correct = factual_prediction.eq(targets)
        status_logits = factual_logits[
            :, tokens_per_frame, [alive_token, death_token]
        ].float()
        death_probability = F.softmax(status_logits, dim=-1)[:, 1]

        flipped_actions = actions.clone()
        flipped_actions[:, -1] = 1 - flipped_actions[:, -1]
        flipped_logits = _forward_logits(
            model, contexts, flipped_actions, alive_token, amp_dtype
        )
        flipped_visual_logits = flipped_logits[:, :tokens_per_frame, :vocab_size].float()
        flipped_log_probs = F.log_softmax(flipped_visual_logits, dim=-1)
        flipped_nll = -flipped_log_probs.gather(
            -1, targets.unsqueeze(-1)
        ).squeeze(-1).mean(dim=-1)
        flipped_prediction = flipped_visual_logits.argmax(dim=-1)

        factual_nll_parts.append(factual_nll.cpu().numpy())
        flipped_nll_parts.append(flipped_nll.cpu().numpy())
        factual_acc_parts.append(factual_correct.float().mean(dim=-1).cpu().numpy())
        flipped_acc_parts.append(
            flipped_prediction.eq(targets).float().mean(dim=-1).cpu().numpy()
        )
        exact_parts.append(factual_correct.all(dim=-1).cpu().numpy())
        changed_parts.append(
            flipped_prediction.ne(factual_prediction)
            .float()
            .mean(dim=-1)
            .cpu()
            .numpy()
        )
        death_probability_parts.append(death_probability.cpu().numpy())
        death_label_parts.append(death_labels_np)
        action_parts.append(actions_np[:, -1])
        source_parts.append(source_np)
        episode_index_parts.append(episode_indices_np)

        completed += len(chunk)
        if completed % (batch_size * 10) == 0 or completed == len(references):
            print(
                f"  One-step windows: {completed}/{len(references)}",
                flush=True,
            )

    factual_nll = np.concatenate(factual_nll_parts)
    flipped_nll = np.concatenate(flipped_nll_parts)
    factual_accuracy = np.concatenate(factual_acc_parts)
    flipped_accuracy = np.concatenate(flipped_acc_parts)
    exact = np.concatenate(exact_parts)
    changed_fraction = np.concatenate(changed_parts)
    death_probabilities = np.concatenate(death_probability_parts)
    death_labels = np.concatenate(death_label_parts)
    actions = np.concatenate(action_parts)
    sources = np.concatenate(source_parts)
    episode_indices = np.concatenate(episode_index_parts)
    episode_sources = {
        index: episode.source for index, episode in enumerate(episodes)
    }

    visual: dict[str, Any] = {}
    death: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    group_masks = {
        "all": np.ones(len(factual_nll), dtype=bool),
        "failure": sources == "failure",
        "expert": sources == "expert",
        "idle": actions == 0,
        "jump": actions == 1,
        "failure_idle": (sources == "failure") & (actions == 0),
        "failure_jump": (sources == "failure") & (actions == 1),
        "expert_idle": (sources == "expert") & (actions == 0),
        "expert_jump": (sources == "expert") & (actions == 1),
    }
    for group, mask in group_masks.items():
        if not mask.any():
            continue
        visual[group] = _visual_metrics(
            factual_nll[mask], factual_accuracy[mask], exact[mask]
        )
        if group in {"all", "failure", "expert"}:
            death[group] = _binary_metrics(
                death_labels[mask], death_probabilities[mask]
            )
        rows.append(
            {
                "group": group,
                **visual[group],
                "death_positives": int(death_labels[mask].sum()),
                "death_probability_mean": float(death_probabilities[mask].mean()),
            }
        )

    action_intervention: dict[str, Any] = {}
    for group, mask in group_masks.items():
        if not mask.any():
            continue
        action_intervention[group] = _action_metrics(
            mask=mask,
            factual_nll=factual_nll,
            flipped_nll=flipped_nll,
            factual_accuracy=factual_accuracy,
            flipped_accuracy=flipped_accuracy,
            changed_fraction=changed_fraction,
            episode_indices=episode_indices,
            episode_sources=episode_sources,
            bootstrap_reps=bootstrap_reps,
            rng=rng,
        )

    per_episode: dict[str, Any] = {}
    for episode_index, episode in enumerate(episodes):
        mask = episode_indices == episode_index
        per_episode[f"{episode.source}/{episode.name}"] = {
            "source": episode.source,
            "n_windows": int(mask.sum()),
            "visual_nll": float(factual_nll[mask].mean()),
            "visual_token_accuracy": float(factual_accuracy[mask].mean()),
            "action_nll_advantage": float(
                (flipped_nll[mask] - factual_nll[mask]).mean()
            ),
            "death_target": int(death_labels[mask].sum()),
            "terminal_death_probability": (
                float(death_probabilities[mask & (death_labels == 1)][0])
                if (mask & (death_labels == 1)).any()
                else None
            ),
            "max_nonterminal_death_probability": (
                float(death_probabilities[mask & (death_labels == 0)].max())
                if (mask & (death_labels == 0)).any()
                else None
            ),
        }

    result = {
        "visual_prediction": visual,
        "death_prediction": death,
        "action_intervention": action_intervention,
        "per_episode": per_episode,
        "protocol": {
            "n_windows": int(len(references)),
            "intervention": (
                "Flip only the last binary action in the K-action conditioning "
                "window while keeping the recorded visual context fixed."
            ),
            "nll_definition": (
                "Hard-target negative log likelihood under a softmax restricted "
                "to the visual-token vocabulary."
            ),
            "ci_unit": (
                "Validation episodes, stratified by failure/expert source; "
                "windows within an episode remain clustered."
            ),
        },
    }
    return result, rows


def _sample_rollout_references(
    *,
    episodes: list[Episode],
    context_frames: int,
    horizon: int,
    requested_samples: int,
    max_per_episode: int,
    rng: np.random.Generator,
) -> list[WindowRef]:
    pools: dict[str, list[WindowRef]] = {"failure": [], "expert": []}
    for episode_index, episode in enumerate(episodes):
        count = episode.length - context_frames - horizon + 1
        if count <= 0:
            continue
        starts = np.arange(count)
        if len(starts) > max_per_episode:
            starts = rng.choice(starts, size=max_per_episode, replace=False)
        pools[episode.source].extend(
            WindowRef(episode_index, int(start)) for start in starts
        )

    for pool in pools.values():
        rng.shuffle(pool)

    target_each = requested_samples // 2
    selected: list[WindowRef] = []
    leftovers: list[WindowRef] = []
    for source in ("failure", "expert"):
        take = min(target_each, len(pools[source]))
        selected.extend(pools[source][:take])
        leftovers.extend(pools[source][take:])
    remaining = requested_samples - len(selected)
    rng.shuffle(leftovers)
    selected.extend(leftovers[:remaining])
    rng.shuffle(selected)
    return selected


def _jensen_shannon_from_tokens(
    predictions: np.ndarray,
    targets: np.ndarray,
    vocab_size: int,
) -> float:
    pred_counts = np.bincount(
        predictions.reshape(-1), minlength=vocab_size
    ).astype(np.float64)
    target_counts = np.bincount(
        targets.reshape(-1), minlength=vocab_size
    ).astype(np.float64)
    p = pred_counts / pred_counts.sum()
    q = target_counts / target_counts.sum()
    midpoint = 0.5 * (p + q)
    p_mask = p > 0
    q_mask = q > 0
    kl_p = np.sum(p[p_mask] * np.log(p[p_mask] / midpoint[p_mask]))
    kl_q = np.sum(q[q_mask] * np.log(q[q_mask] / midpoint[q_mask]))
    return float(0.5 * (kl_p + kl_q))


@torch.inference_mode()
def _decoded_sample_metrics(
    *,
    vae: FSQVAE,
    predictions: np.ndarray,
    targets: np.ndarray,
    device: torch.device,
    grid_size: int,
    batch_size: int,
    amp_dtype: torch.dtype,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mse_parts: list[np.ndarray] = []
    predicted_density_parts: list[np.ndarray] = []
    target_density_parts: list[np.ndarray] = []
    for start in range(0, len(predictions), batch_size):
        pred = torch.from_numpy(
            predictions[start:start + batch_size].astype(np.int64, copy=False)
        ).reshape(-1, grid_size, grid_size).to(device)
        target = torch.from_numpy(
            targets[start:start + batch_size].astype(np.int64, copy=False)
        ).reshape(-1, grid_size, grid_size).to(device)
        with _autocast(device, amp_dtype):
            pred_image = vae.decode_indices(pred).float()
            target_image = vae.decode_indices(target).float()
        mse_parts.append(
            (pred_image - target_image)
            .square()
            .flatten(1)
            .mean(dim=1)
            .cpu()
            .numpy()
        )
        predicted_density_parts.append(
            pred_image.flatten(1).mean(dim=1).cpu().numpy()
        )
        target_density_parts.append(
            target_image.flatten(1).mean(dim=1).cpu().numpy()
        )
    return (
        np.concatenate(mse_parts),
        np.concatenate(predicted_density_parts),
        np.concatenate(target_density_parts),
    )


def _rollout_group_metrics(
    *,
    mask: np.ndarray,
    nll: np.ndarray,
    accuracy: np.ndarray,
    exact: np.ndarray,
    death_probability: np.ndarray,
    death_label: np.ndarray,
    decoded_mse: np.ndarray,
    predicted_density: np.ndarray,
    target_density: np.ndarray,
    predictions: np.ndarray,
    targets: np.ndarray,
    vocab_size: int,
) -> dict[str, Any]:
    mean_mse = float(decoded_mse[mask].mean())
    return {
        "n_rollouts": int(mask.sum()),
        "visual_nll_nats_per_token": float(nll[mask].mean()),
        "visual_token_accuracy": float(accuracy[mask].mean()),
        "exact_grid_accuracy": float(exact[mask].mean()),
        "decoded_mse": mean_mse,
        "decoded_psnr_db": float(
            10.0 * math.log10(1.0 / max(mean_mse, 1e-12))
        ),
        "predicted_decoded_density": float(predicted_density[mask].mean()),
        "target_decoded_density": float(target_density[mask].mean()),
        "token_marginal_js_nats": _jensen_shannon_from_tokens(
            predictions[mask], targets[mask], vocab_size
        ),
        "mean_death_probability": float(death_probability[mask].mean()),
        "death_targets": int(death_label[mask].sum()),
    }


@torch.inference_mode()
def evaluate_rollout_cohort(
    *,
    cohort_name: str,
    model: WorldModel,
    vae: FSQVAE,
    episodes: list[Episode],
    device: torch.device,
    batch_size: int,
    decode_batch_size: int,
    context_frames: int,
    tokens_per_frame: int,
    vocab_size: int,
    alive_token: int,
    death_token: int,
    amp_dtype: torch.dtype,
    horizons: list[int],
    requested_samples: int,
    max_per_episode: int,
    rng: np.random.Generator,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    max_horizon = max(horizons)
    references = _sample_rollout_references(
        episodes=episodes,
        context_frames=context_frames,
        horizon=max_horizon,
        requested_samples=requested_samples,
        max_per_episode=max_per_episode,
        rng=rng,
    )
    if not references:
        return {
            "protocol": {
                "requested_samples": requested_samples,
                "actual_samples": 0,
                "max_horizon": max_horizon,
            },
            "horizons": {},
        }, []

    episode_indices = np.asarray(
        [reference.episode_index for reference in references], dtype=np.int64
    )
    sources = np.asarray(
        [episodes[index].source for index in episode_indices]
    )
    starts = np.asarray([reference.start for reference in references], dtype=np.int64)
    contexts_np = np.stack(
        [
            episodes[reference.episode_index].tokens[
                reference.start:reference.start + context_frames
            ]
            for reference in references
        ]
    )
    action_sequences = np.stack(
        [
            episodes[reference.episode_index].actions[
                reference.start:
                reference.start + context_frames + max_horizon - 1
            ]
            for reference in references
        ]
    ).astype(np.int64, copy=False)
    targets_np = np.stack(
        [
            episodes[reference.episode_index].tokens[
                reference.start + context_frames:
                reference.start + context_frames + max_horizon
            ]
            for reference in references
        ]
    ).astype(np.int64, copy=False)
    death_targets_np = np.zeros(
        (len(references), max_horizon), dtype=np.int64
    )
    for row, reference in enumerate(references):
        episode = episodes[reference.episode_index]
        if episode.source == "failure":
            target_indices = (
                reference.start
                + context_frames
                + np.arange(max_horizon)
            )
            death_targets_np[row] = target_indices == episode.length - 1

    context = torch.from_numpy(
        _append_status(contexts_np, alive_token)
    ).to(device, non_blocking=True)
    context_actions = torch.from_numpy(
        action_sequences[:, :context_frames]
    ).to(device, non_blocking=True)
    targets = torch.from_numpy(targets_np).to(device, non_blocking=True)

    horizon_outputs: dict[int, dict[str, np.ndarray]] = {}
    print(
        f"  {cohort_name}: {len(references)} rollouts to {max_horizon} steps "
        f"({int((sources == 'failure').sum())} failure, "
        f"{int((sources == 'expert').sum())} expert)",
        flush=True,
    )

    for step in range(1, max_horizon + 1):
        prediction_parts: list[torch.Tensor] = []
        death_probability_parts: list[torch.Tensor] = []
        nll_parts: list[torch.Tensor] = []
        accuracy_parts: list[torch.Tensor] = []
        exact_parts: list[torch.Tensor] = []

        for batch_start in range(0, len(references), batch_size):
            batch_end = min(batch_start + batch_size, len(references))
            ctx_batch = context[batch_start:batch_end]
            action_batch = context_actions[batch_start:batch_end]
            logits = _forward_logits(
                model, ctx_batch, action_batch, alive_token, amp_dtype
            )
            visual_logits = logits[:, :tokens_per_frame, :vocab_size].float()
            prediction = visual_logits.argmax(dim=-1)
            prediction_parts.append(prediction)
            status_logits = logits[
                :, tokens_per_frame, [alive_token, death_token]
            ].float()
            death_probability_parts.append(
                F.softmax(status_logits, dim=-1)[:, 1]
            )

            if step in horizons:
                target = targets[batch_start:batch_end, step - 1]
                log_probs = F.log_softmax(visual_logits, dim=-1)
                nll_parts.append(
                    -log_probs.gather(-1, target.unsqueeze(-1))
                    .squeeze(-1)
                    .mean(dim=-1)
                )
                correct = prediction.eq(target)
                accuracy_parts.append(correct.float().mean(dim=-1))
                exact_parts.append(correct.all(dim=-1))

        prediction = torch.cat(prediction_parts, dim=0)
        death_probability = torch.cat(death_probability_parts, dim=0)
        if step in horizons:
            horizon_outputs[step] = {
                "prediction": prediction.cpu().numpy().astype(np.int64),
                "target": targets[:, step - 1].cpu().numpy().astype(np.int64),
                "nll": torch.cat(nll_parts).cpu().numpy(),
                "accuracy": torch.cat(accuracy_parts).cpu().numpy(),
                "exact": torch.cat(exact_parts).cpu().numpy(),
                "death_probability": death_probability.cpu().numpy(),
                "death_label": death_targets_np[:, step - 1],
            }

        if step < max_horizon:
            new_status = torch.full(
                (len(references), 1),
                alive_token,
                dtype=torch.long,
                device=device,
            )
            new_frame = torch.cat([prediction, new_status], dim=1).unsqueeze(1)
            context = torch.cat([context[:, 1:], new_frame], dim=1)
            next_action = torch.from_numpy(
                action_sequences[:, context_frames + step - 1]
            ).to(device, non_blocking=True)
            context_actions = torch.cat(
                [context_actions[:, 1:], next_action.unsqueeze(1)], dim=1
            )

        if step == 1 or step % 10 == 0 or step == max_horizon:
            print(f"    generated step {step}/{max_horizon}", flush=True)

    unique_episode_counts = {
        source: int(
            len(
                set(
                    episode_indices[sources == source].tolist()
                )
            )
        )
        for source in ("failure", "expert")
    }
    result: dict[str, Any] = {
        "protocol": {
            "requested_samples": requested_samples,
            "actual_samples": int(len(references)),
            "max_per_episode": max_per_episode,
            "horizons": horizons,
            "max_horizon": max_horizon,
            "source_counts": {
                "failure": int((sources == "failure").sum()),
                "expert": int((sources == "expert").sum()),
            },
            "source_episode_counts": unique_episode_counts,
            "conditioning": (
                "Greedy autoregression under the recorded future action "
                "sequence; generated frames are fed back with ALIVE status."
            ),
        },
        "horizons": {},
    }
    rows: list[dict[str, Any]] = []
    grid_size = int(round(math.sqrt(tokens_per_frame)))
    for horizon in horizons:
        output = horizon_outputs[horizon]
        decoded_mse, predicted_density, target_density = _decoded_sample_metrics(
            vae=vae,
            predictions=output["prediction"],
            targets=output["target"],
            device=device,
            grid_size=grid_size,
            batch_size=decode_batch_size,
            amp_dtype=amp_dtype,
        )
        groups: dict[str, Any] = {}
        for group, mask in {
            "all": np.ones(len(references), dtype=bool),
            "failure": sources == "failure",
            "expert": sources == "expert",
        }.items():
            if not mask.any():
                continue
            metrics = _rollout_group_metrics(
                mask=mask,
                nll=output["nll"],
                accuracy=output["accuracy"],
                exact=output["exact"],
                death_probability=output["death_probability"],
                death_label=output["death_label"],
                decoded_mse=decoded_mse,
                predicted_density=predicted_density,
                target_density=target_density,
                predictions=output["prediction"],
                targets=output["target"],
                vocab_size=vocab_size,
            )
            groups[group] = metrics
            rows.append(
                {
                    "cohort": cohort_name,
                    "horizon": horizon,
                    "group": group,
                    **metrics,
                }
            )
        result["horizons"][str(horizon)] = groups

    result["sample_manifest"] = [
        {
            "source": episodes[reference.episode_index].source,
            "episode": episodes[reference.episode_index].name,
            "start": int(starts[row]),
        }
        for row, reference in enumerate(references)
    ]
    return result, rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(_json_value(row))


def _format_metric(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(numeric):
        return "n/a"
    return f"{numeric:.{digits}f}"


def _write_report(path: Path, results: dict[str, Any]) -> None:
    one = results["one_step"]
    visual = one["visual_prediction"]["all"]
    death = one["death_prediction"]["all"]
    action = one["action_intervention"]["all"]
    lines = [
        "# Frozen world-model held-out diagnostics",
        "",
        "These are post-hoc diagnostics on the fixed global validation split. "
        "The split was excluded from gradient updates, but its death F1 was "
        "used historically to select the dynamics checkpoint. The action "
        "intervention and autoregressive metrics were not selection criteria.",
        "",
        "## One-step prediction",
        "",
        f"- Windows: {visual['n_windows']:,}",
        f"- Visual-token NLL: {_format_metric(visual['visual_nll_nats_per_token'])} nats/token",
        f"- Visual-token accuracy: {_format_metric(100 * visual['visual_token_accuracy'], 2)}%",
        f"- Exact 8x8 grid accuracy: {_format_metric(100 * visual['exact_grid_accuracy'], 2)}%",
        "",
        "## Death/status prediction",
        "",
        f"- Positive terminal transitions: {death['positives']:,}",
        f"- AUROC: {_format_metric(death['auroc'])}",
        f"- Average precision: {_format_metric(death['average_precision'])}",
        f"- F1 at 0.5: {_format_metric(death['f1_at_0.5'])}",
        f"- Brier score: {_format_metric(death['brier'])}",
        f"- 15-bin ECE: {_format_metric(death['ece_15_bins'])}",
        "",
        "## Paired action intervention",
        "",
        "Only the final binary action is flipped while the recorded visual "
        "context is held fixed. Positive NLL advantage means that the factual "
        "action assigns more likelihood to the observed next frame.",
        "",
        f"- Episode-mean factual NLL advantage: "
        f"{_format_metric(action['episode_mean_nll_advantage'])} nats/token",
        f"- Episode-bootstrap 95% CI: "
        f"[{_format_metric(action['episode_bootstrap_95_ci'][0])}, "
        f"{_format_metric(action['episode_bootstrap_95_ci'][1])}]",
        f"- Windows favoring factual action: "
        f"{_format_metric(100 * action['window_fraction_factual_nll_better'], 2)}%",
        f"- Predicted tokens changed by the flip: "
        f"{_format_metric(100 * action['prediction_changed_fraction'], 2)}%",
        "",
        "## Autoregressive fidelity",
        "",
        "Greedy predictions are fed back while replaying the recorded future "
        "actions. Exact trajectory agreement is a fidelity diagnostic, not by "
        "itself a measure of perceptual plausibility after trajectories diverge.",
        "",
        "| Cohort | Horizon | N | Token accuracy | Decoded PSNR | Token-marginal JS |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for cohort_name, cohort in results["rollouts"].items():
        for horizon, groups in cohort["horizons"].items():
            metrics = groups["all"]
            lines.append(
                f"| {cohort_name} | {horizon} | {metrics['n_rollouts']} | "
                f"{_format_metric(100 * metrics['visual_token_accuracy'], 2)}% | "
                f"{_format_metric(metrics['decoded_psnr_db'], 2)} dB | "
                f"{_format_metric(metrics['token_marginal_js_nats'])} |"
            )
    lines.extend(
        [
            "",
            "The extended cohort contains only trajectories long enough for its "
            "maximum requested horizon; consult `diagnostics.json` for source "
            "and episode counts before generalizing its results.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Held-out diagnostics for the frozen DashVMC world model"
    )
    parser.add_argument(
        "--config", default="configs/deepdash/v7-phase0.yaml"
    )
    parser.add_argument(
        "--transformer-checkpoint",
        default="checkpoints_v7/transformer_best.pt",
    )
    parser.add_argument(
        "--fsq-checkpoint", default="checkpoints_v7/fsq_best.pt"
    )
    parser.add_argument(
        "--episodes-dir", default="data/deepdash/death_episodes"
    )
    parser.add_argument(
        "--expert-episodes-dir", default="data/deepdash/expert_episodes"
    )
    parser.add_argument(
        "--output-dir",
        default="analysis/world_model_diagnostics_20260728",
    )
    parser.add_argument("--encode-batch-size", type=int, default=512)
    parser.add_argument("--eval-batch-size", type=int, default=256)
    parser.add_argument("--decode-batch-size", type=int, default=512)
    parser.add_argument("--standard-samples", type=int, default=1024)
    parser.add_argument("--standard-max-per-episode", type=int, default=64)
    parser.add_argument("--extended-samples", type=int, default=256)
    parser.add_argument("--extended-max-per-episode", type=int, default=192)
    parser.add_argument(
        "--standard-horizons",
        type=int,
        nargs="+",
        default=[1, 5, 10, 20, 45],
    )
    parser.add_argument(
        "--extended-horizons",
        type=int,
        nargs="+",
        default=[45, 100, 200],
    )
    parser.add_argument("--bootstrap-reps", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=20260728)
    parser.add_argument(
        "--amp-dtype",
        choices=["bfloat16", "float16"],
        default="bfloat16",
    )
    parser.add_argument(
        "--compile",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Compile the transformer backbone (default: true).",
    )
    parser.add_argument(
        "--max-val-episodes",
        type=int,
        default=0,
        help="Smoke-test limit per source; zero evaluates the full split.",
    )
    args = parser.parse_args()

    started = time.time()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    config_path = Path(args.config)
    transformer_checkpoint = Path(args.transformer_checkpoint)
    fsq_checkpoint = Path(args.fsq_checkpoint)
    death_dir = Path(args.episodes_dir)
    expert_dir = Path(args.expert_episodes_dir)

    if not torch.cuda.is_available():
        raise SystemExit("This evaluator requires a CUDA GPU")
    device = torch.device("cuda")
    amp_dtype = getattr(torch, args.amp_dtype)
    print(f"Device: {torch.cuda.get_device_name(0)}", flush=True)
    print(f"PyTorch: {torch.__version__}; AMP: {args.amp_dtype}", flush=True)

    config = load_config(config_path, section="transformer")
    levels = list(config["levels"])
    tokens_per_frame = int(config["tokens_per_frame"])
    context_frames = int(config["context_frames"])
    vocab_size = int(config["vocab_size"])
    if math.prod(levels) != vocab_size:
        raise ValueError(
            f"FSQ levels imply {math.prod(levels)} codes, config says {vocab_size}"
        )

    transformer_sha256 = _sha256(transformer_checkpoint)
    fsq_sha256 = _sha256(fsq_checkpoint)
    print(f"Transformer SHA-256: {transformer_sha256}", flush=True)
    print(f"FSQ SHA-256: {fsq_sha256}", flush=True)

    vae = FSQVAE(levels=levels).to(device)
    _load_checkpoint(vae, fsq_checkpoint)
    vae.eval()
    for parameter in vae.parameters():
        parameter.requires_grad_(False)

    model = WorldModel(
        vocab_size=vocab_size,
        n_actions=2,
        embed_dim=int(config["embed_dim"]),
        n_heads=int(config["n_heads"]),
        n_layers=int(config["n_layers"]),
        context_frames=context_frames,
        dropout=float(config["dropout"]),
        tokens_per_frame=tokens_per_frame,
        adaln=bool(config.get("adaln", False)),
        fsq_dim=None,
        use_cpc=bool(config.get("use_cpc", False)),
        cpc_dim=int(config.get("cpc_dim", 64)),
    ).to(device)
    _load_checkpoint(model, transformer_checkpoint)
    # The CPC modules must exist to load the training checkpoint, but the
    # auxiliary loss is irrelevant at evaluation and would add computation.
    model.use_cpc = False
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    if args.compile:
        try:
            model._backbone_forward = torch.compile(
                model._backbone_forward,
                mode="reduce-overhead",
                dynamic=True,
            )
            print("Compiled transformer backbone", flush=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            print(f"torch.compile unavailable; continuing eagerly: {exc}", flush=True)

    val_set = get_val_episodes(death_dir, expert_dir)
    cache_key = (
        f"fsq={fsq_sha256};levels={levels};tokens={tokens_per_frame}"
    )
    episodes, skipped = _load_validation_episodes(
        death_dir=death_dir,
        expert_dir=expert_dir,
        val_set=val_set,
        vae=vae,
        device=device,
        encode_batch_size=args.encode_batch_size,
        tokens_per_frame=tokens_per_frame,
        context_frames=context_frames,
        cache_dir=output_dir / "token_cache",
        cache_key=cache_key,
        max_val_episodes=args.max_val_episodes,
    )
    if not episodes:
        raise RuntimeError("No usable validation episodes were loaded")
    source_counts = {
        source: sum(episode.source == source for episode in episodes)
        for source in ("failure", "expert")
    }
    frame_counts = {
        source: sum(
            episode.length for episode in episodes if episode.source == source
        )
        for source in ("failure", "expert")
    }
    print(
        f"Usable validation episodes: {len(episodes)} "
        f"({source_counts['failure']} failure, {source_counts['expert']} expert); "
        f"{sum(frame_counts.values())} frames",
        flush=True,
    )

    rng = np.random.default_rng(args.seed)
    print("Evaluating all one-step validation windows...", flush=True)
    one_step, one_step_rows = evaluate_one_step(
        model=model,
        episodes=episodes,
        device=device,
        batch_size=args.eval_batch_size,
        context_frames=context_frames,
        tokens_per_frame=tokens_per_frame,
        vocab_size=vocab_size,
        alive_token=model.ALIVE_TOKEN,
        death_token=model.DEATH_TOKEN,
        amp_dtype=amp_dtype,
        bootstrap_reps=args.bootstrap_reps,
        rng=rng,
    )

    rollouts: dict[str, Any] = {}
    rollout_rows: list[dict[str, Any]] = []
    cohort_specs = [
        (
            "standard",
            sorted(set(args.standard_horizons)),
            args.standard_samples,
            args.standard_max_per_episode,
        ),
        (
            "extended",
            sorted(set(args.extended_horizons)),
            args.extended_samples,
            args.extended_max_per_episode,
        ),
    ]
    for cohort_name, horizons, samples, max_per_episode in cohort_specs:
        print(f"Evaluating {cohort_name} autoregressive cohort...", flush=True)
        cohort, rows = evaluate_rollout_cohort(
            cohort_name=cohort_name,
            model=model,
            vae=vae,
            episodes=episodes,
            device=device,
            batch_size=args.eval_batch_size,
            decode_batch_size=args.decode_batch_size,
            context_frames=context_frames,
            tokens_per_frame=tokens_per_frame,
            vocab_size=vocab_size,
            alive_token=model.ALIVE_TOKEN,
            death_token=model.DEATH_TOKEN,
            amp_dtype=amp_dtype,
            horizons=horizons,
            requested_samples=samples,
            max_per_episode=max_per_episode,
            rng=rng,
        )
        rollouts[cohort_name] = cohort
        rollout_rows.extend(rows)

    elapsed = time.time() - started
    results = {
        "schema_version": 1,
        "evaluation": "frozen_world_model_heldout_diagnostics",
        "post_training_only": True,
        "seed": args.seed,
        "elapsed_seconds": elapsed,
        "artifacts": {
            "config": str(config_path),
            "transformer_checkpoint": str(transformer_checkpoint),
            "transformer_sha256": transformer_sha256,
            "fsq_checkpoint": str(fsq_checkpoint),
            "fsq_sha256": fsq_sha256,
        },
        "data": {
            "split_seed": 42,
            "split": "global base-episode validation stratum",
            "role": (
                "Excluded from gradient updates. Dynamics checkpoint selection "
                "used death F1 on this stratum; action-intervention and "
                "autoregressive metrics are post-hoc."
            ),
            "source_episode_counts": source_counts,
            "source_frame_counts": frame_counts,
            "usable_episodes": len(episodes),
            "skipped": skipped,
            "max_val_episodes_per_source": args.max_val_episodes,
        },
        "model": {
            "context_frames": context_frames,
            "tokens_per_frame": tokens_per_frame,
            "vocab_size": vocab_size,
            "levels": levels,
        },
        "one_step": one_step,
        "rollouts": rollouts,
    }
    safe_results = _json_value(results)
    (output_dir / "diagnostics.json").write_text(
        json.dumps(safe_results, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_csv(output_dir / "one_step_metrics.csv", one_step_rows)
    _write_csv(output_dir / "rollout_metrics.csv", rollout_rows)
    _write_report(output_dir / "report.md", safe_results)

    print("", flush=True)
    print((output_dir / "report.md").read_text(encoding="utf-8"), flush=True)
    print(f"Results written to {output_dir}", flush=True)
    print(f"Elapsed: {elapsed / 60:.1f} minutes", flush=True)


if __name__ == "__main__":
    main()

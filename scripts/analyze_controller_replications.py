"""Validate and summarize the three retained V7 BC -> PPO replications.

The script reads the raw live-evaluation JSON files and local training
provenance for seeds 43--45. It writes a machine-readable summary and a
Markdown report with per-seed means, sample standard deviations, and
attempt-level bootstrap intervals for PPO minus its exact parent BC, plus
seed-level aggregate mean, IQM, and empirical-reference optimality-gap
intervals on the official levels.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "analysis" / "2026-07-27_v7_controller_replication"
BOOTSTRAP_RESAMPLES = 50_000
BOOTSTRAP_SEED = 20_260_727
AGGREGATE_BOOTSTRAP_SEED = 20_260_728

# Historical no-op controls use the same 30-FPS diagnostic protocol and provide
# a fixed zero point for official-level score normalization.
OFFICIAL_NOOP_MEAN_FRAMES = {
    "Stereo Madness": 46.2,
    "Back on Track": 63.4,
    "Polargeist": 41.5,
}

SEEDS = {
    43: {
        "eval_dir": ROOT / "analysis" / "2026-07-25_v7_seed43_live25",
        "checkpoint_dir": ROOT / "checkpoints_v7_controller_seed43",
    },
    44: {
        "eval_dir": ROOT / "analysis" / "2026-07-26_v7_seed44_live25",
        "checkpoint_dir": ROOT / "checkpoints_v7_controller_seed44",
    },
    45: {
        "eval_dir": ROOT / "analysis" / "2026-07-27_v7_seed45_live25",
        "checkpoint_dir": ROOT / "checkpoints_v7_controller_seed45",
    },
}

LEVELS = (
    ("stereo_madness", "Stereo Madness", "official"),
    ("back_on_track", "Back on Track", "official"),
    ("polargeist", "Polargeist", "official"),
    ("stereo_madness_copy", "Stereo Madness Copy", "auxiliary"),
    ("stereo_insane_nerfed", "Stereo INSANE Nerfed", "auxiliary"),
)


def parse_manifest(path: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            manifest[key] = value
    return manifest


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bootstrap_difference(
    bc: np.ndarray, ppo: np.ndarray, *, seed: int
) -> tuple[float, float]:
    """Independent attempt bootstrap for one frozen PPO/parent-BC pair."""
    rng = np.random.default_rng(seed)
    differences = []
    batch_size = 5_000
    for start in range(0, BOOTSTRAP_RESAMPLES, batch_size):
        size = min(batch_size, BOOTSTRAP_RESAMPLES - start)
        bc_means = rng.choice(bc, size=(size, bc.size), replace=True).mean(axis=1)
        ppo_means = rng.choice(ppo, size=(size, ppo.size), replace=True).mean(axis=1)
        differences.append(ppo_means - bc_means)
    low, high = np.percentile(np.concatenate(differences), [2.5, 97.5])
    return float(low), float(high)


def aggregate_metrics(scores: np.ndarray) -> np.ndarray:
    """Mean, IQM, and optimality gap for a runs-by-tasks score matrix."""
    flat = np.sort(scores.reshape(scores.shape[:-2] + (-1,)), axis=-1)
    trim = int(0.25 * flat.shape[-1])
    middle = flat[..., trim : flat.shape[-1] - trim]
    return np.stack(
        (
            scores.mean(axis=(-2, -1)),
            middle.mean(axis=-1),
            np.maximum(0.0, 1.0 - scores).mean(axis=(-2, -1)),
        ),
        axis=-1,
    )


def seed_level_aggregate_summary(rows: list[dict]) -> dict:
    """Aggregate official-level seed means with stratified bootstrap intervals."""
    official_levels = [
        level_name for _, level_name, group in LEVELS if group == "official"
    ]
    seeds = sorted({row["seed"] for row in rows})
    raw_scores = {}
    for policy in ("bc", "ppo"):
        raw_scores[policy] = np.asarray(
            [
                [
                    next(
                        row[f"{policy}_mean_frames"]
                        for row in rows
                        if row["seed"] == seed and row["level"] == level
                    )
                    for level in official_levels
                ]
                for seed in seeds
            ],
            dtype=np.float64,
        )

    noop = np.asarray(
        [OFFICIAL_NOOP_MEAN_FRAMES[level] for level in official_levels],
        dtype=np.float64,
    )
    reference = np.maximum(raw_scores["bc"], raw_scores["ppo"]).max(axis=0)
    if np.any(reference <= noop):
        raise ValueError("Official normalization reference must exceed no-op")

    rng = np.random.default_rng(AGGREGATE_BOOTSTRAP_SEED)
    indices = rng.integers(
        0,
        len(seeds),
        size=(BOOTSTRAP_RESAMPLES, len(seeds), len(official_levels)),
    )

    policy_summaries = {}
    for policy, values in raw_scores.items():
        normalized = (values - noop) / (reference - noop)
        bootstrap_scores = np.take_along_axis(
            np.broadcast_to(
                normalized,
                (BOOTSTRAP_RESAMPLES, *normalized.shape),
            ),
            indices,
            axis=1,
        )
        point = aggregate_metrics(normalized)
        bootstrap_metrics = aggregate_metrics(bootstrap_scores)
        low, high = np.percentile(bootstrap_metrics, [2.5, 97.5], axis=0)
        policy_summaries[policy] = {
            "normalized_seed_level_scores": normalized.tolist(),
            "mean": {
                "estimate": float(point[0]),
                "ci95": [float(low[0]), float(high[0])],
            },
            "iqm": {
                "estimate": float(point[1]),
                "ci95": [float(low[1]), float(high[1])],
            },
            "optimality_gap": {
                "estimate": float(point[2]),
                "ci95": [float(low[2]), float(high[2])],
            },
        }

    return {
        "levels": official_levels,
        "seed_order": seeds,
        "normalization": (
            "(seed-level mean frames - fixed no-op mean) / "
            "(best observed retained-policy seed mean - fixed no-op mean)"
        ),
        "no_op_mean_frames": noop.tolist(),
        "empirical_reference_mean_frames": reference.tolist(),
        "bootstrap": (
            "95% percentile intervals from task-stratified resampling of "
            "controller seeds; normalization anchors held fixed"
        ),
        "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
        "bootstrap_seed": AGGREGATE_BOOTSTRAP_SEED,
        "policies": policy_summaries,
    }


def validate_eval(
    payload: dict,
    *,
    policy: str,
    level_name: str,
    controller_hash: str,
    fsq_hash: str,
    transformer_hash: str,
) -> np.ndarray:
    checks = {
        "policy": payload["policy"] == policy,
        "level": payload["level_name"] == level_name,
        "scored attempts": payload["n_runs"] == 25 and len(payload["runs"]) == 25,
        "executed episodes": payload["n_episodes_executed"] == 26,
        "sync exclusion": payload["excluded_initial_episodes"] == 1
        and len(payload["excluded_episodes"]) == 1,
        "controller hash": payload["checkpoint_sha256"]["controller"]
        == controller_hash,
        "FSQ hash": payload["checkpoint_sha256"]["vae"] == fsq_hash,
        "transformer hash": payload["checkpoint_sha256"]["transformer"]
        == transformer_hash,
        "protocol": payload["fps"] == 30
        and payload["inference_path"] == "diagnostic"
        and payload["policy_class"] == "v3_cnn",
        "run numbering": sorted(run["run"] for run in payload["runs"])
        == list(range(1, 26)),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(f"{policy} / {level_name} failed: {', '.join(failed)}")
    return np.asarray(
        [run["frames_survived"] for run in payload["runs"]], dtype=np.float64
    )


def fmt(value: float) -> str:
    return f"{value:.1f}"


def main() -> None:
    rows: list[dict] = []
    training: list[dict] = []
    frozen_hashes: set[tuple[str, str]] = set()

    for seed, paths in SEEDS.items():
        checkpoint_dir = paths["checkpoint_dir"]
        manifest = parse_manifest(checkpoint_dir / "provenance.txt")
        bc_args = json.loads((checkpoint_dir / "controller_bc_args.json").read_text())
        ppo_args = json.loads((checkpoint_dir / "controller_ppo_args.json").read_text())

        expected_parent = f"checkpoints_v7_controller_seed{seed}/controller_bc_best.pt"
        if int(manifest["seed"]) != seed or bc_args["seed"] != seed:
            raise ValueError(f"Seed mismatch in seed {seed} provenance")
        if ppo_args["seed"] != seed or ppo_args["pretrained"] != expected_parent:
            raise ValueError(f"PPO seed {seed} does not identify its retained BC parent")
        if ppo_args["no_pretrained"]:
            raise ValueError(f"PPO seed {seed} disabled BC initialization")

        bc_path = checkpoint_dir / "controller_bc_best.pt"
        ppo_path = checkpoint_dir / "controller_ppo_best.pt"
        if sha256(bc_path) != manifest["bc_sha256"]:
            raise ValueError(f"BC seed {seed} checkpoint hash mismatch")
        if sha256(ppo_path) != manifest["ppo_sha256"]:
            raise ValueError(f"PPO seed {seed} checkpoint hash mismatch")
        if sha256(ROOT / manifest["fsq_checkpoint"]) != manifest["fsq_sha256"]:
            raise ValueError(f"Seed {seed} FSQ hash mismatch")
        if sha256(ROOT / manifest["transformer_checkpoint"]) != manifest["transformer_sha256"]:
            raise ValueError(f"Seed {seed} transformer hash mismatch")

        frozen_hashes.add((manifest["fsq_sha256"], manifest["transformer_sha256"]))
        bc_selection = dict(
            field.split("=", 1) for field in manifest["bc_selection"].split()
        )
        ppo_selection = dict(
            field.split("=", 1) for field in manifest["ppo_selection"].split()
        )
        training.append(
            {
                "seed": seed,
                "code_commit": manifest["code_commit"],
                "bc_selected_epoch": int(bc_selection["epoch"]),
                "bc_val_loss": float(bc_selection["val_loss"]),
                "bc_val_accuracy": float(bc_selection["val_accuracy"]),
                "bc_sha256": manifest["bc_sha256"],
                "ppo_selected_iteration": int(ppo_selection["iteration"]),
                "ppo_eval_survival": float(ppo_selection["eval_survival"]),
                "ppo_iterations_executed": int(manifest["ppo_iterations_executed"]),
                "ppo_sha256": manifest["ppo_sha256"],
                "total_controller_training_wall_seconds": int(
                    manifest["total_controller_training_wall_seconds"]
                ),
            }
        )

        for level_index, (slug, level_name, group) in enumerate(LEVELS):
            attempts = {}
            for policy in ("bc", "ppo"):
                path = paths["eval_dir"] / f"{policy}_{slug}_25.json"
                payload = json.loads(path.read_text(encoding="utf-8"))
                attempts[policy] = validate_eval(
                    payload,
                    policy=policy,
                    level_name=level_name,
                    controller_hash=manifest[f"{policy}_sha256"],
                    fsq_hash=manifest["fsq_sha256"],
                    transformer_hash=manifest["transformer_sha256"],
                )

            bc = attempts["bc"]
            ppo = attempts["ppo"]
            ci_low, ci_high = bootstrap_difference(
                bc, ppo, seed=BOOTSTRAP_SEED + seed * 10 + level_index
            )
            rows.append(
                {
                    "seed": seed,
                    "level": level_name,
                    "level_slug": slug,
                    "group": group,
                    "n_per_policy": 25,
                    "bc_mean_frames": float(bc.mean()),
                    "bc_sample_sd_frames": float(bc.std(ddof=1)),
                    "ppo_mean_frames": float(ppo.mean()),
                    "ppo_sample_sd_frames": float(ppo.std(ddof=1)),
                    "ppo_minus_bc_mean_frames": float(ppo.mean() - bc.mean()),
                    "ppo_minus_bc_ci95_frames": [ci_low, ci_high],
                    "bootstrap_seed": BOOTSTRAP_SEED + seed * 10 + level_index,
                }
            )

    if len(frozen_hashes) != 1:
        raise ValueError("Controller replications do not share one frozen FSQ/world model")

    replication = []
    for _, level_name, group in LEVELS:
        deltas = np.asarray(
            [row["ppo_minus_bc_mean_frames"] for row in rows if row["level"] == level_name]
        )
        replication.append(
            {
                "level": level_name,
                "group": group,
                "seed_differences_frames": deltas.tolist(),
                "mean_seed_difference_frames": float(deltas.mean()),
                "sample_sd_seed_difference_frames": float(deltas.std(ddof=1)),
                "positive_seeds": int((deltas > 0).sum()),
                "n_seeds": int(deltas.size),
            }
        )

    aggregate_summary = seed_level_aggregate_summary(rows)

    fsq_hash, transformer_hash = next(iter(frozen_hashes))
    total_seconds = sum(item["total_controller_training_wall_seconds"] for item in training)
    output = {
        "experiment": "v7_controller_replication",
        "issue": 32,
        "controller_seeds": [43, 44, 45],
        "n_valid_attempts_per_policy_level": 25,
        "total_scored_attempts": 750,
        "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
        "bootstrap_base_seed": BOOTSTRAP_SEED,
        "bootstrap_interpretation": (
            "Attempt-level deployment variability for each frozen PPO/parent-BC pair; "
            "not uncertainty across controller-training seeds."
        ),
        "fsq_sha256": fsq_hash,
        "transformer_sha256": transformer_hash,
        "training": training,
        "total_ppo_iterations_executed": sum(
            item["ppo_iterations_executed"] for item in training
        ),
        "total_controller_training_wall_seconds": total_seconds,
        "total_controller_training_wall_hours": total_seconds / 3600,
        "paired_live_results": rows,
        "replication_summary": replication,
        "official_seed_level_aggregate_summary": aggregate_summary,
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "paired_results.json").write_text(
        json.dumps(output, indent=2) + "\n", encoding="utf-8"
    )

    md = [
        "# Retained V7 BC to PPO controller replications",
        "",
        "This report replaces the historical comparison between PPO and a separately "
        "reconstructed BC checkpoint. Each PPO policy is compared with the exact retained "
        "BC checkpoint that initialized it. The V7 FSQ tokenizer and transformer are fixed "
        "across controller seeds 43, 44, and 45.",
        "",
        "## Protocol",
        "",
        "- 25 valid live attempts per frozen policy and level, plus one excluded synchronization attempt.",
        "- 30 FPS diagnostic inference path, Auto-Retry, memory-delimited episode boundaries.",
        "- Sample standard deviation uses `ddof=1`.",
        f"- Difference intervals use {BOOTSTRAP_RESAMPLES:,} independent attempt-level bootstrap resamples per frozen pair.",
        "- Difference intervals describe deployment variability, not controller-training uncertainty.",
        "- Three paired seed differences are the replication evidence; attempts are not pooled across seeds.",
        "- Official aggregate metrics use seed-level policy means, with no-op mapped to 0 and the best observed retained-policy seed mean on each level mapped to 1.",
        f"- Aggregate 95% intervals use {BOOTSTRAP_RESAMPLES:,} task-stratified bootstrap resamples of controller seeds; normalization anchors remain fixed.",
        "",
        "## Official levels",
        "",
        "| Seed | Level | BC frames | PPO frames | PPO - BC [95% bootstrap CI] |",
        "| ---: | --- | ---: | ---: | ---: |",
    ]
    for row in rows:
        if row["group"] != "official":
            continue
        md.append(
            f"| {row['seed']} | {row['level']} | "
            f"{fmt(row['bc_mean_frames'])} +/- {fmt(row['bc_sample_sd_frames'])} | "
            f"{fmt(row['ppo_mean_frames'])} +/- {fmt(row['ppo_sample_sd_frames'])} | "
            f"{fmt(row['ppo_minus_bc_mean_frames'])} "
            f"[{fmt(row['ppo_minus_bc_ci95_frames'][0])}, {fmt(row['ppo_minus_bc_ci95_frames'][1])}] |"
        )

    md += [
        "",
        "## Auxiliary levels",
        "",
        "| Seed | Level | BC frames | PPO frames | PPO - BC [95% bootstrap CI] |",
        "| ---: | --- | ---: | ---: | ---: |",
    ]
    for row in rows:
        if row["group"] != "auxiliary":
            continue
        md.append(
            f"| {row['seed']} | {row['level']} | "
            f"{fmt(row['bc_mean_frames'])} +/- {fmt(row['bc_sample_sd_frames'])} | "
            f"{fmt(row['ppo_mean_frames'])} +/- {fmt(row['ppo_sample_sd_frames'])} | "
            f"{fmt(row['ppo_minus_bc_mean_frames'])} "
            f"[{fmt(row['ppo_minus_bc_ci95_frames'][0])}, {fmt(row['ppo_minus_bc_ci95_frames'][1])}] |"
        )

    md += [
        "",
        "## Across-seed replication summary",
        "",
        "Values are the mean and sample SD of the three paired seed-level differences, not pooled attempts.",
        "",
        "| Level | Mean PPO - BC across seeds | Positive seeds |",
        "| --- | ---: | ---: |",
    ]
    for item in replication:
        md.append(
            f"| {item['level']} | {fmt(item['mean_seed_difference_frames'])} +/- "
            f"{fmt(item['sample_sd_seed_difference_frames'])} | "
            f"{item['positive_seeds']}/{item['n_seeds']} |"
        )

    md += [
        "",
        "## Official seed-level aggregate metrics",
        "",
        "Scores normalize each official level from its fixed no-op mean (0) to its "
        "best observed retained-policy seed mean (1). Intervals are 95% percentile "
        "intervals from task-stratified bootstrap resampling of controller seeds. "
        "The optimality gap is therefore the shortfall to this empirical reference, "
        "not to true level completion; lower is better.",
        "",
        "| Metric | BC [95% CI] | PPO [95% CI] |",
        "| --- | ---: | ---: |",
    ]
    metric_labels = (
        ("mean", "Mean"),
        ("iqm", "IQM"),
        ("optimality_gap", "Optimality gap"),
    )
    for metric_key, metric_label in metric_labels:
        cells = []
        for policy in ("bc", "ppo"):
            result = aggregate_summary["policies"][policy][metric_key]
            cells.append(
                f"{result['estimate']:.3f} "
                f"[{result['ci95'][0]:.3f}, {result['ci95'][1]:.3f}]"
            )
        md.append(f"| {metric_label} | {cells[0]} | {cells[1]} |")

    md += [
        "",
        "## Training provenance",
        "",
        "| Seed | Selected BC | BC val. loss / accuracy | Selected PPO | Dev. survival | Executed PPO iterations | Total wall time |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in training:
        hours = item["total_controller_training_wall_seconds"] / 3600
        md.append(
            f"| {item['seed']} | epoch {item['bc_selected_epoch']} | "
            f"{item['bc_val_loss']:.6f} / {100 * item['bc_val_accuracy']:.2f}% | "
            f"iteration {item['ppo_selected_iteration']:,} | "
            f"{item['ppo_eval_survival']:.2f}/45 | "
            f"{item['ppo_iterations_executed']:,} | {hours:.2f} h |"
        )
    md += [
        "",
        f"Total controller-training wall time: **{total_seconds / 3600:.2f} hours**.  "
        f"Total PPO iterations executed: **{output['total_ppo_iterations_executed']:,}**.",
        "",
        "Checkpoint hashes and row-level bootstrap seeds are retained in `paired_results.json`.",
    ]
    (OUTPUT_DIR / "SUMMARY.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print(f"Validated {len(rows)} frozen policy pairs and 750 scored attempts")
    print(f"Wrote {OUTPUT_DIR / 'paired_results.json'}")
    print(f"Wrote {OUTPUT_DIR / 'SUMMARY.md'}")


if __name__ == "__main__":
    main()

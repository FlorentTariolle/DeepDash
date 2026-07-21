"""Exclude the manually resumed first episode from saved live evaluations."""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np


BOOTSTRAP_SEED = 20260720


def file_sha256(path):
    if not path:
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_policy_metadata(data):
    changed = False
    if "policy" not in data:
        data["policy"] = "ppo"
        changed = True
    if "checkpoint_sha256" not in data:
        checkpoints = data.get("checkpoints", {})
        data["checkpoint_sha256"] = {
            name: file_sha256(checkpoints.get(name))
            for name in ("vae", "transformer", "controller")
        }
        changed = True
    if "death_detection" not in data:
        data["death_detection"] = "Geometry Dash process memory"
        changed = True
    return changed


def bootstrap_mean_ci(values, n_resamples):
    values = np.asarray(values, dtype=np.float64)
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    means = []
    for start in range(0, n_resamples, 1_000):
        size = min(1_000, n_resamples - start)
        means.append(
            rng.choice(values, size=(size, values.size), replace=True).mean(axis=1)
        )
    return np.percentile(np.concatenate(means), [2.5, 97.5])


def correct(path):
    data = json.loads(path.read_text())
    metadata_changed = ensure_policy_metadata(data)
    if data.get("excluded_initial_episodes", 0):
        if metadata_changed:
            path.write_text(json.dumps(data, indent=2) + "\n")
            print(f"Added policy metadata to corrected file: {path}")
        else:
            print(f"Skipping already-corrected file: {path}")
        return
    if len(data.get("runs", [])) < 2:
        raise ValueError(f"Expected at least two runs in {path}")

    original_runs = data["runs"]
    first = dict(original_runs[0])
    first["episode"] = first.pop("run", 1)
    first["exclusion_reason"] = (
        "Initial synchronization episode; manual game resume can inflate "
        "survival time."
    )

    scored_runs = []
    for run_number, original in enumerate(original_runs[1:], start=1):
        run = dict(original)
        run["episode"] = run.pop("run", run_number + 1)
        run["run"] = run_number
        scored_runs.append(run)

    values = np.asarray(
        [run["frames_survived"] for run in scored_runs], dtype=np.float64
    )
    fps = data["fps"]
    n_resamples = data.get("survival_bootstrap_resamples", 50_000)
    ci_low, ci_high = bootstrap_mean_ci(values, n_resamples)
    p25, p75 = np.percentile(values, [25, 75])

    data.update({
        "n_runs": len(scored_runs),
        "n_episodes_executed": len(original_runs),
        "excluded_initial_episodes": 1,
        "initial_episode_exclusion_reason": (
            "Manual game resume can inflate the first episode."
        ),
        "mean_frames": round(float(values.mean()), 1),
        "mean_frames_ci95": [round(float(ci_low), 1), round(float(ci_high), 1)],
        "std_frames": round(float(values.std()), 1),
        "min_frames": int(values.min()),
        "max_frames": int(values.max()),
        "median_frames": round(float(np.median(values)), 0),
        "p25_frames": round(float(p25), 0),
        "p75_frames": round(float(p75), 0),
        "mean_time_s": round(float(values.mean()) / fps, 2),
        "mean_time_s_ci95": [
            round(float(ci_low) / fps, 2), round(float(ci_high) / fps, 2)
        ],
        "median_time_s": round(float(np.median(values)) / fps, 2),
        "min_time_s": round(float(values.min()) / fps, 2),
        "max_time_s": round(float(values.max()) / fps, 2),
        "p25_time_s": round(float(p25) / fps, 2),
        "p75_time_s": round(float(p75) / fps, 2),
        "survival_bootstrap_resamples": n_resamples,
        "survival_bootstrap_seed": BOOTSTRAP_SEED,
        "excluded_episodes": [first],
        "runs": scored_runs,
    })
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(
        f"Corrected {path}: n={len(scored_runs)}, "
        f"mean={data['mean_frames']} frames, "
        f"CI={data['mean_frames_ci95']}"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", type=Path, nargs="+")
    args = parser.parse_args()
    for path in args.paths:
        correct(path)


if __name__ == "__main__":
    main()

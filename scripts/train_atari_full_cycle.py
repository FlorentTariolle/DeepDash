"""Top-level Atari100K training orchestrator with durable resume state."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atari.replay_buffer import load_metadata
from deepdash.config import load_yaml


def deep_get(cfg, dotted, default=None):
    cur = cfg
    for key in dotted.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def replay_steps(replay_dir: str | Path) -> int:
    return int((load_metadata(replay_dir) or {}).get("total_steps", 0))


def load_state(path: Path, cfg: dict) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "current_cycle": 0,
        "real_env_steps": replay_steps(deep_get(cfg, "atari.replay_dir")),
        "replay_steps": replay_steps(deep_get(cfg, "atari.replay_dir")),
        "completed_phases": [],
        "selected_checkpoints": {},
        "cycles": {},
        "resume_requested": False,
    }


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def command_exists(path: str) -> bool:
    return Path(path).exists()


class Orchestrator:
    def __init__(self, config_path: str, force_phase: str | None = None):
        self.config_path = config_path
        self.cfg = load_yaml(config_path)
        full = self.cfg.get("full_cycle", {})
        default_run_dir = f"runs/atari_{deep_get(self.cfg, 'atari.game', 'Pong').lower()}_h200_full"
        self.run_dir = Path(full.get("run_dir", default_run_dir))
        self.state_path = self.run_dir / "state.json"
        self.state = load_state(self.state_path, self.cfg)
        self.force_phase = force_phase
        self.replay_dir = Path(deep_get(self.cfg, "atari.replay_dir"))
        self.cycle_steps = int(full.get("cycle_steps", deep_get(self.cfg, "atari.cycle_steps", 10000)))
        self.total_budget = int(full.get("total_budget_steps", deep_get(self.cfg, "atari.total_budget_steps", 100000)))
        self.warmup_steps = int(full.get("warmup_steps", deep_get(self.cfg, "atari.warmup_steps", self.cycle_steps)))
        self.final_mode = str(full.get("final_mode", "evaluation_clean"))
        self.skip_real_actor_cycle0 = bool(full.get("skip_real_actor_cycle0", True))
        self.python = sys.executable

    def mark(self, phase: str, extra: dict | None = None):
        steps = replay_steps(self.replay_dir)
        self.state["real_env_steps"] = steps
        self.state["replay_steps"] = steps
        self.state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if phase not in self.state["completed_phases"]:
            self.state["completed_phases"].append(phase)
        if extra:
            self.state.update(extra)
        save_state(self.state_path, self.state)

    def done(self, phase: str) -> bool:
        return self.force_phase != phase and phase in self.state.get("completed_phases", [])

    def run_cmd(self, phase: str, argv: list[str], expected_steps: int | None = None):
        if self.done(phase):
            print(f"skip phase={phase}")
            return
        before = replay_steps(self.replay_dir)
        print(f"phase={phase} before_replay_steps={before}")
        env = os.environ.copy()
        env.setdefault("WANDB_MODE", "disabled")
        env.setdefault("WANDB_SILENT", "true")
        subprocess.run(argv, check=True, env=env)
        after = replay_steps(self.replay_dir)
        if expected_steps is not None and after != expected_steps:
            raise RuntimeError(
                f"{phase} replay step mismatch: expected {expected_steps}, got {after} (before {before})")
        if after > self.total_budget:
            raise RuntimeError(f"training replay exceeded budget: {after} > {self.total_budget}")
        self.mark(phase)

    def phase_overrides(self, section: str) -> list[str]:
        overrides = self.cfg.get("full_cycle", {}).get("phase_overrides", {}).get(section, {})
        args = []
        for key, value in overrides.items():
            cli_key = "--" + key.replace("_", "-")
            if isinstance(value, bool):
                if value:
                    args.append(cli_key)
            else:
                args.extend([cli_key, str(value)])
        return args

    def collect_random(self):
        target = min(self.warmup_steps, self.total_budget)
        current = replay_steps(self.replay_dir)
        if current >= target:
            self.mark("cycle_0_collect_random")
            return
        remaining = target - current
        atari = self.cfg.get("atari", {})
        argv = [
            self.python, "-u", "scripts/collect_atari_episodes.py",
            "--game", str(atari.get("game", "Pong")),
            "--out-dir", str(atari.get("out_dir", "data/atari/Pong")),
            "--storage", "replay",
            "--replay-dir", str(self.replay_dir),
            "--shard-size", str(atari.get("shard_size", 8192)),
            "--max-env-steps", str(remaining),
            "--n-episodes", "100000",
            "--frame-skip", str(atari.get("frame_skip", 4)),
            "--repeat-action-probability", str(atari.get("repeat_action_probability", 0.0)),
            "--seed", str(atari.get("seed", 42)),
        ]
        self.run_cmd("cycle_0_collect_random", argv, expected_steps=target)

    def train_fsq(self, cycle: int, final: bool = False):
        phase = f"{'final_' if final else ''}cycle_{cycle}_fsq"
        ckpt = Path(deep_get(self.cfg, "fsq.checkpoint_dir", "checkpoints")) / "fsq_final.pt"
        argv = [self.python, "-u", "scripts/train_fsq.py", "--config", self.config_path]
        if ckpt.exists():
            argv.extend(["--resume", str(ckpt)])
        argv.extend(self.phase_overrides("fsq"))
        self.run_cmd(phase, argv)
        self.state["selected_checkpoints"]["fsq"] = str(Path(deep_get(self.cfg, "fsq.checkpoint_dir")) / "fsq_best.pt")
        save_state(self.state_path, self.state)

    def train_predictor(self, section: str, cycle: int, final: bool = False):
        phase = f"{'final_' if final else ''}cycle_{cycle}_{section}"
        argv = [self.python, "-u", "scripts/train_atari_predictor.py",
                "--config", self.config_path, "--config-section", section, "--resume"]
        argv.extend(self.phase_overrides(section))
        self.run_cmd(phase, argv)
        self.state["selected_checkpoints"][section] = str(Path(deep_get(self.cfg, f"{section}.checkpoint_dir")) / "predictor_best.pt")
        save_state(self.state_path, self.state)

    def train_dream_actor(self, cycle: int, final: bool = False):
        phase = f"{'final_' if final else ''}cycle_{cycle}_actor_dream"
        argv = [self.python, "-u", "scripts/train_atari_actor_dream.py",
                "--config", self.config_path, "--config-section", "actor_dream", "--resume"]
        argv.extend(self.phase_overrides("actor_dream"))
        self.run_cmd(phase, argv)
        self.state["selected_checkpoints"]["actor_dream"] = str(Path(deep_get(self.cfg, "actor_dream.checkpoint_dir")) / "actor_dream_final.pt")
        save_state(self.state_path, self.state)

    def collect_policy(self, cycle: int):
        target = min((cycle + 1) * self.cycle_steps, self.total_budget)
        current = replay_steps(self.replay_dir)
        if current >= target:
            self.mark(f"cycle_{cycle}_actor_real")
            return
        argv = [self.python, "-u", "scripts/train_atari_actor_real.py",
                "--config", self.config_path, "--config-section", "actor_real",
                "--target-replay-steps", str(target), "--resume"]
        argv.extend(self.phase_overrides("actor_real"))
        self.run_cmd(f"cycle_{cycle}_actor_real", argv, expected_steps=target)

    def evaluate(self):
        phase = "evaluation"
        out = self.run_dir / "evaluation.json"
        argv = [self.python, "-u", "scripts/eval_atari_actor.py",
                "--config", self.config_path, "--output", str(out)]
        argv.extend(self.phase_overrides("evaluation"))
        self.run_cmd(phase, argv, expected_steps=self.total_budget)
        summary = json.loads(out.read_text()) if out.exists() else {}
        summary.update({
            "real_steps": replay_steps(self.replay_dir),
            "replay_steps": replay_steps(self.replay_dir),
            "selected_checkpoints": self.state.get("selected_checkpoints", {}),
        })
        (self.run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        with open(self.run_dir / "summary.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["mean_return", "episode_count", "real_steps", "replay_steps"])
            writer.writeheader()
            writer.writerow({k: summary.get(k) for k in writer.fieldnames})
        self.mark("summary")

    def run(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        save_state(self.state_path, self.state)
        self.collect_random()
        self.train_fsq(0)
        self.train_predictor("predictor", 0)
        self.train_predictor("predictor_sls", 0)
        self.train_dream_actor(0)
        if not self.skip_real_actor_cycle0:
            self.collect_policy(0)
        cycles = max(1, self.total_budget // self.cycle_steps)
        for cycle in range(1, cycles):
            self.state["current_cycle"] = cycle
            save_state(self.state_path, self.state)
            self.collect_policy(cycle)
            self.train_fsq(cycle)
            self.train_predictor("predictor", cycle)
            self.train_predictor("predictor_sls", cycle)
            self.train_dream_actor(cycle)
        if replay_steps(self.replay_dir) != self.total_budget:
            raise RuntimeError(f"final training budget mismatch: replay={replay_steps(self.replay_dir)} budget={self.total_budget}")
        if self.final_mode == "max_performance":
            self.train_fsq(cycles, final=True)
            self.train_predictor("predictor", cycles, final=True)
            self.train_predictor("predictor_sls", cycles, final=True)
            self.train_dream_actor(cycles, final=True)
        elif self.final_mode != "evaluation_clean":
            raise ValueError(f"unknown full_cycle.final_mode={self.final_mode}")
        self.evaluate()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("--force-phase", default=None)
    args = parser.parse_args()
    Orchestrator(args.config, force_phase=args.force_phase).run()


if __name__ == "__main__":
    main()

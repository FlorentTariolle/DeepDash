#!/bin/bash
#SBATCH -J "eval_warm_h200"
#SBATCH -o slurm/logs/eval_realwarmup_compare_mig_%j.out
#SBATCH -e slurm/logs/eval_realwarmup_compare_mig_%j.err
#SBATCH -p ar_h200
#SBATCH --gres=gpu:h200:1
#SBATCH -n 1
#SBATCH --cpus-per-gpu 8
#SBATCH --mem 64G
#SBATCH --time=02:00:00

set -euo pipefail

cd /home/2500001/ftari001/sls-wm || exit 1
export PATH="/soft/AIDL/conda_envs/pytorch210/bin:$HOME/.local/bin:$PATH"
export WANDB_PROJECT=sls-wm-atari
export PYTHONPATH="$HOME/.python3-3.12-torch210/site-packages/lib/python3.12/site-packages:${PYTHONPATH:-}"

CONFIG="configs/atari/atari_pong_h200_realwarmup.yaml"
RUN_DIR="runs/atari_pong_h200_realwarmup_full"
OUT_DIR="outputs/atari/realwarmup_policy_rollouts"
REAL="checkpoints_atari_pong_h200_realwarmup_actor_real/actor_real_final.pt"
DREAM="checkpoints_atari_pong_h200_realwarmup_actor_dream/actor_dream_best_real.pt"

mkdir -p "$RUN_DIR" "$OUT_DIR" slurm/logs

python -u scripts/eval_atari_actor.py --config "$CONFIG" \
  --actor-checkpoint "$REAL" \
  --output "$RUN_DIR/eval_actor_real_final.json"

python -u scripts/eval_atari_actor.py --config "$CONFIG" \
  --actor-checkpoint "$REAL" --stochastic \
  --output "$RUN_DIR/eval_actor_real_final_stochastic.json"

python -u scripts/eval_atari_actor.py --config "$CONFIG" \
  --actor-checkpoint "$DREAM" \
  --output "$RUN_DIR/eval_actor_dream_best_real.json"

python -u scripts/eval_atari_actor.py --config "$CONFIG" \
  --actor-checkpoint "$DREAM" --stochastic \
  --output "$RUN_DIR/eval_actor_dream_best_real_stochastic.json"

python -u scripts/visualize_atari_policy_rollouts.py --config "$CONFIG" \
  --actor-checkpoint "$REAL" --n-episodes 3 --max-steps 3000 --frame-stride 4 \
  --out "$OUT_DIR/actor_real_final_deterministic.html"

python -u scripts/visualize_atari_policy_rollouts.py --config "$CONFIG" \
  --actor-checkpoint "$REAL" --stochastic --n-episodes 3 --max-steps 3000 --frame-stride 4 \
  --out "$OUT_DIR/actor_real_final_stochastic.html"

python -u scripts/visualize_atari_policy_rollouts.py --config "$CONFIG" \
  --actor-checkpoint "$DREAM" --n-episodes 3 --max-steps 3000 --frame-stride 4 \
  --out "$OUT_DIR/actor_dream_best_real_deterministic.html"

python -u scripts/visualize_atari_policy_rollouts.py --config "$CONFIG" \
  --actor-checkpoint "$DREAM" --stochastic --n-episodes 3 --max-steps 3000 --frame-stride 4 \
  --out "$OUT_DIR/actor_dream_best_real_stochastic.html"

python - <<'PY'
import json
from pathlib import Path

run_dir = Path("runs/atari_pong_h200_realwarmup_full")
items = {
    "real_deterministic": run_dir / "eval_actor_real_final.json",
    "real_stochastic": run_dir / "eval_actor_real_final_stochastic.json",
    "dream_deterministic": run_dir / "eval_actor_dream_best_real.json",
    "dream_stochastic": run_dir / "eval_actor_dream_best_real_stochastic.json",
}
summary = {}
for name, path in items.items():
    data = json.loads(path.read_text())
    summary[name] = {
        "mean_return": data.get("mean_return"),
        "returns": data.get("returns"),
        "stochastic": data.get("stochastic"),
        "actor_checkpoint": data.get("actor_checkpoint"),
        "action_counts": data.get("action_counts"),
    }
best = max(summary.items(), key=lambda item: item[1]["mean_return"])
summary["best"] = {"name": best[0], **best[1]}
(run_dir / "eval_policy_compare_summary.json").write_text(json.dumps(summary, indent=2))
print(json.dumps(summary, indent=2))
PY

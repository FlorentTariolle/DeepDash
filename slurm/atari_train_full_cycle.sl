#!/bin/bash
#SBATCH -J "atari_full"
#SBATCH -o slurm/logs/atari_train_full_cycle.out
#SBATCH -e slurm/logs/atari_train_full_cycle.err
#SBATCH -p ar_h200
#SBATCH --gres=gpu:h200:1
#SBATCH -n 1
#SBATCH --cpus-per-gpu 8
#SBATCH --mem 64G
#SBATCH --time=08:00:00
#SBATCH --signal=B:USR1@300

# Run the full Atari100K cycle under one resumable orchestrator.
#
# Submit:
#   sbatch slurm/atari_train_full_cycle.sl configs/atari/atari_pong_h200.yaml

CONFIG=${1:-configs/atari/atari_pong_h200.yaml}
RUN_DIR=$(python -c "import yaml; cfg=yaml.safe_load(open('$CONFIG')) or {}; print(cfg.get('full_cycle',{}).get('run_dir','runs/atari_pong_h200_full'))")
RESUME_FLAG="$RUN_DIR/.resume_full_cycle"

mkdir -p slurm/logs
mkdir -p "$RUN_DIR"

handle_timeout() {
    echo "=== USR1 received ($(date)), resubmitting full-cycle job ==="
    touch "$RESUME_FLAG"
    sbatch "$0" "$CONFIG"
    kill -TERM "$TRAIN_PID" 2>/dev/null
    wait "$TRAIN_PID"
    exit 0
}
trap handle_timeout USR1

export PATH="/soft/AIDL/conda_envs/pytorch210/bin:$HOME/.local/bin:$PATH"
export WANDB_PROJECT=sls-wm-atari
export PYTHONPATH="$HOME/.python3-3.12-torch210/site-packages/lib/python3.12/site-packages:${PYTHONPATH:-}"
pip install --user --upgrade wandb "protobuf>=6.32" "gymnasium[atari,accept-rom-license]>=1.0.0" 2>/dev/null

if [ -f "$RESUME_FLAG" ]; then
    echo "=== Resuming full cycle from $RUN_DIR/state.json ==="
    rm "$RESUME_FLAG"
fi

echo "=== Config: $CONFIG ==="
echo "=== Run dir: $RUN_DIR ==="

python -u scripts/train_atari_full_cycle.py "$CONFIG" &

TRAIN_PID=$!
wait "$TRAIN_PID"

#!/bin/bash
#SBATCH -J "atari_fsq"
#SBATCH -o slurm/logs/atari_train_fsq.out
#SBATCH -e slurm/logs/atari_train_fsq.err
#SBATCH -p ar_h200
#SBATCH --gres=gpu:h200:1
#SBATCH -n 1
#SBATCH --cpus-per-gpu 8
#SBATCH --mem 64G
#SBATCH --time=08:00:00
#SBATCH --signal=B:USR1@300

# Train Atari FSQ from replay, with 8h auto-resubmit.
#
# Submit: sbatch slurm/atari_train_fsq.sl [config]

CONFIG=${1:-configs/atari/atari_pong_h200.yaml}
CKPT_DIR=$(python -c "import yaml; print(yaml.safe_load(open('$CONFIG')).get('fsq',{}).get('checkpoint_dir','checkpoints_atari'))")
RESUME_FLAG="$CKPT_DIR/.resume_fsq"

mkdir -p slurm/logs

handle_timeout() {
    echo "=== USR1 received ($(date)), resubmitting FSQ job ==="
    mkdir -p "$CKPT_DIR"
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

RESUME_ARG=""
if [ -f "$RESUME_FLAG" ]; then
    RESUME_ARG="--resume $CKPT_DIR/fsq_final.pt"
    rm "$RESUME_FLAG"
    echo "=== Resuming FSQ from $CKPT_DIR/fsq_final.pt ==="
fi

echo "=== Config: $CONFIG ==="
echo "=== Checkpoint dir: $CKPT_DIR ==="

python -u scripts/train_fsq.py \
    --config "$CONFIG" \
    $RESUME_ARG &

TRAIN_PID=$!
wait "$TRAIN_PID"

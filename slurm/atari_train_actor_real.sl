#!/bin/bash
#SBATCH -J "atari_actor"
#SBATCH -o slurm/logs/atari_train_actor_real.out
#SBATCH -e slurm/logs/atari_train_actor_real.err
#SBATCH -p ar_h200
#SBATCH --gres=gpu:h200:1
#SBATCH -n 1
#SBATCH --cpus-per-gpu 8
#SBATCH --mem 64G
#SBATCH --time=08:00:00
#SBATCH --signal=B:USR1@300

# Train the Atari actor on real env interactions and append them to replay.
#
# Submit: sbatch slurm/atari_train_actor_real.sl [config]

CONFIG=${1:-configs/atari/atari_pong_h200.yaml}
SECTION=${2:-actor_real}
CKPT_DIR=$(python -c "import yaml; print(yaml.safe_load(open('$CONFIG')).get('$SECTION',{}).get('checkpoint_dir','checkpoints_atari_actor_real'))")
RESUME_FLAG="$CKPT_DIR/.resume_actor_real"

mkdir -p slurm/logs

handle_timeout() {
    echo "=== USR1 received ($(date)), resubmitting real-actor job ==="
    mkdir -p "$CKPT_DIR"
    touch "$RESUME_FLAG"
    sbatch "$0" "$CONFIG" "$SECTION"
    kill -TERM "$TRAIN_PID" 2>/dev/null
    wait "$TRAIN_PID"
    exit 0
}
trap handle_timeout USR1

module purge
module load aidl/pytorch/2.10.0-py3.12-cuda12.6
export PATH="$HOME/.local/bin:$PATH"
pip install --user --upgrade wandb "protobuf>=6.32" 2>/dev/null

RESUME_ARG=""
if [ -f "$RESUME_FLAG" ]; then
    RESUME_ARG="--resume"
    rm "$RESUME_FLAG"
    echo "=== Resuming actor from $CKPT_DIR/actor_real_latest.pt ==="
fi

echo "=== Config: $CONFIG ==="
echo "=== Section: $SECTION ==="
echo "=== Checkpoint dir: $CKPT_DIR ==="

python -u scripts/train_atari_actor_real.py \
    --config "$CONFIG" \
    --config-section "$SECTION" \
    $RESUME_ARG &

TRAIN_PID=$!
wait "$TRAIN_PID"

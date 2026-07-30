#!/bin/bash
#SBATCH -J "train_wm"
#SBATCH -o slurm/logs/train_world_model.out
#SBATCH -e slurm/logs/train_world_model.err
#SBATCH -p ar_h200
#SBATCH --gres=gpu:h200:1
#SBATCH -n 1
#SBATCH --cpus-per-gpu 8
#SBATCH --mem 64G
#SBATCH --time=08:00:00
#SBATCH --signal=B:USR1@300

# Joint FSQ + Transformer training on H200 with USR1 auto-resume.
#
# Submit:  sbatch slurm/train_world_model.sl [config] [training overrides...]
# Example: sbatch slurm/train_world_model.sl configs/deepdash/e6.11-gaussian-cpc.yaml
# Ablation example:
#   sbatch slurm/train_world_model.sl configs/deepdash/v7-phase0.yaml \
#     --checkpoint-dir checkpoints_v7_ablation_no_sls \
#     --label-smoothing 0
# Monitor: tail -f slurm/logs/train_world_model.out

CONFIG=configs/deepdash/e6.10-gaussian-single-group.yaml
if [[ $# -gt 0 && $1 != -* ]]; then
    CONFIG=$1
    shift
fi
EXTRA_ARGS=("$@")

# Resolve checkpoint_dir from a command-line override first, then the config.
CKPT_DIR=""
for ((i = 0; i < ${#EXTRA_ARGS[@]}; i++)); do
    case "${EXTRA_ARGS[$i]}" in
        --checkpoint-dir)
            if ((i + 1 >= ${#EXTRA_ARGS[@]})); then
                echo "--checkpoint-dir requires a value" >&2
                exit 2
            fi
            CKPT_DIR=${EXTRA_ARGS[$((i + 1))]}
            ;;
        --checkpoint-dir=*)
            CKPT_DIR=${EXTRA_ARGS[$i]#--checkpoint-dir=}
            ;;
    esac
done
if [[ -z "$CKPT_DIR" ]]; then
    CKPT_DIR=$(python -c "import yaml; print(yaml.safe_load(open('$CONFIG')).get('transformer',{}).get('checkpoint_dir','checkpoints'))")
fi

echo "=== Config: $CONFIG ==="
echo "=== Checkpoint dir: $CKPT_DIR ==="
printf '=== Overrides:'
printf ' %q' "${EXTRA_ARGS[@]}"
printf ' ===\n'

# Auto-resume: the trap creates a sentinel file before requeuing.
RESUME_FLAG="$CKPT_DIR/.resume_transformer"

handle_timeout() {
    echo "=== USR1 received ($(date)), saving and resubmitting ==="
    # Persist resume intent BEFORE waiting for the child: if Python's save
    # overruns the grace period and we get hard-killed, a fresh submit will
    # still pick up --resume from the flag.
    mkdir -p "$CKPT_DIR"
    touch "$RESUME_FLAG"
    # Queue the resubmit immediately and unconditionally. With the cluster's
    # 1-job-at-a-time policy, the new job sits in the queue until this one
    # finishes saving — no race, no scontrol-requeue silent-failure path.
    echo "=== Submitting resume job ==="
    sbatch "$0" "$CONFIG" "${EXTRA_ARGS[@]}"
    kill -TERM "$TRAIN_PID" 2>/dev/null
    wait "$TRAIN_PID"
    exit 0
}
trap handle_timeout USR1

export PATH="/soft/AIDL/conda_envs/pytorch210/bin:$HOME/.local/bin:$PATH"
export WANDB_PROJECT=sls-wm-atari
export PYTHONPATH="$HOME/.python3-3.12-torch210/site-packages/lib/python3.12/site-packages:${PYTHONPATH:-}"
# wandb bundles gencode that expects protobuf >= 6.32; the torch 2.10
# module ships protobuf 6.31, so upgrade it here on the compute node.
pip install --user --upgrade wandb "protobuf>=6.32" 2>/dev/null

RESUME_ARG=""
if [ -f "$RESUME_FLAG" ]; then
    RESUME_ARG="--resume"
    rm "$RESUME_FLAG"
    echo "=== Resuming from checkpoint ==="
fi

echo "=== Train world model ($(date)) ==="
python -u scripts/train_world_model.py \
    --config "$CONFIG" \
    "${EXTRA_ARGS[@]}" \
    $RESUME_ARG &

TRAIN_PID=$!
wait "$TRAIN_PID"

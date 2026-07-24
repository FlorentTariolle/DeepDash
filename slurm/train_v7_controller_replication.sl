#!/bin/bash
#SBATCH -J "v7_ctrl_rep"
#SBATCH -o slurm/logs/v7_controller_replication_%j.out
#SBATCH -e slurm/logs/v7_controller_replication_%j.err
#SBATCH -p ar_h200
#SBATCH --gres=gpu:h200:1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=08:00:00
#SBATCH --signal=B:USR1@300

# Train one retained V7 BC -> PPO controller pair for issue #32.
#
# Usage:
#   sbatch --job-name=v7_ctrl_s43 \
#     slurm/train_v7_controller_replication.sl 43
#
# The frozen V7 FSQ and transformer remain in checkpoints_v7. Controller
# artifacts are written to a seed-specific directory and cannot overwrite the
# historical V7 controller checkpoints.

set -Eeuo pipefail

SEED=${1:-43}
RUN_DIR=${2:-checkpoints_v7_controller_seed${SEED}}
CONFIG=configs/deepdash/v7-phase0.yaml
FSQ_CHECKPOINT=checkpoints_v7/fsq_best.pt
TRANSFORMER_CHECKPOINT=checkpoints_v7/transformer_best.pt
# Safety ceiling only. Inspect the fixed-development survival curve after at
# least 5,000 iterations and stop at its elbow before this limit if warranted.
PPO_ITERATIONS=15000

if [[ ! "$SEED" =~ ^[0-9]+$ ]]; then
    echo "Seed must be a non-negative integer, got: $SEED" >&2
    exit 2
fi

REPO_ROOT=${SLURM_SUBMIT_DIR:?Submit this job from the DashVMC repository root}
cd "$REPO_ROOT"
SUBMIT_SCRIPT="$REPO_ROOT/slurm/train_v7_controller_replication.sl"

mkdir -p "$RUN_DIR"

export PATH="/soft/AIDL/conda_envs/pytorch210/bin:$HOME/.local/bin:$PATH"
export PYTHONPATH="$HOME/.python3-3.12-torch210/site-packages/lib/python3.12/site-packages:${PYTHONPATH:-}"
export PYTHONUNBUFFERED=1
export WANDB_MODE=offline
export WANDB_PROJECT=dashvmc-controller-replications

RUN_START_FILE="$RUN_DIR/run_started_epoch.txt"
MANIFEST="$RUN_DIR/provenance.txt"
JOB_HISTORY="$RUN_DIR/job_history.tsv"
BC_DONE="$RUN_DIR/.bc_complete"
PPO_DONE="$RUN_DIR/.ppo_complete"

if [[ ! -f "$RUN_START_FILE" ]]; then
    date +%s > "$RUN_START_FILE"
fi

if [[ ! -f "$MANIFEST" ]]; then
    {
        echo "experiment=v7_controller_replication"
        echo "issue=32"
        echo "seed=$SEED"
        echo "eval_seed=42"
        echo "precision=bfloat16"
        echo "config=$CONFIG"
        echo "ppo_iteration_ceiling=$PPO_ITERATIONS"
        echo "ppo_minimum_iterations=5000"
        echo "ppo_stopping_rule=fixed-development survival elbow"
        echo "code_commit=${DASHVMC_CODE_COMMIT:-unknown}"
        echo "fsq_checkpoint=$FSQ_CHECKPOINT"
        echo "fsq_sha256=$(sha256sum "$FSQ_CHECKPOINT" | awk '{print $1}')"
        echo "transformer_checkpoint=$TRANSFORMER_CHECKPOINT"
        echo "transformer_sha256=$(sha256sum "$TRANSFORMER_CHECKPOINT" | awk '{print $1}')"
        echo "started_at=$(date --iso-8601=seconds)"
    } > "$MANIFEST"
fi

if ! grep -q "^ppo_iteration_ceiling=$PPO_ITERATIONS$" "$MANIFEST"; then
    {
        echo "protocol_updated_at=$(date --iso-8601=seconds)"
        echo "ppo_iteration_ceiling=$PPO_ITERATIONS"
        echo "ppo_minimum_iterations=5000"
        echo "ppo_stopping_rule=fixed-development survival elbow"
    } >> "$MANIFEST"
fi

printf "%s\t%s\t%s\t%s\n" \
    "$SLURM_JOB_ID" "$(date --iso-8601=seconds)" "$(hostname)" "started" \
    >> "$JOB_HISTORY"

python - <<'PY'
import torch

if not torch.cuda.is_available():
    raise SystemExit("CUDA is not available inside the H200 job")

name = torch.cuda.get_device_name(0)
if "H200" not in name:
    raise SystemExit(f"Expected an H200, got {name}")
if not torch.cuda.is_bf16_supported():
    raise SystemExit(f"BF16 is not supported by {name}")

print(f"GPU: {name}")
print(f"PyTorch: {torch.__version__}")
print("Precision: BF16 autocast with FP32 parameters/optimizer state")
PY

TRAIN_PID=

resubmit_for_resume() {
    trap - USR1
    echo "$(date --iso-8601=seconds): received pre-timeout USR1"
    printf "%s\t%s\t%s\t%s\n" \
        "$SLURM_JOB_ID" "$(date --iso-8601=seconds)" "$(hostname)" \
        "pre-timeout-resubmit" >> "$JOB_HISTORY"

    if [[ -n "${TRAIN_PID:-}" ]] && kill -0 "$TRAIN_PID" 2>/dev/null; then
        kill -TERM "$TRAIN_PID" 2>/dev/null || true
        wait "$TRAIN_PID" 2>/dev/null || true
    fi

    NEXT_JOB=$(sbatch --parsable \
        --dependency="afterany:${SLURM_JOB_ID}" \
        --job-name="v7_ctrl_s${SEED}" \
        "$SUBMIT_SCRIPT" "$SEED" "$RUN_DIR")
    echo "$(date --iso-8601=seconds): submitted continuation job $NEXT_JOB"
    exit 0
}
trap resubmit_for_resume USR1

run_training() {
    "$@" &
    TRAIN_PID=$!
    if wait "$TRAIN_PID"; then
        TRAIN_PID=
        return 0
    else
        STATUS=$?
        TRAIN_PID=
        return "$STATUS"
    fi
}

if [[ ! -f "$BC_DONE" ]]; then
    echo "=== Phase 1: V7 behavioral cloning, seed $SEED ==="
    BC_START=$(date +%s)
    run_training python -u scripts/train_controller_bc.py \
        --config "$CONFIG" \
        --fsq-checkpoint "$FSQ_CHECKPOINT" \
        --transformer-checkpoint "$TRANSFORMER_CHECKPOINT" \
        --checkpoint-dir "$RUN_DIR" \
        --seed "$SEED" \
        --amp-dtype bfloat16

    BC_SECONDS=$(($(date +%s) - BC_START))
    BC_SELECTION=$(awk -F, '
        NR > 1 && (!seen || ($4 + 0) < best) {
            seen = 1
            best = $4 + 0
            epoch = $1
            accuracy = $5
        }
        END {
            if (seen) {
                printf "epoch=%s val_loss=%.6f val_accuracy=%s",
                       epoch, best, accuracy
            }
        }
    ' "$RUN_DIR/controller_bc_log.csv")

    {
        echo "bc_completed_at=$(date --iso-8601=seconds)"
        echo "bc_wall_seconds=$BC_SECONDS"
        echo "bc_selection=$BC_SELECTION"
        echo "bc_checkpoint=$RUN_DIR/controller_bc_best.pt"
        echo "bc_sha256=$(sha256sum "$RUN_DIR/controller_bc_best.pt" | awk '{print $1}')"
    } >> "$MANIFEST"
    touch "$BC_DONE"
else
    echo "=== Phase 1 already complete; retaining existing BC checkpoint ==="
fi

if [[ ! -f "$PPO_DONE" ]]; then
    echo "=== Phase 2: V7 PPO, seed $SEED ==="
    PPO_ARGS=()
    if [[ -f "$RUN_DIR/controller_ppo_latest.pt" ]]; then
        PPO_ARGS+=(--resume)
    fi

    run_training python -u scripts/train_controller_ppo.py \
        --config "$CONFIG" \
        --fsq-checkpoint "$FSQ_CHECKPOINT" \
        --transformer-checkpoint "$TRANSFORMER_CHECKPOINT" \
        --pretrained "$RUN_DIR/controller_bc_best.pt" \
        --checkpoint-dir "$RUN_DIR" \
        --n-iterations "$PPO_ITERATIONS" \
        --seed "$SEED" \
        --eval-seed 42 \
        --wandb-name "v7-seed${SEED}-ppo" \
        "${PPO_ARGS[@]}"

    PPO_SELECTION=$(awk -F, '
        NR > 1 && $8 != "" && (!seen || ($8 + 0) > best) {
            seen = 1
            best = $8 + 0
            iteration = $1
        }
        END {
            if (seen) {
                printf "iteration=%s eval_survival=%.2f", iteration, best
            }
        }
    ' "$RUN_DIR/controller_ppo_log.csv")
    PPO_EXECUTED=$(tail -n 1 "$RUN_DIR/controller_ppo_log.csv" | cut -d, -f1)
    TOTAL_SECONDS=$(($(date +%s) - $(<"$RUN_START_FILE")))

    {
        echo "ppo_completed_at=$(date --iso-8601=seconds)"
        echo "ppo_iterations_executed=$PPO_EXECUTED"
        echo "ppo_selection=$PPO_SELECTION"
        echo "ppo_checkpoint=$RUN_DIR/controller_ppo_best.pt"
        echo "ppo_sha256=$(sha256sum "$RUN_DIR/controller_ppo_best.pt" | awk '{print $1}')"
        echo "total_controller_training_wall_seconds=$TOTAL_SECONDS"
    } >> "$MANIFEST"
    touch "$PPO_DONE"
else
    echo "=== Phase 2 already complete ==="
fi

printf "%s\t%s\t%s\t%s\n" \
    "$SLURM_JOB_ID" "$(date --iso-8601=seconds)" "$(hostname)" "completed" \
    >> "$JOB_HISTORY"
echo "=== V7 controller replication seed $SEED complete ==="

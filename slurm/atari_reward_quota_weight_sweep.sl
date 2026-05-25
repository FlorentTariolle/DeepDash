#!/bin/bash
#SBATCH -J "atari_rq_w"
#SBATCH -o slurm/logs/atari_reward_quota_weight_sweep_%j.out
#SBATCH -e slurm/logs/atari_reward_quota_weight_sweep_%j.err
#SBATCH -p ar_h200
#SBATCH --gres=gpu:h200:1
#SBATCH -n 1
#SBATCH --cpus-per-gpu 8
#SBATCH --mem 64G
#SBATCH --time=02:30:00

CONFIG=${1:-configs/atari/atari_pong_h200_dreamer_smoke.yaml}
WEIGHTS=${2:-"0.15 0.25 0.35"}
INIT_ACTOR=${3:-checkpoints_atari_pong_h200_controller_sanity_bc_heuristic_30k/actor_bc_final.pt}

mkdir -p slurm/logs

export PATH="/soft/AIDL/conda_envs/pytorch210/bin:$HOME/.local/bin:$PATH"
export WANDB_PROJECT=sls-wm-atari
export PYTHONPATH="$HOME/.python3-3.12-torch210/site-packages/lib/python3.12/site-packages:${PYTHONPATH:-}"
export PYTORCH_ALLOC_CONF=expandable_segments:True

echo "=== Reward-quota neutral-weight sweep ==="
echo "=== Config: $CONFIG ==="
echo "=== Weights: $WEIGHTS ==="
echo "=== Init actor: $INIT_ACTOR ==="
echo "=== CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES ==="
echo "=== Python: $(which python) ==="
python - <<'PY'
import sys, torch
print('python_version', sys.version)
print('torch_version', torch.__version__)
print('cuda_available', torch.cuda.is_available())
if torch.cuda.is_available():
    print('cuda_device', torch.cuda.get_device_name(0))
PY

for WEIGHT in $WEIGHTS; do
  SAFE_WEIGHT=${WEIGHT//./p}
  PRED_CKPT="checkpoints_atari_pong_h200_dreamer_smoke_predictor_sls_reward_quota_zew_${SAFE_WEIGHT}"
  ACTOR_CKPT="checkpoints_atari_pong_h200_dreamer_smoke_actor_dream_reward_quota_zew_${SAFE_WEIGHT}_probe"
  echo ""
  echo "=== neutral event weight: $WEIGHT ==="
  echo "=== Predictor checkpoint dir: $PRED_CKPT ==="
  echo "=== Actor checkpoint dir: $ACTOR_CKPT ==="

  python -u scripts/train_atari_predictor.py \
    --config "$CONFIG" \
    --config-section predictor_sls_rollout_consistency \
    --checkpoint-dir "$PRED_CKPT" \
    --rollout-consistency-event-zero-weight "$WEIGHT" \
    --wandb-name "predictor-reward-quota-zew-${SAFE_WEIGHT}-${SLURM_JOB_ID}" \
    --compile-mode reduce-overhead \
    --amp-dtype bfloat16

  python -u scripts/train_atari_actor_dream.py \
    --config "$CONFIG" \
    --config-section actor_dream \
    --predictor-checkpoint "$PRED_CKPT/predictor_best.pt" \
    --checkpoint-dir "$ACTOR_CKPT" \
    --init-from "$INIT_ACTOR" \
    --actor-update iris_pg \
    --policy-action-subset 4,5 \
    --n-iterations 1 \
    --reward-calibration-samples 512 \
    --dream-gate \
    --dream-gate-real-eval-episodes 0 \
    --wandb-name "actor-dream-reward-quota-zew-${SAFE_WEIGHT}-${SLURM_JOB_ID}" \
    --compile-mode none \
    --amp-dtype bfloat16
done

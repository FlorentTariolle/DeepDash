#!/bin/bash
#SBATCH -J "atari_rq_smoke"
#SBATCH -o slurm/logs/atari_reward_quota_smoke_%j.out
#SBATCH -e slurm/logs/atari_reward_quota_smoke_%j.err
#SBATCH -p ar_h200
#SBATCH --gres=gpu:h200:1
#SBATCH -n 1
#SBATCH --cpus-per-gpu 8
#SBATCH --mem 64G
#SBATCH --time=04:00:00

CONFIG=${1:-configs/atari/atari_pong_h200_dreamer_smoke.yaml}
PRED_SECTION=${2:-predictor_sls_rollout_consistency}
PRED_CKPT=${3:-checkpoints_atari_pong_h200_dreamer_smoke_predictor_sls_reward_quota}
ACTOR_CKPT=${4:-checkpoints_atari_pong_h200_dreamer_smoke_actor_dream_reward_quota_probe}
INIT_ACTOR=${5:-checkpoints_atari_pong_h200_controller_sanity_bc_heuristic_30k/actor_bc_final.pt}

mkdir -p slurm/logs

export PATH="/soft/AIDL/conda_envs/pytorch210/bin:$HOME/.local/bin:$PATH"
export WANDB_PROJECT=sls-wm-atari
export PYTHONPATH="$HOME/.python3-3.12-torch210/site-packages/lib/python3.12/site-packages:${PYTHONPATH:-}"
export PYTORCH_ALLOC_CONF=expandable_segments:True

echo "=== Reward-quota smoke ==="
echo "=== Config: $CONFIG ==="
echo "=== Predictor section: $PRED_SECTION ==="
echo "=== Predictor checkpoint dir: $PRED_CKPT ==="
echo "=== Actor checkpoint dir: $ACTOR_CKPT ==="
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

python -u scripts/train_atari_predictor.py \
  --config "$CONFIG" \
  --config-section "$PRED_SECTION" \
  --checkpoint-dir "$PRED_CKPT" \
  --wandb-name "predictor-reward-quota-${SLURM_JOB_ID}" \
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
  --n-iterations 25 \
  --real-eval-interval 5 \
  --real-eval-episodes 5 \
  --reward-calibration-samples 512 \
  --dream-gate \
  --dream-gate-real-eval-episodes 1 \
  --wandb-name "actor-dream-reward-quota-probe-${SLURM_JOB_ID}" \
  --compile-mode reduce-overhead \
  --amp-dtype bfloat16

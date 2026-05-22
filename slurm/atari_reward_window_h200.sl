#!/bin/bash
#SBATCH -J "atari_rw_h200"
#SBATCH -o slurm/logs/atari_reward_window_h200_%j.out
#SBATCH -e slurm/logs/atari_reward_window_h200_%j.err
#SBATCH -p ar_h200
#SBATCH --gres=gpu:h200:1
#SBATCH -n 1
#SBATCH --cpus-per-gpu 8
#SBATCH --mem 64G
#SBATCH --time=08:00:00

CONFIG=${1:-configs/atari/atari_pong_h200_realwarmup.yaml}
SECTION=${2:-actor_dream}
OUT_DIR=${3:-checkpoints_atari_pong_h200_realwarmup_actor_dream_reward_window_h200}
INIT_FROM=${4:-checkpoints_atari_pong_h200_realwarmup_actor_real/actor_real_final.pt}
REAL_EVAL_INTERVAL=${5:-50}
REAL_EVAL_EPISODES=${6:-5}

mkdir -p slurm/logs

export PATH="/soft/AIDL/conda_envs/pytorch210/bin:$HOME/.local/bin:$PATH"
export WANDB_PROJECT=sls-wm-atari
export PYTHONPATH="$HOME/.python3-3.12-torch210/site-packages/lib/python3.12/site-packages:$PYTHONPATH"
export PYTORCH_ALLOC_CONF=expandable_segments:True

echo "=== Config: $CONFIG ==="
echo "=== Section: $SECTION ==="
echo "=== Output checkpoint dir: $OUT_DIR ==="
echo "=== Init actor: $INIT_FROM ==="
echo "=== Real eval: every $REAL_EVAL_INTERVAL iterations, episodes=$REAL_EVAL_EPISODES ==="
echo "=== CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES ==="
echo "=== Python: $(which python) ==="
python - <<'PY'
import sys, torch
print('python_version', sys.version)
print('torch_version', torch.__version__)
PY

python -u scripts/train_atari_actor_dream.py \
  --config "$CONFIG" \
  --config-section "$SECTION" \
  --checkpoint-dir "$OUT_DIR" \
  --init-from "$INIT_FROM" \
  --real-eval-interval "$REAL_EVAL_INTERVAL" \
  --real-eval-episodes "$REAL_EVAL_EPISODES"

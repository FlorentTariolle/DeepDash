#!/bin/bash
#SBATCH -J "atari_bestreal"
#SBATCH -o slurm/logs/atari_train_actor_dream_bestreal.out
#SBATCH -e slurm/logs/atari_train_actor_dream_bestreal.err
#SBATCH -p ar_h200
#SBATCH --gres=gpu:h200:1
#SBATCH -n 1
#SBATCH --cpus-per-gpu 8
#SBATCH --mem 64G
#SBATCH --time=08:00:00

# Rerun dream PPO from an existing actor checkpoint while selecting the best
# checkpoint by periodic deterministic real-env evaluation.
#
# Submit:
#   sbatch slurm/atari_train_actor_dream_bestreal.sl

CONFIG=${1:-configs/atari/atari_pong_h200.yaml}
SECTION=${2:-actor_dream}
OUT_DIR=${3:-checkpoints_atari_pong_h200_actor_dream_bestreal}
INIT_FROM=${4:-checkpoints_atari_pong_h200_actor_dream/actor_dream_final.pt}
REAL_EVAL_INTERVAL=${5:-100}
REAL_EVAL_EPISODES=${6:-5}

mkdir -p slurm/logs

export PATH="/soft/AIDL/conda_envs/pytorch210/bin:$HOME/.local/bin:$PATH"
export WANDB_PROJECT=sls-wm-atari
export PYTHONPATH="$HOME/.python3-3.12-torch210/site-packages/lib/python3.12/site-packages:${PYTHONPATH:-}"

echo "=== Config: $CONFIG ==="
echo "=== Section: $SECTION ==="
echo "=== Output checkpoint dir: $OUT_DIR ==="
echo "=== Init actor: $INIT_FROM ==="
echo "=== Real eval: every $REAL_EVAL_INTERVAL iterations, episodes=$REAL_EVAL_EPISODES ==="

python -u scripts/train_atari_actor_dream.py \
    --config "$CONFIG" \
    --config-section "$SECTION" \
    --checkpoint-dir "$OUT_DIR" \
    --init-from "$INIT_FROM" \
    --real-eval-interval "$REAL_EVAL_INTERVAL" \
    --real-eval-episodes "$REAL_EVAL_EPISODES"

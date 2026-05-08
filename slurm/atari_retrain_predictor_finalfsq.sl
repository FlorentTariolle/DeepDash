#!/bin/bash
#SBATCH -J "pred_finalfsq"
#SBATCH -o slurm/logs/atari_retrain_predictor_finalfsq.out
#SBATCH -e slurm/logs/atari_retrain_predictor_finalfsq.err
#SBATCH -p ar_h200
#SBATCH --gres=gpu:h200:1
#SBATCH -n 1
#SBATCH --cpus-per-gpu 8
#SBATCH --mem 64G
#SBATCH --time=04:00:00

# Retrain the Atari SLS predictor from scratch against the current final FSQ
# tokenizer and replay. This intentionally writes to a fresh checkpoint
# directory so it cannot resume the stale predictor trained on an older FSQ.

module purge
module load aidl/pytorch/2.10.0-py3.12-cuda12.6

export WANDB_MODE=disabled
export WANDB_SILENT=true
export PYTORCH_ALLOC_CONF=expandable_segments:True

mkdir -p slurm/logs

python -u scripts/train_atari_predictor.py \
  --config configs/atari/atari_pong_h200.yaml \
  --config-section predictor_sls \
  --checkpoint-dir checkpoints_atari_pong_h200_predictor_sls_finalfsq \
  --epochs 100 \
  --batch-size 128 \
  --compile-mode reduce-overhead

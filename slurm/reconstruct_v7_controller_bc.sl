#!/bin/bash
#SBATCH -J "reconstruct_v7_bc"
#SBATCH -o slurm/logs/reconstruct_v7_controller_bc_%j.out
#SBATCH -e slurm/logs/reconstruct_v7_controller_bc_%j.err
#SBATCH -p ar_a100
#SBATCH --gres=gpu:a100:1
#SBATCH -n 1
#SBATCH --cpus-per-gpu 8
#SBATCH --mem 64G
#SBATCH --time=01:00:00

# Reconstruct the intermediate BC checkpoint from the exact V7-tag code.
# Usage: sbatch slurm/reconstruct_v7_controller_bc.sl [V7_WORKTREE]

set -euo pipefail

WORKTREE=${1:-$HOME/dash-vmc-v7}
cd "$WORKTREE"

export PATH="/soft/AIDL/conda_envs/pytorch210/bin:$HOME/.local/bin:$PATH"
export PYTHONPATH="$HOME/.python3-3.12-torch210/site-packages/lib/python3.12/site-packages:${PYTHONPATH:-}"
export WANDB_MODE=offline

python -u scripts/train_controller_bc.py \
    --config configs/v7-phase0.yaml \
    --episodes-dir data/death_episodes \
    --expert-episodes-dir data/expert_episodes \
    --fsq-checkpoint checkpoints_v7/fsq_best.pt \
    --seed 42 \
    --snapshot-epochs 9

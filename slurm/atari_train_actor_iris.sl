#!/bin/bash
#SBATCH -J "atari_iris"
#SBATCH -o slurm/logs/atari_train_actor_iris_%j.out
#SBATCH -e slurm/logs/atari_train_actor_iris_%j.err
#SBATCH -p ar_h200
#SBATCH --gres=gpu:h200:1
#SBATCH -n 1
#SBATCH --cpus-per-gpu 8
#SBATCH --mem 64G
#SBATCH --time=08:00:00

# Train the IRIS-style recurrent observation actor on an existing frozen
# tokenizer/world model checkpoint and replay buffer.
#
# Submit:
#   sbatch slurm/atari_train_actor_iris.sl configs/atari/atari_pong_h200_realwarmup.yaml

CONFIG=${1:-configs/atari/atari_pong_h200_realwarmup.yaml}

mkdir -p slurm/logs

export PATH="/soft/AIDL/conda_envs/pytorch210/bin:$HOME/.local/bin:$PATH"
export WANDB_PROJECT=sls-wm-atari
export PYTHONPATH="$HOME/.python3-3.12-torch210/site-packages/lib/python3.12/site-packages:${PYTHONPATH:-}"
pip install --user --upgrade wandb "protobuf>=6.32" "gymnasium[atari,accept-rom-license]>=1.0.0" 2>/dev/null

echo "=== Config: $CONFIG ==="
python -u scripts/train_atari_actor_iris.py --config "$CONFIG" --config-section actor_iris

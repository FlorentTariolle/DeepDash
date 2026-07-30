#!/bin/bash
#SBATCH -J "tokenize"
#SBATCH -o slurm/logs/tokenize.out
#SBATCH -e slurm/logs/tokenize.err
#SBATCH -p ar_h200
#SBATCH --gres=gpu:h200:1
#SBATCH -n 1
#SBATCH --cpus-per-gpu 8
#SBATCH --mem 64G
#SBATCH --time=04:00:00

# V7 Phase 0: tokenize episodes with frozen FSQ + vertical shift augmentation
# for the transformer training stage.
#
# Submit:  sbatch slurm/tokenize_episodes.sl [config]
# Default config: configs/deepdash/v7-phase0.yaml

CONFIG=${1:-configs/deepdash/v7-phase0.yaml}

CKPT_DIR=$(python -c "import yaml; print(yaml.safe_load(open('$CONFIG')).get('fsq',{}).get('checkpoint_dir','checkpoints'))")
LEVELS=$(python -c "import yaml; print(' '.join(str(x) for x in yaml.safe_load(open('$CONFIG')).get('model',{}).get('levels',[8,5,5,5])))")
DEATH_DIR=$(python -c "import yaml; print(yaml.safe_load(open('$CONFIG')).get('fsq',{}).get('episodes_dir','data/deepdash/death_episodes'))")
EXPERT_DIR=$(python -c "import yaml; print(yaml.safe_load(open('$CONFIG')).get('fsq',{}).get('expert_episodes_dir','data/deepdash/expert_episodes'))")

echo "=== Config: $CONFIG ==="
echo "=== FSQ checkpoint: $CKPT_DIR/fsq_best.pt ==="
echo "=== Levels: $LEVELS ==="
echo "=== Episode dirs: $DEATH_DIR | $EXPERT_DIR ==="

export PATH="/soft/AIDL/conda_envs/pytorch210/bin:$HOME/.local/bin:$PATH"
export WANDB_PROJECT=sls-wm-atari
export PYTHONPATH="$HOME/.python3-3.12-torch210/site-packages/lib/python3.12/site-packages:${PYTHONPATH:-}"

# V3-deploy default: vertical-only shifts at [-4,-2,0,2,4]
# Tokenize death + expert dirs separately so each gets their aug_dirs.
for EP_DIR in "$DEATH_DIR" "$EXPERT_DIR"; do
    if [ -d "$EP_DIR" ]; then
        echo "=== Tokenizing $EP_DIR ($(date)) ==="
        python -u scripts/tokenize_episodes.py \
            --episodes-dir "$EP_DIR" \
            --checkpoint "$CKPT_DIR/fsq_best.pt" \
            --levels $LEVELS \
            --shifts-v -4 -2 0 2 4
    fi
done
echo "=== Done ($(date)) ==="

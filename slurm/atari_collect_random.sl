#!/bin/bash
#SBATCH -J "atari_collect"
#SBATCH -o slurm/logs/atari_collect_random.out
#SBATCH -e slurm/logs/atari_collect_random.err
#SBATCH -p ar_h200
#SBATCH --gres=gpu:h200:1
#SBATCH -n 1
#SBATCH --cpus-per-gpu 8
#SBATCH --mem 32G
#SBATCH --time=08:00:00

# Collect the initial random-policy Atari replay block.
#
# Submit: sbatch slurm/atari_collect_random.sl [config] [steps]
# Default: configs/atari/atari_pong_h200.yaml, warmup_steps from config.

CONFIG=${1:-configs/atari/atari_pong_h200.yaml}
STEPS_ARG=${2:-}

read_cfg() {
    python - "$CONFIG" "$1" "$2" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1]))
section_key = sys.argv[2].split(".")
value = cfg
for key in section_key:
    value = value.get(key, {})
print(value if value != {} else sys.argv[3])
PY
}

GAME=$(read_cfg atari.game Pong)
OUT_DIR=$(read_cfg atari.out_dir "data/atari/$GAME")
REPLAY_DIR=$(read_cfg atari.replay_dir "$OUT_DIR/replay")
SHARD_SIZE=$(read_cfg atari.shard_size 8192)
WARMUP_STEPS=$(read_cfg atari.warmup_steps 10000)
FRAME_SKIP=$(read_cfg atari.frame_skip 4)
STICKY=$(read_cfg atari.repeat_action_probability 0.0)
SEED=$(read_cfg atari.seed 42)
STEPS=${STEPS_ARG:-$WARMUP_STEPS}

mkdir -p slurm/logs

module purge
module load aidl/pytorch/2.10.0-py3.12-cuda12.6
export PATH="$HOME/.local/bin:$PATH"

echo "=== Config: $CONFIG ==="
echo "=== Collect random Atari replay: game=$GAME steps=$STEPS replay=$REPLAY_DIR ==="

python -u scripts/collect_atari_episodes.py \
    --game "$GAME" \
    --out-dir "$OUT_DIR" \
    --storage replay \
    --replay-dir "$REPLAY_DIR" \
    --shard-size "$SHARD_SIZE" \
    --max-env-steps "$STEPS" \
    --n-episodes 100000 \
    --frame-skip "$FRAME_SKIP" \
    --repeat-action-probability "$STICKY" \
    --seed "$SEED"

#!/bin/bash
#SBATCH -J "wm_diag"
#SBATCH -o slurm/logs/world_model_diagnostics_%j.out
#SBATCH -e slurm/logs/world_model_diagnostics_%j.err
#SBATCH -p ar_h200
#SBATCH --gres=gpu:h200:1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=96G
#SBATCH --time=03:00:00

set -Eeuo pipefail

REPO_ROOT=${SLURM_SUBMIT_DIR:?Submit this job from the DashVMC repository root}
cd "$REPO_ROOT"
mkdir -p slurm/logs

export PATH="/soft/AIDL/conda_envs/pytorch210/bin:$HOME/.local/bin:$PATH"
export PYTHONPATH="$HOME/.python3-3.12-torch210/site-packages/lib/python3.12/site-packages:${PYTHONPATH:-}"
export PYTHONUNBUFFERED=1

python - <<'PY'
import torch

if not torch.cuda.is_available():
    raise SystemExit("CUDA is unavailable inside the evaluation job")
print(f"GPU: {torch.cuda.get_device_name(0)}")
print(f"PyTorch: {torch.__version__}")
PY

python -u scripts/eval_world_model_diagnostics.py "$@"

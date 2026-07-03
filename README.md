# DashVMC
### Real-Time Discrete World Model Control in Geometry Dash

[Florent Tariolle](https://tariolle.github.io/)

DashVMC is a real-time discrete Vision-Model-Controller system for Geometry Dash. It combines an FSQ tokenizer, an action-conditioned transformer world model, and a lightweight actor-critic trained from behavioural cloning plus PPO in latent rollouts.

The deployed controller runs at the same 30 FPS cadence as the captured training data. The full loop measures about 15 ms per frame on an RTX 2060 SUPER, leaving compute headroom of roughly 67 FPS; the 30 FPS setting is a data/capture cadence choice, not the inference ceiling.

<p align="center">
   <b>[ <a href="https://tariolle.github.io/dash-vmc/static/pdfs/dashvmc.pdf">Paper Draft</a> | <a href="https://tariolle.github.io/dash-vmc/">Website</a> | <a href="https://github.com/Tariolle/dash-vmc">Code</a> ]</b>
</p>

<p align="center">
  <img src="docs/static/images/pipeline.png" width="82%">
</p>

## System

DashVMC has three sequentially trained components:

1. **Vision:** an FSQ-VAE encodes 64x64 Sobel edge maps into an 8x8 grid of discrete tokens.
2. **Model:** a causal transformer predicts next-frame tokens from recent token grids and interleaved jump/idle actions.
3. **Controller:** a compact actor-critic is warm-started with demonstrations and optimized with PPO inside model-generated latent rollouts.

The decoder is used for tokenizer training and visual inspection. Policy optimization does not reconstruct pixels at each imagined step; the controller consumes token grids and transformer hidden states.

## Current Evidence

Frozen V7 logs currently support the system and latency claims:

| Component | Metric | Value |
| --- | ---: | ---: |
| FSQ tokenizer | Best validation reconstruction MSE | 1.595 |
| World model | Best validation token accuracy | 30.29% |
| World model | Best validation death F1 | 0.794 |
| BC controller | Best validation action accuracy | 79.8% |
| PPO controller | Best latent eval survival | 29.71 / 45 steps |
| Deployment loop | Full-loop latency | ~15 ms |
| Deployment loop | Configured cadence | 30 FPS |
| Deployment loop | Approximate compute headroom | ~67 FPS |

The repository also includes an automated real-game evaluator (`scripts/eval_real_game.py`). A fresh controlled deployment table is the main remaining empirical item before submission.

## Scoped Diagnostics

The Geometry Dash world model uses a local FSQ-neighbour smoothing target as a tokenizer-metric design choice. A matched IRIS/Pong transfer test did not support a broad claim that annealed structured label smoothing generally improves arbitrary discrete world models, so that evidence is treated as an auxiliary diagnostic rather than the project claim.

## Using the Code

**Environment.** Conda, PyTorch 2.10, CUDA 12.6.
```bash
conda run -n <env> python -m pip install -r requirements.txt
```

**Train the FSQ-VAE:**
```bash
python scripts/train_fsq.py
```

**Train the transformer world model on frozen FSQ tokens:**
```bash
python scripts/train_transformer.py
```

**Train the controller: BC warm-start, then PPO in latent rollouts:**
```bash
python scripts/train_controller_bc.py
python scripts/train_controller_ppo.py --pretrained checkpoints/controller_bc_best.pt
```

**Evaluate the live game:**
```bash
python scripts/eval_real_game.py --config configs/deepdash/v7-phase0.yaml --vae-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --controller-checkpoint checkpoints_v7/controller_ppo_best.pt --n-runs 100 --level-name "Level 1" --output analysis/2026-07-03_v7_deploy/eval_100.json
```

**Deploy to the live game at the training cadence:**
```bash
python scripts/deploy.py
```

Cluster launches (SLURM, A100): `sbatch slurm/train_fsq.sl`, `sbatch slurm/train_transformer.sl`, `sbatch slurm/train_controller.sl`.

## Contact

For questions or collaborations, contact `florent.tariolle@insa-rouen.fr`.

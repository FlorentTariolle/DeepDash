# DashVMC
### Real-Time Discrete World Model Control in Geometry Dash

[Florent Tariolle](https://tariolle.github.io/)

DashVMC is a real-time discrete Vision-Model-Controller system for Geometry Dash. It combines an FSQ tokenizer, an action-conditioned transformer world model, and a lightweight actor-critic trained from behavioural cloning plus PPO in latent rollouts.

The deployed controller runs at the same 30 FPS cadence as the captured training data. The full loop measures about 15 ms per frame on an RTX 2060, leaving compute headroom of roughly 67 FPS; the 30 FPS setting is a data/capture cadence choice, not the inference ceiling.

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

## Results

### Model and controller

| Component | Metric | Value |
| --- | --- | ---: |
| FSQ tokenizer | Selected-checkpoint validation reconstruction SSE / frame | 1.595 |
| World model | Selected-checkpoint validation token accuracy | 29.74% |
| World model | Selected-checkpoint validation death F1 | 0.7941 |
| BC controller | Validation action accuracy at the selected epoch | 79.76% |
| PPO controller | Selected-checkpoint latent evaluation survival | 29.71 / 45 steps |

These rows use the metric values at each V7 selection point rather than independent per-metric maxima. The FSQ checkpoint is epoch 920 (minimum validation reconstruction SSE), the world-model checkpoint is epoch 139 (maximum validation death F1), the BC selection point is epoch 9 (minimum validation loss), and the PPO checkpoint is iteration 3250 (maximum fixed latent-evaluation survival). The branch retains the BC training log but ships the downstream PPO checkpoint rather than the intermediate BC checkpoint. The FSQ loss is squared error summed over each 64x64 frame and averaged over samples, not pixelwise mean squared error.

### Training provenance

The archived tokenizer job loaded the selected FSQ checkpoint and retokenized all 4,228 death episodes and 36 expert episodes before world-model training. The base episode plus four vertical shifts produced exactly `(4,228 + 36) * 5 = 21,320` tokenized episodes; the subsequent world-model job reports consuming exactly 21,320. Checkpoint identity, timestamps, job IDs, hashes, and the corresponding log lines are recorded in [`analysis/2026-04-26_v7_training/PROVENANCE.md`](analysis/2026-04-26_v7_training/PROVENANCE.md). Token caches now carry checkpoint-hash metadata and are regenerated when their provenance does not match.

### Controlled live evaluation

The frozen V7 policy was evaluated over 100 consecutive attempts on each of the first three official levels at 30 FPS with Auto-Retry enabled.

| Level | Mean frames [95% CI] | Mean time | Median frames | Maximum frames |
| --- | ---: | ---: | ---: | ---: |
| 1 - Stereo Madness | 279.6 [270.5, 289.2] | 9.32 s | 289 | 439 |
| 2 - Back on Track | 263.3 [239.8, 287.2] | 8.78 s | 239 | 457 |
| 3 - Polargeist | 64.3 [59.3, 70.0] | 2.14 s | 51 | 199 |

The evaluator reports acted frames survived rather than level percentage. Results remain level-specific: the official levels increase in difficulty, and Polargeist introduces a yellow-orb mechanic requiring an additional timed jump while airborne. Raw attempts and bootstrap confidence intervals are available in [`analysis/2026-07-20_v7_deploy/`](analysis/2026-07-20_v7_deploy/).

### Optimized deployment latency

Mean per-stage latency was measured over approximately 5,000 frames on an RTX 2060 using the optimized live deployment path.

| Stage | Mean latency |
| --- | ---: |
| Screen capture | 2.4 ms |
| Crop | 0.5 ms |
| Grayscale | 0.5 ms |
| Sobel | 2.6 ms |
| Downscale | 2.1 ms |
| FSQ encode | 1.4 ms |
| Transformer | 4.2 ms |
| Controller | 1.1 ms |
| **Full loop** | **~15.0 ms** |

The stage values are rounded independently. The approximately 15 ms full loop corresponds to roughly 67 FPS of compute headroom; deployment remains fixed at the 30 FPS capture and training-data cadence.

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
python scripts/eval_real_game.py --config configs/deepdash/v7-phase0.yaml --vae-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --controller-checkpoint checkpoints_v7/controller_ppo_best.pt --n-runs 100 --level-name "Level 1" --output analysis/2026-07-20_v7_deploy/eval_100.json
```

To play interactively inside V7 model rollouts:

```bash
python scripts/play_dream.py --config configs/deepdash/v7-phase0.yaml --vae-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --controller-checkpoint checkpoints_v7/controller_ppo_best.pt --fps 30 --scale 6
```

Press `P` to start or stop recording the displayed rollout. Frames are written
as a timestamped PNG sequence under `analysis/dream_rollouts/`; reaching the end
of a rollout closes the active recording automatically.

**Deploy to the live game at the training cadence:**
```bash
python scripts/deploy.py
```

Cluster launches (SLURM, A100): `sbatch slurm/train_fsq.sl`, `sbatch slurm/train_transformer.sl`, `sbatch slurm/train_controller.sl`.

## Contact

For questions or collaborations, contact `florent.tariolle@insa-rouen.fr`.

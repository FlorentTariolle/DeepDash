# DashVMC
### Real-Time Discrete World Model Control in Geometry Dash

[Florent Tariolle](https://tariolle.github.io/)

https://tariolle.github.io/dash-vmc/demo_preview.mp4

DashVMC is a real-time discrete Vision-Model-Controller system for Geometry Dash. It combines an FSQ tokenizer, an action-conditioned transformer world model, and a lightweight actor-critic trained from behavioural cloning plus PPO in latent rollouts.

The deployed controller runs at the same 30 FPS cadence as the captured training data. Over 5,000 optimized-path frames on an RTX 2060, wall-clock latency from capture through the keyboard action update averaged 16.3 ms (median 16.3 ms, p95 18.7 ms), corresponding to roughly 61 FPS of mean compute headroom.

<p align="center">
   <b>[ <a href="https://tariolle.github.io/dash-vmc/static/pdfs/dashvmc.pdf">Technical Preprint</a> | <a href="https://tariolle.github.io/dash-vmc/">Website</a> | <a href="https://github.com/Tariolle/dash-vmc">Code</a> ]</b>
</p>

<p align="center">
  <img src="docs/static/images/pipeline.png" width="82%">
</p>

## Cite this preprint

```bibtex
@misc{tariolle2026dashvmc,
  title  = {DashVMC: Real-Time Discrete World Model Control in Geometry Dash},
  author = {Florent Tariolle},
  year   = {2026},
  note   = {Technical preprint},
  url    = {https://tariolle.github.io/dash-vmc/}
}
```

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

To play interactively inside the model rollouts:

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

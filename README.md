## DashVMC: Real-Time Discrete World Model Control in Geometry Dash

https://github.com/user-attachments/assets/1de92e9c-cc41-48cc-8305-c0d4491b676b

DashVMC is a real-time discrete Vision-Model-Controller system for Geometry Dash. It combines an FSQ tokenizer, an action-conditioned transformer world model, and a lightweight actor-critic trained from behavioural cloning plus PPO in latent rollouts.

The deployed controller runs at the same 30 FPS cadence as the captured training data. Over 5,000 optimized-path frames on an RTX 2060 SUPER, wall-clock latency from capture through the keyboard action update averaged 16.3 ms, corresponding to roughly 61 FPS of mean compute headroom.

<p align="center">
   <b>[ <a href="https://tariolle.github.io/dash-vmc/static/pdfs/dashvmc.pdf">Preprint</a> | <a href="https://tariolle.github.io/dash-vmc/">Website</a> ]</b>
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
  note   = {Preprint},
  url    = {https://tariolle.github.io/dash-vmc/}
}
```

## Using the Code

**Environment.** Conda, PyTorch 2.11.0+cu126, CUDA 12.6. Live eval/deploy hardware: AMD Ryzen 5 3600X, 16 GB RAM, NVIDIA GeForce RTX 2060 SUPER (8 GB), Windows 11.
```bash
conda run -n <env> python -m pip install -r requirements.txt
```

**Train the FSQ:**
```bash
python scripts/train_fsq.py
```

**Train the transformer on frozen FSQ tokens:**
```bash
python scripts/train_transformer.py
```

**Train the controller: BC warm-start, then PPO in latent rollouts:**
```bash
python scripts/train_controller_bc.py
python scripts/train_controller_ppo.py --pretrained checkpoints/controller_bc_best.pt
```

**Evaluate the end-to-end pipeline on the real game (live):**
```bash
python scripts/eval_real_game.py --config configs/deepdash/v7-phase0.yaml --vae-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --controller-checkpoint checkpoints_v7/controller_ppo_best.pt --n-runs 100 --level-name "Level 1" --output analysis/2026-07-20_v7_deploy/eval_100.json
```

**To play interactively inside the model rollouts:**

```bash
python scripts/play_dream.py --config configs/deepdash/v7-phase0.yaml --vae-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --controller-checkpoint checkpoints_v7/controller_ppo_best.pt --fps 30 --scale 12 --max-dream-steps 500
```

**Deploy to the live game at the training cadence:**
```bash
python scripts/deploy.py --config configs/deepdash/v7-phase0.yaml --vae-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --controller-checkpoint checkpoints_v7/controller_ppo_best.pt
```


## Live evaluation suite (V7)

Controlled live survival uses the frozen V7 stack at 30 FPS with Auto-Retry:

| Setting | Levels / role | PPO / BC / no-op scored attempts |
| --- | --- | ---: |
| Official | Levels 1--3 | 99 / 100 / 10 |
| Late Level-1 copy (not held-out) | Stereo Madness Copy | 20 / 20 / 10 |
| Custom held-out (supported mechanics) | Stereo INSANE Nerfed | 20 / 20 / 10 |

Artifacts:
- Official + latency: `analysis/2026-07-20_v7_deploy/`, `analysis/2026-07-21_live_baselines/`
- Ship-segment proxy: `analysis/2026-07-22_ship_segment_eval/`
- Held-out custom: `analysis/2026-07-22_heldout_stereo_insane_nerfed/`
- Machine facts: `analysis/2026-07-22_experiment_machine/MACHINE_FACTS.md`

Expert-subset note: 33,153 frames equal 18.42 minutes at exact 30 FPS; recorded wall-clock capture duration is 18.95 minutes (~29.16 FPS average).

## Contact

For questions or collaborations, contact `florent.tariolle@insa-rouen.fr`.

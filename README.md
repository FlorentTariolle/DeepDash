## DashVMC: Real-Time Discrete World Model Control in Geometry Dash

https://github.com/user-attachments/assets/1de92e9c-cc41-48cc-8305-c0d4491b676b

DashVMC is a real-time discrete Vision-Model-Controller system for Geometry Dash. It combines an FSQ tokenizer, an action-conditioned transformer world model, and a lightweight actor-critic trained from behavioural cloning plus PPO in latent rollouts.

Training data are captured at a nominal 30 FPS cadence, while the optimized live stack deploys at 60 FPS. Over 5,000 optimized-path frames on an RTX 2060 SUPER, wall-clock latency from capture through the keyboard action update averaged 16.3 ms (below the 16.7 ms / 60-FPS budget). On Stereo Madness Copy, 60-FPS PPO survival is comparable in wall-clock time to 30-FPS deployment.

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

Official-level comparisons use the diagnostic path at 30 FPS (default). For 60 FPS optimized deployment-equivalent eval, pass `--inference-path optimized --fps 60`.

```bash
python scripts/eval_real_game.py --config configs/deepdash/v7-phase0.yaml --vae-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --controller-checkpoint checkpoints_v7/controller_ppo_best.pt --n-runs 100 --level-name "Level 1" --output analysis/2026-07-20_v7_deploy/eval_100.json
```

**To play interactively inside the model rollouts:**

```bash
python scripts/play_dream.py --config configs/deepdash/v7-phase0.yaml --vae-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --controller-checkpoint checkpoints_v7/controller_ppo_best.pt --fps 30 --scale 12 --max-dream-steps 500
```

**Deploy to the live game (60 FPS optimized path):**
```bash
python scripts/deploy.py --config configs/deepdash/v7-phase0.yaml --vae-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --controller-checkpoint checkpoints_v7/controller_ppo_best.pt --fps 60
```


## Live evaluation suite (V7)

Controlled live survival uses the frozen V7 stack with Auto-Retry. Official and auxiliary comparisons use 30 FPS; a cadence-transfer probe uses the optimized path at 60 FPS.

| Setting | Levels / role | Cadence | PPO / BC / no-op scored attempts |
| --- | --- | ---: | ---: |
| Official | Levels 1--3 | 30 FPS | 99 / 100 / 10 |
| Late Level-1 copy (not held-out) | Stereo Madness Copy | 30 FPS | 20 / 20 / 10 |
| Custom held-out (supported mechanics) | Stereo INSANE Nerfed | 30 FPS | 20 / 20 / 10 |
| Cadence transfer | Stereo Madness Copy | **60 FPS optimized** | 20 / -- / -- |

On Stereo Madness Copy, PPO mean wall-clock survival is 17.52 s at 30 FPS vs 16.64 s at 60 FPS (episode wall times 17.79 s vs 17.03 s).

Artifacts:
- Official + latency: `analysis/2026-07-20_v7_deploy/`, `analysis/2026-07-21_live_baselines/`
- Ship-segment proxy: `analysis/2026-07-22_ship_segment_eval/`
- Held-out custom: `analysis/2026-07-22_heldout_stereo_insane_nerfed/`
- 60 FPS cadence probe: `analysis/2026-07-23_fps60_stereo_madness_copy/`
- Machine facts: `analysis/2026-07-22_experiment_machine/MACHINE_FACTS.md`

Optimized live eval path:
```bash
python scripts/eval_real_game.py --config configs/deepdash/v7-phase0.yaml --inference-path optimized --fps 60 --policy ppo --policy-class v3_cnn --vae-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --controller-checkpoint checkpoints_v7/controller_ppo_best.pt --n-runs 20 --level-name "Stereo Madness Copy" --output analysis/2026-07-23_fps60_stereo_madness_copy/ppo_stereo_madness_copy_20.json
```

Expert-subset note: 33,153 frames equal 18.42 minutes at exact 30 FPS; recorded wall-clock capture duration is 18.95 minutes (~29.16 FPS average). Dataset cadence is 30 FPS; optimized deployment is 60 FPS.

## Contact

For questions or collaborations, contact `florent.tariolle@insa-rouen.fr`.

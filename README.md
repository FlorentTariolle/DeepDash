## DashVMC: Real-Time Discrete World Model Control in Geometry Dash

https://github.com/user-attachments/assets/1de92e9c-cc41-48cc-8305-c0d4491b676b

DashVMC is an end-to-end discrete world-model agent that controls the original, non-paused Geometry Dash game from captured screen pixels. It combines an FSQ tokenizer, an action-conditioned transformer world model, and a lightweight actor-critic initialized by behavioural cloning and optimized with PPO entirely in autoregressive latent rollouts.

Training uses approximately two hours of gameplay captured at a nominal 30 FPS cadence, including less than 20 minutes of expert segments; PPO requires no additional game interaction. The decoder-free capture-to-action path sustains live control at 60 FPS on an RTX 2060.

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

**Environment.** Conda, PyTorch 2.11.0+cu126, CUDA 12.6.
```bash
conda run -n <env> python -m pip install -r requirements.txt
```

**Reported code snapshot.** The exact code used to train the reported end-to-end pipeline is preserved by Git tag `V7` (commit `b636bcd4`). This is the immutable historical training snapshot; the commands below use the maintained main-branch paths and frozen `v7-phase0` configuration.

**Train the FSQ and tokenize both episode sets:**
```bash
python scripts/train_fsq.py --config configs/deepdash/v7-phase0.yaml
python scripts/tokenize_episodes.py --episodes-dir data/deepdash/death_episodes --checkpoint checkpoints_v7/fsq_best.pt --levels 8 5 5 5 --shifts-v -4 -2 0 2 4
python scripts/tokenize_episodes.py --episodes-dir data/deepdash/expert_episodes --checkpoint checkpoints_v7/fsq_best.pt --levels 8 5 5 5 --shifts-v -4 -2 0 2 4
```

**Train the transformer on frozen FSQ tokens:**
```bash
python scripts/train_world_model.py --config configs/deepdash/v7-phase0.yaml
```

**Train the controller: BC warm-start, then PPO in latent rollouts:**
```bash
python scripts/train_controller_bc.py --config configs/deepdash/v7-phase0.yaml --fsq-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt
python scripts/train_controller_ppo.py --config configs/deepdash/v7-phase0.yaml --fsq-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --pretrained checkpoints_v7/controller_bc_best.pt
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
## Contact

For questions or collaborations, contact `florent.tariolle@insa-rouen.fr`.

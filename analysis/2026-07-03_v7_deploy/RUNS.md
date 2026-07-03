# V7 Live Deployment Evaluation

Date: 2026-07-03

Frozen system:

- Config: `configs/deepdash/v7-phase0.yaml`
- FSQ: `checkpoints_v7/fsq_best.pt`
- Transformer: `checkpoints_v7/transformer_best.pt`
- Controller: `checkpoints_v7/controller_ppo_best.pt`
- Controller architecture: `v3_cnn`

Smoke attempt:

```bash
python scripts/eval_real_game.py --config configs/deepdash/v7-phase0.yaml --vae-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --controller-checkpoint checkpoints_v7/controller_ppo_best.pt --n-runs 30 --level-name "Level 1" --output analysis/2026-07-03_v7_deploy/smoke_30.json
```

Result: blocked before gameplay because `GeometryDash.exe` was not running. Model loading succeeded on CUDA and selected `Controller: v3_cnn`.

Next run once Geometry Dash is open and the player is alive in Level 1:

```bash
python scripts/eval_real_game.py --config configs/deepdash/v7-phase0.yaml --vae-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --controller-checkpoint checkpoints_v7/controller_ppo_best.pt --n-runs 30 --level-name "Level 1" --output analysis/2026-07-03_v7_deploy/smoke_30.json
python scripts/eval_real_game.py --config configs/deepdash/v7-phase0.yaml --vae-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --controller-checkpoint checkpoints_v7/controller_ppo_best.pt --n-runs 100 --level-name "Level 1" --output analysis/2026-07-03_v7_deploy/eval_100.json
```

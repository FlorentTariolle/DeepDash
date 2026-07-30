# Seed-43 PPO horizon-20 live evaluation

This exploratory evaluation compares the 30,000-iteration, horizon-20 PPO run
with the reported seed-43 horizon-45 controller. It uses the same diagnostic
30-FPS live path as the official-level results in the paper.

## Frozen checkpoint

- Path: `checkpoints_v7_controller_seed43_h20_30k/controller_ppo_best.pt`
- Selected iteration: 25,770
- Development survival: 16.61/20
- SHA-256: `2a0e06501f86170864acbf893a2671e31d1ab35bad32d85957db6d9a35822751`

## Protocol

- Evaluate 10 valid attempts on each of the five layouts reported in the
  horizon probe: the three official levels and the two modified Stereo Madness
  layouts.
- Keep Geometry Dash on the named level before starting each command.
- Use Auto-Retry and the diagnostic inference path at 30 FPS.
- Do not pool attempts across levels.

```powershell
python scripts/eval_real_game.py --config configs/deepdash/v7-phase0.yaml --vae-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --controller-checkpoint checkpoints_v7_controller_seed43_h20_30k/controller_ppo_best.pt --n-runs 10 --fps 30 --level-name "Stereo Madness" --output analysis/2026-07-30_v7_seed43_h20_live10/ppo_stereo_madness_10.json

python scripts/eval_real_game.py --config configs/deepdash/v7-phase0.yaml --vae-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --controller-checkpoint checkpoints_v7_controller_seed43_h20_30k/controller_ppo_best.pt --n-runs 10 --fps 30 --level-name "Back on Track" --output analysis/2026-07-30_v7_seed43_h20_live10/ppo_back_on_track_10.json

python scripts/eval_real_game.py --config configs/deepdash/v7-phase0.yaml --vae-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --controller-checkpoint checkpoints_v7_controller_seed43_h20_30k/controller_ppo_best.pt --n-runs 10 --fps 30 --level-name "Polargeist" --output analysis/2026-07-30_v7_seed43_h20_live10/ppo_polargeist_10.json

python scripts/eval_real_game.py --config configs/deepdash/v7-phase0.yaml --vae-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --controller-checkpoint checkpoints_v7_controller_seed43_h20_30k/controller_ppo_best.pt --n-runs 10 --fps 30 --level-name "Stereo Madness Copy" --output analysis/2026-07-30_v7_seed43_h20_live10/ppo_stereo_madness_copy_10.json

python scripts/eval_real_game.py --config configs/deepdash/v7-phase0.yaml --vae-checkpoint checkpoints_v7/fsq_best.pt --transformer-checkpoint checkpoints_v7/transformer_best.pt --controller-checkpoint checkpoints_v7_controller_seed43_h20_30k/controller_ppo_best.pt --n-runs 10 --fps 30 --level-name "Stereo INSANE Nerfed" --output analysis/2026-07-30_v7_seed43_h20_live10/ppo_stereo_insane_nerfed_10.json
```

Treat the 10-run results as exploratory. If confirmatory precision is needed,
extend all five layouts to 25 attempts rather than selectively extending only
the most favorable or ambiguous layout.

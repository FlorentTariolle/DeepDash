# Stereo Madness Copy late-stage / ship-segment evaluation

Date: 2026-07-22

## Role in the evaluation suite

This level is **not** held-out transfer. `Stereo Madness Copy` by KRUTOYARBUS is a community copy of a later stage of official Level 1 (Stereo Madness). It is used as a **late Level-1 / ship-reachable segment proxy** because the official Level 1--3 controlled evals die before ship, so no-op / BC / PPO differences in ship mode are invisible there.

Claims from this artifact should be limited to:
- late-stage Level-1-like survival under the same frozen V7 stack
- ranking of no-op vs BC vs PPO once the agent can reach farther than the official early-level death points

It is **not** reported as unseen-level generalization.

## Protocol

- Same evaluator as official live baselines: `scripts/eval_real_game.py`
- Config / system: `configs/deepdash/v7-phase0.yaml`, system variant V7
- Cadence: 30 FPS, Auto-Retry, memory-based death detection
- Initial synchronization episode excluded from statistics
- Checkpoints:
  - FSQ: `checkpoints_v7/fsq_best.pt`
  - Transformer: `checkpoints_v7/transformer_best.pt`
  - BC: `checkpoints_v7/controller_bc_epoch9_reconstructed.pt`
  - PPO: `checkpoints_v7/controller_ppo_best.pt`
- Sample sizes (by design for this auxiliary eval): 10 no-op, 20 BC, 20 PPO scored attempts

## Results

| Policy | Scored *n* | Mean frames [95% CI] | Median | Min--max | Mean seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| No-op | 10 | 135.0 [135.0, 135.0] | 135 | 135--135 | 4.50 |
| BC | 20 | 271.2 [224.1, 317.9] | 288 | 134--421 | 9.04 |
| PPO | 20 | **525.6 [503.9, 548.4]** | 551 | 459--615 | 17.52 |

Raw JSON:
- `noop_stereo_madness_copy_10.json`
- `bc_stereo_madness_copy_20.json`
- `ppo_stereo_madness_copy_20.json`

## Read

PPO >> BC >> no-op with non-overlapping mean CIs for the PPO vs BC comparison. No-op is deterministic at 135 frames (fixed first obstacle on this copy). The evaluator records survival frames only; it does not label ship entry, so paper wording should remain late-stage / ship-reachable segment rather than pure isolated ship metrics unless manual verification is added.

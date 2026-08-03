# Seed-43 spatial-only controller ablation

This ablation keeps the selected V7 tokenizer and world model, controller seed
43, the 45-step dream horizon, the 15,000-iteration PPO budget, and all PPO
hyperparameters fixed.  The controller receives only the current token grid
`z_t`; it never receives `h_t`.  The world model still generates the PPO
environment, but it is not loaded for live inference.

The selected spatial-only PPO checkpoint reaches **29.02/45** on the 512 fixed
dream-evaluation contexts at iteration 12,430.  The matched seed-43 controller
with `z_t + h_t` reaches **30.13/45**.

## Live results

Values are mean frames +/- sample standard deviation.  The temporal-state
reference uses 25 attempts from `analysis/2026-07-25_v7_seed43_live25`; the
spatial-only policies use 10 attempts.  Difference intervals independently
bootstrap the two frozen-policy samples 50,000 times.

| Level | Spatial BC | Spatial PPO | Spatial PPO - BC [95% CI] | Temporal PPO | Spatial - temporal PPO [95% CI] |
| --- | ---: | ---: | ---: | ---: | ---: |
| Stereo Madness | 62.5 +/- 33.6 | 277.9 +/- 19.2 | 215.4 [190.8, 236.3] | 280.1 +/- 23.3 | -2.2 [-16.4, 12.6] |
| Back on Track | 87.4 +/- 41.0 | 294.9 +/- 64.4 | 207.5 [160.1, 250.0] | 260.0 +/- 55.5 | 34.9 [-10.1, 76.4] |
| Polargeist | 40.6 +/- 3.8 | 128.0 +/- 65.1 | 87.4 [48.4, 124.3] | 65.2 +/- 33.4 | 62.8 [21.3, 101.7] |
| Stereo INSANE Nerfed | 53.2 +/- 27.7 | 248.1 +/- 77.3 | 194.9 [142.5, 237.8] | 290.4 +/- 48.6 | -42.3 [-94.2, 2.9] |

PPO improves over its spatial-only BC parent on every tested layout, with all
four bootstrap intervals excluding zero.  Relative to the temporal-state PPO
reference, the only interval excluding zero favors the spatial-only controller
on Polargeist.  This single-seed, unequal-attempt probe provides no evidence
that `h_t` is required for live transfer; it does not establish that the two
controller inputs are equivalent.

## Provenance

- FSQ SHA-256: `8a4c488e0310855bcf411787894f0206f3381387bcd9fc179d6a6610ef32e5f7`
- World-model SHA-256: `62db7d8f0dca5cb75684548c2e93a74c4f6dda830250f171e38fe97161dcd770`
- Spatial BC SHA-256: `02ea5a945f690711a1d174b61d2a398c3a9b538f93cfa1bf792869e552435c5a`
- Spatial PPO SHA-256: `79bbf3eee9b57f2674ce21a399e413d6e2c54691b8096bec673d0fbfcdbb76f3`
- Training wall time: 61,622 seconds (17.12 H200 hours)
- Completed PPO iterations: 15,000

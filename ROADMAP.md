# Project Roadmap

Last updated on 2026-07-23.

## Paper Direction

DashVMC is a Geometry Dash world-model control paper.

The defensible claim is narrow and system-level: a compact discrete Vision-Model-Controller stack can tokenize Geometry Dash observations, model action-conditioned dynamics, train a controller in latent rollouts from 30-FPS captures, and deploy live at 60 FPS on consumer hardware.

The paper should emphasize:

- FSQ tokenization of 64x64 Sobel frames into an 8x8 discrete grid.
- Action-conditioned transformer dynamics with interleaved jump/idle tokens.
- Behavioural-cloning warm-start followed by PPO in latent model rollouts.
- Live deployment at 60 FPS from 30-FPS training data, with measured full-loop latency around 16.3 ms (fits the 16.7 ms budget).
- Decoded rollout visualizations as inspection/generation artifacts, not as the policy training signal.
- Implementation diagnostics explaining conservative design choices.

Structured Label Smoothing is in scope only as a Geometry Dash/FSQ design choice plus auxiliary diagnostic. The available IRIS/Pong result does not support a general positive method claim for arbitrary discrete tokenizers.

## Evidence Already Available

Frozen V7 logs support these current system numbers:

| Component | Metric | Value | Source |
| --- | ---: | ---: | --- |
| FSQ tokenizer | Selected-checkpoint validation reconstruction SSE / frame | 1.595 (epoch 920) | `checkpoints_v7/fsq_log.csv` |
| World model | Selected-checkpoint validation token accuracy | 29.74% (epoch 139) | `checkpoints_v7/transformer_log.csv` |
| World model | Selected-checkpoint validation death F1 | 0.7941 (epoch 139) | `checkpoints_v7/transformer_log.csv` |
| BC controller | Validation action accuracy at minimum-loss epoch | 79.76% (epoch 9) | `checkpoints_v7/controller_bc_log.csv` |
| PPO controller | Selected-checkpoint latent eval survival | 29.71 / 45 steps (iteration 3250) | `checkpoints_v7/controller_ppo_log.csv` |
| Deployment | Mean live survival (100 Level 1 attempts) | 279.6 frames / 9.32 s | `analysis/2026-07-20_v7_deploy/eval_100.json` |
| Deployment | 95% bootstrap CI of mean survival | 270.5-289.2 frames | `analysis/2026-07-20_v7_deploy/SUMMARY.md` |
| Deployment | Median / maximum live survival | 289 / 439 frames | `analysis/2026-07-20_v7_deploy/eval_100.json` |
| Deployment | Mean Level 2 survival (95% CI) | 263.3 [239.8, 287.2] frames | `analysis/2026-07-20_v7_deploy/eval_100_level2.json` |
| Deployment | Mean Level 3 survival (95% CI) | 64.3 [59.3, 70.0] frames | `analysis/2026-07-20_v7_deploy/eval_100_level3.json` |
| Deployment | Full-loop latency | 16.3 ms end-to-end (component sum 15.3 ms) | `analysis/2026-07-20_v7_deploy/optimized_wallclock_5000.json` |
| Deployment | Dataset capture cadence | 30 FPS | corpus / capture |
| Deployment | Live deployment cadence | **60 FPS** | optimized path |
| Deployment | Compute headroom | ~61 FPS mean | 1 / 16.3 ms |
| Aux live | Stereo Madness Copy PPO mean [CI] | 525.6 [503.9, 548.4] frames / 17.79 s recorded wall time | `analysis/2026-07-22_ship_segment_eval/` |
| Aux live | Stereo Madness Copy @ 60 FPS | 17.03 s recorded wall time | `analysis/2026-07-23_fps60_stereo_madness_copy/` |
| Aux live | Stereo INSANE Nerfed PPO mean [CI] | 215.7 [184.1, 249.9] | `analysis/2026-07-22_heldout_stereo_insane_nerfed/` |
| Machine | Live GPU | RTX 2060 SUPER / PyTorch 2.11.0+cu126 | `analysis/2026-07-22_experiment_machine/MACHINE_FACTS.md` |

The controlled V7 evaluator runs are complete for the first three official levels, plus a late Level-1 copy (ship-reachable proxy), one custom held-out community level, and a 60-FPS cadence probe on the Level-1 copy. They measure frames survived rather than level percentage; cross-cadence comparisons use recorded episode wall time. Results remain level-specific: difficulty increases across the sequence, Level 3 introduces timed mid-air yellow-orb inputs, and the custom level is reported as within-mechanic transfer only. The 60-FPS probe supports practical cadence transfer, not broad frame-rate adaptation as a core method claim.

## Compute To Finish

1. If useful, add a survival/death branch contrast to the decoded continuation figure.
2. Audit the exact checkpoints used by the website demo, the paper tables, and the live evaluator so the paper cites V7 rather than a mixture of development variants.

## Writing To Finish

1. Add the generated-continuation figure. (Done for the paper build; inspect final PDF placement.)
2. Preserve the optimized deployment benchmark and its per-stage averages as the reported latency evidence.
3. Move any SLS/IRIS discussion behind the DashVMC system results and keep the claim scoped.
4. Compile the PDF and update `docs/static/pdfs/dashvmc.pdf`.

## Do Not Spend Time On Before August

- Do not try to rescue the IRIS/Pong SLS result with more compute.
- Do not start a full GQ tokenizer port.
- Do not start a major architecture redesign.
- Do not claim FSQ is tokenizer-SOTA.
- Do not claim SLS generally improves arbitrary discrete tokenizers.
- Do not use Geometry Dash deployment progress as a causal SLS effect unless the comparison is controlled.

## Out Of Scope

- Full Atari controller port.
- Broad Atari benchmark expansion.
- Main-conference-scale claims about SLS.
- Exporting editor-valid Geometry Dash levels with guaranteed playability.

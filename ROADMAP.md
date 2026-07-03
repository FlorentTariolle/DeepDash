# Project Roadmap

Last updated on 2026-07-03.

## Paper Direction

DashVMC is a Geometry Dash world-model control paper.

The defensible claim is narrow and system-level: a compact discrete Vision-Model-Controller stack can tokenize Geometry Dash observations, model action-conditioned dynamics, train a controller in latent rollouts, and deploy live at the 30 FPS cadence used by the dataset.

The paper should emphasize:

- FSQ tokenization of 64x64 Sobel frames into an 8x8 discrete grid.
- Action-conditioned transformer dynamics with interleaved jump/idle tokens.
- Behavioural-cloning warm-start followed by PPO in latent model rollouts.
- Live deployment at 30 FPS, with measured full-loop latency around 15 ms and roughly 67 FPS compute headroom.
- Decoded rollout visualizations as inspection/generation artifacts, not as the policy training signal.
- Implementation diagnostics explaining conservative design choices.

Structured Label Smoothing is in scope only as a Geometry Dash/FSQ design choice plus auxiliary diagnostic. The available IRIS/Pong result does not support a general positive method claim for arbitrary discrete tokenizers.

## Evidence Already Available

Frozen logs support these current system numbers:

| Component | Metric | Value | Source |
| --- | ---: | ---: | --- |
| FSQ tokenizer | Validation reconstruction MSE | 2.49 | `experiments/v3_deploy/fsq_log.csv` |
| World model | Best validation token accuracy | 35.25% | `experiments/v3_deploy/transformer_log.csv` |
| World model | Best validation death F1 | 0.798 | `experiments/v3_deploy/transformer_log.csv` |
| BC controller | Best validation action accuracy | 87.1% | `experiments/v3_deploy/controller_bc_log.csv` |
| PPO controller | Best latent eval survival | 33.48 / 45 steps | `experiments/v3_deploy/controller_ppo_log.csv` |
| Deployment | Full-loop latency | ~15 ms | `paper/main.tex` deployment notes |
| Deployment | Configured cadence | 30 FPS | dataset/capture cadence |
| Deployment | Compute headroom | ~67 FPS | 1 / 15 ms |

Development notes also record V3 real-game progress across several Geometry Dash levels, but those notes should be replaced by a fresh controlled evaluator run before submission.

## Compute To Finish

1. Run `scripts/eval_real_game.py` on the frozen checkpoints for at least 100 attempts on Level 1.
2. If time permits, repeat the same evaluator on the additional levels already used in development notes: Level 3, Level 5, Level 6, Polargeist VE, and Polargeist V2.
3. Save JSON outputs under a dated analysis folder and summarize mean, median, min, max, and quartiles.
4. Generate one figure panel from decoded rollouts: real prefix, sampled continuation, and if available a survival/death branch contrast.
5. Audit the exact checkpoints used by the website demo, the paper tables, and the live evaluator so the paper cites one frozen system rather than a mixture of development variants.

## Writing To Finish

1. Replace the provisional Geometry Dash system table in the paper with the final controlled evaluator table.
2. Add the generated-continuation figure.
3. Add an inference latency breakdown if stage-level timing logs are available; otherwise keep the measured full-loop number.
4. Move any SLS/IRIS discussion behind the DashVMC system results and keep the claim scoped.
5. Compile the PDF and update `docs/static/pdfs/dashvmc.pdf`.

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

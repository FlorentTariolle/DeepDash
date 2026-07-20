# V7 Controlled Live Evaluation

Date: 2026-07-20

## Protocol

- Environment: the first three official Geometry Dash levels, 1920x1080 fullscreen.
- System: frozen V7 FSQ tokenizer, transformer, and PPO controller.
- Cadence: 30 FPS.
- Attempts: 100 consecutive attempts per level with Auto-Retry enabled.
- Episode boundary: `GeometryDash.exe` memory state (`in_level`, `is_dead`).
- Outcome: acted frames survived; the evaluator does not read level percentage.
- Hardware: NVIDIA GeForce RTX 2060.

Checkpoints:

- `checkpoints_v7/fsq_best.pt`
- `checkpoints_v7/transformer_best.pt`
- `checkpoints_v7/controller_ppo_best.pt`

Raw results: `eval_100.json`, `eval_100_level2.json`, and
`eval_100_level3.json`.

## Results

| Level | Mean frames [95% CI] | Median [P25, P75] | Maximum | Mean seconds |
| --- | ---: | ---: | ---: | ---: |
| 1 - Stereo Madness | 279.6 [270.5, 289.2] | 289 [244, 314] | 439 | 9.32 |
| 2 - Back on Track | 263.3 [239.8, 287.2] | 239 [145, 367] | 457 | 8.78 |
| 3 - Polargeist | 64.3 [59.3, 70.0] | 51 [50, 93] | 199 | 2.14 |

Each confidence interval is a percentile bootstrap over attempts with 50,000
resamples and RNG seed 20260720. Levels 1 and 2 have overlapping mean
confidence intervals, although Level 2 is substantially more variable. Level 3
is a harder task and introduces yellow-orb interactions that require a timed
mid-air jump input; its lower mean of 64.3 frames is therefore a level-specific
difficulty result, not evidence of a generalization failure. Repeated values
correspond to recurring deterministic failure points under small capture and
action-timing variations.

## Selection-aligned V7 model metrics

These values report the validation metrics at the epoch or iteration that
actually produced each selected V7 checkpoint, rather than mixing independent
per-column maxima from different points in training.

| Component | Selection rule | Selected point | Metric at selected point |
| --- | --- | ---: | ---: |
| FSQ tokenizer | Minimum validation reconstruction SSE | Epoch 920 | 1.595 SSE / frame |
| World model | Maximum validation death F1 | Epoch 139 | 29.74% token accuracy; 0.7941 death F1 |
| BC controller | Minimum validation loss | Epoch 9 | 79.76% action accuracy |
| PPO controller | Maximum fixed latent evaluation survival | Iteration 3250 | 29.71 / 45 steps |

The FSQ validation loss is squared pixel error summed over each 64x64 frame and
averaged over samples. It is not pixelwise mean squared error. For comparison,
the independent log maxima of 30.29% world-model token accuracy (epoch 194) and
79.80% BC action accuracy (epoch 6) do not correspond to the selected
world-model and BC checkpoints and are therefore not used in the public table.
The branch tracks the FSQ, world-model, and downstream PPO checkpoints as Git
LFS objects with SHA-256 prefixes `8a4c488e03`, `62db7d8f0d`, and `b00a30ccad`,
respectively. It retains the BC training log but does not track the intermediate
`controller_bc_best.pt` artifact.

## Optimized deployment timing

The deployment benchmark measures the optimized live path over approximately
5,000 frames on an RTX 2060. This path uses GPU-resident buffers, compiled
FSQ inference, a CUDA Graph for transformer context encoding, and the natural
end-of-frame synchronization used to obtain the controller action.

| Stage | Mean ms |
| --- | ---: |
| Screen capture | 2.4 |
| Crop | 0.5 |
| Grayscale | 0.5 |
| Sobel | 2.6 |
| Downscale | 2.1 |
| FSQ encode | 1.4 |
| Transformer | 4.2 |
| Controller | 1.1 |
| **Total** | **15.0** |

The 15.0 ms total corresponds to approximately 67 FPS of compute headroom.
Deployment remains configured at 30 FPS to match the capture and training-data
cadence.

## Scope

These 300 attempts quantify repeated execution of one frozen policy on three
deterministic levels of increasing difficulty. Results are reported per level
and are not normalized for obstacle layout or mechanics, so they should not be
interpreted as a controlled generalization comparison. They measure deployment
variability, not uncertainty over training seeds. No causal SLS claim is made
from this evaluation.

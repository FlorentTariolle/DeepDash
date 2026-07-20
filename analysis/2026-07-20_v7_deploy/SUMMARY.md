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

The deployment benchmark measures 5,000 active post-warmup frames on the
optimized live path on an RTX 2060. This path uses GPU-resident context,
compiled FSQ inference, a CUDA Graph for transformer context encoding, and the
natural end-of-frame synchronization used to obtain the controller action.

| Stage | Mean ms |
| --- | ---: |
| Screen capture | 2.2 |
| Crop | 0.5 |
| Grayscale | 0.5 |
| Sobel | 2.5 |
| Downscale | 2.2 |
| FSQ encode | 2.1 |
| Transformer | 4.9 |
| Controller | 0.5 |
| Component-sum mean | 15.3 |
| **Measured end-to-end** | **16.3** |

End-to-end wall time begins immediately before screen capture and ends after
the keyboard action update when required, excluding the deliberate cadence
sleep. Its mean is 16.289 ms, median 16.308 ms, p75 17.302 ms, and p95
18.722 ms; 2 of 5,000 frames (0.04%) exceed the 33.3 ms budget. The mean
corresponds to approximately 61 FPS of compute headroom, while deployment
remains configured at 30 FPS to match the capture and training-data cadence.
Raw stage and end-to-end samples, hardware, checkpoint paths, and optimization
flags are retained in `optimized_wallclock_5000.json`.

The timing fields in the controlled evaluator JSON files are not deployment
benchmarks. That diagnostic path transfers token context through CPU/NumPy and
calls `torch.cuda.synchronize()` after every GPU stage so that each component
can be timed independently; those operations are absent from the optimized
live path and intentionally make the evaluator slower.

## Scope

These 300 attempts quantify repeated execution of one frozen policy on three
deterministic levels of increasing difficulty. Results are reported per level
and are not normalized for obstacle layout or mechanics, so they should not be
interpreted as a controlled generalization comparison. They measure deployment
variability, not uncertainty over training seeds. No causal SLS claim is made
from this evaluation.

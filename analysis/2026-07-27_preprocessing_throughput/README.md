# Exact CUDA preprocessing and final deployment profile

This directory records the final preprocessing optimization on the deployment
RTX 2060. The candidate preserves CPU OpenCV grayscale conversion, computes
Sobel magnitude on CUDA, reproduces OpenCV area-resize accumulation order with
a fixed-shape Triton kernel, and keeps the normalized 64x64 observation on the
GPU.

## Correctness gate

- 128/128 synthetic crops produced exactly identical 64x64 uint8 outputs.
- 100/100 fresh live captures produced exactly identical outputs.
- Across both gates, zero output bytes differed from the original pipeline.

## Matched 60 FPS comparison

Both 25-attempt runs use the historical frozen V7 PPO controller on Stereo
Madness Copy. They differ only in preprocessing implementation.

| Path | Mean full loop | p95 full loop | Mean frames | Mean cadence-normalized survival |
| --- | ---: | ---: | ---: | ---: |
| Hybrid CUDA Sobel + CPU resize | 14.395 ms | 16.424 ms | 1,022.5 | 17.04 s |
| Exact CUDA | 11.907 ms | 14.043 ms | 1,032.8 | 17.21 s |

The exact path reduces mean latency by 17.3% and p95 latency by 14.5%.
Survival remains in the same recurring obstacle-driven 15--21 s regime.

## Final 5,000-frame profile

The final profile was collected at the configured 60 FPS cadence while the
game remained visible. Deliberate cadence sleep is excluded.

| Statistic | Value |
| --- | ---: |
| Mean [95% bootstrap CI] | 12.018 [11.982, 12.056] ms |
| Median | 12.083 ms |
| p95 | 14.069 ms |
| Maximum | 24.059 ms |
| Frames over 16.667 ms | 11/5,000 |
| Reciprocal-mean compute throughput | 83.2 FPS |

The 83.2 FPS figure is compute-equivalent throughput, not an evaluated control
cadence. Live control remains evaluated at 60 FPS. The p95 latency exceeds the
10 ms budget required for a robust 100 FPS claim.

## Artifacts

- `hybrid_60fps_25.json`: matched hybrid baseline and 300 latency samples.
- `exact_cuda_60fps_25.json`: matched exact-CUDA survival run and 300 latency samples.
- `exact_cuda_60fps_profile_5000.json`: final 5,000-frame exact-CUDA latency profile.

# Experiment / deployment machine facts (issue #31)

Verified on the live experiment PC on 2026-07-22.

## Host

| Item | Value |
| --- | --- |
| CPU | AMD Ryzen 5 3600X 6-Core Processor (6C/12T) |
| RAM | 16 GB (17129512960 bytes) |
| Motherboard | Micro-Star International MS-7C02 |
| OS | Microsoft Windows 11 Professionnel, build 10.0.26200, 64-bit |
| GPU | NVIDIA GeForce RTX 2060 SUPER |
| VRAM | 8192 MiB |
| NVIDIA driver | 596.49 |
| Display | 1920?1080 @ 144 Hz (primary) |

The archived latency artifact and live-eval JSONs already report **RTX 2060 SUPER**; older README/paper shorthand ?RTX 2060? should be read as that SUPER SKU.

## Software stack (verified)

| Item | Value |
| --- | --- |
| Python | 3.11.14 (Anaconda) |
| PyTorch | **2.11.0+cu126** |
| CUDA (PyTorch build) | 12.6 |
| Screen capture | `dxcam` region `(0, 0, 1920, 1080)` |
| Crop | `x=660, y=48, size=1032` ? 1032?1032 before Sobel/downscale to 64?64 |
| Input dispatch | `keyboard` (space press/release) |
| Game display mode (archived + current desktop) | 1920?1080 fullscreen indication in prior eval notes; current desktop 1920?1080 |

PyTorch discrepancy resolution: the optimized deployment artifact already recorded `torch_version: 2.11.0+cu126`. README text saying PyTorch 2.10 was stale and should be corrected to 2.11.0+cu126.

## Optimized inference settings (from `optimized_wallclock_5000.json`)

- FSQ `torch.compile` enabled
- Transformer CUDA Graph enabled for context encode
- GPU-resident context + pinned frame buffer
- No per-stage CUDA synchronization on the optimized path
- Warmup: first context-fill frames excluded
- Cadence: 30 FPS; deliberate sleep excluded from latency

## Model sizes (state-dict parameter counts on disk checkpoints)

| Checkpoint | Params | File size |
| --- | ---: | ---: |
| `fsq_best.pt` | 1,887,465 | 7.3 MB |
| `transformer_best.pt` | 15,232,321 | 56.4 MB |
| `controller_ppo_best.pt` | 45,546 | 182 KB |
| `controller_bc_epoch9_reconstructed.pt` | 45,546 | 182 KB |

World-model training log also reported 14,723,520 parameters for the transformer module configuration used at train time; the on-disk checkpoint tensor count above is the loaded state-dict total.

## Training wall-clock from local stage logs (sum of per-step `time_s`)

These are log-derived stage sums on the archived CSV traces, not a full multi-node accounting:

| Stage | Approx. wall time from log |
| --- | ---: |
| FSQ (1000 epochs) | 3.19 h |
| Transformer (200 epochs) | 4.20 h |
| BC (50 epochs) | 0.01 h (~5 min) |
| PPO (to iter ~4997 in log) | 5.22 h |
| **Sum of logged stages** | **~12.6 h** |

Primary training hardware remains one A100 (paper/setup); the above times come from the retained CSV logs.

## Expert-subset frame/duration discrepancy

Verified from `data/deepdash/expert_episodes`:

- 36 expert episodes
- **33,153** frames total (`frames.npy` lengths)
- Sum of metadata `duration_s` = **1137.09 s = 18.95 minutes**
- At exact 30 FPS, 33,153 frames = **18.418 minutes (~18.42)**
- Implied average capture rate = 33153 / 1137.09 ? **29.16 FPS**
- Mean metadata `fps_actual` ? **28.55 FPS** (per-episode average, unweighted)

Conclusion: keep **both** numbers. 33,153 frames is the frame count; 18.95 minutes is wall-clock capture duration; the difference is below-nominal capture FPS, not an arithmetic error in one quantity alone.

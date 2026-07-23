# 60 FPS cadence probe: Stereo Madness Copy

Date: 2026-07-23

## Role

Compare frozen V7 PPO survival on **Stereo Madness Copy** at:

- 30 FPS (prior diagnostic-path baseline)
- 60 FPS (optimized deploy-equivalent path)

Training data remain 30-FPS captures. This is a **deployment cadence probe**, not a broad frame-rate adaptation study.

## Protocol

- Level: Stereo Madness Copy (KRUTOYARBUS)
- Policy: frozen PPO (`checkpoints_v7/controller_ppo_best.pt`)
- Sample size: 20 scored attempts (+1 excluded sync episode)
- 60 FPS path: `scripts/eval_real_game.py --inference-path optimized --fps 60`
  - FSQ `torch.compile`
  - transformer CUDA graph
  - GPU-resident context
  - no per-stage CUDA syncs
- Compare recorded episode wall time, not frame counts (which scale with cadence)

## Results

| Cadence | Path | Mean frames | Cadence-normalized seconds (`frames/fps`) | Recorded mean wall time [95% CI] | Median wall time |
| --- | --- | ---: | ---: | ---: | ---: |
| 30 FPS | diagnostic | 525.6 | 17.52 | **17.79 [17.06, 18.55]** | 18.64 |
| 60 FPS | optimized | 998.4 | 16.64 | **17.03 [16.32, 17.79]** | 15.61 |

60 FPS latency on this probe: mean full-loop **14.4 ms** (fits 16.7 ms budget).

The recorded mean wall-time difference (60 FPS minus 30 FPS) is -0.75 s with a 95% bootstrap interval of [-1.80, 0.31] s. Both conditions die in the same recurring ~15--21 s band, indicating the same obstacle-driven failure modes rather than a different control regime.

## Claim supported

Practical **60 FPS live deployment from 30-FPS training data** with roughly comparable wall-clock survival on this layout.

## Claim not supported

General "frame-rate adaptability" as a core scientific contribution. This is one level, one policy, n=20.

## Artifacts

- `ppo_stereo_madness_copy_20.json` (this directory)
- 30 FPS baseline: `../2026-07-22_ship_segment_eval/ppo_stereo_madness_copy_20.json`

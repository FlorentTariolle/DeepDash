# Frozen world-model held-out diagnostics

These are post-hoc diagnostics on the fixed global validation split. The split was excluded from gradient updates, but its death F1 was used historically to select the dynamics checkpoint. The action intervention and autoregressive metrics were not selection criteria.

## Matched CPC

- InfoNCE: 0.3182 nats
- Windows: 22,528 (discarded incomplete batch: 217)
- Batch size / in-batch negative set: 512
- Fixed permutation seed: 20260728

## One-step prediction

- Windows: 22,745
- Visual-token NLL: 2.6044 nats/token
- Visual-token accuracy: 29.01%
- Exact 8x8 grid accuracy: 0.12%

## Death/status prediction

- Positive terminal transitions: 410
- AUROC: 0.9939
- Average precision: 0.8750
- F1 at 0.5: 0.8078
- Brier score: 0.0091
- 15-bin ECE: 0.0459

## Paired action intervention

Only the final binary action is flipped while the recorded visual context is held fixed. Positive NLL advantage means that the factual action assigns more likelihood to the observed next frame.

- Episode-mean factual NLL advantage: 0.1958 nats/token
- Episode-bootstrap 95% CI: [0.1776, 0.2143]
- Windows favoring factual action: 69.63%
- Predicted tokens changed by the flip: 13.40%

## Autoregressive fidelity

Greedy predictions are fed back while replaying the recorded future actions. Exact trajectory agreement is a fidelity diagnostic, not by itself a measure of perceptual plausibility after trajectories diverge.

| Cohort | Horizon | N | Token accuracy | Decoded PSNR | Token-marginal JS |
|---|---:|---:|---:|---:|---:|
| standard | 1 | 1024 | 29.06% | 32.52 dB | 0.0048 |
| standard | 5 | 1024 | 18.39% | 28.19 dB | 0.0057 |
| standard | 10 | 1024 | 13.39% | 25.19 dB | 0.0076 |
| standard | 20 | 1024 | 7.81% | 22.39 dB | 0.0107 |
| standard | 45 | 1024 | 3.01% | 19.96 dB | 0.0287 |
| extended | 45 | 256 | 3.22% | 19.76 dB | 0.0470 |
| extended | 100 | 256 | 1.32% | 19.83 dB | 0.0630 |
| extended | 200 | 256 | 0.96% | 19.62 dB | 0.0987 |

The extended cohort contains only trajectories long enough for its maximum requested horizon; consult `diagnostics.json` for source and episode counts before generalizing its results.

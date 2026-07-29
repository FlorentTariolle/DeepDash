# Frozen world-model held-out diagnostics

These are post-hoc diagnostics on the fixed global validation split. The split was excluded from gradient updates, but its death F1 was used historically to select the dynamics checkpoint. The action intervention and autoregressive metrics were not selection criteria.

## One-step prediction

- Windows: 22,745
- Visual-token NLL: 2.6133 nats/token
- Visual-token accuracy: 29.58%
- Exact 8x8 grid accuracy: 0.11%

## Death/status prediction

- Positive terminal transitions: 410
- AUROC: 0.9944
- Average precision: 0.8916
- F1 at 0.5: 0.7930
- Brier score: 0.0095
- 15-bin ECE: 0.0498

## Paired action intervention

Only the final binary action is flipped while the recorded visual context is held fixed. Positive NLL advantage means that the factual action assigns more likelihood to the observed next frame.

- Episode-mean factual NLL advantage: 0.1652 nats/token
- Episode-bootstrap 95% CI: [0.1498, 0.1812]
- Windows favoring factual action: 69.25%
- Predicted tokens changed by the flip: 11.03%

## Autoregressive fidelity

Greedy predictions are fed back while replaying the recorded future actions. Exact trajectory agreement is a fidelity diagnostic, not by itself a measure of perceptual plausibility after trajectories diverge.

| Cohort | Horizon | N | Token accuracy | Decoded PSNR | Token-marginal JS |
|---|---:|---:|---:|---:|---:|
| standard | 1 | 1024 | 30.12% | 32.82 dB | 0.0037 |
| standard | 5 | 1024 | 19.23% | 28.24 dB | 0.0044 |
| standard | 10 | 1024 | 14.04% | 25.21 dB | 0.0050 |
| standard | 20 | 1024 | 8.39% | 22.48 dB | 0.0078 |
| standard | 45 | 1024 | 2.86% | 20.00 dB | 0.0205 |
| extended | 45 | 256 | 2.91% | 19.84 dB | 0.0412 |
| extended | 100 | 256 | 1.49% | 19.62 dB | 0.0498 |
| extended | 200 | 256 | 0.76% | 19.57 dB | 0.0880 |

The extended cohort contains only trajectories long enough for its maximum requested horizon; consult `diagnostics.json` for source and episode counts before generalizing its results.

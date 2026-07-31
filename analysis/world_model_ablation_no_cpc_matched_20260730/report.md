# Frozen world-model held-out diagnostics

These are post-hoc diagnostics on the fixed global validation split. The split was excluded from gradient updates, but its death F1 was used historically to select the dynamics checkpoint. The action intervention and autoregressive metrics were not selection criteria.

## Matched CPC

- Unavailable: checkpoint has no CPC modules

## One-step prediction

- Windows: 22,745
- Visual-token NLL: 2.6340 nats/token
- Visual-token accuracy: 28.39%
- Exact 8x8 grid accuracy: 0.12%

## Death/status prediction

- Positive terminal transitions: 410
- AUROC: 0.9947
- Average precision: 0.8690
- F1 at 0.5: 0.7874
- Brier score: 0.0089
- 15-bin ECE: 0.0427

## Paired action intervention

Only the final binary action is flipped while the recorded visual context is held fixed. Positive NLL advantage means that the factual action assigns more likelihood to the observed next frame.

- Episode-mean factual NLL advantage: 0.1483 nats/token
- Episode-bootstrap 95% CI: [0.1346, 0.1631]
- Windows favoring factual action: 68.71%
- Predicted tokens changed by the flip: 11.97%

## Autoregressive fidelity

Greedy predictions are fed back while replaying the recorded future actions. Exact trajectory agreement is a fidelity diagnostic, not by itself a measure of perceptual plausibility after trajectories diverge.

| Cohort | Horizon | N | Token accuracy | Decoded PSNR | Token-marginal JS |
|---|---:|---:|---:|---:|---:|
| standard | 1 | 1024 | 28.47% | 32.52 dB | 0.0047 |
| standard | 5 | 1024 | 17.78% | 28.06 dB | 0.0060 |
| standard | 10 | 1024 | 12.60% | 25.00 dB | 0.0070 |
| standard | 20 | 1024 | 7.68% | 22.28 dB | 0.0104 |
| standard | 45 | 1024 | 2.90% | 19.93 dB | 0.0240 |
| extended | 45 | 256 | 2.83% | 19.77 dB | 0.0468 |
| extended | 100 | 256 | 1.57% | 19.95 dB | 0.0500 |
| extended | 200 | 256 | 0.95% | 19.69 dB | 0.0683 |

The extended cohort contains only trajectories long enough for its maximum requested horizon; consult `diagnostics.json` for source and episode counts before generalizing its results.

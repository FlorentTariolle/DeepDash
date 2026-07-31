# Frozen world-model held-out diagnostics

These are post-hoc diagnostics on the fixed global validation split. The split was excluded from gradient updates, but its death F1 was used historically to select the dynamics checkpoint. The action intervention and autoregressive metrics were not selection criteria.

## Matched CPC

- InfoNCE: 0.2683 nats
- Windows: 22,528 (discarded incomplete batch: 217)
- Batch size / in-batch negative set: 512
- Fixed permutation seed: 20260728

## One-step prediction

- Windows: 22,745
- Visual-token NLL: 2.6134 nats/token
- Visual-token accuracy: 29.58%
- Exact 8x8 grid accuracy: 0.11%

## Death/status prediction

- Positive terminal transitions: 410
- AUROC: 0.9945
- Average precision: 0.8914
- F1 at 0.5: 0.7939
- Brier score: 0.0095
- 15-bin ECE: 0.0498

## Paired action intervention

Only the final binary action is flipped while the recorded visual context is held fixed. Positive NLL advantage means that the factual action assigns more likelihood to the observed next frame.

- Episode-mean factual NLL advantage: 0.1652 nats/token
- Episode-bootstrap 95% CI: [0.1499, 0.1812]
- Windows favoring factual action: 69.52%
- Predicted tokens changed by the flip: 11.04%

## Autoregressive fidelity

Greedy predictions are fed back while replaying the recorded future actions. Exact trajectory agreement is a fidelity diagnostic, not by itself a measure of perceptual plausibility after trajectories diverge.

| Cohort | Horizon | N | Token accuracy | Decoded PSNR | Token-marginal JS |
|---|---:|---:|---:|---:|---:|
| standard | 1 | 1024 | 30.14% | 32.83 dB | 0.0037 |
| standard | 5 | 1024 | 19.29% | 28.25 dB | 0.0045 |
| standard | 10 | 1024 | 14.06% | 25.23 dB | 0.0052 |
| standard | 20 | 1024 | 8.26% | 22.45 dB | 0.0079 |
| standard | 45 | 1024 | 2.91% | 19.99 dB | 0.0208 |
| extended | 45 | 256 | 3.02% | 19.78 dB | 0.0351 |
| extended | 100 | 256 | 1.38% | 19.49 dB | 0.0428 |
| extended | 200 | 256 | 0.74% | 19.39 dB | 0.0733 |

The extended cohort contains only trajectories long enough for its maximum requested horizon; consult `diagnostics.json` for source and episode counts before generalizing its results.

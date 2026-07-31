# Frozen world-model held-out diagnostics

These are post-hoc diagnostics on the fixed global validation split. The split was excluded from gradient updates, but its death F1 was used historically to select the dynamics checkpoint. The action intervention and autoregressive metrics were not selection criteria.

## Matched CPC

- InfoNCE: 0.4590 nats
- Windows: 22,528 (discarded incomplete batch: 217)
- Batch size / in-batch negative set: 512
- Fixed permutation seed: 20260728

## One-step prediction

- Windows: 22,745
- Visual-token NLL: 2.6753 nats/token
- Visual-token accuracy: 27.93%
- Exact 8x8 grid accuracy: 0.12%

## Death/status prediction

- Positive terminal transitions: 410
- AUROC: 0.9965
- Average precision: 0.8917
- F1 at 0.5: 0.8259
- Brier score: 0.0052
- 15-bin ECE: 0.0040

## Paired action intervention

Only the final binary action is flipped while the recorded visual context is held fixed. Positive NLL advantage means that the factual action assigns more likelihood to the observed next frame.

- Episode-mean factual NLL advantage: 0.1563 nats/token
- Episode-bootstrap 95% CI: [0.1417, 0.1711]
- Windows favoring factual action: 70.06%
- Predicted tokens changed by the flip: 13.12%

## Autoregressive fidelity

Greedy predictions are fed back while replaying the recorded future actions. Exact trajectory agreement is a fidelity diagnostic, not by itself a measure of perceptual plausibility after trajectories diverge.

| Cohort | Horizon | N | Token accuracy | Decoded PSNR | Token-marginal JS |
|---|---:|---:|---:|---:|---:|
| standard | 1 | 1024 | 28.12% | 32.67 dB | 0.0051 |
| standard | 5 | 1024 | 16.49% | 27.69 dB | 0.0067 |
| standard | 10 | 1024 | 11.29% | 24.71 dB | 0.0087 |
| standard | 20 | 1024 | 6.36% | 22.05 dB | 0.0141 |
| standard | 45 | 1024 | 2.24% | 19.92 dB | 0.0358 |
| extended | 45 | 256 | 2.49% | 19.80 dB | 0.0506 |
| extended | 100 | 256 | 1.17% | 20.00 dB | 0.0616 |
| extended | 200 | 256 | 0.72% | 19.72 dB | 0.0752 |

The extended cohort contains only trajectories long enough for its maximum requested horizon; consult `diagnostics.json` for source and episode counts before generalizing its results.

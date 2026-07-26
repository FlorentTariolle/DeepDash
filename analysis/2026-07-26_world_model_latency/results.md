# Results

Environment: NVIDIA GeForce RTX 2060 SUPER (8191.7 MiB), PyTorch
2.11.0+cu126, batch size 1, FP32 eager execution, 30 warm-up transitions and
100 synchronized measured transitions per run.

| Model | Run 1 mean (ms) | Run 2 mean (ms) | Mean of run means (ms) | Peak allocated (MiB) |
|---|---:|---:|---:|---:|
| DashVMC | 11.0372 | 11.0561 | 11.0466 | 82.14 |
| DIAMOND | 50.1273 | 48.4636 | 49.2955 | 89.10 |
| IRIS | 176.9367 | 178.7258 | 177.8312 | 86.81 |

Operations:

- DashVMC: parallel 64-token latent grid and death score.
- DIAMOND: three-step pixel diffusion followed by reward and termination
  prediction.
- IRIS: 16 autoregressive tokens followed by observation decoding and reward
  and termination prediction.

The run-level p95 measurements were 11.6180/11.3012 ms for DashVMC,
56.2202/50.7588 ms for DIAMOND, and 202.8840/209.9185 ms for IRIS.

The models produce different representations, and IRIS and DIAMOND were not
trained on the DashVMC corpus. These results characterize the operational cost
of the released imagination interfaces; they do not compare generation quality
or Geometry Dash control performance.

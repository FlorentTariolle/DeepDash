# Uniform-smoothing dynamics training provenance

The uniform-label-smoothing comparator was trained once on CRIANN as SLURM
job `2771331` (`v7_wm_uniform`). The job completed successfully with exit code
`0:0` after `04:10:55`.

## Configuration and selection

- Checkpoint directory: `checkpoints_v7_ablation_uniform_smoothing`
- Label smoothing: `0.1`
- FSQ smoothing bandwidth: `0.0` (uniform non-target mass)
- CPC weight: `0.1`
- Training seed: `42`
- Schedule: 200 epochs, 500 steps per epoch
- Selection rule: maximum validation death F1
- Selected epoch: `49`
- Selected validation death F1: `0.8191`
- Best-checkpoint SHA-256:
  `e6c9dc386f8511f1cad0c1f8f2446d06662c8b07367ee18c25b53336df2beae7`

The checkpoint itself was not downloaded.

## Matched evaluation

The final baseline, uniform-smoothing, no-smoothing, and no-CPC diagnostics
were jobs `2771345`, `2771339`, `2771346`, and `2771347`, respectively. All
four completed with exit code `0:0`. The evaluator installed on CRIANN had
SHA-256
`10948de2eb6c2761bda88d7f738d7b3477d8896862e79bbb7057d9ca24e502b2`.

Validation confirmed identical tokenizer hashes, data split and counts,
one-step protocol, evaluation seed, rollout cohorts and horizons, and exact
standard and extended sample manifests across all four checkpoints. Matched
CPC uses a fixed permutation seed of `20260728`, batch size 512, and 22,528
complete validation windows; it is unavailable for the no-CPC checkpoint.

# BC epoch-9 checkpoint reconstruction

The original intermediate behavioural-cloning checkpoint was not present in the
repository, Git tags, Git LFS, or the remaining cluster filesystem. The live BC
control therefore uses an explicitly labelled reconstruction rather than claiming
to use the unavailable original artifact.

The reconstruction ran under SLURM job `2660971` on an A100 from tag `V7`
(`b636bcd4`), using seed 42, the archived training configuration, the original
4,228 death and 36 expert episode corpus, and the selected tokenizer and world
model checkpoints. The loader retained 3,781 usable episodes, producing 183,848
training and 19,593 validation samples. Training still ran for 50 epochs; the
added snapshot option only retained the epoch-9 state used by the control.

## Artifact identity

| Artifact | SHA-256 |
| --- | --- |
| `checkpoints_v7/fsq_best.pt` | `8a4c488e0310855bcf411787894f0206f3381387bcd9fc179d6a6610ef32e5f7` |
| `checkpoints_v7/transformer_best.pt` | `62db7d8f0dca5cb75684548c2e93a74c4f6dda830250f171e38fe97161dcd770` |
| `checkpoints_v7/controller_bc_epoch9_reconstructed.pt` | `60e56c2345aa26dd2febe2b66f6e0c9dd7f35473f55fbc1f6914ed69f5e4d3fb` |

## Trace comparison at epoch 9

| Trace | Train loss | Train accuracy | Validation loss | Validation accuracy |
| --- | ---: | ---: | ---: | ---: |
| Archived | 0.501087 | 0.8242 | 0.558837 | 0.7976 |
| Reconstruction | 0.503075 | 0.8233 | 0.560845 | 0.7985 |

The traces are close but not bit-identical, so all public references call this
checkpoint a reconstruction. `controller_bc_args.json`, the reconstructed CSV
trace, and the SLURM stdout/stderr files in this directory retain the execution
evidence.

# Archived tokenizer-to-world-model training provenance

This record documents the tokenizer and token dataset used for the reported
world-model checkpoint. It also explains why old `tokens.npy` files in a local
working tree are not evidence of a mismatch with the shipped tokenizer.

## Selected tokenizer

- Checkpoint: `checkpoints_v7/fsq_best.pt`
- Artifact commit: `150788809343f77180ee2f83d6a1de30d683a4bc`
- Commit timestamp: 2026-04-26 22:56:16 CEST
- SHA-256: `8a4c488e0310855bcf411787894f0206f3381387bcd9fc179d6a6610ef32e5f7`
- FSQ levels: `[8, 5, 5, 5]` (1,000 visual codes)

## Archived training sequence

The archived SLURM logs establish the sequence independently of any token
caches currently present on a developer machine:

1. Tokenization job 1977852 loaded `checkpoints_v7/fsq_best.pt` and started at
   22:58:41 CEST on April 26, 2026. It found all 4,228 death episodes and
   generated 218,810 new base-frame token grids.
2. The same job loaded the same checkpoint for all 36 expert episodes and
   generated 33,153 new base-frame token grids. The job completed successfully
   at 23:01:49 CEST.
3. Tokenization created the base version plus four vertical shifts for every
   episode. The resulting count is exactly
   `(4,228 + 36) * (1 base + 4 shifts) = 21,320` tokenized episodes.
4. The world-model job began at 00:22:16 CEST on April 27 and reported exactly
   21,320 tokenized episodes, split into 19,195 training and 2,125 validation
   episodes. It reported 1,061,890 unique training samples, 113,725 validation
   samples, and a vocabulary of 1,000 visual plus two status tokens.

The relevant contemporaneous output and checkpoint identity are preserved in
the tracked [`EVIDENCE.txt`](EVIDENCE.txt) extract. The raw per-episode SLURM
logs remain in the local archive but are excluded from Git because of their
size.
The tokenizer artifact was committed before tokenization began, tokenization
finished before world-model training began, and the produced and consumed
episode counts agree exactly.

## Stale local caches

Some untracked local `tokens.npy` files predate the selected tokenizer. They
are leftovers from earlier development runs and were not the token dataset
consumed by the archived world-model job. Comparing those files with a fresh
encoding from the selected checkpoint therefore does not test the reported
training pairing. Current dream generation and continuation-figure code load
frames and encode them through the selected FSQ checkpoint at runtime.

The tokenization script now writes a `tokens.meta.json` sidecar containing the
tokenizer SHA-256, FSQ levels, source episode, shift, frame count, and token
shape. A cache is reused only when this metadata matches the requested
tokenizer and source; legacy, incomplete, or mismatched caches are regenerated.
This makes the tokenizer-token relationship directly machine-checkable in
future runs.

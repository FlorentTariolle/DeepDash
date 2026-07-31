# V7 dynamics-model ablation protocol

## Question

Measure the effect of structured label smoothing (SLS) and
action-conditional contrastive predictive coding (CPC) on the frozen V7
dynamics model without retraining a controller.

## Matched variants

| Variant | Label smoothing | CPC |
|---|---|---:|
| Reported V7 baseline | Structured FSQ-lattice, 0.1 | 0.1 |
| Uniform smoothing | PyTorch uniform smoothing, 0.1 | 0.1 |
| No smoothing | Disabled | 0.1 |
| No CPC | Structured FSQ-lattice, 0.1 | Disabled |

The three targeted variants retain the reported tokenizer, tokenized corpus,
split, seed 42, architecture, optimizer, learning-rate schedule, 200 epochs,
500 steps per epoch, corruption rates, focal modulation, death oversampling,
and death-F1 checkpoint selection. Only the named loss component changes.
The uniform variant sets `label_smoothing=0.1` and `fsq_sigma=0.0`; the
no-smoothing variant sets `label_smoothing=0`; the no-CPC variant disables
the CPC modules.

The reported baseline is directly comparable rather than a legacy
approximation. Its training began after commit `572a97d2` set the V7 recipe to
SLS 0.1 and CPC 0.1, and its archived checkpoint (commit `ae5a8536`) contains
the CPC target projection and four CPC predictor heads. Later training-code
changes only shuffled the validation loader to make the reported CPC metric
less sensitive to temporally correlated batches and moved data paths; they did
not change optimization or death-F1 checkpoint selection. All four checkpoints
are nevertheless scored post hoc by the same matched diagnostics evaluator.

## Evaluation

Run `scripts/eval_world_model_diagnostics.py` on the same unaugmented
base-episode validation stratum for all four checkpoints. The matched CPC
endpoint uses one fixed window permutation, batch size 512, and the same
in-batch negative sets for every checkpoint with CPC modules.

Primary model-level endpoints:

1. hard-target visual-token NLL and token accuracy;
2. death AUROC, average precision, F1, Brier score, and ECE;
3. paired factual-versus-flipped-action NLL advantage, fraction of contexts
   favoring the factual action, and fraction of argmax predictions changed;
4. recorded-action autoregressive token accuracy, decoder-space PSNR, and
   pooled-token JS divergence at horizons 1, 5, 10, 20, and 45.

Also record selected epoch, training wall time, and parameter count.

## Scope

This ablation measures effects on the dynamics model. It does not establish
that a model-level difference changes PPO or live-game performance because the
controller is deliberately not retrained. Death F1 is a development estimate:
the same stratum selects the dynamics checkpoint. The action-intervention and
autoregressive endpoints are post-hoc and were not checkpoint-selection
criteria.

## CRIANN jobs

- Retokenization: `2770701` (completed; 21,320 episode variants).
- No-smoothing training and original diagnostics: `2770702`--`2770703`.
- No-CPC training and original diagnostics: `2770704`--`2770705`.
- Original baseline diagnostics: `2770710`.
- Uniform-smoothing training: `2771331` (completed, exit `0:0`).
- Matched uniform diagnostics: `2771339` (completed, exit `0:0`).
- Matched baseline diagnostics: `2771345` (completed, exit `0:0`).
- Matched no-smoothing diagnostics: `2771346` (completed, exit `0:0`).
- Matched no-CPC diagnostics: `2771347` (completed, exit `0:0`).

The matched evaluator installed for the final four jobs has SHA-256
`10948de2eb6c2761bda88d7f738d7b3477d8896862e79bbb7057d9ca24e502b2`.

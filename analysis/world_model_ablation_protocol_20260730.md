# V7 dynamics-model ablation protocol

## Question

Measure the effect of structured label smoothing (SLS) and
action-conditional contrastive predictive coding (CPC) on the frozen V7
dynamics model without retraining a controller.

## One-factor-at-a-time variants

| Variant | SLS | CPC |
|---|---:|---:|
| Reported V7 baseline | 0.1 | 0.1 |
| No SLS | 0.0 | 0.1 |
| No CPC | 0.1 | disabled |

Both ablations retain the reported tokenizer, tokenized corpus, split, seed 42,
architecture, optimizer, learning-rate schedule, 200 epochs, 500 steps per
epoch, corruption rates, focal modulation, death oversampling, and
death-F1 checkpoint selection. Only the named factor changes.

The reported baseline is directly comparable rather than a legacy
approximation. Its training began after commit `572a97d2` set the V7 recipe to
SLS 0.1 and CPC 0.1, and its archived checkpoint (commit `ae5a8536`) contains
the CPC target projection and four CPC predictor heads. Later training-code
changes only shuffled the validation loader to make the reported CPC metric
less sensitive to temporally correlated batches and moved data paths; they did
not change optimization or death-F1 checkpoint selection. All three
checkpoints are nevertheless scored post hoc by the same current diagnostics
script.

## Evaluation

Run `scripts/eval_world_model_diagnostics.py` on the same unaugmented
base-episode validation stratum for all three checkpoints.

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

## Arctic jobs

- Retokenization: `2770701` (completed; 21,320 episode variants).
- No-SLS training: `2770702`.
- No-SLS diagnostics: `2770703`, dependent on `2770702`.
- No-CPC training: `2770704`, dependent on `2770703`.
- No-CPC diagnostics: `2770705`, dependent on `2770704`.
- Baseline diagnostics: `2770710`, dependent on `2770705`.

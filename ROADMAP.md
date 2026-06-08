# Roadmap

Last updated from local artifacts, IRIS runs, and GitHub issues on 2026-06-08.

## Current Paper Direction

The paper now has two contributions with different evidentiary paths.

1. **Annealed Structured Label Smoothing (SLS)** is the method contribution. SLS is framed as a tokenizer-metric-aware replacement for hard CE targets in discrete-token world models. The target distribution puts smoothing mass on nearby tokens under a meaningful token metric, then anneals the smoothing mass back to zero so the objective becomes ordinary CE late in training.
2. **Geometry Dash V7** is the application contribution. It remains the real-time Vision-Model-Controller system where the FSQ lattice originally exposed the near-miss problem: FSQ tokenization, action-conditioned dynamics, imagined rollout/control, procedural rollout or level-continuation generation, and 30 FPS deployment through screen capture.

The old FSQ-only claim is no longer the headline. FSQ remains the motivating and Geometry Dash-specific metric: FSQ codes have an integer coordinate lattice, so SLS can use lattice distance. The broader claim is that SLS applies whenever the tokenizer supplies a useful token neighbourhood, such as codebook or embedding distance in VQ-style models.

Source issues: #6, #13, #14, #15, #16, #21.

## Current Empirical State

The V7-native Atari controller path is retired for the paper. It produced a long reward-head/controller-debug loop and should not be the benchmark vehicle for SLS.

The active benchmark path is a minimal SLS-vs-CE patch on an accepted world-model baseline. The first controlled baseline is IRIS/Pong with architecture, optimizer, data, compile mode, BF16 mode, and training budget held fixed while changing only the target distribution.

Completed IRIS/Pong single-seed runs:

| Condition | Smoothing | Schedule | Final eval return | Best eval return | Read |
| --- | ---: | --- | ---: | ---: | --- |
| CE | 0.0 | pure CE | 20.9375 | 20.9375 @ epoch 600 | Strong asymptote, but unstable mid-training |
| Fixed SLS | 0.1 | constant to epoch 600 | 0.125 | low single digits | Stabilizes the bad CE window, but over-regularizes and fails asymptotically |
| Annealed SLS | 0.1 | hold to epoch 250, cosine to 0 by epoch 450, pure CE to epoch 600 | 19.625 | 19.8125 @ epoch 570 | Keeps much of the stability/sample-efficiency gain and recovers near-CE final performance |

Useful current KPIs from the first matched CE vs annealed SLS run:

- Mean return delta over epochs 250-420: annealed SLS `+7.15625` over CE.
- Eval-return AUC: CE `-993.0208`, annealed SLS `-408.90625`.
- First return >= 10: CE epoch 380, annealed SLS epoch 350.
- First return >= 18: CE epoch 430, annealed SLS epoch 505.
- Mean return from epoch 500 onward: CE `19.6042`, annealed SLS `17.0923`.
- Final return: CE `20.9375`, annealed SLS `19.625`.

Interpretation:

- Fixed SLS is a negative ablation, not the final method.
- Annealing is part of the method, because it preserves early smoothing while removing the late bias toward soft targets.
- The current best claim is not "SLS always improves final return." The defensible claim is "annealed SLS can reduce training brittleness and improve early/mid training stability while recovering near-CE asymptotic performance."

Local plot/metric artifacts outside the repo root:

- `iris_ce_vs_sls_anneal_returns_20260608.png`
- `iris_ce_vs_sls_anneal_diagnostics_20260608.png`
- `iris_ce_vs_sls_anneal_summary_20260608.json`
- `iris_ce_sls_anneal_metrics_20260608.json`

## Immediate Next Steps

1. **Turn the first IRIS result into reviewer-grade evidence.**
   - Run more seeds for Pong.
   - Prefer at least one additional Atari game once Pong seeds are stable.
   - Keep CE, fixed SLS, and annealed SLS in the table: fixed SLS explains why the anneal is necessary.

2. **Lock the KPI story before expanding experiments.**
   - Primary KPI: stability/sample-efficiency under matched training budget.
   - Candidate metrics: return AUC, collapse-window mean delta, rolling return variance, first epoch reaching thresholds, final/tail mean return.
   - Final return should remain reported, but should not be the only claim.

3. **Update the paper results section.**
   - Add the IRIS/Pong table and curves.
   - State clearly that the current result is one seed until seed expansion lands.
   - Explain fixed SLS as over-regularization and annealed SLS as the actual method.

4. **Preserve the Geometry Dash contribution without overclaiming it as the SLS benchmark.**
   - Keep V7 frozen.
   - Add evidence of high-quality predictions and generated rollouts/level continuations.
   - Report latency and real-time deployment.
   - Do not use Geometry Dash deploy-survival as the primary SLS effect-size metric.

5. **Clean the issue tracker around this direction.**
   - #6 should describe the split paper and annealed SLS framing.
   - #13 should record IRIS/Pong as selected baseline/task, with seeds/games still to expand.
   - #14 should become the matched CE vs fixed SLS vs annealed SLS benchmark task.
   - #15 should prioritize ablations that explain the annealed objective.
   - #16 should be optional generalization beyond first baseline, not paper-blocking.
   - #21 should be closeable once README/paper/roadmap/issues consistently retire V7-native Atari.

## Method Definition To Use In The Paper

For a target token `i`, a token metric `d(i, j)`, a kernel `k`, and smoothing mass `epsilon_t`:

```text
q_t(j | i) = 1 - epsilon_t                                      if j = i
q_t(j | i) = epsilon_t * k(d(i, j)) / sum_{l != i} k(d(i, l))    otherwise
```

The current annealed schedule is:

```text
epochs 25-250:  epsilon_t = 0.10, topk = 16
epochs 250-450: cosine anneal epsilon_t from 0.10 to 0.00
epochs 450-600: epsilon_t = 0.00, pure CE
```

For FSQ, `d` is lattice distance. For IRIS-style VQ tokenizers, `d` can be codebook or embedding distance. The phrase to use is **tokenizer-metric-aware**, not unqualified **encoder-agnostic**.

## Scope Boundaries

In scope:

- Annealed SLS on accepted discrete-token world-model baselines.
- Fixed SLS as an ablation.
- Uniform label smoothing and kernel/top-k/schedule ablations if time allows.
- Geometry Dash V7 as the application system.
- Procedural rollout or level-continuation generation from the Geometry Dash world model.

Out of scope for this paper:

- Full V7-native Atari controller port.
- More reward-head calibration/debug cycles as the default next action.
- Geometry Dash deploy-survival as the primary SLS benchmark metric.
- Large architecture changes that confound the CE vs SLS comparison.

## Issue Index

- #6: Write arXiv preprint and paper roadmap.
- #13: Select accepted baseline, task subset, and SLS KPI.
- #14: Tier 1 matched CE vs fixed SLS vs annealed SLS benchmark.
- #15: Tier 2 ablations explaining the annealed SLS method.
- #16: Optional generalization beyond the first baseline.
- #21: Retire the V7-native Atari controller path.

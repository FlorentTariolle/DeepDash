# GitHub Issue Body Updates - 2026-06-08

These are the intended replacement bodies for the open GitHub issues after the annealed-SLS reframing.

The local GitHub connector can read issues but does not expose issue-body editing, and the GitHub CLI is not installed in this environment. These bodies are therefore recorded here as the repo-side source of truth until the GitHub issues can be updated directly.

## #6 - Write arXiv preprint: SLS method + Geometry Dash world-model application

Track the SLS-WM paper to arXiv preprint and NeurIPS 2026 workshop submission.

## Updated scope

The paper has two distinct contributions:

1. **Annealed Structured Label Smoothing (SLS)** as a tokenizer-metric-aware objective for discrete-token world models. SLS replaces hard CE targets with a local soft target early in training, then anneals smoothing mass to zero so the late objective is exactly CE.
2. **Geometry Dash real-time world model application** as the domain-specific system contribution. Present the full VMC pipeline as designed for Geometry Dash: FSQ tokens, action-conditioned dynamics, imagined rollout/control, real-time deployment, and procedural rollout or level-continuation generation.

FSQ is no longer the entire method claim. It is the motivating Geometry Dash instantiation: FSQ supplies an integer lattice metric. For accepted-baseline evaluation, SLS can use the selected tokenizer's codebook or embedding distance.

Atari is no longer a target for porting the full Geometry Dash VMC stack. Atari should only appear through the native benchmark of the selected accepted baseline used for CE vs. SLS validation.

## Paper structure

- **Method**: annealed SLS objective, tokenizer metric, relation to hard CE, fixed SLS, and uniform label smoothing.
- **Benchmark**: CE vs. fixed SLS vs. annealed SLS on an accepted baseline, with architecture, data, training budget, and evaluation held fixed except for the target distribution.
- **Geometry Dash application**: specialized real-time VMC system, deploy constraints, generated rollouts/levels, and controller trained in imagination.

## Current evidence

IRIS/Pong single-seed runs are complete:

- CE final return: `20.9375`.
- Fixed SLS final return: `0.125`; useful as a negative ablation showing late over-regularization.
- Annealed SLS final return: `19.625`, best `19.8125`; improves the early/mid instability window and recovers near-CE asymptotic performance.

This supports the current framing, but is not enough for final submission. More seeds and preferably at least one more game are still required.

## Supervisor notes to preserve

- One part of the paper should cover benchmark/method/evaluation.
- One part should cover the Geometry Dash application.
- Mention that the world model can generate procedural level continuations.
- Provide evidence for at least one KPI improved by SLS compared with CE.

## Minimum acceptance evidence

- At least one defensible stability/sample-efficiency KPI where annealed SLS improves over CE on the selected accepted-baseline benchmark.
- Final/tail performance reported honestly, even if CE remains slightly better.
- Geometry Dash evidence that the world model is useful beyond scalar survival: high-quality predictions, generated continuations/levels, real-time deployment, and system latency.

## Explicitly out of scope

- Making the full Geometry Dash VMC stack work on Atari.
- More Atari reward-head/controller-debug cycles as a prerequisite for the paper.
- Treating Geometry Dash only as an appendix afterthought.
- Claiming fixed SLS is the final method when annealing is necessary.

## Done when

The arXiv preprint is posted and the workshop submission is filed with the split contribution story above.

## #13 - Select accepted baseline, task subset, and SLS KPI

## Goal

Lock the fastest defensible benchmark setup for annealed SLS. This issue is no longer about per-game FSQ calibration or a V7-native Atari port.

## Selected first baseline

- Baseline: **IRIS**.
- First task: **PongNoFrameskip-v4**.
- First completed conditions: CE, fixed SLS, annealed SLS.
- Precision/runtime: BF16 and `torch.compile` reduce-overhead on H200.

## Selection criteria

The benchmark setup should satisfy:

- accepted or widely recognized paper baseline;
- public code and runnable configs;
- discrete-token world-model prediction objective where CE is a natural baseline;
- minimal SLS integration surface;
- smoke-scale reproducibility on available compute;
- KPI close to the SLS claim, not only final policy return.

## Current KPI direction

The first seed says the best claim is stability/sample-efficiency, not guaranteed higher final return.

Primary KPIs to report:

- return AUC;
- mean return over the CE collapse/instability window;
- rolling return variance or collapse depth;
- first epoch to reach return thresholds;
- final return and tail mean return.

## Expansion plan

- Add more Pong seeds first.
- Add at least one additional Atari game if Pong seeds remain coherent.
- Keep fixed SLS in the table as the negative ablation that motivates annealing.

## Done when

The repo records selected baseline commit, environment, task subset, seeds, exact CE-vs-fixed-SLS-vs-annealed-SLS KPIs, and the command matrix for matched runs.

## #14 - Tier 1: Minimal SLS-vs-CE patch on an accepted world-model baseline

## Goal

Answer the core SLS method question without porting the full Geometry Dash VMC stack:

> If we keep an accepted world-model baseline fixed and replace hard CE targets with annealed SLS targets, does at least one relevant stability/sample-efficiency KPI improve while final performance remains competitive?

Primary baseline: **IRIS**, because it is an accepted discrete-token transformer world-model baseline with released code.

## Experimental rules

- Start from upstream baseline code and configs.
- Reproduce an unchanged CE baseline first.
- Add SLS as the smallest possible training-objective diff.
- Compare three conditions: CE, fixed SLS, annealed SLS.
- Keep architecture, data preprocessing, rollout/eval protocol, optimizer, precision, compile mode, and training budget fixed unless a change is required by the baseline itself.
- Use Atari only as the baseline's native benchmark, not as a target for the Geometry Dash VMC architecture.

## Current first-seed result

- CE final return: `20.9375`.
- Fixed SLS final return: `0.125`.
- Annealed SLS final return: `19.625`.
- Annealed SLS improves the unstable epoch 250-420 window by `+7.15625` mean return over CE.
- Annealed SLS improves eval-return AUC from CE `-993.0208` to `-408.90625`.
- CE remains slightly better in final/tail performance.

## Primary KPIs

Prefer KPIs close to the annealed-SLS claim before relying on final return:

- return AUC;
- collapse-window return;
- rolling return variance / collapse depth;
- first epoch reaching return thresholds;
- final return and tail mean;
- prediction loss / NLL if available under the baseline protocol;
- codebook-distance or embedding-distance near-miss quality if cheap to log.

## Done when

A table shows CE vs fixed SLS vs annealed SLS under matched conditions, across enough seeds/tasks to make at least one stability/sample-efficiency improvement defensible, with enough run metadata for reviewer-facing reproducibility.

## #15 - Tier 2: SLS method ablations after the baseline KPI lands

## Goal

Run only ablations that clarify annealed SLS itself. Defer architecture-specific rows until the Tier 1 CE-vs-fixed-SLS-vs-annealed-SLS baseline result is stable.

## Priority ablations

- CE vs fixed SLS vs annealed SLS.
- Anneal schedule: hold length, cosine window, final pure-CE length.
- Smoothing mass: `0.05`, `0.10`, `0.20`.
- Top-k neighbour truncation: `8`, `16`, `32`.
- Kernel family: Gaussian / Laplace / Cauchy.
- Uniform label smoothing vs structured label smoothing.
- Coordinate-distance SLS vs embedding-distance SLS where both are available.

## Conditional ablations

Only run these if directly relevant to the selected baseline:

- tokenizer/codebook geometry variants;
- FSQ codebook shape for Geometry Dash;
- focal loss interaction for the Geometry Dash transformer.

## Removed from critical path

- GRWM on/off for Atari.
- V7-native Atari controller, reward-head, dream-gate, and PPO ablations.
- Large architecture changes that confound the objective comparison.

## Done when

A compact ablation table explains why annealed SLS is the chosen objective, after Tier 1 has already shown that it can improve a meaningful KPI.

## #16 - Tier 3: Generalize SLS beyond the first baseline

## Goal

Show that annealed SLS is not a one-off result tied to a single game, seed, or codebase. This is not a VQ-VAE-in-V7-Atari task.

## Options, in priority order

1. Add seeds and at least one additional Atari game to the IRIS benchmark.
2. Apply the same annealed-SLS patch to a second accepted world-model baseline.
3. Compare coordinate-distance SLS and embedding-distance SLS if the baseline exposes both metrics.
4. Add a Geometry Dash CE-vs-SLS ablation only if the measurement is cheap and interpretable.

## Constraints

- Do not block the paper on this issue unless Tier 1 is too weak alone.
- Do not restart the full Atari VMC adaptation.
- Keep every generalization experiment as a small diff against a working baseline.

## Done when

There is a second controlled result supporting the claim that SLS is a general objective over structured discrete latent spaces, or the issue is explicitly dropped to protect the paper schedule.

## #21 - Retire the V7-native Atari controller path

## Decision

Retire the V7-native Atari controller path for the paper.

The recent runs showed that adapting the Geometry Dash VMC stack to Atari created a long reward-head/controller-debug loop. That loop is no longer aligned with the paper strategy.

## What to keep

- Archive the diagnostic conclusion: the V7-native Atari path did not produce a reliable policy and should not be used as the SLS benchmark vehicle.
- Keep reusable utilities that help baseline reproduction, logging, or evaluation.
- Preserve failed-run metadata as motivation for the pivot, not as an active task list.

## What to stop

- No more full-cycle Atari VMC reruns.
- No more reward-head calibration fixes as the default next step.
- No more dream-gate/controller ladder unless a future project explicitly reopens Atari VMC as its own objective.

## Replacement path

Use accepted-baseline IRIS runs for Atari:

- CE baseline;
- fixed SLS ablation;
- annealed SLS method;
- matched architecture, optimizer, data, precision, compile mode, and budget.

## Done when

The README, paper, roadmap, and GitHub issues clearly state that Atari is only for minimal accepted-baseline SLS-vs-CE validation, while Geometry Dash remains the world-model application contribution.

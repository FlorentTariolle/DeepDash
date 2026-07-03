# GitHub Issue Updates - 2026-07-03

Use this after the paper pivot to **DashVMC: Real-Time Discrete World Model Control in Geometry Dash**.

Do not delete the IRIS fork, run logs, or analysis artifacts. Close or archive IRIS/SLS benchmark issues with a diagnostic summary.

## Existing Issues To Modify

### #6 - Rename/reframe paper around DashVMC

Suggested title:

```text
Write DashVMC workshop paper: real-time discrete world model control in Geometry Dash
```

Suggested body:

```markdown
## Goal

Track the paper to a workshop/arXiv-ready draft under the new framing:

> DashVMC: Real-Time Discrete World Model Control in Geometry Dash

The paper is now a Geometry Dash application/system paper, not a positive SLS benchmark paper.

## Main claim

DashVMC is a real-time discrete Vision-Model-Controller system for Geometry Dash:

- FSQ tokenizer over 64x64 Sobel frames;
- action-conditioned transformer world model;
- controller warm-started by behavioural cloning and trained with PPO in dreamed rollouts;
- live 30 FPS deployment through screen capture;
- visual level-continuation samples from real gameplay prefixes.

## SLS scope

Structured Label Smoothing remains in the paper as:

- the Geometry Dash/FSQ-motivated loss-side prior that started the work;
- a fixed-SLS design choice in the Geometry Dash system;
- a diagnostic IRIS/Pong generalization attempt.

Do not claim that annealed SLS robustly improves discrete world models. The IRIS/Pong n=5 result does not support that.

## Evidence required before submission

- Geometry Dash system table: FSQ recon/usage, WM token accuracy, death F1, BC accuracy, PPO dream survival, real-game progress, latency.
- Figure/video panel: real gameplay prefix -> generated continuation, ideally with action/death branch contrast.
- IRIS/Pong diagnostic table: CE vs annealed SLS n=5, fixed SLS n=2.
- Clear limitations: FSQ is not tokenizer-SOTA; GQ-style tokenizer replacement is future work; SLS is not proven general.

## Done when

- Paper title, abstract, intro, results, README, and website all use the DashVMC-first framing.
- The paper compiles cleanly.
- The final workshop/arXiv draft has no stale positive-SLS benchmark language.
```

### #21 - Retire V7-native Atari controller path

Suggested action: close as completed after posting this body/comment.

Suggested closing comment:

```markdown
Closed after the 2026-07-03 paper pivot.

Decision: the V7-native Atari controller path remains retired. It produced a long reward-head/controller-debug loop and is not part of the August paper path.

Atari now appears only through the IRIS/Pong diagnostic experiment:

- CE n=5 final: 15.68 +/- 4.96; tail 500-600: 15.09 +/- 5.59; failure tail<10: 20%.
- Annealed SLS n=5 final: 12.14 +/- 10.82; tail 500-600: 12.55 +/- 8.63; failure tail<10: 40%.
- Fixed SLS n=2 final: 10.06 +/- 14.05; tail 500-600: 8.53 +/- 16.21; failure tail<10: 50%.

No further Atari VMC work is planned for this paper.
```

## Existing Issues To Close Or Archive

### #13 - Select accepted baseline, task subset, and SLS KPI

Suggested action: close as completed/archived.

Suggested closing comment:

```markdown
Closed as archived diagnostic evidence.

IRIS/Pong was selected and run as the accepted-baseline SLS diagnostic. The n=5 result did not validate annealed SLS as a robust CE replacement, so this benchmark path is no longer the paper's main evidentiary path.

Key result:

| Condition | n | Final return | Tail 500-600 | AUC mean | Failure tail <10 |
| --- | ---: | ---: | ---: | ---: | ---: |
| CE | 5 | 15.68 +/- 4.96 | 15.09 +/- 5.59 | 2.05 +/- 6.92 | 20% |
| Annealed SLS | 5 | 12.14 +/- 10.82 | 12.55 +/- 8.63 | 0.95 +/- 6.16 | 40% |
| Fixed SLS | 2 | 10.06 +/- 14.05 | 8.53 +/- 16.21 | -1.46 +/- 6.79 | 50% |

Annealed SLS showed partial stability signals, but not robust final/tail improvement. The paper now focuses on DashVMC / Geometry Dash.
```

### #14 - Tier 1: Minimal SLS-vs-CE patch on accepted baseline

Suggested action: close as completed negative/conditional result.

Suggested closing comment:

```markdown
Closed after completing the matched IRIS/Pong diagnostic runs.

The experiment answered the original question negatively/conditionally: annealed SLS did not robustly beat CE across seeds. It had lower average drawdown/volatility and a slightly better 250-420 window, but worse final/tail means and a higher catastrophic-failure rate.

This result should remain in the paper as a diagnostic table, not as the main contribution.

No more IRIS/Pong rescue runs are planned before the August deadline.
```

### #15 - Tier 2: SLS method ablations

Suggested action: close as not planned for this paper.

Suggested closing comment:

```markdown
Closed as not planned for the August paper path.

The Tier 1 IRIS/Pong result did not produce a positive SLS benchmark claim, so schedule/top-k/kernel/uniform-LS ablations are no longer worth the time before August.

SLS remains scoped to:

- Geometry Dash/FSQ motivation;
- fixed-SLS design choice in the system;
- IRIS/Pong diagnostic negative/conditional result.
```

### #16 - Generalize SLS beyond first baseline

Suggested action: close as not planned.

Suggested closing comment:

```markdown
Closed as not planned for this paper.

The August path is now a Geometry Dash system/application paper. Generalizing SLS beyond IRIS/Pong would require more compute and risk without solving the core submission need.

Do not run more Atari games or port SLS to another baseline before August unless the paper direction changes again.
```

## New Issues To Create

### New Issue - Lock Geometry Dash system evidence table

```markdown
## Goal

Produce the quantitative system table for the DashVMC paper.

## Required rows/metrics

- Dataset: number of episodes/frames, expert demonstrations, augmentation.
- FSQ tokenizer: reconstruction metric, usage %, perplexity %, token grid shape, code count.
- World model: validation token accuracy, validation loss/NLL, death precision/recall/F1.
- Controller: BC validation accuracy, PPO dream survival, jump ratio.
- Deployment: real-game progress by level, attempts per level, mean/median/max survival where available.
- Latency: mean and p95 loop time, FPS budget, hardware.

## Rules

- Prefer frozen V7/V3 evidence already in the repo.
- If a number is from development notes rather than a controlled eval, label it as such.
- Do not use Geometry Dash deploy survival as a causal SLS effect unless the comparison is controlled.

## Done when

The paper has one compact system table with sources for every number.
```

### New Issue - Generate level-continuation figure

```markdown
## Goal

Create the main qualitative figure for the DashVMC paper: real gameplay prefix -> generated continuation.

## Required figure content

- One real prefix from Geometry Dash.
- Autoregressive world model continuation under an action sequence.
- If feasible, two branches showing different action choices or survival/death outcomes.
- Captions must avoid claiming editor-valid or guaranteed playable generated levels.

## Done when

The paper contains a clean visual panel suitable for a workshop submission, and the website can show the same asset or video preview.
```

### New Issue - Rewrite paper to DashVMC-first

```markdown
## Goal

Finish the paper rewrite after the title/abstract pivot.

## Required changes

- Introduction starts from real-time Geometry Dash world model control, not SLS.
- SLS is presented as a scoped FSQ-neighbour smoothing component.
- Results lead with Geometry Dash system evidence.
- IRIS/Pong appears as a diagnostic subsection.
- Limitations explicitly state that SLS is not proven general and FSQ is not tokenizer-SOTA.
- Conclusion summarizes DashVMC first, SLS diagnostic second.

## Done when

There is no stale language implying that annealed SLS is the main contribution or a robust positive benchmark result.
```

### New Issue - Restore clean paper compile

```markdown
## Goal

Make the paper compile cleanly after the DashVMC pivot.

## Context

The 2026-07-03 compile attempt through the bundled LaTeX helper timed out in MiKTeX/latexmk without producing a useful log, and the leftover process was stopped.

## Tasks

- Run a clean LaTeX build locally or on CI.
- Fix any citation/bibliography issues from the new GQ citation.
- Regenerate `docs/static/pdfs/sls_wm.pdf` or rename the PDF target after the final project name decision.

## Done when

The paper PDF builds cleanly and the website link points to the current draft.
```

### New Issue - Verify repo/site rename after DashVMC rename

```markdown
## Goal

Verify the repository and GitHub Pages path after the rename to `dash-vmc`.

## Current status

The repository has been renamed to `dash-vmc`. Update and verify:

- README links;
- paper header links;
- GitHub Pages canonical URL;
- profile README, CV, and personal website references;
- draft PDF link once the paper builds cleanly.

## Done when

The repo name, paper links, website canonical URL, and citation URL are decided and updated consistently.
```

### Optional New Issue - Audit Geometry Dash SLS evidence

```markdown
## Goal

Determine exactly what can be claimed about SLS inside Geometry Dash.

## Questions

- Is there a controlled CE-vs-fixed-SLS Geometry Dash run with the same tokenizer/data/architecture/training budget?
- If yes, what KPI changes and how reliable is it?
- If no, what evidence is only qualitative or development-confounded?

## Output

One short table separating:

- controlled evidence;
- qualitative FSQ-neighbour evidence;
- development notes that should not be treated as causal.

## Done when

The paper has a defensible SLS-in-Geometry-Dash paragraph and no overclaiming.
```

### Optional New Issue - Add GQ tokenizer related-work caveat

Suggested action: create only if you want review tracking. The first paper edit is already done.

```markdown
## Goal

Track the related-work/limitations update for Gaussian Quant tokenizers.

## Done already

The paper now says FSQ is not claimed as tokenizer-SOTA and cites GQ as a stronger image tokenizer benchmark result.

## Remaining check

Verify final wording after compile and ensure the website/README do not imply FSQ is the best available tokenizer.
```

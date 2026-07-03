# Roadmap

Last updated on 2026-07-03 after the IRIS/Pong n=5 analysis.

## Current Paper Direction

The paper is now a Geometry Dash application/system paper:

1. **Main contribution: DashVMC**, a real-time discrete Vision-Model-Controller system for Geometry Dash. The system uses FSQ tokenization, action-conditioned transformer dynamics, BC warm-start, PPO in dreamed rollouts, 30 FPS deployment, and visual level-continuation samples from real prefixes.
2. **Secondary contribution: scoped SLS evidence**, not a general positive method claim. SLS originated from a Geometry Dash/FSQ observation: neighbouring FSQ codes can decode to visually similar or control-equivalent patches, while hard token CE penalizes all wrong codes equally. Fixed SLS remains a Geometry Dash design choice.
3. **Diagnostic result: annealed SLS on IRIS/Pong did not robustly beat CE.** The IRIS result should be reported honestly as a negative/conditional generalization attempt.

Do not frame the paper as "Annealed SLS improves discrete world models." The current data do not support that.

## Current Empirical State

Geometry Dash evidence already available:

- FSQ tokenizer, action-conditioned transformer, BC + PPO controller, and deployment scripts.
- V3/V7 development logs and notes.
- 30 FPS deployment path with about 15 ms loop time reported in the draft/site.
- Dream rollout and level-continuation functionality.
- SLS/FSQ-neighbour qualitative motivation.

IRIS/Pong diagnostic evidence:

| Condition | n | Final return | Tail 500-600 | AUC mean | Failure tail <10 |
| --- | ---: | ---: | ---: | ---: | ---: |
| CE | 5 | 15.68 +/- 4.96 | 15.09 +/- 5.59 | 2.05 +/- 6.92 | 20% |
| Annealed SLS | 5 | 12.14 +/- 10.82 | 12.55 +/- 8.63 | 0.95 +/- 6.16 | 40% |
| Fixed SLS | 2 | 10.06 +/- 14.05 | 8.53 +/- 16.21 | -1.46 +/- 6.79 | 50% |

Annealed SLS has partial stability signals: lower average max drawdown, lower return-difference volatility, and a slightly better 250-420 window. These do not translate into robust final/tail performance. Treat this as a diagnostic result, not a success result.

## Immediate Next Steps

1. **Finish the paper pivot.**
   - Rewrite introduction around Geometry Dash and the real-time control constraint.
   - Keep SLS as a subsection/design choice and diagnostic study.
   - Remove stale language that calls annealed SLS the primary contribution.

2. **Lock Geometry Dash evidence.**
   - Produce a system table: FSQ recon/usage, WM token accuracy, death F1, BC accuracy, PPO dream survival, real-game progress, and latency.
   - Re-run or audit frozen real-game deployment evaluation if current numbers are not clean enough.
   - Add a figure/video-derived panel showing real prefix -> generated continuation.

3. **Report SLS honestly.**
   - Include Geometry Dash qualitative motivation and any clean in-domain KPI.
   - Include the IRIS/Pong n=5 diagnostic table.
   - State that annealed SLS did not validate as a robust general CE replacement.

4. **Update public framing.**
   - README, paper title, and project page should say DashVMC / Geometry Dash first.
   - Use `dash-vmc` URLs now that the GitHub repository has been renamed.
   - Do not delete the IRIS fork or analysis artifacts; archive them as diagnostic evidence.

## What Not To Do Before August

- Do not spend more compute trying to rescue SLS on IRIS/Pong.
- Do not start a full GQ tokenizer port.
- Do not rename/delete the GitHub repo until links and paper title are stable.
- Do not delete the IRIS fork, logs, or analysis outputs.
- Do not claim FSQ is tokenizer-SOTA.
- Do not claim SLS generally improves arbitrary discrete tokenizers.
- Do not use Geometry Dash deploy survival as the primary SLS effect unless the comparison is controlled.

## Scope Boundaries

In scope:

- Real-time Geometry Dash discrete world-model control.
- FSQ tokenizer as the system tokenizer, with explicit caveats about newer tokenizer families such as GQ.
- Fixed SLS as a Geometry Dash/FSQ design choice.
- IRIS/Pong as a diagnostic negative/conditional generalization test.
- Generated visual level continuations from the learned action-conditioned dynamics.

Out of scope:

- Full Atari controller port.
- New broad Atari benchmark expansion.
- GQ tokenizer replacement.
- Major architecture redesigns.
- Main-conference-scale claims about SLS.

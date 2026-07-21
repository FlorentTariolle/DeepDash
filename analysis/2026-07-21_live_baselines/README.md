# Live policy controls

These artifacts compare no-op, the reconstructed epoch-9 behavioural-cloning
policy, and the frozen PPO policy on the first three official Geometry Dash
levels. The evaluator runs at 30 FPS with Auto-Retry and memory-based death
detection. Each invocation executes one initial synchronization episode that is
retained under `excluded_episodes` but omitted from every statistic.

| Policy | Scored attempts per level | Level 1 mean +/- SD [95% CI] | Level 2 mean +/- SD [95% CI] | Level 3 mean +/- SD [95% CI] |
| --- | ---: | ---: | ---: | ---: |
| No-op | 10 | 46.2 +/- 0.7 [45.7, 46.6] | 63.4 +/- 0.7 [63.0, 63.8] | 41.5 +/- 0.7 [41.1, 41.9] |
| BC reconstruction | 100 | 130.4 +/- 91.1 [112.6, 148.3] | 119.7 +/- 71.5 [105.8, 133.8] | 45.2 +/- 9.0 [43.5, 47.0] |
| PPO | 99 | 279.4 +/- 48.0 [270.2, 289.0] | 262.9 +/- 121.2 [239.4, 287.2] | 63.8 +/- 27.1 [58.8, 69.5] |

No-op and BC artifacts are stored in this directory. The corrected PPO files
remain in `analysis/2026-07-20_v7_deploy/`; their originally recorded first
episodes are preserved but excluded, leaving 99 scored attempts per level.

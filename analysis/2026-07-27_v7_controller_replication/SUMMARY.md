# Retained V7 BC to PPO controller replications

This report replaces the historical comparison between PPO and a separately reconstructed BC checkpoint. Each PPO policy is compared with the exact retained BC checkpoint that initialized it. The V7 FSQ tokenizer and transformer are fixed across controller seeds 43, 44, and 45.

## Protocol

- 25 valid live attempts per frozen policy and level, plus one excluded synchronization attempt.
- 30 FPS diagnostic inference path, Auto-Retry, memory-delimited episode boundaries.
- Sample standard deviation uses `ddof=1`.
- Difference intervals use 50,000 independent attempt-level bootstrap resamples per frozen pair.
- Difference intervals describe deployment variability, not controller-training uncertainty.
- Three paired seed differences are the replication evidence; attempts are not pooled across seeds.
- Official aggregate metrics use seed-level policy means, with no-op mapped to 0 and the best observed retained-policy seed mean on each level mapped to 1.
- Aggregate 95% intervals use 50,000 task-stratified bootstrap resamples of controller seeds; normalization anchors remain fixed.

## Official levels

| Seed | Level | BC frames | PPO frames | PPO - BC [95% bootstrap CI] |
| ---: | --- | ---: | ---: | ---: |
| 43 | Stereo Madness | 120.2 +/- 91.8 | 280.1 +/- 23.3 | 159.9 [122.7, 195.4] |
| 43 | Back on Track | 123.5 +/- 81.5 | 260.0 +/- 55.5 | 136.5 [98.8, 174.4] |
| 43 | Polargeist | 52.4 +/- 16.7 | 65.2 +/- 33.4 | 12.8 [-0.2, 28.6] |
| 44 | Stereo Madness | 130.6 +/- 76.0 | 280.4 +/- 33.5 | 149.8 [116.8, 180.6] |
| 44 | Back on Track | 111.1 +/- 60.3 | 195.9 +/- 126.5 | 84.8 [31.9, 139.6] |
| 44 | Polargeist | 49.0 +/- 16.5 | 68.3 +/- 44.1 | 19.4 [3.0, 39.0] |
| 45 | Stereo Madness | 156.0 +/- 100.2 | 308.8 +/- 68.6 | 152.8 [106.7, 199.8] |
| 45 | Back on Track | 120.2 +/- 69.6 | 209.4 +/- 130.0 | 89.2 [34.6, 147.2] |
| 45 | Polargeist | 46.2 +/- 9.2 | 63.1 +/- 32.9 | 16.8 [5.7, 31.5] |

## Auxiliary levels

| Seed | Level | BC frames | PPO frames | PPO - BC [95% bootstrap CI] |
| ---: | --- | ---: | ---: | ---: |
| 43 | Stereo Madness Copy | 366.8 +/- 172.8 | 435.6 +/- 196.1 | 68.8 [-33.3, 166.1] |
| 43 | Stereo INSANE Nerfed | 104.8 +/- 72.1 | 290.4 +/- 48.6 | 185.5 [151.1, 217.6] |
| 44 | Stereo Madness Copy | 285.7 +/- 153.5 | 495.8 +/- 137.5 | 210.1 [127.9, 285.5] |
| 44 | Stereo INSANE Nerfed | 102.1 +/- 64.0 | 342.8 +/- 80.8 | 240.6 [200.8, 280.2] |
| 45 | Stereo Madness Copy | 299.4 +/- 175.2 | 546.8 +/- 94.1 | 247.4 [167.6, 320.7] |
| 45 | Stereo INSANE Nerfed | 97.8 +/- 57.9 | 266.7 +/- 128.9 | 168.8 [115.4, 223.9] |

## Across-seed replication summary

Values are the mean and sample SD of the three paired seed-level differences, not pooled attempts.

| Level | Mean PPO - BC across seeds | Positive seeds |
| --- | ---: | ---: |
| Stereo Madness | 154.2 +/- 5.2 | 3/3 |
| Back on Track | 103.5 +/- 28.7 | 3/3 |
| Polargeist | 16.3 +/- 3.3 | 3/3 |
| Stereo Madness Copy | 175.4 +/- 94.2 | 3/3 |
| Stereo INSANE Nerfed | 198.3 +/- 37.6 | 3/3 |

## Official seed-level aggregate metrics

Scores normalize each official level from its fixed no-op mean (0) to its best observed retained-policy seed mean (1). Intervals are 95% percentile intervals from task-stratified bootstrap resampling of controller seeds. The empirical-reference gap is the shortfall to this reference, not to true level completion; lower is better.

| Metric | BC [95% CI] | PPO [95% CI] |
| --- | ---: | ---: |
| Mean | 0.302 [0.261, 0.346] | 0.877 [0.818, 0.942] |
| IQM | 0.295 [0.264, 0.350] | 0.894 [0.826, 0.978] |
| Empirical-reference gap | 0.698 [0.654, 0.739] | 0.123 [0.058, 0.182] |

## Training provenance

| Seed | Selected BC | BC val. loss / accuracy | Selected PPO | Dev. survival | Executed PPO iterations | Total wall time |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 43 | epoch 10 | 0.558784 / 78.97% | iteration 12,420 | 30.13/45 | 15,000 | 17.41 h |
| 44 | epoch 10 | 0.560073 / 79.15% | iteration 5,090 | 29.95/45 | 15,000 | 17.06 h |
| 45 | epoch 11 | 0.567360 / 79.15% | iteration 12,280 | 29.96/45 | 15,000 | 17.07 h |

Total controller-training wall time: **51.54 hours**.  Total PPO iterations executed: **45,000**.

Checkpoint hashes and row-level bootstrap seeds are retained in `paired_results.json`.

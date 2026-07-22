# Held-out community level: Stereo INSANE Nerfed

Date: 2026-07-22

## Selection rule

Issue #31 requested held-out community levels absent from the offline corpus, restricted to mechanics supported by the training distribution (no gravity inversion, etc.).

Selected:
1. **Stereo INSANE Nerfed** by XXcapta1n ? single custom community level used for held-out transfer within supported mechanics.
2. Separately, **Stereo Madness Copy** by KRUTOYARBUS was run as a late Level-1 copy / ship-segment proxy and is **not** counted as held-out (see `analysis/2026-07-22_ship_segment_eval/`).

Rejected / not run:
- Additional community levels beyond this one (by design: keep the live suite to 3 official + 1 sub-official copy + 1 custom held-out).

Corpus check:
- Offline episode metadata stores only numeric `level` IDs, not community titles.
- This level was chosen because it is a community layout absent from the official Level 1--3 evaluation set and was not used as a named live evaluation target during V7 development.
- Report as **unseen-level transfer within the supported mechanic distribution**, not novel-mechanic or broad OOD generalization.

## Protocol

- Same as official live baselines / ship-segment eval
- 30 FPS, Auto-Retry, initial sync episode excluded
- Frozen V7 checkpoints; BC uses epoch-9 reconstruction
- Sample sizes: 10 no-op, 20 BC, 20 PPO scored attempts

## Results

| Policy | Scored *n* | Mean frames [95% CI] | Median | Min--max | Mean seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| No-op | 10 | 46.1 [45.6, 46.6] | 46 | 45--47 | 1.54 |
| BC | 20 | 113.7 [84.5, 145.3] | 137 | 45--271 | 3.79 |
| PPO | 20 | **215.7 [184.1, 249.9]** | 196 | 147--361 | 7.19 |

Raw JSON:
- `noop_stereo_insane_nerfed_10.json`
- `bc_stereo_insane_nerfed_20.json`
- `ppo_stereo_insane_nerfed_20.json`

Hardware recorded in artifacts: NVIDIA GeForce RTX 2060 SUPER.

## Read

On this custom held-out level, ranking remains PPO > BC > no-op. Absolute survival is lower than on official Levels 1--2 and on the Level-1 copy, as expected for a harder / unseen layout, but the controller ordering transfers. Mean CIs for PPO and BC do not overlap. No-op again dies near the first obstacle (~46 frames), similar to official Level 1 no-op.

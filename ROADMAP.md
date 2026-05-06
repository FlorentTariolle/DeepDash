# Roadmap

Last updated from GitHub issues plus local artifacts on 2026-05-06.

## Current Paper Direction

This repo is now centered on a method paper for Structured Label Smoothing (SLS) in discrete-token world models with structured codebooks.

The main claim is that FSQ gives token IDs a coordinate geometry, so near-neighbor code predictions should not be penalized like unrelated code predictions. SLS uses a kernel over FSQ lattice coordinates to turn one-hot next-token supervision into structured soft targets.

The Geometry Dash V7 pipeline remains the showcase application. The paper-critical evaluation path has moved to Atari 100k-style experiments because they give a standard benchmark and published baselines.

Source issues: #6, #12, #14.

## Where We Left Off

The working tree now includes the first full-cycle Atari orchestrator implementation.

Recent completed work:

- DeepDash and Atari code/assets were split so DeepDash can stay frozen as the Geometry Dash showcase.
- Atari Pong environment interaction and replay storage were added.
- Atari preprocessing is locked to 64x64 RGB, frame-skip 4, no Sobel, no sticky actions.
- The first Pong tokenizer baseline was promoted in `configs/atari/atari_pong_v0.yaml`.
- `checkpoints_atari_pong_v0` is the retained Atari tokenizer checkpoint family.
- `scripts/train_atari_predictor.py` exists as the next local predictor-training entry point.
- The first local Pong predictor probes have already run:
  - CE artifacts: `checkpoints_atari_pong_v0_predictor_ce`.
  - SLS artifacts: `checkpoints_atari_pong_v0_predictor_sls`.
  - Smoke-test artifacts also exist under `.codex_tmp/atari_predictor_*_smoke*`.

The important empirical state is not "predictor training is next" anymore. We already ran a short, low-data Pong CE/SLS predictor probe.

Observed outcome:

- The effect is slim, which is expected for this setup: little replay data, Pong is simple, and the runs were short.
- SLS mostly shows the expected structured-loss signature: FSQ-coordinate distances are a little lower.
- Token accuracy is effectively flat or only slightly shifted depending on checkpoint/epoch, which is expected because SLS deliberately stops treating nearby wrong codes as equally bad.
- This probe is useful as a sanity check for the predictor path, not as the paper's headline result.

Representative local logs:

- CE final epoch 50: `val_acc=0.767073`, `val_fsq_l1_dist=0.422828`, rollout L1 at 1/5/10/15 steps = `0.003080/0.003378/0.003528/0.003700`.
- SLS best logged epoch 25: `val_acc=0.771982`, `val_fsq_l1_dist=0.411882`, rollout L1 at 1/5/10/15 steps = `0.003088/0.003332/0.003491/0.003600`.
- The meaningful read is the lower FSQ-distance / similar-accuracy pattern, not the exact sign of the tiny accuracy delta.

Source issues: #6, #12, #19. Local artifacts: `checkpoints_atari_pong_v0_predictor_ce/predictor_log.csv`, `checkpoints_atari_pong_v0_predictor_sls/predictor_log.csv`.

## Immediate Next Steps

1. Preserve the Pong predictor probe as a sanity result, not as evidence for the paper claim.
   - Document that it is underpowered.
   - Treat the FSQ-distance improvement and flat accuracy as a qualitative check that SLS is wired correctly.
   - Do not overfit conclusions to Pong v0.

2. Implement the Atari training loop rather than a one-off predictor run.
   - Random-policy warmup for the first 10K real env interactions.
   - Train/resume FSQ on cumulative replay.
   - Freeze FSQ, then train/resume the transformer on cumulative replay.
   - Freeze the transformer, then train the actor-critic in dreams.
   - After warmup, collect each next 10K interactions with the current actor.
   - During real actor collection, also train the actor-critic on those same real env interactions.
   - At every 10K boundary, refresh FSQ and transformer on cumulative replay, then run another dream-training phase.

3. Make the predictor comparison table explicit.
   - Next-token accuracy is not enough because SLS intentionally rewards near misses.
   - Track CE/NLL, token accuracy, FSQ-coordinate distance, reconstruction quality, and rollout degradation over multiple prediction horizons.
   - Report that early Pong v0 shows the expected lower-distance/similar-accuracy pattern but is too small to support a claim.

4. Fill the missing Atari controller/deployment pieces.
   - Generalize controller policy/output paths to categorical Atari actions.
   - Replace Geometry Dash death-token assumptions with Atari `done` handling.
   - Add real-env actor training during collection.
   - Add a deploy/collect loop that appends actor-generated transitions to replay.
   - Add an evaluation harness that reports Atari returns and Atari 100k-compatible metrics.

## Atari Training Protocol

This is the intended full-training protocol for the Atari path.

### Budget

- Training budget: 100K real Atari env interactions.
- Evaluation episodes are separate and must be reported separately.
- No extra real env training steps after the 100K budget unless explicitly labeled as an ablation.

### Cycle 0: Random Warmup

Use random policy for the first 10K interactions.

Purpose:

- Give FSQ enough visual diversity to avoid a degenerate tokenizer.
- Give the transformer enough transition coverage to support non-degenerate dream rollouts.
- Avoid training the actor-critic against a world model that is still too weak to be useful.

Steps:

1. Collect 10K random-policy interactions into replay.
2. Train or resume FSQ on cumulative replay.
3. Freeze FSQ.
4. Train or resume transformer/predictor on cumulative replay.
5. Freeze transformer.
6. Train actor-critic in dreams.

### Cycles 1-9: Policy Collection Plus Dyna Updates

For each block `10K-20K`, `20K-30K`, ..., `90K-100K`:

1. Deploy the current actor in the real Atari env.
2. Collect the next 10K interactions and append them to replay.
3. While collecting those interactions, update the actor-critic from the same real env rollout data.
4. At the 10K boundary, train or resume FSQ on cumulative replay.
5. Freeze FSQ.
6. Train or resume the transformer on cumulative replay.
7. Freeze transformer.
8. Run a dream actor-critic training phase with the refreshed world model.
9. Continue real collection with the updated actor.

This is a Dyna-with-warmup style setup: after the warmup, actor learning uses both real data and imagined data, while FSQ and the transformer are refreshed periodically from cumulative replay.

### Cumulative Replay Policy

Train FSQ and transformer on cumulative replay, not only the newest 10K block.

Reason:

- Avoid tokenizer/world-model forgetting.
- Keep targets stable across the whole data distribution encountered so far.
- Let later policy data expand the replay distribution without discarding earlier random/exploratory coverage.

If compute becomes a bottleneck, use replay sampling or recency weighting as an ablation, but the baseline should be cumulative.

### Final 10K Decision

Keep two final-stage options explicit until experiments decide which is cleaner:

- Evaluation-clean option: use `90K-100K` for final real actor updates, then evaluate without a final WM refresh.
- Max-performance option: after reaching 100K, do one final FSQ + transformer refresh on all 100K, then final dream actor training, then evaluate.

Both are valid if the real env training budget remains exactly 100K and evaluation episodes are separate.

## Phase Plan

### Phase 1: V7-Native Atari Port and Tier 1 Result

Goal: headline result for Q1, "does SLS improve over standard CE?"

Issues: #12, #13, #14, #19.

Deliverable:

- SLS-FSQ vs CE-FSQ on 4-6 reactive Atari games.
- 5 seeds per condition.
- Human-normalized score table against published baselines such as Delta-IRIS, IRIS, DreamerV3, TWISTER, STORM, and SimPLe.

Current status:

- Pong tokenizer baseline is done.
- A first Pong predictor CE/SLS comparison has run and is only a sanity check.
- Supercomputer access is restored. Local training is no longer the target path.
- H200 Atari prototype config exists at `configs/atari/atari_pong_h200.yaml`.
- Atari SLURM wrappers exist for random replay collection, FSQ training, CE/SLS predictor training, and real-env actor training.
- The one-game complete-loop prototype now has a single entry point: `sbatch slurm/atari_train_full_cycle.sl configs/atari/atari_pong_h200.yaml`.
- `scripts/train_atari_full_cycle.py` owns the 100K interaction budget, phase ordering, state/resume markers, replay-step consistency checks, final mode selection, and summary output.
- `configs/atari/atari_pong_h200_smoke.yaml` is the tiny end-to-end smoke target for validating phase completion, resume skipping, replay-step accounting, and evaluation outside the training budget.

### Phase 2: Tier 2 Ablations

Goal: show which parts of SLS and FSQ geometry matter.

Issue: #15.

Planned ablations:

- SLS kernel family: Gaussian, Laplace, Cauchy.
- SLS sigma sweep: 0.5, 0.7, 0.9.
- Isotropic vs calibrated `dim_weights`.
- FSQ codebook shape: `[5,5,5,5]`, `[8,5,5,5]`, `[4,4,4,4]`.
- GRWM regularizers on/off.
- SLS by focal-loss 2x2.

Deliverable: six ablation tables for the paper.

### Phase 3: Generalizability

Goal: answer Q2, "is SLS FSQ-specific, or a principle-level idea?"

Issue: #16.

Priority path:

- Add VQ-VAE as an alternative tokenizer inside the V7 pipeline.
- Adapt SLS to codebook embedding distance.
- Compare VQ+SLS vs VQ+CE on 1-2 games.

Stretch path:

- Apply SLS in Delta-IRIS or an adapted external IRIS-style codebase.
- Only do this after the in-repo VQ-VAE result lands cleanly.

## Deferred Or Droppable Work

If schedule slips, drop in this order:

1. Delta-IRIS external integration.
2. In-repo VQ-VAE generalizability.
3. Weakest Tier 2 ablation rows.

Do not drop Tier 1. The CE vs SLS Atari result is the non-negotiable paper core.

Also deferred:

- Joint encoder/world-model fine-tuning.
- Geometry Dash deploy-survival as the main evaluation metric.
- DeepDash-specific architecture ablations such as AdaLN-Zero, QK norm, MTP, AC-CPC weight, death oversampling, dream horizon, and context length.

## Supercomputer First Run

Use the H200 config:

```bash
sbatch slurm/atari_collect_random.sl configs/atari/atari_pong_h200.yaml
sbatch slurm/atari_train_fsq.sl configs/atari/atari_pong_h200.yaml
sbatch slurm/atari_train_predictor.sl configs/atari/atari_pong_h200.yaml predictor
sbatch slurm/atari_train_predictor.sl configs/atari/atari_pong_h200.yaml predictor_sls
sbatch slurm/atari_train_actor_real.sl configs/atari/atari_pong_h200.yaml actor_real
sbatch slurm/atari_train_actor_dream.sl configs/atari/atari_pong_h200.yaml actor_dream
```

Cluster assumptions:

- 8h walltime per job.
- Jobs use `USR1` before timeout and resubmit themselves.
- Training resumes from latest checkpoints/sentinel files.
- Use `bfloat16` and `compile_mode: reduce-overhead` on the H200 path.

This run validates the first practical Atari cycle:

1. Random 10K replay warmup.
2. FSQ trained on replay.
3. CE and SLS predictors trained from frozen FSQ tokens.
4. Real-env categorical actor trains from real PPO rollouts while appending replay.
5. Dream categorical actor trains from imagined rollouts using predictor reward and `done` heads.

The remaining missing step for the full Dyna loop is orchestration: automatically repeat cumulative replay refreshes, FSQ/predictor retraining, real actor collection, and dream actor updates across the full 100K budget.

## Issue Index

- #6: Write arXiv preprint and paper roadmap.
- #12: Port V7 pipeline to Atari.
- #13: Game selection and per-game FSQ calibration.
- #14: Tier 1 SLS-FSQ vs CE-FSQ headline on Atari subset.
- #15: Tier 2 ablations.
- #16: Tier 3 VQ-VAE and adapted SLS generalizability.
- #19: Local training/eval infrastructure for Atari.
- #20: Delete obsolete checkpoints.

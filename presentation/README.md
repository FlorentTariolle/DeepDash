# Presentation - DeepDash (course project)

Slides delivered on **2026-04-29** for the INSA Rouen Representation Learning course project. The compiled deck is `main.pdf`; source is `main.tex`.

## What this deck represents

The **final Geometry Dash architecture**, in two artifacts:

- **V3-deploy** (commit `75fe40a`, 2026-03-23): the original GD model that produced the strongest play at the presentation deadline. Trained on then-older code which had an FSQ-augmentation-pipeline bug that accidentally inflated FSQ training compute by ~5-15x.
- **V7** (validated 2026-04-29): the **same architecture** (384d / 8L / 8H transformer + V2-style FSQ + V3 PPO) retrained on **current code**, with the augmentation-pipeline bug fixed and multi-directional shifts restored. Reaches V3-deploy deploy-survival parity reproducibly from HEAD. V7 is the locked GD instantiation.

Right after course delivery the project pivoted off GD experimentation entirely to focus on the full paper. The deck is therefore a complete description of the GD instantiation, not an interim snapshot of an evolving model. What the deck does **not** cover is the post-pivot work (SLS as a method paper, Atari benchmark evaluation, VQ-VAE generalizability ablation); for that, see the SLS-WM paper, not these slides.

## Why the deck is so dense

The talk had a hard **10-minute slot** covering the full V-M-C pipeline, SLS, FSQ calibration, dream rollouts, controller, deploy, and timing. To fit:

- Main slides are terse.
- Headline results are deliberately understated.
- A large amount of substantive material lives in the appendix.

That compression is a time-budget artifact, not the intended framing of the work. The full story (with the headline numbers presented at full strength) belongs in the paper, not here.

## Status

Frozen. Do not edit unless reopened deliberately.

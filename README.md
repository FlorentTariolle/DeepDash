# DashVMC
### Real-Time Discrete World Model Control in Geometry Dash

[Florent Tariolle](https://tariolle.github.io/)

DashVMC is a real-time discrete world model control system for Geometry Dash. It combines an FSQ tokenizer, an action-conditioned transformer world model, and a lightweight actor-critic trained from behavioural cloning plus PPO in imagined rollouts.

Structured Label Smoothing (SLS) remains part of the project as the Geometry Dash/FSQ-motivated loss-side prior that started the work. It is no longer the main paper claim: a controlled IRIS/Pong test of annealed SLS did not robustly outperform cross-entropy across seeds.

<p align="center">
   <b>[ <a href="https://tariolle.github.io/dash-vmc/static/pdfs/sls_wm.pdf">Paper Draft</a> | <a href="https://tariolle.github.io/dash-vmc/">Website</a> | <a href="https://github.com/Tariolle/dash-vmc/blob/main/presentation/main.pdf">Presentation</a> ]</b>
</p>

<p align="center">
  <img src="docs/static/images/sls_kernel.svg" width="82%">
</p>

## Geometry Dash System

The Geometry Dash application is the frozen environment-specific showcase. It uses an FSQ tokenizer, an action-conditioned transformer world model, and a lightweight actor-critic trained from BC warm-start plus PPO in imagined rollouts. It runs at 30 FPS through screen capture and can sample plausible visual level continuations by rolling the learned dynamics forward from real gameplay prefixes.

<p align="center">
  <img src="docs/static/images/pipeline.png" width="82%">
</p>

## SLS Status

SLS came from an FSQ-specific observation in Geometry Dash: nearby lattice codes can decode to visually similar or control-equivalent patches, while hard token cross-entropy penalizes all wrong codes equally. Fixed SLS is retained as a Geometry Dash design choice when reporting the system.

The broader claim was tested on [IRIS](https://arxiv.org/abs/2209.00588) Pong by changing only the world model loss target. That generalization attempt was negative/conditional:

| Condition | n | Final return | Tail 500-600 | Failure tail <10 |
|:--|--:|--:|--:|--:|
| CE | 5 | `15.68 +/- 4.96` | `15.09 +/- 5.59` | `20%` |
| Annealed SLS | 5 | `12.14 +/- 10.82` | `12.55 +/- 8.63` | `40%` |
| Fixed SLS | 2 | `10.06 +/- 14.05` | `8.53 +/- 16.21` | `50%` |

Annealed SLS showed partial stability signals, but not a robust performance improvement. The paper now treats IRIS/Pong as a diagnostic result rather than as the main contribution.

## Scope

- The main paper claim is the real-time Geometry Dash world model control system, not a general SLS benchmark win.
- SLS is tokenizer-metric aware, but the current evidence supports only a scoped Geometry Dash/FSQ motivation plus a negative/conditional IRIS diagnostic.
- The Geometry Dash tokenizer remains reconstruction-anchored. A constrained FSQ-JEPA hybrid did not improve control, but this is not a claim against LeWorldModel or continuous-latent JEPA.
- The direct Atari port of the Geometry Dash controller path is retired. Atari appears only through the IRIS/Pong diagnostic.

## Using the code

**Environment.** Conda, PyTorch 2.10, CUDA 12.6.
```bash
conda run -n <env> python -m pip install -r requirements.txt
```

**Train the FSQ-VAE (V):**
```bash
python scripts/train_fsq.py
```

**Train the transformer world model (M) on the frozen FSQ tokens:**
```bash
python scripts/train_transformer.py
```

**Train the controller (C): BC warm-start, then PPO in imagination:**
```bash
python scripts/train_controller_bc.py
python scripts/train_controller_ppo.py --pretrained checkpoints/controller_bc_best.pt
```

**Deploy to the live game (screen capture, 30 FPS):**
```bash
python scripts/deploy.py
```

Cluster launches (SLURM, A100): `sbatch slurm/train_fsq.sl`, `sbatch slurm/train_transformer.sl`, `sbatch slurm/train_controller.sl`.

## Contact

Feel free to open [issues](https://github.com/Tariolle/dash-vmc/issues). For questions or collaborations, contact `florent.tariolle@insa-rouen.fr`.

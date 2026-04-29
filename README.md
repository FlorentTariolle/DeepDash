# SLS-WM
### Structured Label Smoothing over Finite Scalar Quantization for Discrete World Models

[Florent Tariolle](mailto:florent.tariolle@insa-rouen.fr)

**Abstract:** Discrete World Models tokenize observations with a learned quantizer and predict next-frame tokens with a transformer, but standard cross-entropy treats every incorrect prediction as equally wrong. Finite Scalar Quantization (FSQ) makes a richer signal available by construction: each code sits on an integer coordinate lattice, so a token one step away in one dimension is a near-miss while a token at the opposite corner is a gross error. We introduce *Structured Label Smoothing* (SLS), which replaces the one-hot training target with a kernel over codebook coordinates, so a near-miss prediction is treated as a near-miss rather than a gross error. An isotropic kernel with bandwidth fixed by a first-neighbour rule gives a zero-calibration hyperparameter that is robust to codebook drift. We integrate SLS into a complete Vision-Model-Controller pipeline for Geometry Dash, where the controller is trained entirely in imagination and deployed at 30 FPS on the real game via screen capture.

<p align="center">
   <b>[ <a href="https://tariolle.github.io/sls-wm/static/pdfs/sls_wm.pdf">Paper Draft</a> | <a href="https://tariolle.github.io/sls-wm/">Website</a> ]</b>
</p>

<p align="center">
  <img src="docs/static/images/pipeline.png" width="80%">
</p>

> **Project status (updated 2026-04-29).**
>
> **Why SLS exists.** During transformer training we observed an anomaly: predicted frames matched ground truth almost perfectly, yet next-token accuracy stayed below 30%. Investigation revealed that FSQ's coordinate structure makes neighbor codes semantically near-identical, so standard CE was actively penalizing semantically-correct near-miss predictions as if they were gross errors. Structured Label Smoothing (SLS) is the principled fix: replace one-hot targets with a kernel over codebook coordinate distance, so the loss reflects the geometry the quantizer has already learned.
>
> **Why we are pivoting evaluation.** Once SLS was identified, it had to be measured properly. Geometry Dash's deploy metric (% of level reached) is a serial-difficulty cut: a model that is globally worse but lucky on the early section scores higher than a globally better model that dies at one tough obstacle. The SNR is too low to detect SLS effect sizes; even if SLS works, GD cannot tell us. A standard benchmark gives parallel-counted metrics (returns, achievements), statistical signal across seeds, and comparable baselines (DreamerV3, IRIS, TWISTER). The pivot is methodological, not aesthetic.
>
> **What stays, what changes.** The locked GD instantiation is **V7**: the V3-deploy architecture (the project's strongest GD play, originally trained on older code with an FSQ-augmentation-pipeline bug) retrained on current code with the bug fixed and multi-directional shifts restored, reaching V3-deploy deploy-survival parity reproducibly from HEAD. V7 is retained as a real-time application showcase: the full pipeline measures **~15 ms total inference per frame on RTX 2060 SUPER** (~67 FPS achievable; deployed at 30 FPS conservatively for capture-rate stability). Primary evaluation moves to a reactive subset of Atari 100k. Two paper questions guide ongoing work: **(Q1)** does SLS improve over standard CE on a standard benchmark? **(Q2)** is SLS FSQ-specific, or principle-level (validated via a VQ-VAE adaptation)?

> **Status:** V7 (Geometry Dash) is frozen as the application showcase, no further iteration. Benchmark instantiation is pre-freeze and the active research track. NeurIPS 2026 workshop submission target. Numerical results and ablation tables will land with the benchmark freeze; this repository currently describes the method and architecture, not outcomes.

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

Feel free to open [issues](https://github.com/Tariolle/sls-wm/issues). For questions or collaborations, contact `florent.tariolle@insa-rouen.fr`.

# SLS-WM
### Annealed Structured Label Smoothing for Discrete World Models

[Florent Tariolle](https://tariolle.github.io/)

**Abstract:** Discrete world models tokenize observations and train a transformer with cross-entropy over next-token targets, but hard CE treats every wrong token as equally wrong. We introduce *Structured Label Smoothing* (SLS), a metric-aware soft-target objective that allocates probability mass to nearby tokens under a tokenizer-defined distance. The method is not tied to one encoder: FSQ provides an integer lattice distance, while VQ-style tokenizers can use codebook or embedding distance. Our current method is **annealed SLS**: use structured smoothing early to reduce brittle optimization, then anneal the smoothing mass to zero so training converges back to ordinary CE. The paper now separates two contributions: SLS as a benchmarked objective on an accepted discrete world-model baseline, and the Geometry Dash Vision-Model-Controller system as the domain-specific application where FSQ originally exposed the problem, including dreamed control and procedural level-continuation generation from real gameplay prefixes.

<p align="center">
   <b>[ <a href="https://tariolle.github.io/sls-wm/static/pdfs/sls_wm.pdf">Paper Draft</a> | <a href="https://tariolle.github.io/sls-wm/">Website</a> ]</b>
</p>

<p align="center">
  <img src="docs/static/images/pipeline.png" width="80%">
</p>

> **Project status (updated 2026-06-08).**
>
> **Current framing.** SLS started from an FSQ observation in Geometry Dash: the world model could predict visually correct frames while token accuracy stayed low, because near-neighbour FSQ codes were penalized like unrelated codes. That remains the motivating special case, but it is no longer the whole claim. The method claim is now tokenizer-metric agnostic: if a discrete tokenizer gives a meaningful neighbourhood over tokens, SLS replaces one-hot CE with a structured local target, and annealed SLS gradually returns to CE to avoid an asymptotic bias.
>
> **Why the tokenizer stays reconstruction-anchored.** We tested a constrained FSQ-JEPA hybrid inside the existing token architecture. This was not a faithful LeWorldModel/SIGReg port: canonical SIGReg regularizes continuous Gaussian embeddings, whereas this system keeps bounded FSQ tokens for the dynamics model and controller. The hybrid trained, but worked worse for control than the sequential reconstruction-anchored pipeline. Our interpretation is domain-specific rather than anti-JEPA: in natural video, predicting every pixel can waste capacity on unpredictable nuisance detail; in deterministic games, the pixels are repeatable outputs of the simulator, so reconstruction preserves useful state information for tokenization and control.
>
> **Why evaluation moved to IRIS.** Geometry Dash remains the application, but its deploy metric (% of level reached) is a serial-difficulty cut with low signal for method comparison. Rather than keep trying to port the full Geometry Dash VMC stack to Atari, the benchmark path now uses an accepted world-model baseline and changes only the loss target. IRIS/Pong is the first controlled baseline: CE and annealed SLS are the active multi-seed comparison, with fixed SLS retained as a first-seed negative control.
>
> **Current evidence.** On the first IRIS Pong seed, fixed SLS improved the early stability window but collapsed asymptotically (final return `0.125` vs CE `20.9375`). Annealed SLS kept the stabilizing effect while recovering near-CE performance (final return `19.625`, best `19.8125`). The paper claim should therefore be about **annealed SLS as a stability/sample-efficiency regularizer that converges back to CE**, with fixed SLS retained as an ablation showing why annealing matters.
>
> **What stays, what changes.** The locked Geometry Dash instantiation is **V7** and remains the application showcase: FSQ tokens, imagined rollout/control, procedural rollout or level continuation generation, and **~15 ms total inference per frame on RTX 2060 SUPER** (~67 FPS achievable; deployed at 30 FPS for capture-rate stability). The full V7-native Atari controller path is retired. Atari appears only through accepted-baseline SLS-vs-CE validation.

> **Status:** V7 Geometry Dash is frozen as the application showcase. IRIS/Pong CE, fixed SLS, and annealed SLS single-seed runs are complete, and additional CE vs. annealed-SLS Pong seeds are the current paper-critical work. Any conference/workshop venue will be decided after the benchmark freeze. Next paper-critical work is to turn the current result into defensible evidence: multi-seed plots, stability/sample-efficiency KPIs, and at most one additional Atari game if Pong alone is too narrow.

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

# Batch-1 world-model latency

This benchmark characterizes the computational cost of one operational
imagination transition for the released DashVMC, IRIS, and DIAMOND models on
the DashVMC deployment GPU. It is an architecture-level latency comparison,
not a Geometry Dash control or generation-quality comparison.

The external repositories were checked out at:

- IRIS: `24326aaaa283c527f42b89b44cfdecf2665a7a16`
- DIAMOND: `5bcd1599755b4f2fae8e5e079e02f0728e174965`

The released Pong checkpoints are downloaded from
`eloialonso/iris` and `eloialonso/diamond` on Hugging Face. The IRIS
training-only LPIPS network is omitted from inference. Each command runs in a
fresh process with batch size 1 and FP32 eager execution. After 30 warm-up
transitions, 100 transitions are timed individually with a CUDA synchronization
at each boundary. `results.md` contains two independent runs per model and
the average of their run means.

Example PowerShell setup and commands:

```powershell
git clone https://github.com/eloialonso/iris.git $env:TEMP\iris
git -C $env:TEMP\iris checkout 24326aaaa283c527f42b89b44cfdecf2665a7a16
git clone https://github.com/eloialonso/diamond.git $env:TEMP\diamond
git -C $env:TEMP\diamond checkout 5bcd1599755b4f2fae8e5e079e02f0728e174965
python -m pip install einops==0.3.2 gym==0.26.2 hydra-core==1.3 huggingface-hub torcheval==0.0.7
python analysis/2026-07-26_world_model_latency/benchmark.py --model dashvmc
python analysis/2026-07-26_world_model_latency/benchmark.py --model iris --external-root $env:TEMP\iris
python analysis/2026-07-26_world_model_latency/benchmark.py --model diamond --external-root $env:TEMP\diamond
```

The measured operations follow the native imagination interfaces and therefore
produce different representations:

- DashVMC predicts a 64-token latent grid in parallel and a death score.
- IRIS autoregressively predicts 16 tokens, decodes the observation, and
  predicts reward and termination.
- DIAMOND performs three-step pixel diffusion and predicts reward and
  termination.

This is the operational difference the benchmark is intended to expose. It
does not establish that DashVMC has better control or generation quality, and
the external models were not trained on the DashVMC dataset.

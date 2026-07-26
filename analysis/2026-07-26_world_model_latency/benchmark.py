"""Batch-1 latency benchmark for released DashVMC, IRIS, and DIAMOND models.

Run each model in a fresh process because the external repositories both use
top-level ``models`` and ``utils`` packages. See README.md in this directory
for environment setup and exact commands.
"""

from __future__ import annotations

import argparse
import gc
import json
import statistics
import sys
import time
from pathlib import Path

import torch


def measure(fn, warmup: int, repeats: int) -> dict[str, float]:
    for _ in range(warmup):
        fn()
    torch.cuda.synchronize()
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        torch.cuda.synchronize()
        samples.append((time.perf_counter() - start) * 1000.0)
    samples.sort()
    return {
        "mean_ms": statistics.fmean(samples),
        "median_ms": samples[len(samples) // 2],
        "p95_ms": samples[min(len(samples) - 1, int(0.95 * len(samples)))],
    }


def checkpoint_or_download(path: str | None, repo: str, filename: str) -> Path:
    if path:
        return Path(path).resolve()
    from huggingface_hub import hf_hub_download

    return Path(hf_hub_download(repo, filename))


def benchmark_dashvmc(args, device: torch.device) -> dict:
    repo = Path(args.repo_root).resolve()
    sys.path.insert(0, str(repo))
    from deepdash.world_model import WorldModel

    model = WorldModel(
        vocab_size=1000,
        n_actions=2,
        embed_dim=384,
        n_heads=8,
        n_layers=8,
        context_frames=4,
        dropout=0.1,
        tokens_per_frame=64,
        adaln=False,
        fsq_dim=None,
        use_cpc=False,
    ).to(device).eval()
    ckpt = Path(args.checkpoint).resolve() if args.checkpoint else repo / "checkpoints_v7" / "transformer_best.pt"
    state = torch.load(ckpt, map_location=device, weights_only=True)
    state = {k.removeprefix("_orig_mod."): v for k, v in state.items()}
    model.load_state_dict(state, strict=False)
    del state

    context = torch.randint(0, 1000, (1, 4, 65), device=device)
    context[:, :, -1] = model.ALIVE_TOKEN
    actions = torch.zeros(1, 4, dtype=torch.long, device=device)

    def transition():
        return model.predict_next_frame(context, actions)

    torch.cuda.reset_peak_memory_stats()
    with torch.inference_mode():
        latency = measure(transition, args.warmup, args.repeats)
    return {
        "model": "DashVMC selected checkpoint",
        "operation": "parallel latent grid and death score",
        "parameters": sum(p.numel() for p in model.parameters()),
        "latency": latency,
        "peak_allocated_mib": torch.cuda.max_memory_allocated() / 2**20,
    }


def benchmark_iris(args, device: torch.device) -> dict:
    if not args.external_root:
        raise ValueError("--external-root must point to an IRIS checkout")
    root = Path(args.external_root).resolve()
    sys.path.insert(0, str(root / "src"))
    from envs.world_model_env import WorldModelEnv
    from hydra.utils import instantiate
    from models.actor_critic import ActorCritic
    from models.world_model import WorldModel
    from omegaconf import OmegaConf
    from agent import Agent
    from utils import extract_state_dict

    ckpt = checkpoint_or_download(args.checkpoint, "eloialonso/iris", "pretrained_models/Pong.pt")
    saved = torch.load(ckpt, map_location="cpu", weights_only=False)
    actor_state = extract_state_dict(saved, "actor_critic")
    num_actions = actor_state["actor_linear.weight"].shape[0]
    tokenizer_cfg = OmegaConf.load(root / "config" / "tokenizer" / "default.yaml")
    tokenizer_cfg.with_lpips = False
    tokenizer = instantiate(tokenizer_cfg)
    world_cfg = instantiate(OmegaConf.load(root / "config" / "world_model" / "default.yaml"))
    agent = Agent(
        tokenizer,
        WorldModel(tokenizer.vocab_size, num_actions, world_cfg),
        ActorCritic(num_actions, use_original_obs=False),
    )
    agent.tokenizer.load_state_dict(extract_state_dict(saved, "tokenizer"), strict=False)
    agent.world_model.load_state_dict(extract_state_dict(saved, "world_model"))
    agent.actor_critic.load_state_dict(actor_state)
    del saved, actor_state
    gc.collect()
    agent = agent.to(device).eval()

    obs = torch.rand(1, 3, 64, 64, device=device)
    env = WorldModelEnv(agent.tokenizer, agent.world_model, device)
    with torch.inference_mode():
        env.reset_from_initial_observations(obs)

    action = torch.zeros(1, dtype=torch.long, device=device)

    def transition():
        return env.step(action)

    torch.cuda.reset_peak_memory_stats()
    with torch.inference_mode():
        latency = measure(transition, args.warmup, args.repeats)
    return {
        "model": "IRIS released Pong checkpoint",
        "operation": "16 autoregressive tokens, decoded observation, reward and termination",
        "parameters_excluding_training_only_lpips": sum(p.numel() for p in agent.parameters()),
        "latency": latency,
        "peak_allocated_mib": torch.cuda.max_memory_allocated() / 2**20,
    }


def benchmark_diamond(args, device: torch.device) -> dict:
    if not args.external_root:
        raise ValueError("--external-root must point to a DIAMOND checkout")
    root = Path(args.external_root).resolve()
    sys.path.insert(0, str(root / "src"))
    from agent import Agent
    from hydra.utils import instantiate
    from models.diffusion import DiffusionSampler, DiffusionSamplerConfig
    from omegaconf import OmegaConf
    from utils import extract_state_dict

    cfg_path = checkpoint_or_download(None, "eloialonso/diamond", "atari_100k/config/agent/default.yaml")
    ckpt = checkpoint_or_download(args.checkpoint, "eloialonso/diamond", "atari_100k/models/Pong.pt")
    saved = torch.load(ckpt, map_location="cpu", weights_only=False)
    actor_state = extract_state_dict(saved, "actor_critic")
    num_actions = actor_state["actor_linear.weight"].shape[0]
    cfg = OmegaConf.load(cfg_path)
    cfg.num_actions = num_actions
    cfg.rew_end_model.img_channels = cfg.denoiser.inner_model.img_channels
    cfg.rew_end_model.img_size = 64
    cfg.actor_critic.img_channels = cfg.denoiser.inner_model.img_channels
    cfg.actor_critic.img_size = 64
    agent = Agent(instantiate(cfg))
    agent.load(ckpt)
    del saved, actor_state
    gc.collect()
    agent = agent.to(device).eval()

    sampler = DiffusionSampler(
        agent.denoiser,
        DiffusionSamplerConfig(
            num_steps_denoising=3,
            sigma_min=2e-3,
            sigma_max=5.0,
            rho=7,
            order=1,
            s_churn=0.0,
            s_tmin=0.0,
            s_tmax=float("inf"),
            s_noise=1.0,
        ),
    )
    obs = torch.rand(1, 4, 3, 64, 64, device=device).mul(2).sub(1)
    actions = torch.zeros(1, 4, dtype=torch.long, device=device)
    hx = torch.zeros(1, 1, agent.rew_end_model.lstm.hidden_size, device=device)
    cx = torch.zeros_like(hx)

    def transition():
        next_obs = sampler.sample(obs, actions)[0]
        agent.rew_end_model.predict_rew_end(
            obs[:, -1:], actions[:, -1:], next_obs.unsqueeze(1), (hx, cx)
        )
        return next_obs

    torch.cuda.reset_peak_memory_stats()
    with torch.inference_mode():
        latency = measure(transition, args.warmup, args.repeats)
    return {
        "model": "DIAMOND released Pong checkpoint",
        "operation": "three-step pixel diffusion, reward and termination",
        "parameters": sum(p.numel() for p in agent.parameters()),
        "latency": latency,
        "peak_allocated_mib": torch.cuda.max_memory_allocated() / 2**20,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=("dashvmc", "iris", "diamond"), required=True)
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--external-root")
    parser.add_argument("--checkpoint")
    parser.add_argument("--warmup", type=int, default=30)
    parser.add_argument("--repeats", type=int, default=100)
    args = parser.parse_args()
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required")
    device = torch.device("cuda")
    result = {
        "device": torch.cuda.get_device_name(0),
        "total_vram_mib": torch.cuda.get_device_properties(0).total_memory / 2**20,
        "torch": torch.__version__,
        "batch_size": 1,
        "dtype": "float32",
        "warmup": args.warmup,
        "repeats": args.repeats,
    }
    fn = {
        "dashvmc": benchmark_dashvmc,
        "iris": benchmark_iris,
        "diamond": benchmark_diamond,
    }[args.model]
    result.update(fn(args, device))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

"""Automated real-game evaluation: run N episodes and collect statistics.

Runs the full deploy pipeline (screen capture -> FSQ -> Transformer -> Controller)
on the real game, detects death via memory reading, and logs survival stats.

Two inference paths are available:
  diagnostic (default): CPU/NumPy context + per-stage CUDA syncs for stage timing
  optimized: same hot path as scripts/deploy.py (FSQ torch.compile, transformer
             CUDA graph, GPU-resident context, no per-stage syncs)

The game must be running and the player must be in a level.
After each death, the game auto-respawns -- the script waits for respawn
and starts the next episode automatically.

Usage:
    python scripts/eval_real_game.py --n-runs 100
    python scripts/eval_real_game.py --n-runs 50 --output eval_results.json
    python scripts/eval_real_game.py --policy no-op --n-runs 10
    python scripts/eval_real_game.py --inference-path optimized --fps 60 --n-runs 20
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import sys
import time
from pathlib import Path

import cv2
import dxcam
import keyboard
import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deepdash.fsq import FSQVAE
from deepdash.world_model import WorldModel
from deepdash.controller import CNNPolicy, MLPPolicy, V3CNNPolicy
from deepdash.gd_mem import GDReader


_SOBEL_X = torch.tensor(
    [[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]],
    dtype=torch.float32,
).reshape(1, 1, 3, 3)
_SOBEL_Y = torch.tensor(
    [[-1, -2, -1], [0, 0, 0], [1, 2, 1]],
    dtype=torch.float32,
).reshape(1, 1, 3, 3)
_sobel_device = None


def _init_sobel_kernels(device):
    global _SOBEL_X, _SOBEL_Y, _sobel_device
    if _sobel_device != device:
        _SOBEL_X = _SOBEL_X.to(device)
        _SOBEL_Y = _SOBEL_Y.to(device)
        _sobel_device = device


def file_sha256(path):
    """Return a file SHA-256, or None when the policy has no checkpoint."""
    if path is None:
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bootstrap_mean_ci(values, n_resamples, seed):
    """Percentile bootstrap CI for a sample mean, computed in batches."""
    arr = np.asarray(values, dtype=np.float64)
    rng = np.random.default_rng(seed)
    bootstrap_means = []
    batch_size = 1_000
    for start in range(0, n_resamples, batch_size):
        size = min(batch_size, n_resamples - start)
        samples = rng.choice(arr, size=(size, arr.size), replace=True)
        bootstrap_means.append(samples.mean(axis=1))
    bootstrap_means = np.concatenate(bootstrap_means)
    return np.percentile(bootstrap_means, [2.5, 97.5])


class StageTimer:
    """Stage timer with percentile and bootstrap summaries."""

    def __init__(self, enabled, bootstrap_resamples=10_000,
                 bootstrap_seed=20260720):
        self.enabled = enabled
        self.bootstrap_resamples = bootstrap_resamples
        self.bootstrap_seed = bootstrap_seed
        self.samples = {}
        self._starts = {}

    def start(self, name):
        if self.enabled:
            self._starts[name] = time.perf_counter()

    def end(self, name):
        if not self.enabled:
            return
        elapsed_ms = (time.perf_counter() - self._starts.pop(name)) * 1000.0
        self.samples.setdefault(name, []).append(elapsed_ms)

    def cuda_sync(self, device):
        if self.enabled and device.type == "cuda":
            torch.cuda.synchronize()

    def summary(self):
        out = {}
        rng = np.random.default_rng(self.bootstrap_seed)
        for name, values in self.samples.items():
            arr = np.asarray(values, dtype=np.float64)
            stats = {
                "mean_ms": round(float(arr.mean()), 3),
                "median_ms": round(float(np.median(arr)), 3),
                "p25_ms": round(float(np.percentile(arr, 25)), 3),
                "p75_ms": round(float(np.percentile(arr, 75)), 3),
                "p95_ms": round(float(np.percentile(arr, 95)), 3),
                "min_ms": round(float(arr.min()), 3),
                "max_ms": round(float(arr.max()), 3),
                "n": int(arr.size),
                "samples_ms": [round(float(value), 3) for value in arr],
            }
            if self.bootstrap_resamples > 0:
                ci_low, ci_high = bootstrap_mean_ci(
                    arr, self.bootstrap_resamples,
                    int(rng.integers(0, np.iinfo(np.int32).max)),
                )
                stats["mean_ci95_ms"] = [
                    round(float(ci_low), 3), round(float(ci_high), 3)
                ]
            out[name] = stats
        component_names = [
            "capture", "sobel_preprocess", "fsq_encode", "transformer",
            "controller_action",
        ]
        if all(name in out for name in component_names):
            out["component_sum_mean_ms"] = round(
                sum(out[name]["mean_ms"] for name in component_names), 3
            )
        out["bootstrap_resamples"] = self.bootstrap_resamples
        out["bootstrap_seed"] = self.bootstrap_seed
        return out


def preprocess_frame(rgb, crop_x, crop_y, crop_size, target_size, device=None):
    """RGB screenshot -> 64x64 Sobel edge map (uint8). GPU Sobel + CPU resize."""
    cropped = rgb[crop_y:crop_y + crop_size, crop_x:crop_x + crop_size]
    gray = cv2.cvtColor(cropped, cv2.COLOR_RGB2GRAY)

    if device is not None and device.type == "cuda":
        gray_t = torch.from_numpy(gray).float().unsqueeze(0).unsqueeze(0).to(device)
        padded = torch.nn.functional.pad(gray_t, (1, 1, 1, 1), mode='reflect')
        sx_k = torch.tensor([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]],
                            dtype=torch.float32, device=device).reshape(1, 1, 3, 3)
        sy_k = torch.tensor([[-1, -2, -1], [0, 0, 0], [1, 2, 1]],
                            dtype=torch.float32, device=device).reshape(1, 1, 3, 3)
        sx = torch.nn.functional.conv2d(padded, sx_k)
        sy = torch.nn.functional.conv2d(padded, sy_k)
        mag = torch.sqrt(sx ** 2 + sy ** 2)
        edges = torch.clamp(torch.round(mag), 0, 255).to(torch.uint8)
        edges = edges.squeeze().cpu().numpy()
    else:
        sobel_x = cv2.Sobel(gray, cv2.CV_16S, 1, 0, ksize=3)
        sobel_y = cv2.Sobel(gray, cv2.CV_16S, 0, 1, ksize=3)
        edges = cv2.convertScaleAbs(cv2.magnitude(
            sobel_x.astype(np.float32), sobel_y.astype(np.float32)))

    return cv2.resize(edges, (target_size, target_size),
                      interpolation=cv2.INTER_AREA)


def preprocess_frame_optimized(rgb, crop_x, crop_y, crop_size, target_size, device):
    """Deploy-equivalent preprocess: GPU Sobel + CPU resize, no stage syncs."""
    cropped = rgb[crop_y:crop_y + crop_size, crop_x:crop_x + crop_size]
    gray = cv2.cvtColor(cropped, cv2.COLOR_RGB2GRAY)
    if device.type == "cuda":
        _init_sobel_kernels(device)
        gray_t = torch.from_numpy(gray).float().unsqueeze(0).unsqueeze(0).to(device)
        padded = torch.nn.functional.pad(gray_t, (1, 1, 1, 1), mode="reflect")
        sx = torch.nn.functional.conv2d(padded, _SOBEL_X)
        sy = torch.nn.functional.conv2d(padded, _SOBEL_Y)
        mag = torch.sqrt(sx ** 2 + sy ** 2)
        edges = torch.clamp(torch.round(mag), 0, 255).to(torch.uint8).squeeze().cpu().numpy()
    else:
        sobel_x = cv2.Sobel(gray, cv2.CV_16S, 1, 0, ksize=3)
        sobel_y = cv2.Sobel(gray, cv2.CV_16S, 0, 1, ksize=3)
        edges = cv2.convertScaleAbs(cv2.magnitude(
            sobel_x.astype(np.float32), sobel_y.astype(np.float32)))
    return cv2.resize(edges, (target_size, target_size), interpolation=cv2.INTER_AREA)


def enable_optimized_inference(vae, wm, device, context_frames):
    """Match scripts/deploy.py optimized path: compile FSQ + CUDA graph encode."""
    use_compile = False
    use_cuda_graph = False
    encode_graph = None
    graph_ctx_t = None
    graph_ctx_a = None
    graph_h_t = None
    pin_buf = None

    if device.type != "cuda":
        return {
            "fsq_torch_compile": False,
            "transformer_cuda_graph": False,
            "gpu_resident_context": True,
            "pinned_frame_buffer": False,
            "per_stage_cuda_synchronization": False,
            "encode_graph": None,
            "graph_ctx_t": None,
            "graph_ctx_a": None,
            "graph_h_t_holder": None,
            "pin_buf": None,
        }

    torch.backends.cudnn.benchmark = True
    try:
        vae.encode = torch.compile(vae.encode)
        print("torch.compile enabled for FSQ")
        with torch.no_grad():
            dummy_frame = torch.zeros(1, 1, 64, 64, device=device)
            for _ in range(3):
                vae.encode(dummy_frame)
        torch.cuda.synchronize()
        use_compile = True
    except Exception as exc:
        print(f"torch.compile not available: {exc}")

    try:
        graph_ctx_t = torch.zeros(
            1, context_frames, 65, dtype=torch.long, device=device
        )
        graph_ctx_a = torch.zeros(
            1, context_frames, dtype=torch.long, device=device
        )
        with torch.no_grad():
            for _ in range(3):
                wm.encode_context(graph_ctx_t, graph_ctx_a)
        torch.cuda.synchronize()
        encode_graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(encode_graph):
            with torch.no_grad():
                graph_h_t = wm.encode_context(graph_ctx_t, graph_ctx_a)
        use_cuda_graph = True
        print("CUDA Graph captured for encode_context")
    except Exception as exc:
        print(f"CUDA Graph capture failed, using eager: {exc}")
        encode_graph = None
        graph_ctx_t = None
        graph_ctx_a = None
        graph_h_t = None

    pin_buf = torch.zeros(1, 1, 64, 64, dtype=torch.float32, pin_memory=True)
    return {
        "fsq_torch_compile": use_compile,
        "transformer_cuda_graph": use_cuda_graph,
        "gpu_resident_context": True,
        "pinned_frame_buffer": True,
        "per_stage_cuda_synchronization": False,
        "encode_graph": encode_graph,
        "graph_ctx_t": graph_ctx_t,
        "graph_ctx_a": graph_ctx_a,
        "graph_h_t_holder": [graph_h_t],
        "pin_buf": pin_buf,
    }


def run_episode_diagnostic(
    *,
    gd,
    cam,
    device,
    vae,
    wm,
    controller,
    policy_class,
    uses_controller,
    region,
    crop_x,
    crop_y,
    crop_size,
    frame_interval,
    jump_threshold,
    K,
    stage_timer,
    latency_samples,
    latency_frames,
):
    """Original diagnostic path with per-stage CUDA syncs."""
    ctx_tokens = []
    ctx_actions = []
    jumping = False
    warmup_frames = K - 1
    frames_survived = 0
    t_start = time.perf_counter()
    keyboard.release("space")

    while True:
        t0 = time.perf_counter()
        stage_timer.start("full_loop")

        if keyboard.is_pressed("f10"):
            break

        if gd.is_dead():
            if jumping:
                keyboard.release("space")
                jumping = False
            break

        if not uses_controller:
            if warmup_frames > 0:
                warmup_frames -= 1
            else:
                frames_survived += 1
            elapsed = time.perf_counter() - t0
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)
            continue

        stage_timer.start("capture")
        img = cam.grab(region=region)
        stage_timer.end("capture")
        if img is None:
            time.sleep(0.001)
            continue

        stage_timer.start("sobel_preprocess")
        edge_frame = preprocess_frame(img, crop_x, crop_y, crop_size, 64, device)
        stage_timer.cuda_sync(device)
        stage_timer.end("sobel_preprocess")

        stage_timer.start("fsq_encode")
        with torch.no_grad(), torch.amp.autocast("cuda", enabled=device.type == "cuda"):
            frame_t = torch.from_numpy(edge_frame.astype(np.float32) / 255.0)
            frame_t = frame_t.unsqueeze(0).unsqueeze(0).to(device)
            tokens = vae.encode(frame_t)
            tokens_flat = tokens.reshape(-1).cpu().numpy().astype(np.int64)
        stage_timer.cuda_sync(device)
        stage_timer.end("fsq_encode")

        ctx_tokens.append(tokens_flat)
        ctx_actions.append(1 if jumping else 0)
        if len(ctx_tokens) > K:
            ctx_tokens = ctx_tokens[-K:]
            ctx_actions = ctx_actions[-K:]

        if len(ctx_tokens) < K:
            elapsed = time.perf_counter() - t0
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)
            continue

        stage_timer.start("transformer")
        with torch.no_grad(), torch.amp.autocast("cuda", enabled=device.type == "cuda"):
            ctx_tok_np = np.array(ctx_tokens)
            ctx_act_np = np.array(ctx_actions)
            status = np.full((K, 1), wm.ALIVE_TOKEN, dtype=np.int64)
            ctx_with_status = np.concatenate([ctx_tok_np, status], axis=1)
            ctx_t = torch.from_numpy(ctx_with_status[None]).to(device)
            ctx_a = torch.from_numpy(ctx_act_np[None]).to(device)
            h_t = wm.encode_context(ctx_t, ctx_a)
        stage_timer.cuda_sync(device)
        stage_timer.end("transformer")

        stage_timer.start("controller_action")
        with torch.no_grad():
            if policy_class == "mlp":
                prob, _ = controller(h_t.float())
            else:
                z_t = torch.from_numpy(ctx_tokens[-1][None]).to(device)
                prob, _ = controller(z_t, h_t.float())
            jump = prob[0].item() > jump_threshold
        stage_timer.cuda_sync(device)
        stage_timer.end("controller_action")

        if jump and not jumping:
            keyboard.press("space")
            jumping = True
        elif not jump and jumping:
            keyboard.release("space")
            jumping = False

        frames_survived += 1
        stage_timer.end("full_loop")
        if stage_timer.enabled:
            latency_frames += 1
            if latency_frames >= latency_samples:
                stage_timer.enabled = False

        elapsed = time.perf_counter() - t0
        if elapsed < frame_interval:
            time.sleep(frame_interval - elapsed)

    return frames_survived, time.perf_counter() - t_start, latency_frames


def run_episode_optimized(
    *,
    gd,
    cam,
    device,
    vae,
    wm,
    controller,
    policy_class,
    uses_controller,
    region,
    crop_x,
    crop_y,
    crop_size,
    frame_interval,
    jump_threshold,
    K,
    stage_timer,
    latency_samples,
    latency_frames,
    opt,
):
    """Deploy-equivalent optimized path without per-stage CUDA syncs."""
    jumping = False
    frames_survived = 0
    t_start = time.perf_counter()
    keyboard.release("space")

    if not uses_controller:
        warmup_frames = K - 1
        while True:
            t0 = time.perf_counter()
            if keyboard.is_pressed("f10"):
                break
            if gd.is_dead():
                break
            if warmup_frames > 0:
                warmup_frames -= 1
            else:
                frames_survived += 1
            elapsed = time.perf_counter() - t0
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)
        return frames_survived, time.perf_counter() - t_start, latency_frames

    tokens_per_frame = 64
    ctx_tokens = torch.zeros(K, tokens_per_frame, dtype=torch.long, device=device)
    ctx_actions = torch.zeros(K, dtype=torch.long, device=device)
    ctx_status = torch.full((K, 1), wm.ALIVE_TOKEN, dtype=torch.long, device=device)
    ctx_fill = 0
    pin_buf = opt["pin_buf"]
    encode_graph = opt["encode_graph"]
    graph_ctx_t = opt["graph_ctx_t"]
    graph_ctx_a = opt["graph_ctx_a"]
    graph_h_holder = opt["graph_h_t_holder"]
    exact_preprocessor = opt.get("exact_preprocessor")

    while True:
        t0 = time.perf_counter()
        stage_timer.start("full_loop")

        if keyboard.is_pressed("f10"):
            break

        if gd.is_dead():
            if jumping:
                keyboard.release("space")
                jumping = False
            break

        stage_timer.start("capture")
        img = cam.grab(region=region)
        stage_timer.end("capture")
        if img is None:
            time.sleep(0.001)
            continue

        stage_timer.start("sobel_preprocess")
        if exact_preprocessor is not None:
            cropped = img[
                crop_y:crop_y + crop_size,
                crop_x:crop_x + crop_size,
            ]
            gray = cv2.cvtColor(cropped, cv2.COLOR_RGB2GRAY)
            frame_t = exact_preprocessor.preprocess_gray_float(gray)
        else:
            edge_frame = preprocess_frame_optimized(
                img, crop_x, crop_y, crop_size, 64, device
            )
        stage_timer.end("sobel_preprocess")

        stage_timer.start("fsq_encode")
        with torch.no_grad():
            if exact_preprocessor is None:
                if pin_buf is not None:
                    pin_buf[0, 0] = torch.from_numpy(
                        edge_frame.astype(np.float32) * (1.0 / 255.0)
                    )
                    frame_t = pin_buf.to(device, non_blocking=True)
                else:
                    frame_t = torch.from_numpy(
                        edge_frame.astype(np.float32) * (1.0 / 255.0)
                    )
                    frame_t = frame_t.unsqueeze(0).unsqueeze(0).to(device)
            tokens = vae.encode(frame_t).reshape(tokens_per_frame)
        stage_timer.end("fsq_encode")

        if ctx_fill < K:
            ctx_tokens[ctx_fill] = tokens
            ctx_actions[ctx_fill] = 1 if jumping else 0
            ctx_fill += 1
        else:
            ctx_tokens[:-1] = ctx_tokens[1:].clone()
            ctx_actions[:-1] = ctx_actions[1:].clone()
            ctx_tokens[-1] = tokens
            ctx_actions[-1] = 1 if jumping else 0

        if ctx_fill < K:
            elapsed = time.perf_counter() - t0
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)
            continue

        stage_timer.start("transformer")
        if encode_graph is not None:
            ctx_t = torch.cat([ctx_tokens, ctx_status], dim=1).unsqueeze(0)
            graph_ctx_t.copy_(ctx_t)
            graph_ctx_a.copy_(ctx_actions.unsqueeze(0))
            encode_graph.replay()
            h_t = graph_h_holder[0]
        else:
            with torch.no_grad():
                ctx_t = torch.cat([ctx_tokens, ctx_status], dim=1).unsqueeze(0)
                ctx_a = ctx_actions.unsqueeze(0)
                h_t = wm.encode_context(ctx_t, ctx_a)
        stage_timer.end("transformer")

        stage_timer.start("controller_action")
        with torch.no_grad():
            if policy_class == "mlp":
                prob, _ = controller(h_t.float())
            else:
                z_t = ctx_tokens[-1:].clone()
                prob, _ = controller(z_t, h_t.float())
            # Natural end-of-frame sync, same as deploy.py
            jump = prob[0].item() > jump_threshold
        stage_timer.end("controller_action")

        if jump and not jumping:
            keyboard.press("space")
            jumping = True
        elif not jump and jumping:
            keyboard.release("space")
            jumping = False

        frames_survived += 1
        stage_timer.end("full_loop")
        if stage_timer.enabled:
            latency_frames += 1
            if latency_frames >= latency_samples:
                stage_timer.enabled = False

        elapsed = time.perf_counter() - t0
        if elapsed < frame_interval:
            time.sleep(frame_interval - elapsed)

    return frames_survived, time.perf_counter() - t_start, latency_frames


def main():
    parser = argparse.ArgumentParser(description="Automated real-game evaluation")
    parser.add_argument("--n-runs", type=int, default=100)
    parser.add_argument("--policy", choices=["ppo", "bc", "no-op"],
                        default="ppo",
                        help="Action policy. BC and PPO use the full model path; "
                             "no-op never presses jump.")
    parser.add_argument("--vae-checkpoint", default="checkpoints/fsq_best.pt")
    parser.add_argument("--transformer-checkpoint",
                        default="checkpoints/transformer_best.pt")
    parser.add_argument("--controller-checkpoint", default=None)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--jump-threshold", type=float, default=0.5)
    parser.add_argument("--level-name", default=None)
    parser.add_argument("--system-variant", default="V7")
    parser.add_argument(
        "--inference-path",
        choices=["diagnostic", "optimized"],
        default="diagnostic",
        help="diagnostic = stage-synced evaluator path; optimized = deploy.py "
             "compiled/CUDA-graph path without per-stage syncs.",
    )
    parser.add_argument(
        "--preprocessor",
        choices=("hybrid", "exact-cuda"),
        default="hybrid",
        help=(
            "Preprocessing implementation for the optimized path. "
            "exact-cuda is byte-identical to the recording pipeline and "
            "keeps the 64x64 observation on GPU."
        ),
    )
    parser.add_argument("--latency-samples", type=int, default=300,
                        help="Number of frames to include in stage timing.")
    parser.add_argument("--latency-bootstrap-resamples", type=int, default=10_000,
                        help="Bootstrap resamples for latency mean CIs.")
    parser.add_argument("--survival-bootstrap-resamples", type=int, default=50_000,
                        help="Bootstrap resamples for mean survival CI.")
    parser.add_argument("--config", default=None)
    parser.add_argument("--levels", type=int, nargs="+", default=None)
    parser.add_argument("--vocab-size", type=int, default=None)
    parser.add_argument("--embed-dim", type=int, default=None)
    parser.add_argument("--n-heads", type=int, default=None)
    parser.add_argument("--n-layers", type=int, default=None)
    parser.add_argument("--tokens-per-frame", type=int, default=None)
    parser.add_argument("--context-frames", type=int, default=None)
    parser.add_argument("--dropout", type=float, default=None)
    parser.add_argument("--policy-class", type=str, default=None,
                        choices=["mlp", "cnn", "v3_cnn"],
                        help="Controller architecture. V7 uses v3_cnn.")
    parser.add_argument("--output", default="eval_results.json")
    args = parser.parse_args()

    from deepdash.config import apply_config
    apply_config(args)
    apply_config(args, section="controller_ppo")

    if args.controller_checkpoint is None and args.policy != "no-op":
        checkpoint_dir = Path(getattr(args, "checkpoint_dir", "checkpoints"))
        filename = "controller_bc_best.pt" if args.policy == "bc" \
            else "controller_ppo_best.pt"
        args.controller_checkpoint = str(checkpoint_dir / filename)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")
    K = args.context_frames
    uses_controller = args.policy in {"ppo", "bc"}
    use_optimized = args.inference_path == "optimized"

    if args.preprocessor == "exact-cuda" and not use_optimized:
        parser.error("--preprocessor exact-cuda requires --inference-path optimized")

    if use_optimized and not uses_controller:
        print("Note: optimized path has no effect for no-op policy.")

    vae = None
    wm = None
    controller = None
    policy_class = None
    opt = {
        "fsq_torch_compile": False,
        "transformer_cuda_graph": False,
        "gpu_resident_context": False,
        "pinned_frame_buffer": False,
        "per_stage_cuda_synchronization": True,
        "encode_graph": None,
        "graph_ctx_t": None,
        "graph_ctx_a": None,
        "graph_h_t_holder": None,
        "pin_buf": None,
        "preprocessor": "hybrid",
        "exact_preprocessor": None,
    }

    if uses_controller:
        print("Loading models...")
        vae = FSQVAE(levels=args.levels).to(device)
        state = torch.load(args.vae_checkpoint, map_location=device, weights_only=True)
        state = {k.removeprefix("_orig_mod."): v for k, v in state.items()}
        vae.load_state_dict(state)
        vae.eval()
        vae.prepare_for_encoder_only()
        del state

        wm = WorldModel(
            vocab_size=args.vocab_size, embed_dim=args.embed_dim,
            n_heads=args.n_heads, n_layers=args.n_layers,
            context_frames=args.context_frames, dropout=args.dropout,
            tokens_per_frame=args.tokens_per_frame,
            adaln=getattr(args, 'adaln', False),
            fsq_dim=None,
            use_cpc=False,
        ).to(device)
        state = torch.load(args.transformer_checkpoint, map_location=device,
                           weights_only=True)
        state = {k.removeprefix("_orig_mod."): v for k, v in state.items()}
        wm.load_state_dict(state, strict=False)
        wm.eval()
        wm.prepare_for_context_only()
        del state

        grid_size = int(args.tokens_per_frame ** 0.5)
        policy_class = (getattr(args, "policy_class", None) or "mlp").lower()
        if policy_class == "v3_cnn":
            controller = V3CNNPolicy(
                vocab_size=args.vocab_size,
                grid_size=grid_size,
                token_embed_dim=getattr(args, "token_embed_dim", 16),
                h_dim=args.embed_dim,
                mtp_steps=int(getattr(args, "mtp_steps", None) or 8),
            ).to(device)
        elif policy_class == "cnn":
            controller = CNNPolicy(
                vocab_size=args.vocab_size,
                grid_size=grid_size,
                token_embed_dim=getattr(args, "token_embed_dim", 16),
                h_dim=args.embed_dim,
                temporal_dim=getattr(args, "temporal_dim", 32),
            ).to(device)
        else:
            controller = MLPPolicy(h_dim=args.embed_dim).to(device)
        state = torch.load(args.controller_checkpoint, map_location=device,
                           weights_only=True)
        if "controller" in state and isinstance(state["controller"], dict):
            state = state["controller"]
        controller.load_state_dict(state)
        controller.eval()
        del state
        retained_params = sum(
            p.numel() for module in (vae, wm, controller)
            for p in module.parameters()
        )
        print(
            f"All models loaded. Controller: {policy_class}; "
            f"retained parameters: {retained_params:,}"
        )

        if use_optimized:
            print("Enabling optimized inference path (deploy-equivalent)...")
            opt = enable_optimized_inference(vae, wm, device, K)
    else:
        print("No-op policy: model loading and screen capture are disabled.")

    crop_x, crop_y, crop_size = 660, 48, 1032
    if use_optimized and uses_controller:
        region = (crop_x, crop_y, crop_x + crop_size, crop_y + crop_size)
        preprocess_crop_x, preprocess_crop_y = 0, 0
    else:
        region = (0, 0, 1920, 1080)
        preprocess_crop_x, preprocess_crop_y = crop_x, crop_y

    if use_optimized and uses_controller and args.preprocessor == "exact-cuda":
        if device.type != "cuda":
            raise RuntimeError("--preprocessor exact-cuda requires CUDA")
        from benchmark_exact_cuda_resize import ExactCudaCandidate
        exact_preprocessor = ExactCudaCandidate(crop_size, 64, device)
        dummy_gray = np.zeros((crop_size, crop_size), dtype=np.uint8)
        for _ in range(3):
            exact_preprocessor.preprocess_gray_float(dummy_gray)
        torch.cuda.synchronize()
        opt["preprocessor"] = "exact-cuda"
        opt["exact_preprocessor"] = exact_preprocessor
        opt["pin_buf"] = None
        opt["pinned_frame_buffer"] = False
        print("Exact CUDA preprocessing enabled")
    cam = dxcam.create() if uses_controller else None
    frame_interval = 1.0 / args.fps

    gd = GDReader()
    print(f"GD memory reader connected (PID: {gd.pid})")

    results = []
    excluded_episodes = []
    stage_timer = StageTimer(
        enabled=False,
        bootstrap_resamples=args.latency_bootstrap_resamples,
    )
    latency_frames = 0
    print(f"\nRunning one unscored synchronization episode, then "
          f"{args.n_runs} {args.policy} evaluation episodes...")
    print(
        f"Inference path: {args.inference_path} / {args.preprocessor} | "
        f"target cadence: {args.fps} FPS"
    )
    print("Press F10 to abort.\n")

    episode_idx = 0
    while len(results) < args.n_runs:
        if keyboard.is_pressed("f10"):
            print("Aborted by user.")
            break

        while True:
            state_dict = gd.get_state()
            if state_dict["in_level"] and not state_dict["is_dead"]:
                break
            time.sleep(0.05)

        common = dict(
            gd=gd,
            cam=cam,
            device=device,
            vae=vae,
            wm=wm,
            controller=controller,
            policy_class=policy_class,
            uses_controller=uses_controller,
            region=region,
            crop_x=preprocess_crop_x,
            crop_y=preprocess_crop_y,
            crop_size=crop_size,
            frame_interval=frame_interval,
            jump_threshold=args.jump_threshold,
            K=K,
            stage_timer=stage_timer,
            latency_samples=args.latency_samples,
            latency_frames=latency_frames,
        )
        if use_optimized and uses_controller:
            frames_survived, episode_time, latency_frames = run_episode_optimized(
                **common, opt=opt
            )
        else:
            frames_survived, episode_time, latency_frames = run_episode_diagnostic(
                **common
            )

        episode_idx += 1
        episode = {
            "episode": episode_idx,
            "frames_survived": frames_survived,
            "seconds_survived": round(frames_survived / args.fps, 2),
            "wall_time_s": round(episode_time, 2),
            "level_progress": None,
        }

        if episode_idx == 1:
            episode["exclusion_reason"] = (
                "Initial synchronization episode; manual game resume can "
                "inflate survival time."
            )
            excluded_episodes.append(episode)
            stage_timer = StageTimer(
                enabled=uses_controller and args.latency_samples > 0,
                bootstrap_resamples=args.latency_bootstrap_resamples,
            )
            latency_frames = 0
            print(f"  Sync episode: {frames_survived} frames "
                  f"({episode_time:.1f}s) [excluded]")
        else:
            episode["run"] = len(results) + 1
            results.append(episode)
            print(f"  Run {len(results):3d}/{args.n_runs}: "
                  f"{frames_survived} frames ({episode_time:.1f}s)")

        time.sleep(1.0)

    if results:
        survivals = [r["frames_survived"] for r in results]
        print(f"\n{'='*50}")
        print(f"Results ({len(results)} runs):")
        print(f"  Mean survival: {np.mean(survivals):.1f} frames "
              f"({np.mean(survivals)/args.fps:.1f}s)")
        print(f"  Std:  {np.std(survivals):.1f}")
        print(f"  Min:  {np.min(survivals)} | Max: {np.max(survivals)}")
        print(f"  Median: {np.median(survivals):.0f}")
        p25, p75 = np.percentile(survivals, [25, 75])
        print(f"  P25: {p25:.0f} | P75: {p75:.0f}")
        survival_ci = bootstrap_mean_ci(
            survivals, args.survival_bootstrap_resamples, seed=20260720
        )
        print(f"  Mean 95% bootstrap CI: "
              f"[{survival_ci[0]:.1f}, {survival_ci[1]:.1f}] frames")

        gpu_name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
        summary = {
            "system_variant": args.system_variant,
            "policy": args.policy,
            "date_utc": datetime.now(timezone.utc).isoformat(),
            "n_runs": len(results),
            "n_episodes_executed": episode_idx,
            "excluded_initial_episodes": len(excluded_episodes),
            "initial_episode_exclusion_reason": (
                "Manual game resume can inflate the first episode."
            ),
            "level_name": args.level_name,
            "fps": args.fps,
            "inference_path": args.inference_path,
            "optimizations": {
                "preprocessor": opt.get("preprocessor", "hybrid"),
                "direct_model_crop_capture": bool(use_optimized and uses_controller),
                "preprocessed_observation_stays_on_gpu": bool(
                    opt.get("exact_preprocessor") is not None
                ),
                "fsq_torch_compile": bool(opt.get("fsq_torch_compile")),
                "transformer_cuda_graph": bool(opt.get("transformer_cuda_graph")),
                "gpu_resident_context": bool(opt.get("gpu_resident_context")),
                "pinned_frame_buffer": bool(opt.get("pinned_frame_buffer")),
                "per_stage_cuda_synchronization": bool(
                    opt.get("per_stage_cuda_synchronization")
                ),
            },
            "unscored_warmup_frames": K - 1,
            "death_detection": "Geometry Dash process memory",
            "gpu": gpu_name,
            "mean_frames": round(float(np.mean(survivals)), 1),
            "mean_frames_ci95": [
                round(float(survival_ci[0]), 1),
                round(float(survival_ci[1]), 1),
            ],
            "std_frames": round(float(np.std(survivals)), 1),
            "min_frames": int(np.min(survivals)),
            "max_frames": int(np.max(survivals)),
            "median_frames": round(float(np.median(survivals)), 0),
            "p25_frames": round(float(p25), 0),
            "p75_frames": round(float(p75), 0),
            "mean_time_s": round(float(np.mean(survivals)) / args.fps, 2),
            "mean_time_s_ci95": [
                round(float(survival_ci[0]) / args.fps, 2),
                round(float(survival_ci[1]) / args.fps, 2),
            ],
            "median_time_s": round(float(np.median(survivals)) / args.fps, 2),
            "min_time_s": round(float(np.min(survivals)) / args.fps, 2),
            "max_time_s": round(float(np.max(survivals)) / args.fps, 2),
            "p25_time_s": round(float(p25) / args.fps, 2),
            "p75_time_s": round(float(p75) / args.fps, 2),
            "level_progress_available": False,
            "survival_bootstrap_resamples": args.survival_bootstrap_resamples,
            "survival_bootstrap_seed": 20260720,
            "checkpoints": {
                "vae": args.vae_checkpoint if uses_controller else None,
                "transformer": args.transformer_checkpoint if uses_controller else None,
                "controller": args.controller_checkpoint,
            },
            "checkpoint_sha256": {
                "vae": file_sha256(args.vae_checkpoint) if uses_controller else None,
                "transformer": file_sha256(args.transformer_checkpoint)
                if uses_controller else None,
                "controller": file_sha256(args.controller_checkpoint)
                if uses_controller else None,
            },
            "config": args.config,
            "policy_class": policy_class,
            "latency": stage_timer.summary(),
            "excluded_episodes": excluded_episodes,
            "runs": results,
        }

        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(summary, f, indent=2)
        print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()

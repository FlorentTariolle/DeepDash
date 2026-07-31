"""Deploy the World Models agent on real Geometry Dash.

Captures screen at 30 FPS, runs FSQ + Transformer + Controller,
and simulates keyboard input to play the game.

Controls:
    F5  -- toggle agent on/off
    F10 -- quit

HUD: colored dot in top-left corner
    Black = standby, Red = idle, Green = jump

Usage:
    python scripts/deploy.py
    python scripts/deploy.py --controller-checkpoint checkpoints/controller_ppo_best.pt
"""

import argparse
import ctypes
import ctypes.wintypes as wt
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

import cv2
import dxcam
import keyboard
import numpy as np
import torch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from deepdash.fsq import FSQVAE
from deepdash.world_model import WorldModel
from deepdash.controller import CNNPolicy, V3CNNGridPolicy, V3CNNPolicy


# Win32 helpers for topmost window
_user32 = ctypes.windll.user32
_user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
_user32.FindWindowW.restype = wt.HWND
_user32.SetWindowPos.argtypes = [
    wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
_user32.SetWindowPos.restype = wt.BOOL
_HWND_TOPMOST = wt.HWND(-1)
_SWP_NOACTIVATE = 0x0010


def _force_topmost(window_title):
    hwnd = _user32.FindWindowW(None, window_title)
    if hwnd:
        _user32.SetWindowPos(
            hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
            0x0002 | 0x0001 | _SWP_NOACTIVATE)


_SOBEL_X = torch.tensor([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]],
                        dtype=torch.float32).reshape(1, 1, 3, 3)
_SOBEL_Y = torch.tensor([[-1, -2, -1], [0, 0, 0], [1, 2, 1]],
                        dtype=torch.float32).reshape(1, 1, 3, 3)
_sobel_device = None


def _init_sobel_kernels(device):
    global _SOBEL_X, _SOBEL_Y, _sobel_device
    if _sobel_device != device:
        _SOBEL_X = _SOBEL_X.to(device)
        _SOBEL_Y = _SOBEL_Y.to(device)
        _sobel_device = device


class StageBench:
    """Per-stage timing for the deploy hot loop.

    GPU stages use CUDA events queued asynchronously; the elapsed time is
    only queried after the natural per-frame sync (``prob[0].item()`` at
    the end of the controller block), so no extra ``cuda.synchronize()``
    is inserted into the pipeline. CPU stages use ``time.perf_counter``.
    Pending samples are accumulated into running mean/std/max — no Python
    list grows during the loop.
    """

    def __init__(self, gpu_stages, cpu_stages, device,
                 aggregate_stages=None):
        self._device = device
        self._use_cuda = device.type == "cuda"
        self._order = list(cpu_stages) + list(gpu_stages)
        self._aggregate_stages = (list(aggregate_stages)
                                  if aggregate_stages is not None
                                  else list(self._order))
        self._n = {s: 0 for s in self._order}
        self._sum = {s: 0.0 for s in self._order}
        self._sum_sq = {s: 0.0 for s in self._order}
        self._max = {s: 0.0 for s in self._order}
        self._samples = {s: [] for s in self._order}
        if self._use_cuda:
            self._events = {
                s: (torch.cuda.Event(enable_timing=True),
                    torch.cuda.Event(enable_timing=True))
                for s in gpu_stages
            }
        else:
            self._events = {}
        self._gpu_pending = []
        self._cpu_pending = []
        self._cpu_starts = {}

    def cpu_start(self, stage):
        self._cpu_starts[stage] = time.perf_counter()

    def cpu_end(self, stage):
        ms = (time.perf_counter() - self._cpu_starts[stage]) * 1000.0
        self._cpu_pending.append((stage, ms))

    def gpu_start(self, stage):
        if self._use_cuda:
            self._events[stage][0].record()
        else:
            self.cpu_start(stage)

    def gpu_end(self, stage):
        if self._use_cuda:
            self._events[stage][1].record()
            self._gpu_pending.append(stage)
        else:
            self.cpu_end(stage)

    def commit(self):
        """Read pending samples into running stats. Caller must have
        already triggered a GPU sync (e.g., ``prob[0].item()``) so the
        CUDA events are queryable without blocking."""
        for stage in self._gpu_pending:
            a, b = self._events[stage]
            ms = a.elapsed_time(b)  # ms, on the GPU stream
            self._record(stage, ms)
        self._gpu_pending.clear()
        for stage, ms in self._cpu_pending:
            self._record(stage, ms)
        self._cpu_pending.clear()

    def discard(self):
        """Drop any pending samples for the current frame (warmup, idle, no-img)."""
        self._gpu_pending.clear()
        self._cpu_pending.clear()

    def _record(self, stage, ms):
        self._n[stage] += 1
        self._sum[stage] += ms
        self._sum_sq[stage] += ms * ms
        self._samples[stage].append(ms)
        if ms > self._max[stage]:
            self._max[stage] = ms

    def count(self, stage):
        return self._n[stage]

    def count_above(self, stage, threshold_ms):
        return sum(value > threshold_ms for value in self._samples[stage])

    def results(self):
        stages = {}
        for stage in self._order:
            values = np.asarray(self._samples[stage], dtype=np.float64)
            if values.size == 0:
                continue
            stages[stage] = {
                "mean_ms": round(float(values.mean()), 3),
                "median_ms": round(float(np.median(values)), 3),
                "p25_ms": round(float(np.percentile(values, 25)), 3),
                "p75_ms": round(float(np.percentile(values, 75)), 3),
                "p95_ms": round(float(np.percentile(values, 95)), 3),
                "min_ms": round(float(values.min()), 3),
                "max_ms": round(float(values.max()), 3),
                "std_ms": round(float(values.std()), 3),
                "n": int(values.size),
                "samples_ms": [round(float(value), 3) for value in values],
            }
        stages["component_sum_mean_ms"] = round(sum(
            stages[stage]["mean_ms"] for stage in self._aggregate_stages
            if stage in stages
        ), 3)
        return stages

    def summary(self):
        n_max = max(self._n.values(), default=0)
        if n_max == 0:
            return "Pipeline timing: no frames recorded after warmup."
        lines = [f"\n=== Pipeline timing (averaged over {n_max} frames after warmup) ==="]
        lines.append(f"{'stage':<14}{'mean':>11}{'std':>11}{'max':>11}{'n':>8}")
        lines.append("-" * 55)
        results = self.results()
        for stage in self._order:
            if stage not in results:
                continue
            stats = results[stage]
            n = stats["n"]
            mean = stats["mean_ms"]
            std = stats["std_ms"]
            mx = stats["max_ms"]
            lines.append(f"{stage:<14}{mean:>9.3f}ms{std:>9.3f}ms{mx:>9.3f}ms{n:>8}")
        lines.append("-" * 55)
        lines.append(
            f"{'COMPONENT SUM':<14}{results['component_sum_mean_ms']:>9.3f}ms"
        )
        return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Deploy World Models agent on Geometry Dash")
    parser.add_argument("--vae-checkpoint", default="checkpoints/fsq_best.pt")
    parser.add_argument("--transformer-checkpoint",
                        default="checkpoints/transformer_best.pt")
    parser.add_argument("--controller-checkpoint",
                        default="checkpoints/controller_ppo_best.pt")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument(
        "--preprocessor",
        choices=("hybrid", "exact-cuda"),
        default="hybrid",
        help=(
            "hybrid keeps CUDA Sobel + CPU INTER_AREA; exact-cuda uses the "
            "bit-identity-gated Triton Sobel/area path and keeps output on GPU"
        ),
    )
    parser.add_argument("--jump-threshold", type=float, default=0.5,
                        help="Jump probability threshold (higher = less jumping)")
    parser.add_argument(
        "--benchmark-samples", type=int, default=0,
        help="Stop after this many active post-warmup frames (0 = F10 only).",
    )
    parser.add_argument(
        "--benchmark-output", default=None,
        help="Write raw optimized-path timing samples and metadata to JSON.",
    )
    # Model architecture (defaults from configs/v3.yaml)
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
                        choices=["cnn", "v3_cnn", "v3_cnn_grid"],
                        help="Controller architecture. 'cnn' = E6.10-era "
                             "CNNPolicy. 'v3_cnn' = V3-deploy/V7 faithful "
                             "(direct h_t concat, ReLU+MaxPool, MTP head). "
                             "'v3_cnn_grid' is the spatial-only ablation.")
    args = parser.parse_args()

    from deepdash.config import apply_config
    apply_config(args)
    apply_config(args, section="controller_ppo")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")
    K = args.context_frames
    policy_class = (getattr(args, "policy_class", None) or "cnn").lower()
    uses_temporal_state = policy_class != "v3_cnn_grid"

    # --- Load models ---
    print("Loading FSQ-VAE...")
    vae = FSQVAE(levels=args.levels).to(device)
    state = torch.load(args.vae_checkpoint, map_location=device,
                       weights_only=True)
    state = {k.removeprefix("_orig_mod."): v for k, v in state.items()}
    vae.load_state_dict(state)
    vae.eval()
    vae.prepare_for_encoder_only()
    del state

    wm = None
    if uses_temporal_state:
        print("Loading Transformer...")
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
    else:
        print("Grid-only inference: world model is not loaded")

    print("Loading Controller...")
    grid_size = int(args.tokens_per_frame ** 0.5)
    if policy_class == "v3_cnn":
        controller = V3CNNPolicy(
            vocab_size=args.vocab_size,
            grid_size=grid_size,
            token_embed_dim=getattr(args, 'token_embed_dim', 16),
            h_dim=args.embed_dim,
            mtp_steps=int(getattr(args, "mtp_steps", None) or 8),
        ).to(device)
        print(f"  V3CNNPolicy (mtp_steps={controller.mtp_steps})")
    elif policy_class == "v3_cnn_grid":
        controller = V3CNNGridPolicy(
            vocab_size=args.vocab_size,
            grid_size=grid_size,
            token_embed_dim=getattr(args, 'token_embed_dim', 16),
            mtp_steps=int(getattr(args, "mtp_steps", None) or 8),
        ).to(device)
        print(f"  V3CNNGridPolicy (mtp_steps={controller.mtp_steps})")
    else:
        controller = CNNPolicy(
            vocab_size=args.vocab_size,
            grid_size=grid_size,
            token_embed_dim=getattr(args, 'token_embed_dim', 16),
            h_dim=args.embed_dim,
            temporal_dim=getattr(args, 'temporal_dim', 32),
        ).to(device)
        print(f"  CNNPolicy (temporal_dim={getattr(args, 'temporal_dim', 32)})")
    state = torch.load(args.controller_checkpoint, map_location=device,
                       weights_only=True)
    # controller_ppo_best.pt is a raw state_dict; controller_ppo_latest.pt is
    # a full training checkpoint — unwrap it to the same shape.
    if "controller" in state and isinstance(state["controller"], dict):
        state = state["controller"]
    controller.load_state_dict(state)
    controller.eval()
    del state

    retained_params = sum(
        p.numel() for module in (vae, wm, controller) if module is not None
        for p in module.parameters()
    )
    print(f"Retained deployment parameters: {retained_params:,}")

    # Optimize inference
    use_compile = False
    use_cuda_graph = False
    if device.type == "cuda":
        torch.backends.cudnn.benchmark = True
        try:
            vae.encode = torch.compile(vae.encode)
            print("torch.compile enabled for FSQ")
            # Pre-warm so the first real frame doesn't pay the JIT cost
            # (which otherwise dominates the bench max).
            with torch.no_grad():
                dummy_frame = torch.zeros(1, 1, 64, 64, device=device)
                for _ in range(3):
                    vae.encode(dummy_frame)
            torch.cuda.synchronize()
            use_compile = True
        except Exception as e:
            print(f"torch.compile not available: {e}")

        # CUDA Graph for encode_context: records all kernels once,
        # replays with a single CPU call (zero per-op launch overhead).
        if wm is not None:
            try:
                graph_ctx_t = torch.zeros(
                    1, K, 65, dtype=torch.long, device=device)
                graph_ctx_a = torch.zeros(
                    1, K, dtype=torch.long, device=device)

                # Warmup runs (CUDA needs to see the kernels before capture)
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
            except Exception as e:
                print(f"CUDA Graph capture failed, using eager: {e}")

    print("All models loaded.\n")

    # --- Screen capture setup ---
    crop_x, crop_y, crop_size = 660, 48, 1032
    # Capture only the model crop. This preserves the exact RGB pixels while
    # avoiding a full-screen host copy followed by a NumPy slice.
    region = (crop_x, crop_y, crop_x + crop_size, crop_y + crop_size)
    cam = dxcam.create()
    frame_interval = 1.0 / args.fps

    exact_preprocessor = None
    if args.preprocessor == "exact-cuda":
        if device.type != "cuda":
            raise RuntimeError("--preprocessor exact-cuda requires CUDA")
        from benchmark_exact_cuda_resize import ExactCudaCandidate
        exact_preprocessor = ExactCudaCandidate(crop_size, 64, device)
        # Compile Triton and warm all fixed-shape kernels before measurement.
        dummy_gray = np.zeros((crop_size, crop_size), dtype=np.uint8)
        for _ in range(3):
            exact_preprocessor.preprocess_gray_float(dummy_gray)
        torch.cuda.synchronize()
        print("Exact CUDA preprocessing enabled")

    # --- State ---
    active = False
    jumping = False
    # Ring buffer on GPU: avoids GPU->CPU->GPU round-trip each frame
    ctx_tokens = torch.zeros(K, 64, dtype=torch.long, device=device)
    ctx_actions = torch.zeros(K, dtype=torch.long, device=device)
    ctx_fill = 0  # how many frames stored so far
    ctx_status = None if wm is None else torch.full(
        (K, 1), wm.ALIVE_TOKEN, dtype=torch.long, device=device)
    frame_count = 0


    # Pinned memory buffer for CPU->GPU frame transfer (skips implicit staging copy)
    if device.type == "cuda" and exact_preprocessor is None:
        pin_buf = torch.zeros(1, 1, 64, 64, dtype=torch.float32, pin_memory=True)
    else:
        pin_buf = None

    if exact_preprocessor is None:
        gpu_stages = ["fsq"]
        if uses_temporal_state:
            gpu_stages.append("transformer")
        gpu_stages.append("controller")
        cpu_stages = [
            "capture", "crop", "grayscale", "sobel", "downscale",
            "end_to_end",
        ]
        aggregate_stages = [
            "capture", "crop", "grayscale", "sobel", "downscale", "fsq",
        ]
    else:
        gpu_stages = ["preprocess", "fsq"]
        if uses_temporal_state:
            gpu_stages.append("transformer")
        gpu_stages.append("controller")
        cpu_stages = ["capture", "crop", "grayscale", "end_to_end"]
        aggregate_stages = [
            "capture", "crop", "grayscale", "preprocess", "fsq",
        ]
    if uses_temporal_state:
        aggregate_stages.append("transformer")
    aggregate_stages.append("controller")

    bench = StageBench(
        gpu_stages=gpu_stages,
        cpu_stages=cpu_stages,
        device=device,
        aggregate_stages=aggregate_stages,
    )

    print("Controls:")
    print("  F5  -- toggle agent on/off")
    print("  F10 -- quit + log pipeline timing")
    if args.benchmark_samples > 0:
        print(f"  Auto-stop after {args.benchmark_samples} measured frames")
    print("\nWaiting for F5...")

    while True:
        t0 = time.perf_counter()

        # --- Hotkeys ---
        if keyboard.is_pressed("f5"):
            active = not active
            if active:
                ctx_tokens.zero_()
                ctx_actions.zero_()
                ctx_fill = 0
                frame_count = 0

                if jumping:
                    keyboard.release("space")
                    jumping = False
                print(">> Agent ON")
            else:
                if jumping:
                    keyboard.release("space")
                    jumping = False
                print(">> Agent OFF")
            while keyboard.is_pressed("f5"):
                time.sleep(0.01)

        if keyboard.is_pressed("f10"):
            break

        # --- Capture ---
        bench.cpu_start("end_to_end")
        bench.cpu_start("capture")
        img = cam.grab(region=region)
        bench.cpu_end("capture")
        if img is None:
            bench.discard()
            time.sleep(0.001)
            continue

        # --- Preprocess (per-stage; sobel block has implicit sync via .cpu()) ---
        bench.cpu_start("crop")
        cropped = img
        bench.cpu_end("crop")

        bench.cpu_start("grayscale")
        gray = cv2.cvtColor(cropped, cv2.COLOR_RGB2GRAY)
        bench.cpu_end("grayscale")

        if exact_preprocessor is not None:
            bench.gpu_start("preprocess")
            frame_t = exact_preprocessor.preprocess_gray_float(gray)
            bench.gpu_end("preprocess")
        else:
            bench.cpu_start("sobel")
            if device.type == "cuda":
                _init_sobel_kernels(device)
                gray_t = torch.from_numpy(gray).float().unsqueeze(0).unsqueeze(0).to(device)
                padded = torch.nn.functional.pad(gray_t, (1, 1, 1, 1), mode='reflect')
                sx = torch.nn.functional.conv2d(padded, _SOBEL_X)
                sy = torch.nn.functional.conv2d(padded, _SOBEL_Y)
                mag = torch.sqrt(sx ** 2 + sy ** 2)
                edges = torch.clamp(torch.round(mag), 0, 255).to(torch.uint8).squeeze().cpu().numpy()
            else:
                sobel_x = cv2.Sobel(gray, cv2.CV_16S, 1, 0, ksize=3)
                sobel_y = cv2.Sobel(gray, cv2.CV_16S, 0, 1, ksize=3)
                edges = cv2.convertScaleAbs(cv2.magnitude(
                    sobel_x.astype(np.float32), sobel_y.astype(np.float32)))
            bench.cpu_end("sobel")

            bench.cpu_start("downscale")
            edge_frame = cv2.resize(edges, (64, 64), interpolation=cv2.INTER_AREA)
            bench.cpu_end("downscale")

        if not active:
            bench.discard()
            elapsed = time.perf_counter() - t0
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)
            continue

        # --- FSQ encode (stays on GPU) ---
        bench.gpu_start("fsq")
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
            tokens = vae.encode(frame_t).reshape(64)  # (64,) on GPU
        bench.gpu_end("fsq")

        # --- Update context ring buffer (all on GPU) ---
        if ctx_fill < K:
            ctx_tokens[ctx_fill] = tokens
            ctx_actions[ctx_fill] = 1 if jumping else 0
            ctx_fill += 1
        else:
            ctx_tokens[:-1] = ctx_tokens[1:].clone()
            ctx_actions[:-1] = ctx_actions[1:].clone()
            ctx_tokens[-1] = tokens
            ctx_actions[-1] = 1 if jumping else 0
        frame_count += 1

        # --- Warmup: need K frames before acting ---
        if ctx_fill < K:
            print(f"  Warmup: {ctx_fill}/{K} frames")
            bench.discard()
            elapsed = time.perf_counter() - t0
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)
            continue

        # --- Transformer: get h_t (all on GPU, no CPU round-trip) ---
        h_t = None
        if uses_temporal_state:
            bench.gpu_start("transformer")
            if use_cuda_graph:
                ctx_t = torch.cat([ctx_tokens, ctx_status], dim=1).unsqueeze(0)
                graph_ctx_t.copy_(ctx_t)
                graph_ctx_a.copy_(ctx_actions.unsqueeze(0))
                encode_graph.replay()
                h_t = graph_h_t
            else:
                with torch.no_grad():
                    ctx_t = torch.cat([ctx_tokens, ctx_status], dim=1).unsqueeze(0)
                    ctx_a = ctx_actions.unsqueeze(0)
                    h_t = wm.encode_context(ctx_t, ctx_a)
            bench.gpu_end("transformer")

        # --- Controller: decide action ---
        bench.gpu_start("controller")
        with torch.no_grad():
            # ctx_tokens is (K, 64); controller wants (B=1, 64) = current frame
            z_t = ctx_tokens[-1:].clone()
            if policy_class == "v3_cnn_grid":
                prob, _ = controller(z_t)
            else:
                prob, _ = controller(z_t, h_t.float())
        bench.gpu_end("controller")
        # prob[0].item() forces the only sync of the frame; CUDA events are
        # now safe to query.
        p = prob[0].item()
        jump = p > args.jump_threshold

        # --- Execute action ---
        if jump and not jumping:
            keyboard.press("space")
            jumping = True
        elif not jump and jumping:
            keyboard.release("space")
            jumping = False
        bench.cpu_end("end_to_end")
        bench.commit()

        if (args.benchmark_samples > 0
                and bench.count("end_to_end") >= args.benchmark_samples):
            print(f">> Collected {args.benchmark_samples} benchmark frames")
            break

        # Track probability distribution
        if not hasattr(main, '_probs'):
            main._probs = []
        main._probs.append(p)

        # --- Frame rate + per-stage timing ---
        elapsed = time.perf_counter() - t0
        if elapsed < frame_interval:
            time.sleep(frame_interval - elapsed)
        total_frame_time = time.perf_counter() - t0
        if frame_count % 30 == 0:
            real_fps = 1.0 / total_frame_time if total_frame_time > 0 else 0
            probs = main._probs
            lo = sum(1 for x in probs if x < 0.3)
            mid = sum(1 for x in probs if 0.3 <= x <= 0.7)
            hi = sum(1 for x in probs if x > 0.7)
            n = len(probs)
            print(f"  frame {frame_count}: {real_fps:.0f}fps p={p:.2f} | "
                  f"prob dist: <0.3={lo*100//n}% 0.3-0.7={mid*100//n}% >0.7={hi*100//n}%")
            main._probs = []

    # Cleanup
    if jumping:
        keyboard.release("space")
    print(bench.summary(), flush=True)
    if args.benchmark_output and bench.count("end_to_end") > 0:
        output_path = Path(args.benchmark_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        frame_budget_ms = 1000.0 / args.fps
        frames_over_budget = bench.count_above("end_to_end", frame_budget_ms)
        payload = {
            "schema_version": 1,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "measurement": {
                "path": "scripts/deploy.py optimized live deployment",
                "end_to_end_definition": (
                    "wall-clock time from immediately before screen capture "
                    "through the keyboard action update when required, "
                    "excluding frame-rate sleep"
                ),
                "warmup": f"first {K - 1} context-fill frames excluded",
                "requested_samples": args.benchmark_samples,
                "recorded_samples": bench.count("end_to_end"),
                "requested_cadence_fps": args.fps,
                "frame_budget_ms": round(frame_budget_ms, 3),
                "frames_over_budget": frames_over_budget,
                "frames_over_budget_percent": round(
                    100.0 * frames_over_budget / bench.count("end_to_end"), 3
                ),
            },
            "hardware": {
                "device": str(device),
                "gpu": (torch.cuda.get_device_name(0)
                        if device.type == "cuda" else None),
                "torch_version": torch.__version__,
                "cuda_version": torch.version.cuda,
            },
            "optimizations": {
                "preprocessor": args.preprocessor,
                "direct_model_crop_capture": True,
                "preprocessed_observation_stays_on_gpu": (
                    exact_preprocessor is not None
                ),
                "fsq_torch_compile": use_compile,
                "transformer_cuda_graph": use_cuda_graph,
                "gpu_resident_context": True,
                "pinned_frame_buffer": pin_buf is not None,
                "per_stage_cuda_synchronization": False,
                "action_probability_sync": "prob[0].item()",
            },
            "model": {
                "config": args.config,
                "vae_checkpoint": args.vae_checkpoint,
                "transformer_checkpoint": args.transformer_checkpoint
                if wm is not None else None,
                "controller_checkpoint": args.controller_checkpoint,
                "policy_class": policy_class,
                "context_frames": K,
            },
            "timing": bench.results(),
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        print(f"Saved benchmark to {output_path}")
    del cam
    print("\nDone.")


if __name__ == "__main__":
    main()

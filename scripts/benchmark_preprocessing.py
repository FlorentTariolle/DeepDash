"""Check preprocessing bit identity and benchmark CPU versus CUDA Sobel.

The CPU path is the recording-time reference used to create the saved V7
dataset.  The CUDA path mirrors the current optimized deployment path:
OpenCV grayscale, PyTorch Sobel/magnitude on CUDA, then OpenCV INTER_AREA
downsampling on CPU.

Examples:
    python scripts/benchmark_preprocessing.py --source synthetic --samples 64
    python scripts/benchmark_preprocessing.py --source live --samples 300
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass

import cv2
import numpy as np
import torch
import torch.nn.functional as F


SOBEL_X = torch.tensor(
    [[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], dtype=torch.float32
).reshape(1, 1, 3, 3)
SOBEL_Y = torch.tensor(
    [[-1, -2, -1], [0, 0, 0], [1, 2, 1]], dtype=torch.float32
).reshape(1, 1, 3, 3)


@dataclass
class ComparisonStats:
    exact_frames: int = 0
    mismatch_pixels: int = 0
    total_pixels: int = 0
    max_abs_error: int = 0
    abs_error_sum: int = 0

    def update(self, reference: np.ndarray, candidate: np.ndarray) -> None:
        if reference.shape != candidate.shape:
            raise ValueError(
                f"Shape mismatch: reference={reference.shape}, "
                f"candidate={candidate.shape}"
            )
        delta = np.abs(reference.astype(np.int16) - candidate.astype(np.int16))
        mismatches = int(np.count_nonzero(delta))
        self.exact_frames += int(mismatches == 0)
        self.mismatch_pixels += mismatches
        self.total_pixels += int(delta.size)
        self.max_abs_error = max(self.max_abs_error, int(delta.max(initial=0)))
        self.abs_error_sum += int(delta.sum(dtype=np.int64))


def cpu_reference(rgb_crop: np.ndarray, target_size: int) -> np.ndarray:
    """The exact OpenCV pipeline used by ``record_gameplay.py``."""
    gray = cv2.cvtColor(rgb_crop, cv2.COLOR_RGB2GRAY)
    sobel_x = cv2.Sobel(gray, cv2.CV_16S, 1, 0, ksize=3)
    sobel_y = cv2.Sobel(gray, cv2.CV_16S, 0, 1, ksize=3)
    edges = cv2.convertScaleAbs(
        cv2.magnitude(sobel_x.astype(np.float32), sobel_y.astype(np.float32))
    )
    return cv2.resize(
        edges, (target_size, target_size), interpolation=cv2.INTER_AREA
    )


class CudaCandidate:
    """Current deploy-equivalent CUDA Sobel followed by CPU area resize."""

    def __init__(self, device: torch.device):
        self.device = device
        self.sobel_x = SOBEL_X.to(device)
        self.sobel_y = SOBEL_Y.to(device)

    @torch.inference_mode()
    def __call__(self, rgb_crop: np.ndarray, target_size: int) -> np.ndarray:
        gray = cv2.cvtColor(rgb_crop, cv2.COLOR_RGB2GRAY)
        gray_t = (
            torch.from_numpy(gray)
            .to(device=self.device, dtype=torch.float32)
            .unsqueeze(0)
            .unsqueeze(0)
        )
        padded = F.pad(gray_t, (1, 1, 1, 1), mode="reflect")
        sx = F.conv2d(padded, self.sobel_x)
        sy = F.conv2d(padded, self.sobel_y)
        magnitude = torch.sqrt(sx.square() + sy.square())
        edges = (
            torch.clamp(torch.round(magnitude), 0, 255)
            .to(torch.uint8)
            .squeeze()
            .cpu()
            .numpy()
        )
        return cv2.resize(
            edges, (target_size, target_size), interpolation=cv2.INTER_AREA
        )


def synthetic_crops(
    samples: int, crop_size: int, seed: int
) -> list[np.ndarray]:
    """Mix random RGB crops with deterministic high-contrast stress cases."""
    rng = np.random.default_rng(seed)
    frames: list[np.ndarray] = []

    zeros = np.zeros((crop_size, crop_size, 3), dtype=np.uint8)
    frames.append(zeros)
    frames.append(np.full_like(zeros, 255))

    ramp = np.linspace(0, 255, crop_size, dtype=np.uint8)
    ramp_x = np.broadcast_to(ramp[None, :, None], zeros.shape).copy()
    frames.append(ramp_x)
    frames.append(np.swapaxes(ramp_x, 0, 1).copy())

    yy, xx = np.indices((crop_size, crop_size))
    checker = (((xx + yy) & 1) * 255).astype(np.uint8)
    frames.append(np.repeat(checker[:, :, None], 3, axis=2))

    while len(frames) < samples:
        frames.append(
            rng.integers(
                0, 256, size=(crop_size, crop_size, 3), dtype=np.uint8
            )
        )
    return frames[:samples]


def live_crops(
    samples: int,
    region: tuple[int, int, int, int],
    capture_fps: int,
    timeout_seconds: float,
) -> list[np.ndarray]:
    """Capture fresh RGB crops with DXcam; the game may be open or closed."""
    try:
        import dxcam
    except ImportError as exc:
        raise RuntimeError("Live capture requires the dxcam package") from exc

    camera = dxcam.create(output_color="RGB")
    frames: list[np.ndarray] = []
    deadline = time.perf_counter() + timeout_seconds
    try:
        camera.start(region=region, target_fps=capture_fps, video_mode=True)
        while len(frames) < samples:
            if time.perf_counter() >= deadline:
                raise TimeoutError(
                    f"Captured only {len(frames)}/{samples} frames within "
                    f"{timeout_seconds:.1f} seconds"
                )
            frame = camera.get_latest_frame()
            if frame is None:
                time.sleep(0.001)
                continue
            frames.append(np.ascontiguousarray(frame))
            time.sleep(1.0 / capture_fps)
    finally:
        camera.stop()
    return frames


def summarize_latency(name: str, values_ms: list[float]) -> None:
    values = np.asarray(values_ms, dtype=np.float64)
    print(
        f"{name}: mean={values.mean():.3f} ms, "
        f"p50={np.percentile(values, 50):.3f} ms, "
        f"p95={np.percentile(values, 95):.3f} ms, "
        f"min={values.min():.3f} ms, max={values.max():.3f} ms"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("synthetic", "live"), default="synthetic")
    parser.add_argument("--samples", type=int, default=64)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--crop-size", type=int, default=1032)
    parser.add_argument("--target-size", type=int, default=64)
    parser.add_argument("--crop-x", type=int, default=660)
    parser.add_argument("--crop-y", type=int, default=48)
    parser.add_argument("--capture-fps", type=int, default=60)
    parser.add_argument("--capture-timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.samples < 1:
        raise ValueError("--samples must be positive")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required to benchmark the deployment candidate")

    device = torch.device("cuda")
    candidate = CudaCandidate(device)
    region = (
        args.crop_x,
        args.crop_y,
        args.crop_x + args.crop_size,
        args.crop_y + args.crop_size,
    )
    if args.source == "live":
        frames = live_crops(
            args.samples, region, args.capture_fps, args.capture_timeout
        )
    else:
        frames = synthetic_crops(args.samples, args.crop_size, args.seed)

    warmup_frames = frames[: min(args.warmup, len(frames))]
    for frame in warmup_frames:
        cpu_reference(frame, args.target_size)
        candidate(frame, args.target_size)
    torch.cuda.synchronize(device)

    comparison = ComparisonStats()
    cpu_ms: list[float] = []
    cuda_ms: list[float] = []

    for frame in frames:
        start = time.perf_counter()
        reference = cpu_reference(frame, args.target_size)
        cpu_ms.append((time.perf_counter() - start) * 1000.0)

        start = time.perf_counter()
        output = candidate(frame, args.target_size)
        cuda_ms.append((time.perf_counter() - start) * 1000.0)
        comparison.update(reference, output)

    print(
        f"source={args.source}, samples={len(frames)}, "
        f"crop={args.crop_size}x{args.crop_size}, output={args.target_size}x{args.target_size}"
    )
    print(
        f"bit identity: {comparison.exact_frames}/{len(frames)} exact frames; "
        f"{comparison.mismatch_pixels}/{comparison.total_pixels} mismatched pixels; "
        f"max_abs_error={comparison.max_abs_error}; "
        f"mean_abs_error={comparison.abs_error_sum / comparison.total_pixels:.6f}"
    )
    summarize_latency("CPU OpenCV reference", cpu_ms)
    summarize_latency("Current CUDA candidate", cuda_ms)

    if comparison.mismatch_pixels:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

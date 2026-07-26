"""Prototype an exact CUDA replacement for OpenCV INTER_AREA downsampling.

This is an experimental benchmark, not part of the deployment path.  It keeps
the CPU OpenCV grayscale conversion, computes Sobel magnitude on CUDA, then
uses a Triton kernel whose accumulation order mirrors OpenCV's separable area
resize before returning only the final 64x64 uint8 observation to the host.
"""

from __future__ import annotations

import argparse
import time

import cv2
import numpy as np
import torch
import torch.nn.functional as F
import triton
import triton.language as tl

from benchmark_preprocessing import (
    ComparisonStats,
    SOBEL_X,
    SOBEL_Y,
    cpu_reference,
    live_crops,
    summarize_latency,
    synthetic_crops,
)


@triton.jit
def _multiply_rn(left, right):
    return tl.inline_asm_elementwise(
        asm="mul.rn.f32 $0, $1, $2;",
        constraints="=f,f,f",
        args=[left, right],
        dtype=tl.float32,
        is_pure=True,
        pack=1,
    )


@triton.jit
def _add_rn(left, right):
    return tl.inline_asm_elementwise(
        asm="add.rn.f32 $0, $1, $2;",
        constraints="=f,f,f",
        args=[left, right],
        dtype=tl.float32,
        is_pure=True,
        pack=1,
    )


@triton.jit
def _ordered_area_resize_kernel(
    edges,
    indices,
    weights,
    output,
    n_elements: tl.constexpr,
    source_size: tl.constexpr,
    target_size: tl.constexpr,
    taps: tl.constexpr,
    block_size: tl.constexpr,
):
    offsets = tl.program_id(0) * block_size + tl.arange(0, block_size)
    mask = offsets < n_elements
    target_y = offsets // target_size
    target_x = offsets - target_y * target_size

    accumulator = tl.zeros((block_size,), tl.float32)
    for tap_y in range(taps):
        source_y = tl.load(
            indices + target_y * taps + tap_y, mask=mask, other=0
        )
        weight_y = tl.load(
            weights + target_y * taps + tap_y, mask=mask, other=0.0
        )

        horizontal = tl.zeros((block_size,), tl.float32)
        for tap_x in range(taps):
            source_x = tl.load(
                indices + target_x * taps + tap_x, mask=mask, other=0
            )
            weight_x = tl.load(
                weights + target_x * taps + tap_x, mask=mask, other=0.0
            )
            pixel = tl.load(
                edges + source_y * source_size + source_x,
                mask=mask,
                other=0.0,
            ).to(tl.float32)
            horizontal = _add_rn(horizontal, _multiply_rn(pixel, weight_x))
        accumulator = _add_rn(
            accumulator, _multiply_rn(horizontal, weight_y)
        )

    tl.store(output + offsets, accumulator, mask=mask)


def area_tables(source_size: int, target_size: int) -> tuple[np.ndarray, np.ndarray]:
    """Build OpenCV-style separable area indices and float32 coefficients."""
    scale = source_size / target_size
    index_rows: list[list[int]] = []
    weight_rows: list[list[np.float32]] = []
    max_taps = 0

    for target in range(target_size):
        lower = target * scale
        upper = (target + 1) * scale
        row_indices: list[int] = []
        row_weights: list[np.float32] = []
        for source in range(int(np.floor(lower)), int(np.ceil(upper))):
            overlap = max(
                0.0,
                min(upper, source + 1.0) - max(lower, float(source)),
            )
            if overlap:
                row_indices.append(source)
                row_weights.append(np.float32(overlap / scale))
        index_rows.append(row_indices)
        weight_rows.append(row_weights)
        max_taps = max(max_taps, len(row_indices))

    indices = np.zeros((target_size, max_taps), dtype=np.int32)
    weights = np.zeros((target_size, max_taps), dtype=np.float32)
    for target, (row_indices, row_weights) in enumerate(
        zip(index_rows, weight_rows)
    ):
        indices[target, : len(row_indices)] = row_indices
        weights[target, : len(row_weights)] = row_weights
    return indices, weights


class ExactCudaCandidate:
    def __init__(self, source_size: int, target_size: int, device: torch.device):
        self.source_size = source_size
        self.target_size = target_size
        self.device = device
        self.sobel = torch.cat((SOBEL_X, SOBEL_Y), dim=0).to(device)
        indices, weights = area_tables(source_size, target_size)
        self.indices = torch.from_numpy(indices).to(device)
        self.weights = torch.from_numpy(weights).to(device)
        self.taps = indices.shape[1]
        self.area_output = torch.empty(
            (target_size, target_size), dtype=torch.float32, device=device
        )

    @torch.inference_mode()
    def preprocess_gray_uint8(self, gray: np.ndarray) -> torch.Tensor:
        """Return the exact 64x64 uint8 observation on the GPU."""
        gray_t = (
            torch.from_numpy(gray)
            .to(device=self.device, dtype=torch.float32)
            .unsqueeze(0)
            .unsqueeze(0)
        )
        gradients = F.conv2d(
            F.pad(gray_t, (1, 1, 1, 1), mode="reflect"), self.sobel
        )
        edges = torch.clamp(
            torch.round(
                torch.sqrt(gradients[:, 0].square() + gradients[:, 1].square())
            ),
            0,
            255,
        ).squeeze(0)

        n_elements = self.target_size * self.target_size
        block_size = 256
        _ordered_area_resize_kernel[(triton.cdiv(n_elements, block_size),)](
            edges,
            self.indices,
            self.weights,
            self.area_output,
            n_elements=n_elements,
            source_size=self.source_size,
            target_size=self.target_size,
            taps=self.taps,
            block_size=block_size,
        )
        return torch.clamp(torch.round(self.area_output), 0, 255).to(
            torch.uint8
        )

    @torch.inference_mode()
    def preprocess_gray_float(self, gray: np.ndarray) -> torch.Tensor:
        """Return normalized encoder input with shape (1, 1, H, W) on GPU."""
        output = self.preprocess_gray_uint8(gray)
        return output.to(torch.float32).mul_(1.0 / 255.0).unsqueeze(0).unsqueeze(0)

    @torch.inference_mode()
    def __call__(self, rgb_crop: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(rgb_crop, cv2.COLOR_RGB2GRAY)
        return self.preprocess_gray_uint8(gray).cpu().numpy()


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
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required")

    region = (
        args.crop_x,
        args.crop_y,
        args.crop_x + args.crop_size,
        args.crop_y + args.crop_size,
    )
    if args.source == "live":
        frames = live_crops(
            args.samples,
            region,
            args.capture_fps,
            args.capture_timeout,
        )
    else:
        frames = synthetic_crops(args.samples, args.crop_size, args.seed)

    candidate = ExactCudaCandidate(
        args.crop_size, args.target_size, torch.device("cuda")
    )
    for frame in frames[: min(args.warmup, len(frames))]:
        candidate(frame)
    torch.cuda.synchronize()

    comparison = ComparisonStats()
    latency_ms: list[float] = []
    for frame in frames:
        reference = cpu_reference(frame, args.target_size)
        start = time.perf_counter()
        output = candidate(frame)
        latency_ms.append((time.perf_counter() - start) * 1000.0)
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
    summarize_latency("Exact CUDA resize candidate", latency_ms)
    if comparison.mismatch_pixels:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

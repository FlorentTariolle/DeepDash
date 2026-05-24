"""Shared Atari actor-critic target utilities."""

from __future__ import annotations

import math
from dataclasses import dataclass

import torch
import torch.nn.functional as F


def symlog(x: torch.Tensor) -> torch.Tensor:
    """Dreamer-style signed logarithm."""
    return torch.sign(x) * torch.log1p(x.abs())


def symexp(x: torch.Tensor) -> torch.Tensor:
    """Inverse of :func:`symlog`."""
    return torch.sign(x) * torch.expm1(x.abs())


def symlog_support(num_bins: int = 255, low: float = -25.0,
                   high: float = 25.0, device=None, dtype=None) -> torch.Tensor:
    return torch.linspace(
        float(symlog(torch.tensor(low)).item()),
        float(symlog(torch.tensor(high)).item()),
        int(num_bins),
        device=device,
        dtype=dtype or torch.float32,
    )


def twohot_symlog_targets(values: torch.Tensor, num_bins: int = 255,
                          low: float = -25.0, high: float = 25.0) -> torch.Tensor:
    """Encode raw scalar targets as two-hot masses on a symlog support."""
    values = values.float()
    bins = symlog_support(num_bins, low, high, values.device, values.dtype)
    y = symlog(values.clamp(float(low), float(high))).clamp(bins[0], bins[-1])
    pos = torch.searchsorted(bins, y).clamp(1, int(num_bins) - 1)
    lo = pos - 1
    hi = pos
    lo_v = bins[lo]
    hi_v = bins[hi]
    hi_w = ((y - lo_v) / (hi_v - lo_v).clamp_min(1e-8)).clamp(0.0, 1.0)
    lo_w = 1.0 - hi_w
    out = torch.zeros(*values.shape, int(num_bins), device=values.device, dtype=values.dtype)
    out.scatter_add_(-1, lo.unsqueeze(-1), lo_w.unsqueeze(-1))
    out.scatter_add_(-1, hi.unsqueeze(-1), hi_w.unsqueeze(-1))
    return out


def twohot_cross_entropy(logits: torch.Tensor, targets: torch.Tensor) -> torch.Tensor:
    return -(targets * F.log_softmax(logits.float(), dim=-1)).sum(dim=-1).mean()


def decode_twohot_symlog(logits: torch.Tensor, num_bins: int = 255,
                         low: float = -25.0, high: float = 25.0) -> torch.Tensor:
    bins = symlog_support(num_bins, low, high, logits.device, logits.float().dtype)
    expected_symlog = (F.softmax(logits.float(), dim=-1) * bins).sum(dim=-1)
    return symexp(expected_symlog).clamp(float(low), float(high))


@dataclass
class PercentileNormalizer:
    """EMA scale normalizer inspired by the DeepDash PPO controller."""

    low_percentile: float = 5.0
    high_percentile: float = 95.0
    momentum: float = 0.99
    eps: float = 1e-6
    low: float | None = None
    high: float | None = None

    def update(self, values: torch.Tensor) -> None:
        flat = values.detach().float().reshape(-1)
        if flat.numel() == 0:
            return
        q = torch.quantile(
            flat,
            torch.tensor(
                [self.low_percentile / 100.0, self.high_percentile / 100.0],
                device=flat.device,
            ),
        )
        low, high = float(q[0].item()), float(q[1].item())
        if not (math.isfinite(low) and math.isfinite(high)):
            return
        if self.low is None or self.high is None:
            self.low, self.high = low, high
        else:
            self.low = self.momentum * self.low + (1.0 - self.momentum) * low
            self.high = self.momentum * self.high + (1.0 - self.momentum) * high

    @property
    def scale(self) -> float:
        if self.low is None or self.high is None:
            return 1.0
        return max(float(self.high - self.low), self.eps)

    def normalize(self, values: torch.Tensor) -> torch.Tensor:
        return values / self.scale

    def state_dict(self) -> dict:
        return {
            "low_percentile": self.low_percentile,
            "high_percentile": self.high_percentile,
            "momentum": self.momentum,
            "eps": self.eps,
            "low": self.low,
            "high": self.high,
        }

    def load_state_dict(self, state: dict | None) -> None:
        if not state:
            return
        self.low_percentile = float(state.get("low_percentile", self.low_percentile))
        self.high_percentile = float(state.get("high_percentile", self.high_percentile))
        self.momentum = float(state.get("momentum", self.momentum))
        self.eps = float(state.get("eps", self.eps))
        self.low = state.get("low", self.low)
        self.high = state.get("high", self.high)


def update_ema_module(ema_model: torch.nn.Module, model: torch.nn.Module,
                      decay: float) -> None:
    src = model._orig_mod if hasattr(model, "_orig_mod") else model
    dst = ema_model._orig_mod if hasattr(ema_model, "_orig_mod") else ema_model
    with torch.no_grad():
        src_state = src.state_dict()
        for name, value in dst.state_dict().items():
            if name in src_state and value.is_floating_point():
                value.mul_(decay).add_(src_state[name].detach(), alpha=1.0 - decay)
            elif name in src_state:
                value.copy_(src_state[name])

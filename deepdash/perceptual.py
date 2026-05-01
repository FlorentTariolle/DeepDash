"""Differentiable perceptual losses for tokenizer training."""

from __future__ import annotations

from collections import namedtuple
import hashlib
import os
from pathlib import Path
import urllib.request

import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import models


class VGG16FeatureLoss(nn.Module):
    """Frozen VGG16 feature loss.

    This is not learned LPIPS, but follows the same differentiable perceptual
    loss idea used by IRIS: gradients flow through the frozen feature network
    back into the reconstruction path.
    """

    def __init__(self, layers=(3, 8, 15), resize_to=None):
        super().__init__()
        weights = models.VGG16_Weights.IMAGENET1K_V1
        features = models.vgg16(weights=weights).features.eval()
        max_layer = max(layers)
        self.features = nn.Sequential(*[features[i] for i in range(max_layer + 1)])
        self.layers = set(int(i) for i in layers)
        self.resize_to = int(resize_to) if resize_to else None
        self.register_buffer("mean", torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1))
        self.register_buffer("std", torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1))
        for p in self.parameters():
            p.requires_grad = False

    def _preprocess(self, x):
        if x.size(1) == 1:
            x = x.repeat(1, 3, 1, 1)
        if self.resize_to and x.shape[-2:] != (self.resize_to, self.resize_to):
            x = F.interpolate(x, size=(self.resize_to, self.resize_to),
                              mode="bilinear", align_corners=False)
        return (x - self.mean) / self.std

    def forward(self, recon, target):
        recon = self._preprocess(recon)
        target = self._preprocess(target)
        loss = recon.new_zeros(())
        x, y = recon, target
        for i, layer in enumerate(self.features):
            x = layer(x)
            y = layer(y)
            if i in self.layers:
                loss = loss + F.l1_loss(x, y)
        return loss / max(1, len(self.layers))


class ScalingLayer(nn.Module):
    """LPIPS input scaling used by the IRIS tokenizer loss."""

    def __init__(self):
        super().__init__()
        self.register_buffer("shift", torch.tensor([-.030, -.088, -.188]).view(1, 3, 1, 1))
        self.register_buffer("scale", torch.tensor([.458, .448, .450]).view(1, 3, 1, 1))

    def forward(self, x):
        return (x - self.shift) / self.scale


class NetLinLayer(nn.Module):
    def __init__(self, channels, use_dropout=True):
        super().__init__()
        layers = [nn.Dropout()] if use_dropout else []
        layers.append(nn.Conv2d(channels, 1, 1, stride=1, padding=0, bias=False))
        self.model = nn.Sequential(*layers)


class LPIPSVGG16(nn.Module):
    """VGG16 feature slices used by IRIS/LPIPS."""

    def __init__(self):
        super().__init__()
        weights = models.VGG16_Weights.IMAGENET1K_V1
        features = models.vgg16(weights=weights).features
        self.slice1 = nn.Sequential(*[features[i] for i in range(4)])
        self.slice2 = nn.Sequential(*[features[i] for i in range(4, 9)])
        self.slice3 = nn.Sequential(*[features[i] for i in range(9, 16)])
        self.slice4 = nn.Sequential(*[features[i] for i in range(16, 23)])
        self.slice5 = nn.Sequential(*[features[i] for i in range(23, 30)])
        for p in self.parameters():
            p.requires_grad = False

    def forward(self, x):
        h = self.slice1(x)
        h_relu1_2 = h
        h = self.slice2(h)
        h_relu2_2 = h
        h = self.slice3(h)
        h_relu3_3 = h
        h = self.slice4(h)
        h_relu4_3 = h
        h = self.slice5(h)
        h_relu5_3 = h
        outputs = namedtuple("VggOutputs", ["relu1_2", "relu2_2", "relu3_3", "relu4_3", "relu5_3"])
        return outputs(h_relu1_2, h_relu2_2, h_relu3_3, h_relu4_3, h_relu5_3)


def _normalize_tensor(x, eps=1e-10):
    norm_factor = torch.sqrt(torch.sum(x ** 2, dim=1, keepdim=True))
    return x / (norm_factor + eps)


def _spatial_average(x, keepdim=True):
    return x.mean([2, 3], keepdim=keepdim)


def _iris_lpips_checkpoint():
    url = "https://heibox.uni-heidelberg.de/f/607503859c864bc1b30b/?dl=1"
    md5_expected = "d507d7349b931f0638a25a48a722f98a"
    override = os.environ.get("SLS_IRIS_LPIPS_CKPT")
    if override:
        path = Path(override)
        md5 = hashlib.md5(path.read_bytes()).hexdigest()
        if md5 != md5_expected:
            raise RuntimeError(
                f"SLS_IRIS_LPIPS_CKPT has md5 {md5}, expected {md5_expected}: {path}"
            )
        return path

    cache_dir = Path.home() / ".cache" / "iris" / "tokenizer_pretrained_vgg"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        cache_dir = Path(__file__).resolve().parents[1] / ".codex_tmp" / "iris" / "tokenizer_pretrained_vgg"
        cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "vgg.pth"
    if path.exists():
        md5 = hashlib.md5(path.read_bytes()).hexdigest()
        if md5 == md5_expected:
            return path
        path.unlink()

    print(f"Downloading IRIS LPIPS weights to {path}")
    urllib.request.urlretrieve(url, path)
    md5 = hashlib.md5(path.read_bytes()).hexdigest()
    if md5 != md5_expected:
        path.unlink(missing_ok=True)
        raise RuntimeError(
            f"downloaded IRIS LPIPS checkpoint has md5 {md5}, expected {md5_expected}"
        )
    return path


class IRISLPIPSLoss(nn.Module):
    """Pretrained VGG LPIPS loss used by IRIS, frozen during tokenizer training."""

    def __init__(self, use_dropout=True):
        super().__init__()
        self.scaling_layer = ScalingLayer()
        self.chns = [64, 128, 256, 512, 512]
        self.net = LPIPSVGG16()
        self.lin0 = NetLinLayer(self.chns[0], use_dropout=use_dropout)
        self.lin1 = NetLinLayer(self.chns[1], use_dropout=use_dropout)
        self.lin2 = NetLinLayer(self.chns[2], use_dropout=use_dropout)
        self.lin3 = NetLinLayer(self.chns[3], use_dropout=use_dropout)
        self.lin4 = NetLinLayer(self.chns[4], use_dropout=use_dropout)
        state = torch.load(_iris_lpips_checkpoint(), map_location="cpu", weights_only=True)
        self.load_state_dict(state, strict=False)
        for p in self.parameters():
            p.requires_grad = False

    def forward(self, recon, target):
        if recon.size(1) == 1:
            recon = recon.repeat(1, 3, 1, 1)
            target = target.repeat(1, 3, 1, 1)
        recon = recon * 2.0 - 1.0
        target = target * 2.0 - 1.0
        x, y = self.scaling_layer(recon), self.scaling_layer(target)
        feats_x, feats_y = self.net(x), self.net(y)
        lins = [self.lin0, self.lin1, self.lin2, self.lin3, self.lin4]
        loss = recon.new_zeros(())
        for fx, fy, lin in zip(feats_x, feats_y, lins):
            diff = (_normalize_tensor(fx) - _normalize_tensor(fy)) ** 2
            loss = loss + _spatial_average(lin.model(diff), keepdim=True).mean()
        return loss


def build_perceptual_loss(name, device):
    if name in (None, "none"):
        return None
    if name == "vgg16":
        return VGG16FeatureLoss().to(device).eval()
    if name == "lpips":
        return IRISLPIPSLoss().to(device).eval()
    raise ValueError(f"unknown perceptual_loss: {name}")

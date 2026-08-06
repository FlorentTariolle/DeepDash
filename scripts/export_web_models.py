"""Export the frozen V7 dream pipeline for ONNX Runtime Web.

The browser keeps the rollout loop in TypeScript and runs three fixed-shape
graphs:

* ``world.onnx`` predicts the next 8x8 token grid and the status logits.
* ``decoder.onnx`` turns mixed-radix FSQ codes into a 64x64 frame.
* ``controller.onnx`` exposes the optional V7 autoplay policy.

Seed contexts are encoded from the tracked episode data into a compact binary
file.  The format is intentionally tiny and dependency-free for the browser:

    magic[8], version/u32, count/u32, context_frames/u32, block_size/u32,
    tokens/u16[count, context_frames, block_size],
    actions/u8[count, context_frames]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
import torch
import torch.nn as nn
import yaml
from onnx.external_data_helper import set_external_data

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from deepdash.controller import V3CNNPolicy
from deepdash.fsq import FSQVAE
from deepdash.world_model import WorldModel


DEFAULT_CONFIG = REPO_ROOT / "configs" / "deepdash" / "v7-phase0.yaml"
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "static" / "models" / "v7"
MAX_EXTERNAL_SHARD_BYTES = 20 * 1024 * 1024

# The order is part of the seeds.bin browser contract.
SEEDS = (
    ("First Light", "expert_episodes", "perfect_run_1", 96),
    ("The Gauntlet", "expert_episodes", "perfect_run_1", 397),
    ("Inverted Flight", "expert_episodes", "perfect_run_7_5", 157),
    ("Last Chance", "death_episodes", "ep_0044", 33),
)


def _clean_state_dict(state: object) -> dict[str, torch.Tensor]:
    if isinstance(state, dict) and "state_dict" in state:
        state = state["state_dict"]
    if not isinstance(state, dict):
        raise TypeError("checkpoint does not contain a state dict")
    return {str(key).removeprefix("_orig_mod."): value for key, value in state.items()}


def _load_checkpoint(path: Path) -> dict[str, torch.Tensor]:
    return _clean_state_dict(torch.load(path, map_location="cpu", weights_only=True))


class BrowserWorldPredictor(nn.Module):
    """Export-only world pass using WebGPU-friendly explicit attention.

    PyTorch's generic SDPA exporter adds IsNaN cleanup nodes that are not part
    of ORT Web's WebGPU operator set.  The rollout shape is fixed, so spelling
    out scaled dot-product attention gives the same computation while keeping
    the graph on the GPU (apart from metadata-only reshape/shape nodes).
    """

    def __init__(self, world: WorldModel):
        super().__init__()
        if world.adaln:
            raise ValueError("the V7 browser export expects action-token conditioning")
        self.world = world
        self.scale = (world.embed_dim // world.blocks[0].n_heads) ** -0.5
        attention_bias = torch.zeros_like(world.attn_mask, dtype=torch.float32)
        attention_bias.masked_fill_(world.attn_mask, -1.0e9)
        self.register_buffer(
            "attention_bias", attention_bias.unsqueeze(0).unsqueeze(0)
        )

    @staticmethod
    def _apply_rope(
        value: torch.Tensor, cosine: torch.Tensor, sine: torch.Tensor
    ) -> torch.Tensor:
        half = value.shape[-1] // 2
        first, second = value[..., :half], value[..., half:]
        cosine = cosine.unsqueeze(0).unsqueeze(0)
        sine = sine.unsqueeze(0).unsqueeze(0)
        return torch.cat(
            (first * cosine - second * sine, first * sine + second * cosine),
            dim=-1,
        )

    def forward(
        self, frame_tokens: torch.Tensor, actions: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor]:
        # Int32 indices are accepted by torch embedding and avoid BigInt64Array
        # inputs in JavaScript. The model only consumes the K context frames.
        parts = []
        for index in range(self.world.context_frames):
            parts.append(self.world.token_embed(frame_tokens[:, index]))
            parts.append(self.world.action_embed(actions[:, index]).unsqueeze(1))
        context_end = self.world.context_frames * (self.world.block_size + 1)
        parts.append(
            self.world.mask_embed.expand(
                frame_tokens.shape[0], self.world.block_size, -1
            )
        )
        value = self.world.embed_drop(torch.cat(parts, dim=1))

        for block in self.world.blocks:
            residual = block.ln1(value)
            qkv = block.qkv(residual).reshape(
                value.shape[0],
                value.shape[1],
                3,
                block.n_heads,
                block.head_dim,
            ).permute(2, 0, 3, 1, 4)
            query, key, values = qkv[0], qkv[1], qkv[2]
            query = self._apply_rope(
                query, self.world.rope_cos, self.world.rope_sin
            )
            key = self._apply_rope(
                key, self.world.rope_cos, self.world.rope_sin
            )
            scores = torch.matmul(query, key.transpose(-2, -1)) * self.scale
            probabilities = torch.softmax(scores + self.attention_bias, dim=-1)
            attended = torch.matmul(probabilities, values)
            attended = attended.transpose(1, 2).reshape(
                value.shape[0], value.shape[1], self.world.embed_dim
            )
            value = value + block.resid_drop(block.out_proj(attended))
            value = value + block.mlp(block.ln2(value))

        value = self.world.ln_f(value)
        h_t = value[:, context_end - 1]
        logits = self.world.head(
            value[:, context_end:context_end + self.world.block_size]
        )
        return logits.float(), h_t.float()


class BrowserDecoder(nn.Module):
    """Decode browser-computed FSQ codes without integer div/mod operators."""

    def __init__(self, vae: FSQVAE):
        super().__init__()
        self.decoder = vae.decoder

    def forward(self, codes: torch.Tensor) -> torch.Tensor:
        return self.decoder(codes).float()


class BrowserController(nn.Module):
    def __init__(self, controller: V3CNNPolicy):
        super().__init__()
        self.controller = controller

    def forward(
        self, token_ids: torch.Tensor, h_t: torch.Tensor
    ) -> torch.Tensor:
        probability, _ = self.controller(token_ids, h_t)
        return probability.float()


def build_models(
    config: dict, fsq_checkpoint: Path, world_checkpoint: Path,
    controller_checkpoint: Path,
) -> tuple[FSQVAE, BrowserWorldPredictor, BrowserDecoder, BrowserController]:
    model_cfg = config["model"]
    levels = list(model_cfg["levels"])
    vae = FSQVAE(levels=levels)
    vae.load_state_dict(_load_checkpoint(fsq_checkpoint))
    vae.eval()

    world = WorldModel(
        vocab_size=int(model_cfg["vocab_size"]),
        embed_dim=int(model_cfg["embed_dim"]),
        n_heads=int(model_cfg["n_heads"]),
        n_layers=int(model_cfg["n_layers"]),
        context_frames=int(model_cfg["context_frames"]),
        dropout=float(model_cfg["dropout"]),
        tokens_per_frame=int(model_cfg["tokens_per_frame"]),
        adaln=bool(model_cfg.get("adaln", False)),
    )
    missing, unexpected = world.load_state_dict(
        _load_checkpoint(world_checkpoint), strict=False
    )
    allowed_unexpected = {
        key for key in unexpected
        if key.startswith(("cpc_", "fsq_grad_proj", "sls_gamma"))
    }
    real_unexpected = set(unexpected) - allowed_unexpected
    if missing or real_unexpected:
        raise RuntimeError(
            f"world checkpoint mismatch: missing={missing}, "
            f"unexpected={sorted(real_unexpected)}"
        )
    world.eval()

    controller_cfg = config["controller_ppo"]
    controller = V3CNNPolicy(
        vocab_size=int(model_cfg["vocab_size"]),
        grid_size=int(int(model_cfg["tokens_per_frame"]) ** 0.5),
        token_embed_dim=int(controller_cfg["token_embed_dim"]),
        h_dim=int(model_cfg["embed_dim"]),
        mtp_steps=int(controller_cfg["mtp_steps"]),
    )
    controller_state: object = torch.load(
        controller_checkpoint, map_location="cpu", weights_only=True
    )
    if isinstance(controller_state, dict) and "controller" in controller_state:
        controller_state = (
            controller_state.get("ema_controller") or controller_state["controller"]
        )
    controller.load_state_dict(_clean_state_dict(controller_state))
    controller.eval()

    return (
        vae,
        BrowserWorldPredictor(world).eval(),
        BrowserDecoder(vae).eval(),
        BrowserController(controller).eval(),
    )


def export_graphs(
    output_dir: Path,
    world: BrowserWorldPredictor,
    decoder: BrowserDecoder,
    controller: BrowserController,
    context_frames: int,
    block_size: int,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale_shard in output_dir.glob("world.weights.*.bin"):
        stale_shard.unlink()
    dummy_tokens = torch.zeros(
        (1, context_frames, block_size), dtype=torch.int32
    )
    dummy_tokens[:, :, -1] = 1000  # ALIVE status token
    dummy_actions = torch.zeros((1, context_frames), dtype=torch.int32)
    dummy_codes = torch.zeros((1, 4, 8, 8), dtype=torch.float32)
    dummy_grid = torch.zeros((1, 64), dtype=torch.int32)
    dummy_hidden = torch.zeros((1, 384), dtype=torch.float32)

    exports = (
        (
            world,
            (dummy_tokens, dummy_actions),
            output_dir / "world.onnx",
            ["frame_tokens", "actions"],
            ["logits", "h_t"],
        ),
        (
            decoder,
            (dummy_codes,),
            output_dir / "decoder.onnx",
            ["codes"],
            ["frame"],
        ),
        (
            controller,
            (dummy_grid, dummy_hidden),
            output_dir / "controller.onnx",
            ["token_ids", "h_t"],
            ["action_prob"],
        ),
    )

    for module, inputs, path, input_names, output_names in exports:
        print(f"Exporting {path.name} ...", flush=True)
        with torch.inference_mode():
            torch.onnx.export(
                module,
                inputs,
                path,
                input_names=input_names,
                output_names=output_names,
                opset_version=18,
                dynamo=False,
                do_constant_folding=True,
                keep_initializers_as_inputs=False,
            )
        if path.name == "world.onnx":
            shard_external_initializers(path)
        model = onnx.load(path, load_external_data=True)
        onnx.checker.check_model(model)


def shard_external_initializers(model_path: Path) -> None:
    """Move world weights into provider-friendly files below 25 MiB each.

    OpenAI Sites enforces a 25 MiB per-file deployment limit. ONNX external
    data lets the browser load the unchanged graph while keeping every static
    asset comfortably below that boundary. Initializers are never split in the
    middle; the largest V7 tensor is well below the 20 MiB shard target.
    """
    model = onnx.load(model_path, load_external_data=False)
    shard_index = 0
    shard_name = f"world.weights.{shard_index}.bin"
    shard = bytearray()

    def flush() -> None:
        nonlocal shard_index, shard_name, shard
        if not shard:
            return
        (model_path.parent / shard_name).write_bytes(shard)
        shard_index += 1
        shard_name = f"world.weights.{shard_index}.bin"
        shard = bytearray()

    for tensor in model.graph.initializer:
        if not tensor.raw_data:
            continue
        payload = bytes(tensor.raw_data)
        if len(payload) > MAX_EXTERNAL_SHARD_BYTES:
            raise RuntimeError(
                f"initializer {tensor.name!r} is too large for one external shard: "
                f"{len(payload)} bytes"
            )
        if shard and len(shard) + len(payload) > MAX_EXTERNAL_SHARD_BYTES:
            flush()
        offset = len(shard)
        shard.extend(payload)
        set_external_data(
            tensor,
            location=shard_name,
            offset=offset,
            length=len(payload),
        )
        tensor.ClearField("raw_data")

    flush()
    onnx.save_model(model, model_path)


def export_seeds(
    output_dir: Path,
    vae: FSQVAE,
    context_frames: int,
    block_size: int,
) -> list[dict[str, object]]:
    all_tokens: list[np.ndarray] = []
    all_actions: list[np.ndarray] = []
    metadata: list[dict[str, object]] = []

    for name, collection, episode, start in SEEDS:
        episode_dir = (
            REPO_ROOT / "data" / "deepdash" / collection / episode
        )
        frames = np.load(episode_dir / "frames.npy", mmap_mode="r")
        actions = np.load(episode_dir / "actions.npy", mmap_mode="r")
        stop = start + context_frames
        if stop > len(frames) or stop > len(actions):
            raise ValueError(f"seed {name!r} is shorter than its context window")

        frame_batch = np.asarray(frames[start:stop], dtype=np.float32) / 255.0
        with torch.inference_mode():
            encoded = vae.encode(torch.from_numpy(frame_batch).unsqueeze(1))
        visual_tokens = encoded.reshape(context_frames, -1).cpu().numpy()
        status = np.full((context_frames, 1), 1000, dtype=np.int64)
        tokens = np.concatenate((visual_tokens, status), axis=1)
        if tokens.shape != (context_frames, block_size):
            raise ValueError(f"unexpected seed shape for {name}: {tokens.shape}")
        all_tokens.append(tokens.astype("<u2", copy=False))
        all_actions.append(
            np.asarray(actions[start:stop], dtype=np.uint8).reshape(context_frames)
        )
        metadata.append(
            {"name": name, "episode": episode, "start_frame": int(start)}
        )

    token_array = np.stack(all_tokens)
    action_array = np.stack(all_actions)
    path = output_dir / "seeds.bin"
    with path.open("wb") as handle:
        handle.write(b"DVMCSEED")
        handle.write(
            struct.pack(
                "<IIII", 1, len(SEEDS), context_frames, block_size
            )
        )
        handle.write(token_array.tobytes(order="C"))
        handle.write(action_array.tobytes(order="C"))
    return metadata


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(
    output_dir: Path, metadata: list[dict[str, object]], config: dict
) -> None:
    files = {}
    artifact_paths = [
        output_dir / "world.onnx",
        *sorted(output_dir.glob("world.weights.*.bin")),
        output_dir / "decoder.onnx",
        output_dir / "controller.onnx",
        output_dir / "seeds.bin",
    ]
    for path in artifact_paths:
        files[path.name] = {
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        }
    model_cfg = config["model"]
    manifest = {
        "format": 1,
        "model": "DashVMC V7",
        "onnx_opset": 18,
        "onnxruntime_web": "1.26.0",
        "context_frames": int(model_cfg["context_frames"]),
        "tokens_per_frame": int(model_cfg["tokens_per_frame"]),
        "block_size": int(model_cfg["tokens_per_frame"]) + 1,
        "vocab_size": int(model_cfg["vocab_size"]),
        "fsq_levels": list(model_cfg["levels"]),
        "seeds": metadata,
        "files": files,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def validate_exports(
    output_dir: Path,
    world: BrowserWorldPredictor,
    decoder: BrowserDecoder,
    controller: BrowserController,
) -> None:
    """Run the one parity check that is critical for a browser export."""
    payload = (output_dir / "seeds.bin").read_bytes()
    if payload[:8] != b"DVMCSEED":
        raise RuntimeError("invalid seeds.bin magic")
    version, count, context_frames, block_size = struct.unpack_from(
        "<IIII", payload, 8
    )
    if version != 1 or count < 1:
        raise RuntimeError("unsupported or empty seeds.bin")
    token_count = count * context_frames * block_size
    token_offset = 24
    tokens = np.frombuffer(
        payload, dtype="<u2", count=token_count, offset=token_offset
    ).reshape(count, context_frames, block_size)
    actions = np.frombuffer(
        payload,
        dtype=np.uint8,
        count=count * context_frames,
        offset=token_offset + token_count * 2,
    ).reshape(count, context_frames)

    frame_tokens = tokens[:1].astype(np.int32)
    action_input = actions[:1].astype(np.int32)
    current = frame_tokens[:, -1, :64]
    divisions = np.array([125, 25, 5, 1], dtype=np.int32)
    halves = np.array([4, 2, 2, 2], dtype=np.float32)
    codes = ((current[..., None] // divisions) % np.array(
        [8, 5, 5, 5], dtype=np.int32
    ) - halves).astype(np.float32)
    codes = codes.reshape(1, 8, 8, 4).transpose(0, 3, 1, 2)

    with torch.inference_mode():
        torch_logits, torch_hidden = world(
            torch.from_numpy(frame_tokens), torch.from_numpy(action_input)
        )
        reference_logits, reference_hidden = world.world(
            torch.from_numpy(frame_tokens),
            torch.from_numpy(action_input),
            return_hidden=True,
        )
        torch_frame = decoder(torch.from_numpy(codes))
        torch_prob = controller(
            torch.from_numpy(current.astype(np.int32)), torch_hidden
        )

    providers = ["CPUExecutionProvider"]
    world_session = ort.InferenceSession(
        str(output_dir / "world.onnx"), providers=providers
    )
    decoder_session = ort.InferenceSession(
        str(output_dir / "decoder.onnx"), providers=providers
    )
    controller_session = ort.InferenceSession(
        str(output_dir / "controller.onnx"), providers=providers
    )
    ort_logits, ort_hidden = world_session.run(
        None, {"frame_tokens": frame_tokens, "actions": action_input}
    )
    (ort_frame,) = decoder_session.run(None, {"codes": codes})
    (ort_prob,) = controller_session.run(
        None, {"token_ids": current.astype(np.int32), "h_t": ort_hidden}
    )

    comparisons = {
        "export attention logits": (
            reference_logits.cpu().numpy(), torch_logits.cpu().numpy(), 5e-4
        ),
        "export attention hidden": (
            reference_hidden.cpu().numpy(), torch_hidden.cpu().numpy(), 5e-4
        ),
        "world logits": (torch_logits.cpu().numpy(), ort_logits, 5e-4),
        "world hidden": (torch_hidden.cpu().numpy(), ort_hidden, 5e-4),
        "decoder frame": (torch_frame.cpu().numpy(), ort_frame, 1e-5),
        "controller probability": (torch_prob.cpu().numpy(), ort_prob, 1e-5),
    }
    for label, (expected, actual, tolerance) in comparisons.items():
        max_error = float(np.max(np.abs(expected - actual)))
        if not np.isfinite(max_error) or max_error > tolerance:
            raise RuntimeError(
                f"{label} parity failed: max_abs_error={max_error:.6g}"
            )
        print(f"  {label}: max abs error {max_error:.3g}")

    pytorch_tokens = torch_logits[:, :64, :1000].argmax(-1).cpu().numpy()
    onnx_tokens = ort_logits[:, :64, :1000].argmax(-1)
    if not np.array_equal(pytorch_tokens, onnx_tokens):
        raise RuntimeError("world argmax token parity failed")
    print("  greedy token parity: exact")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--fsq-checkpoint", type=Path,
        default=REPO_ROOT / "checkpoints_v7" / "fsq_best.pt",
    )
    parser.add_argument(
        "--world-checkpoint", type=Path,
        default=REPO_ROOT / "checkpoints_v7" / "transformer_best.pt",
    )
    parser.add_argument(
        "--controller-checkpoint", type=Path,
        default=REPO_ROOT / "checkpoints_v7" / "controller_ppo_best.pt",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with args.config.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    vae, world, decoder, controller = build_models(
        config,
        args.fsq_checkpoint,
        args.world_checkpoint,
        args.controller_checkpoint,
    )
    model_cfg = config["model"]
    context_frames = int(model_cfg["context_frames"])
    block_size = int(model_cfg["tokens_per_frame"]) + 1
    export_graphs(
        args.output_dir,
        world,
        decoder,
        controller,
        context_frames,
        block_size,
    )
    metadata = export_seeds(
        args.output_dir, vae, context_frames, block_size
    )
    write_manifest(args.output_dir, metadata, config)
    print("Validating PyTorch / ONNX parity ...", flush=True)
    validate_exports(args.output_dir, world, decoder, controller)
    total = sum(path.stat().st_size for path in args.output_dir.iterdir())
    print(f"Exported browser bundle to {args.output_dir} ({total / 2**20:.1f} MiB)")


if __name__ == "__main__":
    main()

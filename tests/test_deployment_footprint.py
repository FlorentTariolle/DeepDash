"""Regression tests for the live deployment parameter footprint."""

import sys
from pathlib import Path

import pytest
import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deepdash.controller import V3CNNPolicy
from deepdash.fsq import FSQVAE
from deepdash.world_model import WorldModel


def _unique_parameter_count(*modules):
    parameters = {
        id(parameter): parameter
        for module in modules
        for parameter in module.parameters()
    }
    return sum(parameter.numel() for parameter in parameters.values())


def test_selected_model_parameter_partition():
    fsq = FSQVAE(levels=[8, 5, 5, 5])
    world_model = WorldModel(
        vocab_size=1000,
        embed_dim=384,
        n_heads=8,
        n_layers=8,
        context_frames=4,
        tokens_per_frame=64,
        use_cpc=True,
        cpc_dim=64,
    )
    controller = V3CNNPolicy(
        vocab_size=1000,
        grid_size=8,
        token_embed_dim=16,
        h_dim=384,
        mtp_steps=8,
    )

    assert _unique_parameter_count(fsq.encoder) == 941_924
    assert _unique_parameter_count(fsq.decoder) == 941_921
    assert _unique_parameter_count(world_model) == 14_723_520
    assert _unique_parameter_count(controller) == 45_546
    assert world_model.head.weight is world_model.token_embed.weight
    assert world_model.head.weight.numel() == 384_768

    full_count = _unique_parameter_count(fsq, world_model, controller)
    assert full_count == 16_652_911

    fsq.prepare_for_encoder_only()
    world_model.prepare_for_context_only()

    assert _unique_parameter_count(fsq) == 941_924
    assert _unique_parameter_count(world_model) == 14_582_016
    assert _unique_parameter_count(fsq, world_model, controller) == 15_569_486


def test_pruned_models_keep_context_path_only():
    fsq = FSQVAE(levels=[8, 5, 5, 5]).prepare_for_encoder_only()
    frame = torch.rand(1, 1, 64, 64)

    assert fsq.encode(frame).shape == (1, 8, 8)
    with pytest.raises(RuntimeError, match="encoder-only deployment"):
        fsq(frame)

    world_model = WorldModel(
        vocab_size=10,
        embed_dim=48,
        n_heads=2,
        n_layers=1,
        context_frames=2,
        tokens_per_frame=4,
        use_cpc=True,
    ).prepare_for_context_only()
    frame_tokens = torch.zeros(1, 2, 5, dtype=torch.long)
    actions = torch.zeros(1, 2, dtype=torch.long)

    assert world_model.encode_context(frame_tokens, actions).shape == (1, 48)
    with pytest.raises(RuntimeError, match="context-only deployment"):
        world_model.predict_next_frame(frame_tokens, actions)

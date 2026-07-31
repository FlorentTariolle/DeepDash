"""Regression tests for the V7 spatial-only controller ablation."""

import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deepdash.controller import V3CNNGridPolicy, V3CNNPolicy
from scripts.train_controller_ppo import compute_gae, dream_rollout, ppo_update


def test_grid_only_policy_removes_exact_temporal_head_connections():
    full = V3CNNPolicy(h_dim=384)
    grid_only = V3CNNGridPolicy()

    assert sum(p.numel() for p in full.parameters()) == 45_546
    assert sum(p.numel() for p in grid_only.parameters()) == 41_706
    assert grid_only.actor.in_features == 256
    assert grid_only.critic.in_features == 256
    assert grid_only.mtp_head.in_features == 256


def test_grid_only_policy_uses_only_current_token_grid():
    policy = V3CNNGridPolicy()
    token_ids = torch.randint(0, 1000, (3, 64))

    probability, value = policy(token_ids)
    action, log_prob, entropy, sampled_value = policy.act(token_ids)

    assert probability.shape == (3,)
    assert value.shape == (3,)
    assert action.shape == (3,)
    assert log_prob.shape == (3,)
    assert entropy.shape == (3,)
    assert sampled_value.shape == (3,)
    assert policy.predict_future_action_logits(token_ids).shape == (3, 8)


class _DeterministicDreamModel:
    ALIVE_TOKEN = 1000

    def predict_next_frame(self, frame_tokens, actions, temperature=0.0,
                           return_hidden=False):
        del actions, temperature
        prediction = frame_tokens[:, -1, :-1].clone()
        death_probability = torch.zeros(frame_tokens.shape[0])
        if return_hidden:
            raise AssertionError("Grid-only PPO requested h_t")
        return prediction, death_probability


def test_grid_only_ppo_never_requests_or_caches_temporal_state():
    policy = V3CNNGridPolicy()
    model = _DeterministicDreamModel()
    context_tokens = torch.randint(0, 1000, (2, 2, 64)).numpy()
    context_actions = torch.zeros(2, 2, dtype=torch.long).numpy()

    rollout, survival = dream_rollout(
        model,
        policy,
        context_tokens,
        context_actions,
        max_steps=3,
        death_threshold=0.5,
        device=torch.device("cpu"),
        warmup_steps=1,
        uses_temporal_state=False,
    )

    assert "h_t" not in rollout
    assert survival.tolist() == [3.0, 3.0]

    advantages, returns = compute_gae(
        rollout["rewards"],
        rollout["values"],
        gamma=0.995,
        lam=0.95,
        alive_masks=rollout["alive_masks"],
        bootstrap_value=rollout["bootstrap_value"],
    )
    optimizer = torch.optim.Adam(policy.parameters(), lr=1e-4)
    loss, entropy, value = ppo_update(
        policy,
        optimizer,
        rollout,
        advantages,
        returns,
        n_epochs=1,
        minibatch_size=4,
        mtp_coeff=0.1,
        amp_dtype=None,
    )

    assert all(torch.isfinite(torch.tensor(x)) for x in (loss, entropy, value))

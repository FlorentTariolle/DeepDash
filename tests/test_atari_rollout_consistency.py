import numpy as np
import torch
import torch.nn as nn

from scripts.train_atari_predictor import (
    AtariRolloutConsistencyDataset,
    compute_rollout_consistency_loss,
    reward_window_balanced_sampler,
)


class TinyPredictor(nn.Module):
    def __init__(self, vocab_size=7, context_frames=2):
        super().__init__()
        self.context_frames = context_frames
        self.vocab_size = vocab_size
        self.tokens_per_frame = 3
        self.embed = nn.Embedding(vocab_size, 8)
        self.action = nn.Embedding(4, 8)
        self.head = nn.Linear(8, vocab_size)
        self.reward_head = nn.Linear(8, 5)
        self.event_head = nn.Linear(8, 3)
        self.done_head = nn.Linear(8, 1)

    def forward(self, frame_tokens, actions, return_aux=False,
                return_reward_logits=False, return_event_logits=False):
        h = self.embed(frame_tokens[:, -1]).mean(dim=1) + self.action(actions[:, -1])
        logits = self.head(h).unsqueeze(1).expand(-1, self.tokens_per_frame, -1)
        reward_logits = self.reward_head(h)
        reward = reward_logits.softmax(dim=-1).matmul(
            torch.linspace(-1, 1, reward_logits.size(-1), device=reward_logits.device))
        done = self.done_head(h).squeeze(-1)
        event = self.event_head(h)
        return logits, reward, done, reward_logits, event


def test_rollout_consistency_dataset_alignment():
    tokens = torch.arange(10 * 3, dtype=torch.long).view(10, 3)
    actions = np.arange(10) % 4
    rewards = np.zeros(10, dtype=np.float32)
    dones = np.zeros(10, dtype=np.float32)
    ds = AtariRolloutConsistencyDataset(
        np.asarray([2]), tokens, actions, rewards, dones,
        context_frames=2, horizon=3)

    ctx, action_window, targets, reward_window, done_window = ds[0]

    assert torch.equal(ctx, tokens[2:4])
    assert torch.equal(action_window, torch.as_tensor(actions[2:7]))
    assert torch.equal(targets, tokens[4:7])
    assert reward_window.shape == (3,)
    assert done_window.shape == (3,)


def test_reward_window_sampler_prefers_configured_event_gap():
    starts = np.asarray([0, 1, 2, 3])
    rewards = np.zeros(12, dtype=np.float32)
    rewards[5] = 1.0
    sampler = reward_window_balanced_sampler(
        starts, rewards, context_frames=2, horizon=5,
        min_gap=3, max_gap=4,
        zero_weight=1.0, neg_weight=10.0, pos_weight=20.0)

    assert sampler is not None
    assert torch.as_tensor(sampler.weights).max().item() == 20.0


def test_rollout_consistency_loss_is_finite_and_backprops():
    model = TinyPredictor()
    ctx = torch.randint(0, model.vocab_size, (2, model.context_frames, model.tokens_per_frame))
    actions = torch.randint(0, 4, (2, model.context_frames + 3))
    targets = torch.randint(0, model.vocab_size, (2, 3, model.tokens_per_frame))
    rewards = torch.tensor([[0.0, 1.0, 0.0], [0.0, -1.0, 0.0]])
    dones = torch.zeros_like(rewards)
    args = type("Args", (), {
        "reward_head_type": "twohot",
        "reward_twohot_bins": 5,
        "reward_twohot_low": -1.0,
        "reward_twohot_high": 1.0,
        "reward_event_head": True,
        "rollout_consistency_token_loss_weight": 1.0,
        "rollout_consistency_reward_loss_weight": 1.0,
        "rollout_consistency_event_loss_weight": 1.0,
        "rollout_consistency_done_loss_weight": 1.0,
    })()

    loss, parts = compute_rollout_consistency_loss(
        model, ctx, actions, targets, rewards, dones,
        model.vocab_size, None, 0.0, 0.0, args, None)

    assert torch.isfinite(loss)
    assert all(torch.isfinite(value) for value in parts.values())
    loss.backward()
    assert any(param.grad is not None for param in model.parameters())

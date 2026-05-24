"""Categorical Atari actor-critic policies."""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

from atari.rl_targets import decode_twohot_symlog


class AtariCNNPolicy(nn.Module):
    """CNN actor-critic on FSQ token grid plus frozen world-model hidden state."""

    def __init__(self, vocab_size=1000, n_actions=6, grid_size=16,
                 token_embed_dim=16, h_dim=384, temporal_dim=64,
                 value_head_type: str = "scalar", value_bins: int = 255,
                 value_low: float = -25.0, value_high: float = 25.0):
        super().__init__()
        self.grid_size = int(grid_size)
        self.n_actions = int(n_actions)
        self.value_head_type = str(value_head_type)
        self.value_bins = int(value_bins)
        self.value_low = float(value_low)
        self.value_high = float(value_high)

        self.token_embed = nn.Embedding(vocab_size, token_embed_dim)
        self.conv1 = nn.Conv2d(token_embed_dim, 32, 3, stride=2, padding=1)
        self.conv2 = nn.Conv2d(32, 64, 3, stride=2, padding=1)

        cnn_grid = max(1, self.grid_size // 4)
        cnn_out = 64 * cnn_grid * cnn_grid
        self.spatial_norm = nn.LayerNorm(cnn_out)
        self.h_norm = nn.LayerNorm(h_dim)
        self.h_proj = nn.Linear(h_dim, temporal_dim)

        head_input = cnn_out + temporal_dim
        self.actor = nn.Linear(head_input, n_actions)
        critic_out = self.value_bins if self.value_head_type == "twohot" else 1
        self.critic = nn.Linear(head_input, critic_out)
        self._init_weights()

    def _init_weights(self):
        gain = 2 ** 0.5
        for module in (self.conv1, self.conv2, self.h_proj):
            nn.init.orthogonal_(module.weight, gain=gain)
            nn.init.zeros_(module.bias)
        nn.init.orthogonal_(self.actor.weight, gain=0.01)
        nn.init.zeros_(self.actor.bias)
        nn.init.orthogonal_(self.critic.weight, gain=1.0)
        nn.init.zeros_(self.critic.bias)

    def _encode(self, token_ids, h_t):
        bsz = token_ids.shape[0]
        grid = self.grid_size
        x = self.token_embed(token_ids)
        x = x.permute(0, 2, 1).reshape(bsz, -1, grid, grid)
        x = F.silu(self.conv1(x))
        x = F.silu(self.conv2(x))
        x = self.spatial_norm(x.flatten(1))
        h = F.silu(self.h_proj(self.h_norm(h_t)))
        return torch.cat([x, h], dim=1)

    def forward(self, token_ids, h_t, return_value_logits: bool = False):
        features = self._encode(token_ids, h_t)
        logits = self.actor(features)
        value_out = self.critic(features)
        if self.value_head_type == "twohot":
            value = decode_twohot_symlog(
                value_out, self.value_bins, self.value_low, self.value_high)
            if return_value_logits:
                return logits, value, value_out
        else:
            value = value_out.squeeze(-1)
            if return_value_logits:
                return logits, value, None
        return logits, value

    def act(self, token_ids, h_t):
        logits, value = self.forward(token_ids, h_t)
        dist = torch.distributions.Categorical(logits=logits)
        action = dist.sample()
        return action, dist.log_prob(action), dist.entropy(), value

    def act_deterministic(self, token_ids, h_t):
        logits, _ = self.forward(token_ids, h_t)
        return logits.argmax(dim=-1)

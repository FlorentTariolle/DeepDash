"""Atari-specific predictor wrapper with reward and termination heads."""

from __future__ import annotations

import torch
import torch.nn as nn

from atari.rl_targets import decode_twohot_symlog


class AtariPredictorWithHeads(nn.Module):
    """World-model token predictor plus reward and done heads.

    The wrapped ``WorldModel`` still owns next-frame token prediction. The
    auxiliary heads read the last context hidden state and predict the reward
    and terminal flag for the same transition as the predicted next frame.
    """

    def __init__(self, world_model: nn.Module, hidden_dim: int,
                 reward_head_type: str = "scalar", reward_bins: int = 255,
                 reward_low: float = -25.0, reward_high: float = 25.0):
        super().__init__()
        self.world_model = world_model
        self.reward_head_type = str(reward_head_type)
        self.reward_bins = int(reward_bins)
        self.reward_low = float(reward_low)
        self.reward_high = float(reward_high)
        reward_out = self.reward_bins if self.reward_head_type == "twohot" else 1
        self.reward_head = nn.Linear(hidden_dim, reward_out)
        self.done_head = nn.Linear(hidden_dim, 1)

    @property
    def context_frames(self):
        return self.world_model.context_frames

    @property
    def tokens_per_frame(self):
        return self.world_model.tokens_per_frame

    @property
    def vocab_size(self):
        return self.world_model.vocab_size

    def _decode_reward(self, reward_out):
        if self.reward_head_type == "twohot":
            return decode_twohot_symlog(
                reward_out, self.reward_bins, self.reward_low, self.reward_high)
        return reward_out.squeeze(-1)

    def forward(self, frame_tokens, actions, return_aux: bool = False,
                return_reward_logits: bool = False):
        if not return_aux:
            return self.world_model(frame_tokens, actions)
        logits, h_t = self.world_model(frame_tokens, actions, return_hidden=True)
        reward_out = self.reward_head(h_t.float())
        reward = self._decode_reward(reward_out)
        done_logit = self.done_head(h_t.float()).squeeze(-1)
        if return_reward_logits:
            return logits, reward, done_logit, reward_out
        return logits, reward, done_logit

    @torch.no_grad()
    def encode_context(self, frame_tokens, actions):
        return self.world_model.encode_context(frame_tokens, actions)

    @torch.no_grad()
    def encode_controller_context(self, frame_tokens, actions):
        return self.world_model.encode_context(
            frame_tokens, actions, return_action_hidden=False)

    @torch.no_grad()
    def predict_next_frame(self, frame_tokens, actions, temperature=0.0,
                           top_k=0, top_p=0.0, return_hidden=False,
                           return_aux=False, return_reward_logits=False):
        pred, _, h_t = self.world_model.predict_next_frame(
            frame_tokens, actions, temperature=temperature, top_k=top_k,
            top_p=top_p, return_hidden=True)
        reward_out = self.reward_head(h_t.float())
        reward = self._decode_reward(reward_out)
        done_logit = self.done_head(h_t.float()).squeeze(-1)
        done_prob = torch.sigmoid(done_logit)
        if return_aux and return_reward_logits and return_hidden:
            return pred, reward, done_prob, h_t, reward_out
        if return_aux and return_reward_logits:
            return pred, reward, done_prob, reward_out
        if return_aux and return_hidden:
            return pred, reward, done_prob, h_t
        if return_aux:
            return pred, reward, done_prob
        if return_hidden:
            return pred, done_prob, h_t
        return pred, done_prob


def split_atari_predictor_state(state: dict) -> tuple[dict, dict]:
    """Split a checkpoint into WorldModel and auxiliary-head state dicts."""
    model_state = {}
    aux_state = {}
    for key, value in state.items():
        clean = key.removeprefix("_orig_mod.")
        if clean.startswith("world_model."):
            model_state[clean.removeprefix("world_model.")] = value
        elif clean.startswith(("reward_head.", "done_head.")):
            aux_state[clean] = value
        else:
            model_state[clean] = value
    return model_state, aux_state

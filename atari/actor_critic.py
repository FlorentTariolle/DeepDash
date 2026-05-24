"""Shared Atari PPO helpers for real and dream controller training."""

from __future__ import annotations

import torch
import torch.nn.functional as F

from atari.rl_targets import (
    PercentileNormalizer,
    twohot_cross_entropy,
    twohot_symlog_targets,
    update_ema_module,
)


def compute_lambda_returns(rewards: torch.Tensor, values: torch.Tensor,
                           dones: torch.Tensor, bootstrap_value: torch.Tensor,
                           gamma: float, lam: float) -> tuple[torch.Tensor, torch.Tensor]:
    """Compute GAE/lambda returns over ``(T,)`` or ``(T, B)`` tensors."""
    rewards = rewards.float()
    values = values.float()
    dones = dones.float()
    advantages = torch.zeros_like(rewards)
    gae = torch.zeros_like(bootstrap_value.float())
    next_value = bootstrap_value.float()
    for t in reversed(range(rewards.size(0))):
        next_nonterminal = 1.0 - dones[t]
        delta = rewards[t] + float(gamma) * next_value * next_nonterminal - values[t]
        gae = delta + float(gamma) * float(lam) * next_nonterminal * gae
        advantages[t] = gae
        next_value = values[t]
    returns = advantages + values
    return advantages.reshape(-1), returns.reshape(-1)


def _cat_or_tensor(values, device, dtype=None):
    if isinstance(values, torch.Tensor):
        out = values
    elif values and isinstance(values[0], torch.Tensor):
        out = torch.cat([v.reshape(-1, *v.shape[2:]) if v.dim() > 2 else v.reshape(-1, *v.shape[1:]) for v in values], dim=0)
    else:
        out = torch.tensor(values)
    out = out.to(device)
    return out.to(dtype=dtype) if dtype is not None else out


def _prepare_batch(batch: dict, device: torch.device) -> dict:
    return {
        "tokens": torch.cat(batch["tokens"], dim=0).to(device),
        "h": torch.cat(batch["h"], dim=0).to(device),
        "actions": _cat_or_tensor(batch["actions"], device, torch.long).reshape(-1),
        "old_logp": _cat_or_tensor(batch["logp"], device, torch.float32).reshape(-1),
        "advantages": batch["advantages"].to(device).float().reshape(-1),
        "returns": batch["returns"].to(device).float().reshape(-1),
    }


def ppo_update(policy, optimizer, batch: dict, args, device: torch.device,
               amp_dtype=None, normalizer: PercentileNormalizer | None = None,
               ema_policy=None) -> dict:
    """One PPO update shared by real-env and dream Atari actors."""
    data = _prepare_batch(batch, device)
    adv = data["advantages"]
    returns = data["returns"]
    if normalizer is not None:
        normalizer.update(returns)
        adv = normalizer.normalize(adv)
    adv = (adv - adv.mean()) / (adv.std(unbiased=False) + 1e-8)

    n = data["actions"].numel()
    idx = torch.arange(n, device=device)
    metrics = {
        "loss": 0.0,
        "actor_loss": 0.0,
        "critic_loss": 0.0,
        "entropy": 0.0,
        "approx_kl": 0.0,
        "value_mean": 0.0,
        "updates": 0,
    }
    value_head_type = getattr(args, "value_head_type", "scalar")
    for _ in range(int(args.ppo_epochs)):
        perm = idx[torch.randperm(n, device=device)]
        for start in range(0, n, int(args.minibatch_size)):
            mb = perm[start:start + int(args.minibatch_size)]
            with torch.amp.autocast(
                "cuda",
                enabled=amp_dtype is not None and device.type == "cuda",
                dtype=amp_dtype,
            ):
                if value_head_type == "twohot":
                    logits, value, value_logits = policy(
                        data["tokens"][mb], data["h"][mb], return_value_logits=True)
                else:
                    logits, value = policy(data["tokens"][mb], data["h"][mb])
                    value_logits = None
                dist = torch.distributions.Categorical(logits=logits)
                logp = dist.log_prob(data["actions"][mb])
                ratio = (logp - data["old_logp"][mb]).exp()
                pg1 = -adv[mb] * ratio
                pg2 = -adv[mb] * torch.clamp(
                    ratio, 1.0 - float(args.clip_eps), 1.0 + float(args.clip_eps))
                actor_loss = torch.max(pg1, pg2).mean()
                if value_logits is not None:
                    targets = twohot_symlog_targets(
                        returns[mb],
                        int(args.value_twohot_bins),
                        float(args.value_twohot_low),
                        float(args.value_twohot_high),
                    )
                    critic_loss = twohot_cross_entropy(value_logits, targets)
                else:
                    critic_loss = F.mse_loss(value.float(), returns[mb])
                entropy = dist.entropy().mean()
                loss = actor_loss + float(args.critic_coeff) * critic_loss - \
                    float(args.entropy_coeff) * entropy
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(policy.parameters(), float(args.max_grad_norm))
            optimizer.step()
            if ema_policy is not None and float(getattr(args, "ema_decay", 0.0)) > 0.0:
                update_ema_module(ema_policy, policy, float(args.ema_decay))
            with torch.no_grad():
                metrics["loss"] += float(loss.item())
                metrics["actor_loss"] += float(actor_loss.item())
                metrics["critic_loss"] += float(critic_loss.item())
                metrics["entropy"] += float(entropy.item())
                metrics["approx_kl"] += float((data["old_logp"][mb] - logp).mean().item())
                metrics["value_mean"] += float(value.float().mean().item())
                metrics["updates"] += 1
    updates = max(int(metrics["updates"]), 1)
    return {key: (value / updates if key != "updates" else value)
            for key, value in metrics.items()}

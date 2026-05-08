"""Train an Atari actor with PPO inside the learned world model."""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atari.controller import AtariCNNPolicy
from atari.predictor import AtariPredictorWithHeads, split_atari_predictor_state
from atari.replay_buffer import load_metadata, load_replay_arrays
from deepdash.config import apply_config, load_config
from deepdash.fsq import FSQVAE
from deepdash.world_model import WorldModel
from scripts.train_atari_actor_real import amp_dtype, load_clean_state
from scripts.train_atari_predictor import encode_replay_obs


def valid_context_starts(replay, context_frames: int):
    episode_ids = replay.episode_ids
    dones = replay.dones
    starts = []
    for i in range(0, len(episode_ids) - context_frames):
        end = i + context_frames
        if np.all(episode_ids[i:end] == episode_ids[i]) and not np.any(dones[i:end - 1]):
            starts.append(i)
    if not starts:
        raise RuntimeError("no valid dream contexts found in replay")
    return np.asarray(starts, dtype=np.int64)


def sample_contexts(starts, tokens, actions, context_frames, n, rng):
    idx = rng.choice(starts, size=n, replace=len(starts) < n)
    ctx_tokens = torch.stack([tokens[i:i + context_frames] for i in idx], dim=0)
    ctx_actions = torch.from_numpy(np.stack([
        actions[i:i + context_frames] for i in idx
    ]).astype(np.int64))
    return ctx_tokens, ctx_actions


def ppo_update(policy, optimizer, batch, args, device, amp):
    tokens = torch.cat(batch["tokens"], dim=0).to(device)
    h = torch.cat(batch["h"], dim=0).to(device)
    actions = torch.stack(batch["actions"]).reshape(-1).long().to(device)
    old_logp = torch.stack(batch["logp"]).reshape(-1).float().to(device)
    adv = batch["advantages"].to(device)
    returns = batch["returns"].to(device)
    adv = (adv - adv.mean()) / (adv.std(unbiased=False) + 1e-8)

    n = actions.numel()
    idx = torch.arange(n, device=device)
    total_loss = total_entropy = 0.0
    updates = 0
    for _ in range(args.ppo_epochs):
        perm = idx[torch.randperm(n, device=device)]
        for start in range(0, n, args.minibatch_size):
            mb = perm[start:start + args.minibatch_size]
            with torch.amp.autocast("cuda", enabled=amp is not None and device.type == "cuda", dtype=amp):
                logits, value = policy(tokens[mb], h[mb])
                dist = torch.distributions.Categorical(logits=logits)
                logp = dist.log_prob(actions[mb])
                ratio = (logp - old_logp[mb]).exp()
                pg1 = -adv[mb] * ratio
                pg2 = -adv[mb] * torch.clamp(ratio, 1.0 - args.clip_eps, 1.0 + args.clip_eps)
                actor_loss = torch.max(pg1, pg2).mean()
                critic_loss = F.mse_loss(value, returns[mb])
                entropy = dist.entropy().mean()
                loss = actor_loss + args.critic_coeff * critic_loss - args.entropy_coeff * entropy
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(policy.parameters(), args.max_grad_norm)
            optimizer.step()
            total_loss += float(loss.item())
            total_entropy += float(entropy.item())
            updates += 1
    return total_loss / max(updates, 1), total_entropy / max(updates, 1)


def compute_gae_sequence(rewards, values, dones, bootstrap, gamma, lam):
    """GAE over a (T, B) imagined rollout."""
    advantages = torch.zeros_like(rewards)
    gae = torch.zeros(rewards.size(1), device=rewards.device)
    for t in reversed(range(rewards.size(0))):
        if t == rewards.size(0) - 1:
            next_value = bootstrap
            next_nonterminal = 1.0 - dones[t].float()
        else:
            next_value = values[t + 1]
            next_nonterminal = 1.0 - dones[t].float()
        delta = rewards[t] + gamma * next_value * next_nonterminal - values[t]
        gae = delta + gamma * lam * next_nonterminal * gae
        advantages[t] = gae
    return advantages.reshape(-1), (advantages + values).reshape(-1)


@torch.no_grad()
def dream_rollout(predictor, policy, ctx_tokens, ctx_actions, args, device, amp):
    ctx_tokens = ctx_tokens.to(device)
    ctx_actions = ctx_actions.to(device)
    bsz = ctx_tokens.size(0)
    alive = torch.ones(bsz, dtype=torch.bool, device=device)
    episode_return = torch.zeros(bsz, dtype=torch.float32, device=device)

    rollout = {"tokens": [], "h": [], "actions": [], "logp": [], "values": [],
               "rewards": [], "dones": []}

    for _ in range(args.max_dream_steps):
        if not alive.any():
            break
        with torch.amp.autocast("cuda", enabled=amp is not None and device.type == "cuda", dtype=amp):
            h_t = predictor.encode_context(ctx_tokens, ctx_actions)
            token_t = ctx_tokens[:, -1]
            action_t, logp_t, _, value_t = policy.act(token_t, h_t.float())

            pred_actions = torch.cat([ctx_actions[:, 1:], action_t.unsqueeze(1)], dim=1)
            pred_tokens, reward, done_prob = predictor.predict_next_frame(
                ctx_tokens, pred_actions, temperature=args.temperature,
                return_aux=True)

        done = done_prob >= args.done_threshold
        reward = reward.float().clamp(args.reward_clip_min, args.reward_clip_max)
        active = alive.float()
        masked_reward = reward * active
        masked_done = done & alive
        episode_return += masked_reward

        rollout["tokens"].append(token_t.detach().cpu())
        rollout["h"].append(h_t.detach().cpu().float())
        rollout["actions"].append(action_t.detach().cpu())
        rollout["logp"].append(logp_t.detach().cpu())
        rollout["values"].append(value_t.detach().cpu())
        rollout["rewards"].append(masked_reward.detach().cpu())
        rollout["dones"].append(masked_done.detach().cpu())

        alive &= ~done
        ctx_tokens = torch.cat([ctx_tokens[:, 1:], pred_tokens.unsqueeze(1)], dim=1)
        ctx_actions = pred_actions

    if not rollout["rewards"]:
        return None, episode_return

    if alive.any():
        with torch.amp.autocast("cuda", enabled=amp is not None and device.type == "cuda", dtype=amp):
            h_t = predictor.encode_context(ctx_tokens, ctx_actions)
            _, bootstrap = policy(ctx_tokens[:, -1], h_t.float())
        bootstrap = bootstrap * alive.float()
    else:
        bootstrap = torch.zeros(bsz, device=device)

    rewards = torch.stack(rollout["rewards"]).to(device)
    values = torch.stack(rollout["values"]).to(device)
    dones = torch.stack(rollout["dones"]).to(device)
    adv, returns = compute_gae_sequence(
        rewards, values, dones, bootstrap, args.gamma, args.lam)
    rollout["advantages"] = adv.cpu()
    rollout["returns"] = returns.cpu()
    return rollout, episode_return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/atari/atari_pong_h200.yaml")
    parser.add_argument("--config-section", default="actor_dream")
    parser.add_argument("--replay-dir", default=None)
    parser.add_argument("--checkpoint-dir", default=None)
    parser.add_argument("--fsq-checkpoint", default=None)
    parser.add_argument("--predictor-checkpoint", default=None)
    parser.add_argument("--pretrained", default=None)
    parser.add_argument("--n-iterations", type=int, default=None)
    parser.add_argument("--n-episodes", type=int, default=None)
    parser.add_argument("--max-dream-steps", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--done-threshold", type=float, default=None)
    parser.add_argument("--reward-clip-min", type=float, default=None)
    parser.add_argument("--reward-clip-max", type=float, default=None)
    parser.add_argument("--lr", type=float, default=None)
    parser.add_argument("--gamma", type=float, default=None)
    parser.add_argument("--lam", type=float, default=None)
    parser.add_argument("--clip-eps", type=float, default=None)
    parser.add_argument("--ppo-epochs", type=int, default=None)
    parser.add_argument("--minibatch-size", type=int, default=None)
    parser.add_argument("--entropy-coeff", type=float, default=None)
    parser.add_argument("--critic-coeff", type=float, default=None)
    parser.add_argument("--max-grad-norm", type=float, default=None)
    parser.add_argument("--amp-dtype", choices=["none", "float16", "bfloat16"], default=None)
    parser.add_argument("--compile-mode", choices=["none", "default", "reduce-overhead"], default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--init-from", default=None,
                        help="Warm-start actor weights from a checkpoint, but reset optimizer/iteration/RNG state.")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    apply_config(args, section=args.config_section)

    atari_cfg = load_config(args.config, section="atari")
    fsq_cfg = load_config(args.config, section="fsq")
    model_cfg = load_config(args.config, section="model")
    pred_cfg = load_config(args.config, section="predictor_sls")

    args.replay_dir = args.replay_dir or atari_cfg.get("replay_dir", "data/atari/Pong/replay")
    args.checkpoint_dir = args.checkpoint_dir or "checkpoints_atari_actor_dream"
    args.fsq_checkpoint = args.fsq_checkpoint or pred_cfg.get("fsq_checkpoint")
    args.predictor_checkpoint = args.predictor_checkpoint or str(Path(pred_cfg.get("checkpoint_dir")) / "predictor_best.pt")
    args.n_iterations = args.n_iterations or 1000
    args.n_episodes = args.n_episodes or 32
    args.max_dream_steps = args.max_dream_steps or 50
    args.temperature = args.temperature if args.temperature is not None else 0.0
    args.done_threshold = args.done_threshold if args.done_threshold is not None else 0.5
    args.reward_clip_min = args.reward_clip_min if args.reward_clip_min is not None else -1.0
    args.reward_clip_max = args.reward_clip_max if args.reward_clip_max is not None else 1.0
    args.lr = args.lr or 2.5e-4
    args.gamma = args.gamma if args.gamma is not None else 0.99
    args.lam = args.lam if args.lam is not None else 0.95
    args.clip_eps = args.clip_eps if args.clip_eps is not None else 0.2
    args.ppo_epochs = args.ppo_epochs or 4
    args.minibatch_size = args.minibatch_size or 256
    args.entropy_coeff = args.entropy_coeff if args.entropy_coeff is not None else 0.01
    args.critic_coeff = args.critic_coeff if args.critic_coeff is not None else 0.5
    args.max_grad_norm = args.max_grad_norm if args.max_grad_norm is not None else 0.5
    args.amp_dtype = args.amp_dtype or "bfloat16"
    args.compile_mode = args.compile_mode or "reduce-overhead"
    args.seed = args.seed if args.seed is not None else int(atari_cfg.get("seed", 42))

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    rng = np.random.default_rng(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    amp = amp_dtype(args.amp_dtype)
    ckpt_dir = Path(args.checkpoint_dir)
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    replay = load_replay_arrays(args.replay_dir)
    metadata = load_metadata(args.replay_dir) or {}
    n_actions = int(metadata.get("n_actions", pred_cfg.get("n_actions", 6)))
    print(f"Replay: {args.replay_dir} steps={len(replay.obs)} n_actions={n_actions}")

    fsq = FSQVAE(
        img_channels=int(fsq_cfg.get("img_channels", 3)),
        levels=fsq_cfg.get("levels", [8, 5, 5, 5]),
        norm_type=fsq_cfg.get("norm_type", "group"),
        latent_grid=int(fsq_cfg.get("latent_grid", 16)),
    ).to(device)
    fsq.load_state_dict(load_clean_state(args.fsq_checkpoint, device))
    fsq.eval()
    tokens = encode_replay_obs(fsq, replay.obs, batch_size=256, device=device)
    starts = valid_context_starts(replay, int(model_cfg.get("context_frames", 4)))

    world_model = WorldModel(
        vocab_size=int(model_cfg.get("vocab_size", 1000)),
        n_actions=n_actions,
        embed_dim=int(model_cfg.get("embed_dim", 384)),
        n_heads=int(model_cfg.get("n_heads", 8)),
        n_layers=int(model_cfg.get("n_layers", 8)),
        context_frames=int(model_cfg.get("context_frames", 4)),
        dropout=float(model_cfg.get("dropout", 0.1)),
        tokens_per_frame=int(model_cfg.get("tokens_per_frame", 256)),
        adaln=bool(model_cfg.get("adaln", False)),
        use_status_token=False,
        use_cpc=False,
    ).to(device)
    predictor = AtariPredictorWithHeads(
        world_model, hidden_dim=int(model_cfg.get("embed_dim", 384))).to(device)
    state = load_clean_state(args.predictor_checkpoint, device)
    if "world_model.head.weight" in state:
        predictor.load_state_dict(state)
    else:
        wm_state, aux_state = split_atari_predictor_state(state)
        predictor.world_model.load_state_dict(wm_state)
        predictor.load_state_dict(aux_state, strict=False)
    predictor.eval()
    for p in predictor.parameters():
        p.requires_grad_(False)

    policy = AtariCNNPolicy(
        vocab_size=int(model_cfg.get("vocab_size", 1000)),
        n_actions=n_actions,
        grid_size=int(fsq_cfg.get("latent_grid", 16)),
        h_dim=int(model_cfg.get("embed_dim", 384)),
    ).to(device)
    if args.pretrained and Path(args.pretrained).exists():
        policy.load_state_dict(load_clean_state(args.pretrained, device))
        print(f"Loaded pretrained actor from {args.pretrained}")
    elif args.pretrained:
        print(f"Pretrained actor not found at {args.pretrained}; starting cold")

    latest = ckpt_dir / "actor_dream_latest.pt"
    optimizer = torch.optim.Adam(policy.parameters(), lr=args.lr)
    start_iter = 1
    if args.init_from:
        policy.load_state_dict(load_clean_state(args.init_from, device))
        print(f"Warm-started dream actor weights from {args.init_from}; "
              "reset optimizer/iteration/RNG state")
    elif args.resume and latest.exists():
        ckpt = torch.load(latest, map_location=device, weights_only=False)
        policy.load_state_dict(ckpt["controller"])
        optimizer.load_state_dict(ckpt["optimizer"])
        start_iter = int(ckpt["iteration"]) + 1
        rng.bit_generator.state = ckpt["rng_state"]
        print(f"Resumed dream actor from iteration {start_iter - 1}")

    if args.compile_mode != "none" and device.type == "cuda":
        predictor = torch.compile(predictor, mode=args.compile_mode)
        policy = torch.compile(policy, mode=args.compile_mode)
        print(f"torch.compile enabled (mode={args.compile_mode})")

    log_path = ckpt_dir / "actor_dream_log.csv"
    log_file = open(log_path, "a" if (args.resume or args.init_from) and log_path.exists() else "w", newline="")
    log = csv.writer(log_file)
    if log_file.tell() == 0:
        log.writerow(["iteration", "mean_return", "ppo_loss", "entropy", "time_s"])

    for iteration in range(start_iter, args.n_iterations + 1):
        t0 = time.time()
        ctx_tokens, ctx_actions = sample_contexts(
            starts, tokens, replay.actions, int(model_cfg.get("context_frames", 4)),
            args.n_episodes, rng)
        policy.eval()
        rollout, dream_return = dream_rollout(
            predictor, policy, ctx_tokens, ctx_actions, args, device, amp)
        if rollout is None:
            print(f"iteration={iteration} empty dream rollout; skipping")
            continue
        policy.train()
        loss, entropy = ppo_update(policy, optimizer, rollout, args, device, amp)
        elapsed = time.time() - t0
        mean_return = float(dream_return.mean().item())
        print(f"iter={iteration} return={mean_return:+.3f} loss={loss:.4f} "
              f"entropy={entropy:.3f} time={elapsed:.1f}s")
        log.writerow([iteration, f"{mean_return:.6f}", f"{loss:.6f}",
                      f"{entropy:.6f}", f"{elapsed:.1f}"])
        log_file.flush()

        clean_policy = {k.removeprefix("_orig_mod."): v for k, v in policy.state_dict().items()}
        torch.save({
            "iteration": iteration,
            "controller": clean_policy,
            "optimizer": optimizer.state_dict(),
            "rng_state": rng.bit_generator.state,
        }, latest)
        if iteration == 1 or iteration % 10 == 0:
            torch.save(clean_policy, ckpt_dir / "actor_dream_best_effort.pt")

    clean_policy = {k.removeprefix("_orig_mod."): v for k, v in policy.state_dict().items()}
    torch.save(clean_policy, ckpt_dir / "actor_dream_final.pt")
    log_file.close()
    print(f"Done. Checkpoints saved to {ckpt_dir}")


if __name__ == "__main__":
    main()

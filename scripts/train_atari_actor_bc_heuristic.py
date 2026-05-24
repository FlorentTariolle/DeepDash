"""Train an Atari actor by imitating a simple visual Pong heuristic.

This is a controller sanity probe, not a paper training path. It answers:
can the frozen FSQ/predictor features and AtariCNNPolicy represent a basic
Pong control policy when labels are supplied directly?
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
import time
from pathlib import Path

import ale_py
import gymnasium as gym
import numpy as np
import torch
import torch.nn.functional as F

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atari.controller import AtariCNNPolicy
from atari.predictor import split_atari_predictor_state
from deepdash.config import apply_config, load_config
from deepdash.fsq import FSQVAE
from deepdash.wandb_utils import wandb_finish, wandb_init, wandb_log
from deepdash.world_model import WorldModel
from scripts.train_atari_actor_real import (
    encode_frame,
    load_clean_state,
    parse_action_subset,
    resize_frame_to_64,
)

gym.register_envs(ale_py)
ACTION_NAMES = ["NOOP", "FIRE", "RIGHT", "LEFT", "RIGHTFIRE", "LEFTFIRE"]


def largest_component_center(mask: np.ndarray, *, min_area: int, max_area: int,
                             x_min: int = 0, x_max: int | None = None,
                             y_min: int = 0, y_max: int | None = None) -> tuple[float, float] | None:
    """Return center of the largest connected component matching bounds."""
    h, w = mask.shape
    x_max = w if x_max is None else int(x_max)
    y_max = h if y_max is None else int(y_max)
    bounded = mask.copy()
    bounded[:y_min] = False
    bounded[y_max:] = False
    bounded[:, :x_min] = False
    bounded[:, x_max:] = False

    seen = np.zeros_like(bounded, dtype=bool)
    best: tuple[int, float, float] | None = None
    for y0, x0 in zip(*np.nonzero(bounded)):
        if seen[y0, x0]:
            continue
        stack = [(int(y0), int(x0))]
        seen[y0, x0] = True
        xs, ys = [], []
        while stack:
            y, x = stack.pop()
            ys.append(y)
            xs.append(x)
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                yy, xx = y + dy, x + dx
                if (
                    0 <= yy < h and 0 <= xx < w
                    and bounded[yy, xx]
                    and not seen[yy, xx]
                ):
                    seen[yy, xx] = True
                    stack.append((yy, xx))
        area = len(xs)
        if min_area <= area <= max_area and (best is None or area > best[0]):
            best = (area, float(np.mean(xs)), float(np.mean(ys)))
    if best is None:
        return None
    return best[1], best[2]


def pong_objects(obs: np.ndarray) -> tuple[float | None, float | None]:
    """Return approximate (ball_y, player_paddle_y) from raw ALE RGB Pong."""
    # ALE Pong uses stable object colors in RGB. Brightness thresholding sees
    # the large score/playfield bands, so track only the tiny ball component
    # and the right-side green paddle component.
    ball_mask = (obs == np.array([236, 236, 236], dtype=np.uint8)).all(axis=2)
    paddle_mask = (obs == np.array([92, 186, 92], dtype=np.uint8)).all(axis=2)

    ball = largest_component_center(
        ball_mask, min_area=4, max_area=32, x_min=8, x_max=152, y_min=34, y_max=194)
    paddle = largest_component_center(
        paddle_mask, min_area=8, max_area=96, x_min=128, y_min=34, y_max=194)
    ball_y = None if ball is None else ball[1]
    paddle_y = None if paddle is None else paddle[1]
    return ball_y, paddle_y


def heuristic_action(obs: np.ndarray, up_action: int, down_action: int,
                     last_action: int) -> int:
    ball_y, paddle_y = pong_objects(obs)
    if paddle_y is None:
        return int(last_action)
    target_y = 106.0 if ball_y is None else ball_y
    error = target_y - paddle_y
    if abs(error) <= 8.0:
        return int(down_action if last_action == up_action else up_action)
    return int(up_action if error < 0.0 else down_action)


def eval_heuristic(game: str, atari_cfg: dict, up_action: int, down_action: int,
                   episodes: int, max_steps: int, seed: int) -> dict:
    env = gym.make(
        f"ALE/{game}-v5",
        frameskip=int(atari_cfg.get("frame_skip", 4)),
        repeat_action_probability=float(atari_cfg.get("repeat_action_probability", 0.0)),
    )
    rng = np.random.default_rng(seed)
    returns, lengths = [], []
    counts = collections.Counter()
    for _ in range(int(episodes)):
        obs, _ = env.reset(seed=int(rng.integers(0, 2**31 - 1)))
        last_action = int(down_action)
        total = 0.0
        steps = 0
        for _ in range(int(max_steps)):
            action = heuristic_action(obs, up_action, down_action, last_action)
            counts[action] += 1
            obs, reward, terminated, truncated, _ = env.step(action)
            last_action = action
            total += float(reward)
            steps += 1
            if terminated or truncated:
                break
        returns.append(total)
        lengths.append(steps)
    env.close()
    return {
        "up_action": int(up_action),
        "down_action": int(down_action),
        "returns": returns,
        "lengths": lengths,
        "mean_return": float(np.mean(returns)) if returns else 0.0,
        "action_counts": {
            ACTION_NAMES[a] if a < len(ACTION_NAMES) else str(a): int(counts[a])
            for a in sorted(counts)
        },
    }


@torch.no_grad()
def collect_bc_dataset(env, fsq, predictor, device, model_cfg, action_subset,
                       up_action: int, down_action: int, n_steps: int,
                       seed: int):
    k = int(model_cfg.get("context_frames", 4))
    subset_to_label = {action: idx for idx, action in enumerate(action_subset)}
    rng = np.random.default_rng(seed)
    obs, _ = env.reset(seed=seed)
    frame = resize_frame_to_64(obs)
    token = encode_frame(fsq, frame, device).cpu()
    ctx_tokens = [token.clone() for _ in range(k)]
    ctx_actions = [0 for _ in range(k)]
    last_action = int(down_action)

    tokens, hidden, labels = [], [], []
    returns = []
    ep_return = 0.0
    action_counts = collections.Counter()
    for _ in range(int(n_steps)):
        action = heuristic_action(obs, up_action, down_action, last_action)
        label = subset_to_label[action]
        ctx_t = torch.stack(ctx_tokens[-k:], dim=1).to(device)
        ctx_a = torch.tensor([ctx_actions[-k:]], dtype=torch.long, device=device)
        h_t = predictor.encode_context(ctx_t, ctx_a, return_action_hidden=False)
        tokens.append(ctx_t[:, -1].cpu().squeeze(0))
        hidden.append(h_t.cpu().float().squeeze(0))
        labels.append(label)

        obs, reward, terminated, truncated, _ = env.step(action)
        action_counts[action] += 1
        ep_return += float(reward)
        last_action = action
        frame = resize_frame_to_64(obs)
        ctx_tokens.append(encode_frame(fsq, frame, device).cpu())
        ctx_actions.append(action)
        if terminated or truncated:
            returns.append(ep_return)
            ep_return = 0.0
            obs, _ = env.reset(seed=int(rng.integers(0, 2**31 - 1)))
            frame = resize_frame_to_64(obs)
            token = encode_frame(fsq, frame, device).cpu()
            ctx_tokens = [token.clone() for _ in range(k)]
            ctx_actions = [0 for _ in range(k)]
            last_action = int(down_action)
    if ep_return != 0.0:
        returns.append(ep_return)
    return {
        "tokens": torch.stack(tokens),
        "hidden": torch.stack(hidden),
        "labels": torch.tensor(labels, dtype=torch.long),
        "heuristic_returns": returns,
        "action_counts": {
            ACTION_NAMES[a] if a < len(ACTION_NAMES) else str(a): int(action_counts[a])
            for a in sorted(action_counts)
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/atari/atari_pong_h200_dreamer_smoke.yaml")
    parser.add_argument("--config-section", default="actor_real")
    parser.add_argument("--checkpoint-dir", default="checkpoints_atari_actor_bc_heuristic")
    parser.add_argument("--fsq-checkpoint", default=None)
    parser.add_argument("--predictor-checkpoint", default=None)
    parser.add_argument("--policy-action-subset", default="4,5")
    parser.add_argument("--n-steps", type=int, default=20000)
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=512)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--eval-episodes", type=int, default=5)
    parser.add_argument("--max-steps-per-episode", type=int, default=27000)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--wandb-project", default=None)
    parser.add_argument("--wandb-name", default=None)
    args = parser.parse_args()
    apply_config(args, section=args.config_section)

    atari_cfg = load_config(args.config, section="atari")
    fsq_cfg = load_config(args.config, section="fsq")
    model_cfg = load_config(args.config, section="model")
    pred_cfg = load_config(args.config, section="predictor_sls")
    actor_cfg = load_config(args.config, section="actor_real")
    game = atari_cfg.get("game", "Pong")
    args.seed = args.seed if args.seed is not None else int(atari_cfg.get("seed", 42))
    args.fsq_checkpoint = args.fsq_checkpoint or pred_cfg.get("fsq_checkpoint")
    args.predictor_checkpoint = args.predictor_checkpoint or str(
        Path(pred_cfg.get("checkpoint_dir")) / "predictor_best.pt")
    args.wandb_project = args.wandb_project or "sls-wm-atari"
    args.wandb_name = args.wandb_name or f"actor-bc-heuristic-{Path(args.checkpoint_dir).name}"

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    ckpt_dir = Path(args.checkpoint_dir)
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    env = gym.make(
        f"ALE/{game}-v5",
        frameskip=int(atari_cfg.get("frame_skip", 4)),
        repeat_action_probability=float(atari_cfg.get("repeat_action_probability", 0.0)),
    )
    n_actions = int(env.action_space.n)
    action_subset = parse_action_subset(args.policy_action_subset, n_actions)
    if len(action_subset) != 2:
        raise ValueError("heuristic BC currently expects a two-action up/down subset")

    fsq = FSQVAE(
        img_channels=int(fsq_cfg.get("img_channels", 3)),
        levels=fsq_cfg.get("levels", [8, 5, 5, 5]),
        norm_type=fsq_cfg.get("norm_type", "group"),
        latent_grid=int(fsq_cfg.get("latent_grid", 16)),
    ).to(device)
    fsq.load_state_dict(load_clean_state(args.fsq_checkpoint, device))
    fsq.eval()

    predictor = WorldModel(
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
    pred_state, _ = split_atari_predictor_state(
        load_clean_state(args.predictor_checkpoint, device))
    predictor.load_state_dict(pred_state)
    predictor.eval()

    candidates = [
        (action_subset[0], action_subset[1]),
        (action_subset[1], action_subset[0]),
    ]
    heuristic_evals = [
        eval_heuristic(
            game, atari_cfg, up, down, args.eval_episodes,
            args.max_steps_per_episode, args.seed + 100 + i)
        for i, (up, down) in enumerate(candidates)
    ]
    best_map = max(heuristic_evals, key=lambda item: item["mean_return"])
    up_action = int(best_map["up_action"])
    down_action = int(best_map["down_action"])
    print(f"Selected heuristic map: up={up_action} down={down_action}")
    print(json.dumps({"heuristic_evals": heuristic_evals}, indent=2))

    data = collect_bc_dataset(
        env, fsq, predictor, device, model_cfg, action_subset,
        up_action, down_action, args.n_steps, args.seed + 1000)
    env.close()
    print(
        "BC dataset: "
        f"steps={len(data['labels'])} labels={torch.bincount(data['labels']).tolist()} "
        f"heuristic_returns={data['heuristic_returns']} actions={data['action_counts']}"
    )

    policy = AtariCNNPolicy(
        vocab_size=int(model_cfg.get("vocab_size", 1000)),
        n_actions=len(action_subset),
        grid_size=int(fsq_cfg.get("latent_grid", 16)),
        h_dim=int(model_cfg.get("embed_dim", 384)),
        value_head_type=str(actor_cfg.get("value_head_type", "scalar")),
        value_bins=int(actor_cfg.get("value_twohot_bins", 255)),
        value_low=float(actor_cfg.get("value_twohot_low", -25.0)),
        value_high=float(actor_cfg.get("value_twohot_high", 25.0)),
    ).to(device)
    optimizer = torch.optim.AdamW(policy.parameters(), lr=args.lr, weight_decay=0.01)
    wandb_init(
        project=args.wandb_project,
        name=args.wandb_name,
        config={**vars(args), "action_subset": action_subset, "heuristic_evals": heuristic_evals},
    )

    x_tokens = data["tokens"].to(device)
    x_hidden = data["hidden"].to(device)
    y = data["labels"].to(device)
    n = y.numel()
    idx = torch.arange(n, device=device)
    log_path = ckpt_dir / "actor_bc_log.csv"
    with open(log_path, "w", newline="") as f:
        log = csv.writer(f)
        log.writerow(["epoch", "loss", "acc", "entropy", "time_s"])
        for epoch in range(1, int(args.epochs) + 1):
            t0 = time.time()
            perm = idx[torch.randperm(n, device=device)]
            total_loss = total_correct = total_entropy = total_seen = 0
            policy.train()
            for start in range(0, n, int(args.batch_size)):
                mb = perm[start:start + int(args.batch_size)]
                logits, _ = policy(x_tokens[mb], x_hidden[mb])
                loss = F.cross_entropy(logits, y[mb])
                optimizer.zero_grad(set_to_none=True)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(policy.parameters(), 0.5)
                optimizer.step()
                with torch.no_grad():
                    dist = torch.distributions.Categorical(logits=logits)
                    total_loss += float(loss.item()) * mb.numel()
                    total_correct += int((logits.argmax(dim=-1) == y[mb]).sum().item())
                    total_entropy += float(dist.entropy().sum().item())
                    total_seen += int(mb.numel())
            metrics = {
                "epoch": epoch,
                "loss": total_loss / max(total_seen, 1),
                "acc": total_correct / max(total_seen, 1),
                "entropy": total_entropy / max(total_seen, 1),
                "time_s": time.time() - t0,
            }
            print(
                f"epoch={epoch}/{args.epochs} loss={metrics['loss']:.4f} "
                f"acc={100*metrics['acc']:.2f}% entropy={metrics['entropy']:.3f}"
            )
            log.writerow([
                epoch,
                f"{metrics['loss']:.6f}",
                f"{metrics['acc']:.6f}",
                f"{metrics['entropy']:.6f}",
                f"{metrics['time_s']:.1f}",
            ])
            f.flush()
            wandb_log({f"actor_bc/{key}": value for key, value in metrics.items()})

    clean = {k.removeprefix("_orig_mod."): v for k, v in policy.state_dict().items()}
    torch.save(clean, ckpt_dir / "actor_bc_final.pt")
    summary = {
        "action_subset": action_subset,
        "selected_up_action": up_action,
        "selected_down_action": down_action,
        "heuristic_evals": heuristic_evals,
        "dataset_action_counts": data["action_counts"],
        "dataset_returns": data["heuristic_returns"],
        "checkpoint": str(ckpt_dir / "actor_bc_final.pt"),
    }
    (ckpt_dir / "actor_bc_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    wandb_finish()


if __name__ == "__main__":
    main()

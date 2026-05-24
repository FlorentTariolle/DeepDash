"""Render Atari actor rollouts from the real environment to self-contained HTML."""

from __future__ import annotations

import argparse
import base64
import collections
import html
import io
import json
import sys
from pathlib import Path

import ale_py
import gymnasium as gym
import numpy as np
import torch
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atari.controller import AtariCNNPolicy
from atari.predictor import split_atari_predictor_state
from deepdash.config import apply_config, load_config
from deepdash.fsq import FSQVAE
from deepdash.world_model import WorldModel
from scripts.train_atari_actor_real import encode_frame, load_clean_state, resize_frame_to_64

gym.register_envs(ale_py)

ACTION_NAMES = ["NOOP", "FIRE", "RIGHT", "LEFT", "RIGHTFIRE", "LEFTFIRE"]


def image_uri(frame: np.ndarray, scale: int) -> str:
    image = Image.fromarray(frame.astype(np.uint8), mode="RGB")
    if scale > 1:
        image = image.resize((image.width * scale, image.height * scale), Image.Resampling.NEAREST)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def load_models(args, device):
    atari_cfg = load_config(args.config, section="atari")
    fsq_cfg = load_config(args.config, section="fsq")
    model_cfg = load_config(args.config, section="model")
    pred_cfg = load_config(args.config, section="predictor_sls")
    actor_cfg = load_config(args.config, section="actor_dream")

    fsq = FSQVAE(
        img_channels=int(fsq_cfg.get("img_channels", 3)),
        levels=fsq_cfg.get("levels", [8, 5, 5, 5]),
        norm_type=fsq_cfg.get("norm_type", "group"),
        latent_grid=int(fsq_cfg.get("latent_grid", 16)),
    ).to(device)
    fsq.load_state_dict(load_clean_state(args.fsq_checkpoint or pred_cfg.get("fsq_checkpoint"), device))
    fsq.eval()

    env = gym.make(
        f"ALE/{atari_cfg.get('game', 'Pong')}-v5",
        frameskip=int(atari_cfg.get("frame_skip", 4)),
        repeat_action_probability=float(atari_cfg.get("repeat_action_probability", 0.0)),
        render_mode="rgb_array",
    )
    n_actions = int(env.action_space.n)

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
    pred_ckpt = args.predictor_checkpoint or str(Path(pred_cfg.get("checkpoint_dir")) / "predictor_best.pt")
    pred_state, _ = split_atari_predictor_state(load_clean_state(pred_ckpt, device))
    predictor.load_state_dict(pred_state)
    predictor.eval()

    policy = AtariCNNPolicy(
        vocab_size=int(model_cfg.get("vocab_size", 1000)),
        n_actions=n_actions,
        grid_size=int(fsq_cfg.get("latent_grid", 16)),
        h_dim=int(model_cfg.get("embed_dim", 384)),
        value_head_type=str(actor_cfg.get("value_head_type", "scalar")),
        value_bins=int(actor_cfg.get("value_twohot_bins", 255)),
        value_low=float(actor_cfg.get("value_twohot_low", -25.0)),
        value_high=float(actor_cfg.get("value_twohot_high", 25.0)),
    ).to(device)
    policy.load_state_dict(load_clean_state(args.actor_checkpoint, device))
    policy.eval()
    return atari_cfg, model_cfg, env, fsq, predictor, policy


@torch.no_grad()
def collect_rollouts(args):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    atari_cfg, model_cfg, env, fsq, predictor, policy = load_models(args, device)
    k = int(model_cfg.get("context_frames", 4))
    rng = np.random.default_rng(args.seed)
    episodes = []
    total_counts = collections.Counter()

    for ep in range(args.n_episodes):
        obs, _ = env.reset(seed=int(rng.integers(0, 2**31 - 1)))
        frame = resize_frame_to_64(obs)
        token = encode_frame(fsq, frame, device).cpu()
        ctx_tokens = [token.clone() for _ in range(k)]
        ctx_actions = [0 for _ in range(k)]
        ep_return = 0.0
        frames = []
        actions = []
        rewards = []

        for step in range(args.max_steps):
            ctx_t = torch.stack(ctx_tokens[-k:], dim=1).to(device)
            ctx_a = torch.tensor([ctx_actions[-k:]], dtype=torch.long, device=device)
            h_t = predictor.encode_context(ctx_t, ctx_a, return_action_hidden=False)
            logits, _ = policy(ctx_t[:, -1], h_t.float())
            probs = torch.softmax(logits.float(), dim=-1).squeeze(0).cpu().numpy()
            if args.stochastic:
                action = int(np.random.default_rng(args.seed + ep * 100000 + step).choice(len(probs), p=probs))
            else:
                action = int(probs.argmax())
            next_obs, reward, terminated, truncated, _ = env.step(action)
            if step % args.frame_stride == 0 or reward != 0.0 or terminated or truncated:
                frames.append({
                    "image": image_uri(resize_frame_to_64(obs), args.scale),
                    "step": step,
                    "action": ACTION_NAMES[action] if action < len(ACTION_NAMES) else str(action),
                    "reward": float(reward),
                    "return": float(ep_return + reward),
                    "probs": [float(x) for x in probs],
                })
            actions.append(action)
            rewards.append(float(reward))
            total_counts[action] += 1
            ep_return += float(reward)
            obs = next_obs
            frame = resize_frame_to_64(obs)
            ctx_tokens.append(encode_frame(fsq, frame, device).cpu())
            ctx_actions.append(action)
            if terminated or truncated:
                break
        episodes.append({
            "episode": ep + 1,
            "return": ep_return,
            "length": len(frames),
            "action_counts": {
                ACTION_NAMES[a] if a < len(ACTION_NAMES) else str(a): int(actions.count(a))
                for a in range(env.action_space.n)
            },
            "frames": frames,
        })
    env.close()
    return {
        "actor_checkpoint": args.actor_checkpoint,
        "stochastic": bool(args.stochastic),
        "episodes": episodes,
        "mean_return": float(np.mean([ep["return"] for ep in episodes])) if episodes else 0.0,
        "action_counts": {
            ACTION_NAMES[a] if a < len(ACTION_NAMES) else str(a): int(total_counts[a])
            for a in range(env.action_space.n)
        },
    }


def write_html(data, out_path: Path):
    payload = json.dumps(data)
    escaped_title = html.escape(Path(data["actor_checkpoint"]).name)
    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Atari Policy Rollouts - {escaped_title}</title>
<style>
body {{ margin: 0; background: #111; color: #eee; font-family: system-ui, sans-serif; }}
main {{ max-width: 1040px; margin: 0 auto; padding: 24px; }}
.viewer {{ display: grid; grid-template-columns: minmax(320px, 512px) 1fr; gap: 24px; align-items: start; }}
img {{ width: 100%; image-rendering: pixelated; background: #000; border: 1px solid #333; }}
button, select {{ background: #222; color: #eee; border: 1px solid #555; padding: 8px 10px; }}
.controls {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 16px 0; }}
.meta, pre {{ background: #1b1b1b; border: 1px solid #333; padding: 12px; overflow: auto; }}
.bar {{ height: 10px; background: #333; margin: 4px 0 10px; }}
.bar > span {{ display: block; height: 100%; background: #76b7b2; }}
</style>
</head>
<body>
<main>
<h1>Atari Policy Rollouts</h1>
<div class="meta" id="summary"></div>
<div class="controls">
  <select id="episode"></select>
  <button id="prev">Prev</button>
  <button id="play">Play</button>
  <button id="next">Next</button>
  <span id="frameLabel"></span>
</div>
<section class="viewer">
  <img id="frame" alt="Atari frame">
  <div>
    <div class="meta" id="frameMeta"></div>
    <div id="probs"></div>
  </div>
</section>
</main>
<script>
const data = {payload};
let epIndex = 0;
let frameIndex = 0;
let timer = null;
const names = {json.dumps(ACTION_NAMES)};
const episode = document.getElementById('episode');
for (const [i, ep] of data.episodes.entries()) {{
  const opt = document.createElement('option');
  opt.value = i;
  opt.textContent = `Episode ${{ep.episode}} | return ${{ep.return}} | len ${{ep.length}}`;
  episode.appendChild(opt);
}}
function draw() {{
  const ep = data.episodes[epIndex];
  const fr = ep.frames[frameIndex];
  document.getElementById('summary').textContent =
    `checkpoint=${{data.actor_checkpoint}} | stochastic=${{data.stochastic}} | mean_return=${{data.mean_return.toFixed(2)}}`;
  document.getElementById('frame').src = fr.image;
  document.getElementById('frameLabel').textContent = `Frame ${{frameIndex + 1}} / ${{ep.frames.length}}`;
  document.getElementById('frameMeta').textContent =
    `episode=${{ep.episode}} return=${{ep.return}} length=${{ep.length}} | step=${{fr.step}} action=${{fr.action}} reward=${{fr.reward}} cumulative=${{fr.return}}`;
  document.getElementById('probs').innerHTML = fr.probs.map((p, i) =>
    `<div>${{names[i] || i}} ${{p.toFixed(3)}}<div class="bar"><span style="width:${{Math.round(p * 100)}}%"></span></div></div>`
  ).join('');
}}
function advance(delta) {{
  const ep = data.episodes[epIndex];
  frameIndex = Math.max(0, Math.min(ep.frames.length - 1, frameIndex + delta));
  draw();
}}
episode.onchange = () => {{ epIndex = Number(episode.value); frameIndex = 0; draw(); }};
document.getElementById('prev').onclick = () => advance(-1);
document.getElementById('next').onclick = () => advance(1);
document.getElementById('play').onclick = () => {{
  if (timer) {{ clearInterval(timer); timer = null; document.getElementById('play').textContent = 'Play'; return; }}
  document.getElementById('play').textContent = 'Pause';
  timer = setInterval(() => {{
    const ep = data.episodes[epIndex];
    if (frameIndex >= ep.frames.length - 1) {{ clearInterval(timer); timer = null; document.getElementById('play').textContent = 'Play'; }}
    else advance(1);
  }}, 80);
}};
document.addEventListener('keydown', ev => {{
  if (ev.key === 'ArrowLeft') advance(-1);
  if (ev.key === 'ArrowRight') advance(1);
  if (ev.key === ' ') document.getElementById('play').click();
}});
draw();
</script>
</body>
</html>
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/atari/atari_pong_h200.yaml")
    parser.add_argument("--config-section", default="evaluation")
    parser.add_argument("--actor-checkpoint", required=True)
    parser.add_argument("--fsq-checkpoint", default=None)
    parser.add_argument("--predictor-checkpoint", default=None)
    parser.add_argument("--n-episodes", type=int, default=None)
    parser.add_argument("--max-steps", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--scale", type=int, default=6)
    parser.add_argument("--frame-stride", type=int, default=4,
                        help="Record one frame every N env steps, plus reward/done frames.")
    parser.add_argument("--stochastic", action="store_true")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    apply_config(args, section=args.config_section)
    args.n_episodes = args.n_episodes or 3
    args.seed = args.seed if args.seed is not None else 12345
    data = collect_rollouts(args)
    write_html(data, Path(args.out))
    print(json.dumps({k: data[k] for k in ("actor_checkpoint", "stochastic", "mean_return", "action_counts")}, indent=2))
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()

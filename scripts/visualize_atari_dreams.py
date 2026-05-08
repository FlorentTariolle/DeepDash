"""Build an interactive Atari world-model dream viewer.

The viewer compares real replay frames against autoregressive world-model
predictions while feeding the recorded action sequence. It writes a
self-contained HTML file with keyboard controls:

    Left/Right  previous/next frame
    R           reset to the first dreamed frame
    T           next sampled episode/window

Example:
    python scripts/visualize_atari_dreams.py \
        --config configs/atari/atari_pong_h200.yaml \
        --out outputs/atari/pong_h200_dreams.html
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
from pathlib import Path

import numpy as np
import torch
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atari.predictor import AtariPredictorWithHeads, split_atari_predictor_state
from atari.replay_buffer import load_replay_arrays
from deepdash.config import apply_config, load_config
from deepdash.fsq import FSQVAE
from deepdash.world_model import WorldModel
from scripts.train_atari_actor_real import load_clean_state


ACTION_NAMES = ["NOOP", "FIRE", "RIGHT", "LEFT", "RIGHTFIRE", "LEFTFIRE"]


def image_data_uri(frame: np.ndarray, scale: int) -> str:
    img = Image.fromarray(frame.astype(np.uint8), mode="RGB")
    if scale != 1:
        img = img.resize((img.width * scale, img.height * scale), Image.Resampling.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return "data:image/png;base64," + encoded


@torch.no_grad()
def encode_frames(fsq: FSQVAE, frames: np.ndarray, device: torch.device) -> torch.Tensor:
    x = torch.from_numpy(np.asarray(frames).copy()).float().permute(0, 3, 1, 2).to(device) / 255.0
    return fsq.encode(x).reshape(x.size(0), -1)


@torch.no_grad()
def decode_tokens(fsq: FSQVAE, tokens: torch.Tensor, device: torch.device) -> np.ndarray:
    grid = int(fsq.latent_grid)
    indices = tokens.reshape(1, grid, grid).to(device)
    recon = fsq.decode_indices(indices)
    frame = recon[0].permute(1, 2, 0).float().cpu().numpy()
    return (frame * 255.0).clip(0, 255).astype(np.uint8)


def valid_starts(replay, context_frames: int, horizon: int, start_mode: str, seed: int):
    starts = []
    episode_ids = replay.episode_ids
    dones = replay.dones
    n = len(episode_ids)
    for i in range(0, n - context_frames - horizon):
        end = i + context_frames + horizon
        if np.all(episode_ids[i:end] == episode_ids[i]) and not np.any(dones[i:end - 1]):
            starts.append(i)
    starts = np.asarray(starts, dtype=np.int64)
    if len(starts) == 0:
        raise RuntimeError("no replay windows are long enough for this context and horizon")

    if start_mode == "beginning":
        _, first_idx = np.unique(episode_ids[starts], return_index=True)
        starts = starts[np.sort(first_idx)]
    else:
        rng = np.random.default_rng(seed)
        rng.shuffle(starts)
    return starts


@torch.no_grad()
def build_episode(predictor, fsq, replay, start: int, args, device):
    k = int(args.context_frames)
    h = int(args.horizon)
    ctx_tokens = encode_frames(fsq, replay.obs[start:start + k], device).unsqueeze(0)
    actions_np = replay.actions[start:start + k + h].astype(np.int64)
    ctx_actions = torch.from_numpy(actions_np[:k][None]).to(device)

    frames = []
    for step in range(h):
        action_window = torch.from_numpy(actions_np[step:step + k][None]).to(device)
        pred_tokens, pred_reward, done_prob = predictor.predict_next_frame(
            ctx_tokens, action_window, temperature=args.temperature, return_aux=True)
        pred_frame = decode_tokens(fsq, pred_tokens[0], device)
        gt_index = start + k + step
        action_id = int(actions_np[step + k - 1])
        frames.append({
            "gt": image_data_uri(replay.obs[gt_index], args.scale),
            "pred": image_data_uri(pred_frame, args.scale),
            "step": step + 1,
            "replay_index": int(gt_index),
            "action": action_id,
            "action_name": ACTION_NAMES[action_id] if action_id < len(ACTION_NAMES) else str(action_id),
            "reward": float(replay.rewards[gt_index]),
            "pred_reward": float(pred_reward[0].item()),
            "done": bool(replay.dones[gt_index]),
            "done_prob": float(done_prob[0].item()),
        })
        ctx_tokens = torch.cat([ctx_tokens[:, 1:], pred_tokens.unsqueeze(1)], dim=1)

    return {
        "episode_id": int(replay.episode_ids[start]),
        "start": int(start),
        "context_frames": k,
        "frames": frames,
    }


def write_html(episodes, out_path: Path):
    payload = json.dumps(episodes)
    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Atari Dream Viewer</title>
<style>
body {{ margin: 0; background: #111; color: #eee; font: 14px system-ui, sans-serif; }}
header {{ padding: 12px 16px; background: #1b1b1b; display: flex; gap: 18px; align-items: center; flex-wrap: wrap; }}
.wrap {{ padding: 16px; }}
.panes {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; align-items: start; }}
.pane {{ min-width: 0; }}
.label {{ margin-bottom: 8px; color: #bbb; font-weight: 600; }}
img {{ width: 100%; max-width: 512px; image-rendering: pixelated; background: #000; border: 1px solid #333; }}
.meta {{ margin-top: 12px; line-height: 1.6; color: #ccc; }}
kbd {{ background: #2c2c2c; border: 1px solid #555; border-radius: 4px; padding: 1px 5px; }}
@media (max-width: 760px) {{ .panes {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<header>
  <strong>Atari Dream Viewer</strong>
  <span><kbd>Left</kbd>/<kbd>Right</kbd> frame</span>
  <span><kbd>R</kbd> reset frame</span>
  <span><kbd>T</kbd> next episode</span>
  <span id="pos"></span>
</header>
<div class="wrap">
  <div class="panes">
    <div class="pane"><div class="label">Ground Truth</div><img id="gt" alt="ground truth"></div>
    <div class="pane"><div class="label">World Model Prediction</div><img id="pred" alt="prediction"></div>
  </div>
  <div class="meta" id="meta"></div>
</div>
<script>
const episodes = {payload};
let ep = 0;
let frame = 0;
function clampFrame() {{
  frame = Math.max(0, Math.min(frame, episodes[ep].frames.length - 1));
}}
function render() {{
  clampFrame();
  const e = episodes[ep];
  const f = e.frames[frame];
  document.getElementById('gt').src = f.gt;
  document.getElementById('pred').src = f.pred;
  document.getElementById('pos').textContent = `episode ${{ep + 1}}/${{episodes.length}} frame ${{frame + 1}}/${{e.frames.length}}`;
  document.getElementById('meta').innerHTML =
    `replay episode=${{e.episode_id}} start=${{e.start}} index=${{f.replay_index}}<br>` +
    `action=${{f.action}}:${{f.action_name}} reward=${{f.reward.toFixed(1)}} ` +
    `pred_reward=${{f.pred_reward.toFixed(3)}} done=${{f.done}} done_prob=${{f.done_prob.toFixed(3)}}`;
}}
document.addEventListener('keydown', ev => {{
  if (ev.key === 'ArrowRight') frame += 1;
  else if (ev.key === 'ArrowLeft') frame -= 1;
  else if (ev.key.toLowerCase() === 'r') frame = 0;
  else if (ev.key.toLowerCase() === 't') {{ ep = (ep + 1) % episodes.length; frame = 0; }}
  else return;
  ev.preventDefault();
  render();
}});
render();
</script>
</body>
</html>
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/atari/atari_pong_h200.yaml")
    parser.add_argument("--config-section", default="predictor_sls")
    parser.add_argument("--replay-dir", default=None)
    parser.add_argument("--fsq-checkpoint", default=None)
    parser.add_argument("--predictor-checkpoint", default=None)
    parser.add_argument("--out", default="outputs/atari/pong_dreams.html")
    parser.add_argument("--episodes", type=int, default=8)
    parser.add_argument("--horizon", type=int, default=50)
    parser.add_argument("--context-frames", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--start-mode", choices=["random", "beginning"], default="random")
    parser.add_argument("--scale", type=int, default=6)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    apply_config(args, section=args.config_section)

    atari_cfg = load_config(args.config, section="atari")
    fsq_cfg = load_config(args.config, section="fsq")
    model_cfg = load_config(args.config, section="model")
    pred_cfg = load_config(args.config, section=args.config_section)
    args.replay_dir = args.replay_dir or atari_cfg.get("replay_dir", pred_cfg.get("replay_dir"))
    args.fsq_checkpoint = args.fsq_checkpoint or pred_cfg.get("fsq_checkpoint")
    args.predictor_checkpoint = args.predictor_checkpoint or str(
        Path(pred_cfg.get("checkpoint_dir")) / "predictor_best.pt")
    args.context_frames = args.context_frames or int(model_cfg.get("context_frames", 4))

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    replay = load_replay_arrays(args.replay_dir)
    fsq = FSQVAE(
        img_channels=int(fsq_cfg.get("img_channels", 3)),
        levels=fsq_cfg.get("levels", [8, 5, 5, 5]),
        norm_type=fsq_cfg.get("norm_type", "group"),
        latent_grid=int(fsq_cfg.get("latent_grid", 16)),
    ).to(device)
    fsq.load_state_dict(load_clean_state(args.fsq_checkpoint, device))
    fsq.eval()

    world_model = WorldModel(
        vocab_size=int(model_cfg.get("vocab_size", 1000)),
        n_actions=int(pred_cfg.get("n_actions", 6)),
        embed_dim=int(model_cfg.get("embed_dim", 384)),
        n_heads=int(model_cfg.get("n_heads", 8)),
        n_layers=int(model_cfg.get("n_layers", 8)),
        context_frames=int(args.context_frames),
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

    starts = valid_starts(replay, args.context_frames, args.horizon, args.start_mode, args.seed)
    starts = starts[:args.episodes]
    episodes = []
    for idx, start in enumerate(starts, start=1):
        print(f"building dream {idx}/{len(starts)} start={int(start)}")
        episodes.append(build_episode(predictor, fsq, replay, int(start), args, device))
    write_html(episodes, Path(args.out))
    print(f"Saved {args.out}")


if __name__ == "__main__":
    main()

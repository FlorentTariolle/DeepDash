"""Shard-based Atari replay storage.

The replay format is intentionally simple: append transitions to compressed
NumPy shards and keep aggregate counters in metadata.json. Each row is one
Atari env step after preprocessing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np


SHARD_PREFIX = "shard_"
METADATA_NAME = "metadata.json"


def shard_paths(replay_dir):
    return sorted(Path(replay_dir).glob(f"{SHARD_PREFIX}*.npz"))


def load_metadata(replay_dir):
    path = Path(replay_dir) / METADATA_NAME
    if not path.exists():
        return None
    with open(path, "r") as f:
        return json.load(f)


def load_shard(path):
    with np.load(path) as data:
        return {key: data[key] for key in data.files}


@dataclass
class ReplayBatch:
    obs: np.ndarray
    actions: np.ndarray
    rewards: np.ndarray
    dones: np.ndarray
    episode_ids: np.ndarray


class ReplayShardWriter:
    """Append Atari transitions to fixed-size compressed NumPy shards."""

    def __init__(self, replay_dir, shard_size=8192, metadata=None):
        self.replay_dir = Path(replay_dir)
        self.replay_dir.mkdir(parents=True, exist_ok=True)
        self.shard_size = int(shard_size)
        if self.shard_size <= 0:
            raise ValueError("shard_size must be positive")

        existing = load_metadata(self.replay_dir)
        if existing is None:
            if metadata is None:
                metadata = {}
            existing = {
                **metadata,
                "format": "sls-wm-atari-replay-v1",
                "shard_size": self.shard_size,
                "total_steps": 0,
                "total_episodes": 0,
                "next_episode_id": 0,
                "next_shard_idx": 0,
                "shards": [],
            }
        else:
            if int(existing.get("shard_size", self.shard_size)) != self.shard_size:
                raise ValueError(
                    f"existing replay shard_size={existing.get('shard_size')} "
                    f"does not match requested shard_size={self.shard_size}"
                )
            if metadata:
                for key, value in metadata.items():
                    existing.setdefault(key, value)

        self.metadata = existing
        self._obs = []
        self._actions = []
        self._rewards = []
        self._dones = []
        self._episode_ids = []

    @property
    def next_episode_id(self):
        return int(self.metadata["next_episode_id"])

    @property
    def total_steps(self):
        return int(self.metadata["total_steps"]) + len(self._actions)

    def append_episode(self, frames, actions, rewards, dones, episode_id=None):
        frames = np.asarray(frames, dtype=np.uint8)
        actions = np.asarray(actions, dtype=np.int16)
        rewards = np.asarray(rewards, dtype=np.float32)
        dones = np.asarray(dones, dtype=bool)
        if not (len(frames) == len(actions) == len(rewards) == len(dones)):
            raise ValueError("frames/actions/rewards/dones must have the same length")

        if episode_id is None:
            episode_id = self.next_episode_id
        episode_ids = np.full(len(actions), int(episode_id), dtype=np.int32)

        for i in range(len(actions)):
            self._obs.append(frames[i])
            self._actions.append(actions[i])
            self._rewards.append(rewards[i])
            self._dones.append(dones[i])
            self._episode_ids.append(episode_ids[i])
            if len(self._actions) >= self.shard_size:
                self.flush()

        self.metadata["total_episodes"] = int(self.metadata["total_episodes"]) + 1
        self.metadata["next_episode_id"] = max(
            int(self.metadata["next_episode_id"]), int(episode_id) + 1)
        self._write_metadata()
        return int(episode_id)

    def flush(self):
        if not self._actions:
            return None
        shard_idx = int(self.metadata["next_shard_idx"])
        name = f"{SHARD_PREFIX}{shard_idx:06d}.npz"
        path = self.replay_dir / name

        batch = ReplayBatch(
            obs=np.stack(self._obs).astype(np.uint8),
            actions=np.asarray(self._actions, dtype=np.int16),
            rewards=np.asarray(self._rewards, dtype=np.float32),
            dones=np.asarray(self._dones, dtype=bool),
            episode_ids=np.asarray(self._episode_ids, dtype=np.int32),
        )
        np.savez_compressed(
            path,
            obs=batch.obs,
            actions=batch.actions,
            rewards=batch.rewards,
            dones=batch.dones,
            episode_ids=batch.episode_ids,
        )

        steps = int(len(batch.actions))
        self.metadata["total_steps"] = int(self.metadata["total_steps"]) + steps
        self.metadata["next_shard_idx"] = shard_idx + 1
        self.metadata["shards"].append({"file": name, "steps": steps})
        self._obs.clear()
        self._actions.clear()
        self._rewards.clear()
        self._dones.clear()
        self._episode_ids.clear()
        self._write_metadata()
        return path

    def close(self):
        return self.flush()

    def _write_metadata(self):
        path = self.replay_dir / METADATA_NAME
        tmp_path = path.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(self.metadata, f, indent=2)
        tmp_path.replace(path)


def load_replay_arrays(replay_dir):
    """Load all replay shards into memory for local training prototypes."""
    paths = shard_paths(replay_dir)
    if not paths:
        raise FileNotFoundError(f"no replay shards found in {replay_dir}")

    obs, actions, rewards, dones, episode_ids = [], [], [], [], []
    for path in paths:
        shard = load_shard(path)
        obs.append(shard["obs"])
        actions.append(shard["actions"])
        rewards.append(shard["rewards"])
        dones.append(shard["dones"])
        episode_ids.append(shard["episode_ids"])

    return ReplayBatch(
        obs=np.concatenate(obs, axis=0),
        actions=np.concatenate(actions, axis=0),
        rewards=np.concatenate(rewards, axis=0),
        dones=np.concatenate(dones, axis=0),
        episode_ids=np.concatenate(episode_ids, axis=0),
    )

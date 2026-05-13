# Atari Controller Handoff

Date: 2026-05-13

## Context

The full Atari Pong H200 experiment completed through the full cycle:

- FSQ tokenizer training
- CE predictor training
- SLS predictor training
- real actor collection/training phases
- dream actor PPO phases
- final actor evaluation

The visual Transformer rollout HTMLs were inspected qualitatively by Florent and judged semantically solid: predicted reconstructions match ground truth well enough that the world model is not the current bottleneck.

## What Was Added

Commit `7876caaf` added controller diagnostics:

- `scripts/train_atari_actor_dream.py`
  - periodic deterministic real-env evaluation during dream PPO
  - `real_eval_mean_return`, `real_eval_mean_length`, and action histograms in `actor_dream_log.csv`
  - `actor_dream_best_real.pt` checkpoint selected by real eval
- `scripts/eval_atari_actor.py`
  - action histograms printed per episode and written to JSON
- `scripts/train_atari_full_cycle.py`
  - selected checkpoint prefers `actor_dream_best_real.pt` when present
- `configs/atari/atari_pong_h200.yaml`
  - dream actor real eval every 100 iterations over 5 episodes
  - final evaluation points to `actor_dream_best_real.pt`

Commit `035cc777` added:

- `slurm/atari_train_actor_dream_bestreal.sl`
  - one-command A100 20GB MIG launcher for the best-real dream actor rerun

## Tests Run

### Full original run

Artifacts analyzed locally from `sls-wm-analysis/`:

- `fsq_log.csv`
- `predictor_ce_log.csv`
- `predictor_sls_log.csv`
- `actor_dream_log.csv`
- `evaluation.json`
- `summary.json`
- rollout HTMLs for CE and SLS

Original final evaluation:

```text
mean_return = -20.0
returns = [-20.0 repeated 20 times]
lengths = [843 repeated 20 times]
```

Conclusion: final deployed controller was degenerate in real Pong.

### CE vs SLS world model metrics

Final CE:

```text
val_acc          0.897336
val_fsq_l1_dist  0.230011
rollout_15_l1    0.001599
```

Final SLS:

```text
val_acc          0.894252
val_fsq_l1_dist  0.235955
rollout_15_l1    0.001605
```

Best SLS rollout checkpoint:

```text
epoch 5 rollout_15_l1 = 0.001574
```

Conclusion: CE and SLS are close on this run. SLS has a tiny best-checkpoint rollout-L1 edge, but not a decisive downstream win.

### Best-real dream actor rerun

Launched with:

```bash
sbatch slurm/atari_train_actor_dream_bestreal.sl
```

Pulled artifacts:

- `actor_dream_log_bestreal.csv`
- `atari_train_actor_dream_bestreal.out`
- `atari_train_actor_dream_bestreal.err`
- `evaluation_best_real_3ep.json`

Real eval probes during dream PPO:

```text
iteration 1    real_eval_mean_return = -21.0
iteration 100  real_eval_mean_return = -21.0
iteration 200  real_eval_mean_return = -21.0
...
iteration 1000 real_eval_mean_return = -21.0
```

Clean 3-episode best-real eval:

```text
returns = [-21.0, -21.0, -21.0]
lengths = [826, 826, 826]
```

Dream training still improved in-dream:

```text
best rolling 100 dream return ~= +0.132 at iter 957
best rolling 50 dream return  ~= +0.155 at iter 905
```

Conclusion: dream return and real return are decoupled. Checkpoint selection is not the issue.

### Real actor checkpoint eval

Ran:

```bash
python scripts/eval_atari_actor.py \
  --config configs/atari/atari_pong_h200.yaml \
  --actor-checkpoint checkpoints_atari_pong_h200_actor_real/actor_real_latest.pt \
  --output runs/atari_pong_h200_full/evaluation_actor_real_latest.json \
  --n-episodes 3
```

Result:

```text
returns = [-21.0, -21.0, -21.0]
lengths = [824, 824, 824]
actions = {'RIGHT': 72, 'LEFT': 894, 'RIGHTFIRE': 1506}
```

Conclusion: the real actor also fails identically. The bug is not dream PPO alone.

## Current Conclusion

The FSQ and visual world model are not the immediate blockers. The controller path is broken below the dream-vs-real checkpoint selection level.

Most likely suspects:

1. ALE action semantics/control mapping are wrong for the policy.
2. Real-eval feature stream differs from the feature stream used during actor training.
3. PPO actor update collapses into deterministic losing policies.
4. Dream reward/control dynamics are exploitable and do not transfer.

The policy is not a single-action collapse: action histograms are diverse. But trajectories are deterministic and consistently lose, which points to a systematic interface/control issue.

## Next Step

Run cheap action baselines before training anything else:

```bash
python - <<'PY'
import gymnasium as gym
import ale_py
import numpy as np

gym.register_envs(ale_py)

env = gym.make("ALE/Pong-v5", frameskip=4, repeat_action_probability=0.0)
print("n_actions", env.action_space.n)
print("meanings", env.unwrapped.get_action_meanings())

for action in range(env.action_space.n):
    returns = []
    lengths = []
    for ep in range(3):
        obs, _ = env.reset(seed=1000 + ep)
        total = 0.0
        steps = 0
        done = False
        while not done and steps < 27000:
            obs, reward, terminated, truncated, _ = env.step(action)
            total += reward
            steps += 1
            done = terminated or truncated
        returns.append(total)
        lengths.append(steps)
    print(action, returns, lengths)

rng = np.random.default_rng(42)
returns = []
lengths = []
for ep in range(3):
    obs, _ = env.reset(seed=2000 + ep)
    total = 0.0
    steps = 0
    done = False
    while not done and steps < 27000:
        action = int(rng.integers(0, env.action_space.n))
        obs, reward, terminated, truncated, _ = env.step(action)
        total += reward
        steps += 1
        done = terminated or truncated
    returns.append(total)
    lengths.append(steps)
print("random", returns, lengths)
env.close()
PY
```

Interpretation:

- If all constant actions and random are near `-21`, check env setup/action set.
- If random is better than `-21`, trained actor/control is broken.
- If one constant action is much better, action semantics or paddle-side control are likely wrong.


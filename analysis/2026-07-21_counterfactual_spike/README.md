# Counterfactual spike rollout

This artifact isolates action conditioning from a shared generated world-model
context. The four source display frames are frames 171--174 of the recorded
human-controlled dream rollout. Frame 174 is the branch point: its context
action conditions the first predicted frame at `t+1`.

The Pygame HUD was added after model decoding. The generator recovers the
64x64 display pixels, blanks the HUD-covered top-left region (rows 0--3,
columns 0--31), and re-encodes the same cleaned context for both branches.

## Reproduce

From the repository root:

```powershell
python scripts/gen_counterfactual_spike.py
```

The script uses greedy decoding with the selected FSQ and transformer
checkpoints. The jump context actions are `[0, 0, 0, 1]`; the idle actions are
`[0, 0, 0, 0]`. All later actions are idle. A branch terminates immediately
after its first prediction with `p(death) > 0.5`.

## Result

- The jump branch runs for 13 predictions, clears the spike, and lands. Its
  maximum death probability is 0.069.
- The idle branch terminates on prediction 5 with `p(death) = 0.742`.
- The paper figure displays every second jump prediction. It also retains the
  terminal idle frame and uses `DEAD` placeholders for frames that are not
  generated after termination.

`metadata.json` records checkpoint and source-frame SHA-256 hashes, exact
actions, raw death probabilities, and output paths. The `jump/`, `idle/`, and
`shared_context/` directories contain every frame actually generated or used.

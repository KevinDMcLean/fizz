# Strategy: Extreme Momentum Engine

## Purpose

This is the rare-event follower. It should trade infrequently and only when oil or another market is clearly in a major directional shock.

## Behaviour

- waits for very large moves;
- hands off normal tape to the mid-momentum engine;
- aims to capture the truly outsized runs;
- uses trailing logic instead of a tight profit cap.

## Run command

```bash
python3 scripts/run_extreme_momentum.py --market brent --account paper_default --dashboard
```


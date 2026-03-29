# Strategy: Vector Momentum Engine

## Purpose

This is the middle sleeve between passive market making and rare-event chasing.

It is designed to:

- wait for a properly confirmed break;
- risk a smaller starter first;
- add as the move proves itself;
- reduce extra size as the move decays;
- keep a runner on via the trailing stop.

## Current design

- confirmed breakout-only entry;
- smaller starter notional than the failed tactical version;
- add-ons only after real follow-through;
- staged de-risking once the move slows;
- no fixed upside cap.

## Use case

This is the strategy to run when the market is active enough for directional continuation but not so extreme that the rare-event engine should take over.

## Run command

```bash
python3 scripts/run_mid_momentum.py --market brent_runner --account paper_default --dashboard
```


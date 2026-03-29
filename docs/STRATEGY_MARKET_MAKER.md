# Strategy: Atlas Liquidity Engine

## Purpose

This is a fee-aware passive market maker. It tries to collect spread and microstructure edge while avoiding toxic one-way tape.

## How it behaves

- posts passively near the touch or wider;
- widens or stands down when volatility, toxicity, or latency worsen;
- prefers maker flow;
- aggressively flattens only when the inventory state turns materially bad.

## Best current use

- Brent
- CL
- SP500

These are different market configs, not different codebases.

## What matters operationally

- fees and rebates must be correct;
- maker share should remain high;
- kill cost must be watched closely;
- growth-mode markets are cheap to trade but weak for tier-building volume.

## Run command

```bash
python3 scripts/run_market_maker.py --market brent --account paper_default --dashboard
```


# Strategy: Exhaustion Reversal Engine

## Purpose

This strategy is the balancing sleeve for momentum. It waits for an overstretched move, then only fades it once reversal evidence is visible in flow and book behaviour.

## Behaviour

- identifies exhaustion anchors;
- waits for reversal confirmation;
- cuts quickly if the move re-accelerates;
- uses break-even and trailing logic once the reversal works.

## Run command

```bash
python3 scripts/run_exhaustion_reversal.py --market brent --account paper_default --dashboard
```

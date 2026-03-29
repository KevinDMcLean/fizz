# Kevin Hype Liquidity Engine

## What it is

`Kevin Hype Liquidity Engine` is a dedicated passive-first HYPE market maker built as its own strategy namespace inside this suite.

It is meant to demonstrate a serious operator workflow for Kevin:

- its own core, bot, dashboard, launcher, config, tests, and run tree;
- tighter and faster quoting than the conservative oil market maker;
- clear sizing, inventory, and kill behaviour that can be inspected live from the dashboard.

## Why HYPE was chosen

HYPE is a better weekend demonstration market than Brent or SP500 because the crypto tape stays active when traditional macro products are quieter.

During the build on 29 March 2026, Hyperliquid's official API showed HYPE trading around `39.49`, with roughly `120.1m` day notional volume and about `21.76m` open interest. That is the kind of tape where a passive-first liquidity engine can actually show meaningful sizing, one-way mode, and inventory management in real time.

## What "no costs for now" means

This Kevin strategy intentionally runs in a no-cost demo mode for now.

- maker fees are not modelled;
- taker fees are not modelled;
- rebates are not modelled;
- fee tiers are not shown on the dashboard.

This is deliberately isolated to Kevin's strategy only. John's existing fee-aware market maker and account assumptions are unchanged.

## How it differs from John's existing market maker

Compared with the fee-aware oil market maker, Kevin HYPE is:

- faster sampled;
- quicker to requote;
- willing to quote tighter because costs are deliberately set to zero for the demo;
- more willing to keep trading through a healthy crypto tape rather than standing down early;
- tuned to size from visible HYPE depth rather than using oil-style conservative thresholds.

The safety model is still professional:

- queue aware;
- inventory aware;
- one-way in directional tape;
- flat in clearly toxic tape;
- aggressive flatten only as a kill path.

## How it decides when to quote

The engine combines:

- top-of-book and microprice location;
- short-window impulse;
- short-window aggressive flow;
- multi-level book imbalance;
- quote freshness and transport delay;
- live inventory pressure.

Healthy tape produces two-way quoting. Directional tape can withdraw one side. Clearly toxic or stale tape stands down completely.

## How it sizes

Kevin HYPE sizes from the live book, not from a fixed lot size.

It starts from a base per-side notional, then scales around that with:

- visible depth;
- trade rate;
- current spread quality;
- queue ahead;
- live inventory usage;
- configured per-quote and max-inventory caps.

That makes it noticeably more assertive than the oil market maker while still making it obvious when the strategy is being capped by risk, book shape, or inventory.

## How it handles inventory

Inventory is managed in layers:

- skew the reservation price away from the current inventory;
- reduce the same-side quote as inventory grows;
- move to one-way passive inventory protection when markout or tape conditions deteriorate;
- only flatten aggressively when the adverse markout or hold-time kill logic is reached.

## How it kills bad inventory

The kill path is intentionally a last resort rather than the normal exit.

It triggers when the inventory has both:

- a sufficiently poor markout;
- enough hold time, event stress, or toxic reversal confirmation to justify paying the spread to flatten.

That keeps the strategy passive-first while still giving it a clean path out of bad inventory.

## What the dashboard shows

The Kevin dashboard is designed to explain behaviour rather than just print logs.

It shows:

- run state and current quote mode;
- bid, ask, mid, spread, latency, and freshness;
- inventory, inventory bias, and current stand-down reason;
- live quote prices, sizes, notionals, and queue position;
- buying power, per-quote cap, max inventory, and current cap reason;
- realised and total PnL with an equity curve;
- passive fills, kill fills, closed episodes, realised spread, and markout quality;
- whether the strategy looks healthy or is trading smaller for a reason.

## How to run it

From the repo root:

```bash
python3 scripts/run_kevin_hype.py --market hype --account paper_default --dashboard
```

On Windows PowerShell:

```powershell
python .\scripts\run_kevin_hype.py --market hype --account paper_default --dashboard
```

Use `--dry-run` first if you want to inspect the exact commands and run paths without starting the processes.

## If we later make it live and fee-aware

The clean next steps would be:

- add an optional fee-aware Kevin profile rather than changing the no-cost demo profile;
- add exact tick and lot metadata fetched at startup;
- calibrate the adverse flatten path with live HYPE slippage and realised passive fill quality;
- separate weekday and weekend parameter packs;
- add explicit queue-jump and realised markout diagnostics for live promotion.

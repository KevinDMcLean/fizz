# Kevin Update

## Purpose of this note

This note explains the current state of the standalone Hyperliquid strategy suite, the strategy ideas now in place, the engineering fixes made on top of the original prototypes, and the current reference performance of the long-running Brent and CL liquidity bots.

The suite now lives in its own repo and can be run independently of the older prototype folders.

Repo:

- `https://github.com/jgoodacre71/fizz`

Local standalone path:

- `/Users/johngoodacre/work/hyperliquid-strategy-suite`

## High-level architecture

The suite is now organised into a clean standalone structure:

- `config/` for account assumptions and strategy profiles;
- `scripts/` for launchers;
- `src/` for strategy and dashboard code;
- `docs/` for runbooks and strategy notes;
- `tests/` for regression checks;
- `runs/` for per-run output and manifests.

The important design choice is that different instruments using the same underlying logic are not split into separate codebases unless there is a real strategy difference. That keeps maintenance under control and prevents drift between copies.

## Main engineering changes made

### 1. Standalone repo and launcher structure

The strategies were lifted into a self-contained repo with:

- independent virtual environment support;
- account config files rather than code edits;
- per-run manifests;
- consistent launch commands;
- clearer documentation.

This makes future account switching and future repo moves much cleaner.

### 2. Exact fee handling for Hyperliquid-style paper runs

The market-maker stack now supports:

- market-type scaling such as `hip3_growth`;
- account fee overrides;
- separate maker and taker rates;
- fee-aware target edge and kill logic.

A key bug was fixed in the standalone market-maker stack:

- manual account rates now take precedence and are treated as final effective rates;
- schedule-style `userFees` inputs still scale correctly for market metadata.

That matters because the wrong interpretation materially overstated fee drag.

### 3. Long-run dashboard memory fixes

The dashboards had an operational problem rather than a bot problem:

- they were re-reading entire log files on every refresh;
- browser tabs were polling too frequently;
- hidden tabs were still polling;
- memory use ratcheted up over time.

The new repo dashboards were hardened by:

- summary caching;
- slower refresh cadence;
- visibility-aware polling;
- no overlapping refresh loops.

This is important for multi-day operation.

### 4. New event strategy: Initiator Follower - JG Thesis

A new event-campaign strategy was added for the oil-war thesis.

It is not a standard runner and not a standard extreme mover. It is built around:

- cheap probes;
- confirmation before real size;
- staged campaign promotion;
- aggressive scaling only once the move is proving itself;
- retaining a runner core while peeling peripheral risk as momentum decays.

### 5. New operator variant: Trump Liquidity

A new strategy variant was added based on the successful CL liquidity engine.

This does not change the trading logic. It is an independent copy of the CL market-maker path with:

- a separate launcher;
- a separate run namespace;
- a dedicated dashboard;
- a display-only Trump / Iran headline wire.

The news feed is proof-of-principle only and is not wired into trade decisions.

## Strategy overview

### Atlas Liquidity Engine

This is the main fee-aware passive market-maker.

Configured instruments currently include:

- Brent;
- CL / WTI;
- S&P 500.

Core idea:

- harvest passive spread when the tape is healthy;
- reduce to one-way quoting when flow or book becomes too one-sided;
- go flat when quality degrades;
- work inventory out passively where possible;
- cross only in the kill path when necessary.

Main controls:

- max spread filters;
- max quote-age filters;
- flow imbalance guard;
- book imbalance guard;
- toxicity guard;
- inventory skew;
- passive size caps from visible depth;
- queue-ahead penalty;
- protection mode for stressed inventory;
- event-regime controls;
- dynamic kill floor after costs.

The trading style is conservative by design. Most of the money should come from passive closes, not from taking the market.

### Trump Liquidity

This is an independent CL copy of the Atlas market-maker path.

Purpose:

- preserve the successful CL trading logic;
- give it a cleaner operator-facing dashboard;
- add a headline monitor focused on Trump / Iran context.

The feed currently uses easy public sources for reliability:

- BBC World RSS;
- BBC Middle East RSS;
- Sky News RSS feeds;
- Google News RSS search for Trump / Iran.

Headline wire fields shown:

- time;
- source;
- headline;
- source link.

This is context only for now. It is not yet a trade signal.

### Kevin Hype Liquidity Engine

This is Kevin’s independent HYPE market-maker variant.

It is deliberately kept in its own namespace so it can evolve independently from the main Atlas market-maker. It should be thought of as Kevin’s own application rather than a hidden branch of CL or Brent.

This push deliberately does not modify Kevin’s HYPE-specific code path.

### Vector Momentum Engine

This is the mid-frequency momentum runner.

Core idea:

- require confirmation rather than simply chasing impulse;
- start smaller;
- add only when continuation proves itself;
- peel risk as the move decays;
- keep a runner core for larger continuation.

It is designed for cleaner continuation rather than geopolitical shock tape.

### Extreme Momentum Engine

This is the rare-event momentum sleeve.

Core idea:

- react to large impulse and breakout conditions;
- require stronger short-window confirmation;
- keep risk tighter than a campaign strategy;
- avoid treating every fast move as durable.

It is suitable for abrupt dislocations but is not the same as a campaign accumulator.

### Exhaustion Reversal Engine

This is the fade / reversal sleeve.

Core idea:

- wait for overstretch;
- require reversal confirmation;
- trade against the exhausted move only once structure turns.

This is explicitly not a breakout engine.

### Initiator Follower - JG Thesis

This is the most directional new strategy conceptually.

It is built around the view that the real opportunity is not generic momentum but a major repricing move with many false starts first.

State model:

- neutral;
- tension;
- probe;
- campaign;
- cooldown.

Key controls:

- probe sizing stays cheap;
- side-specific and global probe throttles;
- breakout proof required before campaign promotion;
- staged max notional unlock rather than immediate full deployment;
- larger campaign entry once the move is paying for itself;
- runner core retained while add-ons are peeled sooner;
- stronger tradability gating using spread, quote age, flow, and book conditions.

This is the closest strategy to an event-driven oil campaign trader.

## Current account and cost assumptions in the standalone suite

For the main fee-aware market-maker paths, the paper account currently reflects the low-fee maker-first setting you supplied:

- maker: `0.0029%` = `0.29 bps`
- taker: `0.0086%` = `0.86 bps`
- maker rebate override: `0.0 bps`

That applies cleanly in the corrected new-repo fee-aware market-maker paths.

Important distinction:

- `Trump Liquidity` uses the same fee-aware CL market-maker logic as the Atlas CL engine.
- `Kevin Hype Liquidity Engine` is a no-cost demo variant by design.

## Old long-running reference bots: current performance snapshot

These are the old reference runs still running outside the standalone repo. They are useful for context because they have been running for much longer than the newer standalone runs.

### Brent liquidity reference run

Source logs:

- `/Users/johngoodacre/work/algotrading/atlas-liquidity-feeaware/logs/asterion_trades.csv`
- `/Users/johngoodacre/work/algotrading/atlas-liquidity-feeaware/logs/asterion_fills.csv`

Current snapshot:

- closed episodes: `1,773`
- wins / losses / flat: `1,250 / 523 / 0`
- gross realised PnL: `+439.21`
- net realised PnL: `+255.53`
- fee drag: `183.69`
- fill turnover: about `$6.20m`
- closed turnover: about `$5.85m`
- gross edge: `0.750 bps`
- net edge: `0.436 bps`
- maker notional: about `$5.77m`
- taker notional: about `$191.4k`
- maker share: about `93.18%`
- passive realised PnL: `+464.67`
- kill realised PnL: `-209.14`

Important caveat on Brent:

This old Brent run has mixed fee history in the logs. Earlier sections of the run include the older, much higher fee basis, while the latest fills are on the corrected low-rate basis. That makes Brent’s full-history net line less clean than CL’s as a reference series.

### CL liquidity reference run

Source logs:

- `/Users/johngoodacre/work/algotrading/atlas-liquidity-cl/logs/atlas_cl_trades.csv`
- `/Users/johngoodacre/work/algotrading/atlas-liquidity-cl/logs/atlas_cl_fills.csv`

Current snapshot:

- closed episodes: `2,310`
- wins / losses / flat: `1,566 / 744 / 0`
- gross realised PnL: `+757.20`
- net realised PnL: `+489.59`
- fee drag: `267.61`
- fill turnover: about `$9.30m`
- closed turnover: about `$8.64m`
- gross edge: `0.877 bps`
- net edge: `0.567 bps`
- maker notional: about `$8.73m`
- taker notional: about `$167.8k`
- maker share: about `93.85%`
- passive realised PnL: `+676.24`
- kill realised PnL: `-186.66`

CL is the cleaner reference run because its fee basis in the current long-run logs is consistent with the corrected low-rate setup.

## Interpretation of the two liquidity references

The long-running liquidity references show the same basic pattern:

- the market-maker is making its money the right way, via passive spread capture;
- the main structural drag is still the kill path;
- maker share is high enough for the strategy to make sense operationally;
- the fee basis matters a great deal because the gross edge is not wide.

The CL run is the strongest clean reference in the archive so far.

## Current operator URLs

Standalone repo dashboards:

- Brent MM: `http://127.0.0.1:8785`
- CL MM: `http://127.0.0.1:8786`
- SP500 MM: `http://127.0.0.1:8787`
- Extreme Momentum: `http://127.0.0.1:8790`
- Vector Momentum: `http://127.0.0.1:8791`
- Exhaustion Reversal: `http://127.0.0.1:8792`
- Kevin Hype: `http://127.0.0.1:8795`
- Initiator Follower: `http://127.0.0.1:8796`
- Trump Liquidity: `http://127.0.0.1:8797`

Old reference dashboards still open:

- old Brent MM: `http://127.0.0.1:8771`
- old Brent fee-aware Asterion: `http://127.0.0.1:8775`
- old CL fee-aware Asterion: `http://127.0.0.1:8776`
- old reversal: `http://127.0.0.1:8773`

## What to focus on next

For Kevin, the most important practical things to understand are:

1. The market-maker stack is now stable enough operationally to run as a serious paper-trading suite.
2. CL is the strongest current reference market-maker run in the archive.
3. Brent needs to be interpreted carefully because its old long-run fee history is mixed.
4. Kevin Hype is Kevin’s own strategy namespace and should continue independently.
5. The new event strategy is the big strategic addition for oil-event deployment.
6. The news wire in Trump Liquidity is deliberately display-only first; that is the right way to add external context without polluting the signal stack too early.

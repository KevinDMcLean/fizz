# Hyperliquid Strategy Suite

This folder is a clean, self-contained home for the strategies built in this workspace.

It keeps the strategy code, launch scripts, configs, documentation, and run outputs together so you can:

- run the strategies from one place;
- avoid copying the same strategy into separate codebases just to trade a different instrument;
- switch account assumptions without editing code;
- move the suite into a new repo later without dragging the original prototypes with it.

## What is in this suite

- `src/`: the strategy code copied from the current working versions.
- `scripts/`: clear launch scripts for each strategy.
- `config/`: account files and market profiles.
- `docs/`: runbooks and strategy notes.
- `tests/`: copied unit tests, adjusted to run against this suite.
- `runs/`: per-run output folders created by the launch scripts.

## Strategies included

- `Atlas Liquidity Engine`: fee-aware market maker.
- `Trump Liquidity`: independent CL copy of the fee-aware market maker with a display-only Trump / Iran headline wire.
- `Kevin Hype Liquidity Engine`: no-cost passive-first HYPE market maker tuned for active crypto weekends.
- `Initiator Follower - JG Thesis`: event-campaign Brent breakout engine built for false-alarm-tolerant deployment into the true repricing move.
- `Vector Momentum Engine`: mid-frequency confirmed-runner momentum trader.
- `Extreme Momentum Engine`: rare-event momentum engine.
- `Exhaustion Reversal Engine`: fade of overstretched moves after reversal confirmation.

## Instruments currently configured

- Market maker:
  - Brent: `brent`
  - CL / WTI: `cl`
  - S&P 500: `sp500`
- Trump Liquidity:
  - CL / WTI: `cl`
- Kevin HYPE:
  - HYPE: `hype`
- Mid momentum:
  - Brent runner: `brent_runner`
- Initiator follower:
  - Brent event campaign: `brent_event_campaign`
- Extreme momentum:
  - Brent: `brent`
- Exhaustion reversal:
  - Brent: `brent`

## Quick start

From this folder:

```bash
cd "/Users/johngoodacre/work/hyperliquid-strategy-suite"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows PowerShell:

```powershell
cd "$HOME\\work\\trading\\fizz"
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
```

Run the fee-aware market maker on Brent:

```bash
python3 scripts/run_market_maker.py --market brent --account paper_default --dashboard
```

Run Kevin Hype Liquidity Engine on HYPE:

```bash
python3 scripts/run_kevin_hype.py --market hype --account paper_default --dashboard
```

Run Trump Liquidity on CL:

```bash
python3 scripts/run_trump_liquidity.py --market cl --account paper_default --dashboard
```

Run the mid-momentum runner on Brent:

```bash
python3 scripts/run_mid_momentum.py --market brent_runner --account paper_default --dashboard
```

Run the initiator-follower event campaign engine on Brent:

```bash
python3 scripts/run_initiator_follower.py --market brent_event_campaign --account paper_default --dashboard
```

Run the extreme mover on Brent:

```bash
python3 scripts/run_extreme_momentum.py --market brent --account paper_default --dashboard
```

Run the exhaustion reversal engine on Brent:

```bash
python3 scripts/run_exhaustion_reversal.py --market brent --account paper_default --dashboard
```

Use `--dry-run` to print the exact commands and paths without starting anything.

## Account changes

The suite is designed so that account changes are config changes, not code changes.

Read:

- [Account And Repo Switching](docs/ACCOUNT_AND_REPO_SWITCHING.md)
- [Runbook](docs/RUNBOOK.md)
- [Kevin Hype Strategy Note](docs/STRATEGY_KEVIN_HYPE.md)
- [Trump Liquidity Strategy Note](docs/STRATEGY_TRUMP_LIQUIDITY.md)
- [Initiator Follower Strategy Note](docs/STRATEGY_INITIATOR_FOLLOWER_JG_THESIS.md)

## Notes

- The launch scripts create a fresh run folder each time under `runs/`.
- Each run folder includes a `run_manifest.json` with the merged account and market config used for that run.
- The market-maker codebase is shared across Brent, CL, and SP500. They are different configs, not different strategy codebases.
- `Trump Liquidity` is deliberately kept in its own namespace under `src/`, `config/`, `scripts/`, and `runs/`.
- `Kevin Hype Liquidity Engine` is deliberately kept in its own namespace under `src/`, `config/`, `scripts/`, and `runs/`.
- `Initiator Follower - JG Thesis` is deliberately kept in its own namespace under `src/`, `config/`, `scripts/`, and `runs/`.
- If the local `.venv/` exists, the launchers use it automatically.
- The launchers also inject the local certificate bundle automatically so the suite works cleanly on macOS without depending on the old repo environment.

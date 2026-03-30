# Runbook

## Philosophy

This suite is organised to make day-to-day operation explicit:

- choose an account config;
- choose a market profile;
- launch the strategy from `scripts/`;
- inspect the dashboard and the run manifest in `runs/`.

The suite is designed to run from its own folder and its own virtual environment. If `.venv/` exists, the launchers use it automatically.

## Launch commands

### Market maker

```bash
python3 scripts/run_market_maker.py --market brent --account paper_default --dashboard
python3 scripts/run_market_maker.py --market cl --account paper_default --dashboard
python3 scripts/run_market_maker.py --market sp500 --account paper_default --dashboard
```

### Trump Liquidity

```bash
python3 scripts/run_trump_liquidity.py --market cl --account paper_default --dashboard
```

### Kevin HYPE

```bash
python3 scripts/run_kevin_hype.py --market hype --account paper_default --dashboard
```

### Mid momentum

```bash
python3 scripts/run_mid_momentum.py --market brent_runner --account paper_default --dashboard
```

### Initiator follower

```bash
python3 scripts/run_initiator_follower.py --market brent_event_campaign --account paper_default --dashboard
```

### Extreme momentum

```bash
python3 scripts/run_extreme_momentum.py --market brent --account paper_default --dashboard
```

### Exhaustion reversal

```bash
python3 scripts/run_exhaustion_reversal.py --market brent --account paper_default --dashboard
```

## Common flags

- `--dashboard` / `--no-dashboard`
- `--port 8801`
- `--run-name test_run`
- `--python /path/to/python`
- `--dry-run`

## Environment notes

- The launchers inject the local certificate bundle automatically when it is available in the suite virtual environment.
- That keeps the suite independent of any certificate setup that may exist in an older repo or global Python installation.
- On Windows PowerShell, activate the virtual environment with `.\\.venv\\Scripts\\Activate.ps1`.

## Where outputs go

Every launch creates:

`runs/<strategy>/<market>/<run_name>/`

Typical files:

- `events.jsonl`
- `samples.jsonl`
- `trades.csv`
- `fills.csv` for the market maker
- `fills.csv` for `Kevin Hype Liquidity Engine`
- `markouts.jsonl` for momentum and reversal strategies
- `reports/`
- `run_manifest.json`

The manifest is the first file to inspect if you want to know exactly what config was used.

## How to stop a run

If you started a strategy from one of the suite launch scripts, press `Ctrl-C` in that terminal. The launcher sends an interrupt to the bot and the dashboard and allows them to shut down cleanly.

## Suggested workflow

1. Run `--dry-run` first if you are changing account or market settings.
2. Start the live paper run.
3. Open the dashboard URL printed by the launcher.
4. Inspect `run_manifest.json` if anything looks surprising.
5. Keep notes against the run folder rather than editing the strategy files immediately.

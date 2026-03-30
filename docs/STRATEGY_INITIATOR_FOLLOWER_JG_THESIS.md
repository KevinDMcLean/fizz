# Strategy: Initiator Follower - JG Thesis

## Purpose

This strategy is designed for unstable macro repricing regimes where a large move is expected, but the path there is likely to include repeated false alarms first.

It is not a standard mid-momentum runner and it is not a late rare-event chaser.

It is built to:

- detect tension before the real break;
- use small, cheap probes close to the boundary;
- cut probes quickly when the move does not survive the book's defensive response;
- promote rapidly into campaign mode once the break proves itself;
- scale hard into the true move;
- keep a large runner core until the actual structure fails.

## Thesis link

The design follows the initiator-follower wedge in the JG thesis.

The practical reading is:

- the best money is often made by the side that is early to the break, not by the reactive follower;
- follower feasibility deteriorates exactly when informational flow becomes concentrated;
- the strategy therefore needs a decision-time loss-risk gate, not just a directional signal;
- repeated small losses are acceptable if they buy the right to deploy heavily into the true campaign move.

## How it differs from the existing momentum sleeves

- `Vector Momentum Engine` is a cleaner continuation runner and becomes too polite once tape becomes genuinely extreme.
- `Extreme Momentum Engine` waits for a larger impulse and can end up arriving too late or too small once volatility has already exploded.
- `Initiator Follower - JG Thesis` sits in between:
  - earlier than the extreme sleeve;
  - more aggressive than the mid runner;
  - much more explicit about probe-versus-campaign behaviour.

## States

- `cold`: nothing useful to do.
- `tension`: unstable equilibrium near a possible break.
- `probe_setup` / `probe_live`: small initial risk, designed to lose small if the move is fake.
- `campaign_setup` / `campaign_live`: confirmed break, larger deployment, more patient holding logic.
- `cooldown`: pause after exit to avoid reflexive re-entry into the same burst.

## Controls

The main controls are:

- quote-age and spread guardrails before any trade is considered;
- a loss-risk gate using spread, quote age, trade rate, and depth quality;
- per-side probe failure throttles to stop repeated bleeding into the same false break;
- regime-native entry handling so the strategy can arm and enter directly from `tension` and `campaign` states rather than relying on the older momentum sleeve's `active` regime;
- automatic promotion of a winning probe into campaign management before the first add-on, so a true winner is not still managed on short probe rails;
- smaller starter notional for probe entries than for campaign entries;
- aggressive add-ons only after open profit and follow-through justify it;
- staged de-risking of add-on size while preserving a larger core;
- larger breakout-failure tolerance once the trade has become a true campaign hold;
- standard daily loss, trade-count, cooldown, and stale-feed controls.

## Current Brent event profile

The included Brent event profile is intentionally more aggressive than the existing runner while being materially harder to fool:

- account balance assumption: `2500`
- leverage assumption: `20x`
- starter probe cap: `10%` of buying power
- direct campaign entry cap: `45%` of buying power
- total notional cap: `90%` of buying power
- up to `3` add-ons
- runner core floor: `38%` of peak deployed size

This is designed to allow small losses on failed probes while still being capable of deploying heavily into the true move.

The latest hardening pass adds:

- stricter probe confirmation thresholds and much smaller breakout slack;
- a global probe-loss throttle in addition to the existing per-side throttles;
- book-aware notional caps for probe entries, campaign entries, and add-ons;
- faster stop handling with single-tick confirmation;
- a hard-reversal exit for fast failed breaks;
- realistic slippage and fee assumptions for live deployment review;
- quicker campaign size deployment, followed by faster de-risking once confirmation decays.

## Run command

```bash
python3 scripts/run_initiator_follower.py --market brent_event_campaign --account paper_default --dashboard
```

## Dashboard

The dashboard shows the same execution and runner metrics as the directional momentum stack, but the state labels are interpreted differently:

- `tension` means the market is unstable and close to a break;
- `probe` states mean the engine is testing the boundary with controlled risk;
- `campaign` states mean the engine believes the real move is in progress and is prepared to hold more size.

## Tuning priorities later

The first tuning priorities should be:

- probe loss frequency by side;
- promotion rate from probe to campaign before the first add-on;
- add-on timing and total deployed notional;
- realised capture ratio on large winners;
- average give-back after the move has clearly become a campaign;
- false-alarm cost as a percentage of total gross PnL.

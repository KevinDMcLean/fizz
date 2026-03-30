# Trump Liquidity

`Trump Liquidity` is an independent CL copy of the shared fee-aware liquidity engine.

The trading logic is intentionally the same proven CL market-maker logic. The point of this variant is operational:

- keep the successful CL engine intact;
- give it its own launcher, run namespace, and dashboard;
- add a display-only geopolitical headline wire for Trump and Iran monitoring.

## What is different from Atlas CL

- same bot logic and same account/fee handling as the CL market-maker;
- separate launcher: `scripts/run_trump_liquidity.py`;
- separate run output under `runs/trump_liquidity/`;
- separate dashboard branding and layout;
- display-only news panel showing headline time, source, and title.

## News feed

The headline panel is deliberately lightweight and proof-of-principle only.

It uses easy public feeds rather than brittle scraping:

- BBC World RSS
- BBC Middle East RSS
- Sky News Home RSS
- Sky News World RSS
- Google News RSS search for Trump / Iran

This is not wired into quoting, inventory, or risk decisions. It is context only.

Truth Social, X, and other direct Trump-post feeds are better treated later as a separate authenticated integration or carefully managed scraper. They are not needed for the proof-of-principle.

## Run it

```bash
python3 scripts/run_trump_liquidity.py --market cl --account paper_default --dashboard
```

Default dashboard:

`http://127.0.0.1:8797`

## What the dashboard shows

- the same CL market-making metrics as the main Atlas liquidity view;
- a tidier top section with more useful run-state information;
- a headline wire with time, source, title, and source link;
- no signal coupling to the strategy.

## What to improve later

- move from static keyword filters to topic and entity scoring;
- add direct social feeds if a robust source becomes available;
- add sentiment or escalation tagging;
- wire the news layer into a separate decision overlay only after it is reliable enough.

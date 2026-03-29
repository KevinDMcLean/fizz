# Account And Repo Switching

## Why this exists

You said you expect to use a different account and later move into a different repo to work on the strategies in more depth.

This suite is organised so that:

- the strategy logic lives in `src/`;
- account-specific assumptions live in `config/accounts/`;
- market selection lives in `config/<strategy>/`;
- each run stores its exact merged inputs in `run_manifest.json`.

## How to switch account

1. Copy:

`config/accounts/account_template.json`

2. Rename it, for example:

`config/accounts/live_research_account.json`

3. Edit these fields:

- `account_balance`
- `leverage`
- `api_url`
- `dex`
- `account_address`
- `vault_address`
- `maker_fee_pct_override`
- `taker_fee_pct_override`
- `maker_rebate_bps_override`

## Which fields matter most

### For all strategies

- `account_balance`: the paper balance assumption.
- `leverage`: the leverage assumption.
- `api_url`: Hyperliquid endpoint.
- `dex`: exchange namespace such as `xyz`.

### Especially for the market maker

- `maker_fee_pct_override`
- `taker_fee_pct_override`
- `maker_rebate_bps_override`
- `account_address` or `vault_address`

These drive the fee-aware market maker’s net-cost view. If these are wrong, the dashboard fee panel will be wrong.

## How to switch repo later

When you move to a new repo:

1. copy the whole `hyperliquid-strategy-suite/` folder;
2. create a fresh virtual environment there;
3. install `requirements.txt`;
4. point your new account config at the right account and fee inputs;
5. keep the old `runs/` folder if you want the historical manifests and logs.

## What you should not edit first

Do not start by editing strategy code just to change:

- account balance;
- fees;
- rebates;
- addresses;
- instrument symbol;
- dashboard port.

Those should be config changes.


#!/usr/bin/env python3
"""Run the Trump Liquidity CL market maker from the standalone suite."""

from __future__ import annotations

import argparse
import json

from _launcher_common import (
    ROOT,
    default_python_executable,
    load_json,
    make_run_paths,
    run_managed,
    timestamped_run_name,
    write_manifest,
)

BOT_SKIP_KEYS = {
    "strategy_name",
    "market_name",
    "description",
    "dashboard_port",
    "dashboard_title",
    "dashboard_subtitle",
    "news_keywords",
    "news_sources",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Trump Liquidity from this suite")
    parser.add_argument("--market", default="cl", help="Market profile name, for example cl")
    parser.add_argument("--account", default="paper_default", help="Account config name")
    parser.add_argument("--python", default=default_python_executable(), help="Python interpreter to use")
    parser.add_argument("--run-name", default=None, help="Optional run directory name")
    parser.add_argument("--port", type=int, default=None, help="Optional dashboard port override")
    parser.add_argument("--dashboard", action=argparse.BooleanOptionalAction, default=True, help="Run the dashboard alongside the bot")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without starting processes")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    account_file = ROOT / "config" / "accounts" / f"{args.account}.json"
    profile_file = ROOT / "config" / "trump_liquidity" / f"{args.market}.json"
    account_config = load_json(account_file)
    profile_config = load_json(profile_file)
    run_name = args.run_name or timestamped_run_name(args.market)
    paths = make_run_paths(strategy_slug="trump_liquidity", market_slug=args.market, run_name=run_name, include_fills=True)

    bot_config = {}
    for key in ("account_balance", "leverage", "api_url", "dex"):
        if key in account_config:
            bot_config[key] = account_config[key]
    for key, value in profile_config.items():
        if key not in BOT_SKIP_KEYS:
            bot_config[key] = value
    if account_config.get("account_address") or account_config.get("vault_address"):
        bot_config["fee_user_address"] = account_config.get("vault_address") or account_config.get("account_address")
    if account_config.get("maker_fee_pct_override") is not None:
        bot_config["fee_user_maker_rate_pct"] = account_config["maker_fee_pct_override"]
    if account_config.get("taker_fee_pct_override") is not None:
        bot_config["fee_user_taker_rate_pct"] = account_config["taker_fee_pct_override"]
    if account_config.get("maker_rebate_bps_override") is not None:
        bot_config["fee_maker_rebate_bps_override"] = account_config["maker_rebate_bps_override"]
    bot_config.update(
        {
            "events_jsonl": str(paths["events_jsonl"]),
            "samples_jsonl": str(paths["samples_jsonl"]),
            "fills_csv": str(paths["fills_csv"]),
            "trades_csv": str(paths["trades_csv"]),
            "report_dir": str(paths["reports_dir"]),
        }
    )
    dashboard_port = args.port or profile_config["dashboard_port"]
    dashboard_config = {
        "host": "127.0.0.1",
        "port": dashboard_port,
        "events_jsonl": str(paths["events_jsonl"]),
        "samples_jsonl": str(paths["samples_jsonl"]),
        "fills_csv": str(paths["fills_csv"]),
        "trades_csv": str(paths["trades_csv"]),
        "reports_dir": str(paths["reports_dir"]),
        "strategy_title": profile_config["dashboard_title"],
        "strategy_subtitle": profile_config["dashboard_subtitle"],
        "news_keywords_json": json.dumps(profile_config.get("news_keywords", [])),
        "news_sources_json": json.dumps(profile_config.get("news_sources", [])),
    }
    write_manifest(
        manifest_path=paths["run_dir"] / "run_manifest.json",
        strategy_name=profile_config["strategy_name"],
        market_name=profile_config["market_name"],
        account_name=args.account,
        account_file=account_file,
        profile_file=profile_file,
        bot_script="trump_liquidity_bot.py",
        dashboard_script="trump_liquidity_dashboard.py",
        bot_config=bot_config,
        dashboard_config=dashboard_config,
    )
    return run_managed(
        python_executable=args.python,
        bot_script="trump_liquidity_bot.py",
        bot_config=bot_config,
        dashboard_script="trump_liquidity_dashboard.py",
        dashboard_config=dashboard_config,
        with_dashboard=args.dashboard,
        dry_run=args.dry_run,
        title=f"Starting {profile_config['strategy_name']} for {profile_config['market_name']}",
    )


if __name__ == "__main__":
    raise SystemExit(main())

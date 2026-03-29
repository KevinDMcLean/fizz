#!/usr/bin/env python3
"""Run Kevin Hype Liquidity Engine from this suite."""

from __future__ import annotations

import argparse

from _launcher_common import (
    ROOT,
    base_bot_config,
    default_python_executable,
    load_json,
    make_run_paths,
    run_managed,
    timestamped_run_name,
    write_manifest,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Kevin Hype Liquidity Engine from this suite")
    parser.add_argument("--market", default="hype", help="Kevin HYPE market profile name")
    parser.add_argument("--account", default="paper_default", help="Account config name")
    parser.add_argument("--python", default=default_python_executable(), help="Python interpreter to use")
    parser.add_argument("--run-name", default=None, help="Optional run directory name")
    parser.add_argument("--port", type=int, default=None, help="Optional dashboard port override")
    parser.add_argument(
        "--dashboard",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run the dashboard alongside the bot",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without starting processes")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    account_file = ROOT / "config" / "accounts" / f"{args.account}.json"
    profile_file = ROOT / "config" / "kevin_hype" / f"{args.market}.json"
    account_config = load_json(account_file)
    profile_config = load_json(profile_file)

    run_name = args.run_name or timestamped_run_name(args.market)
    paths = make_run_paths(
        strategy_slug="kevin_hype",
        market_slug=args.market,
        run_name=run_name,
        include_fills=True,
    )

    bot_config = base_bot_config(account_config, profile_config)
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
    }
    write_manifest(
        manifest_path=paths["run_dir"] / "run_manifest.json",
        strategy_name="Kevin Hype Liquidity Engine",
        market_name=profile_config["market_name"],
        account_name=args.account,
        account_file=account_file,
        profile_file=profile_file,
        bot_script="kevin_hype_liquidity_bot.py",
        dashboard_script="kevin_hype_liquidity_dashboard.py",
        bot_config=bot_config,
        dashboard_config=dashboard_config,
    )
    return run_managed(
        python_executable=args.python,
        bot_script="kevin_hype_liquidity_bot.py",
        bot_config=bot_config,
        dashboard_script="kevin_hype_liquidity_dashboard.py",
        dashboard_config=dashboard_config,
        with_dashboard=args.dashboard,
        dry_run=args.dry_run,
        title=f"Starting Kevin Hype Liquidity Engine for {profile_config['market_name']}",
    )


if __name__ == "__main__":
    raise SystemExit(main())

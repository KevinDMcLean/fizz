#!/usr/bin/env python3
"""Summarize the latest Oil Campaign Momentum v2 runs for morning review."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable


ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = ROOT / "runs" / "oil_campaign_momentum_v2"
DEFAULT_MARKETS = ("brent", "wti", "brent_medium", "wti_medium")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize the latest oil momentum runs")
    parser.add_argument(
        "--market",
        action="append",
        dest="markets",
        help="Market slug to summarize (default: brent, wti, brent_medium, wti_medium)",
    )
    parser.add_argument(
        "--root",
        default=str(RUNS_ROOT),
        help="Root directory containing oil momentum run folders",
    )
    return parser.parse_args()


def latest_run_dir(root: Path, market: str) -> Path | None:
    market_dir = root / market
    if not market_dir.exists():
        return None
    dirs = [path for path in market_dir.iterdir() if path.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda path: path.stat().st_mtime)


def read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def read_csv_rows(path: Path) -> list[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def summarize_features(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    candidate_rows = []
    entry_rows = []
    promotion_rows = []
    label_rows = []
    exit_buckets = Counter()
    candidate_profiles = Counter()
    entry_profiles = Counter()
    promoted_count = 0

    for row in rows:
        row_type = str(row.get("row_type") or "")
        if row_type == "candidate":
            candidate_rows.append(row)
            candidate_profiles[str(row.get("entry_profile") or "unknown")] += 1
        elif row_type == "entry":
            entry_rows.append(row)
            entry_profiles[str(row.get("entry_profile") or "unknown")] += 1
        elif row_type == "promotion":
            promotion_rows.append(row)
            promoted_count += 1
        elif row_type == "label":
            label_rows.append(row)
            exit_buckets[str(row.get("exit_bucket") or "unknown")] += 1

    return {
        "candidates": len(candidate_rows),
        "entries": len(entry_rows),
        "promotions": len(promotion_rows),
        "labels": len(label_rows),
        "candidate_profiles": candidate_profiles,
        "entry_profiles": entry_profiles,
        "exit_buckets": exit_buckets,
        "avg_entry_notional_pct_recent_traded": (
            sum(_float(row.get("entry_notional_pct_recent_traded")) for row in entry_rows) / len(entry_rows)
            if entry_rows
            else 0.0
        ),
        "avg_hold_1s_score_edge": (
            sum(_float(row.get("hold_1s_score_edge")) for row in label_rows if row.get("hold_1s_score_edge") is not None)
            / max(1, sum(1 for row in label_rows if row.get("hold_1s_score_edge") is not None))
        ),
    }


def summarize_trades(rows: Iterable[Dict[str, str]]) -> Dict[str, Any]:
    rows = list(rows)
    if not rows:
        return {
            "closed_trades": 0,
            "gross_pnl": 0.0,
            "net_pnl": 0.0,
            "fees_paid": 0.0,
            "wins": 0,
            "losses": 0,
            "avg_hold_seconds": 0.0,
            "avg_net_pnl": 0.0,
            "exit_reasons": Counter(),
        }
    exit_reasons = Counter(row.get("exit_reason") or "unknown" for row in rows)
    net_values = [_float(row.get("realized_pnl")) for row in rows]
    hold_values = [_float(row.get("hold_seconds")) for row in rows]
    return {
        "closed_trades": len(rows),
        "gross_pnl": sum(_float(row.get("gross_pnl")) for row in rows),
        "net_pnl": sum(net_values),
        "fees_paid": sum(_float(row.get("fees_paid")) for row in rows),
        "wins": sum(1 for value in net_values if value > 0),
        "losses": sum(1 for value in net_values if value <= 0),
        "avg_hold_seconds": sum(hold_values) / len(hold_values),
        "avg_net_pnl": sum(net_values) / len(net_values),
        "exit_reasons": exit_reasons,
    }


def top_items(counter: Counter[str], limit: int = 3) -> str:
    if not counter:
        return "none"
    return ", ".join(f"{name}={count}" for name, count in counter.most_common(limit))


def print_market_summary(market: str, run_dir: Path) -> None:
    trades = summarize_trades(read_csv_rows(run_dir / "trades.csv"))
    features = summarize_features(read_jsonl(run_dir / "features.jsonl"))
    print(f"{market}: {run_dir.name}")
    print(f"  run_dir: {run_dir}")
    print(
        "  pnl:"
        f" gross={trades['gross_pnl']:.2f}"
        f" net={trades['net_pnl']:.2f}"
        f" fees={trades['fees_paid']:.2f}"
        f" trades={trades['closed_trades']}"
        f" wins={trades['wins']}"
        f" losses={trades['losses']}"
    )
    print(
        "  flow:"
        f" candidates={features['candidates']}"
        f" entries={features['entries']}"
        f" promotions={features['promotions']}"
        f" labels={features['labels']}"
    )
    print(
        "  profile_mix:"
        f" candidates[{top_items(features['candidate_profiles'])}]"
        f" entries[{top_items(features['entry_profiles'])}]"
    )
    print(
        "  exits:"
        f" buckets[{top_items(features['exit_buckets'])}]"
        f" trade_reasons[{top_items(trades['exit_reasons'])}]"
    )
    print(
        "  diagnostics:"
        f" avg_hold_s={trades['avg_hold_seconds']:.2f}"
        f" avg_net_per_trade={trades['avg_net_pnl']:.2f}"
        f" avg_entry_pct_recent_traded={features['avg_entry_notional_pct_recent_traded']:.2f}%"
        f" avg_hold_1s_score_edge={features['avg_hold_1s_score_edge']:.2f}"
    )
    print("")


def main() -> int:
    args = parse_args()
    root = Path(args.root)
    markets = tuple(args.markets or DEFAULT_MARKETS)
    print(f"Oil overnight summary root: {root}")
    print("")
    missing = []
    for market in markets:
        run_dir = latest_run_dir(root, market)
        if run_dir is None:
            missing.append(market)
            continue
        print_market_summary(market, run_dir)
    if missing:
        print(f"Missing markets: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

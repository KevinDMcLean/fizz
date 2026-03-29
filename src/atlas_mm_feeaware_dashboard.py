#!/usr/bin/env python3
"""Dashboard for the Asterion fee-aware liquidity engine."""

from __future__ import annotations

import argparse
import bisect
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

from hyperliquid_fee_model import HyperliquidFeeConfig, fee_rates_for_tier

APP_NAME = "Asterion Liquidity Engine"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local dashboard for the Asterion oil liquidity engine")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8785)
    parser.add_argument("--events-jsonl", default="logs/asterion_events.jsonl")
    parser.add_argument("--samples-jsonl", default="logs/asterion_samples.jsonl")
    parser.add_argument("--fills-csv", default="logs/asterion_fills.csv")
    parser.add_argument("--trades-csv", default="logs/asterion_trades.csv")
    parser.add_argument("--reports-dir", default="logs/asterion_reports")
    return parser.parse_args()


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
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


def _read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _parse_utc(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _tier_index_from_label(label: Any) -> int | None:
    if not isinstance(label, str):
        return None
    raw = label.strip().lower()
    if not raw.startswith("tier"):
        return None
    try:
        return int(raw[4:])
    except ValueError:
        return None


def _latest(rows: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    return rows[-1] if rows else None


def _filter_since(
    rows: List[Dict[str, Any]],
    *,
    field: str,
    since: datetime | None,
) -> List[Dict[str, Any]]:
    if since is None:
        return rows
    filtered: List[Dict[str, Any]] = []
    for row in rows:
        ts = _parse_utc(row.get(field))
        if ts is not None and ts >= since:
            filtered.append(row)
    return filtered


def _percentile(values: List[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(len(ordered) * q) - 1))
    return round(ordered[idx], 3)


def _max_value(values: List[float]) -> float | None:
    if not values:
        return None
    return round(max(values), 3)


def _timestamp_seconds(raw: Any) -> float | None:
    dt = _parse_utc(raw)
    if dt is None:
        return None
    return dt.timestamp()


def _mean(values: List[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _compute_size_metrics(
    samples: List[Dict[str, Any]],
    fills: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
    *,
    account_balance: float | None,
    leverage: float | None,
) -> Dict[str, Any]:
    buying_power = None
    if account_balance is not None and leverage is not None:
        buying_power = account_balance * leverage

    active_quote_notionals: List[float] = []
    for row in samples:
        for side in ("bid", "ask"):
            price = _to_float(row.get(f"live_{side}_order_price"))
            size = _to_float(row.get(f"live_{side}_order_size"))
            if price and size:
                active_quote_notionals.append(abs(price * size))

    sample_pairs = [
        (_timestamp_seconds(row.get("timestamp_utc")), row)
        for row in samples
        if _timestamp_seconds(row.get("timestamp_utc")) is not None
    ]
    sample_pairs.sort(key=lambda item: item[0])
    sample_keys = [item[0] for item in sample_pairs]

    passive_fills = [row for row in fills if row.get("liquidity_role") == "passive"]
    aggressive_fills = [row for row in fills if row.get("liquidity_role") == "aggressive"]
    kill_fills = [row for row in aggressive_fills if not str(row.get("reason") or "").startswith("finalize")]

    passive_fill_notionals: List[float] = []
    passive_fill_touch_shares: List[float] = []
    passive_fill_queue_ahead: List[float] = []
    for fill in passive_fills:
        fill_price = _to_float(fill.get("price"))
        fill_size = _to_float(fill.get("size"))
        fill_ts = _timestamp_seconds(fill.get("timestamp_utc"))
        if fill_price is None or fill_size is None or fill_ts is None:
            continue
        passive_fill_notionals.append(abs(fill_price * fill_size))
        idx = bisect.bisect_right(sample_keys, fill_ts) - 1
        if idx < 0:
            continue
        sample = sample_pairs[idx][1]
        sample_side = "ask" if str(fill.get("side")) == "SELL" else "bid"
        depth = _to_float(sample.get(f"{sample_side}_depth"))
        queue = _to_float(sample.get(f"{sample_side}_queue_ahead_size"))
        if depth and depth > 0:
            passive_fill_touch_shares.append(abs(fill_size) / depth)
        if queue is not None:
            passive_fill_queue_ahead.append(queue)

    realized = [(_to_float(row.get("realized_pnl")) or 0.0) for row in trades]
    wins = sum(1 for value in realized if value > 0)
    losses = sum(1 for value in realized if value < 0)
    flats = sum(1 for value in realized if value == 0)
    closed_turnover = 0.0
    for row in trades:
        entry_price = _to_float(row.get("entry_price_avg")) or 0.0
        exit_price = _to_float(row.get("exit_price_avg")) or 0.0
        qty = abs(_to_float(row.get("max_abs_qty")) or 0.0)
        closed_turnover += (entry_price + exit_price) * qty

    fill_turnover = sum(abs((_to_float(row.get("price")) or 0.0) * (_to_float(row.get("size")) or 0.0)) for row in fills)
    fill_units = sum(abs(_to_float(row.get("size")) or 0.0) for row in fills)

    return {
        "account_balance": account_balance,
        "leverage": leverage,
        "buying_power": buying_power,
        "active_quote_avg_notional": _mean(active_quote_notionals),
        "active_quote_p95_notional": _percentile(active_quote_notionals, 0.95),
        "active_quote_max_notional": _max_value(active_quote_notionals),
        "passive_fill_avg_notional": _mean(passive_fill_notionals),
        "passive_fill_p95_notional": _percentile(passive_fill_notionals, 0.95),
        "passive_fill_max_notional": _max_value(passive_fill_notionals),
        "passive_fill_avg_buying_power_pct": (_mean(passive_fill_notionals) / buying_power * 100.0) if passive_fill_notionals and buying_power else None,
        "passive_fill_p95_buying_power_pct": (_percentile(passive_fill_notionals, 0.95) / buying_power * 100.0) if passive_fill_notionals and buying_power else None,
        "passive_fill_avg_touch_share_pct": (_mean(passive_fill_touch_shares) * 100.0) if passive_fill_touch_shares else None,
        "passive_fill_p95_touch_share_pct": (_percentile(passive_fill_touch_shares, 0.95) * 100.0) if passive_fill_touch_shares else None,
        "passive_fill_under_10pct_touch_pct": (sum(1 for value in passive_fill_touch_shares if value <= 0.10) / len(passive_fill_touch_shares) * 100.0) if passive_fill_touch_shares else None,
        "passive_fill_under_25pct_touch_pct": (sum(1 for value in passive_fill_touch_shares if value <= 0.25) / len(passive_fill_touch_shares) * 100.0) if passive_fill_touch_shares else None,
        "passive_fill_over_50pct_touch_count": sum(1 for value in passive_fill_touch_shares if value > 0.50),
        "passive_fill_queue_ahead_avg": _mean(passive_fill_queue_ahead),
        "passive_fill_queue_ahead_p95": _percentile(passive_fill_queue_ahead, 0.95),
        "fill_turnover_notional": fill_turnover,
        "fill_turnover_units": fill_units,
        "closed_turnover_notional": closed_turnover,
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate_pct": (wins / len(trades) * 100.0) if trades else 0.0,
        "loss_rate_pct": (losses / len(trades) * 100.0) if trades else 0.0,
        "passive_fill_count": len(passive_fills),
        "aggressive_fill_count": len(aggressive_fills),
        "kill_fill_count": len(kill_fills),
    }


def _build_pnl_series(
    samples: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
    *,
    limit: int = 480,
) -> List[Dict[str, Any]]:
    sorted_samples = sorted(
        samples,
        key=lambda row: _parse_utc(row.get("timestamp_utc")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    sorted_trades = sorted(
        trades,
        key=lambda row: _parse_utc(row.get("close_time_utc")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    trade_idx = 0
    realized_running = 0.0
    gross_realized_running = 0.0
    points: List[Dict[str, Any]] = []
    for sample in sorted_samples:
        sample_ts = _parse_utc(sample.get("timestamp_utc"))
        if sample_ts is None:
            continue
        while trade_idx < len(sorted_trades):
            trade_ts = _parse_utc(sorted_trades[trade_idx].get("close_time_utc"))
            if trade_ts is None or trade_ts > sample_ts:
                break
            gross_realized_running += _to_float(sorted_trades[trade_idx].get("gross_pnl")) or 0.0
            realized_running += _to_float(sorted_trades[trade_idx].get("realized_pnl")) or 0.0
            trade_idx += 1
        unrealized = _to_float(sample.get("inventory_unrealized_pnl")) or 0.0
        points.append(
            {
                "timestamp_utc": sample_ts.isoformat(),
                "gross_realized_pnl": round(gross_realized_running, 8),
                "realized_pnl": round(realized_running, 8),
                "fee_drag_pnl": round(gross_realized_running - realized_running, 8),
                "unrealized_pnl": round(unrealized, 8),
                "total_pnl": round(realized_running + unrealized, 8),
            }
        )
    if len(points) <= limit:
        return points
    step = max(1, len(points) // limit)
    reduced = points[::step]
    if reduced[-1] != points[-1]:
        reduced.append(points[-1])
    return reduced


def _decision_headline(sample: Dict[str, Any]) -> str:
    if not sample:
        return "Waiting for live market data."
    quote_mode = str(sample.get("quote_mode") or "flat")
    quote_reason = str(sample.get("quoting_reason") or "unknown")
    if quote_reason == "inventory_protection":
        return "Working a passive exit only because the held inventory is under pressure."
    if not sample.get("quoting_enabled"):
        if quote_reason == "event_regime":
            return "Standing down because the tape is in event regime."
        if quote_reason == "toxicity_guard":
            return "Standing down because flow, book, or toxicity is too one-way."
        if quote_reason in {"daily_loss_limit", "daily_episode_limit", "hourly_episode_limit", "cooldown"}:
            return f"Standing down because {quote_reason.replace('_', ' ')} is active."
        return f"Standing down because {quote_reason}."
    if quote_mode == "both":
        return "Posting both sides passively to harvest spread in a healthy market."
    if quote_mode == "bid_only":
        return "Posting bid only. The tape is leaning up, so the ask is withdrawn."
    if quote_mode == "ask_only":
        return "Posting ask only. The tape is leaning down, so the bid is withdrawn."
    return "Running passive spread logic."


def _freshness_label(sample: Dict[str, Any], max_quote_age_ms: float | None) -> str:
    quote_age = _to_float(sample.get("quote_age_ms"))
    if quote_age is None or max_quote_age_ms is None:
        return "unknown"
    return "fresh" if quote_age <= max_quote_age_ms else "stale"


def build_summary(
    events_path: Path,
    samples_path: Path,
    fills_path: Path,
    trades_path: Path,
    reports_dir: Path,
) -> Dict[str, Any]:
    events = _read_jsonl(events_path)
    samples = _read_jsonl(samples_path)
    fills = _read_csv(fills_path)
    trades = _read_csv(trades_path)

    starts = [row for row in events if row.get("event") == "bot_started"]
    stops = [row for row in events if row.get("event") == "bot_stopped"]
    last_started = _latest(starts)
    last_started_ts = _parse_utc(last_started.get("timestamp_utc")) if last_started else None

    current_events = [
        row
        for row in events
        if row.get("event") not in {"market_tick"}
        and (
            last_started_ts is None
            or (_parse_utc(row.get("timestamp_utc")) or datetime.min.replace(tzinfo=timezone.utc))
            >= last_started_ts
        )
    ]
    current_samples = _filter_since(samples, field="timestamp_utc", since=last_started_ts)
    current_fills = _filter_since(fills, field="timestamp_utc", since=last_started_ts)
    current_trades = _filter_since(trades, field="open_time_utc", since=last_started_ts)

    latest_sample = _latest(current_samples) or _latest(samples) or {}
    latest_fill = _latest(current_fills) or {}

    run_state = "unknown"
    if last_started is not None:
        run_state = "running"
        for stop in reversed(stops):
            stop_ts = _parse_utc(stop.get("timestamp_utc"))
            if stop_ts is not None and last_started_ts is not None and stop_ts >= last_started_ts:
                run_state = "stopped"
                break

    quote_ages = [
        value
        for value in (_to_float(row.get("quote_age_ms")) for row in current_samples)
        if value is not None
    ]
    transports = [
        value
        for value in (_to_float(row.get("transport_delay_ms")) for row in current_samples)
        if value is not None
    ]
    spreads = [
        value
        for value in (_to_float(row.get("spread_bps")) for row in current_samples)
        if value is not None
    ]

    passive_fills = [row for row in current_fills if row.get("liquidity_role") == "passive"]
    aggressive_fills = [row for row in current_fills if row.get("liquidity_role") == "aggressive"]
    kill_fills = [row for row in aggressive_fills if not str(row.get("reason") or "").startswith("finalize")]

    realized_pnl_total = sum((_to_float(row.get("realized_pnl")) or 0.0) for row in current_trades)
    inventory_unrealized = _to_float(latest_sample.get("inventory_unrealized_pnl")) or 0.0
    total_pnl = realized_pnl_total + inventory_unrealized

    wins = sum(1 for row in current_trades if (_to_float(row.get("realized_pnl")) or 0.0) > 0)
    win_rate_pct = (wins / len(current_trades) * 100.0) if current_trades else 0.0
    avg_hold_seconds = (
        sum((_to_float(row.get("hold_seconds")) or 0.0) for row in current_trades) / len(current_trades)
        if current_trades
        else 0.0
    )
    avg_best_markout = (
        sum((_to_float(row.get("best_markout_bps")) or 0.0) for row in current_trades) / len(current_trades)
        if current_trades
        else 0.0
    )

    realized_spreads: List[float] = []
    for row in current_trades:
        realized = _to_float(row.get("realized_pnl"))
        entry_price = _to_float(row.get("entry_price_avg"))
        size = _to_float(row.get("max_abs_qty"))
        if realized is None or entry_price in (None, 0.0) or size in (None, 0.0):
            continue
        entry_notional = entry_price * size
        if entry_notional > 0:
            realized_spreads.append((realized / entry_notional) * 10_000.0)
    avg_realized_spread = sum(realized_spreads) / len(realized_spreads) if realized_spreads else 0.0

    quote_mode_counts = Counter(str(row.get("quote_mode") or "unknown") for row in current_samples)
    quote_reason_counts = Counter(str(row.get("quoting_reason") or "unknown") for row in current_samples)

    latest_report = None
    if reports_dir.exists():
        reports = sorted(reports_dir.glob("*.md"))
        if reports:
            latest_report = str(reports[-1])

    max_quote_age_ms = _to_float(last_started.get("max_quote_age_ms")) if last_started else None
    max_spread_bps = _to_float(last_started.get("max_spread_bps")) if last_started else None
    base_order_notional = _to_float(last_started.get("base_order_notional")) if last_started else None
    max_inventory_notional = _to_float(last_started.get("max_inventory_notional")) if last_started else None
    account_balance = _to_float(last_started.get("account_balance")) if last_started else None
    leverage = _to_float(last_started.get("leverage")) if last_started else None
    pnl_series = _build_pnl_series(current_samples, current_trades)
    size_metrics = _compute_size_metrics(
        current_samples,
        current_fills,
        current_trades,
        account_balance=account_balance,
        leverage=leverage,
    )
    gross_realized_pnl_total = sum((_to_float(row.get("gross_pnl")) or 0.0) for row in current_trades)
    closed_fee_drag_total = sum((_to_float(row.get("fees_paid")) or 0.0) for row in current_trades)
    maker_fee_cost_total = sum((_to_float(row.get("maker_fee_cost")) or 0.0) for row in current_trades)
    taker_fee_cost_total = sum((_to_float(row.get("taker_fee_cost")) or 0.0) for row in current_trades)
    maker_rebates_total = sum((_to_float(row.get("maker_rebates")) or 0.0) for row in current_trades)
    maker_notional_total = sum((_to_float(row.get("maker_notional")) or 0.0) for row in current_trades)
    taker_notional_total = sum((_to_float(row.get("taker_notional")) or 0.0) for row in current_trades)
    fill_fee_drag_total = sum((_to_float(row.get("exchange_fee_delta")) or 0.0) for row in current_fills)
    fill_fee_cost_total = sum(max((_to_float(row.get("exchange_fee_delta")) or 0.0), 0.0) for row in current_fills)
    fill_rebate_total = sum(max(-((_to_float(row.get("exchange_fee_delta")) or 0.0)), 0.0) for row in current_fills)
    closed_turnover_notional = size_metrics.get("closed_turnover_notional") or 0.0
    fill_turnover_notional = size_metrics.get("fill_turnover_notional") or 0.0
    gross_edge_bps = ((gross_realized_pnl_total / closed_turnover_notional) * 10_000.0) if closed_turnover_notional else 0.0
    net_edge_bps = ((realized_pnl_total / closed_turnover_notional) * 10_000.0) if closed_turnover_notional else 0.0
    maker_share_pct = ((maker_notional_total / fill_turnover_notional) * 100.0) if fill_turnover_notional else 0.0
    taker_share_pct = ((taker_notional_total / fill_turnover_notional) * 100.0) if fill_turnover_notional else 0.0
    pnl_positive = sum(max((_to_float(row.get("realized_pnl")) or 0.0), 0.0) for row in current_trades)
    pnl_negative = sum(min((_to_float(row.get("realized_pnl")) or 0.0), 0.0) for row in current_trades)
    profit_factor = (pnl_positive / abs(pnl_negative)) if pnl_negative < 0.0 else None
    passive_trades = [
        row
        for row in current_trades
        if not str(row.get("close_reason") or "").startswith("kill")
    ]
    kill_trades = [
        row
        for row in current_trades
        if str(row.get("close_reason") or "").startswith("kill")
    ]
    passive_realized_pnl_total = sum((_to_float(row.get("realized_pnl")) or 0.0) for row in passive_trades)
    kill_realized_pnl_total = sum((_to_float(row.get("realized_pnl")) or 0.0) for row in kill_trades)
    passive_closed_turnover = sum(
        ((_to_float(row.get("entry_price_avg")) or 0.0) + (_to_float(row.get("exit_price_avg")) or 0.0))
        * abs(_to_float(row.get("max_abs_qty")) or 0.0)
        for row in passive_trades
    )
    kill_closed_turnover = sum(
        ((_to_float(row.get("entry_price_avg")) or 0.0) + (_to_float(row.get("exit_price_avg")) or 0.0))
        * abs(_to_float(row.get("max_abs_qty")) or 0.0)
        for row in kill_trades
    )
    passive_edge_bps = ((passive_realized_pnl_total / passive_closed_turnover) * 10_000.0) if passive_closed_turnover else 0.0
    kill_edge_bps = ((kill_realized_pnl_total / kill_closed_turnover) * 10_000.0) if kill_closed_turnover else 0.0
    fee_tier = str(latest_sample.get("fee_tier_label") or "n/a")
    fee_actual_tier = str(latest_sample.get("fee_actual_tier_label") or "n/a")
    fee_projected_tier = str(latest_sample.get("fee_projected_tier_label") or "n/a")
    fee_market_type = str(latest_sample.get("fee_market_type") or (last_started.get("fee_market_type") if last_started else "n/a"))
    fee_staking_tier = str(latest_sample.get("fee_staking_tier") or (last_started.get("fee_staking_tier") if last_started else "n/a"))
    fee_basis = str(latest_sample.get("fee_basis") or (last_started.get("fee_tier_basis") if last_started else "n/a"))
    fee_maker_rate_bps = _to_float(latest_sample.get("fee_maker_rate_bps"))
    fee_taker_rate_bps = _to_float(latest_sample.get("fee_taker_rate_bps"))
    fee_maker_rebate_bps = _to_float(latest_sample.get("fee_maker_rebate_bps"))
    fee_net_maker_rate_bps = _to_float(latest_sample.get("fee_net_maker_rate_bps"))
    fee_required_edge_bps = _to_float(latest_sample.get("fee_required_edge_bps"))
    fee_dynamic_kill_floor_bps = _to_float(latest_sample.get("fee_dynamic_kill_floor_bps"))
    fee_expected_taker_share_pct = _to_float(latest_sample.get("fee_expected_taker_share_pct"))
    fee_current_14d_weighted_volume = _to_float(latest_sample.get("fee_current_weighted_14d_volume"))
    fee_projected_14d_weighted_volume = _to_float(latest_sample.get("fee_projected_weighted_14d_volume"))
    fee_daily_volume_run_rate = _to_float(latest_sample.get("fee_daily_volume_run_rate"))
    fee_target_tier_label = str(latest_sample.get("fee_target_tier_label") or "n/a")
    fee_target_tier_progress_pct = _to_float(latest_sample.get("fee_target_tier_progress_pct"))
    fee_days_to_next_tier = _to_float(latest_sample.get("fee_days_to_next_tier"))
    volume_boost_multiplier = _to_float(latest_sample.get("volume_boost_multiplier"))
    fee_rate_source = str(latest_sample.get("fee_rate_source") or "estimated_schedule")
    fee_deployer_fee_scale = _to_float(latest_sample.get("fee_deployer_fee_scale")) or 0.0
    fee_growth_mode = bool(latest_sample.get("fee_growth_mode"))
    fee_aligned_quote_token = bool(latest_sample.get("fee_aligned_quote_token"))
    fee_taker_referral_discount_pct = _to_float(last_started.get("fee_taker_referral_discount_pct")) if last_started else 0.0
    fee_maker_rebate_bps_override = _to_float(last_started.get("fee_maker_rebate_bps_override")) if last_started else 0.0
    fee_user_maker_rate_pct_override = _to_float(last_started.get("fee_user_maker_rate_pct_override")) if last_started else None
    fee_user_taker_rate_pct_override = _to_float(last_started.get("fee_user_taker_rate_pct_override")) if last_started else None

    fee_cfg = HyperliquidFeeConfig(
        product="perps",
        market_type=fee_market_type,
        staking_tier=fee_staking_tier,
        taker_referral_discount_pct=fee_taker_referral_discount_pct or 0.0,
        maker_rebate_bps_override=fee_maker_rebate_bps_override or 0.0,
        deployer_fee_scale=fee_deployer_fee_scale,
        growth_mode=fee_growth_mode,
        aligned_quote_token=fee_aligned_quote_token,
        user_maker_rate_pct_override=fee_user_maker_rate_pct_override,
        user_taker_rate_pct_override=fee_user_taker_rate_pct_override,
    )

    fee_actual_tier_index = _tier_index_from_label(fee_actual_tier)
    fee_projected_tier_index = _tier_index_from_label(fee_projected_tier)
    fee_target_tier_index = _tier_index_from_label(fee_target_tier_label)

    fee_actual_rates = (
        fee_rates_for_tier(
            tier_index=fee_actual_tier_index,
            config=fee_cfg,
            use_user_overrides=(
                fee_rate_source in {"userFees", "manual_account_rates"}
                and (fee_user_maker_rate_pct_override is not None or fee_user_taker_rate_pct_override is not None)
            ),
        )
        if fee_actual_tier_index is not None
        else None
    )
    fee_projected_rates = (
        fee_rates_for_tier(tier_index=fee_projected_tier_index, config=fee_cfg)
        if fee_projected_tier_index is not None
        else None
    )
    fee_target_rates = (
        fee_rates_for_tier(tier_index=fee_target_tier_index, config=fee_cfg)
        if fee_target_tier_index is not None
        else None
    )
    fee_precision_note = (
        "exact account rates from userFees"
        if fee_rate_source == "userFees"
        else "exact account rates provided manually"
        if fee_rate_source == "manual_account_rates"
        else "exact market scaling, account tier estimated from local run-rate"
    )

    current_run = {
        "start_utc": last_started.get("timestamp_utc") if last_started else None,
        "latest_sample": latest_sample,
        "latest_fill": latest_fill,
        "fills_count": len(current_fills),
        "passive_fills": len(passive_fills),
        "kill_fills": len(kill_fills),
        "aggressive_fills": len(aggressive_fills),
        "closed_episodes": len(current_trades),
        "realized_pnl_total": realized_pnl_total,
        "gross_realized_pnl_total": gross_realized_pnl_total,
        "inventory_unrealized_pnl": inventory_unrealized,
        "total_pnl": total_pnl,
        "closed_fee_drag_total": closed_fee_drag_total,
        "fill_fee_drag_total": fill_fee_drag_total,
        "fill_fee_cost_total": fill_fee_cost_total,
        "fill_rebate_total": fill_rebate_total,
        "maker_fee_cost_total": maker_fee_cost_total,
        "taker_fee_cost_total": taker_fee_cost_total,
        "maker_rebates_total": maker_rebates_total,
        "maker_notional_total": maker_notional_total,
        "taker_notional_total": taker_notional_total,
        "maker_share_pct": maker_share_pct,
        "taker_share_pct": taker_share_pct,
        "gross_edge_bps": gross_edge_bps,
        "net_edge_bps": net_edge_bps,
        "profit_factor": profit_factor,
        "win_rate_pct": win_rate_pct,
        "avg_hold_seconds": avg_hold_seconds,
        "avg_best_markout_bps": avg_best_markout,
        "avg_realized_spread_bps": avg_realized_spread,
        "passive_realized_pnl_total": passive_realized_pnl_total,
        "kill_realized_pnl_total": kill_realized_pnl_total,
        "passive_closed_turnover": passive_closed_turnover,
        "kill_closed_turnover": kill_closed_turnover,
        "passive_edge_bps": passive_edge_bps,
        "kill_edge_bps": kill_edge_bps,
        "kill_episode_count": len(kill_trades),
        "passive_episode_count": len(passive_trades),
        "polling_errors": sum(1 for row in current_events if row.get("event") == "polling_error"),
        "stale_quote_skips": sum(1 for row in current_events if row.get("event") == "stale_quote_skipped"),
        "quote_gap_warnings": sum(1 for row in current_events if row.get("event") == "quote_gap_warning"),
        "feed_reconnects": sum(1 for row in current_events if row.get("event") == "feed_reconnected"),
        "quote_posts": sum(1 for row in current_events if row.get("event") == "quote_posted"),
        "quote_replaces": sum(1 for row in current_events if row.get("event") == "quote_replaced"),
        "quote_cancels": sum(1 for row in current_events if row.get("event") == "quote_canceled"),
        "current_quote_age_ms": _to_float(latest_sample.get("quote_age_ms")),
        "p50_quote_age_ms": _percentile(quote_ages, 0.50),
        "p95_quote_age_ms": _percentile(quote_ages, 0.95),
        "max_quote_age_ms_seen": _max_value(quote_ages),
        "current_transport_delay_ms": _to_float(latest_sample.get("transport_delay_ms")),
        "p50_transport_delay_ms": _percentile(transports, 0.50),
        "p95_transport_delay_ms": _percentile(transports, 0.95),
        "max_transport_delay_ms": _max_value(transports),
        "current_spread_bps": _to_float(latest_sample.get("spread_bps")),
        "p50_spread_bps": _percentile(spreads, 0.50),
        "p95_spread_bps": _percentile(spreads, 0.95),
        "max_spread_bps_seen": _max_value(spreads),
        "configured_max_quote_age_ms": max_quote_age_ms,
        "configured_max_spread_bps": max_spread_bps,
        "configured_base_order_notional": base_order_notional,
        "configured_max_inventory_notional": max_inventory_notional,
        "configured_account_balance": account_balance,
        "configured_leverage": leverage,
        "fee_tier_label": fee_tier,
        "fee_actual_tier_label": fee_actual_tier,
        "fee_projected_tier_label": fee_projected_tier,
        "fee_market_type": fee_market_type,
        "fee_staking_tier": fee_staking_tier,
        "fee_basis": fee_basis,
        "fee_rate_source": fee_rate_source,
        "fee_maker_rate_bps": fee_maker_rate_bps,
        "fee_taker_rate_bps": fee_taker_rate_bps,
        "fee_maker_rebate_bps": fee_maker_rebate_bps,
        "fee_net_maker_rate_bps": fee_net_maker_rate_bps,
        "fee_actual_maker_rate_bps": fee_actual_rates.maker_rate_bps if fee_actual_rates else None,
        "fee_actual_taker_rate_bps": fee_actual_rates.taker_rate_bps if fee_actual_rates else None,
        "fee_actual_net_maker_rate_bps": fee_actual_rates.net_maker_rate_bps if fee_actual_rates else None,
        "fee_actual_maker_rebate_bps": fee_actual_rates.maker_rebate_bps if fee_actual_rates else None,
        "fee_projected_maker_rate_bps": fee_projected_rates.maker_rate_bps if fee_projected_rates else None,
        "fee_projected_taker_rate_bps": fee_projected_rates.taker_rate_bps if fee_projected_rates else None,
        "fee_projected_net_maker_rate_bps": fee_projected_rates.net_maker_rate_bps if fee_projected_rates else None,
        "fee_projected_maker_rebate_bps": fee_projected_rates.maker_rebate_bps if fee_projected_rates else None,
        "fee_target_maker_rate_bps": fee_target_rates.maker_rate_bps if fee_target_rates else None,
        "fee_target_taker_rate_bps": fee_target_rates.taker_rate_bps if fee_target_rates else None,
        "fee_target_net_maker_rate_bps": fee_target_rates.net_maker_rate_bps if fee_target_rates else None,
        "fee_required_edge_bps": fee_required_edge_bps,
        "fee_dynamic_kill_floor_bps": fee_dynamic_kill_floor_bps,
        "fee_expected_taker_share_pct": fee_expected_taker_share_pct,
        "fee_current_weighted_14d_volume": fee_current_14d_weighted_volume,
        "fee_projected_weighted_14d_volume": fee_projected_14d_weighted_volume,
        "fee_daily_volume_run_rate": fee_daily_volume_run_rate,
        "fee_target_tier_label": fee_target_tier_label,
        "fee_target_tier_progress_pct": fee_target_tier_progress_pct,
        "fee_days_to_next_tier": fee_days_to_next_tier,
        "fee_precision_note": fee_precision_note,
        "volume_boost_multiplier": volume_boost_multiplier,
        "quote_mode_counts": dict(quote_mode_counts),
        "quote_reason_counts": dict(quote_reason_counts),
        "event_regime_samples": sum(1 for row in current_samples if row.get("event_regime")),
        "inventory_protection_samples": sum(
            1 for row in current_samples if row.get("quoting_reason") == "inventory_protection"
        ),
        "size_metrics": size_metrics,
        "pnl_series": pnl_series,
    }

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_state": run_state,
        "run_count": len(starts),
        "latest_report": latest_report,
        "last_started": last_started,
        "latest_sample": latest_sample,
        "current_run": current_run,
        "decision": {
            "headline": _decision_headline(latest_sample),
            "freshness": _freshness_label(latest_sample, max_quote_age_ms),
            "note": latest_sample.get("decision_note"),
            "quote_mode": latest_sample.get("quote_mode"),
            "quote_reason": latest_sample.get("quoting_reason"),
            "bid_reason": latest_sample.get("bid_reason"),
            "ask_reason": latest_sample.get("ask_reason"),
            "data_source": "websocket bbo + l2Book + trades",
        },
        "recent_fills": current_fills[-20:],
        "recent_trades": current_trades[-20:],
    }


def render_html() -> str:
    return """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Asterion Liquidity Engine</title>
  <style>
    :root {
      --bg-a: #081117;
      --bg-b: #102827;
      --panel: rgba(255, 255, 255, 0.065);
      --panel-strong: rgba(255, 255, 255, 0.085);
      --text: #e8f1eb;
      --muted: #a4b6ac;
      --good: #7ee0a5;
      --warn: #f2cd6d;
      --bad: #ef8c7f;
      --line: rgba(255, 255, 255, 0.1);
      --accent: #9ad0c3;
      --body: "Avenir Next", "Segoe UI", "Helvetica Neue", sans-serif;
      --mono: "JetBrains Mono", "IBM Plex Mono", ui-monospace, monospace;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: var(--body);
      background:
        radial-gradient(circle at top left, rgba(68, 135, 129, 0.28), transparent 30%),
        linear-gradient(135deg, var(--bg-a), var(--bg-b));
      color: var(--text);
      min-height: 100vh;
    }
    main { padding: 22px; max-width: 1560px; margin: 0 auto; }
    h1, h2, h3 { margin: 0; font-weight: 650; letter-spacing: 0.01em; }
    h1 { font-size: 24px; }
    h2 { font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.12em; }
    h3 { font-size: 14px; color: var(--accent); }
    p.meta, p.copy { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.45; }
    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: end;
      gap: 16px;
      margin-bottom: 16px;
    }
    .hero, .panel, .table-panel, .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 16px;
      backdrop-filter: blur(12px);
    }
    .hero {
      display: grid;
      grid-template-columns: 1.6fr 1fr;
      gap: 14px;
      padding: 16px;
      margin-bottom: 16px;
    }
    .hero-copy { display: grid; gap: 8px; }
    .hero-stats {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
      gap: 10px;
    }
    .section {
      display: grid;
      gap: 10px;
      margin-bottom: 16px;
    }
    .metric { padding: 11px 12px; min-height: 80px; }
    .metric .k {
      color: var(--muted);
      font-size: 11px;
      margin-bottom: 8px;
      text-transform: uppercase;
      letter-spacing: 0.09em;
    }
    .metric .v {
      font-family: var(--mono);
      font-size: 19px;
      font-weight: 650;
      line-height: 1.1;
      margin-bottom: 4px;
    }
    .metric .s {
      color: var(--muted);
      font-size: 11px;
      line-height: 1.35;
    }
    .good { color: var(--good); }
    .warn { color: var(--warn); }
    .bad { color: var(--bad); }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      width: fit-content;
      padding: 4px 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      font-size: 11px;
      color: var(--muted);
      background: var(--panel-strong);
    }
    .split {
      display: grid;
      grid-template-columns: 1.2fr 1fr;
      gap: 16px;
    }
    .chart-panel {
      padding: 12px 14px;
      min-height: 220px;
      display: grid;
      gap: 8px;
    }
    .chart-meta {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 11px;
    }
    svg.chart {
      width: 100%;
      height: 180px;
      display: block;
      border-radius: 12px;
      background: rgba(0, 0, 0, 0.08);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
      font-family: var(--mono);
    }
    th, td {
      padding: 8px 7px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-weight: 650;
      position: sticky;
      top: 0;
      background: rgba(8, 17, 23, 0.92);
    }
    .table-panel { padding: 0; overflow: auto; max-height: 360px; }
    .table-panel table { min-width: 100%; }
    .stack { display: grid; gap: 8px; }
    @media (max-width: 720px) {
      main { padding: 14px; }
      .hero, .split, .hero-stats { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main>
    <div class="topbar">
      <div class="stack">
        <h1>Asterion Liquidity Engine</h1>
        <p class="meta" id="stamp">Loading...</p>
      </div>
      <div class="pill" id="datasource">Loading source...</div>
    </div>

    <section class="hero">
      <div class="hero-copy">
        <h3 id="headline">Loading decision state...</h3>
        <p class="copy" id="decision-note"></p>
        <div class="pill" id="freshness">Feed freshness</div>
      </div>
      <div class="hero-stats" id="hero-stats"></div>
    </section>

    <section class="section">
      <h2>Market And Feed</h2>
      <div class="grid" id="market"></div>
    </section>

    <section class="split">
      <div class="section">
        <h2>Quote Engine</h2>
        <div class="grid" id="quotes"></div>
      </div>
      <div class="section">
        <h2>Execution And Risk</h2>
        <div class="grid" id="execution"></div>
      </div>
    </section>

    <section class="split">
      <div class="section">
        <h2>Tier And Costs</h2>
        <div class="grid" id="fees"></div>
      </div>
      <div class="section">
        <h2>Kill And Edge Quality</h2>
        <div class="grid" id="kills"></div>
      </div>
    </section>

    <section class="section">
      <h2>Live PnL</h2>
      <div class="chart-panel">
        <div class="chart-meta">
          <span id="chart-summary">Loading chart...</span>
          <span>Gross realized, net realized, and net total from current run start</span>
        </div>
        <svg class="chart" id="pnl-chart" viewBox="0 0 900 180" preserveAspectRatio="none"></svg>
      </div>
    </section>

    <section class="section">
      <h2>Recent Fills</h2>
      <div class="table-panel">
        <table>
          <thead>
            <tr>
              <th>Time</th><th>ID</th><th>Ep</th><th>Role</th><th>Side</th><th>Px</th><th>Sz</th><th>Reason</th><th>Inv</th><th>Fee</th><th>Delta PnL</th>
            </tr>
          </thead>
          <tbody id="fills"></tbody>
        </table>
      </div>
    </section>

    <section class="section">
      <h2>Closed Episodes</h2>
      <div class="table-panel">
        <table>
          <thead>
            <tr>
              <th>Ep</th><th>Side</th><th>Open</th><th>Close</th><th>Qty</th><th>Gross</th><th>Fees</th><th>Rebates</th><th>Realized</th><th>Hold(s)</th><th>Best Markout</th><th>Reason</th>
            </tr>
          </thead>
          <tbody id="trades"></tbody>
        </table>
      </div>
    </section>
  </main>

  <script>
    const fmt = (value, digits = 2) => {
      if (value === null || value === undefined || value === '') return 'n/a';
      const num = Number(value);
      if (Number.isNaN(num)) return String(value);
      return num.toFixed(digits);
    };

    const cls = (value, kind = 'default', context = {}) => {
      if (kind === 'pnl') return Number(value) >= 0 ? 'good' : 'bad';
      if (kind === 'state') {
        if (value === 'running' || value === 'fresh') return 'good';
        if (value === 'stopped') return 'warn';
        return 'bad';
      }
      if (kind === 'latency') {
        const limit = Number(context.limit ?? 900);
        const num = Number(value);
        if (Number.isNaN(num)) return '';
        if (num <= limit * 0.5) return 'good';
        if (num <= limit) return 'warn';
        return 'bad';
      }
      if (kind === 'imbalance') return Math.abs(Number(value ?? 0)) >= 0.55 ? 'warn' : 'good';
      if (kind === 'spread') {
        const limit = Number(context.limit ?? 6);
        const num = Number(value);
        if (Number.isNaN(num)) return '';
        if (num <= limit * 0.65) return 'good';
        if (num <= limit) return 'warn';
        return 'bad';
      }
      return '';
    };

    const metric = (label, value, sub = '', extraClass = '') => `
      <div class="metric">
        <div class="k">${label}</div>
        <div class="v ${extraClass}">${value}</div>
        <div class="s">${sub}</div>
      </div>
    `;

    const renderPnlChart = (series) => {
      const svg = document.getElementById('pnl-chart');
      const summary = document.getElementById('chart-summary');
      if (!series || series.length < 2) {
        svg.innerHTML = '';
        summary.textContent = 'Not enough samples for a live PnL chart yet.';
        return;
      }

      const width = 900;
      const height = 180;
      const padX = 16;
      const padY = 14;
      const grossValues = series.map((p) => Number(p.gross_realized_pnl || 0));
      const netValues = series.map((p) => Number(p.realized_pnl || 0));
      const totalValues = series.map((p) => Number(p.total_pnl || 0));
      const values = totalValues.concat(netValues).concat(grossValues);
      let minY = Math.min(...values);
      let maxY = Math.max(...values);
      if (minY === maxY) {
        minY -= 1;
        maxY += 1;
      }
      const yScale = (value) => {
        const t = (value - minY) / (maxY - minY);
        return height - padY - (t * (height - (padY * 2)));
      };
      const xScale = (idx) => padX + (idx / (series.length - 1)) * (width - (padX * 2));
      const pathFor = (arr) => arr.map((v, i) => `${i === 0 ? 'M' : 'L'} ${xScale(i).toFixed(2)} ${yScale(v).toFixed(2)}`).join(' ');
      const zeroY = yScale(0);
      svg.innerHTML = `
        <line x1="${padX}" y1="${zeroY.toFixed(2)}" x2="${width - padX}" y2="${zeroY.toFixed(2)}" stroke="rgba(255,255,255,0.14)" stroke-width="1" />
        <path d="${pathFor(grossValues)}" fill="none" stroke="#f2cd6d" stroke-width="1.8" />
        <path d="${pathFor(netValues)}" fill="none" stroke="#8fb7ff" stroke-width="2" />
        <path d="${pathFor(totalValues)}" fill="none" stroke="#7ee0a5" stroke-width="2.4" />
      `;
      summary.textContent =
        `range ${fmt(minY, 4)} to ${fmt(maxY, 4)} | gross ${fmt(grossValues[grossValues.length - 1], 4)} | net ${fmt(netValues[netValues.length - 1], 4)} | total ${fmt(totalValues[totalValues.length - 1], 4)}`;
    };

    async function refresh() {
      const res = await fetch('/api/summary');
      const data = await res.json();
      const cur = data.current_run || {};
      const decision = data.decision || {};
      const sample = cur.latest_sample || data.latest_sample || {};
      const size = cur.size_metrics || {};
      const modeCounts = cur.quote_mode_counts || {};
      const reasonCounts = cur.quote_reason_counts || {};
      const bidQuoteNotional = Number(sample.bid_quote_price || 0) * Number(sample.bid_quote_size || 0);
      const askQuoteNotional = Number(sample.ask_quote_price || 0) * Number(sample.ask_quote_size || 0);
      const liveBidNotional = Number(sample.live_bid_order_price || 0) * Number(sample.live_bid_order_size || 0);
      const liveAskNotional = Number(sample.live_ask_order_price || 0) * Number(sample.live_ask_order_size || 0);
      const grossRealized = Number(cur.gross_realized_pnl_total || 0);
      const netRealized = Number(cur.realized_pnl_total || 0);
      const netTotal = Number(cur.total_pnl || 0);
      const feeDrag = Number(cur.fill_fee_drag_total || 0);
      const makerShare = Number(cur.maker_share_pct || 0);
      const takerShare = Number(cur.taker_share_pct || 0);
      const leverage = Number(cur.configured_leverage || 0);
      const fillTurnover = Number(size.fill_turnover_notional || 0);
      const closedTurnover = Number(size.closed_turnover_notional || 0);
      const feeBasis = cur.fee_basis || 'n/a';
      const profitFactor = cur.profit_factor;

      document.getElementById('stamp').textContent = `Updated ${data.generated_at_utc}`;
      document.getElementById('datasource').textContent = `Source: ${decision.data_source || 'n/a'}`;
      document.getElementById('headline').textContent = decision.headline || 'No decision text yet.';
      document.getElementById('decision-note').textContent = decision.note || 'Waiting for the next sample.';
      document.getElementById('freshness').className = `pill ${cls(decision.freshness, 'state')}`;
      document.getElementById('freshness').textContent =
        `Feed ${decision.freshness || 'unknown'} | age ${fmt(cur.current_quote_age_ms, 0)} ms / cap ${fmt(cur.configured_max_quote_age_ms, 0)} ms`;

      document.getElementById('hero-stats').innerHTML =
        metric('Run State', data.run_state || 'unknown', 'engine state', cls(data.run_state, 'state')) +
        metric('Quote Mode', sample.quote_mode || 'n/a', sample.quoting_reason || 'n/a', sample.quoting_enabled ? 'good' : 'warn') +
        metric('Net Total PnL', fmt(netTotal, 4), `net closed ${fmt(netRealized, 4)} / unrl ${fmt(cur.inventory_unrealized_pnl, 4)}`, cls(netTotal, 'pnl')) +
        metric('Gross Closed PnL', fmt(grossRealized, 4), `run fee drag ${fmt(feeDrag, 4)} / rebates ${fmt(cur.fill_rebate_total, 4)}`, cls(grossRealized, 'pnl')) +
        metric('Inventory', `${sample.inventory_side || 'FLAT'} ${fmt(sample.inventory_qty, 4)}`, `mark ${fmt(sample.inventory_unrealized_bps, 2)} bps`, sample.inventory_side === 'FLAT' ? 'warn' : 'good') +
        metric('Tier', cur.fee_tier_label || 'n/a', `${cur.fee_actual_tier_label || 'n/a'} actual / ${cur.fee_projected_tier_label || 'n/a'} projected`, 'good') +
        metric('Capital', `$${fmt(cur.configured_account_balance, 0)} / $${fmt(size.buying_power, 0)}`, `equity / ${fmt(leverage, 0)}x buying power`);

      document.getElementById('market').innerHTML =
        metric('Bid / Ask', `${fmt(sample.bid, 4)} / ${fmt(sample.ask, 4)}`, `mid ${fmt(sample.mid, 4)} micro ${fmt(sample.microprice, 4)}`) +
        metric('Spread bps', fmt(cur.current_spread_bps, 3), `p50 ${fmt(cur.p50_spread_bps, 3)} | p95 ${fmt(cur.p95_spread_bps, 3)} | max ${fmt(cur.max_spread_bps_seen, 3)}`, cls(cur.current_spread_bps, 'spread', {limit: cur.configured_max_spread_bps})) +
        metric('Quote Age ms', fmt(cur.current_quote_age_ms, 0), `p50 ${fmt(cur.p50_quote_age_ms, 0)} | p95 ${fmt(cur.p95_quote_age_ms, 0)} | max ${fmt(cur.max_quote_age_ms_seen, 0)}`, cls(cur.current_quote_age_ms, 'latency', {limit: cur.configured_max_quote_age_ms})) +
        metric('Transport ms', fmt(cur.current_transport_delay_ms, 0), `p50 ${fmt(cur.p50_transport_delay_ms, 0)} | p95 ${fmt(cur.p95_transport_delay_ms, 0)} | max ${fmt(cur.max_transport_delay_ms, 0)}`, cls(cur.current_transport_delay_ms, 'latency', {limit: cur.configured_max_quote_age_ms})) +
        metric('Vol / Impulse', `${fmt(sample.recent_vol_bps, 2)} / ${fmt(sample.impulse_bps, 2)}`, 'bps over short horizon') +
        metric('Trade Rate', fmt(sample.trade_rate_per_second, 2), `trade count ${fmt(sample.trade_count, 0)}`) +
        metric('Flow Imbalance', fmt(sample.flow_imbalance, 3), `buy ${fmt(sample.buy_volume, 2)} / sell ${fmt(sample.sell_volume, 2)}`, cls(sample.flow_imbalance, 'imbalance')) +
        metric('Book Imbalance', fmt(sample.book_imbalance, 3), `bid depth ${fmt(sample.bid_depth, 2)} / ask depth ${fmt(sample.ask_depth, 2)}`, cls(sample.book_imbalance, 'imbalance')) +
        metric('Feed Health', sample.event_regime ? 'event regime' : 'normal', `stale skips ${fmt(cur.stale_quote_skips, 0)} | reconnects ${fmt(cur.feed_reconnects, 0)}`, sample.event_regime ? 'bad' : 'good') +
        metric('Volume Run Rate', `$${fmt(cur.fee_daily_volume_run_rate, 0)}`, `weighted 14d ${fmt(cur.fee_projected_weighted_14d_volume, 0)}`, 'good');

      document.getElementById('quotes').innerHTML =
        metric('Quote Reason', sample.quoting_reason || 'n/a', decision.note || '', sample.quoting_enabled ? 'good' : 'warn') +
        metric('Bid Side', sample.bid_enabled ? (sample.bid_quote_price ? fmt(sample.bid_quote_price, 4) : 'enabled') : 'off', sample.bid_reason || 'n/a', sample.bid_enabled ? 'good' : 'warn') +
        metric('Ask Side', sample.ask_enabled ? (sample.ask_quote_price ? fmt(sample.ask_quote_price, 4) : 'enabled') : 'off', sample.ask_reason || 'n/a', sample.ask_enabled ? 'good' : 'warn') +
        metric('Base / Max Notional', `${fmt(cur.configured_base_order_notional, 0)} / ${fmt(cur.configured_max_inventory_notional, 0)}`, 'per side base / total inventory cap') +
        metric('Active Quote Notional', `${fmt(size.active_quote_avg_notional, 0)} avg`, `p95 ${fmt(size.active_quote_p95_notional, 0)} | max ${fmt(size.active_quote_max_notional, 0)}`) +
        metric('Target Quote Size', `${fmt(sample.bid_quote_size, 4)} / ${fmt(sample.ask_quote_size, 4)}`, `notional ${fmt(bidQuoteNotional, 2)} / ${fmt(askQuoteNotional, 2)}`) +
        metric('Tier Volume Boost', `${fmt(cur.volume_boost_multiplier, 2)}x`, `target ${cur.fee_target_tier_label || 'n/a'} progress ${fmt(cur.fee_target_tier_progress_pct, 1)}%`) +
        metric('Size Risk Mult', `${fmt(sample.size_risk_multiplier, 2)}x`, 'risk shrink applied to quote size before posting') +
        metric('Fair / Reservation', `${fmt(sample.fair_value, 4)} / ${fmt(sample.reservation_price, 4)}`, `alpha ${fmt(sample.alpha_bps, 3)} bps`) +
        metric('Target Half Spread', fmt(sample.target_half_spread_bps, 3), `inventory skew ${fmt(sample.inventory_skew_bps, 3)} bps | fee edge ${fmt(cur.fee_required_edge_bps, 3)} bps`) +
        metric('Live Bid / Ask', `${fmt(sample.live_bid_order_price, 4)} / ${fmt(sample.live_ask_order_price, 4)}`, `size ${fmt(sample.live_bid_order_size, 4)} / ${fmt(sample.live_ask_order_size, 4)}, notional ${fmt(liveBidNotional, 2)} / ${fmt(liveAskNotional, 2)}`) +
        metric('Queue Ahead', `${fmt(sample.bid_queue_ahead_size, 3)} / ${fmt(sample.ask_queue_ahead_size, 3)}`, 'bid / ask') +
        metric('Passive Fill Size', `$${fmt(size.passive_fill_avg_notional, 0)} avg`, `p95 $${fmt(size.passive_fill_p95_notional, 0)} | max $${fmt(size.passive_fill_max_notional, 0)}`) +
        metric('Touch Share', `${fmt(size.passive_fill_avg_touch_share_pct, 1)}% avg`, `p95 ${fmt(size.passive_fill_p95_touch_share_pct, 1)}% | <=25% on ${fmt(size.passive_fill_under_25pct_touch_pct, 1)}% of passive fills`) +
        metric('Capacity Usage', `${fmt(size.passive_fill_avg_buying_power_pct, 2)}% avg`, `p95 ${fmt(size.passive_fill_p95_buying_power_pct, 2)}% of buying power`) +
        metric('Quote Modes', `${fmt(modeCounts.both || 0, 0)} both`, `bid-only ${fmt(modeCounts.bid_only || 0, 0)} | ask-only ${fmt(modeCounts.ask_only || 0, 0)} | flat ${fmt(modeCounts.flat || 0, 0)}`) +
        metric('Protection Time', fmt(cur.inventory_protection_samples, 0), `event samples ${fmt(cur.event_regime_samples, 0)}`) +
        metric('Toxicity Score', fmt(sample.toxicity_score, 3), `top reasons ${Object.keys(reasonCounts).slice(0, 3).join(', ') || 'n/a'}`, Number(sample.toxicity_score) >= 1 ? 'warn' : 'good');

      document.getElementById('execution').innerHTML =
        metric('Passive Fills', fmt(cur.passive_fills, 0), `kill fills ${fmt(cur.kill_fills, 0)} / total fills ${fmt(cur.fills_count, 0)}`) +
        metric('Closed Episodes', fmt(cur.closed_episodes, 0), `win rate ${fmt(cur.win_rate_pct, 2)}%`) +
        metric('Success Split', `${fmt(size.wins, 0)} / ${fmt(size.losses, 0)} / ${fmt(size.flats, 0)}`, `wins / losses / flat, loss rate ${fmt(size.loss_rate_pct, 2)}%`) +
        metric('Profit Factor', profitFactor === null || profitFactor === undefined ? 'n/a' : fmt(profitFactor, 2), 'gross winning pnl over losing pnl', profitFactor && profitFactor >= 1.5 ? 'good' : 'warn') +
        metric('Avg Hold', fmt(cur.avg_hold_seconds, 3), 'seconds per episode') +
        metric('Best Markout', fmt(cur.avg_best_markout_bps, 3), 'average best bps') +
        metric('Realized Spread', fmt(cur.avg_realized_spread_bps, 3), `gross ${fmt(cur.gross_edge_bps, 3)} bps | net ${fmt(cur.net_edge_bps, 3)} bps`, cls(cur.avg_realized_spread_bps, 'pnl')) +
        metric('Fill Turnover', `$${fmt(fillTurnover, 0)}`, `${fmt(size.fill_turnover_units, 2)} units traded this run`) +
        metric('Closed Turnover', `$${fmt(closedTurnover, 0)}`, 'entry plus exit notional across completed episodes') +
        metric('Queue At Fill', `${fmt(size.passive_fill_queue_ahead_avg, 1)} avg`, `p95 ${fmt(size.passive_fill_queue_ahead_p95, 1)} ahead`) +
        metric('Thin-Book Outliers', `${fmt(size.passive_fill_over_50pct_touch_count, 0)}`, `passive fills >50% of visible touch; usually a thinning-book edge case`, size.passive_fill_over_50pct_touch_count > 0 ? 'warn' : 'good') +
        metric('Quote Ops', `${fmt(cur.quote_posts, 0)} / ${fmt(cur.quote_replaces, 0)}`, `posts / replaces, cancels ${fmt(cur.quote_cancels, 0)}`) +
        metric('Loop Errors', fmt(cur.polling_errors, 0), `gap warnings ${fmt(cur.quote_gap_warnings, 0)} | reconnects ${fmt(cur.feed_reconnects, 0)}`, cur.polling_errors > 0 ? 'bad' : 'good') +
        metric('Latest Fill', cur.latest_fill.reason || 'n/a', `${cur.latest_fill.liquidity_role || 'n/a'} ${cur.latest_fill.side || ''}`) +
        metric('Realized PnL', fmt(cur.realized_pnl_total, 4), 'current run closed inventory episodes', cls(cur.realized_pnl_total, 'pnl')) +
        metric('Unrealized PnL', fmt(cur.inventory_unrealized_pnl, 4), `current run inventory ${sample.inventory_side || 'FLAT'}`, cls(cur.inventory_unrealized_pnl, 'pnl'));

      document.getElementById('fees').innerHTML =
        metric('Tier Basis', feeBasis, `${cur.fee_market_type || 'n/a'} | staking ${cur.fee_staking_tier || 'n/a'} | source ${sample.fee_rate_source || 'n/a'}`) +
        metric('Active Tier', cur.fee_tier_label || 'n/a', `${cur.fee_actual_tier_label || 'n/a'} actual / ${cur.fee_projected_tier_label || 'n/a'} projected`, 'good') +
        metric('Actual Now bps', `${fmt(cur.fee_actual_net_maker_rate_bps, 4)} / ${fmt(cur.fee_actual_taker_rate_bps, 4)}`, `maker / taker if current actual tier is used`) +
        metric('Projected Run bps', `${fmt(cur.fee_projected_net_maker_rate_bps, 4)} / ${fmt(cur.fee_projected_taker_rate_bps, 4)}`, `maker / taker if current run-rate persists`) +
        metric('Decision bps', `${fmt(cur.fee_net_maker_rate_bps, 4)} / ${fmt(cur.fee_taker_rate_bps, 4)}`, `engine basis ${feeBasis} | rebate ${fmt(cur.fee_maker_rebate_bps, 4)} bps`) +
        metric('HIP-3 Scaling', `${sample.fee_growth_mode ? 'growth on' : 'growth off'}`, `deployer scale ${fmt(sample.fee_deployer_fee_scale, 4)} | aligned quote ${sample.fee_aligned_quote_token ? 'yes' : 'no'}`) +
        metric('Required Edge', fmt(cur.fee_required_edge_bps, 3), `expected taker share ${fmt(cur.fee_expected_taker_share_pct, 2)}%`, (Number(cur.net_edge_bps) >= Number(cur.fee_required_edge_bps)) ? 'good' : 'warn') +
        metric('Run Fee Drag', fmt(cur.fill_fee_drag_total, 4), `maker fees ${fmt(cur.maker_fee_cost_total, 4)} | taker fees ${fmt(cur.taker_fee_cost_total, 4)} | rebates ${fmt(cur.maker_rebates_total, 4)}`, cls(-cur.fill_fee_drag_total, 'pnl')) +
        metric('Gross / Net Realized', `${fmt(cur.gross_realized_pnl_total, 4)} / ${fmt(cur.realized_pnl_total, 4)}`, `cost impact ${(grossRealized !== 0) ? fmt((feeDrag / Math.abs(grossRealized)) * 100, 1) : 'n/a'}% of gross`) +
        metric('Maker / Taker Mix', `${fmt(makerShare, 1)}% / ${fmt(takerShare, 1)}%`, `$${fmt(cur.maker_notional_total, 0)} / $${fmt(cur.taker_notional_total, 0)} turnover`) +
        metric('Volume Path', `$${fmt(cur.fee_daily_volume_run_rate, 0)}`, `current 14d $${fmt(cur.fee_current_weighted_14d_volume, 0)} | projected $${fmt(cur.fee_projected_weighted_14d_volume, 0)}`) +
        metric('Target Tier', cur.fee_target_tier_label || 'n/a', `target maker / taker ${fmt(cur.fee_target_net_maker_rate_bps, 4)} / ${fmt(cur.fee_target_taker_rate_bps, 4)} bps`) +
        metric('Fee Precision', sample.fee_rate_source || 'n/a', cur.fee_precision_note || 'n/a') +
        metric('Edge Surplus', fmt(Number(cur.net_edge_bps || 0) - Number(cur.fee_required_edge_bps || 0), 3), `net edge minus fee-adjusted required edge`, (Number(cur.net_edge_bps || 0) >= Number(cur.fee_required_edge_bps || 0)) ? 'good' : 'warn');

      document.getElementById('kills').innerHTML =
        metric('Kill Episodes', fmt(cur.kill_episode_count, 0), `passive closes ${fmt(cur.passive_episode_count, 0)}`) +
        metric('Kill PnL', fmt(cur.kill_realized_pnl_total, 4), `$${fmt(cur.kill_closed_turnover, 0)} turnover | ${fmt(cur.kill_edge_bps, 3)} bps`, cls(cur.kill_realized_pnl_total, 'pnl')) +
        metric('Passive PnL', fmt(cur.passive_realized_pnl_total, 4), `$${fmt(cur.passive_closed_turnover, 0)} turnover | ${fmt(cur.passive_edge_bps, 3)} bps`, cls(cur.passive_realized_pnl_total, 'pnl')) +
        metric('Dynamic Kill Floor', fmt(cur.fee_dynamic_kill_floor_bps, 3), `current adverse exit floor after fees and slippage`) +
        metric('Kill Share Of Turnover', `${closedTurnover > 0 ? fmt((Number(cur.kill_closed_turnover || 0) / closedTurnover) * 100, 1) : 'n/a'}%`, 'completed-episode turnover ending in kills', Number(cur.kill_edge_bps || 0) < 0 ? 'warn' : 'good') +
        metric('Kill Share Of Loss', `${grossRealized !== 0 ? fmt((Math.abs(Number(cur.kill_realized_pnl_total || 0)) / Math.max(Math.abs(grossRealized), 1e-9)) * 100, 1) : 'n/a'}%`, 'absolute kill drag relative to gross realized pnl', Number(cur.kill_realized_pnl_total || 0) < 0 ? 'warn' : 'good') +
        metric('Quote vs Kill Mix', `${fmt(cur.passive_fills, 0)} passive`, `${fmt(cur.kill_fills, 0)} aggressive kill fills | expected taker ${fmt(cur.fee_expected_taker_share_pct, 2)}%`) +
        metric('Protection Samples', `${fmt(cur.inventory_protection_samples, 0)}`, `event regime ${fmt(cur.event_regime_samples, 0)} | stale skips ${fmt(cur.stale_quote_skips, 0)}`) +
        metric('Kill Efficiency', `${fmt(cur.avg_best_markout_bps, 3)} best`, `net spread ${fmt(cur.net_edge_bps, 3)} bps | realized spread ${fmt(cur.avg_realized_spread_bps, 3)} bps`, Number(cur.net_edge_bps || 0) > 0 ? 'good' : 'warn');

      renderPnlChart(cur.pnl_series || []);

      const fillsRows = (data.recent_fills || []).slice().reverse().map((row) => `
        <tr>
          <td>${row.timestamp_utc || ''}</td>
          <td>${row.fill_id || ''}</td>
          <td>${row.episode_id || ''}</td>
          <td>${row.liquidity_role || ''}</td>
          <td>${row.side || ''}</td>
          <td>${fmt(row.price, 4)}</td>
          <td>${fmt(row.size, 4)}</td>
          <td>${row.reason || ''}</td>
          <td>${fmt(row.inventory_qty_after, 4)}</td>
          <td>${fmt(row.exchange_fee_delta, 4)}</td>
          <td>${fmt(row.realized_pnl_delta, 4)}</td>
        </tr>
      `).join('');
      document.getElementById('fills').innerHTML = fillsRows || '<tr><td colspan="11">No fills yet.</td></tr>';

      const tradeRows = (data.recent_trades || []).slice().reverse().map((row) => `
        <tr>
          <td>${row.episode_id || ''}</td>
          <td>${row.side || ''}</td>
          <td>${row.open_time_utc || ''}</td>
          <td>${row.close_time_utc || ''}</td>
          <td>${fmt(row.max_abs_qty, 4)}</td>
          <td>${fmt(row.gross_pnl, 4)}</td>
          <td>${fmt(row.fees_paid, 4)}</td>
          <td>${fmt(row.maker_rebates, 4)}</td>
          <td>${fmt(row.realized_pnl, 4)}</td>
          <td>${fmt(row.hold_seconds, 3)}</td>
          <td>${fmt(row.best_markout_bps, 3)}</td>
          <td>${row.close_reason || ''}</td>
        </tr>
      `).join('');
      document.getElementById('trades').innerHTML = tradeRows || '<tr><td colspan="12">No closed episodes yet.</td></tr>';
    }

    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>"""


def make_handler(
    events_path: Path,
    samples_path: Path,
    fills_path: Path,
    trades_path: Path,
    reports_dir: Path,
):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/summary":
                payload = build_summary(
                    events_path,
                    samples_path,
                    fills_path,
                    trades_path,
                    reports_dir,
                )
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if parsed.path in {"/", "/index.html"}:
                data = render_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def main() -> None:
    args = parse_args()
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(
            Path(args.events_jsonl),
            Path(args.samples_jsonl),
            Path(args.fills_csv),
            Path(args.trades_csv),
            Path(args.reports_dir),
        ),
    )
    print(f"{APP_NAME}: http://{args.host}:{args.port}")
    print(f"Events: {args.events_jsonl}")
    print(f"Samples: {args.samples_jsonl}")
    print(f"Fills: {args.fills_csv}")
    print(f"Trades: {args.trades_csv}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping dashboard")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

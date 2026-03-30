#!/usr/bin/env python3
"""Dashboard for the professional mid-frequency oil momentum trader."""

from __future__ import annotations

import argparse
import csv
import json
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

APP_NAME = "Vector Momentum Engine"
SUMMARY_REFRESH_SECONDS = 5.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local dashboard for the pro mid-frequency momentum bot")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--events-jsonl", default="logs/mid_pro_events.jsonl")
    parser.add_argument("--samples-jsonl", default="logs/mid_pro_samples.jsonl")
    parser.add_argument("--trades-csv", default="logs/mid_pro_trades.csv")
    parser.add_argument("--markouts-jsonl", default="logs/mid_pro_markouts.jsonl")
    parser.add_argument("--reports-dir", default="logs/mid_pro_reports")
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


class SummaryCache:
    def __init__(self, builder, *builder_args: Path) -> None:
        self._builder = builder
        self._builder_args = builder_args
        self._payload_bytes: bytes | None = None
        self._signature: tuple[tuple[str, int, int], ...] | None = None
        self._built_at = 0.0
        self._lock = threading.Lock()

    def _current_signature(self) -> tuple[tuple[str, int, int], ...]:
        signature: List[tuple[str, int, int]] = []
        for path in self._builder_args:
            if path.is_dir():
                latest_mtime_ns = -1
                latest_size = 0
                try:
                    for child in path.glob("*.md"):
                        stat = child.stat()
                        if stat.st_mtime_ns >= latest_mtime_ns:
                            latest_mtime_ns = stat.st_mtime_ns
                            latest_size = stat.st_size
                except OSError:
                    latest_mtime_ns = -1
                    latest_size = 0
                signature.append((str(path), latest_mtime_ns, latest_size))
                continue
            try:
                stat = path.stat()
                signature.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                signature.append((str(path), -1, -1))
        return tuple(signature)

    def get_json_bytes(self) -> bytes:
        now = time.monotonic()
        with self._lock:
            if self._payload_bytes is not None and now - self._built_at < SUMMARY_REFRESH_SECONDS:
                return self._payload_bytes

        signature = self._current_signature()
        with self._lock:
            if self._payload_bytes is not None and signature == self._signature:
                self._built_at = now
                return self._payload_bytes

        payload = self._builder(*self._builder_args)
        data = json.dumps(payload).encode("utf-8")
        with self._lock:
            self._payload_bytes = data
            self._signature = signature
            self._built_at = now
        return data


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


def _latest(rows: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    return rows[-1] if rows else None


def _filter_since(rows: List[Dict[str, Any]], field: str, since: datetime | None) -> List[Dict[str, Any]]:
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


def _compress_series(points: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    if len(points) <= limit:
        return points
    step = max(1, len(points) // limit)
    reduced = points[::step]
    if reduced[-1] != points[-1]:
        reduced.append(points[-1])
    return reduced


def _build_pnl_series(samples: List[Dict[str, Any]], trades: List[Dict[str, Any]], limit: int = 480) -> List[Dict[str, Any]]:
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
    points: List[Dict[str, Any]] = []
    for sample in sorted_samples:
        sample_ts = _parse_utc(sample.get("timestamp_utc"))
        if sample_ts is None:
            continue
        while trade_idx < len(sorted_trades):
            trade_ts = _parse_utc(sorted_trades[trade_idx].get("close_time_utc"))
            if trade_ts is None or trade_ts > sample_ts:
                break
            realized_running += _to_float(sorted_trades[trade_idx].get("realized_pnl")) or 0.0
            trade_idx += 1
        unrealized = _to_float(sample.get("unrealized_pnl")) or 0.0
        points.append(
            {
                "timestamp_utc": sample_ts.isoformat(),
                "realized_pnl": round(realized_running, 8),
                "unrealized_pnl": round(unrealized, 8),
                "total_pnl": round(realized_running + unrealized, 8),
            }
        )
    return _compress_series(points, limit)


def _build_score_series(samples: List[Dict[str, Any]], limit: int = 480) -> List[Dict[str, Any]]:
    points: List[Dict[str, Any]] = []
    for sample in samples:
        ts = _parse_utc(sample.get("timestamp_utc"))
        if ts is None:
            continue
        points.append(
            {
                "timestamp_utc": ts.isoformat(),
                "long_score": round(_to_float(sample.get("long_score")) or 0.0, 4),
                "short_score": round(_to_float(sample.get("short_score")) or 0.0, 4),
                "mid": round(_to_float(sample.get("mid")) or 0.0, 8),
            }
        )
    return _compress_series(points, limit)


def _headline(latest_sample: Dict[str, Any], current_run: Dict[str, Any]) -> str:
    if not latest_sample:
        return "Waiting for live market data."
    state = str(latest_sample.get("state") or "idle")
    regime = str(latest_sample.get("regime") or "unknown")
    candidate_side = latest_sample.get("candidate_side")
    position_side = latest_sample.get("position_side")
    if state == "in_position" and position_side:
        return (
            f"Running {position_side} mid-momentum. The trade is live, the stop stack is active, "
            "and winners are uncapped other than the trailing logic."
        )
    if state == "handoff_extreme":
        return "Standing down because the tape has moved into extreme-event territory better handled by the rare-event app."
    if state == "cooldown":
        return "Cooling down after the last trade to avoid reflexive re-entry into the same tape burst."
    if state == "armed" and candidate_side:
        return f"{candidate_side} setup is armed. The app is waiting for confirmation persistence before entering."
    if state == "setup":
        return "The tape is active and directional, but not all confirmation gates agree yet."
    if regime == "blocked":
        return "No trade because the spread is too wide for a professional momentum scalp."
    return "Flat and waiting for a clean medium-frequency impulse, breakout, and tape confirmation."


def _gate_matrix(sample: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    def row(prefix: str) -> List[Dict[str, Any]]:
        return [
            {"label": "Fast impulse", "ok": bool(sample.get(f"{prefix}_fast_ok"))},
            {"label": "Confirm impulse", "ok": bool(sample.get(f"{prefix}_confirm_ok"))},
            {"label": "Breakout", "ok": bool(sample.get(f"{prefix}_breakout_ok"))},
            {"label": "Flow", "ok": bool(sample.get(f"{prefix}_flow_ok"))},
            {"label": "Book", "ok": bool(sample.get(f"{prefix}_book_ok"))},
            {"label": "Trade count", "ok": bool(sample.get(f"{prefix}_trade_count_ok"))},
        ]
    return {"long": row("long"), "short": row("short")}


def _run_config(last_started: Dict[str, Any] | None) -> Dict[str, float | int | None]:
    if not last_started:
        return {}
    keys = [
        "account_balance",
        "leverage",
        "sample_ms",
        "setup_score_min",
        "breakout_buffer_bps",
        "flow_imbalance_min",
        "book_imbalance_min",
        "min_trade_count",
        "max_spread_bps",
        "max_quote_age_ms",
        "equity_risk_pct",
        "entry_confirmation_samples",
        "cooldown_seconds",
        "tactical_score_min",
        "tactical_breakout_slack_bps",
        "score_edge_min",
        "instant_entry_score_min",
        "score_notional_boost",
        "initial_notional_fraction",
        "max_notional_fraction",
        "max_add_ons",
        "add_on_trigger_r",
        "add_on_fraction",
        "add_on_score_min",
        "add_on_breakout_extension_bps",
        "max_reductions",
        "de_risk_fraction",
        "de_risk_score_threshold",
        "de_risk_confirm_ratio",
        "runner_core_fraction",
    ]
    return {key: _to_float(last_started.get(key)) for key in keys}


def _side_gate_details(sample: Dict[str, Any], side: str, config: Dict[str, float | int | None]) -> List[Dict[str, Any]]:
    if not sample:
        return []
    is_long = side == "long"
    gate_prefix = "long" if is_long else "short"
    fast_value = _to_float(sample.get("fast_impulse_bps")) or 0.0
    confirm_value = _to_float(sample.get("confirm_impulse_bps")) or 0.0
    fast_threshold = _to_float(sample.get("dynamic_fast_threshold_bps")) or 0.0
    confirm_threshold = _to_float(sample.get("dynamic_confirm_threshold_bps")) or 0.0
    breakout_buffer = float(config.get("breakout_buffer_bps") or 0.0)
    flow_min = float(config.get("flow_imbalance_min") or 0.0)
    book_min = float(config.get("book_imbalance_min") or 0.0)
    trade_min = float(config.get("min_trade_count") or 0.0)
    breakout_value = _to_float(sample.get(f"breakout_distance_{gate_prefix}_bps")) or 0.0
    flow_value = _to_float(sample.get("flow_imbalance")) or 0.0
    book_value = _to_float(sample.get("book_imbalance")) or 0.0
    trade_value = _to_float(sample.get("trade_count")) or 0.0

    if is_long:
        fast_gap = max(0.0, fast_threshold - fast_value)
        confirm_gap = max(0.0, confirm_threshold - confirm_value)
        flow_gap = max(0.0, flow_min - flow_value)
        book_gap = max(0.0, book_min - book_value)
        breakout_gap = max(0.0, breakout_buffer - breakout_value)
        flow_target = f">= +{flow_min:.2f}"
        book_target = f">= +{book_min:.2f}"
        breakout_target = f">= +{breakout_buffer:.2f} bps"
    else:
        fast_gap = max(0.0, fast_threshold + fast_value)
        confirm_gap = max(0.0, confirm_threshold + confirm_value)
        flow_gap = max(0.0, flow_min + flow_value)
        book_gap = max(0.0, book_min + book_value)
        breakout_gap = max(0.0, breakout_buffer - breakout_value)
        flow_target = f"<= -{flow_min:.2f}"
        book_target = f"<= -{book_min:.2f}"
        breakout_target = f">= +{breakout_buffer:.2f} bps"

    trade_gap = max(0.0, trade_min - trade_value)

    return [
        {
            "label": "Fast impulse",
            "ok": bool(sample.get(f"{gate_prefix}_fast_ok")),
            "current": f"{fast_value:+.2f} bps",
            "target": f"{'>=' if is_long else '<='} {'+' if is_long else '-'}{fast_threshold:.2f} bps",
            "gap": round(fast_gap, 2),
        },
        {
            "label": "Confirm impulse",
            "ok": bool(sample.get(f"{gate_prefix}_confirm_ok")),
            "current": f"{confirm_value:+.2f} bps",
            "target": f"{'>=' if is_long else '<='} {'+' if is_long else '-'}{confirm_threshold:.2f} bps",
            "gap": round(confirm_gap, 2),
        },
        {
            "label": "Breakout",
            "ok": bool(sample.get(f"{gate_prefix}_breakout_ok")),
            "current": f"{breakout_value:+.2f} bps",
            "target": breakout_target,
            "gap": round(breakout_gap, 2),
        },
        {
            "label": "Flow",
            "ok": bool(sample.get(f"{gate_prefix}_flow_ok")),
            "current": f"{flow_value:+.3f}",
            "target": flow_target,
            "gap": round(flow_gap, 3),
        },
        {
            "label": "Book",
            "ok": bool(sample.get(f"{gate_prefix}_book_ok")),
            "current": f"{book_value:+.3f}",
            "target": book_target,
            "gap": round(book_gap, 3),
        },
        {
            "label": "Trade count",
            "ok": bool(sample.get(f"{gate_prefix}_trade_count_ok")),
            "current": f"{trade_value:.0f}",
            "target": f">= {trade_min:.0f}",
            "gap": round(trade_gap, 0),
        },
    ]


def _side_blockers(samples: List[Dict[str, Any]], side: str) -> Dict[str, Any]:
    setup_key = f"{side}_setup"
    ready_key = f"{side}_ready"
    gates = [
        ("Fast impulse", f"{side}_fast_ok"),
        ("Confirm impulse", f"{side}_confirm_ok"),
        ("Breakout", f"{side}_breakout_ok"),
        ("Flow", f"{side}_flow_ok"),
        ("Book", f"{side}_book_ok"),
        ("Trade count", f"{side}_trade_count_ok"),
    ]
    blocker_counts: Counter[str] = Counter()
    setup_samples = 0
    ready_samples = 0
    for row in samples:
        if row.get(setup_key):
            setup_samples += 1
            if row.get(ready_key):
                ready_samples += 1
            else:
                for label, key in gates:
                    if not row.get(key):
                        blocker_counts[label] += 1
    return {
        "setup_samples": setup_samples,
        "ready_samples": ready_samples,
        "blockers": blocker_counts.most_common(4),
    }


def _trade_size_stats(
    trades: List[Dict[str, Any]],
    account_balance: float | None,
    leverage: float | None,
) -> Dict[str, float | None]:
    notionals = [_to_float(row.get("notional")) for row in trades]
    margins = [_to_float(row.get("required_margin")) for row in trades]
    sizes = [_to_float(row.get("size")) for row in trades]
    starter_notionals = [_to_float(row.get("starter_notional")) for row in trades]
    max_notionals = [_to_float(row.get("max_notional_seen")) for row in trades]
    add_ons = [_to_float(row.get("add_on_count")) for row in trades]
    reductions = [_to_float(row.get("reduction_count")) for row in trades]
    notionals = [v for v in notionals if v is not None]
    margins = [v for v in margins if v is not None]
    sizes = [v for v in sizes if v is not None]
    starter_notionals = [v for v in starter_notionals if v is not None]
    max_notionals = [v for v in max_notionals if v is not None]
    add_ons = [v for v in add_ons if v is not None]
    reductions = [v for v in reductions if v is not None]
    latest = trades[-1] if trades else None
    latest_notional = _to_float(latest.get("notional")) if latest else None
    latest_margin = _to_float(latest.get("required_margin")) if latest else None
    buying_power = (account_balance * leverage) if account_balance and leverage else None
    return {
        "avg_notional": (sum(notionals) / len(notionals)) if notionals else None,
        "max_notional": max(notionals) if notionals else None,
        "avg_starter_notional": (sum(starter_notionals) / len(starter_notionals)) if starter_notionals else None,
        "avg_peak_notional": (sum(max_notionals) / len(max_notionals)) if max_notionals else None,
        "max_peak_notional": max(max_notionals) if max_notionals else None,
        "avg_margin": (sum(margins) / len(margins)) if margins else None,
        "max_margin": max(margins) if margins else None,
        "avg_size": (sum(sizes) / len(sizes)) if sizes else None,
        "latest_notional": latest_notional,
        "latest_margin": latest_margin,
        "avg_add_on_count": (sum(add_ons) / len(add_ons)) if add_ons else None,
        "avg_reduction_count": (sum(reductions) / len(reductions)) if reductions else None,
        "avg_margin_pct_equity": ((sum(margins) / len(margins)) / account_balance * 100.0) if margins and account_balance else None,
        "latest_margin_pct_equity": (latest_margin / account_balance * 100.0) if latest_margin and account_balance else None,
        "avg_notional_pct_buying_power": ((sum(notionals) / len(notionals)) / buying_power * 100.0) if notionals and buying_power else None,
        "latest_notional_pct_buying_power": (latest_notional / buying_power * 100.0) if latest_notional and buying_power else None,
        "avg_peak_notional_pct_buying_power": ((sum(max_notionals) / len(max_notionals)) / buying_power * 100.0) if max_notionals and buying_power else None,
    }


def _trade_performance(trades: List[Dict[str, Any]]) -> Dict[str, float | int | Dict[str, Any]]:
    realized = [(_to_float(row.get("realized_pnl")) or 0.0) for row in trades]
    wins = [value for value in realized if value > 0]
    losses = [value for value in realized if value < 0]
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else None
    avg_win = (sum(wins) / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None
    expectancy = (sum(realized) / len(realized)) if realized else None
    side_counts = Counter(str(row.get("side") or "unknown") for row in trades)
    profiles = Counter(str(row.get("entry_profile") or "legacy") for row in trades)
    add_ons = [(_to_float(row.get("add_on_count")) or 0.0) for row in trades]
    reductions = [(_to_float(row.get("reduction_count")) or 0.0) for row in trades]
    return {
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "wins": len(wins),
        "losses": len(losses),
        "profiles": dict(profiles),
        "side_counts": dict(side_counts),
        "avg_add_ons": (sum(add_ons) / len(add_ons)) if add_ons else None,
        "avg_reductions": (sum(reductions) / len(reductions)) if reductions else None,
    }


def _gate_pass_rates(samples: List[Dict[str, Any]], side: str) -> List[Dict[str, Any]]:
    active = [row for row in samples if str(row.get("regime") or "") == "active"]
    total = len(active)
    gates = [
        ("Fast impulse", f"{side}_fast_ok"),
        ("Confirm impulse", f"{side}_confirm_ok"),
        ("Breakout", f"{side}_breakout_ok"),
        ("Flow", f"{side}_flow_ok"),
        ("Book", f"{side}_book_ok"),
        ("Trade count", f"{side}_trade_count_ok"),
    ]
    rows: List[Dict[str, Any]] = []
    for label, key in gates:
        passed = sum(1 for row in active if row.get(key))
        pct = (passed / total * 100.0) if total else 0.0
        rows.append({"label": label, "passed": passed, "total": total, "rate_pct": round(pct, 1)})
    return rows


def _missing_gate_labels(details: List[Dict[str, Any]]) -> List[str]:
    return [row["label"] for row in details if not row["ok"]]


def build_summary(
    events_path: Path,
    samples_path: Path,
    trades_path: Path,
    markouts_path: Path,
    reports_dir: Path,
) -> Dict[str, Any]:
    events = _read_jsonl(events_path)
    samples = _read_jsonl(samples_path)
    trades = _read_csv(trades_path)
    markouts = _read_jsonl(markouts_path)

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
    current_samples = _filter_since(samples, "timestamp_utc", last_started_ts)
    current_trades = _filter_since(trades, "open_time_utc", last_started_ts)
    current_markouts = _filter_since(markouts, "timestamp_utc", last_started_ts)

    latest_sample = _latest(current_samples) or _latest(samples) or {}

    run_state = "unknown"
    if last_started is not None:
        run_state = "running"
        for stop in reversed(stops):
            stop_ts = _parse_utc(stop.get("timestamp_utc"))
            if stop_ts is not None and last_started_ts is not None and stop_ts >= last_started_ts:
                run_state = "stopped"
                break

    realized_pnl_total = sum((_to_float(row.get("realized_pnl")) or 0.0) for row in current_trades)
    unrealized_pnl = _to_float(latest_sample.get("unrealized_pnl")) or 0.0
    total_pnl = realized_pnl_total + unrealized_pnl
    wins = sum(1 for row in current_trades if (_to_float(row.get("realized_pnl")) or 0.0) > 0)
    win_rate_pct = (wins / len(current_trades) * 100.0) if current_trades else 0.0

    quote_ages = [_to_float(row.get("quote_age_ms")) for row in current_samples]
    transports = [_to_float(row.get("transport_delay_ms")) for row in current_samples]
    spreads = [_to_float(row.get("spread_bps")) for row in current_samples]
    quote_ages = [v for v in quote_ages if v is not None]
    transports = [v for v in transports if v is not None]
    spreads = [v for v in spreads if v is not None]

    avg_hold_seconds = (
        sum((_to_float(row.get("hold_seconds")) or 0.0) for row in current_trades) / len(current_trades)
        if current_trades
        else 0.0
    )
    avg_realized_bps = (
        sum((_to_float(row.get("realized_bps")) or 0.0) for row in current_trades) / len(current_trades)
        if current_trades
        else 0.0
    )
    avg_capture_ratio = (
        sum((_to_float(row.get("capture_ratio")) or 0.0) for row in current_trades) / len(current_trades)
        if current_trades
        else 0.0
    )
    avg_mfe = (
        sum((_to_float(row.get("mfe_bps")) or 0.0) for row in current_trades) / len(current_trades)
        if current_trades
        else 0.0
    )
    avg_mae = (
        sum((_to_float(row.get("mae_bps")) or 0.0) for row in current_trades) / len(current_trades)
        if current_trades
        else 0.0
    )

    latest_report = None
    if reports_dir.exists():
        reports = sorted(reports_dir.glob("*.md"))
        if reports:
            latest_report = str(reports[-1])

    regime_counts = Counter(str(row.get("regime") or "unknown") for row in current_samples)
    state_counts = Counter(str(row.get("state") or "unknown") for row in current_samples)
    exit_reason_counts = Counter(str(row.get("exit_reason") or "unknown") for row in current_trades)
    run_config = _run_config(last_started)
    long_gate_details = _side_gate_details(latest_sample, "long", run_config)
    short_gate_details = _side_gate_details(latest_sample, "short", run_config)
    long_blockers = _side_blockers(current_samples, "long")
    short_blockers = _side_blockers(current_samples, "short")

    open_position = None
    if latest_sample.get("position_side"):
        open_position = {
            "side": latest_sample.get("position_side"),
            "trade_id": latest_sample.get("open_trade_id"),
            "entry_price": _to_float(latest_sample.get("entry_price")),
            "active_stop": _to_float(latest_sample.get("active_stop")),
            "locked_stop": _to_float(latest_sample.get("locked_stop")),
            "hold_seconds": _to_float(latest_sample.get("hold_seconds")),
            "unrealized_pnl": _to_float(latest_sample.get("unrealized_pnl")),
            "trail_armed": bool(latest_sample.get("trail_armed")),
            "position_notional": _to_float(latest_sample.get("position_notional")),
            "starter_notional": _to_float(latest_sample.get("starter_notional")),
            "max_notional_seen": _to_float(latest_sample.get("max_notional_seen")),
            "add_on_count": _to_float(latest_sample.get("add_on_count")),
            "reduction_count": _to_float(latest_sample.get("reduction_count")),
            "entry_profile": latest_sample.get("entry_profile"),
        }

    recent_trades = sorted(
        current_trades,
        key=lambda row: _parse_utc(row.get("close_time_utc")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:24]
    recent_markouts = current_markouts[-24:]
    size_stats = _trade_size_stats(
        current_trades,
        _to_float(run_config.get("account_balance")),
        _to_float(run_config.get("leverage")),
    )
    performance = _trade_performance(current_trades)
    gate_pass_rates = {
        "long": _gate_pass_rates(current_samples, "long"),
        "short": _gate_pass_rates(current_samples, "short"),
    }

    signal_candidates = sum(1 for row in current_events if row.get("event") == "signal_candidate")
    signals_confirmed = sum(1 for row in current_events if row.get("event") == "signal_confirmed")
    entries = sum(1 for row in current_events if row.get("event") == "position_opened")
    exits = sum(1 for row in current_events if row.get("event") == "position_closed")

    candidate_side = str(latest_sample.get("candidate_side") or "").upper()
    long_score = _to_float(latest_sample.get("long_score")) or 0.0
    short_score = _to_float(latest_sample.get("short_score")) or 0.0
    dominant_side = "long" if candidate_side == "LONG" else "short" if candidate_side == "SHORT" else ("long" if long_score >= short_score else "short")
    dominant_details = long_gate_details if dominant_side == "long" else short_gate_details
    dominant_missing = _missing_gate_labels(dominant_details)

    if open_position:
        focus_title = f"{open_position['side']} trade is live"
        focus_body = "The engine is managing risk in-position. The only live questions are stop discipline, trailing behaviour, and whether the move keeps extending."
    elif dominant_missing:
        focus_title = f"{dominant_side.upper()} bias, but not tradable yet"
        focus_body = f"Current blocker stack: {', '.join(dominant_missing)}."
    else:
        focus_title = "Entry stack aligned"
        focus_body = "All gate conditions are aligned. The engine should promote to entry once persistence and state controls allow it."

    blocker_snapshot = {
        "long": [{"label": label, "count": count} for label, count in long_blockers["blockers"]],
        "short": [{"label": label, "count": count} for label, count in short_blockers["blockers"]],
    }
    total_samples = max(len(current_samples), 1)
    regime_share = {key: round((value / total_samples) * 100.0, 1) for key, value in regime_counts.items()}
    state_share = {key: round((value / total_samples) * 100.0, 1) for key, value in state_counts.items()}
    signal_funnel = {
        "candidates": signal_candidates,
        "confirmed": signals_confirmed,
        "entries": entries,
        "exits": exits,
        "confirm_rate_pct": round((signals_confirmed / signal_candidates) * 100.0, 1) if signal_candidates else 0.0,
        "entry_rate_pct": round((entries / signal_candidates) * 100.0, 1) if signal_candidates else 0.0,
    }
    run_hours = None
    if last_started_ts is not None:
        run_hours = round((datetime.now(timezone.utc) - last_started_ts).total_seconds() / 3600.0, 2)

    current_run = {
        "start_utc": last_started.get("timestamp_utc") if last_started else None,
        "run_hours": run_hours,
        "state": latest_sample.get("state"),
        "regime": latest_sample.get("regime"),
        "headline": _headline(latest_sample, {}),
        "focus_title": focus_title,
        "focus_body": focus_body,
        "dominant_side": dominant_side,
        "dominant_missing": dominant_missing,
        "latest_sample": latest_sample,
        "open_position": open_position,
        "realized_pnl_total": realized_pnl_total,
        "unrealized_pnl": unrealized_pnl,
        "total_pnl": total_pnl,
        "win_rate_pct": win_rate_pct,
        "trade_count": len(current_trades),
        "avg_hold_seconds": avg_hold_seconds,
        "avg_realized_bps": avg_realized_bps,
        "avg_capture_ratio": avg_capture_ratio,
        "avg_mfe_bps": avg_mfe,
        "avg_mae_bps": avg_mae,
        "signal_candidates": signal_candidates,
        "signals_confirmed": signals_confirmed,
        "entries": entries,
        "exits": exits,
        "polling_errors": sum(1 for row in current_events if row.get("event") == "polling_error"),
        "stale_quote_skips": sum(1 for row in current_events if row.get("event") == "stale_quote_skipped"),
        "quote_gap_warnings": sum(1 for row in current_events if row.get("event") == "quote_gap_warning"),
        "reconnects": sum(1 for row in current_events if row.get("event") == "feed_reconnected"),
        "p50_quote_age_ms": _percentile(quote_ages, 0.50),
        "p95_quote_age_ms": _percentile(quote_ages, 0.95),
        "p50_transport_delay_ms": _percentile(transports, 0.50),
        "p95_transport_delay_ms": _percentile(transports, 0.95),
        "p50_spread_bps": _percentile(spreads, 0.50),
        "p95_spread_bps": _percentile(spreads, 0.95),
        "max_quote_age_ms": _to_float(last_started.get("max_quote_age_ms")) if last_started else None,
        "regime_counts": dict(regime_counts),
        "regime_share": regime_share,
        "state_counts": dict(state_counts),
        "state_share": state_share,
        "exit_reason_counts": dict(exit_reason_counts),
        "gate_matrix": _gate_matrix(latest_sample),
        "gate_details": {"long": long_gate_details, "short": short_gate_details},
        "gate_pass_rates": gate_pass_rates,
        "blocker_snapshot": blocker_snapshot,
        "run_config": run_config,
        "signal_funnel": signal_funnel,
        "size_stats": size_stats,
        "performance": performance,
        "pnl_series": _build_pnl_series(current_samples, current_trades),
        "score_series": _build_score_series(current_samples),
        "recent_trades": recent_trades,
        "recent_markouts": recent_markouts,
    }
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_state": run_state,
        "run_count": len(starts),
        "latest_report": latest_report,
        "current_run": current_run,
    }


def render_html() -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{APP_NAME}</title>
  <style>
    :root {{
      --bg-0: #061019;
      --bg-1: #0a1620;
      --bg-2: #101d29;
      --card: rgba(255,255,255,0.045);
      --card-strong: rgba(255,255,255,0.07);
      --border: rgba(255,255,255,0.08);
      --border-strong: rgba(255,255,255,0.12);
      --text: #eef5f8;
      --muted: #8fa6b3;
      --soft: #c4d2da;
      --good: #78efbd;
      --warn: #ffd27e;
      --bad: #ff8e9f;
      --cyan: #63d8ff;
      --teal: #69efcf;
      --ink: #041018;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      --sans: "Avenir Next", "Neue Haas Grotesk Text Pro", "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--text);
      font: 12px/1.45 var(--sans);
      background:
        radial-gradient(circle at 0% 0%, rgba(99,216,255,0.14), transparent 28%),
        radial-gradient(circle at 100% 0%, rgba(105,239,207,0.09), transparent 30%),
        linear-gradient(180deg, #050d14 0%, var(--bg-0) 18%, var(--bg-1) 58%, #081117 100%);
    }}
    .wrap {{
      max-width: 1580px;
      margin: 0 auto;
      padding: 18px 18px 32px;
    }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 18px;
      margin-bottom: 14px;
    }}
    .eyebrow {{
      color: var(--cyan);
      font: 600 10px/1 var(--mono);
      letter-spacing: 0.16em;
      text-transform: uppercase;
      margin-bottom: 10px;
    }}
    h1 {{
      margin: 0;
      font-size: 30px;
      line-height: 1;
      letter-spacing: -0.05em;
    }}
    .sub {{
      margin-top: 8px;
      max-width: 980px;
      color: var(--muted);
      font-size: 12px;
    }}
    .stamp {{
      min-width: 240px;
      text-align: right;
      color: var(--muted);
      font: 12px/1.55 var(--mono);
    }}
    .hero {{
      display: grid;
      grid-template-columns: 1.35fr .95fr;
      gap: 14px;
      margin-bottom: 14px;
    }}
    .hero-panel {{
      border: 1px solid var(--border-strong);
      border-radius: 18px;
      background:
        linear-gradient(135deg, rgba(99,216,255,0.08), rgba(105,239,207,0.05)),
        rgba(255,255,255,0.035);
      padding: 16px 18px;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.03);
    }}
    .hero-kicker {{
      color: var(--cyan);
      text-transform: uppercase;
      letter-spacing: 0.16em;
      font: 600 10px/1 var(--mono);
      margin-bottom: 8px;
    }}
    .hero-title {{
      font-size: 23px;
      line-height: 1.1;
      letter-spacing: -0.04em;
      margin-bottom: 8px;
    }}
    .hero-body {{
      color: var(--soft);
      max-width: 880px;
    }}
    .hero-tags {{
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
      align-content: flex-start;
    }}
    .tag {{
      display: inline-flex;
      align-items: center;
      padding: 5px 9px;
      border-radius: 999px;
      background: rgba(255,255,255,0.06);
      color: var(--soft);
      font: 11px/1 var(--mono);
      border: 1px solid rgba(255,255,255,0.05);
    }}
    .tag.good {{ color: var(--good); background: rgba(120,239,189,0.12); }}
    .tag.warn {{ color: var(--warn); background: rgba(255,210,126,0.12); }}
    .tag.bad {{ color: var(--bad); background: rgba(255,142,159,0.12); }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }}
    .card {{
      min-height: 88px;
      padding: 11px 12px 12px;
      border-radius: 14px;
      border: 1px solid var(--border);
      background: var(--card);
      backdrop-filter: blur(6px);
    }}
    .label {{
      color: var(--muted);
      font: 10px/1.2 var(--mono);
      text-transform: uppercase;
      letter-spacing: 0.12em;
    }}
    .value {{
      margin-top: 6px;
      font: 700 22px/1 var(--mono);
      letter-spacing: -0.04em;
    }}
    .meta {{
      margin-top: 7px;
      color: var(--muted);
      font-size: 11px;
    }}
    .good {{ color: var(--good); }}
    .warn {{ color: var(--warn); }}
    .bad {{ color: var(--bad); }}
    .grid-2 {{
      display: grid;
      grid-template-columns: 1.25fr 1fr;
      gap: 12px;
      margin-bottom: 14px;
    }}
    .grid-3 {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }}
    .panel {{
      border-radius: 16px;
      border: 1px solid var(--border);
      background: var(--card);
      padding: 14px;
    }}
    .panel h2 {{
      margin: 0;
      font-size: 15px;
      letter-spacing: -0.02em;
    }}
    .panel-note {{
      margin: 6px 0 12px;
      color: var(--muted);
      font-size: 11px;
    }}
    .chart-wrap {{
      height: 248px;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,0.05);
      background: rgba(255,255,255,0.02);
      padding: 8px;
    }}
    canvas {{
      width: 100%;
      height: 100%;
      display: block;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 9px;
    }}
    .metric {{
      padding: 9px 10px;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,0.05);
      background: rgba(255,255,255,0.03);
    }}
    .metric .k {{
      color: var(--muted);
      font: 10px/1.2 var(--mono);
      text-transform: uppercase;
      letter-spacing: 0.1em;
    }}
    .metric .v {{
      margin-top: 4px;
      font: 600 16px/1.2 var(--mono);
    }}
    .focus-list, .blocker-list {{
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .focus-row, .blocker-row {{
      display: grid;
      grid-template-columns: 1.2fr 1fr 1fr .65fr;
      gap: 8px;
      align-items: center;
      padding: 9px 10px;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,0.05);
      background: rgba(255,255,255,0.028);
      font-family: var(--mono);
      font-size: 11px;
    }}
    .blocker-row {{
      grid-template-columns: 1fr .5fr;
    }}
    .bar-list {{
      display: flex;
      flex-direction: column;
      gap: 9px;
    }}
    .bar-row {{
      display: grid;
      grid-template-columns: 108px 1fr 52px;
      gap: 10px;
      align-items: center;
      font: 11px/1.2 var(--mono);
    }}
    .bar-track {{
      position: relative;
      height: 8px;
      border-radius: 999px;
      overflow: hidden;
      background: rgba(255,255,255,0.06);
    }}
    .bar-fill {{
      position: absolute;
      inset: 0 auto 0 0;
      height: 100%;
      border-radius: 999px;
      background: linear-gradient(90deg, rgba(99,216,255,0.95), rgba(120,239,189,0.95));
    }}
    .bar-row.warn .bar-fill {{
      background: linear-gradient(90deg, rgba(255,210,126,0.95), rgba(255,180,109,0.95));
    }}
    .bar-row.bad .bar-fill {{
      background: linear-gradient(90deg, rgba(255,142,159,0.95), rgba(255,114,140,0.95));
    }}
    .focus-row.wait {{
      border-color: rgba(255,210,126,0.18);
      background: rgba(255,210,126,0.05);
    }}
    .focus-row.pass {{
      border-color: rgba(120,239,189,0.18);
      background: rgba(120,239,189,0.05);
    }}
    .section-stack {{
      display: flex;
      flex-direction: column;
      gap: 10px;
    }}
    .gate-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }}
    .gate-box {{
      border-radius: 16px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.035);
      padding: 14px;
    }}
    .gate-title {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 10px;
      font-size: 14px;
      font-weight: 600;
    }}
    .score {{
      font: 700 19px/1 var(--mono);
    }}
    .table-wrap {{
      overflow: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font: 12px/1.35 var(--mono);
    }}
    th, td {{
      padding: 8px 6px;
      text-align: left;
      border-top: 1px solid rgba(255,255,255,0.06);
      white-space: nowrap;
    }}
    th {{
      color: var(--muted);
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.1em;
    }}
    tbody tr:hover {{
      background: rgba(255,255,255,0.03);
    }}
    .gate-state {{
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
    }}
    .funnel {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 12px;
    }}
    .funnel-step {{
      padding: 9px 10px;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,0.05);
      background: rgba(255,255,255,0.03);
    }}
    .funnel-step .num {{
      font: 700 19px/1 var(--mono);
      margin-top: 5px;
    }}
    .footer-note {{
      color: var(--muted);
      font-size: 11px;
      margin-top: 10px;
    }}
    @media (max-width: 1380px) {{
      .cards {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
      .grid-3 {{ grid-template-columns: 1fr; }}
      .grid-2, .hero, .gate-grid {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 860px) {{
      .wrap {{ padding: 12px; }}
      .topbar {{ flex-direction: column; }}
      .cards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .metrics {{ grid-template-columns: 1fr; }}
      .funnel {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .focus-row {{ grid-template-columns: 1fr; }}
      .blocker-row {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div>
        <div class="eyebrow">Medium-Frequency Momentum</div>
        <h1>{APP_NAME}</h1>
        <div class="sub">Institutional operator view for the live mid-frequency Brent momentum engine. The page is built to answer four questions quickly: is the tape tradeable, which side has edge, which gates are blocking entry, and how much size the engine actually commits when it does fire.</div>
      </div>
      <div class="stamp" id="stamp">Loading...</div>
    </div>

    <div class="hero">
      <div class="hero-panel">
        <div class="hero-kicker">Current Read</div>
        <div class="hero-title" id="focusTitle">Waiting for live state...</div>
        <div class="hero-body" id="focusBody">The dashboard will explain what the engine is doing, why it is waiting, and what would need to improve before the next trade.</div>
      </div>
      <div class="hero-panel">
        <div class="hero-kicker">Run Context</div>
        <div class="hero-tags" id="heroTags"></div>
      </div>
    </div>

    <div class="cards" id="cards"></div>

    <div class="grid-2">
      <div class="panel">
        <h2>Current-Run Equity Curve</h2>
        <div class="panel-note">Realized and total PnL from this live run start only. This preserves the existing overnight history and avoids mixing sessions.</div>
        <div class="chart-wrap"><canvas id="pnlChart"></canvas></div>
      </div>
      <div class="panel">
        <h2>Score Pressure</h2>
        <div class="panel-note">Long and short conviction from the live signal engine. A high score alone does not trade; the gate stack still has to clear.</div>
        <div class="chart-wrap"><canvas id="scoreChart"></canvas></div>
      </div>
    </div>

    <div class="grid-3">
      <div class="panel">
        <h2>Market Quality</h2>
        <div class="panel-note">Freshness, spread, volatility, flow, and depth. This is the first place to look before blaming the signal logic.</div>
        <div class="metrics" id="qualityMetrics"></div>
      </div>
      <div class="panel">
        <h2>Why It Is Or Is Not Trading</h2>
        <div class="panel-note">The dominant side is shown first. Missing gates are explicit so you can see whether the blocker is impulse, breakout, flow, book, or tape quality.</div>
        <div class="section-stack">
          <div id="focusTags"></div>
          <div class="focus-list" id="focusDetails"></div>
          <div class="footer-note" id="contextNote"></div>
        </div>
      </div>
      <div class="panel">
        <h2>Sizing And Risk</h2>
        <div class="panel-note">Starter risk, peak deployed size, and average de-risking. This is the quickest check that the engine is scaling into proof rather than starting too big.</div>
        <div class="metrics" id="sizingMetrics"></div>
        <div class="footer-note" id="sizingNote"></div>
      </div>
    </div>

    <div class="gate-grid">
      <div class="gate-box">
        <div class="gate-title"><span>Long Entry Ladder</span><span class="score" id="longScore">0</span></div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Gate</th>
                <th>Current</th>
                <th>Target</th>
                <th>Gap</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody id="longGates"></tbody>
          </table>
        </div>
      </div>
      <div class="gate-box">
        <div class="gate-title"><span>Short Entry Ladder</span><span class="score" id="shortScore">0</span></div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Gate</th>
                <th>Current</th>
                <th>Target</th>
                <th>Gap</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody id="shortGates"></tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="grid-3">
      <div class="panel">
        <h2>Execution Quality</h2>
        <div class="panel-note">Expectancy, win/loss shape, and runner behaviour. This answers whether the engine is still getting chopped or actually converting confirmed breaks into asymmetric outcomes.</div>
        <div class="metrics" id="executionMetrics"></div>
        <div class="footer-note" id="executionNote"></div>
      </div>
      <div class="panel">
        <h2>Regime And State Mix</h2>
        <div class="panel-note">Percent of the current run spent cold, active, blocked, extreme, idle, setup, armed, and in-position.</div>
        <div class="bar-list" id="regimeBars"></div>
        <div class="bar-list" id="stateBars" style="margin-top:10px"></div>
      </div>
      <div class="panel">
        <h2>Gate Pass Rates</h2>
        <div class="panel-note">Within active tape only. This shows exactly which gates are starving the strategy of entries over the run, not just on the last sample.</div>
        <div class="grid-2" style="margin:0">
          <div>
            <div class="label" style="margin-bottom:8px">Long</div>
            <div class="bar-list" id="longPassRates"></div>
          </div>
          <div>
            <div class="label" style="margin-bottom:8px">Short</div>
            <div class="bar-list" id="shortPassRates"></div>
          </div>
        </div>
      </div>
    </div>

    <div class="grid-2">
      <div class="panel">
        <h2>Signal Funnel And Persistent Blockers</h2>
        <div class="panel-note">Candidate setups should narrow aggressively. This panel shows whether the engine is disciplined or simply failing to promote signals.</div>
        <div class="funnel" id="funnel"></div>
        <div class="grid-2" style="margin:0">
          <div>
            <div class="label" style="margin-bottom:8px">Long Blockers</div>
            <div class="blocker-list" id="longBlockers"></div>
          </div>
          <div>
            <div class="label" style="margin-bottom:8px">Short Blockers</div>
            <div class="blocker-list" id="shortBlockers"></div>
          </div>
        </div>
      </div>
      <div class="panel">
        <h2>Recent Trades</h2>
        <div class="panel-note">Closed trades only. Starter size, peak deployed size, and add/de-risk counts are shown so you can see whether the engine built a runner or just scratched the first clip.</div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Side</th>
                <th>Profile</th>
                <th>Starter</th>
                <th>Peak</th>
                <th>Margin</th>
                <th>Add / De-risk</th>
                <th>PnL</th>
                <th>bps</th>
                <th>Hold</th>
                <th>Exit</th>
              </tr>
            </thead>
            <tbody id="tradesTable"></tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="panel">
      <h2>Post-Trade Diagnostics</h2>
      <div class="panel-note">MFE, MAE, and capture ratio show whether the engine is cutting failed follow-through fast enough and allowing real runners to breathe.</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Side</th>
              <th>MFE</th>
              <th>MAE</th>
              <th>Capture</th>
              <th>Entry Driver</th>
              <th>Exit</th>
            </tr>
          </thead>
          <tbody id="markoutsTable"></tbody>
        </table>
      </div>
    </div>
  </div>
  <script>
    function fmtNum(v, digits = 2) {{
      if (v === null || v === undefined || Number.isNaN(Number(v))) return "n/a";
      return Number(v).toFixed(digits);
    }}
    function fmtSigned(v, digits = 2) {{
      if (v === null || v === undefined || Number.isNaN(Number(v))) return "n/a";
      const n = Number(v);
      return `${{n >= 0 ? "+" : ""}}${{n.toFixed(digits)}}`;
    }}
    function fmtPct(v, digits = 1) {{
      if (v === null || v === undefined || Number.isNaN(Number(v))) return "n/a";
      return `${{Number(v).toFixed(digits)}}%`;
    }}
    function statusClass(v) {{
      if (v === null || v === undefined) return "";
      if (typeof v === "string") {{
        if (["running", "active", "in_position", "armed"].includes(v)) return "good";
        if (["setup", "cooldown"].includes(v)) return "warn";
        if (["blocked", "handoff_extreme", "stopped", "extreme"].includes(v)) return "bad";
        return "";
      }}
      return Number(v) >= 0 ? "good" : "bad";
    }}
    function drawChart(canvasId, series, fields, colors, minHint = null, maxHint = null, labels = null) {{
      const canvas = document.getElementById(canvasId);
      const ctx = canvas.getContext("2d");
      const rect = canvas.getBoundingClientRect();
      canvas.width = rect.width * devicePixelRatio;
      canvas.height = rect.height * devicePixelRatio;
      ctx.scale(devicePixelRatio, devicePixelRatio);
      ctx.clearRect(0, 0, rect.width, rect.height);
      ctx.fillStyle = "rgba(255,255,255,0.018)";
      ctx.fillRect(0, 0, rect.width, rect.height);
      if (!series || !series.length) {{
        ctx.fillStyle = "#8fa6b3";
        ctx.font = "12px ui-monospace, monospace";
        ctx.fillText("No data yet", 12, 20);
        return;
      }}
      const pad = {{l: 44, r: 14, t: 14, b: 22}};
      const xs = series.map((_, i) => pad.l + (i / Math.max(1, series.length - 1)) * (rect.width - pad.l - pad.r));
      let values = [];
      for (const field of fields) {{
        for (const point of series) values.push(Number(point[field] ?? 0));
      }}
      let min = minHint !== null ? minHint : Math.min(...values);
      let max = maxHint !== null ? maxHint : Math.max(...values);
      if (min === max) {{
        min -= 1;
        max += 1;
      }}
      const y = (v) => {{
        const ratio = (v - min) / (max - min);
        return rect.height - pad.b - ratio * (rect.height - pad.t - pad.b);
      }};
      ctx.strokeStyle = "rgba(255,255,255,0.07)";
      ctx.lineWidth = 1;
      for (let i = 0; i < 4; i++) {{
        const gy = pad.t + ((rect.height - pad.t - pad.b) * i / 3);
        ctx.beginPath();
        ctx.moveTo(pad.l, gy);
        ctx.lineTo(rect.width - pad.r, gy);
        ctx.stroke();
      }}
      ctx.fillStyle = "#8fa6b3";
      ctx.font = "11px ui-monospace, monospace";
      ctx.fillText(max.toFixed(2), 4, pad.t + 4);
      ctx.fillText(min.toFixed(2), 4, rect.height - pad.b);
      fields.forEach((field, idx) => {{
        ctx.strokeStyle = colors[idx];
        ctx.lineWidth = 2;
        ctx.beginPath();
        series.forEach((point, i) => {{
          const px = xs[i];
          const py = y(Number(point[field] ?? 0));
          if (i === 0) ctx.moveTo(px, py);
          else ctx.lineTo(px, py);
        }});
        ctx.stroke();
      }});
      const legend = labels || fields;
      legend.forEach((label, idx) => {{
        ctx.fillStyle = colors[idx];
        ctx.fillRect(pad.l + idx * 116, rect.height - 14, 14, 2);
        ctx.fillStyle = "#dce6eb";
        ctx.fillText(label, pad.l + 18 + idx * 116, rect.height - 10);
      }});
    }}
    function renderHero(runState, run) {{
      const sample = run.latest_sample || {{}};
      document.getElementById("focusTitle").textContent = run.focus_title || run.headline || "No current read.";
      document.getElementById("focusBody").textContent = run.focus_body || run.headline || "";
      const tags = [
        [`run:${{runState || "unknown"}}`, statusClass(runState)],
        [`state:${{run.state || "n/a"}}`, statusClass(run.state)],
        [`regime:${{run.regime || "n/a"}}`, statusClass(run.regime)],
        [`bias:${{(run.dominant_side || "n/a").toUpperCase()}}`, "warn"],
        [`scores L${{fmtNum(sample.long_score, 1)}} / S${{fmtNum(sample.short_score, 1)}}`, ""],
      ];
      if (sample.candidate_side) tags.push([`candidate:${{sample.candidate_side}} x${{sample.candidate_count || 0}}`, "warn"]);
      if (sample.candidate_profile) tags.push([`profile:${{sample.candidate_profile}}`, ""]);
      if (run.open_position) tags.push([`open:${{run.open_position.side}} #${{run.open_position.trade_id}}`, "good"]);
      if (run.open_position && run.open_position.trail_armed) tags.push(["trail:armed", "good"]);
      tags.push([`stale_skips:${{run.stale_quote_skips || 0}}`, ""]);
      tags.push([`reconnects:${{run.reconnects || 0}}`, run.reconnects ? "warn" : ""]);
      if (run.run_hours !== null && run.run_hours !== undefined) tags.push([`hours:${{fmtNum(run.run_hours, 2)}}`, ""]);
      document.getElementById("heroTags").innerHTML = tags.map(([label, cls]) => `<span class="tag ${{cls}}">${{label}}</span>`).join("");
    }}
    function renderCards(runState, run) {{
      const sample = run.latest_sample || {{}};
      const funnel = run.signal_funnel || {{}};
      const size = run.size_stats || {{}};
      const perf = run.performance || {{}};
      const cards = [
        ["Run State", runState, `state machine ${{run.state || "n/a"}}`],
        ["Regime", run.regime || "n/a", `dominant ${{(run.dominant_side || "n/a").toUpperCase()}}`],
        ["Current-Run PnL", fmtSigned(run.total_pnl, 3), `realized ${{fmtSigned(run.realized_pnl_total, 3)}}`],
        ["Trade Count", String(run.trade_count || 0), `win rate ${{fmtPct(run.win_rate_pct, 1)}}`],
        ["Signal Funnel", `${{funnel.confirmed || 0}} / ${{funnel.candidates || 0}}`, `confirm rate ${{fmtPct(funnel.confirm_rate_pct, 1)}}`],
        ["Scores", `L ${{fmtNum(sample.long_score, 1)}}`, `S ${{fmtNum(sample.short_score, 1)}}`],
        ["Latency", `${{fmtNum(run.p50_quote_age_ms, 0)}} / ${{fmtNum(run.p95_quote_age_ms, 0)}} ms`, "p50 / p95 quote age"],
        ["Spread", `${{fmtNum(run.p50_spread_bps, 2)}} / ${{fmtNum(run.p95_spread_bps, 2)}} bps`, "p50 / p95 spread"],
        ["Avg Notional", size.avg_notional ? `$${{fmtNum(size.avg_notional, 0)}}` : "n/a", `buying power ${{fmtPct(size.avg_notional_pct_buying_power, 1)}}`],
        ["Margin Use", size.avg_margin_pct_equity ? fmtPct(size.avg_margin_pct_equity, 1) : "n/a", `last ${{fmtPct(size.latest_margin_pct_equity, 1)}} of equity`],
        ["Profit Factor", perf.profit_factor ? fmtNum(perf.profit_factor, 2) : "n/a", `avg win ${{fmtSigned(perf.avg_win, 3)}} / avg loss ${{fmtSigned(perf.avg_loss, 3)}}`],
        ["Avg Realized bps", fmtSigned(run.avg_realized_bps, 2), `MFE ${{fmtNum(run.avg_mfe_bps, 2)}} / MAE ${{fmtNum(run.avg_mae_bps, 2)}}`],
        ["Avg Hold", `${{fmtNum(run.avg_hold_seconds, 1)}}s`, `capture ${{fmtNum(run.avg_capture_ratio, 2)}}`],
      ];
      document.getElementById("cards").innerHTML = cards.map(([label, value, meta]) => `
        <div class="card">
          <div class="label">${{label}}</div>
          <div class="value ${{statusClass(value)}}">${{value}}</div>
          <div class="meta">${{meta}}</div>
        </div>`).join("");
    }}
    function renderQuality(run) {{
      const sample = run.latest_sample || {{}};
      const metrics = [
        ["Bid / Ask", `${{fmtNum(sample.bid, 3)}} / ${{fmtNum(sample.ask, 3)}}`],
        ["Mid / Micro", `${{fmtNum(sample.mid, 3)}} / ${{fmtNum(sample.microprice, 3)}}`],
        ["Spread now / p95", `${{fmtNum(sample.spread_bps, 2)}} / ${{fmtNum(run.p95_spread_bps, 2)}} bps`],
        ["Quote age now / p95", `${{fmtNum(sample.quote_age_ms, 0)}} / ${{fmtNum(run.p95_quote_age_ms, 0)}} ms`],
        ["Transport now / p95", `${{fmtNum(sample.transport_delay_ms, 0)}} / ${{fmtNum(run.p95_transport_delay_ms, 0)}} ms`],
        ["Recent vol", `${{fmtNum(sample.recent_vol_bps, 2)}} bps`],
        ["Fast / confirm", `${{fmtSigned(sample.fast_impulse_bps, 2)}} / ${{fmtSigned(sample.confirm_impulse_bps, 2)}}`],
        ["Dynamic thresholds", `${{fmtNum(sample.dynamic_fast_threshold_bps, 2)}} / ${{fmtNum(sample.dynamic_confirm_threshold_bps, 2)}} bps`],
        ["Flow imbalance", fmtSigned(sample.flow_imbalance, 3)],
        ["Book imbalance", fmtSigned(sample.book_imbalance, 3)],
        ["Trade count / rate", `${{sample.trade_count || 0}} / ${{fmtNum(sample.trade_rate_per_second, 2)}}s-1`],
        ["Breakout range", `${{fmtNum(sample.breakout_low, 3)}} to ${{fmtNum(sample.breakout_high, 3)}}`],
      ];
      document.getElementById("qualityMetrics").innerHTML = metrics.map(([k, v]) => `
        <div class="metric"><div class="k">${{k}}</div><div class="v">${{v}}</div></div>`).join("");
    }}
    function renderFocus(run) {{
      const sample = run.latest_sample || {{}};
      const config = run.run_config || {{}};
      const dominantSide = run.dominant_side || "long";
      const details = (run.gate_details || {{}})[dominantSide] || [];
      const missing = run.dominant_missing || [];
      const tags = [
        [`dominant:${{dominantSide.toUpperCase()}}`, "warn"],
        [`score floor:${{fmtNum(config.setup_score_min, 0)}}`, ""],
        [`edge:${{fmtNum(config.score_edge_min, 1)}}`, ""],
        [`breakout buffer:${{fmtNum(config.breakout_buffer_bps, 2)}} bps`, ""],
        [`starter:${{fmtPct(Number(config.initial_notional_fraction || 0) * 100, 1)}} of buying power`, ""],
        [`runner core:${{fmtPct(Number(config.runner_core_fraction || 0) * 100, 1)}} of peak`, ""],
        [`min trades:${{fmtNum(config.min_trade_count, 0)}}`, ""],
      ];
      if (sample.spread_ok === false) tags.push(["spread blocked", "bad"]);
      if (sample.extreme_blocked) tags.push(["extreme handoff", "bad"]);
      if (!missing.length) tags.push(["all dominant gates aligned", "good"]);
      document.getElementById("focusTags").innerHTML = tags.map(([label, cls]) => `<span class="tag ${{cls}}">${{label}}</span>`).join("");
      document.getElementById("focusDetails").innerHTML = details.map(row => `
        <div class="focus-row ${{row.ok ? "pass" : "wait"}}">
          <div>${{row.label}}</div>
          <div>${{row.current}}</div>
          <div>${{row.target}}</div>
          <div class="${{row.ok ? "good" : "warn"}}">${{row.ok ? "pass" : `gap ${{fmtNum(row.gap, 2)}}`}}</div>
        </div>`).join("");
      document.getElementById("contextNote").textContent = run.headline || "";
    }}
    function renderSizing(run) {{
      const config = run.run_config || {{}};
      const size = run.size_stats || {{}};
      const metrics = [
        ["Leverage", `${{fmtNum(config.leverage, 0)}}x`],
        ["Risk per trade", fmtPct(config.equity_risk_pct, 1)],
        ["Avg starter", size.avg_starter_notional ? `$${{fmtNum(size.avg_starter_notional, 0)}}` : "n/a"],
        ["Avg peak notional", size.avg_peak_notional ? `$${{fmtNum(size.avg_peak_notional, 0)}}` : "n/a"],
        ["Max peak notional", size.max_peak_notional ? `$${{fmtNum(size.max_peak_notional, 0)}}` : "n/a"],
        ["Last notional", size.latest_notional ? `$${{fmtNum(size.latest_notional, 0)}}` : "n/a"],
        ["Avg size", fmtNum(size.avg_size, 2)],
        ["Avg margin", size.avg_margin ? `$${{fmtNum(size.avg_margin, 0)}}` : "n/a"],
        ["Avg margin / equity", fmtPct(size.avg_margin_pct_equity, 1)],
        ["Last margin / equity", fmtPct(size.latest_margin_pct_equity, 1)],
        ["Avg peak / buying power", fmtPct(size.avg_peak_notional_pct_buying_power, 1)],
        ["Last notional / buying power", fmtPct(size.latest_notional_pct_buying_power, 1)],
        ["Avg add-ons", fmtNum(size.avg_add_on_count, 2)],
        ["Avg de-risks", fmtNum(size.avg_reduction_count, 2)],
        ["Max trades / hour", fmtNum(config.max_trades_per_hour, 0)],
        ["Cooldown", `${{fmtNum(config.cooldown_seconds, 0)}}s`],
        ["Sample cadence", `${{fmtNum(config.sample_ms, 0)}} ms`],
      ];
      document.getElementById("sizingMetrics").innerHTML = metrics.map(([k, v]) => `
        <div class="metric"><div class="k">${{k}}</div><div class="v">${{v}}</div></div>`).join("");
      let note = "This engine should enter smaller, add only once the move proves itself, then de-risk the extra risk while leaving a runner on.";
      if (size.avg_peak_notional_pct_buying_power) {{
        note = `Average peak deployed notional has used ${{fmtPct(size.avg_peak_notional_pct_buying_power, 1)}} of buying power, while the average starter has remained much smaller. That separation is the core runner discipline.`;
      }}
      document.getElementById("sizingNote").textContent = note;
    }}
    function renderExecution(run) {{
      const perf = run.performance || {{}};
      const metrics = [
        ["Expectancy", fmtSigned(perf.expectancy, 3)],
        ["Profit factor", perf.profit_factor ? fmtNum(perf.profit_factor, 2) : "n/a"],
        ["Avg win", fmtSigned(perf.avg_win, 3)],
        ["Avg loss", fmtSigned(perf.avg_loss, 3)],
        ["Wins / losses", `${{perf.wins || 0}} / ${{perf.losses || 0}}`],
        ["Avg add-ons / de-risks", `${{fmtNum(perf.avg_add_ons, 2)}} / ${{fmtNum(perf.avg_reductions, 2)}}`],
        ["Profiles", Object.entries(perf.profiles || {{}}).map(([k, v]) => `${{k}}:${{v}}`).join(" | ") || "n/a"],
      ];
      document.getElementById("executionMetrics").innerHTML = metrics.map(([k, v]) => `
        <div class="metric"><div class="k">${{k}}</div><div class="v">${{v}}</div></div>`).join("");
      let note = "This engine should earn its keep by waiting for a real breakout, risking a small starter, and only pyramiding when the tape keeps proving itself.";
      if ((run.signal_funnel || {{}}).candidates) {{
        note = `Current funnel is ${{run.signal_funnel.confirmed || 0}} confirmed from ${{run.signal_funnel.candidates || 0}} raw candidates. Confirmations should be selective, but entries should now be breakout-led rather than eager pressure chases.`;
      }}
      document.getElementById("executionNote").textContent = note;
    }}
    function renderExposure(run) {{
      const regime = run.regime_share || {{}};
      const state = run.state_share || {{}};
      const renderBars = (id, items) => {{
        document.getElementById(id).innerHTML = items.map(([label, value]) => `
          <div class="bar-row ${{value > 60 ? "bad" : value > 40 ? "warn" : ""}}">
            <div>${{label}}</div>
            <div class="bar-track"><div class="bar-fill" style="width:${{Math.max(0, Math.min(100, value))}}%"></div></div>
            <div>${{fmtPct(value, 1)}}</div>
          </div>`).join("");
      }};
      renderBars("regimeBars", Object.entries(regime).sort((a, b) => b[1] - a[1]));
      renderBars("stateBars", Object.entries(state).sort((a, b) => b[1] - a[1]));
    }}
    function renderPassRates(run) {{
      const rates = run.gate_pass_rates || {{}};
      const renderSide = (id, rows) => {{
        document.getElementById(id).innerHTML = (rows || []).map((row) => `
          <div class="bar-row ${{row.rate_pct < 15 ? "bad" : row.rate_pct < 35 ? "warn" : ""}}">
            <div>${{row.label}}</div>
            <div class="bar-track"><div class="bar-fill" style="width:${{Math.max(0, Math.min(100, row.rate_pct || 0))}}%"></div></div>
            <div>${{fmtPct(row.rate_pct, 1)}}</div>
          </div>`).join("");
      }};
      renderSide("longPassRates", rates.long || []);
      renderSide("shortPassRates", rates.short || []);
    }}
    function renderGateTables(run) {{
      const sample = run.latest_sample || {{}};
      document.getElementById("longScore").textContent = fmtNum(sample.long_score, 1);
      document.getElementById("shortScore").textContent = fmtNum(sample.short_score, 1);
      const renderSide = (id, rows) => {{
        document.getElementById(id).innerHTML = (rows || []).map(row => `
          <tr>
            <td>${{row.label}}</td>
            <td>${{row.current}}</td>
            <td>${{row.target}}</td>
            <td>${{fmtNum(row.gap, 2)}}</td>
            <td class="gate-state ${{row.ok ? "good" : "warn"}}">${{row.ok ? "pass" : "wait"}}</td>
          </tr>`).join("");
      }};
      renderSide("longGates", (run.gate_details || {{}}).long || []);
      renderSide("shortGates", (run.gate_details || {{}}).short || []);
    }}
    function renderFunnel(run) {{
      const funnel = run.signal_funnel || {{}};
      const steps = [
        ["Candidates", funnel.candidates || 0, "all raw signal candidates"],
        ["Confirmed", funnel.confirmed || 0, `confirm rate ${{fmtPct(funnel.confirm_rate_pct, 1)}}`],
        ["Entries", funnel.entries || 0, `entry rate ${{fmtPct(funnel.entry_rate_pct, 1)}}`],
        ["Exits", funnel.exits || 0, "closed trade count"],
      ];
      document.getElementById("funnel").innerHTML = steps.map(([label, num, meta]) => `
        <div class="funnel-step">
          <div class="label">${{label}}</div>
          <div class="num">${{num}}</div>
          <div class="meta">${{meta}}</div>
        </div>`).join("");

      const renderBlockers = (id, items) => {{
        const rows = items && items.length ? items : [{{label: "No persistent blocker yet", count: 0}}];
        document.getElementById(id).innerHTML = rows.map(row => `
          <div class="blocker-row">
            <div>${{row.label}}</div>
            <div class="${{row.count ? "warn" : "good"}}">${{row.count}}</div>
          </div>`).join("");
      }};
      renderBlockers("longBlockers", (run.blocker_snapshot || {{}}).long || []);
      renderBlockers("shortBlockers", (run.blocker_snapshot || {{}}).short || []);
    }}
    function renderTrades(run) {{
      document.getElementById("tradesTable").innerHTML = (run.recent_trades || []).map(row => `
        <tr>
          <td>${{row.trade_id || ""}}</td>
          <td>${{row.side || ""}}</td>
          <td>${{row.entry_profile || "legacy"}}</td>
          <td>$${{fmtNum(row.starter_notional, 0)}}</td>
          <td>$${{fmtNum(row.max_notional_seen || row.notional, 0)}}</td>
          <td>$${{fmtNum(row.required_margin, 0)}}</td>
          <td>${{row.add_on_count || 0}} / ${{row.reduction_count || 0}}</td>
          <td class="${{Number(row.realized_pnl || 0) >= 0 ? "good" : "bad"}}">${{fmtSigned(row.realized_pnl, 3)}}</td>
          <td>${{fmtSigned(row.realized_bps, 2)}}</td>
          <td>${{fmtNum(row.hold_seconds, 1)}}s</td>
          <td>${{row.exit_reason || ""}}</td>
        </tr>`).join("");
      document.getElementById("markoutsTable").innerHTML = (run.recent_markouts || []).map(row => `
        <tr>
          <td>${{row.trade_id || ""}}</td>
          <td>${{row.side || ""}}</td>
          <td>${{fmtSigned(row.mfe_bps, 2)}}</td>
          <td>${{fmtSigned(row.mae_bps, 2)}}</td>
          <td>${{fmtNum(row.capture_ratio, 2)}}</td>
          <td>${{row.entry_reason || ""}}</td>
          <td>${{row.exit_reason || ""}}</td>
        </tr>`).join("");
    }}
    async function refresh() {{
      const res = await fetch("/api/summary");
      const data = await res.json();
      const run = data.current_run || {{}};
      document.getElementById("stamp").innerHTML = `Updated ${{data.generated_at_utc || ""}}<br/>Run start ${{run.start_utc || "n/a"}}`;
      renderHero(data.run_state || "unknown", run);
      renderCards(data.run_state || "unknown", run);
      renderQuality(run);
      renderFocus(run);
      renderSizing(run);
      renderExecution(run);
      renderExposure(run);
      renderPassRates(run);
      renderGateTables(run);
      renderFunnel(run);
      renderTrades(run);
      drawChart("pnlChart", run.pnl_series || [], ["realized_pnl", "total_pnl"], ["#69efcf", "#63d8ff"], null, null, ["realized", "total"]);
      drawChart("scoreChart", run.score_series || [], ["long_score", "short_score"], ["#78efbd", "#ff8e9f"], 0, 100, ["long", "short"]);
    }}
    const VISIBLE_REFRESH_MS = 5000;
    const HIDDEN_REFRESH_MS = 30000;
    let refreshTimer = null;
    let refreshInFlight = false;

    const scheduleRefresh = (delayMs) => {{
      if (refreshTimer) {{
        clearTimeout(refreshTimer);
      }}
      refreshTimer = setTimeout(() => {{
        refreshLoop().catch(console.error);
      }}, delayMs);
    }};

    async function refreshLoop(force = false) {{
      if (refreshInFlight) {{
        scheduleRefresh(VISIBLE_REFRESH_MS);
        return;
      }}
      if (document.hidden && !force) {{
        scheduleRefresh(HIDDEN_REFRESH_MS);
        return;
      }}
      refreshInFlight = true;
      try {{
        await refresh();
      }} finally {{
        refreshInFlight = false;
        scheduleRefresh(document.hidden ? HIDDEN_REFRESH_MS : VISIBLE_REFRESH_MS);
      }}
    }}

    document.addEventListener('visibilitychange', () => {{
      if (!document.hidden) {{
        refreshLoop(true).catch(console.error);
      }}
    }});

    refreshLoop(true).catch(console.error);
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    events_path: Path
    samples_path: Path
    trades_path: Path
    markouts_path: Path
    reports_dir: Path
    summary_cache: SummaryCache

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/summary":
            body = self.summary_cache.get_json_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/":
            body = render_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> None:
    args = parse_args()
    Handler.events_path = Path(args.events_jsonl)
    Handler.samples_path = Path(args.samples_jsonl)
    Handler.trades_path = Path(args.trades_csv)
    Handler.markouts_path = Path(args.markouts_jsonl)
    Handler.reports_dir = Path(args.reports_dir)
    Handler.summary_cache = SummaryCache(
        build_summary,
        Handler.events_path,
        Handler.samples_path,
        Handler.trades_path,
        Handler.markouts_path,
        Handler.reports_dir,
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Mid momentum dashboard running on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

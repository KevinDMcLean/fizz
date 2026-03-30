#!/usr/bin/env python3
"""Dashboard for Kevin Hype Liquidity Engine."""

from __future__ import annotations

import argparse
import bisect
import csv
import io
import json
import math
from collections import Counter, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List
from urllib.parse import urlparse

from hyperliquid_fee_model import HyperliquidFeeConfig, fee_rates_for_tier

APP_NAME = "Kevin Hype Liquidity Engine"
REPO_ROOT = Path(__file__).resolve().parents[1]
KEVIN_PROFILE_DIR = REPO_ROOT / "config" / "kevin_hype"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local dashboard for Kevin Hype Liquidity Engine")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8795)
    parser.add_argument("--events-jsonl", default="logs/kevin_hype_events.jsonl")
    parser.add_argument("--samples-jsonl", default="logs/kevin_hype_samples.jsonl")
    parser.add_argument("--fills-csv", default="logs/kevin_hype_fills.csv")
    parser.add_argument("--trades-csv", default="logs/kevin_hype_trades.csv")
    parser.add_argument("--reports-dir", default="logs/kevin_hype_reports")
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


def _trade_turnover_notional(row: Dict[str, Any]) -> float:
    entry_price = _to_float(row.get("entry_price_avg")) or 0.0
    exit_price = _to_float(row.get("exit_price_avg")) or 0.0
    qty = abs(_to_float(row.get("max_abs_qty")) or 0.0)
    return (entry_price + exit_price) * qty


def _loss_bucket_label(bucket: str) -> str:
    labels = {
        "immediate_adverse_entry": "Immediate adverse entry",
        "immediate_kill_markout": "Immediate kill markout",
        "kill_markout": "Kill markout",
        "kill_timeout": "Kill timeout",
        "kill_toxic_reversal": "Kill toxic reversal",
        "slow_bleed": "Slow bleed",
        "never_worked": "Never worked",
        "long_flow_flip": "Long flow flip",
        "short_flow_flip": "Short flow flip",
        "other_loss": "Other loss",
    }
    return labels.get(bucket, bucket.replace("_", " "))


def _classify_loss_bucket(row: Dict[str, Any]) -> str | None:
    realized_pnl = _to_float(row.get("realized_pnl")) or 0.0
    if realized_pnl >= 0.0:
        return None
    close_reason = str(row.get("close_reason") or "")
    hold_seconds = _to_float(row.get("hold_seconds")) or 0.0
    best_markout_bps = _to_float(row.get("best_markout_bps")) or 0.0
    worst_markout_bps = _to_float(row.get("worst_markout_bps")) or 0.0
    side = str(row.get("side") or "").upper()

    if close_reason == "kill_timeout":
        return "kill_timeout"
    if close_reason == "kill_toxic_reversal":
        return "kill_toxic_reversal"
    if close_reason == "kill_markout":
        return "immediate_kill_markout" if hold_seconds <= 0.25 else "kill_markout"
    if hold_seconds <= 0.35:
        return "immediate_adverse_entry"
    if hold_seconds >= 8.0:
        return "slow_bleed"
    if best_markout_bps <= 0.10 and worst_markout_bps <= -0.75:
        return "never_worked"
    if side == "LONG":
        return "long_flow_flip"
    if side == "SHORT":
        return "short_flow_flip"
    return "other_loss"


def _summarize_loss_buckets(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in trades:
        bucket = _classify_loss_bucket(row)
        if bucket is None:
            continue
        item = buckets.setdefault(
            bucket,
            {
                "bucket": bucket,
                "label": _loss_bucket_label(bucket),
                "count": 0,
                "realized_pnl": 0.0,
                "turnover_notional": 0.0,
                "long_count": 0,
                "short_count": 0,
            },
        )
        item["count"] += 1
        item["realized_pnl"] += _to_float(row.get("realized_pnl")) or 0.0
        item["turnover_notional"] += _trade_turnover_notional(row)
        if str(row.get("side") or "").upper() == "LONG":
            item["long_count"] += 1
        elif str(row.get("side") or "").upper() == "SHORT":
            item["short_count"] += 1
    return sorted(
        buckets.values(),
        key=lambda item: (abs(float(item["realized_pnl"])), int(item["count"])),
        reverse=True,
    )


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
        closed_turnover += _trade_turnover_notional(row)

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


def _read_json_file(path: Path) -> Dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _venue_name_from_api(api_url: str | None) -> str:
    raw = (api_url or "").lower()
    if "hyperliquid" in raw:
        return "Hyperliquid"
    if not raw:
        return "n/a"
    return api_url or "n/a"


def _available_kevin_profiles() -> List[Dict[str, Any]]:
    profiles: List[Dict[str, Any]] = []
    if not KEVIN_PROFILE_DIR.exists():
        return profiles
    for profile_path in sorted(KEVIN_PROFILE_DIR.glob("*.json")):
        payload = _read_json_file(profile_path) or {}
        profiles.append(
            {
                "slug": profile_path.stem,
                "market_name": payload.get("market_name") or profile_path.stem.upper(),
                "asset": payload.get("asset") or payload.get("market_name") or profile_path.stem.upper(),
                "dashboard_port": payload.get("dashboard_port"),
                "profile_file": str(profile_path),
            }
        )
    return profiles


class _BinnedStats:
    def __init__(self, resolution: float) -> None:
        self.resolution = resolution
        self._buckets: Counter[int] = Counter()
        self._count = 0
        self._total = 0.0
        self._max_value: float | None = None

    @property
    def count(self) -> int:
        return self._count

    def observe(self, value: Any) -> None:
        number = _to_float(value)
        if number is None:
            return
        bucket = int(round(number / self.resolution)) if self.resolution > 0 else int(round(number))
        self._buckets[bucket] += 1
        self._count += 1
        self._total += number
        if self._max_value is None or number > self._max_value:
            self._max_value = number

    def mean(self) -> float | None:
        if self._count <= 0:
            return None
        return self._total / self._count

    def percentile(self, q: float) -> float | None:
        if self._count <= 0:
            return None
        target = max(1, math.ceil(self._count * q))
        running = 0
        for bucket in sorted(self._buckets):
            running += self._buckets[bucket]
            if running >= target:
                return bucket * self.resolution
        last_bucket = max(self._buckets) if self._buckets else 0
        return last_bucket * self.resolution

    def max_value(self) -> float | None:
        return self._max_value


class DashboardSummaryCache:
    RECENT_ROWS = 20
    CHART_POINTS_LIMIT = 960

    def __init__(
        self,
        events_path: Path,
        samples_path: Path,
        fills_path: Path,
        trades_path: Path,
        reports_dir: Path,
    ) -> None:
        self.events_path = events_path
        self.samples_path = samples_path
        self.fills_path = fills_path
        self.trades_path = trades_path
        self.reports_dir = reports_dir
        self.run_dir = reports_dir.parent
        self.manifest_path = self.run_dir / "run_manifest.json"
        self._lock = Lock()
        self._initialized = False
        self._reset_all_state()

    def _reset_all_state(self) -> None:
        self._events_offset = 0
        self._samples_offset = 0
        self._fills_offset = 0
        self._trades_offset = 0
        self._fills_fieldnames: list[str] | None = None
        self._trades_fieldnames: list[str] | None = None
        self.run_count = 0
        self.last_started: Dict[str, Any] | None = None
        self.last_started_ts: datetime | None = None
        self.run_state = "unknown"
        self.latest_report: str | None = None
        self._reset_run_state()

    def _reset_run_state(self) -> None:
        self.event_counts: Counter[str] = Counter()
        self.latest_sample: Dict[str, Any] = {}
        self.latest_sample_ts: datetime | None = None
        self.latest_sample_meta: Dict[str, float | None] | None = None
        self.latest_fill: Dict[str, Any] = {}
        self.quote_mode_counts: Counter[str] = Counter()
        self.quote_reason_counts: Counter[str] = Counter()
        self.quote_age_stats = _BinnedStats(1.0)
        self.transport_stats = _BinnedStats(1.0)
        self.spread_stats = _BinnedStats(0.001)
        self.active_quote_notional_stats = _BinnedStats(1.0)
        self.passive_fill_notional_stats = _BinnedStats(1.0)
        self.touch_share_stats = _BinnedStats(0.001)
        self.queue_ahead_stats = _BinnedStats(0.1)
        self.passive_fill_under_10pct_touch_count = 0
        self.passive_fill_under_25pct_touch_count = 0
        self.passive_fill_over_50pct_touch_count = 0
        self.fill_turnover_notional = 0.0
        self.fill_turnover_units = 0.0
        self.closed_turnover_notional = 0.0
        self.fills_count = 0
        self.passive_fills_count = 0
        self.aggressive_fills_count = 0
        self.kill_fills_count = 0
        self.closed_episodes = 0
        self.realized_pnl_total = 0.0
        self.gross_realized_pnl_total = 0.0
        self.closed_fee_drag_total = 0.0
        self.fill_fee_drag_total = 0.0
        self.fill_fee_cost_total = 0.0
        self.fill_rebate_total = 0.0
        self.maker_fee_cost_total = 0.0
        self.taker_fee_cost_total = 0.0
        self.maker_rebates_total = 0.0
        self.maker_notional_total = 0.0
        self.taker_notional_total = 0.0
        self.pnl_positive = 0.0
        self.pnl_negative = 0.0
        self.wins = 0
        self.losses = 0
        self.flats = 0
        self.avg_hold_seconds_sum = 0.0
        self.avg_best_markout_sum = 0.0
        self.realized_spread_sum = 0.0
        self.realized_spread_count = 0
        self.passive_realized_pnl_total = 0.0
        self.kill_realized_pnl_total = 0.0
        self.passive_closed_turnover = 0.0
        self.kill_closed_turnover = 0.0
        self.kill_episode_count = 0
        self.passive_episode_count = 0
        self.long_episode_count = 0
        self.short_episode_count = 0
        self.long_loss_count = 0
        self.short_loss_count = 0
        self.long_loss_pnl_total = 0.0
        self.short_loss_pnl_total = 0.0
        self.long_loss_turnover_total = 0.0
        self.short_loss_turnover_total = 0.0
        self.loss_bucket_totals: Dict[str, Dict[str, Any]] = {}
        self.event_regime_samples = 0
        self.inventory_protection_samples = 0
        self.recent_fills = deque(maxlen=self.RECENT_ROWS)
        self.recent_trades = deque(maxlen=self.RECENT_ROWS)
        self.chart_points: List[Dict[str, Any]] = []
        self.chart_realized_running = 0.0
        self.chart_gross_realized_running = 0.0
        self.current_unrealized = 0.0

    def _refresh_latest_report(self) -> str | None:
        if not self.reports_dir.exists():
            return None
        reports = sorted(self.reports_dir.glob("*.md"))
        if not reports:
            return None
        return str(reports[-1])

    def _is_current_ts(self, ts: datetime | None) -> bool:
        if self.last_started_ts is None:
            return True
        return ts is not None and ts >= self.last_started_ts

    def _parse_json_line(self, line: str) -> Dict[str, Any] | None:
        line = line.strip()
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None

    def _file_changed_unexpectedly(self, path: Path, offset: int) -> bool:
        if offset <= 0:
            return False
        if not path.exists():
            return True
        try:
            return path.stat().st_size < offset
        except OSError:
            return True

    def _rebuild(self) -> None:
        self._reset_all_state()
        self.latest_report = self._refresh_latest_report()
        self._initial_scan_events()
        self._initial_scan_streams()
        self._initialized = True

    def _initial_scan_events(self) -> None:
        if self.events_path.exists():
            with self.events_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    row = self._parse_json_line(line)
                    if row is None:
                        continue
                    event = str(row.get("event") or "")
                    ts = _parse_utc(row.get("timestamp_utc"))
                    if event == "bot_started":
                        self.run_count += 1
                        self.last_started = row
                        self.last_started_ts = ts
                        self.run_state = "running"
                    elif event == "bot_stopped" and self.last_started_ts is not None and ts is not None and ts >= self.last_started_ts:
                        self.run_state = "stopped"
                self._events_offset = fh.tell()

            with self.events_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    row = self._parse_json_line(line)
                    if row is None:
                        continue
                    event = str(row.get("event") or "unknown")
                    ts = _parse_utc(row.get("timestamp_utc"))
                    if event != "market_tick" and self._is_current_ts(ts):
                        self.event_counts[event] += 1
                        if event == "bot_stopped":
                            self.run_state = "stopped"

    def _next_current_csv_row(
        self,
        reader: csv.DictReader[str],
        *,
        ts_field: str,
    ) -> tuple[Dict[str, str] | None, datetime | None]:
        for row in reader:
            ts = _parse_utc(row.get(ts_field))
            if self._is_current_ts(ts):
                return row, ts
        return None, None

    def _append_chart_point(self, ts: datetime | None) -> None:
        if ts is None:
            return
        point = {
            "timestamp_utc": ts.isoformat(),
            "gross_realized_pnl": round(self.chart_gross_realized_running, 8),
            "realized_pnl": round(self.chart_realized_running, 8),
            "fee_drag_pnl": round(self.chart_gross_realized_running - self.chart_realized_running, 8),
            "unrealized_pnl": round(self.current_unrealized, 8),
            "total_pnl": round(self.chart_realized_running + self.current_unrealized, 8),
        }
        if self.chart_points and self.chart_points[-1]["timestamp_utc"] == point["timestamp_utc"]:
            self.chart_points[-1] = point
        else:
            self.chart_points.append(point)
        if len(self.chart_points) > self.CHART_POINTS_LIMIT:
            reduced = self.chart_points[::2]
            if reduced[-1] != self.chart_points[-1]:
                reduced.append(self.chart_points[-1])
            self.chart_points = reduced

    def _apply_sample_row(self, row: Dict[str, Any], ts: datetime | None) -> None:
        self.latest_sample = row
        self.latest_sample_ts = ts
        self.current_unrealized = _to_float(row.get("inventory_unrealized_pnl")) or 0.0
        self.quote_mode_counts[str(row.get("quote_mode") or "unknown")] += 1
        self.quote_reason_counts[str(row.get("quoting_reason") or "unknown")] += 1
        if row.get("event_regime"):
            self.event_regime_samples += 1
        if row.get("quoting_reason") == "inventory_protection":
            self.inventory_protection_samples += 1
        self.quote_age_stats.observe(row.get("quote_age_ms"))
        self.transport_stats.observe(row.get("transport_delay_ms"))
        self.spread_stats.observe(row.get("spread_bps"))
        for side in ("bid", "ask"):
            price = _to_float(row.get(f"live_{side}_order_price"))
            size = _to_float(row.get(f"live_{side}_order_size"))
            if price and size:
                self.active_quote_notional_stats.observe(abs(price * size))
        self.latest_sample_meta = {
            "bid_depth": _to_float(row.get("bid_depth")),
            "ask_depth": _to_float(row.get("ask_depth")),
            "bid_queue_ahead_size": _to_float(row.get("bid_queue_ahead_size")),
            "ask_queue_ahead_size": _to_float(row.get("ask_queue_ahead_size")),
        }
        self._append_chart_point(ts)

    def _apply_fill_row(self, row: Dict[str, str]) -> None:
        self.latest_fill = row
        self.recent_fills.append(row)
        self.fills_count += 1
        price = _to_float(row.get("price")) or 0.0
        size = abs(_to_float(row.get("size")) or 0.0)
        notional = abs(price * size)
        self.fill_turnover_notional += notional
        self.fill_turnover_units += size
        fee_delta = _to_float(row.get("exchange_fee_delta")) or 0.0
        self.fill_fee_drag_total += fee_delta
        self.fill_fee_cost_total += max(fee_delta, 0.0)
        self.fill_rebate_total += max(-fee_delta, 0.0)
        role = str(row.get("liquidity_role") or "")
        reason = str(row.get("reason") or "")
        if role == "passive":
            self.passive_fills_count += 1
            self.passive_fill_notional_stats.observe(notional)
            if self.latest_sample_meta is not None:
                sample_side = "ask" if str(row.get("side") or "").upper() == "SELL" else "bid"
                depth = self.latest_sample_meta.get(f"{sample_side}_depth")
                queue = self.latest_sample_meta.get(f"{sample_side}_queue_ahead_size")
                if depth is not None and depth > 0:
                    touch_share = size / depth
                    self.touch_share_stats.observe(touch_share)
                    if touch_share <= 0.10:
                        self.passive_fill_under_10pct_touch_count += 1
                    if touch_share <= 0.25:
                        self.passive_fill_under_25pct_touch_count += 1
                    if touch_share > 0.50:
                        self.passive_fill_over_50pct_touch_count += 1
                if queue is not None:
                    self.queue_ahead_stats.observe(queue)
        elif role == "aggressive":
            self.aggressive_fills_count += 1
            if not reason.startswith("finalize"):
                self.kill_fills_count += 1

    def _apply_trade_row(self, row: Dict[str, str], ts: datetime | None) -> None:
        self.recent_trades.append(row)
        self.closed_episodes += 1
        realized = _to_float(row.get("realized_pnl")) or 0.0
        gross = _to_float(row.get("gross_pnl")) or 0.0
        fees_paid = _to_float(row.get("fees_paid")) or 0.0
        maker_fee_cost = _to_float(row.get("maker_fee_cost")) or 0.0
        taker_fee_cost = _to_float(row.get("taker_fee_cost")) or 0.0
        maker_rebates = _to_float(row.get("maker_rebates")) or 0.0
        maker_notional = _to_float(row.get("maker_notional")) or 0.0
        taker_notional = _to_float(row.get("taker_notional")) or 0.0
        hold_seconds = _to_float(row.get("hold_seconds")) or 0.0
        best_markout_bps = _to_float(row.get("best_markout_bps")) or 0.0
        close_reason = str(row.get("close_reason") or "")
        side = str(row.get("side") or "").upper()
        turnover = _trade_turnover_notional(row)

        self.realized_pnl_total += realized
        self.gross_realized_pnl_total += gross
        self.closed_fee_drag_total += fees_paid
        self.maker_fee_cost_total += maker_fee_cost
        self.taker_fee_cost_total += taker_fee_cost
        self.maker_rebates_total += maker_rebates
        self.maker_notional_total += maker_notional
        self.taker_notional_total += taker_notional
        self.closed_turnover_notional += turnover
        self.avg_hold_seconds_sum += hold_seconds
        self.avg_best_markout_sum += best_markout_bps
        if realized > 0.0:
            self.wins += 1
            self.pnl_positive += realized
        elif realized < 0.0:
            self.losses += 1
            self.pnl_negative += realized
        else:
            self.flats += 1
        entry_price = _to_float(row.get("entry_price_avg")) or 0.0
        max_abs_qty = abs(_to_float(row.get("max_abs_qty")) or 0.0)
        entry_notional = entry_price * max_abs_qty
        if entry_notional > 0:
            self.realized_spread_sum += (realized / entry_notional) * 10_000.0
            self.realized_spread_count += 1

        if close_reason.startswith("kill"):
            self.kill_episode_count += 1
            self.kill_realized_pnl_total += realized
            self.kill_closed_turnover += turnover
        else:
            self.passive_episode_count += 1
            self.passive_realized_pnl_total += realized
            self.passive_closed_turnover += turnover

        if side == "LONG":
            self.long_episode_count += 1
            if realized < 0.0:
                self.long_loss_count += 1
                self.long_loss_pnl_total += realized
                self.long_loss_turnover_total += turnover
        elif side == "SHORT":
            self.short_episode_count += 1
            if realized < 0.0:
                self.short_loss_count += 1
                self.short_loss_pnl_total += realized
                self.short_loss_turnover_total += turnover

        bucket = _classify_loss_bucket(row)
        if bucket is not None:
            item = self.loss_bucket_totals.setdefault(
                bucket,
                {
                    "bucket": bucket,
                    "label": _loss_bucket_label(bucket),
                    "count": 0,
                    "realized_pnl": 0.0,
                    "turnover_notional": 0.0,
                    "long_count": 0,
                    "short_count": 0,
                },
            )
            item["count"] += 1
            item["realized_pnl"] += realized
            item["turnover_notional"] += turnover
            if side == "LONG":
                item["long_count"] += 1
            elif side == "SHORT":
                item["short_count"] += 1

        self.chart_realized_running += realized
        self.chart_gross_realized_running += gross
        self._append_chart_point(ts)

    def _initial_scan_streams(self) -> None:
        fills_fh = self.fills_path.open("r", encoding="utf-8", newline="") if self.fills_path.exists() else None
        trades_fh = self.trades_path.open("r", encoding="utf-8", newline="") if self.trades_path.exists() else None
        samples_fh = self.samples_path.open("r", encoding="utf-8") if self.samples_path.exists() else None
        try:
            fill_reader = csv.DictReader(fills_fh) if fills_fh is not None else None
            trade_reader = csv.DictReader(trades_fh) if trades_fh is not None else None
            self._fills_fieldnames = list(fill_reader.fieldnames or []) if fill_reader is not None else []
            self._trades_fieldnames = list(trade_reader.fieldnames or []) if trade_reader is not None else []
            fill_row, fill_ts = self._next_current_csv_row(fill_reader, ts_field="timestamp_utc") if fill_reader is not None else (None, None)
            trade_row, trade_ts = self._next_current_csv_row(trade_reader, ts_field="close_time_utc") if trade_reader is not None else (None, None)
            if samples_fh is not None:
                for line in samples_fh:
                    row = self._parse_json_line(line)
                    if row is None:
                        continue
                    ts = _parse_utc(row.get("timestamp_utc"))
                    if not self._is_current_ts(ts):
                        continue
                    while trade_row is not None and trade_ts is not None and ts is not None and trade_ts <= ts:
                        self._apply_trade_row(trade_row, trade_ts)
                        trade_row, trade_ts = self._next_current_csv_row(trade_reader, ts_field="close_time_utc")
                    while fill_row is not None and fill_ts is not None and ts is not None and fill_ts < ts:
                        self._apply_fill_row(fill_row)
                        fill_row, fill_ts = self._next_current_csv_row(fill_reader, ts_field="timestamp_utc")
                    self._apply_sample_row(row, ts)
                self._samples_offset = samples_fh.tell()
            while trade_row is not None:
                self._apply_trade_row(trade_row, trade_ts)
                trade_row, trade_ts = self._next_current_csv_row(trade_reader, ts_field="close_time_utc")
            while fill_row is not None:
                self._apply_fill_row(fill_row)
                fill_row, fill_ts = self._next_current_csv_row(fill_reader, ts_field="timestamp_utc")
            if fills_fh is not None:
                self._fills_offset = fills_fh.tell()
            if trades_fh is not None:
                self._trades_offset = trades_fh.tell()
        finally:
            if fills_fh is not None:
                fills_fh.close()
            if trades_fh is not None:
                trades_fh.close()
            if samples_fh is not None:
                samples_fh.close()

    def _read_new_events(self) -> bool:
        if not self.events_path.exists():
            return False
        with self.events_path.open("r", encoding="utf-8") as fh:
            fh.seek(self._events_offset)
            for line in fh:
                row = self._parse_json_line(line)
                if row is None:
                    continue
                event = str(row.get("event") or "")
                ts = _parse_utc(row.get("timestamp_utc"))
                if event == "bot_started":
                    return True
                if event != "market_tick" and self._is_current_ts(ts):
                    self.event_counts[event] += 1
                    if event == "bot_stopped":
                        self.run_state = "stopped"
            self._events_offset = fh.tell()
        return False

    def _process_incremental_streams(self) -> None:
        fills_fh = self.fills_path.open("r", encoding="utf-8", newline="") if self.fills_path.exists() else None
        trades_fh = self.trades_path.open("r", encoding="utf-8", newline="") if self.trades_path.exists() else None
        samples_fh = self.samples_path.open("r", encoding="utf-8") if self.samples_path.exists() else None
        try:
            fill_reader = None
            trade_reader = None
            if fills_fh is not None:
                fills_fh.seek(self._fills_offset)
                fill_reader = csv.DictReader(fills_fh, fieldnames=self._fills_fieldnames)
            if trades_fh is not None:
                trades_fh.seek(self._trades_offset)
                trade_reader = csv.DictReader(trades_fh, fieldnames=self._trades_fieldnames)
            fill_row, fill_ts = self._next_current_csv_row(fill_reader, ts_field="timestamp_utc") if fill_reader is not None else (None, None)
            trade_row, trade_ts = self._next_current_csv_row(trade_reader, ts_field="close_time_utc") if trade_reader is not None else (None, None)
            if samples_fh is not None:
                samples_fh.seek(self._samples_offset)
                for line in samples_fh:
                    row = self._parse_json_line(line)
                    if row is None:
                        continue
                    ts = _parse_utc(row.get("timestamp_utc"))
                    if not self._is_current_ts(ts):
                        continue
                    while trade_row is not None and trade_ts is not None and ts is not None and trade_ts <= ts:
                        self._apply_trade_row(trade_row, trade_ts)
                        trade_row, trade_ts = self._next_current_csv_row(trade_reader, ts_field="close_time_utc")
                    while fill_row is not None and fill_ts is not None and ts is not None and fill_ts < ts:
                        self._apply_fill_row(fill_row)
                        fill_row, fill_ts = self._next_current_csv_row(fill_reader, ts_field="timestamp_utc")
                    self._apply_sample_row(row, ts)
                self._samples_offset = samples_fh.tell()
            while trade_row is not None:
                self._apply_trade_row(trade_row, trade_ts)
                trade_row, trade_ts = self._next_current_csv_row(trade_reader, ts_field="close_time_utc")
            while fill_row is not None:
                self._apply_fill_row(fill_row)
                fill_row, fill_ts = self._next_current_csv_row(fill_reader, ts_field="timestamp_utc")
            if fills_fh is not None:
                self._fills_offset = fills_fh.tell()
            if trades_fh is not None:
                self._trades_offset = trades_fh.tell()
        finally:
            if fills_fh is not None:
                fills_fh.close()
            if trades_fh is not None:
                trades_fh.close()
            if samples_fh is not None:
                samples_fh.close()

    def _refresh(self) -> None:
        self.latest_report = self._refresh_latest_report()
        if (
            self._file_changed_unexpectedly(self.events_path, self._events_offset)
            or self._file_changed_unexpectedly(self.samples_path, self._samples_offset)
            or self._file_changed_unexpectedly(self.fills_path, self._fills_offset)
            or self._file_changed_unexpectedly(self.trades_path, self._trades_offset)
        ):
            self._rebuild()
            return
        if self._read_new_events():
            self._rebuild()
            return
        self._process_incremental_streams()

    def _size_metrics_snapshot(self, account_balance: float | None, leverage: float | None) -> Dict[str, Any]:
        buying_power = (account_balance * leverage) if account_balance is not None and leverage is not None else None
        passive_fill_count = self.touch_share_stats.count
        passive_fill_avg_notional = self.passive_fill_notional_stats.mean()
        passive_fill_p95_notional = self.passive_fill_notional_stats.percentile(0.95)
        passive_fill_avg_touch_share = self.touch_share_stats.mean()
        passive_fill_p95_touch_share = self.touch_share_stats.percentile(0.95)
        return {
            "account_balance": account_balance,
            "leverage": leverage,
            "buying_power": buying_power,
            "active_quote_avg_notional": self.active_quote_notional_stats.mean(),
            "active_quote_p95_notional": self.active_quote_notional_stats.percentile(0.95),
            "active_quote_max_notional": self.active_quote_notional_stats.max_value(),
            "passive_fill_avg_notional": passive_fill_avg_notional,
            "passive_fill_p95_notional": passive_fill_p95_notional,
            "passive_fill_max_notional": self.passive_fill_notional_stats.max_value(),
            "passive_fill_avg_buying_power_pct": (passive_fill_avg_notional / buying_power * 100.0) if buying_power and passive_fill_avg_notional is not None else None,
            "passive_fill_p95_buying_power_pct": (passive_fill_p95_notional / buying_power * 100.0) if buying_power and passive_fill_p95_notional is not None else None,
            "passive_fill_avg_touch_share_pct": (passive_fill_avg_touch_share * 100.0) if passive_fill_avg_touch_share is not None else None,
            "passive_fill_p95_touch_share_pct": (passive_fill_p95_touch_share * 100.0) if passive_fill_p95_touch_share is not None else None,
            "passive_fill_under_10pct_touch_pct": (self.passive_fill_under_10pct_touch_count / passive_fill_count * 100.0) if passive_fill_count else None,
            "passive_fill_under_25pct_touch_pct": (self.passive_fill_under_25pct_touch_count / passive_fill_count * 100.0) if passive_fill_count else None,
            "passive_fill_over_50pct_touch_count": self.passive_fill_over_50pct_touch_count,
            "passive_fill_queue_ahead_avg": self.queue_ahead_stats.mean(),
            "passive_fill_queue_ahead_p95": self.queue_ahead_stats.percentile(0.95),
            "fill_turnover_notional": self.fill_turnover_notional,
            "fill_turnover_units": self.fill_turnover_units,
            "closed_turnover_notional": self.closed_turnover_notional,
            "wins": self.wins,
            "losses": self.losses,
            "flats": self.flats,
            "win_rate_pct": (self.wins / self.closed_episodes * 100.0) if self.closed_episodes else 0.0,
            "loss_rate_pct": (self.losses / self.closed_episodes * 100.0) if self.closed_episodes else 0.0,
            "passive_fill_count": self.passive_fills_count,
            "aggressive_fill_count": self.aggressive_fills_count,
            "kill_fill_count": self.kill_fills_count,
        }

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            if not self._initialized:
                self._rebuild()
            else:
                self._refresh()

            latest_sample = self.latest_sample or {}
            latest_fill = self.latest_fill or {}
            manifest = _read_json_file(self.manifest_path) or {}
            available_profiles = _available_kevin_profiles()
            max_quote_age_ms = _to_float(self.last_started.get("max_quote_age_ms")) if self.last_started else None
            max_spread_bps = _to_float(self.last_started.get("max_spread_bps")) if self.last_started else None
            base_order_notional = _to_float(self.last_started.get("base_order_notional")) if self.last_started else None
            max_quote_notional = _to_float(self.last_started.get("max_quote_notional")) if self.last_started else None
            max_inventory_notional = _to_float(self.last_started.get("max_inventory_notional")) if self.last_started else None
            account_balance = _to_float(self.last_started.get("account_balance")) if self.last_started else None
            leverage = _to_float(self.last_started.get("leverage")) if self.last_started else None
            bot_config = manifest.get("bot_config") if isinstance(manifest.get("bot_config"), dict) else {}
            market_name = str(manifest.get("market_name") or latest_sample.get("asset") or (self.last_started.get("asset") if self.last_started else "n/a"))
            asset = str((bot_config or {}).get("asset") or latest_sample.get("asset") or (self.last_started.get("asset") if self.last_started else market_name))
            account_name = str(manifest.get("account_name") or "n/a")
            api_url = str((bot_config or {}).get("api_url") or (self.last_started.get("api_url") if self.last_started else ""))
            venue_name = _venue_name_from_api(api_url)
            profile_file = str(manifest.get("profile_file") or "")
            active_profile_slug = Path(profile_file).stem if profile_file else ""
            switch_command = f".\\.venv\\Scripts\\python.exe .\\scripts\\run_kevin_hype.py --market <profile> --account {account_name}"
            size_metrics = self._size_metrics_snapshot(account_balance, leverage)
            inventory_unrealized = self.current_unrealized
            total_pnl = self.realized_pnl_total + inventory_unrealized
            gross_total_pnl = total_pnl + self.fill_fee_drag_total
            open_fee_drag_total = self.fill_fee_drag_total - self.closed_fee_drag_total
            closed_turnover_notional = size_metrics.get("closed_turnover_notional") or 0.0
            fill_turnover_notional = size_metrics.get("fill_turnover_notional") or 0.0
            gross_edge_bps = ((self.gross_realized_pnl_total / closed_turnover_notional) * 10_000.0) if closed_turnover_notional else 0.0
            net_edge_bps = ((self.realized_pnl_total / closed_turnover_notional) * 10_000.0) if closed_turnover_notional else 0.0
            maker_share_pct = ((self.maker_notional_total / fill_turnover_notional) * 100.0) if fill_turnover_notional else 0.0
            taker_share_pct = ((self.taker_notional_total / fill_turnover_notional) * 100.0) if fill_turnover_notional else 0.0
            profit_factor = (self.pnl_positive / abs(self.pnl_negative)) if self.pnl_negative < 0.0 else None
            avg_hold_seconds = (self.avg_hold_seconds_sum / self.closed_episodes) if self.closed_episodes else 0.0
            avg_best_markout = (self.avg_best_markout_sum / self.closed_episodes) if self.closed_episodes else 0.0
            avg_realized_spread = (self.realized_spread_sum / self.realized_spread_count) if self.realized_spread_count else 0.0
            loss_buckets = sorted(
                self.loss_bucket_totals.values(),
                key=lambda item: (abs(float(item["realized_pnl"])), int(item["count"])),
                reverse=True,
            )
            top_loss_bucket = loss_buckets[0] if loss_buckets else None

            fee_tier = str(latest_sample.get("fee_tier_label") or "n/a")
            fee_actual_tier = str(latest_sample.get("fee_actual_tier_label") or "n/a")
            fee_projected_tier = str(latest_sample.get("fee_projected_tier_label") or "n/a")
            fee_market_type = str(latest_sample.get("fee_market_type") or (self.last_started.get("fee_market_type") if self.last_started else "n/a"))
            fee_staking_tier = str(latest_sample.get("fee_staking_tier") or (self.last_started.get("fee_staking_tier") if self.last_started else "n/a"))
            fee_basis = str(latest_sample.get("fee_basis") or (self.last_started.get("fee_tier_basis") if self.last_started else "n/a"))
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
            fee_taker_referral_discount_pct = _to_float(self.last_started.get("fee_taker_referral_discount_pct")) if self.last_started else 0.0
            fee_maker_rebate_bps_override = _to_float(self.last_started.get("fee_maker_rebate_bps_override")) if self.last_started else 0.0
            fee_user_maker_rate_pct_override = _to_float(self.last_started.get("fee_user_maker_rate_pct_override")) if self.last_started else None
            fee_user_taker_rate_pct_override = _to_float(self.last_started.get("fee_user_taker_rate_pct_override")) if self.last_started else None

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
            fee_mode_label = (
                "manual account fee rates"
                if fee_rate_source == "manual_account_rates"
                else "live userFees account rates"
                if fee_rate_source == "userFees"
                else "estimated fee schedule"
            )

            current_run = {
                "start_utc": self.last_started.get("timestamp_utc") if self.last_started else None,
                "latest_sample": latest_sample,
                "latest_fill": latest_fill,
                "fills_count": self.fills_count,
                "passive_fills": self.passive_fills_count,
                "kill_fills": self.kill_fills_count,
                "aggressive_fills": self.aggressive_fills_count,
                "closed_episodes": self.closed_episodes,
                "realized_pnl_total": self.realized_pnl_total,
                "gross_realized_pnl_total": self.gross_realized_pnl_total,
                "inventory_unrealized_pnl": inventory_unrealized,
                "gross_total_pnl": gross_total_pnl,
                "total_pnl": total_pnl,
                "closed_fee_drag_total": self.closed_fee_drag_total,
                "fill_fee_drag_total": self.fill_fee_drag_total,
                "open_fee_drag_total": open_fee_drag_total,
                "fill_fee_cost_total": self.fill_fee_cost_total,
                "fill_rebate_total": self.fill_rebate_total,
                "maker_fee_cost_total": self.maker_fee_cost_total,
                "taker_fee_cost_total": self.taker_fee_cost_total,
                "maker_rebates_total": self.maker_rebates_total,
                "maker_notional_total": self.maker_notional_total,
                "taker_notional_total": self.taker_notional_total,
                "maker_share_pct": maker_share_pct,
                "taker_share_pct": taker_share_pct,
                "gross_edge_bps": gross_edge_bps,
                "net_edge_bps": net_edge_bps,
                "profit_factor": profit_factor,
                "win_rate_pct": (self.wins / self.closed_episodes * 100.0) if self.closed_episodes else 0.0,
                "avg_hold_seconds": avg_hold_seconds,
                "avg_best_markout_bps": avg_best_markout,
                "avg_realized_spread_bps": avg_realized_spread,
                "passive_realized_pnl_total": self.passive_realized_pnl_total,
                "kill_realized_pnl_total": self.kill_realized_pnl_total,
                "passive_closed_turnover": self.passive_closed_turnover,
                "kill_closed_turnover": self.kill_closed_turnover,
                "passive_edge_bps": ((self.passive_realized_pnl_total / self.passive_closed_turnover) * 10_000.0) if self.passive_closed_turnover else 0.0,
                "kill_edge_bps": ((self.kill_realized_pnl_total / self.kill_closed_turnover) * 10_000.0) if self.kill_closed_turnover else 0.0,
                "kill_episode_count": self.kill_episode_count,
                "passive_episode_count": self.passive_episode_count,
                "long_episode_count": self.long_episode_count,
                "short_episode_count": self.short_episode_count,
                "long_loss_count": self.long_loss_count,
                "short_loss_count": self.short_loss_count,
                "long_loss_pnl_total": self.long_loss_pnl_total,
                "short_loss_pnl_total": self.short_loss_pnl_total,
                "long_loss_turnover_total": self.long_loss_turnover_total,
                "short_loss_turnover_total": self.short_loss_turnover_total,
                "loss_buckets": loss_buckets,
                "top_loss_bucket": top_loss_bucket,
                "polling_errors": self.event_counts.get("polling_error", 0),
                "stale_quote_skips": self.event_counts.get("stale_quote_skipped", 0),
                "quote_gap_warnings": self.event_counts.get("quote_gap_warning", 0),
                "feed_reconnects": self.event_counts.get("feed_reconnected", 0),
                "quote_posts": self.event_counts.get("quote_posted", 0),
                "quote_replaces": self.event_counts.get("quote_replaced", 0),
                "quote_cancels": self.event_counts.get("quote_canceled", 0),
                "current_quote_age_ms": _to_float(latest_sample.get("quote_age_ms")),
                "p50_quote_age_ms": self.quote_age_stats.percentile(0.50),
                "p95_quote_age_ms": self.quote_age_stats.percentile(0.95),
                "max_quote_age_ms_seen": self.quote_age_stats.max_value(),
                "current_transport_delay_ms": _to_float(latest_sample.get("transport_delay_ms")),
                "p50_transport_delay_ms": self.transport_stats.percentile(0.50),
                "p95_transport_delay_ms": self.transport_stats.percentile(0.95),
                "max_transport_delay_ms": self.transport_stats.max_value(),
                "current_spread_bps": _to_float(latest_sample.get("spread_bps")),
                "p50_spread_bps": self.spread_stats.percentile(0.50),
                "p95_spread_bps": self.spread_stats.percentile(0.95),
                "max_spread_bps_seen": self.spread_stats.max_value(),
                "configured_max_quote_age_ms": max_quote_age_ms,
                "configured_max_spread_bps": max_spread_bps,
                "configured_base_order_notional": base_order_notional,
                "configured_max_quote_notional": max_quote_notional,
                "configured_max_inventory_notional": max_inventory_notional,
                "configured_account_balance": account_balance,
                "configured_leverage": leverage,
                "market_name": market_name,
                "asset": asset,
                "account_name": account_name,
                "venue_name": venue_name,
                "api_url": api_url,
                "active_profile_slug": active_profile_slug,
                "profile_file": profile_file,
                "available_profiles": available_profiles,
                "switch_command": switch_command,
                "configured_max_long_episodes_per_day": _to_float(self.last_started.get("max_long_episodes_per_day")) if self.last_started else None,
                "configured_max_short_episodes_per_day": _to_float(self.last_started.get("max_short_episodes_per_day")) if self.last_started else None,
                "configured_max_long_episodes_per_hour": _to_float(self.last_started.get("max_long_episodes_per_hour")) if self.last_started else None,
                "configured_max_short_episodes_per_hour": _to_float(self.last_started.get("max_short_episodes_per_hour")) if self.last_started else None,
                "fee_tier_label": fee_tier,
                "fee_actual_tier_label": fee_actual_tier,
                "fee_projected_tier_label": fee_projected_tier,
                "fee_market_type": fee_market_type,
                "fee_staking_tier": fee_staking_tier,
                "fee_basis": fee_basis,
                "fee_rate_source": fee_rate_source,
                "fee_mode_label": fee_mode_label,
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
                "shadow_fee_accounting": bool(self.last_started.get("shadow_fee_accounting")) if self.last_started else False,
                "volume_boost_multiplier": volume_boost_multiplier,
                "quote_mode_counts": dict(self.quote_mode_counts),
                "quote_reason_counts": dict(self.quote_reason_counts),
                "event_regime_samples": self.event_regime_samples,
                "inventory_protection_samples": self.inventory_protection_samples,
                "size_metrics": size_metrics,
                "pnl_series": self.chart_points[-480:],
            }

            return {
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "run_state": self.run_state,
                "run_count": self.run_count,
                "latest_report": self.latest_report,
                "last_started": self.last_started,
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
                    "data_source": f"websocket bbo + l2Book + trades | Kevin fee-aware fills ({fee_mode_label})",
                },
                "recent_fills": list(self.recent_fills),
                "recent_trades": list(self.recent_trades),
            }


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
    max_quote_notional = _to_float(last_started.get("max_quote_notional")) if last_started else None
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
    gross_total_pnl = total_pnl + fill_fee_drag_total
    open_fee_drag_total = fill_fee_drag_total - closed_fee_drag_total
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
    passive_closed_turnover = sum(_trade_turnover_notional(row) for row in passive_trades)
    kill_closed_turnover = sum(_trade_turnover_notional(row) for row in kill_trades)
    passive_edge_bps = ((passive_realized_pnl_total / passive_closed_turnover) * 10_000.0) if passive_closed_turnover else 0.0
    kill_edge_bps = ((kill_realized_pnl_total / kill_closed_turnover) * 10_000.0) if kill_closed_turnover else 0.0
    long_trades = [row for row in current_trades if str(row.get("side") or "").upper() == "LONG"]
    short_trades = [row for row in current_trades if str(row.get("side") or "").upper() == "SHORT"]
    long_loss_trades = [row for row in long_trades if (_to_float(row.get("realized_pnl")) or 0.0) < 0.0]
    short_loss_trades = [row for row in short_trades if (_to_float(row.get("realized_pnl")) or 0.0) < 0.0]
    long_loss_pnl_total = sum((_to_float(row.get("realized_pnl")) or 0.0) for row in long_loss_trades)
    short_loss_pnl_total = sum((_to_float(row.get("realized_pnl")) or 0.0) for row in short_loss_trades)
    long_loss_turnover_total = sum(_trade_turnover_notional(row) for row in long_loss_trades)
    short_loss_turnover_total = sum(_trade_turnover_notional(row) for row in short_loss_trades)
    loss_buckets = _summarize_loss_buckets(current_trades)
    top_loss_bucket = loss_buckets[0] if loss_buckets else None
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
    fee_mode_label = (
        "manual account fee rates"
        if fee_rate_source == "manual_account_rates"
        else "live userFees account rates"
        if fee_rate_source == "userFees"
        else "estimated fee schedule"
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
        "gross_total_pnl": gross_total_pnl,
        "total_pnl": total_pnl,
        "closed_fee_drag_total": closed_fee_drag_total,
        "fill_fee_drag_total": fill_fee_drag_total,
        "open_fee_drag_total": open_fee_drag_total,
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
        "long_episode_count": len(long_trades),
        "short_episode_count": len(short_trades),
        "long_loss_count": len(long_loss_trades),
        "short_loss_count": len(short_loss_trades),
        "long_loss_pnl_total": long_loss_pnl_total,
        "short_loss_pnl_total": short_loss_pnl_total,
        "long_loss_turnover_total": long_loss_turnover_total,
        "short_loss_turnover_total": short_loss_turnover_total,
        "loss_buckets": loss_buckets,
        "top_loss_bucket": top_loss_bucket,
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
        "configured_max_quote_notional": max_quote_notional,
        "configured_max_inventory_notional": max_inventory_notional,
        "configured_account_balance": account_balance,
        "configured_leverage": leverage,
        "configured_max_long_episodes_per_day": _to_float(last_started.get("max_long_episodes_per_day")) if last_started else None,
        "configured_max_short_episodes_per_day": _to_float(last_started.get("max_short_episodes_per_day")) if last_started else None,
        "configured_max_long_episodes_per_hour": _to_float(last_started.get("max_long_episodes_per_hour")) if last_started else None,
        "configured_max_short_episodes_per_hour": _to_float(last_started.get("max_short_episodes_per_hour")) if last_started else None,
        "fee_tier_label": fee_tier,
        "fee_actual_tier_label": fee_actual_tier,
        "fee_projected_tier_label": fee_projected_tier,
        "fee_market_type": fee_market_type,
        "fee_staking_tier": fee_staking_tier,
        "fee_basis": fee_basis,
        "fee_rate_source": fee_rate_source,
        "fee_mode_label": fee_mode_label,
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
        "shadow_fee_accounting": bool(last_started.get("shadow_fee_accounting")) if last_started else False,
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
            "data_source": f"websocket bbo + l2Book + trades | Kevin fee-aware fills ({fee_mode_label})",
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
  <title>Kevin Hype Liquidity Engine</title>
  <style>
    :root {
      --bg-a: #05080f;
      --bg-b: #0a1320;
      --panel: rgba(10, 20, 31, 0.76);
      --panel-strong: rgba(17, 29, 45, 0.92);
      --text: #ecf8ff;
      --muted: #91a8b8;
      --good: #77f6c1;
      --warn: #ffd972;
      --bad: #ff8d7d;
      --line: rgba(154, 193, 217, 0.14);
      --accent: #7dd5ff;
      --body: "Aptos", "Trebuchet MS", "Segoe UI Variable", sans-serif;
      --mono: "JetBrains Mono", "Cascadia Code", "Consolas", monospace;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: var(--body);
      background:
        radial-gradient(circle at top left, rgba(56, 189, 248, 0.20), transparent 32%),
        radial-gradient(circle at bottom right, rgba(34, 197, 94, 0.16), transparent 28%),
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
      box-shadow: 0 18px 48px rgba(0, 0, 0, 0.22);
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
        <h1>Kevin Hype Liquidity Engine</h1>
        <p class="meta" id="stamp">Loading...</p>
      </div>
      <div class="stack" style="justify-items:end;">
        <div class="pill" id="market-chip">Loading market...</div>
        <div class="pill" id="datasource">Loading source...</div>
      </div>
    </div>

    <section class="hero">
      <div class="hero-copy">
        <h3 id="headline">Loading decision state...</h3>
        <p class="copy" id="decision-note"></p>
        <p class="copy" id="market-context"></p>
        <p class="copy" id="switch-note"></p>
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
        <h2>Size Discipline</h2>
        <div class="grid" id="fees"></div>
      </div>
      <div class="section">
        <h2>Behaviour And Exits</h2>
        <div class="grid" id="kills"></div>
      </div>
    </section>

    <section class="section">
      <h2>Live PnL</h2>
      <div class="chart-panel">
        <div class="chart-meta">
          <span id="chart-summary">Loading chart...</span>
          <span>Closed realised and live total from Kevin&apos;s current run start</span>
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
              <th>Time</th><th>ID</th><th>Ep</th><th>Role</th><th>Side</th><th>Px</th><th>Sz</th><th>Notional</th><th>Reason</th><th>Inv</th><th>Delta PnL</th>
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
              <th>Ep</th><th>Side</th><th>Open</th><th>Close</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Realized</th><th>Hold(s)</th><th>Best Markout</th><th>Worst Markout</th><th>Reason</th>
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

    const pct = (value, digits = 3) => {
      if (value === null || value === undefined || value === '') return 'n/a';
      const num = Number(value);
      if (Number.isNaN(num)) return String(value);
      return `${num.toFixed(digits)}%`;
    };

    const usd = (value, digits = 2) => {
      if (value === null || value === undefined || value === '') return 'n/a';
      const num = Number(value);
      if (Number.isNaN(num)) return String(value);
      const sign = num < 0 ? '-' : '';
      return `${sign}$${Math.abs(num).toFixed(digits)}`;
    };

    const marginPct = (pnl, turnover) => {
      const pnlNum = Number(pnl || 0);
      const turnoverNum = Number(turnover || 0);
      if (!Number.isFinite(turnoverNum) || Math.abs(turnoverNum) < 1e-9) return null;
      return (pnlNum / turnoverNum) * 100;
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

    const liveNotional = (sample, side) => {
      const px = Number(sample[`live_${side}_order_price`] || sample[`${side}_quote_price`] || 0);
      const sz = Number(sample[`live_${side}_order_size`] || sample[`${side}_quote_size`] || 0);
      return px * sz;
    };

    const capReason = (sample, cur) => {
      const inventoryNotional = Math.abs(Number(sample.inventory_qty || 0) * Number(sample.mid || 0));
      const maxInventory = Number(cur.configured_max_inventory_notional || 0);
      const maxQuote = Number(cur.configured_max_quote_notional || 0);
      const quoted = Math.max(liveNotional(sample, 'bid'), liveNotional(sample, 'ask'));
      const sizeRisk = Number(sample.size_risk_multiplier || 1);
      if (!sample.quoting_enabled) {
        return {
          headline: sample.quoting_reason || 'standing down',
          detail: sample.decision_note || 'Quoting is paused until the tape improves.',
        };
      }
      if (maxInventory > 0 && inventoryNotional >= maxInventory * 0.82) {
        return {
          headline: 'inventory cap',
          detail: `Inventory is using ${fmt((inventoryNotional / maxInventory) * 100, 1)}% of the Kevin max inventory budget.`,
        };
      }
      if (maxQuote > 0 && quoted >= maxQuote * 0.92) {
        return {
          headline: 'per-quote cap',
          detail: `Quoted notional is already pressing against the configured per-quote ceiling of $${fmt(maxQuote, 0)}.`,
        };
      }
      if ((sample.bid_reason || 'active') !== 'active' || (sample.ask_reason || 'active') !== 'active') {
        return {
          headline: 'directional guard',
          detail: `One side is being withheld because the tape is leaning ${sample.ask_reason === 'buy_pressure' ? 'up' : sample.bid_reason === 'sell_pressure' ? 'down' : 'with inventory pressure'}.`,
        };
      }
      if (sizeRisk <= 0.78) {
        return {
          headline: 'risk-scaled size',
          detail: 'Size is being clipped by volatility, toxicity, or inventory pressure rather than by a hard lot-size setting.',
        };
      }
      return {
        headline: 'book and queue discipline',
        detail: 'Size is being set by visible depth, queue ahead, and inventory balance so the strategy stays meaningful without bullying the touch.',
      };
    };

    const behaviourVerdict = (sample, cur, cap) => {
      const quoteAge = Number(cur.current_quote_age_ms || 0);
      const maxQuoteAge = Number(cur.configured_max_quote_age_ms || 0);
      const pnl = Number(cur.total_pnl || 0);
      if (!sample.quoting_enabled) {
        return `Standing down cleanly because ${cap.headline}.`;
      }
      if (maxQuoteAge > 0 && quoteAge > maxQuoteAge) {
        return 'Feed is stale relative to the configured cap, so the strategy is behaving defensively.';
      }
      if ((sample.quote_mode || '') === 'both' && Number(sample.size_risk_multiplier || 1) >= 0.85) {
        return 'Healthy two-way quoting with size still open enough to show the HYPE book properly.';
      }
      if ((sample.quote_mode || '') === 'bid_only' || (sample.quote_mode || '') === 'ask_only') {
        return 'Leaning into one-way quoting because the tape is directional, while still avoiding an aggressive chase.';
      }
      return pnl >= 0
        ? 'The engine is staying controlled and capital-efficient while the tape remains mixed.'
        : 'The engine is still active, but it is trading smaller and more selectively while the tape is less friendly.';
    };

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
        `range ${usd(minY, 4)} to ${usd(maxY, 4)} | gross ${usd(grossValues[grossValues.length - 1], 4)} | net ${usd(netValues[netValues.length - 1], 4)} | total ${usd(totalValues[totalValues.length - 1], 4)}`;
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
      const grossTotal = Number(cur.gross_total_pnl || (netTotal + feeDrag));
      const closedFeeDrag = Number(cur.closed_fee_drag_total || 0);
      const openFeeDrag = Number(cur.open_fee_drag_total || 0);
      const makerShare = Number(cur.maker_share_pct || 0);
      const takerShare = Number(cur.taker_share_pct || 0);
      const leverage = Number(cur.configured_leverage || 0);
      const latestClosed = (data.recent_trades || [])[data.recent_trades.length - 1] || {};
      const topLossBucket = (cur.loss_buckets || [])[0] || null;
      const fillTurnover = Number(size.fill_turnover_notional || 0);
      const closedTurnover = Number(size.closed_turnover_notional || 0);
      const closedEpisodes = Number(cur.closed_episodes || 0);
      const fillsCount = Number(cur.fills_count || 0);
      const avgGrossPerClosedTrade = closedEpisodes > 0 ? grossRealized / closedEpisodes : null;
      const avgNetPerClosedTrade = closedEpisodes > 0 ? netRealized / closedEpisodes : null;
      const avgClosedTurnoverPerTrade = closedEpisodes > 0 ? closedTurnover / closedEpisodes : null;
      const avgFillNotional = fillsCount > 0 ? fillTurnover / fillsCount : null;
      const grossTotalMarginPct = marginPct(grossTotal, fillTurnover);
      const netTotalMarginPct = marginPct(netTotal, fillTurnover);
      const grossClosedMarginPct = marginPct(grossRealized, closedTurnover);
      const netClosedMarginPct = marginPct(netRealized, closedTurnover);
      const feeDragMarginPct = marginPct(feeDrag, fillTurnover);
      const inventoryNotional = Math.abs(Number(sample.inventory_qty || 0) * Number(sample.mid || 0));
      const unrealizedMarginPct = marginPct(cur.inventory_unrealized_pnl, inventoryNotional);
      const passiveTurnover = Number(cur.passive_closed_turnover || 0);
      const killTurnover = Number(cur.kill_closed_turnover || 0);
      const passiveMarginPct = marginPct(cur.passive_realized_pnl_total, passiveTurnover);
      const killMarginPct = marginPct(cur.kill_realized_pnl_total, killTurnover);
      const longLossTurnover = Number(cur.long_loss_turnover_total || 0);
      const shortLossTurnover = Number(cur.short_loss_turnover_total || 0);
      const longLossMarginPct = marginPct(cur.long_loss_pnl_total, longLossTurnover);
      const shortLossMarginPct = marginPct(cur.short_loss_pnl_total, shortLossTurnover);
      const topLossBucketTurnover = Number((topLossBucket && topLossBucket.turnover_notional) || 0);
      const topLossBucketMarginPct = marginPct(topLossBucket && topLossBucket.realized_pnl, topLossBucketTurnover);
      const feeBasis = cur.fee_basis || 'n/a';
      const feeModeLabel = cur.fee_mode_label || feeBasis;
      const profitFactor = cur.profit_factor;
      const cap = capReason(sample, cur);
      const behaviour = behaviourVerdict(sample, cur, cap);
      const availableProfiles = (cur.available_profiles || []).map((item) => item.slug || item.market_name || '').filter(Boolean);
      const switchHint = cur.switch_command || '.\\.venv\\Scripts\\python.exe .\\scripts\\run_kevin_hype.py --market <profile>';

      document.getElementById('stamp').textContent = `Updated ${data.generated_at_utc}`;
      document.getElementById('market-chip').textContent = `Market ${cur.market_name || cur.asset || 'n/a'} | Venue ${cur.venue_name || 'n/a'} | Account ${cur.account_name || 'n/a'}`;
      document.getElementById('datasource').textContent = `Source: ${decision.data_source || 'n/a'}`;
      document.getElementById('headline').textContent = decision.headline || 'No decision text yet.';
      document.getElementById('decision-note').textContent = decision.note || 'Waiting for the next sample.';
      document.getElementById('market-context').textContent =
        `Trading ${cur.market_name || 'n/a'} (${cur.asset || 'n/a'}) on ${cur.venue_name || 'n/a'} | profile ${cur.active_profile_slug || 'n/a'} | account ${cur.account_name || 'n/a'}`;
      document.getElementById('switch-note').textContent =
        `One Kevin bot process trades one market at a time. Available profiles: ${availableProfiles.join(', ') || 'none found'}. Switch with: ${switchHint}`;
      document.getElementById('freshness').className = `pill ${cls(decision.freshness, 'state')}`;
      document.getElementById('freshness').textContent =
        `Feed ${decision.freshness || 'unknown'} | age ${fmt(cur.current_quote_age_ms, 0)} ms / cap ${fmt(cur.configured_max_quote_age_ms, 0)} ms`;

      document.getElementById('hero-stats').innerHTML =
        metric('Market', `${cur.market_name || 'n/a'} / ${cur.asset || 'n/a'}`, `${cur.venue_name || 'n/a'} | acct ${cur.account_name || 'n/a'}`) +
        metric('Run State', data.run_state || 'unknown', 'engine state', cls(data.run_state, 'state')) +
        metric('Quote Mode', sample.quote_mode || 'n/a', sample.quoting_reason || 'n/a', sample.quoting_enabled ? 'good' : 'warn') +
        metric('Gross Total PnL', usd(grossTotal, 4), `margin ${pct(grossTotalMarginPct, 3)} on ${usd(fillTurnover, 0)} traded | before fees/rebates`, cls(grossTotal, 'pnl')) +
        metric('Net Total PnL', usd(netTotal, 4), `margin ${pct(netTotalMarginPct, 3)} on ${usd(fillTurnover, 0)} traded | unrl ${usd(cur.inventory_unrealized_pnl, 4)}`, cls(netTotal, 'pnl')) +
        metric('Gross Closed PnL', usd(grossRealized, 4), `margin ${pct(grossClosedMarginPct, 3)} on ${usd(closedTurnover, 0)} closed turnover | ${fmt(cur.closed_episodes, 0)} episodes`, cls(grossRealized, 'pnl')) +
        metric('Net Closed PnL', usd(netRealized, 4), `margin ${pct(netClosedMarginPct, 3)} on ${usd(closedTurnover, 0)} closed turnover | drag ${usd(closedFeeDrag, 4)}`, cls(netRealized, 'pnl')) +
        metric('All Fee Drag', usd(feeDrag, 4), `drag ${pct(feeDragMarginPct, 3)} on ${usd(fillTurnover, 0)} traded | open ${usd(openFeeDrag, 4)}`, feeDrag > 0 ? 'warn' : 'good') +
        metric('Inventory', `${sample.inventory_side || 'FLAT'} ${fmt(sample.inventory_qty, 4)}`, `mark ${fmt(sample.inventory_unrealized_bps, 2)} bps`, sample.inventory_side === 'FLAT' ? 'warn' : 'good') +
        metric('Cap Reason', cap.headline, cap.detail, 'warn') +
        metric('Buying Power', usd(size.buying_power, 0), `equity ${usd(cur.configured_account_balance, 0)} at ${fmt(leverage, 0)}x`);

      document.getElementById('market').innerHTML =
        metric('Bid / Ask', `${usd(sample.bid, 4)} / ${usd(sample.ask, 4)}`, `mid ${usd(sample.mid, 4)} micro ${usd(sample.microprice, 4)}`) +
        metric('Spread bps', fmt(cur.current_spread_bps, 3), `p50 ${fmt(cur.p50_spread_bps, 3)} | p95 ${fmt(cur.p95_spread_bps, 3)} | max ${fmt(cur.max_spread_bps_seen, 3)}`, cls(cur.current_spread_bps, 'spread', {limit: cur.configured_max_spread_bps})) +
        metric('Quote Age ms', fmt(cur.current_quote_age_ms, 0), `p50 ${fmt(cur.p50_quote_age_ms, 0)} | p95 ${fmt(cur.p95_quote_age_ms, 0)} | max ${fmt(cur.max_quote_age_ms_seen, 0)}`, cls(cur.current_quote_age_ms, 'latency', {limit: cur.configured_max_quote_age_ms})) +
        metric('Transport ms', fmt(cur.current_transport_delay_ms, 0), `p50 ${fmt(cur.p50_transport_delay_ms, 0)} | p95 ${fmt(cur.p95_transport_delay_ms, 0)} | max ${fmt(cur.max_transport_delay_ms, 0)}`, cls(cur.current_transport_delay_ms, 'latency', {limit: cur.configured_max_quote_age_ms})) +
        metric('Vol / Impulse', `${fmt(sample.recent_vol_bps, 2)} / ${fmt(sample.impulse_bps, 2)}`, 'bps over short horizon') +
        metric('Trade Rate', fmt(sample.trade_rate_per_second, 2), `trade count ${fmt(sample.trade_count, 0)}`) +
        metric('Flow Imbalance', fmt(sample.flow_imbalance, 3), `buy ${fmt(sample.buy_volume, 2)} / sell ${fmt(sample.sell_volume, 2)}`, cls(sample.flow_imbalance, 'imbalance')) +
        metric('Book Imbalance', fmt(sample.book_imbalance, 3), `bid depth ${fmt(sample.bid_depth, 2)} / ask depth ${fmt(sample.ask_depth, 2)}`, cls(sample.book_imbalance, 'imbalance')) +
        metric('Feed Health', sample.event_regime ? 'event regime' : 'normal', `stale skips ${fmt(cur.stale_quote_skips, 0)} | reconnects ${fmt(cur.feed_reconnects, 0)}`, sample.event_regime ? 'bad' : 'good') +
        metric('Operator Read', behaviour, `decision ${sample.decision_note || 'n/a'}`, sample.quoting_enabled ? 'good' : 'warn');

      document.getElementById('quotes').innerHTML =
        metric('Quote Reason', sample.quoting_reason || 'n/a', decision.note || '', sample.quoting_enabled ? 'good' : 'warn') +
        metric('Bid Side', sample.bid_enabled ? (sample.bid_quote_price ? usd(sample.bid_quote_price, 4) : 'enabled') : 'off', sample.bid_reason || 'n/a', sample.bid_enabled ? 'good' : 'warn') +
        metric('Ask Side', sample.ask_enabled ? (sample.ask_quote_price ? usd(sample.ask_quote_price, 4) : 'enabled') : 'off', sample.ask_reason || 'n/a', sample.ask_enabled ? 'good' : 'warn') +
        metric('Base / Max Notional', `${usd(cur.configured_base_order_notional, 0)} / ${usd(cur.configured_max_quote_notional, 0)}`, `per side base / quote cap, inventory cap ${usd(cur.configured_max_inventory_notional, 0)}`) +
        metric('Active Quote Notional', `${usd(size.active_quote_avg_notional, 0)} avg`, `p95 ${usd(size.active_quote_p95_notional, 0)} | max ${usd(size.active_quote_max_notional, 0)}`) +
        metric('Target Quote Size', `${fmt(sample.bid_quote_size, 4)} / ${fmt(sample.ask_quote_size, 4)}`, `notional ${usd(bidQuoteNotional, 2)} / ${usd(askQuoteNotional, 2)}`) +
        metric('Live Quote Notional', `${usd(liveBidNotional, 2)} / ${usd(liveAskNotional, 2)}`, 'live bid / ask on the book now') +
        metric('Size Risk Mult', `${fmt(sample.size_risk_multiplier, 2)}x`, 'risk shrink applied to quote size before posting') +
        metric('Fair / Reservation', `${usd(sample.fair_value, 4)} / ${usd(sample.reservation_price, 4)}`, `alpha ${fmt(sample.alpha_bps, 3)} bps`) +
        metric('Target Half Spread', fmt(sample.target_half_spread_bps, 3), `inventory skew ${fmt(sample.inventory_skew_bps, 3)} bps | fee mode ${feeModeLabel}`) +
        metric('Live Bid / Ask', `${usd(sample.live_bid_order_price, 4)} / ${usd(sample.live_ask_order_price, 4)}`, `size ${fmt(sample.live_bid_order_size, 4)} / ${fmt(sample.live_ask_order_size, 4)}, notional ${usd(liveBidNotional, 2)} / ${usd(liveAskNotional, 2)}`) +
        metric('Queue Ahead', `${fmt(sample.bid_queue_ahead_size, 3)} / ${fmt(sample.ask_queue_ahead_size, 3)}`, 'bid / ask') +
        metric('Passive Fill Size', `${usd(size.passive_fill_avg_notional, 0)} avg`, `p95 ${usd(size.passive_fill_p95_notional, 0)} | max ${usd(size.passive_fill_max_notional, 0)}`) +
        metric('Touch Share', `${fmt(size.passive_fill_avg_touch_share_pct, 1)}% avg`, `p95 ${fmt(size.passive_fill_p95_touch_share_pct, 1)}% | <=25% on ${fmt(size.passive_fill_under_25pct_touch_pct, 1)}% of passive fills`) +
        metric('Capacity Usage', `${fmt(size.passive_fill_avg_buying_power_pct, 2)}% avg`, `p95 ${fmt(size.passive_fill_p95_buying_power_pct, 2)}% of buying power`) +
        metric('Side Episode Caps', `long ${fmt(cur.long_episode_count, 0)} / ${fmt(cur.configured_max_long_episodes_per_hour, 0)}`, `short ${fmt(cur.short_episode_count, 0)} / ${fmt(cur.configured_max_short_episodes_per_hour, 0)} per hour`) +
        metric('Quote Modes', `${fmt(modeCounts.both || 0, 0)} both`, `bid-only ${fmt(modeCounts.bid_only || 0, 0)} | ask-only ${fmt(modeCounts.ask_only || 0, 0)} | flat ${fmt(modeCounts.flat || 0, 0)}`) +
        metric('Protection Time', fmt(cur.inventory_protection_samples, 0), `event samples ${fmt(cur.event_regime_samples, 0)}`) +
        metric('Toxicity Score', fmt(sample.toxicity_score, 3), `top reasons ${Object.keys(reasonCounts).slice(0, 3).join(', ') || 'n/a'}`, Number(sample.toxicity_score) >= 1 ? 'warn' : 'good');

      document.getElementById('execution').innerHTML =
        metric('Passive Fills', fmt(cur.passive_fills, 0), `kill fills ${fmt(cur.kill_fills, 0)} / total fills ${fmt(cur.fills_count, 0)}`) +
        metric('Closed Episodes', fmt(cur.closed_episodes, 0), `win rate ${fmt(cur.win_rate_pct, 2)}%`) +
        metric('Avg Closed Trade PnL', `${usd(avgGrossPerClosedTrade, 4)} / ${usd(avgNetPerClosedTrade, 4)}`, `gross / net per closed trade | avg turnover ${usd(avgClosedTurnoverPerTrade, 0)}`, cls(avgNetPerClosedTrade, 'pnl')) +
        metric('Avg Fill Notional', usd(avgFillNotional, 2), `all fills | passive avg ${usd(size.passive_fill_avg_notional, 2)}`) +
        metric('Success Split', `${fmt(size.wins, 0)} / ${fmt(size.losses, 0)} / ${fmt(size.flats, 0)}`, `wins / losses / flat, loss rate ${fmt(size.loss_rate_pct, 2)}%`) +
        metric('Profit Factor', profitFactor === null || profitFactor === undefined ? 'n/a' : fmt(profitFactor, 2), 'gross winning pnl over losing pnl', profitFactor && profitFactor >= 1.5 ? 'good' : 'warn') +
        metric('Avg Hold', fmt(cur.avg_hold_seconds, 3), 'seconds per episode') +
        metric('Best Markout', fmt(cur.avg_best_markout_bps, 3), 'average best bps') +
        metric('Realized Spread', fmt(cur.avg_realized_spread_bps, 3), `gross ${fmt(cur.gross_edge_bps, 3)} bps | net ${fmt(cur.net_edge_bps, 3)} bps`, cls(cur.avg_realized_spread_bps, 'pnl')) +
        metric('Fill Turnover', usd(fillTurnover, 0), `${fmt(size.fill_turnover_units, 2)} units traded this run`) +
        metric('Closed Turnover', usd(closedTurnover, 0), 'entry plus exit notional across completed episodes') +
        metric('Queue At Fill', `${fmt(size.passive_fill_queue_ahead_avg, 1)} avg`, `p95 ${fmt(size.passive_fill_queue_ahead_p95, 1)} ahead`) +
        metric('Thin-Book Outliers', `${fmt(size.passive_fill_over_50pct_touch_count, 0)}`, `passive fills >50% of visible touch; usually a thinning-book edge case`, size.passive_fill_over_50pct_touch_count > 0 ? 'warn' : 'good') +
        metric('Quote Ops', `${fmt(cur.quote_posts, 0)} / ${fmt(cur.quote_replaces, 0)}`, `posts / replaces, cancels ${fmt(cur.quote_cancels, 0)}`) +
        metric('Loop Errors', fmt(cur.polling_errors, 0), `gap warnings ${fmt(cur.quote_gap_warnings, 0)} | reconnects ${fmt(cur.feed_reconnects, 0)}`, cur.polling_errors > 0 ? 'bad' : 'good') +
        metric('Latest Fill', cur.latest_fill.reason || 'n/a', `${cur.latest_fill.liquidity_role || 'n/a'} ${cur.latest_fill.side || ''}`) +
        metric('Fee Accounting', feeModeLabel, `maker ${fmt(cur.fee_maker_rate_bps, 3)} bps | taker ${fmt(cur.fee_taker_rate_bps, 3)} bps | drag ${usd(feeDrag, 4)}`, feeDrag > 0 ? 'warn' : 'good') +
        metric('Gross / Net Closed', `${usd(grossRealized, 4)} / ${usd(netRealized, 4)}`, `gross ${pct(grossClosedMarginPct, 3)} / net ${pct(netClosedMarginPct, 3)} on ${usd(closedTurnover, 0)} | drag ${usd(closedFeeDrag, 4)}`, cls(cur.realized_pnl_total, 'pnl')) +
        metric('Unrealized PnL', usd(cur.inventory_unrealized_pnl, 4), `mark ${pct(unrealizedMarginPct, 3)} on ${usd(inventoryNotional, 0)} inventory | ${sample.inventory_side || 'FLAT'}`, cls(cur.inventory_unrealized_pnl, 'pnl'));

      document.getElementById('fees').innerHTML =
        metric('Buying Power', usd(size.buying_power, 0), `equity ${usd(cur.configured_account_balance, 0)} at ${fmt(cur.configured_leverage, 0)}x`) +
        metric('Max Inventory', usd(cur.configured_max_inventory_notional, 0), `${usd(Math.abs(Number(sample.inventory_qty || 0) * Number(sample.mid || 0)), 0)} in current inventory notional`) +
        metric('Per-Quote Cap', usd(cur.configured_max_quote_notional, 0), `base ${usd(cur.configured_base_order_notional, 0)} | bid ${usd(liveBidNotional, 0)} / ask ${usd(liveAskNotional, 0)}`) +
        metric('Cap Reason', cap.headline, cap.detail, 'warn') +
        metric('Average Fill Size', `${usd(size.passive_fill_avg_notional, 0)} notional`, `${fmt(size.passive_fill_avg_buying_power_pct, 2)}% of buying power`) +
        metric('P95 Fill Size', `${usd(size.passive_fill_p95_notional, 0)} notional`, `${fmt(size.passive_fill_p95_buying_power_pct, 2)}% of buying power`) +
        metric('Max Fill Size', `${usd(size.passive_fill_max_notional, 0)} notional`, `${fmt(size.passive_fill_max_notional && size.buying_power ? (size.passive_fill_max_notional / size.buying_power) * 100 : null, 2)}% of buying power`) +
        metric('Touch Share', `${fmt(size.passive_fill_avg_touch_share_pct, 1)}% avg`, `p95 ${fmt(size.passive_fill_p95_touch_share_pct, 1)}% | >50% touch count ${fmt(size.passive_fill_over_50pct_touch_count, 0)}`) +
        metric('Queue Ahead At Fill', `${fmt(size.passive_fill_queue_ahead_avg, 1)} avg`, `p95 ${fmt(size.passive_fill_queue_ahead_p95, 1)}`) +
        metric('Notional Throughput', usd(fillTurnover, 0), `${fmt(size.fill_turnover_units, 2)} HYPE traded this run`) +
        metric('Closed Turnover', usd(closedTurnover, 0), `realised spread ${fmt(cur.avg_realized_spread_bps, 3)} bps`) +
        metric('Fee Mode', feeModeLabel, `${cur.fee_precision_note || 'fee estimate detail unavailable'} | basis ${feeBasis}`) +
        metric('Sizing Read', behaviour, `size risk ${fmt(sample.size_risk_multiplier, 2)}x | quote mode ${sample.quote_mode || 'n/a'}`);

      document.getElementById('kills').innerHTML =
        metric('Kill Episodes', fmt(cur.kill_episode_count, 0), `passive closes ${fmt(cur.passive_episode_count, 0)}`) +
        metric('Kill PnL', usd(cur.kill_realized_pnl_total, 4), `margin ${pct(killMarginPct, 3)} on ${usd(killTurnover, 0)} turnover | ${fmt(cur.kill_edge_bps, 3)} bps`, cls(cur.kill_realized_pnl_total, 'pnl')) +
        metric('Passive PnL', usd(cur.passive_realized_pnl_total, 4), `margin ${pct(passiveMarginPct, 3)} on ${usd(passiveTurnover, 0)} turnover | ${fmt(cur.passive_edge_bps, 3)} bps`, cls(cur.passive_realized_pnl_total, 'pnl')) +
        metric('Kill Share Of Turnover', `${closedTurnover > 0 ? fmt((Number(cur.kill_closed_turnover || 0) / closedTurnover) * 100, 1) : 'n/a'}%`, 'completed-episode turnover ending in kills', Number(cur.kill_edge_bps || 0) < 0 ? 'warn' : 'good') +
        metric('Kill Share Of Loss', `${grossRealized !== 0 ? fmt((Math.abs(Number(cur.kill_realized_pnl_total || 0)) / Math.max(Math.abs(grossRealized), 1e-9)) * 100, 1) : 'n/a'}%`, 'absolute kill drag relative to gross realised pnl', Number(cur.kill_realized_pnl_total || 0) < 0 ? 'warn' : 'good') +
        metric('Top Loss Bucket', topLossBucket ? topLossBucket.label : 'n/a', topLossBucket ? `${fmt(topLossBucket.count, 0)} episodes | ${usd(topLossBucket.realized_pnl, 4)} on ${usd(topLossBucketTurnover, 0)} (${pct(topLossBucketMarginPct, 3)})` : 'no realized losses yet', topLossBucket && Number(topLossBucket.realized_pnl || 0) < 0 ? 'warn' : 'good') +
        metric('Loss Side Split', `long ${fmt(cur.long_loss_count, 0)} / short ${fmt(cur.short_loss_count, 0)}`, `${usd(cur.long_loss_pnl_total, 4)} on ${usd(longLossTurnover, 0)} (${pct(longLossMarginPct, 3)}) vs ${usd(cur.short_loss_pnl_total, 4)} on ${usd(shortLossTurnover, 0)} (${pct(shortLossMarginPct, 3)})`, Number(cur.long_loss_pnl_total || 0) < Number(cur.short_loss_pnl_total || 0) ? 'warn' : 'good') +
        metric('Quote vs Kill Mix', `${fmt(cur.passive_fills, 0)} passive`, `${fmt(cur.kill_fills, 0)} aggressive kill fills`, Number(cur.kill_fills || 0) > Number(cur.passive_fills || 0) ? 'warn' : 'good') +
        metric('Protection Samples', `${fmt(cur.inventory_protection_samples, 0)}`, `event regime ${fmt(cur.event_regime_samples, 0)} | stale skips ${fmt(cur.stale_quote_skips, 0)}`) +
        metric('Kill Efficiency', `${fmt(cur.avg_best_markout_bps, 3)} best`, `net spread ${fmt(cur.net_edge_bps, 3)} bps | realised spread ${fmt(cur.avg_realized_spread_bps, 3)} bps`, Number(cur.net_edge_bps || 0) > 0 ? 'good' : 'warn') +
        metric('Stand-Down Reason', sample.quoting_enabled ? 'quoting live' : (sample.quoting_reason || 'n/a'), sample.quoting_enabled ? cap.detail : (sample.decision_note || cap.detail), sample.quoting_enabled ? 'good' : 'warn') +
        metric('Latest Close Reason', latestClosed.close_reason || 'n/a', latestClosed.close_reason && String(latestClosed.close_reason).startsWith('kill') ? 'latest close was a kill path' : 'latest close was passive or flat') +
        metric('Behaviour', behaviour, `quote mode ${sample.quote_mode || 'n/a'} | latest decision ${sample.decision_note || 'n/a'}`, sample.quoting_enabled ? 'good' : 'warn');

      renderPnlChart(cur.pnl_series || []);

      const fillsRows = (data.recent_fills || []).slice().reverse().map((row) => `
        <tr>
          <td>${row.timestamp_utc || ''}</td>
          <td>${row.fill_id || ''}</td>
          <td>${row.episode_id || ''}</td>
          <td>${row.liquidity_role || ''}</td>
          <td>${row.side || ''}</td>
          <td>${usd(row.price, 4)}</td>
          <td>${fmt(row.size, 4)}</td>
          <td>${usd(row.notional, 2)}</td>
          <td>${row.reason || ''}</td>
          <td>${fmt(row.inventory_qty_after, 4)}</td>
          <td>${usd(row.realized_pnl_delta, 4)}</td>
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
          <td>${usd(row.entry_price_avg, 4)}</td>
          <td>${usd(row.exit_price_avg, 4)}</td>
          <td>${usd(row.realized_pnl, 4)}</td>
          <td>${fmt(row.hold_seconds, 3)}</td>
          <td>${fmt(row.best_markout_bps, 3)}</td>
          <td>${fmt(row.worst_markout_bps, 3)}</td>
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
    cache = DashboardSummaryCache(
        events_path,
        samples_path,
        fills_path,
        trades_path,
        reports_dir,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/summary":
                payload = cache.get_summary()
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

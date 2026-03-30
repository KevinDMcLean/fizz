#!/usr/bin/env python3
"""Dashboard for the professional exhaustion/reversal oil trader."""

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local dashboard for the pro exhaustion/reversal bot")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8792)
    parser.add_argument("--events-jsonl", default="logs/exhaustion_pro_events.jsonl")
    parser.add_argument("--samples-jsonl", default="logs/exhaustion_pro_samples.jsonl")
    parser.add_argument("--trades-csv", default="logs/exhaustion_pro_trades.csv")
    parser.add_argument("--markouts-jsonl", default="logs/exhaustion_pro_markouts.jsonl")
    parser.add_argument("--reports-dir", default="logs/exhaustion_pro_reports")
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
                "long_reversal_score": round(_to_float(sample.get("long_reversal_score")) or 0.0, 4),
                "short_reversal_score": round(_to_float(sample.get("short_reversal_score")) or 0.0, 4),
                "mid": round(_to_float(sample.get("mid")) or 0.0, 8),
            }
        )
    return _compress_series(points, limit)


def _headline(sample: Dict[str, Any], run: Dict[str, Any]) -> str:
    if not sample:
        return "Waiting for live market data."
    state = str(sample.get("state") or "idle")
    regime = str(sample.get("regime") or "idle")
    anchor_side = sample.get("anchor_side")
    if state == "in_position" and sample.get("position_side"):
        return (
            f"Running a {sample.get('position_side')} exhaustion reversal. The strategy is now managing a fade against the prior stretch with both target and trailing protection active."
        )
    if state == "reversal_watch" and anchor_side:
        return f"{anchor_side} reversal anchor is live. The app has identified exhaustion and is waiting for enough rebound plus flow/book flip to actually commit."
    if state == "watch":
        return "The tape looks stretched, but reversal confirmation is not there yet. This is where discipline matters more than eagerness."
    if regime == "blocked":
        return "Standing down because spread or data quality is not good enough to fade this move professionally."
    return "Flat and waiting for a genuine exhaustion event followed by real reversal evidence."


def _gate_matrix(sample: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    def row(prefix: str) -> List[Dict[str, Any]]:
        return [
            {"label": "Anchor live", "ok": bool(sample.get(f"{prefix}_anchor_live"))},
            {"label": "Rebound", "ok": bool(sample.get(f"{prefix}_rebound_ok"))},
            {"label": "Flow flip", "ok": bool(sample.get(f"{prefix}_flow_flip_ok"))},
            {"label": "Book flip", "ok": bool(sample.get(f"{prefix}_book_flip_ok"))},
            {"label": "Trade count", "ok": bool(sample.get(f"{prefix}_trade_count_ok"))},
        ]
    return {"long": row("long"), "short": row("short")}


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

    open_position = None
    if latest_sample.get("position_side"):
        open_position = {
            "side": latest_sample.get("position_side"),
            "trade_id": latest_sample.get("open_trade_id"),
            "entry_price": _to_float(latest_sample.get("entry_price")),
            "active_stop": _to_float(latest_sample.get("active_stop")),
            "locked_stop": _to_float(latest_sample.get("locked_stop")),
            "take_profit_price": _to_float(latest_sample.get("take_profit_price")),
            "hold_seconds": _to_float(latest_sample.get("hold_seconds")),
            "unrealized_pnl": _to_float(latest_sample.get("unrealized_pnl")),
            "trail_armed": bool(latest_sample.get("trail_armed")),
        }

    recent_trades = sorted(
        current_trades,
        key=lambda row: _parse_utc(row.get("close_time_utc")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:24]
    recent_markouts = current_markouts[-24:]

    current_run = {
        "start_utc": last_started.get("timestamp_utc") if last_started else None,
        "state": latest_sample.get("state"),
        "regime": latest_sample.get("regime"),
        "headline": _headline(latest_sample, {}),
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
        "anchors_observed": sum(1 for row in current_events if row.get("event") == "anchor_opened"),
        "signal_candidates": sum(1 for row in current_events if row.get("event") == "signal_candidate"),
        "signals_confirmed": sum(1 for row in current_events if row.get("event") == "signal_confirmed"),
        "entries": sum(1 for row in current_events if row.get("event") == "position_opened"),
        "exits": sum(1 for row in current_events if row.get("event") == "position_closed"),
        "polling_errors": sum(1 for row in current_events if row.get("event") == "polling_error"),
        "stale_quote_skips": sum(1 for row in current_events if row.get("event") == "stale_quote_skipped"),
        "quote_gap_warnings": sum(1 for row in current_events if row.get("event") == "quote_gap_warning"),
        "reconnects": sum(1 for row in current_events if row.get("event") == "feed_reconnected"),
        "p95_quote_age_ms": _percentile(quote_ages, 0.95),
        "p95_transport_delay_ms": _percentile(transports, 0.95),
        "p95_spread_bps": _percentile(spreads, 0.95),
        "max_quote_age_ms": _to_float(last_started.get("max_quote_age_ms")) if last_started else None,
        "regime_counts": dict(regime_counts),
        "state_counts": dict(state_counts),
        "exit_reason_counts": dict(exit_reason_counts),
        "gate_matrix": _gate_matrix(latest_sample),
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
    return """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Exhaustion Reversal Pro</title>
  <style>
    :root {
      --bg-0: #0a1116;
      --bg-1: #101922;
      --bg-2: #171f29;
      --card: rgba(255,255,255,0.06);
      --border: rgba(255,255,255,0.08);
      --text: #eef5f8;
      --muted: #98aeb9;
      --good: #78efc0;
      --warn: #ffce7a;
      --bad: #ff8f98;
      --accent: #f6b74a;
      --accent-2: #59d9ff;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      --sans: "SF Pro Display", ui-sans-serif, system-ui, sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        radial-gradient(circle at top left, rgba(246,183,74,0.13), transparent 34%),
        radial-gradient(circle at top right, rgba(89,217,255,0.11), transparent 33%),
        linear-gradient(180deg, var(--bg-0), var(--bg-1) 58%, #090f14 100%);
      color: var(--text);
      font: 13px/1.45 var(--sans);
    }
    .wrap { max-width: 1520px; margin: 0 auto; padding: 18px 18px 28px; }
    .topbar { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; margin-bottom:14px; }
    h1 { margin:0; font-size:28px; letter-spacing:-0.04em; }
    .sub { color:var(--muted); margin-top:6px; max-width:950px; }
    .stamp { color:var(--muted); font-family:var(--mono); font-size:12px; text-align:right; }
    .headline {
      margin:0 0 14px;
      padding:12px 14px;
      border:1px solid var(--border);
      border-radius:14px;
      background:linear-gradient(135deg, rgba(246,183,74,0.10), rgba(89,217,255,0.07));
      color:#f9fcff;
      font-size:13px;
    }
    .cards { display:grid; grid-template-columns:repeat(6, minmax(0, 1fr)); gap:10px; margin-bottom:12px; }
    .card {
      border:1px solid var(--border);
      border-radius:14px;
      padding:11px 12px 12px;
      background:var(--card);
      min-height:84px;
      backdrop-filter: blur(8px);
    }
    .label { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:0.09em; }
    .value { margin-top:6px; font-size:25px; font-weight:700; letter-spacing:-0.04em; font-family:var(--mono); }
    .meta { margin-top:6px; color:var(--muted); font-size:11px; }
    .value.good { color:var(--good); }
    .value.warn { color:var(--warn); }
    .value.bad { color:var(--bad); }
    .grid-2 { display:grid; grid-template-columns:1.35fr 1fr; gap:12px; margin-bottom:12px; }
    .panel { border:1px solid var(--border); border-radius:16px; background:var(--card); padding:14px; }
    .panel h2 { margin:0 0 10px; font-size:15px; letter-spacing:-0.02em; }
    .panel-note { color:var(--muted); font-size:12px; margin:-2px 0 12px; }
    .chart-wrap { height:250px; border:1px solid rgba(255,255,255,0.05); border-radius:12px; background:rgba(255,255,255,0.025); padding:8px; }
    canvas { width:100%; height:100%; display:block; }
    .metrics { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:10px; }
    .metric { background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.05); border-radius:12px; padding:10px; }
    .metric .k { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:0.08em; }
    .metric .v { margin-top:4px; font:600 17px/1.25 var(--mono); }
    .bands { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:12px; margin-bottom:12px; }
    .side-box { border:1px solid var(--border); border-radius:14px; padding:12px; background:rgba(255,255,255,0.035); }
    .side-title { display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; font-weight:600; }
    .score { font:700 20px/1 var(--mono); }
    .gate { display:flex; justify-content:space-between; align-items:center; padding:7px 0; border-top:1px solid rgba(255,255,255,0.05); }
    .gate:first-of-type { border-top:0; }
    .dot { width:10px; height:10px; border-radius:999px; display:inline-block; margin-right:8px; background:rgba(255,255,255,0.16); }
    .dot.ok { background:var(--good); box-shadow:0 0 16px rgba(120,239,192,0.35); }
    .tiny { font-size:11px; color:var(--muted); font-family:var(--mono); }
    .lower { display:grid; grid-template-columns:1fr 1.15fr; gap:12px; }
    table { width:100%; border-collapse:collapse; font-family:var(--mono); font-size:12px; }
    th, td { padding:8px 6px; border-top:1px solid rgba(255,255,255,0.06); text-align:left; white-space:nowrap; }
    th { color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:0.08em; }
    tbody tr:hover { background:rgba(255,255,255,0.03); }
    .tag { display:inline-flex; align-items:center; padding:4px 7px; border-radius:999px; background:rgba(255,255,255,0.07); font-size:11px; color:var(--text); margin-right:6px; margin-bottom:6px; font-family:var(--mono); }
    .tag.good { background:rgba(120,239,192,0.13); color:var(--good); }
    .tag.warn { background:rgba(255,206,122,0.13); color:var(--warn); }
    .tag.bad { background:rgba(255,143,152,0.13); color:var(--bad); }
    .footer-note { margin-top:10px; color:var(--muted); font-size:11px; }
    @media (max-width:1280px) {
      .cards { grid-template-columns:repeat(3, minmax(0, 1fr)); }
      .grid-2, .lower, .bands { grid-template-columns:1fr; }
    }
    @media (max-width:760px) {
      .wrap { padding:12px; }
      .topbar { flex-direction:column; }
      .cards { grid-template-columns:repeat(2, minmax(0, 1fr)); }
      .metrics { grid-template-columns:1fr; }
      .value { font-size:21px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div>
        <h1>Exhaustion Reversal Pro</h1>
        <div class="sub">A professional fade sleeve for stretched oil tape. It does not catch the impulse itself. It waits for an exhaustion anchor, then only trades when rebound, flow, and book say the move is actually rolling over.</div>
      </div>
      <div class="stamp" id="stamp">Loading…</div>
    </div>
    <div class="headline" id="headline">Waiting for dashboard data…</div>
    <div class="cards" id="cards"></div>
    <div class="grid-2">
      <div class="panel">
        <h2>Equity Curve</h2>
        <div class="panel-note">Full-run realized and total PnL. This is the operator-level answer to whether the fade sleeve is smoothing the stack or just bleeding in noise.</div>
        <div class="chart-wrap"><canvas id="pnlChart"></canvas></div>
      </div>
      <div class="panel">
        <h2>Reversal Pressure</h2>
        <div class="panel-note">Long and short reversal scores. These rise only when a stretched move starts to rebound and the tape stops agreeing with the original impulse.</div>
        <div class="chart-wrap"><canvas id="scoreChart"></canvas></div>
      </div>
    </div>
    <div class="grid-2">
      <div class="panel">
        <h2>Stretch And Reversal Quality</h2>
        <div class="panel-note">Live tape conditions, reversal anchor state, and whether the current fade is actually executable at professional quality.</div>
        <div class="metrics" id="qualityMetrics"></div>
      </div>
      <div class="panel">
        <h2>Run Context</h2>
        <div class="panel-note">State machine, anchor state, open risk, and practical context for what the strategy is doing right now.</div>
        <div id="contextTags"></div>
        <div class="footer-note" id="contextNote"></div>
      </div>
    </div>
    <div class="bands">
      <div class="side-box">
        <div class="side-title"><span>Long Reversal Ladder</span><span class="score" id="longScore">0</span></div>
        <div id="longGates"></div>
      </div>
      <div class="side-box">
        <div class="side-title"><span>Short Reversal Ladder</span><span class="score" id="shortScore">0</span></div>
        <div id="shortGates"></div>
      </div>
    </div>
    <div class="lower">
      <div class="panel">
        <h2>Recent Trades</h2>
        <div class="panel-note">Closed fades only. Focus on realized bps, hold time, and exit reason rather than raw frequency.</div>
        <div style="overflow:auto">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Side</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>PnL</th>
                <th>bps</th>
                <th>Hold</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody id="tradesTable"></tbody>
          </table>
        </div>
      </div>
      <div class="panel">
        <h2>Post-Trade Diagnostics</h2>
        <div class="panel-note">MFE, MAE, and capture ratio show whether the reversal sleeve is taking enough of the snapback while still killing bad fades fast.</div>
        <div style="overflow:auto">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Side</th>
                <th>MFE</th>
                <th>MAE</th>
                <th>Capture</th>
                <th>Exit</th>
              </tr>
            </thead>
            <tbody id="markoutsTable"></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
  <script>
    function fmtNum(v, digits = 2) {
      if (v === null || v === undefined || Number.isNaN(Number(v))) return "n/a";
      return Number(v).toFixed(digits);
    }
    function fmtSigned(v, digits = 2) {
      if (v === null || v === undefined || Number.isNaN(Number(v))) return "n/a";
      const n = Number(v);
      return `${n >= 0 ? "+" : ""}${n.toFixed(digits)}`;
    }
    function statusClass(v) {
      if (v === null || v === undefined) return "";
      if (typeof v === "string") {
        if (["running", "in_position"].includes(v)) return "good";
        if (["watch", "reversal_watch", "armed", "cooldown"].includes(v)) return "warn";
        if (["blocked", "stopped"].includes(v)) return "bad";
        return "";
      }
      return Number(v) >= 0 ? "good" : "bad";
    }
    function drawChart(canvasId, series, fields, colors, minHint = null, maxHint = null) {
      const canvas = document.getElementById(canvasId);
      const ctx = canvas.getContext("2d");
      const rect = canvas.getBoundingClientRect();
      canvas.width = rect.width * devicePixelRatio;
      canvas.height = rect.height * devicePixelRatio;
      ctx.scale(devicePixelRatio, devicePixelRatio);
      ctx.clearRect(0, 0, rect.width, rect.height);
      ctx.fillStyle = "rgba(255,255,255,0.02)";
      ctx.fillRect(0, 0, rect.width, rect.height);
      if (!series || !series.length) {
        ctx.fillStyle = "#98aeb9";
        ctx.font = "12px ui-monospace, monospace";
        ctx.fillText("No data yet", 12, 20);
        return;
      }
      const pad = {l: 42, r: 12, t: 14, b: 22};
      const xs = series.map((_, i) => pad.l + (i / Math.max(1, series.length - 1)) * (rect.width - pad.l - pad.r));
      let values = [];
      for (const field of fields) for (const point of series) values.push(Number(point[field] ?? 0));
      let min = minHint !== null ? minHint : Math.min(...values);
      let max = maxHint !== null ? maxHint : Math.max(...values);
      if (min === max) { min -= 1; max += 1; }
      const y = (v) => rect.height - pad.b - ((v - min) / (max - min)) * (rect.height - pad.t - pad.b);
      ctx.strokeStyle = "rgba(255,255,255,0.08)";
      ctx.lineWidth = 1;
      for (let i = 0; i < 4; i++) {
        const gy = pad.t + ((rect.height - pad.t - pad.b) * i / 3);
        ctx.beginPath(); ctx.moveTo(pad.l, gy); ctx.lineTo(rect.width - pad.r, gy); ctx.stroke();
      }
      ctx.fillStyle = "#98aeb9";
      ctx.font = "11px ui-monospace, monospace";
      ctx.fillText(max.toFixed(2), 4, pad.t + 4);
      ctx.fillText(min.toFixed(2), 4, rect.height - pad.b);
      fields.forEach((field, idx) => {
        ctx.strokeStyle = colors[idx];
        ctx.lineWidth = 2;
        ctx.beginPath();
        series.forEach((point, i) => {
          const px = xs[i];
          const py = y(Number(point[field] ?? 0));
          if (i === 0) ctx.moveTo(px, py);
          else ctx.lineTo(px, py);
        });
        ctx.stroke();
      });
      fields.forEach((field, idx) => {
        ctx.fillStyle = colors[idx];
        ctx.fillRect(pad.l + idx * 140, rect.height - 14, 14, 2);
        ctx.fillStyle = "#d8e4ea";
        ctx.fillText(field, pad.l + 18 + idx * 140, rect.height - 10);
      });
    }
    function renderCards(runState, run) {
      const sample = run.latest_sample || {};
      const cards = [
        ["Run State", runState, `state machine ${run.state || "n/a"}`],
        ["Regime", run.regime || "n/a", `anchor ${sample.anchor_side || "none"}`],
        ["Total PnL", fmtSigned(run.total_pnl, 3), `realized ${fmtSigned(run.realized_pnl_total, 3)}`],
        ["Unrealized", fmtSigned(run.unrealized_pnl, 3), `win rate ${fmtNum(run.win_rate_pct, 1)}%`],
        ["Long Rev Score", fmtNum(sample.long_reversal_score, 1), `short ${fmtNum(sample.short_reversal_score, 1)}`],
        ["Live Mid", fmtNum(sample.mid, 4), `spread ${fmtNum(sample.spread_bps, 2)} bps`],
        ["Anchors", String(run.anchors_observed || 0), `signals ${run.signals_confirmed || 0}`],
        ["Entries", String(run.entries || 0), `exits ${run.exits || 0}`],
        ["Quote Age p95", fmtNum(run.p95_quote_age_ms, 0), `limit ${fmtNum(run.max_quote_age_ms, 0)} ms`],
        ["Transport p95", fmtNum(run.p95_transport_delay_ms, 0), `stale skips ${run.stale_quote_skips || 0}`],
        ["Realized bps", fmtSigned(run.avg_realized_bps, 2), `MFE ${fmtNum(run.avg_mfe_bps, 2)} / MAE ${fmtNum(run.avg_mae_bps, 2)}`],
        ["Avg Hold", fmtNum(run.avg_hold_seconds, 1), `capture ${fmtNum(run.avg_capture_ratio, 2)}`],
      ];
      document.getElementById("cards").innerHTML = cards.map(([label, value, meta]) => `
        <div class="card">
          <div class="label">${label}</div>
          <div class="value ${statusClass(value)}">${value}</div>
          <div class="meta">${meta}</div>
        </div>`).join("");
    }
    function renderMetrics(run) {
      const sample = run.latest_sample || {};
      const metrics = [
        ["Fast impulse", `${fmtSigned(sample.fast_impulse_bps, 2)} bps`],
        ["Confirm impulse", `${fmtSigned(sample.confirm_impulse_bps, 2)} bps`],
        ["Rebound from low", `${fmtNum(sample.rebound_from_low_bps, 2)} bps`],
        ["Pullback from high", `${fmtNum(sample.pullback_from_high_bps, 2)} bps`],
        ["Flow imbalance", fmtSigned(sample.flow_imbalance, 3)],
        ["Book imbalance", fmtSigned(sample.book_imbalance, 3)],
        ["Trade count", `${sample.trade_count || 0}`],
        ["Trade rate", `${fmtNum(sample.trade_rate_per_second, 2)}/s`],
        ["Quote age", `${fmtNum(sample.quote_age_ms, 0)} ms`],
        ["Transport delay", `${fmtNum(sample.transport_delay_ms, 0)} ms`],
        ["Recent vol", `${fmtNum(sample.recent_vol_bps, 2)} bps`],
        ["Anchor age", sample.anchor_age_seconds === null || sample.anchor_age_seconds === undefined ? "n/a" : `${fmtNum(sample.anchor_age_seconds, 1)}s`],
      ];
      document.getElementById("qualityMetrics").innerHTML = metrics.map(([k, v]) => `
        <div class="metric"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
    }
    function renderContext(run) {
      const sample = run.latest_sample || {};
      const tags = [];
      const pushTag = (label, cls="") => tags.push(`<span class="tag ${cls}">${label}</span>`);
      pushTag(`state:${run.state || "n/a"}`, statusClass(run.state));
      pushTag(`regime:${run.regime || "n/a"}`, statusClass(run.regime));
      if (sample.anchor_side) pushTag(`anchor:${sample.anchor_side}`, "warn");
      if (sample.anchor_age_seconds !== null && sample.anchor_age_seconds !== undefined) pushTag(`anchor_age:${fmtNum(sample.anchor_age_seconds, 1)}s`);
      if (run.open_position) {
        pushTag(`open:${run.open_position.side}`, "good");
        pushTag(`stop:${fmtNum(run.open_position.active_stop, 4)}`);
        pushTag(`tp:${fmtNum(run.open_position.take_profit_price, 4)}`);
        if (run.open_position.trail_armed) pushTag("trail:armed", "good");
      }
      pushTag(`reconnects:${run.reconnects || 0}`);
      pushTag(`gaps:${run.quote_gap_warnings || 0}`);
      document.getElementById("contextTags").innerHTML = tags.join("");
      document.getElementById("contextNote").textContent = run.headline || "";
    }
    function renderGates(run) {
      const sample = run.latest_sample || {};
      document.getElementById("longScore").textContent = fmtNum(sample.long_reversal_score, 1);
      document.getElementById("shortScore").textContent = fmtNum(sample.short_reversal_score, 1);
      const renderSide = (id, gates) => {
        document.getElementById(id).innerHTML = gates.map(g => `
          <div class="gate">
            <div><span class="dot ${g.ok ? "ok" : ""}"></span>${g.label}</div>
            <div class="tiny">${g.ok ? "pass" : "wait"}</div>
          </div>`).join("");
      };
      renderSide("longGates", run.gate_matrix.long || []);
      renderSide("shortGates", run.gate_matrix.short || []);
    }
    function renderTrades(run) {
      document.getElementById("tradesTable").innerHTML = (run.recent_trades || []).map(row => `
        <tr>
          <td>${row.trade_id || ""}</td>
          <td>${row.side || ""}</td>
          <td>${fmtNum(row.entry_price, 4)}</td>
          <td>${fmtNum(row.exit_price, 4)}</td>
          <td class="${Number(row.realized_pnl || 0) >= 0 ? "good" : "bad"}">${fmtSigned(row.realized_pnl, 3)}</td>
          <td>${fmtSigned(row.realized_bps, 2)}</td>
          <td>${fmtNum(row.hold_seconds, 1)}s</td>
          <td>${row.exit_reason || ""}</td>
        </tr>`).join("");
      document.getElementById("markoutsTable").innerHTML = (run.recent_markouts || []).map(row => `
        <tr>
          <td>${row.trade_id || ""}</td>
          <td>${row.side || ""}</td>
          <td>${fmtSigned(row.mfe_bps, 2)}</td>
          <td>${fmtSigned(row.mae_bps, 2)}</td>
          <td>${fmtNum(row.capture_ratio, 2)}</td>
          <td>${row.exit_reason || ""}</td>
        </tr>`).join("");
    }
    async function refresh() {
      const res = await fetch('/api/summary');
      const data = await res.json();
      const run = data.current_run || {};
      document.getElementById("stamp").textContent = `Updated ${data.generated_at_utc || ""}`;
      document.getElementById("headline").textContent = run.headline || "No headline available.";
      renderCards(data.run_state || "unknown", run);
      renderMetrics(run);
      renderContext(run);
      renderGates(run);
      renderTrades(run);
      drawChart("pnlChart", run.pnl_series || [], ["realized_pnl", "total_pnl"], ["#78efc0", "#59d9ff"]);
      drawChart("scoreChart", run.score_series || [], ["long_reversal_score", "short_reversal_score"], ["#f6b74a", "#ff8f98"], 0, 100);
    }
    const VISIBLE_REFRESH_MS = 5000;
    const HIDDEN_REFRESH_MS = 30000;
    let refreshTimer = null;
    let refreshInFlight = false;

    const scheduleRefresh = (delayMs) => {
      if (refreshTimer) {
        clearTimeout(refreshTimer);
      }
      refreshTimer = setTimeout(() => {
        refreshLoop().catch(console.error);
      }, delayMs);
    };

    async function refreshLoop(force = false) {
      if (refreshInFlight) {
        scheduleRefresh(VISIBLE_REFRESH_MS);
        return;
      }
      if (document.hidden && !force) {
        scheduleRefresh(HIDDEN_REFRESH_MS);
        return;
      }
      refreshInFlight = true;
      try {
        await refresh();
      } finally {
        refreshInFlight = false;
        scheduleRefresh(document.hidden ? HIDDEN_REFRESH_MS : VISIBLE_REFRESH_MS);
      }
    }

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) {
        refreshLoop(true).catch(console.error);
      }
    });

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
    print(f"Exhaustion reversal dashboard running on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
SUMMARY_REFRESH_SECONDS = 5.0

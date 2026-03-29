#!/usr/bin/env python3
"""Dashboard for the rare-event momentum paper-trading engine."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local dashboard for pro oil momentum bot")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--events-jsonl", default="logs/pro_events.jsonl")
    parser.add_argument("--samples-jsonl", default="logs/pro_samples.jsonl")
    parser.add_argument("--trades-csv", default="logs/pro_trades.csv")
    parser.add_argument("--reports-dir", default="logs/pro_reports")
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


def _read_trades(path: Path) -> List[Dict[str, str]]:
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
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _latest(rows: List[Dict[str, Any]], key: str, expected: str | None = None) -> Dict[str, Any] | None:
    for row in reversed(rows):
        if expected is None or row.get(key) == expected:
            return row
    return None


def _build_trade_records(
    events: List[Dict[str, Any]],
    latest_sample: Dict[str, Any] | None,
    since_ts: datetime | None,
) -> List[Dict[str, Any]]:
    records: Dict[str, Dict[str, Any]] = {}
    for event in events:
        event_ts = _parse_utc(event.get("timestamp_utc"))
        if since_ts is not None and (event_ts is None or event_ts < since_ts):
            continue
        trade_id = event.get("trade_id")
        if trade_id is None:
            continue
        key = str(trade_id)
        kind = event.get("event")
        if kind == "position_opened":
            signal = event.get("signal") if isinstance(event.get("signal"), dict) else {}
            records[key] = {
                "trade_id": trade_id,
                "side": event.get("side"),
                "status": "OPEN",
                "entry_time_utc": event.get("timestamp_utc"),
                "entry_price": event.get("entry_price"),
                "entry_reason": event.get("signal_reason"),
                "stop_distance_bps": event.get("stop_distance_bps"),
                "trail_distance_bps": event.get("trail_distance_bps"),
                "active_stop": event.get("initial_stop"),
                "locked_stop": None,
                "trail_arm_bps": event.get("trail_arm_bps"),
                "entry_impulse_bps": signal.get("impulse_bps"),
                "entry_threshold_bps": signal.get("dynamic_threshold_bps"),
                "entry_flow_imbalance": signal.get("flow_imbalance"),
                "entry_book_imbalance": signal.get("book_imbalance"),
                "entry_recent_vol_bps": signal.get("recent_vol_bps"),
                "entry_trade_count": signal.get("trade_count"),
                "exit_time_utc": None,
                "exit_price": None,
                "exit_reason": None,
                "pnl": None,
                "gross_pnl": None,
                "roi_pct_principal": None,
                "capture_ratio": None,
                "mfe_bps": None,
                "mae_bps": None,
                "hold_seconds": None,
                "unrealized_pnl": None,
            }
        elif kind == "trailing_stop_updated" and key in records:
            records[key]["active_stop"] = event.get("active_stop", records[key].get("active_stop"))
            records[key]["locked_stop"] = event.get("locked_stop", records[key].get("locked_stop"))
        elif kind == "position_closed" and key in records:
            records[key]["status"] = "CLOSED"
            records[key]["exit_time_utc"] = event.get("timestamp_utc")
            records[key]["exit_price"] = event.get("exit_price")
            records[key]["exit_reason"] = event.get("exit_reason")
            records[key]["pnl"] = event.get("pnl")
            records[key]["gross_pnl"] = event.get("gross_pnl")
            records[key]["roi_pct_principal"] = event.get("roi_pct_principal")
            records[key]["capture_ratio"] = event.get("capture_ratio")
            records[key]["mfe_bps"] = event.get("mfe_bps")
            records[key]["mae_bps"] = event.get("mae_bps")
            records[key]["hold_seconds"] = event.get("hold_seconds")

    if latest_sample is not None:
        bid = _to_float(latest_sample.get("bid"))
        ask = _to_float(latest_sample.get("ask"))
        for rec in records.values():
            if rec.get("status") != "OPEN":
                continue
            entry = _to_float(rec.get("entry_price"))
            if entry is None:
                continue
            if rec.get("side") == "LONG" and bid is not None:
                rec["unrealized_pnl"] = ((bid / entry) - 1.0) * 100.0
            elif rec.get("side") == "SHORT" and ask is not None:
                rec["unrealized_pnl"] = ((entry / ask) - 1.0) * 100.0

    rows = list(records.values())
    rows.sort(key=lambda row: (row.get("entry_time_utc") or "", row.get("trade_id")), reverse=True)
    return rows


def build_summary(events_path: Path, samples_path: Path, trades_path: Path, reports_dir: Path) -> Dict[str, Any]:
    events = _read_jsonl(events_path)
    samples = _read_jsonl(samples_path)
    trades = _read_trades(trades_path)
    starts = [row for row in events if row.get("event") == "bot_started"]
    stops = [row for row in events if row.get("event") == "bot_stopped"]
    last_started = starts[-1] if starts else None
    last_started_ts = _parse_utc(last_started.get("timestamp_utc")) if last_started else None
    recent_events = [
        row
        for row in events
        if row.get("event") not in {"market_tick"}
        and (last_started_ts is None or (_parse_utc(row.get("timestamp_utc")) or datetime.min.replace(tzinfo=timezone.utc)) >= last_started_ts)
    ]
    latest_sample = _latest(samples, "asset")
    current_samples = []
    if last_started_ts is not None:
        for row in samples:
            ts = _parse_utc(row.get("timestamp_utc"))
            if ts is not None and ts >= last_started_ts:
                current_samples.append(row)
    else:
        current_samples = samples
    current_latest_sample = current_samples[-1] if current_samples else latest_sample
    current_trade_records = _build_trade_records(events, current_latest_sample, last_started_ts)

    current_realized = 0.0
    current_closed = 0
    current_wins = 0
    for row in current_trade_records:
        pnl = _to_float(row.get("pnl"))
        if pnl is None:
            continue
        current_realized += pnl
        current_closed += 1
        if pnl > 0:
            current_wins += 1

    latest_report = None
    if reports_dir.exists():
        reports = sorted(reports_dir.glob("*.md"))
        if reports:
            latest_report = str(reports[-1])

    run_state = "unknown"
    if last_started is not None:
        run_state = "running"
        for stop in reversed(stops):
            stop_ts = _parse_utc(stop.get("timestamp_utc"))
            if stop_ts is not None and last_started_ts is not None and stop_ts >= last_started_ts:
                run_state = "stopped"
                break

    p95_quote_age = None
    p95_spread = None
    p95_transport = None
    if current_samples:
        quote_ages = sorted(
            v for v in (_to_float(row.get("quote_age_ms")) for row in current_samples) if v is not None
        )
        spreads = sorted(
            v for v in (_to_float(row.get("spread_bps")) for row in current_samples) if v is not None
        )
        transports = sorted(
            v for v in (_to_float(row.get("transport_delay_ms")) for row in current_samples) if v is not None
        )

        def p95(values: List[float]) -> float | None:
            if not values:
                return None
            idx = max(0, min(len(values) - 1, int(len(values) * 0.95) - 1))
            return round(values[idx], 3)

        p95_quote_age = p95(quote_ages)
        p95_spread = p95(spreads)
        p95_transport = p95(transports)

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_state": run_state,
        "run_count": len(starts),
        "latest_report": latest_report,
        "last_started": last_started,
        "latest_sample": latest_sample,
        "current_run": {
            "start_utc": last_started.get("timestamp_utc") if last_started else None,
            "latest_sample": current_latest_sample,
            "trade_records": current_trade_records[:100],
            "open_positions": sum(1 for row in current_trade_records if row.get("status") == "OPEN"),
            "closed_trades": current_closed,
            "realized_pnl_total": current_realized,
            "win_rate_pct": ((current_wins / current_closed) * 100.0) if current_closed else 0.0,
            "signals_confirmed": sum(1 for row in recent_events if row.get("event") == "signal_confirmed"),
            "entries": sum(1 for row in recent_events if row.get("event") == "position_opened"),
            "exits": sum(1 for row in recent_events if row.get("event") == "position_closed"),
            "polling_errors": sum(1 for row in recent_events if row.get("event") == "polling_error"),
            "p95_quote_age_ms": p95_quote_age,
            "p95_spread_bps": p95_spread,
            "p95_transport_delay_ms": p95_transport,
            "max_quote_age_ms": _to_float(last_started.get("max_quote_age_ms")) if last_started else None,
        },
        "recent_events": recent_events[-30:],
        "trade_rows_count": len(trades),
    }


def render_html() -> str:
    return """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Pro Oil Momentum Dashboard</title>
  <style>
    :root {
      --bg-a: #07151d;
      --bg-b: #1b2d23;
      --card: rgba(255,255,255,0.08);
      --text: #edf6ef;
      --muted: #b3c6bc;
      --good: #78f2b3;
      --warn: #ffcf70;
      --bad: #ff8f8f;
      --border: rgba(255,255,255,0.12);
    }
    body {
      margin: 0;
      color: var(--text);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      background:
        radial-gradient(circle at 15% 15%, rgba(66, 122, 143, 0.45) 0%, transparent 34%),
        radial-gradient(circle at 85% 10%, rgba(64, 120, 76, 0.4) 0%, transparent 36%),
        linear-gradient(160deg, var(--bg-a), var(--bg-b));
      min-height: 100vh;
      padding: 24px;
    }
    .wrap { max-width: 1400px; margin: 0 auto; }
    h1 { margin: 0 0 10px 0; font-size: 28px; }
    .sub { color: var(--muted); margin-bottom: 16px; }
    .section { margin-bottom: 18px; }
    .section h2 { margin: 0 0 10px 0; font-size: 16px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 12px;
      backdrop-filter: blur(5px);
    }
    .label { color: var(--muted); font-size: 12px; }
    .value { font-size: 20px; margin-top: 6px; }
    .good { color: var(--good); }
    .warn { color: var(--warn); }
    .bad { color: var(--bad); }
    .row { display: flex; gap: 12px; flex-wrap: wrap; }
    .half { flex: 1 1 500px; }
    .full { flex: 1 1 100%; }
    pre {
      margin: 0;
      white-space: pre-wrap;
      font-size: 12px;
      line-height: 1.4;
      max-height: 360px;
      overflow: auto;
    }
    .table-wrap { max-height: 420px; overflow: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td {
      border-bottom: 1px solid var(--border);
      padding: 6px 4px;
      text-align: left;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      position: sticky;
      top: 0;
      background: rgba(5, 15, 18, 0.95);
    }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Pro Oil Momentum Dashboard</h1>
    <div class="sub" id="sub">Loading...</div>
    <div class="section">
      <h2>Market Quality</h2>
      <div class="grid" id="marketCards"></div>
    </div>
    <div class="section">
      <h2>Signal Quality</h2>
      <div class="grid" id="signalCards"></div>
    </div>
    <div class="section">
      <h2>Execution Quality</h2>
      <div class="grid" id="executionCards"></div>
    </div>
    <div class="row">
      <div class="card half">
        <div class="label">Recent Events</div>
        <pre id="events"></pre>
      </div>
      <div class="card half">
        <div class="label">Latest Report</div>
        <pre id="report"></pre>
      </div>
      <div class="card full">
        <div class="label">Trade Records</div>
        <div class="table-wrap">
          <table id="tradeTable">
            <thead>
              <tr>
                <th>ID</th><th>Side</th><th>Status</th><th>Entry</th><th>Exit</th><th>Stop</th><th>Impulse</th><th>Thresh</th><th>Flow</th><th>Book</th><th>PnL</th><th>MFE</th><th>MAE</th><th>Capture</th><th>Reason</th>
              </tr>
            </thead>
            <tbody><tr><td colspan="15">Loading...</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
  <script>
    function card(label, value, cls='') {
      return `<div class="card"><div class="label">${label}</div><div class="value ${cls}">${value}</div></div>`;
    }
    function fmt(n, digits=2) {
      if (typeof n !== 'number') return n ?? 'n/a';
      return Number(n).toFixed(digits);
    }
    function fmtInt(n) {
      if (typeof n !== 'number') return n ?? 'n/a';
      return String(Math.round(n));
    }
    async function refresh() {
      const res = await fetch('/api/summary');
      const data = await res.json();
      const cur = data.current_run || {};
      const sample = cur.latest_sample || {};
      const state = data.run_state || 'unknown';
      const stateCls = state === 'running' ? 'good' : (state === 'stopped' ? 'warn' : 'bad');
      const quoteAge = sample.quote_age_ms;
      const maxQuoteAge = cur.max_quote_age_ms;
      const quoteAgeCls =
        typeof quoteAge === 'number' && typeof maxQuoteAge === 'number'
          ? (quoteAge <= maxQuoteAge ? 'good' : 'bad')
          : 'warn';
      const regimeCls = sample.regime_on ? 'good' : 'warn';
      const longCls = sample.long_ready ? 'good' : 'warn';
      const shortCls = sample.short_ready ? 'good' : 'warn';
      document.getElementById('sub').textContent =
        `Updated ${data.generated_at_utc} | Run ${state} | Last sample ${sample.sample_exchange_time_utc ?? 'n/a'}`;
      document.getElementById('marketCards').innerHTML =
        card('Run State', state, stateCls) +
        card('Bid', fmt(sample.bid, 4)) +
        card('Ask', fmt(sample.ask, 4)) +
        card('Mid', fmt(sample.mid, 4)) +
        card('Microprice', fmt(sample.microprice, 4)) +
        card('Spread bps', fmt(sample.spread_bps, 3), (sample.spread_bps ?? 0) <= 4 ? 'good' : 'warn') +
        card('Quote Age ms', fmtInt(sample.quote_age_ms), quoteAgeCls) +
        card('Transport ms', fmtInt(sample.transport_delay_ms), (sample.transport_delay_ms ?? 0) <= 300 ? 'good' : 'warn') +
        card('p95 Quote Age', fmt(cur.p95_quote_age_ms, 1)) +
        card('p95 Spread', fmt(cur.p95_spread_bps, 3)) +
        card('p95 Transport', fmt(cur.p95_transport_delay_ms, 1));
      document.getElementById('signalCards').innerHTML =
        card('Regime On', sample.regime_on ? 'yes' : 'no', regimeCls) +
        card('Impulse bps', fmt(sample.impulse_bps, 2), Math.abs(sample.impulse_bps ?? 0) >= Math.abs(sample.dynamic_threshold_bps ?? 0) ? 'good' : 'warn') +
        card('Dyn Threshold', fmt(sample.dynamic_threshold_bps, 2)) +
        card('Recent Vol bps', fmt(sample.recent_vol_bps, 2)) +
        card('Flow Imbalance', fmt(sample.flow_imbalance, 3), Math.abs(sample.flow_imbalance ?? 0) >= 0.2 ? 'good' : 'warn') +
        card('Book Imbalance', fmt(sample.book_imbalance, 3), Math.abs(sample.book_imbalance ?? 0) >= 0.12 ? 'good' : 'warn') +
        card('Trade Count', fmtInt(sample.trade_count)) +
        card('Trade Rate/s', fmt(sample.trade_rate_per_second, 2)) +
        card('Breakout Dist L', fmt(sample.breakout_distance_long_bps, 2), sample.long_breakout ? 'good' : 'warn') +
        card('Breakout Dist S', fmt(sample.breakout_distance_short_bps, 2), sample.short_breakout ? 'good' : 'warn') +
        card('Long Ready', sample.long_ready ? 'yes' : 'no', longCls) +
        card('Short Ready', sample.short_ready ? 'yes' : 'no', shortCls);
      document.getElementById('executionCards').innerHTML =
        card('Signals Confirmed', cur.signals_confirmed ?? 0) +
        card('Entries', cur.entries ?? 0) +
        card('Exits', cur.exits ?? 0) +
        card('Open Positions', cur.open_positions ?? 0) +
        card('Closed Trades', cur.closed_trades ?? 0) +
        card('Realized PnL', fmt(cur.realized_pnl_total ?? 0), (cur.realized_pnl_total ?? 0) >= 0 ? 'good' : 'bad') +
        card('Win Rate %', fmt(cur.win_rate_pct ?? 0)) +
        card('Loop Errors', cur.polling_errors ?? 0, (cur.polling_errors ?? 0) > 0 ? 'warn' : 'good') +
        card('Trade Rows', data.trade_rows_count ?? 0);

      document.getElementById('events').textContent =
        (data.recent_events || []).map(row => JSON.stringify(row)).join('\\n') || 'No events yet.';
      document.getElementById('report').textContent =
        (cur.start_utc ? `Current run started: ${cur.start_utc}\\n` : '') + (data.latest_report || 'No report yet.');

      const rows = (cur.trade_records || []).map(row => `
        <tr>
          <td>${row.trade_id ?? ''}</td>
          <td>${row.side ?? ''}</td>
          <td>${row.status ?? ''}</td>
          <td>${fmt(Number(row.entry_price), 4)}</td>
          <td>${fmt(Number(row.exit_price), 4)}</td>
          <td>${fmt(Number(row.active_stop), 4)}</td>
          <td>${fmt(Number(row.entry_impulse_bps), 2)}</td>
          <td>${fmt(Number(row.entry_threshold_bps), 2)}</td>
          <td>${fmt(Number(row.entry_flow_imbalance), 3)}</td>
          <td>${fmt(Number(row.entry_book_imbalance), 3)}</td>
          <td>${fmt(Number(row.pnl), 4)}</td>
          <td>${fmt(Number(row.mfe_bps), 2)}</td>
          <td>${fmt(Number(row.mae_bps), 2)}</td>
          <td>${fmt(Number(row.capture_ratio), 3)}</td>
          <td>${row.exit_reason ?? row.entry_reason ?? ''}</td>
        </tr>
      `).join('');
      document.querySelector('#tradeTable tbody').innerHTML =
        rows || '<tr><td colspan="15">No trades yet.</td></tr>';
    }
    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>"""


def make_handler(events_path: Path, samples_path: Path, trades_path: Path, reports_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/summary":
                body = json.dumps(
                    build_summary(events_path, samples_path, trades_path, reports_dir)
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path in {"/", "/index.html"}:
                body = render_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(404)
            self.end_headers()

        def log_message(self, _format: str, *args: Any) -> None:
            return

    return Handler


def main() -> None:
    args = parse_args()
    handler = make_handler(
        Path(args.events_jsonl),
        Path(args.samples_jsonl),
        Path(args.trades_csv),
        Path(args.reports_dir),
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Dashboard: http://{args.host}:{args.port}")
    print(f"Events file: {args.events_jsonl}")
    print(f"Samples file: {args.samples_jsonl}")
    print(f"Trades file: {args.trades_csv}")
    print(f"Reports dir: {args.reports_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

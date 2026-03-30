#!/usr/bin/env python3
"""Dashboard for the Trump Liquidity CL market maker."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from atlas_mm_feeaware_dashboard import SummaryCache, build_summary
from news_monitor import NewsCache, parse_json_arg

APP_NAME = "Trump Liquidity"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local dashboard for Trump Liquidity")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8797)
    parser.add_argument("--events-jsonl", default="logs/trump_liquidity_events.jsonl")
    parser.add_argument("--samples-jsonl", default="logs/trump_liquidity_samples.jsonl")
    parser.add_argument("--fills-csv", default="logs/trump_liquidity_fills.csv")
    parser.add_argument("--trades-csv", default="logs/trump_liquidity_trades.csv")
    parser.add_argument("--reports-dir", default="logs/trump_liquidity_reports")
    parser.add_argument("--strategy-title", default="Trump Liquidity")
    parser.add_argument(
        "--strategy-subtitle",
        default="Independent CL liquidity monitor with display-only Trump and Iran headline context.",
    )
    parser.add_argument("--news-keywords-json", default="[]")
    parser.add_argument("--news-sources-json", default="[]")
    return parser.parse_args()


def render_html(title: str, subtitle: str) -> str:
    title_json = json.dumps(title)
    subtitle_json = json.dumps(subtitle)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    :root {{
      --bg-a: #130d0a;
      --bg-b: #302019;
      --panel: rgba(255, 255, 255, 0.07);
      --panel-strong: rgba(255, 255, 255, 0.11);
      --text: #f4ede8;
      --muted: #c1b2a7;
      --good: #8ae0a0;
      --warn: #f0c76d;
      --bad: #f08a7d;
      --line: rgba(255, 255, 255, 0.10);
      --accent: #f2a65a;
      --accent-soft: rgba(242, 166, 90, 0.16);
      --body: "Avenir Next", "Segoe UI", "Helvetica Neue", sans-serif;
      --mono: "JetBrains Mono", "IBM Plex Mono", ui-monospace, monospace;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: var(--body);
      background:
        radial-gradient(circle at top left, rgba(242, 166, 90, 0.24), transparent 28%),
        linear-gradient(135deg, var(--bg-a), var(--bg-b));
      color: var(--text);
      min-height: 100vh;
    }}
    main {{ padding: 22px; max-width: 1600px; margin: 0 auto; }}
    h1, h2, h3 {{ margin: 0; font-weight: 650; letter-spacing: 0.01em; }}
    h1 {{ font-size: 26px; }}
    h2 {{ font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.12em; }}
    h3 {{ font-size: 14px; color: var(--accent); }}
    p.meta, p.copy {{ margin: 0; color: var(--muted); font-size: 12px; line-height: 1.45; }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      align-items: end;
      gap: 16px;
      margin-bottom: 16px;
    }}
    .hero, .table-panel, .metric, .news-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 16px;
      backdrop-filter: blur(12px);
    }}
    .hero {{
      display: grid;
      grid-template-columns: 1.1fr 0.95fr;
      gap: 14px;
      margin-bottom: 16px;
    }}
    .hero-copy {{
      padding: 16px;
      display: grid;
      gap: 12px;
    }}
    .hero-headline {{
      display: grid;
      gap: 6px;
    }}
    .pulse-strip {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }}
    .pulse {{
      padding: 10px 12px;
      border-radius: 14px;
      background: var(--panel-strong);
      border: 1px solid var(--line);
    }}
    .pulse .k {{
      color: var(--muted);
      font-size: 10px;
      margin-bottom: 6px;
      text-transform: uppercase;
      letter-spacing: 0.09em;
    }}
    .pulse .v {{
      font-family: var(--mono);
      font-size: 18px;
      font-weight: 650;
      line-height: 1.1;
      margin-bottom: 2px;
    }}
    .pulse .s {{
      color: var(--muted);
      font-size: 11px;
      line-height: 1.35;
    }}
    .hero-stats {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }}
    .news-panel {{
      padding: 16px;
      display: grid;
      gap: 12px;
    }}
    .news-top {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: start;
      flex-wrap: wrap;
    }}
    .news-grid {{
      display: grid;
      gap: 10px;
      max-height: 290px;
      overflow: auto;
    }}
    .news-card {{
      padding: 12px;
      background: linear-gradient(180deg, var(--accent-soft), rgba(255,255,255,0.03));
    }}
    .news-meta {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      font-size: 11px;
      color: var(--muted);
      margin-bottom: 8px;
      font-family: var(--mono);
    }}
    .news-title {{
      font-size: 13px;
      line-height: 1.4;
      margin-bottom: 6px;
    }}
    .news-link {{
      font-size: 11px;
      color: var(--accent);
      text-decoration: none;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
      gap: 10px;
    }}
    .section {{
      display: grid;
      gap: 10px;
      margin-bottom: 16px;
    }}
    .metric {{ padding: 11px 12px; min-height: 80px; }}
    .metric .k {{
      color: var(--muted);
      font-size: 11px;
      margin-bottom: 8px;
      text-transform: uppercase;
      letter-spacing: 0.09em;
    }}
    .metric .v {{
      font-family: var(--mono);
      font-size: 19px;
      font-weight: 650;
      line-height: 1.1;
      margin-bottom: 4px;
    }}
    .metric .s {{
      color: var(--muted);
      font-size: 11px;
      line-height: 1.35;
    }}
    .good {{ color: var(--good); }}
    .warn {{ color: var(--warn); }}
    .bad {{ color: var(--bad); }}
    .pill {{
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
    }}
    .split {{
      display: grid;
      grid-template-columns: 1.2fr 1fr;
      gap: 16px;
    }}
    .chart-panel {{
      padding: 12px 14px;
      min-height: 220px;
      display: grid;
      gap: 8px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 16px;
      backdrop-filter: blur(12px);
    }}
    .chart-meta {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 11px;
    }}
    svg.chart {{
      width: 100%;
      height: 180px;
      display: block;
      border-radius: 12px;
      background: rgba(0, 0, 0, 0.08);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
      font-family: var(--mono);
    }}
    th, td {{
      padding: 8px 7px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-weight: 650;
      position: sticky;
      top: 0;
      background: rgba(19, 13, 10, 0.92);
    }}
    .table-panel {{ padding: 0; overflow: auto; max-height: 360px; }}
    .table-panel table {{ min-width: 100%; }}
    .stack {{ display: grid; gap: 8px; }}
    @media (max-width: 980px) {{
      .hero, .split {{ grid-template-columns: 1fr; }}
      .pulse-strip, .hero-stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 720px) {{
      main {{ padding: 14px; }}
      .pulse-strip, .hero-stats {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <div class="topbar">
      <div class="stack">
        <h1>{title}</h1>
        <p class="meta" id="stamp">Loading...</p>
      </div>
      <div class="pill" id="datasource">Loading source...</div>
    </div>
    <section class="hero">
      <div class="hero-copy">
        <div class="hero-headline">
          <h3 id="headline">Loading decision state...</h3>
          <p class="copy" id="strategy-subtitle"></p>
          <p class="copy" id="decision-note"></p>
        </div>
        <div class="pulse-strip" id="pulse-strip"></div>
        <div class="hero-stats" id="hero-stats"></div>
      </div>
      <div class="news-panel">
        <div class="news-top">
          <div class="stack">
            <h3>Trump / Iran Headline Wire</h3>
            <p class="copy">Display-only context feed. Not used as a trading signal.</p>
          </div>
          <div class="pill" id="news-status">News loading...</div>
        </div>
        <div class="grid" id="news-summary"></div>
        <div class="news-grid" id="news-grid"></div>
      </div>
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
          <span>Gross realised, net realised, and net total from current run start</span>
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
              <th>Ep</th><th>Side</th><th>Open</th><th>Close</th><th>Qty</th><th>Gross</th><th>Fees</th><th>Rebates</th><th>Realised</th><th>Hold(s)</th><th>Best Markout</th><th>Reason</th>
            </tr>
          </thead>
          <tbody id="trades"></tbody>
        </table>
      </div>
    </section>
  </main>
  <script>
    const STRATEGY_TITLE = {title_json};
    const STRATEGY_SUBTITLE = {subtitle_json};
    const fmt = (value, digits = 2) => {{
      if (value === null || value === undefined || value === '') return 'n/a';
      const num = Number(value);
      if (Number.isNaN(num)) return String(value);
      return num.toFixed(digits);
    }};
    const cls = (value, kind = 'default', context = {{}}) => {{
      if (kind === 'pnl') return Number(value) >= 0 ? 'good' : 'bad';
      if (kind === 'state') {{
        if (value === 'running' || value === 'fresh' || value === 'ok') return 'good';
        if (value === 'stopped' || value === 'quiet' || value === 'degraded') return 'warn';
        return 'bad';
      }}
      if (kind === 'latency') {{
        const limit = Number(context.limit ?? 900);
        const num = Number(value);
        if (Number.isNaN(num)) return '';
        if (num <= limit * 0.5) return 'good';
        if (num <= limit) return 'warn';
        return 'bad';
      }}
      if (kind === 'imbalance') return Math.abs(Number(value ?? 0)) >= 0.55 ? 'warn' : 'good';
      if (kind === 'spread') {{
        const limit = Number(context.limit ?? 6);
        const num = Number(value);
        if (Number.isNaN(num)) return '';
        if (num <= limit * 0.65) return 'good';
        if (num <= limit) return 'warn';
        return 'bad';
      }}
      return '';
    }};
    const metric = (label, value, sub = '', extraClass = '') => `
      <div class="metric">
        <div class="k">${{label}}</div>
        <div class="v ${{extraClass}}">${{value}}</div>
        <div class="s">${{sub}}</div>
      </div>
    `;
    const pulse = (label, value, sub = '', extraClass = '') => `
      <div class="pulse">
        <div class="k">${{label}}</div>
        <div class="v ${{extraClass}}">${{value}}</div>
        <div class="s">${{sub}}</div>
      </div>
    `;
    const fmtTime = (raw) => {{
      if (!raw) return 'n/a';
      const dt = new Date(raw);
      if (Number.isNaN(dt.getTime())) return raw;
      return dt.toLocaleString();
    }};
    const renderPnlChart = (series) => {{
      const svg = document.getElementById('pnl-chart');
      const summary = document.getElementById('chart-summary');
      if (!series || series.length < 2) {{
        svg.innerHTML = '';
        summary.textContent = 'Not enough samples for a live PnL chart yet.';
        return;
      }}
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
      if (minY === maxY) {{
        minY -= 1;
        maxY += 1;
      }}
      const yScale = (value) => {{
        const t = (value - minY) / (maxY - minY);
        return height - padY - (t * (height - (padY * 2)));
      }};
      const xScale = (idx) => padX + (idx / (series.length - 1)) * (width - (padX * 2));
      const pathFor = (arr) => arr.map((v, i) => `${{i === 0 ? 'M' : 'L'}} ${{xScale(i).toFixed(2)}} ${{yScale(v).toFixed(2)}}`).join(' ');
      const zeroY = yScale(0);
      svg.innerHTML = `
        <line x1="${{padX}}" y1="${{zeroY.toFixed(2)}}" x2="${{width - padX}}" y2="${{zeroY.toFixed(2)}}" stroke="rgba(255,255,255,0.14)" stroke-width="1" />
        <path d="${{pathFor(grossValues)}}" fill="none" stroke="#f0c76d" stroke-width="1.8" />
        <path d="${{pathFor(netValues)}}" fill="none" stroke="#9bc0ff" stroke-width="2" />
        <path d="${{pathFor(totalValues)}}" fill="none" stroke="#8ae0a0" stroke-width="2.4" />
      `;
      summary.textContent = `range ${{fmt(minY, 4)}} to ${{fmt(maxY, 4)}} | gross ${{fmt(grossValues[grossValues.length - 1], 4)}} | net ${{fmt(netValues[netValues.length - 1], 4)}} | total ${{fmt(totalValues[totalValues.length - 1], 4)}}`;
    }};
    function renderNews(news) {{
      const status = news?.status || 'offline';
      const items = Array.isArray(news?.items) ? news.items : [];
      document.getElementById('news-status').className = `pill ${{cls(status, 'state')}}`;
      document.getElementById('news-status').textContent = `News ${{status}}`;
      document.getElementById('news-summary').innerHTML =
        metric('Headlines', fmt(items.length, 0), `updated ${{fmtTime(news?.generated_at_utc)}}`) +
        metric('Sources', fmt(news?.source_count, 0), (news?.sources || []).join(' | ') || 'n/a') +
        metric('Mode', 'Display only', 'not wired into quoting or risk', 'warn');
      const cards = items.map((item) => `
        <div class="news-card">
          <div class="news-meta">
            <span>${{item.source || item.feed_name || 'Unknown source'}}</span>
            <span>${{fmtTime(item.published_utc)}}</span>
          </div>
          <div class="news-title">${{item.title || 'Untitled headline'}}</div>
          <div class="copy">${{(item.matched_terms || []).slice(0, 4).join(', ') || 'keyword match'}}</div>
          ${{item.link ? `<a class="news-link" href="${{item.link}}" target="_blank" rel="noreferrer">Open source story</a>` : ''}}
        </div>
      `).join('');
      const errorCopy = (news?.errors || []).length
        ? `<div class="news-card"><div class="news-title">Feed errors</div><div class="copy">${{news.errors.join(' | ')}}</div></div>`
        : '';
      document.getElementById('news-grid').innerHTML =
        cards || errorCopy || '<div class="news-card"><div class="news-title">No matching Trump / Iran headlines yet.</div><div class="copy">The wire is live, but none of the configured feeds currently match the filter.</div></div>';
    }}
    async function refresh() {{
      const res = await fetch('/api/summary');
      const data = await res.json();
      const cur = data.current_run || {{}};
      const decision = data.decision || {{}};
      const sample = cur.latest_sample || data.latest_sample || {{}};
      const size = cur.size_metrics || {{}};
      const modeCounts = cur.quote_mode_counts || {{}};
      const reasonCounts = cur.quote_reason_counts || {{}};
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
      document.getElementById('stamp').textContent = `Updated ${{data.generated_at_utc}}`;
      document.getElementById('datasource').textContent = `Source: ${{decision.data_source || 'n/a'}} + headline wire`;
      document.getElementById('headline').textContent = decision.headline || 'No decision text yet.';
      document.getElementById('strategy-subtitle').textContent = STRATEGY_SUBTITLE;
      document.getElementById('decision-note').textContent = decision.note || 'Waiting for the next sample.';
      document.getElementById('pulse-strip').innerHTML =
        pulse('Run State', data.run_state || 'unknown', 'engine state', cls(data.run_state, 'state')) +
        pulse('Quote Mode', sample.quote_mode || 'n/a', sample.quoting_reason || 'n/a', sample.quoting_enabled ? 'good' : 'warn') +
        pulse('Spread bps', fmt(cur.current_spread_bps, 3), `p95 ${{fmt(cur.p95_spread_bps, 3)}}`, cls(cur.current_spread_bps, 'spread', {{limit: cur.configured_max_spread_bps}})) +
        pulse('Quote Age', `${{fmt(cur.current_quote_age_ms, 0)}} ms`, `cap ${{fmt(cur.configured_max_quote_age_ms, 0)}} ms`, cls(cur.current_quote_age_ms, 'latency', {{limit: cur.configured_max_quote_age_ms}}));
      document.getElementById('hero-stats').innerHTML =
        metric('Net Total PnL', fmt(netTotal, 4), `net closed ${{fmt(netRealized, 4)}} / unrl ${{fmt(cur.inventory_unrealized_pnl, 4)}}`, cls(netTotal, 'pnl')) +
        metric('Gross Closed PnL', fmt(grossRealized, 4), `fee drag ${{fmt(feeDrag, 4)}}`, cls(grossRealized, 'pnl')) +
        metric('Inventory', `${{sample.inventory_side || 'FLAT'}} ${{fmt(sample.inventory_qty, 4)}}`, `mark ${{fmt(sample.inventory_unrealized_bps, 2)}} bps`, sample.inventory_side === 'FLAT' ? 'warn' : 'good') +
        metric('Turnover', `$${{fmt(fillTurnover, 0)}}`, `closed $${{fmt(closedTurnover, 0)}}`) +
        metric('Maker Mix', `${{fmt(makerShare, 1)}}%`, `taker ${{fmt(takerShare, 1)}}%`) +
        metric('Capital', `$${{fmt(cur.configured_account_balance, 0)}} / $${{fmt(size.buying_power, 0)}}`, `equity / ${{fmt(leverage, 0)}}x buying power`);
      document.getElementById('market').innerHTML =
        metric('Bid / Ask', `${{fmt(sample.bid, 4)}} / ${{fmt(sample.ask, 4)}}`, `mid ${{fmt(sample.mid, 4)}} micro ${{fmt(sample.microprice, 4)}}`) +
        metric('Spread bps', fmt(cur.current_spread_bps, 3), `p50 ${{fmt(cur.p50_spread_bps, 3)}} | p95 ${{fmt(cur.p95_spread_bps, 3)}} | max ${{fmt(cur.max_spread_bps_seen, 3)}}`, cls(cur.current_spread_bps, 'spread', {{limit: cur.configured_max_spread_bps}})) +
        metric('Quote Age ms', fmt(cur.current_quote_age_ms, 0), `p50 ${{fmt(cur.p50_quote_age_ms, 0)}} | p95 ${{fmt(cur.p95_quote_age_ms, 0)}} | max ${{fmt(cur.max_quote_age_ms_seen, 0)}}`, cls(cur.current_quote_age_ms, 'latency', {{limit: cur.configured_max_quote_age_ms}})) +
        metric('Transport ms', fmt(cur.current_transport_delay_ms, 0), `p50 ${{fmt(cur.p50_transport_delay_ms, 0)}} | p95 ${{fmt(cur.p95_transport_delay_ms, 0)}}`, cls(cur.current_transport_delay_ms, 'latency', {{limit: cur.configured_max_quote_age_ms}})) +
        metric('Vol / Impulse', `${{fmt(sample.recent_vol_bps, 2)}} / ${{fmt(sample.impulse_bps, 2)}}`, 'bps over short horizon') +
        metric('Trade Rate', fmt(sample.trade_rate_per_second, 2), `trade count ${{fmt(sample.trade_count, 0)}}`) +
        metric('Flow Imbalance', fmt(sample.flow_imbalance, 3), `buy ${{fmt(sample.buy_volume, 2)}} / sell ${{fmt(sample.sell_volume, 2)}}`, cls(sample.flow_imbalance, 'imbalance')) +
        metric('Book Imbalance', fmt(sample.book_imbalance, 3), `bid depth ${{fmt(sample.bid_depth, 2)}} / ask depth ${{fmt(sample.ask_depth, 2)}}`, cls(sample.book_imbalance, 'imbalance')) +
        metric('Feed Health', sample.event_regime ? 'event regime' : 'normal', `stale skips ${{fmt(cur.stale_quote_skips, 0)}} | reconnects ${{fmt(cur.feed_reconnects, 0)}}`, sample.event_regime ? 'bad' : 'good') +
        metric('Volume Run Rate', `$${{fmt(cur.fee_daily_volume_run_rate, 0)}}`, `weighted 14d ${{fmt(cur.fee_projected_weighted_14d_volume, 0)}}`, 'good');
      document.getElementById('quotes').innerHTML =
        metric('Quote Reason', sample.quoting_reason || 'n/a', decision.note || '', sample.quoting_enabled ? 'good' : 'warn') +
        metric('Bid Side', sample.bid_enabled ? (sample.bid_quote_price ? fmt(sample.bid_quote_price, 4) : 'enabled') : 'off', sample.bid_reason || 'n/a', sample.bid_enabled ? 'good' : 'warn') +
        metric('Ask Side', sample.ask_enabled ? (sample.ask_quote_price ? fmt(sample.ask_quote_price, 4) : 'enabled') : 'off', sample.ask_reason || 'n/a', sample.ask_enabled ? 'good' : 'warn') +
        metric('Base / Max Notional', `${{fmt(cur.configured_base_order_notional, 0)}} / ${{fmt(cur.configured_max_inventory_notional, 0)}}`, 'per side base / total inventory cap') +
        metric('Active Quote Notional', `${{fmt(size.active_quote_avg_notional, 0)}} avg`, `p95 ${{fmt(size.active_quote_p95_notional, 0)}} | max ${{fmt(size.active_quote_max_notional, 0)}}`) +
        metric('Target Quote Size', `${{fmt(sample.bid_quote_size, 4)}} / ${{fmt(sample.ask_quote_size, 4)}}`, `notional ${{fmt(bidQuoteNotional, 2)}} / ${{fmt(askQuoteNotional, 2)}}`) +
        metric('Tier Volume Boost', `${{fmt(cur.volume_boost_multiplier, 2)}}x`, `target ${{cur.fee_target_tier_label || 'n/a'}} progress ${{fmt(cur.fee_target_tier_progress_pct, 1)}}%`) +
        metric('Size Risk Mult', `${{fmt(sample.size_risk_multiplier, 2)}}x`, 'risk shrink applied before posting') +
        metric('Fair / Reservation', `${{fmt(sample.fair_value, 4)}} / ${{fmt(sample.reservation_price, 4)}}`, `alpha ${{fmt(sample.alpha_bps, 3)}} bps`) +
        metric('Target Half Spread', fmt(sample.target_half_spread_bps, 3), `inventory skew ${{fmt(sample.inventory_skew_bps, 3)}} bps | fee edge ${{fmt(cur.fee_required_edge_bps, 3)}} bps`) +
        metric('Live Bid / Ask', `${{fmt(sample.live_bid_order_price, 4)}} / ${{fmt(sample.live_ask_order_price, 4)}}`, `size ${{fmt(sample.live_bid_order_size, 4)}} / ${{fmt(sample.live_ask_order_size, 4)}}, notional ${{fmt(liveBidNotional, 2)}} / ${{fmt(liveAskNotional, 2)}}`) +
        metric('Queue Ahead', `${{fmt(sample.bid_queue_ahead_size, 3)}} / ${{fmt(sample.ask_queue_ahead_size, 3)}}`, 'bid / ask') +
        metric('Passive Fill Size', `$${{fmt(size.passive_fill_avg_notional, 0)}} avg`, `p95 $${{fmt(size.passive_fill_p95_notional, 0)}} | max $${{fmt(size.passive_fill_max_notional, 0)}}`) +
        metric('Touch Share', `${{fmt(size.passive_fill_avg_touch_share_pct, 1)}}% avg`, `p95 ${{fmt(size.passive_fill_p95_touch_share_pct, 1)}}% | <=25% on ${{fmt(size.passive_fill_under_25pct_touch_pct, 1)}}% of passive fills`) +
        metric('Capacity Usage', `${{fmt(size.passive_fill_avg_buying_power_pct, 2)}}% avg`, `p95 ${{fmt(size.passive_fill_p95_buying_power_pct, 2)}}% of buying power`) +
        metric('Quote Modes', `${{fmt(modeCounts.both || 0, 0)}} both`, `bid-only ${{fmt(modeCounts.bid_only || 0, 0)}} | ask-only ${{fmt(modeCounts.ask_only || 0, 0)}} | flat ${{fmt(modeCounts.flat || 0, 0)}}`) +
        metric('Protection Time', fmt(cur.inventory_protection_samples, 0), `event samples ${{fmt(cur.event_regime_samples, 0)}}`) +
        metric('Toxicity Score', fmt(sample.toxicity_score, 3), `top reasons ${{Object.keys(reasonCounts).slice(0, 3).join(', ') || 'n/a'}}`, Number(sample.toxicity_score) >= 1 ? 'warn' : 'good');
      document.getElementById('execution').innerHTML =
        metric('Passive Fills', fmt(cur.passive_fills, 0), `kill fills ${{fmt(cur.kill_fills, 0)}} / total fills ${{fmt(cur.fills_count, 0)}}`) +
        metric('Closed Episodes', fmt(cur.closed_episodes, 0), `win rate ${{fmt(cur.win_rate_pct, 2)}}%`) +
        metric('Success Split', `${{fmt(size.wins, 0)}} / ${{fmt(size.losses, 0)}} / ${{fmt(size.flats, 0)}}`, `wins / losses / flat, loss rate ${{fmt(size.loss_rate_pct, 2)}}%`) +
        metric('Profit Factor', profitFactor === null || profitFactor === undefined ? 'n/a' : fmt(profitFactor, 2), 'gross winning pnl over losing pnl', profitFactor && profitFactor >= 1.5 ? 'good' : 'warn') +
        metric('Avg Hold', fmt(cur.avg_hold_seconds, 3), 'seconds per episode') +
        metric('Best Markout', fmt(cur.avg_best_markout_bps, 3), 'average best bps') +
        metric('Realised Spread', fmt(cur.avg_realized_spread_bps, 3), `gross ${{fmt(cur.gross_edge_bps, 3)}} bps | net ${{fmt(cur.net_edge_bps, 3)}} bps`, cls(cur.avg_realized_spread_bps, 'pnl')) +
        metric('Fill Turnover', `$${{fmt(fillTurnover, 0)}}`, `${{fmt(size.fill_turnover_units, 2)}} units traded this run`) +
        metric('Closed Turnover', `$${{fmt(closedTurnover, 0)}}`, 'entry plus exit notional across completed episodes') +
        metric('Queue At Fill', `${{fmt(size.passive_fill_queue_ahead_avg, 1)}} avg`, `p95 ${{fmt(size.passive_fill_queue_ahead_p95, 1)}} ahead`) +
        metric('Thin-Book Outliers', `${{fmt(size.passive_fill_over_50pct_touch_count, 0)}}`, 'passive fills >50% of visible touch', size.passive_fill_over_50pct_touch_count > 0 ? 'warn' : 'good') +
        metric('Quote Ops', `${{fmt(cur.quote_posts, 0)}} / ${{fmt(cur.quote_replaces, 0)}}`, `posts / replaces, cancels ${{fmt(cur.quote_cancels, 0)}}`) +
        metric('Loop Errors', fmt(cur.polling_errors, 0), `gap warnings ${{fmt(cur.quote_gap_warnings, 0)}} | reconnects ${{fmt(cur.feed_reconnects, 0)}}`, cur.polling_errors > 0 ? 'bad' : 'good') +
        metric('Latest Fill', cur.latest_fill.reason || 'n/a', `${{cur.latest_fill.liquidity_role || 'n/a'}} ${{cur.latest_fill.side || ''}}`) +
        metric('Realised PnL', fmt(cur.realized_pnl_total, 4), 'current run closed inventory episodes', cls(cur.realized_pnl_total, 'pnl')) +
        metric('Unrealised PnL', fmt(cur.inventory_unrealized_pnl, 4), `current run inventory ${{sample.inventory_side || 'FLAT'}}`, cls(cur.inventory_unrealized_pnl, 'pnl'));
      document.getElementById('fees').innerHTML =
        metric('Tier Basis', feeBasis, `${{cur.fee_market_type || 'n/a'}} | staking ${{cur.fee_staking_tier || 'n/a'}} | source ${{sample.fee_rate_source || 'n/a'}}`) +
        metric('Active Tier', cur.fee_tier_label || 'n/a', `${{cur.fee_actual_tier_label || 'n/a'}} actual / ${{cur.fee_projected_tier_label || 'n/a'}} projected`, 'good') +
        metric('Actual Now bps', `${{fmt(cur.fee_actual_net_maker_rate_bps, 4)}} / ${{fmt(cur.fee_actual_taker_rate_bps, 4)}}`, 'maker / taker if current actual tier is used') +
        metric('Projected Run bps', `${{fmt(cur.fee_projected_net_maker_rate_bps, 4)}} / ${{fmt(cur.fee_projected_taker_rate_bps, 4)}}`, 'maker / taker if current run-rate persists') +
        metric('Decision bps', `${{fmt(cur.fee_net_maker_rate_bps, 4)}} / ${{fmt(cur.fee_taker_rate_bps, 4)}}`, `engine basis ${{feeBasis}} | rebate ${{fmt(cur.fee_maker_rebate_bps, 4)}} bps`) +
        metric('HIP-3 Scaling', `${{sample.fee_growth_mode ? 'growth on' : 'growth off'}}`, `deployer scale ${{fmt(sample.fee_deployer_fee_scale, 4)}} | aligned quote ${{sample.fee_aligned_quote_token ? 'yes' : 'no'}}`) +
        metric('Required Edge', fmt(cur.fee_required_edge_bps, 3), `expected taker share ${{fmt(cur.fee_expected_taker_share_pct, 2)}}%`, (Number(cur.net_edge_bps) >= Number(cur.fee_required_edge_bps)) ? 'good' : 'warn') +
        metric('Run Fee Drag', fmt(cur.fill_fee_drag_total, 4), `maker fees ${{fmt(cur.maker_fee_cost_total, 4)}} | taker fees ${{fmt(cur.taker_fee_cost_total, 4)}} | rebates ${{fmt(cur.maker_rebates_total, 4)}}`, cls(-cur.fill_fee_drag_total, 'pnl')) +
        metric('Gross / Net Realised', `${{fmt(cur.gross_realized_pnl_total, 4)}} / ${{fmt(cur.realized_pnl_total, 4)}}`, `cost impact ${{(grossRealized !== 0) ? fmt((feeDrag / Math.abs(grossRealized)) * 100, 1) : 'n/a'}}% of gross`) +
        metric('Maker / Taker Mix', `${{fmt(makerShare, 1)}}% / ${{fmt(takerShare, 1)}}%`, `$${{fmt(cur.maker_notional_total, 0)}} / ${{fmt(cur.taker_notional_total, 0)}} turnover`) +
        metric('Volume Path', `$${{fmt(cur.fee_daily_volume_run_rate, 0)}}`, `current 14d $${{fmt(cur.fee_current_weighted_14d_volume, 0)}} | projected $${{fmt(cur.fee_projected_weighted_14d_volume, 0)}}`) +
        metric('Target Tier', cur.fee_target_tier_label || 'n/a', `target maker / taker ${{fmt(cur.fee_target_net_maker_rate_bps, 4)}} / ${{fmt(cur.fee_target_taker_rate_bps, 4)}} bps`) +
        metric('Fee Precision', sample.fee_rate_source || 'n/a', cur.fee_precision_note || 'n/a') +
        metric('Edge Surplus', fmt(Number(cur.net_edge_bps || 0) - Number(cur.fee_required_edge_bps || 0), 3), 'net edge minus fee-adjusted required edge', (Number(cur.net_edge_bps || 0) >= Number(cur.fee_required_edge_bps || 0)) ? 'good' : 'warn');
      document.getElementById('kills').innerHTML =
        metric('Kill Episodes', fmt(cur.kill_episode_count, 0), `passive closes ${{fmt(cur.passive_episode_count, 0)}}`) +
        metric('Kill PnL', fmt(cur.kill_realized_pnl_total, 4), `$${{fmt(cur.kill_closed_turnover, 0)}} turnover | ${{fmt(cur.kill_edge_bps, 3)}} bps`, cls(cur.kill_realized_pnl_total, 'pnl')) +
        metric('Passive PnL', fmt(cur.passive_realized_pnl_total, 4), `$${{fmt(cur.passive_closed_turnover, 0)}} turnover | ${{fmt(cur.passive_edge_bps, 3)}} bps`, cls(cur.passive_realized_pnl_total, 'pnl')) +
        metric('Dynamic Kill Floor', fmt(cur.fee_dynamic_kill_floor_bps, 3), 'current adverse exit floor after fees and slippage') +
        metric('Kill Share Of Turnover', `${{closedTurnover > 0 ? fmt((Number(cur.kill_closed_turnover || 0) / closedTurnover) * 100, 1) : 'n/a'}}%`, 'completed-episode turnover ending in kills', Number(cur.kill_edge_bps || 0) < 0 ? 'warn' : 'good') +
        metric('Kill Share Of Loss', `${{grossRealized !== 0 ? fmt((Math.abs(Number(cur.kill_realized_pnl_total || 0)) / Math.max(Math.abs(grossRealized), 1e-9)) * 100, 1) : 'n/a'}}%`, 'absolute kill drag relative to gross realised pnl', Number(cur.kill_realized_pnl_total || 0) < 0 ? 'warn' : 'good') +
        metric('Quote vs Kill Mix', `${{fmt(cur.passive_fills, 0)}} passive`, `${{fmt(cur.kill_fills, 0)}} aggressive kill fills | expected taker ${{fmt(cur.fee_expected_taker_share_pct, 2)}}%`) +
        metric('Protection Samples', `${{fmt(cur.inventory_protection_samples, 0)}}`, `event regime ${{fmt(cur.event_regime_samples, 0)}} | stale skips ${{fmt(cur.stale_quote_skips, 0)}}`) +
        metric('Kill Efficiency', `${{fmt(cur.avg_best_markout_bps, 3)}} best`, `net edge ${{fmt(cur.net_edge_bps, 3)}} bps | realised spread ${{fmt(cur.avg_realized_spread_bps, 3)}} bps`, Number(cur.net_edge_bps || 0) > 0 ? 'good' : 'warn');
      renderPnlChart(cur.pnl_series || []);
      renderNews(data.news || {{}});
      const fillsRows = (data.recent_fills || []).slice().reverse().map((row) => `
        <tr><td>${{row.timestamp_utc || ''}}</td><td>${{row.fill_id || ''}}</td><td>${{row.episode_id || ''}}</td><td>${{row.liquidity_role || ''}}</td><td>${{row.side || ''}}</td><td>${{fmt(row.price, 4)}}</td><td>${{fmt(row.size, 4)}}</td><td>${{row.reason || ''}}</td><td>${{fmt(row.inventory_qty_after, 4)}}</td><td>${{fmt(row.exchange_fee_delta, 4)}}</td><td>${{fmt(row.realized_pnl_delta, 4)}}</td></tr>
      `).join('');
      document.getElementById('fills').innerHTML = fillsRows || '<tr><td colspan="11">No fills yet.</td></tr>';
      const tradeRows = (data.recent_trades || []).slice().reverse().map((row) => `
        <tr><td>${{row.episode_id || ''}}</td><td>${{row.side || ''}}</td><td>${{row.open_time_utc || ''}}</td><td>${{row.close_time_utc || ''}}</td><td>${{fmt(row.max_abs_qty, 4)}}</td><td>${{fmt(row.gross_pnl, 4)}}</td><td>${{fmt(row.fees_paid, 4)}}</td><td>${{fmt(row.maker_rebates, 4)}}</td><td>${{fmt(row.realized_pnl, 4)}}</td><td>${{fmt(row.hold_seconds, 3)}}</td><td>${{fmt(row.best_markout_bps, 3)}}</td><td>${{row.close_reason || ''}}</td></tr>
      `).join('');
      document.getElementById('trades').innerHTML = tradeRows || '<tr><td colspan="12">No closed episodes yet.</td></tr>';
    }}
    const VISIBLE_REFRESH_MS = 5000;
    const HIDDEN_REFRESH_MS = 30000;
    let refreshTimer = null;
    let refreshInFlight = false;
    const scheduleRefresh = (delayMs) => {{
      if (refreshTimer) clearTimeout(refreshTimer);
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
      if (!document.hidden) refreshLoop(true).catch(console.error);
    }});
    refreshLoop(true).catch(console.error);
  </script>
</body>
</html>"""


def make_handler(
    *,
    events_path: Path,
    samples_path: Path,
    fills_path: Path,
    trades_path: Path,
    reports_dir: Path,
    strategy_title: str,
    strategy_subtitle: str,
    news_sources: list[dict[str, str]],
    news_keywords: list[str],
):
    summary_cache = SummaryCache(build_summary, events_path, samples_path, fills_path, trades_path, reports_dir)
    news_cache = NewsCache(sources=news_sources, keywords=news_keywords, limit=8)
    html_bytes = render_html(strategy_title, strategy_subtitle).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/summary":
                payload = json.loads(summary_cache.get_json_bytes())
                payload["news"] = news_cache.get_snapshot()
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if parsed.path in {"/", "/index.html"}:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.end_headers()
                self.wfile.write(html_bytes)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def main() -> None:
    args = parse_args()
    news_sources = parse_json_arg(args.news_sources_json, default=[])
    news_keywords = parse_json_arg(args.news_keywords_json, default=[])
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(
            events_path=Path(args.events_jsonl),
            samples_path=Path(args.samples_jsonl),
            fills_path=Path(args.fills_csv),
            trades_path=Path(args.trades_csv),
            reports_dir=Path(args.reports_dir),
            strategy_title=args.strategy_title,
            strategy_subtitle=args.strategy_subtitle,
            news_sources=news_sources,
            news_keywords=news_keywords,
        ),
    )
    print(f"{APP_NAME}: http://{args.host}:{args.port}")
    print(f"Events: {args.events_jsonl}")
    print(f"Samples: {args.samples_jsonl}")
    print(f"Fills: {args.fills_csv}")
    print(f"Trades: {args.trades_csv}")
    server.serve_forever()


if __name__ == "__main__":
    main()

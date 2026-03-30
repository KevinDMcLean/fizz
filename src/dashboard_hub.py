#!/usr/bin/env python3
"""Local landing page for switching between active trading dashboards."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

APP_NAME = "Kev The Trader - Dashboard Hub"

UMBRELLA_SECTIONS: List[Dict[str, str]] = [
    {
        "slug": "market-making",
        "title": "Market Making",
        "description": "Passive liquidity engines. Shared Kevin engine changes can affect both Kevin HYPE and Kevin CL, while market profile changes stay local to the chosen instrument.",
    },
    {
        "slug": "momentum",
        "title": "Momentum",
        "description": "Directional campaign runners. Shared momentum-engine changes affect momentum instruments in that family, while instrument config changes stay specific to Brent, WTI, or later markets.",
    },
]

DASHBOARD_TARGETS: List[Dict[str, str]] = [
    {
        "slug": "kevin-hype",
        "name": "Kevin HYPE",
        "button_label": "Open Kevin HYPE",
        "url": "http://127.0.0.1:8795/",
        "summary_url": "http://127.0.0.1:8795/api/summary",
        "umbrella": "market-making",
        "family": "Kevin Market Making",
        "scope": "Shared Kevin engine code affects Kevin HYPE and Kevin CL. HYPE config changes affect only HYPE.",
        "fallback_market": "HYPE",
    },
    {
        "slug": "kevin-cl",
        "name": "Kevin CL",
        "button_label": "Open Kevin CL",
        "url": "http://127.0.0.1:8798/",
        "summary_url": "http://127.0.0.1:8798/api/summary",
        "umbrella": "market-making",
        "family": "Kevin Market Making",
        "scope": "Shared Kevin engine code affects Kevin HYPE and Kevin CL. CL-specific config changes affect only Kevin CL.",
        "fallback_market": "CL",
    },
    {
        "slug": "oil-brent",
        "name": "Oil Brent Momentum",
        "button_label": "Open Oil Brent Momentum",
        "url": "http://127.0.0.1:8801/",
        "summary_url": "http://127.0.0.1:8801/api/summary",
        "umbrella": "momentum",
        "family": "Oil Momentum",
        "scope": "Oil momentum code affects only the oil campaign strategy, not Kevin.",
        "fallback_market": "BRENTOIL",
    },
    {
        "slug": "oil-wti",
        "name": "Oil WTI Momentum",
        "button_label": "Open Oil WTI Momentum",
        "url": "http://127.0.0.1:8802/",
        "summary_url": "http://127.0.0.1:8802/api/summary",
        "umbrella": "momentum",
        "family": "Oil Momentum",
        "scope": "Oil momentum code affects only the oil campaign strategy, not Kevin.",
        "fallback_market": "CL",
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local landing page for active trading dashboards")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8811)
    return parser.parse_args()


def _safe_get(payload: Any, *keys: str) -> Any:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _fetch_summary(url: str) -> Dict[str, Any] | None:
    try:
        with urlopen(url, timeout=1.5) as response:
            raw = response.read().decode("utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except (OSError, URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return None
    return None


def collect_dashboard_statuses() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for target in DASHBOARD_TARGETS:
        summary = _fetch_summary(target["summary_url"])
        current_run = summary.get("current_run") if isinstance(summary, dict) else None
        latest_sample = current_run.get("latest_sample") if isinstance(current_run, dict) else None

        strategy = (
            _safe_get(summary, "last_started", "strategy_name")
            or _safe_get(current_run, "strategy_name")
            or _safe_get(latest_sample, "strategy_name")
            or target["family"]
        )
        market = (
            _safe_get(current_run, "market_name")
            or _safe_get(summary, "last_started", "asset")
            or _safe_get(latest_sample, "asset")
            or target["fallback_market"]
        )
        venue = (
            _safe_get(current_run, "venue_name")
            or _safe_get(summary, "last_started", "dex")
            or _safe_get(latest_sample, "dex")
            or "local"
        )
        run_state = summary.get("run_state", "down") if isinstance(summary, dict) else "down"
        headline = (
            _safe_get(current_run, "headline")
            or _safe_get(current_run, "operator_read")
            or _safe_get(current_run, "focus_body")
            or "No live summary available yet."
        )
        port = urlparse(target["url"]).port or ""
        rows.append(
            {
                "umbrella": target["umbrella"],
                "name": target["name"],
                "button_label": target["button_label"],
                "url": target["url"],
                "port": port,
                "family": target["family"],
                "scope": target["scope"],
                "strategy": strategy,
                "market": market,
                "venue": venue if venue else "local",
                "run_state": run_state,
                "headline": headline,
                "is_up": summary is not None,
            }
        )
    return rows


def render_dashboard_hub(statuses: List[Dict[str, Any]], generated_at: str) -> str:
    section_blocks = []
    for section in UMBRELLA_SECTIONS:
        cards = []
        for status in statuses:
            if status["umbrella"] != section["slug"]:
                continue
            state_class = "up" if status["is_up"] else "down"
            cards.append(
                f"""
                <section class="card">
                  <div class="card-top">
                    <div>
                      <div class="eyebrow">{status["family"]}</div>
                      <h2>{status["name"]}</h2>
                    </div>
                    <div class="status {state_class}">{status["run_state"]}</div>
                  </div>
                  <div class="meta">Market: {status["market"]} | Venue: {status["venue"]} | Port: {status["port"]}</div>
                  <div class="headline">{status["headline"]}</div>
                  <div class="scope"><strong>Change scope:</strong> {status["scope"]}</div>
                  <a class="button" href="{status["url"]}" target="_blank" rel="noopener noreferrer">{status["button_label"]}</a>
                </section>
                """
            )
        if not cards:
            continue
        cards_html = "\n".join(cards)
        section_blocks.append(
            f"""
            <section class="umbrella-section">
              <div class="section-head">
                <div class="section-title">{section["title"]}</div>
                <div class="section-copy">{section["description"]}</div>
              </div>
              <div class="grid">
                {cards_html}
              </div>
            </section>
            """
        )

    sections_html = "\n".join(section_blocks)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="8">
  <title>{APP_NAME}</title>
  <style>
    :root {{
      --bg: #08131f;
      --panel: #0d1d2e;
      --panel-2: #10253b;
      --line: #1d3650;
      --text: #eef7ff;
      --muted: #97abc2;
      --mint: #7af0c3;
      --amber: #ffd76a;
      --rose: #ff9d8a;
      --blue: #7dd3fc;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", "Helvetica Neue", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(125, 211, 252, 0.10), transparent 28%),
        radial-gradient(circle at top right, rgba(122, 240, 195, 0.08), transparent 24%),
        var(--bg);
      color: var(--text);
    }}
    .shell {{
      max-width: 1440px;
      margin: 0 auto;
      padding: 18px 22px 20px;
    }}
    .hero {{
      display: grid;
      gap: 8px;
      margin-bottom: 14px;
    }}
    .hero h1 {{
      margin: 0;
      font-size: 34px;
      line-height: 1.05;
      letter-spacing: -0.03em;
    }}
    .hero p {{
      margin: 0;
      color: var(--muted);
      font-size: 15px;
      max-width: 980px;
    }}
    .rules {{
      background: rgba(16, 37, 59, 0.78);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 12px 14px;
      color: var(--muted);
      line-height: 1.45;
      font-size: 14px;
    }}
    .rules strong {{ color: var(--text); }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 12px;
      margin-top: 10px;
    }}
    .umbrella-section {{
      margin-top: 16px;
    }}
    .section-head {{
      display: grid;
      gap: 4px;
      margin-bottom: 4px;
    }}
    .section-title {{
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.18em;
      color: var(--amber);
    }}
    .section-copy {{
      color: var(--muted);
      line-height: 1.4;
      max-width: 1080px;
      font-size: 14px;
    }}
    .card {{
      background: linear-gradient(180deg, rgba(13,29,46,0.98), rgba(9,22,34,0.98));
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 16px;
      box-shadow: 0 14px 34px rgba(0, 0, 0, 0.18);
      display: grid;
      gap: 10px;
    }}
    .card-top {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
    }}
    .eyebrow {{
      color: var(--blue);
      text-transform: uppercase;
      letter-spacing: 0.14em;
      font-size: 11px;
    }}
    h2 {{
      margin: 4px 0 0;
      font-size: 24px;
      line-height: 1.02;
      letter-spacing: -0.03em;
    }}
    .status {{
      padding: 8px 12px;
      border-radius: 999px;
      font-weight: 700;
      text-transform: lowercase;
      border: 1px solid var(--line);
      white-space: nowrap;
    }}
    .status.up {{
      color: var(--mint);
      background: rgba(122, 240, 195, 0.10);
    }}
    .status.down {{
      color: var(--rose);
      background: rgba(255, 157, 138, 0.10);
    }}
    .meta, .scope {{
      color: var(--muted);
      line-height: 1.45;
      font-size: 14px;
    }}
    .headline {{
      color: var(--text);
      min-height: 38px;
      line-height: 1.45;
      font-size: 14px;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}
    .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 44px;
      padding: 0 16px;
      border-radius: 14px;
      background: linear-gradient(135deg, var(--amber), #ffb86c);
      color: #111922;
      font-weight: 800;
      text-decoration: none;
      letter-spacing: 0.01em;
    }}
    .footer {{
      margin-top: 14px;
      color: var(--muted);
      font-size: 13px;
    }}
    @media (min-width: 1180px) {{
      .grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <h1>{APP_NAME}</h1>
      <p>One place to jump between your live local trading dashboards without guessing ports. Each card opens the target dashboard in a new tab and refreshes its status automatically.</p>
      <div class="rules">
        <strong>Scope rule:</strong> market config changes are usually individual, engine-code changes are shared within that engine family, and shared infrastructure changes can affect more than one bot.
      </div>
    </section>
    {sections_html}
    <div class="footer">Updated {generated_at} UTC</div>
  </main>
</body>
</html>
"""


class DashboardHubHandler(BaseHTTPRequestHandler):
    server_version = "DashboardHub/1.0"

    def _send(self, body: str, *, content_type: str = "text/html; charset=utf-8", status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            statuses = collect_dashboard_statuses()
            self._send(
                json.dumps(
                    {
                        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                        "dashboards": statuses,
                    },
                    indent=2,
                ),
                content_type="application/json; charset=utf-8",
            )
            return
        if parsed.path != "/":
            self._send("Not found", content_type="text/plain; charset=utf-8", status=404)
            return

        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        html = render_dashboard_hub(collect_dashboard_statuses(), generated_at)
        self._send(html)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def main() -> int:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHubHandler)
    print(f"{APP_NAME} available at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

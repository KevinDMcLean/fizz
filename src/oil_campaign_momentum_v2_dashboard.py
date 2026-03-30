#!/usr/bin/env python3
"""Dashboard wrapper for Oil Campaign Momentum v2."""

from __future__ import annotations

import pa_momo_mid_dashboard_pro as base

base.APP_NAME = "Oil Campaign Momentum v2"
_BASE_RENDER_HTML = base.render_html


def render_html() -> str:
    html = _BASE_RENDER_HTML()
    html = html.replace(
        '<div class="eyebrow">Medium-Frequency Momentum</div>',
        '<div class="eyebrow">Oil Momentum Trader</div>',
    )
    html = html.replace(
        "<h1>Oil Campaign Momentum v2</h1>",
        '<h1 id="appTitle">Oil Campaign Momentum v2</h1>',
    )
    html = html.replace(
        '<div class="sub">Institutional operator view for the live mid-frequency Brent momentum engine. The page is built to answer four questions quickly: is the tape tradeable, which side has edge, which gates are blocking entry, and how much size the engine actually commits when it does fire.</div>',
        '<div class="sub" id="subTitle">Plain-English operator view for the live oil momentum trader. It shows the active market clearly and explains in simple terms why the strategy is waiting, probing, or holding a move.</div>',
    )
    html = html.replace(
        '<div class="hero-kicker">Current Read</div>',
        '<div class="hero-kicker">What It Is Doing Now</div>',
    )
    html = html.replace(
        '<div class="hero-kicker">Run Context</div>',
        '<div class="hero-kicker">Market And Strategy</div>',
    )
    html = html.replace(
        '<div class="hero-tags" id="heroTags"></div>',
        '<div class="hero-tags" id="heroTags"></div><div class="panel-note" id="operatorSummary">Loading operator summary...</div>',
    )
    html = html.replace("<h2>Current-Run Equity Curve</h2>", "<h2>PnL This Session</h2>")
    html = html.replace("<h2>Score Pressure</h2>", "<h2>Direction Bias</h2>")
    html = html.replace("<h2>Market Quality</h2>", "<h2>Market Snapshot</h2>")
    html = html.replace("<h2>Why It Is Or Is Not Trading</h2>", "<h2>Why It Is Waiting Or Trading</h2>")
    html = html.replace("<h2>Sizing And Risk</h2>", "<h2>Trade Size And Risk</h2>")
    html = html.replace("<h2>Signal Funnel And Persistent Blockers</h2>", "<h2>Entry Filter Summary</h2>")
    html = html.replace("<h2>Post-Trade Diagnostics</h2>", "<h2>Trade Diagnostics</h2>")
    html = html.replace('["Current-Run PnL"', '["PnL This Session"')
    html = html.replace('["Trade Count"', '["Closed Trades"')
    html = html.replace('["Signal Funnel"', '["Entries Accepted"')
    html = html.replace('["Scores"', '["Direction Bias"')
    html = html.replace('["Avg Notional"', '["Avg Trade Size"')
    html = html.replace(
        '"p50 / p95 spread"',
        '"typical spread in basis points"',
    )
    html = html.replace(
        """    function renderHero(runState, run) {
      const sample = run.latest_sample || {};
      document.getElementById("focusTitle").textContent = run.focus_title || run.headline || "No current read.";
      document.getElementById("focusBody").textContent = run.focus_body || run.headline || "";
      const tags = [""",
        """    function renderHero(runState, run) {
      const sample = run.latest_sample || {};
      const asset = sample.asset || "";
      const marketLabel = asset === "xyz:BRENTOIL" ? "Brent" : asset === "xyz:CL" ? "WTI" : (asset || "Unknown");
      const operatorSummary = document.getElementById("operatorSummary");
      document.getElementById("focusTitle").textContent = run.focus_title || run.headline || "No current read.";
      document.getElementById("focusBody").textContent = run.focus_body || run.headline || "";
      const tags = [
        [`market:${marketLabel}`, ""],""",
    )
    html = html.replace(
        """      document.getElementById("heroTags").innerHTML = tags.map(([label, cls]) => `<span class="tag ${cls}">${label}</span>`).join("");
    }""",
        """      document.getElementById("heroTags").innerHTML = tags.map(([label, cls]) => `<span class="tag ${cls}">${label}</span>`).join("");
      if (operatorSummary) {
        const waitingLine = run.open_position
          ? `Holding a ${run.open_position.side || "live"} oil position and managing it with the trailing stop.`
          : (run.focus_body || run.headline || "Waiting for impulse, breakout, flow, and book confirmation to line up.");
        operatorSummary.textContent = `Trading ${marketLabel} oil momentum. ${waitingLine}`;
      }
    }""",
    )
    html = html.replace(
        """      const run = data.current_run || {};
      document.getElementById("stamp").innerHTML = `Updated ${data.generated_at_utc || ""}<br/>Run start ${run.start_utc || "n/a"}`;""",
        """      const run = data.current_run || {};
      const sample = run.latest_sample || {};
      const asset = sample.asset || "";
      const marketLabel = asset === "xyz:BRENTOIL" ? "Brent" : asset === "xyz:CL" ? "WTI" : (asset || "Unknown Market");
      document.title = `${marketLabel} | Oil Campaign Momentum v2`;
      const appTitle = document.getElementById("appTitle");
      if (appTitle) appTitle.textContent = `Oil Campaign Momentum v2 - ${marketLabel}`;
      const subTitle = document.getElementById("subTitle");
      if (subTitle) subTitle.textContent = `Plain-English operator view for the live ${marketLabel} oil momentum trader. It explains what the engine is doing now, why it is waiting, and when it is confident enough to trade.`;
      document.getElementById("stamp").innerHTML = `Updated ${data.generated_at_utc || ""}<br/>Run start ${run.start_utc || "n/a"}`;""",
    )
    return html


base.render_html = render_html


def main() -> None:
    base.main()


if __name__ == "__main__":
    main()

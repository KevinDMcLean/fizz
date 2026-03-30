#!/usr/bin/env python3
"""Dashboard wrapper for Oil Campaign Momentum v2."""

from __future__ import annotations

import pa_momo_mid_dashboard_pro as base

base.APP_NAME = "Oil Campaign Momentum v2"
_BASE_RENDER_HTML = base.render_html


def render_html() -> str:
    html = _BASE_RENDER_HTML()
    html = html.replace(
        "<h1>Oil Campaign Momentum v2</h1>",
        '<h1 id="appTitle">Oil Campaign Momentum v2</h1>',
    )
    html = html.replace(
        '<div class="sub">Institutional operator view for the live mid-frequency Brent momentum engine. The page is built to answer four questions quickly: is the tape tradeable, which side has edge, which gates are blocking entry, and how much size the engine actually commits when it does fire.</div>',
        '<div class="sub" id="subTitle">Institutional operator view for the live oil momentum engine. This page labels the active market clearly so Brent and WTI can be distinguished at a glance.</div>',
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
      document.getElementById("focusTitle").textContent = run.focus_title || run.headline || "No current read.";
      document.getElementById("focusBody").textContent = run.focus_body || run.headline || "";
      const tags = [
        [`market:${marketLabel}`, ""],""",
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
      if (subTitle) subTitle.textContent = `Institutional operator view for the live ${marketLabel} oil momentum engine. This page labels the active market clearly so Brent and WTI can be distinguished at a glance.`;
      document.getElementById("stamp").innerHTML = `Updated ${data.generated_at_utc || ""}<br/>Run start ${run.start_utc || "n/a"}`;""",
    )
    return html


base.render_html = render_html


def main() -> None:
    base.main()


if __name__ == "__main__":
    main()

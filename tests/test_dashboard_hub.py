from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dashboard_hub import collect_dashboard_statuses, render_dashboard_hub


class DashboardHubTests(unittest.TestCase):
    def test_collect_dashboard_statuses_uses_decision_note_when_headline_missing(self) -> None:
        def fake_fetch(url: str):
            if url.endswith("8795/api/summary"):
                return {
                    "run_state": "running",
                    "current_run": {
                        "market_name": "HYPE",
                        "venue_name": "Hyperliquid",
                        "latest_sample": {
                            "decision_note": "bid_only: buy pressure still active",
                            "quoting_reason": "buy_pressure",
                        },
                    },
                }
            return None

        with patch("dashboard_hub._fetch_summary", side_effect=fake_fetch):
            statuses = collect_dashboard_statuses()

        kevin_hype = next(item for item in statuses if item["slug"] == "kevin-hype")
        self.assertEqual(kevin_hype["headline"], "bid_only: buy pressure still active")
        self.assertTrue(kevin_hype["is_up"])

    def test_render_dashboard_hub_includes_requested_buttons(self) -> None:
        html = render_dashboard_hub(
            [
                {
                    "umbrella": "market-making",
                    "name": "Kevin HYPE",
                    "button_label": "Open Kevin HYPE",
                    "url": "http://127.0.0.1:8795/",
                    "port": 8795,
                    "slug": "kevin-hype",
                    "family": "Kevin Market Making",
                    "scope": "HYPE-only config.",
                    "strategy": "Kevin Hype Liquidity Engine",
                    "market": "HYPE",
                    "venue": "Hyperliquid",
                    "run_state": "running",
                    "headline": "Healthy two-way quoting.",
                    "is_up": True,
                },
                {
                    "umbrella": "market-making",
                    "name": "Kevin CL",
                    "button_label": "Open Kevin CL",
                    "url": "http://127.0.0.1:8798/",
                    "port": 8798,
                    "slug": "kevin-cl",
                    "family": "Kevin Market Making",
                    "scope": "CL-only config.",
                    "strategy": "Kevin Liquidity Engine",
                    "market": "xyz:CL",
                    "venue": "xyz",
                    "run_state": "running",
                    "headline": "Active market making.",
                    "is_up": True,
                },
                {
                    "umbrella": "momentum",
                    "name": "Oil Brent Momentum (Choosy)",
                    "button_label": "Open Oil Brent Momentum (Choosy)",
                    "url": "http://127.0.0.1:8801/",
                    "port": 8801,
                    "slug": "oil-brent",
                    "family": "Oil Momentum - Choosy",
                    "scope": "Oil-only strategy code.",
                    "strategy": "Oil Campaign Momentum v2",
                    "market": "xyz:BRENTOIL",
                    "venue": "xyz",
                    "run_state": "running",
                    "headline": "Watching breakout quality.",
                    "is_up": True,
                },
                {
                    "umbrella": "momentum",
                    "name": "Oil WTI Momentum (Choosy)",
                    "button_label": "Open Oil WTI Momentum (Choosy)",
                    "url": "http://127.0.0.1:8802/",
                    "port": 8802,
                    "slug": "oil-wti",
                    "family": "Oil Momentum - Choosy",
                    "scope": "Oil-only strategy code.",
                    "strategy": "Oil Campaign Momentum v2",
                    "market": "xyz:CL",
                    "venue": "xyz",
                    "run_state": "running",
                    "headline": "Watching WTI breakout quality.",
                    "is_up": True,
                },
                {
                    "umbrella": "momentum",
                    "name": "Oil Brent Momentum (Medium)",
                    "button_label": "Open Oil Brent Momentum (Medium)",
                    "url": "http://127.0.0.1:8803/",
                    "port": 8803,
                    "slug": "oil-brent-medium",
                    "family": "Oil Momentum - Medium",
                    "scope": "Oil-only strategy code.",
                    "strategy": "Oil Campaign Momentum v2",
                    "market": "xyz:BRENTOIL",
                    "venue": "xyz",
                    "run_state": "running",
                    "headline": "Relaxed learning runner.",
                    "is_up": True,
                },
                {
                    "umbrella": "momentum",
                    "name": "Oil WTI Momentum (Medium)",
                    "button_label": "Open Oil WTI Momentum (Medium)",
                    "url": "http://127.0.0.1:8804/",
                    "port": 8804,
                    "slug": "oil-wti-medium",
                    "family": "Oil Momentum - Medium",
                    "scope": "Oil-only strategy code.",
                    "strategy": "Oil Campaign Momentum v2",
                    "market": "xyz:CL",
                    "venue": "xyz",
                    "run_state": "running",
                    "headline": "Relaxed learning runner.",
                    "is_up": True,
                },
            ],
            "2026-03-30 08:20:00",
        )

        self.assertIn("Open Kevin HYPE", html)
        self.assertIn("Open Kevin CL", html)
        self.assertIn("Open Oil Brent Momentum (Choosy)", html)
        self.assertIn("Open Oil WTI Momentum (Choosy)", html)
        self.assertIn("Open Oil Brent Momentum (Medium)", html)
        self.assertIn("Open Oil WTI Momentum (Medium)", html)
        self.assertIn("Market Making", html)
        self.assertIn("Momentum", html)
        self.assertIn("http://127.0.0.1:8801/", html)


if __name__ == "__main__":
    unittest.main()

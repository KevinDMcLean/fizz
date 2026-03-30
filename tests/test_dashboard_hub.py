from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dashboard_hub import render_dashboard_hub


class DashboardHubTests(unittest.TestCase):
    def test_render_dashboard_hub_includes_requested_buttons(self) -> None:
        html = render_dashboard_hub(
            [
                {
                    "umbrella": "market-making",
                    "name": "Kevin HYPE",
                    "button_label": "Open Kevin HYPE",
                    "url": "http://127.0.0.1:8795/",
                    "port": 8795,
                    "family": "Kevin engine",
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
                    "family": "Kevin engine",
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
                    "name": "Oil Brent Momentum",
                    "button_label": "Open Oil Brent Momentum",
                    "url": "http://127.0.0.1:8801/",
                    "port": 8801,
                    "family": "Oil Campaign Momentum v2",
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
                    "name": "Oil WTI Momentum",
                    "button_label": "Open Oil WTI Momentum",
                    "url": "http://127.0.0.1:8802/",
                    "port": 8802,
                    "family": "Oil Campaign Momentum v2",
                    "scope": "Oil-only strategy code.",
                    "strategy": "Oil Campaign Momentum v2",
                    "market": "xyz:CL",
                    "venue": "xyz",
                    "run_state": "running",
                    "headline": "Watching WTI breakout quality.",
                    "is_up": True,
                },
            ],
            "2026-03-30 08:20:00",
        )

        self.assertIn("Open Kevin HYPE", html)
        self.assertIn("Open Kevin CL", html)
        self.assertIn("Open Oil Brent Momentum", html)
        self.assertIn("Open Oil WTI Momentum", html)
        self.assertIn("Market Making", html)
        self.assertIn("Momentum", html)
        self.assertIn("http://127.0.0.1:8801/", html)


if __name__ == "__main__":
    unittest.main()

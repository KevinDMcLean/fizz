from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import oil_campaign_momentum_v2_dashboard as dashboard


class OilCampaignMomentumV2DashboardTests(unittest.TestCase):
    def test_wrapper_labels_market_specific_dashboard(self) -> None:
        html = dashboard.render_html()

        self.assertIn('id="appTitle"', html)
        self.assertIn('id="subTitle"', html)
        self.assertIn("Oil Momentum Trader", html)
        self.assertIn("What It Is Doing Now", html)
        self.assertIn("Market And Strategy", html)
        self.assertIn('id="operatorSummary"', html)
        self.assertIn("Trading ${marketLabel} oil momentum.", html)
        self.assertIn("PnL This Session", html)
        self.assertIn("Why It Is Waiting Or Trading", html)
        self.assertIn('document.title = `${marketLabel} | Oil Campaign Momentum v2`;', html)
        self.assertIn("[`market:${marketLabel}`, \"\"]", html)
        self.assertNotIn("live mid-frequency Brent momentum engine", html)


if __name__ == "__main__":
    unittest.main()

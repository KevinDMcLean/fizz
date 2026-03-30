from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kevin_hype_liquidity_dashboard import DashboardSummaryCache


class KevinHypeLiquidityDashboardTests(unittest.TestCase):
    def test_dashboard_cache_refreshes_incrementally(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            events = root / "events.jsonl"
            samples = root / "samples.jsonl"
            fills = root / "fills.csv"
            trades = root / "trades.csv"
            reports = root / "reports"
            reports.mkdir()

            self._append_json(
                events,
                {
                    "timestamp_utc": "2026-03-30T00:00:00+00:00",
                    "event": "bot_started",
                    "account_balance": 1000.0,
                    "leverage": 20,
                    "max_quote_age_ms": 650,
                    "shadow_fee_accounting": True,
                },
            )
            self._append_json(
                samples,
                {
                    "timestamp_utc": "2026-03-30T00:00:01+00:00",
                    "quote_mode": "both",
                    "quoting_reason": "healthy",
                    "decision_note": "balanced",
                    "quote_age_ms": 110,
                    "transport_delay_ms": 95,
                    "spread_bps": 0.42,
                    "event_regime": False,
                    "inventory_unrealized_pnl": 0.0,
                    "live_bid_order_price": 38.50,
                    "live_bid_order_size": 10.0,
                    "live_ask_order_price": 38.51,
                    "live_ask_order_size": 10.0,
                    "bid_depth": 120.0,
                    "ask_depth": 125.0,
                    "bid_queue_ahead_size": 14.0,
                    "ask_queue_ahead_size": 16.0,
                },
            )
            self._write_csv(
                fills,
                [
                    "timestamp_utc",
                    "fill_id",
                    "episode_id",
                    "liquidity_role",
                    "side",
                    "price",
                    "size",
                    "notional",
                    "order_id",
                    "reason",
                    "exchange_fee_delta",
                ],
                [
                    {
                        "timestamp_utc": "2026-03-30T00:00:01.500000+00:00",
                        "fill_id": "1",
                        "episode_id": "7",
                        "liquidity_role": "passive",
                        "side": "BUY",
                        "price": "38.50",
                        "size": "10.0",
                        "notional": "385.0",
                        "order_id": "101",
                        "reason": "quote_traded_through",
                        "exchange_fee_delta": "0.0112",
                    }
                ],
            )
            self._write_csv(
                trades,
                [
                    "episode_id",
                    "side",
                    "open_time_utc",
                    "close_time_utc",
                    "entry_price_avg",
                    "exit_price_avg",
                    "max_abs_qty",
                    "realized_pnl",
                    "gross_pnl",
                    "fees_paid",
                    "maker_fee_cost",
                    "taker_fee_cost",
                    "maker_rebates",
                    "maker_notional",
                    "taker_notional",
                    "hold_seconds",
                    "best_markout_bps",
                    "worst_markout_bps",
                    "passive_entry_fills",
                    "passive_exit_fills",
                    "aggressive_exit_fills",
                    "fill_count",
                    "close_reason",
                ],
                [
                    {
                        "episode_id": "7",
                        "side": "LONG",
                        "open_time_utc": "2026-03-30T00:00:01.400000+00:00",
                        "close_time_utc": "2026-03-30T00:00:01.800000+00:00",
                        "entry_price_avg": "38.50",
                        "exit_price_avg": "38.53",
                        "max_abs_qty": "10.0",
                        "realized_pnl": "0.1888",
                        "gross_pnl": "0.2000",
                        "fees_paid": "0.0112",
                        "maker_fee_cost": "0.0112",
                        "taker_fee_cost": "0.0",
                        "maker_rebates": "0.0",
                        "maker_notional": "770.3",
                        "taker_notional": "0.0",
                        "hold_seconds": "0.4",
                        "best_markout_bps": "1.2",
                        "worst_markout_bps": "-0.3",
                        "passive_entry_fills": "1",
                        "passive_exit_fills": "1",
                        "aggressive_exit_fills": "0",
                        "fill_count": "2",
                        "close_reason": "quote_traded_through",
                    }
                ],
            )

            cache = DashboardSummaryCache(events, samples, fills, trades, reports)
            summary1 = cache.get_summary()
            self.assertEqual(summary1["current_run"]["fills_count"], 1)
            self.assertEqual(summary1["current_run"]["closed_episodes"], 1)
            self.assertEqual(summary1["current_run"]["size_metrics"]["passive_fill_count"], 1)

            self._append_json(
                events,
                {
                    "timestamp_utc": "2026-03-30T00:00:02+00:00",
                    "event": "quote_gap_warning",
                },
            )
            self._append_json(
                samples,
                {
                    "timestamp_utc": "2026-03-30T00:00:03+00:00",
                    "quote_mode": "ask_only",
                    "quoting_reason": "sell_pressure",
                    "decision_note": "leaning down",
                    "quote_age_ms": 118,
                    "transport_delay_ms": 102,
                    "spread_bps": 0.55,
                    "event_regime": False,
                    "inventory_unrealized_pnl": -0.02,
                    "live_bid_order_price": 38.40,
                    "live_bid_order_size": 0.0,
                    "live_ask_order_price": 38.41,
                    "live_ask_order_size": 8.0,
                    "bid_depth": 100.0,
                    "ask_depth": 130.0,
                    "bid_queue_ahead_size": 11.0,
                    "ask_queue_ahead_size": 18.0,
                },
            )
            self._append_csv(
                fills,
                [
                    "timestamp_utc",
                    "fill_id",
                    "episode_id",
                    "liquidity_role",
                    "side",
                    "price",
                    "size",
                    "notional",
                    "order_id",
                    "reason",
                    "exchange_fee_delta",
                ],
                {
                    "timestamp_utc": "2026-03-30T00:00:02.500000+00:00",
                    "fill_id": "2",
                    "episode_id": "8",
                    "liquidity_role": "passive",
                    "side": "SELL",
                    "price": "38.41",
                    "size": "8.0",
                    "notional": "307.28",
                    "order_id": "102",
                    "reason": "quote_traded_through",
                    "exchange_fee_delta": "0.0089",
                },
            )

            summary2 = cache.get_summary()
            self.assertEqual(summary2["current_run"]["fills_count"], 2)
            self.assertEqual(summary2["current_run"]["quote_gap_warnings"], 1)
            self.assertEqual(summary2["current_run"]["latest_fill"]["side"], "SELL")
            self.assertEqual(summary2["current_run"]["latest_sample"]["quote_mode"], "ask_only")

    def _append_json(self, path: Path, payload: dict[str, object]) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")

    def _write_csv(self, path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)

    def _append_csv(self, path: Path, headers: list[str], row: dict[str, str]) -> None:
        with path.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=headers)
            writer.writerow(row)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pa_pump_pro_core import (
    BookSnapshot,
    MarketSnapshot,
    ProMomentumBot,
    Quote,
    TradePrint,
    compute_book_imbalance,
    compute_trade_flow,
    compute_realized_vol_bps,
)


class ProMomentumCoreTests(unittest.TestCase):
    def test_compute_trade_flow_detects_buy_imbalance(self) -> None:
        trades = [
            TradePrint(side="B", price=100.0, size=4.0, hash="1", exchange_time_ms=1_000),
            TradePrint(side="B", price=100.2, size=2.0, hash="2", exchange_time_ms=1_500),
            TradePrint(side="A", price=100.1, size=1.0, hash="3", exchange_time_ms=1_700),
        ]
        flow = compute_trade_flow(trades, since_ms=900)
        self.assertEqual(flow["count"], 3.0)
        self.assertGreater(flow["buy_volume"], flow["sell_volume"])
        self.assertGreater(flow["imbalance"], 0.0)

    def test_compute_book_imbalance_uses_top_levels(self) -> None:
        book = BookSnapshot(
            bids=[(100.0, 10.0), (99.9, 8.0), (99.8, 5.0)],
            asks=[(100.1, 4.0), (100.2, 3.0), (100.3, 2.0)],
            exchange_time_ms=1_000,
            received_time_ms=1_010,
        )
        imbalance, bid_depth, ask_depth = compute_book_imbalance(book, depth_levels=2)
        self.assertAlmostEqual(bid_depth, 18.0)
        self.assertAlmostEqual(ask_depth, 7.0)
        self.assertGreater(imbalance, 0.0)

    def test_realized_vol_is_positive_for_noisy_series(self) -> None:
        samples = [
            (0, 100.00),
            (1_000, 100.05),
            (2_000, 99.95),
            (3_000, 100.10),
            (4_000, 100.00),
        ]
        vol = compute_realized_vol_bps(samples, since_ms=0, impulse_window_seconds=30.0)
        self.assertGreater(vol, 0.0)

    def test_signal_snapshot_can_confirm_long_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp))
            now_ms = int(time.time() * 1000)
            for i in range(40):
                bot._append_price_sample(now_ms - ((40 - i) * 1_000), 100.0 + (i * 0.001))
            trades = [
                TradePrint(side="B", price=100.34, size=3.0, hash="b1", exchange_time_ms=now_ms - 10_000),
                TradePrint(side="B", price=100.36, size=4.0, hash="b2", exchange_time_ms=now_ms - 6_000),
                TradePrint(side="B", price=100.39, size=5.0, hash="b3", exchange_time_ms=now_ms - 2_000),
                TradePrint(side="A", price=100.37, size=1.0, hash="a1", exchange_time_ms=now_ms - 4_000),
            ]
            quote = Quote(
                bid=100.39,
                ask=100.41,
                bid_size=20.0,
                ask_size=8.0,
                mid=100.40,
                microprice=100.404,
                spread=0.02,
                spread_bps=1.99,
                exchange_time_ms=now_ms,
                received_time_ms=now_ms + 150,
                transport_delay_ms=150,
            )
            book = BookSnapshot(
                bids=[(100.39, 20.0), (100.38, 16.0), (100.37, 12.0)],
                asks=[(100.41, 8.0), (100.42, 7.0), (100.43, 6.0)],
                exchange_time_ms=now_ms,
                received_time_ms=now_ms + 155,
            )
            snapshot = MarketSnapshot(quote=quote, book=book, trades=trades, seq=1)
            signal = bot._build_signal_snapshot(snapshot)
            self.assertIsNotNone(signal)
            assert signal is not None
            self.assertTrue(signal.long_ready)
            self.assertFalse(signal.short_ready)

    def test_failed_breakout_exit_closes_trade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp))
            now_ms = int(time.time() * 1000)
            for i in range(40):
                bot._append_price_sample(now_ms - ((40 - i) * 1_000), 100.0 + (i * 0.002))
            trades = [
                TradePrint(side="B", price=100.29, size=4.0, hash="1", exchange_time_ms=now_ms - 2_000),
                TradePrint(side="B", price=100.30, size=4.0, hash="2", exchange_time_ms=now_ms - 1_000),
            ]
            quote = Quote(
                bid=100.30,
                ask=100.32,
                bid_size=14.0,
                ask_size=9.0,
                mid=100.31,
                microprice=100.312,
                spread=0.02,
                spread_bps=1.99,
                exchange_time_ms=now_ms,
                received_time_ms=now_ms + 120,
                transport_delay_ms=120,
            )
            book = BookSnapshot(
                bids=[(100.30, 14.0), (100.29, 11.0), (100.28, 9.0)],
                asks=[(100.32, 9.0), (100.33, 7.0), (100.34, 6.0)],
                exchange_time_ms=now_ms,
                received_time_ms=now_ms + 125,
            )
            signal = bot._build_signal_snapshot(MarketSnapshot(quote=quote, book=book, trades=trades, seq=1))
            assert signal is not None
            bot._open_position(signal, "LONG", "test_long")
            assert bot.position is not None
            bot.position.opened_at = time.time() - 30.0
            stale_signal = bot._build_signal_snapshot(
                MarketSnapshot(
                    quote=Quote(
                        bid=100.31,
                        ask=100.33,
                        bid_size=11.0,
                        ask_size=10.0,
                        mid=100.32,
                        microprice=100.321,
                        spread=0.02,
                        spread_bps=1.99,
                        exchange_time_ms=now_ms + 25_000,
                        received_time_ms=now_ms + 25_130,
                        transport_delay_ms=130,
                    ),
                    book=book,
                    trades=trades,
                    seq=2,
                )
            )
            assert stale_signal is not None
            bot._maybe_close_position(stale_signal)
            self.assertIsNone(bot.position)
            self.assertEqual(bot.exit_count, 1)
            self.assertGreaterEqual(bot.exit_reason_counts.get("failed_breakout", 0), 1)

    def _make_bot(self, root: Path) -> ProMomentumBot:
        return ProMomentumBot(
            asset="xyz:BRENTOIL",
            account_balance=1000.0,
            api_url="https://api.hyperliquid.xyz",
            dex="xyz",
            leverage=20,
            sample_ms=250,
            warm_start_candles=False,
            startup_state_entry=True,
            entry_confirmation_samples=1,
            impulse_window_seconds=30.0,
            breakout_lookback_seconds=60.0,
            volatility_lookback_seconds=60.0,
            flow_window_seconds=30.0,
            min_impulse_bps=10.0,
            volatility_multiplier=1.0,
            breakout_buffer_bps=0.5,
            flow_imbalance_min=0.15,
            book_imbalance_min=0.05,
            min_trade_count=1,
            depth_levels=2,
            max_spread_bps=10.0,
            max_quote_age_ms=5_000,
            quote_gap_warn_ms=10_000,
            reconnect_gap_ms=20_000,
            initial_stop_min_bps=8.0,
            initial_stop_vol_multiplier=1.0,
            trail_min_bps=6.0,
            trail_vol_multiplier=1.0,
            trail_arm_r=0.5,
            break_even_lock_r=1.0,
            failure_hold_seconds=5.0,
            failure_min_followthrough_bps=12.0,
            stop_confirmation_ticks=1,
            slippage_bps=0.0,
            fee_bps=0.0,
            equity_risk_pct=1.0,
            cooldown_seconds=0.0,
            max_daily_loss=30.0,
            max_trades_per_day=20,
            max_trades_per_hour=10,
            min_hold_seconds=0.0,
            events_jsonl_path=str(root / "events.jsonl"),
            samples_jsonl_path=str(root / "samples.jsonl"),
            trades_csv_path=str(root / "trades.csv"),
            report_dir=str(root / "reports"),
        )


if __name__ == "__main__":
    unittest.main()

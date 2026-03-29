from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pa_exhaustion_pro_core import ProExhaustionReversalBot
from pa_pump_pro_core import BookSnapshot, MarketSnapshot, Quote, TradePrint


class ProExhaustionReversalTests(unittest.TestCase):
    def test_downside_exhaustion_then_reversal_confirms_long(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp))
            now_ms = int(time.time() * 1000)
            self._seed_prices(bot, now_ms, slope=-0.002)
            stretch = bot._build_signal_snapshot(self._down_exhaustion_snapshot(now_ms))
            self.assertIsNotNone(stretch)
            assert stretch is not None
            self.assertEqual(bot.anchor.side if bot.anchor else None, "LONG")
            rebound = bot._build_signal_snapshot(self._long_rebound_snapshot(now_ms + 5_000))
            self.assertIsNotNone(rebound)
            assert rebound is not None
            self.assertTrue(rebound.long_ready)
            self.assertFalse(rebound.short_ready)

    def test_upside_exhaustion_then_reversal_confirms_short(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp))
            now_ms = int(time.time() * 1000)
            self._seed_prices(bot, now_ms, slope=0.002)
            stretch = bot._build_signal_snapshot(self._up_exhaustion_snapshot(now_ms))
            self.assertIsNotNone(stretch)
            assert stretch is not None
            self.assertEqual(bot.anchor.side if bot.anchor else None, "SHORT")
            rebound = bot._build_signal_snapshot(self._short_rebound_snapshot(now_ms + 5_000))
            self.assertIsNotNone(rebound)
            assert rebound is not None
            self.assertTrue(rebound.short_ready)
            self.assertFalse(rebound.long_ready)

    def test_anchor_expires_without_reversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp), reversal_window_seconds=3.0)
            now_ms = int(time.time() * 1000)
            self._seed_prices(bot, now_ms, slope=-0.002)
            bot._build_signal_snapshot(self._down_exhaustion_snapshot(now_ms))
            self.assertIsNotNone(bot.anchor)
            neutral = bot._build_signal_snapshot(self._neutral_snapshot(now_ms + 10_000, mid=99.92))
            self.assertIsNotNone(neutral)
            self.assertIsNone(bot.anchor)

    def test_retest_extreme_exit_closes_losing_fade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp))
            now_ms = int(time.time() * 1000)
            self._seed_prices(bot, now_ms, slope=-0.002)
            bot._build_signal_snapshot(self._down_exhaustion_snapshot(now_ms))
            rebound = bot._build_signal_snapshot(self._long_rebound_snapshot(now_ms + 5_000))
            assert rebound is not None
            bot._open_position(rebound, "LONG", "test_long")
            assert bot.position is not None
            bot.position.opened_at = time.time() - 2.0

            retest_mid = bot.position.anchor_extreme_price * 1.00005
            fail = bot._build_signal_snapshot(
                self._neutral_snapshot(now_ms + 9_000, mid=retest_mid)
            )
            assert fail is not None
            bot._maybe_close_position(fail)
            self.assertIsNone(bot.position)
            self.assertEqual(bot.exit_reason_counts.get("retest_extreme"), 1)

    def test_take_profit_exit_closes_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp))
            now_ms = int(time.time() * 1000)
            self._seed_prices(bot, now_ms, slope=-0.002)
            bot._build_signal_snapshot(self._down_exhaustion_snapshot(now_ms))
            rebound = bot._build_signal_snapshot(self._long_rebound_snapshot(now_ms + 5_000))
            assert rebound is not None
            bot._open_position(rebound, "LONG", "test_long")
            assert bot.position is not None
            bot.position.opened_at = time.time() - 4.0
            take_profit_mid = bot.position.take_profit_price + 0.01
            win = bot._build_signal_snapshot(self._positive_snapshot(now_ms + 8_000, mid=take_profit_mid))
            assert win is not None
            bot._maybe_close_position(win)
            self.assertIsNone(bot.position)
            self.assertEqual(bot.exit_reason_counts.get("take_profit_hit"), 1)

    def _make_bot(self, root: Path, **overrides: float) -> ProExhaustionReversalBot:
        params = {
            "asset": "xyz:BRENTOIL",
            "account_balance": 1000.0,
            "api_url": "https://api.hyperliquid.xyz",
            "dex": "xyz",
            "leverage": 20,
            "sample_ms": 150,
            "warm_start_candles": False,
            "startup_state_entry": True,
            "entry_confirmation_samples": 1,
            "fast_impulse_window_seconds": 6.0,
            "confirm_impulse_window_seconds": 18.0,
            "breakout_lookback_seconds": 90.0,
            "volatility_lookback_seconds": 120.0,
            "flow_window_seconds": 12.0,
            "min_fast_exhaustion_bps": 4.0,
            "min_confirm_exhaustion_bps": 7.0,
            "fast_vol_multiplier": 0.4,
            "confirm_vol_multiplier": 0.5,
            "exhaustion_flow_min": 0.15,
            "exhaustion_book_min": 0.10,
            "reversal_flow_min": 0.03,
            "reversal_book_min": 0.02,
            "rebound_confirm_bps": 3.0,
            "reversal_window_seconds": 20.0,
            "min_trade_count": 1,
            "depth_levels": 2,
            "max_spread_bps": 10.0,
            "max_quote_age_ms": 5_000,
            "quote_gap_warn_ms": 10_000,
            "reconnect_gap_ms": 20_000,
            "setup_score_min": 45.0,
            "initial_stop_min_bps": 6.0,
            "initial_stop_vol_multiplier": 1.0,
            "stop_anchor_buffer_bps": 1.0,
            "take_profit_r": 1.0,
            "min_take_profit_bps": 4.0,
            "trail_min_bps": 5.0,
            "trail_vol_multiplier": 1.0,
            "trail_arm_r": 0.6,
            "break_even_lock_r": 0.8,
            "failure_hold_seconds": 8.0,
            "failure_min_followthrough_bps": 2.0,
            "retest_extreme_buffer_bps": 0.5,
            "adverse_flow_exit_imbalance": 0.08,
            "adverse_book_exit_imbalance": 0.05,
            "max_hold_seconds": 60.0,
            "stop_confirmation_ticks": 1,
            "slippage_bps": 0.0,
            "fee_bps": 0.0,
            "equity_risk_pct": 1.0,
            "cooldown_seconds": 0.0,
            "max_daily_loss": 30.0,
            "max_trades_per_day": 20,
            "max_trades_per_hour": 10,
            "min_hold_seconds": 0.0,
            "events_jsonl_path": str(root / "events.jsonl"),
            "samples_jsonl_path": str(root / "samples.jsonl"),
            "trades_csv_path": str(root / "trades.csv"),
            "markouts_jsonl_path": str(root / "markouts.jsonl"),
            "report_dir": str(root / "reports"),
        }
        params.update(overrides)
        return ProExhaustionReversalBot(**params)

    def _seed_prices(self, bot: ProExhaustionReversalBot, now_ms: int, slope: float) -> None:
        for i in range(120):
            bot._append_price_sample(now_ms - ((120 - i) * 1_000), 100.0 + (i * slope))

    def _make_snapshot(
        self,
        now_ms: int,
        *,
        bid: float,
        ask: float,
        mid: float,
        micro: float,
        bid_size: float,
        ask_size: float,
        trades: list[TradePrint],
        book: BookSnapshot,
    ) -> MarketSnapshot:
        quote = Quote(
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            mid=mid,
            microprice=micro,
            spread=ask - bid,
            spread_bps=((ask - bid) / mid) * 10_000.0,
            exchange_time_ms=now_ms,
            received_time_ms=now_ms + 120,
            transport_delay_ms=120,
        )
        return MarketSnapshot(quote=quote, book=book, trades=trades, seq=1)

    def _down_exhaustion_snapshot(self, now_ms: int) -> MarketSnapshot:
        bid = 99.72
        ask = 99.74
        mid = 99.73
        trades = [
            TradePrint(side="A", price=99.80, size=3.0, hash="a1", exchange_time_ms=now_ms - 9_000),
            TradePrint(side="A", price=99.76, size=4.0, hash="a2", exchange_time_ms=now_ms - 5_000),
            TradePrint(side="A", price=99.72, size=5.0, hash="a3", exchange_time_ms=now_ms - 1_000),
        ]
        book = BookSnapshot(
            bids=[(bid, 6.0), (bid - 0.01, 5.0), (bid - 0.02, 4.0)],
            asks=[(ask, 16.0), (ask + 0.01, 14.0), (ask + 0.02, 12.0)],
            exchange_time_ms=now_ms,
            received_time_ms=now_ms + 120,
        )
        return self._make_snapshot(
            now_ms,
            bid=bid,
            ask=ask,
            mid=mid,
            micro=99.725,
            bid_size=6.0,
            ask_size=16.0,
            trades=trades,
            book=book,
        )

    def _long_rebound_snapshot(self, now_ms: int) -> MarketSnapshot:
        bid = 99.80
        ask = 99.82
        mid = 99.81
        trades = [
            TradePrint(side="B", price=99.76, size=2.0, hash="b1", exchange_time_ms=now_ms - 3_000),
            TradePrint(side="B", price=99.79, size=3.0, hash="b2", exchange_time_ms=now_ms - 2_000),
            TradePrint(side="B", price=99.81, size=4.0, hash="b3", exchange_time_ms=now_ms - 1_000),
        ]
        book = BookSnapshot(
            bids=[(bid, 15.0), (bid - 0.01, 12.0), (bid - 0.02, 9.0)],
            asks=[(ask, 9.0), (ask + 0.01, 8.0), (ask + 0.02, 7.0)],
            exchange_time_ms=now_ms,
            received_time_ms=now_ms + 120,
        )
        return self._make_snapshot(
            now_ms,
            bid=bid,
            ask=ask,
            mid=mid,
            micro=99.815,
            bid_size=15.0,
            ask_size=9.0,
            trades=trades,
            book=book,
        )

    def _up_exhaustion_snapshot(self, now_ms: int) -> MarketSnapshot:
        bid = 100.26
        ask = 100.28
        mid = 100.27
        trades = [
            TradePrint(side="B", price=100.18, size=3.0, hash="u1", exchange_time_ms=now_ms - 9_000),
            TradePrint(side="B", price=100.23, size=4.0, hash="u2", exchange_time_ms=now_ms - 5_000),
            TradePrint(side="B", price=100.27, size=5.0, hash="u3", exchange_time_ms=now_ms - 1_000),
        ]
        book = BookSnapshot(
            bids=[(bid, 18.0), (bid - 0.01, 15.0), (bid - 0.02, 12.0)],
            asks=[(ask, 6.0), (ask + 0.01, 5.0), (ask + 0.02, 4.0)],
            exchange_time_ms=now_ms,
            received_time_ms=now_ms + 120,
        )
        return self._make_snapshot(
            now_ms,
            bid=bid,
            ask=ask,
            mid=mid,
            micro=100.275,
            bid_size=18.0,
            ask_size=6.0,
            trades=trades,
            book=book,
        )

    def _short_rebound_snapshot(self, now_ms: int) -> MarketSnapshot:
        bid = 100.18
        ask = 100.20
        mid = 100.19
        trades = [
            TradePrint(side="A", price=100.22, size=2.0, hash="s1", exchange_time_ms=now_ms - 3_000),
            TradePrint(side="A", price=100.20, size=3.0, hash="s2", exchange_time_ms=now_ms - 2_000),
            TradePrint(side="A", price=100.18, size=4.0, hash="s3", exchange_time_ms=now_ms - 1_000),
        ]
        book = BookSnapshot(
            bids=[(bid, 8.0), (bid - 0.01, 7.0), (bid - 0.02, 6.0)],
            asks=[(ask, 15.0), (ask + 0.01, 12.0), (ask + 0.02, 10.0)],
            exchange_time_ms=now_ms,
            received_time_ms=now_ms + 120,
        )
        return self._make_snapshot(
            now_ms,
            bid=bid,
            ask=ask,
            mid=mid,
            micro=100.185,
            bid_size=8.0,
            ask_size=15.0,
            trades=trades,
            book=book,
        )

    def _neutral_snapshot(self, now_ms: int, *, mid: float) -> MarketSnapshot:
        bid = mid - 0.01
        ask = mid + 0.01
        trades = [
            TradePrint(side="B", price=mid, size=1.0, hash="n1", exchange_time_ms=now_ms - 2_000),
            TradePrint(side="A", price=mid, size=1.0, hash="n2", exchange_time_ms=now_ms - 1_000),
        ]
        book = BookSnapshot(
            bids=[(bid, 10.0), (bid - 0.01, 8.0), (bid - 0.02, 6.0)],
            asks=[(ask, 10.0), (ask + 0.01, 8.0), (ask + 0.02, 6.0)],
            exchange_time_ms=now_ms,
            received_time_ms=now_ms + 120,
        )
        return self._make_snapshot(
            now_ms,
            bid=bid,
            ask=ask,
            mid=mid,
            micro=mid,
            bid_size=10.0,
            ask_size=10.0,
            trades=trades,
            book=book,
        )

    def _positive_snapshot(self, now_ms: int, *, mid: float) -> MarketSnapshot:
        bid = mid - 0.01
        ask = mid + 0.01
        trades = [
            TradePrint(side="B", price=mid - 0.01, size=2.0, hash="p1", exchange_time_ms=now_ms - 2_000),
            TradePrint(side="B", price=mid, size=3.0, hash="p2", exchange_time_ms=now_ms - 1_000),
        ]
        book = BookSnapshot(
            bids=[(bid, 14.0), (bid - 0.01, 12.0), (bid - 0.02, 10.0)],
            asks=[(ask, 8.0), (ask + 0.01, 7.0), (ask + 0.02, 6.0)],
            exchange_time_ms=now_ms,
            received_time_ms=now_ms + 120,
        )
        return self._make_snapshot(
            now_ms,
            bid=bid,
            ask=ask,
            mid=mid,
            micro=mid + 0.004,
            bid_size=14.0,
            ask_size=8.0,
            trades=trades,
            book=book,
        )


if __name__ == "__main__":
    unittest.main()

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

from pa_momo_mid_pro_core import MidFrequencyMomentumBot
from pa_pump_pro_core import BookSnapshot, MarketSnapshot, Quote, TradePrint


class MidMomentumCoreTests(unittest.TestCase):
    def test_signal_snapshot_confirms_long_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp))
            now_ms = int(time.time() * 1000)
            self._seed_prices(bot, now_ms, slope=0.005)
            signal = bot._build_signal_snapshot(self._long_snapshot(now_ms))
            self.assertIsNotNone(signal)
            assert signal is not None
            self.assertEqual(signal.regime, "active")
            self.assertTrue(signal.long_ready)
            self.assertFalse(signal.short_ready)
            self.assertGreater(signal.long_score, signal.short_score)

    def test_extreme_regime_blocks_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp), extreme_impulse_bps=10.0, extreme_vol_bps=8.0)
            now_ms = int(time.time() * 1000)
            self._seed_prices(bot, now_ms, slope=0.02)
            signal = bot._build_signal_snapshot(self._long_snapshot(now_ms, bid=101.55, ask=101.57, mid=101.56, micro=101.565))
            self.assertIsNotNone(signal)
            assert signal is not None
            self.assertEqual(signal.regime, "extreme")
            self.assertFalse(signal.long_ready)

    def test_flow_flip_exit_closes_trade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp))
            now_ms = int(time.time() * 1000)
            self._seed_prices(bot, now_ms, slope=0.005)
            entry_signal = bot._build_signal_snapshot(self._long_snapshot(now_ms))
            assert entry_signal is not None
            bot._open_position(entry_signal, "LONG", "test_long", "breakout_stack")
            assert bot.position is not None
            bot.position.opened_at = time.time() - 1.0

            reverse_signal = bot._build_signal_snapshot(
                self._short_snapshot(
                    now_ms + 5_000,
                    bid=100.37,
                    ask=100.39,
                    mid=100.38,
                    micro=100.376,
                    trades=[
                        TradePrint(side="A", price=100.38, size=4.0, hash="s1", exchange_time_ms=now_ms + 3_000),
                        TradePrint(side="A", price=100.37, size=5.0, hash="s2", exchange_time_ms=now_ms + 4_000),
                    ],
                    book=BookSnapshot(
                        bids=[(100.37, 6.0), (100.36, 5.0), (100.35, 4.0)],
                        asks=[(100.39, 18.0), (100.40, 15.0), (100.41, 12.0)],
                        exchange_time_ms=now_ms + 5_000,
                        received_time_ms=now_ms + 5_120,
                    ),
                )
            )
            assert reverse_signal is not None
            bot._maybe_close_position(reverse_signal)
            self.assertIsNone(bot.position)
            self.assertEqual(bot.exit_count, 1)
            self.assertEqual(bot.exit_reason_counts.get("flow_flip_exit"), 1)

    def test_breakout_failure_exit_closes_trade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp))
            now_ms = int(time.time() * 1000)
            self._seed_prices(bot, now_ms, slope=0.005)
            entry_signal = bot._build_signal_snapshot(self._long_snapshot(now_ms))
            assert entry_signal is not None
            bot._open_position(entry_signal, "LONG", "test_long", "breakout_stack")
            assert bot.position is not None
            bot.position.opened_at = time.time() - 4.0

            fail_level = float(bot.position.opened_features["breakout_high"])
            fail_mid = fail_level * (1.0 - 0.0003)
            fail_signal = bot._build_signal_snapshot(
                self._balanced_snapshot(
                    now_ms + 6_000,
                    bid=fail_mid - 0.01,
                    ask=fail_mid + 0.01,
                    mid=fail_mid,
                    micro=fail_mid,
                )
            )
            assert fail_signal is not None
            bot._maybe_close_position(fail_signal)
            self.assertIsNone(bot.position)
            self.assertEqual(bot.exit_reason_counts.get("breakout_failure"), 1)

    def test_time_stop_closes_lingering_trade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp), max_hold_seconds=5.0, failure_hold_seconds=30.0)
            now_ms = int(time.time() * 1000)
            self._seed_prices(bot, now_ms, slope=0.005)
            entry_signal = bot._build_signal_snapshot(self._long_snapshot(now_ms))
            assert entry_signal is not None
            bot._open_position(entry_signal, "LONG", "test_long", "breakout_stack")
            assert bot.position is not None
            bot.position.opened_at = time.time() - 10.0

            stale_signal = bot._build_signal_snapshot(
                self._balanced_snapshot(
                    now_ms + 7_000,
                    bid=100.64,
                    ask=100.66,
                    mid=100.65,
                    micro=100.651,
                )
            )
            assert stale_signal is not None
            bot._maybe_close_position(stale_signal)
            self.assertIsNone(bot.position)
            self.assertEqual(bot.exit_reason_counts.get("time_stop"), 1)

    def test_partial_tactical_profile_does_not_enter_without_full_breakout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp), entry_confirmation_samples=2, instant_entry_score_min=85.0)
            now_ms = int(time.time() * 1000)
            self._seed_prices(bot, now_ms, slope=0.005)
            signal = bot._build_signal_snapshot(self._long_snapshot(now_ms))
            assert signal is not None
            signal.long_ready = False
            signal.short_ready = False
            signal.long_entry_profile = None
            signal.short_entry_profile = None
            signal.long_score = 96.0
            signal.short_score = 18.0
            bot._maybe_enter(signal)
            self.assertIsNone(bot.position)

    def test_confirmed_runner_can_add_and_reduce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(
                Path(tmp),
                max_add_ons=2,
                add_on_trigger_r=0.4,
                add_on_score_min=80.0,
                add_on_breakout_extension_bps=0.0,
                add_on_flow_multiplier=1.0,
                add_on_book_multiplier=1.0,
                add_on_min_seconds=0.0,
                max_reductions=2,
                de_risk_score_threshold=90.0,
                de_risk_confirm_ratio=0.9,
                de_risk_flow_multiplier=1.2,
                initial_notional_fraction=0.25,
                max_notional_fraction=0.6,
            )
            now_ms = int(time.time() * 1000)
            self._seed_prices(bot, now_ms, slope=0.005)
            entry_signal = bot._build_signal_snapshot(self._long_snapshot(now_ms))
            assert entry_signal is not None
            bot._open_position(entry_signal, "LONG", "test_long", "breakout_stack")
            assert bot.position is not None
            base_size = bot.position.size
            bot.position.trailing_armed = True
            bot.position.best_exit_price_seen = entry_signal.bid * 1.002
            bot.position.opened_at = time.time() - 5.0

            add_signal = bot._build_signal_snapshot(
                self._long_snapshot(
                    now_ms + 5_000,
                    bid=100.90,
                    ask=100.92,
                    mid=100.91,
                    micro=100.915,
                )
            )
            assert add_signal is not None
            self.assertTrue(bot._add_on_ready(add_signal))
            self.assertTrue(bot._scale_in_position(add_signal))
            assert bot.position is not None
            self.assertGreater(bot.position.size, base_size)
            self.assertEqual(bot.position.add_on_count, 1)

            reduce_signal = bot._build_signal_snapshot(
                self._balanced_snapshot(
                    now_ms + 10_000,
                    bid=100.86,
                    ask=100.88,
                    mid=100.87,
                    micro=100.871,
                )
            )
            assert reduce_signal is not None
            bot.position.trailing_armed = True
            self.assertTrue(
                bot._decay_reduction_ready(
                    reduce_signal,
                    hold_seconds=6.0,
                    mfe_bps=bot.position.stop_distance_bps,
                )
            )
            size_before_reduce = bot.position.size
            self.assertTrue(bot._scale_out_position(reduce_signal, "momentum_decay"))
            self.assertLess(bot.position.size, size_before_reduce)
            self.assertEqual(bot.position.reduction_count, 1)

    def _make_bot(self, root: Path, **overrides: float) -> MidFrequencyMomentumBot:
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
            "fast_impulse_window_seconds": 8.0,
            "confirm_impulse_window_seconds": 20.0,
            "breakout_lookback_seconds": 90.0,
            "volatility_lookback_seconds": 120.0,
            "flow_window_seconds": 12.0,
            "min_fast_impulse_bps": 4.0,
            "min_confirm_impulse_bps": 6.0,
            "fast_vol_multiplier": 0.4,
            "confirm_vol_multiplier": 0.6,
            "breakout_buffer_bps": 0.5,
            "flow_imbalance_min": 0.08,
            "book_imbalance_min": 0.04,
            "min_trade_count": 1,
            "depth_levels": 2,
            "max_spread_bps": 10.0,
            "max_quote_age_ms": 5_000,
            "quote_gap_warn_ms": 10_000,
            "reconnect_gap_ms": 20_000,
            "extreme_impulse_bps": 40.0,
            "extreme_vol_bps": 40.0,
            "setup_score_min": 55.0,
            "tactical_score_min": 72.0,
            "tactical_fast_threshold_ratio": 0.55,
            "tactical_confirm_threshold_ratio": 0.65,
            "tactical_breakout_slack_bps": 0.4,
            "tactical_flow_multiplier": 0.85,
            "tactical_book_multiplier": 0.85,
            "score_edge_min": 10.0,
            "instant_entry_score_min": 90.0,
            "initial_stop_min_bps": 8.0,
            "initial_stop_vol_multiplier": 1.0,
            "trail_min_bps": 6.0,
            "trail_vol_multiplier": 1.0,
            "trail_arm_r": 0.5,
            "break_even_lock_r": 0.75,
            "score_notional_boost": 0.35,
            "initial_notional_fraction": 0.30,
            "max_notional_fraction": 0.7,
            "max_add_ons": 2,
            "add_on_trigger_r": 0.6,
            "add_on_fraction": 0.15,
            "add_on_score_min": 82.0,
            "add_on_breakout_extension_bps": 0.75,
            "add_on_flow_multiplier": 1.15,
            "add_on_book_multiplier": 1.1,
            "add_on_min_seconds": 2.0,
            "max_reductions": 2,
            "de_risk_fraction": 0.35,
            "de_risk_score_threshold": 68.0,
            "de_risk_confirm_ratio": 0.55,
            "de_risk_flow_multiplier": 0.75,
            "runner_core_fraction": 0.45,
            "failure_hold_seconds": 8.0,
            "failure_min_followthrough_bps": 5.0,
            "flow_flip_exit_imbalance": 0.10,
            "book_flip_exit_imbalance": 0.05,
            "breakout_fail_buffer_bps": 1.0,
            "max_hold_seconds": 60.0,
            "time_stop_min_r": 0.4,
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
        return MidFrequencyMomentumBot(**params)

    def _seed_prices(self, bot: MidFrequencyMomentumBot, now_ms: int, slope: float) -> None:
        for i in range(120):
            bot._append_price_sample(now_ms - ((120 - i) * 1_000), 100.0 + (i * slope))

    def _long_snapshot(
        self,
        now_ms: int,
        *,
        bid: float = 100.62,
        ask: float = 100.64,
        mid: float = 100.63,
        micro: float = 100.633,
    ) -> MarketSnapshot:
        trades = [
            TradePrint(side="B", price=100.57, size=3.0, hash="b1", exchange_time_ms=now_ms - 10_000),
            TradePrint(side="B", price=100.60, size=3.0, hash="b2", exchange_time_ms=now_ms - 8_000),
            TradePrint(side="B", price=100.62, size=4.0, hash="b3", exchange_time_ms=now_ms - 4_000),
            TradePrint(side="A", price=100.61, size=1.0, hash="a1", exchange_time_ms=now_ms - 6_000),
        ]
        book = BookSnapshot(
            bids=[(bid, 22.0), (bid - 0.01, 17.0), (bid - 0.02, 12.0)],
            asks=[(ask, 8.0), (ask + 0.01, 7.0), (ask + 0.02, 6.0)],
            exchange_time_ms=now_ms,
            received_time_ms=now_ms + 120,
        )
        quote = Quote(
            bid=bid,
            ask=ask,
            bid_size=22.0,
            ask_size=8.0,
            mid=mid,
            microprice=micro,
            spread=ask - bid,
            spread_bps=((ask - bid) / mid) * 10_000.0,
            exchange_time_ms=now_ms,
            received_time_ms=now_ms + 120,
            transport_delay_ms=120,
        )
        return MarketSnapshot(quote=quote, book=book, trades=trades, seq=1)

    def _short_snapshot(
        self,
        now_ms: int,
        *,
        bid: float,
        ask: float,
        mid: float,
        micro: float,
        trades: list[TradePrint],
        book: BookSnapshot,
    ) -> MarketSnapshot:
        quote = Quote(
            bid=bid,
            ask=ask,
            bid_size=book.bids[0][1],
            ask_size=book.asks[0][1],
            mid=mid,
            microprice=micro,
            spread=ask - bid,
            spread_bps=((ask - bid) / mid) * 10_000.0,
            exchange_time_ms=now_ms,
            received_time_ms=now_ms + 120,
            transport_delay_ms=120,
        )
        return MarketSnapshot(quote=quote, book=book, trades=trades, seq=2)

    def _balanced_snapshot(
        self,
        now_ms: int,
        *,
        bid: float,
        ask: float,
        mid: float,
        micro: float,
    ) -> MarketSnapshot:
        trades = [
            TradePrint(side="B", price=mid, size=1.0, hash="m1", exchange_time_ms=now_ms - 2_000),
            TradePrint(side="A", price=mid, size=1.0, hash="m2", exchange_time_ms=now_ms - 1_000),
        ]
        book = BookSnapshot(
            bids=[(bid, 12.0), (bid - 0.01, 10.0), (bid - 0.02, 8.0)],
            asks=[(ask, 11.0), (ask + 0.01, 9.0), (ask + 0.02, 8.0)],
            exchange_time_ms=now_ms,
            received_time_ms=now_ms + 100,
        )
        quote = Quote(
            bid=bid,
            ask=ask,
            bid_size=12.0,
            ask_size=11.0,
            mid=mid,
            microprice=micro,
            spread=ask - bid,
            spread_bps=((ask - bid) / mid) * 10_000.0,
            exchange_time_ms=now_ms,
            received_time_ms=now_ms + 100,
            transport_delay_ms=100,
        )
        return MarketSnapshot(quote=quote, book=book, trades=trades, seq=3)


if __name__ == "__main__":
    unittest.main()

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

from initiator_follower_jg_thesis_core import InitiatorFollowerJGThesisBot
from pa_pump_pro_core import BookSnapshot, MarketSnapshot, Quote, TradePrint


class InitiatorFollowerJGThesisCoreTests(unittest.TestCase):
    def test_tension_probe_signal_arms_before_full_breakout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp))
            now_ms = int(time.time() * 1000)
            self._seed_probe_prices(bot, now_ms)
            signal = bot._build_signal_snapshot(self._probe_long_snapshot(now_ms))
            self.assertIsNotNone(signal)
            assert signal is not None
            self.assertEqual(signal.regime, "tension")
            self.assertEqual(signal.long_entry_profile, "probe")
            self.assertFalse(signal.long_ready)

    def test_campaign_signal_promotes_real_break(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp))
            now_ms = int(time.time() * 1000)
            self._seed_prices(bot, now_ms, slope=0.0045)
            signal = bot._build_signal_snapshot(self._campaign_long_snapshot(now_ms))
            self.assertIsNotNone(signal)
            assert signal is not None
            self.assertEqual(signal.regime, "campaign")
            self.assertEqual(signal.long_entry_profile, "campaign")
            self.assertTrue(signal.long_ready)

    def test_probe_losses_throttle_same_side_reentry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp), max_probe_losses_per_side=2, probe_loss_window_seconds=600.0)
            now_ms = int(time.time() * 1000)
            self._seed_prices(bot, now_ms, slope=0.0023)
            signal = bot._build_signal_snapshot(self._probe_long_snapshot(now_ms))
            assert signal is not None

            for idx in range(2):
                bot._open_position(signal, "LONG", f"probe_{idx}", "probe")
                assert bot.position is not None
                bot.position.opened_at = time.time() - 10.0
                fail_signal = bot._build_signal_snapshot(self._balanced_snapshot(now_ms + ((idx + 1) * 5_000), mid=100.48))
                assert fail_signal is not None
                bot._close_position(fail_signal, "failed_followthrough", fail_signal.bid, 0.5, -2.0)

            blocked_signal = bot._build_signal_snapshot(self._probe_long_snapshot(now_ms + 15_000))
            assert blocked_signal is not None
            self.assertFalse(bot._probe_allowed("LONG"))
            self.assertIsNone(
                bot._entry_profile(
                    side="LONG",
                    score=blocked_signal.long_score,
                    opposing_score=blocked_signal.short_score,
                    fast_impulse_bps=blocked_signal.fast_impulse_bps,
                    confirm_impulse_bps=blocked_signal.confirm_impulse_bps,
                    fast_threshold_bps=blocked_signal.dynamic_fast_threshold_bps,
                    confirm_threshold_bps=blocked_signal.dynamic_confirm_threshold_bps,
                    breakout_distance_bps=blocked_signal.breakout_distance_long_bps,
                    flow_imbalance=blocked_signal.flow_imbalance,
                    book_imbalance=blocked_signal.book_imbalance,
                    trade_count_ok=blocked_signal.long_trade_count_ok,
                    spread_ok=blocked_signal.spread_ok,
                    extreme_blocked=False,
                    full_ready=blocked_signal.long_ready,
                )
            )

    def test_campaign_entry_can_size_larger_than_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp))
            now_ms = int(time.time() * 1000)
            self._seed_prices(bot, now_ms, slope=0.0045)
            probe_signal = bot._build_signal_snapshot(self._probe_long_snapshot(now_ms))
            campaign_signal = bot._build_signal_snapshot(self._campaign_long_snapshot(now_ms + 5_000))
            assert probe_signal is not None
            assert campaign_signal is not None

            bot._open_position(probe_signal, "LONG", "test_probe", "probe")
            assert bot.position is not None
            probe_notional = bot.position.notional
            bot.position = None

            bot._open_position(campaign_signal, "LONG", "test_campaign", "campaign")
            assert bot.position is not None
            campaign_notional = bot.position.notional
            self.assertGreater(campaign_notional, probe_notional)

    def test_global_probe_losses_throttle_both_sides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp), max_probe_losses_per_side=10, max_global_probe_losses=2, probe_loss_window_seconds=600.0)
            now_ms = int(time.time() * 1000)
            self._seed_prices(bot, now_ms, slope=0.0023)
            signal = bot._build_signal_snapshot(self._probe_long_snapshot(now_ms))
            assert signal is not None

            bot._open_position(signal, "LONG", "probe_long", "probe")
            assert bot.position is not None
            bot._close_position(signal, "failed_followthrough", signal.bid, 0.5, -2.0)

            bot._open_position(signal, "SHORT", "probe_short", "probe")
            assert bot.position is not None
            bot._close_position(signal, "failed_followthrough", signal.ask, 0.5, -2.0)

            self.assertFalse(bot._global_probe_allowed())

    def test_tension_regime_can_open_probe_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp), entry_confirmation_samples=1, startup_state_entry=True)
            now_ms = int(time.time() * 1000)
            self._seed_probe_prices(bot, now_ms)
            signal = bot._build_signal_snapshot(self._probe_long_snapshot(now_ms))
            assert signal is not None

            bot._maybe_enter(signal)

            self.assertIsNotNone(bot.position)
            assert bot.position is not None
            self.assertEqual(bot.position.side, "LONG")
            self.assertEqual(bot.position.entry_profile, "probe")

    def test_winning_probe_promotes_to_campaign_management(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp), entry_confirmation_samples=1, startup_state_entry=True)
            now_ms = int(time.time() * 1000)
            self._seed_probe_prices(bot, now_ms)
            probe_signal = bot._build_signal_snapshot(self._probe_long_snapshot(now_ms))
            assert probe_signal is not None

            bot._maybe_enter(probe_signal)
            assert bot.position is not None
            self.assertEqual(bot.position.entry_profile, "probe")

            self._seed_prices(bot, now_ms + 5_000, slope=0.0045)
            campaign_signal = bot._build_signal_snapshot(self._campaign_long_snapshot(now_ms + 5_000))
            assert campaign_signal is not None

            bot._maybe_close_position(campaign_signal)

            self.assertIsNotNone(bot.position)
            assert bot.position is not None
            self.assertEqual(bot.position.entry_profile, "campaign")

    def test_liquidity_cap_reduces_campaign_entry_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp), campaign_depth_share_limit=0.75)
            now_ms = int(time.time() * 1000)
            self._seed_prices(bot, now_ms, slope=0.0045)
            signal = bot._build_signal_snapshot(self._campaign_long_snapshot(now_ms))
            assert signal is not None

            bot._open_position(signal, "LONG", "thin_book_campaign", "campaign")

            assert bot.position is not None
            near_depth_notional = signal.ask_depth * signal.mid
            self.assertLessEqual(bot.position.notional, near_depth_notional * 0.75 + 1e-9)

    def test_hard_reversal_exit_cuts_bad_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp), entry_confirmation_samples=1, startup_state_entry=True, stop_confirmation_ticks=1)
            now_ms = int(time.time() * 1000)
            self._seed_probe_prices(bot, now_ms)
            probe_signal = bot._build_signal_snapshot(self._probe_long_snapshot(now_ms))
            assert probe_signal is not None

            bot._maybe_enter(probe_signal)
            assert bot.position is not None

            reversal_signal = bot._build_signal_snapshot(self._hard_reversal_long_snapshot(now_ms + 2_000))
            assert reversal_signal is not None
            bot._maybe_close_position(reversal_signal)

            self.assertIsNone(bot.position)

    def test_campaign_cap_stays_staged_until_lock_then_unlocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp), campaign_pre_unlock_fraction=0.72)
            now_ms = int(time.time() * 1000)
            self._seed_prices(bot, now_ms, slope=0.0045)
            signal = bot._build_signal_snapshot(self._campaign_long_snapshot(now_ms))
            assert signal is not None

            bot._open_position(signal, "LONG", "campaign_entry", "campaign")
            assert bot.position is not None
            self.assertAlmostEqual(bot._total_notional_cap(), bot.max_notional * 0.72)

            bot.position.add_on_count = 1
            bot.position.trailing_armed = True
            bot.position.locked_stop_price = bot.position.entry_price
            self.assertAlmostEqual(bot._total_notional_cap(), bot.max_notional * bot.max_notional_fraction)

    def _make_bot(self, root: Path, **overrides: float) -> InitiatorFollowerJGThesisBot:
        params = {
            "asset": "xyz:BRENTOIL",
            "account_balance": 2500.0,
            "api_url": "https://api.hyperliquid.xyz",
            "dex": "xyz",
            "leverage": 20,
            "sample_ms": 80,
            "warm_start_candles": False,
            "startup_state_entry": False,
            "entry_confirmation_samples": 2,
            "fast_impulse_window_seconds": 3.5,
            "confirm_impulse_window_seconds": 10.0,
            "breakout_lookback_seconds": 75.0,
            "volatility_lookback_seconds": 150.0,
            "flow_window_seconds": 4.5,
            "min_fast_impulse_bps": 5.5,
            "min_confirm_impulse_bps": 11.0,
            "fast_vol_multiplier": 0.7,
            "confirm_vol_multiplier": 0.95,
            "breakout_buffer_bps": 0.35,
            "flow_imbalance_min": 0.12,
            "book_imbalance_min": 0.06,
            "min_trade_count": 4,
            "depth_levels": 3,
            "max_spread_bps": 5.0,
            "max_quote_age_ms": 2_000,
            "quote_gap_warn_ms": 5_000,
            "reconnect_gap_ms": 10_000,
            "extreme_impulse_bps": 9_999.0,
            "extreme_vol_bps": 9_999.0,
            "setup_score_min": 60.0,
            "tactical_score_min": 58.0,
            "tactical_fast_threshold_ratio": 0.72,
            "tactical_confirm_threshold_ratio": 0.8,
            "tactical_breakout_slack_bps": 0.12,
            "tactical_flow_multiplier": 1.0,
            "tactical_book_multiplier": 1.0,
            "score_edge_min": 10.0,
            "instant_entry_score_min": 98.0,
            "initial_stop_min_bps": 9.0,
            "initial_stop_vol_multiplier": 1.05,
            "trail_min_bps": 14.0,
            "trail_vol_multiplier": 1.95,
            "trail_arm_r": 0.95,
            "break_even_lock_r": 1.15,
            "score_notional_boost": 0.55,
            "initial_notional_fraction": 0.10,
            "max_notional_fraction": 0.90,
            "max_add_ons": 3,
            "add_on_trigger_r": 0.55,
            "add_on_fraction": 0.15,
            "add_on_score_min": 78.0,
            "add_on_breakout_extension_bps": 0.45,
            "add_on_flow_multiplier": 1.0,
            "add_on_book_multiplier": 1.0,
            "add_on_min_seconds": 0.0,
            "max_reductions": 4,
            "de_risk_fraction": 0.22,
            "de_risk_score_threshold": 68.0,
            "de_risk_confirm_ratio": 0.60,
            "de_risk_flow_multiplier": 0.80,
            "runner_core_fraction": 0.38,
            "failure_hold_seconds": 14.0,
            "failure_min_followthrough_bps": 10.0,
            "flow_flip_exit_imbalance": 0.18,
            "book_flip_exit_imbalance": 0.10,
            "breakout_fail_buffer_bps": 0.75,
            "max_hold_seconds": 7_200.0,
            "time_stop_min_r": 0.20,
            "stop_confirmation_ticks": 1,
            "slippage_bps": 1.0,
            "fee_bps": 0.86,
            "equity_risk_pct": 1.0,
            "cooldown_seconds": 0.0,
            "max_daily_loss": 125.0,
            "max_trades_per_day": 60,
            "max_trades_per_hour": 20,
            "min_hold_seconds": 0.0,
            "events_jsonl_path": str(root / "events.jsonl"),
            "samples_jsonl_path": str(root / "samples.jsonl"),
            "trades_csv_path": str(root / "trades.csv"),
            "markouts_jsonl_path": str(root / "markouts.jsonl"),
            "report_dir": str(root / "reports"),
            "probe_score_min": 58.0,
            "probe_score_edge_min": 10.0,
            "probe_fast_threshold_ratio": 0.72,
            "probe_confirm_threshold_ratio": 0.80,
            "probe_breakout_slack_bps": 0.12,
            "probe_flow_multiplier": 1.0,
            "probe_book_multiplier": 1.0,
            "campaign_score_min": 78.0,
            "campaign_score_edge_min": 18.0,
            "event_impulse_bps": 18.0,
            "event_vol_bps": 15.0,
            "loss_risk_spread_ratio": 1.10,
            "loss_risk_quote_age_ratio": 0.85,
            "min_trade_rate_per_second": 0.01,
            "probe_notional_fraction": 0.10,
            "campaign_entry_notional_fraction": 0.45,
            "probe_size_boost": 0.04,
            "campaign_size_boost": 0.35,
            "campaign_add_on_trigger_r": 0.55,
            "probe_failure_hold_seconds": 4.0,
            "probe_failure_min_followthrough_bps": 1.8,
            "campaign_failure_hold_seconds": 14.0,
            "campaign_failure_min_followthrough_bps": 10.0,
            "max_probe_losses_per_side": 3,
            "max_global_probe_losses": 5,
            "probe_loss_window_seconds": 900.0,
            "probe_depth_share_limit": 0.35,
            "campaign_depth_share_limit": 0.85,
            "add_on_depth_share_limit": 0.60,
            "campaign_pre_unlock_fraction": 0.72,
            "de_risk_book_multiplier": 0.70,
            "de_risk_breakout_reclaim_bps": 0.08,
            "campaign_re_add_unlock_r": 1.15,
            "hard_reversal_confirm_ratio": 0.85,
            "hard_reversal_flow_multiplier": 1.25,
            "hard_reversal_book_multiplier": 1.0,
            "hard_reversal_max_profit_r": 0.50,
        }
        params.update(overrides)
        return InitiatorFollowerJGThesisBot(**params)

    def _seed_prices(self, bot: InitiatorFollowerJGThesisBot, now_ms: int, *, slope: float) -> None:
        for idx in range(240):
            ts = now_ms - ((240 - idx) * 1_000)
            price = 100.0 + (idx * slope)
            bot._append_price_sample(ts, price)

    def _seed_probe_prices(self, bot: InitiatorFollowerJGThesisBot, now_ms: int) -> None:
        for idx in range(240):
            ts = now_ms - ((240 - idx) * 1_000)
            if idx < 180:
                price = 100.0 + (idx * 0.0014)
            elif idx < 225:
                price = 100.34 + ((idx - 180) * 0.0060)
            elif idx < 228:
                price = 100.61 - ((228 - idx) * 0.0010)
            elif idx < 236:
                price = 100.545 + ((idx - 228) * 0.0008)
            else:
                price = 100.555 + ((idx - 236) * 0.0105)
            bot._append_price_sample(ts, price)

    def _probe_long_snapshot(self, now_ms: int) -> MarketSnapshot:
        return self._snapshot(
            now_ms,
            bid=100.629,
            ask=100.637,
            mid=100.633,
            micro=100.6355,
            trades=[
                TradePrint(side="B", price=100.606, size=2.4, hash="p1", exchange_time_ms=now_ms - 3_800),
                TradePrint(side="B", price=100.614, size=2.7, hash="p2", exchange_time_ms=now_ms - 2_400),
                TradePrint(side="B", price=100.622, size=3.1, hash="p3", exchange_time_ms=now_ms - 1_300),
                TradePrint(side="B", price=100.636, size=3.4, hash="p4", exchange_time_ms=now_ms - 350),
            ],
            bids=[(100.629, 28.0), (100.628, 26.0), (100.627, 24.0)],
            asks=[(100.637, 20.0), (100.638, 18.0), (100.639, 16.0)],
        )

    def _campaign_long_snapshot(self, now_ms: int) -> MarketSnapshot:
        return self._snapshot(
            now_ms,
            bid=101.30,
            ask=101.34,
            mid=101.32,
            micro=101.332,
            trades=[
                TradePrint(side="B", price=101.10, size=5.0, hash="c1", exchange_time_ms=now_ms - 4_000),
                TradePrint(side="B", price=101.18, size=6.0, hash="c2", exchange_time_ms=now_ms - 2_700),
                TradePrint(side="B", price=101.27, size=7.5, hash="c3", exchange_time_ms=now_ms - 1_300),
                TradePrint(side="B", price=101.33, size=8.2, hash="c4", exchange_time_ms=now_ms - 250),
            ],
            bids=[(101.30, 50.0), (101.29, 48.0), (101.28, 45.0)],
            asks=[(101.34, 40.0), (101.35, 38.0), (101.36, 35.0)],
        )

    def _hard_reversal_long_snapshot(self, now_ms: int) -> MarketSnapshot:
        return self._snapshot(
            now_ms,
            bid=100.53,
            ask=100.56,
            mid=100.545,
            micro=100.538,
            trades=[
                TradePrint(side="A", price=100.59, size=3.2, hash="r1", exchange_time_ms=now_ms - 1_500),
                TradePrint(side="A", price=100.56, size=4.0, hash="r2", exchange_time_ms=now_ms - 900),
                TradePrint(side="A", price=100.54, size=5.4, hash="r3", exchange_time_ms=now_ms - 250),
            ],
            bids=[(100.53, 9.0), (100.52, 8.0), (100.51, 7.0)],
            asks=[(100.56, 24.0), (100.57, 22.0), (100.58, 20.0)],
        )

    def _balanced_snapshot(self, now_ms: int, *, mid: float) -> MarketSnapshot:
        return self._snapshot(
            now_ms,
            bid=mid - 0.01,
            ask=mid + 0.01,
            mid=mid,
            micro=mid,
            trades=[
                TradePrint(side="B", price=mid, size=1.2, hash="b1", exchange_time_ms=now_ms - 1_200),
                TradePrint(side="A", price=mid, size=1.1, hash="b2", exchange_time_ms=now_ms - 400),
            ],
            bids=[(mid - 0.01, 10.0), (mid - 0.02, 9.0), (mid - 0.03, 8.0)],
            asks=[(mid + 0.01, 10.0), (mid + 0.02, 9.0), (mid + 0.03, 8.0)],
        )

    def _snapshot(
        self,
        now_ms: int,
        *,
        bid: float,
        ask: float,
        mid: float,
        micro: float,
        trades: list[TradePrint],
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
    ) -> MarketSnapshot:
        quote = Quote(
            bid=bid,
            ask=ask,
            bid_size=bids[0][1],
            ask_size=asks[0][1],
            mid=mid,
            microprice=micro,
            spread=ask - bid,
            spread_bps=((ask - bid) / max(mid, 1e-9)) * 10_000.0,
            exchange_time_ms=now_ms,
            received_time_ms=now_ms + 120,
            transport_delay_ms=120,
        )
        book = BookSnapshot(
            bids=bids,
            asks=asks,
            exchange_time_ms=now_ms,
            received_time_ms=now_ms + 125,
        )
        return MarketSnapshot(quote=quote, book=book, trades=trades, seq=1)


if __name__ == "__main__":
    unittest.main()

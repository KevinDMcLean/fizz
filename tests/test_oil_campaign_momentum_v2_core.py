from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from oil_campaign_momentum_v2_core import OilCampaignMomentumV2Bot
from pa_pump_pro_core import BookSnapshot, MarketSnapshot, Quote, TradePrint


class OilCampaignMomentumV2CoreTests(unittest.TestCase):
    def test_tick_floor_blocks_sub_tick_breakout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp), initiative_persistence_windows=1)
            now_ms = int(time.time() * 1000)
            self._seed_linear_prices(bot, now_ms, slope=0.001)
            signal = bot._build_signal_snapshot(
                self._snapshot(
                    now_ms,
                    bid=100.2435,
                    ask=100.2455,
                    mid=100.2445,
                    micro=100.2450,
                    trades=[
                        TradePrint(side="B", price=100.2400, size=1.4, hash="t1", exchange_time_ms=now_ms - 1800),
                        TradePrint(side="B", price=100.2410, size=1.2, hash="t2", exchange_time_ms=now_ms - 1200),
                        TradePrint(side="B", price=100.2430, size=1.8, hash="t3", exchange_time_ms=now_ms - 600),
                        TradePrint(side="B", price=100.2455, size=2.2, hash="t4", exchange_time_ms=now_ms - 100),
                    ],
                    bids=[(100.2435, 18.0), (100.2335, 16.0), (100.2235, 14.0)],
                    asks=[(100.2455, 9.0), (100.2555, 8.0), (100.2655, 7.0)],
                )
            )
            self.assertIsNotNone(signal)
            assert signal is not None
            self.assertFalse(signal.long_breakout_ok)

    def test_reclaim_veto_closes_probe_and_starts_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp), entry_confirmation_samples=1, startup_state_entry=True, initiative_persistence_windows=1)
            now_ms = int(time.time() * 1000)
            self._seed_probe_prices(bot, now_ms)
            probe_signal = bot._build_signal_snapshot(self._probe_long_snapshot(now_ms))
            self.assertIsNotNone(probe_signal)
            assert probe_signal is not None

            bot._maybe_enter(probe_signal)
            self.assertIsNotNone(bot.position)
            assert bot.position is not None
            bot.position.opened_at = time.time() - 2.0

            reclaim_signal = bot._build_signal_snapshot(self._balanced_snapshot(now_ms + 2_000, mid=100.48))
            self.assertIsNotNone(reclaim_signal)
            assert reclaim_signal is not None

            bot._maybe_close_position(reclaim_signal)

            self.assertIsNone(bot.position)
            self.assertIsNotNone(bot.last_break_reclaim_exit_ts)

    def test_runner_state_label_after_unlock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp), initiative_persistence_windows=1)
            now_ms = int(time.time() * 1000)
            self._seed_campaign_prices(bot, now_ms)
            signal = bot._build_signal_snapshot(self._campaign_long_snapshot(now_ms))
            self.assertIsNotNone(signal)
            assert signal is not None

            bot._open_position(signal, "LONG", "campaign_entry", "campaign")
            assert bot.position is not None
            bot.position.add_on_count = 1
            bot.position.trailing_armed = True
            bot.position.locked_stop_price = bot.position.entry_price

            self.assertEqual(bot._state_label(signal), "runner")

    def test_features_log_writes_candidate_and_label_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bot = self._make_bot(
                root,
                entry_confirmation_samples=1,
                startup_state_entry=True,
                initiative_persistence_windows=1,
                features_jsonl_path=str(root / "features.jsonl"),
            )
            now_ms = int(time.time() * 1000)
            self._seed_probe_prices(bot, now_ms)
            probe_signal = bot._build_signal_snapshot(self._probe_long_snapshot(now_ms))
            assert probe_signal is not None
            bot._maybe_enter(probe_signal)
            assert bot.position is not None
            bot.position.opened_at = time.time() - 2.0

            reclaim_signal = bot._build_signal_snapshot(self._balanced_snapshot(now_ms + 2_000, mid=100.48))
            assert reclaim_signal is not None
            bot._maybe_close_position(reclaim_signal)

            rows = [
                json.loads(line)
                for line in (root / "features.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertTrue(any(row.get("row_type") == "candidate" for row in rows))
            self.assertTrue(any(row.get("row_type") == "label" for row in rows))

    def test_probe_entry_uses_tick_aware_breakout_slack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp), initiative_persistence_windows=1)
            raw_profile = bot._entry_profile(
                side="LONG",
                score=72.0,
                opposing_score=40.0,
                fast_impulse_bps=6.2,
                confirm_impulse_bps=11.8,
                fast_threshold_bps=5.5,
                confirm_threshold_bps=11.0,
                breakout_distance_bps=-0.50,
                flow_imbalance=0.45,
                book_imbalance=0.30,
                trade_count_ok=True,
                spread_ok=True,
                extreme_blocked=False,
                full_ready=False,
                probe_depth_confirmed=True,
                probe_initiative_confirmed=True,
            )
            tick_aware_profile = bot._entry_profile(
                side="LONG",
                score=72.0,
                opposing_score=40.0,
                fast_impulse_bps=6.2,
                confirm_impulse_bps=11.8,
                fast_threshold_bps=5.5,
                confirm_threshold_bps=11.0,
                breakout_distance_bps=-0.50,
                flow_imbalance=0.45,
                book_imbalance=0.30,
                trade_count_ok=True,
                spread_ok=True,
                extreme_blocked=False,
                full_ready=False,
                reference_price=100.0,
                probe_depth_confirmed=True,
                probe_initiative_confirmed=True,
            )

            self.assertIsNone(raw_profile)
            self.assertEqual(tick_aware_profile, "probe")

    def test_probe_slack_requires_both_confirmation_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp), initiative_persistence_windows=1)
            profile = bot._entry_profile(
                side="LONG",
                score=86.0,
                opposing_score=40.0,
                fast_impulse_bps=6.8,
                confirm_impulse_bps=11.5,
                fast_threshold_bps=5.5,
                confirm_threshold_bps=11.0,
                breakout_distance_bps=-0.40,
                flow_imbalance=0.55,
                book_imbalance=0.30,
                trade_count_ok=True,
                spread_ok=True,
                extreme_blocked=False,
                full_ready=False,
                reference_price=100.0,
                probe_depth_confirmed=False,
                probe_initiative_confirmed=True,
            )

            self.assertIsNone(profile)

    def _make_bot(self, root: Path, **overrides: object) -> OilCampaignMomentumV2Bot:
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
            "breakout_buffer_ticks": 2.0,
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
            "break_even_lock_r": 1.10,
            "score_notional_boost": 0.55,
            "initial_notional_fraction": 0.08,
            "max_notional_fraction": 0.90,
            "max_add_ons": 3,
            "add_on_trigger_r": 0.45,
            "add_on_fraction": 0.16,
            "add_on_score_min": 80.0,
            "add_on_breakout_extension_bps": 0.45,
            "add_on_extension_ticks": 2.0,
            "add_on_flow_multiplier": 1.0,
            "add_on_book_multiplier": 1.0,
            "add_on_min_seconds": 0.0,
            "max_reductions": 4,
            "de_risk_fraction": 0.22,
            "de_risk_score_threshold": 70.0,
            "de_risk_confirm_ratio": 0.62,
            "de_risk_flow_multiplier": 0.82,
            "runner_core_fraction": 0.45,
            "failure_hold_seconds": 14.0,
            "failure_min_followthrough_bps": 10.0,
            "flow_flip_exit_imbalance": 0.18,
            "book_flip_exit_imbalance": 0.10,
            "breakout_fail_buffer_bps": 0.75,
            "failed_breakout_ticks": 3.0,
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
            "probe_breakout_slack_ticks": 1.0,
            "probe_flow_multiplier": 1.0,
            "probe_book_multiplier": 1.0,
            "campaign_score_min": 80.0,
            "campaign_score_edge_min": 18.0,
            "event_impulse_bps": 18.0,
            "event_vol_bps": 15.0,
            "loss_risk_spread_ratio": 1.10,
            "loss_risk_quote_age_ratio": 0.85,
            "min_trade_rate_per_second": 0.01,
            "probe_notional_fraction": 0.08,
            "campaign_entry_notional_fraction": 0.35,
            "probe_size_boost": 0.03,
            "campaign_size_boost": 0.35,
            "campaign_add_on_trigger_r": 0.45,
            "probe_failure_hold_seconds": 3.5,
            "probe_failure_min_followthrough_bps": 2.2,
            "campaign_failure_hold_seconds": 12.0,
            "campaign_failure_min_followthrough_bps": 10.0,
            "max_probe_losses_per_side": 3,
            "max_global_probe_losses": 5,
            "probe_loss_window_seconds": 900.0,
            "probe_depth_share_limit": 0.35,
            "campaign_depth_share_limit": 0.85,
            "add_on_depth_share_limit": 0.60,
            "campaign_pre_unlock_fraction": 0.70,
            "de_risk_book_multiplier": 0.70,
            "de_risk_breakout_reclaim_bps": 0.08,
            "breakout_reclaim_ticks": 2.0,
            "campaign_re_add_unlock_r": 1.10,
            "hard_reversal_confirm_ratio": 0.85,
            "hard_reversal_flow_multiplier": 1.25,
            "hard_reversal_book_multiplier": 1.0,
            "hard_reversal_max_profit_r": 0.50,
            "tick_size": 0.01,
            "depth_vacuum_near_ratio_max": 0.72,
            "initiative_persistence_windows": 3,
            "reclaim_veto_window_seconds": 8.0,
            "reclaim_cooldown_seconds": 90.0,
            "probe_promotion_mfe_r": 0.12,
            "features_jsonl_path": None,
        }
        params.update(overrides)
        return OilCampaignMomentumV2Bot(**params)

    def _seed_linear_prices(self, bot: OilCampaignMomentumV2Bot, now_ms: int, *, slope: float) -> None:
        for idx in range(240):
            ts = now_ms - ((240 - idx) * 1_000)
            bot._append_price_sample(ts, 100.0 + (idx * slope))

    def _seed_campaign_prices(self, bot: OilCampaignMomentumV2Bot, now_ms: int) -> None:
        for idx in range(240):
            ts = now_ms - ((240 - idx) * 1_000)
            bot._append_price_sample(ts, 100.0 + (idx * 0.0045))

    def _seed_probe_prices(self, bot: OilCampaignMomentumV2Bot, now_ms: int) -> None:
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

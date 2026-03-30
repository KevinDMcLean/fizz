from __future__ import annotations

import sys
import time
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from atlas_mm_feeaware_core import MarketFeatures
from kevin_hype_liquidity_core import KevinHypeConfig, KevinHypeLiquidityEngine, build_kevin_quote_plan, enforce_kevin_entry_guards, zero_fee_state
from pa_pump_pro_core import BookSnapshot


class KevinHypeLiquidityCoreTests(unittest.TestCase):
    def test_zero_fee_state_has_zero_rates(self) -> None:
        state = zero_fee_state(turnover=12_500.0, started_at_ts=time.time() - 30.0)
        self.assertEqual(state.maker_rate_bps, 0.0)
        self.assertEqual(state.taker_rate_bps, 0.0)
        self.assertEqual(state.maker_rebate_bps, 0.0)
        self.assertEqual(state.net_maker_rate_bps, 0.0)
        self.assertEqual(state.fee_rate_source, "kevin_no_cost_demo")

    def test_engine_init_applies_fee_user_fee_source_without_mutating_frozen_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bot = KevinHypeLiquidityEngine(
                asset="xyz:CL",
                api_url="https://api.hyperliquid.xyz",
                dex="xyz",
                config=self._config(),
                events_jsonl_path=str(root / "events.jsonl"),
                samples_jsonl_path=str(root / "samples.jsonl"),
                fills_csv_path=str(root / "fills.csv"),
                trades_csv_path=str(root / "trades.csv"),
                report_dir=str(root / "reports"),
            )
            self.assertEqual(bot.fee_config.user_fee_source, "manual_account_rates")

    def test_healthy_tape_quotes_both_sides(self) -> None:
        plan = build_kevin_quote_plan(
            features=self._features(),
            inventory_qty=0.0,
            inventory_avg_price=None,
            book=self._book(),
            config=self._config(),
        )
        self.assertTrue(plan.quoting_enabled)
        self.assertEqual(plan.quote_mode, "both")
        self.assertGreater(plan.bid_size, 0.0)
        self.assertGreater(plan.ask_size, 0.0)
        self.assertLessEqual(plan.target_half_spread_bps, 1.5)

    def test_buy_pressure_switches_to_bid_only(self) -> None:
        features = self._features()
        features.flow_imbalance = 0.45
        features.book_imbalance = 0.24
        plan = build_kevin_quote_plan(
            features=features,
            inventory_qty=0.0,
            inventory_avg_price=None,
            book=self._book(),
            config=self._config(),
        )
        self.assertTrue(plan.quoting_enabled)
        self.assertEqual(plan.quote_mode, "bid_only")
        self.assertTrue(plan.bid_enabled)
        self.assertFalse(plan.ask_enabled)

    def test_toxic_guard_flattens(self) -> None:
        features = self._features()
        features.flow_imbalance = 0.88
        features.book_imbalance = 0.74
        features.toxicity_score = 1.15
        plan = build_kevin_quote_plan(
            features=features,
            inventory_qty=0.0,
            inventory_avg_price=None,
            book=self._book(),
            config=self._config(),
        )
        self.assertFalse(plan.quoting_enabled)
        self.assertEqual(plan.quoting_reason, "toxicity_guard")
        self.assertEqual(plan.quote_mode, "flat")

    def test_deeper_book_scales_quote_size(self) -> None:
        deep_plan = build_kevin_quote_plan(
            features=self._features(),
            inventory_qty=0.0,
            inventory_avg_price=None,
            book=self._book(),
            config=self._config(),
        )
        thin_features = self._features()
        thin_features.bid_depth = 30.0
        thin_features.ask_depth = 24.0
        thin_plan = build_kevin_quote_plan(
            features=thin_features,
            inventory_qty=0.0,
            inventory_avg_price=None,
            book=BookSnapshot(
                bids=[(39.550, 10.0), (39.549, 8.0), (39.548, 7.0)],
                asks=[(39.552, 8.0), (39.553, 7.0), (39.554, 6.0)],
                exchange_time_ms=1_000,
                received_time_ms=1_030,
            ),
            config=self._config(),
        )
        self.assertGreater(deep_plan.bid_size + deep_plan.ask_size, thin_plan.bid_size + thin_plan.ask_size)

    def test_large_long_inventory_disables_bid(self) -> None:
        features = self._features()
        plan = build_kevin_quote_plan(
            features=features,
            inventory_qty=250.0,
            inventory_avg_price=39.50,
            book=self._book(),
            config=self._config(),
        )
        self.assertEqual(plan.quote_mode, "flat")
        self.assertFalse(plan.quoting_enabled)
        self.assertEqual(plan.quoting_reason, "one_way_guard")

    def test_inventory_protection_steps_inside_spread_for_long_inventory(self) -> None:
        features = self._features()
        features.bid = 39.550
        features.ask = 39.554
        features.mid = 39.552
        features.microprice = 39.5522
        features.spread_bps = ((features.ask - features.bid) / features.mid) * 10_000.0
        plan = build_kevin_quote_plan(
            features=features,
            inventory_qty=80.0,
            inventory_avg_price=39.590,
            book=BookSnapshot(
                bids=[(39.550, 95.0), (39.549, 165.0), (39.548, 230.0)],
                asks=[(39.554, 48.0), (39.555, 72.0), (39.556, 118.0)],
                exchange_time_ms=1_000,
                received_time_ms=1_030,
            ),
            config=self._config(),
            protection_reason="markout_protection",
        )
        self.assertTrue(plan.quoting_enabled)
        self.assertEqual(plan.quote_mode, "ask_only")
        self.assertLess(plan.ask_price, features.ask)
        self.assertGreater(plan.ask_price, features.bid)

    def test_inventory_protection_steps_inside_spread_for_short_inventory(self) -> None:
        features = self._features()
        features.bid = 39.550
        features.ask = 39.554
        features.mid = 39.552
        features.microprice = 39.5518
        features.spread_bps = ((features.ask - features.bid) / features.mid) * 10_000.0
        plan = build_kevin_quote_plan(
            features=features,
            inventory_qty=-80.0,
            inventory_avg_price=39.510,
            book=BookSnapshot(
                bids=[(39.550, 95.0), (39.549, 165.0), (39.548, 230.0)],
                asks=[(39.554, 48.0), (39.555, 72.0), (39.556, 118.0)],
                exchange_time_ms=1_000,
                received_time_ms=1_030,
            ),
            config=self._config(),
            protection_reason="markout_protection",
        )
        self.assertTrue(plan.quoting_enabled)
        self.assertEqual(plan.quote_mode, "bid_only")
        self.assertGreater(plan.bid_price, features.bid)
        self.assertLess(plan.bid_price, features.ask)

    def test_same_side_pressure_switches_inventory_to_exit_only(self) -> None:
        features = self._features()
        features.flow_imbalance = 0.72
        features.book_imbalance = 0.26
        features.toxicity_score = 0.82
        plan = build_kevin_quote_plan(
            features=features,
            inventory_qty=42.0,
            inventory_avg_price=39.545,
            book=self._book(),
            config=self._config(),
        )
        self.assertTrue(plan.quoting_enabled)
        self.assertEqual(plan.quoting_reason, "inventory_protection")
        self.assertEqual(plan.quote_mode, "ask_only")
        self.assertFalse(plan.bid_enabled)
        self.assertTrue(plan.ask_enabled)
        self.assertIn("trend_unwind_protection", plan.decision_note)

    def test_adverse_flow_flip_switches_long_inventory_to_exit_only(self) -> None:
        features = self._features()
        features.flow_imbalance = -0.88
        features.book_imbalance = 0.66
        features.impulse_bps = -2.4
        features.toxicity_score = 0.90
        plan = build_kevin_quote_plan(
            features=features,
            inventory_qty=48.0,
            inventory_avg_price=39.552,
            book=self._book(),
            config=self._config(),
        )
        self.assertTrue(plan.quoting_enabled)
        self.assertEqual(plan.quoting_reason, "inventory_protection")
        self.assertEqual(plan.quote_mode, "ask_only")
        self.assertFalse(plan.bid_enabled)
        self.assertTrue(plan.ask_enabled)
        self.assertIn("adverse_flow_flip_protection", plan.decision_note)

    def test_adverse_flow_flip_switches_short_inventory_to_exit_only(self) -> None:
        features = self._features()
        features.flow_imbalance = 0.91
        features.book_imbalance = -0.52
        features.impulse_bps = 2.7
        features.toxicity_score = 0.93
        plan = build_kevin_quote_plan(
            features=features,
            inventory_qty=-52.0,
            inventory_avg_price=39.548,
            book=self._book(),
            config=self._config(),
        )
        self.assertTrue(plan.quoting_enabled)
        self.assertEqual(plan.quoting_reason, "inventory_protection")
        self.assertEqual(plan.quote_mode, "bid_only")
        self.assertTrue(plan.bid_enabled)
        self.assertFalse(plan.ask_enabled)
        self.assertIn("adverse_flow_flip_protection", plan.decision_note)

    def test_flat_one_way_quote_size_is_capped(self) -> None:
        features = self._features()
        features.flow_imbalance = 0.92
        features.book_imbalance = 0.38
        features.toxicity_score = 0.96
        plan = build_kevin_quote_plan(
            features=features,
            inventory_qty=0.0,
            inventory_avg_price=None,
            book=self._book(),
            config=self._config(),
        )
        base_qty = self._config().base_order_notional / features.mid
        self.assertEqual(plan.quote_mode, "bid_only")
        self.assertGreater(plan.bid_size, 0.0)
        self.assertLessEqual(plan.bid_size, base_qty * 0.55)

    def test_extreme_flat_one_way_sell_pressure_uses_probe_size(self) -> None:
        features = self._features()
        features.flow_imbalance = -1.0
        features.book_imbalance = -0.25
        features.toxicity_score = 1.0
        features.recent_vol_bps = 3.45
        features.impulse_bps = -4.8
        plan = build_kevin_quote_plan(
            features=features,
            inventory_qty=0.0,
            inventory_avg_price=None,
            book=self._book(),
            config=self._config(),
        )
        base_qty = self._config().base_order_notional / features.mid
        self.assertEqual(plan.quote_mode, "ask_only")
        self.assertGreater(plan.ask_size, 0.0)
        self.assertLessEqual(plan.ask_size, base_qty * 0.60)

    def test_flat_long_entry_veto_disables_bid_when_bearish_flow_flips(self) -> None:
        features = self._features()
        features.flow_imbalance = -0.86
        features.book_imbalance = 0.20
        features.impulse_bps = -2.8
        features.toxicity_score = 0.94
        plan = build_kevin_quote_plan(
            features=features,
            inventory_qty=0.0,
            inventory_avg_price=None,
            book=self._book(),
            config=self._config(),
        )
        self.assertTrue(plan.quoting_enabled)
        self.assertEqual(plan.quote_mode, "ask_only")
        self.assertFalse(plan.bid_enabled)
        self.assertTrue(plan.ask_enabled)
        self.assertEqual(plan.bid_reason, "long_adverse_veto")
        self.assertIn("long_veto=", plan.decision_note)

    def test_entry_side_limits_disable_blocked_flat_side(self) -> None:
        plan = build_kevin_quote_plan(
            features=self._features(),
            inventory_qty=0.0,
            inventory_avg_price=None,
            book=self._book(),
            config=self._config(),
        )
        guarded = enforce_kevin_entry_guards(
            plan=plan,
            inventory_qty=0.0,
            bid_block_reason="long_hourly_episode_limit",
        )
        self.assertTrue(guarded.quoting_enabled)
        self.assertEqual(guarded.quote_mode, "ask_only")
        self.assertFalse(guarded.bid_enabled)
        self.assertTrue(guarded.ask_enabled)
        self.assertEqual(guarded.bid_reason, "long_hourly_episode_limit")
        self.assertIn("entry_guard", guarded.decision_note)

    def _features(self) -> MarketFeatures:
        return MarketFeatures(
            sample_exchange_time_ms=1_000,
            sample_received_time_ms=1_030,
            quote_age_ms=55,
            transport_delay_ms=18,
            bid=39.550,
            ask=39.552,
            mid=39.551,
            microprice=39.5513,
            spread_bps=0.506,
            tick_size=0.001,
            tick_bps=0.253,
            recent_vol_bps=3.5,
            impulse_bps=2.1,
            flow_imbalance=0.08,
            buy_volume=120.0,
            sell_volume=104.0,
            trade_count=16,
            trade_rate_per_second=2.4,
            book_imbalance=0.06,
            bid_depth=620.0,
            ask_depth=540.0,
            toxicity_score=0.18,
            event_regime=False,
            quoting_health_ok=True,
            quoting_health_reason="healthy",
        )

    def _book(self) -> BookSnapshot:
        return BookSnapshot(
            bids=[(39.550, 95.0), (39.549, 165.0), (39.548, 230.0)],
            asks=[(39.552, 48.0), (39.553, 72.0), (39.554, 118.0)],
            exchange_time_ms=1_000,
            received_time_ms=1_030,
        )

    def _config(self) -> KevinHypeConfig:
        return KevinHypeConfig(
            account_balance=1000.0,
            leverage=20,
            sample_ms=75,
            warm_start_candles=True,
            startup_quote_immediately=True,
            volatility_lookback_seconds=45.0,
            impulse_window_seconds=6.0,
            flow_window_seconds=6.0,
            depth_levels=6,
            min_trade_count=4,
            max_spread_bps=9.0,
            max_quote_age_ms=650,
            quote_gap_warn_ms=1800,
            reconnect_gap_ms=15000,
            min_half_spread_bps=0.45,
            target_edge_bps=0.22,
            vol_spread_multiplier=0.16,
            toxicity_spread_multiplier=1.05,
            latency_spread_multiplier=0.75,
            inventory_skew_bps=7.0,
            base_order_notional=1600.0,
            max_quote_notional=3200.0,
            max_inventory_notional=9500.0,
            requote_price_bps=0.35,
            requote_size_pct=0.12,
            min_requote_interval_ms=150,
            min_quote_size_multiplier=0.50,
            max_quote_size_multiplier=5.50,
            max_top_level_share=0.28,
            max_depth_share=0.12,
            queue_ahead_penalty=0.10,
            quote_guard_flow_imbalance=0.82,
            quote_guard_book_imbalance=0.68,
            quote_guard_toxicity=1.08,
            one_way_flow_imbalance=0.28,
            one_way_book_imbalance=0.16,
            one_way_alpha_bps=0.80,
            protection_markout_bps=4.20,
            protection_flow_imbalance=0.42,
            protection_book_imbalance=0.24,
            event_impulse_bps=32.0,
            event_vol_bps=22.0,
            toxic_flow_imbalance=0.90,
            toxic_book_imbalance=0.75,
            adverse_exit_bps=10.5,
            adverse_flow_exit_imbalance=0.50,
            adverse_book_exit_imbalance=0.30,
            max_inventory_hold_seconds=14.0,
            kill_hold_seconds=20.0,
            kill_on_event_loss_bps=7.0,
            cooldown_seconds=1.5,
            max_daily_loss=45.0,
            max_episodes_per_day=0,
            max_episodes_per_hour=0,
            stop_quoting_on_event=False,
            fee_product="perps",
            fee_market_type="standard",
            fee_staking_tier="base",
            fee_tier_basis="actual",
            fee_target_tier=0,
            fee_initial_14d_perps_volume=0.0,
            fee_initial_14d_spot_volume=0.0,
            fee_taker_referral_discount_pct=0.0,
            fee_maker_rebate_bps_override=0.0,
            fee_deployer_fee_scale=0.0,
            fee_growth_mode=False,
            fee_aligned_quote_token=False,
            fee_user_address="",
            fee_user_maker_rate_pct_override=None,
            fee_user_taker_rate_pct_override=None,
            fee_buffer_bps=0.0,
            fee_kill_buffer_bps=0.0,
            expected_taker_share_floor=0.0,
            tier_volume_boost_multiplier=0.0,
            tier_volume_relaxation_multiplier=0.0,
            size_toxicity_penalty=0.24,
            size_vol_penalty=0.12,
            size_spread_penalty=0.08,
            size_inventory_penalty=0.22,
            large_inventory_protection_ratio=0.48,
            fee_bps=0.0,
            slippage_bps=0.8,
            max_long_episodes_per_day=900,
            max_short_episodes_per_day=1200,
            max_long_episodes_per_hour=140,
            max_short_episodes_per_hour=180,
            fee_user_fee_source="manual_account_rates",
            long_entry_veto_flow_imbalance=0.62,
            long_entry_veto_impulse_bps=1.60,
            long_entry_veto_toxicity=0.82,
        )


if __name__ == "__main__":
    unittest.main()

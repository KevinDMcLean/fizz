from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from atlas_mm_feeaware_core import (
    MMStrategyConfig,
    MMSnapshot,
    MarketFeatures,
    PassiveOrder,
    ProSpreadMarketMaker,
    QuotePlan,
    aggressive_flatten_reason,
    build_quote_plan,
    disable_quote_plan,
    inventory_protection_reason,
)
from pa_pump_pro_core import BookSnapshot, TradePrint
from hyperliquid_fee_model import HyperliquidFeeConfig, estimate_fee_state


class ProSpreadMarketMakerTests(unittest.TestCase):
    def test_build_quote_plan_skews_away_from_long_inventory(self) -> None:
        features = self._make_features()
        book = self._make_book()
        flat_plan = build_quote_plan(
            features=features,
            inventory_qty=0.0,
            inventory_avg_price=None,
            book=book,
            config=self._config(),
            fee_state=self._fee_state(),
            expected_taker_share=0.10,
        )
        long_plan = build_quote_plan(
            features=features,
            inventory_qty=4.0,
            inventory_avg_price=features.mid,
            book=book,
            config=self._config(),
            fee_state=self._fee_state(),
            expected_taker_share=0.10,
        )
        self.assertTrue(flat_plan.quoting_enabled)
        self.assertTrue(long_plan.quoting_enabled)
        self.assertLess(long_plan.reservation_price, flat_plan.reservation_price)
        self.assertLess(long_plan.bid_size, flat_plan.bid_size)
        self.assertGreater(long_plan.ask_size, 0.0)
        self.assertEqual(long_plan.ask_reason, "active")

    def test_build_quote_plan_disables_during_event_regime(self) -> None:
        features = self._make_features(event_regime=True)
        plan = build_quote_plan(
            features=features,
            inventory_qty=0.0,
            inventory_avg_price=None,
            book=self._make_book(),
            config=self._config(),
            fee_state=self._fee_state(),
            expected_taker_share=0.10,
        )
        self.assertFalse(plan.quoting_enabled)
        self.assertEqual(plan.quoting_reason, "event_regime")

    def test_inventory_protection_reason_triggers_before_kill(self) -> None:
        features = self._make_features(bid=99.97, ask=99.98, mid=99.975, microprice=99.976)
        reason = inventory_protection_reason(
            features=features,
            inventory_qty=2.0,
            inventory_avg_price=100.00,
            inventory_opened_at_ts=time.time() - 2.0,
            config=self._config(),
        )
        self.assertEqual(reason, "markout_protection")

    def test_aggressive_flatten_reason_triggers_on_wider_markout(self) -> None:
        features = self._make_features(bid=99.90, ask=99.91, mid=99.905, microprice=99.905)
        reason = aggressive_flatten_reason(
            features=features,
            inventory_qty=2.0,
            inventory_avg_price=100.00,
            inventory_opened_at_ts=time.time() - 2.0,
            config=self._config(),
            fee_state=self._fee_state(),
        )
        self.assertEqual(reason, "kill_markout")

    def test_build_quote_plan_switches_to_one_way_mode_on_buy_pressure(self) -> None:
        features = self._make_features()
        features.flow_imbalance = 0.6
        features.book_imbalance = 0.35
        plan = build_quote_plan(
            features=features,
            inventory_qty=0.0,
            inventory_avg_price=None,
            book=self._make_book(),
            config=self._config(),
            fee_state=self._fee_state(),
            expected_taker_share=0.10,
        )
        self.assertTrue(plan.quoting_enabled)
        self.assertTrue(plan.bid_enabled)
        self.assertFalse(plan.ask_enabled)
        self.assertEqual(plan.quote_mode, "bid_only")

    def test_build_quote_plan_caps_size_by_book_depth(self) -> None:
        features = self._make_features()
        thin_book = BookSnapshot(
            bids=[(100.00, 3.0), (99.99, 2.0), (99.98, 2.0)],
            asks=[(100.01, 3.0), (100.02, 2.0), (100.03, 2.0)],
            exchange_time_ms=1_000,
            received_time_ms=1_050,
        )
        plan = build_quote_plan(
            features=features,
            inventory_qty=0.0,
            inventory_avg_price=None,
            book=thin_book,
            config=self._config(),
            fee_state=self._fee_state(),
            expected_taker_share=0.10,
        )
        self.assertLessEqual(plan.bid_size, 0.75)
        self.assertLessEqual(plan.ask_size, 0.75)

    def test_build_quote_plan_widens_for_fee_edge(self) -> None:
        features = self._make_features()
        book = self._make_book()
        low_fee = self._fee_state()
        high_fee = self._fee_state(
            market_type="hip3_default",
            projected_turnover=40_000.0,
            deployer_fee_scale=1.0,
            growth_mode=False,
        )
        low_plan = build_quote_plan(
            features=features,
            inventory_qty=0.0,
            inventory_avg_price=None,
            book=book,
            config=self._config(),
            fee_state=low_fee,
            expected_taker_share=0.10,
        )
        high_plan = build_quote_plan(
            features=features,
            inventory_qty=0.0,
            inventory_avg_price=None,
            book=book,
            config=self._config(),
            fee_state=high_fee,
            expected_taker_share=0.10,
        )
        self.assertGreater(high_plan.target_half_spread_bps, low_plan.target_half_spread_bps)

    def test_build_quote_plan_shrinks_size_in_toxic_tape(self) -> None:
        book = self._make_book()
        calm_features = self._make_features()
        toxic_features = self._make_features()
        toxic_features.spread_bps = 8.0
        toxic_features.recent_vol_bps = 12.0
        toxic_features.flow_imbalance = 0.55
        toxic_features.book_imbalance = 0.55
        toxic_features.toxicity_score = 0.9
        calm_plan = build_quote_plan(
            features=calm_features,
            inventory_qty=0.0,
            inventory_avg_price=None,
            book=book,
            config=self._config(),
            fee_state=self._fee_state(),
            expected_taker_share=0.10,
        )
        toxic_plan = build_quote_plan(
            features=toxic_features,
            inventory_qty=0.0,
            inventory_avg_price=None,
            book=book,
            config=self._config(),
            fee_state=self._fee_state(),
            expected_taker_share=0.10,
        )
        self.assertLess(toxic_plan.size_risk_multiplier, calm_plan.size_risk_multiplier)
        self.assertLess(toxic_plan.bid_size + toxic_plan.ask_size, calm_plan.bid_size + calm_plan.ask_size)

    def test_large_inventory_triggers_size_protection(self) -> None:
        features = self._make_features(bid=99.99, ask=100.00, mid=99.995, microprice=99.996)
        reason = inventory_protection_reason(
            features=features,
            inventory_qty=3.0,
            inventory_avg_price=100.00,
            inventory_opened_at_ts=time.time() - 2.0,
            config=self._config(),
        )
        self.assertEqual(reason, "size_protection")

    def test_passive_trade_fill_consumes_queue_and_opens_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp))
            snapshot = self._make_snapshot()
            bot.bid_order = PassiveOrder(
                order_id=1,
                side="BUY",
                price=100.00,
                size=2.0,
                remaining_size=2.0,
                queue_ahead_size=1.0,
                placed_exchange_time_ms=snapshot.sample_exchange_time_ms,
                placed_at_ts=time.time(),
                reason="test",
            )
            trade = TradePrint(side="A", price=100.00, size=3.0, hash="t1", exchange_time_ms=1_000)
            bot._fill_order_from_trade(order=bot.bid_order, trade=trade, snapshot=snapshot)
            self.assertIsNone(bot.bid_order)
            self.assertIsNotNone(bot.current_episode)
            assert bot.current_episode is not None
            self.assertAlmostEqual(bot.current_episode.qty_signed, 2.0)
            self.assertEqual(bot.current_episode.passive_entry_fills, 1)

    def test_aggressive_flatten_closes_inventory_episode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp))
            snapshot = self._make_snapshot()
            bot.last_snapshot = snapshot
            bot._apply_fill(
                side="BUY",
                price=100.00,
                size=2.0,
                liquidity_role="passive",
                reason="entry",
                order_id=1,
                snapshot=snapshot,
            )
            self.assertIsNotNone(bot.current_episode)
            flatten_snapshot = self._make_snapshot(bid=100.08, ask=100.09, mid=100.085, microprice=100.084)
            bot.last_snapshot = flatten_snapshot
            bot._flatten_inventory(flatten_snapshot, "test_flatten")
            self.assertIsNone(bot.current_episode)
            self.assertEqual(len(bot.closed_episode_pnls), 1)
            self.assertGreater(bot.closed_episode_pnls[0], 0.0)
            self.assertEqual(bot.exit_reason_counts.get("test_flatten"), 1)

    def test_disable_quote_plan_flattens_runtime_state(self) -> None:
        plan = QuotePlan(
            quoting_enabled=True,
            quoting_reason="active",
            fair_value=100.0,
            reservation_price=100.0,
            alpha_bps=0.3,
            inventory_skew_bps=0.0,
            target_half_spread_bps=1.5,
            bid_price=99.99,
            ask_price=100.01,
            bid_size=2.0,
            ask_size=2.0,
            bid_queue_ahead_size=1.0,
            ask_queue_ahead_size=1.0,
            bid_enabled=True,
            ask_enabled=True,
            bid_reason="active",
            ask_reason="active",
            quote_mode="both",
            decision_note="both: test",
            event_regime=False,
            toxicity_score=0.2,
        )
        disabled = disable_quote_plan(
            plan,
            reason="daily_episode_limit",
            decision_note="flat: daily episode limit",
        )
        self.assertFalse(disabled.quoting_enabled)
        self.assertEqual(disabled.quoting_reason, "daily_episode_limit")
        self.assertEqual(disabled.quote_mode, "flat")
        self.assertIsNone(disabled.bid_price)
        self.assertIsNone(disabled.ask_price)
        self.assertEqual(disabled.bid_size, 0.0)
        self.assertEqual(disabled.ask_size, 0.0)
        self.assertEqual(disabled.fair_value, plan.fair_value)

    def test_quoting_block_reason_respects_episode_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp))
            bot.daily_episode_count = bot.config.max_episodes_per_day
            reason = bot._quoting_block_reason(time.time())
            self.assertEqual(reason, "daily_episode_limit")

    def _make_bot(self, root: Path) -> ProSpreadMarketMaker:
        return ProSpreadMarketMaker(
            asset="xyz:BRENTOIL",
            api_url="https://api.hyperliquid.xyz",
            dex="xyz",
            config=self._config(),
            events_jsonl_path=str(root / "events.jsonl"),
            samples_jsonl_path=str(root / "samples.jsonl"),
            fills_csv_path=str(root / "fills.csv"),
            trades_csv_path=str(root / "trades.csv"),
            report_dir=str(root / "reports"),
        )

    def _config(self) -> MMStrategyConfig:
        return MMStrategyConfig(
            account_balance=1000.0,
            leverage=20,
            sample_ms=100,
            warm_start_candles=False,
            startup_quote_immediately=True,
            volatility_lookback_seconds=60.0,
            impulse_window_seconds=10.0,
            flow_window_seconds=10.0,
            depth_levels=3,
            min_trade_count=1,
            max_spread_bps=10.0,
            max_quote_age_ms=2_000,
            quote_gap_warn_ms=5_000,
            reconnect_gap_ms=10_000,
            min_half_spread_bps=1.0,
            target_edge_bps=0.5,
            vol_spread_multiplier=0.05,
            toxicity_spread_multiplier=0.8,
            latency_spread_multiplier=0.5,
            inventory_skew_bps=4.0,
            base_order_notional=200.0,
            max_quote_notional=500.0,
            max_inventory_notional=600.0,
            requote_price_bps=0.5,
            requote_size_pct=0.25,
            min_requote_interval_ms=0,
            min_quote_size_multiplier=0.35,
            max_quote_size_multiplier=2.0,
            max_top_level_share=0.25,
            max_depth_share=0.08,
            queue_ahead_penalty=0.35,
            quote_guard_flow_imbalance=0.9,
            quote_guard_book_imbalance=0.8,
            quote_guard_toxicity=1.2,
            one_way_flow_imbalance=0.4,
            one_way_book_imbalance=0.3,
            one_way_alpha_bps=0.5,
            protection_markout_bps=2.0,
            protection_flow_imbalance=0.55,
            protection_book_imbalance=0.35,
            event_impulse_bps=20.0,
            event_vol_bps=18.0,
            toxic_flow_imbalance=0.8,
            toxic_book_imbalance=0.7,
            adverse_exit_bps=5.0,
            adverse_flow_exit_imbalance=0.7,
            adverse_book_exit_imbalance=0.5,
            max_inventory_hold_seconds=10.0,
            kill_hold_seconds=20.0,
            kill_on_event_loss_bps=4.0,
            cooldown_seconds=0.0,
            max_daily_loss=30.0,
            max_episodes_per_day=50,
            max_episodes_per_hour=50,
            stop_quoting_on_event=True,
            fee_product="perps",
            fee_market_type="standard",
            fee_staking_tier="base",
            fee_tier_basis="projected",
            fee_target_tier=3,
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
            fee_buffer_bps=0.15,
            fee_kill_buffer_bps=0.75,
            expected_taker_share_floor=0.08,
            tier_volume_boost_multiplier=0.50,
            tier_volume_relaxation_multiplier=0.35,
            size_toxicity_penalty=0.28,
            size_vol_penalty=0.18,
            size_spread_penalty=0.12,
            size_inventory_penalty=0.20,
            large_inventory_protection_ratio=0.45,
            fee_bps=0.0,
            slippage_bps=0.0,
        )

    def _make_book(self) -> BookSnapshot:
        return BookSnapshot(
            bids=[(100.00, 10.0), (99.99, 8.0), (99.98, 7.0)],
            asks=[(100.01, 11.0), (100.02, 9.0), (100.03, 8.0)],
            exchange_time_ms=1_000,
            received_time_ms=1_050,
        )

    def _make_features(
        self,
        *,
        bid: float = 100.00,
        ask: float = 100.01,
        mid: float = 100.005,
        microprice: float = 100.004,
        event_regime: bool = False,
    ) -> MarketFeatures:
        return MarketFeatures(
            sample_exchange_time_ms=1_000,
            sample_received_time_ms=1_050,
            quote_age_ms=200,
            transport_delay_ms=200,
            bid=bid,
            ask=ask,
            mid=mid,
            microprice=microprice,
            spread_bps=1.0,
            tick_size=0.01,
            tick_bps=1.0,
            recent_vol_bps=4.0,
            impulse_bps=3.0,
            flow_imbalance=0.25,
            buy_volume=15.0,
            sell_volume=10.0,
            trade_count=5,
            trade_rate_per_second=3.0,
            book_imbalance=0.20,
            bid_depth=25.0,
            ask_depth=20.0,
            toxicity_score=0.3,
            event_regime=event_regime,
            quoting_health_ok=True,
            quoting_health_reason="healthy",
        )

    def _make_snapshot(
        self,
        *,
        bid: float = 100.00,
        ask: float = 100.01,
        mid: float = 100.005,
        microprice: float = 100.004,
    ) -> MMSnapshot:
        features = self._make_features(bid=bid, ask=ask, mid=mid, microprice=microprice)
        plan = QuotePlan(
            quoting_enabled=True,
            quoting_reason="active",
            fair_value=microprice,
            reservation_price=microprice,
            alpha_bps=0.0,
            inventory_skew_bps=0.0,
            target_half_spread_bps=1.0,
            bid_price=100.00,
            ask_price=100.01,
            bid_size=2.0,
            ask_size=2.0,
            bid_queue_ahead_size=0.0,
            ask_queue_ahead_size=0.0,
            bid_enabled=True,
            ask_enabled=True,
            bid_reason="active",
            ask_reason="active",
            quote_mode="both",
            decision_note="both: test",
            event_regime=False,
            toxicity_score=0.2,
        )
        bot = self._make_bot(Path(tempfile.mkdtemp()))
        return bot._build_snapshot(features, plan, bot._fee_state())

    def _fee_state(
        self,
        *,
        market_type: str = "standard",
        projected_turnover: float = 0.0,
        deployer_fee_scale: float = 0.0,
        growth_mode: bool = False,
        tier_basis: str | None = None,
        user_maker_rate_pct_override: float | None = None,
        user_taker_rate_pct_override: float | None = None,
    ):
        config = self._config()
        return estimate_fee_state(
            elapsed_seconds=3600.0,
            perp_fill_turnover=projected_turnover,
            config=HyperliquidFeeConfig(
                product=config.fee_product,
                market_type=market_type,
                staking_tier=config.fee_staking_tier,
                tier_basis=tier_basis or config.fee_tier_basis,
                target_tier=config.fee_target_tier,
                initial_14d_perps_volume=config.fee_initial_14d_perps_volume,
                initial_14d_spot_volume=config.fee_initial_14d_spot_volume,
                taker_referral_discount_pct=config.fee_taker_referral_discount_pct,
                maker_rebate_bps_override=config.fee_maker_rebate_bps_override,
                deployer_fee_scale=deployer_fee_scale,
                growth_mode=growth_mode,
                aligned_quote_token=False,
                user_maker_rate_pct_override=user_maker_rate_pct_override,
                user_taker_rate_pct_override=user_taker_rate_pct_override,
                user_fee_source="manual_account_rates" if (user_maker_rate_pct_override is not None or user_taker_rate_pct_override is not None) else "estimated",
            ),
        )

    def test_fee_state_uses_exact_account_rates_without_rescaling(self) -> None:
        fee_state = self._fee_state(
            market_type="hip3_growth",
            deployer_fee_scale=1.0,
            growth_mode=True,
            tier_basis="actual",
            user_maker_rate_pct_override=0.0029,
            user_taker_rate_pct_override=0.0086,
        )
        self.assertAlmostEqual(fee_state.maker_rate_bps, 0.29, places=6)
        self.assertAlmostEqual(fee_state.taker_rate_bps, 0.86, places=6)

    def test_fee_state_scales_userfees_schedule_for_growth_mode(self) -> None:
        config = self._config()
        fee_state = estimate_fee_state(
            elapsed_seconds=3600.0,
            perp_fill_turnover=0.0,
            config=HyperliquidFeeConfig(
                product=config.fee_product,
                market_type="hip3_growth",
                staking_tier=config.fee_staking_tier,
                tier_basis="actual",
                target_tier=config.fee_target_tier,
                initial_14d_perps_volume=config.fee_initial_14d_perps_volume,
                initial_14d_spot_volume=config.fee_initial_14d_spot_volume,
                taker_referral_discount_pct=0.0,
                maker_rebate_bps_override=0.0,
                deployer_fee_scale=1.0,
                growth_mode=True,
                aligned_quote_token=False,
                user_maker_rate_pct_override=0.015,
                user_taker_rate_pct_override=0.045,
                user_fee_source="userFees",
            ),
        )
        self.assertAlmostEqual(fee_state.maker_rate_bps, 0.30, places=6)
        self.assertAlmostEqual(fee_state.taker_rate_bps, 0.90, places=6)

    def test_manual_fee_overrides_take_precedence_over_userfees_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._make_bot(Path(tmp))
            bot.config = replace(
                bot.config,
                fee_user_address="0xabc",
                fee_user_maker_rate_pct_override=0.0029,
                fee_user_taker_rate_pct_override=0.0086,
            )
            bot.fee_config = replace(
                bot.fee_config,
                user_maker_rate_pct_override=0.0029,
                user_taker_rate_pct_override=0.0086,
            )

            def fake_post_info(payload):
                if payload.get("type") == "perpDexs":
                    return [{"name": "xyz", "deployerFeeScale": 1.0}]
                if payload.get("type") == "metaAndAssetCtxs":
                    return [{"universe": [{"name": "xyz:BRENTOIL", "growthMode": "enabled"}], "collateralToken": 0}]
                if payload.get("type") == "alignedQuoteTokenInfo":
                    return None
                if payload.get("type") == "userFees":
                    return {"userAddRate": 0.00015, "userCrossRate": 0.00045}
                raise AssertionError(payload)

            bot._post_info = fake_post_info  # type: ignore[method-assign]
            bot._configure_fee_model()
            self.assertEqual(bot.fee_config.user_fee_source, "manual_account_rates")
            self.assertAlmostEqual(bot.fee_config.user_maker_rate_pct_override or 0.0, 0.0029, places=8)
            self.assertAlmostEqual(bot.fee_config.user_taker_rate_pct_override or 0.0, 0.0086, places=8)

    def test_growth_mode_only_contributes_ten_percent_volume_to_tiers(self) -> None:
        fee_state = self._fee_state(
            market_type="hip3_growth",
            deployer_fee_scale=1.0,
            growth_mode=True,
            projected_turnover=10_000_000.0,
        )
        self.assertAlmostEqual(fee_state.current_weighted_14d_volume, 1_000_000.0, places=6)
        self.assertAlmostEqual(fee_state.daily_weighted_volume_run_rate, 24_000_000.0, places=6)


if __name__ == "__main__":
    unittest.main()

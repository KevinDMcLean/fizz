#!/usr/bin/env python3
"""Kevin Hype Liquidity Engine: passive-first HYPE market maker."""

from __future__ import annotations

import argparse
import logging

from atlas_mm_feeaware_core import MMStrategyConfig
from kevin_hype_liquidity_core import APP_NAME, KevinHypeLiquidityEngine

try:
    from hyperliquid.utils import constants
except ImportError:
    constants = None  # type: ignore[assignment]


DEFAULT_ASSET = "HYPE"
DEFAULT_ACCOUNT_BALANCE = 1000.0
DEFAULT_LEVERAGE = 20
DEFAULT_SAMPLE_MS = 75
DEFAULT_WARM_START_CANDLES = True
DEFAULT_STARTUP_QUOTE_IMMEDIATELY = True
DEFAULT_VOLATILITY_LOOKBACK_SECONDS = 45.0
DEFAULT_IMPULSE_WINDOW_SECONDS = 6.0
DEFAULT_FLOW_WINDOW_SECONDS = 6.0
DEFAULT_DEPTH_LEVELS = 6
DEFAULT_MIN_TRADE_COUNT = 4
DEFAULT_MAX_SPREAD_BPS = 9.0
DEFAULT_MAX_QUOTE_AGE_MS = 650
DEFAULT_QUOTE_GAP_WARN_MS = 1800
DEFAULT_RECONNECT_GAP_MS = 15000
DEFAULT_MIN_HALF_SPREAD_BPS = 0.45
DEFAULT_TARGET_EDGE_BPS = 0.22
DEFAULT_VOL_SPREAD_MULTIPLIER = 0.16
DEFAULT_TOXICITY_SPREAD_MULTIPLIER = 1.05
DEFAULT_LATENCY_SPREAD_MULTIPLIER = 0.75
DEFAULT_INVENTORY_SKEW_BPS = 8.5
DEFAULT_BASE_ORDER_NOTIONAL = 1600.0
DEFAULT_MAX_QUOTE_NOTIONAL = 3200.0
DEFAULT_MAX_INVENTORY_NOTIONAL = 9500.0
DEFAULT_REQUOTE_PRICE_BPS = 0.35
DEFAULT_REQUOTE_SIZE_PCT = 0.12
DEFAULT_MIN_REQUOTE_INTERVAL_MS = 150
DEFAULT_MIN_QUOTE_SIZE_MULTIPLIER = 0.50
DEFAULT_MAX_QUOTE_SIZE_MULTIPLIER = 5.50
DEFAULT_MAX_TOP_LEVEL_SHARE = 0.28
DEFAULT_MAX_DEPTH_SHARE = 0.12
DEFAULT_QUEUE_AHEAD_PENALTY = 0.10
DEFAULT_QUOTE_GUARD_FLOW_IMBALANCE = 0.82
DEFAULT_QUOTE_GUARD_BOOK_IMBALANCE = 0.68
DEFAULT_QUOTE_GUARD_TOXICITY = 1.08
DEFAULT_ONE_WAY_FLOW_IMBALANCE = 0.28
DEFAULT_ONE_WAY_BOOK_IMBALANCE = 0.16
DEFAULT_ONE_WAY_ALPHA_BPS = 0.80
DEFAULT_PROTECTION_MARKOUT_BPS = 3.60
DEFAULT_PROTECTION_FLOW_IMBALANCE = 0.34
DEFAULT_PROTECTION_BOOK_IMBALANCE = 0.18
DEFAULT_EVENT_IMPULSE_BPS = 32.0
DEFAULT_EVENT_VOL_BPS = 22.0
DEFAULT_TOXIC_FLOW_IMBALANCE = 0.90
DEFAULT_TOXIC_BOOK_IMBALANCE = 0.75
DEFAULT_ADVERSE_EXIT_BPS = 10.5
DEFAULT_ADVERSE_FLOW_EXIT_IMBALANCE = 0.50
DEFAULT_ADVERSE_BOOK_EXIT_IMBALANCE = 0.30
DEFAULT_MAX_INVENTORY_HOLD_SECONDS = 14.0
DEFAULT_KILL_HOLD_SECONDS = 16.0
DEFAULT_KILL_ON_EVENT_LOSS_BPS = 7.0
DEFAULT_COOLDOWN_SECONDS = 1.5
DEFAULT_MAX_DAILY_LOSS = 45.0
DEFAULT_MAX_EPISODES_PER_DAY = 0
DEFAULT_MAX_EPISODES_PER_HOUR = 0
DEFAULT_STOP_QUOTING_ON_EVENT = False
DEFAULT_SIZE_TOXICITY_PENALTY = 0.24
DEFAULT_SIZE_VOL_PENALTY = 0.12
DEFAULT_SIZE_SPREAD_PENALTY = 0.08
DEFAULT_SIZE_INVENTORY_PENALTY = 0.22
DEFAULT_LARGE_INVENTORY_PROTECTION_RATIO = 0.48
DEFAULT_SLIPPAGE_BPS = 0.8
DEFAULT_FEE_BPS = 0.0
DEFAULT_EVENTS_JSONL = "logs/kevin_hype_events.jsonl"
DEFAULT_SAMPLES_JSONL = "logs/kevin_hype_samples.jsonl"
DEFAULT_FILLS_CSV = "logs/kevin_hype_fills.csv"
DEFAULT_TRADES_CSV = "logs/kevin_hype_trades.csv"
DEFAULT_REPORT_DIR = "logs/kevin_hype_reports"
DEFAULT_API_URL = constants.MAINNET_API_URL if constants is not None else "https://api.hyperliquid.xyz"
DEFAULT_DEX = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--asset", default=DEFAULT_ASSET)
    parser.add_argument("--account-balance", type=float, default=DEFAULT_ACCOUNT_BALANCE)
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--dex", default=DEFAULT_DEX)
    parser.add_argument("--leverage", type=int, default=DEFAULT_LEVERAGE)
    parser.add_argument("--sample-ms", type=int, default=DEFAULT_SAMPLE_MS)
    parser.add_argument("--warm-start-candles", action=argparse.BooleanOptionalAction, default=DEFAULT_WARM_START_CANDLES)
    parser.add_argument("--startup-quote-immediately", action=argparse.BooleanOptionalAction, default=DEFAULT_STARTUP_QUOTE_IMMEDIATELY)
    parser.add_argument("--volatility-lookback-seconds", type=float, default=DEFAULT_VOLATILITY_LOOKBACK_SECONDS)
    parser.add_argument("--impulse-window-seconds", type=float, default=DEFAULT_IMPULSE_WINDOW_SECONDS)
    parser.add_argument("--flow-window-seconds", type=float, default=DEFAULT_FLOW_WINDOW_SECONDS)
    parser.add_argument("--depth-levels", type=int, default=DEFAULT_DEPTH_LEVELS)
    parser.add_argument("--min-trade-count", type=int, default=DEFAULT_MIN_TRADE_COUNT)
    parser.add_argument("--max-spread-bps", type=float, default=DEFAULT_MAX_SPREAD_BPS)
    parser.add_argument("--max-quote-age-ms", type=int, default=DEFAULT_MAX_QUOTE_AGE_MS)
    parser.add_argument("--quote-gap-warn-ms", type=int, default=DEFAULT_QUOTE_GAP_WARN_MS)
    parser.add_argument("--reconnect-gap-ms", type=int, default=DEFAULT_RECONNECT_GAP_MS)
    parser.add_argument("--min-half-spread-bps", type=float, default=DEFAULT_MIN_HALF_SPREAD_BPS)
    parser.add_argument("--target-edge-bps", type=float, default=DEFAULT_TARGET_EDGE_BPS)
    parser.add_argument("--vol-spread-multiplier", type=float, default=DEFAULT_VOL_SPREAD_MULTIPLIER)
    parser.add_argument("--toxicity-spread-multiplier", type=float, default=DEFAULT_TOXICITY_SPREAD_MULTIPLIER)
    parser.add_argument("--latency-spread-multiplier", type=float, default=DEFAULT_LATENCY_SPREAD_MULTIPLIER)
    parser.add_argument("--inventory-skew-bps", type=float, default=DEFAULT_INVENTORY_SKEW_BPS)
    parser.add_argument("--base-order-notional", type=float, default=DEFAULT_BASE_ORDER_NOTIONAL)
    parser.add_argument("--max-quote-notional", type=float, default=DEFAULT_MAX_QUOTE_NOTIONAL)
    parser.add_argument("--max-inventory-notional", type=float, default=DEFAULT_MAX_INVENTORY_NOTIONAL)
    parser.add_argument("--requote-price-bps", type=float, default=DEFAULT_REQUOTE_PRICE_BPS)
    parser.add_argument("--requote-size-pct", type=float, default=DEFAULT_REQUOTE_SIZE_PCT)
    parser.add_argument("--min-requote-interval-ms", type=int, default=DEFAULT_MIN_REQUOTE_INTERVAL_MS)
    parser.add_argument("--min-quote-size-multiplier", type=float, default=DEFAULT_MIN_QUOTE_SIZE_MULTIPLIER)
    parser.add_argument("--max-quote-size-multiplier", type=float, default=DEFAULT_MAX_QUOTE_SIZE_MULTIPLIER)
    parser.add_argument("--max-top-level-share", type=float, default=DEFAULT_MAX_TOP_LEVEL_SHARE)
    parser.add_argument("--max-depth-share", type=float, default=DEFAULT_MAX_DEPTH_SHARE)
    parser.add_argument("--queue-ahead-penalty", type=float, default=DEFAULT_QUEUE_AHEAD_PENALTY)
    parser.add_argument("--quote-guard-flow-imbalance", type=float, default=DEFAULT_QUOTE_GUARD_FLOW_IMBALANCE)
    parser.add_argument("--quote-guard-book-imbalance", type=float, default=DEFAULT_QUOTE_GUARD_BOOK_IMBALANCE)
    parser.add_argument("--quote-guard-toxicity", type=float, default=DEFAULT_QUOTE_GUARD_TOXICITY)
    parser.add_argument("--one-way-flow-imbalance", type=float, default=DEFAULT_ONE_WAY_FLOW_IMBALANCE)
    parser.add_argument("--one-way-book-imbalance", type=float, default=DEFAULT_ONE_WAY_BOOK_IMBALANCE)
    parser.add_argument("--one-way-alpha-bps", type=float, default=DEFAULT_ONE_WAY_ALPHA_BPS)
    parser.add_argument("--protection-markout-bps", type=float, default=DEFAULT_PROTECTION_MARKOUT_BPS)
    parser.add_argument("--protection-flow-imbalance", type=float, default=DEFAULT_PROTECTION_FLOW_IMBALANCE)
    parser.add_argument("--protection-book-imbalance", type=float, default=DEFAULT_PROTECTION_BOOK_IMBALANCE)
    parser.add_argument("--event-impulse-bps", type=float, default=DEFAULT_EVENT_IMPULSE_BPS)
    parser.add_argument("--event-vol-bps", type=float, default=DEFAULT_EVENT_VOL_BPS)
    parser.add_argument("--toxic-flow-imbalance", type=float, default=DEFAULT_TOXIC_FLOW_IMBALANCE)
    parser.add_argument("--toxic-book-imbalance", type=float, default=DEFAULT_TOXIC_BOOK_IMBALANCE)
    parser.add_argument("--adverse-exit-bps", type=float, default=DEFAULT_ADVERSE_EXIT_BPS)
    parser.add_argument("--adverse-flow-exit-imbalance", type=float, default=DEFAULT_ADVERSE_FLOW_EXIT_IMBALANCE)
    parser.add_argument("--adverse-book-exit-imbalance", type=float, default=DEFAULT_ADVERSE_BOOK_EXIT_IMBALANCE)
    parser.add_argument("--max-inventory-hold-seconds", type=float, default=DEFAULT_MAX_INVENTORY_HOLD_SECONDS)
    parser.add_argument("--kill-hold-seconds", type=float, default=DEFAULT_KILL_HOLD_SECONDS)
    parser.add_argument("--kill-on-event-loss-bps", type=float, default=DEFAULT_KILL_ON_EVENT_LOSS_BPS)
    parser.add_argument("--cooldown-seconds", type=float, default=DEFAULT_COOLDOWN_SECONDS)
    parser.add_argument("--max-daily-loss", type=float, default=DEFAULT_MAX_DAILY_LOSS)
    parser.add_argument("--max-episodes-per-day", type=int, default=DEFAULT_MAX_EPISODES_PER_DAY)
    parser.add_argument("--max-episodes-per-hour", type=int, default=DEFAULT_MAX_EPISODES_PER_HOUR)
    parser.add_argument("--stop-quoting-on-event", action=argparse.BooleanOptionalAction, default=DEFAULT_STOP_QUOTING_ON_EVENT)
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    parser.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    parser.add_argument("--events-jsonl", default=DEFAULT_EVENTS_JSONL)
    parser.add_argument("--samples-jsonl", default=DEFAULT_SAMPLES_JSONL)
    parser.add_argument("--fills-csv", default=DEFAULT_FILLS_CSV)
    parser.add_argument("--trades-csv", default=DEFAULT_TRADES_CSV)
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR)
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> MMStrategyConfig:
    return MMStrategyConfig(
        account_balance=args.account_balance,
        leverage=args.leverage,
        sample_ms=args.sample_ms,
        warm_start_candles=args.warm_start_candles,
        startup_quote_immediately=args.startup_quote_immediately,
        volatility_lookback_seconds=args.volatility_lookback_seconds,
        impulse_window_seconds=args.impulse_window_seconds,
        flow_window_seconds=args.flow_window_seconds,
        depth_levels=args.depth_levels,
        min_trade_count=args.min_trade_count,
        max_spread_bps=args.max_spread_bps,
        max_quote_age_ms=args.max_quote_age_ms,
        quote_gap_warn_ms=args.quote_gap_warn_ms,
        reconnect_gap_ms=args.reconnect_gap_ms,
        min_half_spread_bps=args.min_half_spread_bps,
        target_edge_bps=args.target_edge_bps,
        vol_spread_multiplier=args.vol_spread_multiplier,
        toxicity_spread_multiplier=args.toxicity_spread_multiplier,
        latency_spread_multiplier=args.latency_spread_multiplier,
        inventory_skew_bps=args.inventory_skew_bps,
        base_order_notional=args.base_order_notional,
        max_quote_notional=args.max_quote_notional,
        max_inventory_notional=args.max_inventory_notional,
        requote_price_bps=args.requote_price_bps,
        requote_size_pct=args.requote_size_pct,
        min_requote_interval_ms=args.min_requote_interval_ms,
        min_quote_size_multiplier=args.min_quote_size_multiplier,
        max_quote_size_multiplier=args.max_quote_size_multiplier,
        max_top_level_share=args.max_top_level_share,
        max_depth_share=args.max_depth_share,
        queue_ahead_penalty=args.queue_ahead_penalty,
        quote_guard_flow_imbalance=args.quote_guard_flow_imbalance,
        quote_guard_book_imbalance=args.quote_guard_book_imbalance,
        quote_guard_toxicity=args.quote_guard_toxicity,
        one_way_flow_imbalance=args.one_way_flow_imbalance,
        one_way_book_imbalance=args.one_way_book_imbalance,
        one_way_alpha_bps=args.one_way_alpha_bps,
        protection_markout_bps=args.protection_markout_bps,
        protection_flow_imbalance=args.protection_flow_imbalance,
        protection_book_imbalance=args.protection_book_imbalance,
        event_impulse_bps=args.event_impulse_bps,
        event_vol_bps=args.event_vol_bps,
        toxic_flow_imbalance=args.toxic_flow_imbalance,
        toxic_book_imbalance=args.toxic_book_imbalance,
        adverse_exit_bps=args.adverse_exit_bps,
        adverse_flow_exit_imbalance=args.adverse_flow_exit_imbalance,
        adverse_book_exit_imbalance=args.adverse_book_exit_imbalance,
        max_inventory_hold_seconds=args.max_inventory_hold_seconds,
        kill_hold_seconds=args.kill_hold_seconds,
        kill_on_event_loss_bps=args.kill_on_event_loss_bps,
        cooldown_seconds=args.cooldown_seconds,
        max_daily_loss=args.max_daily_loss,
        max_episodes_per_day=args.max_episodes_per_day,
        max_episodes_per_hour=args.max_episodes_per_hour,
        stop_quoting_on_event=args.stop_quoting_on_event,
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
        size_toxicity_penalty=DEFAULT_SIZE_TOXICITY_PENALTY,
        size_vol_penalty=DEFAULT_SIZE_VOL_PENALTY,
        size_spread_penalty=DEFAULT_SIZE_SPREAD_PENALTY,
        size_inventory_penalty=DEFAULT_SIZE_INVENTORY_PENALTY,
        large_inventory_protection_ratio=DEFAULT_LARGE_INVENTORY_PROTECTION_RATIO,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
    )


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    bot = KevinHypeLiquidityEngine(
        asset=args.asset,
        api_url=args.api_url,
        dex=args.dex,
        config=build_config(args),
        events_jsonl_path=args.events_jsonl,
        samples_jsonl_path=args.samples_jsonl,
        fills_csv_path=args.fills_csv,
        trades_csv_path=args.trades_csv,
        report_dir=args.report_dir,
    )
    try:
        bot.run()
    except KeyboardInterrupt:
        logging.info("Stopped by user")
    finally:
        report_path = bot.finalize(reason="keyboard_interrupt_or_exit")
        if report_path is not None:
            logging.info("Summary report: %s", report_path)


if __name__ == "__main__":
    main()

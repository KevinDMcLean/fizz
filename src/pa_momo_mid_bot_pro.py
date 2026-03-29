#!/usr/bin/env python3
"""Paper-trading mid-frequency momentum engine for directional continuation."""

from __future__ import annotations

import argparse
import logging

from pa_momo_mid_pro_core import MidFrequencyMomentumBot

try:
    from hyperliquid.utils import constants
except ImportError:
    constants = None  # type: ignore[assignment]


DEFAULT_ASSET = "xyz:BRENTOIL"
DEFAULT_ACCOUNT_BALANCE = 1000.0
DEFAULT_LEVERAGE = 20
DEFAULT_SAMPLE_MS = 120
DEFAULT_WARM_START_CANDLES = True
DEFAULT_STARTUP_STATE_ENTRY = True
DEFAULT_ENTRY_CONFIRMATION_SAMPLES = 2
DEFAULT_FAST_IMPULSE_WINDOW_SECONDS = 5.0
DEFAULT_CONFIRM_IMPULSE_WINDOW_SECONDS = 14.0
DEFAULT_BREAKOUT_LOOKBACK_SECONDS = 60.0
DEFAULT_VOLATILITY_LOOKBACK_SECONDS = 120.0
DEFAULT_FLOW_WINDOW_SECONDS = 8.0
DEFAULT_MIN_FAST_IMPULSE_BPS = 3.5
DEFAULT_MIN_CONFIRM_IMPULSE_BPS = 6.5
DEFAULT_FAST_VOL_MULTIPLIER = 0.50
DEFAULT_CONFIRM_VOL_MULTIPLIER = 0.72
DEFAULT_BREAKOUT_BUFFER_BPS = 0.35
DEFAULT_FLOW_IMBALANCE_MIN = 0.06
DEFAULT_BOOK_IMBALANCE_MIN = 0.03
DEFAULT_MIN_TRADE_COUNT = 2
DEFAULT_DEPTH_LEVELS = 3
DEFAULT_MAX_SPREAD_BPS = 5.5
DEFAULT_MAX_QUOTE_AGE_MS = 900
DEFAULT_QUOTE_GAP_WARN_MS = 2500
DEFAULT_RECONNECT_GAP_MS = 30000
DEFAULT_EXTREME_IMPULSE_BPS = 55.0
DEFAULT_EXTREME_VOL_BPS = 48.0
DEFAULT_SETUP_SCORE_MIN = 58.0
DEFAULT_TACTICAL_SCORE_MIN = 82.0
DEFAULT_TACTICAL_FAST_THRESHOLD_RATIO = 0.55
DEFAULT_TACTICAL_CONFIRM_THRESHOLD_RATIO = 0.65
DEFAULT_TACTICAL_BREAKOUT_SLACK_BPS = 0.25
DEFAULT_TACTICAL_FLOW_MULTIPLIER = 0.85
DEFAULT_TACTICAL_BOOK_MULTIPLIER = 0.85
DEFAULT_SCORE_EDGE_MIN = 14.0
DEFAULT_INSTANT_ENTRY_SCORE_MIN = 96.0
DEFAULT_INITIAL_STOP_MIN_BPS = 8.0
DEFAULT_INITIAL_STOP_VOL_MULTIPLIER = 0.95
DEFAULT_TRAIL_MIN_BPS = 10.0
DEFAULT_TRAIL_VOL_MULTIPLIER = 1.45
DEFAULT_TRAIL_ARM_R = 0.80
DEFAULT_BREAK_EVEN_LOCK_R = 1.10
DEFAULT_SCORE_NOTIONAL_BOOST = 0.32
DEFAULT_INITIAL_NOTIONAL_FRACTION = 0.24
DEFAULT_MAX_NOTIONAL_FRACTION = 0.65
DEFAULT_MAX_ADD_ONS = 3
DEFAULT_ADD_ON_TRIGGER_R = 0.55
DEFAULT_ADD_ON_FRACTION = 0.14
DEFAULT_ADD_ON_SCORE_MIN = 78.0
DEFAULT_ADD_ON_BREAKOUT_EXTENSION_BPS = 0.45
DEFAULT_ADD_ON_FLOW_MULTIPLIER = 1.00
DEFAULT_ADD_ON_BOOK_MULTIPLIER = 1.00
DEFAULT_ADD_ON_MIN_SECONDS = 2.5
DEFAULT_MAX_REDUCTIONS = 3
DEFAULT_DE_RISK_FRACTION = 0.25
DEFAULT_DE_RISK_SCORE_THRESHOLD = 60.0
DEFAULT_DE_RISK_CONFIRM_RATIO = 0.45
DEFAULT_DE_RISK_FLOW_MULTIPLIER = 0.60
DEFAULT_RUNNER_CORE_FRACTION = 0.40
DEFAULT_FAILURE_HOLD_SECONDS = 9.0
DEFAULT_FAILURE_MIN_FOLLOWTHROUGH_BPS = 2.75
DEFAULT_FLOW_FLIP_EXIT_IMBALANCE = 0.18
DEFAULT_BOOK_FLIP_EXIT_IMBALANCE = 0.10
DEFAULT_BREAKOUT_FAIL_BUFFER_BPS = 0.85
DEFAULT_MAX_HOLD_SECONDS = 600.0
DEFAULT_TIME_STOP_MIN_R = 0.05
DEFAULT_STOP_CONFIRMATION_TICKS = 2
DEFAULT_SLIPPAGE_BPS = 0.0
DEFAULT_FEE_BPS = 0.0
DEFAULT_EQUITY_RISK_PCT = 1.0
DEFAULT_COOLDOWN_SECONDS = 20.0
DEFAULT_MAX_DAILY_LOSS = 35.0
DEFAULT_MAX_TRADES_PER_DAY = 30
DEFAULT_MAX_TRADES_PER_HOUR = 20
DEFAULT_MIN_HOLD_SECONDS = 0.0
DEFAULT_EVENTS_JSONL = "logs/mid_pro_events.jsonl"
DEFAULT_SAMPLES_JSONL = "logs/mid_pro_samples.jsonl"
DEFAULT_TRADES_CSV = "logs/mid_pro_trades.csv"
DEFAULT_MARKOUTS_JSONL = "logs/mid_pro_markouts.jsonl"
DEFAULT_REPORT_DIR = "logs/mid_pro_reports"
DEFAULT_API_URL = (
    constants.MAINNET_API_URL if constants is not None else "https://api.hyperliquid.xyz"
)
DEFAULT_DEX = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Paper-trading mid-frequency momentum engine for Hyperliquid",
    )
    parser.add_argument("--asset", default=DEFAULT_ASSET)
    parser.add_argument("--account-balance", type=float, default=DEFAULT_ACCOUNT_BALANCE)
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument(
        "--dex",
        default=DEFAULT_DEX,
        help="Hyperliquid dex namespace. Leave empty to auto-infer from asset prefix.",
    )
    parser.add_argument("--leverage", type=int, default=DEFAULT_LEVERAGE)
    parser.add_argument("--sample-ms", type=int, default=DEFAULT_SAMPLE_MS)
    parser.add_argument(
        "--warm-start-candles",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_WARM_START_CANDLES,
    )
    parser.add_argument(
        "--startup-state-entry",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_STARTUP_STATE_ENTRY,
    )
    parser.add_argument(
        "--entry-confirmation-samples",
        type=int,
        default=DEFAULT_ENTRY_CONFIRMATION_SAMPLES,
    )
    parser.add_argument(
        "--fast-impulse-window-seconds",
        type=float,
        default=DEFAULT_FAST_IMPULSE_WINDOW_SECONDS,
    )
    parser.add_argument(
        "--confirm-impulse-window-seconds",
        type=float,
        default=DEFAULT_CONFIRM_IMPULSE_WINDOW_SECONDS,
    )
    parser.add_argument(
        "--breakout-lookback-seconds",
        type=float,
        default=DEFAULT_BREAKOUT_LOOKBACK_SECONDS,
    )
    parser.add_argument(
        "--volatility-lookback-seconds",
        type=float,
        default=DEFAULT_VOLATILITY_LOOKBACK_SECONDS,
    )
    parser.add_argument(
        "--flow-window-seconds",
        type=float,
        default=DEFAULT_FLOW_WINDOW_SECONDS,
    )
    parser.add_argument("--min-fast-impulse-bps", type=float, default=DEFAULT_MIN_FAST_IMPULSE_BPS)
    parser.add_argument(
        "--min-confirm-impulse-bps",
        type=float,
        default=DEFAULT_MIN_CONFIRM_IMPULSE_BPS,
    )
    parser.add_argument(
        "--fast-vol-multiplier",
        type=float,
        default=DEFAULT_FAST_VOL_MULTIPLIER,
    )
    parser.add_argument(
        "--confirm-vol-multiplier",
        type=float,
        default=DEFAULT_CONFIRM_VOL_MULTIPLIER,
    )
    parser.add_argument("--breakout-buffer-bps", type=float, default=DEFAULT_BREAKOUT_BUFFER_BPS)
    parser.add_argument("--flow-imbalance-min", type=float, default=DEFAULT_FLOW_IMBALANCE_MIN)
    parser.add_argument("--book-imbalance-min", type=float, default=DEFAULT_BOOK_IMBALANCE_MIN)
    parser.add_argument("--min-trade-count", type=int, default=DEFAULT_MIN_TRADE_COUNT)
    parser.add_argument("--depth-levels", type=int, default=DEFAULT_DEPTH_LEVELS)
    parser.add_argument("--max-spread-bps", type=float, default=DEFAULT_MAX_SPREAD_BPS)
    parser.add_argument("--max-quote-age-ms", type=int, default=DEFAULT_MAX_QUOTE_AGE_MS)
    parser.add_argument("--quote-gap-warn-ms", type=int, default=DEFAULT_QUOTE_GAP_WARN_MS)
    parser.add_argument("--reconnect-gap-ms", type=int, default=DEFAULT_RECONNECT_GAP_MS)
    parser.add_argument("--extreme-impulse-bps", type=float, default=DEFAULT_EXTREME_IMPULSE_BPS)
    parser.add_argument("--extreme-vol-bps", type=float, default=DEFAULT_EXTREME_VOL_BPS)
    parser.add_argument("--setup-score-min", type=float, default=DEFAULT_SETUP_SCORE_MIN)
    parser.add_argument("--tactical-score-min", type=float, default=DEFAULT_TACTICAL_SCORE_MIN)
    parser.add_argument(
        "--tactical-fast-threshold-ratio",
        type=float,
        default=DEFAULT_TACTICAL_FAST_THRESHOLD_RATIO,
    )
    parser.add_argument(
        "--tactical-confirm-threshold-ratio",
        type=float,
        default=DEFAULT_TACTICAL_CONFIRM_THRESHOLD_RATIO,
    )
    parser.add_argument(
        "--tactical-breakout-slack-bps",
        type=float,
        default=DEFAULT_TACTICAL_BREAKOUT_SLACK_BPS,
    )
    parser.add_argument(
        "--tactical-flow-multiplier",
        type=float,
        default=DEFAULT_TACTICAL_FLOW_MULTIPLIER,
    )
    parser.add_argument(
        "--tactical-book-multiplier",
        type=float,
        default=DEFAULT_TACTICAL_BOOK_MULTIPLIER,
    )
    parser.add_argument("--score-edge-min", type=float, default=DEFAULT_SCORE_EDGE_MIN)
    parser.add_argument(
        "--instant-entry-score-min",
        type=float,
        default=DEFAULT_INSTANT_ENTRY_SCORE_MIN,
    )
    parser.add_argument("--initial-stop-min-bps", type=float, default=DEFAULT_INITIAL_STOP_MIN_BPS)
    parser.add_argument(
        "--initial-stop-vol-multiplier",
        type=float,
        default=DEFAULT_INITIAL_STOP_VOL_MULTIPLIER,
    )
    parser.add_argument("--trail-min-bps", type=float, default=DEFAULT_TRAIL_MIN_BPS)
    parser.add_argument(
        "--trail-vol-multiplier",
        type=float,
        default=DEFAULT_TRAIL_VOL_MULTIPLIER,
    )
    parser.add_argument("--trail-arm-r", type=float, default=DEFAULT_TRAIL_ARM_R)
    parser.add_argument("--break-even-lock-r", type=float, default=DEFAULT_BREAK_EVEN_LOCK_R)
    parser.add_argument(
        "--score-notional-boost",
        type=float,
        default=DEFAULT_SCORE_NOTIONAL_BOOST,
    )
    parser.add_argument(
        "--initial-notional-fraction",
        type=float,
        default=DEFAULT_INITIAL_NOTIONAL_FRACTION,
    )
    parser.add_argument(
        "--max-notional-fraction",
        type=float,
        default=DEFAULT_MAX_NOTIONAL_FRACTION,
    )
    parser.add_argument("--max-add-ons", type=int, default=DEFAULT_MAX_ADD_ONS)
    parser.add_argument("--add-on-trigger-r", type=float, default=DEFAULT_ADD_ON_TRIGGER_R)
    parser.add_argument("--add-on-fraction", type=float, default=DEFAULT_ADD_ON_FRACTION)
    parser.add_argument("--add-on-score-min", type=float, default=DEFAULT_ADD_ON_SCORE_MIN)
    parser.add_argument(
        "--add-on-breakout-extension-bps",
        type=float,
        default=DEFAULT_ADD_ON_BREAKOUT_EXTENSION_BPS,
    )
    parser.add_argument(
        "--add-on-flow-multiplier",
        type=float,
        default=DEFAULT_ADD_ON_FLOW_MULTIPLIER,
    )
    parser.add_argument(
        "--add-on-book-multiplier",
        type=float,
        default=DEFAULT_ADD_ON_BOOK_MULTIPLIER,
    )
    parser.add_argument("--add-on-min-seconds", type=float, default=DEFAULT_ADD_ON_MIN_SECONDS)
    parser.add_argument("--max-reductions", type=int, default=DEFAULT_MAX_REDUCTIONS)
    parser.add_argument("--de-risk-fraction", type=float, default=DEFAULT_DE_RISK_FRACTION)
    parser.add_argument(
        "--de-risk-score-threshold",
        type=float,
        default=DEFAULT_DE_RISK_SCORE_THRESHOLD,
    )
    parser.add_argument(
        "--de-risk-confirm-ratio",
        type=float,
        default=DEFAULT_DE_RISK_CONFIRM_RATIO,
    )
    parser.add_argument(
        "--de-risk-flow-multiplier",
        type=float,
        default=DEFAULT_DE_RISK_FLOW_MULTIPLIER,
    )
    parser.add_argument(
        "--runner-core-fraction",
        type=float,
        default=DEFAULT_RUNNER_CORE_FRACTION,
    )
    parser.add_argument(
        "--failure-hold-seconds",
        type=float,
        default=DEFAULT_FAILURE_HOLD_SECONDS,
    )
    parser.add_argument(
        "--failure-min-followthrough-bps",
        type=float,
        default=DEFAULT_FAILURE_MIN_FOLLOWTHROUGH_BPS,
    )
    parser.add_argument(
        "--flow-flip-exit-imbalance",
        type=float,
        default=DEFAULT_FLOW_FLIP_EXIT_IMBALANCE,
    )
    parser.add_argument(
        "--book-flip-exit-imbalance",
        type=float,
        default=DEFAULT_BOOK_FLIP_EXIT_IMBALANCE,
    )
    parser.add_argument(
        "--breakout-fail-buffer-bps",
        type=float,
        default=DEFAULT_BREAKOUT_FAIL_BUFFER_BPS,
    )
    parser.add_argument("--max-hold-seconds", type=float, default=DEFAULT_MAX_HOLD_SECONDS)
    parser.add_argument("--time-stop-min-r", type=float, default=DEFAULT_TIME_STOP_MIN_R)
    parser.add_argument(
        "--stop-confirmation-ticks",
        type=int,
        default=DEFAULT_STOP_CONFIRMATION_TICKS,
    )
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    parser.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    parser.add_argument("--equity-risk-pct", type=float, default=DEFAULT_EQUITY_RISK_PCT)
    parser.add_argument("--cooldown-seconds", type=float, default=DEFAULT_COOLDOWN_SECONDS)
    parser.add_argument("--max-daily-loss", type=float, default=DEFAULT_MAX_DAILY_LOSS)
    parser.add_argument("--max-trades-per-day", type=int, default=DEFAULT_MAX_TRADES_PER_DAY)
    parser.add_argument("--max-trades-per-hour", type=int, default=DEFAULT_MAX_TRADES_PER_HOUR)
    parser.add_argument("--min-hold-seconds", type=float, default=DEFAULT_MIN_HOLD_SECONDS)
    parser.add_argument("--events-jsonl", default=DEFAULT_EVENTS_JSONL)
    parser.add_argument("--samples-jsonl", default=DEFAULT_SAMPLES_JSONL)
    parser.add_argument("--trades-csv", default=DEFAULT_TRADES_CSV)
    parser.add_argument("--markouts-jsonl", default=DEFAULT_MARKOUTS_JSONL)
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR)
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    dex = args.dex
    if not dex and ":" in args.asset:
        dex = args.asset.split(":", 1)[0]

    bot = MidFrequencyMomentumBot(
        asset=args.asset,
        account_balance=args.account_balance,
        api_url=args.api_url,
        dex=dex,
        leverage=args.leverage,
        sample_ms=args.sample_ms,
        warm_start_candles=args.warm_start_candles,
        startup_state_entry=args.startup_state_entry,
        entry_confirmation_samples=args.entry_confirmation_samples,
        fast_impulse_window_seconds=args.fast_impulse_window_seconds,
        confirm_impulse_window_seconds=args.confirm_impulse_window_seconds,
        breakout_lookback_seconds=args.breakout_lookback_seconds,
        volatility_lookback_seconds=args.volatility_lookback_seconds,
        flow_window_seconds=args.flow_window_seconds,
        min_fast_impulse_bps=args.min_fast_impulse_bps,
        min_confirm_impulse_bps=args.min_confirm_impulse_bps,
        fast_vol_multiplier=args.fast_vol_multiplier,
        confirm_vol_multiplier=args.confirm_vol_multiplier,
        breakout_buffer_bps=args.breakout_buffer_bps,
        flow_imbalance_min=args.flow_imbalance_min,
        book_imbalance_min=args.book_imbalance_min,
        min_trade_count=args.min_trade_count,
        depth_levels=args.depth_levels,
        max_spread_bps=args.max_spread_bps,
        max_quote_age_ms=args.max_quote_age_ms,
        quote_gap_warn_ms=args.quote_gap_warn_ms,
        reconnect_gap_ms=args.reconnect_gap_ms,
        extreme_impulse_bps=args.extreme_impulse_bps,
        extreme_vol_bps=args.extreme_vol_bps,
        setup_score_min=args.setup_score_min,
        tactical_score_min=args.tactical_score_min,
        tactical_fast_threshold_ratio=args.tactical_fast_threshold_ratio,
        tactical_confirm_threshold_ratio=args.tactical_confirm_threshold_ratio,
        tactical_breakout_slack_bps=args.tactical_breakout_slack_bps,
        tactical_flow_multiplier=args.tactical_flow_multiplier,
        tactical_book_multiplier=args.tactical_book_multiplier,
        score_edge_min=args.score_edge_min,
        instant_entry_score_min=args.instant_entry_score_min,
        initial_stop_min_bps=args.initial_stop_min_bps,
        initial_stop_vol_multiplier=args.initial_stop_vol_multiplier,
        trail_min_bps=args.trail_min_bps,
        trail_vol_multiplier=args.trail_vol_multiplier,
        trail_arm_r=args.trail_arm_r,
        break_even_lock_r=args.break_even_lock_r,
        score_notional_boost=args.score_notional_boost,
        initial_notional_fraction=args.initial_notional_fraction,
        max_notional_fraction=args.max_notional_fraction,
        max_add_ons=args.max_add_ons,
        add_on_trigger_r=args.add_on_trigger_r,
        add_on_fraction=args.add_on_fraction,
        add_on_score_min=args.add_on_score_min,
        add_on_breakout_extension_bps=args.add_on_breakout_extension_bps,
        add_on_flow_multiplier=args.add_on_flow_multiplier,
        add_on_book_multiplier=args.add_on_book_multiplier,
        add_on_min_seconds=args.add_on_min_seconds,
        max_reductions=args.max_reductions,
        de_risk_fraction=args.de_risk_fraction,
        de_risk_score_threshold=args.de_risk_score_threshold,
        de_risk_confirm_ratio=args.de_risk_confirm_ratio,
        de_risk_flow_multiplier=args.de_risk_flow_multiplier,
        runner_core_fraction=args.runner_core_fraction,
        failure_hold_seconds=args.failure_hold_seconds,
        failure_min_followthrough_bps=args.failure_min_followthrough_bps,
        flow_flip_exit_imbalance=args.flow_flip_exit_imbalance,
        book_flip_exit_imbalance=args.book_flip_exit_imbalance,
        breakout_fail_buffer_bps=args.breakout_fail_buffer_bps,
        max_hold_seconds=args.max_hold_seconds,
        time_stop_min_r=args.time_stop_min_r,
        stop_confirmation_ticks=args.stop_confirmation_ticks,
        slippage_bps=args.slippage_bps,
        fee_bps=args.fee_bps,
        equity_risk_pct=args.equity_risk_pct,
        cooldown_seconds=args.cooldown_seconds,
        max_daily_loss=args.max_daily_loss,
        max_trades_per_day=args.max_trades_per_day,
        max_trades_per_hour=args.max_trades_per_hour,
        min_hold_seconds=args.min_hold_seconds,
        events_jsonl_path=args.events_jsonl,
        samples_jsonl_path=args.samples_jsonl,
        trades_csv_path=args.trades_csv,
        markouts_jsonl_path=args.markouts_jsonl,
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

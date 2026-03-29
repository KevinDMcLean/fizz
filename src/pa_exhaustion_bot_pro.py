#!/usr/bin/env python3
"""Paper-trading exhaustion and reversal engine for overstretched moves."""

from __future__ import annotations

import argparse
import logging

from pa_exhaustion_pro_core import ProExhaustionReversalBot

try:
    from hyperliquid.utils import constants
except ImportError:
    constants = None  # type: ignore[assignment]


DEFAULT_ASSET = "xyz:BRENTOIL"
DEFAULT_ACCOUNT_BALANCE = 1000.0
DEFAULT_LEVERAGE = 20
DEFAULT_SAMPLE_MS = 150
DEFAULT_WARM_START_CANDLES = True
DEFAULT_STARTUP_STATE_ENTRY = True
DEFAULT_ENTRY_CONFIRMATION_SAMPLES = 2
DEFAULT_FAST_IMPULSE_WINDOW_SECONDS = 6.0
DEFAULT_CONFIRM_IMPULSE_WINDOW_SECONDS = 18.0
DEFAULT_BREAKOUT_LOOKBACK_SECONDS = 90.0
DEFAULT_VOLATILITY_LOOKBACK_SECONDS = 180.0
DEFAULT_FLOW_WINDOW_SECONDS = 12.0
DEFAULT_MIN_FAST_EXHAUSTION_BPS = 9.0
DEFAULT_MIN_CONFIRM_EXHAUSTION_BPS = 16.0
DEFAULT_FAST_VOL_MULTIPLIER = 1.0
DEFAULT_CONFIRM_VOL_MULTIPLIER = 1.35
DEFAULT_EXHAUSTION_FLOW_MIN = 0.24
DEFAULT_EXHAUSTION_BOOK_MIN = 0.18
DEFAULT_REVERSAL_FLOW_MIN = 0.06
DEFAULT_REVERSAL_BOOK_MIN = 0.03
DEFAULT_REBOUND_CONFIRM_BPS = 5.0
DEFAULT_REVERSAL_WINDOW_SECONDS = 25.0
DEFAULT_MIN_TRADE_COUNT = 4
DEFAULT_DEPTH_LEVELS = 3
DEFAULT_MAX_SPREAD_BPS = 8.0
DEFAULT_MAX_QUOTE_AGE_MS = 1000
DEFAULT_QUOTE_GAP_WARN_MS = 2500
DEFAULT_RECONNECT_GAP_MS = 30000
DEFAULT_SETUP_SCORE_MIN = 58.0
DEFAULT_INITIAL_STOP_MIN_BPS = 8.0
DEFAULT_INITIAL_STOP_VOL_MULTIPLIER = 1.15
DEFAULT_STOP_ANCHOR_BUFFER_BPS = 2.0
DEFAULT_TAKE_PROFIT_R = 1.35
DEFAULT_MIN_TAKE_PROFIT_BPS = 8.0
DEFAULT_TRAIL_MIN_BPS = 7.0
DEFAULT_TRAIL_VOL_MULTIPLIER = 1.20
DEFAULT_TRAIL_ARM_R = 0.80
DEFAULT_BREAK_EVEN_LOCK_R = 0.85
DEFAULT_FAILURE_HOLD_SECONDS = 8.0
DEFAULT_FAILURE_MIN_FOLLOWTHROUGH_BPS = 4.0
DEFAULT_RETEST_EXTREME_BUFFER_BPS = 1.0
DEFAULT_ADVERSE_FLOW_EXIT_IMBALANCE = 0.14
DEFAULT_ADVERSE_BOOK_EXIT_IMBALANCE = 0.10
DEFAULT_MAX_HOLD_SECONDS = 90.0
DEFAULT_STOP_CONFIRMATION_TICKS = 2
DEFAULT_SLIPPAGE_BPS = 0.0
DEFAULT_FEE_BPS = 0.0
DEFAULT_EQUITY_RISK_PCT = 0.65
DEFAULT_COOLDOWN_SECONDS = 25.0
DEFAULT_MAX_DAILY_LOSS = 30.0
DEFAULT_MAX_TRADES_PER_DAY = 36
DEFAULT_MAX_TRADES_PER_HOUR = 14
DEFAULT_MIN_HOLD_SECONDS = 0.0
DEFAULT_EVENTS_JSONL = "logs/exhaustion_pro_events.jsonl"
DEFAULT_SAMPLES_JSONL = "logs/exhaustion_pro_samples.jsonl"
DEFAULT_TRADES_CSV = "logs/exhaustion_pro_trades.csv"
DEFAULT_MARKOUTS_JSONL = "logs/exhaustion_pro_markouts.jsonl"
DEFAULT_REPORT_DIR = "logs/exhaustion_pro_reports"
DEFAULT_API_URL = (
    constants.MAINNET_API_URL if constants is not None else "https://api.hyperliquid.xyz"
)
DEFAULT_DEX = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Paper-trading exhaustion and reversal engine for Hyperliquid",
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
    parser.add_argument("--entry-confirmation-samples", type=int, default=DEFAULT_ENTRY_CONFIRMATION_SAMPLES)
    parser.add_argument("--fast-impulse-window-seconds", type=float, default=DEFAULT_FAST_IMPULSE_WINDOW_SECONDS)
    parser.add_argument("--confirm-impulse-window-seconds", type=float, default=DEFAULT_CONFIRM_IMPULSE_WINDOW_SECONDS)
    parser.add_argument("--breakout-lookback-seconds", type=float, default=DEFAULT_BREAKOUT_LOOKBACK_SECONDS)
    parser.add_argument("--volatility-lookback-seconds", type=float, default=DEFAULT_VOLATILITY_LOOKBACK_SECONDS)
    parser.add_argument("--flow-window-seconds", type=float, default=DEFAULT_FLOW_WINDOW_SECONDS)
    parser.add_argument("--min-fast-exhaustion-bps", type=float, default=DEFAULT_MIN_FAST_EXHAUSTION_BPS)
    parser.add_argument("--min-confirm-exhaustion-bps", type=float, default=DEFAULT_MIN_CONFIRM_EXHAUSTION_BPS)
    parser.add_argument("--fast-vol-multiplier", type=float, default=DEFAULT_FAST_VOL_MULTIPLIER)
    parser.add_argument("--confirm-vol-multiplier", type=float, default=DEFAULT_CONFIRM_VOL_MULTIPLIER)
    parser.add_argument("--exhaustion-flow-min", type=float, default=DEFAULT_EXHAUSTION_FLOW_MIN)
    parser.add_argument("--exhaustion-book-min", type=float, default=DEFAULT_EXHAUSTION_BOOK_MIN)
    parser.add_argument("--reversal-flow-min", type=float, default=DEFAULT_REVERSAL_FLOW_MIN)
    parser.add_argument("--reversal-book-min", type=float, default=DEFAULT_REVERSAL_BOOK_MIN)
    parser.add_argument("--rebound-confirm-bps", type=float, default=DEFAULT_REBOUND_CONFIRM_BPS)
    parser.add_argument("--reversal-window-seconds", type=float, default=DEFAULT_REVERSAL_WINDOW_SECONDS)
    parser.add_argument("--min-trade-count", type=int, default=DEFAULT_MIN_TRADE_COUNT)
    parser.add_argument("--depth-levels", type=int, default=DEFAULT_DEPTH_LEVELS)
    parser.add_argument("--max-spread-bps", type=float, default=DEFAULT_MAX_SPREAD_BPS)
    parser.add_argument("--max-quote-age-ms", type=int, default=DEFAULT_MAX_QUOTE_AGE_MS)
    parser.add_argument("--quote-gap-warn-ms", type=int, default=DEFAULT_QUOTE_GAP_WARN_MS)
    parser.add_argument("--reconnect-gap-ms", type=int, default=DEFAULT_RECONNECT_GAP_MS)
    parser.add_argument("--setup-score-min", type=float, default=DEFAULT_SETUP_SCORE_MIN)
    parser.add_argument("--initial-stop-min-bps", type=float, default=DEFAULT_INITIAL_STOP_MIN_BPS)
    parser.add_argument("--initial-stop-vol-multiplier", type=float, default=DEFAULT_INITIAL_STOP_VOL_MULTIPLIER)
    parser.add_argument("--stop-anchor-buffer-bps", type=float, default=DEFAULT_STOP_ANCHOR_BUFFER_BPS)
    parser.add_argument("--take-profit-r", type=float, default=DEFAULT_TAKE_PROFIT_R)
    parser.add_argument("--min-take-profit-bps", type=float, default=DEFAULT_MIN_TAKE_PROFIT_BPS)
    parser.add_argument("--trail-min-bps", type=float, default=DEFAULT_TRAIL_MIN_BPS)
    parser.add_argument("--trail-vol-multiplier", type=float, default=DEFAULT_TRAIL_VOL_MULTIPLIER)
    parser.add_argument("--trail-arm-r", type=float, default=DEFAULT_TRAIL_ARM_R)
    parser.add_argument("--break-even-lock-r", type=float, default=DEFAULT_BREAK_EVEN_LOCK_R)
    parser.add_argument("--failure-hold-seconds", type=float, default=DEFAULT_FAILURE_HOLD_SECONDS)
    parser.add_argument("--failure-min-followthrough-bps", type=float, default=DEFAULT_FAILURE_MIN_FOLLOWTHROUGH_BPS)
    parser.add_argument("--retest-extreme-buffer-bps", type=float, default=DEFAULT_RETEST_EXTREME_BUFFER_BPS)
    parser.add_argument("--adverse-flow-exit-imbalance", type=float, default=DEFAULT_ADVERSE_FLOW_EXIT_IMBALANCE)
    parser.add_argument("--adverse-book-exit-imbalance", type=float, default=DEFAULT_ADVERSE_BOOK_EXIT_IMBALANCE)
    parser.add_argument("--max-hold-seconds", type=float, default=DEFAULT_MAX_HOLD_SECONDS)
    parser.add_argument("--stop-confirmation-ticks", type=int, default=DEFAULT_STOP_CONFIRMATION_TICKS)
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

    bot = ProExhaustionReversalBot(
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
        min_fast_exhaustion_bps=args.min_fast_exhaustion_bps,
        min_confirm_exhaustion_bps=args.min_confirm_exhaustion_bps,
        fast_vol_multiplier=args.fast_vol_multiplier,
        confirm_vol_multiplier=args.confirm_vol_multiplier,
        exhaustion_flow_min=args.exhaustion_flow_min,
        exhaustion_book_min=args.exhaustion_book_min,
        reversal_flow_min=args.reversal_flow_min,
        reversal_book_min=args.reversal_book_min,
        rebound_confirm_bps=args.rebound_confirm_bps,
        reversal_window_seconds=args.reversal_window_seconds,
        min_trade_count=args.min_trade_count,
        depth_levels=args.depth_levels,
        max_spread_bps=args.max_spread_bps,
        max_quote_age_ms=args.max_quote_age_ms,
        quote_gap_warn_ms=args.quote_gap_warn_ms,
        reconnect_gap_ms=args.reconnect_gap_ms,
        setup_score_min=args.setup_score_min,
        initial_stop_min_bps=args.initial_stop_min_bps,
        initial_stop_vol_multiplier=args.initial_stop_vol_multiplier,
        stop_anchor_buffer_bps=args.stop_anchor_buffer_bps,
        take_profit_r=args.take_profit_r,
        min_take_profit_bps=args.min_take_profit_bps,
        trail_min_bps=args.trail_min_bps,
        trail_vol_multiplier=args.trail_vol_multiplier,
        trail_arm_r=args.trail_arm_r,
        break_even_lock_r=args.break_even_lock_r,
        failure_hold_seconds=args.failure_hold_seconds,
        failure_min_followthrough_bps=args.failure_min_followthrough_bps,
        retest_extreme_buffer_bps=args.retest_extreme_buffer_bps,
        adverse_flow_exit_imbalance=args.adverse_flow_exit_imbalance,
        adverse_book_exit_imbalance=args.adverse_book_exit_imbalance,
        max_hold_seconds=args.max_hold_seconds,
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

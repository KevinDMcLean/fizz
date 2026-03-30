#!/usr/bin/env python3
"""Paper-trading event campaign runner built from the JG initiator-follower thesis."""

from __future__ import annotations

import argparse
import logging

from initiator_follower_jg_thesis_core import InitiatorFollowerJGThesisBot
from pa_momo_mid_bot_pro import (
    DEFAULT_ACCOUNT_BALANCE,
    DEFAULT_ADD_ON_BOOK_MULTIPLIER,
    DEFAULT_ADD_ON_BREAKOUT_EXTENSION_BPS,
    DEFAULT_ADD_ON_FLOW_MULTIPLIER,
    DEFAULT_ADD_ON_FRACTION,
    DEFAULT_ADD_ON_MIN_SECONDS,
    DEFAULT_ADD_ON_SCORE_MIN,
    DEFAULT_API_URL,
    DEFAULT_ASSET,
    DEFAULT_BOOK_FLIP_EXIT_IMBALANCE,
    DEFAULT_BOOK_IMBALANCE_MIN,
    DEFAULT_BREAKOUT_LOOKBACK_SECONDS,
    DEFAULT_CONFIRM_IMPULSE_WINDOW_SECONDS,
    DEFAULT_CONFIRM_VOL_MULTIPLIER,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_DEX,
    DEFAULT_DEPTH_LEVELS,
    DEFAULT_DE_RISK_CONFIRM_RATIO,
    DEFAULT_DE_RISK_FLOW_MULTIPLIER,
    DEFAULT_DE_RISK_FRACTION,
    DEFAULT_DE_RISK_SCORE_THRESHOLD,
    DEFAULT_ENTRY_CONFIRMATION_SAMPLES,
    DEFAULT_EQUITY_RISK_PCT,
    DEFAULT_EVENTS_JSONL,
    DEFAULT_FAST_IMPULSE_WINDOW_SECONDS,
    DEFAULT_FAST_VOL_MULTIPLIER,
    DEFAULT_FEE_BPS,
    DEFAULT_FLOW_FLIP_EXIT_IMBALANCE,
    DEFAULT_FLOW_IMBALANCE_MIN,
    DEFAULT_FLOW_WINDOW_SECONDS,
    DEFAULT_INITIAL_STOP_MIN_BPS,
    DEFAULT_INITIAL_STOP_VOL_MULTIPLIER,
    DEFAULT_LEVERAGE,
    DEFAULT_MARKOUTS_JSONL,
    DEFAULT_MAX_ADD_ONS,
    DEFAULT_MAX_DAILY_LOSS,
    DEFAULT_MAX_HOLD_SECONDS,
    DEFAULT_MAX_QUOTE_AGE_MS,
    DEFAULT_MAX_REDUCTIONS,
    DEFAULT_MAX_SPREAD_BPS,
    DEFAULT_MAX_TRADES_PER_DAY,
    DEFAULT_MAX_TRADES_PER_HOUR,
    DEFAULT_MIN_HOLD_SECONDS,
    DEFAULT_MIN_TRADE_COUNT,
    DEFAULT_QUOTE_GAP_WARN_MS,
    DEFAULT_RECONNECT_GAP_MS,
    DEFAULT_REPORT_DIR,
    DEFAULT_RUNNER_CORE_FRACTION,
    DEFAULT_SAMPLE_MS,
    DEFAULT_SCORE_NOTIONAL_BOOST,
    DEFAULT_SETUP_SCORE_MIN,
    DEFAULT_SAMPLES_JSONL,
    DEFAULT_SLIPPAGE_BPS,
    DEFAULT_STARTUP_STATE_ENTRY,
    DEFAULT_STOP_CONFIRMATION_TICKS,
    DEFAULT_TRAIL_ARM_R,
    DEFAULT_TRAIL_MIN_BPS,
    DEFAULT_TRAIL_VOL_MULTIPLIER,
    DEFAULT_TRADES_CSV,
    DEFAULT_TIME_STOP_MIN_R,
    DEFAULT_WARM_START_CANDLES,
)

DEFAULT_MIN_FAST_IMPULSE_BPS = 5.5
DEFAULT_MIN_CONFIRM_IMPULSE_BPS = 11.0
DEFAULT_BREAKOUT_BUFFER_BPS = 0.35
DEFAULT_PROBE_SCORE_MIN = 58.0
DEFAULT_PROBE_SCORE_EDGE_MIN = 10.0
DEFAULT_PROBE_FAST_THRESHOLD_RATIO = 0.72
DEFAULT_PROBE_CONFIRM_THRESHOLD_RATIO = 0.80
DEFAULT_PROBE_BREAKOUT_SLACK_BPS = 0.12
DEFAULT_PROBE_FLOW_MULTIPLIER = 1.00
DEFAULT_PROBE_BOOK_MULTIPLIER = 1.00
DEFAULT_CAMPAIGN_SCORE_MIN = 78.0
DEFAULT_CAMPAIGN_SCORE_EDGE_MIN = 18.0
DEFAULT_EVENT_IMPULSE_BPS = 18.0
DEFAULT_EVENT_VOL_BPS = 15.0
DEFAULT_LOSS_RISK_SPREAD_RATIO = 1.10
DEFAULT_LOSS_RISK_QUOTE_AGE_RATIO = 0.95
DEFAULT_MIN_TRADE_RATE_PER_SECOND = 0.40
DEFAULT_PROBE_NOTIONAL_FRACTION = 0.10
DEFAULT_CAMPAIGN_ENTRY_NOTIONAL_FRACTION = 0.45
DEFAULT_MAX_NOTIONAL_FRACTION = 0.90
DEFAULT_PROBE_SIZE_BOOST = 0.04
DEFAULT_CAMPAIGN_SIZE_BOOST = 0.35
DEFAULT_CAMPAIGN_ADD_ON_TRIGGER_R = 0.55
DEFAULT_ADD_ON_TRIGGER_R = DEFAULT_CAMPAIGN_ADD_ON_TRIGGER_R
DEFAULT_ADD_ON_FRACTION = 0.15
DEFAULT_PROBE_FAILURE_HOLD_SECONDS = 4.0
DEFAULT_PROBE_FAILURE_MIN_FOLLOWTHROUGH_BPS = 1.8
DEFAULT_CAMPAIGN_FAILURE_HOLD_SECONDS = 14.0
DEFAULT_CAMPAIGN_FAILURE_MIN_FOLLOWTHROUGH_BPS = 10.0
DEFAULT_MAX_PROBE_LOSSES_PER_SIDE = 3
DEFAULT_MAX_GLOBAL_PROBE_LOSSES = 5
DEFAULT_PROBE_LOSS_WINDOW_SECONDS = 900.0
DEFAULT_PROBE_DEPTH_SHARE_LIMIT = 0.35
DEFAULT_CAMPAIGN_DEPTH_SHARE_LIMIT = 0.85
DEFAULT_ADD_ON_DEPTH_SHARE_LIMIT = 0.60
DEFAULT_CAMPAIGN_PRE_UNLOCK_FRACTION = 0.72
DEFAULT_DE_RISK_BOOK_MULTIPLIER = 0.70
DEFAULT_DE_RISK_BREAKOUT_RECLAIM_BPS = 0.08
DEFAULT_CAMPAIGN_RE_ADD_UNLOCK_R = 1.15
DEFAULT_HARD_REVERSAL_CONFIRM_RATIO = 0.85
DEFAULT_HARD_REVERSAL_FLOW_MULTIPLIER = 1.25
DEFAULT_HARD_REVERSAL_BOOK_MULTIPLIER = 1.00
DEFAULT_HARD_REVERSAL_MAX_PROFIT_R = 0.50


try:
    from hyperliquid.utils import constants
except ImportError:
    constants = None  # type: ignore[assignment]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Paper-trading event campaign engine for Hyperliquid",
    )
    parser.add_argument("--asset", default=DEFAULT_ASSET)
    parser.add_argument("--account-balance", type=float, default=DEFAULT_ACCOUNT_BALANCE)
    parser.add_argument("--api-url", default=constants.MAINNET_API_URL if constants is not None else DEFAULT_API_URL)
    parser.add_argument(
        "--dex",
        default=DEFAULT_DEX,
        help="Hyperliquid dex namespace. Leave empty to auto-infer from asset prefix.",
    )
    parser.add_argument("--leverage", type=int, default=DEFAULT_LEVERAGE)
    parser.add_argument("--sample-ms", type=int, default=DEFAULT_SAMPLE_MS)
    parser.add_argument("--warm-start-candles", action=argparse.BooleanOptionalAction, default=DEFAULT_WARM_START_CANDLES)
    parser.add_argument("--startup-state-entry", action=argparse.BooleanOptionalAction, default=DEFAULT_STARTUP_STATE_ENTRY)
    parser.add_argument("--entry-confirmation-samples", type=int, default=DEFAULT_ENTRY_CONFIRMATION_SAMPLES)
    parser.add_argument("--fast-impulse-window-seconds", type=float, default=DEFAULT_FAST_IMPULSE_WINDOW_SECONDS)
    parser.add_argument("--confirm-impulse-window-seconds", type=float, default=DEFAULT_CONFIRM_IMPULSE_WINDOW_SECONDS)
    parser.add_argument("--breakout-lookback-seconds", type=float, default=DEFAULT_BREAKOUT_LOOKBACK_SECONDS)
    parser.add_argument("--volatility-lookback-seconds", type=float, default=DEFAULT_CONFIRM_IMPULSE_WINDOW_SECONDS * 10.0)
    parser.add_argument("--flow-window-seconds", type=float, default=DEFAULT_FLOW_WINDOW_SECONDS)
    parser.add_argument("--min-fast-impulse-bps", type=float, default=DEFAULT_MIN_FAST_IMPULSE_BPS)
    parser.add_argument("--min-confirm-impulse-bps", type=float, default=DEFAULT_MIN_CONFIRM_IMPULSE_BPS)
    parser.add_argument("--fast-vol-multiplier", type=float, default=DEFAULT_FAST_VOL_MULTIPLIER)
    parser.add_argument("--confirm-vol-multiplier", type=float, default=DEFAULT_CONFIRM_VOL_MULTIPLIER)
    parser.add_argument("--breakout-buffer-bps", type=float, default=DEFAULT_BREAKOUT_BUFFER_BPS)
    parser.add_argument("--flow-imbalance-min", type=float, default=DEFAULT_FLOW_IMBALANCE_MIN)
    parser.add_argument("--book-imbalance-min", type=float, default=DEFAULT_BOOK_IMBALANCE_MIN)
    parser.add_argument("--min-trade-count", type=int, default=DEFAULT_MIN_TRADE_COUNT)
    parser.add_argument("--depth-levels", type=int, default=DEFAULT_DEPTH_LEVELS)
    parser.add_argument("--max-spread-bps", type=float, default=DEFAULT_MAX_SPREAD_BPS)
    parser.add_argument("--max-quote-age-ms", type=int, default=DEFAULT_MAX_QUOTE_AGE_MS)
    parser.add_argument("--quote-gap-warn-ms", type=int, default=DEFAULT_QUOTE_GAP_WARN_MS)
    parser.add_argument("--reconnect-gap-ms", type=int, default=DEFAULT_RECONNECT_GAP_MS)
    parser.add_argument("--extreme-impulse-bps", type=float, default=9_999.0)
    parser.add_argument("--extreme-vol-bps", type=float, default=9_999.0)
    parser.add_argument("--setup-score-min", type=float, default=DEFAULT_SETUP_SCORE_MIN)
    parser.add_argument("--tactical-score-min", type=float, default=DEFAULT_PROBE_SCORE_MIN)
    parser.add_argument("--tactical-fast-threshold-ratio", type=float, default=DEFAULT_PROBE_FAST_THRESHOLD_RATIO)
    parser.add_argument("--tactical-confirm-threshold-ratio", type=float, default=DEFAULT_PROBE_CONFIRM_THRESHOLD_RATIO)
    parser.add_argument("--tactical-breakout-slack-bps", type=float, default=DEFAULT_PROBE_BREAKOUT_SLACK_BPS)
    parser.add_argument("--tactical-flow-multiplier", type=float, default=DEFAULT_PROBE_FLOW_MULTIPLIER)
    parser.add_argument("--tactical-book-multiplier", type=float, default=DEFAULT_PROBE_BOOK_MULTIPLIER)
    parser.add_argument("--score-edge-min", type=float, default=DEFAULT_PROBE_SCORE_EDGE_MIN)
    parser.add_argument("--instant-entry-score-min", type=float, default=98.0)
    parser.add_argument("--initial-stop-min-bps", type=float, default=DEFAULT_INITIAL_STOP_MIN_BPS)
    parser.add_argument("--initial-stop-vol-multiplier", type=float, default=DEFAULT_INITIAL_STOP_VOL_MULTIPLIER)
    parser.add_argument("--trail-min-bps", type=float, default=DEFAULT_TRAIL_MIN_BPS)
    parser.add_argument("--trail-vol-multiplier", type=float, default=DEFAULT_TRAIL_VOL_MULTIPLIER)
    parser.add_argument("--trail-arm-r", type=float, default=DEFAULT_TRAIL_ARM_R)
    parser.add_argument("--break-even-lock-r", type=float, default=1.05)
    parser.add_argument("--score-notional-boost", type=float, default=DEFAULT_SCORE_NOTIONAL_BOOST)
    parser.add_argument("--initial-notional-fraction", type=float, default=DEFAULT_PROBE_NOTIONAL_FRACTION)
    parser.add_argument("--max-notional-fraction", type=float, default=DEFAULT_MAX_NOTIONAL_FRACTION)
    parser.add_argument("--max-add-ons", type=int, default=DEFAULT_MAX_ADD_ONS)
    parser.add_argument("--add-on-trigger-r", type=float, default=DEFAULT_ADD_ON_TRIGGER_R)
    parser.add_argument("--add-on-fraction", type=float, default=DEFAULT_ADD_ON_FRACTION)
    parser.add_argument("--add-on-score-min", type=float, default=DEFAULT_CAMPAIGN_SCORE_MIN)
    parser.add_argument("--add-on-breakout-extension-bps", type=float, default=DEFAULT_ADD_ON_BOOK_MULTIPLIER * 0.4)
    parser.add_argument("--add-on-flow-multiplier", type=float, default=DEFAULT_ADD_ON_FLOW_MULTIPLIER)
    parser.add_argument("--add-on-book-multiplier", type=float, default=DEFAULT_ADD_ON_BOOK_MULTIPLIER)
    parser.add_argument("--add-on-min-seconds", type=float, default=DEFAULT_ADD_ON_MIN_SECONDS)
    parser.add_argument("--max-reductions", type=int, default=DEFAULT_MAX_REDUCTIONS)
    parser.add_argument("--de-risk-fraction", type=float, default=0.22)
    parser.add_argument("--de-risk-score-threshold", type=float, default=68.0)
    parser.add_argument("--de-risk-confirm-ratio", type=float, default=0.60)
    parser.add_argument("--de-risk-flow-multiplier", type=float, default=0.80)
    parser.add_argument("--runner-core-fraction", type=float, default=0.45)
    parser.add_argument("--failure-hold-seconds", type=float, default=DEFAULT_CAMPAIGN_FAILURE_HOLD_SECONDS)
    parser.add_argument("--failure-min-followthrough-bps", type=float, default=DEFAULT_CAMPAIGN_FAILURE_MIN_FOLLOWTHROUGH_BPS)
    parser.add_argument("--flow-flip-exit-imbalance", type=float, default=DEFAULT_FLOW_FLIP_EXIT_IMBALANCE)
    parser.add_argument("--book-flip-exit-imbalance", type=float, default=DEFAULT_BOOK_FLIP_EXIT_IMBALANCE)
    parser.add_argument("--breakout-fail-buffer-bps", type=float, default=0.95)
    parser.add_argument("--max-hold-seconds", type=float, default=DEFAULT_MAX_HOLD_SECONDS)
    parser.add_argument("--time-stop-min-r", type=float, default=DEFAULT_TIME_STOP_MIN_R)
    parser.add_argument("--stop-confirmation-ticks", type=int, default=DEFAULT_STOP_CONFIRMATION_TICKS)
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    parser.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    parser.add_argument("--equity-risk-pct", type=float, default=1.2)
    parser.add_argument("--cooldown-seconds", type=float, default=DEFAULT_COOLDOWN_SECONDS)
    parser.add_argument("--max-daily-loss", type=float, default=180.0)
    parser.add_argument("--max-trades-per-day", type=int, default=DEFAULT_MAX_TRADES_PER_DAY)
    parser.add_argument("--max-trades-per-hour", type=int, default=DEFAULT_MAX_TRADES_PER_HOUR)
    parser.add_argument("--min-hold-seconds", type=float, default=DEFAULT_MIN_HOLD_SECONDS)
    parser.add_argument("--events-jsonl", default=DEFAULT_EVENTS_JSONL)
    parser.add_argument("--samples-jsonl", default=DEFAULT_SAMPLES_JSONL)
    parser.add_argument("--trades-csv", default=DEFAULT_TRADES_CSV)
    parser.add_argument("--markouts-jsonl", default=DEFAULT_MARKOUTS_JSONL)
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR)
    parser.add_argument("--probe-score-min", type=float, default=DEFAULT_PROBE_SCORE_MIN)
    parser.add_argument("--probe-score-edge-min", type=float, default=DEFAULT_PROBE_SCORE_EDGE_MIN)
    parser.add_argument("--probe-fast-threshold-ratio", type=float, default=DEFAULT_PROBE_FAST_THRESHOLD_RATIO)
    parser.add_argument("--probe-confirm-threshold-ratio", type=float, default=DEFAULT_PROBE_CONFIRM_THRESHOLD_RATIO)
    parser.add_argument("--probe-breakout-slack-bps", type=float, default=DEFAULT_PROBE_BREAKOUT_SLACK_BPS)
    parser.add_argument("--probe-flow-multiplier", type=float, default=DEFAULT_PROBE_FLOW_MULTIPLIER)
    parser.add_argument("--probe-book-multiplier", type=float, default=DEFAULT_PROBE_BOOK_MULTIPLIER)
    parser.add_argument("--campaign-score-min", type=float, default=DEFAULT_CAMPAIGN_SCORE_MIN)
    parser.add_argument("--campaign-score-edge-min", type=float, default=DEFAULT_CAMPAIGN_SCORE_EDGE_MIN)
    parser.add_argument("--event-impulse-bps", type=float, default=DEFAULT_EVENT_IMPULSE_BPS)
    parser.add_argument("--event-vol-bps", type=float, default=DEFAULT_EVENT_VOL_BPS)
    parser.add_argument("--loss-risk-spread-ratio", type=float, default=DEFAULT_LOSS_RISK_SPREAD_RATIO)
    parser.add_argument("--loss-risk-quote-age-ratio", type=float, default=DEFAULT_LOSS_RISK_QUOTE_AGE_RATIO)
    parser.add_argument("--min-trade-rate-per-second", type=float, default=DEFAULT_MIN_TRADE_RATE_PER_SECOND)
    parser.add_argument("--probe-notional-fraction", type=float, default=DEFAULT_PROBE_NOTIONAL_FRACTION)
    parser.add_argument("--campaign-entry-notional-fraction", type=float, default=DEFAULT_CAMPAIGN_ENTRY_NOTIONAL_FRACTION)
    parser.add_argument("--probe-size-boost", type=float, default=DEFAULT_PROBE_SIZE_BOOST)
    parser.add_argument("--campaign-size-boost", type=float, default=DEFAULT_CAMPAIGN_SIZE_BOOST)
    parser.add_argument("--campaign-add-on-trigger-r", type=float, default=DEFAULT_CAMPAIGN_ADD_ON_TRIGGER_R)
    parser.add_argument("--probe-failure-hold-seconds", type=float, default=DEFAULT_PROBE_FAILURE_HOLD_SECONDS)
    parser.add_argument("--probe-failure-min-followthrough-bps", type=float, default=DEFAULT_PROBE_FAILURE_MIN_FOLLOWTHROUGH_BPS)
    parser.add_argument("--campaign-failure-hold-seconds", type=float, default=DEFAULT_CAMPAIGN_FAILURE_HOLD_SECONDS)
    parser.add_argument("--campaign-failure-min-followthrough-bps", type=float, default=DEFAULT_CAMPAIGN_FAILURE_MIN_FOLLOWTHROUGH_BPS)
    parser.add_argument("--max-probe-losses-per-side", type=int, default=DEFAULT_MAX_PROBE_LOSSES_PER_SIDE)
    parser.add_argument("--max-global-probe-losses", type=int, default=DEFAULT_MAX_GLOBAL_PROBE_LOSSES)
    parser.add_argument("--probe-loss-window-seconds", type=float, default=DEFAULT_PROBE_LOSS_WINDOW_SECONDS)
    parser.add_argument("--probe-depth-share-limit", type=float, default=DEFAULT_PROBE_DEPTH_SHARE_LIMIT)
    parser.add_argument("--campaign-depth-share-limit", type=float, default=DEFAULT_CAMPAIGN_DEPTH_SHARE_LIMIT)
    parser.add_argument("--add-on-depth-share-limit", type=float, default=DEFAULT_ADD_ON_DEPTH_SHARE_LIMIT)
    parser.add_argument("--campaign-pre-unlock-fraction", type=float, default=DEFAULT_CAMPAIGN_PRE_UNLOCK_FRACTION)
    parser.add_argument("--de-risk-book-multiplier", type=float, default=DEFAULT_DE_RISK_BOOK_MULTIPLIER)
    parser.add_argument("--de-risk-breakout-reclaim-bps", type=float, default=DEFAULT_DE_RISK_BREAKOUT_RECLAIM_BPS)
    parser.add_argument("--campaign-re-add-unlock-r", type=float, default=DEFAULT_CAMPAIGN_RE_ADD_UNLOCK_R)
    parser.add_argument("--hard-reversal-confirm-ratio", type=float, default=DEFAULT_HARD_REVERSAL_CONFIRM_RATIO)
    parser.add_argument("--hard-reversal-flow-multiplier", type=float, default=DEFAULT_HARD_REVERSAL_FLOW_MULTIPLIER)
    parser.add_argument("--hard-reversal-book-multiplier", type=float, default=DEFAULT_HARD_REVERSAL_BOOK_MULTIPLIER)
    parser.add_argument("--hard-reversal-max-profit-r", type=float, default=DEFAULT_HARD_REVERSAL_MAX_PROFIT_R)
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

    bot = InitiatorFollowerJGThesisBot(
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
        probe_score_min=args.probe_score_min,
        probe_score_edge_min=args.probe_score_edge_min,
        probe_fast_threshold_ratio=args.probe_fast_threshold_ratio,
        probe_confirm_threshold_ratio=args.probe_confirm_threshold_ratio,
        probe_breakout_slack_bps=args.probe_breakout_slack_bps,
        probe_flow_multiplier=args.probe_flow_multiplier,
        probe_book_multiplier=args.probe_book_multiplier,
        campaign_score_min=args.campaign_score_min,
        campaign_score_edge_min=args.campaign_score_edge_min,
        event_impulse_bps=args.event_impulse_bps,
        event_vol_bps=args.event_vol_bps,
        loss_risk_spread_ratio=args.loss_risk_spread_ratio,
        loss_risk_quote_age_ratio=args.loss_risk_quote_age_ratio,
        min_trade_rate_per_second=args.min_trade_rate_per_second,
        probe_notional_fraction=args.probe_notional_fraction,
        campaign_entry_notional_fraction=args.campaign_entry_notional_fraction,
        probe_size_boost=args.probe_size_boost,
        campaign_size_boost=args.campaign_size_boost,
        campaign_add_on_trigger_r=args.campaign_add_on_trigger_r,
        probe_failure_hold_seconds=args.probe_failure_hold_seconds,
        probe_failure_min_followthrough_bps=args.probe_failure_min_followthrough_bps,
        campaign_failure_hold_seconds=args.campaign_failure_hold_seconds,
        campaign_failure_min_followthrough_bps=args.campaign_failure_min_followthrough_bps,
        max_probe_losses_per_side=args.max_probe_losses_per_side,
        max_global_probe_losses=args.max_global_probe_losses,
        probe_loss_window_seconds=args.probe_loss_window_seconds,
        probe_depth_share_limit=args.probe_depth_share_limit,
        campaign_depth_share_limit=args.campaign_depth_share_limit,
        add_on_depth_share_limit=args.add_on_depth_share_limit,
        campaign_pre_unlock_fraction=args.campaign_pre_unlock_fraction,
        de_risk_book_multiplier=args.de_risk_book_multiplier,
        de_risk_breakout_reclaim_bps=args.de_risk_breakout_reclaim_bps,
        campaign_re_add_unlock_r=args.campaign_re_add_unlock_r,
        hard_reversal_confirm_ratio=args.hard_reversal_confirm_ratio,
        hard_reversal_flow_multiplier=args.hard_reversal_flow_multiplier,
        hard_reversal_book_multiplier=args.hard_reversal_book_multiplier,
        hard_reversal_max_profit_r=args.hard_reversal_max_profit_r,
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

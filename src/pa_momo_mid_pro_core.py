from __future__ import annotations

import csv
import json
import logging
import math
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from pa_pump_pro_core import (
    BookSnapshot,
    HyperliquidRealtimeMultiFeed,
    MarketSnapshot,
    compute_book_imbalance,
    compute_realized_vol_bps,
    compute_trade_flow,
    latest_reference_price,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso() -> str:
    return _utc_now().isoformat()


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _clamp_non_negative(value: float) -> float:
    return value if value >= 0 else 0.0


def _bps_from_prices(current: float, reference: float) -> float:
    if reference <= 0:
        return 0.0
    return ((current / reference) - 1.0) * 10_000.0


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


@dataclass
class MidSignalSnapshot:
    sample_exchange_time_ms: int
    sample_received_time_ms: int
    quote_age_ms: int
    transport_delay_ms: int
    bid: float
    ask: float
    mid: float
    microprice: float
    spread_bps: float
    fast_impulse_bps: float
    confirm_impulse_bps: float
    dynamic_fast_threshold_bps: float
    dynamic_confirm_threshold_bps: float
    recent_vol_bps: float
    book_imbalance: float
    bid_depth: float
    ask_depth: float
    flow_imbalance: float
    buy_volume: float
    sell_volume: float
    trade_count: int
    trade_rate_per_second: float
    breakout_high: float
    breakout_low: float
    breakout_distance_long_bps: float
    breakout_distance_short_bps: float
    long_fast_ok: bool
    long_confirm_ok: bool
    long_breakout_ok: bool
    long_flow_ok: bool
    long_book_ok: bool
    long_trade_count_ok: bool
    short_fast_ok: bool
    short_confirm_ok: bool
    short_breakout_ok: bool
    short_flow_ok: bool
    short_book_ok: bool
    short_trade_count_ok: bool
    spread_ok: bool
    extreme_blocked: bool
    regime: str
    long_score: float
    short_score: float
    long_setup: bool
    short_setup: bool
    long_ready: bool
    short_ready: bool
    long_tactical_ok: bool
    short_tactical_ok: bool
    long_entry_profile: Optional[str]
    short_entry_profile: Optional[str]
    sample_count: int

    def to_dict(self) -> Dict[str, object]:
        payload = asdict(self)
        payload["sample_exchange_time_utc"] = datetime.fromtimestamp(
            self.sample_exchange_time_ms / 1000.0, tz=timezone.utc
        ).isoformat()
        payload["sample_received_time_utc"] = datetime.fromtimestamp(
            self.sample_received_time_ms / 1000.0, tz=timezone.utc
        ).isoformat()
        return payload


@dataclass
class CandidateState:
    side: str
    profile: str
    required_samples: int
    count: int
    first_seen_exchange_time_ms: int
    first_seen_ts: float
    features: Dict[str, object]


@dataclass
class MidPosition:
    trade_id: int
    side: str
    entry_price: float
    size: float
    notional: float
    required_margin: float
    entry_fee_paid: float
    initial_stop_price: float
    active_stop_price: float
    stop_distance_bps: float
    trail_distance_bps: float
    trail_arm_bps: float
    best_exit_price_seen: float
    worst_exit_price_seen: float
    stop_breach_count: int
    trailing_armed: bool
    locked_stop_price: Optional[float]
    opened_at: float
    opened_quote_exchange_time_ms: int
    opened_features: Dict[str, object]
    entry_reason: str
    entry_profile: str
    entry_score: float
    size_multiplier: float
    starter_size: float
    starter_notional: float
    max_size_seen: float
    max_notional_seen: float
    add_on_count: int
    reduction_count: int
    realized_gross_locked: float
    realized_exit_fees_locked: float
    realized_pnl_locked: float
    last_add_ts: Optional[float]
    last_reduce_ts: Optional[float]
    failure_horizon_seconds: float
    failure_min_followthrough_bps: float
    break_even_lock_r: float
    max_hold_seconds: float
    last_metrics: Dict[str, float] = field(default_factory=dict)


class MidFrequencyMomentumBot:
    def __init__(
        self,
        *,
        asset: str,
        account_balance: float,
        api_url: str,
        dex: str,
        leverage: int,
        sample_ms: int,
        warm_start_candles: bool,
        startup_state_entry: bool,
        entry_confirmation_samples: int,
        fast_impulse_window_seconds: float,
        confirm_impulse_window_seconds: float,
        breakout_lookback_seconds: float,
        volatility_lookback_seconds: float,
        flow_window_seconds: float,
        min_fast_impulse_bps: float,
        min_confirm_impulse_bps: float,
        fast_vol_multiplier: float,
        confirm_vol_multiplier: float,
        breakout_buffer_bps: float,
        flow_imbalance_min: float,
        book_imbalance_min: float,
        min_trade_count: int,
        depth_levels: int,
        max_spread_bps: float,
        max_quote_age_ms: int,
        quote_gap_warn_ms: int,
        reconnect_gap_ms: int,
        extreme_impulse_bps: float,
        extreme_vol_bps: float,
        setup_score_min: float,
        tactical_score_min: float,
        tactical_fast_threshold_ratio: float,
        tactical_confirm_threshold_ratio: float,
        tactical_breakout_slack_bps: float,
        tactical_flow_multiplier: float,
        tactical_book_multiplier: float,
        score_edge_min: float,
        instant_entry_score_min: float,
        initial_stop_min_bps: float,
        initial_stop_vol_multiplier: float,
        trail_min_bps: float,
        trail_vol_multiplier: float,
        trail_arm_r: float,
        break_even_lock_r: float,
        score_notional_boost: float,
        initial_notional_fraction: float,
        max_notional_fraction: float,
        max_add_ons: int,
        add_on_trigger_r: float,
        add_on_fraction: float,
        add_on_score_min: float,
        add_on_breakout_extension_bps: float,
        add_on_flow_multiplier: float,
        add_on_book_multiplier: float,
        add_on_min_seconds: float,
        max_reductions: int,
        de_risk_fraction: float,
        de_risk_score_threshold: float,
        de_risk_confirm_ratio: float,
        de_risk_flow_multiplier: float,
        runner_core_fraction: float,
        failure_hold_seconds: float,
        failure_min_followthrough_bps: float,
        flow_flip_exit_imbalance: float,
        book_flip_exit_imbalance: float,
        breakout_fail_buffer_bps: float,
        max_hold_seconds: float,
        time_stop_min_r: float,
        stop_confirmation_ticks: int,
        slippage_bps: float,
        fee_bps: float,
        equity_risk_pct: float,
        cooldown_seconds: float,
        max_daily_loss: float,
        max_trades_per_day: int,
        max_trades_per_hour: int,
        min_hold_seconds: float,
        events_jsonl_path: str,
        samples_jsonl_path: str,
        trades_csv_path: str,
        markouts_jsonl_path: str,
        report_dir: str,
    ) -> None:
        self.asset = asset
        self.account_balance = account_balance
        self.api_url = api_url
        self.dex = dex
        self.leverage = leverage
        self.sample_ms = sample_ms
        self.warm_start_candles = warm_start_candles
        self.startup_state_entry = startup_state_entry
        self.entry_confirmation_samples = entry_confirmation_samples
        self.fast_impulse_window_seconds = fast_impulse_window_seconds
        self.confirm_impulse_window_seconds = confirm_impulse_window_seconds
        self.breakout_lookback_seconds = breakout_lookback_seconds
        self.volatility_lookback_seconds = volatility_lookback_seconds
        self.flow_window_seconds = flow_window_seconds
        self.min_fast_impulse_bps = min_fast_impulse_bps
        self.min_confirm_impulse_bps = min_confirm_impulse_bps
        self.fast_vol_multiplier = fast_vol_multiplier
        self.confirm_vol_multiplier = confirm_vol_multiplier
        self.breakout_buffer_bps = breakout_buffer_bps
        self.flow_imbalance_min = flow_imbalance_min
        self.book_imbalance_min = book_imbalance_min
        self.min_trade_count = min_trade_count
        self.depth_levels = depth_levels
        self.max_spread_bps = max_spread_bps
        self.max_quote_age_ms = max_quote_age_ms
        self.quote_gap_warn_ms = quote_gap_warn_ms
        self.reconnect_gap_ms = reconnect_gap_ms
        self.extreme_impulse_bps = extreme_impulse_bps
        self.extreme_vol_bps = extreme_vol_bps
        self.setup_score_min = setup_score_min
        self.tactical_score_min = tactical_score_min
        self.tactical_fast_threshold_ratio = tactical_fast_threshold_ratio
        self.tactical_confirm_threshold_ratio = tactical_confirm_threshold_ratio
        self.tactical_breakout_slack_bps = tactical_breakout_slack_bps
        self.tactical_flow_multiplier = tactical_flow_multiplier
        self.tactical_book_multiplier = tactical_book_multiplier
        self.score_edge_min = score_edge_min
        self.instant_entry_score_min = instant_entry_score_min
        self.initial_stop_min_bps = initial_stop_min_bps
        self.initial_stop_vol_multiplier = initial_stop_vol_multiplier
        self.trail_min_bps = trail_min_bps
        self.trail_vol_multiplier = trail_vol_multiplier
        self.trail_arm_r = trail_arm_r
        self.break_even_lock_r = break_even_lock_r
        self.score_notional_boost = score_notional_boost
        self.initial_notional_fraction = initial_notional_fraction
        self.max_notional_fraction = max_notional_fraction
        self.max_add_ons = max_add_ons
        self.add_on_trigger_r = add_on_trigger_r
        self.add_on_fraction = add_on_fraction
        self.add_on_score_min = add_on_score_min
        self.add_on_breakout_extension_bps = add_on_breakout_extension_bps
        self.add_on_flow_multiplier = add_on_flow_multiplier
        self.add_on_book_multiplier = add_on_book_multiplier
        self.add_on_min_seconds = add_on_min_seconds
        self.max_reductions = max_reductions
        self.de_risk_fraction = de_risk_fraction
        self.de_risk_score_threshold = de_risk_score_threshold
        self.de_risk_confirm_ratio = de_risk_confirm_ratio
        self.de_risk_flow_multiplier = de_risk_flow_multiplier
        self.runner_core_fraction = runner_core_fraction
        self.failure_hold_seconds = failure_hold_seconds
        self.failure_min_followthrough_bps = failure_min_followthrough_bps
        self.flow_flip_exit_imbalance = flow_flip_exit_imbalance
        self.book_flip_exit_imbalance = book_flip_exit_imbalance
        self.breakout_fail_buffer_bps = breakout_fail_buffer_bps
        self.max_hold_seconds = max_hold_seconds
        self.time_stop_min_r = time_stop_min_r
        self.stop_confirmation_ticks = stop_confirmation_ticks
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps
        self.equity_risk_pct = equity_risk_pct / 100.0
        self.cooldown_seconds = cooldown_seconds
        self.max_daily_loss = max_daily_loss
        self.max_trades_per_day = max_trades_per_day
        self.max_trades_per_hour = max_trades_per_hour
        self.min_hold_seconds = min_hold_seconds
        self.events_jsonl_path = Path(events_jsonl_path)
        self.samples_jsonl_path = Path(samples_jsonl_path)
        self.trades_csv_path = Path(trades_csv_path)
        self.markouts_jsonl_path = Path(markouts_jsonl_path)
        self.report_dir = Path(report_dir)

        if self.leverage <= 0:
            raise ValueError("Leverage must be > 0.")
        if self.sample_ms < 50:
            raise ValueError("Sample ms must be >= 50.")
        if self.entry_confirmation_samples < 1:
            raise ValueError("Entry confirmation samples must be >= 1.")
        if self.fast_impulse_window_seconds <= 0 or self.confirm_impulse_window_seconds <= 0:
            raise ValueError("Impulse windows must be > 0.")
        if self.fast_impulse_window_seconds > self.confirm_impulse_window_seconds:
            raise ValueError("Fast impulse window must be <= confirm impulse window.")
        if self.breakout_lookback_seconds <= self.confirm_impulse_window_seconds:
            raise ValueError("Breakout lookback should exceed the confirm impulse window.")
        if self.volatility_lookback_seconds <= 0 or self.flow_window_seconds <= 0:
            raise ValueError("Volatility and flow windows must be > 0.")
        if self.max_quote_age_ms < 50:
            raise ValueError("Max quote age ms must be >= 50.")
        if self.quote_gap_warn_ms < self.max_quote_age_ms:
            raise ValueError("Quote gap warn ms must be >= max quote age ms.")
        if self.reconnect_gap_ms < self.quote_gap_warn_ms:
            raise ValueError("Reconnect gap ms must be >= quote gap warn ms.")
        if self.initial_stop_min_bps <= 0 or self.trail_min_bps <= 0:
            raise ValueError("Stop distances must be > 0.")
        if not 0.0 < self.tactical_fast_threshold_ratio <= 1.0:
            raise ValueError("Tactical fast threshold ratio must be in (0, 1].")
        if not 0.0 < self.tactical_confirm_threshold_ratio <= 1.0:
            raise ValueError("Tactical confirm threshold ratio must be in (0, 1].")
        if self.tactical_breakout_slack_bps < 0:
            raise ValueError("Tactical breakout slack bps must be >= 0.")
        if self.tactical_flow_multiplier <= 0 or self.tactical_book_multiplier <= 0:
            raise ValueError("Tactical flow/book multipliers must be > 0.")
        if self.instant_entry_score_min < self.setup_score_min:
            raise ValueError("Instant entry score minimum must be >= setup score minimum.")
        if self.score_notional_boost < 0:
            raise ValueError("Score notional boost must be >= 0.")
        if not 0.0 < self.initial_notional_fraction <= self.max_notional_fraction:
            raise ValueError("Initial notional fraction must be > 0 and <= max notional fraction.")
        if not 0.0 < self.max_notional_fraction <= 1.0:
            raise ValueError("Max notional fraction must be in (0, 1].")
        if self.max_add_ons < 0 or self.max_reductions < 0:
            raise ValueError("Max add-ons and max reductions must be >= 0.")
        if not 0.0 < self.add_on_trigger_r:
            raise ValueError("Add-on trigger R must be > 0.")
        if not 0.0 < self.add_on_fraction <= 1.0:
            raise ValueError("Add-on fraction must be in (0, 1].")
        if self.add_on_min_seconds < 0:
            raise ValueError("Add-on minimum seconds must be >= 0.")
        if not 0.0 < self.de_risk_fraction < 1.0:
            raise ValueError("De-risk fraction must be in (0, 1).")
        if not 0.0 < self.de_risk_confirm_ratio <= 1.0:
            raise ValueError("De-risk confirm ratio must be in (0, 1].")
        if self.de_risk_flow_multiplier <= 0 or not 0.0 < self.runner_core_fraction <= 1.0:
            raise ValueError("De-risk flow multiplier and runner core fraction must be > 0.")
        if self.stop_confirmation_ticks < 1:
            raise ValueError("Stop confirmation ticks must be >= 1.")
        if self.failure_hold_seconds <= 0 or self.max_hold_seconds <= 0:
            raise ValueError("Failure hold and max hold seconds must be > 0.")
        if self.slippage_bps < 0 or self.fee_bps < 0:
            raise ValueError("Slippage and fee bps must be >= 0.")
        if self.cooldown_seconds < 0 or self.min_hold_seconds < 0:
            raise ValueError("Cooldown and min hold must be >= 0.")

        self.feed: Optional[HyperliquidRealtimeMultiFeed] = None
        self.position: Optional[MidPosition] = None
        self.pending_candidate: Optional[CandidateState] = None
        self.trade_seq = 0
        self.started_at_ts = time.time()
        self.preflight_ok = False
        self.current_day_utc = self._utc_day()
        self.last_exit_ts: Optional[float] = None
        self.daily_realized_pnl = 0.0
        self.daily_trade_count = 0
        self.hourly_entry_timestamps: Deque[float] = deque()
        self.price_samples: Deque[Tuple[int, float]] = deque()
        self.last_sample_exchange_time_ms: Optional[int] = None
        self.tick_count = 0
        self.sample_count = 0
        self.signal_candidate_count = 0
        self.signal_confirmed_count = 0
        self.entry_count = 0
        self.exit_count = 0
        self.poll_error_count = 0
        self.stale_quote_skips = 0
        self.last_error: Optional[str] = None
        self.last_snapshot: Optional[MidSignalSnapshot] = None
        self.last_quote_exchange_time_ms: Optional[int] = None
        self.last_quote_transport_delay_ms: Optional[int] = None
        self.quote_age_history_ms: List[float] = []
        self.transport_delay_history_ms: List[float] = []
        self.spread_history_bps: List[float] = []
        self.long_score_history: List[float] = []
        self.short_score_history: List[float] = []
        self.closed_trade_pnls: List[float] = []
        self.closed_trade_hold_seconds: List[float] = []
        self.closed_trade_captures: List[float] = []
        self.realized_bps_history: List[float] = []
        self.exit_reason_counts: Dict[str, int] = {}
        self._finalized = False
        self._report_path: Optional[Path] = None
        self._quote_gap_active = False
        self._quote_gap_started_ms: Optional[int] = None
        self._last_reconnect_attempt_ms = 0
        self._last_stale_skip_event_ms = 0

        self.trade_retention_ms = int(
            max(
                self.confirm_impulse_window_seconds,
                self.breakout_lookback_seconds,
                self.volatility_lookback_seconds,
                self.flow_window_seconds,
            )
            * 1000.0
        ) + 180_000
        self.max_notional = self.account_balance * float(self.leverage)
        self.slippage_pct = self.slippage_bps / 10_000.0
        self.fee_pct = self.fee_bps / 10_000.0
        self._ensure_output_files()

    def _utc_day(self) -> date:
        return _utc_now().date()

    def _ensure_output_files(self) -> None:
        self.events_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self.samples_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self.trades_csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.markouts_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        if not self.trades_csv_path.exists():
            with self.trades_csv_path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    [
                        "trade_id",
                        "side",
                        "open_time_utc",
                        "close_time_utc",
                        "entry_price",
                        "exit_price",
                        "size",
                        "notional",
                        "required_margin",
                        "realized_pnl",
                        "gross_pnl",
                        "fees_paid",
                        "hold_seconds",
                        "realized_bps",
                        "roi_pct_principal",
                        "mfe_bps",
                        "mae_bps",
                        "capture_ratio",
                        "entry_reason",
                        "entry_profile",
                        "entry_score",
                        "size_multiplier",
                        "starter_notional",
                        "max_notional_seen",
                        "add_on_count",
                        "reduction_count",
                        "exit_reason",
                        "entry_fast_impulse_bps",
                        "entry_confirm_impulse_bps",
                        "entry_long_score",
                        "entry_short_score",
                        "entry_flow_imbalance",
                        "entry_book_imbalance",
                        "entry_recent_vol_bps",
                        "exit_flow_imbalance",
                        "exit_book_imbalance",
                        "exit_recent_vol_bps",
                    ]
                )

    def _write_event(self, event: str, **fields: object) -> None:
        payload = {"timestamp_utc": _utc_iso(), "asset": self.asset, "event": event, **fields}
        try:
            with self.events_jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except Exception:
            logging.exception("Failed writing mid-momentum event JSONL")

    def _write_sample(self, snapshot: MidSignalSnapshot) -> None:
        current_unrealized = 0.0
        position_side: Optional[str] = None
        trade_id: Optional[int] = None
        active_stop: Optional[float] = None
        locked_stop: Optional[float] = None
        entry_price: Optional[float] = None
        hold_seconds: Optional[float] = None
        trail_armed = False
        if self.position is not None:
            position_side = self.position.side
            trade_id = self.position.trade_id
            active_stop = self.position.active_stop_price
            locked_stop = self.position.locked_stop_price
            entry_price = self.position.entry_price
            hold_seconds = max(0.0, time.time() - self.position.opened_at)
            trail_armed = self.position.trailing_armed
            current_exit_price = self._current_exit_price(snapshot)
            direction = 1.0 if self.position.side == "LONG" else -1.0
            gross = (current_exit_price - self.position.entry_price) * self.position.size * direction
            current_unrealized = self.position.realized_pnl_locked + gross - self.position.entry_fee_paid
        payload = {
            "timestamp_utc": _utc_iso(),
            "asset": self.asset,
            **snapshot.to_dict(),
            "state": self._state_label(snapshot),
            "candidate_side": self.pending_candidate.side if self.pending_candidate is not None else None,
            "candidate_profile": self.pending_candidate.profile if self.pending_candidate is not None else None,
            "candidate_count": self.pending_candidate.count if self.pending_candidate is not None else 0,
            "candidate_required_samples": self.pending_candidate.required_samples if self.pending_candidate is not None else 0,
            "position_side": position_side,
            "open_trade_id": trade_id,
            "entry_price": entry_price,
            "active_stop": active_stop,
            "locked_stop": locked_stop,
            "trail_armed": trail_armed,
            "hold_seconds": hold_seconds,
            "unrealized_pnl": current_unrealized,
            "position_size": self.position.size if self.position is not None else None,
            "position_notional": self.position.notional if self.position is not None else None,
            "add_on_count": self.position.add_on_count if self.position is not None else 0,
            "reduction_count": self.position.reduction_count if self.position is not None else 0,
            "locked_realized_pnl": self.position.realized_pnl_locked if self.position is not None else 0.0,
            "starter_notional": self.position.starter_notional if self.position is not None else None,
            "max_notional_seen": self.position.max_notional_seen if self.position is not None else None,
            "entry_profile": self.position.entry_profile if self.position is not None else None,
            "size_multiplier": self.position.size_multiplier if self.position is not None else None,
        }
        try:
            with self.samples_jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except Exception:
            logging.exception("Failed writing mid-momentum sample JSONL")

    def _write_trade_row(
        self,
        *,
        position: MidPosition,
        exit_price: float,
        gross_pnl: float,
        fees_paid: float,
        pnl: float,
        hold_seconds: float,
        realized_bps: float,
        roi_pct_principal: Optional[float],
        mfe_bps: float,
        mae_bps: float,
        capture_ratio: Optional[float],
        exit_reason: str,
        signal: MidSignalSnapshot,
    ) -> None:
        opened = position.opened_features
        try:
            with self.trades_csv_path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    [
                        position.trade_id,
                        position.side,
                        datetime.fromtimestamp(position.opened_at, tz=timezone.utc).isoformat(),
                        _utc_iso(),
                        f"{position.entry_price:.8f}",
                        f"{exit_price:.8f}",
                        f"{position.max_size_seen:.8f}",
                        f"{position.max_notional_seen:.8f}",
                        f"{(position.max_notional_seen / float(self.leverage)):.8f}",
                        f"{pnl:.8f}",
                        f"{gross_pnl:.8f}",
                        f"{fees_paid:.8f}",
                        f"{hold_seconds:.6f}",
                        f"{realized_bps:.6f}",
                        f"{roi_pct_principal:.6f}" if roi_pct_principal is not None else "",
                        f"{mfe_bps:.6f}",
                        f"{mae_bps:.6f}",
                        f"{capture_ratio:.6f}" if capture_ratio is not None else "",
                        position.entry_reason,
                        position.entry_profile,
                        f"{position.entry_score:.6f}",
                        f"{position.size_multiplier:.6f}",
                        f"{position.starter_notional:.8f}",
                        f"{position.max_notional_seen:.8f}",
                        position.add_on_count,
                        position.reduction_count,
                        exit_reason,
                        f"{float(opened.get('fast_impulse_bps', 0.0)):.6f}",
                        f"{float(opened.get('confirm_impulse_bps', 0.0)):.6f}",
                        f"{float(opened.get('long_score', 0.0)):.6f}",
                        f"{float(opened.get('short_score', 0.0)):.6f}",
                        f"{float(opened.get('flow_imbalance', 0.0)):.6f}",
                        f"{float(opened.get('book_imbalance', 0.0)):.6f}",
                        f"{float(opened.get('recent_vol_bps', 0.0)):.6f}",
                        f"{signal.flow_imbalance:.6f}",
                        f"{signal.book_imbalance:.6f}",
                        f"{signal.recent_vol_bps:.6f}",
                    ]
                )
        except Exception:
            logging.exception("Failed writing mid-momentum trades CSV")

    def _write_markout_row(
        self,
        *,
        position: MidPosition,
        signal: MidSignalSnapshot,
        exit_reason: str,
        hold_seconds: float,
        realized_bps: float,
        mfe_bps: float,
        mae_bps: float,
        capture_ratio: Optional[float],
        pnl: float,
    ) -> None:
        opened = position.opened_features
        payload = {
            "timestamp_utc": _utc_iso(),
            "asset": self.asset,
            "trade_id": position.trade_id,
            "side": position.side,
            "entry_reason": position.entry_reason,
            "entry_profile": position.entry_profile,
            "entry_score": position.entry_score,
            "size_multiplier": position.size_multiplier,
            "starter_notional": position.starter_notional,
            "max_notional_seen": position.max_notional_seen,
            "add_on_count": position.add_on_count,
            "reduction_count": position.reduction_count,
            "exit_reason": exit_reason,
            "hold_seconds": hold_seconds,
            "realized_bps": realized_bps,
            "mfe_bps": mfe_bps,
            "mae_bps": mae_bps,
            "capture_ratio": capture_ratio,
            "pnl": pnl,
            "entry_fast_impulse_bps": opened.get("fast_impulse_bps"),
            "entry_confirm_impulse_bps": opened.get("confirm_impulse_bps"),
            "entry_long_score": opened.get("long_score"),
            "entry_short_score": opened.get("short_score"),
            "entry_flow_imbalance": opened.get("flow_imbalance"),
            "entry_book_imbalance": opened.get("book_imbalance"),
            "entry_recent_vol_bps": opened.get("recent_vol_bps"),
            "entry_regime": opened.get("regime"),
            "exit_flow_imbalance": signal.flow_imbalance,
            "exit_book_imbalance": signal.book_imbalance,
            "exit_recent_vol_bps": signal.recent_vol_bps,
            "exit_regime": signal.regime,
        }
        try:
            with self.markouts_jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except Exception:
            logging.exception("Failed writing mid-momentum markout JSONL")

    def _pctl(self, values: Sequence[float], q: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        idx = max(0, min(len(ordered) - 1, int(len(ordered) * q) - 1))
        return round(ordered[idx], 3)

    def _roll_day_if_needed(self) -> None:
        day = self._utc_day()
        if day == self.current_day_utc:
            return
        self.current_day_utc = day
        self.daily_realized_pnl = 0.0
        self.daily_trade_count = 0
        self.hourly_entry_timestamps.clear()
        self._write_event("daily_reset", reset_day=str(day))

    def _trim_hourly_entries(self, now_ts: float) -> None:
        cutoff = now_ts - 3600.0
        while self.hourly_entry_timestamps and self.hourly_entry_timestamps[0] < cutoff:
            self.hourly_entry_timestamps.popleft()

    def _entry_allowed(self, now_ts: float) -> bool:
        self._trim_hourly_entries(now_ts)
        if self.position is not None:
            return False
        if self.daily_realized_pnl <= -abs(self.max_daily_loss):
            return False
        if self.max_trades_per_day > 0 and self.daily_trade_count >= self.max_trades_per_day:
            return False
        if self.max_trades_per_hour > 0 and len(self.hourly_entry_timestamps) >= self.max_trades_per_hour:
            return False
        if self.last_exit_ts is not None and (now_ts - self.last_exit_ts) < self.cooldown_seconds:
            return False
        return True

    def _append_price_sample(self, ts_ms: int, microprice: float) -> None:
        self.price_samples.append((ts_ms, microprice))
        oldest_keep_ms = ts_ms - int(
            max(
                self.breakout_lookback_seconds,
                self.volatility_lookback_seconds,
                self.confirm_impulse_window_seconds,
            )
            * 1000.0
        ) - 180_000
        while len(self.price_samples) > 1 and self.price_samples[0][0] < oldest_keep_ms:
            self.price_samples.popleft()

    def _normalize_component(self, value: float, threshold: float) -> float:
        if threshold <= 0:
            return 0.0
        return _clamp(value / threshold, 0.0, 1.4)

    def _score_side(
        self,
        *,
        impulse_fast_bps: float,
        impulse_confirm_bps: float,
        threshold_fast_bps: float,
        threshold_confirm_bps: float,
        breakout_distance_bps: float,
        flow_value: float,
        book_value: float,
        trade_count: int,
        spread_bps: float,
        direction: int,
        spread_ok: bool,
        extreme_blocked: bool,
    ) -> float:
        fast_component = self._normalize_component(direction * impulse_fast_bps, threshold_fast_bps)
        confirm_component = self._normalize_component(direction * impulse_confirm_bps, threshold_confirm_bps)
        breakout_component = self._normalize_component(direction * breakout_distance_bps, max(self.breakout_buffer_bps, 0.5))
        flow_component = self._normalize_component(direction * flow_value, max(self.flow_imbalance_min, 1e-6))
        book_component = self._normalize_component(direction * book_value, max(self.book_imbalance_min, 1e-6))
        trade_component = self._normalize_component(float(trade_count), float(max(self.min_trade_count, 1)))
        spread_component = _clamp(1.0 - _safe_ratio(spread_bps, max(self.max_spread_bps, 0.1)), 0.0, 1.0)
        raw = (
            0.18 * fast_component
            + 0.24 * confirm_component
            + 0.16 * breakout_component
            + 0.16 * flow_component
            + 0.12 * book_component
            + 0.08 * trade_component
            + 0.06 * spread_component
        )
        if not spread_ok:
            raw *= 0.25
        if extreme_blocked:
            raw *= 0.2
        return round(_clamp(raw, 0.0, 1.0) * 100.0, 2)

    def _entry_profile(
        self,
        *,
        side: str,
        score: float,
        opposing_score: float,
        fast_impulse_bps: float,
        confirm_impulse_bps: float,
        fast_threshold_bps: float,
        confirm_threshold_bps: float,
        breakout_distance_bps: float,
        flow_imbalance: float,
        book_imbalance: float,
        trade_count_ok: bool,
        spread_ok: bool,
        extreme_blocked: bool,
        full_ready: bool,
    ) -> Optional[str]:
        score_edge = score - opposing_score
        if (
            full_ready
            and trade_count_ok
            and spread_ok
            and not extreme_blocked
            and score_edge >= self.score_edge_min
        ):
            return "breakout_stack"
        return None

    def _required_confirmation_samples(
        self,
        *,
        side: str,
        profile: str,
        score: float,
    ) -> int:
        if score >= self.instant_entry_score_min:
            return 1
        return self.entry_confirmation_samples

    def _directional_score(self, signal: MidSignalSnapshot, side: str) -> float:
        return signal.long_score if side == "LONG" else signal.short_score

    def _directional_breakout_distance(self, signal: MidSignalSnapshot, side: str) -> float:
        return signal.breakout_distance_long_bps if side == "LONG" else signal.breakout_distance_short_bps

    def _directional_confirm_impulse(self, signal: MidSignalSnapshot, side: str) -> float:
        return signal.confirm_impulse_bps if side == "LONG" else -signal.confirm_impulse_bps

    def _directional_flow(self, signal: MidSignalSnapshot, side: str) -> float:
        return signal.flow_imbalance if side == "LONG" else -signal.flow_imbalance

    def _directional_book(self, signal: MidSignalSnapshot, side: str) -> float:
        return signal.book_imbalance if side == "LONG" else -signal.book_imbalance

    def _size_multiplier(
        self,
        *,
        signal: MidSignalSnapshot,
        side: str,
        profile: str,
    ) -> float:
        score = signal.long_score if side == "LONG" else signal.short_score
        score_component = _clamp(
            _safe_ratio(score - self.setup_score_min, max(100.0 - self.setup_score_min, 1.0)),
            0.0,
            1.0,
        )
        spread_component = _clamp(
            1.0 - _safe_ratio(signal.spread_bps, max(self.max_spread_bps, 0.1)),
            0.0,
            1.0,
        )
        flow_value = abs(signal.flow_imbalance)
        flow_component = _clamp(
            _safe_ratio(flow_value - self.flow_imbalance_min, max(1.0 - self.flow_imbalance_min, 1e-6)),
            0.0,
            1.0,
        )
        profile_boost = 0.08 if profile == "breakout_stack" else 0.0
        multiplier = 1.0 + profile_boost + (self.score_notional_boost * ((0.65 * score_component) + (0.20 * spread_component) + (0.15 * flow_component)))
        return round(min(multiplier, 1.0 + self.score_notional_boost), 6)

    def _build_signal_snapshot(self, market: MarketSnapshot) -> Optional[MidSignalSnapshot]:
        quote = market.quote
        now_ms = int(time.time() * 1000)
        quote_age_ms = int(_clamp_non_negative(now_ms - quote.exchange_time_ms))
        if quote_age_ms > self.max_quote_age_ms:
            return None

        self._append_price_sample(quote.exchange_time_ms, quote.microprice)
        fast_ref = latest_reference_price(
            list(self.price_samples),
            quote.exchange_time_ms - int(self.fast_impulse_window_seconds * 1000.0),
        )
        confirm_ref = latest_reference_price(
            list(self.price_samples),
            quote.exchange_time_ms - int(self.confirm_impulse_window_seconds * 1000.0),
        )
        if fast_ref is None or confirm_ref is None or fast_ref <= 0 or confirm_ref <= 0:
            return None

        breakout_cutoff = quote.exchange_time_ms - int(self.breakout_lookback_seconds * 1000.0)
        prior_prices = [
            px
            for ts_ms, px in self.price_samples
            if breakout_cutoff <= ts_ms < quote.exchange_time_ms
        ]
        if not prior_prices:
            return None

        vol_cutoff = quote.exchange_time_ms - int(self.volatility_lookback_seconds * 1000.0)
        recent_vol_bps = compute_realized_vol_bps(
            list(self.price_samples),
            since_ms=vol_cutoff,
            impulse_window_seconds=self.confirm_impulse_window_seconds,
        )
        dynamic_fast_threshold_bps = max(self.min_fast_impulse_bps, self.fast_vol_multiplier * recent_vol_bps)
        dynamic_confirm_threshold_bps = max(
            self.min_confirm_impulse_bps,
            self.confirm_vol_multiplier * recent_vol_bps,
        )
        fast_impulse_bps = _bps_from_prices(quote.microprice, fast_ref)
        confirm_impulse_bps = _bps_from_prices(quote.microprice, confirm_ref)

        book_imbalance, bid_depth, ask_depth = compute_book_imbalance(market.book, self.depth_levels)
        flow = compute_trade_flow(
            market.trades,
            since_ms=quote.exchange_time_ms - int(self.flow_window_seconds * 1000.0),
        )
        breakout_high = max(prior_prices)
        breakout_low = min(prior_prices)
        breakout_buffer_up = breakout_high * (1.0 + (self.breakout_buffer_bps / 10_000.0))
        breakout_buffer_dn = breakout_low * (1.0 - (self.breakout_buffer_bps / 10_000.0))
        breakout_distance_long_bps = _bps_from_prices(quote.mid, breakout_high)
        breakout_distance_short_bps = _bps_from_prices(breakout_low, quote.mid)
        spread_ok = quote.spread_bps <= self.max_spread_bps
        trade_count_ok = flow["count"] >= self.min_trade_count
        extreme_blocked = (
            abs(confirm_impulse_bps) >= self.extreme_impulse_bps
            or recent_vol_bps >= self.extreme_vol_bps
        )

        long_fast_ok = fast_impulse_bps >= dynamic_fast_threshold_bps
        long_confirm_ok = confirm_impulse_bps >= dynamic_confirm_threshold_bps
        long_breakout_ok = quote.mid >= breakout_buffer_up
        long_flow_ok = flow["imbalance"] >= self.flow_imbalance_min
        long_book_ok = book_imbalance >= self.book_imbalance_min

        short_fast_ok = fast_impulse_bps <= -dynamic_fast_threshold_bps
        short_confirm_ok = confirm_impulse_bps <= -dynamic_confirm_threshold_bps
        short_breakout_ok = quote.mid <= breakout_buffer_dn
        short_flow_ok = flow["imbalance"] <= -self.flow_imbalance_min
        short_book_ok = book_imbalance <= -self.book_imbalance_min

        long_score = self._score_side(
            impulse_fast_bps=fast_impulse_bps,
            impulse_confirm_bps=confirm_impulse_bps,
            threshold_fast_bps=dynamic_fast_threshold_bps,
            threshold_confirm_bps=dynamic_confirm_threshold_bps,
            breakout_distance_bps=breakout_distance_long_bps,
            flow_value=flow["imbalance"],
            book_value=book_imbalance,
            trade_count=int(flow["count"]),
            spread_bps=quote.spread_bps,
            direction=1,
            spread_ok=spread_ok,
            extreme_blocked=extreme_blocked,
        )
        short_score = self._score_side(
            impulse_fast_bps=fast_impulse_bps,
            impulse_confirm_bps=confirm_impulse_bps,
            threshold_fast_bps=dynamic_fast_threshold_bps,
            threshold_confirm_bps=dynamic_confirm_threshold_bps,
            breakout_distance_bps=breakout_distance_short_bps,
            flow_value=flow["imbalance"],
            book_value=book_imbalance,
            trade_count=int(flow["count"]),
            spread_bps=quote.spread_bps,
            direction=-1,
            spread_ok=spread_ok,
            extreme_blocked=extreme_blocked,
        )

        if not spread_ok:
            regime = "blocked"
        elif extreme_blocked:
            regime = "extreme"
        elif (
            trade_count_ok
            and (abs(confirm_impulse_bps) >= dynamic_confirm_threshold_bps * 0.60
                 or max(long_score, short_score) >= self.setup_score_min)
        ):
            regime = "active"
        else:
            regime = "cold"

        long_setup = (
            regime == "active"
            and spread_ok
            and not extreme_blocked
            and long_score >= self.setup_score_min
        )
        short_setup = (
            regime == "active"
            and spread_ok
            and not extreme_blocked
            and short_score >= self.setup_score_min
        )
        long_ready = (
            long_setup
            and trade_count_ok
            and long_fast_ok
            and long_confirm_ok
            and long_breakout_ok
            and long_flow_ok
            and long_book_ok
        )
        short_ready = (
            short_setup
            and trade_count_ok
            and short_fast_ok
            and short_confirm_ok
            and short_breakout_ok
            and short_flow_ok
            and short_book_ok
        )

        long_entry_profile = self._entry_profile(
            side="LONG",
            score=long_score,
            opposing_score=short_score,
            fast_impulse_bps=fast_impulse_bps,
            confirm_impulse_bps=confirm_impulse_bps,
            fast_threshold_bps=dynamic_fast_threshold_bps,
            confirm_threshold_bps=dynamic_confirm_threshold_bps,
            breakout_distance_bps=breakout_distance_long_bps,
            flow_imbalance=flow["imbalance"],
            book_imbalance=book_imbalance,
            trade_count_ok=trade_count_ok,
            spread_ok=spread_ok,
            extreme_blocked=extreme_blocked,
            full_ready=long_ready,
        )
        short_entry_profile = self._entry_profile(
            side="SHORT",
            score=short_score,
            opposing_score=long_score,
            fast_impulse_bps=fast_impulse_bps,
            confirm_impulse_bps=confirm_impulse_bps,
            fast_threshold_bps=dynamic_fast_threshold_bps,
            confirm_threshold_bps=dynamic_confirm_threshold_bps,
            breakout_distance_bps=breakout_distance_short_bps,
            flow_imbalance=flow["imbalance"],
            book_imbalance=book_imbalance,
            trade_count_ok=trade_count_ok,
            spread_ok=spread_ok,
            extreme_blocked=extreme_blocked,
            full_ready=short_ready,
        )

        return MidSignalSnapshot(
            sample_exchange_time_ms=quote.exchange_time_ms,
            sample_received_time_ms=quote.received_time_ms,
            quote_age_ms=quote_age_ms,
            transport_delay_ms=quote.transport_delay_ms,
            bid=quote.bid,
            ask=quote.ask,
            mid=quote.mid,
            microprice=quote.microprice,
            spread_bps=quote.spread_bps,
            fast_impulse_bps=fast_impulse_bps,
            confirm_impulse_bps=confirm_impulse_bps,
            dynamic_fast_threshold_bps=dynamic_fast_threshold_bps,
            dynamic_confirm_threshold_bps=dynamic_confirm_threshold_bps,
            recent_vol_bps=recent_vol_bps,
            book_imbalance=book_imbalance,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            flow_imbalance=flow["imbalance"],
            buy_volume=flow["buy_volume"],
            sell_volume=flow["sell_volume"],
            trade_count=int(flow["count"]),
            trade_rate_per_second=flow["trade_rate_per_second"],
            breakout_high=breakout_high,
            breakout_low=breakout_low,
            breakout_distance_long_bps=breakout_distance_long_bps,
            breakout_distance_short_bps=breakout_distance_short_bps,
            long_fast_ok=long_fast_ok,
            long_confirm_ok=long_confirm_ok,
            long_breakout_ok=long_breakout_ok,
            long_flow_ok=long_flow_ok,
            long_book_ok=long_book_ok,
            long_trade_count_ok=trade_count_ok,
            short_fast_ok=short_fast_ok,
            short_confirm_ok=short_confirm_ok,
            short_breakout_ok=short_breakout_ok,
            short_flow_ok=short_flow_ok,
            short_book_ok=short_book_ok,
            short_trade_count_ok=trade_count_ok,
            spread_ok=spread_ok,
            extreme_blocked=extreme_blocked,
            regime=regime,
            long_score=long_score,
            short_score=short_score,
            long_setup=long_setup,
            short_setup=short_setup,
            long_ready=long_ready,
            short_ready=short_ready,
            long_tactical_ok=long_entry_profile == "pressure_resume",
            short_tactical_ok=short_entry_profile == "pressure_resume",
            long_entry_profile=long_entry_profile,
            short_entry_profile=short_entry_profile,
            sample_count=len(self.price_samples),
        )

    def _state_label(self, snapshot: Optional[MidSignalSnapshot]) -> str:
        if self.position is not None:
            return "in_position"
        if self.last_exit_ts is not None and (time.time() - self.last_exit_ts) < self.cooldown_seconds:
            return "cooldown"
        if snapshot is None:
            return "idle"
        if snapshot.regime == "extreme":
            return "handoff_extreme"
        if self.pending_candidate is not None:
            return "armed" if self.pending_candidate.count >= self.pending_candidate.required_samples else "setup"
        if snapshot.long_setup or snapshot.short_setup:
            return "setup"
        return "idle"

    def _sample_signal_state(self, market: MarketSnapshot) -> Optional[MidSignalSnapshot]:
        quote = market.quote
        if (
            self.last_sample_exchange_time_ms is not None
            and quote.exchange_time_ms < (self.last_sample_exchange_time_ms + self.sample_ms)
        ):
            return None
        self.last_sample_exchange_time_ms = quote.exchange_time_ms
        snapshot = self._build_signal_snapshot(market)
        if snapshot is None:
            return None
        self.last_snapshot = snapshot
        self.sample_count += 1
        self.long_score_history.append(snapshot.long_score)
        self.short_score_history.append(snapshot.short_score)
        self._write_sample(snapshot)
        return snapshot

    def _update_candidate(self, signal: MidSignalSnapshot) -> Optional[CandidateState]:
        candidates: List[Tuple[str, str, float]] = []
        if signal.long_entry_profile is not None:
            candidates.append(("LONG", signal.long_entry_profile, signal.long_score))
        if signal.short_entry_profile is not None:
            candidates.append(("SHORT", signal.short_entry_profile, signal.short_score))
        if not candidates:
            self.pending_candidate = None
            return None
        if len(candidates) == 2 and abs(candidates[0][2] - candidates[1][2]) < 3.0:
            self.pending_candidate = None
            return None
        side, profile, score = max(candidates, key=lambda item: item[2])
        required_samples = self._required_confirmation_samples(
            side=side,
            profile=profile,
            score=score,
        )
        if (
            self.pending_candidate is None
            or self.pending_candidate.side != side
            or self.pending_candidate.profile != profile
        ):
            self.pending_candidate = CandidateState(
                side=side,
                profile=profile,
                required_samples=required_samples,
                count=1,
                first_seen_exchange_time_ms=signal.sample_exchange_time_ms,
                first_seen_ts=time.time(),
                features=signal.to_dict(),
            )
            self.signal_candidate_count += 1
            self._write_event(
                "signal_candidate",
                side=side,
                entry_profile=profile,
                required_samples=required_samples,
                candidate_count=1,
                **signal.to_dict(),
            )
            return self.pending_candidate
        self.pending_candidate.count += 1
        self.pending_candidate.required_samples = required_samples
        self.pending_candidate.features = signal.to_dict()
        return self.pending_candidate

    def _compute_stop_distance_bps(self, signal: MidSignalSnapshot) -> float:
        return max(
            self.initial_stop_min_bps,
            self.initial_stop_vol_multiplier * max(signal.recent_vol_bps, signal.spread_bps),
            abs(signal.confirm_impulse_bps) * 0.40,
        )

    def _compute_trail_distance_bps(self, signal: MidSignalSnapshot) -> float:
        return max(
            self.trail_min_bps,
            self.trail_vol_multiplier * max(signal.recent_vol_bps, signal.spread_bps),
            abs(signal.confirm_impulse_bps) * 0.25,
        )

    def _entry_notional_target(
        self,
        *,
        signal: MidSignalSnapshot,
        side: str,
        profile: str,
    ) -> Tuple[float, float, float, float]:
        stop_distance_bps = self._compute_stop_distance_bps(signal)
        price_stop_fraction = stop_distance_bps / 10_000.0
        risk_amount = self.account_balance * self.equity_risk_pct
        size_multiplier = self._size_multiplier(signal=signal, side=side, profile=profile)
        notional_target = (risk_amount / max(price_stop_fraction, 1e-9)) * size_multiplier
        return stop_distance_bps, price_stop_fraction, size_multiplier, notional_target

    def _starter_notional_cap(self) -> float:
        return self.max_notional * self.initial_notional_fraction

    def _total_notional_cap(self) -> float:
        return self.max_notional * self.max_notional_fraction

    def _runner_core_size_floor(self, position: MidPosition) -> float:
        return max(position.starter_size, position.max_size_seen * self.runner_core_fraction)

    def _add_on_ready(self, signal: MidSignalSnapshot) -> bool:
        if self.position is None:
            return False
        position = self.position
        if position.add_on_count >= self.max_add_ons:
            return False
        if signal.regime != "active" or not signal.spread_ok or signal.extreme_blocked:
            return False
        hold_seconds = max(0.0, time.time() - position.opened_at)
        if hold_seconds < self.add_on_min_seconds:
            return False
        current_exit_price = self._current_exit_price(signal)
        current_realized_bps = self._current_realized_bps(current_exit_price)
        mfe_bps, _ = self._position_mfe_bps(current_exit_price)
        score = self._directional_score(signal, position.side)
        breakout_distance = self._directional_breakout_distance(signal, position.side)
        confirm_impulse = self._directional_confirm_impulse(signal, position.side)
        flow = self._directional_flow(signal, position.side)
        book = self._directional_book(signal, position.side)
        if current_realized_bps < (position.stop_distance_bps * self.add_on_trigger_r):
            return False
        if mfe_bps < (position.stop_distance_bps * self.add_on_trigger_r):
            return False
        if score < self.add_on_score_min:
            return False
        if confirm_impulse < signal.dynamic_confirm_threshold_bps:
            return False
        if breakout_distance < self.add_on_breakout_extension_bps:
            return False
        if flow < (self.flow_imbalance_min * self.add_on_flow_multiplier):
            return False
        if book < (self.book_imbalance_min * self.add_on_book_multiplier):
            return False
        if position.last_add_ts is not None and (time.time() - position.last_add_ts) < self.add_on_min_seconds:
            return False
        return True

    def _scale_in_position(self, signal: MidSignalSnapshot) -> bool:
        assert self.position is not None
        position = self.position
        remaining_cap = self._total_notional_cap() - position.notional
        if remaining_cap <= 0:
            return False
        side = position.side
        add_cap = self.max_notional * self.add_on_fraction
        add_score = max(self._directional_score(signal, side) - self.add_on_score_min, 0.0)
        add_scale = 1.0 + min(0.35, add_score / 100.0)
        add_notional = min(remaining_cap, add_cap * add_scale)
        if add_notional <= 0:
            return False
        fill_price = signal.ask if side == "LONG" else signal.bid
        add_size = add_notional / max(fill_price, 1e-9)
        add_fee = add_notional * self.fee_pct
        combined_size = position.size + add_size
        if combined_size <= 0:
            return False
        weighted_entry = ((position.entry_price * position.size) + (fill_price * add_size)) / combined_size
        position.entry_price = weighted_entry
        position.size = combined_size
        position.notional += add_notional
        position.required_margin = position.notional / float(self.leverage)
        position.entry_fee_paid += add_fee
        position.add_on_count += 1
        position.last_add_ts = time.time()
        position.max_size_seen = max(position.max_size_seen, position.size)
        position.max_notional_seen = max(position.max_notional_seen, position.notional)
        self._write_event(
            "position_added",
            trade_id=position.trade_id,
            side=side,
            add_on_count=position.add_on_count,
            add_price=fill_price,
            add_size=add_size,
            add_notional=add_notional,
            combined_size=position.size,
            combined_notional=position.notional,
            weighted_entry_price=position.entry_price,
            signal=signal.to_dict(),
        )
        return True

    def _decay_reduction_ready(
        self,
        signal: MidSignalSnapshot,
        *,
        hold_seconds: float,
        mfe_bps: float,
    ) -> bool:
        if self.position is None:
            return False
        position = self.position
        if position.add_on_count <= 0 or position.reduction_count >= self.max_reductions:
            return False
        if hold_seconds < self.add_on_min_seconds or not position.trailing_armed:
            return False
        if mfe_bps < (position.stop_distance_bps * self.trail_arm_r):
            return False
        if position.last_reduce_ts is not None and (time.time() - position.last_reduce_ts) < self.add_on_min_seconds:
            return False
        score = self._directional_score(signal, position.side)
        confirm_impulse = self._directional_confirm_impulse(signal, position.side)
        flow = self._directional_flow(signal, position.side)
        slowdown_flags = 0
        if score < self.de_risk_score_threshold:
            slowdown_flags += 1
        if confirm_impulse < (signal.dynamic_confirm_threshold_bps * self.de_risk_confirm_ratio):
            slowdown_flags += 1
        if flow < (self.flow_imbalance_min * self.de_risk_flow_multiplier):
            slowdown_flags += 1
        if signal.regime != "active" or signal.trade_count < self.min_trade_count:
            slowdown_flags += 1
        return slowdown_flags >= 2

    def _scale_out_position(self, signal: MidSignalSnapshot, reason: str) -> bool:
        assert self.position is not None
        position = self.position
        core_floor = self._runner_core_size_floor(position)
        reducible_size = position.size - core_floor
        if reducible_size <= 0:
            return False
        reduce_size = min(position.size * self.de_risk_fraction, reducible_size)
        if reduce_size <= 0:
            return False
        fill_price = signal.bid if position.side == "LONG" else signal.ask
        if self.slippage_bps > 0:
            slippage_fraction = self.slippage_bps / 10_000.0
            if position.side == "LONG":
                fill_price *= (1.0 - slippage_fraction)
            else:
                fill_price *= (1.0 + slippage_fraction)
        exit_notional = reduce_size * fill_price
        direction = 1.0 if position.side == "LONG" else -1.0
        gross_pnl = (fill_price - position.entry_price) * reduce_size * direction
        exit_fee = exit_notional * self.fee_pct
        position.realized_gross_locked += gross_pnl
        position.realized_exit_fees_locked += exit_fee
        position.realized_pnl_locked += gross_pnl - exit_fee
        position.size -= reduce_size
        position.notional = position.size * position.entry_price
        position.required_margin = position.notional / float(self.leverage)
        position.reduction_count += 1
        position.last_reduce_ts = time.time()
        self._write_event(
            "position_reduced",
            trade_id=position.trade_id,
            side=position.side,
            reduce_reason=reason,
            reduce_size=reduce_size,
            reduce_price=fill_price,
            reduce_notional=exit_notional,
            gross_pnl_delta=gross_pnl,
            fee_delta=exit_fee,
            locked_realized_pnl=position.realized_pnl_locked,
            remaining_size=position.size,
            remaining_notional=position.notional,
            signal=signal.to_dict(),
        )
        return True

    def _open_position(self, signal: MidSignalSnapshot, side: str, reason: str, entry_profile: str) -> None:
        now_ts = time.time()
        if not self._entry_allowed(now_ts):
            return
        stop_distance_bps, price_stop_fraction, size_multiplier, notional_target = self._entry_notional_target(
            signal=signal,
            side=side,
            profile=entry_profile,
        )
        trail_distance_bps = self._compute_trail_distance_bps(signal)
        notional = min(notional_target, self._starter_notional_cap())
        entry_price = signal.ask if side == "LONG" else signal.bid
        size = notional / max(entry_price, 1e-9)
        required_margin = notional / float(self.leverage)
        entry_fee = notional * self.fee_pct
        stop_price = (
            entry_price * (1.0 - price_stop_fraction)
            if side == "LONG"
            else entry_price * (1.0 + price_stop_fraction)
        )
        best_exit_price_seen = signal.bid if side == "LONG" else signal.ask
        trail_arm_bps = max(self.failure_min_followthrough_bps, stop_distance_bps * self.trail_arm_r)

        self.trade_seq += 1
        self.position = MidPosition(
            trade_id=self.trade_seq,
            side=side,
            entry_price=entry_price,
            size=size,
            notional=notional,
            required_margin=required_margin,
            entry_fee_paid=entry_fee,
            initial_stop_price=stop_price,
            active_stop_price=stop_price,
            stop_distance_bps=stop_distance_bps,
            trail_distance_bps=trail_distance_bps,
            trail_arm_bps=trail_arm_bps,
            best_exit_price_seen=best_exit_price_seen,
            worst_exit_price_seen=best_exit_price_seen,
            stop_breach_count=0,
            trailing_armed=False,
            locked_stop_price=None,
            opened_at=now_ts,
            opened_quote_exchange_time_ms=signal.sample_exchange_time_ms,
            opened_features=signal.to_dict(),
            entry_reason=reason,
            entry_profile=entry_profile,
            entry_score=signal.long_score if side == "LONG" else signal.short_score,
            size_multiplier=size_multiplier,
            starter_size=size,
            starter_notional=notional,
            max_size_seen=size,
            max_notional_seen=notional,
            add_on_count=0,
            reduction_count=0,
            realized_gross_locked=0.0,
            realized_exit_fees_locked=0.0,
            realized_pnl_locked=0.0,
            last_add_ts=None,
            last_reduce_ts=None,
            failure_horizon_seconds=self.failure_hold_seconds,
            failure_min_followthrough_bps=self.failure_min_followthrough_bps,
            break_even_lock_r=self.break_even_lock_r,
            max_hold_seconds=self.max_hold_seconds,
        )
        self.hourly_entry_timestamps.append(now_ts)
        self.daily_trade_count += 1
        self.entry_count += 1
        self.pending_candidate = None
        self._write_event(
            "position_opened",
            trade_id=self.position.trade_id,
            side=side,
            entry_price=entry_price,
            size=size,
            notional=notional,
            required_margin=required_margin,
            entry_fee=entry_fee,
            size_multiplier=size_multiplier,
            starter_notional=notional,
            stop_distance_bps=stop_distance_bps,
            trail_distance_bps=trail_distance_bps,
            initial_stop=stop_price,
            trail_arm_bps=trail_arm_bps,
            entry_profile=entry_profile,
            signal_reason=reason,
            signal=self.position.opened_features,
        )

    def _maybe_enter(self, signal: MidSignalSnapshot) -> None:
        if signal.regime != "active":
            self.pending_candidate = None
            return
        candidate = self._update_candidate(signal)
        if candidate is None:
            return
        if (
            candidate.first_seen_exchange_time_ms == signal.sample_exchange_time_ms
            and not self.startup_state_entry
        ):
            return
        if candidate.count < candidate.required_samples:
            return
        if not self._entry_allowed(time.time()):
            return
        entry_score = signal.long_score if candidate.side == "LONG" else signal.short_score
        reason = (
            f"{candidate.side} {candidate.profile} "
            f"fast_bps={signal.fast_impulse_bps:.2f} "
            f"confirm_bps={signal.confirm_impulse_bps:.2f} "
            f"flow={signal.flow_imbalance:.3f} "
            f"book={signal.book_imbalance:.3f} "
            f"score={entry_score:.2f}"
        )
        self.signal_confirmed_count += 1
        self._write_event(
            "signal_confirmed",
            side=candidate.side,
            entry_profile=candidate.profile,
            required_samples=candidate.required_samples,
            candidate_count=candidate.count,
            **signal.to_dict(),
        )
        self._open_position(signal, candidate.side, reason, candidate.profile)

    def _current_exit_price(self, signal: MidSignalSnapshot) -> float:
        assert self.position is not None
        return signal.bid if self.position.side == "LONG" else signal.ask

    def _current_realized_bps(self, current_exit_price: float) -> float:
        assert self.position is not None
        direction = 1.0 if self.position.side == "LONG" else -1.0
        return _bps_from_prices(current_exit_price, self.position.entry_price) * direction

    def _position_mfe_bps(self, current_exit_price: float) -> Tuple[float, float]:
        assert self.position is not None
        if self.position.side == "LONG":
            best = max(self.position.best_exit_price_seen, current_exit_price)
            worst = min(self.position.worst_exit_price_seen, current_exit_price)
            mfe = _bps_from_prices(best, self.position.entry_price)
            mae = _bps_from_prices(worst, self.position.entry_price)
        else:
            best = min(self.position.best_exit_price_seen, current_exit_price)
            worst = max(self.position.worst_exit_price_seen, current_exit_price)
            mfe = _bps_from_prices(self.position.entry_price, best)
            mae = _bps_from_prices(self.position.entry_price, worst)
        return mfe, mae

    def _maybe_lock_break_even(self, signal: MidSignalSnapshot, mfe_bps: float) -> None:
        assert self.position is not None
        if mfe_bps < (self.position.stop_distance_bps * self.position.break_even_lock_r):
            return
        costs_bps = self.slippage_bps + (2.0 * self.fee_bps)
        if self.position.side == "LONG":
            lock_price = self.position.entry_price * (1.0 + costs_bps / 10_000.0)
            if self.position.locked_stop_price is None or lock_price > self.position.locked_stop_price:
                self.position.locked_stop_price = lock_price
        else:
            lock_price = self.position.entry_price * (1.0 - costs_bps / 10_000.0)
            if self.position.locked_stop_price is None or lock_price < self.position.locked_stop_price:
                self.position.locked_stop_price = lock_price

    def _update_trailing_stop(
        self,
        signal: MidSignalSnapshot,
        current_exit_price: float,
        mfe_bps: float,
    ) -> None:
        assert self.position is not None
        if self.position.side == "LONG":
            self.position.best_exit_price_seen = max(self.position.best_exit_price_seen, current_exit_price)
            self.position.worst_exit_price_seen = min(self.position.worst_exit_price_seen, current_exit_price)
        else:
            self.position.best_exit_price_seen = min(self.position.best_exit_price_seen, current_exit_price)
            self.position.worst_exit_price_seen = max(self.position.worst_exit_price_seen, current_exit_price)

        if not self.position.trailing_armed and mfe_bps >= self.position.trail_arm_bps:
            self.position.trailing_armed = True
            self._write_event(
                "trailing_armed",
                trade_id=self.position.trade_id,
                side=self.position.side,
                trail_arm_bps=self.position.trail_arm_bps,
                mfe_bps=mfe_bps,
            )
        if not self.position.trailing_armed:
            return

        distance_fraction = self.position.trail_distance_bps / 10_000.0
        if self.position.side == "LONG":
            trail_stop = self.position.best_exit_price_seen * (1.0 - distance_fraction)
            trail_stop = max(trail_stop, self.position.initial_stop_price)
            if self.position.locked_stop_price is not None:
                trail_stop = max(trail_stop, self.position.locked_stop_price)
        else:
            trail_stop = self.position.best_exit_price_seen * (1.0 + distance_fraction)
            trail_stop = min(trail_stop, self.position.initial_stop_price)
            if self.position.locked_stop_price is not None:
                trail_stop = min(trail_stop, self.position.locked_stop_price)
        if trail_stop != self.position.active_stop_price:
            self.position.active_stop_price = trail_stop
            self._write_event(
                "trailing_stop_updated",
                trade_id=self.position.trade_id,
                side=self.position.side,
                active_stop=self.position.active_stop_price,
                trail_distance_bps=self.position.trail_distance_bps,
                best_exit_price_seen=self.position.best_exit_price_seen,
                locked_stop=self.position.locked_stop_price,
            )

    def _breakout_failure(self, signal: MidSignalSnapshot) -> bool:
        if self.position is None:
            return False
        if self.position.side == "LONG":
            fail_level = self.position.opened_features.get("breakout_high")
            if fail_level is None:
                return False
            threshold = float(fail_level) * (1.0 - (self.breakout_fail_buffer_bps / 10_000.0))
            return signal.mid <= threshold
        fail_level = self.position.opened_features.get("breakout_low")
        if fail_level is None:
            return False
        threshold = float(fail_level) * (1.0 + (self.breakout_fail_buffer_bps / 10_000.0))
        return signal.mid >= threshold

    def _flow_flip_exit(self, signal: MidSignalSnapshot, current_realized_bps: float) -> bool:
        if self.position is None:
            return False
        if current_realized_bps >= max(2.0, self.position.stop_distance_bps * 0.35):
            return False
        if self.position.side == "LONG":
            return (
                signal.flow_imbalance <= -abs(self.flow_flip_exit_imbalance)
                and signal.book_imbalance <= -abs(self.book_flip_exit_imbalance)
            )
        return (
            signal.flow_imbalance >= abs(self.flow_flip_exit_imbalance)
            and signal.book_imbalance >= abs(self.book_flip_exit_imbalance)
        )

    def _time_stop_exit(
        self,
        current_realized_bps: float,
        hold_seconds: float,
    ) -> bool:
        if self.position is None:
            return False
        if hold_seconds < self.position.max_hold_seconds:
            return False
        return current_realized_bps < (self.position.stop_distance_bps * self.time_stop_min_r)

    def _close_position(
        self,
        signal: MidSignalSnapshot,
        exit_reason: str,
        trigger_price: float,
        mfe_bps: float,
        mae_bps: float,
    ) -> None:
        assert self.position is not None
        position = self.position
        direction = 1.0 if position.side == "LONG" else -1.0
        fill_price = trigger_price
        if self.slippage_bps > 0:
            slippage_fraction = self.slippage_bps / 10_000.0
            if position.side == "LONG":
                fill_price *= (1.0 - slippage_fraction)
            else:
                fill_price *= (1.0 + slippage_fraction)
        final_gross_pnl = (fill_price - position.entry_price) * position.size * direction
        exit_fee = position.notional * self.fee_pct
        gross_pnl = position.realized_gross_locked + final_gross_pnl
        total_fees = position.entry_fee_paid + position.realized_exit_fees_locked + exit_fee
        pnl = gross_pnl - total_fees
        hold_seconds = max(0.0, time.time() - position.opened_at)
        realized_bps = (gross_pnl / max(position.max_notional_seen, 1e-9)) * 10_000.0
        peak_margin = position.max_notional_seen / float(self.leverage)
        roi_pct_principal = (pnl / peak_margin * 100.0) if peak_margin > 0 else None
        capture_ratio = realized_bps / mfe_bps if mfe_bps > 0 else None

        self.daily_realized_pnl += pnl
        self.last_exit_ts = time.time()
        self.exit_count += 1
        self.closed_trade_pnls.append(pnl)
        self.closed_trade_hold_seconds.append(hold_seconds)
        if capture_ratio is not None:
            self.closed_trade_captures.append(capture_ratio)
        self.realized_bps_history.append(realized_bps)
        self.exit_reason_counts[exit_reason] = self.exit_reason_counts.get(exit_reason, 0) + 1

        self._write_trade_row(
            position=position,
            exit_price=fill_price,
            gross_pnl=gross_pnl,
            fees_paid=total_fees,
            pnl=pnl,
            hold_seconds=hold_seconds,
            realized_bps=realized_bps,
            roi_pct_principal=roi_pct_principal,
            mfe_bps=mfe_bps,
            mae_bps=mae_bps,
            capture_ratio=capture_ratio,
            exit_reason=exit_reason,
            signal=signal,
        )
        self._write_markout_row(
            position=position,
            signal=signal,
            exit_reason=exit_reason,
            hold_seconds=hold_seconds,
            realized_bps=realized_bps,
            mfe_bps=mfe_bps,
            mae_bps=mae_bps,
            capture_ratio=capture_ratio,
            pnl=pnl,
        )
        self._write_event(
            "position_closed",
            trade_id=position.trade_id,
            side=position.side,
            entry_price=position.entry_price,
            exit_price=fill_price,
            pnl=pnl,
            gross_pnl=gross_pnl,
            total_fees=total_fees,
            hold_seconds=hold_seconds,
            roi_pct_principal=roi_pct_principal,
            exit_reason=exit_reason,
            active_stop_at_exit=position.active_stop_price,
            initial_stop=position.initial_stop_price,
            locked_stop_at_exit=position.locked_stop_price,
            trailing_armed_at_exit=position.trailing_armed,
            stop_distance_bps=position.stop_distance_bps,
            trail_distance_bps=position.trail_distance_bps,
            realized_bps=realized_bps,
            mfe_bps=mfe_bps,
            mae_bps=mae_bps,
            capture_ratio=capture_ratio,
            signal=position.opened_features,
        )
        self.position = None
        self.pending_candidate = None

    def _maybe_close_position(self, signal: MidSignalSnapshot) -> None:
        assert self.position is not None
        current_exit_price = self._current_exit_price(signal)
        hold_seconds = max(0.0, time.time() - self.position.opened_at)
        mfe_bps, mae_bps = self._position_mfe_bps(current_exit_price)
        self.position.last_metrics = {"mfe_bps": mfe_bps, "mae_bps": mae_bps}
        current_realized_bps = self._current_realized_bps(current_exit_price)

        if (
            hold_seconds >= self.position.failure_horizon_seconds
            and mfe_bps < self.position.failure_min_followthrough_bps
        ):
            self._close_position(signal, "failed_followthrough", current_exit_price, mfe_bps, mae_bps)
            return
        if self._flow_flip_exit(signal, current_realized_bps):
            self._close_position(signal, "flow_flip_exit", current_exit_price, mfe_bps, mae_bps)
            return
        if hold_seconds >= self.min_hold_seconds and self._breakout_failure(signal) and current_realized_bps <= 0:
            self._close_position(signal, "breakout_failure", current_exit_price, mfe_bps, mae_bps)
            return
        if self._time_stop_exit(current_realized_bps, hold_seconds):
            self._close_position(signal, "time_stop", current_exit_price, mfe_bps, mae_bps)
            return

        self._maybe_lock_break_even(signal, mfe_bps)
        self._update_trailing_stop(signal, current_exit_price, mfe_bps)
        if self.position is not None and self._add_on_ready(signal):
            if self._scale_in_position(signal):
                current_exit_price = self._current_exit_price(signal)
                current_realized_bps = self._current_realized_bps(current_exit_price)
                mfe_bps, mae_bps = self._position_mfe_bps(current_exit_price)
        if self.position is not None and self._decay_reduction_ready(signal, hold_seconds=hold_seconds, mfe_bps=mfe_bps):
            if self._scale_out_position(signal, "momentum_decay"):
                current_exit_price = self._current_exit_price(signal)
                current_realized_bps = self._current_realized_bps(current_exit_price)
                mfe_bps, mae_bps = self._position_mfe_bps(current_exit_price)

        if self.position.side == "LONG":
            stop_hit = current_exit_price <= self.position.active_stop_price
        else:
            stop_hit = current_exit_price >= self.position.active_stop_price
        if not stop_hit:
            self.position.stop_breach_count = 0
            return
        self.position.stop_breach_count += 1
        if self.position.stop_breach_count < self.stop_confirmation_ticks:
            self._write_event(
                "stop_breach_pending",
                trade_id=self.position.trade_id,
                side=self.position.side,
                breach_count=self.position.stop_breach_count,
                stop_confirmation_ticks=self.stop_confirmation_ticks,
                active_stop=self.position.active_stop_price,
                current_exit_price=current_exit_price,
            )
            return
        exit_reason = "trailing_stop_hit" if self.position.trailing_armed else "initial_stop_hit"
        self._close_position(signal, exit_reason, current_exit_price, mfe_bps, mae_bps)

    def _warm_start_prices(self, startup_microprice: float) -> int:
        if not self.warm_start_candles or self.feed is None:
            return 0
        lookback_minutes = max(
            self.confirm_impulse_window_seconds,
            self.breakout_lookback_seconds,
            self.volatility_lookback_seconds,
        ) / 60.0
        try:
            closes = self.feed.get_recent_candle_closes(lookback_minutes, interval="1m")
        except Exception as exc:
            self._write_event("warm_start_failed", error=f"{type(exc).__name__}: {exc}")
            return 0
        if not closes:
            self._write_event("warm_start_empty", lookback_minutes=lookback_minutes)
            return 0
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - (len(closes) * 60_000)
        for idx, close in enumerate(closes):
            self._append_price_sample(start_ms + (idx * 60_000), close)
        self._append_price_sample(now_ms, startup_microprice)
        self.last_sample_exchange_time_ms = now_ms if self.startup_state_entry else None
        self._write_event(
            "warm_start_loaded",
            lookback_minutes=lookback_minutes,
            warm_start_points=len(closes) + 1,
            candles_loaded=len(closes),
        )
        return len(closes) + 1

    def _wait_for_first_snapshot(self, timeout_seconds: float) -> MarketSnapshot:
        assert self.feed is not None
        item = self.feed.wait_for_next(last_seq=0, timeout_seconds=timeout_seconds)
        if item is None:
            raise TimeoutError("Timed out waiting for first market snapshot")
        snapshot, _ = item
        return snapshot

    def _handle_quote_gap_warning(self) -> Optional[Tuple[MarketSnapshot, int]]:
        now_ms = int(time.time() * 1000)
        if not self._quote_gap_active:
            self._quote_gap_active = True
            self._quote_gap_started_ms = now_ms
        latest = self.feed.latest() if self.feed is not None else None
        if latest is None:
            if self._quote_gap_started_ms == now_ms:
                self._write_event("quote_gap_warning", quote_gap_ms=None)
            return None
        snapshot, seq = latest
        gap_ms = now_ms - snapshot.quote.received_time_ms
        if self._quote_gap_started_ms == now_ms:
            self._write_event(
                "quote_gap_warning",
                quote_gap_ms=gap_ms,
                latest_bid=snapshot.quote.bid,
                latest_ask=snapshot.quote.ask,
                latest_mid=snapshot.quote.mid,
            )
            return None
        if gap_ms < self.reconnect_gap_ms:
            return None
        if (now_ms - self._last_reconnect_attempt_ms) < self.reconnect_gap_ms:
            return None
        self._last_reconnect_attempt_ms = now_ms
        return self._reconnect_feed(gap_ms, seq)

    def _reconnect_feed(self, gap_ms: int, _last_seq: int) -> Optional[Tuple[MarketSnapshot, int]]:
        self._write_event("feed_reconnect_started", quote_gap_ms=gap_ms)
        old_feed = self.feed
        if old_feed is not None:
            try:
                old_feed.close()
            except Exception:
                logging.exception("Failed to close existing feed during reconnect")
        try:
            self.feed = HyperliquidRealtimeMultiFeed(
                asset=self.asset,
                api_url=self.api_url,
                dex=self.dex,
                trade_retention_ms=self.trade_retention_ms,
            )
            item = self.feed.wait_for_next(last_seq=0, timeout_seconds=10.0)
            if item is None:
                raise TimeoutError("Timed out waiting for first snapshot after reconnect")
            snapshot, seq = item
            self._write_event(
                "feed_reconnected",
                quote_gap_ms=gap_ms,
                bid=snapshot.quote.bid,
                ask=snapshot.quote.ask,
                mid=snapshot.quote.mid,
                transport_delay_ms=snapshot.quote.transport_delay_ms,
            )
            return snapshot, seq
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._write_event(
                "feed_reconnect_failed",
                quote_gap_ms=gap_ms,
                error=self.last_error,
            )
            return None

    def _on_market_snapshot(self, market: MarketSnapshot) -> None:
        self._roll_day_if_needed()
        quote = market.quote
        now_ms = int(time.time() * 1000)
        quote_age_ms = int(_clamp_non_negative(now_ms - quote.exchange_time_ms))
        self.tick_count += 1
        self.last_quote_exchange_time_ms = quote.exchange_time_ms
        self.last_quote_transport_delay_ms = quote.transport_delay_ms
        self.quote_age_history_ms.append(float(quote_age_ms))
        self.transport_delay_history_ms.append(float(quote.transport_delay_ms))
        self.spread_history_bps.append(float(quote.spread_bps))

        if self._quote_gap_active:
            gap_duration_ms = None
            if self._quote_gap_started_ms is not None:
                gap_duration_ms = max(0, quote.received_time_ms - self._quote_gap_started_ms)
            self._quote_gap_active = False
            self._quote_gap_started_ms = None
            self._write_event(
                "quote_gap_recovered",
                gap_duration_ms=gap_duration_ms,
                bid=quote.bid,
                ask=quote.ask,
                mid=quote.mid,
            )

        self._write_event(
            "market_tick",
            bid=quote.bid,
            ask=quote.ask,
            mid=quote.mid,
            microprice=quote.microprice,
            spread_bps=quote.spread_bps,
            quote_exchange_time_ms=quote.exchange_time_ms,
            quote_received_time_ms=quote.received_time_ms,
            quote_age_ms=quote_age_ms,
            transport_delay_ms=quote.transport_delay_ms,
            trade_cache_size=len(market.trades),
        )

        if quote_age_ms > self.max_quote_age_ms:
            self.stale_quote_skips += 1
            if now_ms - self._last_stale_skip_event_ms >= self.max_quote_age_ms:
                self._last_stale_skip_event_ms = now_ms
                self._write_event(
                    "stale_quote_skipped",
                    quote_age_ms=quote_age_ms,
                    bid=quote.bid,
                    ask=quote.ask,
                    mid=quote.mid,
                    max_quote_age_ms=self.max_quote_age_ms,
                )
            return

        signal = self._sample_signal_state(market)
        if signal is not None:
            if self.position is None:
                self._maybe_enter(signal)
        if self.position is not None:
            active_signal = signal or self.last_snapshot
            if active_signal is not None:
                self._maybe_close_position(active_signal)

    def run(self) -> None:
        logging.info(
            "Starting mid-frequency oil momentum bot asset=%s sample_ms=%d fast_window_s=%.1f confirm_window_s=%.1f breakout_lookback_s=%.1f flow_window_s=%.1f min_confirm_bps=%.2f max_spread_bps=%.2f",
            self.asset,
            self.sample_ms,
            self.fast_impulse_window_seconds,
            self.confirm_impulse_window_seconds,
            self.breakout_lookback_seconds,
            self.flow_window_seconds,
            self.min_confirm_impulse_bps,
            self.max_spread_bps,
        )
        try:
            self.feed = HyperliquidRealtimeMultiFeed(
                asset=self.asset,
                api_url=self.api_url,
                dex=self.dex,
                trade_retention_ms=self.trade_retention_ms,
            )
            first_snapshot = self._wait_for_first_snapshot(timeout_seconds=10.0)
        except Exception:
            logging.exception("Startup preflight failed")
            self._write_event("startup_preflight_failed")
            return

        self.preflight_ok = True
        warm_points = self._warm_start_prices(first_snapshot.quote.microprice)
        self._write_event(
            "bot_started",
            asset=self.asset,
            api_url=self.api_url,
            dex=self.dex,
            account_balance=self.account_balance,
            startup_bid=first_snapshot.quote.bid,
            startup_ask=first_snapshot.quote.ask,
            startup_mid=first_snapshot.quote.mid,
            startup_microprice=first_snapshot.quote.microprice,
            startup_transport_delay_ms=first_snapshot.quote.transport_delay_ms,
            leverage=self.leverage,
            sample_ms=self.sample_ms,
            warm_start_points=warm_points,
            startup_state_entry=self.startup_state_entry,
            entry_confirmation_samples=self.entry_confirmation_samples,
            fast_impulse_window_seconds=self.fast_impulse_window_seconds,
            confirm_impulse_window_seconds=self.confirm_impulse_window_seconds,
            breakout_lookback_seconds=self.breakout_lookback_seconds,
            volatility_lookback_seconds=self.volatility_lookback_seconds,
            flow_window_seconds=self.flow_window_seconds,
            min_fast_impulse_bps=self.min_fast_impulse_bps,
            min_confirm_impulse_bps=self.min_confirm_impulse_bps,
            fast_vol_multiplier=self.fast_vol_multiplier,
            confirm_vol_multiplier=self.confirm_vol_multiplier,
            breakout_buffer_bps=self.breakout_buffer_bps,
            flow_imbalance_min=self.flow_imbalance_min,
            book_imbalance_min=self.book_imbalance_min,
            min_trade_count=self.min_trade_count,
            depth_levels=self.depth_levels,
            max_spread_bps=self.max_spread_bps,
            max_quote_age_ms=self.max_quote_age_ms,
            quote_gap_warn_ms=self.quote_gap_warn_ms,
            reconnect_gap_ms=self.reconnect_gap_ms,
            extreme_impulse_bps=self.extreme_impulse_bps,
            extreme_vol_bps=self.extreme_vol_bps,
            setup_score_min=self.setup_score_min,
            tactical_score_min=self.tactical_score_min,
            tactical_fast_threshold_ratio=self.tactical_fast_threshold_ratio,
            tactical_confirm_threshold_ratio=self.tactical_confirm_threshold_ratio,
            tactical_breakout_slack_bps=self.tactical_breakout_slack_bps,
            tactical_flow_multiplier=self.tactical_flow_multiplier,
            tactical_book_multiplier=self.tactical_book_multiplier,
            score_edge_min=self.score_edge_min,
            instant_entry_score_min=self.instant_entry_score_min,
            initial_stop_min_bps=self.initial_stop_min_bps,
            initial_stop_vol_multiplier=self.initial_stop_vol_multiplier,
            trail_min_bps=self.trail_min_bps,
            trail_vol_multiplier=self.trail_vol_multiplier,
            trail_arm_r=self.trail_arm_r,
            break_even_lock_r=self.break_even_lock_r,
            score_notional_boost=self.score_notional_boost,
            initial_notional_fraction=self.initial_notional_fraction,
            max_notional_fraction=self.max_notional_fraction,
            max_add_ons=self.max_add_ons,
            add_on_trigger_r=self.add_on_trigger_r,
            add_on_fraction=self.add_on_fraction,
            add_on_score_min=self.add_on_score_min,
            add_on_breakout_extension_bps=self.add_on_breakout_extension_bps,
            add_on_flow_multiplier=self.add_on_flow_multiplier,
            add_on_book_multiplier=self.add_on_book_multiplier,
            add_on_min_seconds=self.add_on_min_seconds,
            max_reductions=self.max_reductions,
            de_risk_fraction=self.de_risk_fraction,
            de_risk_score_threshold=self.de_risk_score_threshold,
            de_risk_confirm_ratio=self.de_risk_confirm_ratio,
            de_risk_flow_multiplier=self.de_risk_flow_multiplier,
            runner_core_fraction=self.runner_core_fraction,
            failure_hold_seconds=self.failure_hold_seconds,
            failure_min_followthrough_bps=self.failure_min_followthrough_bps,
            flow_flip_exit_imbalance=self.flow_flip_exit_imbalance,
            book_flip_exit_imbalance=self.book_flip_exit_imbalance,
            breakout_fail_buffer_bps=self.breakout_fail_buffer_bps,
            max_hold_seconds=self.max_hold_seconds,
            time_stop_min_r=self.time_stop_min_r,
            stop_confirmation_ticks=self.stop_confirmation_ticks,
            slippage_bps=self.slippage_bps,
            fee_bps=self.fee_bps,
            equity_risk_pct=self.equity_risk_pct * 100.0,
            cooldown_seconds=self.cooldown_seconds,
        )

        last_seq = 0
        while True:
            try:
                assert self.feed is not None
                item = self.feed.wait_for_next(
                    last_seq=last_seq,
                    timeout_seconds=self.quote_gap_warn_ms / 1000.0,
                )
                if item is None:
                    reconnect_item = self._handle_quote_gap_warning()
                    if reconnect_item is not None:
                        snapshot, last_seq = reconnect_item
                        self._on_market_snapshot(snapshot)
                    continue
                snapshot, last_seq = item
                self._on_market_snapshot(snapshot)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self.poll_error_count += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                logging.exception("Realtime loop error")
                self._write_event("polling_error", error=self.last_error)

    def close(self) -> None:
        if self.feed is not None:
            self.feed.close()

    def finalize(self, reason: str) -> Optional[Path]:
        if self._finalized:
            return self._report_path
        self._finalized = True
        self.close()

        end_ts = time.time()
        duration_seconds = max(0.0, end_ts - self.started_at_ts)
        closed_trades = len(self.closed_trade_pnls)
        total_realized_pnl = sum(self.closed_trade_pnls)
        wins = sum(1 for pnl in self.closed_trade_pnls if pnl > 0)
        win_rate_pct = (wins / closed_trades * 100.0) if closed_trades else 0.0
        avg_hold_seconds = (
            sum(self.closed_trade_hold_seconds) / closed_trades if closed_trades else 0.0
        )
        avg_capture = (
            sum(self.closed_trade_captures) / len(self.closed_trade_captures)
            if self.closed_trade_captures
            else 0.0
        )
        quote_age_p95 = self._pctl(self.quote_age_history_ms, 0.95)
        transport_p95 = self._pctl(self.transport_delay_history_ms, 0.95)
        spread_p95 = self._pctl(self.spread_history_bps, 0.95)
        avg_realized_bps = (
            sum(self.realized_bps_history) / len(self.realized_bps_history)
            if self.realized_bps_history
            else 0.0
        )

        report_name = (
            f"run_{datetime.fromtimestamp(self.started_at_ts, tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}_{self.asset.replace(':', '_')}.md"
        )
        report_path = self.report_dir / report_name
        lines = [
            "# Vector Momentum Engine Run Report",
            "",
            f"- Asset: `{self.asset}`",
            f"- Stop reason: `{reason}`",
            f"- Start (UTC): `{datetime.fromtimestamp(self.started_at_ts, tz=timezone.utc).isoformat()}`",
            f"- End (UTC): `{datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat()}`",
            f"- Duration (s): `{duration_seconds:.1f}`",
            "",
            "## Configuration",
            f"- Sample ms: `{self.sample_ms}`",
            f"- Fast impulse window seconds: `{self.fast_impulse_window_seconds}`",
            f"- Confirm impulse window seconds: `{self.confirm_impulse_window_seconds}`",
            f"- Breakout lookback seconds: `{self.breakout_lookback_seconds}`",
            f"- Volatility lookback seconds: `{self.volatility_lookback_seconds}`",
            f"- Flow window seconds: `{self.flow_window_seconds}`",
            f"- Min fast impulse bps: `{self.min_fast_impulse_bps}`",
            f"- Min confirm impulse bps: `{self.min_confirm_impulse_bps}`",
            f"- Setup score min: `{self.setup_score_min}`",
            f"- Tactical score min: `{self.tactical_score_min}`",
            f"- Tactical fast threshold ratio: `{self.tactical_fast_threshold_ratio}`",
            f"- Tactical confirm threshold ratio: `{self.tactical_confirm_threshold_ratio}`",
            f"- Tactical breakout slack bps: `{self.tactical_breakout_slack_bps}`",
            f"- Score edge min: `{self.score_edge_min}`",
            f"- Instant entry score min: `{self.instant_entry_score_min}`",
            f"- Extreme impulse bps: `{self.extreme_impulse_bps}`",
            f"- Extreme vol bps: `{self.extreme_vol_bps}`",
            f"- Initial stop min bps: `{self.initial_stop_min_bps}`",
            f"- Trail min bps: `{self.trail_min_bps}`",
            f"- Score notional boost: `{self.score_notional_boost}`",
            f"- Initial notional fraction: `{self.initial_notional_fraction}`",
            f"- Max notional fraction: `{self.max_notional_fraction}`",
            f"- Max add-ons: `{self.max_add_ons}`",
            f"- Add-on trigger R: `{self.add_on_trigger_r}`",
            f"- Add-on fraction: `{self.add_on_fraction}`",
            f"- Add-on score min: `{self.add_on_score_min}`",
            f"- Add-on breakout extension bps: `{self.add_on_breakout_extension_bps}`",
            f"- Add-on min seconds: `{self.add_on_min_seconds}`",
            f"- Max reductions: `{self.max_reductions}`",
            f"- De-risk fraction: `{self.de_risk_fraction}`",
            f"- De-risk score threshold: `{self.de_risk_score_threshold}`",
            f"- De-risk confirm ratio: `{self.de_risk_confirm_ratio}`",
            f"- De-risk flow multiplier: `{self.de_risk_flow_multiplier}`",
            f"- Runner core fraction: `{self.runner_core_fraction}`",
            f"- Failure hold seconds: `{self.failure_hold_seconds}`",
            f"- Max hold seconds: `{self.max_hold_seconds}`",
            "",
            "## Feed Health",
            f"- Startup preflight: `{'ok' if self.preflight_ok else 'failed'}`",
            f"- Ticks captured: `{self.tick_count}`",
            f"- Samples captured: `{self.sample_count}`",
            f"- Loop errors: `{self.poll_error_count}`",
            f"- Stale quote skips: `{self.stale_quote_skips}`",
            f"- Quote age p95 ms: `{quote_age_p95 if quote_age_p95 is not None else 'n/a'}`",
            f"- Transport delay p95 ms: `{transport_p95 if transport_p95 is not None else 'n/a'}`",
            f"- Spread p95 bps: `{spread_p95 if spread_p95 is not None else 'n/a'}`",
            "",
            "## Trading Activity",
            f"- Signal candidates: `{self.signal_candidate_count}`",
            f"- Signals confirmed: `{self.signal_confirmed_count}`",
            f"- Entries: `{self.entry_count}`",
            f"- Exits: `{self.exit_count}`",
            f"- Open position at stop: `{'yes' if self.position is not None else 'no'}`",
            "",
            "## Performance",
            f"- Closed trades: `{closed_trades}`",
            f"- Total realized PnL: `{total_realized_pnl:.4f}`",
            f"- Win rate: `{win_rate_pct:.2f}%`",
            f"- Average hold seconds: `{avg_hold_seconds:.2f}`",
            f"- Average realized bps: `{avg_realized_bps:.2f}`",
            f"- Average capture ratio: `{avg_capture:.2f}`",
            "",
            "## Exit Reasons",
        ]
        if self.exit_reason_counts:
            for key in sorted(self.exit_reason_counts):
                lines.append(f"- `{key}`: `{self.exit_reason_counts[key]}`")
        else:
            lines.append("- None")

        try:
            report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            logging.exception("Failed writing mid-momentum summary report")
            report_path = None

        self._write_event(
            "bot_stopped",
            stop_reason=reason,
            duration_seconds=duration_seconds,
            closed_trades=closed_trades,
            total_realized_pnl=total_realized_pnl,
            win_rate_pct=win_rate_pct,
            avg_hold_seconds=avg_hold_seconds,
            report_path=str(report_path) if report_path is not None else None,
        )
        self._report_path = report_path
        return report_path

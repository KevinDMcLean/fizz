from __future__ import annotations

import csv
import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Sequence, Tuple

from pa_pump_pro_core import (
    HyperliquidRealtimeMultiFeed,
    MarketSnapshot,
    latest_reference_price,
    compute_book_imbalance,
    compute_realized_vol_bps,
    compute_trade_flow,
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


@dataclass
class ExhaustionAnchor:
    side: str
    first_seen_exchange_time_ms: int
    last_update_exchange_time_ms: int
    anchor_price: float
    extreme_price: float
    features: Dict[str, object]


@dataclass
class ExhaustionSnapshot:
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
    dynamic_fast_exhaustion_bps: float
    dynamic_confirm_exhaustion_bps: float
    recent_vol_bps: float
    book_imbalance: float
    bid_depth: float
    ask_depth: float
    flow_imbalance: float
    buy_volume: float
    sell_volume: float
    trade_count: int
    trade_rate_per_second: float
    recent_high: float
    recent_low: float
    rebound_from_low_bps: float
    pullback_from_high_bps: float
    upside_exhausted: bool
    downside_exhausted: bool
    long_anchor_live: bool
    short_anchor_live: bool
    long_rebound_ok: bool
    short_rebound_ok: bool
    long_flow_flip_ok: bool
    short_flow_flip_ok: bool
    long_book_flip_ok: bool
    short_book_flip_ok: bool
    long_trade_count_ok: bool
    short_trade_count_ok: bool
    spread_ok: bool
    regime: str
    long_reversal_score: float
    short_reversal_score: float
    long_ready: bool
    short_ready: bool
    anchor_side: Optional[str]
    anchor_age_seconds: Optional[float]
    anchor_price: Optional[float]
    anchor_extreme_price: Optional[float]
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
    count: int
    first_seen_exchange_time_ms: int
    first_seen_ts: float
    features: Dict[str, object]


@dataclass
class ReversalPosition:
    trade_id: int
    side: str
    entry_price: float
    size: float
    notional: float
    required_margin: float
    entry_fee_paid: float
    initial_stop_price: float
    active_stop_price: float
    take_profit_price: float
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
    anchor_extreme_price: float
    failure_horizon_seconds: float
    failure_min_followthrough_bps: float
    break_even_lock_r: float
    max_hold_seconds: float
    last_metrics: Dict[str, float] = field(default_factory=dict)


class ProExhaustionReversalBot:
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
        min_fast_exhaustion_bps: float,
        min_confirm_exhaustion_bps: float,
        fast_vol_multiplier: float,
        confirm_vol_multiplier: float,
        exhaustion_flow_min: float,
        exhaustion_book_min: float,
        reversal_flow_min: float,
        reversal_book_min: float,
        rebound_confirm_bps: float,
        reversal_window_seconds: float,
        min_trade_count: int,
        depth_levels: int,
        max_spread_bps: float,
        max_quote_age_ms: int,
        quote_gap_warn_ms: int,
        reconnect_gap_ms: int,
        setup_score_min: float,
        initial_stop_min_bps: float,
        initial_stop_vol_multiplier: float,
        stop_anchor_buffer_bps: float,
        take_profit_r: float,
        min_take_profit_bps: float,
        trail_min_bps: float,
        trail_vol_multiplier: float,
        trail_arm_r: float,
        break_even_lock_r: float,
        failure_hold_seconds: float,
        failure_min_followthrough_bps: float,
        retest_extreme_buffer_bps: float,
        adverse_flow_exit_imbalance: float,
        adverse_book_exit_imbalance: float,
        max_hold_seconds: float,
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
        self.min_fast_exhaustion_bps = min_fast_exhaustion_bps
        self.min_confirm_exhaustion_bps = min_confirm_exhaustion_bps
        self.fast_vol_multiplier = fast_vol_multiplier
        self.confirm_vol_multiplier = confirm_vol_multiplier
        self.exhaustion_flow_min = exhaustion_flow_min
        self.exhaustion_book_min = exhaustion_book_min
        self.reversal_flow_min = reversal_flow_min
        self.reversal_book_min = reversal_book_min
        self.rebound_confirm_bps = rebound_confirm_bps
        self.reversal_window_seconds = reversal_window_seconds
        self.min_trade_count = min_trade_count
        self.depth_levels = depth_levels
        self.max_spread_bps = max_spread_bps
        self.max_quote_age_ms = max_quote_age_ms
        self.quote_gap_warn_ms = quote_gap_warn_ms
        self.reconnect_gap_ms = reconnect_gap_ms
        self.setup_score_min = setup_score_min
        self.initial_stop_min_bps = initial_stop_min_bps
        self.initial_stop_vol_multiplier = initial_stop_vol_multiplier
        self.stop_anchor_buffer_bps = stop_anchor_buffer_bps
        self.take_profit_r = take_profit_r
        self.min_take_profit_bps = min_take_profit_bps
        self.trail_min_bps = trail_min_bps
        self.trail_vol_multiplier = trail_vol_multiplier
        self.trail_arm_r = trail_arm_r
        self.break_even_lock_r = break_even_lock_r
        self.failure_hold_seconds = failure_hold_seconds
        self.failure_min_followthrough_bps = failure_min_followthrough_bps
        self.retest_extreme_buffer_bps = retest_extreme_buffer_bps
        self.adverse_flow_exit_imbalance = adverse_flow_exit_imbalance
        self.adverse_book_exit_imbalance = adverse_book_exit_imbalance
        self.max_hold_seconds = max_hold_seconds
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
            raise ValueError("Breakout lookback must exceed confirm impulse window.")
        if self.reversal_window_seconds <= 0:
            raise ValueError("Reversal window must be > 0.")
        if self.max_quote_age_ms < 50:
            raise ValueError("Max quote age ms must be >= 50.")
        if self.quote_gap_warn_ms < self.max_quote_age_ms:
            raise ValueError("Quote gap warn ms must be >= max quote age ms.")
        if self.reconnect_gap_ms < self.quote_gap_warn_ms:
            raise ValueError("Reconnect gap ms must be >= quote gap warn ms.")
        if self.initial_stop_min_bps <= 0 or self.trail_min_bps <= 0:
            raise ValueError("Stop distances must be > 0.")
        if self.stop_confirmation_ticks < 1:
            raise ValueError("Stop confirmation ticks must be >= 1.")
        if self.failure_hold_seconds <= 0 or self.max_hold_seconds <= 0:
            raise ValueError("Failure hold and max hold seconds must be > 0.")

        self.feed: Optional[HyperliquidRealtimeMultiFeed] = None
        self.position: Optional[ReversalPosition] = None
        self.anchor: Optional[ExhaustionAnchor] = None
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
        self.anchor_count = 0
        self.last_error: Optional[str] = None
        self.last_snapshot: Optional[ExhaustionSnapshot] = None
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
                        "exit_reason",
                        "entry_fast_impulse_bps",
                        "entry_confirm_impulse_bps",
                        "entry_long_reversal_score",
                        "entry_short_reversal_score",
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
            logging.exception("Failed writing exhaustion event JSONL")

    def _write_sample(self, snapshot: ExhaustionSnapshot) -> None:
        current_unrealized = 0.0
        position_side: Optional[str] = None
        trade_id: Optional[int] = None
        active_stop: Optional[float] = None
        locked_stop: Optional[float] = None
        take_profit: Optional[float] = None
        entry_price: Optional[float] = None
        hold_seconds: Optional[float] = None
        trail_armed = False
        if self.position is not None:
            position_side = self.position.side
            trade_id = self.position.trade_id
            active_stop = self.position.active_stop_price
            locked_stop = self.position.locked_stop_price
            take_profit = self.position.take_profit_price
            entry_price = self.position.entry_price
            hold_seconds = max(0.0, time.time() - self.position.opened_at)
            trail_armed = self.position.trailing_armed
            current_exit_price = self._current_exit_price(snapshot)
            direction = 1.0 if self.position.side == "LONG" else -1.0
            gross = (current_exit_price - self.position.entry_price) * self.position.size * direction
            current_unrealized = gross - self.position.entry_fee_paid
        payload = {
            "timestamp_utc": _utc_iso(),
            "asset": self.asset,
            **snapshot.to_dict(),
            "state": self._state_label(snapshot),
            "candidate_side": self.pending_candidate.side if self.pending_candidate is not None else None,
            "candidate_count": self.pending_candidate.count if self.pending_candidate is not None else 0,
            "position_side": position_side,
            "open_trade_id": trade_id,
            "entry_price": entry_price,
            "active_stop": active_stop,
            "locked_stop": locked_stop,
            "take_profit_price": take_profit,
            "trail_armed": trail_armed,
            "hold_seconds": hold_seconds,
            "unrealized_pnl": current_unrealized,
        }
        try:
            with self.samples_jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except Exception:
            logging.exception("Failed writing exhaustion sample JSONL")

    def _write_trade_row(
        self,
        *,
        position: ReversalPosition,
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
        snapshot: ExhaustionSnapshot,
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
                        f"{position.size:.8f}",
                        f"{position.notional:.8f}",
                        f"{position.required_margin:.8f}",
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
                        exit_reason,
                        f"{float(opened.get('fast_impulse_bps', 0.0)):.6f}",
                        f"{float(opened.get('confirm_impulse_bps', 0.0)):.6f}",
                        f"{float(opened.get('long_reversal_score', 0.0)):.6f}",
                        f"{float(opened.get('short_reversal_score', 0.0)):.6f}",
                        f"{float(opened.get('flow_imbalance', 0.0)):.6f}",
                        f"{float(opened.get('book_imbalance', 0.0)):.6f}",
                        f"{float(opened.get('recent_vol_bps', 0.0)):.6f}",
                        f"{snapshot.flow_imbalance:.6f}",
                        f"{snapshot.book_imbalance:.6f}",
                        f"{snapshot.recent_vol_bps:.6f}",
                    ]
                )
        except Exception:
            logging.exception("Failed writing exhaustion trades CSV")

    def _write_markout_row(
        self,
        *,
        position: ReversalPosition,
        snapshot: ExhaustionSnapshot,
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
            "exit_reason": exit_reason,
            "hold_seconds": hold_seconds,
            "realized_bps": realized_bps,
            "mfe_bps": mfe_bps,
            "mae_bps": mae_bps,
            "capture_ratio": capture_ratio,
            "pnl": pnl,
            "entry_fast_impulse_bps": opened.get("fast_impulse_bps"),
            "entry_confirm_impulse_bps": opened.get("confirm_impulse_bps"),
            "entry_long_reversal_score": opened.get("long_reversal_score"),
            "entry_short_reversal_score": opened.get("short_reversal_score"),
            "entry_flow_imbalance": opened.get("flow_imbalance"),
            "entry_book_imbalance": opened.get("book_imbalance"),
            "entry_recent_vol_bps": opened.get("recent_vol_bps"),
            "entry_regime": opened.get("regime"),
            "exit_flow_imbalance": snapshot.flow_imbalance,
            "exit_book_imbalance": snapshot.book_imbalance,
            "exit_recent_vol_bps": snapshot.recent_vol_bps,
            "exit_regime": snapshot.regime,
        }
        try:
            with self.markouts_jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except Exception:
            logging.exception("Failed writing exhaustion markout JSONL")

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

    def _score_reversal(
        self,
        *,
        rebound_bps: float,
        flow_value: float,
        book_value: float,
        trade_count: int,
        spread_bps: float,
        direction: int,
        spread_ok: bool,
    ) -> float:
        rebound_component = self._normalize_component(rebound_bps, max(self.rebound_confirm_bps, 0.5))
        flow_component = self._normalize_component(direction * flow_value, max(self.reversal_flow_min, 1e-6))
        book_component = self._normalize_component(direction * book_value, max(self.reversal_book_min, 1e-6))
        trade_component = self._normalize_component(float(trade_count), float(max(self.min_trade_count, 1)))
        spread_component = _clamp(1.0 - (spread_bps / max(self.max_spread_bps, 0.1)), 0.0, 1.0)
        raw = 0.34 * rebound_component + 0.26 * flow_component + 0.16 * book_component + 0.12 * trade_component + 0.12 * spread_component
        if not spread_ok:
            raw *= 0.2
        return round(_clamp(raw, 0.0, 1.0) * 100.0, 2)

    def _detect_exhaustion(self, *, signal: Dict[str, float], direction: int) -> bool:
        if direction > 0:
            return (
                signal["fast_impulse_bps"] >= signal["dynamic_fast_exhaustion_bps"]
                and signal["confirm_impulse_bps"] >= signal["dynamic_confirm_exhaustion_bps"]
                and signal["flow_imbalance"] >= self.exhaustion_flow_min
                and signal["book_imbalance"] >= self.exhaustion_book_min
                and signal["trade_count"] >= self.min_trade_count
            )
        return (
            signal["fast_impulse_bps"] <= -signal["dynamic_fast_exhaustion_bps"]
            and signal["confirm_impulse_bps"] <= -signal["dynamic_confirm_exhaustion_bps"]
            and signal["flow_imbalance"] <= -self.exhaustion_flow_min
            and signal["book_imbalance"] <= -self.exhaustion_book_min
            and signal["trade_count"] >= self.min_trade_count
        )

    def _update_anchor(self, signal_data: Dict[str, float], sample_exchange_time_ms: int, mid: float) -> None:
        now_side: Optional[str] = None
        if self._detect_exhaustion(signal=signal_data, direction=1):
            now_side = "SHORT"
        elif self._detect_exhaustion(signal=signal_data, direction=-1):
            now_side = "LONG"

        if now_side is None:
            if self.anchor is not None and (
                sample_exchange_time_ms - self.anchor.last_update_exchange_time_ms
            ) > int(self.reversal_window_seconds * 1000.0):
                self._write_event(
                    "anchor_expired",
                    side=self.anchor.side,
                    anchor_price=self.anchor.anchor_price,
                    anchor_extreme_price=self.anchor.extreme_price,
                )
                self.anchor = None
            return

        if self.anchor is None or self.anchor.side != now_side:
            self.anchor = ExhaustionAnchor(
                side=now_side,
                first_seen_exchange_time_ms=sample_exchange_time_ms,
                last_update_exchange_time_ms=sample_exchange_time_ms,
                anchor_price=mid,
                extreme_price=mid,
                features=signal_data.copy(),
            )
            self.anchor_count += 1
            self._write_event(
                "anchor_opened",
                side=now_side,
                anchor_price=mid,
                **signal_data,
            )
            return

        self.anchor.last_update_exchange_time_ms = sample_exchange_time_ms
        if now_side == "LONG":
            if mid < self.anchor.extreme_price:
                self.anchor.extreme_price = mid
                self.anchor.anchor_price = mid
                self.anchor.features = signal_data.copy()
                self._write_event("anchor_updated", side=now_side, anchor_extreme_price=mid, **signal_data)
        else:
            if mid > self.anchor.extreme_price:
                self.anchor.extreme_price = mid
                self.anchor.anchor_price = mid
                self.anchor.features = signal_data.copy()
                self._write_event("anchor_updated", side=now_side, anchor_extreme_price=mid, **signal_data)

    def _build_signal_snapshot(self, market: MarketSnapshot) -> Optional[ExhaustionSnapshot]:
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
        dynamic_fast_exhaustion_bps = max(self.min_fast_exhaustion_bps, self.fast_vol_multiplier * recent_vol_bps)
        dynamic_confirm_exhaustion_bps = max(
            self.min_confirm_exhaustion_bps,
            self.confirm_vol_multiplier * recent_vol_bps,
        )
        fast_impulse_bps = _bps_from_prices(quote.microprice, fast_ref)
        confirm_impulse_bps = _bps_from_prices(quote.microprice, confirm_ref)
        book_imbalance, bid_depth, ask_depth = compute_book_imbalance(market.book, self.depth_levels)
        flow = compute_trade_flow(
            market.trades,
            since_ms=quote.exchange_time_ms - int(self.flow_window_seconds * 1000.0),
        )

        recent_high = max(prior_prices)
        recent_low = min(prior_prices)
        rebound_from_low_bps = _bps_from_prices(quote.mid, recent_low)
        pullback_from_high_bps = _bps_from_prices(recent_high, quote.mid)
        spread_ok = quote.spread_bps <= self.max_spread_bps

        signal_data = {
            "fast_impulse_bps": fast_impulse_bps,
            "confirm_impulse_bps": confirm_impulse_bps,
            "dynamic_fast_exhaustion_bps": dynamic_fast_exhaustion_bps,
            "dynamic_confirm_exhaustion_bps": dynamic_confirm_exhaustion_bps,
            "recent_vol_bps": recent_vol_bps,
            "book_imbalance": book_imbalance,
            "flow_imbalance": flow["imbalance"],
            "trade_count": int(flow["count"]),
            "trade_rate_per_second": flow["trade_rate_per_second"],
            "recent_high": recent_high,
            "recent_low": recent_low,
            "rebound_from_low_bps": rebound_from_low_bps,
            "pullback_from_high_bps": pullback_from_high_bps,
            "spread_bps": quote.spread_bps,
        }
        self._update_anchor(signal_data, quote.exchange_time_ms, quote.mid)

        anchor_side = self.anchor.side if self.anchor is not None else None
        anchor_age_seconds = None
        anchor_price = None
        anchor_extreme_price = None
        long_anchor_live = False
        short_anchor_live = False
        long_rebound_ok = False
        short_rebound_ok = False
        long_flow_flip_ok = False
        short_flow_flip_ok = False
        long_book_flip_ok = False
        short_book_flip_ok = False

        if self.anchor is not None:
            anchor_age_seconds = max(
                0.0,
                (quote.exchange_time_ms - self.anchor.first_seen_exchange_time_ms) / 1000.0,
            )
            anchor_price = self.anchor.anchor_price
            anchor_extreme_price = self.anchor.extreme_price
            if self.anchor.side == "LONG":
                long_anchor_live = anchor_age_seconds <= self.reversal_window_seconds
                long_rebound_ok = _bps_from_prices(quote.mid, self.anchor.extreme_price) >= self.rebound_confirm_bps
                long_flow_flip_ok = flow["imbalance"] >= self.reversal_flow_min
                long_book_flip_ok = book_imbalance >= -self.reversal_book_min
            else:
                short_anchor_live = anchor_age_seconds <= self.reversal_window_seconds
                short_rebound_ok = _bps_from_prices(self.anchor.extreme_price, quote.mid) >= self.rebound_confirm_bps
                short_flow_flip_ok = flow["imbalance"] <= -self.reversal_flow_min
                short_book_flip_ok = book_imbalance <= self.reversal_book_min

        upside_exhausted = self._detect_exhaustion(signal=signal_data, direction=1)
        downside_exhausted = self._detect_exhaustion(signal=signal_data, direction=-1)
        long_trade_count_ok = int(flow["count"]) >= self.min_trade_count
        short_trade_count_ok = int(flow["count"]) >= self.min_trade_count

        long_reversal_score = self._score_reversal(
            rebound_bps=_bps_from_prices(quote.mid, anchor_extreme_price) if long_anchor_live and anchor_extreme_price else 0.0,
            flow_value=flow["imbalance"],
            book_value=book_imbalance,
            trade_count=int(flow["count"]),
            spread_bps=quote.spread_bps,
            direction=1,
            spread_ok=spread_ok,
        )
        short_reversal_score = self._score_reversal(
            rebound_bps=_bps_from_prices(anchor_extreme_price, quote.mid) if short_anchor_live and anchor_extreme_price else 0.0,
            flow_value=flow["imbalance"],
            book_value=book_imbalance,
            trade_count=int(flow["count"]),
            spread_bps=quote.spread_bps,
            direction=-1,
            spread_ok=spread_ok,
        )

        if not spread_ok:
            regime = "blocked"
        elif long_anchor_live or short_anchor_live:
            regime = "armed"
        elif upside_exhausted or downside_exhausted:
            regime = "stretched"
        else:
            regime = "idle"

        long_ready = (
            spread_ok
            and long_anchor_live
            and long_rebound_ok
            and long_flow_flip_ok
            and long_book_flip_ok
            and long_trade_count_ok
            and long_reversal_score >= self.setup_score_min
        )
        short_ready = (
            spread_ok
            and short_anchor_live
            and short_rebound_ok
            and short_flow_flip_ok
            and short_book_flip_ok
            and short_trade_count_ok
            and short_reversal_score >= self.setup_score_min
        )

        return ExhaustionSnapshot(
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
            dynamic_fast_exhaustion_bps=dynamic_fast_exhaustion_bps,
            dynamic_confirm_exhaustion_bps=dynamic_confirm_exhaustion_bps,
            recent_vol_bps=recent_vol_bps,
            book_imbalance=book_imbalance,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            flow_imbalance=flow["imbalance"],
            buy_volume=flow["buy_volume"],
            sell_volume=flow["sell_volume"],
            trade_count=int(flow["count"]),
            trade_rate_per_second=flow["trade_rate_per_second"],
            recent_high=recent_high,
            recent_low=recent_low,
            rebound_from_low_bps=rebound_from_low_bps,
            pullback_from_high_bps=pullback_from_high_bps,
            upside_exhausted=upside_exhausted,
            downside_exhausted=downside_exhausted,
            long_anchor_live=long_anchor_live,
            short_anchor_live=short_anchor_live,
            long_rebound_ok=long_rebound_ok,
            short_rebound_ok=short_rebound_ok,
            long_flow_flip_ok=long_flow_flip_ok,
            short_flow_flip_ok=short_flow_flip_ok,
            long_book_flip_ok=long_book_flip_ok,
            short_book_flip_ok=short_book_flip_ok,
            long_trade_count_ok=long_trade_count_ok,
            short_trade_count_ok=short_trade_count_ok,
            spread_ok=spread_ok,
            regime=regime,
            long_reversal_score=long_reversal_score,
            short_reversal_score=short_reversal_score,
            long_ready=long_ready,
            short_ready=short_ready,
            anchor_side=anchor_side,
            anchor_age_seconds=anchor_age_seconds,
            anchor_price=anchor_price,
            anchor_extreme_price=anchor_extreme_price,
            sample_count=len(self.price_samples),
        )

    def _state_label(self, snapshot: Optional[ExhaustionSnapshot]) -> str:
        if self.position is not None:
            return "in_position"
        if self.last_exit_ts is not None and (time.time() - self.last_exit_ts) < self.cooldown_seconds:
            return "cooldown"
        if snapshot is None:
            return "idle"
        if self.pending_candidate is not None:
            return "armed"
        if snapshot.regime == "blocked":
            return "blocked"
        if snapshot.regime == "stretched":
            return "watch"
        if snapshot.regime == "armed":
            return "reversal_watch"
        return "idle"

    def _sample_signal_state(self, market: MarketSnapshot) -> Optional[ExhaustionSnapshot]:
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
        self.long_score_history.append(snapshot.long_reversal_score)
        self.short_score_history.append(snapshot.short_reversal_score)
        self._write_sample(snapshot)
        return snapshot

    def _update_candidate(self, snapshot: ExhaustionSnapshot) -> Optional[CandidateState]:
        side: Optional[str] = None
        if snapshot.long_ready and not snapshot.short_ready:
            side = "LONG"
        elif snapshot.short_ready and not snapshot.long_ready:
            side = "SHORT"
        elif snapshot.long_ready and snapshot.short_ready:
            side = "LONG" if snapshot.long_reversal_score >= snapshot.short_reversal_score else "SHORT"
        else:
            self.pending_candidate = None
            return None

        if self.pending_candidate is None or self.pending_candidate.side != side:
            self.pending_candidate = CandidateState(
                side=side,
                count=1,
                first_seen_exchange_time_ms=snapshot.sample_exchange_time_ms,
                first_seen_ts=time.time(),
                features=snapshot.to_dict(),
            )
            self.signal_candidate_count += 1
            self._write_event(
                "signal_candidate",
                side=side,
                candidate_count=1,
                **snapshot.to_dict(),
            )
            return self.pending_candidate
        self.pending_candidate.count += 1
        self.pending_candidate.features = snapshot.to_dict()
        return self.pending_candidate

    def _compute_stop_distance_bps(self, snapshot: ExhaustionSnapshot) -> float:
        return max(
            self.initial_stop_min_bps,
            self.initial_stop_vol_multiplier * max(snapshot.recent_vol_bps, snapshot.spread_bps),
            self.rebound_confirm_bps * 0.8,
        )

    def _compute_trail_distance_bps(self, snapshot: ExhaustionSnapshot) -> float:
        return max(
            self.trail_min_bps,
            self.trail_vol_multiplier * max(snapshot.recent_vol_bps, snapshot.spread_bps),
        )

    def _open_position(self, snapshot: ExhaustionSnapshot, side: str, reason: str) -> None:
        now_ts = time.time()
        if not self._entry_allowed(now_ts):
            return
        if self.anchor is None or self.anchor.side != side:
            return
        stop_distance_bps = self._compute_stop_distance_bps(snapshot)
        trail_distance_bps = self._compute_trail_distance_bps(snapshot)
        price_stop_fraction = stop_distance_bps / 10_000.0
        risk_amount = self.account_balance * self.equity_risk_pct
        notional_target = risk_amount / max(price_stop_fraction, 1e-9)
        notional = min(notional_target, self.max_notional)
        entry_price = snapshot.ask if side == "LONG" else snapshot.bid
        size = notional / max(entry_price, 1e-9)
        required_margin = notional / float(self.leverage)
        entry_fee = notional * self.fee_pct
        entry_stop = (
            entry_price * (1.0 - price_stop_fraction)
            if side == "LONG"
            else entry_price * (1.0 + price_stop_fraction)
        )
        anchor_stop = (
            self.anchor.extreme_price * (1.0 - (self.stop_anchor_buffer_bps / 10_000.0))
            if side == "LONG"
            else self.anchor.extreme_price * (1.0 + (self.stop_anchor_buffer_bps / 10_000.0))
        )
        stop_price = min(entry_stop, anchor_stop) if side == "LONG" else max(entry_stop, anchor_stop)
        take_profit_bps = max(self.min_take_profit_bps, stop_distance_bps * self.take_profit_r)
        take_profit_price = (
            entry_price * (1.0 + (take_profit_bps / 10_000.0))
            if side == "LONG"
            else entry_price * (1.0 - (take_profit_bps / 10_000.0))
        )
        best_exit_price_seen = snapshot.bid if side == "LONG" else snapshot.ask
        trail_arm_bps = max(self.failure_min_followthrough_bps, stop_distance_bps * self.trail_arm_r)

        self.trade_seq += 1
        self.position = ReversalPosition(
            trade_id=self.trade_seq,
            side=side,
            entry_price=entry_price,
            size=size,
            notional=notional,
            required_margin=required_margin,
            entry_fee_paid=entry_fee,
            initial_stop_price=stop_price,
            active_stop_price=stop_price,
            take_profit_price=take_profit_price,
            stop_distance_bps=stop_distance_bps,
            trail_distance_bps=trail_distance_bps,
            trail_arm_bps=trail_arm_bps,
            best_exit_price_seen=best_exit_price_seen,
            worst_exit_price_seen=best_exit_price_seen,
            stop_breach_count=0,
            trailing_armed=False,
            locked_stop_price=None,
            opened_at=now_ts,
            opened_quote_exchange_time_ms=snapshot.sample_exchange_time_ms,
            opened_features=snapshot.to_dict(),
            entry_reason=reason,
            anchor_extreme_price=self.anchor.extreme_price,
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
            stop_distance_bps=stop_distance_bps,
            trail_distance_bps=trail_distance_bps,
            initial_stop=stop_price,
            take_profit_price=take_profit_price,
            trail_arm_bps=trail_arm_bps,
            signal_reason=reason,
            signal=self.position.opened_features,
        )
        self.anchor = None

    def _maybe_enter(self, snapshot: ExhaustionSnapshot) -> None:
        candidate = self._update_candidate(snapshot)
        if candidate is None:
            return
        if (
            candidate.first_seen_exchange_time_ms == snapshot.sample_exchange_time_ms
            and not self.startup_state_entry
        ):
            return
        if candidate.count < self.entry_confirmation_samples:
            return
        score = snapshot.long_reversal_score if candidate.side == "LONG" else snapshot.short_reversal_score
        reason = (
            f"{candidate.side} exhaustion_reversal "
            f"fast_bps={snapshot.fast_impulse_bps:.2f} "
            f"confirm_bps={snapshot.confirm_impulse_bps:.2f} "
            f"rebound_low_bps={snapshot.rebound_from_low_bps:.2f} "
            f"pullback_high_bps={snapshot.pullback_from_high_bps:.2f} "
            f"flow={snapshot.flow_imbalance:.3f} "
            f"book={snapshot.book_imbalance:.3f} "
            f"score={score:.2f}"
        )
        self.signal_confirmed_count += 1
        self._write_event(
            "signal_confirmed",
            side=candidate.side,
            candidate_count=candidate.count,
            **snapshot.to_dict(),
        )
        self._open_position(snapshot, candidate.side, reason)

    def _current_exit_price(self, snapshot: ExhaustionSnapshot) -> float:
        assert self.position is not None
        return snapshot.bid if self.position.side == "LONG" else snapshot.ask

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

    def _maybe_lock_break_even(self, snapshot: ExhaustionSnapshot, mfe_bps: float) -> None:
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

    def _update_trailing_stop(self, snapshot: ExhaustionSnapshot, current_exit_price: float, mfe_bps: float) -> None:
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

    def _retest_extreme(self, snapshot: ExhaustionSnapshot) -> bool:
        if self.position is None:
            return False
        if self.position.side == "LONG":
            threshold = self.position.anchor_extreme_price * (1.0 + (self.retest_extreme_buffer_bps / 10_000.0))
            return snapshot.mid <= threshold
        threshold = self.position.anchor_extreme_price * (1.0 - (self.retest_extreme_buffer_bps / 10_000.0))
        return snapshot.mid >= threshold

    def _adverse_reacceleration(self, snapshot: ExhaustionSnapshot, current_realized_bps: float) -> bool:
        if self.position is None:
            return False
        if current_realized_bps >= max(2.0, self.position.stop_distance_bps * 0.30):
            return False
        if self.position.side == "LONG":
            return (
                snapshot.flow_imbalance <= -self.adverse_flow_exit_imbalance
                and snapshot.book_imbalance <= -self.adverse_book_exit_imbalance
            )
        return (
            snapshot.flow_imbalance >= self.adverse_flow_exit_imbalance
            and snapshot.book_imbalance >= self.adverse_book_exit_imbalance
        )

    def _close_position(
        self,
        snapshot: ExhaustionSnapshot,
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
        gross_pnl = (fill_price - position.entry_price) * position.size * direction
        exit_fee = position.notional * self.fee_pct
        total_fees = position.entry_fee_paid + exit_fee
        pnl = gross_pnl - total_fees
        hold_seconds = max(0.0, time.time() - position.opened_at)
        realized_bps = _bps_from_prices(fill_price, position.entry_price) * direction
        roi_pct_principal = (pnl / position.required_margin * 100.0) if position.required_margin > 0 else None
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
            snapshot=snapshot,
        )
        self._write_markout_row(
            position=position,
            snapshot=snapshot,
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
            take_profit_at_exit=position.take_profit_price,
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

    def _maybe_close_position(self, snapshot: ExhaustionSnapshot) -> None:
        assert self.position is not None
        current_exit_price = self._current_exit_price(snapshot)
        hold_seconds = max(0.0, time.time() - self.position.opened_at)
        mfe_bps, mae_bps = self._position_mfe_bps(current_exit_price)
        self.position.last_metrics = {"mfe_bps": mfe_bps, "mae_bps": mae_bps}
        current_realized_bps = self._current_realized_bps(current_exit_price)

        if hold_seconds >= self.position.failure_horizon_seconds and mfe_bps < self.position.failure_min_followthrough_bps:
            self._close_position(snapshot, "failed_reversal", current_exit_price, mfe_bps, mae_bps)
            return
        if self._adverse_reacceleration(snapshot, current_realized_bps):
            self._close_position(snapshot, "adverse_reacceleration", current_exit_price, mfe_bps, mae_bps)
            return
        if hold_seconds >= self.min_hold_seconds and self._retest_extreme(snapshot) and current_realized_bps <= 0:
            self._close_position(snapshot, "retest_extreme", current_exit_price, mfe_bps, mae_bps)
            return
        if hold_seconds >= self.position.max_hold_seconds:
            self._close_position(snapshot, "time_stop", current_exit_price, mfe_bps, mae_bps)
            return

        if self.position.side == "LONG":
            take_profit_hit = current_exit_price >= self.position.take_profit_price
        else:
            take_profit_hit = current_exit_price <= self.position.take_profit_price
        if take_profit_hit:
            self._close_position(snapshot, "take_profit_hit", current_exit_price, mfe_bps, mae_bps)
            return

        self._maybe_lock_break_even(snapshot, mfe_bps)
        self._update_trailing_stop(snapshot, current_exit_price, mfe_bps)

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
        self._close_position(snapshot, exit_reason, current_exit_price, mfe_bps, mae_bps)

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
            self._write_event("feed_reconnect_failed", quote_gap_ms=gap_ms, error=self.last_error)
            return None

    def _on_market_snapshot(self, market: MarketSnapshot) -> None:
        self._roll_day_if_needed()
        quote = market.quote
        now_ms = int(time.time() * 1000)
        quote_age_ms = int(_clamp_non_negative(now_ms - quote.exchange_time_ms))
        self.tick_count += 1
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

        snapshot = self._sample_signal_state(market)
        if snapshot is not None and self.position is None:
            self._maybe_enter(snapshot)
        if self.position is not None:
            active_snapshot = snapshot or self.last_snapshot
            if active_snapshot is not None:
                self._maybe_close_position(active_snapshot)

    def run(self) -> None:
        logging.info(
            "Starting pro exhaustion-reversal bot asset=%s sample_ms=%d fast_window_s=%.1f confirm_window_s=%.1f breakout_lookback_s=%.1f reversal_window_s=%.1f",
            self.asset,
            self.sample_ms,
            self.fast_impulse_window_seconds,
            self.confirm_impulse_window_seconds,
            self.breakout_lookback_seconds,
            self.reversal_window_seconds,
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
            min_fast_exhaustion_bps=self.min_fast_exhaustion_bps,
            min_confirm_exhaustion_bps=self.min_confirm_exhaustion_bps,
            fast_vol_multiplier=self.fast_vol_multiplier,
            confirm_vol_multiplier=self.confirm_vol_multiplier,
            exhaustion_flow_min=self.exhaustion_flow_min,
            exhaustion_book_min=self.exhaustion_book_min,
            reversal_flow_min=self.reversal_flow_min,
            reversal_book_min=self.reversal_book_min,
            rebound_confirm_bps=self.rebound_confirm_bps,
            reversal_window_seconds=self.reversal_window_seconds,
            min_trade_count=self.min_trade_count,
            depth_levels=self.depth_levels,
            max_spread_bps=self.max_spread_bps,
            max_quote_age_ms=self.max_quote_age_ms,
            quote_gap_warn_ms=self.quote_gap_warn_ms,
            reconnect_gap_ms=self.reconnect_gap_ms,
            setup_score_min=self.setup_score_min,
            initial_stop_min_bps=self.initial_stop_min_bps,
            initial_stop_vol_multiplier=self.initial_stop_vol_multiplier,
            stop_anchor_buffer_bps=self.stop_anchor_buffer_bps,
            take_profit_r=self.take_profit_r,
            min_take_profit_bps=self.min_take_profit_bps,
            trail_min_bps=self.trail_min_bps,
            trail_vol_multiplier=self.trail_vol_multiplier,
            trail_arm_r=self.trail_arm_r,
            break_even_lock_r=self.break_even_lock_r,
            failure_hold_seconds=self.failure_hold_seconds,
            failure_min_followthrough_bps=self.failure_min_followthrough_bps,
            retest_extreme_buffer_bps=self.retest_extreme_buffer_bps,
            adverse_flow_exit_imbalance=self.adverse_flow_exit_imbalance,
            adverse_book_exit_imbalance=self.adverse_book_exit_imbalance,
            max_hold_seconds=self.max_hold_seconds,
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
                item = self.feed.wait_for_next(last_seq=last_seq, timeout_seconds=self.quote_gap_warn_ms / 1000.0)
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
        avg_hold_seconds = sum(self.closed_trade_hold_seconds) / closed_trades if closed_trades else 0.0
        avg_capture = (
            sum(self.closed_trade_captures) / len(self.closed_trade_captures)
            if self.closed_trade_captures
            else 0.0
        )
        avg_realized_bps = sum(self.realized_bps_history) / len(self.realized_bps_history) if self.realized_bps_history else 0.0
        quote_age_p95 = self._pctl(self.quote_age_history_ms, 0.95)
        transport_p95 = self._pctl(self.transport_delay_history_ms, 0.95)
        spread_p95 = self._pctl(self.spread_history_bps, 0.95)

        report_name = (
            f"run_{datetime.fromtimestamp(self.started_at_ts, tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}_{self.asset.replace(':', '_')}.md"
        )
        report_path = self.report_dir / report_name
        lines = [
            "# Pro Exhaustion Reversal Run Report",
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
            f"- Reversal window seconds: `{self.reversal_window_seconds}`",
            f"- Rebound confirm bps: `{self.rebound_confirm_bps}`",
            f"- Setup score min: `{self.setup_score_min}`",
            f"- Initial stop min bps: `{self.initial_stop_min_bps}`",
            f"- Take profit R: `{self.take_profit_r}`",
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
            f"- Anchors observed: `{self.anchor_count}`",
            f"- Signal candidates: `{self.signal_candidate_count}`",
            f"- Signals confirmed: `{self.signal_confirmed_count}`",
            f"- Entries: `{self.entry_count}`",
            f"- Exits: `{self.exit_count}`",
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
            logging.exception("Failed writing exhaustion summary report")
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

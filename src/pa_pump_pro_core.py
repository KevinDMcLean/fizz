from __future__ import annotations

import csv
import json
import logging
import math
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
except ImportError:
    Info = None  # type: ignore[assignment]
    constants = None  # type: ignore[assignment]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso() -> str:
    return _utc_now().isoformat()


def _clamp_non_negative(value: float) -> float:
    return value if value >= 0 else 0.0


def _bps_from_prices(current: float, reference: float) -> float:
    if reference <= 0:
        return 0.0
    return ((current / reference) - 1.0) * 10_000.0


def _safe_float(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Quote:
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    mid: float
    microprice: float
    spread: float
    spread_bps: float
    exchange_time_ms: int
    received_time_ms: int
    transport_delay_ms: int


@dataclass
class TradePrint:
    side: str
    price: float
    size: float
    hash: str
    exchange_time_ms: int

    @property
    def notional(self) -> float:
        return self.price * self.size

    @property
    def aggressive_buy(self) -> bool:
        return self.side == "B"

    @property
    def aggressive_sell(self) -> bool:
        return self.side == "A"


@dataclass
class BookSnapshot:
    bids: List[Tuple[float, float]]
    asks: List[Tuple[float, float]]
    exchange_time_ms: int
    received_time_ms: int


@dataclass
class MarketSnapshot:
    quote: Quote
    book: Optional[BookSnapshot]
    trades: List[TradePrint]
    seq: int


@dataclass
class SignalSnapshot:
    sample_exchange_time_ms: int
    sample_received_time_ms: int
    quote_age_ms: int
    transport_delay_ms: int
    bid: float
    ask: float
    mid: float
    microprice: float
    spread_bps: float
    impulse_bps: float
    dynamic_threshold_bps: float
    recent_vol_bps: float
    book_imbalance: float
    flow_imbalance: float
    buy_volume: float
    sell_volume: float
    trade_count: int
    trade_rate_per_second: float
    breakout_high: float
    breakout_low: float
    breakout_distance_long_bps: float
    breakout_distance_short_bps: float
    long_breakout: bool
    short_breakout: bool
    long_ready: bool
    short_ready: bool
    regime_on: bool
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
class Position:
    trade_id: int
    side: str
    entry_price: float
    size: float
    notional: float
    required_margin: float
    entry_fee_paid: float
    initial_stop_price: float
    active_stop_price: float
    trail_distance_bps: float
    stop_distance_bps: float
    trail_arm_bps: float
    best_exit_price_seen: float
    worst_exit_price_seen: float
    stop_breach_count: int
    trailing_armed: bool
    locked_stop_price: Optional[float]
    opened_at: float
    opened_quote_exchange_time_ms: int
    opened_features: Dict[str, object]
    failure_horizon_seconds: float
    failure_min_followthrough_bps: float
    break_even_lock_r: float
    last_metrics: Dict[str, float] = field(default_factory=dict)


@dataclass
class CandidateState:
    side: str
    count: int
    first_seen_exchange_time_ms: int
    features: Dict[str, object]


def compute_book_imbalance(book: Optional[BookSnapshot], depth_levels: int) -> Tuple[float, float, float]:
    if book is None:
        return 0.0, 0.0, 0.0
    bid_depth = sum(size for _, size in book.bids[:depth_levels])
    ask_depth = sum(size for _, size in book.asks[:depth_levels])
    denom = bid_depth + ask_depth
    if denom <= 0:
        return 0.0, bid_depth, ask_depth
    return (bid_depth - ask_depth) / denom, bid_depth, ask_depth


def compute_trade_flow(
    trades: Sequence[TradePrint],
    since_ms: int,
) -> Dict[str, float]:
    buy_volume = 0.0
    sell_volume = 0.0
    buy_notional = 0.0
    sell_notional = 0.0
    count = 0
    newest_ms: Optional[int] = None
    oldest_ms: Optional[int] = None
    for trade in trades:
        if trade.exchange_time_ms < since_ms:
            continue
        if oldest_ms is None:
            oldest_ms = trade.exchange_time_ms
        newest_ms = trade.exchange_time_ms
        count += 1
        if trade.aggressive_buy:
            buy_volume += trade.size
            buy_notional += trade.notional
        else:
            sell_volume += trade.size
            sell_notional += trade.notional
    total_volume = buy_volume + sell_volume
    imbalance = ((buy_volume - sell_volume) / total_volume) if total_volume > 0 else 0.0
    elapsed_seconds = 0.0
    if oldest_ms is not None and newest_ms is not None:
        elapsed_seconds = max(0.001, (newest_ms - oldest_ms) / 1000.0)
    trade_rate = (count / elapsed_seconds) if elapsed_seconds > 0 else float(count)
    return {
        "buy_volume": buy_volume,
        "sell_volume": sell_volume,
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "count": float(count),
        "imbalance": imbalance,
        "trade_rate_per_second": trade_rate,
    }


def compute_realized_vol_bps(
    samples: Sequence[Tuple[int, float]],
    since_ms: int,
    impulse_window_seconds: float,
) -> float:
    filtered = [(ts, px) for ts, px in samples if ts >= since_ms and px > 0]
    if len(filtered) < 3:
        return 0.0
    returns: List[float] = []
    prev_ts, prev_px = filtered[0]
    for ts, px in filtered[1:]:
        if px <= 0 or prev_px <= 0 or ts <= prev_ts:
            prev_ts, prev_px = ts, px
            continue
        returns.append(math.log(px / prev_px))
        prev_ts, prev_px = ts, px
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    stdev = math.sqrt(max(0.0, var))
    total_seconds = max(0.001, (filtered[-1][0] - filtered[0][0]) / 1000.0)
    avg_dt_seconds = max(0.001, total_seconds / max(1, len(filtered) - 1))
    scaled = stdev * math.sqrt(max(1.0, impulse_window_seconds / avg_dt_seconds))
    return scaled * 10_000.0


def latest_reference_price(
    samples: Sequence[Tuple[int, float]],
    target_ts_ms: int,
) -> Optional[float]:
    if not samples:
        return None
    last_at_or_before: Optional[float] = None
    for ts_ms, price in samples:
        if ts_ms <= target_ts_ms:
            last_at_or_before = price
            continue
        if last_at_or_before is not None:
            return last_at_or_before
        return price
    return last_at_or_before if last_at_or_before is not None else samples[0][1]


class HyperliquidRealtimeMultiFeed:
    def __init__(self, asset: str, api_url: str, dex: str, trade_retention_ms: int) -> None:
        if Info is None or constants is None:
            raise RuntimeError(
                "hyperliquid-python SDK is not installed. Install it before running this bot."
            )
        self.asset = asset
        self.api_url = api_url
        self.dex = dex
        self.trade_retention_ms = trade_retention_ms
        perp_dexs = [self.dex] if self.dex else [""]
        self.info = Info(self.api_url, skip_ws=False, perp_dexs=perp_dexs)
        self._cv = threading.Condition()
        self._latest_quote: Optional[Quote] = None
        self._latest_book: Optional[BookSnapshot] = None
        self._recent_trades: Deque[TradePrint] = deque()
        self._seq = 0
        self.info.subscribe({"type": "bbo", "coin": self.asset}, self._on_bbo)
        self.info.subscribe({"type": "l2Book", "coin": self.asset}, self._on_l2book)
        self.info.subscribe({"type": "trades", "coin": self.asset}, self._on_trades)
        self._seed_from_rest_snapshot()

    def _next_seq(self) -> None:
        self._seq += 1
        self._cv.notify_all()

    def _trim_trades(self, now_ms: int) -> None:
        cutoff = now_ms - self.trade_retention_ms
        while self._recent_trades and self._recent_trades[0].exchange_time_ms < cutoff:
            self._recent_trades.popleft()

    def _seed_from_rest_snapshot(self) -> None:
        try:
            data = self.info.l2_snapshot(self.asset)
        except Exception:
            logging.exception("Failed seeding multi-feed from REST l2 snapshot")
            return
        if not isinstance(data, dict):
            return
        levels = data.get("levels")
        if not isinstance(levels, (list, tuple)) or len(levels) != 2:
            return
        bids_raw, asks_raw = levels
        if not isinstance(bids_raw, list) or not isinstance(asks_raw, list):
            return

        def convert(levels_raw: Iterable[object]) -> List[Tuple[float, float]]:
            rows: List[Tuple[float, float]] = []
            for level in levels_raw:
                if not isinstance(level, dict):
                    continue
                px = _safe_float(level.get("px"))
                sz = _safe_float(level.get("sz"))
                if px is None or sz is None or px <= 0 or sz < 0:
                    continue
                rows.append((px, sz))
            return rows

        bids = convert(bids_raw)
        asks = convert(asks_raw)
        if not bids or not asks:
            return
        exchange_time_ms = int(_safe_float(data.get("time")) or time.time() * 1000)
        received_time_ms = int(time.time() * 1000)
        top_bid_px, top_bid_sz = bids[0]
        top_ask_px, top_ask_sz = asks[0]
        if top_bid_px <= 0 or top_ask_px <= 0 or top_bid_px >= top_ask_px:
            return
        mid = (top_bid_px + top_ask_px) / 2.0
        denom = top_bid_sz + top_ask_sz
        microprice = ((top_ask_px * top_bid_sz) + (top_bid_px * top_ask_sz)) / denom if denom > 0 else mid
        spread = top_ask_px - top_bid_px
        spread_bps = (spread / mid) * 10_000.0 if mid > 0 else 0.0
        quote = Quote(
            bid=top_bid_px,
            ask=top_ask_px,
            bid_size=top_bid_sz,
            ask_size=top_ask_sz,
            mid=mid,
            microprice=microprice,
            spread=spread,
            spread_bps=spread_bps,
            exchange_time_ms=exchange_time_ms,
            received_time_ms=received_time_ms,
            transport_delay_ms=int(_clamp_non_negative(received_time_ms - exchange_time_ms)),
        )
        book = BookSnapshot(
            bids=bids,
            asks=asks,
            exchange_time_ms=exchange_time_ms,
            received_time_ms=received_time_ms,
        )
        with self._cv:
            self._latest_quote = quote
            self._latest_book = book
            self._next_seq()

    def _on_bbo(self, ws_msg: Dict[str, object]) -> None:
        data = ws_msg.get("data")
        if not isinstance(data, dict):
            return
        bbo = data.get("bbo")
        if not isinstance(bbo, (list, tuple)) or len(bbo) != 2:
            return
        bid_level, ask_level = bbo
        if not isinstance(bid_level, dict) or not isinstance(ask_level, dict):
            return
        try:
            bid = float(bid_level["px"])
            ask = float(ask_level["px"])
            bid_size = float(bid_level["sz"])
            ask_size = float(ask_level["sz"])
            exchange_time_ms = int(data["time"])
        except (KeyError, TypeError, ValueError):
            return
        if bid <= 0 or ask <= 0 or bid >= ask:
            return
        received_time_ms = int(time.time() * 1000)
        mid = (bid + ask) / 2.0
        denom = bid_size + ask_size
        if denom > 0:
            microprice = ((ask * bid_size) + (bid * ask_size)) / denom
        else:
            microprice = mid
        spread = ask - bid
        spread_bps = (spread / mid) * 10_000.0 if mid > 0 else 0.0
        transport_delay_ms = int(_clamp_non_negative(received_time_ms - exchange_time_ms))
        quote = Quote(
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            mid=mid,
            microprice=microprice,
            spread=spread,
            spread_bps=spread_bps,
            exchange_time_ms=exchange_time_ms,
            received_time_ms=received_time_ms,
            transport_delay_ms=transport_delay_ms,
        )
        with self._cv:
            self._latest_quote = quote
            self._trim_trades(exchange_time_ms)
            self._next_seq()

    def _on_l2book(self, ws_msg: Dict[str, object]) -> None:
        data = ws_msg.get("data")
        if not isinstance(data, dict):
            return
        levels = data.get("levels")
        if not isinstance(levels, (list, tuple)) or len(levels) != 2:
            return
        bids_raw, asks_raw = levels
        if not isinstance(bids_raw, list) or not isinstance(asks_raw, list):
            return

        def convert(levels_raw: Iterable[object]) -> List[Tuple[float, float]]:
            rows: List[Tuple[float, float]] = []
            for level in levels_raw:
                if not isinstance(level, dict):
                    continue
                px = _safe_float(level.get("px"))
                sz = _safe_float(level.get("sz"))
                if px is None or sz is None or px <= 0 or sz < 0:
                    continue
                rows.append((px, sz))
            return rows

        bids = convert(bids_raw)
        asks = convert(asks_raw)
        if not bids or not asks:
            return
        received_time_ms = int(time.time() * 1000)
        try:
            exchange_time_ms = int(data["time"])
        except (KeyError, TypeError, ValueError):
            exchange_time_ms = received_time_ms
        book = BookSnapshot(
            bids=bids,
            asks=asks,
            exchange_time_ms=exchange_time_ms,
            received_time_ms=received_time_ms,
        )
        with self._cv:
            self._latest_book = book
            self._next_seq()

    def _on_trades(self, ws_msg: Dict[str, object]) -> None:
        data = ws_msg.get("data")
        if not isinstance(data, list):
            return
        received_time_ms = int(time.time() * 1000)
        parsed: List[TradePrint] = []
        for trade in data:
            if not isinstance(trade, dict):
                continue
            try:
                side = str(trade["side"])
                price = float(trade["px"])
                size = float(trade["sz"])
                exchange_time_ms = int(trade["time"])
                trade_hash = str(trade["hash"])
            except (KeyError, TypeError, ValueError):
                continue
            if side not in {"A", "B"} or price <= 0 or size <= 0:
                continue
            parsed.append(
                TradePrint(
                    side=side,
                    price=price,
                    size=size,
                    hash=trade_hash,
                    exchange_time_ms=exchange_time_ms,
                )
            )
        if not parsed:
            return
        with self._cv:
            for trade in parsed:
                self._recent_trades.append(trade)
            self._trim_trades(parsed[-1].exchange_time_ms)
            if self._latest_quote is not None and self._latest_quote.received_time_ms <= received_time_ms:
                pass
            self._next_seq()

    def wait_for_next(
        self, last_seq: int, timeout_seconds: float
    ) -> Optional[Tuple[MarketSnapshot, int]]:
        deadline = time.monotonic() + timeout_seconds
        with self._cv:
            while self._seq <= last_seq:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(timeout=remaining)
            if self._latest_quote is None:
                return None
            snapshot = MarketSnapshot(
                quote=self._latest_quote,
                book=self._latest_book,
                trades=list(self._recent_trades),
                seq=self._seq,
            )
            return snapshot, self._seq

    def latest(self) -> Optional[Tuple[MarketSnapshot, int]]:
        with self._cv:
            if self._latest_quote is None:
                return None
            snapshot = MarketSnapshot(
                quote=self._latest_quote,
                book=self._latest_book,
                trades=list(self._recent_trades),
                seq=self._seq,
            )
            return snapshot, self._seq

    def get_recent_candle_closes(
        self, lookback_minutes: float, interval: str = "1m"
    ) -> List[float]:
        if lookback_minutes <= 0:
            return []
        end_dt = _utc_now()
        start_dt = end_dt - timedelta(minutes=lookback_minutes + 2.0)
        rows = self.info.candles_snapshot(
            self.asset,
            interval,
            int(start_dt.timestamp() * 1000),
            int(end_dt.timestamp() * 1000),
        )
        closes: List[float] = []
        for row in rows:
            try:
                closes.append(float(row["c"]))
            except Exception:
                continue
        return closes

    def close(self) -> None:
        try:
            self.info.disconnect_websocket()
        except Exception:
            logging.exception("Failed to disconnect websocket cleanly")


class ProMomentumBot:
    def __init__(
        self,
        asset: str,
        account_balance: float,
        api_url: str,
        dex: str,
        leverage: int,
        sample_ms: int,
        warm_start_candles: bool,
        startup_state_entry: bool,
        entry_confirmation_samples: int,
        impulse_window_seconds: float,
        breakout_lookback_seconds: float,
        volatility_lookback_seconds: float,
        flow_window_seconds: float,
        min_impulse_bps: float,
        volatility_multiplier: float,
        breakout_buffer_bps: float,
        flow_imbalance_min: float,
        book_imbalance_min: float,
        min_trade_count: int,
        depth_levels: int,
        max_spread_bps: float,
        max_quote_age_ms: int,
        quote_gap_warn_ms: int,
        reconnect_gap_ms: int,
        initial_stop_min_bps: float,
        initial_stop_vol_multiplier: float,
        trail_min_bps: float,
        trail_vol_multiplier: float,
        trail_arm_r: float,
        break_even_lock_r: float,
        failure_hold_seconds: float,
        failure_min_followthrough_bps: float,
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
        self.impulse_window_seconds = impulse_window_seconds
        self.breakout_lookback_seconds = breakout_lookback_seconds
        self.volatility_lookback_seconds = volatility_lookback_seconds
        self.flow_window_seconds = flow_window_seconds
        self.min_impulse_bps = min_impulse_bps
        self.volatility_multiplier = volatility_multiplier
        self.breakout_buffer_bps = breakout_buffer_bps
        self.flow_imbalance_min = flow_imbalance_min
        self.book_imbalance_min = book_imbalance_min
        self.min_trade_count = min_trade_count
        self.depth_levels = depth_levels
        self.max_spread_bps = max_spread_bps
        self.max_quote_age_ms = max_quote_age_ms
        self.quote_gap_warn_ms = quote_gap_warn_ms
        self.reconnect_gap_ms = reconnect_gap_ms
        self.initial_stop_min_bps = initial_stop_min_bps
        self.initial_stop_vol_multiplier = initial_stop_vol_multiplier
        self.trail_min_bps = trail_min_bps
        self.trail_vol_multiplier = trail_vol_multiplier
        self.trail_arm_r = trail_arm_r
        self.break_even_lock_r = break_even_lock_r
        self.failure_hold_seconds = failure_hold_seconds
        self.failure_min_followthrough_bps = failure_min_followthrough_bps
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
        self.report_dir = Path(report_dir)

        self.feed: Optional[HyperliquidRealtimeMultiFeed] = None
        self.position: Optional[Position] = None
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
        self.signal_count = 0
        self.entry_count = 0
        self.exit_count = 0
        self.poll_error_count = 0
        self.stale_quote_skips = 0
        self.last_error: Optional[str] = None
        self.last_quote_exchange_time_ms: Optional[int] = None
        self.last_quote_transport_delay_ms: Optional[int] = None
        self.last_bid: Optional[float] = None
        self.last_ask: Optional[float] = None
        self.last_mid: Optional[float] = None
        self.last_snapshot: Optional[SignalSnapshot] = None
        self.closed_trade_pnls: List[float] = []
        self.closed_trade_hold_seconds: List[float] = []
        self.closed_trade_captures: List[float] = []
        self.exit_reason_counts: Dict[str, int] = {}
        self.quote_age_history_ms: List[float] = []
        self.transport_delay_history_ms: List[float] = []
        self.spread_history_bps: List[float] = []
        self._finalized = False
        self._report_path: Optional[Path] = None
        self._quote_gap_active = False
        self._quote_gap_started_ms: Optional[int] = None
        self._last_reconnect_attempt_ms = 0
        self._last_stale_skip_event_ms = 0

        self.trade_retention_ms = int(
            max(
                impulse_window_seconds,
                breakout_lookback_seconds,
                volatility_lookback_seconds,
                flow_window_seconds,
            )
            * 1000.0
        ) + 180_000

        if self.leverage <= 0:
            raise ValueError("Leverage must be > 0.")
        if self.sample_ms < 50:
            raise ValueError("Sample ms must be >= 50.")
        if self.entry_confirmation_samples < 1:
            raise ValueError("Entry confirmation samples must be >= 1.")
        if self.impulse_window_seconds <= 0 or self.breakout_lookback_seconds <= 0:
            raise ValueError("Signal lookbacks must be > 0.")
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
        if self.stop_confirmation_ticks < 1:
            raise ValueError("Stop confirmation ticks must be >= 1.")
        if self.slippage_bps < 0 or self.fee_bps < 0:
            raise ValueError("Slippage and fee bps must be >= 0.")
        if self.cooldown_seconds < 0 or self.min_hold_seconds < 0:
            raise ValueError("Cooldown and min hold must be >= 0.")

        self.max_notional = self.account_balance * float(self.leverage)
        self.slippage_pct = self.slippage_bps / 10_000.0
        self.fee_pct = self.fee_bps / 10_000.0
        self._ensure_structured_log_files()

    def _utc_day(self) -> date:
        return _utc_now().date()

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
        oldest_keep_ms = ts_ms - max(
            int(self.breakout_lookback_seconds * 1000.0),
            int(self.volatility_lookback_seconds * 1000.0),
            int(self.impulse_window_seconds * 1000.0),
        ) - 180_000
        while len(self.price_samples) > 1 and self.price_samples[0][0] < oldest_keep_ms:
            self.price_samples.popleft()

    def _build_signal_snapshot(self, market: MarketSnapshot) -> Optional[SignalSnapshot]:
        quote = market.quote
        now_ms = int(time.time() * 1000)
        quote_age_ms = int(_clamp_non_negative(now_ms - quote.exchange_time_ms))
        if quote_age_ms > self.max_quote_age_ms:
            return None

        self._append_price_sample(quote.exchange_time_ms, quote.microprice)
        impulse_ref = latest_reference_price(
            list(self.price_samples),
            quote.exchange_time_ms - int(self.impulse_window_seconds * 1000.0),
        )
        if impulse_ref is None or impulse_ref <= 0:
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
            impulse_window_seconds=self.impulse_window_seconds,
        )
        dynamic_threshold_bps = max(self.min_impulse_bps, self.volatility_multiplier * recent_vol_bps)
        impulse_bps = _bps_from_prices(quote.microprice, impulse_ref)

        book_imbalance, bid_depth, ask_depth = compute_book_imbalance(
            market.book, self.depth_levels
        )
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
        long_breakout = quote.mid >= breakout_buffer_up
        short_breakout = quote.mid <= breakout_buffer_dn
        spread_ok = quote.spread_bps <= self.max_spread_bps
        long_ready = (
            spread_ok
            and impulse_bps >= dynamic_threshold_bps
            and long_breakout
            and flow["imbalance"] >= self.flow_imbalance_min
            and flow["count"] >= self.min_trade_count
            and book_imbalance >= self.book_imbalance_min
        )
        short_ready = (
            spread_ok
            and impulse_bps <= -dynamic_threshold_bps
            and short_breakout
            and flow["imbalance"] <= -self.flow_imbalance_min
            and flow["count"] >= self.min_trade_count
            and book_imbalance <= -self.book_imbalance_min
        )
        regime_on = (
            spread_ok
            and (abs(impulse_bps) >= dynamic_threshold_bps * 0.75 or abs(flow["imbalance"]) >= self.flow_imbalance_min)
        )

        return SignalSnapshot(
            sample_exchange_time_ms=quote.exchange_time_ms,
            sample_received_time_ms=quote.received_time_ms,
            quote_age_ms=quote_age_ms,
            transport_delay_ms=quote.transport_delay_ms,
            bid=quote.bid,
            ask=quote.ask,
            mid=quote.mid,
            microprice=quote.microprice,
            spread_bps=quote.spread_bps,
            impulse_bps=impulse_bps,
            dynamic_threshold_bps=dynamic_threshold_bps,
            recent_vol_bps=recent_vol_bps,
            book_imbalance=book_imbalance,
            flow_imbalance=flow["imbalance"],
            buy_volume=flow["buy_volume"],
            sell_volume=flow["sell_volume"],
            trade_count=int(flow["count"]),
            trade_rate_per_second=flow["trade_rate_per_second"],
            breakout_high=breakout_high,
            breakout_low=breakout_low,
            breakout_distance_long_bps=breakout_distance_long_bps,
            breakout_distance_short_bps=breakout_distance_short_bps,
            long_breakout=long_breakout,
            short_breakout=short_breakout,
            long_ready=long_ready,
            short_ready=short_ready,
            regime_on=regime_on,
            sample_count=len(self.price_samples),
        )

    def _sample_signal_state(self, market: MarketSnapshot) -> Optional[SignalSnapshot]:
        quote = market.quote
        if (
            self.last_sample_exchange_time_ms is not None
            and (quote.exchange_time_ms - self.last_sample_exchange_time_ms) < self.sample_ms
        ):
            return None
        self.last_sample_exchange_time_ms = quote.exchange_time_ms
        snapshot = self._build_signal_snapshot(market)
        if snapshot is None:
            return None
        self.last_snapshot = snapshot
        self._write_sample(snapshot)
        return snapshot

    def _write_event(self, event: str, **fields: object) -> None:
        payload = {"timestamp_utc": _utc_iso(), "asset": self.asset, "event": event, **fields}
        try:
            with self.events_jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except Exception:
            logging.exception("Failed writing event JSONL")

    def _write_sample(self, snapshot: SignalSnapshot) -> None:
        payload = {"timestamp_utc": _utc_iso(), "asset": self.asset, **snapshot.to_dict()}
        try:
            with self.samples_jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except Exception:
            logging.exception("Failed writing sample JSONL")

    def _write_trade_row(
        self,
        *,
        trade_id: int,
        action: str,
        side: str,
        price: float,
        size: float,
        notional: float,
        snapshot: SignalSnapshot,
        reason: str,
        pnl: Optional[float],
        pnl_pct_equity: Optional[float],
        gross_pnl: Optional[float],
        fees_paid: Optional[float],
        roi_pct_principal: Optional[float],
        stop_distance_bps: Optional[float],
        trail_distance_bps: Optional[float],
        mfe_bps: Optional[float],
        mae_bps: Optional[float],
        capture_ratio: Optional[float],
    ) -> None:
        row = [
            _utc_iso(),
            self.asset,
            trade_id,
            action,
            side,
            f"{price:.8f}",
            f"{size:.8f}",
            f"{notional:.8f}",
            f"{snapshot.bid:.8f}",
            f"{snapshot.ask:.8f}",
            f"{snapshot.mid:.8f}",
            f"{snapshot.spread_bps:.4f}",
            str(snapshot.quote_age_ms),
            str(snapshot.transport_delay_ms),
            "" if pnl is None else f"{pnl:.8f}",
            "" if pnl_pct_equity is None else f"{pnl_pct_equity:.8f}",
            "" if gross_pnl is None else f"{gross_pnl:.8f}",
            "" if fees_paid is None else f"{fees_paid:.8f}",
            "" if roi_pct_principal is None else f"{roi_pct_principal:.8f}",
            reason,
            f"{snapshot.impulse_bps:.4f}",
            f"{snapshot.dynamic_threshold_bps:.4f}",
            f"{snapshot.breakout_distance_long_bps:.4f}" if side == "LONG" else f"{snapshot.breakout_distance_short_bps:.4f}",
            f"{snapshot.flow_imbalance:.6f}",
            f"{snapshot.book_imbalance:.6f}",
            f"{snapshot.recent_vol_bps:.4f}",
            str(snapshot.trade_count),
            "" if stop_distance_bps is None else f"{stop_distance_bps:.4f}",
            "" if trail_distance_bps is None else f"{trail_distance_bps:.4f}",
            "" if mfe_bps is None else f"{mfe_bps:.4f}",
            "" if mae_bps is None else f"{mae_bps:.4f}",
            "" if capture_ratio is None else f"{capture_ratio:.6f}",
        ]
        try:
            with self.trades_csv_path.open("a", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(row)
        except Exception:
            logging.exception("Failed writing trade CSV row")

    def _ensure_structured_log_files(self) -> None:
        self.events_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self.samples_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self.trades_csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        if not self.trades_csv_path.exists():
            with self.trades_csv_path.open("a", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(
                    [
                        "timestamp_utc",
                        "asset",
                        "trade_id",
                        "action",
                        "side",
                        "price",
                        "size",
                        "notional",
                        "bid",
                        "ask",
                        "mid",
                        "spread_bps",
                        "quote_age_ms",
                        "transport_delay_ms",
                        "pnl",
                        "pnl_pct_equity",
                        "gross_pnl",
                        "fees_paid",
                        "roi_pct_principal",
                        "reason",
                        "impulse_bps",
                        "dynamic_threshold_bps",
                        "breakout_distance_bps",
                        "flow_imbalance",
                        "book_imbalance",
                        "recent_vol_bps",
                        "trade_count",
                        "stop_distance_bps",
                        "trail_distance_bps",
                        "mfe_bps",
                        "mae_bps",
                        "capture_ratio",
                    ]
                )

    def _warm_start_prices(self, startup_microprice: float) -> int:
        if not self.warm_start_candles or self.feed is None:
            return 0
        lookback_minutes = max(
            self.breakout_lookback_seconds,
            self.volatility_lookback_seconds,
            self.impulse_window_seconds,
            self.flow_window_seconds,
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
        self.last_sample_exchange_time_ms = now_ms
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

    def _update_candidate(self, signal: SignalSnapshot) -> Optional[CandidateState]:
        if signal.long_ready and not signal.short_ready:
            side = "LONG"
        elif signal.short_ready and not signal.long_ready:
            side = "SHORT"
        else:
            self.pending_candidate = None
            return None
        if self.pending_candidate is None or self.pending_candidate.side != side:
            self.pending_candidate = CandidateState(
                side=side,
                count=1,
                first_seen_exchange_time_ms=signal.sample_exchange_time_ms,
                features=signal.to_dict(),
            )
            self._write_event(
                "signal_candidate",
                side=side,
                candidate_count=1,
                **signal.to_dict(),
            )
            return self.pending_candidate
        self.pending_candidate.count += 1
        self.pending_candidate.features = signal.to_dict()
        return self.pending_candidate

    def _compute_stop_distance_bps(self, signal: SignalSnapshot) -> float:
        return max(
            self.initial_stop_min_bps,
            self.initial_stop_vol_multiplier * max(signal.recent_vol_bps, signal.spread_bps),
            signal.spread_bps * 2.0,
        )

    def _compute_trail_distance_bps(self, signal: SignalSnapshot) -> float:
        return max(
            self.trail_min_bps,
            self.trail_vol_multiplier * max(signal.recent_vol_bps, signal.spread_bps),
        )

    def _open_position(self, signal: SignalSnapshot, side: str, reason: str) -> None:
        now_ts = time.time()
        if not self._entry_allowed(now_ts):
            return
        stop_distance_bps = self._compute_stop_distance_bps(signal)
        trail_distance_bps = self._compute_trail_distance_bps(signal)
        price_stop_fraction = stop_distance_bps / 10_000.0
        risk_amount = self.account_balance * self.equity_risk_pct
        notional_target = risk_amount / max(price_stop_fraction, 1e-9)
        notional = min(notional_target, self.max_notional)
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
        worst_exit_price_seen = best_exit_price_seen
        trail_arm_bps = max(self.failure_min_followthrough_bps, stop_distance_bps * self.trail_arm_r)

        self.trade_seq += 1
        trade_id = self.trade_seq
        self.position = Position(
            trade_id=trade_id,
            side=side,
            entry_price=entry_price,
            size=size,
            notional=notional,
            required_margin=required_margin,
            entry_fee_paid=entry_fee,
            initial_stop_price=stop_price,
            active_stop_price=stop_price,
            trail_distance_bps=trail_distance_bps,
            stop_distance_bps=stop_distance_bps,
            trail_arm_bps=trail_arm_bps,
            best_exit_price_seen=best_exit_price_seen,
            worst_exit_price_seen=worst_exit_price_seen,
            stop_breach_count=0,
            trailing_armed=False,
            locked_stop_price=None,
            opened_at=now_ts,
            opened_quote_exchange_time_ms=signal.sample_exchange_time_ms,
            opened_features=signal.to_dict(),
            failure_horizon_seconds=self.failure_hold_seconds,
            failure_min_followthrough_bps=self.failure_min_followthrough_bps,
            break_even_lock_r=self.break_even_lock_r,
        )
        self.hourly_entry_timestamps.append(now_ts)
        self.daily_trade_count += 1
        self.entry_count += 1
        self.signal_count += 1
        self.pending_candidate = None

        self._write_trade_row(
            trade_id=trade_id,
            action="ENTRY",
            side=side,
            price=entry_price,
            size=size,
            notional=notional,
            snapshot=signal,
            reason=reason,
            pnl=None,
            pnl_pct_equity=None,
            gross_pnl=None,
            fees_paid=entry_fee,
            roi_pct_principal=None,
            stop_distance_bps=stop_distance_bps,
            trail_distance_bps=trail_distance_bps,
            mfe_bps=0.0,
            mae_bps=0.0,
            capture_ratio=None,
        )
        self._write_event(
            "position_opened",
            trade_id=trade_id,
            side=side,
            entry_price=entry_price,
            size=size,
            notional=notional,
            required_margin=required_margin,
            entry_fee=entry_fee,
            stop_distance_bps=stop_distance_bps,
            trail_distance_bps=trail_distance_bps,
            initial_stop=stop_price,
            trail_arm_bps=trail_arm_bps,
            signal_reason=reason,
            signal=self.position.opened_features,
        )

    def _maybe_enter(self, signal: SignalSnapshot) -> None:
        candidate = self._update_candidate(signal)
        if candidate is None:
            return
        if candidate.first_seen_exchange_time_ms == signal.sample_exchange_time_ms and not self.startup_state_entry:
            return
        if candidate.count < self.entry_confirmation_samples:
            return
        reason = (
            f"{candidate.side} impulse_breakout "
            f"impulse_bps={signal.impulse_bps:.2f} "
            f"threshold_bps={signal.dynamic_threshold_bps:.2f} "
            f"flow_imbalance={signal.flow_imbalance:.3f} "
            f"book_imbalance={signal.book_imbalance:.3f}"
        )
        self._write_event(
            "signal_confirmed",
            side=candidate.side,
            candidate_count=candidate.count,
            **signal.to_dict(),
        )
        self._open_position(signal, candidate.side, reason)

    def _current_exit_price(self, signal: SignalSnapshot) -> float:
        assert self.position is not None
        return signal.bid if self.position.side == "LONG" else signal.ask

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

    def _maybe_lock_break_even(self, signal: SignalSnapshot, mfe_bps: float) -> None:
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

    def _update_trailing_stop(self, signal: SignalSnapshot, current_exit_price: float, mfe_bps: float) -> None:
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

    def _maybe_close_position(self, signal: SignalSnapshot) -> None:
        assert self.position is not None
        current_exit_price = self._current_exit_price(signal)
        hold_seconds = max(0.0, time.time() - self.position.opened_at)
        mfe_bps, mae_bps = self._position_mfe_bps(current_exit_price)
        self.position.last_metrics = {"mfe_bps": mfe_bps, "mae_bps": mae_bps}

        if hold_seconds >= self.position.failure_horizon_seconds and mfe_bps < self.position.failure_min_followthrough_bps:
            self._close_position(signal, "failed_breakout", current_exit_price, mfe_bps, mae_bps)
            return

        self._maybe_lock_break_even(signal, mfe_bps)
        self._update_trailing_stop(signal, current_exit_price, mfe_bps)

        stop_hit = False
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

        if hold_seconds < self.min_hold_seconds:
            return

        exit_reason = "trailing_stop_hit" if self.position.trailing_armed else "initial_stop_hit"
        self._close_position(signal, exit_reason, current_exit_price, mfe_bps, mae_bps)

    def _close_position(
        self,
        signal: SignalSnapshot,
        exit_reason: str,
        trigger_price: float,
        mfe_bps: float,
        mae_bps: float,
    ) -> None:
        assert self.position is not None
        direction = 1.0 if self.position.side == "LONG" else -1.0
        fill_price = trigger_price
        if self.slippage_bps > 0:
            slippage_fraction = self.slippage_bps / 10_000.0
            if self.position.side == "LONG":
                fill_price *= (1.0 - slippage_fraction)
            else:
                fill_price *= (1.0 + slippage_fraction)
        gross_pnl = (fill_price - self.position.entry_price) * self.position.size * direction
        exit_fee = self.position.notional * self.fee_pct
        total_fees = self.position.entry_fee_paid + exit_fee
        pnl = gross_pnl - total_fees
        pnl_pct_equity = (pnl / self.account_balance * 100.0) if self.account_balance > 0 else None
        roi_pct_principal = (pnl / self.position.required_margin * 100.0) if self.position.required_margin > 0 else None
        hold_seconds = max(0.0, time.time() - self.position.opened_at)
        realized_bps = _bps_from_prices(fill_price, self.position.entry_price) * direction
        capture_ratio = (
            realized_bps / mfe_bps if mfe_bps > 0 else None
        )

        self.daily_realized_pnl += pnl
        self.last_exit_ts = time.time()
        self.exit_count += 1
        self.closed_trade_pnls.append(pnl)
        self.closed_trade_hold_seconds.append(hold_seconds)
        if capture_ratio is not None:
            self.closed_trade_captures.append(capture_ratio)
        self.exit_reason_counts[exit_reason] = self.exit_reason_counts.get(exit_reason, 0) + 1

        self._write_trade_row(
            trade_id=self.position.trade_id,
            action="EXIT",
            side=self.position.side,
            price=fill_price,
            size=self.position.size,
            notional=self.position.notional,
            snapshot=signal,
            reason=(
                f"{exit_reason} trigger={trigger_price:.6f} exec_px={fill_price:.6f} "
                f"stop={self.position.active_stop_price:.6f}"
            ),
            pnl=pnl,
            pnl_pct_equity=pnl_pct_equity,
            gross_pnl=gross_pnl,
            fees_paid=total_fees,
            roi_pct_principal=roi_pct_principal,
            stop_distance_bps=self.position.stop_distance_bps,
            trail_distance_bps=self.position.trail_distance_bps,
            mfe_bps=mfe_bps,
            mae_bps=mae_bps,
            capture_ratio=capture_ratio,
        )
        self._write_event(
            "position_closed",
            trade_id=self.position.trade_id,
            side=self.position.side,
            entry_price=self.position.entry_price,
            exit_price=fill_price,
            pnl=pnl,
            gross_pnl=gross_pnl,
            total_fees=total_fees,
            pnl_pct_equity=pnl_pct_equity,
            roi_pct_principal=roi_pct_principal,
            hold_seconds=hold_seconds,
            exit_reason=exit_reason,
            active_stop_at_exit=self.position.active_stop_price,
            initial_stop=self.position.initial_stop_price,
            locked_stop_at_exit=self.position.locked_stop_price,
            trailing_armed_at_exit=self.position.trailing_armed,
            stop_distance_bps=self.position.stop_distance_bps,
            trail_distance_bps=self.position.trail_distance_bps,
            mfe_bps=mfe_bps,
            mae_bps=mae_bps,
            capture_ratio=capture_ratio,
            signal=self.position.opened_features,
        )
        self.position = None
        self.pending_candidate = None

    def _on_market_snapshot(self, market: MarketSnapshot) -> None:
        self._roll_day_if_needed()
        quote = market.quote
        now_ms = int(time.time() * 1000)
        quote_age_ms = int(_clamp_non_negative(now_ms - quote.exchange_time_ms))
        self.tick_count += 1
        self.last_bid = quote.bid
        self.last_ask = quote.ask
        self.last_mid = quote.mid
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
            self._write_event(
                "state_snapshot",
                **signal.to_dict(),
                position_side=self.position.side if self.position is not None else None,
                open_trade_id=self.position.trade_id if self.position is not None else None,
            )
            if self.position is None:
                self._maybe_enter(signal)
        if self.position is not None:
            active_signal = signal or self.last_snapshot
            if active_signal is not None:
                self._maybe_close_position(active_signal)

    def run(self) -> None:
        logging.info(
            "Starting pro oil momentum bot asset=%s sample_ms=%d impulse_window_s=%.1f breakout_lookback_s=%.1f flow_window_s=%.1f min_impulse_bps=%.2f vol_multiplier=%.2f max_spread_bps=%.2f",
            self.asset,
            self.sample_ms,
            self.impulse_window_seconds,
            self.breakout_lookback_seconds,
            self.flow_window_seconds,
            self.min_impulse_bps,
            self.volatility_multiplier,
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
            impulse_window_seconds=self.impulse_window_seconds,
            breakout_lookback_seconds=self.breakout_lookback_seconds,
            volatility_lookback_seconds=self.volatility_lookback_seconds,
            flow_window_seconds=self.flow_window_seconds,
            min_impulse_bps=self.min_impulse_bps,
            volatility_multiplier=self.volatility_multiplier,
            breakout_buffer_bps=self.breakout_buffer_bps,
            flow_imbalance_min=self.flow_imbalance_min,
            book_imbalance_min=self.book_imbalance_min,
            min_trade_count=self.min_trade_count,
            depth_levels=self.depth_levels,
            max_spread_bps=self.max_spread_bps,
            max_quote_age_ms=self.max_quote_age_ms,
            quote_gap_warn_ms=self.quote_gap_warn_ms,
            reconnect_gap_ms=self.reconnect_gap_ms,
            initial_stop_min_bps=self.initial_stop_min_bps,
            initial_stop_vol_multiplier=self.initial_stop_vol_multiplier,
            trail_min_bps=self.trail_min_bps,
            trail_vol_multiplier=self.trail_vol_multiplier,
            trail_arm_r=self.trail_arm_r,
            break_even_lock_r=self.break_even_lock_r,
            failure_hold_seconds=self.failure_hold_seconds,
            failure_min_followthrough_bps=self.failure_min_followthrough_bps,
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

        report_name = (
            f"run_{datetime.fromtimestamp(self.started_at_ts, tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}_{self.asset.replace(':', '_')}.md"
        )
        report_path = self.report_dir / report_name
        lines = [
            "# Pro Oil Momentum Bot Run Report",
            "",
            f"- Asset: `{self.asset}`",
            f"- Stop reason: `{reason}`",
            f"- Start (UTC): `{datetime.fromtimestamp(self.started_at_ts, tz=timezone.utc).isoformat()}`",
            f"- End (UTC): `{datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat()}`",
            f"- Duration (s): `{duration_seconds:.1f}`",
            "",
            "## Configuration",
            f"- Sample ms: `{self.sample_ms}`",
            f"- Impulse window seconds: `{self.impulse_window_seconds}`",
            f"- Breakout lookback seconds: `{self.breakout_lookback_seconds}`",
            f"- Flow window seconds: `{self.flow_window_seconds}`",
            f"- Min impulse bps: `{self.min_impulse_bps}`",
            f"- Volatility multiplier: `{self.volatility_multiplier}`",
            f"- Max spread bps: `{self.max_spread_bps}`",
            f"- Initial stop min bps: `{self.initial_stop_min_bps}`",
            f"- Trail min bps: `{self.trail_min_bps}`",
            f"- Failure hold seconds: `{self.failure_hold_seconds}`",
            f"- Failure min follow-through bps: `{self.failure_min_followthrough_bps}`",
            "",
            "## Feed Health",
            f"- Startup preflight: `{'ok' if self.preflight_ok else 'failed'}`",
            f"- Ticks captured: `{self.tick_count}`",
            f"- Loop errors: `{self.poll_error_count}`",
            f"- Stale quote skips: `{self.stale_quote_skips}`",
            f"- Quote age p95 ms: `{quote_age_p95 if quote_age_p95 is not None else 'n/a'}`",
            f"- Transport delay p95 ms: `{transport_p95 if transport_p95 is not None else 'n/a'}`",
            f"- Spread p95 bps: `{spread_p95 if spread_p95 is not None else 'n/a'}`",
            "",
            "## Trading Activity",
            f"- Signals confirmed: `{self.signal_count}`",
            f"- Entries: `{self.entry_count}`",
            f"- Exits: `{self.exit_count}`",
            f"- Open position at stop: `{'yes' if self.position is not None else 'no'}`",
            "",
            "## Performance",
            f"- Closed trades: `{closed_trades}`",
            f"- Total realized PnL: `{total_realized_pnl:.4f}`",
            f"- Win rate: `{win_rate_pct:.2f}%`",
            f"- Avg hold seconds: `{avg_hold_seconds:.2f}`",
            f"- Avg capture ratio: `{avg_capture:.4f}`",
            "",
            "## Exit Reasons",
        ]
        for key in sorted(self.exit_reason_counts):
            lines.append(f"- {key}: `{self.exit_reason_counts[key]}`")
        lines.extend(
            [
                "",
                "## Data Files",
                f"- Events JSONL: `{self.events_jsonl_path}`",
                f"- Samples JSONL: `{self.samples_jsonl_path}`",
                f"- Trades CSV: `{self.trades_csv_path}`",
            ]
        )
        try:
            report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._report_path = report_path
            self._write_event(
                "bot_stopped",
                stop_reason=reason,
                duration_seconds=duration_seconds,
                ticks=self.tick_count,
                signals=self.signal_count,
                entries=self.entry_count,
                exits=self.exit_count,
                closed_trades=closed_trades,
                realized_pnl=total_realized_pnl,
                report_path=str(report_path),
            )
            return report_path
        except Exception:
            logging.exception("Failed writing report")
            return None

    @staticmethod
    def _pctl(values: Sequence[float], percentile: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        idx = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
        return round(ordered[idx], 3)

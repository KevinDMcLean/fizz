from __future__ import annotations

import csv
import json
import logging
import math
import time
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib import request

from hyperliquid_fee_model import (
    FeeState,
    HyperliquidFeeConfig,
    dynamic_kill_floor_bps,
    estimate_exchange_fee,
    estimate_fee_state,
    required_edge_bps,
)
from pa_pump_pro_core import (
    BookSnapshot,
    HyperliquidRealtimeMultiFeed,
    MarketSnapshot,
    TradePrint,
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


def _round_down(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    return math.floor(price / tick_size) * tick_size


def _round_up(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    return math.ceil(price / tick_size) * tick_size


def infer_tick_size(book: Optional[BookSnapshot], bid: float, ask: float) -> float:
    candidates: List[float] = []
    if book is not None:
        for levels in (book.bids, book.asks):
            for (px_a, _), (px_b, _) in zip(levels, levels[1:]):
                diff = abs(px_a - px_b)
                if diff > 0:
                    candidates.append(diff)
    spread = ask - bid
    if spread > 0:
        candidates.append(spread)
    if candidates:
        tick = min(candidates)
        return max(round(tick, 8), 1e-6)
    decimals = max(len(f"{bid:.8f}".rstrip("0").split(".")[-1]), len(f"{ask:.8f}".rstrip("0").split(".")[-1]))
    return max(10 ** (-decimals), 1e-6)


def visible_size_at_price(
    levels: Sequence[Tuple[float, float]],
    price: float,
    tick_size: float,
) -> float:
    tolerance = max(tick_size / 2.0, 1e-9)
    for level_price, level_size in levels:
        if abs(level_price - price) <= tolerance:
            return level_size
    return 0.0


def top_level_size(levels: Sequence[Tuple[float, float]]) -> float:
    if not levels:
        return 0.0
    return max(0.0, levels[0][1])


@dataclass
class MMStrategyConfig:
    account_balance: float
    leverage: int
    sample_ms: int
    warm_start_candles: bool
    startup_quote_immediately: bool
    volatility_lookback_seconds: float
    impulse_window_seconds: float
    flow_window_seconds: float
    depth_levels: int
    min_trade_count: int
    max_spread_bps: float
    max_quote_age_ms: int
    quote_gap_warn_ms: int
    reconnect_gap_ms: int
    min_half_spread_bps: float
    target_edge_bps: float
    vol_spread_multiplier: float
    toxicity_spread_multiplier: float
    latency_spread_multiplier: float
    inventory_skew_bps: float
    base_order_notional: float
    max_quote_notional: float
    max_inventory_notional: float
    requote_price_bps: float
    requote_size_pct: float
    min_requote_interval_ms: int
    min_quote_size_multiplier: float
    max_quote_size_multiplier: float
    max_top_level_share: float
    max_depth_share: float
    queue_ahead_penalty: float
    quote_guard_flow_imbalance: float
    quote_guard_book_imbalance: float
    quote_guard_toxicity: float
    one_way_flow_imbalance: float
    one_way_book_imbalance: float
    one_way_alpha_bps: float
    protection_markout_bps: float
    protection_flow_imbalance: float
    protection_book_imbalance: float
    event_impulse_bps: float
    event_vol_bps: float
    toxic_flow_imbalance: float
    toxic_book_imbalance: float
    adverse_exit_bps: float
    adverse_flow_exit_imbalance: float
    adverse_book_exit_imbalance: float
    max_inventory_hold_seconds: float
    kill_hold_seconds: float
    kill_on_event_loss_bps: float
    cooldown_seconds: float
    max_daily_loss: float
    max_episodes_per_day: int
    max_episodes_per_hour: int
    stop_quoting_on_event: bool
    fee_product: str
    fee_market_type: str
    fee_staking_tier: str
    fee_tier_basis: str
    fee_target_tier: int
    fee_initial_14d_perps_volume: float
    fee_initial_14d_spot_volume: float
    fee_taker_referral_discount_pct: float
    fee_maker_rebate_bps_override: float
    fee_deployer_fee_scale: float
    fee_growth_mode: bool
    fee_aligned_quote_token: bool
    fee_user_address: str
    fee_user_maker_rate_pct_override: float | None
    fee_user_taker_rate_pct_override: float | None
    fee_buffer_bps: float
    fee_kill_buffer_bps: float
    expected_taker_share_floor: float
    tier_volume_boost_multiplier: float
    tier_volume_relaxation_multiplier: float
    size_toxicity_penalty: float
    size_vol_penalty: float
    size_spread_penalty: float
    size_inventory_penalty: float
    large_inventory_protection_ratio: float
    fee_bps: float
    slippage_bps: float


@dataclass
class MarketFeatures:
    sample_exchange_time_ms: int
    sample_received_time_ms: int
    quote_age_ms: int
    transport_delay_ms: int
    bid: float
    ask: float
    mid: float
    microprice: float
    spread_bps: float
    tick_size: float
    tick_bps: float
    recent_vol_bps: float
    impulse_bps: float
    flow_imbalance: float
    buy_volume: float
    sell_volume: float
    trade_count: int
    trade_rate_per_second: float
    book_imbalance: float
    bid_depth: float
    ask_depth: float
    toxicity_score: float
    event_regime: bool
    quoting_health_ok: bool
    quoting_health_reason: str


@dataclass
class QuotePlan:
    quoting_enabled: bool
    quoting_reason: str
    fair_value: float
    reservation_price: float
    alpha_bps: float
    inventory_skew_bps: float
    target_half_spread_bps: float
    bid_price: Optional[float]
    ask_price: Optional[float]
    bid_size: float
    ask_size: float
    bid_queue_ahead_size: float
    ask_queue_ahead_size: float
    bid_enabled: bool
    ask_enabled: bool
    bid_reason: str
    ask_reason: str
    quote_mode: str
    decision_note: str
    event_regime: bool
    toxicity_score: float
    size_risk_multiplier: float = 1.0


@dataclass
class MMSnapshot:
    sample_exchange_time_ms: int
    sample_received_time_ms: int
    quote_age_ms: int
    transport_delay_ms: int
    bid: float
    ask: float
    mid: float
    microprice: float
    spread_bps: float
    tick_size: float
    recent_vol_bps: float
    impulse_bps: float
    flow_imbalance: float
    buy_volume: float
    sell_volume: float
    trade_count: int
    trade_rate_per_second: float
    book_imbalance: float
    bid_depth: float
    ask_depth: float
    toxicity_score: float
    event_regime: bool
    quoting_health_ok: bool
    quoting_health_reason: str
    quoting_enabled: bool
    quoting_reason: str
    fair_value: float
    reservation_price: float
    alpha_bps: float
    inventory_skew_bps: float
    target_half_spread_bps: float
    bid_quote_price: Optional[float]
    ask_quote_price: Optional[float]
    bid_quote_size: float
    ask_quote_size: float
    bid_queue_ahead_size: float
    ask_queue_ahead_size: float
    bid_enabled: bool
    ask_enabled: bool
    bid_reason: str
    ask_reason: str
    quote_mode: str
    decision_note: str
    live_bid_order_price: Optional[float]
    live_ask_order_price: Optional[float]
    live_bid_order_size: float
    live_ask_order_size: float
    inventory_qty: float
    inventory_side: str
    inventory_avg_price: Optional[float]
    inventory_unrealized_pnl: float
    inventory_unrealized_bps: float
    inventory_hold_seconds: float
    episode_id: Optional[int]
    sample_count: int
    fee_tier_label: str
    fee_market_type: str
    fee_staking_tier: str
    fee_basis: str
    fee_rate_source: str
    fee_deployer_fee_scale: float
    fee_growth_mode: bool
    fee_aligned_quote_token: bool
    fee_actual_tier_label: str
    fee_projected_tier_label: str
    fee_maker_rate_bps: float
    fee_taker_rate_bps: float
    fee_maker_rebate_bps: float
    fee_net_maker_rate_bps: float
    fee_required_edge_bps: float
    fee_dynamic_kill_floor_bps: float
    fee_expected_taker_share_pct: float
    fee_current_weighted_14d_volume: float
    fee_projected_weighted_14d_volume: float
    fee_daily_volume_run_rate: float
    fee_target_tier_label: str
    fee_target_tier_progress_pct: float
    fee_days_to_next_tier: Optional[float]
    volume_boost_multiplier: float
    size_risk_multiplier: float

    def to_dict(self) -> Dict[str, object]:
        payload = asdict(self)
        payload["sample_exchange_time_utc"] = datetime.fromtimestamp(
            self.sample_exchange_time_ms / 1000.0,
            tz=timezone.utc,
        ).isoformat()
        payload["sample_received_time_utc"] = datetime.fromtimestamp(
            self.sample_received_time_ms / 1000.0,
            tz=timezone.utc,
        ).isoformat()
        return payload


@dataclass
class PassiveOrder:
    order_id: int
    side: str
    price: float
    size: float
    remaining_size: float
    queue_ahead_size: float
    placed_exchange_time_ms: int
    placed_at_ts: float
    reason: str


@dataclass
class InventoryEpisode:
    episode_id: int
    direction: str
    qty_signed: float
    avg_price: float
    opened_at_ts: float
    opened_exchange_time_ms: int
    max_abs_qty: float
    passive_entry_fills: int = 0
    passive_exit_fills: int = 0
    aggressive_exit_fills: int = 0
    fill_count: int = 0
    realized_pnl: float = 0.0
    gross_pnl: float = 0.0
    fees_paid: float = 0.0
    maker_fee_cost: float = 0.0
    taker_fee_cost: float = 0.0
    maker_rebates: float = 0.0
    maker_notional: float = 0.0
    taker_notional: float = 0.0
    best_markout_bps: float = 0.0
    worst_markout_bps: float = 0.0
    close_reason: Optional[str] = None
    closed_at_ts: Optional[float] = None
    closed_exchange_time_ms: Optional[int] = None

    @property
    def inventory_side(self) -> str:
        return "LONG" if self.qty_signed > 0 else "SHORT"


def build_quote_plan(
    *,
    features: MarketFeatures,
    inventory_qty: float,
    inventory_avg_price: Optional[float],
    book: Optional[BookSnapshot],
    config: MMStrategyConfig,
    fee_state: FeeState,
    expected_taker_share: float,
    protection_reason: Optional[str] = None,
) -> QuotePlan:
    inv_notional = inventory_qty * features.mid
    inv_ratio = 0.0
    if config.max_inventory_notional > 0:
        inv_ratio = _clamp(inv_notional / config.max_inventory_notional, -1.0, 1.0)

    if not features.quoting_health_ok:
        return QuotePlan(
            quoting_enabled=False,
            quoting_reason=features.quoting_health_reason,
            fair_value=features.microprice,
            reservation_price=features.microprice,
            alpha_bps=0.0,
            inventory_skew_bps=0.0,
            target_half_spread_bps=0.0,
            bid_price=None,
            ask_price=None,
            bid_size=0.0,
            ask_size=0.0,
            bid_queue_ahead_size=0.0,
            ask_queue_ahead_size=0.0,
            bid_enabled=False,
            ask_enabled=False,
            bid_reason=features.quoting_health_reason,
            ask_reason=features.quoting_health_reason,
            quote_mode="flat",
            decision_note=f"flat: {features.quoting_health_reason}",
            event_regime=features.event_regime,
            toxicity_score=features.toxicity_score,
        )

    base_qty = config.base_order_notional / max(features.mid, 1e-9)
    fair_value = features.microprice
    reservation_price = features.microprice
    alpha_bps = 0.0
    inventory_skew_bps = 0.0

    if protection_reason is not None and inventory_qty != 0.0:
        exit_side = "SELL" if inventory_qty > 0 else "BUY"
        exit_size = abs(inventory_qty)
        bid_price = None
        ask_price = None
        bid_queue = 0.0
        ask_queue = 0.0
        if exit_side == "SELL":
            ask_price = _round_up(features.ask, features.tick_size)
            if book is not None:
                ask_queue = visible_size_at_price(book.asks, ask_price, features.tick_size)
        else:
            bid_price = _round_down(features.bid, features.tick_size)
            if book is not None:
                bid_queue = visible_size_at_price(book.bids, bid_price, features.tick_size)
        quote_mode = "ask_only" if exit_side == "SELL" else "bid_only"
        decision_note = (
            f"{quote_mode}: passive inventory protection at touch because {protection_reason}; "
            f"flow={features.flow_imbalance:.3f} book={features.book_imbalance:.3f} "
            f"impulse={features.impulse_bps:.2f}bps"
        )
        return QuotePlan(
            quoting_enabled=True,
            quoting_reason="inventory_protection",
            fair_value=fair_value,
            reservation_price=reservation_price,
            alpha_bps=alpha_bps,
            inventory_skew_bps=inventory_skew_bps,
            target_half_spread_bps=max(features.tick_bps, features.spread_bps / 2.0),
            bid_price=bid_price,
            ask_price=ask_price,
            bid_size=exit_size if bid_price is not None else 0.0,
            ask_size=exit_size if ask_price is not None else 0.0,
            bid_queue_ahead_size=bid_queue,
            ask_queue_ahead_size=ask_queue,
            bid_enabled=bid_price is not None,
            ask_enabled=ask_price is not None,
            bid_reason="inventory_protection" if bid_price is not None else "inventory_protection_off",
            ask_reason="inventory_protection" if ask_price is not None else "inventory_protection_off",
            quote_mode=quote_mode,
            decision_note=decision_note,
            event_regime=features.event_regime,
            toxicity_score=features.toxicity_score,
        )

    if config.stop_quoting_on_event and features.event_regime:
        return QuotePlan(
            quoting_enabled=False,
            quoting_reason="event_regime",
            fair_value=features.microprice,
            reservation_price=features.microprice,
            alpha_bps=0.0,
            inventory_skew_bps=0.0,
            target_half_spread_bps=0.0,
            bid_price=None,
            ask_price=None,
            bid_size=0.0,
            ask_size=0.0,
            bid_queue_ahead_size=0.0,
            ask_queue_ahead_size=0.0,
            bid_enabled=False,
            ask_enabled=False,
            bid_reason="event_regime",
            ask_reason="event_regime",
            quote_mode="flat",
            decision_note="flat: event regime",
            event_regime=True,
            toxicity_score=features.toxicity_score,
        )

    if (
        features.toxicity_score >= config.quote_guard_toxicity
        or (
            abs(features.flow_imbalance) >= config.quote_guard_flow_imbalance
            and abs(features.book_imbalance) >= config.quote_guard_book_imbalance
        )
    ):
        return QuotePlan(
            quoting_enabled=False,
            quoting_reason="toxicity_guard",
            fair_value=features.microprice,
            reservation_price=features.microprice,
            alpha_bps=0.0,
            inventory_skew_bps=0.0,
            target_half_spread_bps=0.0,
            bid_price=None,
            ask_price=None,
            bid_size=0.0,
            ask_size=0.0,
            bid_queue_ahead_size=0.0,
            ask_queue_ahead_size=0.0,
            bid_enabled=False,
            ask_enabled=False,
            bid_reason="toxicity_guard",
            ask_reason="toxicity_guard",
            quote_mode="flat",
            decision_note=(
                f"flat: toxicity guard flow={features.flow_imbalance:.3f} "
                f"book={features.book_imbalance:.3f} tox={features.toxicity_score:.3f}"
            ),
            event_regime=features.event_regime,
            toxicity_score=features.toxicity_score,
        )

    current_half_spread_bps = max(features.tick_bps, features.spread_bps / 2.0)
    latency_ratio = _clamp(features.quote_age_ms / max(config.max_quote_age_ms, 1), 0.0, 1.0)
    required_fee_edge_bps = required_edge_bps(
        fee_state=fee_state,
        expected_taker_share=expected_taker_share,
        fee_buffer_bps=config.fee_buffer_bps,
    )
    half_spread_bps = max(
        config.min_half_spread_bps + config.target_edge_bps + required_fee_edge_bps,
        current_half_spread_bps * 0.85,
        config.min_half_spread_bps
        + (features.recent_vol_bps * config.vol_spread_multiplier)
        + (features.toxicity_score * config.toxicity_spread_multiplier)
        + (latency_ratio * config.latency_spread_multiplier),
    )

    alpha_bps = (
        (features.flow_imbalance * 0.65)
        + (features.book_imbalance * 0.45)
        + ((_clamp(features.impulse_bps / max(config.event_impulse_bps, 1.0), -1.0, 1.0)) * 0.20)
    ) * max(current_half_spread_bps, config.min_half_spread_bps)
    fair_value = features.microprice * (1.0 + (alpha_bps / 10_000.0))

    inventory_skew_bps = -inv_ratio * config.inventory_skew_bps
    reservation_price = fair_value * (1.0 + (inventory_skew_bps / 10_000.0))

    bid_price = _round_down(
        min(
            reservation_price * (1.0 - (half_spread_bps / 10_000.0)),
            features.ask - features.tick_size,
        ),
        features.tick_size,
    )
    ask_price = _round_up(
        max(
            reservation_price * (1.0 + (half_spread_bps / 10_000.0)),
            features.bid + features.tick_size,
        ),
        features.tick_size,
    )

    if bid_price >= ask_price:
        bid_price = _round_down(features.bid, features.tick_size)
        ask_price = _round_up(features.ask, features.tick_size)

    bid_queue = 0.0
    ask_queue = 0.0
    bid_top_level_qty = 0.0
    ask_top_level_qty = 0.0
    if book is not None:
        bid_top_level_qty = top_level_size(book.bids)
        ask_top_level_qty = top_level_size(book.asks)
        if bid_price is not None:
            bid_queue = visible_size_at_price(book.bids, bid_price, features.tick_size)
        if ask_price is not None:
            ask_queue = visible_size_at_price(book.asks, ask_price, features.tick_size)

    target_gap_ratio = 0.0
    if fee_state.target_tier_threshold and fee_state.target_tier_threshold > 0:
        target_gap_ratio = _clamp(
            1.0 - (fee_state.projected_weighted_14d_volume / fee_state.target_tier_threshold),
            0.0,
            1.0,
        )
    healthy_volume_score = _clamp(
        1.0
        - max(
            features.toxicity_score * 0.55,
            abs(features.flow_imbalance) * 0.35,
            abs(features.book_imbalance) * 0.20,
        ),
        0.0,
        1.0,
    )
    volume_boost_multiplier = 1.0 + (
        config.tier_volume_boost_multiplier * target_gap_ratio * healthy_volume_score
    )
    spread_risk = _clamp(features.spread_bps / max(config.max_spread_bps, 1e-9), 0.0, 1.0)
    vol_risk = _clamp(features.recent_vol_bps / max(config.event_vol_bps, 1e-9), 0.0, 1.0)
    inventory_risk = abs(inv_ratio)
    size_risk_multiplier = _clamp(
        1.0
        - (features.toxicity_score * config.size_toxicity_penalty)
        - (vol_risk * config.size_vol_penalty)
        - (spread_risk * config.size_spread_penalty)
        - (inventory_risk * config.size_inventory_penalty),
        0.35,
        1.0,
    )

    def compute_side_quote_qty(
        *,
        side_base_qty: float,
        top_level_qty: float,
        side_depth_qty: float,
        queue_ahead_qty: float,
    ) -> float:
        if side_base_qty <= 0:
            return 0.0
        remaining_inventory_qty = float("inf")
        if config.max_inventory_notional > 0:
            remaining_inventory_qty = max(
                0.0,
                (config.max_inventory_notional - abs(inv_notional)) / max(features.mid, 1e-9),
            )
        max_notional_qty = float("inf")
        if config.max_quote_notional > 0:
            max_notional_qty = config.max_quote_notional / max(features.mid, 1e-9)
        touch_cap_qty = float("inf")
        if top_level_qty > 0:
            touch_cap_qty = top_level_qty * config.max_top_level_share
        depth_cap_qty = float("inf")
        if side_depth_qty > 0:
            depth_cap_qty = side_depth_qty * config.max_depth_share
        cap_qty = min(remaining_inventory_qty, max_notional_qty, touch_cap_qty, depth_cap_qty)
        if not math.isfinite(cap_qty):
            cap_qty = side_base_qty * config.max_quote_size_multiplier
        if cap_qty <= 0:
            return 0.0
        cap_qty *= (0.55 + (0.45 * size_risk_multiplier))

        top_level_richness = max(top_level_qty / max(side_base_qty, 1e-9), 1.0)
        depth_richness = max(side_depth_qty / max(side_base_qty, 1e-9), 1.0)
        spread_bonus = _clamp(
            (current_half_spread_bps / max(config.min_half_spread_bps, 1e-9)) - 1.0,
            0.0,
            2.5,
        )
        top_level_bonus = 0.16 * math.sqrt(top_level_richness - 1.0)
        depth_bonus = 0.14 * math.sqrt(depth_richness - 1.0)
        desired_multiplier = 1.0 + (0.16 * spread_bonus) + top_level_bonus + depth_bonus
        desired_multiplier = _clamp(
            desired_multiplier,
            config.min_quote_size_multiplier,
            config.max_quote_size_multiplier,
        )
        desired_qty = side_base_qty * desired_multiplier
        queue_reference_qty = max(top_level_qty, side_base_qty, 1e-9)
        queue_grace_qty = 0.35 * queue_reference_qty
        queue_excess_qty = max(0.0, queue_ahead_qty - queue_grace_qty)
        queue_ratio = queue_excess_qty / queue_reference_qty
        desired_qty *= 1.0 / (1.0 + (config.queue_ahead_penalty * queue_ratio))
        desired_qty *= volume_boost_multiplier
        desired_qty *= size_risk_multiplier

        min_qty = min(side_base_qty * config.min_quote_size_multiplier * max(0.65, size_risk_multiplier), cap_qty)
        target_qty = min(desired_qty, cap_qty)
        return max(min_qty, target_qty)

    same_side_limit_reached = abs(inv_notional) >= config.max_inventory_notional
    bid_size_scale = 1.0
    ask_size_scale = 1.0
    if inv_ratio > 0:
        bid_size_scale = max(0.0, 1.0 - (1.5 * inv_ratio))
        ask_size_scale = min(1.5, 1.0 + (0.4 * inv_ratio))
    elif inv_ratio < 0:
        ask_size_scale = max(0.0, 1.0 - (1.5 * abs(inv_ratio)))
        bid_size_scale = min(1.5, 1.0 + (0.4 * abs(inv_ratio)))

    bid_size = compute_side_quote_qty(
        side_base_qty=base_qty * bid_size_scale,
        top_level_qty=bid_top_level_qty,
        side_depth_qty=features.bid_depth,
        queue_ahead_qty=bid_queue,
    )
    ask_size = compute_side_quote_qty(
        side_base_qty=base_qty * ask_size_scale,
        top_level_qty=ask_top_level_qty,
        side_depth_qty=features.ask_depth,
        queue_ahead_qty=ask_queue,
    )
    bid_enabled = True
    ask_enabled = True
    bid_reason = "active"
    ask_reason = "active"
    directional_bias = (
        (features.flow_imbalance / max(config.one_way_flow_imbalance, 1e-9)) * 0.65
        + (features.book_imbalance / max(config.one_way_book_imbalance, 1e-9)) * 0.35
    )
    alpha_gate = max(config.one_way_alpha_bps, current_half_spread_bps * 0.35) * (
        1.0 + (config.tier_volume_relaxation_multiplier * target_gap_ratio * healthy_volume_score)
    )
    relaxed_threshold = 1.0 + (
        config.tier_volume_relaxation_multiplier * target_gap_ratio * healthy_volume_score
    )
    if directional_bias <= -relaxed_threshold or alpha_bps <= -alpha_gate:
        bid_enabled = False
        bid_reason = "sell_pressure"
        bid_size = 0.0
        bid_price = None
    if directional_bias >= relaxed_threshold or alpha_bps >= alpha_gate:
        ask_enabled = False
        ask_reason = "buy_pressure"
        ask_size = 0.0
        ask_price = None

    if same_side_limit_reached:
        if inventory_qty > 0:
            bid_size = 0.0
            bid_enabled = False
            bid_reason = "inventory_limit"
            bid_price = None
        elif inventory_qty < 0:
            ask_size = 0.0
            ask_enabled = False
            ask_reason = "inventory_limit"
            ask_price = None

    if bid_size <= 0 and ask_size <= 0:
        return QuotePlan(
            quoting_enabled=False,
            quoting_reason="one_way_guard" if (bid_reason != "active" or ask_reason != "active") else "inventory_limit",
            fair_value=fair_value,
            reservation_price=reservation_price,
            alpha_bps=alpha_bps,
            inventory_skew_bps=inventory_skew_bps,
            target_half_spread_bps=half_spread_bps,
            bid_price=None,
            ask_price=None,
            bid_size=0.0,
            ask_size=0.0,
            bid_queue_ahead_size=0.0,
            ask_queue_ahead_size=0.0,
            bid_enabled=False,
            ask_enabled=False,
            bid_reason=bid_reason,
            ask_reason=ask_reason,
            quote_mode="flat",
            decision_note=(
                f"flat: bid={bid_reason} ask={ask_reason} "
                f"flow={features.flow_imbalance:.3f} book={features.book_imbalance:.3f} "
                f"alpha={alpha_bps:.3f}"
            ),
            event_regime=features.event_regime,
            toxicity_score=features.toxicity_score,
        )

    if bid_enabled and ask_enabled:
        quote_mode = "both"
    elif bid_enabled:
        quote_mode = "bid_only"
    else:
        quote_mode = "ask_only"

    decision_note = (
        f"{quote_mode}: flow={features.flow_imbalance:.3f} book={features.book_imbalance:.3f} "
        f"alpha={alpha_bps:.3f} half_spread={half_spread_bps:.2f}bps "
        f"fee_edge={required_fee_edge_bps:.2f}bps vol_boost={volume_boost_multiplier:.2f} "
        f"size_risk={size_risk_multiplier:.2f} "
        f"bid={bid_reason} ask={ask_reason}"
    )

    return QuotePlan(
        quoting_enabled=True,
        quoting_reason="active",
        fair_value=fair_value,
        reservation_price=reservation_price,
        alpha_bps=alpha_bps,
        inventory_skew_bps=inventory_skew_bps,
        target_half_spread_bps=half_spread_bps,
        bid_price=bid_price if bid_size > 0 else None,
        ask_price=ask_price if ask_size > 0 else None,
        bid_size=bid_size,
        ask_size=ask_size,
        bid_queue_ahead_size=bid_queue,
        ask_queue_ahead_size=ask_queue,
        bid_enabled=bid_enabled,
        ask_enabled=ask_enabled,
        bid_reason=bid_reason,
        ask_reason=ask_reason,
        quote_mode=quote_mode,
        decision_note=decision_note,
        event_regime=features.event_regime,
        toxicity_score=features.toxicity_score,
        size_risk_multiplier=size_risk_multiplier,
    )


def disable_quote_plan(
    plan: QuotePlan,
    *,
    reason: str,
    decision_note: str,
) -> QuotePlan:
    return QuotePlan(
        quoting_enabled=False,
        quoting_reason=reason,
        fair_value=plan.fair_value,
        reservation_price=plan.reservation_price,
        alpha_bps=plan.alpha_bps,
        inventory_skew_bps=plan.inventory_skew_bps,
        target_half_spread_bps=plan.target_half_spread_bps,
        bid_price=None,
        ask_price=None,
        bid_size=0.0,
        ask_size=0.0,
        bid_queue_ahead_size=0.0,
        ask_queue_ahead_size=0.0,
        bid_enabled=False,
        ask_enabled=False,
        bid_reason=reason,
        ask_reason=reason,
        quote_mode="flat",
        decision_note=decision_note,
        event_regime=plan.event_regime,
        toxicity_score=plan.toxicity_score,
        size_risk_multiplier=plan.size_risk_multiplier,
    )


def _inventory_markout_bps(
    *,
    features: MarketFeatures,
    inventory_qty: float,
    inventory_avg_price: Optional[float],
    ) -> Optional[float]:
    if inventory_qty == 0 or inventory_avg_price is None:
        return None
    exit_price = features.bid if inventory_qty > 0 else features.ask
    markout_bps = _bps_from_prices(exit_price, inventory_avg_price)
    if inventory_qty < 0:
        markout_bps *= -1.0
    return markout_bps


def inventory_protection_reason(
    *,
    features: MarketFeatures,
    inventory_qty: float,
    inventory_avg_price: Optional[float],
    inventory_opened_at_ts: Optional[float],
    config: MMStrategyConfig,
) -> Optional[str]:
    if inventory_qty == 0 or inventory_avg_price is None or inventory_opened_at_ts is None:
        return None
    markout_bps = _inventory_markout_bps(
        features=features,
        inventory_qty=inventory_qty,
        inventory_avg_price=inventory_avg_price,
    )
    if markout_bps is None:
        return None
    hold_seconds = max(0.0, time.time() - inventory_opened_at_ts)
    inventory_risk_ratio = 0.0
    if config.max_inventory_notional > 0:
        inventory_risk_ratio = _clamp(
            abs(inventory_qty * features.mid) / config.max_inventory_notional,
            0.0,
            1.0,
        )

    if config.stop_quoting_on_event and features.event_regime:
        return "event_regime_protection"
    if markout_bps <= -abs(config.protection_markout_bps):
        return "markout_protection"
    if inventory_risk_ratio >= config.large_inventory_protection_ratio:
        if markout_bps <= -(abs(config.protection_markout_bps) * 0.5):
            return "size_protection"
    if inventory_qty > 0:
        if (
            features.flow_imbalance <= -abs(config.protection_flow_imbalance)
            and features.book_imbalance <= -abs(config.protection_book_imbalance)
        ):
            return "toxic_protection"
        if inventory_risk_ratio >= config.large_inventory_protection_ratio and markout_bps <= 0.0:
            return "size_toxic_protection"
    else:
        if (
            features.flow_imbalance >= abs(config.protection_flow_imbalance)
            and features.book_imbalance >= abs(config.protection_book_imbalance)
        ):
            return "toxic_protection"
        if inventory_risk_ratio >= config.large_inventory_protection_ratio and markout_bps <= 0.0:
            return "size_toxic_protection"
    if hold_seconds >= config.max_inventory_hold_seconds and markout_bps <= 0.0:
        return "inventory_timeout_protection"
    return None


def aggressive_flatten_reason(
    *,
    features: MarketFeatures,
    inventory_qty: float,
    inventory_avg_price: Optional[float],
    inventory_opened_at_ts: Optional[float],
    config: MMStrategyConfig,
    fee_state: FeeState,
    protection_reason: Optional[str] = None,
) -> Optional[str]:
    if inventory_qty == 0 or inventory_avg_price is None or inventory_opened_at_ts is None:
        return None
    markout_bps = _inventory_markout_bps(
        features=features,
        inventory_qty=inventory_qty,
        inventory_avg_price=inventory_avg_price,
    )
    if markout_bps is None:
        return None
    hold_seconds = max(0.0, time.time() - inventory_opened_at_ts)
    inventory_risk_ratio = 0.0
    if config.max_inventory_notional > 0:
        inventory_risk_ratio = _clamp(
            abs(inventory_qty * features.mid) / config.max_inventory_notional,
            0.0,
            1.0,
        )
    dynamic_kill_bps = max(
        abs(config.adverse_exit_bps),
        dynamic_kill_floor_bps(
            fee_state=fee_state,
            slippage_bps=config.slippage_bps,
            kill_fee_buffer_bps=config.fee_kill_buffer_bps,
        ),
    )
    event_kill_bps = max(abs(config.kill_on_event_loss_bps), dynamic_kill_bps * 0.65)
    timeout_floor_bps = max(
        abs(config.protection_markout_bps) * max(0.60, 1.0 - (0.35 * inventory_risk_ratio)),
        dynamic_kill_bps * max(0.35, 0.55 - (0.15 * inventory_risk_ratio)),
    )
    patience_seconds = max(
        6.0,
        (config.kill_hold_seconds + (fee_state.taker_rate_bps * 0.35)) * (1.0 - (0.45 * inventory_risk_ratio)),
    )
    if protection_reason is not None and hold_seconds < patience_seconds and markout_bps > -(dynamic_kill_bps + 1.0):
        return None

    if markout_bps <= -dynamic_kill_bps:
        return "kill_markout"
    if config.stop_quoting_on_event and features.event_regime and markout_bps <= -event_kill_bps:
        return "kill_event_loss"
    if inventory_qty > 0:
        if (
            features.flow_imbalance <= -abs(config.adverse_flow_exit_imbalance)
            and features.book_imbalance <= -abs(config.adverse_book_exit_imbalance)
            and markout_bps <= -timeout_floor_bps
        ):
            return "kill_toxic_reversal"
    else:
        if (
            features.flow_imbalance >= abs(config.adverse_flow_exit_imbalance)
            and features.book_imbalance >= abs(config.adverse_book_exit_imbalance)
            and markout_bps <= -timeout_floor_bps
        ):
            return "kill_toxic_reversal"
    if (
        inventory_risk_ratio >= config.large_inventory_protection_ratio
        and hold_seconds >= max(4.0, patience_seconds * 0.6)
        and markout_bps <= -timeout_floor_bps
    ):
        return "kill_timeout"
    if hold_seconds >= patience_seconds and markout_bps <= -timeout_floor_bps:
        return "kill_timeout"
    return None


class ProSpreadMarketMaker:
    def __init__(
        self,
        *,
        asset: str,
        api_url: str,
        dex: str,
        config: MMStrategyConfig,
        events_jsonl_path: str,
        samples_jsonl_path: str,
        fills_csv_path: str,
        trades_csv_path: str,
        report_dir: str,
    ) -> None:
        self.asset = asset
        self.api_url = api_url
        self.dex = dex
        self.config = config
        self.events_jsonl_path = Path(events_jsonl_path)
        self.samples_jsonl_path = Path(samples_jsonl_path)
        self.fills_csv_path = Path(fills_csv_path)
        self.trades_csv_path = Path(trades_csv_path)
        self.report_dir = Path(report_dir)

        self.feed: Optional[HyperliquidRealtimeMultiFeed] = None
        self.started_at_ts = time.time()
        self.preflight_ok = False
        self._finalized = False
        self._report_path: Optional[Path] = None

        self.current_day_utc = self._utc_day()
        self.daily_realized_pnl = 0.0
        self.daily_episode_count = 0
        self.hourly_episode_closed_at: Deque[float] = deque()
        self.last_episode_closed_ts: Optional[float] = None

        self.price_samples: Deque[Tuple[int, float]] = deque()
        self.last_sample_exchange_time_ms: Optional[int] = None
        self.last_snapshot: Optional[MMSnapshot] = None
        self.last_quote_exchange_time_ms: Optional[int] = None
        self.last_quote_transport_delay_ms: Optional[int] = None
        self.tick_count = 0
        self.sample_count = 0
        self.poll_error_count = 0
        self.stale_quote_skips = 0
        self.last_error: Optional[str] = None
        self.quote_age_history_ms: List[float] = []
        self.transport_delay_history_ms: List[float] = []
        self.spread_history_bps: List[float] = []
        self.realized_spread_bps: List[float] = []

        self.bid_order: Optional[PassiveOrder] = None
        self.ask_order: Optional[PassiveOrder] = None
        self.order_seq = 0
        self.fill_seq = 0
        self.episode_seq = 0
        self.current_episode: Optional[InventoryEpisode] = None
        self.closed_episodes: List[InventoryEpisode] = []
        self.closed_episode_pnls: List[float] = []
        self.closed_episode_hold_seconds: List[float] = []
        self.closed_episode_markouts: List[float] = []
        self.exit_reason_counts: Dict[str, int] = {}
        self.processed_trade_hashes: Deque[Tuple[str, int]] = deque()
        self.processed_trade_hash_set: set[str] = set()
        self.last_fill_exchange_time_ms = 0
        self.perp_fill_turnover = 0.0
        self.maker_fill_turnover = 0.0
        self.taker_fill_turnover = 0.0
        self.estimated_exchange_fees = 0.0
        self.estimated_maker_fees = 0.0
        self.estimated_taker_fees = 0.0
        self.estimated_maker_rebates = 0.0

        self._quote_gap_active = False
        self._quote_gap_started_ms: Optional[int] = None
        self._last_reconnect_attempt_ms = 0
        self._last_stale_skip_event_ms = 0

        self.trade_retention_ms = int(
            max(
                self.config.volatility_lookback_seconds,
                self.config.impulse_window_seconds,
                self.config.flow_window_seconds,
            )
            * 1000.0
        ) + 180_000

        self.fee_config = HyperliquidFeeConfig(
            product=self.config.fee_product,
            market_type=self.config.fee_market_type,
            staking_tier=self.config.fee_staking_tier,
            tier_basis=self.config.fee_tier_basis,
            target_tier=self.config.fee_target_tier,
            initial_14d_perps_volume=self.config.fee_initial_14d_perps_volume,
            initial_14d_spot_volume=self.config.fee_initial_14d_spot_volume,
            taker_referral_discount_pct=self.config.fee_taker_referral_discount_pct,
            maker_rebate_bps_override=self.config.fee_maker_rebate_bps_override,
            deployer_fee_scale=self.config.fee_deployer_fee_scale,
            growth_mode=self.config.fee_growth_mode,
            aligned_quote_token=self.config.fee_aligned_quote_token,
            user_maker_rate_pct_override=self.config.fee_user_maker_rate_pct_override,
            user_taker_rate_pct_override=self.config.fee_user_taker_rate_pct_override,
        )
        self.fee_pct = self.config.fee_bps / 10_000.0
        self.slippage_pct = self.config.slippage_bps / 10_000.0
        self._ensure_output_files()

    def _utc_day(self) -> date:
        return _utc_now().date()

    def _fee_state(self) -> FeeState:
        elapsed_seconds = max(1.0, time.time() - self.started_at_ts)
        return estimate_fee_state(
            elapsed_seconds=elapsed_seconds,
            perp_fill_turnover=self.perp_fill_turnover,
            config=self.fee_config,
        )

    def _expected_taker_share(self) -> float:
        if self.perp_fill_turnover <= 0:
            return max(0.0, self.config.expected_taker_share_floor)
        observed = self.taker_fill_turnover / self.perp_fill_turnover
        return max(self.config.expected_taker_share_floor, observed)

    def _post_info(self, payload: Dict[str, object]) -> object:
        endpoint = self.api_url.rstrip("/") + "/info"
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _configure_fee_model(self) -> None:
        market_type = self.fee_config.market_type
        deployer_fee_scale = self.fee_config.deployer_fee_scale
        growth_mode = self.fee_config.growth_mode
        aligned_quote_token = self.fee_config.aligned_quote_token
        user_maker_rate_pct_override = self.fee_config.user_maker_rate_pct_override
        user_taker_rate_pct_override = self.fee_config.user_taker_rate_pct_override
        active_referral_discount_pct = self.fee_config.taker_referral_discount_pct
        rate_source = "estimated_schedule"

        try:
            if self.dex:
                dexes = self._post_info({"type": "perpDexs"})
                if isinstance(dexes, list):
                    for item in dexes:
                        if isinstance(item, dict) and item.get("name") == self.dex:
                            deployer_fee_scale = float(item.get("deployerFeeScale") or deployer_fee_scale)
                            market_type = "hip3_default"
                            rate_source = "market_metadata"
                            break

                meta_ctxs = self._post_info({"type": "metaAndAssetCtxs", "dex": self.dex})
                if isinstance(meta_ctxs, list) and meta_ctxs:
                    meta = meta_ctxs[0] if isinstance(meta_ctxs[0], dict) else {}
                    universe = meta.get("universe", []) if isinstance(meta, dict) else []
                    for asset_meta in universe:
                        if isinstance(asset_meta, dict) and asset_meta.get("name") == self.asset:
                            growth_mode = asset_meta.get("growthMode") == "enabled"
                            market_type = "hip3_growth" if growth_mode else "hip3_default"
                            rate_source = "market_metadata"
                            break
                    collateral_token = meta.get("collateralToken") if isinstance(meta, dict) else None
                    if collateral_token is not None:
                        try:
                            aligned_info = self._post_info(
                                {"type": "alignedQuoteTokenInfo", "token": int(collateral_token)}
                            )
                            aligned_quote_token = aligned_info is not None
                        except Exception:
                            aligned_quote_token = False
        except Exception:
            logging.exception("Failed loading exact Hyperliquid market fee metadata; using configured defaults")

        manual_rate_override = (
            user_maker_rate_pct_override is not None or user_taker_rate_pct_override is not None
        )
        if manual_rate_override:
            rate_source = "manual_account_rates"

        user_address = (self.config.fee_user_address or "").strip()
        if user_address and not manual_rate_override:
            try:
                user_fees = self._post_info({"type": "userFees", "user": user_address})
                if isinstance(user_fees, dict):
                    if user_fees.get("userAddRate") is not None:
                        user_maker_rate_pct_override = float(user_fees["userAddRate"]) * 100.0
                    if user_fees.get("userCrossRate") is not None:
                        user_taker_rate_pct_override = float(user_fees["userCrossRate"]) * 100.0
                    if user_fees.get("activeReferralDiscount") is not None:
                        active_referral_discount_pct = float(user_fees["activeReferralDiscount"]) * 100.0
                    rate_source = "userFees"
            except Exception:
                logging.exception("Failed loading exact Hyperliquid userFees for %s; using estimated schedule", user_address)

        self.fee_config = replace(
            self.fee_config,
            market_type=market_type,
            deployer_fee_scale=deployer_fee_scale,
            growth_mode=growth_mode,
            aligned_quote_token=aligned_quote_token,
            user_maker_rate_pct_override=user_maker_rate_pct_override,
            user_taker_rate_pct_override=user_taker_rate_pct_override,
            taker_referral_discount_pct=active_referral_discount_pct,
            user_fee_source=rate_source,
        )
        self._write_event(
            "fee_model_configured",
            fee_market_type=market_type,
            fee_deployer_fee_scale=deployer_fee_scale,
            fee_growth_mode=growth_mode,
            fee_aligned_quote_token=aligned_quote_token,
            fee_rate_source=rate_source,
            fee_user_address=user_address or None,
            fee_user_maker_rate_pct_override=user_maker_rate_pct_override,
            fee_user_taker_rate_pct_override=user_taker_rate_pct_override,
            fee_active_referral_discount_pct=active_referral_discount_pct,
        )

    def _ensure_output_files(self) -> None:
        self.events_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self.samples_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self.fills_csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.trades_csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        if not self.fills_csv_path.exists():
            with self.fills_csv_path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    [
                        "timestamp_utc",
                        "fill_id",
                        "episode_id",
                        "liquidity_role",
                        "side",
                        "price",
                        "size",
                        "notional",
                        "order_id",
                        "reason",
                        "exchange_fee_delta",
                        "fee_rate_bps",
                        "fee_tier_label",
                        "fee_actual_tier_label",
                        "fee_projected_tier_label",
                        "maker_rate_bps",
                        "taker_rate_bps",
                        "maker_rebate_bps",
                        "quote_age_ms",
                        "transport_delay_ms",
                        "spread_bps",
                        "flow_imbalance",
                        "book_imbalance",
                        "recent_vol_bps",
                        "inventory_qty_after",
                        "inventory_avg_price_after",
                        "realized_pnl_delta",
                    ]
                )
        if not self.trades_csv_path.exists():
            with self.trades_csv_path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    [
                        "episode_id",
                        "side",
                        "open_time_utc",
                        "close_time_utc",
                        "entry_price_avg",
                        "exit_price_avg",
                        "max_abs_qty",
                        "realized_pnl",
                        "gross_pnl",
                        "fees_paid",
                        "maker_fee_cost",
                        "taker_fee_cost",
                        "maker_rebates",
                        "maker_notional",
                        "taker_notional",
                        "hold_seconds",
                        "best_markout_bps",
                        "worst_markout_bps",
                        "passive_entry_fills",
                        "passive_exit_fills",
                        "aggressive_exit_fills",
                        "fill_count",
                        "close_reason",
                    ]
                )

    def _write_event(self, event: str, **fields: object) -> None:
        payload = {"timestamp_utc": _utc_iso(), "asset": self.asset, "event": event, **fields}
        try:
            with self.events_jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except Exception:
            logging.exception("Failed writing market-maker event JSONL")

    def _write_sample(self, snapshot: MMSnapshot) -> None:
        payload = {"timestamp_utc": _utc_iso(), "asset": self.asset, **snapshot.to_dict()}
        try:
            with self.samples_jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except Exception:
            logging.exception("Failed writing market-maker sample JSONL")

    def _write_fill_row(
        self,
        *,
        fill_id: int,
        episode_id: Optional[int],
        liquidity_role: str,
        side: str,
        price: float,
        size: float,
        order_id: Optional[int],
        reason: str,
        snapshot: MMSnapshot,
        realized_pnl_delta: float,
        exchange_fee_delta: float,
        fee_state: FeeState,
    ) -> None:
        try:
            with self.fills_csv_path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    [
                        _utc_iso(),
                        fill_id,
                        episode_id if episode_id is not None else "",
                        liquidity_role,
                        side,
                        f"{price:.8f}",
                        f"{size:.8f}",
                        f"{price * size:.8f}",
                        order_id if order_id is not None else "",
                        reason,
                        f"{exchange_fee_delta:.8f}",
                        f"{fee_state.taker_rate_bps if liquidity_role == 'aggressive' else fee_state.net_maker_rate_bps:.4f}",
                        fee_state.tier_label,
                        f"Tier{fee_state.actual_tier_index}",
                        f"Tier{fee_state.projected_tier_index}",
                        f"{fee_state.maker_rate_bps:.4f}",
                        f"{fee_state.taker_rate_bps:.4f}",
                        f"{fee_state.maker_rebate_bps:.4f}",
                        snapshot.quote_age_ms,
                        snapshot.transport_delay_ms,
                        f"{snapshot.spread_bps:.6f}",
                        f"{snapshot.flow_imbalance:.6f}",
                        f"{snapshot.book_imbalance:.6f}",
                        f"{snapshot.recent_vol_bps:.6f}",
                        f"{snapshot.inventory_qty:.8f}",
                        f"{snapshot.inventory_avg_price:.8f}" if snapshot.inventory_avg_price else "",
                        f"{realized_pnl_delta:.8f}",
                    ]
                )
        except Exception:
            logging.exception("Failed writing fill CSV")

    def _write_episode_row(
        self,
        *,
        episode: InventoryEpisode,
        exit_price_avg: Optional[float],
        hold_seconds: float,
    ) -> None:
        try:
            with self.trades_csv_path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    [
                        episode.episode_id,
                        episode.direction,
                        datetime.fromtimestamp(episode.opened_at_ts, tz=timezone.utc).isoformat(),
                        datetime.fromtimestamp(episode.closed_at_ts, tz=timezone.utc).isoformat()
                        if episode.closed_at_ts is not None
                        else "",
                        f"{episode.avg_price:.8f}",
                        f"{exit_price_avg:.8f}" if exit_price_avg is not None else "",
                        f"{episode.max_abs_qty:.8f}",
                        f"{episode.realized_pnl:.8f}",
                        f"{episode.gross_pnl:.8f}",
                        f"{episode.fees_paid:.8f}",
                        f"{episode.maker_fee_cost:.8f}",
                        f"{episode.taker_fee_cost:.8f}",
                        f"{episode.maker_rebates:.8f}",
                        f"{episode.maker_notional:.8f}",
                        f"{episode.taker_notional:.8f}",
                        f"{hold_seconds:.4f}",
                        f"{episode.best_markout_bps:.6f}",
                        f"{episode.worst_markout_bps:.6f}",
                        episode.passive_entry_fills,
                        episode.passive_exit_fills,
                        episode.aggressive_exit_fills,
                        episode.fill_count,
                        episode.close_reason or "",
                    ]
                )
        except Exception:
            logging.exception("Failed writing episode CSV")

    def _trim_hourly_episodes(self, now_ts: float) -> None:
        cutoff = now_ts - 3600.0
        while self.hourly_episode_closed_at and self.hourly_episode_closed_at[0] < cutoff:
            self.hourly_episode_closed_at.popleft()

    def _quoting_block_reason(self, now_ts: float) -> Optional[str]:
        self._trim_hourly_episodes(now_ts)
        if self.daily_realized_pnl <= -abs(self.config.max_daily_loss):
            return "daily_loss_limit"
        if (
            self.config.max_episodes_per_day > 0
            and self.daily_episode_count >= self.config.max_episodes_per_day
        ):
            return "daily_episode_limit"
        if (
            self.config.max_episodes_per_hour > 0
            and len(self.hourly_episode_closed_at) >= self.config.max_episodes_per_hour
        ):
            return "hourly_episode_limit"
        if self.last_episode_closed_ts is not None and (
            now_ts - self.last_episode_closed_ts
        ) < self.config.cooldown_seconds:
            return "cooldown"
        return None

    def _quoting_allowed_now(self, now_ts: float) -> bool:
        return self._quoting_block_reason(now_ts) is None

    def _roll_day_if_needed(self) -> None:
        day = self._utc_day()
        if day == self.current_day_utc:
            return
        self.current_day_utc = day
        self.daily_realized_pnl = 0.0
        self.daily_episode_count = 0
        self.hourly_episode_closed_at.clear()
        self._write_event("daily_reset", reset_day=str(day))

    def _append_price_sample(self, ts_ms: int, microprice: float) -> None:
        self.price_samples.append((ts_ms, microprice))
        oldest_keep_ms = ts_ms - int(
            max(
                self.config.volatility_lookback_seconds,
                self.config.impulse_window_seconds,
                self.config.flow_window_seconds,
            )
            * 1000.0
        ) - 180_000
        while len(self.price_samples) > 1 and self.price_samples[0][0] < oldest_keep_ms:
            self.price_samples.popleft()

    def _warm_start_prices(self, startup_microprice: float) -> int:
        if not self.config.warm_start_candles or self.feed is None:
            return 0
        lookback_minutes = max(
            self.config.volatility_lookback_seconds,
            self.config.impulse_window_seconds,
            self.config.flow_window_seconds,
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
        self.last_sample_exchange_time_ms = now_ms if self.config.startup_quote_immediately else None
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

    def _compute_market_features(self, market: MarketSnapshot) -> MarketFeatures:
        quote = market.quote
        now_ms = int(time.time() * 1000)
        quote_age_ms = int(_clamp_non_negative(now_ms - quote.exchange_time_ms))
        tick_size = infer_tick_size(market.book, quote.bid, quote.ask)
        tick_bps = (tick_size / quote.mid) * 10_000.0 if quote.mid > 0 else 0.0

        impulse_ref = None
        if self.price_samples:
            impulse_cutoff = quote.exchange_time_ms - int(self.config.impulse_window_seconds * 1000.0)
            for ts_ms, price in self.price_samples:
                if ts_ms <= impulse_cutoff:
                    impulse_ref = price
                elif impulse_ref is not None:
                    break
            if impulse_ref is None and self.price_samples:
                impulse_ref = self.price_samples[0][1]
        impulse_bps = _bps_from_prices(quote.microprice, impulse_ref) if impulse_ref else 0.0

        vol_cutoff = quote.exchange_time_ms - int(self.config.volatility_lookback_seconds * 1000.0)
        recent_vol_bps = compute_realized_vol_bps(
            list(self.price_samples),
            since_ms=vol_cutoff,
            impulse_window_seconds=self.config.impulse_window_seconds,
        )
        flow = compute_trade_flow(
            market.trades,
            since_ms=quote.exchange_time_ms - int(self.config.flow_window_seconds * 1000.0),
        )
        book_imbalance, bid_depth, ask_depth = compute_book_imbalance(
            market.book,
            self.config.depth_levels,
        )
        toxicity_score = max(
            abs(flow["imbalance"]),
            abs(book_imbalance),
            abs(impulse_bps) / max(self.config.event_impulse_bps, 1.0),
        )
        event_regime = (
            abs(impulse_bps) >= self.config.event_impulse_bps
            or recent_vol_bps >= self.config.event_vol_bps
            or (
                abs(flow["imbalance"]) >= self.config.toxic_flow_imbalance
                and abs(book_imbalance) >= self.config.toxic_book_imbalance
            )
            or quote.spread_bps > self.config.max_spread_bps
        )

        quoting_health_ok = True
        quoting_health_reason = "healthy"
        if quote_age_ms > self.config.max_quote_age_ms:
            quoting_health_ok = False
            quoting_health_reason = "stale_quote"
        elif flow["count"] < self.config.min_trade_count:
            quoting_health_ok = False
            quoting_health_reason = "insufficient_flow"
        elif bid_depth <= 0 or ask_depth <= 0:
            quoting_health_ok = False
            quoting_health_reason = "empty_book"

        return MarketFeatures(
            sample_exchange_time_ms=quote.exchange_time_ms,
            sample_received_time_ms=quote.received_time_ms,
            quote_age_ms=quote_age_ms,
            transport_delay_ms=quote.transport_delay_ms,
            bid=quote.bid,
            ask=quote.ask,
            mid=quote.mid,
            microprice=quote.microprice,
            spread_bps=quote.spread_bps,
            tick_size=tick_size,
            tick_bps=tick_bps,
            recent_vol_bps=recent_vol_bps,
            impulse_bps=impulse_bps,
            flow_imbalance=flow["imbalance"],
            buy_volume=flow["buy_volume"],
            sell_volume=flow["sell_volume"],
            trade_count=int(flow["count"]),
            trade_rate_per_second=flow["trade_rate_per_second"],
            book_imbalance=book_imbalance,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            toxicity_score=toxicity_score,
            event_regime=event_regime,
            quoting_health_ok=quoting_health_ok,
            quoting_health_reason=quoting_health_reason,
        )

    def _inventory_qty(self) -> float:
        return self.current_episode.qty_signed if self.current_episode is not None else 0.0

    def _inventory_avg_price(self) -> Optional[float]:
        return self.current_episode.avg_price if self.current_episode is not None else None

    def _inventory_opened_at_ts(self) -> Optional[float]:
        return self.current_episode.opened_at_ts if self.current_episode is not None else None

    def _current_inventory_snapshot(
        self,
        features: MarketFeatures,
    ) -> Tuple[float, str, Optional[float], float, float, float, Optional[int]]:
        qty = self._inventory_qty()
        avg_price = self._inventory_avg_price()
        side = "FLAT"
        if qty > 0:
            side = "LONG"
        elif qty < 0:
            side = "SHORT"
        unrealized_pnl = 0.0
        unrealized_bps = 0.0
        hold_seconds = 0.0
        episode_id: Optional[int] = None
        if self.current_episode is not None and avg_price is not None:
            exit_price = features.bid if qty > 0 else features.ask
            unrealized_pnl = (exit_price - avg_price) * qty
            unrealized_bps = _bps_from_prices(exit_price, avg_price)
            if qty < 0:
                unrealized_bps *= -1.0
            hold_seconds = max(0.0, time.time() - self.current_episode.opened_at_ts)
            episode_id = self.current_episode.episode_id
        return qty, side, avg_price, unrealized_pnl, unrealized_bps, hold_seconds, episode_id

    def _build_snapshot(
        self,
        features: MarketFeatures,
        plan: QuotePlan,
        fee_state: FeeState,
    ) -> MMSnapshot:
        qty, side, avg_price, unrealized_pnl, unrealized_bps, hold_seconds, episode_id = (
            self._current_inventory_snapshot(features)
        )
        fee_required_edge = required_edge_bps(
            fee_state=fee_state,
            expected_taker_share=self._expected_taker_share(),
            fee_buffer_bps=self.config.fee_buffer_bps,
        )
        kill_floor = dynamic_kill_floor_bps(
            fee_state=fee_state,
            slippage_bps=self.config.slippage_bps,
            kill_fee_buffer_bps=self.config.fee_kill_buffer_bps,
        )
        actual_tier_label = f"Tier{fee_state.actual_tier_index}"
        projected_tier_label = f"Tier{fee_state.projected_tier_index}"
        target_gap_ratio = 0.0
        if fee_state.target_tier_threshold and fee_state.target_tier_threshold > 0:
            target_gap_ratio = _clamp(
                1.0 - (fee_state.projected_weighted_14d_volume / fee_state.target_tier_threshold),
                0.0,
                1.0,
            )
        healthy_volume_score = _clamp(
            1.0
            - max(
                features.toxicity_score * 0.55,
                abs(features.flow_imbalance) * 0.35,
                abs(features.book_imbalance) * 0.20,
            ),
            0.0,
            1.0,
        )
        volume_boost_multiplier = 1.0 + (
            self.config.tier_volume_boost_multiplier * target_gap_ratio * healthy_volume_score
        )
        return MMSnapshot(
            sample_exchange_time_ms=features.sample_exchange_time_ms,
            sample_received_time_ms=features.sample_received_time_ms,
            quote_age_ms=features.quote_age_ms,
            transport_delay_ms=features.transport_delay_ms,
            bid=features.bid,
            ask=features.ask,
            mid=features.mid,
            microprice=features.microprice,
            spread_bps=features.spread_bps,
            tick_size=features.tick_size,
            recent_vol_bps=features.recent_vol_bps,
            impulse_bps=features.impulse_bps,
            flow_imbalance=features.flow_imbalance,
            buy_volume=features.buy_volume,
            sell_volume=features.sell_volume,
            trade_count=features.trade_count,
            trade_rate_per_second=features.trade_rate_per_second,
            book_imbalance=features.book_imbalance,
            bid_depth=features.bid_depth,
            ask_depth=features.ask_depth,
            toxicity_score=features.toxicity_score,
            event_regime=features.event_regime,
            quoting_health_ok=features.quoting_health_ok,
            quoting_health_reason=features.quoting_health_reason,
            quoting_enabled=plan.quoting_enabled,
            quoting_reason=plan.quoting_reason,
            fair_value=plan.fair_value,
            reservation_price=plan.reservation_price,
            alpha_bps=plan.alpha_bps,
            inventory_skew_bps=plan.inventory_skew_bps,
            target_half_spread_bps=plan.target_half_spread_bps,
            bid_quote_price=plan.bid_price,
            ask_quote_price=plan.ask_price,
            bid_quote_size=plan.bid_size,
            ask_quote_size=plan.ask_size,
            bid_queue_ahead_size=plan.bid_queue_ahead_size,
            ask_queue_ahead_size=plan.ask_queue_ahead_size,
            bid_enabled=plan.bid_enabled,
            ask_enabled=plan.ask_enabled,
            bid_reason=plan.bid_reason,
            ask_reason=plan.ask_reason,
            quote_mode=plan.quote_mode,
            decision_note=plan.decision_note,
            live_bid_order_price=self.bid_order.price if self.bid_order is not None else None,
            live_ask_order_price=self.ask_order.price if self.ask_order is not None else None,
            live_bid_order_size=self.bid_order.remaining_size if self.bid_order is not None else 0.0,
            live_ask_order_size=self.ask_order.remaining_size if self.ask_order is not None else 0.0,
            inventory_qty=qty,
            inventory_side=side,
            inventory_avg_price=avg_price,
            inventory_unrealized_pnl=unrealized_pnl,
            inventory_unrealized_bps=unrealized_bps,
            inventory_hold_seconds=hold_seconds,
            episode_id=episode_id,
            sample_count=len(self.price_samples),
            fee_tier_label=fee_state.tier_label,
            fee_market_type=fee_state.market_type,
            fee_staking_tier=fee_state.staking_tier,
            fee_basis=self.config.fee_tier_basis,
            fee_rate_source=fee_state.fee_rate_source,
            fee_deployer_fee_scale=fee_state.deployer_fee_scale,
            fee_growth_mode=fee_state.growth_mode,
            fee_aligned_quote_token=fee_state.aligned_quote_token,
            fee_actual_tier_label=actual_tier_label,
            fee_projected_tier_label=projected_tier_label,
            fee_maker_rate_bps=fee_state.maker_rate_bps,
            fee_taker_rate_bps=fee_state.taker_rate_bps,
            fee_maker_rebate_bps=fee_state.maker_rebate_bps,
            fee_net_maker_rate_bps=fee_state.net_maker_rate_bps,
            fee_required_edge_bps=fee_required_edge,
            fee_dynamic_kill_floor_bps=kill_floor,
            fee_expected_taker_share_pct=self._expected_taker_share() * 100.0,
            fee_current_weighted_14d_volume=fee_state.current_weighted_14d_volume,
            fee_projected_weighted_14d_volume=fee_state.projected_weighted_14d_volume,
            fee_daily_volume_run_rate=fee_state.daily_weighted_volume_run_rate,
            fee_target_tier_label=f"Tier{fee_state.target_tier_index}",
            fee_target_tier_progress_pct=fee_state.progress_to_target_tier_pct or 0.0,
            fee_days_to_next_tier=fee_state.days_to_next_tier,
            volume_boost_multiplier=volume_boost_multiplier,
            size_risk_multiplier=plan.size_risk_multiplier,
        )

    def _process_trade_hash(self, trade: TradePrint) -> bool:
        if trade.hash in self.processed_trade_hash_set:
            return False
        self.processed_trade_hash_set.add(trade.hash)
        self.processed_trade_hashes.append((trade.hash, trade.exchange_time_ms))
        cutoff = trade.exchange_time_ms - self.trade_retention_ms
        while self.processed_trade_hashes and self.processed_trade_hashes[0][1] < cutoff:
            old_hash, _ = self.processed_trade_hashes.popleft()
            self.processed_trade_hash_set.discard(old_hash)
        return True

    def _cancel_order(self, side: str, reason: str) -> None:
        if side == "BUY":
            order = self.bid_order
            self.bid_order = None
        else:
            order = self.ask_order
            self.ask_order = None
        if order is not None:
            self._write_event(
                "quote_canceled",
                side=side,
                order_id=order.order_id,
                price=order.price,
                remaining_size=order.remaining_size,
                reason=reason,
            )

    def _place_order(
        self,
        *,
        side: str,
        price: float,
        size: float,
        queue_ahead_size: float,
        reason: str,
        exchange_time_ms: int,
    ) -> None:
        self.order_seq += 1
        order = PassiveOrder(
            order_id=self.order_seq,
            side=side,
            price=price,
            size=size,
            remaining_size=size,
            queue_ahead_size=queue_ahead_size,
            placed_exchange_time_ms=exchange_time_ms,
            placed_at_ts=time.time(),
            reason=reason,
        )
        if side == "BUY":
            self.bid_order = order
        else:
            self.ask_order = order
        self._write_event(
            "quote_posted",
            side=side,
            order_id=order.order_id,
            price=order.price,
            size=order.size,
            queue_ahead_size=order.queue_ahead_size,
            reason=reason,
        )

    def _sync_side_order(
        self,
        *,
        side: str,
        target_price: Optional[float],
        target_size: float,
        queue_ahead_size: float,
        exchange_time_ms: int,
        reason: str,
    ) -> None:
        existing = self.bid_order if side == "BUY" else self.ask_order
        if target_price is None or target_size <= 0:
            self._cancel_order(side, reason)
            return
        if existing is None:
            self._place_order(
                side=side,
                price=target_price,
                size=target_size,
                queue_ahead_size=queue_ahead_size,
                reason=reason,
                exchange_time_ms=exchange_time_ms,
            )
            return
        price_delta_bps = abs(_bps_from_prices(target_price, existing.price))
        size_delta_pct = abs(target_size - existing.remaining_size) / max(existing.remaining_size, 1e-9)
        order_age_ms = max(0, int((time.time() - existing.placed_at_ts) * 1000.0))
        if order_age_ms < self.config.min_requote_interval_ms:
            return
        if price_delta_bps >= self.config.requote_price_bps or size_delta_pct >= self.config.requote_size_pct:
            self._write_event(
                "quote_replaced",
                side=side,
                old_order_id=existing.order_id,
                old_price=existing.price,
                new_price=target_price,
                old_remaining_size=existing.remaining_size,
                new_size=target_size,
                reason=reason,
            )
            self._cancel_order(side, "replace")
            self._place_order(
                side=side,
                price=target_price,
                size=target_size,
                queue_ahead_size=queue_ahead_size,
                reason=reason,
                exchange_time_ms=exchange_time_ms,
            )

    def _apply_fill(
        self,
        *,
        side: str,
        price: float,
        size: float,
        liquidity_role: str,
        reason: str,
        order_id: Optional[int],
        snapshot: MMSnapshot,
    ) -> None:
        if size <= 0:
            return
        self.fill_seq += 1
        fill_id = self.fill_seq
        fill_qty_signed = size if side == "BUY" else -size
        episode_id_before = self.current_episode.episode_id if self.current_episode is not None else None
        realized_pnl_delta = 0.0
        fill_notional = price * size
        fee_state = self._fee_state()
        exchange_fee_delta = estimate_exchange_fee(
            notional=fill_notional,
            liquidity_role=liquidity_role,
            fee_state=fee_state,
        ) + (fill_notional * self.fee_pct)

        self.perp_fill_turnover += fill_notional
        if liquidity_role == "aggressive":
            self.taker_fill_turnover += fill_notional
            if exchange_fee_delta >= 0:
                self.estimated_taker_fees += exchange_fee_delta
        else:
            self.maker_fill_turnover += fill_notional
            if exchange_fee_delta >= 0:
                self.estimated_maker_fees += exchange_fee_delta
            else:
                self.estimated_maker_rebates += -exchange_fee_delta
        self.estimated_exchange_fees += exchange_fee_delta

        def apply_fee_bucket(episode: InventoryEpisode, notional: float, fee_delta: float) -> None:
            episode.fees_paid += fee_delta
            if liquidity_role == "aggressive":
                episode.taker_notional += notional
                if fee_delta >= 0:
                    episode.taker_fee_cost += fee_delta
            else:
                episode.maker_notional += notional
                if fee_delta >= 0:
                    episode.maker_fee_cost += fee_delta
                else:
                    episode.maker_rebates += -fee_delta

        if self.current_episode is None:
            self.episode_seq += 1
            direction = "LONG" if fill_qty_signed > 0 else "SHORT"
            episode = InventoryEpisode(
                episode_id=self.episode_seq,
                direction=direction,
                qty_signed=fill_qty_signed,
                avg_price=price,
                opened_at_ts=time.time(),
                opened_exchange_time_ms=snapshot.sample_exchange_time_ms,
                max_abs_qty=abs(fill_qty_signed),
            )
            if liquidity_role == "passive":
                episode.passive_entry_fills += 1
            else:
                episode.aggressive_exit_fills += 1
            episode.fill_count += 1
            apply_fee_bucket(episode, fill_notional, exchange_fee_delta)
            episode.realized_pnl -= exchange_fee_delta
            self.current_episode = episode
            self._write_event(
                "inventory_opened",
                episode_id=episode.episode_id,
                side=episode.direction,
                fill_id=fill_id,
                price=price,
                size=size,
                liquidity_role=liquidity_role,
                reason=reason,
            )
        else:
            episode = self.current_episode
            existing_qty = episode.qty_signed
            same_direction = (existing_qty > 0 and fill_qty_signed > 0) or (
                existing_qty < 0 and fill_qty_signed < 0
            )
            if same_direction:
                total_qty = abs(existing_qty) + abs(fill_qty_signed)
                episode.avg_price = (
                    (episode.avg_price * abs(existing_qty)) + (price * abs(fill_qty_signed))
                ) / max(total_qty, 1e-9)
                episode.qty_signed += fill_qty_signed
                episode.max_abs_qty = max(episode.max_abs_qty, abs(episode.qty_signed))
                if liquidity_role == "passive":
                    episode.passive_entry_fills += 1
                else:
                    episode.aggressive_exit_fills += 1
                episode.fill_count += 1
                apply_fee_bucket(episode, fill_notional, exchange_fee_delta)
                episode.realized_pnl -= exchange_fee_delta
            else:
                close_qty = min(abs(existing_qty), abs(fill_qty_signed))
                direction = 1.0 if existing_qty > 0 else -1.0
                gross_realized = (price - episode.avg_price) * close_qty * direction
                close_notional = price * close_qty
                close_fee_delta = estimate_exchange_fee(
                    notional=close_notional,
                    liquidity_role=liquidity_role,
                    fee_state=fee_state,
                ) + (close_notional * self.fee_pct)
                realized_pnl_delta = gross_realized - close_fee_delta
                episode.gross_pnl += gross_realized
                episode.realized_pnl += realized_pnl_delta
                apply_fee_bucket(episode, close_notional, close_fee_delta)
                episode.fill_count += 1
                if liquidity_role == "passive":
                    episode.passive_exit_fills += 1
                else:
                    episode.aggressive_exit_fills += 1

                remainder = abs(fill_qty_signed) - close_qty
                if abs(existing_qty) > close_qty:
                    episode.qty_signed = existing_qty + fill_qty_signed
                else:
                    episode.qty_signed = 0.0

                if episode.qty_signed == 0.0:
                    self._close_episode(episode, reason=reason, exit_price_avg=price)
                    self.current_episode = None
                elif remainder > 0:
                    old_episode = episode
                    self._close_episode(old_episode, reason=reason, exit_price_avg=price)
                    self.current_episode = None
                    new_qty_signed = math.copysign(remainder, fill_qty_signed)
                    self.episode_seq += 1
                    direction_name = "LONG" if new_qty_signed > 0 else "SHORT"
                    new_episode = InventoryEpisode(
                        episode_id=self.episode_seq,
                        direction=direction_name,
                        qty_signed=new_qty_signed,
                        avg_price=price,
                        opened_at_ts=time.time(),
                        opened_exchange_time_ms=snapshot.sample_exchange_time_ms,
                        max_abs_qty=abs(new_qty_signed),
                    )
                    if liquidity_role == "passive":
                        new_episode.passive_entry_fills += 1
                    else:
                        new_episode.aggressive_exit_fills += 1
                    new_episode.fill_count += 1
                    remainder_notional = price * remainder
                    remainder_fee_delta = estimate_exchange_fee(
                        notional=remainder_notional,
                        liquidity_role=liquidity_role,
                        fee_state=fee_state,
                    ) + (remainder_notional * self.fee_pct)
                    apply_fee_bucket(new_episode, remainder_notional, remainder_fee_delta)
                    new_episode.realized_pnl -= remainder_fee_delta
                    self.current_episode = new_episode

        if self.current_episode is not None:
            self._update_markout(snapshot)

        snapshot_after = self.last_snapshot if self.last_snapshot is not None else snapshot
        self._write_fill_row(
            fill_id=fill_id,
            episode_id=episode_id_before if episode_id_before is not None else (
                self.current_episode.episode_id if self.current_episode is not None else None
            ),
            liquidity_role=liquidity_role,
            side=side,
            price=price,
            size=size,
            order_id=order_id,
            reason=reason,
            snapshot=snapshot_after,
            realized_pnl_delta=realized_pnl_delta,
            exchange_fee_delta=exchange_fee_delta,
            fee_state=fee_state,
        )
        self._write_event(
            "fill_recorded",
            fill_id=fill_id,
            episode_id=episode_id_before if episode_id_before is not None else (
                self.current_episode.episode_id if self.current_episode is not None else None
            ),
            liquidity_role=liquidity_role,
            side=side,
            price=price,
            size=size,
            notional=price * size,
            order_id=order_id,
            reason=reason,
            realized_pnl_delta=realized_pnl_delta,
            exchange_fee_delta=exchange_fee_delta,
            fee_tier_label=fee_state.tier_label,
            fee_actual_tier_label=f"Tier{fee_state.actual_tier_index}",
            fee_projected_tier_label=f"Tier{fee_state.projected_tier_index}",
            maker_rate_bps=fee_state.maker_rate_bps,
            taker_rate_bps=fee_state.taker_rate_bps,
            maker_rebate_bps=fee_state.maker_rebate_bps,
        )

    def _close_episode(
        self,
        episode: InventoryEpisode,
        *,
        reason: str,
        exit_price_avg: float,
    ) -> None:
        episode.close_reason = reason
        episode.closed_at_ts = time.time()
        episode.closed_exchange_time_ms = self.last_fill_exchange_time_ms or int(time.time() * 1000)
        hold_seconds = max(0.0, episode.closed_at_ts - episode.opened_at_ts)
        self.closed_episodes.append(episode)
        self.closed_episode_pnls.append(episode.realized_pnl)
        self.closed_episode_hold_seconds.append(hold_seconds)
        self.closed_episode_markouts.append(episode.best_markout_bps)
        self.daily_realized_pnl += episode.realized_pnl
        self.daily_episode_count += 1
        self.hourly_episode_closed_at.append(episode.closed_at_ts)
        self.last_episode_closed_ts = episode.closed_at_ts
        self.exit_reason_counts[reason] = self.exit_reason_counts.get(reason, 0) + 1
        entry_notional = abs(episode.avg_price * episode.max_abs_qty)
        if entry_notional > 0:
            self.realized_spread_bps.append((episode.realized_pnl / entry_notional) * 10_000.0)
        self._write_episode_row(
            episode=episode,
            exit_price_avg=exit_price_avg,
            hold_seconds=hold_seconds,
        )
        self._write_event(
            "inventory_closed",
            episode_id=episode.episode_id,
            side=episode.direction,
            realized_pnl=episode.realized_pnl,
            gross_pnl=episode.gross_pnl,
            fees_paid=episode.fees_paid,
            maker_fee_cost=episode.maker_fee_cost,
            taker_fee_cost=episode.taker_fee_cost,
            maker_rebates=episode.maker_rebates,
            maker_notional=episode.maker_notional,
            taker_notional=episode.taker_notional,
            hold_seconds=hold_seconds,
            close_reason=reason,
            best_markout_bps=episode.best_markout_bps,
            worst_markout_bps=episode.worst_markout_bps,
            passive_entry_fills=episode.passive_entry_fills,
            passive_exit_fills=episode.passive_exit_fills,
            aggressive_exit_fills=episode.aggressive_exit_fills,
            fill_count=episode.fill_count,
        )

    def _update_markout(self, snapshot: MMSnapshot) -> None:
        if self.current_episode is None or self.current_episode.qty_signed == 0:
            return
        exit_price = snapshot.bid if self.current_episode.qty_signed > 0 else snapshot.ask
        markout_bps = _bps_from_prices(exit_price, self.current_episode.avg_price)
        if self.current_episode.qty_signed < 0:
            markout_bps *= -1.0
        self.current_episode.best_markout_bps = max(self.current_episode.best_markout_bps, markout_bps)
        self.current_episode.worst_markout_bps = min(self.current_episode.worst_markout_bps, markout_bps)

    def _fill_order_from_trade(
        self,
        *,
        order: PassiveOrder,
        trade: TradePrint,
        snapshot: MMSnapshot,
    ) -> None:
        if order.remaining_size <= 0:
            return
        qualifies = False
        if order.side == "BUY" and trade.aggressive_sell:
            qualifies = trade.price <= order.price + max(snapshot.tick_size / 2.0, 1e-9)
        elif order.side == "SELL" and trade.aggressive_buy:
            qualifies = trade.price >= order.price - max(snapshot.tick_size / 2.0, 1e-9)
        if not qualifies:
            return
        trade_remaining = trade.size
        if order.queue_ahead_size > 0:
            consumed = min(order.queue_ahead_size, trade_remaining)
            order.queue_ahead_size -= consumed
            trade_remaining -= consumed
        if trade_remaining <= 0:
            return
        fill_size = min(order.remaining_size, trade_remaining)
        order.remaining_size -= fill_size
        self.last_fill_exchange_time_ms = trade.exchange_time_ms
        self._apply_fill(
            side=order.side,
            price=order.price,
            size=fill_size,
            liquidity_role="passive",
            reason="passive_trade_fill",
            order_id=order.order_id,
            snapshot=snapshot,
        )
        if order.remaining_size <= 1e-12:
            self._write_event(
                "quote_filled",
                side=order.side,
                order_id=order.order_id,
                price=order.price,
                reason="trade_consumed",
            )
            if order.side == "BUY":
                self.bid_order = None
            else:
                self.ask_order = None

    def _quote_through_fill_check(self, snapshot: MMSnapshot) -> None:
        if self.bid_order is not None and snapshot.bid < (self.bid_order.price - snapshot.tick_size / 2.0):
            remaining = self.bid_order.remaining_size
            order_id = self.bid_order.order_id
            price = self.bid_order.price
            self.bid_order = None
            self._apply_fill(
                side="BUY",
                price=price,
                size=remaining,
                liquidity_role="passive",
                reason="quote_traded_through",
                order_id=order_id,
                snapshot=snapshot,
            )
        if self.ask_order is not None and snapshot.ask > (self.ask_order.price + snapshot.tick_size / 2.0):
            remaining = self.ask_order.remaining_size
            order_id = self.ask_order.order_id
            price = self.ask_order.price
            self.ask_order = None
            self._apply_fill(
                side="SELL",
                price=price,
                size=remaining,
                liquidity_role="passive",
                reason="quote_traded_through",
                order_id=order_id,
                snapshot=snapshot,
            )

    def _process_new_trades(self, market: MarketSnapshot, snapshot: MMSnapshot) -> None:
        new_trades = [trade for trade in market.trades if self._process_trade_hash(trade)]
        if not new_trades:
            return
        for trade in sorted(new_trades, key=lambda item: item.exchange_time_ms):
            if self.bid_order is not None:
                self._fill_order_from_trade(order=self.bid_order, trade=trade, snapshot=snapshot)
            if self.ask_order is not None:
                self._fill_order_from_trade(order=self.ask_order, trade=trade, snapshot=snapshot)

    def _flatten_inventory(self, snapshot: MMSnapshot, reason: str) -> None:
        if self.current_episode is None or self.current_episode.qty_signed == 0:
            return
        qty = abs(self.current_episode.qty_signed)
        side = "SELL" if self.current_episode.qty_signed > 0 else "BUY"
        price = snapshot.bid if side == "SELL" else snapshot.ask
        if self.slippage_pct > 0:
            if side == "SELL":
                price *= 1.0 - self.slippage_pct
            else:
                price *= 1.0 + self.slippage_pct
        self._cancel_order("BUY", f"{reason}_flatten")
        self._cancel_order("SELL", f"{reason}_flatten")
        self.last_fill_exchange_time_ms = snapshot.sample_exchange_time_ms
        self._apply_fill(
            side=side,
            price=price,
            size=qty,
            liquidity_role="aggressive",
            reason=reason,
            order_id=None,
            snapshot=snapshot,
        )

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
        if gap_ms < self.config.reconnect_gap_ms:
            return None
        if (now_ms - self._last_reconnect_attempt_ms) < self.config.reconnect_gap_ms:
            return None
        self._last_reconnect_attempt_ms = now_ms
        return self._reconnect_feed(gap_ms)

    def _reconnect_feed(self, gap_ms: int) -> Optional[Tuple[MarketSnapshot, int]]:
        self._write_event("feed_reconnect_started", quote_gap_ms=gap_ms)
        old_feed = self.feed
        if old_feed is not None:
            try:
                old_feed.close()
            except Exception:
                logging.exception("Failed closing feed during reconnect")
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
        self.tick_count += 1
        self.last_quote_exchange_time_ms = quote.exchange_time_ms
        self.last_quote_transport_delay_ms = quote.transport_delay_ms
        quote_age_ms = int(_clamp_non_negative((time.time() * 1000) - quote.exchange_time_ms))
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

        if quote_age_ms > self.config.max_quote_age_ms:
            self.stale_quote_skips += 1
            self._cancel_order("BUY", "stale_quote")
            self._cancel_order("SELL", "stale_quote")
            now_ms = int(time.time() * 1000)
            if now_ms - self._last_stale_skip_event_ms >= self.config.max_quote_age_ms:
                self._last_stale_skip_event_ms = now_ms
                self._write_event(
                    "stale_quote_skipped",
                    quote_age_ms=quote_age_ms,
                    bid=quote.bid,
                    ask=quote.ask,
                    mid=quote.mid,
                    max_quote_age_ms=self.config.max_quote_age_ms,
                )
            return

        self._append_price_sample(quote.exchange_time_ms, quote.microprice)
        features = self._compute_market_features(market)
        fee_state = self._fee_state()
        expected_taker_share = self._expected_taker_share()
        protection_reason = inventory_protection_reason(
            features=features,
            inventory_qty=self._inventory_qty(),
            inventory_avg_price=self._inventory_avg_price(),
            inventory_opened_at_ts=self._inventory_opened_at_ts(),
            config=self.config,
        )
        kill_reason = aggressive_flatten_reason(
            features=features,
            inventory_qty=self._inventory_qty(),
            inventory_avg_price=self._inventory_avg_price(),
            inventory_opened_at_ts=self._inventory_opened_at_ts(),
            config=self.config,
            fee_state=fee_state,
            protection_reason=protection_reason,
        )
        initial_plan = build_quote_plan(
            features=features,
            inventory_qty=self._inventory_qty(),
            inventory_avg_price=self._inventory_avg_price(),
            book=market.book,
            config=self.config,
            fee_state=fee_state,
            expected_taker_share=expected_taker_share,
            protection_reason=protection_reason,
        )
        snapshot = self._build_snapshot(features, initial_plan, fee_state)
        self.last_snapshot = snapshot
        self._process_new_trades(market, snapshot)
        self._quote_through_fill_check(snapshot)

        protection_reason = inventory_protection_reason(
            features=features,
            inventory_qty=self._inventory_qty(),
            inventory_avg_price=self._inventory_avg_price(),
            inventory_opened_at_ts=self._inventory_opened_at_ts(),
            config=self.config,
        )
        kill_reason = aggressive_flatten_reason(
            features=features,
            inventory_qty=self._inventory_qty(),
            inventory_avg_price=self._inventory_avg_price(),
            inventory_opened_at_ts=self._inventory_opened_at_ts(),
            config=self.config,
            fee_state=fee_state,
            protection_reason=protection_reason,
        )
        plan = build_quote_plan(
            features=features,
            inventory_qty=self._inventory_qty(),
            inventory_avg_price=self._inventory_avg_price(),
            book=market.book,
            config=self.config,
            fee_state=fee_state,
            expected_taker_share=expected_taker_share,
            protection_reason=protection_reason,
        )
        snapshot = self._build_snapshot(features, plan, fee_state)
        self.last_snapshot = snapshot
        self._update_markout(snapshot)

        if kill_reason is not None:
            self._flatten_inventory(snapshot, kill_reason)
            fee_state = self._fee_state()
            expected_taker_share = self._expected_taker_share()
            plan = build_quote_plan(
                features=features,
                inventory_qty=self._inventory_qty(),
                inventory_avg_price=self._inventory_avg_price(),
                book=market.book,
                config=self.config,
                fee_state=fee_state,
                expected_taker_share=expected_taker_share,
            )
        now_ts = time.time()
        block_reason = self._quoting_block_reason(now_ts)
        effective_plan = plan
        if block_reason is not None and self._inventory_qty() == 0.0:
            effective_plan = disable_quote_plan(
                plan,
                reason=block_reason,
                decision_note=(
                    f"flat: {block_reason} daily_pnl={self.daily_realized_pnl:.4f} "
                    f"daily_episodes={self.daily_episode_count} hourly_episodes={len(self.hourly_episode_closed_at)}"
                ),
            )

        if effective_plan.quoting_enabled:
            self._sync_side_order(
                side="BUY",
                target_price=effective_plan.bid_price,
                target_size=effective_plan.bid_size,
                queue_ahead_size=effective_plan.bid_queue_ahead_size,
                exchange_time_ms=features.sample_exchange_time_ms,
                reason=effective_plan.quoting_reason,
            )
            self._sync_side_order(
                side="SELL",
                target_price=effective_plan.ask_price,
                target_size=effective_plan.ask_size,
                queue_ahead_size=effective_plan.ask_queue_ahead_size,
                exchange_time_ms=features.sample_exchange_time_ms,
                reason=effective_plan.quoting_reason,
            )
        else:
            self._cancel_order("BUY", effective_plan.quoting_reason)
            self._cancel_order("SELL", effective_plan.quoting_reason)

        if (
            self.last_sample_exchange_time_ms is None
            or (features.sample_exchange_time_ms - self.last_sample_exchange_time_ms) >= self.config.sample_ms
        ):
            self.last_sample_exchange_time_ms = features.sample_exchange_time_ms
            self.sample_count += 1
            snapshot = self._build_snapshot(features, effective_plan, fee_state)
            self.last_snapshot = snapshot
            self._write_sample(snapshot)
            self._write_event(
                "state_snapshot",
                **snapshot.to_dict(),
            )

    def run(self) -> None:
        logging.info(
            "Starting Asterion liquidity engine asset=%s sample_ms=%d vol_window_s=%.1f impulse_window_s=%.1f flow_window_s=%.1f base_notional=%.2f max_inventory_notional=%.2f max_spread_bps=%.2f",
            self.asset,
            self.config.sample_ms,
            self.config.volatility_lookback_seconds,
            self.config.impulse_window_seconds,
            self.config.flow_window_seconds,
            self.config.base_order_notional,
            self.config.max_inventory_notional,
            self.config.max_spread_bps,
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
        self._configure_fee_model()
        startup_fee_state = self._fee_state()
        self._write_event(
            "bot_started",
            asset=self.asset,
            api_url=self.api_url,
            dex=self.dex,
            account_balance=self.config.account_balance,
            leverage=self.config.leverage,
            startup_bid=first_snapshot.quote.bid,
            startup_ask=first_snapshot.quote.ask,
            startup_mid=first_snapshot.quote.mid,
            startup_microprice=first_snapshot.quote.microprice,
            startup_transport_delay_ms=first_snapshot.quote.transport_delay_ms,
            warm_start_points=warm_points,
            sample_ms=self.config.sample_ms,
            volatility_lookback_seconds=self.config.volatility_lookback_seconds,
            impulse_window_seconds=self.config.impulse_window_seconds,
            flow_window_seconds=self.config.flow_window_seconds,
            depth_levels=self.config.depth_levels,
            min_trade_count=self.config.min_trade_count,
            max_spread_bps=self.config.max_spread_bps,
            max_quote_age_ms=self.config.max_quote_age_ms,
            quote_gap_warn_ms=self.config.quote_gap_warn_ms,
            reconnect_gap_ms=self.config.reconnect_gap_ms,
            min_half_spread_bps=self.config.min_half_spread_bps,
            target_edge_bps=self.config.target_edge_bps,
            vol_spread_multiplier=self.config.vol_spread_multiplier,
            toxicity_spread_multiplier=self.config.toxicity_spread_multiplier,
            latency_spread_multiplier=self.config.latency_spread_multiplier,
            inventory_skew_bps=self.config.inventory_skew_bps,
            base_order_notional=self.config.base_order_notional,
            max_quote_notional=self.config.max_quote_notional,
            max_inventory_notional=self.config.max_inventory_notional,
            requote_price_bps=self.config.requote_price_bps,
            requote_size_pct=self.config.requote_size_pct,
            min_requote_interval_ms=self.config.min_requote_interval_ms,
            min_quote_size_multiplier=self.config.min_quote_size_multiplier,
            max_quote_size_multiplier=self.config.max_quote_size_multiplier,
            max_top_level_share=self.config.max_top_level_share,
            max_depth_share=self.config.max_depth_share,
            queue_ahead_penalty=self.config.queue_ahead_penalty,
            quote_guard_flow_imbalance=self.config.quote_guard_flow_imbalance,
            quote_guard_book_imbalance=self.config.quote_guard_book_imbalance,
            quote_guard_toxicity=self.config.quote_guard_toxicity,
            one_way_flow_imbalance=self.config.one_way_flow_imbalance,
            one_way_book_imbalance=self.config.one_way_book_imbalance,
            one_way_alpha_bps=self.config.one_way_alpha_bps,
            protection_markout_bps=self.config.protection_markout_bps,
            protection_flow_imbalance=self.config.protection_flow_imbalance,
            protection_book_imbalance=self.config.protection_book_imbalance,
            event_impulse_bps=self.config.event_impulse_bps,
            event_vol_bps=self.config.event_vol_bps,
            toxic_flow_imbalance=self.config.toxic_flow_imbalance,
            toxic_book_imbalance=self.config.toxic_book_imbalance,
            adverse_exit_bps=self.config.adverse_exit_bps,
            adverse_flow_exit_imbalance=self.config.adverse_flow_exit_imbalance,
            adverse_book_exit_imbalance=self.config.adverse_book_exit_imbalance,
            max_inventory_hold_seconds=self.config.max_inventory_hold_seconds,
            kill_hold_seconds=self.config.kill_hold_seconds,
            kill_on_event_loss_bps=self.config.kill_on_event_loss_bps,
            cooldown_seconds=self.config.cooldown_seconds,
            stop_quoting_on_event=self.config.stop_quoting_on_event,
            fee_product=self.config.fee_product,
            fee_market_type=startup_fee_state.market_type,
            fee_staking_tier=self.config.fee_staking_tier,
            fee_tier_basis=self.config.fee_tier_basis,
            fee_target_tier=self.config.fee_target_tier,
            fee_initial_14d_perps_volume=self.config.fee_initial_14d_perps_volume,
            fee_initial_14d_spot_volume=self.config.fee_initial_14d_spot_volume,
            fee_taker_referral_discount_pct=self.config.fee_taker_referral_discount_pct,
            fee_maker_rebate_bps_override=self.config.fee_maker_rebate_bps_override,
            fee_user_maker_rate_pct_override=self.fee_config.user_maker_rate_pct_override,
            fee_user_taker_rate_pct_override=self.fee_config.user_taker_rate_pct_override,
            fee_buffer_bps=self.config.fee_buffer_bps,
            fee_kill_buffer_bps=self.config.fee_kill_buffer_bps,
            expected_taker_share_floor=self.config.expected_taker_share_floor,
            tier_volume_boost_multiplier=self.config.tier_volume_boost_multiplier,
            tier_volume_relaxation_multiplier=self.config.tier_volume_relaxation_multiplier,
            startup_fee_tier_label=startup_fee_state.tier_label,
            startup_fee_rate_source=startup_fee_state.fee_rate_source,
            startup_fee_deployer_fee_scale=startup_fee_state.deployer_fee_scale,
            startup_fee_growth_mode=startup_fee_state.growth_mode,
            startup_fee_aligned_quote_token=startup_fee_state.aligned_quote_token,
            startup_fee_market_type=startup_fee_state.market_type,
            startup_fee_actual_tier_label=f"Tier{startup_fee_state.actual_tier_index}",
            startup_fee_projected_tier_label=f"Tier{startup_fee_state.projected_tier_index}",
            startup_maker_rate_bps=startup_fee_state.maker_rate_bps,
            startup_taker_rate_bps=startup_fee_state.taker_rate_bps,
            startup_maker_rebate_bps=startup_fee_state.maker_rebate_bps,
            startup_projected_14d_weighted_volume=startup_fee_state.projected_weighted_14d_volume,
            slippage_bps=self.config.slippage_bps,
            fee_bps=self.config.fee_bps,
        )

        last_seq = 0
        while True:
            try:
                assert self.feed is not None
                item = self.feed.wait_for_next(
                    last_seq=last_seq,
                    timeout_seconds=self.config.quote_gap_warn_ms / 1000.0,
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
                logging.exception("Realtime market-maker loop error")
                self._write_event("polling_error", error=self.last_error)

    def close(self) -> None:
        if self.feed is not None:
            self.feed.close()

    def finalize(self, reason: str) -> Optional[Path]:
        if self._finalized:
            return self._report_path
        self._finalized = True
        self.close()
        self._cancel_order("BUY", "finalize")
        self._cancel_order("SELL", "finalize")

        if self.current_episode is not None and self.last_snapshot is not None:
            self._flatten_inventory(self.last_snapshot, "finalize_flatten")

        end_ts = time.time()
        duration_seconds = max(0.0, end_ts - self.started_at_ts)
        closed_episodes = len(self.closed_episode_pnls)
        total_realized_pnl = sum(self.closed_episode_pnls)
        wins = sum(1 for pnl in self.closed_episode_pnls if pnl > 0)
        win_rate_pct = (wins / closed_episodes * 100.0) if closed_episodes else 0.0
        avg_hold_seconds = (
            sum(self.closed_episode_hold_seconds) / closed_episodes if closed_episodes else 0.0
        )
        avg_markout = (
            sum(self.closed_episode_markouts) / len(self.closed_episode_markouts)
            if self.closed_episode_markouts
            else 0.0
        )
        quote_age_p95 = self._pctl(self.quote_age_history_ms, 0.95)
        transport_p95 = self._pctl(self.transport_delay_history_ms, 0.95)
        spread_p95 = self._pctl(self.spread_history_bps, 0.95)
        realized_spread_avg = (
            sum(self.realized_spread_bps) / len(self.realized_spread_bps)
            if self.realized_spread_bps
            else 0.0
        )

        fee_state = self._fee_state()
        gross_realized_pnl = sum(episode.gross_pnl for episode in self.closed_episodes)
        net_fee_drag = total_realized_pnl - gross_realized_pnl

        report_name = (
            f"run_{datetime.fromtimestamp(self.started_at_ts, tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}_{self.asset.replace(':', '_')}.md"
        )
        report_path = self.report_dir / report_name
        lines = [
            "# Asterion Liquidity Engine Run Report",
            "",
            f"- Asset: `{self.asset}`",
            f"- Stop reason: `{reason}`",
            f"- Start (UTC): `{datetime.fromtimestamp(self.started_at_ts, tz=timezone.utc).isoformat()}`",
            f"- End (UTC): `{datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat()}`",
            f"- Duration (s): `{duration_seconds:.1f}`",
            "",
            "## Configuration",
            f"- Sample ms: `{self.config.sample_ms}`",
            f"- Volatility lookback seconds: `{self.config.volatility_lookback_seconds}`",
            f"- Impulse window seconds: `{self.config.impulse_window_seconds}`",
            f"- Flow window seconds: `{self.config.flow_window_seconds}`",
            f"- Base order notional: `{self.config.base_order_notional}`",
            f"- Max quote notional: `{self.config.max_quote_notional}`",
            f"- Max inventory notional: `{self.config.max_inventory_notional}`",
            f"- Min half spread bps: `{self.config.min_half_spread_bps}`",
            f"- Target edge bps: `{self.config.target_edge_bps}`",
            f"- Max spread bps: `{self.config.max_spread_bps}`",
            f"- Quote size multipliers min/max: `{self.config.min_quote_size_multiplier}` / `{self.config.max_quote_size_multiplier}`",
            f"- Max top-level share / depth share: `{self.config.max_top_level_share}` / `{self.config.max_depth_share}`",
            f"- Queue-ahead penalty: `{self.config.queue_ahead_penalty}`",
            f"- Quote guard flow/book/toxicity: `{self.config.quote_guard_flow_imbalance}` / `{self.config.quote_guard_book_imbalance}` / `{self.config.quote_guard_toxicity}`",
            f"- One-way flow/book/alpha: `{self.config.one_way_flow_imbalance}` / `{self.config.one_way_book_imbalance}` / `{self.config.one_way_alpha_bps}`",
            f"- Protection markout bps: `{self.config.protection_markout_bps}`",
            f"- Protection flow/book imbalance: `{self.config.protection_flow_imbalance}` / `{self.config.protection_book_imbalance}`",
            f"- Kill exit bps: `{self.config.adverse_exit_bps}`",
            f"- Kill flow/book imbalance: `{self.config.adverse_flow_exit_imbalance}` / `{self.config.adverse_book_exit_imbalance}`",
            f"- Passive protection hold seconds: `{self.config.max_inventory_hold_seconds}`",
            f"- Kill hold seconds: `{self.config.kill_hold_seconds}`",
            f"- Kill on event loss bps: `{self.config.kill_on_event_loss_bps}`",
            f"- Fee market type: `{self.config.fee_market_type}`",
            f"- Fee staking tier: `{self.config.fee_staking_tier}`",
            f"- Fee tier basis: `{self.config.fee_tier_basis}`",
            f"- Fee target tier: `Tier{self.config.fee_target_tier}`",
            f"- Fee buffer / kill buffer bps: `{self.config.fee_buffer_bps}` / `{self.config.fee_kill_buffer_bps}`",
            f"- Tier volume boost / relaxation multipliers: `{self.config.tier_volume_boost_multiplier}` / `{self.config.tier_volume_relaxation_multiplier}`",
            "",
            "## Feed Health",
            f"- Startup preflight: `{'ok' if self.preflight_ok else 'failed'}`",
            f"- Ticks captured: `{self.tick_count}`",
            f"- Samples written: `{self.sample_count}`",
            f"- Loop errors: `{self.poll_error_count}`",
            f"- Stale quote skips: `{self.stale_quote_skips}`",
            f"- Quote age p95 ms: `{quote_age_p95 if quote_age_p95 is not None else 'n/a'}`",
            f"- Transport delay p95 ms: `{transport_p95 if transport_p95 is not None else 'n/a'}`",
            f"- Spread p95 bps: `{spread_p95 if spread_p95 is not None else 'n/a'}`",
            "",
            "## Trading Activity",
            f"- Closed episodes: `{closed_episodes}`",
            f"- Total realized PnL: `{total_realized_pnl:.6f}`",
            f"- Gross realized PnL: `{gross_realized_pnl:.6f}`",
            f"- Net fee drag: `{net_fee_drag:.6f}`",
            f"- Win rate: `{win_rate_pct:.2f}%`",
            f"- Avg hold seconds: `{avg_hold_seconds:.3f}`",
            f"- Avg best markout bps: `{avg_markout:.4f}`",
            f"- Avg realized spread bps: `{realized_spread_avg:.4f}`",
            f"- Fee tier label: `{fee_state.tier_label}`",
            f"- Actual / projected tier: `Tier{fee_state.actual_tier_index}` / `Tier{fee_state.projected_tier_index}`",
            f"- Net maker / taker rate bps: `{fee_state.net_maker_rate_bps:.4f}` / `{fee_state.taker_rate_bps:.4f}`",
            f"- Maker rebate bps: `{fee_state.maker_rebate_bps:.4f}`",
            f"- Projected 14d weighted volume: `{fee_state.projected_weighted_14d_volume:.2f}`",
            f"- Daily weighted volume run-rate: `{fee_state.daily_weighted_volume_run_rate:.2f}`",
            f"- Estimated exchange fees: `{self.estimated_exchange_fees:.6f}`",
            f"- Estimated maker fees: `{self.estimated_maker_fees:.6f}`",
            f"- Estimated taker fees: `{self.estimated_taker_fees:.6f}`",
            f"- Estimated maker rebates: `{self.estimated_maker_rebates:.6f}`",
            f"- Maker / taker turnover: `{self.maker_fill_turnover:.2f}` / `{self.taker_fill_turnover:.2f}`",
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
                f"- Fills CSV: `{self.fills_csv_path}`",
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
                samples=self.sample_count,
                closed_episodes=closed_episodes,
                realized_pnl=total_realized_pnl,
                report_path=str(report_path),
            )
            return report_path
        except Exception:
            logging.exception("Failed writing market-maker report")
            return None

    @staticmethod
    def _pctl(values: Sequence[float], percentile: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        idx = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
        return round(ordered[idx], 3)

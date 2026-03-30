from __future__ import annotations

import logging
import math
import time
from typing import Optional

from atlas_mm_feeaware_core import (
    MMStrategyConfig,
    MarketFeatures,
    ProSpreadMarketMaker,
    QuotePlan,
    aggressive_flatten_reason,
    disable_quote_plan,
    inventory_protection_reason,
    top_level_size,
    visible_size_at_price,
    _clamp,
    _round_down,
    _round_up,
)
from hyperliquid_fee_model import FeeState
from pa_pump_pro_core import BookSnapshot, HyperliquidRealtimeMultiFeed, MarketSnapshot

APP_NAME = "Kevin Hype Liquidity Engine"


def zero_fee_state(*, turnover: float = 0.0, started_at_ts: Optional[float] = None) -> FeeState:
    elapsed_seconds = max(1.0, time.time() - started_at_ts) if started_at_ts is not None else 1.0
    daily_run_rate = max(0.0, turnover) * (86_400.0 / elapsed_seconds)
    return FeeState(
        product="perps",
        market_type="standard",
        staking_tier="base",
        fee_rate_source="kevin_no_cost_demo",
        deployer_fee_scale=0.0,
        growth_mode=False,
        aligned_quote_token=False,
        current_weighted_14d_volume=max(0.0, turnover),
        projected_weighted_14d_volume=max(0.0, turnover),
        decision_weighted_14d_volume=max(0.0, turnover),
        actual_tier_index=0,
        projected_tier_index=0,
        decision_tier_index=0,
        tier_label="Tier0",
        maker_rate_pct=0.0,
        taker_rate_pct=0.0,
        maker_rate_bps=0.0,
        taker_rate_bps=0.0,
        maker_rebate_bps=0.0,
        net_maker_rate_bps=0.0,
        daily_perp_volume_run_rate=daily_run_rate,
        daily_weighted_volume_run_rate=daily_run_rate,
        next_tier_index=None,
        next_tier_label=None,
        next_tier_threshold=None,
        progress_to_next_tier_pct=None,
        days_to_next_tier=None,
        target_tier_index=0,
        target_tier_threshold=None,
        progress_to_target_tier_pct=None,
        markets_needed_for_target_tier=None,
    )


def _inventory_markout_bps(
    *,
    features: MarketFeatures,
    inventory_qty: float,
    inventory_avg_price: Optional[float],
) -> float:
    if inventory_qty == 0.0 or inventory_avg_price in (None, 0.0):
        return 0.0
    if inventory_qty > 0:
        return ((features.mid / max(inventory_avg_price, 1e-9)) - 1.0) * 10_000.0
    return ((inventory_avg_price / max(features.mid, 1e-9)) - 1.0) * 10_000.0


def _same_side_pressure_score(
    *,
    features: MarketFeatures,
    inventory_qty: float,
    config: MMStrategyConfig,
) -> float:
    if inventory_qty == 0.0:
        return 0.0
    aligned_flow = features.flow_imbalance if inventory_qty > 0 else -features.flow_imbalance
    aligned_book = features.book_imbalance if inventory_qty > 0 else -features.book_imbalance
    return (
        (aligned_flow / max(config.one_way_flow_imbalance, 1e-9)) * 0.62
        + (aligned_book / max(config.one_way_book_imbalance, 1e-9)) * 0.38
    )


def _adverse_inventory_flip_score(
    *,
    features: MarketFeatures,
    inventory_qty: float,
    config: MMStrategyConfig,
) -> float:
    if inventory_qty == 0.0:
        return 0.0
    adverse_flow = -features.flow_imbalance if inventory_qty > 0 else features.flow_imbalance
    adverse_impulse = -features.impulse_bps if inventory_qty > 0 else features.impulse_bps
    adverse_flow_gate = max(config.quote_guard_flow_imbalance, 0.80)
    impulse_gate = max(config.one_way_alpha_bps, 1.0)
    return (
        (max(0.0, adverse_flow) / max(adverse_flow_gate, 1e-9)) * 0.68
        + (features.toxicity_score / max(adverse_flow_gate, 1e-9)) * 0.24
        + (max(0.0, adverse_impulse) / max(impulse_gate, 1e-9)) * 0.08
    )


def _inventory_unwind_reason(
    *,
    features: MarketFeatures,
    inventory_qty: float,
    config: MMStrategyConfig,
) -> Optional[str]:
    if inventory_qty == 0.0:
        return None
    adverse_flow = -features.flow_imbalance if inventory_qty > 0 else features.flow_imbalance
    if (
        adverse_flow >= max(config.protection_flow_imbalance, 0.55)
        and _adverse_inventory_flip_score(
            features=features,
            inventory_qty=inventory_qty,
            config=config,
        ) >= 1.0
    ):
        return "adverse_flow_flip_protection"
    if _same_side_pressure_score(
        features=features,
        inventory_qty=inventory_qty,
        config=config,
    ) >= 1.0:
        return "trend_unwind_protection"
    return None


def _protection_exit_quotes(
    *,
    features: MarketFeatures,
    inventory_qty: float,
    inventory_avg_price: Optional[float],
    exit_size: float,
    book: Optional[BookSnapshot],
    config: MMStrategyConfig,
    protection_reason: str,
    inv_ratio: float,
) -> tuple[Optional[float], Optional[float], float, float, int, float]:
    exit_side = "SELL" if inventory_qty > 0 else "BUY"
    markout_bps = _inventory_markout_bps(
        features=features,
        inventory_qty=inventory_qty,
        inventory_avg_price=inventory_avg_price,
    )
    spread_ticks = max(
        1,
        int(round(max(features.ask - features.bid, features.tick_size) / max(features.tick_size, 1e-9))),
    )
    available_inside_ticks = max(0, spread_ticks - 1)
    improvement_ticks = 0

    touch_queue = 0.0
    if book is not None:
        if exit_side == "SELL":
            touch_queue = visible_size_at_price(book.asks, features.ask, features.tick_size)
        else:
            touch_queue = visible_size_at_price(book.bids, features.bid, features.tick_size)

    severe_reasons = {
        "adverse_flow_flip_protection",
        "markout_protection",
        "toxic_protection",
        "size_toxic_protection",
        "inventory_timeout_protection",
        "event_regime_protection",
    }
    if available_inside_ticks > 0:
        urgency = 0.0
        if protection_reason in severe_reasons:
            urgency += 1.0
        if protection_reason == "trend_unwind_protection":
            aligned_flow = features.flow_imbalance if inventory_qty > 0 else -features.flow_imbalance
            aligned_book = features.book_imbalance if inventory_qty > 0 else -features.book_imbalance
            urgency += max(0.0, aligned_flow - 0.35) * 0.85
            urgency += max(0.0, aligned_book - 0.10) * 0.55
        if protection_reason == "adverse_flow_flip_protection":
            adverse_flow = -features.flow_imbalance if inventory_qty > 0 else features.flow_imbalance
            adverse_impulse = -features.impulse_bps if inventory_qty > 0 else features.impulse_bps
            urgency += max(0.0, adverse_flow - 0.45) * 1.00
            urgency += max(0.0, adverse_impulse) * 0.12
        if markout_bps <= 0.0:
            urgency += min(1.5, abs(markout_bps) / max(config.protection_markout_bps, 1e-9))
        urgency += max(0.0, features.toxicity_score - 0.55)
        urgency += max(0.0, abs(inv_ratio) - 0.18) * 1.4
        if touch_queue > 0.0 and exit_size > 0.0:
            urgency += min(1.0, touch_queue / exit_size) * 0.6

        if urgency > 0.45:
            improvement_ticks = 1
        if urgency > 1.2:
            improvement_ticks += 1
        if urgency > 2.0:
            improvement_ticks += 1
        improvement_ticks = min(available_inside_ticks, improvement_ticks)

    bid_price = None
    ask_price = None
    bid_queue = 0.0
    ask_queue = 0.0
    if exit_side == "SELL":
        ask_price = _round_up(features.ask, features.tick_size)
        if improvement_ticks > 0:
            passive_floor = _round_up(features.bid + features.tick_size, features.tick_size)
            ask_price = _round_up(
                max(passive_floor, ask_price - (improvement_ticks * features.tick_size)),
                features.tick_size,
            )
        if book is not None:
            ask_queue = visible_size_at_price(book.asks, ask_price, features.tick_size)
    else:
        bid_price = _round_down(features.bid, features.tick_size)
        if improvement_ticks > 0:
            passive_ceiling = _round_down(features.ask - features.tick_size, features.tick_size)
            bid_price = _round_down(
                min(passive_ceiling, bid_price + (improvement_ticks * features.tick_size)),
                features.tick_size,
            )
        if book is not None:
            bid_queue = visible_size_at_price(book.bids, bid_price, features.tick_size)

    return bid_price, ask_price, bid_queue, ask_queue, improvement_ticks, markout_bps


def build_kevin_quote_plan(
    *,
    features: MarketFeatures,
    inventory_qty: float,
    inventory_avg_price: Optional[float],
    book: Optional[BookSnapshot],
    config: MMStrategyConfig,
    protection_reason: Optional[str] = None,
) -> QuotePlan:
    inv_notional = inventory_qty * features.mid
    inv_ratio = 0.0
    if config.max_inventory_notional > 0:
        inv_ratio = _clamp(inv_notional / config.max_inventory_notional, -1.0, 1.0)
    if protection_reason is None:
        protection_reason = _inventory_unwind_reason(
            features=features,
            inventory_qty=inventory_qty,
            config=config,
        )

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
        bid_price, ask_price, bid_queue, ask_queue, improvement_ticks, markout_bps = _protection_exit_quotes(
            features=features,
            inventory_qty=inventory_qty,
            inventory_avg_price=inventory_avg_price,
            exit_size=exit_size,
            book=book,
            config=config,
            protection_reason=protection_reason,
            inv_ratio=inv_ratio,
        )
        quote_mode = "ask_only" if exit_side == "SELL" else "bid_only"
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
            decision_note=(
                f"{quote_mode}: passive inventory protection because {protection_reason}; "
                f"flow={features.flow_imbalance:.3f} book={features.book_imbalance:.3f} "
                f"impulse={features.impulse_bps:.2f}bps markout={markout_bps:.2f}bps "
                f"shade={improvement_ticks}t"
            ),
            event_regime=features.event_regime,
            toxicity_score=features.toxicity_score,
        )

    if features.toxicity_score >= config.quote_guard_toxicity or (
        abs(features.flow_imbalance) >= config.quote_guard_flow_imbalance
        and abs(features.book_imbalance) >= config.quote_guard_book_imbalance
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

    current_half_spread_bps = max(features.tick_bps, features.spread_bps / 2.0)
    latency_ratio = _clamp(features.quote_age_ms / max(config.max_quote_age_ms, 1), 0.0, 1.0)

    alpha_bps = (
        (features.flow_imbalance * 0.72)
        + (features.book_imbalance * 0.52)
        + ((_clamp(features.impulse_bps / max(config.event_impulse_bps, 1.0), -1.0, 1.0)) * 0.26)
    ) * max(current_half_spread_bps, config.min_half_spread_bps)
    fair_value = features.microprice * (1.0 + (alpha_bps / 10_000.0))

    inventory_skew_bps = -inv_ratio * config.inventory_skew_bps
    reservation_price = fair_value * (1.0 + (inventory_skew_bps / 10_000.0))

    depth_score = math.sqrt(
        _clamp(min(features.bid_depth, features.ask_depth) / max(base_qty, 1e-9), 0.0, 25.0)
    )
    trade_score = _clamp(
        features.trade_rate_per_second / max(config.min_trade_count / max(config.flow_window_seconds, 1e-9), 0.25),
        0.0,
        3.5,
    )
    spread_tightness = _clamp(
        max(0.0, 2.0 - (current_half_spread_bps / max(config.min_half_spread_bps, 1e-9))),
        0.0,
        1.5,
    )
    active_tape_bonus = min(
        0.22,
        (0.035 * depth_score) + (0.028 * trade_score) + (0.020 * spread_tightness),
    )

    half_spread_bps = max(
        config.min_half_spread_bps,
        current_half_spread_bps * 0.78,
        (
            config.min_half_spread_bps
            + config.target_edge_bps
            + (features.recent_vol_bps * config.vol_spread_multiplier)
            + (features.toxicity_score * config.toxicity_spread_multiplier)
            + (latency_ratio * config.latency_spread_multiplier)
            - active_tape_bonus
        ),
    )

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
        bid_queue = visible_size_at_price(book.bids, bid_price, features.tick_size)
        ask_queue = visible_size_at_price(book.asks, ask_price, features.tick_size)

    spread_risk = _clamp(features.spread_bps / max(config.max_spread_bps, 1e-9), 0.0, 1.0)
    vol_risk = _clamp(features.recent_vol_bps / max(config.event_vol_bps, 1e-9), 0.0, 1.0)
    inventory_risk = abs(inv_ratio)
    size_risk_multiplier = _clamp(
        1.0
        - (features.toxicity_score * config.size_toxicity_penalty)
        - (vol_risk * config.size_vol_penalty)
        - (spread_risk * config.size_spread_penalty)
        - (inventory_risk * config.size_inventory_penalty),
        0.40,
        1.10,
    )
    liquidity_bonus_multiplier = 1.0 + min(
        0.60,
        (0.11 * depth_score) + (0.08 * trade_score) + (0.05 * spread_tightness),
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
        cap_qty *= (0.58 + (0.42 * min(size_risk_multiplier, 1.0)))

        top_level_richness = max(top_level_qty / max(side_base_qty, 1e-9), 1.0)
        depth_richness = max(side_depth_qty / max(side_base_qty, 1e-9), 1.0)
        spread_bonus = _clamp(
            (current_half_spread_bps / max(config.min_half_spread_bps, 1e-9)) - 1.0,
            0.0,
            2.0,
        )
        desired_multiplier = 1.0
        desired_multiplier += 0.14 * spread_bonus
        desired_multiplier += 0.18 * math.sqrt(top_level_richness - 1.0)
        desired_multiplier += 0.16 * math.sqrt(depth_richness - 1.0)
        desired_multiplier = _clamp(
            desired_multiplier,
            config.min_quote_size_multiplier,
            config.max_quote_size_multiplier,
        )
        desired_qty = side_base_qty * desired_multiplier
        queue_reference_qty = max(top_level_qty, side_base_qty, 1e-9)
        queue_grace_qty = 0.45 * queue_reference_qty
        queue_excess_qty = max(0.0, queue_ahead_qty - queue_grace_qty)
        queue_ratio = queue_excess_qty / queue_reference_qty
        desired_qty *= 1.0 / (1.0 + (config.queue_ahead_penalty * queue_ratio))
        desired_qty *= liquidity_bonus_multiplier
        desired_qty *= min(size_risk_multiplier, 1.0)

        min_qty = min(
            side_base_qty * config.min_quote_size_multiplier * max(0.70, min(size_risk_multiplier, 1.0)),
            cap_qty,
        )
        return max(min_qty, min(desired_qty, cap_qty))

    same_side_limit_reached = abs(inv_notional) >= config.max_inventory_notional
    bid_size_scale = 1.0
    ask_size_scale = 1.0
    if inv_ratio > 0:
        bid_size_scale = max(0.0, 1.0 - (1.7 * inv_ratio))
        ask_size_scale = min(1.7, 1.0 + (0.5 * inv_ratio))
    elif inv_ratio < 0:
        ask_size_scale = max(0.0, 1.0 - (1.7 * abs(inv_ratio)))
        bid_size_scale = min(1.7, 1.0 + (0.5 * abs(inv_ratio)))

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
        (features.flow_imbalance / max(config.one_way_flow_imbalance, 1e-9)) * 0.62
        + (features.book_imbalance / max(config.one_way_book_imbalance, 1e-9)) * 0.38
    )
    alpha_gate = max(config.one_way_alpha_bps, current_half_spread_bps * 0.30)
    if directional_bias <= -1.0 or alpha_bps <= -alpha_gate:
        bid_enabled = False
        bid_reason = "sell_pressure"
        bid_size = 0.0
        bid_price = None
    if directional_bias >= 1.0 or alpha_bps >= alpha_gate:
        ask_enabled = False
        ask_reason = "buy_pressure"
        ask_size = 0.0
        ask_price = None

    if same_side_limit_reached:
        if inventory_qty > 0:
            bid_enabled = False
            bid_reason = "inventory_limit"
            bid_size = 0.0
            bid_price = None
        elif inventory_qty < 0:
            ask_enabled = False
            ask_reason = "inventory_limit"
            ask_size = 0.0
            ask_price = None

    if inventory_qty == 0.0 and (bid_enabled != ask_enabled):
        one_way_cap_multiplier = _clamp(
            0.95
            - (0.18 * max(0.0, abs(directional_bias) - 1.0))
            - (0.30 * max(0.0, features.toxicity_score - 0.55)),
            0.40,
            0.95,
        )
        one_way_cap_qty = base_qty * one_way_cap_multiplier
        if bid_enabled:
            bid_size = min(bid_size, one_way_cap_qty)
        if ask_enabled:
            ask_size = min(ask_size, one_way_cap_qty)

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
                f"flat: bid={bid_reason} ask={ask_reason} flow={features.flow_imbalance:.3f} "
                f"book={features.book_imbalance:.3f} alpha={alpha_bps:.3f}"
            ),
            event_regime=features.event_regime,
            toxicity_score=features.toxicity_score,
        )

    quote_mode = "both" if bid_enabled and ask_enabled else "bid_only" if bid_enabled else "ask_only"
    decision_note = (
        f"{quote_mode}: flow={features.flow_imbalance:.3f} book={features.book_imbalance:.3f} "
        f"alpha={alpha_bps:.3f} half_spread={half_spread_bps:.2f}bps "
        f"liq_boost={liquidity_bonus_multiplier:.2f} size_risk={size_risk_multiplier:.2f} "
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


class KevinHypeLiquidityEngine(ProSpreadMarketMaker):
    def _configure_fee_model(self) -> None:
        self._write_event(
            "kevin_no_cost_mode_enabled",
            strategy_name=APP_NAME,
            note="Maker fees, taker fees, and rebates are disabled in this demo strategy.",
        )

    def _fee_state(self) -> FeeState:
        return zero_fee_state(turnover=self.perp_fill_turnover, started_at_ts=self.started_at_ts)

    def _on_market_snapshot(self, market: MarketSnapshot) -> None:
        self.tick_count += 1
        self._roll_day_if_needed()
        features = self._compute_market_features(market)
        self.last_quote_exchange_time_ms = features.sample_exchange_time_ms
        self.last_quote_transport_delay_ms = features.transport_delay_ms
        self.quote_age_history_ms.append(float(features.quote_age_ms))
        self.transport_delay_history_ms.append(float(features.transport_delay_ms))
        self.spread_history_bps.append(float(features.spread_bps))
        self._append_price_sample(features.sample_exchange_time_ms, features.microprice)

        fee_state = self._fee_state()
        protection_reason = inventory_protection_reason(
            features=features,
            inventory_qty=self._inventory_qty(),
            inventory_avg_price=self._inventory_avg_price(),
            inventory_opened_at_ts=self._inventory_opened_at_ts(),
            config=self.config,
        )
        initial_plan = build_kevin_quote_plan(
            features=features,
            inventory_qty=self._inventory_qty(),
            inventory_avg_price=self._inventory_avg_price(),
            book=market.book,
            config=self.config,
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
        plan = build_kevin_quote_plan(
            features=features,
            inventory_qty=self._inventory_qty(),
            inventory_avg_price=self._inventory_avg_price(),
            book=market.book,
            config=self.config,
            protection_reason=protection_reason,
        )
        snapshot = self._build_snapshot(features, plan, fee_state)
        self.last_snapshot = snapshot
        self._update_markout(snapshot)

        if kill_reason is not None:
            self._flatten_inventory(snapshot, kill_reason)
            fee_state = self._fee_state()
            plan = build_kevin_quote_plan(
                features=features,
                inventory_qty=self._inventory_qty(),
                inventory_avg_price=self._inventory_avg_price(),
                book=market.book,
                config=self.config,
            )

        block_reason = self._quoting_block_reason(time.time())
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
            self._write_event("state_snapshot", strategy_name=APP_NAME, **snapshot.to_dict())

    def run(self) -> None:
        logging.info(
            "Starting Kevin Hype Liquidity Engine asset=%s sample_ms=%d vol_window_s=%.1f impulse_window_s=%.1f flow_window_s=%.1f base_notional=%.2f max_inventory_notional=%.2f max_spread_bps=%.2f",
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
            logging.exception("Kevin Hype startup preflight failed")
            self._write_event("startup_preflight_failed", strategy_name=APP_NAME)
            return

        self.preflight_ok = True
        warm_points = self._warm_start_prices(first_snapshot.quote.microprice)
        self._configure_fee_model()
        self._write_event(
            "bot_started",
            strategy_name=APP_NAME,
            no_cost_mode=True,
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
                logging.exception("Kevin Hype realtime loop error")
                self._write_event("polling_error", strategy_name=APP_NAME, error=self.last_error)

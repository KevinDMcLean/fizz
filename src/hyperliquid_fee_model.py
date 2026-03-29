from __future__ import annotations

from dataclasses import dataclass
from math import inf
from typing import Dict, List, Optional, Tuple


PERP_TIER_THRESHOLDS: List[float] = [
    0.0,
    5_000_000.0,
    25_000_000.0,
    100_000_000.0,
    500_000_000.0,
    2_000_000_000.0,
    7_000_000_000.0,
]
PERP_TIER_LABELS = [f"Tier{i}" for i in range(len(PERP_TIER_THRESHOLDS))]

# Rates are expressed in percentage points, as in the official Hyperliquid fee table.
PERP_RATES_BY_STAKING: Dict[str, List[Tuple[float, float]]] = {
    "base": [
        (0.0450, 0.0150),
        (0.0400, 0.0120),
        (0.0350, 0.0080),
        (0.0300, 0.0040),
        (0.0280, 0.0000),
        (0.0260, 0.0000),
        (0.0240, 0.0000),
    ],
    "wood": [
        (0.0428, 0.0143),
        (0.0380, 0.0114),
        (0.0333, 0.0076),
        (0.0285, 0.0038),
        (0.0266, 0.0000),
        (0.0247, 0.0000),
        (0.0228, 0.0000),
    ],
    "bronze": [
        (0.0405, 0.0135),
        (0.0360, 0.0108),
        (0.0315, 0.0072),
        (0.0270, 0.0036),
        (0.0252, 0.0000),
        (0.0234, 0.0000),
        (0.0216, 0.0000),
    ],
    "silver": [
        (0.0383, 0.0128),
        (0.0340, 0.0102),
        (0.0298, 0.0068),
        (0.0255, 0.0034),
        (0.0238, 0.0000),
        (0.0221, 0.0000),
        (0.0204, 0.0000),
    ],
    "gold": [
        (0.0360, 0.0120),
        (0.0320, 0.0096),
        (0.0280, 0.0064),
        (0.0240, 0.0032),
        (0.0224, 0.0000),
        (0.0208, 0.0000),
        (0.0192, 0.0000),
    ],
    "platinum": [
        (0.0315, 0.0105),
        (0.0280, 0.0084),
        (0.0245, 0.0056),
        (0.0210, 0.0028),
        (0.0196, 0.0000),
        (0.0182, 0.0000),
        (0.0168, 0.0000),
    ],
    "diamond": [
        (0.0270, 0.0090),
        (0.0240, 0.0072),
        (0.0210, 0.0048),
        (0.0180, 0.0024),
        (0.0168, 0.0000),
        (0.0156, 0.0000),
        (0.0144, 0.0000),
    ],
}

MARKET_TYPES = {"standard", "hip3", "hip3_default", "hip3_growth"}


@dataclass(frozen=True)
class HyperliquidFeeConfig:
    product: str = "perps"
    market_type: str = "standard"
    staking_tier: str = "base"
    tier_basis: str = "projected"
    target_tier: int = 3
    initial_14d_perps_volume: float = 0.0
    initial_14d_spot_volume: float = 0.0
    taker_referral_discount_pct: float = 0.0
    maker_rebate_bps_override: float = 0.0
    deployer_fee_scale: float = 0.0
    growth_mode: bool = False
    aligned_quote_token: bool = False
    user_maker_rate_pct_override: Optional[float] = None
    user_taker_rate_pct_override: Optional[float] = None
    user_fee_source: str = "estimated"


@dataclass(frozen=True)
class FeeState:
    product: str
    market_type: str
    staking_tier: str
    fee_rate_source: str
    deployer_fee_scale: float
    growth_mode: bool
    aligned_quote_token: bool
    current_weighted_14d_volume: float
    projected_weighted_14d_volume: float
    decision_weighted_14d_volume: float
    actual_tier_index: int
    projected_tier_index: int
    decision_tier_index: int
    tier_label: str
    maker_rate_pct: float
    taker_rate_pct: float
    maker_rate_bps: float
    taker_rate_bps: float
    maker_rebate_bps: float
    net_maker_rate_bps: float
    daily_perp_volume_run_rate: float
    daily_weighted_volume_run_rate: float
    next_tier_index: int | None
    next_tier_label: str | None
    next_tier_threshold: float | None
    progress_to_next_tier_pct: float | None
    days_to_next_tier: float | None
    target_tier_index: int
    target_tier_threshold: float | None
    progress_to_target_tier_pct: float | None
    markets_needed_for_target_tier: float | None


@dataclass(frozen=True)
class TierRateView:
    maker_rate_pct: float
    taker_rate_pct: float
    maker_rate_bps: float
    taker_rate_bps: float
    maker_rebate_bps: float
    net_maker_rate_bps: float


def _normalize_staking_tier(raw: str) -> str:
    value = (raw or "base").strip().lower()
    if value not in PERP_RATES_BY_STAKING:
        return "base"
    return value


def _normalize_market_type(raw: str) -> str:
    value = (raw or "standard").strip().lower()
    if value not in MARKET_TYPES:
        return "standard"
    return value


def _hip3_scale(deployer_fee_scale: float) -> float:
    scale = max(0.0, deployer_fee_scale)
    if scale < 1.0:
        return scale + 1.0
    return scale * 2.0


def _perp_volume_contribution_scale(*, growth_mode: bool, aligned_quote_token: bool) -> float:
    scale = 0.1 if growth_mode else 1.0
    if aligned_quote_token:
        scale *= 1.2
    return scale


def _tier_index_for_volume(weighted_14d_volume: float) -> int:
    idx = 0
    for i, threshold in enumerate(PERP_TIER_THRESHOLDS):
        if weighted_14d_volume >= threshold:
            idx = i
    return idx


def fee_rates_for_tier(
    *,
    tier_index: int,
    config: HyperliquidFeeConfig,
    use_user_overrides: bool = False,
) -> TierRateView:
    market_type = _normalize_market_type(config.market_type)
    staking_tier = _normalize_staking_tier(config.staking_tier)
    tier_index = max(0, min(len(PERP_TIER_THRESHOLDS) - 1, tier_index))
    is_hip3 = market_type != "standard"
    deployer_fee_scale = max(0.0, config.deployer_fee_scale)
    growth_mode = bool(config.growth_mode or market_type == "hip3_growth")
    aligned_quote_token = bool(config.aligned_quote_token)

    table_taker_rate_pct, table_maker_rate_pct = PERP_RATES_BY_STAKING[staking_tier][tier_index]
    has_user_override = use_user_overrides and (
        config.user_maker_rate_pct_override is not None or config.user_taker_rate_pct_override is not None
    )
    active_referral_discount = max(0.0, min(1.0, config.taker_referral_discount_pct / 100.0))
    scale_if_hip3 = _hip3_scale(deployer_fee_scale) if is_hip3 else 1.0
    growth_mode_scale = 0.1 if growth_mode else 1.0
    deployer_share = (
        (deployer_fee_scale / (1.0 + deployer_fee_scale))
        if is_hip3 and deployer_fee_scale < 1.0
        else (0.5 if is_hip3 else 0.0)
    )
    aligned_taker_scale = ((1.0 - deployer_share) * 0.8 + deployer_share) if aligned_quote_token else 1.0
    aligned_maker_rebate_scale = ((1.0 - deployer_share) * 1.5 + deployer_share) if aligned_quote_token else 1.0

    if has_user_override:
        # Hyperliquid userFees returns final account-specific rates. Do not apply HIP-3,
        # referral, or aligned-token scaling again.
        taker_rate_pct = (
            config.user_taker_rate_pct_override
            if config.user_taker_rate_pct_override is not None
            else table_taker_rate_pct * growth_mode_scale * scale_if_hip3 * aligned_taker_scale * (1.0 - active_referral_discount)
        )
        maker_rate_pct = (
            config.user_maker_rate_pct_override
            if config.user_maker_rate_pct_override is not None
            else table_maker_rate_pct * growth_mode_scale
        )
        if maker_rate_pct > 0.0:
            maker_rate_bps = maker_rate_pct * 100.0
            maker_rebate_bps = max(0.0, config.maker_rebate_bps_override)
        else:
            maker_rate_bps = 0.0
            maker_rebate_bps = (-maker_rate_pct * 100.0) + max(0.0, config.maker_rebate_bps_override)
        taker_rate_bps = taker_rate_pct * 100.0
    else:
        maker_rate_pct = table_maker_rate_pct * growth_mode_scale
        if maker_rate_pct > 0.0:
            maker_rate_pct *= scale_if_hip3 * (1.0 - active_referral_discount)
            maker_rate_bps = maker_rate_pct * 100.0
            maker_rebate_bps = max(0.0, config.maker_rebate_bps_override)
        else:
            maker_rate_pct *= scale_if_hip3 * aligned_maker_rebate_scale
            maker_rate_bps = 0.0
            maker_rebate_bps = (-maker_rate_pct * 100.0) + max(0.0, config.maker_rebate_bps_override)

        taker_rate_pct = table_taker_rate_pct * growth_mode_scale * scale_if_hip3 * aligned_taker_scale
        taker_rate_pct *= (1.0 - active_referral_discount)
        taker_rate_bps = taker_rate_pct * 100.0
    net_maker_rate_bps = maker_rate_bps - maker_rebate_bps

    return TierRateView(
        maker_rate_pct=maker_rate_pct,
        taker_rate_pct=taker_rate_pct,
        maker_rate_bps=maker_rate_bps,
        taker_rate_bps=taker_rate_bps,
        maker_rebate_bps=maker_rebate_bps,
        net_maker_rate_bps=net_maker_rate_bps,
    )


def estimate_fee_state(
    *,
    elapsed_seconds: float,
    perp_fill_turnover: float,
    config: HyperliquidFeeConfig,
) -> FeeState:
    product = (config.product or "perps").strip().lower()
    if product != "perps":
        raise ValueError(f"Unsupported Hyperliquid product '{product}' in fee model")

    market_type = _normalize_market_type(config.market_type)
    staking_tier = _normalize_staking_tier(config.staking_tier)
    is_hip3 = market_type != "standard"
    deployer_fee_scale = max(0.0, config.deployer_fee_scale)
    growth_mode = bool(config.growth_mode or market_type == "hip3_growth")
    aligned_quote_token = bool(config.aligned_quote_token)

    elapsed_hours = max(elapsed_seconds / 3600.0, 1e-9)
    daily_perp_volume_run_rate = (perp_fill_turnover / elapsed_hours) * 24.0
    volume_contribution_scale = _perp_volume_contribution_scale(
        growth_mode=growth_mode,
        aligned_quote_token=aligned_quote_token,
    )
    daily_weighted_volume_run_rate = daily_perp_volume_run_rate * volume_contribution_scale

    current_weighted_14d_volume = (
        max(0.0, config.initial_14d_perps_volume)
        + (2.0 * max(0.0, config.initial_14d_spot_volume))
        + (max(0.0, perp_fill_turnover) * volume_contribution_scale)
    )
    projected_weighted_14d_volume = (
        max(0.0, config.initial_14d_perps_volume)
        + (2.0 * max(0.0, config.initial_14d_spot_volume))
        + (daily_weighted_volume_run_rate * 14.0)
    )
    if config.tier_basis == "actual":
        decision_weighted_14d_volume = current_weighted_14d_volume
    else:
        decision_weighted_14d_volume = max(current_weighted_14d_volume, projected_weighted_14d_volume)

    actual_tier_index = _tier_index_for_volume(current_weighted_14d_volume)
    projected_tier_index = _tier_index_for_volume(projected_weighted_14d_volume)
    decision_tier_index = _tier_index_for_volume(decision_weighted_14d_volume)
    tier_label = PERP_TIER_LABELS[decision_tier_index]

    use_actual_user_rates = (
        config.tier_basis == "actual"
        and (config.user_maker_rate_pct_override is not None or config.user_taker_rate_pct_override is not None)
    )
    rate_view = fee_rates_for_tier(
        tier_index=decision_tier_index,
        config=config,
        use_user_overrides=use_actual_user_rates,
    )
    maker_rate_pct = rate_view.maker_rate_pct
    taker_rate_pct = rate_view.taker_rate_pct
    maker_rate_bps = rate_view.maker_rate_bps
    taker_rate_bps = rate_view.taker_rate_bps
    maker_rebate_bps = rate_view.maker_rebate_bps
    net_maker_rate_bps = rate_view.net_maker_rate_bps

    next_tier_index = decision_tier_index + 1 if decision_tier_index < (len(PERP_TIER_THRESHOLDS) - 1) else None
    next_tier_label = PERP_TIER_LABELS[next_tier_index] if next_tier_index is not None else None
    next_tier_threshold = (
        PERP_TIER_THRESHOLDS[next_tier_index] if next_tier_index is not None else None
    )
    progress_to_next_tier_pct = None
    days_to_next_tier = None
    if next_tier_index is not None and next_tier_threshold is not None:
        lower = PERP_TIER_THRESHOLDS[decision_tier_index]
        span = max(next_tier_threshold - lower, 1.0)
        progress_to_next_tier_pct = max(
            0.0,
            min(100.0, ((decision_weighted_14d_volume - lower) / span) * 100.0),
        )
        remaining = max(0.0, next_tier_threshold - decision_weighted_14d_volume)
        if daily_weighted_volume_run_rate > 0.0:
            days_to_next_tier = remaining / daily_weighted_volume_run_rate

    target_tier_index = max(0, min(len(PERP_TIER_THRESHOLDS) - 1, config.target_tier))
    target_tier_threshold = PERP_TIER_THRESHOLDS[target_tier_index]
    progress_to_target_tier_pct = None
    markets_needed_for_target_tier = None
    if target_tier_threshold > 0:
        progress_to_target_tier_pct = max(
            0.0,
            min(100.0, (decision_weighted_14d_volume / target_tier_threshold) * 100.0),
        )
        if daily_weighted_volume_run_rate > 0.0:
            target_daily_volume = target_tier_threshold / 14.0
            markets_needed_for_target_tier = target_daily_volume / daily_weighted_volume_run_rate

    return FeeState(
        product=product,
        market_type=market_type,
        staking_tier=staking_tier,
        fee_rate_source=config.user_fee_source,
        deployer_fee_scale=deployer_fee_scale,
        growth_mode=growth_mode,
        aligned_quote_token=aligned_quote_token,
        current_weighted_14d_volume=current_weighted_14d_volume,
        projected_weighted_14d_volume=projected_weighted_14d_volume,
        decision_weighted_14d_volume=decision_weighted_14d_volume,
        actual_tier_index=actual_tier_index,
        projected_tier_index=projected_tier_index,
        decision_tier_index=decision_tier_index,
        tier_label=tier_label,
        maker_rate_pct=maker_rate_pct,
        taker_rate_pct=taker_rate_pct,
        maker_rate_bps=maker_rate_bps,
        taker_rate_bps=taker_rate_bps,
        maker_rebate_bps=maker_rebate_bps,
        net_maker_rate_bps=net_maker_rate_bps,
        daily_perp_volume_run_rate=daily_perp_volume_run_rate,
        daily_weighted_volume_run_rate=daily_weighted_volume_run_rate,
        next_tier_index=next_tier_index,
        next_tier_label=next_tier_label,
        next_tier_threshold=next_tier_threshold,
        progress_to_next_tier_pct=progress_to_next_tier_pct,
        days_to_next_tier=days_to_next_tier,
        target_tier_index=target_tier_index,
        target_tier_threshold=target_tier_threshold,
        progress_to_target_tier_pct=progress_to_target_tier_pct,
        markets_needed_for_target_tier=markets_needed_for_target_tier,
    )


def estimate_exchange_fee(
    *,
    notional: float,
    liquidity_role: str,
    fee_state: FeeState,
) -> float:
    role = (liquidity_role or "").strip().lower()
    if role == "aggressive":
        return max(0.0, notional) * (fee_state.taker_rate_pct / 100.0)
    maker_rate_pct = fee_state.net_maker_rate_bps / 100.0
    return max(0.0, notional) * (maker_rate_pct / 100.0)


def required_edge_bps(
    *,
    fee_state: FeeState,
    expected_taker_share: float,
    fee_buffer_bps: float,
) -> float:
    return max(
        0.0,
        fee_state.net_maker_rate_bps
        + (max(0.0, expected_taker_share) * fee_state.taker_rate_bps)
        + max(0.0, fee_buffer_bps),
    )


def dynamic_kill_floor_bps(
    *,
    fee_state: FeeState,
    slippage_bps: float,
    kill_fee_buffer_bps: float,
) -> float:
    return max(0.0, fee_state.taker_rate_bps + max(0.0, slippage_bps) + max(0.0, kill_fee_buffer_bps))


def tier_name(index: int) -> str:
    clamped = max(0, min(index, len(PERP_TIER_LABELS) - 1))
    return PERP_TIER_LABELS[clamped]


def tier_threshold(index: int) -> float:
    clamped = max(0, min(index, len(PERP_TIER_THRESHOLDS) - 1))
    return PERP_TIER_THRESHOLDS[clamped]

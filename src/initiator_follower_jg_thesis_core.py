from __future__ import annotations

import json
import time
from collections import deque
from typing import Deque, List, Optional, Tuple

from pa_momo_mid_pro_core import (
    CandidateState,
    MidFrequencyMomentumBot,
    MidPosition,
    MidSignalSnapshot,
    _bps_from_prices,
    _clamp,
    _clamp_non_negative,
    _safe_ratio,
    _utc_iso,
    _utc_now,
    compute_book_imbalance,
    compute_realized_vol_bps,
    compute_trade_flow,
    latest_reference_price,
    MarketSnapshot,
)


class InitiatorFollowerJGThesisBot(MidFrequencyMomentumBot):
    """Event-campaign runner built around the initiator-follower wedge thesis.

    The design is intentionally asymmetric:
    - use small, cheap probes near the break;
    - promote quickly into campaign mode once the move survives defensive response;
    - add hard only once open profit and follow-through justify it;
    - keep a large runner core until the actual structure fails.
    """

    def __init__(
        self,
        *,
        probe_score_min: float,
        probe_score_edge_min: float,
        probe_fast_threshold_ratio: float,
        probe_confirm_threshold_ratio: float,
        probe_breakout_slack_bps: float,
        probe_flow_multiplier: float,
        probe_book_multiplier: float,
        campaign_score_min: float,
        campaign_score_edge_min: float,
        event_impulse_bps: float,
        event_vol_bps: float,
        loss_risk_spread_ratio: float,
        loss_risk_quote_age_ratio: float,
        min_trade_rate_per_second: float,
        probe_notional_fraction: float,
        campaign_entry_notional_fraction: float,
        probe_size_boost: float,
        campaign_size_boost: float,
        campaign_add_on_trigger_r: float,
        probe_failure_hold_seconds: float,
        probe_failure_min_followthrough_bps: float,
        campaign_failure_hold_seconds: float,
        campaign_failure_min_followthrough_bps: float,
        max_probe_losses_per_side: int,
        max_global_probe_losses: int,
        probe_loss_window_seconds: float,
        probe_depth_share_limit: float,
        campaign_depth_share_limit: float,
        add_on_depth_share_limit: float,
        campaign_pre_unlock_fraction: float,
        de_risk_book_multiplier: float,
        de_risk_breakout_reclaim_bps: float,
        campaign_re_add_unlock_r: float,
        hard_reversal_confirm_ratio: float,
        hard_reversal_flow_multiplier: float,
        hard_reversal_book_multiplier: float,
        hard_reversal_max_profit_r: float,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self.probe_score_min = probe_score_min
        self.probe_score_edge_min = probe_score_edge_min
        self.probe_fast_threshold_ratio = probe_fast_threshold_ratio
        self.probe_confirm_threshold_ratio = probe_confirm_threshold_ratio
        self.probe_breakout_slack_bps = probe_breakout_slack_bps
        self.probe_flow_multiplier = probe_flow_multiplier
        self.probe_book_multiplier = probe_book_multiplier
        self.campaign_score_min = campaign_score_min
        self.campaign_score_edge_min = campaign_score_edge_min
        self.event_impulse_bps = event_impulse_bps
        self.event_vol_bps = event_vol_bps
        self.loss_risk_spread_ratio = loss_risk_spread_ratio
        self.loss_risk_quote_age_ratio = loss_risk_quote_age_ratio
        self.min_trade_rate_per_second = min_trade_rate_per_second
        self.probe_notional_fraction = probe_notional_fraction
        self.campaign_entry_notional_fraction = campaign_entry_notional_fraction
        self.probe_size_boost = probe_size_boost
        self.campaign_size_boost = campaign_size_boost
        self.campaign_add_on_trigger_r = campaign_add_on_trigger_r
        self.probe_failure_hold_seconds = probe_failure_hold_seconds
        self.probe_failure_min_followthrough_bps = probe_failure_min_followthrough_bps
        self.campaign_failure_hold_seconds = campaign_failure_hold_seconds
        self.campaign_failure_min_followthrough_bps = campaign_failure_min_followthrough_bps
        self.max_probe_losses_per_side = max_probe_losses_per_side
        self.max_global_probe_losses = max_global_probe_losses
        self.probe_loss_window_seconds = probe_loss_window_seconds
        self.probe_depth_share_limit = probe_depth_share_limit
        self.campaign_depth_share_limit = campaign_depth_share_limit
        self.add_on_depth_share_limit = add_on_depth_share_limit
        self.campaign_pre_unlock_fraction = campaign_pre_unlock_fraction
        self.de_risk_book_multiplier = de_risk_book_multiplier
        self.de_risk_breakout_reclaim_bps = de_risk_breakout_reclaim_bps
        self.campaign_re_add_unlock_r = campaign_re_add_unlock_r
        self.hard_reversal_confirm_ratio = hard_reversal_confirm_ratio
        self.hard_reversal_flow_multiplier = hard_reversal_flow_multiplier
        self.hard_reversal_book_multiplier = hard_reversal_book_multiplier
        self.hard_reversal_max_profit_r = hard_reversal_max_profit_r

        if not 0.0 < self.probe_fast_threshold_ratio <= 1.0:
            raise ValueError("Probe fast threshold ratio must be in (0, 1].")
        if not 0.0 < self.probe_confirm_threshold_ratio <= 1.0:
            raise ValueError("Probe confirm threshold ratio must be in (0, 1].")
        if self.probe_breakout_slack_bps < 0:
            raise ValueError("Probe breakout slack bps must be >= 0.")
        if self.probe_flow_multiplier <= 0 or self.probe_book_multiplier <= 0:
            raise ValueError("Probe flow/book multipliers must be > 0.")
        if self.loss_risk_spread_ratio < 1.0 or self.loss_risk_quote_age_ratio <= 0:
            raise ValueError("Loss-risk spread and quote-age ratios must be sensible positive values.")
        if self.probe_notional_fraction <= 0 or self.campaign_entry_notional_fraction <= 0:
            raise ValueError("Entry notional fractions must be > 0.")
        if self.probe_notional_fraction > self.max_notional_fraction:
            raise ValueError("Probe notional fraction must be <= max notional fraction.")
        if self.campaign_entry_notional_fraction > self.max_notional_fraction:
            raise ValueError("Campaign entry notional fraction must be <= max notional fraction.")
        if self.max_probe_losses_per_side < 0:
            raise ValueError("Max probe losses per side must be >= 0.")
        if self.max_global_probe_losses < 0:
            raise ValueError("Max global probe losses must be >= 0.")
        if self.probe_loss_window_seconds <= 0:
            raise ValueError("Probe loss window seconds must be > 0.")
        if self.probe_depth_share_limit <= 0 or self.campaign_depth_share_limit <= 0 or self.add_on_depth_share_limit <= 0:
            raise ValueError("Depth share limits must be > 0.")
        if self.campaign_pre_unlock_fraction <= 0 or self.campaign_pre_unlock_fraction > self.max_notional_fraction:
            raise ValueError("Campaign pre-unlock fraction must be in (0, max_notional_fraction].")
        if self.campaign_pre_unlock_fraction < self.campaign_entry_notional_fraction:
            raise ValueError("Campaign pre-unlock fraction must be >= campaign entry fraction.")
        if self.de_risk_book_multiplier <= 0 or self.de_risk_breakout_reclaim_bps < 0:
            raise ValueError("De-risk book multiplier must be > 0 and reclaim bps must be >= 0.")
        if self.campaign_re_add_unlock_r <= 0:
            raise ValueError("Campaign re-add unlock R must be > 0.")
        if self.hard_reversal_confirm_ratio <= 0 or self.hard_reversal_flow_multiplier <= 0 or self.hard_reversal_book_multiplier <= 0:
            raise ValueError("Hard reversal ratios and multipliers must be > 0.")
        if self.hard_reversal_max_profit_r < 0:
            raise ValueError("Hard reversal max profit R must be >= 0.")

        self.long_probe_failures: Deque[float] = deque()
        self.short_probe_failures: Deque[float] = deque()
        self.global_probe_failures: Deque[float] = deque()

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
        del extreme_blocked
        fast_component = self._normalize_component(direction * impulse_fast_bps, threshold_fast_bps)
        confirm_component = self._normalize_component(direction * impulse_confirm_bps, threshold_confirm_bps)
        breakout_component = self._normalize_component(direction * breakout_distance_bps, max(self.breakout_buffer_bps, 0.35))
        flow_component = self._normalize_component(direction * flow_value, max(self.flow_imbalance_min, 1e-6))
        book_component = self._normalize_component(direction * book_value, max(self.book_imbalance_min, 1e-6))
        trade_component = self._normalize_component(float(trade_count), float(max(self.min_trade_count, 1)))
        spread_component = _clamp(1.0 - _safe_ratio(spread_bps, max(self.max_spread_bps, 0.1)), 0.0, 1.0)
        raw = (
            0.13 * fast_component
            + 0.25 * confirm_component
            + 0.22 * breakout_component
            + 0.18 * flow_component
            + 0.12 * book_component
            + 0.05 * trade_component
            + 0.05 * spread_component
        )
        if not spread_ok:
            raw *= 0.15
        if abs(direction * impulse_confirm_bps) >= self.event_impulse_bps:
            raw += 0.08
        return round(_clamp(raw, 0.0, 1.0) * 100.0, 2)

    def _prune_probe_failures(self, bucket: Deque[float]) -> None:
        cutoff = time.time() - self.probe_loss_window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def _probe_allowed(self, side: str) -> bool:
        bucket = self.long_probe_failures if side == "LONG" else self.short_probe_failures
        self._prune_probe_failures(bucket)
        if self.max_probe_losses_per_side <= 0:
            return True
        return len(bucket) < self.max_probe_losses_per_side

    def _global_probe_allowed(self) -> bool:
        self._prune_probe_failures(self.global_probe_failures)
        if self.max_global_probe_losses <= 0:
            return True
        return len(self.global_probe_failures) < self.max_global_probe_losses

    def _record_probe_failure(self, side: str) -> None:
        now_ts = time.time()
        bucket = self.long_probe_failures if side == "LONG" else self.short_probe_failures
        bucket.append(now_ts)
        self.global_probe_failures.append(now_ts)

    def _near_side_depth_notional(self, signal: MidSignalSnapshot, side: str) -> float:
        depth = signal.ask_depth if side == "LONG" else signal.bid_depth
        return max(depth, 0.0) * max(signal.mid, 0.0)

    def _entry_depth_share_limit(self, profile: str) -> float:
        if profile == "probe":
            return self.probe_depth_share_limit
        return self.campaign_depth_share_limit

    def _liquidity_cap_notional(
        self,
        signal: MidSignalSnapshot,
        side: str,
        *,
        profile: str,
        add_on: bool,
    ) -> float:
        near_depth_notional = self._near_side_depth_notional(signal, side)
        if near_depth_notional <= 0:
            return 0.0
        share_limit = self.add_on_depth_share_limit if add_on else self._entry_depth_share_limit(profile)
        return near_depth_notional * share_limit

    def _minimum_effective_notional(self, profile: str, *, add_on: bool = False) -> float:
        if add_on:
            return self.max_notional * 0.04
        return self.max_notional * (0.02 if profile == "probe" else 0.04)

    def _campaign_unlocked(self) -> bool:
        if self.position is None or not self._campaign_live():
            return False
        return (
            self.position.add_on_count > 0
            and self.position.trailing_armed
            and self.position.locked_stop_price is not None
        )

    def _total_notional_cap(self) -> float:
        full_cap = super()._total_notional_cap()
        if self.position is None or not self._campaign_live():
            return full_cap
        staged_cap = self.max_notional * self.campaign_pre_unlock_fraction
        if self._campaign_unlocked():
            return full_cap
        return min(full_cap, staged_cap)

    def _loss_risk_blocked(
        self,
        *,
        quote_age_ms: int,
        spread_bps: float,
        trade_rate_per_second: float,
        bid_depth: float,
        ask_depth: float,
    ) -> bool:
        if quote_age_ms > int(self.max_quote_age_ms * self.loss_risk_quote_age_ratio):
            return True
        if spread_bps > (self.max_spread_bps * self.loss_risk_spread_ratio):
            return True
        if trade_rate_per_second < self.min_trade_rate_per_second:
            return True
        if min(bid_depth, ask_depth) <= 0:
            return True
        return False

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
        del extreme_blocked
        direction = 1.0 if side == "LONG" else -1.0
        score_edge = score - opposing_score
        flow = direction * flow_imbalance
        book = direction * book_imbalance
        directional_fast = direction * fast_impulse_bps
        directional_confirm = direction * confirm_impulse_bps
        if (
            full_ready
            and trade_count_ok
            and spread_ok
            and score >= self.campaign_score_min
            and score_edge >= self.campaign_score_edge_min
        ):
            return "campaign"
        probe_boundary = -self.probe_breakout_slack_bps
        if (
            self._global_probe_allowed()
            and self._probe_allowed(side)
            and trade_count_ok
            and spread_ok
            and score >= self.probe_score_min
            and score_edge >= self.probe_score_edge_min
            and directional_fast >= fast_threshold_bps * self.probe_fast_threshold_ratio
            and directional_confirm >= confirm_threshold_bps * self.probe_confirm_threshold_ratio
            and breakout_distance_bps >= probe_boundary
            and flow >= (self.flow_imbalance_min * self.probe_flow_multiplier)
            and book >= (self.book_imbalance_min * self.probe_book_multiplier)
        ):
            return "probe"
        return None

    def _required_confirmation_samples(
        self,
        *,
        side: str,
        profile: str,
        score: float,
    ) -> int:
        del side
        if profile == "campaign":
            return 1
        if score >= self.instant_entry_score_min:
            return 1
        return max(1, self.entry_confirmation_samples)

    def _campaign_live(self) -> bool:
        if self.position is None:
            return False
        return self.position.entry_profile == "campaign" or self.position.add_on_count > 0

    def _maybe_promote_probe_to_campaign(
        self,
        signal: MidSignalSnapshot,
        *,
        current_realized_bps: float,
        mfe_bps: float,
    ) -> bool:
        if self.position is None or self.position.entry_profile != "probe":
            return False
        side = self.position.side
        score = signal.long_score if side == "LONG" else signal.short_score
        opposing_score = signal.short_score if side == "LONG" else signal.long_score
        directional_breakout = self._directional_breakout_distance(signal, side)
        directional_confirm = self._directional_confirm_impulse(signal, side)
        directional_flow = self._directional_flow(signal, side)
        directional_book = self._directional_book(signal, side)
        if signal.regime != "campaign":
            return False
        if score < self.campaign_score_min:
            return False
        if (score - opposing_score) < self.campaign_score_edge_min:
            return False
        if directional_confirm < signal.dynamic_confirm_threshold_bps:
            return False
        if directional_breakout < -max(self.probe_breakout_slack_bps * 0.5, 0.05):
            return False
        if directional_flow < self.flow_imbalance_min or directional_book < self.book_imbalance_min:
            return False
        if max(current_realized_bps, mfe_bps) < (self.position.stop_distance_bps * 0.20):
            return False
        self.position.entry_profile = "campaign"
        self.position.failure_horizon_seconds = self.campaign_failure_hold_seconds
        self.position.failure_min_followthrough_bps = self.campaign_failure_min_followthrough_bps
        self.position.trail_arm_bps = max(
            self.campaign_failure_min_followthrough_bps,
            self.position.stop_distance_bps * self.trail_arm_r,
        )
        self.position.trail_distance_bps = max(
            self.position.trail_distance_bps,
            self._compute_trail_distance_bps(signal) * 1.10,
        )
        self._write_event(
            "probe_promoted_to_campaign",
            trade_id=self.position.trade_id,
            side=side,
            score=score,
            opposing_score=opposing_score,
            current_realized_bps=current_realized_bps,
            mfe_bps=mfe_bps,
            directional_breakout_bps=directional_breakout,
            directional_confirm_bps=directional_confirm,
            directional_flow=directional_flow,
            directional_book=directional_book,
            trail_arm_bps=self.position.trail_arm_bps,
            failure_horizon_seconds=self.position.failure_horizon_seconds,
            failure_min_followthrough_bps=self.position.failure_min_followthrough_bps,
        )
        return True

    def _state_label(self, snapshot: Optional[MidSignalSnapshot]) -> str:
        if self.position is not None:
            if self._campaign_live():
                return "campaign_live"
            return "probe_live"
        if self.last_exit_ts is not None and (time.time() - self.last_exit_ts) < self.cooldown_seconds:
            return "cooldown"
        if snapshot is None:
            return "idle"
        if self.pending_candidate is not None:
            if self.pending_candidate.count >= self.pending_candidate.required_samples:
                return "armed"
            return "probe_setup" if self.pending_candidate.profile == "probe" else "campaign_setup"
        if snapshot.regime == "campaign":
            return "campaign_watch"
        if snapshot.regime == "tension":
            return "tension"
        return "idle"

    def _maybe_enter(self, signal: MidSignalSnapshot) -> None:
        if signal.regime not in {"tension", "campaign"}:
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
        event_regime = (
            abs(confirm_impulse_bps) >= self.event_impulse_bps
            or recent_vol_bps >= self.event_vol_bps
        )
        loss_risk_blocked = self._loss_risk_blocked(
            quote_age_ms=quote_age_ms,
            spread_bps=quote.spread_bps,
            trade_rate_per_second=flow["trade_rate_per_second"],
            bid_depth=bid_depth,
            ask_depth=ask_depth,
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
            extreme_blocked=False,
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
            extreme_blocked=False,
        )

        long_setup = (
            spread_ok
            and not loss_risk_blocked
            and trade_count_ok
            and long_score >= self.probe_score_min
        )
        short_setup = (
            spread_ok
            and not loss_risk_blocked
            and trade_count_ok
            and short_score >= self.probe_score_min
        )
        long_ready = (
            long_setup
            and long_fast_ok
            and long_confirm_ok
            and long_breakout_ok
            and long_flow_ok
            and long_book_ok
            and long_score >= self.campaign_score_min
            and (long_score - short_score) >= self.campaign_score_edge_min
        )
        short_ready = (
            short_setup
            and short_fast_ok
            and short_confirm_ok
            and short_breakout_ok
            and short_flow_ok
            and short_book_ok
            and short_score >= self.campaign_score_min
            and (short_score - long_score) >= self.campaign_score_edge_min
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
            spread_ok=spread_ok and not loss_risk_blocked,
            extreme_blocked=False,
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
            spread_ok=spread_ok and not loss_risk_blocked,
            extreme_blocked=False,
            full_ready=short_ready,
        )

        if not spread_ok or loss_risk_blocked:
            regime = "blocked"
        elif long_ready or short_ready or event_regime:
            regime = "campaign"
        elif long_setup or short_setup or abs(confirm_impulse_bps) >= dynamic_confirm_threshold_bps * 0.55:
            regime = "tension"
        else:
            regime = "cold"

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
            spread_ok=spread_ok and not loss_risk_blocked,
            extreme_blocked=False,
            regime=regime,
            long_score=long_score,
            short_score=short_score,
            long_setup=long_setup,
            short_setup=short_setup,
            long_ready=long_ready,
            short_ready=short_ready,
            long_tactical_ok=long_entry_profile == "probe",
            short_tactical_ok=short_entry_profile == "probe",
            long_entry_profile=long_entry_profile,
            short_entry_profile=short_entry_profile,
            sample_count=len(self.price_samples),
        )

    def _entry_notional_target(
        self,
        *,
        signal: MidSignalSnapshot,
        side: str,
        profile: str,
    ) -> Tuple[float, float, float, float]:
        stop_distance_bps = self._compute_stop_distance_bps_for_profile(signal, profile)
        price_stop_fraction = stop_distance_bps / 10_000.0
        risk_amount = self.account_balance * self.equity_risk_pct
        size_multiplier = self._size_multiplier(signal=signal, side=side, profile=profile)
        notional_target = (risk_amount / max(price_stop_fraction, 1e-9)) * size_multiplier
        return stop_distance_bps, price_stop_fraction, size_multiplier, notional_target

    def _compute_stop_distance_bps_for_profile(self, signal: MidSignalSnapshot, profile: str) -> float:
        base = max(
            self.initial_stop_min_bps,
            self.initial_stop_vol_multiplier * max(signal.recent_vol_bps, signal.spread_bps),
            abs(signal.confirm_impulse_bps) * (0.28 if profile == "probe" else 0.40),
        )
        if profile == "probe":
            return max(self.initial_stop_min_bps * 0.85, base * 0.85)
        return max(self.initial_stop_min_bps, base)

    def _entry_cap_fraction(self, profile: str) -> float:
        if profile == "probe":
            return self.probe_notional_fraction
        return self.campaign_entry_notional_fraction

    def _size_multiplier(
        self,
        *,
        signal: MidSignalSnapshot,
        side: str,
        profile: str,
    ) -> float:
        score = signal.long_score if side == "LONG" else signal.short_score
        score_component = _clamp(
            _safe_ratio(score - self.probe_score_min, max(100.0 - self.probe_score_min, 1.0)),
            0.0,
            1.0,
        )
        spread_component = _clamp(
            1.0 - _safe_ratio(signal.spread_bps, max(self.max_spread_bps, 0.1)),
            0.0,
            1.0,
        )
        breakout_component = _clamp(
            _safe_ratio(
                self._directional_breakout_distance(signal, side) + self.probe_breakout_slack_bps,
                max(self.breakout_buffer_bps + self.probe_breakout_slack_bps, 0.5),
            ),
            0.0,
            1.0,
        )
        flow_component = _clamp(abs(signal.flow_imbalance), 0.0, 1.0)
        profile_boost = self.probe_size_boost if profile == "probe" else self.campaign_size_boost
        event_boost = 0.10 if signal.regime == "campaign" else 0.0
        multiplier = 1.0 + profile_boost + event_boost + (
            self.score_notional_boost
            * ((0.45 * score_component) + (0.25 * breakout_component) + (0.20 * flow_component) + (0.10 * spread_component))
        )
        return round(min(multiplier, 1.0 + profile_boost + event_boost + self.score_notional_boost), 6)

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
        if entry_profile == "probe":
            trail_distance_bps *= 0.80
        else:
            trail_distance_bps *= 1.15
        notional_cap = self.max_notional * self._entry_cap_fraction(entry_profile)
        liquidity_cap = self._liquidity_cap_notional(signal, side, profile=entry_profile, add_on=False)
        notional = min(notional_target, notional_cap, liquidity_cap)
        if notional < self._minimum_effective_notional(entry_profile):
            self._write_event(
                "entry_rejected_liquidity_cap",
                side=side,
                entry_profile=entry_profile,
                requested_notional=notional_target,
                cap_notional=notional_cap,
                liquidity_cap=liquidity_cap,
                near_side_depth_notional=self._near_side_depth_notional(signal, side),
                signal=signal.to_dict(),
            )
            self.pending_candidate = None
            return
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
        if entry_profile == "probe":
            failure_horizon_seconds = self.probe_failure_hold_seconds
            failure_min_followthrough_bps = self.probe_failure_min_followthrough_bps
            trail_arm_bps = max(failure_min_followthrough_bps, stop_distance_bps * 0.70)
        else:
            failure_horizon_seconds = self.campaign_failure_hold_seconds
            failure_min_followthrough_bps = self.campaign_failure_min_followthrough_bps
            trail_arm_bps = max(failure_min_followthrough_bps, stop_distance_bps * self.trail_arm_r)

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
            failure_horizon_seconds=failure_horizon_seconds,
            failure_min_followthrough_bps=failure_min_followthrough_bps,
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

    def _add_on_ready(self, signal: MidSignalSnapshot) -> bool:
        if self.position is None:
            return False
        position = self.position
        if position.add_on_count >= self.max_add_ons:
            return False
        if signal.regime not in {"tension", "campaign"} or not signal.spread_ok:
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
        trigger_r = self.campaign_add_on_trigger_r
        if position.add_on_count > 0 and position.locked_stop_price is None:
            if current_realized_bps < (position.stop_distance_bps * self.campaign_re_add_unlock_r):
                return False
        if current_realized_bps < (position.stop_distance_bps * trigger_r):
            return False
        if mfe_bps < (position.stop_distance_bps * trigger_r):
            return False
        if score < self.add_on_score_min:
            return False
        if confirm_impulse < (signal.dynamic_confirm_threshold_bps * 0.90):
            return False
        if position.add_on_count == 0 and position.locked_stop_price is None:
            first_add_extension = max(
                self.add_on_breakout_extension_bps,
                signal.breakout_buffer_bps + max(self.probe_breakout_slack_bps * 0.35, 0.10),
            )
            if breakout_distance < first_add_extension:
                return False
        else:
            breakout_hold_floor = -max(self.probe_breakout_slack_bps * 0.35, 0.05)
            if breakout_distance < breakout_hold_floor:
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
        liquidity_cap = self._liquidity_cap_notional(signal, side, profile=position.entry_profile, add_on=True)
        add_notional = min(remaining_cap, add_cap * add_scale, liquidity_cap)
        if add_notional < self._minimum_effective_notional(position.entry_profile, add_on=True):
            self._write_event(
                "add_on_rejected_liquidity_cap",
                trade_id=position.trade_id,
                side=side,
                add_on_count=position.add_on_count,
                remaining_cap=remaining_cap,
                raw_add_cap=add_cap * add_scale,
                liquidity_cap=liquidity_cap,
                near_side_depth_notional=self._near_side_depth_notional(signal, side),
                signal=signal.to_dict(),
            )
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
        book = self._directional_book(signal, position.side)
        breakout_distance = self._directional_breakout_distance(signal, position.side)

        confirm_weak = confirm_impulse < (signal.dynamic_confirm_threshold_bps * self.de_risk_confirm_ratio)
        flow_weak = flow < (self.flow_imbalance_min * self.de_risk_flow_multiplier)
        book_weak = book < (self.book_imbalance_min * self.de_risk_book_multiplier)
        breakout_reclaimed = breakout_distance < -max(
            self.de_risk_breakout_reclaim_bps,
            self.probe_breakout_slack_bps * 0.25,
        )

        if breakout_reclaimed and ((confirm_weak and flow_weak) or book_weak):
            return True

        slowdown_flags = 0
        if score < self.de_risk_score_threshold:
            slowdown_flags += 1
        if confirm_weak:
            slowdown_flags += 1
        if flow_weak:
            slowdown_flags += 1
        if book_weak:
            slowdown_flags += 1
        if breakout_reclaimed:
            slowdown_flags += 1
        if signal.regime == "blocked" or signal.trade_count < self.min_trade_count:
            slowdown_flags += 1
        return slowdown_flags >= 3

    def _breakout_failure(self, signal: MidSignalSnapshot) -> bool:
        if self.position is None:
            return False
        live_campaign = self._campaign_live()
        fail_buffer_bps = self.breakout_fail_buffer_bps * (1.5 if live_campaign else 0.65)
        if self.position.side == "LONG":
            fail_level = self.position.opened_features.get("breakout_high")
            if fail_level is None:
                return False
            threshold = float(fail_level) * (1.0 - (fail_buffer_bps / 10_000.0))
            return signal.mid <= threshold
        fail_level = self.position.opened_features.get("breakout_low")
        if fail_level is None:
            return False
        threshold = float(fail_level) * (1.0 + (fail_buffer_bps / 10_000.0))
        return signal.mid >= threshold

    def _flow_flip_exit(self, signal: MidSignalSnapshot, current_realized_bps: float) -> bool:
        if self.position is None:
            return False
        flip_floor = max(1.5, self.position.stop_distance_bps * (0.25 if self._campaign_live() else 0.10))
        if current_realized_bps >= flip_floor:
            return False
        return super()._flow_flip_exit(signal, current_realized_bps)

    def _hard_reversal_exit(
        self,
        signal: MidSignalSnapshot,
        *,
        hold_seconds: float,
        current_realized_bps: float,
    ) -> bool:
        if self.position is None or hold_seconds < 0.75:
            return False
        max_profit_floor = self.position.stop_distance_bps * self.hard_reversal_max_profit_r
        if current_realized_bps > max_profit_floor:
            return False
        directional_confirm = self._directional_confirm_impulse(signal, self.position.side)
        directional_flow = self._directional_flow(signal, self.position.side)
        directional_book = self._directional_book(signal, self.position.side)
        return (
            directional_confirm <= -(signal.dynamic_confirm_threshold_bps * self.hard_reversal_confirm_ratio)
            and directional_flow <= -(self.flow_imbalance_min * self.hard_reversal_flow_multiplier)
            and directional_book <= -(self.book_imbalance_min * self.hard_reversal_book_multiplier)
        )

    def _close_position(
        self,
        signal: MidSignalSnapshot,
        exit_reason: str,
        trigger_price: float,
        mfe_bps: float,
        mae_bps: float,
    ) -> None:
        if self.position is not None and self.position.entry_profile == "probe":
            probe_side = self.position.side
            should_count = exit_reason in {
                "failed_followthrough",
                "breakout_failure",
                "flow_flip_exit",
                "hard_reversal_exit",
                "initial_stop_hit",
                "time_stop",
            }
            if should_count:
                self._record_probe_failure(probe_side)
        super()._close_position(signal, exit_reason, trigger_price, mfe_bps, mae_bps)

    def _maybe_close_position(self, signal: MidSignalSnapshot) -> None:
        assert self.position is not None
        current_exit_price = self._current_exit_price(signal)
        hold_seconds = max(0.0, time.time() - self.position.opened_at)
        mfe_bps, mae_bps = self._position_mfe_bps(current_exit_price)
        self.position.last_metrics = {"mfe_bps": mfe_bps, "mae_bps": mae_bps}
        current_realized_bps = self._current_realized_bps(current_exit_price)

        self._maybe_promote_probe_to_campaign(
            signal,
            current_realized_bps=current_realized_bps,
            mfe_bps=mfe_bps,
        )

        if (
            hold_seconds >= self.position.failure_horizon_seconds
            and mfe_bps < self.position.failure_min_followthrough_bps
        ):
            self._close_position(signal, "failed_followthrough", current_exit_price, mfe_bps, mae_bps)
            return
        if self._hard_reversal_exit(
            signal,
            hold_seconds=hold_seconds,
            current_realized_bps=current_realized_bps,
        ):
            self._close_position(signal, "hard_reversal_exit", current_exit_price, mfe_bps, mae_bps)
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

    def _write_sample(self, snapshot: MidSignalSnapshot) -> None:
        current_unrealized = 0.0
        position_side: Optional[str] = None
        trade_id: Optional[int] = None
        active_stop: Optional[float] = None
        locked_stop: Optional[float] = None
        entry_price: Optional[float] = None
        hold_seconds: Optional[float] = None
        trail_armed = False
        position_phase: Optional[str] = None
        if self.position is not None:
            position_side = self.position.side
            trade_id = self.position.trade_id
            active_stop = self.position.active_stop_price
            locked_stop = self.position.locked_stop_price
            entry_price = self.position.entry_price
            hold_seconds = max(0.0, time.time() - self.position.opened_at)
            trail_armed = self.position.trailing_armed
            position_phase = "campaign" if self._campaign_live() else "probe"
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
            "position_phase": position_phase,
            "size_multiplier": self.position.size_multiplier if self.position is not None else None,
            "probe_long_failures": len(self.long_probe_failures),
            "probe_short_failures": len(self.short_probe_failures),
            "probe_global_failures": len(self.global_probe_failures),
            "probe_allowed_long": self._probe_allowed("LONG"),
            "probe_allowed_short": self._probe_allowed("SHORT"),
            "probe_allowed_global": self._global_probe_allowed(),
            "event_regime": snapshot.regime == "campaign",
            "loss_risk_guarded": snapshot.regime == "blocked",
            "campaign_unlocked": self._campaign_unlocked(),
            "position_cap_fraction": (_safe_ratio(self._total_notional_cap(), self.max_notional) if self.max_notional > 0 else None),
        }
        try:
            with self.samples_jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except Exception:
            import logging

            logging.exception("Failed writing initiator-follower sample JSONL")

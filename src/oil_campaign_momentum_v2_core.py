from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path
from typing import Deque, Dict, Optional

from initiator_follower_jg_thesis_core import InitiatorFollowerJGThesisBot
from pa_momo_mid_pro_core import (
    CandidateState,
    MarketSnapshot,
    MidSignalSnapshot,
    _bps_from_prices,
    _clamp,
    _safe_ratio,
    _utc_iso,
    compute_book_imbalance,
    compute_realized_vol_bps,
    compute_trade_flow,
    latest_reference_price,
)


APP_NAME = "Oil Campaign Momentum v2"


class OilCampaignMomentumV2Bot(InitiatorFollowerJGThesisBot):
    """Tick-aware oil campaign runner built on the initiator-follower base."""

    def __init__(
        self,
        *,
        tick_size: float,
        breakout_buffer_ticks: float,
        probe_breakout_slack_ticks: float,
        probe_min_breakout_fraction: float,
        probe_max_spread_bps: float,
        breakout_reclaim_ticks: float,
        add_on_extension_ticks: float,
        failed_breakout_ticks: float,
        depth_vacuum_near_ratio_max: float,
        initiative_persistence_windows: int,
        reclaim_veto_window_seconds: float,
        reclaim_cooldown_seconds: float,
        probe_promotion_mfe_r: float,
        probe_promotion_min_hold_seconds: float,
        probe_trade_participation_cap: float,
        campaign_trade_participation_cap: float,
        add_on_trade_participation_cap: float,
        campaign_wide_trail_until_r: float,
        campaign_tighten_after_r: float,
        campaign_loose_trail_multiplier: float,
        campaign_tight_trail_multiplier: float,
        features_jsonl_path: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        if tick_size <= 0:
            raise ValueError("Tick size must be > 0.")
        if breakout_buffer_ticks < 0 or probe_breakout_slack_ticks < 0:
            raise ValueError("Tick floor controls must be >= 0.")
        if probe_min_breakout_fraction <= 0:
            raise ValueError("Probe minimum breakout fraction must be > 0.")
        if probe_max_spread_bps <= 0:
            raise ValueError("Probe max spread bps must be > 0.")
        if breakout_reclaim_ticks < 0 or add_on_extension_ticks < 0 or failed_breakout_ticks < 0:
            raise ValueError("Breakout reclaim/add-on/failure tick floors must be >= 0.")
        if not 0.0 < depth_vacuum_near_ratio_max <= 1.0:
            raise ValueError("Depth vacuum near ratio max must be in (0, 1].")
        if initiative_persistence_windows < 1:
            raise ValueError("Initiative persistence windows must be >= 1.")
        if reclaim_veto_window_seconds <= 0 or reclaim_cooldown_seconds < 0:
            raise ValueError("Reclaim veto window must be > 0 and cooldown must be >= 0.")
        if probe_promotion_mfe_r <= 0:
            raise ValueError("Probe promotion MFE R must be > 0.")
        if probe_promotion_min_hold_seconds <= 0:
            raise ValueError("Probe promotion minimum hold seconds must be > 0.")
        if probe_trade_participation_cap <= 0 or campaign_trade_participation_cap <= 0 or add_on_trade_participation_cap <= 0:
            raise ValueError("Trade participation caps must be > 0.")
        if campaign_wide_trail_until_r <= 0 or campaign_tighten_after_r <= 0:
            raise ValueError("Campaign trail R thresholds must be > 0.")
        if campaign_tighten_after_r < campaign_wide_trail_until_r:
            raise ValueError("Campaign tighten threshold must be >= wide-trail threshold.")
        if campaign_loose_trail_multiplier <= 0 or campaign_tight_trail_multiplier <= 0:
            raise ValueError("Campaign trail multipliers must be > 0.")

        self.tick_size = tick_size
        self.breakout_buffer_ticks = breakout_buffer_ticks
        self.probe_breakout_slack_ticks = probe_breakout_slack_ticks
        self.probe_min_breakout_fraction = probe_min_breakout_fraction
        self.probe_max_spread_bps = probe_max_spread_bps
        self.breakout_reclaim_ticks = breakout_reclaim_ticks
        self.add_on_extension_ticks = add_on_extension_ticks
        self.failed_breakout_ticks = failed_breakout_ticks
        self.depth_vacuum_near_ratio_max = depth_vacuum_near_ratio_max
        self.initiative_persistence_windows = initiative_persistence_windows
        self.reclaim_veto_window_seconds = reclaim_veto_window_seconds
        self.reclaim_cooldown_seconds = reclaim_cooldown_seconds
        self.probe_promotion_mfe_r = probe_promotion_mfe_r
        self.probe_promotion_min_hold_seconds = probe_promotion_min_hold_seconds
        self.probe_trade_participation_cap = probe_trade_participation_cap
        self.campaign_trade_participation_cap = campaign_trade_participation_cap
        self.add_on_trade_participation_cap = add_on_trade_participation_cap
        self.campaign_wide_trail_until_r = campaign_wide_trail_until_r
        self.campaign_tighten_after_r = campaign_tighten_after_r
        self.campaign_loose_trail_multiplier = campaign_loose_trail_multiplier
        self.campaign_tight_trail_multiplier = campaign_tight_trail_multiplier
        self.features_jsonl_path = Path(features_jsonl_path) if features_jsonl_path else None
        if self.features_jsonl_path is not None:
            self.features_jsonl_path.parent.mkdir(parents=True, exist_ok=True)

        self.flow_window_history: Deque[float] = deque(maxlen=max(initiative_persistence_windows, 6))
        self.last_break_reclaim_exit_ts: float | None = None

    def _tick_bps(self, price: float) -> float:
        if price <= 0:
            return 0.0
        return (self.tick_size / price) * 10_000.0

    def _effective_bps(self, price: float, raw_bps: float, tick_floor: float) -> float:
        return max(raw_bps, self._tick_bps(price) * tick_floor)

    def _effective_breakout_buffer_bps(self, price: float) -> float:
        return self._effective_bps(price, self.breakout_buffer_bps, self.breakout_buffer_ticks)

    def _effective_probe_breakout_slack_bps(self, price: float) -> float:
        return self._effective_bps(price, self.probe_breakout_slack_bps, self.probe_breakout_slack_ticks)

    def _effective_breakout_reclaim_bps(self, price: float) -> float:
        return self._effective_bps(price, self.de_risk_breakout_reclaim_bps, self.breakout_reclaim_ticks)

    def _effective_add_on_extension_bps(self, price: float) -> float:
        return self._effective_bps(price, self.add_on_breakout_extension_bps, self.add_on_extension_ticks)

    def _effective_failed_breakout_bps(self, price: float) -> float:
        return self._effective_bps(price, self.breakout_fail_buffer_bps, self.failed_breakout_ticks)

    def _depth_vacuum_ok(self, *, side: str, bid_depth: float, ask_depth: float) -> bool:
        near_depth = ask_depth if side == "LONG" else bid_depth
        far_depth = bid_depth if side == "LONG" else ask_depth
        if near_depth <= 0 or far_depth <= 0:
            return False
        return (near_depth / far_depth) <= self.depth_vacuum_near_ratio_max

    def _append_flow_window(self, value: float) -> None:
        self.flow_window_history.append(value)

    def _initiative_persistent(self, side: str) -> bool:
        if len(self.flow_window_history) < self.initiative_persistence_windows:
            return False
        threshold = self.flow_imbalance_min * 0.85
        recent = list(self.flow_window_history)[-self.initiative_persistence_windows :]
        if side == "LONG":
            return all(value >= threshold for value in recent)
        return all(value <= -threshold for value in recent)

    def _runner_live(self) -> bool:
        if self.position is None:
            return False
        return self.position.trailing_armed and self.position.locked_stop_price is not None and self.position.add_on_count > 0

    def _recent_traded_notional(self, signal: MidSignalSnapshot) -> float:
        traded_volume = max(signal.buy_volume + signal.sell_volume, 0.0)
        return traded_volume * max(signal.mid, 0.0)

    def _trade_participation_cap_notional(
        self,
        signal: MidSignalSnapshot,
        *,
        profile: str,
        add_on: bool,
    ) -> float:
        traded_notional = self._recent_traded_notional(signal)
        if traded_notional <= 0:
            return 0.0
        if add_on:
            cap_fraction = self.add_on_trade_participation_cap
        elif profile == "probe":
            cap_fraction = self.probe_trade_participation_cap
        else:
            cap_fraction = self.campaign_trade_participation_cap
        return traded_notional * cap_fraction

    def _depth_liquidity_cap_notional(
        self,
        signal: MidSignalSnapshot,
        side: str,
        *,
        profile: str,
        add_on: bool,
    ) -> float:
        return super()._liquidity_cap_notional(signal, side, profile=profile, add_on=add_on)

    def _liquidity_cap_notional(
        self,
        signal: MidSignalSnapshot,
        side: str,
        *,
        profile: str,
        add_on: bool,
    ) -> float:
        depth_cap = self._depth_liquidity_cap_notional(signal, side, profile=profile, add_on=add_on)
        trade_cap = self._trade_participation_cap_notional(signal, profile=profile, add_on=add_on)
        if trade_cap <= 0:
            return depth_cap
        if depth_cap <= 0:
            return trade_cap
        return min(depth_cap, trade_cap)

    def _entry_size_logging_fields(
        self,
        signal: MidSignalSnapshot,
        *,
        side: str,
        profile: str,
        notional: float,
    ) -> Dict[str, float]:
        recent_traded_notional = self._recent_traded_notional(signal)
        depth_cap = self._depth_liquidity_cap_notional(signal, side, profile=profile, add_on=False)
        trade_cap = self._trade_participation_cap_notional(signal, profile=profile, add_on=False)
        return {
            "entry_notional_pct_buying_power": _safe_ratio(notional, self.max_notional) * 100.0 if self.max_notional > 0 else 0.0,
            "entry_required_margin_pct_equity": _safe_ratio(notional / float(self.leverage), self.account_balance) * 100.0
            if self.account_balance > 0 and self.leverage > 0
            else 0.0,
            "entry_notional_pct_recent_traded": _safe_ratio(notional, recent_traded_notional) * 100.0 if recent_traded_notional > 0 else 0.0,
            "entry_notional_pct_depth_cap": _safe_ratio(notional, depth_cap) * 100.0 if depth_cap > 0 else 0.0,
            "entry_notional_pct_trade_cap": _safe_ratio(notional, trade_cap) * 100.0 if trade_cap > 0 else 0.0,
        }

    def _position_metrics_snapshot(
        self,
        signal: MidSignalSnapshot,
        *,
        hold_seconds: float | None = None,
        current_realized_bps: float | None = None,
        mfe_bps: float | None = None,
        mae_bps: float | None = None,
    ) -> Dict[str, float]:
        assert self.position is not None
        current_exit_price = self._current_exit_price(signal)
        if hold_seconds is None:
            hold_seconds = max(0.0, time.time() - self.position.opened_at)
        if current_realized_bps is None:
            current_realized_bps = self._current_realized_bps(current_exit_price)
        if mfe_bps is None:
            mfe_bps = self._position_mfe_bps(current_exit_price)[0]
        if mae_bps is None:
            mae_bps = self._position_mfe_bps(current_exit_price)[1]
        direction = 1.0 if self.position.side == "LONG" else -1.0
        gross = (current_exit_price - self.position.entry_price) * self.position.size * direction
        current_unrealized = self.position.realized_pnl_locked + gross - self.position.entry_fee_paid
        score = signal.long_score if self.position.side == "LONG" else signal.short_score
        opposing_score = signal.short_score if self.position.side == "LONG" else signal.long_score
        return {
            "hold_seconds": hold_seconds,
            "breakout_distance_bps": self._directional_breakout_distance(signal, self.position.side),
            "score": score,
            "score_edge": score - opposing_score,
            "directional_flow": self._directional_flow(signal, self.position.side),
            "directional_book": self._directional_book(signal, self.position.side),
            "recent_traded_notional": self._recent_traded_notional(signal),
            "current_realized_bps": current_realized_bps,
            "mfe_bps": mfe_bps,
            "mae_bps": mae_bps,
            "current_unrealized_pnl": current_unrealized,
        }

    def _capture_position_hold_quality(
        self,
        signal: MidSignalSnapshot,
        *,
        hold_seconds: float | None = None,
        current_realized_bps: float | None = None,
        mfe_bps: float | None = None,
        mae_bps: float | None = None,
    ) -> Dict[str, float]:
        assert self.position is not None
        metrics = self._position_metrics_snapshot(
            signal,
            hold_seconds=hold_seconds,
            current_realized_bps=current_realized_bps,
            mfe_bps=mfe_bps,
            mae_bps=mae_bps,
        )
        self.position.last_metrics.update(
            {
                "last_hold_seconds": metrics["hold_seconds"],
                "last_breakout_distance_bps": metrics["breakout_distance_bps"],
                "last_score": metrics["score"],
                "last_score_edge": metrics["score_edge"],
                "last_directional_flow": metrics["directional_flow"],
                "last_directional_book": metrics["directional_book"],
                "last_recent_traded_notional": metrics["recent_traded_notional"],
                "last_current_realized_bps": metrics["current_realized_bps"],
                "last_mfe_bps": metrics["mfe_bps"],
                "last_mae_bps": metrics["mae_bps"],
                "last_current_unrealized_pnl": metrics["current_unrealized_pnl"],
            }
        )
        for threshold, label in ((1.0, "hold_1s"), (2.0, "hold_2s")):
            if metrics["hold_seconds"] >= threshold and f"{label}_realized_bps" not in self.position.last_metrics:
                self.position.last_metrics.update(
                    {
                        f"{label}_realized_bps": metrics["current_realized_bps"],
                        f"{label}_breakout_distance_bps": metrics["breakout_distance_bps"],
                        f"{label}_score_edge": metrics["score_edge"],
                        f"{label}_directional_flow": metrics["directional_flow"],
                        f"{label}_directional_book": metrics["directional_book"],
                        f"{label}_recent_traded_notional": metrics["recent_traded_notional"],
                        f"{label}_unrealized_pnl": metrics["current_unrealized_pnl"],
                    }
                )
        return metrics

    def _exit_bucket(self, exit_reason: str, *, hold_seconds: float) -> str:
        if exit_reason == "break_reclaim_veto":
            return "reclaim_veto"
        if exit_reason == "failed_followthrough":
            return "failed_followthrough"
        if exit_reason == "breakout_failure":
            return "breakout_failure"
        if exit_reason == "trailing_stop_hit":
            return "trail_stop"
        if exit_reason == "initial_stop_hit":
            return "initial_stop"
        if exit_reason == "flow_flip_exit":
            return "flow_flip"
        if exit_reason == "hard_reversal_exit":
            return "hard_reversal"
        if exit_reason == "time_stop":
            return "time_stop"
        if hold_seconds <= 10.0:
            return "early_exit_other"
        return "other"

    def _entry_allowed(self, now_ts: float) -> bool:
        if not super()._entry_allowed(now_ts):
            return False
        if self.last_break_reclaim_exit_ts is None:
            return True
        return (now_ts - self.last_break_reclaim_exit_ts) >= self.reclaim_cooldown_seconds

    def _allow_campaign_first_sample_entry(
        self,
        signal: MidSignalSnapshot,
        candidate: CandidateState,
    ) -> bool:
        if candidate.profile != "campaign":
            return False
        if candidate.count < candidate.required_samples:
            return False
        if signal.regime != "campaign":
            return False

        side = candidate.side
        campaign_ready = signal.long_ready if side == "LONG" else signal.short_ready
        if not campaign_ready:
            return False

        warmup_seconds = max(8.0, self.confirm_impulse_window_seconds * 0.5)
        if (time.time() - self.started_at_ts) < warmup_seconds:
            return False
        if signal.sample_count < max(self.initiative_persistence_windows + 2, 6):
            return False

        entry_score = signal.long_score if side == "LONG" else signal.short_score
        opposing_score = signal.short_score if side == "LONG" else signal.long_score
        score_edge = entry_score - opposing_score
        directional_breakout = self._directional_breakout_distance(signal, side)
        directional_confirm = self._directional_confirm_impulse(signal, side)
        directional_flow = self._directional_flow(signal, side)
        directional_book = self._directional_book(signal, side)
        tick_bps = self._tick_bps(signal.mid)
        effective_breakout_buffer_bps = self._effective_breakout_buffer_bps(signal.mid)
        instant_breakout_floor_bps = effective_breakout_buffer_bps + max(
            tick_bps * 0.5,
            self._effective_probe_breakout_slack_bps(signal.mid) * 0.25,
        )
        instant_spread_cap_bps = min(
            self.max_spread_bps,
            max(self.probe_max_spread_bps * 1.25, tick_bps * 3.0),
        )

        if signal.spread_bps > instant_spread_cap_bps:
            return False
        if entry_score < max(self.campaign_score_min + 8.0, self.instant_entry_score_min * 0.95):
            return False
        if score_edge < max(self.campaign_score_edge_min + 10.0, 40.0):
            return False
        if directional_confirm < signal.dynamic_confirm_threshold_bps:
            return False
        if directional_breakout < instant_breakout_floor_bps:
            return False
        if directional_flow < self.flow_imbalance_min:
            return False
        if directional_book < self.book_imbalance_min:
            return False
        return True

    def _maybe_enter(self, signal: MidSignalSnapshot) -> None:
        if signal.regime not in {"tension", "campaign"}:
            self.pending_candidate = None
            return
        candidate = self._update_candidate(signal)
        if candidate is None:
            return

        first_sample = candidate.first_seen_exchange_time_ms == signal.sample_exchange_time_ms
        first_sample_campaign_ready = False
        if first_sample and not self.startup_state_entry:
            first_sample_campaign_ready = self._allow_campaign_first_sample_entry(signal, candidate)
            if not first_sample_campaign_ready:
                return
        if candidate.count < candidate.required_samples:
            return
        if not self._entry_allowed(time.time()):
            return

        entry_score = signal.long_score if candidate.side == "LONG" else signal.short_score
        entry_profile = candidate.profile
        if candidate.profile == "campaign":
            # Oil entries should earn campaign sizing; the first fill seeds as a
            # cheap probe and only promotes once the break actually holds.
            entry_profile = "probe"
            self._write_event(
                "campaign_seeded_as_probe",
                side=candidate.side,
                candidate_profile=candidate.profile,
                entry_profile=entry_profile,
                candidate_count=candidate.count,
                required_samples=candidate.required_samples,
                **signal.to_dict(),
            )

        reason_profile = entry_profile
        if candidate.profile != entry_profile:
            reason_profile = f"{entry_profile} seed_from_{candidate.profile}"
        reason = (
            f"{candidate.side} {reason_profile} "
            f"fast_bps={signal.fast_impulse_bps:.2f} "
            f"confirm_bps={signal.confirm_impulse_bps:.2f} "
            f"flow={signal.flow_imbalance:.3f} "
            f"book={signal.book_imbalance:.3f} "
            f"score={entry_score:.2f}"
        )
        if first_sample_campaign_ready:
            self._write_event(
                "campaign_first_sample_ready",
                side=candidate.side,
                entry_profile=candidate.profile,
                candidate_count=candidate.count,
                required_samples=candidate.required_samples,
                score=entry_score,
                score_edge=entry_score
                - (signal.short_score if candidate.side == "LONG" else signal.long_score),
                breakout_distance_bps=self._directional_breakout_distance(signal, candidate.side),
                **signal.to_dict(),
            )
        self.signal_confirmed_count += 1
        self._write_event(
            "signal_confirmed",
            side=candidate.side,
            candidate_profile=candidate.profile,
            entry_profile=entry_profile,
            required_samples=candidate.required_samples,
            candidate_count=candidate.count,
            **signal.to_dict(),
        )
        self._open_position(signal, candidate.side, reason, entry_profile)
        if self.position is not None:
            size_fields = self._entry_size_logging_fields(
                signal,
                side=self.position.side,
                profile=self.position.entry_profile,
                notional=self.position.notional,
            )
            self._write_feature_row(
                {
                    "row_type": "entry",
                    "timestamp_utc": _utc_iso(),
                    "asset": self.asset,
                    "trade_id": self.position.trade_id,
                    "side": self.position.side,
                    "candidate_profile": candidate.profile,
                    "entry_profile": self.position.entry_profile,
                    "entry_price": self.position.entry_price,
                    "notional": self.position.notional,
                    "required_margin": self.position.required_margin,
                    "size_multiplier": self.position.size_multiplier,
                    "recent_traded_notional": self._recent_traded_notional(signal),
                    "near_side_depth_notional": self._near_side_depth_notional(signal, self.position.side),
                    "depth_liquidity_cap_notional": self._depth_liquidity_cap_notional(
                        signal,
                        self.position.side,
                        profile=self.position.entry_profile,
                        add_on=False,
                    ),
                    "trade_participation_cap_notional": self._trade_participation_cap_notional(
                        signal,
                        profile=self.position.entry_profile,
                        add_on=False,
                    ),
                    "trail_distance_bps": self.position.trail_distance_bps,
                    "trail_arm_bps": self.position.trail_arm_bps,
                    "stop_distance_bps": self.position.stop_distance_bps,
                    "score": entry_score,
                    "score_edge": entry_score
                    - (signal.short_score if candidate.side == "LONG" else signal.long_score),
                    "breakout_distance_bps": self._directional_breakout_distance(signal, candidate.side),
                    **size_fields,
                }
            )

    def _state_label(self, snapshot: Optional[MidSignalSnapshot]) -> str:
        if snapshot is None:
            return "arm"
        if self.position is not None:
            if self._runner_live():
                return "runner"
            if self._campaign_live():
                return "campaign"
            return "probe"
        if self.pending_candidate is not None:
            return "arm"
        return "arm" if snapshot.regime in {"tension", "campaign"} else "flat"

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
        spread_bps: float,
        spread_ok: bool,
        extreme_blocked: bool,
        full_ready: bool,
        reference_price: float | None = None,
        probe_depth_confirmed: bool = False,
        probe_initiative_confirmed: bool = False,
    ) -> Optional[str]:
        del extreme_blocked
        direction = 1.0 if side == "LONG" else -1.0
        score_edge = score - opposing_score
        flow = direction * flow_imbalance
        book = direction * book_imbalance
        directional_fast = direction * fast_impulse_bps
        directional_confirm = direction * confirm_impulse_bps
        side_probe_failures = self.long_probe_failures if side == "LONG" else self.short_probe_failures
        self._prune_probe_failures(side_probe_failures)
        recent_side_probe_failures = len(side_probe_failures)
        if (
            full_ready
            and trade_count_ok
            and spread_ok
            and score >= self.campaign_score_min
            and score_edge >= self.campaign_score_edge_min
        ):
            return "campaign"
        effective_probe_slack_bps = self.probe_breakout_slack_bps
        effective_breakout_buffer_bps = self.breakout_buffer_bps
        if reference_price is not None and reference_price > 0:
            effective_probe_slack_bps = self._effective_probe_breakout_slack_bps(reference_price)
            effective_breakout_buffer_bps = self._effective_breakout_buffer_bps(reference_price)
        minimum_probe_breakout_bps = effective_breakout_buffer_bps * self.probe_min_breakout_fraction
        if breakout_distance_bps < minimum_probe_breakout_bps:
            return None
        probe_boundary = -effective_probe_slack_bps
        partial_breakout = breakout_distance_bps < effective_breakout_buffer_bps
        required_probe_fast = fast_threshold_bps * self.probe_fast_threshold_ratio
        required_probe_confirm = confirm_threshold_bps * self.probe_confirm_threshold_ratio
        required_score_edge = self.probe_score_edge_min
        required_flow = self.flow_imbalance_min * self.probe_flow_multiplier
        required_book = self.book_imbalance_min * self.probe_book_multiplier
        if partial_breakout:
            # Partial breaks still get probe treatment, but they must show both
            # confirmation modes and a stronger score/impulse profile than a full break.
            if not (probe_depth_confirmed and probe_initiative_confirmed):
                return None
            required_probe_fast = max(required_probe_fast, fast_threshold_bps * 0.72)
            required_probe_confirm = max(required_probe_confirm, confirm_threshold_bps * 0.80)
            required_score_edge += 2.0
        if recent_side_probe_failures > 0:
            # After a same-side probe loss, stop paying for another "almost there"
            # entry and force the next probe to be a cleaner breakout.
            if partial_breakout:
                return None
            if side == "LONG":
                required_probe_fast = max(required_probe_fast, fast_threshold_bps)
                required_probe_confirm = max(required_probe_confirm, confirm_threshold_bps)
                required_score_edge += 4.0
                required_flow = max(required_flow, self.flow_imbalance_min * 1.15)
                required_book = max(required_book, self.book_imbalance_min * 1.35)
        if (
            self._global_probe_allowed()
            and self._probe_allowed(side)
            and trade_count_ok
            and spread_ok
            and spread_bps <= self.probe_max_spread_bps
            and score >= self.probe_score_min
            and score_edge >= required_score_edge
            and directional_fast >= required_probe_fast
            and directional_confirm >= required_probe_confirm
            and breakout_distance_bps >= probe_boundary
            and flow >= required_flow
            and book >= required_book
        ):
            return "probe"
        return None

    def _write_feature_row(self, payload: Dict[str, object]) -> None:
        if self.features_jsonl_path is None:
            return
        try:
            with self.features_jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except Exception:
            import logging

            logging.exception("Failed writing oil campaign feature JSONL")

    def _build_signal_snapshot(self, market: MarketSnapshot) -> Optional[MidSignalSnapshot]:
        quote = market.quote
        now_ms = int(time.time() * 1000)
        quote_age_ms = max(0, now_ms - quote.exchange_time_ms)
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
        self._append_flow_window(flow["imbalance"])

        breakout_high = max(prior_prices)
        breakout_low = min(prior_prices)
        effective_breakout_buffer_bps = self._effective_breakout_buffer_bps(quote.mid)
        breakout_buffer_up = breakout_high * (1.0 + (effective_breakout_buffer_bps / 10_000.0))
        breakout_buffer_dn = breakout_low * (1.0 - (effective_breakout_buffer_bps / 10_000.0))
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
        long_depth_vacuum = self._depth_vacuum_ok(side="LONG", bid_depth=bid_depth, ask_depth=ask_depth)
        long_initiative_persistent = self._initiative_persistent("LONG")

        short_fast_ok = fast_impulse_bps <= -dynamic_fast_threshold_bps
        short_confirm_ok = confirm_impulse_bps <= -dynamic_confirm_threshold_bps
        short_breakout_ok = quote.mid <= breakout_buffer_dn
        short_flow_ok = flow["imbalance"] <= -self.flow_imbalance_min
        short_book_ok = book_imbalance <= -self.book_imbalance_min
        short_depth_vacuum = self._depth_vacuum_ok(side="SHORT", bid_depth=bid_depth, ask_depth=ask_depth)
        short_initiative_persistent = self._initiative_persistent("SHORT")

        def score_side(direction: int, breakout_distance_bps: float) -> float:
            fast_component = self._normalize_component(direction * fast_impulse_bps, dynamic_fast_threshold_bps)
            confirm_component = self._normalize_component(direction * confirm_impulse_bps, dynamic_confirm_threshold_bps)
            breakout_component = self._normalize_component(
                direction * breakout_distance_bps,
                max(effective_breakout_buffer_bps, self._tick_bps(quote.mid)),
            )
            flow_component = self._normalize_component(direction * flow["imbalance"], max(self.flow_imbalance_min, 1e-6))
            book_component = self._normalize_component(direction * book_imbalance, max(self.book_imbalance_min, 1e-6))
            trade_component = self._normalize_component(float(flow["count"]), float(max(self.min_trade_count, 1)))
            spread_component = _clamp(1.0 - _safe_ratio(quote.spread_bps, max(self.max_spread_bps, 0.1)), 0.0, 1.0)
            raw = (
                0.12 * fast_component
                + 0.24 * confirm_component
                + 0.24 * breakout_component
                + 0.16 * flow_component
                + 0.10 * book_component
                + 0.05 * trade_component
                + 0.04 * spread_component
            )
            if direction == 1:
                if long_depth_vacuum:
                    raw += 0.03
                if long_initiative_persistent:
                    raw += 0.06
            else:
                if short_depth_vacuum:
                    raw += 0.03
                if short_initiative_persistent:
                    raw += 0.06
            if not spread_ok:
                raw *= 0.15
            if abs(direction * confirm_impulse_bps) >= self.event_impulse_bps:
                raw += 0.08
            return round(_clamp(raw, 0.0, 1.0) * 100.0, 2)

        long_score = score_side(1, breakout_distance_long_bps)
        short_score = score_side(-1, breakout_distance_short_bps)

        long_probe_confirmed = long_depth_vacuum or long_initiative_persistent
        short_probe_confirmed = short_depth_vacuum or short_initiative_persistent
        long_campaign_confirmed = long_depth_vacuum and long_initiative_persistent
        short_campaign_confirmed = short_depth_vacuum and short_initiative_persistent

        long_setup = (
            spread_ok
            and not loss_risk_blocked
            and trade_count_ok
            and long_score >= self.probe_score_min
            and long_probe_confirmed
        )
        short_setup = (
            spread_ok
            and not loss_risk_blocked
            and trade_count_ok
            and short_score >= self.probe_score_min
            and short_probe_confirmed
        )
        long_ready = (
            long_setup
            and long_fast_ok
            and long_confirm_ok
            and long_breakout_ok
            and long_flow_ok
            and long_book_ok
            and long_campaign_confirmed
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
            and short_campaign_confirmed
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
            spread_bps=quote.spread_bps,
            spread_ok=spread_ok and not loss_risk_blocked and long_probe_confirmed,
            extreme_blocked=False,
            full_ready=long_ready,
            reference_price=quote.mid,
            probe_depth_confirmed=long_depth_vacuum,
            probe_initiative_confirmed=long_initiative_persistent,
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
            spread_bps=quote.spread_bps,
            spread_ok=spread_ok and not loss_risk_blocked and short_probe_confirmed,
            extreme_blocked=False,
            full_ready=short_ready,
            reference_price=quote.mid,
            probe_depth_confirmed=short_depth_vacuum,
            probe_initiative_confirmed=short_initiative_persistent,
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

    def _size_multiplier(self, *, signal: MidSignalSnapshot, side: str, profile: str) -> float:
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
        effective_probe_slack_bps = self._effective_probe_breakout_slack_bps(signal.mid)
        effective_breakout_buffer_bps = self._effective_breakout_buffer_bps(signal.mid)
        breakout_component = _clamp(
            _safe_ratio(
                self._directional_breakout_distance(signal, side) + effective_probe_slack_bps,
                max(effective_breakout_buffer_bps + effective_probe_slack_bps, self._tick_bps(signal.mid)),
            ),
            0.0,
            1.0,
        )
        flow_component = _clamp(abs(signal.flow_imbalance), 0.0, 1.0)
        profile_boost = self.probe_size_boost if profile == "probe" else self.campaign_size_boost
        if profile == "probe":
            profile_boost *= 0.60
        event_boost = 0.10 if signal.regime == "campaign" else 0.0
        multiplier = 1.0 + profile_boost + event_boost + (
            self.score_notional_boost
            * ((0.44 * score_component) + (0.26 * breakout_component) + (0.20 * flow_component) + (0.10 * spread_component))
        )
        return round(min(multiplier, 1.0 + profile_boost + event_boost + self.score_notional_boost), 6)

    def _campaign_trail_distance_bps(
        self,
        signal: MidSignalSnapshot,
        *,
        mfe_bps: float,
    ) -> tuple[float, str]:
        assert self.position is not None
        base_distance = max(self._compute_trail_distance_bps(signal), self.position.stop_distance_bps * 0.85)
        if not self._campaign_live():
            return max(base_distance, self.position.trail_distance_bps), "probe"

        stop_r = max(self.position.stop_distance_bps, 1e-9)
        if mfe_bps < (stop_r * self.campaign_wide_trail_until_r):
            return (
                max(
                    self.position.trail_distance_bps,
                    base_distance * self.campaign_loose_trail_multiplier,
                    stop_r * 1.05,
                ),
                "wide",
            )
        if mfe_bps < (stop_r * self.campaign_tighten_after_r):
            return (
                max(
                    base_distance,
                    stop_r * 0.90,
                ),
                "hold",
            )
        return (
            max(
                base_distance * self.campaign_tight_trail_multiplier,
                stop_r * 0.65,
            ),
            "tight",
        )

    def _maybe_promote_probe_to_campaign(
        self,
        signal: MidSignalSnapshot,
        *,
        current_realized_bps: float,
        mfe_bps: float,
        hold_seconds: float,
    ) -> bool:
        if self.position is None or self.position.entry_profile != "probe":
            return False
        if hold_seconds < self.probe_promotion_min_hold_seconds:
            return False
        metrics = self._capture_position_hold_quality(
            signal,
            hold_seconds=hold_seconds,
            current_realized_bps=current_realized_bps,
            mfe_bps=mfe_bps,
        )
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
        promotion_breakout_floor_bps = max(
            self._effective_breakout_buffer_bps(signal.mid) * max(self.probe_min_breakout_fraction, 0.50),
            self._tick_bps(signal.mid),
        )
        if directional_breakout < promotion_breakout_floor_bps:
            return False
        reclaim_floor_bps = max(self._effective_probe_breakout_slack_bps(signal.mid) * 0.5, self._tick_bps(signal.mid))
        if directional_breakout < -reclaim_floor_bps:
            return False
        if directional_flow < self.flow_imbalance_min or directional_book < self.book_imbalance_min:
            return False
        if not self._depth_vacuum_ok(side=side, bid_depth=signal.bid_depth, ask_depth=signal.ask_depth):
            return False
        if not self._initiative_persistent(side):
            return False
        if max(current_realized_bps, mfe_bps) < (self.position.stop_distance_bps * self.probe_promotion_mfe_r):
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
        promotion_timestamp = _utc_iso()
        self.position.last_metrics.update(
            {
                "promoted_to_campaign": True,
                "promotion_timestamp_utc": promotion_timestamp,
                "promotion_hold_seconds": hold_seconds,
                "promotion_mfe_bps": mfe_bps,
                "promotion_realized_bps": current_realized_bps,
                "promotion_breakout_distance_bps": directional_breakout,
                "promotion_score": score,
                "promotion_score_edge": score - opposing_score,
            }
        )
        self._write_feature_row(
            {
                "row_type": "promotion",
                "timestamp_utc": promotion_timestamp,
                "asset": self.asset,
                "trade_id": self.position.trade_id,
                "side": self.position.side,
                "from_profile": "probe",
                "to_profile": "campaign",
                "hold_seconds": hold_seconds,
                "mfe_bps": mfe_bps,
                "realized_bps": current_realized_bps,
                "breakout_distance_bps": directional_breakout,
                "score": score,
                "score_edge": score - opposing_score,
                "recent_traded_notional": metrics["recent_traded_notional"],
                "directional_flow": metrics["directional_flow"],
                "directional_book": metrics["directional_book"],
                "trail_arm_bps": self.position.trail_arm_bps,
                "trail_distance_bps": self.position.trail_distance_bps,
            }
        )
        self._write_event(
            "probe_promoted_to_campaign",
            trade_id=self.position.trade_id,
            side=side,
            score=score,
            opposing_score=opposing_score,
            current_realized_bps=current_realized_bps,
            mfe_bps=mfe_bps,
            hold_seconds=hold_seconds,
            directional_breakout_bps=directional_breakout,
            promotion_breakout_floor_bps=promotion_breakout_floor_bps,
            directional_confirm_bps=directional_confirm,
            directional_flow=directional_flow,
            directional_book=directional_book,
            trail_arm_bps=self.position.trail_arm_bps,
            failure_horizon_seconds=self.position.failure_horizon_seconds,
            failure_min_followthrough_bps=self.position.failure_min_followthrough_bps,
        )
        return True

    def _add_on_ready(self, signal: MidSignalSnapshot) -> bool:
        if not super()._add_on_ready(signal):
            return False
        assert self.position is not None
        breakout_distance = self._directional_breakout_distance(signal, self.position.side)
        if self.position.add_on_count == 0 and self.position.locked_stop_price is None:
            first_add_extension = max(
                self._effective_add_on_extension_bps(signal.mid),
                self._effective_breakout_buffer_bps(signal.mid) + max(self._effective_probe_breakout_slack_bps(signal.mid) * 0.35, self._tick_bps(signal.mid)),
            )
            if breakout_distance < first_add_extension:
                return False
        else:
            breakout_hold_floor = -max(self._effective_probe_breakout_slack_bps(signal.mid) * 0.35, self._tick_bps(signal.mid))
            if breakout_distance < breakout_hold_floor:
                return False
        return self._depth_vacuum_ok(side=self.position.side, bid_depth=signal.bid_depth, ask_depth=signal.ask_depth) or self._initiative_persistent(self.position.side)

    def _decay_reduction_ready(self, signal: MidSignalSnapshot, *, hold_seconds: float, mfe_bps: float) -> bool:
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
            self._effective_breakout_reclaim_bps(signal.mid),
            self._effective_probe_breakout_slack_bps(signal.mid) * 0.25,
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
        fail_buffer_bps = self._effective_failed_breakout_bps(signal.mid) * (1.5 if live_campaign else 0.65)
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

    def _break_reclaim_veto(self, signal: MidSignalSnapshot, *, hold_seconds: float) -> bool:
        if self.position is None or hold_seconds > self.reclaim_veto_window_seconds:
            return False
        breakout_distance = self._directional_breakout_distance(signal, self.position.side)
        reclaim_floor = max(self._effective_breakout_reclaim_bps(signal.mid), self._tick_bps(signal.mid))
        if breakout_distance >= -reclaim_floor:
            return False
        directional_flow = self._directional_flow(signal, self.position.side)
        directional_book = self._directional_book(signal, self.position.side)
        if directional_flow >= self.flow_imbalance_min and directional_book >= self.book_imbalance_min:
            return False
        return True

    def _candidate_seed_breakout_distance(self, candidate: CandidateState, signal: MidSignalSnapshot) -> float:
        seed_key = "_probe_seed_breakout_distance_bps"
        if seed_key in candidate.features:
            return float(candidate.features[seed_key])
        return self._directional_breakout_distance(signal, candidate.side)

    def _update_candidate(self, signal: MidSignalSnapshot) -> CandidateState | None:
        previous_candidate = self.pending_candidate
        previous_seed_breakout_distance: float | None = None
        if previous_candidate is not None and previous_candidate.profile == "probe":
            seed_value = previous_candidate.features.get("_probe_seed_breakout_distance_bps")
            if seed_value is not None:
                previous_seed_breakout_distance = float(seed_value)
        candidate = super()._update_candidate(signal)
        if candidate is None:
            return None
        is_new_candidate = candidate.count == 1 and candidate.first_seen_exchange_time_ms == signal.sample_exchange_time_ms
        directional_breakout = self._directional_breakout_distance(signal, candidate.side)
        if candidate.profile == "probe":
            if previous_seed_breakout_distance is not None:
                seed_breakout_distance = previous_seed_breakout_distance
            else:
                seed_breakout_distance = self._candidate_seed_breakout_distance(
                    previous_candidate if previous_candidate is not None else candidate,
                    signal,
                )
            candidate.features["_probe_seed_breakout_distance_bps"] = seed_breakout_distance
            if seed_breakout_distance < 0.0:
                candidate.required_samples = max(candidate.required_samples, self.entry_confirmation_samples + 1)
            # Probe candidates must actually hold the broken level on the
            # confirming samples; otherwise we reset and wait for a cleaner break.
            if not is_new_candidate and directional_breakout < 0.0:
                self.pending_candidate = None
                self._write_event(
                    "probe_hold_reset",
                    side=candidate.side,
                    entry_profile=candidate.profile,
                    breakout_distance_bps=directional_breakout,
                    seed_breakout_distance_bps=seed_breakout_distance,
                    candidate_count=candidate.count,
                    required_samples=candidate.required_samples,
                    **signal.to_dict(),
                )
                return None
        if candidate.count == 1 and candidate.first_seen_exchange_time_ms == signal.sample_exchange_time_ms:
            recent_traded_notional = self._recent_traded_notional(signal)
            self._write_feature_row(
                {
                    "row_type": "candidate",
                    "timestamp_utc": _utc_iso(),
                    "asset": self.asset,
                    "side": candidate.side,
                    "entry_profile": candidate.profile,
                    "mid": signal.mid,
                    "spread_bps": signal.spread_bps,
                    "quote_age_ms": signal.quote_age_ms,
                    "trade_count": signal.trade_count,
                    "trade_rate_per_second": signal.trade_rate_per_second,
                    "fast_impulse_bps": signal.fast_impulse_bps,
                    "confirm_impulse_bps": signal.confirm_impulse_bps,
                    "breakout_distance_bps": self._directional_breakout_distance(signal, candidate.side),
                    "flow_imbalance": signal.flow_imbalance,
                    "book_imbalance": signal.book_imbalance,
                    "recent_vol_bps": signal.recent_vol_bps,
                    "long_score": signal.long_score,
                    "short_score": signal.short_score,
                    "recent_traded_notional": recent_traded_notional,
                    "near_side_depth_notional": self._near_side_depth_notional(signal, candidate.side),
                    "depth_liquidity_cap_notional": self._depth_liquidity_cap_notional(
                        signal,
                        candidate.side,
                        profile=candidate.profile,
                        add_on=False,
                    ),
                    "trade_participation_cap_notional": self._trade_participation_cap_notional(
                        signal,
                        profile=candidate.profile,
                        add_on=False,
                    ),
                    "tick_bps": self._tick_bps(signal.mid),
                    "effective_breakout_buffer_bps": self._effective_breakout_buffer_bps(signal.mid),
                    "effective_probe_breakout_slack_bps": self._effective_probe_breakout_slack_bps(signal.mid),
                    "depth_vacuum_ok": self._depth_vacuum_ok(side=candidate.side, bid_depth=signal.bid_depth, ask_depth=signal.ask_depth),
                    "initiative_persistent": self._initiative_persistent(candidate.side),
                }
            )
        return candidate

    def _close_position(
        self,
        signal: MidSignalSnapshot,
        exit_reason: str,
        trigger_price: float,
        mfe_bps: float,
        mae_bps: float,
    ) -> None:
        label_payload: Dict[str, object] | None = None
        if self.position is not None:
            if self.position.entry_profile == "probe" and exit_reason == "break_reclaim_veto":
                self._record_probe_failure(self.position.side)
            hold_seconds = max(0.0, time.time() - self.position.opened_at)
            current_realized_bps = self._current_realized_bps(trigger_price)
            metrics = self._capture_position_hold_quality(
                signal,
                hold_seconds=hold_seconds,
                current_realized_bps=current_realized_bps,
                mfe_bps=mfe_bps,
                mae_bps=mae_bps,
            )
            label_payload = {
                "row_type": "label",
                "timestamp_utc": _utc_iso(),
                "asset": self.asset,
                "trade_id": self.position.trade_id,
                "side": self.position.side,
                "entry_profile": self.position.entry_profile,
                "exit_reason": exit_reason,
                "hold_seconds": hold_seconds,
                "mfe_bps": mfe_bps,
                "mae_bps": mae_bps,
                "realized_bps": current_realized_bps,
                "add_on_count": self.position.add_on_count,
                "runner_live": self._runner_live(),
                "exit_bucket": self._exit_bucket(exit_reason, hold_seconds=hold_seconds),
                "reclaim_exit_seconds": hold_seconds if exit_reason == "break_reclaim_veto" else None,
                "false_break": hold_seconds <= 10.0 and exit_reason in {
                    "failed_followthrough",
                    "breakout_failure",
                    "break_reclaim_veto",
                    "flow_flip_exit",
                    "hard_reversal_exit",
                    "initial_stop_hit",
                },
                "campaign_success": mfe_bps >= (self.position.stop_distance_bps * 1.0),
                "add_on_success": self.position.add_on_count > 0 and mfe_bps >= (self.position.stop_distance_bps * 0.6),
                "promoted_to_campaign": bool(self.position.last_metrics.get("promoted_to_campaign")),
                "promotion_hold_seconds": self.position.last_metrics.get("promotion_hold_seconds"),
                "promotion_mfe_bps": self.position.last_metrics.get("promotion_mfe_bps"),
                "promotion_realized_bps": self.position.last_metrics.get("promotion_realized_bps"),
                "promotion_breakout_distance_bps": self.position.last_metrics.get("promotion_breakout_distance_bps"),
                "promotion_score_edge": self.position.last_metrics.get("promotion_score_edge"),
                "last_breakout_distance_bps": metrics["breakout_distance_bps"],
                "last_score_edge": metrics["score_edge"],
                "last_directional_flow": metrics["directional_flow"],
                "last_directional_book": metrics["directional_book"],
                "last_recent_traded_notional": metrics["recent_traded_notional"],
                "hold_1s_realized_bps": self.position.last_metrics.get("hold_1s_realized_bps"),
                "hold_1s_breakout_distance_bps": self.position.last_metrics.get("hold_1s_breakout_distance_bps"),
                "hold_1s_score_edge": self.position.last_metrics.get("hold_1s_score_edge"),
                "hold_1s_directional_flow": self.position.last_metrics.get("hold_1s_directional_flow"),
                "hold_1s_directional_book": self.position.last_metrics.get("hold_1s_directional_book"),
                "hold_1s_recent_traded_notional": self.position.last_metrics.get("hold_1s_recent_traded_notional"),
                "hold_2s_realized_bps": self.position.last_metrics.get("hold_2s_realized_bps"),
                "hold_2s_breakout_distance_bps": self.position.last_metrics.get("hold_2s_breakout_distance_bps"),
                "hold_2s_score_edge": self.position.last_metrics.get("hold_2s_score_edge"),
                "hold_2s_directional_flow": self.position.last_metrics.get("hold_2s_directional_flow"),
                "hold_2s_directional_book": self.position.last_metrics.get("hold_2s_directional_book"),
                "hold_2s_recent_traded_notional": self.position.last_metrics.get("hold_2s_recent_traded_notional"),
            }
        super()._close_position(signal, exit_reason, trigger_price, mfe_bps, mae_bps)
        if exit_reason == "break_reclaim_veto":
            self.last_break_reclaim_exit_ts = time.time()
        if label_payload is not None:
            self._write_feature_row(label_payload)

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
            hold_seconds=hold_seconds,
        )

        if (
            hold_seconds >= self.position.failure_horizon_seconds
            and mfe_bps < self.position.failure_min_followthrough_bps
        ):
            self._close_position(signal, "failed_followthrough", current_exit_price, mfe_bps, mae_bps)
            return
        if self._break_reclaim_veto(signal, hold_seconds=hold_seconds):
            self._close_position(signal, "break_reclaim_veto", current_exit_price, mfe_bps, mae_bps)
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

    def _update_trailing_stop(
        self,
        signal: MidSignalSnapshot,
        current_exit_price: float,
        mfe_bps: float,
    ) -> None:
        assert self.position is not None
        trail_distance_bps, trail_stage = self._campaign_trail_distance_bps(signal, mfe_bps=mfe_bps)
        previous_trail_distance_bps = self.position.trail_distance_bps
        self.position.trail_distance_bps = trail_distance_bps
        previous_locked_stop = self.position.locked_stop_price

        super()._update_trailing_stop(signal, current_exit_price, mfe_bps)

        locked_stop = self.position.locked_stop_price
        if (
            abs(previous_trail_distance_bps - trail_distance_bps) >= 0.5
            or previous_locked_stop != locked_stop
        ):
            self._write_event(
                "campaign_trail_profile_updated",
                trade_id=self.position.trade_id,
                side=self.position.side,
                trail_stage=trail_stage,
                trail_distance_bps=trail_distance_bps,
                previous_trail_distance_bps=previous_trail_distance_bps,
                mfe_bps=mfe_bps,
                locked_stop=locked_stop,
                active_stop=self.position.active_stop_price,
            )

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
        campaign_trail_stage: Optional[str] = None
        if self.position is not None:
            position_side = self.position.side
            trade_id = self.position.trade_id
            active_stop = self.position.active_stop_price
            locked_stop = self.position.locked_stop_price
            entry_price = self.position.entry_price
            hold_seconds = max(0.0, time.time() - self.position.opened_at)
            trail_armed = self.position.trailing_armed
            if self._runner_live():
                position_phase = "runner"
            else:
                position_phase = "campaign" if self._campaign_live() else "probe"
            current_exit_price = self._current_exit_price(snapshot)
            _, campaign_trail_stage = self._campaign_trail_distance_bps(
                snapshot,
                mfe_bps=self._position_mfe_bps(current_exit_price)[0],
            )
            direction = 1.0 if self.position.side == "LONG" else -1.0
            gross = (current_exit_price - self.position.entry_price) * self.position.size * direction
            current_unrealized = self.position.realized_pnl_locked + gross - self.position.entry_fee_paid
            self._capture_position_hold_quality(snapshot, hold_seconds=hold_seconds)

        payload = {
            "timestamp_utc": _utc_iso(),
            "asset": self.asset,
            **snapshot.to_dict(),
            "state": self._state_label(snapshot),
            "strategy_name": APP_NAME,
            "recent_traded_notional": self._recent_traded_notional(snapshot),
            "tick_size": self.tick_size,
            "tick_bps": self._tick_bps(snapshot.mid),
            "effective_breakout_buffer_bps": self._effective_breakout_buffer_bps(snapshot.mid),
            "effective_probe_breakout_slack_bps": self._effective_probe_breakout_slack_bps(snapshot.mid),
            "effective_breakout_reclaim_bps": self._effective_breakout_reclaim_bps(snapshot.mid),
            "effective_add_on_extension_bps": self._effective_add_on_extension_bps(snapshot.mid),
            "effective_failed_breakout_bps": self._effective_failed_breakout_bps(snapshot.mid),
            "depth_vacuum_long": self._depth_vacuum_ok(side="LONG", bid_depth=snapshot.bid_depth, ask_depth=snapshot.ask_depth),
            "depth_vacuum_short": self._depth_vacuum_ok(side="SHORT", bid_depth=snapshot.bid_depth, ask_depth=snapshot.ask_depth),
            "initiative_persistent_long": self._initiative_persistent("LONG"),
            "initiative_persistent_short": self._initiative_persistent("SHORT"),
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
            "campaign_trail_stage": campaign_trail_stage,
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
            "runner_live": self._runner_live(),
            "reclaim_cooldown_active": self.last_break_reclaim_exit_ts is not None and (time.time() - self.last_break_reclaim_exit_ts) < self.reclaim_cooldown_seconds,
            "position_cap_fraction": (_safe_ratio(self._total_notional_cap(), self.max_notional) if self.max_notional > 0 else None),
        }
        try:
            with self.samples_jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except Exception:
            import logging

            logging.exception("Failed writing oil campaign sample JSONL")

#!/usr/bin/env python3
"""Paper-trading oil campaign runner built on the initiator-follower base."""

from __future__ import annotations

import argparse
import logging
import sys

import initiator_follower_jg_thesis_bot as base_bot

from oil_campaign_momentum_v2_core import OilCampaignMomentumV2Bot


DEFAULT_TICK_SIZE = 0.01
DEFAULT_BREAKOUT_BUFFER_TICKS = 2.0
DEFAULT_PROBE_BREAKOUT_SLACK_TICKS = 1.0
DEFAULT_PROBE_MIN_BREAKOUT_FRACTION = 0.55
DEFAULT_PROBE_MAX_SPREAD_BPS = 2.5
DEFAULT_BREAKOUT_RECLAIM_TICKS = 2.0
DEFAULT_ADD_ON_EXTENSION_TICKS = 2.0
DEFAULT_FAILED_BREAKOUT_TICKS = 3.0
DEFAULT_DEPTH_VACUUM_NEAR_RATIO_MAX = 0.72
DEFAULT_INITIATIVE_PERSISTENCE_WINDOWS = 3
DEFAULT_RECLAIM_VETO_WINDOW_SECONDS = 8.0
DEFAULT_RECLAIM_COOLDOWN_SECONDS = 90.0
DEFAULT_PROBE_PROMOTION_MFE_R = 0.12
DEFAULT_PROBE_PROMOTION_MIN_HOLD_SECONDS = 1.0
DEFAULT_PROBE_TRADE_PARTICIPATION_CAP = 2.0
DEFAULT_CAMPAIGN_TRADE_PARTICIPATION_CAP = 6.0
DEFAULT_ADD_ON_TRADE_PARTICIPATION_CAP = 4.0
DEFAULT_CAMPAIGN_WIDE_TRAIL_UNTIL_R = 2.0
DEFAULT_CAMPAIGN_TIGHTEN_AFTER_R = 4.0
DEFAULT_CAMPAIGN_LOOSE_TRAIL_MULTIPLIER = 1.25
DEFAULT_CAMPAIGN_TIGHT_TRAIL_MULTIPLIER = 0.85
DEFAULT_FEATURES_JSONL = "logs/oil_campaign_features.jsonl"


def parse_args() -> argparse.Namespace:
    extra = argparse.ArgumentParser(add_help=False)
    extra.add_argument("--tick-size", type=float, default=DEFAULT_TICK_SIZE)
    extra.add_argument("--breakout-buffer-ticks", type=float, default=DEFAULT_BREAKOUT_BUFFER_TICKS)
    extra.add_argument("--probe-breakout-slack-ticks", type=float, default=DEFAULT_PROBE_BREAKOUT_SLACK_TICKS)
    extra.add_argument("--probe-min-breakout-fraction", type=float, default=DEFAULT_PROBE_MIN_BREAKOUT_FRACTION)
    extra.add_argument("--probe-max-spread-bps", type=float, default=DEFAULT_PROBE_MAX_SPREAD_BPS)
    extra.add_argument("--breakout-reclaim-ticks", type=float, default=DEFAULT_BREAKOUT_RECLAIM_TICKS)
    extra.add_argument("--add-on-extension-ticks", type=float, default=DEFAULT_ADD_ON_EXTENSION_TICKS)
    extra.add_argument("--failed-breakout-ticks", type=float, default=DEFAULT_FAILED_BREAKOUT_TICKS)
    extra.add_argument("--depth-vacuum-near-ratio-max", type=float, default=DEFAULT_DEPTH_VACUUM_NEAR_RATIO_MAX)
    extra.add_argument("--initiative-persistence-windows", type=int, default=DEFAULT_INITIATIVE_PERSISTENCE_WINDOWS)
    extra.add_argument("--reclaim-veto-window-seconds", type=float, default=DEFAULT_RECLAIM_VETO_WINDOW_SECONDS)
    extra.add_argument("--reclaim-cooldown-seconds", type=float, default=DEFAULT_RECLAIM_COOLDOWN_SECONDS)
    extra.add_argument("--probe-promotion-mfe-r", type=float, default=DEFAULT_PROBE_PROMOTION_MFE_R)
    extra.add_argument("--probe-promotion-min-hold-seconds", type=float, default=DEFAULT_PROBE_PROMOTION_MIN_HOLD_SECONDS)
    extra.add_argument("--probe-trade-participation-cap", type=float, default=DEFAULT_PROBE_TRADE_PARTICIPATION_CAP)
    extra.add_argument("--campaign-trade-participation-cap", type=float, default=DEFAULT_CAMPAIGN_TRADE_PARTICIPATION_CAP)
    extra.add_argument("--add-on-trade-participation-cap", type=float, default=DEFAULT_ADD_ON_TRADE_PARTICIPATION_CAP)
    extra.add_argument("--campaign-wide-trail-until-r", type=float, default=DEFAULT_CAMPAIGN_WIDE_TRAIL_UNTIL_R)
    extra.add_argument("--campaign-tighten-after-r", type=float, default=DEFAULT_CAMPAIGN_TIGHTEN_AFTER_R)
    extra.add_argument("--campaign-loose-trail-multiplier", type=float, default=DEFAULT_CAMPAIGN_LOOSE_TRAIL_MULTIPLIER)
    extra.add_argument("--campaign-tight-trail-multiplier", type=float, default=DEFAULT_CAMPAIGN_TIGHT_TRAIL_MULTIPLIER)
    extra.add_argument("--features-jsonl", default=DEFAULT_FEATURES_JSONL)
    extra_args, remaining = extra.parse_known_args()

    original_argv = sys.argv[:]
    try:
        sys.argv = [sys.argv[0], *remaining]
        base_args = base_bot.parse_args()
    finally:
        sys.argv = original_argv

    merged = vars(base_args)
    merged.update(vars(extra_args))
    return argparse.Namespace(**merged)


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = vars(args).copy()
    dex = config.get("dex")
    asset = str(config.get("asset") or "")
    if not dex and ":" in asset:
        dex = asset.split(":", 1)[0]
    config["dex"] = dex
    config["events_jsonl_path"] = config.pop("events_jsonl")
    config["samples_jsonl_path"] = config.pop("samples_jsonl")
    config["trades_csv_path"] = config.pop("trades_csv")
    config["markouts_jsonl_path"] = config.pop("markouts_jsonl")
    config["features_jsonl_path"] = config.pop("features_jsonl")
    config.pop("log_level", None)

    bot = OilCampaignMomentumV2Bot(**config)
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

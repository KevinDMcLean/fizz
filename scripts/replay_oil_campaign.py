#!/usr/bin/env python3
"""Summarize Oil Campaign Momentum v2 feature rows and label outcomes."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay and score Oil Campaign Momentum v2 feature logs")
    parser.add_argument("--features-jsonl", required=True, help="Path to Oil Campaign Momentum v2 features.jsonl")
    return parser.parse_args()


def read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def pct(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return (part / whole) * 100.0


def main() -> int:
    args = parse_args()
    path = Path(args.features_jsonl)
    if not path.exists():
        raise SystemExit(f"Feature log not found: {path}")

    candidate_count = 0
    label_count = 0
    side_candidates = Counter()
    profile_candidates = Counter()
    exit_reasons = Counter()
    false_breaks = 0
    campaign_successes = 0
    add_on_successes = 0
    runner_labels = 0
    side_false_breaks = Counter()
    side_campaign_success = Counter()
    side_labels = Counter()
    hold_seconds_by_reason: Dict[str, list[float]] = defaultdict(list)
    realized_bps_by_reason: Dict[str, list[float]] = defaultdict(list)

    for row in read_jsonl(path):
        row_type = str(row.get("row_type") or "")
        if row_type == "candidate":
            candidate_count += 1
            side_candidates[str(row.get("side") or "unknown")] += 1
            profile_candidates[str(row.get("entry_profile") or "unknown")] += 1
            continue
        if row_type != "label":
            continue

        label_count += 1
        side = str(row.get("side") or "unknown")
        reason = str(row.get("exit_reason") or "unknown")
        exit_reasons[reason] += 1
        side_labels[side] += 1
        if bool(row.get("false_break")):
            false_breaks += 1
            side_false_breaks[side] += 1
        if bool(row.get("campaign_success")):
            campaign_successes += 1
            side_campaign_success[side] += 1
        if bool(row.get("add_on_success")):
            add_on_successes += 1
        if bool(row.get("runner_live")):
            runner_labels += 1
        try:
            hold_seconds_by_reason[reason].append(float(row.get("hold_seconds") or 0.0))
        except (TypeError, ValueError):
            pass
        try:
            realized_bps_by_reason[reason].append(float(row.get("realized_bps") or 0.0))
        except (TypeError, ValueError):
            pass

    print(f"Feature file: {path}")
    print(f"Candidates: {candidate_count}")
    print(f"Closed labels: {label_count}")
    print(f"False-break rate: {false_breaks}/{label_count} ({pct(false_breaks, label_count):.2f}%)")
    print(f"Campaign success rate: {campaign_successes}/{label_count} ({pct(campaign_successes, label_count):.2f}%)")
    print(f"Add-on success rate: {add_on_successes}/{label_count} ({pct(add_on_successes, label_count):.2f}%)")
    print(f"Runner reached: {runner_labels}/{label_count} ({pct(runner_labels, label_count):.2f}%)")
    print("")
    print("Candidates by side:")
    for side, count in sorted(side_candidates.items()):
        print(f"  {side}: {count}")
    print("Candidates by profile:")
    for profile, count in sorted(profile_candidates.items()):
        print(f"  {profile}: {count}")
    print("Outcomes by side:")
    for side, count in sorted(side_labels.items()):
        print(
            f"  {side}: labels={count} false_breaks={side_false_breaks[side]} "
            f"campaign_success={side_campaign_success[side]}"
        )
    print("Exit reasons:")
    for reason, count in exit_reasons.most_common():
        avg_hold = sum(hold_seconds_by_reason[reason]) / max(len(hold_seconds_by_reason[reason]), 1)
        avg_realized = sum(realized_bps_by_reason[reason]) / max(len(realized_bps_by_reason[reason]), 1)
        print(f"  {reason}: count={count} avg_hold_s={avg_hold:.2f} avg_realized_bps={avg_realized:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

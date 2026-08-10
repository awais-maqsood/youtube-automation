#!/usr/bin/env python3
"""
Single-command entrypoint for Shorts generation.

Runs topic selection, script generation, stock lookup, and local video render
in one terminal window.

Slots:
  All slots → app_safety (BlinkViral "Is It Safe? The TRUTH" series)

Daily volume is gated by DAILY_UPLOAD_CAP (default 2).
"""

import argparse
import sys

# Log lines carry arrows/em dashes; legacy Windows consoles default to cp1252
# and would abort the run on the first such character.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from generate import generate
from content_gen import niche_for_slot
from daily_cap import DailyCapExceeded, assert_under_daily_cap


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", default="morning", choices=["morning", "afternoon", "evening", "night"])
    ap.add_argument("--out", default="output")
    ap.add_argument(
        "--ignore-daily-cap",
        action="store_true",
        help="Skip DAILY_UPLOAD_CAP check for this run",
    )
    args = ap.parse_args()

    niche = niche_for_slot(args.slot)
    print("== BlinkViral App TRUTH Pipeline ==")
    print(f"Slot: {args.slot} | Niche: {niche}")

    if not args.ignore_daily_cap:
        try:
            assert_under_daily_cap(source="run_pipeline")
        except DailyCapExceeded as exc:
            print(f"== Pipeline skipped (daily cap) ==")
            print(str(exc))
            return 3

    print("Step 1/4: pick niche-fit topic")
    print("Step 2/4: generate title + script")
    print("Step 3/4: fetch stock images and render video")
    print("Step 4/4: save kit metadata\n")

    kit = generate(None, args.slot, args.out)
    print("\n== Pipeline Complete ==")
    print(f"Video: {kit['video']}")
    print(f"Title: {kit['title']}")
    print(f"Topic: {kit['topic']}")
    print(f"Niche: {kit.get('niche', niche)}")
    print(f"Schedule: {kit['scheduled_time_utc']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

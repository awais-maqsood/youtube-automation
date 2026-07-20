#!/usr/bin/env python3
"""
Single-command entrypoint for Shorts generation.

Runs topic selection, script generation, stock lookup, and local video render
in one terminal window.

Slots (4/day):
  morning, afternoon → high_cpm (AI tools / finance / SaaS)
  evening, night     → viral trending topics
"""

import argparse
from generate import generate
from content_gen import niche_for_slot


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", default="morning", choices=["morning", "afternoon", "evening", "night"])
    ap.add_argument("--out", default="output")
    args = ap.parse_args()

    niche = niche_for_slot(args.slot)
    print("== Trending Shorts Pipeline ==")
    print(f"Slot: {args.slot} | Niche: {niche}")
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

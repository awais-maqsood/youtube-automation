#!/usr/bin/env python3
"""
Single-command entrypoint for FIFA World Cup Shorts generation.

Runs topic selection, script generation, stock lookup, and local video render
in one terminal window.
"""

import argparse
from generate import generate


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", default="morning", choices=["morning", "afternoon", "evening", "night"])
    ap.add_argument("--out", default="output")
    args = ap.parse_args()

    print("== FIFA World Cup Shorts Pipeline ==")
    print("Step 1/4: pick latest channel-fit topic")
    print("Step 2/4: generate title + script")
    print("Step 3/4: fetch stock images and render video")
    print("Step 4/4: save kit metadata\n")

    kit = generate(None, args.slot, args.out)
    print("\n== Pipeline Complete ==")
    print(f"Video: {kit['video']}")
    print(f"Title: {kit['title']}")
    print(f"Topic: {kit['topic']}")
    print(f"Schedule: {kit['scheduled_time_utc']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

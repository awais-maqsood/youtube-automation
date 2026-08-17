#!/usr/bin/env python3
"""
Configurable daily upload/generation cap.

Volume control for scheduled publishes.
Set DAILY_UPLOAD_CAP (default 1) to match the App TRUTH 1x/day cadence.

State file: data/daily_upload_log.json (UTC day keys).
Bypass: BYPASS_DAILY_UPLOAD_CAP=1 (manual workflow_dispatch).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "data" / "daily_upload_log.json"
DEFAULT_CAP = 1


class DailyCapExceeded(RuntimeError):
    """Raised when today's generation/upload count is at the configured cap."""

    def __init__(self, message: str, *, count: int = 0, cap: int = 0, day: str = "") -> None:
        super().__init__(message)
        self.count = count
        self.cap = cap
        self.day = day


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def daily_upload_cap() -> int:
    """Max successful publishes (and pipeline starts) allowed per UTC day."""
    raw = _env("DAILY_UPLOAD_CAP", str(DEFAULT_CAP)) or str(DEFAULT_CAP)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_CAP


def bypass_daily_cap() -> bool:
    return _env("BYPASS_DAILY_UPLOAD_CAP", "").lower() in {"1", "true", "yes", "on"}


def _load_log() -> dict:
    try:
        if not LOG_PATH.exists():
            return {"days": {}}
        data = json.loads(LOG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"days": {}}
        days = data.get("days")
        if not isinstance(days, dict):
            days = {}
        return {"days": days, "updated_utc": data.get("updated_utc")}
    except Exception as exc:
        print(f"  [WARN] daily cap log read failed: {exc}")
        return {"days": {}}


def _save_log(data: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["updated_utc"] = datetime.now(timezone.utc).isoformat()
    # Keep last ~60 days only.
    days = data.get("days") or {}
    if isinstance(days, dict) and len(days) > 60:
        keep = sorted(days.keys())[-60:]
        data["days"] = {k: days[k] for k in keep}
    LOG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def today_count(day: str | None = None) -> int:
    day = day or utc_day()
    data = _load_log()
    entry = (data.get("days") or {}).get(day) or {}
    if isinstance(entry, dict):
        return int(entry.get("count") or 0)
    try:
        return int(entry)
    except (TypeError, ValueError):
        return 0


def remaining_today() -> int:
    if bypass_daily_cap():
        return 10**9
    cap = daily_upload_cap()
    return max(0, cap - today_count())


def assert_under_daily_cap(*, source: str = "daily_cap") -> dict:
    """Fail closed when today's count is already at/above DAILY_UPLOAD_CAP."""
    day = utc_day()
    cap = daily_upload_cap()
    count = today_count(day)
    info = {
        "day": day,
        "count": count,
        "cap": cap,
        "remaining": max(0, cap - count) if not bypass_daily_cap() else None,
        "bypass": bypass_daily_cap(),
        "source": source,
    }
    if bypass_daily_cap():
        print(f"  [OK] Daily cap bypassed at {source} (count={count}, cap={cap})")
        return info
    if count >= cap:
        print(
            f"  [SKIP] Daily upload cap reached at {source}: "
            f"{count}/{cap} on {day} UTC (set DAILY_UPLOAD_CAP or BYPASS_DAILY_UPLOAD_CAP=1)"
        )
        raise DailyCapExceeded(
            f"Daily upload cap reached ({count}/{cap}) for {day} UTC",
            count=count,
            cap=cap,
            day=day,
        )
    print(f"  [OK] Daily cap check at {source}: {count}/{cap} used on {day} UTC")
    return info


def record_daily_upload(
    *,
    title: str = "",
    slot: str = "",
    source: str = "upload",
) -> dict:
    """Increment today's counter after a successful publish (or committed generate)."""
    day = utc_day()
    data = _load_log()
    days = data.setdefault("days", {})
    entry = days.get(day)
    if not isinstance(entry, dict):
        entry = {"count": int(entry or 0), "events": []}
    entry["count"] = int(entry.get("count") or 0) + 1
    events = list(entry.get("events") or [])
    events.append(
        {
            "utc": datetime.now(timezone.utc).isoformat(),
            "title": title,
            "slot": slot,
            "source": source,
        }
    )
    entry["events"] = events[-20:]
    days[day] = entry
    _save_log(data)
    print(f"  Daily cap counter: {entry['count']}/{daily_upload_cap()} on {day} UTC")
    return {"day": day, "count": entry["count"], "cap": daily_upload_cap()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daily upload cap helper")
    ap.add_argument("--check", action="store_true", help="Exit 0 if under cap, 3 if capped")
    ap.add_argument(
        "--github-output",
        action="store_true",
        help="Write should_run=true/false to $GITHUB_OUTPUT",
    )
    ap.add_argument("--status", action="store_true", help="Print today's count/cap and exit 0")
    args = ap.parse_args(argv)

    day = utc_day()
    cap = daily_upload_cap()
    count = today_count(day)
    under = bypass_daily_cap() or count < cap

    if args.status or (not args.check and not args.github_output):
        print(json.dumps({
            "day": day,
            "count": count,
            "cap": cap,
            "under_cap": under,
            "bypass": bypass_daily_cap(),
        }))

    if args.github_output:
        out = os.environ.get("GITHUB_OUTPUT")
        line = f"should_run={'true' if under else 'false'}\n"
        if out:
            with open(out, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.write(f"daily_count={count}\n")
                fh.write(f"daily_cap={cap}\n")
        else:
            print(line, end="")

    if args.check:
        if under:
            print(f"  [OK] Under daily cap ({count}/{cap}) on {day} UTC")
            return 0
        print(f"  [SKIP] At daily cap ({count}/{cap}) on {day} UTC")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

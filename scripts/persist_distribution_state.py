#!/usr/bin/env python3
"""Commit distribution state to git so dedup survives failed CI jobs."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_FILES = (
    "data/publish_catalog.json",
    "data/daily_upload_log.json",
    "data/app_safety_queue.json",
)


def _run(cmd: list[str], *, cwd: Path = ROOT) -> None:
    print(f"  $ {' '.join(cmd)}")
    subprocess.run(cmd, cwd=cwd, check=True)


def persist(*, branch: str = "main", dry_run: bool = False) -> int:
    missing = [rel for rel in STATE_FILES if not (ROOT / rel).exists()]
    if missing:
        print(f"Missing state files: {', '.join(missing)}", file=sys.stderr)
        return 1

    _run(["git", "config", "user.name", "github-actions[bot]"])
    _run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"])
    _run(["git", "add", *STATE_FILES])

    diff = subprocess.run(
        ["git", "diff", "--staged", "--quiet"],
        cwd=ROOT,
        check=False,
    )
    if diff.returncode == 0:
        print("No distribution state changes to commit.")
        return 0

    if dry_run:
        _run(["git", "diff", "--staged"])
        print("Dry run — would commit distribution state.")
        return 0

    _run(["git", "commit", "-m", "chore: sync publish catalog [skip ci]"])
    _run(["git", "pull", "--rebase", f"origin", branch])
    _run(["git", "push", f"origin", branch])
    print("Distribution state pushed to git.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Persist publish catalog to git")
    ap.add_argument("--branch", default=os.environ.get("GITHUB_REF_NAME", "main"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        return persist(branch=args.branch, dry_run=args.dry_run)
    except subprocess.CalledProcessError as exc:
        print(f"Persist failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

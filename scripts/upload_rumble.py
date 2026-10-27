#!/usr/bin/env python3
"""
Upload the generated Short to Rumble (BlinkViral channel).

After YouTube upload succeeds, CI runs:
  python scripts/upload_rumble.py --kit output/kit.json

Uses Rumble's official PRO Upload API:
  POST https://rumble.com/api/simple-upload.php

Env (GitHub Secrets / Variables):
  RUMBLE_ACCESS_TOKEN  — required (PRO Upload API token from Rumble support)
  RUMBLE_CHANNEL_ID    — default: BlinkViralOfficial channel (7972292)
  RUMBLE_LICENSE_TYPE  — default: 0
  RUMBLE_ENABLED       — set to 0/false to skip without failing

Get a token: Rumble PRO → email bd@rumble.com (or your account manager)
and request an Upload API access_token for channel uploads.
Docs: https://rumbleplayer.com/developers/Upload.html
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_CHANNEL_ID = "7972292"  # BlinkViralOfficial (display title: BlinkViral)
UPLOAD_URL = "https://rumble.com/api/simple-upload.php"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _truthy(name: str, default: str = "1") -> bool:
    return _env(name, default).lower() not in {"0", "false", "no", "off", ""}


def _resolve_video(kit: dict, kit_path: Path) -> Path:
    video_rel = kit.get("video") or "output/video.mp4"
    candidates = [
        Path(video_rel),
        kit_path.parent / Path(video_rel).name,
        Path("output/video.mp4"),
    ]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError(f"Video not found for kit (tried {[str(c) for c in candidates]})")


def _resolve_thumb(kit: dict, kit_path: Path) -> Path | None:
    thumb_rel = kit.get("thumbnail") or ""
    if not thumb_rel:
        return None
    candidates = [
        Path(thumb_rel),
        kit_path.parent / Path(thumb_rel).name,
        Path("output/thumbnail.jpg"),
        Path("output/thumbnail.png"),
    ]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    return None


def _build_description(kit: dict) -> str:
    """Rumble description: kit description, append YouTube URL when present."""
    desc = (kit.get("description") or "").strip()
    yt = (kit.get("youtube_url") or "").strip()
    if yt and yt not in desc:
        desc = f"{desc}\n\nWatch on YouTube: {yt}".strip() if desc else f"Watch on YouTube: {yt}"
    return desc or (kit.get("title") or "BlinkViral Short")


def _parse_response(raw: str) -> dict:
    text = (raw or "").strip()
    if not text:
        raise RuntimeError("Rumble API returned an empty response.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Some older responses wrap JSON in HTML noise — take first object.
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError(f"Rumble API non-JSON response: {text[:400]}")
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise RuntimeError(f"Rumble API unexpected payload type: {type(data)}")
    return data


def _extract_url(data: dict) -> str:
    for key in ("url", "video_url", "watch_url", "permalink"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    path = data.get("url_path") or data.get("path")
    if isinstance(path, str) and path.strip():
        path = path.strip()
        return path if path.startswith("http") else f"https://rumble.com{path}"
    vid = data.get("video_id") or data.get("fid") or data.get("id")
    if vid:
        return f"https://rumble.com/v{vid}"
    return ""


def upload_to_rumble(
    *,
    video_path: Path,
    title: str,
    description: str,
    access_token: str,
    channel_id: str,
    license_type: str = "0",
    thumb_path: Path | None = None,
) -> dict:
    """Multipart upload via curl (available on GitHub Actions runners)."""
    # Write text fields to temp files so shell metacharacters in titles/descriptions
    # never break -F parsing.
    with tempfile.TemporaryDirectory(prefix="rumble_upload_") as tmp:
        tmp_path = Path(tmp)
        title_file = tmp_path / "title.txt"
        desc_file = tmp_path / "description.txt"
        title_file.write_text(title, encoding="utf-8")
        desc_file.write_text(description, encoding="utf-8")

        cmd = [
            "curl",
            "-sS",
            "-X",
            "POST",
            UPLOAD_URL,
            "-F",
            f"access_token={access_token}",
            "-F",
            f"title=<{title_file}",
            "-F",
            f"description=<{desc_file}",
            "-F",
            f"license_type={license_type}",
            "-F",
            f"channel_id={channel_id}",
            "-F",
            f"video=@{video_path};type=video/mp4",
        ]
        if thumb_path is not None:
            mime = "image/png" if thumb_path.suffix.lower() == ".png" else "image/jpeg"
            cmd.extend(["-F", f"thumb=@{thumb_path};type={mime}"])

        try:
            completed = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=900,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("curl is required for Rumble upload but was not found on PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Rumble upload timed out after 900s.") from exc

        if completed.returncode != 0:
            err = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"curl failed ({completed.returncode}): {err[:500]}")

        data = _parse_response(completed.stdout)
        if data.get("success") is False:
            errors = data.get("errors") or data.get("error") or data
            raise RuntimeError(f"Rumble upload rejected: {json.dumps(errors, ensure_ascii=False)[:600]}")
        return data


def upload_kit(kit_path: Path) -> dict:
    kit = json.loads(kit_path.read_text(encoding="utf-8"))
    token = _env("RUMBLE_ACCESS_TOKEN")
    if not token:
        raise RuntimeError(
            "RUMBLE_ACCESS_TOKEN is required. Request a PRO Upload API token from Rumble "
            "(bd@rumble.com / account manager), then add it as a GitHub Actions secret."
        )
    channel_id = _env("RUMBLE_CHANNEL_ID", DEFAULT_CHANNEL_ID)
    license_type = _env("RUMBLE_LICENSE_TYPE", "0") or "0"

    video_path = _resolve_video(kit, kit_path)
    thumb_path = _resolve_thumb(kit, kit_path)
    title = (kit.get("title") or "BlinkViral Short").strip()[:100]
    description = _build_description(kit)

    print(f"Rumble upload → channel_id={channel_id}")
    print(f"  title={title}")
    print(f"  video={video_path} ({video_path.stat().st_size} bytes)")
    if thumb_path:
        print(f"  thumb={thumb_path}")

    data = upload_to_rumble(
        video_path=video_path,
        title=title,
        description=description,
        access_token=token,
        channel_id=channel_id,
        license_type=license_type,
        thumb_path=thumb_path,
    )

    rumble_url = _extract_url(data)
    video_id = str(data.get("video_id") or data.get("fid") or data.get("id") or "")
    result = {
        "ok": True,
        "channel_id": channel_id,
        "video_id": video_id,
        "rumble_url": rumble_url,
        "raw": data,
    }

    # Persist onto kit for downstream steps / artifacts.
    kit["rumble_id"] = video_id
    if rumble_url:
        kit["rumble_url"] = rumble_url
    try:
        kit_path.write_text(json.dumps(kit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"  [WARN] Could not update kit with rumble_url: {exc}")

    out = Path("output/rumble_upload.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if rumble_url:
        print(f"  ✓ Uploaded: {rumble_url}")
    else:
        print(f"  ✓ Uploaded (no URL in response): {json.dumps(data, ensure_ascii=False)[:300]}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Upload kit Short to Rumble BlinkViral channel")
    ap.add_argument("--kit", default="output/kit.json", help="Path to kit.json")
    args = ap.parse_args()

    if not _truthy("RUMBLE_ENABLED", "1"):
        print("Skipping Rumble — RUMBLE_ENABLED is off.")
        return 0

    if not _env("RUMBLE_ACCESS_TOKEN"):
        print("::warning::Skipping Rumble — set RUMBLE_ACCESS_TOKEN secret (PRO Upload API).")
        return 0

    try:
        upload_kit(Path(args.kit))
    except Exception as exc:
        print(f"❌ Rumble upload failed: {exc}", file=sys.stderr)
        return 1
    print("✅ Rumble upload done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Upload the generated Short + caption sidecar to Google Drive for Zapier → Publer.

After YouTube upload succeeds, CI runs:
  python scripts/upload_drive.py --kit output/kit.json

Creates in GOOGLE_DRIVE_FOLDER_ID:
  {stamp}_{slug}.mp4   — video
  {stamp}_{slug}.txt   — caption (title + description) for Zapier
  {stamp}_{slug}.json  — machine metadata (file ids, title, platforms)

Zapier (free middle ground — no Publer API):
  Zap A: Google Drive "New File in Folder" (.mp4)
       → Publer "Post Immediately" (or Create Post) with video + caption from .txt
       → Google Drive "Delete File" (video)
       → Google Drive "Delete File" (matching .txt and .json)  OR use Zap B below

  Zap B (if you schedule instead of post immediately):
       Publer "Post Published"
       → Google Drive "Delete File" using file id stored in the .json / Sheet

Env (GitHub Secrets):
  GOOGLE_DRIVE_CLIENT_ID       — or reuse YOUTUBE_CLIENT_ID
  GOOGLE_DRIVE_CLIENT_SECRET   — or reuse YOUTUBE_CLIENT_SECRET
  GOOGLE_DRIVE_REFRESH_TOKEN   — from scripts/auth_drive.py
  GOOGLE_DRIVE_FOLDER_ID       — target folder id
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
FILES_URL = "https://www.googleapis.com/drive/v3/files"


def _env(*names: str) -> str | None:
    for name in names:
        val = os.environ.get(name)
        if val:
            return val
    return None


def get_access_token() -> str:
    client_id = _env("GOOGLE_DRIVE_CLIENT_ID", "YOUTUBE_CLIENT_ID")
    client_secret = _env("GOOGLE_DRIVE_CLIENT_SECRET", "YOUTUBE_CLIENT_SECRET")
    refresh_token = _env("GOOGLE_DRIVE_REFRESH_TOKEN")
    if not client_id or not client_secret or not refresh_token:
        raise RuntimeError(
            "Missing Drive OAuth secrets. Need GOOGLE_DRIVE_REFRESH_TOKEN and "
            "GOOGLE_DRIVE_CLIENT_ID/SECRET (or YOUTUBE_CLIENT_ID/SECRET)."
        )
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())["access_token"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Drive token refresh failed HTTP {e.code}: {body}") from e


def slugify(text: str, max_len: int = 48) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "short").strip().lower()).strip("-")
    return (s or "short")[:max_len].rstrip("-")


def build_caption(kit: dict) -> str:
    title = (kit.get("title") or "").strip()
    description = (kit.get("description") or "").strip()
    tags = kit.get("tags") or []
    parts = []
    if title:
        parts.append(title)
    if description:
        parts.append(description)
    if tags:
        hashtags = " ".join(
            t if str(t).startswith("#") else f"#{str(t).replace(' ', '')}" for t in tags[:12]
        )
        if hashtags:
            parts.append(hashtags)
    return "\n\n".join(parts).strip() or title or "New Short"


def upload_bytes(
    access_token: str,
    *,
    name: str,
    data: bytes,
    mime_type: str,
    folder_id: str,
) -> dict:
    metadata = {"name": name, "parents": [folder_id]}
    boundary = f"boundary_{int(time.time() * 1000)}"
    meta_json = json.dumps(metadata, separators=(",", ":"))
    body = (
        f"--{boundary}\r\n"
        f'Content-Type: application/json; charset=UTF-8\r\n\r\n'
        f"{meta_json}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(
        UPLOAD_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Drive upload failed for {name} HTTP {e.code}: {body_err}") from e


def upload_kit_to_drive(kit_path: Path, folder_id: str) -> dict:
    kit = json.loads(kit_path.read_text(encoding="utf-8"))
    video_rel = kit.get("video") or "output/video.mp4"
    candidates = [
        Path(video_rel),
        Path.cwd() / video_rel,
        kit_path.parent / "video.mp4",
        kit_path.parent / Path(video_rel).name,
    ]
    video_path = next((p.resolve() for p in candidates if p.exists()), None)
    if video_path is None:
        raise FileNotFoundError(f"Video not found for kit: tried {video_rel}")

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    slug = slugify(kit.get("title") or kit.get("topic") or "short")
    base = f"{stamp}_{slug}"
    caption = build_caption(kit)

    token = get_access_token()
    video_bytes = video_path.read_bytes()
    video_meta = upload_bytes(
        token,
        name=f"{base}.mp4",
        data=video_bytes,
        mime_type="video/mp4",
        folder_id=folder_id,
    )
    caption_meta = upload_bytes(
        token,
        name=f"{base}.txt",
        data=caption.encode("utf-8"),
        mime_type="text/plain",
        folder_id=folder_id,
    )

    result = {
        "uploaded_at_utc": stamp,
        "base_name": base,
        "title": kit.get("title"),
        "topic": kit.get("topic"),
        "caption": caption,
        "platforms": ["instagram", "facebook", "tiktok"],
        "video": {
            "id": video_meta.get("id"),
            "name": video_meta.get("name"),
            "webViewLink": f"https://drive.google.com/file/d/{video_meta.get('id')}/view",
        },
        "caption_file": {
            "id": caption_meta.get("id"),
            "name": caption_meta.get("name"),
        },
        "delete_after_publer": True,
        "note": (
            "Zapier: after Publer publishes, delete video + caption (+ this json) "
            "from Drive using these file ids."
        ),
    }

    json_meta = upload_bytes(
        token,
        name=f"{base}.json",
        data=(json.dumps(result, indent=2) + "\n").encode("utf-8"),
        mime_type="application/json",
        folder_id=folder_id,
    )
    result["meta_file"] = {
        "id": json_meta.get("id"),
        "name": json_meta.get("name"),
    }

    out_path = kit_path.parent / "drive_upload.json"
    out_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Drive upload OK: {result['video']['webViewLink']}")
    print(f"Wrote {out_path}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload Short + caption to Google Drive for Publer/Zapier")
    parser.add_argument("--kit", default="output/kit.json", help="Path to kit.json")
    args = parser.parse_args()

    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    if not folder_id:
        print("GOOGLE_DRIVE_FOLDER_ID is required.", file=sys.stderr)
        return 1

    kit_path = Path(args.kit)
    if not kit_path.exists():
        print(f"Kit not found: {kit_path}", file=sys.stderr)
        return 1

    try:
        upload_kit_to_drive(kit_path, folder_id)
    except Exception as exc:
        print(f"Drive upload failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

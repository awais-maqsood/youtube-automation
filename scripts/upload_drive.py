#!/usr/bin/env python3
"""
Upload the generated Short to Google Drive for Zapier → Publer.

After YouTube upload succeeds, CI runs:
  python scripts/upload_drive.py --kit output/kit.json

Uploads ONLY one file into GOOGLE_DRIVE_FOLDER_ID:
  {stamp}_{slug}.mp4
  - Drive "name"      ≈ title (Zapier can map to Publer title)
  - Drive "description" = full social caption (title + body + hashtags)

IMPORTANT (duplicates + missing captions):
  Do NOT upload .txt/.json into the same Zap-watched folder — each file
  retriggers Zapier and causes repeat posts.

Zapier setup:
  1. Trigger: Google Drive → New File in Folder → Publer Inbox
  2. Filter: File Extension is mp4  (or MIME Type contains video)
  3. Publer Post Immediately:
       - Media  = Drive file
       - Caption / Text = Drive Description   ← title + hashtags live here
       - Title (if field exists) = Drive Name without .mp4
       - Accounts = IG + FB + TikTok  (once each — do not add accounts twice)
  4. Delete File = the triggering mp4 only

Env (GitHub Secrets):
  GOOGLE_DRIVE_CLIENT_ID / SECRET / REFRESH_TOKEN / FOLDER_ID
  (CLIENT_ID/SECRET may fall back to YOUTUBE_*)
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
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,description,webViewLink"


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


def _normalize_hashtag(tag: str) -> str:
    t = str(tag).strip()
    if not t:
        return ""
    if not t.startswith("#"):
        t = "#" + re.sub(r"\s+", "", t)
    return t


def build_social_caption(kit: dict, *, max_chars: int = 2100) -> str:
    """Caption for IG/FB/TikTok via Publer (title + body + hashtags)."""
    title = (kit.get("title") or "").strip()
    description = (kit.get("description") or "").strip()
    tags = kit.get("tags") or []

    body_lines: list[str] = []
    hashtag_lines: list[str] = []
    for line in description.splitlines():
        stripped = line.strip()
        if not stripped:
            if body_lines and body_lines[-1] != "":
                body_lines.append("")
            continue
        # Lines that are mostly hashtags
        if stripped.startswith("#") or (
            stripped.count("#") >= 2 and len(stripped.split()) <= 12
        ):
            hashtag_lines.append(stripped)
        else:
            body_lines.append(stripped)

    body = "\n".join(body_lines).strip()
    existing_tags = " ".join(hashtag_lines).strip()

    from_kit = " ".join(filter(None, (_normalize_hashtag(t) for t in tags[:15])))
    # Prefer kit tags if description had none; otherwise keep description hashtags
    # and append any missing kit tags.
    if existing_tags:
        have = {h.lower() for h in re.findall(r"#\w+", existing_tags)}
        extra = [
            _normalize_hashtag(t)
            for t in tags[:15]
            if _normalize_hashtag(t).lower() not in have
        ]
        hashtags = (existing_tags + (" " + " ".join(extra) if extra else "")).strip()
    else:
        hashtags = from_kit

    parts = [p for p in (title, body, hashtags) if p]
    caption = "\n\n".join(parts).strip() or title or "New Short"
    if len(caption) > max_chars:
        # Keep title + hashtags; trim body
        keep_tail = f"\n\n{hashtags}" if hashtags else ""
        budget = max_chars - len(title) - len(keep_tail) - 4
        if budget < 40:
            caption = (title + keep_tail)[:max_chars]
        else:
            caption = f"{title}\n\n{body[:budget].rstrip()}…{keep_tail}"
    return caption


def upload_video(
    access_token: str,
    *,
    name: str,
    description: str,
    data: bytes,
    folder_id: str,
) -> dict:
    metadata = {
        "name": name,
        "parents": [folder_id],
        "description": description,
        "mimeType": "video/mp4",
    }
    boundary = f"boundary_{int(time.time() * 1000)}"
    meta_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    body = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{meta_json}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: video/mp4\r\n\r\n"
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
    title = (kit.get("title") or kit.get("topic") or "short").strip()
    slug = slugify(title)
    # Unique name so Zapier never reuses an old trigger fingerprint.
    file_name = f"{stamp}_{slug}.mp4"
    caption = build_social_caption(kit)

    token = get_access_token()
    video_meta = upload_video(
        token,
        name=file_name,
        description=caption,
        data=video_path.read_bytes(),
        folder_id=folder_id,
    )

    result = {
        "uploaded_at_utc": stamp,
        "file_name": file_name,
        "title": title,
        "caption": caption,
        "platforms": ["instagram", "facebook", "tiktok"],
        "video": {
            "id": video_meta.get("id"),
            "name": video_meta.get("name") or file_name,
            "description": video_meta.get("description") or caption,
            "webViewLink": video_meta.get("webViewLink")
            or f"https://drive.google.com/file/d/{video_meta.get('id')}/view",
        },
        "zapier": {
            "filter": "only .mp4 / video/*",
            "map_caption_from": "Google Drive Description",
            "map_title_from": "Google Drive Name (strip .mp4)",
            "accounts_once": True,
        },
    }

    out_path = kit_path.parent / "drive_upload.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Drive upload OK: {result['video']['webViewLink']}")
    print(f"Caption chars: {len(caption)}")
    print(f"Wrote {out_path}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upload Short to Google Drive for Publer/Zapier (single mp4 + caption in description)"
    )
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

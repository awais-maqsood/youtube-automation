#!/usr/bin/env python3
"""
Upload the generated Short to Postiz (Instagram + Facebook).

After YouTube upload succeeds, CI runs:
  python scripts/upload_postiz.py --kit output/kit.json

Flow:
  1. POST /upload  — multipart video/mp4
  2. POST /posts   — caption + media to IG/FB integrations

Env (GitHub Secrets / Variables):
  POSTIZ_API_KEY              — required (Authorization header, no Bearer prefix)
  POSTIZ_BASE_URL             — default: BlinkViral self-hosted public API
  POSTIZ_POST_TYPE            — now | draft | schedule  (default: now)
  POSTIZ_PLATFORMS            — comma list: instagram,facebook  (default)
  POSTIZ_INSTAGRAM_CHANNEL_ID — integration id for IG
  POSTIZ_FACEBOOK_CHANNEL_ID  — integration id for FB
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from upload_drive import build_social_caption

DEFAULT_BASE_URL = "https://apis.ideationtec.com/blinkviral/app/api/public/v1"
DEFAULT_INSTAGRAM_CHANNEL_ID = "cmtcy2nh60001qt77v5u1reio"
DEFAULT_FACEBOOK_CHANNEL_ID = "cmtcy401f0003qt77pu879g0i"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _api_base() -> str:
    base = _env("POSTIZ_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    return base


def _auth_headers(*, json_body: bool = False) -> dict[str, str]:
    api_key = _env("POSTIZ_API_KEY")
    if not api_key:
        raise RuntimeError("POSTIZ_API_KEY is required.")
    headers = {"Authorization": api_key}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _http_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = None
    headers = _auth_headers(json_body=payload is not None)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Postiz API {method} {url} failed HTTP {exc.code}: {body}") from exc


def _multipart_upload(url: str, file_path: Path, mime: str = "video/mp4") -> dict:
    boundary = f"----PostizUpload{int(time.time() * 1000)}"
    file_bytes = file_path.read_bytes()
    filename = file_path.name

    preamble = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8")
    epilogue = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = preamble + file_bytes + epilogue

    headers = _auth_headers()
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    headers["Content-Length"] = str(len(body))

    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Postiz upload failed HTTP {exc.code}: {err_body}") from exc


def _resolve_video_path(kit: dict, kit_path: Path) -> Path:
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
    return video_path


def _platform_channel_ids() -> dict[str, str]:
    return {
        "instagram": _env("POSTIZ_INSTAGRAM_CHANNEL_ID", DEFAULT_INSTAGRAM_CHANNEL_ID),
        "facebook": _env("POSTIZ_FACEBOOK_CHANNEL_ID", DEFAULT_FACEBOOK_CHANNEL_ID),
    }


def _selected_platforms() -> list[str]:
    raw = _env("POSTIZ_PLATFORMS", "instagram,facebook")
    platforms = [p.strip().lower() for p in raw.split(",") if p.strip()]
    supported = {"instagram", "facebook"}
    unknown = [p for p in platforms if p not in supported]
    if unknown:
        raise RuntimeError(f"Unsupported POSTIZ_PLATFORMS: {unknown} (supported: instagram, facebook)")
    if not platforms:
        raise RuntimeError("POSTIZ_PLATFORMS is empty.")
    return platforms


def _post_entry(platform: str, channel_id: str, caption: str, media: dict) -> dict:
    value = [{"content": caption, "image": [{"id": media["id"], "path": media["path"]}]}]
    if platform == "instagram":
        return {
            "integration": {"id": channel_id},
            "value": value,
            "settings": {
                "__type": "instagram",
                "post_type": "post",
                "is_trial_reel": False,
                "collaborators": [],
            },
        }
    if platform == "facebook":
        return {
            "integration": {"id": channel_id},
            "value": value,
            "settings": {"__type": "facebook"},
        }
    raise ValueError(f"Unknown platform: {platform}")


def build_post_payload(
    *,
    caption: str,
    media: dict,
    post_type: str,
    platforms: list[str],
    channel_ids: dict[str, str],
    schedule_date: str | None = None,
) -> dict:
    if post_type not in {"now", "draft", "schedule"}:
        raise ValueError(f"Invalid post_type: {post_type}")

    posts = []
    for platform in platforms:
        channel_id = channel_ids.get(platform, "")
        if not channel_id:
            raise RuntimeError(f"Missing channel id for platform: {platform}")
        posts.append(_post_entry(platform, channel_id, caption, media))

    date = schedule_date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "type": post_type,
        "shortLink": False,
        "date": date,
        "tags": [],
        "posts": posts,
    }


def upload_kit_to_postiz(
    kit_path: Path,
    *,
    post_type: str | None = None,
    dry_run: bool = False,
) -> dict:
    kit = json.loads(kit_path.read_text(encoding="utf-8"))
    video_path = _resolve_video_path(kit, kit_path)
    caption = build_social_caption(kit)
    platforms = _selected_platforms()
    channel_ids = _platform_channel_ids()
    resolved_type = (post_type or _env("POSTIZ_POST_TYPE", "now")).lower()
    schedule_date = _env("POSTIZ_SCHEDULE_DATE") or None

    base = _api_base()
    upload_url = f"{base}/upload"
    posts_url = f"{base}/posts"

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    title = (kit.get("title") or kit.get("topic") or "short").strip()

    if dry_run:
        media = {"id": "DRY_RUN_MEDIA_ID", "path": "https://example.com/video.mp4"}
        payload = build_post_payload(
            caption=caption,
            media=media,
            post_type=resolved_type,
            platforms=platforms,
            channel_ids=channel_ids,
            schedule_date=schedule_date,
        )
        result = {
            "dry_run": True,
            "uploaded_at_utc": stamp,
            "title": title,
            "caption": caption,
            "platforms": platforms,
            "post_type": resolved_type,
            "video_path": str(video_path),
            "upload_url": upload_url,
            "posts_url": posts_url,
            "post_payload": payload,
        }
        out_path = kit_path.parent / "postiz_upload.json"
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        print(f"Dry run — wrote {out_path}")
        return result

    media = _multipart_upload(upload_url, video_path)
    if not media.get("id") or not media.get("path"):
        raise RuntimeError(f"Upload response missing id/path: {media}")

    payload = build_post_payload(
        caption=caption,
        media=media,
        post_type=resolved_type,
        platforms=platforms,
        channel_ids=channel_ids,
        schedule_date=schedule_date,
    )
    post_resp = _http_json("POST", posts_url, payload)

    result = {
        "uploaded_at_utc": stamp,
        "title": title,
        "caption": caption,
        "platforms": platforms,
        "post_type": resolved_type,
        "media": {"id": media["id"], "path": media["path"]},
        "post_response": post_resp,
    }

    out_path = kit_path.parent / "postiz_upload.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Postiz upload OK — media id={media['id']}")
    print(f"Postiz publish ({resolved_type}) → {', '.join(platforms)}")
    print(f"Caption chars: {len(caption)}")
    print(f"Wrote {out_path}")
    return result


def list_integrations() -> list[dict]:
    base = _api_base()
    data = _http_json("GET", f"{base}/integrations")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("integrations", "data", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload Short to Postiz (Instagram + Facebook)")
    parser.add_argument("--kit", default="output/kit.json", help="Path to kit.json")
    parser.add_argument(
        "--post-type",
        choices=["now", "draft", "schedule"],
        help="Override POSTIZ_POST_TYPE (default: now)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Build payload only; skip API upload")
    parser.add_argument(
        "--list-integrations",
        action="store_true",
        help="Print connected channels and exit",
    )
    args = parser.parse_args()

    if args.list_integrations:
        try:
            integrations = list_integrations()
        except Exception as exc:
            print(f"Failed to list integrations: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(integrations, indent=2, ensure_ascii=False))
        return 0

    kit_path = Path(args.kit)
    if not kit_path.exists():
        print(f"Kit not found: {kit_path}", file=sys.stderr)
        return 1

    try:
        upload_kit_to_postiz(kit_path, post_type=args.post_type, dry_run=args.dry_run)
    except Exception as exc:
        print(f"Postiz upload failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

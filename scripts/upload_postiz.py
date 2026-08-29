#!/usr/bin/env python3
"""
Upload the generated Short to Postiz (Instagram + Facebook).

After YouTube upload succeeds, CI runs:
  python scripts/upload_postiz.py --kit output/kit.json

Flow:
  1. POST /upload  — multipart video/mp4 (Instagram only)
  2. POST /posts   — IG: caption + video; FB: caption + YouTube link

Env (GitHub Secrets / Variables):
  POSTIZ_API_KEY              — required (Authorization header, no Bearer prefix)
  POSTIZ_BASE_URL             — default: BlinkViral self-hosted public API
  POSTIZ_POST_TYPE            — now | draft | schedule  (default: now)
  POSTIZ_PLATFORMS            — comma list: instagram,facebook  (default)
  POSTIZ_INSTAGRAM_CHANNEL_ID — integration id for IG
  POSTIZ_FACEBOOK_CHANNEL_ID  — integration id for FB
  POSTIZ_FACEBOOK_MODE        — link (default: caption + YouTube URL) | video
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
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


def _http_json(method: str, url: str, payload: dict | None = None):
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


def _list_recent_posts(*, hours: int = 6) -> list[dict]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    params = (
        f"startDate={start.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
        f"&endDate={end.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
    )
    data = _http_json("GET", f"{_api_base()}/posts?{params}")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("posts", "data", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _wait_for_post_states(post_ids: list[str], *, timeout_s: int = 90) -> dict[str, dict]:
    """Poll until posts leave QUEUE/PENDING, or timeout."""
    wanted = {pid: {} for pid in post_ids if pid}
    if not wanted:
        return wanted
    deadline = time.time() + timeout_s
    terminal = {"PUBLISHED", "ERROR", "DRAFT"}
    while time.time() < deadline:
        for post in _list_recent_posts(hours=12):
            pid = post.get("id")
            if pid in wanted:
                wanted[pid] = post
        if wanted and all((p.get("state") or "").upper() in terminal for p in wanted.values() if p):
            return wanted
        time.sleep(5)
    return wanted


def _assert_platforms_ok(post_response, channel_ids: dict[str, str]) -> None:
    """Fail the job if a requested platform ended in ERROR (common for FB video perms)."""
    if not isinstance(post_response, list):
        return
    id_to_platform = {cid: plat for plat, cid in channel_ids.items()}
    post_ids = []
    mapping: dict[str, str] = {}
    for item in post_response:
        if not isinstance(item, dict):
            continue
        pid = item.get("postId") or item.get("id")
        integ = item.get("integration")
        if isinstance(integ, dict):
            integ = integ.get("id")
        if pid:
            post_ids.append(pid)
            if integ:
                mapping[pid] = id_to_platform.get(str(integ), str(integ))

    states = _wait_for_post_states(post_ids)
    failures: list[str] = []
    for pid, post in states.items():
        state = (post.get("state") or "UNKNOWN").upper()
        platform = mapping.get(pid, "?")
        release = post.get("releaseURL") or ""
        print(f"  Postiz {platform}: state={state}" + (f" url={release}" if release else ""))
        if state == "ERROR":
            failures.append(platform)

    if failures:
        hint = ""
        if "facebook" in failures:
            hint = (
                " Facebook publish failed. For link posts, confirm pages_manage_posts "
                "and reconnect the Page in Postiz. For native video, Meta must also "
                "allow page video publish (App Live + Advanced Access)."
            )
        raise RuntimeError(
            "Postiz publish failed for: " + ", ".join(sorted(set(failures))) + "." + hint
        )


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


def _facebook_mode() -> str:
    mode = _env("POSTIZ_FACEBOOK_MODE", "link").lower()
    if mode not in {"link", "video"}:
        raise RuntimeError(f"Invalid POSTIZ_FACEBOOK_MODE: {mode} (use link or video)")
    return mode


def _youtube_url_from_kit(kit: dict) -> str:
    for key in ("youtube_url", "youtubeUrl"):
        url = (kit.get(key) or "").strip()
        if url:
            return url
    vid = (kit.get("youtube_id") or kit.get("youtubeId") or "").strip()
    if vid:
        return f"https://youtu.be/{vid}"
    return ""


def _post_entry(
    platform: str,
    channel_id: str,
    caption: str,
    media: dict | None,
    *,
    youtube_url: str = "",
    facebook_mode: str = "link",
) -> dict:
    if platform == "instagram":
        if not media or not media.get("id") or not media.get("path"):
            raise RuntimeError("Instagram post requires uploaded video media.")
        return {
            "integration": {"id": channel_id},
            "value": [
                {
                    "content": caption,
                    "image": [{"id": media["id"], "path": media["path"]}],
                }
            ],
            "settings": {
                "__type": "instagram",
                "post_type": "post",
                "is_trial_reel": False,
                "collaborators": [],
            },
        }
    if platform == "facebook":
        settings: dict = {"__type": "facebook", "post_type": "post"}
        if facebook_mode == "link":
            if not youtube_url:
                raise RuntimeError(
                    "Facebook link mode needs kit.youtube_url (from YouTube upload)."
                )
            settings["url"] = youtube_url
            value = [{"content": caption, "image": []}]
        else:
            if not media or not media.get("id") or not media.get("path"):
                raise RuntimeError("Facebook video mode requires uploaded video media.")
            value = [
                {
                    "content": caption,
                    "image": [{"id": media["id"], "path": media["path"]}],
                }
            ]
        return {
            "integration": {"id": channel_id},
            "value": value,
            "settings": settings,
        }
    raise ValueError(f"Unknown platform: {platform}")


def build_post_payload(
    *,
    caption: str,
    media: dict | None,
    post_type: str,
    platforms: list[str],
    channel_ids: dict[str, str],
    schedule_date: str | None = None,
    youtube_url: str = "",
    facebook_mode: str = "link",
) -> dict:
    if post_type not in {"now", "draft", "schedule"}:
        raise ValueError(f"Invalid post_type: {post_type}")

    posts = []
    for platform in platforms:
        channel_id = channel_ids.get(platform, "")
        if not channel_id:
            raise RuntimeError(f"Missing channel id for platform: {platform}")
        posts.append(
            _post_entry(
                platform,
                channel_id,
                caption,
                media,
                youtube_url=youtube_url,
                facebook_mode=facebook_mode,
            )
        )

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
    caption = build_social_caption(kit)
    platforms = _selected_platforms()
    channel_ids = _platform_channel_ids()
    facebook_mode = _facebook_mode()
    youtube_url = _youtube_url_from_kit(kit)
    resolved_type = (post_type or _env("POSTIZ_POST_TYPE", "now")).lower()
    schedule_date = _env("POSTIZ_SCHEDULE_DATE") or None

    needs_video = "instagram" in platforms or (
        "facebook" in platforms and facebook_mode == "video"
    )
    video_path = _resolve_video_path(kit, kit_path) if needs_video else None

    if "facebook" in platforms and facebook_mode == "link" and not youtube_url:
        raise RuntimeError(
            "Facebook link mode requires kit.youtube_url. "
            "Ensure YouTube upload ran first and wrote youtube_url into kit.json."
        )

    base = _api_base()
    upload_url = f"{base}/upload"
    posts_url = f"{base}/posts"

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    title = (kit.get("title") or kit.get("topic") or "short").strip()

    if dry_run:
        media = (
            {"id": "DRY_RUN_MEDIA_ID", "path": "https://example.com/video.mp4"}
            if needs_video
            else None
        )
        payload = build_post_payload(
            caption=caption,
            media=media,
            post_type=resolved_type,
            platforms=platforms,
            channel_ids=channel_ids,
            schedule_date=schedule_date,
            youtube_url=youtube_url or "https://youtu.be/DRY_RUN",
            facebook_mode=facebook_mode,
        )
        result = {
            "dry_run": True,
            "uploaded_at_utc": stamp,
            "title": title,
            "caption": caption,
            "platforms": platforms,
            "post_type": resolved_type,
            "facebook_mode": facebook_mode,
            "youtube_url": youtube_url,
            "video_path": str(video_path) if video_path else None,
            "upload_url": upload_url,
            "posts_url": posts_url,
            "post_payload": payload,
        }
        out_path = kit_path.parent / "postiz_upload.json"
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        print(f"Dry run — wrote {out_path}")
        return result

    media = None
    if needs_video:
        assert video_path is not None
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
        youtube_url=youtube_url,
        facebook_mode=facebook_mode,
    )
    post_resp = _http_json("POST", posts_url, payload)

    result = {
        "uploaded_at_utc": stamp,
        "title": title,
        "caption": caption,
        "platforms": platforms,
        "post_type": resolved_type,
        "facebook_mode": facebook_mode,
        "youtube_url": youtube_url or None,
        "media": (
            {"id": media["id"], "path": media["path"]}
            if media
            else None
        ),
        "post_response": post_resp,
    }

    out_path = kit_path.parent / "postiz_upload.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if media:
        print(f"Postiz upload OK — media id={media['id']}")
    else:
        print("Postiz: no video upload (Facebook link-only mode)")
    print(f"Postiz publish ({resolved_type}) → {', '.join(platforms)}")
    if "facebook" in platforms and facebook_mode == "link":
        print(f"Facebook link: {youtube_url}")
    print(f"Caption chars: {len(caption)}")
    print(f"Wrote {out_path}")

    # Create returns immediately; poll for ERROR (esp. Facebook).
    if resolved_type != "draft":
        _assert_platforms_ok(post_resp, channel_ids)

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

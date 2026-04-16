#!/usr/bin/env python3
"""
YouTube uploader — reads kit.json and uploads video + thumbnail.
Requires env vars: YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN
Run auth.py once to get your refresh token.
"""

import os, sys, json, argparse, mimetypes, time
import urllib.request, urllib.parse

CLIENT_ID     = os.environ.get("YOUTUBE_CLIENT_ID")
CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET")
REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN")
TOKEN_URL     = "https://oauth2.googleapis.com/token"
UPLOAD_URL    = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMB_URL     = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"


def get_access_token():
    payload = urllib.parse.urlencode({
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type":    "refresh_token",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=payload,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())["access_token"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"YouTube token refresh failed HTTP {e.code}: {body}") from e


def upload_video(access_token, video_path, title, description, scheduled_utc=None, privacy="private"):
    """Resumable upload. Returns video_id.
    privacy: "private" | "public" | "unlisted"
    scheduled_utc: ISO 8601 string — if set, video stays private until that time.
    """
    status = privacy
    publish_at = None

    if scheduled_utc:
        status = "private"          # must be private to schedule
        publish_at = scheduled_utc  # ISO 8601 e.g. "2025-12-15T15:30:00Z"

    metadata = {
        "snippet": {
            "title":       title,
            "description": description,
            "categoryId":  "28",   # Science & Technology
        },
        "status": {
            "privacyStatus": status,
        }
    }
    if publish_at:
        metadata["status"]["publishAt"] = publish_at

    body = json.dumps(metadata).encode()
    file_size = os.path.getsize(video_path)

    # Step 1: initiate resumable session
    init_req = urllib.request.Request(
        UPLOAD_URL + "?uploadType=resumable&part=snippet,status",
        data=body,
        headers={
            "Authorization":          f"Bearer {access_token}",
            "Content-Type":           "application/json",
            "X-Upload-Content-Type":  "video/mp4",
            "X-Upload-Content-Length": str(file_size),
        }
    )
    init_req.get_method = lambda: "POST"
    with urllib.request.urlopen(init_req) as r:
        session_uri = r.headers["Location"]

    # Step 2: upload bytes
    chunk = 8 * 1024 * 1024   # 8 MB
    uploaded = 0
    video_id = None

    with open(video_path, "rb") as f:
        while uploaded < file_size:
            data = f.read(chunk)
            end  = uploaded + len(data) - 1
            hdrs = {
                "Authorization":  f"Bearer {access_token}",
                "Content-Type":   "video/mp4",
                "Content-Range":  f"bytes {uploaded}-{end}/{file_size}",
            }
            req = urllib.request.Request(session_uri, data=data, headers=hdrs)
            req.get_method = lambda: "PUT"
            try:
                with urllib.request.urlopen(req) as r:
                    if r.status in (200, 201):
                        resp = json.loads(r.read())
                        video_id = resp["id"]
                        print(f"  ✓ Uploaded: https://youtu.be/{video_id}")
                        break
            except urllib.error.HTTPError as e:
                if e.code == 308:  # Resume Incomplete
                    uploaded = int(e.headers.get("Range","bytes=0-0").split("-")[1]) + 1
                    f.seek(uploaded)
                    continue
                raise
            uploaded += len(data)

    return video_id


def upload_thumbnail(access_token, video_id, thumb_path):
    with open(thumb_path, "rb") as f:
        data = f.read()
    mime = "image/png" if thumb_path.endswith(".png") else "image/jpeg"
    req = urllib.request.Request(
        f"{THUMB_URL}?videoId={video_id}",
        data=data,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type":  mime,
        }
    )
    req.get_method = lambda: "POST"
    with urllib.request.urlopen(req) as r:
        print(f"  ✓ Thumbnail set")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", default="output/kit.json", help="Path to kit.json")
    ap.add_argument("--no-schedule", action="store_true",
                    help="Upload immediately (public) instead of scheduling")
    args = ap.parse_args()

    if not all([CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN]):
        print("❌ Missing env vars. Run auth.py first to get YOUTUBE_REFRESH_TOKEN.")
        sys.exit(1)

    with open(args.kit) as f:
        kit = json.load(f)

    print(f"📤 Uploading: {kit['title']}")
    token = get_access_token()

    scheduled = None if args.no_schedule else kit.get("scheduled_time_utc")
    vid_id = upload_video(token, kit["video"], kit["title"],
                          kit["description"], scheduled_utc=scheduled)

    if vid_id and os.path.exists(kit.get("thumbnail","")):
        upload_thumbnail(token, vid_id, kit["thumbnail"])

    print(f"✅ Done. video_id={vid_id}")
    if scheduled:
        print(f"   Scheduled for: {scheduled}")


if __name__ == "__main__":
    main()

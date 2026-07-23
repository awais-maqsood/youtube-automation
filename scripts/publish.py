#!/usr/bin/env python3
"""Triggered by Approve button — uploads video to YouTube."""

import os, sys, json, hmac, hashlib, smtplib, ssl, urllib.request, urllib.error, tempfile, base64
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import upload as up
from topic_validation import EntityValidationError, assert_publishable_metadata, log_entity_rejection


def verify(run_id: str, sig: str) -> bool:
    secret = os.environ.get("PIPELINE_SECRET", "change-me")
    expected = hmac.new(secret.encode(), run_id.encode(), hashlib.sha256).hexdigest()[
        :24
    ]
    return hmac.compare_digest(expected, sig)


def get_kit_from_gh_pages(run_id: str) -> dict | None:
    """Fetch the review page from gh-pages and extract kit data from it."""
    repo = os.environ["GH_REPO"]
    owner = repo.split("/")[0]
    repo_name = repo.split("/")[1]
    pat = os.environ["GH_PAT"]

    # Download the review HTML from gh-pages via API
    url = f"https://api.github.com/repos/{repo}/contents/review/{run_id}.html?ref=gh-pages"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"token {pat}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req) as r:
        data = json.loads(r.read())
        content = base64.b64decode(data["content"]).decode()

    # Extract kit fields embedded in the HTML
    import re
    import html as html_mod

    kit = {}
    for field, pattern in [
        ("title", r"<h2>([^<]+)</h2>"),
        ("topic", r'<span class="badge">[^·]+·&nbsp;([^<]+)</span>'),
        ("slot", r'<span class="badge">([^&]+)'),
    ]:
        m = re.search(pattern, content)
        if m:
            kit[field] = m.group(1).strip()

    # Real diversified description + niche/tags from generation (not the old AI-hashtag stub).
    meta = re.search(
        r'<!--kit-meta niche="([^"]*)" tags="([^"]*)" description-b64="([^"]*)" -->',
        content,
    )
    if meta:
        kit["niche"] = html_mod.unescape(meta.group(1) or "viral")
        try:
            kit["tags"] = json.loads(html_mod.unescape(meta.group(2) or "[]"))
        except json.JSONDecodeError:
            kit["tags"] = []
        try:
            kit["description"] = base64.b64decode(meta.group(3)).decode("utf-8")
        except Exception:
            kit["description"] = ""
    else:
        kit["niche"] = "viral"
        kit["tags"] = []
        kit["description"] = ""

    # Extract release asset video URL
    vm = re.search(r'<source src="([^"]+)" type="video/mp4">', content)
    if not vm:
        return None
    kit["video_url"] = vm.group(1)
    return kit


def send_confirm(title: str, video_id: str):
    gmail = os.environ.get("GMAIL_ADDRESS", "")
    passwd = os.environ.get("GMAIL_APP_PASSWORD", "")
    notify = os.environ.get("NOTIFY_EMAIL", gmail)
    if not gmail or not passwd:
        return
    yt = f"https://www.youtube.com/watch?v={video_id}"
    html = f"""<html><body style="font-family:Arial;background:#111;color:#ddd;padding:30px;max-width:500px">
<h2 style="color:#00b894">✅ Published!</h2>
<p style="margin:12px 0"><b>{title}</b> is now live on YouTube.</p>
<a href="{yt}" style="color:#5865f2">{yt}</a>
</body></html>"""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    plain = f"✅ Published to YouTube!\n\n{title}\n\nWatch it live:\n{yt}\n\n— LLM Shorts"

    msg = MIMEMultipart("alternative")
    msg["Subject"]           = f"✅ [LLM Shorts] Published: {title}"
    msg["From"]              = gmail
    msg["To"]                = notify
    msg["Reply-To"]          = gmail
    msg["X-Priority"]        = "1 (Highest)"
    msg["X-MSMail-Priority"] = "High"
    msg["Importance"]        = "High"
    msg["Priority"]          = "urgent"
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(gmail, passwd)
        s.sendmail(gmail, notify, msg.as_string())
    print("  ✓ Confirmation email sent")


run_id = os.environ.get("RUN_ID", "")
sig = os.environ.get("SIG", "")

if not verify(run_id, sig):
    print("❌ Invalid signature.")
    sys.exit(1)

print(f"🔓 Signature valid. Fetching video for run {run_id}...")
kit = get_kit_from_gh_pages(run_id)
if not kit:
    print("❌ Could not find review page / video.")
    sys.exit(1)

# Download video from GitHub Release asset
print(f"  Downloading video from release asset...")
pat = os.environ["GH_PAT"]
req = urllib.request.Request(
    kit["video_url"],
    headers={"Authorization": f"token {pat}"},
)
with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
    with urllib.request.urlopen(req, timeout=300) as r:
        tmp.write(r.read())
    tmp_path = tmp.name
print(f"  ✓ Video downloaded ({os.path.getsize(tmp_path)//1024}KB)")

# Use the same diversified description from generation (never the old hardcoded AI tag dump).
description = (kit.get("description") or "").strip()
if not description:
    print("❌ Missing description on review page — regenerate the Short so kit-meta is embedded.")
    sys.exit(1)

missing = [v for v in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")
           if not os.environ.get(v)]
if missing:
    print(f"❌ Missing YouTube secrets: {', '.join(missing)}")
    print("   Add them to GitHub → Settings → Secrets and variables → Actions")
    sys.exit(1)

token = up.get_access_token()
title = kit.get("title", "LLM Short")
try:
    assert_publishable_metadata(title, description, source="publish.pre_upload")
except EntityValidationError as exc:
    log_entity_rejection("publish.pre_upload", title, {"description": description, "error": str(exc)})
    print(f"❌ Blocked publish — invalid title/description: {exc}")
    sys.exit(1)

category_id = up.youtube_category_id(
    niche=kit.get("niche"),
    title=title,
    description=description,
)
print(f"  YouTube categoryId={category_id} (niche={kit.get('niche', 'n/a')})")
video_id = up.upload_video(
    token,
    tmp_path,
    title,
    description,
    tags=kit.get("tags"),
    privacy="public",
    category_id=category_id,
)
os.unlink(tmp_path)

if not video_id:
    print("❌ Upload failed.")
    sys.exit(1)

print(f"✅ Published: https://youtu.be/{video_id}")
send_confirm(kit.get("title", "LLM Short"), video_id)

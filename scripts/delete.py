#!/usr/bin/env python3
"""Triggered by Reject button — deletes the GitHub Release asset and confirms via email."""

import os, sys, hmac, hashlib, smtplib, ssl, json, urllib.request, urllib.error


def verify(run_id: str, sig: str) -> bool:
    secret = os.environ.get("PIPELINE_SECRET", "change-me")
    expected = hmac.new(secret.encode(), run_id.encode(), hashlib.sha256).hexdigest()[
        :24
    ]
    return hmac.compare_digest(expected, sig)


def send_confirm(run_id: str):
    gmail = os.environ.get("GMAIL_ADDRESS", "")
    passwd = os.environ.get("GMAIL_APP_PASSWORD", "")
    notify = os.environ.get("NOTIFY_EMAIL", gmail)
    if not gmail or not passwd:
        return
    html = f"""<html><body style="font-family:Arial;background:#111;color:#ddd;padding:30px;max-width:500px">
<h2 style="color:#d63031">🗑 Rejected</h2>
<p>Run <code>{run_id}</code> was rejected. Nothing was uploaded to YouTube.</p>
</body></html>"""
    plain = f"🗑 Video rejected.\n\nRun {run_id} was rejected. Nothing was uploaded to YouTube.\n\n— LLM Shorts"

    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("alternative")
    msg["Subject"]           = "🗑 [LLM Shorts] Video Rejected"
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
    print("  ✓ Rejection email sent")


def delete_release(run_id: str):
    """Delete the GitHub pre-release and its video asset for this run."""
    pat  = os.environ.get("GH_PAT", "")
    repo = os.environ.get("GH_REPO", "")
    if not pat or not repo:
        print("  ⚠ GH_PAT/GH_REPO not set — skipping release cleanup")
        return
    tag = f"review-{run_id}"
    headers = {
        "Authorization":        f"token {pat}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    # Find the release by tag
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases/tags/{tag}",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            release = json.loads(r.read())
        release_id = release["id"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"  ⚠ Release {tag} not found — already deleted or never created")
            return
        print(f"  ⚠ Could not fetch release {tag}: HTTP {e.code}")
        return

    # Delete the release (also removes its assets)
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases/{release_id}",
            headers=headers, method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=30):
            pass
        print(f"  ✓ GitHub Release {tag} deleted")
    except urllib.error.HTTPError as e:
        print(f"  ⚠ Could not delete release {tag}: HTTP {e.code}")

    # Also delete the tag itself
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/git/refs/tags/{tag}",
            headers=headers, method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=30):
            pass
        print(f"  ✓ Tag {tag} deleted")
    except urllib.error.HTTPError as e:
        print(f"  ⚠ Could not delete tag {tag}: HTTP {e.code}")


run_id = os.environ.get("RUN_ID", "")
sig = os.environ.get("SIG", "")

if not verify(run_id, sig):
    print("❌ Invalid signature.")
    sys.exit(1)

print(f"🗑 Rejected run {run_id}. Nothing uploaded to YouTube.")
delete_release(run_id)
send_confirm(run_id)

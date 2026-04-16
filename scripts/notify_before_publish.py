#!/usr/bin/env python3
"""
Send a pre-publish Gmail notification after video generation.

This runs in GitHub Actions before YouTube upload so the owner gets a
"generated, about to publish" email with run details.
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def build_links() -> tuple[str, str]:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    repo = required_env("GITHUB_REPOSITORY")
    run_id = required_env("GITHUB_RUN_ID")
    run_url = f"{server}/{repo}/actions/runs/{run_id}"
    repo_actions_url = f"{server}/{repo}/actions"
    return run_url, repo_actions_url


def send_pre_publish_email(kit: dict, wait_seconds: int) -> None:
    gmail = required_env("GMAIL_ADDRESS")
    password = required_env("GMAIL_APP_PASSWORD")
    notify_to = os.environ.get("NOTIFY_EMAIL", gmail).strip() or gmail
    run_url, actions_url = build_links()

    title = kit.get("title", "Generated video")
    topic = kit.get("topic", "n/a")
    slot = kit.get("slot", "n/a")

    mins = max(1, int(round(wait_seconds / 60.0)))
    subject = f"[YouTube Automation] Generated: {title}"

    plain = (
        "Video generated successfully.\n\n"
        f"Title: {title}\n"
        f"Topic: {topic}\n"
        f"Slot: {slot}\n\n"
        "Please review this run now:\n"
        f"{run_url}\n\n"
        f"Auto-publish is scheduled in ~{mins} minute(s), then YouTube upload starts automatically.\n"
        f"All workflow runs: {actions_url}\n"
    )

    html = f"""\
<html>
  <body style="font-family:Segoe UI,Arial,sans-serif;background:#0f0f14;color:#eaeaea;padding:20px;">
    <div style="max-width:620px;margin:0 auto;background:#1a1a24;padding:20px;border-radius:12px;">
      <h2 style="margin-top:0;">Video Generated (Pre-Publish Check)</h2>
      <p style="line-height:1.5;">
        <b>Title:</b> {title}<br>
        <b>Topic:</b> {topic}<br>
        <b>Slot:</b> {slot}
      </p>
      <p style="line-height:1.5;">
        Review this workflow run now:<br>
        <a href="{run_url}" style="color:#8ab4ff;">{run_url}</a>
      </p>
      <p style="line-height:1.5;">
        Auto-publish will continue in about <b>{mins} minute(s)</b>,
        then upload to YouTube runs automatically.
      </p>
      <p style="line-height:1.5;">
        Actions dashboard: <a href="{actions_url}" style="color:#8ab4ff;">{actions_url}</a>
      </p>
    </div>
  </body>
</html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail
    msg["To"] = notify_to
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(gmail, password)
        server.sendmail(gmail, notify_to, msg.as_string())

    print(f"Pre-publish email sent to {notify_to}")


def main() -> int:
    raw_wait = (os.environ.get("PRE_PUBLISH_WAIT_SECONDS") or "").strip()
    default_wait = int(raw_wait) if raw_wait.isdigit() else 300

    parser = argparse.ArgumentParser()
    parser.add_argument("--kit", default="scripts/output/kit.json", help="Path to kit.json")
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=default_wait,
        help="How long until auto-publish starts",
    )
    args = parser.parse_args()

    with open(args.kit, "r", encoding="utf-8") as f:
        kit = json.load(f)

    send_pre_publish_email(kit, max(0, args.wait_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
One-time OAuth2 helper — get a Google Drive refresh token for CI uploads.

Prereqs:
1. Same Google Cloud project as YouTube (or a new one)
2. Enable "Google Drive API"
3. OAuth client: add http://localhost:8080 to Authorized redirect URIs
4. Run: python scripts/auth_drive.py
5. Add printed secrets to GitHub Actions

Scope is drive.file (only files this app creates) — enough for upload + Zapier delete
when Zapier is connected as the same Google account.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def load_env_file(env_path: Path) -> dict:
    env = {}
    if not env_path.exists():
        return env
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


repo_root = Path(__file__).resolve().parent.parent
env_values = load_env_file(repo_root / ".env")

CLIENT_ID = (
    env_values.get("GOOGLE_DRIVE_CLIENT_ID")
    or env_values.get("YOUTUBE_CLIENT_ID")
    or os.getenv("GOOGLE_DRIVE_CLIENT_ID")
    or os.getenv("YOUTUBE_CLIENT_ID")
    or input("Paste your OAuth client_id: ").strip()
)
CLIENT_SECRET = (
    env_values.get("GOOGLE_DRIVE_CLIENT_SECRET")
    or env_values.get("YOUTUBE_CLIENT_SECRET")
    or os.getenv("GOOGLE_DRIVE_CLIENT_SECRET")
    or os.getenv("YOUTUBE_CLIENT_SECRET")
    or input("Paste your OAuth client_secret: ").strip()
)
SCOPE = "https://www.googleapis.com/auth/drive.file"
REDIRECT_URI = (
    env_values.get("GOOGLE_DRIVE_REDIRECT_URI")
    or env_values.get("YOUTUBE_REDIRECT_URI")
    or os.getenv("GOOGLE_DRIVE_REDIRECT_URI")
    or os.getenv("YOUTUBE_REDIRECT_URI")
    or "http://localhost:8080"
)
AUTH_TIMEOUT_SECONDS = 180

if not CLIENT_ID or not CLIENT_SECRET:
    print("Missing GOOGLE_DRIVE_CLIENT_ID / GOOGLE_DRIVE_CLIENT_SECRET (or YouTube equivalents).")
    sys.exit(1)

auth_url = (
    "https://accounts.google.com/o/oauth2/auth?"
    + urllib.parse.urlencode(
        {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
)

captured_code: list[str] = []
captured_error: list[str] = []


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            captured_code.append(params["code"][0])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>Drive authorized. You can close this tab.</h2>")
        elif "error" in params:
            captured_error.append(params["error"][0])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>Authorization cancelled. You can close this tab.</h2>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No code received.")

    def log_message(self, *args):
        pass


redirect = urllib.parse.urlparse(REDIRECT_URI)
if redirect.hostname not in {"localhost", "127.0.0.1"} or not redirect.port:
    print(f"Unsupported REDIRECT_URI: {REDIRECT_URI} — falling back to http://localhost:8080")
    REDIRECT_URI = "http://localhost:8080"
    redirect = urllib.parse.urlparse(REDIRECT_URI)

server = HTTPServer((redirect.hostname, redirect.port), Handler)
server.timeout = 1
print("\nOpening browser for Google Drive authorization...")
webbrowser.open(auth_url)
print(f"Waiting for redirect to {REDIRECT_URI} (timeout: {AUTH_TIMEOUT_SECONDS}s)...\n")

deadline = time.time() + AUTH_TIMEOUT_SECONDS
try:
    while not captured_code and not captured_error:
        if time.time() >= deadline:
            print("Timed out waiting for Google redirect.")
            sys.exit(1)
        server.handle_request()
except KeyboardInterrupt:
    print("\nAuthorization cancelled (Ctrl+C).")
    sys.exit(130)
finally:
    server.server_close()

if captured_error:
    print(f"Google authorization failed: {captured_error[0]}")
    sys.exit(1)

payload = urllib.parse.urlencode(
    {
        "code": captured_code[0],
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }
).encode()

req = urllib.request.Request(
    "https://oauth2.googleapis.com/token",
    data=payload,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
try:
    with urllib.request.urlopen(req) as r:
        tokens = json.loads(r.read())
except urllib.error.HTTPError as e:
    print(f"Token exchange failed HTTP {e.code}: {e.read().decode()}")
    sys.exit(1)

refresh_token = tokens.get("refresh_token")
if not refresh_token:
    print("No refresh token returned. Re-run with prompt=consent and approve again.")
    sys.exit(1)

print("Success!")
print(f"\nGOOGLE_DRIVE_CLIENT_ID={CLIENT_ID}")
print(f"GOOGLE_DRIVE_CLIENT_SECRET={CLIENT_SECRET}")
print(f"GOOGLE_DRIVE_REFRESH_TOKEN={refresh_token}")
print(
    "\nAlso create a Drive folder (e.g. Publer Inbox), copy its ID from the URL,\n"
    "and set GOOGLE_DRIVE_FOLDER_ID=... in GitHub Secrets."
)
print("Add all four secrets under GitHub → Settings → Secrets → Actions.")

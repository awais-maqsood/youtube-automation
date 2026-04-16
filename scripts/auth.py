#!/usr/bin/env python3
"""
One-time OAuth2 helper — run locally to get your YouTube refresh token.
Uses localhost redirect (replaces the blocked OOB flow).

Steps:
1. Google Cloud Console → APIs & Services → Enable "YouTube Data API v3"
2. OAuth consent screen → Publish App (so token never expires)
3. Credentials → edit your OAuth 2.0 Desktop client
   → add http://localhost:8080 to Authorized redirect URIs → Save
4. Run: python3 scripts/auth.py
5. Browser opens automatically — sign in and approve
6. Terminal prints YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN
7. Add all three to GitHub → Settings → Secrets and variables → Actions
"""

import os
import urllib.request, urllib.parse, json, sys, webbrowser, time
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

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
    env_values.get("YOUTUBE_CLIENT_ID")
    or os.getenv("YOUTUBE_CLIENT_ID")
    or input("Paste your client_id: ").strip()
)
CLIENT_SECRET = (
    env_values.get("YOUTUBE_CLIENT_SECRET")
    or os.getenv("YOUTUBE_CLIENT_SECRET")
    or input("Paste your client_secret: ").strip()
)
SCOPE         = "https://www.googleapis.com/auth/youtube.upload"
REDIRECT_URI  = env_values.get("YOUTUBE_REDIRECT_URI") or os.getenv("YOUTUBE_REDIRECT_URI") or "http://localhost:8080"
AUTH_TIMEOUT_SECONDS = 180

if not CLIENT_ID or not CLIENT_SECRET:
    print("❌ Missing YOUTUBE_CLIENT_ID or YOUTUBE_CLIENT_SECRET.")
    print("   Set them in .env or environment variables, or enter them when prompted.")
    sys.exit(1)

auth_url = (
    "https://accounts.google.com/o/oauth2/auth?"
    + urllib.parse.urlencode({
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         SCOPE,
        "access_type":   "offline",
        "prompt":        "consent",
    })
)

captured_code = []
captured_error = []

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            captured_code.append(params["code"][0])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>Authorized! You can close this tab.</h2>")
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
        pass  # silence request logs


redirect = urllib.parse.urlparse(REDIRECT_URI)
if redirect.hostname not in {"localhost", "127.0.0.1"} or not redirect.port:
    print(f"⚠️ Unsupported REDIRECT_URI: {REDIRECT_URI}")
    print("   Falling back to http://localhost:8080")
    REDIRECT_URI = "http://localhost:8080"
    redirect = urllib.parse.urlparse(REDIRECT_URI)

server = HTTPServer((redirect.hostname, redirect.port), Handler)
server.timeout = 1
print("\n→ Opening browser for authorization...")
webbrowser.open(auth_url)
print(f"  Waiting for Google to redirect back to {REDIRECT_URI} (timeout: {AUTH_TIMEOUT_SECONDS}s) ...\n")

deadline = time.time() + AUTH_TIMEOUT_SECONDS
try:
    while not captured_code and not captured_error:
        if time.time() >= deadline:
            print("❌ Timed out waiting for Google redirect.")
            print("   Re-run auth.py when you're ready to try again.")
            sys.exit(1)
        server.handle_request()
except KeyboardInterrupt:
    print("\n⚠️ Authorization cancelled by user (Ctrl+C).")
    sys.exit(130)
finally:
    server.server_close()

if captured_error:
    print(f"❌ Google authorization failed/cancelled: {captured_error[0]}")
    sys.exit(1)

code = captured_code[0]

payload = urllib.parse.urlencode({
    "code":          code,
    "client_id":     CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "redirect_uri":  REDIRECT_URI,
    "grant_type":    "authorization_code",
}).encode()

req = urllib.request.Request(
    "https://oauth2.googleapis.com/token",
    data=payload,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
try:
    with urllib.request.urlopen(req) as r:
        tokens = json.loads(r.read())
except urllib.error.HTTPError as e:
    print(f"❌ Token exchange failed HTTP {e.code}: {e.read().decode()}")
    sys.exit(1)

print("✅ Success!")
print(f"\nYOUTUBE_CLIENT_ID={CLIENT_ID}")
print(f"YOUTUBE_CLIENT_SECRET={CLIENT_SECRET}")
refresh_token = tokens.get("refresh_token")
if not refresh_token:
    print("❌ No refresh token returned.")
    print("   Make sure prompt=consent is used and approval was completed.")
    sys.exit(1)
print(f"YOUTUBE_REFRESH_TOKEN={refresh_token}")
print("\nAdd these three to GitHub → Settings → Secrets and variables → Actions")

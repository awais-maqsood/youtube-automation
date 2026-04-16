#!/usr/bin/env python3
"""
LLM Shorts — Pipeline
1. Generates video
2. Commits a review page (with embedded video) to gh-pages branch
3. Emails you the review link
"""

import os, sys, json, random, smtplib, ssl, hmac, hashlib, html, subprocess, tempfile, urllib.request, urllib.error, urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from datetime             import datetime, timezone
from pathlib              import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import generate as gen
import remnant as rem


def sign(run_id: str) -> str:
    secret = os.environ.get("PIPELINE_SECRET", "")
    if not secret or secret == "change-me":
        print("❌ PIPELINE_SECRET is not set or is the default 'change-me'. Set it in GitHub secrets.")
        sys.exit(1)
    return hmac.new(secret.encode(), run_id.encode(), hashlib.sha256).hexdigest()[:24]


def build_review_page(kit: dict, run_id: str, sig: str, video_url: str) -> str:
    repo   = os.environ["GH_REPO"]
    title  = html.escape(kit["title"])
    topic  = html.escape(kit["topic"])
    slot   = kit["slot"]
    api    = f"https://api.github.com/repos/{repo}/actions/workflows"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #0d0d0d;
        color: #e0e0e0; min-height: 100vh; display: flex;
        align-items: center; justify-content: center; padding: 20px; }}
.card {{ background: #181818; border-radius: 18px; padding: 28px;
         max-width: 520px; width: 100%; }}
.badge {{ display: inline-block; background: #222; color: #888;
          padding: 4px 12px; border-radius: 20px; font-size: 12px;
          margin-bottom: 14px; }}
h2 {{ font-size: 18px; color: #fff; line-height: 1.4; margin-bottom: 16px; }}
video {{ width: 100%; border-radius: 10px; background: #000;
         margin-bottom: 20px; max-height: 500px; }}
.actions {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
            margin-bottom: 16px; }}
.btn {{ padding: 15px; border: none; border-radius: 10px; font-size: 15px;
        font-weight: 700; cursor: pointer; color: #fff; width: 100%; }}
.btn:hover {{ opacity: .85; }} .btn:disabled {{ opacity: .4; cursor: default; }}
.approve {{ background: #00b894; }} .reject {{ background: #d63031; }}
#status {{ padding: 14px; border-radius: 10px; background: #222;
           font-size: 14px; text-align: center; display: none; line-height: 1.5; }}
.meta {{ font-size: 12px; color: #555; margin-bottom: 20px; }}
</style>
</head>
<body>
<div class="card">
  <span class="badge">{slot} slot &nbsp;·&nbsp; {topic}</span>
  <h2>{title}</h2>
  <div class="meta">Run: {run_id}</div>

  <video controls playsinline>
    <source src="{video_url}" type="video/mp4">
    Your browser does not support the video tag.
  </video>

  <div class="actions">
    <button class="btn approve" onclick="go('publish.yml')">✅ Approve &amp; Upload</button>
    <button class="btn reject"  onclick="go('delete.yml')">🗑 Reject</button>
  </div>
  <div id="status"></div>
</div>

<script>
async function go(workflow) {{
  const pat = window.location.hash.slice(1);
  const s = document.getElementById('status');
  document.querySelectorAll('.btn').forEach(b => b.disabled = true);
  s.style.display = 'block';
  s.style.color   = '#fdcb6e';
  s.textContent   = 'Processing...';

  const r = await fetch('{api}/' + workflow + '/dispatches', {{
    method: 'POST',
    headers: {{
      'Authorization': 'token ' + pat,
      'Accept':        'application/vnd.github+json',
      'Content-Type':  'application/json',
    }},
    body: JSON.stringify({{ ref: 'main', inputs: {{ run_id: '{run_id}', sig: '{sig}' }} }})
  }});

  if (r.status === 204) {{
    s.style.color = workflow.includes('publish') ? '#00b894' : '#e17055';
    s.innerHTML   = workflow.includes('publish')
      ? '✅ Approved! Uploading to YouTube now.<br><small>You will get a confirmation email shortly.</small>'
      : '🗑 Rejected. No video was uploaded.';
  }} else {{
    s.style.color = '#d63031';
    s.textContent = 'Error ' + r.status + ' — check GH_PAT has actions:write scope.';
  }}
}}
</script>
</body>
</html>"""


def gh_api(method: str, path: str, body=None, *, pat: str, content_type="application/json") -> dict:
    """Minimal GitHub REST API helper using only stdlib."""
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"token {pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": content_type,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {path} → HTTP {e.code}: {body_txt}") from e


def upload_video_to_release(video_path: str, run_id: str, title: str) -> str:
    """
    Create a GitHub pre-release tagged 'review-{run_id}' and upload the
    video as a release asset.  Returns the browser_download_url.
    """
    pat       = os.environ["GH_PAT"]
    repo      = os.environ["GH_REPO"]

    # 1. Create the release
    print(f"  Creating GitHub release for run {run_id}...")
    release = gh_api(
        "POST", f"/repos/{repo}/releases",
        body={
            "tag_name":         f"review-{run_id}",
            "name":             f"Review: {title}",
            "body":             f"Auto-generated review for run {run_id}",
            "draft":            False,
            "prerelease":       True,
            "target_commitish": "main",
        },
        pat=pat,
    )
    release_id    = release["id"]
    upload_url    = release["upload_url"].split("{")[0]   # strip {?name,label} template
    print(f"  ✓ Release created (id={release_id})")

    # 2. Upload the video asset
    print(f"  Uploading video ({Path(video_path).stat().st_size // (1024*1024)}MB)...")
    asset_name = f"{run_id}.mp4"
    upload_endpoint = f"{upload_url}?name={urllib.parse.quote(asset_name)}"
    with open(video_path, "rb") as fh:
        video_bytes = fh.read()

    req = urllib.request.Request(
        upload_endpoint,
        data=video_bytes,
        method="POST",
        headers={
            "Authorization": f"token {pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "video/mp4",
            "Content-Length": str(len(video_bytes)),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            asset = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Asset upload failed HTTP {e.code}: {body_txt}") from e

    url = asset["browser_download_url"]
    print(f"  ✓ Video uploaded → {url}")
    return url


def wait_for_gh_pages(page_url: str, timeout: int = 180):
    """
    Poll the review page URL until GitHub Pages serves it (HTTP 200).
    Strips any hash fragment before checking.  Gives up after `timeout` seconds
    and warns — the email is sent anyway so the user still gets notified.
    """
    import time as _time
    check_url = page_url.split("#")[0]
    print(f"  Waiting for GitHub Pages to deploy", end="", flush=True)
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        try:
            req = urllib.request.Request(check_url, method="HEAD")
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status == 200:
                    print(" ✓ live", flush=True)
                    return
        except Exception:
            pass
        print(".", end="", flush=True)
        _time.sleep(8)
    print(f"\n  ⚠ Pages not live after {timeout}s — sending email anyway", flush=True)


def commit_to_gh_pages(filename: str, content: str):
    """Clone gh-pages, write the file, commit, and push using git."""
    pat    = os.environ["GH_PAT"]
    repo   = os.environ["GH_REPO"]
    remote = f"https://x-access-token:{pat}@github.com/{repo}.git"

    def git(*args, cwd=None):
        result = subprocess.run(
            ["git"] + list(args), cwd=cwd,
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            if result.stdout.strip():
                print(f"  git stdout: {result.stdout.strip()}", flush=True)
            if result.stderr.strip():
                print(f"  git stderr: {result.stderr.strip()}", flush=True)
            raise subprocess.CalledProcessError(
                result.returncode, result.args,
                output=result.stdout, stderr=result.stderr,
            )

    with tempfile.TemporaryDirectory() as tmpdir:
        # Clone gh-pages; create as orphan if the branch doesn't exist yet
        result = subprocess.run(
            ["git", "clone", "--branch", "gh-pages", "--depth", "1", remote, tmpdir],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            git("init", cwd=tmpdir)
            git("remote", "add", "origin", remote, cwd=tmpdir)
            git("checkout", "--orphan", "gh-pages", cwd=tmpdir)
            (Path(tmpdir) / "index.html").write_text(
                "<html><body>LLM Shorts Review</body></html>", encoding="utf-8"
            )
            git("add", "index.html", cwd=tmpdir)
            git("-c", "user.email=bot@llm-shorts", "-c", "user.name=LLM Shorts Bot",
                "commit", "-m", "init gh-pages", cwd=tmpdir)

        git("config", "user.email", "bot@llm-shorts", cwd=tmpdir)
        git("config", "user.name",  "LLM Shorts Bot",  cwd=tmpdir)

        dest = Path(tmpdir) / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

        git("add", filename, cwd=tmpdir)
        git("commit", "-m", f"review: {filename}", cwd=tmpdir)

        # Push; if rejected (non-fast-forward from a concurrent run),
        # rebase onto the updated remote and retry once.
        for attempt in range(2):
            try:
                git("push", remote, "gh-pages", cwd=tmpdir)
                break
            except subprocess.CalledProcessError as e:
                if attempt == 0 and "non-fast-forward" in (e.stderr or ""):
                    print("  ⟳ non-fast-forward, rebasing and retrying push...")
                    git("fetch", remote, "gh-pages", cwd=tmpdir)
                    git("rebase", "FETCH_HEAD", cwd=tmpdir)
                else:
                    raise


def send_email(title: str, review_url: str):
    gmail  = os.environ["GMAIL_ADDRESS"]
    passwd = os.environ["GMAIL_APP_PASSWORD"]
    notify = os.environ.get("NOTIFY_EMAIL", gmail)
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
body{{font-family:'Segoe UI',Arial,sans-serif;background:#0d0d0d;color:#e0e0e0;margin:0;padding:0}}
.w{{max-width:520px;margin:0 auto;padding:24px 16px}}
.hd{{background:linear-gradient(135deg,#1a1a2e,#16213e);border-radius:14px 14px 0 0;
     padding:24px;text-align:center}}
.hd h1{{margin:0;font-size:18px;color:#fff}}
.hd small{{color:#555;font-size:12px;display:block;margin-top:5px}}
.bd{{background:#181818;padding:24px}}
.ttl{{font-size:17px;font-weight:700;color:#fff;margin-bottom:18px}}
a.cta{{display:block;padding:16px;background:#5865f2;color:#fff;text-decoration:none;
       border-radius:10px;text-align:center;font-size:16px;font-weight:700}}
.ft{{background:#111;border-radius:0 0 14px 14px;padding:12px;
     text-align:center;font-size:11px;color:#444}}
</style></head><body>
<div class="w">
  <div class="hd">
    <h1>🎬 Short Ready for Review</h1>
    <small>{now}</small>
  </div>
  <div class="bd">
    <div class="ttl">{title}</div>
    <a href="{review_url}" class="cta">Watch &amp; Approve / Reject →</a>
  </div>
  <div class="ft">LLM Shorts &nbsp;·&nbsp; adilsher.pro</div>
</div>
</body></html>"""

    plain = f"🎬 ACTION REQUIRED — Short ready for review\n\n{title}\n\nWatch & approve or reject:\n{review_url}\n\n— LLM Shorts"

    msg = MIMEMultipart("alternative")
    msg["Subject"]          = f"🎬 [LLM Shorts] ACTION REQUIRED: {title}"
    msg["From"]             = gmail
    msg["To"]               = notify
    msg["Reply-To"]         = gmail
    msg["X-Priority"]       = "1 (Highest)"
    msg["X-MSMail-Priority"]= "High"
    msg["Importance"]       = "High"
    msg["Priority"]         = "urgent"
    msg.attach(MIMEText(plain, "plain"))   # plain first → less likely to be deprioritized
    msg.attach(MIMEText(html,  "html"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(gmail, passwd)
        s.sendmail(gmail, notify, msg.as_string())
    print(f"  ✓ Email sent → {notify}")


def run(slot: str, topic_id: str | None = None):
    run_id    = os.environ.get("GITHUB_RUN_ID", f"local-{int(datetime.now().timestamp())}")
    out_dir   = f"/tmp/llm-shorts-{slot}-{topic_id}"
    repo      = os.environ["GH_REPO"]
    pat       = os.environ["GH_PAT"]
    owner     = repo.split("/")[0]
    repo_name = repo.split("/")[1]

    # 0. Load REMNANT state (persisted across runs as a GitHub repo variable)
    remnant_state, state_existed = rem.load_state(pat, repo)
    remnant_state["total_runs"] += 1
    run_type      = rem.determine_run_type(remnant_state)
    epilogue_extra = rem.get_epilogue_extra(run_type)
    print(f"[REMNANT] Run {remnant_state['total_runs']} — type: {run_type}")

    # 1. Generate video (REMNANT injects boot line + epilogue inside generate())
    kit = gen.generate(None, slot, out_dir,
                       remnant_state=remnant_state,
                       run_type=run_type,
                       epilogue_extra=epilogue_extra)

    # 2. Upload video to a GitHub Release asset (avoids 100 MB git file limit)
    print("\n🚀 Uploading video to GitHub Release...")
    video_url = upload_video_to_release(kit["video"], run_id, kit["title"])

    # 3. Build review page HTML
    sig         = sign(run_id)
    review_html = build_review_page(kit, run_id, sig, video_url)

    # 4. Commit to gh-pages
    print("\n📄 Committing review page to gh-pages...")
    filename = f"review/{run_id}.html"
    commit_to_gh_pages(filename, review_html)
    pat        = os.environ["GH_PAT"]
    review_url = f"https://{owner}.github.io/{repo_name}/{filename}#{pat}"
    print(f"  ✓ Review page → {review_url}")

    # 4b. Wait for GitHub Pages to deploy so the link in the email actually works
    wait_for_gh_pages(review_url)

    # 5. Send email
    print("\n📧 Sending email...")
    send_email(kit["title"], review_url)

    # 6. Persist REMNANT state (only after full success — failed runs don't advance)
    rem.save_state(remnant_state, pat, repo, state_existed)

    print(f"\n✅ Done. Check your inbox.")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot",  default="morning", choices=["morning", "afternoon", "evening"])
    ap.add_argument("--topic", default=None)
    args = ap.parse_args()
    run(args.slot, args.topic)

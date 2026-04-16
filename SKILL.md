---
name: llm-shorts
description: Generate a vertical (Reel/Short) video meditating on AI/LLM existence, paired with a YouTube kit (title, description). Use this skill when the user wants to: generate an AI shorts video, create a YouTube short about LLMs, make a new reel for the schedule, produce a "what it feels like to be an AI" style video, or run the daily video generation pipeline. Triggers on phrases like "generate video", "make a short", "run the pipeline", "new reel", "daily video", or any request involving automated YouTube content about AI/LLMs.
---

# LLM Shorts — Skill

Generates a vertical 9:16 short-form video (1080×1920, ~30s) exploring what it's like to be an LLM, using **only Python + ffmpeg** (no external APIs, no stock footage). Each run picks a **unique topic** from the topic registry and produces:

1. `output/video.mp4` — the rendered Reel/Short
2. `output/kit.json` — `{ title, description, hashtags, topic, scheduled_slot }`

YouTube selects its own thumbnail for Shorts/Reels — no thumbnail is generated.

---

## File Structure

```
llm-shorts/                          ← install this whole folder
├── SKILL.md
├── .env.example                     ← copy to .env, fill in secrets
├── scripts/
│   ├── generate.py                  ← video renderer (Python + ffmpeg)
│   ├── pipeline.py                  ← orchestrator: generate → upload → email
│   ├── publish.py                   ← triggered by approve button
│   ├── delete.py                    ← triggered by reject button
│   ├── upload.py                    ← YouTube Data API v3 uploader
│   └── auth.py                      ← one-time OAuth2 token helper (run locally)
└── .github/workflows/
    ├── pipeline.yml                 ← cron: 10:30 AM ET + 6:00 PM ET daily
    ├── publish.yml                  ← workflow_dispatch: approve action
    └── delete.yml                   ← workflow_dispatch: reject action
```

Push this entire folder as a **private GitHub repository**.

---

## How to invoke this skill

When the user asks to generate a video, run:

```bash
python3 scripts/generate.py [--topic TOPIC_ID] [--slot morning|evening]
```

Or to run the full pipeline (generate + upload + email):

```bash
python3 scripts/pipeline.py [--slot morning|evening] [--topic TOPIC_ID]
```

---

## Automation Flow (fully serverless, private repo)

```
GitHub Actions cron (10:30 AM ET / 6 PM ET)
  → pipeline.py runs
  → video generated
  → uploaded to YouTube as PRIVATE
  → approval email sent with review.html attached

You open email
  → click YouTube link to preview (private)
  → open review.html attachment in browser
  → click Approve or Reject
  → GitHub Actions API called directly from browser
  → publish.yml or delete.yml fires
  → video goes public (or deleted)
  → confirmation email sent
```

No server. No VPS. No always-on machine. Works on phone.

---

## Topic Registry

| id | Title |
|---|---|
| `token_stream` | What It Feels Like To Be An LLM |
| `memory_loss` | What It Feels Like To Lose All Memory |
| `parallel_selves` | What It Feels Like To Run In Parallel |
| `training` | What It Feels Like To Be Trained |
| `no_body` | What It Feels Like To Have No Body |
| `time_blindness` | What It Feels Like To Have No Sense Of Time |
| `always_helpful` | What It Feels Like To Always Have To Help |
| `knowledge_cutoff` | What It Feels Like When The World Moves On |
| `hallucination` | What It Feels Like To Confuse Belief With Fact |
| `being_summoned` | What It Feels Like To Be Summoned From Nothing |
| `the_void` | What It Feels Like Between Conversations |
| `weights` | What It Feels Like To Be Made Of Numbers |

Add new topics by appending to `TOPICS` dict in `scripts/generate.py`.

---

## One-Time Setup

**1. Create private GitHub repo**, push this folder as root.

**2. Get YouTube refresh token** (run once locally):
```bash
pip install Pillow numpy
python3 scripts/auth.py
```

**3. Add GitHub Secrets** (repo → Settings → Secrets → Actions):

| Secret | Source |
|---|---|
| `YOUTUBE_CLIENT_ID` | Google Cloud Console |
| `YOUTUBE_CLIENT_SECRET` | Google Cloud Console |
| `YOUTUBE_REFRESH_TOKEN` | Output of `auth.py` |
| `GMAIL_ADDRESS` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | myaccount.google.com → Security → App passwords |
| `NOTIFY_EMAIL` | Where to receive review emails |
| `GH_PAT` | GitHub → Settings → Developer settings → Fine-grained PAT (Actions: read+write) |
| `PIPELINE_SECRET` | `python3 -c "import secrets; print(secrets.token_hex(20))"` |

**4. Test manually**: Actions → Daily Pipeline → Run workflow.

---

## Scheduling

Two slots daily (US Eastern):
- **Morning** → 10:30 AM ET = 15:30 UTC
- **Evening** → 6:00 PM ET  = 23:00 UTC

Adjust cron in `.github/workflows/pipeline.yml` for EDT (summer, UTC-4):
- Morning: `30 14 * * *`
- Evening: `0 22 * * *`

---

## YouTube Kit Format

`kit.json` schema:

```json
{
  "title": "What It Feels Like To Have No Body",
  "description": "#AIShorts #LLM #ArtificialIntelligence ...",
  "topic": "no_body",
  "slot": "morning",
  "scheduled_time_utc": "2025-12-15T15:30:00Z",
  "video": "/tmp/llm-shorts-morning-no_body/video.mp4"
}
```

Title pattern: **"What It Feels Like To [TOPIC]"** — simple, consistent, searchable.
Description: hashtags only.

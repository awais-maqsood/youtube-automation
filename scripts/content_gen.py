#!/usr/bin/env python3
"""
content_gen.py
Calls OpenRouter to build a topical short-form explainer package from
the latest Google Trends RSS topic.

Required env var: OPENROUTER_API_KEY
"""

import os, json, random, time, re, urllib.request, urllib.error, xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone

API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
GOOGLE_TRENDS_RSS = "https://trends.google.com/trending/rss?geo={geo}"


def load_env_file(env_path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not env_path.exists():
        return env
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


ENV_FILE_VALUES = load_env_file(Path(__file__).resolve().parent.parent / ".env")
FALLBACK_LOG_PATH = Path(__file__).resolve().parent.parent / "output" / "fallback_stats.json"
SEED_HISTORY_PATH = Path(__file__).resolve().parent.parent / "output" / "topic_seed_history.json"


def env_value(name: str, default: str = "") -> str:
    return os.environ.get(name) or ENV_FILE_VALUES.get(name, default)

# Models tried in order — first available wins. All are free-tier on OpenRouter.
# Using multiple providers so a single upstream outage doesn't block every run.
MODELS = [
    "arcee-ai/trinity-large-preview:free",      # 400B instruction model — most reliable lately
    "stepfun/step-3.5-flash:free",              # 196B MoE — fast; inline <think> blocks stripped by re.sub
    "nvidia/nemotron-3-super-120b-a12b:free",  # 120B hybrid MoE — last resort
]

ANGLES = [
    "app safety verdict",
    "phone hype vs reality",
    "AI feature truth check",
    "tech comparison short",
    "viral gadget quick review",
]

CHANNEL_FIT_KEYWORDS = [
    "app", "apps", "iphone", "pixel", "oneplus", "samsung", "android", "ios",
    "ai", "camera", "review", "pro", "max", "ultra", "smartphone", "phone",
    "chatgpt", "gemini", "openai", "google", "meta", "whatsapp", "instagram",
    "tiktok", "youtube", "movie", "netflix", "box", "feature", "update",
    "launch", "chip", "processor", "battery", "macbook", "windows", "galaxy",
]

# Used when Trends RSS has no channel-fit row. Rotated (see pick_rotated_channel_fit_fallback)
# so the same seed is not picked run after run. Avoid niche piracy-app names that read as spam.
CHANNEL_FIT_FALLBACKS = [
    "pixel camera",
    "iphone ai features",
    "oneplus review",
    "chatgpt update",
    "whatsapp new feature",
    "samsung galaxy ai update",
    "google gemini android features",
    "macbook air m4 battery life",
    "windows 11 new ai feature",
    "playstation plus price change",
    "netflix password sharing rules",
    "spotify hi-fi audio update",
    "meta quest 3 games",
    "tiktok algorithm change rumor",
]

OPENAI_TOPIC_SELECTOR_SYSTEM = """You select the best YouTube Shorts topic for a channel like BlinkViral.

Channel style:
- apps, phones, AI, gadgets, internet tools
- safety checks, hype vs reality, review hooks, comparisons
- light Hinglish/Urdu phrasing is acceptable
- prefer topics that can become titles like:
  - "X - Is It Safe? The TRUTH"
  - "X ... hype ya reality?"
  - "X Review: Worth It?"
  - "X vs Y: The NEW King?"

Return ONLY valid JSON:
{
  "selected_topic": "string",
  "reason": "short reason",
  "search_query": "specific image/topic search query"
}
"""

SYSTEM_PROMPT = """You write 30-second vertical YouTube Shorts for a channel like BlinkViral.

The short should feel topical, fast, bold, and highly clickable.
Do NOT write fiction, horror, haunted internet stories, creepypasta, or made-up events.
Base everything on the supplied trending topic and keep the wording broad enough to avoid invented facts.

The video has 5 acts:
1. HOOK — a strong first-line headline about the topic
2. CONTEXT — 3 short lines that explain what it is
3. WHY — 3 short lines explaining why people care
4. QUESTION — one open-loop question plus 6 to 8 rapid short captions
5. CLOSE — 2 or 3 short lines wrapping up with a verdict/question vibe

Style rules:
- Write like a viral tech/app/product explainer short, not a documentary.
- Use a mix of simple English with light Hinglish/Urdu phrasing where natural.
- Focus on apps, phones, AI, gadgets, reviews, safety, comparisons, hype, and internet products.
- Be punchy, simple, broad, and readable on screen.
- Avoid unverifiable specifics unless the topic itself is widely understood from the trend phrase.
- Prefer title patterns like:
  - "X - Is It Safe? The TRUTH"
  - "X vs Y: The NEW King?"
  - "X ... hype ya reality?"
  - "X real hai... ya AI illusion?"
  - "X Review: Worth It?"
- Hook: 4-10 words.
- Context lines: 3 short lines, max 8 words each.
- Why lines: 3 short lines, max 8 words each.
- Question: 4-10 words.
- Captions: 6 to 8 items, max 5 words each, energetic but factual.
- Close lines: 2 or 3 short lines, reflective or curiosity-driven.
- Title: clickable, topic-first, channel-style, max 12 words.

Palette: pick 3 RGB colors that match a modern topical Shorts aesthetic.
Use strong contrast and readability.

Respond ONLY with valid JSON. No markdown fences. No explanation."""


def make_prompt(latest_topic: str, epilogue_extra: str | None = None) -> str:
    angle = random.choice(ANGLES)
    base = f"""Create a 30-second BlinkViral-style YouTube Short package.

Current trending topic: {latest_topic}
Content angle: {angle}

The output must be directly about this exact trend phrase.
If the trend is not obviously tech/app/product related, reinterpret it through the internet/app/AI/device angle only if that still feels natural.
Prefer app safety, phone comparison, AI feature, camera, review, or hype/reality framing.
Do not turn it into fiction.
Do not invent a spooky backstory.
Do not convert it into a haunted-tech metaphor.
Use the exact trend phrase "{latest_topic}" inside the title OR hook OR one context line.

Return this exact JSON:
{{
  "title": "BlinkViral-style clickable title with Hinglish/tech-review vibe",
  "topic_id": "snake_case_identifier",
  "palette": [[r,g,b], [r,g,b], [r,g,b]],
  "hook": "4-10 word opening headline",
  "context_lines": ["3 short lines", "max 8 words each", "directly about the trend"],
  "why_lines": ["3 short lines", "why people care", "max 8 words each"],
  "question": "4-10 word open loop question",
  "captions": [["CAPTION", [r,g,b]], "... 6 to 8 total"],
  "close_lines": ["2 or 3 short closing lines"],
  "search_query": "specific stock-image search phrase for Freepik"
}}"""
    if epilogue_extra:
        base += f"\n\nEpilogue instruction: {epilogue_extra}"
    return base


def fetch_latest_topics(max_items: int = 20) -> list[str]:
    """
    Fetch current Google Trends topics from RSS.
    Falls back silently if network/feed parsing fails.
    """
    geo = env_value("TREND_GEO", "US").strip().upper() or "US"
    url = GOOGLE_TRENDS_RSS.format(geo=geo)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; llm-shorts/1.0)",
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()

    root = ET.fromstring(raw)
    topics: list[str] = []
    for item in root.findall(".//item"):
        t = (item.findtext("title") or "").strip()
        if not t:
            continue
        # Remove hash-prefixes and extra separators for cleaner prompt context.
        t = re.sub(r"\s*#\w+", "", t).strip()
        t = re.sub(r"\s*[|:]\s*", " - ", t)
        if t and t not in topics:
            topics.append(t)
        if len(topics) >= max_items:
            break
    return topics


def _is_usable_topic(topic: str) -> bool:
    t = (topic or "").strip()
    if len(t) < 4:
        return False
    if t in {"...", "unknown", "n/a", "null"}:
        return False
    if not re.search(r"[A-Za-z0-9]", t):
        return False
    return True


def _is_channel_fit_topic(topic: str) -> bool:
    t = (topic or "").lower()
    tokens = re.findall(r"[a-z0-9]+", t)
    if not tokens:
        return False
    token_set = set(tokens)
    multiword_matches = [
        "stock price", "new feature", "movie box", "chatgpt update",
        "pixel camera", "iphone ai", "phone camera",
    ]
    if any(phrase in t for phrase in multiword_matches):
        return True
    return any(k in token_set for k in CHANNEL_FIT_KEYWORDS)


def select_topic_with_openai(topics: list[str]) -> tuple[str | None, str | None]:
    api_key = env_value("OPENAI_API_KEY", "").strip()
    if not api_key or not topics:
        return None, None

    payload = json.dumps(
        {
            "model": "gpt-4.1-mini",
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": OPENAI_TOPIC_SELECTOR_SYSTEM},
                {
                    "role": "user",
                    "content": "Choose the best topic from this live list for BlinkViral style:\n"
                    + json.dumps(topics, ensure_ascii=False),
                },
            ],
        }
    ).encode()

    req = urllib.request.Request(
        OPENAI_API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            resp = json.loads(r.read())
        raw = resp["choices"][0]["message"]["content"].strip()
        data = json.loads(raw)
        topic = (data.get("selected_topic") or "").strip()
        query = (data.get("search_query") or "").strip()
        if topic:
            return topic, query or None
    except Exception as e:
        print(f"  OpenAI topic selector failed: {e}")
    return None, None


def pick_rotated_channel_fit_fallback() -> str:
    """Pick a synthetic seed while avoiding the last few picks (reduces duplicate titles)."""
    try:
        SEED_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        recent: list[str] = []
        if SEED_HISTORY_PATH.exists():
            data = json.loads(SEED_HISTORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                recent = [str(x) for x in data.get("recent_seeds", []) if str(x).strip()]
        avoid = set(s.lower() for s in recent[-4:])
        pool = [s for s in CHANNEL_FIT_FALLBACKS if s.lower() not in avoid]
        if not pool:
            pool = list(CHANNEL_FIT_FALLBACKS)
        choice = random.choice(pool)
        recent.append(choice)
        SEED_HISTORY_PATH.write_text(
            json.dumps({"recent_seeds": recent[-32:]}, indent=2),
            encoding="utf-8",
        )
        return choice
    except Exception as e:
        print(f"  [WARN] Seed rotation unavailable: {e}")
        return random.choice(CHANNEL_FIT_FALLBACKS)


def pick_latest_topic() -> tuple[str, str | None]:
    """Pick a topic from RSS, preferring OpenAI-ranked channel-fit topics when available."""
    try:
        topics = fetch_latest_topics()
        if topics:
            preview = ", ".join(topics[:5])
            print(f"  RSS top topics: {preview}")
            chosen, search_query = select_topic_with_openai(topics[:10])
            if chosen:
                print(f"  Trending topic seed: {chosen} (OpenAI-selected)")
                return chosen, search_query
            for candidate in topics:
                if _is_usable_topic(candidate) and _is_channel_fit_topic(candidate):
                    print(f"  Trending topic seed: {candidate} (latest channel-fit)")
                    return candidate, None
            print("  No channel-fit topic in RSS; using channel-fit fallback.")
        print("  Trending topic feed empty; using synthetic angle seed.")
    except Exception as e:
        print(f"  Trending topic fetch failed: {e}")
    fallback_topic = pick_rotated_channel_fit_fallback()
    print(f"  Trending topic seed: {fallback_topic} (channel-fit fallback)")
    return fallback_topic, None


def call_llm(prompt: str, model: str) -> dict:
    api_key = env_value("OPENROUTER_API_KEY", "")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY not set")

    payload = json.dumps(
        {
            "model": model,
            "temperature": 1.0,
            "max_tokens": 1200,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
    ).encode()

    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM HTTP {e.code} {e.reason}: {body}") from e

    msg = resp["choices"][0]["message"]
    raw = (msg.get("content") or msg.get("reasoning") or "").strip()
    if not raw:
        raise RuntimeError("Model returned empty response")
    # Strip <think>...</think> reasoning blocks (some models include them inline in content)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    # Strip markdown fences if model adds them
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(raw)


def validate(content: dict) -> dict:
    """Ensure all required fields exist and have correct types."""
    if not content.get("title"):
        content["title"] = "This App - Is It Safe?"

    if not content.get("topic_id"):
        content["topic_id"] = "unknown"

    pal = content.get("palette", [])
    while len(pal) < 3:
        pal.append([random.randint(140, 255), random.randint(120, 220), random.randint(80, 200)])
    content["palette"] = [[max(0, min(255, int(v))) for v in p] for p in pal[:3]]

    if not content.get("hook"):
        content["hook"] = content["title"]

    context_lines = [str(x) for x in content.get("context_lines", []) if str(x).strip()]
    while len(context_lines) < 3:
        context_lines.append("People are paying attention.")
    content["context_lines"] = context_lines[:3]

    why_lines = [str(x) for x in content.get("why_lines", []) if str(x).strip()]
    while len(why_lines) < 3:
        why_lines.append("It is trending across feeds.")
    content["why_lines"] = why_lines[:3]

    if not content.get("question"):
        content["question"] = "Worth it... ya skip?"

    caps = content.get("captions", [])
    fixed = []
    for cap in caps:
        if isinstance(cap, list) and len(cap) == 2:
            text = str(cap[0])
            color = (
                cap[1]
                if isinstance(cap[1], list) and len(cap[1]) == 3
                else [255, 255, 255]
            )
            fixed.append([text, [max(0, min(255, int(v))) for v in color]])
    while len(fixed) < 6:
        fixed.append(["TRENDING NOW", [255, 255, 255]])
    content["captions"] = fixed[:8]

    close_lines = [str(x) for x in content.get("close_lines", []) if str(x).strip()]
    while len(close_lines) < 2:
        close_lines.append("This trend is moving fast.")
    content["close_lines"] = close_lines[:3]

    if not content.get("search_query"):
        content["search_query"] = str(content.get("search_query") or content.get("title", "")).strip() or "smartphone app ai technology"

    return _normalize_display_text(content)


def _compact_ws(s: str) -> str:
    return " ".join(s.split()).strip()


def _normalize_display_text(content: dict) -> dict:
    """
    Reduce ugly duplicates between title/hook and accidental doubled clauses,
    which also blow up on-screen line length in Shorts.
    """
    title = _compact_ws(str(content.get("title", "")))
    hook = _compact_ws(str(content.get("hook", "")))

    if title and hook:
        tl, hl = title.lower(), hook.lower()
        if tl == hl or tl in hl or hl in tl:
            if len(hook) <= len(title):
                content["hook"] = "Quick breakdown + safety check."
            else:
                content["title"] = hook
                content["hook"] = "Yeh trend abhi viral hai — detail dekho."

    title = _compact_ws(str(content.get("title", "")))
    low = title.lower()
    half = max(8, len(low) // 2)
    if len(low) >= 24 and low[:half] == low[half : half * 2]:
        content["title"] = _compact_ws(title[: len(title) // 2])

    return content


_APP_TITLE_SUFFIXES = [
    "Is It Safe?",
    "Scam Ya Real?",
    "Install Karein?",
    "The Honest Truth",
    "Kya Scene Hai?",
    "Hype Ya Reality?",
]


def _fallback_title_from_topic(latest_topic: str) -> str:
    topic = _compact_ws(latest_topic or "").strip(" -:")
    if not topic:
        return "Trending Topic - Hype Ya Reality?"
    topic_l = topic.lower()
    if "vs" in topic_l or " vs " in topic_l:
        return f"{topic} - Who Wins?"
    if "price" in topic_l or "stock" in topic_l:
        return f"{topic} - What Just Happened?"
    if "feature" in topic_l or "update" in topic_l:
        return f"{topic} - Worth It?"
    if "app" in topic_l or "apk" in topic_l:
        suf = random.choice(_APP_TITLE_SUFFIXES)
        return f"{topic} - {suf}"
    return f"{topic} - Hype Ya Reality?"


def fallback_for_topic(latest_topic: str) -> dict:
    """
    Build a topic-anchored fallback so failed model runs do not keep reusing
    an unrelated static title like "Movie Box App".
    """
    lt = _compact_ws(latest_topic or "").strip()
    base = random.choice(_FALLBACK_POOL).copy()
    focus = lt or "this trend"
    base["title"] = _fallback_title_from_topic(lt)
    base["hook"] = f"{focus}... kya scene hai?"
    base["context_lines"] = [
        focus,
        "Feeds pe yeh topic viral hai.",
        "Log details samajhna chahte hain.",
    ]
    base["why_lines"] = [
        "Opinions online split ho rahe hain.",
        "Search interest fast grow kar raha hai.",
        "Har koi quick verdict chahta hai.",
    ]
    base["question"] = "Real story kya hai?"
    base["captions"] = [
        [focus[:42], [255, 255, 255]],
        ["TREND ALERT", [255, 220, 120]],
        ["HYPE YA REAL?", [255, 150, 150]],
        ["QUICK BREAKDOWN", [255, 255, 255]],
        ["FACTS CHECK", [255, 220, 120]],
        ["FINAL VERDICT?", [255, 150, 150]],
    ]
    base["close_lines"] = [
        "Trend tez hai, details check zaroor karo.",
        "Aapke hisaab se hype ya reality?",
    ]
    if lt:
        base["search_query"] = lt
    base["trend_topic"] = lt
    return base


def _track_fallback_usage(latest_topic: str, reason: str) -> None:
    """
    Persist fallback usage stats locally so recurring model failures are visible.
    Best-effort only; should never break generation flow.
    """
    try:
        FALLBACK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if FALLBACK_LOG_PATH.exists():
            stats = json.loads(FALLBACK_LOG_PATH.read_text(encoding="utf-8"))
            if not isinstance(stats, dict):
                stats = {}
        else:
            stats = {}
        now = datetime.now(timezone.utc).isoformat()
        stats["count"] = int(stats.get("count", 0)) + 1
        stats["last_used_utc"] = now
        stats["last_topic"] = _compact_ws(latest_topic or "") or "unknown"
        stats["last_reason"] = _compact_ws(reason or "") or "all_models_failed"
        history = stats.get("history", [])
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "used_at_utc": now,
                "topic": stats["last_topic"],
                "reason": stats["last_reason"],
            }
        )
        stats["history"] = history[-25:]
        FALLBACK_LOG_PATH.write_text(json.dumps(stats, indent=2), encoding="utf-8")
        print(
            f"  [WARN] Fallback usage count: {stats['count']} "
            f"(stats: {FALLBACK_LOG_PATH.as_posix()})"
        )
    except Exception as e:
        print(f"  [WARN] Could not persist fallback stats: {e}")


def fallback() -> dict:
    """Return one of several pre-written topics so repeated LLM failures don't produce identical videos."""
    return random.choice(_FALLBACK_POOL)


_FALLBACK_POOL = [
    {
        "title": "Third-Party APKs - What To Know",
        "topic_id": "trend_everywhere",
        "palette": [[255, 235, 120], [255, 120, 120], [255, 255, 255]],
        "hook": "Random APKs... safe?",
        "context_lines": ["People sideload apps for free features.", "Permissions can be aggressive.", "Security teams keep flagging clones."],
        "why_lines": ["One bad install can leak data.", "Clones mimic popular brands.", "Shortcuts feel tempting anyway."],
        "question": "Install karein... ya skip?",
        "captions": [
            ["CHECK SOURCE", [255, 255, 255]],
            ["PERMISSIONS MATTER", [255, 220, 120]],
            ["APK RISKS", [255, 150, 150]],
            ["DATA SAFE?", [255, 255, 255]],
            ["RED FLAGS", [255, 220, 120]],
            ["STAY CAREFUL", [255, 150, 150]],
        ],
        "close_lines": ["Free cheez tempting hoti hai.", "Lekin verify pehle, install baad mein."],
        "search_query": "smartphone security apk warning",
    },
    {
        "title": "Pixel vs iPhone - Camera King?",
        "topic_id": "everyone_talking_about",
        "palette": [[120, 220, 255], [255, 180, 120], [255, 255, 255]],
        "hook": "Pixel ya iPhone... camera king?",
        "context_lines": ["Both phones look premium.", "Camera battle is getting serious.", "AI edits change everything."],
        "why_lines": ["People want better photos.", "Social media loves camera tests.", "AI is changing the game."],
        "question": "Real winner kaun hai?",
        "captions": [
            ["PIXEL VS IPHONE", [255, 255, 255]],
            ["CAMERA TEST TIME", [120, 220, 255]],
            ["AI BHI FACTOR HAI", [255, 180, 120]],
            ["DETAILS MATTER", [255, 255, 255]],
            ["NIGHT SHOTS COUNT", [120, 220, 255]],
            ["NEW CAMERA KING?", [255, 180, 120]],
        ],
        "close_lines": ["Specs alag cheez hain.", "Real winner camera test batata hai."],
        "search_query": "smartphone camera comparison pixel iphone",
    },
]


def generate_topic(epilogue_extra: str | None = None) -> dict:
    """Generate a completely fresh topic and all content via OpenRouter."""
    key = env_value("OPENROUTER_API_KEY", "")
    print(f"  OPENROUTER_API_KEY: {'SET (' + key[:8] + '...)' if key else 'NOT SET'}")
    latest_topic, selected_search_query = pick_latest_topic()
    prompt = make_prompt(latest_topic, epilogue_extra)
    for i, model in enumerate(MODELS):
        try:
            print(f"  Generating topic (model: {model})...")
            content = call_llm(prompt, model)
            content = validate(content)
            if selected_search_query and not content.get("search_query"):
                content["search_query"] = selected_search_query
            # Hard anchor: keep output clearly attached to the real trend.
            trend_l = latest_topic.lower()
            title = str(content.get("title", ""))
            hook = str(content.get("hook", ""))
            context_lines = [str(x) for x in content.get("context_lines", [])]
            captions = content.get("captions", [])
            has_trend = (
                trend_l in title.lower()
                or trend_l in hook.lower()
                or any(trend_l in line.lower() for line in context_lines)
                or any(trend_l in str(cap[0]).lower() for cap in captions if isinstance(cap, list) and cap)
            )
            if not has_trend:
                content["title"] = f"{latest_topic}: {title}".strip(": ")
                content["hook"] = latest_topic
                if context_lines:
                    context_lines[0] = latest_topic
                else:
                    context_lines = [latest_topic, "People are talking about it.", "Here is the quick context."]
                content["context_lines"] = context_lines[:3]
                if isinstance(captions, list) and captions:
                    first = captions[0]
                    if isinstance(first, list) and len(first) == 2:
                        first[0] = latest_topic[:42]
                        captions[0] = first
                        content["captions"] = captions
            content = _normalize_display_text(content)
            content["trend_topic"] = latest_topic
            if selected_search_query:
                content["search_query"] = selected_search_query
            print(f"  Topic: '{content['title']}'")
            print(f"  Question: '{content['question']}'")
            return content
        except Exception as e:
            print(f"  {model} failed: {e}")
            if i < len(MODELS) - 1:
                print("  Waiting 5s before trying next model...")
                time.sleep(5)
    print("  All models failed. Using fallback content.")
    _track_fallback_usage(latest_topic, "all_models_failed")
    content = fallback_for_topic(latest_topic)
    if selected_search_query:
        content["search_query"] = selected_search_query
    return validate(content)


if __name__ == "__main__":
    import sys

    content = generate_topic()
    print(json.dumps(content, indent=2))

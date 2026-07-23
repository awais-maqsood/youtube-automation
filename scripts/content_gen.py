#!/usr/bin/env python3
"""
content_gen.py
Calls OpenRouter to build a topical short-form explainer package from
the latest Google Trends RSS topic.

Required env var: OPENROUTER_API_KEY
"""

import os, json, random, time, re, hashlib, urllib.request, urllib.error, xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone

from topic_validation import (
    EntityValidationError,
    assert_publishable_title,
    contains_invalid_publish_text,
    is_valid_entity,
    log_entity_rejection,
    normalize_entity,
    require_valid_entity,
)

API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
GOOGLE_TRENDS_RSS = "https://trends.google.com/trending/rss?geo={geo}"
# Namespace for the ht:* demand fields already present in the Trends RSS payload.
GOOGLE_TRENDS_HT_NS = "https://trends.google.com/trending/rss"


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
# Multiple providers so a single 429/outage doesn't force the generic fallback.
# Verified live against the OpenRouter /models endpoint (the previous slugs had
# been delisted and 404'd every run, which is why every video used canned text).
MODELS = [
    "openai/gpt-oss-120b:free",                 # clean JSON, strong instruction following
    "meta-llama/llama-3.3-70b-instruct:free",   # excellent when not rate-limited
    "qwen/qwen3-next-80b-a3b-instruct:free",    # solid instruct fallback
    "z-ai/glm-4.5-air:free",                     # returns fenced JSON (fences stripped below)
    "google/gemma-4-31b-it:free",               # last-resort, still capable
]

# 4 videos/day: morning + afternoon = high-CPM niches; evening + night = viral trends.
HIGH_CPM_SLOTS = {"morning", "afternoon"}

VIRAL_ANGLES = [
    "breaking update explainer",
    "what changed and why",
    "quick fact-check breakdown",
    "viral moment context",
    "controversy timeline",
]

HIGH_CPM_ANGLES = [
    "money impact explainer",
    "tool vs alternative comparison",
    "hidden cost / fee breakdown",
    "beginner mistake warning",
    "ROI / productivity payoff",
]

VIRAL_KEYWORDS = [
    "viral", "trending", "breaking", "update", "controversy", "reaction",
    "review", "launch", "release", "policy", "election", "celebrity",
    "netflix", "movie", "series", "music", "youtube", "instagram", "tiktok",
    "interview", "leak", "scam", "warning", "ban", "lawsuit", "game",
    "gaming", "esports", "iphone", "android", "feature",
]

# Advertiser-friendly niches: AI tools, finance, SaaS, business, insurance, credit.
HIGH_CPM_KEYWORDS = [
    "ai", "chatgpt", "openai", "claude", "gemini", "copilot", "saas",
    "software", "startup", "business", "finance", "investing", "investment",
    "stock", "stocks", "market", "economy", "inflation", "interest", "rate",
    "mortgage", "loan", "credit", "creditcard", "insurance", "tax", "taxes",
    "crypto", "bitcoin", "ethereum", "banking", "budget", "salary", "income",
    "sidehustle", "marketing", "productivity", "automation", "pricing",
    "subscription", "refund", "layoff", "hiring", "remote", "freelancer",
    "apple", "google", "microsoft", "amazon", "tesla", "nvidia",
]

VIRAL_FALLBACKS = [
    "celebrity interview controversy",
    "streaming platform new release",
    "viral social media challenge",
    "new smartphone launch reaction",
    "policy change public reaction",
    "breaking entertainment headline",
    "internet debate going viral",
    "app feature rollout reaction",
]

HIGH_CPM_FALLBACKS = [
    "ChatGPT new feature pricing",
    "best AI tools for freelancers",
    "credit score mistake to avoid",
    "high yield savings rate update",
    "SaaS subscription cost trap",
    "mortgage rate change explained",
    "side hustle tax rules beginners",
    "insurance claim denial reasons",
    "stock market volatility explained",
    "AI coding tool ROI for startups",
    "personal finance budget reset",
    "business software pricing war",
]

# Backward-compatible aliases used by older helpers.
ANGLES = VIRAL_ANGLES
CHANNEL_FIT_KEYWORDS = VIRAL_KEYWORDS + HIGH_CPM_KEYWORDS
CHANNEL_FIT_FALLBACKS = VIRAL_FALLBACKS + HIGH_CPM_FALLBACKS


def niche_for_slot(slot: str | None) -> str:
    """morning/afternoon → high_cpm; evening/night → viral."""
    s = (slot or "").strip().lower()
    return "high_cpm" if s in HIGH_CPM_SLOTS else "viral"


OPENAI_TOPIC_SELECTOR_VIRAL = """You select the best YouTube Shorts topic for a viral trending-news channel.

Prefer high-discovery public topics: entertainment, internet culture, celebrity, product launches, controversies.
Skip niche finance/AI-only stories unless they are already mainstream viral.

Return ONLY valid JSON:
{
  "selected_topic": "string",
  "reason": "short reason",
  "search_query": "specific image/topic search query based on chosen trend"
}
"""

OPENAI_TOPIC_SELECTOR_HIGH_CPM = """You select the best YouTube Shorts topic for a HIGH-CPM monetization niche.

Prefer advertiser-friendly topics in this order:
1) AI tools / SaaS / productivity software
2) Personal finance / investing / credit / insurance / taxes
3) Business / startups / pricing / career money decisions

Reject pure celebrity gossip, sports scores, and meme-only topics unless they have a clear money/tool angle.

Return ONLY valid JSON:
{
  "selected_topic": "string",
  "reason": "short reason",
  "search_query": "specific image/topic search query based on chosen trend"
}
"""

SYSTEM_PROMPT_VIRAL = """You write 30-second vertical YouTube Shorts for a viral trending-topics channel.

The short should feel topical, fast, clear, and highly clickable.
Do NOT write fiction, horror, haunted internet stories, creepypasta, or made-up events.
Base everything on the supplied trending topic and avoid unverifiable fabricated specifics.

CRITICAL — SEARCH & TRUST RULES:
- Do NOT fabricate facts, quotes, numbers, timelines, or outcomes.
- Title MUST include the exact trending topic phrase or a recognizable keyword from it.
- Title max 55 characters (mobile feed truncates longer titles).
- BANNED title clichés (never use): "Shock Awaits", "Nobody Saw Coming", "Shocking Turnaround", "Flip the Group", "Schedule Secrets", "The Truth", "Worth It", "Hype Ya Reality".
- Write titles people actually search: names, products, companies, events, or public keywords from the trend.

The video has 5 acts:
1. HOOK — a strong first-line headline about the topic (must grab attention in 2 seconds)
2. CONTEXT — 3 short lines that explain what it is
3. WHY — 3 short lines explaining why people care
4. QUESTION — one open-loop question plus 6 to 8 rapid short captions
5. CLOSE — 2 or 3 short lines; LAST line must ask viewers to comment their take

Style rules:
- Write like a viral trending-topic explainer short, not a documentary.
- Use a mix of simple English with light Hinglish/Urdu phrasing where natural.
- Focus on what happened, why it matters, and what people are debating.
- Be punchy, simple, broad, and readable on screen.
- Hook: 4-8 words, punchy, different wording from title.
- Context lines: 3 short lines, max 8 words each.
- Why lines: 3 short lines, max 8 words each.
- Question: 4-10 words, must invite comments.
- Captions: 6 to 8 items, max 5 words each, energetic but factual.
- CAPTION UNIQUENESS: every caption MUST include a concrete word from the trend; invent fresh phrasing every time.
- BANNED caption fillers: "FULL TIME?", "MATCH ALERT", "TRENDING NOW", "GOAL ALERT", "QUICK BREAKDOWN".
- Close lines: 2 or 3 short lines; final line = comment CTA (no emoji in JSON).
- youtube_tags: 8-12 search tags based on the trend.

Palette: pick 3 RGB colors — high contrast for mobile screens.
Respond ONLY with valid JSON. No markdown fences. No explanation."""

SYSTEM_PROMPT_HIGH_CPM = """You write 30-second vertical YouTube Shorts for HIGH-CPM niches (AI tools, personal finance, SaaS, business money decisions).

Goal: educational, advertiser-safe, searchable explainers that attract high-value viewers.
Do NOT write fiction, get-rich-quick promises, guaranteed returns, medical advice, or illegal advice.
Base everything on the supplied topic and keep claims broad/safe (no fabricated fees, rates, or ROI numbers).

CRITICAL — SEARCH & TRUST RULES:
- Do NOT invent prices, rates, tax rules, or product claims.
- Title MUST include a keyword from the topic (tool name, finance term, company, or money phrase).
- Title max 55 characters.
- BANNED clichés: "Shock Awaits", "Nobody Saw Coming", "The Truth", "Worth It", "Hype Ya Reality", "Get Rich".
- Prefer titles people search: "ChatGPT pricing", "credit score tip", "AI tool for freelancers", "mortgage rate update".

The video has 5 acts:
1. HOOK — money/tool impact headline (2-second grab)
2. CONTEXT — 3 short lines explaining the update/tool/rule
3. WHY — 3 short lines on cost, risk, or opportunity
4. QUESTION — open-loop question + 6 to 8 rapid captions
5. CLOSE — wrap-up; LAST line must ask viewers to comment their take

Style rules:
- Clear, practical, beginner-friendly explainer tone.
- Light Hinglish/Urdu is fine where natural.
- Focus on costs, benefits, mistakes, comparisons, and next steps — not drama.
- Hook: 4-8 words, different from title.
- Context/why lines: max 8 words each.
- Captions: 6-8 unique items, max 5 words, include a topic keyword.
- BANNED caption fillers: "FULL TIME?", "MATCH ALERT", "TRENDING NOW", "GOAL ALERT".
- youtube_tags: include niche tags like "AI tools", "personal finance", "investing", "SaaS", "business tips" plus topic keywords.

Palette: pick 3 RGB colors — professional high-contrast for mobile.
Respond ONLY with valid JSON. No markdown fences. No explanation."""

# Default aliases for older call sites.
OPENAI_TOPIC_SELECTOR_SYSTEM = OPENAI_TOPIC_SELECTOR_VIRAL
SYSTEM_PROMPT = SYSTEM_PROMPT_VIRAL


def make_prompt(latest_topic: str, epilogue_extra: str | None = None, niche: str = "viral") -> str:
    angles = HIGH_CPM_ANGLES if niche == "high_cpm" else VIRAL_ANGLES
    angle = random.choice(angles)
    if niche == "high_cpm":
        package_line = "Create a 30-second HIGH-CPM (AI tools / finance / SaaS / business) YouTube Short package."
        framing = (
            "Frame this as a practical money/tool explainer when possible "
            "(cost, risk, productivity, beginner mistake, comparison)."
        )
        tags_hint = '["AI tools", "personal finance", "... 8-12 niche + trend tags"]'
        search_hint = "specific stock-image search phrase for office finance AI productivity"
    else:
        package_line = "Create a 30-second viral trending-topic YouTube Short package."
        framing = "Keep it discovery-friendly and broadly interesting."
        tags_hint = '["trend keyword 1", "trend keyword 2", "... 8-12 search tags"]'
        search_hint = "specific stock-image search phrase based on trend"

    base = f"""{package_line}

Current trending topic: {latest_topic}
Content angle: {angle}
Niche mode: {niche}

The output must be directly about this exact trend phrase.
{framing}
Do not turn it into fiction.
Do not fabricate facts, timelines, prices, or outcomes.
Do not convert it into a haunted or horror metaphor.
The title MUST contain words from "{latest_topic}" and stay under 55 characters.
The hook must use different wording than the title.

Return this exact JSON:
{{
  "title": "short searchable title under 55 chars using trend keywords",
  "topic_id": "snake_case_identifier",
  "palette": [[r,g,b], [r,g,b], [r,g,b]],
  "hook": "4-8 word punchy opener (not same as title)",
  "context_lines": ["3 short lines", "max 8 words each", "directly about the trend"],
  "why_lines": ["3 short lines", "why people care", "max 8 words each"],
  "question": "4-10 word question that invites comments",
  "captions": [["TOPIC-SPECIFIC CAPTION", [r,g,b]], "... 6 to 8 unique, each uses a word from the trend"],
  "close_lines": ["2 lines wrap-up", "Comment your pick below"],
  "search_query": "{search_hint}",
  "youtube_tags": {tags_hint}
}}"""
    if epilogue_extra:
        base += f"\n\nEpilogue instruction: {epilogue_extra}"
    return base


def _parse_approx_traffic(text: str) -> int:
    """Convert Trends RSS approx_traffic text to an integer.

    Examples: '20,000+' -> 20000 ; '200+' -> 200 ; '' -> 0.
    """
    m = re.search(r"[\d,]+", text or "")
    return int(m.group(0).replace(",", "")) if m else 0


def demand_score(traffic: int, news_count: int = 0) -> float:
    """Return a 0..1 demand estimate for a trending topic.

    Traffic (log-scaled from approx_traffic) is the primary signal.
    News-item count is a small secondary nudge for cross-coverage breadth.
    """
    import math
    traffic_component = min(1.0, math.log10(traffic + 1) / 6.0)  # ~1M+ ≈ 1.0
    news_component = min(1.0, news_count / 5.0) * 0.15           # up to +0.15
    return round(min(1.0, traffic_component + news_component), 4)


def fetch_trending_entries(max_items: int = 20) -> list[dict]:
    """Fetch Google Trends topics from RSS, preserving demand metadata.

    Each returned dict has keys: topic, traffic (int), news_count (int), demand (float).
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
    entries: list[dict] = []
    seen: set[str] = set()
    for item in root.findall(".//item"):
        t = (item.findtext("title") or "").strip()
        if not t:
            continue
        # Remove hash-prefixes and extra separators for cleaner prompt context.
        t = re.sub(r"\s*#\w+", "", t).strip()
        t = re.sub(r"\s*[|:]\s*", " - ", t)
        if not t or not is_valid_entity(t, min_length=4) or t in seen:
            continue
        seen.add(t)
        traffic = _parse_approx_traffic(
            item.findtext(f"{{{GOOGLE_TRENDS_HT_NS}}}approx_traffic") or ""
        )
        news_count = len(item.findall(f"{{{GOOGLE_TRENDS_HT_NS}}}news_item"))
        entries.append(
            {
                "topic": t,
                "traffic": traffic,
                "news_count": news_count,
                "demand": demand_score(traffic, news_count),
            }
        )
        if len(entries) >= max_items:
            break
    return entries


def fetch_latest_topics(max_items: int = 20) -> list[str]:
    """Back-compat wrapper — returns topic strings only (demand metadata dropped)."""
    return [e["topic"] for e in fetch_trending_entries(max_items)]


def _demand_min_score() -> float:
    """Minimum demand score a trend must clear before the LLM selector sees it."""
    try:
        return float(env_value("DEMAND_MIN_SCORE", "0.55"))
    except (TypeError, ValueError):
        return 0.55


def _is_usable_topic(topic: str) -> bool:
    return is_valid_entity(topic, min_length=4)


def _is_channel_fit_topic(topic: str, niche: str = "viral") -> bool:
    t = (topic or "").lower()
    tokens = re.findall(r"[a-z0-9]+", t)
    if not tokens:
        return False
    token_set = set(tokens)
    if niche == "high_cpm":
        keywords = HIGH_CPM_KEYWORDS
        multiword_matches = [
            "stock market", "interest rate", "credit score", "personal finance",
            "ai tool", "chatgpt", "high yield", "mortgage rate", "side hustle",
            "product pricing", "saas pricing",
        ]
    else:
        keywords = VIRAL_KEYWORDS
        multiword_matches = [
            "breaking news", "box office", "social media", "product launch",
            "policy update", "viral clip",
        ]
    if any(phrase in t for phrase in multiword_matches):
        return True
    return any(k in token_set for k in keywords)


def select_topic_with_openai(
    topics: list[str], niche: str = "viral"
) -> tuple[str | None, str | None]:
    api_key = env_value("OPENAI_API_KEY", "").strip()
    if not api_key or not topics:
        return None, None

    system = (
        OPENAI_TOPIC_SELECTOR_HIGH_CPM
        if niche == "high_cpm"
        else OPENAI_TOPIC_SELECTOR_VIRAL
    )
    user_hint = (
        "Choose the best HIGH-CPM topic (AI tools / finance / SaaS / business money):\n"
        if niche == "high_cpm"
        else "Choose the best viral discovery topic from this live list:\n"
    )

    payload = json.dumps(
        {
            "model": "gpt-4.1-mini",
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": user_hint + json.dumps(topics, ensure_ascii=False),
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
        topic = normalize_entity(data.get("selected_topic"))
        query = normalize_entity(data.get("search_query"))
        if topic and is_valid_entity(topic, min_length=4):
            return topic, query or None
        if topic or data.get("selected_topic") is not None:
            log_entity_rejection(
                "openai_topic_selector",
                data.get("selected_topic"),
                {"raw_response": raw, "parsed": data, "topics_input": topics},
            )
    except Exception as e:
        print(f"  OpenAI topic selector failed: {e}")
    return None, None


def pick_rotated_channel_fit_fallback(niche: str = "viral") -> str:
    """Pick a synthetic seed while avoiding the last few picks (reduces duplicate titles)."""
    pool_src = HIGH_CPM_FALLBACKS if niche == "high_cpm" else VIRAL_FALLBACKS
    try:
        SEED_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        recent: list[str] = []
        if SEED_HISTORY_PATH.exists():
            data = json.loads(SEED_HISTORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                recent = [str(x) for x in data.get("recent_seeds", []) if str(x).strip()]
        avoid = set(s.lower() for s in recent[-4:])
        pool = [s for s in pool_src if s.lower() not in avoid]
        if not pool:
            pool = list(pool_src)
        choice = random.choice(pool)
        recent.append(choice)
        SEED_HISTORY_PATH.write_text(
            json.dumps({"recent_seeds": recent[-32:]}, indent=2),
            encoding="utf-8",
        )
        return choice
    except Exception as e:
        print(f"  [WARN] Seed rotation unavailable: {e}")
        return random.choice(pool_src)


def pick_latest_topic(niche: str = "viral") -> tuple[str, str | None]:
    """Pick a topic from RSS, demand-filtered and niche-filtered."""
    label = "HIGH-CPM" if niche == "high_cpm" else "viral"
    try:
        entries = fetch_trending_entries()
        thr = _demand_min_score()
        topics: list[str] = []
        if entries:
            viable = [e for e in entries if e["demand"] >= thr]
            # Log below-threshold topics separately (distinct reason from null/placeholder).
            for e in entries:
                if e["demand"] < thr:
                    log_entity_rejection(
                        "demand_filter",
                        e["topic"],
                        {
                            "demand": e["demand"],
                            "traffic": e["traffic"],
                            "news_count": e["news_count"],
                            "threshold": thr,
                        },
                        reason="low_demand",
                    )
            # Never pass an empty list to the selector on a low-traffic day.
            pool_entries = viable if viable else entries
            topics = [e["topic"] for e in pool_entries]
            preview = ", ".join(
                f'{e["topic"]} ({e["demand"]})' for e in pool_entries[:5]
            )
            print(f"  RSS top topics (demand≥{thr}): {preview}")
        if topics:
            # Prefer niche-matching candidates first for OpenAI ranking.
            niche_first = [
                t for t in topics if _is_usable_topic(t) and _is_channel_fit_topic(t, niche)
            ]
            rank_pool = (niche_first + [t for t in topics if t not in niche_first])[:10]
            chosen, search_query = select_topic_with_openai(rank_pool, niche=niche)
            if chosen and _is_usable_topic(chosen):
                # If OpenAI picked off-niche for high_cpm, try a niche candidate instead.
                if niche == "high_cpm" and not _is_channel_fit_topic(chosen, niche) and niche_first:
                    chosen = niche_first[0]
                    search_query = None
                    print(f"  Trending topic seed: {chosen} ({label} override)")
                    return require_valid_entity(
                        chosen, source="pick_latest_topic.openai_override", raw_upstream=topics
                    ), search_query
                print(f"  Trending topic seed: {chosen} (OpenAI-selected, {label})")
                return require_valid_entity(
                    chosen, source="pick_latest_topic.openai", raw_upstream=topics
                ), search_query
            for candidate in topics:
                if _is_usable_topic(candidate) and _is_channel_fit_topic(candidate, niche):
                    print(f"  Trending topic seed: {candidate} (latest {label})")
                    return require_valid_entity(
                        candidate, source="pick_latest_topic.rss", raw_upstream=topics
                    ), None
            print(f"  No {label} topic in RSS; using {label} fallback seed.")
        print(f"  Trending topic feed empty; using synthetic {label} seed.")
    except Exception as e:
        print(f"  Trending topic fetch failed: {e}")
    fallback_topic = pick_rotated_channel_fit_fallback(niche=niche)
    print(f"  Trending topic seed: {fallback_topic} ({label} fallback)")
    return require_valid_entity(
        fallback_topic, source="pick_latest_topic.fallback", raw_upstream={"niche": niche}
    ), None


def call_llm(prompt: str, model: str, niche: str = "viral") -> dict:
    api_key = env_value("OPENROUTER_API_KEY", "")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY not set")

    system = SYSTEM_PROMPT_HIGH_CPM if niche == "high_cpm" else SYSTEM_PROMPT_VIRAL
    payload = json.dumps(
        {
            "model": model,
            "temperature": 1.0,
            "max_tokens": 1200,
            "messages": [
                {"role": "system", "content": system},
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


TITLE_BANNED_PHRASES = [
    "shock awaits", "nobody saw coming", "shocking turnaround", "flip the group",
    "schedule secrets", "group stage shock", "the truth", "worth it",
    "hype ya reality", "is it safe", "secrets:", "nobody is talking",
]

TITLE_MAX_CHARS = 55


def _sanitize_title(title: str, trend: str = "") -> str:
    """Strip spammy clichés and enforce mobile-friendly length."""
    t = _compact_ws(title)
    low = t.lower()
    safe_trend = trend if is_valid_entity(trend, min_length=4) else ""
    if any(phrase in low for phrase in TITLE_BANNED_PHRASES):
        t = _fallback_title_from_topic(safe_trend) if safe_trend else "Trending update explained"
    if not is_valid_entity(t, min_length=4) or contains_invalid_publish_text(t):
        t = _fallback_title_from_topic(safe_trend) if safe_trend else "Trending update explained"
    if len(t) > TITLE_MAX_CHARS:
        cut = t[: TITLE_MAX_CHARS - 3].rsplit(" ", 1)[0]
        t = (cut or t[: TITLE_MAX_CHARS - 3]) + "..."
    return t


def _default_youtube_tags(trend: str = "", niche: str = "viral") -> list[str]:
    if niche == "high_cpm":
        tags = [
            "AI tools", "Personal Finance", "Investing", "Business Tips",
            "SaaS", "Money Tips", "Productivity", "Shorts",
        ]
    else:
        tags = [
            "Trending", "Breaking News", "Explainer", "Viral",
            "Shorts", "YouTube Shorts", "Latest Update", "Internet Trends",
        ]
    for word in re.findall(r"[A-Za-z0-9]+", trend or ""):
        if len(word) >= 4 and word.lower() not in {
            "today", "viral", "trend", "trending", "update", "news", "latest",
        }:
            tag = word.title()
            if tag not in tags:
                tags.append(tag)
        if len(", ".join(tags)) > 420:
            break
    return tags[:12]


def validate(content: dict, trend: str = "") -> dict:
    """Ensure all required fields exist and have correct types."""
    if not content.get("title"):
        content["title"] = "Trending Update - What Happened?"
    content["title"] = _sanitize_title(str(content["title"]), trend)

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
        content["question"] = "Aapka take kya hai?"

    caps = content.get("captions", [])
    fixed = []
    seen_texts: set[str] = set()
    for cap in caps:
        if isinstance(cap, list) and len(cap) == 2:
            text = _compact_ws(str(cap[0]))
            if not text:
                continue
            key = text.upper()
            if key in CAPTION_BANNED or key in seen_texts:
                continue
            seen_texts.add(key)
            color = (
                cap[1]
                if isinstance(cap[1], list) and len(cap[1]) == 3
                else [255, 255, 255]
            )
            fixed.append([text, [max(0, min(255, int(v))) for v in color]])
    # Pad with topic-unique captions — never recycle banned fillers like "FULL TIME?"
    if len(fixed) < 6:
        for text, color in _unique_fallback_captions(trend or content.get("title") or "Trending Update"):
            key = text.upper()
            if key in seen_texts:
                continue
            seen_texts.add(key)
            fixed.append([text, color])
            if len(fixed) >= 6:
                break
    content["captions"] = fixed[:8]

    close_lines = [str(x) for x in content.get("close_lines", []) if str(x).strip()]
    while len(close_lines) < 2:
        close_lines.append("This trend is moving fast.")
    if not any("comment" in line.lower() for line in close_lines):
        close_lines.append("Comment your pick below.")
    content["close_lines"] = close_lines[:3]

    tags = content.get("youtube_tags", [])
    if not isinstance(tags, list):
        tags = []
    tags = [str(t).strip() for t in tags if str(t).strip()]
    if len(tags) < 4:
        tags = _default_youtube_tags(trend or content.get("title", ""), niche=content.get("_niche", "viral"))
    content["youtube_tags"] = tags[:12]

    if not content.get("search_query"):
        default_q = (
            "ai finance productivity office laptop"
            if content.get("_niche") == "high_cpm"
            else "trending topic social media news"
        )
        content["search_query"] = str(content.get("search_query") or content.get("title", "")).strip() or default_q

    try:
        assert_publishable_title(
            str(content.get("title", "")),
            source="content_gen.validate",
        )
    except EntityValidationError as exc:
        log_entity_rejection(
            "content_gen.validate.title",
            content.get("title"),
            {"trend": trend, "error": str(exc)},
        )
        safe_trend = trend if is_valid_entity(trend, min_length=4) else ""
        content["title"] = (
            _fallback_title_from_topic(safe_trend)
            if safe_trend
            else "Trending Update - What Happened?"
        )
        assert_publishable_title(content["title"], source="content_gen.validate.recovered")

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
                content["hook"] = "Quick trend breakdown in 30 sec."
            else:
                content["title"] = hook
                content["hook"] = "Yeh trend abhi viral hai — detail dekho."

    title = _compact_ws(str(content.get("title", "")))
    low = title.lower()
    half = max(8, len(low) // 2)
    if len(low) >= 24 and low[:half] == low[half : half * 2]:
        content["title"] = _compact_ws(title[: len(title) // 2])

    return content


# Varied, non-repetitive title shapes. {t} is the topic phrase (title-cased).
# Avoid spammy/bot-looking phrases that YouTube Shorts suppress ("Everyone's talking",
# "The real story behind", "explained in 30 sec").
_TITLE_TEMPLATES_GENERIC = [
    "{t} — what actually changed?",
    "3 things to know about {t}",
    "{t} isn't what most people think",
    "Why {t} just flipped the timeline",
    "{t} just took a sharp turn",
    "Wait — this {t} detail matters",
    "What {t} means in plain English",
    "{t}: the part that got buried",
]
_TITLE_TEMPLATES_VS = [
    "{t}: who actually wins?",
    "{t} — the gap is bigger than you think",
    "{t}: it's not even close",
    "Settling {t} once and for all",
]
_TITLE_TEMPLATES_PRICE = [
    "{t}: what just changed",
    "Why {t} caught everyone off guard",
    "{t} — should you care?",
    "The cost angle on {t} nobody leads with",
]
_TITLE_TEMPLATES_FEATURE = [
    "{t}: the part nobody mentions",
    "What {t} actually changes",
    "{t} is quietly a big upgrade",
    "Is {t} worth switching for?",
]
_TITLE_TEMPLATES_MATCH = [
    "Can {t} survive this?",
    "{t}: the stakes just jumped",
    "Before {t}, know this",
    "{t} could go either way",
]

_HOOK_SHAPES = [
    "{t} — here's the quick version",
    "Wait, what's going on with {t}?",
    "This {t} update is a big deal",
    "The {t} detail most people miss",
    "{t}: what you actually need to know",
    "Stop scrolling — {t} just shifted",
    "{t} just flipped the conversation",
    "Plain English: what's up with {t}",
]

_CONTEXT_SHAPES = [
    "{t} is picking up fast.",
    "People can't stop discussing {t}.",
    "Here's what's driving {t}.",
    "The {t} story keeps growing.",
    "{t} is all over the feeds.",
    "Reactions to {t} are split.",
    "This is why {t} matters.",
]

_ANCHOR_JOINERS = ["{e}: {ti}", "{ti} — {e}", "{e} — {ti}", "{ti} ({e})"]


def _stable_hash(text: str) -> int:
    """Deterministic integer hash used for stable template rotation."""
    return int(hashlib.md5(str(text).encode("utf-8", errors="replace")).hexdigest(), 16)


def _rotating_choice(pool: list[str], *, category: str, topic: str) -> str:
    """Deterministic template pick that needs no persisted state.

    Production runs on ephemeral GitHub Actions runners, so a local
    shape_rotation.json cannot survive across scheduled invocations.
    Index is derived from hash(category + topic + UTC date) — same topic
    on the same day is stable/debuggable; the shape can shift next day.
    """
    n = len(pool)
    if n == 0:
        return ""
    from datetime import date
    salt = date.today().isoformat()
    return pool[_stable_hash(f"{category}:{topic}:{salt}") % n]


def _anchor_title(entity: str, existing_title: str = "") -> str:
    """Re-attach trend entity using a varied but deterministic title shape."""
    e = _compact_ws(entity or "").strip()
    ti = _compact_ws(existing_title or "").strip()
    if not is_valid_entity(e, min_length=4):
        return _sanitize_title(ti or _fallback_title_from_topic(""), trend="")
    if not ti or contains_invalid_publish_text(ti):
        return _sanitize_title(_fallback_title_from_topic(e), trend=e)
    if e.lower() in ti.lower():
        return _sanitize_title(ti, trend=e)
    joiner = _rotating_choice(_ANCHOR_JOINERS, category="anchor_join", topic=e)
    return _sanitize_title(joiner.format(e=e, ti=ti), trend=e)


def _anchor_hook(topic: str) -> str:
    """Topic-anchored hook with varied sentence structure."""
    t = _compact_ws(topic or "").strip()
    if not is_valid_entity(t, min_length=4):
        return "Here's the quick breakdown"
    return _rotating_choice(_HOOK_SHAPES, category="hook", topic=t).format(t=t)


def _anchor_context_lines(topic: str) -> list[str]:
    """Three topic-anchored context lines, deterministically varied."""
    t = _compact_ws(topic or "").strip()
    base = t if is_valid_entity(t, min_length=4) else "this trend"
    h = _stable_hash(base)
    n = len(_CONTEXT_SHAPES)
    return [
        base,
        _CONTEXT_SHAPES[h % n].format(t=base),
        _CONTEXT_SHAPES[(h + 1) % n].format(t=base),
    ]


CAPTION_BANNED = {
    "FULL TIME?",
    "MATCH ALERT",
    "TRENDING NOW",
    "GOAL ALERT",
    "QUICK BREAKDOWN",
    "GROUP STAKES",
    "WHO ADVANCES?",
    "MUST WIN?",
    "UPSET ALERT",
    "GOAL DIFF",
    "KNOCKOUT RACE",
}

# Rotating banks so fallback captions never collapse to the same Shorts cover frame.
_CAPTION_BANKS = [
    ["PRESSURE ON", "ONE CHANCE", "DO OR DIE", "LOCK IN", "NO MERCY", "FINAL PUSH"],
    ["TABLE TILTS", "POINT HUNT", "EDGE CASE", "LAST SHOT", "CALL IT", "NET EMPTY?"],
    ["VAR CHECK", "REF STORM", "DECISION TIME", "REPLAY IT", "FOUL OR DIVE?", "CARD OUT"],
    ["FORM DIP", "HOT STREAK", "CLINICAL FINISH", "ICE VEINS", "BOX CHAOS", "TAP IN"],
    ["FAN FURY", "STADIUM ROAR", "AWAY END", "HOME PRESSURE", "PURE NOISE", "CHAOS MODE"],
    ["SQUAD SHUFFLE", "BENCH BOMB", "TACTIC FLIP", "HIGH LINE", "LOW BLOCK", "SET PIECE"],
    ["GOLDEN BOOT", "ASSIST KING", "CLEAN SHEET", "HAT TRICK?", "OWN GOAL", "STOPPAGE TIME"],
    ["BRACKET SHAKE", "PATH OPENS", "GROUP EXIT?", "ROUND OF 16", "NIGHTMARE DRAW", "EASY RUN?"],
]

_CAPTION_COLORS = [
    [255, 255, 255],
    [255, 220, 120],
    [255, 150, 150],
    [120, 220, 255],
    [180, 255, 160],
    [255, 180, 80],
]


def _unique_fallback_captions(focus: str) -> list:
    """
    Build 6 topic-anchored captions that differ each call.
    Prevents every failed LLM run from showing the same 'FULL TIME?' Shorts cover.
    """
    focus = _compact_ws(focus or "").strip() or "Trending Update"
    words = re.findall(r"[A-Za-z0-9']+", focus)
    # Fresh randomness every run (not seeded by topic alone — same topic can rerun).
    rng = random.Random()
    caps: list[list] = []
    seen: set[str] = set()

    def _add(text: str) -> None:
        t = _compact_ws(text).upper()[:32]
        if not t or t in seen or t in CAPTION_BANNED:
            return
        seen.add(t)
        caps.append([t, list(rng.choice(_CAPTION_COLORS))])

    if words:
        _add(" ".join(words[:3]))
        lead = words[0]
        suffixes = ["WATCH", "MOMENT", "STAKES", "DRAMA", "PULSE", "SPARK"]
        _add(f"{lead} {rng.choice(suffixes)}")
        if len(words) >= 2:
            _add(f"{words[1]} {rng.choice(['FIRE', 'RISING', 'CHECK', 'SURGE'])}")

    bank = list(rng.choice(_CAPTION_BANKS))
    rng.shuffle(bank)
    for phrase in bank:
        _add(phrase)
        if len(caps) >= 6:
            break

    extras = [
        f"{rng.choice(['WHY', 'HOW', 'WHEN'])} NOW?",
        f"{(words[0] if words else 'CUP')} FIRST",
        "SAY IT AGAIN",
        "BELIEVE IT?",
        "NO DEBATE",
        "THAT CHANGED IT",
    ]
    for phrase in extras:
        if len(caps) >= 6:
            break
        _add(phrase)

    while len(caps) < 6:
        _add(f"TAKE {len(caps) + 1}")
    return caps[:6]


def _fallback_title_from_topic(latest_topic: str) -> str:
    topic = _compact_ws(latest_topic or "").strip(" -:")
    if not is_valid_entity(topic, min_length=4):
        return _rotating_choice(
            _TITLE_TEMPLATES_GENERIC,
            category="title_generic",
            topic="this trend",
        ).format(t="this trend")
    topic_l = topic.lower()
    if " vs " in f" {topic_l} ":
        pool, cat = _TITLE_TEMPLATES_VS, "title_vs"
    elif "price" in topic_l or "stock" in topic_l:
        pool, cat = _TITLE_TEMPLATES_PRICE, "title_price"
    elif "feature" in topic_l or "update" in topic_l:
        pool, cat = _TITLE_TEMPLATES_FEATURE, "title_feature"
    elif any(w in topic_l for w in ("launch", "update", "release", "debate", "controversy")):
        pool, cat = _TITLE_TEMPLATES_MATCH, "title_match"
    else:
        pool, cat = _TITLE_TEMPLATES_GENERIC, "title_generic"
    return _rotating_choice(pool, category=cat, topic=topic).format(t=topic)


def fallback_for_topic(latest_topic: str) -> dict:
    """
    Build a topic-anchored fallback so failed model runs do not keep reusing
    an unrelated static title like "Movie Box App".
    """
    lt = _compact_ws(latest_topic or "").strip()
    if not is_valid_entity(lt, min_length=4):
        log_entity_rejection(
            "fallback_for_topic",
            latest_topic,
            raw_upstream={"latest_topic": latest_topic},
        )
        lt = ""
    base = random.choice(_FALLBACK_POOL).copy()
    focus = lt or "this trend"
    base["title"] = _fallback_title_from_topic(lt)
    base["hook"] = _anchor_hook(focus)
    base["context_lines"] = _anchor_context_lines(focus)
    base["why_lines"] = [
        "Opinions online split ho rahe hain.",
        "Search interest fast grow kar raha hai.",
        "Har koi quick verdict chahta hai.",
    ]
    base["question"] = "Real story kya hai?"
    base["captions"] = _unique_fallback_captions(focus)
    base["close_lines"] = [
        "Trend fast move kar raha hai, update check zaroor karo.",
        "Comment your pick below.",
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
        "title": "AI tool update just dropped",
        "topic_id": "ai_tool_update",
        "palette": [[48, 114, 255], [130, 220, 255], [255, 255, 255]],
        "hook": "Yeh feature game-change hai?",
        "context_lines": ["Naya update suddenly live ho gaya.", "Users screenshots share kar rahe hain.", "Timeline par strong reactions aa rahi hain."],
        "why_lines": ["Workflow direct impact ho sakta hai.", "Early adopters fast experiment kar rahe.", "Competitors pe pressure build ho raha."],
        "question": "Hype ya actually useful?",
        "captions": [
            ["UPDATE DROP", [255, 255, 255]],
            ["REAL IMPACT?", [255, 220, 120]],
            ["EARLY REVIEWS", [255, 150, 150]],
            ["MASSIVE DEMAND", [255, 255, 255]],
            ["WORTH SWITCHING?", [255, 220, 120]],
            ["YOUR TAKE?", [255, 150, 150]],
        ],
        "close_lines": ["Aaj ka shift kal trend bana deta hai.", "Comment your pick below."],
        "search_query": "ai product launch user reaction interface",
        "youtube_tags": ["AI update", "tech news", "trending", "shorts"],
    },
    {
        "title": "Celebrity interview sparks debate",
        "topic_id": "celeb_interview_debate",
        "palette": [[255, 94, 98], [255, 196, 113], [255, 255, 255]],
        "hook": "Ek line ne internet hila diya",
        "context_lines": ["Clip short hai, debate huge hai.", "Different edits alag story bana rahe.", "Comments section full split hai."],
        "why_lines": ["Fan bases directly clash kar rahe.", "Media coverage fast scale ho rahi.", "Narrative har hour change ho raha."],
        "question": "Out of context ya fair point?",
        "captions": [
            ["CLIP VIRAL", [255, 255, 255]],
            ["CONTEXT CHECK", [255, 220, 120]],
            ["FANS DIVIDED", [255, 180, 120]],
            ["FULL STORY?", [255, 255, 255]],
            ["HOT DEBATE", [255, 220, 120]],
            ["YOUR PICK?", [255, 180, 120]],
        ],
        "close_lines": ["Narrative fast turn hota hai online.", "Comment your pick below."],
        "search_query": "podcast interview studio microphone reaction",
        "youtube_tags": ["celebrity news", "viral clip", "internet debate", "shorts"],
    },
]


def generate_topic(epilogue_extra: str | None = None, slot: str | None = None) -> dict:
    """Generate a completely fresh topic and all content via OpenRouter."""
    niche = niche_for_slot(slot)
    key = env_value("OPENROUTER_API_KEY", "")
    print(f"  OPENROUTER_API_KEY: {'SET (' + key[:8] + '...)' if key else 'NOT SET'}")
    print(f"  Niche mode: {niche} (slot={slot or 'n/a'})")
    latest_topic, selected_search_query = pick_latest_topic(niche=niche)
    try:
        latest_topic = require_valid_entity(
            latest_topic,
            source="generate_topic.pick_latest_topic",
            raw_upstream={"niche": niche, "slot": slot},
        )
    except EntityValidationError as exc:
        log_entity_rejection(
            "generate_topic.pick_latest_topic",
            latest_topic,
            raw_upstream={"niche": niche, "slot": slot, "error": str(exc)},
        )
        latest_topic = pick_rotated_channel_fit_fallback(niche=niche)
        latest_topic = require_valid_entity(
            latest_topic,
            source="generate_topic.recovered_fallback",
            raw_upstream={"niche": niche, "slot": slot},
        )
    prompt = make_prompt(latest_topic, epilogue_extra, niche=niche)
    for i, model in enumerate(MODELS):
        try:
            print(f"  Generating topic (model: {model})...")
            content = call_llm(prompt, model, niche=niche)
            content["_niche"] = niche
            content = validate(content, latest_topic)
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
                content["title"] = _anchor_title(latest_topic, title)
                content["hook"] = _anchor_hook(latest_topic)
                content["context_lines"] = _anchor_context_lines(latest_topic)
                if isinstance(captions, list) and captions:
                    first = captions[0]
                    if isinstance(first, list) and len(first) == 2:
                        first[0] = latest_topic[:42]
                        captions[0] = first
                        content["captions"] = captions
            content = _normalize_display_text(content)
            content["trend_topic"] = latest_topic
            content["niche"] = niche
            content.pop("_niche", None)
            if selected_search_query:
                content["search_query"] = selected_search_query
            assert_publishable_title(
                str(content.get("title", "")),
                source="generate_topic.pre_return",
            )
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
    content["_niche"] = niche
    content["niche"] = niche
    if selected_search_query:
        content["search_query"] = selected_search_query
    out = validate(content, latest_topic)
    out["niche"] = niche
    out.pop("_niche", None)
    assert_publishable_title(str(out.get("title", "")), source="generate_topic.fallback_return")
    return out


if __name__ == "__main__":
    import sys

    content = generate_topic()
    print(json.dumps(content, indent=2))

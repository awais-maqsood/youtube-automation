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
    passes_named_entity_gate,
    require_named_entity,
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
TITLE_HISTORY_PATH = Path(__file__).resolve().parent.parent / "output" / "title_history.json"
TITLE_HISTORY_MAX = 100
TITLE_HISTORY_NEGATIVES = 20
# Do not reuse the same title/opener structural family within this many recent videos.
TITLE_FAMILY_COOLDOWN = 3


def env_value(name: str, default: str = "") -> str:
    return os.environ.get(name) or ENV_FILE_VALUES.get(name, default)

# Models tried in order — first available wins. All are free-tier on OpenRouter.
# Multiple providers so a single 429/outage doesn't force the generic fallback.
# Re-verified live against OpenRouter chat/completions: the previous slugs lost
# their free tier and 404'd every run, which is why every video used canned text.
MODELS = [
    "google/gemma-4-26b-a4b-it:free",                          # reliable JSON on the real prompt
    "poolside/laguna-s-2.1:free",                             # solid instruct fallback
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",     # reasoning model; JSON recovered below
    "google/gemma-4-31b-it:free",                             # last-resort, frequently rate-limited
]

# 4 videos/day historically; volume is now gated by DAILY_UPLOAD_CAP (default 2).
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

# Fallback seeds MUST be concrete named entities (person/org/product/event).
# Generic category phrases ("streaming platform new release") get distribution-suppressed.
VIRAL_FALLBACKS = [
    "Taylor Swift Eras Tour",
    "iPhone 18 Pro leak",
    "Netflix Squid Game season",
    "Sony Xperia 1 VIII",
    "Nintendo Switch 2 launch",
    "FIFA World Cup 2026",
    "Elon Musk SpaceX Starship",
    "GTA 6 release window",
    "Beyonce Cowboy Carter tour",
    "PlayStation 5 Pro review",
]

HIGH_CPM_FALLBACKS = [
    "ChatGPT Plus pricing update",
    "NVIDIA Blackwell GPU launch",
    "Apple Vision Pro apps",
    "Claude AI Anthropic pricing",
    "Tesla FSD subscription cost",
    "Google Gemini Android features",
    "Microsoft Copilot Office pricing",
    "Amazon AWS price change",
    "Stripe SaaS billing update",
    "Federal Reserve interest rate",
    "Shopify Plus merchant fees",
    "OpenAI GPT-5 release rumors",
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

HARD REQUIREMENT — named entity only:
- selected_topic MUST name a specific person, company, product, event, or titled work
  (e.g. "iPhone 18", "Taylor Swift", "FIFA World Cup 2026", "Netflix Squid Game").
- REJECT generic categories with no proper noun ("internet debate going viral",
  "streaming platform new release", "app feature rollout reaction").

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

HARD REQUIREMENT — named entity only:
- selected_topic MUST name a specific product, company, person, or event
  (e.g. "ChatGPT Plus", "NVIDIA Blackwell", "Federal Reserve", "Apple Vision Pro").
- REJECT generic categories ("AI tools for freelancers", "SaaS pricing war",
  "credit score mistake") unless they include a concrete proper noun.

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

SYSTEM_PROMPT_VIRAL = """You write vertical YouTube Shorts for a viral trending-topics channel.

The short should feel topical, fast, clear, and highly clickable.
Do NOT write fiction, horror, haunted internet stories, creepypasta, or made-up events.
Base everything on the supplied trending topic and avoid unverifiable fabricated specifics.

CRITICAL — SEARCH & TRUST RULES:
- Do NOT fabricate facts, quotes, numbers, timelines, or outcomes.
- Title MUST include the exact trending topic phrase or a recognizable keyword from it.
- Title max 55 characters (mobile feed truncates longer titles).
- Write titles people actually search: names, products, companies, events, or public keywords from the trend.

CRITICAL — ANTI-TEMPLATE (YouTube suppresses formulaic clusters):
- Every video MUST use a genuinely different sentence architecture for title, hook, and CTA.
- Vary opening hook type across videos: question | bold claim | contrarian | direct fact | how/why | imperative.
- Do NOT reuse rhetorical scaffolds. BANNED patterns (never use):
  "Trending now", "3 things to know", "Wait — this", "Before X, know this",
  "just flipped the conversation/timeline", "Which side are you on",
  "could go either way", "the part that got buried", "Comment your prediction",
  "Shock Awaits", "Nobody Saw Coming", "The Truth", "Worth It", "Hype Ya Reality",
  "explained in 30 sec", "Everyone's talking".
- If recent titles are provided as negative examples, do NOT copy their structure or phrasing.

The video has 5 acts:
1. HOOK — a strong first-line headline about the topic (must grab attention in 2 seconds)
2. CONTEXT — 3 short lines that explain what it is
3. WHY — 3 short lines explaining why people care
4. QUESTION — one open-loop question plus 6 to 8 rapid short captions
5. CLOSE — 2 or 3 short lines; LAST line must ask viewers to comment (vary CTA wording)

Style rules:
- Write like a viral trending-topic explainer short, not a documentary.
- Use a mix of simple English with light Hinglish/Urdu phrasing where natural.
- Focus on what happened, why it matters, and what people are debating.
- Be punchy, simple, broad, and readable on screen.
- Hook: 4-8 words, punchy, different wording AND structure from title.
- Context lines: 3 short lines, max 8 words each.
- Why lines: 3 short lines, max 8 words each.
- Question: 4-10 words, must invite comments (fresh phrasing each time).
- Captions: 6 to 8 items, max 5 words each, energetic but factual.
- CAPTION UNIQUENESS: every caption MUST include a concrete word from the trend; invent fresh phrasing every time.
- BANNED caption fillers: "FULL TIME?", "MATCH ALERT", "TRENDING NOW", "GOAL ALERT", "QUICK BREAKDOWN".
- Close lines: 2 or 3 short lines; final line = unique comment CTA (no emoji in JSON).
- youtube_tags: 8-12 search tags based on the trend.

Palette: pick 3 RGB colors — high contrast for mobile screens.
Respond ONLY with valid JSON. No markdown fences. No explanation."""

SYSTEM_PROMPT_HIGH_CPM = """You write vertical YouTube Shorts for HIGH-CPM niches (AI tools, personal finance, SaaS, business money decisions).

Goal: educational, advertiser-safe, searchable explainers that attract high-value viewers.
Do NOT write fiction, get-rich-quick promises, guaranteed returns, medical advice, or illegal advice.
Base everything on the supplied topic and keep claims broad/safe (no fabricated fees, rates, or ROI numbers).

CRITICAL — SEARCH & TRUST RULES:
- Do NOT invent prices, rates, tax rules, or product claims.
- Title MUST include a keyword from the topic (tool name, finance term, company, or money phrase).
- Title max 55 characters.
- Prefer titles people search: named products/companies + concrete angle.

CRITICAL — ANTI-TEMPLATE (YouTube suppresses formulaic clusters):
- Every video MUST use a genuinely different sentence architecture for title, hook, and CTA.
- Vary opening type: question | claim | contrarian | direct fact | how/why | cost-warning.
- BANNED patterns (never use): "Trending now", "Money tip:", "3 things to know",
  "Wait — this", "Before X, know this", "just flipped", "Which side are you on",
  "Shock Awaits", "Nobody Saw Coming", "The Truth", "Worth It", "Get Rich",
  "explained in 30 sec".
- If recent titles are provided as negative examples, do NOT copy their structure or phrasing.

The video has 5 acts:
1. HOOK — money/tool impact headline (2-second grab)
2. CONTEXT — 3 short lines explaining the update/tool/rule
3. WHY — 3 short lines on cost, risk, or opportunity
4. QUESTION — open-loop question + 6 to 8 rapid captions
5. CLOSE — wrap-up; LAST line must ask viewers to comment (vary CTA wording)

Style rules:
- Clear, practical, beginner-friendly explainer tone.
- Light Hinglish/Urdu is fine where natural.
- Focus on costs, benefits, mistakes, comparisons, and next steps — not drama.
- Hook: 4-8 words, different wording AND structure from title.
- Context/why lines: max 8 words each.
- Captions: 6-8 unique items, max 5 words, include a topic keyword.
- BANNED caption fillers: "FULL TIME?", "MATCH ALERT", "TRENDING NOW", "GOAL ALERT".
- youtube_tags: include niche tags like "AI tools", "personal finance", "investing", "SaaS", "business tips" plus topic keywords.

Palette: pick 3 RGB colors — professional high-contrast for mobile.
Respond ONLY with valid JSON. No markdown fences. No explanation."""

# Default aliases for older call sites.
OPENAI_TOPIC_SELECTOR_SYSTEM = OPENAI_TOPIC_SELECTOR_VIRAL
SYSTEM_PROMPT = SYSTEM_PROMPT_VIRAL


def make_prompt(
    latest_topic: str,
    epilogue_extra: str | None = None,
    niche: str = "viral",
    recent_titles: list[str] | None = None,
) -> str:
    angles = HIGH_CPM_ANGLES if niche == "high_cpm" else VIRAL_ANGLES
    angle = random.choice(angles)
    # Force a structural family so the LLM doesn't collapse to one skeleton.
    hook_families = [
        "question",
        "bold claim",
        "contrarian statement",
        "direct fact",
        "how/why explainer",
        "imperative / watch-this",
    ]
    title_family = random.choice(hook_families)
    if niche == "high_cpm":
        package_line = "Create a HIGH-CPM (AI tools / finance / SaaS / business) YouTube Short package."
        framing = (
            "Frame this as a practical money/tool explainer when possible "
            "(cost, risk, productivity, beginner mistake, comparison)."
        )
        tags_hint = '["AI tools", "personal finance", "... 8-12 niche + trend tags"]'
        search_hint = "specific stock-image search phrase for office finance AI productivity"
    else:
        package_line = "Create a viral trending-topic YouTube Short package."
        framing = "Keep it discovery-friendly and broadly interesting."
        tags_hint = '["trend keyword 1", "trend keyword 2", "... 8-12 search tags"]'
        search_hint = "specific stock-image search phrase based on trend"

    base = f"""{package_line}

Current trending topic: {latest_topic}
Content angle: {angle}
Niche mode: {niche}
Required title opening style for THIS video only: {title_family}
(Do not reuse this same style next time — it is assigned per run.)

The output must be directly about this exact trend phrase.
{framing}
Do not turn it into fiction.
Do not fabricate facts, timelines, prices, or outcomes.
Do not convert it into a haunted or horror metaphor.
The title MUST contain words from "{latest_topic}" and stay under 55 characters.
The hook must use different wording AND a different sentence structure than the title.
CTA / close final line must be freshly phrased (not a stock "which side are you on").

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
  "close_lines": ["2 lines wrap-up", "fresh comment CTA"],
  "search_query": "{search_hint}",
  "youtube_tags": {tags_hint}
}}"""
    negatives = [t for t in (recent_titles or []) if str(t).strip()][-TITLE_HISTORY_NEGATIVES:]
    if negatives:
        listed = "\n".join(f"- {t}" for t in negatives)
        base += (
            "\n\nNEGATIVE EXAMPLES — recent titles already used. "
            "Do NOT copy their phrasing, openings, or sentence skeletons:\n"
            f"{listed}"
        )
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
    """Pick a named-entity seed while avoiding the last few picks (reduces duplicate titles)."""
    pool_src = HIGH_CPM_FALLBACKS if niche == "high_cpm" else VIRAL_FALLBACKS
    try:
        SEED_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        recent: list[str] = []
        if SEED_HISTORY_PATH.exists():
            data = json.loads(SEED_HISTORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                recent = [str(x) for x in data.get("recent_seeds", []) if str(x).strip()]
        avoid = set(s.lower() for s in recent[-4:])
        # Hard gate: never emit a generic category seed.
        pool = [
            s for s in pool_src
            if s.lower() not in avoid and passes_named_entity_gate(s)
        ]
        if not pool:
            pool = [s for s in pool_src if passes_named_entity_gate(s)]
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
    """Pick a topic from RSS, demand-filtered, niche-filtered, and named-entity gated."""
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
            # Hard named-entity gate: reject generic category topics before ranking.
            named_entries: list[dict] = []
            for e in pool_entries:
                if passes_named_entity_gate(e["topic"]):
                    named_entries.append(e)
                else:
                    log_entity_rejection(
                        "named_entity_gate",
                        e["topic"],
                        {
                            "demand": e["demand"],
                            "traffic": e["traffic"],
                            "news_count": e["news_count"],
                        },
                        reason="no_named_entity",
                    )
            if not named_entries:
                print(
                    f"  All RSS topics failed named-entity gate "
                    f"({len(pool_entries)} candidates); using {label} fallback seed."
                )
            else:
                topics = [e["topic"] for e in named_entries]
                preview = ", ".join(
                    f'{e["topic"]} ({e["demand"]})' for e in named_entries[:5]
                )
                print(f"  RSS named-entity topics (demand≥{thr}): {preview}")
        if topics:
            # Prefer niche-matching candidates first for OpenAI ranking.
            niche_first = [
                t for t in topics if _is_usable_topic(t) and _is_channel_fit_topic(t, niche)
            ]
            rank_pool = (niche_first + [t for t in topics if t not in niche_first])[:10]
            chosen, search_query = select_topic_with_openai(rank_pool, niche=niche)
            if chosen and _is_usable_topic(chosen):
                # Reject OpenAI picks that lack a concrete proper noun.
                if not passes_named_entity_gate(chosen):
                    log_entity_rejection(
                        "named_entity_gate.openai",
                        chosen,
                        {"rank_pool": rank_pool, "niche": niche},
                        reason="no_named_entity",
                    )
                    chosen = None
                    search_query = None
            if chosen and _is_usable_topic(chosen):
                # If OpenAI picked off-niche for high_cpm, try a niche candidate instead.
                if niche == "high_cpm" and not _is_channel_fit_topic(chosen, niche) and niche_first:
                    # Keep override only if it still clears the entity gate.
                    override = next(
                        (t for t in niche_first if passes_named_entity_gate(t)),
                        None,
                    )
                    if override:
                        chosen = override
                        search_query = None
                        print(f"  Trending topic seed: {chosen} ({label} override)")
                        return require_named_entity(
                            chosen,
                            source="pick_latest_topic.openai_override",
                            raw_upstream=topics,
                        ), search_query
                print(f"  Trending topic seed: {chosen} (OpenAI-selected, {label})")
                return require_named_entity(
                    chosen, source="pick_latest_topic.openai", raw_upstream=topics
                ), search_query
            for candidate in topics:
                if (
                    _is_usable_topic(candidate)
                    and _is_channel_fit_topic(candidate, niche)
                    and passes_named_entity_gate(candidate)
                ):
                    print(f"  Trending topic seed: {candidate} (latest {label})")
                    return require_named_entity(
                        candidate, source="pick_latest_topic.rss", raw_upstream=topics
                    ), None
            # Named-entity RSS topics exist but none niche-matched — take first gated topic.
            for candidate in topics:
                if _is_usable_topic(candidate) and passes_named_entity_gate(candidate):
                    print(f"  Trending topic seed: {candidate} (named-entity RSS, {label})")
                    return require_named_entity(
                        candidate, source="pick_latest_topic.rss_any", raw_upstream=topics
                    ), None
            print(f"  No {label} named-entity topic in RSS; using {label} fallback seed.")
        print(f"  Trending topic feed empty; using synthetic {label} seed.")
    except Exception as e:
        print(f"  Trending topic fetch failed: {e}")
    fallback_topic = pick_rotated_channel_fit_fallback(niche=niche)
    print(f"  Trending topic seed: {fallback_topic} ({label} fallback)")
    return require_named_entity(
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

    msg = (resp.get("choices") or [{}])[0].get("message") or {}
    raw = (msg.get("content") or msg.get("reasoning") or "").strip()
    if not raw:
        raise RuntimeError("Model returned empty response")
    # Strip <think>...</think> reasoning blocks (some models include them inline in content)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    # Strip markdown fences if model adds them
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        obj = _extract_json_object(raw)
        if obj is None:
            raise RuntimeError(f"Model returned non-JSON content: {raw[:180]!r}") from None
        return obj


def _extract_json_object(raw: str) -> dict | None:
    """Recover the JSON object from models that wrap it in commentary."""
    start = raw.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escaped = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(raw[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = raw.find("{", start + 1)
    return None


TITLE_BANNED_PHRASES = [
    "shock awaits", "nobody saw coming", "shocking turnaround", "flip the group",
    "schedule secrets", "group stage shock", "the truth", "worth it",
    "hype ya reality", "is it safe", "secrets:", "nobody is talking",
    "3 things to know", "flipped the conversation", "flipped the timeline",
    "could go either way", "the part that got buried", "trending now",
    "which side are you on", "comment your prediction", "explained in 30",
    "everyone's talking", "wait — this", "wait - this",
]

TITLE_MAX_CHARS = 55


def _sanitize_title(title: str, trend: str = "") -> str:
    """Strip spammy/formulaic scaffolds and enforce mobile-friendly length."""
    t = _compact_ws(title)
    low = t.lower()
    safe_trend = trend if is_valid_entity(trend, min_length=4) else ""
    if any(phrase in low for phrase in TITLE_BANNED_PHRASES) or _contains_formulaic_scaffold(t):
        t = _fallback_title_from_topic(safe_trend) if safe_trend else "Fresh update — what changed"
    if not is_valid_entity(t, min_length=4) or contains_invalid_publish_text(t):
        t = _fallback_title_from_topic(safe_trend) if safe_trend else "Fresh update — what changed"
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
        close_lines.append("Context is still moving — verify before sharing.")
    if not any("comment" in line.lower() or "reply" in line.lower() or "below" in line.lower() for line in close_lines):
        close_lines.append(random.choice(_CLOSE_CTA_POOL))
    # Replace formulaic CTAs if the LLM slipped.
    close_lines = [
        random.choice(_CLOSE_CTA_POOL)
        if _contains_formulaic_scaffold(line) or "which side are you on" in line.lower()
        else line
        for line in close_lines
    ]
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
            # Rebuild hook with a different structure instead of a fixed filler line.
            trend = _compact_ws(str(content.get("trend_topic") or title))
            content["hook"] = _anchor_hook(trend)
            hook = _compact_ws(str(content.get("hook", "")))
            if hook.lower() == tl or tl in hook.lower():
                content["hook"] = random.choice(
                    ["Cold open — stay with this.", "Short read, no fluff.", "Context first."]
                )

    title = _compact_ws(str(content.get("title", "")))
    low = title.lower()
    half = max(8, len(low) // 2)
    if len(low) >= 24 and low[:half] == low[half : half * 2]:
        content["title"] = _compact_ws(title[: len(title) // 2])

    return content


# Structurally distinct title families — NOT synonym rotations of one scaffold.
# Each tuple: (family_id, template). {t} = topic phrase.
_TITLE_STRUCTURES: list[tuple[str, str]] = [
    # question
    ("question", "Did {t} just rewrite the odds?"),
    ("question", "Is {t} the real story?"),
    ("question", "Who benefits from {t}?"),
    ("question", "What if {t} sticks?"),
    ("question", "Where does {t} go next?"),
    # bold claim
    ("claim", "{t} just broke the usual script"),
    ("claim", "{t} is rewriting the feed"),
    ("claim", "{t} hit harder than expected"),
    ("claim", "{t} forced a new narrative"),
    ("claim", "{t} moved the goalposts"),
    # contrarian
    ("contrarian", "Most takes on {t} miss this"),
    ("contrarian", "Ignore the loud take on {t}"),
    ("contrarian", "{t} isn't the plot twist people want"),
    ("contrarian", "The quiet angle on {t}"),
    ("contrarian", "Stop overreading {t}"),
    # direct fact
    ("fact", "{t}: the detail that stuck"),
    ("fact", "{t} — timeline in one breath"),
    ("fact", "Inside {t}, stripped down"),
    ("fact", "{t}, no fluff"),
    ("fact", "Plain read: {t}"),
    # how/why
    ("how_why", "How {t} got here so fast"),
    ("how_why", "Why {t} is everywhere tonight"),
    ("how_why", "How search caught up to {t}"),
    ("how_why", "Why {t} won't stay quiet"),
    ("how_why", "How {t} spilled over"),
    # imperative
    ("imperative", "Catch {t} before the take hardens"),
    ("imperative", "Read {t} without the noise"),
    ("imperative", "Sit with {t} for 20 seconds"),
    ("imperative", "Don't scroll past {t}"),
    ("imperative", "Clock {t} while it's live"),
    # comparison / stakes
    ("compare", "{t} vs the expected story"),
    ("compare", "{t}: hype vs substance"),
    ("compare", "Early read on {t}'s stakes"),
    ("compare", "{t} under pressure"),
    ("compare", "Pressure test: {t}"),
]

_HOOK_STRUCTURES: list[tuple[str, str]] = [
    ("question", "So… what's actually up with {t}?"),
    ("question", "Hold up — {t}?"),
    ("claim", "{t} just landed."),
    ("claim", "Here's {t}, cut short."),
    ("contrarian", "Skip the loud take on {t}."),
    ("contrarian", "{t} is quieter than the noise."),
    ("fact", "{t}. Straight facts."),
    ("fact", "Cold open: {t}."),
    ("how_why", "How {t} got loud."),
    ("how_why", "Why {t} matters now."),
    ("imperative", "Look at {t} first."),
    ("imperative", "Start with {t}."),
]

_CONTEXT_STRUCTURES: list[tuple[str, str]] = [
    ("fact", "{t} is in heavy search."),
    ("fact", "Coverage around {t} spiked."),
    ("claim", "{t} is driving the chat."),
    ("claim", "Feeds keep looping {t}."),
    ("contrarian", "Not everyone buys the {t} take."),
    ("contrarian", "Split opinions on {t}."),
    ("how_why", "Here's the push behind {t}."),
    ("how_why", "Context for {t}, fast."),
    ("question", "Why is {t} everywhere?"),
    ("question", "What's fueling {t}?"),
]

_CLOSE_CTA_POOL = [
    "What do you make of it? Say so below.",
    "Would you bet on this outcome? Reply.",
    "Honest read — leave yours in comments.",
    "If you disagree, write why under this.",
    "One-line verdict in the comments.",
    "Curious what you'd do — tell me.",
    "Your angle beats mine? Prove it below.",
    "Vote with a comment: buy-in or pass.",
    "Leave the take you won't say out loud.",
    "Type the detail everyone is skipping.",
    "Reply with the counter-argument.",
    "Drop the version of this you'd trust.",
]

_ANCHOR_JOINERS = [
    "{e}: {ti}",
    "{ti} — {e}",
    "{e} — {ti}",
    "{ti} ({e})",
    "{e} | {ti}",
]

# Phrases that signal the old formulaic scaffolds — sanitize/regenerate if seen.
_FORMULAIC_TITLE_MARKERS = (
    "3 things to know",
    "wait — this",
    "wait - this",
    "before ",
    ", know this",
    "flipped the conversation",
    "flipped the timeline",
    "could go either way",
    "the part that got buried",
    "trending now",
    "which side are you on",
    "comment your prediction",
    "explained in 30",
    "everyone's talking",
    "shock awaits",
    "nobody saw coming",
)


def _stable_hash(text: str) -> int:
    """Deterministic integer hash used for stable picks when needed."""
    return int(hashlib.md5(str(text).encode("utf-8", errors="replace")).hexdigest(), 16)


def _rotating_choice(pool: list[str], *, category: str, topic: str) -> str:
    """Pick from a pool with date+topic salt (kept for callers that need stability)."""
    n = len(pool)
    if n == 0:
        return ""
    from datetime import date
    salt = date.today().isoformat()
    return pool[_stable_hash(f"{category}:{topic}:{salt}") % n]


def _load_title_history() -> list[dict]:
    try:
        if not TITLE_HISTORY_PATH.exists():
            return []
        data = json.loads(TITLE_HISTORY_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            rows = data.get("titles", [])
        elif isinstance(data, list):
            rows = data
        else:
            return []
        return [r for r in rows if isinstance(r, dict) and str(r.get("title", "")).strip()]
    except Exception as e:
        print(f"  [WARN] title history read failed: {e}")
        return []


def recent_title_strings(limit: int = TITLE_HISTORY_NEGATIVES) -> list[str]:
    rows = _load_title_history()
    out: list[str] = []
    for r in rows[-max(1, limit) :]:
        t = _compact_ws(str(r.get("title", "")))
        if t:
            out.append(t)
    return out


def record_generated_title(
    title: str,
    *,
    hook: str = "",
    family: str = "",
    trend: str = "",
) -> None:
    """Append a generated title so later runs can treat it as a negative example."""
    try:
        TITLE_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        rows = _load_title_history()
        rows.append(
            {
                "title": _compact_ws(title),
                "hook": _compact_ws(hook),
                "family": family or title_structure_family(title),
                "trend": _compact_ws(trend),
                "utc": datetime.now(timezone.utc).isoformat(),
            }
        )
        TITLE_HISTORY_PATH.write_text(
            json.dumps({"titles": rows[-TITLE_HISTORY_MAX:]}, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"  [WARN] title history write failed: {e}")


def title_structure_family(title: str) -> str:
    """Coarse structural family label for diversity checks."""
    t = _compact_ws(title).lower()
    if not t:
        return "empty"
    if t.startswith(("how ", "why ")):
        return "how_why"
    if t.endswith("?") or t.startswith(("did ", "is ", "who ", "what ", "where ")):
        return "question"
    if t.startswith(("most ", "ignore ", "stop ", "don't ", "dont ")):
        return "contrarian"
    if t.startswith(("catch ", "read ", "sit ", "clock ", "look ", "start ", "don't scroll", "dont scroll")):
        return "imperative"
    if " vs " in f" {t} " or t.startswith("pressure test") or t.startswith("early read"):
        return "compare"
    if ":" in t or "—" in t or " - " in t:
        return "fact"
    return "claim"


def _strip_entity_skeleton(title: str, entity: str = "") -> str:
    """Approximate structural skeleton by blanking the entity phrase."""
    t = _compact_ws(title).lower()
    e = _compact_ws(entity).lower()
    if e and e in t:
        t = t.replace(e, "{t}")
    else:
        # Blank longest capitalized-ish token runs heuristically
        t = re.sub(r"\b[a-z0-9][a-z0-9'&+.-]{2,}\b", "{x}", t, count=3)
    return re.sub(r"\s+", " ", t).strip()


def _family_cooldown() -> int:
    try:
        return max(1, int(env_value("TITLE_FAMILY_COOLDOWN", str(TITLE_FAMILY_COOLDOWN)) or TITLE_FAMILY_COOLDOWN))
    except (TypeError, ValueError):
        return TITLE_FAMILY_COOLDOWN


def _recent_families_and_skeletons(limit: int | None = None) -> tuple[set[str], set[str]]:
    """Return families/skeletons that must be avoided (hard cooldown window)."""
    cool = _family_cooldown() if limit is None else max(1, limit)
    rows = _load_title_history()[-cool:]
    families = {
        str(r.get("family") or title_structure_family(str(r.get("title", ""))))
        for r in rows
        if str(r.get("title", "")).strip()
    }
    skeletons = {
        _strip_entity_skeleton(str(r.get("title", "")), str(r.get("trend", "")))
        for r in rows
    }
    skeletons.discard("")
    return families, skeletons


def _contains_formulaic_scaffold(text: str) -> bool:
    low = _compact_ws(text).lower()
    if not low:
        return False
    if "before " in low and "know this" in low:
        return True
    markers = [m for m in _FORMULAIC_TITLE_MARKERS if m not in ("before ", ", know this")]
    return any(m in low for m in markers)


def _pick_structure(
    structures: list[tuple[str, str]],
    *,
    topic: str,
    avoid_families: set[str] | None = None,
    avoid_skeletons: set[str] | None = None,
) -> tuple[str, str]:
    """Pick a (family, template). Hard-avoid recent families when any alternative exists."""
    avoid_families = avoid_families or set()
    avoid_skeletons = avoid_skeletons or set()
    rng = random.Random(
        _stable_hash(f"{topic}:{datetime.now(timezone.utc).isoformat()}:{random.random()}")
    )

    def _candidates(*, skip_families: bool, skip_skeletons: bool) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for fam, tmpl in structures:
            filled = tmpl.format(t=topic)
            skel = _strip_entity_skeleton(filled, topic)
            if skip_families and fam in avoid_families:
                continue
            if skip_skeletons and skel in avoid_skeletons:
                continue
            if _contains_formulaic_scaffold(filled):
                continue
            out.append((fam, tmpl))
        return out

    # Prefer: new family AND new skeleton → new family → new skeleton → anything.
    pool = (
        _candidates(skip_families=True, skip_skeletons=True)
        or _candidates(skip_families=True, skip_skeletons=False)
        or _candidates(skip_families=False, skip_skeletons=True)
        or _candidates(skip_families=False, skip_skeletons=False)
        or list(structures)
    )
    return rng.choice(pool)


def _anchor_title(entity: str, existing_title: str = "") -> str:
    """Re-attach trend entity without collapsing to a fixed scaffold."""
    e = _compact_ws(entity or "").strip()
    ti = _compact_ws(existing_title or "").strip()
    if not is_valid_entity(e, min_length=4):
        return _sanitize_title(ti or _fallback_title_from_topic(""), trend="")
    if not ti or contains_invalid_publish_text(ti) or _contains_formulaic_scaffold(ti):
        return _sanitize_title(_fallback_title_from_topic(e), trend=e)
    if e.lower() in ti.lower():
        return _sanitize_title(ti, trend=e)
    joiner = random.choice(_ANCHOR_JOINERS)
    return _sanitize_title(joiner.format(e=e, ti=ti), trend=e)


def _anchor_hook(topic: str) -> str:
    """Topic-anchored hook with a structurally distinct family from recent titles."""
    t = _compact_ws(topic or "").strip()
    if not is_valid_entity(t, min_length=4):
        return random.choice(["Cold open — listen up.", "Straight to the point.", "Here's the short read."])
    families, skeletons = _recent_families_and_skeletons()
    fam, tmpl = _pick_structure(
        _HOOK_STRUCTURES, topic=t, avoid_families=families, avoid_skeletons=skeletons
    )
    return tmpl.format(t=t)


def _anchor_context_lines(topic: str) -> list[str]:
    """Three topic-anchored context lines from mixed structural families."""
    t = _compact_ws(topic or "").strip()
    base = t if is_valid_entity(t, min_length=4) else "this update"
    families, _ = _recent_families_and_skeletons()
    used: set[str] = set()
    lines = [base]
    pool = list(_CONTEXT_STRUCTURES)
    random.shuffle(pool)
    for fam, tmpl in pool:
        if fam in used and len(used) < 3:
            continue
        if fam in families and len(lines) < 2:
            # Prefer unused families early, but don't stall.
            continue
        lines.append(tmpl.format(t=base))
        used.add(fam)
        if len(lines) >= 3:
            break
    while len(lines) < 3:
        fam, tmpl = random.choice(_CONTEXT_STRUCTURES)
        lines.append(tmpl.format(t=base))
    return lines[:3]


def _fallback_close_lines() -> list[str]:
    return [
        "Context moves fast — check the source before you share.",
        random.choice(_CLOSE_CTA_POOL),
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
    """Build a title from structurally distinct families — not one scaffold bank."""
    topic = _compact_ws(latest_topic or "").strip(" -:")
    if not is_valid_entity(topic, min_length=4):
        topic = "this update"
    families, skeletons = _recent_families_and_skeletons()
    fam, tmpl = _pick_structure(
        _TITLE_STRUCTURES,
        topic=topic,
        avoid_families=families,
        avoid_skeletons=skeletons,
    )
    title = tmpl.format(t=topic)
    # Persist family hint on the string via sanitize only; callers record family later.
    _ = fam
    if len(title) > TITLE_MAX_CHARS:
        cut = title[: TITLE_MAX_CHARS - 3].rsplit(" ", 1)[0]
        title = (cut or title[: TITLE_MAX_CHARS - 3]) + "..."
    return title


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
    focus = lt or "this update"
    title = _fallback_title_from_topic(lt)
    base["title"] = title
    base["hook"] = _anchor_hook(focus)
    base["context_lines"] = _anchor_context_lines(focus)
    base["why_lines"] = [
        "Search interest jumped fast.",
        "Takes online are already splitting.",
        "People want a clean read now.",
    ]
    base["question"] = random.choice(
        [
            "What stands out to you?",
            "Does this change your view?",
            "Buy the take or pass?",
            "What's the missing piece?",
        ]
    )
    base["captions"] = _unique_fallback_captions(focus)
    base["close_lines"] = _fallback_close_lines()
    if lt:
        base["search_query"] = lt
    base["trend_topic"] = lt
    base["_title_family"] = title_structure_family(title)
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
        latest_topic = require_named_entity(
            latest_topic,
            source="generate_topic.pick_latest_topic",
            raw_upstream={"niche": niche, "slot": slot},
        )
    except EntityValidationError as exc:
        log_entity_rejection(
            "generate_topic.pick_latest_topic",
            latest_topic,
            raw_upstream={"niche": niche, "slot": slot, "error": str(exc)},
            reason="no_named_entity",
        )
        latest_topic = pick_rotated_channel_fit_fallback(niche=niche)
        latest_topic = require_named_entity(
            latest_topic,
            source="generate_topic.recovered_fallback",
            raw_upstream={"niche": niche, "slot": slot},
        )
    prompt = make_prompt(
        latest_topic,
        epilogue_extra,
        niche=niche,
        recent_titles=recent_title_strings(TITLE_HISTORY_NEGATIVES),
    )
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
            # Kill residual formulaic scaffolds from the LLM.
            if _contains_formulaic_scaffold(str(content.get("title", ""))):
                content["title"] = _fallback_title_from_topic(latest_topic)
            if _contains_formulaic_scaffold(str(content.get("hook", ""))):
                content["hook"] = _anchor_hook(latest_topic)
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
            content = _apply_similarity_guard(content, latest_topic)
            record_generated_title(
                str(content.get("title", "")),
                hook=str(content.get("hook", "")),
                family=title_structure_family(str(content.get("title", ""))),
                trend=latest_topic,
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
    out = _apply_similarity_guard(out, latest_topic)
    record_generated_title(
        str(out.get("title", "")),
        hook=str(out.get("hook", "")),
        family=str(out.pop("_title_family", "") or title_structure_family(str(out.get("title", "")))),
        trend=latest_topic,
    )
    return out


def _apply_similarity_guard(content: dict, trend: str) -> dict:
    """Hard-reject near-duplicate title/opener/CTA vs the published catalog; regenerate."""
    from similarity_guard import SimilarityRejectError, diversify_content_against_catalog

    def _regen_title(entity: str, _content: dict) -> str:
        return _fallback_title_from_topic(entity or trend)

    def _regen_opener(entity: str, _content: dict) -> str:
        return _anchor_hook(entity or trend)

    def _regen_cta(entity: str, _content: dict) -> list[str]:
        return _fallback_close_lines()

    # Family cooldown: if title family was used in the last N videos, force a new family now.
    recent_families, _ = _recent_families_and_skeletons()
    fam = title_structure_family(str(content.get("title", "")))
    if fam and fam in recent_families:
        print(f"  [SIM] title family '{fam}' still in cooldown — regenerating title")
        content["title"] = _fallback_title_from_topic(trend)
        content["hook"] = _anchor_hook(trend)

    try:
        content, scores = diversify_content_against_catalog(
            content,
            regenerate_title=_regen_title,
            regenerate_opener=_regen_opener,
            regenerate_cta=_regen_cta,
            max_attempts=int(env_value("SIMILARITY_MAX_ATTEMPTS", "6") or "6"),
        )
        # Second family check after diversification.
        fam2 = title_structure_family(str(content.get("title", "")))
        if fam2 in recent_families:
            content["title"] = _fallback_title_from_topic(trend)
            fam2 = title_structure_family(str(content.get("title", "")))
            scores = content.get("_similarity") or scores
        print(
            f"  Similarity: title={scores['title']['score']:.3f} "
            f"opener={scores['opener']['score']:.3f} "
            f"cta={scores['cta']['score']:.3f} "
            f"family={fam2} "
            f"(catalog={scores.get('catalog_size', 0)})"
        )
        return content
    except SimilarityRejectError as exc:
        log_entity_rejection(
            "similarity_guard",
            content.get("title"),
            {"scores": exc.scores, "trend": trend},
            reason="similarity_reject",
        )
        # Last-resort diversification even after failed attempts.
        content["title"] = _fallback_title_from_topic(trend)
        content["hook"] = _anchor_hook(trend)
        content["close_lines"] = _fallback_close_lines()
        content["_similarity"] = exc.scores
        content["_similarity_failed"] = True
        print(f"  [WARN] Similarity guard exhausted retries; forced fresh scaffolds ({exc})")
        return content


if __name__ == "__main__":
    import sys

    content = generate_topic()
    print(json.dumps(content, indent=2))

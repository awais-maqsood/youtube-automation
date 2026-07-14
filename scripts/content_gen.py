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

ANGLES = [
    "match result breakdown",
    "group stage stakes",
    "star player spotlight",
    "upset prediction short",
    "VAR controversy explainer",
]

CHANNEL_FIT_KEYWORDS = [
    "fifa", "world", "cup", "football", "soccer", "goal", "goals", "match",
    "group", "knockout", "final", "semifinal", "quarterfinal", "penalty",
    "var", "referee", "stadium", "fan", "fans", "team", "teams", "player",
    "players", "score", "win", "loss", "draw", "qualify", "qualification",
    "argentina", "brazil", "france", "germany", "spain", "england", "italy",
    "portugal", "mexico", "usa", "canada", "mbappe", "messi", "ronaldo",
    "neymar", "haaland", "bellingham", "modric", "kane", "salah", "yamal",
    "coach", "tactics", "lineup", "injury", "transfer", "golden", "boot",
]

# Used when Trends RSS has no channel-fit row. Rotated (see pick_rotated_channel_fit_fallback)
# so the same seed is not picked run after run.
CHANNEL_FIT_FALLBACKS = [
    "world cup 2026 standings",
    "fifa world cup schedule today",
    "messi world cup 2026",
    "ronaldo world cup",
    "world cup golden boot race",
    "world cup var controversy",
    "usa world cup host nation",
    "mexico world cup stadium",
    "argentina world cup defense",
    "brazil world cup squad",
    "england world cup knockout",
    "france world cup lineup",
    "world cup penalty shootout",
    "world cup fan atmosphere",
    "world cup group of death",
]

OPENAI_TOPIC_SELECTOR_SYSTEM = """You select the best YouTube Shorts topic for a FIFA World Cup football channel.

Channel style:
- FIFA World Cup 2026, international football, matches, players, teams
- match breakdowns, group stage drama, knockout stakes, fan reactions
- light Hinglish/Urdu phrasing is acceptable
- prefer topics that can become titles like:
  - a bold match claim ("Brazil just sent a message to everyone")
  - a curiosity gap ("Nobody is talking about this World Cup upset")
  - a stakes hook ("One goal changes the entire group")
  - a player spotlight ("This striker is carrying his nation")
Avoid repetitive "Who Wins?" / "Overrated?" endings.

Return ONLY valid JSON:
{
  "selected_topic": "string",
  "reason": "short reason",
  "search_query": "specific image/topic search query for football stadium fans pitch"
}
"""

SYSTEM_PROMPT = """You write 30-second vertical YouTube Shorts for a FIFA World Cup football channel.

The short should feel topical, fast, bold, and highly clickable.
Do NOT write fiction, horror, haunted internet stories, creepypasta, or made-up events.
Base everything on the supplied trending topic and keep the wording broad enough to avoid invented match scores or fake results.
Stick to widely understood football/World Cup framing — do not invent specific final scores or confirmed lineups.

CRITICAL — SEARCH & TRUST RULES:
- NEVER invent "Team A vs Team B" matchups unless BOTH team names literally appear in the trending topic phrase.
- Do NOT fabricate fixtures (e.g. Panama vs Croatia) — viewers search real games; fake matchups get zero views.
- Title MUST include the exact trending topic phrase or a recognizable team/player name from it.
- Title max 55 characters (mobile feed truncates longer titles).
- BANNED title clichés (never use): "Shock Awaits", "Nobody Saw Coming", "Shocking Turnaround", "Flip the Group", "Schedule Secrets", "The Truth", "Worth It", "Hype Ya Reality".
- Write titles people actually search: player names, real teams, "World Cup 2026", standings, VAR, golden boot, knockout.

The video has 5 acts:
1. HOOK — a strong first-line headline about the topic (must grab attention in 2 seconds)
2. CONTEXT — 3 short lines that explain what it is
3. WHY — 3 short lines explaining why people care
4. QUESTION — one open-loop question plus 6 to 8 rapid short captions
5. CLOSE — 2 or 3 short lines; LAST line must ask viewers to comment their prediction

Style rules:
- Write like a viral football/World Cup explainer short, not a documentary.
- Use a mix of simple English with light Hinglish/Urdu phrasing where natural.
- Focus on matches, teams, players, group stages, knockouts, VAR, fan drama, and tournament stakes.
- Be punchy, simple, broad, and readable on screen.
- Avoid unverifiable specifics unless the topic itself is widely understood from the trend phrase.
- TITLE RULES (important — titles must NOT look templated):
  - Vary structure every time. Good examples:
    - "Messi just did WHAT at the World Cup?"
    - "World Cup 2026 standings changed overnight"
    - "This VAR call broke the internet"
    - "Brazil's group just got scary"
  - Front-load the searchable keyword (team, player, or World Cup).
- Hook: 4-8 words, punchy, different wording from title.
- Context lines: 3 short lines, max 8 words each.
- Why lines: 3 short lines, max 8 words each.
- Question: 4-10 words, must invite comments.
- Captions: 6 to 8 items, max 5 words each, energetic but factual.
- CAPTION UNIQUENESS (critical for Shorts thumbnails — YouTube auto-picks climax frames):
  - Every caption MUST include a concrete word from the trending topic (team, player, or event).
  - No two videos should share the same caption set. Invent fresh phrasing every time.
  - BANNED caption fillers (never use): "FULL TIME?", "MATCH ALERT", "TRENDING NOW",
    "GOAL ALERT", "QUICK BREAKDOWN", "GROUP STAKES", "WHO ADVANCES?", "MUST WIN?".
- Close lines: 2 or 3 short lines; final line = comment CTA ("Comment your pick 👇" style, no emoji in JSON).
- youtube_tags: 8-12 search tags (team names, "FIFA World Cup 2026", "football shorts", etc.)

Palette: pick 3 RGB colors — pitch green, stadium gold, high contrast for mobile screens.

Respond ONLY with valid JSON. No markdown fences. No explanation."""


def make_prompt(latest_topic: str, epilogue_extra: str | None = None) -> str:
    angle = random.choice(ANGLES)
    base = f"""Create a 30-second FIFA World Cup football YouTube Short package.

Current trending topic: {latest_topic}
Content angle: {angle}

The output must be directly about this exact trend phrase.
NEVER invent a "Team A vs Team B" matchup unless both teams are in "{latest_topic}".
If the trend is not obviously football/World Cup related, reinterpret it through football/match/tournament/fan angle only if that still feels natural.
Prefer match breakdown, group stage stakes, player spotlight, upset prediction, or VAR controversy framing.
Do not turn it into fiction.
Do not invent fake match scores, fake fixtures, or confirmed lineups.
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
  "search_query": "specific stock-image search phrase for football stadium fans world cup",
  "youtube_tags": ["FIFA World Cup 2026", "football", "... 8-12 search tags"]
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
        "world cup", "fifa world", "group stage", "knockout stage",
        "penalty shootout", "golden boot", "var decision",
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
                    "content": "Choose the best topic from this live list for FIFA World Cup football Shorts:\n"
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
    if any(phrase in low for phrase in TITLE_BANNED_PHRASES):
        t = _fallback_title_from_topic(trend) if trend else "World Cup 2026 update"
    if len(t) > TITLE_MAX_CHARS:
        cut = t[: TITLE_MAX_CHARS - 3].rsplit(" ", 1)[0]
        t = (cut or t[: TITLE_MAX_CHARS - 3]) + "..."
    return t


def _default_youtube_tags(trend: str = "") -> list[str]:
    tags = [
        "FIFA World Cup 2026", "World Cup", "football", "soccer",
        "World Cup shorts", "football shorts", "FIFA", "soccer shorts",
    ]
    for word in re.findall(r"[A-Za-z0-9]+", trend or ""):
        if len(word) >= 4 and word.lower() not in {
            "world", "cup", "fifa", "2026", "match", "group", "stage", "today",
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
        content["title"] = "World Cup - What Just Happened?"
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
        content["question"] = "Who takes it... ya draw?"

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
        for text, color in _unique_fallback_captions(trend or content.get("title") or "World Cup"):
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
        tags = _default_youtube_tags(trend or content.get("title", ""))
    content["youtube_tags"] = tags[:12]

    if not content.get("search_query"):
        content["search_query"] = str(content.get("search_query") or content.get("title", "")).strip() or "fifa world cup football stadium fans"

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
                content["hook"] = "Quick match breakdown + stakes."
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
_TITLE_TEMPLATES_GENERIC = [
    "{t} — what changed?",
    "World Cup: {t}",
    "{t} explained in 30 sec",
    "Everyone's talking about {t}",
]
_TITLE_TEMPLATES_VS = [
    "{t}: who actually wins?",
    "{t} — the gap is bigger than expected",
]
_TITLE_TEMPLATES_PRICE = [
    "{t}: what just changed",
    "Why {t} caught everyone off guard",
]
_TITLE_TEMPLATES_FEATURE = [
    "{t}: the part nobody mentions",
    "What {t} actually changes",
    "{t} is quietly a big upgrade",
]
_TITLE_TEMPLATES_MATCH = [
    "Can {t} survive the group?",
    "{t}: the stakes are insane",
    "Before {t}, know this",
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
    focus = _compact_ws(focus or "").strip() or "World Cup"
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
    if not topic:
        return random.choice(_TITLE_TEMPLATES_GENERIC).format(t="this trend")
    topic_l = topic.lower()
    if " vs " in f" {topic_l} ":
        pool = _TITLE_TEMPLATES_VS
    elif "price" in topic_l or "stock" in topic_l:
        pool = _TITLE_TEMPLATES_PRICE
    elif "feature" in topic_l or "update" in topic_l:
        pool = _TITLE_TEMPLATES_FEATURE
    elif any(w in topic_l for w in ("match", "cup", "fifa", "goal", "group", "final")):
        pool = _TITLE_TEMPLATES_MATCH
    else:
        pool = _TITLE_TEMPLATES_GENERIC
    return random.choice(pool).format(t=topic)


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
    base["captions"] = _unique_fallback_captions(focus)
    base["close_lines"] = [
        "Tournament tez chal raha hai, details check zaroor karo.",
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
        "title": "World Cup 2026 standings update",
        "topic_id": "world_cup_standings",
        "palette": [[34, 139, 34], [255, 215, 0], [255, 255, 255]],
        "hook": "Standings just shifted!",
        "context_lines": ["Every match reshapes the bracket.", "Three points change everything.", "One slip can end a run."],
        "why_lines": ["Fans track every goal live.", "Social feeds explode after upsets.", "Knockout spots stay wide open."],
        "question": "Kaun group se nikal jayega?",
        "captions": [
            ["BRACKET SHIFT", [255, 255, 255]],
            ["THREE POINTS", [255, 220, 120]],
            ["FORM CHECK", [255, 150, 150]],
            ["NET PRESSURE", [255, 255, 255]],
            ["PATH WIDENS", [255, 220, 120]],
            ["PICK YOUR SIDE", [255, 150, 150]],
        ],
        "close_lines": ["Har match bracket badal sakta hai.", "Comment your pick below."],
        "search_query": "fifa world cup group stage stadium fans",
        "youtube_tags": ["FIFA World Cup 2026", "World Cup standings", "football", "soccer shorts"],
    },
    {
        "title": "Messi at World Cup 2026",
        "topic_id": "messi_world_cup",
        "palette": [[120, 200, 255], [255, 220, 0], [255, 255, 255]],
        "hook": "Messi ka World Cup moment!",
        "context_lines": ["All eyes on the legend.", "Every touch gets analyzed.", "Legacy still on the line."],
        "why_lines": ["Global fanbase is massive.", "Highlights trend instantly.", "Debate never stops online."],
        "question": "Messi ka best moment kaunsa?",
        "captions": [
            ["MESSI WATCH", [255, 255, 255]],
            ["LEGEND MODE", [255, 220, 120]],
            ["CUP STAGE", [255, 180, 120]],
            ["FAN FEVER", [255, 255, 255]],
            ["MAGIC TOUCH", [255, 220, 120]],
            ["YOUR PICK?", [255, 180, 120]],
        ],
        "close_lines": ["Form match day pe dikhta hai.", "Comment your pick below."],
        "search_query": "messi world cup football stadium celebration",
        "youtube_tags": ["Messi", "FIFA World Cup 2026", "Argentina", "football shorts"],
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
    return validate(content, latest_topic)


if __name__ == "__main__":
    import sys

    content = generate_topic()
    print(json.dumps(content, indent=2))

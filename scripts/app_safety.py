#!/usr/bin/env python3
"""
BlinkViral "Is It Safe? The TRUTH" series — fixed app queue + verdict packages.

Search-intent Shorts for South Asian 18–34 mobile audiences. Titles stay locked
to the breakout pattern. Scripts stay 15–20s verdict format (not full reviews).
"""

from __future__ import annotations

import json
import os
import random
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
QUEUE_STATE_PATH = ROOT / "data" / "app_safety_queue.json"

# Ranked by likely IN/PK/BD mobile search interest. Verdicts are safety education
# (risk warnings), not install guides.
APP_SAFETY_QUEUE: list[dict[str, Any]] = [
    {
        "id": "vidmate",
        "name": "Vidmate",
        "search_query": "android phone download app screen colorful desk",
        "verdict": "HIGH RISK",
        "red_flags": [
            "Not on Play Store",
            "Asks weird permissions",
            "Ads open random links",
        ],
    },
    {
        "id": "snaptube",
        "name": "Snaptube",
        "search_query": "smartphone video download notification bright room",
        "verdict": "HIGH RISK",
        "red_flags": [
            "Sideloaded APK only",
            "Aggressive ad network",
            "Unknown update source",
        ],
    },
    {
        "id": "movie_box",
        "name": "Movie Box",
        "search_query": "phone streaming movies couch colorful living room",
        "verdict": "AVOID",
        "red_flags": [
            "Pirated streams",
            "Clone sites everywhere",
            "Malware-ridden APKs",
        ],
    },
    {
        "id": "cinema_hd",
        "name": "Cinema HD",
        "search_query": "android tv box living room daylight",
        "verdict": "AVOID",
        "red_flags": [
            "Unofficial APK builds",
            "Untrusted mirrors",
            "Account phishing risk",
        ],
    },
    {
        "id": "terabox",
        "name": "TeraBox",
        "search_query": "cloud storage phone files bright office",
        "verdict": "CAUTION",
        "red_flags": [
            "Heavy ad prompts",
            "Privacy fine print",
            "Watch shared-link sources",
        ],
    },
    {
        "id": "flixvision",
        "name": "FlixVision",
        "search_query": "mobile streaming app interface colorful",
        "verdict": "AVOID",
        "red_flags": [
            "Not official Play Store",
            "Clone APK spam",
            "Risky third-party links",
        ],
    },
    {
        "id": "showbox",
        "name": "Showbox",
        "search_query": "phone movie night popcorn colorful sofa",
        "verdict": "AVOID",
        "red_flags": [
            "Long-dead originals",
            "Fake clones dominate",
            "High malware reports",
        ],
    },
    {
        "id": "mobdro",
        "name": "Mobdro",
        "search_query": "live sports on phone bright cafe",
        "verdict": "AVOID",
        "red_flags": [
            "Shutdown history",
            "Fake APK resurfaces",
            "No trusted publisher",
        ],
    },
    {
        "id": "teatv",
        "name": "TeaTV",
        "search_query": "android tablet streaming daylight room",
        "verdict": "AVOID",
        "red_flags": [
            "Unofficial builds",
            "Tracker-heavy APKs",
            "Phishing update pages",
        ],
    },
    {
        "id": "beetv",
        "name": "BeeTV",
        "search_query": "phone binge watching colorful bedroom",
        "verdict": "AVOID",
        "red_flags": [
            "Sideload only",
            "Clone APK flood",
            "Unsafe permissions",
        ],
    },
    {
        "id": "cyberflix",
        "name": "CyberFlix TV",
        "search_query": "smart tv streaming apps colorful living room",
        "verdict": "AVOID",
        "red_flags": [
            "Not Play Store official",
            "Random APK hosts",
            "Credential steal risk",
        ],
    },
    {
        "id": "apkpure",
        "name": "APKPure",
        "search_query": "android app store phone screen bright desk",
        "verdict": "CAUTION",
        "red_flags": [
            "Third-party store",
            "Verify publisher name",
            "Skip unknown mirrors",
        ],
    },
    {
        "id": "aptoide",
        "name": "Aptoide",
        "search_query": "android apps marketplace phone daylight",
        "verdict": "CAUTION",
        "red_flags": [
            "User-uploaded stores",
            "Check app signatures",
            "Avoid cracked listings",
        ],
    },
    {
        "id": "happymod",
        "name": "HappyMod",
        "search_query": "android games phone colorful desk setup",
        "verdict": "HIGH RISK",
        "red_flags": [
            "Modded APKs",
            "Account ban risk",
            "Hidden adware common",
        ],
    },
    {
        "id": "lucky_patcher",
        "name": "Lucky Patcher",
        "search_query": "android security settings phone screen",
        "verdict": "AVOID",
        "red_flags": [
            "Breaks app security",
            "Ban / malware risk",
            "Often bundled malware",
        ],
    },
    {
        "id": "free_vpn_apps",
        "name": "Free VPN Apps",
        "search_query": "vpn lock icon smartphone colorful background",
        "verdict": "HIGH RISK",
        "red_flags": [
            "Sells your traffic",
            "Fake no-logs claims",
            "Avoid unknown free VPNs",
        ],
    },
    {
        "id": "urban_vpn",
        "name": "Urban VPN",
        "search_query": "vpn app phone map colorful travel",
        "verdict": "CAUTION",
        "red_flags": [
            "Free VPN tradeoffs",
            "Read data policy",
            "Prefer known paid VPNs",
        ],
    },
    {
        "id": "1dm",
        "name": "1DM Downloader",
        "search_query": "phone download manager progress bar bright",
        "verdict": "CAUTION",
        "red_flags": [
            "Install from known source",
            "Watch ad permissions",
            "Skip cracked clones",
        ],
    },
    {
        "id": "fake_vlc",
        "name": "Fake VLC APK",
        "search_query": "vlc media player phone tablet colorful desk",
        "verdict": "AVOID",
        "red_flags": [
            "Lookalike package names",
            "Only trust VideoLAN",
            "Fake sites push malware",
        ],
    },
    {
        "id": "gb_whatsapp",
        "name": "GB WhatsApp",
        "search_query": "whatsapp chat phone colorful cafe table",
        "verdict": "AVOID",
        "red_flags": [
            "Ban risk from Meta",
            "Message interception risk",
            "Unofficial mods unsafe",
        ],
    },
]

_APP_BY_ID = {a["id"]: a for a in APP_SAFETY_QUEUE}
_APP_BY_NAME = {a["name"].lower(): a for a in APP_SAFETY_QUEUE}


def channel_niche() -> str:
    """Active channel niche. Default app_safety (BlinkViral pivot)."""
    raw = (os.environ.get("CHANNEL_NICHE") or "").strip().lower()
    if raw in {"app_safety", "viral", "high_cpm"}:
        return raw
    # Legacy: CHANNEL_MODE=app_safety
    mode = (os.environ.get("CHANNEL_MODE") or "").strip().lower()
    if mode in {"app_safety", "appsafety", "app-safety"}:
        return "app_safety"
    return "app_safety"


def is_app_safety_mode(niche: str | None = None) -> bool:
    """True when this package/slot is the App TRUTH series.

    If an explicit niche is provided (including empty string from callers that
    pass kit.niche), honor it. Only fall back to CHANNEL_NICHE when niche is None.
    """
    if niche is not None:
        return str(niche).strip().lower() == "app_safety"
    return channel_niche() == "app_safety"


def is_app_safety_title(title: str) -> bool:
    t = str(title or "").lower()
    return "is it safe" in t and "truth" in t


def app_safety_title(app_name: str) -> str:
    name = " ".join(str(app_name or "").split()).strip() or "This App"
    return f"{name} - Is It Safe? The TRUTH"


def normalize_app_key(value: str) -> str:
    text = " ".join(str(value or "").split()).strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    text = re.sub(
        r"\b(is it safe|the truth|app|apk|safe|scam|verdict)\b",
        " ",
        text,
    )
    return " ".join(text.split())


def _load_state() -> dict[str, Any]:
    if not QUEUE_STATE_PATH.exists():
        return {"cursor": 0, "used_ids": []}
    try:
        data = json.loads(QUEUE_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                "cursor": int(data.get("cursor") or 0),
                "used_ids": [str(x) for x in (data.get("used_ids") or [])],
            }
    except Exception:
        pass
    return {"cursor": 0, "used_ids": []}


def _save_state(state: dict[str, Any]) -> None:
    QUEUE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def pick_next_app(*, avoid_ids: set[str] | None = None) -> dict[str, Any]:
    """Rotate through the ranked queue; skip recently used ids when possible."""
    state = _load_state()
    avoid = {*(avoid_ids or set()), *state.get("used_ids", [])[-8:]}
    n = len(APP_SAFETY_QUEUE)
    start = int(state.get("cursor") or 0) % n
    chosen = None
    for i in range(n):
        cand = APP_SAFETY_QUEUE[(start + i) % n]
        if cand["id"] not in avoid:
            chosen = cand
            state["cursor"] = (start + i + 1) % n
            break
    if chosen is None:
        chosen = APP_SAFETY_QUEUE[start]
        state["cursor"] = (start + 1) % n
    used = [str(x) for x in state.get("used_ids") or [] if str(x) != chosen["id"]]
    used.append(chosen["id"])
    state["used_ids"] = used[-40:]
    _save_state(state)
    return dict(chosen)


def lookup_app(name_or_id: str) -> dict[str, Any] | None:
    key = normalize_app_key(name_or_id)
    if not key:
        return None
    if key in _APP_BY_ID:
        return dict(_APP_BY_ID[key])
    if key in _APP_BY_NAME:
        return dict(_APP_BY_NAME[key])
    for app in APP_SAFETY_QUEUE:
        if normalize_app_key(app["name"]) == key or key in normalize_app_key(app["name"]):
            return dict(app)
    return None


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "app"


def build_app_safety_package(app: dict[str, Any]) -> dict[str, Any]:
    """Deterministic 15–20s verdict package matching the breakout format."""
    name = str(app.get("name") or "This App").strip()
    verdict = str(app.get("verdict") or "CAUTION").strip().upper()
    flags = [str(x) for x in (app.get("red_flags") or []) if str(x).strip()][:3]
    while len(flags) < 3:
        flags.append("Unknown APK source")

    hook_opts = [
        f"{name} safe hai?",
        f"Everyone downloading {name}",
        f"{name} — the real risk",
    ]
    question_opts = [
        "Kaunsa app check karun next?",
        "Comment the next app name",
        "Kis app ka TRUTH chahiye?",
    ]
    cta_opts = [
        "Comment the app — I check it.",
        "Follow for App TRUTH series.",
        "Next app name in comments.",
    ]

    palette_by_verdict = {
        "AVOID": [[220, 40, 40], [255, 200, 60], [255, 255, 255]],
        "HIGH RISK": [[255, 90, 20], [255, 210, 70], [255, 255, 255]],
        "CAUTION": [[255, 170, 20], [60, 160, 255], [255, 255, 255]],
    }
    palette = palette_by_verdict.get(verdict, palette_by_verdict["CAUTION"])

    return {
        "title": app_safety_title(name),
        "topic_id": f"app_safety_{app.get('id') or _slug(name)}",
        "trend_topic": name,
        "niche": "app_safety",
        "palette": palette,
        "hook": random.choice(hook_opts),
        "context_lines": [
            f"{name} is blowing up searches.",
            "People want a fast safety check.",
            "Not a full review — just the TRUTH.",
        ],
        "why_lines": [
            f"Verdict: {verdict}.",
            flags[0],
            flags[1],
        ],
        "question": random.choice(question_opts),
        "captions": [
            [f"{name.upper()} SAFE?", [255, 255, 255]],
            [f"VERDICT: {verdict}", [255, 220, 80]],
            [flags[0][:28], [255, 160, 120]],
            [flags[1][:28], [255, 255, 255]],
            [flags[2][:28], [255, 220, 80]],
            ["CHECK BEFORE INSTALL", [255, 160, 120]],
            ["APP TRUTH SERIES", [255, 255, 255]],
            ["COMMENT NEXT APP", [255, 220, 80]],
        ],
        "close_lines": [
            f"{name}: {verdict} — know before you install.",
            random.choice(cta_opts),
        ],
        "search_query": str(app.get("search_query") or f"{name} android phone colorful"),
        "youtube_tags": [
            name,
            f"{name} safe",
            f"{name} scam",
            f"is {name} safe",
            "app safety",
            "apk safe",
            "android apps",
            "shorts",
            "app truth",
            "mobile security",
        ],
        "verdict": verdict,
        "red_flags": flags,
        "app_id": app.get("id") or _slug(name),
    }


def lexicon_terms() -> list[str]:
    """Extra named-entity lexicon terms for the series."""
    terms: list[str] = []
    for app in APP_SAFETY_QUEUE:
        terms.append(str(app["name"]).lower())
        terms.append(str(app["id"]).replace("_", " ").lower())
    terms.extend(
        [
            "vidmate",
            "snaptube",
            "movie box",
            "cinema hd",
            "terabox",
            "flixvision",
            "flix hd",
            "showbox",
            "mobdro",
            "teatv",
            "tea tv",
            "beetv",
            "cyberflix",
            "apkpure",
            "aptoide",
            "happymod",
            "lucky patcher",
            "urban vpn",
            "1dm",
            "gb whatsapp",
            "fm whatsapp",
            "vlc",
        ]
    )
    return sorted(set(t for t in terms if t.strip()))

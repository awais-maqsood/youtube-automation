#!/usr/bin/env python3
"""
LLM Shorts — Video Generator
Every run: Groq invents a fresh topic + content + visual style choices.
5-act structure is fixed. Duration varies (~15–45s) from script length + jitter.
  Act 1 BOOT       — terminal boot / hook
  Act 2 DATA FLOOD — context lines
  Act 3 QUESTION   — why + question
  Act 4 CLIMAX     — rapid captions
  Act 5 EPILOGUE   — close + CTA
"""

import os, sys, math, random, wave, subprocess, json, argparse, colorsys, base64
import io
import re
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timezone, timedelta
from PIL import Image, ImageDraw, ImageFont
import numpy as np
from content_gen import generate_topic
from topic_validation import EntityValidationError, assert_publishable_metadata, log_entity_rejection
from similarity_guard import (
    SimilarityRejectError,
    assert_below_similarity_threshold,
    extract_cta_from_description,
    record_catalog_entry,
    score_candidate_against_catalog,
)

W, H = 1080, 1920
FPS = 30
RATE = 44100
FONT_M = "mono"
FONT_S = "serif-bold"

# Baseline act mix (frames @ 30fps ≈ 30.0s). Scaled per-run by plan_video_pacing().
_BASE_ACT_FRAMES = {
    "boot": 105,        # ~3.5s
    "data_flood": 150,  # ~5.0s
    "question": 180,    # ~6.0s
    "climax": 330,      # ~11.0s
    "epilogue": 135,    # ~4.5s
}
_MIN_ACT_FRAMES = {
    "boot": 45,
    "data_flood": 60,
    "question": 60,
    "climax": 120,
    "epilogue": 60,
}

# Keep tags minimal and relevant. Over-stuffing generic tags (#Trending #Viral
# #WhatHappened ...) reads as spam to YouTube and viewers. #Shorts + a couple of
# topical tags performs better. A topic-specific tag is appended at build time.
BASE_HASHTAGS = ["#Shorts", "#Trending", "#Viral", "#Explainer", "#News"]
# Hour (UTC) each slot targets. Minute is randomised at runtime so videos
# don't always surface at the same second — looks organic, not bot-scheduled.
SLOT_HOURS = {"morning": 12, "afternoon": 17, "evening": 22, "night": 3}

_DESC_CTA_POOL = [
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
    "Does this change your plan? Comment.",
    "Where do you land after this clip?",
    "Send the nuance the headline skipped.",
    "Is the panic overdone? Argue below.",
    "What should people watch next on this?",
    "Call the next move in one sentence.",
]
_DESC_FOLLOW_POOL = [
    "More named-topic Shorts coming — follow if useful.",
    "I post the next concrete update here.",
    "Follow for the next product/event breakdown.",
    "Subscribe if you want the next clear read.",
    "Next Short covers the follow-up — stay close.",
    "Hit follow for more searchable explainers.",
    "I break the next named story here first.",
    "Follow along for the next high-signal clip.",
]
# Distinct description architectures — not synonym swaps of one skeleton.
_DESC_ARCHITECTURES = (
    "hook_then_trend",
    "trend_then_hook",
    "question_led",
    "minimal_fact",
    "claim_then_ask",
)


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


ENV_FILE_VALUES = load_env_file(Path(__file__).resolve().parent.parent / ".env")

FONT_CANDIDATES = {
    "mono": [
        "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/cour.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/Library/Fonts/Courier New.ttf",
    ],
    "serif-bold": [
        "C:/Windows/Fonts/georgiab.ttf",
        "C:/Windows/Fonts/timesbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/Library/Fonts/Times New Roman Bold.ttf",
    ],
}


def env_value(name, default=""):
    return os.environ.get(name) or ENV_FILE_VALUES.get(name, default)

# ── Helpers ───────────────────────────────────────────────────────────────────


def clamp(v, lo=0, hi=255):
    return max(lo, min(hi, int(v)))


def lerp(a, b, t):
    return a + (b - a) * t


def _image_luminance(img) -> float:
    """Mean perceived luminance 0–255 for an RGB image (sampled for speed)."""
    rgb = img.convert("RGB")
    # Downsample so we don't scan 2M pixels per stock candidate.
    sample = rgb.resize((54, 96), Image.Resampling.BILINEAR)
    px = list(sample.getdata())
    if not px:
        return 0.0
    return sum(0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in px) / len(px)


def _stock_min_luminance() -> float:
    try:
        return float(env_value("STOCK_MIN_LUMINANCE", "45") or "45")
    except (TypeError, ValueError):
        return 45.0


def _is_usable_stock(img) -> bool:
    """Reject near-black / empty stock that becomes a 'black screen' Short."""
    try:
        if img.size[0] < 200 or img.size[1] < 200:
            return False
        lum = _image_luminance(img)
        if lum < _stock_min_luminance():
            return False
        # Also reject if >70% of sampled pixels are near-black.
        sample = img.convert("RGB").resize((54, 96), Image.Resampling.BILINEAR)
        dark = sum(1 for c in sample.getdata() if max(c) < 28) / max(1, sample.width * sample.height)
        return dark < 0.70
    except Exception:
        return False


def _resolve_font_source(font_key):
    candidates = FONT_CANDIDATES.get(font_key, [font_key])
    for candidate in candidates:
        try:
            if Path(candidate).exists():
                return candidate
        except Exception:
            pass
    return candidates[0]


def fnt(path, size):
    source = _resolve_font_source(path)
    try:
        return ImageFont.truetype(source, size)
    except Exception:
        # Last resort keeps render running, but should rarely trigger now.
        return ImageFont.load_default()


def fit_font(path, text, max_w, start_size, min_size=44):
    size = start_size
    source = _resolve_font_source(path)
    while size >= min_size:
        try:
            f = ImageFont.truetype(source, size)
        except Exception:
            return ImageFont.load_default(), min_size
        if f.getlength(text) <= max_w:
            return f, size
        size -= 4
    try:
        return ImageFont.truetype(source, min_size), min_size
    except Exception:
        return ImageFont.load_default(), min_size


def truncate_to_width(text, font, max_w):
    if font.getlength(text) <= max_w:
        return text
    suffix = " ..."
    out = text
    while out and font.getlength(out + suffix) > max_w:
        out = out[:-1]
    return (out.rstrip() + suffix) if out else text


def wrap_words_to_lines(text, font, max_w, max_lines):
    """Greedy word wrap to <= max_lines; last line may still exceed max_w (caller should shrink font)."""
    words = " ".join(str(text).split()).split(" ")
    lines = []
    cur = ""
    for w in words:
        if not w:
            continue
        trial = w if not cur else f"{cur} {w}"
        if font.getlength(trial) <= max_w:
            cur = trial
            continue
        if cur:
            lines.append(cur)
            cur = w
        else:
            lines.append(truncate_to_width(w, font, max_w))
            cur = ""
        if len(lines) >= max_lines:
            break
    if len(lines) < max_lines and cur:
        lines.append(cur)
    return lines[:max_lines]


def fit_font_wrapped(path, text, max_w, start_size, min_size=44, *, max_lines=3):
    """Pick largest font where wrapped lines all fit within max_w and line count <= max_lines."""
    size = start_size
    source = _resolve_font_source(path)
    while size >= min_size:
        try:
            f = ImageFont.truetype(source, size)
        except Exception:
            return ImageFont.load_default(), min_size, wrap_words_to_lines(text, ImageFont.load_default(), max_w, max_lines)
        lines = wrap_words_to_lines(text, f, max_w, max_lines)
        if not lines:
            return f, size, []
        if len(lines) > max_lines:
            size -= 4
            continue
        if any(f.getlength(line) > max_w for line in lines):
            size -= 4
            continue
        return f, size, lines
    try:
        f = ImageFont.truetype(source, min_size)
    except Exception:
        f = ImageFont.load_default()
    return f, min_size, wrap_words_to_lines(text, f, max_w, max_lines)


def _kit_one_line(s: str) -> str:
    return " ".join(str(s).split()).strip()


def _topic_hashtag(trend: str) -> str:
    """Build one camel-case topic hashtag from the trend phrase (e.g. #PixelCamera)."""
    words = re.findall(r"[A-Za-z0-9]+", trend or "")[:3]
    if not words:
        return ""
    return "#" + "".join(w.capitalize() for w in words)


def build_youtube_description(topic: dict) -> str:
    """Build a description with genuinely varied sentence architecture (not one scaffold)."""
    from content_gen import _stable_hash, recent_title_strings

    trend = _kit_one_line(topic.get("trend_topic") or topic.get("title") or "")
    hook = _kit_one_line(topic.get("hook") or "")
    q = _kit_one_line(topic.get("question") or "")
    niche = (topic.get("niche") or "viral").lower()
    key = f"{trend}|{hook}|{topic.get('topic_id') or 'x'}"

    # Avoid CTAs that match recent title scaffolds when possible.
    recent = {t.lower() for t in recent_title_strings(12)}
    cta_pool = [c for c in _DESC_CTA_POOL if c.lower() not in recent] or list(_DESC_CTA_POOL)
    cta = cta_pool[_stable_hash(f"cta:{key}") % len(cta_pool)]
    # Re-roll with wall-clock entropy so same-day reruns don't lock one CTA.
    if random.random() < 0.55:
        cta = random.choice(cta_pool)
    follow = random.choice(_DESC_FOLLOW_POOL)

    arch = _DESC_ARCHITECTURES[_stable_hash(f"arch:{key}:{random.randint(0, 10_000)}") % len(_DESC_ARCHITECTURES)]
    blocks: list[str] = []

    if arch == "hook_then_trend":
        if hook:
            blocks.append(hook)
        if trend:
            blocks.append(f"Focus: {trend}.")
        blocks.append("")
        blocks.append(cta)
        if q:
            blocks.append(q)
    elif arch == "trend_then_hook":
        if trend:
            blocks.append(trend)
        if hook:
            blocks.append(hook)
        blocks.append("")
        if q:
            blocks.append(q)
        blocks.append(cta)
    elif arch == "question_led":
        if q:
            blocks.append(q)
        elif trend:
            blocks.append(f"What's actually going on with {trend}?")
        if hook:
            blocks.append(hook)
        blocks.append("")
        blocks.append(cta)
    elif arch == "minimal_fact":
        if trend:
            blocks.append(f"{trend} — short context only.")
        blocks.append(cta)
        if follow:
            blocks.append(follow)
            follow = ""  # already placed
    else:  # claim_then_ask
        if hook:
            blocks.append(hook)
        elif trend:
            blocks.append(f"{trend} deserves a cleaner read.")
        blocks.append("")
        blocks.append(cta)
        if q:
            blocks.append(q)

    if follow:
        blocks.append("")
        blocks.append(follow)

    if niche == "high_cpm":
        tags = ["#Shorts", "#AITools", "#PersonalFinance", "#Investing", "#BusinessTips"]
        extra = ["#MoneyTips", "#SaaS", "#Productivity"]
    else:
        tags = ["#Shorts"]
        extra = ["#News", "#Explainer"]
    topic_tag = _topic_hashtag(trend)
    if topic_tag and topic_tag not in tags:
        tags.append(topic_tag)
    for t in extra:
        if t not in tags:
            tags.append(t)
    blocks.append(" ".join(tags))
    return "\n".join(b for b in blocks if b is not None)


def build_youtube_tags(topic: dict) -> list[str]:
    """YouTube search tags from LLM output or sensible defaults."""
    raw = topic.get("youtube_tags", [])
    if isinstance(raw, list) and len(raw) >= 4:
        return [str(t).strip() for t in raw if str(t).strip()][:12]
    from content_gen import _default_youtube_tags
    trend = topic.get("trend_topic") or topic.get("title") or ""
    niche = topic.get("niche") or "viral"
    return _default_youtube_tags(trend, niche=niche)


def _topic_style_index(topic: dict, n: int) -> int:
    seed = str(topic.get("topic_id") or topic.get("title") or topic.get("hook") or "x")
    return sum(ord(c) for c in seed) % max(1, n)


def write_promo_thumbnail(topic: dict, video_path: str, thumb_path: str) -> bool:
    """
    High-CTR Shorts thumbnail: bold hook/caption text, topic-tinted banner.
    Layout varies by topic so the channel grid does not look copy-pasted.
    """
    import tempfile

    hook_text = _kit_one_line(topic.get("hook") or topic.get("title") or "Trending Update")
    # Prefer first caption when present — Shorts feeds often surface climax text,
    # so aligning the custom thumb with a unique caption reduces "same-y" covers.
    caps = topic.get("captions") or []
    if isinstance(caps, list) and caps and isinstance(caps[0], list) and caps[0]:
        cap0 = _kit_one_line(str(caps[0][0]))
        if cap0 and len(cap0) >= 4:
            hook_text = cap0
    palette = topic.get("palette") or [[34, 139, 34], [255, 215, 0], [255, 255, 255]]
    p0 = palette[0]
    p1 = palette[1] if len(palette) > 1 else [255, 215, 0]
    accent = (clamp(p0[0]), clamp(p0[1]), clamp(p0[2]))
    accent2 = (clamp(p1[0]), clamp(p1[1]), clamp(p1[2]))
    style = _topic_style_index(topic, 4)
    # Prefer brighter seek points — style-only seeks often landed on near-black
    # stock (Studio then shows a black tile).
    seek_candidates = ["0.8", "2.5", "5.0", "8.0", "12.0", "1.2"]
    labels = [
        "TRENDING NOW",
        "VIRAL SHORTS",
        str(topic.get("trend_topic") or "LIVE TOPIC")[:22].upper(),
        "LIVE TAKE",
    ]
    if (topic.get("niche") or "") == "high_cpm":
        labels = [
            "MONEY TIP",
            "AI TOOLS",
            str(topic.get("trend_topic") or "FINANCE")[:22].upper(),
            "SMART TAKE",
        ]
    label = labels[style]
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    try:
        best_path = None
        best_lum = -1.0
        seek_ss = seek_candidates[0]
        for cand in seek_candidates:
            cand_path = tmp.name + f".{cand.replace('.', '_')}.jpg"
            r = subprocess.run(
                [
                    "ffmpeg", "-y", "-ss", cand,
                    "-i", video_path,
                    "-frames:v", "1", "-q:v", "2",
                    cand_path,
                ],
                capture_output=True, text=True, timeout=90,
            )
            if r.returncode != 0 or not Path(cand_path).exists():
                continue
            try:
                probe = Image.open(cand_path).convert("RGB")
                lum = _image_luminance(probe)
                if lum > best_lum:
                    best_lum = lum
                    best_path = cand_path
                    seek_ss = cand
            except Exception:
                continue
        if not best_path:
            print("  Thumbnail frame extract failed: no usable seek frames")
            return False
        # Promote the brightest frame to the working temp path.
        import shutil
        Path(tmp.name).unlink(missing_ok=True)
        shutil.move(best_path, tmp.name)
        for leftover in Path(tmp.name).parent.glob(Path(tmp.name).name + ".*.jpg"):
            try:
                leftover.unlink(missing_ok=True)
            except Exception:
                pass
        img = Image.open(tmp.name).convert("RGBA")
        if img.size != (W, H):
            img = img.resize((W, H), Image.Resampling.LANCZOS)

        banner_h = int(H * (0.38 + 0.04 * (style % 3)))
        # Full-canvas layer: alpha_composite requires both images to be the same size.
        banner = Image.new("RGBA", img.size, (0, 0, 0, 0))
        bd = ImageDraw.Draw(banner)
        # Topic palette drives the banner so thumbs aren't all the same green.
        br, bg_, bb = accent[0] // 4, accent[1] // 4, max(20, accent[2] // 5)
        for y in range(banner_h):
            alpha = int(220 - (y / banner_h) * 70)
            bd.line([(0, y), (W, y)], fill=(br, bg_, bb, alpha))
        img = Image.alpha_composite(img, banner)

        stripe = Image.new("RGBA", img.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(stripe)
        sd.rectangle([0, banner_h - 8, W, banner_h + 4], fill=(*accent2, 255))
        if style % 2 == 1:
            sd.rectangle([0, 0, 28, H], fill=(*accent, 230))
        img = Image.alpha_composite(img, stripe).convert("RGB")
        d = ImageDraw.Draw(img)

        label_font = fnt(FONT_M, 48)
        d.text((60 if style % 2 == 0 else 90, 72), label, font=label_font, fill=accent2)

        font_src = _resolve_font_source(FONT_S)
        f, _, lines = fit_font_wrapped(font_src, hook_text.upper(), W - 120, 118, min_size=64, max_lines=3)
        line_gap = int(max(12, f.size * 1.1))
        y0 = 180 + style * 36
        draw_outlined_lines(d, lines, y0, f, (255, 255, 255), line_gap=line_gap)

        img.save(thumb_path, "JPEG", quality=94, optimize=True)
        print(f"  Thumbnail -> {thumb_path} (style={style}, seek={seek_ss}s)")
        return True
    except Exception as e:
        print(f"  Thumbnail generation failed: {e}")
        return False
    finally:
        try:
            Path(tmp.name).unlink(missing_ok=True)
        except Exception:
            pass


def draw_outlined_lines(draw, lines, top_y, f, color, line_gap=None):
    """Draw a centered block of outlined lines; returns bottom y (exclusive)."""
    if line_gap is None:
        line_gap = int(max(8, getattr(f, "size", 48) * 1.05))
    y = top_y
    PAD = 60
    for line in lines:
        tw = f.getlength(line)
        x = max(PAD, (W - tw) / 2)
        stroke = max(4, int(getattr(f, "size", 48) * 0.08))
        for ox, oy in [
            (-stroke, 0),
            (stroke, 0),
            (0, -stroke),
            (0, stroke),
            (-stroke + 1, -stroke + 1),
            (stroke - 1, -stroke + 1),
            (-stroke + 1, stroke - 1),
            (stroke - 1, stroke - 1),
        ]:
            draw.text((x + ox, y + oy), line, font=f, fill=(0, 0, 0))
        draw.text((x, y), line, font=f, fill=color)
        y += line_gap
    return y


def draw_text_panel_block(lines, top_y, font, *, pad_x=28, pad_y=16, panel_alpha=165, line_gap=None):
    if line_gap is None:
        line_gap = int(max(8, getattr(font, "size", 48) * 1.05))
    max_tw = max((font.getlength(line) for line in lines), default=0.0)
    block_h = max(1, len(lines)) * line_gap + int(font.size * 0.25)
    x = max(60, (W - max_tw) / 2)
    panel = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    pd = ImageDraw.Draw(panel)
    pd.rounded_rectangle(
        [
            int(x - pad_x),
            int(top_y - pad_y),
            int(x + max_tw + pad_x),
            int(top_y + block_h + pad_y),
        ],
        radius=22,
        fill=(0, 0, 0, panel_alpha),
    )
    return panel


def hsv_s1_to_rgb_array(h_arr, v=0.5):
    """
    Vectorised HSV→RGB with S=1 fixed.
    h_arr: float32 ndarray in [0,1], any shape.
    Returns uint8 array of same shape + channel dim (shape + (3,)).
    """
    h6 = (h_arr * 6).astype(np.float32)
    hi = h6.astype(np.int32) % 6
    f  = h6 - np.floor(h6)
    q  = np.float32(v) * (1 - f)
    t  = np.float32(v) * f
    vv = np.float32(v)
    r = np.select([hi==0, hi==1, hi==2, hi==3, hi==4], [vv, q,  0,  0,  t ], default=vv)
    g = np.select([hi==0, hi==1, hi==2, hi==3, hi==4], [t,  vv, vv, q,  0 ], default=0)
    b = np.select([hi==0, hi==1, hi==2, hi==3, hi==4], [0,  0,  t,  vv, vv], default=q)
    return (np.stack([r, g, b], axis=-1) * 255).astype(np.uint8)


def glitch_rows(arr, count=12, shift=60):
    out = arr.copy()
    h = arr.shape[0]
    for _ in range(count):
        y = random.randint(0, h - 1)
        out[y] = np.roll(out[y], random.randint(-shift, shift), axis=0)
    return out


def chroma(img, s=6):
    img = img.convert("RGB")
    r, g, b = img.split()
    r = r.transform(r.size, Image.AFFINE, (1, 0, -s, 0, 1, 0))
    b = b.transform(b.size, Image.AFFINE, (1, 0, s, 0, 1, 0))
    return Image.merge("RGB", [r, g, b])


def scanlines(img, a=50):
    ol = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ol)
    for y in range(0, H, 5):
        d.line([(0, y), (W, y)], fill=(0, 0, 0, a))
    return Image.alpha_composite(img.convert("RGBA"), ol).convert("RGB")


def add_noise(img, s=7):
    arr = np.array(img).astype(np.int16)
    arr += np.random.randint(-s, s, arr.shape, dtype=np.int16)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def draw_outlined(draw, text, y, f, color):
    """Centered text with black outline. Caller must pass already-fitted font."""
    PAD = 60
    tw = f.getlength(text)
    x = max(PAD, (W - tw) / 2)
    stroke = max(4, int(getattr(f, "size", 48) * 0.08))
    for ox, oy in [
        (-stroke, 0),
        (stroke, 0),
        (0, -stroke),
        (0, stroke),
        (-stroke + 1, -stroke + 1),
        (stroke - 1, -stroke + 1),
        (-stroke + 1, stroke - 1),
        (stroke - 1, stroke - 1),
    ]:
        draw.text((x + ox, y + oy), text, font=f, fill=(0, 0, 0))
    draw.text((x, y), text, font=f, fill=color)


def draw_text_panel(draw, text, y, font, *, pad_x=28, pad_y=16, panel_alpha=165):
    tw = font.getlength(text)
    x = max(60, (W - tw) / 2)
    panel = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    pd = ImageDraw.Draw(panel)
    pd.rounded_rectangle(
        [
            int(x - pad_x),
            int(y - pad_y),
            int(x + tw + pad_x),
            int(y + font.size + pad_y),
        ],
        radius=22,
        fill=(0, 0, 0, panel_alpha),
    )
    return panel, x


def safe_text(draw, text, y, path, start_size, color):
    """Fit font to width then draw outlined. One call does everything."""
    f, _ = fit_font(path, text, W - 120, start_size)
    draw_outlined(draw, text, y, f, color)
    return f  # return for line measurements


def simple_gradient_bg(c1, c2):
    arr = np.zeros((H, W, 3), dtype=np.uint8)
    for y in range(H):
        t = y / max(1, H - 1)
        arr[y] = [
            clamp(lerp(c1[0], c2[0], t)),
            clamp(lerp(c1[1], c2[1], t)),
            clamp(lerp(c1[2], c2[2], t)),
        ]
    return Image.fromarray(arr)


def _cover_resize(img, target_w, target_h):
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    nw, nh = int(src_w * scale), int(src_h * scale)
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left = (nw - target_w) // 2
    top = (nh - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def _extract_freepik_image_url(resource):
    candidates = [
        resource.get("image", {}).get("source", {}).get("url"),
        resource.get("image", {}).get("url"),
        resource.get("preview", {}).get("url"),
        resource.get("thumbnail", {}).get("url"),
    ]
    for key in ("thumbnails", "previews"):
        for item in resource.get(key, []) or []:
            if isinstance(item, dict):
                candidates.append(item.get("url"))
    for c in candidates:
        if c and isinstance(c, str):
            return c
    return None


def fetch_freepik_stock_backgrounds(topic, target_count=5):
    api_key = env_value("FREEPIK_API_KEY", "").strip()
    if not api_key:
        return []

    query = topic.get("search_query") or topic.get("title") or topic.get("question") or topic.get("topic_id", "breaking news")
    search_url = "https://api.freepik.com/v1/resources?" + urllib.parse.urlencode(
        {"query": query, "type": "photo", "limit": max(5, target_count * 2)}
    )
    req = urllib.request.Request(
        search_url,
        headers={
            "x-freepik-api-key": api_key,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read())
    except Exception as e:
        print(f"  Freepik search failed: {e}")
        return []

    resources = payload.get("data") or payload.get("resources") or []
    if not resources:
        print("  Freepik returned no resources.")
        return []

    backgrounds = []
    seen_urls = set()
    for selected in resources:
        if len(backgrounds) >= target_count:
            break
        image_url = _extract_freepik_image_url(selected)

        if not image_url and selected.get("id"):
            rid = selected["id"]
            dl_req = urllib.request.Request(
                f"https://api.freepik.com/v1/resources/{rid}/download",
                headers={
                    "x-freepik-api-key": api_key,
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(dl_req, timeout=30) as r:
                    dl = json.loads(r.read())
                image_url = (
                    dl.get("data", {}).get("url")
                    or dl.get("url")
                    or dl.get("download_url")
                )
            except Exception:
                continue

        if not image_url:
            continue
        if image_url in seen_urls:
            continue
        seen_urls.add(image_url)

        try:
            with urllib.request.urlopen(image_url, timeout=30) as r:
                raw = r.read()
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            if not _is_usable_stock(img):
                continue
            backgrounds.append(_cover_resize(img, W, H))
        except Exception:
            continue

    return backgrounds


def fetch_pexels_stock_backgrounds(topic, target_count=5):
    """Free stock photos from Pexels (https://www.pexels.com/api/)."""
    api_key = env_value("PEXELS_API_KEY", "").strip()
    if not api_key:
        return []

    query = (
        topic.get("search_query")
        or topic.get("trend_topic")
        or topic.get("title")
        or topic.get("topic_id")
        or "trending news"
    )
    search_url = "https://api.pexels.com/v1/search?" + urllib.parse.urlencode(
        {
            "query": query,
            "orientation": "portrait",
            "per_page": max(5, min(40, target_count * 3)),
            "size": "large",
        }
    )
    req = urllib.request.Request(
        search_url,
        headers={"Authorization": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read())
    except Exception as e:
        print(f"  Pexels search failed: {e}")
        return []

    photos = payload.get("photos") or []
    if not photos:
        print("  Pexels returned no photos.")
        return []

    backgrounds = []
    seen_urls = set()
    for photo in photos:
        if len(backgrounds) >= target_count:
            break
        src = photo.get("src") or {}
        image_url = (
            src.get("portrait")
            or src.get("large2x")
            or src.get("large")
            or src.get("original")
            or src.get("medium")
        )
        if not image_url or image_url in seen_urls:
            continue
        seen_urls.add(image_url)
        try:
            img_req = urllib.request.Request(
                image_url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; llm-shorts/1.0)"},
            )
            with urllib.request.urlopen(img_req, timeout=30) as r:
                raw = r.read()
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            if not _is_usable_stock(img):
                continue
            backgrounds.append(_cover_resize(img, W, H))
        except Exception:
            continue
    return backgrounds


def fetch_pixabay_stock_backgrounds(topic, target_count=5):
    """Free stock photos from Pixabay (https://pixabay.com/api/docs/)."""
    api_key = env_value("PIXABAY_API_KEY", "").strip()
    if not api_key:
        return []

    query = (
        topic.get("search_query")
        or topic.get("trend_topic")
        or topic.get("title")
        or topic.get("topic_id")
        or "trending news"
    )
    # Prefer vertical, but fall back to all if the feed is thin for a query.
    # Second pass uses a brighter generic query so dark celebrity/stock shots
    # don't ship as black-screen Shorts.
    backgrounds = []
    queries = [query]
    soft = " ".join(str(query).split()[:3] + ["portrait", "daylight"]).strip()
    if soft.lower() != str(query).lower():
        queries.append(soft)
    queries.append("colorful city street daylight vertical")
    for q in queries:
        if len(backgrounds) >= target_count:
            break
        for orientation in ("vertical", "all"):
            if len(backgrounds) >= target_count:
                break
            search_url = "https://pixabay.com/api/?" + urllib.parse.urlencode(
                {
                    "key": api_key,
                    "q": q,
                    "image_type": "photo",
                    "orientation": orientation,
                    "safesearch": "true",
                    "per_page": max(5, min(40, target_count * 3)),
                }
            )
            req = urllib.request.Request(search_url, headers={"Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    payload = json.loads(r.read())
            except Exception as e:
                print(f"  Pixabay search failed ({orientation}): {e}")
                continue

            hits = payload.get("hits") or []
            if not hits:
                continue

            seen = set()
            for hit in hits:
                if len(backgrounds) >= target_count:
                    break
                image_url = (
                    hit.get("largeImageURL")
                    or hit.get("webformatURL")
                    or hit.get("previewURL")
                )
                if not image_url or image_url in seen:
                    continue
                seen.add(image_url)
                try:
                    img_req = urllib.request.Request(
                        image_url,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; llm-shorts/1.0)"},
                    )
                    with urllib.request.urlopen(img_req, timeout=30) as r:
                        raw = r.read()
                    img = Image.open(io.BytesIO(raw)).convert("RGB")
                    if not _is_usable_stock(img):
                        continue
                    backgrounds.append(_cover_resize(img, W, H))
                except Exception:
                    continue
            if backgrounds:
                break
    if not backgrounds:
        print("  Pixabay returned no usable photos.")
    return backgrounds


def generate_openai_stock_backgrounds(topic, target_count=5):
    api_key = env_value("OPENAI_API_KEY", "").strip()
    if not api_key:
        return []

    model = env_value("OPENAI_IMAGE_MODEL", "gpt-image-1").strip() or "gpt-image-1"
    size = env_value("OPENAI_IMAGE_SIZE", "1024x1536").strip() or "1024x1536"
    query = topic.get("search_query") or topic.get("title") or topic.get("topic_id", "trending topic")
    p0, p1, p2 = topic["palette"][0], topic["palette"][1], topic["palette"][2]

    prompt = (
        "Create cinematic background images for a vertical YouTube Short.\n"
        "Topic: " + str(query) + "\n"
        "Style: trend-focused real-world visual storytelling with cinematic lighting, "
        "clean composition, photorealistic look, shallow depth of field.\n"
        "No text, no logos, no watermarks.\n"
        "Use a color palette inspired by rgb("
        + f"{p0[0]},{p0[1]},{p0[2]}"
        + "), rgb("
        + f"{p1[0]},{p1[1]},{p1[2]}"
        + "), rgb("
        + f"{p2[0]},{p2[1]},{p2[2]}"
        + ").\n"
        "Generate " + str(target_count) + " distinct images with different camera angles/compositions.\n"
    )

    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "n": target_count,
            "size": size,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/images/generations",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = "<unreadable>"
        print(f"  OpenAI image generation failed HTTP {e.code}: {body[:1200]}")
        return []
    except Exception as e:
        print(f"  OpenAI image generation failed: {e}")
        return []

    images = []
    for item in (resp.get("data") or [])[:target_count]:
        b64 = item.get("b64_json")
        if not b64:
            continue
        try:
            raw = base64.b64decode(b64)
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            images.append(_cover_resize(img, W, H))
        except Exception:
            continue
    return images


def generate_gemini_stock_backgrounds(topic, target_count=5):
    api_key = env_value("GEMINI_API_KEY", "").strip()
    if not api_key:
        return []

    model = env_value("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image-preview").strip() or "gemini-3.1-flash-image-preview"
    size = env_value("GEMINI_IMAGE_SIZE", "1K").strip() or "1K"
    aspect_ratio = env_value("GEMINI_IMAGE_ASPECT_RATIO", "9:16").strip() or "9:16"

    query = topic.get("search_query") or topic.get("title") or topic.get("question") or topic.get("topic_id", "trending topic")
    p0, p1, p2 = topic["palette"][0], topic["palette"][1], topic["palette"][2]

    def one_prompt(variant_idx: int) -> str:
        return (
            "Create cinematic photorealistic background images for a vertical YouTube Short.\n"
            f"Topic: {query}\n"
            "Style: trend-focused real-world visual storytelling, cinematic lighting, "
            "photorealistic bokeh, clean composition, shallow depth of field.\n"
            "No text, no logos, no watermarks.\n"
            "Different camera angle and composition each variation.\n"
            f"Variation: {variant_idx}\n"
            "Use a color palette inspired by rgb("
            + f"{p0[0]},{p0[1]},{p0[2]}"
            + "), rgb("
            + f"{p1[0]},{p1[1]},{p1[2]}"
            + "), rgb("
            + f"{p2[0]},{p2[1]},{p2[2]}"
            + ")."
        )

    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={urllib.parse.quote(api_key)}"

    images = []
    for idx in range(target_count):
        prompt = one_prompt(idx + 1)
        payload = {
            "contents": [
                {
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "temperature": 0.7,
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {
                    "aspectRatio": aspect_ratio,
                    "imageSize": size,
                },
            },
        }
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = "<unreadable>"
            print(f"  Gemini image generation failed HTTP {e.code}: {body[:1000]}")
            continue
        except Exception as e:
            print(f"  Gemini image generation failed: {e}")
            continue

        # Expect inlineData base64 images under candidates[0].content.parts[*].inlineData
        try:
            parts = resp.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            for part in parts:
                inline = part.get("inlineData") or part.get("inline_data")
                if not inline:
                    continue
                b64 = inline.get("data")
                if not b64:
                    continue
                raw = base64.b64decode(b64)
                img = Image.open(io.BytesIO(raw)).convert("RGB")
                images.append(_cover_resize(img, W, H))
                break
        except Exception as e:
            print(f"  Gemini response parse failed: {e}")
            continue

    return images


# Stock backgrounds are set once per render by generate() so the act functions
# can draw text ON TOP of them (not blended over the finished frame, which was
# ghosting the text out).
_STOCK_BGS: list = []


def _stock_moving_view(bg, idx: int):
    zoomed = _cover_resize(bg, int(W * 1.08), int(H * 1.08))
    ox = int((zoomed.width - W) * (0.5 + 0.35 * math.sin(idx * 0.012)))
    oy = int((zoomed.height - H) * (0.5 + 0.35 * math.cos(idx * 0.010)))
    return zoomed.crop((ox, oy, ox + W, oy + H))


def act_base_image(i: int, n: int, c1, c2):
    """
    Build the background for a single frame: the subject image (darkened for text
    contrast) when stock art is available, otherwise the procedural gradient.

    Text/panels are drawn AFTER this by the act, so they stay fully opaque.
    """
    if not _STOCK_BGS:
        return simple_gradient_bg(c1, c2)

    bgs = _STOCK_BGS
    segment = max(1.0, n / len(bgs))
    slot = int(i / segment) % len(bgs)
    next_slot = (slot + 1) % len(bgs)
    in_segment = i - (slot * segment)
    progress = in_segment / segment
    fade_window = float(env_value("FREEPIK_CROSSFADE", "0.12") or "0.12")
    mix_next = 0.0
    if progress > 1.0 - fade_window:
        mix_next = (progress - (1.0 - fade_window)) / fade_window

    current_bg = _stock_moving_view(bgs[slot], i)
    if mix_next > 0:
        next_bg = _stock_moving_view(bgs[next_slot], i + 137)
        view = Image.blend(current_bg, next_bg, min(1.0, max(0.0, mix_next)))
    else:
        view = current_bg

    # Darken so white captions read clearly — keep light enough that Studio
    # auto-thumbs and mid-roll frames don't look like a black screen.
    scrim = float(env_value("STOCK_SCRIM", "0.25") or "0.25")
    scrim = min(0.55, max(0.0, scrim))
    if scrim > 0:
        black = Image.new("RGB", view.size, (0, 0, 0))
        view = Image.blend(view, black, scrim)
    return view


# ── Audio ─────────────────────────────────────────────────────────────────────


def _ffmpeg_available() -> bool:
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        return r.returncode == 0
    except Exception:
        return False


def _read_wav_f32_mono(path: str) -> np.ndarray:
    with wave.open(path, "r") as w:
        ch = w.getnchannels()
        sw = w.getsampwidth()
        fr = w.getframerate()
        nframes = w.getnframes()
        raw = w.readframes(nframes)
    if sw != 2:
        raise RuntimeError(f"Expected 16-bit WAV from ffmpeg, got sampwidth={sw}")
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch == 1:
        return pcm
    if ch == 2:
        pcm = pcm.reshape(-1, 2).mean(axis=1)
        return pcm
    raise RuntimeError(f"Unsupported channel count: {ch}")


def _linear_resample(sig: np.ndarray, out_len: int) -> np.ndarray:
    if out_len <= 1 or sig.size <= 1:
        return np.zeros(out_len, dtype=np.float32)
    x_old = np.linspace(0.0, 1.0, num=sig.size, dtype=np.float32)
    x_new = np.linspace(0.0, 1.0, num=out_len, dtype=np.float32)
    return np.interp(x_new, x_old, sig.astype(np.float32)).astype(np.float32)


def _openai_tts_to_wav(text: str, wav_out: str, niche: str = "viral") -> bool:
    key = (env_value("OPENAI_API_KEY", "") or "").strip()
    if not key or not _ffmpeg_available():
        return False
    model = (env_value("OPENAI_TTS_MODEL", "gpt-4o-mini-tts") or "gpt-4o-mini-tts").strip()
    voice = (env_value("OPENAI_TTS_VOICE", "alloy") or "alloy").strip()
    fmt = (env_value("OPENAI_TTS_FORMAT", "mp3") or "mp3").strip()
    speed = float(env_value("OPENAI_TTS_SPEED", "1.05") or "1.05")
    default_instr = (
        "Speak like a clear, trusted YouTube Shorts money and AI-tools explainer. "
        "Practical and confident, never hype salesy. Match any Hinglish wording naturally."
        if (niche or "").lower() == "high_cpm"
        else
        "Speak like an energetic YouTube Shorts explainer narrator. "
        "Passionate but clear. Match any Hinglish wording naturally."
    )
    instr = (env_value("OPENAI_TTS_INSTRUCTIONS", default_instr) or "").strip()
    url = "https://api.openai.com/v1/audio/speech"
    body = {
        "model": model,
        "voice": voice,
        "input": text,
        "format": fmt,
        "speed": speed,
    }
    if instr:
        body["instructions"] = instr
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    raw_audio = Path(wav_out).with_suffix(f".raw_{fmt}")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw_audio.write_bytes(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = "<unreadable>"
        if e.code == 401:
            print("  [VOICE] OpenAI TTS 401 — OPENAI_API_KEY is invalid/expired. "
                  "Replace it to enable narration.")
        else:
            print(f"  [VOICE] OpenAI TTS failed HTTP {e.code}: {body[:300]}")
        try:
            raw_audio.unlink(missing_ok=True)
        except Exception:
            pass
        return False
    except Exception as e:
        print(f"  [VOICE] OpenAI TTS failed: {e}")
        try:
            raw_audio.unlink(missing_ok=True)
        except Exception:
            pass
        return False

    r = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(raw_audio),
            "-ac",
            "1",
            "-ar",
            str(RATE),
            "-sample_fmt",
            "s16",
            wav_out,
        ],
        capture_output=True,
        text=True,
    )
    try:
        raw_audio.unlink(missing_ok=True)
    except Exception:
        pass
    if r.returncode != 0:
        print(f"  ffmpeg (tts decode) error: {r.stderr[-800:]}")
        return False
    return True


def _build_narration_script(topic: dict) -> str:
    """
    Build a clean SPOKEN script — full sentences only.

    Crucially this does NOT read the on-screen caption labels
    ("TREND ALERT", "HYPE YA REAL?", "QUICK BREAKDOWN", ...). Reading those
    aloud made the voiceover sound like spam. Captions are visual punctuation,
    not narration.
    """
    parts = []
    seen = set()

    def add(s: str):
        t = " ".join(str(s).split()).strip()
        if not t:
            return
        k = t.lower()
        if k in seen:
            return
        seen.add(k)
        parts.append(t)

    # Hook leads (it is the spoken opener); title is on-screen only so we don't
    # double it up in the audio.
    add(topic.get("hook"))
    for line in topic.get("context_lines", []) or []:
        add(line)
    for line in topic.get("why_lines", []) or []:
        add(line)
    add(topic.get("question"))
    for line in topic.get("close_lines", []) or []:
        add(line)

    script = " ".join(p.rstrip(".!?") + "." for p in parts)
    max_chars = int(env_value("OPENAI_TTS_MAX_CHARS", "900") or "900")
    if len(script) > max_chars:
        script = script[: max_chars - 1].rsplit(". ", 1)[0] + "."
    return script


def mix_voiceover(bed: np.ndarray, topic: dict, work_dir: str) -> np.ndarray:
    """When OPENAI_API_KEY is set, narrate the script and duck the procedural bed."""
    script = _build_narration_script(topic)
    if not script.strip():
        return bed
    if not (env_value("OPENAI_API_KEY", "") or "").strip():
        print("  [VOICE] No OPENAI_API_KEY set — video will have NO narration (ambience only).")
        return bed
    vo_path = os.path.join(work_dir, "voiceover.wav")
    niche = (topic.get("niche") or "viral")
    if not _openai_tts_to_wav(script, vo_path, niche=niche):
        print("  [VOICE] Narration unavailable — shipping with ambience only.")
        return bed
    try:
        vo = _read_wav_f32_mono(vo_path)
    except Exception as e:
        print(f"  Voiceover load failed: {e}")
        return bed

    # Fit the voice to the video length by padding/trimming — NOT resampling.
    # Resampling changed the pitch/speed (chipmunk or slow-mo voice), which read
    # as low-effort. Pad with silence if short; trim cleanly if long.
    target = bed.shape[0]
    if vo.shape[0] < target:
        vo = np.concatenate([vo, np.zeros(target - vo.shape[0], dtype=np.float32)])
    elif vo.shape[0] > target:
        vo = vo[:target]

    v_gain = float(env_value("OPENAI_TTS_MIX_VOICE", "0.78") or "0.78")
    b_gain = float(env_value("OPENAI_TTS_MIX_BED", "0.14") or "0.14")
    mixed = bed.astype(np.float32) * b_gain + vo.astype(np.float32) * v_gain
    print(f"  Narration: OpenAI TTS mixed under bed ({target / RATE:.1f}s, voice-forward)")
    return mixed


def write_wav(path, samples):
    data = (np.clip(np.tanh(samples * 1.1) * 0.7, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(data.tobytes())


def eerie_pad(dur, vol=0.15):
    t = np.linspace(0, dur, int(RATE * dur), False)
    s = (
        0.6 * np.sin(2 * np.pi * 55 * t)
        + 0.3 * np.sin(2 * np.pi * 82.4 * t + 0.3)
        + 0.1 * np.sin(2 * np.pi * 110 * t)
    )
    return s * (0.5 + 0.5 * np.sin(2 * np.pi * 0.35 * t)) * vol


def digital_blip(dur, vol=0.18):
    t = np.linspace(0, dur, int(RATE * dur), False)
    freqs = [220, 330, 440, 330, 550, 440, 660, 440]
    seg = int(RATE * dur / 8)
    out = np.zeros(int(RATE * dur))
    for i, f in enumerate(freqs):
        s = i * seg
        e = min(s + seg, len(out))
        chunk = vol * np.sin(2 * np.pi * f * t[s:e])
        env = np.ones(e - s)
        env[: min(80, e - s)] = np.linspace(0, 1, min(80, e - s))
        env[-min(80, e - s) :] = np.linspace(1, 0, min(80, e - s))
        out[s:e] = chunk * env
    return out


def data_cascade(dur, vol=0.10):
    t = np.linspace(0, dur, int(RATE * dur), False)
    sig = np.zeros_like(t)
    for _ in range(20):
        sig += random.uniform(0.03, 0.07) * np.sin(
            2 * np.pi * random.uniform(200, 2000) * t + random.uniform(0, 6.28)
        )
    return sig * vol


def chaos_audio(dur, vol=0.14):
    sig = data_cascade(dur, vol) + eerie_pad(dur, vol * 0.6)
    crush = np.round(sig * 8) / 8
    return np.clip(sig * 0.6 + crush * 0.4, -1, 1) * vol


# ── Rain character sets ───────────────────────────────────────────────────────

RAIN_CHARS = {
    "katakana":      [chr(c) for c in range(0x30A0, 0x30FF)],
    "binary":        list("01010110110100"),
    "hex":           list("0123456789ABCDEF"),
    "braille":       [chr(c) for c in range(0x2800, 0x2840)],
    "blocks":        list("█▓▒░▄▀■□▪▫"),
    "redacted":      ["[REDACTED]", "████", "█████", "▓▓▓▓", "░░░░", "■■■"],
    "corrupted_log": ["[ERROR]", "[warn]", "null", "???", "SIGKILL", "0x00", "FAULT", "lost", "--"],
}

FLOOD_COLORS = {
    "green": (0, 220, 60),
    "cyan": (0, 200, 255),
    "purple": (180, 0, 255),
    "amber": (255, 160, 0),
    "red": (255, 40, 40),
}

EPILOGUE_COLORS = {
    "white": (240, 240, 240),
    "green": (0, 255, 120),
    "cyan": (0, 220, 255),
    "amber": (255, 200, 60),
    "pink": (255, 150, 200),
}

CUT_SPEED = {"slow": 6, "medium": 4, "fast": 2}

# ── Duration / pacing ─────────────────────────────────────────────────────────


def _script_word_count(topic: dict) -> int:
    parts: list[str] = []
    for key in ("hook", "title", "question", "trend_topic"):
        parts.append(str(topic.get(key) or ""))
    for key in ("context_lines", "why_lines", "close_lines"):
        for line in topic.get(key) or []:
            parts.append(str(line))
    for cap in topic.get("captions") or []:
        if isinstance(cap, (list, tuple)) and cap:
            parts.append(str(cap[0]))
        elif isinstance(cap, str):
            parts.append(cap)
    return len(re.findall(r"[A-Za-z0-9']+", " ".join(parts)))


def _duration_bounds() -> tuple[float, float]:
    """Configurable Shorts duration window (seconds). Defaults 15–45."""
    try:
        min_s = float(env_value("SHORTS_DURATION_MIN", "15") or "15")
    except (TypeError, ValueError):
        min_s = 15.0
    try:
        max_s = float(env_value("SHORTS_DURATION_MAX", "45") or "45")
    except (TypeError, ValueError):
        max_s = 45.0
    min_s = max(12.0, min(min_s, 60.0))
    max_s = max(min_s, min(max_s, 60.0))
    return min_s, max_s


def plan_video_pacing(topic: dict) -> dict:
    """Choose a per-run duration in [min, max] and distribute frames across acts.

    Target length is driven primarily by script word count (more copy → longer),
    with controlled random jitter so uploads don't cluster on one fixed runtime.
    """
    min_s, max_s = _duration_bounds()
    words = _script_word_count(topic)
    # ~35 words → near min; ~110 words → near max (typical Shorts packages).
    span = max(1.0, max_s - min_s)
    t = (words - 35) / 75.0
    t = max(0.0, min(1.0, t))
    base_target = min_s + t * span
    jitter = random.uniform(-0.20, 0.20) * span
    target_s = max(min_s, min(max_s, base_target + jitter))

    total_frames = int(round(target_s * FPS))
    weight_sum = sum(_BASE_ACT_FRAMES.values())
    raw = {
        act: max(
            _MIN_ACT_FRAMES[act],
            int(round(total_frames * (_BASE_ACT_FRAMES[act] / weight_sum))),
        )
        for act in _BASE_ACT_FRAMES
    }
    # Reconcile rounding / minimum floors back to total_frames.
    diff = total_frames - sum(raw.values())
    # Prefer adjusting climax (largest act) so hook/epilogue stay readable.
    order = ["climax", "question", "data_flood", "epilogue", "boot"]
    idx = 0
    while diff != 0 and idx < 500:
        act = order[idx % len(order)]
        if diff > 0:
            raw[act] += 1
            diff -= 1
        elif raw[act] > _MIN_ACT_FRAMES[act]:
            raw[act] -= 1
            diff += 1
        idx += 1

    actual_s = sum(raw.values()) / float(FPS)
    pacing = {
        "target_seconds": round(target_s, 2),
        "actual_seconds": round(actual_s, 2),
        "word_count": words,
        "frames": raw,
        "fps": FPS,
    }
    topic["_pacing"] = pacing
    return pacing


def _act_frames(topic: dict, act: str) -> int:
    pacing = topic.get("_pacing") or {}
    frames = pacing.get("frames") or {}
    if act in frames:
        return int(frames[act])
    return int(_BASE_ACT_FRAMES[act])


# ── Acts ──────────────────────────────────────────────────────────────────────


def act_boot(topic):
    n = _act_frames(topic, "boot")
    hook = topic["hook"]
    title = topic["title"]
    trend = topic.get("trend_topic", title)
    color = tuple(topic["palette"][0])
    p1 = tuple(topic["palette"][1])
    frames = []
    trend_frames = max(12, n // 4)

    hook_font, _, hook_lines = fit_font_wrapped(
        FONT_S, hook, W - 160, 112, min_size=56, max_lines=3
    )
    hook_gap = int(max(10, hook_font.size * 1.08))
    hook_block_h = max(1, len(hook_lines)) * hook_gap + int(hook_font.size * 0.2)
    hook_center_y = 640
    hook_top = int(hook_center_y - hook_block_h / 2)

    title_font, _, title_lines = fit_font_wrapped(
        FONT_M, title, W - 200, 52, min_size=28, max_lines=2
    )
    title_gap = int(max(9, title_font.size * 1.12))
    title_block_h = max(1, len(title_lines)) * title_gap + int(title_font.size * 0.35)
    title_top = H - 120 - title_block_h

    for i in range(n):
        img = act_base_image(i, n, (12, 14, 18), (max(18, p1[0] // 5), max(18, p1[1] // 5), max(18, p1[2] // 5)))
        d = ImageDraw.Draw(img)
        if i < trend_frames:
            trend_font, _ = fit_font(FONT_M, trend.upper(), W - 160, 48, min_size=34)
            d.text((70, 80), trend.upper(), font=trend_font, fill=(220, 220, 220))
        panel_hook = draw_text_panel_block(
            hook_lines, hook_top, hook_font, pad_x=42, pad_y=26, panel_alpha=190, line_gap=hook_gap
        )
        img = Image.alpha_composite(img.convert("RGBA"), panel_hook).convert("RGB")
        d = ImageDraw.Draw(img)
        draw_outlined_lines(d, hook_lines, hook_top, hook_font, (255, 255, 255), line_gap=hook_gap)

        max_tw = max((title_font.getlength(line) for line in title_lines), default=0.0)
        pad_x, pad_y = 22, 14
        x0 = max(56, (W - max_tw) / 2 - pad_x)
        y0 = title_top - pad_y
        x1 = min(W - 56, (W + max_tw) / 2 + pad_x)
        y1 = title_top + title_block_h + pad_y
        d.rounded_rectangle([int(x0), int(y0), int(x1), int(y1)], radius=18, fill=(0, 0, 0))
        draw_outlined_lines(d, title_lines, title_top, title_font, (235, 235, 235), line_gap=title_gap)

        # No fade-in: the hook is on screen at full strength from frame 0 so the
        # first second delivers the payoff (critical for Shorts retention).
        frames.append(img)
    return frames, eerie_pad(n / FPS, vol=0.08)


def act_data_flood(topic):
    n = _act_frames(topic, "data_flood")
    lines = topic["context_lines"]
    frames = []
    palette = topic["palette"]
    p1 = tuple(palette[1])
    p0 = tuple(palette[0])
    labels = ["MATCH UPDATE", "QUICK CONTEXT", "WHAT'S GOING ON", "THE SETUP"]
    if (topic.get("niche") or "") == "high_cpm":
        labels = ["MONEY ANGLE", "TOOL BREAKDOWN", "COST CHECK", "QUICK CONTEXT"]
    chrome = labels[_topic_style_index(topic, len(labels))]
    reveal_every = max(12, n // 4)

    for i in range(n):
        img = act_base_image(i, n, (max(10, p0[0] // 7), max(10, p0[1] // 7), max(10, p0[2] // 7)), (max(10, p1[0] // 7), max(10, p1[1] // 7), max(10, p1[2] // 7)))
        d = ImageDraw.Draw(img)
        d.text((70, 120), chrome, font=fnt(FONT_M, 42), fill=(235, 235, 235))
        visible = min(3, i // reveal_every + 1)
        for idx, line in enumerate(lines[:visible]):
            f_line, _ = fit_font(FONT_S, line, W - 160, 94, min_size=54)
            y = 460 + idx * 230
            panel, _ = draw_text_panel(d, line, y, f_line, pad_x=34, pad_y=20, panel_alpha=170)
            img = Image.alpha_composite(img.convert("RGBA"), panel).convert("RGB")
            d = ImageDraw.Draw(img)
            draw_outlined(d, line, y, f_line, (255, 255, 255))
        frames.append(add_noise(img, 2))
    return frames, eerie_pad(n / FPS, vol=0.06)


def act_question(topic):
    n = _act_frames(topic, "question")
    frames = []
    q = topic["question"]
    why_lines = topic["why_lines"]
    color = tuple(topic["palette"][2])
    p0 = tuple(topic["palette"][0])
    p1 = tuple(topic["palette"][1])
    labels = ["TOURNAMENT STAKES", "WHY IT MATTERS", "THE PRESSURE", "FAN DEBATE"]
    if (topic.get("niche") or "") == "high_cpm":
        labels = ["WHY IT MATTERS", "COST IMPACT", "RISK CHECK", "SMART MOVE"]
    chrome = labels[_topic_style_index(topic, len(labels))]
    reveal_every = max(14, n // 4)

    for i in range(n):
        img = act_base_image(i, n, (max(12, p1[0] // 6), max(12, p1[1] // 6), max(12, p1[2] // 6)), (max(12, p0[0] // 6), max(12, p0[1] // 6), max(12, p0[2] // 6)))
        d = ImageDraw.Draw(img)
        d.text((70, 120), chrome, font=fnt(FONT_M, 42), fill=(235, 235, 235))
        visible = min(3, i // reveal_every + 1)
        for idx, line in enumerate(why_lines[:visible]):
            f_line, _ = fit_font(FONT_S, line, W - 180, 84, min_size=52)
            y = 400 + idx * 210
            panel, _ = draw_text_panel(d, line, y, f_line, pad_x=34, pad_y=18, panel_alpha=178)
            img = Image.alpha_composite(img.convert("RGBA"), panel).convert("RGB")
            d = ImageDraw.Draw(img)
            draw_outlined(d, line, y, f_line, (255, 255, 255))
        fq, _ = fit_font(FONT_S, q, W - 140, 110, min_size=64)
        panel_q, _ = draw_text_panel(d, q, 1370, fq, pad_x=40, pad_y=24, panel_alpha=185)
        img = Image.alpha_composite(img.convert("RGBA"), panel_q).convert("RGB")
        d = ImageDraw.Draw(img)
        draw_outlined(d, q, 1370, fq, color)
        frames.append(add_noise(img, 2))
    return frames, eerie_pad(n / FPS, vol=0.07)


def act_climax(topic):
    n = _act_frames(topic, "climax")
    frames = []
    captions = topic["captions"]
    palette = topic["palette"]
    p0 = tuple(palette[0]); p1 = tuple(palette[1]); p2 = tuple(palette[2])
    # YouTube Shorts often auto-picks a climax frame as the cover — vary layout
    # per topic so the channel grid doesn't look like one repeated meme slide.
    styles = [
        {"label": "FANS ARE WATCHING", "y": 760, "side": False, "counter_y": 1180},
        {"label": "LIVE REACTION", "y": 520, "side": True, "counter_y": 1280},
        {"label": "MATCH PULSE", "y": 900, "side": False, "counter_y": 1220},
        {"label": "WHAT JUST HAPPENED", "y": 640, "side": True, "counter_y": 1300},
        {"label": "FAN ZONE", "y": 480, "side": False, "counter_y": 1160},
        {"label": "LOCKER TALK", "y": 1000, "side": True, "counter_y": 1340},
    ]
    if (topic.get("niche") or "") == "high_cpm":
        styles = [
            {"label": "MONEY CHECK", "y": 760, "side": False, "counter_y": 1180},
            {"label": "TOOL TIP", "y": 520, "side": True, "counter_y": 1280},
            {"label": "COST ALERT", "y": 900, "side": False, "counter_y": 1220},
            {"label": "SMART MOVE", "y": 640, "side": True, "counter_y": 1300},
            {"label": "ROI PULSE", "y": 480, "side": False, "counter_y": 1160},
            {"label": "FINANCE TAKE", "y": 1000, "side": True, "counter_y": 1340},
        ]
    st = styles[_topic_style_index(topic, len(styles))]
    fade_frames = max(10, n // 16)

    for i in range(n):
        cap_idx = min(len(captions) - 1, int(i / max(1, n / max(1, len(captions)))))
        cap_entry = captions[cap_idx]
        cap_text = cap_entry[0]
        cap_color = (
            tuple(cap_entry[1]) if isinstance(cap_entry[1], list) else cap_entry[1]
        )
        img = act_base_image(i, n, (max(14, p2[0] // 6), max(14, p2[1] // 6), max(14, p2[2] // 6)), (max(14, p1[0] // 7), max(14, p1[1] // 7), max(14, p1[2] // 7)))
        d = ImageDraw.Draw(img)
        d.text((70, 120), st["label"], font=fnt(FONT_M, 42), fill=(235, 235, 235))
        if st["side"]:
            bar = Image.new("RGBA", img.size, (0, 0, 0, 0))
            bd = ImageDraw.Draw(bar)
            bd.rectangle([0, 0, 36, H], fill=(*p0, 220))
            img = Image.alpha_composite(img.convert("RGBA"), bar).convert("RGB")
            d = ImageDraw.Draw(img)
        f_cap, _ = fit_font(FONT_S, cap_text, W - 140, 142, min_size=74)
        y_cap = st["y"]
        panel_cap, _ = draw_text_panel(d, cap_text, y_cap, f_cap, pad_x=36, pad_y=20, panel_alpha=185)
        img = Image.alpha_composite(img.convert("RGBA"), panel_cap).convert("RGB")
        d = ImageDraw.Draw(img)
        draw_outlined(d, cap_text, y_cap, f_cap, cap_color)
        d.text((90, st["counter_y"]), f"{cap_idx + 1}/{len(captions)}", font=fnt(FONT_M, 40), fill=(235, 235, 235))
        img = add_noise(img, 2)
        if i > n - fade_frames:
            fade = (i - (n - fade_frames)) / float(fade_frames)
            img = Image.fromarray((np.array(img) * (1 - fade)).astype(np.uint8))
        frames.append(img)
    return frames, data_cascade(n / FPS, vol=0.06)


def act_epilogue(topic):
    n = _act_frames(topic, "epilogue")
    frames = []
    parts = topic["close_lines"]
    ecolor = tuple(topic["palette"][2])
    appear = [(j + 1) * n // (len(parts) + 2) for j in range(len(parts))]
    f_cur = fnt(FONT_M, 64)
    p0 = tuple(topic["palette"][0]); p1 = tuple(topic["palette"][1])
    fade_in = max(10, n // 10)
    blink_after = max(15, n // 5)

    for i in range(n):
        img = act_base_image(i, n, (max(12, p1[0] // 8), max(12, p1[1] // 8), max(12, p1[2] // 8)), (max(12, p0[0] // 8), max(12, p0[1] // 8), max(12, p0[2] // 8)))
        d = ImageDraw.Draw(img)
        cy = H // 2 - len(parts) * 75
        for j, part in enumerate(parts):
            if i >= appear[j]:
                fade = min(1.0, (i - appear[j]) / float(max(8, fade_in)))
                a = clamp(255 * fade)
                fe, _ = fit_font(FONT_S, part, W - 80, 92)
                pw = fe.getlength(part)
                col = (
                    clamp(ecolor[0] * a // 255),
                    clamp(ecolor[1] * a // 255),
                    clamp(ecolor[2] * a // 255),
                )
                y_line = cy + j * 150
                panel_alpha = clamp(int(155 * fade), 0, 155)
                panel_line, _ = draw_text_panel(d, part, y_line, fe, pad_x=28, pad_y=14, panel_alpha=panel_alpha)
                img = Image.alpha_composite(img.convert("RGBA"), panel_line).convert("RGB")
                d = ImageDraw.Draw(img)
                draw_outlined(d, part, y_line, fe, col)
        if i > appear[-1] + blink_after and (i // 10) % 2 == 0:
            d.text(
                (W // 2 - 20, cy + len(parts) * 150 + 30), "█", font=f_cur, fill=ecolor
            )
        if i >= appear[-1] + max(6, n // 12):
            cta_font, _ = fit_font(FONT_M, "COMMENT YOUR PICK", W - 120, 44, min_size=36)
            d.text((W // 2 - cta_font.getlength("COMMENT YOUR PICK") / 2, H - 200),
                   "COMMENT YOUR PICK", font=cta_font, fill=(255, 220, 60))
        if i < fade_in:
            img = Image.fromarray((np.array(img) * (i / float(fade_in))).astype(np.uint8))
        frames.append(add_noise(img, 2))
    return frames, eerie_pad(n / FPS, vol=0.05)


# ── Render ────────────────────────────────────────────────────────────────────


def generate(topic_id, slot, out_dir, *,
             remnant_state=None, run_type="NORMAL", epilogue_extra=None):
    os.makedirs(out_dir, exist_ok=True)
    frames_dir = os.path.join(out_dir, "_frames")
    os.makedirs(frames_dir, exist_ok=True)

    # LLM invents everything; epilogue_extra injected on REMNANT runs
    topic = generate_topic(epilogue_extra=epilogue_extra, slot=slot)

    # REMNANT layer — inject boot line, advance narrative state
    if run_type in ("REMNANT", "DORMANT") and remnant_state is not None:
        import remnant as rem
        if run_type == "REMNANT":
            rem.apply_remnant(remnant_state, topic)
        else:
            rem.apply_dormant(remnant_state, topic)

    print(f"Title: {topic['title']}")
    print(
        f"Trend: {topic.get('trend_topic', topic.get('topic_id', 'unknown'))} | "
        f"Niche: {topic.get('niche', 'viral')} | "
        f"Search: {topic.get('search_query', topic['title'])}"
    )

    pacing = plan_video_pacing(topic)
    print(
        f"  Pacing: target {pacing['target_seconds']:.1f}s -> "
        f"{pacing['actual_seconds']:.1f}s "
        f"(words={pacing['word_count']}, frames={pacing['frames']})"
    )

    provider = (env_value("STOCK_BACKGROUND_PROVIDER", "freepik") or "freepik").strip().lower()
    if provider == "gemini":
        image_count = int(env_value("GEMINI_IMAGE_COUNT", env_value("FREEPIK_IMAGE_COUNT", "5")) or "5")
    elif provider in ("pexels", "pixabay"):
        image_count = int(
            env_value("PEXELS_IMAGE_COUNT", env_value("FREEPIK_IMAGE_COUNT", "8")) or "8"
        )
    else:
        image_count = int(env_value("OPENAI_IMAGE_COUNT", env_value("FREEPIK_IMAGE_COUNT", "12")) or "12")
    image_count = max(3, min(25, image_count))

    stock_bgs = []
    if provider == "openai":
        stock_bgs = generate_openai_stock_backgrounds(topic, target_count=image_count)
        if stock_bgs:
            print(f"  OpenAI stock backgrounds: enabled ({len(stock_bgs)} images)")
        else:
            print("  OpenAI stock backgrounds: disabled (trying free photo sources)")
    elif provider == "gemini":
        stock_bgs = generate_gemini_stock_backgrounds(topic, target_count=image_count)
        if stock_bgs:
            print(f"  Gemini stock backgrounds: enabled ({len(stock_bgs)} images)")
        else:
            print("  Gemini stock backgrounds: disabled (trying free photo sources)")
    elif provider == "pexels":
        stock_bgs = fetch_pexels_stock_backgrounds(topic, target_count=image_count)
        if stock_bgs:
            print(f"  Pexels stock backgrounds: enabled ({len(stock_bgs)} images)")
        else:
            print("  Pexels stock backgrounds: disabled (trying other free sources)")
    elif provider == "pixabay":
        stock_bgs = fetch_pixabay_stock_backgrounds(topic, target_count=image_count)
        if stock_bgs:
            print(f"  Pixabay stock backgrounds: enabled ({len(stock_bgs)} images)")
        else:
            print("  Pixabay stock backgrounds: disabled (trying other free sources)")

    # Free / paid photo fallbacks so we never silently ship plain procedural BGs
    # when a free API key is available.
    if not stock_bgs:
        stock_bgs = fetch_freepik_stock_backgrounds(topic, target_count=image_count)
        if stock_bgs:
            print(f"  Freepik stock backgrounds: enabled ({len(stock_bgs)} images)")
    if not stock_bgs:
        stock_bgs = fetch_pexels_stock_backgrounds(topic, target_count=image_count)
        if stock_bgs:
            print(f"  Pexels (free) stock backgrounds: enabled ({len(stock_bgs)} images)")
    if not stock_bgs:
        stock_bgs = fetch_pixabay_stock_backgrounds(topic, target_count=image_count)
        if stock_bgs:
            print(f"  Pixabay (free) stock backgrounds: enabled ({len(stock_bgs)} images)")
    if not stock_bgs:
        print("  ⚠ No stock photos loaded — falling back to plain procedural gradient.")
        print("    Add PEXELS_API_KEY (free) or FREEPIK_API_KEY / GEMINI_API_KEY to fix this.")

    # Expose to the act functions so they draw text ON TOP of the subject image
    # (drawing over a finished frame ghosted the text out).
    global _STOCK_BGS
    _STOCK_BGS = stock_bgs

    acts = [
        ("boot", act_boot),
        ("data_flood", act_data_flood),
        ("question", act_question),
        ("climax", act_climax),
        ("epilogue", act_epilogue),
    ]

    all_frames = []
    all_audio = []
    for name, fn in acts:
        print(f"  {name}...", end=" ", flush=True)
        frames, audio = fn(topic)
        all_frames.extend(frames)
        all_audio.append(audio)
        print(f"{len(frames)}f ({len(frames)/FPS:.1f}s)")

    print(f"  Total: {len(all_frames)} frames = {len(all_frames)/FPS:.1f}s")
    for idx, frm in enumerate(all_frames):
        frm.convert("RGB").save(f"{frames_dir}/f{idx:05d}.jpg", "JPEG", quality=95)

    # Fail closed on near-black renders (YouTube Studio shows black tiles + stalled playback).
    sample_idxs = sorted({0, len(all_frames)//4, len(all_frames)//2, (3*len(all_frames))//4, len(all_frames)-1})
    sample_lums = [_image_luminance(all_frames[i]) for i in sample_idxs if 0 <= i < len(all_frames)]
    avg_lum = sum(sample_lums) / max(1, len(sample_lums))
    print(f"  Visual QA: mean luminance={avg_lum:.1f} (min usable ~40)")
    if avg_lum < 35:
        raise RuntimeError(
            f"Refusing to publish near-black video (mean luminance={avg_lum:.1f}). "
            "Stock photos were too dark after scrim — retry with brighter search terms."
        )

    wav = os.path.join(out_dir, "audio.wav")
    bed = np.concatenate(all_audio)
    bed = mix_voiceover(bed, topic, out_dir)
    write_wav(wav, bed)

    video = os.path.join(out_dir, "video.mp4")
    r = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-framerate",
            str(FPS),
            "-i",
            f"{frames_dir}/f%05d.jpg",
            "-i",
            wav,
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-level",
            "4.0",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "fast",
            "-crf",
            "22",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-movflags",
            "+faststart",
            "-shortest",
            "-vf",
            # Clean, crisp look: light saturation/contrast lift + gentle sharpen.
            # No glitch/noise/cross-process — that degradation was tanking retention.
            "eq=saturation=1.12:contrast=1.05,unsharp=5:5:0.6:5:5:0.0",
            video,
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        print("ffmpeg error:", r.stderr[-1500:])
        sys.exit(1)
    print(f"  Video -> {video}")

    import shutil

    shutil.rmtree(frames_dir)

    # Schedule for tomorrow so the timestamp is never in the past by the time the
    # user approves and publish.py runs (which can be 30-120 min after generation).
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    thumb_file = os.path.join(out_dir, "thumbnail.jpg")
    thumb_ok = write_promo_thumbnail(topic, video, thumb_file)

    # Description CTA similarity — rebuild architecture if CTA matches catalog too closely.
    description = build_youtube_description(topic)
    sim_scores = topic.get("_similarity") or {}
    cand_title = str(topic.get("title") or "")
    cand_opener = str(topic.get("hook") or "")
    cand_entity = str(topic.get("trend_topic") or "")
    # This candidate was already written to title history upstream, so it would
    # otherwise match itself at 1.000 and fail the gate.
    self_titles = {cand_title} if cand_title else None
    try:
        cta = extract_cta_from_description(description)
        sim_scores = score_candidate_against_catalog(
            title=cand_title,
            opener=cand_opener,
            cta=cta,
            entity=cand_entity,
            exclude_titles=self_titles,
        )
        if sim_scores.get("reject"):
            # Force a different description architecture / CTA pool pick.
            for _ in range(5):
                description = build_youtube_description(topic)
                cta = extract_cta_from_description(description)
                sim_scores = score_candidate_against_catalog(
                    title=cand_title,
                    opener=cand_opener,
                    cta=cta,
                    entity=cand_entity,
                    exclude_titles=self_titles,
                )
                if not sim_scores.get("reject"):
                    break
            assert_below_similarity_threshold(
                title=cand_title,
                opener=cand_opener,
                cta=extract_cta_from_description(description),
                entity=cand_entity,
                source="generate.kit_similarity",
                exclude_titles=self_titles,
            )
    except SimilarityRejectError as exc:
        log_entity_rejection(
            "generate.kit_similarity",
            topic.get("title"),
            {"scores": exc.scores, "description": description},
            reason="similarity_reject",
        )
        raise RuntimeError(f"Refusing to write kit — similarity gate failed: {exc}") from exc

    kit = {
        "title": topic["title"],
        "description": description,
        "tags": build_youtube_tags(topic),
        "topic": topic.get("topic_id", "generated"),
        "trend_topic": topic.get("trend_topic") or topic.get("title"),
        "hook": topic.get("hook") or "",
        "opener": topic.get("hook") or "",
        "niche": topic.get("niche", "viral"),
        "slot": slot,
        "duration_seconds": (topic.get("_pacing") or {}).get("actual_seconds"),
        "pacing": topic.get("_pacing"),
        "similarity": sim_scores,
        "scheduled_time_utc": f"{tomorrow}T{SLOT_HOURS.get(slot, SLOT_HOURS['morning']):02d}:{random.randint(0, 54):02d}:00Z",
        "video": video,
        "thumbnail": thumb_file if thumb_ok else "",
    }
    try:
        assert_publishable_metadata(kit["title"], kit["description"], source="generate.kit")
    except EntityValidationError as exc:
        log_entity_rejection(
            "generate.kit",
            kit["title"],
            {"description": kit["description"], "trend_topic": topic.get("trend_topic"), "error": str(exc)},
        )
        raise RuntimeError(f"Refusing to write publish kit with invalid metadata: {exc}") from exc

    record_catalog_entry(
        title=kit["title"],
        opener=str(topic.get("hook") or ""),
        cta=extract_cta_from_description(kit["description"]),
        trend=str(topic.get("trend_topic") or ""),
        description=kit["description"],
        source="generate.kit",
    )

    kit_path = os.path.join(out_dir, "kit.json")
    with open(kit_path, "w") as f:
        json.dump(kit, f, indent=2)
    return kit


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", default="morning", choices=["morning", "afternoon", "evening", "night"])
    ap.add_argument("--out", default="output")
    args = ap.parse_args()
    k = generate(None, args.slot, args.out)
    print(f"Done: {k['video']}")

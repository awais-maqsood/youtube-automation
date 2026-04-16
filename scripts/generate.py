#!/usr/bin/env python3
"""
LLM Shorts — Video Generator
Every run: Groq invents a fresh topic + content + visual style choices.
5-act structure is fixed. Everything inside is driven by Groq output.
  Act 1 BOOT       5s  — terminal boot
  Act 2 DATA FLOOD 5s  — chaotic token rain
  Act 3 QUESTION   6s  — core question + answers
  Act 4 CLIMAX     8s  — YTP meme captions
  Act 5 EPILOGUE   6s  — quiet personal close
"""

import os, sys, math, random, wave, subprocess, json, argparse, colorsys, base64
import io
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timezone, timedelta
from PIL import Image, ImageDraw, ImageFont
import numpy as np
from content_gen import generate_topic

W, H = 1080, 1920
FPS = 30
RATE = 44100
FONT_M = "mono"
FONT_S = "serif-bold"

HASHTAGS = "#Shorts #Trending #News #Explainer #Viral #Update #WhatHappened #WhyItsTrending"
# Hour (UTC) each slot targets. Minute is randomised at runtime so videos
# don't always surface at the same second — looks organic, not bot-scheduled.
SLOT_HOURS = {"morning": 12, "afternoon": 17, "evening": 22}


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
            backgrounds.append(_cover_resize(img, W, H))
        except Exception:
            continue

    return backgrounds


def generate_openai_stock_backgrounds(topic, target_count=5):
    api_key = env_value("OPENAI_API_KEY", "").strip()
    if not api_key:
        return []

    model = env_value("OPENAI_IMAGE_MODEL", "gpt-image-1").strip() or "gpt-image-1"
    size = env_value("OPENAI_IMAGE_SIZE", "1024x1536").strip() or "1024x1536"
    query = topic.get("search_query") or topic.get("title") or topic.get("topic_id", "tech short")
    p0, p1, p2 = topic["palette"][0], topic["palette"][1], topic["palette"][2]

    prompt = (
        "Create cinematic background images for a vertical YouTube Short.\n"
        "Topic: " + str(query) + "\n"
        "Style: modern tech / abstract / photorealistic bokeh, clean composition, shallow depth of field.\n"
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

    query = topic.get("search_query") or topic.get("title") or topic.get("question") or topic.get("topic_id", "tech short")
    p0, p1, p2 = topic["palette"][0], topic["palette"][1], topic["palette"][2]

    def one_prompt(variant_idx: int) -> str:
        return (
            "Create cinematic photorealistic background images for a vertical YouTube Short.\n"
            f"Topic: {query}\n"
            "Style: modern tech / abstract / photorealistic bokeh, clean composition, shallow depth of field.\n"
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


def blend_stock_frame(frame, stock_bgs, idx, total_frames):
    if not stock_bgs:
        return frame

    # Rotate across the full timeline with soft crossfades.
    segment = max(1.0, total_frames / len(stock_bgs))
    slot = int(idx / segment) % len(stock_bgs)
    next_slot = (slot + 1) % len(stock_bgs)
    in_segment = idx - (slot * segment)
    progress = in_segment / segment
    fade_window = float(env_value("FREEPIK_CROSSFADE", "0.12"))
    mix_next = 0.0
    if progress > 1.0 - fade_window:
        mix_next = (progress - (1.0 - fade_window)) / fade_window

    def moving_view(bg, seed_offset):
        zoomed = _cover_resize(bg, int(W * 1.08), int(H * 1.08))
        ox = int((zoomed.width - W) * (0.5 + 0.35 * math.sin((idx + seed_offset) * 0.012)))
        oy = int((zoomed.height - H) * (0.5 + 0.35 * math.cos((idx + seed_offset) * 0.010)))
        return zoomed.crop((ox, oy, ox + W, oy + H))

    current_bg = moving_view(stock_bgs[slot], 0)
    if mix_next > 0:
        next_bg = moving_view(stock_bgs[next_slot], 137)
        stock_view = Image.blend(current_bg, next_bg, min(1.0, max(0.0, mix_next)))
    else:
        stock_view = current_bg

    # Keep stock visible but avoid drowning out text/UI overlays.
    return Image.blend(frame.convert("RGB"), stock_view, 0.16)


# ── Audio ─────────────────────────────────────────────────────────────────────


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

# ── Acts ──────────────────────────────────────────────────────────────────────


def act_boot(topic):
    n = 150
    hook = topic["hook"]
    title = topic["title"]
    trend = topic.get("trend_topic", title)
    color = tuple(topic["palette"][0])
    p1 = tuple(topic["palette"][1])
    frames = []

    for i in range(n):
        img = simple_gradient_bg((12, 14, 18), (max(18, p1[0] // 5), max(18, p1[1] // 5), max(18, p1[2] // 5)))
        d = ImageDraw.Draw(img)
        if i < 30:
            trend_font, _ = fit_font(FONT_M, trend.upper(), W - 160, 48, min_size=34)
            d.text((70, 80), trend.upper(), font=trend_font, fill=(220, 220, 220))
        hook_font, _ = fit_font(FONT_S, hook, W - 140, 118, min_size=72)
        panel_hook, _ = draw_text_panel(d, hook, 620, hook_font, pad_x=42, pad_y=26, panel_alpha=190)
        img = Image.alpha_composite(img.convert("RGBA"), panel_hook).convert("RGB")
        d = ImageDraw.Draw(img)
        draw_outlined(d, hook, 620, hook_font, (255, 255, 255))

        title_font, _ = fit_font(FONT_M, title, W - 180, 54, min_size=34)
        title_text = truncate_to_width(title, title_font, W - 180)
        tw = title_font.getlength(title_text)
        d.rounded_rectangle([70, 960, 110 + tw, 1035], radius=18, fill=(0, 0, 0))
        d.text((90, 980), title_text, font=title_font, fill=(235, 235, 235))

        progress = min(1.0, i / 30.0)
        if progress < 1:
            img = Image.fromarray((np.array(img) * progress).astype(np.uint8))
        frames.append(add_noise(img, 2))
    return frames, eerie_pad(n / FPS, vol=0.08)


def act_data_flood(topic):
    n = 150
    lines = topic["context_lines"]
    frames = []
    palette = topic["palette"]
    p1 = tuple(palette[1])
    p0 = tuple(palette[0])

    for i in range(n):
        img = simple_gradient_bg((max(10, p0[0] // 7), max(10, p0[1] // 7), max(10, p0[2] // 7)), (max(10, p1[0] // 7), max(10, p1[1] // 7), max(10, p1[2] // 7)))
        d = ImageDraw.Draw(img)
        d.text((70, 120), "WHAT HAPPENED", font=fnt(FONT_M, 42), fill=(235, 235, 235))
        visible = min(3, i // 36 + 1)
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
    n = 180
    frames = []
    q = topic["question"]
    why_lines = topic["why_lines"]
    color = tuple(topic["palette"][2])
    p0 = tuple(topic["palette"][0])
    p1 = tuple(topic["palette"][1])

    for i in range(n):
        img = simple_gradient_bg((max(12, p1[0] // 6), max(12, p1[1] // 6), max(12, p1[2] // 6)), (max(12, p0[0] // 6), max(12, p0[1] // 6), max(12, p0[2] // 6)))
        d = ImageDraw.Draw(img)
        d.text((70, 120), "WHY PEOPLE CARE", font=fnt(FONT_M, 42), fill=(235, 235, 235))
        visible = min(3, i // 42 + 1)
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
    n = 240
    frames = []
    captions = topic["captions"]
    palette = topic["palette"]
    p0 = tuple(palette[0]); p1 = tuple(palette[1]); p2 = tuple(palette[2])

    for i in range(n):
        cap_idx = min(len(captions) - 1, int(i / max(1, n / len(captions))))
        cap_entry = captions[cap_idx]
        cap_text = cap_entry[0]
        cap_color = (
            tuple(cap_entry[1]) if isinstance(cap_entry[1], list) else cap_entry[1]
        )
        img = simple_gradient_bg((max(14, p2[0] // 6), max(14, p2[1] // 6), max(14, p2[2] // 6)), (max(14, p1[0] // 7), max(14, p1[1] // 7), max(14, p1[2] // 7)))
        d = ImageDraw.Draw(img)
        d.text((70, 120), "WHY IT'S TRENDING", font=fnt(FONT_M, 42), fill=(235, 235, 235))
        f_cap, _ = fit_font(FONT_S, cap_text, W - 140, 142, min_size=74)
        y_cap = 760
        panel_cap, _ = draw_text_panel(d, cap_text, y_cap, f_cap, pad_x=36, pad_y=20, panel_alpha=185)
        img = Image.alpha_composite(img.convert("RGBA"), panel_cap).convert("RGB")
        d = ImageDraw.Draw(img)
        draw_outlined(d, cap_text, y_cap, f_cap, cap_color)
        d.text((90, 1180), f"{cap_idx + 1}/{len(captions)}", font=fnt(FONT_M, 40), fill=(235, 235, 235))
        img = add_noise(img, 2)
        if i > n - 20:
            fade = (i - (n - 20)) / 20
            img = Image.fromarray((np.array(img) * (1 - fade)).astype(np.uint8))
        frames.append(img)
    return frames, data_cascade(n / FPS, vol=0.06)


def act_epilogue(topic):
    n = 180
    frames = []
    parts = topic["close_lines"]
    ecolor = tuple(topic["palette"][2])
    appear = [(j + 1) * n // (len(parts) + 2) for j in range(len(parts))]
    f_cur = fnt(FONT_M, 64)
    p0 = tuple(topic["palette"][0]); p1 = tuple(topic["palette"][1])

    for i in range(n):
        img = simple_gradient_bg((max(12, p1[0] // 8), max(12, p1[1] // 8), max(12, p1[2] // 8)), (max(12, p0[0] // 8), max(12, p0[1] // 8), max(12, p0[2] // 8)))
        d = ImageDraw.Draw(img)
        cy = H // 2 - len(parts) * 75
        for j, part in enumerate(parts):
            if i >= appear[j]:
                fade = min(1.0, (i - appear[j]) / 20.0)
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
        if i > appear[-1] + 30 and (i // 10) % 2 == 0:
            d.text(
                (W // 2 - 20, cy + len(parts) * 150 + 30), "█", font=f_cur, fill=ecolor
            )
        if i < 20:
            img = Image.fromarray((np.array(img) * (i / 20)).astype(np.uint8))
        frames.append(add_noise(img, 2))
    return frames, eerie_pad(n / FPS, vol=0.05)


# ── Render ────────────────────────────────────────────────────────────────────


def generate(topic_id, slot, out_dir, *,
             remnant_state=None, run_type="NORMAL", epilogue_extra=None):
    os.makedirs(out_dir, exist_ok=True)
    frames_dir = os.path.join(out_dir, "_frames")
    os.makedirs(frames_dir, exist_ok=True)

    # LLM invents everything; epilogue_extra injected on REMNANT runs
    topic = generate_topic(epilogue_extra=epilogue_extra)

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
        f"Search: {topic.get('search_query', topic['title'])}"
    )

    provider = (env_value("STOCK_BACKGROUND_PROVIDER", "freepik") or "freepik").strip().lower()
    if provider == "gemini":
        image_count = int(env_value("GEMINI_IMAGE_COUNT", env_value("FREEPIK_IMAGE_COUNT", "5")) or "5")
    else:
        image_count = int(env_value("OPENAI_IMAGE_COUNT", env_value("FREEPIK_IMAGE_COUNT", "12")) or "12")
    image_count = max(3, min(25, image_count))

    stock_bgs = []
    if provider == "openai":
        stock_bgs = generate_openai_stock_backgrounds(topic, target_count=image_count)
        if stock_bgs:
            print(f"  OpenAI stock backgrounds: enabled ({len(stock_bgs)} images)")
        else:
            print("  OpenAI stock backgrounds: disabled (fallback to Freepik)")
    elif provider == "gemini":
        stock_bgs = generate_gemini_stock_backgrounds(topic, target_count=image_count)
        if stock_bgs:
            print(f"  Gemini stock backgrounds: enabled ({len(stock_bgs)} images)")
        else:
            print("  Gemini stock backgrounds: disabled (fallback to Freepik)")

    if not stock_bgs:
        stock_bgs = fetch_freepik_stock_backgrounds(topic, target_count=image_count)
        if stock_bgs:
            print(f"  Freepik stock backgrounds: enabled ({len(stock_bgs)} images)")
        else:
            print("  Freepik stock backgrounds: disabled (fallback to procedural visuals)")

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
    if stock_bgs:
        total_frames = len(all_frames)
        all_frames = [blend_stock_frame(frm, stock_bgs, i, total_frames) for i, frm in enumerate(all_frames)]
    for idx, frm in enumerate(all_frames):
        frm.save(f"{frames_dir}/f{idx:05d}.jpg", "JPEG", quality=95)

    wav = os.path.join(out_dir, "audio.wav")
    write_wav(wav, np.concatenate(all_audio))

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
            "-preset",
            "fast",
            "-crf",
            "22",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-pix_fmt",
            "yuv420p",
            "-shortest",
            "-vf",
            "curves=preset=cross_process,noise=alls=2:allf=t+u,vignette=PI/6",
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
    kit = {
        "title": topic["title"],
        "description": f"{topic.get('trend_topic', topic['title'])}\n\n{HASHTAGS}",
        "topic": topic.get("topic_id", "generated"),
        "slot": slot,
        "scheduled_time_utc": f"{tomorrow}T{SLOT_HOURS.get(slot, SLOT_HOURS['morning']):02d}:{random.randint(0, 54):02d}:00Z",
        "video": video,
    }
    kit_path = os.path.join(out_dir, "kit.json")
    with open(kit_path, "w") as f:
        json.dump(kit, f, indent=2)
    return kit


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", default="morning", choices=["morning", "afternoon", "evening"])
    ap.add_argument("--out", default="output")
    args = ap.parse_args()
    k = generate(None, args.slot, args.out)
    print(f"Done: {k['video']}")

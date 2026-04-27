from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


def render_text_rgba(
    text: str,
    box_width: int,
    font_size: int,
    color: str,
    stroke_color: str,
    stroke_width: int = 2,
    align: str = "center",
    padding: int = 24,
) -> np.ndarray:
    font = _load_font(font_size)
    lines = _wrap_text(text=text, font=font, max_width=box_width - 2 * padding)
    if not lines:
        lines = [text]

    line_heights = []
    line_widths = []
    for line in lines:
        bbox = font.getbbox(line)
        line_widths.append(max(1, bbox[2] - bbox[0]))
        line_heights.append(max(1, bbox[3] - bbox[1]))

    spacing = max(10, font_size // 6)
    text_h = sum(line_heights) + spacing * (len(lines) - 1)
    img_h = text_h + 2 * padding
    img = Image.new("RGBA", (box_width, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    y = padding
    for idx, line in enumerate(lines):
        w = line_widths[idx]
        h = line_heights[idx]
        if align == "left":
            x = padding
        elif align == "right":
            x = box_width - padding - w
        else:
            x = (box_width - w) // 2
        draw.text(
            (x, y),
            line,
            font=font,
            fill=color,
            stroke_width=stroke_width,
            stroke_fill=stroke_color,
        )
        y += h + spacing

    return np.array(img)


def _wrap_text(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for w in words[1:]:
        candidate = f"{current} {w}"
        if _text_width(candidate, font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = w
    lines.append(current)
    return lines


def _text_width(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0]


def _load_font(font_size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "arialbd.ttf",
        "arial.ttf",
        "segoeuib.ttf",
        "segoeui.ttf",
    ]
    for name in candidates:
        try:
            return ImageFont.truetype(name, font_size)
        except OSError:
            continue
    return ImageFont.load_default()

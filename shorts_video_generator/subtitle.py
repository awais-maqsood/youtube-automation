from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from moviepy.editor import ImageClip

from shorts_video_generator.config import RenderConfig
from shorts_video_generator.text_render import render_text_rgba


@dataclass
class TimedWord:
    word: str
    start: float
    end: float


def word_timeline(script: str, duration: float) -> list[TimedWord]:
    words = [w.strip() for w in script.split() if w.strip()]
    if not words:
        return []
    slot = max(duration / len(words), 0.08)
    timed: list[TimedWord] = []
    cursor = 0.0
    for word in words:
        end = min(duration, cursor + slot)
        timed.append(TimedWord(word=word, start=cursor, end=end))
        cursor = end
    if timed:
        timed[-1].end = duration
    return timed


def create_word_by_word_clips(
    words: Iterable[TimedWord],
    config: RenderConfig,
    width: int,
    height: int,
) -> list[ImageClip]:
    clips: list[ImageClip] = []
    y_pos = height - config.subtitle_bottom_margin
    for token in words:
        text = token.word.upper()
        text_img = render_text_rgba(
            text=text,
            box_width=int(width * 0.92),
            font_size=config.subtitle_font_size,
            color=config.subtitle_color,
            stroke_color=config.subtitle_stroke_color,
            stroke_width=config.subtitle_stroke_width,
            align="center",
        )
        clip = (
            ImageClip(text_img, ismask=False)
            .set_start(token.start)
            .set_end(token.end)
            .set_position(("center", y_pos))
        )
        clips.append(clip)
    return clips

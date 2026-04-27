from __future__ import annotations

import random
from pathlib import Path

import numpy as np
from moviepy.audio.AudioClip import AudioClip, CompositeAudioClip
from moviepy.audio.fx.all import audio_fadeout, audio_loop, volumex
from moviepy.editor import (
    AudioFileClip,
    ColorClip,
    CompositeVideoClip,
    ImageClip,
    VideoClip,
    VideoFileClip,
    concatenate_videoclips,
    vfx,
)

from shorts_video_generator.config import RenderConfig
from shorts_video_generator.subtitle import create_word_by_word_clips, word_timeline
from shorts_video_generator.text_render import render_text_rgba


def build_shorts_video(
    topic: str,
    script: str,
    voiceover_path: Path,
    background_paths: list[Path],
    sfx_paths: list[Path],
    music_path: Path | None,
    config: RenderConfig,
) -> CompositeVideoClip:
    voiceover = AudioFileClip(str(voiceover_path))
    duration = max(voiceover.duration, 4.0)

    base_video = _build_background_timeline(
        total_duration=duration,
        background_paths=background_paths,
        config=config,
    )
    overlays = []

    hook_text = f"TECH TRUTH: {topic.upper()}"
    hook = _create_title_card(
        text=hook_text,
        duration=min(config.hook_duration, duration),
        config=config,
        mode="hook",
    )
    overlays.append(hook)

    cta_start = max(0.0, duration - config.cta_duration)
    subtitle_duration = max(0.0, cta_start)
    timed_words = word_timeline(script=script, duration=subtitle_duration)
    overlays.extend(
        create_word_by_word_clips(
            words=timed_words,
            config=config,
            width=config.width,
            height=config.height,
        )
    )

    cta = _create_title_card(
        text="FOLLOW FOR MORE TECH TRUTH",
        duration=duration - cta_start,
        config=config,
        mode="cta",
    ).set_start(cta_start)
    overlays.append(cta)

    progress = _create_progress_bar(duration=duration, config=config)
    overlays.append(progress)

    sound_design = _build_sound_design(
        duration=duration,
        voiceover=voiceover,
        sfx_paths=sfx_paths,
        music_path=music_path,
        interval=config.transition_interval,
    )

    return CompositeVideoClip([base_video, *overlays], size=(config.width, config.height)).set_audio(sound_design)


def _build_background_timeline(
    total_duration: float,
    background_paths: list[Path],
    config: RenderConfig,
) -> VideoClip:
    segments: list[VideoClip] = []
    t = 0.0
    idx = 0
    while t < total_duration:
        chunk_duration = min(config.transition_interval, total_duration - t)
        segment = _make_segment(
            path=background_paths[idx % len(background_paths)] if background_paths else None,
            duration=chunk_duration,
            index=idx,
            config=config,
        )
        segments.append(segment)
        t += chunk_duration
        idx += 1
    return concatenate_videoclips(segments, method="compose")


def _make_segment(path: Path | None, duration: float, index: int, config: RenderConfig) -> VideoClip:
    if path and path.exists():
        clip = VideoFileClip(str(path), audio=False)
        if clip.duration > duration:
            start = random.uniform(0, max(clip.duration - duration, 0))
            clip = clip.subclip(start, start + duration)
        else:
            clip = clip.loop(duration=duration).subclip(0, duration)
        clip = clip.resize(height=config.height).crop(x_center=clip.w / 2, width=config.width)
    else:
        tint = (
            min(255, config.bg_color[0] + random.randint(0, 20)),
            min(255, config.bg_color[1] + random.randint(0, 30)),
            min(255, config.bg_color[2] + random.randint(20, 55)),
        )
        clip = ColorClip(size=(config.width, config.height), color=tint, duration=duration)

    scale_start = 1.0 + (0.03 if index % 2 == 0 else 0.06)
    scale_end = scale_start + 0.07
    clip = clip.fx(vfx.resize, lambda t: scale_start + (scale_end - scale_start) * (t / max(duration, 0.001)))
    clip = clip.set_duration(duration).set_fps(config.fps)
    return clip


def _create_title_card(text: str, duration: float, config: RenderConfig, mode: str) -> VideoClip:
    font_size = config.hook_font_size if mode == "hook" else config.cta_font_size
    y_pos = 180 if mode == "hook" else config.height - 430
    text_img = render_text_rgba(
        text=text,
        box_width=int(config.width * 0.9),
        font_size=font_size,
        color="#FFFFFF",
        stroke_color="#00F5FF",
        stroke_width=2,
        align="center",
    )
    return (
        ImageClip(text_img, ismask=False)
        .set_duration(duration)
        .set_position(("center", y_pos))
        .crossfadein(0.2)
        .crossfadeout(0.2)
    )


def _create_progress_bar(duration: float, config: RenderConfig) -> VideoClip:
    w, h = config.width, config.height
    bar_h = config.progress_bar_height
    y0 = h - bar_h - 12
    color = np.array(config.progress_bar_color, dtype=np.uint8)

    def make_color_frame(t: float) -> np.ndarray:
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        progress_w = int(min(1.0, t / max(duration, 0.001)) * w)
        if progress_w > 0:
            frame[y0 : y0 + bar_h, :progress_w, :3] = color
        return frame

    def make_mask_frame(t: float) -> np.ndarray:
        mask = np.zeros((h, w), dtype=float)
        progress_w = int(min(1.0, t / max(duration, 0.001)) * w)
        if progress_w > 0:
            mask[y0 : y0 + bar_h, :progress_w] = 0.85
        return mask

    bar_clip = VideoClip(make_frame=make_color_frame, ismask=False, duration=duration)
    mask_clip = VideoClip(make_frame=make_mask_frame, ismask=True, duration=duration)
    return bar_clip.set_mask(mask_clip).set_position((0, 0))


def _build_sound_design(
    duration: float,
    voiceover: AudioFileClip,
    sfx_paths: list[Path],
    music_path: Path | None,
    interval: float,
) -> CompositeAudioClip:
    layers = [voiceover.fx(volumex, 1.0)]

    for transition_t in np.arange(interval, duration, interval):
        if sfx_paths:
            sfx_clip = AudioFileClip(str(random.choice(sfx_paths))).fx(volumex, 0.24)
            if sfx_clip.duration > 0.45:
                sfx_clip = sfx_clip.subclip(0, 0.45)
        else:
            sfx_clip = _synthetic_whoosh(duration=0.35).fx(volumex, 0.20)
        layers.append(sfx_clip.set_start(float(transition_t)))

    ambient = _synthetic_ambient(duration=duration).fx(volumex, 0.05)
    layers.append(ambient)

    if music_path and music_path.exists():
        music = AudioFileClip(str(music_path))
        if music.duration < duration:
            music = music.fx(audio_loop, duration=duration)
        music = music.subclip(0, duration).fx(volumex, 0.12)
        layers.append(music)

    composite = CompositeAudioClip(layers).set_duration(duration)
    return composite.fx(audio_fadeout, 0.25)


def _synthetic_whoosh(duration: float) -> AudioClip:
    def make_whoosh(t):
        tt = np.asarray(t, dtype=float)
        safe_duration = max(duration, 0.001)
        freq = 240 + (680 * (tt / safe_duration))
        envelope = np.clip(1 - (tt / safe_duration), 0.0, 1.0)
        wave = envelope * np.sin(2 * np.pi * freq * tt)
        if np.isscalar(t):
            value = float(wave)
            return np.array([value, value], dtype=float)
        return np.column_stack((wave, wave))

    return AudioClip(make_whoosh, duration=duration, fps=44100)


def _synthetic_ambient(duration: float) -> AudioClip:
    def make_ambient(t):
        tt = np.asarray(t, dtype=float)
        low = np.sin(2 * np.pi * 44 * tt)
        high = np.sin(2 * np.pi * 88 * tt + 0.35)
        pulse = 0.6 + 0.4 * np.sin(2 * np.pi * 0.22 * tt)
        wave = 0.15 * pulse * (0.65 * low + 0.35 * high)
        if np.isscalar(t):
            value = float(wave)
            return np.array([value, value], dtype=float)
        return np.column_stack((wave, wave))

    return AudioClip(make_ambient, duration=duration, fps=44100)

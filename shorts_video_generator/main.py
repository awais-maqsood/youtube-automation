from __future__ import annotations

import argparse
import json
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from moviepy.editor import VideoFileClip
from dotenv import load_dotenv

from shorts_video_generator.config import (
    RenderConfig,
    backgrounds_dir,
    music_dir,
    output_dir,
    sfx_dir,
    temp_dir,
    STYLE_PRESETS,
)
from shorts_video_generator.stock import download_stock_backgrounds
from shorts_video_generator.template import build_shorts_video
from shorts_video_generator.voice import generate_voiceover


def run() -> None:
    # Automatically load local secrets from .env (if present).
    load_dotenv()
    args = _parse_args()
    topic, script = _resolve_inputs(args)
    config = RenderConfig()
    config = _apply_style(config, args.style)
    if args.music_bpm and args.music_bpm > 0:
        beat_interval = max(0.4, 60.0 / args.music_bpm)
        config = replace(config, transition_interval=beat_interval)

    output_dir().mkdir(parents=True, exist_ok=True)
    temp_dir().mkdir(parents=True, exist_ok=True)
    music_dir().mkdir(parents=True, exist_ok=True)

    voice_path = temp_dir() / "voiceover.mp3"
    generate_voiceover(
        script=script,
        output_path=voice_path,
        provider=args.voice_provider,
        elevenlabs_voice_id=args.elevenlabs_voice_id,
    )

    if args.auto_stock:
        stock_query = args.stock_query.strip() or _default_stock_query(topic)
        download_stock_backgrounds(
            query=stock_query,
            out_dir=backgrounds_dir(),
            max_downloads=args.stock_count,
        )

    bg_paths = _collect_media(backgrounds_dir(), (".mp4", ".mov", ".mkv", ".webm"))
    fx_paths = _collect_media(sfx_dir(), (".mp3", ".wav", ".ogg", ".m4a"))
    default_music = _collect_media(music_dir(), (".mp3", ".wav", ".ogg", ".m4a"))
    music_path = Path(args.music_track) if args.music_track else (default_music[0] if default_music else None)

    final_video = build_shorts_video(
        topic=topic,
        script=script,
        voiceover_path=voice_path,
        background_paths=bg_paths,
        sfx_paths=fx_paths,
        music_path=music_path,
        config=config,
    )

    file_stem = _slugify(topic)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = output_dir() / f"{file_stem}-{timestamp}.mp4"

    final_video.write_videofile(
        str(out_path),
        fps=config.fps,
        codec="libx264",
        audio_codec="aac",
        preset="medium",
        bitrate=config.bitrate,
        threads=4,
        ffmpeg_params=["-movflags", "+faststart", "-pix_fmt", "yuv420p"],
    )
    final_video.close()

    # Lightweight post-check: open and close to verify generated mp4 integrity.
    validation_clip = VideoFileClip(str(out_path))
    validation_clip.close()

    print(f"Generated Shorts video: {out_path}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate cinematic faceless YouTube Shorts for FIFA World Cup football topics."
    )
    parser.add_argument("--topic", type=str, help="Video topic/title.")
    parser.add_argument("--script", type=str, help="Narration script text.")
    parser.add_argument(
        "--input-json",
        type=Path,
        default=Path("sample_script.json"),
        help="Optional JSON file with keys: topic, script",
    )
    parser.add_argument(
        "--voice-provider",
        type=str,
        default="gtts",
        choices=["gtts", "elevenlabs"],
        help="Voice generation provider.",
    )
    parser.add_argument(
        "--elevenlabs-voice-id",
        type=str,
        default="EXAVITQu4vr4xnSDxMaL",
        help="ElevenLabs voice id if provider is elevenlabs.",
    )
    parser.add_argument(
        "--style",
        type=str,
        default="dark_cyber",
        choices=sorted(STYLE_PRESETS.keys()),
        help="Visual style preset.",
    )
    parser.add_argument(
        "--auto-stock",
        action="store_true",
        help="Auto-download stock clips (requires PEXELS_API_KEY).",
    )
    parser.add_argument(
        "--stock-query",
        type=str,
        default="",
        help="Search query for stock auto-download.",
    )
    parser.add_argument(
        "--stock-count",
        type=int,
        default=5,
        help="How many stock clips to download.",
    )
    parser.add_argument(
        "--music-track",
        type=str,
        default="",
        help="Optional music bed path.",
    )
    parser.add_argument(
        "--music-bpm",
        type=float,
        default=0.0,
        help="Sync cut interval to beat using BPM (60/BPM seconds).",
    )
    return parser.parse_args()


def _resolve_inputs(args: argparse.Namespace) -> tuple[str, str]:
    topic = (args.topic or "").strip()
    script = (args.script or "").strip()
    if topic and script:
        return topic, script

    if args.input_json.exists():
        payload = json.loads(args.input_json.read_text(encoding="utf-8"))
        topic = topic or str(payload.get("topic", "")).strip()
        script = script or str(payload.get("script", "")).strip()

    if not topic or not script:
        raise ValueError(
            "Provide both --topic and --script, or set them in sample_script.json."
        )
    return topic, script


def _collect_media(path: Path, allowed_ext: tuple[str, ...]) -> list[Path]:
    if not path.exists():
        return []
    return [p for p in sorted(path.iterdir()) if p.suffix.lower() in allowed_ext and p.is_file()]


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return cleaned or "shorts-video"


def _apply_style(config: RenderConfig, style: str) -> RenderConfig:
    preset = STYLE_PRESETS.get(style, {})
    return replace(config, **preset) if preset else config


def _default_stock_query(topic: str) -> str:
    return f"football soccer world cup stadium fans fifa {topic}".strip()

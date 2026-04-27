from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RenderConfig:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    transition_interval: float = 2.0
    hook_duration: float = 2.0
    cta_duration: float = 2.0
    subtitle_font: str = "Arial-Bold"
    subtitle_font_size: int = 74
    subtitle_color: str = "#00F5FF"
    subtitle_stroke_color: str = "#0A0A0A"
    subtitle_stroke_width: int = 3
    subtitle_bottom_margin: int = 300
    hook_font_size: int = 86
    cta_font_size: int = 78
    progress_bar_height: int = 14
    progress_bar_color: tuple[int, int, int] = (0, 245, 255)
    bg_color: tuple[int, int, int] = (8, 10, 18)
    bitrate: str = "6500k"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def assets_dir() -> Path:
    return project_root() / "assets"


def backgrounds_dir() -> Path:
    return assets_dir() / "backgrounds"


def sfx_dir() -> Path:
    return assets_dir() / "sfx"


def music_dir() -> Path:
    return assets_dir() / "music"


def output_dir() -> Path:
    return project_root() / "output"


def temp_dir() -> Path:
    return project_root() / "temp"


STYLE_PRESETS: dict[str, dict[str, object]] = {
    "dark_cyber": {
        "subtitle_color": "#00F5FF",
        "progress_bar_color": (0, 245, 255),
        "bg_color": (8, 10, 18),
        "transition_interval": 2.0,
    },
    "minimal_clean": {
        "subtitle_color": "#EAF1FF",
        "progress_bar_color": (122, 196, 255),
        "bg_color": (14, 16, 24),
        "transition_interval": 2.2,
    },
    "glitch_heavy": {
        "subtitle_color": "#00FFD1",
        "progress_bar_color": (0, 255, 209),
        "bg_color": (5, 6, 14),
        "transition_interval": 1.6,
    },
}

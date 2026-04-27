from __future__ import annotations

import os
from pathlib import Path

import requests


def download_stock_backgrounds(
    query: str,
    out_dir: Path,
    per_page: int = 8,
    max_downloads: int = 5,
) -> list[Path]:
    """
    Download portrait stock clips.
    Provider order:
    1) Pexels (PEXELS_API_KEY)
    2) Pixabay (PIXABAY_API_KEY)
    """
    pexels_key = os.getenv("PEXELS_API_KEY")
    if pexels_key:
        downloaded = _download_from_pexels(
            api_key=pexels_key,
            query=query,
            out_dir=out_dir,
            per_page=per_page,
            max_downloads=max_downloads,
        )
        if downloaded:
            return downloaded

    pixabay_key = os.getenv("PIXABAY_API_KEY")
    if pixabay_key:
        return _download_from_pixabay(
            api_key=pixabay_key,
            query=query,
            out_dir=out_dir,
            per_page=per_page,
            max_downloads=max_downloads,
        )

    return []


def _download_from_pexels(
    api_key: str,
    query: str,
    out_dir: Path,
    per_page: int,
    max_downloads: int,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": api_key}
    url = (
        "https://api.pexels.com/videos/search"
        f"?query={query}&orientation=portrait&size=medium&per_page={per_page}"
    )
    response = requests.get(url, headers=headers, timeout=60)
    response.raise_for_status()
    payload = response.json()

    downloaded: list[Path] = []
    for item in payload.get("videos", []):
        files = item.get("video_files", [])
        if not files:
            continue
        sorted_files = sorted(files, key=lambda f: f.get("width", 0) * f.get("height", 0), reverse=True)
        file_url = None
        for candidate in sorted_files:
            link = candidate.get("link", "")
            if link and ".mp4" in link:
                file_url = link
                break
        if not file_url:
            continue

        clip_id = item.get("id")
        target = out_dir / f"pexels_{clip_id}.mp4"
        _save_if_needed(target, file_url)
        downloaded.append(target)
        if len(downloaded) >= max_downloads:
            break

    return downloaded


def _download_from_pixabay(
    api_key: str,
    query: str,
    out_dir: Path,
    per_page: int,
    max_downloads: int,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    url = (
        "https://pixabay.com/api/videos/"
        f"?key={api_key}&q={query}&per_page={per_page}&safesearch=true"
    )
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    payload = response.json()

    downloaded: list[Path] = []
    for item in payload.get("hits", []):
        variants = item.get("videos", {})
        if not variants:
            continue

        choices = ["large", "medium", "small", "tiny"]
        file_url = ""
        for key in choices:
            info = variants.get(key, {})
            link = info.get("url", "")
            if link:
                file_url = link
                break
        if not file_url:
            continue

        clip_id = item.get("id", "unknown")
        target = out_dir / f"pixabay_{clip_id}.mp4"
        _save_if_needed(target, file_url)
        downloaded.append(target)
        if len(downloaded) >= max_downloads:
            break

    return downloaded


def _save_if_needed(target: Path, file_url: str) -> None:
    if target.exists():
        return
    stream = requests.get(file_url, timeout=120)
    stream.raise_for_status()
    target.write_bytes(stream.content)


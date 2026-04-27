from __future__ import annotations

import os
from pathlib import Path

import requests
from gtts import gTTS


def generate_voiceover(
    script: str,
    output_path: Path,
    provider: str = "gtts",
    elevenlabs_voice_id: str = "EXAVITQu4vr4xnSDxMaL",
    elevenlabs_model: str = "eleven_multilingual_v2",
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_provider = provider.lower().strip()

    if normalized_provider == "elevenlabs":
        api_key = os.getenv("ELEVENLABS_API_KEY")
        if api_key:
            _generate_elevenlabs_voice(
                script=script,
                output_path=output_path,
                api_key=api_key,
                voice_id=elevenlabs_voice_id,
                model_id=elevenlabs_model,
            )
            return output_path

    _generate_gtts_voice(script=script, output_path=output_path)
    return output_path


def _generate_gtts_voice(script: str, output_path: Path) -> None:
    speech = gTTS(text=script, lang="en", slow=False)
    speech.save(str(output_path))


def _generate_elevenlabs_voice(
    script: str,
    output_path: Path,
    api_key: str,
    voice_id: str,
    model_id: str,
) -> None:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": script,
        "model_id": model_id,
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.8},
    }

    response = requests.post(url, headers=headers, json=payload, timeout=90)
    response.raise_for_status()
    output_path.write_bytes(response.content)

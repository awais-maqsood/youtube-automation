# Cinematic Faceless YouTube Shorts Generator

Fast-paced dark-tech vertical Shorts generator for:
- Tech Truth
- AI explainers
- App reviews

It builds 1080x1920 videos with:
- AI voiceover (`gTTS` default, `ElevenLabs` optional)
- Word-by-word dynamic subtitles
- Zoom transition rhythm every 2 seconds
- Dramatic SFX layers
- Animated bottom progress bar
- Hook text in first 2 seconds
- CTA text at end: `Follow for more tech truth`
- YouTube Shorts optimized MP4 output

## Folder Structure

```text
youtube-poop-main/
  main.py
  requirements.txt
  sample_script.json
  SHORTS_GENERATOR_README.md
  assets/
    backgrounds/      # put stock tech clips here (.mp4/.mov/.mkv/.webm)
    sfx/              # put dramatic sound effects here (.mp3/.wav/.ogg/.m4a)
    music/            # optional background music bed
  shorts_video_generator/
    __init__.py
    config.py
    main.py
    subtitle.py
    template.py
    voice.py
  temp/
  output/
```

## Install

1. Install FFmpeg (must be available in PATH).
2. Install Python dependencies:

```bash
pip install -r requirements.txt
```

The generator auto-loads environment variables from `.env` using `python-dotenv`.

## Usage

### CLI direct input

```bash
python main.py --topic "Is MovieBox Safe?" --script "MovieBox looks convenient, but here is the tech truth..."
```

### From JSON input file

```bash
python main.py --input-json sample_script.json
```

### Use ElevenLabs voice

```bash
set ELEVENLABS_API_KEY=your_key_here
python main.py --topic "5 Dangerous Android Apps" --script "Some apps can quietly track your behavior..." --voice-provider elevenlabs
```

Or place this in `.env`:

```bash
ELEVENLABS_API_KEY=your_key_here
```

### Style presets

```bash
python main.py --topic "ChatGPT vs Gemini vs Claude" --script "..." --style glitch_heavy
```

Available styles:
- `dark_cyber`
- `minimal_clean`
- `glitch_heavy`

### Auto-download stock clips (Pexels or Pixabay)

```bash
set PEXELS_API_KEY=your_key_here
python main.py --topic "Best Free AI Tools for Students" --script "..." --auto-stock --stock-query "ai tools app smartphone coding"
```

Pixabay fallback:

```bash
set PIXABAY_API_KEY=your_key_here
python main.py --topic "Best Free AI Tools for Students" --script "..." --auto-stock
```

Or in `.env`:

```bash
PIXABAY_API_KEY=your_key_here
```

### BPM-synced cuts + music bed

```bash
python main.py --topic "iPhone 18 Leak Explained" --script "..." --music-track "assets/music/track.mp3" --music-bpm 120
```

`--music-bpm` sets cut speed to `60/BPM` seconds for beat-synced transitions.

## Recommended Stock Clip Keywords

Use these search tags when collecting B-roll:
- technology neon city
- smartphone close-up
- coding screen dark mode
- AI robot hologram
- hacking terminal cyber
- app UI scrolling
- data privacy lock
- silicon chip macro

## Topic Examples

1. Is MovieBox Safe in 2026?
2. 5 Dangerous Android Apps
3. Best Free AI Tools for Students
4. Hidden WhatsApp Features You Need
5. ChatGPT vs Gemini vs Claude
6. This App Can Steal Your Data
7. iPhone 18 Leak Explained
8. Best Video Editing App Free


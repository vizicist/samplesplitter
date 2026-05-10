# samplesplitter

Polyphonic MIDI-driven MP3 sample splitter and player with a browser UI.

## Features

- Browser UI at `http://localhost:9876` — select MP3s, adjust split settings, view waveform
- Split by silence detection or fixed intervals — re-splits live as you change settings
- Waveform visualisation with labelled split markers (MIDI note names + timestamps)
- Each split mapped to a MIDI note (starting at C3 by default)
- Polyphonic playback — multiple splits simultaneously
- Pitch bend for pitch shifting (±2 semitones)
- Velocity controls volume
- Note-off stops playback
- MIDI port selectable in the UI

## Requirements

- Python 3.8+
- [ffmpeg](https://ffmpeg.org/)
- `pyo` — audio engine
- `mido` + `python-rtmidi` — MIDI input

```bash
pip3 install pyo mido python-rtmidi
brew install ffmpeg   # macOS
```

## Usage

```bash
python3 samplesplitter.py --dir /path/to/mp3s
```

Opens a browser at `http://localhost:9876` automatically.

```
Options:
  --dir DIR          Directory containing MP3 files (required)
  --port PORT        HTTP port (default: 9876)
  --base-note NOTE   MIDI note number for split 0 (default: 48 = C3)
```

## MIDI Mapping

| Control | Action |
|---|---|
| Note on (base + N) | Play split N |
| Note off | Stop that split |
| Velocity | Volume (0–127 → 0–1) |
| Pitch bend | Pitch shift ±2 semitones |

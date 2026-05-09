# samplesplitter

Polyphonic MIDI-driven sample splitter and player for short (10–30s) MP3 files.

## Features

- Split an MP3 by silence detection or fixed intervals
- Each split mapped to a MIDI note (starting at C3 by default)
- Polyphonic playback — multiple splits simultaneously
- Pitch bend controls pitch shifting (±2 semitones by default)
- Velocity controls volume
- Note-off stops playback

## Requirements

- Python 3.8+
- [ffmpeg](https://ffmpeg.org/) (for MP3 decoding)
- `pyo` — audio engine
- `mido` + `python-rtmidi` — MIDI input

```bash
pip3 install pyo mido python-rtmidi
```

## Usage

### Step 1 — Generate cue points

```bash
# By silence detection (default)
python3 splitter.py mysample.mp3

# By fixed 10-second intervals
python3 splitter.py mysample.mp3 --mode fixed --interval 10

# Silence options
python3 splitter.py mysample.mp3 --silence-thresh 0.02 --silence-min 0.3
```

Writes `mysample.cues.json` with split timestamps.

### Step 2 — Play with MIDI

```bash
# List available MIDI ports
python3 player.py --list-ports

# Play (uses first available MIDI port)
python3 player.py mysample.mp3

# Specify MIDI port (partial name match)
python3 player.py mysample.mp3 --midi-port "Keystep"

# Custom base note (default: 48 = C3)
python3 player.py mysample.mp3 --base-note 36

# Listen on specific MIDI channel (default: 0 = all)
python3 player.py mysample.mp3 --base-channel 1
```

## MIDI Mapping

| Control | Action |
|---|---|
| Note on (base note + N) | Play split N at current pitch |
| Note off | Stop that split's playback |
| Velocity | Volume (0–127 → 0.0–1.0) |
| Pitch bend | Pitch shift ±2 semitones |

## Cue File Format

```json
{
  "file": "/path/to/mysample.mp3",
  "duration": 28.43,
  "mode": "silence",
  "splits": [0.0, 4.2, 8.7, 13.1, 19.5, 24.0],
  "num_splits": 6
}
```

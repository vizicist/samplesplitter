#!/usr/bin/env python3
"""
samplesplitter.py — MIDI-driven MP3 sample splitter and polyphonic player.

Usage:
    python3 samplesplitter.py --dir /path/to/mp3s [--port 9876] [--base-note 48]

Opens a browser UI at http://localhost:9876 for file selection, splitting,
and MIDI port configuration. Plays back splits polyphonically via pyo.
"""

import argparse
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import mido
except ImportError:
    mido = None

try:
    import pyo
except ImportError:
    pyo = None

# ---------------------------------------------------------------------------
# ffmpeg path resolution
# ---------------------------------------------------------------------------

def find_ffmpeg():
    """Find ffmpeg binary: check script-relative ffmpeg/bin/ first, then PATH."""
    script_dir = Path(__file__).parent.resolve()
    candidates = [
        script_dir / "ffmpeg" / "bin" / "ffmpeg",
        script_dir / "ffmpeg" / "bin" / "ffmpeg.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    # Fall back to PATH
    import shutil
    found = shutil.which("ffmpeg")
    if found:
        return found
    print("Error: ffmpeg not found. Put ffmpeg/bin/ffmpeg next to this script, or add ffmpeg to PATH.",
          file=sys.stderr)
    sys.exit(1)

FFMPEG = find_ffmpeg()

# ---------------------------------------------------------------------------
# Global state (shared between HTTP handler and player thread)
# ---------------------------------------------------------------------------

state = {
    "mp3_dir": None,
    "current_file": None,
    "cue_data": None,
    "waveform": None,          # list of floats 0.0-1.0 (downsampled RMS)
    "midi_port_name": None,
    "midi_error": None,
    "midi_activity_count": 0,
    "midi_activity_time": None,
    "base_note": 48,
    "pitch_bend_semitones": 0.0,
    "active_voices": {},       # voice key -> (pyo.SfPlayer, pyo.CallAfter)
    "midi_note_voices": {},    # MIDI note -> queued voice keys
    "midi_voice_counter": 0,
    "pyo_server": None,
    "pyo_ready": False,
    "audio_error": None,
    "audio_output_id": None,
    "audio_output_name": None,
    "midi_thread": None,
    "midi_stop": threading.Event(),
}

state_lock = threading.Lock()

PITCH_BEND_RANGE = 2.0
PITCH_BEND_MAX = 8192
WAVEFORM_POINTS = 1200        # number of amplitude points sent to browser


def get_midi_input_names():
    if mido is None:
        return [], "mido is not installed"
    try:
        return mido.get_input_names(), None
    except Exception as e:
        return [], f"MIDI backend unavailable: {e}"


def get_audio_output_devices():
    if pyo is None:
        return [], None, "pyo is not installed"
    try:
        _, output_infos = pyo.pa_get_devices_infos()
        default_id = pyo.pa_get_default_output()
        preferred_host_order = {0: 0, 1: 1, 3: 2, 4: 3}
        by_name = {}
        for device_id, info in output_infos.items():
            name = info["name"]
            host_api = info["host api index"]
            normalized = name.lower()
            if (
                host_api == 4 and (
                    normalized.startswith("speakers 1 ") or
                    normalized.startswith("speakers 2 ") or
                    normalized.startswith("headphones 1 ") or
                    normalized.startswith("headphones 2 ")
                )
            ):
                continue
            rank = preferred_host_order.get(host_api, 99)
            if int(device_id) == int(default_id):
                rank = -1
            current = by_name.get(normalized)
            if current is None or rank < current["rank"]:
                by_name[normalized] = {
                    "id": int(device_id),
                    "name": name,
                    "rank": rank,
                }

        devices = sorted(
            (
                {"id": d["id"], "name": d["name"]}
                for d in by_name.values()
            ),
            key=lambda d: (0 if d["id"] == int(default_id) else 1, d["name"].lower())
        )
        return devices, int(default_id), None
    except Exception as e:
        return [], None, f"Audio outputs unavailable: {e}"


# ---------------------------------------------------------------------------
# Audio analysis
# ---------------------------------------------------------------------------

def mp3_to_wav(mp3_path, wav_path):
    result = subprocess.run(
        [FFMPEG, "-y", "-i", str(mp3_path), "-ar", "44100", "-ac", "1", str(wav_path)],
        capture_output=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.decode()}")


def read_wav(wav_path):
    """Return (samples_float[], frame_rate, duration_sec)."""
    import wave
    with wave.open(str(wav_path), "rb") as wf:
        frame_rate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
    samples = struct.unpack(f"{len(raw)//2}h", raw)
    floats = [s / 32768.0 for s in samples]
    duration = n_frames / frame_rate
    return floats, frame_rate, duration


def compute_waveform(samples, num_points=WAVEFORM_POINTS):
    """Downsample to num_points RMS values, normalised 0-1."""
    block = max(1, len(samples) // num_points)
    out = []
    for i in range(num_points):
        chunk = samples[i * block: (i + 1) * block]
        if not chunk:
            out.append(0.0)
        else:
            rms = math.sqrt(sum(s * s for s in chunk) / len(chunk))
            out.append(rms)
    peak = max(out) or 1.0
    return [v / peak for v in out]


def detect_splits_silence(samples, frame_rate, duration,
                           silence_thresh=0.01, min_silence_sec=0.5):
    block_sec = 0.05
    block_size = int(frame_rate * block_sec)
    min_blocks = max(1, int(min_silence_sec / block_sec))
    num_blocks = len(samples) // block_size

    silent = []
    for i in range(num_blocks):
        chunk = samples[i * block_size: (i + 1) * block_size]
        rms = math.sqrt(sum(s * s for s in chunk) / len(chunk))
        silent.append(rms < silence_thresh)

    splits = [0.0]
    i = 0
    while i < len(silent):
        if silent[i]:
            run_start = i
            while i < len(silent) and silent[i]:
                i += 1
            run_end = i
            if (run_end - run_start) >= min_blocks:
                mid_t = ((run_start + run_end) // 2) * block_sec
                if mid_t > 0.0:
                    splits.append(round(mid_t, 4))
        else:
            i += 1
    return splits


def detect_splits_fixed(duration, interval_sec=10.0):
    splits = []
    t = 0.0
    while t < duration:
        splits.append(round(t, 4))
        t += interval_sec
    return splits


def detect_splits_words(samples, frame_rate, duration,
                        silence_thresh=0.01, min_silence_sec=0.12,
                        min_word_sec=0.16, max_word_sec=0.65):
    block_sec = 0.01
    block_size = max(1, int(frame_rate * block_sec))
    min_gap_blocks = max(1, int(min_silence_sec / block_sec))
    min_word_blocks = max(1, int(min_word_sec / block_sec))
    max_word_blocks = max(min_word_blocks + 1, int(max_word_sec / block_sec))
    num_blocks = len(samples) // block_size

    if num_blocks == 0:
        return [0.0]

    rms_values = []
    for i in range(num_blocks):
        chunk = samples[i * block_size: (i + 1) * block_size]
        rms = math.sqrt(sum(s * s for s in chunk) / len(chunk))
        rms_values.append(rms)

    smooth_radius = 3
    envelope = []
    for i in range(len(rms_values)):
        start = max(0, i - smooth_radius)
        end = min(len(rms_values), i + smooth_radius + 1)
        envelope.append(sum(rms_values[start:end]) / (end - start))

    sorted_rms = sorted(rms_values)
    noise_floor = sorted_rms[max(0, int(len(sorted_rms) * 0.2) - 1)]
    peak = max(envelope) or 1.0
    threshold = max(silence_thresh, noise_floor * 3.0, peak * 0.04)
    voiced = [rms >= threshold for rms in envelope]

    runs = []
    i = 0
    while i < len(voiced):
        if voiced[i]:
            start = i
            while i < len(voiced) and voiced[i]:
                i += 1
            runs.append([start, i])
        else:
            i += 1

    if not runs:
        return [0.0]

    merged = [runs[0]]
    for start, end in runs[1:]:
        prev = merged[-1]
        if start - prev[1] < min_gap_blocks:
            prev[1] = end
        else:
            merged.append([start, end])

    split_blocks = []
    for start, end in merged:
        if end - start >= min_word_blocks:
            split_blocks.append(start)

            segment_start = start
            while segment_start + max_word_blocks < end:
                search_start = segment_start + min_word_blocks
                search_end = min(segment_start + max_word_blocks, end - min_word_blocks)
                if search_end <= search_start:
                    break

                valley = min(
                    range(search_start, search_end),
                    key=lambda idx: envelope[idx]
                )
                local_peak = max(envelope[segment_start:search_end]) or peak
                if envelope[valley] < local_peak * 0.75:
                    split_blocks.append(valley)
                    segment_start = valley
                else:
                    segment_start += max_word_blocks

    if not split_blocks:
        return [0.0]

    split_blocks = sorted(set(split_blocks))
    splits = []
    for block in split_blocks:
        t = round(block * block_sec, 4)
        if not splits or t - splits[-1] >= min_word_sec:
            splits.append(t)

    if splits[0] > 0.05:
        splits.insert(0, 0.0)
    else:
        splits[0] = 0.0
    return splits


def analyze_file(mp3_path, mode="silence", interval=10.0,
                 silence_thresh=0.01, silence_min=0.5):
    """Return (cue_data dict, waveform list)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    try:
        mp3_to_wav(mp3_path, wav_path)
        samples, frame_rate, duration = read_wav(wav_path)
    finally:
        os.unlink(wav_path)

    waveform = compute_waveform(samples)

    if mode == "silence":
        splits = detect_splits_silence(samples, frame_rate, duration,
                                       silence_thresh=silence_thresh,
                                       min_silence_sec=silence_min)
    elif mode == "words":
        splits = detect_splits_words(samples, frame_rate, duration,
                                     silence_thresh=silence_thresh,
                                     min_silence_sec=silence_min)
    else:
        splits = detect_splits_fixed(duration, interval_sec=interval)

    cue_data = {
        "file": str(mp3_path),
        "duration": round(duration, 4),
        "mode": mode,
        "splits": splits,
        "num_splits": len(splits),
    }
    return cue_data, waveform


# ---------------------------------------------------------------------------
# Pyo player
# ---------------------------------------------------------------------------

def boot_pyo_server(output_id=None):
    server = pyo.Server(duplex=0)
    if output_id is not None:
        server.setOutputDevice(int(output_id))
    server.boot()
    server.start()
    return server


def init_pyo():
    if pyo is None:
        with state_lock:
            state["audio_error"] = "pyo is not installed"
            state["pyo_ready"] = False
        print("Audio disabled: pyo is not installed.", file=sys.stderr)
        return

    with state_lock:
        output_id = state["audio_output_id"]

    server = boot_pyo_server(output_id)
    devices, default_id, _ = get_audio_output_devices()
    active_id = output_id if output_id is not None else default_id
    active_name = next((d["name"] for d in devices if d["id"] == active_id), None)
    with state_lock:
        state["pyo_server"] = server
        state["pyo_ready"] = True
        state["audio_error"] = None
        state["audio_output_id"] = active_id
        state["audio_output_name"] = active_name
    print("pyo audio server ready.")


def set_audio_output(output_id):
    if pyo is None:
        raise RuntimeError("pyo is not installed")

    devices, _, error = get_audio_output_devices()
    if error:
        raise RuntimeError(error)
    output_id = int(output_id)
    if output_id not in {d["id"] for d in devices}:
        raise RuntimeError("audio output device not found")

    player_stop_all()
    with state_lock:
        old_server = state["pyo_server"]
        state["pyo_ready"] = False
        state["pyo_server"] = None

    if old_server:
        old_server.stop()
        old_server.shutdown()

    server = boot_pyo_server(output_id)
    active_name = next((d["name"] for d in devices if d["id"] == output_id), None)
    with state_lock:
        state["pyo_server"] = server
        state["pyo_ready"] = True
        state["audio_error"] = None
        state["audio_output_id"] = output_id
        state["audio_output_name"] = active_name
    return active_name


def semitones_to_ratio(semitones):
    return 2.0 ** (semitones / 12.0)


def player_note_on(note, velocity):
    with state_lock:
        if not state["pyo_ready"] or state["current_file"] is None:
            return
        cue_data = state["cue_data"]
        if cue_data is None:
            return
        base_note = state["base_note"]
        split_index = note - base_note
        splits = cue_data["splits"]
        if split_index < 0 or split_index >= len(splits):
            return

        voice_key = f"midi-{note}-{state['midi_voice_counter']}"
        state["midi_voice_counter"] += 1
        _play_split_locked(voice_key, split_index, velocity)
        state["midi_note_voices"].setdefault(note, []).append(voice_key)


def player_preview_on(split_index, velocity=110, voice_key="preview"):
    with state_lock:
        if not state["pyo_ready"]:
            raise RuntimeError("audio server is not ready")
        if state["current_file"] is None or state["cue_data"] is None:
            raise RuntimeError("no file has been analyzed")
        splits = state["cue_data"]["splits"]
        if split_index < 0 or split_index >= len(splits):
            raise RuntimeError("split index out of range")

        _play_split_locked(voice_key, split_index, velocity)


def _play_split_locked(voice_key, split_index, velocity):
    """Must be called with state_lock held."""
    cue_data = state["cue_data"]
    splits = cue_data["splits"]
    duration = cue_data["duration"]

    _stop_voice(voice_key)

    start_sec = splits[split_index]
    end_sec = splits[split_index + 1] if split_index + 1 < len(splits) else duration
    volume = velocity / 127.0
    pitch_ratio = semitones_to_ratio(state["pitch_bend_semitones"])
    seg_duration = (end_sec - start_sec) / pitch_ratio

    mp3_path = state["current_file"]
    player = pyo.SfPlayer(
        str(mp3_path),
        speed=pitch_ratio,
        loop=False,
        offset=start_sec,
        interp=2,
        mul=volume,
    ).out()

    stop_cb = pyo.CallAfter(lambda: _stop_voice_cb(voice_key), seg_duration)
    state["active_voices"][voice_key] = (player, stop_cb)


def _stop_voice(note):
    """Must be called with state_lock held."""
    if note in state["active_voices"]:
        player, _ = state["active_voices"].pop(note)
        player.stop()
    _remove_midi_voice_key_locked(note)


def _remove_midi_voice_key_locked(voice_key):
    """Must be called with state_lock held."""
    for note, voice_keys in list(state["midi_note_voices"].items()):
        if voice_key in voice_keys:
            voice_keys.remove(voice_key)
        if not voice_keys:
            state["midi_note_voices"].pop(note, None)


def _stop_voice_cb(note):
    with state_lock:
        if note in state["active_voices"]:
            state["active_voices"].pop(note)
        _remove_midi_voice_key_locked(note)


def player_note_off(note):
    with state_lock:
        voice_keys = state["midi_note_voices"].get(note)
        if not voice_keys:
            return
        _stop_voice(voice_keys[0])


def player_preview_off(voice_key="preview"):
    with state_lock:
        _stop_voice(voice_key)


def player_pitch_bend(bend_value):
    with state_lock:
        semitones = (bend_value / PITCH_BEND_MAX) * PITCH_BEND_RANGE
        state["pitch_bend_semitones"] = semitones
        ratio = semitones_to_ratio(semitones)
        for note, (player, _) in state["active_voices"].items():
            player.setSpeed(ratio)


def player_stop_all():
    with state_lock:
        for note in list(state["active_voices"].keys()):
            _stop_voice(note)
        state["midi_note_voices"].clear()


# ---------------------------------------------------------------------------
# MIDI listener
# ---------------------------------------------------------------------------

def midi_listener(port_name, stop_event, port):
    if mido is None:
        print("MIDI disabled: mido is not installed.", file=sys.stderr)
        return

    print(f"MIDI: listening on port '{port_name}'")
    try:
        while not stop_event.is_set():
            for msg in port.iter_pending():
                handle_midi_message(msg)
            time.sleep(0.001)
    except Exception as e:
        print(f"MIDI error: {e}", file=sys.stderr)
    finally:
        port.close()


def handle_midi_message(msg):
    with state_lock:
        state["midi_activity_count"] += 1
        state["midi_activity_time"] = time.time()

    if msg.type == "note_on" and msg.velocity > 0:
        player_note_on(msg.note, msg.velocity)
    elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
        player_note_off(msg.note)
    elif msg.type == "pitchwheel":
        player_pitch_bend(msg.pitch)


def start_midi(port_name):
    if mido is None:
        raise RuntimeError("mido is not installed")

    with state_lock:
        old_thread = state["midi_thread"] if state["midi_thread"] and state["midi_thread"].is_alive() else None
        old_stop = state["midi_stop"]
    if old_thread:
        old_stop.set()
        old_thread.join(timeout=2)

    player_stop_all()
    with state_lock:
        old_server = state["pyo_server"]
        old_pyo_ready = state["pyo_ready"]
        state["pyo_server"] = None
        state["pyo_ready"] = False

    if old_server:
        old_server.stop()
        old_server.shutdown()

    try:
        midi_port = mido.open_input(port_name)
    except Exception as e:
        with state_lock:
            state["midi_error"] = str(e)
        if old_pyo_ready:
            init_pyo()
        raise

    with state_lock:
        state["midi_stop"] = threading.Event()
        state["midi_port_name"] = port_name
        state["midi_error"] = None
        t = threading.Thread(target=midi_listener,
                             args=(port_name, state["midi_stop"], midi_port),
                             daemon=True)
        state["midi_thread"] = t
    t.start()
    if old_pyo_ready:
        init_pyo()


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

def json_response(handler, data, status=200):
    body = json.dumps(data).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", len(body))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def serve_file(handler, path, content_type):
    try:
        with open(path, "rb") as f:
            data = f.read()
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", len(data))
        handler.end_headers()
        handler.wfile.write(data)
    except FileNotFoundError:
        handler.send_response(404)
        handler.end_headers()


def resolve_mp3_file(filename):
    if not filename:
        return None
    mp3_dir = Path(state["mp3_dir"]).resolve()
    mp3_path = (mp3_dir / filename).resolve()
    if mp3_path.parent != mp3_dir or mp3_path.suffix.lower() != ".mp3":
        return None
    return mp3_path if mp3_path.exists() else None


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress request logging

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            serve_file(self, Path(__file__).parent / "static" / "index.html", "text/html")

        elif path == "/api/files":
            mp3_dir = state["mp3_dir"]
            files = sorted(
                p.name for p in Path(mp3_dir).iterdir()
                if p.suffix.lower() == ".mp3"
            )
            json_response(self, {"files": files, "dir": str(mp3_dir)})

        elif path == "/api/media":
            filename = params.get("file", [None])[0]
            mp3_path = resolve_mp3_file(filename)
            if mp3_path is None:
                self.send_response(404)
                self.end_headers()
                return
            serve_file(self, mp3_path, "audio/mpeg")

        elif path == "/api/midi_ports":
            ports, error = get_midi_input_names()
            with state_lock:
                state["midi_error"] = error
                current = state["midi_port_name"]
            json_response(self, {
                "ports": ports,
                "current": current,
                "error": error,
            })

        elif path == "/api/audio_outputs":
            devices, default_id, error = get_audio_output_devices()
            with state_lock:
                current_id = state["audio_output_id"]
                current_name = state["audio_output_name"]
                if current_id is None:
                    current_id = default_id
                    current_name = next(
                        (d["name"] for d in devices if d["id"] == current_id),
                        None
                    )
            json_response(self, {
                "devices": devices,
                "default": default_id,
                "current": current_id,
                "current_name": current_name,
                "error": error,
            })

        elif path == "/api/state":
            with state_lock:
                resp = {
                    "current_file": Path(state["current_file"]).name if state["current_file"] else None,
                    "cue_data": state["cue_data"],
                    "waveform": state["waveform"],
                    "midi_port": state["midi_port_name"],
                    "midi_error": state["midi_error"],
                    "midi_activity_count": state["midi_activity_count"],
                    "midi_activity_time": state["midi_activity_time"],
                    "base_note": state["base_note"],
                    "active_voices": list(state["active_voices"].keys()),
                    "pyo_ready": state["pyo_ready"],
                    "audio_error": state["audio_error"],
                    "audio_output_id": state["audio_output_id"],
                    "audio_output_name": state["audio_output_name"],
                }
            json_response(self, resp)

        elif path == "/api/analyze":
            filename = params.get("file", [None])[0]
            mode = params.get("mode", ["fixed"])[0]
            interval = float(params.get("interval", [1.0])[0])
            silence_thresh = float(params.get("silence_thresh", [0.01])[0])
            silence_min = float(params.get("silence_min", [0.5])[0])

            if not filename:
                json_response(self, {"error": "missing file"}, 400)
                return

            mp3_path = resolve_mp3_file(filename)
            if mp3_path is None:
                json_response(self, {"error": "file not found"}, 404)
                return

            try:
                player_stop_all()
                cue_data, waveform = analyze_file(
                    mp3_path, mode=mode, interval=interval,
                    silence_thresh=silence_thresh, silence_min=silence_min
                )
                with state_lock:
                    state["current_file"] = mp3_path
                    state["cue_data"] = cue_data
                    state["waveform"] = waveform
                json_response(self, {"cue_data": cue_data, "waveform": waveform})
            except Exception as e:
                json_response(self, {"error": str(e)}, 500)

        elif path == "/api/set_midi":
            port = params.get("port", [None])[0]
            if not port:
                json_response(self, {"error": "missing port"}, 400)
                return
            if mido is None:
                json_response(self, {"error": "mido is not installed"}, 503)
                return
            try:
                start_midi(port)
                json_response(self, {"ok": True, "port": port})
            except Exception as e:
                json_response(self, {"error": str(e)}, 500)

        elif path == "/api/set_audio_output":
            output_id = params.get("id", [None])[0]
            if output_id is None:
                json_response(self, {"error": "missing id"}, 400)
                return
            try:
                name = set_audio_output(int(output_id))
                json_response(self, {"ok": True, "id": int(output_id), "name": name})
            except Exception as e:
                with state_lock:
                    state["audio_error"] = str(e)
                    state["pyo_ready"] = False
                json_response(self, {"error": str(e)}, 500)

        elif path == "/api/set_base_note":
            note = params.get("note", [None])[0]
            if note is None:
                json_response(self, {"error": "missing note"}, 400)
                return
            with state_lock:
                state["base_note"] = int(note)
            json_response(self, {"ok": True, "base_note": int(note)})

        elif path == "/api/stop_all":
            player_stop_all()
            json_response(self, {"ok": True})

        elif path == "/api/preview_on":
            index = params.get("index", [None])[0]
            voice = params.get("voice", ["preview"])[0]
            if index is None:
                json_response(self, {"error": "missing index"}, 400)
                return
            try:
                player_preview_on(int(index), voice_key=voice)
                json_response(self, {"ok": True, "index": int(index), "voice": voice})
            except Exception as e:
                json_response(self, {"error": str(e)}, 500)

        elif path == "/api/preview_off":
            voice = params.get("voice", ["preview"])[0]
            player_preview_off(voice)
            json_response(self, {"ok": True, "voice": voice})

        else:
            self.send_response(404)
            self.end_headers()


# ---------------------------------------------------------------------------
# Browser open (detect if already open via a flag file)
# ---------------------------------------------------------------------------

def open_browser(port):
    url = f"http://localhost:{port}"
    flag = Path(tempfile.gettempdir()) / f"samplesplitter_{port}.open"
    if not flag.exists():
        flag.touch()
        time.sleep(0.8)
        webbrowser.open(url)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sample splitter and MIDI player.")
    parser.add_argument("--dir", required=True, help="Directory containing MP3 files")
    parser.add_argument("--port", type=int, default=9876, help="HTTP port (default: 9876)")
    parser.add_argument("--base-note", type=int, default=48, help="MIDI base note (default: 48 = C3)")
    args = parser.parse_args()

    mp3_dir = Path(args.dir).expanduser().resolve()
    if not mp3_dir.is_dir():
        print(f"Error: directory not found: {mp3_dir}", file=sys.stderr)
        sys.exit(1)

    state["mp3_dir"] = mp3_dir
    state["base_note"] = args.base_note

    # Start pyo in background thread
    pyo_thread = threading.Thread(target=init_pyo, daemon=True)
    pyo_thread.start()

    # Open browser (non-blocking, detects if already open)
    browser_thread = threading.Thread(target=open_browser, args=(args.port,), daemon=True)
    browser_thread.start()

    print(f"Sample Splitter running at http://localhost:{args.port}")
    print(f"MP3 directory: {mp3_dir}")
    print("Press Ctrl+C to quit.\n")

    server = HTTPServer(("localhost", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        player_stop_all()
        with state_lock:
            if state["midi_stop"]:
                state["midi_stop"].set()
            if state["pyo_server"]:
                state["pyo_server"].stop()


if __name__ == "__main__":
    main()

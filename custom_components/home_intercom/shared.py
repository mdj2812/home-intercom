"""Shared audio processing — imported by both Docker (Flask) and HA integration.

Both deployment modes use the same PCM→WAV conversion and WAV concatenation.
Audio constants (PCM_RATE, PCM_BPS, WAV_MAGIC, WAV_HEADER_SIZE) come from const.py.
"""

from __future__ import annotations

import logging
import os
import shutil
import wave

try:
    from .const import PCM_BPS, WAV_MAGIC  # HA integration (relative)
except ImportError:
    from const import PCM_BPS, WAV_MAGIC  # Docker standalone (absolute)

_LOGGER = logging.getLogger(__name__)


def is_wav(data: bytes) -> bool:
    """Check if raw data starts with WAV RIFF magic."""
    return data[: len(WAV_MAGIC)] == WAV_MAGIC


def handle_wav_passthrough(data: bytes, filepath: str) -> tuple[int, float]:
    """ESP32 / complete WAV file → write as-is.

    Returns (sample_rate, duration_seconds).
    """
    with open(filepath, "wb") as f:
        f.write(data)
    with wave.open(filepath, "rb") as wf:
        rate = wf.getframerate()
        duration = wf.getnframes() / rate
    _LOGGER.info(
        "WAV passthrough %dB, %dHz, %dch, %dbit, %.1fs",
        len(data),
        rate,
        wf.getnchannels(),
        wf.getsampwidth() * 8,
        duration,
    )
    return rate, duration


def handle_pcm_to_wav(data: bytes, rate: int, filepath: str) -> float:
    """Raw 16-bit mono PCM → write WAV file with correct header.

    Returns duration_seconds.
    """
    with wave.open(filepath, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(PCM_BPS)
        wf.setframerate(rate)
        wf.writeframes(data)
    duration = len(data) / (rate * PCM_BPS)
    file_size = os.path.getsize(filepath)
    _LOGGER.info(
        "WAV written: %s (%dB, %.1fs, %dHz)",
        os.path.basename(filepath),
        file_size,
        duration,
        rate,
    )
    return duration


def concat_wavs(chime_path: str, audio_path: str, output_path: str) -> float:
    """Prepend chime WAV to audio WAV. Returns total duration (seconds).

    Both files must have the same sample rate, channels, and sample width.
    On format mismatch, copies audio as-is and logs a warning.
    """
    with wave.open(chime_path, "rb") as wf_chime:
        chime_rate = wf_chime.getframerate()
        chime_frames = wf_chime.readframes(wf_chime.getnframes())
        chime_width = wf_chime.getsampwidth()
        chime_channels = wf_chime.getnchannels()

    with wave.open(audio_path, "rb") as wf_audio:
        audio_rate = wf_audio.getframerate()
        audio_frames = wf_audio.readframes(wf_audio.getnframes())
        audio_width = wf_audio.getsampwidth()
        audio_channels = wf_audio.getnchannels()

    if (chime_rate, chime_width, chime_channels) != (audio_rate, audio_width, audio_channels):
        _LOGGER.warning(
            "chime/audio format mismatch (chime=%dHz/%dB/%dch, audio=%dHz/%dB/%dch) — skipping chime",
            chime_rate,
            chime_width,
            chime_channels,
            audio_rate,
            audio_width,
            audio_channels,
        )
        shutil.copyfile(audio_path, output_path)
        with wave.open(output_path, "rb") as wf:
            duration = wf.getnframes() / wf.getframerate()
        return duration

    with wave.open(output_path, "wb") as wf:
        wf.setnchannels(audio_channels)
        wf.setsampwidth(audio_width)
        wf.setframerate(audio_rate)
        wf.writeframes(chime_frames + audio_frames)

    total_frames = (len(chime_frames) + len(audio_frames)) // (audio_width * audio_channels)
    duration = total_frames / audio_rate
    _LOGGER.info("chime + audio combined: %s (%.1fs)", os.path.basename(output_path), duration)
    return duration


def write_wav_metadata(filepath: str, title: str = "家庭广播", artist: str = "") -> None:
    """Inject LIST INFO metadata into a WAV file.

    Xiaomi screen speakers display metadata from the file itself when
    playing local audio — this overrides the random cloud metadata they
    would otherwise show during broadcasts.
    """
    import struct as _struct

    # Build LIST INFO chunk
    info_tags = []
    if title:
        title_utf8 = title.encode("utf-8")
        title_len = len(title_utf8)
        tag = b"INAM" + _struct.pack("<I", title_len) + title_utf8 + b"\x00"
        if len(tag) % 2:
            tag += b"\x00"
        info_tags.append(tag)
    if artist:
        artist_utf8 = artist.encode("utf-8")
        artist_len = len(artist_utf8)
        tag = b"IART" + _struct.pack("<I", artist_len) + artist_utf8 + b"\x00"
        if len(tag) % 2:
            tag += b"\x00"
        info_tags.append(tag)

    if not info_tags:
        return

    info_data = b"".join(info_tags)
    list_data = b"INFO" + info_data
    list_chunk = b"LIST" + _struct.pack("<I", len(list_data)) + list_data

    with open(filepath, "r+b") as f:
        riff = f.read(4)
        if riff != b"RIFF":
            _LOGGER.warning("Not a WAV file: %s", filepath)
            return
        total_size = _struct.unpack("<I", f.read(4))[0]
        wave = f.read(4)
        if wave != b"WAVE":
            _LOGGER.warning("Not a WAV file (no WAVE marker): %s", filepath)
            return

        # Find 'data' chunk and its position
        while True:
            chunk_id = f.read(4)
            chunk_size = _struct.unpack("<I", f.read(4))[0]
            if chunk_id == b"data":
                data_start = f.tell()
                break
            # Skip non-data chunks
            f.seek(chunk_size + (chunk_size % 2), 1)

        # Read everything from data chunk onwards
        f.seek(data_start)
        rest = f.read()

        # Write LIST chunk, then data chunk
        f.seek(data_start)
        f.write(list_chunk)
        f.write(rest)

        # Update RIFF total size
        new_total = total_size + len(list_chunk)
        f.seek(4)
        f.write(_struct.pack("<I", new_total))

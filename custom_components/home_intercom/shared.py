"""Shared audio processing — imported by both Docker (Flask) and HA integration.

Both deployment modes use the same PCM→WAV conversion and WAV concatenation.
Audio constants (PCM_RATE, PCM_BPS, WAV_MAGIC, WAV_HEADER_SIZE) come from const.py.
"""

from __future__ import annotations

import logging
import os
import shutil
import wave

from const import PCM_BPS, WAV_MAGIC

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
        len(data), rate, wf.getnchannels(), wf.getsampwidth() * 8, duration,
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
        os.path.basename(filepath), file_size, duration, rate,
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
            chime_rate, chime_width, chime_channels, audio_rate, audio_width, audio_channels,
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

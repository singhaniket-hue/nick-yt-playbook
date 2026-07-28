"""ffmpeg audio assembly: decode chunks, build true-silence gaps, concatenate."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

SAMPLE_RATE = 44100
CHANNELS = 1


def _run(args: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"ffmpeg failed: {' '.join(args[:4])} ...\n{detail}")
    return result


def probe_duration(path: Path) -> float:
    """Duration in seconds, via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True,
        check=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def build_silence(seconds: float, out_path: Path) -> Path:
    """Write true digital silence, not room tone.

    The format mutes the bed completely before a reveal; room tone would read as a
    dropout rather than a deliberate beat.
    """
    _run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"anullsrc=r={SAMPLE_RATE}:cl=mono",
            "-t", str(seconds), "-ac", str(CHANNELS),
            "-c:a", "pcm_s16le", str(out_path),
        ]
    )
    return out_path


def decode_to_wav(src: Path, out_path: Path) -> Path:
    """Normalise any TTS output to mono 44.1k PCM so concat is lossless."""
    _run(
        [
            "ffmpeg", "-y", "-i", str(src),
            "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
            "-c:a", "pcm_s16le", str(out_path),
        ]
    )
    return out_path


def concat_wavs(parts: list[Path], out_path: Path) -> Path:
    """Concatenate WAVs in order via the ffmpeg concat demuxer."""
    if not parts:
        raise ValueError("concat_wavs needs at least one input file.")

    listing = out_path.with_suffix(".concat.txt")
    listing.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in parts) + "\n",
        encoding="utf-8",
    )

    _run(
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(listing), "-c:a", "pcm_s16le", str(out_path),
        ]
    )
    listing.unlink()
    return out_path

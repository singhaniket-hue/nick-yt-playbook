"""Which H.264 encoder to use, and its quality flags.

One place, because six modules encode video and they must agree: `render`
(per-cut segments and the finish pass), `subtitles` (the burn), `graphics` (the
composite), `sources/plates`, `sources/capture`. A render is a chain of full-
length passes, so an encoder chosen in five of six places is barely faster than
one chosen in none.

**Why this is worth having at all.** A 36.8-minute episode is 710 per-cut
encodes plus four full-length passes. On this machine NVENC encodes 10 seconds
of 1080p in 1.6s against libx264's 3.5s; the filter graphs stay on the CPU
either way, so the saving applies to the encode half of every pass.

**The flags are not interchangeable, which is the trap.** libx264 takes `-crf`;
NVENC rejects it and wants `-cq` with its own `-preset` vocabulary (`p1`-`p7`,
not `veryfast`/`slow`). Passing x264 flags to NVENC fails outright, and passing
NVENC's preset names to x264 fails differently -- so quality is expressed here
as a single number and each backend translates it.

Selection is auto-detect with an override: `RABBITHOLE_VIDEO_ENCODER=libx264`
forces the CPU path, which matters because NVENC is a fixed-function encoder and
at matched bitrate is slightly behind x264 on quality. For intermediate segments
that is irrelevant; for a final master an author may reasonably want x264, and
should be able to say so without editing code.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
import shutil
import subprocess

CPU_ENCODER = "libx264"
GPU_ENCODERS = ("h264_nvenc", "h264_qsv", "h264_amf")

ENV_OVERRIDE = "RABBITHOLE_VIDEO_ENCODER"

# NVENC preset vocabulary is p1 (fastest) .. p7 (slowest/best). p4 is its
# balanced default and the closest analogue to x264's "medium".
_NVENC_PRESET = "p4"


@functools.lru_cache(maxsize=None)
def available_filters(executable: str = "ffmpeg") -> frozenset[str]:
    """Filter names this ffmpeg build advertises.

    Homebrew's regular FFmpeg 8 formula is intentionally a smaller build and
    does not include libass.  In that build a graph such as ``ass=subs.ass``
    fails with the misleading parser message ``No option name near`` rather
    than saying that the filter is absent.  Querying ``-filters`` lets callers
    fail before starting an encode and explain the actual dependency.
    """
    try:
        result = subprocess.run(
            [executable, "-hide_banner", "-filters"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    text = result.stdout.decode("utf-8", errors="replace")
    names: set[str] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3 and "->" in fields[2]:
            names.add(fields[1])
    return frozenset(names)


def _ffmpeg_candidates() -> tuple[str, ...]:
    """FFmpeg executables worth probing, in user-visible priority order.

    ``ffmpeg-full`` is keg-only, so Homebrew deliberately does not put it on
    PATH. Probe its stable ``opt`` location after the user's active ffmpeg; this
    lets an ASS-only stage use the capable build without changing which FFmpeg
    the rest of an established pipeline uses.
    """
    candidates: list[str] = []
    active = shutil.which("ffmpeg")
    if active:
        candidates.append(active)
    for path in (
        Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"),
        Path("/usr/local/opt/ffmpeg-full/bin/ffmpeg"),
    ):
        if path.is_file():
            candidates.append(os.fspath(path))
    return tuple(dict.fromkeys(candidates))


def require_filter(name: str) -> str:
    """Return an FFmpeg providing ``name``, or raise an actionable error."""
    for executable in _ffmpeg_candidates():
        if name in available_filters(executable):
            return executable
    raise RuntimeError(
        f"FFmpeg on PATH is missing the required {name!r} filter. Install an "
        "FFmpeg build compiled with libass. On macOS with Homebrew, use "
        "`brew install ffmpeg-full` and put "
        "`$(brew --prefix ffmpeg-full)/bin` before the regular FFmpeg on PATH."
    )


@functools.lru_cache(maxsize=1)
def _available_encoders() -> frozenset[str]:
    """Encoder names this ffmpeg build advertises.

    Cached: `ffmpeg -encoders` is spawned once per process rather than once per
    cut, which on a 710-cut render is the difference between negligible and not.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    text = result.stdout.decode("utf-8", errors="replace")
    return frozenset(
        line.split()[1] for line in text.splitlines()
        if line.startswith(" V") and len(line.split()) > 1
    )


def _encoder_works(name: str) -> bool:
    """Whether this encoder can actually encode a frame right now.

    Being listed by `ffmpeg -encoders` only means it was compiled in. NVENC on a
    machine with no NVIDIA driver, or with the GPU otherwise unavailable, is
    listed and then fails at run time -- so the check is a real one-frame encode
    to null, not a string match.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi",
             "-i", "color=c=black:s=320x240:d=0.1", "-c:v", name,
             "-f", "null", "-"],
            capture_output=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


@functools.lru_cache(maxsize=1)
def detect_encoder() -> str:
    """The H.264 encoder to use: a working GPU one, else libx264.

    `RABBITHOLE_VIDEO_ENCODER` overrides, and is honoured verbatim -- an author
    naming an encoder has said what they want, and silently substituting a
    different one would be worse than failing at the ffmpeg call.
    """
    override = os.environ.get(ENV_OVERRIDE, "").strip()
    if override:
        return override

    available = _available_encoders()
    for candidate in GPU_ENCODERS:
        if candidate in available and _encoder_works(candidate):
            return candidate
    return CPU_ENCODER


# When a bitrate cap is requested, NVENC needs a target to rate-control toward.
# Two thirds of the cap leaves headroom for the peaks the cap exists to bound.
_NVENC_TARGET_FRACTION = 2 / 3


def _parse_rate(rate: str) -> int:
    """"6M" -> 6000000. Accepts a plain integer count of bits/s too."""
    text = str(rate).strip().upper()
    multiplier = 1
    if text.endswith("K"):
        multiplier, text = 1_000, text[:-1]
    elif text.endswith("M"):
        multiplier, text = 1_000_000, text[:-1]
    return int(float(text) * multiplier)


def video_args(
    quality: int = 20,
    encoder: str | None = None,
    maxrate: str | None = None,
    bufsize: str | None = None,
) -> list[str]:
    """ffmpeg output args for one H.264 encode at roughly `quality`.

    `quality` is on x264's CRF scale (lower is better, ~18 visually lossless,
    ~28 poor). NVENC's `-cq` uses the same direction and a close enough scale
    that one number can drive both; it is not claimed to be an exact match.

    **`maxrate` is not optional decoration where it is passed.** A static plate
    is pure noise, which is the worst case for any encoder: one 12-second plate
    once produced a 311 MB file, and `PLATE_MAXRATE` exists to bound that. The
    cap has to survive the switch to a GPU encoder, and NVENC makes that subtle
    -- it honours `-maxrate` only when `-b:v` is set and `-cq` is *absent*.
    Supply both and it silently ignores the cap: measured at 10.16 Mbit/s
    against a 6 Mbit cap, versus 6.30 once `-cq` is dropped for `-rc vbr -b:v`.
    So a capped NVENC encode is rate-controlled, not quality-controlled, and
    `quality` is deliberately not passed in that mode.
    """
    chosen = encoder or detect_encoder()
    cap = ["-maxrate", maxrate, "-bufsize", bufsize] if maxrate and bufsize else []

    if chosen.endswith("_nvenc"):
        if maxrate:
            target = int(_parse_rate(maxrate) * _NVENC_TARGET_FRACTION)
            return ["-c:v", chosen, "-preset", _NVENC_PRESET,
                    "-rc", "vbr", "-b:v", str(target), *cap]
        return ["-c:v", chosen, "-preset", _NVENC_PRESET, "-cq", str(quality)]
    if chosen.endswith("_qsv"):
        return ["-c:v", chosen, "-global_quality", str(quality), *cap]
    if chosen.endswith("_amf"):
        return ["-c:v", chosen, "-quality", "balanced", "-qp_i", str(quality),
                "-qp_p", str(quality), *cap]
    return ["-c:v", chosen, "-crf", str(quality), *cap]


def is_gpu(encoder: str | None = None) -> bool:
    chosen = encoder or detect_encoder()
    return chosen != CPU_ENCODER and any(
        chosen.endswith(suffix) for suffix in ("_nvenc", "_qsv", "_amf", "_vaapi")
    )

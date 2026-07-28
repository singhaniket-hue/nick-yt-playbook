"""Helpers for rendering a bounded excerpt of an episode.

The production renderer historically had one unit of work: the entire EDL.
That made visual review needlessly expensive and, more importantly, made it
too easy to approve a structurally valid 36-minute render without first
watching representative footage.  This module keeps source-timeline
coordinates where asset seeking needs them, while shifting the timing
document and overlays to the excerpt's zero-based output timeline.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import replace
from pathlib import Path

from rabbithole.edl import Cut
from rabbithole.overlays import Overlay

_TIMECODE_RE = re.compile(
    r"^(?:(?P<hours>\d+):)?(?P<minutes>\d{1,2}):(?P<seconds>\d{1,2}(?:\.\d+)?)$"
)
_WINDOW_EPSILON = 1e-6


def parse_timecode(value: str | float | int) -> float:
    """Parse seconds or ``HH:MM:SS(.sss)`` / ``MM:SS(.sss)``."""
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass

    match = _TIMECODE_RE.fullmatch(text)
    if match is None:
        raise ValueError(
            f"Invalid timecode {value!r}; use seconds or MM:SS / HH:MM:SS."
        )

    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes"))
    seconds = float(match.group("seconds"))
    if minutes >= 60 and match.group("hours") is not None:
        raise ValueError(f"Invalid timecode {value!r}: minutes must be below 60.")
    if seconds >= 60:
        raise ValueError(f"Invalid timecode {value!r}: seconds must be below 60.")
    return hours * 3600 + minutes * 60 + seconds


def validate_window(start: float, end: float, duration: float) -> None:
    if start < 0:
        raise ValueError("Segment start must be zero or greater.")
    if end <= start:
        raise ValueError("Segment end must be later than its start.")
    if end > duration + 1e-6:
        raise ValueError(
            f"Segment ends at {end:.3f}s, past the episode duration of {duration:.3f}s."
        )


def window_cuts(cuts: list[Cut], start: float, end: float) -> list[Cut]:
    """Clip cuts to ``[start, end]`` while retaining source coordinates.

    ``render.cut_segment`` uses ``cut.start - slot.start`` to seek into the
    underlying asset, so cut times must remain in episode coordinates.
    Concatenation naturally makes the selected cuts start at output time zero.
    """
    selected: list[Cut] = []
    for cut in cuts:
        clipped_start = max(cut.start, start)
        clipped_end = min(cut.end, end)
        # JSON round-trips can leave a boundary such as 503.344 represented
        # as 503.34400000000005.  Never turn that numerical dust into an
        # extra zero-length cut (and an unnecessary source-quality gate).
        if clipped_end - clipped_start <= _WINDOW_EPSILON:
            continue
        selected.append(
            replace(
                cut,
                index=len(selected),
                start=clipped_start,
                end=clipped_end,
                reason=f"{cut.reason}; clipped to review segment {start:.3f}-{end:.3f}s",
            )
        )
    return selected


def window_overlays(
    overlays: list[Overlay], start: float, end: float
) -> list[Overlay]:
    """Select, clip, and shift overlays onto a zero-based excerpt timeline."""
    selected: list[Overlay] = []
    for overlay in overlays:
        clipped_start = max(overlay.start, start)
        clipped_end = min(overlay.end, end)
        if clipped_end - clipped_start <= _WINDOW_EPSILON:
            continue
        selected.append(
            replace(
                overlay,
                start=clipped_start - start,
                end=clipped_end - start,
            )
        )
    return selected


def window_document(document: dict, start: float, end: float) -> dict:
    """Return a zero-based timing document for audio and subtitle generation."""
    selected_words = []
    for word in document.get("words", []):
        if float(word["end"]) <= start or float(word["start"]) >= end:
            continue
        shifted = dict(word)
        shifted["start"] = max(float(word["start"]), start) - start
        shifted["end"] = min(float(word["end"]), end) - start
        selected_words.append(shifted)

    selected_markers: list[dict] = []
    markers = document.get("markers", [])
    for marker in markers:
        seconds = float(marker.get("seconds", 0.0))
        if start <= seconds < end:
            shifted = dict(marker)
            shifted["seconds"] = seconds - start
            selected_markers.append(shifted)

    # A segment beginning mid-bed still needs the MUSIC state active at its
    # first frame.  Carry forward the last cue at/before the window start.
    earlier_music = [
        marker
        for marker in markers
        if marker.get("kind") == "MUSIC"
        and float(marker.get("seconds", 0.0)) <= start
    ]
    if earlier_music and not any(
        marker.get("kind") == "MUSIC"
        and abs(float(marker.get("seconds", 0.0))) < 1e-9
        for marker in selected_markers
    ):
        carried = dict(max(earlier_music, key=lambda m: float(m.get("seconds", 0.0))))
        carried["seconds"] = 0.0
        carried["line"] = carried.get("line", 0)
        selected_markers.append(carried)

    selected_markers.sort(key=lambda marker: (float(marker.get("seconds", 0.0)), marker.get("kind", "")))
    result = dict(document)
    result["duration_seconds"] = end - start
    result["words"] = selected_words
    result["word_count"] = len(selected_words)
    result["markers"] = selected_markers
    result["segment_source_start"] = start
    result["segment_source_end"] = end
    return result


def trim_audio(
    source: Path,
    out_path: Path,
    *,
    start: float,
    duration: float,
) -> Path:
    """Decode a precise audio excerpt to the pipeline's working PCM format."""
    source = Path(source)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start:.6f}",
            "-t",
            f"{duration:.6f}",
            "-i",
            str(source),
            "-ar",
            "44100",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(out_path),
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-1200:]
        if out_path.exists():
            out_path.unlink()
        raise RuntimeError(f"ffmpeg failed while trimming segment audio:\n{detail}")
    return out_path


def segment_slug(start: float, end: float) -> str:
    def token(seconds: float) -> str:
        millis = round(seconds * 1000)
        return f"{millis // 1000:05d}-{millis % 1000:03d}"

    return f"{token(start)}_to_{token(end)}"

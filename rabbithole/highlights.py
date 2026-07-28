"""Timed yellow article/newspaper highlights.

The treatment is intentionally evidence-specific: an opaque amber strip
reveals left-to-right behind the exact line being narrated, then remains while
the viewer reads the surrounding source.  The matte is multiplied with the
page so dark type remains dark instead of being painted over.  It is not a
general keyword title effect and never invents rectangles; every region is
authored against a verified page capture in ``research/highlights.json``.
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from rabbithole.encoding import video_args
from rabbithole.sources.plates import PLATE_BUFSIZE, PLATE_CRF, PLATE_MAXRATE
from rabbithole.validate import Finding

DEFAULT_COLOR = "FFB900"
DEFAULT_OPACITY = 1.0
DEFAULT_REVEAL_SECONDS = 0.52


@dataclass(frozen=True)
class ArticleHighlight:
    start: float
    end: float
    x: float
    y: float
    width: float
    height: float
    reveal_seconds: float = DEFAULT_REVEAL_SECONDS
    color: str = DEFAULT_COLOR
    opacity: float = DEFAULT_OPACITY
    label: str = ""
    source: str = ""


def _word_start(document: dict, index: int) -> float:
    for word in document.get("words", []):
        if int(word.get("index", -1)) == index:
            return float(word["start"])
    raise ValueError(f"Highlight word_index {index} does not exist in timing.json.")


def load_highlights(path: Path, document: dict) -> list[ArticleHighlight]:
    """Load and validate normalized rectangles from a project highlight spec."""
    path = Path(path)
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        defaults: dict = {}
        entries = raw
    elif isinstance(raw, dict):
        defaults = raw.get("style", {})
        entries = raw.get("highlights", [])
    else:
        raise ValueError("Highlight spec must be a JSON object or list.")
    result: list[ArticleHighlight] = []

    for index, entry in enumerate(entries):
        if "word_index" in entry:
            start = _word_start(document, int(entry["word_index"]))
        elif "start" in entry:
            start = float(entry["start"])
        else:
            raise ValueError(
                f"Highlight {index} needs word_index or absolute start seconds."
            )

        if "end" in entry:
            end = float(entry["end"])
        elif "hold_seconds" in entry:
            end = start + float(entry["hold_seconds"])
        else:
            raise ValueError(f"Highlight {index} needs end or hold_seconds.")

        rect = entry.get("rect")
        if not isinstance(rect, list) or len(rect) != 4:
            raise ValueError(
                f"Highlight {index} rect must be [x, y, width, height] normalized to 0..1."
            )
        x, y, width, height = (float(value) for value in rect)
        if (
            x < 0
            or y < 0
            or width <= 0
            or height <= 0
            or x + width > 1
            or y + height > 1
        ):
            raise ValueError(
                f"Highlight {index} rect {rect!r} lies outside the normalized frame."
            )
        if end <= start:
            raise ValueError(
                f"Highlight {index} ends at {end:.3f}s, not after {start:.3f}s."
            )

        color = str(entry.get("color", defaults.get("color", DEFAULT_COLOR)))
        color = color.removeprefix("#")
        if len(color) != 6 or any(c not in "0123456789abcdefABCDEF" for c in color):
            raise ValueError(f"Highlight {index} color {color!r} is not RRGGBB.")
        opacity = float(entry.get("opacity", defaults.get("opacity", DEFAULT_OPACITY)))
        reveal = float(
            entry.get(
                "reveal_seconds",
                defaults.get("reveal_seconds", DEFAULT_REVEAL_SECONDS),
            )
        )
        if not 0 < opacity <= 1:
            raise ValueError(f"Highlight {index} opacity must be in (0, 1].")
        if reveal <= 0:
            raise ValueError(f"Highlight {index} reveal_seconds must be positive.")
        if reveal > end - start:
            raise ValueError(
                f"Highlight {index} reveal_seconds ({reveal:.3f}) exceeds its "
                f"{end - start:.3f}s visible interval."
            )

        result.append(
            ArticleHighlight(
                start=start,
                end=end,
                x=x,
                y=y,
                width=width,
                height=height,
                reveal_seconds=reveal,
                color=color.upper(),
                opacity=opacity,
                label=str(entry.get("label", "")),
                source=str(entry.get("source", "")),
            )
        )

    result.sort(key=lambda item: (item.start, item.y, item.x))
    return result


def window_highlights(
    highlights: list[ArticleHighlight], start: float, end: float
) -> list[ArticleHighlight]:
    """Clip and shift absolute episode highlights to a review segment."""
    selected: list[ArticleHighlight] = []
    for item in highlights:
        clipped_start = max(item.start, start)
        clipped_end = min(item.end, end)
        if clipped_end <= clipped_start:
            continue
        # If a segment begins after the original reveal, show the completed
        # strip immediately rather than replaying an out-of-context animation.
        reveal = (
            min(item.reveal_seconds, clipped_end - clipped_start)
            if item.start >= start
            else 0.001
        )
        selected.append(
            replace(
                item,
                start=clipped_start - start,
                end=clipped_end - start,
                reveal_seconds=reveal,
            )
        )
    return selected


def highlight_filter(
    highlights: list[ArticleHighlight],
    width: int,
    height: int,
    fps: float = 30.0,
) -> str:
    """Build a frame-quantized left-to-right wipe on a white matte.

    FFmpeg's ``drawbox`` width is evaluated when the filter initializes, not
    once per frame.  A time-dependent width therefore jumps straight to a full
    strip on real renders even though the expression looks correct.  Instead,
    divide the strip into one fixed slice per reveal frame and enable those
    slices in sequence.  The visible edge advances at the output frame rate,
    while every completed slice remains until the evidence cut.

    The resulting white/yellow matte is multiplied into the page so black
    glyphs survive unchanged.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"Highlight frame dimensions must be positive, got {width}x{height}.")
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError(f"Highlight frame rate must be positive, got {fps!r}.")

    filters: list[str] = []
    for item in highlights:
        x = round(item.x * width)
        y = round(item.y * height)
        box_width = max(1, round(item.width * width))
        box_height = max(1, round(item.height * height))
        reveal = min(item.reveal_seconds, item.end - item.start)
        color = f"0x{item.color}@{item.opacity:.3f}"
        steps = max(1, int(math.ceil(reveal * fps)))
        for step in range(steps):
            left = x + round(box_width * step / steps)
            right = x + round(box_width * (step + 1) / steps)
            slice_width = max(1, right - left)
            slice_start = item.start + reveal * step / steps
            enable = f"between(t\\,{slice_start:.6f}\\,{item.end:.6f})"
            filters.append(
                f"drawbox=x={left}:y={y}:w={slice_width}:h={box_height}:"
                f"color={color}:t=fill:enable='{enable}'"
            )

    return ",".join(filters) if filters else "null"


def apply_highlights(
    video_path: Path,
    highlights: list[ArticleHighlight],
    out_path: Path,
    *,
    width: int,
    height: int,
    fps: float = 30.0,
) -> tuple[Path, list[Finding]]:
    """Multiply authored highlight strips under type and copy mastered audio."""
    video_path = Path(video_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not highlights:
        raise ValueError("apply_highlights requires at least one highlight.")
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError(f"Highlight frame rate must be positive, got {fps!r}.")

    filter_graph = (
        "[0:v]format=gbrp[page];"
        f"color=c=white:s={width}x{height}:r={fps:.6f},format=gbrp[mattebase];"
        f"[mattebase]{highlight_filter(highlights, width, height, fps)}[matte];"
        "[page][matte]blend=all_mode=multiply:shortest=1,"
        "format=yuv420p[outv]"
    )

    # A dense evidence sequence can expand to hundreds of frame-quantized
    # drawbox slices.  Passing that graph inline exceeds Windows' process
    # command-line limit, so hand FFmpeg a UTF-8 filter script instead.
    filter_script = out_path.with_suffix(".filter-complex.txt")
    filter_script.write_text(filter_graph, encoding="utf-8")
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-filter_complex_script",
                str(filter_script),
                "-map",
                "[outv]",
                "-map",
                "0:a?",
                *video_args(PLATE_CRF, maxrate=PLATE_MAXRATE, bufsize=PLATE_BUFSIZE),
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "copy",
                str(out_path),
            ],
            capture_output=True,
        )
    finally:
        filter_script.unlink(missing_ok=True)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-1200:]
        if out_path.exists():
            out_path.unlink()
        raise RuntimeError(f"ffmpeg failed applying article highlights:\n{detail}")

    findings = [
        Finding(
            gate="highlights",
            severity="warning" if not item.source else "info",
            message=(
                f"Article highlight {item.label or '(unlabelled)'} at "
                f"{item.start:.2f}-{item.end:.2f}s"
                + (
                    f" is tied to {item.source}."
                    if item.source
                    else " has no source label; verify it against the displayed artifact."
                )
            ),
        )
        for item in highlights
    ]
    return out_path, findings

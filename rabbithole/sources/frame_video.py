"""Derive a portable still-video asset from an already downloaded video.

This module deliberately has no downloader and accepts no URL.  It turns one
frame from a retained source video into the slot-length MP4 used by a
``screenshot-only`` editorial slot:

* seek to an explicit source-relative timestamp;
* optionally crop an explicit source-pixel rectangle;
* retain the whole requested rectangle on a 1920x1080 canvas;
* optionally add a restrained source/date label;
* encode a silent, constant-frame-rate H.264 MP4; and
* probe the completed temporary file before atomically replacing the output.

The seek is an ffmpeg *output* seek (``-ss`` after ``-i``).  That is slower
than a keyframe seek, but it makes the selected frame deterministic rather
than allowing ffmpeg to land on an earlier keyframe.  ``-noautorotate`` makes
crop coordinates refer to the encoded source pixels on Windows and macOS
alike.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from fractions import Fraction
from numbers import Integral
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image, ImageDraw, ImageFont

from rabbithole.encoding import CPU_ENCODER, video_args

FRAME_WIDTH = 1920
FRAME_HEIGHT = 1080
FRAME_RATE = 30
ENCODE_QUALITY = 20

# argv -> (returncode, stdout, stderr)
Runner = Callable[[list[str]], tuple[int, bytes, bytes]]
Crop = tuple[int, int, int, int]


def _default_runner(argv: list[str]) -> tuple[int, bytes, bytes]:
    result = subprocess.run(argv, capture_output=True)  # pragma: no cover
    return result.returncode, result.stdout, result.stderr  # pragma: no cover


def _text(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _run_checked(
    argv: list[str],
    *,
    purpose: str,
    runner: Runner,
) -> bytes:
    try:
        returncode, stdout, stderr = runner(argv)
    except OSError as exc:
        raise RuntimeError(
            f"{purpose} could not start {argv[0]!r}: {exc}"
        ) from exc
    if returncode != 0:
        detail = _text(stderr)[-1000:].strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"{purpose} failed (exit {returncode}){suffix}"
        )
    return stdout if isinstance(stdout, bytes) else str(stdout).encode("utf-8")


def _probe(
    path: Path,
    *,
    runner: Runner,
    count_frames: bool,
) -> dict:
    argv = [
        "ffprobe",
        "-v",
        "error",
    ]
    if count_frames:
        argv.append("-count_frames")
    argv.extend(
        [
            "-show_entries",
            (
                "stream=index,codec_type,codec_name,width,height,pix_fmt,"
                "r_frame_rate,avg_frame_rate,nb_read_frames,duration:"
                "format=duration"
            ),
            "-of",
            "json",
            str(path),
        ]
    )
    stdout = _run_checked(argv, purpose=f"ffprobe validation for {path.name}", runner=runner)
    try:
        document = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"ffprobe returned unreadable metadata for {path.name}; refusing "
            "to trust the derived asset."
        ) from exc
    if not isinstance(document, dict) or not isinstance(document.get("streams"), list):
        raise RuntimeError(
            f"ffprobe returned no stream list for {path.name}; refusing to "
            "trust the derived asset."
        )
    return document


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _source_video(document: dict, source: Path) -> tuple[int, int, float]:
    videos = [
        stream
        for stream in document["streams"]
        if stream.get("codec_type") == "video"
    ]
    if not videos:
        raise RuntimeError(
            f"Source {source.name} has no decodable video stream."
        )
    stream = videos[0]
    try:
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"ffprobe did not report source dimensions for {source.name}."
        ) from exc
    if width <= 0 or height <= 0:
        raise RuntimeError(
            f"ffprobe reported invalid source dimensions {width}x{height} "
            f"for {source.name}."
        )

    duration = _positive_float(stream.get("duration"))
    if duration is None:
        duration = _positive_float(document.get("format", {}).get("duration"))
    if duration is None:
        raise RuntimeError(
            f"ffprobe did not report a finite source duration for "
            f"{source.name}; a requested timestamp cannot be validated."
        )
    return width, height, duration


def _normalise_crop(
    crop: Sequence[int] | None,
    *,
    source_width: int,
    source_height: int,
) -> Crop | None:
    if crop is None:
        return None
    if isinstance(crop, (str, bytes)) or len(crop) != 4:
        raise ValueError("crop must be [x, y, width, height] in source pixels.")
    if any(isinstance(value, bool) or not isinstance(value, Integral) for value in crop):
        raise ValueError("crop values must be integer source-pixel coordinates.")
    x, y, width, height = (int(value) for value in crop)
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise ValueError(
            f"crop contains an invalid source-pixel rectangle: "
            f"{[x, y, width, height]!r}."
        )
    if x + width > source_width or y + height > source_height:
        raise ValueError(
            f"crop {[x, y, width, height]!r} exceeds the encoded source frame "
            f"({source_width}x{source_height})."
        )
    return x, y, width, height


def _label_value(value: str | None, *, field: str) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if any(character in text for character in ("\r", "\n", "\x00")):
        raise ValueError(f"{field} must be a single line.")
    if len(text) > 240:
        raise ValueError(f"{field} is too long for the source label (240 characters max).")
    return text


def _font(size: int, *, bold: bool) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    windows = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates = (
        windows / ("arialbd.ttf" if bold else "arial.ttf"),
        Path(
            "/System/Library/Fonts/Supplemental/"
            + ("Arial Bold.ttf" if bold else "Arial.ttf")
        ),
        Path(
            "/usr/share/fonts/truetype/dejavu/"
            + ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
        ),
    )
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def _label_frame(
    frame: Path,
    labelled: Path,
    *,
    attribution: str,
    date_label: str,
) -> Path:
    try:
        with Image.open(frame) as opened:
            image = opened.convert("RGBA")
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"ffmpeg did not produce a readable source frame at {frame}."
        ) from exc
    if image.size != (FRAME_WIDTH, FRAME_HEIGHT):
        raise RuntimeError(
            f"Extracted source frame is {image.width}x{image.height}; expected "
            f"{FRAME_WIDTH}x{FRAME_HEIGHT}."
        )

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    title_font = _font(28, bold=True)
    date_font = _font(20, bold=False)
    left = 64
    bottom = FRAME_HEIGHT - 52
    padding_x = 24
    padding_y = 17
    line_gap = 7

    lines: list[tuple[str, ImageFont.FreeTypeFont | ImageFont.ImageFont, str]] = []
    if attribution:
        lines.append((attribution, title_font, "#FFFFFF"))
    if date_label:
        lines.append((date_label, date_font, "#BFC3C8"))

    measurements: list[tuple[int, int]] = []
    for text, font, _color in lines:
        bounds = draw.textbbox((0, 0), text, font=font)
        measurements.append((bounds[2] - bounds[0], bounds[3] - bounds[1]))
    content_width = max(width for width, _height in measurements)
    max_content_width = FRAME_WIDTH - left - 128
    if content_width > max_content_width:
        raise ValueError(
            "Source attribution/date label does not fit the 1920x1080 frame."
        )
    content_height = sum(height for _width, height in measurements)
    content_height += line_gap * max(0, len(lines) - 1)
    box_width = content_width + 2 * padding_x
    box_height = content_height + 2 * padding_y
    top = bottom - box_height

    draw.rectangle(
        (left, top, left + box_width, bottom),
        fill=(6, 8, 11, 220),
    )
    draw.rectangle(
        (left, top, left + 6, bottom),
        fill=(255, 185, 0, 255),
    )
    cursor_y = top + padding_y
    for (text, font, color), (_text_width, text_height) in zip(lines, measurements):
        draw.text((left + padding_x, cursor_y), text, font=font, fill=color)
        cursor_y += text_height + line_gap

    labelled.parent.mkdir(parents=True, exist_ok=True)
    Image.alpha_composite(image, overlay).convert("RGB").save(labelled)
    return labelled


def _inspect_frame(frame: Path) -> None:
    try:
        with Image.open(frame) as image:
            image.load()
            size = image.size
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"ffmpeg did not produce a readable source frame at {frame}."
        ) from exc
    if size != (FRAME_WIDTH, FRAME_HEIGHT):
        raise RuntimeError(
            f"Extracted source frame is {size[0]}x{size[1]}; expected "
            f"{FRAME_WIDTH}x{FRAME_HEIGHT}."
        )


def _rate(value: object) -> float | None:
    try:
        parsed = float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _validate_output(
    document: dict,
    *,
    output_name: str,
    frame_count: int,
    fps: int,
) -> None:
    streams = document["streams"]
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(videos) != 1:
        raise RuntimeError(
            f"Derived {output_name} has {len(videos)} video streams; expected exactly one."
        )
    if audios:
        raise RuntimeError(
            f"Derived {output_name} contains an audio stream; screenshot-only "
            "slot assets must be silent."
        )

    stream = videos[0]
    if stream.get("codec_name") != "h264":
        raise RuntimeError(
            f"Derived {output_name} codec is {stream.get('codec_name')!r}; "
            "expected H.264."
        )
    if (stream.get("width"), stream.get("height")) != (FRAME_WIDTH, FRAME_HEIGHT):
        raise RuntimeError(
            f"Derived {output_name} is {stream.get('width')}x{stream.get('height')}; "
            f"expected {FRAME_WIDTH}x{FRAME_HEIGHT}."
        )
    if stream.get("pix_fmt") != "yuv420p":
        raise RuntimeError(
            f"Derived {output_name} pixel format is {stream.get('pix_fmt')!r}; "
            "expected yuv420p for portable Resolve decoding."
        )

    actual_rate = _rate(stream.get("avg_frame_rate"))
    if actual_rate is None:
        actual_rate = _rate(stream.get("r_frame_rate"))
    if actual_rate is None or abs(actual_rate - fps) > 1e-6:
        raise RuntimeError(
            f"Derived {output_name} frame rate is {actual_rate!r}; expected {fps} fps."
        )

    try:
        actual_frames = int(stream["nb_read_frames"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"ffprobe did not count frames in derived {output_name}; refusing "
            "to trust the slot duration."
        ) from exc
    if actual_frames != frame_count:
        raise RuntimeError(
            f"Derived {output_name} contains {actual_frames} frames; expected "
            f"{frame_count}."
        )

    actual_duration = _positive_float(document.get("format", {}).get("duration"))
    if actual_duration is None:
        actual_duration = _positive_float(stream.get("duration"))
    expected_duration = frame_count / fps
    tolerance = max(0.002, 0.25 / fps)
    if actual_duration is None or abs(actual_duration - expected_duration) > tolerance:
        raise RuntimeError(
            f"Derived {output_name} duration is {actual_duration!r}; expected "
            f"{expected_duration:.6f}s (within {tolerance:.6f}s)."
        )


def derive_source_frame_video(
    source_path: Path,
    out_path: Path,
    *,
    timestamp: float,
    duration: float,
    crop: Sequence[int] | None = None,
    attribution: str | None = None,
    date_label: str | None = None,
    fps: int = FRAME_RATE,
    encoder: str = CPU_ENCODER,
    runner: Runner | None = None,
) -> Path:
    """Build one silent 1920x1080 H.264 MP4 from a retained source frame.

    ``timestamp`` is relative to the beginning of the encoded source stream.
    ``crop``, when supplied, is ``[x, y, width, height]`` in *encoded source
    pixels*; rotation metadata is deliberately ignored.  The output contains
    ``ceil(duration * fps)`` frames, so it never ends before the requested slot
    and exceeds it by less than one frame when the slot is off the frame grid.

    Work is written beside ``out_path`` and validated before ``os.replace``.
    A failed extraction, encode, or probe therefore cannot replace an existing
    approved asset with a partial or malformed file.
    """
    source = Path(source_path).resolve()
    output = Path(out_path).resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Retained source video does not exist: {source}")
    if source == output:
        raise ValueError("source_path and out_path must be different files.")
    if output.suffix.lower() != ".mp4":
        raise ValueError("out_path must use the .mp4 extension.")
    if isinstance(fps, bool) or not isinstance(fps, Integral) or fps <= 0:
        raise ValueError(f"fps must be a positive integer, got {fps!r}.")
    fps = int(fps)
    try:
        timestamp = float(timestamp)
        duration = float(duration)
    except (TypeError, ValueError) as exc:
        raise ValueError("timestamp and duration must be numeric seconds.") from exc
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError(f"timestamp must be finite and non-negative, got {timestamp!r}.")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"duration must be finite and positive, got {duration!r}.")
    source_label = _label_value(attribution, field="attribution")
    source_date = _label_value(date_label, field="date_label")

    active_runner = runner or _default_runner
    source_document = _probe(source, runner=active_runner, count_frames=False)
    source_width, source_height, source_duration = _source_video(
        source_document, source
    )
    if timestamp >= source_duration:
        raise ValueError(
            f"timestamp {timestamp:.6f}s falls outside {source.name}, whose "
            f"duration is {source_duration:.6f}s."
        )
    resolved_crop = _normalise_crop(
        crop,
        source_width=source_width,
        source_height=source_height,
    )
    frame_count = max(1, math.ceil(duration * fps - 1e-9))

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output.stem}-derive-",
        dir=output.parent,
    ) as work_value:
        work = Path(work_value)
        extracted = work / "source-frame.png"
        filters: list[str] = []
        if resolved_crop is not None:
            x, y, width, height = resolved_crop
            filters.append(f"crop={width}:{height}:{x}:{y}")
        filters.extend(
            [
                (
                    f"scale={FRAME_WIDTH}:{FRAME_HEIGHT}:"
                    "force_original_aspect_ratio=decrease:flags=lanczos"
                ),
                (
                    f"pad={FRAME_WIDTH}:{FRAME_HEIGHT}:"
                    "(ow-iw)/2:(oh-ih)/2:color=black"
                ),
                "setsar=1",
                "format=rgb24",
            ]
        )
        extract_argv = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-noautorotate",
            "-i",
            str(source),
            # Output seek: decode to the requested timestamp rather than land
            # on an earlier keyframe.
            "-ss",
            f"{timestamp:.6f}",
            "-map",
            "0:v:0",
            "-an",
            "-frames:v",
            "1",
            "-vf",
            ",".join(filters),
            str(extracted),
        ]
        _run_checked(
            extract_argv,
            purpose=f"source-frame extraction from {source.name}",
            runner=active_runner,
        )
        _inspect_frame(extracted)

        encode_frame = extracted
        if source_label or source_date:
            encode_frame = _label_frame(
                extracted,
                work / "labelled-frame.png",
                attribution=source_label,
                date_label=source_date,
            )

        temporary_output = work / "derived.mp4"
        encode_argv = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            str(encode_frame),
            "-map",
            "0:v:0",
            "-an",
            "-frames:v",
            str(frame_count),
            "-r",
            str(fps),
            "-fps_mode",
            "cfr",
            "-vf",
            "setsar=1,format=yuv420p",
            *video_args(ENCODE_QUALITY, encoder=encoder),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-map_metadata",
            "-1",
            str(temporary_output),
        ]
        _run_checked(
            encode_argv,
            purpose=f"source-frame video encode for {output.name}",
            runner=active_runner,
        )
        if not temporary_output.exists() or temporary_output.stat().st_size <= 0:
            raise RuntimeError(
                f"ffmpeg reported success but wrote no derived video for {output.name}."
            )

        output_document = _probe(
            temporary_output,
            runner=active_runner,
            count_frames=True,
        )
        _validate_output(
            output_document,
            output_name=output.name,
            frame_count=frame_count,
            fps=fps,
        )
        os.replace(temporary_output, output)
    return output

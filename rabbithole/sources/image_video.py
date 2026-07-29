"""Derive a portable slot video from a retained local still image.

This module deliberately performs no acquisition and accepts no URL.  It
turns one already retained, single-frame image into an editorial asset:

* decode the source image completely before trusting it;
* optionally crop an explicit rectangle in stored source pixels;
* retain the whole selected region on a 1920x1080 canvas;
* optionally add a restrained source/date label;
* encode a silent, constant-frame-rate H.264/yuv420p MP4; and
* count and validate the completed frames before atomically replacing output.

Pillow does not apply EXIF orientation implicitly, so crop coordinates refer
to the stored source raster on Windows and macOS alike.  Shared presentation
and validation helpers live with :mod:`rabbithole.sources.frame_video` so a
still captured from a video and an independently retained image produce the
same Resolve-friendly asset contract.
"""

from __future__ import annotations

import math
import os
import tempfile
from numbers import Integral
from pathlib import Path
from typing import Sequence

from PIL import Image

from rabbithole.encoding import CPU_ENCODER, video_args
from rabbithole.sources.frame_video import (
    ENCODE_QUALITY,
    FRAME_HEIGHT,
    FRAME_RATE,
    FRAME_WIDTH,
    Runner,
    _default_runner,
    _inspect_frame,
    _label_frame,
    _label_value,
    _normalise_crop,
    _probe,
    _run_checked,
    _validate_output,
)


def _decode_image(source: Path) -> Image.Image:
    """Fully decode one still and return a detached RGBA source raster."""
    try:
        with Image.open(source) as opened:
            frame_total = int(getattr(opened, "n_frames", 1))
            if frame_total != 1:
                raise RuntimeError(
                    f"Source {source.name} contains {frame_total} frames; "
                    "a retained still image must contain exactly one."
                )
            opened.load()
            width, height = opened.size
            if width <= 0 or height <= 0:
                raise RuntimeError(
                    f"Source {source.name} has invalid dimensions "
                    f"{width}x{height}."
                )
            return opened.convert("RGBA")
    except RuntimeError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise RuntimeError(
            f"Retained source image {source.name} could not be fully decoded."
        ) from exc


def _render_frame(
    source: Image.Image,
    frame_path: Path,
    *,
    crop: Sequence[int] | None,
) -> Path:
    resolved_crop = _normalise_crop(
        crop,
        source_width=source.width,
        source_height=source.height,
    )
    selected = source
    if resolved_crop is not None:
        x, y, width, height = resolved_crop
        selected = source.crop((x, y, x + width, y + height))

    factor = min(
        FRAME_WIDTH / selected.width,
        FRAME_HEIGHT / selected.height,
    )
    rendered_width = min(
        FRAME_WIDTH,
        max(1, round(selected.width * factor)),
    )
    rendered_height = min(
        FRAME_HEIGHT,
        max(1, round(selected.height * factor)),
    )
    rendered = selected.resize(
        (rendered_width, rendered_height),
        Image.Resampling.LANCZOS,
    )
    canvas = Image.new("RGBA", (FRAME_WIDTH, FRAME_HEIGHT), (0, 0, 0, 255))
    left = (FRAME_WIDTH - rendered_width) // 2
    top = (FRAME_HEIGHT - rendered_height) // 2
    canvas.alpha_composite(rendered, (left, top))

    frame_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        canvas.convert("RGB").save(frame_path, format="PNG")
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Could not render retained source image to {frame_path.name}."
        ) from exc
    _inspect_frame(frame_path)
    return frame_path


def derive_source_image_video(
    source_path: Path,
    out_path: Path,
    *,
    duration: float,
    crop: Sequence[int] | None = None,
    attribution: str | None = None,
    date_label: str | None = None,
    fps: int = FRAME_RATE,
    encoder: str = CPU_ENCODER,
    runner: Runner | None = None,
) -> Path:
    """Build one silent 1920x1080 H.264 MP4 from a retained still image.

    ``crop``, when supplied, is ``[x, y, width, height]`` in stored source
    pixels.  The output contains ``ceil(duration * fps)`` frames, so it never
    ends before the requested editorial slot and exceeds an off-grid duration
    by less than one frame.

    Work is written beside ``out_path`` and validated before ``os.replace``.
    A failed source decode, encode, or frame-count probe therefore cannot
    replace an existing approved asset with a partial or malformed file.
    """
    source = Path(source_path).resolve()
    output = Path(out_path).resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Retained source image does not exist: {source}")
    if source == output:
        raise ValueError("source_path and out_path must be different files.")
    if output.suffix.lower() != ".mp4":
        raise ValueError("out_path must use the .mp4 extension.")
    if isinstance(fps, bool) or not isinstance(fps, Integral) or fps <= 0:
        raise ValueError(f"fps must be a positive integer, got {fps!r}.")
    fps = int(fps)
    try:
        duration = float(duration)
    except (TypeError, ValueError) as exc:
        raise ValueError("duration must be numeric seconds.") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"duration must be finite and positive, got {duration!r}.")

    source_label = _label_value(attribution, field="attribution")
    source_date = _label_value(date_label, field="date_label")
    decoded = _decode_image(source)
    frame_count = max(1, math.ceil(duration * fps - 1e-9))
    active_runner = runner or _default_runner

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output.stem}-derive-",
        dir=output.parent,
    ) as work_value:
        work = Path(work_value)
        frame = _render_frame(
            decoded,
            work / "source-image.png",
            crop=crop,
        )
        encode_frame = frame
        if source_label or source_date:
            encode_frame = _label_frame(
                frame,
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
            purpose=f"source-image video encode for {output.name}",
            runner=active_runner,
        )
        if not temporary_output.exists() or temporary_output.stat().st_size <= 0:
            raise RuntimeError(
                f"ffmpeg reported success but wrote no derived video for "
                f"{output.name}."
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

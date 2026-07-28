"""Build readable, static evidence shots from verified source captures.

Raw documentary captures are deliberately conservative: a whole PDF page or
browser viewport is retained so capture never destroys information.  That is
the correct ingest policy and often a poor editorial frame; a 1241x1754 notice
letterboxed at 1080p is technically present but its body copy is unreadable.

This module performs the separate, authored step:

* crop only a declared region of a retained source image;
* place it on a 16:9 evidence canvas without inventing source text;
* optionally combine authentic regions (for example masthead + paragraph);
* allow restrained amber outlines on dark social posts;
* concatenate static frames with hard cuts at declared narration beats.

The instructions live in a project JSON file, so every crop and internal cut is
reviewable.  Article highlights are intentionally *not* painted here; they are
timed later by :mod:`rabbithole.highlights`, where the amber matte is multiplied
under the source's black type.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from rabbithole.slots import build_slots
from rabbithole.sources.capture import still_to_video

FRAME_WIDTH = 1920
FRAME_HEIGHT = 1080
FRAME_RATE = 30
DEFAULT_BACKGROUND = "#090A0B"
DEFAULT_ACCENT = "#FFB900"
_COVERAGE_TOLERANCE = 0.002


@dataclass(frozen=True)
class EvidenceBuild:
    slot_id: str
    output: Path
    frames: tuple[Path, ...]
    duration: float


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        Path(r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
             if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def _box(value: Any, *, field: str) -> tuple[int, int, int, int]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{field} must be [x, y, width, height].")
    x, y, width, height = (int(round(float(item))) for item in value)
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise ValueError(f"{field} contains an invalid box: {value!r}.")
    return x, y, width, height


def _resolve_image(project_root: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = project_root / path
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"Evidence layer image does not exist: {path}")
    return path


def _paste_layer(
    canvas: Image.Image,
    project_root: Path,
    layer: dict,
) -> None:
    source_path = _resolve_image(project_root, str(layer["image"]))
    with Image.open(source_path) as opened:
        source = opened.convert("RGB")
        crop_x, crop_y, crop_w, crop_h = _box(layer["crop"], field="layer crop")
        if crop_x + crop_w > source.width or crop_y + crop_h > source.height:
            raise ValueError(
                f"Layer crop {layer['crop']!r} exceeds {source_path.name} "
                f"({source.width}x{source.height})."
            )
        region = source.crop((crop_x, crop_y, crop_x + crop_w, crop_y + crop_h))

    target_x, target_y, target_w, target_h = _box(
        layer["target"], field="layer target"
    )
    if target_x + target_w > canvas.width or target_y + target_h > canvas.height:
        raise ValueError(
            f"Layer target {layer['target']!r} exceeds the evidence frame "
            f"({canvas.width}x{canvas.height})."
        )

    fit = str(layer.get("fit", "contain")).lower()
    if fit == "stretch":
        rendered = region.resize((target_w, target_h), Image.Resampling.LANCZOS)
        paste_x, paste_y = target_x, target_y
    else:
        if fit not in {"contain", "cover"}:
            raise ValueError(f"Unknown evidence layer fit {fit!r}.")
        factor = (
            min(target_w / region.width, target_h / region.height)
            if fit == "contain"
            else max(target_w / region.width, target_h / region.height)
        )
        rendered = region.resize(
            (
                max(1, round(region.width * factor)),
                max(1, round(region.height * factor)),
            ),
            Image.Resampling.LANCZOS,
        )
        if fit == "cover":
            left = max(0, (rendered.width - target_w) // 2)
            top = max(0, (rendered.height - target_h) // 2)
            rendered = rendered.crop((left, top, left + target_w, top + target_h))
            paste_x, paste_y = target_x, target_y
        else:
            paste_x = target_x + (target_w - rendered.width) // 2
            paste_y = target_y + (target_h - rendered.height) // 2

    canvas.paste(rendered, (paste_x, paste_y))


def render_evidence_frame(
    project_root: Path,
    frame: dict,
    out_path: Path,
    *,
    width: int = FRAME_WIDTH,
    height: int = FRAME_HEIGHT,
) -> Path:
    """Render one declared evidence frame to a PNG."""
    background = str(frame.get("background", DEFAULT_BACKGROUND))
    canvas = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(canvas)

    for panel in frame.get("panels", []):
        x, y, panel_w, panel_h = _box(panel, field="panel")
        # Square paper edges match the reference treatment and avoid making an
        # official document look like a floating UI card.
        draw.rectangle(
            (x + 12, y + 14, x + panel_w + 12, y + panel_h + 14),
            fill="#000000",
        )
        draw.rectangle((x, y, x + panel_w, y + panel_h), fill="#FFFFFF")

    for layer in frame.get("layers", []):
        _paste_layer(canvas, Path(project_root), layer)

    accent = str(frame.get("accent", DEFAULT_ACCENT))
    for line in frame.get("lines", []):
        points = line.get("points", [])
        if not isinstance(points, list) or len(points) < 2:
            raise ValueError("Evidence line needs at least two [x, y] points.")
        parsed_points = [(int(point[0]), int(point[1])) for point in points]
        draw.line(
            parsed_points,
            fill=str(line.get("color", accent)),
            width=max(1, int(line.get("width", 5))),
            joint="curve",
        )

    for marker in frame.get("markers", []):
        x = int(marker["x"])
        y = int(marker["y"])
        radius = max(3, int(marker.get("radius", 14)))
        color = str(marker.get("color", accent))
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill="#090A0B",
            outline=color,
            width=max(2, int(marker.get("width", 5))),
        )
        label = str(marker.get("label", "")).strip()
        if label:
            label_x = int(marker.get("label_x", x + radius + 14))
            label_y = int(marker.get("label_y", y - 18))
            font = _font(int(marker.get("size", 24)), bold=True)
            bounds = draw.textbbox((label_x, label_y), label, font=font)
            pad = 8
            draw.rectangle(
                (
                    bounds[0] - pad,
                    bounds[1] - pad,
                    bounds[2] + pad,
                    bounds[3] + pad,
                ),
                fill=str(marker.get("label_background", "#090A0B")),
            )
            draw.text((label_x, label_y), label, font=font, fill=color)

    for text in frame.get("texts", []):
        value = str(text["text"])
        x = int(text["x"])
        y = int(text["y"])
        font = _font(int(text.get("size", 22)), bold=bool(text.get("bold", False)))
        bounds = draw.textbbox((x, y), value, font=font)
        background = text.get("background")
        if background:
            pad = int(text.get("padding", 10))
            draw.rectangle(
                (
                    bounds[0] - pad,
                    bounds[1] - pad,
                    bounds[2] + pad,
                    bounds[3] + pad,
                ),
                fill=str(background),
            )
        draw.text((x, y), value, font=font, fill=str(text.get("color", "#FFFFFF")))

    for outline in frame.get("outlines", []):
        x, y, box_w, box_h = _box(outline["rect"], field="outline rect")
        line_width = int(outline.get("width", 5))
        color = str(outline.get("color", accent))
        draw.rectangle(
            (x, y, x + box_w, y + box_h),
            outline=color,
            width=max(1, line_width),
        )

    source_label = str(frame.get("source_label", "")).strip()
    if source_label:
        label_x = int(frame.get("source_label_x", 142))
        label_y = int(frame.get("source_label_y", 34))
        draw.rectangle((label_x, label_y + 2, label_x + 8, label_y + 26), fill=accent)
        draw.text(
            (label_x + 22, label_y),
            source_label.upper(),
            font=_font(22, bold=True),
            fill=str(frame.get("source_label_color", "#D4D4D4")),
        )

    note = str(frame.get("note", "")).strip()
    if note:
        draw.text(
            (int(frame.get("note_x", 142)), int(frame.get("note_y", height - 42))),
            note,
            font=_font(16),
            fill=str(frame.get("note_color", "#8E8E8E")),
        )

    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return out_path


def _validate_frames(slot_id: str, frames: list[dict], duration: float) -> None:
    if not frames:
        raise ValueError(f"Evidence slot {slot_id} has no frames.")
    cursor = 0.0
    for index, frame in enumerate(frames):
        start = float(frame["start"])
        end = float(frame["end"])
        if end <= start:
            raise ValueError(
                f"Evidence slot {slot_id} frame {index} ends before it starts."
            )
        if abs(start - cursor) > _COVERAGE_TOLERANCE:
            raise ValueError(
                f"Evidence slot {slot_id} has a gap/overlap at {start:.3f}s; "
                f"expected {cursor:.3f}s."
            )
        cursor = end
    if abs(cursor - duration) > _COVERAGE_TOLERANCE:
        raise ValueError(
            f"Evidence slot {slot_id} frames end at {cursor:.3f}s, but the "
            f"timing slot lasts {duration:.3f}s."
        )


def _concat_clips(clips: list[Path], manifest: Path, output: Path) -> Path:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "".join(f"file '{path.resolve().as_posix()}'\n" for path in clips),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(manifest),
            "-c",
            "copy",
            str(output),
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-800:]
        output.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed concatenating evidence frames: {detail}")
    return output


def build_evidence_assets(
    timing_path: Path,
    spec_path: Path,
) -> list[EvidenceBuild]:
    """Build every evidence slot declared in ``spec_path``."""
    timing_path = Path(timing_path).resolve()
    spec_path = Path(spec_path).resolve()
    project_root = timing_path.parent.parent
    document = json.loads(timing_path.read_text(encoding="utf-8"))
    slots = {slot.slot_id: slot for slot in build_slots(document)}
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    frame_root = project_root / "assets" / "evidence" / "frames"
    work_root = project_root / "assets" / "evidence" / "work"
    results: list[EvidenceBuild] = []

    for slot_id, slot_spec in spec.get("slots", {}).items():
        if slot_id not in slots:
            raise ValueError(f"Evidence spec names unknown timing slot {slot_id!r}.")
        slot = slots[slot_id]
        duration = slot.end - slot.start
        frames = list(slot_spec.get("frames", []))
        _validate_frames(slot_id, frames, duration)

        rendered_frames: list[Path] = []
        clips: list[Path] = []
        for index, frame in enumerate(frames, start=1):
            still = render_evidence_frame(
                project_root,
                frame,
                frame_root / f"{slot_id}-{index:02d}.png",
            )
            clip = work_root / f"{slot_id}-{index:02d}.mp4"
            still_to_video(
                still,
                clip,
                float(frame["end"]) - float(frame["start"]),
                width=FRAME_WIDTH,
                height=FRAME_HEIGHT,
                fps=FRAME_RATE,
            )
            rendered_frames.append(still)
            clips.append(clip)

        output_value = slot_spec.get(
            "output", f"assets/{slot_id}-capture.mp4"
        )
        output = (project_root / output_value).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        if len(clips) == 1:
            # Encode directly to the canonical path so there is no needless
            # second lossless-looking-but-lossy H.264 generation.
            still_to_video(
                rendered_frames[0],
                output,
                duration,
                width=FRAME_WIDTH,
                height=FRAME_HEIGHT,
                fps=FRAME_RATE,
            )
        else:
            _concat_clips(
                clips,
                work_root / f"{slot_id}-concat.txt",
                output,
            )
        results.append(
            EvidenceBuild(
                slot_id=slot_id,
                output=output,
                frames=tuple(rendered_frames),
                duration=duration,
            )
        )

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build readable evidence-shot assets from an authored crop spec."
    )
    parser.add_argument("timing_json", type=Path)
    parser.add_argument("spec_json", type=Path)
    args = parser.parse_args(argv)
    for result in build_evidence_assets(args.timing_json, args.spec_json):
        print(
            f"{result.slot_id}: {len(result.frames)} frame(s), "
            f"{result.duration:.3f}s -> {result.output}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI surface.
    raise SystemExit(main())

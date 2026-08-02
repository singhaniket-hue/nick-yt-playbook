"""Paginated visual QA sheets from an episode's timing and provenance.

This is deliberately a review-only tool.  It reads the timing spine and
provenance ledger, samples the already acquired local media, and writes labelled
PNG sheets.  It never downloads, edits, or replaces source media and it never
talks to Resolve.

Run it directly without adding another command to the main CLI::

    python -m rabbithole.contactsheet \
        projects/example/narration/timing.json \
        --output projects/example/review/contact-sheets

The provenance path defaults to ``<project>/provenance.json``.  Graphic/plate
slots and evidence slots are paginated separately, while both retain the exact
timeline order from ``timing.json``.  Missing or unreadable assets become visible
error cards instead of aborting the rest of the review sheet.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from io import BytesIO
import json
import math
from pathlib import Path
import subprocess
import sys
from typing import Callable, Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps

from rabbithole.jsonio import read_json
from rabbithole.provenance import AssetRecord, load_provenance
from rabbithole.slots import Slot, build_slots


SCHEMA_VERSION = "rabbithole-contact-sheets.v1"
GROUPS = ("graphics", "evidence")
GRAPHIC_KINDS = frozenset({"graphic", "plate"})
STILL_SUFFIXES = frozenset(
    {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
)

DEFAULT_COLUMNS = 4
DEFAULT_ROWS = 4
DEFAULT_CELL_WIDTH = 400
DEFAULT_SAMPLE_SECONDS = 0.5

_PAGE_MARGIN = 24
_PAGE_HEADER_HEIGHT = 76
_CELL_GAP = 18
_LABEL_HEIGHT = 94
_VIDEO_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class ReviewItem:
    """One timeline slot/asset pairing shown on a contact sheet."""

    timeline_order: int
    group: str
    slot: Slot
    asset: AssetRecord | None

    @property
    def asset_id(self) -> str:
        return self.asset.asset_id if self.asset is not None else ""


@dataclass(frozen=True)
class SheetIssue:
    """An item whose preview could not be rendered."""

    group: str
    slot_id: str
    asset_id: str
    message: str


@dataclass(frozen=True)
class ContactSheetResult:
    """Files and non-fatal preview failures produced by one run."""

    output_dir: Path
    pages: dict[str, tuple[Path, ...]]
    item_counts: dict[str, int]
    issues: tuple[SheetIssue, ...]
    manifest_path: Path


FrameLoader = Callable[[ReviewItem, Path, tuple[int, int], float], Image.Image]


def review_group(slot_kind: str) -> str:
    """Map a timing slot kind to one of the two editorial QA groups."""

    return "graphics" if slot_kind.lower() in GRAPHIC_KINDS else "evidence"


def review_kind(item: ReviewItem) -> str:
    """Name what the reviewer is actually seeing, not only the slot request.

    A screenshot slot can legitimately fall back to an authored citation card
    after a consent wall or dynamic UI blocks source pixels.  Calling that
    output a screenshot in the QA sheet obscures an important editorial
    distinction, so provenance-backed derived forms receive explicit labels.
    """
    if item.asset is not None:
        provider = item.asset.provider.casefold()
        if provider == "rabbithole-evidence-card":
            return "CITATION CARD"
        if provider == "rabbithole-source-text-extract":
            return "SOURCE-TEXT EXTRACT"
        if provider == "rabbithole-source-frame":
            return "SOURCE FRAME"
        if provider == "rabbithole-source-image":
            return "SOURCE IMAGE"
    return item.slot.kind.upper()


def plan_review_items(
    document: dict,
    records: Sequence[AssetRecord],
) -> list[ReviewItem]:
    """Pair provenance records to timing slots in deterministic timeline order.

    A missing claimant is retained as an item with ``asset=None`` so the review
    sheet makes the hole obvious.  If an invalid ledger claims one slot more than
    once, every claimant is shown, ordered by asset id/path; the provenance gate
    remains responsible for rejecting the duplicate.
    """

    records_by_slot: dict[str, list[AssetRecord]] = {}
    for record in records:
        for slot_id in record.used_in_slots:
            records_by_slot.setdefault(slot_id, []).append(record)

    items: list[ReviewItem] = []
    for timeline_order, slot in enumerate(build_slots(document)):
        claimants = sorted(
            records_by_slot.get(slot.slot_id, ()),
            key=lambda record: (
                record.asset_id.casefold(),
                record.local_path.casefold(),
            ),
        )
        if not claimants:
            claimants = [None]
        for record in claimants:
            items.append(
                ReviewItem(
                    timeline_order=timeline_order,
                    group=review_group(slot.kind),
                    slot=slot,
                    asset=record,
                )
            )
    return items


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    """A portable UI font with a deterministic local fallback."""

    names = (
        (
            Path(r"C:\Windows\Fonts\segoeuib.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        )
        if bold
        else (
            Path(r"C:\Windows\Fonts\segoeui.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        )
    )
    for candidate in names:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def _time_label(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    minutes, remainder = divmod(seconds, 60.0)
    return f"{int(minutes):02d}:{remainder:04.1f}"


def _elide(
    draw: ImageDraw.ImageDraw,
    value: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    """Truncate a label by rendered width rather than character count."""

    value = " ".join(str(value).split())
    if draw.textlength(value, font=font) <= max_width:
        return value
    suffix = "..."
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = value[:middle].rstrip() + suffix
        if draw.textlength(candidate, font=font) <= max_width:
            low = middle
        else:
            high = middle - 1
    return value[:low].rstrip() + suffix


def resolve_media_path(item: ReviewItem, project_root: Path) -> Path | None:
    """Resolve portable provenance paths without changing the ledger."""

    if item.asset is None or not item.asset.local_path.strip():
        return None
    path = Path(item.asset.local_path).expanduser()
    if not path.is_absolute():
        path = Path(project_root) / path
    return path.resolve(strict=False)


def _read_still(path: Path) -> Image.Image:
    with Image.open(path) as opened:
        return ImageOps.exif_transpose(opened).convert("RGB")


def _ffmpeg_frame(path: Path, sample_seconds: float) -> Image.Image:
    """Read one video frame through stdout, leaving no temporary frame files."""

    attempts = (sample_seconds, 0.0) if sample_seconds > 0 else (0.0,)
    last_error = ""
    for at in attempts:
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-ss",
                    f"{at:.3f}",
                    "-i",
                    str(path),
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    "1",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "png",
                    "pipe:1",
                ],
                capture_output=True,
                timeout=_VIDEO_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "ffmpeg is required to preview video assets but was not found"
            ) from exc
        except subprocess.TimeoutExpired:
            last_error = (
                f"ffmpeg timed out after {_VIDEO_TIMEOUT_SECONDS}s at {at:.3f}s"
            )
            continue

        if result.returncode == 0 and result.stdout:
            try:
                with Image.open(BytesIO(result.stdout)) as opened:
                    return opened.convert("RGB")
            except OSError as exc:
                last_error = f"ffmpeg returned an unreadable frame: {exc}"
                continue
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        last_error = stderr.splitlines()[-1] if stderr else "ffmpeg returned no frame"

    raise RuntimeError(last_error)


def load_preview(
    item: ReviewItem,
    project_root: Path,
    frame_size: tuple[int, int],
    sample_seconds: float,
) -> Image.Image:
    """Load and letterbox a source still or sampled video frame."""

    path = resolve_media_path(item, project_root)
    if path is None:
        raise FileNotFoundError("no provenance asset claims this slot")
    if not path.is_file():
        local_path = item.asset.local_path if item.asset is not None else ""
        raise FileNotFoundError(f"local media is missing: {local_path}")

    if path.suffix.lower() in STILL_SUFFIXES:
        frame = _read_still(path)
    else:
        frame = _ffmpeg_frame(path, sample_seconds)

    fitted = ImageOps.contain(frame, frame_size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", frame_size, "#050607")
    canvas.paste(
        fitted,
        ((frame_size[0] - fitted.width) // 2, (frame_size[1] - fitted.height) // 2),
    )
    return canvas


def _error_preview(
    frame_size: tuple[int, int],
    heading: str,
    detail: str,
) -> Image.Image:
    image = Image.new("RGB", frame_size, "#2B1519")
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (1, 1, frame_size[0] - 2, frame_size[1] - 2),
        outline="#E55757",
        width=4,
    )
    heading_font = _font(24, bold=True)
    detail_font = _font(15)
    max_width = frame_size[0] - 36
    draw.text(
        (18, frame_size[1] // 2 - 30),
        _elide(draw, heading, heading_font, max_width),
        fill="#FF8A8A",
        font=heading_font,
    )
    draw.text(
        (18, frame_size[1] // 2 + 8),
        _elide(draw, detail, detail_font, max_width),
        fill="#E5BFC4",
        font=detail_font,
    )
    return image


def _render_cell(
    page: Image.Image,
    item: ReviewItem,
    *,
    x: int,
    y: int,
    cell_width: int,
    project_root: Path,
    sample_seconds: float,
    frame_loader: FrameLoader,
) -> SheetIssue | None:
    draw = ImageDraw.Draw(page)
    frame_size = (cell_width, round(cell_width * 9 / 16))
    issue: SheetIssue | None = None
    try:
        preview = frame_loader(item, project_root, frame_size, sample_seconds)
        if preview.size != frame_size:
            preview = ImageOps.fit(
                preview.convert("RGB"), frame_size, method=Image.Resampling.LANCZOS
            )
        status_color = "#5CCB84"
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        preview = _error_preview(
            frame_size,
            "MISSING" if item.asset is None else "UNREADABLE",
            message,
        )
        status_color = "#E55757"
        issue = SheetIssue(
            group=item.group,
            slot_id=item.slot.slot_id,
            asset_id=item.asset_id,
            message=message,
        )

    page.paste(preview, (x, y))
    draw.rectangle(
        (x, y, x + frame_size[0] - 1, y + frame_size[1] - 1),
        outline=status_color,
        width=3,
    )

    title_font = _font(18, bold=True)
    body_font = _font(15)
    small_font = _font(13)
    label_y = y + frame_size[1] + 9
    max_width = cell_width
    title = (
        f"{item.slot.slot_id}  {review_kind(item)}  "
        f"{_time_label(item.slot.start)}-{_time_label(item.slot.end)}"
    )
    draw.text(
        (x, label_y),
        _elide(draw, title, title_font, max_width),
        fill="#F5F6F7",
        font=title_font,
    )
    draw.text(
        (x, label_y + 26),
        _elide(draw, item.slot.detail or "(no detail)", body_font, max_width),
        fill="#C7CBD0",
        font=body_font,
    )

    if item.asset is None:
        source = "NO PROVENANCE RECORD"
    else:
        filename = Path(item.asset.local_path).name or "(no local path)"
        source = f"{item.asset.asset_id} | {item.asset.provider} | {filename}"
    draw.text(
        (x, label_y + 50),
        _elide(draw, source, small_font, max_width),
        fill=status_color,
        font=small_font,
    )
    return issue


def _page_geometry(
    *,
    columns: int,
    rows: int,
    cell_width: int,
) -> tuple[int, int, int]:
    thumb_height = round(cell_width * 9 / 16)
    cell_height = thumb_height + _LABEL_HEIGHT
    page_width = (
        _PAGE_MARGIN * 2 + columns * cell_width + max(0, columns - 1) * _CELL_GAP
    )
    page_height = (
        _PAGE_HEADER_HEIGHT
        + _PAGE_MARGIN
        + rows * cell_height
        + max(0, rows - 1) * _CELL_GAP
        + _PAGE_MARGIN
    )
    return page_width, page_height, cell_height


def _validate_layout(columns: int, rows: int, cell_width: int) -> None:
    if columns <= 0 or rows <= 0:
        raise ValueError("columns and rows must be positive")
    if cell_width < 240:
        raise ValueError("cell width must be at least 240 pixels")


def _ordered(items: Iterable[ReviewItem]) -> list[ReviewItem]:
    return sorted(
        items,
        key=lambda item: (
            item.timeline_order,
            item.slot.slot_id.casefold(),
            item.asset_id.casefold(),
        ),
    )


def render_contact_sheets(
    items: Sequence[ReviewItem],
    output_dir: Path,
    *,
    project_root: Path,
    episode_label: str = "",
    columns: int = DEFAULT_COLUMNS,
    rows: int = DEFAULT_ROWS,
    cell_width: int = DEFAULT_CELL_WIDTH,
    sample_seconds: float = DEFAULT_SAMPLE_SECONDS,
    frame_loader: FrameLoader = load_preview,
) -> ContactSheetResult:
    """Render deterministic, separately paginated graphics/evidence sheets."""

    _validate_layout(columns, rows, cell_width)
    if sample_seconds < 0:
        raise ValueError("sample seconds cannot be negative")

    output_dir = Path(output_dir).resolve()
    project_root = Path(project_root).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    per_page = columns * rows
    page_width, page_height, cell_height = _page_geometry(
        columns=columns, rows=rows, cell_width=cell_width
    )

    pages: dict[str, tuple[Path, ...]] = {}
    item_counts: dict[str, int] = {}
    issues: list[SheetIssue] = []
    for group in GROUPS:
        group_items = _ordered(item for item in items if item.group == group)
        item_counts[group] = len(group_items)
        page_count = math.ceil(len(group_items) / per_page)
        group_pages: list[Path] = []
        for page_index in range(page_count):
            start = page_index * per_page
            page_items = group_items[start : start + per_page]
            page = Image.new("RGB", (page_width, page_height), "#101214")
            draw = ImageDraw.Draw(page)

            title_font = _font(28, bold=True)
            meta_font = _font(16)
            prefix = f"{episode_label} / " if episode_label else ""
            draw.text(
                (_PAGE_MARGIN, 20),
                f"{prefix}{group.upper()} QA",
                fill="#F4F5F6",
                font=title_font,
            )
            range_start = start + 1
            range_end = start + len(page_items)
            metadata = (
                f"{range_start:03d}-{range_end:03d} of {len(group_items):03d}  |  "
                f"page {page_index + 1:02d}/{page_count:02d}"
            )
            meta_width = draw.textlength(metadata, font=meta_font)
            draw.text(
                (page_width - _PAGE_MARGIN - meta_width, 31),
                metadata,
                fill="#9AA0A6",
                font=meta_font,
            )
            draw.line(
                (
                    _PAGE_MARGIN,
                    _PAGE_HEADER_HEIGHT - 8,
                    page_width - _PAGE_MARGIN,
                    _PAGE_HEADER_HEIGHT - 8,
                ),
                fill="#303438",
                width=2,
            )

            for item_index, item in enumerate(page_items):
                row, column = divmod(item_index, columns)
                x = _PAGE_MARGIN + column * (cell_width + _CELL_GAP)
                y = _PAGE_HEADER_HEIGHT + _PAGE_MARGIN + row * (
                    cell_height + _CELL_GAP
                )
                issue = _render_cell(
                    page,
                    item,
                    x=x,
                    y=y,
                    cell_width=cell_width,
                    project_root=project_root,
                    sample_seconds=sample_seconds,
                    frame_loader=frame_loader,
                )
                if issue is not None:
                    issues.append(issue)

            page_path = output_dir / f"{group}-{page_index + 1:03d}.png"
            page.save(page_path, format="PNG", compress_level=6)
            group_pages.append(page_path)
        pages[group] = tuple(group_pages)

    manifest_path = output_dir / "contact-sheets.json"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "episode": episode_label,
        "groups": {
            group: {
                "item_count": item_counts[group],
                "pages": [path.name for path in pages[group]],
            }
            for group in GROUPS
        },
        "issues": [asdict(issue) for issue in issues],
    }
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return ContactSheetResult(
        output_dir=output_dir,
        pages=pages,
        item_counts=item_counts,
        issues=tuple(issues),
        manifest_path=manifest_path,
    )


def _project_root_for(timing_path: Path, provenance_path: Path | None) -> Path:
    if provenance_path is not None:
        return provenance_path.parent
    if timing_path.parent.name.casefold() == "narration":
        return timing_path.parent.parent
    return timing_path.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rabbithole.contactsheet",
        description=(
            "Build labelled, paginated graphics and evidence QA sheets from "
            "existing episode media."
        ),
    )
    parser.add_argument("timing_json", type=Path)
    parser.add_argument(
        "--provenance",
        type=Path,
        help="Provenance ledger (default: <project>/provenance.json).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output directory (default: <project>/review/contact-sheets).",
    )
    parser.add_argument("--columns", type=int, default=DEFAULT_COLUMNS)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--cell-width", type=int, default=DEFAULT_CELL_WIDTH)
    parser.add_argument(
        "--sample-seconds",
        type=float,
        default=DEFAULT_SAMPLE_SECONDS,
        help="Video frame sample position; a failed seek retries at 0 seconds.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    timing_path = args.timing_json.expanduser().resolve(strict=False)
    supplied_provenance = (
        args.provenance.expanduser().resolve(strict=False)
        if args.provenance is not None
        else None
    )
    project_root = _project_root_for(timing_path, supplied_provenance)
    provenance_path = supplied_provenance or project_root / "provenance.json"
    output_dir = args.output or project_root / "review" / "contact-sheets"

    try:
        if not timing_path.is_file():
            raise FileNotFoundError(f"timing document does not exist: {timing_path}")
        if not provenance_path.is_file():
            raise FileNotFoundError(
                f"provenance ledger does not exist: {provenance_path}"
            )
        document = read_json(timing_path)
        if not isinstance(document, dict):
            raise ValueError("timing document must be a JSON object")
        records = load_provenance(provenance_path)
        items = plan_review_items(document, records)
        if not items:
            raise ValueError("timing document contains no SHOT slots to review")
        result = render_contact_sheets(
            items,
            output_dir,
            project_root=project_root,
            episode_label=project_root.name,
            columns=args.columns,
            rows=args.rows,
            cell_width=args.cell_width,
            sample_seconds=args.sample_seconds,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Contact-sheet QA failed: {exc}", file=sys.stderr)
        return 2

    print(f"Contact sheets: {result.output_dir}")
    for group in GROUPS:
        print(
            f"  {group}: {result.item_counts[group]} item(s), "
            f"{len(result.pages[group])} page(s)"
        )
    print(f"Manifest: {result.manifest_path}")
    if result.issues:
        print(f"QA issues: {len(result.issues)} (shown as red cards)")
        return 1
    print("QA issues: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

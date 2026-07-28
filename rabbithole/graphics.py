"""Chapter cards and censor boxes: timed graphics composited onto rendered footage.

Design decision: ffmpeg, not Remotion
--------------------------------------
Plan 2 specified Remotion for this graphics layer. We are deliberately using
ffmpeg instead. Every other stage in this pipeline -- plates, framing, the
grade, subtitle burn-in -- is already ffmpeg; it all works, and adding a
Node/Remotion dependency and a second rendering runtime to draw a handful of
title cards and blurred rectangles is not worth the operational cost. Chapter
cards reuse exactly the same mechanism `subtitles.py` already proved works
for burned-in text: an ASS document rendered by libass via ffmpeg's `ass=`
filter, which handles typography (font shaping, colour, alignment) for us.
Censor boxes are a `crop` + `boxblur` + `colorchannelmixer` + `overlay` filter
chain, again ordinary ffmpeg.

Remotion stays the right tool for genuinely complex motion graphics later --
multi-layer animation, keyframed camera moves synced to beats, anything that
benefits from a real component model and a timeline. This module does not
attempt that; it draws static cards and static censor regions, which ffmpeg
filters do natively without needing a browser-based renderer in the loop.

Two traps this module is written to avoid (see `subtitles.burn` and
`render.finish` for the same fights fought earlier in this project):

- ffmpeg filter paths containing ':' collide with the filtergraph parser's
  own use of ':' to separate filter options -- a bare Windows drive letter
  (`C:\\...`) breaks `ass=`. `draw_graphics` reuses `subtitles.burn` for the
  ASS-driven card stage, which already runs ffmpeg with `cwd` set to the
  ASS file's own directory and passes a bare relative filename -- exactly
  the fix proven there.
- A filter that renders but draws nothing looks like success (ffmpeg exits 0
  either way). Every visual feature here is proven with a real ffmpeg render
  and a pixel comparison in tests/test_graphics.py, not just a clean exit
  code.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from rabbithole.encoding import video_args
from rabbithole.overlays import Overlay
from rabbithole.render import _probe_video_dims
from rabbithole.sources.plates import PLATE_BUFSIZE, PLATE_CRF, PLATE_MAXRATE
from rabbithole.subtitles import (
    _escape_ass_text,
    _installed_font_families,
    _wrap_override,
    ass_colour,
    burn,
    format_ass_timestamp,
)
from rabbithole.validate import Finding

# =============================================================================
# Part A: title font selection
# =============================================================================

# In priority order: the first of these actually installed on the machine is
# used. `style/typography.json` names "Bebas Neue" for title cards, but Bebas
# Neue is almost certainly not installed on this machine (confirmed directly
# via `subtitles._installed_font_families()`: it is absent). The candidates
# below are condensed or heavy-weight display faces that read as "title card",
# not body text -- the same spirit as Bebas Neue (tall, bold, built for a
# handful of uppercase words) -- falling back through progressively more
# generic Windows system fonts so something legible always renders. Bahnschrift
# and Impact are both installed on this Windows machine (confirmed via the
# same registry read `_installed_font_families` uses); Arial Black is a
# near-universal Windows font used as the last resort before the generic
# 'sans-serif' fallback `pick_title_font` returns when nothing on this list
# is present at all.
TITLE_FONT_CANDIDATES = (
    "Bebas Neue",
    "Anton",
    "Oswald",
    "Avenir Next Condensed",
    "Helvetica Neue",
    "Bahnschrift",
    "Impact",
    "Arial Black",
)


def pick_title_font(typography: dict, available: set[str] | None = None) -> tuple[str, list[Finding]]:
    """Choose an available font for title cards.

    Mirrors `subtitles.pick_font`'s shape (same override-and-warn contract)
    but solves a different problem: `pick_font` hunts for Devanagari script
    coverage, this hunts for a condensed/display face standing in for Bebas
    Neue. Returns the first of `TITLE_FONT_CANDIDATES` actually installed
    (via `available`, defaulting to `subtitles._installed_font_families()`
    for real detection -- reused rather than re-reading the font registry a
    second way). If the chosen font differs from `typography.json`'s
    requested `title_card.family`, a `severity="warning"` Finding says so.

    If nothing on the candidate list is installed, falls back to
    'sans-serif' with a `severity="error"` Finding: a chapter card in a
    generic font is still a chapter card, but it is worth flagging loudly
    rather than silently rendering in whatever ffmpeg/libass happens to
    default to.
    """
    if available is None:
        available = _installed_font_families()

    requested = typography.get("title_card", {}).get("family", "")
    findings: list[Finding] = []

    for candidate in TITLE_FONT_CANDIDATES:
        if candidate in available:
            if candidate != requested:
                findings.append(
                    Finding(
                        gate="graphics",
                        severity="warning",
                        message=(
                            f"typography.json's title_card family {requested!r} is not "
                            f"installed on this machine; overridden with {candidate!r}, the "
                            f"first condensed/display fallback found installed."
                        ),
                    )
                )
            return candidate, findings

    findings.append(
        Finding(
            gate="graphics",
            severity="error",
            message=(
                f"None of {', '.join(TITLE_FONT_CANDIDATES)} is installed on this machine; "
                f"falling back to 'sans-serif' for chapter cards."
            ),
        )
    )
    return "sans-serif", findings


# =============================================================================
# Part B: chapter cards (ASS)
# =============================================================================

# As a fraction of frame height -- large enough to read as a title card at a
# glance, distinct from `subtitles.py`'s much smaller `_FONT_SIZE_FRACTION`
# (0.045) used for lower-third dialogue text.
_TITLE_FONT_SIZE_FRACTION = 0.09
_BOLD_WEIGHT_THRESHOLD = 600  # typography.json weights >= this render bold

# Numpad-style ASS \an alignment code for middle-center. Deliberately NOT
# `subtitles._ASS_ALIGNMENT_BOTTOM_CENTER` (2): chapter cards must sit
# centre-frame, visibly distinct from the lower-third subtitle placement, so
# the two never collide on screen even if their time windows overlap.
_ASS_ALIGNMENT_MIDDLE_CENTER = 5

# A run of space characters, underlined, forms a plain horizontal rule below
# the title -- ASS's own \u (underline) override draws under the run's full
# advance width, spaces included, so this needs no vector drawing. The count
# is fixed rather than derived from title length: a stable rule width reads
# as an intentional design element regardless of how long any given chapter
# title is.
_TITLE_RULE_SPACE_COUNT = 14


def chapter_card_ass(
    overlays: list[Overlay], typography: dict, palette: dict, width: int = 1920, height: int = 1080
) -> str:
    """An ASS document drawing the chapter cards.

    One `Dialogue` line per `kind == "chapter-card"` overlay, in the order
    given; every other overlay kind (`censor`, `keyword`) is ignored here --
    this module's `censor_box_filter` handles censor boxes separately, via a
    completely different mechanism (an ffmpeg filter chain, not ASS), and
    keyword overlays are `subtitles.py`'s concern, not this module's.

    Each card's text is the overlay's `text` (the chapter title), uppercased
    when `typography.json`'s `title_card.transform` is `"uppercase"`, styled
    in `palette`'s `text_light` with a thin `accent_red` underline rule on
    the line below -- a plain run of underlined spaces, coloured via the
    same inline `\\c` override `subtitles._cue_text` uses (reusing
    `subtitles._wrap_override`/`ass_colour` rather than re-deriving the BGR
    packing). Centred at `_ASS_ALIGNMENT_MIDDLE_CENTER`, distinctly
    different from subtitle placement, so a card and a subtitle cue can never
    visually collide even if their time windows overlap.
    """
    title_style = typography.get("title_card", {})
    transform = title_style.get("transform", "")
    weight = title_style.get("weight", 400)
    tracking = float(title_style.get("tracking", 0.0) or 0.0)

    font, _font_findings = pick_title_font(typography)
    fill_hex = palette.get("text_light", "#E0E0E0")
    rule_hex = palette.get("accent_red", "#E02020")

    fontsize = max(1, round(height * _TITLE_FONT_SIZE_FRACTION))
    spacing = round(fontsize * tracking, 2)
    bold = -1 if weight >= _BOLD_WEIGHT_THRESHOLD else 0
    primary_colour = ass_colour(fill_hex)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding"
        ),
        (
            f"Style: TitleCard,{font},{fontsize},{primary_colour},{primary_colour},"
            f"&H00000000,&H00000000,{bold},0,0,0,100,100,{spacing},0,1,"
            f"2.0,1.0,{_ASS_ALIGNMENT_MIDDLE_CENTER},40,40,0,1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for overlay in overlays:
        if overlay.kind != "chapter-card":
            continue

        title_text = overlay.text.upper() if transform == "uppercase" else overlay.text
        title_text = _escape_ass_text(title_text)
        rule = " " * _TITLE_RULE_SPACE_COUNT

        text = (
            f"{{\\c{_wrap_override(fill_hex)}}}{title_text}{{\\r}}"
            f"\\N{{\\c{_wrap_override(rule_hex)}\\u1}}{rule}{{\\r}}"
        )
        start_ts = format_ass_timestamp(overlay.start)
        end_ts = format_ass_timestamp(overlay.end)
        lines.append(f"Dialogue: 0,{start_ts},{end_ts},TitleCard,,0,0,0,,{text}")

    return "\n".join(lines) + "\n"


# =============================================================================
# Part C: censor boxes (ffmpeg filter chain)
# =============================================================================

# A documented default region, not real face/object detection. `Overlay.detail`
# carries a free-text hint (e.g. "face") that this module cannot resolve to
# real coordinates -- there is no detector wired in. Centre-upper, sized to
# comfortably cover a face in a medium shot without blanking most of the
# frame. Retuning these constants retunes every censor box in one place.
CENSOR_REGION_WIDTH_FRACTION = 0.30
CENSOR_REGION_HEIGHT_FRACTION = 0.28
CENSOR_REGION_Y_FRACTION = 0.12  # distance from the top edge

# boxblur radius, in pixels, applied to the cropped censor region. Not scaled
# off the crop size: a fixed radius keeps the box visibly, unmistakably
# blurred at any resolution this pipeline renders at (1080p today). Capped at
# ffmpeg's own hard ceiling: boxblur's chroma_radius (defaulted from
# luma_radius here) rejects anything above 12 outright ("Invalid chroma_param
# radius value ... must be >= 0 and <= 12", confirmed empirically), so this
# stays at the top of that range rather than the higher value first tried.
_CENSOR_BLUR_RADIUS = 12


def censor_box_filter(overlays: list[Overlay], palette: dict, width: int = 1920, height: int = 1080) -> str:
    """An ffmpeg filter chain drawing red blurred censor boxes.

    Empty string when there are no `kind == "censor"` overlays -- callers
    (`draw_graphics`) skip the censor stage entirely rather than running
    ffmpeg with a no-op filter.

    Each censor overlay becomes one `split` -> `crop`+`boxblur`+
    `colorchannelmixer` -> `overlay` segment: `split` duplicates the frame,
    one copy (`crop`) is reduced to the default region, blurred
    (`boxblur`), and tinted red (`colorchannelmixer` boosts the red output
    channel and suppresses green/blue), then `overlay`ed back onto the
    untouched copy at the same position, gated by `enable='between(t,start,
    end)'` so it only appears for the overlay's own time window. Multiple
    censor overlays chain: each segment after the first reads from the
    previous segment's merged output (`[mergedN]`), so N overlays produce N
    independent boxes (same region, different windows) composited in
    sequence. This whole chain is valid as a single `-vf` value -- despite
    using `split`/labelled pads internally, the overall graph still has
    exactly one external input and one external output, which is all `-vf`
    requires.
    """
    censor_overlays = [o for o in overlays if o.kind == "censor"]
    if not censor_overlays:
        return ""

    cw = max(2, round(width * CENSOR_REGION_WIDTH_FRACTION))
    ch = max(2, round(height * CENSOR_REGION_HEIGHT_FRACTION))
    cx = round((width - cw) / 2)
    cy = round(height * CENSOR_REGION_Y_FRACTION)

    parts: list[str] = []
    last_index = len(censor_overlays) - 1
    for i, overlay in enumerate(censor_overlays):
        base_label = f"cbase{i}"
        blur_label = f"cblur{i}"
        tint_label = f"ctint{i}"
        source = f"[merged{i - 1}]" if i > 0 else ""

        parts.append(f"{source}split=2[{base_label}][{blur_label}]")
        parts.append(
            f"[{blur_label}]crop={cw}:{ch}:{cx}:{cy},"
            f"boxblur={_CENSOR_BLUR_RADIUS}:2,"
            f"colorchannelmixer=rr=1.0:rg=0.15:rb=0.15:"
            f"gr=0.05:gg=0.25:gb=0.05:br=0.05:bg=0.05:bb=0.25[{tint_label}]"
        )
        enable = f"between(t\\,{overlay.start:.6f}\\,{overlay.end:.6f})"
        out_label = "" if i == last_index else f"[merged{i}]"
        parts.append(f"[{base_label}][{tint_label}]overlay=x={cx}:y={cy}:enable='{enable}'{out_label}")

    return ";".join(parts)


def censor_region_findings(overlays: list[Overlay]) -> list[Finding]:
    """One `severity="warning"` Finding per censor overlay: the region was defaulted.

    `Overlay.detail` is a free-text hint from the script (e.g. "face"), not
    real coordinates -- this module never pretends to know where the actual
    face is. Separate from `censor_box_filter` (which only returns the
    filter-chain string, matching its documented signature) so `draw_graphics`
    can report this without needing an ffmpeg run.
    """
    return [
        Finding(
            gate="graphics",
            severity="warning",
            message=(
                f"[CENSOR:{overlay.detail}] at {overlay.start:.2f}s drawn over the "
                f"documented default region (centre-upper, "
                f"{int(CENSOR_REGION_WIDTH_FRACTION * 100)}% width x "
                f"{int(CENSOR_REGION_HEIGHT_FRACTION * 100)}% height); 'detail' is a "
                f"free-text hint, not real coordinates -- no face/object detection ran."
            ),
        )
        for overlay in overlays
        if overlay.kind == "censor"
    ]


# =============================================================================
# Part D: compositing
# =============================================================================


def _run(args: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-1200:]
        raise RuntimeError(f"ffmpeg failed: {' '.join(args[:4])} ...\n{detail}")
    return result


def _apply_censor_boxes(video_path: Path, vf: str, out_path: Path) -> Path:
    """Run the censor-box filter chain, same encode settings as `subtitles.burn`."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-vf", vf,
            *video_args(PLATE_CRF, maxrate=PLATE_MAXRATE, bufsize=PLATE_BUFSIZE),
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            str(out_path),
        ]
    )
    return out_path


def draw_graphics(
    video_path: Path,
    overlays: list[Overlay],
    typography: dict,
    palette: dict,
    out_path: Path,
    work_dir: Path,
) -> tuple[Path, list[Finding]]:
    """Composite chapter cards and censor boxes onto a video.

    Two independent stages, each skipped cleanly when there is nothing of
    its kind in `overlays`:

    1. Censor boxes, via `censor_box_filter` -- an ordinary `-vf` pass with
       the project's bitrate cap and audio copied, not re-encoded (same
       settings `subtitles.burn` uses).
    2. Chapter cards, via `chapter_card_ass` written to a real `.ass` file
       and burned in with `subtitles.burn` -- reused directly rather than
       reimplemented, so the ':' / Windows-drive-letter fix already proven
       there (running ffmpeg with `cwd` set to the ASS file's directory,
       passing a bare relative filename) applies here too.

    Whichever stage runs last writes directly to `out_path` (no redundant
    final copy); if a stage runs first only, its own encode already lands at
    the final resolution/bitrate for the next stage to pick up from
    `work_dir`. If neither stage has anything to draw, the input is copied
    to `out_path` unchanged (`shutil.copy2`, not a re-encode) -- a
    pass-through render must not alter pixels or duration at all.
    """
    video_path = Path(video_path)
    out_path = Path(out_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    findings: list[Finding] = []
    width, height = _probe_video_dims(video_path)

    censor_overlays = [o for o in overlays if o.kind == "censor"]
    chapter_overlays = [o for o in overlays if o.kind == "chapter-card"]
    stages_remaining = int(bool(censor_overlays)) + int(bool(chapter_overlays))

    current = video_path
    stage_index = 0

    if censor_overlays:
        stage_index += 1
        target = out_path if stage_index == stages_remaining else work_dir / "censored.mp4"
        vf = censor_box_filter(overlays, palette, width, height)
        current = _apply_censor_boxes(current, vf, target)
        findings += censor_region_findings(overlays)

    if chapter_overlays:
        stage_index += 1
        ass_text = chapter_card_ass(overlays, typography, palette, width, height)
        ass_path = work_dir / "chapter-cards.ass"
        ass_path.write_text(ass_text, encoding="utf-8")
        _font, font_findings = pick_title_font(typography)
        findings += font_findings
        current = burn(current, ass_path, out_path)

    if current == video_path:
        shutil.copy2(video_path, out_path)

    return out_path, findings

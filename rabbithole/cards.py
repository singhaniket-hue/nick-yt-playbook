"""Graphic cards: turning `[SHOT:graphic ...]` slots into actual pictures.

Why this module exists
----------------------
`assets.py` planned every `graphic` slot as `deferred`, reasoning that
"graphics are rendered by the compose stage, not sourced". Nothing kept that
promise. `graphics.py` draws chapter cards (from `[CHAPTER:]`) and censor boxes
(from `[CENSOR:]`) and has no code path that turns a
`[SHOT:graphic timeline, pandrah May to solah May]` slot into a frame.

In one production audit that was 81 of 183 slots -- 330 of 706 cuts, 45% of
the episode -- planned as handled and silently unhandled. `assemble_footage`
would report each one as a missing asset and skip the cut. This module closes
that gap.

Mechanism: ffmpeg, reused not reinvented
----------------------------------------
A card is a `plates.build_plate` background with an ASS document burned onto it
by `subtitles.burn`. Both already exist, both are already proven against real
renders, and `burn` already solves the Windows `:`-in-filter-path problem that
breaks `ass=` (it runs ffmpeg with `cwd` set and passes a bare filename).
Following `graphics.py`'s explicit reasoning: no Remotion, no second rendering
runtime, no Node toolchain, to draw text and rectangles that libass and ffmpeg
draw natively.

Typography comes from two sources on purpose. Headings use
`graphics.pick_title_font` -- a condensed display face, the register a title
card wants. Body text uses `subtitles.pick_font`, which hunts specifically for
Devanagari coverage, because a card's content can legitimately be Hindi (the
narration it sits under is) and a display face that renders Latin beautifully
will render Devanagari as tofu.

The title-safe constraint is not decorative
-------------------------------------------
`render.cut_segment` applies `framing_filter` to every asset unconditionally,
and `detail` framing enlarges the frame to 130% and centre-crops back down --
so only the central 1/1.30 = 76.9% of a card survives that framing. Text laid
out to the frame edge would be cropped away on exactly the cuts that are
supposed to emphasise it. Everything here is laid out inside
`TITLE_SAFE_FRACTION`, which is set below that ratio with margin to spare, so a
card reads correctly under all four framings rather than only under `wide`.

What a card can and cannot know
-------------------------------
The content a card should show is not always in the marker. `[SHOT:graphic
checklist, four criteria]` names an archetype and a count, not the four
criteria -- those are in the narration beside it, as prose, in Devanagari.
Reliably splitting spoken prose into four bullets is not something this module
pretends to do.

So there are two paths, and the distinction is deliberate:

- **Explicit content.** A detail may carry its own items after a colon,
  pipe-separated: `[SHOT:graphic checklist: unemployed | lazy | eleven hours
  online | professional ranting]` renders four real bullets. Structured
  shorthands are also understood -- `A to B` becomes a two-point timeline,
  `A versus B` becomes a two-panel comparison.
- **Label only (animatic).** Anything else can render as a designed card
  carrying the detail's own words, so a preview has an intentional frame
  instead of a hole in the timeline. It is still a production placeholder:
  final-quality asset and render commands reject it. Enriching a marker's
  detail upgrades its card with no code change, which is the seam this design
  is built around.

None of these are data visualisations. A `stat` card sets a number
typographically; it does not plot anything, because the numbers live in
`claims.json` and binding a slot to a claim is an editorial decision this
module does not make.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_EVEN
from functools import lru_cache
from pathlib import Path

from rabbithole.graphics import pick_title_font
from rabbithole.slots import Slot
from rabbithole.sources.plates import PlateSpec, build_plate
from rabbithole.subtitles import (
    _escape_ass_text,
    _font_directories,
    _wrap_override,
    ass_colour,
    burn,
    format_ass_timestamp,
    pick_font,
)
from rabbithole.validate import Finding

CARD_KINDS = (
    "callout",
    "comparison",
    "checklist",
    "timeline",
    "stat",
    "document",
    "montage",
    "label",
)

# The fraction of the frame every element stays inside. `detail` framing crops
# to 1/1.30 = 76.9% of the frame (see render._DETAIL_ZOOM). 0.68 leaves roughly
# 4.5% of the original frame on each side between authored content and that
# crop, enough for Resolve's resampling and libass outlines instead of relying
# on the former 0.74 near-boundary tolerance.
TITLE_SAFE_FRACTION = 0.68

# Text receives another inset inside title-safe. Shapes may use the complete
# title-safe box, but authored glyphs never touch it: clipping a subtitle-style
# outline at the exact text anchor was enough to make a first character look
# missing even when its mathematical position was technically in bounds.
TEXT_SAFE_INSET_FRACTION = 0.05
_TEXT_WIDTH_SAFETY_MULTIPLIER = 1.12

# The background every card is drawn onto. 'grain' rather than 'black': a card
# on pure black reads as a slide, a card on moving grain reads as part of the
# same graded film as the footage around it.
CARD_PLATE_KIND = "grain"

# Resolve rounds absolute timeline endpoints independently from a card's local
# duration. A half-frame boundary can therefore ask for one source frame past
# the nearest-frame authored card. Keep exactly one repeated terminal frame as
# a media handle; it is never included in the authored card timing.
CARD_SAFE_TRAILING_FRAMES = 1

_HEADING_SIZE_FRACTION = 0.062
_DISCLOSURE_SIZE_FRACTION = 0.026
# Body text is read, not glanced at -- a card holds for 3-12 seconds and the
# viewer has to finish it. Rendered at 0.040 the items were legible but weak
# against the heading; 0.052 reads at a glance without crowding six of them.
_BODY_SIZE_FRACTION = 0.052
_STAT_SIZE_FRACTION = 0.150
_RULE_THICKNESS_FRACTION = 0.0045
# libass centres a multi-line ``\an5`` event as one block. Use an explicit
# conservative line box when placing the rule below that block; a fixed offset
# crosses the final line as soon as a source title wraps.
_HEADING_LINE_HEIGHT_MULTIPLIER = 1.25
_HEADING_RULE_PADDING_FRACTION = 0.018

_BOLD_WEIGHT_THRESHOLD = 600
_ASS_TOP_LEFT = 7
_ASS_MIDDLE_LEFT = 4
_ASS_MIDDLE_CENTER = 5
_ASS_MIDDLE_RIGHT = 6

# An authored comparison heading beginning with this phrase opts into the
# reusable test-signal visual system below. Keeping it in the heading rather
# than adding a new CardSpec kind means existing scripts and manifests remain
# compatible, while an ordinary comparison still takes the established
# text-column path byte-for-byte.
_SIGNAL_COMPARISON_PREFIX_RE = re.compile(
    r"^\s*signal comparison\s*-\s*(?P<variant>.+?)\s*$",
    re.IGNORECASE,
)
_SIGNAL_DISCLOSURE = "LOCAL ILLUSTRATION · GENERAL TESTING LOGIC"
_SIGNAL_CAVEAT = "NOT WEBDRIVER TORSO'S PUBLISHED ALGORITHM"
SIGNAL_COMPARISON_ITEM_COUNTS = {
    "edge baseline": 2,
    "processed change": 2,
    "timing and audio": 2,
    "automated flag": 4,
}

# Longest run of items any template lays out. Beyond this the layout stops
# being legible at 1080p, so extra items are dropped and reported rather than
# silently overflowing off the safe area.
MAX_ITEMS = 6

# Ordered longest-first and matched as complete words or phrases in the
# lowercased detail. Order between groups is significant: a detail reading
# "timeline comparison" is a timeline first.
_CLASSIFY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("timeline", ("timeline", "chronology", "calendar", "growth curve", " to ")),
    ("comparison", ("split-screen", "side-by-side", "versus", " vs ", "compared",
                    "comparison", "mismatch", "two headlines")),
    ("montage", ("montage", "three-clip", "three-part", "four-element", "panels",
                 "three panels")),
    ("checklist", ("checklist", "criteria", "demands", "points", "list")),
    ("document", ("excerpt", "order", "overlay", "paper", "floor plan", "form")),
    (
        "stat",
        (
            "percent",
            "percentage",
            "percentages",
            "crore",
            "crores",
            "lakh",
            "lakhs",
            "million",
            "millions",
            "thousand",
            "thousands",
            "figure",
            "figures",
            "number",
            "numbered",
            "numbers",
            "count",
            "counts",
            "statistic",
            "statistics",
        ),
    ),
    (
        "callout",
        (
            "callout",
            "label",
            "labels",
            "quote",
            "manifesto text",
            "claims",
            "statement",
            "icon",
            "symbol",
        ),
    ),
)


@dataclass(frozen=True)
class CardSpec:
    """One card to draw.

    `heading` is always shown. `items` is template-specific: bullets for
    `checklist`, the two sides of a `comparison`, points on a `timeline`,
    panels of a `montage`. An empty `items` is valid -- every template
    degrades to a heading-only card rather than failing.

    `disclosure` is optional frame-native editorial context. Document cards
    use it for labels such as ``EDITORIAL PARAPHRASE · SOURCE-ATTRIBUTED`` so
    a locally composed evidence summary cannot be mistaken for source pixels.
    """

    kind: str
    heading: str
    duration: float
    items: tuple[str, ...] = ()
    disclosure: str = ""

    def __post_init__(self) -> None:
        if self.kind not in CARD_KINDS:
            raise ValueError(
                f"Unknown card kind {self.kind!r}; valid kinds: {', '.join(CARD_KINDS)}"
            )
        if self.duration <= 0:
            raise ValueError(f"Card duration must be positive, got {self.duration}")


def _contains_classification_cue(detail: str, cue: str) -> bool:
    """Whether *cue* appears as authored words rather than inside another word."""

    normalized_cue = cue.strip()
    return bool(
        normalized_cue
        and re.search(
            rf"(?<!\w){re.escape(normalized_cue)}(?!\w)",
            detail,
        )
    )


def classify(detail: str) -> str:
    """The card archetype a slot detail asks for.

    Word-boundary matching on the lowercased detail, in `_CLASSIFY_RULES`
    order. This keeps a real ``label`` directive while preventing words such
    as ``labelled`` from silently turning a motion direction into a callout.
    Falls back to `label` -- a designed card carrying the detail's own words --
    rather than raising, because an unrecognised detail should still put
    something intentional on screen.
    """
    lowered = re.sub(r"\s+", " ", detail.lower().strip())
    for kind, needles in _CLASSIFY_RULES:
        if any(_contains_classification_cue(lowered, needle) for needle in needles):
            return kind
    return "label"


# Words that name the *archetype* rather than the content. "33 percent versus
# 50 percent comparison" splits into two sides, and the trailing "comparison"
# belongs to neither -- left in, it renders as part of the second value.
_ARCHETYPE_NOISE = (
    "comparison", "compared", "timeline", "checklist", "montage", "callout",
    "overlay", "split-screen", "split screen", "side-by-side", "generic",
    "summary", "span", "curve", "excerpt",
)

# These words describe a production instruction or a desired source, not the
# evidence that should appear on screen. Heading-only cards containing one of
# them are useful in an animatic, where they communicate editorial intent, but
# must never pass as finished documentary footage.
_PRODUCTION_NOTE_TERMS = (
    "callout",
    "capture",
    "card",
    "clip",
    "comparison",
    "document",
    "excerpt",
    "footage",
    "frame",
    "generic",
    "graphic",
    "headline",
    "icon",
    "label",
    "map",
    "mockup",
    "montage",
    "overlay",
    "placeholder",
    "plate",
    "profile",
    "screenshot",
    "side-by-side",
    "split screen",
    "split-screen",
    "still",
    "timeline",
    "zoom",
)


def _strip_archetype_noise(item: str) -> str:
    """Drop a leading/trailing archetype word from an extracted item."""
    cleaned = item.strip().strip(",").strip()
    changed = True
    while changed:
        changed = False
        lowered = cleaned.lower()
        for noise in _ARCHETYPE_NOISE:
            if lowered.endswith(" " + noise):
                cleaned = cleaned[: -(len(noise) + 1)].rstrip(" ,")
                changed = True
                break
            if lowered.startswith(noise + " "):
                cleaned = cleaned[len(noise) + 1:].lstrip(" ,")
                changed = True
                break
    return cleaned


def _split_items(text: str) -> list[str]:
    """Explicit pipe-separated items, or structured shorthands.

    Recognises, in order: an explicit `a | b | c` run; `a versus b` / `a vs b`;
    `a to b`. Returns `[]` when the text carries no item structure, which is
    the common case for the details already written into a script.
    """
    if "|" in text:
        return [
            cleaned for cleaned in (part.strip() for part in text.split("|")) if cleaned
        ]

    for separator in (" versus ", " vs. ", " vs ", " to "):
        if separator in text.lower():
            index = text.lower().index(separator)
            left = _strip_archetype_noise(text[:index])
            right = _strip_archetype_noise(text[index + len(separator):])
            if left and right:
                return [left, right]
    return []


def parse_detail(detail: str, duration: float) -> tuple[CardSpec, list[Finding]]:
    """Turn a slot's `[SHOT:graphic <detail>]` text into a CardSpec.

    Splits on the first colon: text before it is the heading, text after it
    carries explicit items. With no colon, the detail's first comma-separated
    clause is the heading and the remainder is searched for a structured
    shorthand (`A to B`, `A versus B`).
    """
    findings: list[Finding] = []
    detail = (detail or "").strip()
    kind = classify(detail)

    if ":" in detail:
        head, _, tail = detail.partition(":")
        heading = head.strip()
        tail = tail.strip()
        items = _split_items(tail)
        # Text after the colon is content the author wrote deliberately. When
        # it carries no pipe run and no shorthand it is still content -- one
        # item, not nothing. Dropping it silently discarded the only thing the
        # author asked to be shown.
        if not items and tail:
            items = [tail]
    else:
        parts = [p.strip() for p in detail.split(",") if p.strip()]
        heading = parts[0] if parts else "graphic"
        remainder = ", ".join(parts[1:])
        items = _split_items(remainder)
        if not items:
            # No shorthand in the tail -- try the whole detail. When that is
            # what supplies the items ("33 percent versus 50 percent"), the
            # first clause is one of the sides, not a heading, so the heading
            # becomes the archetype name instead of repeating the content.
            items = _split_items(detail)
            if items:
                heading = kind

    # The archetype word is instruction to this renderer, not content for the
    # viewer. "open threads montage" and "official statement excerpt" are how an
    # author names a shot; on screen they read as a caption describing its own
    # format. Strip it -- unless that empties the heading, in which case the
    # archetype word is all the author gave us and it stays.
    stripped_heading = _strip_archetype_noise(heading)
    if stripped_heading:
        heading = stripped_heading

    # A heading that is *only* an archetype word ("comparison", "timeline") is
    # a label describing the card's own format. Drop it when the items can
    # carry the card alone -- but never when they cannot, since a card with no
    # heading and no items would render empty.
    if items and heading.strip().lower() in {*_ARCHETYPE_NOISE, *CARD_KINDS}:
        heading = ""

    # A callout needs a verbal anchor. Details such as
    # `three labels confirmed date | playful reference | broader intent
    # uncertain` intentionally carry their content in items, but classification
    # strips the generic "callout/label" heading. Promoting the first authored
    # item gives the remaining points context instead of leaving a small body
    # line floating under an otherwise blank card.
    if kind == "callout" and items and not heading.strip():
        heading = items.pop(0)

    if len(items) > MAX_ITEMS:
        findings.append(
            Finding(
                gate="cards",
                severity="warning",
                message=(
                    f"Card {detail!r} lists {len(items)} items; only the first "
                    f"{MAX_ITEMS} are laid out, since more stops being legible at "
                    f"1080p. Split it across two [SHOT:graphic] slots instead."
                ),
            )
        )
        items = items[:MAX_ITEMS]

    # Explicit items with an unrecognised archetype must not be silently
    # dropped: `label` renders a heading only, so `open threads: a | b | c`
    # would lose all three. A list of items is a checklist by default.
    if items and kind == "label":
        kind = "checklist"

    if not items and kind in ("comparison", "montage", "document"):
        findings.append(
            Finding(
                gate="cards",
                severity="warning",
                message=(
                    f"Card {detail!r} classified as {kind!r} but carries no items to "
                    f"lay out, so it renders as a heading over an empty frame. Add "
                    f"them explicitly, e.g. "
                    f"[SHOT:graphic {heading}: first | second]."
                ),
            )
        )

    spec = CardSpec(
        kind=kind,
        heading=heading,
        duration=duration,
        items=tuple(items),
    )
    findings.extend(_signal_comparison_findings(spec, detail=detail))
    return spec, findings


def production_note_reason(detail: str, spec: CardSpec | None = None) -> str:
    """Why a heading-only card is an animatic production note, if it is one.

    A final card needs authored, visible content. Explicit items after a colon,
    pipe run, ``versus``, or ``to`` always satisfy that requirement. A bare
    unclassified label does not: it is the old catch-all path that converted
    any instruction into a styled placeholder. Likewise, a heading whose words
    explicitly ask for a screenshot, montage, excerpt, zoom, and so on is a
    direction to an editor rather than evidence for the audience.

    The function returns a human-readable reason instead of only a boolean so
    callers can produce actionable fail-closed CLI messages. It deliberately
    does not alter :func:`parse_detail`; animatic generation remains available.
    """
    if spec is None:
        spec, _findings = parse_detail(detail, 1.0)
    if spec.items:
        return ""

    lowered = re.sub(r"\s+", " ", (detail or "").lower()).strip()
    if spec.kind == "label":
        return (
            "it is an unstructured label with no authored on-screen evidence "
            "(add explicit content after a colon)"
        )

    matched = next(
        (
            term
            for term in _PRODUCTION_NOTE_TERMS
            if re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", lowered)
        ),
        "",
    )
    if matched:
        return (
            f"it is a heading-only production direction containing {matched!r}; "
            "replace it with a sourced capture or add explicit card content"
        )
    return ""


def spec_for_slot(slot: Slot) -> tuple[CardSpec, list[Finding]]:
    """The CardSpec for a `graphic` slot, spanning its whole hold."""
    if slot.kind != "graphic":
        raise ValueError(f"spec_for_slot expects a graphic slot, got {slot.kind!r}")
    return parse_detail(slot.detail, slot.hold_seconds)


# --- ASS construction ------------------------------------------------------------


def _transform(text: str, typography: dict) -> str:
    if typography.get("title_card", {}).get("transform") == "uppercase":
        return text.upper()
    return text


def _rect(x: float, y: float, w: float, h: float) -> str:
    """An ASS filled-rectangle drawing command, origin at the \\pos anchor."""
    return (
        f"{{\\p1}}m {x:.0f} {y:.0f} "
        f"l {x + w:.0f} {y:.0f} {x + w:.0f} {y + h:.0f} {x:.0f} {y + h:.0f}{{\\p0}}"
    )


@lru_cache(maxsize=32)
def _measurement_font(font_family: str, font_size: int):
    """Resolve *font_family* to the same native face libass is likely to use.

    Pillow and libass both measure through FreeType. Resolving the actual face
    makes wrapping deterministic for condensed Latin headings and wide
    Devanagari body glyphs instead of pretending every character is 0.62em.
    The path/index walk is cached and sorted so Windows, macOS, and Linux each
    make one stable choice per family and size.
    """
    try:
        from PIL import ImageFont
    except ImportError:  # pragma: no cover - Pillow is a project dependency
        return None

    wanted = (font_family or "").strip().casefold()
    if wanted:
        for directory in _font_directories():
            if not directory.is_dir():
                continue
            try:
                paths = sorted(
                    (
                        path
                        for path in directory.rglob("*")
                        if path.is_file()
                        and path.suffix.lower() in {".ttf", ".otf", ".ttc"}
                    ),
                    key=lambda path: str(path).casefold(),
                )
            except OSError:
                continue
            for path in paths:
                max_faces = 16 if path.suffix.lower() == ".ttc" else 1
                for index in range(max_faces):
                    try:
                        face = ImageFont.truetype(
                            str(path), size=font_size, index=index
                        )
                        family, _style = face.getname()
                    except (OSError, ValueError):
                        break
                    if str(family).strip().casefold() == wanted:
                        return face

    # DejaVu Sans ships with Pillow in supported environments. It is a stable,
    # deliberately wider fallback for measurement only; libass still uses the
    # selected production family when drawing.
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size=font_size)
    except OSError:  # pragma: no cover - only a broken Pillow install
        return None


def _measured_line_width(
    text: str,
    font_size: float,
    *,
    font_family: str = "",
    letter_spacing: float = 0.0,
) -> float:
    """Conservative rendered width for one unescaped line, in ASS pixels."""
    normalized = str(text or "")
    size = max(1, round(font_size))
    font = _measurement_font(font_family, size)
    if font is not None:
        width = float(font.getlength(normalized))
    else:
        # Last-resort deterministic estimate. Wide glyphs deliberately cost
        # more than 1em; the safety multiplier below absorbs shaping drift.
        width = sum(
            size * (1.0 if char in "MW@#%&" else 0.72 if ord(char) > 127 else 0.62)
            for char in normalized
        )
    width += max(0, len(normalized) - 1) * max(0.0, letter_spacing)
    return width * _TEXT_WIDTH_SAFETY_MULTIPLIER


def _wrap_measured_lines(
    text: str,
    max_width: float,
    font_size: float,
    *,
    font_family: str = "",
    letter_spacing: float = 0.0,
) -> list[str]:
    """Wrap words, and overlong words, against measured rendered width."""
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return []
    budget = max(1.0, float(max_width))

    def fits(candidate: str) -> bool:
        return _measured_line_width(
            candidate,
            font_size,
            font_family=font_family,
            letter_spacing=letter_spacing,
        ) <= budget

    def split_word(word: str) -> list[str]:
        pieces: list[str] = []
        current = ""
        for char in word:
            candidate = current + char
            if current and not fits(candidate):
                pieces.append(current)
                current = char
            else:
                current = candidate
        if current:
            pieces.append(current)
        return pieces or [word]

    lines: list[str] = []
    current = ""
    for word in normalized.split(" "):
        pieces = [word] if fits(word) else split_word(word)
        for piece_index, piece in enumerate(pieces):
            candidate = f"{current} {piece}".strip()
            if current and not fits(candidate):
                lines.append(current)
                current = piece
            else:
                current = candidate
            # A split word has no semantic whitespace between its pieces, but
            # each full-budget piece must become a deterministic hard line.
            if piece_index < len(pieces) - 1:
                lines.append(current)
                current = ""
    if current:
        lines.append(current)
    return lines or [normalized]


def _wrapped_ass_text(
    text: str,
    max_width: float,
    font_size: float,
    *,
    font_family: str = "",
    letter_spacing: float = 0.0,
) -> str:
    """Escape *text* and hard-wrap it by measured rendered width."""
    lines = _wrap_measured_lines(
        text,
        max_width,
        font_size,
        font_family=font_family,
        letter_spacing=letter_spacing,
    )
    return r"\N".join(_escape_ass_text(line) for line in lines)


@dataclass
class _Layout:
    """Frame geometry, all of it inside the title-safe box."""

    width: int
    height: int

    @property
    def safe_w(self) -> float:
        return self.width * TITLE_SAFE_FRACTION

    @property
    def safe_h(self) -> float:
        return self.height * TITLE_SAFE_FRACTION

    @property
    def left(self) -> float:
        return (self.width - self.safe_w) / 2

    @property
    def top(self) -> float:
        return (self.height - self.safe_h) / 2

    @property
    def centre_x(self) -> float:
        return self.width / 2

    @property
    def centre_y(self) -> float:
        return self.height / 2

    @property
    def text_left(self) -> float:
        return self.left + self.safe_w * TEXT_SAFE_INSET_FRACTION

    @property
    def text_right(self) -> float:
        return self.left + self.safe_w * (1 - TEXT_SAFE_INSET_FRACTION)

    @property
    def text_w(self) -> float:
        return self.text_right - self.text_left


def _styles(typography: dict, palette: dict, width: int, height: int) -> list[str]:
    title_font, _ = pick_title_font(typography)
    body_font, _ = pick_font(typography)
    weight = typography.get("title_card", {}).get("weight", 400)
    tracking = float(typography.get("title_card", {}).get("tracking", 0.0) or 0.0)
    bold = -1 if weight >= _BOLD_WEIGHT_THRESHOLD else 0

    heading_size = max(1, round(height * _HEADING_SIZE_FRACTION))
    disclosure_size = max(1, round(height * _DISCLOSURE_SIZE_FRACTION))
    body_size = max(1, round(height * _BODY_SIZE_FRACTION))
    stat_size = max(1, round(height * _STAT_SIZE_FRACTION))
    light = ass_colour(palette.get("text_light", "#E0E0E0"))
    accent = ass_colour(palette.get("accent_red", "#E02020"))

    fmt = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding"
    )
    return [
        fmt,
        f"Style: CardHead,{title_font},{heading_size},{light},{light},"
        f"&H00000000,&H00000000,{bold},0,0,0,100,100,{round(heading_size * tracking, 2)},"
        f"0,1,2.0,1.0,{_ASS_MIDDLE_CENTER},0,0,0,1",
        f"Style: CardBody,{body_font},{body_size},{light},{light},"
        f"&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1.5,1.0,{_ASS_TOP_LEFT},0,0,0,1",
        f"Style: CardDisclosure,{body_font},{disclosure_size},{accent},{accent},"
        f"&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1.5,1.0,"
        f"{_ASS_TOP_LEFT},0,0,0,1",
        f"Style: CardStat,{title_font},{stat_size},{light},{light},"
        f"&H00000000,&H00000000,{bold},0,0,0,100,100,0,0,1,2.0,1.0,{_ASS_MIDDLE_CENTER},0,0,0,1",
        f"Style: CardShape,{body_font},{body_size},{light},{light},"
        f"&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,{_ASS_TOP_LEFT},0,0,0,1",
    ]


def _dialogue(style: str, duration: float, text: str) -> str:
    return (
        f"Dialogue: 0,{format_ass_timestamp(0.0)},{format_ass_timestamp(duration)},"
        f"{style},,0,0,0,,{text}"
    )


def _heading_events(spec: CardSpec, layout: _Layout, palette: dict, typography: dict,
                    y: float) -> list[str]:
    """Heading plus the accent rule under it, the shared identity of every card."""
    fill = palette.get("text_light", "#E0E0E0")
    rule = palette.get("accent_red", "#E02020")
    thickness = max(2.0, layout.height * _RULE_THICKNESS_FRACTION)
    rule_w = layout.safe_w * 0.16

    events = []
    rule_y = y + layout.height * 0.055
    if spec.heading.strip():
        heading_size = max(1, round(layout.height * _HEADING_SIZE_FRACTION))
        heading_font, _findings = pick_title_font(typography)
        tracking = float(
            typography.get("title_card", {}).get("tracking", 0.0) or 0.0
        )
        heading = _wrapped_ass_text(
            _transform(spec.heading, typography),
            layout.text_w * 0.90,
            heading_size,
            font_family=heading_font,
            letter_spacing=heading_size * tracking,
        )
        line_count = heading.count(r"\N") + 1
        heading_bottom = (
            y
            + (
                line_count
                * heading_size
                * _HEADING_LINE_HEIGHT_MULTIPLIER
            )
            / 2
        )
        rule_y = heading_bottom + max(
            layout.height * _HEADING_RULE_PADDING_FRACTION,
            thickness * 2,
        )
        events.append(_dialogue(
            "CardHead", spec.duration,
            f"{{\\an{_ASS_MIDDLE_CENTER}\\pos({layout.centre_x:.0f},{y:.0f})"
            f"\\clip({layout.text_left:.0f},{layout.top:.0f},"
            f"{layout.text_right:.0f},{layout.top + layout.safe_h:.0f})"
            f"\\c{_wrap_override(fill)}}}{heading}"))

    # The rule is drawn either way: with a heading it underlines it, without one
    # it is the card's only fixed anchor, which keeps a heading-less comparison
    # or timeline visually part of the same set as every other card.
    events.append(_dialogue(
        "CardShape", spec.duration,
        f"{{\\an{_ASS_TOP_LEFT}\\pos({layout.centre_x - rule_w / 2:.0f},"
        f"{rule_y:.0f})\\c{_wrap_override(rule)}}}"
        f"{_rect(0, 0, rule_w, thickness)}"))
    return events


def _callout_events(spec, layout, palette, typography):
    events = _heading_events(spec, layout, palette, typography, layout.centre_y)
    if spec.items:
        body_size = max(1, round(layout.height * _BODY_SIZE_FRACTION))
        body_font, _findings = pick_font(typography)
        body = _wrapped_ass_text(
            " / ".join(spec.items),
            layout.text_w * 0.78,
            body_size,
            font_family=body_font,
        )
        clip_w = layout.text_w * 0.82
        clip_left = layout.centre_x - clip_w / 2
        clip_right = layout.centre_x + clip_w / 2
        events.append(_dialogue(
            "CardBody", spec.duration,
            f"{{\\an{_ASS_MIDDLE_CENTER}\\pos({layout.centre_x:.0f},"
            f"{layout.centre_y + layout.height * 0.13:.0f})"
            f"\\clip({clip_left:.0f},{layout.top:.0f},{clip_right:.0f},"
            f"{layout.top + layout.safe_h:.0f})}}{body}"))
    return events


def _label_events(spec, layout, palette, typography):
    return _heading_events(spec, layout, palette, typography, layout.centre_y)


def _checklist_events(spec, layout, palette, typography):
    events = _heading_events(spec, layout, palette, typography, layout.top + layout.safe_h * 0.14)
    if not spec.items:
        return events

    accent = palette.get("accent_red", "#E02020")
    light = palette.get("text_light", "#E0E0E0")
    thickness = max(2.0, layout.height * _RULE_THICKNESS_FRACTION)
    marker = layout.height * 0.018
    body_size = max(1, round(layout.height * _BODY_SIZE_FRACTION))
    body_font, _findings = pick_font(typography)

    # Centre the item block in the space below the heading rather than hanging
    # it from a fixed offset: with two items a fixed start leaves the card
    # visibly bottom-empty, and with six it crowds the lower safe edge.
    step = min(layout.height * 0.095, (layout.safe_h * 0.6) / max(1, len(spec.items)))
    block_h = step * max(0, len(spec.items) - 1)
    area_top = layout.top + layout.safe_h * 0.30
    area_bottom = layout.top + layout.safe_h * 0.94
    first = max(area_top, (area_top + area_bottom - block_h) / 2)
    text_x = layout.text_left + layout.safe_w * 0.11
    clip_right = layout.text_right
    body_width = max(1.0, clip_right - text_x - layout.safe_w * 0.025)

    for index, item in enumerate(spec.items):
        y = first + index * step
        events.append(_dialogue(
            "CardShape", spec.duration,
            f"{{\\an{_ASS_TOP_LEFT}\\pos({layout.left + layout.safe_w * 0.10:.0f},{y:.0f})"
            f"\\c{_wrap_override(accent)}}}{_rect(0, 0, marker, thickness * 1.6)}"))
        events.append(_dialogue(
            "CardBody", spec.duration,
            f"{{\\an{_ASS_TOP_LEFT}\\pos({text_x:.0f},{y - layout.height * 0.020:.0f})"
            f"\\clip({text_x - layout.safe_w * 0.01:.0f},{layout.top:.0f},"
            f"{clip_right:.0f},{layout.top + layout.safe_h:.0f})"
            f"\\c{_wrap_override(light)}}}"
            f"{_wrapped_ass_text(item, body_width, body_size, font_family=body_font)}"))
    return events


def _signal_comparison_variant(heading: str) -> str | None:
    """Return the requested signal motif, or ``None`` for a normal comparison."""
    match = _SIGNAL_COMPARISON_PREFIX_RE.match(heading or "")
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group("variant").strip()).casefold()


def _signal_comparison_findings(
    spec: CardSpec, *, detail: str = ""
) -> list[Finding]:
    """Validate the closed signal-motif vocabulary and authored item shape."""
    variant = _signal_comparison_variant(spec.heading)
    if variant is None:
        return []
    expected = SIGNAL_COMPARISON_ITEM_COUNTS.get(variant)
    label = detail or spec.heading
    if expected is None:
        supported = ", ".join(SIGNAL_COMPARISON_ITEM_COUNTS)
        return [
            Finding(
                gate="cards",
                severity="error",
                message=(
                    f"Signal-comparison card {label!r} names unsupported variant "
                    f"{variant!r}; expected one of {supported}."
                ),
            )
        ]
    if len(spec.items) != expected:
        noun = "item" if expected == 1 else "items"
        return [
            Finding(
                gate="cards",
                severity="error",
                message=(
                    f"Signal-comparison variant {variant!r} requires exactly "
                    f"{expected} authored {noun}, but {label!r} supplies "
                    f"{len(spec.items)}. Use pipe-separated labels after the "
                    "colon."
                ),
            )
        ]
    return []


def _signal_comparison_events(spec, layout, palette, typography, variant):
    """A reusable reference/processed signal diagram with an editorial caveat.

    These panels explain ordinary quality-control concepts; they do not claim
    to reproduce Google's private test implementation. The two disclosures are
    therefore part of the rendered frame, not metadata an export could lose.
    """
    light = palette.get("text_light", "#E0E0E0")
    mid = palette.get("text_mid", "#404040")
    accent = palette.get("accent_red", "#E02020")
    blue = "#2058D8"
    shifted_red = "#D66A45"
    shifted_blue = "#5C43D7"
    panel_fill = "#D8D8D8"
    disclosure_size = max(
        1, round(layout.height * _DISCLOSURE_SIZE_FRACTION)
    )
    disclosure_font, _findings = pick_font(typography)

    viewer_spec = CardSpec(
        kind="comparison",
        heading="REFERENCE / PROCESSED",
        duration=spec.duration,
    )
    events = _heading_events(
        viewer_spec,
        layout,
        palette,
        typography,
        layout.top + layout.safe_h * 0.085,
    )

    gap = layout.safe_w * 0.055
    panel_w = layout.safe_w * 0.40
    panel_h = layout.safe_h * 0.34
    total_w = panel_w * 2 + gap
    left_x = layout.centre_x - total_w / 2
    right_x = left_x + panel_w + gap
    panel_y = layout.top + layout.safe_h * 0.31
    label_y = panel_y - layout.safe_h * 0.055
    thickness = max(2.0, layout.height * _RULE_THICKNESS_FRACTION * 0.7)

    def shape(
        x: float,
        y: float,
        w: float,
        h: float,
        colour: str,
        *,
        blur: int = 0,
        alpha: str = "",
    ) -> None:
        effects = f"\\blur{blur}" if blur else ""
        if alpha:
            effects += f"\\alpha&H{alpha}&"
        events.append(_dialogue(
            "CardShape",
            spec.duration,
            f"{{\\an{_ASS_TOP_LEFT}\\pos({x:.0f},{y:.0f})"
            f"\\c{_wrap_override(colour)}{effects}}}{_rect(0, 0, w, h)}",
        ))

    # The light panels make the colour/edge comparison readable against the
    # moving grain plate. A dark lower strip gives timing and audio marks a
    # consistent plotting surface without leaving the common card identity.
    for panel_x in (left_x, right_x):
        shape(panel_x, panel_y, panel_w, panel_h, panel_fill)
        shape(
            panel_x,
            panel_y + panel_h - thickness,
            panel_w,
            thickness,
            mid,
        )

    for label, x in (
        ("REFERENCE", left_x + panel_w / 2),
        ("PROCESSED", right_x + panel_w / 2),
    ):
        events.append(_dialogue(
            "CardDisclosure",
            spec.duration,
            f"{{\\an{_ASS_MIDDLE_CENTER}\\pos({x:.0f},{label_y:.0f})"
            f"\\clip({layout.text_left:.0f},{layout.top:.0f},"
            f"{layout.text_right:.0f},{layout.top + layout.safe_h:.0f})"
            f"\\c{_wrap_override(light)}}}{label}",
        ))

    inner_x = panel_w * 0.075
    block_gap = panel_w * 0.045
    block_w = (panel_w - inner_x * 2 - block_gap) / 2
    block_y = panel_y + panel_h * 0.16
    block_h = panel_h * (0.43 if variant == "timing and audio" else 0.68)
    offset = max(2.0, layout.width * 0.00625)

    # Reference edges are deliberately crisp.
    shape(left_x + inner_x, block_y, block_w, block_h, accent)
    shape(left_x + inner_x + block_w + block_gap, block_y, block_w, block_h, blue)

    # Faint expected-position ghosts make the processed offset measurable,
    # while the coloured blocks show blur and colour drift. At 1920px the
    # offset is 12px, scaled proportionally on smaller renders.
    shape(
        right_x + inner_x,
        block_y,
        block_w,
        block_h,
        accent,
        alpha="90",
    )
    shape(
        right_x + inner_x + block_w + block_gap,
        block_y,
        block_w,
        block_h,
        blue,
        alpha="90",
    )
    shape(
        right_x + inner_x + offset,
        block_y + offset,
        block_w,
        block_h,
        shifted_red,
        blur=6,
    )
    shape(
        right_x + inner_x + block_w + block_gap + offset,
        block_y + offset,
        block_w,
        block_h,
        shifted_blue,
        blur=6,
    )

    if variant == "edge baseline":
        # A thin baseline under each pair keeps attention on crisp-versus-soft
        # edge behaviour before later cards introduce specific measurements.
        baseline_y = panel_y + panel_h * 0.88
        shape(left_x + inner_x, baseline_y, panel_w - inner_x * 2, thickness, mid)
        shape(
            right_x + inner_x,
            baseline_y,
            panel_w - inner_x * 2,
            thickness,
            mid,
            alpha="50",
        )
    elif variant == "processed change":
        # Two small deltas call out the changed processed output without adding
        # invented numbers to what is only a general explanatory diagram.
        delta_y = panel_y + panel_h * 0.88
        delta_w = (panel_w - inner_x * 2 - block_gap) / 2
        shape(right_x + inner_x, delta_y, delta_w, thickness * 2, shifted_red)
        shape(
            right_x + inner_x + delta_w + block_gap,
            delta_y,
            delta_w,
            thickness * 2,
            shifted_blue,
        )
    elif variant == "timing and audio":
        plot_y = panel_y + panel_h * 0.67
        plot_w = panel_w - inner_x * 2
        tick_h = panel_h * 0.075
        for panel_x, active_index in ((left_x, 2), (right_x, 4)):
            shape(panel_x + inner_x, plot_y, plot_w, thickness, mid)
            for index in range(5):
                tick_x = panel_x + inner_x + plot_w * index / 4
                shape(
                    tick_x - thickness / 2,
                    plot_y - tick_h / 2,
                    thickness,
                    tick_h,
                    accent if index == active_index else mid,
                )

        tone_y = panel_y + panel_h * 0.79
        tone_gap = panel_w * 0.025
        tone_w = (plot_w - tone_gap * 3) / 4
        reference_heights = (0.030, 0.065, 0.045, 0.075)
        processed_heights = (0.055, 0.035, 0.080, 0.050)
        for panel_x, heights, colour in (
            (left_x, reference_heights, blue),
            (right_x, processed_heights, shifted_red),
        ):
            for index, height_fraction in enumerate(heights):
                tone_h = panel_h * height_fraction
                shape(
                    panel_x + inner_x + index * (tone_w + tone_gap),
                    tone_y - tone_h,
                    tone_w,
                    tone_h,
                    colour,
                )
    elif variant == "automated flag":
        chip_gap_x = layout.safe_w * 0.025
        chip_gap_y = layout.safe_h * 0.020
        chip_w = (total_w - chip_gap_x) / 2
        chip_h = layout.safe_h * 0.070
        chip_top = panel_y + panel_h + layout.safe_h * 0.045
        for index, item in enumerate(spec.items[:4]):
            column = index % 2
            row = index // 2
            chip_x = left_x + column * (chip_w + chip_gap_x)
            chip_y = chip_top + row * (chip_h + chip_gap_y)
            shape(chip_x, chip_y, chip_w, chip_h, mid)
            shape(chip_x, chip_y, thickness * 2.2, chip_h, accent)
            events.append(_dialogue(
                "CardDisclosure",
                spec.duration,
                f"{{\\an{_ASS_MIDDLE_LEFT}\\pos({chip_x + chip_w * 0.06:.0f},"
                f"{chip_y + chip_h / 2:.0f})"
                f"\\clip({chip_x + chip_w * 0.035:.0f},{chip_y:.0f},"
                f"{chip_x + chip_w * 0.965:.0f},{chip_y + chip_h:.0f})"
                f"\\c{_wrap_override(light)}}}"
                f"{_wrapped_ass_text(item, chip_w * 0.84, disclosure_size, font_family=disclosure_font)}",
            ))

    # For the two-panel variants, retain the author's exact labels beneath
    # their matching panels. The flag variant uses all four authored items in
    # its status chips instead.
    if variant != "automated flag":
        item_y = panel_y + panel_h + layout.safe_h * 0.055
        item_size = disclosure_size
        for index, item in enumerate(spec.items[:2]):
            panel_x = left_x if index == 0 else right_x
            body = _wrapped_ass_text(
                item,
                panel_w * 0.84,
                item_size,
                font_family=disclosure_font,
            )
            events.append(_dialogue(
                "CardDisclosure",
                spec.duration,
                f"{{\\an{_ASS_MIDDLE_CENTER}\\pos({panel_x + panel_w / 2:.0f},"
                f"{item_y:.0f})"
                f"\\clip({panel_x + panel_w * 0.05:.0f},{layout.top:.0f},"
                f"{panel_x + panel_w * 0.95:.0f},{layout.top + layout.safe_h:.0f})"
                f"\\c{_wrap_override(light)}}}{body}",
            ))

    disclosure_y = layout.top + layout.safe_h * 0.875
    caveat_y = layout.top + layout.safe_h * 0.935
    for text, y, colour in (
        (_SIGNAL_DISCLOSURE, disclosure_y, light),
        (_SIGNAL_CAVEAT, caveat_y, accent),
    ):
        events.append(_dialogue(
            "CardDisclosure",
            spec.duration,
            f"{{\\an{_ASS_MIDDLE_CENTER}\\pos({layout.centre_x:.0f},{y:.0f})"
            f"\\clip({layout.text_left:.0f},{layout.top:.0f},"
            f"{layout.text_right:.0f},{layout.top + layout.safe_h:.0f})"
            f"\\c{_wrap_override(colour)}}}"
            f"{_wrapped_ass_text(text, layout.text_w, disclosure_size, font_family=disclosure_font)}",
        ))
    return events


def _comparison_events(spec, layout, palette, typography):
    variant = _signal_comparison_variant(spec.heading)
    if variant is not None:
        return _signal_comparison_events(
            spec,
            layout,
            palette,
            typography,
            variant,
        )

    events = _heading_events(spec, layout, palette, typography, layout.top + layout.safe_h * 0.14)
    if not spec.items:
        return events

    accent = palette.get("accent_red", "#E02020")
    light = palette.get("text_light", "#E0E0E0")
    divider_h = layout.safe_h * 0.42
    divider_y = layout.centre_y - divider_h / 2 + layout.height * 0.04
    thickness = max(2.0, layout.height * _RULE_THICKNESS_FRACTION * 0.8)
    body_size = max(1, round(layout.height * _BODY_SIZE_FRACTION))
    body_font, _findings = pick_font(typography)

    # A vertical rule only reads as "these two things are opposed" when there
    # are exactly two sides; three or more lay out as evenly spaced columns.
    if len(spec.items) == 2:
        events.append(_dialogue(
            "CardShape", spec.duration,
            f"{{\\an{_ASS_TOP_LEFT}\\pos({layout.centre_x:.0f},{divider_y:.0f})"
            f"\\c{_wrap_override(accent)}}}{_rect(0, 0, thickness, divider_h)}"))

    count = len(spec.items)
    column = layout.text_w / count
    for index, item in enumerate(spec.items):
        x = layout.text_left + column * (index + 0.5)
        padding = min(column * 0.10, layout.width * 0.025)
        clip_left = layout.text_left + column * index + padding
        clip_right = layout.text_left + column * (index + 1) - padding
        body = _wrapped_ass_text(
            item,
            max(1.0, clip_right - clip_left - padding * 0.5),
            body_size,
            font_family=body_font,
        )
        events.append(_dialogue(
            "CardBody", spec.duration,
            f"{{\\an{_ASS_MIDDLE_CENTER}\\pos({x:.0f},{layout.centre_y + layout.height * 0.05:.0f})"
            f"\\clip({clip_left:.0f},{layout.top:.0f},{clip_right:.0f},"
            f"{layout.top + layout.safe_h:.0f})"
            f"\\c{_wrap_override(light)}}}{body}"))
    return events


def _timeline_events(spec, layout, palette, typography):
    events = _heading_events(spec, layout, palette, typography, layout.top + layout.safe_h * 0.14)
    if not spec.items:
        return events

    accent = palette.get("accent_red", "#E02020")
    light = palette.get("text_light", "#E0E0E0")
    thickness = max(2.0, layout.height * _RULE_THICKNESS_FRACTION)
    axis_y = layout.centre_y + layout.height * 0.03
    axis_w = layout.text_w * 0.86
    axis_x = layout.centre_x - axis_w / 2
    tick_h = layout.height * 0.030
    body_size = max(1, round(layout.height * _BODY_SIZE_FRACTION))
    body_font, _findings = pick_font(typography)

    events.append(_dialogue(
        "CardShape", spec.duration,
        f"{{\\an{_ASS_TOP_LEFT}\\pos({axis_x:.0f},{axis_y:.0f})"
        f"\\c{_wrap_override(light)}}}{_rect(0, 0, axis_w, thickness * 0.6)}"))

    count = len(spec.items)
    for index, item in enumerate(spec.items):
        # A single point sits mid-axis rather than at its left end.
        fraction = 0.5 if count == 1 else index / (count - 1)
        x = axis_x + axis_w * fraction
        events.append(_dialogue(
            "CardShape", spec.duration,
            f"{{\\an{_ASS_TOP_LEFT}\\pos({x - thickness / 2:.0f},{axis_y - tick_h / 2:.0f})"
            f"\\c{_wrap_override(accent)}}}{_rect(0, 0, thickness, tick_h)}"))

        # Endpoint text grows inward from its tick. Centre anchoring made half
        # of a long first/last label extend beyond title-safe. Give every point
        # the non-overlapping region between the midpoints to its neighbours,
        # with a visible gutter at each shared boundary. The old 0.86 * interval
        # boxes overlapped one another by 72% for a two-point timeline: wrapping
        # each label independently kept it title-safe but still allowed the two
        # rendered labels to collide.
        if count == 1:
            alignment = _ASS_MIDDLE_CENTER
            clip_left = layout.text_left
            clip_right = layout.text_right
            text_x = layout.centre_x
        else:
            interval = axis_w / (count - 1)
            gutter = max(layout.width * 0.025, body_size * 0.75)
            region_left = (
                x
                if index == 0
                else x - interval / 2 + gutter / 2
            )
            region_right = (
                x
                if index == count - 1
                else x + interval / 2 - gutter / 2
            )
            edge_inset = max(
                layout.width * 0.012,
                (region_right - region_left) * 0.04,
            )
            if index == 0:
                alignment = _ASS_MIDDLE_LEFT
                clip_left = region_left
                clip_right = region_right
                text_x = clip_left + edge_inset
            elif index == count - 1:
                alignment = _ASS_MIDDLE_RIGHT
                clip_left = region_left
                clip_right = region_right
                text_x = clip_right - edge_inset
            else:
                alignment = _ASS_MIDDLE_CENTER
                clip_left = region_left
                clip_right = region_right
                text_x = x
        horizontal_inset = max(layout.width * 0.008, (clip_right - clip_left) * 0.025)
        if alignment == _ASS_MIDDLE_LEFT:
            available_width = clip_right - text_x - horizontal_inset
        elif alignment == _ASS_MIDDLE_RIGHT:
            available_width = text_x - clip_left - horizontal_inset
        else:
            available_width = (
                2
                * min(text_x - clip_left, clip_right - text_x)
                - horizontal_inset * 2
            )
        body = _wrapped_ass_text(
            item,
            max(1.0, available_width),
            body_size,
            font_family=body_font,
        )
        events.append(_dialogue(
            "CardBody", spec.duration,
            f"{{\\an{alignment}\\pos({text_x:.0f},{axis_y + layout.height * 0.06:.0f})"
            f"\\clip({clip_left:.0f},{layout.top:.0f},{clip_right:.0f},"
            f"{layout.top + layout.safe_h:.0f})"
            f"\\c{_wrap_override(light)}}}{body}"))
    return events


def _long_nonnumeric_stat_events(spec, figure, layout, palette, typography):
    """Readable heading/body fallback for prose misclassified as a stat."""
    heading = spec.heading.strip()
    body = " / ".join(spec.items).strip()

    # A heading-only prose stat still needs two levels. Derive a short,
    # content-bearing heading from its opening words rather than inventing a
    # generic label that tells the viewer nothing.
    if not body or body == heading:
        words = figure.split()
        split_at = min(5, max(2, len(words) // 3))
        if len(words) > split_at:
            heading = " ".join(words[:split_at])
            body = " ".join(words[split_at:])
        else:
            heading = figure
            body = ""

    fallback = CardSpec(
        kind="stat",
        heading=heading,
        duration=spec.duration,
        items=spec.items,
    )
    events = _heading_events(
        fallback,
        layout,
        palette,
        typography,
        layout.top + layout.safe_h * 0.18,
    )
    if body:
        light = palette.get("text_light", "#E0E0E0")
        body_size = max(1, round(layout.height * _BODY_SIZE_FRACTION))
        body_font, _findings = pick_font(typography)
        clip_left = layout.text_left + layout.text_w * 0.04
        clip_right = layout.text_right - layout.text_w * 0.04
        wrapped = _wrapped_ass_text(
            body,
            (clip_right - clip_left) * 0.94,
            body_size,
            font_family=body_font,
        )
        events.append(_dialogue(
            "CardBody",
            spec.duration,
            f"{{\\an{_ASS_MIDDLE_CENTER}\\pos({layout.centre_x:.0f},"
            f"{layout.centre_y + layout.height * 0.07:.0f})"
            f"\\clip({clip_left:.0f},{layout.top:.0f},{clip_right:.0f},"
            f"{layout.top + layout.safe_h:.0f})"
            f"\\c{_wrap_override(light)}}}{wrapped}",
        ))
    return events


def _stat_events(spec, layout, palette, typography):
    """The number carries the card; the heading captions it underneath."""
    light = palette.get("text_light", "#E0E0E0")
    accent = palette.get("accent_red", "#E02020")
    number = _first_number(spec.heading)
    figure = spec.items[0] if spec.items else (number or spec.heading)
    caption = spec.heading if spec.items or number else ""
    # The figure is already set large; repeating it in the caption below reads
    # as a duplicate rather than as a caption. Strip it, keep the words.
    if caption and number and not spec.items:
        remainder = caption.replace(number, "", 1).strip(" ,-")
        caption = remainder or ""

    # CardStat is intentionally huge and works for figures such as "400" or
    # "2.279 million". It is not a prose style. Classification can still land
    # here through words like "numbered"; only long, nonnumeric figures take
    # this fallback, leaving established numeric/short layouts unchanged.
    stat_size = max(1, round(layout.height * _STAT_SIZE_FRACTION))
    title_font, _findings = pick_title_font(typography)
    figure_text = _transform(figure, typography)
    if (
        not _first_number(figure)
        and _measured_line_width(
            figure_text,
            stat_size,
            font_family=title_font,
        ) > layout.text_w * 0.84
    ):
        return _long_nonnumeric_stat_events(
            spec, figure, layout, palette, typography
        )

    figure_wrapped = _wrapped_ass_text(
        figure_text,
        layout.text_w * 0.84,
        stat_size,
        font_family=title_font,
    )

    events = [_dialogue(
        "CardStat", spec.duration,
        f"{{\\an{_ASS_MIDDLE_CENTER}\\pos({layout.centre_x:.0f},"
        f"{layout.centre_y - layout.height * 0.04:.0f})"
        f"\\clip({layout.text_left:.0f},{layout.top:.0f},"
        f"{layout.text_right:.0f},{layout.top + layout.safe_h:.0f})"
        f"\\c{_wrap_override(light)}}}{figure_wrapped}")]

    thickness = max(2.0, layout.height * _RULE_THICKNESS_FRACTION)
    rule_w = layout.safe_w * 0.16
    events.append(_dialogue(
        "CardShape", spec.duration,
        f"{{\\an{_ASS_TOP_LEFT}\\pos({layout.centre_x - rule_w / 2:.0f},"
        f"{layout.centre_y + layout.height * 0.07:.0f})\\c{_wrap_override(accent)}}}"
        f"{_rect(0, 0, rule_w, thickness)}"))

    if caption:
        body_size = max(1, round(layout.height * _BODY_SIZE_FRACTION))
        body_font, _findings = pick_font(typography)
        events.append(_dialogue(
            "CardBody", spec.duration,
            f"{{\\an{_ASS_MIDDLE_CENTER}\\pos({layout.centre_x:.0f},"
            f"{layout.centre_y + layout.height * 0.13:.0f})"
            f"\\clip({layout.text_left:.0f},{layout.top:.0f},"
            f"{layout.text_right:.0f},{layout.top + layout.safe_h:.0f})"
            f"\\c{_wrap_override(light)}}}"
            f"{_wrapped_ass_text(caption, layout.text_w * 0.88, body_size, font_family=body_font)}"))
    return events


def _document_events(spec, layout, palette, typography):
    """Heading over a framed block, standing in for an excerpt or a scan."""
    events = _heading_events(spec, layout, palette, typography, layout.top + layout.safe_h * 0.12)
    light = palette.get("text_light", "#E0E0E0")
    mid = palette.get("text_mid", "#404040")
    thickness = max(2.0, layout.height * _RULE_THICKNESS_FRACTION * 0.7)

    frame_w = layout.safe_w * 0.70
    frame_h = layout.safe_h * 0.46
    frame_x = layout.centre_x - frame_w / 2
    frame_y = layout.centre_y - frame_h / 2 + layout.height * 0.05

    for x, y, w, h in (
        (frame_x, frame_y, frame_w, thickness),
        (frame_x, frame_y + frame_h - thickness, frame_w, thickness),
        (frame_x, frame_y, thickness, frame_h),
        (frame_x + frame_w - thickness, frame_y, thickness, frame_h),
    ):
        events.append(_dialogue(
            "CardShape", spec.duration,
            f"{{\\an{_ASS_TOP_LEFT}\\pos({x:.0f},{y:.0f})"
            f"\\c{_wrap_override(mid)}}}{_rect(0, 0, w, h)}"))

    if spec.disclosure.strip():
        disclosure_size = max(
            1, round(layout.height * _DISCLOSURE_SIZE_FRACTION)
        )
        disclosure = _wrapped_ass_text(
            _transform(spec.disclosure, typography),
            frame_w * 0.84,
            disclosure_size,
            font_family=pick_font(typography)[0],
        )
        events.append(_dialogue(
            "CardDisclosure", spec.duration,
            f"{{\\an{_ASS_TOP_LEFT}\\pos({frame_x + frame_w * 0.05:.0f},"
            f"{frame_y + frame_h * 0.06:.0f})"
            f"\\clip({frame_x + frame_w * 0.035:.0f},{frame_y:.0f},"
            f"{frame_x + frame_w * 0.965:.0f},{frame_y + frame_h:.0f})}}"
            f"{disclosure}"))

    # Only fill the frame when there is something to put in it. Echoing the
    # heading inside its own frame reads as a rendering mistake, not a design.
    if spec.items:
        body = " / ".join(spec.items)
        body_size = max(1, round(layout.height * _BODY_SIZE_FRACTION))
        body_font, _findings = pick_font(typography)
        events.append(_dialogue(
            "CardBody", spec.duration,
            f"{{\\an{_ASS_MIDDLE_CENTER}\\pos({layout.centre_x:.0f},{frame_y + frame_h / 2:.0f})"
            f"\\clip({frame_x + frame_w * 0.05:.0f},{frame_y:.0f},"
            f"{frame_x + frame_w * 0.95:.0f},{frame_y + frame_h:.0f})"
            f"\\c{_wrap_override(light)}}}"
            f"{_wrapped_ass_text(body, frame_w * 0.82, body_size, font_family=body_font)}"))
    return events


def _montage_events(spec, layout, palette, typography):
    """Evenly divided panels -- the frame a multi-clip sequence drops into."""
    events = _heading_events(spec, layout, palette, typography, layout.top + layout.safe_h * 0.12)
    if not spec.items:
        return events

    mid = palette.get("text_mid", "#404040")
    light = palette.get("text_light", "#E0E0E0")
    thickness = max(2.0, layout.height * _RULE_THICKNESS_FRACTION * 0.7)
    body_size = max(1, round(layout.height * _BODY_SIZE_FRACTION))
    body_font, _findings = pick_font(typography)

    count = len(spec.items)
    gap = layout.safe_w * 0.02
    panel_w = (layout.safe_w - gap * (count - 1)) / count
    panel_h = layout.safe_h * 0.44
    panel_y = layout.centre_y - panel_h / 2 + layout.height * 0.05

    for index, item in enumerate(spec.items):
        px = layout.left + index * (panel_w + gap)
        for x, y, w, h in (
            (px, panel_y, panel_w, thickness),
            (px, panel_y + panel_h - thickness, panel_w, thickness),
            (px, panel_y, thickness, panel_h),
            (px + panel_w - thickness, panel_y, thickness, panel_h),
        ):
            events.append(_dialogue(
                "CardShape", spec.duration,
                f"{{\\an{_ASS_TOP_LEFT}\\pos({x:.0f},{y:.0f})"
                f"\\c{_wrap_override(mid)}}}{_rect(0, 0, w, h)}"))
        events.append(_dialogue(
            "CardBody", spec.duration,
            f"{{\\an{_ASS_MIDDLE_CENTER}\\pos({px + panel_w / 2:.0f},"
            f"{panel_y + panel_h / 2:.0f})"
            f"\\clip({px + panel_w * 0.06:.0f},{panel_y:.0f},"
            f"{px + panel_w * 0.94:.0f},{panel_y + panel_h:.0f})"
            f"\\c{_wrap_override(light)}}}"
            f"{_wrapped_ass_text(item, panel_w * 0.78, body_size, font_family=body_font)}"))
    return events


_NUMBER_RE = re.compile(r"\d[\d,.]*\s*(?:percent|%|crore|lakh|million|thousand)?", re.I)


def _first_number(text: str) -> str:
    match = _NUMBER_RE.search(text or "")
    return match.group(0).strip() if match else ""


_EVENT_BUILDERS = {
    "callout": _callout_events,
    "label": _label_events,
    "checklist": _checklist_events,
    "comparison": _comparison_events,
    "timeline": _timeline_events,
    "stat": _stat_events,
    "document": _document_events,
    "montage": _montage_events,
}


def card_ass(
    spec: CardSpec, typography: dict, palette: dict, width: int = 1920, height: int = 1080
) -> str:
    """The ASS document for one card."""
    signal_errors = _signal_comparison_findings(spec)
    if signal_errors:
        raise ValueError(signal_errors[0].message)
    layout = _Layout(width=width, height=height)
    events = _EVENT_BUILDERS[spec.kind](spec, layout, palette, typography)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        *_styles(typography, palette, width, height),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *events,
    ]
    return "\n".join(lines) + "\n"


def _card_frame_counts(duration: float, fps: int) -> tuple[int, int]:
    """Return ``(authored, encoded)`` frames for one generated card."""

    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise TypeError("Card duration must be a number")
    if not math.isfinite(float(duration)) or duration <= 0:
        raise ValueError("Card duration must be positive and finite")
    if isinstance(fps, bool) or not isinstance(fps, int) or fps <= 0:
        raise ValueError("Card fps must be a positive integer")

    # Keep the established nearest-frame (half-even) authored timing. The
    # independently rounded Resolve endpoint is covered by the duplicate
    # terminal handle, not by extending what the card author asked to show.
    authored_frame_count = max(
        1,
        int(
            (Decimal(str(duration)) * Decimal(fps)).to_integral_value(
                rounding=ROUND_HALF_EVEN
            )
        ),
    )
    return (
        authored_frame_count,
        authored_frame_count + CARD_SAFE_TRAILING_FRAMES,
    )


def build_card(
    spec: CardSpec,
    out_path: Path,
    typography: dict,
    palette: dict,
    grade: dict,
    work_dir: Path,
    *,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
) -> Path:
    """Render one card plus one safe terminal media-handle frame to MP4.

    A graded `plates.build_plate` background with `card_ass` burned onto it by
    `subtitles.burn` -- so the card carries the same grain and grade as the
    footage it cuts against, and the ASS path never reaches an ffmpeg filter
    argument as an absolute Windows path. ``spec.duration`` still owns the ASS
    event timing; the additional encoded frame is a clone of the final authored
    frame solely for Resolve's independently rounded source endpoint.
    """
    out_path = Path(out_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    authored_frame_count, _encoded_frame_count = _card_frame_counts(
        spec.duration, fps
    )

    background = work_dir / f"{out_path.stem}-bg.mp4"
    build_plate(
        PlateSpec(kind=CARD_PLATE_KIND, duration=spec.duration, fps=fps,
                  width=width, height=height),
        background,
        grade=grade,
    )

    ass_path = work_dir / f"{out_path.stem}.ass"
    ass_path.write_text(card_ass(spec, typography, palette, width, height), encoding="utf-8")

    try:
        burn(
            background,
            ass_path,
            out_path,
            authored_frame_count=authored_frame_count,
            safe_trailing_frames=CARD_SAFE_TRAILING_FRAMES,
            fps=fps,
        )
    except RuntimeError:
        if out_path.exists():
            out_path.unlink()
        raise
    return out_path

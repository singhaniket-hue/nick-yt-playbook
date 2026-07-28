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

import re
from dataclasses import dataclass, field
from pathlib import Path

from rabbithole.graphics import pick_title_font
from rabbithole.slots import Slot
from rabbithole.sources.plates import PlateSpec, build_plate
from rabbithole.subtitles import (
    _escape_ass_text,
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
# to 1/1.30 = 76.9% of the frame (see render._DETAIL_ZOOM); this sits under
# that so a card survives the tightest framing with margin, rather than being
# clipped exactly on the cuts meant to emphasise it.
TITLE_SAFE_FRACTION = 0.74

# The background every card is drawn onto. 'grain' rather than 'black': a card
# on pure black reads as a slide, a card on moving grain reads as part of the
# same graded film as the footage around it.
CARD_PLATE_KIND = "grain"

_HEADING_SIZE_FRACTION = 0.062
# Body text is read, not glanced at -- a card holds for 3-12 seconds and the
# viewer has to finish it. Rendered at 0.040 the items were legible but weak
# against the heading; 0.052 reads at a glance without crowding six of them.
_BODY_SIZE_FRACTION = 0.052
_STAT_SIZE_FRACTION = 0.150
_RULE_THICKNESS_FRACTION = 0.0045

_BOLD_WEIGHT_THRESHOLD = 600
_ASS_TOP_LEFT = 7
_ASS_MIDDLE_CENTER = 5

# Longest run of items any template lays out. Beyond this the layout stops
# being legible at 1080p, so extra items are dropped and reported rather than
# silently overflowing off the safe area.
MAX_ITEMS = 6

# Ordered longest-first so 'split-screen' is matched before 'split', and
# checked as substrings of the lowercased detail. Order between groups is
# significant: a detail reading "timeline comparison" is a timeline first.
_CLASSIFY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("timeline", ("timeline", "chronology", "calendar", "growth curve", " to ")),
    ("comparison", ("split-screen", "side-by-side", "versus", " vs ", "compared",
                    "comparison", "mismatch", "two headlines")),
    ("montage", ("montage", "three-clip", "three-part", "four-element", "panels",
                 "three panels")),
    ("checklist", ("checklist", "criteria", "demands", "points", "list")),
    ("document", ("excerpt", "order", "overlay", "paper", "floor plan", "form")),
    ("stat", ("percent", "crore", "lakh", "million", "thousand", "figure",
              "number", "count", "statistic")),
    ("callout", ("callout", "label", "quote", "manifesto text", "claims",
                 "statement", "icon", "symbol")),
)


@dataclass(frozen=True)
class CardSpec:
    """One card to draw.

    `heading` is always shown. `items` is template-specific: bullets for
    `checklist`, the two sides of a `comparison`, points on a `timeline`,
    panels of a `montage`. An empty `items` is valid -- every template
    degrades to a heading-only card rather than failing.
    """

    kind: str
    heading: str
    duration: float
    items: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in CARD_KINDS:
            raise ValueError(
                f"Unknown card kind {self.kind!r}; valid kinds: {', '.join(CARD_KINDS)}"
            )
        if self.duration <= 0:
            raise ValueError(f"Card duration must be positive, got {self.duration}")


def classify(detail: str) -> str:
    """The card archetype a slot detail asks for.

    Substring matching on the lowercased detail, in `_CLASSIFY_RULES` order.
    Falls back to `label` -- a designed card carrying the detail's own words --
    rather than raising, because an unrecognised detail should still put
    something intentional on screen.
    """
    lowered = f" {detail.lower().strip()} "
    for kind, needles in _CLASSIFY_RULES:
        if any(needle in lowered for needle in needles):
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

    return CardSpec(kind=kind, heading=heading, duration=duration, items=tuple(items)), findings


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


def _styles(typography: dict, palette: dict, width: int, height: int) -> list[str]:
    title_font, _ = pick_title_font(typography)
    body_font, _ = pick_font(typography)
    weight = typography.get("title_card", {}).get("weight", 400)
    tracking = float(typography.get("title_card", {}).get("tracking", 0.0) or 0.0)
    bold = -1 if weight >= _BOLD_WEIGHT_THRESHOLD else 0

    heading_size = max(1, round(height * _HEADING_SIZE_FRACTION))
    body_size = max(1, round(height * _BODY_SIZE_FRACTION))
    stat_size = max(1, round(height * _STAT_SIZE_FRACTION))
    light = ass_colour(palette.get("text_light", "#E0E0E0"))

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
    if spec.heading.strip():
        heading = _escape_ass_text(_transform(spec.heading, typography))
        events.append(_dialogue(
            "CardHead", spec.duration,
            f"{{\\an{_ASS_MIDDLE_CENTER}\\pos({layout.centre_x:.0f},{y:.0f})"
            f"\\c{_wrap_override(fill)}}}{heading}"))

    # The rule is drawn either way: with a heading it underlines it, without one
    # it is the card's only fixed anchor, which keeps a heading-less comparison
    # or timeline visually part of the same set as every other card.
    events.append(_dialogue(
        "CardShape", spec.duration,
        f"{{\\an{_ASS_TOP_LEFT}\\pos({layout.centre_x - rule_w / 2:.0f},"
        f"{y + layout.height * 0.055:.0f})\\c{_wrap_override(rule)}}}"
        f"{_rect(0, 0, rule_w, thickness)}"))
    return events


def _callout_events(spec, layout, palette, typography):
    events = _heading_events(spec, layout, palette, typography, layout.centre_y)
    if spec.items:
        body = _escape_ass_text(" / ".join(spec.items))
        events.append(_dialogue(
            "CardBody", spec.duration,
            f"{{\\an{_ASS_MIDDLE_CENTER}\\pos({layout.centre_x:.0f},"
            f"{layout.centre_y + layout.height * 0.13:.0f})}}{body}"))
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

    # Centre the item block in the space below the heading rather than hanging
    # it from a fixed offset: with two items a fixed start leaves the card
    # visibly bottom-empty, and with six it crowds the lower safe edge.
    step = min(layout.height * 0.095, (layout.safe_h * 0.6) / max(1, len(spec.items)))
    block_h = step * max(0, len(spec.items) - 1)
    area_top = layout.top + layout.safe_h * 0.30
    area_bottom = layout.top + layout.safe_h * 0.94
    first = max(area_top, (area_top + area_bottom - block_h) / 2)
    text_x = layout.left + layout.safe_w * 0.16

    for index, item in enumerate(spec.items):
        y = first + index * step
        events.append(_dialogue(
            "CardShape", spec.duration,
            f"{{\\an{_ASS_TOP_LEFT}\\pos({layout.left + layout.safe_w * 0.10:.0f},{y:.0f})"
            f"\\c{_wrap_override(accent)}}}{_rect(0, 0, marker, thickness * 1.6)}"))
        events.append(_dialogue(
            "CardBody", spec.duration,
            f"{{\\an{_ASS_TOP_LEFT}\\pos({text_x:.0f},{y - layout.height * 0.020:.0f})"
            f"\\c{_wrap_override(light)}}}{_escape_ass_text(item)}"))
    return events


def _comparison_events(spec, layout, palette, typography):
    events = _heading_events(spec, layout, palette, typography, layout.top + layout.safe_h * 0.14)
    if not spec.items:
        return events

    accent = palette.get("accent_red", "#E02020")
    light = palette.get("text_light", "#E0E0E0")
    divider_h = layout.safe_h * 0.42
    divider_y = layout.centre_y - divider_h / 2 + layout.height * 0.04
    thickness = max(2.0, layout.height * _RULE_THICKNESS_FRACTION * 0.8)

    # A vertical rule only reads as "these two things are opposed" when there
    # are exactly two sides; three or more lay out as evenly spaced columns.
    if len(spec.items) == 2:
        events.append(_dialogue(
            "CardShape", spec.duration,
            f"{{\\an{_ASS_TOP_LEFT}\\pos({layout.centre_x:.0f},{divider_y:.0f})"
            f"\\c{_wrap_override(accent)}}}{_rect(0, 0, thickness, divider_h)}"))

    count = len(spec.items)
    column = layout.safe_w / count
    for index, item in enumerate(spec.items):
        x = layout.left + column * (index + 0.5)
        events.append(_dialogue(
            "CardBody", spec.duration,
            f"{{\\an{_ASS_MIDDLE_CENTER}\\pos({x:.0f},{layout.centre_y + layout.height * 0.05:.0f})"
            f"\\c{_wrap_override(light)}}}{_escape_ass_text(item)}"))
    return events


def _timeline_events(spec, layout, palette, typography):
    events = _heading_events(spec, layout, palette, typography, layout.top + layout.safe_h * 0.14)
    if not spec.items:
        return events

    accent = palette.get("accent_red", "#E02020")
    light = palette.get("text_light", "#E0E0E0")
    thickness = max(2.0, layout.height * _RULE_THICKNESS_FRACTION)
    axis_y = layout.centre_y + layout.height * 0.03
    axis_w = layout.safe_w * 0.86
    axis_x = layout.centre_x - axis_w / 2
    tick_h = layout.height * 0.030

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
        events.append(_dialogue(
            "CardBody", spec.duration,
            f"{{\\an{_ASS_MIDDLE_CENTER}\\pos({x:.0f},{axis_y + layout.height * 0.06:.0f})"
            f"\\c{_wrap_override(light)}}}{_escape_ass_text(item)}"))
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

    events = [_dialogue(
        "CardStat", spec.duration,
        f"{{\\an{_ASS_MIDDLE_CENTER}\\pos({layout.centre_x:.0f},"
        f"{layout.centre_y - layout.height * 0.04:.0f})"
        f"\\c{_wrap_override(light)}}}{_escape_ass_text(_transform(figure, typography))}")]

    thickness = max(2.0, layout.height * _RULE_THICKNESS_FRACTION)
    rule_w = layout.safe_w * 0.16
    events.append(_dialogue(
        "CardShape", spec.duration,
        f"{{\\an{_ASS_TOP_LEFT}\\pos({layout.centre_x - rule_w / 2:.0f},"
        f"{layout.centre_y + layout.height * 0.07:.0f})\\c{_wrap_override(accent)}}}"
        f"{_rect(0, 0, rule_w, thickness)}"))

    if caption:
        events.append(_dialogue(
            "CardBody", spec.duration,
            f"{{\\an{_ASS_MIDDLE_CENTER}\\pos({layout.centre_x:.0f},"
            f"{layout.centre_y + layout.height * 0.13:.0f})"
            f"\\c{_wrap_override(light)}}}{_escape_ass_text(caption)}"))
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

    # Only fill the frame when there is something to put in it. Echoing the
    # heading inside its own frame reads as a rendering mistake, not a design.
    if spec.items:
        body = " / ".join(spec.items)
        events.append(_dialogue(
            "CardBody", spec.duration,
            f"{{\\an{_ASS_MIDDLE_CENTER}\\pos({layout.centre_x:.0f},{frame_y + frame_h / 2:.0f})"
            f"\\c{_wrap_override(light)}}}{_escape_ass_text(body)}"))
    return events


def _montage_events(spec, layout, palette, typography):
    """Evenly divided panels -- the frame a multi-clip sequence drops into."""
    events = _heading_events(spec, layout, palette, typography, layout.top + layout.safe_h * 0.12)
    if not spec.items:
        return events

    mid = palette.get("text_mid", "#404040")
    light = palette.get("text_light", "#E0E0E0")
    thickness = max(2.0, layout.height * _RULE_THICKNESS_FRACTION * 0.7)

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
            f"{panel_y + panel_h / 2:.0f})\\c{_wrap_override(light)}}}"
            f"{_escape_ass_text(item)}"))
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
    """Render one card to an MP4 spanning `spec.duration`.

    A graded `plates.build_plate` background with `card_ass` burned onto it by
    `subtitles.burn` -- so the card carries the same grain and grade as the
    footage it cuts against, and the ASS path never reaches an ffmpeg filter
    argument as an absolute Windows path.
    """
    out_path = Path(out_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

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
        burn(background, ass_path, out_path)
    except RuntimeError:
        if out_path.exists():
            out_path.unlink()
        raise
    return out_path

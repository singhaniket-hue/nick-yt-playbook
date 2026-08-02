"""Graphic cards.

Real ffmpeg renders and real pixel measurements, not clean exit codes. This
module's whole reason to exist is that 81 slots were planned as handled and
silently produced nothing, so "it ran" is precisely the evidence not worth
having -- see graphics.py's own note that a filter which draws nothing exits 0.
"""

from __future__ import annotations

import json
import subprocess
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import rabbithole.cards as cards_module
from rabbithole.cards import (
    CARD_KINDS,
    GROUPED_ROW_SEPARATOR,
    MAX_ITEMS,
    TEXT_SAFE_INSET_FRACTION,
    TITLE_SAFE_FRACTION,
    CardSpec,
    _measured_line_width,
    build_card,
    card_ass,
    classify,
    group_same_heading_specs,
    parse_detail,
    production_note_reason,
    spec_for_slot,
)
from rabbithole.edl import Cut
from rabbithole.jsonio import read_json
from rabbithole.render import FRAMINGS, cut_segment
from rabbithole.slots import Slot
from rabbithole.sources.plates import load_grade
from rabbithole.subtitles import pick_font

REPO_ROOT = Path(__file__).resolve().parent.parent
STYLE_DIR = REPO_ROOT / "style"


@pytest.fixture(scope="module")
def style():
    return (
        read_json(STYLE_DIR / "typography.json"),
        read_json(STYLE_DIR / "palette.json"),
        load_grade(STYLE_DIR),
    )


def _frame(video: Path, at: float = 0.8) -> np.ndarray:
    png = video.with_name(video.stem + f"-{at}.png")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(video),
         "-ss", str(at), "-frames:v", "1", str(png)],
        check=True,
    )
    return np.asarray(Image.open(png).convert("L")).astype(np.float64)


def _rgb_frame(video: Path, at: float = 0.8) -> np.ndarray:
    png = video.with_name(video.stem + f"-rgb-{at}.png")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(video),
         "-ss", str(at), "-frames:v", "1", str(png)],
        check=True,
    )
    return np.asarray(Image.open(png).convert("RGB"))


def _ink_fraction(frame: np.ndarray, threshold: float = 110.0) -> float:
    """Share of pixels bright enough to be text rather than graded plate."""
    return float((frame > threshold).mean())


# --- classification --------------------------------------------------------------


@pytest.mark.parametrize(
    "detail,expected",
    [
        ("split-screen, two headlines compared", "comparison"),
        ("33 percent versus 50 percent comparison", "comparison"),
        ("timeline, pandrah May to solah May", "timeline"),
        ("calendar, cancellation to retest span", "timeline"),
        ("checklist, four criteria", "checklist"),
        ("NTA security checklist", "checklist"),
        ("Supreme Court order excerpt", "document"),
        ("three-clip summary, all false or misattributed", "montage"),
        ("UAPA clause callout", "callout"),
        ('"Godi media" label callout', "callout"),
    ],
)
def test_classify_maps_representative_episode_details(detail, expected):
    """Every case here represents wording accepted in a marked script."""
    assert classify(detail) == expected


def test_classify_falls_back_rather_than_raising():
    assert classify("something nobody anticipated") == "label"


@pytest.mark.parametrize(
    "detail",
    [
        "red-blue rectangles settle into a labelled test grid and stop moving",
        "an unlabelled diagnostic frame",
        "account totals",
        "platform overview",
        "iconic geometry",
    ],
)
def test_classify_does_not_match_cues_inside_other_words(detail):
    assert classify(detail) == "label"


@pytest.mark.parametrize(
    "detail",
    [
        "evidence label: anomaly documented | meaning unknown",
        "three labels confirmed date | playful reference | intent uncertain",
    ],
)
def test_classify_keeps_explicit_singular_and_plural_label_directives(detail):
    assert classify(detail) == "callout"


def test_webdriver_s322_motion_direction_falls_back_to_a_blocked_label_card():
    detail = "red-blue rectangles settle into a labelled test grid and stop moving"
    slot = Slot(
        slot_id="s322",
        kind="graphic",
        detail=detail,
        start=1007.928,
        end=1010.981,
        queries=(),
        marker_word_index=2500,
    )

    spec, findings = spec_for_slot(slot)

    assert findings == []
    assert spec.kind == "label"
    assert spec.heading == detail
    assert spec.items == ()
    assert spec.duration == pytest.approx(3.053)
    assert "unstructured label" in production_note_reason(detail, spec)


def test_every_classified_kind_has_an_event_builder():
    """A kind with no builder would raise KeyError at render time."""
    from rabbithole.cards import _EVENT_BUILDERS

    for kind in CARD_KINDS:
        assert kind in _EVENT_BUILDERS, kind


# --- detail parsing --------------------------------------------------------------


def test_explicit_items_are_used():
    spec, _ = parse_detail("criteria: unemployed | lazy | online", 4.0)
    assert spec.items == ("unemployed", "lazy", "online")


def test_signal_comparison_marker_remains_a_comparison_with_explicit_items():
    spec, findings = parse_detail(
        "signal comparison - processed change: "
        "REFERENCE - SHARP EDGE | PROCESSED - BLUR + COLOUR SHIFT",
        4.0,
    )

    assert findings == []
    assert spec.kind == "comparison"
    assert spec.heading == "signal comparison - processed change"
    assert spec.items == (
        "REFERENCE - SHARP EDGE",
        "PROCESSED - BLUR + COLOUR SHIFT",
    )


def test_signal_comparison_rejects_an_undocumented_variant():
    spec, findings = parse_detail(
        "signal comparison - secret decoder: REFERENCE | PROCESSED",
        4.0,
    )

    assert spec.heading == "signal comparison - secret decoder"
    assert any(
        finding.severity == "error"
        and "unsupported variant 'secret decoder'" in finding.message
        and "edge baseline" in finding.message
        and "automated flag" in finding.message
        for finding in findings
    )


@pytest.mark.parametrize(
    "detail,expected_count,actual_count",
    [
        (
            "signal comparison - edge baseline: REFERENCE",
            2,
            1,
        ),
        (
            "signal comparison - processed change: "
            "REFERENCE | PROCESSED | EXTRA",
            2,
            3,
        ),
        (
            "signal comparison - timing and audio: "
            "REFERENCE | PROCESSED | EXTRA",
            2,
            3,
        ),
        (
            "signal comparison - automated flag: EDGE | COLOUR | TIMING",
            4,
            3,
        ),
    ],
)
def test_signal_comparison_enforces_variant_item_cardinality(
    detail, expected_count, actual_count
):
    _spec, findings = parse_detail(detail, 4.0)

    assert any(
        finding.severity == "error"
        and f"requires exactly {expected_count} authored items" in finding.message
        and f"supplies {actual_count}" in finding.message
        for finding in findings
    )


def test_card_ass_refuses_an_invalid_signal_spec(style):
    typo, pal, _ = style
    spec = CardSpec(
        kind="comparison",
        heading="signal comparison - unknown",
        duration=3.0,
        items=("REFERENCE", "PROCESSED"),
    )

    with pytest.raises(ValueError, match="unsupported variant"):
        card_ass(spec, typo, pal)


def test_unstructured_content_after_a_colon_is_kept_as_one_item():
    """It used to be discarded: no pipe run and no shorthand meant no items, so
    'paragraph fourteen' -- the only thing the author asked to be shown --
    never reached the card."""
    spec, _ = parse_detail("order excerpt: paragraph fourteen", 4.0)
    assert spec.items == ("paragraph fourteen",)


def test_an_archetype_only_heading_is_dropped_when_items_can_carry_the_card():
    """'COMPARISON' on screen is a caption describing the card's own format."""
    spec, _ = parse_detail("33 percent versus 50 percent comparison", 4.0)
    assert spec.heading == ""
    assert spec.items == ("33 percent", "50 percent")


def test_an_archetype_only_heading_is_kept_when_there_is_nothing_else():
    """Dropping it here would render a completely empty card."""
    spec, _ = parse_detail("checklist, four criteria", 4.0)
    assert spec.heading
    assert spec.items == ()


def test_the_archetype_word_is_stripped_from_a_heading():
    spec, _ = parse_detail("Supreme Court order excerpt", 4.0)
    assert spec.heading == "Supreme Court order"


def test_versus_shorthand_becomes_two_sides():
    spec, _ = parse_detail("33 percent versus 50 percent", 4.0)
    assert spec.items == ("33 percent", "50 percent")


def test_to_shorthand_becomes_two_timeline_points():
    spec, _ = parse_detail("timeline, pandrah May to solah May", 4.0)
    assert spec.kind == "timeline"
    assert spec.items == ("pandrah May", "solah May")


def test_trailing_archetype_word_is_not_treated_as_content():
    """'33 percent versus 50 percent comparison' must not yield a side literally
    reading '50 percent comparison'."""
    spec, _ = parse_detail("33 percent versus 50 percent comparison", 4.0)
    assert spec.items == ("33 percent", "50 percent")


def test_a_whole_detail_shorthand_does_not_duplicate_itself_as_the_heading():
    spec, _ = parse_detail("33 percent versus 50 percent comparison", 4.0)
    assert spec.heading not in spec.items


def test_explicit_items_are_never_silently_dropped():
    """An unrecognised archetype used to classify as `label`, which renders a
    heading only -- so all three items here vanished without a word."""
    spec, _ = parse_detail("open threads: surveillance | miscaption | funding", 4.0)
    assert spec.items == ("surveillance", "miscaption", "funding")
    assert spec.kind != "label"


def test_too_many_items_is_capped_and_reported(caplog):
    detail = "list: " + " | ".join(str(i) for i in range(MAX_ITEMS + 4))
    spec, findings = parse_detail(detail, 4.0)
    assert len(spec.items) == MAX_ITEMS
    assert any(f.severity == "warning" and "items" in f.message for f in findings)


def test_a_comparison_with_no_items_warns():
    _, findings = parse_detail("split-screen, two headlines compared", 4.0)
    assert any(f.severity == "warning" for f in findings)


def test_a_callout_with_no_items_does_not_warn():
    """Heading-only is the intended design for a callout, not a shortfall."""
    _, findings = parse_detail("UAPA clause callout", 4.0)
    assert findings == []


def test_headingless_callout_promotes_its_first_authored_item():
    """Regression for s212: generic `callout` was stripped and left a nearly
    blank frame with all useful text compressed into one small body line."""
    spec, findings = parse_detail(
        "three labels confirmed date | playful reference | broader intent uncertain",
        4.0,
    )

    assert findings == []
    assert spec.kind == "callout"
    assert spec.heading == "three labels confirmed date"
    assert spec.items == ("playful reference", "broader intent uncertain")


@pytest.mark.parametrize(
    "detail",
    [
        "UAPA clause callout",
        "Reuters imagery still",
        "headline zoom",
        "generic profile screenshot",
        "Signal Room Archive",
    ],
)
def test_label_only_production_notes_are_identified_for_final_qa(detail):
    spec, _ = parse_detail(detail, 4.0)

    assert production_note_reason(detail, spec)


@pytest.mark.parametrize(
    "detail",
    [
        "criteria: unemployed | lazy | chronically online",
        "order excerpt: paragraph fourteen",
        "33 percent versus 50 percent comparison",
        "66 lakh followers",
    ],
)
def test_authored_card_content_is_not_misidentified_as_a_production_note(detail):
    spec, _ = parse_detail(detail, 4.0)

    assert production_note_reason(detail, spec) == ""


def test_spec_for_slot_spans_the_whole_hold():
    slot = Slot(slot_id="s001", kind="graphic", detail="UAPA clause callout",
                start=10.0, end=22.5, queries=(), marker_word_index=0)
    spec, _ = spec_for_slot(slot)
    assert spec.duration == pytest.approx(12.5)


def test_spec_for_slot_rejects_a_non_graphic_slot():
    slot = Slot(slot_id="s001", kind="archival", detail="x", start=0.0, end=1.0,
                queries=(), marker_word_index=0)
    with pytest.raises(ValueError, match="graphic slot"):
        spec_for_slot(slot)


# --- CardSpec validation ---------------------------------------------------------


def test_unknown_kind_is_rejected_naming_it():
    with pytest.raises(ValueError, match="lava-lamp"):
        CardSpec(kind="lava-lamp", heading="x", duration=1.0)


def test_nonpositive_duration_is_rejected():
    with pytest.raises(ValueError, match="positive"):
        CardSpec(kind="callout", heading="x", duration=0.0)


@pytest.mark.parametrize("active_index", [-1, 2])
def test_active_item_index_must_name_an_existing_row(active_index):
    with pytest.raises(ValueError, match="existing item"):
        CardSpec(
            kind="checklist",
            heading="x",
            duration=1.0,
            items=("first", "second"),
            active_item_index=active_index,
        )


def test_active_item_index_rejects_boolean_even_though_bool_is_an_int():
    with pytest.raises(TypeError, match="integer or None"):
        CardSpec(
            kind="checklist",
            heading="x",
            duration=1.0,
            items=("first",),
            active_item_index=True,
        )


def test_active_spoken_rows_refuse_more_than_six_items():
    with pytest.raises(ValueError, match="at most 6 readable"):
        CardSpec(
            kind="checklist",
            heading="x",
            duration=1.0,
            items=tuple(f"row {index}" for index in range(MAX_ITEMS + 1)),
            active_item_index=0,
        )


# --- same-heading consolidation -------------------------------------------------


def test_same_heading_specs_share_rows_and_track_each_original_slot():
    source = [
        (
            "s008",
            CardSpec(
                kind="checklist",
                heading="TEST FRAME RECONSTRUCTION",
                duration=2.6,
                items=("SHAPES MOVE", "TEMPLATE STAYS"),
            ),
        ),
        (
            "s009",
            CardSpec(
                kind="checklist",
                heading=" test   frame reconstruction ",
                duration=3.5,
                items=("TONE CHANGES", "NO VERIFIED MESSAGE"),
            ),
        ),
    ]

    grouped = group_same_heading_specs(source)
    expected_rows = (
        GROUPED_ROW_SEPARATOR.join(("SHAPES MOVE", "TEMPLATE STAYS")),
        GROUPED_ROW_SEPARATOR.join(("TONE CHANGES", "NO VERIFIED MESSAGE")),
    )

    assert list(grouped) == ["s008", "s009"]
    assert grouped["s008"].items == expected_rows
    assert grouped["s009"].items == expected_rows
    assert grouped["s008"].active_item_index == 0
    assert grouped["s009"].active_item_index == 1
    assert grouped["s008"].duration == 2.6
    assert grouped["s009"].duration == 3.5
    assert grouped["s009"].heading == "TEST FRAME RECONSTRUCTION"
    assert source[0][1].active_item_index is None, "source specs must stay immutable"


def test_same_heading_run_is_chunked_at_six_readable_rows():
    source = [
        (
            f"s{index:03d}",
            CardSpec(
                kind="timeline",
                heading="UPLOAD TIMELINE",
                duration=2.0 + index / 10,
                items=(f"spoken row {index}",),
            ),
        )
        for index in range(MAX_ITEMS + 1)
    ]

    grouped = group_same_heading_specs(source)

    first_chunk_rows = tuple(f"spoken row {index}" for index in range(MAX_ITEMS))
    for index in range(MAX_ITEMS):
        spec = grouped[f"s{index:03d}"]
        assert spec.items == first_chunk_rows
        assert spec.active_item_index == index
        assert len(spec.items) == MAX_ITEMS

    trailing = grouped[f"s{MAX_ITEMS:03d}"]
    assert trailing.items == (f"spoken row {MAX_ITEMS}",)
    assert trailing.active_item_index == 0
    assert trailing.duration == pytest.approx(2.0 + MAX_ITEMS / 10)


def test_same_heading_does_not_group_across_an_intervening_card():
    first = CardSpec(
        kind="checklist", heading="DECODE CHECK", duration=2.0, items=("RULE",)
    )
    middle = CardSpec(
        kind="checklist", heading="ANOMALY", duration=2.0, items=("00014",)
    )
    last = CardSpec(
        kind="checklist", heading="DECODE CHECK", duration=2.0, items=("KEY",)
    )

    grouped = group_same_heading_specs(
        (("s001", first), ("s002", middle), ("s003", last))
    )

    assert grouped == {"s001": first, "s002": middle, "s003": last}
    assert all(spec.active_item_index is None for spec in grouped.values())


# --- ASS construction ------------------------------------------------------------


def test_ass_carries_the_heading_text(style):
    typo, pal, _ = style
    doc = card_ass(CardSpec(kind="callout", heading="the hook", duration=3.0), typo, pal)
    assert "THE HOOK" in doc or "the hook" in doc


def test_ass_carries_every_item(style):
    typo, pal, _ = style
    spec = CardSpec(kind="checklist", heading="criteria", duration=3.0,
                    items=("first thing", "second thing", "third thing"))
    doc = card_ass(spec, typo, pal)
    for item in spec.items:
        assert item in doc


def test_long_url_wrap_does_not_leave_a_one_character_orphan(style):
    typo, _pal, _grade = style
    body_font, _findings = pick_font(typo)
    lines = cards_module._wrap_measured_lines(
        "https://www.theguardian.com/technology/shortcuts/2014/may/01/"
        "truth-youtube-mysterious-videos-webdriver-torso",
        360,
        18,
        font_family=body_font,
    )

    assert len(lines) >= 2
    assert len(lines[-1]) >= 4


def test_active_spoken_row_gets_one_yellow_band_and_dark_ink(style):
    typo, pal, _ = style
    spec = CardSpec(
        # A grouped timeline deliberately uses the stable row surface while
        # retaining its semantic kind for provenance.
        kind="timeline",
        heading="UPLOAD TIMELINE",
        duration=3.0,
        items=("FIRST OBSERVATION", "PEAK PERIOD", "CONFIRMATION"),
        active_item_index=1,
    )

    doc = card_ass(spec, typo, pal)
    warning_override = cards_module._wrap_override(pal["warning_yellow"])
    dark_override = cards_module._wrap_override(pal["text_mid"])
    highlight_events = [
        line
        for line in doc.splitlines()
        if line.startswith("Dialogue:")
        and ",CardShape," in line
        and warning_override in line
    ]
    body_events = [
        line
        for line in doc.splitlines()
        if line.startswith("Dialogue:") and ",CardBody," in line
    ]

    assert len(highlight_events) == 1
    assert r"\alpha&H18&" in highlight_events[0]
    assert len(body_events) == 3
    assert all(r"\fs" in event for event in body_events)
    assert "PEAK PERIOD" in body_events[1]
    assert dark_override in body_events[1]
    assert dark_override not in body_events[0]
    assert dark_override not in body_events[2]


def test_active_spoken_row_renders_as_one_visible_yellow_band(style, tmp_path):
    typo, pal, grade = style
    spec = CardSpec(
        kind="checklist",
        heading="TEST FRAME RECONSTRUCTION",
        duration=1.5,
        items=(
            "SHAPES MOVE · TEMPLATE STAYS",
            "TONE CHANGES · NO VERIFIED MESSAGE",
            "OUTPUT REPEATS · PURPOSE UNKNOWN",
        ),
        active_item_index=1,
    )

    out = build_card(
        spec,
        tmp_path / "highlight.mp4",
        typo,
        pal,
        grade,
        tmp_path / "work",
        width=640,
        height=360,
        fps=12,
    )
    frame = _rgb_frame(out, 0.5)
    yellow = (
        (frame[:, :, 0] > 175)
        & (frame[:, :, 1] > 155)
        & (frame[:, :, 2] < 100)
    )
    ys, _xs = np.where(yellow)

    assert yellow.mean() > 0.008, "the active band is not visibly present"
    assert ys.size
    assert int(ys.max()) - int(ys.min()) < frame.shape[0] * 0.22, (
        "yellow leaked into multiple row bands"
    )


def test_document_disclosure_is_rendered_inside_the_card(style):
    typo, pal, _ = style
    disclosure = "EDITORIAL PARAPHRASE · SOURCE-ATTRIBUTED"
    doc = card_ass(
        CardSpec(
            kind="document",
            heading="Source headline",
            duration=3.0,
            items=("Slot-specific summary", "23 Sep 2013", "SOURCE · example.com"),
            disclosure=disclosure,
        ),
        typo,
        pal,
    )

    disclosure_events = [
        line
        for line in doc.splitlines()
        if line.startswith("Dialogue:") and ",CardDisclosure," in line
    ]
    assert len(disclosure_events) == 1
    assert disclosure in disclosure_events[0]


def test_heading_rule_sits_below_the_actual_wrapped_title_block(style):
    import re

    typo, pal, _ = style
    height = 1080
    doc = card_ass(
        CardSpec(
            kind="document",
            heading=(
                "A deliberately long source title that wraps across multiple "
                "lines before its evidence summary begins"
            ),
            duration=3.0,
            items=("Summary",),
        ),
        typo,
        pal,
        width=1920,
        height=height,
    )
    events = [line for line in doc.splitlines() if line.startswith("Dialogue:")]
    heading_event = next(line for line in events if ",CardHead," in line)
    rule_event = next(line for line in events if ",CardShape," in line)
    heading_y = int(re.search(r"\\pos\(-?\d+,(-?\d+)\)", heading_event).group(1))
    rule_y = int(re.search(r"\\pos\(-?\d+,(-?\d+)\)", rule_event).group(1))
    heading_size = int(
        re.search(r"^Style: CardHead,[^,]+,(\d+),", doc, re.MULTILINE).group(1)
    )
    line_count = heading_event.count(r"\N") + 1

    assert line_count >= 2
    # A centred title needs at least half a font-size per rendered line below
    # its anchor, followed by visible padding. This rejects the former fixed
    # 5.5%-of-frame rule offset, which crossed the last line.
    assert rule_y >= (
        heading_y + line_count * heading_size * 0.5 + height * 0.01
    )


def test_four_line_checklist_heading_stays_below_title_safe_top(style):
    """A tall s266-style title must not be clipped above the safe frame."""
    import re

    typo, pal, _ = style
    width = 1920
    height = 1080
    doc = card_ass(
        CardSpec(
            kind="checklist",
            heading=(
                "MYSTERY BOARD CLEARS LEAVING ONLY A QA CHECKLIST AND "
                "RED-BLUE THUMBNAIL"
            ),
            duration=3.0,
        ),
        typo,
        pal,
        width=width,
        height=height,
    )
    events = [line for line in doc.splitlines() if line.startswith("Dialogue:")]
    heading_event = next(line for line in events if ",CardHead," in line)
    rule_event = next(line for line in events if ",CardShape," in line)
    heading_y = int(re.search(r"\\pos\(-?\d+,(-?\d+)\)", heading_event).group(1))
    rule_y = int(re.search(r"\\pos\(-?\d+,(-?\d+)\)", rule_event).group(1))
    heading_size = int(
        re.search(r"^Style: CardHead,[^,]+,(\d+),", doc, re.MULTILINE).group(1)
    )
    line_count = heading_event.count(r"\N") + 1
    heading_height = (
        line_count
        * heading_size
        * cards_module._HEADING_LINE_HEIGHT_MULTIPLIER
    )
    heading_top = heading_y - heading_height / 2
    heading_bottom = heading_y + heading_height / 2
    title_safe_top = height * (1 - TITLE_SAFE_FRACTION) / 2

    assert line_count == 4
    assert heading_top >= title_safe_top - 1
    assert rule_y > heading_bottom


def test_ass_positions_everything_inside_the_title_safe_box(style):
    """Arithmetic guard on the constraint the pixel test below proves."""
    import re

    typo, pal, _ = style
    width = height = 1000
    spec = CardSpec(kind="checklist", heading="criteria", duration=3.0,
                    items=tuple(f"item {i}" for i in range(MAX_ITEMS)))
    doc = card_ass(spec, typo, pal, width, height)

    margin_x = width * (1 - TITLE_SAFE_FRACTION) / 2
    margin_y = height * (1 - TITLE_SAFE_FRACTION) / 2
    positions = re.findall(r"\\pos\((-?\d+),(-?\d+)\)", doc)
    assert positions, "no positioned elements found"
    for x, y in positions:
        assert margin_x - 1 <= int(x) <= width - margin_x + 1, f"x={x} outside safe box"
        assert margin_y - 1 <= int(y) <= height - margin_y + 1, f"y={y} outside safe box"


def test_braces_in_content_cannot_open_an_ass_override_block(style):
    """A literal '{' would start a real override block and swallow the text."""
    typo, pal, _ = style
    doc = card_ass(
        CardSpec(kind="callout", heading="a {\\b1} trap", duration=3.0), typo, pal
    )
    events = [line for line in doc.splitlines() if line.startswith("Dialogue:")]
    body = "".join(events)
    assert "{\\b1}" not in body


def test_timeline_endpoint_labels_anchor_inward_and_wrap_inside_safe(style):
    typo, pal, _ = style
    spec = CardSpec(
        kind="timeline",
        heading="upload history",
        duration=3.0,
        items=(
            "first observed public upload with unexplained test pattern",
            "final documented confirmation of automated quality testing",
        ),
    )
    doc = card_ass(spec, typo, pal)
    body_events = [
        line for line in doc.splitlines() if line.startswith("Dialogue:") and ",CardBody," in line
    ]

    assert len(body_events) == 2
    assert r"\an4" in body_events[0], "first endpoint must grow right, into the axis"
    assert r"\an6" in body_events[1], "last endpoint must grow left, into the axis"
    assert all(r"\clip(" in line for line in body_events)
    assert all(r"\N" in line for line in body_events), "long endpoints were not wrapped"


def test_short_timeline_labels_keep_single_line_text(style):
    typo, pal, _ = style
    doc = card_ass(
        CardSpec(
            kind="timeline",
            heading="dates",
            duration=3.0,
            items=("May 15", "May 16"),
        ),
        typo,
        pal,
    )
    body_events = [
        line for line in doc.splitlines() if line.startswith("Dialogue:") and ",CardBody," in line
    ]

    assert len(body_events) == 2
    assert all(r"\N" not in line for line in body_events)


def test_upload_timeline_labels_are_nonoverlapping_and_title_safe(style):
    """Regression for the upload card whose endpoint labels collided.

    A position inside title-safe is not sufficient: left/right anchored glyphs
    also need an inset anchor, a clip box inside the 1.30x detail crop, and each
    hard-wrapped line must measure narrower than the space remaining from that
    anchor to its clip edge. The endpoint clip and rendered bounding boxes must
    also be disjoint; checking each endpoint in isolation missed the collision.
    """
    import re

    typo, pal, _ = style
    width = 1920
    height = 1080
    doc = card_ass(
        CardSpec(
            kind="timeline",
            heading="UPLOAD",
            duration=3.0,
            items=(
                "ONE UPLOAD ABOUT EVERY TWO MINUTES",
                "PEAK OBSERVED PERIOD",
            ),
        ),
        typo,
        pal,
        width=width,
        height=height,
    )
    body_events = [
        line
        for line in doc.splitlines()
        if line.startswith("Dialogue:") and ",CardBody," in line
    ]
    assert len(body_events) == 2

    body_size = int(
        re.search(r"^Style: CardBody,[^,]+,(\d+),", doc, re.MULTILINE).group(1)
    )
    body_font, _findings = pick_font(typo)
    detail_crop_left = width * (1 - 1 / 1.30) / 2
    detail_crop_right = width - detail_crop_left
    expected_title_safe_margin = width * (1 - TITLE_SAFE_FRACTION) / 2
    expected_text_inset = width * TITLE_SAFE_FRACTION * TEXT_SAFE_INSET_FRACTION

    rendered_boxes = []
    clip_boxes = []
    for event in body_events:
        position = re.search(r"\\pos\((-?\d+),(-?\d+)\)", event)
        clip = re.search(r"\\clip\((\d+),(\d+),(\d+),(\d+)\)", event)
        assert position and clip
        x, y = map(int, position.groups())
        clip_left, clip_top, clip_right, clip_bottom = map(int, clip.groups())

        assert clip_left > detail_crop_left
        assert clip_right < detail_crop_right
        assert clip_left >= expected_title_safe_margin - 1
        assert clip_right <= width - expected_title_safe_margin + 1
        assert min(x - clip_left, clip_right - x) >= expected_text_inset * 0.20

        payload = event.rsplit("}", 1)[-1]
        rendered_lines = payload.split(r"\N")
        measured_widths = []
        for rendered_line in rendered_lines:
            measured = _measured_line_width(
                rendered_line,
                body_size,
                font_family=body_font,
            )
            measured_widths.append(measured)
            if r"\an4" in event:
                available = clip_right - x
            elif r"\an6" in event:
                available = x - clip_left
            else:
                available = 2 * min(x - clip_left, clip_right - x)
            assert measured <= available

        rendered_width = max(measured_widths)
        if r"\an4" in event:
            rendered_left, rendered_right = x, x + rendered_width
        elif r"\an6" in event:
            rendered_left, rendered_right = x - rendered_width, x
        else:
            rendered_left = x - rendered_width / 2
            rendered_right = x + rendered_width / 2
        line_height = body_size * 1.2
        rendered_height = len(rendered_lines) * line_height
        rendered_boxes.append(
            (
                rendered_left,
                y - rendered_height / 2,
                rendered_right,
                y + rendered_height / 2,
            )
        )
        clip_boxes.append((clip_left, clip_top, clip_right, clip_bottom))

    left_box, right_box = rendered_boxes
    assert left_box[2] < right_box[0], "timeline endpoint label bboxes overlap"
    assert clip_boxes[0][2] < clip_boxes[1][0], "timeline label regions lack a safe gap"
    for left, top, right, bottom in rendered_boxes:
        assert left >= expected_title_safe_margin - 1
        assert right <= width - expected_title_safe_margin + 1
        assert top >= height * (1 - TITLE_SAFE_FRACTION) / 2 - 1
        assert bottom <= height - height * (1 - TITLE_SAFE_FRACTION) / 2 + 1


def test_comparison_items_wrap_and_are_clipped_to_their_own_columns(style):
    import re

    typo, pal, _ = style
    width = 1920
    spec = CardSpec(
        kind="comparison",
        heading="viewer interpretation",
        duration=3.0,
        items=(
            "ordinary software testing behaviour encountered without any specification",
            "sinister coded message inferred from repeated rectangles and electronic tones",
        ),
    )
    doc = card_ass(spec, typo, pal, width=width, height=1080)
    body_events = [
        line for line in doc.splitlines() if line.startswith("Dialogue:") and ",CardBody," in line
    ]

    assert len(body_events) == 2
    assert all(r"\N" in line for line in body_events)
    clips = [
        tuple(map(int, re.search(r"\\clip\((\d+),(\d+),(\d+),(\d+)\)", line).groups()))
        for line in body_events
    ]
    assert clips[0][2] < width // 2
    assert clips[1][0] > width // 2


@pytest.mark.parametrize(
    "variant,items",
    [
        (
            "edge baseline",
            ("REFERENCE - SHARP RED/BLUE EDGES", "PROCESSED - SAME TEST SIGNAL"),
        ),
        (
            "processed change",
            ("REFERENCE - SHARP EDGE", "PROCESSED - BLUR + COLOUR SHIFT"),
        ),
        (
            "timing and audio",
            ("EXPECTED - FRAME 00 + TONE A", "PROCESSED - FRAME +02 + TONE DELTA"),
        ),
        (
            "automated flag",
            ("EDGE FLAGGED", "COLOUR FLAGGED", "TIMING FLAGGED", "AUDIO FLAGGED"),
        ),
    ],
)
def test_signal_comparison_variants_burn_caveats_and_processed_effects(
    variant, items, style
):
    typo, pal, _ = style
    doc = card_ass(
        CardSpec(
            kind="comparison",
            heading=f"signal comparison - {variant}",
            duration=3.0,
            items=items,
        ),
        typo,
        pal,
    )

    assert "REFERENCE / PROCESSED" in doc
    assert "SIGNAL COMPARISON" not in doc
    assert r"\blur6" in doc
    assert "LOCAL ILLUSTRATION · GENERAL TESTING LOGIC" in doc
    assert "NOT WEBDRIVER TORSO'S PUBLISHED ALGORITHM" in doc
    visible_text = doc.replace(r"\N", " ")
    for item in items:
        assert item in visible_text


def test_each_signal_comparison_variant_has_distinct_shape_geometry(style):
    typo, pal, _ = style
    variants = {
        "edge baseline": (
            "REFERENCE - SHARP RED/BLUE EDGES",
            "PROCESSED - SAME TEST SIGNAL",
        ),
        "processed change": (
            "REFERENCE - SHARP EDGE",
            "PROCESSED - BLUR + COLOUR SHIFT",
        ),
        "timing and audio": (
            "EXPECTED - FRAME 00 + TONE A",
            "PROCESSED - FRAME +02 + TONE DELTA",
        ),
        "automated flag": (
            "EDGE FLAGGED",
            "COLOUR FLAGGED",
            "TIMING FLAGGED",
            "AUDIO FLAGGED",
        ),
    }

    shape_fingerprints = []
    for variant, items in variants.items():
        doc = card_ass(
            CardSpec(
                kind="comparison",
                heading=f"signal comparison - {variant}",
                duration=3.0,
                items=items,
            ),
            typo,
            pal,
        )
        shape_fingerprints.append("\n".join(
            line
            for line in doc.splitlines()
            if line.startswith("Dialogue:") and ",CardShape," in line
        ))

    assert len(set(shape_fingerprints)) == len(variants)


def test_signal_comparison_shapes_stay_inside_title_safe(style):
    import re

    typo, pal, _ = style
    width = 1920
    height = 1080
    doc = card_ass(
        CardSpec(
            kind="comparison",
            heading="signal comparison - automated flag",
            duration=3.0,
            items=(
                "EDGE FLAGGED",
                "COLOUR FLAGGED",
                "TIMING FLAGGED",
                "AUDIO FLAGGED",
            ),
        ),
        typo,
        pal,
        width=width,
        height=height,
    )
    margin_x = width * (1 - TITLE_SAFE_FRACTION) / 2
    margin_y = height * (1 - TITLE_SAFE_FRACTION) / 2

    shape_events = [
        line
        for line in doc.splitlines()
        if line.startswith("Dialogue:") and ",CardShape," in line
    ]
    assert shape_events
    for event in shape_events:
        position = re.search(r"\\pos\((-?\d+),(-?\d+)\)", event)
        rectangle = re.search(
            r"\{\\p1\}m 0 0 l (-?\d+) 0 (-?\d+) (-?\d+) 0 (-?\d+)",
            event,
        )
        assert position and rectangle
        x, y = map(int, position.groups())
        width_from_top, width_from_bottom, height_from_side = (
            int(rectangle.group(1)),
            int(rectangle.group(2)),
            int(rectangle.group(4)),
        )
        assert width_from_top == width_from_bottom
        assert margin_x - 1 <= x
        assert margin_y - 1 <= y
        assert x + width_from_top <= width - margin_x + 1
        assert y + height_from_side <= height - margin_y + 1


def test_regular_comparison_does_not_opt_into_signal_visuals(style):
    typo, pal, _ = style
    doc = card_ass(
        CardSpec(
            kind="comparison",
            heading="viewer interpretation",
            duration=3.0,
            items=("normal software testing", "sinister coded message"),
        ),
        typo,
        pal,
    )

    assert "viewer interpretation" in doc or "VIEWER INTERPRETATION" in doc
    assert "REFERENCE / PROCESSED" not in doc
    assert "LOCAL ILLUSTRATION" not in doc
    assert r"\blur6" not in doc


def test_long_nonnumeric_stat_uses_readable_heading_body_fallback(style):
    """Regression for s016: its prose item was set in the giant CardStat style."""
    typo, pal, _ = style
    spec, _ = parse_detail(
        "anatomy of one standard upload: ten numbered one-second slides red block "
        "blue block labels tones",
        3.0,
    )
    assert spec.kind == "stat"

    doc = card_ass(spec, typo, pal)
    events = [line for line in doc.splitlines() if line.startswith("Dialogue:")]

    assert not any(",CardStat," in line for line in events)
    assert any(",CardHead," in line for line in events)
    assert any(",CardBody," in line for line in events)
    assert any(r"\N" in line for line in events if ",CardBody," in line)


def test_normal_numeric_stat_keeps_the_large_figure_layout(style):
    typo, pal, _ = style
    spec, _ = parse_detail("2.279 million registered candidates", 3.0)
    doc = card_ass(spec, typo, pal)
    events = [line for line in doc.splitlines() if line.startswith("Dialogue:")]

    assert any(",CardStat," in line for line in events)
    assert not any(",CardHead," in line for line in events)


# --- real renders ----------------------------------------------------------------


def test_card_frame_counts_preserve_authored_timing_then_add_one_handle():
    assert cards_module._card_frame_counts(1.0, 30) == (30, 31)
    assert cards_module._card_frame_counts(1.01, 30) == (30, 31)
    assert cards_module._card_frame_counts(2.95, 30) == (88, 89)


@pytest.mark.parametrize(
    ("duration", "fps"),
    [(2.95, 30), (2.9, 30), (3.123, 30), (0.5, 6)],
)
def test_card_handle_covers_independently_rounded_resolve_endpoint(
    duration, fps
):
    authored, encoded = cards_module._card_frame_counts(duration, fps)
    resolve_endpoint = int(
        (Decimal(str(duration)) * Decimal(fps)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )

    assert encoded == authored + 1
    # Resolve source-end values are exclusive. An encoded count of 89 safely
    # serves Resolve's half-up endpoint 89 even when authored half-even timing
    # chose 88 visible frames.
    assert resolve_endpoint <= encoded


def test_card_handle_covers_absolute_slot_endpoint_rounding_regression():
    # Representative authored card boundary: local nearest-frame timing is 81
    # frames, while independently rounding its absolute timeline endpoints asks
    # Resolve for 82 source frames.
    fps = 30
    slot_start = 996.515
    slot_end = 999.220
    authored, encoded = cards_module._card_frame_counts(
        slot_end - slot_start, fps
    )

    def resolve_frame(seconds: float) -> int:
        return int(
            (Decimal(str(seconds)) * Decimal(fps)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )

    required_source_frames = resolve_frame(slot_end) - resolve_frame(slot_start)
    assert authored == 81
    assert required_source_frames == 82
    assert encoded == required_source_frames


@pytest.mark.parametrize("kind,detail", [
    ("callout", "UAPA clause callout"),
    ("checklist", "criteria: unemployed | lazy | online"),
    ("comparison", "33 percent versus 50 percent"),
    ("timeline", "timeline, pandrah May to solah May"),
    ("stat", "2.279 million registered candidates"),
    ("document", "order excerpt: paragraph fourteen"),
    ("montage", "montage: surveillance | miscaption | funding"),
])
def test_every_template_actually_draws_something(kind, detail, style, tmp_path):
    """The failure this whole module exists to prevent: a stage that reports
    success and puts nothing on screen.

    Measured against a bare background plate rather than an absolute ink
    threshold. Templates differ hugely in how much ink they put down -- a
    `stat` card sets one huge number, a `document` card is a thin frame outline
    and a couple of words -- so any single magic number is either too loose to
    catch a blank card or too tight for the sparse ones. The plate is the true
    null case, and it contains no ink at all.
    """
    from rabbithole.cards import CARD_PLATE_KIND
    from rabbithole.sources.plates import PlateSpec, build_plate

    typo, pal, grade = style
    spec, _ = parse_detail(detail, 1.5)
    out = build_card(spec, tmp_path / f"{kind}.mp4", typo, pal, grade, tmp_path / "work",
                     width=640, height=360, fps=12)
    assert out.exists()

    bare = build_plate(
        PlateSpec(kind=CARD_PLATE_KIND, duration=1.5, fps=12, width=640, height=360),
        tmp_path / "bare.mp4", grade=grade,
    )

    card_ink = _ink_fraction(_frame(out, 0.5))
    plate_ink = _ink_fraction(_frame(bare, 0.5))

    assert plate_ink < 1e-5, (
        f"the null case is not null: a bare plate measured {plate_ink:.6f} ink, "
        f"so this comparison proves nothing"
    )
    assert card_ink > plate_ink + 1e-4, (
        f"{kind} rendered but drew essentially nothing "
        f"(card {card_ink:.6f} vs bare plate {plate_ink:.6f})"
    )


def test_signal_comparison_renders_visible_red_and_blue_blocks(style, tmp_path):
    typo, pal, grade = style
    spec = CardSpec(
        kind="comparison",
        heading="signal comparison - processed change",
        duration=1.0,
        items=(
            "REFERENCE - SHARP EDGE",
            "PROCESSED - BLUR + COLOUR SHIFT",
        ),
    )
    out = build_card(
        spec,
        tmp_path / "signal-comparison.mp4",
        typo,
        pal,
        grade,
        tmp_path / "signal-work",
        width=640,
        height=360,
        fps=12,
    )
    frame = _rgb_frame(out, 0.5).astype(np.float64)
    red_pixels = (
        (frame[:, :, 0] > 80)
        & (frame[:, :, 0] > frame[:, :, 1] * 1.20)
        & (frame[:, :, 0] > frame[:, :, 2] * 1.20)
    )
    blue_pixels = (
        (frame[:, :, 2] > 80)
        & (frame[:, :, 2] > frame[:, :, 0] * 1.20)
        & (frame[:, :, 2] > frame[:, :, 1] * 1.20)
    )

    assert red_pixels.mean() > 0.005
    assert blue_pixels.mean() > 0.005


def test_a_card_spans_its_requested_duration(style, tmp_path):
    from rabbithole.sources.soundgen import probe_duration

    typo, pal, grade = style
    spec, _ = parse_detail("UAPA clause callout", 2.5)
    out = build_card(spec, tmp_path / "c.mp4", typo, pal, grade, tmp_path / "work",
                     width=640, height=360, fps=12)
    assert probe_duration(out) == pytest.approx(2.5, abs=0.15)


def test_half_frame_card_encodes_h264_cfr_with_an_identical_terminal_handle(
    style, tmp_path
):
    typo, pal, grade = style
    spec, _ = parse_detail("UAPA clause callout", 2.95)
    work_dir = tmp_path / "terminal-work"
    out = build_card(
        spec,
        tmp_path / "terminal-handle.mp4",
        typo,
        pal,
        grade,
        work_dir,
        width=320,
        height=180,
        fps=30,
    )

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,pix_fmt,r_frame_rate,avg_frame_rate,"
            "nb_frames,duration",
            "-of",
            "json",
            str(out),
        ],
        capture_output=True,
        check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["codec_name"] == "h264"
    assert stream["width"] == 320
    assert stream["height"] == 180
    assert stream["pix_fmt"] == "yuv420p"
    assert stream["r_frame_rate"] == "30/1"
    assert stream["avg_frame_rate"] == "30/1"
    assert int(stream["nb_frames"]) == 89
    assert float(stream["duration"]) == pytest.approx(89 / 30, abs=1e-6)

    def decoded_frame(index: int) -> bytes:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(out),
                "-vf",
                f"select=eq(n\\,{index})",
                "-frames:v",
                "1",
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "-",
            ],
            capture_output=True,
            check=True,
        )
        assert len(result.stdout) == 320 * 180 * 3
        return result.stdout

    penultimate = np.frombuffer(decoded_frame(87), dtype=np.uint8).astype(np.int16)
    terminal = np.frombuffer(decoded_frame(88), dtype=np.uint8).astype(np.int16)
    encoded_delta = np.abs(penultimate - terminal)
    # ``tpad=stop_mode=clone`` repeats the filtered frame before H.264. Lossy
    # inter-frame quantization can move a few decoded bytes, so assert visual
    # identity tightly enough to distinguish a clone from the animated grain's
    # next authored frame.
    assert encoded_delta.mean() < 0.5
    assert encoded_delta.max() < 24
    # The ASS contract remains authored at 2.95 seconds. Only the encoded media
    # carries the 89th handle frame.
    assert "0:00:02.95" in (
        work_dir / "terminal-handle.ass"
    ).read_text(encoding="utf-8")


def test_more_items_put_more_ink_on_screen(style, tmp_path):
    """Guards against a layout that silently overwrites items in one spot."""
    typo, pal, grade = style
    few, _ = parse_detail("criteria: one | two", 1.5)
    many, _ = parse_detail("criteria: one | two | three | four | five", 1.5)

    a = build_card(few, tmp_path / "few.mp4", typo, pal, grade, tmp_path / "w1",
                   width=640, height=360, fps=12)
    b = build_card(many, tmp_path / "many.mp4", typo, pal, grade, tmp_path / "w2",
                   width=640, height=360, fps=12)

    assert _ink_fraction(_frame(b, 0.5)) > _ink_fraction(_frame(a, 0.5))


def test_card_text_survives_every_framing(style, tmp_path):
    """The title-safe constraint, measured rather than asserted.

    `cut_segment` applies framing to every asset unconditionally and `detail`
    crops to 1/1.30 of the frame. If a card were laid out to the frame edge,
    its text would be cropped away on exactly the cuts meant to emphasise it.

    Pure magnification with no loss scales the ink area by the zoom squared, so
    a zoomed framing must show MORE ink than `wide`, never less.
    """
    typo, pal, grade = style
    detail = "criteria: unemployed | lazy | eleven hours online | ranting"
    spec, _ = parse_detail(detail, 2.0)
    card = build_card(spec, tmp_path / "card.mp4", typo, pal, grade, tmp_path / "work",
                      width=640, height=360, fps=12)
    slot = Slot(slot_id="s001", kind="graphic", detail=detail, start=0.0, end=2.0,
                queries=(), marker_word_index=0)

    measured = {}
    for framing in FRAMINGS:
        cut = Cut(index=0, start=0.0, end=1.5, slot_id="s001", origin="script",
                  framing=framing, transition="cut", reason="test")
        seg = tmp_path / f"seg-{framing}.mp4"
        cut_segment(cut, slot, card, seg, width=640, height=360, fps=12)
        measured[framing] = _ink_fraction(_frame(seg, 0.5))

    for framing in FRAMINGS:
        if framing == "wide":
            continue
        assert measured[framing] >= measured["wide"] * 0.98, (
            f"{framing} lost text: {measured[framing]:.5f} vs wide "
            f"{measured['wide']:.5f} -- laid out outside the title-safe box"
        )

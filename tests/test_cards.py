"""Graphic cards.

Real ffmpeg renders and real pixel measurements, not clean exit codes. This
module's whole reason to exist is that 81 slots were planned as handled and
silently produced nothing, so "it ran" is precisely the evidence not worth
having -- see graphics.py's own note that a filter which draws nothing exits 0.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from rabbithole.cards import (
    CARD_KINDS,
    MAX_ITEMS,
    TITLE_SAFE_FRACTION,
    CardSpec,
    build_card,
    card_ass,
    classify,
    parse_detail,
    production_note_reason,
    spec_for_slot,
)
from rabbithole.edl import Cut
from rabbithole.jsonio import read_json
from rabbithole.render import FRAMINGS, cut_segment
from rabbithole.slots import Slot
from rabbithole.sources.plates import load_grade

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


def test_every_classified_kind_has_an_event_builder():
    """A kind with no builder would raise KeyError at render time."""
    from rabbithole.cards import _EVENT_BUILDERS

    for kind in CARD_KINDS:
        assert kind in _EVENT_BUILDERS, kind


# --- detail parsing --------------------------------------------------------------


def test_explicit_items_are_used():
    spec, _ = parse_detail("criteria: unemployed | lazy | online", 4.0)
    assert spec.items == ("unemployed", "lazy", "online")


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


# --- real renders ----------------------------------------------------------------


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


def test_a_card_spans_its_requested_duration(style, tmp_path):
    from rabbithole.sources.soundgen import probe_duration

    typo, pal, grade = style
    spec, _ = parse_detail("UAPA clause callout", 2.5)
    out = build_card(spec, tmp_path / "c.mp4", typo, pal, grade, tmp_path / "work",
                     width=640, height=360, fps=12)
    assert probe_duration(out) == pytest.approx(2.5, abs=0.15)


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

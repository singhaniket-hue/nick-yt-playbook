import json

import pytest

from rabbithole.markers import parse
from rabbithole.validate import check_marker_args


@pytest.fixture
def sfx_names():
    return {"vhs-burst", "sub-drop", "glitch-sting", "heartbeat"}


def test_valid_markers_pass(sfx_names):
    source = (
        "[ACT:1 Cold Open] [CHAPTER:1 Shuruaat] [SHOT:screenshot push 105-115] "
        "[SILENCE:1.5s] [SFX:vhs-burst] [MUSIC:chasms] [CENSOR:face] [KEY:archive] Text."
    )

    assert check_marker_args(parse(source), sfx_names) == []


def test_silence_below_range_is_flagged(sfx_names):
    findings = check_marker_args(parse("[SILENCE:0.2s] Text."), sfx_names)

    assert len(findings) == 1
    assert findings[0].gate == "marker_args"
    assert "0.5" in findings[0].message


def test_silence_above_range_is_flagged(sfx_names):
    findings = check_marker_args(parse("[SILENCE:3.0s] Text."), sfx_names)

    assert "2.0" in findings[0].message


def test_silence_without_unit_suffix_is_flagged(sfx_names):
    findings = check_marker_args(parse("[SILENCE:1.5] Text."), sfx_names)

    assert "seconds" in findings[0].message


def test_unknown_sfx_name_is_flagged(sfx_names):
    findings = check_marker_args(parse("[SFX:airhorn] Text."), sfx_names)

    assert len(findings) == 1
    assert "airhorn" in findings[0].message


def test_unknown_shot_kind_is_flagged(sfx_names):
    findings = check_marker_args(parse("[SHOT:hologram spin] Text."), sfx_names)

    assert "hologram" in findings[0].message


def test_act_number_outside_one_to_five_is_flagged(sfx_names):
    findings = check_marker_args(parse("[ACT:7 Extra] Text."), sfx_names)

    assert "1-5" in findings[0].message


def test_chapter_without_number_is_flagged(sfx_names):
    findings = check_marker_args(parse("[CHAPTER:Shuruaat] Text."), sfx_names)

    assert "number" in findings[0].message


def test_sfx_names_load_from_style_pack(tmp_path):
    path = tmp_path / "sfx.json"
    path.write_text(json.dumps({"vhs-burst": {}, "sub-drop": {}}), encoding="utf-8")

    from rabbithole.validate import load_sfx_names

    assert load_sfx_names(path) == {"vhs-burst", "sub-drop"}

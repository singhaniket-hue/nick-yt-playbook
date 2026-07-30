from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from rabbithole.fcpxml import FCPXMLError, build_fcpxml
from rabbithole.resolve_manifest import compile_resolve_plan
from test_resolve_manifest import (
    _add_generated_sound_library,
    _add_prepared_sound_stems,
    _project,
    _write_wave,
)


def test_fcpxml_is_deterministic_and_uses_valid_effect_references(tmp_path):
    root = _project(tmp_path)
    plan = compile_resolve_plan(root)

    first = build_fcpxml(plan, project_root=root)
    second = build_fcpxml(plan, project_root=root)
    document = ET.fromstring(first)

    assert first == second
    assert document.attrib["version"] == "1.10"
    assert [child.tag for child in document] == ["resources", "library"]
    library = document.find("./library")
    assert library is not None
    assert library.attrib == {}
    assert len(library.findall("./event")) == 1
    effects = {
        effect.attrib["id"]: effect.attrib["name"]
        for effect in document.findall("./resources/effect")
    }
    transitions = document.findall(
        "./library/event/project/sequence/spine/transition"
    )
    assert len(transitions) == plan["timeline_validation"][
        "video_transition_count"
    ]
    for transition in transitions:
        filter_video = transition.find("filter-video")
        assert filter_video is not None
        assert filter_video.attrib["ref"] in effects


def test_fcpxml_maps_primary_and_connected_items_to_resolve_tracks(tmp_path):
    root = _project(tmp_path)
    plan = compile_resolve_plan(root)
    document = ET.fromstring(build_fcpxml(plan, project_root=root))

    spine = document.find("./library/event/project/sequence/spine")
    assert spine is not None
    assert spine.find("./clip") is None
    assert spine.find("./spine") is None

    primary_visuals = [
        item
        for item in spine.findall("./asset-clip")
        if "lane" not in item.attrib
    ]
    assert len(primary_visuals) == 2
    assert primary_visuals[1].attrib["start"] == "30/30s"
    assert primary_visuals[1].find("adjust-transform") is not None

    connected_visuals = [
        item
        for item in spine.findall(".//asset-clip")
        if int(item.attrib.get("lane", "0")) > 0
        and item.attrib.get("srcEnable") == "video"
    ]
    assert len(connected_visuals) == 1
    assert connected_visuals[0].attrib["lane"] == "1"
    assert "track=V2" in (connected_visuals[0].findtext("note") or "")

    connected_audio = [
        item
        for item in spine.findall(".//asset-clip")
        if int(item.attrib.get("lane", "0")) < 0
    ]
    assert {item.attrib["lane"] for item in connected_audio} == {"-1", "-2"}
    captions = spine.findall(".//caption")
    assert captions
    assert {item.attrib["lane"] for item in captions} == {"1"}
    assert captions[0].findtext("./text/text-style") == "One & sentence."
    titles = spine.findall(".//title")
    assert titles
    assert {item.attrib["lane"] for item in titles} == {"2"}

    anchor_tags = {"asset-clip", "caption", "spine", "title"}
    for primary in spine:
        if primary.tag not in {"asset-clip", "gap"}:
            continue
        for child in primary:
            if child.tag in anchor_tags:
                assert int(child.attrib["lane"]) != 0

    assert "One &amp; sentence." in build_fcpxml(plan, project_root=root)
    # The in-app runner adds one grouped marker per frame with customData.
    # Importing plan markers through FCPXML as well would collide with those
    # API calls because Resolve permits only one timeline marker per frame.
    assert document.findall(".//marker") == []
    assert document.findall(".//chapter-marker") == []


def test_fcpxml_declares_actual_narration_channel_count_and_rate(tmp_path):
    root = _project(tmp_path)
    _write_wave(
        root / "narration" / "vo.wav",
        channels=1,
        sample_rate=44_100,
        seconds=4.0,
    )
    plan = compile_resolve_plan(root)

    document = ET.fromstring(build_fcpxml(plan, project_root=root))
    narration_resource = next(
        asset
        for asset in document.findall("./resources/asset")
        if asset.attrib["name"] == "narration-vo"
    )

    assert narration_resource.attrib["audioChannels"] == "1"
    assert narration_resource.attrib["audioRate"] == "44.1k"


def test_fcpxml_places_generated_music_and_sfx_on_separate_audio_lanes(tmp_path):
    root = _project(tmp_path)
    _add_generated_sound_library(root, windows_separators=True)
    plan = compile_resolve_plan(root)

    document = ET.fromstring(build_fcpxml(plan, project_root=root))
    audio = [
        item
        for item in document.findall(".//asset-clip")
        if int(item.attrib.get("lane", "0")) < 0
    ]
    music = [item for item in audio if item.attrib["lane"] == "-3"]
    effects = [item for item in audio if item.attrib["lane"] == "-4"]

    assert len(music) == 2
    assert len(effects) == 1
    assert {item.attrib["audioRole"] for item in music} == {"music"}
    assert {item.attrib["audioRole"] for item in effects} == {"effects.sfx"}
    assert [item.attrib["duration"] for item in music] == ["45/30s", "30/30s"]
    assert effects[0].attrib["duration"] == "15/30s"
    assert "track=A3" in (music[0].findtext("note") or "")
    assert "track=A4" in (effects[0].findtext("note") or "")

    resource_sources = {
        representation.attrib["suggestedFilename"]
        for representation in document.findall("./resources/asset/media-rep")
    }
    assert {
        "drone-low-00.wav",
        "drone-low-01.wav",
        "sub-drop.wav",
    } <= resource_sources
    generated_resources = {
        representation.attrib["suggestedFilename"]: asset
        for asset in document.findall("./resources/asset")
        for representation in asset.findall("media-rep")
        if representation.attrib["suggestedFilename"]
        in {"drone-low-00.wav", "drone-low-01.wav", "sub-drop.wav"}
    }
    assert {
        asset.attrib["audioRate"] for asset in generated_resources.values()
    } == {"44.1k"}
    assert {
        asset.attrib["audioChannels"] for asset in generated_resources.values()
    } == {"1"}


def test_fcpxml_preserves_audio_clip_gain_as_editable_volume_adjustment(tmp_path):
    root = _project(tmp_path)
    _add_generated_sound_library(root)
    _add_prepared_sound_stems(root)
    plan = compile_resolve_plan(root)

    document = ET.fromstring(build_fcpxml(plan, project_root=root))
    audio = [
        item
        for item in document.findall(".//asset-clip")
        if int(item.attrib.get("lane", "0")) < 0
    ]

    assert {item.attrib["lane"] for item in audio} == {"-1", "-3", "-4"}
    assert {
        item.find("adjust-volume").attrib["amount"]
        for item in audio
    } == {"-0.125dB"}
    assert {
        tuple(child.tag for child in item)
        for item in audio
    } == {("note", "adjust-volume")}


def test_fcpxml_omits_identity_audio_volume_adjustment(tmp_path):
    root = _project(tmp_path)
    plan = compile_resolve_plan(root)
    narration = next(
        clip for clip in plan["audio"] if clip["track"] == "A1"
    )
    assert narration["gain_db"] == 0.0

    document = ET.fromstring(build_fcpxml(plan, project_root=root))
    narration_item = next(
        item
        for item in document.findall(".//asset-clip")
        if item.attrib.get("lane") == "-1"
    )
    assert narration_item.find("adjust-volume") is None


def test_fcpxml_keeps_music_and_sfx_importable_at_the_same_timestamp(tmp_path):
    root = _project(tmp_path)
    _add_generated_sound_library(root)
    timing_path = root / "narration" / "timing.json"
    timing = json.loads(timing_path.read_text(encoding="utf-8"))
    next(
        marker for marker in timing["markers"] if marker["kind"] == "SFX"
    )["seconds"] = 0.0
    timing_path.write_text(json.dumps(timing, indent=2), encoding="utf-8")

    plan = compile_resolve_plan(root)
    document = ET.fromstring(build_fcpxml(plan, project_root=root))
    simultaneous = [
        item
        for item in document.findall(".//asset-clip")
        if item.attrib.get("lane") in {"-3", "-4"}
        and item.attrib["offset"] == "0s"
    ]

    assert {item.attrib["lane"] for item in simultaneous} == {"-3", "-4"}
    assert {item.attrib["audioRole"] for item in simultaneous} == {
        "music",
        "effects.sfx",
    }


def test_source_caption_uses_editable_basic_title_in_bottom_left(tmp_path):
    root = _project(tmp_path)
    plan = compile_resolve_plan(root)
    plan["overlays"].append(
        {
            "id": "authored-title",
            "kind": "chapter_card",
            "track": "V3",
            "start_frame": 0,
            "end_frame": 30,
            "duration_frames": 30,
            "text": "Authored title",
            "detail": {},
        }
    )
    document = ET.fromstring(build_fcpxml(plan, project_root=root))

    basic_title = next(
        effect
        for effect in document.findall("./resources/effect")
        if effect.attrib["name"] == "Basic Title"
    )
    source_caption = next(
        title
        for title in document.findall(".//title")
        if "kind=source_caption" in (title.findtext("note") or "")
    )
    assert source_caption.attrib["ref"] == basic_title.attrib["id"]
    assert source_caption.attrib["lane"] == "2"
    assert source_caption.findtext("./text/text-style") == "Archive <2024>"
    transform = source_caption.find("adjust-transform")
    assert transform is not None
    assert transform.attrib == {
        "position": "-38 -42",
        "scale": "1 1",
        "rotation": "0",
    }
    style = source_caption.find("./text-style-def/text-style")
    assert style is not None
    assert style.attrib["font"] == "Courier New"
    assert style.attrib["fontSize"] == "32"
    assert style.attrib["alignment"] == "left"
    assert style.attrib["backgroundColor"] == "0 0 0 0.65"
    assert "editable=1 layout=bottom-left" in (
        source_caption.findtext("note") or ""
    )
    assert [child.tag for child in source_caption] == [
        "text",
        "text-style-def",
        "note",
        "adjust-transform",
    ]
    authored = next(
        title
        for title in document.findall(".//title")
        if title.findtext("./text/text-style") == "Authored title"
    )
    assert authored.find("adjust-transform") is None
    authored_style = authored.find("./text-style-def/text-style")
    assert authored_style is not None
    assert authored_style.attrib["fontSize"] == "54"
    assert authored_style.attrib["alignment"] == "center"


def test_connected_offset_uses_covering_primary_items_local_timeline(tmp_path):
    root = _project(tmp_path)
    plan = compile_resolve_plan(root)
    plan["clips"][1]["source_start_frame"] = 300
    plan["clips"][1]["source_end_frame"] = 330
    connected = plan["clips"][2]
    connected["start_frame"] = 45
    connected["end_frame"] = 60
    connected["duration_frames"] = 15

    document = ET.fromstring(build_fcpxml(plan, project_root=root))
    item = next(
        candidate
        for candidate in document.findall(".//asset-clip")
        if "track=V2" in (candidate.findtext("note") or "")
    )

    # The covering V1 clip starts at frame 30 in the sequence but at frame 300
    # in its source-local timeline: 300 + (45 - 30) = 315.
    assert item.attrib["offset"] == "315/30s"


def test_overlapping_clips_on_one_video_track_are_rejected(tmp_path):
    root = _project(tmp_path)
    plan = compile_resolve_plan(root)
    plan["clips"][1]["start_frame"] = 15
    plan["clips"][1]["end_frame"] = 45

    with pytest.raises(FCPXMLError, match="V1 clips overlap"):
        build_fcpxml(plan, project_root=root)

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from rabbithole.fcpxml import FCPXMLError, build_fcpxml
from rabbithole.resolve_manifest import compile_resolve_plan
from test_resolve_manifest import _project


def test_fcpxml_is_deterministic_and_uses_valid_effect_references(tmp_path):
    root = _project(tmp_path)
    plan = compile_resolve_plan(root)

    first = build_fcpxml(plan, project_root=root)
    second = build_fcpxml(plan, project_root=root)
    document = ET.fromstring(first)

    assert first == second
    assert document.attrib["version"] == "1.10"
    effects = {
        effect.attrib["id"]: effect.attrib["name"]
        for effect in document.findall("./resources/effect")
    }
    transitions = document.findall("./event/project/sequence/spine/transition")
    assert transitions
    for transition in transitions:
        filter_video = transition.find("filter-video")
        assert filter_video is not None
        assert filter_video.attrib["ref"] in effects


def test_fcpxml_maps_primary_and_connected_items_to_resolve_tracks(tmp_path):
    root = _project(tmp_path)
    plan = compile_resolve_plan(root)
    document = ET.fromstring(build_fcpxml(plan, project_root=root))

    spine = document.find("./event/project/sequence/spine")
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

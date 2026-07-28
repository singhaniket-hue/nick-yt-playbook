from __future__ import annotations

import json
from pathlib import Path

from rabbithole.resolve_manifest import compile_resolve_plan, write_resolve_bundle


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "repo" / "projects" / "episode"
    (root / "narration").mkdir(parents=True)
    (root / "edit").mkdir()
    (root / "assets").mkdir()
    (root / "research").mkdir()
    (root / "narration" / "vo.wav").write_bytes(b"narration-v1")
    (root / "research" / "bite.wav").write_bytes(b"source-bite-v1")
    (root / "assets" / "plate.mp4").write_bytes(b"plate-v1")
    (root / "assets" / "capture.png").write_bytes(b"capture-v1")
    _write_json(
        root / "narration" / "timing.json",
        {
            "duration_seconds": 4.0,
            "word_count": 4,
            "words": [
                [0, "One", 0.0, 0.4],
                [1, "sentence.", 0.5, 1.1],
                [2, "Next", 2.0, 2.4],
                [3, "line.", 2.5, 3.1],
            ],
            "markers": [
                {
                    "kind": "SHOT",
                    "arg": "plate opening",
                    "word_index": 0,
                    "line": 1,
                    "seconds": 0.0,
                },
                {
                    "kind": "SHOT",
                    "arg": "screenshot account post",
                    "word_index": 2,
                    "line": 2,
                    "seconds": 2.0,
                },
                {
                    "kind": "CHAPTER",
                    "arg": "Evidence",
                    "word_index": 2,
                    "line": 2,
                    "seconds": 2.0,
                },
            ],
        },
    )
    _write_json(
        root / "edit" / "edl.json",
        {
            "quality": "draft",
            "mode": "standard",
            "duration_seconds": 4.0,
            "cut_count": 3,
            "average_shot_length": 4 / 3,
            "cuts": [
                {
                    "index": 0,
                    "start": 0.0,
                    "end": 1.0,
                    "slot_id": "s001",
                    "origin": "primary",
                    "framing": "wide",
                    "transition": "cut",
                    "reason": "opening",
                },
                {
                    "index": 1,
                    "start": 1.0,
                    "end": 2.0,
                    "slot_id": "s001",
                    "origin": "primary",
                    "framing": "close",
                    "transition": "cross dissolve",
                    "reason": "reframe",
                },
                {
                    "index": 2,
                    "start": 2.0,
                    "end": 4.0,
                    "slot_id": "s002",
                    "origin": "primary",
                    "framing": "medium",
                    "transition": "cut",
                    "reason": "show evidence",
                },
            ],
            "overlays": [
                {
                    "kind": "subtitle",
                    "start": 0.0,
                    "end": 1.1,
                    "text": "One & sentence.",
                    "detail": {},
                },
                {
                    "kind": "source_caption",
                    "start": 2.0,
                    "end": 3.0,
                    "text": "Archive <2024>",
                    "detail": {"position": "lower-left"},
                },
            ],
        },
    )
    _write_json(
        root / "provenance.json",
        [
            {
                "asset_id": "plate-s001",
                "tier": "primary",
                "provider": "local",
                "original_url": "",
                "license": "owned",
                "retrieved_at": "2026-07-28T00:00:00Z",
                "local_path": "assets/plate.mp4",
                "used_in_slots": ["s001"],
                "notes": "",
            },
            {
                "asset_id": "capture-s002",
                "tier": "primary",
                "provider": "archive.example",
                "original_url": "https://example.test/post",
                "license": "fair-use evidence",
                "retrieved_at": "2026-07-28T00:00:00Z",
                "local_path": "projects/episode/assets/capture.png",
                "used_in_slots": [],
                "notes": "screenshot evidence capture",
            },
        ],
    )
    _write_json(
        root / "research" / "source-audio.json",
        {
            "clips": [
                {
                    "local_path": "research/bite.wav",
                    "source_start": 0.25,
                    "timeline_start": 2.0,
                    "duration": 1.0,
                    "gain_db": -2.0,
                    "duck_vo_db": -18.0,
                }
            ]
        },
    )
    _write_json(
        root / "research" / "highlights.json",
        {"highlights": [{"start": 2.1, "end": 2.4, "text": "Verify post"}]},
    )
    return root


def test_compile_is_deterministic_and_preserves_render_offsets(tmp_path):
    root = _project(tmp_path)

    first = compile_resolve_plan(root)
    second = compile_resolve_plan(root)

    assert first == second
    assert first["build_id"].startswith("b-")
    assert first["timeline_name"].startswith("AUTO_BUILD_")
    assert first["project_root"] == "."
    assert first["fps"] == 30
    assert first["compiler_version"] == "resolve-compiler.v3"
    assert first["render"]["format"] == "mp4"
    assert first["render"]["codec"] == "H264"
    assert first["render"]["mode"] == "single_clip"
    assert first["render"]["settings"]["ExportAudio"] is True
    assert first["render"]["settings"]["SubtitleFormat"] == "BurnIn"
    assert first["style"]["schema_version"] == "resolve-style.v1"
    assert len(first["style"]["contract_sha256"]) == 64
    assert first["style"]["grade"]["lut_sha256"]
    assert first["style"]["grade"]["drx_sha256"] is None
    assert first["style"]["grade"]["clip_ids"] == []
    assert first["timeline_validation"] == {
        "start_frame": 0,
        "end_frame": first["duration_frames"],
        "video_clip_count": 3,
        "video_title_count": 1,
        "audio_clip_count": 2,
        "subtitle_count": len(first["subtitles"]),
    }
    assert first["clips"][0]["source_start_frame"] == 0
    assert first["clips"][1]["source_start_frame"] == 30
    assert first["clips"][1]["source_end_frame"] == 60
    assert first["clips"][2]["binding"] == "asset_id_suffix"
    assert first["clips"][2]["track"] == "V2"
    assert first["clips"][2]["media_path"] == "assets/capture.png"
    assert [track["id"] for track in first["tracks"]["audio"]] == [
        "A1",
        "A2",
        "A3",
        "A4",
        "A5",
    ]
    assert [clip["track"] for clip in first["audio"]] == ["A1", "A2"]
    assert first["highlights"][0]["start_frame"] == 63
    assert not first["missing_media"]


def test_legacy_projects_slug_path_rebases_inside_transferred_episode(tmp_path):
    root = _project(tmp_path)
    transferred = tmp_path / "transferred" / "episode"
    transferred.parent.mkdir()
    root.rename(transferred)

    plan = compile_resolve_plan(transferred)

    evidence = next(clip for clip in plan["clips"] if clip["track"] == "V2")
    assert evidence["media_path"] == "assets/capture.png"
    assert not plan["missing_media"]


def test_build_id_includes_bound_media_bytes(tmp_path):
    root = _project(tmp_path)
    before = compile_resolve_plan(root)["build_id"]

    (root / "assets" / "plate.mp4").write_bytes(b"plate-v2")
    after = compile_resolve_plan(root)["build_id"]

    assert before != after


def test_cross_track_transition_is_explicit_manual_review(tmp_path):
    root = _project(tmp_path)
    edl_path = root / "edit" / "edl.json"
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    edl["cuts"][2]["transition"] = "cross_dissolve"
    _write_json(edl_path, edl)

    plan = compile_resolve_plan(root)

    reviews = [
        item
        for item in plan["review_flags"]
        if item["kind"] == "manual_cross_track_transition"
    ]
    assert len(reviews) == 1
    assert reviews[0]["frame"] == plan["clips"][2]["start_frame"]
    assert "V1 to V2" in reviews[0]["message"]


def test_bundle_paths_and_current_pointer_are_atomic_contract(tmp_path):
    root = _project(tmp_path)

    result = write_resolve_bundle(root)

    assert result["plan_path"].is_file()
    assert result["fcpxml_path"].is_file()
    assert result["current_path"] == root / "resolve" / "current.json"
    current = json.loads(result["current_path"].read_text(encoding="utf-8"))
    assert current["build_id"] == result["build_id"]
    assert current["plan_path"] == result["plan"]["output_paths"]["plan"]
    assert current["fcpxml_path"] == result["plan"]["output_paths"]["fcpxml"]
    assert (
        current["fcpxml_sha256"]
        == result["plan"]["output_paths"]["fcpxml_sha256"]
        == result["fcpxml_sha256"]
    )

from __future__ import annotations

import json
import hashlib
import wave
from pathlib import Path

import pytest

from rabbithole.resolve_manifest import (
    build_resolve_srt,
    compile_resolve_plan,
    write_resolve_bundle,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_wave(
    path: Path,
    *,
    channels: int,
    sample_rate: int,
    seconds: float = 1.0,
) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(
            b"\x00\x00" * channels * round(sample_rate * seconds)
        )


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


def _make_primary_video_source(
    root: Path,
    *,
    notes: str,
    provider: str = "www.youtube.com",
    retrieved_at: str = "2026-07-28T00:00:00Z",
) -> None:
    provenance_path = root / "provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    source = provenance[0]
    source["provider"] = provider
    source["original_url"] = "https://www.youtube.com/watch?v=source"
    source["retrieved_at"] = retrieved_at
    source["notes"] = notes
    _write_json(provenance_path, provenance)


def _add_generated_sound_library(
    root: Path,
    *,
    windows_separators: bool = False,
) -> None:
    timing_path = root / "narration" / "timing.json"
    timing = json.loads(timing_path.read_text(encoding="utf-8"))
    timing["markers"].extend(
        [
            {
                "kind": "MUSIC",
                "arg": "drone-low",
                "word_index": 0,
                "line": 1,
                "seconds": 0.0,
            },
            {
                "kind": "SILENCE",
                "arg": "0.5s",
                "word_index": 2,
                "line": 2,
                "seconds": 2.0,
            },
            {
                "kind": "MUSIC",
                "arg": "out",
                "word_index": 3,
                "line": 2,
                "seconds": 3.0,
            },
            {
                "kind": "SFX",
                "arg": "sub-drop",
                "word_index": 3,
                "line": 2,
                "seconds": 3.25,
            },
        ]
    )
    _write_json(timing_path, timing)

    library = root / "assets" / "soundlib"
    (library / "beds").mkdir(parents=True)
    (library / "beds" / "raw").mkdir()
    (library / "sfx").mkdir()
    for variant in range(2):
        (library / "beds" / f"drone-low-{variant:02d}.wav").write_bytes(
            f"playable-bed-{variant}".encode()
        )
        (library / "beds" / "raw" / f"drone-low-{variant:02d}.wav").write_bytes(
            f"raw-bed-{variant}".encode()
        )
    (library / "sfx" / "sub-drop.wav").write_bytes(b"generated-sub-drop")

    separator = "\\" if windows_separators else "/"
    _write_json(
        library / "manifest.json",
        {
            "entries": {
                "beds/drone-low/0": {
                    "duration_seconds": 4.0,
                    "path": f"beds{separator}raw{separator}drone-low-00.wav",
                    "variant": 0,
                },
                "beds/drone-low/1": {
                    "duration_seconds": 4.0,
                    "path": f"beds{separator}raw{separator}drone-low-01.wav",
                    "variant": 1,
                },
                "sfx/sub-drop": {
                    "duration_seconds": 0.5,
                    "path": f"sfx{separator}sub-drop.wav",
                    "variant": 0,
                },
            }
        },
    )
    _write_json(
        root / "brief.json",
        {
            "sound_design": {
                "manifest_path": (
                    "assets\\soundlib\\manifest.json"
                    if windows_separators
                    else "assets/soundlib/manifest.json"
                )
            }
        },
    )


def _add_prepared_sound_stems(root: Path) -> Path:
    source_audio = root / "research" / "source-audio.json"
    if source_audio.exists():
        source_audio.unlink()
    contract = {"generator_version": "resolve-audio-stems.v1", "inputs": []}
    fingerprint = hashlib.sha256(
        json.dumps(
            contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    stem_dir = root / "resolve" / "audio-stems" / fingerprint
    stem_dir.mkdir(parents=True)
    music = stem_dir / "music-stem.wav"
    sfx = stem_dir / "sfx-stem.wav"
    music.write_bytes(b"approved-music-stem")
    sfx.write_bytes(b"approved-sfx-stem")
    manifest_path = stem_dir / "manifest.json"
    _write_json(
        manifest_path,
        {
            "schema_version": "resolve-audio-stems.v1",
            "generator_version": "resolve-audio-stems.v1",
            "fingerprint": fingerprint,
            "duration_seconds": 4.0,
            "contract": contract,
            "entries": {
                "music": {
                    "path": music.name,
                    "sha256": _sha256(music),
                    "duration_seconds": 4.0,
                    "channels": 1,
                    "sample_rate": 44100,
                    "codec": "pcm_s16le",
                    "track": "A3",
                    "kind": "music",
                },
                "sfx": {
                    "path": sfx.name,
                    "sha256": _sha256(sfx),
                    "duration_seconds": 4.0,
                    "channels": 1,
                    "sample_rate": 44100,
                    "codec": "pcm_s16le",
                    "track": "A4",
                    "kind": "sfx",
                },
            },
            "mix_semantics": {
                "bed_tiling": "qsin_constant_power",
                "raw_library_retained": True,
                "master_gain_db": -0.125,
            },
            "findings": [],
        },
    )
    _write_json(
        root / "resolve" / "audio-stems" / "current.json",
        {
            "schema_version": "resolve-audio-stems.v1",
            "fingerprint": fingerprint,
            "manifest_path": f"{fingerprint}/manifest.json",
            "manifest_sha256": _sha256(manifest_path),
        },
    )
    return manifest_path


def test_compile_is_deterministic_and_preserves_render_offsets(tmp_path):
    root = _project(tmp_path)

    first = compile_resolve_plan(root)
    second = compile_resolve_plan(root)

    assert first == second
    assert first["build_id"].startswith("b-")
    assert first["timeline_name"].startswith("AUTO_BUILD_")
    assert first["project_root"] == "."
    assert first["fps"] == 30
    assert first["compiler_version"] == "resolve-compiler.v7"
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


def test_audio_source_metadata_is_probed_for_portable_fcpxml(tmp_path):
    root = _project(tmp_path)
    _write_wave(
        root / "narration" / "vo.wav",
        channels=1,
        sample_rate=44_100,
        seconds=4.0,
    )
    _write_wave(
        root / "research" / "bite.wav",
        channels=2,
        sample_rate=48_000,
        seconds=2.0,
    )

    plan = compile_resolve_plan(root)
    narration = next(item for item in plan["audio"] if item["track"] == "A1")
    source_bite = next(item for item in plan["audio"] if item["track"] == "A2")

    assert (narration["channels"], narration["source_sample_rate"]) == (1, 44_100)
    assert (source_bite["channels"], source_bite["source_sample_rate"]) == (
        2,
        48_000,
    )


def test_generated_sound_markers_compile_to_editable_a3_and_a4_clips(tmp_path):
    root = _project(tmp_path)
    _add_generated_sound_library(root, windows_separators=True)

    first = compile_resolve_plan(root)
    second = compile_resolve_plan(root)

    assert first == second
    assert first["sources"]["sound_manifest"]["path"] == (
        "assets/soundlib/manifest.json"
    )
    assert [clip["track"] for clip in first["audio"]] == [
        "A1",
        "A2",
        "A3",
        "A3",
        "A4",
    ]
    music = [clip for clip in first["audio"] if clip["track"] == "A3"]
    assert [
        (
            clip["start_frame"],
            clip["end_frame"],
            clip["variant"],
            clip["media_path"],
        )
        for clip in music
    ] == [
        (0, 45, 0, "assets/soundlib/beds/drone-low-00.wav"),
        (60, 90, 1, "assets/soundlib/beds/drone-low-01.wav"),
    ]
    assert all(clip["kind"] == "music" and clip["loop"] for clip in music)
    [effect] = [clip for clip in first["audio"] if clip["track"] == "A4"]
    assert (effect["start_frame"], effect["end_frame"]) == (98, 113)
    assert effect["media_path"] == "assets/soundlib/sfx/sub-drop.wav"
    assert effect["kind"] == "sfx"
    assert effect["loop"] is False
    assert all(
        clip["end_frame"] <= first["duration_frames"]
        and clip["duration_frames"] > 0
        for clip in first["audio"]
    )
    assert first["timeline_validation"]["audio_clip_count"] == 5
    assert not first["missing_media"]


def test_prepared_mix_stems_apply_uniform_master_gain_to_all_layers(tmp_path):
    root = _project(tmp_path)
    _add_generated_sound_library(root)
    manifest_path = _add_prepared_sound_stems(root)

    plan = compile_resolve_plan(root)

    assert plan["sources"]["audio_stems"]["path"] == (
        manifest_path.relative_to(root).as_posix()
    )
    assert [clip["track"] for clip in plan["audio"]] == [
        "A1",
        "A3",
        "A4",
    ]
    [music] = [clip for clip in plan["audio"] if clip["track"] == "A3"]
    [effect] = [clip for clip in plan["audio"] if clip["track"] == "A4"]
    assert music["asset_id"] == "sound-stem-music"
    assert effect["asset_id"] == "sound-stem-sfx"
    assert music["media_path"].endswith("/music-stem.wav")
    assert effect["media_path"].endswith("/sfx-stem.wav")
    assert music["mix_baked"] is True
    assert effect["mix_baked"] is True
    [narration] = [clip for clip in plan["audio"] if clip["track"] == "A1"]
    assert narration["gain_db"] == music["gain_db"] == effect["gain_db"] == -0.125
    assert music["start_frame"] == effect["start_frame"] == 0
    assert music["end_frame"] == effect["end_frame"] == plan["duration_frames"]
    assert all(
        "assets/soundlib/" not in clip["media_path"]
        for clip in plan["audio"]
        if clip["track"] in {"A3", "A4"}
    )
    assert plan["timeline_validation"]["audio_clip_count"] == 3


def test_prepared_mix_stems_reject_source_audio_until_ducking_is_baked(tmp_path):
    root = _project(tmp_path)
    _add_generated_sound_library(root)
    source_audio = (
        json.loads((root / "research" / "source-audio.json").read_text("utf-8"))
    )
    _add_prepared_sound_stems(root)
    _write_json(root / "research" / "source-audio.json", source_audio)

    with pytest.raises(ValueError, match="cannot be combined with"):
        compile_resolve_plan(root)


def test_changed_prepared_stem_is_rejected_before_compile(tmp_path):
    root = _project(tmp_path)
    _add_generated_sound_library(root)
    manifest_path = _add_prepared_sound_stems(root)
    (manifest_path.parent / "music-stem.wav").write_bytes(b"changed-after-approval")

    with pytest.raises(ValueError, match="missing or changed"):
        compile_resolve_plan(root)


def test_music_and_sfx_markers_require_generated_sound_manifest(tmp_path):
    root = _project(tmp_path)
    timing_path = root / "narration" / "timing.json"
    timing = json.loads(timing_path.read_text(encoding="utf-8"))
    timing["markers"].extend(
        [
            {"kind": "MUSIC", "arg": "drone-low", "seconds": 0.0},
            {"kind": "SFX", "arg": "sub-drop", "seconds": 1.0},
        ]
    )
    _write_json(timing_path, timing)

    with pytest.raises(ValueError, match="will not silently drop"):
        compile_resolve_plan(root)


def test_configured_sound_manifest_must_stay_inside_project(tmp_path):
    root = _project(tmp_path)
    _write_json(
        root / "brief.json",
        {"sound_design": {"manifest_path": "../shared/soundlib/manifest.json"}},
    )

    with pytest.raises(
        ValueError,
        match="manifest_path contains an escaping or ambiguous component",
    ):
        compile_resolve_plan(root)


def test_missing_generated_sound_file_is_a_blocking_media_review(tmp_path):
    root = _project(tmp_path)
    _add_generated_sound_library(root)
    missing = root / "assets" / "soundlib" / "sfx" / "sub-drop.wav"
    missing.unlink()

    plan = compile_resolve_plan(root)

    effect = next(clip for clip in plan["audio"] if clip["track"] == "A4")
    assert effect["exists"] is False
    assert effect["sha256"] is None
    assert any(
        item["kind"] == "missing_audio_file"
        and item["path"] == "assets/soundlib/sfx/sub-drop.wav"
        for item in plan["missing_media"]
    )
    assert any(
        item["kind"] == "missing_audio"
        and item["severity"] == "error"
        and item["asset_id"] == effect["asset_id"]
        for item in plan["review_flags"]
    )


def test_direct_source_cuts_get_one_merged_editable_attribution(tmp_path):
    root = _project(tmp_path)
    _make_primary_video_source(
        root,
        notes=(
            "catalogue metadata: artifact_id='wt-aqua-s001'; title='aqua'; "
            "date='2013-09-23'; source_role='primary-original'; "
            "rights_note='Brief excerpt; credit the original.'"
        ),
    )

    plan = compile_resolve_plan(root)

    generated = [
        overlay
        for overlay in plan["overlays"]
        if overlay.get("generated")
    ]
    assert len(generated) == 1
    caption = generated[0]
    assert caption["kind"] == "source_caption"
    assert caption["track"] == "V3"
    assert (caption["start_frame"], caption["end_frame"]) == (0, 60)
    assert caption["duration_frames"] == 60
    assert caption["text"] == "SOURCE · aqua · 2013-09-23"
    assert caption["editable"] is True
    assert caption["detail"]["position"] == "lower-left"
    assert caption["detail"]["source_title"] == "aqua"
    assert caption["detail"]["source_date"] == "2013-09-23"
    assert caption["detail"]["clip_ids"] == [
        plan["clips"][0]["id"],
        plan["clips"][1]["id"],
    ]
    assert "2026-07-28" not in caption["text"]
    normalized = next(
        item
        for item in plan["provenance"]
        if item["asset_id"] == "plate-s001"
    )
    assert normalized["source_title"] == "aqua"
    assert normalized["source_date"] == "2013-09-23"
    assert normalized["source_role"] == "primary-original"
    # Existing authored overlay and marker survive synthesis unchanged.
    assert any(
        overlay["text"] == "Archive <2024>"
        and not overlay.get("generated", False)
        for overlay in plan["overlays"]
    )
    assert any(marker["arg"] == "Evidence" for marker in plan["markers"])


def test_retrieval_timestamp_is_never_used_as_source_date(tmp_path):
    root = _project(tmp_path)
    _make_primary_video_source(
        root,
        notes=(
            "catalogue metadata: artifact_id='wt-aqua-s001'; title='aqua'; "
            "source_role='primary-original'"
        ),
        retrieved_at="2099-12-31T23:59:59Z",
    )

    plan = compile_resolve_plan(root)

    [caption] = [
        overlay
        for overlay in plan["overlays"]
        if overlay.get("generated")
    ]
    assert caption["text"] == "SOURCE · aqua"
    assert caption["detail"]["source_date"] == ""
    assert "2099" not in caption["text"]


def test_authored_source_caption_covers_span_without_generated_duplicate(
    tmp_path,
):
    root = _project(tmp_path)
    _make_primary_video_source(
        root,
        notes=(
            "catalogue metadata: artifact_id='wt-aqua-s001'; title='aqua'; "
            "date='2013-09-23'; source_role='primary-original'"
        ),
    )
    edl_path = root / "edit" / "edl.json"
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    edl["overlays"].append(
        {
            "kind": "source_caption",
            "start": 0.0,
            "end": 2.0,
            "text": "Authored source label",
            "detail": {"position": "lower-left"},
        }
    )
    _write_json(edl_path, edl)

    plan = compile_resolve_plan(root)

    assert not [
        overlay
        for overlay in plan["overlays"]
        if overlay.get("generated")
        and overlay["asset_id"] == "plate-s001"
    ]
    assert any(
        overlay["text"] == "Authored source label"
        and not overlay.get("generated", False)
        for overlay in plan["overlays"]
    )


def test_known_burned_attribution_provider_does_not_get_second_caption(
    tmp_path,
):
    root = _project(tmp_path)
    _make_primary_video_source(
        root,
        provider="rabbithole-source-frame",
        notes=(
            "catalogue metadata: artifact_id='wt-aqua-s001'; title='aqua'; "
            "date='2013-09-23'; source_role='primary-original'"
        ),
    )

    plan = compile_resolve_plan(root)

    assert not [overlay for overlay in plan["overlays"] if overlay.get("generated")]
    source = next(
        item
        for item in plan["provenance"]
        if item["asset_id"] == "plate-s001"
    )
    assert source["source_attribution_burned"] is True


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


def test_license_reviews_only_cover_active_non_authored_media(tmp_path):
    root = _project(tmp_path)
    provenance_path = root / "provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance[0]["provider"] = "rabbithole-cards"
    provenance[0]["license"] = ""
    provenance[1]["license"] = ""
    provenance.append(
        {
            "asset_id": "card-s999--retired-20260729T000000Z",
            "tier": "atmospheric",
            "provider": "rabbithole-cards",
            "original_url": "",
            "license": "",
            "retrieved_at": "2026-07-29T00:00:00Z",
            "local_path": "revisions/quarantine/card-s999.mp4",
            "used_in_slots": [],
            "notes": "retired generated card",
        }
    )
    _write_json(provenance_path, provenance)

    plan = compile_resolve_plan(root)

    warnings = [
        item
        for item in plan["review_flags"]
        if item["kind"] == "license_missing"
    ]
    assert [item["asset_id"] for item in warnings] == ["capture-s002"]


def test_bundle_paths_and_current_pointer_are_atomic_contract(tmp_path):
    root = _project(tmp_path)

    result = write_resolve_bundle(root)

    assert result["plan_path"].is_file()
    assert result["fcpxml_path"].is_file()
    assert result["subtitles_path"].is_file()
    assert result["current_path"] == root / "resolve" / "current.json"
    current = json.loads(result["current_path"].read_text(encoding="utf-8"))
    assert current["build_id"] == result["build_id"]
    assert current["plan_path"] == result["plan"]["output_paths"]["plan"]
    assert current["fcpxml_path"] == result["plan"]["output_paths"]["fcpxml"]
    assert (
        current["subtitles_path"]
        == result["plan"]["output_paths"]["subtitles"]
    )
    assert (
        current["fcpxml_sha256"]
        == result["plan"]["output_paths"]["fcpxml_sha256"]
        == result["fcpxml_sha256"]
    )
    assert (
        current["subtitles_sha256"]
        == result["plan"]["output_paths"]["subtitles_sha256"]
        == result["subtitles_sha256"]
        == _sha256(result["subtitles_path"])
    )
    assert result["subtitles_path"].read_text(encoding="utf-8") == (
        "1\n"
        "00:00:00,000 --> 00:00:01,100\n"
        "One & sentence.\n"
    )


def test_resolve_srt_is_sorted_utf8_and_frame_deterministic():
    plan = {
        "fps": 30,
        "subtitles": [
            {
                "id": "later",
                "start_frame": 30,
                "end_frame": 61,
                "text": "English stays Latin",
            },
            {
                "id": "first",
                "start_frame": 1,
                "end_frame": 30,
                "text": "रात finally silent है।",
            },
        ],
    }

    assert build_resolve_srt(plan) == (
        "1\n"
        "00:00:00,033 --> 00:00:01,000\n"
        "रात finally silent है।\n\n"
        "2\n"
        "00:00:01,000 --> 00:00:02,033\n"
        "English stays Latin\n"
    )

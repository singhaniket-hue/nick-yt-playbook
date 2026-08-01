from __future__ import annotations

import json
import hashlib
import wave
from pathlib import Path

import pytest
from jsonschema.validators import validator_for

import rabbithole.resolve_manifest as resolve_manifest
from rabbithole.resolve_manifest import (
    ResolveManifestError,
    _assign_non_overlapping_title_tracks,
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
                "notes": "page capture of screenshot evidence",
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


def _add_cold_open_override(root: Path) -> Path:
    crackle = root / "assets" / "crackle.wav"
    _write_wave(crackle, channels=2, sample_rate=48_000, seconds=0.25)
    override_path = root / "resolve-overrides.json"
    _write_json(
        override_path,
        {
            "cold_open": {
                "duration_seconds": 1,
                "video": [
                    {
                        "asset_id": "plate-s001",
                        "timeline_start": 0,
                        "source_start": 0,
                        "duration": 1,
                        "source_audio": True,
                    }
                ],
                "sfx": {
                    "local_path": "assets/crackle.wav",
                    "timeline_start": 0,
                    "source_start": 0,
                    "duration": 0.25,
                    "gain_db": -2,
                },
            }
        },
    )
    return override_path


def _configure_visual_subtitle_case(
    root: Path,
    *,
    slot_kind: str,
    provider: str,
    notes: str,
) -> str:
    timing_path = root / "narration" / "timing.json"
    timing = json.loads(timing_path.read_text(encoding="utf-8"))
    second_shot = next(
        marker
        for marker in timing["markers"]
        if marker["kind"] == "SHOT" and marker["seconds"] == 2.0
    )
    second_shot["arg"] = f"{slot_kind} retained visual"
    _write_json(timing_path, timing)

    visual_path = root / "assets" / "retained-visual.mp4"
    visual_path.write_bytes(b"retained-visual-v1")
    provenance_path = root / "provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance[1].update(
        {
            "provider": provider,
            "local_path": "assets/retained-visual.mp4",
            "used_in_slots": ["s002"],
            "notes": notes,
        }
    )
    _write_json(provenance_path, provenance)

    cue_text = "Evidence remains readable"
    edl_path = root / "edit" / "edl.json"
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    edl["overlays"].append(
        {
            "kind": "subtitle",
            "start": 2.1,
            "end": 3.0,
            "text": cue_text,
            "detail": {},
        }
    )
    _write_json(edl_path, edl)
    return cue_text


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
    assert first["compiler_version"] == "resolve-compiler.v13"
    assert "cold_open" not in first
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
        "video_transition_count": 1,
        "audio_clip_count": 2,
        "subtitle_count": len(first["subtitles"]),
    }
    assert first["upload_subtitles"] == first["subtitles"]
    assert first["subtitle_policy"]["policy_version"] == (
        "presentation-subtitles.v1"
    )
    assert first["subtitle_policy"]["timeline_track_name"] == (
        "PRESENTATION_SUBTITLES"
    )
    assert first["subtitle_policy"]["upload_cue_count"] == 1
    assert first["subtitle_policy"]["presentation_cue_count"] == 1
    assert first["subtitle_policy"]["track_style"] == {
        "schema_version": "presentation-subtitle-style.v1",
        "track_name": "PRESENTATION_SUBTITLES",
        "font_color": "#FFFFFF",
        "background_color": "#000000",
        "minimum_background_opacity": 0.65,
        "position": "lower-center-title-safe",
        "application": "manual-resolve-track-style",
        "render_approval_required": True,
        "contract_sha256": first["subtitle_policy"]["track_style"][
            "contract_sha256"
        ],
    }
    assert len(
        first["subtitle_policy"]["track_style"]["contract_sha256"]
    ) == 64
    assert any(
        flag["kind"] == "presentation_caption_readability"
        and flag["severity"] == "human"
        and flag["resolved"] is False
        for flag in first["review_flags"]
    )
    [screenshot_exclusion] = first["subtitle_policy"]["exclusion_intervals"]
    assert (
        screenshot_exclusion["start_frame"],
        screenshot_exclusion["end_frame"],
        screenshot_exclusion["reasons"],
    ) == (60, 120, ["text_led_visual"])
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


@pytest.mark.parametrize("with_cold_open", [False, True])
def test_written_v12_plan_conforms_to_repository_schema(
    tmp_path,
    monkeypatch,
    with_cold_open,
):
    root = _project(tmp_path)
    overrides_path = None
    if with_cold_open:
        overrides_path = _add_cold_open_override(root)
        monkeypatch.setattr(
            resolve_manifest,
            "_probe_audio_metadata",
            lambda _path: {"channels": 2, "sample_rate": 48_000},
        )

    result = write_resolve_bundle(
        root,
        overrides_path=overrides_path,
        update_current=False,
    )
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "schemas"
        / "resolve-plan.v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    validator_class(schema).validate(result["plan"])


def test_cold_open_override_prefixes_plan_and_owns_build_identity(
    tmp_path, monkeypatch
):
    root = _project(tmp_path)
    override_path = _add_cold_open_override(root)
    monkeypatch.setattr(
        resolve_manifest,
        "_probe_audio_metadata",
        lambda _path: {"channels": 2, "sample_rate": 48_000},
    )

    baseline = compile_resolve_plan(root)
    first = compile_resolve_plan(root, overrides_path=override_path)
    second = compile_resolve_plan(root, overrides_path=override_path)

    assert first == second
    assert first["build_id"] != baseline["build_id"]
    assert first["timeline_name"] == (
        f"AUTO_BUILD_{first['checksums']['build_fingerprint_sha256'][:12].upper()}"
    )
    assert first["output_paths"]["plan"] == (
        f"resolve/builds/{first['build_id']}/resolve-plan.v1.json"
    )
    assert first["output_paths"]["fcpxml"] == (
        f"resolve/builds/{first['build_id']}/timeline.fcpxml"
    )
    assert first["output_paths"]["subtitles"] == (
        f"resolve/builds/{first['build_id']}/subtitles.srt"
    )
    assert first["output_paths"]["presentation_subtitles"] == (
        f"resolve/builds/{first['build_id']}/presentation-subtitles.srt"
    )

    prefix = first["cold_open"]
    assert prefix["prefix_frames"] == 30
    assert len(prefix["clips"]) == 1
    assert {item["role"] for item in prefix["media_inputs"]} == {
        "video",
        "source_audio",
        "sfx",
    }
    assert first["checksums"]["cold_open_contract_sha256"] == prefix[
        "contract_sha256"
    ]
    assert first["duration_frames"] == baseline["duration_frames"] + 30
    assert first["timing"]["duration_frames"] == (
        baseline["timing"]["duration_frames"] + 30
    )

    narrative_clips = [
        item for item in first["clips"] if item.get("origin") != "cold_open"
    ]
    assert min(item["start_frame"] for item in narrative_clips) == 30
    assert narrative_clips[0]["source_start_frame"] == 0
    assert all(item["frame"] >= 30 for item in first["markers"])
    assert all(item["start_frame"] >= 30 for item in first["subtitles"])
    assert all(item["start_frame"] >= 30 for item in first["upload_subtitles"])
    assert all(item["start_frame"] >= 30 for item in first["overlays"])
    assert all(
        item["track"] in {"A2", "A4"}
        for item in first["audio"]
        if item["start_frame"] < 30
    )
    assert first["timeline_validation"]["end_frame"] == first["duration_frames"]
    assert first["timeline_validation"]["video_clip_count"] == (
        baseline["timeline_validation"]["video_clip_count"] + 1
    )
    assert first["timeline_validation"]["audio_clip_count"] == (
        baseline["timeline_validation"]["audio_clip_count"] + 2
    )

    bundle = write_resolve_bundle(
        root,
        overrides_path=override_path,
        update_current=False,
    )
    assert bundle["build_id"] == first["build_id"]
    assert bundle["plan_path"].parent.name == first["build_id"]
    assert "cold-open-001" in bundle["fcpxml_path"].read_text(encoding="utf-8")


def test_cold_open_sfx_bytes_change_build_identity(tmp_path, monkeypatch):
    root = _project(tmp_path)
    override_path = _add_cold_open_override(root)
    monkeypatch.setattr(
        resolve_manifest,
        "_probe_audio_metadata",
        lambda _path: {"channels": 2, "sample_rate": 48_000},
    )

    before = compile_resolve_plan(root, overrides_path=override_path)
    crackle = root / "assets" / "crackle.wav"
    crackle.write_bytes(crackle.read_bytes() + b"changed")
    after = compile_resolve_plan(root, overrides_path=override_path)

    assert after["build_id"] != before["build_id"]
    assert after["checksums"]["build_fingerprint_sha256"] != before[
        "checksums"
    ]["build_fingerprint_sha256"]
    sfx = next(
        item for item in after["cold_open"]["media_inputs"] if item["role"] == "sfx"
    )
    assert sfx["sha256"] == _sha256(crackle)


def test_invalid_cold_open_override_uses_public_manifest_error(tmp_path):
    root = _project(tmp_path)
    override_path = root / "resolve-overrides.json"
    _write_json(
        override_path,
        {"cold_open": {"duration_seconds": 1, "video": []}},
    )

    with pytest.raises(ResolveManifestError, match="invalid cold_open override"):
        compile_resolve_plan(root, overrides_path=override_path)


def test_presentation_subtitles_exclude_cold_open_and_text_led_visuals(
    tmp_path,
):
    root = _project(tmp_path)
    edl_path = root / "edit" / "edl.json"
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    edl["overlays"].extend(
        [
            {
                "kind": "subtitle_exclusion",
                "start": 0.0,
                "end": 0.5,
                "text": "",
                "detail": {"reason": "cold_open"},
            },
            {
                "kind": "subtitle",
                "start": 1.8,
                "end": 2.3,
                "text": "Boundary caption",
                "detail": {},
            },
            {
                "kind": "subtitle",
                "start": 2.5,
                "end": 3.0,
                "text": "Article caption",
                "detail": {},
            },
        ]
    )
    _write_json(edl_path, edl)

    plan = compile_resolve_plan(root)

    assert [cue["text"] for cue in plan["upload_subtitles"]] == [
        "One & sentence.",
        "Boundary caption",
        "Article caption",
    ]
    assert [
        (cue["start_frame"], cue["end_frame"], cue["text"])
        for cue in plan["subtitles"]
    ] == [
        (15, 33, "One & sentence."),
        (54, 60, "Boundary caption"),
    ]
    assert all(
        overlay["kind"] != "subtitle_exclusion"
        for overlay in plan["overlays"]
    )
    policy = plan["subtitle_policy"]
    assert [
        (item["start_frame"], item["end_frame"], item["reasons"])
        for item in policy["exclusion_intervals"]
    ] == [
        (0, 15, ["cold_open"]),
        (60, 120, ["text_led_visual"]),
    ]
    assert policy["upload_cue_count"] == 3
    assert policy["presentation_cue_count"] == 2
    assert policy["affected_upload_cue_count"] == 3
    assert policy["fully_suppressed_upload_cue_count"] == 1
    assert len(policy["contract_sha256"]) == 64
    assert plan["timeline_validation"]["subtitle_count"] == 2

    result = write_resolve_bundle(root, update_current=False)
    upload_srt = result["subtitles_path"].read_text(encoding="utf-8")
    presentation_srt = result["presentation_subtitles_path"].read_text(
        encoding="utf-8"
    )
    assert "Article caption" in upload_srt
    assert "Article caption" not in presentation_srt
    assert "00:00:00,500 --> 00:00:01,100" in presentation_srt
    assert "00:00:01,800 --> 00:00:02,000" in presentation_srt


@pytest.mark.parametrize("technical_kind", ["browser", "screenshot", "graphic"])
def test_generic_technical_source_frame_kind_keeps_presentation_caption(
    tmp_path, technical_kind
):
    root = _project(tmp_path)
    cue_text = _configure_visual_subtitle_case(
        root,
        slot_kind=technical_kind,
        provider="rabbithole-source-frame",
        notes="silent retained source frame at 1.250s",
    )

    plan = compile_resolve_plan(root)

    assert cue_text in [cue["text"] for cue in plan["upload_subtitles"]]
    assert cue_text in [cue["text"] for cue in plan["subtitles"]]
    assert technical_kind not in plan["subtitle_policy"]["text_led_slot_kinds"]
    assert not any(
        interval["start_frame"] < 90 and interval["end_frame"] > 63
        for interval in plan["subtitle_policy"]["exclusion_intervals"]
    )


def test_page_capture_metadata_suppresses_only_presentation_sidecar(tmp_path):
    root = _project(tmp_path)
    cue_text = _configure_visual_subtitle_case(
        root,
        slot_kind="screenshot",
        provider="archive.example",
        notes="page capture with claim target and browser framing",
    )

    plan = compile_resolve_plan(root)

    assert cue_text in [cue["text"] for cue in plan["upload_subtitles"]]
    assert cue_text not in [cue["text"] for cue in plan["subtitles"]]
    assert any(
        interval["start_frame"] <= 63
        and interval["end_frame"] >= 90
        and "text_led_visual" in interval["reasons"]
        for interval in plan["subtitle_policy"]["exclusion_intervals"]
    )

    result = write_resolve_bundle(root, update_current=False)
    assert cue_text in result["subtitles_path"].read_text(encoding="utf-8")
    assert cue_text not in result["presentation_subtitles_path"].read_text(
        encoding="utf-8"
    )


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


def test_simultaneous_source_caption_and_chapter_card_use_separate_tracks(
    tmp_path,
):
    root = _project(tmp_path)
    edl_path = root / "edit" / "edl.json"
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    edl["overlays"].extend(
        [
            {
                "kind": "source_caption",
                "start": 0.0,
                "end": 1.0,
                "text": "Source label",
                "detail": {"position": "lower-left"},
            },
            {
                "kind": "chapter_card",
                "start": 0.0,
                "end": 1.2,
                "text": "Chapter One",
                "detail": {},
            },
        ]
    )
    _write_json(edl_path, edl)

    plan = compile_resolve_plan(root)

    tracks_by_text = {
        overlay["text"]: overlay["track"]
        for overlay in plan["overlays"]
        if overlay.get("text") in {"Source label", "Chapter One"}
    }
    assert tracks_by_text == {
        "Source label": "V3",
        "Chapter One": "V4",
    }


def test_title_allocator_reserves_future_fixed_v4_interval():
    overlays = [
        {
            "id": "a",
            "kind": "chapter_card",
            "track": "V3",
            "start_frame": 0,
            "end_frame": 10,
            "text": "Short flexible title",
        },
        {
            "id": "b",
            "kind": "chapter_card",
            "track": "V3",
            "start_frame": 0,
            "end_frame": 20,
            "text": "Long flexible title",
        },
        {
            "id": "c",
            "kind": "texture",
            "track": "V4",
            "start_frame": 10,
            "end_frame": 15,
            "text": "Fixed finishing overlay",
        },
    ]

    _assign_non_overlapping_title_tracks(overlays)

    assert {overlay["id"]: overlay["track"] for overlay in overlays} == {
        "a": "V4",
        "b": "V3",
        "c": "V4",
    }


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
    assert result["presentation_subtitles_path"].is_file()
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
    assert (
        current["presentation_subtitles_path"]
        == result["plan"]["output_paths"]["presentation_subtitles"]
    )
    assert (
        current["presentation_subtitles_sha256"]
        == result["plan"]["output_paths"]["presentation_subtitles_sha256"]
        == result["presentation_subtitles_sha256"]
        == _sha256(result["presentation_subtitles_path"])
    )
    assert result["subtitles_path"].read_text(encoding="utf-8") == (
        "1\n"
        "00:00:00,000 --> 00:00:01,100\n"
        "One & sentence.\n"
    )
    assert result["presentation_subtitles_path"].read_text(
        encoding="utf-8"
    ) == result["subtitles_path"].read_text(encoding="utf-8")


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

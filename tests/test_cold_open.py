from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from rabbithole.cold_open import (
    ColdOpenError,
    compile_cold_open,
    shift_plan_for_prefix,
)


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _fixture(tmp_path: Path) -> tuple[Path, list[dict[str, object]]]:
    root = tmp_path / "episode"
    root.mkdir()
    first = _write(root / "assets" / "first.mp4", b"first-video")
    second = _write(root / "assets" / "second.mov", b"second-video")
    _write(root / "assets" / "soundlib" / "sfx" / "crackle.wav", b"wave")
    provenance: list[dict[str, object]] = [
        {
            "asset_id": "video-one",
            "local_path": "assets/first.mp4",
            "media_type": "video",
            "sha256": hashlib.sha256(first.read_bytes()).hexdigest(),
        },
        {
            "asset_id": "video-two",
            "local_path": "assets/second.mov",
            "media_type": "video",
            "sha256": hashlib.sha256(second.read_bytes()).hexdigest(),
        },
    ]
    return root, provenance


def _config() -> dict[str, object]:
    return {
        "duration_seconds": 11,
        "video": [
            {
                "asset_id": "video-one",
                "timeline_start": 0,
                "source_start": 0.25,
                "duration": 10,
                "source_audio": True,
            },
            {
                "asset_id": "video-two",
                "timeline_start": 10,
                "source_start": 1,
                "duration": 1,
                "source_audio": True,
            },
        ],
        "sfx": {
            "local_path": "assets/soundlib/sfx/crackle.wav",
            "timeline_start": 0,
            "source_start": 0,
            "duration": 0.6,
            "gain_db": -2,
        },
    }


def _probe(_path: Path) -> dict[str, int]:
    return {"channels": 2, "sample_rate": 48_000}


def test_compile_cold_open_builds_exact_v1_a2_and_a4_prefix(tmp_path: Path) -> None:
    root, provenance = _fixture(tmp_path)

    prefix = compile_cold_open(
        _config(), root=root, fps=30, provenance=provenance, probe_audio=_probe
    )

    assert prefix["schema_version"] == "resolve-cold-open.v1"
    assert prefix["prefix_frames"] == 330
    assert [(item["start_frame"], item["end_frame"]) for item in prefix["clips"]] == [
        (0, 300),
        (300, 330),
    ]
    assert all(item["track"] == "V1" for item in prefix["clips"])
    assert prefix["clips"][0]["source_start_frame"] == 8
    assert prefix["clips"][0]["source_end_frame"] == 308

    source_audio = [item for item in prefix["audio"] if item["track"] == "A2"]
    sfx = [item for item in prefix["audio"] if item["track"] == "A4"]
    assert len(source_audio) == 2
    assert len(sfx) == 1
    for video, sound in zip(prefix["clips"], source_audio, strict=True):
        assert sound["asset_id"] != video["asset_id"]
        assert sound["media_path"] == video["media_path"]
        assert sound["start_frame"] == video["start_frame"]
        assert sound["end_frame"] == video["end_frame"]
        assert sound["source_start_frame"] == video["source_start_frame"]
        assert sound["channels"] == 2
        assert sound["source_sample_rate"] == 48_000
    assert (sfx[0]["start_frame"], sfx[0]["end_frame"]) == (0, 18)
    assert sfx[0]["media_path"] == "assets/soundlib/sfx/crackle.wav"
    assert len(prefix["media_inputs"]) == 5
    assert all(len(item["sha256"]) == 64 for item in prefix["media_inputs"])
    assert len(prefix["contract_sha256"]) == 64


def test_compile_cold_open_is_deterministic(tmp_path: Path) -> None:
    root, provenance = _fixture(tmp_path)
    first = compile_cold_open(
        _config(), root=root, fps=30, provenance=provenance, probe_audio=_probe
    )
    second = compile_cold_open(
        copy.deepcopy(_config()),
        root=root,
        fps=30,
        provenance=copy.deepcopy(provenance),
        probe_audio=_probe,
    )
    assert first == second


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda config: config["video"][1].update({"timeline_start": 9}),
            "overlap",
        ),
        (
            lambda config: config["video"][1].update(
                {"timeline_start": 10.5, "duration": 0.5}
            ),
            "gap",
        ),
        (
            lambda config: config["video"][1].update({"duration": 2}),
            "past",
        ),
        (
            lambda config: config["video"][0].update({"duration": -1}),
            "positive duration",
        ),
    ],
)
def test_compile_cold_open_fails_closed_on_invalid_coverage(
    tmp_path: Path, mutate, message: str
) -> None:
    root, provenance = _fixture(tmp_path)
    config = _config()
    mutate(config)
    with pytest.raises(ColdOpenError, match=message):
        compile_cold_open(
            config, root=root, fps=30, provenance=provenance, probe_audio=_probe
        )


def test_compile_cold_open_rejects_missing_or_non_video_provenance(
    tmp_path: Path,
) -> None:
    root, provenance = _fixture(tmp_path)
    config = _config()
    config["video"][0]["asset_id"] = "missing"
    with pytest.raises(ColdOpenError, match="unknown provenance"):
        compile_cold_open(
            config, root=root, fps=30, provenance=provenance, probe_audio=_probe
        )

    still = _write(root / "assets" / "still.png", b"still")
    provenance[0] = {
        "asset_id": "video-one",
        "local_path": "assets/still.png",
        "media_type": "still",
        "sha256": hashlib.sha256(still.read_bytes()).hexdigest(),
    }
    with pytest.raises(ColdOpenError, match="not video"):
        compile_cold_open(
            _config(), root=root, fps=30, provenance=provenance, probe_audio=_probe
        )


def test_compile_cold_open_rejects_escaping_and_missing_media(tmp_path: Path) -> None:
    root, provenance = _fixture(tmp_path)
    outside = _write(tmp_path / "outside.mp4", b"outside")
    provenance[0] = {
        "asset_id": "video-one",
        "local_path": "../outside.mp4",
        "media_type": "video",
        "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
    }
    with pytest.raises(ColdOpenError, match="escapes project root"):
        compile_cold_open(
            _config(), root=root, fps=30, provenance=provenance, probe_audio=_probe
        )

    provenance[0]["local_path"] = "assets/absent.mp4"
    with pytest.raises(ColdOpenError, match="missing or empty"):
        compile_cold_open(
            _config(), root=root, fps=30, provenance=provenance, probe_audio=_probe
        )


def test_compile_cold_open_requires_embedded_audio_metadata(tmp_path: Path) -> None:
    root, provenance = _fixture(tmp_path)
    with pytest.raises(ColdOpenError, match="no readable audio metadata"):
        compile_cold_open(
            _config(),
            root=root,
            fps=30,
            provenance=provenance,
            probe_audio=lambda _path: None,
        )


def _plan() -> dict[str, object]:
    return {
        "duration_frames": 900,
        "clips": [
            {
                "id": "clip-main",
                "index": 0,
                "track": "V1",
                "start_frame": 0,
                "end_frame": 90,
                "duration_frames": 90,
                "source_start_frame": 12,
                "source_end_frame": 102,
                "media_path": "assets/main.mp4",
            }
        ],
        "markers": [{"id": "marker-main", "frame": 0}],
        "subtitles": [
            {
                "id": "presentation",
                "start_frame": 0,
                "end_frame": 30,
                "duration_frames": 30,
            }
        ],
        "upload_subtitles": [
            {
                "id": "upload",
                "start_frame": 0,
                "end_frame": 30,
                "duration_frames": 30,
            }
        ],
        "subtitle_policy": {
            "exclusion_intervals": [
                {
                    "id": "existing-exclusion",
                    "start_frame": 60,
                    "end_frame": 90,
                    "duration_frames": 30,
                }
            ],
            "contract_sha256": "old",
        },
        "overlays": [
            {
                "id": "title",
                "kind": "title",
                "track": "V3",
                "start_frame": 30,
                "end_frame": 60,
                "duration_frames": 30,
            }
        ],
        "audio": [
            {
                "id": "narration",
                "asset_id": "narration-vo",
                "track": "A1",
                "start_frame": 0,
                "end_frame": 900,
                "duration_frames": 900,
                "source_start_frame": 0,
                "media_path": "narration/vo.wav",
            },
            {
                "id": "music",
                "asset_id": "music",
                "track": "A3",
                "start_frame": 0,
                "end_frame": 300,
                "duration_frames": 300,
                "source_start_frame": 45,
                "media_path": "assets/music.wav",
            },
        ],
        "highlights": [
            {
                "id": "highlight",
                "start_frame": 60,
                "end_frame": 75,
                "duration_frames": 15,
            }
        ],
        "review_flags": [{"id": "review", "frame": 0}],
        "missing_media": [{"id": "missing", "start_frame": 0}],
        "timing": {
            "duration_frames": 900,
            "words": [
                {
                    "index": 0,
                    "word": "First",
                    "start_frame": 0,
                    "end_frame": 15,
                }
            ],
        },
        "timeline_validation": {
            "start_frame": 0,
            "end_frame": 900,
            "video_clip_count": 1,
            "audio_clip_count": 2,
            "subtitle_count": 1,
        },
        "statistics": {
            "cut_count": 1,
            "average_shot_length_frames": 90,
        },
        "checksums": {"build_fingerprint_sha256": "existing"},
    }


def test_shift_plan_for_prefix_offsets_every_timeline_payload_only(tmp_path: Path) -> None:
    root, provenance = _fixture(tmp_path)
    prefix = compile_cold_open(
        _config(), root=root, fps=30, provenance=provenance, probe_audio=_probe
    )
    plan = _plan()
    untouched = copy.deepcopy(plan)

    shifted = shift_plan_for_prefix(plan, prefix)

    assert plan == untouched
    assert shifted["duration_frames"] == 1230
    assert shifted["timing"]["duration_frames"] == 1230
    main_clip = next(item for item in shifted["clips"] if item["id"] == "clip-main")
    assert (main_clip["start_frame"], main_clip["end_frame"]) == (330, 420)
    assert (main_clip["source_start_frame"], main_clip["source_end_frame"]) == (
        12,
        102,
    )
    assert shifted["markers"][0]["frame"] == 330
    assert shifted["subtitles"][0]["start_frame"] == 330
    assert shifted["upload_subtitles"][0]["start_frame"] == 330
    assert shifted["overlays"][0]["start_frame"] == 360
    assert shifted["highlights"][0]["start_frame"] == 390
    assert shifted["review_flags"][0]["frame"] == 330
    assert shifted["missing_media"][0]["start_frame"] == 330
    assert shifted["timing"]["words"][0]["start_frame"] == 330
    narration = next(item for item in shifted["audio"] if item["id"] == "narration")
    music = next(item for item in shifted["audio"] if item["id"] == "music")
    assert narration["start_frame"] == 330
    assert narration["source_start_frame"] == 0
    assert music["start_frame"] == 330
    assert music["source_start_frame"] == 45
    assert shifted["subtitle_policy"]["exclusion_intervals"][0]["reasons"] == [
        "cold_open"
    ]
    assert shifted["subtitle_policy"]["exclusion_intervals"][1][
        "start_frame"
    ] == 390
    assert shifted["timeline_validation"]["end_frame"] == 1230
    assert shifted["timeline_validation"]["video_clip_count"] == 3
    assert shifted["timeline_validation"]["audio_clip_count"] == 5
    assert shifted["timeline_validation"]["subtitle_count"] == 1
    assert shifted["statistics"]["cut_count"] == 3
    assert shifted["checksums"]["cold_open_contract_sha256"] == prefix[
        "contract_sha256"
    ]
    assert all(item["start_frame"] >= 330 for item in shifted["subtitles"])
    assert all(item["start_frame"] >= 330 for item in shifted["upload_subtitles"])
    assert all(item["start_frame"] >= 330 for item in shifted["overlays"])
    assert all(
        item["track"] in {"A2", "A4"}
        for item in shifted["audio"]
        if item["start_frame"] < 330
    )


def test_shift_plan_rejects_malformed_compiled_prefix() -> None:
    prefix = {
        "prefix_frames": 30,
        "clips": [],
        "audio": [],
        "media_inputs": [],
    }
    with pytest.raises(ColdOpenError, match="no V1 coverage"):
        shift_plan_for_prefix(_plan(), prefix)

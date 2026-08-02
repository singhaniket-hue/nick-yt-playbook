from __future__ import annotations

import json
import shutil
import struct
import wave
from pathlib import Path

import pytest

import rabbithole.resolve_audio as resolve_audio


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _silence(path: Path, seconds: float = 1.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44_100)
        handle.writeframes(b"\x00\x00" * round(seconds * 44_100))
    return path


def _tone(
    path: Path,
    *,
    amplitude: int = 10_000,
    sample_rate: int = 44_100,
    seconds: float = 1.0,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sample_count = round(sample_rate * seconds)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(struct.pack(f"<{sample_count}h", *([amplitude] * sample_count)))
    return path


def _episode(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "projects" / "episode"
    timing_path = root / "narration" / "timing.json"
    _write_json(
        timing_path,
        {
            "duration_seconds": 1.0,
            "words": [],
            "markers": [
                {"kind": "MUSIC", "arg": "drone-low", "seconds": 0.0},
                {"kind": "SFX", "arg": "sub-drop", "seconds": 0.70},
                {"kind": "SILENCE", "arg": "0.1s", "seconds": 0.75},
            ],
        },
    )
    _silence(root / "narration" / "vo.wav")
    library = root / "assets" / "soundlib"
    entries = {}
    for variant in range(4):
        playable = library / "beds" / f"drone-low-{variant:02d}.wav"
        playable.parent.mkdir(parents=True, exist_ok=True)
        playable.write_bytes(f"bed-{variant}".encode())
        entries[f"beds/drone-low/{variant}"] = {
            "path": f"beds/raw/drone-low-{variant:02d}.wav",
            "duration_seconds": 30.0,
            "variant": variant,
        }
    effect = library / "sfx" / "sub-drop.wav"
    effect.parent.mkdir(parents=True, exist_ok=True)
    effect.write_bytes(b"effect")
    entries["sfx/sub-drop"] = {
        "path": "sfx/sub-drop.wav",
        "duration_seconds": 0.5,
        "variant": 0,
    }
    manifest = library / "manifest.json"
    _write_json(manifest, {"entries": entries})
    return root, manifest


def test_prepare_builds_immutable_stems_and_reuses_exact_inputs(
    tmp_path, monkeypatch
):
    root, sound_manifest = _episode(tmp_path)
    calls: list[str] = []

    def fake_bed(spans, duration, out_path, work_dir, library=None):
        calls.append("bed")
        return _silence(Path(out_path), duration), []

    def fake_sfx(events, duration, out_path, work_dir, categories, library=None):
        calls.append("sfx")
        return _silence(Path(out_path), duration), []

    def fake_duck(source, windows, out_path):
        calls.append("duck")
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, out_path)
        return Path(out_path)

    monkeypatch.setattr(resolve_audio, "build_bed_layer", fake_bed)
    monkeypatch.setattr(resolve_audio, "build_sfx_layer", fake_sfx)
    monkeypatch.setattr(resolve_audio, "duck", fake_duck)

    first = resolve_audio.prepare_resolve_audio_stems(root, sound_manifest)
    old_music = first["directory"] / "music-stem.wav"
    old_bytes = old_music.read_bytes()
    second = resolve_audio.prepare_resolve_audio_stems(root, sound_manifest)

    assert first["generated"] is True
    assert second["generated"] is False
    assert first["fingerprint"] == second["fingerprint"]
    assert first["directory"] == second["directory"]
    assert calls == ["bed", "sfx", "duck", "duck"]
    assert old_music.read_bytes() == old_bytes
    assert first["manifest"]["mix_semantics"]["master_gain_db"] == 0.0
    assert any(
        finding["severity"] == "warning"
        and "falls inside the silence window" in finding["message"]
        for finding in first["manifest"]["findings"]
    )
    assert (root / "resolve" / "audio-stems" / "current.json").is_file()

    timing_path = root / "narration" / "timing.json"
    timing = json.loads(timing_path.read_text(encoding="utf-8"))
    timing["markers"][1]["seconds"] = 0.5
    _write_json(timing_path, timing)
    third = resolve_audio.prepare_resolve_audio_stems(root, sound_manifest)

    assert third["generated"] is True
    assert third["fingerprint"] != first["fingerprint"]
    assert third["directory"] != first["directory"]
    assert first["directory"].is_dir()
    assert old_music.read_bytes() == old_bytes


def test_prepare_refuses_missing_approved_generated_cue(tmp_path):
    root, sound_manifest = _episode(tmp_path)
    (root / "assets" / "soundlib" / "sfx" / "sub-drop.wav").unlink()

    try:
        resolve_audio.prepare_resolve_audio_stems(root, sound_manifest)
    except resolve_audio.ResolveAudioStemError as exc:
        assert "[SFX:sub-drop]" in str(exc)
    else:
        raise AssertionError("missing approved cue should block stem preparation")


def test_repository_text_hash_is_line_ending_portable(tmp_path):
    source = tmp_path / "policy.py"
    source.write_bytes(b"one = 1\r\ntwo = 2\r\n")
    windows_hash = resolve_audio._sha256_text_lf(source)
    source.write_bytes(b"one = 1\ntwo = 2\n")

    assert resolve_audio._sha256_text_lf(source) == windows_hash


def test_frame_alignment_matches_resolve_half_up_rounding():
    assert resolve_audio._frame_aligned_duration(1.01) == (30, 44_100, 1.0)
    assert resolve_audio._frame_aligned_duration(1.02) == (31, 45_570, 31 / 30)


def test_frame_aligned_wav_pads_and_truncates_to_exact_samples(tmp_path):
    short = _silence(tmp_path / "short.wav", 0.9)
    padded = tmp_path / "padded.wav"
    resolve_audio._write_frame_aligned_pcm_wav(
        short,
        padded,
        sample_count=44_100,
    )
    with wave.open(str(padded), "rb") as handle:
        assert handle.getnframes() == 44_100

    long = _silence(tmp_path / "long.wav", 1.1)
    truncated = tmp_path / "truncated.wav"
    resolve_audio._write_frame_aligned_pcm_wav(
        long,
        truncated,
        sample_count=44_100,
    )
    with wave.open(str(truncated), "rb") as handle:
        assert handle.getnframes() == 44_100


def test_prepare_rejects_source_audio_until_source_ducking_is_baked(tmp_path):
    root, sound_manifest = _episode(tmp_path)
    _write_json(
        root / "research" / "source-audio.json",
        {
            "clips": [
                {
                    "local_path": "narration/vo.wav",
                    "source_start": 0.0,
                    "timeline_start": 0.0,
                    "duration": 0.25,
                }
            ]
        },
    )

    try:
        resolve_audio.prepare_resolve_audio_stems(root, sound_manifest)
    except resolve_audio.ResolveAudioStemError as exc:
        assert "does not yet support" in str(exc)
    else:
        raise AssertionError("source-audio bites should block stem preparation")


def test_gain_bake_is_exact_length_scaled_and_source_preserving(tmp_path):
    root = tmp_path / "episode"
    source = _tone(root / "narration" / "vo.wav", seconds=0.9)
    source_before = source.read_bytes()
    clip = {
        "id": "voice",
        "media_path": "narration/vo.wav",
        "path_kind": "project-relative",
        "sha256": resolve_audio._sha256_file(source),
        "source_start_frame": 0,
        "duration_frames": 30,
        "gain_db": -6.020599913,
    }

    first = resolve_audio.prepare_resolve_gain_bake(root, clip, fps=30)
    output = first["audio_path"]
    with wave.open(str(output), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 44_100
        assert handle.getnframes() == 44_100
        pcm = struct.unpack("<44100h", handle.readframes(44_100))

    assert first["generated"] is True
    assert output.name.startswith("vo.gain-")
    assert all(abs(sample - 5_000) <= 1 for sample in pcm[:39_690])
    assert set(pcm[39_690:]) == {0}
    assert first["manifest"]["output"]["padded_sample_count"] == 4_410
    assert source.read_bytes() == source_before

    reused = resolve_audio.prepare_resolve_gain_bake(root, clip, fps=30)
    changed = resolve_audio.prepare_resolve_gain_bake(
        root,
        {**clip, "gain_db": -3.0},
        fps=30,
    )
    assert reused["generated"] is False
    assert reused["fingerprint"] == first["fingerprint"]
    assert changed["fingerprint"] != first["fingerprint"]
    assert changed["audio_path"].name != first["audio_path"].name
    assert source.read_bytes() == source_before


def test_gain_bake_fingerprint_distinguishes_identical_project_sources(tmp_path):
    root = tmp_path / "episode"
    first_source = _tone(
        root / "resolve" / "audio-stems" / "first" / "music-stem.wav"
    )
    second_source = root / "resolve" / "audio-stems" / "second" / "music-stem.wav"
    second_source.parent.mkdir(parents=True)
    second_source.write_bytes(first_source.read_bytes())
    source_sha = resolve_audio._sha256_file(first_source)

    clip = {
        "id": "music",
        "media_path": "resolve/audio-stems/first/music-stem.wav",
        "path_kind": "project-relative",
        "sha256": source_sha,
        "source_start_frame": 0,
        "duration_frames": 30,
        "gain_db": -3.0,
    }

    first = resolve_audio.prepare_resolve_gain_bake(root, clip, fps=30)
    second = resolve_audio.prepare_resolve_gain_bake(
        root,
        {
            **clip,
            "media_path": "resolve/audio-stems/second/music-stem.wav",
        },
        fps=30,
    )

    assert first["fingerprint"] != second["fingerprint"]
    assert first["manifest"]["source"]["media_path"].endswith(
        "/first/music-stem.wav"
    )
    assert second["manifest"]["source"]["media_path"].endswith(
        "/second/music-stem.wav"
    )
    assert (
        first["manifest"]["output"]["sha256"]
        == second["manifest"]["output"]["sha256"]
    )


def test_gain_bake_plan_rewrites_nested_cold_open_audio(tmp_path):
    root = tmp_path / "episode"
    source = _tone(root / "assets" / "crackle.wav", seconds=0.6)
    clip = {
        "id": "cold-crackle",
        "asset_id": "crackle",
        "track": "A4",
        "kind": "cold_open_sfx",
        "start_frame": 0,
        "end_frame": 18,
        "duration_frames": 18,
        "source_start_frame": 0,
        "media_path": "assets/crackle.wav",
        "path_kind": "project-relative",
        "exists": True,
        "sha256": resolve_audio._sha256_file(source),
        "channels": 1,
        "source_sample_rate": 44_100,
        "gain_db": -2.0,
        "duck_vo_db": 0.0,
    }
    plan = {
        "fps": 30,
        "audio": [dict(clip)],
        "cold_open": {"audio": [dict(clip)]},
    }

    result = resolve_audio.bake_resolve_plan_audio_gains(root, plan)

    assert result["count"] == 1
    assert plan["audio"][0]["gain_db"] == 0.0
    assert plan["audio"][0]["gain_baked_db"] == -2.0
    assert plan["audio"][0]["media_path"] == plan["cold_open"]["audio"][0][
        "media_path"
    ]
    assert plan["cold_open"]["audio"][0]["gain_db"] == 0.0


def test_gain_bake_plan_rejects_non_wav_non_unity_audio(tmp_path):
    root = tmp_path / "episode"
    source = root / "assets" / "music.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"not-decoded-before-format-gate")
    clip = {
        "id": "music",
        "media_path": "assets/music.mp3",
        "path_kind": "project-relative",
        "exists": True,
        "sha256": resolve_audio._sha256_file(source),
        "source_start_frame": 0,
        "duration_frames": 30,
        "gain_db": -3.0,
    }
    plan = {"fps": 30, "audio": [clip]}

    with pytest.raises(
        resolve_audio.ResolveAudioBakeError,
        match=(
            r"audio clip 'music' uses non-zero gain -3 dB.*"
            r"Convert it to an uncompressed integer PCM WAV.*"
            r"refusing to emit editable timeline gain"
        ),
    ):
        resolve_audio.bake_resolve_plan_audio_gains(root, plan)

    assert clip["media_path"] == "assets/music.mp3"
    assert clip["gain_db"] == -3.0
    assert not (root / "resolve" / "audio-bakes").exists()


def test_gain_bake_plan_leaves_non_wav_unity_audio_unchanged(tmp_path):
    root = tmp_path / "episode"
    clip = {
        "id": "music",
        "media_path": "assets/music.mp3",
        "path_kind": "project-relative",
        "exists": True,
        "sha256": "0" * 64,
        "source_start_frame": 0,
        "duration_frames": 30,
        "gain_db": 0.0,
    }
    plan = {"fps": 30, "audio": [clip]}

    result = resolve_audio.bake_resolve_plan_audio_gains(root, plan)

    assert result == {"count": 0, "entries": []}
    assert clip["media_path"] == "assets/music.mp3"
    assert clip["gain_db"] == 0.0
    assert not (root / "resolve" / "audio-bakes").exists()

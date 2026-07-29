from __future__ import annotations

import json
import shutil
import wave
from pathlib import Path

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

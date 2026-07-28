"""Layer 3 finish: grade filter, narration mux, deferred audio cues.

Real ffmpeg, small dimensions and short durations to keep runtime sane.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest

from rabbithole.render import deferred_audio_cues, finish, grade_filter, probe_duration

W, H, FPS = 320, 180, 30

DEFAULT_GRADE = {
    "lut": "style/luts/crowley-noir.cube",
    "grain_strength": 0.18,
    "vignette_strength": 0.35,
    "scanline_opacity": 0.12,
}


def _run(args):
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-1500:])
    return result


def _footage(path, seconds=1.0, width=W, height=H, fps=FPS):
    _run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate={fps}:duration={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ]
    )
    return path


def _vo(path, seconds=1.0):
    _run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}:sample_rate=44100",
            "-ac", "1", "-c:a", "pcm_s16le", str(path),
        ]
    )
    return path


def _identity_cube_lut(path):
    """A minimal, valid 2x2x2 identity 3D LUT -- enough for ffmpeg's lut3d to load."""
    path.write_text(
        "LUT_3D_SIZE 2\n"
        "0.0 0.0 0.0\n"
        "1.0 0.0 0.0\n"
        "0.0 1.0 0.0\n"
        "1.0 1.0 0.0\n"
        "0.0 0.0 1.0\n"
        "1.0 0.0 1.0\n"
        "0.0 1.0 1.0\n"
        "1.0 1.0 1.0\n",
        encoding="utf-8",
    )
    return path


def _streams(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(path)],
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)["streams"]


# --- grade_filter --------------------------------------------------------------


def test_grade_filter_renders_without_error(tmp_path):
    footage = _footage(tmp_path / "footage.mp4")
    vf = grade_filter(DEFAULT_GRADE, W, H)

    out = tmp_path / "graded.mp4"
    _run(["ffmpeg", "-y", "-i", str(footage), "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)])

    assert out.exists()
    assert out.stat().st_size > 0


def test_grade_filter_nonpositive_dimensions_raise():
    with pytest.raises(ValueError):
        grade_filter(DEFAULT_GRADE, 0, H)


def test_grade_filter_all_zero_treatments_is_a_real_noop():
    assert grade_filter(
        {
            "brightness": 0,
            "contrast": 1,
            "saturation": 1,
            "grain_strength": 0,
            "vignette_strength": 0,
            "scanline_opacity": 0,
        },
        W,
        H,
    ) == "null"


def test_grade_filter_maps_small_vignette_strength_to_small_angle():
    vf = grade_filter(
        {
            "brightness": 0,
            "contrast": 1,
            "saturation": 1,
            "grain_strength": 0,
            "vignette_strength": 0.08,
            "scanline_opacity": 0,
        },
        W,
        H,
    )

    assert "vignette=angle=0.125664" in vf


# --- finish ----------------------------------------------------------------


def test_finish_output_has_video_and_audio_streams(tmp_path):
    footage = _footage(tmp_path / "footage.mp4")
    vo = _vo(tmp_path / "vo.wav")

    out_path, findings = finish(footage, vo, tmp_path / "finished.mp4", DEFAULT_GRADE)

    kinds = {s["codec_type"] for s in _streams(out_path)}
    assert "video" in kinds
    assert "audio" in kinds


def test_finish_audio_is_stereo_48k_aac_for_delivery(tmp_path):
    footage = _footage(tmp_path / "footage.mp4")
    vo = _vo(tmp_path / "vo.wav")

    out_path, findings = finish(footage, vo, tmp_path / "finished.mp4", DEFAULT_GRADE)

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,channels,sample_rate",
            "-of",
            "json",
            str(out_path),
        ],
        capture_output=True,
        check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    assert stream["codec_name"] == "aac"
    assert stream["channels"] == 2
    assert stream["sample_rate"] == "48000"


def test_finish_output_duration_matches_footage_duration(tmp_path):
    footage = _footage(tmp_path / "footage.mp4", seconds=1.4)
    vo = _vo(tmp_path / "vo.wav", seconds=1.4)

    out_path, findings = finish(footage, vo, tmp_path / "finished.mp4", DEFAULT_GRADE)

    assert probe_duration(out_path) == pytest.approx(1.4, abs=0.1)


def test_finish_mismatched_audio_length_emits_warning_naming_both_durations(tmp_path):
    footage = _footage(tmp_path / "footage.mp4", seconds=2.0)
    vo = _vo(tmp_path / "vo.wav", seconds=0.3)

    out_path, findings = finish(footage, vo, tmp_path / "finished.mp4", DEFAULT_GRADE)

    warnings = [f for f in findings if f.severity == "warning"]
    assert any("2.00" in f.message and "0.30" in f.message for f in warnings)
    # The EDL/footage duration is authoritative regardless of the mismatch.
    assert probe_duration(out_path) == pytest.approx(2.0, abs=0.1)


def test_finish_video_longer_than_audio_keeps_video_duration(tmp_path):
    footage = _footage(tmp_path / "footage.mp4", seconds=1.5)
    vo = _vo(tmp_path / "vo.wav", seconds=0.5)

    out_path, findings = finish(footage, vo, tmp_path / "finished.mp4", DEFAULT_GRADE)

    assert probe_duration(out_path) == pytest.approx(1.5, abs=0.1)


def test_finish_audio_longer_than_video_keeps_video_duration(tmp_path):
    footage = _footage(tmp_path / "footage.mp4", seconds=0.5)
    vo = _vo(tmp_path / "vo.wav", seconds=1.5)

    out_path, findings = finish(footage, vo, tmp_path / "finished.mp4", DEFAULT_GRADE)

    assert probe_duration(out_path) == pytest.approx(0.5, abs=0.1)


def test_finish_absent_lut_emits_warning_and_still_produces_output(tmp_path):
    footage = _footage(tmp_path / "footage.mp4")
    vo = _vo(tmp_path / "vo.wav")
    missing_lut = tmp_path / "does-not-exist.cube"

    out_path, findings = finish(
        footage, vo, tmp_path / "finished.mp4", DEFAULT_GRADE, lut_path=missing_lut
    )

    assert out_path.exists()
    warnings = [f for f in findings if f.severity == "warning"]
    assert any("LUT" in f.message for f in warnings)


def test_finish_no_lut_path_given_also_warns_when_grade_names_one(tmp_path):
    footage = _footage(tmp_path / "footage.mp4")
    vo = _vo(tmp_path / "vo.wav")

    out_path, findings = finish(footage, vo, tmp_path / "finished.mp4", DEFAULT_GRADE)

    assert out_path.exists()
    assert any("LUT" in f.message and f.severity == "warning" for f in findings)


def test_finish_applies_lut_when_present_and_does_not_warn(tmp_path):
    footage = _footage(tmp_path / "footage.mp4")
    vo = _vo(tmp_path / "vo.wav")
    lut = _identity_cube_lut(tmp_path / "identity.cube")
    grade = dict(DEFAULT_GRADE, lut=str(lut))

    out_path, findings = finish(footage, vo, tmp_path / "finished.mp4", grade, lut_path=lut)

    assert out_path.exists()
    assert not any("LUT" in f.message for f in findings)


def test_finish_grade_with_no_lut_key_does_not_warn(tmp_path):
    footage = _footage(tmp_path / "footage.mp4")
    vo = _vo(tmp_path / "vo.wav")
    grade = {k: v for k, v in DEFAULT_GRADE.items() if k != "lut"}

    out_path, findings = finish(footage, vo, tmp_path / "finished.mp4", grade)

    assert not any("LUT" in f.message for f in findings)


# --- deferred_audio_cues ----------------------------------------------------


def _sfx(arg, seconds, word_index=0, line=1):
    return {"kind": "SFX", "arg": arg, "word_index": word_index, "line": line, "seconds": seconds}


def _music(arg, seconds, word_index=0, line=1):
    return {"kind": "MUSIC", "arg": arg, "word_index": word_index, "line": line, "seconds": seconds}


def _shot(arg, seconds, word_index=0, line=1):
    return {"kind": "SHOT", "arg": arg, "word_index": word_index, "line": line, "seconds": seconds}


def test_deferred_audio_cues_known_sfx_names_do_not_warn():
    # All seven cues in style/sfx.json are now synthesizable and placeable by
    # audiomix.build_mix -- a resolvable cue must no longer warn.
    document = {
        "markers": [
            _sfx("vhs-burst", 0.0),
            _sfx("glitch-sting", 10.0),
        ]
    }

    assert deferred_audio_cues(document) == []


def test_deferred_audio_cues_music_out_does_not_warn():
    # MUSIC:out resolves exactly (resolve_cue maps it to true silence) --
    # not a fallback, so no warning.
    document = {"markers": [_music("out", 30.0), _music("out", 40.0)]}

    assert deferred_audio_cues(document) == []


def test_deferred_audio_cues_warns_once_per_unknown_sfx_name():
    document = {
        "markers": [
            _sfx("not-a-real-cue", 0.0),
            _sfx("not-a-real-cue", 5.0),  # repeat -- should not double-warn
            _sfx("also-unknown", 10.0),
        ]
    }

    findings = deferred_audio_cues(document)

    assert len(findings) == 2
    assert all(f.severity == "warning" for f in findings)
    assert any("not-a-real-cue" in f.message for f in findings)
    assert any("also-unknown" in f.message for f in findings)


def test_deferred_audio_cues_warns_once_per_music_cue_that_falls_back():
    # Any MUSIC cue other than "out" -- including evocative licensed-track
    # names like "chasms" -- falls back to a generic drone-low bed rather
    # than actually matching (see sources/music.py resolve_cue); that's
    # still worth a warning.
    document = {"markers": [_music("chasms", 30.0), _music("chasms", 40.0)]}

    findings = deferred_audio_cues(document)

    assert len(findings) == 1
    assert "chasms" in findings[0].message


def test_deferred_audio_cues_ignores_other_marker_kinds():
    document = {"markers": [_shot("plate grain", 0.0)]}

    assert deferred_audio_cues(document) == []


def test_deferred_audio_cues_empty_for_no_markers():
    assert deferred_audio_cues({"markers": []}) == []
    assert deferred_audio_cues({}) == []


def test_deferred_audio_cues_unresolved_sfx_and_music_both_warn():
    document = {"markers": [_sfx("not-a-real-cue", 1.0), _music("chasms", 2.0)]}

    findings = deferred_audio_cues(document)

    assert len(findings) == 2


# --- finish with a pre-mixed audio track --------------------------------------


def _silence_track(path, seconds):
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", str(seconds), "-ac", "1", "-c:a", "pcm_s16le", str(path),
        ],
        capture_output=True,
        check=True,
    )
    return path


def test_finish_uses_mixed_audio_path_when_given(tmp_path):
    footage = _footage(tmp_path / "footage.mp4", seconds=2.0)
    vo = _vo(tmp_path / "vo.wav", seconds=2.0)  # a loud 440Hz tone
    mixed = _silence_track(tmp_path / "mixed.wav", 2.0)  # pure digital silence

    out_path, findings = finish(
        footage, vo, tmp_path / "finished.mp4", DEFAULT_GRADE, mixed_audio_path=mixed
    )

    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-i", str(out_path),
            "-af", "volumedetect", "-f", "null", "-",
        ],
        capture_output=True,
    )
    text = result.stderr.decode("utf-8", errors="replace")
    match = re.search(r"max_volume:\s*(-?\d+\.?\d*) dB", text)
    assert match, f"volumedetect produced no max_volume:\n{text}"
    # If finish had actually used vo_path (a loud tone) instead of
    # mixed_audio_path (silence), this would read far louder than -50 dBFS.
    assert float(match.group(1)) < -50.0


def test_finish_without_mixed_audio_path_still_uses_vo(tmp_path):
    footage = _footage(tmp_path / "footage.mp4", seconds=1.0)
    vo = _vo(tmp_path / "vo.wav", seconds=1.0)

    out_path, findings = finish(footage, vo, tmp_path / "finished.mp4", DEFAULT_GRADE)

    kinds = {s["codec_type"] for s in _streams(out_path)}
    assert "audio" in kinds


def test_finish_mismatch_check_uses_mixed_audio_path_duration(tmp_path):
    footage = _footage(tmp_path / "footage.mp4", seconds=2.0)
    vo = _vo(tmp_path / "vo.wav", seconds=2.0)  # matches footage -- no warning if used
    mixed = _silence_track(tmp_path / "mixed.wav", 0.2)  # way off -- should warn if used

    out_path, findings = finish(
        footage, vo, tmp_path / "finished.mp4", DEFAULT_GRADE, mixed_audio_path=mixed
    )

    warnings = [f for f in findings if f.severity == "warning"]
    assert any("2.00" in f.message and "0.20" in f.message for f in warnings)

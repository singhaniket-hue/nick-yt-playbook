"""Original-source audio manifest, placement, ducking, and mix integration."""

from __future__ import annotations

import json
import re
import subprocess

import pytest

from rabbithole.assemble import probe_duration
from rabbithole.audiomix import (
    build_source_audio_layer,
    duck_for_source_audio,
    mix_audio,
)
from rabbithole.sourceaudio import (
    DEFAULT_DUCK_VO_DB,
    SourceAudioBite,
    load_source_audio,
    window_source_audio,
)


def _tone(path, seconds, *, frequency=440, peak_db=-6.0):
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi",
            "-i", f"sine=frequency={frequency}:duration={seconds}:sample_rate=44100",
            "-af", f"volume={peak_db + 18.1:.2f}dB",
            "-ac", "1", "-c:a", "pcm_s16le", str(path),
        ],
        check=True,
    )
    return path


def _silence(path, seconds):
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", str(seconds), "-c:a", "pcm_s16le", str(path),
        ],
        check=True,
    )
    return path


def _segment_peak(path, start, duration):
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(duration),
            "-i", str(path), "-af", "volumedetect", "-f", "null", "-",
        ],
        capture_output=True,
    )
    text = result.stderr.decode("utf-8", errors="replace")
    match = re.search(r"max_volume:\s*(-?\d+\.?\d*) dB", text)
    return float(match.group(1)) if match else -150.0


def test_missing_manifest_is_an_opt_out(tmp_path):
    assert load_source_audio(
        tmp_path / "research" / "source-audio.json", tmp_path
    ) == []


def test_manifest_resolves_project_relative_path_and_defaults_duck(tmp_path):
    source = _tone(tmp_path / "clip.wav", 2.0)
    research = tmp_path / "research"
    research.mkdir()
    manifest = research / "source-audio.json"
    manifest.write_text(
        json.dumps(
            {
                "clips": [
                    {
                        "local_path": "clip.wav",
                        "source_start": 0.25,
                        "timeline_start": 1.0,
                        "duration": 0.75,
                        "gain_db": -2,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    bites = load_source_audio(manifest, tmp_path, episode_duration=4.0)

    assert bites == [
        SourceAudioBite(
            local_path=source.resolve(),
            source_start=0.25,
            timeline_start=1.0,
            duration=0.75,
            gain_db=-2.0,
            duck_vo_db=DEFAULT_DUCK_VO_DB,
        )
    ]


def test_window_source_audio_advances_seek_and_shifts_segment_timeline(tmp_path):
    bite = SourceAudioBite(
        local_path=tmp_path / "clip.wav",
        source_start=10.0,
        timeline_start=20.0,
        duration=8.0,
        gain_db=-1.0,
        duck_vo_db=-24.0,
    )

    selected = window_source_audio([bite], 23.0, 26.0)

    assert selected == [
        SourceAudioBite(
            local_path=bite.local_path,
            source_start=13.0,
            timeline_start=0.0,
            duration=3.0,
            gain_db=-1.0,
            duck_vo_db=-24.0,
        )
    ]


def test_build_source_audio_layer_places_only_the_authored_window(tmp_path):
    source = _tone(tmp_path / "source.wav", 2.0, frequency=880)
    bite = SourceAudioBite(
        local_path=source,
        source_start=0.5,
        timeline_start=1.0,
        duration=1.0,
        gain_db=0.0,
        duck_vo_db=-18.0,
    )

    out = build_source_audio_layer(
        [bite], 3.0, tmp_path / "source-layer.wav", tmp_path / "work"
    )

    assert probe_duration(out) == pytest.approx(3.0, abs=0.05)
    assert _segment_peak(out, 0.1, 0.5) < -60.0
    assert _segment_peak(out, 1.1, 0.5) > -15.0
    assert _segment_peak(out, 2.2, 0.5) < -60.0


def test_duck_for_source_audio_attenuates_only_inside_bite(tmp_path):
    track = _tone(tmp_path / "vo.wav", 4.0, peak_db=-6.0)
    bite = SourceAudioBite(
        local_path=tmp_path / "unused.wav",
        source_start=0.0,
        timeline_start=1.0,
        duration=2.0,
        gain_db=0.0,
        duck_vo_db=-24.0,
    )

    out = duck_for_source_audio(track, [bite], tmp_path / "ducked.wav")

    before = _segment_peak(out, 0.2, 0.5)
    inside = _segment_peak(out, 1.3, 1.0)
    after = _segment_peak(out, 3.3, 0.5)
    assert inside < before - 20.0
    assert after == pytest.approx(before, abs=0.5)


def test_mix_audio_includes_optional_source_layer_and_keeps_vo_duration(tmp_path):
    vo = _silence(tmp_path / "vo.wav", 3.0)
    bed = _silence(tmp_path / "bed.wav", 3.0)
    sfx = _silence(tmp_path / "sfx.wav", 3.0)
    source = _tone(tmp_path / "source.wav", 3.0, frequency=880)

    out = mix_audio(
        vo, bed, sfx, tmp_path / "mix.wav", source_audio_path=source
    )

    assert probe_duration(out) == pytest.approx(3.0, abs=0.05)
    assert _segment_peak(out, 0.2, 0.5) > -15.0

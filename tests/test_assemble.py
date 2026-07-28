import subprocess
import wave
from pathlib import Path

import pytest

from rabbithole.assemble import build_silence, concat_wavs, probe_duration


def _write_tone(path: Path, seconds: float) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={seconds}:sample_rate=44100",
            "-ac", "1", "-c:a", "pcm_s16le", str(path),
        ],
        check=True,
        capture_output=True,
    )


def test_build_silence_produces_a_wav_of_the_right_length(tmp_path):
    out = tmp_path / "gap.wav"

    build_silence(1.5, out)

    assert out.exists()
    assert probe_duration(out) == pytest.approx(1.5, abs=0.05)


def test_build_silence_is_mono_44100(tmp_path):
    out = tmp_path / "gap.wav"

    build_silence(0.5, out)

    with wave.open(str(out), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 44100


def test_build_silence_is_actually_silent(tmp_path):
    out = tmp_path / "gap.wav"

    build_silence(0.5, out)

    with wave.open(str(out), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    assert set(frames) == {0}


def test_concat_sums_the_durations(tmp_path):
    a, b, out = tmp_path / "a.wav", tmp_path / "b.wav", tmp_path / "out.wav"
    _write_tone(a, 1.0)
    _write_tone(b, 2.0)

    concat_wavs([a, b], out)

    assert probe_duration(out) == pytest.approx(3.0, abs=0.05)


def test_concat_preserves_order(tmp_path):
    a, gap, out = tmp_path / "a.wav", tmp_path / "gap.wav", tmp_path / "out.wav"
    _write_tone(a, 1.0)
    build_silence(1.0, gap)

    concat_wavs([a, gap, a], out)

    assert probe_duration(out) == pytest.approx(3.0, abs=0.05)


def test_concat_rejects_an_empty_list(tmp_path):
    with pytest.raises(ValueError, match="at least one"):
        concat_wavs([], tmp_path / "out.wav")

import json
import re
import subprocess

import pytest

from rabbithole.config import REPO_ROOT
from rabbithole.sources.sfx import SFX_NAMES, DEFAULT_DURATIONS, SfxSpec, build_sfx

# A synthesized cue that renders but carries no real signal should fail this
# floor, not read as "silent = broken filter chain returned nothing" -- see
# module docstring for why non-silence is the important thing to verify here.
SILENCE_FLOOR_DB = -40.0


def _probe_audio_stream(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=channels,sample_rate,codec_name",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    data = json.loads(result.stdout)
    return data["streams"][0], data["format"]


def _peak_dbfs(path):
    """Peak level in dBFS via ffmpeg's volumedetect, measured independently of
    however build_sfx did its own internal normalisation."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
    )
    text = result.stderr.decode("utf-8", errors="replace")
    match = re.search(r"max_volume:\s*(-?\d+\.?\d*) dB", text)
    assert match, f"volumedetect produced no max_volume for {path}:\n{text}"
    return float(match.group(1))


def _band_rms_db(path, highpass=None, lowpass=None):
    """RMS level (dB) of the signal after restricting it to a frequency band."""
    filters = []
    if highpass is not None:
        filters.append(f"highpass=f={highpass}")
    if lowpass is not None:
        filters.append(f"lowpass=f={lowpass}")
    filters.append("astats=metadata=0")
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", ",".join(filters), "-f", "null", "-"],
        capture_output=True,
    )
    text = result.stderr.decode("utf-8", errors="replace")
    match = re.search(r"RMS level dB:\s*(-?\d+\.?\d*|-inf)", text)
    assert match, f"astats produced no RMS level for {path}:\n{text}"
    value = match.group(1)
    return -150.0 if value == "-inf" else float(value)


def test_sfx_names_match_style_pack_registry():
    registry = json.loads((REPO_ROOT / "style" / "sfx.json").read_text(encoding="utf-8"))
    assert set(SFX_NAMES) == set(registry.keys())


def test_default_durations_cover_every_cue():
    assert set(DEFAULT_DURATIONS.keys()) == set(SFX_NAMES)
    for name, duration in DEFAULT_DURATIONS.items():
        assert duration > 0, name


@pytest.mark.parametrize("name", SFX_NAMES)
def test_each_cue_renders_a_nonempty_file(tmp_path, name):
    out = tmp_path / f"{name}.wav"

    result = build_sfx(SfxSpec(name=name, duration=DEFAULT_DURATIONS[name]), out)

    assert result == out
    assert out.exists()
    assert out.stat().st_size > 0


@pytest.mark.parametrize("name", SFX_NAMES)
def test_each_cue_duration_matches_default(tmp_path, name):
    out = tmp_path / f"{name}.wav"
    duration = DEFAULT_DURATIONS[name]

    build_sfx(SfxSpec(name=name, duration=duration), out)

    _, fmt = _probe_audio_stream(out)
    assert float(fmt["duration"]) == pytest.approx(duration, abs=0.05)


@pytest.mark.parametrize("name", SFX_NAMES)
def test_custom_duration_is_honoured_not_just_the_default(tmp_path, name):
    out = tmp_path / f"{name}.wav"
    duration = 0.22

    build_sfx(SfxSpec(name=name, duration=duration), out)

    _, fmt = _probe_audio_stream(out)
    assert float(fmt["duration"]) == pytest.approx(duration, abs=0.05)


@pytest.mark.parametrize("name", SFX_NAMES)
def test_each_cue_is_mono_44100(tmp_path, name):
    out = tmp_path / f"{name}.wav"

    build_sfx(SfxSpec(name=name, duration=DEFAULT_DURATIONS[name]), out)

    stream, _ = _probe_audio_stream(out)
    assert stream["channels"] == 1
    assert int(stream["sample_rate"]) == 44100


@pytest.mark.parametrize("name", SFX_NAMES)
def test_each_cue_clears_the_silence_floor(tmp_path, name):
    out = tmp_path / f"{name}.wav"

    build_sfx(SfxSpec(name=name, duration=DEFAULT_DURATIONS[name]), out)

    peak = _peak_dbfs(out)
    assert peak > SILENCE_FLOOR_DB, f"{name} measured {peak} dBFS -- looks silent"


@pytest.mark.parametrize("name", SFX_NAMES)
def test_each_cue_peak_lands_in_a_sane_band(tmp_path, name):
    out = tmp_path / f"{name}.wav"

    build_sfx(SfxSpec(name=name, duration=DEFAULT_DURATIONS[name]), out)

    peak = _peak_dbfs(out)
    # Not clipped at 0 dBFS, not near-silent, centred on the -6 dBFS target.
    assert peak < -0.5
    assert peak > SILENCE_FLOOR_DB
    assert peak == pytest.approx(-6.0, abs=1.5)


def test_sub_drop_has_more_low_energy_than_high(tmp_path):
    out = tmp_path / "sub-drop.wav"
    build_sfx(SfxSpec(name="sub-drop", duration=DEFAULT_DURATIONS["sub-drop"]), out)

    low = _band_rms_db(out, lowpass=100)
    high = _band_rms_db(out, highpass=1000)

    assert low > high


def test_glitch_sting_has_more_high_energy_than_low(tmp_path):
    out = tmp_path / "glitch-sting.wav"
    build_sfx(SfxSpec(name="glitch-sting", duration=DEFAULT_DURATIONS["glitch-sting"]), out)

    low = _band_rms_db(out, lowpass=100)
    high = _band_rms_db(out, highpass=1000)

    assert high > low


def test_unknown_name_raises_value_error_naming_it(tmp_path):
    with pytest.raises(ValueError, match="chainsaw-rev"):
        build_sfx(SfxSpec(name="chainsaw-rev", duration=0.3), tmp_path / "out.wav")


def test_unknown_name_error_lists_valid_names(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        build_sfx(SfxSpec(name="chainsaw-rev", duration=0.3), tmp_path / "out.wav")

    for name in SFX_NAMES:
        assert name in str(excinfo.value)


@pytest.mark.parametrize("duration", [0.0, -1.0, -0.5])
def test_nonpositive_duration_raises_value_error(tmp_path, duration):
    with pytest.raises(ValueError):
        build_sfx(SfxSpec(name="bass-thud", duration=duration), tmp_path / "out.wav")

import json
import re
import struct
import subprocess
import wave

import pytest

from rabbithole.sources.music import (
    BED_KINDS,
    BedSpec,
    build_bed,
    exact_cue_names,
    resolve_cue,
)
from rabbithole.sources.sfx import DEFAULT_DURATIONS as SFX_DEFAULT_DURATIONS
from rabbithole.sources.sfx import SfxSpec, build_sfx

SILENCE_FLOOR_DB = -40.0
NON_SILENT_KINDS = [k for k in BED_KINDS if k != "silence"]


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
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
    )
    text = result.stderr.decode("utf-8", errors="replace")
    match = re.search(r"max_volume:\s*(-?\d+\.?\d*) dB", text)
    assert match, f"volumedetect produced no max_volume for {path}:\n{text}"
    return float(match.group(1))


def _band_rms_db(path, highpass=None, lowpass=None):
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


def _read_samples(path):
    with wave.open(str(path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    return struct.unpack(f"<{len(frames) // 2}h", frames)


@pytest.mark.parametrize("kind", BED_KINDS)
def test_each_kind_renders_with_requested_duration(tmp_path, kind):
    out = tmp_path / f"{kind}.wav"
    duration = 1.5

    result = build_bed(BedSpec(kind=kind, duration=duration), out)

    assert result == out
    assert out.exists()
    _, fmt = _probe_audio_stream(out)
    assert float(fmt["duration"]) == pytest.approx(duration, abs=0.05)


@pytest.mark.parametrize("kind", BED_KINDS)
def test_each_kind_is_mono_44100(tmp_path, kind):
    out = tmp_path / f"{kind}.wav"

    build_bed(BedSpec(kind=kind, duration=1.0), out)

    stream, _ = _probe_audio_stream(out)
    assert stream["channels"] == 1
    assert int(stream["sample_rate"]) == 44100


def test_silence_is_actually_silent_every_sample_zero(tmp_path):
    out = tmp_path / "silence.wav"

    build_bed(BedSpec(kind="silence", duration=1.0), out)

    with wave.open(str(out), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    assert set(frames) == {0}


@pytest.mark.parametrize("kind", NON_SILENT_KINDS)
def test_non_silent_kinds_clear_the_silence_floor(tmp_path, kind):
    out = tmp_path / f"{kind}.wav"

    build_bed(BedSpec(kind=kind, duration=2.0), out)

    peak = _peak_dbfs(out)
    assert peak > SILENCE_FLOOR_DB, f"{kind} measured {peak} dBFS -- looks silent"


def test_drone_low_has_most_energy_below_200hz(tmp_path):
    out = tmp_path / "drone-low.wav"
    build_bed(BedSpec(kind="drone-low", duration=2.0), out)

    low = _band_rms_db(out, lowpass=200)
    high = _band_rms_db(out, highpass=200)

    assert low > high


@pytest.mark.parametrize("kind", NON_SILENT_KINDS)
def test_beds_loop_safely_no_click_at_the_boundaries(tmp_path, kind):
    out = tmp_path / f"{kind}.wav"
    build_bed(BedSpec(kind=kind, duration=2.0), out)

    samples = _read_samples(out)
    # A short fade to (near) zero at both ends means looping the bed back
    # to back never produces a discontinuity/click at the join.
    edge = 8
    boundary_peak = max(abs(s) for s in samples[:edge] + samples[-edge:])
    assert boundary_peak < 200  # out of a possible 32767


@pytest.mark.parametrize("kind", NON_SILENT_KINDS)
def test_bed_peak_sits_near_target(tmp_path, kind):
    out = tmp_path / f"{kind}.wav"
    build_bed(BedSpec(kind=kind, duration=2.0), out)

    peak = _peak_dbfs(out)
    assert peak == pytest.approx(-20.0, abs=1.5)


def test_bed_peak_is_measurably_quieter_than_an_sfx_cue_of_the_same_length(tmp_path):
    duration = SFX_DEFAULT_DURATIONS["heartbeat"]
    bed_out = tmp_path / "bed.wav"
    sfx_out = tmp_path / "sfx.wav"

    build_bed(BedSpec(kind="drone-low", duration=duration), bed_out)
    build_sfx(SfxSpec(name="heartbeat", duration=duration), sfx_out)

    bed_peak = _peak_dbfs(bed_out)
    sfx_peak = _peak_dbfs(sfx_out)

    # Real headroom, not assumed: the bed must measurably sit under the SFX
    # cue so it can play under narration without masking it.
    assert bed_peak < sfx_peak - 10.0


def test_resolve_cue_out_maps_to_silence():
    assert resolve_cue("out") == "silence"


@pytest.mark.parametrize("cue", ["chasms", "lurking", "some-unrecognised-track"])
def test_resolve_cue_unknown_falls_back_to_drone_low(cue):
    assert resolve_cue(cue) == "drone-low"


@pytest.mark.parametrize("cue", ["drone-low", "drone-tense", "pulse-slow"])
def test_resolve_cue_passes_an_implemented_bed_kind_straight_through(cue):
    """A script naming a bed kind this module implements must get that bed.

    The fallback used to be unconditional, so `[MUSIC:drone-tense]` silently
    became `drone-low`: the tense bed existed, was reachable, and was never
    used by any script that asked for it. The only tests here covered
    *unknown* cue names, which is why nothing caught it.
    """
    assert resolve_cue(cue) == cue


def test_exact_cue_names_covers_out_and_every_implemented_kind():
    """render.deferred_audio_cues asks this instead of keeping its own copy;
    if it under-reports, real fallbacks stop being warned about."""
    names = exact_cue_names()
    assert "out" in names
    for kind in BED_KINDS:
        if kind != "silence":
            assert kind in names, kind


def test_exact_cue_names_excludes_a_licensed_track_name():
    assert "chasms" not in exact_cue_names()


def test_unknown_kind_raises_value_error_naming_it(tmp_path):
    with pytest.raises(ValueError, match="lava-lamp"):
        build_bed(BedSpec(kind="lava-lamp", duration=1.0), tmp_path / "out.wav")


def test_unknown_kind_error_lists_valid_kinds(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        build_bed(BedSpec(kind="lava-lamp", duration=1.0), tmp_path / "out.wav")

    for kind in BED_KINDS:
        assert kind in str(excinfo.value)


@pytest.mark.parametrize("duration", [0.0, -1.0, -0.5])
def test_nonpositive_duration_raises_value_error(tmp_path, duration):
    with pytest.raises(ValueError):
        build_bed(BedSpec(kind="drone-low", duration=duration), tmp_path / "out.wav")

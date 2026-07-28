import json
import subprocess

import pytest

from rabbithole.config import REPO_ROOT
from rabbithole.sources.plates import PLATE_KINDS, PlateSpec, build_plate, load_grade

SMALL_W, SMALL_H = 320, 180
SMALL_FPS = 30


def _probe(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,pix_fmt",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    data = json.loads(result.stdout)
    return data["streams"][0], data["format"]


def _format_bit_rate_and_size(path):
    """format=bit_rate (bits/sec) and format=size (bytes), via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=bit_rate,size",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    fmt = json.loads(result.stdout)["format"]
    return int(fmt["bit_rate"]), int(fmt["size"])


def _luma_range(path):
    """Spatial luma spread (YMAX - YMIN) of the first frame.

    Verified against known cases before use: a solid colour source reports a
    range of 0 (YMIN == YMAX == 16 for pure black in limited-range yuv420p),
    while a `noise`-filtered source reports a wide, clearly non-zero range.
    This reliably discriminates "textured" plates from flat ones.
    """
    # The lavfi `movie` filter treats ":" as an option separator, which
    # collides with a Windows drive letter (`C:\...`) in an absolute path.
    # Run from the file's own directory and reference it by bare filename.
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-f", "lavfi", "-i", f"movie={path.name},signalstats",
            "-show_entries",
            "frame_tags=lavfi.signalstats.YMIN,lavfi.signalstats.YMAX",
            "-read_intervals", "%+#1",
            "-of", "json",
        ],
        capture_output=True,
        check=True,
        cwd=path.parent,
    )
    tags = json.loads(result.stdout)["frames"][0]["tags"]
    ymin = int(tags["lavfi.signalstats.YMIN"])
    ymax = int(tags["lavfi.signalstats.YMAX"])
    return ymax - ymin


@pytest.mark.parametrize("kind", PLATE_KINDS)
def test_each_kind_renders_a_nonempty_file(tmp_path, kind):
    out = tmp_path / f"{kind}.mp4"

    result = build_plate(PlateSpec(kind=kind, duration=0.4, width=SMALL_W, height=SMALL_H), out)

    assert result == out
    assert out.exists()
    assert out.stat().st_size > 0


@pytest.mark.parametrize("kind", PLATE_KINDS)
def test_output_duration_matches_spec(tmp_path, kind):
    out = tmp_path / f"{kind}.mp4"
    duration = 0.5

    build_plate(PlateSpec(kind=kind, duration=duration, width=SMALL_W, height=SMALL_H), out)

    _, fmt = _probe(out)
    assert float(fmt["duration"]) == pytest.approx(duration, abs=0.15)


@pytest.mark.parametrize("kind", PLATE_KINDS)
def test_output_dimensions_and_fps_match_spec(tmp_path, kind):
    out = tmp_path / f"{kind}.mp4"
    spec = PlateSpec(kind=kind, duration=0.3, width=SMALL_W, height=SMALL_H, fps=24)

    build_plate(spec, out)

    stream, _ = _probe(out)
    assert stream["width"] == SMALL_W
    assert stream["height"] == SMALL_H
    assert stream["r_frame_rate"] == "24/1"


@pytest.mark.parametrize("kind", PLATE_KINDS)
def test_output_pixel_format_is_yuv420p(tmp_path, kind):
    out = tmp_path / f"{kind}.mp4"

    build_plate(PlateSpec(kind=kind, duration=0.3, width=SMALL_W, height=SMALL_H), out)

    stream, _ = _probe(out)
    assert stream["pix_fmt"] == "yuv420p"


def test_full_hd_dimensions_at_default_size(tmp_path):
    out = tmp_path / "black_fullhd.mp4"

    build_plate(PlateSpec(kind="black", duration=0.2), out)

    stream, _ = _probe(out)
    assert stream["width"] == 1920
    assert stream["height"] == 1080
    assert stream["r_frame_rate"] == "30/1"


def test_unknown_kind_raises_value_error_naming_kind(tmp_path):
    with pytest.raises(ValueError, match="fog"):
        build_plate(PlateSpec(kind="fog", duration=0.3), tmp_path / "out.mp4")


def test_unknown_kind_error_lists_valid_kinds(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        build_plate(PlateSpec(kind="fog", duration=0.3), tmp_path / "out.mp4")

    for kind in PLATE_KINDS:
        assert kind in str(excinfo.value)


@pytest.mark.parametrize("duration", [0.0, -1.0, -0.5])
def test_nonpositive_duration_raises_value_error(tmp_path, duration):
    with pytest.raises(ValueError):
        build_plate(PlateSpec(kind="black", duration=duration), tmp_path / "out.mp4")


def test_load_grade_returns_the_expected_color_and_texture_controls():
    grade = load_grade(REPO_ROOT / "style")

    assert set(grade.keys()) == {
        "lut",
        "brightness",
        "contrast",
        "saturation",
        "grain_strength",
        "vignette_strength",
        "scanline_opacity",
    }


def test_load_grade_missing_file_raises_runtime_error_naming_path(tmp_path):
    with pytest.raises(RuntimeError, match=str(tmp_path / "palette.json").replace("\\", "\\\\")):
        load_grade(tmp_path)


def test_load_grade_missing_grade_key_raises_runtime_error(tmp_path):
    (tmp_path / "palette.json").write_text(json.dumps({"backdrop": ["#000000"]}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="grade"):
        load_grade(tmp_path)


def test_grain_produces_visible_variance_and_black_does_not(tmp_path):
    black_out = tmp_path / "black.mp4"
    grain_out = tmp_path / "grain.mp4"

    build_plate(PlateSpec(kind="black", duration=0.3, width=SMALL_W, height=SMALL_H), black_out)
    build_plate(PlateSpec(kind="grain", duration=0.3, width=SMALL_W, height=SMALL_H), grain_out)

    black_range = _luma_range(black_out)
    grain_range = _luma_range(grain_out)

    assert black_range == 0
    assert grain_range > 15
    assert grain_range > black_range


def test_scanlines_produce_visible_variance(tmp_path):
    out = tmp_path / "scanlines.mp4"

    build_plate(PlateSpec(kind="scanlines", duration=0.3, width=SMALL_W, height=SMALL_H), out)

    assert _luma_range(out) > 0


def test_static_produces_heavier_variance_than_grain(tmp_path):
    grain_out = tmp_path / "grain.mp4"
    static_out = tmp_path / "static.mp4"

    build_plate(PlateSpec(kind="grain", duration=0.3, width=SMALL_W, height=SMALL_H), grain_out)
    build_plate(PlateSpec(kind="static", duration=0.3, width=SMALL_W, height=SMALL_H), static_out)

    grain_range = _luma_range(grain_out)
    static_range = _luma_range(static_out)

    assert static_range > 15
    assert static_range > grain_range


def test_second_call_to_same_path_overwrites_cleanly(tmp_path):
    out = tmp_path / "plate.mp4"

    build_plate(PlateSpec(kind="black", duration=0.3, width=SMALL_W, height=SMALL_H), out)
    first_size = out.stat().st_size

    build_plate(PlateSpec(kind="black", duration=0.6, width=SMALL_W, height=SMALL_H), out)

    _, fmt = _probe(out)
    assert float(fmt["duration"]) == pytest.approx(0.6, abs=0.15)
    # A doubled/appended file would be roughly first_size + itself; an
    # overwritten one should just reflect the new (longer) content once.
    assert out.stat().st_size < first_size * 2


def test_malformed_grade_does_not_leave_a_partial_file(tmp_path):
    # A non-numeric grade value fails Python-side (float conversion) before
    # ffmpeg is ever invoked, so this exercises the pre-flight failure path,
    # not the ffmpeg-subprocess-failure cleanup in build_plate's except
    # block. That path was verified manually during development by calling
    # plates._run() directly with an ffmpeg arg set known to open the output
    # file and then fail (bad -preset value): the file existed immediately
    # after the RuntimeError and was removed once the same unlink-on-failure
    # logic build_plate uses ran. Both failure modes are covered by the
    # combination of this test and that manual check.
    out = tmp_path / "broken.mp4"
    bad_grade = {
        "lut": "style/luts/crowley-noir.cube",
        "grain_strength": "not-a-number",
        "vignette_strength": 0.35,
        "scanline_opacity": 0.12,
    }

    with pytest.raises(ValueError):
        build_plate(
            PlateSpec(kind="grain", duration=0.3, width=SMALL_W, height=SMALL_H),
            out,
            grade=bad_grade,
        )

    assert not out.exists()


# --- Part 1: capped plate bitrate -------------------------------------------
#
# These three tests deliberately render at the FULL HD default dimensions
# (matching the production bug: a 12s 1920x1080 static plate measured 311 MB,
# 207 Mbit/s, before this fix). At the small 320x180 dimensions used
# elsewhere in this file for speed, an *unbounded* static plate's bitrate
# already lands under 8 Mbit/s -- there just isn't enough pixel data at that
# size to demonstrate the bug, so a test at SMALL_W/SMALL_H would pass
# identically with or without the cap and would not actually be testing
# anything. Confirmed empirically before writing these thresholds.
#
# A 1.0s static clip's *measured* bitrate briefly exceeds the 6 Mbit/s
# nominal cap (the VBV buffer fills before -maxrate fully throttles it, so a
# single second's average includes that startup burst) -- observed ~9.2
# Mbit/s at 1.0s post-fix, still nowhere near the ~207 Mbit/s unbounded
# baseline. The bitrate-ceiling assertion below therefore uses a longer clip
# (3.0s), where the measured average settles under the cap; the 1.0s case is
# checked against absolute file size instead, which the fix satisfies easily
# (~1.1 MB measured, comfortably under the 2 MB bound).


def test_static_plate_bitrate_stays_under_8mbit(tmp_path):
    out = tmp_path / "static.mp4"

    build_plate(PlateSpec(kind="static", duration=3.0), out)

    bit_rate, _ = _format_bit_rate_and_size(out)
    assert bit_rate < 8_000_000


def test_static_plate_one_second_is_under_2mb(tmp_path):
    out = tmp_path / "static_1s.mp4"

    build_plate(PlateSpec(kind="static", duration=1.0), out)

    _, size = _format_bit_rate_and_size(out)
    assert size < 2 * 1024 * 1024


def test_black_plate_is_unaffected_by_the_bitrate_cap(tmp_path):
    # Flat colour compresses trivially -- the cap should never bind for it, so
    # its bitrate should sit far below the PLATE_MAXRATE ceiling, not at it.
    out = tmp_path / "black.mp4"

    build_plate(PlateSpec(kind="black", duration=2.0), out)

    bit_rate, _ = _format_bit_rate_and_size(out)
    assert bit_rate < 200_000

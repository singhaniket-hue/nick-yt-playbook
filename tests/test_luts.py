"""Part 2: the Crowley noir 3D LUT.

Structural tests run pure Python, no ffmpeg. The final section proves the
generated .cube file is actually valid and actually does something once
ffmpeg's own `lut3d` filter loads it -- a LUT ffmpeg rejects, or one that
turns out to be an identity transform, is exactly the failure this section
exists to catch.
"""

from __future__ import annotations

import colorsys
import subprocess

from rabbithole.luts import LUT_SIZE, noir_transform, write_cube

PALETTE = {
    "backdrop": ["#000000", "#202020"],
    "accent_red": "#E02020",
    "warning_yellow": "#FFFF00",
    "warm_grey": "#C0A080",
    "text_light": "#E0E0E0",
    "grade": {"lut": "style/luts/crowley-noir.cube"},
}


def _hex_to_unit(hex_rgb):
    value = hex_rgb.lstrip("#")
    return tuple(int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))


# --- write_cube: file format ----------------------------------------------------


def test_write_cube_has_right_line_count_for_its_size(tmp_path):
    size = 4
    out_path = write_cube(tmp_path / "test.cube", size=size, palette=PALETTE)

    lines = out_path.read_text(encoding="utf-8").strip("\n").split("\n")
    assert len(lines) == 1 + size**3


def test_write_cube_header_present_and_correct(tmp_path):
    size = 5
    out_path = write_cube(tmp_path / "test.cube", size=size, palette=PALETTE)

    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == f"LUT_3D_SIZE {size}"


def test_write_cube_every_value_parses_as_float_in_unit_range(tmp_path):
    out_path = write_cube(tmp_path / "test.cube", size=6, palette=PALETTE)

    lines = out_path.read_text(encoding="utf-8").splitlines()
    for line in lines[1:]:
        parts = line.split()
        assert len(parts) == 3
        for part in parts:
            value = float(part)
            assert 0.0 <= value <= 1.0, f"{part!r} in {line!r} is outside [0,1]"


def test_write_cube_creates_parent_directories(tmp_path):
    out_path = tmp_path / "nested" / "dirs" / "crowley-noir.cube"

    result = write_cube(out_path, size=3, palette=PALETTE)

    assert result == out_path
    assert out_path.exists()


# --- write_cube: axis ordering ----------------------------------------------------


def test_write_cube_axis_order_red_fastest_blue_slowest(tmp_path):
    """The classic .cube mistake: getting the axis order backwards. Size 2
    makes every grid coordinate exactly 0 or 1, so index k's expected input
    triple is unambiguous: r = k % 2, g = (k // 2) % 2, b = k // 4."""
    size = 2
    out_path = write_cube(tmp_path / "test.cube", size=size, palette=PALETTE)

    data_lines = out_path.read_text(encoding="utf-8").splitlines()[1:]
    assert len(data_lines) == size**3

    for k, line in enumerate(data_lines):
        r_in = float(k % 2)
        g_in = float((k // 2) % 2)
        b_in = float(k // 4)
        expected = noir_transform(r_in, g_in, b_in, palette=PALETTE)

        actual = tuple(float(v) for v in line.split())
        for e, a in zip(expected, actual):
            assert abs(e - a) < 1e-6, f"index {k}: expected {expected} (r={r_in},g={g_in},b={b_in}), got {actual}"


# --- noir_transform: black/white -------------------------------------------------


def test_black_maps_near_black():
    r, g, b = noir_transform(0.0, 0.0, 0.0, palette=PALETTE)
    assert r < 0.06 and g < 0.06 and b < 0.10


def test_white_maps_near_white():
    r, g, b = noir_transform(1.0, 1.0, 1.0, palette=PALETTE)
    assert r > 0.95 and g > 0.95 and b > 0.95


# --- noir_transform: monotonic in luma -------------------------------------------


def _luma(r, g, b):
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_noir_transform_monotonic_in_luma_across_a_ramp():
    previous_luma = -1.0
    for i in range(32):
        x = i / 31
        r, g, b = noir_transform(x, x, x, palette=PALETTE)
        out_luma = _luma(r, g, b)
        assert out_luma >= previous_luma - 1e-9, (
            f"step {i} (input luma {x:.4f}) produced output luma {out_luma:.6f}, "
            f"darker than the previous step's {previous_luma:.6f}"
        )
        previous_luma = out_luma


def test_noir_transform_neutral_grey_stays_approximately_neutral():
    """Grey in must stay close to grey out -- some deliberate shadow tint is
    expected, but not a wild colour cast anywhere on the ramp."""
    for i in range(32):
        x = i / 31
        r, g, b = noir_transform(x, x, x, palette=PALETTE)
        spread = max(r, g, b) - min(r, g, b)
        assert spread < 0.12, f"grey input {x:.4f} produced a large colour cast: ({r:.4f},{g:.4f},{b:.4f})"


# --- noir_transform: desaturation ------------------------------------------------


def test_saturated_input_comes_out_measurably_less_saturated():
    r, g, b = 1.0, 0.0, 0.0  # pure, fully saturated red
    in_sat = colorsys.rgb_to_hsv(r, g, b)[1]

    ro, go, bo = noir_transform(r, g, b, palette=PALETTE)
    out_sat = colorsys.rgb_to_hsv(ro, go, bo)[1]

    assert out_sat < in_sat - 0.1, f"saturation barely moved: {in_sat:.3f} -> {out_sat:.3f}"


def test_accent_red_and_warm_grey_are_also_desaturated_but_not_erased():
    for name in ("accent_red", "warm_grey"):
        r, g, b = _hex_to_unit(PALETTE[name])
        in_sat = colorsys.rgb_to_hsv(r, g, b)[1]

        ro, go, bo = noir_transform(r, g, b, palette=PALETTE)
        out_sat = colorsys.rgb_to_hsv(ro, go, bo)[1]

        assert out_sat < in_sat, f"{name}: saturation did not decrease ({in_sat:.3f} -> {out_sat:.3f})"
        assert out_sat > 0.01, f"{name}: fully desaturated to grey ({out_sat:.4f}), expected some to survive"


# --- real ffmpeg: the generated LUT actually does something ----------------------


def _run(args):
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-1500:])
    return result


def _mean_abs_diff(im_a, im_b):
    from PIL import ImageChops
    import numpy as np

    diff = ImageChops.difference(im_a.convert("RGB"), im_b.convert("RGB"))
    return float(np.array(diff).mean())


def test_generated_lut_applies_via_ffmpeg_lut3d_and_changes_pixels(tmp_path):
    """Generate the real-size cube, apply it through ffmpeg's lut3d filter to
    a saturated test image, and prove the pixels actually changed. A LUT
    ffmpeg rejects, or an identity transform, is exactly the failure this
    test exists to catch."""
    cube_path = write_cube(tmp_path / "crowley-noir.cube", size=LUT_SIZE, palette=PALETTE)

    source = tmp_path / "source.png"
    _run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x90:rate=1:duration=1",
            "-frames:v", "1", str(source),
        ]
    )

    graded = tmp_path / "graded.png"
    # Same colon/drive-letter fix `subtitles.burn` proved for `ass=`: run
    # ffmpeg with cwd set to the LUT's own directory and pass a bare
    # filename, rather than an absolute Windows path colliding with the
    # filtergraph parser's use of ':' as an option separator.
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(source.resolve()),
            "-vf", f"lut3d=file={cube_path.name}",
            str(graded.resolve()),
        ],
        capture_output=True,
        cwd=cube_path.parent,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")[-1500:]
    assert graded.exists()

    from PIL import Image

    diff = _mean_abs_diff(Image.open(source), Image.open(graded))
    assert diff > 5.0, f"lut3d barely changed the image (mean abs diff {diff}); LUT may be an identity transform"

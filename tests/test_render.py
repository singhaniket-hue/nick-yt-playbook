"""Layer 1 footage assembly: framing filters, cut segments, full assembly.

Real ffmpeg, kept fast with short durations (0.4-0.6s) and small dimensions
(320x180) -- see the module docstring in `rabbithole/render.py` for why the
filter chain is shaped the way it is (crop's w/h don't re-evaluate per frame;
scale's do, but only with `eval=frame` set explicitly).
"""

from __future__ import annotations

import subprocess

import pytest

from rabbithole.edl import FRAMINGS, Cut
from rabbithole.provenance import AssetRecord
from rabbithole.render import assemble_footage, cut_segment, framing_filter, probe_duration
from rabbithole.slots import Slot

W, H, FPS = 320, 180, 30


# --- fixtures / helpers ------------------------------------------------------


def _run(args):
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-1500:])
    return result


def _probe_video(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-count_frames",
            "-show_entries", "stream=width,height,r_frame_rate,nb_read_frames",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    import json
    data = json.loads(result.stdout)
    return data["streams"][0], data["format"]


def _first_frame_rgb(path):
    raw = subprocess.run(
        ["ffmpeg", "-y", "-i", str(path), "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
        capture_output=True,
        check=True,
    ).stdout
    return raw[0], raw[1], raw[2]


def _two_color_asset(path, first="red", second="blue", half_seconds=1.0, width=W, height=H, fps=FPS):
    """A video whose first half is one solid colour and second half another."""
    _run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c={first}:s={width}x{height}:r={fps}:d={half_seconds}",
            "-f", "lavfi", "-i", f"color=c={second}:s={width}x{height}:r={fps}:d={half_seconds}",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ]
    )
    return path


def _solid_asset(path, color, seconds, width=W, height=H, fps=FPS):
    _run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}:r={fps}:d={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ]
    )
    return path


def _cut(index, start, end, slot_id, framing="wide", origin="script"):
    return Cut(
        index=index, start=start, end=end, slot_id=slot_id, origin=origin,
        framing=framing, transition="cut", reason="test",
    )


def _record(asset_id, path, slot_ids):
    return AssetRecord(
        asset_id=asset_id, tier="atmospheric", provider="ffmpeg-lavfi",
        original_url="", license="", retrieved_at="2026-01-01T00:00:00Z",
        local_path=str(path), used_in_slots=tuple(slot_ids),
    )


# --- framing_filter: exact dims and duration for every framing --------------


@pytest.mark.parametrize("framing", FRAMINGS)
@pytest.mark.parametrize("duration", [0.4, 0.6, 3.1])
def test_framing_renders_exact_dimensions_and_duration(tmp_path, framing, duration):
    out = tmp_path / f"{framing}-{duration}.mp4"
    vf = framing_filter(framing, W, H, FPS, duration)

    _run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=640x360:rate={FPS}:duration={duration + 1}",
            "-vf", vf,
            "-t", f"{duration:.6f}",
            "-r", str(FPS),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(out),
        ]
    )

    stream, fmt = _probe_video(out)
    assert int(stream["width"]) == W
    assert int(stream["height"]) == H
    assert stream["r_frame_rate"] == f"{FPS}/1"
    assert float(fmt["duration"]) == pytest.approx(duration, abs=0.05)
    assert int(stream["nb_read_frames"]) == round(duration * FPS)


def test_unknown_framing_raises_value_error():
    with pytest.raises(ValueError, match="fisheye"):
        framing_filter("fisheye", W, H, FPS, 1.0)


def test_nonpositive_duration_raises_value_error():
    with pytest.raises(ValueError):
        framing_filter("wide", W, H, FPS, 0.0)


# --- cut_segment: seeks to the correct asset-relative sub-range -------------


def test_cut_segment_takes_the_first_half_of_the_asset(tmp_path):
    asset = _two_color_asset(tmp_path / "asset.mp4", first="red", second="blue", half_seconds=1.0)
    slot = Slot(slot_id="s001", kind="plate", detail="grain", start=10.0, end=12.0, queries=(), marker_word_index=0)
    cut = _cut(0, start=10.0, end=10.4, slot_id="s001")

    out = tmp_path / "seg.mp4"
    result = cut_segment(cut, slot, asset, out, width=W, height=H, fps=FPS)

    assert result == out
    r, g, b = _first_frame_rgb(out)
    assert (r, g, b) == pytest.approx((254, 0, 0), abs=5)


def test_cut_segment_mid_slot_takes_the_second_half_of_the_asset(tmp_path):
    asset = _two_color_asset(tmp_path / "asset.mp4", first="red", second="blue", half_seconds=1.0)
    slot = Slot(slot_id="s001", kind="plate", detail="grain", start=10.0, end=12.0, queries=(), marker_word_index=0)
    # Asset-relative offset is 10.2s - 10.0s = 1.2s, past the 1.0s halfway point.
    cut = _cut(3, start=11.2, end=11.6, slot_id="s001")

    out = tmp_path / "seg.mp4"
    cut_segment(cut, slot, asset, out, width=W, height=H, fps=FPS)

    r, g, b = _first_frame_rgb(out)
    assert (r, g, b) == pytest.approx((0, 0, 254), abs=5)


def test_cut_segment_output_duration_matches_cut_duration(tmp_path):
    asset = _solid_asset(tmp_path / "asset.mp4", "gray", seconds=2.0)
    slot = Slot(slot_id="s001", kind="plate", detail="grain", start=0.0, end=2.0, queries=(), marker_word_index=0)
    cut = _cut(0, start=0.5, end=1.1, slot_id="s001")

    out = tmp_path / "seg.mp4"
    cut_segment(cut, slot, asset, out, width=W, height=H, fps=FPS)

    assert probe_duration(out) == pytest.approx(cut.duration, abs=0.05)


@pytest.mark.parametrize("framing", FRAMINGS)
def test_cut_segment_applies_requested_framing_at_exact_dimensions(tmp_path, framing):
    asset = _solid_asset(tmp_path / "asset.mp4", "gray", seconds=1.0)
    slot = Slot(slot_id="s001", kind="plate", detail="grain", start=0.0, end=1.0, queries=(), marker_word_index=0)
    cut = _cut(0, start=0.0, end=0.5, slot_id="s001", framing=framing)

    out = tmp_path / "seg.mp4"
    cut_segment(cut, slot, asset, out, width=W, height=H, fps=FPS)

    stream, fmt = _probe_video(out)
    assert int(stream["width"]) == W
    assert int(stream["height"]) == H
    assert float(fmt["duration"]) == pytest.approx(0.5, abs=0.05)


# --- assemble_footage: index order, missing-asset skip, total duration ------


def test_assemble_footage_total_duration_matches_sum_of_cut_durations(tmp_path):
    asset = _solid_asset(tmp_path / "asset.mp4", "gray", seconds=2.0)
    slot = Slot(slot_id="s001", kind="plate", detail="grain", start=0.0, end=2.0, queries=(), marker_word_index=0)
    cuts = [
        _cut(0, 0.0, 0.4, "s001", framing="wide"),
        _cut(1, 0.4, 0.9, "s001", framing="push-in"),
        _cut(2, 0.9, 1.3, "s001", framing="detail"),
    ]
    records = [_record("a1", asset, ["s001"])]

    out_path, findings = assemble_footage(
        cuts, [slot], records, tmp_path / "footage.mp4", tmp_path / "work",
        width=W, height=H, fps=FPS,
    )

    assert findings == []
    assert out_path.exists()
    expected = sum(c.duration for c in cuts)
    assert probe_duration(out_path) == pytest.approx(expected, abs=0.1)


def test_assemble_footage_skips_slot_with_no_record_but_renders_the_rest(tmp_path):
    asset = _solid_asset(tmp_path / "asset.mp4", "gray", seconds=1.0)
    slot_ok = Slot(slot_id="s001", kind="plate", detail="grain", start=0.0, end=1.0, queries=(), marker_word_index=0)
    slot_missing = Slot(slot_id="s002", kind="plate", detail="grain", start=1.0, end=2.0, queries=(), marker_word_index=0)
    cuts = [
        _cut(0, 0.0, 0.4, "s001"),
        _cut(1, 1.0, 1.4, "s002"),  # no provenance record for s002
    ]
    records = [_record("a1", asset, ["s001"])]

    out_path, findings = assemble_footage(
        cuts, [slot_ok, slot_missing], records, tmp_path / "footage.mp4", tmp_path / "work",
        width=W, height=H, fps=FPS,
    )

    assert len(findings) == 1
    assert findings[0].gate == "render"
    assert findings[0].severity == "error"
    assert "s002" in findings[0].message
    assert out_path.exists()
    # Only the s001 cut made it in.
    assert probe_duration(out_path) == pytest.approx(0.4, abs=0.1)


def test_assemble_footage_every_slot_missing_still_reports_and_writes_nothing(tmp_path):
    slot = Slot(slot_id="s001", kind="plate", detail="grain", start=0.0, end=1.0, queries=(), marker_word_index=0)
    cuts = [_cut(0, 0.0, 0.4, "s001")]

    out_path, findings = assemble_footage(
        cuts, [slot], [], tmp_path / "footage.mp4", tmp_path / "work",
        width=W, height=H, fps=FPS,
    )

    assert any(f.severity == "error" for f in findings)
    assert not out_path.exists()


def test_assemble_footage_orders_by_cut_index_not_input_order(tmp_path):
    asset = _two_color_asset(tmp_path / "asset.mp4", first="red", second="blue", half_seconds=0.5)
    slot = Slot(slot_id="s001", kind="plate", detail="grain", start=0.0, end=1.0, queries=(), marker_word_index=0)
    # Deliberately pass cuts out of index order: index 1 (blue half) first,
    # index 0 (red half) second. The assembled file must still play red-then-blue.
    cuts = [
        _cut(1, 0.5, 0.9, "s001", framing="wide"),
        _cut(0, 0.0, 0.4, "s001", framing="wide"),
    ]
    records = [_record("a1", asset, ["s001"])]

    out_path, findings = assemble_footage(
        cuts, [slot], records, tmp_path / "footage.mp4", tmp_path / "work",
        width=W, height=H, fps=FPS,
    )

    assert findings == []
    r, g, b = _first_frame_rgb(out_path)
    assert (r, g, b) == pytest.approx((254, 0, 0), abs=5), "first frame of the assembled output must be the red half (cut index 0), not blue (index 1)"


def test_assemble_footage_names_segments_by_cut_index(tmp_path):
    asset = _solid_asset(tmp_path / "asset.mp4", "gray", seconds=1.0)
    slot = Slot(slot_id="s001", kind="plate", detail="grain", start=0.0, end=1.0, queries=(), marker_word_index=0)
    cuts = [_cut(5, 0.0, 0.4, "s001")]
    records = [_record("a1", asset, ["s001"])]

    work_dir = tmp_path / "work"
    assemble_footage(cuts, [slot], records, tmp_path / "footage.mp4", work_dir, width=W, height=H, fps=FPS)

    assert (work_dir / "cut-0005.mp4").exists()


def test_assemble_footage_continues_when_a_cut_fails_to_render(tmp_path):
    # A cut whose asset path is simply broken (unreadable / does not exist)
    # must not take the whole assembly down with it: an earlier cut's
    # already-rendered segment stays on disk, the failure is reported as a
    # Finding, and the function returns rather than raising uncaught.
    asset = _solid_asset(tmp_path / "asset.mp4", "gray", seconds=1.0)
    slot_ok = Slot(slot_id="s001", kind="plate", detail="grain", start=0.0, end=1.0, queries=(), marker_word_index=0)
    slot_broken = Slot(slot_id="s002", kind="plate", detail="grain", start=1.0, end=1.4, queries=(), marker_word_index=0)
    cuts = [
        _cut(0, 0.0, 0.4, "s001"),
        _cut(1, 1.0, 1.4, "s002"),
    ]
    records = [
        _record("a1", asset, ["s001"]),
        _record("a2", tmp_path / "does-not-exist.mp4", ["s002"]),
    ]

    out_path, findings = assemble_footage(
        cuts, [slot_ok, slot_broken], records, tmp_path / "footage.mp4", tmp_path / "work",
        width=W, height=H, fps=FPS,
    )

    assert any(f.severity == "error" and "Cut 1" in f.message for f in findings)
    # The earlier, successfully rendered segment is left on disk, not cleaned up.
    assert (tmp_path / "work" / "cut-0000.mp4").exists()
    # The function returned normally with the good cut included, rather than
    # raising an uncaught exception out to the caller.
    assert out_path.exists()
    assert probe_duration(out_path) == pytest.approx(0.4, abs=0.1)


def test_assemble_footage_freezes_last_frame_when_asset_shorter_than_slot(tmp_path):
    asset = _solid_asset(tmp_path / "asset.mp4", "green", seconds=1.0)
    slot = Slot(slot_id="s001", kind="plate", detail="grain", start=0.0, end=2.0, queries=(), marker_word_index=0)
    # Cut wants 0.8s starting 0.7s into the slot -> asset only has 0.3s left.
    cuts = [_cut(0, 0.7, 1.5, "s001")]
    records = [_record("a1", asset, ["s001"])]

    out_path, findings = assemble_footage(
        cuts, [slot], records, tmp_path / "footage.mp4", tmp_path / "work",
        width=W, height=H, fps=FPS,
    )

    assert findings == []
    assert out_path.exists()
    stream, fmt = _probe_video(out_path)
    assert int(stream["nb_read_frames"]) == round(cuts[0].duration * FPS)
    assert float(fmt["duration"]) == pytest.approx(0.8, abs=0.01)


def test_assemble_footage_pads_late_multi_cut_slot_without_timeline_drift(tmp_path):
    # The source only covers the first 0.5s of a 2.0s slot.  All three cuts
    # begin after EOF, which used to produce empty/truncated segments and pull
    # every subsequent edit early.
    asset = _two_color_asset(
        tmp_path / "asset.mp4",
        first="red",
        second="blue",
        half_seconds=0.25,
    )
    slot = Slot(
        slot_id="s001",
        kind="plate",
        detail="grain",
        start=10.0,
        end=12.0,
        queries=(),
        marker_word_index=0,
    )
    cuts = [
        _cut(0, 10.80, 11.14, "s001", framing="wide"),
        _cut(1, 11.14, 11.48, "s001", framing="push-in"),
        _cut(2, 11.48, 11.82, "s001", framing="detail"),
    ]
    records = [_record("a1", asset, ["s001"])]

    out_path, findings = assemble_footage(
        cuts,
        [slot],
        records,
        tmp_path / "footage.mp4",
        tmp_path / "work",
        width=W,
        height=H,
        fps=FPS,
    )

    assert findings == []
    stream, fmt = _probe_video(out_path)
    expected_frames = round(cuts[-1].end * FPS) - round(cuts[0].start * FPS)
    assert int(stream["nb_read_frames"]) == expected_frames
    assert float(fmt["duration"]) == pytest.approx(expected_frames / FPS, abs=0.01)
    # All cuts are a freeze of the source's last (blue) frame, including the
    # final cut whose asset-relative offset is 0.98s past source EOF.
    r, g, b = _first_frame_rgb(tmp_path / "work" / "cut-0002.mp4")
    assert (r, g, b) == pytest.approx((0, 0, 254), abs=5)

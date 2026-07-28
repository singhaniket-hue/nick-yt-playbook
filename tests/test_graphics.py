"""Part 1: chapter cards and censor boxes, composited via ffmpeg.

Structural tests (font selection, ASS text, filter-string shape) run without
ffmpeg. The `draw_graphics` tests use real ffmpeg with small dimensions and
short durations, same shape as tests/test_subtitles_burn.py.
"""

from __future__ import annotations

import json
import subprocess

from rabbithole.graphics import (
    CENSOR_REGION_HEIGHT_FRACTION,
    CENSOR_REGION_WIDTH_FRACTION,
    censor_box_filter,
    censor_region_findings,
    chapter_card_ass,
    draw_graphics,
    pick_title_font,
)
from rabbithole.overlays import Overlay
from rabbithole.render import probe_duration

W, H, FPS = 320, 180, 30

TYPOGRAPHY = {
    "title_card": {"family": "Bebas Neue", "weight": 400, "transform": "uppercase", "tracking": 0.04},
    "subtitle": {
        "family": "Inter",
        "weight": 600,
        "placement": "lower-third-center",
        "fill": "#E0E0E0",
        "keyword_fill": "#FFFF00",
        "shadow": "0 2px 6px rgba(0,0,0,0.9)",
    },
}

PALETTE = {
    "backdrop": ["#000000", "#202020"],
    "accent_red": "#E02020",
    "warning_yellow": "#FFFF00",
    "warm_grey": "#C0A080",
    "text_light": "#E0E0E0",
}


def _chapter_overlay(start, end, text="Dead Air", detail="1"):
    return Overlay(kind="chapter-card", start=start, end=end, text=text, detail=detail)


def _censor_overlay(start, end, detail="face"):
    return Overlay(kind="censor", start=start, end=end, text="", detail=detail)


def _keyword_overlay(start, end, text="ghost"):
    return Overlay(kind="keyword", start=start, end=end, text=text, detail="")


# --- pick_title_font ----------------------------------------------------------


def test_pick_title_font_returns_available_font_and_warns_when_unavailable():
    # Bebas Neue is not installed on this machine (confirmed via
    # _installed_font_families empirically) -- must fall back to a
    # condensed/sans candidate and warn about the override.
    font, findings = pick_title_font(TYPOGRAPHY, available={"Arial Black", "Impact"})

    assert font in ("Arial Black", "Impact")
    assert any(f.severity == "warning" for f in findings)
    assert any("Bebas Neue" in f.message and font in f.message for f in findings)


def test_pick_title_font_no_warning_when_requested_family_already_available():
    typography = {"title_card": {"family": "Bahnschrift"}}

    font, findings = pick_title_font(typography, available={"Bahnschrift"})

    assert font == "Bahnschrift"
    assert findings == []


def test_pick_title_font_falls_back_to_sans_serif_when_nothing_available():
    font, findings = pick_title_font(TYPOGRAPHY, available=set())

    assert font == "sans-serif"
    assert any(f.severity == "error" for f in findings)


def test_pick_title_font_real_detection_finds_something_installed():
    # Real system detection, not mocked -- this machine has no Bebas Neue,
    # Anton, or Oswald installed, but does have Windows system fonts like
    # Arial Black / Impact / Bahnschrift, so a real candidate should be found.
    font, findings = pick_title_font(TYPOGRAPHY)

    assert font != "sans-serif"
    assert any(f.severity == "warning" for f in findings)


# --- chapter_card_ass -----------------------------------------------------------


def test_chapter_card_ass_one_dialogue_per_chapter_card_overlay():
    overlays = [
        _chapter_overlay(0.0, 3.0, text="Dead Air"),
        _censor_overlay(5.0, 6.0),
        _keyword_overlay(7.0, 7.5),
        _chapter_overlay(10.0, 13.0, text="The Second Chapter"),
    ]

    ass = chapter_card_ass(overlays, TYPOGRAPHY, PALETTE, width=W, height=H)

    dialogue_lines = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogue_lines) == 2


def test_chapter_card_ass_empty_for_no_chapter_card_overlays():
    overlays = [_censor_overlay(1.0, 2.0), _keyword_overlay(3.0, 3.5)]

    ass = chapter_card_ass(overlays, TYPOGRAPHY, PALETTE, width=W, height=H)

    dialogue_lines = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert dialogue_lines == []
    assert "[Script Info]" in ass


def test_chapter_card_ass_uppercases_text_when_transform_says_so():
    overlays = [_chapter_overlay(0.0, 3.0, text="Dead Air")]

    ass = chapter_card_ass(overlays, TYPOGRAPHY, PALETTE, width=W, height=H)

    assert "DEAD AIR" in ass
    assert "Dead Air" not in ass


def test_chapter_card_ass_preserves_case_when_transform_is_not_uppercase():
    typography = {
        "title_card": {"family": "Bebas Neue", "weight": 400, "transform": "none", "tracking": 0.0}
    }
    overlays = [_chapter_overlay(0.0, 3.0, text="Dead Air")]

    ass = chapter_card_ass(overlays, typography, PALETTE, width=W, height=H)

    assert "Dead Air" in ass


def test_chapter_card_ass_timing_matches_overlay():
    from rabbithole.subtitles import format_ass_timestamp

    overlays = [_chapter_overlay(11.995, 14.995, text="Dead Air")]

    ass = chapter_card_ass(overlays, TYPOGRAPHY, PALETTE, width=W, height=H)

    start_ts = format_ass_timestamp(11.995)
    end_ts = format_ass_timestamp(14.995)
    dialogue = next(line for line in ass.splitlines() if line.startswith("Dialogue:"))
    assert f",{start_ts},{end_ts}," in dialogue


def test_chapter_card_ass_uses_middle_center_alignment_distinct_from_subtitles():
    # Subtitles use ASS alignment 2 (bottom-center); chapter cards must use a
    # different alignment so the two never collide on screen even if their
    # time windows overlap.
    overlays = [_chapter_overlay(0.0, 3.0)]

    ass = chapter_card_ass(overlays, TYPOGRAPHY, PALETTE, width=W, height=H)

    styles_line = next(line for line in ass.splitlines() if line.startswith("Style: TitleCard,"))
    fields = styles_line.split(",")
    alignment_index = fields[0].split(": ")[0]  # unused, just documents intent
    assert "5" in fields  # numpad alignment 5 = middle-center


# --- censor_box_filter -----------------------------------------------------------


def test_censor_box_filter_empty_for_no_censor_overlays():
    overlays = [_chapter_overlay(0.0, 3.0), _keyword_overlay(1.0, 1.5)]

    assert censor_box_filter(overlays, PALETTE, width=W, height=H) == ""


def test_censor_box_filter_produces_a_chain_for_one_censor_overlay():
    overlays = [_censor_overlay(1.0, 2.0)]

    chain = censor_box_filter(overlays, PALETTE, width=W, height=H)

    assert chain != ""
    assert "split" in chain
    assert "boxblur" in chain
    assert "overlay" in chain
    assert "between(t" in chain


def test_censor_box_filter_chains_multiple_censor_overlays():
    overlays = [_censor_overlay(1.0, 2.0), _censor_overlay(3.0, 4.0)]

    chain = censor_box_filter(overlays, PALETTE, width=W, height=H)

    assert chain.count("boxblur") == 2
    assert chain.count("overlay") == 2


# --- censor_region_findings -------------------------------------------------------


def test_censor_region_findings_warns_once_per_censor_overlay():
    overlays = [_censor_overlay(1.0, 2.0, detail="face"), _censor_overlay(5.0, 6.0, detail="license plate")]

    findings = censor_region_findings(overlays)

    assert len(findings) == 2
    assert all(f.severity == "warning" for f in findings)
    assert any("face" in f.message for f in findings)
    assert any("license plate" in f.message for f in findings)
    assert all("default" in f.message for f in findings)


def test_censor_region_findings_empty_for_no_censor_overlays():
    overlays = [_chapter_overlay(0.0, 3.0)]

    assert censor_region_findings(overlays) == []


# --- draw_graphics: real ffmpeg ---------------------------------------------------


def _run(args):
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-1500:])
    return result


def _video_with_audio(path, seconds=2.0, width=W, height=H, fps=FPS, color="gray"):
    _run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}:rate={fps}:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}:sample_rate=44100",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest",
            str(path),
        ]
    )
    return path


def _probe(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def _frame(video_path, at_seconds, out_path):
    _run(["ffmpeg", "-y", "-ss", str(at_seconds), "-i", str(video_path), "-frames:v", "1", str(out_path)])
    return out_path


def _centre_region(im):
    from PIL import Image  # noqa: F401

    w, h = im.size
    return im.crop((int(w * 0.25), int(h * 0.25), int(w * 0.75), int(h * 0.75)))


def _mean_abs_diff(im_a, im_b):
    from PIL import ImageChops
    import numpy as np

    diff = ImageChops.difference(im_a.convert("RGB"), im_b.convert("RGB"))
    return float(np.array(diff).mean())


def test_draw_graphics_chapter_card_changes_centre_pixels_and_leaves_uncarded_second_unchanged(tmp_path):
    """The load-bearing test: a filter that renders but draws nothing looks
    like success. Prove pixels actually change during the card and don't
    change outside it."""
    video = _video_with_audio(tmp_path / "video.mp4", seconds=2.0)
    overlays = [_chapter_overlay(0.0, 0.9, text="Dead Air")]

    out_path, findings = draw_graphics(
        video, overlays, TYPOGRAPHY, PALETTE, tmp_path / "carded.mp4", tmp_path / "work"
    )

    from PIL import Image

    before_carded = Image.open(_frame(video, 0.4, tmp_path / "before-carded.png"))
    after_carded = Image.open(_frame(out_path, 0.4, tmp_path / "after-carded.png"))
    diff_during_card = _mean_abs_diff(_centre_region(before_carded), _centre_region(after_carded))
    assert diff_during_card > 1.0, (
        f"centre region barely changed during the chapter card (mean abs diff "
        f"{diff_during_card}); draw_graphics may be a no-op"
    )

    before_uncarded = Image.open(_frame(video, 1.5, tmp_path / "before-uncarded.png"))
    after_uncarded = Image.open(_frame(out_path, 1.5, tmp_path / "after-uncarded.png"))
    diff_outside_card = _mean_abs_diff(before_uncarded, after_uncarded)
    assert diff_outside_card < 1.0, (
        f"the un-carded second changed unexpectedly (mean abs diff {diff_outside_card})"
    )


def test_draw_graphics_censor_box_changes_pixels_in_its_region_and_window(tmp_path):
    video = _video_with_audio(tmp_path / "video.mp4", seconds=2.0, color="gray")
    overlays = [_censor_overlay(0.0, 0.9, detail="face")]

    out_path, findings = draw_graphics(
        video, overlays, TYPOGRAPHY, PALETTE, tmp_path / "censored.mp4", tmp_path / "work"
    )

    from PIL import Image

    cw = round(W * CENSOR_REGION_WIDTH_FRACTION)
    ch = round(H * CENSOR_REGION_HEIGHT_FRACTION)
    cx = round((W - cw) / 2)
    cy = round(H * 0.12)
    box = (cx, cy, cx + cw, cy + ch)

    before = Image.open(_frame(video, 0.4, tmp_path / "before-censor.png"))
    after = Image.open(_frame(out_path, 0.4, tmp_path / "after-censor.png"))
    diff_in_box = _mean_abs_diff(before.crop(box), after.crop(box))
    assert diff_in_box > 1.0, f"censor region barely changed (mean abs diff {diff_in_box})"

    # Outside the censor's time window the region should be untouched.
    before_after_window = Image.open(_frame(video, 1.5, tmp_path / "before-after-window.png"))
    after_after_window = Image.open(_frame(out_path, 1.5, tmp_path / "after-after-window.png"))
    diff_after_window = _mean_abs_diff(before_after_window.crop(box), after_after_window.crop(box))
    assert diff_after_window < 1.0, (
        f"censor region changed outside its time window (mean abs diff {diff_after_window})"
    )


def test_draw_graphics_finding_reports_defaulted_censor_region(tmp_path):
    video = _video_with_audio(tmp_path / "video.mp4", seconds=1.0)
    overlays = [_censor_overlay(0.0, 0.5, detail="face")]

    out_path, findings = draw_graphics(
        video, overlays, TYPOGRAPHY, PALETTE, tmp_path / "censored.mp4", tmp_path / "work"
    )

    assert any(f.severity == "warning" and "face" in f.message for f in findings)


def test_draw_graphics_audio_survives_with_same_codec(tmp_path):
    video = _video_with_audio(tmp_path / "video.mp4", seconds=1.0)
    overlays = [_chapter_overlay(0.0, 0.5)]

    out_path, findings = draw_graphics(
        video, overlays, TYPOGRAPHY, PALETTE, tmp_path / "carded.mp4", tmp_path / "work"
    )

    input_data = _probe(video)
    output_data = _probe(out_path)
    input_audio = next(s for s in input_data["streams"] if s["codec_type"] == "audio")
    output_audio = next(s for s in output_data["streams"] if s["codec_type"] == "audio")
    assert output_audio["codec_name"] == input_audio["codec_name"] == "aac"


def test_draw_graphics_output_duration_and_dimensions_match_input(tmp_path):
    video = _video_with_audio(tmp_path / "video.mp4", seconds=1.4, width=400, height=222)
    overlays = [_chapter_overlay(0.0, 0.5)]

    out_path, findings = draw_graphics(
        video, overlays, TYPOGRAPHY, PALETTE, tmp_path / "carded.mp4", tmp_path / "work"
    )

    assert probe_duration(out_path) == probe_duration(video)
    data = _probe(out_path)
    vstream = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert vstream["width"] == 400
    assert vstream["height"] == 222


def test_draw_graphics_no_overlays_passes_through_unchanged_in_duration(tmp_path):
    video = _video_with_audio(tmp_path / "video.mp4", seconds=1.4)

    out_path, findings = draw_graphics(video, [], TYPOGRAPHY, PALETTE, tmp_path / "out.mp4", tmp_path / "work")

    assert probe_duration(out_path) == probe_duration(video)
    assert findings == []


def test_draw_graphics_no_overlays_of_relevant_kind_passes_through(tmp_path):
    # A keyword overlay alone (no chapter-card, no censor) should skip both
    # stages cleanly rather than erroring.
    video = _video_with_audio(tmp_path / "video.mp4", seconds=1.0)
    overlays = [_keyword_overlay(0.1, 0.4)]

    out_path, findings = draw_graphics(video, overlays, TYPOGRAPHY, PALETTE, tmp_path / "out.mp4", tmp_path / "work")

    assert out_path.exists()
    assert probe_duration(out_path) == probe_duration(video)

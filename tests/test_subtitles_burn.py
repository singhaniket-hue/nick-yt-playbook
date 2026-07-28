"""Part 3: burn-in.

Real ffmpeg, small dimensions and short durations to keep runtime sane, same
shape as tests/test_render_finish.py.
"""

from __future__ import annotations

import json
import subprocess

from rabbithole.render import probe_duration
from rabbithole.subtitles import SubtitleCue, burn, write_ass

W, H, FPS = 320, 180, 30

TYPOGRAPHY = {
    "subtitle": {
        "family": "Inter",
        "weight": 600,
        "placement": "lower-third-center",
        "fill": "#E0E0E0",
        "keyword_fill": "#FFFF00",
        "shadow": "0 2px 6px rgba(0,0,0,0.9)",
    }
}


def _run(args):
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-1500:])
    return result


def _video_with_audio(path, seconds=1.0, width=W, height=H, fps=FPS, color="gray"):
    """A short video with both a solid-colour video stream and an AAC audio stream."""
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


def _one_cue():
    return [SubtitleCue(index=0, start=0.0, end=5.0, words=("test", "subtitle"), keyword_positions=())]


def _write_test_ass(path):
    out_path, _findings = write_ass(_one_cue(), TYPOGRAPHY, path, width=W, height=H)
    return out_path


def _lower_third_region(im):
    """Crop the bottom third of a PIL image -- where subtitles are placed."""
    w, h = im.size
    return im.crop((0, int(h * 2 / 3), w, h))


def _mean_abs_diff(im_a, im_b):
    from PIL import ImageChops
    import numpy as np

    diff = ImageChops.difference(im_a.convert("RGB"), im_b.convert("RGB"))
    return float(np.array(diff).mean())


# --- burn: duration and dimensions --------------------------------------------


def test_burn_output_duration_matches_input(tmp_path):
    video = _video_with_audio(tmp_path / "video.mp4", seconds=1.4)
    ass_path = _write_test_ass(tmp_path / "subs.ass")

    out_path = burn(video, ass_path, tmp_path / "burned.mp4")

    assert probe_duration(out_path) == probe_duration(video)


def test_burn_output_dimensions_match_input(tmp_path):
    video = _video_with_audio(tmp_path / "video.mp4", width=400, height=222)
    ass_path = _write_test_ass(tmp_path / "subs.ass")

    out_path = burn(video, ass_path, tmp_path / "burned.mp4")

    data = _probe(out_path)
    vstream = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert vstream["width"] == 400
    assert vstream["height"] == 222


# --- burn: audio survives untouched -------------------------------------------


def test_burn_preserves_audio_stream_codec_via_copy(tmp_path):
    video = _video_with_audio(tmp_path / "video.mp4")
    ass_path = _write_test_ass(tmp_path / "subs.ass")

    out_path = burn(video, ass_path, tmp_path / "burned.mp4")

    input_data = _probe(video)
    output_data = _probe(out_path)
    input_audio = next(s for s in input_data["streams"] if s["codec_type"] == "audio")
    output_audio = next(s for s in output_data["streams"] if s["codec_type"] == "audio")

    # -c:a copy: the codec must be untouched, not re-encoded to some other
    # default (e.g. via a lossy re-encode ffmpeg would otherwise apply).
    assert output_audio["codec_name"] == input_audio["codec_name"] == "aac"


# --- burn: subtitles actually change pixels ------------------------------------


def test_burning_a_cue_changes_pixels_in_the_lower_third(tmp_path):
    """The test that catches a filter silently doing nothing."""
    video = _video_with_audio(tmp_path / "video.mp4", seconds=2.0, color="gray")
    ass_path = _write_test_ass(tmp_path / "subs.ass")

    out_path = burn(video, ass_path, tmp_path / "burned.mp4")

    frame_before = tmp_path / "before.png"
    frame_after = tmp_path / "after.png"
    _run(["ffmpeg", "-y", "-ss", "0.5", "-i", str(video), "-frames:v", "1", str(frame_before)])
    _run(["ffmpeg", "-y", "-ss", "0.5", "-i", str(out_path), "-frames:v", "1", str(frame_after)])

    from PIL import Image

    im_before = Image.open(frame_before)
    im_after = Image.open(frame_after)

    region_before = _lower_third_region(im_before)
    region_after = _lower_third_region(im_after)

    diff = _mean_abs_diff(region_before, region_after)
    assert diff > 1.0, f"lower-third region barely changed (mean abs diff {diff}); burn may be a no-op"


def test_burning_a_cue_does_not_meaningfully_change_pixels_outside_the_lower_third(tmp_path):
    """The subtitle is placed in the lower third -- the rest of the frame should
    stay effectively untouched (grade/framing are separate layers, not this one)."""
    video = _video_with_audio(tmp_path / "video.mp4", seconds=2.0, color="gray")
    ass_path = _write_test_ass(tmp_path / "subs.ass")

    out_path = burn(video, ass_path, tmp_path / "burned.mp4")

    frame_before = tmp_path / "before.png"
    frame_after = tmp_path / "after.png"
    _run(["ffmpeg", "-y", "-ss", "0.5", "-i", str(video), "-frames:v", "1", str(frame_before)])
    _run(["ffmpeg", "-y", "-ss", "0.5", "-i", str(out_path), "-frames:v", "1", str(frame_after)])

    from PIL import Image

    im_before = Image.open(frame_before)
    im_after = Image.open(frame_after)

    w, h = im_before.size
    top_two_thirds_before = im_before.crop((0, 0, w, int(h * 2 / 3)))
    top_two_thirds_after = im_after.crop((0, 0, w, int(h * 2 / 3)))

    diff = _mean_abs_diff(top_two_thirds_before, top_two_thirds_after)
    assert diff < 1.0, f"top two-thirds changed unexpectedly (mean abs diff {diff})"


# --- burn: the ':' filter-path problem ------------------------------------------


def test_burn_works_with_a_path_containing_spaces_and_a_drive_letter_colon(tmp_path):
    spaced_dir = tmp_path / "a dir with spaces"
    spaced_dir.mkdir()
    video = _video_with_audio(spaced_dir / "my video.mp4")
    ass_path = _write_test_ass(spaced_dir / "my subs.ass")
    out_path_target = spaced_dir / "burned output.mp4"

    # tmp_path is already an absolute Windows path containing a drive letter
    # colon (e.g. C:\Users\...\pytest-.../test_...0); combined with the
    # space in "a dir with spaces" this exercises exactly the failure mode
    # that made the `ass=` filter's colon collide with a Windows drive
    # letter in earlier work on this project.
    assert ":" in str(spaced_dir)

    out_path = burn(video, ass_path, out_path_target)

    assert out_path.exists()
    assert out_path.stat().st_size > 0
    assert probe_duration(out_path) > 0

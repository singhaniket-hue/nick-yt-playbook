"""Part 3: burn-in.

Real ffmpeg, small dimensions and short durations to keep runtime sane, same
shape as tests/test_render_finish.py.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from rabbithole.render import probe_duration
import rabbithole.subtitles as subtitles
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


def test_burn_works_with_a_spaced_absolute_path_on_each_host(tmp_path):
    spaced_dir = tmp_path / "a dir with spaces"
    spaced_dir.mkdir()
    video = _video_with_audio(spaced_dir / "my video.mp4")
    ass_path = _write_test_ass(spaced_dir / "my subs.ass")
    out_path_target = spaced_dir / "burned output.mp4"

    # On Windows the absolute tmp_path also contains a drive-letter colon,
    # exercising the filtergraph collision that motivated the cwd/bare-name
    # approach. macOS has no drive letter, but the same test still covers its
    # absolute path and whitespace.
    assert spaced_dir.is_absolute()
    if os.name == "nt":
        assert ":" in str(spaced_dir)

    out_path = burn(video, ass_path, out_path_target)

    assert out_path.exists()
    assert out_path.stat().st_size > 0
    assert probe_duration(out_path) > 0


def test_burn_names_the_ass_filename_option_explicitly(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    ass_path = tmp_path / "subs.ass"
    output = tmp_path / "burned.mp4"
    video.write_bytes(b"mock video")
    ass_path.write_text("[Script Info]\n", encoding="utf-8")
    observed = {}

    def fake_require_filter(name):
        observed["filter"] = name
        return "/mock/bin/ffmpeg-with-libass"

    monkeypatch.setattr(subtitles, "require_filter", fake_require_filter)

    def fake_run(args, cwd=None):
        observed["args"] = args
        observed["cwd"] = cwd

    monkeypatch.setattr(subtitles, "_run", fake_run)

    assert burn(video, ass_path, output) == output.resolve()
    vf = observed["args"][observed["args"].index("-vf") + 1]
    assert vf == "ass=filename=subs.ass"
    assert observed["args"][0] == "/mock/bin/ffmpeg-with-libass"
    assert observed["filter"] == "ass"
    assert observed["cwd"] == tmp_path.resolve()


def test_burn_exact_frame_contract_trims_then_clones_the_terminal_frame(
    tmp_path, monkeypatch
):
    video = tmp_path / "card-background.mp4"
    ass_path = tmp_path / "card.ass"
    output = tmp_path / "card.mp4"
    video.write_bytes(b"mock video")
    ass_path.write_text("[Script Info]\n", encoding="utf-8")
    observed = {}

    monkeypatch.setattr(subtitles, "require_filter", lambda _name: "ffmpeg")

    def fake_run(args, cwd=None):
        observed["args"] = args
        observed["cwd"] = cwd

    monkeypatch.setattr(subtitles, "_run", fake_run)

    assert burn(
        video,
        ass_path,
        output,
        authored_frame_count=88,
        safe_trailing_frames=1,
        fps=30,
    ) == output.resolve()

    args = observed["args"]
    assert args[args.index("-vf") + 1] == (
        "ass=filename=card.ass,trim=end_frame=88,"
        "tpad=stop_mode=clone:stop=1"
    )
    assert args[args.index("-r") + 1] == "30"
    assert args[args.index("-fps_mode") + 1] == "cfr"
    assert args[args.index("-frames:v") + 1] == "89"
    assert args[args.index("-pix_fmt") + 1] == "yuv420p"
    assert observed["cwd"] == tmp_path.resolve()


def test_burn_checks_for_libass_before_starting_encode(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    ass_path = tmp_path / "subs.ass"
    output = tmp_path / "burned.mp4"

    def missing_filter(_name):
        raise RuntimeError("Install an FFmpeg build compiled with libass")

    monkeypatch.setattr(subtitles, "require_filter", missing_filter)
    monkeypatch.setattr(
        subtitles,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("encode must not start without libass"),
    )

    with pytest.raises(RuntimeError, match="compiled with libass"):
        burn(video, ass_path, output)

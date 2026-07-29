from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from rabbithole.sources.frame_video import derive_source_frame_video


def _source_probe(*, width=640, height=360, duration=5.0) -> bytes:
    return json.dumps(
        {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": width,
                    "height": height,
                    "duration": str(duration),
                },
                {"index": 1, "codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"duration": str(duration)},
        }
    ).encode()


def _output_probe(
    *,
    frames=75,
    fps=30,
    codec="h264",
    width=1920,
    height=1080,
    pix_fmt="yuv420p",
    with_audio=False,
) -> bytes:
    streams = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": codec,
            "width": width,
            "height": height,
            "pix_fmt": pix_fmt,
            "r_frame_rate": f"{fps}/1",
            "avg_frame_rate": f"{fps}/1",
            "nb_read_frames": str(frames),
            "duration": str(frames / fps),
        }
    ]
    if with_audio:
        streams.append({"index": 1, "codec_type": "audio", "codec_name": "aac"})
    return json.dumps(
        {"streams": streams, "format": {"duration": str(frames / fps)}}
    ).encode()


class _SuccessfulRunner:
    def __init__(self, *, output_probe: bytes | None = None):
        self.calls: list[list[str]] = []
        self.output_probe = output_probe or _output_probe()

    def __call__(self, argv: list[str]):
        self.calls.append(argv)
        if argv[0] == "ffprobe":
            path = Path(argv[-1])
            if path.name == "source.mp4":
                return 0, _source_probe(), b""
            return 0, self.output_probe, b""

        output = Path(argv[-1])
        if output.suffix == ".png":
            Image.new("RGB", (1920, 1080), "#285078").save(output)
        else:
            output.write_bytes(b"fake mp4")
        return 0, b"", b""


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"retained source video")
    return source


def test_derives_frame_accurate_cropped_silent_h264_video(tmp_path):
    source = _source(tmp_path)
    output = tmp_path / "slot.mp4"
    runner = _SuccessfulRunner()

    result = derive_source_frame_video(
        source,
        output,
        timestamp=1.25,
        duration=2.5,
        crop=(40, 20, 320, 180),
        encoder="libx264",
        runner=runner,
    )

    assert result == output.resolve()
    assert output.read_bytes() == b"fake mp4"
    ffmpeg_calls = [call for call in runner.calls if call[0] == "ffmpeg"]
    assert len(ffmpeg_calls) == 2

    extract, encode = ffmpeg_calls
    assert extract.index("-ss") > extract.index("-i")
    assert extract[extract.index("-ss") + 1] == "1.250000"
    filters = extract[extract.index("-vf") + 1]
    assert "crop=320:180:40:20" in filters
    assert "scale=1920:1080" in filters
    assert "pad=1920:1080" in filters

    assert "-an" in encode
    assert encode[encode.index("-frames:v") + 1] == "75"
    assert encode[encode.index("-c:v") + 1] == "libx264"
    assert encode[encode.index("-pix_fmt") + 1] == "yuv420p"
    assert "+faststart" in encode

    output_probe = [
        call
        for call in runner.calls
        if call[0] == "ffprobe" and Path(call[-1]).name == "derived.mp4"
    ]
    assert len(output_probe) == 1
    assert "-count_frames" in output_probe[0]


def test_off_grid_duration_rounds_up_so_asset_never_ends_before_slot(tmp_path):
    source = _source(tmp_path)
    runner = _SuccessfulRunner(output_probe=_output_probe(frames=4))

    derive_source_frame_video(
        source,
        tmp_path / "slot.mp4",
        timestamp=0,
        duration=0.101,
        encoder="libx264",
        runner=runner,
    )

    encode = [call for call in runner.calls if call[0] == "ffmpeg"][1]
    assert encode[encode.index("-frames:v") + 1] == "4"


def test_optional_attribution_and_date_are_burned_into_the_retained_frame(tmp_path):
    source = _source(tmp_path)
    runner = _SuccessfulRunner()

    derive_source_frame_video(
        source,
        tmp_path / "slot.mp4",
        timestamp=0.5,
        duration=2.5,
        attribution="SOURCE: WEBDRIVER TORSO",
        date_label="Uploaded 2013-09-27",
        encoder="libx264",
        runner=runner,
    )

    encode = [call for call in runner.calls if call[0] == "ffmpeg"][1]
    encoded_frame = Path(encode[encode.index("-i") + 1])
    assert encoded_frame.name == "labelled-frame.png"


def test_crop_outside_encoded_source_fails_before_ffmpeg(tmp_path):
    source = _source(tmp_path)
    output = tmp_path / "slot.mp4"
    output.write_bytes(b"approved old asset")
    runner = _SuccessfulRunner()

    with pytest.raises(ValueError, match="exceeds the encoded source frame"):
        derive_source_frame_video(
            source,
            output,
            timestamp=1,
            duration=2.5,
            crop=(500, 0, 200, 360),
            runner=runner,
        )

    assert output.read_bytes() == b"approved old asset"
    assert not any(call[0] == "ffmpeg" for call in runner.calls)


def test_timestamp_outside_source_fails_before_ffmpeg(tmp_path):
    source = _source(tmp_path)
    runner = _SuccessfulRunner()

    with pytest.raises(ValueError, match="falls outside"):
        derive_source_frame_video(
            source,
            tmp_path / "slot.mp4",
            timestamp=5,
            duration=1,
            runner=runner,
        )

    assert not any(call[0] == "ffmpeg" for call in runner.calls)


@pytest.mark.parametrize(
    ("probe", "message"),
    [
        (_output_probe(codec="hevc"), "expected H.264"),
        (_output_probe(width=1280), "expected 1920x1080"),
        (_output_probe(pix_fmt="yuv444p"), "expected yuv420p"),
        (_output_probe(with_audio=True), "must be silent"),
        (_output_probe(frames=74), "expected 75"),
    ],
)
def test_validation_fails_closed_and_preserves_existing_asset(
    tmp_path, probe, message
):
    source = _source(tmp_path)
    output = tmp_path / "slot.mp4"
    output.write_bytes(b"approved old asset")
    runner = _SuccessfulRunner(output_probe=probe)

    with pytest.raises(RuntimeError, match=message):
        derive_source_frame_video(
            source,
            output,
            timestamp=1,
            duration=2.5,
            encoder="libx264",
            runner=runner,
        )

    assert output.read_bytes() == b"approved old asset"
def test_ffmpeg_failure_preserves_existing_asset(tmp_path):
    source = _source(tmp_path)
    output = tmp_path / "slot.mp4"
    output.write_bytes(b"approved old asset")
    calls = []

    def runner(argv):
        calls.append(argv)
        if argv[0] == "ffprobe":
            return 0, _source_probe(), b""
        return 1, b"", b"decoder exploded"

    with pytest.raises(RuntimeError, match="decoder exploded"):
        derive_source_frame_video(
            source,
            output,
            timestamp=1,
            duration=2.5,
            runner=runner,
        )

    assert output.read_bytes() == b"approved old asset"

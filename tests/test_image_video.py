from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from rabbithole.sources.image_video import derive_source_image_video


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
        self.encoded_frame_name = ""
        self.encoded_frame_size: tuple[int, int] | None = None
        self.encoded_frame_pixels: dict[tuple[int, int], tuple[int, int, int]] = {}

    def __call__(self, argv: list[str]):
        self.calls.append(argv)
        if argv[0] == "ffprobe":
            return 0, self.output_probe, b""

        frame_path = Path(argv[argv.index("-i") + 1])
        self.encoded_frame_name = frame_path.name
        with Image.open(frame_path) as frame:
            converted = frame.convert("RGB")
            self.encoded_frame_size = converted.size
            for point in ((10, 540), (960, 540)):
                self.encoded_frame_pixels[point] = converted.getpixel(point)
        Path(argv[-1]).write_bytes(b"fake mp4")
        return 0, b"", b""


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source.png"
    image = Image.new("RGB", (400, 200), "#FF0000")
    image.paste("#0000FF", (200, 0, 400, 200))
    image.save(source)
    return source


def test_derives_cropped_slot_length_silent_h264_video(tmp_path):
    source = _source(tmp_path)
    output = tmp_path / "slot.mp4"
    runner = _SuccessfulRunner()

    result = derive_source_image_video(
        source,
        output,
        duration=2.5,
        crop=(0, 0, 200, 200),
        encoder="libx264",
        runner=runner,
    )

    assert result == output.resolve()
    assert output.read_bytes() == b"fake mp4"
    assert runner.encoded_frame_size == (1920, 1080)
    assert runner.encoded_frame_pixels[(10, 540)] == (0, 0, 0)
    assert runner.encoded_frame_pixels[(960, 540)] == (255, 0, 0)

    ffmpeg_calls = [call for call in runner.calls if call[0] == "ffmpeg"]
    assert len(ffmpeg_calls) == 1
    encode = ffmpeg_calls[0]
    assert "-loop" in encode
    assert "-an" in encode
    assert encode[encode.index("-frames:v") + 1] == "75"
    assert encode[encode.index("-c:v") + 1] == "libx264"
    assert encode[encode.index("-pix_fmt") + 1] == "yuv420p"
    assert "+faststart" in encode

    probes = [call for call in runner.calls if call[0] == "ffprobe"]
    assert len(probes) == 1
    assert "-count_frames" in probes[0]


def test_off_grid_duration_rounds_up_so_asset_never_ends_before_slot(tmp_path):
    source = _source(tmp_path)
    runner = _SuccessfulRunner(output_probe=_output_probe(frames=4))

    derive_source_image_video(
        source,
        tmp_path / "slot.mp4",
        duration=0.101,
        encoder="libx264",
        runner=runner,
    )

    encode = [call for call in runner.calls if call[0] == "ffmpeg"][0]
    assert encode[encode.index("-frames:v") + 1] == "4"


def test_optional_attribution_and_date_are_burned_into_the_frame(tmp_path):
    source = _source(tmp_path)
    runner = _SuccessfulRunner()

    derive_source_image_video(
        source,
        tmp_path / "slot.mp4",
        duration=2.5,
        attribution="SOURCE: WEBDRIVER TORSO",
        date_label="Captured 2014-05-01",
        encoder="libx264",
        runner=runner,
    )

    assert runner.encoded_frame_name == "labelled-frame.png"


def test_crop_outside_source_fails_before_ffmpeg_and_preserves_asset(tmp_path):
    source = _source(tmp_path)
    output = tmp_path / "slot.mp4"
    output.write_bytes(b"approved old asset")
    runner = _SuccessfulRunner()

    with pytest.raises(ValueError, match="exceeds the encoded source frame"):
        derive_source_image_video(
            source,
            output,
            duration=2.5,
            crop=(300, 0, 200, 200),
            runner=runner,
        )

    assert output.read_bytes() == b"approved old asset"
    assert not runner.calls


def test_corrupt_source_fails_closed_and_preserves_existing_asset(tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(b"not an image")
    output = tmp_path / "slot.mp4"
    output.write_bytes(b"approved old asset")
    runner = _SuccessfulRunner()

    with pytest.raises(RuntimeError, match="could not be fully decoded"):
        derive_source_image_video(
            source,
            output,
            duration=2.5,
            runner=runner,
        )

    assert output.read_bytes() == b"approved old asset"
    assert not runner.calls


def test_animated_source_is_rejected_as_not_a_still(tmp_path):
    source = tmp_path / "source.gif"
    frames = [
        Image.new("RGB", (20, 20), "#FF0000"),
        Image.new("RGB", (20, 20), "#0000FF"),
    ]
    frames[0].save(
        source,
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )

    with pytest.raises(RuntimeError, match="must contain exactly one"):
        derive_source_image_video(
            source,
            tmp_path / "slot.mp4",
            duration=1,
            runner=_SuccessfulRunner(),
        )


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
        derive_source_image_video(
            source,
            output,
            duration=2.5,
            encoder="libx264",
            runner=runner,
        )

    assert output.read_bytes() == b"approved old asset"


def test_ffmpeg_failure_preserves_existing_asset(tmp_path):
    source = _source(tmp_path)
    output = tmp_path / "slot.mp4"
    output.write_bytes(b"approved old asset")

    def runner(argv):
        if argv[0] == "ffmpeg":
            return 1, b"", b"encoder exploded"
        return 0, _output_probe(), b""

    with pytest.raises(RuntimeError, match="encoder exploded"):
        derive_source_image_video(
            source,
            output,
            duration=2.5,
            runner=runner,
        )

    assert output.read_bytes() == b"approved old asset"

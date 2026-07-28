from __future__ import annotations

import subprocess

import pytest

from rabbithole.edl import Cut
from rabbithole.overlays import Overlay
from rabbithole.segments import (
    parse_timecode,
    trim_audio,
    validate_window,
    window_cuts,
    window_document,
    window_overlays,
)


def test_parse_timecode_accepts_seconds_mmss_and_hhmmss():
    assert parse_timecode("12.5") == pytest.approx(12.5)
    assert parse_timecode("02:03.5") == pytest.approx(123.5)
    assert parse_timecode("1:02:03.5") == pytest.approx(3723.5)


@pytest.mark.parametrize("value", ["nope", "01:70", "1:70:00"])
def test_parse_timecode_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        parse_timecode(value)


def test_validate_window_refuses_invalid_or_out_of_bounds_ranges():
    with pytest.raises(ValueError):
        validate_window(-1, 2, 10)
    with pytest.raises(ValueError):
        validate_window(2, 2, 10)
    with pytest.raises(ValueError):
        validate_window(2, 11, 10)


def test_window_cuts_clips_edges_but_keeps_episode_coordinates():
    cuts = [
        Cut(0, 0.0, 2.0, "s001", "script", "wide", "cut", "one"),
        Cut(1, 2.0, 4.0, "s002", "script", "detail", "cut", "two"),
        Cut(2, 4.0, 6.0, "s003", "script", "wide", "cut", "three"),
    ]

    selected = window_cuts(cuts, 1.0, 5.0)

    assert [(cut.start, cut.end) for cut in selected] == [
        (1.0, 2.0),
        (2.0, 4.0),
        (4.0, 5.0),
    ]
    assert [cut.index for cut in selected] == [0, 1, 2]


def test_window_cuts_ignores_floating_point_dust_at_the_window_boundary():
    cuts = [
        Cut(
            0,
            500.0,
            503.34400000000005,
            "s042",
            "script",
            "wide",
            "cut",
            "previous",
        ),
        Cut(
            1,
            503.34400000000005,
            510.0,
            "s043",
            "script",
            "wide",
            "cut",
            "inside",
        ),
    ]

    selected = window_cuts(cuts, 503.344, 510.0)

    assert [cut.slot_id for cut in selected] == ["s043"]
    assert selected[0].start == pytest.approx(503.34400000000005)


def test_window_overlays_shifts_to_segment_zero():
    overlays = [
        Overlay("chapter-card", 8.0, 12.0, "Title", "1"),
        Overlay("censor", 20.0, 21.0, "", "face"),
    ]

    selected = window_overlays(overlays, 10.0, 15.0)

    assert selected == [Overlay("chapter-card", 0.0, 2.0, "Title", "1")]


def test_window_document_shifts_words_and_carries_active_music():
    document = {
        "duration_seconds": 30.0,
        "words": [
            {"index": 0, "word": "before", "start": 1.0, "end": 1.5},
            {"index": 1, "word": "inside", "start": 11.0, "end": 11.5},
        ],
        "markers": [
            {"kind": "MUSIC", "arg": "drone-low", "seconds": 5.0, "line": 1},
            {"kind": "SFX", "arg": "bass-thud", "seconds": 12.0, "line": 2},
            {"kind": "MUSIC", "arg": "out", "seconds": 18.0, "line": 3},
        ],
    }

    selected = window_document(document, 10.0, 15.0)

    assert selected["duration_seconds"] == pytest.approx(5.0)
    assert selected["words"][0]["start"] == pytest.approx(1.0)
    assert [(m["kind"], m["arg"], m["seconds"]) for m in selected["markers"]] == [
        ("MUSIC", "drone-low", 0.0),
        ("SFX", "bass-thud", 2.0),
    ]


def test_trim_audio_writes_only_the_requested_duration(tmp_path):
    source = tmp_path / "source.wav"
    out = tmp_path / "out.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3:sample_rate=44100",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(source),
        ],
        check=True,
    )

    trim_audio(source, out, start=1.0, duration=0.75)

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(out),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    assert float(result.stdout.strip()) == pytest.approx(0.75, abs=0.02)

from __future__ import annotations

import json
import subprocess

import pytest

from rabbithole.highlights import (
    ArticleHighlight,
    apply_highlights,
    highlight_filter,
    load_highlights,
    window_highlights,
)


def _document() -> dict:
    return {
        "words": [
            {"index": 7, "word": "cockroaches", "start": 12.25, "end": 12.8},
        ]
    }


def test_load_highlights_accepts_list_specs(tmp_path):
    path = tmp_path / "highlights.json"
    path.write_text(
        json.dumps(
            [
                {
                    "start": 3.0,
                    "end": 8.0,
                    "rect": [0.1, 0.2, 0.5, 0.08],
                    "source": "AP, 16 May 2026",
                }
            ]
        ),
        encoding="utf-8",
    )

    [item] = load_highlights(path, _document())

    assert item.start == pytest.approx(3.0)
    assert item.color == "FFB900"
    assert item.opacity == pytest.approx(1.0)
    assert item.source == "AP, 16 May 2026"


def test_load_highlights_resolves_word_time_and_style_defaults(tmp_path):
    path = tmp_path / "highlights.json"
    path.write_text(
        json.dumps(
            {
                "style": {
                    "color": "#E2BC2A",
                    "opacity": 0.62,
                    "reveal_seconds": 0.6,
                },
                "highlights": [
                    {
                        "word_index": 7,
                        "hold_seconds": 4,
                        "rect": [0.15, 0.3, 0.7, 0.06],
                        "label": "quoted insult",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    [item] = load_highlights(path, _document())

    assert item.start == pytest.approx(12.25)
    assert item.end == pytest.approx(16.25)
    assert item.color == "E2BC2A"
    assert item.opacity == pytest.approx(0.62)
    assert item.reveal_seconds == pytest.approx(0.6)


@pytest.mark.parametrize(
    "rect",
    [
        [-0.1, 0.1, 0.5, 0.1],
        [0.1, 0.1, 0.0, 0.1],
        [0.8, 0.1, 0.3, 0.1],
        [0.1, 0.95, 0.3, 0.1],
    ],
)
def test_load_highlights_rejects_rectangles_outside_frame(tmp_path, rect):
    path = tmp_path / "highlights.json"
    path.write_text(
        json.dumps(
            {
                "highlights": [
                    {"start": 1, "end": 2, "rect": rect},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="outside"):
        load_highlights(path, _document())


def test_window_highlights_clips_and_shifts_to_segment_zero():
    items = [
        ArticleHighlight(9.0, 14.0, 0.1, 0.2, 0.5, 0.1),
        ArticleHighlight(13.0, 20.0, 0.2, 0.4, 0.4, 0.1),
    ]

    selected = window_highlights(items, 10.0, 15.0)

    assert [(item.start, item.end) for item in selected] == [
        (0.0, 4.0),
        (3.0, 5.0),
    ]
    assert selected[0].reveal_seconds == pytest.approx(0.001)
    assert selected[1].reveal_seconds == pytest.approx(0.52)


def test_highlight_filter_builds_frame_quantized_wipe_then_persistent_hold():
    item = ArticleHighlight(
        start=2.0,
        end=5.0,
        x=0.1,
        y=0.2,
        width=0.5,
        height=0.1,
        reveal_seconds=0.4,
        color="FFB900",
        opacity=1.0,
    )

    chain = highlight_filter([item], 1000, 500, fps=10)

    assert chain.count("drawbox=") == 4
    assert "x=100:y=100:w=125" in chain
    assert "x=225:y=100:w=125" in chain
    assert "x=350:y=100:w=125" in chain
    assert "x=475:y=100:w=125" in chain
    assert ":h=50" in chain
    assert "0xFFB900@1.000" in chain
    assert "between(t\\,2.000000\\,5.000000)" in chain
    assert "between(t\\,2.300000\\,5.000000)" in chain


def test_apply_highlights_multiplies_under_dark_type(tmp_path):
    source = tmp_path / "page.mp4"
    out = tmp_path / "highlighted.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=320x180:r=10:d=1",
            "-vf",
            "drawbox=x=64:y=72:w=64:h=18:color=black:t=fill",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    item = ArticleHighlight(
        start=0.1,
        end=0.9,
        x=0.1,
        y=0.3,
        width=0.6,
        height=0.3,
        reveal_seconds=0.2,
    )

    apply_highlights(source, [item], out, width=320, height=180, fps=10)

    sample = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            "0.5",
            "-i",
            str(out),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout

    def pixel(x: int, y: int) -> tuple[int, int, int]:
        offset = (y * 320 + x) * 3
        return tuple(sample[offset : offset + 3])

    yellow = pixel(48, 64)
    ink = pixel(80, 80)
    page = pixel(10, 10)
    assert yellow[0] > 230 and 150 < yellow[1] < 210 and yellow[2] < 40
    assert max(ink) < 35
    assert min(page) > 230

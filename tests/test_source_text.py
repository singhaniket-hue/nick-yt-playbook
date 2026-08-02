"""Disclosed source-text extracts for obstructed browser evidence."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import rabbithole.cards as cards_module
from rabbithole.jsonio import read_json
from rabbithole.source_text import (
    SOURCE_TEXT_EXTRACT_DISCLOSURE,
    SourceTextExtractSpec,
    build_source_text_extract,
    publisher_from_url,
    source_text_extract_ass,
)
from rabbithole.sources.plates import load_grade


REPO_ROOT = Path(__file__).resolve().parent.parent
STYLE_DIR = REPO_ROOT / "style"


@pytest.fixture(scope="module")
def style():
    return (
        read_json(STYLE_DIR / "typography.json"),
        read_json(STYLE_DIR / "palette.json"),
        load_grade(STYLE_DIR),
    )


def _spec(**overrides) -> SourceTextExtractSpec:
    values = {
        "text": "But the truth is, as ever, more mundane",
        "publisher": "theguardian.com",
        "title": "The truth behind one YouTube account's 77,000 mysterious videos",
        "date": "2014-05-01",
        "url": (
            "https://www.theguardian.com/technology/shortcuts/2014/may/01/"
            "truth-youtube-mysterious-videos-webdriver-torso"
        ),
        "duration": 1.5,
    }
    values.update(overrides)
    return SourceTextExtractSpec(**values)


def test_spec_requires_truthful_source_metadata_and_http_url():
    with pytest.raises(ValueError, match="date must be non-empty"):
        _spec(date="")
    with pytest.raises(ValueError, match=r"absolute HTTP\(S\) URL"):
        _spec(url="guardian article")
    with pytest.raises(ValueError, match="text must be non-empty"):
        _spec(text="  ")


def test_publisher_label_is_portable_and_drops_only_www():
    assert publisher_from_url("https://www.theguardian.com/story") == "theguardian.com"
    assert publisher_from_url("https://news.example.test/story") == "news.example.test"


def test_ass_discloses_typesetting_and_highlights_only_the_verbatim_row(style):
    typography, palette, _grade = style
    spec = _spec()

    document = source_text_extract_ass(spec, typography, palette, width=640, height=360)
    readable_text = document.replace(r"\N", " ")
    compact_text = document.replace(r"\N", "")
    warning = cards_module._wrap_override(palette["warning_yellow"])
    yellow_bands = [
        line
        for line in document.splitlines()
        if line.startswith("Dialogue:")
        and ",CardShape," in line
        and warning in line
    ]

    assert SOURCE_TEXT_EXTRACT_DISCLOSURE in readable_text
    assert "SOURCE |" in readable_text
    assert " · " not in readable_text
    assert spec.publisher in readable_text
    assert spec.title in readable_text
    assert spec.date in readable_text
    assert spec.url in compact_text
    assert spec.text in readable_text
    assert len(yellow_bands) == 1
    assert "SCREENSHOT" not in readable_text.upper()
    assert "SOURCE PIXELS" not in readable_text.upper()


def test_real_render_contains_one_visible_yellow_reading_band(style, tmp_path):
    typography, palette, grade = style
    output = build_source_text_extract(
        _spec(duration=1.2),
        tmp_path / "source-text.mp4",
        typography,
        palette,
        grade,
        tmp_path / "work",
        width=640,
        height=360,
        fps=12,
    )
    frame_path = tmp_path / "frame.png"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(output),
            "-ss",
            "0.5",
            "-frames:v",
            "1",
            str(frame_path),
        ],
        check=True,
    )
    frame = np.asarray(Image.open(frame_path).convert("RGB"))
    yellow = (
        (frame[:, :, 0] > 175)
        & (frame[:, :, 1] > 155)
        & (frame[:, :, 2] < 100)
    )
    ys, _xs = np.where(yellow)

    assert yellow.mean() > 0.004
    assert ys.size
    assert int(ys.max()) - int(ys.min()) < frame.shape[0] * 0.25

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from rabbithole.evidence import _validate_frames, render_evidence_frame


def test_render_evidence_frame_crops_source_and_draws_outline(tmp_path):
    project = tmp_path / "project"
    source = project / "assets" / "source.png"
    source.parent.mkdir(parents=True)
    image = Image.new("RGB", (100, 100), "white")
    for x in range(50, 100):
        for y in range(100):
            image.putpixel((x, y), (0, 90, 180))
    image.save(source)

    out = tmp_path / "frame.png"
    render_evidence_frame(
        project,
        {
            "background": "#000000",
            "layers": [
                {
                    "image": "assets/source.png",
                    "crop": [50, 0, 50, 100],
                    "target": [20, 10, 100, 80],
                    "fit": "stretch",
                }
            ],
            "outlines": [{"rect": [20, 10, 100, 80], "width": 3}],
        },
        out,
        width=160,
        height=100,
    )

    rendered = Image.open(out).convert("RGB")
    assert rendered.getpixel((70, 50))[2] > 150
    assert rendered.getpixel((20, 10))[0] > 230
    assert rendered.getpixel((5, 5)) == (0, 0, 0)


def test_validate_frames_requires_exact_slot_coverage():
    _validate_frames(
        "s001",
        [{"start": 0, "end": 2}, {"start": 2, "end": 5}],
        5,
    )

    with pytest.raises(ValueError, match="gap/overlap"):
        _validate_frames(
            "s001",
            [{"start": 0, "end": 2}, {"start": 2.5, "end": 5}],
            5,
        )

    with pytest.raises(ValueError, match="timing slot lasts"):
        _validate_frames("s001", [{"start": 0, "end": 4}], 5)

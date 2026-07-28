"""Fail-closed CLI source-quality preflight."""

from __future__ import annotations

import argparse
import json

import pytest

from rabbithole import cli


def _project(
    tmp_path,
    shot_arg: str,
    *,
    with_record: bool = True,
    record_tier: str = "atmospheric",
):
    root = tmp_path / "projects" / "qa"
    (root / "narration").mkdir(parents=True)
    (root / "edit").mkdir()

    timing = {
        "duration_seconds": 10.0,
        "word_count": 0,
        "words": [],
        "markers": [
            {
                "kind": "SHOT",
                "arg": shot_arg,
                "word_index": 0,
                "line": 1,
                "seconds": 0.0,
            }
        ],
    }
    timing_path = root / "narration" / "timing.json"
    timing_path.write_text(json.dumps(timing), encoding="utf-8")

    (root / "edit" / "edl.json").write_text(
        json.dumps(
            {
                "duration_seconds": 10.0,
                "cut_count": 1,
                "average_shot_length": 10.0,
                "cuts": [
                    {
                        "index": 0,
                        "start": 0.0,
                        "end": 10.0,
                        "slot_id": "s001",
                        "origin": "script",
                        "framing": "wide",
                        "transition": "cut",
                        "reason": "test",
                    }
                ],
                "overlays": [],
            }
        ),
        encoding="utf-8",
    )

    records = []
    if with_record:
        records.append(
            {
                "asset_id": "card-s001",
                "tier": record_tier,
                "provider": (
                    "rabbithole-cards"
                    if record_tier == "atmospheric"
                    else "licensed-source"
                ),
                "original_url": (
                    "" if record_tier == "atmospheric" else "https://example.com/clip.mp4"
                ),
                "license": (
                    "" if record_tier == "atmospheric" else "licensed for documentary use"
                ),
                "retrieved_at": "2026-07-27T00:00:00Z",
                "local_path": str(root / "assets" / "clip.mp4"),
                "used_in_slots": ["s001"],
                "notes": "",
            }
        )
    (root / "provenance.json").write_text(
        json.dumps(records), encoding="utf-8"
    )
    return timing_path


def _args(timing_path, *, quality: str, dry_run: bool = True):
    return argparse.Namespace(
        timing_json=str(timing_path),
        dry_run=dry_run,
        quality=quality,
    )


def test_final_render_cli_reports_placeholder_and_evidence_failures(tmp_path, capsys):
    timing_path = _project(tmp_path, "graphic headline zoom")

    result = cli.cmd_render(_args(timing_path, quality="final"))

    output = capsys.readouterr().out
    assert result == 1
    assert "label-only production note" in output
    assert "Sourced-evidence ratio is 0.0%" in output
    assert "minimum of 50%" in output


def test_animatic_render_cli_explicitly_allows_the_same_placeholder(tmp_path, capsys):
    timing_path = _project(tmp_path, "graphic headline zoom")

    result = cli.cmd_render(_args(timing_path, quality="animatic"))

    assert result == 0
    assert "PASSED" in capsys.readouterr().out


@pytest.mark.parametrize(
    "shot_arg",
    [
        "plate dark corridor reenactment",
        "graphic headline zoom",
    ],
)
def test_final_render_cli_accepts_sourced_footage_replacing_generated_visual(
    tmp_path, capsys, shot_arg
):
    timing_path = _project(tmp_path, shot_arg, record_tier="primary")

    result = cli.cmd_render(_args(timing_path, quality="final"))

    assert result == 0
    assert "PASSED" in capsys.readouterr().out


def test_render_cli_runs_the_provenance_gate_before_rendering(tmp_path, capsys):
    timing_path = _project(
        tmp_path, "graphic criteria: one | two", with_record=False
    )

    result = cli.cmd_render(_args(timing_path, quality="animatic"))

    output = capsys.readouterr().out
    assert result == 1
    assert "[provenance]" in output
    assert "no asset claiming it" in output


def test_live_final_render_stops_before_media_work_on_source_qa_error(
    tmp_path, monkeypatch, capsys
):
    timing_path = _project(tmp_path, "graphic headline zoom")

    def must_not_render(*args, **kwargs):
        raise AssertionError("assemble_footage was called before source QA")

    monkeypatch.setattr(cli, "assemble_footage", must_not_render)

    result = cli.cmd_render(
        _args(timing_path, quality="final", dry_run=False)
    )

    assert result == 1
    assert "Refusing to render" in capsys.readouterr().out

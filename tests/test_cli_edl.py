import argparse
import json

from rabbithole import cli


# --- fixtures ---------------------------------------------------------------


def _word(index, word, start, end):
    return {"index": index, "word": word, "start": start, "end": end}


def _words(count, start=0.0, step=0.34):
    """`count` synthetic words at a fixed cadence, mimicking ~177 WPM speech."""
    out = []
    t = start
    for i in range(count):
        out.append(_word(i, f"w{i}", t, t + step * 0.8))
        t += step
    return out


def _shot(arg, seconds, word_index=0, line=1):
    return {"kind": "SHOT", "arg": arg, "word_index": word_index, "line": line, "seconds": seconds}


def _key(arg, seconds, word_index=0, line=1):
    return {"kind": "KEY", "arg": arg, "word_index": word_index, "line": line, "seconds": seconds}


def _document(duration, markers, words=None):
    words = words or []
    return {
        "duration_seconds": duration,
        "word_count": len(words),
        "words": words,
        "markers": markers,
    }


def _clean_document():
    """A two-slot, 30s document that builds a clean EDL with no findings."""
    return _document(
        30.0,
        [
            _shot("screenshot a", 0.0, word_index=0),
            _shot("archival b", 15.0, word_index=45),
        ],
        words=_words(90),
    )


def _timing_path(tmp_path, slug="demo"):
    path = tmp_path / "projects" / slug / "narration" / "timing.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")


def _namespace(timing_json, dry_run=False, out=None, quality=None):
    values = {
        "timing_json": str(timing_json),
        "dry_run": dry_run,
        "out": out,
    }
    if quality is not None:
        values["quality"] = quality
    return argparse.Namespace(**values)


# --- writes edl.json at the derived project path -------------------------------


def test_writes_edl_json_at_derived_project_path(tmp_path, capsys):
    timing_path = _timing_path(tmp_path)
    _write(timing_path, _clean_document())

    cli.cmd_edl(_namespace(timing_path))

    expected = timing_path.parent.parent / "edit" / "edl.json"
    assert expected.exists()


# --- --dry-run writes nothing ---------------------------------------------------


def test_dry_run_writes_nothing(tmp_path, capsys):
    timing_path = _timing_path(tmp_path)
    _write(timing_path, _clean_document())

    cli.cmd_edl(_namespace(timing_path, dry_run=True))

    expected = timing_path.parent.parent / "edit" / "edl.json"
    assert not expected.exists()
    # Something is still printed even though nothing is written.
    out = capsys.readouterr().out
    assert out.strip() != ""


# --- --out overrides the path ---------------------------------------------------


def test_out_overrides_the_default_path(tmp_path, capsys):
    timing_path = _timing_path(tmp_path)
    _write(timing_path, _clean_document())
    custom_out = tmp_path / "custom" / "cuts.json"

    cli.cmd_edl(_namespace(timing_path, out=custom_out))

    assert custom_out.exists()
    default_path = timing_path.parent.parent / "edit" / "edl.json"
    assert not default_path.exists()


# --- written JSON shape ----------------------------------------------------------


def test_written_json_round_trips_and_cut_count_matches_cuts_array_length(tmp_path, capsys):
    timing_path = _timing_path(tmp_path)
    _write(timing_path, _clean_document())

    cli.cmd_edl(_namespace(timing_path))

    out_path = timing_path.parent.parent / "edit" / "edl.json"
    data = json.loads(out_path.read_text(encoding="utf-8"))

    assert data["cut_count"] == len(data["cuts"])
    assert data["cut_count"] > 0
    assert "overlays" in data
    assert "duration_seconds" in data
    assert "average_shot_length" in data
    # Direct legacy callers without a parser-provided quality retain the
    # historical preview behaviour.
    assert data["quality"] == "animatic"
    assert data["mode"] == "animatic"


def test_cli_edl_defaults_to_final_editorial_mode(tmp_path, capsys):
    timing_path = _timing_path(tmp_path)
    _write(timing_path, _clean_document())

    result = cli.main(["edl", str(timing_path)])

    data = json.loads(
        (timing_path.parent.parent / "edit" / "edl.json").read_text(
            encoding="utf-8"
        )
    )
    assert result == 0
    assert data["quality"] == "final"
    assert data["mode"] == "editorial"
    assert data["cut_count"] == 2
    assert all(cut["origin"] == "script" for cut in data["cuts"])
    assert all(cut["transition"] == "cut" for cut in data["cuts"])


def test_cli_edl_animatic_quality_preserves_asl_preview_cuts(tmp_path, capsys):
    timing_path = _timing_path(tmp_path)
    _write(timing_path, _clean_document())

    result = cli.main(
        ["edl", str(timing_path), "--quality", "animatic"]
    )

    data = json.loads(
        (timing_path.parent.parent / "edit" / "edl.json").read_text(
            encoding="utf-8"
        )
    )
    assert result == 0
    assert data["quality"] == "animatic"
    assert data["mode"] == "animatic"
    assert any(cut["origin"] == "asl-fill" for cut in data["cuts"])


# --- exit codes -------------------------------------------------------------------


def test_exit_code_zero_on_clean_document(tmp_path, capsys):
    timing_path = _timing_path(tmp_path)
    _write(timing_path, _clean_document())

    result = cli.cmd_edl(_namespace(timing_path, dry_run=True))

    assert result == 0


def test_exit_code_one_when_an_error_finding_is_present(tmp_path, capsys):
    # No [SHOT:] markers at all -> build_slots yields no slots -> check_slots
    # reports an error ("every episode needs at least one [SHOT:] marker").
    timing_path = _timing_path(tmp_path)
    _write(timing_path, _document(10.0, []))

    result = cli.cmd_edl(_namespace(timing_path, dry_run=True))

    assert result == 1


def test_warnings_alone_do_not_produce_exit_code_one(tmp_path, capsys):
    # A [KEY:] marker naming a word that does not exist in the script produces
    # a warning-severity finding (rabbithole.overlays.check_overlays) and
    # nothing else; the rest of the document is otherwise clean.
    document = _clean_document()
    document["markers"].append(_key("nonexistent-word", 5.0, word_index=0))
    timing_path = _timing_path(tmp_path)
    _write(timing_path, document)

    result = cli.cmd_edl(_namespace(timing_path, dry_run=True))

    report = capsys.readouterr().out
    assert "[WARNING]" in report
    assert "[ERROR]" not in report
    assert result == 0

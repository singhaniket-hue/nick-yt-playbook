"""Re-marking an episode against narration already rendered.

Why this exists: a production episode had 24 static slots holding 16-40 seconds
each, which the shot-density gate flags as needing more `[SHOT:]` markers. Fixing
that by re-narrating would cost ~30,000 characters. It does not have to: markers
are stripped before TTS, so adding one leaves every spoken word and every
recorded timestamp exactly valid.

The guard is the load-bearing part. If the words drift, the timings no longer
describe the text, and a rebuilt spine would anchor cuts to words at moments they
are not spoken -- wrong in a way that looks completely plausible downstream.
"""

from __future__ import annotations

import pytest

from rabbithole.markers import parse
from rabbithole.timing import (
    WordTime,
    rebuild_timing,
    timing_document,
    word_times_from_document,
)

SCRIPT = (
    "[ACT:1 Cold Open]\n"
    "ek do teen chaar paanch chhah saat aath nau das.\n"
)


def _times(words: list[str], step: float = 0.5) -> list[WordTime]:
    return [
        WordTime(index=i, word=w, start=i * step, end=(i + 1) * step)
        for i, w in enumerate(words)
    ]


def _document(script: str = SCRIPT, duration: float = 5.0) -> dict:
    parsed = parse(script)
    return timing_document(parsed, _times(parsed.text.split()), duration)


def test_round_trips_word_times_through_a_document():
    document = _document()
    recovered = word_times_from_document(document)

    assert [w.word for w in recovered] == [e["word"] for e in document["words"]]
    assert [w.index for w in recovered] == [e["index"] for e in document["words"]]
    assert recovered[0].start == pytest.approx(document["words"][0]["start"])
    assert recovered[-1].end == pytest.approx(document["words"][-1]["end"])


def test_adding_a_shot_marker_needs_no_renarration():
    """The whole point: a new marker gets a real time from existing timings."""
    document = _document()
    edited = SCRIPT.replace("teen chaar", "teen [SHOT:archival a courtroom] chaar")

    rebuilt, problems = rebuild_timing(parse(edited), document)

    assert problems == []
    shots = [m for m in rebuilt["markers"] if m["kind"] == "SHOT"]
    assert len(shots) == 1
    assert shots[0]["seconds"] == pytest.approx(1.5)  # start of "chaar", word 3


def test_rebuilding_preserves_the_word_timings_untouched():
    document = _document()
    edited = SCRIPT.replace("teen chaar", "teen [SHOT:archival a courtroom] chaar")

    rebuilt, _ = rebuild_timing(parse(edited), document)

    assert rebuilt["words"] == document["words"]
    assert rebuilt["duration_seconds"] == document["duration_seconds"]


def test_rebuilding_keeps_the_markers_that_were_already_there():
    document = _document()
    edited = SCRIPT.replace("teen chaar", "teen [SHOT:archival a courtroom] chaar")

    rebuilt, _ = rebuild_timing(parse(edited), document)

    kinds = [m["kind"] for m in rebuilt["markers"]]
    assert "ACT" in kinds
    assert kinds.count("SHOT") == 1


def test_removing_a_marker_also_works():
    with_shot = SCRIPT.replace("teen chaar", "teen [SHOT:archival a courtroom] chaar")
    document = _document(with_shot)

    rebuilt, problems = rebuild_timing(parse(SCRIPT), document)

    assert problems == []
    assert [m["kind"] for m in rebuilt["markers"]] == ["ACT"]


# --- the guard -------------------------------------------------------------------


def test_a_changed_word_is_refused():
    """Re-marking a script whose words drifted would anchor cuts to words at
    moments they are not spoken -- plausible-looking and completely wrong."""
    document = _document()
    edited = SCRIPT.replace("teen", "TEEN_CHANGED")

    rebuilt, problems = rebuild_timing(parse(edited), document)

    assert problems, "a changed word must be refused"
    assert "differ from what was narrated" in problems[0]
    assert rebuilt == document, "the original document must be returned unmodified"


def test_an_added_word_is_refused_and_says_why():
    document = _document()
    edited = SCRIPT.replace("teen chaar", "teen gyarah chaar")

    _, problems = rebuild_timing(parse(edited), document)

    assert problems
    assert "re-run `narrate`" in problems[0]


def test_a_removed_word_is_refused():
    document = _document()
    edited = SCRIPT.replace("teen chaar ", "teen ")

    _, problems = rebuild_timing(parse(edited), document)

    assert problems


def test_the_refusal_names_the_offending_word():
    """A bare 'words differ' is not actionable across 6,000 words."""
    document = _document()
    edited = SCRIPT.replace("saat", "SAAT_WRONG")

    _, problems = rebuild_timing(parse(edited), document)

    assert "SAAT_WRONG" in problems[0]


def test_an_empty_timing_document_is_refused():
    _, problems = rebuild_timing(parse(SCRIPT), {"words": [], "markers": []})
    assert problems
    assert "narrate" in problems[0]


def test_word_count_mismatch_is_reported_before_per_word_comparison():
    """A length mismatch has its own clearer message than a wall of diffs."""
    document = _document()
    edited = SCRIPT + "\ngyarah baarah terah.\n"

    _, problems = rebuild_timing(parse(edited), document)

    assert len(problems) == 1
    assert "words but the narration was rendered from" in problems[0]

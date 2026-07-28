import pytest

from rabbithole.edl import (
    ASL_MAX_SECONDS,
    ASL_MIN_SECONDS,
    MIN_CUT_SECONDS,
    TARGET_ASL_SECONDS,
    Cut,
    build_edl,
    check_edl,
    pinned_boundaries,
    snap_to_word,
    word_starts,
)
from rabbithole.slots import Slot, build_slots


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


def _silence(seconds, arg="1.0s", word_index=0, line=1):
    return {"kind": "SILENCE", "arg": arg, "word_index": word_index, "line": line, "seconds": seconds}


def _chapter(seconds, arg="1 Opening", word_index=0, line=1):
    return {"kind": "CHAPTER", "arg": arg, "word_index": word_index, "line": line, "seconds": seconds}


def _document(duration, markers, words=None):
    words = words or []
    return {
        "duration_seconds": duration,
        "word_count": len(words),
        "words": words,
        "markers": markers,
    }


def _cut(index, start, end, slot_id="s001", origin="script", framing="wide", transition="cut", reason="r"):
    return Cut(
        index=index,
        start=start,
        end=end,
        slot_id=slot_id,
        origin=origin,
        framing=framing,
        transition=transition,
        reason=reason,
    )


# --- word_starts -------------------------------------------------------------


def test_word_starts_extracts_ascending_starts():
    document = _document(10.0, [], words=[_word(0, "a", 0.0, 0.3), _word(1, "b", 0.5, 0.8), _word(2, "c", 1.1, 1.4)])

    assert word_starts(document) == [0.0, 0.5, 1.1]


def test_word_starts_empty_document_yields_empty_list():
    document = _document(10.0, [], words=[])

    assert word_starts(document) == []


# --- snap_to_word --------------------------------------------------------------


def test_snap_to_word_picks_nearest_start():
    starts = [0.0, 1.0, 2.0, 3.0]

    assert snap_to_word(1.9, starts) == 2.0
    assert snap_to_word(1.4, starts) == 1.0


def test_snap_to_word_exact_tie_prefers_earlier():
    starts = [1.0, 2.0]

    assert snap_to_word(1.5, starts) == 1.0


def test_snap_to_word_returns_target_unchanged_on_empty_starts():
    assert snap_to_word(5.5, []) == 5.5


def test_snap_to_word_target_before_first_start_clamps_to_first():
    starts = [2.0, 4.0]

    assert snap_to_word(0.5, starts) == 2.0


def test_snap_to_word_target_after_last_start_clamps_to_last():
    starts = [2.0, 4.0]

    assert snap_to_word(9.0, starts) == 4.0


# --- pinned_boundaries ---------------------------------------------------------


def test_pinned_boundaries_includes_slot_starts_silence_and_chapter_times():
    document = _document(
        20.0,
        [
            _shot("screenshot a", 0.0, word_index=0),
            _shot("archival b", 10.0, word_index=50),
            _silence(5.0, word_index=25),
            _chapter(8.0, word_index=40),
        ],
    )
    slots = build_slots(document)

    pins = pinned_boundaries(slots, document)
    times = [p[0] for p in pins]

    assert 0.0 in times
    assert 10.0 in times
    assert 5.0 in times
    assert 8.0 in times


def test_pinned_boundaries_ascending_and_deduplicated():
    document = _document(
        20.0,
        [
            _shot("screenshot a", 0.0, word_index=0),
            _shot("archival b", 10.0, word_index=50),
            _silence(5.0, word_index=25),
            _chapter(8.0, word_index=40),
        ],
    )
    slots = build_slots(document)

    pins = pinned_boundaries(slots, document)
    times = [p[0] for p in pins]

    assert times == sorted(times)
    assert len(times) == len(set(times))


def test_pinned_boundaries_coinciding_chapter_and_slot_yields_dip_to_black():
    document = _document(
        20.0,
        [
            _shot("screenshot a", 0.0, word_index=0),
            _shot("archival b", 10.0, word_index=50),
            _chapter(10.0, word_index=50),
        ],
    )
    slots = build_slots(document)

    pins = pinned_boundaries(slots, document)
    matching = [p for p in pins if abs(p[0] - 10.0) <= 0.01]

    assert len(matching) == 1
    assert matching[0][2] == "dip-to-black"


def test_pinned_boundaries_dedupes_within_hundredth_of_a_second():
    document = _document(
        20.0,
        [
            _shot("screenshot a", 0.0, word_index=0),
            _shot("archival b", 10.0, word_index=50),
            _chapter(10.004, word_index=50),
        ],
    )
    slots = build_slots(document)

    pins = pinned_boundaries(slots, document)
    matching = [p for p in pins if 9.9 <= p[0] <= 10.1]

    assert len(matching) == 1


# --- build_edl: basic shape ----------------------------------------------------


def test_build_edl_no_slots_returns_empty_list():
    document = _document(10.0, [], words=_words(30))

    assert build_edl([], document) == []


def test_build_edl_single_ten_second_slot_produces_multiple_contiguous_cuts():
    document = _document(10.0, [_shot("screenshot a", 0.0, word_index=0)], words=_words(30))
    slots = build_slots(document)

    cuts = build_edl(slots, document)

    assert len(cuts) > 1
    for a, b in zip(cuts, cuts[1:]):
        assert a.end == b.start


def test_build_edl_cuts_contiguous_and_last_ends_at_duration():
    document = _document(10.0, [_shot("screenshot a", 0.0, word_index=0)], words=_words(30))
    slots = build_slots(document)

    cuts = build_edl(slots, document)

    for a, b in zip(cuts, cuts[1:]):
        assert a.end == b.start
    assert cuts[-1].end == document["duration_seconds"]


def test_build_edl_cut_starts_land_on_word_start_or_pinned_boundary():
    document = _document(
        20.0,
        [_shot("screenshot a", 0.0, word_index=0), _shot("archival b", 10.0, word_index=30)],
        words=_words(60),
    )
    slots = build_slots(document)
    starts = set(word_starts(document))
    pins = {round(p[0], 6) for p in pinned_boundaries(slots, document)}

    cuts = build_edl(slots, document)

    for cut in cuts[1:]:
        assert round(cut.start, 6) in {round(s, 6) for s in starts} | pins


def test_build_edl_index_sequential_from_zero():
    document = _document(10.0, [_shot("screenshot a", 0.0, word_index=0)], words=_words(30))
    slots = build_slots(document)

    cuts = build_edl(slots, document)

    assert [c.index for c in cuts] == list(range(len(cuts)))


def test_build_edl_every_cut_has_nonempty_reason():
    document = _document(10.0, [_shot("screenshot a", 0.0, word_index=0)], words=_words(30))
    slots = build_slots(document)

    cuts = build_edl(slots, document)

    assert all(c.reason.strip() for c in cuts)


def test_build_edl_adjacent_cuts_never_share_framing():
    document = _document(
        20.0,
        [_shot("screenshot a", 0.0, word_index=0), _shot("archival b", 10.0, word_index=30)],
        words=_words(60),
    )
    slots = build_slots(document)

    cuts = build_edl(slots, document)

    for a, b in zip(cuts, cuts[1:]):
        assert a.framing != b.framing


def test_build_edl_no_cut_shorter_than_min_cut_seconds_from_asl_fill():
    document = _document(10.0, [_shot("screenshot a", 0.0, word_index=0)], words=_words(30))
    slots = build_slots(document)

    cuts = build_edl(slots, document)

    fills = [c for c in cuts if c.origin == "asl-fill"]
    assert fills  # sanity: this slot is long enough to actually generate fills
    for c in fills:
        assert c.duration >= MIN_CUT_SECONDS


# --- build_edl: origin, pins, transitions --------------------------------------


def test_build_edl_slot_boundary_cuts_are_script_interior_are_asl_fill():
    document = _document(
        20.0,
        [_shot("screenshot a", 0.0, word_index=0), _shot("archival b", 10.0, word_index=30)],
        words=_words(60),
    )
    slots = build_slots(document)

    cuts = build_edl(slots, document)

    slot_starts = {slot.start for slot in slots}
    for cut in cuts:
        if cut.start in slot_starts:
            assert cut.origin == "script"
    assert any(c.origin == "asl-fill" for c in cuts)
    for cut in cuts:
        if cut.origin == "asl-fill":
            assert cut.start not in slot_starts


def test_build_edl_silence_time_appears_as_boundary_with_dip_to_black():
    document = _document(
        20.0,
        [
            _shot("screenshot a", 0.0, word_index=0),
            _silence(7.0, word_index=20),
        ],
        words=_words(60),
    )
    slots = build_slots(document)

    cuts = build_edl(slots, document)

    matching = [c for c in cuts if abs(c.start - 7.0) <= 0.001]
    assert matching
    assert matching[0].transition == "dip-to-black"


def test_build_edl_chapter_time_appears_as_boundary_with_dip_to_black():
    document = _document(
        20.0,
        [
            _shot("screenshot a", 0.0, word_index=0),
            _chapter(7.0, word_index=20),
        ],
        words=_words(60),
    )
    slots = build_slots(document)

    cuts = build_edl(slots, document)

    matching = [c for c in cuts if abs(c.start - 7.0) <= 0.001]
    assert matching
    assert matching[0].transition == "dip-to-black"


def test_build_edl_slot_boundary_transition_is_glitch_when_no_other_pin_coincides():
    document = _document(
        20.0,
        [_shot("screenshot a", 0.0, word_index=0), _shot("archival b", 10.0, word_index=30)],
        words=_words(60),
    )
    slots = build_slots(document)

    cuts = build_edl(slots, document)

    matching = [c for c in cuts if abs(c.start - 10.0) <= 0.001]
    assert matching
    assert matching[0].transition == "glitch"


# --- build_edl: fallback with no words -----------------------------------------


def test_build_edl_no_words_falls_back_to_even_subdivision():
    document = _document(10.0, [_shot("screenshot a", 0.0, word_index=0)], words=[])
    slots = build_slots(document)

    cuts = build_edl(slots, document)

    assert len(cuts) > 1
    for a, b in zip(cuts, cuts[1:]):
        assert a.end == b.start
    assert cuts[-1].end == document["duration_seconds"]
    durations = [c.duration for c in cuts]
    assert max(durations) - min(durations) < 0.01


# --- adversarial: short slot / tight pins forcing sub-minimum cuts -------------


def test_build_edl_slot_shorter_than_min_cut_seconds_still_covers_its_span():
    document = _document(
        10.5,
        [
            _shot("screenshot a", 0.0, word_index=0),
            _shot("archival b", 10.0, word_index=25),
        ],
        words=_words(30),
    )
    slots = build_slots(document)
    assert slots[-1].hold_seconds < MIN_CUT_SECONDS

    cuts = build_edl(slots, document)

    tail = [c for c in cuts if c.slot_id == slots[-1].slot_id]
    assert tail
    assert sum(c.duration for c in tail) == slots[-1].hold_seconds


def test_build_edl_two_close_silences_both_pinned_even_if_cut_is_short():
    document = _document(
        20.0,
        [
            _shot("screenshot a", 0.0, word_index=0),
            _silence(5.0, arg="1.0s", word_index=15),
            _silence(5.5, arg="1.0s", word_index=16),
        ],
        words=_words(60),
    )
    slots = build_slots(document)

    cuts = build_edl(slots, document)
    starts = [round(c.start, 3) for c in cuts]

    assert 5.0 in starts
    assert 5.5 in starts


def test_build_edl_framing_never_repeats_across_slot_boundary():
    document = _document(
        30.0,
        [
            _shot("screenshot a", 0.0, word_index=0),
            _shot("archival b", 9.36, word_index=25),
            _shot("plate c", 20.0, word_index=55),
        ],
        words=_words(90),
    )
    slots = build_slots(document)

    cuts = build_edl(slots, document)

    for a, b in zip(cuts, cuts[1:]):
        assert a.framing != b.framing


# --- build_edl: evidence-led editorial mode -----------------------------------


def test_editorial_mode_emits_only_authored_boundaries_without_asl_fill():
    document = _document(
        20.0,
        [
            _shot("screenshot a", 0.0, word_index=0),
            _silence(5.0, word_index=15),
            _chapter(9.0, word_index=27),
            _shot("archival b", 12.0, word_index=36),
        ],
        words=_words(60),
    )
    slots = build_slots(document)

    cuts = build_edl(slots, document, mode="editorial")

    assert [cut.start for cut in cuts] == [0.0, 5.0, 9.0, 12.0]
    assert all(cut.origin == "script" for cut in cuts)
    assert not any(cut.origin == "asl-fill" for cut in cuts)


def test_editorial_mode_is_wide_per_slot_and_alternates_only_within_split_slot():
    document = _document(
        20.0,
        [
            _shot("screenshot a", 0.0, word_index=0),
            _silence(5.0, word_index=15),
            _chapter(9.0, word_index=27),
            _shot("archival b", 12.0, word_index=36),
        ],
        words=_words(60),
    )
    slots = build_slots(document)

    cuts = build_edl(slots, document, mode="editorial")

    assert [cut.framing for cut in cuts] == [
        "wide",
        "push-in",
        "wide",
        "wide",
    ]
    assert [cut.transition for cut in cuts] == [
        "cut",
        "dip-to-black",
        "dip-to-black",
        "cut",
    ]


def test_build_edl_rejects_unknown_mode():
    document = _document(
        10.0,
        [_shot("screenshot a", 0.0, word_index=0)],
        words=_words(30),
    )

    with pytest.raises(ValueError, match="Unknown EDL mode"):
        build_edl(build_slots(document), document, mode="broadcast")


# --- check_edl ------------------------------------------------------------------


def test_check_edl_passes_on_well_formed_edl():
    document = _document(10.0, [_shot("screenshot a", 0.0, word_index=0)], words=_words(30))
    slots = build_slots(document)
    cuts = build_edl(slots, document)

    assert check_edl(cuts, document) == []


def test_check_edl_flags_duration_mismatch():
    document = _document(10.0, [], words=[])
    cuts = [_cut(0, 0.0, 5.0)]

    findings = check_edl(cuts, document)

    assert any(f.gate == "edl" and f.severity == "error" for f in findings)


def test_check_edl_flags_gap_between_cuts():
    document = _document(10.0, [], words=[])
    cuts = [
        _cut(0, 0.0, 4.0, framing="wide"),
        _cut(1, 4.5, 10.0, framing="push-in"),
    ]

    findings = check_edl(cuts, document)

    assert any(f.gate == "edl" and f.severity == "error" for f in findings)


def test_check_edl_flags_empty_reason():
    document = _document(10.0, [], words=[])
    cuts = [_cut(0, 0.0, 10.0, reason="")]

    findings = check_edl(cuts, document)

    assert any(f.gate == "edl" and f.severity == "error" and "reason" in f.message.lower() for f in findings)


def test_check_edl_flags_adjacent_shared_framing():
    document = _document(10.0, [], words=[])
    cuts = [
        _cut(0, 0.0, 4.0, framing="wide"),
        _cut(1, 4.0, 10.0, framing="wide"),
    ]

    findings = check_edl(cuts, document)

    assert any(f.gate == "edl" and f.severity == "error" and "framing" in f.message.lower() for f in findings)


def test_check_edl_allows_shared_framing_across_distinct_slots():
    document = _document(10.0, [], words=[])
    cuts = [
        _cut(0, 0.0, 5.0, slot_id="s001", framing="wide"),
        _cut(1, 5.0, 10.0, slot_id="s002", framing="wide"),
    ]

    findings = check_edl(cuts, document)

    assert not any(
        f.gate == "edl" and "framing" in f.message.lower()
        for f in findings
    )


def test_check_edl_flags_fifth_transition_value():
    document = _document(10.0, [], words=[])
    cuts = [
        _cut(0, 0.0, 2.0, framing="wide", transition="cut"),
        _cut(1, 2.0, 4.0, framing="push-in", transition="glitch"),
        _cut(2, 4.0, 6.0, framing="detail", transition="dip-to-black"),
        _cut(3, 6.0, 8.0, framing="slow-pan", transition="flash"),
        _cut(4, 8.0, 10.0, framing="wide", transition="strobe"),
    ]

    findings = check_edl(cuts, document)

    assert any(f.gate == "edl" and f.severity == "error" and "transition" in f.message.lower() for f in findings)


def test_check_edl_flags_too_short_cut():
    document = _document(10.0, [], words=[])
    cuts = [
        _cut(0, 0.0, 0.5, framing="wide"),
        _cut(1, 0.5, 10.0, framing="push-in"),
    ]

    findings = check_edl(cuts, document)

    assert any(f.gate == "edl" and f.severity == "error" and "short" in f.message.lower() for f in findings)


def test_check_edl_warns_on_out_of_band_average_shot_length():
    document = _document(10.0, [], words=[])
    cuts = [_cut(0, 0.0, 10.0, framing="wide")]

    findings = check_edl(cuts, document)

    asl_findings = [f for f in findings if f.gate == "edl" and "average" in f.message.lower()]
    assert asl_findings
    assert asl_findings[0].severity == "warning"


def test_asl_max_min_and_target_constants_are_sane():
    assert ASL_MIN_SECONDS < TARGET_ASL_SECONDS < ASL_MAX_SECONDS

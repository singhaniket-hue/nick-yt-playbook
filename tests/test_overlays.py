from rabbithole.edl import Cut
from rabbithole.overlays import (
    CHAPTER_CARD_SECONDS,
    KEYWORD_SEARCH_WINDOW_WORDS,
    Overlay,
    build_overlays,
    check_overlays,
    missing_keywords,
)


# --- fixtures ---------------------------------------------------------------


def _word(index, word, start, end):
    return {"index": index, "word": word, "start": start, "end": end}


def _chapter(arg, seconds, word_index=0, line=1):
    return {"kind": "CHAPTER", "arg": arg, "word_index": word_index, "line": line, "seconds": seconds}


def _censor(arg, seconds, word_index=0, line=1):
    return {"kind": "CENSOR", "arg": arg, "word_index": word_index, "line": line, "seconds": seconds}


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


def _overlay(kind, start, end, text="t", detail="d"):
    return Overlay(kind=kind, start=start, end=end, text=text, detail=detail)


def _words_with_target(total_words, target_index, target_word, other_word="filler"):
    """`total_words` words, all `other_word` except `target_word` at `target_index`.

    Each word is 0.2s long, back-to-back from t=0 -- enough to give the target
    word distinct, assertable `start`/`end` timings regardless of how far
    `target_index` is from the front of the list.
    """
    word_dur = 0.2
    words = []
    t = 0.0
    for i in range(total_words):
        word = target_word if i == target_index else other_word
        words.append(_word(i, word, t, t + word_dur))
        t += word_dur
    return words


# --- build_overlays: chapter-card -------------------------------------------


def test_chapter_card_has_correct_timing_and_holds_for_chapter_card_seconds():
    document = _document(60.0, [_chapter("1 Khali Kamre", 12.5)])

    overlays = build_overlays(document, [])

    assert len(overlays) == 1
    overlay = overlays[0]
    assert overlay.kind == "chapter-card"
    assert overlay.start == 12.5
    assert overlay.end == 12.5 + CHAPTER_CARD_SECONDS


def test_chapter_card_title_has_leading_number_stripped():
    document = _document(60.0, [_chapter("1 Khali Kamre", 12.5)])

    overlays = build_overlays(document, [])

    assert overlays[0].text == "Khali Kamre"
    assert overlays[0].detail == "1"


def test_chapter_card_near_end_is_clamped_to_duration_seconds():
    document = _document(20.0, [_chapter("3 Aakhri", 19.0)])

    overlays = build_overlays(document, [])

    assert len(overlays) == 1
    assert overlays[0].end == 20.0


def test_chapter_card_clamped_result_passes_check_overlays():
    document = _document(20.0, [_chapter("3 Aakhri", 19.0)])

    overlays = build_overlays(document, [])

    assert check_overlays(overlays, document) == []


# --- build_overlays: censor --------------------------------------------------


def test_censor_box_ends_at_containing_cuts_end():
    document = _document(30.0, [_censor("face-blur", 5.0)])
    cuts = [
        _cut(0, 0.0, 3.0),
        _cut(1, 3.0, 8.0),
        _cut(2, 8.0, 30.0),
    ]

    overlays = build_overlays(document, cuts)

    assert len(overlays) == 1
    overlay = overlays[0]
    assert overlay.kind == "censor"
    assert overlay.start == 5.0
    assert overlay.end == 8.0
    assert overlay.text == ""
    assert overlay.detail == "face-blur"


def test_censor_with_no_containing_cut_ends_one_second_later():
    document = _document(30.0, [_censor("face-blur", 5.0)])

    overlays = build_overlays(document, [])

    assert len(overlays) == 1
    assert overlays[0].end == 6.0


# --- build_overlays: keyword --------------------------------------------------


def test_keyword_spans_the_timing_of_the_named_word():
    document = _document(
        10.0,
        [_key("dead", 3.0, word_index=0)],
        words=[
            _word(0, "khali", 0.0, 0.5),
            _word(1, "dead", 0.6, 1.1),
            _word(2, "channel", 1.2, 1.6),
        ],
    )

    overlays = build_overlays(document, [])

    assert len(overlays) == 1
    overlay = overlays[0]
    assert overlay.kind == "keyword"
    assert overlay.start == 0.6
    assert overlay.end == 1.1
    assert overlay.text == "dead"


def test_keyword_matches_ignoring_case():
    document = _document(
        10.0,
        [_key("Dead", 3.0, word_index=0)],
        words=[_word(0, "dead", 0.6, 1.1)],
    )

    overlays = build_overlays(document, [])

    assert len(overlays) == 1
    assert overlays[0].start == 0.6
    assert overlays[0].end == 1.1


def test_keyword_matches_ignoring_trailing_comma():
    document = _document(
        10.0,
        [_key("comment", 3.0, word_index=0)],
        words=[_word(0, "comment,", 0.6, 1.1)],
    )

    overlays = build_overlays(document, [])

    assert len(overlays) == 1
    assert overlays[0].start == 0.6
    assert overlays[0].end == 1.1


def test_keyword_matches_ignoring_trailing_danda():
    document = _document(
        10.0,
        [_key("है", 3.0, word_index=0)],
        words=[_word(0, "है।", 0.6, 1.1)],
    )

    overlays = build_overlays(document, [])

    assert len(overlays) == 1
    assert overlays[0].start == 0.6
    assert overlays[0].end == 1.1


def test_keyword_scans_forward_from_word_index_skipping_earlier_occurrences():
    document = _document(
        10.0,
        [_key("dead", 3.0, word_index=2)],
        words=[
            _word(0, "dead", 0.0, 0.3),
            _word(1, "channel", 0.4, 0.8),
            _word(2, "dead", 1.0, 1.4),
            _word(3, "again", 1.5, 1.8),
        ],
    )

    overlays = build_overlays(document, [])

    assert len(overlays) == 1
    assert overlays[0].start == 1.0
    assert overlays[0].end == 1.4


def test_keyword_absent_produces_no_overlay_and_a_warning():
    document = _document(
        10.0,
        [_key("ghost", 3.0, word_index=0)],
        words=[_word(0, "khali", 0.0, 0.5)],
    )

    overlays = build_overlays(document, [])

    assert overlays == []
    findings = check_overlays(overlays, document)
    assert any(
        f.gate == "overlays" and f.severity == "warning" and "ghost" in f.message
        for f in findings
    )


def test_keyword_within_search_window_is_found():
    words = _words_with_target(total_words=15, target_index=10, target_word="channel")
    document = _document(10.0, [_key("channel", 3.0, word_index=0)], words=words)

    overlays = build_overlays(document, [])

    assert len(overlays) == 1
    assert overlays[0].kind == "keyword"
    assert overlays[0].start == words[10]["start"]
    assert overlays[0].end == words[10]["end"]


def test_keyword_at_markers_own_word_index_is_found_inclusive_at_near_end():
    marker_word_index = 5
    words = _words_with_target(total_words=10, target_index=marker_word_index, target_word="channel")
    document = _document(10.0, [_key("channel", 3.0, word_index=marker_word_index)], words=words)

    overlays = build_overlays(document, [])

    assert len(overlays) == 1
    assert overlays[0].start == words[marker_word_index]["start"]
    assert overlays[0].end == words[marker_word_index]["end"]


def test_keyword_at_exact_window_boundary_is_found_inclusive_at_far_end():
    marker_word_index = 0
    target_index = marker_word_index + KEYWORD_SEARCH_WINDOW_WORDS
    words = _words_with_target(total_words=target_index + 1, target_index=target_index, target_word="channel")
    document = _document(30.0, [_key("channel", 3.0, word_index=marker_word_index)], words=words)

    overlays = build_overlays(document, [])

    assert len(overlays) == 1
    assert overlays[0].start == words[target_index]["start"]
    assert overlays[0].end == words[target_index]["end"]


def test_keyword_one_word_beyond_window_produces_no_overlay():
    marker_word_index = 0
    target_index = marker_word_index + KEYWORD_SEARCH_WINDOW_WORDS + 1
    words = _words_with_target(total_words=target_index + 1, target_index=target_index, target_word="channel")
    document = _document(30.0, [_key("channel", 3.0, word_index=marker_word_index)], words=words)

    overlays = build_overlays(document, [])

    assert overlays == []


def test_keyword_one_word_beyond_window_produces_warning_naming_the_word():
    marker_word_index = 0
    target_index = marker_word_index + KEYWORD_SEARCH_WINDOW_WORDS + 1
    words = _words_with_target(total_words=target_index + 1, target_index=target_index, target_word="channel")
    document = _document(30.0, [_key("channel", 3.0, word_index=marker_word_index)], words=words)

    overlays = build_overlays(document, [])
    findings = check_overlays(overlays, document)

    assert any(
        f.gate == "overlays" and f.severity == "warning" and "channel" in f.message
        for f in findings
    )


def test_keyword_900_filler_words_away_yields_no_overlay_and_a_warning_not_a_distant_highlight():
    # Regression case: a [KEY:channel] marker at word_index=0 with the nearest
    # matching word 900 filler words later used to attach the overlay ~306s
    # (5.1 minutes) downstream with no error and no warning. It must now
    # produce neither an overlay nor a silent success -- just the same
    # missing-keyword warning as any other unmatched marker.
    marker_word_index = 0
    target_index = marker_word_index + 900
    words = _words_with_target(total_words=target_index + 1, target_index=target_index, target_word="channel")
    document = _document(300.0, [_key("channel", 0.0, word_index=marker_word_index)], words=words)

    overlays = build_overlays(document, [])

    assert overlays == []
    findings = check_overlays(overlays, document)
    assert any(
        f.gate == "overlays" and f.severity == "warning" and "channel" in f.message
        for f in findings
    )


# --- build_overlays: mixed / sorting -------------------------------------------


def test_output_is_sorted_by_start_ascending():
    document = _document(
        60.0,
        [
            _chapter("2 Second", 20.0),
            _chapter("1 First", 5.0),
        ],
    )

    overlays = build_overlays(document, [])

    assert [o.start for o in overlays] == [5.0, 20.0]


def test_ties_at_same_start_are_ordered_by_kind_and_deterministic_regardless_of_source_order():
    document_a = _document(
        30.0,
        [
            _chapter("1 First", 5.0),
            _censor("blur", 5.0),
        ],
        words=[],
    )
    document_b = _document(
        30.0,
        [
            _censor("blur", 5.0),
            _chapter("1 First", 5.0),
        ],
        words=[],
    )
    cuts = [_cut(0, 0.0, 30.0)]

    overlays_a = build_overlays(document_a, cuts)
    overlays_b = build_overlays(document_b, cuts)

    assert [o.kind for o in overlays_a] == ["censor", "chapter-card"]
    assert [o.kind for o in overlays_b] == ["censor", "chapter-card"]


def test_only_chapter_markers_produce_only_chapter_cards_no_special_casing_needed():
    # A calibration-style spine: chapters only, no censor or key markers at all.
    document = _document(
        60.0,
        [
            _chapter("1 Khali Kamre", 5.0),
            _chapter("2 Digital Kabristan", 20.0),
            _chapter("3 Jo Baaki Rehta Hai", 40.0),
        ],
    )

    overlays = build_overlays(document, [])

    assert len(overlays) == 3
    assert all(o.kind == "chapter-card" for o in overlays)
    assert check_overlays(overlays, document) == []


# --- check_overlays: each rule fires -------------------------------------------


def test_check_overlays_flags_end_at_or_before_start():
    document = _document(10.0, [])
    overlays = [_overlay("chapter-card", 5.0, 5.0, text="Title")]

    findings = check_overlays(overlays, document)

    assert any(f.gate == "overlays" and f.severity == "error" for f in findings)


def test_check_overlays_flags_end_past_duration():
    document = _document(10.0, [])
    overlays = [_overlay("censor", 5.0, 20.0, text="", detail="r")]

    findings = check_overlays(overlays, document)

    assert any(f.gate == "overlays" and f.severity == "error" for f in findings)


def test_check_overlays_flags_chapter_card_with_empty_text():
    document = _document(10.0, [])
    overlays = [_overlay("chapter-card", 1.0, 4.0, text="", detail="1")]

    findings = check_overlays(overlays, document)

    assert any(f.gate == "overlays" and f.severity == "error" for f in findings)


def test_check_overlays_flags_keyword_with_empty_text():
    document = _document(10.0, [])
    overlays = [_overlay("keyword", 1.0, 2.0, text="", detail="")]

    findings = check_overlays(overlays, document)

    assert any(f.gate == "overlays" and f.severity == "error" for f in findings)


def test_check_overlays_warns_on_key_marker_that_produced_no_overlay():
    document = _document(
        10.0,
        [_key("nowhere", 1.0, word_index=0)],
        words=[_word(0, "khali", 0.0, 0.5)],
    )

    findings = check_overlays([], document)

    assert any(f.gate == "overlays" and f.severity == "warning" for f in findings)


def test_check_overlays_clean_set_produces_no_findings():
    document = _document(
        30.0,
        [
            _chapter("1 First", 5.0),
            _censor("blur", 10.0),
            _key("dead", 15.0, word_index=0),
        ],
        words=[_word(0, "dead", 15.0, 15.4)],
    )
    cuts = [_cut(0, 0.0, 30.0)]

    overlays = build_overlays(document, cuts)

    assert check_overlays(overlays, document) == []


def test_empty_document_produces_no_overlays():
    assert build_overlays({}, []) == []


# --- keywords on a transliterated episode ---------------------------------


def _deva_document():
    """A spine whose words are Devanagari, as narration from the Devanagari
    edition produces, with a [KEY:] marker written in the romanized script."""
    words = ["पंद्रह", "May,", "दो", "हज़ार", "छब्बीस।", "एक", "कॉकरोच", "जैसा"]
    return {
        "duration_seconds": 8.0,
        "words": [
            {"index": i, "word": w, "start": float(i), "end": float(i) + 1.0}
            for i, w in enumerate(words)
        ],
        "markers": [
            {"kind": "KEY", "arg": "cockroach", "word_index": 5, "line": 1, "seconds": 5.0}
        ],
    }


ROMANIZED = ["pandrah", "May,", "do", "hazaar", "chhabbis.", "ek", "cockroach", "jaisa"]


def test_a_romanized_keyword_does_not_match_a_devanagari_spine():
    """The bug: [KEY:] args are written in the canonical romanized script while
    the spine's words come from the edition that was narrated. In one audit
    only 4 of 15 keywords resolved, and the four were the
    ones that stay Latin after transliteration."""
    assert missing_keywords(_deva_document()) == ["cockroach"]


def test_supplying_the_romanized_words_resolves_the_keyword():
    assert missing_keywords(_deva_document(), ROMANIZED) == []


def test_the_overlay_takes_its_timing_from_the_spine_not_the_romanized_text():
    """The romanized edition says WHICH word; the spine says WHEN. That is only
    valid because check_transliteration guarantees word n is the same word in
    both editions."""
    overlays = build_overlays(_deva_document(), [], ROMANIZED)
    keyword = [o for o in overlays if o.kind == "keyword"]
    assert len(keyword) == 1
    assert keyword[0].start == 6.0   # index 6 in the spine
    assert keyword[0].end == 7.0
    assert keyword[0].text == "cockroach"


def test_omitting_romanized_words_keeps_the_original_behaviour():
    """A script narrated from its canonical edition must still work unchanged."""
    document = {
        "duration_seconds": 4.0,
        "words": [
            {"index": i, "word": w, "start": float(i), "end": float(i) + 1.0}
            for i, w in enumerate(["ek", "cockroach", "jaisa", "naujawan"])
        ],
        "markers": [
            {"kind": "KEY", "arg": "cockroach", "word_index": 0, "line": 1, "seconds": 0.0}
        ],
    }
    assert missing_keywords(document) == []
    assert len(build_overlays(document, [])) == 1


def test_a_keyword_outside_the_window_still_fails_with_romanized_words():
    """The search window is the guard against a typo'd keyword latching onto a
    coincidental match minutes away; supplying romanized words must not widen it."""
    document = _deva_document()
    document["markers"][0]["word_index"] = 0
    long_romanized = ["filler"] * 200 + ["cockroach"]
    assert missing_keywords(document, long_romanized) == ["cockroach"]

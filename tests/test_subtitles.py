"""Part 1: subtitle cue grouping.

Word timings and markers use the same document shape as timing.py /
overlays.py / audiomix.py: `words` is a list of {index, word, start, end},
`markers` is a list of {kind, arg, word_index, line, seconds}.
"""

from __future__ import annotations

from rabbithole.subtitles import (
    DEVANAGARI_FONT_CANDIDATES,
    MAX_CUE_CHARS,
    MAX_CUE_SECONDS,
    MAX_CUE_WORDS,
    MIN_CUE_SECONDS,
    SubtitleCue,
    ass_colour,
    build_ass,
    format_ass_timestamp,
    group_cues,
    keyword_word_indices,
    pick_font,
    write_ass,
)


# --- fixtures ----------------------------------------------------------------


def _word(index, word, start, end):
    return {"index": index, "word": word, "start": start, "end": end}


def _key(arg, seconds, word_index=0, line=1):
    return {"kind": "KEY", "arg": arg, "word_index": word_index, "line": line, "seconds": seconds}


def _silence(seconds, length, word_index=0, line=1):
    return {"kind": "SILENCE", "arg": f"{length}s", "word_index": word_index, "line": line, "seconds": seconds}


def _document(duration, words, markers=None):
    return {
        "duration_seconds": duration,
        "word_count": len(words),
        "words": words,
        "markers": markers or [],
    }


def _sequential_words(words, word_dur=0.3, gap=0.05, start=0.0):
    """`words` (strings) laid back to back, each `word_dur` long with `gap` between."""
    out = []
    t = start
    for i, w in enumerate(words):
        out.append(_word(i, w, t, t + word_dur))
        t += word_dur + gap
    return out


# --- group_cues: basic grouping -----------------------------------------------


def test_one_short_sentence_yields_one_cue():
    words = _sequential_words(["यह", "एक", "sentence।"])
    document = _document(2.0, words)

    cues = group_cues(document)

    assert len(cues) == 1
    assert cues[0].index == 0
    assert cues[0].words == ("यह", "एक", "sentence।")
    assert cues[0].start == words[0]["start"]
    assert cues[0].end == words[-1]["end"]


def test_empty_document_yields_no_cues():
    assert group_cues(_document(0.0, [])) == []
    assert group_cues({}) == []


def test_long_paragraph_splits_on_char_limit():
    # 7-char words: joined length after n words is 8n - 1.
    # n=5 -> 39 chars (fits under MAX_CUE_CHARS=42); n=6 -> 47 (breaks).
    words_text = [f"word{i:03d}" for i in range(6)]
    assert all(len(w) == 7 for w in words_text)
    words = _sequential_words(words_text, word_dur=0.1, gap=0.02)
    document = _document(5.0, words)

    cues = group_cues(document)

    assert len(cues) == 2
    assert cues[0].words == tuple(words_text[:5])
    assert cues[1].words == tuple(words_text[5:])
    joined = " ".join(cues[0].words)
    assert len(joined) <= MAX_CUE_CHARS


def test_adding_next_word_over_max_cue_words_forces_a_break():
    words_text = [f"w{i}" for i in range(MAX_CUE_WORDS + 1)]
    words = _sequential_words(words_text, word_dur=0.05, gap=0.02)
    document = _document(5.0, words)

    cues = group_cues(document)

    assert len(cues) == 2
    assert len(cues[0].words) == MAX_CUE_WORDS
    assert len(cues[1].words) == 1


def test_adding_next_word_over_max_cue_seconds_forces_a_break():
    # Each word covers 1s with no gap; a 6th word would push the cue's span
    # (first start to that word's end) past MAX_CUE_SECONDS=5.0.
    words = [_word(i, f"w{i}", float(i), float(i) + 1.0) for i in range(6)]
    document = _document(10.0, words)

    cues = group_cues(document)

    assert len(cues) == 2
    assert len(cues[0].words) == 5
    assert len(cues[1].words) == 1


def test_danda_forces_a_break():
    words = _sequential_words(["पहला।", "दूसरा"])
    document = _document(2.0, words)

    cues = group_cues(document)

    assert len(cues) == 2
    assert cues[0].words == ("पहला।",)
    assert cues[1].words == ("दूसरा",)


def test_latin_sentence_punctuation_forces_a_break():
    words = _sequential_words(["Hello.", "World"])
    document = _document(2.0, words)

    cues = group_cues(document)

    assert len(cues) == 2
    assert cues[0].words == ("Hello.",)
    assert cues[1].words == ("World",)


def test_very_long_single_word_is_emitted_oversized_not_looped_forever():
    huge_word = "a" * (MAX_CUE_CHARS * 3)
    words = [_word(0, huge_word, 0.0, 1.0), _word(1, "next", 1.2, 1.5)]
    document = _document(3.0, words)

    cues = group_cues(document)

    assert len(cues) == 2
    assert cues[0].words == (huge_word,)
    assert cues[1].words == ("next",)


# --- group_cues: silence windows ----------------------------------------------


def test_cue_never_spans_a_silence_window():
    words = [
        _word(0, "पहले", 0.0, 1.0),
        _word(1, "बाद", 3.0, 3.5),
        _word(2, "में", 3.6, 4.1),
        _word(3, "आया", 4.2, 4.7),
    ]
    # window = [3.0 - 2.0, 3.0] = [1.0, 3.0], sitting exactly in the gap
    # between word 0's end (1.0) and word 1's start (3.0).
    markers = [_silence(seconds=3.0, length=2.0, word_index=1)]
    document = _document(5.0, words, markers)

    cues = group_cues(document)

    assert len(cues) == 2
    assert cues[0].words == ("पहले",)
    assert cues[1].words == ("बाद", "में", "आया")
    assert cues[0].end <= 1.0
    assert cues[1].start >= 3.0


# --- group_cues: minimum cue duration ------------------------------------------


def test_short_cue_is_extended_but_not_past_next_cue_start():
    words = [
        _word(0, "hi।", 0.0, 0.1),  # danda forces its own cue, very short
        _word(1, "there", 0.5, 0.8),
        _word(2, "friend", 0.85, 1.2),
    ]
    document = _document(3.0, words)

    cues = group_cues(document)

    assert len(cues) == 2
    assert cues[0].words == ("hi।",)
    # Raw end was 0.1s (too short); MIN_CUE_SECONDS is 0.8, so 0.0+0.8=0.8
    # would be wanted, but the next cue starts at 0.5 -- clamp there.
    assert cues[0].end == 0.5
    assert cues[0].end > 0.1  # actually extended, not left as-is
    assert cues[0].end <= cues[1].start  # never pushed into the next cue


def test_short_final_cue_is_extended_but_not_past_duration_seconds():
    words = [
        _word(0, "one।", 0.0, 0.5),  # danda forces a break before word 1
        _word(1, "two", 4.9, 5.0),  # last cue, very short
    ]
    document = _document(5.2, words)

    cues = group_cues(document)

    assert len(cues) == 2
    assert cues[1].words == ("two",)
    assert cues[1].end <= 5.2
    assert cues[1].end > 5.0


# --- group_cues: ordering invariants -------------------------------------------


def test_cues_are_sequential_time_ordered_and_never_overlap():
    words_text = (
        "यह एक लंबा paragraph है। इसमें कई sentences हैं, "
        "और कुछ बहुत लंबे शब्द भी हैं जो cue को तोड़ देंगे। "
        "आख़िर में यह ख़त्म होता है।"
    ).split()
    words = _sequential_words(words_text, word_dur=0.25, gap=0.05)
    document = _document(words[-1]["end"] + 1.0, words)

    cues = group_cues(document)

    assert len(cues) > 1
    for i, cue in enumerate(cues):
        assert cue.index == i
        assert cue.start <= cue.end
    for a, b in zip(cues, cues[1:]):
        assert a.end <= b.start


# --- keyword_word_indices / keyword_positions ----------------------------------


def test_keyword_word_indices_finds_the_named_word():
    words = _sequential_words(["देखो", "वह", "आदमी", "वहाँ", "है"])
    markers = [_key("आदमी", seconds=0.0, word_index=0)]
    document = _document(3.0, words, markers)

    assert keyword_word_indices(document) == {2}


def test_keyword_word_indices_empty_set_when_no_key_markers():
    words = _sequential_words(["देखो", "वह", "आदमी"])
    document = _document(3.0, words, markers=[])

    assert keyword_word_indices(document) == set()


def test_keyword_positions_marks_right_position_within_its_cue():
    words = _sequential_words(["देखो", "वह", "आदमी", "वहाँ", "है।"])
    markers = [_key("आदमी", seconds=0.0, word_index=0)]
    document = _document(3.0, words, markers)

    cues = group_cues(document)

    assert len(cues) == 1
    assert cues[0].keyword_positions == (2,)


def test_cue_with_no_keyword_has_empty_keyword_positions():
    words = _sequential_words(["देखो", "वह", "आदमी", "वहाँ", "है।"])
    document = _document(3.0, words)

    cues = group_cues(document)

    assert cues[0].keyword_positions == ()


def test_romanized_keyword_matches_devanagari_spine_at_the_same_word_index():
    words = [
        _word(0, "देखो", 4.0, 4.3),
        _word(1, "वह", 4.4, 4.6),
        _word(2, "आदमी", 6.0, 6.7),
        _word(3, "वहाँ", 6.8, 7.1),
        _word(4, "है।", 7.2, 7.5),
    ]
    markers = [_key("aadmi", seconds=4.0, word_index=0)]
    document = _document(8.0, words, markers)
    romanized_words = ["dekho", "woh", "aadmi", "wahan", "hai."]

    assert keyword_word_indices(document, romanized_words) == {2}

    cues = group_cues(document, romanized_words)
    cue = next(cue for cue in cues if "आदमी" in cue.words)
    assert cue.words == ("देखो", "वह", "आदमी", "वहाँ", "है।")
    assert cue.start == 4.0
    assert cue.end == 7.5
    assert cue.keyword_positions == (2,)


def test_romanized_keyword_lookup_uses_global_indices_in_a_segment_document():
    """Segment timing keeps global word indices but contains only a word slice."""
    words = [
        _word(40, "पहले", 0.0, 0.3),
        _word(41, "आदमी", 0.4, 0.8),
        _word(42, "यहाँ।", 0.9, 1.2),
    ]
    markers = [_key("aadmi", seconds=0.0, word_index=40)]
    document = _document(1.5, words, markers)
    romanized_words = ["filler"] * 40 + ["pehle", "aadmi", "yahan."]

    cues = group_cues(document, romanized_words)

    assert len(cues) == 1
    assert cues[0].words == ("पहले", "आदमी", "यहाँ।")
    assert cues[0].keyword_positions == (1,)


# =============================================================================
# Part 2: ASS generation
# =============================================================================

TYPOGRAPHY = {
    "subtitle": {
        "family": "Inter",
        "weight": 600,
        "placement": "lower-third-center",
        "fill": "#E0E0E0",
        "keyword_fill": "#FFFF00",
        "shadow": "0 2px 6px rgba(0,0,0,0.9)",
    }
}


def _cue(index, start, end, words, keyword_positions=()):
    return SubtitleCue(
        index=index, start=start, end=end, words=tuple(words), keyword_positions=tuple(keyword_positions)
    )


# --- ass_colour ----------------------------------------------------------------


def test_ass_colour_white():
    assert ass_colour("#FFFFFF") == "&H00FFFFFF"


def test_ass_colour_yellow_swaps_r_and_b():
    # The exact worked example from the spec: #FFFF00 (R=FF,G=FF,B=00) packs
    # as alpha(00) + B(00) + G(FF) + R(FF) = &H0000FFFF, not &H00FFFF00.
    assert ass_colour("#FFFF00") == "&H0000FFFF"


def test_ass_colour_mixed_value_is_not_symmetric():
    # An asymmetric colour catches a swap that a same-in-both-orders colour
    # (like white or black) could never catch.
    assert ass_colour("#1A2B3C") == "&H003C2B1A"


def test_ass_colour_accepts_lowercase_hex():
    assert ass_colour("#ffff00") == "&H0000FFFF"


# --- pick_font -------------------------------------------------------------


def test_pick_font_returns_a_real_available_font_and_warns_about_override():
    # Real system detection, not mocked: typography.json's family is "Inter",
    # which has no Devanagari coverage, so this must be overridden by
    # whichever DEVANAGARI_FONT_CANDIDATES entry is actually installed here.
    font, findings = pick_font(TYPOGRAPHY)

    assert font in DEVANAGARI_FONT_CANDIDATES
    assert any(f.severity == "warning" for f in findings)
    assert any("Inter" in f.message and font in f.message for f in findings)


def test_pick_font_falls_back_to_sans_serif_when_nothing_available():
    font, findings = pick_font(TYPOGRAPHY, available=set())

    assert font == "sans-serif"
    assert any(f.severity == "error" for f in findings)


def test_pick_font_no_warning_when_requested_family_already_available():
    typography = {"subtitle": {"family": "Nirmala UI"}}

    font, findings = pick_font(typography, available={"Nirmala UI"})

    assert font == "Nirmala UI"
    assert findings == []


# --- build_ass: structure ---------------------------------------------------


def _two_cues():
    return [
        _cue(0, 0.0, 1.0, ["देखो", "वह"]),
        _cue(1, 1.0, 2.0, ["आदमी", "वहाँ"], keyword_positions=(0,)),
    ]


def test_build_ass_contains_required_sections_and_font():
    font, _ = pick_font(TYPOGRAPHY)
    ass = build_ass(_two_cues(), TYPOGRAPHY)

    assert "[Script Info]" in ass
    assert "[V4+ Styles]" in ass
    assert "[Events]" in ass
    assert font in ass
    assert ass.count("Dialogue:") == 2


def test_build_ass_playres_matches_requested_dimensions():
    ass = build_ass(_two_cues(), TYPOGRAPHY, width=640, height=360)

    assert "PlayResX: 640" in ass
    assert "PlayResY: 360" in ass


def test_build_ass_default_dimensions_are_1920x1080():
    ass = build_ass(_two_cues(), TYPOGRAPHY)

    assert "PlayResX: 1920" in ass
    assert "PlayResY: 1080" in ass


# --- build_ass: keyword override ---------------------------------------------


def test_keyword_cue_wraps_only_the_keyword_word():
    ass = build_ass([_cue(0, 0.0, 1.0, ["वह", "आदमी", "वहाँ"], keyword_positions=(1,))], TYPOGRAPHY)

    dialogue_line = next(line for line in ass.splitlines() if line.startswith("Dialogue:"))

    # The keyword word is preceded by a colour-override tag and followed by
    # one resetting back to fill colour; the other two words carry no tags.
    assert r"{\c" in dialogue_line
    assert dialogue_line.count(r"{\c") == 2  # one to enter keyword colour, one to reset
    # Non-keyword words appear as bare text, with no tag glued to them: "वह"
    # is followed directly by the opening tag, and "वहाँ" directly follows
    # the reset tag at the end of the line.
    assert "वह {" in dialogue_line
    assert dialogue_line.endswith("वहाँ")
    # The keyword word itself sits directly between an opening and the reset tag.
    keyword_colour = ass_colour(TYPOGRAPHY["subtitle"]["keyword_fill"])
    assert f"{{\\c{keyword_colour}&}}आदमी{{\\c" in dialogue_line


def test_non_keyword_cue_has_no_override_tags():
    ass = build_ass([_cue(0, 0.0, 1.0, ["वह", "आदमी", "वहाँ"])], TYPOGRAPHY)

    dialogue_line = next(line for line in ass.splitlines() if line.startswith("Dialogue:"))

    assert r"{\c" not in dialogue_line
    assert dialogue_line.endswith("वह आदमी वहाँ")


# --- format_ass_timestamp ----------------------------------------------------


def test_format_ass_timestamp_zero():
    assert format_ass_timestamp(0.0) == "0:00:00.00"


def test_format_ass_timestamp_rounds_1_005_seconds():
    # 1.005 as an IEEE754 double is actually 1.00499999999999989... (strictly
    # below the halfway point), so correct rounding to the nearest centisecond
    # yields 1.00, not 1.01. Confirmed against Python's own round(1.005*100).
    assert format_ass_timestamp(1.005) == "0:00:01.00"


def test_format_ass_timestamp_rounds_3661_5_seconds_across_hour_and_minute():
    assert format_ass_timestamp(3661.5) == "1:01:01.50"


# --- write_ass ---------------------------------------------------------------


def test_write_ass_writes_utf8_and_is_readable_back(tmp_path):
    cues = _two_cues()
    out_path, findings = write_ass(cues, TYPOGRAPHY, tmp_path / "subs.ass")

    assert out_path.exists()
    text = out_path.read_text(encoding="utf-8")
    assert "[Script Info]" in text
    assert "देखो" in text
    assert isinstance(findings, list)


# --- adversarial: ASS special characters in word text -------------------------


def test_word_containing_ass_braces_and_backslash_cannot_inject_an_override():
    # A word literally containing '{', '}', or a backslash-letter sequence
    # ('\N' is a hard line break even in plain, non-override ASS text) must
    # not be able to open/close an override block or inject a directive --
    # only the two override tags this module itself emits for a keyword
    # should ever appear as '{' / '}' in the output.
    dangerous_word = "safe{\\c&H000000FF&}danger}\\N"
    ass = build_ass([_cue(0, 0.0, 1.0, [dangerous_word])], TYPOGRAPHY)

    dialogue_line = next(line for line in ass.splitlines() if line.startswith("Dialogue:"))

    assert "{" not in dialogue_line
    assert "}" not in dialogue_line
    assert "\\N" not in dialogue_line
    assert "\\c" not in dialogue_line

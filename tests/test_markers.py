from rabbithole.markers import Marker, parse, word_count


def test_parse_extracts_marker_kind_and_arg():
    parsed = parse("[SFX:vhs-burst] Yahan se kahani shuru hoti hai.")

    assert len(parsed.markers) == 1
    m = parsed.markers[0]
    assert m.kind == "SFX"
    assert m.arg == "vhs-burst"
    assert m.raw == "[SFX:vhs-burst]"


def test_parse_removes_markers_from_narration_text():
    parsed = parse("[SFX:vhs-burst] Yahan se kahani shuru hoti hai.")

    assert parsed.text == "Yahan se kahani shuru hoti hai."


def test_parse_records_word_index_of_each_marker():
    source = "Ek account appear hota hai. [SILENCE:1.5s] Koi subscriber nahi tha."
    parsed = parse(source)

    assert parsed.markers[0].word_index == 5


def test_parse_records_one_indexed_line_numbers():
    source = "Pehli line.\n[REHOOK]\nDusri line."
    parsed = parse(source)

    assert parsed.markers[0].line == 2


def test_parse_handles_argless_marker():
    parsed = parse("[REHOOK] Lekin yahan se kahani sinister ho jaati hai.")

    assert parsed.markers[0].kind == "REHOOK"
    assert parsed.markers[0].arg == ""


def test_parse_collapses_whitespace_left_by_removal():
    parsed = parse("Do   spaces [SFX:sting]  aur phir.")

    assert parsed.text == "Do spaces aur phir."


def test_parse_preserves_paragraph_breaks():
    parsed = parse("Pehla para.\n\n[REHOOK]\n\nDusra para.")

    assert parsed.text == "Pehla para.\n\nDusra para."


def test_parse_ignores_unknown_bracket_text():
    parsed = parse("[NOTE:not a marker] Yeh text rehna chahiye.")

    assert parsed.markers == ()
    assert parsed.text == "[NOTE:not a marker] Yeh text rehna chahiye."


def test_word_count_counts_narration_only():
    parsed = parse("[ACT:1 Cold Open] Ek do teen chaar.")

    assert word_count(parsed) == 4


def test_marker_is_hashable_and_frozen():
    m = Marker(kind="REHOOK", arg="", line=1, word_index=0, raw="[REHOOK]")

    assert {m}


def test_word_index_agrees_with_text_when_marker_is_glued_to_a_word():
    parsed = parse("abc[KEY:x]def ghi")

    assert parsed.text == "abc def ghi"
    assert parsed.markers[0].word_index == 1
    assert parsed.text.split()[parsed.markers[0].word_index] == "def"


def test_word_index_agrees_with_text_when_marker_follows_punctuation():
    parsed = parse("Pehla hissa.[SILENCE:1.5s]Dusra hissa.")

    assert parsed.markers[0].word_index == 2
    assert " ".join(parsed.text.split()[:2]) == "Pehla hissa."


def test_normalize_converts_crlf_line_endings():
    parsed = parse("Line one.\r\n[REHOOK]\r\nLine two.")

    assert "\r" not in parsed.text
    # The REHOOK marker occupies its own source line (one newline before it, one
    # after), and removing a marker that owns a whole line leaves those two
    # newlines adjacent -- the same paragraph-break collapse that already
    # applies to a bare "\n[MARKER]\n" (see test_parse_preserves_paragraph_breaks
    # for the double-newline case). This assertion is about CRLF specifically:
    # no raw "\r" should survive, and the result must match the LF-only behavior.
    assert parsed.text == "Line one.\n\nLine two."


def test_unclosed_marker_does_not_swallow_later_lines():
    source = "Intro line.\n[MUSIC:swelling strings\nAsli narration yahan hai.\nAakhri line."
    parsed = parse(source)

    assert parsed.markers == ()
    assert "Asli narration yahan hai." in parsed.text
    assert "Aakhri line." in parsed.text


def test_markers_of_filters_by_kind_and_preserves_source_order():
    from rabbithole.markers import markers_of

    parsed = parse("[ACT:1 A] ek [REHOOK] do [ACT:2 B] teen [REHOOK] chaar")

    acts = markers_of(parsed, "ACT")
    assert [m.arg for m in acts] == ["1 A", "2 B"]
    assert markers_of(parsed, "CENSOR") == ()

from rabbithole.markers import parse
from rabbithole.validate import check_transliteration


ROMAN = "[ACT:1 Cold Open] [SFX:vhs-burst] Uske paas koi subscriber nahi tha. [SILENCE:1.5s] Kuch bhi nahi."
DEVA = "[ACT:1 Cold Open] [SFX:vhs-burst] उसके पास कोई सब्सक्राइबर नहीं था। [SILENCE:1.5s] कुछ भी नहीं।"


def test_faithful_transliteration_passes():
    assert check_transliteration(parse(ROMAN), parse(DEVA)) == []


def test_missing_devanagari_edition_is_flagged():
    findings = check_transliteration(parse(ROMAN), None)

    assert [f.gate for f in findings] == ["transliteration"]
    assert "05-devanagari" in findings[0].message


def test_word_count_mismatch_is_flagged():
    short = "[ACT:1 Cold Open] [SFX:vhs-burst] उसके पास। [SILENCE:1.5s] कुछ भी नहीं।"

    findings = check_transliteration(parse(ROMAN), parse(short))

    assert any("word count" in f.message.lower() for f in findings)


def test_marker_count_mismatch_is_flagged():
    missing_marker = "[ACT:1 Cold Open] उसके पास कोई सब्सक्राइबर नहीं था। [SILENCE:1.5s] कुछ भी नहीं।"

    findings = check_transliteration(parse(ROMAN), parse(missing_marker))

    assert any("marker" in f.message.lower() for f in findings)


def test_marker_kind_mismatch_is_flagged():
    wrong_kind = "[ACT:1 Cold Open] [MUSIC:chasms] उसके पास कोई सब्सक्राइबर नहीं था। [SILENCE:1.5s] कुछ भी नहीं।"

    findings = check_transliteration(parse(ROMAN), parse(wrong_kind))

    assert any("SFX" in f.message and "MUSIC" in f.message for f in findings)


def test_marker_arg_mismatch_is_flagged():
    wrong_arg = "[ACT:1 Cold Open] [SFX:vhs-burst] उसके पास कोई सब्सक्राइबर नहीं था। [SILENCE:2.0s] कुछ भी नहीं।"

    findings = check_transliteration(parse(ROMAN), parse(wrong_arg))

    assert any("1.5s" in f.message and "2.0s" in f.message for f in findings)


def test_devanagari_edition_without_devanagari_script_is_flagged():
    # Someone copied 04-final.md to 05-devanagari.md without transliterating.
    findings = check_transliteration(parse(ROMAN), parse(ROMAN))

    assert any("no Devanagari" in f.message for f in findings)


def test_romanized_edition_containing_devanagari_is_flagged():
    # The canonical file must stay romanized or check_register silently no-ops.
    findings = check_transliteration(parse(DEVA), parse(DEVA))

    assert any("must stay romanized" in f.message for f in findings)


def test_english_loanwords_in_latin_do_not_trip_the_script_check():
    roman = "[ACT:1 Cold Open] October mein ek account appear hota hai."
    deva = "[ACT:1 Cold Open] अक्टूबर में एक account अपियर होता है।"

    assert check_transliteration(parse(roman), parse(deva)) == []

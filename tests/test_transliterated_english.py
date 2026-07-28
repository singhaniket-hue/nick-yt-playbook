import argparse

from rabbithole import cli
from rabbithole.markers import parse
from rabbithole.validate import TRANSLITERATED_ENGLISH, check_transliterated_english


def test_english_loanword_in_devanagari_warns():
    parsed = parse("Yeh ek वीडियो hai.")

    findings = check_transliterated_english(parsed)

    assert len(findings) == 1
    assert findings[0].severity == "warning"
    assert findings[0].gate == "transliterated_english"


def test_message_names_devanagari_term_and_latin_form():
    parsed = parse("Yeh ek वीडियो hai.")

    findings = check_transliterated_english(parsed)

    assert "वीडियो" in findings[0].message
    assert "video" in findings[0].message


def test_term_repeated_many_times_produces_one_finding():
    parsed = parse(" ".join(["वीडियो"] * 12))

    findings = check_transliterated_english(parsed)

    assert len(findings) == 1
    assert "वीडियो" in findings[0].message


def test_authors_example_sentence_produces_no_findings():
    # Hindi in Devanagari, English ('finally', 'silence') in Latin -- exactly
    # the target convention this check enforces.
    parsed = parse("रात हो चुकी है, घर में finally silence है।")

    assert check_transliterated_english(parsed) == []


def test_clean_devanagari_hindi_produces_no_findings():
    parsed = parse("उसके पास कोई नहीं था। कुछ भी नहीं।")

    assert check_transliterated_english(parsed) == []


def test_multiple_distinct_terms_produce_one_finding_each():
    parsed = parse(
        "उसके पास कोई सब्सक्राइबर नहीं था। उसका एक चैनल भी था और एक वीडियो भी।"
    )

    findings = check_transliterated_english(parsed)

    terms_flagged = {term for term in TRANSLITERATED_ENGLISH if term in " ".join(f.message for f in findings)}
    assert len(findings) == 3
    assert terms_flagged == {"सब्सक्राइबर", "चैनल", "वीडियो"}


def test_term_embedded_inside_longer_devanagari_word_does_not_false_positive():
    # 'वीडियोग्राफर' (videographer) contains 'वीडियो' as a substring but is a
    # distinct, genuinely Devanagari-spelled word -- it must not trip the check.
    parsed = parse("वह एक वीडियोग्राफर hai, jise sab jaante hain.")

    assert check_transliterated_english(parsed) == []


def test_term_at_start_and_end_of_string_is_still_detected():
    parsed = parse("वीडियो")

    findings = check_transliterated_english(parsed)

    assert len(findings) == 1


def test_check_runs_as_part_of_validating_a_devanagari_edition(tmp_path, capsys):
    # End-to-end: `rabbithole.cli validate` on a script with a 05-devanagari.md
    # sitting beside it must surface this warning without being asked to.
    script_dir = tmp_path / "project" / "script"
    script_dir.mkdir(parents=True)
    roman_path = script_dir / "04-final.md"
    deva_path = script_dir / "05-devanagari.md"

    roman_path.write_text("[ACT:1 Cold Open] Uske paas ek video tha.", encoding="utf-8")
    deva_path.write_text("[ACT:1 Cold Open] उसके पास एक वीडियो था।", encoding="utf-8")

    cli.cmd_validate(argparse.Namespace(script=str(roman_path), wpm=177))

    report = capsys.readouterr().out
    assert "[transliterated_english]" in report
    assert "[WARNING]" in report

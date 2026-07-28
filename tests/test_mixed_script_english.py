import argparse
import json

import pytest

from rabbithole import cli
from rabbithole.config import Config
from rabbithole.markers import parse
from rabbithole.validate import (
    LatinTermOccurrence,
    LatinTerms,
    check_transliteration,
    load_latin_terms,
)


def _strict(romanized: str, mixed: str, terms):
    return check_transliteration(
        parse(romanized),
        parse(mixed),
        latin_terms=terms,
        strict_latin_terms=True,
    )


def test_hindi_devanagari_and_declared_english_latin_passes():
    findings = _strict(
        "Raat ho chuki hai, ghar mein finally silence hai.",
        "रात हो चुकी है, घर में finally silence है।",
        ["finally", "silence"],
    )

    assert findings == []


def test_exact_occurrence_disambiguates_english_is_from_hindi_is():
    lexicon = LatinTerms(
        terms=frozenset({"quote", "English"}),
        occurrences=(LatinTermOccurrence(token="is", word_index=5),),
    )

    findings = _strict(
        "Is quote mein shabd is English hai.",
        "इस quote में शब्द is English है।",
        lexicon,
    )

    assert findings == []


def test_global_homograph_still_applies_to_every_canonical_match():
    findings = _strict(
        "Is quote mein shabd is English hai.",
        "इस quote में शब्द is English है।",
        ["is", "quote", "English"],
    )

    assert any(
        finding.gate == "mixed_script_english"
        and "'Is'" in finding.message
        and "spoken word 1" in finding.message
        for finding in findings
    )


def test_exact_occurrence_rendered_in_devanagari_is_a_hard_error():
    lexicon = LatinTerms(
        terms=frozenset({"quote", "English"}),
        occurrences=(LatinTermOccurrence(token="is", word_index=5),),
    )

    findings = _strict(
        "Is quote mein shabd is English hai.",
        "इस quote में शब्द इज़ English है।",
        lexicon,
    )

    assert any(
        finding.gate == "mixed_script_english"
        and finding.severity == "error"
        and "'is'" in finding.message
        and "'इज़'" in finding.message
        and "spoken word 5" in finding.message
        for finding in findings
    )


@pytest.mark.parametrize(
    ("latin", "devanagari"),
    [
        ("account", "अकाउंट"),
        ("finally", "फाइनली"),
        ("silence", "साइलेंस"),
    ],
)
def test_declared_english_rendered_in_devanagari_is_a_hard_error(
    latin, devanagari
):
    findings = _strict(
        f"Yeh {latin} hai.",
        f"यह {devanagari} है।",
        [latin],
    )

    english_findings = [
        finding
        for finding in findings
        if finding.gate == "mixed_script_english"
    ]
    assert len(english_findings) == 1
    assert english_findings[0].severity == "error"
    assert repr(latin) in english_findings[0].message
    assert repr(devanagari) in english_findings[0].message
    assert "same word position" in english_findings[0].message


def test_declared_english_preserves_canonical_case_and_ignores_sentence_punctuation():
    findings = _strict(
        "Yeh YouTube hai.",
        "यह YouTube है।",
        ["YouTube"],
    )

    assert findings == []


def test_changed_case_is_not_identically_spelled():
    findings = _strict(
        "Yeh YouTube hai.",
        "यह youtube है।",
        ["YouTube"],
    )

    assert any(
        finding.gate == "mixed_script_english"
        and finding.severity == "error"
        and "'YouTube'" in finding.message
        and "'youtube'" in finding.message
        for finding in findings
    )


def test_undeclared_latin_token_in_mixed_edition_fails_closed():
    findings = _strict(
        "Raat ho chuki hai.",
        "Raat हो चुकी है।",
        [],
    )

    assert any(
        finding.gate == "mixed_script_english"
        and finding.severity == "error"
        and "'Raat'" in finding.message
        and "not declared" in finding.message
        for finding in findings
    )


def test_repeated_undeclared_latin_token_produces_one_aggregated_finding():
    findings = _strict(
        "Raat raat raat ke baad ghar shaant hai.",
        "Raat Raat raat के बाद घर शांत है।",
        [],
    )

    undeclared = [
        finding
        for finding in findings
        if finding.gate == "mixed_script_english"
        and "'Raat'" in finding.message
        and "not declared" in finding.message
    ]
    assert len(undeclared) == 1
    assert "spoken word 1" in undeclared[0].message
    assert "3 occurrences" in undeclared[0].message


def test_declared_term_cannot_move_to_a_different_aligned_word():
    findings = _strict(
        "Yeh khaata hai.",
        "यह account है।",
        ["account"],
    )

    assert any(
        finding.gate == "mixed_script_english"
        and "'account'" in finding.message
        and "not declared for its canonical position" in finding.message
        for finding in findings
    )


def test_occurrence_outside_canonical_word_range_is_rejected():
    lexicon = LatinTerms(
        terms=frozenset(),
        occurrences=(LatinTermOccurrence(token="is", word_index=4),),
    )

    findings = _strict(
        "Yeh is hai.",
        "यह इस है।",
        lexicon,
    )

    assert any(
        finding.gate == "mixed_script_english"
        and "spoken word 4" in finding.message
        and "only 3 words" in finding.message
        for finding in findings
    )


def test_occurrence_must_match_canonical_token_and_case_exactly():
    lexicon = LatinTerms(
        terms=frozenset(),
        occurrences=(LatinTermOccurrence(token="Is", word_index=2),),
    )

    findings = _strict(
        "Yeh is hai.",
        "यह is है।",
        lexicon,
    )

    assert any(
        finding.gate == "mixed_script_english"
        and "names 'Is'" in finding.message
        and "canonical token there is 'is'" in finding.message
        for finding in findings
    )


def test_unused_global_terms_are_allowed():
    lexicon = LatinTerms(terms=frozenset({"account", "YouTube"}))

    findings = _strict(
        "Yeh khaata hai.",
        "यह खाता है।",
        lexicon,
    )

    assert findings == []


def test_strict_mode_without_a_lexicon_fails_closed():
    findings = _strict(
        "Yeh khaata hai.",
        "यह खाता है।",
        None,
    )

    assert any(
        finding.gate == "mixed_script_english"
        and finding.severity == "error"
        and "requires an explicit" in finding.message
        for finding in findings
    )


def test_default_mode_remains_backward_compatible():
    findings = check_transliteration(
        parse("Yeh account hai."),
        parse("यह अकाउंट है।"),
    )

    assert findings == []


def test_strict_checks_do_not_replace_word_or_marker_parity_gates():
    findings = check_transliteration(
        parse("[SFX:vhs-burst] Yeh account hai."),
        parse("[MUSIC:chasms] यह अकाउंट अभी है।"),
        latin_terms=["account"],
        strict_latin_terms=True,
    )

    messages = " ".join(finding.message for finding in findings)
    assert "Word count differs" in messages
    assert "SFX" in messages
    assert "MUSIC" in messages


def test_load_latin_terms_accepts_self_documenting_object(tmp_path):
    path = tmp_path / "latin-terms.json"
    path.write_text(
        json.dumps(
            {
                "terms": ["account", "finally", "silence", "YouTube"],
                "occurrences": [{"token": "is", "word_index": 123}],
            }
        ),
        encoding="utf-8",
    )

    lexicon, findings = load_latin_terms(path)

    assert findings == []
    assert lexicon is not None
    assert lexicon.terms == frozenset(
        {"account", "finally", "silence", "YouTube"}
    )
    assert lexicon.occurrences == (
        LatinTermOccurrence(token="is", word_index=123),
    )


def test_loaded_occurrence_drives_homograph_safe_strict_validation(tmp_path):
    path = tmp_path / "latin-terms.json"
    path.write_text(
        json.dumps(
            {
                "terms": ["quote", "English"],
                "occurrences": [{"token": "is", "word_index": 5}],
            }
        ),
        encoding="utf-8",
    )
    lexicon, load_findings = load_latin_terms(path)

    assert load_findings == []
    assert _strict(
        "Is quote mein shabd is English hai.",
        "इस quote में शब्द is English है।",
        lexicon,
    ) == []


def test_load_latin_terms_accepts_legacy_top_level_array(tmp_path):
    path = tmp_path / "latin-terms.json"
    path.write_text(json.dumps(["account"]), encoding="utf-8")

    lexicon, findings = load_latin_terms(path)

    assert findings == []
    assert lexicon == LatinTerms(terms=frozenset({"account"}))


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"terms": ["account"], "mode": "strict"}, "unknown field"),
        ({"wrong": ["account"]}, "unknown field"),
        (["account", "ACCOUNT"], "more than once"),
        (["123"], "no Latin letter"),
        (["अकाउंट"], "contains a Devanagari character"),
        (["accountअकाउंट"], "contains a Devanagari character"),
        (["aा"], "contains a Devanagari character"),
        (["video quality"], "whitespace"),
        (["account", 42], "must be a string"),
        ([""], "is empty"),
        (
            {"terms": [], "occurrences": "not-a-list"},
            "occurrences must contain a JSON array",
        ),
        (
            {"terms": [], "occurrences": ["is"]},
            "occurrences entry 0 must be an object",
        ),
        (
            {
                "terms": [],
                "occurrences": [
                    {"token": "is", "word_index": 2, "note": "quote"}
                ],
            },
            "unknown field",
        ),
        (
            {"terms": [], "occurrences": [{"token": "is"}]},
            "missing required field",
        ),
        (
            {
                "terms": [],
                "occurrences": [{"token": "इस", "word_index": 2}],
            },
            "contains a Devanagari character",
        ),
        (
            {
                "terms": [],
                "occurrences": [{"token": "is quote", "word_index": 2}],
            },
            "contains whitespace",
        ),
        (
            {
                "terms": [],
                "occurrences": [{"token": 42, "word_index": 2}],
            },
            "token must be a string",
        ),
        (
            {
                "terms": [],
                "occurrences": [
                    {"token": "is", "word_index": 2},
                    {"token": "account", "word_index": 2},
                ],
            },
            "duplicates spoken word 2",
        ),
    ],
)
def test_load_latin_terms_rejects_malformed_or_ambiguous_lexicons(
    tmp_path, payload, message
):
    path = tmp_path / "latin-terms.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    lexicon, findings = load_latin_terms(path)

    assert lexicon is None
    assert len(findings) == 1
    assert findings[0].gate == "mixed_script_english"
    assert findings[0].severity == "error"
    assert message in findings[0].message


@pytest.mark.parametrize("word_index", [0, -1, True, 2.5, "2"])
def test_load_latin_terms_rejects_invalid_occurrence_word_index(
    tmp_path, word_index
):
    path = tmp_path / "latin-terms.json"
    path.write_text(
        json.dumps(
            {
                "terms": [],
                "occurrences": [{"token": "is", "word_index": word_index}],
            }
        ),
        encoding="utf-8",
    )

    lexicon, findings = load_latin_terms(path)

    assert lexicon is None
    assert len(findings) == 1
    assert "positive 1-based integer" in findings[0].message


def test_load_latin_terms_missing_file_is_a_finding(tmp_path):
    lexicon, findings = load_latin_terms(tmp_path / "latin-terms.json")

    assert lexicon is None
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert "Missing strict English-token lexicon" in findings[0].message


def test_load_latin_terms_malformed_json_is_a_finding(tmp_path):
    path = tmp_path / "latin-terms.json"
    path.write_text('{"terms": [', encoding="utf-8")

    lexicon, findings = load_latin_terms(path)

    assert lexicon is None
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert "Cannot read strict English-token lexicon" in findings[0].message


def test_load_latin_terms_invalid_utf8_is_a_finding(tmp_path):
    path = tmp_path / "latin-terms.json"
    path.write_bytes(b"\xff")

    lexicon, findings = load_latin_terms(path)

    assert lexicon is None
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert "Cannot read strict English-token lexicon" in findings[0].message


@pytest.mark.parametrize(
    ("lexicon_source", "expected_gate"),
    [
        (
            json.dumps({"terms": ["account"]}),
            "mixed_script_english",
        ),
        (
            json.dumps({"terms": []}),
            "transliterated_english",
        ),
        (None, "mixed_script_english"),
        ('{"terms": [', "mixed_script_english"),
    ],
)
def test_narrate_force_cannot_bypass_the_pronunciation_contract(
    tmp_path, monkeypatch, capsys, lexicon_source, expected_gate
):
    project = tmp_path / "projects" / "episode"
    script_dir = project / "script"
    script_dir.mkdir(parents=True)
    roman_path = script_dir / "04-final.md"
    roman_path.write_text(
        "[ACT:1 Cold Open] Yeh account hai.",
        encoding="utf-8",
    )
    (script_dir / "05-devanagari.md").write_text(
        "[ACT:1 Cold Open] यह अकाउंट है।",
        encoding="utf-8",
    )
    if lexicon_source is not None:
        (script_dir / "latin-terms.json").write_text(
            lexicon_source,
            encoding="utf-8",
        )
    (project / "claims.json").write_text("[]", encoding="utf-8")
    (project / "brief.json").write_text(
        json.dumps({"target_duration_minutes": 1}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda: Config(
            elevenlabs_api_key="unused",
            voice_id="unused",
            model_id="eleven_multilingual_v2",
            wpm=177,
            episode_cap_usd=25.0,
            budget_mode="warn",
        ),
    )

    result = cli.cmd_narrate(
        argparse.Namespace(
            script=str(roman_path),
            out=str(project / "narration" / "vo.wav"),
            dry_run=True,
            force=True,
            wpm=None,
        )
    )

    report = capsys.readouterr().out
    assert result == 1
    assert f"[{expected_gate}] [ERROR]" in report
    assert "--force cannot bypass this safety gate" in report
    assert not (project / "narration" / "vo.wav").exists()


def test_lower_stage_script_keeps_legacy_non_strict_validation(tmp_path):
    findings = cli._mixed_script_findings(
        tmp_path / "script" / "03-draft.md",
        parse("Yeh khaata hai."),
        parse("यह खाता है।"),
    )

    assert findings == []

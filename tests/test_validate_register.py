import pytest

from rabbithole.markers import parse
from rabbithole.validate import check_register


@pytest.mark.parametrize(
    "token",
    ["tum", "tumhara", "tumhari", "tumhare", "tumhe", "tumne", "yaar", "bhai", "arre"],
)
def test_casual_tokens_are_flagged(token):
    parsed = parse(f"Aur {token} yahan par dekhiye.")

    findings = check_register(parsed)

    assert len(findings) == 1
    assert findings[0].gate == "register"
    assert token in findings[0].message


@pytest.mark.parametrize(
    "phrase", ["karte ho", "jaate ho", "dete ho", "chahte ho", "soch lo", "yaad rakho"]
)
def test_casual_phrases_are_flagged(phrase):
    parsed = parse(f"Aap jo {phrase} wahi hota hai.")

    assert [f.gate for f in check_register(parsed)] == ["register"]


@pytest.mark.parametrize("imperative", ["socho", "dekho", "suno", "samjho", "karoge"])
def test_casual_imperatives_are_flagged(imperative):
    parsed = parse(f"Ab {imperative} ki kya hua.")

    assert len(check_register(parsed)) == 1


def test_formal_register_passes_clean():
    parsed = parse(
        "Aap dekhiye ki kya hua. Aapko samajhna hoga ki log aisa kyun karte hain. "
        "Sochiye ek baar."
    )

    assert check_register(parsed) == []


def test_substring_matches_do_not_false_positive():
    # 'Batumi' contains 'tum'; 'bhaimaan' contains 'bhai'.
    parsed = parse("Batumi ek shehar hai aur bhaimaan alag shabd hai.")

    assert check_register(parsed) == []


def test_matching_is_case_insensitive():
    parsed = parse("Tumhara naam kya hai.")

    assert len(check_register(parsed)) == 1


def test_finding_reports_the_line_number():
    parsed = parse("Pehli line theek hai.\nDusri line mein tum hai.")

    assert check_register(parsed)[0].line == 2

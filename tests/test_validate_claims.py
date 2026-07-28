import json

import pytest

from rabbithole.markers import parse
from rabbithole.validate import check_claims, load_claims


def _ledger(tmp_path, records):
    path = tmp_path / "claims.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def test_load_claims_returns_empty_list_for_missing_file(tmp_path):
    assert load_claims(tmp_path / "nope.json") == []


def test_load_claims_reads_records(tmp_path):
    path = _ledger(tmp_path, [{"claim_id": "c1", "text": "x", "confidence": "documented", "sources": ["u"]}])

    assert len(load_claims(path)) == 1


def test_well_formed_ledger_passes():
    claims = [
        {"claim_id": "c1", "text": "A", "confidence": "documented", "sources": ["u1"]},
        {"claim_id": "c2", "text": "B", "confidence": "reported", "sources": ["u2"]},
    ]

    assert check_claims(parse("Koi bhi narration."), claims) == []


def test_missing_claim_id_is_flagged():
    claims = [{"claim_id": "", "text": "A", "confidence": "documented", "sources": ["u"]}]

    findings = check_claims(parse("Text."), claims)

    assert [f.gate for f in findings] == ["claims"]
    assert "claim_id" in findings[0].message


def test_duplicate_claim_ids_are_flagged():
    claims = [
        {"claim_id": "c1", "text": "A", "confidence": "documented", "sources": ["u"]},
        {"claim_id": "c1", "text": "B", "confidence": "documented", "sources": ["u"]},
    ]

    findings = check_claims(parse("Text."), claims)

    assert any("duplicate" in f.message.lower() for f in findings)


def test_unknown_confidence_tag_is_flagged():
    claims = [{"claim_id": "c1", "text": "A", "confidence": "vibes", "sources": ["u"]}]

    findings = check_claims(parse("Text."), claims)

    assert any("vibes" in f.message for f in findings)


def test_documented_claim_without_sources_is_flagged():
    claims = [{"claim_id": "c1", "text": "A", "confidence": "documented", "sources": []}]

    findings = check_claims(parse("Text."), claims)

    assert any("source" in f.message.lower() for f in findings)


def test_reported_claim_without_sources_is_flagged():
    claims = [{"claim_id": "c1", "text": "A", "confidence": "reported", "sources": []}]

    assert any("source" in f.message.lower() for f in check_claims(parse("Text."), claims))


def test_alleged_claim_needs_attributed_to():
    claims = [{"claim_id": "c1", "text": "A", "confidence": "alleged", "sources": []}]

    findings = check_claims(parse("Police ke mutabik yeh hua."), claims)

    assert any("attributed_to" in f.message for f in findings)


def test_alleged_claim_with_attributed_to_and_attribution_phrase_passes():
    claims = [
        {"claim_id": "c1", "text": "A", "confidence": "alleged", "sources": [], "attributed_to": "Delhi Police"}
    ]

    assert check_claims(parse("Police ke mutabik yeh hua."), claims) == []


def test_speculation_needs_no_sources_or_attributed_to():
    claims = [{"claim_id": "c1", "text": "A", "confidence": "speculation", "sources": []}]

    assert check_claims(parse("Report ke anusaar shayad yeh hua."), claims) == []


def test_unattributed_script_is_flagged_when_ledger_has_alleged_claims():
    claims = [
        {"claim_id": "c1", "text": "A", "confidence": "alleged", "sources": [], "attributed_to": "X"},
        {"claim_id": "c2", "text": "B", "confidence": "alleged", "sources": [], "attributed_to": "Y"},
    ]

    findings = check_claims(parse("Unhone yeh kiya. Woh doshi hain."), claims)

    assert any("attribution" in f.message.lower() for f in findings)


def test_attribution_count_must_cover_every_alleged_claim():
    claims = [
        {"claim_id": "c1", "text": "A", "confidence": "alleged", "sources": [], "attributed_to": "X"},
        {"claim_id": "c2", "text": "B", "confidence": "alleged", "sources": [], "attributed_to": "Y"},
    ]

    # Only one attribution phrase for two alleged claims.
    findings = check_claims(parse("Police ke mutabik yeh hua. Woh doshi hain."), claims)

    assert any("attribution" in f.message.lower() for f in findings)


def test_empty_ledger_with_plain_script_passes():
    assert check_claims(parse("Bilkul saadharan narration."), []) == []

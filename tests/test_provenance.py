import json

from rabbithole.provenance import (
    AssetRecord,
    add_record,
    check_provenance,
    load_provenance,
    save_provenance,
)


def _record(asset_id, **overrides):
    defaults = dict(
        asset_id=asset_id,
        tier="archival",
        provider="wayback",
        original_url="https://example.com/img.jpg",
        license="public-domain",
        retrieved_at="2026-07-01T00:00:00Z",
        local_path=f"/assets/{asset_id}.jpg",
        used_in_slots=(),
        notes="",
    )
    defaults.update(overrides)
    return AssetRecord(**defaults)


# --- load_provenance / save_provenance -----------------------------------


def test_missing_ledger_file_loads_as_empty_list(tmp_path):
    path = tmp_path / "provenance.json"

    assert load_provenance(path) == []


def test_save_then_load_round_trips_records_faithfully(tmp_path):
    path = tmp_path / "provenance.json"
    record = _record("a001", used_in_slots=("s001", "s002"))

    save_provenance(path, [record])
    loaded = load_provenance(path)

    assert loaded == [record]
    assert isinstance(loaded[0].used_in_slots, tuple)


# --- add_record ------------------------------------------------------------


def test_add_record_appends_without_mutating_original():
    original = [_record("a001")]
    new_record = _record("a002")

    result = add_record(original, new_record)

    assert result == [_record("a001"), _record("a002")]
    assert original == [_record("a001")]


def test_add_record_raises_on_duplicate_asset_id():
    original = [_record("a001")]
    duplicate = _record("a001", provider="different-provider")

    try:
        add_record(original, duplicate)
        assert False, "expected ValueError"
    except ValueError:
        pass


# --- check_provenance --------------------------------------------------


def test_well_formed_ledger_covering_every_slot_passes_clean():
    records = [
        _record("a001", used_in_slots=("s001",)),
        _record("a002", used_in_slots=("s002",)),
    ]

    assert check_provenance(records, ["s001", "s002"]) == []


def test_uncovered_slot_is_flagged():
    records = [_record("a001", used_in_slots=("s001",))]

    findings = check_provenance(records, ["s001", "s002"])

    assert any(f.gate == "provenance" and "s002" in f.message for f in findings)


def test_slot_claimed_by_two_assets_is_flagged():
    records = [
        _record("a001", used_in_slots=("s001",)),
        _record("a002", used_in_slots=("s001",)),
    ]

    findings = check_provenance(records, ["s001"])

    assert any(f.gate == "provenance" and "s001" in f.message for f in findings)


def test_unknown_tier_is_flagged():
    records = [_record("a001", tier="stock-footage", used_in_slots=("s001",))]

    findings = check_provenance(records, ["s001"])

    assert any(f.gate == "provenance" and "stock-footage" in f.message for f in findings)


def test_archival_asset_with_no_licence_is_flagged():
    records = [_record("a001", license="", used_in_slots=("s001",))]

    findings = check_provenance(records, ["s001"])

    assert any(f.gate == "provenance" and "a001" in f.message for f in findings)


def test_atmospheric_asset_with_no_url_and_no_licence_passes():
    records = [
        _record(
            "a001",
            tier="atmospheric",
            original_url="",
            license="",
            used_in_slots=("s001",),
        )
    ]

    assert check_provenance(records, ["s001"]) == []


def test_atmospheric_asset_that_claims_a_url_is_flagged():
    records = [
        _record(
            "a001",
            tier="atmospheric",
            original_url="https://example.com/generated.jpg",
            license="",
            used_in_slots=("s001",),
        )
    ]

    findings = check_provenance(records, ["s001"])

    assert any(f.gate == "provenance" and "a001" in f.message for f in findings)


def test_illustrative_stock_requires_and_accepts_source_and_licence():
    record = _record(
        "stock-001",
        tier="illustrative",
        provider="Mixkit",
        original_url="https://mixkit.co/free-stock-video/example/",
        license="Mixkit Stock Video Free License",
        used_in_slots=("s001",),
    )

    assert check_provenance([record], ["s001"]) == []

    findings = check_provenance(
        [record.__class__(**{**record.__dict__, "license": ""})], ["s001"]
    )
    assert any("missing original_url or license" in finding.message for finding in findings)


def test_asset_claiming_unknown_slot_id_is_flagged():
    records = [_record("a001", used_in_slots=("s999",))]

    findings = check_provenance(records, ["s001"])

    assert any(f.gate == "provenance" and "s999" in f.message for f in findings)


def test_same_asset_in_two_non_adjacent_slots_passes():
    records = [
        _record("a001", used_in_slots=("s001", "s003")),
        _record("a002", used_in_slots=("s002",)),
    ]

    findings = check_provenance(records, ["s001", "s002", "s003"])

    assert findings == []


def test_same_asset_in_two_adjacent_slots_is_flagged():
    records = [_record("a001", used_in_slots=("s001", "s002"))]

    findings = check_provenance(records, ["s001", "s002"])

    assert len(findings) == 1
    assert findings[0].gate == "provenance"
    assert "adjacent" in findings[0].message

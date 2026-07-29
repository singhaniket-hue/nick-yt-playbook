import json
from datetime import datetime, timezone
from pathlib import Path

import rabbithole.provenance as provenance_module
from rabbithole.provenance import (
    AssetRecord,
    add_record,
    apply_asset_refresh,
    check_provenance,
    load_provenance,
    plan_asset_refresh,
    resumable_refresh_id,
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


def test_save_replaces_the_ledger_only_after_complete_json_is_on_disk(
    tmp_path, monkeypatch
):
    path = tmp_path / "provenance.json"
    path.write_text('[{"old": true}]', encoding="utf-8")
    real_replace = provenance_module.os.replace
    observed = {}

    def inspecting_replace(source, destination):
        observed["old"] = path.read_text(encoding="utf-8")
        observed["new"] = json.loads(
            provenance_module.Path(source).read_text(encoding="utf-8")
        )
        real_replace(source, destination)

    monkeypatch.setattr(provenance_module.os, "replace", inspecting_replace)
    record = _record("a001", used_in_slots=("s001",))

    save_provenance(path, [record])

    assert observed["old"] == '[{"old": true}]'
    assert observed["new"][0]["asset_id"] == "a001"
    assert load_provenance(path) == [record]
    assert list(tmp_path.glob(".provenance.json.*.tmp")) == []


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


# --- recoverable explicit refresh -----------------------------------------


REFRESH_NOW = datetime(2026, 7, 30, 12, 34, 56, tzinfo=timezone.utc)


def _project_record(project, asset_id, slots, filename=None, **overrides):
    filename = filename or f"{asset_id}.mp4"
    path = project / "assets" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"media for {asset_id}".encode())
    return _record(
        asset_id,
        local_path=path.relative_to(project).as_posix(),
        used_in_slots=tuple(slots),
        **overrides,
    )


def test_refresh_requires_every_slot_claimed_by_a_multi_slot_asset(tmp_path):
    project = tmp_path / "episode"
    record = _project_record(project, "shared", ("s001", "s002"))

    try:
        plan_asset_refresh([record], ["s001"], project, now=REFRESH_NOW)
        assert False, "expected refresh scope failure"
    except ValueError as exc:
        assert "s002" in str(exc)
        assert "every affected" in str(exc)

    assert (project / record.local_path).exists()


def test_refresh_plan_preserves_a_collision_free_unclaimed_audit_record(
    tmp_path,
):
    project = tmp_path / "episode"
    current = _project_record(project, "capture-s001", ("s001",))
    colliding = _record(
        "capture-s001--retired-20260730T123456000000Z",
        used_in_slots=(),
    )

    plan = plan_asset_refresh(
        [current, colliding], ["s001"], project, now=REFRESH_NOW
    )

    [retired] = plan.retired_records
    assert (
        retired.asset_id
        == "capture-s001--retired-20260730T123456000000Z-2"
    )
    assert retired.used_in_slots == ()
    assert not Path(retired.local_path).is_absolute()
    assert retired.local_path.startswith(
        "revisions/quarantine/assets/20260730T123456000000Z/"
    )
    assert "previous_asset_id='capture-s001'" in retired.notes
    assert "previous_slots='s001'" in retired.notes
    assert "[rabbithole-refresh id=" in retired.notes
    assert plan.preview_records[0].used_in_slots == ()
    assert plan.preview_records[0].local_path == current.local_path


def test_explicit_scope_retires_all_ambiguous_claimants_sharing_one_file(
    tmp_path,
):
    project = tmp_path / "episode"
    first = _project_record(
        project, "capture-a", ("s001",), filename="shared.mp4"
    )
    second = _record(
        "capture-b",
        local_path=first.local_path,
        used_in_slots=("s001",),
    )

    plan = plan_asset_refresh(
        [first, second], ["s001"], project, now=REFRESH_NOW
    )

    assert len(plan.retired_records) == 2
    assert len({record.asset_id for record in plan.retired_records}) == 2
    assert all(record.used_in_slots == () for record in plan.retired_records)
    assert len(plan.moves) == 1
    assert len({record.local_path for record in plan.retired_records}) == 1


def test_apply_refresh_moves_media_and_atomically_saves_retired_ledger(tmp_path):
    project = tmp_path / "episode"
    ledger = project / "provenance.json"
    current = _project_record(project, "capture-s001", ("s001",))
    save_provenance(ledger, [current])
    source = project / current.local_path
    original_bytes = source.read_bytes()
    plan = plan_asset_refresh(
        [current], ["s001"], project, now=REFRESH_NOW
    )

    updated = apply_asset_refresh(ledger, plan)

    [retired] = updated
    destination = project / retired.local_path
    assert not source.exists()
    assert destination.read_bytes() == original_bytes
    assert load_provenance(ledger) == updated
    assert retired.used_in_slots == ()


def test_refresh_rolls_media_back_when_atomic_ledger_save_fails(
    tmp_path, monkeypatch
):
    project = tmp_path / "episode"
    ledger = project / "provenance.json"
    current = _project_record(project, "capture-s001", ("s001",))
    save_provenance(ledger, [current])
    source = project / current.local_path
    plan = plan_asset_refresh(
        [current], ["s001"], project, now=REFRESH_NOW
    )

    def fail_save(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(provenance_module, "save_provenance", fail_save)

    try:
        apply_asset_refresh(ledger, plan)
        assert False, "expected ledger save failure"
    except OSError as exc:
        assert "disk full" in str(exc)

    assert source.exists()
    assert not plan.moves[0].destination.exists()
    assert load_provenance(ledger) == [current]


def test_exact_incomplete_refresh_is_resumable_but_completed_one_is_not(
    tmp_path,
):
    project = tmp_path / "episode"
    old = _project_record(project, "capture-s001", ("s001",))
    plan = plan_asset_refresh(
        [old], ["s001", "s002"], project, now=REFRESH_NOW
    )
    retired = plan.retired_records[0]
    current_s001 = _record(
        "capture-s001",
        local_path="assets/capture-s001.mp4",
        used_in_slots=("s001",),
    )

    assert (
        resumable_refresh_id(
            [retired, current_s001], ["s001", "s002"]
        )
        == plan.refresh_id
    )
    current_s002 = _record(
        "capture-s002",
        local_path="assets/capture-s002.mp4",
        used_in_slots=("s002",),
    )
    assert (
        resumable_refresh_id(
            [retired, current_s001, current_s002], ["s001", "s002"]
        )
        is None
    )


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

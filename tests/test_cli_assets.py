import pytest
from pathlib import Path

import rabbithole.cli as cli
from rabbithole.assets import Artifact
from rabbithole.provenance import AssetRecord, load_provenance, save_provenance
from rabbithole.slots import Slot
from rabbithole.validate import Finding


def test_assets_cli_accepts_repeatable_slots_and_a_bounded_batch(monkeypatch):
    observed = {}

    def fake_cmd(args):
        observed["slot"] = args.slot
        observed["batch_size"] = args.batch_size
        return 0

    monkeypatch.setattr(cli, "cmd_assets", fake_cmd)

    result = cli.main(
        [
            "assets",
            "projects/demo/narration/timing.json",
            "--slot",
            "s041",
            "--slot",
            "s042",
            "--batch-size",
            "12",
        ]
    )

    assert result == 0
    assert observed == {"slot": ["s041", "s042"], "batch_size": 12}


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_assets_cli_rejects_an_unbounded_or_invalid_batch_size(value):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "assets",
                "projects/demo/narration/timing.json",
                "--batch-size",
                value,
            ]
        )

    assert exc_info.value.code == 2


def test_assets_cli_exposes_refresh_and_requires_explicit_slots(monkeypatch):
    observed = {}

    def fake_cmd(args):
        observed["refresh"] = args.refresh
        observed["slot"] = args.slot
        return 0

    monkeypatch.setattr(cli, "cmd_assets", fake_cmd)

    assert (
        cli.main(
            [
                "assets",
                "projects/demo/narration/timing.json",
                "--refresh",
                "--slot",
                "s041",
            ]
        )
        == 0
    )
    assert observed == {"refresh": True, "slot": ["s041"]}


def test_refresh_without_slot_fails_before_timing_file_is_read(tmp_path):
    result = cli.main(
        [
            "assets",
            str(tmp_path / "missing" / "timing.json"),
            "--refresh",
        ]
    )

    assert result == 2


@pytest.mark.parametrize("extra", [["--tier", "primary"], ["--batch-size", "1"]])
def test_refresh_rejects_partial_scope_filters_before_file_io(tmp_path, extra):
    result = cli.main(
        [
            "assets",
            str(tmp_path / "missing" / "timing.json"),
            "--refresh",
            "--slot",
            "s001",
            *extra,
        ]
    )

    assert result == 2


def test_final_refresh_dry_run_validates_current_records_not_unclaimed_preview(
    tmp_path, monkeypatch
):
    project = tmp_path / "demo"
    timing = project / "narration" / "timing.json"
    timing.parent.mkdir(parents=True)
    timing.write_text("{}", encoding="utf-8")
    media = project / "assets" / "source.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"current source media")
    current = AssetRecord(
        asset_id="source-s001",
        tier="primary",
        provider="source",
        original_url="https://example.test/source",
        license="CC-BY-4.0",
        retrieved_at="2026-07-01T00:00:00Z",
        local_path="assets/source.mp4",
        used_in_slots=("s001",),
        notes="",
    )
    save_provenance(project / "provenance.json", [current])
    slots = [
        Slot(
            slot_id="s001",
            kind="capture",
            detail="original upload",
            start=0.0,
            end=1.0,
            queries=("original upload",),
            marker_word_index=0,
        )
    ]
    monkeypatch.setattr(cli, "build_slots", lambda _document: slots)
    monkeypatch.setattr(cli, "check_slots", lambda *_args: [])
    monkeypatch.setattr(cli, "load_claims", lambda *_args: [])
    monkeypatch.setattr(
        cli,
        "load_artifacts",
        lambda *_args: [
            Artifact(
                artifact_id="source-s001",
                url="https://example.test/source",
                slot_id="s001",
            )
        ],
    )
    monkeypatch.setattr(
        cli,
        "usable_provenance_records",
        lambda records, *_args, **_kwargs: (list(records), []),
    )

    result = cli.main(
        [
            "assets",
            str(timing),
            "--refresh",
            "--slot",
            "s001",
            "--dry-run",
            "--quality",
            "final",
        ]
    )

    assert result == 0
    assert load_provenance(project / "provenance.json") == [current]
    assert media.read_bytes() == b"current source media"


def test_failed_refresh_keeps_quarantine_and_rerun_resumes_missing_slot(
    tmp_path, monkeypatch
):
    project = tmp_path / "demo"
    timing = project / "narration" / "timing.json"
    timing.parent.mkdir(parents=True)
    timing.write_text("{}", encoding="utf-8")
    media = project / "assets" / "s001-plate.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"old current media")
    old = AssetRecord(
        asset_id="plate-s001",
        tier="atmospheric",
        provider="ffmpeg-lavfi",
        original_url="",
        license="",
        retrieved_at="2026-07-01T00:00:00Z",
        local_path="assets/s001-plate.mp4",
        used_in_slots=("s001",),
        notes="original",
    )
    save_provenance(project / "provenance.json", [old])
    slots = [
        Slot(
            slot_id="s001",
            kind="plate",
            detail="grain",
            start=0.0,
            end=1.0,
            queries=("plate grain",),
            marker_word_index=0,
        ),
        Slot(
            slot_id="s002",
            kind="plate",
            detail="grain",
            start=1.0,
            end=2.0,
            queries=("plate grain",),
            marker_word_index=1,
        ),
    ]
    monkeypatch.setattr(cli, "build_slots", lambda _document: slots)
    monkeypatch.setattr(cli, "check_slots", lambda *_args: [])
    monkeypatch.setattr(cli, "load_claims", lambda *_args: [])
    monkeypatch.setattr(cli, "load_artifacts", lambda *_args: [])
    monkeypatch.setattr(cli, "load_grade", lambda *_args: {})
    monkeypatch.setattr(cli, "check_source_quality", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cli, "check_provenance", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        cli,
        "usable_provenance_records",
        lambda records, *_args, **_kwargs: (list(records), []),
    )
    calls = []

    def fake_execute(items, _slots, out_dir, *, on_record, **_kwargs):
        calls.append([item.slot_id for item in items if item.action != "satisfied"])
        new_records = []
        for item in items:
            if item.action == "satisfied":
                continue
            if len(calls) == 1 and item.slot_id == "s002":
                return new_records, [
                    Finding(
                        gate="assets",
                        severity="error",
                        message="simulated regeneration failure",
                    )
                ]
            out_path = Path(out_dir) / f"{item.slot_id}-plate.mp4"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(f"new {item.slot_id}".encode())
            record = AssetRecord(
                asset_id=f"plate-{item.slot_id}",
                tier="atmospheric",
                provider="ffmpeg-lavfi",
                original_url="",
                license="",
                retrieved_at="2026-07-30T00:00:00Z",
                local_path=str(out_path),
                used_in_slots=(item.slot_id,),
                notes="replacement",
            )
            on_record(record)
            new_records.append(record)
        return new_records, []

    monkeypatch.setattr(cli, "execute_plan", fake_execute)
    command = [
        "assets",
        str(timing),
        "--refresh",
        "--slot",
        "s001",
        "--slot",
        "s002",
        "--quality",
        "animatic",
    ]

    first_result = cli.main(command)
    first_records = load_provenance(project / "provenance.json")
    quarantine = list(
        (project / "revisions" / "quarantine").rglob("*.mp4")
    )

    assert first_result == 1
    assert media.read_bytes() == b"new s001"
    assert len(quarantine) == 1
    assert quarantine[0].read_bytes() == b"old current media"
    assert len(first_records) == 2
    assert {record.used_in_slots for record in first_records} == {
        (),
        ("s001",),
    }

    second_result = cli.main(command)
    final_records = load_provenance(project / "provenance.json")

    assert second_result == 0
    assert len(
        list((project / "revisions" / "quarantine").rglob("*.mp4"))
    ) == 1
    assert sum("--retired-" in record.asset_id for record in final_records) == 1
    assert {
        slot
        for record in final_records
        for slot in record.used_in_slots
    } == {"s001", "s002"}
    assert calls == [["s001", "s002"], ["s002"]]

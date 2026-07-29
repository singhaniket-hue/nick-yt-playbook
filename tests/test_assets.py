import json
from pathlib import Path

import pytest

from rabbithole.assets import (
    ACTION_REASONS,
    KIND_TO_ACTION,
    KIND_TO_TIER,
    Artifact,
    PlanItem,
    artifact_bindings_by_slot,
    artifact_urls_by_slot,
    check_source_quality,
    evidence_metrics,
    execute_plan,
    format_plan,
    load_artifacts,
    plan_assets,
)
from rabbithole.provenance import AssetRecord, check_provenance
from rabbithole.slots import Slot

GRADE = {
    "lut": "style/luts/crowley-noir.cube",
    "grain_strength": 0.18,
    "vignette_strength": 0.35,
    "scanline_opacity": 0.12,
}


def _slot(slot_id, kind, detail="", start=0.0, end=1.0, queries=None, marker_word_index=0):
    return Slot(
        slot_id=slot_id,
        kind=kind,
        detail=detail,
        start=start,
        end=end,
        queries=tuple(queries) if queries is not None else (f"{kind} {detail}".strip(),),
        marker_word_index=marker_word_index,
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


def _artifact(artifact_id, url, slot_id="", **overrides):
    return Artifact(
        artifact_id=artifact_id,
        url=url,
        slot_id=slot_id,
        **overrides,
    )


def _transport_for(url_to_response):
    """Build a fake archives.Transport from {substring: (status, body)}."""

    def transport(url):
        for substring, response in url_to_response.items():
            if substring in url:
                return response
        raise AssertionError(f"no fake response registered for url: {url}")

    return transport


def _archive_org_body(docs):
    return json.dumps({"response": {"docs": docs}}).encode("utf-8")


def _metadata_body(files, identifier="id1"):
    return json.dumps({"files": files, "metadata": {"identifier": identifier}}).encode("utf-8")


def _wikimedia_body(pages):
    return json.dumps({"query": {"pages": pages}}).encode("utf-8")


# --- load_artifacts ----------------------------------------------------


def test_load_artifacts_missing_file_returns_empty_list(tmp_path):
    assert load_artifacts(tmp_path / "artifacts.json") == []


def test_load_artifacts_parses_the_rich_schema_with_every_field_populated(tmp_path):
    path = tmp_path / "artifacts.json"
    path.write_text(
        json.dumps(
            [
                {
                    "artifact_id": "a001",
                    "title": "Supreme Court cause list for the May 15 hearing",
                    "url": "https://api.sci.gov.in/jonew/cl/advance/2026-05-15/M_J.pdf",
                    "kind": "official_court_record",
                    "date": "2026-05-15",
                    "source_role": "primary",
                    "use": "Establish the actual case and hearing.",
                    "rights_note": "Official public record; retain attribution.",
                    "slot_id": "s001",
                    "acquisition_mode": "screenshot-only",
                    "max_use_seconds": 8.5,
                }
            ]
        ),
        encoding="utf-8",
    )

    result = load_artifacts(path)

    assert result == [
        Artifact(
            artifact_id="a001",
            title="Supreme Court cause list for the May 15 hearing",
            url="https://api.sci.gov.in/jonew/cl/advance/2026-05-15/M_J.pdf",
            kind="official_court_record",
            date="2026-05-15",
            source_role="primary",
            use="Establish the actual case and hearing.",
            rights_note="Official public record; retain attribution.",
            slot_id="s001",
            acquisition_mode="screenshot-only",
            max_use_seconds=8.5,
        )
    ]


def test_load_artifacts_ignores_unknown_extra_keys_rather_than_raising(tmp_path):
    path = tmp_path / "artifacts.json"
    path.write_text(
        json.dumps(
            [
                {
                    "artifact_id": "a001",
                    "url": "https://example.com/a",
                    "some_future_field": "the catalogue may grow fields",
                    "another_one": 42,
                }
            ]
        ),
        encoding="utf-8",
    )

    result = load_artifacts(path)

    assert result == [Artifact(artifact_id="a001", url="https://example.com/a")]


def test_load_artifacts_skips_an_entry_with_no_url(tmp_path):
    path = tmp_path / "artifacts.json"
    path.write_text(
        json.dumps([{"artifact_id": "a001"}, {"artifact_id": "a002", "url": "https://x.com/b"}]),
        encoding="utf-8",
    )

    result = load_artifacts(path)

    assert result == [Artifact(artifact_id="a002", url="https://x.com/b")]


def test_load_artifacts_skips_an_entry_with_no_artifact_id(tmp_path):
    path = tmp_path / "artifacts.json"
    path.write_text(
        json.dumps(
            [{"url": "https://x.com/a"}, {"artifact_id": "a002", "url": "https://x.com/b"}]
        ),
        encoding="utf-8",
    )

    result = load_artifacts(path)

    assert result == [Artifact(artifact_id="a002", url="https://x.com/b")]


def test_load_artifacts_raises_runtime_error_naming_the_path_for_a_json_object(tmp_path):
    path = tmp_path / "artifacts.json"
    path.write_text(json.dumps({"artifact_id": "a001", "url": "https://x.com/a"}), encoding="utf-8")

    with pytest.raises(RuntimeError) as exc_info:
        load_artifacts(path)

    assert str(path) in str(exc_info.value)


def test_load_artifacts_raises_runtime_error_naming_the_path_for_a_list_of_non_dicts(tmp_path):
    path = tmp_path / "artifacts.json"
    path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

    with pytest.raises(RuntimeError) as exc_info:
        load_artifacts(path)

    assert str(path) in str(exc_info.value)


def test_load_artifacts_on_the_synthetic_demo_catalogue():
    # The repository ships a small fictional catalogue so this integration
    # check never depends on a private or production episode workspace.
    path = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "demo-project"
        / "research"
        / "artifacts.json"
    )

    result = load_artifacts(path)

    assert len(result) == 3
    assert all(isinstance(a, Artifact) for a in result)
    assert all(a.url for a in result)
    assert len({a.artifact_id for a in result}) == len(result)
    bound = [a for a in result if a.slot_id]
    assert all(a.slot_id.startswith("s") for a in bound)
    assert len({a.slot_id for a in bound}) == len(bound), "two artifacts share a slot"


# --- artifact_urls_by_slot ----------------------------------------------


def test_artifact_urls_by_slot_returns_only_bound_artifacts():
    artifacts = [
        _artifact("a001", "https://example.com/a", slot_id="s001"),
        _artifact("a002", "https://example.com/b"),  # unbound
        _artifact("a003", "https://example.com/c", slot_id="s003"),
    ]

    result = artifact_urls_by_slot(artifacts)

    assert result == {
        "s001": "https://example.com/a",
        "s003": "https://example.com/c",
    }


def test_artifact_urls_by_slot_is_empty_when_none_are_bound():
    artifacts = [
        _artifact("a001", "https://example.com/a"),
        _artifact("a002", "https://example.com/b"),
    ]

    assert artifact_urls_by_slot(artifacts) == {}


def test_artifact_bindings_by_slot_preserves_complete_rights_metadata():
    artifact = _artifact(
        "a001",
        "https://example.com/a",
        slot_id="s001",
        title="Source title",
        date="2026-07-01",
        source_role="primary",
        rights_note="Screenshot only; credit on screen.",
        acquisition_mode="screenshot-only",
        max_use_seconds=4.0,
    )

    assert artifact_bindings_by_slot([artifact]) == {"s001": artifact}


# --- plan_assets: satisfied precedence --------------------------------


def test_plan_assets_marks_a_slot_already_in_the_ledger_as_satisfied():
    slot = _slot("s001", "archival")
    records = [_record("a001", used_in_slots=("s001",))]

    items = plan_assets([slot], records, [])

    assert items[0].action == "satisfied"
    assert "a001" in items[0].reason


def test_satisfied_takes_precedence_over_the_kinds_normal_action():
    # A capture slot with no artifact URL would normally be "blocked", but a
    # ledger record already claiming it must short-circuit to "satisfied"
    # before the missing-URL check ever runs.
    #
    # tier="primary" matches what a `capture` slot requires. The record's
    # default tier is "archival", which is now (correctly) reported as a stale
    # claim -- an incidental mismatch that was never what this test was about.
    slot = _slot("s001", "capture")
    records = [_record("a001", tier="primary", used_in_slots=("s001",))]

    items = plan_assets([slot], records, [])

    assert items[0].action == "satisfied"


# --- plan_assets: the five kinds ----------------------------------------

KIND_EXPECTATIONS = {
    "plate": ("atmospheric", "generate"),
    "archival": ("archival", "search"),
    "capture": ("primary", "fetch"),
    "screenshot": ("primary", "shoot"),
    "graphic": ("atmospheric", "draw"),
}


@pytest.mark.parametrize("kind", list(KIND_EXPECTATIONS))
def test_each_kind_produces_the_right_action_and_tier(kind):
    expected_tier, expected_action = KIND_EXPECTATIONS[kind]
    slot = _slot("s001", kind, detail="x")
    artifacts = (
        [_artifact("a001", "https://example.com/video", slot_id="s001")]
        if kind in ("capture", "screenshot")
        else []
    )

    items = plan_assets([slot], [], artifacts)

    assert items[0].tier == expected_tier
    assert items[0].action == expected_action


def test_kind_to_tier_and_kind_to_action_match_the_five_kind_table():
    assert set(KIND_TO_TIER) == set(KIND_EXPECTATIONS)
    assert set(KIND_TO_ACTION) == set(KIND_EXPECTATIONS)
    for kind, (tier, action) in KIND_EXPECTATIONS.items():
        assert KIND_TO_TIER[kind] == tier
        assert KIND_TO_ACTION[kind] == action


# --- plan_assets: capture gating ----------------------------------------


def test_capture_slot_with_empty_catalogue_is_blocked_saying_no_artifacts():
    slot = _slot("s001", "capture")

    items = plan_assets([slot], [], [])

    assert items[0].action == "blocked"
    assert "artifacts.json" in items[0].reason
    assert "no artifact" in items[0].reason.lower()


def test_capture_slot_with_unbound_catalogue_of_three_is_blocked_naming_count_and_slot_id():
    slot = _slot("s001", "capture")
    artifacts = [
        _artifact("a001", "https://example.com/1"),
        _artifact("a002", "https://example.com/2"),
        _artifact("a003", "https://example.com/3"),
    ]

    items = plan_assets([slot], [], artifacts)

    assert items[0].action == "blocked"
    assert "3" in items[0].reason
    assert "slot_id" in items[0].reason


def test_capture_slot_with_a_bound_artifact_is_fetch():
    slot = _slot("s001", "capture")
    artifacts = [_artifact("a001", "https://example.com/video", slot_id="s001")]

    items = plan_assets([slot], [], artifacts)

    assert items[0].action == "fetch"


def test_screenshot_only_artifact_blocks_a_capture_slot_before_fetch():
    slot = _slot("s001", "capture", start=0.0, end=4.0)
    artifact = _artifact(
        "a001",
        "https://example.com/video",
        slot_id="s001",
        acquisition_mode="screenshot-only",
    )

    item = plan_assets([slot], [], [artifact])[0]

    assert item.action == "blocked"
    assert "screenshot-only" in item.reason
    assert "fetch" in item.reason


def test_screenshot_only_artifact_allows_a_screenshot_slot():
    slot = _slot("s001", "screenshot", start=0.0, end=4.0)
    artifact = _artifact(
        "a001",
        "https://example.com/page",
        slot_id="s001",
        acquisition_mode="screenshot-only",
    )

    assert plan_assets([slot], [], [artifact])[0].action == "shoot"


def test_video_only_artifact_blocks_a_screenshot_slot():
    slot = _slot("s001", "screenshot", start=0.0, end=4.0)
    artifact = _artifact(
        "a001",
        "https://example.com/video",
        slot_id="s001",
        acquisition_mode="video-only",
    )

    item = plan_assets([slot], [], [artifact])[0]

    assert item.action == "blocked"
    assert "video-only" in item.reason
    assert "shoot" in item.reason


def test_unknown_acquisition_mode_fails_closed_at_planning_time():
    slot = _slot("s001", "capture", start=0.0, end=4.0)
    artifact = _artifact(
        "a001",
        "https://example.com/video",
        slot_id="s001",
        acquisition_mode="download-somehow",
    )

    item = plan_assets([slot], [], [artifact])[0]

    assert item.action == "blocked"
    assert "unsupported acquisition_mode" in item.reason


@pytest.mark.parametrize("limit", [0, -1, float("inf"), "five", True])
def test_invalid_max_use_seconds_fails_closed(limit):
    slot = _slot("s001", "screenshot", start=0.0, end=4.0)
    artifact = _artifact(
        "a001",
        "https://example.com/page",
        slot_id="s001",
        max_use_seconds=limit,
    )

    item = plan_assets([slot], [], [artifact])[0]

    assert item.action == "blocked"
    assert "positive finite number" in item.reason


def test_max_use_seconds_blocks_a_source_slot_that_holds_too_long():
    slot = _slot("s001", "screenshot", start=0.0, end=4.1)
    artifact = _artifact(
        "a001",
        "https://example.com/page",
        slot_id="s001",
        max_use_seconds=4.0,
    )

    item = plan_assets([slot], [], [artifact])[0]

    assert item.action == "blocked"
    assert "at most 4s" in item.reason
    assert "4.1s" in item.reason


def test_max_use_seconds_allows_a_source_slot_at_the_exact_limit():
    slot = _slot("s001", "screenshot", start=0.0, end=4.0)
    artifact = _artifact(
        "a001",
        "https://example.com/page",
        slot_id="s001",
        max_use_seconds=4.0,
    )

    assert plan_assets([slot], [], [artifact])[0].action == "shoot"


def test_source_policy_still_blocks_an_already_claimed_slot():
    slot = _slot("s001", "capture", start=0.0, end=5.0)
    artifact = _artifact(
        "a001",
        "https://example.com/video",
        slot_id="s001",
        max_use_seconds=4.0,
    )
    existing = _record("existing", tier="primary", used_in_slots=("s001",))

    assert plan_assets([slot], [existing], [artifact])[0].action == "blocked"


# --- plan_assets: unknown kind -------------------------------------------


def test_unknown_kind_is_blocked_naming_the_kind():
    slot = _slot("s001", "mystery-kind")

    items = plan_assets([slot], [], [])

    assert items[0].action == "blocked"
    assert "mystery-kind" in items[0].reason


# --- plan_assets: general properties -------------------------------------


def test_every_plan_item_has_a_nonempty_reason():
    slots = [
        _slot("s001", "plate"),
        _slot("s002", "archival"),
        _slot("s003", "capture"),
        _slot("s004", "screenshot"),
        _slot("s005", "graphic"),
        _slot("s006", "capture"),  # no artifact -> blocked
        _slot("s007", "mystery"),  # unknown kind -> blocked
        _slot("s008", "plate"),  # will be satisfied
    ]
    artifacts = [_artifact("a001", "https://example.com/v", slot_id="s003")]
    records = [_record("aX", used_in_slots=("s008",))]

    items = plan_assets(slots, records, artifacts)

    assert len(items) == len(slots)
    for item in items:
        assert item.reason


def test_plan_assets_with_no_slots_and_empty_ledger_returns_empty_list_without_special_casing():
    assert plan_assets([], [], []) == []


# --- format_plan -----------------------------------------------------------


def test_format_plan_groups_by_action_and_includes_a_count_summary():
    items = [
        PlanItem("s001", "plate", "atmospheric", "generate", "generate a plate"),
        PlanItem("s002", "archival", "archival", "search", "search archives"),
        PlanItem("s003", "screenshot", "primary", "manual", ACTION_REASONS["manual"]),
    ]

    text = format_plan(items)

    assert "generate (1)" in text
    assert "search (1)" in text
    assert "manual (1)" in text
    assert "s001" in text
    assert "s002" in text
    assert "s003" in text
    assert "3 slot(s)" in text


def test_format_plan_with_no_items_does_not_raise():
    assert isinstance(format_plan([]), str)


# --- execute_plan: generate (plate) ---------------------------------------


def test_execute_plan_generates_a_plate_and_returns_an_atmospheric_record(tmp_path):
    slot = _slot("s001", "plate", detail="grain", start=0.0, end=0.3)
    items = plan_assets([slot], [], [])

    records, findings = execute_plan(items, [slot], tmp_path, grade=GRADE, claims=[])

    assert findings == []
    assert len(records) == 1
    record = records[0]
    assert record.tier == "atmospheric"
    assert record.original_url == ""
    assert record.license == ""
    assert Path(record.local_path).exists()
    assert Path(record.local_path).stat().st_size > 0


def test_generated_plate_records_used_in_slots_contains_exactly_that_slot(tmp_path):
    slot = _slot("s001", "plate", detail="grain", start=0.0, end=0.3)
    items = plan_assets([slot], [], [])

    records, _ = execute_plan(items, [slot], tmp_path, grade=GRADE, claims=[])

    assert records[0].used_in_slots == ("s001",)


def test_valid_plate_kind_in_detail_is_honoured_with_empty_notes(tmp_path):
    slot = _slot("s001", "plate", detail="vignette", start=0.0, end=0.3)
    items = plan_assets([slot], [], [])

    records, findings = execute_plan(items, [slot], tmp_path, grade=GRADE, claims=[])

    assert findings == []
    assert records[0].notes == ""


def test_invalid_plate_detail_falls_back_to_grain_and_says_so_in_notes(tmp_path):
    slot = _slot("s001", "plate", detail="dark corridor", start=0.0, end=0.3)
    items = plan_assets([slot], [], [])

    records, findings = execute_plan(items, [slot], tmp_path, grade=GRADE, claims=[])

    assert findings == []
    assert "grain" in records[0].notes.lower()
    assert "dark corridor" in records[0].notes


def test_final_quality_refuses_an_invalid_scene_plate_instead_of_defaulting(tmp_path):
    slot = _slot("s001", "plate", detail="dark corridor", start=0.0, end=0.3)
    items = plan_assets([slot], [], [])

    records, findings = execute_plan(
        items, [slot], tmp_path, grade=GRADE, claims=[], quality="final"
    )

    assert records == []
    assert any(
        f.severity == "error"
        and "Refusing final-quality plate" in f.message
        and "dark corridor" in f.message
        for f in findings
    )
    assert not (tmp_path / "s001-plate.mp4").exists()


# --- execute_plan: search (archival) --------------------------------------


def test_execute_plan_sources_an_archival_slot_via_fake_transports(tmp_path):
    slot = _slot("s001", "archival", detail="civil defense", start=0.0, end=5.0)
    items = plan_assets([slot], [], [])

    docs = [
        {
            "identifier": "duck_and_cover_1951",
            "title": "Duck and Cover",
            "collection": ["prelinger"],
            "mediatype": "movies",
        }
    ]
    files = [{"name": "duck_and_cover_1951.mp4", "format": "h.264", "size": 12345}]
    transport = _transport_for(
        {
            "advancedsearch.php": (200, _archive_org_body(docs)),
            "commons.wikimedia.org": (200, _wikimedia_body({})),
            "archive.org/metadata/duck_and_cover_1951": (
                200,
                _metadata_body(files, "duck_and_cover_1951"),
            ),
            "duck_and_cover_1951.mp4": (200, b"fake video bytes"),
        }
    )

    records, findings = execute_plan(
        items, [slot], tmp_path, grade=GRADE, claims=[], archive_transport=transport
    )

    assert findings == []
    assert len(records) == 1
    record = records[0]
    assert record.tier == "archival"
    assert record.used_in_slots == ("s001",)
    assert Path(record.local_path).read_bytes() == b"fake video bytes"


def test_archival_slot_with_no_hits_yields_a_finding_not_an_exception(tmp_path):
    slot = _slot("s001", "archival", detail="nonexistent topic", start=0.0, end=5.0)
    items = plan_assets([slot], [], [])

    transport = _transport_for(
        {
            "advancedsearch.php": (200, _archive_org_body([])),
            "commons.wikimedia.org": (200, _wikimedia_body({})),
        }
    )

    records, findings = execute_plan(
        items, [slot], tmp_path, grade=GRADE, claims=[], archive_transport=transport
    )

    assert records == []
    assert len(findings) == 1
    assert findings[0].gate == "assets"
    assert findings[0].severity == "error"
    assert "s001" in findings[0].message


def test_archival_slot_whose_hit_fails_to_resolve_yields_a_finding(tmp_path):
    slot = _slot("s001", "archival", detail="obscure film", start=0.0, end=5.0)
    items = plan_assets([slot], [], [])

    docs = [
        {
            "identifier": "obscure_id",
            "title": "Obscure",
            "collection": ["prelinger"],
            "mediatype": "movies",
        }
    ]
    # No format in this file list matches VIDEO_FORMAT_PREFERENCE, so
    # resolve_media_url returns the hit unresolved (media_url stays "").
    files = [{"name": "obscure.avi", "format": "Cinepack", "size": 1000}]
    transport = _transport_for(
        {
            "advancedsearch.php": (200, _archive_org_body(docs)),
            "commons.wikimedia.org": (200, _wikimedia_body({})),
            "archive.org/metadata/obscure_id": (200, _metadata_body(files, "obscure_id")),
        }
    )

    records, findings = execute_plan(
        items, [slot], tmp_path, grade=GRADE, claims=[], archive_transport=transport
    )

    assert records == []
    assert len(findings) == 1
    assert findings[0].gate == "assets"
    assert "s001" in findings[0].message


def test_one_slot_failing_does_not_prevent_a_later_slot_from_being_sourced(tmp_path):
    failing_slot = _slot("s001", "archival", detail="nothing here", start=0.0, end=1.0)
    later_slot = _slot("s002", "plate", detail="grain", start=1.0, end=1.3)
    slots = [failing_slot, later_slot]
    items = plan_assets(slots, [], [])

    transport = _transport_for(
        {
            "advancedsearch.php": (200, _archive_org_body([])),
            "commons.wikimedia.org": (200, _wikimedia_body({})),
        }
    )

    records, findings = execute_plan(
        items, slots, tmp_path, grade=GRADE, claims=[], archive_transport=transport
    )

    assert len(findings) == 1
    assert findings[0].gate == "assets"
    assert "s001" in findings[0].message

    assert len(records) == 1
    assert records[0].used_in_slots == ("s002",)


# --- execute_plan: fetch (capture) ----------------------------------------


def test_execute_plan_rechecks_acquisition_policy_before_fetch(tmp_path):
    slot = _slot("s001", "capture", start=0.0, end=5.0)
    item = PlanItem(
        slot_id="s001",
        kind="capture",
        tier="primary",
        action="fetch",
        reason="stale plan prepared before the rights policy changed",
    )
    artifact = _artifact(
        "a001",
        "https://www.youtube.com/watch?v=abc123",
        slot_id="s001",
        acquisition_mode="screenshot-only",
    )

    def runner(_argv):
        raise AssertionError("yt-dlp must not run against a screenshot-only source")

    records, findings = execute_plan(
        [item],
        [slot],
        tmp_path,
        grade=GRADE,
        claims=[],
        ytdlp_runner=runner,
        artifacts=artifact_bindings_by_slot([artifact]),
    )

    assert records == []
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert "Acquisition refused" in findings[0].message
    assert "screenshot-only" in findings[0].message


def test_fetch_slot_whose_claims_ledger_does_not_cite_the_url_yields_a_finding(tmp_path):
    slot = _slot("s001", "capture", start=0.0, end=5.0)
    artifact_list = [
        _artifact("a001", "https://www.youtube.com/watch?v=abc123", slot_id="s001")
    ]
    items = plan_assets([slot], [], artifact_list)

    def runner(argv):
        raise AssertionError("runner must not be invoked when the ledger refuses")

    records, findings = execute_plan(
        items,
        [slot],
        tmp_path,
        grade=GRADE,
        claims=[],
        ytdlp_runner=runner,
        ytdlp_prober=lambda p: True,
        artifacts=artifact_urls_by_slot(artifact_list),
    )

    assert records == []
    assert len(findings) == 1
    assert findings[0].gate == "assets"
    assert findings[0].severity == "error"
    assert "refus" in findings[0].message.lower()


def test_fetch_slot_whose_claims_ledger_cites_the_url_yields_a_record(tmp_path):
    slot = _slot("s001", "capture", start=0.0, end=5.0)
    url = "https://www.youtube.com/watch?v=abc123"
    artifact_list = [
        _artifact(
            "a001",
            url,
            slot_id="s001",
            title="Original test upload",
            date="2013-09-23",
            source_role="primary-original",
            rights_note="Use no more than a brief credited excerpt.",
        )
    ]
    items = plan_assets([slot], [], artifact_list)
    claims = [
        {
            "claim_id": "c1",
            "text": "A claim",
            "confidence": "documented",
            "sources": [url],
        }
    ]

    def runner(argv):
        return (0, b"", b"")

    records, findings = execute_plan(
        items,
        [slot],
        tmp_path,
        grade=GRADE,
        claims=claims,
        ytdlp_runner=runner,
        ytdlp_prober=lambda p: True,
        artifacts=artifact_bindings_by_slot(artifact_list),
    )

    assert findings == []
    assert len(records) == 1
    assert records[0].tier == "primary"
    assert records[0].used_in_slots == ("s001",)
    assert "artifact_id='a001'" in records[0].notes
    assert "title='Original test upload'" in records[0].notes
    assert "date='2013-09-23'" in records[0].notes
    assert "source_role='primary-original'" in records[0].notes
    assert "rights_note='Use no more than a brief credited excerpt.'" in records[0].notes


# --- execute_plan: no-op actions ------------------------------------------


def test_manual_and_satisfied_items_produce_no_records_or_findings(tmp_path):
    """`graphic` is deliberately absent here: it used to be a no-op action
    (`deferred`) and is now `draw`, which really does produce a record. An
    unbound screenshot slot is `blocked` (it needs a URL), and a screenshot slot
    WITH a binding is `shoot`, which also produces one -- so `manual` now only
    appears via ACTION_REASONS for a kind that has no binding path at all."""
    screenshot_slot = _slot("s001", "screenshot", start=0.0, end=1.0)
    satisfied_slot = _slot("s003", "archival", start=2.0, end=3.0)
    slots = [screenshot_slot, satisfied_slot]
    existing = [_record("existing-asset", used_in_slots=("s003",))]

    items = plan_assets(slots, existing, [])
    assert [item.action for item in items] == ["blocked", "satisfied"]

    records, findings = execute_plan(items, slots, tmp_path, grade=GRADE, claims=[])

    assert records == []
    assert findings == []


def test_a_graphic_slot_now_produces_a_real_asset(tmp_path):
    """The regression this whole change exists to prevent. A `graphic` slot was
    planned as handled and produced nothing, so assemble_footage skipped its
    cuts -- 81 slots and 45% of the audited episode."""
    from rabbithole.jsonio import read_json

    style = Path(__file__).resolve().parent.parent / "style"
    slot = _slot("s002", "graphic", detail="UAPA clause callout", start=0.0, end=1.0)
    items = plan_assets([slot], [], [])
    assert items[0].action == "draw"

    records, findings = execute_plan(
        items, [slot], tmp_path, grade=GRADE, claims=[],
        typography=read_json(style / "typography.json"),
        palette=read_json(style / "palette.json"),
    )

    assert [f for f in findings if f.severity == "error"] == []
    assert len(records) == 1
    assert records[0].asset_id == "card-s002"
    assert records[0].tier == "atmospheric"
    assert records[0].used_in_slots == ("s002",)
    assert Path(records[0].local_path).exists()


def test_final_quality_refuses_a_label_only_production_note_card(tmp_path):
    slot = _slot(
        "s002", "graphic", detail="UAPA clause callout", start=0.0, end=1.0
    )
    items = plan_assets([slot], [], [])

    records, findings = execute_plan(
        items,
        [slot],
        tmp_path,
        grade=GRADE,
        claims=[],
        quality="final",
    )

    assert records == []
    assert any(
        f.severity == "error"
        and "Refusing final-quality card" in f.message
        and "UAPA clause callout" in f.message
        for f in findings
    )
    assert not (tmp_path / "s002-card.mp4").exists()


def test_a_graphic_slot_without_a_style_pack_reports_rather_than_guessing(tmp_path):
    """A caller that forgot typography/palette should get a clear message, not a
    card rendered in whatever font libass defaults to."""
    slot = _slot("s002", "graphic", detail="UAPA clause callout", start=0.0, end=1.0)
    items = plan_assets([slot], [], [])

    records, findings = execute_plan(items, [slot], tmp_path, grade=GRADE, claims=[])

    assert records == []
    errors = [f for f in findings if f.severity == "error"]
    assert len(errors) == 1
    assert "typography/palette" in errors[0].message


# --- execute_plan output satisfies check_provenance -----------------------


def test_execute_plan_records_satisfy_check_provenance_for_full_coverage(tmp_path):
    plate_slot = _slot("s001", "plate", detail="grain", start=0.0, end=0.3)
    items = plan_assets([plate_slot], [], [])

    records, findings = execute_plan(items, [plate_slot], tmp_path, grade=GRADE, claims=[])

    assert findings == []
    assert check_provenance(records, ["s001"]) == []


def test_execute_plan_records_satisfy_check_provenance_across_mixed_kinds(tmp_path):
    plate_slot = _slot("s001", "plate", detail="grain", start=0.0, end=0.3)
    archival_slot = _slot("s002", "archival", detail="civil defense", start=0.3, end=5.0)
    slots = [plate_slot, archival_slot]
    items = plan_assets(slots, [], [])

    docs = [
        {
            "identifier": "duck_and_cover_1951",
            "title": "Duck and Cover",
            "collection": ["prelinger"],
            "mediatype": "movies",
        }
    ]
    files = [{"name": "duck_and_cover_1951.mp4", "format": "h.264", "size": 12345}]
    transport = _transport_for(
        {
            "advancedsearch.php": (200, _archive_org_body(docs)),
            "commons.wikimedia.org": (200, _wikimedia_body({})),
            "archive.org/metadata/duck_and_cover_1951": (
                200,
                _metadata_body(files, "duck_and_cover_1951"),
            ),
            "duck_and_cover_1951.mp4": (200, b"fake video bytes"),
        }
    )

    records, findings = execute_plan(
        items, slots, tmp_path, grade=GRADE, claims=[], archive_transport=transport
    )

    assert findings == []
    assert len(records) == 2
    assert check_provenance(records, ["s001", "s002"]) == []


# --- final-quality source gates -----------------------------------------------


def test_evidence_metrics_are_duration_weighted_not_just_a_record_count():
    slots = [
        _slot("s001", "graphic", detail="criteria: one | two", start=0.0, end=9.0),
        _slot("s002", "archival", detail="court order", start=9.0, end=10.0),
    ]
    records = [
        _record(
            "card",
            tier="atmospheric",
            original_url="",
            license="",
            used_in_slots=("s001",),
        ),
        _record("order", used_in_slots=("s002",)),
    ]

    metrics = evidence_metrics(slots, records)

    assert metrics.duration_ratio == pytest.approx(0.10)
    assert metrics.slot_ratio == pytest.approx(0.50)
    assert metrics.evidence_seconds == pytest.approx(1.0)


def test_final_quality_reports_the_measured_evidence_ratio():
    slots = [
        _slot("s001", "graphic", detail="criteria: one | two", start=0.0, end=9.0),
        _slot("s002", "archival", detail="court order", start=9.0, end=10.0),
    ]
    records = [
        _record(
            "card",
            tier="atmospheric",
            original_url="",
            license="",
            used_in_slots=("s001",),
        ),
        _record("order", used_in_slots=("s002",)),
    ]

    findings = check_source_quality(slots, records, quality="final")

    ratio_errors = [
        finding
        for finding in findings
        if finding.severity == "error" and "Sourced-evidence ratio" in finding.message
    ]
    assert len(ratio_errors) == 1
    assert "10.0%" in ratio_errors[0].message
    assert "1.00/10.00s" in ratio_errors[0].message
    assert "minimum of 50%" in ratio_errors[0].message


def test_final_quality_accepts_explicit_cards_above_the_evidence_floor():
    slots = [
        _slot("s001", "archival", detail="court order", start=0.0, end=6.0),
        _slot("s002", "graphic", detail="criteria: one | two", start=6.0, end=10.0),
    ]
    records = [
        _record("order", used_in_slots=("s001",)),
        _record(
            "card",
            tier="atmospheric",
            original_url="",
            license="",
            used_in_slots=("s002",),
        ),
    ]

    assert check_source_quality(slots, records, quality="final") == []


def test_final_quality_accepts_framework_authored_implicit_opening_as_grain():
    slots = [
        _slot("s001", "plate", detail="implicit opening", start=0.0, end=1.0),
        _slot("s002", "archival", detail="court order", start=1.0, end=2.0),
    ]
    records = [
        _record(
            "opening-grain",
            tier="atmospheric",
            provider="ffmpeg-lavfi",
            original_url="",
            license="",
            used_in_slots=("s001",),
        ),
        _record("order", used_in_slots=("s002",)),
    ]

    assert check_source_quality(slots, records, quality="final") == []


@pytest.mark.parametrize(
    "kind,detail",
    [
        ("plate", "dark corridor reenactment"),
        ("graphic", "headline zoom"),
    ],
)
@pytest.mark.parametrize("replacement_tier", ["primary", "archival", "illustrative"])
def test_sourced_footage_can_replace_generated_visual_slots(
    kind, detail, replacement_tier
):
    slots = [
        _slot("s001", "archival", detail="court order", start=0.0, end=6.0),
        _slot("s002", kind, detail=detail, start=6.0, end=10.0),
    ]
    records = [
        _record("order", used_in_slots=("s001",)),
        _record(
            "replacement-footage",
            tier=replacement_tier,
            provider="licensed-source",
            original_url="https://example.com/replacement.mp4",
            license="licensed for documentary use",
            local_path="/assets/replacement.mp4",
            used_in_slots=("s002",),
        ),
    ]

    assert plan_assets([slots[1]], [records[1]], [])[0].action == "satisfied"
    assert check_source_quality(slots, records, quality="final") == []
    expected_evidence = 6.0 if replacement_tier == "illustrative" else 10.0
    assert evidence_metrics(slots, records).evidence_seconds == pytest.approx(
        expected_evidence
    )


@pytest.mark.parametrize(
    "kind,detail,error_text",
    [
        ("plate", "dark corridor reenactment", "describes a scene"),
        ("graphic", "headline zoom", "label-only production note"),
    ],
)
def test_final_quality_still_rejects_generated_visual_placeholders(
    kind, detail, error_text
):
    slots = [
        _slot("s001", "archival", detail="court order", start=0.0, end=6.0),
        _slot("s002", kind, detail=detail, start=6.0, end=10.0),
    ]
    records = [
        _record("order", used_in_slots=("s001",)),
        _record(
            "generated-placeholder",
            tier="atmospheric",
            provider="local-generator",
            original_url="",
            license="",
            local_path="/assets/generated-placeholder.mp4",
            used_in_slots=("s002",),
        ),
    ]

    findings = check_source_quality(slots, records, quality="final")

    assert any(
        finding.severity == "error" and error_text in finding.message
        for finding in findings
    )


def test_animatic_quality_explicitly_preserves_placeholders_and_zero_evidence():
    slots = [
        _slot("s001", "plate", detail="dark corridor", start=0.0, end=5.0),
        _slot("s002", "graphic", detail="headline zoom", start=5.0, end=10.0),
    ]

    assert check_source_quality(slots, [], quality="animatic") == []


def test_final_quality_rejects_a_stale_record_of_the_wrong_tier():
    slot = _slot("s001", "archival", detail="court order", start=0.0, end=1.0)
    stale = _record("old-primary", tier="primary", used_in_slots=("s001",))

    findings = check_source_quality([slot], [stale], quality="final")

    assert any(
        finding.severity == "error"
        and "requires tier 'archival'" in finding.message
        and "old-primary" in finding.message
        for finding in findings
    )


# --- stale ledger detection -----------------------------------------------------


def test_a_record_of_the_wrong_tier_does_not_satisfy_a_slot():
    """Slot ids are positional, so adding a [SHOT:] marker renumbers everything
    after it and a ledger written against the old plan claims different shots.
    Checking only "is this slot id claimed" put a generated graphic card into an
    archival slot with no error raised anywhere."""
    slot = _slot("s010", "archival", detail="courtroom exterior")
    stale = _record("card-s010", tier="atmospheric", used_in_slots=("s010",))

    items = plan_assets([slot], [stale], [])

    assert items[0].action == "stale"
    assert items[0].action != "satisfied"


def test_the_stale_reason_names_both_tiers_and_the_likely_cause():
    slot = _slot("s010", "archival", detail="courtroom exterior")
    stale = _record("card-s010", tier="atmospheric", used_in_slots=("s010",))

    reason = plan_assets([slot], [stale], [])[0].reason

    assert "atmospheric" in reason
    assert "archival" in reason
    assert "markers were added or moved" in reason


def test_a_record_of_the_right_tier_still_satisfies():
    """The check must not break the ordinary case it guards."""
    slot = _slot("s010", "archival", detail="courtroom exterior")
    good = _record("archival-s010", tier="archival", used_in_slots=("s010",))

    items = plan_assets([slot], [good], [])

    assert items[0].action == "satisfied"


def test_a_stale_slot_is_not_re_sourced_over(tmp_path):
    """Sourcing over a corrupt ledger would leave the bad record in place; the
    author has to drop it first, so execute_plan must do nothing here."""
    slot = _slot("s010", "archival", detail="courtroom exterior")
    stale = _record("card-s010", tier="atmospheric", used_in_slots=("s010",))
    items = plan_assets([slot], [stale], [])

    records, findings = execute_plan(items, [slot], tmp_path, grade=GRADE, claims=[])

    assert records == []
    assert findings == []


@pytest.mark.parametrize("kind,wrong_tier", [
    ("archival", "atmospheric"),
    ("archival", "primary"),
    ("capture", "atmospheric"),
    ("screenshot", "archival"),
])
def test_every_kind_rejects_a_mismatched_tier(kind, wrong_tier):
    slot = _slot("s010", kind, detail="x")
    artifacts = [_artifact("a001", "https://example.com/v", slot_id="s010")]
    items = plan_assets([slot], [_record("a", tier=wrong_tier, used_in_slots=("s010",))], artifacts)
    assert items[0].action == "stale"


# --- the shoot action -----------------------------------------------------


def test_a_bound_screenshot_slot_is_captured_and_recorded(tmp_path):
    """`screenshot` was a declared gap for the life of this package -- planned
    `manual`, never sourced. It now produces a real record."""
    from PIL import Image

    slot = _slot("s001", "screenshot", detail="archived homepage", start=0.0, end=1.0)
    artifact_list = [
        _artifact(
            "a001",
            "https://web.archive.org/web/2026/https://x",
            slot_id="s001",
            title="Archived homepage",
            date="2026-07-01",
            source_role="contemporaneous-reporting",
            rights_note="Screenshot only; preserve visible attribution.",
        )
    ]
    items = plan_assets([slot], [], artifact_list)
    assert items[0].action == "shoot"

    def runner(argv):
        if argv[0] == "ffmpeg":
            import subprocess
            subprocess.run(argv, capture_output=True)
        else:
            import numpy as np

            work = tmp_path / ".capturework"
            work.mkdir(parents=True, exist_ok=True)
            rng = np.random.default_rng(3)
            Image.fromarray(
                (rng.random((180, 320, 3)) * 255).astype("uint8")
            ).save(work / "s001-capture.png")
        return 0, b"", b""

    records, findings = execute_plan(
        items, [slot], tmp_path, grade=GRADE, claims=[],
        artifacts=artifact_bindings_by_slot(artifact_list),
        capture_runner=runner,
    )

    assert [f for f in findings if f.severity == "error"] == []
    assert len(records) == 1
    assert records[0].asset_id == "capture-s001"
    assert records[0].tier == "primary"
    assert Path(records[0].local_path).exists()
    assert "artifact_id='a001'" in records[0].notes
    assert "title='Archived homepage'" in records[0].notes
    assert "date='2026-07-01'" in records[0].notes
    assert "source_role='contemporaneous-reporting'" in records[0].notes
    assert "rights_note='Screenshot only; preserve visible attribution.'" in records[0].notes


def test_capturing_a_live_page_surfaces_the_archive_warning(tmp_path):
    """The provenance point: a claim about what a page said on a date needs the
    archived copy, and the author should be told when it is not one."""
    from PIL import Image

    slot = _slot("s001", "screenshot", detail="homepage", start=0.0, end=1.0)
    artifact_list = [_artifact("a001", "https://example.com", slot_id="s001")]
    items = plan_assets([slot], [], artifact_list)

    def runner(argv):
        if argv[0] == "ffmpeg":
            import subprocess
            subprocess.run(argv, capture_output=True)
        else:
            import numpy as np

            work = tmp_path / ".capturework"
            work.mkdir(parents=True, exist_ok=True)
            rng = np.random.default_rng(3)
            Image.fromarray(
                (rng.random((180, 320, 3)) * 255).astype("uint8")
            ).save(work / "s001-capture.png")
        return 0, b"", b""

    _records, findings = execute_plan(
        items, [slot], tmp_path, grade=GRADE, claims=[],
        artifacts=artifact_urls_by_slot(artifact_list),
        capture_runner=runner,
    )

    assert any(f.severity == "warning" and "archive snapshot" in f.message for f in findings)


def test_a_failed_capture_is_reported_per_slot_and_does_not_abort(tmp_path):
    slots = [
        _slot("s001", "screenshot", start=0.0, end=1.0),
        _slot("s002", "plate", detail="grain", start=1.0, end=1.3),
    ]
    artifact_list = [_artifact("a001", "https://example.com/x", slot_id="s001")]
    items = plan_assets(slots, [], artifact_list)

    def runner(argv):
        return 1, b"", b"browser exploded"

    records, findings = execute_plan(
        items, slots, tmp_path, grade=GRADE, claims=[],
        artifacts=artifact_urls_by_slot(artifact_list),
        capture_runner=runner,
    )

    assert any("Capture failed for slot 's001'" in f.message for f in findings)
    # The plate after it still got generated: one failure does not end the run.
    assert any(r.asset_id == "plate-s002" for r in records)

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

import rabbithole.assets as assets_module
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
    select_plan_items,
    usable_provenance_records,
)
from rabbithole.provenance import AssetRecord, check_provenance
from rabbithole.slots import Slot
from rabbithole.sources.capture import (
    CaptureFraming,
    CaptureMotionFraming,
    CaptureRectangle,
    CaptureResult,
)

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


def test_citation_card_copy_prefers_authored_sentence_and_strips_marker_metadata():
    assert (
        assets_module._citation_card_primary_text(
            'source=wt-guardian query="mystery" detail=headline and publication date',
            "The Guardian published the report on 1 May 2014. "
            "The browser consent layer obscured the retained pixels.",
        )
        == "The Guardian published the report on 1 May 2014."
    )
    assert (
        assets_module._citation_card_primary_text(
            "source=wt-guardian detail=headline and publication date"
        )
        == "headline and publication date"
    )


# --- load_artifacts ----------------------------------------------------


def test_load_artifacts_missing_file_returns_empty_list(tmp_path):
    assert load_artifacts(tmp_path / "artifacts.json") == []


def test_load_artifacts_merges_capture_target_overlay_by_slot(tmp_path):
    artifact_path = tmp_path / "artifacts.json"
    artifact_path.write_text(
        json.dumps(
            [
                {
                    "artifact_id": "source-page-s001",
                    "url": "https://example.com/article",
                    "slot_id": "s001",
                    "acquisition_mode": "screenshot-only",
                }
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "capture-targets-archived.json").write_text(
        json.dumps(
            {
                "s001": {
                    "source": "source-page",
                    "spec": {"text": "Exact evidence phrase"},
                    "strategy": "browser-text",
                    "note": "Verified against the archived DOM.",
                }
            }
        ),
        encoding="utf-8",
    )

    [artifact] = load_artifacts(artifact_path)

    assert artifact.capture_spec == {"text": "Exact evidence phrase"}
    assert artifact.capture_strategy == "browser-text"
    assert artifact.capture_note == "Verified against the archived DOM."


def test_load_artifacts_rejects_capture_target_source_mismatch(tmp_path):
    artifact_path = tmp_path / "artifacts.json"
    artifact_path.write_text(
        json.dumps(
            [
                {
                    "artifact_id": "source-page-s001",
                    "url": "https://example.com/article",
                    "slot_id": "s001",
                }
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "capture-targets.json").write_text(
        json.dumps(
            {
                "s001": {
                    "source": "different-source",
                    "spec": {"text": "Evidence"},
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="different-source"):
        load_artifacts(artifact_path)


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
                    "source_license": "CC-BY-4.0",
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
            source_license="CC-BY-4.0",
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


def test_batch_selects_only_the_next_actionable_items():
    items = [
        PlanItem("s001", "plate", "atmospheric", "satisfied", "done"),
        PlanItem("s002", "graphic", "atmospheric", "draw", "draw"),
        PlanItem("s003", "screenshot", "primary", "blocked", "missing binding"),
        PlanItem("s004", "capture", "primary", "fetch", "fetch"),
    ]

    selected = select_plan_items(items, batch_size=2)

    assert [item.slot_id for item in selected] == ["s002", "s004"]


def test_exact_slot_filter_composes_with_batch_limit():
    items = [
        PlanItem("s001", "graphic", "atmospheric", "draw", "draw"),
        PlanItem("s002", "graphic", "atmospheric", "draw", "draw"),
        PlanItem("s003", "graphic", "atmospheric", "draw", "draw"),
    ]

    selected = select_plan_items(
        items, slot_ids=("s002", "s003"), batch_size=1
    )

    assert [item.slot_id for item in selected] == ["s002"]


def test_existing_record_without_a_decodable_local_file_does_not_satisfy(tmp_path):
    record = _record(
        "missing",
        local_path="assets/missing.mp4",
        used_in_slots=("s001",),
    )

    usable, findings = usable_provenance_records(
        [record], tmp_path, prober=lambda _path: True
    )

    assert usable == []
    assert len(findings) == 1
    assert "will not satisfy acquisition" in findings[0].message


def test_existing_record_must_decode_even_when_the_path_exists(tmp_path):
    broken = tmp_path / "assets" / "broken.mp4"
    broken.parent.mkdir()
    broken.write_bytes(b"not media")
    record = _record(
        "broken",
        local_path="assets/broken.mp4",
        used_in_slots=("s001",),
    )

    usable, _findings = usable_provenance_records(
        [record], tmp_path, prober=lambda _path: False
    )

    assert usable == []


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


def test_record_callback_runs_immediately_after_each_decodable_output(tmp_path):
    slots = [
        _slot("s001", "plate", detail="grain", start=0.0, end=0.2),
        _slot("s002", "plate", detail="grain", start=0.2, end=0.4),
    ]
    seen = []

    records, findings = execute_plan(
        plan_assets(slots, [], []),
        slots,
        tmp_path,
        grade=GRADE,
        claims=[],
        on_record=seen.append,
    )

    assert findings == []
    assert seen == records
    assert [record.used_in_slots for record in seen] == [("s001",), ("s002",)]


def test_undecodable_output_is_not_recorded_or_checkpointed(tmp_path, monkeypatch):
    slot = _slot("s001", "plate", detail="grain", start=0.0, end=0.2)
    monkeypatch.setattr(
        assets_module,
        "build_plate",
        lambda _spec, path, grade: path.write_bytes(b"broken"),
    )
    seen = []

    records, findings = execute_plan(
        plan_assets([slot], [], []),
        [slot],
        tmp_path,
        grade=GRADE,
        claims=[],
        media_prober=lambda _path: False,
        on_record=seen.append,
    )

    assert records == []
    assert seen == []
    assert any("provenance was not recorded" in finding.message for finding in findings)


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
        items,
        [slot],
        tmp_path,
        grade=GRADE,
        claims=[],
        archive_transport=transport,
        media_prober=lambda _path: True,
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
        Path(argv[argv.index("-o") + 1]).write_bytes(b"fake video bytes")
        return (0, b"", b"")

    records, findings = execute_plan(
        items,
        [slot],
        tmp_path,
        grade=GRADE,
        claims=claims,
        ytdlp_runner=runner,
        ytdlp_prober=lambda p: True,
        media_prober=lambda _path: True,
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


def test_execute_plan_keeps_adjacent_same_heading_cards_stable(monkeypatch, tmp_path):
    slots = [
        _slot(
            "s001",
            "graphic",
            detail="UPLOAD: EVERY TWO MINUTES | OBSERVED",
            start=0.0,
            end=2.0,
        ),
        _slot(
            "s002",
            "graphic",
            detail="UPLOAD: PEAK PERIOD | MAY 2014",
            start=2.0,
            end=4.0,
        ),
    ]
    rendered = []

    def fake_build(spec, out_path, *_args, **_kwargs):
        rendered.append(spec)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"grouped card")
        return out_path

    monkeypatch.setattr(assets_module.cards, "build_card", fake_build)
    records, findings = execute_plan(
        plan_assets(slots, [], []),
        slots,
        tmp_path,
        grade=GRADE,
        claims=[],
        typography={},
        palette={},
        media_prober=lambda _path: True,
    )

    assert [finding for finding in findings if finding.severity == "error"] == []
    assert len(records) == 2
    assert rendered[0].items == rendered[1].items
    assert len(rendered[0].items) == 2
    assert [spec.active_item_index for spec in rendered] == [0, 1]


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
        items,
        slots,
        tmp_path,
        grade=GRADE,
        claims=[],
        archive_transport=transport,
        media_prober=lambda _path: True,
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
    assert metrics.source_pixel_duration_ratio == pytest.approx(0.10)
    assert metrics.source_pixel_seconds == pytest.approx(1.0)


def test_citation_card_is_source_backed_but_not_source_pixel_evidence():
    slots = [
        _slot("s001", "screenshot", start=0.0, end=6.0),
        _slot("s002", "screenshot", start=6.0, end=10.0),
    ]
    records = [
        _record(
            "citation",
            tier="primary",
            provider="rabbithole-evidence-card",
            used_in_slots=("s001",),
        ),
        _record(
            "capture",
            tier="primary",
            provider="web.archive.org",
            used_in_slots=("s002",),
        ),
    ]

    metrics = evidence_metrics(slots, records)

    assert metrics.source_backed_duration_ratio == pytest.approx(1.0)
    assert metrics.source_backed_seconds == pytest.approx(10.0)
    assert metrics.source_pixel_duration_ratio == pytest.approx(0.4)
    assert metrics.source_pixel_seconds == pytest.approx(4.0)


def test_source_text_extract_is_source_backed_but_not_source_pixel_evidence():
    slots = [
        _slot("s001", "screenshot", start=0.0, end=6.0),
        _slot("s002", "screenshot", start=6.0, end=10.0),
    ]
    records = [
        _record(
            "extract",
            tier="primary",
            provider="rabbithole-source-text-extract",
            used_in_slots=("s001",),
        ),
        _record(
            "capture",
            tier="primary",
            provider="web.archive.org",
            used_in_slots=("s002",),
        ),
    ]

    metrics = evidence_metrics(slots, records)

    assert metrics.source_backed_duration_ratio == pytest.approx(1.0)
    assert metrics.source_pixel_duration_ratio == pytest.approx(0.4)


def test_citation_cards_cannot_satisfy_final_evidence_gate_by_themselves():
    slots = [
        _slot("s001", "screenshot", start=0.0, end=6.0),
        _slot(
            "s002",
            "graphic",
            detail="testing logic: input | output",
            start=6.0,
            end=10.0,
        ),
    ]
    records = [
        _record(
            "citation",
            tier="primary",
            provider="rabbithole-evidence-card",
            used_in_slots=("s001",),
        ),
        _record(
            "graphic",
            tier="atmospheric",
            provider="rabbithole-cards",
            original_url="",
            license="",
            used_in_slots=("s002",),
        ),
    ]

    findings = check_source_quality(slots, records, quality="final")

    assert not any(
        "Sourced-evidence ratio is" in finding.message
        for finding in findings
    )
    assert any(
        finding.severity == "error"
        and "Source-pixel evidence ratio is 0.0%" in finding.message
        and "citation cards are source-backed only" in finding.message
        for finding in findings
    )


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


@pytest.mark.parametrize(
    "fingerprint,expected_capture_calls",
    [
        ("", 2),
        ("same-explicit-capture-spec", 1),
    ],
)
def test_capture_reuse_requires_an_explicit_identical_spec_and_keeps_slot_outputs(
    tmp_path, monkeypatch, fingerprint, expected_capture_calls
):
    slots = [
        _slot("s001", "screenshot", start=0.0, end=1.0),
        _slot("s002", "screenshot", start=1.0, end=3.0),
    ]
    url = "https://example.com/one-page"
    artifacts = [
        _artifact(
            "a001",
            url,
            slot_id="s001",
            capture_spec_fingerprint=fingerprint,
        ),
        _artifact(
            "a002",
            url,
            slot_id="s002",
            capture_spec_fingerprint=fingerprint,
        ),
    ]
    calls = []

    def fake_capture(_url, out_path, duration, _work_dir, **_kwargs):
        calls.append((out_path, duration))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"decodable stand-in")
        return CaptureResult(path=out_path, kind="page")

    monkeypatch.setattr(assets_module.capture, "capture_to_video", fake_capture)
    records, findings = execute_plan(
        plan_assets(slots, [], artifacts),
        slots,
        tmp_path,
        grade=GRADE,
        claims=[],
        artifacts=artifact_bindings_by_slot(artifacts),
        media_prober=lambda _path: True,
    )

    assert [finding for finding in findings if finding.severity == "error"] == []
    assert len(calls) == expected_capture_calls
    assert [record.asset_id for record in records] == [
        "capture-s001",
        "capture-s002",
    ]
    assert records[0].local_path != records[1].local_path
    assert all(Path(record.local_path).exists() for record in records)


def test_targeted_same_url_slots_share_one_page_batch_and_keep_distinct_records(
    tmp_path, monkeypatch
):
    slots = [
        _slot("s001", "screenshot", start=0.0, end=1.0),
        _slot("s002", "screenshot", start=1.0, end=3.0),
    ]
    url = "https://web.archive.org/web/2026/https://example.com/article"
    specs = [
        {"full_page": True, "text": "first evidence"},
        {"full_page": True, "text": "second evidence"},
    ]
    artifacts = [
        _artifact(
            f"a00{index}",
            url,
            slot_id=slot.slot_id,
            capture_spec=spec,
        )
        for index, (slot, spec) in enumerate(zip(slots, specs), start=1)
    ]
    page_contexts = []
    capture_calls = []
    source_probes = []
    recorded = []
    callback = object()

    @contextmanager
    def fake_shared_page_capture():
        page_contexts.append("opened")
        yield callback

    def fake_capture(
        requested_url, out_path, duration, _work_dir, **kwargs
    ):
        capture_calls.append(
            (
                requested_url,
                out_path,
                duration,
                kwargs["spec"],
                kwargs["targeted_capture"],
            )
        )
        # capture_to_video performs this optional probe once per call; the
        # memoized wrapper supplied by execute_plan must collapse it to one.
        assert kwargs["transport"](requested_url) == (200, b"<html>source</html>")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(f"video for {out_path.stem}".encode())
        return CaptureResult(path=out_path, kind="page")

    def transport(requested_url):
        source_probes.append(requested_url)
        return 200, b"<html>source</html>"

    monkeypatch.setattr(
        assets_module.capture,
        "shared_page_capture",
        fake_shared_page_capture,
    )
    monkeypatch.setattr(
        assets_module.capture, "capture_to_video", fake_capture
    )

    records, findings = execute_plan(
        plan_assets(slots, [], artifacts),
        slots,
        tmp_path,
        grade=GRADE,
        claims=[],
        artifacts=artifact_bindings_by_slot(artifacts),
        capture_transport=transport,
        media_prober=lambda _path: True,
        on_record=recorded.append,
    )

    assert [finding for finding in findings if finding.severity == "error"] == []
    assert page_contexts == ["opened"]
    assert source_probes == [url]
    assert [call[3] for call in capture_calls] == specs
    assert all(call[4] is callback for call in capture_calls)
    assert [call[2] for call in capture_calls] == [1.0, 2.0]
    assert [record.asset_id for record in records] == [
        "capture-s001",
        "capture-s002",
    ]
    assert records == recorded
    assert records[0].local_path != records[1].local_path


def test_final_browser_source_url_moves_only_once_across_entire_episode():
    slots = [
        _slot("s001", "screenshot", start=0.0, end=1.0),
        _slot("s002", "graphic", start=1.0, end=2.0),
        _slot("s003", "screenshot", start=2.0, end=3.0),
        _slot("s004", "screenshot", start=3.0, end=4.0),
    ]
    first_url = "https://example.com/article"
    other_url = "https://example.com/other"
    artifacts = [
        _artifact(
            f"a00{slot.slot_id[-1]}",
            url,
            slot_id=slot.slot_id,
            capture_spec={"text": f"exact line {slot.slot_id[-1]}", "motion": True},
        )
        for slot, url in (
            (slots[0], first_url),
            (slots[2], other_url),
            (slots[3], first_url),
        )
    ]

    specs = assets_module._browser_capture_specs_for_slots(
        slots,
        artifact_bindings_by_slot(artifacts),
        quality="final",
    )

    assert set(specs) == {"s001", "s003", "s004"}
    assert all(spec["highlight"] is True for spec in specs.values())
    assert "motion" in specs["s001"]
    assert "motion" in specs["s003"]
    assert "motion" not in specs["s004"]


def test_later_noncontiguous_browser_line_uses_motion_only_as_static_locator(
    tmp_path, monkeypatch
):
    slots = [
        _slot("s001", "screenshot", start=0.0, end=1.0),
        _slot("s002", "screenshot", start=1.0, end=2.0),
        _slot("s003", "screenshot", start=2.0, end=3.0),
    ]
    url = "https://example.com/article"
    other_url = "https://example.com/other"
    artifacts = [
        _artifact(
            f"a00{index}",
            artifact_url,
            slot_id=slot.slot_id,
            capture_spec={"text": f"exact line {index}", "motion": True},
        )
        for index, (slot, artifact_url) in enumerate(
            zip(slots, [url, other_url, url]), start=1
        )
    ]
    capture_specs = []
    derived = []

    @contextmanager
    def fake_shared_page_capture():
        yield object()

    def fake_capture(_url, out_path, _duration, _work_dir, **kwargs):
        spec = assets_module.capture.CaptureSpec.from_value(kwargs["spec"])
        capture_specs.append((out_path.name, spec))
        if out_path.name == "s003-capture.mp4" and spec.motion is None:
            raise RuntimeError("native still crop was blank")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"browser capture")
        framing = None
        if spec.motion is not None:
            clip = CaptureRectangle(0, 0, 1920, 1080)
            framing = CaptureFraming(
                mode="motion",
                target=CaptureRectangle(200, 300, 500, 60),
                clip=clip,
                content=CaptureRectangle(0, 0, 1920, 4000),
                motion=CaptureMotionFraming(
                    frame_count=31,
                    fps=30,
                    duration_seconds=31 / 30,
                    authored_frame_count=30,
                    safe_trailing_frames=1,
                    establish_fraction=0.22,
                    move_fraction=0.50,
                    easing="smoothstep",
                    start_clip=clip,
                    end_clip=clip,
                ),
            )
        return CaptureResult(path=out_path, kind="page", framing=framing)

    def fake_derive(source_path, out_path, **kwargs):
        derived.append((source_path, out_path, kwargs))
        out_path.write_bytes(b"static final frame")
        return out_path

    monkeypatch.setattr(
        assets_module.capture, "shared_page_capture", fake_shared_page_capture
    )
    monkeypatch.setattr(assets_module.capture, "capture_to_video", fake_capture)
    monkeypatch.setattr(
        assets_module.frame_video, "derive_source_frame_video", fake_derive
    )

    records, findings = execute_plan(
        plan_assets(slots, [], artifacts),
        slots,
        tmp_path,
        grade=GRADE,
        claims=[],
        artifacts=artifact_bindings_by_slot(artifacts),
        quality="final",
        media_prober=lambda _path: True,
    )

    assert len(records) == 3
    # Same-URL requests share a page batch, so s003 is attempted before the
    # intervening URL. Its emitted request is static; the following motion
    # request is only the internal locator used to derive a frozen frame.
    assert [
        (name, spec.motion is not None) for name, spec in capture_specs
    ] == [
        ("s001-capture.mp4", True),
        ("s003-capture.mp4", False),
        (".s003-motion-locator.mp4", True),
        ("s002-capture.mp4", True),
    ]
    assert all(spec.highlight for _, spec in capture_specs)
    assert len(derived) == 1
    assert derived[0][2]["timestamp"] == pytest.approx(1.0)
    assert derived[0][2]["duration"] == pytest.approx(1.0)
    assert any(
        finding.severity == "warning" and "No repeated move" in finding.message
        for finding in findings
    )
    assert '"mode":"target-hold-fallback"' in records[2].notes


def test_invalid_artifact_capture_spec_blocks_before_io():
    slot = _slot("s001", "screenshot", start=0.0, end=1.0)
    artifact = _artifact(
        "a001",
        "https://example.com/article",
        slot_id="s001",
        acquisition_mode="screenshot-only",
        capture_spec={"scroll_target": {"y": -1}},
    )

    [item] = plan_assets([slot], [], [artifact])

    assert item.action == "blocked"
    assert "invalid capture_spec" in item.reason
    assert "non-negative" in item.reason


def test_execute_plan_forwards_authored_capture_spec_and_records_it(
    tmp_path, monkeypatch
):
    slot = _slot("s001", "screenshot", start=0.0, end=1.0)
    spec = {"text": "Google behind Webdriver Torso mystery"}
    artifact = _artifact(
        "a001",
        "https://example.com/article",
        slot_id="s001",
        acquisition_mode="screenshot-only",
        capture_spec=spec,
    )
    calls = []

    def fake_capture(_url, out_path, _duration, _work_dir, **kwargs):
        calls.append(kwargs)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"decodable stand-in")
        return CaptureResult(
            path=out_path,
            kind="page",
            framing=CaptureFraming(
                mode="target",
                target=CaptureRectangle(100, 200, 300, 50),
                clip=CaptureRectangle(0, 0, 1920, 1080),
                content=CaptureRectangle(0, 0, 1920, 4000),
            ),
        )

    monkeypatch.setattr(assets_module.capture, "capture_to_video", fake_capture)
    records, findings = execute_plan(
        plan_assets([slot], [], [artifact]),
        [slot],
        tmp_path,
        grade=GRADE,
        claims=[],
        artifacts=artifact_bindings_by_slot([artifact]),
        media_prober=lambda _path: True,
    )

    assert [finding for finding in findings if finding.severity == "error"] == []
    assert calls[0]["spec"] == spec
    assert '"text":"Google behind Webdriver Torso mystery"' in records[0].notes
    assert (
        'browser_framing={"authored_crop":null,"clip":'
        '{"height":1080,"width":1920,"x":0,"y":0},'
        '"content":{"height":4000,"width":1920,"x":0,"y":0},'
        '"mode":"target","target":{"height":50,"width":300,"x":100,"y":200}}'
        in records[0].notes
    )


def test_source_frame_artifact_requires_a_valid_timestamp():
    slot = _slot("s002", "screenshot", start=0.0, end=1.0)
    artifact = _artifact(
        "a002",
        "https://www.youtube.com/watch?v=example",
        slot_id="s002",
        acquisition_mode="screenshot-only",
        source_video_slot="s001",
    )

    [item] = plan_assets([slot], [], [artifact])

    assert item.action == "blocked"
    assert "source_frame_timestamp" in item.reason


def test_execute_plan_derives_source_frame_without_browser_or_network(
    tmp_path, monkeypatch
):
    slot = _slot("s002", "screenshot", start=0.0, end=1.25)
    artifact = _artifact(
        "a002",
        "https://www.youtube.com/watch?v=example",
        slot_id="s002",
        acquisition_mode="screenshot-only",
        source_video_slot="s001",
        source_frame_timestamp=2.5,
        source_attribution="Webdriver Torso / YouTube",
        source_date_label="23 Sep 2013",
    )
    source = tmp_path / "s001-capture.mp4"
    source.write_bytes(b"validated source")
    calls = []

    def fake_derive(source_path, out_path, **kwargs):
        calls.append((source_path, out_path, kwargs))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"derived source frame")
        return out_path

    monkeypatch.setattr(
        assets_module.frame_video, "derive_source_frame_video", fake_derive
    )
    monkeypatch.setattr(
        assets_module.capture,
        "capture_to_video",
        lambda *_args, **_kwargs: pytest.fail("browser capture must not run"),
    )

    records, findings = execute_plan(
        plan_assets([slot], [], [artifact]),
        [slot],
        tmp_path,
        grade=GRADE,
        claims=[],
        artifacts=artifact_bindings_by_slot([artifact]),
        media_prober=lambda _path: True,
        source_media_by_slot={"s001": source},
    )

    assert [finding for finding in findings if finding.severity == "error"] == []
    assert calls[0][0] == source.resolve()
    assert calls[0][2]["timestamp"] == 2.5
    assert calls[0][2]["duration"] == 1.25
    assert calls[0][2]["attribution"] == "Webdriver Torso / YouTube"
    assert records[0].provider == "rabbithole-source-frame"
    assert "source_video_slot='s001'" in records[0].notes


def test_execute_plan_builds_local_evidence_card_without_browser(
    tmp_path, monkeypatch
):
    slot = _slot(
        "s034",
        "screenshot",
        detail="current channel identity and access-date context; verify totals manually",
        start=0.0,
        end=1.5,
    )
    artifact = _artifact(
        "wt-channel-s034",
        "https://www.youtube.com/@realwebdrivertorso",
        title="Webdriver Torso official YouTube channel",
        date="23 Sep 2013",
        slot_id="s034",
        acquisition_mode="screenshot-only",
        capture_strategy="manual-editorial-card-no-page-capture",
        capture_note="Dynamic page requires human verification.",
    )
    built = []

    def fake_build(spec, out_path, *_args, **_kwargs):
        built.append(spec)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"evidence card")
        return out_path

    monkeypatch.setattr(assets_module.cards, "build_card", fake_build)
    monkeypatch.setattr(
        assets_module.capture,
        "capture_to_video",
        lambda *_args, **_kwargs: pytest.fail("browser capture must not run"),
    )

    records, findings = execute_plan(
        plan_assets([slot], [], [artifact]),
        [slot],
        tmp_path,
        grade=GRADE,
        claims=[],
        artifacts=artifact_bindings_by_slot([artifact]),
        typography={},
        palette={},
        media_prober=lambda _path: True,
    )

    assert [finding for finding in findings if finding.severity == "error"] == []
    assert built[0].kind == "document"
    assert built[0].heading == "Webdriver Torso official YouTube channel"
    assert built[0].disclosure == assets_module.EVIDENCE_CARD_DISCLOSURE
    assert built[0].items == (
        "Dynamic page requires human verification.",
        "23 Sep 2013",
        "SOURCE · www.youtube.com",
    )
    assert records[0].provider == "rabbithole-evidence-card"
    assert "manual review required" in records[0].notes


def test_source_text_extract_strategy_requires_an_exact_authored_text_target():
    slot = _slot("s069", "screenshot", start=0.0, end=2.0)
    artifact = _artifact(
        "wt-guardian-s069",
        "https://www.theguardian.com/example",
        title="Example article",
        date="2014-05-01",
        slot_id="s069",
        acquisition_mode="screenshot-only",
        capture_strategy="source-text-extract",
        capture_spec={"selector": "article"},
    )

    reason = assets_module._artifact_policy_block(artifact, slot, "shoot")

    assert reason is not None
    assert "without an exact authored text target" in reason
    assert "selectors and coordinates are not source text" in reason


def test_execute_plan_builds_disclosed_source_text_extract_without_browser(
    tmp_path, monkeypatch
):
    slot = _slot("s069", "screenshot", start=0.0, end=2.0)
    url = "https://www.theguardian.com/technology/example"
    target = "But the truth is, as ever, more mundane"
    artifact = _artifact(
        "wt-guardian-s069",
        url,
        title="The truth behind the mysterious videos",
        date="2014-05-01",
        slot_id="s069",
        acquisition_mode="screenshot-only",
        capture_strategy="source-text-extract",
        capture_spec={"scroll_target": {"text": target}, "motion": True},
        capture_note="Browser output is a Guardian consent wall.",
    )
    built = []

    def fake_build(spec, out_path, *_args, **_kwargs):
        built.append(spec)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"source text extract")
        return out_path

    monkeypatch.setattr(
        assets_module.source_text, "build_source_text_extract", fake_build
    )
    monkeypatch.setattr(
        assets_module.capture,
        "capture_to_video",
        lambda *_args, **_kwargs: pytest.fail("browser capture must not run"),
    )

    records, findings = execute_plan(
        plan_assets([slot], [], [artifact]),
        [slot],
        tmp_path,
        grade=GRADE,
        claims=[],
        artifacts=artifact_bindings_by_slot([artifact]),
        typography={},
        palette={},
        capture_transport=lambda _url: (
            200,
            f"<html><article>{target}</article></html>".encode(),
        ),
        media_prober=lambda _path: True,
    )

    assert [finding for finding in findings if finding.severity == "error"] == []
    assert built[0].text == target
    assert built[0].publisher == "theguardian.com"
    assert built[0].date == "2014-05-01"
    assert built[0].url == url
    assert records[0].provider == "rabbithole-source-text-extract"
    assert records[0].tier == "primary"
    assert records[0].original_url == url
    assert "verbatim source-text extract" in records[0].notes
    assert "target verified against fetched visible source text" in records[0].notes
    assert "no webpage image retained" in records[0].notes
    assert f"exact_target={target!r}" in records[0].notes


def test_source_text_extract_refuses_when_target_is_absent_from_fetched_source(
    tmp_path, monkeypatch
):
    slot = _slot("s069", "screenshot", start=0.0, end=2.0)
    target = "But the truth is, as ever, more mundane"
    artifact = _artifact(
        "wt-guardian-s069",
        "https://www.theguardian.com/technology/example",
        title="The truth behind the mysterious videos",
        date="2014-05-01",
        slot_id="s069",
        acquisition_mode="screenshot-only",
        capture_strategy="source-text-extract",
        capture_spec={"text": target},
    )
    monkeypatch.setattr(
        assets_module.source_text,
        "build_source_text_extract",
        lambda *_args, **_kwargs: pytest.fail("unverified text must not render"),
    )

    records, findings = execute_plan(
        plan_assets([slot], [], [artifact]),
        [slot],
        tmp_path,
        grade=GRADE,
        claims=[],
        artifacts=artifact_bindings_by_slot([artifact]),
        typography={},
        palette={},
        capture_transport=lambda _url: (200, b"<article>Different text</article>"),
        media_prober=lambda _path: True,
    )

    assert records == []
    assert any(
        finding.severity == "error"
        and "was not found in fetched visible source text" in finding.message
        and "No extract was rendered" in finding.message
        for finding in findings
    )


def test_source_text_extract_refuses_when_verification_fetch_fails(
    tmp_path, monkeypatch
):
    slot = _slot("s069", "screenshot", start=0.0, end=2.0)
    artifact = _artifact(
        "wt-guardian-s069",
        "https://www.theguardian.com/technology/example",
        title="The truth behind the mysterious videos",
        date="2014-05-01",
        slot_id="s069",
        acquisition_mode="screenshot-only",
        capture_strategy="source-text-extract",
        capture_spec={"text": "verified phrase"},
    )
    monkeypatch.setattr(
        assets_module.source_text,
        "build_source_text_extract",
        lambda *_args, **_kwargs: pytest.fail("failed fetch must not render"),
    )

    def failed_transport(_url):
        raise RuntimeError("network unavailable")

    records, findings = execute_plan(
        plan_assets([slot], [], [artifact]),
        [slot],
        tmp_path,
        grade=GRADE,
        claims=[],
        artifacts=artifact_bindings_by_slot([artifact]),
        typography={},
        palette={},
        capture_transport=failed_transport,
        media_prober=lambda _path: True,
    )

    assert records == []
    assert any(
        finding.severity == "error"
        and "Source-text verification fetch failed" in finding.message
        and "network unavailable" in finding.message
        and "No extract was rendered" in finding.message
        for finding in findings
    )


def test_webdriver_guardian_bindings_force_source_text_extract_with_exact_targets():
    project_root = Path(__file__).resolve().parent.parent / "projects" / "webdriver-torso"
    artifacts = load_artifacts(project_root / "research" / "artifacts.json")
    guardian = [
        artifact
        for artifact in artifacts
        if artifact.artifact_id.startswith("wt-guardian-")
    ]

    assert len(guardian) == 19
    assert {artifact.capture_strategy for artifact in guardian} == {
        "source-text-extract"
    }
    assert all(assets_module._capture_exact_text(artifact.capture_spec) for artifact in guardian)


def test_execute_plan_fetches_direct_source_image_once_for_distinct_slot_crops(
    tmp_path, monkeypatch
):
    slots = [
        _slot("s062", "screenshot", start=0.0, end=1.0),
        _slot("s063", "screenshot", start=1.0, end=2.0),
    ]
    url = "https://upload.wikimedia.org/example.jpg"
    artifacts = [
        _artifact(
            f"wt-commons-shortwave-sx115-{slot.slot_id}",
            url,
            slot_id=slot.slot_id,
            acquisition_mode="screenshot-only",
            capture_strategy="licensed-direct-image-crop-deferred",
            source_license="CC0-1.0",
        )
        for slot in slots
    ]
    fetches = []
    derives = []

    def fake_fetch(_url, _transport):
        fetches.append(_url)
        return b"retained image bytes"

    def fake_derive(source_path, out_path, **kwargs):
        derives.append((source_path, out_path, kwargs))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"derived image video")
        return out_path

    monkeypatch.setattr(assets_module.capture, "fetch_source_bytes", fake_fetch)
    monkeypatch.setattr(
        assets_module.image_video, "derive_source_image_video", fake_derive
    )

    records, findings = execute_plan(
        plan_assets(slots, [], artifacts),
        slots,
        tmp_path,
        grade=GRADE,
        claims=[],
        artifacts=artifact_bindings_by_slot(artifacts),
        media_prober=lambda _path: True,
    )

    assert [finding for finding in findings if finding.severity == "error"] == []
    assert fetches == [url]
    assert len(derives) == 2
    assert len({call[1] for call in derives}) == 2
    assert [record.provider for record in records] == [
        "rabbithole-source-image",
        "rabbithole-source-image",
    ]
    assert [record.license for record in records] == ["CC0-1.0", "CC0-1.0"]
    assert all("source_license='CC0-1.0'" in record.notes for record in records)


@pytest.mark.parametrize(
    "source_license,reason",
    [
        ("", "has no source_license"),
        (
            "https://creativecommons.org/publicdomain/zero/1.0/",
            "invalid source_license",
        ),
        ("CC0 1.0 Universal", "invalid source_license"),
        ("CCO-1.0", "invalid source_license"),
        ("NOPE", "invalid source_license"),
        ("copyrighted", "invalid source_license"),
        (123, "invalid source_license"),
    ],
)
def test_direct_source_image_requires_explicit_machine_readable_license(
    source_license, reason
):
    slot = _slot("s062", "screenshot")
    artifact = _artifact(
        "wt-commons-shortwave-sx115-s062",
        "https://upload.wikimedia.org/example.jpg",
        slot_id=slot.slot_id,
        acquisition_mode="screenshot-only",
        capture_strategy="licensed-direct-image-crop-deferred",
        source_license=source_license,
    )

    [item] = plan_assets([slot], [], [artifact])

    assert item.action == "blocked"
    assert reason in item.reason


def test_direct_source_image_accepts_documented_custom_license_ref():
    slot = _slot("s062", "screenshot")
    artifact = _artifact(
        "permissioned-image-s062",
        "https://example.test/permissioned.jpg",
        slot_id=slot.slot_id,
        acquisition_mode="screenshot-only",
        capture_strategy="licensed-direct-image-crop-deferred",
        source_license="LicenseRef-Permission-Granted",
        rights_note="The photographer granted written permission for this episode.",
        source_attribution="Example Photographer",
    )

    [item] = plan_assets([slot], [], [artifact])

    assert item.action == "shoot"


@pytest.mark.parametrize(
    "missing_field,reason",
    [
        ("rights_note", "has no rights_note"),
        ("source_attribution", "has no source_attribution"),
    ],
)
def test_direct_source_image_custom_license_ref_requires_documentation(
    missing_field, reason
):
    slot = _slot("s062", "screenshot")
    values = {
        "rights_note": "The photographer granted written permission.",
        "source_attribution": "Example Photographer",
    }
    values[missing_field] = ""
    artifact = _artifact(
        "permissioned-image-s062",
        "https://example.test/permissioned.jpg",
        slot_id=slot.slot_id,
        acquisition_mode="screenshot-only",
        capture_strategy="licensed-direct-image-crop-deferred",
        source_license="LicenseRef-Permission-Granted",
        **values,
    )

    [item] = plan_assets([slot], [], [artifact])

    assert item.action == "blocked"
    assert reason in item.reason

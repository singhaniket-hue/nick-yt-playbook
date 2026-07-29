import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

import rabbithole.contactsheet as contactsheet
from rabbithole.contactsheet import (
    GROUPS,
    load_preview,
    main,
    plan_review_items,
    render_contact_sheets,
    review_kind,
)
from rabbithole.provenance import AssetRecord, save_provenance


def _timing(kinds):
    markers = [
        {
            "kind": "SHOT",
            "arg": f"{kind} detail {index}",
            "word_index": index,
            "line": index + 1,
            "seconds": float(index * 2),
        }
        for index, kind in enumerate(kinds)
    ]
    return {
        "duration_seconds": float(len(kinds) * 2),
        "word_count": len(kinds),
        "words": [],
        "markers": markers,
    }


def _record(asset_id, slot_id, local_path=None):
    return AssetRecord(
        asset_id=asset_id,
        tier="atmospheric" if asset_id.startswith("card") else "primary",
        provider="rabbithole-cards" if asset_id.startswith("card") else "source",
        original_url="" if asset_id.startswith("card") else "https://example.test/",
        license="" if asset_id.startswith("card") else "commentary-use",
        retrieved_at="2026-07-29T00:00:00Z",
        local_path=local_path or f"assets/{asset_id}.png",
        used_in_slots=(slot_id,),
        notes="",
    )


def _solid_loader(item, project_root, frame_size, sample_seconds):
    # The colour makes each generated cell non-empty without reading source
    # media; the renderer remains under test, not Pillow/ffmpeg decoding.
    shade = 60 + item.timeline_order * 20
    return Image.new("RGB", frame_size, (shade, 30, 30))


def test_plan_is_timeline_ordered_and_groups_graphics_from_evidence():
    document = _timing(["screenshot", "graphic", "capture", "plate"])
    records = [
        _record("z-card", "s002"),
        _record("capture-s003", "s003"),
        _record("capture-s001", "s001"),
        _record("a-card", "s002"),
        # s004 is intentionally absent: missing media must remain visible.
    ]

    items = plan_review_items(document, records)

    assert [
        (item.slot.slot_id, item.asset_id)
        for item in items
        if item.group == "graphics"
    ] == [
        ("s002", "a-card"),
        ("s002", "z-card"),
        ("s004", ""),
    ]
    assert [
        item.slot.slot_id for item in items if item.group == "evidence"
    ] == ["s001", "s003"]


def test_review_kind_discloses_derived_evidence_instead_of_calling_it_a_screenshot():
    document = _timing(["screenshot", "screenshot", "screenshot"])
    records = [
        _record("capture-s001", "s001"),
        _record("capture-s002", "s002"),
        _record("capture-s003", "s003"),
    ]
    records[0] = replace(records[0], provider="rabbithole-evidence-card")
    records[1] = replace(records[1], provider="rabbithole-source-frame")
    records[2] = replace(records[2], provider="rabbithole-source-image")

    items = plan_review_items(document, records)

    assert [review_kind(item) for item in items] == [
        "CITATION CARD",
        "SOURCE FRAME",
        "SOURCE IMAGE",
    ]


def test_render_paginates_each_group_separately_and_writes_manifest(tmp_path):
    document = _timing(["graphic", "screenshot", "graphic", "capture", "plate"])
    records = [
        _record("card-s001", "s001"),
        _record("capture-s002", "s002"),
        _record("card-s003", "s003"),
        _record("capture-s004", "s004"),
        _record("card-s005", "s005"),
    ]
    items = list(reversed(plan_review_items(document, records)))

    first = render_contact_sheets(
        items,
        tmp_path / "sheets",
        project_root=tmp_path,
        episode_label="test-episode",
        columns=2,
        rows=1,
        cell_width=240,
        frame_loader=_solid_loader,
    )
    first_bytes = {
        path.name: path.read_bytes()
        for group in GROUPS
        for path in first.pages[group]
    }

    # Input order is deliberately reversed, and a second run must still produce
    # the same named pages and pixels.
    second = render_contact_sheets(
        items,
        tmp_path / "sheets",
        project_root=tmp_path,
        episode_label="test-episode",
        columns=2,
        rows=1,
        cell_width=240,
        frame_loader=_solid_loader,
    )

    assert [path.name for path in second.pages["graphics"]] == [
        "graphics-001.png",
        "graphics-002.png",
    ]
    assert [path.name for path in second.pages["evidence"]] == [
        "evidence-001.png"
    ]
    assert second.item_counts == {"graphics": 3, "evidence": 2}
    assert second.issues == ()
    assert {
        path.name: path.read_bytes()
        for group in GROUPS
        for path in second.pages[group]
    } == first_bytes

    manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "rabbithole-contact-sheets.v1"
    assert manifest["groups"]["graphics"] == {
        "item_count": 3,
        "pages": ["graphics-001.png", "graphics-002.png"],
    }
    assert manifest["groups"]["evidence"] == {
        "item_count": 2,
        "pages": ["evidence-001.png"],
    }


def test_missing_and_unreadable_items_become_red_cards_not_aborted_pages(tmp_path):
    document = _timing(["graphic", "screenshot", "capture"])
    records = [
        _record("card-good", "s001"),
        _record("capture-bad", "s002"),
        # s003 has no provenance claimant.
    ]
    items = plan_review_items(document, records)

    def loader(item, project_root, frame_size, sample_seconds):
        if item.asset_id == "capture-bad":
            raise OSError("decoder rejected this file")
        if item.asset is None:
            raise FileNotFoundError("no provenance asset claims this slot")
        return Image.new("RGB", frame_size, "navy")

    result = render_contact_sheets(
        items,
        tmp_path / "sheets",
        project_root=tmp_path,
        columns=3,
        rows=1,
        cell_width=240,
        frame_loader=loader,
    )

    assert {issue.slot_id for issue in result.issues} == {"s002", "s003"}
    assert all(path.is_file() for group in GROUPS for path in result.pages[group])
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert [issue["slot_id"] for issue in manifest["issues"]] == ["s002", "s003"]


def test_load_preview_resolves_project_relative_still_and_letterboxes(tmp_path):
    source = tmp_path / "assets" / "portrait.png"
    source.parent.mkdir()
    Image.new("RGB", (100, 200), "orange").save(source)
    item = plan_review_items(
        _timing(["screenshot"]),
        [_record("capture-s001", "s001", "assets/portrait.png")],
    )[0]

    preview = load_preview(item, tmp_path, (320, 180), 0.5)

    assert preview.size == (320, 180)
    assert preview.getpixel((160, 90)) == (255, 165, 0)
    assert preview.getpixel((0, 0)) == (5, 6, 7)


def test_load_preview_samples_video_through_ffmpeg_without_temp_frames(
    tmp_path, monkeypatch
):
    source = tmp_path / "assets" / "clip.mp4"
    source.parent.mkdir()
    source.write_bytes(b"test fixture; subprocess is injected")
    encoded = tmp_path / "frame.png"
    Image.new("RGB", (320, 180), "teal").save(encoded)
    png_bytes = encoded.read_bytes()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=png_bytes, stderr=b"")

    monkeypatch.setattr(contactsheet.subprocess, "run", fake_run)
    item = plan_review_items(
        _timing(["capture"]),
        [_record("capture-s001", "s001", "assets/clip.mp4")],
    )[0]

    preview = load_preview(item, tmp_path, (320, 180), 0.75)

    assert preview.getpixel((160, 90)) == (0, 128, 128)
    assert len(calls) == 1
    assert calls[0][0][calls[0][0].index("-ss") + 1] == "0.750"
    assert "pipe:1" in calls[0][0]
    assert calls[0][1]["timeout"] == 30


def test_module_main_uses_inferred_provenance_and_separate_default_groups(tmp_path):
    project = tmp_path / "portable-episode"
    narration = project / "narration"
    assets = project / "assets"
    narration.mkdir(parents=True)
    assets.mkdir()
    timing_path = narration / "timing.json"
    timing_path.write_text(
        json.dumps(_timing(["graphic", "screenshot"])), encoding="utf-8"
    )
    Image.new("RGB", (320, 180), "black").save(assets / "graphic.png")
    Image.new("RGB", (320, 180), "white").save(assets / "evidence.png")
    save_provenance(
        project / "provenance.json",
        [
            _record("card-s001", "s001", "assets/graphic.png"),
            _record("capture-s002", "s002", "assets/evidence.png"),
        ],
    )
    output = project / "qa"

    exit_code = main(
        [
            str(timing_path),
            "--output",
            str(output),
            "--columns",
            "1",
            "--rows",
            "1",
            "--cell-width",
            "240",
        ]
    )

    assert exit_code == 0
    assert (output / "graphics-001.png").is_file()
    assert (output / "evidence-001.png").is_file()
    assert json.loads((output / "contact-sheets.json").read_text())["issues"] == []

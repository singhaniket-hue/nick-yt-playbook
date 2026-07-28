from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from rabbithole.resolve_style import (
    ResolveStyleError,
    apply_style_to_new_timeline,
    style_contract_hash,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _style(root: Path, *, with_drx: bool = False) -> dict:
    lut = root / "style" / "luts" / "crowley-noir.cube"
    lut.parent.mkdir(parents=True)
    lut.write_text('TITLE "test"\nLUT_3D_SIZE 2\n', encoding="utf-8")
    drx = root / "resolve" / "grades" / "crowley_v1.drx"
    if with_drx:
        drx.parent.mkdir(parents=True)
        drx.write_bytes(b"versioned-drx")
    value = {
        "schema_version": "resolve-style.v1",
        "grade": {
            "policy": "archival_v1_only",
            "grade_mode": 0,
            "eligible_slot_kinds": ["archival"],
            "clip_ids": ["clip-1"],
            "drx_path": "resolve/grades/crowley_v1.drx",
            "installed_drx_path": "RabbitHole/grades/crowley_v1.drx",
            "drx_sha256": _sha(drx) if with_drx else None,
            "lut_path": "style/luts/crowley-noir.cube",
            "installed_lut_path": "RabbitHole/crowley-noir.cube",
            "lut_sha256": _sha(lut),
        },
        "fusion": {
            "automation": "intent_only_api_placement_unavailable",
        },
        "texture": {"automation": "editable_manual_intent", "track": "V4"},
    }
    value["contract_sha256"] = hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert style_contract_hash(value) == value["contract_sha256"]
    return value


class FakeGraph:
    def __init__(self, *, drx_result: bool = False, lut_result: bool = True):
        self.drx_result = drx_result
        self.lut_result = lut_result
        self.drx_calls: list[tuple[str, int]] = []
        self.lut_calls: list[tuple[int, str]] = []

    def GetNumNodes(self):
        return 1

    def ApplyGradeFromDRX(self, path, mode):
        self.drx_calls.append((path, mode))
        return self.drx_result

    def SetLUT(self, node, path):
        self.lut_calls.append((node, path))
        return self.lut_result


class FakeItem:
    def __init__(self, name: str, start: int, graph: FakeGraph):
        self.name = name
        self.start = start
        self.graph = graph

    def GetName(self):
        return self.name

    def GetStart(self, subframe_precision=False):
        return self.start

    def GetNodeGraph(self):
        return self.graph


class FakeTimeline:
    def __init__(self, items: dict[int, list[FakeItem]], start: int = 86400):
        self.items = items
        self.start = start

    def GetStartFrame(self):
        return self.start

    def GetItemListInTrack(self, kind, index):
        assert kind == "video"
        return self.items.get(index, [])


class FakeProject:
    def __init__(self):
        self.refresh_calls = 0

    def RefreshLUTList(self):
        self.refresh_calls += 1
        return True


def _plan(style: dict) -> dict:
    return {
        "style": style,
        "clips": [
            {
                "id": "clip-1",
                "slot_id": "s001",
                "asset_id": "archive-1",
                "track": "V1",
                "start_frame": 30,
            },
            {
                "id": "clip-evidence",
                "slot_id": "s002",
                "asset_id": "evidence-1",
                "track": "V2",
                "start_frame": 60,
            },
        ],
    }


def test_drx_is_preferred_and_only_declared_clip_is_styled(tmp_path: Path) -> None:
    style = _style(tmp_path, with_drx=True)
    archival_graph = FakeGraph(drx_result=True)
    evidence_graph = FakeGraph()
    timeline = FakeTimeline(
        {
            1: [FakeItem("s001", 86430, archival_graph)],
            2: [FakeItem("s002", 86460, evidence_graph)],
        }
    )
    project = FakeProject()

    result = apply_style_to_new_timeline(
        project, timeline, _plan(style), project_root=tmp_path
    )

    assert result["status"] == "applied"
    assert result["eligible_clip_count"] == 1
    assert result["applied_clip_count"] == 1
    assert archival_graph.drx_calls == [
        (str(tmp_path / "resolve" / "grades" / "crowley_v1.drx"), 0)
    ]
    assert archival_graph.lut_calls == []
    assert evidence_graph.drx_calls == []
    assert evidence_graph.lut_calls == []
    assert project.refresh_calls == 1


def test_rejected_drx_falls_back_to_checksum_verified_lut(tmp_path: Path) -> None:
    style = _style(tmp_path, with_drx=True)
    graph = FakeGraph(drx_result=False, lut_result=True)
    timeline = FakeTimeline({1: [FakeItem("s001", 86430, graph)]})

    result = apply_style_to_new_timeline(
        FakeProject(), timeline, _plan(style), project_root=tmp_path
    )

    assert result["status"] == "applied"
    assert result["clips"][0]["method"] == "lut"
    assert graph.drx_calls
    assert graph.lut_calls == [
        (1, str(tmp_path / "style" / "luts" / "crowley-noir.cube"))
    ]


def test_missing_or_changed_style_asset_records_manual_intent(
    tmp_path: Path,
) -> None:
    style = _style(tmp_path)
    (tmp_path / "style" / "luts" / "crowley-noir.cube").write_text(
        "changed after compile\n", encoding="utf-8"
    )
    graph = FakeGraph()
    timeline = FakeTimeline({1: [FakeItem("s001", 86430, graph)]})

    result = apply_style_to_new_timeline(
        FakeProject(), timeline, _plan(style), project_root=tmp_path
    )

    assert result["status"] == "manual_required"
    assert result["clips"][0]["reason"] == "versioned_grade_assets_unavailable"
    assert graph.drx_calls == []
    assert graph.lut_calls == []


def test_style_contract_tampering_is_rejected_before_api_calls(
    tmp_path: Path,
) -> None:
    style = _style(tmp_path)
    style["grade"]["policy"] = "grade_everything"
    project = FakeProject()

    with pytest.raises(ResolveStyleError, match="checksum"):
        apply_style_to_new_timeline(
            project, FakeTimeline({}), _plan(style), project_root=tmp_path
        )

    assert project.refresh_calls == 0

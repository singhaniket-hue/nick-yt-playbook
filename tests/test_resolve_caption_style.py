from __future__ import annotations

from pathlib import Path

import pytest

import rabbithole.resolve_service as service
from rabbithole.resolve_manifest import _compile_presentation_subtitles


def _plan() -> dict:
    return {
        "build_id": "b-caption1234",
        "timeline_name": "AUTO_BUILD_CAPTION1234",
        "subtitle_policy": {
            "track_style": {
                "render_approval_required": True,
                "contract_sha256": "a" * 64,
            }
        },
    }


def test_presentation_policy_requires_a_readable_track_style():
    presentation, policy = _compile_presentation_subtitles(
        [
            {
                "id": "subtitle-1",
                "start_frame": 0,
                "end_frame": 30,
                "duration_frames": 30,
                "text": "Readable caption",
            }
        ],
        clips=[],
        overlays=[],
        provenance=[],
        slot_metadata={},
        authored_exclusions=[],
        duration_frames=30,
    )

    assert len(presentation) == 1
    style = policy["track_style"]
    assert style["track_name"] == "PRESENTATION_SUBTITLES"
    assert style["font_color"] == "#FFFFFF"
    assert style["background_color"] == "#000000"
    assert style["minimum_background_opacity"] == 0.65
    assert style["application"] == "manual-resolve-track-style"
    assert style["render_approval_required"] is True
    assert len(style["contract_sha256"]) == 64


def test_caption_style_approval_is_scoped_to_built_timeline_and_host(
    tmp_path, monkeypatch
):
    root = tmp_path / "episode"
    root.mkdir()
    plan = _plan()
    monkeypatch.setattr(service, "compile_resolve_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        service,
        "_caption_style_host",
        lambda: {
            "system": "TestOS",
            "release": "1",
            "machine": "test64",
            "node": "test-host",
        },
    )
    monkeypatch.setattr(service, "utc_now", lambda: "2026-08-01T00:00:00Z")

    missing = service.caption_style_approval_status(root)
    assert missing["required"] is True
    assert missing["approved"] is False
    assert missing["reason"] == "approval_missing"

    monkeypatch.setattr(
        service,
        "read_status",
        lambda *_args, **_kwargs: {
            "state": "succeeded",
            "detail": "build_succeeded",
            "timeline_name": plan["timeline_name"],
        },
    )
    approved = service.approve_caption_style(root, note="White on black checked")
    assert approved["approved"] is True
    assert approved["reason"] == "approved_on_this_host"
    assert approved["note"] == "White on black checked"
    assert Path(approved["approval_path"]).is_file()

    monkeypatch.setattr(
        service,
        "_caption_style_host",
        lambda: {
            "system": "OtherOS",
            "release": "2",
            "machine": "arm64",
            "node": "other-host",
        },
    )
    transferred = service.caption_style_approval_status(root)
    assert transferred["approved"] is False
    assert transferred["reason"] == "approval_contract_mismatch"
    assert transferred["mismatched_fields"] == ["host"]


def test_caption_style_cannot_be_approved_before_exact_build_succeeds(
    tmp_path, monkeypatch
):
    root = tmp_path / "episode"
    root.mkdir()
    monkeypatch.setattr(service, "compile_resolve_plan", lambda *args, **kwargs: _plan())
    monkeypatch.setattr(
        service,
        "read_status",
        lambda *_args, **_kwargs: {
            "state": "queued",
            "detail": "awaiting_in_app_runner",
            "timeline_name": None,
        },
    )

    with pytest.raises(service.ResolveServiceError, match="only after this exact"):
        service.approve_caption_style(root)


def test_render_queue_fails_closed_until_caption_style_is_approved(
    tmp_path, monkeypatch
):
    root = tmp_path / "episode"
    root.mkdir()
    plan_path = root / "resolve" / "builds" / "b-caption" / "resolve-plan.v1.json"
    monkeypatch.setattr(
        service,
        "prepare_project",
        lambda *args, **kwargs: {
            "plan_path": str(plan_path),
            "summary": {
                "blocking_review_flags": 0,
                "caption_style": {"required": True, "approved": False},
            },
        },
    )

    with pytest.raises(service.ResolveServiceError, match="render not queued"):
        service.queue_project_action(root, "render", mode="free")

from pathlib import Path
from types import SimpleNamespace

import pytest

import rabbithole.resolve_service as service


def _probe_result(
    duration: float,
    *,
    frame_rate: str = "30/1",
    nb_frames: int | None = None,
    format_duration: float | None = None,
    returncode: int = 0,
):
    frames = round(duration * 30) if nb_frames is None else nb_frames
    format_section = (
        f', "format": {{"duration": "{format_duration}"}}'
        if format_duration is not None
        else ""
    )
    return SimpleNamespace(
        returncode=returncode,
        stdout=(
            '{"streams": [{"duration": "'
            f'{duration}", "nb_frames": "{frames}", '
            f'"avg_frame_rate": "{frame_rate}"}}]{format_section}}}'
        ),
        stderr="" if returncode == 0 else "probe failed",
    )


def test_video_source_range_audit_probes_each_file_once(tmp_path, monkeypatch):
    root = tmp_path / "episode"
    root.mkdir()
    calls = []
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/{name}")

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _probe_result(10.0)

    monkeypatch.setattr(service.subprocess, "run", fake_run)
    plan = {
        "fps": 30,
        "clips": [
            {
                "id": "clip-a",
                "media_type": "video",
                "media_path": "assets/source.mp4",
                "source_end_frame": 300,
            },
            {
                "id": "clip-b",
                "media_type": "video",
                "media_path": "assets/source.mp4",
                "source_end_frame": 150,
            },
            {
                "id": "still",
                "media_type": "image",
                "media_path": "assets/source.png",
                "source_end_frame": 900,
            },
        ],
    }

    result = service._audit_video_source_ranges(root, plan)

    assert result["ok"] is True
    assert result["clip_count"] == 2
    assert result["unique_media_count"] == 1
    assert len(calls) == 1
    assert calls[0][0][-1] == str((root / "assets" / "source.mp4").resolve())


def test_video_source_range_audit_fails_for_unavailable_terminal_frame(
    tmp_path, monkeypatch
):
    root = tmp_path / "episode"
    root.mkdir()
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/{name}")
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda *args, **kwargs: _probe_result(9.99),
    )
    plan = {
        "fps": 30,
        "clips": [
            {
                "id": "clip-a",
                "media_type": "video",
                "media_path": "assets/source.mp4",
                "source_end_frame": 300,
            }
        ],
    }

    result = service._audit_video_source_ranges(root, plan)

    assert result["ok"] is False
    assert result["probe_failures"] == []
    assert result["shortages"][0]["required_end_frame"] == 300
    assert result["shortages"][0]["clip_ids"] == ["clip-a"]


def test_video_source_range_audit_ignores_longer_container_duration(
    tmp_path, monkeypatch
):
    root = tmp_path / "episode"
    root.mkdir()
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/{name}")
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda *args, **kwargs: _probe_result(
            9.9,
            nb_frames=297,
            format_duration=10.5,
        ),
    )
    plan = {
        "fps": 30,
        "clips": [
            {
                "id": "clip-a",
                "media_type": "video",
                "media_path": "assets/source.mp4",
                "source_end_frame": 300,
            }
        ],
    }

    result = service._audit_video_source_ranges(root, plan)

    assert result["ok"] is False
    assert result["shortages"][0]["available_seconds"] == 9.9


def test_video_source_range_audit_allows_mp4_timescale_rounding(
    tmp_path, monkeypatch
):
    root = tmp_path / "episode"
    root.mkdir()
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/{name}")
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda *args, **kwargs: _probe_result(9.9995),
    )
    plan = {
        "fps": 30,
        "clips": [
            {
                "id": "clip-a",
                "media_type": "video",
                "media_path": "assets/source.mp4",
                "source_end_frame": 300,
            }
        ],
    }

    result = service._audit_video_source_ranges(root, plan)

    assert result["ok"] is True
    assert result["shortages"] == []


def test_free_preflight_keeps_console_python_as_manual_warning(
    tmp_path, monkeypatch
):
    root = tmp_path / "episode"
    root.mkdir()
    monkeypatch.setattr(
        service,
        "_resolve_installation",
        lambda: {
            "installed": True,
            "executable": "Resolve.exe",
            "scripting_module_available": False,
            "script_api": None,
            "script_library": None,
        },
    )
    monkeypatch.setattr(service.shutil, "which", lambda name: f"C:/{name}.exe")
    monkeypatch.setattr(
        service,
        "compile_resolve_plan",
        lambda *args, **kwargs: {
            "build_id": "b-12345678",
            "timeline_name": "AUTO_BUILD_12345678",
            "clips": [],
            "missing_media": [],
            "review_flags": [],
        },
    )

    result = service.preflight_project(root, mode="free")

    console_check = next(
        check for check in result["checks"] if check["name"] == "console_python_version"
    )
    assert result["ok"] is True
    assert console_check["ok"] is False
    assert console_check["severity"] == "warning"
    assert "3.11+" in console_check["detail"]


def test_prepare_keeps_output_under_project_resolve(tmp_path, monkeypatch):
    root = tmp_path / "episode"
    root.mkdir()
    expected = root / "resolve" / "builds" / "b-123"

    def fake_bundle(project_root, output_dir=None, overrides_path=None):
        assert project_root == root
        assert output_dir == expected
        return {
            "build_id": "b-123",
            "timeline_name": "AUTO_BUILD_12345678",
            "plan_path": expected / "resolve-plan.v1.json",
            "fcpxml_path": expected / "timeline.fcpxml",
            "plan": {"large": ["omitted by callers only when requested"]},
        }

    monkeypatch.setattr(service, "write_resolve_bundle", fake_bundle)
    result = service.prepare_project(root, output_dir="resolve/builds/b-123")
    assert result["plan_path"].endswith("resolve-plan.v1.json")

    with pytest.raises(service.ResolveServiceError, match="must stay under"):
        service.prepare_project(root, output_dir=tmp_path / "outside")


def test_free_queue_returns_console_loader_without_external_connection(tmp_path, monkeypatch):
    root = tmp_path / "episode"
    root.mkdir()
    plan = root / "resolve" / "builds" / "b" / "resolve-plan.v1.json"
    calls = []
    monkeypatch.setattr(
        service,
        "prepare_project",
        lambda *args, **kwargs: {"plan_path": str(plan), "build_id": "b"},
    )
    monkeypatch.setattr(
        service,
        "enqueue_job",
        lambda *args, **kwargs: {
            "job_id": "job-1",
            "state": "queued",
            "console_loader": "loader()",
        },
    )
    monkeypatch.setattr(
        service, "read_status", lambda *args: {"state": "queued"}
    )
    monkeypatch.setattr(
        service,
        "run_pending_jobs",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = service.queue_project_action(root, "build", mode="free")
    assert result["detail"] == "awaiting_in_app_runner"
    assert result["console_loader"] == "loader()"
    assert calls == []


def test_queue_refuses_compiled_plan_with_blocking_reviews(tmp_path, monkeypatch):
    root = tmp_path / "episode"
    root.mkdir()
    monkeypatch.setattr(
        service,
        "prepare_project",
        lambda *args, **kwargs: {
            "plan_path": str(root / "resolve" / "plan.json"),
            "summary": {"blocking_review_flags": 2},
        },
    )

    with pytest.raises(service.ResolveServiceError, match="not queued"):
        service.queue_project_action(root, "build", mode="free")


def test_studio_queue_is_explicitly_executed(tmp_path, monkeypatch):
    root = tmp_path / "episode"
    root.mkdir()
    monkeypatch.setattr(
        service,
        "prepare_project",
        lambda *args, **kwargs: {"plan_path": str(root / "resolve" / "plan.json")},
    )
    monkeypatch.setattr(
        service,
        "enqueue_job",
        lambda *args, **kwargs: {"job_id": "job-2", "state": "queued"},
    )
    monkeypatch.setattr(
        service,
        "read_status",
        lambda *args: {"state": "succeeded"},
    )
    observed = {}

    def fake_run(**kwargs):
        observed.update(kwargs)
        return [{"state": "succeeded"}]

    monkeypatch.setattr(service, "run_pending_jobs", fake_run)
    result = service.queue_project_action(root, "build", mode="studio")
    assert result["execution"][0]["state"] == "succeeded"
    assert observed["allow_studio_external"] is True

from pathlib import Path

import pytest

import rabbithole.resolve_service as service


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

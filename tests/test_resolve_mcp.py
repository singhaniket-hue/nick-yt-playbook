from pathlib import Path

import rabbithole.resolve_mcp as resolve_mcp


def test_free_build_tool_delegates_to_durable_queue(tmp_path, monkeypatch):
    observed = {}

    def fake_queue(project_root, action, **kwargs):
        observed.update(
            {
                "project_root": project_root,
                "action": action,
                **kwargs,
            }
        )
        return {"detail": "awaiting_in_app_runner"}

    monkeypatch.setattr(resolve_mcp, "queue_project_action", fake_queue)
    result = resolve_mcp.resolve_build(str(tmp_path), mode="free")
    assert result["detail"] == "awaiting_in_app_runner"
    assert observed["project_root"] == Path(tmp_path)
    assert observed["action"] == "build"
    assert observed["mode"] == "free"


def test_handoff_tool_passes_full_source_options(tmp_path, monkeypatch):
    observed = {}

    def fake_queue(project_root, action, **kwargs):
        observed.update({"action": action, **kwargs})
        return {"job": {"state": "queued"}}

    monkeypatch.setattr(resolve_mcp, "queue_project_action", fake_queue)
    destination = tmp_path / "handoff"
    resolve_mcp.resolve_handoff(
        str(tmp_path),
        str(destination),
        include_proxy_media=True,
    )
    assert observed["action"] == "handoff"
    assert observed["options"]["destination"] == str(destination.resolve())
    assert observed["options"]["include_proxy_media"] is True

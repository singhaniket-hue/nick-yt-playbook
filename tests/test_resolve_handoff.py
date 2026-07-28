from __future__ import annotations

import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import zipfile

import pytest

from rabbithole.resolve_handoff import (
    HandoffValidationError,
    ResolveHandoffError,
    package_handoff,
    restore_handoff,
    validate_handoff,
    validate_handoff_output_root,
)
from rabbithole.resolve_safety import ResolveBusyError, UnsafeWriteError


class FakeTimeline:
    def __init__(self, name: str) -> None:
        self.name = name

    def GetName(self):
        return self.name


class FakeProject:
    def __init__(
        self,
        timeline_name: str = "AUTO_BUILD_DEADBEEFCAFE",
        *,
        rendering: bool = False,
    ) -> None:
        self.name = "Editor Current Project"
        self.rendering = rendering
        self.timelines = [FakeTimeline("EDITORIAL_v1"), FakeTimeline(timeline_name)]

    def GetName(self):
        return self.name

    def IsRenderingInProgress(self):
        return self.rendering

    def GetTimelineCount(self):
        return len(self.timelines)

    def GetTimelineByIndex(self, index):
        return self.timelines[index - 1]


class FakeManager:
    def __init__(self, project: FakeProject) -> None:
        self.project = project
        self.archive_calls: list[tuple] = []
        self.export_calls: list[tuple] = []

    def GetCurrentProject(self):
        return self.project

    def ArchiveProject(self, *args):
        self.archive_calls.append(args)
        dra = Path(args[1])
        dra.mkdir()
        (dra / "source-media.bin").write_bytes(b"source media")
        return True

    def ExportProject(self, *args):
        self.export_calls.append(args)
        Path(args[1]).write_bytes(b"resolve project export")
        return True


class FakeResolve:
    def __init__(self, project: FakeProject) -> None:
        self.manager = FakeManager(project)

    def GetProjectManager(self):
        return self.manager

    def GetVersionString(self):
        return "21.0.1"


def _plan(project: Path) -> dict:
    media = project / "assets" / "clip.mp4"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"x" * 1024)
    return {
        "build_id": "b-deadbeefcafe",
        "timeline_name": "AUTO_BUILD_DEADBEEFCAFE",
        "provenance": [
            {
                "asset_id": "clip",
                "local_path": "assets/clip.mp4",
            }
        ],
        "audio": [{"media_path": "assets/clip.mp4"}],
    }


def test_source_inclusive_handoff_manifest_checksums_defaults_and_zip(
    tmp_path: Path,
) -> None:
    project = tmp_path / "episode"
    project.mkdir()
    plan = _plan(project)
    (project / "fonts").mkdir()
    (project / "fonts" / "EpisodeFont.txt").write_text("font placeholder")
    (project / "licenses").mkdir()
    (project / "licenses" / "EpisodeFont-LICENSE.txt").write_text("test only")
    (project / "chapters.txt").write_text("00:00 Opening")
    explicit_preset = tmp_path / "preset.setting"
    explicit_preset.write_text("preset")

    resolve = FakeResolve(FakeProject())
    hooks = []

    def hook(context):
        hooks.append(context.manifest["project_name"])
        return True

    result = package_handoff(
        project,
        resolve=resolve,
        plan=plan,
        destination=tmp_path / "handoffs",
        presets=[explicit_preset],
        validation_hooks=[hook],
    )
    assert hooks == ["Editor Current Project"]
    assert resolve.manager.archive_calls == [
        (
            "Editor Current Project",
            str(
                next((tmp_path / "handoffs").glob("RABBITHOLE_HANDOFF_*"))
                / "project.dra"
            ),
            True,
            False,
            False,
        )
    ] or resolve.manager.archive_calls[0][2:] == (True, False, False)
    # Staging is renamed after Resolve returns, so assert the stable safety args
    # separately from the temporary destination string.
    assert resolve.manager.archive_calls[0][0] == "Editor Current Project"
    assert resolve.manager.archive_calls[0][2:] == (True, False, False)
    assert resolve.manager.export_calls[0][0] == "Editor Current Project"
    assert resolve.manager.export_calls[0][2] is True

    package = Path(result["package_directory"])
    manifest = json.loads((package / "manifest.json").read_text())
    assert manifest["project_name"] == "Editor Current Project"
    assert manifest["archive"] == {
        "dra": "project.dra",
        "drp": "project.drp",
        "source_media": True,
        "render_cache": False,
        "proxy_media": False,
    }
    assert manifest["disk_space_preflight"]["unique_source_bytes"] == 1024
    assert manifest["disk_space_preflight"]["estimated_required_bytes"] > 2048
    assert (package / "fonts" / "fonts" / "EpisodeFont.txt").is_file()
    assert (
        package / "licenses" / "licenses" / "EpisodeFont-LICENSE.txt"
    ).is_file()
    assert (package / "presets" / "preset.setting").is_file()
    assert (package / "presets" / "grades" / "README.md").is_file()
    assert (
        package / "project-files" / "project" / "chapters.txt"
    ).is_file()
    assert Path(result["zip_path"]).is_file()
    assert Path(result["zip_path"] + ".sha256").is_file()
    assert validate_handoff(package)["valid"] is True
    assert validate_handoff(result["zip_path"])["valid"] is True


def test_checksum_tamper_is_detected_and_restore_hooks_run(tmp_path: Path) -> None:
    project = tmp_path / "episode"
    project.mkdir()
    result = package_handoff(
        project,
        resolve=FakeResolve(FakeProject()),
        plan=_plan(project),
        destination=tmp_path / "handoffs",
    )
    seen = []

    def hook(context):
        seen.append(context.restored_project)
        return True

    restored = restore_handoff(
        result["zip_path"],
        tmp_path / "restored",
        hooks=[hook],
        restored_project="manually-restored-project",
    )
    assert restored["resolve_import_performed"] is False
    assert seen == ["manually-restored-project"]

    package = Path(result["package_directory"])
    (package / "README.txt").write_text("tampered")
    with pytest.raises(HandoffValidationError, match="checksum mismatch"):
        validate_handoff(package)


def test_handoff_refuses_wrong_current_project_timeline_before_archive(
    tmp_path: Path,
) -> None:
    project = tmp_path / "episode"
    project.mkdir()
    resolve = FakeResolve(FakeProject("AUTO_BUILD_111111111111"))
    with pytest.raises(ResolveHandoffError, match="possibly wrong open project"):
        package_handoff(
            project,
            resolve=resolve,
            plan=_plan(project),
            destination=tmp_path / "handoffs",
        )
    assert resolve.manager.archive_calls == []


def test_active_render_and_insufficient_space_fail_before_archive(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "episode"
    project.mkdir()
    plan = _plan(project)
    busy = FakeResolve(FakeProject(rendering=True))
    with pytest.raises(ResolveBusyError):
        package_handoff(
            project,
            resolve=busy,
            plan=plan,
            destination=tmp_path / "busy-handoffs",
        )
    assert busy.manager.archive_calls == []

    resolve = FakeResolve(FakeProject())
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=10, used=9, free=1),
    )
    with pytest.raises(ResolveHandoffError, match="insufficient free space"):
        package_handoff(
            project,
            resolve=resolve,
            plan=plan,
            destination=tmp_path / "small-disk",
        )
    assert resolve.manager.archive_calls == []


def test_explicit_proxy_archive_policy_is_recorded(tmp_path: Path) -> None:
    project = tmp_path / "episode"
    project.mkdir()
    resolve = FakeResolve(FakeProject())
    result = package_handoff(
        project,
        resolve=resolve,
        plan=_plan(project),
        destination=tmp_path / "handoffs",
        include_proxy_media=True,
    )
    assert resolve.manager.archive_calls[0][2:] == (True, False, True)
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert manifest["archive"]["proxy_media"] is True
    assert validate_handoff(result["package_directory"])["valid"] is True


def test_disk_preflight_counts_external_media_without_leaking_absolute_path(
    tmp_path: Path,
) -> None:
    project = tmp_path / "episode"
    project.mkdir()
    plan = _plan(project)
    external = tmp_path / "external-source.mov"
    external.write_bytes(b"z" * 2048)
    plan["provenance"].append(
        {"asset_id": "external", "local_path": str(external.resolve())}
    )

    result = package_handoff(
        project,
        resolve=FakeResolve(FakeProject()),
        plan=plan,
        destination=tmp_path / "handoffs",
    )
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    preflight = manifest["disk_space_preflight"]
    assert preflight["unique_source_bytes"] == 3072
    assert any(path.endswith("/external-source.mov") for path in preflight["media_paths"])
    assert str(tmp_path.resolve()) not in json.dumps(preflight)


def test_external_output_root_and_zip_slip_are_rejected(tmp_path: Path) -> None:
    project = tmp_path / "episode"
    project.mkdir()
    with pytest.raises(UnsafeWriteError, match="filesystem root"):
        validate_handoff_output_root(
            Path(tmp_path.anchor),
            project_root=project,
        )

    malicious = tmp_path / "malicious.zip"
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr("../escape.txt", "no")
    with pytest.raises(HandoffValidationError, match="unsafe path"):
        validate_handoff(malicious)

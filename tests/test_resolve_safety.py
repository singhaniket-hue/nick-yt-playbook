from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from rabbithole.resolve_safety import (
    ProcessProbe,
    ProjectLock,
    ResolveBusyError,
    ResolveLockError,
    UnsafeWriteError,
    normalize_write_roots,
    require_render_idle,
)


class _Project:
    def __init__(self, state):
        self.state = state

    def IsRenderingInProgress(self):
        if isinstance(self.state, BaseException):
            raise self.state
        return self.state


def test_project_lock_records_process_identity_stage_and_roots(tmp_path: Path) -> None:
    project = tmp_path / "episode"
    output = project / "resolve"
    with ProjectLock(project, stage="build", write_roots=(output,)) as lock:
        payload = json.loads(lock.path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
        assert payload["process_start"] > 0
        assert payload["stage"] == "build"
        assert payload["write_roots"] == [str(output.resolve())]
        assert payload["heartbeat_at"]
        lock.assert_write_path(output / "builds" / "plan.json")
        with pytest.raises(UnsafeWriteError):
            lock.assert_write_path(project / "assets" / "source.mp4")
    assert not lock.path.exists()


def test_live_owner_and_unreadable_lock_fail_closed(tmp_path: Path) -> None:
    project = tmp_path / "episode"
    first = ProjectLock(project, stage="build").acquire()
    try:
        with pytest.raises(ResolveLockError, match="locked by PID"):
            ProjectLock(project, stage="render").acquire()
    finally:
        first.release()

    malformed = project / "resolve" / ".resolve-runner.lock"
    malformed.parent.mkdir(parents=True, exist_ok=True)
    malformed.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ResolveLockError, match="cannot be validated"):
        ProjectLock(project, stage="render").acquire()


def test_pid_reuse_stale_lock_is_reclaimed(tmp_path: Path) -> None:
    project = tmp_path / "episode"
    lock_path = project / "resolve" / ".resolve-runner.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pid": 444,
                "process_start": 10.0,
                "stage": "old",
                "write_roots": [str(project / "resolve")],
                "project_root": str(project),
                "created_at": "2026-01-01T00:00:00Z",
                "heartbeat_at": "2026-01-01T00:00:00Z",
                "hostname": "test",
                "token": "old-token",
            }
        ),
        encoding="utf-8",
    )
    lock = ProjectLock(
        project,
        stage="new",
        process_probe=lambda pid: ProcessProbe(True, 99.0),
    ).acquire()
    try:
        assert lock.metadata is not None
        assert lock.metadata.stage == "new"
    finally:
        lock.release()


def test_unknown_process_state_is_not_treated_as_stale(tmp_path: Path) -> None:
    project = tmp_path / "episode"
    first = ProjectLock(project, stage="build").acquire()
    try:
        second = ProjectLock(
            project,
            stage="render",
            process_probe=lambda pid: ProcessProbe(None, detail="access denied"),
        )
        with pytest.raises(ResolveLockError, match="cannot establish"):
            second.acquire()
    finally:
        first.release()


def test_external_write_roots_require_explicit_mode_and_reject_drive_root(
    tmp_path: Path,
) -> None:
    project = tmp_path / "episode"
    external = tmp_path / "handoffs"
    with pytest.raises(UnsafeWriteError, match="escapes project"):
        normalize_write_roots(project, (external,))
    assert normalize_write_roots(
        project, (external,), allow_external_write_roots=True
    ) == (external.resolve(),)

    drive_root = Path(tmp_path.anchor)
    with pytest.raises(UnsafeWriteError, match="filesystem root"):
        normalize_write_roots(
            project, (drive_root,), allow_external_write_roots=True
        )


@pytest.mark.parametrize("state", [True, None, 1, RuntimeError("API failure")])
def test_render_state_fails_closed(state) -> None:
    with pytest.raises(ResolveBusyError):
        require_render_idle(_Project(state))


def test_explicit_idle_render_state_is_accepted() -> None:
    require_render_idle(_Project(False))

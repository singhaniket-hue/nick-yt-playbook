"""Durable DaVinci Resolve job queue and in-application executor.

Resolve Free intentionally does not use the external scripting bridge.  A
prepared job is executed with either the ``resolve`` object injected into
Resolve's Python console or ``app.GetResolve()`` in a Fusion-hosted script.
External Studio scripting is available only through an explicit, gated
adapter.

Generated timelines are immutable.  This runner may create or reuse an
``AUTO_BUILD_<hash>`` timeline; it never rewrites/deletes one and never mutates
an ``EDITORIAL_*`` timeline.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from typing import Any

from .resolve_safety import (
    ProjectLock,
    ResolveApiError,
    ResolveBusyError,
    ResolveSafetyError,
    UnsafeWriteError,
    get_current_project,
    get_project_manager,
    is_path_within,
    project_name,
    require_render_idle,
    require_write_path,
    utc_now,
)
from .resolve_platform import (
    configure_resolve_scripting_environment,
    resolve_runner_install_directory,
)
from .resolve_style import (
    ResolveStyleError,
    apply_style_to_new_timeline,
    style_contract_hash,
)


QUEUE_SCHEMA_VERSION = 2
STATUS_SCHEMA_VERSION = 1
POINTER_SCHEMA_VERSION = 1
QUEUE_RELATIVE_PATH = Path("resolve") / "queue"
STATUS_RELATIVE_PATH = Path("resolve") / "state.json"
RUNNER_POINTER_FILENAME = "resolve-runner.json"
RUNNER_SCRIPT_FILENAME = "RabbitHole Resolve Runner.py"
SOURCE_RUNNER_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "rabbithole_resolve_runner.py"
)

JOB_STATES = frozenset(
    {"queued", "running", "rendering", "superseded", "succeeded", "failed"}
)
TERMINAL_JOB_STATES = frozenset({"superseded", "succeeded", "failed"})
SUPPORTED_ACTIONS = frozenset({"build", "render", "handoff"})
_AUTO_TIMELINE_RE = re.compile(r"^AUTO_BUILD_([A-Za-z0-9][A-Za-z0-9_-]{7,63})$")
_SAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


class ResolveRunnerError(RuntimeError):
    """Base class for queue, connection, build, and render errors."""


class ResolveUnavailableError(ResolveRunnerError):
    """No explicitly permitted Resolve connection is available."""


class QueueError(ResolveRunnerError):
    """A durable queue document is invalid or cannot transition safely."""


class ImmutableTimelineError(ResolveRunnerError):
    """A generated or editorial timeline would be mutated unsafely."""


class ResolveExecutionError(ResolveRunnerError):
    """Resolve rejected a supported build/render operation."""


@dataclass(frozen=True)
class ResolveConnection:
    resolve: Any
    mode: str


@dataclass(frozen=True)
class BuildResult:
    action: str
    build_hash: str
    project_name: str
    timeline_name: str
    reused: bool
    project_created: bool
    import_path: str | None


@dataclass(frozen=True)
class RenderResult:
    action: str
    project_name: str
    timeline_name: str
    render_job_id: str
    render_status: str


def _canonical(path: os.PathLike[str] | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _json_clone(value: Any, *, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise QueueError(f"{label} must be JSON-serializable: {exc}") from exc


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    value, _ = _read_json_object_and_sha256(path, label=label)
    return value


def _read_json_object_and_sha256(
    path: Path, *, label: str
) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise QueueError(f"cannot read {label} {path}: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QueueError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise QueueError(f"{label} must be a JSON object: {path}")
    return value, hashlib.sha256(raw).hexdigest()


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def queue_directory(project_root: os.PathLike[str] | str) -> Path:
    return _canonical(project_root) / QUEUE_RELATIVE_PATH


def status_path(project_root: os.PathLike[str] | str) -> Path:
    return _canonical(project_root) / STATUS_RELATIVE_PATH


def runner_pointer_path() -> Path:
    """Return the user-local pointer consumed by the Workspace menu script."""

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return _canonical(base) / "RabbitHole" / RUNNER_POINTER_FILENAME
        return Path.home() / "AppData" / "Local" / "RabbitHole" / RUNNER_POINTER_FILENAME
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "RabbitHole"
            / RUNNER_POINTER_FILENAME
        )
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return _canonical(base) / "rabbithole" / RUNNER_POINTER_FILENAME
    return Path.home() / ".local" / "state" / "rabbithole" / RUNNER_POINTER_FILENAME


def _write_runner_pointer(
    project_root: Path,
    queue_path: Path,
    job_id: str,
    *,
    pointer_path: os.PathLike[str] | str | None = None,
) -> Path:
    destination = (
        _canonical(pointer_path) if pointer_path is not None else runner_pointer_path()
    )
    payload = {
        "schema_version": POINTER_SCHEMA_VERSION,
        "project_root": os.fspath(project_root),
        "queue_path": os.fspath(queue_path),
        "job_id": job_id,
        "source_root": os.fspath(Path(__file__).resolve().parents[1]),
        "updated_at": utc_now(),
    }
    _atomic_write_json(destination, payload)
    return destination


def read_runner_pointer(
    pointer_path: os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    path = _canonical(pointer_path) if pointer_path else runner_pointer_path()
    pointer = _read_json_object(path, label="Resolve runner pointer")
    if pointer.get("schema_version") != POINTER_SCHEMA_VERSION:
        raise QueueError(
            f"unsupported Resolve runner pointer schema: {pointer.get('schema_version')!r}"
        )
    for field in ("project_root", "queue_path", "job_id"):
        if not isinstance(pointer.get(field), str) or not pointer[field]:
            raise QueueError(f"Resolve runner pointer is missing {field!r}: {path}")
    pointer["path"] = os.fspath(path)
    return pointer


def _job_fingerprint(
    project_root: Path,
    *,
    action: str,
    plan_path: Path,
    plan_sha256: str,
    fcpxml_path: Path,
    fcpxml_sha256: str,
    media_sha256: Mapping[str, str],
    options: Mapping[str, Any],
) -> dict[str, Any]:
    plan = _canonical(plan_path)
    fcpxml = _canonical(fcpxml_path)
    if not is_path_within(plan, project_root):
        raise QueueError(f"queued Resolve plan escapes project root: {plan}")
    if not is_path_within(fcpxml, project_root):
        raise QueueError(f"queued FCPXML escapes project root: {fcpxml}")
    return {
        "action": action,
        "plan_path": plan.relative_to(project_root).as_posix(),
        "plan_sha256": plan_sha256,
        "fcpxml_path": fcpxml.relative_to(project_root).as_posix(),
        "fcpxml_sha256": fcpxml_sha256,
        "media_sha256": dict(sorted(media_sha256.items())),
        "options": dict(options),
    }


def _validate_job_fingerprint(job: Mapping[str, Any], project_root: Path) -> None:
    fingerprint = _job_fingerprint(
        project_root,
        action=str(job["action"]),
        plan_path=_canonical(str(job["plan_path"])),
        plan_sha256=_validated_sha256(
            job["plan_sha256"], label="queued Resolve plan"
        ),
        fcpxml_path=_canonical(str(job["fcpxml_path"])),
        fcpxml_sha256=_validated_sha256(
            job["fcpxml_sha256"], label="queued FCPXML"
        ),
        media_sha256=dict(job["media_sha256"]),
        options=dict(job["options"]),
    )
    expected_job_id = _canonical_json_hash(fingerprint)[:24]
    if job.get("job_id") != expected_job_id:
        raise QueueError(
            "Resolve queue job fingerprint does not match its filename; "
            "refusing a modified queue document"
        )


def _enqueue_job_locked(
    project_root: os.PathLike[str] | str,
    action: str,
    plan_path: os.PathLike[str] | str,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Durably enqueue an idempotent Resolve action.

    The job ID includes the plan checksum, action, and options.  Enqueuing the
    same logical job returns its existing document, including a terminal state;
    succeeded/failed jobs are never implicitly rerun.
    """

    project = _canonical(project_root)
    action_value = str(action).strip().lower()
    if action_value not in SUPPORTED_ACTIONS:
        raise QueueError(
            f"unsupported Resolve action {action!r}; expected one of "
            f"{', '.join(sorted(SUPPORTED_ACTIONS))}"
        )
    plan = _canonical(plan_path)
    if not is_path_within(plan, project):
        raise UnsafeWriteError(f"Resolve plan escapes project root: {plan}")
    if not plan.is_file():
        raise QueueError(f"Resolve plan does not exist: {plan}")

    plan_data, plan_sha256 = _read_json_object_and_sha256(
        plan, label="Resolve plan"
    )
    fcpxml = timeline_import_path(plan_data, plan, project)
    try:
        fcpxml_sha256 = _file_sha256(fcpxml)
    except OSError as exc:
        raise QueueError(f"cannot hash Resolve FCPXML {fcpxml}: {exc}") from exc
    declared_fcpxml_sha256 = _plan_fcpxml_sha256(plan_data)
    if fcpxml_sha256 != declared_fcpxml_sha256:
        raise QueueError(
            "Resolve FCPXML does not match the checksum recorded in the plan: "
            f"expected {declared_fcpxml_sha256}, got {fcpxml_sha256}: {fcpxml}"
        )
    media_sha256 = _plan_media_checksum_snapshot(plan_data, project)
    safe_options = _json_clone(dict(options or {}), label="job options")
    fingerprint = _job_fingerprint(
        project,
        action=action_value,
        plan_path=plan,
        plan_sha256=plan_sha256,
        fcpxml_path=fcpxml,
        fcpxml_sha256=fcpxml_sha256,
        media_sha256=media_sha256,
        options=safe_options,
    )
    job_id = _canonical_json_hash(fingerprint)[:24]
    queue_path = queue_directory(project)
    job_path = queue_path / f"{job_id}.json"

    if job_path.exists():
        existing = _read_job(job_path)
        _validate_job_fingerprint(existing, project)
        if existing["action"] == "build" and existing["state"] == "queued":
            _supersede_older_queued_builds(
                project,
                keep_job_id=job_id,
                keep_created_at=str(existing.get("created_at") or ""),
            )
        _write_runner_pointer(project, queue_path, job_id)
        result = dict(existing)
        result["queue_file"] = os.fspath(job_path)
        result["path"] = os.fspath(job_path)
        return result

    now = utc_now()
    job = {
        "schema_version": QUEUE_SCHEMA_VERSION,
        "job_id": job_id,
        "action": action_value,
        "plan_path": os.fspath(plan),
        "plan_sha256": fingerprint["plan_sha256"],
        "fcpxml_path": os.fspath(fcpxml),
        "fcpxml_sha256": fingerprint["fcpxml_sha256"],
        "media_sha256": media_sha256,
        "options": safe_options,
        "state": "queued",
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "error": None,
        "result": None,
    }
    queue_path.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            job_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
    except FileExistsError:
        existing = _read_job(job_path)
        _validate_job_fingerprint(existing, project)
        _write_runner_pointer(project, queue_path, job_id)
        result = dict(existing)
        result["queue_file"] = os.fspath(job_path)
        result["path"] = os.fspath(job_path)
        return result
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(job, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())

    if action_value == "build":
        _supersede_older_queued_builds(
            project,
            keep_job_id=job_id,
            keep_created_at=now,
        )
    pointer = _write_runner_pointer(project, queue_path, job_id)
    _write_status(
        project,
        state="queued",
        active_job=None,
        last_job=job_id,
        detail=(
            "awaiting_in_app_runner"
            if str(safe_options.get("mode", "free")).lower() != "studio"
            else "queued_for_studio_runner"
        ),
    )
    result = dict(job)
    result["queue_file"] = os.fspath(job_path)
    result["path"] = os.fspath(job_path)
    result["runner_pointer"] = os.fspath(pointer)
    result["console_loader"] = console_loader_command(project, job_id=job_id)
    return result


def enqueue_job(
    project_root: os.PathLike[str] | str,
    action: str,
    plan_path: os.PathLike[str] | str,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Durably enqueue under the same project identity lock used by execution."""

    project = _canonical(project_root)
    pointer_root = runner_pointer_path().parent
    roots = (project / "resolve", pointer_root)
    with ProjectLock(
        project,
        stage="resolve-enqueue",
        write_roots=roots,
        allow_external_write_roots=not is_path_within(pointer_root, project),
    ):
        return _enqueue_job_locked(project, action, plan_path, options)


def _read_job(path: Path) -> dict[str, Any]:
    job = _read_json_object(path, label="Resolve queue job")
    if job.get("schema_version") != QUEUE_SCHEMA_VERSION:
        raise QueueError(
            f"unsupported queue schema in {path}: {job.get('schema_version')!r}"
        )
    if job.get("state") not in JOB_STATES:
        raise QueueError(f"invalid queue state in {path}: {job.get('state')!r}")
    if job.get("action") not in SUPPORTED_ACTIONS:
        raise QueueError(f"invalid queue action in {path}: {job.get('action')!r}")
    if not isinstance(job.get("options"), Mapping):
        raise QueueError(f"Resolve queue job has invalid options: {path}")
    if path.stem != job.get("job_id"):
        raise QueueError(f"job ID does not match queue filename: {path}")
    for field in ("plan_path", "plan_sha256", "fcpxml_path", "fcpxml_sha256"):
        if not isinstance(job.get(field), str) or not job[field]:
            raise QueueError(f"Resolve queue job is missing {field!r}: {path}")
    for field in ("plan_sha256", "fcpxml_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", str(job[field])) is None:
            raise QueueError(
                f"Resolve queue job has an invalid {field!r}: {path}"
            )
    media_sha256 = job.get("media_sha256")
    if not isinstance(media_sha256, Mapping):
        raise QueueError(f"Resolve queue job has invalid media_sha256: {path}")
    for media_path, checksum in media_sha256.items():
        if (
            not isinstance(media_path, str)
            or not media_path
            or not isinstance(checksum, str)
            or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
        ):
            raise QueueError(
                f"Resolve queue job has an invalid media checksum entry: {path}"
            )
    return job


def _is_legacy_queue_job(path: Path) -> bool:
    document = _read_json_object(path, label="Resolve queue job")
    version = document.get("schema_version")
    if version == QUEUE_SCHEMA_VERSION:
        return False
    if type(version) is int and 0 < version < QUEUE_SCHEMA_VERSION:
        return True
    raise QueueError(f"unsupported queue schema in {path}: {version!r}")


_ALLOWED_TRANSITIONS = {
    "queued": frozenset({"running", "superseded"}),
    "running": frozenset({"rendering", "succeeded", "failed"}),
    "rendering": frozenset({"succeeded", "failed"}),
    "superseded": frozenset(),
    "succeeded": frozenset(),
    "failed": frozenset(),
}


def _transition_job(
    path: Path,
    job: Mapping[str, Any],
    state: str,
    *,
    result: Any = None,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    current = str(job.get("state"))
    if state not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise QueueError(f"invalid queue transition {current!r} -> {state!r}")
    updated = dict(job)
    now = utc_now()
    updated["state"] = state
    updated["updated_at"] = now
    if state == "running":
        updated["attempts"] = int(updated.get("attempts", 0)) + 1
        updated["started_at"] = now
        updated["finished_at"] = None
        updated["error"] = None
        updated["result"] = None
    elif state == "rendering":
        updated["finished_at"] = None
        updated["result"] = _json_clone(result, label="job result")
        updated["error"] = None
    else:
        updated["finished_at"] = now
        updated["result"] = _json_clone(result, label="job result")
        updated["error"] = _json_clone(error, label="job error") if error else None
    _atomic_write_json(path, updated)
    return updated


def _supersede_older_queued_builds(
    project_root: Path,
    *,
    keep_job_id: str,
    keep_created_at: str,
) -> None:
    """Retire only older unclaimed build jobs after a replacement is durable."""

    for path in _queue_files(project_root):
        if path.stem == keep_job_id or _is_legacy_queue_job(path):
            continue
        candidate = _read_job(path)
        _validate_job_fingerprint(candidate, project_root)
        if candidate["action"] != "build" or candidate["state"] != "queued":
            continue
        candidate_created_at = candidate.get("created_at")
        if (
            not isinstance(candidate_created_at, str)
            or not keep_created_at
            or candidate_created_at >= keep_created_at
        ):
            continue
        _transition_job(
            path,
            candidate,
            "superseded",
            result={
                "reason": "newer_build_enqueued",
                "superseded_by": keep_job_id,
            },
        )


def _queue_files(
    project_root: Path,
    queue_path: os.PathLike[str] | str | None = None,
) -> list[Path]:
    directory = _canonical(queue_path) if queue_path else queue_directory(project_root)
    if not is_path_within(directory, project_root):
        raise UnsafeWriteError(f"queue path escapes project root: {directory}")
    if directory.is_file():
        return [directory]
    if not directory.exists():
        return []
    return sorted(directory.glob("*.json"))


def _status_counts(project_root: Path) -> dict[str, int]:
    counts = {state: 0 for state in sorted(JOB_STATES)}
    counts["legacy"] = 0
    for path in _queue_files(project_root):
        if _is_legacy_queue_job(path):
            counts["legacy"] += 1
            continue
        job = _read_job(path)
        counts[job["state"]] += 1
    return counts


def _write_status(
    project_root: Path,
    *,
    state: str,
    active_job: str | None,
    last_job: str | None,
    detail: str | None = None,
    project_name_value: str | None = None,
    timeline_name: str | None = None,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": STATUS_SCHEMA_VERSION,
        "state": state,
        "detail": detail,
        "project_root": os.fspath(project_root),
        "active_job": active_job,
        "last_job": last_job,
        "project_name": project_name_value,
        "timeline_name": timeline_name,
        "updated_at": utc_now(),
        "queue": _status_counts(project_root),
        "error": dict(error) if error else None,
    }
    destination = status_path(project_root)
    require_write_path(destination, (project_root / "resolve",))
    _atomic_write_json(destination, payload)
    return payload


def read_status(project_root: os.PathLike[str] | str) -> dict[str, Any]:
    """Read durable status and refresh its queue counts from job documents."""

    project = _canonical(project_root)
    path = status_path(project)
    if path.exists():
        status = _read_json_object(path, label="Resolve status")
        if status.get("schema_version") != STATUS_SCHEMA_VERSION:
            raise QueueError(
                f"unsupported Resolve status schema: {status.get('schema_version')!r}"
            )
    else:
        status = {
            "schema_version": STATUS_SCHEMA_VERSION,
            "state": "idle",
            "detail": None,
            "project_root": os.fspath(project),
            "active_job": None,
            "last_job": None,
            "project_name": None,
            "timeline_name": None,
            "updated_at": None,
            "error": None,
        }
    status["queue"] = _status_counts(project)
    status["status_file"] = os.fspath(path)
    return status


def _default_studio_external_adapter() -> Any:
    """Connect through Resolve's external bridge.

    This function is never reached unless ``allow_studio_external=True``.
    """

    configure_resolve_scripting_environment()
    try:
        module = importlib.import_module("DaVinciResolveScript")
    except ImportError as exc:
        raise ResolveUnavailableError(
            "DaVinciResolveScript is unavailable. Resolve Free jobs must run "
            "inside Resolve; Studio external mode requires its scripting module."
        ) from exc
    factory = getattr(module, "scriptapp", None)
    if not callable(factory):
        raise ResolveUnavailableError(
            "DaVinciResolveScript does not expose scriptapp('Resolve')"
        )
    return factory("Resolve")


def connect_resolve(
    *,
    resolve: Any = None,
    app: Any = None,
    allow_studio_external: bool = False,
    studio_external_adapter: Callable[[], Any] | None = None,
) -> ResolveConnection:
    """Resolve an explicitly permitted connection without launching Resolve."""

    if resolve is not None:
        return ResolveConnection(resolve, "injected_resolve")
    if app is not None:
        getter = getattr(app, "GetResolve", None)
        if not callable(getter):
            raise ResolveUnavailableError("injected app does not expose GetResolve()")
        try:
            connected = getter()
        except Exception as exc:
            raise ResolveUnavailableError(f"app.GetResolve() failed: {exc}") from exc
        if connected is None:
            raise ResolveUnavailableError("app.GetResolve() returned no Resolve object")
        return ResolveConnection(connected, "injected_app")

    if studio_external_adapter is not None and not allow_studio_external:
        raise ResolveUnavailableError(
            "Studio external adapter was supplied but external mode is not enabled"
        )
    if not allow_studio_external:
        raise ResolveUnavailableError(
            "no in-app Resolve object is available. Run the queued job from "
            "Resolve Free's Python console/Workspace script, or explicitly enable "
            "Studio external mode."
        )
    adapter = studio_external_adapter or _default_studio_external_adapter
    try:
        connected = adapter()
    except ResolveUnavailableError:
        raise
    except Exception as exc:
        raise ResolveUnavailableError(
            f"Studio external Resolve adapter failed: {exc}"
        ) from exc
    if connected is None:
        raise ResolveUnavailableError(
            "Studio external Resolve adapter returned no Resolve object"
        )
    return ResolveConnection(connected, "studio_external")


def get_resolve(
    resolve: Any = None,
    app: Any = None,
    *,
    allow_studio_external: bool = False,
    studio_external_adapter: Callable[[], Any] | None = None,
) -> Any:
    """Convenience wrapper returning only the connected Resolve object."""

    return connect_resolve(
        resolve=resolve,
        app=app,
        allow_studio_external=allow_studio_external,
        studio_external_adapter=studio_external_adapter,
    ).resolve


def _load_plan(
    plan_path: os.PathLike[str] | str,
    *,
    expected_sha256: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    path = _canonical(plan_path)
    plan, actual = _read_json_object_and_sha256(path, label="Resolve plan")
    if expected_sha256 is not None:
        expected = _validated_sha256(
            expected_sha256, label="queued Resolve plan"
        )
        if actual != expected:
            raise QueueError(
                f"Resolve plan changed after enqueue: expected {expected}, "
                f"got {actual}: {path}"
            )
    return path, plan


def _extract_explicit_hash(plan: Mapping[str, Any]) -> str | None:
    candidates: list[Any] = [
        plan.get("build_hash"),
        plan.get("plan_hash"),
        plan.get("content_hash"),
        plan.get("build_id"),
    ]
    build = plan.get("build")
    if isinstance(build, Mapping):
        candidates.extend((build.get("hash"), build.get("build_hash")))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            cleaned = value.strip()
            if cleaned.startswith("sha256:"):
                cleaned = cleaned.partition(":")[2]
            if cleaned.startswith("b-"):
                cleaned = cleaned[2:]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}", cleaned):
                raise ResolveExecutionError(f"invalid build hash in plan: {value!r}")
            return cleaned
    return None


def build_hash_for_plan(plan: Mapping[str, Any]) -> str:
    return _extract_explicit_hash(plan) or _canonical_json_hash(plan)[:16]


def timeline_name_for_plan(plan: Mapping[str, Any]) -> str:
    explicit: Any = plan.get("timeline_name")
    timeline = plan.get("timeline")
    if explicit is None and isinstance(timeline, Mapping):
        explicit = timeline.get("name")
    explicit_hash = _extract_explicit_hash(plan)
    build_hash = explicit_hash or build_hash_for_plan(plan)
    name = str(explicit) if explicit is not None else f"AUTO_BUILD_{build_hash}"
    match = _AUTO_TIMELINE_RE.fullmatch(name)
    if not match:
        raise ImmutableTimelineError(
            f"generated timeline name must be AUTO_BUILD_<hash>, got {name!r}"
        )
    if explicit_hash is not None and not build_hash.lower().startswith(
        match.group(1).lower()
    ) and not match.group(1).lower().startswith(build_hash.lower()):
        raise ImmutableTimelineError(
            f"timeline name {name!r} does not match plan build hash {build_hash!r}"
        )
    return name


def _safe_project_slug(project_root: Path, plan: Mapping[str, Any]) -> str:
    raw: Any = plan.get("project_slug") or plan.get("slug")
    project_block = plan.get("project")
    if raw is None and isinstance(project_block, Mapping):
        raw = project_block.get("slug") or project_block.get("name")
    value = _SAFE_SLUG_RE.sub("_", str(raw or project_root.name)).strip("_")
    return (value or "PROJECT")[:32]


def deterministic_project_name(
    project_root: os.PathLike[str] | str, plan: Mapping[str, Any]
) -> str:
    project = _canonical(project_root)
    return (
        f"RABBITHOLE_{_safe_project_slug(project, plan)}_"
        f"{build_hash_for_plan(plan)[:10]}"
    )[:63]


def _plan_path_value(plan: Mapping[str, Any]) -> str | None:
    for key in ("fcpxml_path", "timeline_path", "import_path"):
        value = plan.get(key)
        if isinstance(value, str) and value:
            return value
    for container_name in (
        "timeline",
        "artifacts",
        "files",
        "outputs",
        "output_paths",
    ):
        container = plan.get(container_name)
        if not isinstance(container, Mapping):
            continue
        for key in (
            "fcpxml_path",
            "fcpxml",
            "timeline_path",
            "import_path",
            "path",
        ):
            value = container.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, Mapping):
                nested = value.get("path")
                if isinstance(nested, str) and nested:
                    return nested
    return None


def timeline_import_path(
    plan: Mapping[str, Any], plan_path: Path, project_root: Path
) -> Path:
    raw = _plan_path_value(plan)
    if raw is None:
        raise ResolveExecutionError(
            "Resolve plan has no FCPXML/timeline import path"
        )
    value = Path(raw).expanduser()
    if not value.is_absolute():
        plan_relative = (plan_path.parent / value).resolve(strict=False)
        project_relative = (project_root / value).resolve(strict=False)
        # Compiler paths are explicitly project-relative.  Prefer that contract
        # whenever it exists; older hand-authored plans remain plan-relative.
        output_paths = plan.get("output_paths")
        path_kind = (
            output_paths.get("fcpxml_path_kind")
            if isinstance(output_paths, Mapping)
            else None
        )
        if path_kind == "project-relative" or project_relative.exists():
            value = project_relative
        else:
            value = plan_relative
    value = value.resolve(strict=False)
    if not is_path_within(value, project_root):
        raise ResolveExecutionError(f"timeline import path escapes project: {value}")
    if not value.is_file():
        raise ResolveExecutionError(f"timeline import file does not exist: {value}")
    return value


def subtitle_import_path(
    plan: Mapping[str, Any], plan_path: Path, project_root: Path
) -> Path:
    """Resolve the deterministic presentation SRT inside the project.

    New compiler plans keep ``subtitles.srt`` complete for upload and point
    Resolve at a selective ``presentation_subtitles`` artifact.  Older plans
    expose only ``subtitles`` and retain their original behavior.
    """

    output_paths = plan.get("output_paths")
    if not isinstance(output_paths, Mapping):
        raise ResolveExecutionError("Resolve plan has no output_paths object")
    field = (
        "presentation_subtitles"
        if output_paths.get("presentation_subtitles")
        else "subtitles"
    )
    raw = output_paths.get(field)
    if not isinstance(raw, str) or not raw:
        raise ResolveExecutionError(
            "Resolve plan has no subtitle SRT fallback path"
        )
    value = Path(raw).expanduser()
    if not value.is_absolute():
        path_kind = output_paths.get(f"{field}_path_kind")
        plan_relative = (plan_path.parent / value).resolve(strict=False)
        project_relative = (project_root / value).resolve(strict=False)
        if path_kind == "project-relative" or project_relative.exists():
            value = project_relative
        else:
            value = plan_relative
    value = value.resolve(strict=False)
    if not is_path_within(value, project_root):
        raise ResolveExecutionError(f"subtitle import path escapes project: {value}")
    if not value.is_file():
        raise ResolveExecutionError(
            f"subtitle import file does not exist: {value}"
        )
    return value


def _validated_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise QueueError(f"{label} has no valid SHA-256 checksum")
    return value.lower()


def _resolve_recorded_media_path(
    raw_path: Any,
    path_kind: Any,
    project_root: Path,
    *,
    label: str,
) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise QueueError(f"{label} has no media path")
    value = Path(raw_path).expanduser()
    inferred_kind = "external-absolute" if value.is_absolute() else "project-relative"
    kind = inferred_kind if path_kind in (None, "") else str(path_kind)
    if kind not in {"project-relative", "external-absolute"}:
        raise QueueError(f"{label} has unsupported path_kind {kind!r}")
    if kind == "project-relative":
        if value.is_absolute():
            raise QueueError(f"{label} declares an absolute project-relative path")
        resolved = (project_root / value).resolve(strict=False)
        if not is_path_within(resolved, project_root):
            raise QueueError(f"{label} escapes the project root: {resolved}")
        return resolved
    if not value.is_absolute():
        raise QueueError(f"{label} declares a relative external-absolute path")
    return value.resolve(strict=False)


def _media_identity(path: Path, project_root: Path) -> str:
    if is_path_within(path, project_root):
        return path.relative_to(project_root).as_posix()
    return path.as_posix()


def _plan_fcpxml_sha256(plan: Mapping[str, Any]) -> str:
    output_paths = plan.get("output_paths")
    if not isinstance(output_paths, Mapping):
        raise QueueError("Resolve plan has no output_paths object")
    return _validated_sha256(
        output_paths.get("fcpxml_sha256"),
        label="Resolve plan output_paths.fcpxml_sha256",
    )


def _plan_subtitles_sha256(plan: Mapping[str, Any]) -> str:
    output_paths = plan.get("output_paths")
    if not isinstance(output_paths, Mapping):
        raise QueueError("Resolve plan has no output_paths object")
    field = (
        "presentation_subtitles_sha256"
        if output_paths.get("presentation_subtitles")
        else "subtitles_sha256"
    )
    return _validated_sha256(
        output_paths.get(field),
        label=f"Resolve plan output_paths.{field}",
    )


def _verified_subtitle_import_path(
    plan: Mapping[str, Any], plan_path: Path, project_root: Path
) -> Path:
    path = subtitle_import_path(plan, plan_path, project_root)
    try:
        expected = _plan_subtitles_sha256(plan)
    except QueueError as exc:
        raise ResolveExecutionError(str(exc)) from exc
    try:
        actual = _file_sha256(path)
    except OSError as exc:
        raise ResolveExecutionError(
            f"cannot hash Resolve subtitle SRT {path}: {exc}"
        ) from exc
    if actual != expected:
        raise ResolveExecutionError(
            "Resolve subtitle SRT does not match the checksum recorded in the "
            f"plan: expected {expected}, got {actual}: {path}"
        )
    return path


def _collect_plan_media_expectations(
    plan: Mapping[str, Any],
    project_root: Path,
) -> dict[Path, str]:
    """Collect linked input checksums without reading potentially large media."""

    provenance_values = plan.get("provenance")
    provenance = (
        [item for item in provenance_values if isinstance(item, Mapping)]
        if isinstance(provenance_values, Sequence)
        and not isinstance(provenance_values, (str, bytes))
        else []
    )
    provenance_records: list[tuple[str | None, Path, str | None]] = []
    for index, record in enumerate(provenance):
        raw_path = record.get("local_path")
        if raw_path in (None, ""):
            continue
        path = _resolve_recorded_media_path(
            raw_path,
            record.get("path_kind"),
            project_root,
            label=f"provenance[{index}]",
        )
        raw_checksum = record.get("sha256")
        checksum = (
            _validated_sha256(raw_checksum, label=f"provenance[{index}]")
            if raw_checksum not in (None, "")
            else None
        )
        asset_id = record.get("asset_id", record.get("id"))
        provenance_records.append(
            (str(asset_id) if asset_id not in (None, "") else None, path, checksum)
        )

    expected_by_path: dict[Path, str] = {}

    def add_linked_input(
        item: Mapping[str, Any],
        *,
        path_field: str,
        label: str,
    ) -> None:
        raw_path = item.get(path_field)
        if raw_path in (None, ""):
            return
        asset_value = item.get("asset_id")
        if asset_value in (None, ""):
            raise QueueError(f"{label} has media_path but no asset_id")
        asset_id = str(asset_value)
        path = _resolve_recorded_media_path(
            raw_path,
            item.get("path_kind"),
            project_root,
            label=label,
        )
        raw_checksum = item.get("sha256")
        checksum: str | None = None
        if raw_checksum not in (None, ""):
            checksum = _validated_sha256(raw_checksum, label=label)
        else:
            matches = [
                record_checksum
                for record_asset, record_path, record_checksum in provenance_records
                if record_path == path
                and record_asset == asset_id
                and record_checksum is not None
            ]
            unique_matches = sorted(set(matches))
            if len(unique_matches) == 1:
                checksum = unique_matches[0]
            elif len(unique_matches) > 1:
                raise QueueError(
                    f"{label} has conflicting provenance checksums for {path}"
                )
        if checksum is None:
            raise QueueError(
                f"{label} is linked media but has no matching plan-recorded SHA-256"
            )
        existing = expected_by_path.get(path)
        if existing is not None and existing != checksum:
            raise QueueError(f"linked media has conflicting checksums: {path}")
        expected_by_path[path] = checksum

    for collection_name, path_field in (("clips", "media_path"), ("audio", "media_path")):
        values = plan.get(collection_name)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            continue
        for index, item in enumerate(values):
            if not isinstance(item, Mapping):
                raise QueueError(
                    f"Resolve plan {collection_name}[{index}] must be an object"
                )
            add_linked_input(
                item,
                path_field=path_field,
                label=f"{collection_name}[{index}]",
            )
    return expected_by_path


def _plan_media_checksum_snapshot(
    plan: Mapping[str, Any],
    project_root: Path,
) -> dict[str, str]:
    expectations = _collect_plan_media_expectations(plan, project_root)
    return {
        _media_identity(path, project_root): checksum
        for path, checksum in sorted(
            expectations.items(),
            key=lambda item: _media_identity(item[0], project_root),
        )
    }


def _validate_plan_media_integrity(
    plan: Mapping[str, Any],
    project_root: Path,
) -> dict[str, str]:
    """Hash every linked video/audio input against the immutable plan."""

    expected_by_path = _collect_plan_media_expectations(plan, project_root)
    verified: dict[str, str] = {}
    for path, expected in sorted(
        expected_by_path.items(),
        key=lambda item: _media_identity(item[0], project_root),
    ):
        if not path.is_file():
            raise QueueError(f"linked media file does not exist: {path}")
        try:
            actual = _file_sha256(path)
        except OSError as exc:
            raise QueueError(f"cannot hash linked media {path}: {exc}") from exc
        if actual != expected:
            raise QueueError(
                "Resolve media changed after plan compilation: "
                f"expected {expected}, got {actual}: {path}"
            )
        verified[_media_identity(path, project_root)] = expected
    return verified


def _validate_queued_inputs(
    job: Mapping[str, Any],
    plan: Mapping[str, Any],
    plan_path: Path,
    project_root: Path,
) -> None:
    fcpxml = timeline_import_path(plan, plan_path, project_root)
    queued_fcpxml = _canonical(str(job["fcpxml_path"]))
    if not is_path_within(queued_fcpxml, project_root):
        raise QueueError(f"queued FCPXML escapes project root: {queued_fcpxml}")
    if queued_fcpxml != fcpxml:
        raise QueueError(
            "Resolve plan FCPXML path changed after enqueue: "
            f"expected {queued_fcpxml}, got {fcpxml}"
        )
    expected_fcpxml_sha256 = _validated_sha256(
        job["fcpxml_sha256"], label="queued FCPXML"
    )
    declared_fcpxml_sha256 = _plan_fcpxml_sha256(plan)
    if declared_fcpxml_sha256 != expected_fcpxml_sha256:
        raise QueueError(
            "queued FCPXML checksum no longer matches the immutable plan"
        )
    try:
        actual_fcpxml_sha256 = _file_sha256(fcpxml)
    except OSError as exc:
        raise QueueError(f"cannot hash queued FCPXML {fcpxml}: {exc}") from exc
    if actual_fcpxml_sha256 != expected_fcpxml_sha256:
        raise QueueError(
            "FCPXML changed after enqueue: "
            f"expected {expected_fcpxml_sha256}, got {actual_fcpxml_sha256}: "
            f"{fcpxml}"
        )

    output_paths = plan.get("output_paths")
    if isinstance(output_paths, Mapping) and (
        output_paths.get("presentation_subtitles")
        or output_paths.get("subtitles")
    ):
        try:
            subtitles = subtitle_import_path(plan, plan_path, project_root)
        except ResolveExecutionError as exc:
            raise QueueError(str(exc)) from exc
        expected_subtitles_sha256 = _plan_subtitles_sha256(plan)
        try:
            actual_subtitles_sha256 = _file_sha256(subtitles)
        except OSError as exc:
            raise QueueError(
                f"cannot hash queued subtitle SRT {subtitles}: {exc}"
            ) from exc
        if actual_subtitles_sha256 != expected_subtitles_sha256:
            raise QueueError(
                "subtitle SRT changed after enqueue: "
                f"expected {expected_subtitles_sha256}, got "
                f"{actual_subtitles_sha256}: {subtitles}"
            )

    expected_media = dict(job["media_sha256"])
    actual_media = _validate_plan_media_integrity(plan, project_root)
    if actual_media != expected_media:
        raise QueueError(
            "Resolve linked-media checksum set changed after enqueue"
        )


def _call_required(owner: Any, name: str, *args: Any) -> Any:
    method = getattr(owner, name, None)
    if not callable(method):
        raise ResolveApiError(f"Resolve API object does not expose {name}()")
    try:
        return method(*args)
    except Exception as exc:
        raise ResolveExecutionError(f"{name}() failed: {exc}") from exc


def _timeline_name(timeline: Any) -> str:
    value = _call_required(timeline, "GetName")
    if not isinstance(value, str) or not value:
        raise ResolveExecutionError("timeline GetName() returned an empty value")
    return value


def _all_timelines(project: Any) -> list[Any]:
    count = _call_required(project, "GetTimelineCount")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ResolveExecutionError(f"GetTimelineCount() returned {count!r}")
    timelines: list[Any] = []
    for index in range(1, count + 1):
        timeline = _call_required(project, "GetTimelineByIndex", index)
        if timeline is None:
            raise ResolveExecutionError(
                f"GetTimelineByIndex({index}) returned no timeline"
            )
        timelines.append(timeline)
    return timelines


def _find_generated_timeline(project: Any, name: str) -> Any | None:
    matches = [
        timeline for timeline in _all_timelines(project) if _timeline_name(timeline) == name
    ]
    if len(matches) > 1:
        raise ImmutableTimelineError(
            f"multiple timelines are named {name!r}; refusing an ambiguous mutation"
        )
    return matches[0] if matches else None


def _sequence_values(value: Any, *, label: str) -> list[Any]:
    if isinstance(value, Mapping):
        return list(value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    raise ImmutableTimelineError(f"{label} returned {value!r}, expected a list")


def _expected_subtitle_count(plan: Mapping[str, Any]) -> int:
    validation = plan.get("timeline_validation")
    if isinstance(validation, Mapping) and validation.get("subtitle_count") is not None:
        return int(validation["subtitle_count"])
    raw = plan.get("subtitles")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return len(raw)
    return 0


def _subtitle_items(timeline: Any) -> list[Any]:
    getter = getattr(timeline, "GetItemListInTrack", None)
    if not callable(getter):
        raise ImmutableTimelineError(
            "timeline cannot validate imported subtitle items"
        )
    return _sequence_values(
        getter("subtitle", 1),
        label="GetItemListInTrack('subtitle', 1)",
    )


def _normalized_subtitle_text(value: Any) -> str:
    lines = [
        " ".join(line.split())
        for line in str(value or "").replace("\r", "").split("\n")
        if line.strip()
    ]
    return "\n".join(lines)


def _validate_subtitle_items(
    timeline: Any,
    items: Sequence[Any],
    plan: Mapping[str, Any],
    *,
    require_text: bool,
) -> None:
    """Validate cue placement, and SRT-expanded text, before saving the build."""

    raw_subtitles = plan.get("subtitles")
    subtitles = (
        [item for item in raw_subtitles if isinstance(item, Mapping)]
        if isinstance(raw_subtitles, Sequence)
        and not isinstance(raw_subtitles, (str, bytes))
        else []
    )
    timeline_start = int(_call_required(timeline, "GetStartFrame"))
    expected_timing: list[tuple[int, int]] = []
    expected_records: list[tuple[int, int, str]] = []
    for subtitle in subtitles:
        start = timeline_start + int(subtitle.get("start_frame", 0))
        raw_end = subtitle.get("end_frame")
        if raw_end is None:
            raw_end = int(subtitle.get("start_frame", 0)) + int(
                subtitle.get("duration_frames", 0)
            )
        end = timeline_start + int(raw_end)
        expected_timing.append((start, end))
        expected_records.append(
            (start, end, _normalized_subtitle_text(subtitle.get("text")))
        )

    actual_timing: list[tuple[int, int]] = []
    actual_records: list[tuple[int, int, str]] = []
    for position, item in enumerate(items):
        start_getter = getattr(item, "GetStart", None)
        end_getter = getattr(item, "GetEnd", None)
        if not callable(start_getter) or not callable(end_getter):
            raise ImmutableTimelineError(
                f"subtitle item {position + 1} cannot report its frame range"
            )
        start = int(start_getter())
        end = int(end_getter())
        actual_timing.append((start, end))
        if require_text:
            name_getter = getattr(item, "GetName", None)
            if not callable(name_getter):
                raise ImmutableTimelineError(
                    f"subtitle item {position + 1} cannot report its text"
                )
            actual_records.append(
                (start, end, _normalized_subtitle_text(name_getter()))
            )

    if sorted(actual_timing) != sorted(expected_timing):
        raise ImmutableTimelineError(
            "imported subtitle cue timing does not match the Resolve plan"
        )
    if require_text and sorted(actual_records) != sorted(expected_records):
        raise ImmutableTimelineError(
            "imported subtitle text or timing does not match the Resolve plan"
        )


def _ensure_imported_subtitles(
    project: Any,
    media_pool: Any,
    timeline: Any,
    plan: Mapping[str, Any],
    plan_path: Path,
    project_root: Path,
) -> dict[str, Any]:
    """Use the SRT sidecar only when Resolve ignored every FCPXML caption."""

    expected = _expected_subtitle_count(plan)
    items = _subtitle_items(timeline)
    actual = len(items)
    if actual == expected:
        _validate_subtitle_items(
            timeline,
            items,
            plan,
            require_text=True,
        )
        return {
            "status": "not_required" if expected == 0 else "fcpxml",
            "count": actual,
        }
    if actual:
        raise ImmutableTimelineError(
            f"imported timeline has {actual} subtitles; expected {expected}; "
            "refusing to append an SRT over a partial FCPXML caption import"
        )
    if expected == 0:
        return {"status": "not_required", "count": 0}

    subtitles_path = _verified_subtitle_import_path(
        plan, plan_path, project_root
    )
    selected = _call_required(project, "SetCurrentTimeline", timeline)
    if selected is not True:
        raise ResolveExecutionError(
            "SetCurrentTimeline() did not select the imported timeline before "
            "subtitle fallback"
        )
    imported = _sequence_values(
        _call_required(media_pool, "ImportMedia", [os.fspath(subtitles_path)]),
        label="ImportMedia([subtitles.srt])",
    )
    if len(imported) != 1 or imported[0] is None:
        raise ResolveExecutionError(
            "ImportMedia([subtitles.srt]) did not create exactly one subtitle "
            f"Media Pool item; returned {len(imported)}"
        )
    appended = _sequence_values(
        _call_required(media_pool, "AppendToTimeline", imported),
        label="AppendToTimeline([subtitle MediaPoolItem])",
    )
    if not appended:
        raise ResolveExecutionError(
            "AppendToTimeline() did not append the subtitle Media Pool item"
        )
    items = _subtitle_items(timeline)
    actual = len(items)
    if actual != expected:
        raise ImmutableTimelineError(
            f"SRT fallback produced {actual} editable subtitles; expected {expected}"
        )
    _validate_subtitle_items(
        timeline,
        items,
        plan,
        require_text=True,
    )
    return {
        "status": "srt_imported",
        "count": actual,
        "path": os.fspath(subtitles_path),
    }


def _timeline_marker_documents(timeline: Any) -> list[dict[str, Any]]:
    marker_getter = getattr(timeline, "GetMarkers", None)
    if not callable(marker_getter):
        return []
    marker_map = marker_getter()
    if not isinstance(marker_map, Mapping):
        raise ImmutableTimelineError("timeline returned invalid marker metadata")
    documents: list[dict[str, Any]] = []
    for raw_frame, marker in marker_map.items():
        if not isinstance(marker, Mapping):
            continue
        raw = marker.get("customData", marker.get("custom_data"))
        if not isinstance(raw, str) or not raw:
            continue
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            try:
                frame = int(float(raw_frame))
            except (TypeError, ValueError) as exc:
                raise ImmutableTimelineError(
                    f"timeline returned invalid marker frame {raw_frame!r}"
                ) from exc
            document = dict(decoded)
            document["_timeline_frame"] = frame
            documents.append(document)
    return documents


def _primary_transition_count_from_plan(
    clips: Sequence[Mapping[str, Any]],
) -> int:
    """Infer the pre-v9 V1 transition contract from a compiled plan."""

    primary = sorted(
        (
            clip
            for clip in clips
            if str(clip.get("track") or "V1") == "V1"
        ),
        key=lambda item: (int(item["start_frame"]), str(item["id"])),
    )
    previous: Mapping[str, Any] | None = None
    cursor = 0
    count = 0
    supported = {"cross_dissolve", "dip_to_black", "fade"}
    for clip in primary:
        start = int(clip["start_frame"])
        if (
            "end_frame" not in clip
            or "duration_frames" not in clip
        ):
            previous = None
            cursor = max(cursor, start)
            continue
        if start > cursor:
            previous = None
        if (
            previous is not None
            and start == int(previous["end_frame"])
            and previous.get("asset_id")
            and previous.get("media_path")
            and clip.get("asset_id")
            and clip.get("media_path")
        ):
            transition = clip.get("transition") or {}
            kind = str(transition.get("kind") or "cut")
            requested = int(transition.get("duration_frames") or 0)
            duration = min(
                requested,
                max(0, int(previous["duration_frames"]) // 2),
                max(0, int(clip["duration_frames"]) // 2),
            )
            if kind in supported and duration > 0:
                count += 1
        cursor = max(cursor, int(clip["end_frame"]))
        previous = clip
    return count


def _validate_imported_items(
    timeline: Any,
    plan: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> None:
    getter = getattr(timeline, "GetItemListInTrack", None)
    if not callable(getter):
        raise ImmutableTimelineError(
            "timeline cannot validate imported clip/subtitle items"
        )

    raw_clips = plan.get("clips")
    clips = (
        [item for item in raw_clips if isinstance(item, Mapping)]
        if isinstance(raw_clips, Sequence)
        and not isinstance(raw_clips, (str, bytes))
        else []
    )
    expected_total = int(
        validation.get(
            "video_clip_count",
            sum(1 for item in clips if item.get("media_path")),
        )
    )
    raw_overlays = plan.get("overlays")
    overlays = (
        [item for item in raw_overlays if isinstance(item, Mapping)]
        if isinstance(raw_overlays, Sequence)
        and not isinstance(raw_overlays, (str, bytes))
        else []
    )
    title_overlays = [
        item
        for item in overlays
        if item.get("text")
        and str(item.get("kind") or "").lower()
        not in {"caption", "captions", "subtitle", "subtitles"}
    ]
    expected_title_total = int(
        validation.get("video_title_count", len(title_overlays))
    )
    transition_contract = validation.get("video_transition_count")
    expected_transition_total = (
        _primary_transition_count_from_plan(clips)
        if transition_contract is None
        else int(transition_contract)
    )
    actual_media_total = 0
    actual_generated_total = 0
    for spec in _track_specs(plan, "video"):
        items = _sequence_values(
            getter("video", spec["index"]),
            label=f"GetItemListInTrack('video', {spec['index']})",
        )
        expected_media_track = sum(
            1
            for clip in clips
            if clip.get("media_path") and clip.get("track") == spec["id"]
        )
        expected_title_track = sum(
            1
            for overlay in title_overlays
            if str(overlay.get("track") or "V3") == spec["id"]
        )
        expected_transition_track = (
            expected_transition_total if spec["id"] == "V1" else 0
        )
        expected_track_total = (
            expected_media_track
            + expected_title_track
            + expected_transition_track
        )
        if len(items) != expected_track_total:
            raise ImmutableTimelineError(
                f"imported {spec['id']} has {len(items)} items; "
                f"expected {expected_media_track} media clips and "
                f"{expected_title_track} titles and "
                f"{expected_transition_track} transitions"
            )
        linked_count = 0
        for item in items:
            media_getter = getattr(item, "GetMediaPoolItem", None)
            if not callable(media_getter):
                raise ImmutableTimelineError(
                    f"imported {spec['id']} contains an item that cannot "
                    "report media linkage"
                )
            if media_getter() is not None:
                linked_count += 1
        unlinked_count = len(items) - linked_count
        if (
            linked_count != expected_media_track
            or unlinked_count
            != expected_title_track + expected_transition_track
        ):
            raise ImmutableTimelineError(
                f"imported {spec['id']} contains {linked_count} linked media "
                f"items and {unlinked_count} unlinked/generated items; expected "
                f"{expected_media_track} linked media clips and "
                f"{expected_title_track} titles plus "
                f"{expected_transition_track} transitions"
            )
        actual_media_total += linked_count
        actual_generated_total += unlinked_count
    if actual_media_total != expected_total:
        raise ImmutableTimelineError(
            f"imported timeline has {actual_media_total} linked video clips; "
            f"expected {expected_total}"
        )
    if actual_generated_total != (
        expected_title_total + expected_transition_total
    ):
        raise ImmutableTimelineError(
            f"imported timeline has {actual_generated_total} generated video "
            f"items; expected {expected_title_total} titles and "
            f"{expected_transition_total} transitions"
        )

    raw_audio = plan.get("audio")
    audio_clips = (
        [item for item in raw_audio if isinstance(item, Mapping)]
        if isinstance(raw_audio, Sequence)
        and not isinstance(raw_audio, (str, bytes))
        else []
    )
    expected_audio_total = int(
        validation.get(
            "audio_clip_count",
            sum(1 for item in audio_clips if item.get("media_path")),
        )
    )
    actual_audio_total = 0
    for spec in _track_specs(plan, "audio"):
        items = _sequence_values(
            getter("audio", spec["index"]),
            label=f"GetItemListInTrack('audio', {spec['index']})",
        )
        actual_audio_total += len(items)
        expected_track = sum(
            1
            for clip in audio_clips
            if clip.get("media_path") and clip.get("track") == spec["id"]
        )
        if len(items) != expected_track:
            raise ImmutableTimelineError(
                f"imported {spec['id']} has {len(items)} items; "
                f"expected {expected_track}"
            )
        for item in items:
            media_getter = getattr(item, "GetMediaPoolItem", None)
            if not callable(media_getter) or media_getter() is None:
                raise ImmutableTimelineError(
                    f"imported {spec['id']} contains an unlinked media item"
                )
    if actual_audio_total != expected_audio_total:
        raise ImmutableTimelineError(
            f"imported timeline has {actual_audio_total} linked audio clips; "
            f"expected {expected_audio_total}"
        )

    subtitle_items = _sequence_values(
        getter("subtitle", 1),
        label="GetItemListInTrack('subtitle', 1)",
    )
    expected_subtitles = int(
        validation.get(
            "subtitle_count",
            len(plan.get("subtitles", []))
            if isinstance(plan.get("subtitles"), Sequence)
            else 0,
        )
    )
    if len(subtitle_items) != expected_subtitles:
        raise ImmutableTimelineError(
            f"imported timeline has {len(subtitle_items)} subtitles; "
            f"expected {expected_subtitles}"
        )


def _validate_expected_timeline(
    timeline: Any,
    plan: Mapping[str, Any],
    validators: Iterable[Callable[[Any, Mapping[str, Any]], Any]],
) -> None:
    expected_name = timeline_name_for_plan(plan)
    if _timeline_name(timeline) != expected_name:
        raise ImmutableTimelineError(
            f"timeline validation selected {_timeline_name(timeline)!r}, "
            f"expected {expected_name!r}"
        )
    for kind in ("video", "audio"):
        counter = getattr(timeline, "GetTrackCount", None)
        if not callable(counter):
            continue
        specs = _track_specs(plan, kind)
        expected_count = max((item["index"] for item in specs), default=0)
        actual_count = counter(kind)
        if (
            isinstance(actual_count, bool)
            or not isinstance(actual_count, int)
            or actual_count < expected_count
        ):
            raise ImmutableTimelineError(
                f"existing {expected_name!r} has {actual_count!r} {kind} tracks; "
                f"expected at least {expected_count}"
            )
        name_getter = getattr(timeline, "GetTrackName", None)
        if callable(name_getter):
            for spec in specs:
                actual_name = name_getter(kind, spec["index"])
                if actual_name != spec["id"]:
                    raise ImmutableTimelineError(
                        f"existing {expected_name!r} {kind} track "
                        f"{spec['index']} is {actual_name!r}; expected {spec['id']!r}"
                    )
    subtitle_counter = getattr(timeline, "GetTrackCount", None)
    if callable(subtitle_counter):
        subtitle_count = subtitle_counter("subtitle")
        if (
            isinstance(subtitle_count, bool)
            or not isinstance(subtitle_count, int)
            or subtitle_count < 1
        ):
            raise ImmutableTimelineError(
                f"existing {expected_name!r} has no subtitle track"
            )
    _validate_subtitle_items(
        timeline,
        _subtitle_items(timeline),
        plan,
        require_text=True,
    )

    marker_getter = getattr(timeline, "GetMarkers", None)
    if callable(marker_getter):
        _validate_complete_marker_contract(timeline, plan)
    validation = plan.get("timeline_validation")
    if isinstance(validation, Mapping):
        start = int(_call_required(timeline, "GetStartFrame"))
        end = int(_call_required(timeline, "GetEndFrame"))
        expected_duration = int(validation["end_frame"]) - int(
            validation["start_frame"]
        )
        if abs((end - start) - expected_duration) > 1:
            raise ImmutableTimelineError(
                f"existing generated timeline duration is {end - start} frames, "
                f"expected {expected_duration}"
            )
        _validate_imported_items(timeline, plan, validation)
    for validator in validators:
        verdict = validator(timeline, plan)
        if verdict is False:
            name = getattr(validator, "__name__", repr(validator))
            raise ImmutableTimelineError(
                f"timeline validation hook {name} rejected {expected_name}"
            )


def _track_specs(plan: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    tracks = plan.get("tracks")
    values = tracks.get(kind) if isinstance(tracks, Mapping) else None
    fallback = (
        ("V1", "V2", "V3", "V4")
        if kind == "video"
        else ("A1", "A2", "A3", "A4", "A5")
    )
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return [
            {"id": name, "index": index}
            for index, name in enumerate(fallback, start=1)
        ]
    result: list[dict[str, Any]] = []
    for position, value in enumerate(values, start=1):
        if not isinstance(value, Mapping):
            raise ResolveExecutionError(f"plan tracks.{kind} entries must be objects")
        name = str(value.get("id") or value.get("name") or "")
        index = int(value.get("index", position))
        expected_prefix = "V" if kind == "video" else "A"
        if not name.startswith(expected_prefix) or index <= 0:
            raise ResolveExecutionError(
                f"invalid {kind} track declaration: {dict(value)!r}"
            )
        result.append({"id": name, "index": index})
    result.sort(key=lambda item: item["index"])
    return result


def _materialize_sparse_audio_tracks(
    timeline: Any,
    plan: Mapping[str, Any],
    specs: Sequence[Mapping[str, Any]],
    count: int,
) -> int:
    """Restore logical A-track gaps that Resolve compacts during FCPXML import."""

    getter = getattr(timeline, "GetItemListInTrack", None)
    if not callable(getter) or count <= 0:
        return count
    raw_audio = plan.get("audio")
    audio_clips = (
        [item for item in raw_audio if isinstance(item, Mapping)]
        if isinstance(raw_audio, Sequence)
        and not isinstance(raw_audio, (str, bytes))
        else []
    )
    desired = max((int(spec["index"]) for spec in specs), default=0)
    ids_by_index = {
        int(spec["index"]): str(spec["id"]) for spec in specs
    }
    expected_counts = [
        sum(
            1
            for clip in audio_clips
            if clip.get("media_path")
            and str(clip.get("track") or "") == ids_by_index.get(index, "")
        )
        for index in range(1, desired + 1)
    ]
    actual_counts = [
        len(
            _sequence_values(
                getter("audio", index),
                label=f"GetItemListInTrack('audio', {index})",
            )
        )
        for index in range(1, count + 1)
    ]

    def trim_trailing_zeros(values: Sequence[int]) -> list[int]:
        result = list(values)
        while result and result[-1] == 0:
            result.pop()
        return result

    expected_logical = trim_trailing_zeros(expected_counts)
    actual = trim_trailing_zeros(actual_counts)
    if actual == expected_logical:
        return count
    expected_compact = [value for value in expected_counts if value > 0]
    if actual != expected_compact:
        return count

    for index, expected in enumerate(expected_counts, start=1):
        if expected != 0 or not any(expected_counts[index:]):
            continue
        added = _call_required(
            timeline,
            "AddTrack",
            "audio",
            {"audioType": "stereo", "index": index},
        )
        if added is not True:
            raise ResolveExecutionError(
                f"AddTrack('audio', index={index}) did not restore "
                "the compacted Resolve audio-lane gap"
            )
        count += 1
    return count


def _ensure_and_name_tracks(timeline: Any, plan: Mapping[str, Any]) -> None:
    """Apply the plan's track contract to a newly imported timeline only."""

    for kind in ("video", "audio"):
        specs = _track_specs(plan, kind)
        desired = max((item["index"] for item in specs), default=0)
        count = _call_required(timeline, "GetTrackCount", kind)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ResolveExecutionError(
                f"GetTrackCount({kind!r}) returned {count!r}"
            )
        if kind == "audio":
            count = _materialize_sparse_audio_tracks(
                timeline,
                plan,
                specs,
                count,
            )
        while count < desired:
            added = _call_required(timeline, "AddTrack", kind)
            if added is not True:
                raise ResolveExecutionError(
                    f"AddTrack({kind!r}) did not create track {count + 1}"
                )
            count += 1
        for spec in specs:
            named = _call_required(
                timeline, "SetTrackName", kind, spec["index"], spec["id"]
            )
            if named is not True:
                raise ResolveExecutionError(
                    f"SetTrackName({kind!r}, {spec['index']}, {spec['id']!r}) "
                    "did not succeed"
                )

    subtitle_count = _call_required(timeline, "GetTrackCount", "subtitle")
    if (
        isinstance(subtitle_count, bool)
        or not isinstance(subtitle_count, int)
        or subtitle_count < 0
    ):
        raise ResolveExecutionError(
            f"GetTrackCount('subtitle') returned {subtitle_count!r}"
        )
    if subtitle_count < 1:
        added = _call_required(timeline, "AddTrack", "subtitle")
        if added is not True:
            raise ResolveExecutionError("AddTrack('subtitle') did not succeed")
    subtitle_policy = plan.get("subtitle_policy")
    track_name = (
        subtitle_policy.get("timeline_track_name")
        if isinstance(subtitle_policy, Mapping)
        else None
    )
    if not isinstance(track_name, str) or not track_name.strip():
        track_name = "SUBTITLES"
    named = _call_required(timeline, "SetTrackName", "subtitle", 1, track_name)
    if named is not True:
        raise ResolveExecutionError(
            f"SetTrackName('subtitle', 1, {track_name!r}) did not succeed"
        )


def _marker_color(kind: str, severity: str | None = None) -> str:
    if severity == "error":
        return "Red"
    if severity in {"warning", "human"}:
        return "Yellow"
    if "chapter" in kind:
        return "Purple"
    if "highlight" in kind:
        return "Yellow"
    if "redact" in kind:
        return "Red"
    return "Blue"


def _marker_provenance(
    marker: Mapping[str, Any],
    reviews: Sequence[Mapping[str, Any]],
    provenance: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    asset_ids = {
        str(value)
        for value in (
            marker.get("asset_id"),
            marker.get("source_id"),
            *(review.get("asset_id") for review in reviews),
        )
        if value
    }
    matched: list[dict[str, Any]] = []
    for record in provenance:
        identity = record.get("asset_id") or record.get("id")
        if identity is not None and str(identity) in asset_ids:
            matched.append(dict(record))
    explicit = marker.get("provenance")
    if isinstance(explicit, Mapping):
        matched.append(dict(explicit))
    elif isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
        matched.extend(dict(item) for item in explicit if isinstance(item, Mapping))
    return matched


def _style_marker_summary(
    style_result: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(style_result, Mapping):
        return None
    raw_records = style_result.get("clips")
    records = (
        raw_records
        if isinstance(raw_records, Sequence)
        and not isinstance(raw_records, (str, bytes))
        else []
    )
    methods = sorted(
        {
            str(record.get("method"))
            for record in records
            if isinstance(record, Mapping) and record.get("method")
        }
    )
    return {
        "schema_version": style_result.get("schema_version"),
        "status": style_result.get("status"),
        "contract_sha256": style_result.get("contract_sha256"),
        "eligible_clip_count": style_result.get("eligible_clip_count", 0),
        "applied_clip_count": style_result.get("applied_clip_count", 0),
        "methods": methods,
        "fusion": style_result.get("fusion"),
    }


def _group_plan_marker_records(
    plan: Mapping[str, Any],
) -> dict[int, dict[str, list[dict[str, Any]]]]:
    raw_markers = plan.get("markers")
    markers = (
        [dict(item) for item in raw_markers if isinstance(item, Mapping)]
        if isinstance(raw_markers, Sequence)
        and not isinstance(raw_markers, (str, bytes))
        else []
    )
    raw_reviews = plan.get("review_flags")
    reviews = (
        [dict(item) for item in raw_reviews if isinstance(item, Mapping)]
        if isinstance(raw_reviews, Sequence)
        and not isinstance(raw_reviews, (str, bytes))
        else []
    )
    grouped: dict[int, dict[str, list[dict[str, Any]]]] = {}
    for marker in markers:
        frame = int(marker.get("frame", marker.get("start_frame", 0)))
        grouped.setdefault(frame, {"markers": [], "reviews": []})[
            "markers"
        ].append(marker)
    for review in reviews:
        frame = int(review.get("frame", review.get("start_frame", 0)))
        grouped.setdefault(frame, {"markers": [], "reviews": []})[
            "reviews"
        ].append(review)
    if 0 not in grouped:
        grouped[0] = {
            "markers": [
                {
                    "id": f"build-{build_hash_for_plan(plan)}",
                    "kind": "build_identity",
                    "arg": "RabbitHole immutable build identity",
                    "frame": 0,
                }
            ],
            "reviews": [],
        }
    return grouped


_MARKER_ADD_RETRY_DELAYS_SECONDS = (0.05, 0.15, 0.30)


def _marker_at_frame(
    markers: Mapping[Any, Any],
    frame: int,
) -> Mapping[str, Any] | None:
    """Return one Resolve marker while tolerating numeric-string frame keys."""

    for raw_frame, marker in markers.items():
        try:
            marker_frame = int(float(raw_frame))
        except (TypeError, ValueError):
            continue
        if marker_frame == frame and isinstance(marker, Mapping):
            return marker
    return None


def _marker_shell_matches(
    marker: Mapping[str, Any],
    *,
    color: str,
    name: str,
    note: str,
    duration: int,
) -> bool:
    """Identify only the marker shell created by the current runner call."""

    try:
        actual_duration = int(marker.get("duration", 0))
    except (TypeError, ValueError):
        return False
    return (
        marker.get("color") == color
        and marker.get("name") == name
        and marker.get("note") == note
        and actual_duration == duration
    )


def _add_marker_with_bounded_retry(
    timeline: Any,
    *,
    frame: int,
    color: str,
    name: str,
    note: str,
    duration: int,
) -> bool:
    """Boundedly retry marker creation after a ``False`` response.

    The API returns only a Boolean and no rejection detail. Every retry is
    bounded, and a marker that appears asynchronously is accepted only when
    its visible shell exactly matches this call.
    """

    attempts = len(_MARKER_ADD_RETRY_DELAYS_SECONDS) + 1
    for attempt in range(attempts):
        if attempt:
            time.sleep(_MARKER_ADD_RETRY_DELAYS_SECONDS[attempt - 1])
            current = _call_required(timeline, "GetMarkers")
            if not isinstance(current, Mapping):
                raise ResolveExecutionError(
                    "GetMarkers() returned invalid metadata during marker retry"
                )
            existing = _marker_at_frame(current, frame)
            if existing is not None:
                return _marker_shell_matches(
                    existing,
                    color=color,
                    name=name,
                    note=note,
                    duration=duration,
                )

        added = _call_required(
            timeline,
            "AddMarker",
            frame,
            color,
            name,
            note,
            duration,
            "",
        )
        if added is True:
            return True

        current = _call_required(timeline, "GetMarkers")
        if not isinstance(current, Mapping):
            raise ResolveExecutionError(
                "GetMarkers() returned invalid metadata after AddMarker()"
            )
        existing = _marker_at_frame(current, frame)
        if existing is not None:
            return _marker_shell_matches(
                existing,
                color=color,
                name=name,
                note=note,
                duration=duration,
            )
    return False


def _validate_complete_marker_contract(
    timeline: Any,
    plan: Mapping[str, Any],
) -> None:
    """Require every RabbitHole marker document before reuse or rendering."""

    documents = _timeline_marker_documents(timeline)
    grouped = _group_plan_marker_records(plan)
    build_id = str(plan.get("build_id") or f"b-{build_hash_for_plan(plan)}")
    raw_provenance = plan.get("provenance")
    provenance = (
        [dict(item) for item in raw_provenance if isinstance(item, Mapping)]
        if isinstance(raw_provenance, Sequence)
        and not isinstance(raw_provenance, (str, bytes))
        else []
    )
    build_documents = [
        document
        for document in documents
        if document.get("build_id") == build_id
    ]
    if len(build_documents) != len(grouped):
        raise ImmutableTimelineError(
            f"existing {_timeline_name(timeline)!r} has "
            f"{len(build_documents)} complete RabbitHole markers for "
            f"{build_id!r}; expected {len(grouped)}"
        )

    style_documents: list[dict[str, Any]] = []
    for frame, entries in sorted(grouped.items()):
        matches = [
            document
            for document in build_documents
            if document.get("_timeline_frame") == frame
        ]
        if len(matches) != 1:
            raise ImmutableTimelineError(
                f"existing {_timeline_name(timeline)!r} has {len(matches)} "
                f"RabbitHole markers for {build_id!r} at frame {frame}; "
                "expected exactly one"
            )
        actual = matches[0]
        frame_markers = entries["markers"]
        frame_reviews = entries["reviews"]
        primary: Mapping[str, Any] = (
            frame_markers[0] if frame_markers else frame_reviews[0]
        )
        marker_id = str(primary.get("id") or f"frame-{frame}")
        expected_fields = {
            "schema": "rabbithole.resolve-marker.v1",
            "build_id": build_id,
            "marker_id": marker_id,
            "marker_ids": [
                str(item.get("id") or f"frame-{frame}")
                for item in frame_markers
            ],
            "provenance": _marker_provenance(
                primary, frame_reviews, provenance
            ),
            "review": frame_reviews,
            "markers": frame_markers,
        }
        for field, expected in expected_fields.items():
            if actual.get(field) != expected:
                raise ImmutableTimelineError(
                    f"existing {_timeline_name(timeline)!r} RabbitHole marker "
                    f"at frame {frame} has invalid {field!r} metadata"
                )
        actual_style = actual.get("style")
        if not isinstance(actual_style, Mapping):
            raise ImmutableTimelineError(
                f"existing {_timeline_name(timeline)!r} RabbitHole marker at "
                f"frame {frame} has no Resolve style metadata"
            )
        style_documents.append(dict(actual_style))

    if any(style != style_documents[0] for style in style_documents[1:]):
        raise ImmutableTimelineError(
            f"existing {_timeline_name(timeline)!r} has inconsistent Resolve "
            "style metadata across RabbitHole markers"
        )
    style = style_documents[0]
    raw_style = plan.get("style")
    if isinstance(raw_style, Mapping):
        try:
            expected_style_hash = style_contract_hash(raw_style)
        except ResolveStyleError as exc:
            raise ImmutableTimelineError(str(exc)) from exc
        if style.get("contract_sha256") != expected_style_hash:
            raise ImmutableTimelineError(
                f"existing {_timeline_name(timeline)!r} has no matching Resolve "
                f"style marker for {expected_style_hash}"
            )
    else:
        expected_disabled = {
            "status": "disabled",
            "contract_sha256": None,
            "eligible_clip_count": 0,
            "applied_clip_count": 0,
            "methods": [],
            "fusion": "not_requested",
        }
        if any(style.get(key) != value for key, value in expected_disabled.items()):
            raise ImmutableTimelineError(
                f"existing {_timeline_name(timeline)!r} has invalid disabled "
                "Resolve style metadata"
            )


def _add_plan_markers(
    timeline: Any,
    plan: Mapping[str, Any],
    *,
    style_result: Mapping[str, Any] | None = None,
) -> None:
    """Add machine-readable plan/review markers to a newly imported timeline."""

    raw_provenance = plan.get("provenance")
    provenance = (
        [dict(item) for item in raw_provenance if isinstance(item, Mapping)]
        if isinstance(raw_provenance, Sequence)
        and not isinstance(raw_provenance, (str, bytes))
        else []
    )
    grouped = _group_plan_marker_records(plan)
    build_id = str(plan.get("build_id") or f"b-{build_hash_for_plan(plan)}")
    existing_raw = _call_required(timeline, "GetMarkers")
    if not isinstance(existing_raw, Mapping):
        raise ResolveExecutionError("GetMarkers() returned invalid metadata")
    existing = dict(existing_raw)
    style_summary = _style_marker_summary(style_result)
    created_frames: list[int] = []
    updated_existing: list[tuple[int, str]] = []
    try:
        for frame, entries in sorted(grouped.items()):
            frame_markers = entries["markers"]
            frame_reviews = entries["reviews"]
            primary: Mapping[str, Any] = (
                frame_markers[0] if frame_markers else frame_reviews[0]
            )
            marker_id = str(primary.get("id") or f"frame-{frame}")
            kind = str(primary.get("kind") or "review")
            severity = (
                str(frame_reviews[0].get("severity"))
                if frame_reviews
                and frame_reviews[0].get("severity") is not None
                else None
            )
            custom_data = {
                "schema": "rabbithole.resolve-marker.v1",
                "build_id": build_id,
                "marker_id": marker_id,
                "marker_ids": [
                    str(item.get("id") or f"frame-{frame}")
                    for item in frame_markers
                ],
                "provenance": _marker_provenance(
                    primary, frame_reviews, provenance
                ),
                "review": frame_reviews,
                "markers": frame_markers,
            }
            if style_summary is not None:
                custom_data["style"] = style_summary
            note_parts = [
                str(
                    item.get("arg")
                    or item.get("message")
                    or item.get("kind")
                    or ""
                )
                for item in (*frame_markers, *frame_reviews)
            ]
            if frame == 0 and style_summary is not None:
                note_parts.append(
                    "Resolve style="
                    f"{style_summary.get('status')}; "
                    "Fusion titles/transitions and V4 texture remain editable "
                    "manual intent"
                )
            note = " | ".join(part for part in note_parts if part)[:2048]
            duration = max(
                1,
                max(
                    (
                        int(item.get("duration_frames", 1))
                        for item in (*frame_markers, *frame_reviews)
                    ),
                    default=1,
                ),
            )
            encoded = json.dumps(
                custom_data, ensure_ascii=False, sort_keys=True
            )
            existing_marker = _marker_at_frame(existing, frame)
            if isinstance(existing_marker, Mapping):
                previous = existing_marker.get(
                    "customData", existing_marker.get("custom_data", "")
                )
                updated_existing.append(
                    (frame, previous if isinstance(previous, str) else "")
                )
                updated = _call_required(
                    timeline, "UpdateMarkerCustomData", frame, encoded
                )
                if updated is not True:
                    raise ResolveExecutionError(
                        "UpdateMarkerCustomData() did not succeed at frame "
                        f"{frame}"
                    )
            else:
                color = _marker_color(kind, severity)
                name = f"RH {kind}: {marker_id}"[:128]
                added = _add_marker_with_bounded_retry(
                    timeline,
                    frame=frame,
                    color=color,
                    name=name,
                    note=note,
                    duration=duration,
                )
                if added is not True:
                    raise ResolveExecutionError(
                        f"AddMarker() did not succeed at frame {frame}"
                    )
                created_frames.append(frame)
                updated = _call_required(
                    timeline, "UpdateMarkerCustomData", frame, encoded
                )
                if updated is not True:
                    raise ResolveExecutionError(
                        "UpdateMarkerCustomData() did not persist metadata for "
                        f"the new marker at frame {frame}"
                    )
    except Exception as exc:
        rollback_failures: list[str] = []
        delete_marker = getattr(timeline, "DeleteMarkerAtFrame", None)
        for frame in reversed(created_frames):
            try:
                deleted = (
                    delete_marker(frame) if callable(delete_marker) else False
                )
            except Exception as rollback_exc:
                rollback_failures.append(
                    f"delete frame {frame}: {rollback_exc}"
                )
            else:
                if deleted is not True:
                    rollback_failures.append(f"delete frame {frame}: rejected")
        restore_custom = getattr(timeline, "UpdateMarkerCustomData", None)
        for frame, previous in reversed(updated_existing):
            try:
                restored = (
                    restore_custom(frame, previous)
                    if callable(restore_custom)
                    else False
                )
            except Exception as rollback_exc:
                rollback_failures.append(
                    f"restore frame {frame}: {rollback_exc}"
                )
            else:
                if restored is not True:
                    rollback_failures.append(
                        f"restore frame {frame}: rejected"
                    )
        if rollback_failures:
            raise ResolveExecutionError(
                f"{exc}; marker rollback incomplete: "
                + "; ".join(rollback_failures)
            ) from exc
        raise


def _create_project_when_none(
    manager: Any,
    project_root: Path,
    plan: Mapping[str, Any],
) -> Any:
    name = deterministic_project_name(project_root, plan)
    media_location: Any = plan.get("media_location_path")
    project_block = plan.get("project")
    if media_location is None and isinstance(project_block, Mapping):
        media_location = project_block.get("media_location_path")
    if media_location is None:
        created = _call_required(manager, "CreateProject", name)
    else:
        media_path = Path(str(media_location)).expanduser()
        if not media_path.is_absolute():
            media_path = project_root / media_path
        media_path = media_path.resolve(strict=False)
        if not is_path_within(media_path, project_root):
            raise UnsafeWriteError(
                f"Resolve project media location escapes project: {media_path}"
            )
        media_path.mkdir(parents=True, exist_ok=True)
        created = _call_required(manager, "CreateProject", name, os.fspath(media_path))
    if created is None or created is False:
        raise ResolveExecutionError(
            f"CreateProject({name!r}) failed. The deterministic project name may "
            "already exist; the runner will not LoadProject or switch projects."
        )
    return created


def execute_build(
    resolve: Any,
    plan: Mapping[str, Any] | os.PathLike[str] | str,
    *,
    project_root: os.PathLike[str] | str,
    plan_path: os.PathLike[str] | str | None = None,
    timeline_validators: Iterable[
        Callable[[Any, Mapping[str, Any]], Any]
    ] = (),
) -> dict[str, Any]:
    """Create or idempotently reuse one immutable generated timeline."""

    root = _canonical(project_root)
    if isinstance(plan, (str, os.PathLike)):
        loaded_path, loaded = _load_plan(plan)
        plan_data = loaded
        plan_file = loaded_path
    else:
        plan_data = dict(plan)
        plan_file = _canonical(plan_path) if plan_path else root / "resolve" / "plan.json"

    manager = get_project_manager(resolve)
    project = get_current_project(manager)
    project_created = False
    if project is None:
        project = _create_project_when_none(manager, root, plan_data)
        project_created = True
    require_render_idle(project)
    actual_project_name = project_name(project)

    target_name = timeline_name_for_plan(plan_data)
    existing = _find_generated_timeline(project, target_name)
    if existing is not None:
        _validate_expected_timeline(existing, plan_data, timeline_validators)
        selected = _call_required(project, "SetCurrentTimeline", existing)
        if selected is not True:
            raise ResolveExecutionError(
                f"SetCurrentTimeline({target_name!r}) did not succeed"
            )
        result = BuildResult(
            action="build",
            build_hash=build_hash_for_plan(plan_data),
            project_name=actual_project_name,
            timeline_name=target_name,
            reused=True,
            project_created=project_created,
            import_path=None,
        )
        payload = asdict(result)
        raw_style = plan_data.get("style")
        if isinstance(raw_style, Mapping):
            payload["style"] = {
                "status": "reused_immutable",
                "contract_sha256": style_contract_hash(raw_style),
            }
        else:
            payload["style"] = {"status": "disabled", "contract_sha256": None}
        return payload

    import_path = timeline_import_path(plan_data, plan_file, root)
    media_pool = _call_required(project, "GetMediaPool")
    import_options: dict[str, Any] = {}
    timeline_block = plan_data.get("timeline")
    if isinstance(timeline_block, Mapping) and isinstance(
        timeline_block.get("import_options"), Mapping
    ):
        import_options.update(dict(timeline_block["import_options"]))
    if isinstance(plan_data.get("import_options"), Mapping):
        import_options.update(dict(plan_data["import_options"]))
    import_options["timelineName"] = target_name

    imported = _call_required(
        media_pool,
        "ImportTimelineFromFile",
        os.fspath(import_path),
        import_options,
    )
    if imported is None or imported is False:
        raise ResolveExecutionError(
            f"ImportTimelineFromFile() did not create {target_name!r}"
        )
    imported_name = _timeline_name(imported)
    if imported_name != target_name:
        # Renaming is allowed only for the timeline created by this call.  The
        # runner never calls SetName on an existing generated/editorial timeline.
        renamed = _call_required(imported, "SetName", target_name)
        if renamed is not True or _timeline_name(imported) != target_name:
            raise ResolveExecutionError(
                f"imported timeline is {imported_name!r} and could not be named "
                f"{target_name!r}"
            )

    _ensure_and_name_tracks(imported, plan_data)
    subtitle_result = _ensure_imported_subtitles(
        project,
        media_pool,
        imported,
        plan_data,
        plan_file,
        root,
    )
    try:
        style_result = apply_style_to_new_timeline(
            project,
            imported,
            plan_data,
            project_root=root,
        )
    except ResolveStyleError as exc:
        raise ResolveExecutionError(str(exc)) from exc
    _add_plan_markers(imported, plan_data, style_result=style_result)
    _validate_expected_timeline(imported, plan_data, timeline_validators)
    selected = _call_required(project, "SetCurrentTimeline", imported)
    if selected is not True:
        raise ResolveExecutionError(
            f"SetCurrentTimeline({target_name!r}) did not succeed"
        )
    saved = _call_required(manager, "SaveProject")
    if saved is not True:
        raise ResolveExecutionError(
            "SaveProject() did not persist the validated generated timeline"
        )
    result = BuildResult(
        action="build",
        build_hash=build_hash_for_plan(plan_data),
        project_name=actual_project_name,
        timeline_name=target_name,
        reused=False,
        project_created=project_created,
        import_path=os.fspath(import_path),
    )
    payload = asdict(result)
    payload["style"] = style_result
    payload["subtitles"] = subtitle_result
    payload["saved"] = True
    return payload


def _render_settings(
    plan: Mapping[str, Any],
    project_root: Path,
    *,
    output_path: os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    render = plan.get("render")
    if isinstance(render, Mapping):
        nested = render.get("settings")
        if isinstance(nested, Mapping):
            settings.update(dict(nested))
        for key, value in render.items():
            if key not in {
                "settings",
                "wait",
                "wait_timeout_seconds",
                "format",
                "codec",
                "mode",
            }:
                settings.setdefault(str(key), value)
    direct = plan.get("render_settings")
    if isinstance(direct, Mapping):
        settings.update(dict(direct))

    if output_path is not None:
        requested_output = Path(output_path).expanduser()
        if not requested_output.is_absolute():
            requested_output = project_root / requested_output
        requested_output = requested_output.resolve(strict=False)
        render_format = (
            str(render.get("format") or "mp4").lower()
            if isinstance(render, Mapping)
            else "mp4"
        )
        expected_suffix = f".{render_format}"
        if requested_output.suffix and requested_output.suffix.lower() != expected_suffix:
            raise ResolveExecutionError(
                f"render output suffix {requested_output.suffix!r} does not "
                f"match the plan format {render_format!r}"
            )
        target_path = requested_output.parent
        settings["CustomName"] = requested_output.stem
    else:
        target = settings.get("TargetDir") or settings.get("target_dir")
        if target is None:
            target_path = project_root / "resolve" / "renders"
        else:
            target_path = Path(str(target)).expanduser()
            if not target_path.is_absolute():
                target_path = project_root / target_path
    target_path = require_write_path(
        target_path.resolve(strict=False),
        (project_root / "resolve" / "renders", project_root / "renders"),
    )
    target_path.mkdir(parents=True, exist_ok=True)
    settings.pop("target_dir", None)
    settings["TargetDir"] = os.fspath(target_path)
    settings.setdefault("CustomName", timeline_name_for_plan(plan))
    return settings


_RENDER_COMPLETE_STATUSES = frozenset({"complete", "completed"})
_RENDER_FAILED_STATUSES = frozenset({"failed", "cancelled", "canceled"})
_RENDER_ACTIVE_STATUSES = frozenset(
    {"ready", "rendering", "running", "queued", "pending", "waiting", "in_progress"}
)


def _normalized_render_status(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _render_job_status(
    project: Any,
    render_job_id: str,
) -> tuple[str, Mapping[str, Any] | None]:
    method = getattr(project, "GetRenderJobStatus", None)
    if not callable(method):
        return "", None
    try:
        detail = method(render_job_id)
    except Exception as exc:
        raise ResolveExecutionError(
            f"GetRenderJobStatus({render_job_id!r}) failed: {exc}"
        ) from exc
    if detail in (None, False):
        return "", None
    if not isinstance(detail, Mapping):
        raise ResolveExecutionError(
            f"GetRenderJobStatus({render_job_id!r}) returned {detail!r}"
        )
    raw_status = detail.get("JobStatus", detail.get("job_status"))
    return _normalized_render_status(raw_status), detail


def execute_render(
    resolve: Any,
    plan: Mapping[str, Any] | os.PathLike[str] | str,
    *,
    project_root: os.PathLike[str] | str,
    wait: bool = False,
    wait_timeout_seconds: float = 0,
    poll_interval_seconds: float = 1.0,
    output_path: os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    """Configure and start a render of an immutable AUTO_BUILD timeline."""

    root = _canonical(project_root)
    if isinstance(plan, (str, os.PathLike)):
        _, plan_data = _load_plan(plan)
    else:
        plan_data = dict(plan)
    target_name = timeline_name_for_plan(plan_data)
    if target_name.startswith("EDITORIAL_"):
        raise ImmutableTimelineError("automation never renders/mutates EDITORIAL timelines")

    manager = get_project_manager(resolve)
    project = get_current_project(manager)
    if project is None:
        raise ResolveExecutionError(
            "no current Resolve project; render will not create/load/switch a project"
        )
    require_render_idle(project)
    timeline = _find_generated_timeline(project, target_name)
    if timeline is None:
        raise ResolveExecutionError(
            f"current project has no immutable timeline {target_name!r}"
        )
    _validate_expected_timeline(timeline, plan_data, ())
    selected = _call_required(project, "SetCurrentTimeline", timeline)
    if selected is not True:
        raise ResolveExecutionError(
            f"SetCurrentTimeline({target_name!r}) did not succeed"
        )

    render_block = plan_data.get("render")
    render_format = (
        str(render_block.get("format") or "mp4")
        if isinstance(render_block, Mapping)
        else "mp4"
    )
    render_codec = (
        str(render_block.get("codec") or "H264")
        if isinstance(render_block, Mapping)
        else "H264"
    )
    format_configured = _call_required(
        project,
        "SetCurrentRenderFormatAndCodec",
        render_format,
        render_codec,
    )
    if format_configured is not True:
        raise ResolveExecutionError(
            "SetCurrentRenderFormatAndCodec() rejected the deterministic "
            f"{render_format}/{render_codec} render contract"
        )
    mode_configured = _call_required(project, "SetCurrentRenderMode", 1)
    if mode_configured is not True:
        raise ResolveExecutionError(
            "SetCurrentRenderMode(1) did not select single-clip rendering"
        )
    settings = _render_settings(plan_data, root, output_path=output_path)
    configured = _call_required(project, "SetRenderSettings", settings)
    if configured is not True:
        raise ResolveExecutionError("SetRenderSettings() did not succeed")
    render_job_id = _call_required(project, "AddRenderJob")
    if render_job_id in (None, False, ""):
        raise ResolveExecutionError("AddRenderJob() returned no render job ID")
    started = _call_required(project, "StartRendering", render_job_id)
    if started is not True:
        raise ResolveExecutionError(
            f"StartRendering({render_job_id!r}) did not succeed"
        )

    status = "started"
    render_block = plan_data.get("render")
    requested_wait = wait or (
        isinstance(render_block, Mapping) and render_block.get("wait") is True
    )
    timeout = wait_timeout_seconds
    if (
        timeout <= 0
        and isinstance(render_block, Mapping)
        and render_block.get("wait_timeout_seconds") is not None
    ):
        timeout = float(render_block["wait_timeout_seconds"])
    if requested_wait:
        if timeout <= 0:
            raise ResolveExecutionError(
                "render wait requested without a positive wait timeout"
            )
        deadline = time.monotonic() + timeout
        while True:
            job_status, _ = _render_job_status(project, str(render_job_id))
            if job_status in _RENDER_COMPLETE_STATUSES:
                status = "complete"
                break
            if job_status in _RENDER_FAILED_STATUSES:
                raise ResolveExecutionError(
                    f"Resolve render ended with status {job_status!r}"
                )
            checker = getattr(project, "IsRenderingInProgress", None)
            if not callable(checker):
                raise ResolveExecutionError(
                    "cannot wait for render: IsRenderingInProgress() is unavailable"
                )
            observed = checker()
            if not isinstance(observed, bool):
                raise ResolveExecutionError(
                    f"cannot wait for render: render state is {observed!r}"
                )
            if not observed:
                observed_label = job_status or "unavailable"
                raise ResolveExecutionError(
                    "Resolve stopped rendering without positively reporting "
                    f"Complete for job {render_job_id!r}; observed status "
                    f"{observed_label!r}"
                )
            if time.monotonic() >= deadline:
                raise ResolveBusyError(
                    f"render {render_job_id!r} remains active after {timeout:g}s"
                )
            time.sleep(max(0.05, min(float(poll_interval_seconds), 5.0)))

    result = RenderResult(
        action="render",
        project_name=project_name(project),
        timeline_name=target_name,
        render_job_id=str(render_job_id),
        render_status=status,
    )
    return asdict(result)


def _job_error(exc: BaseException) -> dict[str, Any]:
    return {
        "type": type(exc).__name__,
        "message": str(exc),
    }


def _rendering_job_result(job: Mapping[str, Any]) -> dict[str, Any]:
    result = job.get("result")
    if not isinstance(result, Mapping):
        raise QueueError(
            f"rendering job {job.get('job_id')!r} has no durable render result"
        )
    render_job_id = result.get("render_job_id")
    if not isinstance(render_job_id, str) or not render_job_id:
        raise QueueError(
            f"rendering job {job.get('job_id')!r} has no Resolve render job ID"
        )
    return dict(result)


def _persist_rendering_observation(
    path: Path,
    job: Mapping[str, Any],
    result: Mapping[str, Any],
    render_status: str,
) -> dict[str, Any]:
    updated = dict(job)
    observed_result = dict(result)
    observed_result["render_status"] = render_status
    updated["result"] = _json_clone(observed_result, label="job result")
    updated["updated_at"] = utc_now()
    updated["finished_at"] = None
    updated["error"] = None
    _atomic_write_json(path, updated)
    return updated


def _reconcile_rendering_job(
    path: Path,
    job: Mapping[str, Any],
    project: Any,
) -> tuple[dict[str, Any], str]:
    """Reconcile one durable render without inferring success from idleness."""

    result = _rendering_job_result(job)
    expected_project = result.get("project_name")
    actual_project = project_name(project)
    if (
        isinstance(expected_project, str)
        and expected_project
        and expected_project != actual_project
    ):
        raise ResolveBusyError(
            f"render job {job.get('job_id')!r} belongs to project "
            f"{expected_project!r}, but {actual_project!r} is currently open"
        )

    render_job_id = str(result["render_job_id"])
    observed_status, _ = _render_job_status(project, render_job_id)
    if observed_status in _RENDER_COMPLETE_STATUSES:
        result["render_status"] = "complete"
        return (
            _transition_job(path, job, "succeeded", result=result),
            "succeeded",
        )
    if observed_status in _RENDER_FAILED_STATUSES:
        result["render_status"] = observed_status
        failure = _job_error(
            ResolveExecutionError(
                f"Resolve render {render_job_id!r} ended with status "
                f"{observed_status!r}"
            )
        )
        return (
            _transition_job(
                path,
                job,
                "failed",
                result=result,
                error=failure,
            ),
            "failed",
        )

    checker = getattr(project, "IsRenderingInProgress", None)
    if not callable(checker):
        raise ResolveExecutionError(
            "cannot reconcile render: IsRenderingInProgress() is unavailable"
        )
    try:
        active = checker()
    except Exception as exc:
        raise ResolveExecutionError(
            f"IsRenderingInProgress() failed during reconciliation: {exc}"
        ) from exc
    if not isinstance(active, bool):
        raise ResolveExecutionError(
            f"cannot reconcile render: render state is {active!r}"
        )

    if active:
        status = (
            observed_status
            if observed_status in _RENDER_ACTIVE_STATUSES
            else "rendering"
        )
    else:
        # Resolve being idle proves only that no render is currently running.
        # Preserve the nonterminal state until this exact job reports Complete,
        # Failed, or Cancelled.
        status = observed_status or "completion_unverified"
    return (
        _persist_rendering_observation(path, job, result, status),
        "rendering",
    )


def _execute_job(
    connection: ResolveConnection,
    project_root: Path,
    job: Mapping[str, Any],
    plan_path: Path,
    plan: Mapping[str, Any],
    operation_lock: ProjectLock,
) -> dict[str, Any]:
    action = str(job["action"])
    options = job.get("options")
    safe_options = dict(options) if isinstance(options, Mapping) else {}
    if action == "build":
        return execute_build(
            connection.resolve,
            plan,
            project_root=project_root,
            plan_path=plan_path,
        )
    if action == "render":
        requested_output = safe_options.get("output_path")
        if requested_output is None:
            requested_output = safe_options.get("destination")
        return execute_render(
            connection.resolve,
            plan,
            project_root=project_root,
            wait=bool(safe_options.get("wait", False)),
            wait_timeout_seconds=float(safe_options.get("wait_timeout_seconds", 0)),
            output_path=requested_output,
        )
    if action == "handoff":
        from .resolve_handoff import package_handoff

        handoff_options = {
            key: value
            for key, value in safe_options.items()
            if key
            in {
                "destination",
                "fonts",
                "presets",
                "licenses",
                "include_files",
                "bundle_name",
                "include_proxy_media",
            }
        }
        return package_handoff(
            project_root,
            resolve=connection.resolve,
            plan=plan,
            operation_lock=operation_lock,
            **handoff_options,
        )
    raise QueueError(f"unsupported queued action: {action!r}")


def run_pending_jobs(
    resolve: Any = None,
    app: Any = None,
    project_root: os.PathLike[str] | str | None = None,
    queue_path: os.PathLike[str] | str | None = None,
    *,
    job_id: str | None = None,
    allow_studio_external: bool = False,
    studio_external_adapter: Callable[[], Any] | None = None,
    pointer_path: os.PathLike[str] | str | None = None,
) -> list[dict[str, Any]]:
    """Claim and run queued jobs under one project-scoped safety lock."""

    pointer: dict[str, Any] | None = None
    if project_root is None:
        pointer = read_runner_pointer(pointer_path)
        project_root = pointer["project_root"]
        if queue_path is None:
            queue_path = pointer["queue_path"]
        if job_id is None:
            job_id = pointer.get("job_id")
    root = _canonical(project_root)
    files = _queue_files(root, queue_path)
    if job_id is not None:
        files = [path for path in files if path.stem == job_id]
        if not files:
            raise QueueError(f"queued Resolve job not found: {job_id}")
    legacy_files = [path for path in files if _is_legacy_queue_job(path)]
    if job_id is not None and legacy_files:
        raise QueueError(
            "queued Resolve job uses a legacy integrity schema; explicitly "
            "enqueue a fresh job from the current plan"
        )
    files = [path for path in files if path not in legacy_files]
    if not files:
        _write_status(
            root,
            state="idle",
            active_job=None,
            last_job=None,
            detail=(
                "legacy_queue_jobs_ignored" if legacy_files else "no_queued_jobs"
            ),
        )
        return []

    connection: ResolveConnection | None = None
    results: list[dict[str, Any]] = []
    resolve_root = root / "resolve"
    declared_write_roots: list[Path] = [resolve_root]
    for path in files:
        candidate = _read_job(path)
        _validate_job_fingerprint(candidate, root)
        if candidate["state"] != "queued" or candidate["action"] != "handoff":
            continue
        candidate_options = candidate.get("options")
        if not isinstance(candidate_options, Mapping):
            continue
        destination = candidate_options.get("destination")
        if destination is not None:
            from .resolve_handoff import validate_handoff_output_root

            declared_write_roots.append(
                validate_handoff_output_root(destination, project_root=root)
            )
    allow_external_roots = any(
        not is_path_within(path, root) for path in declared_write_roots
    )
    with ProjectLock(
        root,
        stage="resolve-queue",
        write_roots=declared_write_roots,
        allow_external_write_roots=allow_external_roots,
    ) as lock:
        rendering_candidates: list[tuple[Path, dict[str, Any]]] = []
        for path in files:
            candidate = _read_job(path)
            _validate_job_fingerprint(candidate, root)
            if candidate["state"] == "rendering":
                rendering_candidates.append((path, candidate))

        if rendering_candidates:
            try:
                if connection is None:
                    connection = connect_resolve(
                        resolve=resolve,
                        app=app,
                        allow_studio_external=allow_studio_external,
                        studio_external_adapter=studio_external_adapter,
                    )
                manager = get_project_manager(connection.resolve)
                current = get_current_project(manager)
                if current is None:
                    raise ResolveExecutionError(
                        "no current Resolve project; cannot reconcile render"
                    )
            except (
                ResolveUnavailableError,
                ResolveBusyError,
                ResolveApiError,
                ResolveExecutionError,
            ) as exc:
                active_job = str(rendering_candidates[0][1]["job_id"])
                _write_status(
                    root,
                    state="rendering",
                    active_job=active_job,
                    last_job=active_job,
                    detail="awaiting_render_reconciliation",
                    error=_job_error(exc),
                )
                return [dict(candidate) for _, candidate in rendering_candidates]

            unresolved: list[dict[str, Any]] = []
            unresolved_errors: dict[str, dict[str, Any]] = {}
            reconciliation_failed = False
            for path, candidate in rendering_candidates:
                try:
                    reconciled, outcome = _reconcile_rendering_job(
                        path, candidate, current
                    )
                except (
                    ResolveBusyError,
                    ResolveApiError,
                    ResolveExecutionError,
                    QueueError,
                ) as exc:
                    unresolved.append(dict(candidate))
                    unresolved_errors[str(candidate["job_id"])] = _job_error(exc)
                    continue
                results.append(dict(reconciled))
                if outcome == "rendering":
                    unresolved.append(reconciled)
                elif outcome == "failed":
                    reconciliation_failed = True
                    _write_status(
                        root,
                        state="failed",
                        active_job=None,
                        last_job=str(candidate["job_id"]),
                        detail="render_failed",
                        project_name_value=(
                            reconciled.get("result", {}).get("project_name")
                            if isinstance(reconciled.get("result"), Mapping)
                            else None
                        ),
                        timeline_name=(
                            reconciled.get("result", {}).get("timeline_name")
                            if isinstance(reconciled.get("result"), Mapping)
                            else None
                        ),
                        error=(
                            reconciled.get("error")
                            if isinstance(reconciled.get("error"), Mapping)
                            else None
                        ),
                    )
                else:
                    _write_status(
                        root,
                        state="succeeded",
                        active_job=None,
                        last_job=str(candidate["job_id"]),
                        detail="render_succeeded",
                        project_name_value=(
                            reconciled.get("result", {}).get("project_name")
                            if isinstance(reconciled.get("result"), Mapping)
                            else None
                        ),
                        timeline_name=(
                            reconciled.get("result", {}).get("timeline_name")
                            if isinstance(reconciled.get("result"), Mapping)
                            else None
                        ),
                    )
            if unresolved:
                active = unresolved[0]
                active_result = active.get("result")
                active_error = unresolved_errors.get(str(active["job_id"]))
                _write_status(
                    root,
                    state="rendering",
                    active_job=str(active["job_id"]),
                    last_job=str(active["job_id"]),
                    detail=(
                        "render_reconciliation_blocked"
                        if active_error is not None
                        else (
                            "render_in_progress"
                            if isinstance(active_result, Mapping)
                            and active_result.get("render_status")
                            in _RENDER_ACTIVE_STATUSES
                            else "render_completion_unverified"
                        )
                    ),
                    project_name_value=(
                        active_result.get("project_name")
                        if isinstance(active_result, Mapping)
                        else None
                    ),
                    timeline_name=(
                        active_result.get("timeline_name")
                        if isinstance(active_result, Mapping)
                        else None
                    ),
                    error=active_error,
                )
                return results or [dict(item) for item in unresolved]
            if reconciliation_failed:
                return results

        # A previous process cannot still own this lock.  Preserve interrupted
        # jobs as failed diagnostics; do not silently rerun them.
        for path in files:
            candidate = _read_job(path)
            _validate_job_fingerprint(candidate, root)
            if candidate["state"] == "running":
                error = {
                    "type": "InterruptedRunner",
                    "message": (
                        "job was left running by a previous runner; explicit "
                        "reenqueue with changed options is required"
                    ),
                }
                failed = _transition_job(
                    path, candidate, "failed", result=None, error=error
                )
                results.append(dict(failed))

        for path in files:
            job = _read_job(path)
            _validate_job_fingerprint(job, root)
            if job["state"] != "queued":
                continue
            lock.heartbeat(stage=f"resolve-{job['action']}")
            try:
                if connection is None:
                    connection = connect_resolve(
                        resolve=resolve,
                        app=app,
                        allow_studio_external=allow_studio_external,
                        studio_external_adapter=studio_external_adapter,
                    )
                # Connection/render availability are runner-level conditions,
                # not failures in the queued job.  Establish them before
                # claiming so Free jobs remain queued/awaiting when invoked
                # outside Resolve or while another render is active.
                manager = get_project_manager(connection.resolve)
                current = get_current_project(manager)
                if current is not None:
                    require_render_idle(current)
            except (ResolveUnavailableError, ResolveBusyError, ResolveApiError) as exc:
                detail = (
                    "awaiting_in_app_runner"
                    if isinstance(exc, ResolveUnavailableError)
                    else "blocked_by_resolve_state"
                )
                _write_status(
                    root,
                    state="queued",
                    active_job=None,
                    last_job=job["job_id"],
                    detail=detail,
                    error=_job_error(exc),
                )
                break

            running = _transition_job(path, job, "running")
            _write_status(
                root,
                state="running",
                active_job=job["job_id"],
                last_job=job["job_id"],
                detail=f"executing_{job['action']}",
            )
            try:
                plan_path_value = _canonical(running["plan_path"])
                if not is_path_within(plan_path_value, root):
                    raise UnsafeWriteError(
                        f"queued Resolve plan escapes project: {plan_path_value}"
                    )
                loaded_path, plan = _load_plan(
                    plan_path_value,
                    expected_sha256=str(running["plan_sha256"]),
                )
                _validate_queued_inputs(running, plan, loaded_path, root)

                result = _execute_job(
                    connection, root, running, loaded_path, plan, lock
                )
                if (
                    job["action"] == "render"
                    and isinstance(result, Mapping)
                    and result.get("render_status") == "started"
                ):
                    rendering = _transition_job(
                        path, running, "rendering", result=result
                    )
                    results.append(dict(rendering))
                    _write_status(
                        root,
                        state="rendering",
                        active_job=job["job_id"],
                        last_job=job["job_id"],
                        detail="render_in_progress",
                        project_name_value=(
                            result.get("project_name")
                            if isinstance(result, Mapping)
                            else None
                        ),
                        timeline_name=(
                            result.get("timeline_name")
                            if isinstance(result, Mapping)
                            else None
                        ),
                    )
                    break
                completed = _transition_job(
                    path, running, "succeeded", result=result
                )
                results.append(dict(completed))
                _write_status(
                    root,
                    state="succeeded",
                    active_job=None,
                    last_job=job["job_id"],
                    detail=f"{job['action']}_succeeded",
                    project_name_value=(
                        result.get("project_name")
                        if isinstance(result, Mapping)
                        else None
                    ),
                    timeline_name=(
                        result.get("timeline_name")
                        if isinstance(result, Mapping)
                        else None
                    ),
                )
            except Exception as exc:
                failure = _job_error(exc)
                failed = _transition_job(
                    path, running, "failed", result=None, error=failure
                )
                results.append(dict(failed))
                _write_status(
                    root,
                    state="failed",
                    active_job=None,
                    last_job=job["job_id"],
                    detail=f"{job['action']}_failed",
                    error=failure,
                )
                # Safety/API failures can describe global Resolve state; do not
                # risk later queue entries in the same session.
                if isinstance(
                    exc,
                    (
                        ResolveSafetyError,
                        ResolveApiError,
                        ResolveUnavailableError,
                    ),
                ):
                    break
        lock.heartbeat(stage="resolve-queue-complete")
    return results


def _default_runner_install_directory() -> Path:
    return resolve_runner_install_directory().resolve(strict=False)


def install_runner(
    destination: os.PathLike[str] | str | None = None,
) -> Path:
    """Install the in-app runner into Resolve's Workspace scripts directory."""

    source = SOURCE_RUNNER_SCRIPT
    if not source.is_file():
        raise ResolveRunnerError(f"runner source script is missing: {source}")
    if destination is None:
        target = _default_runner_install_directory() / RUNNER_SCRIPT_FILENAME
    else:
        requested = _canonical(destination)
        target = requested if requested.suffix.lower() == ".py" else requested / RUNNER_SCRIPT_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


def console_loader_command(
    project_root: os.PathLike[str] | str,
    *,
    job_id: str | None = None,
    runner_path: os.PathLike[str] | str | None = None,
) -> str:
    """Return a one-line Resolve Console loader for an exact root/job."""

    root = os.fspath(_canonical(project_root))
    script = os.fspath(_canonical(runner_path or SOURCE_RUNNER_SCRIPT))
    globals_value = {
        "RUN_RABBITHOLE_RESOLVE_RUNNER": True,
        "PROJECT_ROOT": root,
        "JOB_ID": job_id,
    }
    literal = repr(globals_value)
    # Inject the host objects without requiring either name to exist.
    return (
        "import runpy; "
        f"_rh={literal}; "
        "_rh['resolve']=globals().get('resolve'); "
        "_rh['app']=globals().get('app'); "
        f"runpy.run_path({script!r}, init_globals=_rh)"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rabbithole-resolve-runner",
        description="Run durable RabbitHole jobs through an injected Resolve API",
    )
    parser.add_argument("--project-root")
    parser.add_argument("--queue-path")
    parser.add_argument("--job-id")
    parser.add_argument(
        "--studio-external",
        action="store_true",
        help="explicitly allow the Studio-only external scripting adapter",
    )
    args = parser.parse_args(argv)
    results = run_pending_jobs(
        project_root=args.project_root,
        queue_path=args.queue_path,
        job_id=args.job_id,
        allow_studio_external=args.studio_external,
    )
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0 if all(result.get("state") == "succeeded" for result in results) else 1


__all__ = [
    "BuildResult",
    "ImmutableTimelineError",
    "JOB_STATES",
    "QueueError",
    "RenderResult",
    "ResolveConnection",
    "ResolveExecutionError",
    "ResolveRunnerError",
    "ResolveUnavailableError",
    "build_hash_for_plan",
    "connect_resolve",
    "console_loader_command",
    "deterministic_project_name",
    "enqueue_job",
    "execute_build",
    "execute_render",
    "get_resolve",
    "install_runner",
    "main",
    "queue_directory",
    "read_runner_pointer",
    "read_status",
    "run_pending_jobs",
    "runner_pointer_path",
    "status_path",
    "timeline_import_path",
    "timeline_name_for_plan",
]


if __name__ == "__main__":
    raise SystemExit(main())

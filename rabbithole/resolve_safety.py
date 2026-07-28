"""Fail-closed safety primitives for DaVinci Resolve automation.

The Resolve runner deliberately has a very small mutation surface.  This
module owns the two pieces that are easy to get subtly wrong:

* a project-scoped, process-identity lock (a PID alone is not an identity);
* canonical write-root and active-render checks.

Nothing in this module launches Resolve, changes the current project, stops a
render, or removes Resolve objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4


LOCK_SCHEMA_VERSION = 1
DEFAULT_LOCK_RELATIVE_PATH = Path("resolve") / ".resolve-runner.lock"
_STAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")

# Kept in one auditable list.  The runner contains no call sites for these.
FORBIDDEN_RESOLVE_METHODS = frozenset(
    {
        "StopRendering",
        "Quit",
        "DeleteAllRenderJobs",
        "DeleteRenderJob",
        "DeleteRenderJobs",
        "DeleteProject",
        "DeleteTimelines",
        "DeleteTimeline",
        "DeleteClips",
        "DeleteMediaPoolItems",
        "SetCurrentDatabase",
        "LoadProject",
        "CloseProject",
    }
)


class ResolveSafetyError(RuntimeError):
    """Base class for an operation refused by the Resolve safety layer."""


class ResolveLockError(ResolveSafetyError):
    """A project lock cannot safely be acquired, updated, or released."""


class ResolveBusyError(ResolveSafetyError):
    """Resolve is rendering, or its render state cannot be established."""


class UnsafeWriteError(ResolveSafetyError):
    """A requested output is outside the operation's declared write roots."""


class ResolveApiError(ResolveSafetyError):
    """The connected object does not expose a safely usable Resolve API."""


def utc_now() -> str:
    """Return a stable, UTC ISO-8601 timestamp."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _canonical(path: os.PathLike[str] | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.fspath(left)) == os.path.normcase(os.fspath(right))


def is_path_within(path: os.PathLike[str] | str, root: os.PathLike[str] | str) -> bool:
    """Return whether *path* resolves to *root* or one of its descendants."""

    candidate = _canonical(path)
    boundary = _canonical(root)
    try:
        candidate.relative_to(boundary)
    except ValueError:
        return False
    return True


def normalize_write_roots(
    project_root: os.PathLike[str] | str,
    write_roots: Iterable[os.PathLike[str] | str] | None,
    *,
    allow_external_write_roots: bool = False,
) -> tuple[Path, ...]:
    """Validate and canonicalize an operation's declared write roots.

    Normal runner-owned writes are episode-local.  A handoff may opt into a
    separately validated, exact external destination.  Even in that mode a
    filesystem root and the user's home directory are never valid write roots.
    """

    project = _canonical(project_root)
    requested = tuple(write_roots or (project / "resolve",))
    if not requested:
        raise UnsafeWriteError("at least one write root must be declared")

    roots: list[Path] = []
    for value in requested:
        root = _canonical(value)
        if not allow_external_write_roots and not is_path_within(root, project):
            raise UnsafeWriteError(
                f"write root escapes project: {root} (project: {project})"
            )
        if allow_external_write_roots and not is_path_within(root, project):
            anchor = Path(root.anchor).resolve(strict=False)
            home = Path.home().resolve(strict=False)
            if _same_path(root, anchor):
                raise UnsafeWriteError(
                    f"external write root cannot be a filesystem root: {root}"
                )
            if _same_path(root, home):
                raise UnsafeWriteError(
                    f"external write root cannot be the user home directory: {root}"
                )
            if not root.name:
                raise UnsafeWriteError(
                    f"external write root must have a non-empty basename: {root}"
                )
        if not any(_same_path(root, existing) for existing in roots):
            roots.append(root)
    return tuple(roots)


def require_write_path(
    path: os.PathLike[str] | str,
    write_roots: Iterable[os.PathLike[str] | str],
) -> Path:
    """Return a canonical path or refuse it when no declared root contains it."""

    candidate = _canonical(path)
    roots = tuple(_canonical(root) for root in write_roots)
    if not roots or not any(is_path_within(candidate, root) for root in roots):
        rendered = ", ".join(os.fspath(root) for root in roots) or "<none>"
        raise UnsafeWriteError(
            f"write path escapes declared roots: {candidate}; allowed: {rendered}"
        )
    return candidate


@dataclass(frozen=True)
class ProcessProbe:
    """Observed process identity.

    ``exists=None`` means the operating system would not let us establish
    liveness.  Callers must treat that as live/unknown rather than stale.
    """

    exists: bool | None
    process_start: float | None = None
    detail: str | None = None


def _psutil_probe(pid: int) -> ProcessProbe | None:
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return None

    try:
        process = psutil.Process(pid)
        return ProcessProbe(True, float(process.create_time()))
    except psutil.NoSuchProcess:
        return ProcessProbe(False)
    except (psutil.AccessDenied, OSError) as exc:
        return ProcessProbe(None, detail=str(exc))


def _windows_process_start(pid: int) -> ProcessProbe:
    # ctypes is intentionally imported only on Windows.
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_INVALID_PARAMETER = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == ERROR_INVALID_PARAMETER:
            return ProcessProbe(False)
        return ProcessProbe(None, detail=f"OpenProcess failed with Windows error {error}")

    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            error = ctypes.get_last_error()
            return ProcessProbe(
                None, detail=f"GetProcessTimes failed with Windows error {error}"
            )
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        # Windows FILETIME is 100 ns since 1601-01-01.
        return ProcessProbe(True, ticks / 10_000_000 - 11_644_473_600)
    finally:
        kernel32.CloseHandle(handle)


def probe_process(pid: int) -> ProcessProbe:
    """Probe a PID and, where possible, its operating-system start time."""

    if not isinstance(pid, int) or pid <= 0:
        return ProcessProbe(False, detail="invalid PID")

    result = _psutil_probe(pid)
    if result is not None:
        return result

    if sys.platform == "win32":
        return _windows_process_start(pid)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ProcessProbe(False)
    except PermissionError as exc:
        return ProcessProbe(None, detail=str(exc))
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return ProcessProbe(False)
        return ProcessProbe(None, detail=str(exc))

    # Liveness is known, but without psutil the start time is not.  That is
    # enough to refuse a lock takeover, never enough to declare one stale.
    return ProcessProbe(True, None, "process start time unavailable")


def current_process_start() -> float:
    """Return this process's start time, refusing an unverifiable identity."""

    observed = probe_process(os.getpid())
    if observed.exists is True and observed.process_start is not None:
        return observed.process_start
    # A module-local start value still gives the owner a release token on
    # platforms without a start-time API.  Other processes will fail closed
    # rather than declaring the lock stale.
    return _MODULE_PROCESS_START


_MODULE_PROCESS_START = time.time()


def _coerce_probe(value: Any) -> ProcessProbe:
    if isinstance(value, ProcessProbe):
        return value
    if value is None:
        # An injected probe commonly uses None for "PID not found".
        return ProcessProbe(False)
    if isinstance(value, (int, float)):
        return ProcessProbe(True, float(value))
    if isinstance(value, tuple):
        if len(value) == 2:
            return ProcessProbe(value[0], value[1])
        if len(value) == 3:
            return ProcessProbe(value[0], value[1], value[2])
    if isinstance(value, Mapping):
        return ProcessProbe(
            value.get("exists"),
            value.get("process_start"),
            value.get("detail"),
        )
    raise TypeError(f"unsupported process probe result: {value!r}")


@dataclass(frozen=True)
class LockMetadata:
    pid: int
    process_start: float
    stage: str
    write_roots: tuple[str, ...]
    project_root: str
    created_at: str
    heartbeat_at: str
    hostname: str
    token: str
    schema_version: int = LOCK_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pid": self.pid,
            "process_start": self.process_start,
            "stage": self.stage,
            "write_roots": list(self.write_roots),
            "project_root": self.project_root,
            "created_at": self.created_at,
            "heartbeat_at": self.heartbeat_at,
            "hostname": self.hostname,
            "token": self.token,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LockMetadata":
        try:
            schema = int(value["schema_version"])
            pid = int(value["pid"])
            process_start = float(value["process_start"])
            stage = str(value["stage"])
            roots_value = value["write_roots"]
            if not isinstance(roots_value, list) or not roots_value:
                raise ValueError("write_roots must be a non-empty list")
            roots = tuple(str(item) for item in roots_value)
            project_root = str(value["project_root"])
            created_at = str(value["created_at"])
            heartbeat_at = str(value["heartbeat_at"])
            hostname = str(value["hostname"])
            token = str(value["token"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ResolveLockError(f"invalid lock metadata: {exc}") from exc

        if schema != LOCK_SCHEMA_VERSION:
            raise ResolveLockError(f"unsupported lock schema version: {schema}")
        if pid <= 0 or process_start <= 0:
            raise ResolveLockError("invalid process identity in lock")
        if not _STAGE_RE.fullmatch(stage):
            raise ResolveLockError(f"invalid lock stage: {stage!r}")
        if not token:
            raise ResolveLockError("lock token is empty")
        return cls(
            pid=pid,
            process_start=process_start,
            stage=stage,
            write_roots=roots,
            project_root=project_root,
            created_at=created_at,
            heartbeat_at=heartbeat_at,
            hostname=hostname,
            token=token,
            schema_version=schema,
        )


class ProjectLock:
    """An atomic, project-scoped lock carrying a complete process identity."""

    def __init__(
        self,
        project_root: os.PathLike[str] | str,
        *,
        stage: str,
        write_roots: Iterable[os.PathLike[str] | str] | None = None,
        lock_path: os.PathLike[str] | str | None = None,
        process_probe: Callable[[int], Any] | None = None,
        allow_external_write_roots: bool = False,
    ) -> None:
        if not _STAGE_RE.fullmatch(stage):
            raise ValueError(
                "stage must start with an alphanumeric and contain only "
                "alphanumerics, dot, underscore, colon, or hyphen"
            )
        self.project_root = _canonical(project_root)
        self.write_roots = normalize_write_roots(
            project_root,
            write_roots,
            allow_external_write_roots=allow_external_write_roots,
        )
        candidate = (
            _canonical(lock_path)
            if lock_path is not None
            else self.project_root / DEFAULT_LOCK_RELATIVE_PATH
        )
        if not is_path_within(candidate, self.project_root):
            raise UnsafeWriteError(f"lock path escapes project: {candidate}")
        self.path = candidate
        self.stage = stage
        self._probe = process_probe or probe_process
        self._metadata: LockMetadata | None = None

    @property
    def metadata(self) -> LockMetadata | None:
        return self._metadata

    def _new_metadata(self) -> LockMetadata:
        now = utc_now()
        return LockMetadata(
            pid=os.getpid(),
            process_start=current_process_start(),
            stage=self.stage,
            write_roots=tuple(os.fspath(root) for root in self.write_roots),
            project_root=os.fspath(self.project_root),
            created_at=now,
            heartbeat_at=now,
            hostname=socket.gethostname(),
            token=uuid4().hex,
        )

    def _read_existing(self) -> tuple[bytes, LockMetadata]:
        try:
            payload = self.path.read_bytes()
        except FileNotFoundError:
            raise
        try:
            decoded = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            # Malformed locks are not assumed stale.
            raise ResolveLockError(
                f"lock exists but cannot be validated: {self.path}: {exc}"
            ) from exc
        if not isinstance(decoded, Mapping):
            raise ResolveLockError(f"lock is not a JSON object: {self.path}")
        return payload, LockMetadata.from_dict(decoded)

    def _existing_is_stale(self, owner: LockMetadata) -> bool:
        try:
            observed = _coerce_probe(self._probe(owner.pid))
        except ProcessLookupError:
            observed = ProcessProbe(False)
        except Exception as exc:
            raise ResolveLockError(
                f"cannot establish lock owner liveness for PID {owner.pid}: {exc}"
            ) from exc

        if observed.exists is False:
            return True
        if observed.exists is None:
            raise ResolveLockError(
                f"cannot establish lock owner liveness for PID {owner.pid}: "
                f"{observed.detail or 'unknown process state'}"
            )
        if observed.process_start is None:
            raise ResolveLockError(
                f"PID {owner.pid} is live but its process start time is unavailable"
            )
        # Windows and psutil timestamps are sub-second floats.  JSON preserves
        # enough precision that only a tiny representation tolerance is needed;
        # a whole-second tolerance could misidentify rapid PID reuse.
        return abs(observed.process_start - owner.process_start) > 0.001

    def _remove_stale_if_unchanged(self, payload: bytes) -> bool:
        try:
            current = self.path.read_bytes()
        except FileNotFoundError:
            return True
        if current != payload:
            return False
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        return True

    def acquire(self) -> "ProjectLock":
        if self._metadata is not None:
            raise ResolveLockError("lock instance is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        metadata = self._new_metadata()
        encoded = (
            json.dumps(metadata.to_dict(), indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

        for _ in range(4):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                payload, owner = self._read_existing()
                if not self._existing_is_stale(owner):
                    raise ResolveLockError(
                        "project is locked by "
                        f"PID {owner.pid} (started {owner.process_start:.6f}), "
                        f"stage {owner.stage!r}, lock {self.path}"
                    )
                if not self._remove_stale_if_unchanged(payload):
                    continue
                continue

            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                raise
            self._metadata = metadata
            return self

        raise ResolveLockError(f"lock changed repeatedly while acquiring: {self.path}")

    def _replace_owned_metadata(self, metadata: LockMetadata) -> None:
        if self._metadata is None:
            raise ResolveLockError("lock is not acquired")
        try:
            _, on_disk = self._read_existing()
        except FileNotFoundError as exc:
            raise ResolveLockError("owned lock disappeared") from exc
        if on_disk.token != self._metadata.token:
            raise ResolveLockError("owned lock was replaced by another process")

        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                delete=False,
            ) as stream:
                json.dump(metadata.to_dict(), stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
                temporary = Path(stream.name)
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        self._metadata = metadata

    def heartbeat(self, *, stage: str | None = None) -> LockMetadata:
        """Refresh the heartbeat and optionally move to a named sub-stage."""

        if self._metadata is None:
            raise ResolveLockError("lock is not acquired")
        next_stage = stage or self._metadata.stage
        if not _STAGE_RE.fullmatch(next_stage):
            raise ValueError(f"invalid lock stage: {next_stage!r}")
        current = self._metadata
        refreshed = LockMetadata(
            pid=current.pid,
            process_start=current.process_start,
            stage=next_stage,
            write_roots=current.write_roots,
            project_root=current.project_root,
            created_at=current.created_at,
            heartbeat_at=utc_now(),
            hostname=current.hostname,
            token=current.token,
            schema_version=current.schema_version,
        )
        self._replace_owned_metadata(refreshed)
        return refreshed

    def assert_write_path(self, path: os.PathLike[str] | str) -> Path:
        return require_write_path(path, self.write_roots)

    def release(self) -> None:
        if self._metadata is None:
            return
        owned = self._metadata
        try:
            _, on_disk = self._read_existing()
        except FileNotFoundError:
            self._metadata = None
            return
        if on_disk.token != owned.token:
            self._metadata = None
            raise ResolveLockError("refusing to remove a lock owned by another process")
        self.path.unlink()
        self._metadata = None

    def __enter__(self) -> "ProjectLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


# Backwards-friendly, descriptive alias used by callers and tests.
ResolveProjectLock = ProjectLock


def get_project_manager(resolve: Any) -> Any:
    """Get Resolve's project manager without loading or switching a project."""

    method = getattr(resolve, "GetProjectManager", None)
    if not callable(method):
        raise ResolveApiError("Resolve object does not expose GetProjectManager()")
    try:
        manager = method()
    except Exception as exc:
        raise ResolveApiError(f"GetProjectManager() failed: {exc}") from exc
    if manager is None:
        raise ResolveApiError("GetProjectManager() returned no project manager")
    return manager


def get_current_project(project_manager: Any) -> Any | None:
    """Return the current project; ``None`` is a valid no-project state."""

    method = getattr(project_manager, "GetCurrentProject", None)
    if not callable(method):
        raise ResolveApiError("project manager does not expose GetCurrentProject()")
    try:
        return method()
    except Exception as exc:
        raise ResolveApiError(f"GetCurrentProject() failed: {exc}") from exc


def project_name(project: Any) -> str:
    method = getattr(project, "GetName", None)
    if not callable(method):
        raise ResolveApiError("current project does not expose GetName()")
    try:
        value = method()
    except Exception as exc:
        raise ResolveApiError(f"project GetName() failed: {exc}") from exc
    if not isinstance(value, str) or not value.strip():
        raise ResolveApiError("current project returned an empty name")
    return value


def require_render_idle(project: Any) -> None:
    """Fail closed unless Resolve explicitly reports an idle render state."""

    method = getattr(project, "IsRenderingInProgress", None)
    if not callable(method):
        raise ResolveBusyError(
            "cannot establish render state: IsRenderingInProgress() is unavailable"
        )
    try:
        state = method()
    except Exception as exc:
        raise ResolveBusyError(
            f"cannot establish render state: IsRenderingInProgress() failed: {exc}"
        ) from exc
    if not isinstance(state, bool):
        raise ResolveBusyError(
            "cannot establish render state: "
            f"IsRenderingInProgress() returned {state!r}"
        )
    if state:
        raise ResolveBusyError("Resolve is already rendering; operation refused")


def assert_safe_method_name(name: str) -> None:
    """Refuse a dangerous Resolve method name in optional adapters/hooks."""

    if name in FORBIDDEN_RESOLVE_METHODS:
        raise ResolveSafetyError(f"Resolve method is forbidden by policy: {name}")


__all__ = [
    "DEFAULT_LOCK_RELATIVE_PATH",
    "FORBIDDEN_RESOLVE_METHODS",
    "LockMetadata",
    "ProcessProbe",
    "ProjectLock",
    "ResolveApiError",
    "ResolveBusyError",
    "ResolveLockError",
    "ResolveProjectLock",
    "ResolveSafetyError",
    "UnsafeWriteError",
    "assert_safe_method_name",
    "current_process_start",
    "get_current_project",
    "get_project_manager",
    "is_path_within",
    "normalize_write_roots",
    "probe_process",
    "project_name",
    "require_render_idle",
    "require_write_path",
    "utc_now",
]

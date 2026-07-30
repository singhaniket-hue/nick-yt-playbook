"""Source-inclusive, checksum-verifiable DaVinci Resolve editor handoffs."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import tempfile
from typing import Any
import unicodedata
from uuid import uuid4
import zipfile

from .resolve_safety import (
    ProjectLock,
    ResolveApiError,
    UnsafeWriteError,
    get_current_project,
    get_project_manager,
    is_path_within,
    project_name,
    require_render_idle,
    require_write_path,
    utc_now,
)


HANDOFF_SCHEMA_VERSION = "rabbithole-resolve-handoff.v1"
CHECKSUM_FILENAME = "checksums.sha256"
MANIFEST_FILENAME = "manifest.json"
README_FILENAME = "README.txt"
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_WINDOWS_FORBIDDEN_RE = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)
_ZIP_STORED_SUFFIXES = frozenset(
    {
        ".3gp",
        ".7z",
        ".aac",
        ".avif",
        ".bz2",
        ".dra",
        ".drp",
        ".flac",
        ".gif",
        ".gz",
        ".heic",
        ".jpeg",
        ".jpg",
        ".m4a",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".ogg",
        ".png",
        ".rar",
        ".webm",
        ".webp",
        ".xz",
        ".zip",
    }
)
_COPY_BUFFER_BYTES = 1024 * 1024


class ResolveHandoffError(RuntimeError):
    """A handoff could not be created or validated without guessing."""


class HandoffValidationError(ResolveHandoffError):
    """A handoff/restore candidate is incomplete, corrupt, or hook-rejected."""


@dataclass(frozen=True)
class HandoffValidationContext:
    package_root: Path
    manifest: Mapping[str, Any]
    restored_project: Any | None = None


@dataclass(frozen=True)
class HandoffResult:
    action: str
    project_name: str
    timeline_name: str | None
    package_directory: str
    zip_path: str
    zip_sha256: str
    manifest_path: str
    checksums_path: str
    dra_path: str
    drp_path: str
    file_count: int


def _canonical(path: os.PathLike[str] | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _safe_name(value: str, *, fallback: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", value).strip("._-")
    return (cleaned or fallback)[:96]


def _validate_portable_relative(value: str, *, label: str) -> str:
    """Validate a relative name on Windows and case-insensitive macOS volumes."""

    if not value or value != value.strip():
        raise HandoffValidationError(
            f"{label} must be a non-empty relative path without edge whitespace"
        )
    if "\\" in value:
        raise HandoffValidationError(
            f"{label} uses a Windows-only separator; use '/': {value!r}"
        )
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if windows.drive or windows.is_absolute() or posix.is_absolute():
        raise HandoffValidationError(
            f"{label} must not be absolute or drive-qualified: {value!r}"
        )
    if value.startswith("~"):
        raise HandoffValidationError(
            f"{label} must not be home-relative: {value!r}"
        )
    raw_parts = value.split("/")
    if (
        any(part in {"", ".", ".."} for part in raw_parts)
        or posix.as_posix() != value
    ):
        raise HandoffValidationError(
            f"{label} contains an escaping or ambiguous component: {value!r}"
        )
    if len(value) > 240:
        raise HandoffValidationError(
            f"{label} exceeds the portable 240-character path limit: {value!r}"
        )
    for part in posix.parts:
        if unicodedata.normalize("NFC", part) != part:
            raise HandoffValidationError(
                f"{label} must use NFC-normalized names: {value!r}"
            )
        if len(part.encode("utf-8")) > 255:
            raise HandoffValidationError(
                f"{label} has a component longer than 255 bytes: {value!r}"
            )
        if _WINDOWS_FORBIDDEN_RE.search(part) or part.endswith((" ", ".")):
            raise HandoffValidationError(
                f"{label} is not valid on Windows: {value!r}"
            )
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise HandoffValidationError(
                f"{label} uses a Windows-reserved name: {value!r}"
            )
    return posix.as_posix()


def _portable_key(value: str) -> str:
    return "/".join(
        unicodedata.normalize("NFC", part).casefold()
        for part in PurePosixPath(value).parts
    )


def _validate_portable_tree(root: Path) -> None:
    """Reject links, non-portable names, and cross-platform name collisions."""

    portable_paths: dict[str, str] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if path.is_symlink() or (
            callable(getattr(path, "is_junction", None)) and path.is_junction()
        ):
            raise HandoffValidationError(
                f"handoff package cannot contain links: {path}"
            )
        relative = _validate_portable_relative(
            path.relative_to(root).as_posix(),
            label="handoff package path",
        )
        collision_key = _portable_key(relative)
        previous = portable_paths.get(collision_key)
        if previous is not None and previous != relative:
            raise HandoffValidationError(
                "handoff paths collide on a case-insensitive or "
                f"Unicode-normalizing filesystem: {previous!r} and {relative!r}"
            )
        portable_paths[collision_key] = relative


def validate_handoff_output_root(
    destination: os.PathLike[str] | str,
    *,
    project_root: os.PathLike[str] | str,
) -> Path:
    """Validate an exact external handoff root.

    Episode-local roots are always accepted.  External roots must have a real
    basename and may not be a filesystem root or the user's home directory.
    """

    root = _canonical(destination)
    project = _canonical(project_root)
    if is_path_within(root, project):
        return root
    anchor = Path(root.anchor).resolve(strict=False)
    home = Path.home().resolve(strict=False)
    if os.path.normcase(os.fspath(root)) == os.path.normcase(os.fspath(anchor)):
        raise UnsafeWriteError(f"handoff destination cannot be a filesystem root: {root}")
    if os.path.normcase(os.fspath(root)) == os.path.normcase(os.fspath(home)):
        raise UnsafeWriteError(
            f"handoff destination cannot be the user home directory: {root}"
        )
    if not root.name:
        raise UnsafeWriteError(
            f"handoff destination must have a non-empty basename: {root}"
        )
    return root


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_text(path: Path, text: str) -> None:
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
            stream.write(text)
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


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _call_required(owner: Any, name: str, *args: Any) -> Any:
    method = getattr(owner, name, None)
    if not callable(method):
        raise ResolveApiError(f"Resolve API object does not expose {name}()")
    try:
        return method(*args)
    except Exception as exc:
        raise ResolveHandoffError(f"{name}() failed: {exc}") from exc


def _normalize_sources(
    values: (
        Mapping[str, os.PathLike[str] | str]
        | Iterable[os.PathLike[str] | str]
        | os.PathLike[str]
        | str
        | None
    ),
) -> list[tuple[str, Path]]:
    if values is None:
        return []
    if isinstance(values, Mapping):
        return [
            (_safe_name(str(name), fallback="item"), _canonical(path))
            for name, path in values.items()
        ]
    if isinstance(values, (str, os.PathLike)):
        path = _canonical(values)
        return [(path.name, path)]
    result: list[tuple[str, Path]] = []
    for value in values:
        path = _canonical(value)
        result.append((path.name, path))
    return result


def _copy_source(source: Path, target: Path) -> int:
    if not source.exists():
        raise ResolveHandoffError(f"handoff source does not exist: {source}")
    if source.is_symlink():
        raise ResolveHandoffError(f"handoff source cannot be a symlink: {source}")
    if target.exists():
        raise ResolveHandoffError(f"handoff destination collision: {target}")
    if source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return 1
    if not source.is_dir():
        raise ResolveHandoffError(f"unsupported handoff source: {source}")
    count = 0
    target.mkdir(parents=True)
    for child in sorted(source.rglob("*")):
        if child.is_symlink():
            raise ResolveHandoffError(f"handoff source cannot contain symlink: {child}")
        relative = child.relative_to(source)
        destination = target / relative
        if child.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif child.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, destination)
            count += 1
    return count


def _remove_owned_path(path: Path) -> None:
    """Remove one exact runner-owned artifact, tolerating only absence."""

    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        pass


def _copy_category(
    staging: Path,
    category: str,
    sources: (
        Mapping[str, os.PathLike[str] | str]
        | Iterable[os.PathLike[str] | str]
        | os.PathLike[str]
        | str
        | None
    ),
) -> list[str]:
    target_root = staging / category
    target_root.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name, source in _normalize_sources(sources):
        target = target_root / _safe_name(name, fallback=source.name or "item")
        _copy_source(source, target)
        copied.append(target.relative_to(staging).as_posix())
    if not copied:
        _atomic_text(
            target_root / "README.txt",
            f"No {category} were supplied for this handoff.\n",
        )
        copied.append((target_root / "README.txt").relative_to(staging).as_posix())
    return copied


def _with_default_sources(
    defaults: Iterable[Path],
    explicit: (
        Mapping[str, os.PathLike[str] | str]
        | Iterable[os.PathLike[str] | str]
        | os.PathLike[str]
        | str
        | None
    ),
) -> dict[str, Path]:
    combined: dict[str, Path] = {}
    for source in defaults:
        if source.exists():
            name = source.name
            position = 2
            while name in combined:
                name = f"{source.stem}-{position}{source.suffix}"
                position += 1
            combined[name] = source
    for requested_name, source in _normalize_sources(explicit):
        name = requested_name
        position = 2
        while name in combined:
            path = Path(name)
            name = f"{path.stem}-{position}{path.suffix}"
            position += 1
        combined[name] = source
    return combined


def _plan_paths(plan: Mapping[str, Any], project_root: Path) -> list[tuple[str, Path]]:
    result: list[tuple[str, Path]] = []
    output_paths = plan.get("output_paths")
    if not isinstance(output_paths, Mapping):
        return result
    for key in ("plan", "fcpxml", "subtitles"):
        raw = output_paths.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        source = Path(raw).expanduser()
        if not source.is_absolute():
            source = project_root / source
        source = source.resolve(strict=False)
        if source.is_file() and is_path_within(source, project_root):
            result.append((source.name, source))
    return result


def _copy_project_files(
    staging: Path,
    project_root: Path,
    plan: Mapping[str, Any],
    include_files: (
        Mapping[str, os.PathLike[str] | str]
        | Iterable[os.PathLike[str] | str]
        | os.PathLike[str]
        | str
        | None
    ),
) -> list[str]:
    sources = _plan_paths(plan, project_root)
    sources.extend(_normalize_sources(include_files))
    canonical_defaults = (
        project_root / "narration" / "timing.json",
        project_root / "edit" / "edl.json",
        project_root / "provenance.json",
        project_root / "research" / "source-audio.json",
        project_root / "research" / "highlights.json",
        project_root / "chapters.txt",
        project_root / "credits.txt",
        project_root / "qc_report.txt",
        project_root / "qc-report.txt",
        project_root / "qc_report.json",
        project_root / "qc-report.json",
        project_root / "resolve" / "qc_report.txt",
        project_root / "resolve" / "qc-report.txt",
        project_root / "resolve" / "qc_report.json",
        project_root / "resolve" / "qc-report.json",
    )
    sources.extend((path.name, path) for path in canonical_defaults if path.is_file())

    destination_root = staging / "project-files"
    destination_root.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    seen_targets: set[str] = set()
    for requested_name, source in sources:
        if not source.exists():
            raise ResolveHandoffError(f"included project file is missing: {source}")
        name = _safe_name(requested_name, fallback=source.name or "item")
        # Preserve project-relative structure to avoid timing.json collisions.
        if is_path_within(source, project_root):
            relative = source.relative_to(project_root)
            target = destination_root / "project" / relative
        else:
            target = destination_root / "extra" / name
        key = os.path.normcase(os.fspath(target.resolve(strict=False)))
        if key in seen_targets:
            continue
        seen_targets.add(key)
        _copy_source(source, target)
        copied.append(target.relative_to(staging).as_posix())
    if not copied:
        _atomic_text(
            destination_root / "README.txt",
            "No additional project-side files were available.\n",
        )
        copied.append(
            (destination_root / "README.txt").relative_to(staging).as_posix()
        )
    return copied


def _readme(
    project_name_value: str,
    timeline_name: str | None,
    *,
    include_proxy_media: bool,
) -> str:
    timeline = timeline_name or "<recorded in manifest>"
    return f"""RabbitHole DaVinci Resolve editor handoff

Project recorded at export: {project_name_value}
Generated timeline: {timeline}

Primary restore:
1. Verify this directory with checksums.sha256 before opening it.
2. In Resolve's Project Manager, restore the source-inclusive project.dra.
3. Use project.drp only as a lightweight backup/inspection export.
4. If Resolve requests a relink, select the media inside the restored archive.
5. Install only fonts and presets whose notes in licenses/ permit that use.
6. Duplicate {timeline} to EDITORIAL_v1 before editing.

Archive policy:
- source media included: true
- render cache included: false
- proxy media included: {str(bool(include_proxy_media)).lower()}

RabbitHole does not automatically import, load, delete, or switch projects during
restore. Validate the restored project with the supplied hook interface before
using it as the only copy.
"""


def _file_manifest(root: Path, *, exclude: set[str] | None = None) -> list[dict[str, Any]]:
    ignored = exclude or set()
    records: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in ignored:
            continue
        records.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return records


def _write_checksums(root: Path) -> tuple[Path, int]:
    _validate_portable_tree(root)
    checksum_path = root / CHECKSUM_FILENAME
    records = _file_manifest(root, exclude={CHECKSUM_FILENAME})
    lines = [f"{record['sha256']}  {record['path']}" for record in records]
    _atomic_text(checksum_path, "\n".join(lines) + ("\n" if lines else ""))
    return checksum_path, len(records) + 1


def _zip_compression(path: Path) -> int:
    return (
        zipfile.ZIP_STORED
        if path.suffix.casefold() in _ZIP_STORED_SUFFIXES
        else zipfile.ZIP_DEFLATED
    )


def _zip_tree(source: Path, target: Path, archive_root_name: str) -> None:
    _validate_portable_tree(source)
    root_name = _validate_portable_relative(
        archive_root_name,
        label="handoff ZIP root",
    )
    with zipfile.ZipFile(
        target,
        mode="x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            relative = path.relative_to(source).as_posix()
            member_name = _validate_portable_relative(
                f"{root_name}/{relative}",
                label="handoff ZIP member",
            )
            info = zipfile.ZipInfo(
                member_name,
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            info.compress_type = _zip_compression(path)
            info.external_attr = 0o100644 << 16
            with path.open("rb") as source_stream, archive.open(
                info,
                mode="w",
                force_zip64=True,
            ) as target_stream:
                shutil.copyfileobj(
                    source_stream,
                    target_stream,
                    length=_COPY_BUFFER_BYTES,
                )


def _resolve_version(resolve: Any) -> str | None:
    method = getattr(resolve, "GetVersionString", None)
    if not callable(method):
        return None
    try:
        value = method()
    except Exception:
        return None
    return str(value) if value else None


def _timeline_from_plan(plan: Mapping[str, Any]) -> str | None:
    value = plan.get("timeline_name")
    return str(value) if isinstance(value, str) and value else None


def _require_plan_timeline(
    project: Any,
    plan: Mapping[str, Any],
) -> Any:
    timeline_name = _timeline_from_plan(plan)
    if (
        timeline_name is None
        or not re.fullmatch(
            r"AUTO_BUILD_[A-Za-z0-9][A-Za-z0-9_-]{7,63}", timeline_name
        )
    ):
        raise ResolveHandoffError(
            "handoff plan must identify an AUTO_BUILD_<hash> timeline"
        )
    count = _call_required(project, "GetTimelineCount")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ResolveHandoffError(f"GetTimelineCount() returned {count!r}")
    matches: list[Any] = []
    for index in range(1, count + 1):
        timeline = _call_required(project, "GetTimelineByIndex", index)
        if timeline is None:
            raise ResolveHandoffError(
                f"GetTimelineByIndex({index}) returned no timeline"
            )
        name = _call_required(timeline, "GetName")
        if name == timeline_name:
            matches.append(timeline)
    if len(matches) != 1:
        raise ResolveHandoffError(
            f"current project must contain exactly one {timeline_name!r} timeline; "
            f"found {len(matches)}. Refusing to archive a possibly wrong open project."
        )
    # Import lazily because resolve_runner invokes package_handoff lazily too.
    # Handoff must authenticate the immutable build, not merely trust a name
    # that an unrelated or edited timeline could be given.
    from .resolve_runner import ResolveRunnerError, _validate_expected_timeline

    try:
        _validate_expected_timeline(matches[0], plan, ())
    except ResolveRunnerError as exc:
        raise ResolveHandoffError(
            f"timeline {timeline_name!r} does not match the immutable plan: {exc}"
        ) from exc
    return matches[0]


def _media_size_estimate(
    plan: Mapping[str, Any], project_root: Path
) -> tuple[int, int, list[str]]:
    candidates: list[str] = []
    for collection_name in ("provenance", "audio", "clips"):
        collection = plan.get(collection_name)
        if not isinstance(collection, Sequence) or isinstance(
            collection, (str, bytes)
        ):
            continue
        for value in collection:
            if not isinstance(value, Mapping):
                continue
            for key in ("local_path", "media_path", "path"):
                raw = value.get(key)
                if isinstance(raw, str) and raw:
                    candidates.append(raw)
                    break
    unique: dict[str, Path] = {}
    for raw in candidates:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = project_root / path
        path = path.resolve(strict=False)
        if not path.is_file():
            continue
        unique[os.path.normcase(os.fspath(path))] = path
    source_bytes = sum(path.stat().st_size for path in unique.values())
    # DRA source copy + portable ZIP, with conservative fixed room for DRP,
    # manifests, presets, filesystem allocation, and Resolve bookkeeping.
    estimate = int(source_bytes * 2.1) + 64 * 1024 * 1024
    relative_paths = []
    for path in sorted(unique.values()):
        if is_path_within(path, project_root):
            relative_paths.append(path.relative_to(project_root).as_posix())
        else:
            identity = hashlib.sha256(
                os.path.normcase(os.fspath(path)).encode("utf-8")
            ).hexdigest()[:12]
            relative_paths.append(f"external/{identity}/{path.name}")
    return source_bytes, estimate, relative_paths


def _nearest_existing_directory(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise ResolveHandoffError(
                f"cannot find an existing filesystem for handoff destination: {path}"
            )
        candidate = parent
    if not candidate.is_dir():
        candidate = candidate.parent
    return candidate


def package_handoff(
    project_root: os.PathLike[str] | str,
    *,
    resolve: Any = None,
    app: Any = None,
    plan: Mapping[str, Any] | None = None,
    destination: os.PathLike[str] | str | None = None,
    fonts: (
        Mapping[str, os.PathLike[str] | str]
        | Iterable[os.PathLike[str] | str]
        | os.PathLike[str]
        | str
        | None
    ) = None,
    presets: (
        Mapping[str, os.PathLike[str] | str]
        | Iterable[os.PathLike[str] | str]
        | os.PathLike[str]
        | str
        | None
    ) = None,
    licenses: (
        Mapping[str, os.PathLike[str] | str]
        | Iterable[os.PathLike[str] | str]
        | os.PathLike[str]
        | str
        | None
    ) = None,
    include_files: (
        Mapping[str, os.PathLike[str] | str]
        | Iterable[os.PathLike[str] | str]
        | os.PathLike[str]
        | str
        | None
    ) = None,
    bundle_name: str | None = None,
    validation_hooks: Iterable[Callable[[HandoffValidationContext], Any]] = (),
    operation_lock: ProjectLock | None = None,
    include_proxy_media: bool = False,
) -> dict[str, Any]:
    """Create an immutable DRA + DRP + portable ZIP of the current project."""

    root = _canonical(project_root)
    if not root.is_dir():
        raise ResolveHandoffError(f"project root does not exist: {root}")
    if not isinstance(include_proxy_media, bool):
        raise ResolveHandoffError("include_proxy_media must be a boolean")
    plan_data = dict(plan or {})
    output_root = validate_handoff_output_root(
        destination or (root / "resolve" / "handoffs"), project_root=root
    )
    if output_root.exists() and not output_root.is_dir():
        raise ResolveHandoffError(
            f"handoff destination is not a directory: {output_root}"
        )

    # Import lazily to keep the handoff validator usable without runner setup.
    from .resolve_runner import connect_resolve

    connection = connect_resolve(resolve=resolve, app=app)
    manager = get_project_manager(connection.resolve)
    project = get_current_project(manager)
    if project is None:
        raise ResolveHandoffError(
            "no current Resolve project; handoff will not load/switch a project"
        )
    require_render_idle(project)
    actual_project_name = project_name(project)
    timeline_name = _timeline_from_plan(plan_data)
    _require_plan_timeline(project, plan_data)
    build_token = str(
        plan_data.get("build_id")
        or plan_data.get("build_hash")
        or hashlib.sha256(
            json.dumps(plan_data, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]
    )
    requested_name = bundle_name or (
        f"RABBITHOLE_HANDOFF_{_safe_name(actual_project_name, fallback='PROJECT')}_"
        f"{_safe_name(build_token, fallback='BUILD')}"
    )
    final_name = _safe_name(requested_name, fallback="RABBITHOLE_HANDOFF")
    final_directory = output_root / final_name
    final_zip = output_root / f"{final_name}.zip"
    final_zip_checksum = output_root / f"{final_name}.zip.sha256"
    for target in (final_directory, final_zip, final_zip_checksum):
        if target.exists():
            raise ResolveHandoffError(
                f"immutable handoff target already exists: {target}"
            )

    source_bytes, required_free_bytes, estimated_media_paths = _media_size_estimate(
        plan_data, root
    )
    free_bytes = shutil.disk_usage(_nearest_existing_directory(output_root)).free
    if free_bytes < required_free_bytes:
        raise ResolveHandoffError(
            "insufficient free space for source-inclusive DRA + ZIP: "
            f"need at least {required_free_bytes} bytes, have {free_bytes} bytes"
        )

    # Keep staging names short: Resolve handoffs contain deeply nested Fusion
    # templates and Windows still commonly enforces MAX_PATH in copy APIs.
    staging = output_root / f".rh-{uuid4().hex[:12]}"
    temporary_zip = output_root / f".rh-{uuid4().hex[:12]}.zip.tmp"
    temporary_zip_checksum = output_root / f".rh-{uuid4().hex[:12]}.sha256.tmp"
    write_roots = (root / "resolve", output_root)
    lock_context = (
        nullcontext(operation_lock)
        if operation_lock is not None
        else ProjectLock(
            root,
            stage="resolve-handoff",
            write_roots=write_roots,
            allow_external_write_roots=True,
        )
    )
    with lock_context as lock:
        if lock is None:
            raise ResolveHandoffError("handoff operation lock is unavailable")
        # A queue-owned lock must have declared the requested external root.
        for final_target in (final_directory, final_zip, final_zip_checksum):
            lock.assert_write_path(final_target)
        staging = lock.assert_write_path(staging)
        temporary_zip = lock.assert_write_path(temporary_zip)
        temporary_zip_checksum = lock.assert_write_path(temporary_zip_checksum)
        output_root.mkdir(parents=True, exist_ok=True)
        for target in (final_directory, final_zip, final_zip_checksum):
            if target.exists():
                raise ResolveHandoffError(
                    f"immutable handoff target appeared while waiting for lock: {target}"
                )
        free_bytes = shutil.disk_usage(output_root).free
        if free_bytes < required_free_bytes:
            raise ResolveHandoffError(
                "insufficient free space for source-inclusive DRA + ZIP: "
                f"need at least {required_free_bytes} bytes, have {free_bytes} bytes"
            )
        staging.mkdir(parents=False, exist_ok=False)
        published: list[Path] = []
        try:
            current_now = get_current_project(manager)
            if current_now is None or project_name(current_now) != actual_project_name:
                raise ResolveHandoffError(
                    "current Resolve project changed before archive; operation refused"
                )
            require_render_idle(current_now)
            _require_plan_timeline(current_now, plan_data)
            dra_path = staging / "project.dra"
            drp_path = staging / "project.drp"
            archived = _call_required(
                manager,
                "ArchiveProject",
                actual_project_name,
                os.fspath(dra_path),
                True,
                False,
                bool(include_proxy_media),
            )
            if archived is not True:
                raise ResolveHandoffError(
                    "ArchiveProject() did not create a source-inclusive DRA"
                )
            if not dra_path.exists():
                raise ResolveHandoffError(
                    f"ArchiveProject() reported success but DRA is missing: {dra_path}"
                )

            exported = _call_required(
                manager,
                "ExportProject",
                actual_project_name,
                os.fspath(drp_path),
                True,
            )
            if exported is not True:
                raise ResolveHandoffError("ExportProject() did not create a DRP")
            if not drp_path.is_file():
                raise ResolveHandoffError(
                    f"ExportProject() reported success but DRP is missing: {drp_path}"
                )

            repository_root = Path(__file__).resolve().parents[1]
            default_fonts = (root / "fonts",)
            default_presets = (
                repository_root / "resolve" / "crowley_style.yaml",
                repository_root / "resolve" / "Fusion",
                repository_root / "resolve" / "grades",
                repository_root / "style" / "luts" / "crowley-noir.cube",
                root / "resolve" / "presets",
            )
            default_licenses = (root / "licenses", root / "licences")
            copied = {
                "fonts": _copy_category(
                    staging,
                    "fonts",
                    _with_default_sources(default_fonts, fonts),
                ),
                "presets": _copy_category(
                    staging,
                    "presets",
                    _with_default_sources(default_presets, presets),
                ),
                "licenses": _copy_category(
                    staging,
                    "licenses",
                    _with_default_sources(default_licenses, licenses),
                ),
                "project_files": _copy_project_files(
                    staging, root, plan_data, include_files
                ),
            }
            readme = staging / README_FILENAME
            _atomic_text(
                readme,
                _readme(
                    actual_project_name,
                    timeline_name,
                    include_proxy_media=include_proxy_media,
                ),
            )

            manifest = {
                "schema_version": HANDOFF_SCHEMA_VERSION,
                "created_at": utc_now(),
                "project_name": actual_project_name,
                "timeline_name": timeline_name,
                "build_id": plan_data.get("build_id"),
                "resolve_version": _resolve_version(connection.resolve),
                "archive": {
                    "dra": "project.dra",
                    "drp": "project.drp",
                    "source_media": True,
                    "render_cache": False,
                    "proxy_media": bool(include_proxy_media),
                },
                "disk_space_preflight": {
                    "unique_source_bytes": source_bytes,
                    "estimated_required_bytes": required_free_bytes,
                    "free_bytes_before_archive": free_bytes,
                    "media_paths": estimated_media_paths,
                    "multiplier": 2.1,
                    "fixed_overhead_bytes": 64 * 1024 * 1024,
                },
                "copied": copied,
                "restore": {
                    "automatic_import_performed": False,
                    "validation_hooks_supported": True,
                },
            }
            manifest_path = staging / MANIFEST_FILENAME
            _write_json(manifest_path, manifest)

            # Hooks can inspect the freshly written package before it becomes
            # immutable.  They never receive an importer or a project switch.
            context = HandoffValidationContext(staging, manifest, project)
            for hook in validation_hooks:
                verdict = hook(context)
                if verdict is False:
                    name = getattr(hook, "__name__", repr(hook))
                    raise HandoffValidationError(
                        f"handoff validation hook {name} rejected the package"
                    )

            checksum_path, file_count = _write_checksums(staging)
            validate_handoff(staging)
            _zip_tree(staging, temporary_zip, final_name)
            zip_digest = _sha256(temporary_zip)
            _atomic_text(
                temporary_zip_checksum,
                f"{zip_digest}  {final_zip.name}\n",
            )

            # Publish as a small transaction. If any later promotion fails,
            # remove only the exact artifacts successfully promoted by this
            # lock holder; an incomplete handoff must never look immutable.
            for source, target in (
                (staging, final_directory),
                (temporary_zip, final_zip),
                (temporary_zip_checksum, final_zip_checksum),
            ):
                if target.exists():
                    raise ResolveHandoffError(
                        f"immutable handoff target appeared before publish: {target}"
                    )
                os.replace(source, target)
                published.append(target)
        except Exception as exc:
            cleanup_errors: list[str] = []
            # Only exact published or unique runner-owned paths are removed.
            for cleanup in (
                *reversed(published),
                staging,
                temporary_zip,
                temporary_zip_checksum,
            ):
                try:
                    _remove_owned_path(cleanup)
                except OSError as cleanup_exc:
                    cleanup_errors.append(f"{cleanup}: {cleanup_exc}")
            if cleanup_errors:
                raise ResolveHandoffError(
                    "handoff publication failed and rollback was incomplete: "
                    + "; ".join(cleanup_errors)
                ) from exc
            raise

    result = HandoffResult(
        action="handoff",
        project_name=actual_project_name,
        timeline_name=timeline_name,
        package_directory=os.fspath(final_directory),
        zip_path=os.fspath(final_zip),
        zip_sha256=zip_digest,
        manifest_path=os.fspath(final_directory / MANIFEST_FILENAME),
        checksums_path=os.fspath(final_directory / CHECKSUM_FILENAME),
        dra_path=os.fspath(final_directory / "project.dra"),
        drp_path=os.fspath(final_directory / "project.drp"),
        file_count=file_count,
    )
    return asdict(result)


create_handoff = package_handoff


def _parse_checksums(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    portable_paths: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise HandoffValidationError(f"cannot read checksums: {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        digest, separator, relative = line.partition("  ")
        if (
            not separator
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not relative
        ):
            raise HandoffValidationError(
                f"invalid checksum line {line_number} in {path}"
            )
        normalized = _validate_portable_relative(
            relative,
            label=f"checksum path on line {line_number}",
        )
        if normalized in checksums:
            raise HandoffValidationError(f"duplicate checksum path: {normalized}")
        collision_key = _portable_key(normalized)
        previous = portable_paths.get(collision_key)
        if previous is not None and previous != normalized:
            raise HandoffValidationError(
                "checksum paths collide on a case-insensitive or "
                f"Unicode-normalizing filesystem: {previous!r} and {normalized!r}"
            )
        portable_paths[collision_key] = normalized
        checksums[normalized] = digest
    return checksums


def _validate_directory(
    root: Path,
    *,
    hooks: Iterable[Callable[[HandoffValidationContext], Any]],
    restored_project: Any = None,
) -> dict[str, Any]:
    _validate_portable_tree(root)
    manifest_path = root / MANIFEST_FILENAME
    checksum_path = root / CHECKSUM_FILENAME
    dra_path = root / "project.dra"
    drp_path = root / "project.drp"
    readme_path = root / README_FILENAME
    for required in (
        manifest_path,
        checksum_path,
        dra_path,
        drp_path,
        readme_path,
        root / "fonts",
        root / "presets",
        root / "licenses",
    ):
        if not required.exists():
            raise HandoffValidationError(f"required handoff artifact is missing: {required}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HandoffValidationError(f"invalid handoff manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise HandoffValidationError("handoff manifest must be a JSON object")
    if manifest.get("schema_version") != HANDOFF_SCHEMA_VERSION:
        raise HandoffValidationError(
            f"unsupported handoff schema: {manifest.get('schema_version')!r}"
        )
    archive = manifest.get("archive")
    if not isinstance(archive, Mapping) or (
        archive.get("source_media") is not True
        or archive.get("render_cache") is not False
        or not isinstance(archive.get("proxy_media"), bool)
    ):
        raise HandoffValidationError(
            "handoff archive policy must include source media and exclude "
            "render cache, and must explicitly record proxy-media policy"
        )

    expected = _parse_checksums(checksum_path)
    actual_files = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and path != checksum_path
    }
    if set(expected) != set(actual_files):
        missing = sorted(set(expected) - set(actual_files))
        extra = sorted(set(actual_files) - set(expected))
        raise HandoffValidationError(
            f"checksum inventory mismatch; missing={missing}, extra={extra}"
        )
    for relative, digest in expected.items():
        actual = _sha256(actual_files[relative])
        if actual != digest:
            raise HandoffValidationError(
                f"checksum mismatch for {relative}: expected {digest}, got {actual}"
            )

    context = HandoffValidationContext(root, manifest, restored_project)
    for hook in hooks:
        verdict = hook(context)
        if verdict is False:
            name = getattr(hook, "__name__", repr(hook))
            raise HandoffValidationError(
                f"restore validation hook {name} rejected the handoff"
            )
    return {
        "valid": True,
        "package_root": os.fspath(root),
        "project_name": manifest.get("project_name"),
        "timeline_name": manifest.get("timeline_name"),
        "file_count": len(actual_files) + 1,
        "manifest": manifest,
    }


def _safe_zip_members(
    archive: zipfile.ZipFile,
) -> tuple[str, list[zipfile.ZipInfo]]:
    members = archive.infolist()
    if not members:
        raise HandoffValidationError("handoff ZIP is empty")
    root_name: str | None = None
    member_names: dict[str, str] = {}
    portable_names: dict[str, str] = {}
    file_names: set[str] = set()
    for info in members:
        raw = info.filename
        directory = info.is_dir()
        candidate = raw[:-1] if directory else raw
        try:
            normalized = _validate_portable_relative(
                candidate,
                label="handoff ZIP member",
            )
        except HandoffValidationError as exc:
            raise HandoffValidationError(
                f"unsafe path in handoff ZIP: {info.filename!r}: {exc}"
            ) from exc
        path = PurePosixPath(normalized)
        if len(path.parts) == 1 and not directory:
            raise HandoffValidationError(
                "handoff ZIP members must live below one exact top-level "
                f"package root: {info.filename!r}"
            )
        current_root = path.parts[0]
        if root_name is None:
            root_name = current_root
        elif current_root != root_name:
            raise HandoffValidationError(
                "handoff ZIP must contain one exact top-level package root; "
                f"found {root_name!r} and {current_root!r}"
            )

        previous_exact = member_names.get(normalized)
        if previous_exact is not None:
            raise HandoffValidationError(
                f"duplicate handoff ZIP member: {info.filename!r}"
            )
        member_names[normalized] = raw

        collision_key = _portable_key(normalized)
        previous_portable = portable_names.get(collision_key)
        if previous_portable is not None and previous_portable != normalized:
            raise HandoffValidationError(
                "handoff ZIP members collide on a case-insensitive or "
                "Unicode-normalizing filesystem: "
                f"{previous_portable!r} and {normalized!r}"
            )
        portable_names[collision_key] = normalized

        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if unix_mode and stat.S_ISLNK(unix_mode):
            raise HandoffValidationError(
                f"handoff ZIP cannot contain symbolic links: {info.filename!r}"
            )
        if not directory:
            file_names.add(normalized)

    if root_name is None:
        raise HandoffValidationError("handoff ZIP has no package root")
    for file_name in file_names:
        prefix = f"{file_name}/"
        if any(name.startswith(prefix) for name in member_names):
            raise HandoffValidationError(
                "handoff ZIP contains a file that is also a parent directory: "
                f"{file_name!r}"
            )
    return root_name, members


def _extract_zip_members(
    archive: zipfile.ZipFile,
    destination: Path,
    members: Iterable[zipfile.ZipInfo],
) -> None:
    for info in members:
        raw = info.filename
        candidate = raw[:-1] if info.is_dir() else raw
        normalized = _validate_portable_relative(
            candidate,
            label="handoff ZIP member",
        )
        target = destination.joinpath(*PurePosixPath(normalized).parts)
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with archive.open(info, mode="r") as source_stream, target.open(
                "xb"
            ) as target_stream:
                shutil.copyfileobj(
                    source_stream,
                    target_stream,
                    length=_COPY_BUFFER_BYTES,
                )
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise HandoffValidationError(
                f"cannot extract handoff ZIP member {info.filename!r}: {exc}"
            ) from exc


def validate_handoff(
    package: os.PathLike[str] | str,
    *,
    hooks: Iterable[Callable[[HandoffValidationContext], Any]] = (),
    restored_project: Any = None,
) -> dict[str, Any]:
    """Validate checksums/policy and run optional restored-project hooks."""

    source = _canonical(package)
    if source.is_dir():
        return _validate_directory(
            source, hooks=hooks, restored_project=restored_project
        )
    if not source.is_file() or source.suffix.lower() != ".zip":
        raise HandoffValidationError(
            f"handoff must be a package directory or ZIP: {source}"
        )
    with tempfile.TemporaryDirectory(prefix="rabbithole-handoff-validate-") as temp:
        destination = Path(temp)
        with zipfile.ZipFile(source, "r") as archive:
            root_name, members = _safe_zip_members(archive)
            _extract_zip_members(archive, destination, members)
        package_root = destination / root_name
        if not package_root.is_dir():
            raise HandoffValidationError(
                "handoff ZIP top-level package root is not a directory"
            )
        result = _validate_directory(
            package_root,
            hooks=hooks,
            restored_project=restored_project,
        )
        result["zip_path"] = os.fspath(source)
        return result


def restore_handoff(
    package: os.PathLike[str] | str,
    destination: os.PathLike[str] | str,
    *,
    hooks: Iterable[Callable[[HandoffValidationContext], Any]] = (),
    restored_project: Any = None,
) -> dict[str, Any]:
    """Extract/copy and validate a handoff without importing a Resolve project.

    Restore into Resolve's Project Manager remains a deliberate human action.
    Hooks can inspect ``restored_project`` after that action, but this function
    never calls ImportProject, LoadProject, or SetCurrentDatabase.
    """

    source = _canonical(package)
    target = _canonical(destination)
    anchor = Path(target.anchor).resolve(strict=False)
    home = Path.home().resolve(strict=False)
    if target in (anchor, home):
        raise UnsafeWriteError(f"restore destination is too broad: {target}")
    if target.exists():
        raise ResolveHandoffError(
            f"restore destination already exists; refusing overwrite: {target}"
        )
    _validate_portable_relative(
        target.name,
        label="restore destination name",
    )
    if source.is_dir() and is_path_within(target, source):
        raise UnsafeWriteError(
            f"restore destination cannot be inside source package: {target}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".rh-restore-{uuid4().hex[:12]}"
    if staging.exists():
        raise ResolveHandoffError(
            f"unique restore staging path already exists: {staging}"
        )
    promoted = False
    try:
        package_relative = Path()
        if source.is_dir():
            _validate_portable_tree(source)
            shutil.copytree(source, staging, symlinks=True)
            package_root = staging
        elif source.is_file() and source.suffix.lower() == ".zip":
            staging.mkdir(parents=False, exist_ok=False)
            with zipfile.ZipFile(source, "r") as archive:
                root_name, members = _safe_zip_members(archive)
                _extract_zip_members(archive, staging, members)
            package_relative = Path(root_name)
            package_root = staging / package_relative
            if not package_root.is_dir():
                raise HandoffValidationError(
                    "handoff ZIP top-level package root is not a directory"
                )
        else:
            raise HandoffValidationError(
                f"handoff must be a package directory or ZIP: {source}"
            )
        result = _validate_directory(
            package_root, hooks=hooks, restored_project=restored_project
        )
        if target.exists():
            raise ResolveHandoffError(
                f"restore destination appeared before publish: {target}"
            )
        os.replace(staging, target)
        promoted = True
        final_package_root = target / package_relative
        result["package_root"] = os.fspath(final_package_root)
        result["restore_directory"] = os.fspath(target)
        result["resolve_import_performed"] = False
        return result
    except Exception as exc:
        # Staging is a unique sibling of the requested target. If promotion
        # succeeded and a later step failed, remove only that exact target.
        cleanup = target if promoted else staging
        try:
            _remove_owned_path(cleanup)
        except OSError as cleanup_exc:
            raise ResolveHandoffError(
                "handoff restore failed and rollback was incomplete for "
                f"{cleanup}: {cleanup_exc}"
            ) from exc
        raise


__all__ = [
    "CHECKSUM_FILENAME",
    "HANDOFF_SCHEMA_VERSION",
    "HandoffResult",
    "HandoffValidationContext",
    "HandoffValidationError",
    "MANIFEST_FILENAME",
    "README_FILENAME",
    "ResolveHandoffError",
    "create_handoff",
    "package_handoff",
    "restore_handoff",
    "validate_handoff",
    "validate_handoff_output_root",
]

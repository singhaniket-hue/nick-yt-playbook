"""Portable, deterministic episode bundles for pre-Resolve project transfer.

This module deliberately performs no Resolve, network, or process operations.
It packages only project-local inputs and rejects machine-specific metadata so
that a restored episode has the same relative paths on Windows and macOS.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import tempfile
from typing import Any, Callable
import unicodedata
import zipfile


BUNDLE_SCHEMA_VERSION = "rabbithole-episode-bundle.v1"
ARCHIVE_ROOT = "project"
METADATA_DIRECTORY = ".rabbithole-bundle"
MANIFEST_PATH = f"{METADATA_DIRECTORY}/manifest.json"
CHECKSUMS_PATH = f"{METADATA_DIRECTORY}/checksums.sha256"
README_PATH = f"{METADATA_DIRECTORY}/README.txt"

_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_WINDOWS_FORBIDDEN_RE = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".cache",
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        "__pycache__",
        "cache",
        "caches",
        "cacheclip",
        "handoffs",
        "proxymedia",
        "renders",
    }
)
_RESOLVE_GENERATED_NAMES = frozenset(
    {
        ".resolve-runner.lock",
        "state.json",
    }
)
_RESOLVE_ROOT_GENERATED_NAMES = frozenset(
    {
        "current.json",
    }
)
_RESOLVE_GENERATED_DIRECTORIES = frozenset(
    {
        "build",
        "builds",
        "cache",
        "caches",
        "handoffs",
        "queue",
        "renders",
    }
)
_PATH_KEYS = frozenset(
    {
        "directory",
        "evidence_overlay",
        "fcpxml_path",
        "file",
        "local_path",
        "media_root",
        "media_path",
        "output_path",
        "path",
        "plan_path",
        "project_root",
        "queue_path",
        "source_path",
    }
)
_PATH_CONTAINER_KEYS = frozenset(
    {
        "local_paths",
        "media_paths",
        "output_paths",
        "path_overrides",
        "paths",
        "source_paths",
    }
)
_AUDIO_STEMS_ROOT = "resolve/audio-stems"
_AUDIO_STEM_POINTER = f"{_AUDIO_STEMS_ROOT}/current.json"
_AUDIO_STEM_SCHEMA = "resolve-audio-stems.v1"
_AUDIO_STEM_MANIFEST_NAME = "manifest.json"
_AUDIO_STEM_FILES = {
    "music": ("A3", "music-stem.wav"),
    "sfx": ("A4", "sfx-stem.wav"),
}


class EpisodeBundleError(RuntimeError):
    """An episode could not be packaged or restored safely."""


class EpisodeBundleValidationError(EpisodeBundleError):
    """A bundle or its project metadata is invalid or not portable."""


class UnsafeEpisodeDestinationError(EpisodeBundleError):
    """A package or restore destination is too broad or would overwrite data."""


@dataclass(frozen=True)
class _SourceFile:
    relative: str
    path: Path
    size: int
    sha256: str


def _canonical(path: os.PathLike[str] | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.fspath(left)) == os.path.normcase(os.fspath(right))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_link_like(path: Path, metadata: os.stat_result | None = None) -> bool:
    if path.is_symlink():
        return True
    details = metadata if metadata is not None else path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(details, "st_file_attributes", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _validate_exact_project_root(project_root: os.PathLike[str] | str) -> Path:
    raw = Path(project_root).expanduser()
    if raw.is_symlink():
        raise EpisodeBundleValidationError(
            f"project root cannot be a symlink: {raw}"
        )
    root = raw.resolve(strict=False)
    if not root.is_dir():
        raise EpisodeBundleValidationError(f"project root does not exist: {root}")
    anchor = Path(root.anchor).resolve(strict=False)
    home = Path.home().resolve(strict=False)
    if _same_path(root, anchor) or _same_path(root, home) or not root.name:
        raise EpisodeBundleValidationError(
            f"project root is too broad for an episode bundle: {root}"
        )
    return root


def _validate_portable_relative(
    value: str,
    *,
    label: str,
    allow_metadata_directory: bool = False,
) -> str:
    if not value or value != value.strip():
        raise EpisodeBundleValidationError(
            f"{label} must be a non-empty relative path without edge whitespace"
        )
    if "\\" in value:
        raise EpisodeBundleValidationError(
            f"{label} uses a Windows-only separator; use '/': {value!r}"
        )
    if _URI_RE.match(value):
        raise EpisodeBundleValidationError(
            f"{label} is an external URI, not a project-relative path: {value!r}"
        )
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if windows.drive or windows.is_absolute() or posix.is_absolute():
        raise EpisodeBundleValidationError(
            f"{label} must not be absolute or drive-qualified: {value!r}"
        )
    if value.startswith("~"):
        raise EpisodeBundleValidationError(
            f"{label} must not be home-relative: {value!r}"
        )
    raw_parts = value.split("/")
    if (
        any(part in {"", ".", ".."} for part in raw_parts)
        or posix.as_posix() != value
    ):
        raise EpisodeBundleValidationError(
            f"{label} contains an escaping or ambiguous component: {value!r}"
        )
    if len(value) > 240:
        raise EpisodeBundleValidationError(
            f"{label} exceeds the portable 240-character path limit: {value!r}"
        )
    for part in posix.parts:
        normalized = unicodedata.normalize("NFC", part)
        if normalized != part:
            raise EpisodeBundleValidationError(
                f"{label} must use NFC-normalized names: {value!r}"
            )
        if len(part.encode("utf-8")) > 255:
            raise EpisodeBundleValidationError(
                f"{label} has a component longer than 255 bytes: {value!r}"
            )
        if _WINDOWS_FORBIDDEN_RE.search(part) or part.endswith((" ", ".")):
            raise EpisodeBundleValidationError(
                f"{label} is not valid on Windows: {value!r}"
            )
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise EpisodeBundleValidationError(
                f"{label} uses a Windows-reserved name: {value!r}"
            )
    if not allow_metadata_directory and posix.parts[0].casefold() == (
        METADATA_DIRECTORY.casefold()
    ):
        raise EpisodeBundleValidationError(
            f"{label} uses the reserved bundle metadata directory"
        )
    return posix.as_posix()


def _portable_key(value: str) -> str:
    return "/".join(
        unicodedata.normalize("NFC", part).casefold()
        for part in PurePosixPath(value).parts
    )


def _is_env_secret(name: str) -> bool:
    lowered = name.casefold()
    return (
        lowered == ".env"
        or lowered.startswith(".env.")
        or lowered == ".envrc"
        or lowered.startswith(".envrc.")
        or lowered.endswith(".env")
    )


def _is_excluded(relative: str, *, is_directory: bool) -> bool:
    parts = tuple(part.casefold() for part in PurePosixPath(relative).parts)
    if not parts:
        return False
    if any(_is_env_secret(part) for part in parts):
        return True
    if METADATA_DIRECTORY.casefold() in parts:
        return True
    if any(part in _EXCLUDED_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    if is_directory and parts[-1] in _EXCLUDED_DIRECTORY_NAMES:
        return True
    if "resolve" in parts:
        resolve_index = parts.index("resolve")
        tail = parts[resolve_index + 1 :]
        if tail:
            if tail[0] in _RESOLVE_GENERATED_DIRECTORIES:
                return True
            if not is_directory and (
                tail[-1] in _RESOLVE_GENERATED_NAMES
                or tail[-1].endswith(".lock")
                or (
                    len(tail) == 1
                    and tail[-1] in _RESOLVE_ROOT_GENERATED_NAMES
                )
            ):
                return True
    if not is_directory and parts[-1] in {".ds_store", "thumbs.db"}:
        return True
    return False


def _scan_tree(root: Path) -> tuple[list[Path], list[Path]]:
    """Return included files/directories while inspecting all entries for links."""

    files: list[Path] = []
    directories: list[Path] = []

    def visit(directory: Path, *, excluded_ancestor: bool) -> None:
        try:
            entries = sorted(
                os.scandir(directory),
                key=lambda entry: unicodedata.normalize("NFC", entry.name).casefold(),
            )
        except OSError as exc:
            raise EpisodeBundleValidationError(
                f"cannot inspect project directory {directory}: {exc}"
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                details = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise EpisodeBundleValidationError(
                    f"cannot inspect project entry {path}: {exc}"
                ) from exc
            if _is_link_like(path, details):
                raise EpisodeBundleValidationError(
                    f"episode bundles cannot contain symlinks or junctions: {path}"
                )
            relative = path.relative_to(root).as_posix()
            is_directory = entry.is_dir(follow_symlinks=False)
            excluded = excluded_ancestor or _is_excluded(
                relative, is_directory=is_directory
            )
            if is_directory:
                if not excluded:
                    directories.append(path)
                visit(path, excluded_ancestor=excluded)
            elif entry.is_file(follow_symlinks=False):
                if not excluded:
                    files.append(path)
            else:
                raise EpisodeBundleValidationError(
                    f"episode bundles support only regular files and directories: {path}"
                )

    visit(root, excluded_ancestor=False)
    return files, directories


def _validate_metadata_path(
    value: Any,
    *,
    key: str,
    metadata_path: str,
    project_root: Path | None,
    included_files: set[str],
    require_file: bool,
) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise EpisodeBundleValidationError(
            f"{metadata_path}: {key!r} must be a string or null"
        )
    if not value:
        if require_file:
            raise EpisodeBundleValidationError(
                f"{metadata_path}: {key!r} must be a non-empty string when present"
            )
        return
    relative = _validate_portable_relative(
        value,
        label=f"{metadata_path} field {key!r}",
    )
    if require_file:
        if project_root is not None:
            resolved = (
                project_root / Path(*PurePosixPath(relative).parts)
            ).resolve(strict=False)
            if not _is_within(resolved, project_root):
                raise EpisodeBundleValidationError(
                    f"{metadata_path}: {key!r} escapes the project root: {value!r}"
                )
        else:
            resolved = None
        if resolved is not None and not resolved.is_file():
            raise EpisodeBundleValidationError(
                f"{metadata_path}: referenced local_path media is missing: {value!r}"
            )
        if relative not in included_files:
            detail = (
                "is excluded from the portable bundle"
                if resolved is not None
                else "is missing or excluded from the portable bundle"
            )
            raise EpisodeBundleValidationError(
                f"{metadata_path}: referenced local_path media {detail}: {value!r}"
            )


def _validate_path_container(
    value: Any,
    *,
    key: str,
    metadata_path: str,
    project_root: Path | None,
    included_files: set[str],
    require_file: bool,
) -> None:
    if value is None:
        return
    if isinstance(value, str):
        _validate_metadata_path(
            value,
            key=key,
            metadata_path=metadata_path,
            project_root=project_root,
            included_files=included_files,
            require_file=require_file,
        )
        return
    if isinstance(value, list):
        for item in value:
            _validate_path_container(
                item,
                key=key,
                metadata_path=metadata_path,
                project_root=project_root,
                included_files=included_files,
                require_file=require_file,
            )
        return
    if isinstance(value, dict):
        for item in value.values():
            _validate_path_container(
                item,
                key=key,
                metadata_path=metadata_path,
                project_root=project_root,
                included_files=included_files,
                require_file=require_file,
            )
        return
    raise EpisodeBundleValidationError(
        f"{metadata_path}: path container {key!r} has an unsupported value"
    )


def _audit_json_value(
    value: Any,
    *,
    metadata_path: str,
    project_root: Path | None,
    included_files: set[str],
) -> None:
    if isinstance(value, list):
        for item in value:
            _audit_json_value(
                item,
                metadata_path=metadata_path,
                project_root=project_root,
                included_files=included_files,
            )
        return
    if not isinstance(value, dict):
        return
    for raw_key, item in value.items():
        key = str(raw_key).casefold().replace("-", "_")
        is_single_path = key in _PATH_KEYS or key.endswith(
            ("_path", "_file", "_directory", "_dir")
        )
        is_path_container = key in _PATH_CONTAINER_KEYS or key.endswith(
            ("_paths", "_files", "_directories", "_dirs")
        )
        if is_single_path:
            _validate_metadata_path(
                item,
                key=str(raw_key),
                metadata_path=metadata_path,
                project_root=project_root,
                included_files=included_files,
                require_file=key == "local_path",
            )
        elif is_path_container:
            _validate_path_container(
                item,
                key=str(raw_key),
                metadata_path=metadata_path,
                project_root=project_root,
                included_files=included_files,
                require_file=key == "local_paths",
            )
        else:
            _audit_json_value(
                item,
                metadata_path=metadata_path,
                project_root=project_root,
                included_files=included_files,
            )


def _audit_json_metadata(
    root: Path,
    files: list[Path],
    included_files: set[str],
) -> None:
    for path in files:
        if path.suffix.casefold() != ".json":
            continue
        relative = path.relative_to(root).as_posix()
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EpisodeBundleValidationError(
                f"cannot audit JSON metadata {relative}: {exc}"
            ) from exc
        _audit_json_value(
            value,
            metadata_path=relative,
            project_root=root,
            included_files=included_files,
        )


def _audio_json_object(
    value: Any,
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EpisodeBundleValidationError(f"{label} must contain a JSON object")
    return value


def _audio_portable_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise EpisodeBundleValidationError(
            f"{label} must be a non-empty portable path"
        )
    return _validate_portable_relative(value, label=label)


def _audio_positive_number(value: Any, *, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise EpisodeBundleValidationError(
            f"{label} must be a finite positive number"
        )
    return float(value)


def _audit_audio_stem_selection(
    *,
    included_files: set[str],
    read_json_value: Callable[[str], Any],
    sha256_for: Callable[[str], str],
) -> None:
    """Validate the complete portable chain selected by audio-stems/current.json.

    Project inputs are required in the episode and checked against the immutable
    stem contract. Repository inputs (currently ``style/sfx.json``) retain their
    hash in that contract but are deliberately not required inside the episode
    bundle: a destination checkout can either reuse the stem set with the same
    style or prepare a new content-addressed set from the bundled raw sources.
    """

    if _AUDIO_STEM_POINTER not in included_files:
        return

    pointer = _audio_json_object(
        read_json_value(_AUDIO_STEM_POINTER),
        label=_AUDIO_STEM_POINTER,
    )
    if pointer.get("schema_version") != _AUDIO_STEM_SCHEMA:
        raise EpisodeBundleValidationError(
            f"{_AUDIO_STEM_POINTER}: unsupported schema_version "
            f"{pointer.get('schema_version')!r}"
        )
    fingerprint = pointer.get("fingerprint")
    if not isinstance(fingerprint, str) or not _HASH_RE.fullmatch(fingerprint):
        raise EpisodeBundleValidationError(
            f"{_AUDIO_STEM_POINTER}: fingerprint must be a full SHA-256 digest"
        )
    expected_manifest_path = f"{fingerprint}/{_AUDIO_STEM_MANIFEST_NAME}"
    manifest_path = _audio_portable_path(
        pointer.get("manifest_path"),
        label=f"{_AUDIO_STEM_POINTER} manifest_path",
    )
    if manifest_path != expected_manifest_path:
        raise EpisodeBundleValidationError(
            f"{_AUDIO_STEM_POINTER}: manifest_path must select the immutable "
            f"fingerprint directory {expected_manifest_path!r}"
        )
    manifest_relative = f"{_AUDIO_STEMS_ROOT}/{manifest_path}"
    if manifest_relative not in included_files:
        raise EpisodeBundleValidationError(
            f"{_AUDIO_STEM_POINTER}: selected audio-stem manifest is missing "
            f"from the portable bundle: {manifest_relative!r}"
        )
    manifest_sha = pointer.get("manifest_sha256")
    if not isinstance(manifest_sha, str) or not _HASH_RE.fullmatch(manifest_sha):
        raise EpisodeBundleValidationError(
            f"{_AUDIO_STEM_POINTER}: manifest_sha256 must be a full SHA-256 digest"
        )
    if sha256_for(manifest_relative) != manifest_sha:
        raise EpisodeBundleValidationError(
            f"{_AUDIO_STEM_POINTER}: selected audio-stem manifest checksum changed"
        )

    manifest = _audio_json_object(
        read_json_value(manifest_relative),
        label=manifest_relative,
    )
    if manifest.get("schema_version") != _AUDIO_STEM_SCHEMA:
        raise EpisodeBundleValidationError(
            f"{manifest_relative}: unsupported audio-stem schema"
        )
    if manifest.get("fingerprint") != fingerprint:
        raise EpisodeBundleValidationError(
            f"{manifest_relative}: fingerprint does not match current.json"
        )
    contract = manifest.get("contract")
    if not isinstance(contract, dict):
        raise EpisodeBundleValidationError(
            f"{manifest_relative}: contract must be an object"
        )
    observed_fingerprint = _sha256_bytes(
        json.dumps(
            contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if observed_fingerprint != fingerprint:
        raise EpisodeBundleValidationError(
            f"{manifest_relative}: contract fingerprint does not match its "
            "immutable directory"
        )

    inputs = contract.get("inputs")
    if not isinstance(inputs, list):
        raise EpisodeBundleValidationError(
            f"{manifest_relative}: contract.inputs must be an array"
        )
    seen_inputs: set[tuple[str, str]] = set()
    for index, raw_input in enumerate(inputs):
        label = f"{manifest_relative} contract.inputs[{index}]"
        if not isinstance(raw_input, dict):
            raise EpisodeBundleValidationError(f"{label} must be an object")
        location = raw_input.get("location")
        if location not in {"project", "repository"}:
            raise EpisodeBundleValidationError(
                f"{label}.location must be 'project' or 'repository'"
            )
        input_path = _audio_portable_path(
            raw_input.get("path"),
            label=f"{label}.path",
        )
        input_sha = raw_input.get("sha256")
        if not isinstance(input_sha, str) or not _HASH_RE.fullmatch(input_sha):
            raise EpisodeBundleValidationError(
                f"{label}.sha256 must be a full SHA-256 digest"
            )
        identity = (location, input_path)
        if identity in seen_inputs:
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: duplicate audio-stem input {identity!r}"
            )
        seen_inputs.add(identity)
        if location == "project":
            if input_path not in included_files:
                raise EpisodeBundleValidationError(
                    f"{label}: project input is missing from the portable bundle: "
                    f"{input_path!r}"
                )
            if sha256_for(input_path) != input_sha:
                raise EpisodeBundleValidationError(
                    f"{label}: project input is stale or changed: {input_path!r}"
                )

    entries = manifest.get("entries")
    if not isinstance(entries, dict) or set(entries) != set(_AUDIO_STEM_FILES):
        raise EpisodeBundleValidationError(
            f"{manifest_relative}: entries must contain exactly music and sfx"
        )
    manifest_seconds = _audio_positive_number(
        manifest.get("duration_seconds"),
        label=f"{manifest_relative} duration_seconds",
    )
    manifest_directory = str(PurePosixPath(manifest_relative).parent)
    seen_stems: set[str] = set()
    for name, (expected_track, expected_name) in _AUDIO_STEM_FILES.items():
        label = f"{manifest_relative} entries[{name!r}]"
        entry = entries[name]
        if not isinstance(entry, dict):
            raise EpisodeBundleValidationError(f"{label} must be an object")
        if entry.get("track") != expected_track or entry.get("kind") != name:
            raise EpisodeBundleValidationError(
                f"{label} must identify {name!r} on track {expected_track}"
            )
        stem_path = _audio_portable_path(
            entry.get("path"),
            label=f"{label}.path",
        )
        if stem_path != expected_name:
            raise EpisodeBundleValidationError(
                f"{label}.path must be {expected_name!r}"
            )
        stem_relative = f"{manifest_directory}/{stem_path}"
        if stem_relative in seen_stems:
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: stem entries select the same file"
            )
        seen_stems.add(stem_relative)
        if stem_relative not in included_files:
            raise EpisodeBundleValidationError(
                f"{label}: stem file is missing from the portable bundle: "
                f"{stem_relative!r}"
            )
        expected_sha = entry.get("sha256")
        if not isinstance(expected_sha, str) or not _HASH_RE.fullmatch(expected_sha):
            raise EpisodeBundleValidationError(
                f"{label}.sha256 must be a full SHA-256 digest"
            )
        if sha256_for(stem_relative) != expected_sha:
            raise EpisodeBundleValidationError(
                f"{label}: stem checksum changed: {stem_relative!r}"
            )
        stem_seconds = _audio_positive_number(
            entry.get("duration_seconds"),
            label=f"{label}.duration_seconds",
        )
        if abs(stem_seconds - manifest_seconds) > 0.075:
            raise EpisodeBundleValidationError(
                f"{label}: stem duration does not match the selected manifest"
            )
        for field in ("channels", "sample_rate"):
            field_value = entry.get(field)
            if (
                isinstance(field_value, bool)
                or not isinstance(field_value, int)
                or field_value <= 0
            ):
                raise EpisodeBundleValidationError(
                    f"{label}.{field} must be a positive integer"
                )


def _audit_local_audio_stems(root: Path, included_files: set[str]) -> None:
    def read_value(relative: str) -> Any:
        path = root.joinpath(*PurePosixPath(relative).parts)
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EpisodeBundleValidationError(
                f"cannot audit audio-stem metadata {relative}: {exc}"
            ) from exc

    def checksum(relative: str) -> str:
        return _sha256_file(root.joinpath(*PurePosixPath(relative).parts))

    _audit_audio_stem_selection(
        included_files=included_files,
        read_json_value=read_value,
        sha256_for=checksum,
    )


def _select_current_audio_stem_paths(
    root: Path,
    paths: list[Path],
    directory_paths: list[Path],
) -> tuple[list[Path], list[Path]]:
    """Exclude historical immutable stem sets from a portable episode.

    The selected set is already content-addressed and fully audited through
    ``current.json``. Carrying every older set doubles the bundle after each
    mix revision and provides no receiving-host value.
    """

    stems_root = root.joinpath(*PurePosixPath(_AUDIO_STEMS_ROOT).parts)
    pointer_path = root.joinpath(*PurePosixPath(_AUDIO_STEM_POINTER).parts)
    selected_fingerprint: str | None = None
    if pointer_path.is_file():
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pointer = None
        if isinstance(pointer, dict):
            candidate = pointer.get("fingerprint")
            if isinstance(candidate, str) and _HASH_RE.fullmatch(candidate):
                selected_fingerprint = candidate

    def selected(path: Path, *, is_directory: bool) -> bool:
        try:
            tail = path.relative_to(stems_root).parts
        except ValueError:
            return True
        if not tail:
            return True
        if not is_directory and tail == ("current.json",):
            return True
        return (
            selected_fingerprint is not None
            and tail[0] == selected_fingerprint
        )

    return (
        [path for path in paths if selected(path, is_directory=False)],
        [
            path
            for path in directory_paths
            if selected(path, is_directory=True)
        ],
    )


def _collect_sources(root: Path) -> tuple[list[_SourceFile], list[str]]:
    paths, directory_paths = _scan_tree(root)
    paths, directory_paths = _select_current_audio_stem_paths(
        root,
        paths,
        directory_paths,
    )
    portable_paths: dict[str, str] = {}
    relative_files: set[str] = set()
    for path in [*directory_paths, *paths]:
        relative = _validate_portable_relative(
            path.relative_to(root).as_posix(),
            label=f"project entry {path}",
        )
        collision_key = _portable_key(relative)
        previous = portable_paths.get(collision_key)
        if previous is not None and previous != relative:
            raise EpisodeBundleValidationError(
                "project paths collide on a case-insensitive or Unicode-normalizing "
                f"filesystem: {previous!r} and {relative!r}"
            )
        portable_paths[collision_key] = relative
        if path in paths:
            relative_files.add(relative)

    _audit_local_audio_stems(root, relative_files)
    _audit_json_metadata(root, paths, relative_files)

    sources: list[_SourceFile] = []
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        try:
            before = path.stat()
            checksum = _sha256_file(path)
            after = path.stat()
        except OSError as exc:
            raise EpisodeBundleValidationError(
                f"cannot read project file {path}: {exc}"
            ) from exc
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise EpisodeBundleValidationError(
                f"project file changed while it was being packaged: {path}"
            )
        sources.append(
            _SourceFile(
                relative=relative,
                path=path,
                size=after.st_size,
                sha256=checksum,
            )
        )
    directories = sorted(
        path.relative_to(root).as_posix() for path in directory_paths
    )
    return sources, directories


def _readme_bytes() -> bytes:
    return (
        "RabbitHole portable pre-Resolve episode bundle\n"
        "\n"
        "This ZIP contains project-local source inputs using POSIX relative paths.\n"
        "Validate checksums before restoring or editing it. Restore into a new,\n"
        "non-existent project directory; never merge it over another project.\n"
        "\n"
        "Intentionally excluded: .env secrets, renders, handoffs, caches, and\n"
        "generated Resolve build/queue/lock state. Recreate machine-local secrets\n"
        "and Resolve state on the destination computer.\n"
    ).encode("utf-8")


def _manifest_bytes(
    project_name: str,
    sources: list[_SourceFile],
    directories: list[str],
) -> bytes:
    manifest = {
        "archive_root": ARCHIVE_ROOT,
        "directories": directories,
        "exclusion_policy": {
            "caches": True,
            "env_secrets": True,
            "handoffs": True,
            "renders": True,
            "resolve_generated_state": True,
        },
        "files": [
            {
                "path": source.relative,
                "sha256": source.sha256,
                "size": source.size,
            }
            for source in sources
        ],
        "project_name": project_name,
        "schema_version": BUNDLE_SCHEMA_VERSION,
    }
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _checksum_bytes(
    sources: list[_SourceFile],
    manifest_bytes: bytes,
    readme_bytes: bytes,
) -> bytes:
    records = [
        *[(source.relative, source.sha256) for source in sources],
        (MANIFEST_PATH, _sha256_bytes(manifest_bytes)),
        (README_PATH, _sha256_bytes(readme_bytes)),
    ]
    records.sort(key=lambda item: item[0])
    return "".join(
        f"{checksum}  {relative}\n" for relative, checksum in records
    ).encode("utf-8")


def _zip_info(name: str, *, directory: bool) -> zipfile.ZipInfo:
    filename = name.rstrip("/") + "/" if directory else name
    info = zipfile.ZipInfo(filename=filename, date_time=_ZIP_TIMESTAMP)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (
        ((stat.S_IFDIR | 0o755) << 16) | 0x10
        if directory
        else (stat.S_IFREG | 0o644) << 16
    )
    return info


def _write_source(
    archive: zipfile.ZipFile,
    member: str,
    source: _SourceFile,
) -> None:
    info = _zip_info(member, directory=False)
    info.file_size = source.size
    with source.path.open("rb") as input_stream:
        with archive.open(info, mode="w", force_zip64=True) as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)


def package_episode(
    project_root: os.PathLike[str] | str,
    output_zip: os.PathLike[str] | str,
) -> dict[str, Any]:
    """Package a project-local episode into a deterministic, portable ZIP.

    The output is immutable: an existing path is never replaced.
    """

    root = _validate_exact_project_root(project_root)
    requested_output = Path(output_zip).expanduser()
    if requested_output.suffix.casefold() != ".zip":
        raise UnsafeEpisodeDestinationError("episode bundle output must end in .zip")
    if requested_output.is_symlink() or requested_output.exists():
        raise UnsafeEpisodeDestinationError(
            f"episode bundle output already exists; refusing overwrite: "
            f"{requested_output}"
        )
    output = requested_output.resolve(strict=False)
    if not output.name:
        raise UnsafeEpisodeDestinationError(
            f"episode bundle output must name an exact ZIP file: {output}"
        )
    if _is_within(output, root):
        raise UnsafeEpisodeDestinationError(
            f"episode bundle output must stay outside the project root: {output}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.parent.is_dir():
        raise UnsafeEpisodeDestinationError(
            f"episode bundle parent is not a directory: {output.parent}"
        )

    sources, directories = _collect_sources(root)
    manifest_bytes = _manifest_bytes(root.name, sources, directories)
    readme_bytes = _readme_bytes()
    checksums_bytes = _checksum_bytes(sources, manifest_bytes, readme_bytes)

    directory_members = {
        f"{ARCHIVE_ROOT}/",
        f"{ARCHIVE_ROOT}/{METADATA_DIRECTORY}/",
        *{
            f"{ARCHIVE_ROOT}/{relative}/"
            for relative in directories
        },
    }
    generated_files = {
        f"{ARCHIVE_ROOT}/{MANIFEST_PATH}": manifest_bytes,
        f"{ARCHIVE_ROOT}/{CHECKSUMS_PATH}": checksums_bytes,
        f"{ARCHIVE_ROOT}/{README_PATH}": readme_bytes,
    }
    source_members = {
        f"{ARCHIVE_ROOT}/{source.relative}": source for source in sources
    }

    try:
        with zipfile.ZipFile(
            output,
            mode="x",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as archive:
            for member in sorted(
                [*directory_members, *generated_files, *source_members]
            ):
                if member in directory_members:
                    archive.writestr(_zip_info(member, directory=True), b"")
                elif member in generated_files:
                    archive.writestr(
                        _zip_info(member, directory=False),
                        generated_files[member],
                    )
                else:
                    _write_source(archive, member, source_members[member])
        validation = validate_episode_bundle(output)
    except Exception:
        try:
            output.unlink()
        except FileNotFoundError:
            pass
        raise

    return {
        **validation,
        "action": "package_episode",
        "bundle_path": os.fspath(output),
    }


def _validate_zip_member_name(name: str, *, directory: bool) -> str:
    if not name or "\\" in name:
        raise EpisodeBundleValidationError(
            f"unsafe path in episode bundle: {name!r}"
        )
    stripped = name.rstrip("/") if directory else name
    prefix = f"{ARCHIVE_ROOT}/"
    if stripped == ARCHIVE_ROOT and directory:
        return ""
    if not stripped.startswith(prefix):
        raise EpisodeBundleValidationError(
            f"episode bundle member is outside {ARCHIVE_ROOT!r}: {name!r}"
        )
    relative = stripped[len(prefix) :]
    return _validate_portable_relative(
        relative,
        label=f"episode bundle member {name!r}",
        allow_metadata_directory=True,
    )


def _validated_infos(
    archive: zipfile.ZipFile,
) -> tuple[dict[str, zipfile.ZipInfo], set[str]]:
    infos: dict[str, zipfile.ZipInfo] = {}
    directories: set[str] = set()
    portable_names: dict[str, str] = {}
    for info in archive.infolist():
        if info.flag_bits & 0x1:
            raise EpisodeBundleValidationError(
                f"encrypted ZIP members are not supported: {info.filename!r}"
            )
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            raise EpisodeBundleValidationError(
                f"episode bundle cannot contain a symlink: {info.filename!r}"
            )
        is_directory = info.is_dir()
        file_type = stat.S_IFMT(mode)
        expected_type = stat.S_IFDIR if is_directory else stat.S_IFREG
        # ZIP writers commonly record only permission bits and omit the POSIX
        # file type. Treat that as a regular file/directory according to the ZIP
        # name, while still refusing explicit special-file types.
        if file_type not in {0, expected_type}:
            raise EpisodeBundleValidationError(
                f"episode bundle contains a non-regular entry: {info.filename!r}"
            )
        relative = _validate_zip_member_name(
            info.filename, directory=is_directory
        )
        normalized_member = (
            f"{ARCHIVE_ROOT}/{relative}" if relative else ARCHIVE_ROOT
        )
        if is_directory:
            normalized_member += "/"
        if normalized_member in infos or normalized_member in directories:
            raise EpisodeBundleValidationError(
                f"duplicate path in episode bundle: {info.filename!r}"
            )
        collision_key = _portable_key(normalized_member.rstrip("/"))
        previous = portable_names.get(collision_key)
        if previous is not None and previous != normalized_member:
            raise EpisodeBundleValidationError(
                "bundle paths collide on a case-insensitive or Unicode-normalizing "
                f"filesystem: {previous!r} and {normalized_member!r}"
            )
        portable_names[collision_key] = normalized_member
        if is_directory:
            directories.add(normalized_member)
        else:
            infos[normalized_member] = info
    if f"{ARCHIVE_ROOT}/" not in directories:
        raise EpisodeBundleValidationError(
            "episode bundle is missing its project root directory"
        )
    return infos, directories


def _read_small_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    label: str,
    maximum: int = 16 * 1024 * 1024,
) -> bytes:
    if info.file_size > maximum:
        raise EpisodeBundleValidationError(f"{label} is unexpectedly large")
    try:
        return archive.read(info)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise EpisodeBundleValidationError(f"cannot read {label}: {exc}") from exc


def _manifest_records(
    manifest: Any,
) -> tuple[str, list[dict[str, Any]], list[str]]:
    if not isinstance(manifest, dict):
        raise EpisodeBundleValidationError("bundle manifest must be a JSON object")
    if manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise EpisodeBundleValidationError(
            f"unsupported episode bundle schema: {manifest.get('schema_version')!r}"
        )
    if manifest.get("archive_root") != ARCHIVE_ROOT:
        raise EpisodeBundleValidationError("bundle manifest has an invalid archive root")
    project_name = manifest.get("project_name")
    if not isinstance(project_name, str) or not project_name:
        raise EpisodeBundleValidationError(
            "bundle manifest has an invalid project name"
        )
    files = manifest.get("files")
    directories = manifest.get("directories")
    if not isinstance(files, list) or not isinstance(directories, list):
        raise EpisodeBundleValidationError(
            "bundle manifest files/directories must be arrays"
        )
    normalized_files: list[dict[str, Any]] = []
    previous_path = ""
    for record in files:
        if not isinstance(record, dict):
            raise EpisodeBundleValidationError(
                "bundle manifest contains an invalid file record"
            )
        relative = _validate_portable_relative(
            record.get("path"),
            label="bundle manifest file path",
        ) if isinstance(record.get("path"), str) else ""
        size = record.get("size")
        checksum = record.get("sha256")
        if (
            not relative
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(checksum, str)
            or not _HASH_RE.fullmatch(checksum)
        ):
            raise EpisodeBundleValidationError(
                f"bundle manifest contains an invalid file record: {record!r}"
            )
        if previous_path and relative <= previous_path:
            raise EpisodeBundleValidationError(
                "bundle manifest file records must be unique and sorted"
            )
        previous_path = relative
        normalized_files.append(
            {"path": relative, "size": size, "sha256": checksum}
        )

    normalized_directories: list[str] = []
    previous_path = ""
    for raw in directories:
        if not isinstance(raw, str):
            raise EpisodeBundleValidationError(
                "bundle manifest contains an invalid directory record"
            )
        relative = _validate_portable_relative(
            raw,
            label="bundle manifest directory path",
        )
        if previous_path and relative <= previous_path:
            raise EpisodeBundleValidationError(
                "bundle manifest directories must be unique and sorted"
            )
        previous_path = relative
        normalized_directories.append(relative)
    return project_name, normalized_files, normalized_directories


def _parse_checksums(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EpisodeBundleValidationError(
            f"bundle checksums are not UTF-8: {exc}"
        ) from exc
    records: dict[str, str] = {}
    previous = ""
    for line in text.splitlines():
        if not line:
            continue
        if len(line) < 67 or line[64:66] != "  ":
            raise EpisodeBundleValidationError(
                f"invalid checksum line: {line!r}"
            )
        checksum, relative = line[:64], line[66:]
        if not _HASH_RE.fullmatch(checksum):
            raise EpisodeBundleValidationError(
                f"invalid checksum digest for {relative!r}"
            )
        normalized = _validate_portable_relative(
            relative,
            label="bundle checksum path",
            allow_metadata_directory=True,
        )
        if previous and normalized <= previous:
            raise EpisodeBundleValidationError(
                "bundle checksum records must be unique and sorted"
            )
        previous = normalized
        records[normalized] = checksum
    return records


def _hash_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
) -> str:
    digest = hashlib.sha256()
    try:
        with archive.open(info, mode="r") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise EpisodeBundleValidationError(
            f"cannot read bundle member {info.filename!r}: {exc}"
        ) from exc
    return digest.hexdigest()


def _audit_bundled_json_metadata(
    archive: zipfile.ZipFile,
    infos: dict[str, zipfile.ZipInfo],
    file_records: list[dict[str, Any]],
) -> None:
    included_files = {record["path"] for record in file_records}
    for record in file_records:
        relative = record["path"]
        if Path(relative).suffix.casefold() != ".json":
            continue
        info = infos[f"{ARCHIVE_ROOT}/{relative}"]
        try:
            value = json.loads(
                _read_small_member(
                    archive,
                    info,
                    label=f"JSON metadata {relative!r}",
                    maximum=64 * 1024 * 1024,
                ).decode("utf-8-sig")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EpisodeBundleValidationError(
                f"cannot audit JSON metadata {relative}: {exc}"
            ) from exc
        _audit_json_value(
            value,
            metadata_path=relative,
            project_root=None,
            included_files=included_files,
        )


def _audit_bundled_audio_stems(
    archive: zipfile.ZipFile,
    infos: dict[str, zipfile.ZipInfo],
    file_records: list[dict[str, Any]],
) -> None:
    records_by_path = {record["path"]: record for record in file_records}
    included_files = set(records_by_path)

    def read_value(relative: str) -> Any:
        member = f"{ARCHIVE_ROOT}/{relative}"
        try:
            return json.loads(
                _read_small_member(
                    archive,
                    infos[member],
                    label=f"audio-stem metadata {relative!r}",
                    maximum=64 * 1024 * 1024,
                ).decode("utf-8-sig")
            )
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EpisodeBundleValidationError(
                f"cannot audit audio-stem metadata {relative}: {exc}"
            ) from exc

    def checksum(relative: str) -> str:
        record = records_by_path.get(relative)
        if record is None:
            raise EpisodeBundleValidationError(
                f"audio-stem file is missing from bundle manifest: {relative!r}"
            )
        # validate_episode_bundle has already compared this manifest digest with
        # the exact ZIP member bytes before invoking the semantic audit.
        return str(record["sha256"])

    _audit_audio_stem_selection(
        included_files=included_files,
        read_json_value=read_value,
        sha256_for=checksum,
    )


def validate_episode_bundle(
    bundle: os.PathLike[str] | str,
) -> dict[str, Any]:
    """Verify paths, schema, exact membership, sizes, and every checksum."""

    raw = Path(bundle).expanduser()
    if raw.is_symlink():
        raise EpisodeBundleValidationError(
            f"episode bundle cannot be a symlink: {raw}"
        )
    source = raw.resolve(strict=False)
    if not source.is_file():
        raise EpisodeBundleValidationError(
            f"episode bundle does not exist: {source}"
        )
    try:
        archive_context = zipfile.ZipFile(source, mode="r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise EpisodeBundleValidationError(
            f"episode bundle is not a readable ZIP: {source}: {exc}"
        ) from exc

    with archive_context as archive:
        infos, actual_directories = _validated_infos(archive)
        manifest_member = f"{ARCHIVE_ROOT}/{MANIFEST_PATH}"
        checksums_member = f"{ARCHIVE_ROOT}/{CHECKSUMS_PATH}"
        readme_member = f"{ARCHIVE_ROOT}/{README_PATH}"
        for required in (manifest_member, checksums_member, readme_member):
            if required not in infos:
                raise EpisodeBundleValidationError(
                    f"episode bundle is missing required metadata: {required}"
                )
        try:
            manifest = json.loads(
                _read_small_member(
                    archive, infos[manifest_member], label="bundle manifest"
                ).decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EpisodeBundleValidationError(
                f"bundle manifest is invalid JSON: {exc}"
            ) from exc
        project_name, file_records, directories = _manifest_records(manifest)
        checksums = _parse_checksums(
            _read_small_member(
                archive, infos[checksums_member], label="bundle checksums"
            )
        )

        expected_files = {
            manifest_member,
            checksums_member,
            readme_member,
            *{
                f"{ARCHIVE_ROOT}/{record['path']}" for record in file_records
            },
        }
        expected_directories = {
            f"{ARCHIVE_ROOT}/",
            f"{ARCHIVE_ROOT}/{METADATA_DIRECTORY}/",
            *{
                f"{ARCHIVE_ROOT}/{relative}/" for relative in directories
            },
        }
        if set(infos) != expected_files:
            missing = sorted(expected_files - set(infos))
            extra = sorted(set(infos) - expected_files)
            raise EpisodeBundleValidationError(
                f"bundle membership mismatch; missing={missing}, extra={extra}"
            )
        if actual_directories != expected_directories:
            missing = sorted(expected_directories - actual_directories)
            extra = sorted(actual_directories - expected_directories)
            raise EpisodeBundleValidationError(
                f"bundle directory membership mismatch; missing={missing}, extra={extra}"
            )

        expected_checksum_paths = {
            MANIFEST_PATH,
            README_PATH,
            *{record["path"] for record in file_records},
        }
        if set(checksums) != expected_checksum_paths:
            raise EpisodeBundleValidationError(
                "bundle checksum membership does not match the manifest"
            )

        total_bytes = 0
        records_by_path = {record["path"]: record for record in file_records}
        for relative in sorted(expected_checksum_paths):
            member = f"{ARCHIVE_ROOT}/{relative}"
            info = infos[member]
            record = records_by_path.get(relative)
            if record is not None and info.file_size != record["size"]:
                raise EpisodeBundleValidationError(
                    f"bundle size mismatch for {relative!r}"
                )
            observed = _hash_zip_member(archive, info)
            if observed != checksums[relative]:
                raise EpisodeBundleValidationError(
                    f"bundle checksum mismatch for {relative!r}"
                )
            if record is not None and observed != record["sha256"]:
                raise EpisodeBundleValidationError(
                    f"manifest checksum mismatch for {relative!r}"
                )
            if record is not None:
                total_bytes += info.file_size

        _audit_bundled_audio_stems(archive, infos, file_records)
        _audit_bundled_json_metadata(archive, infos, file_records)

    return {
        "valid": True,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "project_name": project_name,
        "file_count": len(file_records),
        "directory_count": len(directories),
        "source_bytes": total_bytes,
        "bundle_sha256": _sha256_file(source),
    }


def _validate_restore_destination(
    destination: os.PathLike[str] | str,
) -> Path:
    raw = Path(destination).expanduser()
    if raw.is_symlink() or raw.exists():
        raise UnsafeEpisodeDestinationError(
            f"restore destination already exists; refusing overwrite: {raw}"
        )
    target = raw.resolve(strict=False)
    anchor = Path(target.anchor).resolve(strict=False)
    home = Path.home().resolve(strict=False)
    if _same_path(target, anchor) or _same_path(target, home) or not target.name:
        raise UnsafeEpisodeDestinationError(
            f"restore destination is too broad: {target}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.parent.is_dir():
        raise UnsafeEpisodeDestinationError(
            f"restore parent is not a directory: {target.parent}"
        )
    return target


def _extract_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    written = 0
    try:
        with archive.open(info, mode="r") as input_stream:
            with target.open("xb") as output_stream:
                while True:
                    block = input_stream.read(1024 * 1024)
                    if not block:
                        break
                    output_stream.write(block)
                    digest.update(block)
                    written += len(block)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise EpisodeBundleValidationError(
            f"cannot restore bundle member {info.filename!r}: {exc}"
        ) from exc
    if written != expected_size or digest.hexdigest() != expected_sha256:
        raise EpisodeBundleValidationError(
            f"restored member failed checksum verification: {info.filename!r}"
        )


def restore_episode_bundle(
    bundle: os.PathLike[str] | str,
    destination: os.PathLike[str] | str,
) -> dict[str, Any]:
    """Restore a validated bundle into one new project directory."""

    validation = validate_episode_bundle(bundle)
    source = _canonical(bundle)
    target = _validate_restore_destination(destination)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent)
    )
    committed = False
    try:
        with zipfile.ZipFile(source, mode="r") as archive:
            infos, _ = _validated_infos(archive)
            manifest_member = f"{ARCHIVE_ROOT}/{MANIFEST_PATH}"
            checksums_member = f"{ARCHIVE_ROOT}/{CHECKSUMS_PATH}"
            manifest = json.loads(archive.read(infos[manifest_member]).decode("utf-8"))
            _, records, directories = _manifest_records(manifest)
            checksums = _parse_checksums(archive.read(infos[checksums_member]))
            sizes = {record["path"]: record["size"] for record in records}

            for relative in directories:
                restored_directory = (
                    staging / Path(*PurePosixPath(relative).parts)
                ).resolve(strict=False)
                if not _is_within(restored_directory, staging):
                    raise EpisodeBundleValidationError(
                        f"bundle directory escapes restore staging: {relative!r}"
                    )
                restored_directory.mkdir(parents=True, exist_ok=False)

            restore_files = [
                *[record["path"] for record in records],
                MANIFEST_PATH,
                CHECKSUMS_PATH,
                README_PATH,
            ]
            for relative in sorted(restore_files):
                normalized = _validate_portable_relative(
                    relative,
                    label="restore member",
                    allow_metadata_directory=True,
                )
                restored_file = (
                    staging / Path(*PurePosixPath(normalized).parts)
                ).resolve(strict=False)
                if not _is_within(restored_file, staging):
                    raise EpisodeBundleValidationError(
                        f"bundle member escapes restore staging: {relative!r}"
                    )
                member = f"{ARCHIVE_ROOT}/{normalized}"
                info = infos[member]
                if normalized == CHECKSUMS_PATH:
                    expected_size = info.file_size
                    expected_sha256 = _hash_zip_member(archive, info)
                else:
                    expected_size = sizes.get(normalized, info.file_size)
                    expected_sha256 = checksums[normalized]
                _extract_member(
                    archive,
                    info,
                    restored_file,
                    expected_size=expected_size,
                    expected_sha256=expected_sha256,
                )

        if target.exists() or target.is_symlink():
            raise UnsafeEpisodeDestinationError(
                f"restore destination appeared during extraction; refusing overwrite: "
                f"{target}"
            )
        staging.rename(target)
        committed = True
    finally:
        if not committed and staging.exists():
            shutil.rmtree(staging)

    return {
        **validation,
        "action": "restore_episode_bundle",
        "restored_path": os.fspath(target),
    }


__all__ = [
    "ARCHIVE_ROOT",
    "BUNDLE_SCHEMA_VERSION",
    "CHECKSUMS_PATH",
    "EpisodeBundleError",
    "EpisodeBundleValidationError",
    "MANIFEST_PATH",
    "METADATA_DIRECTORY",
    "README_PATH",
    "UnsafeEpisodeDestinationError",
    "package_episode",
    "restore_episode_bundle",
    "validate_episode_bundle",
]

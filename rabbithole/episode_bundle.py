"""Portable, deterministic episode bundles for pre-Resolve project transfer.

This module deliberately performs no Resolve or network operations.  It
packages only project-local inputs and rejects machine-specific metadata so
that a restored episode has the same relative paths on Windows and macOS.  A
small, non-interactive Git probe records the repository contract when the
episode lives inside a checkout; it never mutates the repository.
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
import subprocess
import tempfile
from typing import Any, Callable
import unicodedata
import zipfile


BUNDLE_SCHEMA_VERSION = "rabbithole-episode-bundle.v2"
REPOSITORY_CONTRACT_SCHEMA_VERSION = "rabbithole-repository-contract.v1"
ARCHIVE_ROOT = "project"
METADATA_DIRECTORY = ".rabbithole-bundle"
MANIFEST_PATH = f"{METADATA_DIRECTORY}/manifest.json"
CHECKSUMS_PATH = f"{METADATA_DIRECTORY}/checksums.sha256"
README_PATH = f"{METADATA_DIRECTORY}/README.txt"

_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
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
        ".capturework",
        ".cardwork",
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
        "review-approvals",
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
_AUDIO_BAKES_ROOT = "resolve/audio-bakes"
_AUDIO_BAKE_SCHEMA = "resolve-audio-gain-bake.v1"
_AUDIO_BAKE_GENERATOR = "resolve-audio-gain-bake.v1"
_AUDIO_BAKE_MANIFEST_NAME = "manifest.json"
_RESOLVE_CURRENT_POINTER = "resolve/current.json"


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


def _run_git(cwd: Path, *arguments: str) -> bytes | None:
    """Run one read-only Git query without inheriting repository redirects.

    Git worktree discovery is optional bundle metadata.  Missing Git, dubious
    ownership, an invalid checkout, and a bounded timeout therefore return
    ``None`` instead of making an otherwise portable episode unpackageable.
    The environment cleanup prevents caller-provided ``GIT_DIR``/
    ``GIT_WORK_TREE`` values from redirecting the probe to another checkout;
    disabling fsmonitor also prevents ``git status`` from launching a
    repository-configured monitor executable.
    """

    environment = os.environ.copy()
    for name in (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    ):
        environment.pop(name, None)
    for name in list(environment):
        if name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(name, None)
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=",
                "-C",
                os.fspath(cwd),
                *arguments,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _unavailable_repository_contract() -> dict[str, Any]:
    return {
        "available": False,
        "head_commit": None,
        "project_path": None,
        "reproducible": False,
        "reproducible_revision": None,
        "schema_version": REPOSITORY_CONTRACT_SCHEMA_VERSION,
        "vcs": None,
        "worktree_state": "unavailable",
    }


def _discover_repository_contract(project_root: Path) -> dict[str, Any]:
    """Describe the checkout containing ``project_root`` without mutating it.

    A dirty checkout deliberately records its HEAD only as context.  The
    ``reproducible_revision`` field remains null so downstream tools cannot
    silently present that commit as the generator state that made the bundle.
    """

    top_level_output = _run_git(project_root, "rev-parse", "--show-toplevel")
    if top_level_output is None:
        return _unavailable_repository_contract()
    try:
        top_level_text = top_level_output.decode("utf-8").strip()
    except UnicodeDecodeError:
        return _unavailable_repository_contract()
    if not top_level_text:
        return _unavailable_repository_contract()
    repository_root = Path(top_level_text).resolve(strict=False)
    if not repository_root.is_dir() or not _is_within(project_root, repository_root):
        return _unavailable_repository_contract()

    relative_project = project_root.relative_to(repository_root).as_posix()
    project_path = (
        "."
        if relative_project == "."
        else _validate_portable_relative(
            relative_project,
            label="repository-relative project path",
        )
    )
    head_output = _run_git(repository_root, "rev-parse", "--verify", "HEAD")
    head_commit: str | None = None
    if head_output is not None:
        try:
            candidate = head_output.decode("ascii").strip().casefold()
        except UnicodeDecodeError:
            candidate = ""
        if _GIT_OBJECT_RE.fullmatch(candidate):
            head_commit = candidate

    status_output = _run_git(
        repository_root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=normal",
        "--ignore-submodules=none",
    )
    if head_commit is None:
        worktree_state = "unborn"
    elif status_output is None:
        worktree_state = "unknown"
    elif status_output:
        worktree_state = "dirty"
    else:
        worktree_state = "clean"
    reproducible = worktree_state == "clean"
    return {
        "available": True,
        "head_commit": head_commit,
        "project_path": project_path,
        "reproducible": reproducible,
        "reproducible_revision": head_commit if reproducible else None,
        "schema_version": REPOSITORY_CONTRACT_SCHEMA_VERSION,
        "vcs": "git",
        "worktree_state": worktree_state,
    }


def _validate_repository_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EpisodeBundleValidationError(
            "bundle manifest has no repository contract"
        )
    expected_keys = {
        "available",
        "head_commit",
        "project_path",
        "reproducible",
        "reproducible_revision",
        "schema_version",
        "vcs",
        "worktree_state",
    }
    if set(value) != expected_keys:
        raise EpisodeBundleValidationError(
            "bundle repository contract has unexpected or missing fields"
        )
    if value.get("schema_version") != REPOSITORY_CONTRACT_SCHEMA_VERSION:
        raise EpisodeBundleValidationError(
            "bundle repository contract has an unsupported schema_version"
        )
    available = value.get("available")
    reproducible = value.get("reproducible")
    if not isinstance(available, bool) or not isinstance(reproducible, bool):
        raise EpisodeBundleValidationError(
            "bundle repository contract flags must be booleans"
        )
    vcs = value.get("vcs")
    state = value.get("worktree_state")
    head = value.get("head_commit")
    revision = value.get("reproducible_revision")
    project_path = value.get("project_path")
    if available:
        if vcs != "git" or state not in {"clean", "dirty", "unknown", "unborn"}:
            raise EpisodeBundleValidationError(
                "bundle repository contract has invalid Git state"
            )
        if not isinstance(project_path, str):
            raise EpisodeBundleValidationError(
                "bundle repository contract has no project_path"
            )
        if project_path != ".":
            _validate_portable_relative(
                project_path,
                label="bundle repository project_path",
            )
        if head is not None and (
            not isinstance(head, str) or not _GIT_OBJECT_RE.fullmatch(head)
        ):
            raise EpisodeBundleValidationError(
                "bundle repository contract has an invalid head_commit"
            )
        if state == "unborn" and head is not None:
            raise EpisodeBundleValidationError(
                "an unborn repository contract cannot have a head_commit"
            )
        if state != "unborn" and head is None:
            raise EpisodeBundleValidationError(
                "bundle repository contract Git state requires a head_commit"
            )
    else:
        if (
            vcs is not None
            or state != "unavailable"
            or head is not None
            or project_path is not None
        ):
            raise EpisodeBundleValidationError(
                "unavailable repository contract contains Git metadata"
            )
    if reproducible:
        if not available or state != "clean" or revision != head:
            raise EpisodeBundleValidationError(
                "reproducible repository contract must name its clean HEAD"
            )
    elif revision is not None:
        raise EpisodeBundleValidationError(
            "non-reproducible repository contract must not claim a revision"
        )
    elif available and state == "clean":
        raise EpisodeBundleValidationError(
            "clean repository contract must claim its reproducible revision"
        )
    return dict(value)


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


def _load_current_audio_bake_selection(root: Path) -> dict[str, dict[str, Any]]:
    """Load gain-bakes reachable from the current Resolve plan.

    ``resolve/current.json`` and generated build plans are intentionally not
    transferred.  They are nevertheless the authoritative local reachability
    roots used while packaging immutable ``resolve/audio-bakes`` media.
    """

    bakes_root = root.joinpath(*PurePosixPath(_AUDIO_BAKES_ROOT).parts)
    if not bakes_root.is_dir():
        return {}
    pointer_path = root.joinpath(*PurePosixPath(_RESOLVE_CURRENT_POINTER).parts)
    if not pointer_path.is_file():
        return {}
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EpisodeBundleValidationError(
            f"cannot audit current Resolve plan pointer {_RESOLVE_CURRENT_POINTER}: "
            f"{exc}"
        ) from exc
    pointer = _audio_json_object(pointer, label=_RESOLVE_CURRENT_POINTER)
    plan_relative = _audio_portable_path(
        pointer.get("plan_path"),
        label=f"{_RESOLVE_CURRENT_POINTER} plan_path",
    )
    plan_parts = PurePosixPath(plan_relative).parts
    if (
        len(plan_parts) != 4
        or plan_parts[:2] != ("resolve", "builds")
        or plan_parts[-1] != "resolve-plan.v1.json"
    ):
        raise EpisodeBundleValidationError(
            f"{_RESOLVE_CURRENT_POINTER}: plan_path must select one generated "
            "resolve/builds/<build-id>/resolve-plan.v1.json"
        )
    plan_path = root.joinpath(*plan_parts)
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EpisodeBundleValidationError(
            f"cannot audit current Resolve plan {plan_relative}: {exc}"
        ) from exc
    plan = _audio_json_object(plan, label=plan_relative)
    raw_audio = plan.get("audio")
    if not isinstance(raw_audio, list):
        raise EpisodeBundleValidationError(
            f"{plan_relative}: audio must be an array"
        )
    fps = plan.get("fps")
    if isinstance(fps, bool) or not isinstance(fps, int) or fps <= 0:
        raise EpisodeBundleValidationError(
            f"{plan_relative}: fps must be a positive integer"
        )

    selected: dict[str, dict[str, Any]] = {}
    for index, raw_clip in enumerate(raw_audio):
        label = f"{plan_relative} audio[{index}]"
        if not isinstance(raw_clip, dict):
            raise EpisodeBundleValidationError(f"{label} must be an object")
        raw_media_path = raw_clip.get("media_path")
        has_bake_metadata = any(
            key in raw_clip
            for key in (
                "gain_baked_db",
                "gain_bake_fingerprint",
                "gain_bake_source_sha256",
            )
        )
        is_bake_path = (
            isinstance(raw_media_path, str)
            and raw_media_path.startswith(f"{_AUDIO_BAKES_ROOT}/")
        )
        if not has_bake_metadata and not is_bake_path:
            continue
        if not has_bake_metadata or not is_bake_path:
            raise EpisodeBundleValidationError(
                f"{label}: gain-bake metadata and media_path must be present together"
            )
        fingerprint = raw_clip.get("gain_bake_fingerprint")
        if not isinstance(fingerprint, str) or not _HASH_RE.fullmatch(fingerprint):
            raise EpisodeBundleValidationError(
                f"{label}: gain_bake_fingerprint must be a full SHA-256 digest"
            )
        media_path = _audio_portable_path(
            raw_media_path,
            label=f"{label}.media_path",
        )
        media_parts = PurePosixPath(media_path).parts
        if (
            len(media_parts) != 4
            or media_parts[:2] != ("resolve", "audio-bakes")
            or media_parts[2] != fingerprint
        ):
            raise EpisodeBundleValidationError(
                f"{label}: media_path must select its immutable gain-bake "
                f"directory {_AUDIO_BAKES_ROOT}/{fingerprint}/"
            )
        if raw_clip.get("path_kind") != "project-relative":
            raise EpisodeBundleValidationError(
                f"{label}: gain-bake media must be project-relative"
            )
        if raw_clip.get("gain_bake_source_path_kind") != "project-relative":
            raise EpisodeBundleValidationError(
                f"{label}: gain-bake source must be project-relative for transfer"
            )
        source_path = _audio_portable_path(
            raw_clip.get("gain_bake_source_media_path"),
            label=f"{label}.gain_bake_source_media_path",
        )
        for field in ("sha256", "gain_bake_source_sha256"):
            digest = raw_clip.get(field)
            if not isinstance(digest, str) or not _HASH_RE.fullmatch(digest):
                raise EpisodeBundleValidationError(
                    f"{label}.{field} must be a full SHA-256 digest"
                )
        for field in (
            "source_start_frame",
            "gain_bake_source_start_frame",
            "duration_frames",
            "channels",
            "source_sample_rate",
        ):
            value = raw_clip.get(field)
            minimum = 1 if field in {"duration_frames", "channels", "source_sample_rate"} else 0
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise EpisodeBundleValidationError(
                    f"{label}.{field} must be an integer >= {minimum}"
                )
        gain_db = raw_clip.get("gain_db")
        baked_gain = raw_clip.get("gain_baked_db")
        if (
            isinstance(gain_db, bool)
            or not isinstance(gain_db, (int, float))
            or not math.isfinite(float(gain_db))
            or abs(float(gain_db)) > 1e-9
        ):
            raise EpisodeBundleValidationError(
                f"{label}: gain-baked media must be authored at unity"
            )
        if (
            isinstance(baked_gain, bool)
            or not isinstance(baked_gain, (int, float))
            or not math.isfinite(float(baked_gain))
        ):
            raise EpisodeBundleValidationError(
                f"{label}.gain_baked_db must be finite"
            )
        if raw_clip.get("gain_bake_generator_version") != _AUDIO_BAKE_GENERATOR:
            raise EpisodeBundleValidationError(
                f"{label}: unsupported gain-bake generator"
            )
        record = {
            "fingerprint": fingerprint,
            "media_path": media_path,
            "sha256": raw_clip["sha256"],
            "source_media_path": source_path,
            "source_path_kind": raw_clip["gain_bake_source_path_kind"],
            "source_sha256": raw_clip["gain_bake_source_sha256"],
            "source_start_frame": raw_clip["gain_bake_source_start_frame"],
            "duration_frames": raw_clip["duration_frames"],
            "fps": fps,
            "gain_db": float(baked_gain),
            "generator_version": raw_clip["gain_bake_generator_version"],
            "channels": raw_clip["channels"],
            "sample_rate": raw_clip["source_sample_rate"],
        }
        previous = selected.get(fingerprint)
        if previous is not None and previous != record:
            raise EpisodeBundleValidationError(
                f"{plan_relative}: fingerprint {fingerprint} selects conflicting "
                "gain-bake contracts"
            )
        selected[fingerprint] = record
    return selected


def _select_current_audio_bake_paths(
    root: Path,
    paths: list[Path],
    directory_paths: list[Path],
    selected: dict[str, dict[str, Any]],
) -> tuple[list[Path], list[Path]]:
    """Exclude every gain-bake directory not referenced by the current plan."""

    bakes_root = root.joinpath(*PurePosixPath(_AUDIO_BAKES_ROOT).parts)

    def reachable(path: Path) -> bool:
        try:
            tail = path.relative_to(bakes_root).parts
        except ValueError:
            return True
        if not tail:
            return True
        return tail[0] in selected

    return (
        [path for path in paths if reachable(path)],
        [path for path in directory_paths if reachable(path)],
    )


def _audit_audio_bake_selection(
    selected: dict[str, dict[str, Any]],
    *,
    included_files: set[str],
    read_json_value: Callable[[str], Any],
    sha256_for: Callable[[str], str],
) -> None:
    """Validate selected bake manifests, output bytes, and original sources."""

    for fingerprint, record in sorted(selected.items()):
        bake_directory = f"{_AUDIO_BAKES_ROOT}/{fingerprint}"
        manifest_relative = f"{bake_directory}/{_AUDIO_BAKE_MANIFEST_NAME}"
        if manifest_relative not in included_files:
            raise EpisodeBundleValidationError(
                f"current Resolve plan gain-bake manifest is missing from the "
                f"portable bundle: {manifest_relative!r}"
            )
        manifest = _audio_json_object(
            read_json_value(manifest_relative),
            label=manifest_relative,
        )
        if manifest.get("schema_version") != _AUDIO_BAKE_SCHEMA:
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: unsupported gain-bake schema"
            )
        if manifest.get("generator_version") != _AUDIO_BAKE_GENERATOR:
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: unsupported gain-bake generator"
            )
        if manifest.get("fingerprint") != fingerprint:
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: fingerprint does not match its directory"
            )
        contract = _audio_json_object(
            manifest.get("contract"),
            label=f"{manifest_relative} contract",
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
        expected_contract = {
            "generator_version": record["generator_version"],
            "source_sha256": record["source_sha256"],
            "gain_db": record["gain_db"],
            "source_start_frame": record["source_start_frame"],
            "duration_frames": record["duration_frames"],
            "fps": record["fps"],
        }
        for field, expected in expected_contract.items():
            if contract.get(field) != expected:
                raise EpisodeBundleValidationError(
                    f"{manifest_relative}: contract {field} does not match the "
                    "current Resolve plan"
                )
        contract_source_media_path = contract.get("source_media_path")
        contract_source_path_kind = contract.get("source_path_kind")
        if (
            contract_source_media_path is not None
            or contract_source_path_kind is not None
        ) and (
            contract_source_media_path != record["source_media_path"]
            or contract_source_path_kind != record["source_path_kind"]
        ):
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: contract source identity does not match "
                "the current Resolve plan"
            )

        source = _audio_json_object(
            manifest.get("source"),
            label=f"{manifest_relative} source",
        )
        if (
            source.get("path_kind") != record["source_path_kind"]
            or source.get("media_path") != record["source_media_path"]
            or source.get("sha256") != record["source_sha256"]
        ):
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: source does not match the current Resolve plan"
            )
        source_relative = record["source_media_path"]
        if source_relative not in included_files:
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: original gain-bake source is missing from "
                f"the portable bundle: {source_relative!r}"
            )
        if sha256_for(source_relative) != record["source_sha256"]:
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: original gain-bake source is stale or changed: "
                f"{source_relative!r}"
            )

        output = _audio_json_object(
            manifest.get("output"),
            label=f"{manifest_relative} output",
        )
        output_name = _audio_portable_path(
            output.get("path"),
            label=f"{manifest_relative} output.path",
        )
        if len(PurePosixPath(output_name).parts) != 1:
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: output.path must be one filename"
            )
        output_relative = f"{bake_directory}/{output_name}"
        if (
            output_relative != record["media_path"]
            or output.get("sha256") != record["sha256"]
        ):
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: output does not match the current Resolve plan"
            )
        if output_relative not in included_files:
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: gain-baked WAV is missing from the portable "
                f"bundle: {output_relative!r}"
            )
        if sha256_for(output_relative) != record["sha256"]:
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: gain-baked WAV checksum changed: "
                f"{output_relative!r}"
            )

        contract_output = _audio_json_object(
            contract.get("output"),
            label=f"{manifest_relative} contract.output",
        )
        for field, expected in (
            ("channels", record["channels"]),
            ("sample_rate", record["sample_rate"]),
        ):
            if contract_output.get(field) != expected or output.get(field) != expected:
                raise EpisodeBundleValidationError(
                    f"{manifest_relative}: output {field} is inconsistent"
                )
        if contract_output.get("sample_width") != 2:
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: output sample_width is inconsistent"
            )
        sample_count = contract_output.get("sample_count")
        copied = output.get("copied_sample_count")
        padded = output.get("padded_sample_count")
        if (
            isinstance(sample_count, bool)
            or not isinstance(sample_count, int)
            or sample_count <= 0
            or output.get("sample_count") != sample_count
            or isinstance(copied, bool)
            or not isinstance(copied, int)
            or copied < 0
            or isinstance(padded, bool)
            or not isinstance(padded, int)
            or padded < 0
            or copied + padded != sample_count
            or output.get("codec") != "pcm_s16le"
        ):
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: output sample contract is inconsistent"
            )
        expected_samples = (
            (
                2
                * (record["source_start_frame"] + record["duration_frames"])
                * record["sample_rate"]
                + record["fps"]
            )
            // (2 * record["fps"])
            - (
                2
                * record["source_start_frame"]
                * record["sample_rate"]
                + record["fps"]
            )
            // (2 * record["fps"])
        )
        if sample_count != expected_samples:
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: output sample count does not exactly match "
                "the current Resolve plan frame range"
            )
        actual_bake_files = {
            relative
            for relative in included_files
            if PurePosixPath(relative).parent.as_posix() == bake_directory
        }
        expected_bake_files = {manifest_relative, output_relative}
        if actual_bake_files != expected_bake_files:
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: immutable gain-bake directory has unexpected "
                "or missing files"
            )


def _audit_local_audio_bakes(
    root: Path,
    included_files: set[str],
    selected: dict[str, dict[str, Any]],
) -> None:
    def read_value(relative: str) -> Any:
        path = root.joinpath(*PurePosixPath(relative).parts)
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EpisodeBundleValidationError(
                f"cannot audit gain-bake metadata {relative}: {exc}"
            ) from exc

    def checksum(relative: str) -> str:
        return _sha256_file(root.joinpath(*PurePosixPath(relative).parts))

    _audit_audio_bake_selection(
        selected,
        included_files=included_files,
        read_json_value=read_value,
        sha256_for=checksum,
    )


def _collect_sources(root: Path) -> tuple[list[_SourceFile], list[str]]:
    paths, directory_paths = _scan_tree(root)
    paths, directory_paths = _select_current_audio_stem_paths(
        root,
        paths,
        directory_paths,
    )
    selected_audio_bakes = _load_current_audio_bake_selection(root)
    paths, directory_paths = _select_current_audio_bake_paths(
        root,
        paths,
        directory_paths,
        selected_audio_bakes,
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
    _audit_local_audio_bakes(root, relative_files, selected_audio_bakes)
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


def _readme_bytes(repository_contract: dict[str, Any]) -> bytes:
    if repository_contract["reproducible"]:
        repository_note = (
            "Repository contract: clean Git revision\n"
            f"{repository_contract['reproducible_revision']}. Check out that exact\n"
            "revision on the destination computer before running prepare.\n"
        )
    elif repository_contract["available"]:
        head = repository_contract["head_commit"] or "no committed HEAD"
        repository_note = (
            "WARNING: the source Git worktree was not clean and reproducible.\n"
            f"Recorded HEAD {head} is context only; it does NOT reproduce the\n"
            "repository code that created this bundle. Commit or stash repository\n"
            "changes and create a new bundle for a reproducible editor transfer.\n"
        )
    else:
        repository_note = (
            "WARNING: no containing Git checkout was available. This bundle has no\n"
            "reproducible repository revision; supply and verify the matching\n"
            "RabbitHole repository separately before running prepare.\n"
        )
    return (
        "RabbitHole portable pre-Resolve episode bundle\n"
        "\n"
        "This ZIP contains project-local source inputs using POSIX relative paths.\n"
        "Validate checksums before restoring or editing it. Restore into a new,\n"
        "non-existent project directory; never merge it over another project.\n"
        "\n"
        "Intentionally excluded: .env secrets, renders, handoffs, caches, and\n"
        "generated Resolve build/queue/lock/review state. Transient .cardwork and\n"
        ".capturework directories are also excluded; packaging fails if included\n"
        "metadata references an input there. Recreate machine-local secrets and\n"
        "Resolve state on the destination computer.\n"
        "\n"
        + repository_note
    ).encode("utf-8")


def _manifest_bytes(
    project_name: str,
    sources: list[_SourceFile],
    directories: list[str],
    repository_contract: dict[str, Any],
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
            "transient_asset_workdirs": True,
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
        "repository_contract": repository_contract,
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

    repository_contract = _discover_repository_contract(root)
    sources, directories = _collect_sources(root)
    confirmed_contract = _discover_repository_contract(root)
    if repository_contract != confirmed_contract:
        raise EpisodeBundleValidationError(
            "repository HEAD or worktree state changed while the episode was "
            "being packaged"
        )
    manifest_bytes = _manifest_bytes(
        root.name,
        sources,
        directories,
        repository_contract,
    )
    readme_bytes = _readme_bytes(repository_contract)
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
    _validate_repository_contract(manifest.get("repository_contract"))
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


def _audit_bundled_audio_bakes(
    archive: zipfile.ZipFile,
    infos: dict[str, zipfile.ZipInfo],
    file_records: list[dict[str, Any]],
) -> None:
    records_by_path = {record["path"]: record for record in file_records}
    included_files = set(records_by_path)
    prefix = f"{_AUDIO_BAKES_ROOT}/"
    bake_files = sorted(path for path in included_files if path.startswith(prefix))
    if not bake_files:
        return

    def read_value(relative: str) -> Any:
        member = f"{ARCHIVE_ROOT}/{relative}"
        try:
            return json.loads(
                _read_small_member(
                    archive,
                    infos[member],
                    label=f"gain-bake metadata {relative!r}",
                    maximum=64 * 1024 * 1024,
                ).decode("utf-8-sig")
            )
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EpisodeBundleValidationError(
                f"cannot audit gain-bake metadata {relative}: {exc}"
            ) from exc

    def checksum(relative: str) -> str:
        record = records_by_path.get(relative)
        if record is None:
            raise EpisodeBundleValidationError(
                f"gain-bake file is missing from bundle manifest: {relative!r}"
            )
        return str(record["sha256"])

    fingerprints: set[str] = set()
    for relative in bake_files:
        parts = PurePosixPath(relative).parts
        if (
            len(parts) != 4
            or parts[:2] != ("resolve", "audio-bakes")
            or not _HASH_RE.fullmatch(parts[2])
        ):
            raise EpisodeBundleValidationError(
                f"bundle has an invalid immutable gain-bake path: {relative!r}"
            )
        fingerprints.add(parts[2])

    selected: dict[str, dict[str, Any]] = {}
    for fingerprint in sorted(fingerprints):
        manifest_relative = (
            f"{_AUDIO_BAKES_ROOT}/{fingerprint}/{_AUDIO_BAKE_MANIFEST_NAME}"
        )
        if manifest_relative not in included_files:
            raise EpisodeBundleValidationError(
                f"bundled gain-bake has no manifest: {manifest_relative!r}"
            )
        manifest = _audio_json_object(
            read_value(manifest_relative),
            label=manifest_relative,
        )
        contract = _audio_json_object(
            manifest.get("contract"),
            label=f"{manifest_relative} contract",
        )
        output = _audio_json_object(
            manifest.get("output"),
            label=f"{manifest_relative} output",
        )
        source = _audio_json_object(
            manifest.get("source"),
            label=f"{manifest_relative} source",
        )
        output_name = _audio_portable_path(
            output.get("path"),
            label=f"{manifest_relative} output.path",
        )
        source_path = _audio_portable_path(
            source.get("media_path"),
            label=f"{manifest_relative} source.media_path",
        )
        integer_fields = {
            "source_start_frame": contract.get("source_start_frame"),
            "duration_frames": contract.get("duration_frames"),
            "fps": contract.get("fps"),
            "channels": output.get("channels"),
            "sample_rate": output.get("sample_rate"),
        }
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in integer_fields.values()
        ) or any(
            integer_fields[field] <= 0
            for field in ("duration_frames", "fps", "channels", "sample_rate")
        ):
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: gain-bake frame/audio fields are invalid"
            )
        gain_db = contract.get("gain_db")
        if (
            isinstance(gain_db, bool)
            or not isinstance(gain_db, (int, float))
            or not math.isfinite(float(gain_db))
        ):
            raise EpisodeBundleValidationError(
                f"{manifest_relative}: gain_db must be finite"
            )
        selected[fingerprint] = {
            "fingerprint": fingerprint,
            "media_path": f"{_AUDIO_BAKES_ROOT}/{fingerprint}/{output_name}",
            "sha256": output.get("sha256"),
            "source_media_path": source_path,
            "source_path_kind": source.get("path_kind"),
            "source_sha256": source.get("sha256"),
            "source_start_frame": integer_fields["source_start_frame"],
            "duration_frames": integer_fields["duration_frames"],
            "fps": integer_fields["fps"],
            "gain_db": float(gain_db),
            "generator_version": contract.get("generator_version"),
            "channels": integer_fields["channels"],
            "sample_rate": integer_fields["sample_rate"],
        }
    _audit_audio_bake_selection(
        selected,
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
        repository_contract = _validate_repository_contract(
            manifest.get("repository_contract")
        )
        readme = _read_small_member(
            archive,
            infos[readme_member],
            label="bundle README",
        )
        if readme != _readme_bytes(repository_contract):
            raise EpisodeBundleValidationError(
                "bundle README does not match its repository contract"
            )
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
        _audit_bundled_audio_bakes(archive, infos, file_records)
        _audit_bundled_json_metadata(archive, infos, file_records)

    return {
        "valid": True,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "project_name": project_name,
        "repository_contract": repository_contract,
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


def _verify_restore_repository_contract(
    expected: dict[str, Any],
    target: Path,
) -> dict[str, Any]:
    """Verify an exact clean revision when restore occurs inside a checkout.

    A bundle can be restored next to, rather than inside, a Git checkout, so an
    unavailable destination repository is reported instead of rejected.  When
    a destination checkout *is* discoverable, however, accepting a mismatched
    or dirty tree would falsely imply that the recorded contract was honored;
    those cases fail before any archive member is extracted.
    """

    expected = _validate_repository_contract(expected)
    expected_revision = expected["reproducible_revision"]
    if not expected["available"]:
        return {
            "expected_revision": None,
            "observed_revision": None,
            "reproducible": False,
            "status": "source_repository_unavailable",
        }
    if not expected["reproducible"]:
        return {
            "expected_revision": None,
            "observed_revision": None,
            "reproducible": False,
            "status": "source_worktree_not_reproducible",
        }

    observed = _discover_repository_contract(target.parent)
    if not observed["available"]:
        return {
            "expected_revision": expected_revision,
            "observed_revision": None,
            "reproducible": False,
            "status": "destination_repository_unavailable",
        }
    if not observed["reproducible"]:
        raise EpisodeBundleValidationError(
            "destination Git worktree is not clean; cannot verify the bundle's "
            "repository revision"
        )
    if observed["reproducible_revision"] != expected_revision:
        raise EpisodeBundleValidationError(
            "destination Git revision does not match the bundle repository "
            f"contract: expected {expected_revision}, observed "
            f"{observed['reproducible_revision']}"
        )
    return {
        "expected_revision": expected_revision,
        "observed_revision": observed["reproducible_revision"],
        "reproducible": True,
        "status": "verified",
    }


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
    repository_verification = _verify_restore_repository_contract(
        validation["repository_contract"],
        target,
    )
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
        "repository_verification": repository_verification,
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
    "REPOSITORY_CONTRACT_SCHEMA_VERSION",
    "UnsafeEpisodeDestinationError",
    "package_episode",
    "restore_episode_bundle",
    "validate_episode_bundle",
]

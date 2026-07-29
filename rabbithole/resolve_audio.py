"""Render edit-faithful, portable A3/A4 stems for DaVinci Resolve.

Resolve's FCPXML importer does not preserve the mix automation RabbitHole uses
for generated sound.  Importing the raw 30-second bed tiles and SFX files
therefore changes the approved mix: constant-power joins become hard cuts,
category-specific SFX levels become unity gain, and authored silence drops no
longer mute the score.

This module freezes those *mix decisions* into two otherwise ordinary WAV
files:

* A3 ``music-stem.wav`` contains the generated bed, constant-power tiling,
  style-pack gain, and authored silence-drop ducking.
* A4 ``sfx-stem.wav`` contains cue placement, category-specific gains, and
  silence ducking.

The raw sound library remains in the episode bundle, so an editor can replace
or redesign individual sounds.  The stems are the render-faithful default.

Every stem set lives in a content-addressed directory.  ``current.json`` is
only a small pointer used by the compiler; a new prepare never overwrites
audio that an already-running Resolve render may have open.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from rabbithole.assemble import probe_duration
from rabbithole.audiomix import (
    BED_GAIN_DB,
    DEFAULT_SFX_GAIN_DB,
    DUCK_RAMP_SECONDS,
    SFX_CATEGORY_GAIN_DB,
    bed_spans,
    build_bed_layer,
    build_sfx_layer,
    duck,
    events_inside_silence_findings,
    load_sfx_categories,
    measure_mix_master_gain_db,
    sfx_events,
    silence_windows,
)
from rabbithole.jsonio import read_json
from rabbithole.sourceaudio import load_source_audio
from rabbithole.sources.music import resolve_cue
from rabbithole.sources.soundgen import (
    BED_CROSSFADE_SECONDS,
    Library,
    library_bed_variants,
    library_sfx,
)


SCHEMA_VERSION = "resolve-audio-stems.v1"
GENERATOR_VERSION = "resolve-audio-stems.v1"
STEMS_ROOT = Path("resolve") / "audio-stems"
POINTER_NAME = "current.json"
MANIFEST_NAME = "manifest.json"
MUSIC_NAME = "music-stem.wav"
SFX_NAME = "sfx-stem.wav"


class ResolveAudioStemError(RuntimeError):
    """Raised when approved sound cannot be represented faithfully."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text_lf(path: Path) -> str:
    """Hash repository text independent of Git's Windows line endings."""

    text = Path(path).read_text(encoding="utf-8-sig")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _atomic_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            Path(temp_name).unlink()
        except FileNotFoundError:
            pass
        raise


def _project_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ResolveAudioStemError(
            f"audio-stem input must remain inside the episode: {path}"
        ) from exc


def _required_library_files(
    timing: Mapping[str, Any],
    library: Library,
) -> list[Path]:
    """Return every approved generated source the authored markers require."""

    required: set[Path] = set()
    missing: list[str] = []

    for span in bed_spans(dict(timing)):
        kind = resolve_cue(str(span.cue))
        variants = [Path(path).resolve() for path in library_bed_variants(library, kind)]
        if not variants:
            missing.append(f"[MUSIC:{span.cue}] ({kind})")
            continue
        required.update(variants)

    for event in sfx_events(dict(timing)):
        cue = str(event.name)
        source = library_sfx(library, cue)
        if source is None:
            missing.append(f"[SFX:{cue}]")
            continue
        required.add(Path(source).resolve())

    if missing:
        raise ResolveAudioStemError(
            "approved generated sound is missing from the project-local library: "
            + ", ".join(sorted(set(missing)))
        )
    return sorted(required, key=lambda path: path.as_posix().casefold())


def _input_contract(
    root: Path,
    timing_path: Path,
    narration_path: Path,
    sound_manifest_path: Path,
    style_path: Path,
    required_audio: list[Path],
) -> dict[str, Any]:
    inputs: list[dict[str, str]] = []
    for path, kind in (
        (timing_path, "timing"),
        (narration_path, "narration"),
        (sound_manifest_path, "sound_manifest"),
    ):
        inputs.append(
            {
                "kind": kind,
                "location": "project",
                "path": _project_relative(path, root),
                "sha256": _sha256_file(path),
            }
        )
    repo_root = Path(__file__).resolve().parents[1]
    repository_inputs = (
        ("sfx_style", style_path),
        ("stem_builder", Path(__file__).resolve()),
        ("audio_mix_implementation", Path(__file__).with_name("audiomix.py").resolve()),
        (
            "sound_tiling_implementation",
            Path(__file__).parent / "sources" / "soundgen.py",
        ),
    )
    for kind, repository_path in repository_inputs:
        repository_path = Path(repository_path).resolve()
        try:
            relative = repository_path.relative_to(repo_root).as_posix()
        except ValueError as exc:
            raise ResolveAudioStemError(
                "audio-stem implementation input must remain inside the "
                f"RabbitHole checkout: {repository_path}"
            ) from exc
        inputs.append(
            {
                "kind": kind,
                "location": "repository",
                "path": relative,
                "sha256": _sha256_text_lf(repository_path),
                "hash_mode": "text-lf",
            }
        )
    for path in required_audio:
        inputs.append(
            {
                "kind": "generated_sound",
                "location": "project",
                "path": _project_relative(path, root),
                "sha256": _sha256_file(path),
            }
        )
    return {
        "generator_version": GENERATOR_VERSION,
        "mix_policy": {
            "music_gain_db": BED_GAIN_DB,
            "sfx_category_gain_db": dict(sorted(SFX_CATEGORY_GAIN_DB.items())),
            "default_sfx_gain_db": DEFAULT_SFX_GAIN_DB,
            "duck_ramp_seconds": DUCK_RAMP_SECONDS,
            "bed_crossfade_seconds": BED_CROSSFADE_SECONDS,
            "bed_tiling": "qsin_constant_power",
        },
        "inputs": inputs,
    }


def _fingerprint(contract: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(contract)).hexdigest()


def _finding_payload(finding: Any) -> dict[str, str]:
    return {
        "gate": str(getattr(finding, "gate", "audiomix")),
        "severity": str(getattr(finding, "severity", "warning")),
        "message": str(getattr(finding, "message", finding)),
    }


def _validate_stem(path: Path, expected_seconds: float) -> dict[str, Any]:
    if not path.is_file():
        raise ResolveAudioStemError(f"audio stem was not created: {path}")
    actual = probe_duration(path)
    if abs(actual - expected_seconds) > 0.075:
        raise ResolveAudioStemError(
            f"audio stem duration mismatch for {path.name}: "
            f"expected {expected_seconds:.6f}s, got {actual:.6f}s"
        )
    return {
        "path": path.name,
        "sha256": _sha256_file(path),
        "duration_seconds": round(actual, 6),
        "channels": 1,
        "sample_rate": 44_100,
        "codec": "pcm_s16le",
    }


def prepare_resolve_audio_stems(
    project_root: Path,
    sound_manifest_path: Path,
) -> dict[str, Any]:
    """Create or reuse a content-addressed, render-faithful stem set."""

    root = Path(project_root).resolve()
    manifest_path = Path(sound_manifest_path).resolve()
    timing_path = root / "narration" / "timing.json"
    narration_path = root / "narration" / "vo.wav"
    style_path = Path(__file__).resolve().parents[1] / "style" / "sfx.json"
    if not timing_path.is_file():
        raise ResolveAudioStemError(f"narration timing is missing: {timing_path}")
    if not narration_path.is_file():
        raise ResolveAudioStemError(f"narration audio is missing: {narration_path}")
    if not manifest_path.is_file():
        raise ResolveAudioStemError(
            f"generated sound manifest is missing: {manifest_path}"
        )
    if not style_path.is_file():
        raise ResolveAudioStemError(f"SFX style contract is missing: {style_path}")

    raw_timing = read_json(timing_path)
    if not isinstance(raw_timing, Mapping):
        raise ResolveAudioStemError("narration/timing.json must contain an object")
    timing = dict(raw_timing)
    source_audio_manifest = root / "research" / "source-audio.json"
    if source_audio_manifest.is_file():
        source_audio_duration = float(
            timing.get("duration_seconds")
            or probe_duration(root / "narration" / "vo.wav")
        )
        try:
            source_audio_bites = load_source_audio(
                source_audio_manifest,
                root,
                episode_duration=source_audio_duration,
            )
        except ValueError as exc:
            raise ResolveAudioStemError(str(exc)) from exc
        if source_audio_bites:
            raise ResolveAudioStemError(
                "Resolve audio-stem preparation does not yet support "
                "research/source-audio.json bites because the approved mix ducks "
                "narration and music around each bite. Remove the source-audio "
                "bites or use the FFmpeg renderer until source-aware Resolve stems "
                "are implemented."
            )
    library = Library(manifest_path.parent)
    required_audio = _required_library_files(timing, library)
    contract = _input_contract(
        root,
        timing_path,
        narration_path,
        manifest_path,
        style_path,
        required_audio,
    )
    fingerprint = _fingerprint(contract)
    stems_root = root / STEMS_ROOT
    target = stems_root / fingerprint
    target_manifest = target / MANIFEST_NAME

    if target_manifest.is_file():
        loaded = load_audio_stem_manifest(target_manifest, root=root, verify=True)
        _atomic_json(
            stems_root / POINTER_NAME,
            {
                "schema_version": SCHEMA_VERSION,
                "fingerprint": fingerprint,
                "manifest_path": f"{fingerprint}/{MANIFEST_NAME}",
                "manifest_sha256": _sha256_file(target_manifest),
            },
        )
        return {
            "generated": False,
            "fingerprint": fingerprint,
            "directory": target,
            "manifest_path": target_manifest,
            "manifest": loaded,
        }
    if target.exists():
        raise ResolveAudioStemError(
            f"content-addressed stem directory exists but is incomplete: {target}. "
            "Move it aside and run prepare again; RabbitHole will not overwrite a "
            "path a running render may have open."
        )

    stems_root.mkdir(parents=True, exist_ok=True)
    build_path = Path(
        tempfile.mkdtemp(prefix=f".build-{fingerprint[:12]}-", dir=stems_root)
    )
    publish_path = build_path / "publish"
    work_path = build_path / "work"
    publish_path.mkdir()
    try:
        duration = float(
            timing.get("duration_seconds")
            or probe_duration(root / "narration" / "vo.wav")
        )
        if duration <= 0:
            raise ResolveAudioStemError(
                f"timing document has a non-positive duration: {duration}"
            )
        windows = silence_windows(timing)
        spans = bed_spans(timing)
        events = sfx_events(timing)
        categories = load_sfx_categories(style_path)

        bed_raw, bed_findings = build_bed_layer(
            spans,
            duration,
            work_path / "bed-layer.wav",
            work_path / "bed-cache",
            library=library,
        )
        sfx_raw, sfx_findings = build_sfx_layer(
            events,
            duration,
            work_path / "sfx-layer.wav",
            work_path / "sfx-cache",
            categories,
            library=library,
        )
        findings = [
            *(_finding_payload(item) for item in bed_findings),
            *(_finding_payload(item) for item in sfx_findings),
            *(
                _finding_payload(item)
                for item in events_inside_silence_findings(events, windows)
            ),
        ]
        errors = [item for item in findings if item["severity"] == "error"]
        if errors:
            raise ResolveAudioStemError(
                "audio stem build reported blocking findings: "
                + "; ".join(item["message"] for item in errors)
            )

        music_ducked = duck(
            bed_raw,
            windows,
            work_path / "music-ducked.wav",
        )
        sfx_ducked = duck(
            sfx_raw,
            windows,
            work_path / "sfx-ducked.wav",
        )
        master_gain_db = measure_mix_master_gain_db(
            [narration_path, music_ducked, sfx_ducked],
            work_path / "master-gain-measure.wav",
        )
        music_path = publish_path / MUSIC_NAME
        sfx_path = publish_path / SFX_NAME
        shutil.copyfile(music_ducked, music_path)
        shutil.copyfile(sfx_ducked, sfx_path)
        entries = {
            "music": {
                **_validate_stem(music_path, duration),
                "track": "A3",
                "kind": "music",
            },
            "sfx": {
                **_validate_stem(sfx_path, duration),
                "track": "A4",
                "kind": "sfx",
            },
        }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "generator_version": GENERATOR_VERSION,
            "fingerprint": fingerprint,
            "duration_seconds": round(duration, 6),
            "contract": contract,
            "entries": entries,
            "mix_semantics": {
                "music_gain_db": BED_GAIN_DB,
                "sfx_category_gain_db": dict(sorted(SFX_CATEGORY_GAIN_DB.items())),
                "default_sfx_gain_db": DEFAULT_SFX_GAIN_DB,
                "silence_duck_gain_db": "-inf",
                "duck_ramp_seconds": DUCK_RAMP_SECONDS,
                "bed_crossfade_seconds": BED_CROSSFADE_SECONDS,
                "bed_tiling": "qsin_constant_power",
                "raw_library_retained": True,
                "master_gain_db": round(master_gain_db, 4),
            },
            "findings": findings,
        }
        _atomic_json(publish_path / MANIFEST_NAME, manifest)
        os.replace(publish_path, target)
        _atomic_json(
            stems_root / POINTER_NAME,
            {
                "schema_version": SCHEMA_VERSION,
                "fingerprint": fingerprint,
                "manifest_path": f"{fingerprint}/{MANIFEST_NAME}",
                "manifest_sha256": _sha256_file(target_manifest),
            },
        )
        return {
            "generated": True,
            "fingerprint": fingerprint,
            "directory": target,
            "manifest_path": target_manifest,
            "manifest": manifest,
        }
    finally:
        resolved_build = build_path.resolve()
        resolved_root = stems_root.resolve()
        if resolved_build.parent == resolved_root and resolved_build.name.startswith(
            ".build-"
        ):
            shutil.rmtree(resolved_build, ignore_errors=True)


def _safe_manifest_path(pointer: Mapping[str, Any], stems_root: Path) -> Path:
    raw = pointer.get("manifest_path")
    if not isinstance(raw, str) or not raw.strip():
        raise ResolveAudioStemError(
            "resolve/audio-stems/current.json has no manifest_path"
        )
    portable = raw.replace("\\", "/")
    parts = PurePosixPath(portable).parts
    if (
        not parts
        or PurePosixPath(portable).is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ResolveAudioStemError(
            f"audio-stem manifest path is not portable: {raw!r}"
        )
    candidate = stems_root.joinpath(*parts).resolve()
    try:
        candidate.relative_to(stems_root.resolve())
    except ValueError as exc:
        raise ResolveAudioStemError(
            f"audio-stem manifest escapes the episode: {raw!r}"
        ) from exc
    return candidate


def load_current_audio_stems(
    project_root: Path,
    *,
    verify: bool = True,
) -> tuple[Path, dict[str, Any]] | None:
    """Load the immutable stem set selected by ``current.json``."""

    root = Path(project_root).resolve()
    stems_root = root / STEMS_ROOT
    pointer_path = stems_root / POINTER_NAME
    if not pointer_path.is_file():
        return None
    raw_pointer = read_json(pointer_path)
    if not isinstance(raw_pointer, Mapping):
        raise ResolveAudioStemError(f"{pointer_path} must contain an object")
    manifest_path = _safe_manifest_path(raw_pointer, stems_root)
    if not manifest_path.is_file():
        raise ResolveAudioStemError(
            f"selected audio-stem manifest is missing: {manifest_path}"
        )
    expected = raw_pointer.get("manifest_sha256")
    if expected and expected != _sha256_file(manifest_path):
        raise ResolveAudioStemError(
            f"selected audio-stem manifest checksum changed: {manifest_path}"
        )
    return manifest_path, load_audio_stem_manifest(
        manifest_path,
        root=root,
        verify=verify,
    )


def load_audio_stem_manifest(
    manifest_path: Path,
    *,
    root: Path,
    verify: bool,
) -> dict[str, Any]:
    """Validate one stem manifest and its source/content checksums."""

    manifest_path = Path(manifest_path).resolve()
    root = Path(root).resolve()
    raw = read_json(manifest_path)
    if not isinstance(raw, Mapping):
        raise ResolveAudioStemError(f"{manifest_path} must contain an object")
    manifest = dict(raw)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ResolveAudioStemError(
            f"unsupported audio-stem schema in {manifest_path}: "
            f"{manifest.get('schema_version')!r}"
        )
    contract = manifest.get("contract")
    if not isinstance(contract, Mapping):
        raise ResolveAudioStemError(
            f"audio-stem manifest has no input contract: {manifest_path}"
        )
    fingerprint = _fingerprint(contract)
    if fingerprint != manifest.get("fingerprint"):
        raise ResolveAudioStemError(
            f"audio-stem fingerprint mismatch: {manifest_path}"
        )

    repo_root = Path(__file__).resolve().parents[1]
    if verify:
        for item in contract.get("inputs", []):
            if not isinstance(item, Mapping):
                raise ResolveAudioStemError(
                    f"audio-stem input record is malformed: {item!r}"
                )
            location = item.get("location")
            if location not in {"project", "repository"}:
                raise ResolveAudioStemError(
                    f"audio-stem input location is invalid: {location!r}"
                )
            base = root if location == "project" else repo_root
            path_value = item.get("path")
            if not isinstance(path_value, str):
                raise ResolveAudioStemError(
                    f"audio-stem input path is malformed: {path_value!r}"
                )
            path = base.joinpath(*PurePosixPath(path_value).parts).resolve()
            try:
                path.relative_to(base.resolve())
            except ValueError as exc:
                raise ResolveAudioStemError(
                    f"audio-stem input escapes {location}: {path_value!r}"
                ) from exc
            hash_mode = item.get("hash_mode", "bytes")
            if hash_mode not in {"bytes", "text-lf"}:
                raise ResolveAudioStemError(
                    f"audio-stem input hash mode is invalid: {hash_mode!r}"
                )
            actual_sha256 = (
                _sha256_text_lf(path)
                if hash_mode == "text-lf" and path.is_file()
                else _sha256_file(path)
                if path.is_file()
                else None
            )
            if actual_sha256 != item.get("sha256"):
                raise ResolveAudioStemError(
                    "audio stems are stale or their approved source changed: "
                    f"{path}. Run `rabbithole resolve prepare` to create a new "
                    "content-addressed stem set."
                )

    entries = manifest.get("entries")
    if not isinstance(entries, Mapping):
        raise ResolveAudioStemError(
            f"audio-stem manifest has no entries: {manifest_path}"
        )
    for key in ("music", "sfx"):
        entry = entries.get(key)
        if not isinstance(entry, Mapping):
            raise ResolveAudioStemError(
                f"audio-stem manifest is missing {key!r}: {manifest_path}"
            )
        raw_path = entry.get("path")
        if not isinstance(raw_path, str):
            raise ResolveAudioStemError(
                f"audio-stem {key!r} path is malformed: {raw_path!r}"
            )
        path = manifest_path.parent.joinpath(*PurePosixPath(raw_path).parts).resolve()
        try:
            path.relative_to(manifest_path.parent.resolve())
        except ValueError as exc:
            raise ResolveAudioStemError(
                f"audio-stem {key!r} escapes its immutable directory: {raw_path!r}"
            ) from exc
        if verify and (
            not path.is_file() or _sha256_file(path) != entry.get("sha256")
        ):
            raise ResolveAudioStemError(
                f"audio-stem {key!r} is missing or changed: {path}"
            )
    return manifest


__all__ = [
    "ResolveAudioStemError",
    "load_audio_stem_manifest",
    "load_current_audio_stems",
    "prepare_resolve_audio_stems",
]

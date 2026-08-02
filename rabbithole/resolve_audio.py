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
import math
import os
import re
import shutil
import tempfile
import wave
from collections.abc import Mapping, MutableMapping
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np

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
GENERATOR_VERSION = "resolve-audio-stems.v2"
RESOLVE_FRAME_RATE = 30
AUDIO_SAMPLE_RATE = 44_100
SAMPLES_PER_RESOLVE_FRAME = AUDIO_SAMPLE_RATE // RESOLVE_FRAME_RATE
STEMS_ROOT = Path("resolve") / "audio-stems"
POINTER_NAME = "current.json"
MANIFEST_NAME = "manifest.json"
MUSIC_NAME = "music-stem.wav"
SFX_NAME = "sfx-stem.wav"
GAIN_BAKE_SCHEMA_VERSION = "resolve-audio-gain-bake.v1"
GAIN_BAKE_GENERATOR_VERSION = "resolve-audio-gain-bake.v1"
GAIN_BAKES_ROOT = Path("resolve") / "audio-bakes"
GAIN_BAKE_MANIFEST_NAME = "manifest.json"


class ResolveAudioStemError(RuntimeError):
    """Raised when approved sound cannot be represented faithfully."""


class ResolveAudioBakeError(RuntimeError):
    """Raised when an editable Resolve gain cannot be frozen safely."""


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


def _frame_aligned_duration(seconds: float) -> tuple[int, int, float]:
    """Return Resolve frames, PCM samples, and exact seconds for ``seconds``.

    The Resolve compiler converts timing seconds to 30-fps frames with decimal
    half-up rounding.  Freezing stems to the matching whole-frame sample count
    prevents Resolve from shortening an audio-only append by one frame when a
    timing duration falls between frame boundaries.
    """

    if AUDIO_SAMPLE_RATE % RESOLVE_FRAME_RATE:
        raise ResolveAudioStemError(
            "Resolve audio sample rate must contain a whole number of samples "
            "per video frame"
        )
    frame_count = max(
        1,
        int(
            (Decimal(str(seconds)) * Decimal(RESOLVE_FRAME_RATE)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        ),
    )
    sample_count = frame_count * SAMPLES_PER_RESOLVE_FRAME
    return frame_count, sample_count, sample_count / AUDIO_SAMPLE_RATE


def _write_frame_aligned_pcm_wav(
    source: Path,
    destination: Path,
    *,
    sample_count: int,
) -> None:
    """Copy PCM audio while padding/truncating to an exact sample count."""

    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp.wav",
        dir=destination.parent,
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with wave.open(str(source), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            compression = reader.getcomptype()
            if (
                channels != 1
                or sample_width != 2
                or sample_rate != AUDIO_SAMPLE_RATE
                or compression != "NONE"
            ):
                raise ResolveAudioStemError(
                    "Resolve audio stems must be mono 44.1 kHz PCM s16le before "
                    f"frame alignment: {source}"
                )

            source_samples = reader.getnframes()
            copy_samples = min(source_samples, sample_count)
            with wave.open(str(temp_path), "wb") as writer:
                writer.setnchannels(channels)
                writer.setsampwidth(sample_width)
                writer.setframerate(sample_rate)
                remaining = copy_samples
                while remaining:
                    requested = min(remaining, 1_048_576)
                    data = reader.readframes(requested)
                    actual = len(data) // (channels * sample_width)
                    if actual <= 0:
                        raise ResolveAudioStemError(
                            f"audio stem ended before its WAV header: {source}"
                        )
                    writer.writeframesraw(data)
                    remaining -= actual

                remaining = sample_count - copy_samples
                silence_frame = b"\x00" * channels * sample_width
                while remaining:
                    count = min(remaining, 1_048_576)
                    writer.writeframesraw(silence_frame * count)
                    remaining -= count
        os.replace(temp_path, destination)
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


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
            "timeline_frame_rate": RESOLVE_FRAME_RATE,
            "audio_sample_rate": AUDIO_SAMPLE_RATE,
            "samples_per_timeline_frame": SAMPLES_PER_RESOLVE_FRAME,
        },
        "inputs": inputs,
    }


def _fingerprint(contract: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(contract)).hexdigest()


def _gain_bake_source_path(root: Path, clip: Mapping[str, Any]) -> Path:
    raw = clip.get("media_path")
    if not isinstance(raw, str) or not raw.strip():
        raise ResolveAudioBakeError(
            f"audio clip {clip.get('id')!r} has no media_path to gain-bake"
        )
    path_kind = clip.get("path_kind")
    if path_kind == "project-relative":
        portable = raw.replace("\\", "/")
        parts = PurePosixPath(portable).parts
        if (
            not parts
            or PurePosixPath(portable).is_absolute()
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ResolveAudioBakeError(
                f"audio clip {clip.get('id')!r} has an unsafe project path: {raw!r}"
            )
        path = root.joinpath(*parts).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ResolveAudioBakeError(
                f"audio clip {clip.get('id')!r} escapes the project: {raw!r}"
            ) from exc
        return path
    if path_kind == "external-absolute":
        return Path(raw).expanduser().resolve()
    raise ResolveAudioBakeError(
        f"audio clip {clip.get('id')!r} has unsupported path_kind {path_kind!r}"
    )


def _pcm_wave_metadata(path: Path) -> dict[str, int]:
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            sample_count = handle.getnframes()
            compression = handle.getcomptype()
    except (OSError, wave.Error) as exc:
        raise ResolveAudioBakeError(f"gain-bake source is not readable PCM WAV: {path}") from exc
    if (
        channels <= 0
        or sample_width not in {1, 2, 3, 4}
        or sample_rate <= 0
        or sample_count < 0
        or compression != "NONE"
    ):
        raise ResolveAudioBakeError(
            "gain-bake source must be uncompressed integer PCM WAV with a "
            f"supported sample width: {path}"
        )
    return {
        "channels": channels,
        "sample_width": sample_width,
        "sample_rate": sample_rate,
        "sample_count": sample_count,
    }


def _frame_to_audio_sample(frame: int, sample_rate: int, fps: int) -> int:
    if frame < 0 or sample_rate <= 0 or fps <= 0:
        raise ResolveAudioBakeError(
            "gain-bake frame, sample rate, and frame rate must be positive"
        )
    # Exact rational half-up rounding; independent of host floating-point mode.
    return (2 * frame * sample_rate + fps) // (2 * fps)


def _gain_bake_audio_name(source_name: str, fingerprint: str) -> str:
    """Return a Resolve-unambiguous, portable derivative filename."""

    stem = Path(source_name).stem
    portable_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")
    portable_stem = portable_stem[:64] or "audio"
    return f"{portable_stem}.gain-{fingerprint[:12]}.wav"


def _pcm_chunk_to_i16(data: bytes, sample_width: int) -> np.ndarray:
    if sample_width == 1:
        return (np.frombuffer(data, dtype=np.uint8).astype(np.int32) - 128) << 8
    if sample_width == 2:
        return np.frombuffer(data, dtype="<i2").astype(np.int32)
    if sample_width == 3:
        values = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        decoded = values[:, 0] | (values[:, 1] << 8) | (values[:, 2] << 16)
        decoded = np.where(decoded & 0x800000, decoded - 0x1000000, decoded)
        return decoded >> 8
    if sample_width == 4:
        return np.frombuffer(data, dtype="<i4").astype(np.int64) >> 16
    raise ResolveAudioBakeError(f"unsupported PCM sample width: {sample_width}")


def _apply_fixed_pcm_gain(data: bytes, sample_width: int, gain_db: float) -> bytes:
    samples = _pcm_chunk_to_i16(data, sample_width).astype(np.int64, copy=False)
    if samples.size == 0:
        return b""
    # Quantize the linear multiplier once, then perform every sample operation
    # as integer Q30 math.  This makes Windows/x64 and macOS/arm64 derivatives
    # byte-identical for the same source and contract.
    with localcontext() as context:
        context.prec = 50
        exponent = Decimal(str(gain_db)) / Decimal(20)
        linear_gain = (Decimal(10).ln() * exponent).exp()
        coefficient = int(
            (linear_gain * Decimal(1 << 30)).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )
    products = samples * coefficient
    magnitudes = (np.abs(products) + (1 << 29)) >> 30
    scaled = np.where(products < 0, -magnitudes, magnitudes)
    clipped = np.clip(scaled, -32768, 32767).astype("<i2")
    return clipped.tobytes()


def _write_gain_baked_pcm_wav(
    source: Path,
    destination: Path,
    *,
    source_start_sample: int,
    sample_count: int,
    gain_db: float,
) -> dict[str, int]:
    """Write an exact-length PCM16 derivative without modifying ``source``."""

    if source_start_sample < 0 or sample_count <= 0:
        raise ResolveAudioBakeError("gain-bake sample range must be positive")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp.wav",
        dir=destination.parent,
    )
    os.close(fd)
    temp_path = Path(temp_name)
    copied_frames = 0
    try:
        with wave.open(str(source), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            if (
                channels <= 0
                or sample_width not in {1, 2, 3, 4}
                or sample_rate <= 0
                or reader.getcomptype() != "NONE"
            ):
                raise ResolveAudioBakeError(
                    f"gain-bake source is not supported integer PCM WAV: {source}"
                )
            source_frames = reader.getnframes()
            reader.setpos(min(source_start_sample, source_frames))
            available = max(0, source_frames - source_start_sample)
            frames_to_copy = min(sample_count, available)
            with wave.open(str(temp_path), "wb") as writer:
                writer.setnchannels(channels)
                writer.setsampwidth(2)
                writer.setframerate(sample_rate)
                remaining = frames_to_copy
                while remaining:
                    requested = min(remaining, 262_144)
                    raw = reader.readframes(requested)
                    actual = len(raw) // (channels * sample_width)
                    if actual <= 0:
                        break
                    writer.writeframesraw(
                        _apply_fixed_pcm_gain(raw, sample_width, gain_db)
                    )
                    copied_frames += actual
                    remaining -= actual

                padding = sample_count - copied_frames
                silence_frame = b"\x00\x00" * channels
                while padding:
                    count = min(padding, 262_144)
                    writer.writeframesraw(silence_frame * count)
                    padding -= count
        os.replace(temp_path, destination)
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return {
        "channels": channels,
        "sample_rate": sample_rate,
        "sample_count": sample_count,
        "copied_sample_count": copied_frames,
        "padded_sample_count": sample_count - copied_frames,
    }


def _validate_gain_bake(
    manifest_path: Path,
    *,
    expected_contract: Mapping[str, Any],
) -> dict[str, Any]:
    raw = read_json(manifest_path)
    if not isinstance(raw, Mapping):
        raise ResolveAudioBakeError(f"gain-bake manifest must be an object: {manifest_path}")
    manifest = dict(raw)
    if manifest.get("schema_version") != GAIN_BAKE_SCHEMA_VERSION:
        raise ResolveAudioBakeError(f"unsupported gain-bake manifest: {manifest_path}")
    if manifest.get("contract") != dict(expected_contract):
        raise ResolveAudioBakeError(
            f"gain-bake contract does not match its content-addressed path: {manifest_path}"
        )
    fingerprint = _fingerprint(expected_contract)
    if manifest.get("fingerprint") != fingerprint:
        raise ResolveAudioBakeError(f"gain-bake fingerprint mismatch: {manifest_path}")
    source_basename = expected_contract.get("source_basename")
    if not isinstance(source_basename, str) or not source_basename:
        raise ResolveAudioBakeError(
            f"gain-bake contract has no source basename: {manifest_path}"
        )
    audio_name = _gain_bake_audio_name(source_basename, fingerprint)
    output = manifest.get("output")
    if not isinstance(output, Mapping) or output.get("path") != audio_name:
        raise ResolveAudioBakeError(f"gain-bake manifest has invalid output: {manifest_path}")
    audio_path = manifest_path.parent / audio_name
    if not audio_path.is_file() or _sha256_file(audio_path) != output.get("sha256"):
        raise ResolveAudioBakeError(f"gain-bake audio is missing or changed: {audio_path}")
    metadata = _pcm_wave_metadata(audio_path)
    expected_output = expected_contract["output"]
    if (
        metadata["channels"] != expected_output["channels"]
        or metadata["sample_width"] != 2
        or metadata["sample_rate"] != expected_output["sample_rate"]
        or metadata["sample_count"] != expected_output["sample_count"]
    ):
        raise ResolveAudioBakeError(f"gain-bake audio format changed: {audio_path}")
    return manifest


def prepare_resolve_gain_bake(
    project_root: Path,
    clip: Mapping[str, Any],
    *,
    fps: int,
) -> dict[str, Any]:
    """Create or reuse one immutable, exact-duration gain-baked WAV clip."""

    root = Path(project_root).resolve()
    source = _gain_bake_source_path(root, clip)
    if not source.is_file():
        raise ResolveAudioBakeError(f"gain-bake source is missing: {source}")
    if source.suffix.casefold() != ".wav":
        raise ResolveAudioBakeError(f"gain-bake source is not WAV: {source}")
    declared_sha = clip.get("sha256")
    actual_sha = _sha256_file(source)
    if not isinstance(declared_sha, str) or declared_sha != actual_sha:
        raise ResolveAudioBakeError(
            f"gain-bake source checksum changed after plan compilation: {source}"
        )
    raw_gain = clip.get("gain_db", 0.0)
    if isinstance(raw_gain, bool) or not isinstance(raw_gain, (int, float)):
        raise ResolveAudioBakeError(
            f"audio clip {clip.get('id')!r} has invalid gain_db {raw_gain!r}"
        )
    gain_db = float(raw_gain)
    if not math.isfinite(gain_db) or not -80.0 <= gain_db <= 24.0:
        raise ResolveAudioBakeError(
            f"audio clip {clip.get('id')!r} gain_db must be between -80 and +24"
        )
    source_start_frame = int(clip.get("source_start_frame", 0))
    duration_frames = int(clip.get("duration_frames", 0))
    if source_start_frame < 0 or duration_frames <= 0 or fps <= 0:
        raise ResolveAudioBakeError(
            f"audio clip {clip.get('id')!r} has an invalid gain-bake frame range"
        )
    metadata = _pcm_wave_metadata(source)
    source_media_path = str(clip["media_path"]).replace("\\", "/")
    source_path_kind = clip.get("path_kind")
    source_start_sample = _frame_to_audio_sample(
        source_start_frame, metadata["sample_rate"], fps
    )
    source_end_sample = _frame_to_audio_sample(
        source_start_frame + duration_frames,
        metadata["sample_rate"],
        fps,
    )
    output_samples = source_end_sample - source_start_sample
    if output_samples <= 0:
        raise ResolveAudioBakeError(
            f"audio clip {clip.get('id')!r} produces no PCM samples"
        )
    contract = {
        "generator_version": GAIN_BAKE_GENERATOR_VERSION,
        "source_basename": source.name,
        # The checksum identifies the PCM bytes, while the normalized logical
        # path identifies which immutable project source supplied them.  Two
        # content-addressed stem sets may legitimately contain byte-identical
        # WAVs.  Keeping the path in the contract prevents a later prepare from
        # reusing a bake manifest whose provenance points at a retired stem set.
        "source_media_path": source_media_path,
        "source_path_kind": source_path_kind,
        "source_sha256": actual_sha,
        "gain_db": gain_db,
        "source_start_frame": source_start_frame,
        "duration_frames": duration_frames,
        "fps": fps,
        "source": {
            "channels": metadata["channels"],
            "sample_width": metadata["sample_width"],
            "sample_rate": metadata["sample_rate"],
            "sample_count": metadata["sample_count"],
        },
        "output": {
            "channels": metadata["channels"],
            "sample_width": 2,
            "sample_rate": metadata["sample_rate"],
            "sample_count": output_samples,
        },
    }
    fingerprint = _fingerprint(contract)
    audio_name = _gain_bake_audio_name(source.name, fingerprint)
    bakes_root = root / GAIN_BAKES_ROOT
    target = bakes_root / fingerprint
    target_manifest = target / GAIN_BAKE_MANIFEST_NAME
    if target_manifest.is_file():
        manifest = _validate_gain_bake(
            target_manifest,
            expected_contract=contract,
        )
        return {
            "generated": False,
            "fingerprint": fingerprint,
            "directory": target,
            "audio_path": target / audio_name,
            "manifest_path": target_manifest,
            "manifest": manifest,
        }
    if target.exists():
        raise ResolveAudioBakeError(
            f"content-addressed gain-bake directory is incomplete: {target}"
        )

    bakes_root.mkdir(parents=True, exist_ok=True)
    build_path = Path(
        tempfile.mkdtemp(prefix=f".build-{fingerprint[:12]}-", dir=bakes_root)
    )
    publish_path = build_path / "publish"
    publish_path.mkdir()
    try:
        output_path = publish_path / audio_name
        write_result = _write_gain_baked_pcm_wav(
            source,
            output_path,
            source_start_sample=source_start_sample,
            sample_count=output_samples,
            gain_db=gain_db,
        )
        output_sha = _sha256_file(output_path)
        manifest = {
            "schema_version": GAIN_BAKE_SCHEMA_VERSION,
            "generator_version": GAIN_BAKE_GENERATOR_VERSION,
            "fingerprint": fingerprint,
            "contract": contract,
            "source": {
                "media_path": source_media_path,
                "path_kind": source_path_kind,
                "sha256": actual_sha,
            },
            "output": {
                "path": audio_name,
                "sha256": output_sha,
                "codec": "pcm_s16le",
                **write_result,
            },
        }
        _atomic_json(publish_path / GAIN_BAKE_MANIFEST_NAME, manifest)
        os.replace(publish_path, target)
        return {
            "generated": True,
            "fingerprint": fingerprint,
            "directory": target,
            "audio_path": target / audio_name,
            "manifest_path": target_manifest,
            "manifest": manifest,
        }
    finally:
        shutil.rmtree(build_path, ignore_errors=True)


def bake_resolve_plan_audio_gains(
    project_root: Path,
    plan: MutableMapping[str, Any],
) -> dict[str, Any]:
    """Replace non-unity WAV clips with immutable unity-gain derivatives."""

    root = Path(project_root).resolve()
    raw_audio = plan.get("audio")
    if not isinstance(raw_audio, list):
        raise ResolveAudioBakeError("Resolve plan audio must be an array")
    fps = int(plan.get("fps", 0))
    baked: list[dict[str, Any]] = []
    baked_by_id: dict[str, dict[str, Any]] = {}
    for position, raw_clip in enumerate(raw_audio):
        if not isinstance(raw_clip, MutableMapping):
            raise ResolveAudioBakeError(
                f"Resolve plan audio[{position}] must be an object"
            )
        clip = raw_clip
        raw_gain = clip.get("gain_db", 0.0)
        if isinstance(raw_gain, bool) or not isinstance(raw_gain, (int, float)):
            raise ResolveAudioBakeError(
                f"audio clip {clip.get('id')!r} has invalid gain_db {raw_gain!r}"
            )
        gain_db = float(raw_gain)
        if not math.isfinite(gain_db):
            raise ResolveAudioBakeError(
                f"audio clip {clip.get('id')!r} has non-finite gain_db"
            )
        media_path = clip.get("media_path")
        if abs(gain_db) <= 1e-9 or not clip.get("exists"):
            continue
        if (
            not isinstance(media_path, str)
            or PurePosixPath(media_path.replace("\\", "/")).suffix.casefold()
            != ".wav"
        ):
            raise ResolveAudioBakeError(
                f"audio clip {clip.get('id')!r} uses non-zero gain "
                f"{gain_db:g} dB, but {media_path!r} is not a supported WAV. "
                "Convert it to an uncompressed integer PCM WAV, update the "
                "clip media path and checksum, then run Resolve preparation "
                "again; refusing to emit editable timeline gain"
            )
        original = {
            "media_path": media_path,
            "path_kind": clip.get("path_kind"),
            "sha256": clip.get("sha256"),
            "source_start_frame": int(clip.get("source_start_frame", 0)),
        }
        result = prepare_resolve_gain_bake(root, clip, fps=fps)
        audio_path = Path(result["audio_path"]).resolve()
        try:
            portable_path = audio_path.relative_to(root).as_posix()
        except ValueError as exc:
            raise ResolveAudioBakeError(
                f"gain-bake output escapes the project: {audio_path}"
            ) from exc
        output = result["manifest"]["output"]
        clip.update(
            {
                "media_path": portable_path,
                "path_kind": "project-relative",
                "sha256": output["sha256"],
                "channels": output["channels"],
                "source_sample_rate": output["sample_rate"],
                "source_start_frame": 0,
                "gain_db": 0.0,
                "gain_baked_db": gain_db,
                "gain_bake_source_sha256": original["sha256"],
                "gain_bake_source_media_path": original["media_path"],
                "gain_bake_source_path_kind": original["path_kind"],
                "gain_bake_source_start_frame": original["source_start_frame"],
                "gain_bake_fingerprint": result["fingerprint"],
                "gain_bake_generator_version": GAIN_BAKE_GENERATOR_VERSION,
            }
        )
        record = {
            "id": str(clip.get("id") or ""),
            "fingerprint": result["fingerprint"],
            "generated": bool(result["generated"]),
            "audio_path": portable_path,
            "source_sha256": original["sha256"],
            "gain_baked_db": gain_db,
        }
        baked.append(record)
        baked_by_id[record["id"]] = dict(clip)

    cold_open = plan.get("cold_open")
    if isinstance(cold_open, MutableMapping):
        nested_audio = cold_open.get("audio")
        if isinstance(nested_audio, list):
            for index, item in enumerate(nested_audio):
                if isinstance(item, Mapping):
                    replacement = baked_by_id.get(str(item.get("id") or ""))
                    if replacement is not None:
                        nested_audio[index] = dict(replacement)
    return {"count": len(baked), "entries": baked}


def _finding_payload(finding: Any) -> dict[str, str]:
    return {
        "gate": str(getattr(finding, "gate", "audiomix")),
        "severity": str(getattr(finding, "severity", "warning")),
        "message": str(getattr(finding, "message", finding)),
    }


def _validate_stem(
    path: Path,
    expected_seconds: float,
    *,
    expected_samples: int,
) -> dict[str, Any]:
    if not path.is_file():
        raise ResolveAudioStemError(f"audio stem was not created: {path}")
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            compression = handle.getcomptype()
            sample_count = handle.getnframes()
    except (OSError, wave.Error) as exc:
        raise ResolveAudioStemError(f"audio stem is not a readable WAV: {path}") from exc
    if (
        channels != 1
        or sample_width != 2
        or sample_rate != AUDIO_SAMPLE_RATE
        or compression != "NONE"
    ):
        raise ResolveAudioStemError(
            f"audio stem format mismatch for {path.name}; expected mono 44.1 kHz "
            "PCM s16le"
        )
    if sample_count != expected_samples:
        raise ResolveAudioStemError(
            f"audio stem sample-count mismatch for {path.name}: expected "
            f"{expected_samples}, got {sample_count}"
        )
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
        "sample_count": sample_count,
        "channels": channels,
        "sample_rate": sample_rate,
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
        source_duration = float(
            timing.get("duration_seconds")
            or probe_duration(root / "narration" / "vo.wav")
        )
        if source_duration <= 0:
            raise ResolveAudioStemError(
                f"timing document has a non-positive duration: {source_duration}"
            )
        duration_frames, duration_samples, duration = _frame_aligned_duration(
            source_duration
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
        _write_frame_aligned_pcm_wav(
            music_ducked,
            music_path,
            sample_count=duration_samples,
        )
        _write_frame_aligned_pcm_wav(
            sfx_ducked,
            sfx_path,
            sample_count=duration_samples,
        )
        entries = {
            "music": {
                **_validate_stem(
                    music_path,
                    duration,
                    expected_samples=duration_samples,
                ),
                "track": "A3",
                "kind": "music",
            },
            "sfx": {
                **_validate_stem(
                    sfx_path,
                    duration,
                    expected_samples=duration_samples,
                ),
                "track": "A4",
                "kind": "sfx",
            },
        }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "generator_version": GENERATOR_VERSION,
            "fingerprint": fingerprint,
            "duration_seconds": round(duration, 6),
            "duration_frames": duration_frames,
            "source_duration_seconds": round(source_duration, 6),
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

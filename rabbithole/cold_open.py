"""Deterministic cold-open prefix compilation for Resolve plans.

The regular edit is authored against narration time, which starts at frame zero.
A cold open is a prefix, not a destructive retime of those source documents.  This
module therefore has two deliberately pure stages:

``compile_cold_open``
    Validates project-local retained-source media and returns Resolve-shaped V1,
    A2, and A4 items with checksums.

``shift_plan_for_prefix``
    Deep-copies an already compiled Resolve plan, moves every narration-era
    timeline payload by the exact prefix length, and prepends the cold-open items.

No file is written and no Resolve API is called here.  Invalid or non-portable
media fails before the caller can create a new timeline.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import math
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence


SCHEMA_VERSION = "resolve-cold-open.v1"
POLICY_VERSION = "cold-open-prefix.v1"

_AUDIO_SUFFIXES = {
    ".aac",
    ".aif",
    ".aiff",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".wave",
}
_STILL_SUFFIXES = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
_VIDEO_SUFFIXES = {
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".mxf",
    ".ts",
    ".webm",
    ".wmv",
}
_CAPTION_KINDS = {"caption", "captions", "subtitle", "subtitles"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[/\\]")


class ColdOpenError(ValueError):
    """Raised when a cold-open prefix cannot be compiled safely."""


def compile_cold_open(
    raw_config: Any,
    *,
    root: Path,
    fps: int,
    provenance: Sequence[Any],
    probe_audio: Callable[[Path], Mapping[str, Any] | None],
) -> dict[str, Any]:
    """Validate and normalize one project-local cold-open configuration.

    ``raw_config`` accepts ``duration_seconds`` (or exact ``duration_frames``),
    a ``video`` list, and an optional ``sfx`` object/list.  Video entries use
    seconds by default::

        {
          "duration_seconds": 11,
          "video": [{
            "asset_id": "source-s001",
            "timeline_start": 0,
            "source_start": 0,
            "duration": 10,
            "source_audio": false
          }],
          "sfx": {
            "local_path": "assets/soundlib/sfx/static-crackle.wav",
            "timeline_start": 0,
            "source_start": 0,
            "duration": 0.6
          }
        }

    V1 must cover the prefix exactly, with no gap or overlap.  A video entry
    with ``source_audio: true`` produces a time-matched A2 item that references
    the same file through its own audio asset id.  Callers must enable it only
    when their rights ledger permits that exact use.  SFX is placed on A4.
    """

    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise ColdOpenError(f"cold-open project root does not exist: {root_path}")
    frame_rate = _positive_integer(fps, "fps")
    if not callable(probe_audio):
        raise ColdOpenError("probe_audio must be callable")

    if raw_config in (None, {}):
        return _empty_prefix()
    config = _mapping(raw_config, "cold_open")
    allowed = {
        "schema_version",
        "enabled",
        "name",
        "label",
        "description",
        "prefix_frames",
        "duration_frames",
        "duration_seconds",
        "duration",
        "video",
        "clips",
        "sfx",
        "sound_effects",
    }
    unknown = sorted(str(key) for key in set(config) - allowed)
    if unknown:
        raise ColdOpenError(
            f"unsupported cold_open field(s): {', '.join(unknown)}"
        )
    if config.get("enabled") is not None and not isinstance(
        config.get("enabled"), bool
    ):
        raise ColdOpenError("cold_open.enabled must be boolean")
    if config.get("enabled") is False:
        authored = set(config) - {
            "schema_version",
            "enabled",
            "name",
            "label",
            "description",
        }
        if authored:
            raise ColdOpenError(
                "disabled cold_open cannot also contain timing or media"
            )
        return _empty_prefix()

    prefix_frames = _frames_from_aliases(
        config,
        frame_keys=("prefix_frames", "duration_frames"),
        second_keys=("duration_seconds", "duration"),
        fps=frame_rate,
        label="cold_open.duration",
        required=True,
    )
    if prefix_frames <= 0:
        raise ColdOpenError("cold_open duration must be greater than zero")

    video_raw = _one_collection_alias(
        config, ("video", "clips"), label="cold_open.video", required=True
    )
    if not video_raw:
        raise ColdOpenError("cold_open.video must contain at least one clip")

    assets = _provenance_index(provenance)
    clips: list[dict[str, Any]] = []
    audio: list[dict[str, Any]] = []
    media_inputs: list[dict[str, Any]] = []

    for position, raw in enumerate(video_raw):
        item = _mapping(raw, f"cold_open.video[{position}]")
        asset_id = _required_string(
            item.get("asset_id"), f"cold_open.video[{position}].asset_id"
        )
        if asset_id not in assets:
            raise ColdOpenError(
                f"cold_open.video[{position}] references unknown provenance "
                f"asset_id {asset_id!r}"
            )
        asset = assets[asset_id]
        media_path, resolved = _validated_project_file(
            asset.get("local_path", asset.get("media_path")),
            root_path,
            label=f"provenance asset {asset_id!r}",
        )
        media_type = str(asset.get("media_type") or "").strip().lower()
        suffix = resolved.suffix.lower()
        if (
            (media_type and media_type != "video")
            or suffix in _AUDIO_SUFFIXES
            or suffix in _STILL_SUFFIXES
            or suffix not in _VIDEO_SUFFIXES
        ):
            raise ColdOpenError(
                f"cold-open provenance asset {asset_id!r} is not video media: "
                f"{media_path}"
            )
        checksum = _sha256_file(resolved)
        recorded_checksum = str(asset.get("sha256") or "").strip().lower()
        if recorded_checksum:
            if not _SHA256_RE.fullmatch(recorded_checksum):
                raise ColdOpenError(
                    f"provenance asset {asset_id!r} has an invalid SHA-256"
                )
            if checksum != recorded_checksum:
                raise ColdOpenError(
                    f"provenance asset {asset_id!r} changed after it was "
                    "checksummed"
                )

        timeline_start = _frames_from_aliases(
            item,
            frame_keys=("timeline_start_frame", "timeline_start_frames", "start_frame"),
            second_keys=("timeline_start", "timeline_start_seconds", "start"),
            fps=frame_rate,
            label=f"cold_open.video[{position}].timeline_start",
            default=0,
        )
        source_start = _frames_from_aliases(
            item,
            frame_keys=("source_start_frame", "source_start_frames"),
            second_keys=("source_start", "source_start_seconds", "in"),
            fps=frame_rate,
            label=f"cold_open.video[{position}].source_start",
            default=0,
        )
        duration = _frames_from_aliases(
            item,
            frame_keys=("duration_frames",),
            second_keys=("duration", "duration_seconds"),
            fps=frame_rate,
            label=f"cold_open.video[{position}].duration",
            required=True,
        )
        if timeline_start < 0 or source_start < 0 or duration <= 0:
            raise ColdOpenError(
                f"cold_open.video[{position}] timing must use non-negative "
                "starts and a positive duration"
            )
        end_frame = timeline_start + duration
        if end_frame > prefix_frames:
            raise ColdOpenError(
                f"cold_open.video[{position}] ends at frame {end_frame}, past "
                f"the {prefix_frames}-frame prefix"
            )
        source_audio = item.get("source_audio", False)
        if not isinstance(source_audio, bool):
            raise ColdOpenError(
                f"cold_open.video[{position}].source_audio must be boolean"
            )

        clip = {
            "index": position - len(video_raw),
            "slot_id": _optional_string(item.get("slot_id"))
            or f"cold-open-{position + 1:03d}",
            "track": "V1",
            "start_frame": timeline_start,
            "end_frame": end_frame,
            "duration_frames": duration,
            "source_start_frame": source_start,
            "source_end_frame": source_start + duration,
            "asset_id": asset_id,
            "media_path": media_path,
            "path_kind": "project-relative",
            "media_type": "video",
            "sha256": checksum,
            "binding": "cold_open_asset_id",
            "origin": "cold_open",
            "slot_kind": "cold_open",
            "framing": "wide",
            "transform": {
                "scale_x": 1.0,
                "scale_y": 1.0,
                "position_x": 0.0,
                "position_y": 0.0,
                "rotation": 0.0,
            },
            "transition": {"kind": "cut", "duration_frames": 0},
            "reason": _optional_string(item.get("reason")) or "cold_open",
            "source_audio": source_audio,
        }
        clip["id"] = _stable_id(
            "cold-open-clip",
            {
                key: clip[key]
                for key in (
                    "asset_id",
                    "track",
                    "start_frame",
                    "end_frame",
                    "source_start_frame",
                    "source_end_frame",
                )
            },
        )
        clips.append(clip)
        media_inputs.append(
            _media_input(
                role="video",
                asset_id=asset_id,
                media_path=media_path,
                sha256=checksum,
                source_asset_id=asset_id,
            )
        )

        if source_audio:
            metadata = _required_audio_metadata(
                probe_audio,
                resolved,
                label=f"cold_open.video[{position}] source audio",
            )
            audio_asset_id = _stable_id(
                "cold-open-source-audio-asset",
                {
                    "source_asset_id": asset_id,
                    "media_path": media_path,
                    "timeline_start": timeline_start,
                    "source_start": source_start,
                    "duration": duration,
                },
            )
            audio_clip = {
                "asset_id": audio_asset_id,
                "source_asset_id": asset_id,
                "source_clip_id": clip["id"],
                "track": "A2",
                "kind": "cold_open_source_audio",
                "start_frame": timeline_start,
                "end_frame": end_frame,
                "duration_frames": duration,
                "source_start_frame": source_start,
                "media_path": media_path,
                "path_kind": "project-relative",
                "exists": True,
                "sha256": checksum,
                "channels": metadata["channels"],
                "source_sample_rate": metadata["sample_rate"],
                "gain_db": _finite_number(
                    item.get("gain_db", 0.0),
                    f"cold_open.video[{position}].gain_db",
                ),
                "duck_vo_db": 0.0,
            }
            audio_clip["id"] = _stable_id(
                "cold-open-audio",
                {
                    key: audio_clip[key]
                    for key in (
                        "asset_id",
                        "track",
                        "start_frame",
                        "end_frame",
                        "source_start_frame",
                    )
                },
            )
            audio.append(audio_clip)
            media_inputs.append(
                _media_input(
                    role="source_audio",
                    asset_id=audio_asset_id,
                    media_path=media_path,
                    sha256=checksum,
                    source_asset_id=asset_id,
                )
            )

    clips.sort(key=lambda value: (value["start_frame"], value["id"]))
    cursor = 0
    for clip in clips:
        start = int(clip["start_frame"])
        if start < cursor:
            raise ColdOpenError(
                f"cold-open V1 clips overlap at frame {start}"
            )
        if start > cursor:
            raise ColdOpenError(
                f"cold-open V1 has a gap from frame {cursor} to {start}"
            )
        cursor = int(clip["end_frame"])
    if cursor != prefix_frames:
        raise ColdOpenError(
            f"cold-open V1 ends at frame {cursor}; exact prefix coverage "
            f"requires frame {prefix_frames}"
        )

    sfx_raw = _one_collection_alias(
        config,
        ("sfx", "sound_effects"),
        label="cold_open.sfx",
        required=False,
        mapping_is_single=True,
    )
    sfx_ranges: list[tuple[int, int]] = []
    for position, raw in enumerate(sfx_raw):
        item = _mapping(raw, f"cold_open.sfx[{position}]")
        raw_path = _one_value_alias(
            item,
            ("local_path", "path", "file"),
            label=f"cold_open.sfx[{position}].local_path",
            required=True,
        )
        media_path, resolved = _validated_project_file(
            raw_path,
            root_path,
            label=f"cold_open.sfx[{position}]",
            require_relative=True,
        )
        if resolved.suffix.lower() not in _AUDIO_SUFFIXES:
            raise ColdOpenError(
                f"cold_open.sfx[{position}] is not supported audio media: "
                f"{media_path}"
            )
        timeline_start = _frames_from_aliases(
            item,
            frame_keys=("timeline_start_frame", "timeline_start_frames", "start_frame"),
            second_keys=("timeline_start", "timeline_start_seconds", "start"),
            fps=frame_rate,
            label=f"cold_open.sfx[{position}].timeline_start",
            default=0,
        )
        source_start = _frames_from_aliases(
            item,
            frame_keys=("source_start_frame", "source_start_frames"),
            second_keys=("source_start", "source_start_seconds", "in"),
            fps=frame_rate,
            label=f"cold_open.sfx[{position}].source_start",
            default=0,
        )
        duration = _frames_from_aliases(
            item,
            frame_keys=("duration_frames",),
            second_keys=("duration", "duration_seconds"),
            fps=frame_rate,
            label=f"cold_open.sfx[{position}].duration",
            required=True,
        )
        if timeline_start < 0 or source_start < 0 or duration <= 0:
            raise ColdOpenError(
                f"cold_open.sfx[{position}] timing must use non-negative "
                "starts and a positive duration"
            )
        end_frame = timeline_start + duration
        if end_frame > prefix_frames:
            raise ColdOpenError(
                f"cold_open.sfx[{position}] ends at frame {end_frame}, past "
                f"the {prefix_frames}-frame prefix"
            )
        for prior_start, prior_end in sfx_ranges:
            if timeline_start < prior_end and end_frame > prior_start:
                raise ColdOpenError(
                    f"cold_open.sfx[{position}] overlaps another A4 effect"
                )
        sfx_ranges.append((timeline_start, end_frame))

        checksum = _sha256_file(resolved)
        metadata = _optional_audio_metadata(
            probe_audio,
            resolved,
            label=f"cold_open.sfx[{position}]",
        )
        asset_id = _optional_string(item.get("asset_id")) or _stable_id(
            "cold-open-sfx-asset",
            {
                "media_path": media_path,
                "timeline_start": timeline_start,
                "source_start": source_start,
                "duration": duration,
            },
        )
        sfx_clip = {
            "asset_id": asset_id,
            "track": "A4",
            "kind": "cold_open_sfx",
            "start_frame": timeline_start,
            "end_frame": end_frame,
            "duration_frames": duration,
            "source_start_frame": source_start,
            "media_path": media_path,
            "path_kind": "project-relative",
            "exists": True,
            "sha256": checksum,
            "channels": metadata.get("channels") if metadata else None,
            "source_sample_rate": metadata.get("sample_rate") if metadata else None,
            "gain_db": _finite_number(
                item.get("gain_db", 0.0),
                f"cold_open.sfx[{position}].gain_db",
            ),
            "duck_vo_db": 0.0,
        }
        sfx_clip["id"] = _stable_id(
            "cold-open-audio",
            {
                key: sfx_clip[key]
                for key in (
                    "asset_id",
                    "track",
                    "start_frame",
                    "end_frame",
                    "source_start_frame",
                )
            },
        )
        audio.append(sfx_clip)
        media_inputs.append(
            _media_input(
                role="sfx",
                asset_id=asset_id,
                media_path=media_path,
                sha256=checksum,
                source_asset_id=None,
            )
        )

    audio.sort(key=lambda value: (value["track"], value["start_frame"], value["id"]))
    media_inputs.sort(
        key=lambda value: (value["media_path"], value["role"], value["asset_id"])
    )
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "prefix_frames": prefix_frames,
        "clips": clips,
        "audio": audio,
        "media_inputs": media_inputs,
    }
    result["contract_sha256"] = _sha256_json(result)
    _validate_compiled_prefix(result)
    return result


def shift_plan_for_prefix(
    plan: Mapping[str, Any], cold_open: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a shifted copy of ``plan`` with ``cold_open`` prepended.

    Timeline starts/ends move; source starts/ends do not.  The function is
    intentionally independent of compiler globals so callers can apply it
    before the bundle is serialized or handed to Resolve.
    """

    if not isinstance(plan, Mapping):
        raise ColdOpenError("Resolve plan must be an object")
    prefix = copy.deepcopy(dict(cold_open))
    _validate_compiled_prefix(prefix)
    prefix_frames = _plan_frame(prefix.get("prefix_frames"), "prefix_frames")
    result = copy.deepcopy(dict(plan))
    if prefix_frames == 0:
        return result

    original_duration = _plan_frame(
        result.get("duration_frames"), "plan.duration_frames"
    )
    result["duration_frames"] = original_duration + prefix_frames

    for collection in (
        "clips",
        "subtitles",
        "upload_subtitles",
        "overlays",
        "audio",
        "highlights",
    ):
        _shift_interval_collection(result, collection, prefix_frames)
    for collection in ("markers", "review_flags", "reviews"):
        _shift_point_collection(result, collection, "frame", prefix_frames)
    _shift_point_collection(result, "missing_media", "start_frame", prefix_frames)

    timing = result.get("timing")
    if timing is not None:
        if not isinstance(timing, Mapping):
            raise ColdOpenError("plan.timing must be an object")
        timing_copy = dict(timing)
        timing_copy["duration_frames"] = _plan_frame(
            timing_copy.get("duration_frames"), "plan.timing.duration_frames"
        ) + prefix_frames
        words = timing_copy.get("words", [])
        if not isinstance(words, list):
            raise ColdOpenError("plan.timing.words must be a list")
        shifted_words: list[dict[str, Any]] = []
        for position, raw_word in enumerate(words):
            word = dict(_mapping(raw_word, f"plan.timing.words[{position}]"))
            _shift_interval(
                word, prefix_frames, f"plan.timing.words[{position}]"
            )
            shifted_words.append(word)
        timing_copy["words"] = shifted_words
        result["timing"] = timing_copy

    for policy_key in ("subtitle_policy", "subtitle_exclusion_policy"):
        policy = result.get(policy_key)
        if policy is None:
            continue
        if not isinstance(policy, Mapping):
            raise ColdOpenError(f"plan.{policy_key} must be an object")
        policy_copy = dict(policy)
        intervals = policy_copy.get("exclusion_intervals", [])
        if not isinstance(intervals, list):
            raise ColdOpenError(
                f"plan.{policy_key}.exclusion_intervals must be a list"
            )
        shifted_intervals: list[dict[str, Any]] = []
        for position, raw_interval in enumerate(intervals):
            interval = dict(
                _mapping(
                    raw_interval,
                    f"plan.{policy_key}.exclusion_intervals[{position}]",
                )
            )
            _shift_interval(
                interval,
                prefix_frames,
                f"plan.{policy_key}.exclusion_intervals[{position}]",
            )
            shifted_intervals.append(interval)
        cold_exclusion = {
            "start_frame": 0,
            "end_frame": prefix_frames,
            "duration_frames": prefix_frames,
            "reasons": ["cold_open"],
            "sources": [
                {
                    "type": "cold_open_prefix",
                    "contract_sha256": prefix.get("contract_sha256"),
                }
            ],
        }
        cold_exclusion["id"] = _stable_id(
            "subtitle-exclusion",
            {
                "policy": POLICY_VERSION,
                "start_frame": 0,
                "end_frame": prefix_frames,
                "reasons": ["cold_open"],
                "sources": cold_exclusion["sources"],
            },
        )
        policy_copy["exclusion_intervals"] = [cold_exclusion] + sorted(
            shifted_intervals,
            key=lambda value: (value["start_frame"], value["end_frame"], value.get("id", "")),
        )
        policy_payload = {
            key: value
            for key, value in policy_copy.items()
            if key != "contract_sha256"
        }
        policy_copy["contract_sha256"] = _sha256_json(policy_payload)
        result[policy_key] = policy_copy

    result["clips"] = sorted(
        copy.deepcopy(prefix["clips"]) + list(result.get("clips", [])),
        key=lambda value: (
            int(value["start_frame"]),
            int(value.get("index", 0)),
            str(value["id"]),
        ),
    )
    result["audio"] = sorted(
        copy.deepcopy(prefix["audio"]) + list(result.get("audio", [])),
        key=lambda value: (
            str(value["track"]),
            int(value["start_frame"]),
            str(value["id"]),
        ),
    )
    result["cold_open"] = prefix

    validation = result.get("timeline_validation")
    if validation is not None:
        if not isinstance(validation, Mapping):
            raise ColdOpenError("plan.timeline_validation must be an object")
        validation_copy = dict(validation)
        validation_copy["end_frame"] = result["duration_frames"]
        validation_copy["video_clip_count"] = sum(
            1 for clip in result["clips"] if clip.get("media_path")
        )
        validation_copy["audio_clip_count"] = sum(
            1 for clip in result["audio"] if clip.get("media_path")
        )
        validation_copy["subtitle_count"] = len(result.get("subtitles", []))
        result["timeline_validation"] = validation_copy

    statistics = result.get("statistics")
    if statistics is not None:
        if not isinstance(statistics, Mapping):
            raise ColdOpenError("plan.statistics must be an object")
        statistics_copy = dict(statistics)
        statistics_copy["cut_count"] = len(result["clips"])
        statistics_copy["average_shot_length_frames"] = _rounded_average(
            [int(clip["duration_frames"]) for clip in result["clips"]]
        )
        result["statistics"] = statistics_copy

    checksums = result.get("checksums")
    if checksums is not None:
        if not isinstance(checksums, Mapping):
            raise ColdOpenError("plan.checksums must be an object")
        checksums_copy = dict(checksums)
        checksums_copy["cold_open_contract_sha256"] = str(
            prefix["contract_sha256"]
        )
        result["checksums"] = checksums_copy

    _assert_clean_prefix(result, prefix)
    return result


def _empty_prefix() -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "prefix_frames": 0,
        "clips": [],
        "audio": [],
        "media_inputs": [],
    }
    result["contract_sha256"] = _sha256_json(result)
    return result


def _provenance_index(provenance: Sequence[Any]) -> dict[str, Mapping[str, Any]]:
    if isinstance(provenance, (str, bytes)) or not isinstance(provenance, Sequence):
        raise ColdOpenError("provenance must be a list")
    result: dict[str, Mapping[str, Any]] = {}
    for position, raw in enumerate(provenance):
        if dataclasses.is_dataclass(raw):
            item: Mapping[str, Any] = dataclasses.asdict(raw)
        elif isinstance(raw, Mapping):
            item = raw
        else:
            raise ColdOpenError(f"provenance[{position}] must be an object")
        asset_id = _required_string(
            item.get("asset_id"), f"provenance[{position}].asset_id"
        )
        if asset_id in result:
            raise ColdOpenError(f"duplicate provenance asset_id: {asset_id}")
        result[asset_id] = item
    return result


def _validated_project_file(
    raw_path: Any,
    root: Path,
    *,
    label: str,
    require_relative: bool = False,
) -> tuple[str, Path]:
    path_text = _required_string(raw_path, f"{label}.local_path")
    portable = path_text.replace("\\", "/")
    looks_absolute = (
        portable.startswith("/")
        or portable.startswith("//")
        or bool(_WINDOWS_ABSOLUTE_RE.match(portable))
    )
    candidate = Path(path_text)
    if require_relative and (looks_absolute or candidate.is_absolute()):
        raise ColdOpenError(f"{label} path must be project-relative")
    if looks_absolute or candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = root.joinpath(*PurePosixPath(portable).parts).resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ColdOpenError(
            f"{label} path escapes project root: {path_text!r}"
        ) from exc
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise ColdOpenError(f"{label} media is missing or empty: {path_text!r}")
    return relative.as_posix(), resolved


def _required_audio_metadata(
    probe: Callable[[Path], Mapping[str, Any] | None],
    path: Path,
    *,
    label: str,
) -> dict[str, int]:
    metadata = _optional_audio_metadata(probe, path, label=label)
    if metadata is None:
        raise ColdOpenError(f"{label} has no readable audio metadata: {path}")
    return metadata


def _optional_audio_metadata(
    probe: Callable[[Path], Mapping[str, Any] | None],
    path: Path,
    *,
    label: str,
) -> dict[str, int] | None:
    try:
        raw = probe(path)
    except Exception as exc:  # probe adapters vary; normalize their failure here.
        raise ColdOpenError(f"cannot probe {label}: {exc}") from exc
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ColdOpenError(f"{label} audio metadata must be an object")
    channels = _positive_integer(raw.get("channels"), f"{label}.channels")
    sample_rate = _positive_integer(
        raw.get("sample_rate"), f"{label}.sample_rate"
    )
    return {"channels": channels, "sample_rate": sample_rate}


def _media_input(
    *,
    role: str,
    asset_id: str,
    media_path: str,
    sha256: str,
    source_asset_id: str | None,
) -> dict[str, Any]:
    payload = {
        "role": role,
        "asset_id": asset_id,
        "source_asset_id": source_asset_id,
        "media_path": media_path,
        "path_kind": "project-relative",
        "sha256": sha256,
    }
    payload["id"] = _stable_id("cold-open-input", payload)
    return payload


def _validate_compiled_prefix(prefix: Mapping[str, Any]) -> None:
    if not isinstance(prefix, Mapping):
        raise ColdOpenError("compiled cold_open must be an object")
    prefix_frames = _plan_frame(prefix.get("prefix_frames"), "prefix_frames")
    clips = prefix.get("clips", [])
    audio = prefix.get("audio", [])
    media_inputs = prefix.get("media_inputs", [])
    for name, values in (
        ("clips", clips),
        ("audio", audio),
        ("media_inputs", media_inputs),
    ):
        if not isinstance(values, list):
            raise ColdOpenError(f"compiled cold_open.{name} must be a list")
    if prefix_frames == 0:
        if clips or audio or media_inputs:
            raise ColdOpenError("zero-frame cold_open cannot contain media")
        return
    if not clips:
        raise ColdOpenError("compiled cold_open has no V1 coverage")

    cursor = 0
    clip_ids: set[str] = set()
    source_audio_by_clip: dict[str, Mapping[str, Any]] = {}
    for position, raw in enumerate(
        sorted(clips, key=lambda value: (value.get("start_frame", 0), value.get("id", "")))
    ):
        clip = _mapping(raw, f"compiled cold_open.clips[{position}]")
        if clip.get("track") != "V1" or clip.get("slot_kind") != "cold_open":
            raise ColdOpenError("compiled cold-open video must be on V1")
        start, end = _validated_bounds(
            clip, f"compiled cold_open.clips[{position}]"
        )
        if start != cursor:
            relation = "overlap" if start < cursor else "gap"
            raise ColdOpenError(f"compiled cold-open V1 has a {relation} at frame {start}")
        if end > prefix_frames:
            raise ColdOpenError("compiled cold-open V1 extends past the prefix")
        cursor = end
        clip_id = _required_string(clip.get("id"), "cold-open clip id")
        if clip_id in clip_ids:
            raise ColdOpenError(f"duplicate cold-open clip id: {clip_id}")
        clip_ids.add(clip_id)
    if cursor != prefix_frames:
        raise ColdOpenError("compiled cold-open V1 does not fill the prefix")

    for position, raw in enumerate(audio):
        item = _mapping(raw, f"compiled cold_open.audio[{position}]")
        if item.get("track") not in {"A2", "A4"}:
            raise ColdOpenError("cold-open audio may use only A2 or A4")
        start, end = _validated_bounds(
            item, f"compiled cold_open.audio[{position}]"
        )
        if end > prefix_frames:
            raise ColdOpenError("compiled cold-open audio extends past the prefix")
        if item.get("track") == "A2":
            source_clip_id = _required_string(
                item.get("source_clip_id"), "cold-open A2 source_clip_id"
            )
            if source_clip_id not in clip_ids:
                raise ColdOpenError("cold-open A2 references an unknown V1 clip")
            source_audio_by_clip[source_clip_id] = item
            if _positive_integer(
                item.get("channels"), "cold-open A2 channels"
            ) <= 0 or _positive_integer(
                item.get("source_sample_rate"), "cold-open A2 sample rate"
            ) <= 0:
                raise ColdOpenError("cold-open A2 has invalid audio metadata")

    for raw in clips:
        clip = _mapping(raw, "compiled cold_open clip")
        if not clip.get("source_audio"):
            continue
        matched = source_audio_by_clip.get(str(clip["id"]))
        if matched is None:
            raise ColdOpenError("source-audio cold-open clip has no matched A2 item")
        for key in ("start_frame", "end_frame", "duration_frames", "source_start_frame"):
            if int(matched[key]) != int(clip[key]):
                raise ColdOpenError("cold-open A2 timing does not match its V1 clip")
        if matched.get("media_path") != clip.get("media_path"):
            raise ColdOpenError("cold-open A2 does not reuse its V1 media path")

    for position, raw in enumerate(media_inputs):
        item = _mapping(raw, f"compiled cold_open.media_inputs[{position}]")
        checksum = str(item.get("sha256") or "").lower()
        if not _SHA256_RE.fullmatch(checksum):
            raise ColdOpenError("cold-open media input has invalid SHA-256")


def _assert_clean_prefix(plan: Mapping[str, Any], prefix: Mapping[str, Any]) -> None:
    prefix_frames = int(prefix["prefix_frames"])
    cold_clip_ids = {str(item["id"]) for item in prefix["clips"]}
    cold_audio_ids = {str(item["id"]) for item in prefix["audio"]}

    for raw in plan.get("clips", []):
        item = _mapping(raw, "plan clip")
        if int(item["start_frame"]) < prefix_frames and str(item.get("id")) not in cold_clip_ids:
            raise ColdOpenError("non-cold video leaks into the cold-open prefix")
    for raw in plan.get("audio", []):
        item = _mapping(raw, "plan audio")
        if int(item["start_frame"]) >= prefix_frames:
            continue
        if str(item.get("id")) not in cold_audio_ids or item.get("track") not in {"A2", "A4"}:
            raise ColdOpenError("narration, music, or unauthored audio leaks into the cold open")
    for collection in ("subtitles", "upload_subtitles"):
        for raw in plan.get(collection, []):
            item = _mapping(raw, f"plan {collection} item")
            if int(item["start_frame"]) < prefix_frames:
                raise ColdOpenError("captions leak into the cold-open prefix")
    for raw in plan.get("overlays", []):
        item = _mapping(raw, "plan overlay")
        if int(item["start_frame"]) < prefix_frames:
            kind = str(item.get("kind") or "").lower()
            detail = "caption" if kind in _CAPTION_KINDS else "overlay"
            raise ColdOpenError(f"{detail} leaks into the cold-open prefix")


def _shift_interval_collection(
    plan: dict[str, Any], collection: str, offset: int
) -> None:
    values = plan.get(collection)
    if values is None:
        return
    if not isinstance(values, list):
        raise ColdOpenError(f"plan.{collection} must be a list")
    shifted: list[dict[str, Any]] = []
    for position, raw in enumerate(values):
        item = dict(_mapping(raw, f"plan.{collection}[{position}]"))
        _shift_interval(item, offset, f"plan.{collection}[{position}]")
        shifted.append(item)
    plan[collection] = shifted


def _shift_interval(item: dict[str, Any], offset: int, label: str) -> None:
    start, end = _validated_bounds(item, label)
    item["start_frame"] = start + offset
    item["end_frame"] = end + offset


def _shift_point_collection(
    plan: dict[str, Any], collection: str, field: str, offset: int
) -> None:
    values = plan.get(collection)
    if values is None:
        return
    if not isinstance(values, list):
        raise ColdOpenError(f"plan.{collection} must be a list")
    shifted: list[dict[str, Any]] = []
    for position, raw in enumerate(values):
        item = dict(_mapping(raw, f"plan.{collection}[{position}]"))
        if field not in item:
            raise ColdOpenError(f"plan.{collection}[{position}] has no {field}")
        item[field] = _plan_frame(
            item[field], f"plan.{collection}[{position}].{field}"
        ) + offset
        shifted.append(item)
    plan[collection] = shifted


def _validated_bounds(item: Mapping[str, Any], label: str) -> tuple[int, int]:
    start = _plan_frame(item.get("start_frame"), f"{label}.start_frame")
    end = _plan_frame(item.get("end_frame"), f"{label}.end_frame")
    if end <= start:
        raise ColdOpenError(f"{label} must have end_frame after start_frame")
    if item.get("duration_frames") is not None:
        duration = _plan_frame(item.get("duration_frames"), f"{label}.duration_frames")
        if duration <= 0 or end - start != duration:
            raise ColdOpenError(f"{label} has inconsistent duration_frames")
    return start, end


def _frames_from_aliases(
    item: Mapping[str, Any],
    *,
    frame_keys: Sequence[str],
    second_keys: Sequence[str],
    fps: int,
    label: str,
    required: bool = False,
    default: int | None = None,
) -> int:
    candidates: list[tuple[str, int]] = []
    for key in frame_keys:
        if key in item and item[key] is not None:
            candidates.append((key, _plan_frame(item[key], f"{label}.{key}")))
    for key in second_keys:
        if key in item and item[key] is not None:
            seconds = _finite_number(item[key], f"{label}.{key}")
            candidates.append((key, _seconds_to_frame(seconds, fps)))
    if not candidates:
        if required:
            raise ColdOpenError(f"{label} is required")
        if default is None:
            raise ColdOpenError(f"{label} has no default")
        return default
    first = candidates[0][1]
    conflicts = [key for key, value in candidates[1:] if value != first]
    if conflicts:
        names = ", ".join(key for key, _ in candidates)
        raise ColdOpenError(f"{label} aliases disagree: {names}")
    return first


def _one_collection_alias(
    item: Mapping[str, Any],
    keys: Sequence[str],
    *,
    label: str,
    required: bool,
    mapping_is_single: bool = False,
) -> list[Any]:
    present = [key for key in keys if item.get(key) is not None]
    if len(present) > 1:
        raise ColdOpenError(f"{label} aliases cannot be combined: {', '.join(present)}")
    if not present:
        if required:
            raise ColdOpenError(f"{label} is required")
        return []
    value = item[present[0]]
    if mapping_is_single and isinstance(value, Mapping):
        return [value]
    if not isinstance(value, list):
        raise ColdOpenError(f"{label} must be a list")
    return list(value)


def _one_value_alias(
    item: Mapping[str, Any],
    keys: Sequence[str],
    *,
    label: str,
    required: bool,
) -> Any:
    present = [key for key in keys if item.get(key) not in (None, "")]
    if not present:
        if required:
            raise ColdOpenError(f"{label} is required")
        return None
    values = {str(item[key]) for key in present}
    if len(values) > 1:
        raise ColdOpenError(f"{label} aliases disagree: {', '.join(present)}")
    return item[present[0]]


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ColdOpenError(f"{label} must be an object")
    return value


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ColdOpenError(f"{label} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ColdOpenError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ColdOpenError(f"{label} must be a finite number")
    return number


def _positive_integer(value: Any, label: str) -> int:
    integer = _plan_frame(value, label)
    if integer <= 0:
        raise ColdOpenError(f"{label} must be greater than zero")
    return integer


def _plan_frame(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ColdOpenError(f"{label} must be an integer frame")
    number = float(value)
    if not math.isfinite(number) or not number.is_integer():
        raise ColdOpenError(f"{label} must be an integer frame")
    integer = int(number)
    if integer < 0:
        raise ColdOpenError(f"{label} must be zero or greater")
    return integer


def _seconds_to_frame(seconds: float, fps: int) -> int:
    return int(
        (Decimal(str(seconds)) * Decimal(fps)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _rounded_average(values: Sequence[int]) -> int:
    if not values:
        return 0
    return int(
        (Decimal(sum(values)) / Decimal(len(values))).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _stable_id(prefix: str, payload: Any) -> str:
    return f"{prefix}-{_sha256_json(payload)[:12]}"


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

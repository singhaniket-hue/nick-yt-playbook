"""Deterministic compiler for Resolve hand-off bundles.

The compiler intentionally does not talk to DaVinci Resolve.  It turns the
canonical timing, EDL, and provenance documents into a relocatable JSON plan.
``write_resolve_bundle`` is the only API in this module that writes files.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import wave
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from rabbithole.audiomix import bed_spans, sfx_events, silence_windows
from rabbithole.resolve_audio import ResolveAudioStemError, load_current_audio_stems
from rabbithole.sources.music import resolve_cue
from rabbithole.sources.soundgen import (
    BED_EDGE_TRIM_SECONDS,
    CHANNELS as SOUND_CHANNELS,
    SAMPLE_RATE as SOUND_SAMPLE_RATE,
    Library,
)


SCHEMA_VERSION = "resolve-plan.v1"
COMPILER_VERSION = "resolve-compiler.v5"
DEFAULT_FPS = 30
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_SAMPLE_RATE = 48_000
_SOURCE_CAPTION_POLICY_VERSION = "source-caption.v1"

_VIDEO_TRACKS = (
    ("V1", "base_footage"),
    ("V2", "evidence_and_inserts"),
    ("V3", "text_and_graphics"),
    ("V4", "grain_and_texture"),
)
_AUDIO_TRACKS = (
    ("A1", "narration"),
    ("A2", "source_bites"),
    ("A3", "music"),
    ("A4", "sound_effects"),
    ("A5", "room_tone_and_utility"),
)
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
_SUBTITLE_KINDS = {"caption", "captions", "subtitle", "subtitles"}
_EVIDENCE_TERMS = {
    "document",
    "evidence",
    "insert",
    "map",
    "post",
    "screenshot",
    "source",
    "thread",
}
_V4_TERMS = {"film_grain", "grain", "texture", "vhs_overlay"}
_GRADE_ELIGIBLE_SLOT_KINDS = {"archival"}
_ATTRIBUTION_BURNED_PROVIDERS = {
    "rabbithole-evidence-card",
    "rabbithole-source-frame",
    "rabbithole-source-image",
}
_PROJECT_AUTHORED_PROVIDERS = {
    "rabbithole-cards",
    "rabbithole-evidence-card",
}
_AUDIO_KIND_TRACKS = {
    "dialogue": "A1",
    "narration": "A1",
    "voice": "A1",
    "voiceover": "A1",
    "vo": "A1",
    "source": "A2",
    "source_bite": "A2",
    "interview": "A2",
    "music": "A3",
    "score": "A3",
    "ambience": "A5",
    "ambient": "A5",
    "room_tone": "A5",
    "sfx": "A4",
    "sound_effect": "A4",
    "sound_effects": "A4",
    "reference": "A5",
    "scratch": "A5",
}


def _resolve_style_contract() -> dict[str, Any]:
    """Return the portable, versioned baseline used by the Resolve runner.

    Paths remain repository/project relative.  Their content hashes, rather
    than machine-specific absolute paths, participate in the build fingerprint.
    """

    repo = Path(__file__).resolve().parents[1]
    lut_path = repo / "style" / "luts" / "crowley-noir.cube"
    if not lut_path.is_file():
        raise ResolveManifestError(
            f"canonical Resolve LUT is missing: {lut_path}"
        )
    drx_path = repo / "resolve" / "grades" / "crowley_v1.drx"
    return {
        "schema_version": "resolve-style.v1",
        "grade": {
            "policy": "archival_v1_only",
            "grade_mode": 0,
            "eligible_slot_kinds": sorted(_GRADE_ELIGIBLE_SLOT_KINDS),
            "drx_path": "resolve/grades/crowley_v1.drx",
            "installed_drx_path": "RabbitHole/grades/crowley_v1.drx",
            "drx_sha256": _sha256_file(drx_path) if drx_path.is_file() else None,
            "lut_path": "style/luts/crowley-noir.cube",
            "installed_lut_path": "RabbitHole/crowley-noir.cube",
            "lut_sha256": _sha256_file(lut_path),
        },
        "fusion": {
            "chapter_title": "RabbitholeChapter",
            "source_title": "RabbitholeSource",
            "evidence_transition": "RabbitholeGlitch",
            "automation": "intent_only_api_placement_unavailable",
        },
        "texture": {
            "track": "V4",
            "grain_opacity": 0.18,
            "vignette_amount": 0.20,
            "automation": "editable_manual_intent",
        },
    }


class ResolveManifestError(ValueError):
    """Raised when canonical compiler input is absent or malformed."""


def _discover_sound_manifest(root: Path) -> Path | None:
    """Return an explicitly configured or conventional project-local library.

    Generated sound-library manifests store cue paths relative to their own
    directory.  The manifest itself must remain inside the episode so every
    compiled media path can be serialized relative to ``project_root`` and
    restored unchanged on Windows or macOS.
    """

    default = root / "assets" / "soundlib" / "manifest.json"
    brief_path = root / "brief.json"
    configured: Any = None
    if brief_path.is_file():
        brief = _load_json(brief_path)
        if isinstance(brief, Mapping):
            sound_design = brief.get("sound_design")
            if isinstance(sound_design, Mapping):
                configured = sound_design.get("manifest_path")

    if configured is None or configured == "":
        return default if default.is_file() else None
    if not isinstance(configured, str):
        raise ResolveManifestError(
            "brief.sound_design.manifest_path must be a project-relative string"
        )

    portable = _portable_string(configured)
    if _looks_absolute_portable(portable):
        raise ResolveManifestError(
            "brief.sound_design.manifest_path must stay project-relative: "
            f"{configured!r}"
        )
    parts = PurePosixPath(portable).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ResolveManifestError(
            "brief.sound_design.manifest_path contains an escaping or ambiguous "
            f"component: {configured!r}"
        )
    candidate = root.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ResolveManifestError(
            "brief.sound_design.manifest_path escapes the project root: "
            f"{configured!r}"
        ) from exc
    if not candidate.is_file():
        raise ResolveManifestError(
            f"configured sound manifest does not exist: {candidate}"
        )
    return candidate


def compile_resolve_plan(
    project_root: Path,
    *,
    fps: int = DEFAULT_FPS,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    overrides_path: Path | None = None,
) -> dict[str, Any]:
    """Compile canonical project files into a deterministic Resolve plan.

    Required files are auto-discovered at:

    * ``narration/timing.json``
    * ``edit/edl.json``
    * ``provenance.json``

    ``research/source-audio.json``, ``highlights.json``, and a project-local
    generated sound-library manifest are optional.
    ``overrides_path`` is an optional JSON object with the narrow override
    fields documented by ``schemas/resolve-overrides.v1.schema.json``.
    """

    root = Path(project_root).resolve()
    if not root.is_dir():
        raise ResolveManifestError(f"project root does not exist: {root}")
    _validate_format_settings(fps, width, height, sample_rate)

    paths: dict[str, Path] = {
        "timing": root / "narration" / "timing.json",
        "edl": root / "edit" / "edl.json",
        "provenance": root / "provenance.json",
    }
    optional_paths = {
        "source_audio": root / "research" / "source-audio.json",
        "highlights": root / "research" / "highlights.json",
    }
    if not optional_paths["highlights"].is_file():
        optional_paths["highlights"] = root / "highlights.json"
    sound_manifest_path = _discover_sound_manifest(root)
    if sound_manifest_path is not None:
        optional_paths["sound_manifest"] = sound_manifest_path
    audio_stems_path: Path | None = None
    if sound_manifest_path is not None:
        try:
            selected_stems = load_current_audio_stems(root, verify=True)
        except ResolveAudioStemError as exc:
            raise ResolveManifestError(str(exc)) from exc
        if selected_stems is not None:
            audio_stems_path, _ = selected_stems
            optional_paths["audio_stems"] = audio_stems_path
    missing_inputs = [str(path) for path in paths.values() if not path.is_file()]
    if missing_inputs:
        joined = ", ".join(missing_inputs)
        raise ResolveManifestError(f"required compiler input missing: {joined}")

    loaded: dict[str, Any] = {name: _load_json(path) for name, path in paths.items()}
    for name, path in optional_paths.items():
        if path.is_file():
            paths[name] = path
            loaded[name] = _load_json(path)

    if overrides_path is not None:
        override_file = Path(overrides_path)
        if not override_file.is_absolute():
            override_file = root / override_file
        override_file = override_file.resolve()
        if not override_file.is_file():
            raise ResolveManifestError(f"overrides file does not exist: {override_file}")
        paths["overrides"] = override_file
        loaded["overrides"] = _load_json(override_file)
    else:
        loaded["overrides"] = {}

    source_records: dict[str, dict[str, str]] = {}
    for name, path in sorted(paths.items()):
        source_records[name] = {
            "path": _path_for_plan(path, root)[0],
            "sha256": _sha256_file(path),
            "canonical_sha256": _sha256_json(loaded[name]),
        }

    return _compile_loaded(
        root,
        loaded["timing"],
        loaded["edl"],
        loaded["provenance"],
        source_audio=loaded.get("source_audio"),
        sound_manifest=loaded.get("sound_manifest"),
        sound_manifest_path=sound_manifest_path,
        audio_stems=loaded.get("audio_stems"),
        audio_stems_path=audio_stems_path,
        highlights=loaded.get("highlights"),
        overrides=loaded["overrides"],
        sources=source_records,
        fps=fps,
        width=width,
        height=height,
        sample_rate=sample_rate,
    )


def write_resolve_bundle(
    project_root: Path,
    output_dir: Path | None = None,
    *,
    fps: int = DEFAULT_FPS,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    overrides_path: Path | None = None,
    update_current: bool = True,
) -> dict[str, Any]:
    """Compile and atomically write a Resolve plan plus deterministic FCPXML.

    The return value is a plain dictionary so orchestration layers can serialize
    it directly.  Paths in the result are ``Path`` objects.
    """

    root = Path(project_root).resolve()
    plan = compile_resolve_plan(
        root,
        fps=fps,
        width=width,
        height=height,
        sample_rate=sample_rate,
        overrides_path=overrides_path,
    )
    if output_dir is None:
        destination = root / "resolve" / "builds" / plan["build_id"]
    else:
        candidate = Path(output_dir)
        destination = candidate if candidate.is_absolute() else root / candidate
        destination = destination.resolve()

    plan_path = destination / "resolve-plan.v1.json"
    fcpxml_path = destination / "timeline.fcpxml"
    portable_plan_path, plan_path_kind = _path_for_plan(plan_path, root)
    portable_xml_path, xml_path_kind = _path_for_plan(fcpxml_path, root)
    plan["output_paths"] = {
        "plan": portable_plan_path,
        "plan_path_kind": plan_path_kind,
        "fcpxml": portable_xml_path,
        "fcpxml_path_kind": xml_path_kind,
    }

    from .fcpxml import build_fcpxml

    fcpxml_text = build_fcpxml(plan, project_root=root)
    fcpxml_sha256 = _sha256_bytes(fcpxml_text.encode("utf-8"))
    plan["output_paths"]["fcpxml_sha256"] = fcpxml_sha256
    plan_text = _canonical_json(plan, pretty=True) + "\n"
    _atomic_write_text(plan_path, plan_text)
    _atomic_write_text(fcpxml_path, fcpxml_text)

    current_path: Path | None = None
    if update_current:
        current_path = root / "resolve" / "current.json"
        current = {
            "schema_version": "resolve-current.v1",
            "build_id": plan["build_id"],
            "timeline_name": plan["timeline_name"],
            "plan_path": portable_plan_path,
            "fcpxml_path": portable_xml_path,
            "fcpxml_sha256": fcpxml_sha256,
        }
        _atomic_write_text(current_path, _canonical_json(current, pretty=True) + "\n")

    return {
        "build_id": plan["build_id"],
        "timeline_name": plan["timeline_name"],
        "output_dir": destination,
        "plan_path": plan_path,
        "fcpxml_path": fcpxml_path,
        "current_path": current_path,
        "plan_sha256": _sha256_bytes(plan_text.encode("utf-8")),
        "fcpxml_sha256": fcpxml_sha256,
        "plan": plan,
    }


def _compile_loaded(
    root: Path,
    timing_raw: Any,
    edl_raw: Any,
    provenance_raw: Any,
    *,
    source_audio: Any,
    sound_manifest: Any,
    sound_manifest_path: Path | None,
    audio_stems: Any,
    audio_stems_path: Path | None,
    highlights: Any,
    overrides: Any,
    sources: Mapping[str, Mapping[str, str]],
    fps: int,
    width: int,
    height: int,
    sample_rate: int,
) -> dict[str, Any]:
    timing = _normalize_timing(timing_raw, fps)
    edl = _normalize_edl(edl_raw, fps)
    overrides_map = _normalize_overrides(overrides)
    provenance = _normalize_provenance(provenance_raw, root, overrides_map)
    slot_metadata = _slot_metadata(timing_raw)
    audio = _compile_audio(source_audio, root, fps, timing["duration_frames"])
    if audio_stems is not None and audio_stems_path is not None:
        if any(item.get("track") == "A2" for item in audio):
            raise ResolveManifestError(
                "prepared Resolve sound stems cannot be combined with "
                "research/source-audio.json yet: the approved FFmpeg mix also "
                "ducks narration and music around each source bite. Remove the "
                "source-audio bites or use the FFmpeg renderer until "
                "source-aware Resolve stems are implemented."
            )
        stem_audio = _compile_mixed_sound_stems(
            audio_stems,
            audio_stems_path,
            root,
            fps,
            timing["duration_frames"],
        )
        master_gain_db = stem_audio[0]["gain_db"] if stem_audio else 0.0
        for item in audio:
            if item.get("track") == "A1":
                item["gain_db"] = round(
                    float(item.get("gain_db", 0.0)) + master_gain_db,
                    4,
                )
        audio.extend(stem_audio)
    else:
        audio.extend(
            _compile_generated_sound_audio(
                timing_raw,
                sound_manifest,
                sound_manifest_path,
                root,
                fps,
                timing["duration_frames"],
            )
        )
    style_base = _resolve_style_contract()

    fingerprint_payload = {
        "compiler": COMPILER_VERSION,
        "source_caption_policy": _SOURCE_CAPTION_POLICY_VERSION,
        "schema_version": SCHEMA_VERSION,
        "fps": fps,
        "width": width,
        "height": height,
        "sample_rate": sample_rate,
        "style": style_base,
        "sources": {
            name: record["sha256"] for name, record in sorted(sources.items())
        },
        "media": {
            f"asset:{asset['asset_id']}:{asset['local_path'] or ''}": asset["sha256"]
            for asset in provenance
        }
        | {
            f"audio:{item['asset_id']}:{item['media_path'] or ''}": item["sha256"]
            for item in audio
        },
    }
    build_digest = _sha256_json(fingerprint_payload)
    build_id = f"b-{build_digest[:12]}"
    timeline_name = f"AUTO_BUILD_{build_digest[:12].upper()}"
    default_plan_path = f"resolve/builds/{build_id}/resolve-plan.v1.json"
    default_xml_path = f"resolve/builds/{build_id}/timeline.fcpxml"

    review_flags: list[dict[str, Any]] = []
    missing_media: list[dict[str, Any]] = []
    clips: list[dict[str, Any]] = []

    slot_assets = _asset_binding_index(provenance)
    for cut in edl["cuts"]:
        slot = slot_metadata.get(cut["slot_id"])
        if slot is not None:
            cut["slot_kind"] = slot["kind"]
            cut["slot_detail"] = slot["detail"]
            cut["source_start_frame"] = _seconds_to_frame(
                max(0.0, cut["start_seconds"] - slot["start"]), fps
            )
        else:
            cut["slot_kind"] = ""
            cut["slot_detail"] = ""
            cut["source_start_frame"] = 0
        candidates, binding = _bind_asset(cut["slot_id"], provenance, slot_assets)
        selected = candidates[0] if candidates else None
        if len(candidates) > 1:
            _add_review(
                review_flags,
                "ambiguous_asset_binding",
                "warning",
                cut["start_frame"],
                (
                    f"Slot {cut['slot_id']} matched {len(candidates)} assets via "
                    f"{binding}; selected {selected['asset_id']} deterministically."
                ),
                slot_id=cut["slot_id"],
                asset_id=selected["asset_id"],
            )

        track = _cut_track(cut, selected)
        override_track = _lookup_override(
            overrides_map.get("clip_tracks", {}), cut["slot_id"], str(cut["index"])
        )
        if override_track is not None:
            if override_track not in {"V1", "V2"}:
                raise ResolveManifestError(
                    f"clip track override for {cut['slot_id']} must be V1 or V2"
                )
            track = override_track

        transform = _transform_for_framing(cut["framing"])
        transform_override = _lookup_override(
            overrides_map.get("transforms", {}), cut["slot_id"], str(cut["index"])
        )
        if transform_override is not None:
            transform = _normalize_transform(transform_override)

        clip_payload = {
            "index": cut["index"],
            "slot_id": cut["slot_id"],
            "track": track,
            "start_frame": cut["start_frame"],
            "end_frame": cut["end_frame"],
            "duration_frames": cut["duration_frames"],
            "source_start_frame": cut["source_start_frame"],
            "source_end_frame": cut["source_start_frame"] + cut["duration_frames"],
            "asset_id": selected["asset_id"] if selected else None,
            "media_path": selected["local_path"] if selected else None,
            "media_type": selected["media_type"] if selected else None,
            "binding": binding,
            "origin": cut["origin"],
            "slot_kind": cut["slot_kind"],
            "framing": cut["framing"],
            "transform": transform,
            "transition": _normalize_transition(
                cut["transition"], cut["duration_frames"]
            ),
            "reason": cut["reason"],
        }
        clip_payload["id"] = _stable_id(
            "clip",
            {
                "index": cut["index"],
                "slot_id": cut["slot_id"],
                "start_frame": cut["start_frame"],
                "end_frame": cut["end_frame"],
                "asset_id": clip_payload["asset_id"],
            },
        )
        clips.append(clip_payload)

        if selected is None:
            missing = {
                "id": _stable_id(
                    "missing",
                    {"kind": "unbound_slot", "slot_id": cut["slot_id"]},
                ),
                "kind": "unbound_slot",
                "slot_id": cut["slot_id"],
                "asset_id": None,
                "path": None,
                "start_frame": cut["start_frame"],
            }
            missing_media.append(missing)
            _add_review(
                review_flags,
                "missing_media",
                "error",
                cut["start_frame"],
                f"No provenance asset binds to slot {cut['slot_id']}.",
                slot_id=cut["slot_id"],
            )
        elif not selected["exists"]:
            missing = {
                "id": _stable_id(
                    "missing",
                    {
                        "kind": "missing_file",
                        "asset_id": selected["asset_id"],
                        "path": selected["local_path"],
                    },
                ),
                "kind": "missing_file",
                "slot_id": cut["slot_id"],
                "asset_id": selected["asset_id"],
                "path": selected["local_path"],
                "start_frame": cut["start_frame"],
            }
            missing_media.append(missing)
            _add_review(
                review_flags,
                "missing_media",
                "error",
                cut["start_frame"],
                (
                    f"Asset {selected['asset_id']} is bound to "
                    f"{cut['slot_id']} but its file is missing."
                ),
                slot_id=cut["slot_id"],
                asset_id=selected["asset_id"],
            )

        if selected is not None and _is_evidence(cut, selected):
            _add_review(
                review_flags,
                "evidence_verification",
                "human",
                cut["start_frame"],
                f"Verify evidence asset {selected['asset_id']} against its source.",
                slot_id=cut["slot_id"],
                asset_id=selected["asset_id"],
            )
            if _looks_generated(selected):
                _add_review(
                    review_flags,
                    "generated_evidence",
                    "error",
                    cut["start_frame"],
                    (
                        f"Generated asset {selected['asset_id']} is classified "
                        "as evidence."
                    ),
                    slot_id=cut["slot_id"],
                    asset_id=selected["asset_id"],
                )

        transition_kind = clip_payload["transition"]["kind"]
        if transition_kind not in {
            "cut",
            "cross_dissolve",
            "dip_to_black",
            "fade",
            "none",
        }:
            _add_review(
                review_flags,
                "manual_transition",
                "human",
                cut["end_frame"],
                (
                    f"Transition {transition_kind!r} on slot {cut['slot_id']} "
                    "requires a Resolve preset or manual pass."
                ),
                slot_id=cut["slot_id"],
                asset_id=clip_payload["asset_id"],
            )
        elif (
            transition_kind not in {"cut", "none"}
            and len(clips) > 1
            and clips[-2]["track"] != clip_payload["track"]
        ):
            _add_review(
                review_flags,
                "manual_cross_track_transition",
                "human",
                cut["start_frame"],
                (
                    f"Transition {transition_kind!r} into slot "
                    f"{cut['slot_id']} crosses {clips[-2]['track']} to "
                    f"{clip_payload['track']} and requires a manual Resolve pass."
                ),
                slot_id=cut["slot_id"],
                asset_id=clip_payload["asset_id"],
            )

    markers = _compile_markers(timing["markers"], fps)
    for marker in markers:
        if marker["kind"] in {"heavy", "deepest", "deepest_point"}:
            _add_review(
                review_flags,
                "heavy_timing",
                "human",
                marker["frame"],
                "Review the timing and stillness of this heavy story beat.",
            )
        if "redact" in marker["kind"]:
            _add_review(
                review_flags,
                "redaction_verification",
                "human",
                marker["frame"],
                "Verify redaction placement and tracking.",
            )

    overlays, explicit_subtitles = _compile_overlays(edl["overlays"], fps)
    overlays.extend(
        _compile_source_caption_overlays(clips, provenance, overlays)
    )
    for overlay in overlays:
        if "redact" in overlay["kind"]:
            _add_review(
                review_flags,
                "redaction_verification",
                "human",
                overlay["start_frame"],
                "Verify redaction placement and tracking.",
            )
        if overlay["kind"] in {"heavy", "deepest", "deepest_point"}:
            _add_review(
                review_flags,
                "heavy_timing",
                "human",
                overlay["start_frame"],
                "Review the timing of this heavy story beat.",
            )

    subtitles = (
        explicit_subtitles
        if explicit_subtitles
        else _subtitles_from_words(timing["words"], fps)
    )
    for audio_clip in audio:
        if audio_clip["media_path"] and not audio_clip["exists"]:
            missing = {
                "id": _stable_id(
                    "missing",
                    {
                        "kind": "missing_audio_file",
                        "asset_id": audio_clip["asset_id"],
                        "path": audio_clip["media_path"],
                    },
                ),
                "kind": "missing_audio_file",
                "slot_id": None,
                "asset_id": audio_clip["asset_id"],
                "path": audio_clip["media_path"],
                "start_frame": audio_clip["start_frame"],
            }
            missing_media.append(missing)
            _add_review(
                review_flags,
                "missing_audio",
                "error",
                audio_clip["start_frame"],
                f"Audio file is missing: {audio_clip['media_path']}.",
                asset_id=audio_clip["asset_id"],
            )

    compiled_highlights = _compile_highlights(highlights, fps)
    for highlight in compiled_highlights:
        markers.append(
            {
                "id": _stable_id(
                    "marker",
                    {
                        "kind": "highlight",
                        "frame": highlight["start_frame"],
                        "text": highlight["text"],
                    },
                ),
                "kind": "highlight",
                "arg": highlight["text"],
                "word_index": None,
                "line": highlight["line"],
                "frame": highlight["start_frame"],
            }
        )

    timing_duration = timing["duration_frames"]
    edl_duration = edl["duration_frames"]
    max_cut_end = max((clip["end_frame"] for clip in clips), default=0)
    max_audio_end = max((item["end_frame"] for item in audio), default=0)
    duration_frames = max(timing_duration, edl_duration, max_cut_end, max_audio_end)
    if abs(timing_duration - edl_duration) > 1:
        _add_review(
            review_flags,
            "duration_mismatch",
            "warning",
            0,
            (
                f"Timing duration ({timing_duration} frames) and EDL duration "
                f"({edl_duration} frames) differ."
            ),
        )
    if edl["declared_cut_count"] != len(clips):
        _add_review(
            review_flags,
            "cut_count_mismatch",
            "warning",
            0,
            (
                f"EDL declares {edl['declared_cut_count']} cuts but contains "
                f"{len(clips)}."
            ),
        )
    actual_average = (
        int(
            (
                Decimal(sum(clip["duration_frames"] for clip in clips))
                / Decimal(len(clips))
            ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        if clips
        else 0
    )
    declared_average = _seconds_to_frame(edl["average_shot_length"], fps)
    if clips and abs(actual_average - declared_average) > 1:
        _add_review(
            review_flags,
            "average_shot_length_mismatch",
            "warning",
            0,
            (
                f"EDL average shot length is {declared_average} frames; "
                f"compiled cuts average {actual_average} frames."
            ),
        )

    active_asset_ids = {
        str(clip["asset_id"])
        for clip in clips
        if clip.get("asset_id") and clip.get("media_path")
    }
    for asset in provenance:
        if asset["path_kind"] == "external-absolute":
            _add_review(
                review_flags,
                "external_media_path",
                "warning",
                0,
                (
                    f"Asset {asset['asset_id']} is outside the project root and "
                    "will need relinking after relocation."
                ),
                asset_id=asset["asset_id"],
            )
        if (
            asset["asset_id"] in active_asset_ids
            and not asset["license"]
            and asset["provider"].lower() not in _PROJECT_AUTHORED_PROVIDERS
        ):
            _add_review(
                review_flags,
                "license_missing",
                "warning",
                0,
                f"Asset {asset['asset_id']} has no recorded license.",
                asset_id=asset["asset_id"],
            )

    markers.sort(key=lambda item: (item["frame"], item["kind"], item["id"]))
    clips.sort(key=lambda item: (item["start_frame"], item["index"], item["id"]))
    overlays.sort(key=lambda item: (item["start_frame"], item["track"], item["id"]))
    subtitles.sort(key=lambda item: (item["start_frame"], item["id"]))
    audio.sort(key=lambda item: (item["track"], item["start_frame"], item["id"]))
    missing_media.sort(
        key=lambda item: (
            item["start_frame"],
            item["kind"],
            item.get("asset_id") or "",
            item["id"],
        )
    )
    review_flags = _finalize_reviews(
        review_flags, overrides_map.get("review_resolutions", {})
    )

    media_root = overrides_map.get("media_root") or _infer_media_root(
        [asset["local_path"] for asset in provenance if asset["local_path"]]
        + [item["media_path"] for item in audio if item["media_path"]]
    )
    media_root = _portable_string(str(media_root))

    normalized_sources = {
        name: dict(record) for name, record in sorted(sources.items())
    }
    checksums = {
        f"{name}_sha256": record["sha256"]
        for name, record in normalized_sources.items()
    }
    checksums["build_fingerprint_sha256"] = build_digest
    style = _json_safe(style_base)
    style["grade"]["clip_ids"] = [
        clip["id"]
        for clip in clips
        if clip["track"] == "V1"
        and str(clip.get("slot_kind") or "").lower()
        in _GRADE_ELIGIBLE_SLOT_KINDS
        and clip.get("media_path")
    ]
    style["contract_sha256"] = _sha256_json(style)

    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "compiler_version": COMPILER_VERSION,
        "build_id": build_id,
        "timeline_name": timeline_name,
        "project_root": ".",
        "media_root": media_root,
        "output_paths": {
            "plan": default_plan_path,
            "plan_path_kind": "project-relative",
            "fcpxml": default_xml_path,
            "fcpxml_path_kind": "project-relative",
        },
        "fps": fps,
        "resolution": {"width": width, "height": height},
        "sample_rate": sample_rate,
        "duration_frames": duration_frames,
        "render": {
            "format": "mp4",
            "codec": "H264",
            "mode": "single_clip",
            "settings": {
                "SelectAllFrames": True,
                "ExportVideo": True,
                "ExportAudio": True,
                "FormatWidth": width,
                "FormatHeight": height,
                "FrameRate": fps,
                "PixelAspectRatio": "square",
                "VideoQuality": 0,
                "AudioCodec": "aac",
                "AudioSampleRate": sample_rate,
                "ColorSpaceTag": "Same as Project",
                "GammaTag": "Same as Project",
                "NetworkOptimization": True,
                "ReplaceExistingFilesInPlace": False,
                "ExportSubtitle": True,
                "SubtitleFormat": "BurnIn",
            },
        },
        "timeline_validation": {
            "start_frame": 0,
            "end_frame": duration_frames,
            "video_clip_count": sum(
                1 for clip in clips if clip.get("media_path")
            ),
            "video_title_count": sum(
                1
                for overlay in overlays
                if overlay.get("text")
                and overlay.get("kind") not in _SUBTITLE_KINDS
            ),
            "audio_clip_count": sum(
                1 for clip in audio if clip.get("media_path")
            ),
            "subtitle_count": len(subtitles),
        },
        "style": style,
        "quality": edl["quality"],
        "mode": edl["mode"],
        "sources": normalized_sources,
        "checksums": checksums,
        "tracks": {
            "video": [
                {"id": track_id, "index": index, "intent": intent}
                for index, (track_id, intent) in enumerate(_VIDEO_TRACKS, start=1)
            ],
            "audio": [
                {"id": track_id, "index": index, "intent": intent}
                for index, (track_id, intent) in enumerate(_AUDIO_TRACKS, start=1)
            ],
        },
        "statistics": {
            "word_count": timing["word_count"],
            "cut_count": len(clips),
            "average_shot_length_frames": actual_average,
        },
        "timing": {
            "duration_frames": timing["duration_frames"],
            "word_count": timing["word_count"],
            "words": timing["words"],
        },
        "clips": clips,
        "markers": markers,
        "subtitles": subtitles,
        "overlays": overlays,
        "audio": audio,
        "provenance": provenance,
        "highlights": compiled_highlights,
        "missing_media": missing_media,
        "review_flags": review_flags,
    }
    return plan


def _normalize_timing(raw: Any, fps: int) -> dict[str, Any]:
    obj = _require_mapping(raw, "timing")
    duration_seconds = _nonnegative_number(
        obj.get("duration_seconds"), "timing.duration_seconds"
    )
    words_raw = _require_list(obj.get("words"), "timing.words")
    words: list[dict[str, Any]] = []
    seen_indices: set[int] = set()
    for position, value in enumerate(words_raw):
        if isinstance(value, Mapping):
            index = _integer(value.get("index"), f"timing.words[{position}].index")
            text = value.get("word")
            start = value.get("start")
            end = value.get("end")
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if len(value) != 4:
                raise ResolveManifestError(
                    f"timing.words[{position}] array must have four values"
                )
            index, text, start, end = value
            index = _integer(index, f"timing.words[{position}][0]")
        else:
            raise ResolveManifestError(
                f"timing.words[{position}] must be an object or four-item array"
            )
        if index in seen_indices:
            raise ResolveManifestError(f"duplicate timing word index: {index}")
        seen_indices.add(index)
        if not isinstance(text, str):
            raise ResolveManifestError(
                f"timing.words[{position}].word must be a string"
            )
        start_seconds = _nonnegative_number(
            start, f"timing.words[{position}].start"
        )
        end_seconds = _nonnegative_number(end, f"timing.words[{position}].end")
        if end_seconds < start_seconds:
            raise ResolveManifestError(
                f"timing.words[{position}] ends before it starts"
            )
        start_frame = _seconds_to_frame(start_seconds, fps)
        end_frame = _end_frame(start_seconds, end_seconds, fps)
        words.append(
            {
                "index": index,
                "word": text,
                "start_frame": start_frame,
                "end_frame": end_frame,
            }
        )
    words.sort(key=lambda item: (item["index"], item["start_frame"]))

    declared_word_count = _integer(obj.get("word_count"), "timing.word_count")
    if declared_word_count != len(words):
        raise ResolveManifestError(
            "timing.word_count does not match the number of timing.words"
        )

    markers_raw = _require_list(obj.get("markers"), "timing.markers")
    markers: list[dict[str, Any]] = []
    for position, value in enumerate(markers_raw):
        marker = _require_mapping(value, f"timing.markers[{position}]")
        kind = _required_string(marker.get("kind"), f"timing.markers[{position}].kind")
        seconds = _nonnegative_number(
            marker.get("seconds"), f"timing.markers[{position}].seconds"
        )
        word_index = marker.get("word_index")
        if word_index is not None:
            word_index = _integer(
                word_index, f"timing.markers[{position}].word_index"
            )
        line = marker.get("line")
        if line is not None:
            line = _integer(line, f"timing.markers[{position}].line")
        markers.append(
            {
                "kind": _slug(kind),
                "arg": _json_safe(marker.get("arg")),
                "word_index": word_index,
                "line": line,
                "seconds": seconds,
            }
        )

    return {
        "duration_frames": _seconds_to_frame(duration_seconds, fps),
        "word_count": declared_word_count,
        "words": words,
        "markers": markers,
    }


def _normalize_edl(raw: Any, fps: int) -> dict[str, Any]:
    obj = _require_mapping(raw, "edl")
    quality = _required_string(obj.get("quality"), "edl.quality")
    mode = _required_string(obj.get("mode"), "edl.mode")
    duration_seconds = _nonnegative_number(
        obj.get("duration_seconds"), "edl.duration_seconds"
    )
    cuts_raw = _require_list(obj.get("cuts"), "edl.cuts")
    cuts: list[dict[str, Any]] = []
    seen_indices: set[int] = set()
    for position, value in enumerate(cuts_raw):
        cut = _require_mapping(value, f"edl.cuts[{position}]")
        index = _integer(cut.get("index"), f"edl.cuts[{position}].index")
        if index in seen_indices:
            raise ResolveManifestError(f"duplicate EDL cut index: {index}")
        seen_indices.add(index)
        start_seconds = _nonnegative_number(
            cut.get("start"), f"edl.cuts[{position}].start"
        )
        end_seconds = _nonnegative_number(
            cut.get("end"), f"edl.cuts[{position}].end"
        )
        if end_seconds <= start_seconds:
            raise ResolveManifestError(
                f"edl.cuts[{position}] must have end greater than start"
            )
        start_frame = _seconds_to_frame(start_seconds, fps)
        end_frame = _end_frame(start_seconds, end_seconds, fps)
        cuts.append(
            {
                "index": index,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "duration_frames": end_frame - start_frame,
                "start_seconds": start_seconds,
                "slot_id": _required_string(
                    cut.get("slot_id"), f"edl.cuts[{position}].slot_id"
                ),
                "origin": _string_or_empty(cut.get("origin")),
                "framing": _json_safe(cut.get("framing")),
                "transition": _json_safe(cut.get("transition")),
                "reason": _string_or_empty(cut.get("reason")),
            }
        )
    overlays_raw = _require_list(obj.get("overlays"), "edl.overlays")
    overlays: list[dict[str, Any]] = []
    for position, value in enumerate(overlays_raw):
        overlay = _require_mapping(value, f"edl.overlays[{position}]")
        start_seconds = _nonnegative_number(
            overlay.get("start"), f"edl.overlays[{position}].start"
        )
        end_seconds = _nonnegative_number(
            overlay.get("end"), f"edl.overlays[{position}].end"
        )
        if end_seconds <= start_seconds:
            raise ResolveManifestError(
                f"edl.overlays[{position}] must have end greater than start"
            )
        overlays.append(
            {
                "kind": _required_string(
                    overlay.get("kind"), f"edl.overlays[{position}].kind"
                ),
                "start": start_seconds,
                "end": end_seconds,
                "text": _string_or_empty(overlay.get("text")),
                "detail": _json_safe(overlay.get("detail")),
            }
        )
    return {
        "quality": quality,
        "mode": mode,
        "duration_frames": _seconds_to_frame(duration_seconds, fps),
        "declared_cut_count": _integer(obj.get("cut_count"), "edl.cut_count"),
        "average_shot_length": _nonnegative_number(
            obj.get("average_shot_length"), "edl.average_shot_length"
        ),
        "cuts": cuts,
        "overlays": overlays,
    }


def _catalogue_metadata(notes: Any) -> dict[str, str]:
    """Recover the structured artifact fields carried in ledger notes.

    Asset acquisition serializes the originating artifact as Python-literal
    values separated by semicolons. Split only outside quotes so punctuation
    in a title or rights note cannot corrupt the fields that follow it.
    """

    if not isinstance(notes, str):
        return {}
    marker = "catalogue metadata:"
    marker_at = notes.find(marker)
    if marker_at < 0:
        return {}
    payload = notes[marker_at + len(marker):].strip()
    fields: list[str] = []
    start = 0
    quote_char = ""
    escaped = False
    for index, char in enumerate(payload):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote_char:
            escaped = True
            continue
        if char in {"'", '"'}:
            if not quote_char:
                quote_char = char
            elif quote_char == char:
                quote_char = ""
            continue
        if char == ";" and not quote_char:
            fields.append(payload[start:index].strip())
            start = index + 1
    fields.append(payload[start:].strip())

    result: dict[str, str] = {}
    for field in fields:
        key, separator, raw_value = field.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            continue
        raw_value = raw_value.strip()
        try:
            value = ast.literal_eval(raw_value)
        except (SyntaxError, ValueError):
            continue
        if isinstance(value, str):
            result[key] = value
    return result


def _clean_source_value(value: Any, *, max_length: int) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if not isinstance(value, (str, int, float)):
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    if (
        not text
        or len(text) > max_length
        or text.lower()
        in {"n/a", "none", "not recorded", "null", "tbd", "undated", "unknown"}
    ):
        return ""
    return text


def _true_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False


def _source_metadata(item: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize attribution metadata without treating retrieval as publication."""

    nested = item.get("source")
    source = nested if isinstance(nested, Mapping) else {}
    catalogue = _catalogue_metadata(item.get("notes"))

    def first_value(
        candidates: Sequence[Any], *, max_length: int
    ) -> str:
        for candidate in candidates:
            value = _clean_source_value(candidate, max_length=max_length)
            if value:
                return value
        return ""

    title = first_value(
        (
            source.get("attribution"),
            source.get("title"),
            item.get("source_attribution"),
            item.get("source_title"),
            item.get("title"),
            catalogue.get("source_attribution"),
            catalogue.get("title"),
        ),
        max_length=180,
    )
    # Intentionally omit retrieved_at. It is acquisition audit metadata, never
    # a publication/upload date.
    date = first_value(
        (
            source.get("date_label"),
            source.get("date"),
            source.get("publication_date"),
            item.get("source_date_label"),
            item.get("source_date"),
            item.get("publication_date"),
            item.get("date"),
            catalogue.get("source_date_label"),
            catalogue.get("date"),
        ),
        max_length=80,
    )
    role = first_value(
        (
            source.get("role"),
            item.get("source_role"),
            catalogue.get("source_role"),
        ),
        max_length=80,
    )
    provider = _clean_source_value(item.get("provider"), max_length=120).lower()
    burned = (
        provider in _ATTRIBUTION_BURNED_PROVIDERS
        or _true_flag(source.get("attribution_burned"))
        or _true_flag(item.get("source_attribution_burned"))
        or _true_flag(item.get("attribution_burned"))
    )
    return {
        "title": title,
        "date": date,
        "role": role,
        "attribution_burned": burned,
    }


def _normalize_provenance(
    raw: Any, root: Path, overrides: Mapping[str, Any]
) -> list[dict[str, Any]]:
    values = _require_list(raw, "provenance")
    path_overrides = overrides.get("asset_paths", {})
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for position, value in enumerate(values):
        item = _require_mapping(value, f"provenance[{position}]")
        asset_id = _required_string(
            item.get("asset_id"), f"provenance[{position}].asset_id"
        )
        if asset_id in seen_ids:
            raise ResolveManifestError(f"duplicate provenance asset_id: {asset_id}")
        seen_ids.add(asset_id)
        raw_path = path_overrides.get(asset_id, item.get("local_path"))
        local_path = _string_or_empty(raw_path)
        portable_path, path_kind = _path_for_plan(local_path, root)
        resolved_path = _resolve_media_path(local_path, root)
        exists = bool(resolved_path and resolved_path.is_file())
        source_metadata = _source_metadata(item)
        used = item.get("used_in_slots")
        if isinstance(used, str):
            used_in_slots = [used]
        else:
            used_in_slots = _require_list(
                used, f"provenance[{position}].used_in_slots"
            )
            if not all(isinstance(slot, str) and slot for slot in used_in_slots):
                raise ResolveManifestError(
                    f"provenance[{position}].used_in_slots must contain strings"
                )
        result.append(
            {
                "asset_id": asset_id,
                "tier": _string_or_empty(item.get("tier")),
                "provider": _string_or_empty(item.get("provider")),
                "original_url": _string_or_empty(item.get("original_url")),
                "license": _string_or_empty(item.get("license")),
                "retrieved_at": _string_or_empty(item.get("retrieved_at")),
                "source_title": source_metadata["title"],
                "source_date": source_metadata["date"],
                "source_role": source_metadata["role"],
                "source_attribution_burned": source_metadata[
                    "attribution_burned"
                ],
                "local_path": portable_path or None,
                "path_kind": path_kind,
                "media_type": _media_type(portable_path),
                "exists": exists,
                "sha256": _sha256_file(resolved_path) if exists and resolved_path else None,
                "used_in_slots": sorted(set(used_in_slots)),
                "notes": _json_safe(item.get("notes")),
            }
        )
    result.sort(key=lambda item: (item["asset_id"], item["local_path"] or ""))
    return result


def _normalize_overrides(raw: Any) -> dict[str, Any]:
    if raw in (None, {}):
        return {}
    obj = _require_mapping(raw, "overrides")
    allowed = {
        "asset_paths",
        "clip_tracks",
        "media_root",
        "review_resolutions",
        "transforms",
    }
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise ResolveManifestError(
            f"unsupported override field(s): {', '.join(unknown)}"
        )
    normalized: dict[str, Any] = {}
    for key in ("asset_paths", "clip_tracks", "review_resolutions", "transforms"):
        if key in obj:
            normalized[key] = dict(_require_mapping(obj[key], f"overrides.{key}"))
    if "media_root" in obj:
        normalized["media_root"] = _required_string(
            obj["media_root"], "overrides.media_root"
        )
    return normalized


def _slot_metadata(timing_raw: Any) -> dict[str, dict[str, Any]]:
    """Return canonical slot starts/kinds without duplicating slot semantics."""

    from .slots import build_slots

    timing = _require_mapping(timing_raw, "timing")
    return {
        slot.slot_id: {
            "start": float(slot.start),
            "end": float(slot.end),
            "kind": slot.kind,
            "detail": slot.detail,
        }
        for slot in build_slots(dict(timing))
    }


def _asset_binding_index(
    provenance: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for asset in provenance:
        for slot in asset["used_in_slots"]:
            index.setdefault(slot, []).append(dict(asset))
    for assets in index.values():
        assets.sort(key=lambda item: (item["asset_id"], item["local_path"] or ""))
    return index


def _bind_asset(
    slot_id: str,
    provenance: Sequence[dict[str, Any]],
    slot_assets: Mapping[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], str]:
    direct = slot_assets.get(slot_id, [])
    if direct:
        return list(direct), "used_in_slots"
    normalized_slot = _binding_token(slot_id)
    suffix_matches = [
        asset
        for asset in provenance
        if _binding_token(asset["asset_id"]).endswith(normalized_slot)
    ]
    suffix_matches.sort(
        key=lambda item: (
            len(_binding_token(item["asset_id"])) - len(normalized_slot),
            item["asset_id"],
            item["local_path"] or "",
        )
    )
    if suffix_matches:
        return suffix_matches, "asset_id_suffix"
    return [], "unbound"


def _cut_track(
    cut: Mapping[str, Any], asset: Mapping[str, Any] | None
) -> str:
    haystack = " ".join(
        [
            _string_or_empty(cut.get("origin")),
            _string_or_empty(cut.get("framing")),
            _string_or_empty(asset.get("tier") if asset else ""),
            _string_or_empty(asset.get("provider") if asset else ""),
            _string_or_empty(asset.get("asset_id") if asset else ""),
            _string_or_empty(asset.get("notes") if asset else ""),
            _string_or_empty(cut.get("slot_kind")),
            _string_or_empty(cut.get("slot_detail")),
        ]
    ).lower()
    return "V2" if any(term in haystack for term in _EVIDENCE_TERMS) else "V1"


def _transform_for_framing(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return _normalize_transform(raw)
    value = _slug(_string_or_empty(raw))
    scale = 1.0
    if value in {"close", "close_up", "closeup", "ken_burns", "push_in"}:
        scale = 1.08
    elif value in {"medium", "medium_close", "medium_close_up"}:
        scale = 1.04
    return {
        "scale_x": scale,
        "scale_y": scale,
        "position_x": 0.0,
        "position_y": 0.0,
        "rotation": 0.0,
    }


def _normalize_transform(raw: Any) -> dict[str, Any]:
    obj = _require_mapping(raw, "transform")
    scale = obj.get("scale", 1.0)
    if isinstance(scale, Sequence) and not isinstance(scale, (str, bytes)):
        if len(scale) != 2:
            raise ResolveManifestError("transform.scale array must contain x and y")
        scale_x = _finite_number(scale[0], "transform.scale[0]")
        scale_y = _finite_number(scale[1], "transform.scale[1]")
    else:
        scale_x = scale_y = _finite_number(scale, "transform.scale")
    if scale_x > 10:
        scale_x /= 100.0
    if scale_y > 10:
        scale_y /= 100.0
    position = obj.get("position")
    if isinstance(position, Sequence) and not isinstance(position, (str, bytes)):
        if len(position) != 2:
            raise ResolveManifestError(
                "transform.position array must contain x and y"
            )
        position_x = _finite_number(position[0], "transform.position[0]")
        position_y = _finite_number(position[1], "transform.position[1]")
    else:
        position_x = _finite_number(obj.get("x", 0), "transform.x")
        position_y = _finite_number(obj.get("y", 0), "transform.y")
    return {
        "scale_x": scale_x,
        "scale_y": scale_y,
        "position_x": position_x,
        "position_y": position_y,
        "rotation": _finite_number(obj.get("rotation", 0), "transform.rotation"),
    }


def _normalize_transition(raw: Any, clip_duration: int) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        kind = _slug(_string_or_empty(raw.get("kind") or raw.get("type") or "cut"))
        frames_value = raw.get("duration_frames")
        if frames_value is None and raw.get("duration_seconds") is not None:
            frames_value = _seconds_to_frame(
                _nonnegative_number(
                    raw["duration_seconds"], "transition.duration_seconds"
                ),
                DEFAULT_FPS,
            )
        duration_frames = (
            _integer(frames_value, "transition.duration_frames")
            if frames_value is not None
            else _default_transition_frames(kind)
        )
    else:
        kind = _slug(_string_or_empty(raw) or "cut")
        duration_frames = _default_transition_frames(kind)
    aliases = {
        "crossfade": "cross_dissolve",
        "dissolve": "cross_dissolve",
        "dip": "dip_to_black",
        "hard": "cut",
        "hard_cut": "cut",
    }
    kind = aliases.get(kind, kind)
    duration_frames = max(0, min(duration_frames, max(0, clip_duration // 2)))
    return {"kind": kind, "duration_frames": duration_frames}


def _default_transition_frames(kind: str) -> int:
    if kind in {"cross_dissolve", "crossfade", "dissolve", "fade"}:
        return 15
    if kind in {"dip", "dip_to_black"}:
        return 30
    return 0


def _compile_markers(
    raw_markers: Sequence[Mapping[str, Any]], fps: int
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for marker in raw_markers:
        payload = {
            "kind": marker["kind"],
            "arg": marker["arg"],
            "word_index": marker["word_index"],
            "line": marker["line"],
            "frame": _seconds_to_frame(marker["seconds"], fps),
        }
        payload["id"] = _stable_id("marker", payload)
        result.append(payload)
    return result


def _compile_overlays(
    raw_overlays: Sequence[Mapping[str, Any]], fps: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    overlays: list[dict[str, Any]] = []
    subtitles: list[dict[str, Any]] = []
    for position, raw in enumerate(raw_overlays):
        kind = _slug(raw["kind"])
        start_frame = _seconds_to_frame(raw["start"], fps)
        end_frame = _end_frame(raw["start"], raw["end"], fps)
        track = "V4" if kind in _V4_TERMS else "V3"
        payload = {
            "kind": kind,
            "track": track,
            "start_frame": start_frame,
            "end_frame": end_frame,
            "duration_frames": end_frame - start_frame,
            "text": raw["text"],
            "detail": raw["detail"],
        }
        payload["id"] = _stable_id("overlay", {"position": position, **payload})
        overlays.append(payload)
        if kind in _SUBTITLE_KINDS and payload["text"]:
            subtitle = {
                "id": _stable_id(
                    "subtitle",
                    {
                        "position": position,
                        "start_frame": start_frame,
                        "end_frame": end_frame,
                        "text": payload["text"],
                    },
                ),
                "track": "V3",
                "start_frame": start_frame,
                "end_frame": end_frame,
                "duration_frames": end_frame - start_frame,
                "text": payload["text"],
                "source": "edl_overlay",
                "editable": True,
            }
            subtitles.append(subtitle)
    return overlays, subtitles


def _source_caption_text(asset: Mapping[str, Any]) -> str:
    if (
        asset.get("media_type") != "video"
        or not _string_or_empty(asset.get("original_url")).strip()
        or bool(asset.get("source_attribution_burned"))
    ):
        return ""

    title = _clean_source_value(asset.get("source_title"), max_length=180)
    if not title:
        provider = _clean_source_value(asset.get("provider"), max_length=120)
        if provider.lower() not in {"", "local", "unknown"}:
            title = provider
        else:
            title = urlparse(
                _string_or_empty(asset.get("original_url"))
            ).netloc
    if not title:
        return ""
    date = _clean_source_value(asset.get("source_date"), max_length=80)
    parts = ("SOURCE", title, date)
    return " · ".join(part for part in parts if part)


def _uncovered_caption_spans(
    start_frame: int,
    end_frame: int,
    authored: Sequence[tuple[int, int]],
) -> list[tuple[int, int]]:
    cursor = start_frame
    uncovered: list[tuple[int, int]] = []
    for covered_start, covered_end in authored:
        if covered_end <= cursor or covered_start >= end_frame:
            continue
        if covered_start > cursor:
            uncovered.append((cursor, min(covered_start, end_frame)))
        cursor = max(cursor, covered_end)
        if cursor >= end_frame:
            break
    if cursor < end_frame:
        uncovered.append((cursor, end_frame))
    return [(start, end) for start, end in uncovered if end > start]


def _compile_source_caption_overlays(
    clips: Sequence[Mapping[str, Any]],
    provenance: Sequence[Mapping[str, Any]],
    authored_overlays: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build one editable V3 attribution per contiguous direct-source span."""

    assets = {str(asset["asset_id"]): asset for asset in provenance}
    candidates: list[dict[str, Any]] = []
    for clip in clips:
        asset_id = _string_or_empty(clip.get("asset_id"))
        asset = assets.get(asset_id)
        if asset is None:
            continue
        text = _source_caption_text(asset)
        if not text:
            continue
        candidates.append(
            {
                "asset_id": asset_id,
                "start_frame": int(clip["start_frame"]),
                "end_frame": int(clip["end_frame"]),
                "source_start_frame": int(clip.get("source_start_frame", 0)),
                "source_end_frame": int(clip.get("source_end_frame", 0)),
                "text": text,
                "source_title": asset.get("source_title") or "",
                "source_date": asset.get("source_date") or "",
                "clip_ids": [str(clip["id"])],
                "slot_ids": [str(clip.get("slot_id") or "")],
            }
        )
    candidates.sort(
        key=lambda item: (
            item["asset_id"],
            item["start_frame"],
            item["end_frame"],
            item["source_start_frame"],
        )
    )

    runs: list[dict[str, Any]] = []
    for candidate in candidates:
        previous = runs[-1] if runs else None
        if (
            previous is not None
            and previous["asset_id"] == candidate["asset_id"]
            and previous["text"] == candidate["text"]
            and previous["end_frame"] == candidate["start_frame"]
            and previous["source_end_frame"]
            == candidate["source_start_frame"]
        ):
            previous["end_frame"] = candidate["end_frame"]
            previous["source_end_frame"] = candidate["source_end_frame"]
            previous["clip_ids"].extend(candidate["clip_ids"])
            previous["slot_ids"].extend(candidate["slot_ids"])
        else:
            runs.append(dict(candidate))

    authored = sorted(
        (
            (int(overlay["start_frame"]), int(overlay["end_frame"]))
            for overlay in authored_overlays
            if _slug(_string_or_empty(overlay.get("kind")))
            == "source_caption"
            and _string_or_empty(overlay.get("text")).strip()
        ),
        key=lambda span: (span[0], span[1]),
    )

    overlays: list[dict[str, Any]] = []
    for run in runs:
        for start_frame, end_frame in _uncovered_caption_spans(
            run["start_frame"], run["end_frame"], authored
        ):
            source_start_frame = (
                run["source_start_frame"]
                + start_frame
                - run["start_frame"]
            )
            source_end_frame = source_start_frame + end_frame - start_frame
            detail = {
                "position": "lower-left",
                "source": "provenance",
                "synthesized": True,
                "asset_id": run["asset_id"],
                "source_title": run["source_title"],
                "source_date": run["source_date"],
                "source_start_frame": source_start_frame,
                "source_end_frame": source_end_frame,
                "clip_ids": list(run["clip_ids"]),
                "slot_ids": [
                    slot_id for slot_id in run["slot_ids"] if slot_id
                ],
            }
            payload = {
                "kind": "source_caption",
                "track": "V3",
                "start_frame": start_frame,
                "end_frame": end_frame,
                "duration_frames": end_frame - start_frame,
                "text": run["text"],
                "detail": detail,
                "editable": True,
                "generated": True,
                "asset_id": run["asset_id"],
            }
            payload["id"] = _stable_id(
                "overlay",
                {
                    "policy": _SOURCE_CAPTION_POLICY_VERSION,
                    "asset_id": run["asset_id"],
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "source_start_frame": source_start_frame,
                    "source_end_frame": source_end_frame,
                    "text": run["text"],
                },
            )
            overlays.append(payload)
    return overlays


def _subtitles_from_words(
    words: Sequence[Mapping[str, Any]], fps: int
) -> list[dict[str, Any]]:
    if not words:
        return []
    max_words = 8
    max_duration_frames = int(Decimal("2.5") * fps)
    gap_frames = int(Decimal("0.75") * fps)
    groups: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    for word in words:
        if current:
            previous = current[-1]
            span = word["end_frame"] - current[0]["start_frame"]
            gap = word["start_frame"] - previous["end_frame"]
            if len(current) >= max_words or span > max_duration_frames or gap >= gap_frames:
                groups.append(current)
                current = []
        current.append(word)
        if re.search(r"[.!?][\"')\]]*$", word["word"]):
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    result: list[dict[str, Any]] = []
    for position, group in enumerate(groups):
        start_frame = group[0]["start_frame"]
        end_frame = max(start_frame + 1, group[-1]["end_frame"])
        text = _join_caption_words([str(word["word"]) for word in group])
        payload = {
            "track": "V3",
            "start_frame": start_frame,
            "end_frame": end_frame,
            "duration_frames": end_frame - start_frame,
            "text": text,
            "source": "word_timing",
            "word_start_index": group[0]["index"],
            "word_end_index": group[-1]["index"],
            "editable": True,
        }
        payload["id"] = _stable_id("subtitle", {"position": position, **payload})
        result.append(payload)
    return result


def _join_caption_words(words: Sequence[str]) -> str:
    text = " ".join(words)
    text = re.sub(r"\s+([,.;:!?%])", r"\1", text)
    text = re.sub(r"([(\[{])\s+", r"\1", text)
    return text.strip()


def _sound_manifest_entries(raw: Any) -> Mapping[str, Any]:
    manifest = _require_mapping(raw, "sound_manifest")
    entries = manifest.get("entries")
    if not isinstance(entries, Mapping):
        raise ResolveManifestError("sound_manifest.entries must be an object")
    return entries


def _sound_duration(entry: Mapping[str, Any], label: str) -> float:
    duration = _finite_number(entry.get("duration_seconds"), f"{label}.duration_seconds")
    if duration <= 0:
        raise ResolveManifestError(f"{label}.duration_seconds must be positive")
    return duration


def _project_local_sound_path(
    raw_path: Any,
    *,
    manifest_root: Path,
    project_root: Path,
    label: str,
) -> tuple[Path, str, str]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ResolveManifestError(f"{label}.path must be a non-empty string")
    portable = _portable_string(raw_path.strip())
    if _looks_absolute_portable(portable):
        raise ResolveManifestError(
            f"{label}.path must be relative to the sound manifest: {raw_path!r}"
        )
    parts = PurePosixPath(portable).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ResolveManifestError(
            f"{label}.path contains an escaping or ambiguous component: {raw_path!r}"
        )
    resolved = manifest_root.joinpath(*parts).resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise ResolveManifestError(
            f"{label}.path escapes the project root: {raw_path!r}"
        ) from exc
    plan_path, path_kind = _path_for_plan(resolved, project_root)
    if path_kind != "project-relative":
        raise ResolveManifestError(
            f"{label}.path is not portable: {raw_path!r}"
        )
    return resolved, plan_path, path_kind


def _playable_bed_seconds(generated_seconds: float) -> float:
    """Mirror ``soundgen.trim_edges`` without probing media during compile."""

    trim = max(0.0, min(BED_EDGE_TRIM_SECONDS, generated_seconds / 4.0))
    return generated_seconds - (2.0 * trim)


def _audible_frame_ranges(
    start_frame: int,
    end_frame: int,
    silence_ranges: Sequence[tuple[int, int]],
) -> list[tuple[int, int]]:
    cursor = start_frame
    audible: list[tuple[int, int]] = []
    for silence_start, silence_end in silence_ranges:
        if silence_end <= cursor:
            continue
        if silence_start >= end_frame:
            break
        if silence_start > cursor:
            audible.append((cursor, min(silence_start, end_frame)))
        cursor = max(cursor, silence_end)
        if cursor >= end_frame:
            break
    if cursor < end_frame:
        audible.append((cursor, end_frame))
    return [(start, end) for start, end in audible if end > start]


def _compile_mixed_sound_stems(
    raw_manifest: Any,
    manifest_path: Path,
    root: Path,
    fps: int,
    duration_frames: int,
) -> list[dict[str, Any]]:
    """Compile the render-faithful A3/A4 stems selected during prepare.

    The stem builder has already baked the approved constant-power bed joins,
    style-pack levels, cue placement, and silence-drop ramps.  Resolve should
    therefore import each stem once rather than reconstructing those decisions
    from raw generated clips.  ``master_gain_db`` is the uniform attenuation
    the approved FFmpeg mix applies after summing all three layers; the caller
    applies the same value to narration and these two stems.
    """

    manifest = _require_mapping(raw_manifest, "audio_stems")
    if manifest.get("schema_version") != "resolve-audio-stems.v1":
        raise ResolveManifestError(
            "audio_stems.schema_version must be 'resolve-audio-stems.v1'"
        )
    entries = manifest.get("entries")
    if not isinstance(entries, Mapping):
        raise ResolveManifestError("audio_stems.entries must be an object")
    fingerprint = manifest.get("fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise ResolveManifestError(
            "audio_stems.fingerprint must be a full SHA-256 string"
        )
    declared_seconds = _finite_number(
        manifest.get("duration_seconds"),
        "audio_stems.duration_seconds",
    )
    declared_frames = max(1, _seconds_to_frame(declared_seconds, fps))
    if abs(declared_frames - duration_frames) > 2:
        raise ResolveManifestError(
            "audio stems do not match the narration duration; run "
            "`rabbithole resolve prepare` to build a fresh immutable stem set"
        )
    mix_semantics = _require_mapping(
        manifest.get("mix_semantics"),
        "audio_stems.mix_semantics",
    )
    master_gain_db = _finite_number(
        mix_semantics.get("master_gain_db"),
        "audio_stems.mix_semantics.master_gain_db",
    )
    if not -80.0 <= master_gain_db <= 0.0:
        raise ResolveManifestError(
            "audio_stems.mix_semantics.master_gain_db must be between "
            "-80 and 0 dB"
        )

    compiled: list[dict[str, Any]] = []
    for sequence, (name, expected_track) in enumerate(
        (("music", "A3"), ("sfx", "A4"))
    ):
        entry = entries.get(name)
        if not isinstance(entry, Mapping):
            raise ResolveManifestError(
                f"audio_stems.entries[{name!r}] must be an object"
            )
        label = f"audio_stems.entries[{name!r}]"
        track = entry.get("track")
        if track != expected_track:
            raise ResolveManifestError(
                f"{label}.track must be {expected_track!r}, got {track!r}"
            )
        source, media_path, path_kind = _project_local_sound_path(
            entry.get("path"),
            manifest_root=Path(manifest_path).parent.resolve(),
            project_root=root,
            label=label,
        )
        actual_sha = _sha256_file(source) if source.is_file() else None
        expected_sha = entry.get("sha256")
        if actual_sha is not None and expected_sha != actual_sha:
            raise ResolveManifestError(
                f"{label} checksum changed after audio-stem validation: {source}"
            )
        source_rate = entry.get("sample_rate")
        if isinstance(source_rate, bool) or not isinstance(source_rate, int):
            raise ResolveManifestError(f"{label}.sample_rate must be an integer")
        channels = entry.get("channels")
        if isinstance(channels, bool) or not isinstance(channels, int):
            raise ResolveManifestError(f"{label}.channels must be an integer")

        asset_id = f"sound-stem-{name}"
        payload = {
            "asset_id": asset_id,
            "track": expected_track,
            "kind": name,
            "start_frame": 0,
            "end_frame": duration_frames,
            "duration_frames": duration_frames,
            "source_start_frame": 0,
            "media_path": media_path,
            "path_kind": path_kind,
            "exists": source.is_file(),
            "sha256": actual_sha,
            "channels": channels,
            "source_sample_rate": source_rate,
            "gain_db": round(master_gain_db, 4),
            "duck_vo_db": 0.0,
            "loop": False,
            "mix_baked": True,
            "stem_fingerprint": fingerprint,
        }
        payload["id"] = _stable_id(
            "audio",
            {
                "sequence": sequence,
                "asset_id": asset_id,
                "track": expected_track,
                "start_frame": 0,
                "end_frame": duration_frames,
                "stem_fingerprint": fingerprint,
            },
        )
        compiled.append(payload)
    return compiled


def _compile_generated_sound_audio(
    timing_raw: Any,
    sound_manifest: Any,
    sound_manifest_path: Path | None,
    root: Path,
    fps: int,
    duration_frames: int,
) -> list[dict[str, Any]]:
    """Place generated MUSIC/SFX cues as editable A3/A4 source clips.

    A script without generated sound markers can remain A1/A2-only. If MUSIC
    or SFX was authored, however, a missing manifest is a blocking preparation
    error rather than a silent omission. Once a manifest is present, every cue
    must have a generated entry; missing playable files remain in the plan so
    preflight can surface them as blocking media errors.
    """

    if sound_manifest is None or sound_manifest_path is None:
        authored_cues = [
            marker
            for marker in (
                timing_raw.get("markers", [])
                if isinstance(timing_raw, Mapping)
                else []
            )
            if isinstance(marker, Mapping)
            and marker.get("kind") in {"MUSIC", "SFX"}
        ]
        if authored_cues:
            kinds = sorted({str(marker.get("kind")) for marker in authored_cues})
            raise ResolveManifestError(
                "narration timing contains authored "
                f"{'/'.join(kinds)} cues but no project-local generated sound "
                "manifest. Run the approved sound-generation step before "
                "`rabbithole resolve prepare`; Resolve will not silently drop "
                "authored audio."
            )
        return []

    entries = _sound_manifest_entries(sound_manifest)
    manifest_root = sound_manifest_path.parent.resolve()
    library = Library(manifest_root)
    checksum_cache: dict[Path, str] = {}

    def checksum(path: Path) -> str | None:
        if not path.is_file():
            return None
        if path not in checksum_cache:
            checksum_cache[path] = _sha256_file(path)
        return checksum_cache[path]

    try:
        spans = bed_spans(timing_raw)
        events = sfx_events(timing_raw)
        windows = silence_windows(timing_raw)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise ResolveManifestError(f"cannot place generated sound markers: {exc}") from exc

    silence_ranges = sorted(
        (
            max(0, _seconds_to_frame(window.start, fps)),
            min(duration_frames, _seconds_to_frame(window.end, fps)),
        )
        for window in windows
        if window.end > window.start
    )

    audio: list[dict[str, Any]] = []
    sequence = 0
    for span_index, span in enumerate(spans):
        bed_kind = resolve_cue(str(span.cue))
        declared_variants: list[tuple[int, Mapping[str, Any]]] = []
        prefix = f"beds/{bed_kind}/"
        for key, raw_entry in entries.items():
            if not isinstance(key, str) or not key.startswith(prefix):
                continue
            suffix = key[len(prefix) :]
            if not suffix.isdigit():
                continue
            if not isinstance(raw_entry, Mapping):
                raise ResolveManifestError(
                    f"sound_manifest.entries[{key!r}] must be an object"
                )
            declared_variants.append((int(suffix), raw_entry))
        declared_variants.sort(key=lambda item: item[0])
        if not declared_variants:
            raise ResolveManifestError(
                f"sound manifest has no generated variants for "
                f"[MUSIC:{span.cue}] (bed kind {bed_kind!r})"
            )

        variants: list[dict[str, Any]] = []
        for variant, entry in declared_variants:
            label = f"sound_manifest.entries[{f'beds/{bed_kind}/{variant}'!r}]"
            playable_seconds = _playable_bed_seconds(_sound_duration(entry, label))
            if playable_seconds <= 0:
                raise ResolveManifestError(
                    f"{label} produces no playable audio after edge trimming"
                )
            playable = library.bed_path(bed_kind, variant).resolve()
            try:
                playable.relative_to(root)
            except ValueError as exc:
                raise ResolveManifestError(
                    f"{label} resolves outside the project: {playable}"
                ) from exc
            media_path, path_kind = _path_for_plan(playable, root)
            if path_kind != "project-relative":
                raise ResolveManifestError(
                    f"{label} resolves to a non-portable path: {playable}"
                )
            variants.append(
                {
                    "variant": variant,
                    "path": playable,
                    "media_path": media_path,
                    "path_kind": path_kind,
                    "duration_frames": max(
                        1, _seconds_to_frame(playable_seconds, fps)
                    ),
                }
            )

        span_start = max(0, _seconds_to_frame(span.start, fps))
        span_end = min(duration_frames, _seconds_to_frame(span.end, fps))
        variant_cursor = 0
        for audible_start, audible_end in _audible_frame_ranges(
            span_start, span_end, silence_ranges
        ):
            cursor = audible_start
            while cursor < audible_end:
                variant_data = variants[variant_cursor % len(variants)]
                clip_end = min(
                    audible_end,
                    cursor + int(variant_data["duration_frames"]),
                )
                playable = Path(variant_data["path"])
                payload = {
                    "asset_id": (
                        f"sound-bed-{_slug(bed_kind)}-"
                        f"{int(variant_data['variant']):02d}"
                    ),
                    "track": "A3",
                    "kind": "music",
                    "cue": str(span.cue),
                    "start_frame": cursor,
                    "end_frame": clip_end,
                    "duration_frames": clip_end - cursor,
                    "source_start_frame": 0,
                    "media_path": str(variant_data["media_path"]),
                    "path_kind": str(variant_data["path_kind"]),
                    "exists": playable.is_file(),
                    "sha256": checksum(playable),
                    "channels": SOUND_CHANNELS,
                    "source_sample_rate": SOUND_SAMPLE_RATE,
                    "gain_db": 0.0,
                    "duck_vo_db": -15.0,
                    "loop": True,
                    "span_index": span_index,
                    "variant": int(variant_data["variant"]),
                }
                payload["id"] = _stable_id(
                    "audio",
                    {
                        "sequence": sequence,
                        "asset_id": payload["asset_id"],
                        "track": payload["track"],
                        "start_frame": cursor,
                        "end_frame": clip_end,
                    },
                )
                audio.append(payload)
                sequence += 1
                variant_cursor += 1
                cursor = clip_end

    for event_index, event in enumerate(events):
        cue = str(event.name)
        key = f"sfx/{cue}"
        raw_entry = entries.get(key)
        if not isinstance(raw_entry, Mapping):
            raise ResolveManifestError(
                f"sound manifest has no generated entry for [SFX:{cue}]"
            )
        label = f"sound_manifest.entries[{key!r}]"
        source, media_path, path_kind = _project_local_sound_path(
            raw_entry.get("path"),
            manifest_root=manifest_root,
            project_root=root,
            label=label,
        )
        start_frame = max(0, _seconds_to_frame(event.seconds, fps))
        if start_frame >= duration_frames:
            continue
        source_duration = max(
            1, _seconds_to_frame(_sound_duration(raw_entry, label), fps)
        )
        end_frame = min(duration_frames, start_frame + source_duration)
        payload = {
            "asset_id": f"sound-sfx-{_slug(cue)}",
            "track": "A4",
            "kind": "sfx",
            "cue": cue,
            "start_frame": start_frame,
            "end_frame": end_frame,
            "duration_frames": end_frame - start_frame,
            "source_start_frame": 0,
            "media_path": media_path,
            "path_kind": path_kind,
            "exists": source.is_file(),
            "sha256": checksum(source),
            "channels": SOUND_CHANNELS,
            "source_sample_rate": SOUND_SAMPLE_RATE,
            "gain_db": 0.0,
            "duck_vo_db": 0.0,
            "loop": False,
            "event_index": event_index,
        }
        payload["id"] = _stable_id(
            "audio",
            {
                "event_index": event_index,
                "asset_id": payload["asset_id"],
                "track": payload["track"],
                "start_frame": start_frame,
                "end_frame": end_frame,
            },
        )
        audio.append(payload)

    audio.sort(key=lambda item: (item["start_frame"], item["track"], item["id"]))
    return audio


def _compile_audio(
    raw: Any, root: Path, fps: int, default_duration_frames: int
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    narration_path = root / "narration" / "vo.wav"
    narration_portable, narration_path_kind = _path_for_plan(narration_path, root)
    narration_exists = narration_path.is_file()
    narration_metadata = (
        _probe_audio_metadata(narration_path) if narration_exists else None
    )
    narration_payload = {
        "asset_id": "narration-vo",
        "track": "A1",
        "kind": "narration",
        "start_frame": 0,
        "end_frame": max(1, default_duration_frames),
        "duration_frames": max(1, default_duration_frames),
        "source_start_frame": 0,
        "media_path": narration_portable,
        "path_kind": narration_path_kind,
        "exists": narration_exists,
        "sha256": _sha256_file(narration_path) if narration_exists else None,
        "channels": (
            narration_metadata["channels"] if narration_metadata is not None else None
        ),
        "source_sample_rate": (
            narration_metadata["sample_rate"]
            if narration_metadata is not None
            else None
        ),
        "gain_db": 0.0,
        "duck_vo_db": 0.0,
    }
    narration_payload["id"] = _stable_id(
        "audio",
        {
            "asset_id": narration_payload["asset_id"],
            "track": "A1",
            "start_frame": 0,
            "end_frame": narration_payload["end_frame"],
        },
    )
    result.append(narration_payload)

    if raw is None:
        return result
    if isinstance(raw, list):
        values = raw
    else:
        obj = _require_mapping(raw, "source_audio")
        nested = next(
            (
                obj[key]
                for key in ("clips", "files", "segments", "sources")
                if isinstance(obj.get(key), list)
            ),
            None,
        )
        values = nested if nested is not None else [obj]

    for position, value in enumerate(values):
        item = _require_mapping(value, f"source_audio[{position}]")
        raw_path = next(
            (
                item[key]
                for key in ("local_path", "path", "file", "source_path")
                if item.get(key)
            ),
            "",
        )
        local_path, path_kind = _path_for_plan(_string_or_empty(raw_path), root)
        resolved = _resolve_media_path(_string_or_empty(raw_path), root)
        exists = bool(resolved and resolved.is_file())
        source_metadata = (
            _probe_audio_metadata(resolved)
            if exists and resolved is not None
            else None
        )
        start_seconds = _first_number(
            item,
            ("timeline_start", "start_seconds", "start"),
            default=0.0,
            label=f"source_audio[{position}].start",
        )
        start_frame = _seconds_to_frame(start_seconds, fps)
        if any(key in item for key in ("timeline_end", "end_seconds", "end")):
            end_seconds = _first_number(
                item,
                ("timeline_end", "end_seconds", "end"),
                default=0.0,
                label=f"source_audio[{position}].end",
            )
            end_frame = _end_frame(start_seconds, end_seconds, fps)
        elif item.get("duration_seconds") is not None or item.get("duration") is not None:
            duration = _nonnegative_number(
                item.get("duration_seconds", item.get("duration")),
                f"source_audio[{position}].duration",
            )
            end_frame = start_frame + max(1, _seconds_to_frame(duration, fps))
        else:
            end_frame = max(start_frame + 1, default_duration_frames)
        if end_frame <= start_frame:
            raise ResolveManifestError(
                f"source_audio[{position}] must have positive duration"
            )
        kind = _slug(
            _string_or_empty(item.get("kind") or item.get("role") or "source_bite")
        )
        track = _string_or_empty(item.get("track")).upper() or "A2"
        if track != "A2":
            raise ResolveManifestError(
                f"source_audio[{position}].track must be A2 (source bites)"
            )
        source_start = _first_number(
            item,
            ("source_start", "source_start_seconds", "in"),
            default=0.0,
            label=f"source_audio[{position}].source_start",
        )
        asset_id = _string_or_empty(item.get("asset_id") or item.get("id"))
        if not asset_id:
            asset_id = f"source-audio-{position + 1:03d}-{_short_hash(local_path)}"
        payload = {
            "asset_id": asset_id,
            "track": track,
            "kind": kind,
            "start_frame": start_frame,
            "end_frame": end_frame,
            "duration_frames": end_frame - start_frame,
            "source_start_frame": _seconds_to_frame(source_start, fps),
            "media_path": local_path or None,
            "path_kind": path_kind,
            "exists": exists,
            "sha256": _sha256_file(resolved) if exists and resolved else None,
            "channels": (
                source_metadata["channels"]
                if source_metadata is not None
                else item.get("channels")
            ),
            "source_sample_rate": (
                source_metadata["sample_rate"]
                if source_metadata is not None
                else item.get("sample_rate")
            ),
            "gain_db": _finite_number(
                item.get("gain_db", 0), f"source_audio[{position}].gain_db"
            ),
            "duck_vo_db": _finite_number(
                item.get("duck_vo_db", -18),
                f"source_audio[{position}].duck_vo_db",
            ),
        }
        payload["id"] = _stable_id(
            "audio",
            {
                "position": position,
                "asset_id": asset_id,
                "track": track,
                "start_frame": start_frame,
                "end_frame": end_frame,
            },
        )
        result.append(payload)
    return result


def _probe_audio_metadata(path: Path) -> dict[str, int] | None:
    """Return source channel/rate metadata without changing the media.

    Narration is normally PCM WAV, so use the standard-library parser first.
    Source bites can be embedded in video containers; those fall back to
    ffprobe, which is already a required pipeline dependency.  A malformed
    legacy fixture or unavailable probe returns ``None`` so compilation can
    still report the existing media diagnostics instead of crashing here.
    """

    path = Path(path)
    if path.suffix.lower() in {".wav", ".wave"}:
        try:
            with wave.open(str(path), "rb") as handle:
                channels = int(handle.getnchannels())
                sample_rate = int(handle.getframerate())
            if channels > 0 and sample_rate > 0:
                return {"channels": channels, "sample_rate": sample_rate}
        except (EOFError, OSError, wave.Error):
            return None
        return None

    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=channels,sample_rate",
                "-of",
                "json",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout)
        stream = payload["streams"][0]
        channels = int(stream["channels"])
        sample_rate = int(stream["sample_rate"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if channels <= 0 or sample_rate <= 0:
        return None
    return {"channels": channels, "sample_rate": sample_rate}


def _compile_highlights(raw: Any, fps: int) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, list):
        values = raw
    else:
        obj = _require_mapping(raw, "highlights")
        values = next(
            (
                obj[key]
                for key in ("highlights", "items", "segments")
                if isinstance(obj.get(key), list)
            ),
            [obj],
        )
    result: list[dict[str, Any]] = []
    for position, value in enumerate(values):
        item = _require_mapping(value, f"highlights[{position}]")
        start_seconds = _first_number(
            item,
            ("start", "start_seconds", "seconds", "time"),
            default=0.0,
            label=f"highlights[{position}].start",
        )
        end_seconds = _first_number(
            item,
            ("end", "end_seconds"),
            default=start_seconds,
            label=f"highlights[{position}].end",
        )
        start_frame = _seconds_to_frame(start_seconds, fps)
        end_frame = max(start_frame, _seconds_to_frame(end_seconds, fps))
        text = _string_or_empty(
            item.get("text")
            or item.get("note")
            or item.get("reason")
            or item.get("label")
        )
        line = item.get("line")
        if line is not None:
            line = _integer(line, f"highlights[{position}].line")
        payload = {
            "start_frame": start_frame,
            "end_frame": end_frame,
            "text": text,
            "line": line,
            "detail": _json_safe(
                {
                    key: value
                    for key, value in item.items()
                    if key
                    not in {
                        "start",
                        "start_seconds",
                        "seconds",
                        "time",
                        "end",
                        "end_seconds",
                        "text",
                        "note",
                        "reason",
                        "label",
                        "line",
                    }
                }
            ),
        }
        payload["id"] = _stable_id("highlight", {"position": position, **payload})
        result.append(payload)
    result.sort(key=lambda item: (item["start_frame"], item["id"]))
    return result


def _is_evidence(cut: Mapping[str, Any], asset: Mapping[str, Any]) -> bool:
    value = " ".join(
        [
            _string_or_empty(cut.get("origin")),
            _string_or_empty(asset.get("tier")),
            _string_or_empty(asset.get("provider")),
            _string_or_empty(asset.get("notes")),
        ]
    ).lower()
    return any(term in value for term in _EVIDENCE_TERMS)


def _looks_generated(asset: Mapping[str, Any]) -> bool:
    value = " ".join(
        [
            _string_or_empty(asset.get("tier")),
            _string_or_empty(asset.get("provider")),
            _string_or_empty(asset.get("local_path")),
        ]
    ).lower()
    return "generated" in value or "/generated/" in value


def _add_review(
    target: list[dict[str, Any]],
    kind: str,
    severity: str,
    frame: int,
    message: str,
    *,
    slot_id: str | None = None,
    asset_id: str | None = None,
) -> None:
    payload = {
        "kind": kind,
        "severity": severity,
        "frame": int(frame),
        "slot_id": slot_id,
        "asset_id": asset_id,
        "message": message,
    }
    payload["id"] = _stable_id("review", payload)
    payload["resolved"] = False
    target.append(payload)


def _finalize_reviews(
    flags: Sequence[dict[str, Any]], resolutions: Mapping[str, Any]
) -> list[dict[str, Any]]:
    unique = {flag["id"]: dict(flag) for flag in flags}
    for review_id, resolution in resolutions.items():
        if review_id not in unique:
            continue
        if isinstance(resolution, bool):
            unique[review_id]["resolved"] = resolution
        elif isinstance(resolution, Mapping):
            unique[review_id]["resolved"] = bool(resolution.get("resolved", True))
            if resolution.get("note") is not None:
                unique[review_id]["resolution_note"] = str(resolution["note"])
        else:
            raise ResolveManifestError(
                f"review resolution {review_id} must be boolean or object"
            )
    return sorted(
        unique.values(),
        key=lambda item: (
            item["frame"],
            item["severity"],
            item["kind"],
            item["id"],
        ),
    )


def _infer_media_root(paths: Iterable[str]) -> str:
    relative = [
        PurePosixPath(path)
        for path in paths
        if path and not _looks_absolute_portable(path) and not path.startswith("../")
    ]
    if not relative:
        return "."
    first_parts = {path.parts[0] for path in relative if path.parts}
    return next(iter(first_parts)) if len(first_parts) == 1 else "."


def _media_type(path: str | None) -> str | None:
    if not path:
        return None
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in _AUDIO_SUFFIXES:
        return "audio"
    if suffix in _STILL_SUFFIXES:
        return "still"
    return "video"


def _path_for_plan(path: str | Path, root: Path) -> tuple[str, str]:
    raw = str(path)
    if not raw:
        return "", "missing"
    portable_raw = _portable_string(raw)
    native_absolute = Path(raw).is_absolute()
    windows_absolute = bool(re.match(r"^[A-Za-z]:/", portable_raw))
    posix_absolute = portable_raw.startswith("/") and not portable_raw.startswith("//")
    foreign_absolute = (
        (windows_absolute and os.name != "nt")
        or (posix_absolute and os.name == "nt")
    )
    if foreign_absolute or (portable_raw.startswith("//") and not native_absolute):
        return portable_raw, "external-absolute"
    legacy = _legacy_project_path(raw, root)
    if legacy is not None:
        _, portable = legacy
        return portable, "project-relative"
    candidate = Path(raw)
    if not candidate.is_absolute():
        resolved = (root / candidate).resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            return _portable_string(str(resolved)), "external-absolute"
        return _portable_string(str(relative)), "project-relative"
    resolved = candidate.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return _portable_string(str(resolved)), "external-absolute"
    return _portable_string(str(relative)), "project-relative"


def _resolve_media_path(path: str, root: Path) -> Path | None:
    if not path:
        return None
    portable = _portable_string(path)
    if (
        bool(re.match(r"^[A-Za-z]:/", portable)) and os.name != "nt"
    ) or (
        portable.startswith("/") and os.name == "nt"
    ):
        return None
    legacy = _legacy_project_path(path, root)
    if legacy is not None:
        return legacy[0]
    candidate = Path(path)
    return candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()


def _legacy_project_path(path: str, root: Path) -> tuple[Path, str] | None:
    """Rebase old repo-relative ``projects/<slug>/...`` provenance entries."""

    if _looks_absolute_portable(path):
        return None
    parts = PurePosixPath(path.replace("\\", "/")).parts
    if len(parts) < 3 or parts[0] != "projects" or parts[1] != root.name:
        return None
    portable = PurePosixPath(*parts[2:]).as_posix()
    # The portable episode may no longer live under a repository's
    # projects/<slug>/ directory. Prefer its own project-relative media, then
    # retain the old checkout lookup only as a compatibility fallback.
    rebased = root.joinpath(*parts[2:]).resolve()
    if rebased.exists():
        return rebased, portable
    repository_root = root.parents[1] if len(root.parents) > 1 else root.parent
    legacy = repository_root.joinpath(*parts).resolve()
    resolved = legacy if legacy.exists() else rebased
    return resolved, portable


def _portable_string(value: str) -> str:
    normalized = value.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized or "."


def _looks_absolute_portable(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return (
        normalized.startswith("/")
        or normalized.startswith("//")
        or bool(re.match(r"^[A-Za-z]:/", normalized))
    )


def _binding_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "unknown"


def _seconds_to_frame(seconds: float, fps: int) -> int:
    return int(
        (Decimal(str(seconds)) * Decimal(fps)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _end_frame(start_seconds: float, end_seconds: float, fps: int) -> int:
    start_frame = _seconds_to_frame(start_seconds, fps)
    end_frame = _seconds_to_frame(end_seconds, fps)
    if end_seconds > start_seconds and end_frame <= start_frame:
        return start_frame + 1
    return end_frame


def _stable_id(prefix: str, payload: Any) -> str:
    return f"{prefix}-{_sha256_json(payload)[:12]}"


def _short_hash(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))[:8]


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ResolveManifestError(
            f"invalid JSON in {path}: line {exc.lineno}, column {exc.colno}"
        ) from exc
    except OSError as exc:
        raise ResolveManifestError(f"cannot read {path}: {exc}") from exc


def _canonical_json(value: Any, *, pretty: bool = False) -> str:
    kwargs: dict[str, Any] = {
        "ensure_ascii": False,
        "sort_keys": True,
        "allow_nan": False,
    }
    if pretty:
        kwargs["indent"] = 2
    else:
        kwargs["separators"] = (",", ":")
    return json.dumps(value, **kwargs)


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(_json_safe(value)).encode("utf-8"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == content:
                return
        except OSError:
            pass
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_format_settings(
    fps: int, width: int, height: int, sample_rate: int
) -> None:
    if not isinstance(fps, int) or isinstance(fps, bool) or fps <= 0:
        raise ResolveManifestError("fps must be a positive integer")
    if fps != 30:
        raise ResolveManifestError("resolve-plan.v1 currently requires integer 30 fps")
    for name, value in (
        ("width", width),
        ("height", height),
        ("sample_rate", sample_rate),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ResolveManifestError(f"{name} must be a positive integer")


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResolveManifestError(f"{label} must be a JSON object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ResolveManifestError(f"{label} must be a JSON array")
    return value


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResolveManifestError(f"{label} must be a non-empty string")
    return value.strip()


def _string_or_empty(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return _canonical_json(_json_safe(value))
    return str(value)


def _integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ResolveManifestError(f"{label} must be an integer")
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResolveManifestError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ResolveManifestError(f"{label} must be finite")
    return result


def _nonnegative_number(value: Any, label: str) -> float:
    result = _finite_number(value, label)
    if result < 0:
        raise ResolveManifestError(f"{label} must be non-negative")
    return result


def _first_number(
    item: Mapping[str, Any],
    keys: Sequence[str],
    *,
    default: float,
    label: str,
) -> float:
    for key in keys:
        if item.get(key) is not None:
            return _nonnegative_number(item[key], label)
    return default


def _lookup_override(values: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in values:
            return values[key]
    return None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ResolveManifestError("JSON input contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_safe(item) for item in value]
    return str(value)


__all__ = [
    "COMPILER_VERSION",
    "DEFAULT_FPS",
    "ResolveManifestError",
    "SCHEMA_VERSION",
    "compile_resolve_plan",
    "write_resolve_bundle",
]

"""Deterministic, non-destructive Resolve style application.

This module deliberately limits itself to calls documented by Resolve 21:

* ``Timeline.GetItemListInTrack``
* ``TimelineItem.GetStart`` / ``GetName`` / ``GetNodeGraph``
* ``Graph.GetNumNodes`` / ``ApplyGradeFromDRX`` / ``SetLUT``
* ``Project.RefreshLUTList``

It is used only while constructing a brand-new ``AUTO_BUILD_*`` timeline.
Existing generated timelines and editor timelines are never restyled in place.
Fusion title and transition templates are recorded as deterministic intent
because the public scripting API cannot place and trim those templates at an
exact edit range without relying on UI state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .resolve_install import repository_root, user_resolve_support_root


STYLE_RESULT_SCHEMA_VERSION = "rabbithole.resolve-style-result.v1"


class ResolveStyleError(RuntimeError):
    """The style contract is malformed or internally inconsistent."""


def _canonical_json_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def style_contract_hash(style: Mapping[str, Any]) -> str:
    """Return and validate the embedded deterministic style contract hash."""

    payload = dict(style)
    expected = payload.pop("contract_sha256", None)
    actual = _canonical_json_hash(payload)
    if not isinstance(expected, str) or expected != actual:
        raise ResolveStyleError(
            "Resolve style contract checksum does not match its contents"
        )
    return actual


def _call_optional(owner: Any, name: str, *args: Any) -> tuple[bool, Any]:
    method = getattr(owner, name, None)
    if not callable(method):
        return False, None
    try:
        return True, method(*args)
    except Exception:
        return True, None


def _item_start(item: Any, timeline_start: int) -> int | None:
    method = getattr(item, "GetStart", None)
    if not callable(method):
        return None
    try:
        value = method(False)
    except TypeError:
        try:
            value = method()
        except Exception:
            return None
    except Exception:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(round(float(value))) - timeline_start


def _item_name(item: Any) -> str:
    available, value = _call_optional(item, "GetName")
    return str(value) if available and isinstance(value, str) else ""


def _timeline_start(timeline: Any) -> int:
    available, value = _call_optional(timeline, "GetStartFrame")
    if available and isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(round(float(value)))
    return 0


def _track_items(timeline: Any, track_index: int) -> list[Any] | None:
    available, raw = _call_optional(
        timeline, "GetItemListInTrack", "video", track_index
    )
    if not available or raw is None:
        return None
    if isinstance(raw, Mapping):
        return list(raw.values())
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return list(raw)
    return None


def _verified_file(
    candidates: Sequence[Path],
    expected_sha256: str | None,
) -> Path | None:
    if not expected_sha256:
        return None
    for candidate in candidates:
        path = candidate.expanduser().resolve(strict=False)
        if not path.is_file():
            continue
        try:
            if _sha256(path) == expected_sha256:
                return path
        except OSError:
            continue
    return None


def _portable_candidates(
    raw_path: str | None,
    project_root: Path,
) -> list[Path]:
    if not raw_path:
        return []
    value = Path(raw_path).expanduser()
    if value.is_absolute():
        return [value]
    return [project_root / value, repository_root() / value]


def _style_media(
    style: Mapping[str, Any],
    project_root: Path,
) -> tuple[Path | None, Path | None]:
    grade = style.get("grade")
    if not isinstance(grade, Mapping):
        return None, None

    drx_path = grade.get("drx_path")
    drx_candidates: list[Path] = []
    installed_drx = grade.get("installed_drx_path")
    if isinstance(installed_drx, str) and installed_drx:
        drx_candidates.append(user_resolve_support_root() / Path(installed_drx))
    drx_candidates.extend(
        _portable_candidates(
            str(drx_path) if isinstance(drx_path, str) else None,
            project_root,
        )
    )
    drx = _verified_file(
        drx_candidates,
        (
            str(grade["drx_sha256"])
            if isinstance(grade.get("drx_sha256"), str)
            else None
        ),
    )

    lut_path = grade.get("lut_path")
    lut_candidates: list[Path] = []
    installed_lut = grade.get("installed_lut_path")
    if isinstance(installed_lut, str) and installed_lut:
        lut_candidates.append(
            user_resolve_support_root() / "LUT" / Path(installed_lut)
        )
    lut_candidates.extend(
        _portable_candidates(
            str(lut_path) if isinstance(lut_path, str) else None,
            project_root,
        )
    )
    lut = _verified_file(
        lut_candidates,
        (
            str(grade["lut_sha256"])
            if isinstance(grade.get("lut_sha256"), str)
            else None
        ),
    )
    return drx, lut


def _eligible_clips(
    plan: Mapping[str, Any],
    style: Mapping[str, Any],
) -> list[dict[str, Any]]:
    grade = style.get("grade")
    if not isinstance(grade, Mapping):
        return []
    raw_ids = grade.get("clip_ids")
    clip_ids = (
        {str(value) for value in raw_ids}
        if isinstance(raw_ids, Sequence)
        and not isinstance(raw_ids, (str, bytes))
        else set()
    )
    raw_clips = plan.get("clips")
    if not isinstance(raw_clips, Sequence) or isinstance(raw_clips, (str, bytes)):
        return []
    return [
        dict(clip)
        for clip in raw_clips
        if isinstance(clip, Mapping) and str(clip.get("id")) in clip_ids
    ]


def _find_item(
    timeline: Any,
    clip: Mapping[str, Any],
    *,
    timeline_start: int,
    track_cache: dict[int, list[Any] | None],
    claimed: set[int],
) -> Any | None:
    track_name = str(clip.get("track") or "")
    if not track_name.startswith("V"):
        return None
    try:
        track_index = int(track_name[1:])
        expected_start = int(clip["start_frame"])
    except (TypeError, ValueError, KeyError):
        return None
    if track_index not in track_cache:
        track_cache[track_index] = _track_items(timeline, track_index)
    items = track_cache[track_index]
    if items is None:
        return None
    candidates = [
        item
        for item in items
        if id(item) not in claimed
        and _item_start(item, timeline_start) == expected_start
    ]
    if not candidates:
        return None
    expected_names = {
        str(value)
        for value in (clip.get("slot_id"), clip.get("asset_id"), clip.get("id"))
        if value
    }
    named = [item for item in candidates if _item_name(item) in expected_names]
    selected = named[0] if len(named) == 1 else candidates[0]
    claimed.add(id(selected))
    return selected


def apply_style_to_new_timeline(
    project: Any,
    timeline: Any,
    plan: Mapping[str, Any],
    *,
    project_root: os.PathLike[str] | str,
) -> dict[str, Any]:
    """Apply the versioned grade to eligible clips on a new generated timeline.

    A missing style asset or unsupported API is reported as editable manual
    intent; it does not trigger a destructive recovery operation.  The caller
    must persist the returned result in a timeline marker.
    """

    raw_style = plan.get("style")
    if not isinstance(raw_style, Mapping):
        return {
            "schema_version": STYLE_RESULT_SCHEMA_VERSION,
            "status": "disabled",
            "contract_sha256": None,
            "eligible_clip_count": 0,
            "applied_clip_count": 0,
            "clips": [],
            "fusion": "not_requested",
        }

    style = dict(raw_style)
    contract_sha256 = style_contract_hash(style)
    root = Path(project_root).expanduser().resolve(strict=False)
    eligible = _eligible_clips(plan, style)
    drx, lut = _style_media(style, root)

    refresh_available, refresh_result = _call_optional(project, "RefreshLUTList")
    track_cache: dict[int, list[Any] | None] = {}
    claimed: set[int] = set()
    timeline_start = _timeline_start(timeline)
    records: list[dict[str, Any]] = []

    for clip in eligible:
        record: dict[str, Any] = {
            "clip_id": str(clip.get("id")),
            "slot_id": str(clip.get("slot_id") or ""),
            "track": str(clip.get("track") or ""),
            "start_frame": int(clip.get("start_frame", 0)),
            "status": "manual_required",
            "method": None,
        }
        item = _find_item(
            timeline,
            clip,
            timeline_start=timeline_start,
            track_cache=track_cache,
            claimed=claimed,
        )
        if item is None:
            record["reason"] = "matching_timeline_item_not_found"
            records.append(record)
            continue

        graph_available, graph = _call_optional(item, "GetNodeGraph")
        if not graph_available or graph is None:
            record["reason"] = "clip_node_graph_unavailable"
            records.append(record)
            continue
        nodes_available, node_count = _call_optional(graph, "GetNumNodes")
        if (
            not nodes_available
            or isinstance(node_count, bool)
            or not isinstance(node_count, int)
            or node_count < 1
        ):
            record["reason"] = "clip_node_graph_has_no_editable_node"
            records.append(record)
            continue

        if drx is not None:
            method_available, applied = _call_optional(
                graph, "ApplyGradeFromDRX", os.fspath(drx), 0
            )
            if method_available and applied is True:
                record.update(
                    {
                        "status": "applied",
                        "method": "drx",
                        "asset_sha256": _sha256(drx),
                    }
                )
                records.append(record)
                continue

        if lut is not None:
            method_available, applied = _call_optional(
                graph, "SetLUT", 1, os.fspath(lut)
            )
            if method_available and applied is True:
                record.update(
                    {
                        "status": "applied",
                        "method": "lut",
                        "asset_sha256": _sha256(lut),
                    }
                )
                records.append(record)
                continue

        if drx is None and lut is None:
            record["reason"] = "versioned_grade_assets_unavailable"
        else:
            record["reason"] = "resolve_rejected_versioned_grade"
        records.append(record)

    applied_count = sum(record["status"] == "applied" for record in records)
    if not eligible:
        status = "intent_only"
    elif applied_count == len(eligible):
        status = "applied"
    elif applied_count:
        status = "partially_applied"
    else:
        status = "manual_required"

    fusion = style.get("fusion")
    fusion_status = (
        str(fusion.get("automation"))
        if isinstance(fusion, Mapping) and fusion.get("automation")
        else "intent_only"
    )
    return {
        "schema_version": STYLE_RESULT_SCHEMA_VERSION,
        "status": status,
        "contract_sha256": contract_sha256,
        "eligible_clip_count": len(eligible),
        "applied_clip_count": applied_count,
        "clips": records,
        "lut_refresh": {
            "available": refresh_available,
            "succeeded": refresh_result is True,
        },
        "fusion": fusion_status,
        "limitations": [
            "V4 grain, vignette, and VHS treatments remain editable manual intent.",
            "Fusion titles/transitions are not inserted because the documented "
            "API cannot place and trim them deterministically.",
        ],
    }


__all__ = [
    "ResolveStyleError",
    "STYLE_RESULT_SCHEMA_VERSION",
    "apply_style_to_new_timeline",
    "style_contract_hash",
]

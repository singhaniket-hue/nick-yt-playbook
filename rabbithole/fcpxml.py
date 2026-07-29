"""Deterministic FCPXML 1.10 export for resolve-plan.v1 manifests."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
from urllib.parse import quote


FCPXML_VERSION = "1.10"
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


class FCPXMLError(ValueError):
    """Raised when a Resolve plan cannot be represented as FCPXML."""


def build_fcpxml(
    plan: Mapping[str, Any], *, project_root: Path | None = None
) -> str:
    """Return deterministic, Resolve-importable FCPXML 1.10.

    The JSON plan remains the authoritative track contract.  V1 is the
    sequence's primary storyline.  FCPXML connected lanes are relative to that
    storyline, so V2..V4 map to positive lanes 1..3 and A1..A5 map to
    negative lanes -1..-5.
    """

    _validate_plan(plan)
    fps = int(plan["fps"])
    duration_frames = int(plan["duration_frames"])
    width = int(plan["resolution"]["width"])
    height = int(plan["resolution"]["height"])
    sample_rate = int(plan["sample_rate"])
    root_path = Path(project_root).resolve() if project_root is not None else None

    fcpxml = ET.Element("fcpxml", {"version": FCPXML_VERSION})
    resources = ET.SubElement(fcpxml, "resources")
    ET.SubElement(
        resources,
        "format",
        {
            "id": "r1",
            "name": f"FFVideoFormat{height}p{fps}",
            "frameDuration": _time(1, fps),
            "width": str(width),
            "height": str(height),
            "colorSpace": "1-1-1 (Rec. 709)",
        },
    )

    media = _collect_media(plan)
    resource_ids: dict[tuple[str, str], str] = {}
    next_resource = 2
    for media_item in media:
        key = (media_item["asset_id"], media_item["path"])
        resource_id = f"r{next_resource}"
        next_resource += 1
        resource_ids[key] = resource_id
        attributes = {
            "id": resource_id,
            "name": media_item["name"],
            "start": "0s",
            "duration": _time(media_item["duration_frames"], fps),
        }
        if media_item["media_type"] == "audio":
            attributes.update(
                {
                    "hasAudio": "1",
                    "audioSources": "1",
                    "audioChannels": str(media_item.get("channels") or 2),
                    "audioRate": _audio_rate(
                        int(media_item.get("sample_rate") or sample_rate)
                    ),
                }
            )
        else:
            attributes.update({"hasVideo": "1", "format": "r1"})
        asset = ET.SubElement(resources, "asset", attributes)
        ET.SubElement(
            asset,
            "media-rep",
            {
                "kind": "original-media",
                "src": _media_uri(media_item["path"], root_path),
                "suggestedFilename": PurePosixPath(media_item["path"]).name,
            },
        )

    transition_effect_ids: dict[str, str] = {}
    transition_kinds = {
        str((clip.get("transition") or {}).get("kind") or "cut")
        for clip in plan.get("clips", [])
    }
    transition_resources = (
        (
            "cross_dissolve",
            "Cross Dissolve",
            "FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265",
        ),
        (
            "fade",
            "Cross Dissolve",
            "FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265",
        ),
        (
            "dip_to_black",
            "Fade To Color",
            "FxPlug:F779C565-486D-4633-8035-0374B4DB8F5C",
        ),
    )
    for kind, name, uid in transition_resources:
        if kind not in transition_kinds:
            continue
        effect_id = f"r{next_resource}"
        next_resource += 1
        transition_effect_ids[kind] = effect_id
        ET.SubElement(
            resources,
            "effect",
            {"id": effect_id, "name": name, "uid": uid},
        )

    title_effect_id: str | None = None
    if any(overlay.get("text") for overlay in plan.get("overlays", [])):
        title_effect_id = f"r{next_resource}"
        next_resource += 1
        ET.SubElement(
            resources,
            "effect",
            {
                "id": title_effect_id,
                "name": "Basic Title",
                "uid": (
                    ".../Titles.localized/"
                    "Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"
                ),
            },
        )

    event = ET.SubElement(fcpxml, "event", {"name": "RabbitHole Auto Builds"})
    project = ET.SubElement(event, "project", {"name": str(plan["timeline_name"])})
    sequence = ET.SubElement(
        project,
        "sequence",
        {
            "format": "r1",
            "duration": _time(duration_frames, fps),
            "tcStart": "0s",
            "tcFormat": "NDF",
            "audioLayout": "stereo",
            "audioRate": _audio_rate(sample_rate),
        },
    )
    sequence_note = ET.SubElement(sequence, "note")
    sequence_note.text = (
        f"{plan['schema_version']} build={plan['build_id']} "
        "track intent V1-V4/A1-A5; relink from resolve-plan.v1.json "
        "when the project moves"
    )
    primary_spine = ET.SubElement(sequence, "spine")

    clips = list(plan.get("clips", []))
    primary_story = _append_visual_timeline(
        primary_spine,
        clips,
        resource_ids,
        transition_effect_ids,
        fps,
        duration_frames,
    )

    audio_clips = list(plan.get("audio", []))
    for track_number in range(1, 6):
        track_id = f"A{track_number}"
        track_audio = [
            clip for clip in audio_clips if clip.get("track") == track_id
        ]
        if track_audio:
            _append_audio_track(
                primary_story,
                track_id,
                -track_number,
                track_audio,
                resource_ids,
                fps,
            )

    for subtitle in sorted(
        plan.get("subtitles", []),
        key=lambda item: (item["start_frame"], item["id"]),
    ):
        _append_caption(primary_story, subtitle, fps)

    if title_effect_id is not None:
        for overlay in sorted(
            plan.get("overlays", []),
            key=lambda item: (item["start_frame"], item["id"]),
        ):
            if overlay.get("text") and overlay.get("kind") not in {
                "caption",
                "captions",
                "subtitle",
                "subtitles",
            }:
                _append_title(primary_story, overlay, title_effect_id, fps)

    ET.indent(fcpxml, space="  ")
    body = ET.tostring(fcpxml, encoding="unicode", short_empty_elements=True)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"


def write_fcpxml(
    plan: Mapping[str, Any],
    path: Path,
    *,
    project_root: Path | None = None,
) -> Path:
    """Write FCPXML directly.

    Bundle code normally uses ``write_resolve_bundle`` so plan and FCPXML move
    together.  This helper is convenient for callers that only need XML.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        build_fcpxml(plan, project_root=project_root),
        encoding="utf-8",
        newline="\n",
    )
    return destination


def _collect_media(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    durations: dict[tuple[str, str], int] = {}
    types: dict[tuple[str, str], str] = {}
    channels: dict[tuple[str, str], Any] = {}
    sample_rates: dict[tuple[str, str], Any] = {}
    names: dict[tuple[str, str], str] = {}
    for clip in plan.get("clips", []):
        asset_id = clip.get("asset_id")
        path = clip.get("media_path")
        if not asset_id or not path:
            continue
        key = (str(asset_id), str(path))
        durations[key] = max(durations.get(key, 0), int(clip["source_end_frame"]))
        types[key] = str(clip.get("media_type") or _media_type(str(path)))
        names[key] = str(asset_id)
    for clip in plan.get("audio", []):
        asset_id = clip.get("asset_id")
        path = clip.get("media_path")
        if not asset_id or not path:
            continue
        key = (str(asset_id), str(path))
        source_end = int(clip.get("source_start_frame", 0)) + int(
            clip["duration_frames"]
        )
        durations[key] = max(durations.get(key, 0), source_end)
        types[key] = "audio"
        channels[key] = clip.get("channels")
        sample_rates[key] = clip.get("source_sample_rate")
        names[key] = str(asset_id)
    result = [
        {
            "asset_id": key[0],
            "path": key[1],
            "name": names[key],
            "duration_frames": max(1, durations[key]),
            "media_type": types[key],
            "channels": channels.get(key),
            "sample_rate": sample_rates.get(key),
        }
        for key in durations
    ]
    result.sort(key=lambda item: (item["asset_id"], item["path"]))
    return result


def _append_visual_timeline(
    parent: ET.Element,
    clips: Sequence[Mapping[str, Any]],
    resource_ids: Mapping[tuple[str, str], str],
    transition_effect_ids: Mapping[str, str],
    fps: int,
    duration_frames: int,
) -> list[tuple[int, int, int, ET.Element]]:
    """Append V1 as the primary story and V2..V4 as connected clips.

    FCPXML lanes are relative to a primary storyline; they are not absolute
    NLE track numbers.  Resolve therefore receives V1 from the sequence spine
    and receives V2..V4 from lanes 1..3 anchored to the covering V1 item (or
    to a primary gap where V1 is intentionally empty).
    """

    by_track: dict[str, list[Mapping[str, Any]]] = {
        f"V{number}": [] for number in range(1, 5)
    }
    for clip in clips:
        track_id = str(clip.get("track") or "V1")
        if track_id not in by_track:
            raise FCPXMLError(f"unsupported FCPXML video track {track_id!r}")
        by_track[track_id].append(clip)
    for track_id, track_clips in by_track.items():
        track_clips.sort(key=lambda item: (item["start_frame"], item["id"]))
        _require_non_overlapping_video_track(
            track_id, track_clips, duration_frames
        )

    primary_story: list[tuple[int, int, int, ET.Element]] = []
    cursor = 0
    previous: Mapping[str, Any] | None = None
    for clip in by_track["V1"]:
        start = int(clip["start_frame"])
        duration = int(clip["duration_frames"])
        if start > cursor:
            gap = _append_primary_gap(
                parent,
                cursor,
                start - cursor,
                fps,
                name="V1 gap",
            )
            primary_story.append((cursor, start, cursor, gap))
            previous = None
        if (
            previous is not None
            and start == int(previous["end_frame"])
            and previous.get("asset_id")
            and previous.get("media_path")
            and clip.get("asset_id")
            and clip.get("media_path")
        ):
            _append_transition(
                parent, previous, clip, transition_effect_ids, fps
            )
        if clip.get("asset_id") and clip.get("media_path"):
            item = _append_visual_clip(
                parent,
                clip,
                resource_ids,
                fps,
                offset_frame=start,
            )
            source_start = int(clip.get("source_start_frame", 0))
        else:
            item = _append_primary_gap(
                parent,
                start,
                duration,
                fps,
                name=f"MISSING: {clip.get('slot_id', clip['id'])}",
            )
            missing_note = ET.SubElement(item, "note")
            missing_note.text = f"id={clip['id']} missing media"
            source_start = start
        primary_story.append(
            (start, int(clip["end_frame"]), source_start, item)
        )
        cursor = max(cursor, int(clip["end_frame"]))
        previous = clip
    if cursor < duration_frames:
        gap = _append_primary_gap(
            parent,
            cursor,
            duration_frames - cursor,
            fps,
            name="V1 gap",
        )
        primary_story.append((cursor, duration_frames, cursor, gap))

    for track_number in range(2, 5):
        track_id = f"V{track_number}"
        for clip in by_track[track_id]:
            asset_id = clip.get("asset_id")
            path = clip.get("media_path")
            # A gap cannot be an anchored item in FCPXML. Missing connected
            # media remains explicit in the authoritative plan/review flags.
            if not asset_id or not path:
                continue
            anchor, local_offset = _story_anchor(
                primary_story, int(clip["start_frame"]), track_id
            )
            _append_visual_clip(
                anchor,
                clip,
                resource_ids,
                fps,
                offset_frame=local_offset,
                lane=track_number - 1,
            )
    return primary_story


def _append_primary_gap(
    parent: ET.Element,
    start_frame: int,
    duration_frames: int,
    fps: int,
    *,
    name: str,
) -> ET.Element:
    return ET.SubElement(
        parent,
        "gap",
        {
            "name": name,
            "offset": _time(start_frame, fps),
            "start": _time(start_frame, fps),
            "duration": _time(duration_frames, fps),
        },
    )


def _append_visual_clip(
    parent: ET.Element,
    clip: Mapping[str, Any],
    resource_ids: Mapping[tuple[str, str], str],
    fps: int,
    *,
    offset_frame: int,
    lane: int | None = None,
) -> ET.Element:
    asset_id = str(clip["asset_id"])
    path = str(clip["media_path"])
    resource_id = resource_ids.get((asset_id, path))
    if resource_id is None:
        raise FCPXMLError(f"missing FCPXML resource for {asset_id}")
    attributes = {
        "name": str(clip.get("slot_id") or asset_id),
        "ref": resource_id,
        "offset": _time(offset_frame, fps),
        "start": _time(int(clip.get("source_start_frame", 0)), fps),
        "duration": _time(int(clip["duration_frames"]), fps),
        "srcEnable": "video",
    }
    if lane is not None:
        if lane <= 0:
            raise FCPXMLError("connected video clips require a positive lane")
        attributes["lane"] = str(lane)
    item = ET.SubElement(parent, "asset-clip", attributes)
    item_note = ET.SubElement(item, "note")
    item_note.text = (
        f"id={clip['id']} track={clip.get('track', 'V1')} "
        f"origin={clip.get('origin', '')}"
    )
    transform = clip.get("transform") or {}
    if _non_identity_transform(transform):
        ET.SubElement(
            item,
            "adjust-transform",
            {
                "position": (
                    f"{_number(transform.get('position_x', 0))} "
                    f"{_number(transform.get('position_y', 0))}"
                ),
                "scale": (
                    f"{_number(transform.get('scale_x', 1))} "
                    f"{_number(transform.get('scale_y', 1))}"
                ),
                "rotation": _number(transform.get("rotation", 0)),
            },
        )
    return item


def _require_non_overlapping_video_track(
    track_id: str,
    clips: Sequence[Mapping[str, Any]],
    duration_frames: int,
) -> None:
    cursor = 0
    for clip in clips:
        start = int(clip["start_frame"])
        end = int(clip["end_frame"])
        duration = int(clip["duration_frames"])
        if start < 0 or duration <= 0 or end != start + duration:
            raise FCPXMLError(
                f"{track_id} clip {clip.get('id')!r} has invalid frame bounds"
            )
        if end > duration_frames:
            raise FCPXMLError(
                f"{track_id} clip {clip.get('id')!r} exceeds the sequence"
            )
        if start < cursor:
            raise FCPXMLError(
                f"{track_id} clips overlap at frame {start}; "
                "a deterministic FCPXML lane cannot represent that contract"
            )
        cursor = end


def _story_anchor(
    story: Sequence[tuple[int, int, int, ET.Element]],
    frame: int,
    track_id: str,
) -> tuple[ET.Element, int]:
    for timeline_start, timeline_end, local_start, element in story:
        if timeline_start <= frame < timeline_end:
            return element, local_start + frame - timeline_start
    raise FCPXMLError(
        f"{track_id} item at frame {frame} has no covering V1 story item"
    )


def _append_transition(
    spine: ET.Element,
    outgoing: Mapping[str, Any],
    incoming: Mapping[str, Any],
    effect_ids: Mapping[str, str],
    fps: int,
) -> None:
    # RabbitHole's EDL stores the authored transition on the cut that begins
    # at the boundary, so the incoming clip is authoritative here.
    transition = incoming.get("transition") or {}
    kind = str(transition.get("kind") or "cut")
    if kind not in {"cross_dissolve", "dip_to_black", "fade"}:
        return
    requested = int(transition.get("duration_frames") or 0)
    duration = min(
        requested,
        max(0, int(outgoing["duration_frames"]) // 2),
        max(0, int(incoming["duration_frames"]) // 2),
    )
    if duration <= 0:
        return
    effect_id = effect_ids.get(kind)
    if effect_id is None:
        return
    boundary = int(outgoing["end_frame"])
    offset = max(0, boundary - duration // 2)
    names = {
        "cross_dissolve": "Cross Dissolve",
        "dip_to_black": "Dip to Color",
        "fade": "Fade",
    }
    element = ET.SubElement(
        spine,
        "transition",
        {
            "name": names[kind],
            "offset": _time(offset, fps),
            "duration": _time(duration, fps),
        },
    )
    filter_video = ET.SubElement(
        element, "filter-video", {"ref": effect_id, "name": names[kind]}
    )
    if kind == "dip_to_black":
        ET.SubElement(
            filter_video,
            "param",
            {"name": "color", "key": "3", "value": "0 0 0 1"},
        )


def _append_audio_track(
    primary_story: Sequence[tuple[int, int, int, ET.Element]],
    track_id: str,
    lane: int,
    clips: Sequence[Mapping[str, Any]],
    resource_ids: Mapping[tuple[str, str], str],
    fps: int,
) -> None:
    roles = {
        "A1": "dialogue.narration",
        "A2": "dialogue.source",
        "A3": "music",
        "A4": "effects.sfx",
        "A5": "effects.utility",
    }
    for clip in sorted(clips, key=lambda item: (item["start_frame"], item["id"])):
        asset_id = clip.get("asset_id")
        path = clip.get("media_path")
        if not asset_id or not path:
            continue
        resource_id = resource_ids.get((str(asset_id), str(path)))
        if resource_id is None:
            raise FCPXMLError(f"missing FCPXML audio resource for {asset_id}")
        parent, local_offset = _story_anchor(
            primary_story, int(clip["start_frame"]), track_id
        )
        item = ET.SubElement(
            parent,
            "asset-clip",
            {
                "name": str(asset_id),
                "ref": resource_id,
                "offset": _time(local_offset, fps),
                "start": _time(int(clip.get("source_start_frame", 0)), fps),
                "duration": _time(int(clip["duration_frames"]), fps),
                "srcEnable": "audio",
                "audioRole": roles[track_id],
                "lane": str(lane),
            },
        )
        gain_db = _audio_gain_db(clip)
        if abs(gain_db) > 1e-9:
            ET.SubElement(
                item,
                "adjust-volume",
                {"amount": f"{_number(gain_db)}dB"},
            )
        note = ET.SubElement(item, "note")
        note.text = f"id={clip['id']} track={track_id}"


def _append_caption(
    primary_story: Sequence[tuple[int, int, int, ET.Element]],
    subtitle: Mapping[str, Any],
    fps: int,
) -> None:
    parent, local_offset = _story_anchor(
        primary_story, int(subtitle["start_frame"]), "SUBTITLES"
    )
    caption = ET.SubElement(
        parent,
        "caption",
        {
            "name": str(subtitle["id"]),
            "lane": "1",
            "offset": _time(local_offset, fps),
            "start": "0s",
            "duration": _time(int(subtitle["duration_frames"]), fps),
            "role": "caption.English",
        },
    )
    style_id = _xml_id(f"ts-{subtitle['id']}")
    text = ET.SubElement(caption, "text")
    styled = ET.SubElement(text, "text-style", {"ref": style_id})
    styled.text = str(subtitle["text"])
    style_def = ET.SubElement(caption, "text-style-def", {"id": style_id})
    ET.SubElement(
        style_def,
        "text-style",
        {
            "font": "Arial",
            "fontSize": "48",
            "fontColor": "1 1 1 1",
            "backgroundColor": "0 0 0 0.65",
            "alignment": "center",
        },
    )
    note = ET.SubElement(caption, "note")
    note.text = f"id={subtitle['id']} editable=1 source={subtitle.get('source', '')}"


def _append_title(
    primary_story: Sequence[tuple[int, int, int, ET.Element]],
    overlay: Mapping[str, Any],
    effect_id: str,
    fps: int,
) -> None:
    track_id = str(overlay.get("track") or "V3")
    kind = str(overlay.get("kind") or "")
    match = re.fullmatch(r"V([2-4])", track_id)
    if match is None:
        raise FCPXMLError(f"title {overlay['id']!r} has invalid track {track_id!r}")
    parent, local_offset = _story_anchor(
        primary_story, int(overlay["start_frame"]), track_id
    )
    title = ET.SubElement(
        parent,
        "title",
        {
            "name": str(overlay["id"]),
            "ref": effect_id,
            "lane": str(int(match.group(1)) - 1),
            "offset": _time(local_offset, fps),
            "start": "0s",
            "duration": _time(int(overlay["duration_frames"]), fps),
            "role": "titles",
        },
    )
    source_caption = kind == "source_caption"
    if source_caption:
        # Basic Title is born centred. FCPXML transform coordinates are
        # percentages of frame size, so this places the editable title within
        # lower-left title-safe while leaving the generator and text editable.
        ET.SubElement(
            title,
            "adjust-transform",
            {
                "position": "-38 -42",
                "scale": "1 1",
                "rotation": "0",
            },
        )
    style_id = _xml_id(f"ts-{overlay['id']}")
    text = ET.SubElement(title, "text")
    styled = ET.SubElement(text, "text-style", {"ref": style_id})
    styled.text = str(overlay["text"])
    style_def = ET.SubElement(title, "text-style-def", {"id": style_id})
    style_attributes = {
        "font": "Courier New",
        "fontSize": "32" if source_caption else "54",
        "fontColor": "1 1 1 1",
        "alignment": "left" if source_caption else "center",
    }
    if source_caption:
        style_attributes["backgroundColor"] = "0 0 0 0.65"
    ET.SubElement(style_def, "text-style", style_attributes)
    note = ET.SubElement(title, "note")
    note.text = f"id={overlay['id']} kind={kind} track={track_id}"
    if source_caption:
        note.text += " editable=1 layout=bottom-left"


def _validate_plan(plan: Mapping[str, Any]) -> None:
    if not isinstance(plan, Mapping):
        raise FCPXMLError("plan must be a mapping")
    if plan.get("schema_version") != "resolve-plan.v1":
        raise FCPXMLError("build_fcpxml requires schema_version resolve-plan.v1")
    if plan.get("fps") != 30:
        raise FCPXMLError("resolve-plan.v1 FCPXML export requires 30 fps")
    for key in ("build_id", "timeline_name", "duration_frames", "resolution"):
        if key not in plan:
            raise FCPXMLError(f"plan is missing {key}")


def _media_uri(path: str, project_root: Path | None) -> str:
    portable = path.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", portable):
        return "file:///" + quote(portable, safe="/:._~-")
    if portable.startswith("//"):
        return "file:" + quote(portable, safe="/:._~-")
    if portable.startswith("/"):
        return "file://" + quote(portable, safe="/:._~-")
    candidate = Path(path)
    if project_root is not None and not candidate.is_absolute():
        candidate = (project_root / candidate).resolve()
    if candidate.is_absolute():
        return candidate.as_uri()
    return "file:./" + quote(portable, safe="/:._~-")


def _media_type(path: str) -> str:
    return (
        "audio"
        if PurePosixPath(path.replace("\\", "/")).suffix.lower() in _AUDIO_SUFFIXES
        else "video"
    )


def _audio_rate(sample_rate: int) -> str:
    values = {
        32_000: "32k",
        44_100: "44.1k",
        48_000: "48k",
        88_200: "88.2k",
        96_000: "96k",
        176_400: "176.4k",
        192_000: "192k",
    }
    try:
        return values[sample_rate]
    except KeyError as exc:
        raise FCPXMLError(
            f"sample rate {sample_rate} is not representable in FCPXML 1.10"
        ) from exc


def _audio_gain_db(clip: Mapping[str, Any]) -> float:
    value = clip.get("gain_db", 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FCPXMLError(
            f"audio clip {clip.get('id')!r} gain_db must be a finite number"
        )
    gain_db = float(value)
    if not math.isfinite(gain_db) or not -80.0 <= gain_db <= 24.0:
        raise FCPXMLError(
            f"audio clip {clip.get('id')!r} gain_db must be between "
            "-80 and +24 dB"
        )
    return gain_db


def _time(frames: int, fps: int) -> str:
    if frames == 0:
        return "0s"
    return f"{int(frames)}/{int(fps)}s"


def _number(value: Any) -> str:
    number = float(value)
    if number == int(number):
        return str(int(number))
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _non_identity_transform(transform: Mapping[str, Any]) -> bool:
    return any(
        abs(float(transform.get(key, default)) - default) > 1e-9
        for key, default in (
            ("scale_x", 1.0),
            ("scale_y", 1.0),
            ("position_x", 0.0),
            ("position_y", 0.0),
            ("rotation", 0.0),
        )
    )


def _xml_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value)
    if not normalized or not re.match(r"[A-Za-z_]", normalized):
        normalized = "id-" + normalized
    return normalized


__all__ = ["FCPXMLError", "FCPXML_VERSION", "build_fcpxml", "write_fcpxml"]

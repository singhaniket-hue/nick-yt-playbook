"""Build attributed, timeline-ready video excerpts for a documentary project.

The input plan deliberately separates raw downloads from editorial use:

* ``sources`` records the original URL, publisher, date, rights note and local
  raw file.
* ``retain_fullscreen_evidence_slots`` is the 30% document/graphic side of the
  intended 70/30 visual balance.
* ``groups`` supplies a broad story-relevant source pool for every act.
* ``slot_overrides`` pins sensitive fact-check and confrontation sequences to
  exact source windows and optional on-screen caveats.

Every output is a unique 1920x1080/30fps H.264 asset. Portrait clips are
contained over a dark blurred duplicate instead of being cover-cropped. A
source/date strap is burned into every excerpt. The script can update the
active provenance map only after all requested excerpts render successfully;
the previous map is backed up first.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from rabbithole.provenance import AssetRecord, load_provenance
from rabbithole.slots import Slot, build_slots


REPO_ROOT = Path(__file__).resolve().parents[1]
FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/arialbd.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    Path("/Library/Fonts/Arial Bold.ttf"),
    Path("/System/Library/Fonts/Helvetica.ttc"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
)


def find_font(explicit: Path | None = None) -> Path:
    candidates = (explicit.expanduser(),) if explicit else FONT_CANDIDATES
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    requested = f" at {explicit}" if explicit else ""
    raise FileNotFoundError(
        "No FFmpeg drawtext font was found"
        + requested
        + ". Supply --font with a licensed TTF/OTF/TTC file."
    )


def resolve_project_path(project_root: Path, value: str) -> Path:
    """Resolve new project-relative paths with a legacy repo-relative fallback."""

    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    portable = (project_root / candidate).resolve()
    if portable.exists():
        return portable
    return (REPO_ROOT / candidate).resolve()


def portable_project_path(project_root: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def ffmpeg_filter_path(path: Path) -> str:
    return path.resolve().as_posix().replace(":", "\\:").replace("'", "\\'")


def run(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True)
    if result.returncode:
        stderr = result.stderr.decode("utf-8", errors="replace")[-2400:]
        raise RuntimeError(f"Command failed: {' '.join(args[:5])}\n{stderr}")


def slot_number(slot_id: str) -> int:
    if len(slot_id) != 4 or not slot_id.startswith("s") or not slot_id[1:].isdigit():
        raise ValueError(f"Invalid slot id {slot_id!r}; expected s001 style")
    return int(slot_id[1:])


def selected_slot_ids(
    slots: list[Slot],
    *,
    slot_range: str | None,
    slot_list: str | None,
) -> set[str]:
    known = {slot.slot_id for slot in slots}
    if slot_range and slot_list:
        raise ValueError("Use either --range or --slots, not both")
    if slot_list:
        selected = {item.strip() for item in slot_list.split(",") if item.strip()}
    elif slot_range:
        pieces = slot_range.split(":", 1)
        if len(pieces) != 2:
            raise ValueError("--range must look like s112:s164")
        lo, hi = map(slot_number, pieces)
        if hi < lo:
            raise ValueError("--range end must not precede its start")
        selected = {f"s{number:03d}" for number in range(lo, hi + 1)}
    else:
        selected = set(known)
    unknown = sorted(selected - known)
    if unknown:
        raise ValueError(f"Unknown slot ids: {', '.join(unknown)}")
    return selected


def group_for_slot(plan: dict, slot: Slot) -> dict:
    number = slot_number(slot.slot_id)
    for group in plan["groups"]:
        if int(group["slot_start"]) <= number <= int(group["slot_end"]):
            return group
    raise ValueError(f"No actual-footage group covers {slot.slot_id}")


def target_slots(plan: dict, slots: list[Slot]) -> list[Slot]:
    retained = set(plan["retain_fullscreen_evidence_slots"])
    return [slot for slot in slots if slot.slot_id not in retained]


def source_assignment(plan: dict, slots: list[Slot], slot: Slot) -> dict:
    override = dict(plan.get("slot_overrides", {}).get(slot.slot_id, {}))
    group = group_for_slot(plan, slot)
    if override:
        override["group"] = group["name"]
        return override

    retained = set(plan["retain_fullscreen_evidence_slots"])
    group_targets = [
        candidate
        for candidate in slots
        if candidate.slot_id not in retained
        and int(group["slot_start"])
        <= slot_number(candidate.slot_id)
        <= int(group["slot_end"])
    ]
    index = next(
        i for i, candidate in enumerate(group_targets) if candidate.slot_id == slot.slot_id
    )
    cycle = group["source_cycle"]
    source_key = cycle[index % len(cycle)]
    source = plan["sources"][source_key]
    windows = source["windows"]
    window_index = (index // len(cycle)) % len(windows)
    window_start, window_end = map(float, windows[window_index])
    room = max(0.0, window_end - window_start - slot.hold_seconds - 0.25)
    offset = math.fmod(slot_number(slot.slot_id) * 7.13, room) if room else 0.0
    return {
        "source": source_key,
        "source_start": round(window_start + offset, 3),
        "group": group["name"],
    }


def treatment_filters(name: str) -> list[str]:
    if name == "archive":
        return [
            "eq=contrast=0.97:saturation=0.82",
            "noise=alls=4:allf=t+u",
            "drawgrid=width=1920:height=4:thickness=1:color=black@0.055",
        ]
    if name == "field":
        return [
            "eq=contrast=1.025:saturation=0.92",
            "unsharp=5:5:0.28:5:5:0",
        ]
    if name == "social":
        return ["eq=contrast=1.02:saturation=0.95"]
    return ["eq=contrast=1.01:saturation=0.97"]


def build_filter(
    *,
    duration: float,
    treatment: str,
    label_file: Path,
    caption_file: Path | None,
    glitch_intro: bool,
    evidence_overlay: bool,
    font_path: Path,
) -> str:
    chains = [
        (
            f"[0:v]tpad=stop_mode=clone:stop_duration={duration + 1:.3f},"
            "fps=30,split=2[bg][fg]"
        ),
        (
            "[bg]scale=1920:1080:force_original_aspect_ratio=increase,"
            "crop=1920:1080,gblur=sigma=34,"
            "eq=brightness=-0.22:saturation=0.68[bg2]"
        ),
        (
            "[fg]scale=1920:1080:force_original_aspect_ratio=decrease,"
            "setsar=1[fg2]"
        ),
    ]
    base = "[bg2][fg2]overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1"
    base += "," + ",".join(treatment_filters(treatment))
    if glitch_intro:
        base += (
            ",noise=alls=52:allf=t+u:enable='lt(t,0.12)'"
            ",drawbox=x=0:y=0:w=iw:h=ih:color=white@0.78:t=fill:"
            "enable='between(t\\,0.045\\,0.075)'"
        )
    base += "[base]"
    chains.append(base)

    current = "[base]"
    if evidence_overlay:
        chains.extend(
            [
                (
                    f"[1:v]tpad=stop_mode=clone:stop_duration={duration + 1:.3f},"
                    "fps=30,scale=1380:776:force_original_aspect_ratio=decrease,"
                    "pad=1380:776:(ow-iw)/2:(oh-ih)/2:color=black,"
                    "format=rgba,colorchannelmixer=aa=0.97[evidence]"
                ),
                (
                    "[base]drawbox=x=250:y=135:w=1420:h=816:"
                    "color=black@0.72:t=fill[basebox]"
                ),
                (
                    "[basebox][evidence]overlay=x=270:y=155:"
                    "eof_action=pass:enable='gte(t\\,0.35)'[composed]"
                ),
            ]
        )
        current = "[composed]"

    label_path = ffmpeg_filter_path(label_file)
    finishing = current + (
        "drawbox=x=48:y=963:w=1060:h=72:color=black@0.72:t=fill"
        f",drawtext=fontfile='{ffmpeg_filter_path(font_path)}':"
        f"textfile='{label_path}':fontcolor=white:fontsize=28:"
        "x=70:y=983:borderw=1:bordercolor=black"
    )
    if caption_file is not None:
        caption_path = ffmpeg_filter_path(caption_file)
        finishing += (
            f",drawtext=fontfile='{ffmpeg_filter_path(font_path)}':"
            f"textfile='{caption_path}':fontcolor=white:fontsize=40:"
            "x=(w-text_w)/2:y=76:box=1:boxcolor=black@0.80:"
            "boxborderw=20:borderw=1:bordercolor=black"
        )
    finishing += "[outv]"
    chains.append(finishing)
    return ";".join(chains)


def build_asset(
    *,
    project_root: Path,
    output_dir: Path,
    slot: Slot,
    source_key: str,
    source: dict,
    assignment: dict,
    force: bool,
    font_path: Path,
) -> Path:
    raw_path = resolve_project_path(project_root, str(source["raw_path"]))
    if not raw_path.exists():
        raise FileNotFoundError(f"{source_key}: missing raw file {raw_path}")

    output_path = output_dir / f"{slot.slot_id}.mp4"
    if output_path.exists() and not force:
        return output_path

    output_dir.mkdir(parents=True, exist_ok=True)
    label_file = output_dir / f"{slot.slot_id}.label.txt"
    label_file.write_text(
        str(assignment.get("label", source["label"])), encoding="utf-8"
    )
    caption = assignment.get("caption")
    caption_file = None
    if caption:
        caption_file = output_dir / f"{slot.slot_id}.caption.txt"
        caption_file.write_text(str(caption), encoding="utf-8")

    duration = slot.hold_seconds
    frame_count = max(1, round(duration * 30))
    vf = build_filter(
        duration=duration,
        treatment=source.get("treatment", "clean"),
        label_file=label_file,
        caption_file=caption_file,
        glitch_intro=bool(assignment.get("glitch_intro")),
        evidence_overlay=bool(assignment.get("evidence_overlay")),
        font_path=font_path,
    )
    inputs = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{float(assignment.get('source_start', 0.0)):.3f}",
        "-i",
        str(raw_path),
    ]
    evidence_overlay = assignment.get("evidence_overlay")
    if evidence_overlay:
        overlay_path = resolve_project_path(project_root, str(evidence_overlay))
        if not overlay_path.exists():
            raise FileNotFoundError(
                f"{slot.slot_id}: missing evidence overlay {overlay_path}"
            )
        inputs.extend(["-stream_loop", "-1", "-i", str(overlay_path)])
    run(
        [
            *inputs,
            "-filter_complex",
            vf,
            "-map",
            "[outv]",
            "-frames:v",
            str(frame_count),
            "-r",
            "30",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "19",
            "-maxrate",
            "15M",
            "-bufsize",
            "30M",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    return output_path


def backup_provenance(project_root: Path) -> Path:
    provenance_path = project_root / "provenance.json"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    backup_dir = project_root / "backups" / f"{stamp}-pre-actual-footage"
    backup_dir.mkdir(parents=True, exist_ok=False)
    target = backup_dir / "provenance.json"
    shutil.copy2(provenance_path, target)
    return target


def rebind_provenance(
    *,
    project_root: Path,
    built: list[tuple[Slot, str, dict, dict, Path]],
) -> Path:
    provenance_path = project_root / "provenance.json"
    records = load_provenance(provenance_path)
    built_ids = {slot.slot_id for slot, *_rest in built}
    new_asset_ids = {f"actual-video-{slot_id}" for slot_id in built_ids}

    cleaned: list[AssetRecord] = []
    for record in records:
        if record.asset_id in new_asset_ids:
            continue
        cleaned.append(
            AssetRecord(
                asset_id=record.asset_id,
                tier=record.tier,
                provider=record.provider,
                original_url=record.original_url,
                license=record.license,
                retrieved_at=record.retrieved_at,
                local_path=record.local_path,
                used_in_slots=tuple(
                    slot_id
                    for slot_id in record.used_in_slots
                    if slot_id not in built_ids
                ),
                notes=record.notes,
            )
        )

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for slot, source_key, source, assignment, output_path in built:
        cleaned.append(
            AssetRecord(
                asset_id=f"actual-video-{slot.slot_id}",
                tier="primary",
                provider=source["provider"],
                original_url=source["url"],
                license=source["license"],
                retrieved_at=now,
                local_path=portable_project_path(project_root, output_path),
                used_in_slots=(slot.slot_id,),
                notes=(
                    f"Actual-footage base; source_key={source_key}; "
                    f"source_start={float(assignment.get('source_start', 0.0)):.3f}s; "
                    f"story_group={assignment['group']}; treatment="
                    f"{source.get('treatment', 'clean')}. Source/date strap burned in. "
                    "Rights note is a workflow flag, not a licence grant."
                ),
            )
        )

    provenance_path.write_text(
        json.dumps([asdict(record) for record in cleaned], indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    return provenance_path


def duration_ratio(slots: list[Slot], selected: set[str]) -> tuple[float, float, float]:
    total = sum(slot.hold_seconds for slot in slots)
    video = sum(slot.hold_seconds for slot in slots if slot.slot_id in selected)
    return video, total, video / total if total else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project",
        type=Path,
        required=True,
        help="Project root containing narration/timing.json and the research plan",
    )
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--range", dest="slot_range")
    parser.add_argument("--slots", dest="slot_list")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--font",
        type=Path,
        help="Licensed TTF/OTF/TTC used for source straps (auto-detected by OS)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply-provenance", action="store_true")
    args = parser.parse_args()

    project_root = args.project.resolve()
    plan_path = (
        args.plan.resolve()
        if args.plan
        else project_root / "research" / "actual-footage-plan.json"
    )
    plan = read_json(plan_path)
    timing = read_json(project_root / "narration" / "timing.json")
    slots = build_slots(timing)
    requested = selected_slot_ids(
        slots, slot_range=args.slot_range, slot_list=args.slot_list
    )
    targets = target_slots(plan, slots)
    targets = [slot for slot in targets if slot.slot_id in requested]
    if not targets:
        raise SystemExit("No video-led slots selected")

    output_value = Path(str(plan["output_directory"])).expanduser()
    output_dir = (
        output_value.resolve()
        if output_value.is_absolute()
        else (project_root / output_value).resolve()
    )
    font_path = find_font(args.font)
    assignments: list[tuple[Slot, str, dict, dict]] = []
    for slot in targets:
        assignment = source_assignment(plan, slots, slot)
        source_key = assignment["source"]
        source = plan["sources"][source_key]
        assignments.append((slot, source_key, source, assignment))
        print(
            f"{slot.slot_id} {slot.start:8.3f}-{slot.end:8.3f} "
            f"{source_key:22} @{float(assignment.get('source_start', 0.0)):7.3f}s"
        )

    planned_ids = {slot.slot_id for slot in target_slots(plan, slots)}
    planned_video, total, planned_ratio = duration_ratio(slots, planned_ids)
    selected_video = sum(slot.hold_seconds for slot in targets)
    print()
    print(
        f"Planned full balance: {planned_video:.3f}/{total:.3f}s "
        f"= {planned_ratio:.3%} actual-video base"
    )
    print(
        f"This build: {len(targets)} slots, {selected_video:.3f}s "
        f"({targets[0].slot_id}..{targets[-1].slot_id})"
    )
    if args.dry_run:
        return 0

    built: list[tuple[Slot, str, dict, dict, Path]] = []
    for index, (slot, source_key, source, assignment) in enumerate(assignments, 1):
        print(f"[{index}/{len(assignments)}] Rendering {slot.slot_id} from {source_key}")
        path = build_asset(
            project_root=project_root,
            output_dir=output_dir,
            slot=slot,
            source_key=source_key,
            source=source,
            assignment=assignment,
            force=args.force,
            font_path=font_path,
        )
        built.append((slot, source_key, source, assignment, path))

    backup_path = None
    if args.apply_provenance:
        backup_path = backup_provenance(project_root)
        rebind_provenance(project_root=project_root, built=built)

    available_slots = [
        slot
        for slot in target_slots(plan, slots)
        if (output_dir / f"{slot.slot_id}.mp4").exists()
    ]
    available_seconds = sum(slot.hold_seconds for slot in available_slots)
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "plan": portable_project_path(project_root, plan_path),
        "planned_full_video_seconds": round(planned_video, 3),
        "episode_seconds": round(total, 3),
        "planned_full_video_ratio": round(planned_ratio, 6),
        "built_slot_count": len(built),
        "built_video_seconds": round(selected_video, 3),
        "built_slots": [slot.slot_id for slot, *_rest in built],
        "available_slot_count": len(available_slots),
        "available_video_seconds": round(available_seconds, 3),
        "available_slots": [slot.slot_id for slot in available_slots],
        "provenance_backup": (
            portable_project_path(project_root, backup_path) if backup_path else None
        ),
    }
    report_path = project_root / "research" / "actual-footage-build.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Wrote {report_path}")
    if backup_path:
        print(f"Backed up provenance to {backup_path}")
        print(f"Rebound {len(built)} slots in {project_root / 'provenance.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

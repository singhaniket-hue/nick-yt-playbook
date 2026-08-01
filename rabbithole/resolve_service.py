"""High-level, JSON-friendly Resolve orchestration used by CLI and MCP."""

from __future__ import annotations

import json
import math
import os
import platform
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping

from .encoding import require_filter
from .resolve_install import install_style_assets
from .resolve_audio import prepare_resolve_audio_stems
from .resolve_manifest import (
    ResolveManifestError,
    _discover_sound_manifest,
    compile_resolve_plan,
    write_resolve_bundle,
)
from .resolve_runner import (
    _atomic_write_json,
    console_loader_command,
    enqueue_job,
    install_runner,
    read_status,
    run_pending_jobs,
)
from .resolve_safety import (
    DEFAULT_LOCK_RELATIVE_PATH,
    is_path_within,
    require_write_path,
    utc_now,
)
from .resolve_platform import (
    find_resolve_application,
    host_report,
    resolve_scripting_module_available,
)


class ResolveServiceError(RuntimeError):
    """A high-level Resolve command is invalid or unsafe."""


CAPTION_STYLE_APPROVAL_SCHEMA_VERSION = 1
CAPTION_STYLE_APPROVAL_RELATIVE_DIRECTORY = (
    Path("resolve") / "review-approvals" / "caption-style"
)
VIDEO_SOURCE_RANGE_TOLERANCE_SECONDS = 0.001


def _root(path: os.PathLike[str] | str) -> Path:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ResolveServiceError(f"project root does not exist: {root}")
    return root


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return os.fspath(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _mode(value: str | None) -> str:
    mode = str(value or "free").strip().lower()
    if mode not in {"free", "studio"}:
        raise ResolveServiceError("Resolve mode must be 'free' or 'studio'")
    return mode


def _caption_style_contract(plan: Mapping[str, Any]) -> dict[str, Any] | None:
    policy = plan.get("subtitle_policy")
    if not isinstance(policy, Mapping):
        return None
    style = policy.get("track_style")
    if not isinstance(style, Mapping):
        return None
    return dict(style)


def _caption_style_host() -> dict[str, str]:
    """Return the local host identity that scopes a visual approval."""

    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "node": platform.node(),
    }


def _caption_style_approval_path(root: Path, build_id: str) -> Path:
    safe_build_id = str(build_id or "")
    if (
        not safe_build_id.startswith("b-")
        or len(safe_build_id) > 80
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in safe_build_id
        )
    ):
        raise ResolveServiceError(
            f"invalid build ID for caption-style approval: {safe_build_id!r}"
        )
    destination = (
        root
        / CAPTION_STYLE_APPROVAL_RELATIVE_DIRECTORY
        / f"{safe_build_id}.json"
    )
    return require_write_path(destination, (root / "resolve",))


def _caption_style_approval_status_for_plan(
    root: Path,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    style = _caption_style_contract(plan)
    required = bool(style and style.get("render_approval_required"))
    build_id = str(plan.get("build_id") or "")
    timeline_name = str(plan.get("timeline_name") or "")
    if not required:
        return {
            "required": False,
            "approved": True,
            "reason": "not_required",
            "build_id": build_id or None,
            "timeline_name": timeline_name or None,
            "approval_path": None,
        }

    contract_sha256 = str((style or {}).get("contract_sha256") or "")
    if len(contract_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in contract_sha256
    ):
        raise ResolveServiceError(
            "compiled presentation-caption style has no valid contract checksum"
        )
    path = _caption_style_approval_path(root, build_id)
    base = {
        "required": True,
        "approved": False,
        "build_id": build_id,
        "timeline_name": timeline_name,
        "contract_sha256": contract_sha256,
        "approval_path": os.fspath(path),
    }
    if not path.is_file():
        return {**base, "reason": "approval_missing"}
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {**base, "reason": "approval_unreadable", "detail": str(exc)}
    if not isinstance(document, Mapping):
        return {**base, "reason": "approval_not_an_object"}
    expected = {
        "schema_version": CAPTION_STYLE_APPROVAL_SCHEMA_VERSION,
        "build_id": build_id,
        "timeline_name": timeline_name,
        "contract_sha256": contract_sha256,
        "host": _caption_style_host(),
    }
    mismatches = sorted(
        key for key, value in expected.items() if document.get(key) != value
    )
    if mismatches:
        return {
            **base,
            "reason": "approval_contract_mismatch",
            "mismatched_fields": mismatches,
        }
    return {
        **base,
        "approved": True,
        "reason": "approved_on_this_host",
        "approved_at": document.get("approved_at"),
        "note": document.get("note"),
    }


def caption_style_approval_status(
    project_root: os.PathLike[str] | str,
    *,
    overrides_path: os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    """Read the machine-local readability approval for the current build."""

    root = _root(project_root)
    plan = compile_resolve_plan(
        root,
        overrides_path=Path(overrides_path) if overrides_path else None,
    )
    return _json_safe(_caption_style_approval_status_for_plan(root, plan))


def approve_caption_style(
    project_root: os.PathLike[str] | str,
    *,
    overrides_path: os.PathLike[str] | str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Record a visual Track Style check for one built timeline and host."""

    root = _root(project_root)
    plan = compile_resolve_plan(
        root,
        overrides_path=Path(overrides_path) if overrides_path else None,
    )
    approval = _caption_style_approval_status_for_plan(root, plan)
    if not approval["required"]:
        return _json_safe(approval)

    status = read_status(root)
    if (
        status.get("state") != "succeeded"
        or status.get("detail") != "build_succeeded"
        or status.get("timeline_name") != plan.get("timeline_name")
    ):
        raise ResolveServiceError(
            "caption style can be approved only after this exact AUTO_BUILD "
            "timeline succeeds in Resolve"
        )
    style = _caption_style_contract(plan)
    assert style is not None  # required=True above proves the contract exists.
    path = _caption_style_approval_path(root, str(plan["build_id"]))
    payload = {
        "schema_version": CAPTION_STYLE_APPROVAL_SCHEMA_VERSION,
        "build_id": str(plan["build_id"]),
        "timeline_name": str(plan["timeline_name"]),
        "contract_sha256": str(style["contract_sha256"]),
        "host": _caption_style_host(),
        "approved_at": utc_now(),
        "approved_by_user": True,
        "note": str(note).strip() if note else None,
    }
    _atomic_write_json(path, payload)
    return _json_safe(_caption_style_approval_status_for_plan(root, plan))


def _resolve_installation() -> dict[str, Any]:
    host = host_report()
    executable = find_resolve_application()
    module_available = resolve_scripting_module_available()
    return {
        "installed": executable is not None,
        "executable": os.fspath(executable) if executable else None,
        "scripting_module_available": module_available,
        "script_api": os.environ.get("RESOLVE_SCRIPT_API")
        or host["resolve_script_api"],
        "script_library": os.environ.get("RESOLVE_SCRIPT_LIB")
        or host["resolve_script_library"],
        "script_module": host["resolve_script_module"],
    }


def _audit_video_source_ranges(
    root: Path,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify every requested source range fits inside its video file.

    Resolve may accept a timeline whose final source frame is unavailable and
    then expose the problem only during playback or render.  Probe each unique
    video once and fail closed before a job reaches Resolve.  A one-millisecond
    tolerance accommodates MP4 timescale rounding without masking a missing
    authored frame.
    """

    try:
        fps = int(plan.get("fps", 0))
    except (TypeError, ValueError):
        fps = 0
    clips = [
        clip
        for clip in plan.get("clips", [])
        if isinstance(clip, Mapping)
        and clip.get("media_type") == "video"
        and clip.get("media_path")
    ]
    base = {
        "ok": True,
        "clip_count": len(clips),
        "unique_media_count": 0,
        "tolerance_seconds": VIDEO_SOURCE_RANGE_TOLERANCE_SECONDS,
        "probe_failures": [],
        "shortages": [],
    }
    if not clips:
        return base
    if fps <= 0:
        return {
            **base,
            "ok": False,
            "probe_failures": [
                {
                    "media_path": None,
                    "detail": "compiled plan has no positive fps",
                }
            ],
        }

    requirements: dict[str, dict[str, Any]] = {}
    for clip in clips:
        portable_path = os.fspath(clip["media_path"])
        try:
            source_end_frame = int(clip.get("source_end_frame", 0))
        except (TypeError, ValueError):
            source_end_frame = 0
        current = requirements.setdefault(
            portable_path,
            {
                "required_end_frame": 0,
                "clip_ids": [],
            },
        )
        current["required_end_frame"] = max(
            int(current["required_end_frame"]), source_end_frame
        )
        current["clip_ids"].append(str(clip.get("id") or clip.get("slot_id") or ""))
    base["unique_media_count"] = len(requirements)

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        base["ok"] = False
        base["probe_failures"].append(
            {"media_path": None, "detail": "ffprobe is not available on PATH"}
        )
        return base

    durations: dict[str, float] = {}
    for portable_path in sorted(requirements):
        source = Path(portable_path).expanduser()
        if not source.is_absolute():
            source = root / source
        source = source.resolve()
        try:
            completed = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=duration,nb_frames,avg_frame_rate",
                    "-of",
                    "json",
                    os.fspath(source),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            base["probe_failures"].append(
                {"media_path": portable_path, "detail": str(exc)}
            )
            continue
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "ffprobe returned no duration"
            base["probe_failures"].append(
                {"media_path": portable_path, "detail": detail}
            )
            continue
        try:
            document = json.loads(completed.stdout)
            stream = document["streams"][0]
            if not isinstance(stream, Mapping):
                raise TypeError("first video stream is not an object")
            candidates: list[float] = []
            raw_duration = stream.get("duration")
            if raw_duration not in (None, "", "N/A"):
                candidates.append(float(raw_duration))
            raw_frames = stream.get("nb_frames")
            raw_rate = str(stream.get("avg_frame_rate") or "")
            if raw_frames not in (None, "", "N/A") and "/" in raw_rate:
                numerator_text, denominator_text = raw_rate.split("/", 1)
                numerator = int(numerator_text)
                denominator = int(denominator_text)
                if numerator > 0 and denominator > 0:
                    candidates.append(
                        int(raw_frames) * denominator / numerator
                    )
            if not candidates or any(
                not math.isfinite(value) or value < 0 for value in candidates
            ):
                raise ValueError("video stream has no finite non-negative duration")
            duration = min(candidates)
        except (
            IndexError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            base["probe_failures"].append(
                {
                    "media_path": portable_path,
                    "detail": f"invalid ffprobe video-stream duration: {exc}",
                }
            )
            continue
        durations[portable_path] = duration

    for portable_path, requirement in requirements.items():
        if portable_path not in durations:
            continue
        required_end_frame = int(requirement["required_end_frame"])
        required_seconds = required_end_frame / fps
        duration = durations[portable_path]
        shortage = required_seconds - duration
        if shortage > VIDEO_SOURCE_RANGE_TOLERANCE_SECONDS:
            base["shortages"].append(
                {
                    "media_path": portable_path,
                    "required_end_frame": required_end_frame,
                    "required_seconds": required_seconds,
                    "available_seconds": duration,
                    "shortage_seconds": shortage,
                    "clip_ids": requirement["clip_ids"],
                }
            )

    base["ok"] = not base["probe_failures"] and not base["shortages"]
    return base


def portability_report(*, mode: str = "free") -> dict[str, Any]:
    """Report host prerequisites without opening Resolve or touching a project."""

    selected_mode = _mode(mode)
    host = host_report()
    tools = {
        name: shutil.which(name)
        for name in ("ffmpeg", "ffprobe", "pdftoppm", "yt-dlp")
    }
    try:
        from .sources.capture import find_browser

        browser = find_browser()
    except Exception:
        browser = None
    checks = [
        {
            "name": "host_python",
            "ok": bool(host["python_ok"]),
            "severity": "error",
            "detail": {
                "version": host["python"],
                "minimum": "3.11",
                "executable": host["python_executable"],
            },
        },
        {
            "name": "davinci_resolve",
            "ok": bool(host["resolve_installed"]),
            "severity": "error",
            "detail": host["resolve_application"],
        },
    ]
    checks.extend(
        {
            "name": name,
            "ok": tools[name] is not None,
            "severity": "error" if name in {"ffmpeg", "ffprobe"} else "warning",
            "detail": tools[name],
        }
        for name in tools
    )
    try:
        ass_ffmpeg = require_filter("ass")
        ass_filter_detail: Any = ass_ffmpeg
        ass_filter_ok = True
    except RuntimeError as exc:
        ass_filter_detail = str(exc)
        ass_filter_ok = False
    checks.append(
        {
            "name": "ffmpeg_ass_filter",
            "ok": ass_filter_ok,
            "severity": "error",
            "detail": ass_filter_detail,
        }
    )
    checks.append(
        {
            "name": "chromium_browser",
            "ok": browser is not None,
            "severity": "warning",
            "detail": os.fspath(browser) if browser else None,
        }
    )
    if selected_mode == "studio":
        checks.append(
            {
                "name": "studio_external_bridge",
                "ok": bool(host["resolve_script_module_available"]),
                "severity": "error",
                "detail": {
                    "module": host["resolve_script_module"],
                    "api": host["resolve_script_api"],
                    "library": host["resolve_script_library"],
                },
            }
        )
    else:
        checks.append(
            {
                "name": "free_console_python",
                "ok": False,
                "severity": "warning",
                "detail": (
                    "Open Workspace > Console and run `import sys; "
                    "print(sys.version)`. The checked-in runner requires 3.11+."
                ),
            }
        )
    return {
        "ok": all(
            item["ok"] or item["severity"] != "error" for item in checks
        ),
        "mode": selected_mode,
        "host": host,
        "checks": checks,
        "safe": {
            "resolve_started": False,
            "render_queue_touched": False,
            "project_mutated": False,
        },
    }


def preflight_project(
    project_root: os.PathLike[str] | str,
    *,
    mode: str = "free",
    overrides_path: os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    """Compile without writing and return actionable environment/project checks."""

    root = _root(project_root)
    selected_mode = _mode(mode)
    checks: list[dict[str, Any]] = []
    installation = _resolve_installation()
    checks.append(
        {
            "name": "davinci_resolve",
            "ok": bool(installation["installed"]),
            "severity": "error",
            "detail": installation,
        }
    )
    checks.extend(
        {
            "name": executable,
            "ok": shutil.which(executable) is not None,
            "severity": "error",
            "detail": shutil.which(executable),
        }
        for executable in ("ffmpeg", "ffprobe")
    )
    checkout_root = Path(__file__).resolve().parents[1]
    runtime_assets = (
        checkout_root / "scripts" / "rabbithole_resolve_runner.py",
        checkout_root / "resolve" / "crowley_style.yaml",
        checkout_root / "style" / "luts" / "crowley-noir.cube",
    )
    missing_runtime_assets = [
        os.fspath(path) for path in runtime_assets if not path.is_file()
    ]
    checks.append(
        {
            "name": "editable_checkout_assets",
            "ok": not missing_runtime_assets,
            "severity": "error",
            "detail": (
                "runner, style contract, and LUT available"
                if not missing_runtime_assets
                else {
                    "missing": missing_runtime_assets,
                    "action": (
                        "run RabbitHole from a repository checkout using an "
                        "editable install; standalone wheels are not supported"
                    ),
                }
            ),
        }
    )
    lock_path = root / DEFAULT_LOCK_RELATIVE_PATH
    checks.append(
        {
            "name": "project_lock",
            "ok": not lock_path.exists(),
            "severity": "error",
            "detail": os.fspath(lock_path) if lock_path.exists() else "clear",
        }
    )
    if selected_mode == "studio":
        checks.append(
            {
                "name": "studio_external_bridge",
                "ok": bool(installation["scripting_module_available"]),
                "severity": "error",
                "detail": (
                    "DaVinciResolveScript importable"
                    if installation["scripting_module_available"]
                    else "Studio external mode requires DaVinciResolveScript"
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "free_in_app_runner",
                "ok": True,
                "severity": "info",
                "detail": "build/render/handoff jobs execute inside Resolve",
            }
        )
        checks.append(
            {
                "name": "console_python_version",
                "ok": False,
                "severity": "warning",
                "detail": (
                    "manual gate: run `import sys; print(sys.version)` in "
                    "Resolve's Python Console; RabbitHole requires Python 3.11+"
                ),
            }
        )

    try:
        plan = compile_resolve_plan(
            root,
            overrides_path=Path(overrides_path) if overrides_path else None,
        )
    except (OSError, ResolveManifestError, ValueError) as exc:
        checks.append(
            {
                "name": "project_contract",
                "ok": False,
                "severity": "error",
                "detail": str(exc),
            }
        )
        plan_summary = None
    else:
        error_reviews = [
            flag for flag in plan.get("review_flags", []) if flag.get("severity") == "error"
        ]
        checks.append(
            {
                "name": "project_contract",
                "ok": not error_reviews,
                "severity": "error",
                "detail": {
                    "build_id": plan["build_id"],
                    "timeline_name": plan["timeline_name"],
                    "cuts": len(plan.get("clips", [])),
                    "missing_media": len(plan.get("missing_media", [])),
                    "blocking_reviews": len(error_reviews),
                },
            }
        )
        plan_summary = checks[-1]["detail"]
        source_ranges = _audit_video_source_ranges(root, plan)
        checks.append(
            {
                "name": "video_source_ranges",
                "ok": bool(source_ranges["ok"]),
                "severity": "error",
                "detail": source_ranges,
            }
        )
        plan_summary["source_range_audit"] = source_ranges

    return {
        "ok": all(item["ok"] or item["severity"] != "error" for item in checks),
        "mode": selected_mode,
        "project_root": os.fspath(root),
        "installation": installation,
        "plan": plan_summary,
        "checks": checks,
    }


def prepare_project(
    project_root: os.PathLike[str] | str,
    *,
    output_dir: os.PathLike[str] | str | None = None,
    overrides_path: os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    root = _root(project_root)
    destination: Path | None = None
    if output_dir is not None:
        candidate = Path(output_dir).expanduser()
        destination = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        if not is_path_within(destination, root / "resolve"):
            raise ResolveServiceError(
                f"prepared bundles must stay under {root / 'resolve'}: {destination}"
            )
    audio_stems: dict[str, Any] | None = None
    sound_manifest_path = _discover_sound_manifest(root)
    if sound_manifest_path is not None:
        audio_stems = prepare_resolve_audio_stems(root, sound_manifest_path)
    result = write_resolve_bundle(
        root,
        output_dir=destination,
        overrides_path=Path(overrides_path) if overrides_path else None,
    )
    response = dict(result)
    plan = response.pop("plan")
    source_ranges = _audit_video_source_ranges(root, plan)
    if not source_ranges["ok"]:
        raise ResolveServiceError(
            "Resolve bundle has unavailable video source frames; run "
            "`rabbithole resolve preflight` and repair the reported media "
            "before queuing a build."
        )
    caption_style = _caption_style_approval_status_for_plan(root, plan)
    response["summary"] = {
        "duration_frames": plan.get("duration_frames"),
        "cuts": len(plan.get("clips", [])),
        "audio_clips": len(plan.get("audio", [])),
        "subtitles": len(plan.get("subtitles", [])),
        "missing_media": len(plan.get("missing_media", [])),
        "review_flags": len(plan.get("review_flags", [])),
        "blocking_review_flags": sum(
            1
            for flag in plan.get("review_flags", [])
            if flag.get("severity") == "error"
        ),
        "source_range_audit": source_ranges,
        "caption_style": caption_style,
    }
    if audio_stems is not None:
        stem_findings = list(audio_stems["manifest"].get("findings", []))
        response["audio_stems"] = {
            "generated": bool(audio_stems["generated"]),
            "fingerprint": str(audio_stems["fingerprint"]),
            "manifest_path": os.fspath(audio_stems["manifest_path"]),
            "findings": stem_findings,
            "warning_count": sum(
                1 for finding in stem_findings if finding.get("severity") == "warning"
            ),
        }
    return _json_safe(response)


def queue_project_action(
    project_root: os.PathLike[str] | str,
    action: str,
    *,
    mode: str = "free",
    overrides_path: os.PathLike[str] | str | None = None,
    options: Mapping[str, Any] | None = None,
    execute_studio: bool = True,
) -> dict[str, Any]:
    root = _root(project_root)
    selected_mode = _mode(mode)
    bundle = prepare_project(root, overrides_path=overrides_path)
    summary = bundle.get("summary")
    blocking_reviews = (
        int(summary.get("blocking_review_flags", 0))
        if isinstance(summary, Mapping)
        else 0
    )
    if blocking_reviews:
        raise ResolveServiceError(
            f"Resolve job not queued: the compiled plan has {blocking_reviews} "
            "blocking review flag(s). Run `rabbithole resolve preflight`, fix "
            "the reported media/evidence errors, and prepare again."
        )
    caption_style = (
        summary.get("caption_style") if isinstance(summary, Mapping) else None
    )
    if (
        action == "render"
        and isinstance(caption_style, Mapping)
        and caption_style.get("required")
        and not caption_style.get("approved")
    ):
        raise ResolveServiceError(
            "Resolve render not queued: verify PRESENTATION_SUBTITLES Track "
            "Style uses white text on a black background at 65% or greater "
            "opacity, then run `rabbithole resolve approve-caption-style` "
            "for this project on this machine."
        )
    job_options = dict(options or {})
    job_options["mode"] = selected_mode
    job = enqueue_job(root, action, bundle["plan_path"], job_options)
    response: dict[str, Any] = {
        "mode": selected_mode,
        "action": action,
        "build": bundle,
        "job": _json_safe(job),
        "status": _json_safe(read_status(root)),
    }
    if selected_mode == "studio" and execute_studio and job.get("state") == "queued":
        response["execution"] = _json_safe(
            run_pending_jobs(
                project_root=root,
                job_id=str(job["job_id"]),
                allow_studio_external=True,
            )
        )
        response["status"] = _json_safe(read_status(root))
    else:
        response["detail"] = "awaiting_in_app_runner"
        response["console_loader"] = job.get("console_loader") or console_loader_command(
            root, job_id=str(job["job_id"])
        )
    return response


def project_status(project_root: os.PathLike[str] | str) -> dict[str, Any]:
    return _json_safe(read_status(_root(project_root)))


def install_resolve_integration(
    *,
    runner_destination: os.PathLike[str] | str | None = None,
    support_root: os.PathLike[str] | str | None = None,
    include_style_assets: bool = True,
) -> dict[str, Any]:
    runner = install_runner(runner_destination)
    assets = (
        install_style_assets(
            support_root=Path(support_root).expanduser().resolve()
            if support_root
            else None
        )
        if include_style_assets
        else []
    )
    return {"runner": os.fspath(runner), "style_assets": _json_safe(assets)}


__all__ = [
    "ResolveServiceError",
    "approve_caption_style",
    "caption_style_approval_status",
    "install_resolve_integration",
    "preflight_project",
    "prepare_project",
    "portability_report",
    "project_status",
    "queue_project_action",
]

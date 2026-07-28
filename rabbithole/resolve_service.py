"""High-level, JSON-friendly Resolve orchestration used by CLI and MCP."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Mapping

from .resolve_install import install_style_assets
from .resolve_manifest import ResolveManifestError, compile_resolve_plan, write_resolve_bundle
from .resolve_runner import (
    console_loader_command,
    enqueue_job,
    install_runner,
    read_status,
    run_pending_jobs,
)
from .resolve_safety import DEFAULT_LOCK_RELATIVE_PATH, is_path_within


class ResolveServiceError(RuntimeError):
    """A high-level Resolve command is invalid or unsafe."""


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


def _resolve_installation() -> dict[str, Any]:
    executable: Path | None = None
    candidates: list[Path] = []
    if sys.platform == "win32":
        program_files = os.environ.get("ProgramFiles")
        if program_files:
            candidates.append(
                Path(program_files)
                / "Blackmagic Design"
                / "DaVinci Resolve"
                / "Resolve.exe"
            )
    elif sys.platform == "darwin":
        candidates.append(
            Path("/Applications/DaVinci Resolve/DaVinci Resolve.app")
        )
    else:
        candidates.extend((Path("/opt/resolve/bin/resolve"), Path("/usr/bin/resolve")))
    discovered = shutil.which("Resolve") or shutil.which("resolve")
    if discovered:
        candidates.insert(0, Path(discovered))
    for candidate in candidates:
        if candidate.exists():
            executable = candidate.resolve()
            break
    module_available = importlib.util.find_spec("DaVinciResolveScript") is not None
    return {
        "installed": executable is not None,
        "executable": os.fspath(executable) if executable else None,
        "scripting_module_available": module_available,
        "script_api": os.environ.get("RESOLVE_SCRIPT_API"),
        "script_library": os.environ.get("RESOLVE_SCRIPT_LIB"),
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
    result = write_resolve_bundle(
        root,
        output_dir=destination,
        overrides_path=Path(overrides_path) if overrides_path else None,
    )
    response = dict(result)
    plan = response.pop("plan")
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
    "install_resolve_integration",
    "preflight_project",
    "prepare_project",
    "project_status",
    "queue_project_action",
]

"""Argparse integration for the Resolve compiler, queue, runner, and handoff."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Callable

from .resolve_service import (
    install_resolve_integration,
    portability_report,
    preflight_project,
    prepare_project,
    project_status,
    queue_project_action,
)
from .resolve_handoff import restore_handoff, validate_handoff
from .episode_bundle import (
    package_episode,
    restore_episode_bundle,
    validate_episode_bundle,
)


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _guard(call: Callable[[], dict[str, Any]]) -> int:
    try:
        result = call()
    except Exception as exc:
        _print(
            {
                "ok": False,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        )
        return 1
    _print(result)
    return 0


def cmd_resolve_preflight(args: argparse.Namespace) -> int:
    try:
        result = preflight_project(
            args.project_root,
            mode=args.mode,
            overrides_path=args.overrides,
        )
    except Exception as exc:
        _print(
            {
                "ok": False,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        )
        return 1
    _print(result)
    return 0 if result["ok"] else 1


def cmd_resolve_prepare(args: argparse.Namespace) -> int:
    return _guard(
        lambda: prepare_project(
            args.project_root,
            output_dir=args.out_dir,
            overrides_path=args.overrides,
        )
    )


def cmd_resolve_install_runner(args: argparse.Namespace) -> int:
    return _guard(
        lambda: install_resolve_integration(
            runner_destination=args.destination,
            support_root=args.support_root,
            include_style_assets=not args.runner_only,
        )
    )


def _queue(args: argparse.Namespace, action: str) -> int:
    options: dict[str, Any] = {}
    if action == "render":
        if getattr(args, "out", None):
            options["output_path"] = os.fspath(Path(args.out).expanduser().resolve())
        if getattr(args, "wait", False):
            options["wait"] = True
            options["wait_timeout_seconds"] = float(args.wait_timeout)
    if action == "handoff":
        options["destination"] = os.fspath(Path(args.out).expanduser().resolve())
        if getattr(args, "include_proxy_media", False):
            options["include_proxy_media"] = True
    return _guard(
        lambda: queue_project_action(
            args.project_root,
            action,
            mode=args.mode,
            overrides_path=args.overrides,
            options=options,
        )
    )


def cmd_resolve_build(args: argparse.Namespace) -> int:
    return _queue(args, "build")


def cmd_resolve_render(args: argparse.Namespace) -> int:
    return _queue(args, "render")


def cmd_resolve_handoff(args: argparse.Namespace) -> int:
    return _queue(args, "handoff")


def cmd_resolve_status(args: argparse.Namespace) -> int:
    try:
        result = project_status(args.project_root)
    except Exception as exc:
        _print(
            {
                "ok": False,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        )
        return 1
    _print(result)
    return 1 if result.get("state") == "failed" else 0


def cmd_resolve_doctor(args: argparse.Namespace) -> int:
    result = portability_report(mode=args.mode)
    _print(result)
    return 0 if result["ok"] else 1


def cmd_resolve_verify_handoff(args: argparse.Namespace) -> int:
    return _guard(lambda: validate_handoff(args.package))


def cmd_resolve_restore_handoff(args: argparse.Namespace) -> int:
    return _guard(lambda: restore_handoff(args.package, args.out))


def cmd_resolve_bundle(args: argparse.Namespace) -> int:
    return _guard(lambda: package_episode(args.project_root, args.out))


def cmd_resolve_verify_bundle(args: argparse.Namespace) -> int:
    return _guard(lambda: validate_episode_bundle(args.package))


def cmd_resolve_restore_bundle(args: argparse.Namespace) -> int:
    return _guard(lambda: restore_episode_bundle(args.package, args.out))


def add_resolve_parser(subparsers: Any) -> argparse.ArgumentParser:
    resolve = subparsers.add_parser(
        "resolve",
        help="Compile, build, render, and hand off editable DaVinci Resolve projects",
    )
    actions = resolve.add_subparsers(dest="resolve_command", required=True)

    doctor = actions.add_parser(
        "doctor",
        help="Inspect portable host prerequisites without opening Resolve",
    )
    doctor.add_argument(
        "--mode",
        choices=("free", "studio"),
        default="free",
        help="Check Free in-app or Studio external scripting prerequisites",
    )
    doctor.set_defaults(func=cmd_resolve_doctor)

    def project_command(name: str, help_text: str) -> argparse.ArgumentParser:
        command = actions.add_parser(name, help=help_text)
        command.add_argument("project_root", help="Episode root containing timing/EDL/provenance")
        command.add_argument(
            "--mode",
            choices=("free", "studio"),
            default="free",
            help="Free queues work for the in-app runner; Studio may connect externally",
        )
        command.add_argument(
            "--overrides",
            help="Optional project-relative resolve-overrides.json",
        )
        return command

    preflight = project_command("preflight", "Validate Resolve, inputs, media, and locks")
    preflight.set_defaults(func=cmd_resolve_preflight)

    prepare = project_command("prepare", "Write resolve-plan.v1.json and FCPXML")
    prepare.add_argument(
        "--out-dir",
        help="Output under <project>/resolve (default: content-addressed build directory)",
    )
    prepare.set_defaults(func=cmd_resolve_prepare)

    build = project_command("build", "Queue/import an immutable AUTO_BUILD timeline")
    build.set_defaults(func=cmd_resolve_build)

    render = project_command("render", "Queue a Resolve render from AUTO_BUILD")
    render.add_argument("--out", help="Optional output file path")
    render.add_argument(
        "--wait",
        action="store_true",
        help="Studio runner waits for completion (Free Console should normally return immediately)",
    )
    render.add_argument(
        "--wait-timeout",
        type=float,
        default=7200,
        help="Maximum wait seconds when --wait is selected",
    )
    render.set_defaults(func=cmd_resolve_render)

    handoff = project_command(
        "handoff", "Queue a source-inclusive DRA/DRP editor handoff"
    )
    handoff.add_argument("--out", required=True, help="Handoff destination directory")
    handoff.add_argument(
        "--include-proxy-media",
        action="store_true",
        help="Include proxies in addition to source media (off by default)",
    )
    handoff.set_defaults(func=cmd_resolve_handoff)

    status = actions.add_parser("status", help="Read durable Resolve queue/status state")
    status.add_argument("project_root")
    status.set_defaults(func=cmd_resolve_status)

    bundle = actions.add_parser(
        "bundle",
        help="Package an in-progress episode for another Windows/macOS host",
    )
    bundle.add_argument("project_root")
    bundle.add_argument("--out", required=True, help="New .zip path outside the episode")
    bundle.set_defaults(func=cmd_resolve_bundle)

    verify_bundle = actions.add_parser(
        "verify-bundle",
        help="Verify a portable pre-Resolve episode bundle",
    )
    verify_bundle.add_argument("package")
    verify_bundle.set_defaults(func=cmd_resolve_verify_bundle)

    restore_bundle = actions.add_parser(
        "restore-bundle",
        help="Restore a verified episode bundle into a new project directory",
    )
    restore_bundle.add_argument("package")
    restore_bundle.add_argument("--out", required=True)
    restore_bundle.set_defaults(func=cmd_resolve_restore_bundle)

    verify_handoff = actions.add_parser(
        "verify-handoff",
        help="Verify a portable handoff ZIP/directory and all checksums",
    )
    verify_handoff.add_argument("package")
    verify_handoff.set_defaults(func=cmd_resolve_verify_handoff)

    restore_handoff_parser = actions.add_parser(
        "restore-handoff",
        help="Safely extract/copy and verify a portable handoff",
    )
    restore_handoff_parser.add_argument("package")
    restore_handoff_parser.add_argument("--out", required=True)
    restore_handoff_parser.set_defaults(func=cmd_resolve_restore_handoff)

    install = actions.add_parser(
        "install-runner",
        help="Install the Resolve Workspace runner and reusable style assets",
    )
    install.add_argument(
        "--destination",
        help="Runner .py file or Scripts/Utility directory (default: current user)",
    )
    install.add_argument(
        "--support-root",
        help="Override the Resolve user support root for style assets",
    )
    install.add_argument(
        "--runner-only",
        action="store_true",
        help="Install only the menu runner, not Fusion templates/LUTs",
    )
    install.set_defaults(func=cmd_resolve_install_runner)
    return resolve


def cmd_legacy_resolve_render(args: argparse.Namespace) -> int:
    """Route `rabbithole render --backend resolve` through the Resolve queue."""

    timing = Path(args.timing_json).expanduser().resolve()
    project_root = timing.parent.parent
    if getattr(args, "start", None) is not None or getattr(args, "end", None) is not None:
        _print(
            {
                "ok": False,
                "error": {
                    "type": "UnsupportedResolveSegment",
                    "message": (
                        "Resolve backend does not yet build segment-only timelines; "
                        "use --backend ffmpeg for a bounded review render."
                    ),
                },
            }
        )
        return 1
    if getattr(args, "dry_run", False):
        result = preflight_project(project_root, mode="free")
        _print(result)
        return 0 if result["ok"] else 1
    options = (
        {"output_path": os.fspath(Path(args.out).expanduser().resolve())}
        if getattr(args, "out", None)
        else {}
    )
    return _guard(
        lambda: queue_project_action(
            project_root,
            "render",
            mode="free",
            options=options,
        )
    )


__all__ = ["add_resolve_parser", "cmd_legacy_resolve_render"]

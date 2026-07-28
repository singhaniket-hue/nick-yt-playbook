"""Local stdio MCP façade for the Resolve-first RabbitHole pipeline.

The tool functions are ordinary Python callables so they remain testable
without the optional MCP SDK. The server is created only by :func:`main`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .resolve_service import (
    preflight_project,
    prepare_project,
    project_status,
    queue_project_action,
)


def resolve_preflight(
    project_root: str,
    mode: str = "free",
    overrides_path: str | None = None,
) -> dict[str, Any]:
    """Validate inputs, media, tools, locks, and the selected Resolve mode."""

    return preflight_project(
        Path(project_root),
        mode=mode,
        overrides_path=Path(overrides_path) if overrides_path else None,
    )


def resolve_prepare(
    project_root: str,
    overrides_path: str | None = None,
) -> dict[str, Any]:
    """Compile the immutable Resolve plan and FCPXML without opening Resolve."""

    return prepare_project(
        Path(project_root),
        overrides_path=Path(overrides_path) if overrides_path else None,
    )


def resolve_build(
    project_root: str,
    mode: str = "free",
    overrides_path: str | None = None,
) -> dict[str, Any]:
    """Queue an immutable AUTO_BUILD timeline.

    Free returns ``awaiting_in_app_runner``; Studio external execution is used
    only when ``mode='studio'`` is explicitly supplied.
    """

    return queue_project_action(
        Path(project_root),
        "build",
        mode=mode,
        overrides_path=Path(overrides_path) if overrides_path else None,
    )


def resolve_render(
    project_root: str,
    mode: str = "free",
    output_path: str | None = None,
    wait: bool = False,
    wait_timeout_seconds: float = 7200,
    overrides_path: str | None = None,
) -> dict[str, Any]:
    """Queue a render from the immutable generated timeline."""

    options: dict[str, Any] = {}
    if output_path:
        options["output_path"] = str(Path(output_path).expanduser().resolve())
    if wait:
        options.update(
            {
                "wait": True,
                "wait_timeout_seconds": float(wait_timeout_seconds),
            }
        )
    return queue_project_action(
        Path(project_root),
        "render",
        mode=mode,
        overrides_path=Path(overrides_path) if overrides_path else None,
        options=options,
    )


def resolve_handoff(
    project_root: str,
    destination: str,
    mode: str = "free",
    include_proxy_media: bool = False,
    overrides_path: str | None = None,
) -> dict[str, Any]:
    """Queue a full-source DRA/DRP/checksum editor handoff."""

    return queue_project_action(
        Path(project_root),
        "handoff",
        mode=mode,
        overrides_path=Path(overrides_path) if overrides_path else None,
        options={
            "destination": str(Path(destination).expanduser().resolve()),
            "include_proxy_media": bool(include_proxy_media),
        },
    )


def resolve_status(project_root: str) -> dict[str, Any]:
    """Read durable queue state without connecting to Resolve."""

    return project_status(Path(project_root))


def create_server() -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "The optional MCP SDK is not installed. "
            "Install this project with `pip install -e \".[mcp]\"`."
        ) from exc

    server = FastMCP(
        "rabbithole-resolve",
        instructions=(
            "Prepare and queue portable RabbitHole edits for DaVinci Resolve. "
            "Resolve Free jobs must be completed by the in-app runner."
        ),
    )
    server.tool()(resolve_preflight)
    server.tool()(resolve_prepare)
    server.tool()(resolve_build)
    server.tool()(resolve_render)
    server.tool()(resolve_handoff)
    server.tool()(resolve_status)
    return server


def main() -> int:
    server = create_server()
    server.run(transport="stdio")
    return 0


__all__ = [
    "create_server",
    "main",
    "resolve_build",
    "resolve_handoff",
    "resolve_preflight",
    "resolve_prepare",
    "resolve_render",
    "resolve_status",
]

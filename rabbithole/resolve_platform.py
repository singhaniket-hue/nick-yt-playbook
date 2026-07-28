"""Cross-platform discovery for DaVinci Resolve and its scripting bridge.

The application, scripting module, library, and per-user support paths live in
different places on Windows, macOS, and Linux.  Keep that knowledge here so a
project prepared on one machine can be rebuilt on another without committing
machine-specific paths.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import platform
import shutil
import sys
from typing import Any, Mapping, MutableMapping


MINIMUM_HOST_PYTHON = (3, 11)
RESOLVE_PATH_ENV = "RABBITHOLE_RESOLVE_PATH"


def host_platform(value: str | None = None) -> str:
    """Normalize a Python platform value to ``windows``, ``macos``, or ``linux``."""

    selected = (value or sys.platform).lower()
    if selected.startswith("win"):
        return "windows"
    if selected == "darwin":
        return "macos"
    if selected.startswith("linux"):
        return "linux"
    return selected


def _home(value: os.PathLike[str] | str | None = None) -> Path:
    return Path(value).expanduser().resolve(strict=False) if value else Path.home()


def _dedupe(paths: list[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        normalized = os.path.normcase(os.fspath(path))
        if normalized not in seen:
            seen.add(normalized)
            result.append(path)
    return tuple(result)


def resolve_application_candidates(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: os.PathLike[str] | str | None = None,
) -> tuple[Path, ...]:
    """Return ordered Resolve application candidates without launching it."""

    selected = host_platform(platform_name)
    values = environ if environ is not None else os.environ
    user_home = _home(home)
    candidates: list[Path] = []
    override = values.get(RESOLVE_PATH_ENV)
    if override:
        override_path = Path(override).expanduser()
        if selected == "macos" and override_path.suffix.lower() == ".app":
            override_path = (
                override_path / "Contents" / "MacOS" / "Resolve"
            )
        candidates.append(override_path)

    if selected == "windows":
        for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            base = values.get(variable)
            if base:
                candidates.append(
                    Path(base)
                    / "Blackmagic Design"
                    / "DaVinci Resolve"
                    / "Resolve.exe"
                )
    elif selected == "macos":
        relative = (
            Path("DaVinci Resolve")
            / "DaVinci Resolve.app"
            / "Contents"
            / "MacOS"
            / "Resolve"
        )
        candidates.extend(
            (
                Path("/Applications") / relative,
                user_home / "Applications" / relative,
            )
        )
    elif selected == "linux":
        candidates.extend(
            (
                Path("/opt/resolve/bin/resolve"),
                Path("/home/resolve/bin/resolve"),
                Path("/usr/bin/resolve"),
            )
        )
    return _dedupe(candidates)


def find_resolve_application(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: os.PathLike[str] | str | None = None,
) -> Path | None:
    """Locate an installed Resolve application or executable."""

    discovered = shutil.which("Resolve") or shutil.which("resolve")
    candidates = list(
        resolve_application_candidates(
            platform_name=platform_name,
            environ=environ,
            home=home,
        )
    )
    if discovered:
        candidates.insert(0, Path(discovered))
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve(strict=False)
    return None


def user_resolve_support_root(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: os.PathLike[str] | str | None = None,
) -> Path:
    """Return the current user's Resolve support root."""

    selected = host_platform(platform_name)
    values = environ if environ is not None else os.environ
    user_home = _home(home)
    if selected == "windows":
        appdata = values.get("APPDATA")
        base = Path(appdata) if appdata else user_home / "AppData" / "Roaming"
        return (
            base
            / "Blackmagic Design"
            / "DaVinci Resolve"
            / "Support"
        )
    if selected == "macos":
        return (
            user_home
            / "Library"
            / "Application Support"
            / "Blackmagic Design"
            / "DaVinci Resolve"
        )
    return user_home / ".local" / "share" / "DaVinciResolve"


def resolve_runner_install_directory(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: os.PathLike[str] | str | None = None,
) -> Path:
    """Return Resolve's per-user ``Workspace > Scripts > Utility`` directory."""

    return (
        user_resolve_support_root(
            platform_name=platform_name,
            environ=environ,
            home=home,
        )
        / "Fusion"
        / "Scripts"
        / "Utility"
    )


def resolve_scripting_paths(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: os.PathLike[str] | str | None = None,
) -> dict[str, Path]:
    """Return the official Resolve scripting API/module/library locations."""

    selected = host_platform(platform_name)
    values = environ if environ is not None else os.environ
    user_home = _home(home)
    if selected == "windows":
        program_data = Path(
            values.get("PROGRAMDATA", r"C:\ProgramData")
        )
        program_files = Path(
            values.get("ProgramFiles", r"C:\Program Files")
        )
        api_root = (
            program_data
            / "Blackmagic Design"
            / "DaVinci Resolve"
            / "Support"
            / "Developer"
            / "Scripting"
        )
        library = (
            program_files
            / "Blackmagic Design"
            / "DaVinci Resolve"
            / "fusionscript.dll"
        )
    elif selected == "macos":
        api_root = (
            Path("/Library")
            / "Application Support"
            / "Blackmagic Design"
            / "DaVinci Resolve"
            / "Developer"
            / "Scripting"
        )
        application_value = values.get(RESOLVE_PATH_ENV)
        if application_value:
            application = Path(application_value).expanduser()
            bundle = (
                application
                if application.suffix.lower() == ".app"
                else application.parents[2]
            )
        else:
            bundle = (
                Path("/Applications")
                / "DaVinci Resolve"
                / "DaVinci Resolve.app"
            )
        library = (
            bundle
            / "Contents"
            / "Libraries"
            / "Fusion"
            / "fusionscript.so"
        )
    else:
        api_root = Path("/opt/resolve/Developer/Scripting")
        if not api_root.exists() and Path("/home/resolve/Developer/Scripting").exists():
            api_root = Path("/home/resolve/Developer/Scripting")
        library = (
            Path("/opt/resolve/libs/Fusion/fusionscript.so")
            if api_root.parts[:3] != ("/", "home", "resolve")
            else Path("/home/resolve/libs/Fusion/fusionscript.so")
        )

    explicit_api = values.get("RESOLVE_SCRIPT_API")
    explicit_library = values.get("RESOLVE_SCRIPT_LIB")
    if explicit_api:
        api_root = Path(explicit_api).expanduser()
    if explicit_library:
        library = Path(explicit_library).expanduser()
    modules = api_root / "Modules"
    return {
        "api_root": api_root,
        "modules": modules,
        "module": modules / "DaVinciResolveScript.py",
        "library": library,
    }


def resolve_scripting_module_available(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: os.PathLike[str] | str | None = None,
) -> bool:
    """Report whether the Studio bridge module is importable or installed."""

    try:
        if importlib.util.find_spec("DaVinciResolveScript") is not None:
            return True
    except (ImportError, ValueError):
        pass
    return resolve_scripting_paths(
        platform_name=platform_name,
        environ=environ,
        home=home,
    )["module"].is_file()


def configure_resolve_scripting_environment(
    *,
    environ: MutableMapping[str, str] | None = None,
    module_search_path: list[str] | None = None,
) -> dict[str, Any]:
    """Configure this process for the Studio bridge using installed defaults.

    Existing user-provided environment values remain authoritative.  This does
    not start Resolve or change application preferences.
    """

    values = environ if environ is not None else os.environ
    search_path = module_search_path if module_search_path is not None else sys.path
    paths = resolve_scripting_paths(environ=values)
    if paths["api_root"].exists():
        values.setdefault("RESOLVE_SCRIPT_API", os.fspath(paths["api_root"]))
    if paths["library"].exists():
        values.setdefault("RESOLVE_SCRIPT_LIB", os.fspath(paths["library"]))
    if paths["modules"].is_dir():
        module_value = os.fspath(paths["modules"])
        if module_value not in search_path:
            search_path.insert(0, module_value)
    return {
        "api_root": os.fspath(paths["api_root"]),
        "module": os.fspath(paths["module"]),
        "library": os.fspath(paths["library"]),
        "module_available": resolve_scripting_module_available(environ=values),
    }


def host_report() -> dict[str, Any]:
    """Return JSON-safe host information used by the portability doctor."""

    application = find_resolve_application()
    scripting = resolve_scripting_paths()
    return {
        "platform": host_platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "python_ok": sys.version_info[:2] >= MINIMUM_HOST_PYTHON,
        "resolve_application": os.fspath(application) if application else None,
        "resolve_installed": application is not None,
        "resolve_support_root": os.fspath(user_resolve_support_root()),
        "resolve_runner_directory": os.fspath(resolve_runner_install_directory()),
        "resolve_script_api": os.fspath(scripting["api_root"]),
        "resolve_script_library": os.fspath(scripting["library"]),
        "resolve_script_module": os.fspath(scripting["module"]),
        "resolve_script_module_available": resolve_scripting_module_available(),
    }


__all__ = [
    "MINIMUM_HOST_PYTHON",
    "RESOLVE_PATH_ENV",
    "configure_resolve_scripting_environment",
    "find_resolve_application",
    "host_platform",
    "host_report",
    "resolve_application_candidates",
    "resolve_runner_install_directory",
    "resolve_scripting_module_available",
    "resolve_scripting_paths",
    "user_resolve_support_root",
]

from __future__ import annotations

import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from rabbithole.fcpxml import build_fcpxml
import rabbithole.resolve_platform as resolve_platform
import rabbithole.resolve_service as resolve_service
import rabbithole.sources.capture as capture
from rabbithole.resolve_manifest import compile_resolve_plan
from test_resolve_manifest import _project


def _portable(path: Path) -> str:
    return os.fspath(path).replace("\\", "/")


def _host_report(*, bridge_available: bool = True) -> dict[str, object]:
    return {
        "platform": "macos",
        "system": "Darwin",
        "release": "25.0",
        "machine": "arm64",
        "python": "3.12.8",
        "python_executable": "/opt/homebrew/bin/python3",
        "python_ok": True,
        "resolve_application": (
            "/Applications/DaVinci Resolve/DaVinci Resolve.app/"
            "Contents/MacOS/Resolve"
        ),
        "resolve_installed": True,
        "resolve_support_root": (
            "/Users/editor/Library/Application Support/Blackmagic Design/"
            "DaVinci Resolve"
        ),
        "resolve_runner_directory": (
            "/Users/editor/Library/Application Support/Blackmagic Design/"
            "DaVinci Resolve/Fusion/Scripts/Utility"
        ),
        "resolve_script_api": (
            "/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
            "Developer/Scripting"
        ),
        "resolve_script_library": (
            "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/"
            "Libraries/Fusion/fusionscript.so"
        ),
        "resolve_script_module": (
            "/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
            "Developer/Scripting/Modules/DaVinciResolveScript.py"
        ),
        "resolve_script_module_available": bridge_available,
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("win32", "windows"),
        ("Windows", "windows"),
        ("darwin", "macos"),
        ("linux", "linux"),
        ("linux2", "linux"),
    ],
)
def test_host_platform_normalizes_python_platform_names(value, expected):
    assert resolve_platform.host_platform(value) == expected


def test_windows_resolve_paths_honor_machine_and_user_environment(tmp_path):
    custom = tmp_path / "portable-tools" / "Resolve.exe"
    custom.parent.mkdir(parents=True)
    custom.write_bytes(b"mock executable")
    environment = {
        resolve_platform.RESOLVE_PATH_ENV: os.fspath(custom),
        "ProgramFiles": "C:/Program Files",
        "ProgramW6432": "D:/Program Files",
        "ProgramFiles(x86)": "C:/Program Files (x86)",
        "PROGRAMDATA": "C:/ProgramData",
        "APPDATA": "C:/Users/editor/AppData/Roaming",
    }

    candidates = resolve_platform.resolve_application_candidates(
        platform_name="win32",
        environ=environment,
        home=tmp_path / "home",
    )
    support = resolve_platform.user_resolve_support_root(
        platform_name="win32",
        environ=environment,
        home=tmp_path / "home",
    )
    runner = resolve_platform.resolve_runner_install_directory(
        platform_name="win32",
        environ=environment,
        home=tmp_path / "home",
    )
    scripting = resolve_platform.resolve_scripting_paths(
        platform_name="win32",
        environ=environment,
        home=tmp_path / "home",
    )

    assert candidates[0] == custom
    assert _portable(candidates[1]).endswith(
        "Program Files/Blackmagic Design/DaVinci Resolve/Resolve.exe"
    )
    assert _portable(support) == (
        "C:/Users/editor/AppData/Roaming/Blackmagic Design/"
        "DaVinci Resolve/Support"
    )
    assert runner == support / "Fusion" / "Scripts" / "Utility"
    assert _portable(scripting["api_root"]) == (
        "C:/ProgramData/Blackmagic Design/DaVinci Resolve/Support/"
        "Developer/Scripting"
    )
    assert _portable(scripting["library"]) == (
        "C:/Program Files/Blackmagic Design/DaVinci Resolve/fusionscript.dll"
    )


def test_macos_resolve_bundle_support_and_scripting_paths_are_discoverable(
    tmp_path, monkeypatch
):
    bundle = tmp_path / "Apps" / "DaVinci Resolve.app"
    executable = bundle / "Contents" / "MacOS" / "Resolve"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"mock executable")
    environment = {resolve_platform.RESOLVE_PATH_ENV: os.fspath(bundle)}
    home = tmp_path / "Users" / "editor"
    monkeypatch.setattr(resolve_platform.shutil, "which", lambda _name: None)

    candidates = resolve_platform.resolve_application_candidates(
        platform_name="darwin", environ=environment, home=home
    )
    found = resolve_platform.find_resolve_application(
        platform_name="darwin", environ=environment, home=home
    )
    support = resolve_platform.user_resolve_support_root(
        platform_name="darwin", environ=environment, home=home
    )
    scripting = resolve_platform.resolve_scripting_paths(
        platform_name="darwin", environ=environment, home=home
    )

    assert candidates[0] == executable
    assert found == executable.resolve()
    assert support == (
        home.resolve()
        / "Library"
        / "Application Support"
        / "Blackmagic Design"
        / "DaVinci Resolve"
    )
    assert scripting["api_root"].as_posix().endswith(
        "/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
        "Developer/Scripting"
    )
    assert scripting["library"] == (
        bundle
        / "Contents"
        / "Libraries"
        / "Fusion"
        / "fusionscript.so"
    )


def test_scripting_path_overrides_are_authoritative_on_every_host(tmp_path):
    api_root = tmp_path / "resolve-api"
    library = tmp_path / "resolve-libs" / "fusionscript.custom"
    environment = {
        "RESOLVE_SCRIPT_API": os.fspath(api_root),
        "RESOLVE_SCRIPT_LIB": os.fspath(library),
    }

    for platform_name in ("win32", "darwin"):
        paths = resolve_platform.resolve_scripting_paths(
            platform_name=platform_name,
            environ=environment,
            home=tmp_path / platform_name,
        )
        assert paths["api_root"] == api_root
        assert paths["modules"] == api_root / "Modules"
        assert paths["module"] == api_root / "Modules" / "DaVinciResolveScript.py"
        assert paths["library"] == library


def test_studio_environment_configuration_does_not_launch_resolve(
    tmp_path, monkeypatch
):
    api_root = tmp_path / "resolve-api"
    modules = api_root / "Modules"
    modules.mkdir(parents=True)
    (modules / "DaVinciResolveScript.py").write_text(
        "# mocked Resolve bridge\n", encoding="utf-8"
    )
    library = tmp_path / "resolve-libs" / "fusionscript.so"
    library.parent.mkdir()
    library.write_bytes(b"mock library")
    environment = {
        "RESOLVE_SCRIPT_API": os.fspath(api_root),
        "RESOLVE_SCRIPT_LIB": os.fspath(library),
    }
    search_path: list[str] = []

    monkeypatch.setattr(
        resolve_platform.importlib.util, "find_spec", lambda _name: None
    )
    monkeypatch.setattr(
        resolve_platform,
        "find_resolve_application",
        lambda **_kwargs: pytest.fail("environment setup must not start or find Resolve"),
    )

    report = resolve_platform.configure_resolve_scripting_environment(
        environ=environment,
        module_search_path=search_path,
    )

    assert environment == {
        "RESOLVE_SCRIPT_API": os.fspath(api_root),
        "RESOLVE_SCRIPT_LIB": os.fspath(library),
    }
    assert search_path == [os.fspath(modules)]
    assert report["module"] == os.fspath(modules / "DaVinciResolveScript.py")
    assert report["library"] == os.fspath(library)
    assert report["module_available"] is True


def test_free_doctor_is_read_only_and_manual_console_gate_is_nonfatal(
    monkeypatch,
):
    monkeypatch.setattr(
        resolve_service, "host_report", lambda: _host_report(bridge_available=False)
    )
    monkeypatch.setattr(
        resolve_service.shutil, "which", lambda name: f"/mock/bin/{name}"
    )
    monkeypatch.setattr(
        capture,
        "find_browser",
        lambda: Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    )

    report = resolve_service.portability_report(mode="free")
    checks = {item["name"]: item for item in report["checks"]}

    assert report["ok"] is True
    assert report["safe"] == {
        "resolve_started": False,
        "render_queue_touched": False,
        "project_mutated": False,
    }
    assert checks["free_console_python"]["ok"] is False
    assert checks["free_console_python"]["severity"] == "warning"
    assert "studio_external_bridge" not in checks


def test_studio_doctor_requires_the_external_bridge_but_remains_read_only(
    monkeypatch,
):
    monkeypatch.setattr(
        resolve_service, "host_report", lambda: _host_report(bridge_available=False)
    )
    monkeypatch.setattr(
        resolve_service.shutil, "which", lambda name: f"/mock/bin/{name}"
    )
    monkeypatch.setattr(capture, "find_browser", lambda: None)

    report = resolve_service.portability_report(mode="studio")
    checks = {item["name"]: item for item in report["checks"]}

    assert report["ok"] is False
    assert checks["studio_external_bridge"]["ok"] is False
    assert checks["studio_external_bridge"]["severity"] == "error"
    assert report["safe"]["resolve_started"] is False
    assert report["safe"]["render_queue_touched"] is False
    assert report["safe"]["project_mutated"] is False


def test_macos_browser_bundle_and_environment_override_are_discovered(
    tmp_path, monkeypatch
):
    chrome = (
        tmp_path
        / "Applications"
        / "Google Chrome.app"
        / "Contents"
        / "MacOS"
        / "Google Chrome"
    )
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b"mock browser")
    fallback = tmp_path / "chromium"
    fallback.write_bytes(b"mock fallback")
    monkeypatch.setenv(capture.BROWSER_PATH_ENV, os.fspath(chrome))
    monkeypatch.setattr(capture.shutil, "which", lambda _name: None)

    assert capture.find_browser(candidates=(os.fspath(fallback),)) == chrome


def test_default_browser_discovery_accepts_a_macos_application_bundle(
    tmp_path, monkeypatch
):
    assert (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        in capture.BROWSER_CANDIDATES
    )
    chrome = (
        tmp_path
        / "Applications"
        / "Google Chrome.app"
        / "Contents"
        / "MacOS"
        / "Google Chrome"
    )
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b"mock browser")
    monkeypatch.delenv(capture.BROWSER_PATH_ENV, raising=False)
    monkeypatch.setattr(capture, "BROWSER_CANDIDATES", (os.fspath(chrome),))
    monkeypatch.setattr(capture.shutil, "which", lambda _name: None)

    assert capture.find_browser() == chrome


def test_foreign_windows_and_macos_media_paths_survive_plan_and_fcpxml(
    tmp_path,
):
    root = _project(tmp_path)
    provenance_path = root / "provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance[0]["local_path"] = r"D:\Media Library\plate.mp4"
    provenance[1]["local_path"] = "/Volumes/Media Library/capture.png"
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

    plan = compile_resolve_plan(root)
    assets = {item["asset_id"]: item for item in plan["provenance"]}

    assert assets["plate-s001"]["local_path"] == "D:/Media Library/plate.mp4"
    assert assets["plate-s001"]["path_kind"] == "external-absolute"
    assert assets["capture-s002"]["local_path"] == (
        "/Volumes/Media Library/capture.png"
    )
    assert assets["capture-s002"]["path_kind"] == "external-absolute"
    external_reviews = {
        item["asset_id"]
        for item in plan["review_flags"]
        if item["kind"] == "external_media_path"
    }
    assert external_reviews == {"plate-s001", "capture-s002"}

    document = ET.fromstring(build_fcpxml(plan, project_root=root))
    media_uris = {
        item.attrib["suggestedFilename"]: item.attrib["src"]
        for item in document.findall("./resources/asset/media-rep")
    }
    assert media_uris["plate.mp4"] == "file:///D:/Media%20Library/plate.mp4"
    assert media_uris["capture.png"] == (
        "file:///Volumes/Media%20Library/capture.png"
    )

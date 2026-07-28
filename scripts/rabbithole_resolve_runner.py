"""Resolve Workspace/Console entry point for the RabbitHole durable queue.

This file intentionally bootstraps before importing ``rabbithole``.  Resolve's
embedded Python does not necessarily share the terminal virtual environment,
so the user-local pointer written by ``enqueue_job`` also records the checkout
that owns the runner package.

Keep this bootstrap parseable by Python 3.6. Resolve's installed scripting
documentation still lists Python 3.6 even though RabbitHole itself requires
Python 3.11. An older Console therefore receives a clear, non-mutating error
before the project package is imported or a queued job is claimed.
"""

import json
import os
from pathlib import Path
import sys


MINIMUM_RUNNER_PYTHON = (3, 11)


def _require_supported_python():
    if sys.version_info[:2] < MINIMUM_RUNNER_PYTHON:
        current = ".".join(str(value) for value in sys.version_info[:3])
        required = ".".join(str(value) for value in MINIMUM_RUNNER_PYTHON)
        raise RuntimeError(
            "RabbitHole's in-app Resolve runner requires Python "
            + required
            + " or newer; this Console is Python "
            + current
            + ". The queued job was not claimed. Use the generated FCPXML "
            "manually, keep the FFmpeg backend, or run the Studio external "
            "bridge from a Python 3.11+ environment."
        )


def _pointer_path():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "RabbitHole" / "resolve-runner.json"
        return (
            Path.home()
            / "AppData"
            / "Local"
            / "RabbitHole"
            / "resolve-runner.json"
        )
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "RabbitHole"
            / "resolve-runner.json"
        )
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "rabbithole" / "resolve-runner.json"
    return Path.home() / ".local" / "state" / "rabbithole" / "resolve-runner.json"


def _read_pointer():
    path = _pointer_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            "RabbitHole has no queued-project pointer. Enqueue a Resolve job "
            f"first; expected pointer: {path}"
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read RabbitHole runner pointer {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"RabbitHole runner pointer is not a JSON object: {path}")
    return value


def _bootstrap_source(pointer):
    # A console loader points at the exact checkout's script, so prefer its
    # sibling package.  An installed Workspace copy falls back to the pointer.
    candidates = [Path(__file__).resolve().parents[1]]
    if pointer is not None and isinstance(pointer.get("source_root"), str):
        candidates.append(Path(pointer["source_root"]))
    for candidate in candidates:
        if (candidate / "rabbithole" / "resolve_runner.py").is_file():
            text = os.fspath(candidate.resolve())
            if text not in sys.path:
                sys.path.insert(0, text)
            return


def main():
    _require_supported_python()
    explicit_root = globals().get("PROJECT_ROOT")
    explicit_job = globals().get("JOB_ID")
    pointer = None
    if explicit_root is None:
        pointer = _read_pointer()
        explicit_root = pointer.get("project_root")
        explicit_job = explicit_job or pointer.get("job_id")
    else:
        # Still use the pointer for source bootstrapping when available, but an
        # explicit one-line loader remains authoritative for root/job selection.
        try:
            pointer = _read_pointer()
        except RuntimeError:
            pointer = None

    _bootstrap_source(pointer)
    from rabbithole.resolve_runner import run_pending_jobs

    results = run_pending_jobs(
        resolve=globals().get("resolve"),
        app=globals().get("app"),
        project_root=explicit_root,
        job_id=explicit_job,
    )
    print(json.dumps(results, indent=2, sort_keys=True))
    return results


if __name__ == "__main__" or globals().get("RUN_RABBITHOLE_RESOLVE_RUNNER"):
    RESULTS = main()

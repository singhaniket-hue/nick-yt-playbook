"""Primary-artifact retrieval, gated on the claims ledger.

Tier-1 footage is the actual thing being investigated — a video, a channel, a
capture of a real person. It may not be retrieved unless the claims ledger already
carries an entry citing that URL as a source. The gate is a refusal rather than a
warning: sourcing discipline that can be skipped under deadline is not discipline.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

# argv -> (returncode, stdout, stderr)
Runner = Callable[[list[str]], tuple[int, bytes, bytes]]

# path -> True if the file carries a decodable video stream
Prober = Callable[[Path], bool]

YTDLP_PROCESS_TIMEOUT_SECONDS = 300
MEDIA_PROBE_TIMEOUT_SECONDS = 20
PROCESS_TERMINATION_GRACE_SECONDS = 3.0
PROCESS_OUTPUT_DRAIN_TIMEOUT_SECONDS = 3.0
WINDOWS_TASKKILL_TIMEOUT_SECONDS = 10.0

_WINDOWS_CREATE_NEW_PROCESS_GROUP = getattr(
    subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
)
_WINDOWS_CREATE_NO_WINDOW = getattr(
    subprocess, "CREATE_NO_WINDOW", 0x08000000
)
_POSIX_SIGTERM = getattr(signal, "SIGTERM", 15)
_POSIX_SIGKILL = getattr(signal, "SIGKILL", 9)

# Resolve Free on Windows and macOS reliably accepts this delivery profile.
# Pinning both the container and codecs also prevents yt-dlp from silently
# changing an `out.mp4` request into `out.mp4.webm` when its unconstrained
# "best" choice happens to be AV1/Opus WebM.
PORTABLE_VIDEO_FORMAT = (
    "bv[ext=mp4][vcodec^=avc1]+ba[ext=m4a][acodec^=mp4a]/"
    "b[ext=mp4][vcodec^=avc1][acodec^=mp4a]/"
    "bv[ext=mp4][vcodec^=avc1]"
)


def media_decodes(path: Path) -> bool:
    """Whether ffprobe can open *path* and find a visual stream."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True,
            timeout=MEDIA_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and b"video" in result.stdout


# Backward-compatible private name used by older integrations and focused tests.
_default_prober = media_decodes


def normalize_url(url: str) -> str:
    """Canonical form for ledger comparison.

    Strips surrounding whitespace and exactly one trailing slash. Deliberately
    does NOT lowercase the path or strip query parameters: for YouTube, `?v=`
    IS the video's identity, and two URLs that differ only in query string (or
    in path casing) are two different videos. A fuzzy match here -- treating
    near-identical URLs as the same -- would undermine the ledger gate by
    letting an uncited URL slip through on the strength of a cited neighbour.
    """
    stripped = url.strip()
    if stripped.endswith("/"):
        stripped = stripped[:-1]
    return stripped


def claim_citing(url: str, claims: list[dict]) -> dict | None:
    """The first claim whose sources cite this URL, or None.

    Comparison is exact equality of normalized URLs -- not substring or prefix
    matching. A cited `.../watch?v=abc123` must not match a requested
    `.../watch?v=abc`, or the ledger gate would be trivially bypassable by
    requesting a truncated URL that happens to prefix a legitimately cited one.
    """
    target = normalize_url(url)
    for claim in claims:
        sources = claim.get("sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, str):
                continue
            if normalize_url(source) == target:
                return claim
    return None


def _iso_utc(ts: datetime) -> str:
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_windows() -> bool:
    return os.name == "nt"


def _process_group_popen(
    argv: list[str],
) -> subprocess.Popen[bytes]:
    """Launch one attempt in a process group owned by this runner."""

    options: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if _is_windows():
        options["creationflags"] = (
            _WINDOWS_CREATE_NEW_PROCESS_GROUP | _WINDOWS_CREATE_NO_WINDOW
        )
    else:
        # A new session also makes the child the leader of a new process group,
        # so a timeout can terminate yt-dlp and any ffmpeg descendants together.
        options["start_new_session"] = True
    return subprocess.Popen(argv, **options)


def _posix_process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reap_direct_process(process: subprocess.Popen[bytes]) -> None:
    """Bounded fallback for the direct child after group/tree termination."""

    try:
        process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"timed-out yt-dlp process {process.pid} could not be reaped"
        ) from exc


def _terminate_posix_process_tree(process: subprocess.Popen[bytes]) -> None:
    process_group_id = process.pid
    try:
        os.killpg(process_group_id, _POSIX_SIGTERM)
    except ProcessLookupError:
        _reap_direct_process(process)
        return
    except OSError as exc:
        try:
            process.terminate()
        except (OSError, ProcessLookupError):
            pass
        _reap_direct_process(process)
        raise RuntimeError(
            f"could not signal yt-dlp process group {process_group_id}: {exc}"
        ) from exc

    deadline = time.monotonic() + PROCESS_TERMINATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        # Reap the group leader when it exits so its zombie does not make a
        # now-empty process group look alive for the entire grace period.
        process.poll()
        if not _posix_process_group_exists(process_group_id):
            break
        time.sleep(0.05)
    process.poll()
    if _posix_process_group_exists(process_group_id):
        try:
            os.killpg(process_group_id, _POSIX_SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            _reap_direct_process(process)
            raise RuntimeError(
                f"could not kill yt-dlp process group {process_group_id}: {exc}"
            ) from exc
    _reap_direct_process(process)


def _terminate_windows_process_tree(process: subprocess.Popen[bytes]) -> None:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    taskkill = Path(system_root) / "System32" / "taskkill.exe"
    taskkill_error: str | None = None
    try:
        result = subprocess.run(
            [
                os.fspath(taskkill),
                "/PID",
                str(process.pid),
                "/T",
                "/F",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=WINDOWS_TASKKILL_TIMEOUT_SECONDS,
            creationflags=_WINDOWS_CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            detail = (result.stderr or b"").decode("utf-8", errors="replace")[-300:]
            taskkill_error = (
                f"taskkill exited {result.returncode}"
                + (f": {detail}" if detail else "")
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        taskkill_error = str(exc)

    _reap_direct_process(process)
    if taskkill_error:
        raise RuntimeError(
            f"could not terminate yt-dlp process tree {process.pid}: "
            f"{taskkill_error}"
        )


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Terminate yt-dlp and converter descendants created by this attempt."""

    if _is_windows():
        _terminate_windows_process_tree(process)
    else:
        _terminate_posix_process_tree(process)


def _output_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    return value if isinstance(value, bytes) else value.encode("utf-8", errors="replace")


def _drain_terminated_process(
    process: subprocess.Popen[bytes],
    *,
    stdout: bytes | str | None,
    stderr: bytes | str | None,
) -> tuple[bytes, bytes]:
    """Collect bounded output without waiting forever on inherited pipe handles."""

    fallback_stdout = _output_bytes(stdout)
    fallback_stderr = _output_bytes(stderr)
    try:
        drained_stdout, drained_stderr = process.communicate(
            timeout=PROCESS_OUTPUT_DRAIN_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as exc:
        drained_stdout = exc.stdout
        drained_stderr = exc.stderr
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
    return (
        _output_bytes(drained_stdout) or fallback_stdout,
        _output_bytes(drained_stderr) or fallback_stderr,
    )


def _default_runner(argv: list[str]) -> tuple[int, bytes, bytes]:
    process = _process_group_popen(argv)
    try:
        stdout, stderr = process.communicate(
            timeout=YTDLP_PROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        termination_error = ""
        try:
            _terminate_process_tree(process)
        except Exception as cleanup_exc:
            termination_error = (
                f"; process-tree termination reported: {cleanup_exc}"
            )
        stdout, stderr = _drain_terminated_process(
            process,
            stdout=exc.stdout,
            stderr=exc.stderr,
        )
        detail = (
            f"\nyt-dlp exceeded the {YTDLP_PROCESS_TIMEOUT_SECONDS}s process "
            f"timeout; its isolated process tree was terminated{termination_error}"
        ).encode("utf-8")
        return 124, stdout, stderr + detail
    return int(process.returncode or 0), stdout, stderr


def _asset_id(claim_id: str, url: str) -> str:
    """Derive a stable asset_id from the authorising claim and the URL.

    `claim-id + short hash of the normalized URL` rather than the raw URL: the
    URL can contain characters (`?`, `&`, `:`) that are awkward in an id used
    elsewhere as a filename-ish token, and a short hash keeps the id compact
    while still being deterministic (same claim + same URL always yields the
    same asset_id, so re-running fetch_primary on the same input is detectable
    as a duplicate by `provenance.add_record`).
    """
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"primary-{claim_id}-{digest}"


def fetch_primary(
    url: str,
    out_path: Path,
    claims: list[dict],
    runner: Runner | None = None,
    now: datetime | None = None,
    prober: Prober | None = None,
) -> dict:
    """Retrieve a primary artifact. Raises if the ledger does not cite the URL.

    Also raises if what came back is not decodable video: yt-dlp's generic
    extractor returns 0 for pages and documents, so the exit code alone cannot
    distinguish a retrieved clip from a retrieved PDF.
    """
    normalized = normalize_url(url)
    claim = claim_citing(normalized, claims)
    if claim is None:
        raise RuntimeError(
            f"Refusing to retrieve {normalized!r}: no claim in the ledger cites "
            f"this URL ({len(claims)} claims checked). Add a claims.json entry "
            f"citing this URL as a source before retrieving it."
        )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    active_runner = runner if runner is not None else _default_runner
    active_prober = prober if prober is not None else _default_prober

    # yt-dlp may create several format fragments (`.f137.mp4`, `.m4a`, `.part`)
    # before muxing the requested output. Keep the whole attempt in a disposable
    # sibling directory so a timeout or failed merge cannot contaminate the
    # deterministic final path. Only a present, non-empty, decodable result is
    # atomically promoted.
    with tempfile.TemporaryDirectory(
        dir=out_path.parent,
        prefix=".rabbithole-ytdlp-",
    ) as attempt_dir:
        attempt_path = Path(attempt_dir) / "download.mp4"
        argv = [
            "yt-dlp",
            "--no-playlist",
            "--socket-timeout",
            "30",
            "--retries",
            "3",
            "--format",
            PORTABLE_VIDEO_FORMAT,
            "--merge-output-format",
            "mp4",
            "--remux-video",
            "mp4",
            "-o",
            str(attempt_path),
            normalized,
        ]
        returncode, _stdout, stderr = active_runner(argv)
        if returncode != 0:
            tail = (
                stderr[-800:].decode("utf-8", errors="replace")
                if isinstance(stderr, bytes)
                else str(stderr)[-800:]
            )
            raise RuntimeError(
                f"yt-dlp failed for {normalized!r} (exit {returncode}): {tail}"
            )

        if not attempt_path.is_file() or attempt_path.stat().st_size <= 0:
            raise RuntimeError(
                f"yt-dlp exited 0 for {normalized!r} but wrote no non-empty "
                "MP4 output. Not recorded."
            )

        # A zero exit is not proof of video. yt-dlp falls back to a generic
        # extractor on URLs it has no site handler for, and that extractor
        # happily saves whatever the server returned under the requested
        # filename. Two government-PDF URLs in this project produced files
        # beginning `%PDF-1.7` named `.mp4`; validate before promotion.
        if not active_prober(attempt_path):
            raise RuntimeError(
                f"Retrieved {normalized!r} but it carries no decodable video "
                f"stream (yt-dlp exited 0, most likely via its generic extractor "
                f"on a page or document rather than a video). Not recorded."
            )

        os.replace(attempt_path, out_path)

    claim_id = str(claim.get("claim_id", ""))
    ts = now if now is not None else datetime.now(timezone.utc)
    provider = urlparse(normalized).netloc or "unknown"

    return {
        "asset_id": _asset_id(claim_id, normalized),
        "tier": "primary",
        "provider": provider,
        "original_url": normalized,
        "license": "commentary-use",
        "retrieved_at": _iso_utc(ts),
        "local_path": str(out_path),
        "notes": f"Authorised by claim {claim_id!r}",
    }

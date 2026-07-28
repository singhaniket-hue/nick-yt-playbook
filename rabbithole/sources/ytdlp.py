"""Primary-artifact retrieval, gated on the claims ledger.

Tier-1 footage is the actual thing being investigated — a video, a channel, a
capture of a real person. It may not be retrieved unless the claims ledger already
carries an entry citing that URL as a source. The gate is a refusal rather than a
warning: sourcing discipline that can be skipped under deadline is not discipline.
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

# argv -> (returncode, stdout, stderr)
Runner = Callable[[list[str]], tuple[int, bytes, bytes]]

# path -> True if the file carries a decodable video stream
Prober = Callable[[Path], bool]


def _default_prober(path: Path) -> bool:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
        capture_output=True,
    )
    return result.returncode == 0 and b"video" in result.stdout


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


def _default_runner(argv: list[str]) -> tuple[int, bytes, bytes]:
    result = subprocess.run(argv, capture_output=True)  # pragma: no cover
    return result.returncode, result.stdout, result.stderr  # pragma: no cover


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

    active_runner = runner if runner is not None else _default_runner
    argv = ["yt-dlp", "--no-playlist", "-o", str(out_path), normalized]
    returncode, _stdout, stderr = active_runner(argv)
    if returncode != 0:
        tail = stderr[-800:].decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr)[-800:]
        raise RuntimeError(f"yt-dlp failed for {normalized!r} (exit {returncode}): {tail}")

    # A zero exit is not proof of video. yt-dlp falls back to a generic
    # extractor on URLs it has no site handler for, and that extractor happily
    # saves whatever the server returned under the requested filename. Two
    # government-PDF URLs in this project produced files beginning `%PDF-1.7`
    # named `.mp4`, recorded as primary video assets; the failure only surfaced
    # later, as "moov atom not found" from ffmpeg, in the middle of a render.
    active_prober = prober if prober is not None else _default_prober
    if not active_prober(out_path):
        if out_path.exists():
            out_path.unlink()
        raise RuntimeError(
            f"Retrieved {normalized!r} but it carries no decodable video stream "
            f"(yt-dlp exited 0, most likely via its generic extractor on a page "
            f"or document rather than a video). Not recorded."
        )

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

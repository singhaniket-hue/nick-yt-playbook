"""Archival footage from keyless public sources.

Archive.org and Wikimedia Commons need no API key. Both expose a licence for each
item, and this module refuses to hand back anything whose licence it cannot
determine — an asset with an unknown licence is worse than no asset, because it
enters the edit looking usable. The same posture applies to the media URL: for
archive.org, `search_archive_org` cannot know a real derivative filename (the
search API returns no file list), so it leaves `media_url` empty rather than
guess. `resolve_media_url` makes the separate `/metadata/<identifier>` call
that can answer that question. A hit is only `is_usable` once both the licence
and the media URL are resolved.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

# A GET transport: url -> (status, body)
Transport = Callable[[str], tuple[int, bytes]]

UNKNOWN_LICENSE = "unknown"

_ARCHIVE_ORG_SEARCH_URL = "https://archive.org/advancedsearch.php"
_ARCHIVE_ORG_METADATA_URL = "https://archive.org/metadata"
_WIKIMEDIA_API_URL = "https://commons.wikimedia.org/w/api.php"

# Collections known to be public domain outright, so a doc with no `licenseurl`
# but membership here can still be marked usable rather than falling through to
# UNKNOWN_LICENSE. Deliberately small and conservative -- add to this list only
# for collections that are unambiguously PD across every item in them.
_PUBLIC_DOMAIN_COLLECTIONS = ("prelinger",)

# Preferred video derivative formats, most preferred first. Order is
# deliberate: everything gets re-encoded downstream in the render stage, so
# broad decoder compatibility beats raw bitrate or resolution here. Do not
# "fix" this ordering later to chase file size or quality -- the re-encode
# erases that difference anyway.
VIDEO_FORMAT_PREFERENCE = ("h.264", "HiRes MPEG4", "MPEG4", "512Kb MPEG4", "Ogg Video", "MPEG2")


@dataclass(frozen=True)
class SearchHit:
    provider: str
    identifier: str
    title: str
    original_url: str
    media_url: str
    license: str
    mediatype: str = ""

    @property
    def is_usable(self) -> bool:
        """Whether this hit may enter the edit.

        True only once the hit has been BOTH licensed (a determinable
        licence) AND resolved (a non-empty `media_url`). A hit fresh out of
        `search_archive_org` is licensed but not resolved -- search cannot
        know the real derivative filename, so `media_url` starts empty until
        `resolve_media_url` fills it in. An unknown licence or an
        unresolved URL are each, on their own, enough to make a hit unusable.
        """
        return bool(self.license) and self.license != UNKNOWN_LICENSE and bool(self.media_url)


def _iso_utc(ts: datetime) -> str:
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_text(value: object) -> str:
    """Flatten archive.org's sometimes-list-sometimes-string fields."""
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(v) for v in value if v is not None).strip()
    return str(value).strip()


def _collection_names(doc: dict) -> list[str]:
    raw = doc.get("collection")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(c) for c in raw]
    return [str(raw)]


def _archive_org_license(doc: dict) -> str:
    license_url = doc.get("licenseurl")
    if license_url:
        return str(license_url)
    collections = {c.lower() for c in _collection_names(doc)}
    if collections & set(_PUBLIC_DOMAIN_COLLECTIONS):
        return "publicdomain"
    return UNKNOWN_LICENSE


def search_archive_org(query: str, transport: Transport, limit: int = 10) -> list[SearchHit]:
    params = [
        ("q", query),
        ("output", "json"),
        ("rows", str(limit)),
        ("page", "1"),
        ("fl[]", "identifier"),
        ("fl[]", "title"),
        ("fl[]", "licenseurl"),
        ("fl[]", "collection"),
        ("fl[]", "mediatype"),
    ]
    url = f"{_ARCHIVE_ORG_SEARCH_URL}?{urlencode(params)}"

    status, body = transport(url)
    if status != 200:
        raise RuntimeError(f"archive.org search failed: HTTP {status}")

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(data, dict):
        return []

    docs = ((data.get("response") or {}).get("docs")) or []
    if not isinstance(docs, list):
        return []

    hits: list[SearchHit] = []
    for doc in docs[:limit]:
        if not isinstance(doc, dict):
            continue
        identifier = doc.get("identifier")
        if not identifier:
            continue
        # Only movies have a video derivative at all; a text or audio item
        # (or a doc with mediatype missing entirely) has none, and handing
        # one back is the most likely route to a broken asset later.
        if doc.get("mediatype") != "movies":
            continue
        identifier = str(identifier)
        title = _to_text(doc.get("title"))
        hits.append(
            SearchHit(
                provider="archive_org",
                identifier=identifier,
                title=title,
                original_url=f"https://archive.org/details/{identifier}",
                # advancedsearch.php carries no file list at all -- there is
                # no derivative filename to put here. Getting the real one
                # needs a separate /metadata/<identifier> call; see
                # resolve_media_url. Search must not guess.
                media_url="",
                license=_archive_org_license(doc),
                mediatype="movies",
            )
        )
    return hits


def _coerce_size(value: object) -> int:
    """Best-effort int coercion for archive.org's `size`, which may be a str.

    A missing or unparseable size is treated as 0 rather than raising --
    it just means this file loses size comparisons against ones with a
    real size, not that resolution should blow up.
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def resolve_media_url(hit: SearchHit, transport: Transport) -> SearchHit:
    """Resolve `hit`'s real derivative filename via archive.org's metadata API.

    `search_archive_org` cannot know a file's name: the advancedsearch.php
    response carries no file list, and the old `<identifier>.mp4` guess was a
    naming *convention*, not a guarantee -- an item whose mediatype isn't
    `movies`, or whose derivation failed, may not have one, and even when it
    exists it is not always the best file. This calls
    `https://archive.org/metadata/<identifier>`, which does carry the file
    list, and picks the best video file per `VIDEO_FORMAT_PREFERENCE`: the
    first preferred format with any matching file, and the largest file (by
    `size`) among files of that format. Files under a `.thumbs/` path are
    never candidates.

    Returns a new SearchHit with `media_url` set to
    `https://archive.org/download/<identifier>/<filename>`. If no file
    matches any preferred format, returns `hit` unchanged with `media_url`
    still `""` -- this is a normal outcome, not an error (not every item has
    a usable derivative); `is_usable` already reports it to callers.

    Raises RuntimeError, naming `hit.identifier`, on a non-200 response.
    """
    url = f"{_ARCHIVE_ORG_METADATA_URL}/{hit.identifier}"
    status, body = transport(url)
    if status != 200:
        raise RuntimeError(
            f"archive.org metadata lookup for {hit.identifier!r} failed: HTTP {status}"
        )

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    files = data.get("files")
    if not isinstance(files, list):
        files = []

    chosen_name: str | None = None
    for fmt in VIDEO_FORMAT_PREFERENCE:
        candidates: list[tuple[str, int]] = []
        for entry in files:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not name:
                continue
            name = str(name)
            if ".thumbs/" in name:
                continue
            if entry.get("format") != fmt:
                continue
            candidates.append((name, _coerce_size(entry.get("size"))))
        if candidates:
            chosen_name = _pick_derivative(candidates)
            break

    if chosen_name is None:
        return hit

    return replace(hit, media_url=f"https://archive.org/download/{hit.identifier}/{chosen_name}")


# What a cut can actually contain. Stills are fine -- the framing filters give a
# still motion -- so images count alongside video. Everything else in Commons'
# File: namespace (PDF and DjVu book scans, audio, office documents) does not,
# however cleanly licensed it is.
USABLE_MEDIA_PREFIXES = ("image/", "video/")

# Types that satisfy the prefixes above but are still not footage:
#
# - SVG is an image by MIME type, but an SVG coat of arms or schematic is a
#   graphic, and this pipeline draws its own graphics from the style pack rather
#   than borrowing someone else's.
# - DjVu is the trap, because it is served as `image/vnd.djvu` and so passes an
#   `image/` prefix check while being a scanned *book* -- exactly the material
#   this gate was added to keep out, wearing an image MIME type.
EXCLUDED_MEDIA_TYPES = (
    "image/svg+xml",
    "image/vnd.djvu",
    "image/x-djvu",
    "image/tiff",
)


def is_usable_mediatype(mime: str) -> bool:
    """Whether a MIME type can appear in a cut at all."""
    mime = (mime or "").lower().strip()
    if not mime or mime in EXCLUDED_MEDIA_TYPES:
        return False
    return mime.startswith(USABLE_MEDIA_PREFIXES)


def search_wikimedia(query: str, transport: Transport, limit: int = 10) -> list[SearchHit]:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": "6",
        "gsrlimit": str(limit),
        "prop": "imageinfo",
        # `mime` is requested so hits can be gated on media type. Namespace 6
        # (File:) is not a video/image namespace -- it holds PDFs, DjVu book
        # scans, audio, SVG diagrams -- and this is a full-text search over file
        # titles, so a query like "courtroom sketch, hand-drawn" matches the
        # scanned title page of a 19th-century lecture collection.
        "iiprop": "url|extmetadata|mime",
    }
    url = f"{_WIKIMEDIA_API_URL}?{urlencode(params)}"

    status, body = transport(url)
    if status != 200:
        raise RuntimeError(f"Wikimedia Commons search failed: HTTP {status}")

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(data, dict):
        return []

    pages_map = ((data.get("query") or {}).get("pages")) or {}
    if not isinstance(pages_map, dict):
        return []
    pages = list(pages_map.values())

    hits: list[SearchHit] = []
    for page in pages[:limit]:
        if not isinstance(page, dict):
            continue
        imageinfo = page.get("imageinfo") or []
        if not imageinfo:
            continue
        info = imageinfo[0]
        if not isinstance(info, dict):
            continue

        title = _to_text(page.get("title"))
        extmeta = info.get("extmetadata") or {}
        license_field = extmeta.get("LicenseShortName") or {}
        license_value = license_field.get("value") or UNKNOWN_LICENSE
        media_url = info.get("url") or ""
        original_url = info.get("descriptionurl") or (
            f"https://commons.wikimedia.org/wiki/{title}" if title else ""
        )
        identifier = str(page.get("pageid") or title)
        mime = _to_text(info.get("mime")).lower()

        # `search_archive_org` already refuses anything whose mediatype is not
        # "movies". Wikimedia had no equivalent gate, so it was the provider
        # putting book PDFs into an episode's archival tier -- licensed,
        # resolvable, downloadable, and completely unusable as footage. A
        # documentary can cut to a still or a clip; it cannot cut to a PDF.
        if not is_usable_mediatype(mime):
            continue

        hits.append(
            SearchHit(
                provider="wikimedia",
                identifier=identifier,
                title=title,
                original_url=str(original_url),
                media_url=str(media_url),
                license=str(license_value),
                mediatype=mime,
            )
        )
    return hits


# Vocabulary describing the *shot* rather than its subject. `slots.build_slots`
# builds its query seed from the marker's kind and detail and says so: "a seed
# for asset search, not a search strategy... real query refinement is an
# authoring decision made later, by whatever retrieves the asset". This module
# is that retriever, and until now it refined nothing -- sending Commons the
# literal string "archival courtroom sketch, hand-drawn", which contains this
# pipeline's own word for the tier, a comma, and a stylistic modifier that no
# archive file title carries.
#
# Measured across 13 real slot queries with the media-type gate on: the raw seed
# returned 5 usable hits across 4 queries; refined, 43 across 6. "courtroom
# sketch" finds 12 where "archival courtroom sketch, hand-drawn" finds none.
_SHOT_VOCABULARY = frozenset({
    "archival", "screenshot", "capture", "plate", "graphic",
    "generic", "hand-drawn", "handdrawn", "wide", "shot", "shots",
    "footage", "still", "stills", "b-roll", "broll", "overlay",
    "callout", "montage", "closeup", "close-up", "aerial", "cuts",
    "quick", "zoom", "pan", "frame", "frames",
})

_QUERY_SPLIT_RE = re.compile(r"[\s,;:]+")


# Ceiling on a downloaded derivative, in bytes.
#
# An archival slot holds a few seconds and the render is 1080p, so the source
# only has to survive a downscale and a crop. Picking the largest available file
# meant picking archive.org's preservation master: one 12-second slot pulled a
# 182 MB file, and 50 slots were on course for roughly 8 GB. 120 MB is generous
# for a short clip while ruling that out.
MAX_DERIVATIVE_BYTES = 120 * 1024 * 1024


def _pick_derivative(candidates: list[tuple[str, int]]) -> str:
    """Best-quality file that is not a preservation master.

    Largest under `MAX_DERIVATIVE_BYTES`, since within one format bigger means
    better quality up to the point of absurdity. If every candidate is over the
    ceiling the smallest is taken -- something has to be chosen, and the smallest
    master is the least wasteful of them. Size 0 (archive.org omits `size` on
    some entries) sorts as unknown rather than as tiny, so a missing size cannot
    win by looking like the smallest file.
    """
    known = [(name, size) for name, size in candidates if size > 0]
    if not known:
        return candidates[0][0]

    under = [(name, size) for name, size in known if size <= MAX_DERIVATIVE_BYTES]
    if under:
        return max(under, key=lambda item: item[1])[0]
    return min(known, key=lambda item: item[1])[0]


def refine_query(seed: str) -> str:
    """Turn a slot's query seed into something an archive full-text search can match.

    Drops shot vocabulary and punctuation, keeping the subject. Returns the seed
    unchanged if refining would leave nothing -- a query made entirely of shot
    words is better sent as-is than sent empty, since an empty search matches
    everything.
    """
    words = [w for w in _QUERY_SPLIT_RE.split(seed.lower()) if w]
    kept = [w for w in words if w not in _SHOT_VOCABULARY]
    return " ".join(kept) if kept else seed.strip()


def commons_file(title: str, transport: Transport) -> SearchHit:
    """Look up one named Wikimedia Commons file, by `File:...` title.

    The counterpart to searching: an editor who has already chosen a specific
    file needs its real licence and URL, not a keyword match. Full-text search
    over file titles is a poor way to source documentary footage -- measured on
    this pipeline's own 50 archival queries, 54% returned a licensed image or
    video and only a handful were about the subject at all -- so hand-picking
    is the normal path for archival material, not the exception.

    Licence and attribution come from the API rather than from whatever the
    editor remembers seeing, because both end up in the ledger and in the
    episode's attribution.
    """
    params = {
        "action": "query",
        "format": "json",
        "titles": title,
        "prop": "imageinfo",
        "iiprop": "url|mime|extmetadata",
    }
    status, body = transport(f"{_WIKIMEDIA_API_URL}?{urlencode(params)}")
    if status != 200:
        raise RuntimeError(f"Commons lookup for {title!r} failed: HTTP {status}")

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Commons lookup for {title!r} returned unparseable JSON") from exc

    pages = ((data.get("query") or {}).get("pages")) or {}
    if not isinstance(pages, dict) or not pages:
        raise RuntimeError(f"Commons lookup for {title!r} returned no pages")

    page = next(iter(pages.values()))
    if not isinstance(page, dict) or "missing" in page:
        raise RuntimeError(f"Commons has no file titled {title!r}")

    imageinfo = page.get("imageinfo") or []
    if not imageinfo or not isinstance(imageinfo[0], dict):
        raise RuntimeError(f"Commons file {title!r} carries no imageinfo")
    info = imageinfo[0]

    mime = _to_text(info.get("mime")).lower()
    if not is_usable_mediatype(mime):
        raise RuntimeError(
            f"Commons file {title!r} has media type {mime!r}, which cannot appear "
            f"in a cut (usable: {', '.join(USABLE_MEDIA_PREFIXES)})."
        )

    extra = info.get("extmetadata") or {}
    license_value = (extra.get("LicenseShortName") or {}).get("value") or UNKNOWN_LICENSE
    resolved_title = _to_text(page.get("title")) or title

    return SearchHit(
        provider="wikimedia",
        identifier=str(page.get("pageid") or resolved_title),
        title=resolved_title,
        original_url=str(info.get("descriptionurl") or ""),
        media_url=str(info.get("url") or ""),
        license=str(license_value),
        mediatype=mime,
    )


def search_archives(
    query: str, transport: Transport, limit: int = 10
) -> tuple[list[SearchHit], list[str]]:
    """Search both providers, archive.org first.

    `query` is refined by `refine_query` before it is sent: callers pass the slot
    plan's raw seed, and stripping the shot vocabulary out of it is this
    module's job (see `_SHOT_VOCABULARY`).

    Returns `(hits, failures)`. A provider that fails (non-200 transport
    response) does not abort the other provider's search -- its failure is
    recorded as a string in `failures` (e.g. "archive_org: archive.org search
    failed: HTTP 500") instead of being swallowed, so a caller can tell "zero
    results" apart from "a provider was unreachable" and decide whether to
    retry, warn, or proceed with a partial hit list.
    """
    refined = refine_query(query)
    hits: list[SearchHit] = []
    failures: list[str] = []

    try:
        hits.extend(search_archive_org(refined, transport, limit))
    except RuntimeError as exc:
        failures.append(f"archive_org: {exc}")

    try:
        hits.extend(search_wikimedia(refined, transport, limit))
    except RuntimeError as exc:
        failures.append(f"wikimedia: {exc}")

    return hits, failures


def download(hit: SearchHit, out_path: Path, transport: Transport) -> Path:
    """Download `hit`'s media to `out_path`. This is the rights gate.

    Raises RuntimeError -- naming the hit -- before any transport call, and
    the message distinguishes which of `is_usable`'s two requirements was
    unmet: an undeterminable licence, or an unresolved `media_url` (i.e.
    `resolve_media_url` was never called). That distinction matters to a
    caller deciding what to do next. Also raises on a non-200 response and on
    an empty body. On any failure after the file may have been written, the
    partial file is removed; the containing directory (which
    `mkdir(parents=True)` may have just created) is left in place, since
    removing it could delete a pre-existing directory this call did not own.
    """
    if not hit.license or hit.license == UNKNOWN_LICENSE:
        raise RuntimeError(
            f"Refusing to download {hit.identifier!r} from {hit.provider}: "
            f"licence is {hit.license!r}, which is not usable."
        )
    if not hit.media_url:
        raise RuntimeError(
            f"Refusing to download {hit.identifier!r} from {hit.provider}: "
            f"media_url is not resolved -- call resolve_media_url first."
        )

    status, body = transport(hit.media_url)
    if status != 200:
        raise RuntimeError(
            f"Download of {hit.identifier!r} from {hit.media_url} failed: HTTP {status}"
        )
    if not body:
        raise RuntimeError(f"Download of {hit.identifier!r} from {hit.media_url} returned an empty body")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        out_path.write_bytes(body)
    except Exception:
        if out_path.exists():
            out_path.unlink()
        raise
    return out_path


def to_record_fields(
    hit: SearchHit, asset_id: str, local_path: Path, now: datetime | None = None
) -> dict:
    """Fields needed to construct an `AssetRecord` for this hit."""
    ts = now if now is not None else datetime.now(timezone.utc)
    notes = f"{hit.title} ({hit.identifier})" if hit.title else hit.identifier
    if hit.media_url:
        # original_url only points at the item page; with six-plus
        # derivatives possible per item, the ledger must say exactly which
        # file was taken, not just which item it came from.
        notes = f"{notes} -- resolved to {hit.media_url}"
    return {
        "asset_id": asset_id,
        "tier": "archival",
        "provider": hit.provider,
        "original_url": hit.original_url,
        "license": hit.license,
        "retrieved_at": _iso_utc(ts),
        "local_path": str(local_path),
        "notes": notes,
    }

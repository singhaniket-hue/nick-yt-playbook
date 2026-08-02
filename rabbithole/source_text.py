"""Truthful, source-backed text extracts for pages whose pixels are unavailable.

This module is deliberately separate from browser capture.  A consent wall can
produce a perfectly decodable video while hiding the article the editor meant
to cite, so decode QA alone cannot make that output evidence.  When research
has already authored a short, verbatim source fragment, this renderer places
that fragment on an explicitly disclosed editorial reading surface instead.

The result contains no webpage image and never presents itself as a screenshot.
It carries the publisher, article title, publication date, and canonical URL in
the frame, while a single yellow band marks the exact fragment selected for the
current narration beat.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from rabbithole import cards


SOURCE_TEXT_EXTRACT_DISCLOSURE = "VERBATIM SOURCE-TEXT EXTRACT"
MAX_EXTRACT_CHARACTERS = 320
MAX_TITLE_CHARACTERS = 240
MAX_URL_CHARACTERS = 500


def _clean(value: str) -> str:
    """Collapse authoring whitespace without changing the quoted words."""

    return re.sub(r"\s+", " ", str(value or "")).strip()


def publisher_from_url(url: str) -> str:
    """Return a stable visible publisher label from a canonical HTTP(S) URL."""

    host = (urlsplit(str(url or "").strip()).hostname or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


@dataclass(frozen=True)
class SourceTextExtractSpec:
    """One short, verbatim source fragment rendered as editorial typesetting."""

    text: str
    publisher: str
    title: str
    date: str
    url: str
    duration: float

    def __post_init__(self) -> None:
        cleaned = {
            "text": _clean(self.text),
            "publisher": _clean(self.publisher),
            "title": _clean(self.title),
            "date": _clean(self.date),
            "url": str(self.url or "").strip(),
        }
        for name, value in cleaned.items():
            if not value:
                raise ValueError(f"Source-text extract {name} must be non-empty")
            object.__setattr__(self, name, value)

        if len(self.text) > MAX_EXTRACT_CHARACTERS:
            raise ValueError(
                "Source-text extract is too long for a brief attributed reading "
                f"surface ({len(self.text)} > {MAX_EXTRACT_CHARACTERS} characters)"
            )
        if len(self.title) > MAX_TITLE_CHARACTERS:
            raise ValueError(
                f"Source-text extract title exceeds {MAX_TITLE_CHARACTERS} characters"
            )
        if len(self.url) > MAX_URL_CHARACTERS:
            raise ValueError(
                f"Source-text extract URL exceeds {MAX_URL_CHARACTERS} characters"
            )

        parsed = urlsplit(self.url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError(
                "Source-text extract URL must be an absolute HTTP(S) URL"
            )
        if isinstance(self.duration, bool) or not isinstance(
            self.duration, (int, float)
        ):
            raise TypeError("Source-text extract duration must be a number")
        if not math.isfinite(float(self.duration)) or self.duration <= 0:
            raise ValueError(
                "Source-text extract duration must be positive and finite"
            )

    def card_spec(self) -> cards.CardSpec:
        """Map the extract to the shared stable-row card renderer.

        The fourth row is only the verbatim fragment.  Its yellow band therefore
        cannot accidentally imply that an editorial label or URL was spoken.
        """

        return cards.CardSpec(
            kind="document",
            heading=SOURCE_TEXT_EXTRACT_DISCLOSURE,
            duration=float(self.duration),
            items=(
                f"SOURCE | {self.publisher} | {self.date}",
                self.title,
                self.url,
                self.text,
            ),
            active_item_index=3,
        )


def source_text_extract_ass(
    spec: SourceTextExtractSpec,
    typography: dict,
    palette: dict,
    *,
    width: int = 1920,
    height: int = 1080,
) -> str:
    """Return the disclosed ASS reading surface for one source fragment."""

    return cards.card_ass(
        spec.card_spec(), typography, palette, width=width, height=height
    )


def build_source_text_extract(
    spec: SourceTextExtractSpec,
    out_path: Path,
    typography: dict,
    palette: dict,
    grade: dict,
    work_dir: Path,
    *,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
) -> Path:
    """Render a portable MP4 containing the disclosed source-text extract."""

    return cards.build_card(
        spec.card_spec(),
        out_path,
        typography,
        palette,
        grade,
        work_dir,
        width=width,
        height=height,
        fps=fps,
    )

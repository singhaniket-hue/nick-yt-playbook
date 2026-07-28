"""The marker grammar: parse a script into narration text plus timing events."""

from __future__ import annotations

import re
from dataclasses import dataclass

MARKER_KINDS = (
    "ACT",
    "CHAPTER",
    "SHOT",
    "SILENCE",
    "SFX",
    "MUSIC",
    "REHOOK",
    "CENSOR",
    "KEY",
)

SHOT_KINDS = ("archival", "screenshot", "capture", "plate", "graphic")

# Nested brackets (e.g. `[SFX:foo[bar]]`) are unsupported: the arg stops at the
# first "]", so a nested "]" closes the marker early and leaves a stray "]" in text.
_MARKER_RE = re.compile(
    r"\[(" + "|".join(MARKER_KINDS) + r")(?::([^\]\n]*))?\]"
)


@dataclass(frozen=True)
class Marker:
    """A single timing event lifted out of the script."""

    kind: str
    arg: str
    line: int
    word_index: int
    raw: str


@dataclass(frozen=True)
class ParsedScript:
    """Narration text with all markers removed, plus the markers in source order."""

    text: str
    markers: tuple[Marker, ...]


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse(source: str) -> ParsedScript:
    """Split a marked-up script into narration text and marker events.

    `word_index` is the number of narration words that precede the marker, which
    is the anchor the validators and the narration planner both work in.
    """
    parts: list[str] = []
    markers: list[Marker] = []
    pos = 0
    words = 0

    for match in _MARKER_RE.finditer(source):
        chunk = source[pos : match.start()]
        parts.append(chunk)
        words += len(chunk.split())
        markers.append(
            Marker(
                kind=match.group(1),
                arg=(match.group(2) or "").strip(),
                line=source.count("\n", 0, match.start()) + 1,
                word_index=words,
                raw=match.group(0),
            )
        )
        pos = match.end()

    parts.append(source[pos:])
    return ParsedScript(text=_normalize(" ".join(parts)), markers=tuple(markers))


def word_count(parsed: ParsedScript) -> int:
    """Number of spoken words, markers excluded."""
    return len(parsed.text.split())


def markers_of(parsed: ParsedScript, kind: str) -> tuple[Marker, ...]:
    """All markers of one kind, in source order."""
    return tuple(m for m in parsed.markers if m.kind == kind)

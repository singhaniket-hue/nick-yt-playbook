"""Timed graphics instructions derived from the script's overlay markers.

Three marker kinds describe things drawn ON TOP of the footage rather than
sourced as footage: `[CHAPTER:<n> <title>]`, `[CENSOR:<region>]`, and
`[KEY:<word>]`. This module turns them into timed instructions consumed later
by the Remotion graphics layer. It never invents an overlay the script did not
ask for -- a document with none of these three marker kinds yields `[]`, and a
document with only chapters yields only chapter-cards, with no special-casing
required by the caller.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from rabbithole.edl import Cut
from rabbithole.validate import Finding

CHAPTER_CARD_SECONDS = 3.0

# Upper bound, in words, on how far forward `_find_keyword_word` scans from a
# `[KEY:]` marker's `word_index` looking for its target word. At this format's
# measured narration rate of 164.5 WPM, 30 words is roughly eleven seconds --
# generous for "the word in the sentence this marker precedes", tight enough
# that a stale or typo'd keyword cannot silently latch onto a coincidental
# match in a different chapter. Do not relax this without re-deriving the
# number from the format's actual pacing.
KEYWORD_SEARCH_WINDOW_WORDS = 30

# A censor box with no containing cut (only reachable when `cuts` is empty --
# see `_censor_overlay`) holds for this long rather than being left undefined.
_CENSOR_FALLBACK_SECONDS = 1.0


@dataclass(frozen=True)
class Overlay:
    kind: str  # "chapter-card" | "censor" | "keyword"
    start: float
    end: float
    text: str
    detail: str


def _strip_punctuation(word: str) -> str:
    """Strip leading/trailing punctuation, Latin or Devanagari, from a word.

    Narration words carry trailing commas, periods, and Devanagari dandas
    ('।', '॥') picked up from the script. A plain `\\W` strip is unsafe here:
    Devanagari dependent vowel signs (matras) are Unicode category Mn/Mc, not
    alphanumeric, so a `\\W`-based strip would eat the last matra off a word
    like 'है' along with the punctuation that follows it. Stripping by actual
    Unicode punctuation category (P*) instead leaves matras untouched and only
    removes real punctuation.
    """
    start = 0
    end = len(word)
    while start < end and unicodedata.category(word[start]).startswith("P"):
        start += 1
    while end > start and unicodedata.category(word[end - 1]).startswith("P"):
        end -= 1
    return word[start:end]


def _normalize_word(word: str) -> str:
    return _strip_punctuation(word).casefold()


def _find_keyword_word(
    marker: dict, document: dict, romanized_words: list[str] | None = None
) -> dict | None:
    """The first word matching the marker's arg within the search window.

    Matching is case-insensitive and ignores surrounding punctuation. The
    search scans forward from the marker's `word_index` (inclusive) through
    `word_index + KEYWORD_SEARCH_WINDOW_WORDS` (also inclusive) and no
    further. A `[KEY:]` marker names a word in the sentence it precedes; an
    unbounded search would let a typo'd keyword, or one whose target word a
    later revision deleted, silently latch onto a coincidental match minutes
    later in the script instead of failing loudly. Beyond the window this
    returns no match, which `missing_keywords` turns into the same
    `severity="warning"` finding used for any other absent keyword.

    **`romanized_words` is what makes this work on a Hinglish episode.** The
    timing spine's words come from whichever edition was narrated, and for a
    Hindi voice clone that is the Devanagari one -- while `[KEY:]` args are
    written in the canonical romanized script. Matching a romanized arg against
    Devanagari text fails for every genuinely Hindi word: in one production
    audit only 4 of 15 keywords resolved, and the four were the
    ones that stay Latin after transliteration ("CBI", "manifesto"). The other
    eleven highlights were lost with nothing but a warning.

    Passing the romanized script's words matches in the edition the marker was
    written in and then reads the *timing* from the spine at the same index --
    valid precisely because `check_transliteration` guarantees word n is the
    same word in both editions. Omit it and matching falls back to the spine's
    own words, which is correct for a script narrated from its canonical
    edition.
    """
    target = _normalize_word(marker.get("arg", ""))
    if not target:
        return None

    word_index = marker.get("word_index", 0)
    window_end = word_index + KEYWORD_SEARCH_WINDOW_WORDS
    words = document.get("words", [])

    if romanized_words is not None:
        words_by_index = {word["index"]: word for word in words}
        for index in range(word_index, min(window_end, len(romanized_words) - 1) + 1):
            if _normalize_word(romanized_words[index]) == target:
                # The spine is authoritative on timing; the romanized edition is
                # authoritative on which word this is.  Match by the explicit
                # global index instead of list position because review-segment
                # documents retain original indices while containing only a
                # slice of the full word list.
                return words_by_index.get(index)
        return None

    for word in words:
        if word["index"] < word_index or word["index"] > window_end:
            continue
        if _normalize_word(word["word"]) == target:
            return word
    return None


def missing_keywords(document: dict, romanized_words: list[str] | None = None) -> list[str]:
    """`[KEY:<word>]` markers whose target word could not be located.

    Recomputes the same search `build_overlays` uses, from `document` alone,
    so `check_overlays` can report on markers that produced no overlay without
    needing anything beyond the document it was already given.
    """
    return [
        marker.get("arg", "")
        for marker in document.get("markers", [])
        if marker.get("kind") == "KEY"
        and _find_keyword_word(marker, document, romanized_words) is None
    ]


def _chapter_overlay(marker: dict, duration_seconds: float) -> Overlay:
    parts = marker.get("arg", "").split(None, 1)
    number = parts[0] if parts else ""
    title = parts[1] if len(parts) > 1 else ""
    start = marker["seconds"]
    end = min(start + CHAPTER_CARD_SECONDS, duration_seconds)
    return Overlay(kind="chapter-card", start=start, end=end, text=title, detail=number)


def _censor_overlay(marker: dict, cuts: list[Cut]) -> Overlay:
    start = marker["seconds"]
    detail = marker.get("arg", "")
    containing = next((cut for cut in cuts if cut.start <= start < cut.end), None)
    end = containing.end if containing is not None else start + _CENSOR_FALLBACK_SECONDS
    return Overlay(kind="censor", start=start, end=end, text="", detail=detail)


def _keyword_overlay(
    marker: dict, document: dict, romanized_words: list[str] | None = None
) -> Overlay | None:
    word = _find_keyword_word(marker, document, romanized_words)
    if word is None:
        return None
    return Overlay(
        kind="keyword", start=word["start"], end=word["end"], text=marker.get("arg", ""), detail=""
    )


def build_overlays(
    document: dict, cuts: list[Cut], romanized_words: list[str] | None = None
) -> list[Overlay]:
    """Timed graphics instructions derived from the script's markers.

    Output is sorted by `start`, then by `kind` (the string comparison alone
    gives a fixed, deterministic order for coincident overlays: "censor" <
    "chapter-card" < "keyword") -- ties are stable regardless of the source
    markers' order in the document.
    """
    duration_seconds = document.get("duration_seconds", 0.0)
    overlays: list[Overlay] = []

    for marker in document.get("markers", []):
        kind = marker.get("kind")
        if kind == "CHAPTER":
            overlays.append(_chapter_overlay(marker, duration_seconds))
        elif kind == "CENSOR":
            overlays.append(_censor_overlay(marker, cuts))
        elif kind == "KEY":
            overlay = _keyword_overlay(marker, document, romanized_words)
            if overlay is not None:
                overlays.append(overlay)

    overlays.sort(key=lambda o: (o.start, o.kind))
    return overlays


def check_overlays(
    overlays: list[Overlay], document: dict, romanized_words: list[str] | None = None
) -> list[Finding]:
    """Verify overlays sit inside the episode and carry content.

    Every rule is `severity="error"` except the missing-keyword rule, which is
    `severity="warning"`: a `[KEY:]` marker whose word was never found is an
    authoring slip (a typo, a cut line) worth flagging, not a broken build --
    every other overlay in the document is still usable.
    """
    findings: list[Finding] = []
    duration_seconds = document.get("duration_seconds", 0.0)

    def error(message: str) -> None:
        findings.append(Finding(gate="overlays", severity="error", message=message))

    for overlay in overlays:
        if overlay.end <= overlay.start:
            error(
                f"{overlay.kind} overlay at {overlay.start}s ends at {overlay.end}s, "
                f"at or before its own start; overlays must have positive duration."
            )
        if overlay.end > duration_seconds + 1e-9:
            error(
                f"{overlay.kind} overlay ends at {overlay.end}s, past the episode "
                f"duration of {duration_seconds}s."
            )
        if overlay.kind == "chapter-card" and not overlay.text.strip():
            error(f"chapter-card overlay at {overlay.start}s has no title text.")
        if overlay.kind == "keyword" and not overlay.text.strip():
            error(f"keyword overlay at {overlay.start}s has no word text.")

    for word in missing_keywords(document, romanized_words):
        findings.append(
            Finding(
                gate="overlays",
                severity="warning",
                message=(
                    f"[KEY:{word}] found no matching word at or after its marker "
                    f"position; no keyword overlay was produced."
                ),
            )
        )

    return findings

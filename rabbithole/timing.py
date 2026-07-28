r"""Word-level timing derived from TTS character alignment.

The edit stage places roughly 650 cuts against narration at a target average shot
length of 3.12 seconds. That needs real word timestamps. Interpolating linearly
inside a chunk of up to 2,500 characters drifts by seconds, so timing comes from the
TTS provider's own character alignment instead.

Script-agnostic by construction, which matters here: TTS renders (and its character
alignment) run over the Devanagari edition (`05-devanagari.md`), because the voice
clone is a Hindi voice that mispronounces romanized Latin input. But marker
`word_index` values -- and everything downstream that anchors cuts to words -- come
from the romanized edition (`04-final.md`). This module never reads script content;
it only splits on `\S+` and indexes characters, so it works unchanged for either
edition. The two editions line up only because `word_index` n names the same word in
both -- a correspondence guaranteed by the `transliteration` gate
(`rabbithole/validate.py::check_transliteration`), not by anything in this module.
If that gate is ever bypassed, the alignment this module builds is still internally
consistent, but it no longer anchors cuts to the correct romanized word.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rabbithole.markers import Marker, ParsedScript
from rabbithole.narrate import PlanItem, SilenceGap, SpeechChunk, SynthResult

_WORD_RE = re.compile(r"\S+")
_SILENCE_ARG_RE = re.compile(r"^(\d+(?:\.\d+)?)s$")

WPM_BAND = (166, 191)


@dataclass(frozen=True)
class WordTime:
    """One narration word and when it is spoken, in whole-episode seconds."""

    index: int
    word: str
    start: float
    end: float


def build_word_times(
    rendered: list[tuple[SpeechChunk, SynthResult]],
    gaps: list[tuple[int, SilenceGap]],
) -> list[WordTime]:
    """Flatten per-chunk character alignment into whole-episode word times.

    `gaps` pairs a chunk index with the silence that PRECEDES it, so a gap shifts
    that chunk and everything after it.

    Raises `RuntimeError` if a chunk's character alignment does not cover its full
    text (words would otherwise be silently dropped, and every later word
    mis-numbered against the script), or if a gap is keyed to a chunk index that
    was never rendered (the gap would otherwise be silently ignored and everything
    after it would drift).
    """
    chunk_indices = {chunk.index for chunk, _ in rendered}
    for gap_index, _ in gaps:
        if gap_index not in chunk_indices:
            raise RuntimeError(
                f"Silence gap keyed to chunk {gap_index}, but no chunk with that "
                f"index was rendered ({sorted(chunk_indices)} available); the gap "
                f"would be silently dropped and everything after it would drift."
            )

    shift_before = {index: gap.seconds for index, gap in gaps}
    times: list[WordTime] = []
    offset = 0.0
    word_index = 0

    for chunk, synth in rendered:
        offset += shift_before.get(chunk.index, 0.0)

        if len(synth.start_times) != len(chunk.text) or len(synth.end_times) != len(
            chunk.text
        ):
            raise RuntimeError(
                f"chunk {chunk.index}: alignment length "
                f"({len(synth.start_times)} start times, {len(synth.end_times)} end "
                f"times) does not match chunk text length ({len(chunk.text)} "
                f"characters); words would be silently dropped or mis-numbered."
            )

        for match in _WORD_RE.finditer(chunk.text):
            first, last = match.start(), match.end() - 1
            times.append(
                WordTime(
                    index=word_index,
                    word=match.group(0),
                    start=offset + synth.start_times[first],
                    end=offset + synth.end_times[last],
                )
            )
            word_index += 1

        if synth.end_times:
            offset += synth.end_times[-1]

    return times


def marker_times(
    parsed: ParsedScript, times: list[WordTime]
) -> list[tuple[Marker, float]]:
    """Resolve each marker to the moment it fires.

    A marker fires as the word it precedes begins. A marker past the last word fires
    at the end of narration.
    """
    if not times:
        return []

    out: list[tuple[Marker, float]] = []
    for marker in parsed.markers:
        if marker.word_index < len(times):
            out.append((marker, times[marker.word_index].start))
        else:
            out.append((marker, times[-1].end))
    return out


def plan_gaps(plan: list[PlanItem]) -> list[tuple[int, SilenceGap]]:
    """Pair each silence gap with the index of the chunk it precedes.

    A gap shifts the chunk that follows it and everything after, so it is keyed to
    that chunk. A trailing gap with no chunk after it is dropped: it lengthens the
    audio but shifts no word.

    Two [SILENCE:] markers with no words between them produce two adjacent
    SilenceGap plan items -- both precede the same next chunk, so their seconds are
    summed into a single entry rather than one overwriting the other.
    """
    out: list[tuple[int, SilenceGap]] = []
    pending = 0.0
    has_pending = False

    for item in plan:
        if isinstance(item, SilenceGap):
            pending += item.seconds
            has_pending = True
        elif isinstance(item, SpeechChunk):
            if has_pending:
                out.append((item.index, SilenceGap(seconds=pending)))
                pending = 0.0
                has_pending = False

    return out


def timing_document(
    parsed: ParsedScript, times: list[WordTime], duration_seconds: float
) -> dict:
    """The serialisable timing spine.

    This is the handoff artifact between the script half and the edit half: it
    means the edit stage never re-runs TTS.
    """
    resolved_markers = marker_times(parsed, times)
    return {
        "duration_seconds": duration_seconds,
        "word_count": len(times),
        "words": [
            {"index": t.index, "word": t.word, "start": t.start, "end": t.end}
            for t in times
        ],
        "markers": [
            {
                "kind": marker.kind,
                "arg": marker.arg,
                "word_index": marker.word_index,
                "line": marker.line,
                "seconds": seconds,
            }
            for marker, seconds in resolved_markers
        ],
    }


def word_times_from_document(document: dict) -> list[WordTime]:
    """Recover the word timings a timing document already carries.

    The inverse of `timing_document`'s `words` block. Exists so an edited script
    can be re-marked against narration that has already been rendered, without
    going back to the TTS provider for an alignment it already gave us once.
    """
    return [
        WordTime(index=int(entry["index"]), word=entry["word"],
                 start=float(entry["start"]), end=float(entry["end"]))
        for entry in document.get("words", [])
    ]


def rebuild_timing(parsed: ParsedScript, document: dict) -> tuple[dict, list[str]]:
    """Re-derive a timing spine for an edited script against existing narration.

    Adding, moving or removing a `[SHOT:]` marker changes nothing a listener
    hears: markers are stripped before the text reaches TTS, so the rendered
    audio and every word timestamp in it stay exactly valid. Only the *marker*
    block of the timing document is wrong, and every marker's time is a lookup
    into word timings this document already holds. So re-marking an episode
    costs nothing, and the 36.8-minute narration does not have to be re-rendered
    to break up a long hold.

    That only holds while the words themselves are untouched, which is why the
    guard below is not advisory. If the script's word sequence has drifted from
    what was narrated, the timings no longer describe this text, and a rebuilt
    spine would place cuts against words at moments they are not spoken -- a
    silent, plausible-looking corruption of every downstream stage. Any
    mismatch is returned as a problem and no document is produced.

    Returns `(document, problems)`. `problems` is empty on success; when it is
    not, the returned document is the original, unmodified.
    """
    times = word_times_from_document(document)
    script_words = _WORD_RE.findall(parsed.text)
    problems: list[str] = []

    if not times:
        problems.append(
            "The timing document carries no word timings, so there is nothing to "
            "re-mark against. Run `narrate` first."
        )
        return document, problems

    if len(script_words) != len(times):
        problems.append(
            f"The script now has {len(script_words)} words but the narration was "
            f"rendered from {len(times)}. Markers can be re-marked for free; "
            f"changing the words cannot -- re-run `narrate` instead."
        )
        return document, problems

    mismatches = [
        (index, recorded.word, script_words[index])
        for index, recorded in enumerate(times)
        if recorded.word != script_words[index]
    ]
    if mismatches:
        preview = ", ".join(
            f"#{index} narrated {narrated!r} vs script {current!r}"
            for index, narrated, current in mismatches[:3]
        )
        problems.append(
            f"{len(mismatches)} word(s) differ from what was narrated ({preview}). "
            f"The recorded timings do not describe this text, so re-marking would "
            f"anchor cuts to words at moments they are not spoken. Re-run `narrate`."
        )
        return document, problems

    rebuilt = timing_document(parsed, times, float(document.get("duration_seconds", 0.0)))
    return rebuilt, problems


def timing_summary(document: dict) -> dict:
    """Derived statistics from a timing document.

    Silence is not speech and must not drag the measured rate down: WPM is
    computed against `speech_seconds`, duration with every [SILENCE:] marker's
    seconds subtracted out, not against total duration.
    """
    duration_seconds = document["duration_seconds"]
    word_count = document["word_count"]
    markers = document.get("markers", [])

    silence_seconds = 0.0
    marker_counts: dict[str, int] = {}
    rehook_seconds: list[float] = []

    for marker in markers:
        kind = marker["kind"]
        marker_counts[kind] = marker_counts.get(kind, 0) + 1
        if kind == "SILENCE":
            match = _SILENCE_ARG_RE.match(marker.get("arg", ""))
            if match:
                silence_seconds += float(match.group(1))
        elif kind == "REHOOK":
            rehook_seconds.append(marker["seconds"])

    speech_seconds = duration_seconds - silence_seconds
    measured_wpm = word_count / (speech_seconds / 60) if speech_seconds > 0 else 0.0
    in_band = WPM_BAND[0] <= measured_wpm <= WPM_BAND[1]

    rehook_gaps_minutes = [
        (later - earlier) / 60 for earlier, later in zip(rehook_seconds, rehook_seconds[1:])
    ]

    marker_counts_sorted = dict(
        sorted(marker_counts.items(), key=lambda kv: kv[1], reverse=True)
    )

    return {
        "duration_seconds": duration_seconds,
        "word_count": word_count,
        "silence_seconds": silence_seconds,
        "speech_seconds": speech_seconds,
        "measured_wpm": measured_wpm,
        "in_band": in_band,
        "marker_counts": marker_counts_sorted,
        "rehook_gaps_minutes": rehook_gaps_minutes,
    }

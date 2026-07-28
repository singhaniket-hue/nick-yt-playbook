"""Narration planning and the ElevenLabs client."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Callable

from rabbithole.config import Config
from rabbithole.markers import ParsedScript, parse

MAX_CHARS = 2500
STITCH_CONTEXT_CHARS = 500

_SENTENCE_END_RE = re.compile(r"(?<=[.!?।])\s+")
_SILENCE_ARG_RE = re.compile(r"^(\d+(?:\.\d+)?)s$")


@dataclass(frozen=True)
class SpeechChunk:
    index: int
    text: str
    previous_text: str
    next_text: str


@dataclass(frozen=True)
class SilenceGap:
    seconds: float


PlanItem = SpeechChunk | SilenceGap


def _split_paragraph(paragraph: str, max_chars: int) -> list[str]:
    if len(paragraph) <= max_chars:
        return [paragraph]

    out: list[str] = []
    current = ""
    for sentence in _SENTENCE_END_RE.split(paragraph):
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > max_chars:
            out.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def _pack(text: str, max_chars: int) -> list[str]:
    """Greedily pack paragraphs into chunks no longer than max_chars."""
    out: list[str] = []
    current = ""
    for paragraph in [p.strip() for p in text.split("\n\n") if p.strip()]:
        for piece in _split_paragraph(paragraph, max_chars):
            candidate = f"{current}\n\n{piece}".strip() if current else piece
            if current and len(candidate) > max_chars:
                out.append(current)
                current = piece
            else:
                current = candidate
    if current:
        out.append(current)
    return out


def _word_offsets(text: str) -> list[int]:
    """Character offset where each whitespace-delimited word begins.

    `len(_word_offsets(text)) == len(text.split())`, and offsets[i] is where
    word i starts in the original text, so slicing by these offsets (rather
    than splitting on whitespace and rejoining with a single space) preserves
    the original whitespace verbatim -- including blank-line paragraph breaks.
    """
    return [m.start() for m in re.finditer(r"\S+", text)]


def _slice_words(text: str, offsets: list[int], start: int, end: int) -> str:
    """Substring spanning word indices [start, end), original whitespace intact."""
    start_offset = offsets[start] if start < len(offsets) else len(text)
    end_offset = offsets[end] if end < len(offsets) else len(text)
    return text[start_offset:end_offset].strip()


def _parse_silence_seconds(marker) -> float:
    """Parse a [SILENCE:] marker's argument, raising a clear, actionable error.

    This is reachable directly from `narrate --force`, which bypasses
    `validate_all`, so it cannot assume the arg was already checked.
    """
    match = _SILENCE_ARG_RE.match(marker.arg)
    if match is None:
        raise RuntimeError(
            f"Malformed silence marker {marker.raw!r} on line {marker.line}: "
            "expected seconds with a lowercase 's' suffix, e.g. [SILENCE:1.5s]."
        )
    return float(match.group(1))


def _segments_between_silences(parsed: ParsedScript) -> list[tuple[str, float | None]]:
    """Slice narration text at silence markers.

    Returns (text, silence_seconds_following) pairs. The final pair's silence is None.
    """
    silences = [m for m in parsed.markers if m.kind == "SILENCE"]
    text = parsed.text
    offsets = _word_offsets(text)
    out: list[tuple[str, float | None]] = []
    cursor = 0

    for marker in silences:
        seconds = _parse_silence_seconds(marker)
        out.append((_slice_words(text, offsets, cursor, marker.word_index), seconds))
        cursor = marker.word_index

    out.append((_slice_words(text, offsets, cursor, len(offsets)), None))
    return out


def plan_narration(parsed: ParsedScript, max_chars: int = MAX_CHARS) -> list[PlanItem]:
    """Build the ordered narration plan.

    Chunk boundaries are forced at every [SILENCE:] marker so the gap always falls
    between two rendered requests, which keeps audio assembly a plain concat.
    """
    plan: list[PlanItem] = []
    index = 0

    for text, silence in _segments_between_silences(parsed):
        for piece in _pack(text, max_chars):
            plan.append(SpeechChunk(index=index, text=piece, previous_text="", next_text=""))
            index += 1
        if silence is not None:
            plan.append(SilenceGap(seconds=silence))

    return _apply_stitch_context(plan)


def _apply_stitch_context(plan: list[PlanItem]) -> list[PlanItem]:
    """Give each chunk the tail of the previous and the head of the next.

    Without this the clone resets tone at every join, producing an audible
    discontinuity roughly every 40 seconds of finished narration.
    """
    chunks = [i for i, item in enumerate(plan) if isinstance(item, SpeechChunk)]
    out = list(plan)

    for n, position in enumerate(chunks):
        chunk = out[position]
        previous = out[chunks[n - 1]].text[-STITCH_CONTEXT_CHARS:] if n > 0 else ""
        following = (
            out[chunks[n + 1]].text[:STITCH_CONTEXT_CHARS] if n + 1 < len(chunks) else ""
        )
        out[position] = SpeechChunk(
            index=chunk.index,
            text=chunk.text,
            previous_text=previous,
            next_text=following,
        )

    return out


def plan_from_source(source: str, max_chars: int = MAX_CHARS) -> list[PlanItem]:
    """Convenience wrapper: parse a raw script and plan it in one call."""
    return plan_narration(parse(source), max_chars=max_chars)


API_BASE = "https://api.elevenlabs.io/v1"
OUTPUT_FORMAT = "mp3_44100_128"

Transport = Callable[[str, dict, dict], tuple[int, bytes]]


def _requests_transport(url: str, headers: dict, json: dict) -> tuple[int, bytes]:
    import requests

    response = requests.post(url, headers=headers, json=json, timeout=180)
    return response.status_code, response.content


@dataclass(frozen=True)
class SynthResult:
    """Rendered audio plus the character alignment the edit stage needs.

    Alignment comes from the same request as the audio, so exact word timing costs
    no extra characters and no second API call.
    """

    audio: bytes
    characters: tuple[str, ...]
    start_times: tuple[float, ...]
    end_times: tuple[float, ...]


def synthesize(
    chunk: SpeechChunk, cfg: Config, transport: Transport | None = None
) -> SynthResult:
    """Render one chunk to audio bytes plus character-level timing.

    `transport` is injected so tests never reach the network.
    """
    send = transport or _requests_transport
    url = (
        f"{API_BASE}/text-to-speech/{cfg.voice_id}/with-timestamps"
        f"?output_format={OUTPUT_FORMAT}"
    )

    body: dict = {"text": chunk.text, "model_id": cfg.model_id}
    if chunk.previous_text:
        body["previous_text"] = chunk.previous_text
    if chunk.next_text:
        body["next_text"] = chunk.next_text

    status, payload = send(
        url,
        {"xi-api-key": cfg.elevenlabs_api_key, "Content-Type": "application/json"},
        body,
    )

    if status != 200:
        detail = payload.decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"ElevenLabs returned {status} for chunk {chunk.index}: {detail}")

    document = json.loads(payload)
    alignment = document.get("alignment")
    if not alignment:
        raise RuntimeError(
            f"ElevenLabs response for chunk {chunk.index} carried no alignment; "
            f"the edit stage cannot place cuts without it."
        )
    if "audio_base64" not in document:
        raise RuntimeError(
            f"ElevenLabs response for chunk {chunk.index} carried no audio_base64; "
            f"cannot render narration without audio."
        )

    return SynthResult(
        audio=base64.b64decode(document["audio_base64"]),
        characters=tuple(alignment["characters"]),
        start_times=tuple(alignment["character_start_times_seconds"]),
        end_times=tuple(alignment["character_end_times_seconds"]),
    )


def estimate_characters(plan: list[PlanItem]) -> int:
    """Billable characters for a plan. Stitch context is not billed."""
    return sum(len(item.text) for item in plan if isinstance(item, SpeechChunk))

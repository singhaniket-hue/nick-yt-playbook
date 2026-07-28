"""Orchestration: script text in, finished narration audio out."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rabbithole.assemble import build_silence, concat_wavs, decode_to_wav, probe_duration
from rabbithole.config import Config
from rabbithole.markers import parse
from rabbithole.narrate import (
    SilenceGap,
    SpeechChunk,
    SynthResult,
    estimate_characters,
    plan_narration,
    synthesize,
)
from rabbithole.timing import build_word_times, plan_gaps, timing_document

Synth = Callable[[SpeechChunk, Config], SynthResult]


@dataclass(frozen=True)
class NarrationResult:
    output_path: Path
    chunk_count: int
    silence_count: int
    characters: int
    duration_seconds: float
    alignments: tuple[SynthResult, ...]
    timing_path: Path


def render_narration(
    source: str,
    cfg: Config,
    out_path: Path,
    work_dir: Path,
    synth: Synth | None = None,
) -> NarrationResult:
    """Render a marked-up script to a single narration WAV.

    Also writes `timing.json` beside `out_path`: the word-level timing spine the
    edit stage reads, so it never has to re-run TTS.
    """
    render = synth or (lambda chunk, config: synthesize(chunk, config))
    work_dir.mkdir(parents=True, exist_ok=True)

    parsed = parse(source)
    plan = plan_narration(parsed)
    parts: list[Path] = []
    rendered: list[tuple[SpeechChunk, SynthResult]] = []
    chunks = silences = 0

    for item in plan:
        if isinstance(item, SpeechChunk):
            result = render(item, cfg)
            raw = work_dir / f"chunk-{item.index:04d}.raw"
            raw.write_bytes(result.audio)
            parts.append(decode_to_wav(raw, work_dir / f"chunk-{item.index:04d}.wav"))
            rendered.append((item, result))
            chunks += 1
        elif isinstance(item, SilenceGap):
            parts.append(
                build_silence(item.seconds, work_dir / f"gap-{silences:04d}.wav")
            )
            silences += 1

    concat_wavs(parts, out_path)
    duration = probe_duration(out_path)

    times = build_word_times(rendered, plan_gaps(plan))
    document = timing_document(parsed, times, duration)
    timing_path = out_path.parent / "timing.json"
    timing_path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return NarrationResult(
        output_path=out_path,
        chunk_count=chunks,
        silence_count=silences,
        characters=estimate_characters(plan),
        duration_seconds=duration,
        alignments=tuple(result for _, result in rendered),
        timing_path=timing_path,
    )

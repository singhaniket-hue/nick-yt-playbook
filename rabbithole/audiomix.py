"""Layer 2 audio mix: place SFX and music beds, duck them through silence
drops, and mix the result with the narration.

**Silence drops are the point.** `[SILENCE:n s]` means the bed mutes
*completely* -- that is the format's central tension device. The narration
WAV already contains true digital silence in those windows (an earlier phase
built it that way), so the VO itself needs no ducking here. The bed and SFX
layers absolutely do: if a drone keeps humming through a silence drop, the
device is destroyed. `duck` is what enforces that, applied to the bed and
SFX layers only, never to the VO.

**Mix levels come from the style pack, not from taste.** `style/sfx.json`
already categorises every cue, and the categories mean different things in
the mix: a `texture` crackle sits far under the narration, an `impact`
sub-drop punches through. `SFX_CATEGORY_GAIN_DB` derives gain from category
rather than using one gain for all seven cues. Beds sit at a single
`BED_GAIN_DB` on top of their own -20 dBFS synthesis target (see
`sources/music.py`), since a bed's whole job is to sit underneath everything
else, not to differentiate the way SFX categories do.

Layer shape, in order:

1. `silence_windows` / `bed_spans` / `sfx_events` read the timing document.
2. `build_sfx_layer` and `build_bed_layer` each render onto their own
   duration-length canvas (silent except where an event or span places
   sound).
3. `duck` mutes both layers -- never the VO -- inside every silence window,
   with a short ramp so the mute doesn't click.
4. `mix_audio` combines the VO (at unity) with the two ducked layers.

`build_mix` orchestrates all four steps for one document.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rabbithole.assemble import build_silence, concat_wavs, probe_duration
from rabbithole.jsonio import read_json
from rabbithole.sources.music import BedSpec, build_bed, resolve_cue
from rabbithole.sources.sfx import DEFAULT_DURATIONS, SFX_NAMES, SfxSpec, build_sfx
from rabbithole.sources.soundgen import (
    Library,
    library_bed_variants,
    library_sfx,
    tile_to_duration,
)
from rabbithole.sourceaudio import SourceAudioBite
from rabbithole.validate import Finding

SAMPLE_RATE = 44100
CHANNELS = 1

# Cues normalise to -6 dBFS at synthesis (see sources/sfx.py TARGET_PEAK_DB);
# these gains position each category relative to narration. An impact is
# meant to punch through a sentence; a texture is meant to sit beneath one
# and barely register as a separate sound.
SFX_CATEGORY_GAIN_DB = {
    "texture": -10.0,
    "accent": -8.0,
    "tension": -6.0,
    # The sub-drop was already the one cue that dominated the old mix.  Keep
    # impacts controlled while lifting the quieter informational textures.
    "impact": -8.0,
}
DEFAULT_SFX_GAIN_DB = -8.0  # unknown category
# A production test bed measured roughly 23 LU below narration. A +10 dB
# correction puts it in the restrained-but-audible documentary range without
# turning the score into trailer music.
BED_GAIN_DB = 4.0  # beds are already -20 dBFS at synthesis

# A single amix's input count is bounded here so a long episode with hundreds
# of SFX events never builds one unusable filter graph. Batches are mixed
# independently and the batch outputs are then folded together the same way
# (see _reduce_tracks), so there is no upper bound on total event count.
MAX_AMIX_INPUTS = 32

# The mute ramp at each silence-window edge. Long enough to be inaudible as
# a "ramp" rather than a click (see tests/test_audiomix.py's boundary-delta
# measurement), short enough not to eat into short silence windows.
DUCK_RAMP_SECONDS = 0.03

# Original-source bites use the same click-safe edge ramp as silence drops.
# The depth is authored per bite (``SourceAudioBite.duck_vo_db``).
SOURCE_DUCK_RAMP_SECONDS = DUCK_RAMP_SECONDS

# mix_audio never boosts a mix, only attenuates enough to keep the true peak
# at or below this ceiling -- a two-pass measure-then-attenuate, the same
# shape sources/sfx.py and sources/music.py already use for their own
# peak-normalisation, just capping instead of targeting a fixed peak.
MIX_CLIP_CEILING_DB = -1.0

_SILENCE_ARG_RE = re.compile(r"^(\d+(?:\.\d+)?)s$")


def _run(args: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-1200:]
        raise RuntimeError(f"ffmpeg failed: {' '.join(args[:4])} ...\n{detail}")
    return result


def _apply_volume(path: Path, out_path: Path, gain_db: float) -> Path:
    _run(
        [
            "ffmpeg", "-y", "-i", str(path),
            "-af", f"volume={gain_db:.2f}dB",
            "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
            "-c:a", "pcm_s16le", str(out_path),
        ]
    )
    return out_path


def _measure_peak_db(path: Path) -> float:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
    )
    text = result.stderr.decode("utf-8", errors="replace")
    match = re.search(r"max_volume:\s*(-?\d+\.?\d*) dB", text)
    if not match:
        raise RuntimeError(f"volumedetect produced no max_volume for {path}:\n{text[-800:]}")
    return float(match.group(1))


@dataclass(frozen=True)
class SilenceWindow:
    start: float
    end: float


@dataclass(frozen=True)
class BedSpan:
    start: float
    end: float
    cue: str


@dataclass(frozen=True)
class SfxEvent:
    seconds: float
    name: str


# --- document parsing ----------------------------------------------------------


def silence_windows(document: dict) -> list[SilenceWindow]:
    """[SILENCE:n s] markers as merged, duration-clamped mute windows.

    A marker's `seconds` is when it *fires*, and every marker kind fires as
    the word it precedes begins (`timing.py::marker_times`: "A marker fires
    as the word it precedes begins"). For a SILENCE marker that word is the
    one narration resumes on *after* the gap -- so `seconds` is the gap's
    END, not its start. The window is `[seconds - length, seconds]`.

    This was confirmed against real production data, not just the docstring:
    in `projects/deadair-01/narration/vo.wav`, true digital silence sits in
    exactly that trailing interval before each SILENCE marker's `seconds`
    (e.g. the first marker has `seconds=9.348`, `arg="1.5s"`, and vo.wav
    measures true silence across roughly [8.0, 9.2], landing inside
    [7.848, 9.348] once the word-boundary transition is accounted for --
    not inside [9.348, 10.848]). Ducking the wrong side would leave the bed
    and SFX layers playing right through the actual silence drop, which is
    exactly the failure this module's docstring calls out as destroying the
    format's central device.

    A malformed arg raises `RuntimeError` naming the offending marker,
    consistent with how `narrate.py`'s `_parse_silence_seconds` already
    handles this same grammar.
    """
    duration = document.get("duration_seconds")
    raw: list[tuple[float, float]] = []

    for marker in document.get("markers", []):
        if marker.get("kind") != "SILENCE":
            continue
        arg = marker.get("arg", "")
        match = _SILENCE_ARG_RE.match(arg)
        if match is None:
            raise RuntimeError(
                f"Malformed silence marker [SILENCE:{arg}] on line {marker.get('line')}: "
                "expected seconds with a lowercase 's' suffix, e.g. [SILENCE:1.5s]."
            )
        length = float(match.group(1))
        fires_at = float(marker.get("seconds", 0.0))
        end = fires_at
        start = end - length
        if duration is not None:
            end = min(end, float(duration))
        start = max(start, 0.0)
        raw.append((start, end))

    raw.sort(key=lambda w: w[0])
    merged: list[SilenceWindow] = []
    for start, end in raw:
        if merged and start <= merged[-1].end:
            prior = merged.pop()
            merged.append(SilenceWindow(start=prior.start, end=max(prior.end, end)))
        else:
            merged.append(SilenceWindow(start=start, end=end))
    return merged


def bed_spans(document: dict) -> list[BedSpan]:
    """[MUSIC:<cue>] markers as contiguous spans.

    Each span starts at one MUSIC marker and ends at the next (or at
    `duration_seconds` for the last one). A cue of `out` produces no span
    itself -- it just ends the previous one -- and a document with no MUSIC
    marker at all produces `[]`: a script with no music cue gets no bed
    invented for it.
    """
    duration = float(document.get("duration_seconds", 0.0))
    markers = sorted(
        (m for m in document.get("markers", []) if m.get("kind") == "MUSIC"),
        key=lambda m: float(m.get("seconds", 0.0)),
    )

    spans: list[BedSpan] = []
    for index, marker in enumerate(markers):
        start = float(marker.get("seconds", 0.0))
        end = float(markers[index + 1]["seconds"]) if index + 1 < len(markers) else duration
        cue = marker.get("arg", "")
        if cue == "out":
            continue
        spans.append(BedSpan(start=start, end=end, cue=cue))
    return spans


def sfx_events(document: dict) -> list[SfxEvent]:
    """[SFX:<cue>] markers as events, in time order.

    Unknown cue names are not filtered here -- that is `build_sfx_layer`'s
    job, since only it can produce the `Finding` that reports one.
    """
    markers = sorted(
        (m for m in document.get("markers", []) if m.get("kind") == "SFX"),
        key=lambda m: float(m.get("seconds", 0.0)),
    )
    return [SfxEvent(seconds=float(m.get("seconds", 0.0)), name=m.get("arg", "")) for m in markers]


def load_sfx_categories(path: Path) -> dict[str, str]:
    """Cue name -> category, straight from the style pack registry."""
    data = read_json(Path(path))
    return {name: str(info.get("category", "")) for name, info in data.items()}


# --- layer builders --------------------------------------------------------------


def _reduce_tracks(paths: list[Path], duration: float, out_path: Path, work_dir: Path, prefix: str) -> Path:
    """Sum full-length (== duration) wav tracks together, batched at MAX_AMIX_INPUTS.

    Each round mixes at most MAX_AMIX_INPUTS tracks into one; repeat until a
    single track remains. This bounds every individual amix filter graph
    regardless of how many tracks came in.
    """
    if len(paths) == 1:
        if paths[0] != out_path:
            shutil.copy(paths[0], out_path)
        return out_path

    current = list(paths)
    level = 0
    while len(current) > 1:
        next_level: list[Path] = []
        for batch_index, start in enumerate(range(0, len(current), MAX_AMIX_INPUTS)):
            batch = current[start : start + MAX_AMIX_INPUTS]
            if len(batch) == 1:
                next_level.append(batch[0])
                continue
            batch_out = work_dir / f"{prefix}-reduce-l{level}-{batch_index:04d}.wav"
            inputs: list[str] = []
            for p in batch:
                inputs += ["-i", str(p)]
            n = len(batch)
            streams = "".join(f"[{i}:a]" for i in range(n))
            filter_complex = f"{streams}amix=inputs={n}:duration=longest:normalize=0[out]"
            _run(
                [
                    "ffmpeg", "-y", *inputs,
                    "-filter_complex", filter_complex,
                    "-map", "[out]",
                    "-t", f"{duration:.6f}",
                    "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                    "-c:a", "pcm_s16le", str(batch_out),
                ]
            )
            next_level.append(batch_out)
        current = next_level
        level += 1

    if current[0] != out_path:
        shutil.copy(current[0], out_path)
    return out_path


def build_sfx_layer(
    events: list[SfxEvent],
    duration: float,
    out_path: Path,
    work_dir: Path,
    categories: dict[str, str],
    library: Library | None = None,
) -> tuple[Path, list[Finding]]:
    """Render every SFX event onto one silent, `duration`-long canvas.

    Each distinct cue name is resolved once (cached under `work_dir`) no
    matter how many times it fires. Placement is `adelay` to the event's
    time, gain is `SFX_CATEGORY_GAIN_DB[category]`. Events are batched at
    `MAX_AMIX_INPUTS` per amix call (see `_reduce_tracks`) so an episode with
    hundreds of cues never builds one unusable filter graph.

    `library`, when given, supplies generated audio for any cue it holds and
    local synthesis covers the rest. Both arrive peak-normalised to
    `sfx.TARGET_PEAK_DB` (soundgen normalises on import for exactly this
    reason), so the category gain math is identical either way and this
    function needs no knowledge of which source a cue came from. A cue the
    library is missing produces a warning rather than a silent downgrade:
    asking for generated sound and getting synthesis is worth knowing about.
    """
    out_path = Path(out_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    findings: list[Finding] = []
    valid: list[SfxEvent] = []
    for event in events:
        if event.name not in SFX_NAMES:
            findings.append(
                Finding(
                    gate="audiomix",
                    severity="error",
                    message=(
                        f"[SFX:{event.name}] at {event.seconds:.2f}s is not a known cue; "
                        f"style/sfx.json lists {', '.join(SFX_NAMES)}. Skipped."
                    ),
                )
            )
            continue
        valid.append(event)

    cache_paths: dict[str, Path] = {}
    for name in sorted({e.name for e in valid}):
        generated = library_sfx(library, name) if library is not None else None
        if generated is not None:
            cache_paths[name] = generated
            continue

        if library is not None:
            findings.append(
                Finding(
                    gate="audiomix",
                    severity="warning",
                    message=(
                        f"[SFX:{name}] is not in the sound library; falling back to "
                        f"local synthesis, which is a functional stand-in rather than "
                        f"sound design. Run `rabbithole sound build` to generate it."
                    ),
                )
            )

        cache_path = work_dir / f"sfx-{name}.wav"
        if not cache_path.exists():
            build_sfx(SfxSpec(name=name, duration=DEFAULT_DURATIONS[name]), cache_path)
        cache_paths[name] = cache_path

    if not valid:
        build_silence(duration, out_path)
        return out_path, findings

    batch_paths: list[Path] = []
    for batch_index, start in enumerate(range(0, len(valid), MAX_AMIX_INPUTS)):
        batch = valid[start : start + MAX_AMIX_INPUTS]
        batch_out = work_dir / f"sfx-batch-{batch_index:04d}.wav"
        _place_sfx_batch(batch, cache_paths, categories, duration, batch_out)
        batch_paths.append(batch_out)

    _reduce_tracks(batch_paths, duration, out_path, work_dir, prefix="sfx")
    return out_path, findings


def _place_sfx_batch(
    batch: list[SfxEvent],
    cache_paths: dict[str, Path],
    categories: dict[str, str],
    duration: float,
    out_path: Path,
) -> Path:
    inputs: list[str] = []
    labels: list[str] = []
    for index, event in enumerate(batch):
        inputs += ["-i", str(cache_paths[event.name])]
        delay_ms = max(0, round(event.seconds * 1000))
        category = categories.get(event.name, "")
        gain = SFX_CATEGORY_GAIN_DB.get(category, DEFAULT_SFX_GAIN_DB)
        label = f"e{index}"
        labels.append(
            f"[{index}:a]adelay=delays={delay_ms}:all=1,volume={gain:.2f}dB[{label}]"
        )

    chain = ";".join(labels)
    if len(batch) == 1:
        merged = "[e0]"
    else:
        streams = "".join(f"[e{i}]" for i in range(len(batch)))
        chain += f";{streams}amix=inputs={len(batch)}:duration=longest:normalize=0[mixed]"
        merged = "[mixed]"

    filter_complex = f"{chain};{merged}atrim=0:{duration:.6f},apad=whole_dur={duration:.6f}[out]"

    _run(
        [
            "ffmpeg", "-y", *inputs,
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
            "-c:a", "pcm_s16le", str(out_path),
        ]
    )
    return out_path


def build_bed_layer(
    spans: list[BedSpan],
    duration: float,
    out_path: Path,
    work_dir: Path,
    library: Library | None = None,
) -> tuple[Path, list[Finding]]:
    """Render every bed span onto one silent, `duration`-long canvas.

    Spans are non-overlapping by construction (`bed_spans` builds them that
    way), so this concatenates rather than mixes: one bed per span via
    `resolve_cue` + `build_bed`, true silence in the gaps -- before the
    first span, between spans, and after the last one. The concatenated
    result is then attenuated by `BED_GAIN_DB` on top of `build_bed`'s own
    -20 dBFS synthesis target, positioning the bed under the mix; applying
    it after concatenation (rather than per-segment) is equivalent since
    gain scales silence to silence, and is simpler than threading it through
    every segment build.

    `library`, when given, supplies generated bed variants for a span instead
    of synthesizing one. The API caps a generation at 30 seconds, so a span
    longer than that is covered by crossfade-tiling the variants
    (`soundgen.tile_to_duration`) rather than by one long request -- and by
    *several* distinct variants rather than one repeated, since a single 30s
    loop under a twelve-minute span is audible as a loop however clean the
    crossfade is.
    """
    out_path = Path(out_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    findings: list[Finding] = []

    if not spans:
        build_silence(duration, out_path)
        return out_path, findings

    ordered = sorted(spans, key=lambda s: s.start)
    segments: list[Path] = []
    cursor = 0.0
    eps = 1e-6

    for index, span in enumerate(ordered):
        if span.start > cursor + eps:
            gap_path = work_dir / f"bed-gap-{index:04d}.wav"
            build_silence(span.start - cursor, gap_path)
            segments.append(gap_path)

        bed_kind = resolve_cue(span.cue)
        span_seconds = span.end - span.start
        bed_path = work_dir / f"bed-{index:04d}-{bed_kind}.wav"

        variants = library_bed_variants(library, bed_kind) if library is not None else []
        if variants:
            if not bed_path.exists():
                tile_to_duration(variants, span_seconds, bed_path)
        else:
            if library is not None:
                findings.append(
                    Finding(
                        gate="audiomix",
                        severity="warning",
                        message=(
                            f"[MUSIC:{span.cue}] has no generated variants in the sound "
                            f"library for bed kind '{bed_kind}'; falling back to local "
                            f"synthesis. Run `rabbithole sound build` to generate them."
                        ),
                    )
                )
            if not bed_path.exists():
                build_bed(BedSpec(kind=bed_kind, duration=span_seconds), bed_path)

        segments.append(bed_path)
        cursor = span.end

    if cursor < duration - eps:
        tail_path = work_dir / "bed-tail.wav"
        build_silence(duration - cursor, tail_path)
        segments.append(tail_path)

    concat_path = work_dir / "bed-concat.wav"
    concat_wavs(segments, concat_path)
    _apply_volume(concat_path, out_path, BED_GAIN_DB)
    return out_path, findings


def duck(track_path: Path, windows: list[SilenceWindow], out_path: Path) -> Path:
    """Mute `track_path` completely inside every window, ramped at each edge.

    The `volume` filter's expression is unity everywhere except inside a
    window, where it is 0, with a `DUCK_RAMP_SECONDS` linear ramp on either
    side so the mute doesn't click. With no windows the track passes through
    unchanged (a stream copy, not a re-encode, so level and duration are
    exactly preserved).
    """
    track_path = Path(track_path)
    out_path = Path(out_path)

    if not windows:
        _run(["ffmpeg", "-y", "-i", str(track_path), "-c", "copy", str(out_path)])
        return out_path

    ramp = DUCK_RAMP_SECONDS
    traps = []
    for window in windows:
        s, e = window.start, window.end
        traps.append(
            f"clip(min((t-({s:.6f}-{ramp:.6f}))/{ramp:.6f},"
            f"({e:.6f}+{ramp:.6f}-t)/{ramp:.6f}),0,1)"
        )
    mute = traps[0]
    for trap in traps[1:]:
        mute = f"max({mute},{trap})"
    expr = f"1-({mute})"

    _run(
        [
            "ffmpeg", "-y", "-i", str(track_path),
            "-af", f"volume=eval=frame:volume='{expr}'",
            "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
            "-c:a", "pcm_s16le", str(out_path),
        ]
    )
    return out_path


def build_source_audio_layer(
    bites: list[SourceAudioBite],
    duration: float,
    out_path: Path,
    work_dir: Path,
) -> Path:
    """Trim, gain, and place original-source audio on one timeline canvas.

    Every bite is decoded independently so a manifest can mix audio from
    video containers, standalone audio files, and different codecs. The
    source seek is input-side for efficiency; the authored duration is then
    enforced again in the filter graph before the excerpt is delayed to its
    episode position. Each intermediate is a full-duration WAV, allowing the
    existing bounded ``_reduce_tracks`` tree to handle any bite count.
    """
    out_path = Path(out_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    if not bites:
        build_silence(duration, out_path)
        return out_path

    tracks: list[Path] = []
    for index, bite in enumerate(bites):
        bite_path = work_dir / f"source-bite-{index:04d}.wav"
        delay_ms = max(0, round(bite.timeline_start * 1000))
        filter_complex = (
            f"[0:a:0]atrim=duration={bite.duration:.6f},"
            f"asetpts=PTS-STARTPTS,volume={bite.gain_db:.2f}dB,"
            f"adelay=delays={delay_ms}:all=1,"
            f"atrim=0:{duration:.6f},apad=whole_dur={duration:.6f}[out]"
        )
        try:
            _run(
                [
                    "ffmpeg", "-y",
                    "-ss", f"{bite.source_start:.6f}",
                    "-t", f"{bite.duration:.6f}",
                    "-i", str(bite.local_path),
                    "-filter_complex", filter_complex,
                    "-map", "[out]",
                    "-t", f"{duration:.6f}",
                    "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                    "-c:a", "pcm_s16le", str(bite_path),
                ]
            )
        except RuntimeError as exc:
            if bite_path.exists():
                bite_path.unlink()
            raise RuntimeError(
                f"Could not render source-audio bite {index} from "
                f"{bite.local_path}: {exc}"
            ) from exc
        tracks.append(bite_path)

    _reduce_tracks(tracks, duration, out_path, work_dir, prefix="source-audio")
    return out_path


def duck_for_source_audio(
    track_path: Path,
    bites: list[SourceAudioBite],
    out_path: Path,
) -> Path:
    """Attenuate a track under source bites using each bite's authored depth.

    Overlapping bites choose the strongest attenuation instead of multiplying
    reductions. A 30 ms linear edge ramp prevents clicks. With no bites this
    is a stream copy, preserving the pre-feature render path exactly.
    """
    track_path = Path(track_path)
    out_path = Path(out_path)
    if not bites:
        _run(["ffmpeg", "-y", "-i", str(track_path), "-c", "copy", str(out_path)])
        return out_path

    ramp = SOURCE_DUCK_RAMP_SECONDS
    gains: list[str] = []
    for bite in bites:
        start = bite.timeline_start
        end = bite.timeline_end
        amplitude = 10 ** (bite.duck_vo_db / 20.0)
        active = (
            f"clip(min((t-({start:.6f}-{ramp:.6f}))/{ramp:.6f},"
            f"({end:.6f}+{ramp:.6f}-t)/{ramp:.6f}),0,1)"
        )
        gains.append(f"(1-(1-{amplitude:.9f})*{active})")

    expression = gains[0]
    for gain in gains[1:]:
        expression = f"min({expression},{gain})"

    _run(
        [
            "ffmpeg", "-y", "-i", str(track_path),
            "-af", f"volume=eval=frame:volume='{expression}'",
            "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
            "-c:a", "pcm_s16le", str(out_path),
        ]
    )
    return out_path


def mix_audio(
    vo_path: Path,
    bed_path: Path,
    sfx_path: Path,
    out_path: Path,
    *,
    source_audio_path: Path | None = None,
) -> Path:
    """Mix VO, the ducked bed/SFX layers, and optional original-source audio.

    Output runs the VO's own duration (`amix ... duration=first`, VO is the
    first input) and is mono 44100 pcm_s16le. Never clips: measured
    two-pass, the same shape `sources/sfx.py`/`sources/music.py` already use
    for their own peak normalisation -- if the raw mix's peak would exceed
    `MIX_CLIP_CEILING_DB`, attenuate just enough to bring it under; a mix
    that already sits under the ceiling is left exactly as mixed, never
    boosted.
    """
    out_path = Path(out_path)
    raw_path = out_path.with_suffix(".raw.wav")

    try:
        inputs = [
            "-i", str(vo_path),
            "-i", str(bed_path),
            "-i", str(sfx_path),
        ]
        input_count = 3
        if source_audio_path is not None:
            inputs += ["-i", str(source_audio_path)]
            input_count += 1
        streams = "".join(f"[{index}:a]" for index in range(input_count))
        _run(
            [
                "ffmpeg", "-y",
                *inputs,
                "-filter_complex",
                f"{streams}amix=inputs={input_count}:duration=first:normalize=0[out]",
                "-map", "[out]",
                "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                "-c:a", "pcm_s16le", str(raw_path),
            ]
        )
        peak = _measure_peak_db(raw_path)
        gain = min(0.0, MIX_CLIP_CEILING_DB - peak)
        _run(
            [
                "ffmpeg", "-y", "-i", str(raw_path),
                "-af", f"volume={gain:.4f}dB",
                "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                "-c:a", "pcm_s16le", str(out_path),
            ]
        )
    finally:
        if raw_path.exists():
            raw_path.unlink()

    return out_path


def build_mix(
    document: dict,
    vo_path: Path,
    out_path: Path,
    work_dir: Path,
    style_dir: Path,
    library: Library | None = None,
    source_audio_bites: list[SourceAudioBite] | None = None,
) -> tuple[Path, list[Finding]]:
    """Layer 2: place SFX, beds, and optional original-source audio.

    `duration_seconds` from the timing document drives both canvases (the
    VO's own probed duration is the fallback if the document ever lacks
    it); `mix_audio`'s `duration=first` then pins the final output to the
    VO's actual length, matching `render.finish`'s own "footage/VO length is
    authoritative" convention.

    `library` is passed through to both layer builders. Omitting it keeps the
    original all-synthesis behaviour, so a draft render needs no network call
    and no generated assets.

    ``source_audio_bites`` is opt-in. When present, each original excerpt is
    placed on a fourth layer while narration and the music bed are attenuated
    by that bite's ``duck_vo_db``. SFX stay independent. Omitting bites takes
    the exact pre-feature three-input mix path.
    """
    vo_path = Path(vo_path)
    out_path = Path(out_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    style_dir = Path(style_dir)

    duration = float(document.get("duration_seconds") or probe_duration(vo_path))

    windows = silence_windows(document)
    spans = bed_spans(document)
    events = sfx_events(document)
    categories = load_sfx_categories(style_dir / "sfx.json")
    bites = list(source_audio_bites or [])

    sfx_raw, sfx_findings = build_sfx_layer(
        events, duration, work_dir / "sfx-layer.wav", work_dir / "sfx-cache", categories,
        library=library,
    )
    bed_raw, bed_findings = build_bed_layer(
        spans, duration, work_dir / "bed-layer.wav", work_dir / "bed-cache",
        library=library,
    )
    findings = [*sfx_findings, *bed_findings, *_events_inside_silence_findings(events, windows)]

    sfx_ducked = duck(sfx_raw, windows, work_dir / "sfx-ducked.wav")
    bed_ducked = duck(bed_raw, windows, work_dir / "bed-ducked.wav")

    if bites:
        source_layer = build_source_audio_layer(
            bites,
            duration,
            work_dir / "source-audio-layer.wav",
            work_dir / "source-audio-cache",
        )
        vo_for_mix = duck_for_source_audio(
            vo_path, bites, work_dir / "vo-source-ducked.wav"
        )
        bed_for_mix = duck_for_source_audio(
            bed_ducked, bites, work_dir / "bed-source-ducked.wav"
        )
        mix_audio(
            vo_for_mix,
            bed_for_mix,
            sfx_ducked,
            out_path,
            source_audio_path=source_layer,
        )
    else:
        mix_audio(vo_path, bed_ducked, sfx_ducked, out_path)
    return out_path, findings


def _events_inside_silence_findings(events: list[SfxEvent], windows: list[SilenceWindow]) -> list[Finding]:
    """Warn about an SFX cue scheduled inside a silence window.

    The drop has to be total -- `duck` mutes the SFX layer there along with
    the bed, no exceptions -- so a cue placed inside the window can never
    actually be heard. That is very likely a script authoring mistake (the
    cue and the silence marker were probably meant to land on either side of
    each other, not overlap), so it is surfaced rather than silently
    swallowed with no trace.
    """
    findings: list[Finding] = []
    for event in events:
        for window in windows:
            if window.start <= event.seconds < window.end:
                findings.append(
                    Finding(
                        gate="audiomix",
                        severity="warning",
                        message=(
                            f"[SFX:{event.name}] at {event.seconds:.2f}s falls inside "
                            f"the silence window [{window.start:.2f}s, {window.end:.2f}s]; "
                            f"it will be ducked to nothing along with the rest of that "
                            f"drop. Likely an authoring mistake -- move it outside the "
                            f"window if it should be heard."
                        ),
                    )
                )
                break
    return findings

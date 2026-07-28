"""Local synthesis of dark ambient music beds for `[MUSIC:<cue>]` markers.

Same gap as `sfx.py`, different marker: scripts carry `[MUSIC:<cue>]` spans,
`render.py::deferred_audio_cues` warns that nothing exists to play under
them, and no music beds exist anywhere in this repo. This module synthesizes
beds locally with ffmpeg `lavfi` sources, the same approach `plates.py` uses
for atmospheric plates and `sfx.py` uses for the seven SFX cues.

**Be clear about what this is.** The format research in
``docs/dark-documentary-playbook.md`` describes a sustained, slow, low
dark-ambient register. That register is genuinely synthesizable, which is why
this module exists at all; a fast melodic score would not be. These beds are
functional stand-ins for pacing and mixing, not a substitute for licensed
tracks a finished episode needs.

Scripts use evocative cue names lifted from real track titles (`chasms`,
`lurking`, from MONST3R's catalogue) that describe a mood no synthesis can
match to a specific piece of music. `resolve_cue` maps every cue that isn't
`out` to the single generic `drone-low` fallback and says plainly, in its
own docstring, that this is a fallback rather than a match -- and it is
deliberately the *only* place that mapping happens, so a future licensed-
track lookup has one seam to slot into instead of scattered call sites.

Beds sit at a lower peak (-20 dBFS) than SFX cues (-6 dBFS, see
`sfx.TARGET_PEAK_DB`) because a bed plays continuously under narration
rather than punctuating it -- see `test_bed_peak_is_measurably_quieter_
than_an_sfx_cue_of_the_same_length` in tests/test_music.py, which asserts
that headroom is real rather than assumed. Beds also fade in/out over a
short window so they loop at their own boundaries without a click; see
`test_beds_loop_safely_no_click_at_the_boundaries`.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 44100
CHANNELS = 1

BED_KINDS = ("drone-low", "drone-tense", "pulse-slow", "silence")

# Quieter than SFX (-6 dBFS, see sfx.TARGET_PEAK_DB): a bed underlies
# narration continuously, an SFX cue punctuates a single moment.
TARGET_PEAK_DB = -20.0

# The fade window at each end of a bed. Long enough that the fade itself is
# inaudible as a fade (this is a sustained drone, not a hit), short enough
# that it doesn't eat into short test durations -- _fade_time clamps it
# further for very short specs.
_FADE_SECONDS = 1.5

_FUNDAMENTAL_HZ = 55.0  # A1 -- low enough to read as sub-bass, not a note.
_DETUNE_RATIO = 1.012  # ~12 cents sharp: audible slow beating, not a chord.
_DISSONANT_RATIO = 1.42  # a harsh, unresolved interval above the fundamental.
_RING_HZ = 2200.0  # faint high partial for drone-tense's "metallic ringing".
_PULSE_BPM = 45.0  # inside the 40-50 BPM range the spec calls for.


def _run(args: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"ffmpeg failed: {' '.join(args[:4])} ...\n{detail}")
    return result


@dataclass(frozen=True)
class BedSpec:
    kind: str
    duration: float
    sample_rate: int = SAMPLE_RATE


def _fade_time(duration: float) -> float:
    return min(_FADE_SECONDS, duration * 0.4)


def _fade_envelope(duration: float) -> str:
    fade = _fade_time(duration)
    return (
        f"afade=t=in:st=0:d={fade:.6f}:curve=qsin,"
        f"afade=t=out:st={duration - fade:.6f}:d={fade:.6f}:curve=qsin"
    )


def _drone_low_graph(duration: float) -> str:
    # A sustained low fundamental plus a slightly detuned second layer
    # (beats slowly against it) and a very slow amplitude drift -- the
    # CO.AG-style "low sub-bass drone" register, minus the licence.
    f0 = _FUNDAMENTAL_HZ
    f1 = f0 * _DETUNE_RATIO
    return (
        f"sine=frequency={f0}:duration={duration:.6f}:sample_rate={SAMPLE_RATE}[a];"
        f"sine=frequency={f1}:duration={duration:.6f}:sample_rate={SAMPLE_RATE}[b];"
        "[a][b]amix=inputs=2:duration=longest:normalize=0,"
        "tremolo=f=0.1:d=0.25,"
        f"{_fade_envelope(duration)}"
    )


def _drone_tense_graph(duration: float) -> str:
    # drone-low's two layers, plus a dissonant interval (quiet, so it reads
    # as tension rather than a chord) and a faint high sine standing in for
    # "subtle metallic ringing".
    f0 = _FUNDAMENTAL_HZ
    f1 = f0 * _DETUNE_RATIO
    f2 = f0 * _DISSONANT_RATIO
    return (
        f"sine=frequency={f0}:duration={duration:.6f}:sample_rate={SAMPLE_RATE}[a];"
        f"sine=frequency={f1}:duration={duration:.6f}:sample_rate={SAMPLE_RATE}[b];"
        f"sine=frequency={f2}:duration={duration:.6f}:sample_rate={SAMPLE_RATE},volume=0.35[c];"
        f"sine=frequency={_RING_HZ}:duration={duration:.6f}:sample_rate={SAMPLE_RATE},volume=0.05[d];"
        "[a][b][c][d]amix=inputs=4:duration=longest:normalize=0,"
        "tremolo=f=0.12:d=0.3,"
        f"{_fade_envelope(duration)}"
    )


def _pulse_slow_graph(duration: float) -> str:
    # A low drone with a slow rhythmic swell at _PULSE_BPM (converted to
    # Hz), rather than the near-static drift of drone-low/drone-tense.
    hz = _PULSE_BPM / 60.0
    return (
        f"sine=frequency={_FUNDAMENTAL_HZ}:duration={duration:.6f}:sample_rate={SAMPLE_RATE},"
        f"tremolo=f={hz:.6f}:d=0.6,"
        f"{_fade_envelope(duration)}"
    )


_GRAPH_BUILDERS = {
    "drone-low": _drone_low_graph,
    "drone-tense": _drone_tense_graph,
    "pulse-slow": _pulse_slow_graph,
}


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


def _build_silence(spec: BedSpec, out_path: Path) -> Path:
    # True digital silence, matching assemble.py's build_silence: a
    # `[MUSIC:out]` span should be an intentional drop-to-nothing, not room
    # tone standing in for "no bed".
    _run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"anullsrc=r={spec.sample_rate}:cl=mono",
            "-t", f"{spec.duration:.6f}",
            "-ac", str(CHANNELS), "-c:a", "pcm_s16le", str(out_path),
        ]
    )
    return out_path


def build_bed(spec: BedSpec, out_path: Path) -> Path:
    """Synthesize one music bed to a mono WAV."""
    if spec.kind not in BED_KINDS:
        raise ValueError(
            f"Unknown bed kind '{spec.kind}'; valid kinds: {', '.join(BED_KINDS)}"
        )
    if spec.duration <= 0:
        raise ValueError(f"Bed duration must be positive, got {spec.duration}")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    if spec.kind == "silence":
        try:
            return _build_silence(spec, out_path)
        except RuntimeError:
            if out_path.exists():
                out_path.unlink()
            raise

    raw_path = out_path.with_suffix(".raw.wav")
    try:
        graph = _GRAPH_BUILDERS[spec.kind](spec.duration)
        padded_graph = f"{graph},apad=whole_dur={spec.duration:.6f}"
        _run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", padded_graph,
                "-t", f"{spec.duration:.6f}",
                "-ar", str(spec.sample_rate), "-ac", str(CHANNELS),
                "-c:a", "pcm_s16le", str(raw_path),
            ]
        )

        measured_peak = _measure_peak_db(raw_path)
        gain = TARGET_PEAK_DB - measured_peak

        _run(
            [
                "ffmpeg", "-y",
                "-i", str(raw_path),
                "-af", f"volume={gain:.4f}dB",
                "-ar", str(spec.sample_rate), "-ac", str(CHANNELS),
                "-c:a", "pcm_s16le", str(out_path),
            ]
        )
    except RuntimeError:
        if out_path.exists():
            out_path.unlink()
        raise
    finally:
        if raw_path.exists():
            raw_path.unlink()

    return out_path


def resolve_cue(cue: str) -> str:
    """Map a script's [MUSIC:<cue>] name to a bed kind.

    Three cases, and the distinction between the last two matters:

    - `out` maps to true silence.
    - A cue that *names a bed kind this module implements* passes straight
      through. `[MUSIC:drone-tense]` is a request for the tense bed, and
      `_drone_tense_graph` exists to serve it; collapsing it into `drone-low`
      would silently discard a deliberate editorial choice. An earlier version
      of this function did exactly that, which meant an episode asking for
      tension got the neutral bed with nothing reported.
    - Any other name -- including evocative ones lifted from real (licensed)
      track titles, like `chasms` or `lurking` -- falls back to `drone-low`,
      because no synthesis here can tell those titles apart or match their
      mood.

    This is deliberately the only place the mapping happens, so a future
    licensed-track lookup has one seam to replace instead of call sites
    scattered through the codebase.
    """
    if cue == "out":
        return "silence"
    if cue in _GRAPH_BUILDERS:
        return cue
    return "drone-low"


def exact_cue_names() -> frozenset[str]:
    """Cue names `resolve_cue` maps exactly, rather than falling back on.

    Exported so callers that need to report fallbacks (`render.deferred_audio_cues`)
    can ask this module instead of restating its mapping. A second copy of that
    knowledge is how the two drift apart -- and the drift is silent, because a
    wrong answer here produces a misleading warning rather than a failure.
    """
    return frozenset({"out", *_GRAPH_BUILDERS})

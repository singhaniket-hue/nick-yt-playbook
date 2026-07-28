"""Local synthesis of the seven SFX cues named in `style/sfx.json`.

`style/sfx.json` is a registry of cue *names* -- what each one is for and how
often it should fire -- not audio files. Nothing in this repo could actually
play a `[SFX:sub-drop]` marker until now. This module closes that gap the
same way `plates.py` closes the missing-footage gap: by synthesizing locally
with ffmpeg's `lavfi` sources, so the output carries no third-party rights
claim and costs nothing to render.

**Be clear about what this is.** These are functional stand-ins, not finished
sound design. The intended register is documented in
``docs/dark-documentary-playbook.md``. A synthesized burst of band-limited
noise makes the timeline audibly complete for editing and pacing; it is not a
substitute for a licensed hit and should not be mistaken for one in a final
mix.

Each cue is a short filter chain built from `lavfi` sources (`anoisesrc`,
`sine`, `aevalsrc`) and filters (`highpass`/`lowpass`/`bandpass`, `tremolo`,
`afade`). Noise-based cues use a fixed `anoisesrc` seed (see
`_NOISE_SEEDS`) rather than the filter's default random seed, so the same
spec renders byte-identical output on every run -- required for the
duration/peak assertions in tests/test_sfx.py to be stable, and generally
the right default for anything that might get checked into or diffed
against a rendered asset later. If a future cue genuinely wants per-render
variation that would need to be an opt-in, not the default.

Peak normalisation is two-pass, the same approach `plates.py` documents
choosing for bitrate: render the raw filter chain once, *measure* its peak
with `volumedetect` rather than assuming a filter got it to the target, then
render again through a `volume=<gain>dB` computed from that measurement.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 44100
CHANNELS = 1

SFX_NAMES = (
    "vhs-burst",
    "static-crackle",
    "sub-drop",
    "bass-thud",
    "glitch-sting",
    "metal-scrape",
    "heartbeat",
)

DEFAULT_DURATIONS = {
    "vhs-burst": 0.3,
    "static-crackle": 0.6,
    "sub-drop": 1.2,
    "bass-thud": 0.4,
    "glitch-sting": 0.5,
    "metal-scrape": 1.0,
    "heartbeat": 1.8,
}

# Cues sit at roughly -6 dBFS peak so they read clearly against narration
# without ever threatening to clip once mixed. -6 (not 0) leaves headroom
# for the mixdown; -6 (not -12 or quieter) keeps a "sting" reading as a
# sting rather than a background texture -- that's `music.py`'s job, at its
# own quieter -20 dBFS target.
TARGET_PEAK_DB = -6.0

# anoisesrc's default seed is -1 ("random"): two renders of the same spec
# would differ byte-for-byte. Fixed per-cue seeds make build_sfx
# deterministic instead, matching every other generator in this package.
_NOISE_SEEDS = {
    "vhs-burst": 101,
    "static-crackle": 102,
    "glitch-sting": 103,
    "metal-scrape": 104,
}

# Heartbeat cadence: two thuds ("lub", "dub") per cycle, not one. 90 BPM
# lands inside the 80-100 BPM range the spec calls for; dub_offset places
# the second thud shortly after the first the way an actual S1/S2 pair
# reads, rather than splitting the cycle evenly in two.
_HEARTBEAT_BPM = 90.0
_HEARTBEAT_DUB_OFFSET_FRAC = 0.28
_HEARTBEAT_THUD_DURATION = 0.14
_HEARTBEAT_FREQUENCY = 68.0


def _run(args: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"ffmpeg failed: {' '.join(args[:4])} ...\n{detail}")
    return result


@dataclass(frozen=True)
class SfxSpec:
    name: str
    duration: float
    sample_rate: int = SAMPLE_RATE


def _attack_time(duration: float, preferred: float) -> float:
    """Clamp an envelope's attack so it always leaves room for a decay,
    however short `duration` is (tests render cues far shorter than their
    real-world default)."""
    return min(preferred, duration * 0.2)


def _envelope(duration: float, attack: float, curve: str = "exp") -> str:
    attack = _attack_time(duration, attack)
    decay = duration - attack
    return (
        f"afade=t=in:st=0:d={attack:.6f}:curve=qsin,"
        f"afade=t=out:st={attack:.6f}:d={decay:.6f}:curve={curve}"
    )


def _vhs_burst_graph(duration: float) -> str:
    # Band-limited noise, fast attack, harsh short decay -- a visual-cut
    # texture rather than a musical tone. Narrower/harsher band and faster
    # default duration than static-crackle is what makes it read as a
    # "burst" rather than a "crackle".
    seed = _NOISE_SEEDS["vhs-burst"]
    return (
        f"anoisesrc=color=white:duration={duration:.6f}:sample_rate={SAMPLE_RATE}:seed={seed},"
        "highpass=f=600,lowpass=f=9000,"
        f"{_envelope(duration, attack=0.005)}"
    )


def _static_crackle_graph(duration: float) -> str:
    # Same idea as vhs-burst but a wider/softer band, a slower decay, and a
    # tremolo riding on top so it reads as a crackle texture (a screenshot
    # reveal) rather than a single hit.
    seed = _NOISE_SEEDS["static-crackle"]
    return (
        f"anoisesrc=color=white:duration={duration:.6f}:sample_rate={SAMPLE_RATE}:seed={seed},"
        "highpass=f=300,lowpass=f=7000,"
        "tremolo=f=45:d=0.85,"
        f"{_envelope(duration, attack=0.02)}"
    )


def _sub_drop_graph(duration: float) -> str:
    # A sine sweeping 90Hz -> 25Hz over the full cue. The instantaneous
    # phase is the integral of frequency over time (2*pi*(f0*t +
    # (f1-f0)*t^2/(2*T))), not sin(2*pi*f(t)*t) -- the naive form would
    # introduce a phase discontinuity as f(t) changes.
    f0, f1 = 90.0, 25.0
    expr = f"sin(2*PI*({f0}*t+({f1}-{f0})*t*t/(2*{duration:.6f})))"
    return (
        f"aevalsrc=exprs='{expr}':sample_rate={SAMPLE_RATE}:duration={duration:.6f},"
        f"{_envelope(duration, attack=0.015)}"
    )


def _bass_thud_graph(duration: float) -> str:
    # A short low sine (60Hz, a kick-drum-ish fundamental) with a very fast
    # decay -- chapter punctuation, not a tone.
    return (
        f"sine=frequency=60:duration={duration:.6f}:sample_rate={SAMPLE_RATE},"
        f"{_envelope(duration, attack=0.003)}"
    )


def _glitch_sting_graph(duration: float) -> str:
    # Noise with rapid amplitude modulation (tremolo), high-passed hard so
    # it reads as digital chatter rather than rumble -- the opposite
    # spectral character from sub-drop, which is the point of the
    # low-vs-high test in tests/test_sfx.py.
    seed = _NOISE_SEEDS["glitch-sting"]
    return (
        f"anoisesrc=color=white:duration={duration:.6f}:sample_rate={SAMPLE_RATE}:seed={seed},"
        "highpass=f=2500,"
        "tremolo=f=28:d=0.95,"
        f"{_envelope(duration, attack=0.003)}"
    )


def _metal_scrape_graph(duration: float) -> str:
    # Two closely-spaced resonant band-pass filters over noise, plus a slow
    # tremolo for an irregular "drag" motion. This is the hardest of the
    # seven to synthesize -- see the report for an honest assessment of how
    # close it actually gets to metal scraping versus just filtered noise.
    seed = _NOISE_SEEDS["metal-scrape"]
    return (
        f"anoisesrc=color=white:duration={duration:.6f}:sample_rate={SAMPLE_RATE}:seed={seed},"
        "bandpass=f=2800:width_type=q:w=9,"
        "bandpass=f=3400:width_type=q:w=12,"
        "tremolo=f=6:d=0.5,"
        f"{_envelope(duration, attack=0.03)}"
    )


def _heartbeat_graph(duration: float) -> str:
    # Two low thuds per cycle (lub, dub) at _HEARTBEAT_BPM, laid out as
    # individually-enveloped sine bursts delayed into place and mixed --
    # not one giant hand-written envelope expression, so the cadence stays
    # readable and adjustable. Cycles are generated until they run past
    # `duration`; apad/-t (applied by the caller) trims/pads the tail, so a
    # cue cut short mid-thud degrades gracefully instead of erroring.
    cycle = 60.0 / _HEARTBEAT_BPM
    dub_offset = cycle * _HEARTBEAT_DUB_OFFSET_FRAC
    onsets = [0.0]
    i = 0
    while True:
        cycle_start = i * cycle
        if cycle_start >= duration:
            break
        dub = cycle_start + dub_offset
        if dub < duration:
            onsets.append(dub)
        i += 1
        next_start = i * cycle
        if next_start < duration:
            onsets.append(next_start)
    onsets = sorted(set(onsets))

    thud_dur = min(_HEARTBEAT_THUD_DURATION, duration * 0.5) or 0.01
    parts = []
    labels = []
    for index, onset in enumerate(onsets):
        delay_ms = max(0, round(onset * 1000))
        label = f"h{index}"
        parts.append(
            f"sine=frequency={_HEARTBEAT_FREQUENCY}:duration={thud_dur:.6f}:sample_rate={SAMPLE_RATE},"
            f"{_envelope(thud_dur, attack=0.004)},"
            f"adelay={delay_ms}|{delay_ms}[{label}]"
        )
        labels.append(f"[{label}]")

    mix = "".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0"
    return ";".join(parts) + ";" + mix


_GRAPH_BUILDERS = {
    "vhs-burst": _vhs_burst_graph,
    "static-crackle": _static_crackle_graph,
    "sub-drop": _sub_drop_graph,
    "bass-thud": _bass_thud_graph,
    "glitch-sting": _glitch_sting_graph,
    "metal-scrape": _metal_scrape_graph,
    "heartbeat": _heartbeat_graph,
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


def build_sfx(spec: SfxSpec, out_path: Path) -> Path:
    """Synthesize one cue to a mono WAV, peak-normalised to TARGET_PEAK_DB."""
    if spec.name not in SFX_NAMES:
        raise ValueError(
            f"Unknown SFX cue '{spec.name}'; valid cues: {', '.join(SFX_NAMES)}"
        )
    if spec.duration <= 0:
        raise ValueError(f"SFX duration must be positive, got {spec.duration}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path = out_path.with_suffix(".raw.wav")

    try:
        graph = _GRAPH_BUILDERS[spec.name](spec.duration)
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

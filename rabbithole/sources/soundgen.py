"""Real sound assets from the ElevenLabs sound-generation API.

`sfx.py` and `music.py` synthesize cues locally with ffmpeg and both say
plainly, in their own module docstrings, that the result is a functional
stand-in rather than sound design. This module is the upgrade path: the same
cue names, generated as actual recorded-sounding audio, cached to disk with
their provenance recorded.

**Why this is worth a network call.** The synthesized `metal-scrape` was the
clearest failure -- measured spectral flatness 0.0399, meaning energy spread
broadly across the spectrum, which is what "filtered noise" looks like.
Generated, the same cue measures 0.0005: energy concentrated into resonant
peaks, which is what makes a sound read as *metal* rather than as noise
shaped by a band-pass filter. Both cues' brightness trajectories are equally
coherent (lag-1 autocorrelation 0.97 vs 0.91), so movement was never the
differentiator and it would be wrong to claim it was -- the gain is spectral
structure, and that is the specific thing static filters over noise cannot
produce.

**API constraints, measured against the live endpoint rather than assumed:**

- `duration_seconds` must be in [0.5, 30.0]. A bed for a twelve-minute span
  therefore cannot be generated in one call; it is tiled (see
  `tile_to_duration`).
- `eleven_text_to_sound_v2` is the only accepted `model_id`.
- Billing is roughly 11 characters per generated second, drawn from the same
  quota as text-to-speech but reported on a lag, so a generation's cost does
  not appear immediately in `/v1/user/subscription`.
- **The endpoint ignores unknown fields and still returns 200.** A bogus key
  is accepted silently, so a 200 is not evidence that a parameter was
  honoured. `loop` was verified behaviourally instead: it improves the
  end-to-start seam by ~18 dB (-21.2 -> -39.4 dBFS) but leaves it ~15 dB
  worse than an ordinary interior splice, so it is genuinely doing something
  and is genuinely not seamless. `tile_to_duration` crossfades regardless
  rather than trusting it.

Caching is by content identity, not by call order: a cue is regenerated only
if its slug, prompt, duration or variant changed. That keeps re-renders free
and makes a spend deliberate. `manifest.json` records the prompt and model
behind every file so a finished mix can be traced back to what produced it --
the same obligation `provenance.py` enforces for footage.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import re
import shutil
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rabbithole.sources.music import TARGET_PEAK_DB as BED_TARGET_PEAK_DB
from rabbithole.sources.sfx import TARGET_PEAK_DB as SFX_TARGET_PEAK_DB
from rabbithole.jsonio import read_json

API_URL = "https://api.elevenlabs.io/v1/sound-generation"
MODEL_ID = "eleven_text_to_sound_v2"

# Enforced by the API; see the module docstring. Requesting outside this range
# is rejected at validation before any audio is produced.
MIN_DURATION = 0.5
MAX_DURATION = 30.0

# How literally the model follows the prompt. 0.6 keeps the negative guidance
# ("no music") effective without making short cues sound synthetic.
DEFAULT_PROMPT_INFLUENCE = 0.6

# Beds are generated at the API ceiling: fewer, longer tiles mean fewer
# crossfade seams across a long span.
BED_VARIANT_SECONDS = 30.0

# Distinct generations per bed kind. A 37-minute episode with three bed spans
# would otherwise repeat a single 30s loop ~75 times, which is audible as a
# loop however good the crossfade is. Four variants give two minutes of
# unique material per kind before anything repeats.
BED_VARIANTS = 4

# Crossfade between bed tiles. Long enough to be inaudible under a sustained
# drone, short enough not to eat a short span whole (_crossfade_for clamps it).
BED_CROSSFADE_SECONDS = 2.0

# Seconds discarded from each end of a generated bed before it is used.
#
# The model does not hold a steady level across a full 30 seconds: measured
# against its own middle, one variant ran +8.99 dB over its last two seconds
# and another -7.30 dB under its first two. Those excursions sit exactly where
# tiling joins one variant to the next, so they -- not the crossfade -- are
# what makes a long tiled bed pump. Trimming to the stable interior is the fix;
# loudness-matching alone left a 3.31 dB step at joins against a 2.25 dB
# baseline, because matching whole-file RMS cannot flatten a contour *within*
# a file.
BED_EDGE_TRIM_SECONDS = 2.5

SAMPLE_RATE = 44100
CHANNELS = 1

Transport = Callable[[str, dict, dict], tuple[int, bytes]]


def _requests_transport(url: str, headers: dict, json_body: dict) -> tuple[int, bytes]:
    import requests

    response = requests.post(url, headers=headers, json=json_body, timeout=300)
    return response.status_code, response.content


def _run(args: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"ffmpeg failed: {' '.join(args[:4])} ...\n{detail}")
    return result


@dataclass(frozen=True)
class SoundRequest:
    """One generation. `variant` distinguishes repeat draws on the same prompt."""

    slug: str
    prompt: str
    duration: float
    variant: int = 0
    prompt_influence: float = DEFAULT_PROMPT_INFLUENCE
    loop: bool = False

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError(f"SoundRequest {self.slug!r} has an empty prompt")
        if not (MIN_DURATION <= self.duration <= MAX_DURATION):
            raise ValueError(
                f"SoundRequest {self.slug!r} duration {self.duration} is outside the "
                f"API's accepted range [{MIN_DURATION}, {MAX_DURATION}] seconds. "
                f"Longer material must be tiled, not requested in one call."
            )

    @property
    def fingerprint(self) -> str:
        """Content identity: changes only if something affecting the audio changed.

        `variant` is included so repeat draws on one prompt cache separately;
        without it every variant would collide on the same key and a bed would
        get one generation copied four times.
        """
        payload = json.dumps(
            {
                "slug": self.slug,
                "prompt": self.prompt,
                "duration": round(self.duration, 3),
                "variant": self.variant,
                "prompt_influence": round(self.prompt_influence, 3),
                "loop": self.loop,
                "model": MODEL_ID,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load_prompts(path: Path) -> dict[str, dict[str, str]]:
    """Read style/sound-prompts.json, dropping the `_comment` block."""
    data = read_json(Path(path))
    return {
        "sfx": dict(data.get("sfx", {})),
        "beds": dict(data.get("beds", {})),
    }


def generate(request: SoundRequest, api_key: str, transport: Transport | None = None) -> bytes:
    """Generate one cue, returning mp3 bytes.

    `transport` is injected so tests never reach the network -- the same
    convention `narrate.synthesize` uses.
    """
    send = transport or _requests_transport
    body = {
        "text": request.prompt,
        "duration_seconds": round(request.duration, 3),
        "prompt_influence": request.prompt_influence,
        "model_id": MODEL_ID,
        "loop": request.loop,
    }

    status, payload = send(
        API_URL,
        {"xi-api-key": api_key, "Content-Type": "application/json"},
        body,
    )

    if status != 200:
        detail = payload.decode("utf-8", errors="replace")[:400]
        raise RuntimeError(
            f"ElevenLabs sound-generation returned {status} for {request.slug!r}: {detail}"
        )
    if not payload:
        raise RuntimeError(
            f"ElevenLabs sound-generation returned 200 but no audio for {request.slug!r}"
        )
    return payload


# --- the on-disk library ---------------------------------------------------------


@dataclass(frozen=True)
class Library:
    """A directory of generated cues plus the manifest describing them."""

    root: Path

    @property
    def manifest_path(self) -> Path:
        return Path(self.root) / "manifest.json"

    def sfx_path(self, name: str) -> Path:
        return Path(self.root) / "sfx" / f"{name}.wav"

    def bed_path(self, kind: str, variant: int) -> Path:
        return Path(self.root) / "beds" / f"{kind}-{variant:02d}.wav"

    def bed_raw_path(self, kind: str, variant: int) -> Path:
        """Where the unnormalised generation is kept.

        Beds are loudness-matched as a *group* (see `normalize_bed_group`), so
        the normalisation cannot be baked in at generation time -- a second
        `sound build` would re-apply gain to an already-adjusted file and the
        group would drift further every run. Keeping the raw generation makes
        normalisation a pure function of it: cache the expensive irreproducible
        step, recompute the cheap deterministic one.
        """
        return Path(self.root) / "beds" / "raw" / f"{kind}-{variant:02d}.wav"

    def read_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {"entries": {}}
        return read_json(self.manifest_path)

    def write_manifest(self, manifest: dict) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def has(self, key: str, fingerprint: str) -> bool:
        """True only if the file exists *and* was made from this exact request."""
        entry = self.read_manifest().get("entries", {}).get(key)
        if not entry or entry.get("fingerprint") != fingerprint:
            return False
        return (Path(self.root) / entry["path"]).exists()

    def record(self, key: str, path: Path, request: SoundRequest) -> None:
        manifest = self.read_manifest()
        manifest.setdefault("entries", {})[key] = {
            "path": Path(path).relative_to(self.root).as_posix(),
            "fingerprint": request.fingerprint,
            "slug": request.slug,
            "prompt": request.prompt,
            "duration_seconds": request.duration,
            "variant": request.variant,
            "prompt_influence": request.prompt_influence,
            "loop": request.loop,
            "model_id": MODEL_ID,
            "api": API_URL,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        self.write_manifest(manifest)


def measure_rms_db(path: Path) -> float:
    """Mean-square level, which is what a sustained bed's loudness tracks."""
    with wave.open(str(path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
        width = handle.getsampwidth()
    if width != 2:
        raise RuntimeError(f"expected 16-bit PCM for RMS measurement, got {width * 8}-bit: {path}")
    samples = array.array("h")
    samples.frombytes(frames)
    if not samples:
        raise RuntimeError(f"no samples to measure in {path}")
    total = math.fsum(float(s) * float(s) for s in samples)
    rms = math.sqrt(total / len(samples)) / 32768.0
    return 20 * math.log10(rms + 1e-12)


def trim_edges(src: Path, dst: Path, trim_seconds: float) -> Path:
    """Copy `src` to `dst` with `trim_seconds` removed from each end.

    Used on generated beds before tiling; see `BED_EDGE_TRIM_SECONDS` for why
    the edges are the problem. Refuses to trim away more than half the source,
    so a short input degrades to a smaller trim rather than to nothing.
    """
    src, dst = Path(src), Path(dst)
    total = probe_duration(src)
    trim = max(0.0, min(trim_seconds, total / 4.0))
    kept = total - 2 * trim
    if kept <= 0:
        raise RuntimeError(f"trimming {trim_seconds}s from each end of {total}s leaves nothing")

    dst.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-ss", f"{trim:.6f}", "-t", f"{kept:.6f}", "-i", str(src),
            "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
            "-c:a", "pcm_s16le", str(dst),
        ]
    )
    return dst


def normalize_bed_group(paths: list[Path], target_peak_db: float) -> list[Path]:
    """Loudness-match bed variants to each other, then cap the group's peak.

    Peak-normalising each variant independently is wrong for beds, and the
    failure is specific: measured across the generated set, variants with
    identical -20 dBFS peaks differed by up to 11.65 dB in RMS, because their
    crest factors ranged from 3.3 to 18.1 dB. Tiling then crossfades between
    two genuinely different loudnesses, which reads as the bed pumping every
    time a variant changes -- a 3.71 dB mean level step at joins against
    2.25 dB elsewhere in a twelve-minute tiling. (The first thing suspected was
    the crossfade curve; switching linear to constant-power sine changed the
    figure by 0.03 dB, which ruled it out.)

    So: match RMS across the group, then apply one uniform gain so the loudest
    peak lands on `target_peak_db`. The uniform second step preserves the
    loudness match while keeping the peak contract `audiomix` relies on --
    every variant sits at or below the target, none above.

    Sustained material is loudness-normalised and transient material is
    peak-normalised; that split is why SFX cues keep per-cue peak
    normalisation and beds do not.
    """
    if not paths:
        return []

    rms = [measure_rms_db(p) for p in paths]
    reference = max(rms)  # match up to the loudest; never boost past it later
    matched_peaks = []
    for path, level in zip(paths, rms):
        gain = reference - level
        _apply_gain(path, gain)
        matched_peaks.append(measure_peak_db(path))

    group_gain = target_peak_db - max(matched_peaks)
    for path in paths:
        _apply_gain(path, group_gain)
    return paths


def _apply_gain(path: Path, gain_db: float) -> None:
    """Apply `gain_db` to `path` in place (via a temp file ffmpeg can write)."""
    if abs(gain_db) < 1e-6:
        return
    tmp = Path(path).with_suffix(".gain.wav")
    _run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(path),
            "-af", f"volume={gain_db:.4f}dB",
            "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
            "-c:a", "pcm_s16le", str(tmp),
        ]
    )
    tmp.replace(path)


def measure_peak_db(path: Path) -> float:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
    )
    text = result.stderr.decode("utf-8", errors="replace")
    match = re.search(r"max_volume:\s*(-?\d+\.?\d*) dB", text)
    if not match:
        raise RuntimeError(f"volumedetect produced no max_volume for {path}:\n{text[-800:]}")
    return float(match.group(1))


def _mp3_to_wav(
    mp3_bytes: bytes,
    out_path: Path,
    work_dir: Path,
    target_peak_db: float | None = None,
) -> Path:
    """Decode to the mono 44.1k pcm_s16le every other layer in the mix speaks.

    `target_peak_db` matters more than it looks. `audiomix` derives its mix
    levels from cue *category* on the stated assumption that a cue arrives
    peak-normalised -- SFX at `sfx.TARGET_PEAK_DB` (-6 dBFS), beds at
    `music.TARGET_PEAK_DB` (-20 dBFS). Generated audio arrives at whatever
    level the model produced, which is not that. Dropping it into the library
    unnormalised would leave every category gain quietly wrong: an `impact`
    cue mixed at -6 dB on top of an already-hot generation clips, a quiet one
    vanishes under the narration. Normalising here keeps that contract true
    whatever the source, so `audiomix` needs no knowledge of where a cue came
    from.

    Two-pass measure-then-apply, matching `sfx.py` and `music.py`: render,
    *measure* the actual peak, then apply a computed gain -- rather than
    assuming a filter landed on target.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    tmp = work_dir / f"{out_path.stem}.src.mp3"
    tmp.write_bytes(mp3_bytes)
    decoded = work_dir / f"{out_path.stem}.decoded.wav"
    try:
        _run(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(tmp),
                "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
                "-c:a", "pcm_s16le", str(decoded),
            ]
        )

        if target_peak_db is None:
            shutil.copy(decoded, out_path)
            return out_path

        gain = target_peak_db - measure_peak_db(decoded)
        _run(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(decoded),
                "-af", f"volume={gain:.4f}dB",
                "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
                "-c:a", "pcm_s16le", str(out_path),
            ]
        )
    finally:
        for path in (tmp, decoded):
            if path.exists():
                path.unlink()
    return out_path


def ensure_sound(
    key: str,
    out_path: Path,
    request: SoundRequest,
    library: Library,
    api_key: str,
    transport: Transport | None = None,
    work_dir: Path | None = None,
    target_peak_db: float | None = None,
) -> tuple[Path, bool]:
    """Return (path, generated). Generates only if the cache misses.

    A cache hit requires the manifest fingerprint to match, so editing a
    prompt in style/sound-prompts.json invalidates exactly the cues that
    prompt produced and nothing else.
    """
    if library.has(key, request.fingerprint):
        return Path(out_path), False

    audio = generate(request, api_key, transport=transport)
    _mp3_to_wav(
        audio,
        out_path,
        work_dir or Path(library.root) / ".work",
        target_peak_db=target_peak_db,
    )
    library.record(key, out_path, request)
    return Path(out_path), True


# --- tiling ----------------------------------------------------------------------


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True,
    )
    text = result.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        raise RuntimeError(f"ffprobe reported no duration for {path}")
    return float(text)


def _crossfade_for(tile_seconds: float, requested: float) -> float:
    """Clamp the crossfade so it can never exceed what the material allows.

    `acrossfade` consumes `d` seconds from *both* sides of a join, so a
    crossfade longer than half a tile would consume the tile entirely. Very
    short spans clamp further still.
    """
    return max(0.05, min(BED_CROSSFADE_SECONDS, tile_seconds / 3.0, requested / 3.0))


def tile_to_duration(
    sources: list[Path],
    duration: float,
    out_path: Path,
    crossfade: float | None = None,
) -> Path:
    """Crossfade `sources` end to end, cycling them, until `duration` is covered.

    Chained `acrossfade` in a single ffmpeg graph rather than one subprocess
    per join: a twelve-minute span needs dozens of tiles, and dozens of
    process launches is the slowest possible way to build one file.

    Each join consumes `crossfade` seconds of overlap, so N tiles of length L
    cover `N*L - (N-1)*crossfade`, not `N*L`. Getting that wrong would leave
    a span short and let silence show through under the narration.

    **The fade curve is `qsin`, not `tri`, and that is not cosmetic.** A linear
    (triangular) crossfade holds both signals at 0.5 amplitude at the midpoint;
    for *uncorrelated* material -- which two different drone variants are --
    the powers add rather than the amplitudes, giving 0.25 + 0.25 = 0.5, a
    3 dB power dip at every join. Measured across a twelve-minute tiling that
    showed up as a mean level step of 3.71 dB at joins against 2.25 dB
    elsewhere: audible pumping every 28 seconds. Quarter-sine curves are
    constant-power (sin^2 + cos^2 = 1), which removes the dip.
    """
    if not sources:
        raise ValueError("tile_to_duration needs at least one source")
    if duration <= 0:
        raise ValueError(f"tile_to_duration needs a positive duration, got {duration}")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tile_seconds = min(probe_duration(p) for p in sources)
    fade = crossfade if crossfade is not None else _crossfade_for(tile_seconds, duration)
    fade = min(fade, tile_seconds / 2.0 - 0.01)

    advance = tile_seconds - fade
    if advance <= 0:
        raise RuntimeError(
            f"crossfade {fade:.3f}s leaves no forward progress on {tile_seconds:.3f}s tiles"
        )

    tiles_needed = max(1, math.ceil((duration - tile_seconds) / advance) + 1)
    picks = [Path(sources[i % len(sources)]) for i in range(tiles_needed)]

    inputs: list[str] = []
    for path in picks:
        inputs += ["-i", str(path)]

    if len(picks) == 1:
        chain = f"[0:a]atrim=0:{duration:.6f},apad=whole_dur={duration:.6f}[out]"
    else:
        parts = []
        current = "[0:a]"
        for index in range(1, len(picks)):
            label = f"[x{index}]"
            parts.append(f"{current}[{index}:a]acrossfade=d={fade:.6f}:c1=qsin:c2=qsin{label}")
            current = label
        parts.append(f"{current}atrim=0:{duration:.6f},apad=whole_dur={duration:.6f}[out]")
        chain = ";".join(parts)

    _run(
        [
            "ffmpeg", "-y", "-v", "error", *inputs,
            "-filter_complex", chain,
            "-map", "[out]",
            "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
            "-c:a", "pcm_s16le", str(out_path),
        ]
    )
    return out_path


# --- high-level build ------------------------------------------------------------


def build_library(
    library: Library,
    prompts: dict[str, dict[str, str]],
    api_key: str,
    sfx_durations: dict[str, float],
    bed_kinds: tuple[str, ...] = (),
    bed_variants: int = BED_VARIANTS,
    transport: Transport | None = None,
    on_progress: Callable[[str, bool], None] | None = None,
) -> dict[str, int]:
    """Generate every cue and bed variant the style pack names.

    Returns counts so a caller can report what it actually spent versus what
    it reused. Nothing is regenerated on a cache hit.
    """
    made = 0
    reused = 0

    for name, prompt in sorted(prompts.get("sfx", {}).items()):
        duration = float(sfx_durations.get(name, 1.0))
        duration = max(MIN_DURATION, min(MAX_DURATION, duration))
        request = SoundRequest(slug=f"sfx/{name}", prompt=prompt, duration=duration)
        path, generated = ensure_sound(
            f"sfx/{name}", library.sfx_path(name), request, library, api_key,
            transport=transport, target_peak_db=SFX_TARGET_PEAK_DB,
        )
        made += int(generated)
        reused += int(not generated)
        if on_progress:
            on_progress(f"sfx/{name}", generated)

    for kind in bed_kinds:
        prompt = prompts.get("beds", {}).get(kind)
        if not prompt:
            continue

        raw_paths: list[Path] = []
        for variant in range(bed_variants):
            request = SoundRequest(
                slug=f"beds/{kind}",
                prompt=prompt,
                duration=BED_VARIANT_SECONDS,
                variant=variant,
                loop=True,
            )
            key = f"beds/{kind}/{variant}"
            # Raw, unnormalised: the group normalisation below is what sets level.
            raw_path, generated = ensure_sound(
                key, library.bed_raw_path(kind, variant), request, library, api_key,
                transport=transport, target_peak_db=None,
            )
            raw_paths.append(raw_path)
            made += int(generated)
            reused += int(not generated)
            if on_progress:
                on_progress(key, generated)

        # Derived every run from the cached raws, so it is idempotent and a
        # newly added variant re-levels the whole group rather than joining it
        # at a mismatched loudness.
        playable: list[Path] = []
        for variant, raw_path in enumerate(raw_paths):
            target = library.bed_path(kind, variant)
            target.parent.mkdir(parents=True, exist_ok=True)
            trim_edges(raw_path, target, BED_EDGE_TRIM_SECONDS)
            playable.append(target)
        normalize_bed_group(playable, BED_TARGET_PEAK_DB)

    return {"generated": made, "reused": reused}


def library_sfx(library: Library, name: str) -> Path | None:
    """The generated cue for `name`, or None if the library has no usable copy."""
    path = library.sfx_path(name)
    entry = library.read_manifest().get("entries", {}).get(f"sfx/{name}")
    return path if entry and path.exists() else None


def library_bed_variants(library: Library, kind: str) -> list[Path]:
    """Every generated variant for `kind`, in variant order. May be empty."""
    entries = library.read_manifest().get("entries", {})
    out: list[Path] = []
    for variant in range(BED_VARIANTS):
        if f"beds/{kind}/{variant}" in entries:
            path = library.bed_path(kind, variant)
            if path.exists():
                out.append(path)
    return out

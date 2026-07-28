"""Atmospheric plate generation: the ~15% "dark atmospheric motion graphics"
connective tissue between archival footage and screenshots.

This module renders plates locally with ffmpeg's `lavfi` sources and filters,
so the output carries no third-party rights claim at all.

**Boundary, stated plainly:** ffmpeg's `lavfi` sources can generate *textures
and overlays* — grain, scanlines, vignette, static, flat colour fields. They
cannot generate *scenes*. A "dark corridor" or "abandoned room" plate needs a
generative video model, which costs money and is out of scope for this
module. Do not wire this module up to a slot that actually needs a scene.

Every plate is driven by the `grade` values in the active style pack's
`palette.json` (`grain_strength`, `vignette_strength`, `scanline_opacity`),
so retuning the style pack retunes the plates without touching this code.
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rabbithole.config import REPO_ROOT
from rabbithole.jsonio import read_json
from rabbithole.encoding import CPU_ENCODER, video_args

PLATE_KINDS = ("black", "grain", "scanlines", "vignette", "static")

# Noise is near-incompressible, so CRF alone does not bound the output: a 12s
# static plate reached 311 MB at x264 defaults. A capped bitrate does bound it
# (measured 30x smaller at 6 Mbit/s) and costs nothing that matters, because a
# plate is composited texture rather than content whose exact pixels are meaningful.
# Do NOT add -tune grain here; it preserves noise and made output larger.
#
# PLATE_CRF is 23, not the naive "just raise CRF" instinct of 28-32: measured
# at full HD, noisy plates are so incompressible that -maxrate/-bufsize is what
# actually bounds the bitrate (crf 18 through 30 all land ~6.4-7.4 Mbit/s once
# capped -- CRF barely moves the needle once the cap binds). At the small
# dimensions this package's own tests render at, though, CRF *does* still
# matter: crf=30 quantizes a 320x180 grain plate hard enough that the first
# frame's luma range drops from ~28 to ~12, under the 15 threshold
# `test_grain_produces_visible_variance_and_black_does_not` already asserts.
# crf=23 keeps that test's margin (measured luma range ~28) while the capped
# bitrate at full HD is unchanged in every way that matters.
PLATE_CRF = 23
PLATE_MAXRATE = "6M"
PLATE_BUFSIZE = "12M"

# "static" has no dedicated grade key of its own -- it is a much heavier dose
# of the same `noise` filter that drives `grain`, scaled off grain_strength so
# retuning the style pack's grain still moves it. 5x turns the default 0.18
# grain_strength (alls=18, subtle) into alls=90 (heavy, VHS-style).
_STATIC_MULTIPLIER = 5.0

_DEFAULT_STYLE_DIR = REPO_ROOT / "style"
_DEFAULT_BACKDROP = ["#000000", "#202020"]


@dataclass(frozen=True)
class PlateSpec:
    kind: str
    duration: float
    width: int = 1920
    height: int = 1080
    fps: int = 30


def _run(args: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"ffmpeg failed: {' '.join(args[:4])} ...\n{detail}")
    return result


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _to_ffmpeg_color(hex_str: str) -> str:
    return "0x" + hex_str.lstrip("#")


def _load_palette(style_dir: Path) -> dict:
    palette_path = style_dir / "palette.json"
    if not palette_path.exists():
        raise RuntimeError(f"Style pack palette not found: {palette_path}")
    return read_json(palette_path)


def load_grade(style_dir: Path) -> dict:
    """Grade parameters from the style pack's palette.json."""
    palette_path = style_dir / "palette.json"
    data = _load_palette(style_dir)
    grade = data.get("grade")
    if grade is None:
        raise RuntimeError(f"palette.json has no 'grade' key: {palette_path}")
    return grade


def _load_backdrop(style_dir: Path) -> list[str]:
    try:
        data = _load_palette(style_dir)
    except RuntimeError:
        return _DEFAULT_BACKDROP
    backdrop = data.get("backdrop")
    return backdrop if backdrop else _DEFAULT_BACKDROP


def _build_filtergraph(spec: PlateSpec, grade: dict, backdrop: list[str]) -> str:
    duration = f"{spec.duration:.6f}"
    dims = f"{spec.width}x{spec.height}"

    if spec.kind == "black":
        color = _to_ffmpeg_color(backdrop[0])
        return f"color=c={color}:s={dims}:r={spec.fps}:d={duration}"

    # Every other plate kind is a texture laid over the lighter backdrop tone
    # -- pure black would still show noise (noise adds signal on top of it),
    # but the lighter tone reads as an intentional atmospheric plate rather
    # than "grain on nothing".
    base_color = backdrop[1] if len(backdrop) > 1 else backdrop[0]
    color = _to_ffmpeg_color(base_color)
    base = f"color=c={color}:s={dims}:r={spec.fps}:d={duration}"

    if spec.kind == "grain":
        # A named grain plate must remain visibly grainy even when the global
        # episode grade is deliberately clean.  The style value still makes a
        # stronger treatment possible; this floor only prevents the explicit
        # plate from collapsing into a flat colour field after H.264 encoding.
        strength = _clamp(
            max(float(grade.get("grain_strength", 0.18)), 0.18) * 100,
            1,
            100,
        )
        return f"{base},noise=alls={strength:.2f}:allf=t+u"

    if spec.kind == "static":
        # Static is an authored transition texture, not the episode-wide grain
        # control.  Keep it heavy enough to survive compression.
        strength = _clamp(
            max(float(grade.get("grain_strength", 0.18)), 0.18)
            * 100
            * _STATIC_MULTIPLIER,
            1,
            100,
        )
        return f"{base},noise=alls={strength:.2f}:allf=t+u"

    if spec.kind == "scanlines":
        # Likewise, requesting a scanline plate should never silently produce
        # a featureless plate merely because global scanlines are disabled.
        opacity = _clamp(
            max(float(grade.get("scanline_opacity", 0.12)), 0.08),
            0,
            1,
        )
        keep = 1 - opacity
        # Darken every other row by `opacity`, sampling the source pixel back
        # via geq's lum()/cb()/cr() so the underlying backdrop colour survives.
        # Commas inside the expression must be backslash-escaped: ffmpeg's own
        # filtergraph parser (not the shell) uses bare commas to split filters.
        lum_expr = (
            "if(mod(Y\\,2)\\,lum(X\\,Y)*" + f"{keep:.4f}" + "\\,lum(X\\,Y))"
        )
        return f"{base},geq=lum={lum_expr}:cb=cb(X\\,Y):cr=cr(X\\,Y)"

    if spec.kind == "vignette":
        strength = _clamp(float(grade.get("vignette_strength", 0.35)), 0, 1)
        # FFmpeg's angle grows from no visible falloff near zero toward an
        # extreme vignette near PI/2.
        angle = (math.pi / 2) * strength
        return f"{base},vignette=angle={angle:.6f}"

    raise AssertionError(f"unhandled plate kind: {spec.kind}")  # pragma: no cover


def build_plate(spec: PlateSpec, out_path: Path, grade: dict | None = None) -> Path:
    """Render one atmospheric plate to an MP4."""
    if spec.kind not in PLATE_KINDS:
        raise ValueError(
            f"Unknown plate kind '{spec.kind}'; valid kinds: {', '.join(PLATE_KINDS)}"
        )
    if spec.duration <= 0:
        raise ValueError(f"Plate duration must be positive, got {spec.duration}")

    resolved_grade = grade if grade is not None else load_grade(_DEFAULT_STYLE_DIR)
    backdrop = _load_backdrop(_DEFAULT_STYLE_DIR)
    filtergraph = _build_filtergraph(spec, resolved_grade, backdrop)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", filtergraph,
                "-t", f"{spec.duration:.6f}",
                "-r", str(spec.fps),
                # Pinned to the CPU encoder on purpose. A plate is synthesized
                # noise -- the worst case for any encoder, and the reason
                # PLATE_MAXRATE exists at all after one 12-second static plate
                # produced 311 MB. NVENC will not hold that cap on this content:
                # measured at 18.2 Mbit/s against a 6 Mbit cap even with
                # `-rc vbr -b:v -maxrate`, where x264 lands at 7.1.
                #
                # Nothing is lost by it. Plates are generated once per project
                # (110 of them here), while a render is 710 per-cut encodes plus
                # four full-length passes -- that is where the GPU pays, and it
                # is still used there.
                *video_args(
                    PLATE_CRF, encoder=CPU_ENCODER,
                    maxrate=PLATE_MAXRATE, bufsize=PLATE_BUFSIZE,
                ),
                "-pix_fmt", "yuv420p",
                str(out_path),
            ]
        )
    except RuntimeError:
        # A graph-parse failure never opens the output file, but a crash or
        # kill mid-encode can leave a corrupt, moov-less partial behind.
        # Don't let a failed build_plate() call leave junk on disk.
        if out_path.exists():
            out_path.unlink()
        raise
    return out_path

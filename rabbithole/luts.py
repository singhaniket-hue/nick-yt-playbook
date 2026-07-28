"""Generate the optional dark-documentary 3D LUT.

Generated ``.cube`` files are intentionally not stored in this reusable
repository. Point ``style/palette.json``'s ``grade.lut`` at a generated file
when a project should use this look.

The look, from the corpus analysis: crushed blacks, heavy desaturation, a
cold shadow tint, and warmth retained only in highlights (the palette's
`accent_red` and `warm_grey` are the tones that should survive). `noir_transform`
builds that look as a sequence of provably well-behaved steps rather than one
opaque formula, specifically so the monotonicity requirement below is a
consequence of the *shape* of the transform, not something hoped for and
then checked:

1. Desaturate toward luma (Rec. 709 weights), heavier in shadows than
   highlights -- this is where "heavy desaturation" and "saturation return in
   the highlights" both come from, via one desaturation-amount curve that
   falls off with luma.
2. Push a cold (blue-up, red-down) tint into the result, strongest at low
   luma and fading to nothing at luma 1 -- the shadow tint. This step is
   allowed to nudge the pixel's luma slightly; step 4 corrects that.
3. Compute a target luma via an S-curve (`_scurve`) applied to the
   *original* luma alone, with a small lift so true black does not crush all
   the way to zero (a "lift", in the Lift/Gamma/Gain sense, carrying the
   shadow tint rather than pure black). The S-curve's steep toe is the
   "crushed blacks" -- shadow tones compress hard toward the lifted floor.
4. Renormalize: shift all three channels by a constant so the pixel's actual
   luma exactly equals that target. Because this shift is additive and
   uniform across R/G/B, it changes brightness only, not the hue/chroma
   relationships steps 1-2 established. This is the step that makes
   monotonicity provable rather than incidental: post-renormalization luma
   is *exactly* `_scurve(original_luma)` for *any* starting colour, gray or
   saturated, and `_scurve` is monotonic non-decreasing by construction. The
   only way this guarantee can be disturbed is final [0,1] clamping at the
   very end, an unavoidable limit of any LUT (some colours are simply
   outside the target gamut).
5. Let saturation return in the highlights: scale the (now luma-target-
   centred) colour deviation by a factor that grows with luma. Because the
   deviation vector is Rec.709-zero-weighted by construction (it is exactly
   colour minus its own luma), scaling it by any factor leaves luma
   unchanged -- so this step cannot undo step 4's monotonicity guarantee
   either.
6. Clamp to [0, 1].
"""

from __future__ import annotations

from pathlib import Path

LUT_SIZE = 33

# Rec. 709 luma weights -- the same convention `render.grade_filter` and
# `subtitles`'s ASS colour handling implicitly share with the rest of this
# pipeline's ffmpeg-based colour work (ffmpeg's own `lut`/`geq` luma
# expressions use this weighting too).
_LUMA_R, _LUMA_G, _LUMA_B = 0.2126, 0.7152, 0.0722

# Shadow floor: true black does not crush all the way to (0,0,0) -- it lifts
# to this luma, carrying the cold shadow tint, rather than clipping to a flat
# black with no information in it at all.
_LIFT = 0.02
_GAIN = 1.0

# S-curve steepness (see `_scurve`). >1 crushes the shadow half toward the
# lifted floor and pushes the highlight half toward full gain; this is the
# "crushed blacks" half of the look.
_SCURVE_POWER = 1.6

# Desaturation-toward-luma amount, as a function of luma: `_DESAT_BASE` at
# luma 0, falling by up to `_DESAT_HIGHLIGHT_RELIEF` by luma 1. Heavy in the
# shadows (most of the frame, most of the time, for this format), relieved
# in the highlights so warm highlight colour is not flattened to grey.
_DESAT_BASE = 0.85
_DESAT_HIGHLIGHT_RELIEF = 0.65

# Cold shadow tint: subtracts from red, adds to blue (green barely moves),
# scaled by `_TINT_STRENGTH * (1 - luma)` so it is strongest in shadow and
# fades to nothing by luma 1.
_TINT_STRENGTH = 0.05
_TINT_COOL_R = 1.0
_TINT_COOL_G = 0.15
_TINT_COOL_B = 1.0

# How strongly saturation returns as luma rises toward 1 (scaled by luma**2
# so it is negligible in shadows/midtones and only really acts near the top
# of the range -- "warmth retained only in highlights").
_HIGHLIGHT_SAT_BOOST = 1.4


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _luma(r: float, g: float, b: float) -> float:
    return _LUMA_R * r + _LUMA_G * g + _LUMA_B * b


def _scurve(x: float) -> float:
    """A monotonic S-curve on [0,1]: `x**p / (x**p + (1-x)**p)`, lifted/gained.

    At `p == 1` this is the identity. At `p > 1` it crushes the low half
    toward 0 and boosts the high half toward 1, pivoting at 0.5 -- exactly
    the "S-curve that crushes the low end" the module docstring calls for.
    Monotonic non-decreasing on [0,1] for any `p > 0` (verified directly by
    `tests/test_luts.py`'s ramp test, not just asserted here).
    """
    x = _clamp(x)
    xp = x**_SCURVE_POWER
    ixp = (1 - x) ** _SCURVE_POWER
    denom = xp + ixp
    s = xp / denom if denom else 0.0
    return _LIFT + (_GAIN - _LIFT) * s


def noir_transform(r: float, g: float, b: float, *, palette: dict) -> tuple[float, float, float]:
    """Map one linear RGB triple through the Crowley noir look.

    `palette` is accepted (rather than hard-coding hex values here) so the
    look stays tunable from `style/palette.json` without changing this
    function's signature; today's transform is self-contained (its shape is
    what the corpus analysis specified), but the parameter keeps the door
    open for a future palette-driven tint colour without an API break.

    See the module docstring for the six-step derivation and why the result
    is provably monotonic in luma.
    """
    del palette  # not consulted by today's fixed-shape transform; see docstring

    luma0 = _luma(r, g, b)

    desat = _clamp(_DESAT_BASE - _DESAT_HIGHLIGHT_RELIEF * luma0)
    r1 = r + (luma0 - r) * desat
    g1 = g + (luma0 - g) * desat
    b1 = b + (luma0 - b) * desat

    tint = _TINT_STRENGTH * (1 - luma0)
    r2 = r1 - tint * _TINT_COOL_R
    g2 = g1 - tint * _TINT_COOL_G
    b2 = b1 + tint * _TINT_COOL_B

    target_luma = _scurve(luma0)
    shift = target_luma - _luma(r2, g2, b2)
    r3, g3, b3 = r2 + shift, g2 + shift, b2 + shift

    sat_factor = 1.0 + _HIGHLIGHT_SAT_BOOST * (luma0**2)
    dr, dg, db = r3 - target_luma, g3 - target_luma, b3 - target_luma
    r4 = target_luma + dr * sat_factor
    g4 = target_luma + dg * sat_factor
    b4 = target_luma + db * sat_factor

    return _clamp(r4), _clamp(g4), _clamp(b4)


def write_cube(out_path: Path, size: int = LUT_SIZE, *, palette: dict) -> Path:
    """Write a .cube 3D LUT.

    Format: a `LUT_3D_SIZE <size>` header line, then `size**3` data lines of
    three space-separated floats each. Axis order follows the .cube
    convention: red varies fastest, then green, then blue slowest -- the
    outer loop below is `b`, the middle `g`, the inner `r`, so consecutive
    output lines step through red first. Getting this backwards is the
    classic .cube mistake (ffmpeg's `lut3d` would silently sample the wrong
    input colour for every output colour); `tests/test_luts.py` checks a
    known index against the exact input triple it should correspond to,
    not just that the file parses.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [f"LUT_3D_SIZE {size}"]
    denom = size - 1 if size > 1 else 1
    for b in range(size):
        bf = b / denom
        for g in range(size):
            gf = g / denom
            for r in range(size):
                rf = r / denom
                ro, go, bo = noir_transform(rf, gf, bf, palette=palette)
                lines.append(f"{ro:.6f} {go:.6f} {bo:.6f}")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path

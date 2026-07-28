"""The three-layer render: EDL + sourced assets -> finished episode.

Layer 1 (this module's `assemble_footage`) turns the EDL plus the provenance
ledger into a single cut timeline with no graphics: one ffmpeg segment per
cut, framed and reframed per `edl.FRAMINGS`, concatenated in cut-index order.

A slot's asset covers the whole slot -- it was generated or fetched to span
`slot.start .. slot.end` -- while a cut is only a sub-range of that slot. So a
cut's absolute timeline time `t` maps to an asset-relative time `t -
slot.start`, and framing (wide / push-in / detail / slow-pan) is a camera
move applied on top of that sub-range, not a property of the source asset.

Layer 3 (`finish`) applies the style pack's grade (grain, vignette,
scanlines, and an optional LUT) and muxes the narration track.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path

from rabbithole.edl import FRAMINGS, Cut
from rabbithole.encoding import video_args
from rabbithole.provenance import AssetRecord
from rabbithole.slots import Slot
from rabbithole.sources.music import exact_cue_names
from rabbithole.sources.plates import PLATE_BUFSIZE, PLATE_CRF, PLATE_MAXRATE
from rabbithole.sources.sfx import SFX_NAMES
from rabbithole.validate import Finding

# Asked of sources/music.py rather than restated here: `out` plus every bed
# kind it actually implements. Cues outside that set -- including evocative
# licensed-track names like "chasms" that no synthesis can reproduce -- fall
# back to a generic drone-low bed and are worth a warning.
_MUSIC_EXACT_MATCHES = exact_cue_names()

_PUSH_IN_START_ZOOM = 1.05
_PUSH_IN_END_ZOOM = 1.15
_DETAIL_ZOOM = 1.30
_SLOW_PAN_ZOOM = 1.08

# Segment encode settings. Not the plate bitrate problem (real footage/plate
# textures composited here are already bounded by PLATE_MAXRATE upstream),
# but a sane, consistent default rather than x264's unconstrained CRF mode.
_SEGMENT_CRF = "20"

# How far a rendered segment's actual duration may drift from its requested
# frame-quantised duration before it is treated as an encoder failure rather
# than ordinary container timestamp rounding.
_SEGMENT_DURATION_TOLERANCE = 0.15

# How far footage and narration durations may differ before `finish` warns
# about it. The EDL is built from the timing spine that produced the
# narration in the first place, so the two should already agree closely;
# anything past half a second means something upstream drifted.
_AUDIO_MISMATCH_TOLERANCE = 0.5


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _run(args: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-1200:]
        raise RuntimeError(f"ffmpeg failed: {' '.join(args[:4])} ...\n{detail}")
    return result


def _probe_format(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)["format"]


def probe_duration(path: Path) -> float:
    """Duration in seconds, via ffprobe."""
    return float(_probe_format(path)["duration"])


# --- Layer 1: framing + cut segments + assembly ------------------------------


def framing_filter(framing: str, width: int, height: int, fps: int, duration: float) -> str:
    """The ffmpeg filter chain implementing one framing.

    Every framing starts from the same `base`: normalise timestamps to start
    at 0 (`setpts=PTS-STARTPTS`, so the time-based expressions below are
    relative to the cut, not to wherever it was seeked from in the source
    asset), then cover-scale and centre-crop onto exactly `width x height`.
    That base frame IS the `wide` framing outright; the other three enlarge
    it by a (possibly time-varying) zoom factor and crop back down to
    exactly `width x height` from the centre (or, for `slow-pan`, from a
    drifting x-offset).

    Two ffmpeg quirks drove this specific shape, both confirmed empirically
    against real ffmpeg output rather than assumed:

    - `crop`'s `w`/`h` options are evaluated ONCE at filter init, not per
      frame -- feeding them a `t`-dependent expression fails outright
      ("Failed to configure input pad"). Its `x`/`y` options, by contrast,
      DO re-evaluate every frame. So the crop used here always has constant
      `w`/`h` (exactly `width`/`height`); all motion goes through `x`/`y`,
      and the zoom itself is done by `scale` instead of by shrinking a crop.
    - `scale`'s `w`/`h` also default to evaluating once; they only track
      `t` per frame with the filter's own `eval=frame` option set
      explicitly. Without it, ffmpeg raises "Expressions with frame
      variables ... are not valid in init eval_mode."

    Deliberately not `zoompan`: its per-input-frame duplication model
    (`d` frames emitted per source frame) is easy to get subtly wrong on
    output frame count/timing, which is exactly the failure mode this
    module's tests check for directly against ffprobe.

    - `wide`: the base frame outright, no magnification.
    - `push-in`: the base frame is enlarged by a zoom factor rising linearly
      from 105% to 115% across the cut, then centre-cropped back down.
    - `detail`: the base frame is enlarged by a fixed ~130% and
      centre-cropped back down.
    - `slow-pan`: the base frame is enlarged by a fixed, mild ~108%, and the
      crop's x-offset drifts from the enlarged frame's left edge to its
      right edge across the cut (y stays centred).
    """
    if framing not in FRAMINGS:
        raise ValueError(f"Unknown framing {framing!r}; expected one of {FRAMINGS}")
    if duration <= 0:
        raise ValueError(f"framing_filter duration must be positive, got {duration}")
    if width <= 0 or height <= 0:
        raise ValueError(f"framing_filter width/height must be positive, got {width}x{height}")

    base = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}"
    )

    if framing == "wide":
        return f"setpts=PTS-STARTPTS,{base},setsar=1"

    if framing == "push-in":
        z0, z1 = _PUSH_IN_START_ZOOM, _PUSH_IN_END_ZOOM
        zoom_expr = f"({z0}+({z1}-{z0})/{duration:.6f}*t)"
        enlarge = f"scale=w='{width}*{zoom_expr}':h='{height}*{zoom_expr}':eval=frame"
        recrop = f"crop={width}:{height}:x='(in_w-out_w)/2':y='(in_h-out_h)/2'"
    elif framing == "detail":
        z = _DETAIL_ZOOM
        enlarge = f"scale=w='{width}*{z}':h='{height}*{z}'"
        recrop = f"crop={width}:{height}:x='(in_w-out_w)/2':y='(in_h-out_h)/2'"
    elif framing == "slow-pan":
        z = _SLOW_PAN_ZOOM
        enlarge = f"scale=w='{width}*{z}':h='{height}*{z}'"
        recrop = f"crop={width}:{height}:x='t/{duration:.6f}*(in_w-out_w)':y='(in_h-out_h)/2'"
    else:  # pragma: no cover -- FRAMINGS membership already checked above
        raise AssertionError(f"unhandled framing: {framing}")

    return f"setpts=PTS-STARTPTS,{base},{enlarge},{recrop},setsar=1"


def _cut_frame_count(cut: Cut, fps: int) -> int:
    """Number of output frames occupied by ``cut`` on the episode timeline.

    Quantising both absolute cut boundaries, rather than rounding every cut's
    duration independently, makes contiguous cuts telescope: their frame
    counts add up to the frame-quantised episode duration without cumulative
    per-cut rounding drift.
    """
    if fps <= 0:
        raise ValueError(f"fps must be positive, got {fps}")
    start_frame = round(cut.start * fps)
    end_frame = round(cut.end * fps)
    return max(1, end_frame - start_frame)


def cut_segment(
    cut: Cut,
    slot: Slot,
    asset_path: Path,
    out_path: Path,
    *,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
) -> Path:
    """Render one cut: seek into the asset, take the cut's duration, apply framing.

    The asset was sourced to span the whole slot, so the cut's asset-relative
    offset is `cut.start - slot.start`. Seeking is done as an OUTPUT option
    (`-ss` after `-i`) rather than an input option, trading speed for frame
    accuracy: a fast keyframe-based input seek could land a mid-slot cut on
    the wrong content, which is exactly the failure this module's tests
    check for.
    """
    offset = cut.start - slot.start
    if offset < -1e-6:
        raise ValueError(
            f"Cut {cut.index} starts at {cut.start}, before its slot {slot.slot_id!r} "
            f"begins at {slot.start}; cannot map to an asset-relative offset."
        )
    offset = max(offset, 0.0)
    duration = cut.duration
    frame_count = _cut_frame_count(cut, fps)

    out_path = Path(out_path)
    # ``-ss`` remains an output option for frame-accurate seeking.  Output
    # seeking is applied after the filter graph, so the graph's input timeline
    # must physically reach ``offset`` even when the source video ends before
    # a later cut in a multi-cut slot.  tpad clones the final source frame far
    # enough to cover that offset and the complete cut.  One extra output frame
    # protects the exclusive endpoint when source and output frame grids differ.
    pad_duration = offset + duration + (1.0 / fps)
    vf = (
        f"tpad=stop_mode=clone:stop_duration={pad_duration:.6f},"
        f"{framing_filter(cut.framing, width, height, fps, duration)}"
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run(
            [
                "ffmpeg", "-y",
                "-i", str(asset_path),
                "-ss", f"{offset:.6f}",
                "-vf", vf,
                "-frames:v", str(frame_count),
                "-r", str(fps),
                "-an",
                *video_args(int(_SEGMENT_CRF)),
                "-pix_fmt", "yuv420p",
                str(out_path),
            ]
        )
    except RuntimeError:
        if out_path.exists():
            out_path.unlink()
        raise
    return out_path


def _asset_path_for_slot(slot_id: str, records: list[AssetRecord]) -> Path | None:
    for record in records:
        if slot_id in record.used_in_slots:
            return Path(record.local_path)
    return None


def _concat_segments(segments: list[Path], out_path: Path) -> Path:
    listing = out_path.with_suffix(".concat.txt")
    listing.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in segments) + "\n",
        encoding="utf-8",
    )
    try:
        _run(
            [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(listing),
                "-c", "copy",
                str(out_path),
            ]
        )
    finally:
        listing.unlink()
    return out_path


def assemble_footage(
    cuts: list[Cut],
    slots: list[Slot],
    records: list[AssetRecord],
    out_path: Path,
    work_dir: Path,
    *,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
) -> tuple[Path, list[Finding]]:
    """Layer 1: the cut timeline, no graphics.

    Each cut is rendered to its own segment file under `work_dir`, named by
    cut index (`cut-0000.mp4`, ...) so a partial or failed run is directly
    inspectable rather than leaving anonymous temp files behind. A slot with
    no matching provenance record is reported as a `severity="error"`
    `Finding` and that one cut is skipped -- the rest of the assembly still
    proceeds, because one missing asset should not abort an otherwise-good
    render.
    """
    out_path = Path(out_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    slots_by_id = {slot.slot_id: slot for slot in slots}
    findings: list[Finding] = []
    segments: list[Path] = []

    for cut in sorted(cuts, key=lambda c: c.index):
        slot = slots_by_id.get(cut.slot_id)
        if slot is None:
            findings.append(
                Finding(
                    gate="render",
                    severity="error",
                    message=(
                        f"Cut {cut.index} references slot {cut.slot_id!r}, which is "
                        f"not in the slot plan; skipped."
                    ),
                )
            )
            continue

        asset_path = _asset_path_for_slot(cut.slot_id, records)
        if asset_path is None:
            findings.append(
                Finding(
                    gate="render",
                    severity="error",
                    message=(
                        f"Slot {cut.slot_id!r} has no asset in the provenance ledger; "
                        f"cut {cut.index} skipped."
                    ),
                )
            )
            continue

        seg_path = work_dir / f"cut-{cut.index:04d}.mp4"
        try:
            cut_segment(cut, slot, asset_path, seg_path, width=width, height=height, fps=fps)
        except RuntimeError as exc:
            # A broken/unreadable asset (or any other ffmpeg failure) on one
            # cut must not take the whole assembly down with it, exactly like
            # the missing-record case above -- earlier segments already
            # written to work_dir are left in place (still inspectable), and
            # this cut is skipped rather than the run crashing uncaught.
            findings.append(
                Finding(
                    gate="render",
                    severity="error",
                    message=(
                        f"Cut {cut.index} (slot {cut.slot_id!r}) failed to render "
                        f"from {asset_path}: {exc}"
                    ),
                )
            )
            continue

        # cut_segment pads a short source with its final frame and pins output
        # to an exact timeline-derived frame count.  Keep a post-encode guard:
        # if the encoder nevertheless returns a short segment, concatenating it
        # would desynchronise every later cut from the EDL.
        actual = probe_duration(seg_path)
        expected = _cut_frame_count(cut, fps) / fps
        if abs(actual - expected) > _SEGMENT_DURATION_TOLERANCE:
            findings.append(
                Finding(
                    gate="render",
                    severity="error",
                    message=(
                        f"Cut {cut.index} (slot {cut.slot_id!r}) wanted "
                        f"{expected:.3f}s ({_cut_frame_count(cut, fps)} frames) "
                        f"but the encoder yielded {actual:.3f}s. The segment is "
                        f"included, but the concatenated output may desynchronise "
                        f"from the EDL from this cut onward."
                    ),
                )
            )
        segments.append(seg_path)

    if not segments:
        findings.append(
            Finding(
                gate="render",
                severity="error",
                message="No cuts were rendered; footage assembly produced nothing.",
            )
        )
        return out_path, findings

    _concat_segments(segments, out_path)
    return out_path, findings


# --- Layer 3: grade + narration mux ------------------------------------------


def _probe_video_dims(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    return int(stream["width"]), int(stream["height"])


def _ffmpeg_path(path: Path) -> str:
    """Format a filesystem path for use inside an ffmpeg filtergraph option value.

    Mirrors a problem `sources/plates.py`'s own tests already worked around for
    the `movie` filter: a colon after a Windows drive letter (`C:\\...`) collides
    with the filtergraph parser's own use of ':' to separate filter options, so
    it must be escaped.
    """
    posix = path.resolve().as_posix()
    return posix.replace(":", "\\:")


def _audio_master_filter(path: Path) -> str:
    """Return the delivery master filter, or a no-op for digital silence.

    FFmpeg's EBU loudness normalizer emits no audio frames for a fully silent
    input and consequently makes the MP4 mux fail.  Silence is a valid test
    fixture and can be a valid deliberate documentary beat, so detect that
    case rather than treating it as broken audio.
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
    )
    text = result.stderr.decode("utf-8", errors="replace")
    match = re.search(r"max_volume:\s*(-inf|-?\d+(?:\.\d+)?) dB", text)
    if match is not None:
        measured = match.group(1)
        if measured == "-inf" or float(measured) <= -80.0:
            return "anull"
    return "loudnorm=I=-16:TP=-1.5:LRA=9"


def grade_filter(grade: dict, width: int, height: int) -> str:
    """Build a restrained, evidence-safe global finish.

    The old filter always stacked a crushed-black LUT, 18-point noise,
    alternating-row darkening, and a strong vignette over every frame,
    including court records and browser captures.  A documentary grade must
    not make its evidence unreadable.  All treatments are now independently
    optional, zero really means disabled, and conservative colour controls
    are explicit rather than hidden inside a destructive LUT.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"grade_filter width/height must be positive, got {width}x{height}")

    brightness = _clamp(float(grade.get("brightness", 0.0)), -1, 1)
    contrast = _clamp(float(grade.get("contrast", 1.0)), 0.5, 2.0)
    saturation = _clamp(float(grade.get("saturation", 1.0)), 0.0, 3.0)
    grain_strength = _clamp(float(grade.get("grain_strength", 0.0)) * 100, 0, 100)
    scanline_opacity = _clamp(float(grade.get("scanline_opacity", 0.0)), 0, 1)
    vignette_strength = _clamp(float(grade.get("vignette_strength", 0.0)), 0, 1)

    filters: list[str] = []
    if (
        abs(brightness) > 1e-9
        or abs(contrast - 1.0) > 1e-9
        or abs(saturation - 1.0) > 1e-9
    ):
        filters.append(
            f"eq=brightness={brightness:.4f}:contrast={contrast:.4f}:"
            f"saturation={saturation:.4f}"
        )
    if grain_strength > 0:
        filters.append(f"noise=alls={grain_strength:.2f}:allf=t+u")
    if scanline_opacity > 0:
        keep = 1 - scanline_opacity
        lum_expr = "if(mod(Y\\,2)\\,lum(X\\,Y)*" + f"{keep:.4f}" + "\\,lum(X\\,Y))"
        filters.append(f"geq=lum={lum_expr}:cb=cb(X\\,Y):cr=cr(X\\,Y)")
    if vignette_strength > 0:
        # FFmpeg's angle grows from no visible falloff near zero toward an
        # extreme vignette near PI/2.  Mapping it backwards made an intended
        # 8% finish crush almost the entire evidence page to black.
        angle = (math.pi / 2) * vignette_strength
        filters.append(f"vignette=angle={angle:.6f}")

    return ",".join(filters) if filters else "null"


def finish(
    footage_path: Path,
    vo_path: Path,
    out_path: Path,
    grade: dict,
    *,
    lut_path: Path | None = None,
    mixed_audio_path: Path | None = None,
) -> tuple[Path, list[Finding]]:
    """Layer 3: apply the grade and mux the narration.

    The EDL is authoritative on length: the output always runs exactly as
    long as `footage_path` (`-t` pinned to its probed duration), so a longer
    audio track is cut off and a shorter one just ends early rather than
    being padded. A mismatch over `_AUDIO_MISMATCH_TOLERANCE` is still worth
    a human's attention, so it is reported as a warning naming both
    durations, not silently absorbed.

    `mixed_audio_path`, when given, is muxed in place of the bare `vo_path`
    -- the output of `audiomix.build_mix` (VO plus ducked SFX and bed
    layers) rather than the raw narration. `vo_path` is still required (the
    existing signature keeps working for every current caller/test); it is
    simply not the audio track muxed when a mix is supplied. The mismatch
    check compares footage against whichever audio track actually gets
    muxed, since that -- not the original vo.wav -- is what needs to agree
    with the footage length.

    `lut_path` is applied only when it is given AND the file exists --
    `style/palette.json`'s `grade.lut` currently names a file that does not
    exist in this repo, and that is expected to happen, not a hard failure.
    When the grade names a LUT that isn't available, a warning says so.
    """
    footage_path = Path(footage_path)
    vo_path = Path(vo_path)
    out_path = Path(out_path)
    audio_path = Path(mixed_audio_path) if mixed_audio_path is not None else vo_path

    findings: list[Finding] = []

    width, height = _probe_video_dims(footage_path)
    footage_duration = probe_duration(footage_path)
    audio_duration = probe_duration(audio_path)
    audio_master_filter = _audio_master_filter(audio_path)

    if abs(footage_duration - audio_duration) > _AUDIO_MISMATCH_TOLERANCE:
        findings.append(
            Finding(
                gate="render",
                severity="warning",
                message=(
                    f"Footage is {footage_duration:.2f}s but narration is "
                    f"{audio_duration:.2f}s -- they differ by more than "
                    f"{_AUDIO_MISMATCH_TOLERANCE}s. The footage duration is "
                    f"authoritative; narration will end early or be cut."
                ),
            )
        )

    lut_applied = lut_path is not None and Path(lut_path).exists()
    named_lut = grade.get("lut")
    if named_lut and not lut_applied:
        where = f" (checked {lut_path})" if lut_path is not None else ""
        findings.append(
            Finding(
                gate="render",
                severity="warning",
                message=(
                    f"Grade names LUT {named_lut!r} but it was not found{where}; "
                    f"rendering without it."
                ),
            )
        )

    filters = []
    if lut_applied:
        filters.append(f"lut3d=file='{_ffmpeg_path(Path(lut_path))}'")
    filters.append(grade_filter(grade, width, height))
    vf = ",".join(filters)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run(
            [
                "ffmpeg", "-y",
                "-i", str(footage_path),
                "-i", str(audio_path),
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-t", f"{footage_duration:.6f}",
                "-vf", vf,
                "-af", audio_master_filter,
                *video_args(PLATE_CRF, maxrate=PLATE_MAXRATE, bufsize=PLATE_BUFSIZE),
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", "48000",
                "-ac", "2",
                str(out_path),
            ]
        )
    except RuntimeError:
        if out_path.exists():
            out_path.unlink()
        raise

    return out_path, findings


def deferred_audio_cues(document: dict) -> list[Finding]:
    """Report SFX and MUSIC markers that `audiomix.build_mix` cannot resolve.

    `audiomix.build_mix` can now actually place SFX and music beds (see
    `rabbithole/audiomix.py`), so most cues no longer need a warning here.
    What's left is the genuinely unresolved subset:

    - an `SFX` name absent from `sources.sfx.SFX_NAMES` -- there is no
      synthesis for it at all;
    - a `MUSIC` cue that `resolve_cue` falls back on rather than matching.
      `out` maps exactly (to true silence) and so does any cue naming a bed
      kind `sources.music` implements, so neither warns. What warns is a cue
      naming a licensed track (e.g. `chasms`) that no local synthesis can
      reproduce, since that one really does silently become a different bed.

    One warning per distinct (kind, name) cue, not one per occurrence, since
    a cue repeated ten times in the script is one gap, not ten.
    """
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()

    for marker in document.get("markers", []):
        kind = marker.get("kind")
        if kind not in ("SFX", "MUSIC"):
            continue
        name = marker.get("arg", "")

        if kind == "SFX" and name in SFX_NAMES:
            continue
        if kind == "MUSIC" and name in _MUSIC_EXACT_MATCHES:
            continue

        key = (kind, name)
        if key in seen:
            continue
        seen.add(key)

        if kind == "SFX":
            message = (
                f"[SFX:{name}] is not a known cue; style/sfx.json lists "
                f"{', '.join(SFX_NAMES)}. Not mixed."
            )
        else:
            message = (
                f"[MUSIC:{name}] has no matching bed; it falls back to a "
                f"generic drone-low bed rather than the licensed track the "
                f"cue names."
            )

        findings.append(Finding(gate="render", severity="warning", message=message))

    return findings

"""Layer 2 audio mix: SFX + music beds, ducked through silence drops, mixed with VO.

Real ffmpeg. Short synthetic timing documents as fixtures -- never the real project.
"""

from __future__ import annotations

import json
import re
import struct
import subprocess
import wave

import pytest

from rabbithole.assemble import probe_duration
from rabbithole.audiomix import (
    BedSpan,
    SfxEvent,
    SilenceWindow,
    bed_spans,
    build_bed_layer,
    build_mix,
    build_sfx_layer,
    duck,
    load_sfx_categories,
    mix_audio,
    sfx_events,
    silence_windows,
)
from rabbithole.config import REPO_ROOT
from rabbithole.sources.sfx import SFX_NAMES

STYLE_DIR = REPO_ROOT / "style"


# --- helpers -----------------------------------------------------------------


def _marker(kind, arg, seconds, word_index=0, line=1):
    return {"kind": kind, "arg": arg, "word_index": word_index, "line": line, "seconds": seconds}


def _document(duration, markers):
    return {"duration_seconds": duration, "word_count": 0, "words": [], "markers": markers}


def _peak_dbfs(path):
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
    )
    text = result.stderr.decode("utf-8", errors="replace")
    match = re.search(r"max_volume:\s*(-?\d+\.?\d*) dB", text)
    assert match, f"volumedetect produced no max_volume for {path}:\n{text}"
    return float(match.group(1))


def _segment_peak_dbfs(path, start, duration):
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(duration), "-i", str(path),
            "-af", "volumedetect", "-f", "null", "-",
        ],
        capture_output=True,
    )
    text = result.stderr.decode("utf-8", errors="replace")
    match = re.search(r"max_volume:\s*(-?\d+\.?\d*) dB", text)
    if not match:
        return -150.0  # no volumedetect line at all == pure digital silence
    return float(match.group(1))


def _read_samples(path):
    with wave.open(str(path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    return struct.unpack(f"<{len(frames) // 2}h", frames)


def _tone(path, seconds, freq=220, sample_rate=44100, peak_db=-6.0):
    """A sine tone peak-normalised (two-pass measure-then-correct, same shape
    sources/sfx.py uses) to an actual absolute `peak_db` -- lavfi's `sine`
    source does not default to 0 dBFS peak in this ffmpeg build (it measures
    ~-18 dBFS unattenuated), so a bare relative `volume=Ndb` would not land
    where the name `peak_db` promises."""
    raw = path.with_suffix(".raw.wav")
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate={sample_rate}",
            "-ac", "1", "-c:a", "pcm_s16le", str(raw),
        ],
        capture_output=True,
        check=True,
    )
    measured = _peak_dbfs(raw)
    gain = peak_db - measured
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(raw), "-af", f"volume={gain:.4f}dB",
            "-ac", "1", "-c:a", "pcm_s16le", str(path),
        ],
        capture_output=True,
        check=True,
    )
    raw.unlink()
    return path


def _vo_with_silence(path, duration, windows, freq=440, sample_rate=44100):
    """A VO-shaped track: a continuous tone except true digital silence
    inside each (start, end) window -- mirroring the real narration
    convention this module documents (an earlier phase already bakes real
    silence into vo.wav for [SILENCE:] spans), so a synthetic fixture that
    checks ducking against a silence window isn't fighting a VO that's still
    playing through it.
    """
    segments = []
    cursor = 0.0
    parts_dir = path.parent / f"{path.stem}-parts"
    parts_dir.mkdir(exist_ok=True)
    for index, (start, end) in enumerate(sorted(windows)):
        if start > cursor:
            seg = parts_dir / f"tone-{index}.wav"
            _tone(seg, start - cursor, freq=freq, sample_rate=sample_rate, peak_db=-6.0)
            segments.append(seg)
        gap = parts_dir / f"silence-{index}.wav"
        _silence(gap, end - start, sample_rate=sample_rate)
        segments.append(gap)
        cursor = end
    if cursor < duration:
        seg = parts_dir / "tone-tail.wav"
        _tone(seg, duration - cursor, freq=freq, sample_rate=sample_rate, peak_db=-6.0)
        segments.append(seg)

    listing = parts_dir / "concat.txt"
    listing.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in segments) + "\n", encoding="utf-8"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c:a", "pcm_s16le", str(path),
        ],
        capture_output=True,
        check=True,
    )
    return path


def _silence(path, seconds, sample_rate=44100):
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=mono",
            "-t", str(seconds), "-ac", "1", "-c:a", "pcm_s16le", str(path),
        ],
        capture_output=True,
        check=True,
    )
    return path


def _vo(path, seconds, freq=440):
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate=44100",
            "-ac", "1", "-c:a", "pcm_s16le", str(path),
        ],
        capture_output=True,
        check=True,
    )
    return path


# --- silence_windows -----------------------------------------------------------


def test_silence_windows_parses_one_window():
    # A marker's `seconds` is when it *fires* -- the word it precedes
    # beginning, per timing.py::marker_times -- which for a SILENCE marker
    # is the moment the gap ENDS, not when it starts. Confirmed against real
    # vo.wav data (see the module docstring on silence_windows): the window
    # is [seconds - length, seconds].
    document = _document(20.0, [_marker("SILENCE", "1.5s", seconds=5.0)])

    windows = silence_windows(document)

    assert windows == [SilenceWindow(start=3.5, end=5.0)]


def test_silence_windows_merges_two_overlapping():
    document = _document(
        20.0,
        [
            _marker("SILENCE", "3.0s", seconds=8.0),  # 5.0 - 8.0
            _marker("SILENCE", "3.0s", seconds=6.0),  # 3.0 - 6.0, overlaps
        ],
    )

    windows = silence_windows(document)

    assert windows == [SilenceWindow(start=3.0, end=8.0)]


def test_silence_windows_clamps_one_running_past_the_start():
    # length longer than the marker's own fire time would push the window
    # before t=0; it clamps to 0 instead.
    document = _document(10.0, [_marker("SILENCE", "5.0s", seconds=2.0)])

    windows = silence_windows(document)

    assert windows == [SilenceWindow(start=0.0, end=2.0)]


def test_silence_windows_malformed_arg_raises_naming_the_marker():
    document = _document(10.0, [_marker("SILENCE", "oops", seconds=1.0, line=7)])

    with pytest.raises(RuntimeError) as excinfo:
        silence_windows(document)

    assert "oops" in str(excinfo.value)
    assert "7" in str(excinfo.value)


def test_silence_windows_empty_for_no_silence_markers():
    document = _document(10.0, [_marker("SFX", "sub-drop", seconds=1.0)])

    assert silence_windows(document) == []


# --- bed_spans -----------------------------------------------------------------


def test_bed_spans_single_cue_spans_to_duration():
    document = _document(30.0, [_marker("MUSIC", "drone-low", seconds=0.0)])

    spans = bed_spans(document)

    assert spans == [BedSpan(start=0.0, end=30.0, cue="drone-low")]


def test_bed_spans_two_adjacent_spans_for_two_cues():
    document = _document(
        30.0,
        [
            _marker("MUSIC", "drone-low", seconds=0.0),
            _marker("MUSIC", "pulse-slow", seconds=10.0),
        ],
    )

    spans = bed_spans(document)

    assert spans == [
        BedSpan(start=0.0, end=10.0, cue="drone-low"),
        BedSpan(start=10.0, end=30.0, cue="pulse-slow"),
    ]


def test_bed_spans_no_span_for_out():
    document = _document(
        30.0,
        [
            _marker("MUSIC", "drone-low", seconds=0.0),
            _marker("MUSIC", "out", seconds=10.0),
        ],
    )

    spans = bed_spans(document)

    assert spans == [BedSpan(start=0.0, end=10.0, cue="drone-low")]


def test_bed_spans_empty_when_no_music_markers():
    document = _document(30.0, [_marker("SFX", "sub-drop", seconds=1.0)])

    assert bed_spans(document) == []


# --- sfx_events ------------------------------------------------------------------


def test_sfx_events_one_per_marker_in_time_order():
    document = _document(
        30.0,
        [
            _marker("SFX", "sub-drop", seconds=10.0),
            _marker("SFX", "vhs-burst", seconds=1.0),
        ],
    )

    events = sfx_events(document)

    assert events == [
        SfxEvent(seconds=1.0, name="vhs-burst"),
        SfxEvent(seconds=10.0, name="sub-drop"),
    ]


# --- load_sfx_categories ---------------------------------------------------------


def test_load_sfx_categories_reads_the_real_style_pack():
    categories = load_sfx_categories(STYLE_DIR / "sfx.json")

    assert set(categories.keys()) == set(SFX_NAMES)
    for name in SFX_NAMES:
        assert categories[name]


# --- build_sfx_layer ---------------------------------------------------------------


def test_build_sfx_layer_has_requested_duration(tmp_path):
    events = [SfxEvent(seconds=1.0, name="vhs-burst")]
    categories = load_sfx_categories(STYLE_DIR / "sfx.json")

    out_path, findings = build_sfx_layer(
        events, 5.0, tmp_path / "sfx.wav", tmp_path / "cache", categories
    )

    assert probe_duration(out_path) == pytest.approx(5.0, abs=0.1)
    assert findings == []


def test_build_sfx_layer_signal_near_event_silence_away_from_it(tmp_path):
    events = [SfxEvent(seconds=2.5, name="sub-drop")]
    categories = load_sfx_categories(STYLE_DIR / "sfx.json")

    out_path, findings = build_sfx_layer(
        events, 5.0, tmp_path / "sfx.wav", tmp_path / "cache", categories
    )

    near = _segment_peak_dbfs(out_path, 2.5, 1.0)
    away = _segment_peak_dbfs(out_path, 0.0, 1.0)

    assert near > -40.0, f"expected signal near the event, measured {near} dBFS"
    assert away < -60.0, f"expected silence away from the event, measured {away} dBFS"


def test_build_sfx_layer_unknown_cue_errors_and_is_skipped_valid_still_renders(tmp_path):
    events = [
        SfxEvent(seconds=1.0, name="not-a-real-cue"),
        SfxEvent(seconds=3.0, name="sub-drop"),
    ]
    categories = load_sfx_categories(STYLE_DIR / "sfx.json")

    out_path, findings = build_sfx_layer(
        events, 5.0, tmp_path / "sfx.wav", tmp_path / "cache", categories
    )

    errors = [f for f in findings if f.severity == "error"]
    assert len(errors) == 1
    assert "not-a-real-cue" in errors[0].message
    assert errors[0].gate == "audiomix"

    valid_signal = _segment_peak_dbfs(out_path, 3.0, 1.0)
    assert valid_signal > -40.0


def test_build_sfx_layer_reused_cue_synthesized_once(tmp_path):
    events = [SfxEvent(seconds=t, name="vhs-burst") for t in (1.0, 2.0, 3.0, 4.0)]
    categories = load_sfx_categories(STYLE_DIR / "sfx.json")
    cache_dir = tmp_path / "cache"

    build_sfx_layer(events, 6.0, tmp_path / "sfx.wav", cache_dir, categories)

    rendered = [p for p in cache_dir.glob("*vhs-burst*") if p.exists()]
    assert len(rendered) == 1


# --- build_bed_layer ---------------------------------------------------------------


def test_build_bed_layer_has_requested_duration(tmp_path):
    spans = [BedSpan(start=0.0, end=5.0, cue="drone-low")]

    out_path, findings = build_bed_layer(spans, 5.0, tmp_path / "bed.wav", tmp_path / "cache")

    assert probe_duration(out_path) == pytest.approx(5.0, abs=0.1)


def test_build_bed_layer_gap_between_spans_is_silent(tmp_path):
    spans = [
        BedSpan(start=0.0, end=2.0, cue="drone-low"),
        BedSpan(start=4.0, end=6.0, cue="drone-low"),
    ]

    out_path, findings = build_bed_layer(spans, 6.0, tmp_path / "bed.wav", tmp_path / "cache")

    gap_peak = _segment_peak_dbfs(out_path, 2.2, 1.5)
    assert gap_peak < -60.0, f"expected silence in the gap, measured {gap_peak} dBFS"


# --- the generated sound library ---------------------------------------------------


def _library_with(tmp_path, sfx_names=(), bed_kinds=(), bed_variants=1, bed_seconds=4.0):
    """A Library populated without touching the network.

    Files are written straight to the paths a Library exposes and recorded in
    its manifest, which is exactly what a real `sound build` leaves behind --
    so these tests exercise the resolution path, not the generator.
    """
    from rabbithole.sources.music import TARGET_PEAK_DB as BED_TARGET
    from rabbithole.sources.sfx import TARGET_PEAK_DB as SFX_TARGET
    from rabbithole.sources.soundgen import Library, SoundRequest

    library = Library(root=tmp_path / "soundlib")
    for name in sfx_names:
        path = library.sfx_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        _tone(path, 1.0, freq=1200, peak_db=SFX_TARGET)
        library.record(f"sfx/{name}", path,
                       SoundRequest(slug=f"sfx/{name}", prompt="p", duration=1.0))
    for kind in bed_kinds:
        for variant in range(bed_variants):
            path = library.bed_path(kind, variant)
            path.parent.mkdir(parents=True, exist_ok=True)
            _tone(path, bed_seconds, freq=110 + 20 * variant, peak_db=BED_TARGET)
            library.record(f"beds/{kind}/{variant}", path,
                           SoundRequest(slug=f"beds/{kind}", prompt="p",
                                        duration=bed_seconds, variant=variant))
    return library


def test_sfx_layer_prefers_the_library_over_synthesis(tmp_path):
    """The whole point of generating cues: when one exists, it must be used."""
    library = _library_with(tmp_path, sfx_names=("vhs-burst",))
    events = [SfxEvent(seconds=1.0, name="vhs-burst")]
    categories = load_sfx_categories(STYLE_DIR / "sfx.json")
    cache_dir = tmp_path / "cache"

    out_path, findings = build_sfx_layer(
        events, 4.0, tmp_path / "sfx.wav", cache_dir, categories, library=library
    )

    assert not list(cache_dir.glob("sfx-vhs-burst.wav")), "synthesized despite a library hit"
    assert probe_duration(out_path) == pytest.approx(4.0, abs=0.1)
    assert [f for f in findings if f.severity == "error"] == []


def test_sfx_layer_warns_when_the_library_lacks_a_cue(tmp_path):
    """Asking for generated sound and silently getting synthesis is the failure
    mode worth reporting -- the mix still renders, just not as intended."""
    library = _library_with(tmp_path, sfx_names=("vhs-burst",))
    events = [SfxEvent(seconds=1.0, name="sub-drop")]
    categories = load_sfx_categories(STYLE_DIR / "sfx.json")

    out_path, findings = build_sfx_layer(
        events, 4.0, tmp_path / "sfx.wav", tmp_path / "cache", categories, library=library
    )

    warnings = [f for f in findings if f.severity == "warning"]
    assert len(warnings) == 1
    assert "sub-drop" in warnings[0].message
    assert _segment_peak_dbfs(out_path, 1.0, 1.0) > -40.0, "fallback must still render"


def test_sfx_layer_without_a_library_synthesizes_and_stays_quiet(tmp_path):
    """The default path must not start warning; drafts render with no library."""
    events = [SfxEvent(seconds=1.0, name="sub-drop")]
    categories = load_sfx_categories(STYLE_DIR / "sfx.json")

    _, findings = build_sfx_layer(
        events, 4.0, tmp_path / "sfx.wav", tmp_path / "cache", categories
    )

    assert findings == []


def test_bed_layer_tiles_library_variants_across_a_long_span(tmp_path):
    """The API caps a generation at 30s. A span longer than the material must
    still be covered end to end -- a short bed would let silence show through."""
    library = _library_with(tmp_path, bed_kinds=("drone-low",), bed_variants=2, bed_seconds=4.0)
    spans = [BedSpan(start=0.0, end=20.0, cue="drone-low")]

    out_path, findings = build_bed_layer(
        spans, 20.0, tmp_path / "bed.wav", tmp_path / "cache", library=library
    )

    assert probe_duration(out_path) == pytest.approx(20.0, abs=0.15)
    for at in (1.0, 8.0, 15.0, 18.5):
        peak = _segment_peak_dbfs(out_path, at, 0.8)
        assert peak > -70.0, f"bed dropped out at {at}s ({peak} dBFS)"
    assert [f for f in findings if f.severity == "warning"] == []


def test_bed_layer_uses_the_cue_the_script_actually_asked_for(tmp_path):
    """`drone-tense` used to collapse into `drone-low`. With variants present
    for the tense bed only, a span asking for it must resolve without warning --
    which it cannot do if the cue is still being rewritten."""
    library = _library_with(tmp_path, bed_kinds=("drone-tense",), bed_variants=1)
    spans = [BedSpan(start=0.0, end=6.0, cue="drone-tense")]

    _, findings = build_bed_layer(
        spans, 6.0, tmp_path / "bed.wav", tmp_path / "cache", library=library
    )

    assert [f for f in findings if f.severity == "warning"] == []


def test_bed_layer_warns_when_the_library_lacks_the_kind(tmp_path):
    library = _library_with(tmp_path, bed_kinds=("drone-low",))
    spans = [BedSpan(start=0.0, end=6.0, cue="pulse-slow")]

    out_path, findings = build_bed_layer(
        spans, 6.0, tmp_path / "bed.wav", tmp_path / "cache", library=library
    )

    warnings = [f for f in findings if f.severity == "warning"]
    assert len(warnings) == 1
    assert "pulse-slow" in warnings[0].message
    assert probe_duration(out_path) == pytest.approx(6.0, abs=0.1)


def test_build_mix_threads_the_library_through(tmp_path):
    library = _library_with(
        tmp_path, sfx_names=("bass-thud",), bed_kinds=("drone-low",), bed_variants=1
    )
    vo = _vo(tmp_path / "vo.wav", 8.0)
    document = _document(8.0, [
        _marker("MUSIC", "drone-low", 0.0),
        _marker("SFX", "bass-thud", 3.0),
    ])

    out_path, findings = build_mix(
        document, vo, tmp_path / "mix.wav", tmp_path / "work", STYLE_DIR, library=library
    )

    assert probe_duration(out_path) == pytest.approx(8.0, abs=0.15)
    assert [f for f in findings if f.severity == "warning"] == []


# --- duck ------------------------------------------------------------------------


def test_duck_silences_a_window(tmp_path):
    track = _tone(tmp_path / "tone.wav", 6.0)
    windows = [SilenceWindow(start=2.0, end=4.0)]

    out_path = duck(track, windows, tmp_path / "ducked.wav")

    inside = _segment_peak_dbfs(out_path, 2.3, 1.4)
    outside = _segment_peak_dbfs(out_path, 0.0, 1.5)

    assert inside < -50.0, f"expected near-silence inside the window, measured {inside} dBFS"
    assert outside > -10.0, f"expected the tone untouched outside the window, measured {outside} dBFS"


def test_duck_no_windows_leaves_track_unchanged(tmp_path):
    track = _tone(tmp_path / "tone.wav", 3.0)

    out_path = duck(track, [], tmp_path / "ducked.wav")

    assert probe_duration(out_path) == pytest.approx(3.0, abs=0.05)
    assert _peak_dbfs(out_path) == pytest.approx(_peak_dbfs(track), abs=0.5)


def test_duck_edge_does_not_click(tmp_path):
    track = _tone(tmp_path / "tone.wav", 6.0, freq=220)
    windows = [SilenceWindow(start=2.0, end=4.0)]

    out_path = duck(track, windows, tmp_path / "ducked.wav")

    samples = _read_samples(out_path)
    sample_rate = 44100
    boundary_indices = [int(2.0 * sample_rate), int(4.0 * sample_rate)]

    # Threshold justified: a hard (unramped) mute of a -6 dBFS tone (peak
    # ~16383) would jump the full sample value in a single step. A 30ms
    # linear ramp spreads that same drop across ~1300 samples, capping the
    # ramp's own contribution to any single delta at roughly peak/1300 (~13).
    # 800 is comfortably above ordinary sample-to-sample signal jitter for a
    # 220Hz tone at this sample rate, and comfortably below a real
    # discontinuity (thousands).
    threshold = 800
    for center in boundary_indices:
        window_samples = samples[max(0, center - 50) : center + 50]
        deltas = [abs(b - a) for a, b in zip(window_samples, window_samples[1:])]
        max_delta = max(deltas) if deltas else 0
        assert max_delta < threshold, f"click at boundary {center}: max delta {max_delta}"


def test_duck_window_covering_entire_track_is_pure_silence(tmp_path):
    track = _tone(tmp_path / "tone.wav", 3.0)
    windows = [SilenceWindow(start=0.0, end=3.0)]

    out_path = duck(track, windows, tmp_path / "ducked.wav")

    assert _peak_dbfs(out_path) < -50.0


# --- mix_audio ---------------------------------------------------------------------


def test_mix_audio_has_vo_duration_signal_and_no_clip(tmp_path):
    vo = _vo(tmp_path / "vo.wav", 4.0)
    bed = _tone(tmp_path / "bed.wav", 4.0, freq=80, peak_db=-20.0)
    sfx = _silence(tmp_path / "sfx.wav", 4.0)

    out_path = mix_audio(vo, bed, sfx, tmp_path / "mixed.wav")

    assert probe_duration(out_path) == pytest.approx(4.0, abs=0.1)
    assert _peak_dbfs(out_path) > -40.0
    assert _peak_dbfs(out_path) < 0.0


# --- build_mix -----------------------------------------------------------------------


def test_build_mix_with_sfx_bed_and_silence(tmp_path):
    duration = 10.0
    # SILENCE marker fires (its `seconds`) at the END of the gap -- see
    # silence_windows' docstring -- so arg="2.0s" at seconds=5.0 is the
    # window [3.0, 5.0], not [5.0, 7.0].
    # The narration convention this module documents: vo.wav already carries
    # true silence in [SILENCE:] windows. A VO fixture that keeps playing
    # through the window would mask the very thing this test measures.
    vo = _vo_with_silence(tmp_path / "vo.wav", duration, [(3.0, 5.0)])
    document = _document(
        duration,
        [
            _marker("MUSIC", "drone-low", seconds=0.0),
            _marker("SFX", "sub-drop", seconds=1.0),
            _marker("SILENCE", "2.0s", seconds=5.0),
        ],
    )

    out_path, findings = build_mix(
        document, vo, tmp_path / "mixed.wav", tmp_path / "work", STYLE_DIR
    )

    assert probe_duration(out_path) == pytest.approx(duration, abs=0.1)

    silence_peak = _segment_peak_dbfs(out_path, 3.3, 1.4)
    neighbour_peak = _segment_peak_dbfs(out_path, 6.5, 1.4)
    assert silence_peak < neighbour_peak - 10.0, (
        f"silence window ({silence_peak} dBFS) should be measurably quieter than "
        f"a neighbouring second ({neighbour_peak} dBFS)"
    )


def test_build_mix_warns_when_an_sfx_event_lands_inside_a_silence_window(tmp_path):
    # An SFX cue scheduled inside a [SILENCE:] window gets ducked to
    # nothing along with everything else -- the drop has to be total, no
    # exceptions, or the device is defeated. That's very likely an authoring
    # mistake (the cue can never be heard), so build_mix warns about it
    # rather than silently swallowing the cue with no trace.
    duration = 10.0
    vo = _vo_with_silence(tmp_path / "vo.wav", duration, [(3.0, 5.0)])
    document = _document(
        duration,
        [
            _marker("SFX", "sub-drop", seconds=4.0),  # inside [3.0, 5.0]
            _marker("SILENCE", "2.0s", seconds=5.0),
        ],
    )

    out_path, findings = build_mix(
        document, vo, tmp_path / "mixed.wav", tmp_path / "work", STYLE_DIR
    )

    warnings = [f for f in findings if f.severity == "warning"]
    assert any("sub-drop" in f.message and "silence" in f.message.lower() for f in warnings)


def test_build_mix_no_warning_when_sfx_event_is_outside_silence_windows(tmp_path):
    duration = 10.0
    vo = _vo_with_silence(tmp_path / "vo.wav", duration, [(3.0, 5.0)])
    document = _document(
        duration,
        [
            _marker("SFX", "sub-drop", seconds=1.0),  # outside [3.0, 5.0]
            _marker("SILENCE", "2.0s", seconds=5.0),
        ],
    )

    out_path, findings = build_mix(
        document, vo, tmp_path / "mixed.wav", tmp_path / "work", STYLE_DIR
    )

    assert not any("silence" in f.message.lower() for f in findings)


def test_build_mix_no_sfx_no_music_is_essentially_the_vo(tmp_path):
    duration = 4.0
    vo = _vo(tmp_path / "vo.wav", duration)
    document = _document(duration, [])

    out_path, findings = build_mix(
        document, vo, tmp_path / "mixed.wav", tmp_path / "work", STYLE_DIR
    )

    assert probe_duration(out_path) == pytest.approx(duration, abs=0.1)
    assert findings == []
    assert _peak_dbfs(out_path) == pytest.approx(_peak_dbfs(vo), abs=1.0)


# --- batching (MAX_AMIX_INPUTS) ------------------------------------------------------


def test_build_sfx_layer_batches_beyond_max_amix_inputs(tmp_path):
    from rabbithole.audiomix import MAX_AMIX_INPUTS

    n_events = MAX_AMIX_INPUTS + 5
    duration = float(n_events) + 2.0
    events = [SfxEvent(seconds=float(i) + 0.5, name="bass-thud") for i in range(n_events)]
    categories = load_sfx_categories(STYLE_DIR / "sfx.json")

    out_path, findings = build_sfx_layer(
        events, duration, tmp_path / "sfx.wav", tmp_path / "cache", categories
    )

    assert probe_duration(out_path) == pytest.approx(duration, abs=0.2)
    # Spot-check a couple of events actually made it into the mix.
    first = _segment_peak_dbfs(out_path, 0.5, 0.6)
    last = _segment_peak_dbfs(out_path, float(n_events - 1) + 0.5, 0.6)
    assert first > -40.0
    assert last > -40.0

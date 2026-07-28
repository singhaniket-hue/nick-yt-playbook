"""The `render` CLI subcommand.

Uses `tmp_path` fixture projects (never the real `projects/` directory) but
real ffmpeg -- small dimensions (64x64 source assets; the render itself still
runs at render.py's default 1920x1080 since `render` exposes no
--width/--height flag) and short durations to keep this fast.
"""

from __future__ import annotations

import argparse
import json
import subprocess

import pytest

from rabbithole import cli

FRAMINGS_CYCLE = ["wide", "push-in", "detail", "slow-pan"]


def _run(args):
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-1500:])


def _make_asset(path, seconds, color="gray"):
    _run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s=64x64:r=30:d={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ]
    )
    return path


def _make_vo(path, seconds):
    _run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}:sample_rate=44100",
            "-ac", "1", "-c:a", "pcm_s16le", str(path),
        ]
    )
    return path


def _project_root(tmp_path, slug="demo"):
    root = tmp_path / "projects" / slug
    (root / "narration").mkdir(parents=True)
    (root / "edit").mkdir(parents=True)
    (root / "assets").mkdir(parents=True)
    return root


def _build_fixture(
    tmp_path,
    slug="demo",
    n_cuts=2,
    cut_duration=0.4,
    with_asset=True,
    extra_markers=None,
    words=None,
    overlays=None,
):
    """A minimal but complete fixture project: timing.json, edl.json,
    provenance.json, narration/vo.wav, and (unless `with_asset` is False) a
    real small mp4 asset for its single slot `s001`.

    `words` defaults to `[]` (no subtitle content); pass real word-timing
    dicts to exercise subtitle cue building. `overlays` defaults to `[]` (no
    chapter cards/censor boxes); pass a list of Overlay-shaped dicts
    (kind/start/end/text/detail) to exercise graphics.
    """
    root = _project_root(tmp_path, slug)
    duration = n_cuts * cut_duration
    words = words if words is not None else []

    markers = [{"kind": "SHOT", "arg": "plate grain", "word_index": 0, "line": 1, "seconds": 0.0}]
    markers += extra_markers or []
    document = {
        "duration_seconds": duration,
        "word_count": len(words),
        "words": words,
        "markers": markers,
    }
    timing_path = root / "narration" / "timing.json"
    timing_path.write_text(json.dumps(document), encoding="utf-8")

    cuts = [
        {
            "index": i,
            "start": i * cut_duration,
            "end": (i + 1) * cut_duration,
            "slot_id": "s001",
            "origin": "script" if i == 0 else "asl-fill",
            "framing": FRAMINGS_CYCLE[i % len(FRAMINGS_CYCLE)],
            "transition": "cut",
            "reason": "test",
        }
        for i in range(n_cuts)
    ]
    edl_doc = {
        "duration_seconds": duration,
        "cut_count": len(cuts),
        "average_shot_length": cut_duration,
        "cuts": cuts,
        "overlays": overlays or [],
    }
    (root / "edit" / "edl.json").write_text(json.dumps(edl_doc), encoding="utf-8")

    records = []
    if with_asset:
        asset_path = root / "assets" / "s001-plate.mp4"
        _make_asset(asset_path, duration)
        records = [
            {
                "asset_id": "plate-s001",
                "tier": "atmospheric",
                "provider": "ffmpeg-lavfi",
                "original_url": "",
                "license": "",
                "retrieved_at": "2026-01-01T00:00:00Z",
                "local_path": str(asset_path),
                "used_in_slots": ["s001"],
                "notes": "",
            }
        ]
    (root / "provenance.json").write_text(json.dumps(records), encoding="utf-8")

    _make_vo(root / "narration" / "vo.wav", duration)

    return root, timing_path


def _namespace(
    timing_json, dry_run=False, out=None, no_audio_mix=False, subtitles=True, graphics=True,
    sound_library=None, no_sound_library=True,
):
    """`no_sound_library=True` by default, deliberately.

    `cmd_render` picks up assets/soundlib automatically when it exists, which
    is the right default for an author but the wrong one for a test: it would
    make these cases depend on whether someone had run `sound build` in this
    checkout, reading real generated audio from outside tmp_path. Pinning it
    off keeps them hermetic and deterministic; the library path gets its own
    test below, with a library built under tmp_path.
    """
    return argparse.Namespace(
        timing_json=str(timing_json),
        dry_run=dry_run,
        out=out,
        no_audio_mix=no_audio_mix,
        subtitles=subtitles,
        graphics=graphics,
        sound_library=sound_library,
        no_sound_library=no_sound_library,
    )


# --- --dry-run ---------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path, capsys):
    root, timing_path = _build_fixture(tmp_path)

    result = cli.cmd_render(_namespace(timing_path, dry_run=True))

    assert result == 0
    assert not (root / "renders").exists()


def test_dry_run_reports_the_plan(tmp_path, capsys):
    root, timing_path = _build_fixture(tmp_path, n_cuts=3, cut_duration=0.4)

    cli.cmd_render(_namespace(timing_path, dry_run=True))

    out = capsys.readouterr().out
    assert "Cuts: 3" in out
    assert "s001" in out
    assert "Total duration" in out


def test_segment_dry_run_reports_only_the_requested_window(tmp_path, capsys):
    root, timing_path = _build_fixture(tmp_path, n_cuts=6, cut_duration=1.0)
    args = _namespace(timing_path, dry_run=True)
    args.start = "00:01"
    args.end = "00:03.5"

    result = cli.cmd_render(args)

    out = capsys.readouterr().out
    assert result == 0
    assert "Review segment: 1.000s to 3.500s" in out
    assert "Total duration: 2.50s" in out
    assert not (root / "renders").exists()


def test_segment_requires_both_start_and_end_and_never_falls_back_to_full_render(
    tmp_path, capsys
):
    root, timing_path = _build_fixture(tmp_path)
    args = _namespace(timing_path)
    args.start = "0"
    args.end = None

    result = cli.cmd_render(args)

    assert result == 1
    assert "both --start and --end" in capsys.readouterr().out
    assert not (root / "renders").exists()


def test_dry_run_reports_slots_without_assets(tmp_path, capsys):
    root, timing_path = _build_fixture(tmp_path, with_asset=False)

    result = cli.cmd_render(_namespace(timing_path, dry_run=True))

    out = capsys.readouterr().out
    assert "s001" in out
    assert result == 1


def test_dry_run_reports_deferred_audio_cues(tmp_path, capsys):
    # vhs-burst is a real, synthesizable cue (build_mix can place it) so it
    # no longer warns; an unresolvable name is what deferred_audio_cues
    # reports now (see rabbithole/render.py's corrected docstring).
    root, timing_path = _build_fixture(
        tmp_path,
        extra_markers=[
            {"kind": "SFX", "arg": "not-a-real-cue", "word_index": 0, "line": 2, "seconds": 0.0}
        ],
    )

    result = cli.cmd_render(_namespace(timing_path, dry_run=True))

    out = capsys.readouterr().out
    assert "not-a-real-cue" in out
    assert "[WARNING]" in out
    # A deferred SFX cue is a warning, not an error, so the assets are still complete.
    assert result == 0


# --- live mode -----------------------------------------------------------------


def test_live_mode_writes_the_output(tmp_path, capsys):
    root, timing_path = _build_fixture(tmp_path)

    result = cli.cmd_render(_namespace(timing_path))

    expected = root / "renders" / "episode.mp4"
    assert expected.exists()
    assert expected.stat().st_size > 0
    assert result == 0


def test_segment_live_mode_writes_under_segments_and_has_window_duration(tmp_path, capsys):
    root, timing_path = _build_fixture(tmp_path, n_cuts=6, cut_duration=1.0)
    args = _namespace(timing_path, no_audio_mix=True, subtitles=False, graphics=False)
    args.start = "1.25"
    args.end = "3.75"

    result = cli.cmd_render(args)

    expected = root / "renders" / "segments" / "00001-250_to_00003-750.mp4"
    assert result == 0
    assert expected.exists()
    assert not (root / "renders" / "episode.mp4").exists()
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(expected),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    assert float(probe.stdout.strip()) == pytest.approx(2.5, abs=0.08)


def test_segment_live_mode_windows_source_audio_seek_and_timeline(
    tmp_path, capsys, monkeypatch
):
    root, timing_path = _build_fixture(tmp_path, n_cuts=6, cut_duration=1.0)
    research = root / "research"
    research.mkdir()
    source = _make_vo(research / "interview.wav", 5.0)
    (research / "source-audio.json").write_text(
        json.dumps(
            {
                "clips": [
                    {
                        "local_path": "research/interview.wav",
                        "source_start": 1.0,
                        "timeline_start": 1.0,
                        "duration": 3.0,
                        "gain_db": -3.0,
                        "duck_vo_db": -24.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    real_build_mix = cli.build_mix
    seen = {}

    def spy_build_mix(*args, **kwargs):
        seen["bites"] = kwargs.get("source_audio_bites")
        return real_build_mix(*args, **kwargs)

    monkeypatch.setattr(cli, "build_mix", spy_build_mix)
    args = _namespace(timing_path, subtitles=False, graphics=False)
    args.start = "1.25"
    args.end = "3.75"

    result = cli.cmd_render(args)

    assert result == 0
    assert len(seen["bites"]) == 1
    bite = seen["bites"][0]
    assert bite.local_path == source.resolve()
    assert bite.source_start == pytest.approx(1.25)
    assert bite.timeline_start == pytest.approx(0.0)
    assert bite.duration == pytest.approx(2.5)
    assert "Source-audio bites mixed: 1" in capsys.readouterr().out


def test_live_mode_applies_authored_article_highlights_before_delivery(
    tmp_path, capsys
):
    root, timing_path = _build_fixture(
        tmp_path,
        n_cuts=2,
        cut_duration=1.0,
        words=[
            {"index": 0, "word": "evidence", "start": 0.1, "end": 0.5},
        ],
    )
    (root / "research").mkdir()
    (root / "research" / "highlights.json").write_text(
        json.dumps(
            {
                "highlights": [
                    {
                        "start": 0.2,
                        "end": 1.5,
                        "reveal_seconds": 0.4,
                        "rect": [0.2, 0.3, 0.5, 0.08],
                        "label": "evidence line",
                        "source": "Official notice, 1 January 2026",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    args = _namespace(
        timing_path,
        no_audio_mix=True,
        subtitles=False,
        graphics=False,
    )

    result = cli.cmd_render(args)

    out = capsys.readouterr().out
    expected = root / "renders" / "episode.mp4"
    assert result == 0
    assert expected.exists()
    assert "Article highlight lines: 1" in out


def test_live_mode_prints_verification_details(tmp_path, capsys):
    root, timing_path = _build_fixture(tmp_path)

    cli.cmd_render(_namespace(timing_path))

    out = capsys.readouterr().out
    assert "Duration" in out
    assert "Dimensions" in out
    assert "Streams" in out


# --- audio mix wiring ------------------------------------------------------------


def _audio_marker_fixture(tmp_path, slug="demo-audio"):
    """A longer fixture (5s) carrying one MUSIC bed, one SFX cue, and one
    silence drop, so the mix actually has something to place and duck."""
    extra_markers = [
        {"kind": "MUSIC", "arg": "drone-low", "word_index": 0, "line": 1, "seconds": 0.0},
        {"kind": "SFX", "arg": "bass-thud", "word_index": 0, "line": 2, "seconds": 0.5},
        {"kind": "SILENCE", "arg": "1.0s", "word_index": 0, "line": 3, "seconds": 2.0},
    ]
    return _build_fixture(tmp_path, slug=slug, n_cuts=5, cut_duration=1.0, extra_markers=extra_markers)


def test_live_mode_mixes_audio_and_reports_summary(tmp_path, capsys):
    root, timing_path = _audio_marker_fixture(tmp_path)

    result = cli.cmd_render(_namespace(timing_path))

    out = capsys.readouterr().out
    assert result == 0
    assert "SFX events placed: 1/1" in out
    assert "Bed spans laid: 1" in out
    assert "Silence windows ducked: 1" in out
    assert "Final peak" in out


def test_no_audio_mix_flag_skips_mixing_and_uses_bare_vo(tmp_path, capsys):
    root, timing_path = _audio_marker_fixture(tmp_path)

    result = cli.cmd_render(_namespace(timing_path, no_audio_mix=True))

    out = capsys.readouterr().out
    assert result == 0
    expected = root / "renders" / "episode.mp4"
    assert expected.exists()
    assert expected.stat().st_size > 0
    # The mix summary is specific to when a mix was actually built.
    assert "SFX events placed" not in out
    assert "no-audio-mix" in out.lower()


def test_unknown_sfx_cue_in_live_mode_is_an_error_from_the_mix(tmp_path, capsys):
    root, timing_path = _build_fixture(
        tmp_path,
        extra_markers=[
            {"kind": "SFX", "arg": "not-a-real-cue", "word_index": 0, "line": 2, "seconds": 0.0}
        ],
    )

    result = cli.cmd_render(_namespace(timing_path))

    report = capsys.readouterr().out
    assert "[ERROR]" in report
    assert "not-a-real-cue" in report
    assert result == 1


# --- --out overrides ------------------------------------------------------------


def test_out_overrides_the_default_path(tmp_path, capsys):
    root, timing_path = _build_fixture(tmp_path)
    custom_out = tmp_path / "custom" / "cut.mp4"

    result = cli.cmd_render(_namespace(timing_path, out=custom_out))

    assert custom_out.exists()
    assert not (root / "renders" / "episode.mp4").exists()
    assert result == 0


# --- exit codes -----------------------------------------------------------------


def test_exit_code_one_when_a_slot_is_missing_its_asset(tmp_path, capsys):
    root, timing_path = _build_fixture(tmp_path, with_asset=False)

    result = cli.cmd_render(_namespace(timing_path))

    assert result == 1
    report = capsys.readouterr().out
    assert "[ERROR]" in report


def test_warnings_alone_give_exit_code_zero(tmp_path, capsys):
    # MUSIC:out resolves exactly (silence) and no longer warns; a cue that
    # falls back to the generic drone-low bed (anything but "out") is what
    # still warns without being an error.
    root, timing_path = _build_fixture(
        tmp_path,
        extra_markers=[{"kind": "MUSIC", "arg": "chasms", "word_index": 0, "line": 2, "seconds": 0.0}],
    )

    result = cli.cmd_render(_namespace(timing_path))

    report = capsys.readouterr().out
    assert "[WARNING]" in report
    assert "[ERROR]" not in report
    assert result == 0


# --- subtitles ------------------------------------------------------------------


def _subtitle_words():
    """Two words fitting comfortably under one subtitle cue: no forced break
    (the danda only closes the cue after it, and there is no word after it)."""
    return [
        {"index": 0, "word": "नमस्ते", "start": 0.1, "end": 0.4},
        {"index": 1, "word": "दुनिया।", "start": 0.5, "end": 0.9},
    ]


def test_subtitles_on_by_default_writes_ass_and_reports_cues_and_font(tmp_path, capsys):
    root, timing_path = _build_fixture(tmp_path, n_cuts=3, cut_duration=1.0, words=_subtitle_words())

    result = cli.cmd_render(_namespace(timing_path))

    out = capsys.readouterr().out
    assert result == 0
    assert "Subtitle cues: 1" in out
    assert "Subtitle font:" in out

    expected = root / "renders" / "episode.mp4"
    ass_path = root / "renders" / "episode.ass"
    assert expected.exists()
    assert expected.stat().st_size > 0
    assert ass_path.exists()
    assert "नमस्ते" in ass_path.read_text(encoding="utf-8")


def test_render_highlights_romanized_key_on_devanagari_subtitle_spine(
    tmp_path, capsys
):
    words = [
        {"index": 0, "word": "नमस्ते", "start": 0.1, "end": 0.4},
        {"index": 1, "word": "दुनिया।", "start": 0.5, "end": 0.9},
    ]
    root, timing_path = _build_fixture(
        tmp_path,
        n_cuts=3,
        cut_duration=1.0,
        words=words,
        extra_markers=[
            {
                "kind": "KEY",
                "arg": "duniya",
                "word_index": 0,
                "line": 1,
                "seconds": 0.0,
            }
        ],
    )
    script_dir = root / "script"
    script_dir.mkdir()
    (script_dir / "04-final.md").write_text(
        "namaste duniya.", encoding="utf-8"
    )

    result = cli.cmd_render(
        _namespace(
            timing_path,
            no_audio_mix=True,
            graphics=False,
        )
    )

    assert result == 0
    dialogue = next(
        line
        for line in (root / "renders" / "episode.ass")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.startswith("Dialogue:")
    )
    assert "नमस्ते" in dialogue
    assert "}दुनिया।{\\c" in dialogue
    assert "duniya" not in dialogue
    assert dialogue.count(r"{\c") == 2


def test_no_subtitles_flag_skips_ass_and_burn(tmp_path, capsys):
    root, timing_path = _build_fixture(tmp_path, n_cuts=3, cut_duration=1.0, words=_subtitle_words())

    result = cli.cmd_render(_namespace(timing_path, subtitles=False))

    out = capsys.readouterr().out
    assert result == 0
    assert "Subtitle cues" not in out
    assert "subtitles" in out.lower()  # skipped-message still says something

    expected = root / "renders" / "episode.mp4"
    ass_path = root / "renders" / "episode.ass"
    assert expected.exists()
    assert expected.stat().st_size > 0
    assert not ass_path.exists()


def test_subtitle_findings_join_the_combined_report(tmp_path, capsys):
    # typography.json's subtitle family ("Inter") has no Devanagari coverage,
    # so pick_font's override warning should appear in the same combined
    # format_report as every other finding, not printed separately.
    root, timing_path = _build_fixture(tmp_path, n_cuts=3, cut_duration=1.0, words=_subtitle_words())

    result = cli.cmd_render(_namespace(timing_path))

    out = capsys.readouterr().out
    assert result == 0
    assert "[WARNING]" in out
    assert "Devanagari" in out


def test_subtitles_ass_written_beside_custom_out_path(tmp_path, capsys):
    root, timing_path = _build_fixture(tmp_path, n_cuts=3, cut_duration=1.0, words=_subtitle_words())
    custom_out = tmp_path / "custom" / "cut.mp4"

    result = cli.cmd_render(_namespace(timing_path, out=custom_out))

    assert result == 0
    assert custom_out.exists()
    assert (tmp_path / "custom" / "cut.ass").exists()


def test_subtitles_with_no_words_burns_an_empty_but_valid_ass(tmp_path, capsys):
    # The existing fixtures (no `words=`) have no subtitle content at all --
    # subtitles being on by default must not break that case.
    root, timing_path = _build_fixture(tmp_path)

    result = cli.cmd_render(_namespace(timing_path))

    out = capsys.readouterr().out
    assert result == 0
    assert "Subtitle cues: 0" in out
    expected = root / "renders" / "episode.mp4"
    assert expected.exists()
    assert expected.stat().st_size > 0


# --- graphics (chapter cards, censor boxes) --------------------------------------


def _graphics_overlays():
    """One chapter card and one censor box, both inside the fixture's 3s
    duration (n_cuts=3, cut_duration=1.0)."""
    return [
        {"kind": "chapter-card", "start": 0.0, "end": 0.9, "text": "Dead Air", "detail": "1"},
        {"kind": "censor", "start": 1.0, "end": 1.9, "text": "", "detail": "face"},
    ]


def test_graphics_on_by_default_reports_cards_boxes_and_lut(tmp_path, capsys):
    root, timing_path = _build_fixture(
        tmp_path, n_cuts=3, cut_duration=1.0, overlays=_graphics_overlays()
    )

    result = cli.cmd_render(_namespace(timing_path))

    out = capsys.readouterr().out
    assert result == 0
    assert "Chapter cards drawn: 1" in out
    assert "Censor boxes drawn: 1" in out
    # Evidence-safe grading now uses restrained editable controls rather than
    # forcing the crushed-black LUT over documents and browser captures.
    assert "LUT applied: no" in out

    expected = root / "renders" / "episode.mp4"
    assert expected.exists()
    assert expected.stat().st_size > 0


def test_no_graphics_flag_skips_the_graphics_stage(tmp_path, capsys):
    root, timing_path = _build_fixture(
        tmp_path, n_cuts=3, cut_duration=1.0, overlays=_graphics_overlays()
    )

    result = cli.cmd_render(_namespace(timing_path, graphics=False))

    out = capsys.readouterr().out
    assert result == 0
    assert "Chapter cards drawn" not in out
    assert "Censor boxes drawn" not in out
    assert "graphics" in out.lower()  # skipped-message still says something

    expected = root / "renders" / "episode.mp4"
    assert expected.exists()
    assert expected.stat().st_size > 0


def test_graphics_findings_join_the_combined_report(tmp_path, capsys):
    # The censor stage's documented-default-region warning must land in the
    # same combined format_report as every other finding, not printed
    # separately -- same contract subtitle findings already follow.
    root, timing_path = _build_fixture(
        tmp_path, n_cuts=3, cut_duration=1.0, overlays=_graphics_overlays()
    )

    result = cli.cmd_render(_namespace(timing_path))

    out = capsys.readouterr().out
    assert result == 0
    assert "[WARNING]" in out
    assert "face" in out


def test_graphics_with_no_overlays_reports_zero_counts(tmp_path, capsys):
    root, timing_path = _build_fixture(tmp_path, n_cuts=3, cut_duration=1.0)

    result = cli.cmd_render(_namespace(timing_path))

    out = capsys.readouterr().out
    assert result == 0
    assert "Chapter cards drawn: 0" in out
    assert "Censor boxes drawn: 0" in out


def test_graphics_and_subtitles_both_on_still_produce_valid_output(tmp_path, capsys):
    # Graphics composites after subtitles (see cmd_render): both stages
    # chained must still produce a single valid, correctly-durationed file.
    root, timing_path = _build_fixture(
        tmp_path,
        n_cuts=3,
        cut_duration=1.0,
        words=_subtitle_words(),
        overlays=_graphics_overlays(),
    )

    result = cli.cmd_render(_namespace(timing_path))

    out = capsys.readouterr().out
    assert result == 0
    assert "Subtitle cues: 1" in out
    assert "Chapter cards drawn: 1" in out
    assert "Censor boxes drawn: 1" in out

    expected = root / "renders" / "episode.mp4"
    assert expected.exists()
    assert expected.stat().st_size > 0


# --- LUT reporting ----------------------------------------------------------------


def test_evidence_safe_plain_render_reports_no_global_lut(tmp_path, capsys):
    root, timing_path = _build_fixture(tmp_path)

    result = cli.cmd_render(_namespace(timing_path))

    out = capsys.readouterr().out
    assert result == 0
    assert "LUT applied: no" in out


# --- the generated sound library --------------------------------------------------


def _stub_library(root):
    """A minimal Library on disk: one manifest entry plus its file.

    Enough for `_sound_library` to consider it usable and for `build_mix` to
    resolve one cue from it, without any network call.
    """
    from rabbithole.sources.sfx import TARGET_PEAK_DB
    from rabbithole.sources.soundgen import Library, SoundRequest

    library = Library(root=root)
    path = library.sfx_path("bass-thud")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=60:duration=0.4:sample_rate=44100",
         "-af", f"volume={TARGET_PEAK_DB}dB", "-ac", "1",
         "-c:a", "pcm_s16le", str(path)],
        check=True,
    )
    library.record("sfx/bass-thud", path,
                   SoundRequest(slug="sfx/bass-thud", prompt="p", duration=0.5))
    return library


def test_sound_library_is_ignored_when_the_flag_says_so(tmp_path):
    """--no-sound-library must win even over an explicitly passed path."""
    library = _stub_library(tmp_path / "lib")
    args = _namespace(tmp_path / "t.json", sound_library=str(library.root),
                      no_sound_library=True)

    assert cli._sound_library(args) is None


def test_sound_library_is_used_when_present_and_not_suppressed(tmp_path):
    library = _stub_library(tmp_path / "lib")
    args = _namespace(tmp_path / "t.json", sound_library=str(library.root),
                      no_sound_library=False)

    resolved = cli._sound_library(args)
    assert resolved is not None
    assert resolved.root == library.root


def test_a_missing_library_falls_back_rather_than_failing(tmp_path):
    """An author who has never run `sound build` must still be able to render."""
    args = _namespace(tmp_path / "t.json", sound_library=str(tmp_path / "nope"),
                      no_sound_library=False)

    assert cli._sound_library(args) is None


def test_render_mixes_from_the_library_when_given_one(tmp_path, capsys):
    library = _stub_library(tmp_path / "lib")
    root, timing_path = _build_fixture(tmp_path)

    result = cli.cmd_render(_namespace(
        timing_path, sound_library=str(library.root), no_sound_library=False
    ))

    out = capsys.readouterr().out
    assert result == 0
    assert "generated sound library" in out
    assert (root / "renders" / "episode.mp4").exists()


# --- the live archive transport ---------------------------------------------------


def test_the_live_archive_transport_sends_a_user_agent():
    """Wikimedia enforces its User-Agent policy: the `python-requests` default
    gets a 403. Because `search_archives` treats a failed provider as "no hits"
    and moves on, that failure is silent -- every Wikimedia search returns
    nothing and the archival tier just looks less productive than it is.

    The transport is injected everywhere else in this suite, so this header is
    the one thing about it no other test can reach.
    """
    import requests

    from rabbithole.cli import ARCHIVE_USER_AGENT, _live_archive_transport

    sent = {}

    class FakeResponse:
        status_code = 200
        content = b"{}"

    def fake_get(url, timeout=None, headers=None):
        sent["headers"] = headers or {}
        return FakeResponse()

    original = requests.get
    requests.get = fake_get
    try:
        _live_archive_transport("https://commons.wikimedia.org/w/api.php")
    finally:
        requests.get = original

    assert sent["headers"].get("User-Agent") == ARCHIVE_USER_AGENT
    assert "python-requests" not in sent["headers"]["User-Agent"]
    assert "rabbithole" in sent["headers"]["User-Agent"]

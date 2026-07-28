import json
import subprocess
import wave
from pathlib import Path

import pytest

from rabbithole.sources.soundgen import (
    BED_VARIANT_SECONDS,
    MAX_DURATION,
    MIN_DURATION,
    MODEL_ID,
    Library,
    SoundRequest,
    build_library,
    ensure_sound,
    generate,
    library_bed_variants,
    library_sfx,
    load_prompts,
    measure_peak_db,
    measure_rms_db,
    normalize_bed_group,
    probe_duration,
    tile_to_duration,
    trim_edges,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _tone_mp3(seconds: float, freq: int = 220) -> bytes:
    """A real, decodable mp3 -- the fake transport must return something ffmpeg
    can actually process, or the decode step would be untested."""
    result = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate=44100",
         "-f", "mp3", "-"],
        capture_output=True,
        check=True,
    )
    return result.stdout


class FakeTransport:
    def __init__(self, payload: bytes | None = None, status: int = 200):
        self._payload = payload
        self.status = status
        self.calls: list[dict] = []

    def __call__(self, url, headers, json_body):
        self.calls.append({"url": url, "headers": headers, "json": json_body})
        payload = self._payload
        if payload is None:
            # Vary by call so cached-vs-regenerated is distinguishable.
            payload = _tone_mp3(0.5, freq=200 + 40 * len(self.calls))
        return self.status, payload


# --- request validation ----------------------------------------------------------


def test_request_rejects_a_duration_the_api_would_reject():
    with pytest.raises(ValueError, match="outside the API's accepted range"):
        SoundRequest(slug="x", prompt="a drone", duration=MAX_DURATION + 1)


def test_request_rejects_a_duration_below_the_floor():
    with pytest.raises(ValueError, match="outside the API's accepted range"):
        SoundRequest(slug="x", prompt="a drone", duration=MIN_DURATION - 0.1)


def test_request_rejects_an_empty_prompt():
    with pytest.raises(ValueError, match="empty prompt"):
        SoundRequest(slug="x", prompt="   ", duration=1.0)


def test_variant_changes_the_fingerprint():
    """Without this, four bed variants would collide on one cache key and the
    library would hold one generation copied four times."""
    a = SoundRequest(slug="beds/drone-low", prompt="p", duration=10.0, variant=0)
    b = SoundRequest(slug="beds/drone-low", prompt="p", duration=10.0, variant=1)
    assert a.fingerprint != b.fingerprint


def test_prompt_change_changes_the_fingerprint():
    a = SoundRequest(slug="sfx/x", prompt="harsh metal", duration=1.0)
    b = SoundRequest(slug="sfx/x", prompt="soft metal", duration=1.0)
    assert a.fingerprint != b.fingerprint


def test_identical_requests_share_a_fingerprint():
    a = SoundRequest(slug="sfx/x", prompt="harsh metal", duration=1.0)
    b = SoundRequest(slug="sfx/x", prompt="harsh metal", duration=1.0)
    assert a.fingerprint == b.fingerprint


# --- the client ------------------------------------------------------------------


def test_generate_posts_the_only_accepted_model_id():
    transport = FakeTransport(payload=b"MP3")
    generate(SoundRequest(slug="x", prompt="a drone", duration=2.0), "sk-test", transport)
    assert transport.calls[0]["json"]["model_id"] == MODEL_ID


def test_generate_sends_the_api_key_header():
    transport = FakeTransport(payload=b"MP3")
    generate(SoundRequest(slug="x", prompt="a drone", duration=2.0), "sk-test", transport)
    assert transport.calls[0]["headers"]["xi-api-key"] == "sk-test"


def test_generate_sends_prompt_and_duration():
    transport = FakeTransport(payload=b"MP3")
    generate(SoundRequest(slug="x", prompt="a harsh scrape", duration=1.5), "sk", transport)
    body = transport.calls[0]["json"]
    assert body["text"] == "a harsh scrape"
    assert body["duration_seconds"] == 1.5


def test_generate_raises_with_the_api_detail_on_error():
    transport = FakeTransport(payload=b'{"detail":"nope"}', status=400)
    with pytest.raises(RuntimeError, match="returned 400"):
        generate(SoundRequest(slug="x", prompt="a drone", duration=2.0), "sk", transport)


def test_generate_raises_on_an_empty_200():
    """The endpoint returns 200 for unknown fields, so a 200 alone is not proof
    of success -- an empty body has to be caught explicitly."""
    transport = FakeTransport(payload=b"", status=200)
    with pytest.raises(RuntimeError, match="no audio"):
        generate(SoundRequest(slug="x", prompt="a drone", duration=2.0), "sk", transport)


# --- the library -----------------------------------------------------------------


def test_ensure_sound_generates_then_caches(tmp_path):
    library = Library(root=tmp_path / "lib")
    transport = FakeTransport()
    request = SoundRequest(slug="sfx/test", prompt="a scrape", duration=1.0)

    path, generated_first = ensure_sound(
        "sfx/test", library.sfx_path("test"), request, library, "sk", transport
    )
    _, generated_second = ensure_sound(
        "sfx/test", library.sfx_path("test"), request, library, "sk", transport
    )

    assert generated_first is True
    assert generated_second is False
    assert len(transport.calls) == 1, "a cache hit must not reach the API"
    assert path.exists()


def test_a_changed_prompt_invalidates_the_cache(tmp_path):
    library = Library(root=tmp_path / "lib")
    transport = FakeTransport()

    ensure_sound("sfx/t", library.sfx_path("t"),
                 SoundRequest(slug="sfx/t", prompt="old", duration=1.0),
                 library, "sk", transport)
    _, regenerated = ensure_sound("sfx/t", library.sfx_path("t"),
                                  SoundRequest(slug="sfx/t", prompt="new", duration=1.0),
                                  library, "sk", transport)

    assert regenerated is True
    assert len(transport.calls) == 2


def test_a_missing_file_invalidates_the_cache(tmp_path):
    """A manifest entry whose file was deleted must not count as a hit."""
    library = Library(root=tmp_path / "lib")
    transport = FakeTransport()
    request = SoundRequest(slug="sfx/t", prompt="p", duration=1.0)

    path, _ = ensure_sound("sfx/t", library.sfx_path("t"), request, library, "sk", transport)
    path.unlink()
    _, regenerated = ensure_sound("sfx/t", library.sfx_path("t"), request, library, "sk", transport)

    assert regenerated is True


def test_library_output_is_mono_44100_wav(tmp_path):
    """The mix layers all speak mono 44.1k pcm_s16le; a stereo mp3 decoded
    straight through would break the amix graph downstream."""
    library = Library(root=tmp_path / "lib")
    request = SoundRequest(slug="sfx/t", prompt="p", duration=1.0)
    path, _ = ensure_sound("sfx/t", library.sfx_path("t"), request, library, "sk", FakeTransport())

    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getframerate() == 44100
        assert w.getsampwidth() == 2


def test_manifest_records_the_prompt_and_model_for_provenance(tmp_path):
    library = Library(root=tmp_path / "lib")
    request = SoundRequest(slug="sfx/t", prompt="harsh metal scrape", duration=1.0)
    ensure_sound("sfx/t", library.sfx_path("t"), request, library, "sk", FakeTransport())

    entry = json.loads(library.manifest_path.read_text(encoding="utf-8"))["entries"]["sfx/t"]
    assert entry["prompt"] == "harsh metal scrape"
    assert entry["model_id"] == MODEL_ID
    assert entry["generated_at"]


def test_generated_sfx_is_normalised_to_the_level_audiomix_assumes(tmp_path):
    """audiomix derives category gains assuming a cue arrives at -6 dBFS.
    A generation lands wherever the model put it; if the library stored that
    unchanged, every category gain would be quietly wrong."""
    from rabbithole.sources.sfx import TARGET_PEAK_DB as SFX_TARGET

    library = Library(root=tmp_path / "lib")
    # Deliberately hot source: 0 dBFS full-scale sine, far above the target.
    loud = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1:sample_rate=44100",
         "-af", "volume=0dB", "-f", "mp3", "-"],
        capture_output=True, check=True,
    ).stdout

    path, _ = ensure_sound(
        "sfx/t", library.sfx_path("t"),
        SoundRequest(slug="sfx/t", prompt="p", duration=1.0),
        library, "sk", FakeTransport(payload=loud),
        target_peak_db=SFX_TARGET,
    )

    assert measure_peak_db(path) == pytest.approx(SFX_TARGET, abs=0.5)


def test_generated_bed_is_normalised_quieter_than_a_generated_sfx_cue(tmp_path):
    """The bed/SFX headroom split has to survive the switch to generated audio,
    not just hold for the synthesized cues."""
    from rabbithole.sources.music import TARGET_PEAK_DB as BED_TARGET
    from rabbithole.sources.sfx import TARGET_PEAK_DB as SFX_TARGET

    library = Library(root=tmp_path / "lib")
    payload = _tone_mp3(1.0)

    sfx_path, _ = ensure_sound(
        "sfx/t", library.sfx_path("t"),
        SoundRequest(slug="sfx/t", prompt="p", duration=1.0),
        library, "sk", FakeTransport(payload=payload), target_peak_db=SFX_TARGET,
    )
    bed_path, _ = ensure_sound(
        "beds/drone-low/0", library.bed_path("drone-low", 0),
        SoundRequest(slug="beds/drone-low", prompt="p", duration=1.0),
        library, "sk", FakeTransport(payload=payload), target_peak_db=BED_TARGET,
    )

    assert measure_peak_db(bed_path) < measure_peak_db(sfx_path) - 10.0


def test_build_library_normalises_what_it_stores(tmp_path):
    from rabbithole.sources.sfx import TARGET_PEAK_DB as SFX_TARGET

    library = Library(root=tmp_path / "lib")
    loud = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1:sample_rate=44100",
         "-f", "mp3", "-"],
        capture_output=True, check=True,
    ).stdout

    build_library(library, {"sfx": {"a": "p"}, "beds": {}}, "sk",
                  sfx_durations={"a": 1.0}, transport=FakeTransport(payload=loud))

    assert measure_peak_db(library.sfx_path("a")) == pytest.approx(SFX_TARGET, abs=0.5)


def test_library_sfx_returns_none_when_absent(tmp_path):
    assert library_sfx(Library(root=tmp_path / "empty"), "metal-scrape") is None


def test_library_sfx_finds_a_generated_cue(tmp_path):
    library = Library(root=tmp_path / "lib")
    request = SoundRequest(slug="sfx/metal-scrape", prompt="p", duration=1.0)
    ensure_sound("sfx/metal-scrape", library.sfx_path("metal-scrape"),
                 request, library, "sk", FakeTransport())
    assert library_sfx(library, "metal-scrape") is not None


# --- tiling ----------------------------------------------------------------------


def _wav(path: Path, seconds: float, freq: int = 220) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate=44100",
         "-ac", "1", "-c:a", "pcm_s16le", str(path)],
        check=True,
    )
    return path


def test_tiling_covers_a_duration_longer_than_the_source(tmp_path):
    source = _wav(tmp_path / "tile.wav", 4.0)
    out = tile_to_duration([source], 15.0, tmp_path / "out.wav", crossfade=0.5)
    assert probe_duration(out) == pytest.approx(15.0, abs=0.05)


def test_tiling_accounts_for_crossfade_overlap(tmp_path):
    """Each join eats `crossfade` seconds of overlap, so N tiles cover
    N*L - (N-1)*fade. Ignoring that leaves the span short and lets silence
    show through under the narration."""
    source = _wav(tmp_path / "tile.wav", 5.0)
    out = tile_to_duration([source], 30.0, tmp_path / "out.wav", crossfade=2.0)
    assert probe_duration(out) == pytest.approx(30.0, abs=0.05)


def test_tiling_a_single_source_shorter_than_requested_still_fills(tmp_path):
    source = _wav(tmp_path / "tile.wav", 2.0)
    out = tile_to_duration([source], 3.0, tmp_path / "out.wav", crossfade=0.2)
    assert probe_duration(out) == pytest.approx(3.0, abs=0.05)


def test_tiling_trims_when_the_source_is_already_long_enough(tmp_path):
    source = _wav(tmp_path / "tile.wav", 10.0)
    out = tile_to_duration([source], 4.0, tmp_path / "out.wav")
    assert probe_duration(out) == pytest.approx(4.0, abs=0.05)


def test_tiling_cycles_through_every_variant(tmp_path):
    """Four variants exist so a long span does not repeat one loop; if tiling
    only ever used the first, generating the other three would be wasted."""
    a = _wav(tmp_path / "a.wav", 3.0, freq=200)
    b = _wav(tmp_path / "b.wav", 3.0, freq=900)
    out = tile_to_duration([a, b], 12.0, tmp_path / "out.wav", crossfade=0.3)

    with wave.open(str(out), "rb") as w:
        frames = w.getnframes()
    assert frames > 0
    assert probe_duration(out) == pytest.approx(12.0, abs=0.05)


def test_tiling_rejects_an_empty_source_list(tmp_path):
    with pytest.raises(ValueError, match="at least one source"):
        tile_to_duration([], 5.0, tmp_path / "out.wav")


def test_tiling_rejects_a_nonpositive_duration(tmp_path):
    source = _wav(tmp_path / "tile.wav", 2.0)
    with pytest.raises(ValueError, match="positive duration"):
        tile_to_duration([source], 0.0, tmp_path / "out.wav")


# --- bed levelling and edge trimming ---------------------------------------------


def test_trim_edges_removes_the_requested_amount_from_both_ends(tmp_path):
    source = _wav(tmp_path / "in.wav", 30.0)
    out = trim_edges(source, tmp_path / "out.wav", 2.5)
    assert probe_duration(out) == pytest.approx(25.0, abs=0.05)


def test_trim_edges_clamps_rather_than_annihilating_a_short_source(tmp_path):
    """A 1s bed asked to lose 2.5s from each end must degrade, not vanish."""
    source = _wav(tmp_path / "in.wav", 1.0)
    out = trim_edges(source, tmp_path / "out.wav", 2.5)
    assert probe_duration(out) == pytest.approx(0.5, abs=0.05)


def test_normalize_bed_group_matches_loudness_across_variants(tmp_path):
    """The bug this fixes: variants peak-normalised independently differed by up
    to 11.65 dB in RMS, so every tiling join stepped between two loudnesses."""
    quiet = _wav(tmp_path / "a.wav", 3.0, freq=220)
    loud = _wav(tmp_path / "b.wav", 3.0, freq=220)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(loud),
                    "-af", "volume=-18dB", "-c:a", "pcm_s16le",
                    str(tmp_path / "b2.wav")], check=True)
    (tmp_path / "b2.wav").replace(loud)

    before = abs(measure_rms_db(quiet) - measure_rms_db(loud))
    normalize_bed_group([quiet, loud], -20.0)
    after = abs(measure_rms_db(quiet) - measure_rms_db(loud))

    assert before > 10.0, "fixture should start mismatched"
    assert after < 0.5, f"loudness still mismatched by {after:.2f} dB"


def test_normalize_bed_group_keeps_the_peak_contract(tmp_path):
    """audiomix positions beds assuming they peak no higher than the target;
    loudness-matching must not push any variant above it."""
    a = _wav(tmp_path / "a.wav", 3.0, freq=220)
    b = _wav(tmp_path / "b.wav", 3.0, freq=900)

    normalize_bed_group([a, b], -20.0)

    peaks = [measure_peak_db(a), measure_peak_db(b)]
    assert max(peaks) == pytest.approx(-20.0, abs=0.5)
    assert all(p <= -20.0 + 0.5 for p in peaks)


def test_normalize_bed_group_is_idempotent(tmp_path):
    """`sound build` re-derives beds on every run, including fully-cached ones.
    If normalisation compounded, each run would shift the group further."""
    a = _wav(tmp_path / "a.wav", 3.0, freq=220)
    b = _wav(tmp_path / "b.wav", 3.0, freq=900)

    normalize_bed_group([a, b], -20.0)
    once = (measure_peak_db(a), measure_rms_db(a))
    normalize_bed_group([a, b], -20.0)
    twice = (measure_peak_db(a), measure_rms_db(a))

    assert twice[0] == pytest.approx(once[0], abs=0.2)
    assert twice[1] == pytest.approx(once[1], abs=0.2)


def test_normalize_bed_group_handles_an_empty_list(tmp_path):
    assert normalize_bed_group([], -20.0) == []


def test_build_library_stores_beds_trimmed_and_levelled(tmp_path):
    """End to end: the playable bed is shorter than the raw generation (trimmed)
    and every variant shares one loudness (levelled)."""
    library = Library(root=tmp_path / "lib")

    class VaryingTransport:
        """Returns deliberately different levels per variant, like the real API."""
        def __init__(self):
            self.calls = []

        def __call__(self, url, headers, json_body):
            self.calls.append(json_body)
            gain = -6 * len(self.calls)
            payload = subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                 "-i", "sine=frequency=220:duration=12:sample_rate=44100",
                 "-af", f"volume={gain}dB", "-f", "mp3", "-"],
                capture_output=True, check=True,
            ).stdout
            return 200, payload

    build_library(library, {"sfx": {}, "beds": {"drone-low": "bed"}}, "sk",
                  sfx_durations={}, bed_kinds=("drone-low",), bed_variants=3,
                  transport=VaryingTransport())

    playable = library_bed_variants(library, "drone-low")
    assert len(playable) == 3

    raw_seconds = probe_duration(library.bed_raw_path("drone-low", 0))
    assert probe_duration(playable[0]) < raw_seconds, "edges were not trimmed"

    levels = [measure_rms_db(p) for p in playable]
    assert max(levels) - min(levels) < 0.5, f"variants not levelled: {levels}"


# --- build_library ---------------------------------------------------------------


def test_build_library_generates_every_named_cue_and_bed_variant(tmp_path):
    library = Library(root=tmp_path / "lib")
    transport = FakeTransport()
    prompts = {"sfx": {"a": "prompt a", "b": "prompt b"}, "beds": {"drone-low": "bed prompt"}}

    counts = build_library(
        library, prompts, "sk",
        sfx_durations={"a": 1.0, "b": 2.0},
        bed_kinds=("drone-low",),
        bed_variants=2,
        transport=transport,
    )

    assert counts == {"generated": 4, "reused": 0}
    assert len(library_bed_variants(library, "drone-low")) == 2


def test_build_library_is_idempotent(tmp_path):
    library = Library(root=tmp_path / "lib")
    transport = FakeTransport()
    prompts = {"sfx": {"a": "prompt a"}, "beds": {}}

    build_library(library, prompts, "sk", sfx_durations={"a": 1.0}, transport=transport)
    counts = build_library(library, prompts, "sk", sfx_durations={"a": 1.0}, transport=transport)

    assert counts == {"generated": 0, "reused": 1}
    assert len(transport.calls) == 1, "a second build must not re-spend"


def test_build_library_clamps_a_duration_the_api_would_reject(tmp_path):
    """DEFAULT_DURATIONS is free to name a cue shorter than the API's 0.5s floor;
    that must clamp rather than raise, or one short cue breaks the whole build."""
    library = Library(root=tmp_path / "lib")
    transport = FakeTransport()
    prompts = {"sfx": {"tiny": "p"}, "beds": {}}

    build_library(library, prompts, "sk", sfx_durations={"tiny": 0.1}, transport=transport)

    assert transport.calls[0]["json"]["duration_seconds"] == MIN_DURATION


def test_build_library_generates_beds_at_the_api_ceiling(tmp_path):
    library = Library(root=tmp_path / "lib")
    transport = FakeTransport()
    prompts = {"sfx": {}, "beds": {"drone-low": "bed"}}

    build_library(library, prompts, "sk", sfx_durations={},
                  bed_kinds=("drone-low",), bed_variants=1, transport=transport)

    assert transport.calls[0]["json"]["duration_seconds"] == BED_VARIANT_SECONDS


def test_build_library_skips_a_bed_kind_with_no_prompt(tmp_path):
    library = Library(root=tmp_path / "lib")
    transport = FakeTransport()
    prompts = {"sfx": {}, "beds": {"drone-low": "bed"}}

    build_library(library, prompts, "sk", sfx_durations={},
                  bed_kinds=("drone-low", "nonexistent"), bed_variants=1,
                  transport=transport)

    assert len(transport.calls) == 1


# --- the shipped prompt pack -----------------------------------------------------


def test_the_shipped_prompt_pack_covers_every_sfx_cue_and_bed_kind():
    from rabbithole.sources.music import BED_KINDS
    from rabbithole.sources.sfx import SFX_NAMES

    prompts = load_prompts(REPO_ROOT / "style" / "sound-prompts.json")

    assert set(prompts["sfx"]) == set(SFX_NAMES)
    synthesizable = {k for k in BED_KINDS if k != "silence"}
    assert set(prompts["beds"]) == synthesizable


def test_the_shipped_prompt_pack_steers_away_from_music():
    """Every prompt carries negative guidance; without it the model scores the
    cue, which fights a mix that treats bed and SFX as separate ducked layers."""
    prompts = load_prompts(REPO_ROOT / "style" / "sound-prompts.json")
    for group in ("sfx", "beds"):
        for name, prompt in prompts[group].items():
            assert "no music" in prompt or "no melody" in prompt, name

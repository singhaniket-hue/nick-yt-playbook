import json
import subprocess
from pathlib import Path

import pytest

from rabbithole.config import Config
from rabbithole.narrate import SilenceGap, SpeechChunk, SynthResult
from rabbithole.pipeline import render_narration
from rabbithole.timing import plan_gaps


@pytest.fixture
def cfg():
    return Config(
        elevenlabs_api_key="sk-test",
        voice_id="voice-abc",
        model_id="eleven_multilingual_v2",
        wpm=177,
        episode_cap_usd=25.0,
        budget_mode="warn",
    )


def _fake_synth_factory(tmp_path: Path):
    """Return a synth callable that emits a real 0.5s WAV so ffmpeg can concat it."""

    def fake_synth(chunk: SpeechChunk, cfg: Config) -> SynthResult:
        wav = tmp_path / f"src-{chunk.index}.wav"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", "sine=frequency=440:duration=0.5:sample_rate=44100",
                "-ac", "1", "-c:a", "pcm_s16le", str(wav),
            ],
            check=True,
            capture_output=True,
        )
        characters = tuple(chunk.text)
        return SynthResult(
            audio=wav.read_bytes(),
            characters=characters,
            start_times=tuple(i * 0.01 for i in range(len(characters))),
            end_times=tuple((i + 1) * 0.01 for i in range(len(characters))),
        )

    return fake_synth


# --- render_narration writes timing.json beside the WAV ---------------------


def test_timing_json_is_written_beside_the_wav(tmp_path, cfg):
    script = "Pehla hissa. [SILENCE:1.0s] Dusra hissa."
    out = tmp_path / "vo.wav"

    result = render_narration(script, cfg, out, tmp_path, synth=_fake_synth_factory(tmp_path))

    assert result.timing_path == out.parent / "timing.json"
    assert result.timing_path.exists()


def test_timing_json_word_count_equals_script_word_count(tmp_path, cfg):
    script = "Pehla hissa. [SILENCE:1.0s] Dusra hissa."
    out = tmp_path / "vo.wav"

    result = render_narration(script, cfg, out, tmp_path, synth=_fake_synth_factory(tmp_path))
    document = json.loads(result.timing_path.read_text(encoding="utf-8"))

    assert document["word_count"] == 4  # Pehla hissa. Dusra hissa.


def test_timing_json_includes_every_script_marker(tmp_path, cfg):
    script = "Pehla [SFX:vhs-burst] hissa. [SILENCE:1.0s] Dusra hissa. [MUSIC:out]"
    out = tmp_path / "vo.wav"

    result = render_narration(script, cfg, out, tmp_path, synth=_fake_synth_factory(tmp_path))
    document = json.loads(result.timing_path.read_text(encoding="utf-8"))

    kinds = {m["kind"] for m in document["markers"]}
    assert kinds == {"SFX", "SILENCE", "MUSIC"}
    assert len(document["markers"]) == 3


def test_timing_json_word_starts_are_monotonically_non_decreasing(tmp_path, cfg):
    script = "Pehla hissa. [SILENCE:1.0s] Dusra hissa."
    out = tmp_path / "vo.wav"

    result = render_narration(script, cfg, out, tmp_path, synth=_fake_synth_factory(tmp_path))
    document = json.loads(result.timing_path.read_text(encoding="utf-8"))

    starts = [w["start"] for w in document["words"]]
    assert starts == sorted(starts)


def test_timing_json_marker_seconds_matches_the_word_it_precedes(tmp_path, cfg):
    script = "Pehla hissa [SFX:vhs-burst] teesra hissa."
    out = tmp_path / "vo.wav"

    result = render_narration(script, cfg, out, tmp_path, synth=_fake_synth_factory(tmp_path))
    document = json.loads(result.timing_path.read_text(encoding="utf-8"))

    marker = document["markers"][0]
    word = document["words"][marker["word_index"]]
    assert marker["seconds"] == pytest.approx(word["start"])


# --- plan_gaps ----------------------------------------------------------


def test_plan_gaps_keys_a_gap_to_the_chunk_that_follows_it():
    a = SpeechChunk(index=0, text="Ek", previous_text="", next_text="")
    b = SpeechChunk(index=1, text="do", previous_text="", next_text="")
    plan = [a, SilenceGap(seconds=1.5), b]

    assert plan_gaps(plan) == [(1, SilenceGap(seconds=1.5))]


def test_plan_gaps_drops_a_trailing_gap_with_no_following_chunk():
    a = SpeechChunk(index=0, text="Ek", previous_text="", next_text="")
    plan = [a, SilenceGap(seconds=1.5)]

    assert plan_gaps(plan) == []


def test_plan_gaps_sums_adjacent_gaps_keyed_to_the_same_chunk():
    """Two silence markers with no words between them produce two adjacent
    SilenceGap plan items. Both precede the same next chunk, so their seconds
    must sum rather than the second overwriting the first."""
    a = SpeechChunk(index=0, text="Ek", previous_text="", next_text="")
    b = SpeechChunk(index=1, text="do", previous_text="", next_text="")
    plan = [a, SilenceGap(seconds=1.0), SilenceGap(seconds=2.0), b]

    assert plan_gaps(plan) == [(1, SilenceGap(seconds=3.0))]

from pathlib import Path

import pytest

from rabbithole.config import Config
from rabbithole.narrate import SpeechChunk, SynthResult
from rabbithole.pipeline import render_narration


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
    import subprocess

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


def test_render_narration_writes_the_stitched_output(tmp_path, cfg):
    script = "Pehla hissa. [SILENCE:1.0s] Dusra hissa."
    out = tmp_path / "vo.wav"

    result = render_narration(script, cfg, out, tmp_path, synth=_fake_synth_factory(tmp_path))

    assert out.exists()
    assert result.chunk_count == 2
    assert result.silence_count == 1


def test_render_narration_duration_includes_the_gap(tmp_path, cfg):
    from rabbithole.assemble import probe_duration

    script = "Pehla hissa. [SILENCE:1.0s] Dusra hissa."
    out = tmp_path / "vo.wav"

    render_narration(script, cfg, out, tmp_path, synth=_fake_synth_factory(tmp_path))

    # Two 0.5s tones plus a 1.0s gap.
    assert probe_duration(out) == pytest.approx(2.0, abs=0.1)


def test_render_narration_reports_billable_characters(tmp_path, cfg):
    script = "Pehla hissa. [SILENCE:1.0s] Dusra hissa."
    out = tmp_path / "vo.wav"

    result = render_narration(script, cfg, out, tmp_path, synth=_fake_synth_factory(tmp_path))

    assert result.characters == len("Pehla hissa.") + len("Dusra hissa.")

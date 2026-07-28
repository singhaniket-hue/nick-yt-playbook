import base64
import json

import pytest

from rabbithole.config import Config
from rabbithole.narrate import SpeechChunk, SynthResult, synthesize


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


def _alignment_payload(text: str, audio: bytes = b"AUDIO") -> bytes:
    """Build a response shaped like the ElevenLabs with-timestamps endpoint."""
    return json.dumps(
        {
            "audio_base64": base64.b64encode(audio).decode("ascii"),
            "alignment": {
                "characters": list(text),
                "character_start_times_seconds": [i * 0.1 for i in range(len(text))],
                "character_end_times_seconds": [(i + 1) * 0.1 for i in range(len(text))],
            },
        }
    ).encode("utf-8")


class FakeTransport:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.calls = []

    def __call__(self, url, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.status, self.payload


def test_synthesize_posts_to_the_timestamps_endpoint(cfg):
    transport = FakeTransport(_alignment_payload("Ek do."))
    chunk = SpeechChunk(index=0, text="Ek do.", previous_text="", next_text="")

    synthesize(chunk, cfg, transport=transport)

    assert "/with-timestamps" in transport.calls[0]["url"]


def test_synthesize_returns_audio_and_alignment(cfg):
    transport = FakeTransport(_alignment_payload("Ek do.", audio=b"MP3DATA"))
    chunk = SpeechChunk(index=0, text="Ek do.", previous_text="", next_text="")

    result = synthesize(chunk, cfg, transport=transport)

    assert isinstance(result, SynthResult)
    assert result.audio == b"MP3DATA"
    assert result.characters == tuple("Ek do.")
    assert result.start_times[0] == pytest.approx(0.0)


def test_synthesize_alignment_arrays_are_same_length_as_characters(cfg):
    text = "Ek do teen."
    transport = FakeTransport(_alignment_payload(text))
    chunk = SpeechChunk(index=0, text=text, previous_text="", next_text="")

    result = synthesize(chunk, cfg, transport=transport)

    assert len(result.characters) == len(result.start_times) == len(result.end_times)


def test_synthesize_raises_on_error_status(cfg):
    transport = FakeTransport(b"quota exceeded", status=401)
    chunk = SpeechChunk(index=0, text="Ek.", previous_text="", next_text="")

    with pytest.raises(RuntimeError, match="401"):
        synthesize(chunk, cfg, transport=transport)


def test_synthesize_raises_when_alignment_is_missing(cfg):
    transport = FakeTransport(json.dumps({"audio_base64": "QUJD"}).encode("utf-8"))
    chunk = SpeechChunk(index=0, text="Ek.", previous_text="", next_text="")

    with pytest.raises(RuntimeError, match="alignment"):
        synthesize(chunk, cfg, transport=transport)


def test_synthesize_raises_when_audio_is_missing(cfg):
    payload = json.dumps(
        {"alignment": {"characters": ["a"], "character_start_times_seconds": [0.0], "character_end_times_seconds": [0.1]}}
    ).encode("utf-8")
    transport = FakeTransport(payload)
    chunk = SpeechChunk(index=0, text="a", previous_text="", next_text="")

    with pytest.raises(RuntimeError, match="audio"):
        synthesize(chunk, cfg, transport=transport)

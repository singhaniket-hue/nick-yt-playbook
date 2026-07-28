import base64
import json as json_module

import pytest

from rabbithole.config import Config
from rabbithole.narrate import SpeechChunk, SynthResult, estimate_characters, synthesize


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
    return json_module.dumps(
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
    def __init__(self, payload=None, status=200):
        self.payload = payload if payload is not None else _alignment_payload("AUDIO")
        self.status = status
        self.calls = []

    def __call__(self, url, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.status, self.payload


def test_synthesize_posts_to_the_configured_voice(cfg):
    transport = FakeTransport()
    chunk = SpeechChunk(index=0, text="Namaste.", previous_text="", next_text="")

    synthesize(chunk, cfg, transport=transport)

    assert "/text-to-speech/voice-abc" in transport.calls[0]["url"]


def test_synthesize_sends_api_key_header(cfg):
    transport = FakeTransport()
    chunk = SpeechChunk(index=0, text="Namaste.", previous_text="", next_text="")

    synthesize(chunk, cfg, transport=transport)

    assert transport.calls[0]["headers"]["xi-api-key"] == "sk-test"


def test_synthesize_sends_model_and_stitch_context(cfg):
    transport = FakeTransport()
    chunk = SpeechChunk(index=1, text="Do.", previous_text="Ek.", next_text="Teen.")

    synthesize(chunk, cfg, transport=transport)

    body = transport.calls[0]["json"]
    assert body["model_id"] == "eleven_multilingual_v2"
    assert body["text"] == "Do."
    assert body["previous_text"] == "Ek."
    assert body["next_text"] == "Teen."


def test_synthesize_omits_empty_stitch_fields(cfg):
    transport = FakeTransport()
    chunk = SpeechChunk(index=0, text="Ek.", previous_text="", next_text="")

    synthesize(chunk, cfg, transport=transport)
    body = transport.calls[0]["json"]

    assert "previous_text" not in body
    assert "next_text" not in body


def test_synthesize_returns_audio_bytes(cfg):
    transport = FakeTransport(payload=_alignment_payload("Ek.", audio=b"MP3DATA"))
    chunk = SpeechChunk(index=0, text="Ek.", previous_text="", next_text="")

    result = synthesize(chunk, cfg, transport=transport)

    assert isinstance(result, SynthResult)
    assert result.audio == b"MP3DATA"


def test_synthesize_raises_on_error_status(cfg):
    transport = FakeTransport(payload=b"quota exceeded", status=401)
    chunk = SpeechChunk(index=0, text="Ek.", previous_text="", next_text="")

    with pytest.raises(RuntimeError, match="401"):
        synthesize(chunk, cfg, transport=transport)


def test_estimate_characters_counts_spoken_text_only():
    plan = [
        SpeechChunk(index=0, text="12345", previous_text="ignored", next_text="ignored"),
        SpeechChunk(index=1, text="123", previous_text="", next_text=""),
    ]

    assert estimate_characters(plan) == 8

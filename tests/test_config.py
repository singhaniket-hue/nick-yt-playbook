import pytest

from rabbithole.config import Config, load_config


def test_load_config_reads_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "ELEVENLABS_API_KEY=sk-test-123\n"
        "RABBITHOLE_VOICE_ID=voice-abc\n"
        "RABBITHOLE_WPM=180\n",
        encoding="utf-8",
    )

    cfg = load_config(env)

    assert isinstance(cfg, Config)
    assert cfg.elevenlabs_api_key == "sk-test-123"
    assert cfg.voice_id == "voice-abc"
    assert cfg.wpm == 180


def test_load_config_applies_non_account_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("RABBITHOLE_MODEL_ID", raising=False)
    monkeypatch.delenv("RABBITHOLE_WPM", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "ELEVENLABS_API_KEY=sk-test-123\nRABBITHOLE_VOICE_ID=voice-abc\n",
        encoding="utf-8",
    )

    cfg = load_config(env)

    assert cfg.voice_id == "voice-abc"
    assert cfg.model_id == "eleven_multilingual_v2"
    assert cfg.wpm == 177


def test_load_config_rejects_missing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("RABBITHOLE_WPM=177\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY"):
        load_config(env)


def test_load_config_rejects_missing_voice_id(tmp_path, monkeypatch):
    monkeypatch.delenv("RABBITHOLE_VOICE_ID", raising=False)
    env = tmp_path / ".env"
    env.write_text("ELEVENLABS_API_KEY=sk-test-123\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="RABBITHOLE_VOICE_ID"):
        load_config(env)


def test_load_config_allows_missing_voice_id_when_not_required(tmp_path, monkeypatch):
    monkeypatch.delenv("RABBITHOLE_VOICE_ID", raising=False)
    env = tmp_path / ".env"
    env.write_text("ELEVENLABS_API_KEY=sk-test-123\n", encoding="utf-8")

    cfg = load_config(env, require_voice_id=False)

    assert cfg.elevenlabs_api_key == "sk-test-123"
    assert cfg.voice_id == ""


def test_load_config_still_requires_api_key_without_voice_requirement(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("RABBITHOLE_VOICE_ID", raising=False)
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY"):
        load_config(env, require_voice_id=False)


def test_empty_env_value_falls_back_to_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-from-shell")
    monkeypatch.setenv("RABBITHOLE_VOICE_ID", "voice-abc")
    env = tmp_path / ".env"
    env.write_text("ELEVENLABS_API_KEY=\n", encoding="utf-8")

    cfg = load_config(env)

    assert cfg.elevenlabs_api_key == "sk-from-shell"


def test_filled_env_value_overrides_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-from-shell")
    monkeypatch.setenv("RABBITHOLE_VOICE_ID", "voice-abc")
    env = tmp_path / ".env"
    env.write_text("ELEVENLABS_API_KEY=sk-from-file\n", encoding="utf-8")

    cfg = load_config(env)

    assert cfg.elevenlabs_api_key == "sk-from-file"

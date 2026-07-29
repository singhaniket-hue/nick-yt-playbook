from argparse import Namespace

from rabbithole import cli
from rabbithole.config import Config
from rabbithole.sources import soundgen


def test_sound_loads_api_credentials_without_requiring_voice_id(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        soundgen,
        "load_prompts",
        lambda _path: {"sfx": {}, "beds": {}},
    )
    monkeypatch.setattr(
        soundgen,
        "build_library",
        lambda *_args, **_kwargs: {"generated": 0, "reused": 0},
    )

    require_voice_id_calls = []

    def fake_load_config(*, require_voice_id=True):
        require_voice_id_calls.append(require_voice_id)
        return Config(
            elevenlabs_api_key="sk-test",
            voice_id="",
            model_id="eleven_multilingual_v2",
            wpm=177,
            episode_cap_usd=25.0,
            budget_mode="warn",
        )

    monkeypatch.setattr(cli, "load_config", fake_load_config)

    result = cli.cmd_sound(
        Namespace(
            library=str(tmp_path / "soundlib"),
            bed_variants=0,
            dry_run=False,
        )
    )

    assert result == 0
    assert require_voice_id_calls == [False]

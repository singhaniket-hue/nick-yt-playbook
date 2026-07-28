import argparse
import json
from pathlib import Path

import pytest

from rabbithole import cli
from rabbithole.config import Config
from rabbithole.markers import parse
from rabbithole.validate import (
    ACT_BUDGETS,
    WORD_COUNT_MAX,
    WORD_COUNT_MIN,
    ValidationProfile,
    ValidationProfileError,
    validate_all,
    validation_profile_for_script,
)


def _script_for(profile: ValidationProfile, word: str = "shabd") -> str:
    names = {1: "Cold Open", 2: "Origin", 3: "Rabbit Hole", 4: "Climax", 5: "Outro"}
    parts: list[str] = []
    for act, count in profile.act_budgets.items():
        parts.append(f"[ACT:{act} {names[act]}]")
        if act == 3:
            # 600 words is 3.39 minutes at 177 WPM, inside the 3-5 minute gate.
            parts.append(" ".join([word] * 600))
            parts.append("[REHOOK]")
            parts.append(" ".join([word] * (count - 600)))
        else:
            parts.append(" ".join([word] * count))
    return "\n".join(parts)


def _write_project(tmp_path, brief: object) -> tuple[Path, Path]:
    project = tmp_path / "projects" / "episode"
    script_dir = project / "script"
    script_dir.mkdir(parents=True)
    script_path = script_dir / "04-final.md"
    (project / "brief.json").write_text(json.dumps(brief), encoding="utf-8")
    return project, script_path


def test_short_profile_derives_word_target_and_scales_all_five_acts():
    profile = ValidationProfile.for_duration(16, wpm=177)

    assert profile.target_word_count == 2832
    assert (profile.word_count_min, profile.word_count_max) == (2408, 3256)
    assert dict(profile.act_budgets) == {1: 43, 2: 207, 3: 1583, 4: 749, 5: 250}
    assert sum(profile.act_budgets.values()) == profile.target_word_count


def test_short_profile_passes_the_same_combined_validation_gates():
    profile = ValidationProfile.for_duration(16, wpm=177)

    assert validate_all(
        parse(_script_for(profile)),
        sfx_names=set(),
        profile=profile,
    ) == []


def test_approved_brief_can_supply_wpm_tolerance_and_act_word_budgets(tmp_path):
    budgets = {"1": 138, "2": 344, "3": 1210, "4": 825, "5": 124}
    _, script_path = _write_project(
        tmp_path,
        {
            "target_duration_minutes": 16,
            "target_wpm": 165,
            "validation": {
                "word_count_tolerance": 0.15,
                "act_word_budgets": budgets,
            },
        },
    )

    profile = validation_profile_for_script(script_path)

    assert profile.wpm == 165
    assert profile.target_word_count == 2640
    assert (profile.word_count_min, profile.word_count_max) == (2244, 3036)
    assert dict(profile.act_budgets) == {
        1: 138,
        2: 344,
        3: 1210,
        4: 825,
        5: 124,
    }


def test_act_durations_are_converted_with_the_selected_wpm(tmp_path):
    _, script_path = _write_project(
        tmp_path,
        {
            "target_duration_minutes": 16,
            "target_wpm": 165,
            "validation": {
                "act_duration_seconds": {
                    "1": 50,
                    "2": 125,
                    "3": 440,
                    "4": 300,
                    "5": 45,
                }
            },
        },
    )

    profile = validation_profile_for_script(script_path)

    assert dict(profile.act_budgets) == {
        1: 138,
        2: 344,
        3: 1210,
        4: 825,
        5: 124,
    }


def test_explicit_wpm_overrides_the_brief_target_wpm(tmp_path):
    _, script_path = _write_project(
        tmp_path,
        {"target_duration_minutes": 16, "target_wpm": 165},
    )

    profile = validation_profile_for_script(
        script_path, wpm=180, fallback_wpm=177
    )

    assert profile.wpm == 180
    assert profile.target_word_count == 2880


def test_no_brief_preserves_exact_long_format_defaults(tmp_path):
    script_path = tmp_path / "project" / "script" / "04-final.md"

    profile = validation_profile_for_script(script_path, wpm=177)

    assert profile.target_duration_minutes is None
    assert (profile.word_count_min, profile.word_count_max) == (
        WORD_COUNT_MIN,
        WORD_COUNT_MAX,
    )
    assert dict(profile.act_budgets) == ACT_BUDGETS


def test_well_formed_brief_without_target_falls_back_to_defaults(tmp_path):
    _, script_path = _write_project(tmp_path, {"slug": "episode"})

    profile = validation_profile_for_script(script_path, wpm=177)

    assert not profile.is_brief_aware
    assert dict(profile.act_budgets) == ACT_BUDGETS


def test_present_but_malformed_brief_fails_closed(tmp_path):
    project, script_path = _write_project(tmp_path, {"target_duration_minutes": 16})
    (project / "brief.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(ValidationProfileError, match="Cannot load validation target"):
        validation_profile_for_script(script_path, wpm=177)


@pytest.mark.parametrize("bad_target", [0, -1, "16", True])
def test_invalid_present_duration_is_an_error_not_a_silent_fallback(
    tmp_path, bad_target
):
    _, script_path = _write_project(
        tmp_path, {"target_duration_minutes": bad_target}
    )

    with pytest.raises(ValidationProfileError, match="positive finite number"):
        validation_profile_for_script(script_path, wpm=177)


@pytest.mark.parametrize(
    "validation",
    [
        {"act_word_budgets": {"1": 138}},
        {"act_word_budgets": None},
        {"act_duration_seconds": None},
        {
            "act_word_budgets": {
                "1": 138,
                "2": 344,
                "3": "1210",
                "4": 825,
                "5": 124,
            }
        },
        {"word_count_tolerance": 1.5},
        {
            "act_word_budgets": {
                "1": 1000,
                "2": 1000,
                "3": 1000,
                "4": 1000,
                "5": 1000,
            }
        },
        {
            "act_word_budgets": {
                "1": 138,
                "2": 344,
                "3": 1210,
                "4": 825,
                "5": 124,
            },
            "act_duration_seconds": {
                "1": 50,
                "2": 125,
                "3": 440,
                "4": 300,
                "5": 45,
            },
        },
    ],
)
def test_malformed_explicit_validation_values_fail_closed(
    tmp_path, validation
):
    _, script_path = _write_project(
        tmp_path,
        {
            "target_duration_minutes": 16,
            "target_wpm": 165,
            "validation": validation,
        },
    )

    with pytest.raises(ValidationProfileError):
        validation_profile_for_script(script_path)


def test_narrate_dry_run_uses_the_same_brief_aware_profile(
    tmp_path, monkeypatch, capsys
):
    budgets = {"1": 138, "2": 344, "3": 1210, "4": 825, "5": 124}
    project, script_path = _write_project(
        tmp_path,
        {
            "target_duration_minutes": 16,
            "target_wpm": 165,
            "validation": {"act_word_budgets": budgets},
        },
    )
    profile = validation_profile_for_script(script_path)
    script_path.write_text(_script_for(profile), encoding="utf-8")
    (script_path.parent / "05-devanagari.md").write_text(
        _script_for(profile, word="शब्द"), encoding="utf-8"
    )
    (script_path.parent / "latin-terms.json").write_text(
        json.dumps({"terms": []}), encoding="utf-8"
    )
    (project / "claims.json").write_text("[]", encoding="utf-8")

    cfg = Config(
        elevenlabs_api_key="sk-test",
        voice_id="voice-test",
        model_id="eleven_multilingual_v2",
        wpm=177,
        episode_cap_usd=25.0,
        budget_mode="warn",
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    observed: list[ValidationProfile] = []
    real_validate_all = cli.validate_all

    def capture_profile(*args, profile=None, **kwargs):
        observed.append(profile)
        return real_validate_all(*args, profile=profile, **kwargs)

    monkeypatch.setattr(cli, "validate_all", capture_profile)
    result = cli.cmd_narrate(
        argparse.Namespace(
            script=str(script_path),
            out=str(project / "narration" / "vo.wav"),
            dry_run=True,
            force=False,
        )
    )

    assert result == 0
    assert len(observed) == 1
    assert observed[0].target_duration_minutes == 16
    assert observed[0].wpm == 165
    assert observed[0].target_word_count == 2640
    assert dict(observed[0].act_budgets) == {
        1: 138,
        2: 344,
        3: 1210,
        4: 825,
        5: 124,
    }
    assert "Billable characters:" in capsys.readouterr().out


def test_narrate_force_cannot_bypass_a_malformed_brief(
    tmp_path, monkeypatch, capsys
):
    project, script_path = _write_project(
        tmp_path, {"target_duration_minutes": 16}
    )
    script_path.write_text("[ACT:1 Cold Open] shabd", encoding="utf-8")
    (project / "brief.json").write_text("{broken", encoding="utf-8")

    cfg = Config(
        elevenlabs_api_key="sk-test",
        voice_id="voice-test",
        model_id="eleven_multilingual_v2",
        wpm=177,
        episode_cap_usd=25.0,
        budget_mode="warn",
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    result = cli.cmd_narrate(
        argparse.Namespace(
            script=str(script_path),
            out=str(project / "narration" / "vo.wav"),
            dry_run=True,
            force=True,
            wpm=None,
        )
    )

    report = capsys.readouterr().out
    assert result == 1
    assert "[brief_profile]" in report
    assert "Refusing to narrate" in report

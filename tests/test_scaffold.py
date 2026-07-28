import json

import pytest

from rabbithole.scaffold import new_project


def test_new_project_creates_the_directory_tree(tmp_path):
    root = new_project(tmp_path, "aviloop-hindi")

    for sub in ("research", "script", "narration", "assets", "edit", "checkpoints", "renders"):
        assert (root / sub).is_dir()


def test_new_project_seeds_empty_ledgers(tmp_path):
    root = new_project(tmp_path, "aviloop-hindi")

    assert json.loads((root / "claims.json").read_text(encoding="utf-8")) == []
    assert json.loads((root / "provenance.json").read_text(encoding="utf-8")) == []


def test_new_project_writes_a_brief_stub(tmp_path):
    root = new_project(tmp_path, "aviloop-hindi")
    brief = json.loads((root / "brief.json").read_text(encoding="utf-8"))

    assert brief["slug"] == "aviloop-hindi"
    assert brief["target_duration_minutes"] == 34
    assert brief["language"] == "hinglish"


def test_new_project_writes_the_act_skeleton(tmp_path):
    root = new_project(tmp_path, "aviloop-hindi")
    skeleton = (root / "script" / "01-beat-sheet.md").read_text(encoding="utf-8")

    for act in range(1, 6):
        assert f"[ACT:{act} " in skeleton


def test_new_project_refuses_to_clobber(tmp_path):
    new_project(tmp_path, "aviloop-hindi")

    with pytest.raises(FileExistsError):
        new_project(tmp_path, "aviloop-hindi")

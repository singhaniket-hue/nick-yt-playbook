from __future__ import annotations

import json
from pathlib import Path
import stat
import zipfile

import pytest

from rabbithole.episode_bundle import (
    EpisodeBundleValidationError,
    UnsafeEpisodeDestinationError,
    package_episode,
    restore_episode_bundle,
    validate_episode_bundle,
)


def _episode(root: Path) -> Path:
    root.mkdir()
    (root / "assets").mkdir()
    (root / "assets" / "clip.mp4").write_bytes(b"portable media")
    (root / "narration").mkdir()
    (root / "narration" / "vo.wav").write_bytes(b"voice")
    (root / "empty").mkdir()
    (root / "brief.json").write_text(
        json.dumps({"title": "Portable episode"}), encoding="utf-8"
    )
    (root / "provenance.json").write_text(
        json.dumps(
            [
                {
                    "asset_id": "clip",
                    "local_path": "assets/clip.mp4",
                    "original_url": "https://example.test/source",
                }
            ]
        ),
        encoding="utf-8",
    )
    return root


def test_package_is_deterministic_portable_and_excludes_machine_state(
    tmp_path: Path,
) -> None:
    episode = _episode(tmp_path / "episode")
    (episode / ".env").write_text("ELEVENLABS_API_KEY=secret", encoding="utf-8")
    (episode / ".env.local").write_text("TOKEN=secret", encoding="utf-8")
    (episode / "renders").mkdir()
    (episode / "renders" / "final.mp4").write_bytes(b"render")
    (episode / "handoffs").mkdir()
    (episode / "handoffs" / "old.zip").write_bytes(b"handoff")
    (episode / ".cache").mkdir()
    (episode / ".cache" / "index").write_bytes(b"cache")
    (episode / "resolve" / "builds" / "b-123").mkdir(parents=True)
    (episode / "resolve" / "builds" / "b-123" / "timeline.fcpxml").write_text(
        "generated", encoding="utf-8"
    )
    (episode / "resolve" / "queue").mkdir()
    (episode / "resolve" / "queue" / "job.json").write_text(
        "{}", encoding="utf-8"
    )
    (episode / "resolve" / ".resolve-runner.lock").write_text(
        "locked", encoding="utf-8"
    )

    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    result = package_episode(episode, first)
    package_episode(episode, second)

    assert result["valid"] is True
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert "project/assets/clip.mp4" in names
        assert "project/empty/" in names
        assert "project/.rabbithole-bundle/manifest.json" in names
        assert "project/.rabbithole-bundle/checksums.sha256" in names
        assert "project/.rabbithole-bundle/README.txt" in names
        joined = "\n".join(names)
        assert ".env" not in joined
        assert "renders/" not in joined
        assert "handoffs/" not in joined
        assert ".cache/" not in joined
        assert "resolve/builds/" not in joined
        assert "resolve/queue/" not in joined
        assert ".resolve-runner.lock" not in joined
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())


def test_restore_preserves_project_tree_and_refuses_overwrite(tmp_path: Path) -> None:
    episode = _episode(tmp_path / "episode")
    bundle = tmp_path / "episode.zip"
    package_episode(episode, bundle)

    destination = tmp_path / "mac-mini" / "episode"
    result = restore_episode_bundle(bundle, destination)
    assert result["valid"] is True
    assert (destination / "assets" / "clip.mp4").read_bytes() == b"portable media"
    assert (destination / "narration" / "vo.wav").read_bytes() == b"voice"
    assert (destination / "empty").is_dir()
    assert (destination / ".rabbithole-bundle" / "manifest.json").is_file()

    with pytest.raises(UnsafeEpisodeDestinationError, match="refusing overwrite"):
        restore_episode_bundle(bundle, destination)


@pytest.mark.parametrize(
    "local_path",
    [
        "../outside.mp4",
        "/private/tmp/outside.mp4",
        "C:/Users/editor/outside.mp4",
        r"assets\clip.mp4",
    ],
)
def test_package_rejects_nonportable_local_path_metadata(
    tmp_path: Path,
    local_path: str,
) -> None:
    episode = _episode(tmp_path / "episode")
    (episode / "provenance.json").write_text(
        json.dumps([{"asset_id": "clip", "local_path": local_path}]),
        encoding="utf-8",
    )
    with pytest.raises(EpisodeBundleValidationError):
        package_episode(episode, tmp_path / "episode.zip")
    assert not (tmp_path / "episode.zip").exists()


def test_package_rejects_missing_or_excluded_local_path_media(
    tmp_path: Path,
) -> None:
    episode = _episode(tmp_path / "episode")
    (episode / "provenance.json").write_text(
        json.dumps([{"asset_id": "missing", "local_path": "assets/missing.mp4"}]),
        encoding="utf-8",
    )
    with pytest.raises(
        EpisodeBundleValidationError, match="local_path media is missing"
    ):
        package_episode(episode, tmp_path / "missing.zip")

    (episode / "renders").mkdir()
    (episode / "renders" / "draft.mp4").write_bytes(b"excluded")
    (episode / "provenance.json").write_text(
        json.dumps([{"asset_id": "draft", "local_path": "renders/draft.mp4"}]),
        encoding="utf-8",
    )
    with pytest.raises(
        EpisodeBundleValidationError, match="local_path media is excluded"
    ):
        package_episode(episode, tmp_path / "excluded.zip")


def test_package_rejects_symlink_without_creating_bundle(tmp_path: Path) -> None:
    episode = _episode(tmp_path / "episode")
    external = tmp_path / "external.txt"
    external.write_text("outside", encoding="utf-8")
    try:
        (episode / "assets" / "external.txt").symlink_to(external)
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")

    output = tmp_path / "episode.zip"
    with pytest.raises(EpisodeBundleValidationError, match="symlinks"):
        package_episode(episode, output)
    assert not output.exists()


def test_validation_detects_tamper_and_zip_slip(tmp_path: Path) -> None:
    episode = _episode(tmp_path / "episode")
    good = tmp_path / "good.zip"
    package_episode(episode, good)

    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(good) as source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            payload = source.read(info)
            if info.filename == "project/assets/clip.mp4":
                payload = b"tampered media"
            target.writestr(info, payload)
    with pytest.raises(EpisodeBundleValidationError, match="checksum mismatch"):
        validate_episode_bundle(tampered)

    malicious = tmp_path / "malicious.zip"
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr("project/../escape.txt", "no")
    with pytest.raises(EpisodeBundleValidationError, match="escaping"):
        validate_episode_bundle(malicious)
    assert not (tmp_path / "escape.txt").exists()


def test_validation_rejects_zip_symlink_and_existing_output(
    tmp_path: Path,
) -> None:
    episode = _episode(tmp_path / "episode")
    existing = tmp_path / "existing.zip"
    existing.write_bytes(b"keep")
    with pytest.raises(UnsafeEpisodeDestinationError, match="refusing overwrite"):
        package_episode(episode, existing)
    assert existing.read_bytes() == b"keep"

    with pytest.raises(
        UnsafeEpisodeDestinationError, match="outside the project root"
    ):
        package_episode(episode, episode / "self.zip")

    malicious = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("project/link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr(info, "outside")
    with pytest.raises(EpisodeBundleValidationError, match="symlink"):
        validate_episode_bundle(malicious)

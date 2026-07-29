from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import zipfile

import pytest

import rabbithole.episode_bundle as episode_bundle_module
from rabbithole.episode_bundle import (
    EpisodeBundleValidationError,
    UnsafeEpisodeDestinationError,
    package_episode,
    restore_episode_bundle,
    validate_episode_bundle,
)
from rabbithole.resolve_audio import (
    _fingerprint,
    _input_contract,
    prepare_resolve_audio_stems,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_audio_stems(root: Path) -> str:
    timing = root / "narration" / "timing.json"
    timing.write_text("{}", encoding="utf-8")
    sound_manifest = root / "assets" / "soundlib" / "manifest.json"
    sound_manifest.parent.mkdir(parents=True)
    sound_manifest.write_text('{"entries":{}}', encoding="utf-8")
    style_path = Path(__file__).resolve().parents[1] / "style" / "sfx.json"
    contract = _input_contract(
        root,
        timing,
        root / "narration" / "vo.wav",
        sound_manifest,
        style_path,
        [],
    )
    fingerprint = _fingerprint(contract)
    stems = root / "resolve" / "audio-stems" / fingerprint
    stems.mkdir(parents=True)
    music = stems / "music-stem.wav"
    sfx = stems / "sfx-stem.wav"
    music.write_bytes(b"immutable music")
    sfx.write_bytes(b"immutable sfx")
    manifest = {
        "schema_version": "resolve-audio-stems.v1",
        "generator_version": "resolve-audio-stems.v1",
        "fingerprint": fingerprint,
        "duration_seconds": 1.0,
        "contract": contract,
        "entries": {
            "music": {
                "path": music.name,
                "sha256": _sha256(music),
                "duration_seconds": 1.0,
                "channels": 1,
                "sample_rate": 44_100,
                "codec": "pcm_s16le",
                "track": "A3",
                "kind": "music",
            },
            "sfx": {
                "path": sfx.name,
                "sha256": _sha256(sfx),
                "duration_seconds": 1.0,
                "channels": 1,
                "sample_rate": 44_100,
                "codec": "pcm_s16le",
                "track": "A4",
                "kind": "sfx",
            },
        },
    }
    stem_manifest = stems / "manifest.json"
    stem_manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (root / "resolve" / "audio-stems" / "current.json").write_text(
        json.dumps(
            {
                "schema_version": "resolve-audio-stems.v1",
                "fingerprint": fingerprint,
                "manifest_path": f"{fingerprint}/manifest.json",
                "manifest_sha256": _sha256(stem_manifest),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return fingerprint


def _episode(root: Path) -> Path:
    root.mkdir()
    (root / "assets").mkdir()
    (root / "assets" / "clip.mp4").write_bytes(b"portable media")
    (root / "narration").mkdir()
    (root / "narration" / "vo.wav").write_bytes(b"voice")
    (root / "script").mkdir()
    (root / "script" / "latin-terms.json").write_text(
        json.dumps({"terms": ["account", "YouTube"]}),
        encoding="utf-8",
    )
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
    (episode / "resolve" / "current.json").write_text(
        '{"plan_path":"resolve/builds/b-123/resolve-plan.v1.json"}',
        encoding="utf-8",
    )
    fingerprint = _write_audio_stems(episode)
    historical = (
        episode
        / "resolve"
        / "audio-stems"
        / ("f" * 64)
    )
    historical.mkdir()
    (historical / "manifest.json").write_bytes(b"historical manifest")
    (historical / "music-stem.wav").write_bytes(b"historical music")
    (historical / "sfx-stem.wav").write_bytes(b"historical sfx")

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
        assert "project/script/latin-terms.json" in names
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
        assert "project/resolve/current.json" not in names
        assert "project/resolve/audio-stems/current.json" in names
        assert (
            f"project/resolve/audio-stems/{fingerprint}/music-stem.wav" in names
        )
        assert not any(f"audio-stems/{'f' * 64}/" in name for name in names)
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())


def test_restored_audio_stem_selection_is_reusable_without_bundled_repo_style(
    tmp_path: Path,
) -> None:
    episode = _episode(tmp_path / "episode")
    fingerprint = _write_audio_stems(episode)
    bundle = tmp_path / "episode.zip"
    package_episode(episode, bundle)
    destination = tmp_path / "mac-mini" / "episode"

    restore_episode_bundle(bundle, destination)
    result = prepare_resolve_audio_stems(
        destination,
        destination / "assets" / "soundlib" / "manifest.json",
    )

    assert result["generated"] is False
    assert result["fingerprint"] == fingerprint
    assert not (destination / "style" / "sfx.json").exists()


def test_package_rejects_audio_stems_with_stale_project_input(
    tmp_path: Path,
) -> None:
    episode = _episode(tmp_path / "episode")
    _write_audio_stems(episode)
    (episode / "narration" / "timing.json").write_text(
        '{"duration_seconds":2}', encoding="utf-8"
    )

    with pytest.raises(EpisodeBundleValidationError, match="stale or changed"):
        package_episode(episode, tmp_path / "stale.zip")


def test_bundle_validation_rejects_self_consistent_zip_with_broken_stem_pointer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    episode = _episode(tmp_path / "episode")
    _write_audio_stems(episode)
    pointer_path = episode / "resolve" / "audio-stems" / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["manifest_path"] = "missing/manifest.json"
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    bundle = tmp_path / "broken-pointer.zip"

    real_validate = episode_bundle_module.validate_episode_bundle
    monkeypatch.setattr(
        episode_bundle_module,
        "_audit_local_audio_stems",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        episode_bundle_module,
        "validate_episode_bundle",
        lambda _path: {"valid": True},
    )
    episode_bundle_module.package_episode(episode, bundle)

    with pytest.raises(
        EpisodeBundleValidationError,
        match="manifest_path must select the immutable fingerprint directory",
    ):
        real_validate(bundle)


def test_restore_preserves_project_tree_and_refuses_overwrite(tmp_path: Path) -> None:
    episode = _episode(tmp_path / "episode")
    bundle = tmp_path / "episode.zip"
    package_episode(episode, bundle)

    destination = tmp_path / "mac-mini" / "episode"
    result = restore_episode_bundle(bundle, destination)
    assert result["valid"] is True
    assert (destination / "assets" / "clip.mp4").read_bytes() == b"portable media"
    assert (destination / "narration" / "vo.wav").read_bytes() == b"voice"
    assert json.loads(
        (destination / "script" / "latin-terms.json").read_text(encoding="utf-8")
    ) == {"terms": ["account", "YouTube"]}
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

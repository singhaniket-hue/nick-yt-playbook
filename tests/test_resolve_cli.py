import rabbithole.cli as cli
import rabbithole.resolve_cli as resolve_cli


def test_nested_resolve_status_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        resolve_cli,
        "project_status",
        lambda project_root: {"state": "idle", "project_root": str(project_root)},
    )
    result = cli.main(["resolve", "status", str(tmp_path)])
    assert result == 0
    assert '"state": "idle"' in capsys.readouterr().out


def test_general_render_defaults_to_ffmpeg_until_resolve_pilot_passes(
    monkeypatch, tmp_path
):
    observed = {}

    def fake_ffmpeg(args):
        observed["backend"] = args.backend
        return 7

    monkeypatch.setattr(cli, "cmd_render", fake_ffmpeg)
    result = cli.main(["render", str(tmp_path / "timing.json"), "--dry-run"])
    assert result == 7
    assert observed["backend"] == "ffmpeg"


def test_resolve_doctor_reports_without_opening_resolve(monkeypatch, capsys):
    monkeypatch.setattr(
        resolve_cli,
        "portability_report",
        lambda *, mode: {
            "ok": True,
            "mode": mode,
            "safe": {
                "resolve_started": False,
                "render_queue_touched": False,
                "project_mutated": False,
            },
        },
    )

    result = cli.main(["resolve", "doctor", "--mode", "studio"])

    assert result == 0
    output = capsys.readouterr().out
    assert '"mode": "studio"' in output
    assert '"resolve_started": false' in output


def test_resolve_bundle_command_delegates_to_portable_packager(
    tmp_path, monkeypatch, capsys
):
    observed = {}

    def fake_package(project_root, output):
        observed.update(project_root=project_root, output=output)
        return {"valid": True, "bundle_path": str(output)}

    monkeypatch.setattr(resolve_cli, "package_episode", fake_package)
    project = tmp_path / "episode"
    output = tmp_path / "episode.zip"

    result = cli.main(
        ["resolve", "bundle", str(project), "--out", str(output)]
    )

    assert result == 0
    assert observed == {"project_root": str(project), "output": str(output)}
    assert '"valid": true' in capsys.readouterr().out

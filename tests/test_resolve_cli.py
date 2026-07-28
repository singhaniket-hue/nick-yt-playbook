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

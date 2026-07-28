from pathlib import Path

from rabbithole.resolve_install import install_style_assets, style_asset_pairs


def _sources(root: Path) -> tuple[Path, Path]:
    setting = (
        root
        / "resolve"
        / "Fusion"
        / "Templates"
        / "Edit"
        / "Titles"
        / "Demo.setting"
    )
    lut = root / "style" / "luts" / "crowley-noir.cube"
    setting.parent.mkdir(parents=True)
    lut.parent.mkdir(parents=True)
    setting.write_text("{ Tools = ordered() {} }\n", encoding="utf-8")
    lut.write_text('TITLE "demo"\nLUT_3D_SIZE 2\n', encoding="utf-8")
    return setting, lut


def test_style_assets_install_idempotently_and_backup_differences(tmp_path):
    source_root = tmp_path / "source"
    setting, lut = _sources(source_root)
    support = tmp_path / "support"

    pairs = style_asset_pairs(source_root=source_root, support_root=support)
    assert {source for source, _ in pairs} == {setting, lut}
    first = install_style_assets(pairs=pairs)
    assert all(item["changed"] for item in first)
    assert all(Path(str(item["target"])).is_file() for item in first)

    second = install_style_assets(pairs=pairs)
    assert not any(item["changed"] for item in second)

    target = Path(str(first[0]["target"]))
    target.write_text("editor customization\n", encoding="utf-8")
    third = install_style_assets(pairs=pairs)
    changed = next(item for item in third if item["target"] == str(target))
    assert changed["changed"] is True
    assert Path(str(changed["backup"])).read_text(encoding="utf-8") == "editor customization\n"
    assert target.read_bytes() == setting.read_bytes()


def test_optional_drx_is_installed_only_when_present(tmp_path):
    source_root = tmp_path / "source"
    _sources(source_root)
    support = tmp_path / "support"

    without_drx = style_asset_pairs(
        source_root=source_root, support_root=support
    )
    assert not any(source.suffix == ".drx" for source, _ in without_drx)

    drx = source_root / "resolve" / "grades" / "crowley_v1.drx"
    drx.parent.mkdir(parents=True)
    drx.write_bytes(b"reviewed grade")
    with_drx = style_asset_pairs(source_root=source_root, support_root=support)
    source, target = next(
        pair for pair in with_drx if pair[0].suffix == ".drx"
    )
    assert source == drx
    assert target == support / "RabbitHole" / "grades" / drx.name

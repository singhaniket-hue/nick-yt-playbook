"""Install reusable Resolve runner/style assets without touching projects."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shutil
from typing import Iterable

from .resolve_platform import user_resolve_support_root as _platform_support_root


class ResolveInstallError(RuntimeError):
    """A Resolve support directory or source asset is unsafe/unavailable."""


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def user_resolve_support_root() -> Path:
    return _platform_support_root().resolve(strict=False)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _backup_name(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{counter}")
        counter += 1
    return candidate


def _copy_reviewed_file(source: Path, target: Path) -> dict[str, str | bool | None]:
    if not source.is_file():
        raise ResolveInstallError(f"Resolve asset is missing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and _sha256(source) == _sha256(target):
        return {
            "source": os.fspath(source),
            "target": os.fspath(target),
            "changed": False,
            "backup": None,
        }

    backup: Path | None = None
    if target.exists():
        if not target.is_file():
            raise ResolveInstallError(f"Resolve asset target is not a file: {target}")
        backup = _backup_name(target)
        shutil.copy2(target, backup)

    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return {
        "source": os.fspath(source),
        "target": os.fspath(target),
        "changed": True,
        "backup": os.fspath(backup) if backup else None,
    }


def style_asset_pairs(
    *,
    source_root: Path | None = None,
    support_root: Path | None = None,
) -> list[tuple[Path, Path]]:
    repo = (source_root or repository_root()).resolve()
    support = (support_root or user_resolve_support_root()).resolve()
    fusion_source = repo / "resolve" / "Fusion"
    pairs: list[tuple[Path, Path]] = []
    if fusion_source.is_dir():
        for source in sorted(path for path in fusion_source.rglob("*") if path.is_file()):
            relative = source.relative_to(fusion_source)
            pairs.append((source, support / "Fusion" / relative))
    lut = repo / "style" / "luts" / "crowley-noir.cube"
    if lut.is_file():
        pairs.append((lut, support / "LUT" / "RabbitHole" / lut.name))
    drx = repo / "resolve" / "grades" / "crowley_v1.drx"
    if drx.is_file():
        pairs.append((drx, support / "RabbitHole" / "grades" / drx.name))
    return pairs


def install_style_assets(
    *,
    source_root: Path | None = None,
    support_root: Path | None = None,
    pairs: Iterable[tuple[Path, Path]] | None = None,
) -> list[dict[str, str | bool | None]]:
    """Install templates/LUTs, backing up only differing existing files."""

    selected = list(
        pairs
        if pairs is not None
        else style_asset_pairs(source_root=source_root, support_root=support_root)
    )
    if not selected:
        raise ResolveInstallError("no reusable Resolve style assets were found")
    return [_copy_reviewed_file(source.resolve(), target.resolve()) for source, target in selected]


__all__ = [
    "ResolveInstallError",
    "install_style_assets",
    "repository_root",
    "style_asset_pairs",
    "user_resolve_support_root",
]

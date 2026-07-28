"""Create tiny, synthetic Resolve-demo media without network access."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import wave
from pathlib import Path


COLORS = ("0x13232b", "0x271820", "0x101012")


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(completed.stderr[-2000:])


def create_demo_media(project_root: Path, *, force: bool = False) -> list[Path]:
    root = project_root.expanduser().resolve()
    timing = root / "narration" / "timing.json"
    edl = root / "edit" / "edl.json"
    provenance = root / "provenance.json"
    missing = [path for path in (timing, edl, provenance) if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"demo project contract is incomplete: {missing}")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to generate the demo clips")

    assets = root / "assets"
    narration = root / "narration"
    assets.mkdir(parents=True, exist_ok=True)
    narration.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for index, color in enumerate(COLORS, start=1):
        target = assets / f"s{index:03d}.mp4"
        if target.exists() and not force:
            created.append(target)
            continue
        _run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=1920x1080:r=30:d=4",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(target),
            ]
        )
        created.append(target)

    vo = narration / "vo.wav"
    if force or not vo.exists():
        sample_rate = 48_000
        frame_count = sample_rate * 12
        with wave.open(str(vo), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(sample_rate)
            block = b"\x00\x00" * sample_rate
            remaining = frame_count
            while remaining:
                count = min(sample_rate, remaining)
                stream.writeframes(block[: count * 2])
                remaining -= count
        created.append(vo)
    return created


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project",
        default=str(Path(__file__).resolve().parents[1] / "examples" / "demo-project"),
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    for path in create_demo_media(Path(args.project), force=args.force):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

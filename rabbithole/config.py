"""Environment and path configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_VOICE_ID = ""
DEFAULT_MODEL_ID = "eleven_multilingual_v2"
DEFAULT_WPM = 177


@dataclass(frozen=True)
class Config:
    elevenlabs_api_key: str
    voice_id: str
    model_id: str
    wpm: int
    episode_cap_usd: float
    budget_mode: str

    @property
    def projects_dir(self) -> Path:
        configured = os.environ.get("RABBITHOLE_PROJECTS_DIR")
        return (
            Path(configured).expanduser().resolve()
            if configured
            else REPO_ROOT / "projects"
        )

    @property
    def style_dir(self) -> Path:
        return REPO_ROOT / "style"


def load_config(env_path: Path | None = None) -> Config:
    """Load configuration from a .env file, falling back to the process environment.

    A value in the .env file wins over the process environment only if it is
    non-empty, so a project-local .env is authoritative. An empty value (e.g. the
    unfilled `ELEVENLABS_API_KEY=` placeholder that bootstrap copies from
    .env.example) is treated as absent rather than as an override, so it cannot
    silently blank out a key already exported in the shell.
    """
    path = env_path if env_path is not None else REPO_ROOT / ".env"
    values = dict(os.environ)
    if path.exists():
        values.update({k: v for k, v in dotenv_values(path).items() if v})

    key = values.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "ELEVENLABS_API_KEY is not set. Add it to .env "
            "(copy .env.example) or export it in the shell."
        )
    voice_id = values.get("RABBITHOLE_VOICE_ID", DEFAULT_VOICE_ID).strip()
    if not voice_id:
        raise RuntimeError(
            "RABBITHOLE_VOICE_ID is not set. Add the voice ID to .env "
            "(copy .env.example) or export it in the shell."
        )

    return Config(
        elevenlabs_api_key=key,
        voice_id=voice_id,
        model_id=values.get("RABBITHOLE_MODEL_ID", DEFAULT_MODEL_ID),
        wpm=int(values.get("RABBITHOLE_WPM", DEFAULT_WPM)),
        episode_cap_usd=float(values.get("RABBITHOLE_EPISODE_CAP_USD", "25")),
        budget_mode=values.get("RABBITHOLE_BUDGET_MODE", "warn"),
    )

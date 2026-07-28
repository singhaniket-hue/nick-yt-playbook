"""Authored excerpts of original interview, news, and social-media audio.

Visual assets are intentionally rendered silent by ``render.cut_segment``:
the edit may reuse, crop, or freeze a source independently of its soundtrack.
When an original voice or location sound is editorially important, the author
opts it in here with an explicit source range and episode-timeline range.

The manifest lives at ``research/source-audio.json`` and uses project-relative
paths::

    {
      "clips": [
        {
          "local_path": "research/footage/raw/interview.mp4",
          "source_start": 12.4,
          "timeline_start": 185.0,
          "duration": 6.5,
          "gain_db": -2.0,
          "duck_vo_db": -18.0
        }
      ]
    }

``duck_vo_db`` also attenuates the music bed during the bite.  It defaults to
``-18 dB`` so an omitted optional value still makes the source intelligible;
``0`` deliberately disables ducking.  SFX remain independent accents.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path

from rabbithole.jsonio import read_json

DEFAULT_DUCK_VO_DB = -18.0
_EPSILON = 1e-6


@dataclass(frozen=True)
class SourceAudioBite:
    """One source-audio excerpt positioned on the active render timeline."""

    local_path: Path
    source_start: float
    timeline_start: float
    duration: float
    gain_db: float = 0.0
    duck_vo_db: float = DEFAULT_DUCK_VO_DB

    @property
    def timeline_end(self) -> float:
        return self.timeline_start + self.duration


def _number(entry: dict, field: str, index: int, *, default: float | None = None) -> float:
    value = entry.get(field, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"Source-audio clip {index} field {field!r} must be a finite number."
        )
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(
            f"Source-audio clip {index} field {field!r} must be a finite number."
        )
    return result


def load_source_audio(
    manifest_path: Path,
    project_root: Path,
    *,
    episode_duration: float | None = None,
) -> list[SourceAudioBite]:
    """Load and validate ``research/source-audio.json``.

    A missing manifest is an intentional opt-out and returns ``[]``. Relative
    ``local_path`` values are resolved from ``project_root`` (not the current
    working directory), making renders deterministic from any shell location.
    """
    manifest_path = Path(manifest_path)
    project_root = Path(project_root)
    if not manifest_path.exists():
        return []

    payload = read_json(manifest_path)
    if not isinstance(payload, dict) or not isinstance(payload.get("clips"), list):
        raise ValueError(
            f"{manifest_path} must contain an object with a 'clips' array."
        )

    bites: list[SourceAudioBite] = []
    for index, entry in enumerate(payload["clips"]):
        if not isinstance(entry, dict):
            raise ValueError(f"Source-audio clip {index} must be a JSON object.")

        raw_path = entry.get("local_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(
                f"Source-audio clip {index} field 'local_path' must be a non-empty string."
            )
        local_path = Path(raw_path)
        if not local_path.is_absolute():
            local_path = project_root / local_path
        local_path = local_path.resolve()
        if not local_path.is_file():
            raise ValueError(
                f"Source-audio clip {index} file does not exist: {local_path}"
            )

        source_start = _number(entry, "source_start", index)
        timeline_start = _number(entry, "timeline_start", index)
        duration = _number(entry, "duration", index)
        gain_db = _number(entry, "gain_db", index, default=0.0)
        duck_vo_db = _number(
            entry, "duck_vo_db", index, default=DEFAULT_DUCK_VO_DB
        )

        if source_start < 0:
            raise ValueError(
                f"Source-audio clip {index} source_start must be zero or greater."
            )
        if timeline_start < 0:
            raise ValueError(
                f"Source-audio clip {index} timeline_start must be zero or greater."
            )
        if duration <= 0:
            raise ValueError(f"Source-audio clip {index} duration must be greater than zero.")
        if not -80.0 <= gain_db <= 24.0:
            raise ValueError(
                f"Source-audio clip {index} gain_db must be between -80 and +24 dB."
            )
        if not -80.0 <= duck_vo_db <= 0.0:
            raise ValueError(
                f"Source-audio clip {index} duck_vo_db must be between -80 and 0 dB."
            )
        if (
            episode_duration is not None
            and timeline_start + duration > float(episode_duration) + _EPSILON
        ):
            raise ValueError(
                f"Source-audio clip {index} ends at "
                f"{timeline_start + duration:.3f}s, past the episode duration of "
                f"{float(episode_duration):.3f}s."
            )

        bites.append(
            SourceAudioBite(
                local_path=local_path,
                source_start=source_start,
                timeline_start=timeline_start,
                duration=duration,
                gain_db=gain_db,
                duck_vo_db=duck_vo_db,
            )
        )

    return sorted(bites, key=lambda bite: bite.timeline_start)


def window_source_audio(
    bites: list[SourceAudioBite], start: float, end: float
) -> list[SourceAudioBite]:
    """Clip bites to an episode excerpt and shift them to segment time zero.

    The source seek advances by the amount clipped from the bite's leading
    edge. This is the audio counterpart of ``segments.window_cuts`` and is
    what keeps ``render --start ... --end ...`` on the correct spoken words.
    """
    selected: list[SourceAudioBite] = []
    for bite in bites:
        clipped_start = max(bite.timeline_start, start)
        clipped_end = min(bite.timeline_end, end)
        if clipped_end - clipped_start <= _EPSILON:
            continue
        leading_trim = clipped_start - bite.timeline_start
        selected.append(
            replace(
                bite,
                source_start=bite.source_start + leading_trim,
                timeline_start=clipped_start - start,
                duration=clipped_end - clipped_start,
            )
        )
    return selected

"""Create the per-episode directory tree."""

from __future__ import annotations

import json
from pathlib import Path

SUBDIRS = (
    "research",
    "script",
    "narration",
    "assets",
    "edit",
    "checkpoints",
    "renders",
)

ACT_SKELETON = """[ACT:1 Cold Open]
<!-- 90 words. Immediate disruption. No greeting, no channel intro. -->

[ACT:2 Origin]
<!-- 440 words. Mundane baseline, then the first anomaly. -->

[ACT:3 Rabbit Hole]
<!-- 3360 words. Numbered chapters. A [REHOOK] every 3-5 minutes. -->

[ACT:4 Climax]
<!-- 1590 words. The darkest payload, preceded by a silence drop. -->

[ACT:5 Outro]
<!-- 530 words. No neat closure. Reframe as symptom, then cut to black. -->
"""


def new_project(projects_dir: Path, slug: str) -> Path:
    """Create projects/<slug>/ with ledgers and an act skeleton.

    Refuses to overwrite an existing project, so a mistyped slug cannot destroy
    work already in progress.
    """
    root = projects_dir / slug
    if root.exists():
        raise FileExistsError(f"Project already exists: {root}")

    for sub in SUBDIRS:
        (root / sub).mkdir(parents=True)

    (root / "claims.json").write_text("[]", encoding="utf-8")
    (root / "provenance.json").write_text("[]", encoding="utf-8")
    (root / "brief.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "",
                "thumbnail_text": "",
                "target_duration_minutes": 34,
                "language": "hinglish",
                "format": "crowley",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (root / "script" / "01-beat-sheet.md").write_text(ACT_SKELETON, encoding="utf-8")
    (root / "script" / "latin-terms.json").write_text(
        json.dumps({"terms": [], "occurrences": []}, indent=2) + "\n",
        encoding="utf-8",
    )

    return root

"""Reading JSON files that a human edits on Windows.

Every JSON file this pipeline reads is hand-editable: claims ledgers,
provenance ledgers, artifact catalogues, the style pack. On Windows the
ordinary ways of producing a file -- PowerShell's `Out-File` and `>`, Notepad,
Excel exports -- write a UTF-8 *byte order mark* by default. `json.loads` on
text decoded as plain `utf-8` then fails on the very first character:

    json.decoder.JSONDecodeError: Unexpected UTF-8 BOM
    (decode using utf-8-sig): line 1 column 1 (char 0)

That is what a BOM'd `provenance.json` did to `rabbithole assets` -- a
traceback out of `load_provenance`, after the slot plan had already been built
and reported, with nothing in the message connecting it to the file the author
would need to fix.

`utf-8-sig` strips a BOM when present and behaves exactly like `utf-8` when
absent, so reading through it is strictly more permissive with no downside.
This module exists so that decision lives in one place with one explanation,
rather than as fifteen scattered encoding arguments that a new call site would
inevitably get wrong again.

Writing deliberately stays plain `utf-8`: nothing here should *emit* a BOM.
"""

from __future__ import annotations

import json
from pathlib import Path


def read_json(path: Path) -> object:
    """Parse a JSON file, tolerating a UTF-8 BOM.

    Raises `json.JSONDecodeError` with the path named for genuinely malformed
    content, so a hand-edit that broke the syntax says which file to look at.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise json.JSONDecodeError(f"{path}: {exc.msg}", exc.doc, exc.pos) from None


def read_json_text(text: str) -> object:
    """Parse JSON from a string that may carry a BOM.

    For content that arrived as text rather than from a file -- an HTTP body,
    a subprocess's stdout -- where a BOM is rarer but not impossible.
    """
    return json.loads(text.lstrip("﻿"))

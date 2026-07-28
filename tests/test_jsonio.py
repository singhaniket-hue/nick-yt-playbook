"""Reading JSON that a Windows tool wrote.

The bug these cover: `rabbithole assets` crashed with a raw
`json.decoder.JSONDecodeError` on a production project because its
`provenance.json` carried a UTF-8 BOM -- written by a PowerShell redirect,
which emits one by default. The traceback came out of `load_provenance` after
the slot plan had already been printed, and named neither the file nor the
cause in a way an author could act on.
"""

import json

import pytest

from rabbithole.assets import load_artifacts
from rabbithole.jsonio import read_json, read_json_text
from rabbithole.provenance import load_provenance
from rabbithole.validate import load_claims, load_sfx_names

BOM = "﻿"


def _write(path, text, *, bom=False):
    path.write_text((BOM if bom else "") + text, encoding="utf-8")
    return path


def test_read_json_accepts_a_bom(tmp_path):
    path = _write(tmp_path / "d.json", '{"a": 1}', bom=True)
    assert read_json(path) == {"a": 1}


def test_read_json_still_reads_a_plain_file(tmp_path):
    path = _write(tmp_path / "d.json", '{"a": 1}')
    assert read_json(path) == {"a": 1}


def test_read_json_names_the_file_on_malformed_content(tmp_path):
    """A hand-edit that broke the syntax should say which file to open."""
    path = _write(tmp_path / "broken.json", '{"a": ')
    with pytest.raises(json.JSONDecodeError, match="broken.json"):
        read_json(path)


def test_read_json_text_strips_a_bom():
    assert read_json_text(BOM + '{"a": 1}') == {"a": 1}


def test_provenance_reads_a_bom_file(tmp_path):
    """The exact failure: a BOM'd empty ledger crashed the assets command."""
    path = _write(tmp_path / "provenance.json", "[]", bom=True)
    assert load_provenance(path) == []


def test_provenance_reads_a_bom_file_with_records(tmp_path):
    record = {
        "asset_id": "a1", "tier": "archival", "provider": "commons",
        "original_url": "https://example.org/x", "license": "CC BY 4.0",
        "retrieved_at": "2026-01-01T00:00:00Z", "local_path": "x.mp4",
        "used_in_slots": ["s001"], "notes": "",
    }
    path = _write(tmp_path / "provenance.json", json.dumps([record]), bom=True)
    loaded = load_provenance(path)
    assert len(loaded) == 1
    assert loaded[0].asset_id == "a1"


def test_artifacts_read_a_bom_file(tmp_path):
    entry = [{"artifact_id": "a001", "url": "https://example.org/x"}]
    path = _write(tmp_path / "artifacts.json", json.dumps(entry), bom=True)
    loaded = load_artifacts(path)
    assert len(loaded) == 1
    assert loaded[0].artifact_id == "a001"


def test_claims_read_a_bom_file(tmp_path):
    claims = [{"claim_id": "c001", "text": "x", "confidence": "documented", "sources": []}]
    path = _write(tmp_path / "claims.json", json.dumps(claims), bom=True)
    assert len(load_claims(path)) == 1


def test_sfx_names_read_a_bom_file(tmp_path):
    path = _write(tmp_path / "sfx.json", json.dumps({"vhs-burst": {"category": "texture"}}), bom=True)
    assert load_sfx_names(path) == {"vhs-burst"}

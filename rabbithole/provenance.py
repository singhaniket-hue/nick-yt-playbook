"""The provenance ledger: where every visual asset came from.

Append-only by design. An asset's record is written when it is retrieved and is
never rewritten, so the ledger is an audit trail rather than a cache.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rabbithole.validate import Finding
from rabbithole.jsonio import read_json

TIERS = ("primary", "archival", "illustrative", "atmospheric")


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    tier: str
    provider: str
    original_url: str
    license: str
    retrieved_at: str
    local_path: str
    used_in_slots: tuple[str, ...] = ()
    notes: str = ""


def load_provenance(path: Path) -> list[AssetRecord]:
    """Read the ledger. A missing ledger is an empty ledger, not an error."""
    if not path.exists():
        return []

    raw = read_json(path)
    records = []
    for entry in raw:
        entry = dict(entry)
        entry["used_in_slots"] = tuple(entry.get("used_in_slots", ()))
        records.append(AssetRecord(**entry))
    return records


def save_provenance(path: Path, records: list[AssetRecord]) -> Path:
    """Write the ledger as a JSON array, one object per asset."""
    payload = [asdict(record) for record in records]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def add_record(records: list[AssetRecord], record: AssetRecord) -> list[AssetRecord]:
    """Append a record, returning a new list.

    Raises ValueError on a duplicate asset_id -- the ledger is append-only and an
    asset is retrieved once.
    """
    if any(existing.asset_id == record.asset_id for existing in records):
        raise ValueError(
            f"Asset {record.asset_id!r} is already in the ledger; "
            f"the ledger is append-only and an asset is retrieved once."
        )
    return [*records, record]


def check_provenance(
    records: list[AssetRecord], slot_ids: list[str]
) -> list[Finding]:
    """Verify the ledger covers the slot plan exactly.

    Reuse of one asset across the episode is legitimate and expected -- the format
    reuses screenshots and archival stills, so the same asset_id can legitimately
    turn up in act two and again in act four. What is NOT legitimate is the same
    asset in two slots that are ADJACENT in timeline order: back-to-back slots
    against identical footage read as a jump cut on the same image, which is a
    defect the reuse-elsewhere case is not. Adjacency is judged purely by position
    in `slot_ids` (which is timeline order), not by asset identity alone.
    """
    findings: list[Finding] = []
    slot_id_set = set(slot_ids)
    position = {slot_id: index for index, slot_id in enumerate(slot_ids)}
    claimants: dict[str, list[str]] = {slot_id: [] for slot_id in slot_ids}

    for record in records:
        if record.tier not in TIERS:
            findings.append(
                Finding(
                    gate="provenance",
                    severity="error",
                    message=(
                        f"Asset {record.asset_id!r} has tier {record.tier!r}; "
                        f"expected one of {', '.join(TIERS)}."
                    ),
                )
            )

        if record.tier == "atmospheric":
            if record.original_url or record.license:
                findings.append(
                    Finding(
                        gate="provenance",
                        severity="error",
                        message=(
                            f"Asset {record.asset_id!r} is tier 'atmospheric' "
                            f"(generated) but claims original_url={record.original_url!r} "
                            f"or license={record.license!r}; a generated plate has "
                            f"neither."
                        ),
                    )
                )
        else:
            if not record.original_url or not record.license:
                findings.append(
                    Finding(
                        gate="provenance",
                        severity="error",
                        message=(
                            f"Asset {record.asset_id!r} (tier {record.tier!r}) is "
                            f"missing original_url or license; both are required "
                            f"outside tier 'atmospheric'."
                        ),
                    )
                )

        for slot_id in record.used_in_slots:
            if slot_id not in slot_id_set:
                findings.append(
                    Finding(
                        gate="provenance",
                        severity="error",
                        message=(
                            f"Asset {record.asset_id!r} claims slot {slot_id!r}, "
                            f"which is not in the slot plan."
                        ),
                    )
                )
            else:
                claimants[slot_id].append(record.asset_id)

    for slot_id in slot_ids:
        claiming = claimants[slot_id]
        if not claiming:
            findings.append(
                Finding(
                    gate="provenance",
                    severity="error",
                    message=f"Slot {slot_id!r} has no asset claiming it.",
                )
            )
        elif len(claiming) > 1:
            findings.append(
                Finding(
                    gate="provenance",
                    severity="error",
                    message=(
                        f"Slot {slot_id!r} is claimed by {len(claiming)} assets "
                        f"({', '.join(claiming)}); each slot needs exactly one."
                    ),
                )
            )

    for record in records:
        positions = sorted(
            position[slot_id] for slot_id in record.used_in_slots if slot_id in position
        )
        for earlier, later in zip(positions, positions[1:]):
            if later - earlier == 1:
                findings.append(
                    Finding(
                        gate="provenance",
                        severity="error",
                        message=(
                            f"Asset {record.asset_id!r} is used in adjacent slots "
                            f"{slot_ids[earlier]!r} and {slot_ids[later]!r}. Reuse "
                            f"elsewhere in the episode is fine, but the same asset "
                            f"in two consecutive slots reads as a jump cut on "
                            f"identical footage."
                        ),
                    )
                )

    return findings

"""The provenance ledger: where every visual asset came from.

Append-only by design. An asset's record is written when it is retrieved and is
never discarded, so the ledger is an audit trail rather than a cache.  The
explicit ``assets --refresh --slot ...`` workflow is the narrow exception to
record immutability: it converts the current record into an unclaimed retired
record, preserving every source field plus the previous identity and slot
claims in its notes while moving the media into project-local quarantine.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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
    attribution_burned: bool = False


@dataclass(frozen=True)
class AssetRetirementMove:
    """One recoverable same-project media move for an explicit refresh."""

    source: Path
    destination: Path


@dataclass(frozen=True)
class AssetRefreshPlan:
    """A fully validated retirement transaction prepared before any mutation."""

    refresh_id: str
    selected_slots: tuple[str, ...]
    preview_records: tuple[AssetRecord, ...]
    retired_records: tuple[AssetRecord, ...]
    records_after_retirement: tuple[AssetRecord, ...]
    moves: tuple[AssetRetirementMove, ...]


_REFRESH_MARKER = re.compile(
    r"\[rabbithole-refresh id=(?P<id>[A-Za-z0-9_.-]+) "
    r"slots=(?P<slots>[A-Za-z0-9_.,-]+)\]"
)


def _record_path(record: AssetRecord, project_root: Path) -> Path:
    raw = Path(record.local_path).expanduser()
    if raw.is_absolute():
        return raw
    return Path(project_root) / Path(*raw.parts)


def _portable_project_path(path: Path, project_root: Path) -> str:
    try:
        relative = path.resolve().relative_to(Path(project_root).resolve())
    except ValueError as exc:
        raise ValueError(
            f"Refresh media must be inside project root {str(project_root)!r}; "
            f"got {str(path)!r}."
        ) from exc
    return relative.as_posix()


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return cleaned or "asset"


def _refresh_stamp(now: datetime | None) -> tuple[str, str]:
    value = now if now is not None else datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return (
        value.strftime("%Y%m%dT%H%M%S%fZ"),
        value.isoformat().replace("+00:00", "Z"),
    )


def resumable_refresh_id(
    records: list[AssetRecord], selected_slots: tuple[str, ...] | list[str]
) -> str | None:
    """Return the prior refresh id when an exact explicit batch is incomplete.

    A successful replacement is checkpointed immediately.  If a later slot in
    that same refresh fails, rerunning the command must not retire the successful
    replacement again.  An unclaimed retired record carries the exact slot set;
    it becomes resumable only while at least one selected slot remains unclaimed
    and every already-claimed selected slot has a single current claimant.
    """

    selected = frozenset(selected_slots)
    if not selected:
        return None
    claimants = {
        slot_id: [
            record
            for record in records
            if slot_id in record.used_in_slots
        ]
        for slot_id in selected
    }
    if all(claimants[slot_id] for slot_id in selected):
        return None
    if any(len(values) > 1 for values in claimants.values()):
        return None

    expected_slots = tuple(sorted(selected))
    for record in reversed(records):
        if record.used_in_slots:
            continue
        match = _REFRESH_MARKER.search(record.notes)
        if not match:
            continue
        marker_slots = tuple(
            sorted(value for value in match.group("slots").split(",") if value)
        )
        if marker_slots == expected_slots:
            return match.group("id")
    return None


def plan_asset_refresh(
    records: list[AssetRecord],
    selected_slots: tuple[str, ...] | list[str],
    project_root: Path,
    *,
    now: datetime | None = None,
) -> AssetRefreshPlan:
    """Validate and describe a recoverable refresh without changing files.

    Every slot claimed by a record that intersects the requested set must also
    be explicit in that set.  This rejects a one-slot command that would
    silently break a multi-slot asset.  Multiple current claimants are handled
    only when the complete union of their slot claims is explicit; all of those
    records are then retired as an auditable ambiguity.
    """

    selected = tuple(dict.fromkeys(selected_slots))
    if not selected:
        raise ValueError(
            "Asset refresh requires one or more explicit selected slot ids."
        )
    selected_set = frozenset(selected)
    claimant_indexes = [
        index
        for index, record in enumerate(records)
        if selected_set.intersection(record.used_in_slots)
    ]
    affected_slots = {
        slot_id
        for index in claimant_indexes
        for slot_id in records[index].used_in_slots
    }
    unselected = sorted(affected_slots - selected_set)
    if unselected:
        raise ValueError(
            "Refresh would retire asset record(s) that also claim unselected "
            f"slot(s): {', '.join(unselected)}. Repeat --slot for every affected "
            "slot; no media or provenance was changed."
        )

    refresh_id, retired_at = _refresh_stamp(now)
    root = Path(project_root).resolve()
    quarantine = root / "revisions" / "quarantine" / "assets" / refresh_id
    existing_ids = {record.asset_id for record in records}
    reserved_ids = set(existing_ids)
    updated = list(records)
    preview = list(records)
    retired: list[AssetRecord] = []
    destination_by_source: dict[Path, Path] = {}
    moves: list[AssetRetirementMove] = []

    for index in claimant_indexes:
        record = records[index]
        source = _record_path(record, root).resolve()
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"Cannot refresh asset {record.asset_id!r}: current media "
                f"{str(source)!r} is outside project root {str(root)!r}. "
                "Move it into the portable project before refreshing."
            ) from exc
        if not source.is_file() or source.stat().st_size <= 0:
            raise ValueError(
                f"Cannot refresh asset {record.asset_id!r}: current media "
                f"{str(source)!r} is missing or empty, so it cannot be retired "
                "recoverably."
            )

        destination = destination_by_source.get(source)
        if destination is None:
            source_name = _safe_filename(source.name)
            destination = quarantine / (
                f"{_safe_filename(record.asset_id)}--{source_name}"
            )
            suffix = 2
            while destination.exists() or destination in destination_by_source.values():
                destination = quarantine / (
                    f"{_safe_filename(record.asset_id)}-{suffix}--{source_name}"
                )
                suffix += 1
            destination_by_source[source] = destination
            moves.append(
                AssetRetirementMove(source=source, destination=destination)
            )

        base_id = f"{record.asset_id}--retired-{refresh_id}"
        retired_id = base_id
        suffix = 2
        while retired_id in reserved_ids:
            retired_id = f"{base_id}-{suffix}"
            suffix += 1
        reserved_ids.add(retired_id)

        prior_slots = ",".join(record.used_in_slots)
        marker_slots = ",".join(sorted(selected_set))
        marker = (
            f"[rabbithole-refresh id={refresh_id} slots={marker_slots}]"
        )
        retirement_note = (
            f"{marker} retired at {retired_at}; previous_asset_id="
            f"{record.asset_id!r}; previous_slots={prior_slots!r}; "
            f"previous_local_path={record.local_path!r}; media retained in "
            "project-local revisions/quarantine"
        )
        notes = (
            f"{record.notes}; {retirement_note}"
            if record.notes
            else retirement_note
        )
        retired_record = dataclasses.replace(
            record,
            asset_id=retired_id,
            local_path=_portable_project_path(destination, root),
            used_in_slots=(),
            notes=notes,
        )
        preview[index] = dataclasses.replace(record, used_in_slots=())
        updated[index] = retired_record
        retired.append(retired_record)

    return AssetRefreshPlan(
        refresh_id=refresh_id,
        selected_slots=tuple(selected),
        preview_records=tuple(preview),
        retired_records=tuple(retired),
        records_after_retirement=tuple(updated),
        moves=tuple(moves),
    )


def apply_asset_refresh(
    provenance_path: Path, plan: AssetRefreshPlan
) -> list[AssetRecord]:
    """Move current media to quarantine and atomically checkpoint retirement.

    All media moves happen before the atomic ledger replacement.  If any move
    or the ledger write fails, completed moves are rolled back in reverse order
    so the old records remain usable.  No media is deleted.
    """

    if not plan.moves:
        return list(plan.records_after_retirement)

    moved: list[AssetRetirementMove] = []
    try:
        for move in plan.moves:
            if move.destination.exists():
                raise RuntimeError(
                    f"Refresh quarantine destination already exists: "
                    f"{str(move.destination)!r}"
                )
            move.destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(move.source, move.destination)
            moved.append(move)
        save_provenance(
            provenance_path, list(plan.records_after_retirement)
        )
    except Exception as exc:
        rollback_errors: list[str] = []
        for move in reversed(moved):
            try:
                if move.destination.exists() and not move.source.exists():
                    move.source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(move.destination, move.source)
            except OSError as rollback_exc:
                rollback_errors.append(
                    f"{str(move.destination)!r} -> {str(move.source)!r}: "
                    f"{rollback_exc}"
                )
        if rollback_errors:
            raise RuntimeError(
                "Asset refresh failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        raise
    return list(plan.records_after_retirement)


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
    """Atomically write the ledger as a JSON array, one object per asset.

    The temporary file lives beside the ledger, so ``os.replace`` stays on the
    same filesystem and cannot expose a half-written JSON document if the
    acquisition process is interrupted.
    """
    payload = [asdict(record) for record in records]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
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

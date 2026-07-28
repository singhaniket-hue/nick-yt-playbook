"""Asset sourcing: the join point between the slot plan and the provenance ledger.

Given a slot plan (`rabbithole.slots.build_slots`) and the current provenance
ledger, `plan_assets` decides -- per slot -- what needs to happen: generate a
plate locally, search the keyless archives, fetch a primary artifact via
yt-dlp, or neither, because the slot is already satisfied or because sourcing
it isn't implemented (yet, or ever) by this package. `execute_plan` then does
the sourcing the plan calls for and returns new ledger records plus any
per-slot failures.

All five shot kinds can now be served. Two of them could not until recently, and
both failed quietly rather than loudly:

`screenshot` was a declared gap -- "capture is not implemented; supply the file
manually". `sources/capture.py` closes it using the headless browser already
installed on the machine, so the action is `shoot`. The same module also covers
the `capture` slots that yt-dlp cannot reach, which turned out to be most of
them: this format's capture slots overwhelmingly want documents (an NTA notice,
a High Court order, a press communique, a policy posted as text), and yt-dlp
retrieves video from video hosts. That was a mechanism mismatch, not a sourcing
failure -- the material was cited and identified all along.

`graphic` used to be the second such gap, and was the worse of the two because
it did not look like one. It was planned as `deferred` -- "rendered later by
the compose stage" -- but `graphics.py` draws chapter cards and censor boxes
only, from `[CHAPTER:]`/`[CENSOR:]` overlays, and had no code path for a
`[SHOT:graphic ...]` slot. So those slots were reported as handled and then
dropped by `assemble_footage` for want of an asset: in one production audit,
81 slots and 45% of the episode. `cards.build_card` now
draws them locally from the style pack, and the action is `draw`.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from rabbithole import cards
from rabbithole.jsonio import read_json
from rabbithole.provenance import AssetRecord
from rabbithole.slots import Slot
from rabbithole.sources import archives, capture, ytdlp
from rabbithole.sources.plates import PLATE_KINDS, PlateSpec, build_plate
from rabbithole.validate import Finding

KIND_TO_TIER = {
    "plate": "atmospheric",
    "archival": "archival",
    "capture": "primary",
    "screenshot": "primary",
    # Drawn locally from the style pack, like a plate: no external source, no
    # rights question, nothing retrieved. Same tier for the same reasons.
    "graphic": "atmospheric",
}
KIND_TO_ACTION = {
    "plate": "generate",
    "archival": "search",
    "capture": "fetch",
    # Was "manual" -- a declared gap. `sources/capture.py` closes it with the
    # headless browser already on the machine, so a screenshot slot bound to an
    # artifact is now captured like any other asset.
    "screenshot": "shoot",
    # Was "deferred", on the reasoning that "graphics are rendered by the
    # compose stage, not sourced". That was wrong in a way that produced no
    # error: graphics.py draws chapter cards and censor boxes only, so every
    # `graphic` slot was reported as handled and then skipped by
    # assemble_footage for want of an asset. In one production audit that was
    # 81 slots and 45% of the episode. `cards.build_card` now draws them.
    "graphic": "draw",
}

ACTION_REASONS = {
    "manual": "no artifact bound to this screenshot slot; bind one in research/artifacts.json",
}

# Actions plan_assets can hand to a slot that KIND_TO_ACTION never produces on
# its own: a slot already covered by the ledger, or one whose kind or missing
# inputs make sourcing impossible right now.
_SATISFIED = "satisfied"
_BLOCKED = "blocked"
# A slot claimed by a record that cannot serve it. Distinct from _BLOCKED
# because the fix is different: not "go source something" but "the ledger is
# describing a plan that no longer exists".
_STALE = "stale"

# Actions execute_plan does nothing for: already reported by the plan, so no
# record and no finding is correct, not an omission.
_NO_OP_ACTIONS = frozenset({"manual", _BLOCKED, _STALE, _SATISFIED})

# Group order for format_plan: work-to-do first (in the order a human would
# tackle it -- free and local, then network searches, then gated fetches),
# then the three "nothing to do here" buckets.
_ACTION_ORDER = ("generate", "draw", "shoot", "search", "fetch", "manual", _STALE, _BLOCKED, _SATISFIED)

# Low-level callers historically used generated plates and heading-only cards
# to make an animatic. Keep that workflow explicit, while the CLI's production
# path uses ``final`` and fails closed on those placeholders.
QUALITY_MODES = ("final", "animatic")
FINAL_MIN_EVIDENCE_RATIO = 0.50
EVIDENCE_TIERS = frozenset({"primary", "archival"})
SOURCE_REPLACEMENT_TIERS = frozenset({"primary", "archival", "illustrative"})
FLEXIBLE_VISUAL_KINDS = frozenset({"plate", "graphic"})


def allowed_tiers_for_kind(kind: str) -> frozenset[str]:
    """Ledger tiers that can honestly serve one visual-slot kind.

    A plate or graphic marker describes an editorial visual role, not a
    compulsory renderer. Its locally generated fallback remains
    ``atmospheric``, while sourced primary, archival, or illustrative footage
    may intentionally replace that fallback. Illustrative media never enters
    :data:`EVIDENCE_TIERS`.
    """
    expected = KIND_TO_TIER.get(kind)
    if expected is None:
        return frozenset()
    if kind in FLEXIBLE_VISUAL_KINDS:
        return frozenset({expected}) | SOURCE_REPLACEMENT_TIERS
    return frozenset({expected})


@dataclass(frozen=True)
class PlanItem:
    slot_id: str
    kind: str
    tier: str
    action: str
    reason: str


@dataclass(frozen=True)
class EvidenceMetrics:
    """Measurable sourced-evidence coverage of the visual timeline."""

    evidence_seconds: float
    total_seconds: float
    evidence_slots: int
    total_slots: int

    @property
    def duration_ratio(self) -> float:
        return self.evidence_seconds / self.total_seconds if self.total_seconds else 1.0

    @property
    def slot_ratio(self) -> float:
        return self.evidence_slots / self.total_slots if self.total_slots else 1.0


def normalize_quality(quality: str) -> str:
    """Validate and normalise a public quality-mode value."""
    value = (quality or "").strip().lower()
    if value not in QUALITY_MODES:
        raise ValueError(
            f"Unknown quality mode {quality!r}; expected one of {', '.join(QUALITY_MODES)}"
        )
    return value


def evidence_metrics(
    slots: list[Slot], records: list[AssetRecord]
) -> EvidenceMetrics:
    """Duration- and slot-weighted coverage by primary or archival evidence.

    A generated texture or explanatory card can support pacing, but it is not a
    source. Each slot counts at most once even if a malformed ledger has two
    claimants; :func:`check_provenance` separately rejects that ambiguity.
    """
    evidence_slot_ids = {
        slot_id
        for record in records
        if record.tier in EVIDENCE_TIERS
        for slot_id in record.used_in_slots
    }
    total_seconds = sum(max(0.0, slot.hold_seconds) for slot in slots)
    evidence_seconds = sum(
        max(0.0, slot.hold_seconds)
        for slot in slots
        if slot.slot_id in evidence_slot_ids
    )
    return EvidenceMetrics(
        evidence_seconds=evidence_seconds,
        total_seconds=total_seconds,
        evidence_slots=sum(1 for slot in slots if slot.slot_id in evidence_slot_ids),
        total_slots=len(slots),
    )


def check_source_quality(
    slots: list[Slot],
    records: list[AssetRecord],
    *,
    quality: str = "final",
    min_evidence_ratio: float = FINAL_MIN_EVIDENCE_RATIO,
) -> list[Finding]:
    """Fail-closed documentary-source gates for a completed visual plan.

    ``animatic`` preserves the deliberately permissive preview workflow.
    ``final`` rejects label-only production directions and invalid scene-like
    descriptions when they would become generated atmospheric placeholders,
    plus timelines dominated by generated atmospherics. A sourced replacement
    bound to a plate or graphic slot is validated as that asset, not as the
    unused local fallback.
    """
    mode = normalize_quality(quality)
    if mode == "animatic":
        return []
    if not 0.0 <= min_evidence_ratio <= 1.0:
        raise ValueError(
            f"min_evidence_ratio must be between 0 and 1, got {min_evidence_ratio}"
        )

    findings: list[Finding] = []
    records_by_slot: dict[str, list[AssetRecord]] = {}
    for record in records:
        for slot_id in record.used_in_slots:
            records_by_slot.setdefault(slot_id, []).append(record)

    for slot in slots:
        expected_tier = KIND_TO_TIER.get(slot.kind)
        allowed_tiers = allowed_tiers_for_kind(slot.kind)
        slot_records = records_by_slot.get(slot.slot_id, [])
        for record in slot_records:
            if expected_tier is not None and record.tier not in allowed_tiers:
                findings.append(
                    Finding(
                        gate="source-quality",
                        severity="error",
                        message=(
                            f"Slot {slot.slot_id!r} is {slot.kind!r} and requires "
                            f"tier {expected_tier!r}, but provenance asset "
                            f"{record.asset_id!r} is tier {record.tier!r}. The "
                            "ledger appears to target an older slot plan; re-source "
                            "the slot before a final render."
                        ),
                    )
                )

        has_sourced_replacement = any(
            record.tier in SOURCE_REPLACEMENT_TIERS for record in slot_records
        )
        if slot.kind == "plate" and not has_sourced_replacement:
            _plate_kind, was_valid = _plate_kind_for(slot)
            if not was_valid:
                findings.append(
                    Finding(
                        gate="source-quality",
                        severity="error",
                        message=(
                            f"Final-quality slot {slot.slot_id!r} describes a scene "
                            f"({slot.detail!r}) that the plate generator cannot depict. "
                            f"It would silently become 'grain' in an animatic. Bind "
                            f"sourced footage or name an explicit texture plate "
                            f"({', '.join(PLATE_KINDS)})."
                        ),
                    )
                )
        elif slot.kind == "graphic" and not has_sourced_replacement:
            spec, _card_findings = cards.spec_for_slot(slot)
            reason = cards.production_note_reason(slot.detail, spec)
            if reason:
                findings.append(
                    Finding(
                        gate="source-quality",
                        severity="error",
                        message=(
                            f"Final-quality graphic slot {slot.slot_id!r} is a "
                            f"label-only production note: {reason}. Detail was "
                            f"{slot.detail!r}. Use --quality animatic only for a "
                            f"deliberate preview."
                        ),
                    )
                )

    metrics = evidence_metrics(slots, records)
    if metrics.duration_ratio < min_evidence_ratio:
        findings.append(
            Finding(
                gate="source-quality",
                severity="error",
                message=(
                    "Sourced-evidence ratio is "
                    f"{metrics.duration_ratio:.1%} by screen time "
                    f"({metrics.evidence_seconds:.2f}/{metrics.total_seconds:.2f}s; "
                    f"{metrics.evidence_slots}/{metrics.total_slots} slots, "
                    f"{metrics.slot_ratio:.1%}), below the final-quality minimum "
                    f"of {min_evidence_ratio:.0%}. Primary and archival ledger tiers "
                    "count as evidence; generated cards and plates do not."
                ),
            )
        )
    return findings


@dataclass(frozen=True)
class Artifact:
    """One entry from the author's research/artifacts.json catalogue.

    The catalogue is hand-built during research, before any script or slot
    plan exists, so most artifacts start unbound (`slot_id == ""`). Binding
    an artifact to a slot -- adding `slot_id` -- is a later editorial
    decision made once a script exists; see `artifact_urls_by_slot`.
    """

    artifact_id: str
    url: str
    title: str = ""
    kind: str = ""
    date: str = ""
    source_role: str = ""
    use: str = ""
    rights_note: str = ""
    slot_id: str = ""


_ARTIFACT_FIELDS = frozenset(f.name for f in dataclasses.fields(Artifact))


def load_artifacts(path: Path) -> list[Artifact]:
    """Read the artifact catalogue from research/artifacts.json.

    A missing file is an empty catalogue, not an error -- most projects
    never need a `capture` slot, so most projects never have this file.
    Entries missing `artifact_id` or `url` are skipped rather than raising:
    one malformed entry in a hand-edited research file should not block
    every other slot's plan. Unknown extra keys are ignored so the
    catalogue can grow fields (e.g. `rights_note`, `use`) without breaking
    this reader. A JSON value that is not an array of objects -- the file
    was replaced with something structurally wrong -- fails loudly with a
    `RuntimeError` naming the path, rather than an opaque `AttributeError`
    deep in the loop below.
    """
    if not path.exists():
        return []

    raw = read_json(path)
    if not isinstance(raw, list):
        raise RuntimeError(f"{path}: expected a JSON array of artifact objects")

    artifacts: list[Artifact] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise RuntimeError(f"{path}: expected a JSON array of artifact objects")

        artifact_id = entry.get("artifact_id")
        url = entry.get("url")
        if not artifact_id or not url:
            continue

        fields = {
            key: value
            for key, value in entry.items()
            if key in _ARTIFACT_FIELDS and value is not None
        }
        fields["artifact_id"] = str(artifact_id)
        fields["url"] = str(url)
        artifacts.append(Artifact(**fields))
    return artifacts


def artifact_urls_by_slot(artifacts: list[Artifact]) -> dict[str, str]:
    """Slot bindings only: slot_id -> url, for artifacts that carry a slot_id.

    Binding is a separate, later step from cataloguing -- most artifacts
    have no `slot_id` until an editor assigns them to a shot in the slot
    plan, so this is almost always a strict subset of `artifacts`.
    """
    return {artifact.slot_id: artifact.url for artifact in artifacts if artifact.slot_id}


def _iso_utc(ts: datetime) -> str:
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def plan_assets(
    slots: list[Slot], records: list[AssetRecord], artifacts: list[Artifact]
) -> list[PlanItem]:
    """Decide what each slot needs.

    Checked in this order per slot, and the first match wins:
      1. Claimed by a ledger record of the wrong tier -> `stale`.
      2. Claimed by a ledger record of the right tier -> `satisfied`.
      3. Unknown kind -> `blocked`.
      4. `capture` with no artifact bound to it -> `blocked`.
      5. Otherwise, the kind's normal action from `KIND_TO_ACTION`.

    Step 1 exists because slot ids are positional. Adding a `[SHOT:]` marker
    renumbers every slot after it, so a ledger written against the earlier plan
    still claims slot ids that now name different shots -- and a check of "is
    this slot id claimed" accepts that silently, putting a graphic card into an
    archival slot with no error raised anywhere.

    `artifacts` is the whole catalogue read by `load_artifacts`, not just
    the bound subset -- that lets the `blocked` reason for an unbound
    `capture` slot distinguish "the catalogue is empty" from "the catalogue
    has entries but none is bound to this slot yet", which is the
    difference between "go catalogue an artifact" and "go bind one".
    """
    claimed_by: dict[str, tuple[str, str]] = {}
    for record in records:
        for slot_id in record.used_in_slots:
            claimed_by.setdefault(slot_id, (record.asset_id, record.tier))

    bindings = artifact_urls_by_slot(artifacts)

    items: list[PlanItem] = []
    for slot in slots:
        tier = KIND_TO_TIER.get(slot.kind, "")

        if slot.slot_id in claimed_by:
            asset_id, record_tier = claimed_by[slot.slot_id]
            # Slot ids are POSITIONAL, so inserting a [SHOT:] marker renumbers
            # every slot after it and a ledger written against the old plan now
            # claims different slots. Checking only "is this slot id claimed"
            # accepts that silently: a graphic card drawn for one shot ends up
            # satisfying an archival slot, and the render puts it on screen with
            # no error anywhere. Tier compatibility catches that corruption for
            # fixed-role slots. Plate and graphic slots deliberately also accept
            # sourced footage tiers, because their generated renderers are
            # fallbacks rather than mandatory asset types.
            if record_tier not in allowed_tiers_for_kind(slot.kind):
                items.append(
                    PlanItem(
                        slot_id=slot.slot_id,
                        kind=slot.kind,
                        tier=tier,
                        action=_STALE,
                        reason=(
                            f"claimed by asset {asset_id!r} of tier {record_tier!r}, "
                            f"but a {slot.kind!r} slot needs tier {tier!r}. The ledger "
                            f"was written against a different slot plan -- most likely "
                            f"markers were added or moved since. Drop the stale record "
                            f"and re-source this slot."
                        ),
                    )
                )
                continue

            items.append(
                PlanItem(
                    slot_id=slot.slot_id,
                    kind=slot.kind,
                    tier=tier,
                    action=_SATISFIED,
                    reason=f"already sourced as asset {asset_id!r}",
                )
            )
            continue

        if slot.kind not in KIND_TO_ACTION:
            items.append(
                PlanItem(
                    slot_id=slot.slot_id,
                    kind=slot.kind,
                    tier=tier,
                    action=_BLOCKED,
                    reason=f"unknown shot kind {slot.kind!r}; nothing knows how to source it",
                )
            )
            continue

        action = KIND_TO_ACTION[slot.kind]

        if action in ("fetch", "shoot") and slot.slot_id not in bindings:
            if not artifacts:
                reason = (
                    "research/artifacts.json has no artifacts at all; catalogue an "
                    f"artifact before slot {slot.slot_id!r} can be fetched"
                )
            else:
                reason = (
                    f"research/artifacts.json has {len(artifacts)} artifact(s) but none "
                    f"bound to slot {slot.slot_id!r}; bind one by adding "
                    f"\"slot_id\": {slot.slot_id!r} to the chosen artifact"
                )
            items.append(
                PlanItem(
                    slot_id=slot.slot_id,
                    kind=slot.kind,
                    tier=tier,
                    action=_BLOCKED,
                    reason=reason,
                )
            )
            continue

        query = slot.queries[0] if slot.queries else f"{slot.kind} {slot.detail}".strip()
        if action == "generate":
            reason = (
                f"generate a {slot.hold_seconds:.1f}s atmospheric plate locally with "
                f"ffmpeg -- no network, no rights questions"
            )
        elif action == "draw":
            card_kind = cards.classify(slot.detail)
            reason = (
                f"draw a {slot.hold_seconds:.1f}s {card_kind} card locally from the "
                f"style pack -- no network, no rights questions"
            )
        elif action == "search":
            reason = (
                f"search archive.org and Wikimedia Commons for {query!r}, "
                f"then download the first usable, licensed hit"
            )
        elif action == "fetch":
            reason = f"fetch {bindings[slot.slot_id]!r} via yt-dlp, gated on the claims ledger"
        elif action == "shoot":
            reason = (
                f"capture {bindings[slot.slot_id]!r} with the headless browser "
                f"(or render it, if it is a document)"
            )
        else:
            reason = ACTION_REASONS[action]

        items.append(
            PlanItem(slot_id=slot.slot_id, kind=slot.kind, tier=tier, action=action, reason=reason)
        )

    return items


def format_plan(items: list[PlanItem]) -> str:
    """Human-readable plan, grouped by action, with a summary line."""
    if not items:
        return "No slots to plan."

    groups: dict[str, list[PlanItem]] = {}
    for item in items:
        groups.setdefault(item.action, []).append(item)

    order = [action for action in _ACTION_ORDER if action in groups]
    order += sorted(action for action in groups if action not in _ACTION_ORDER)

    lines: list[str] = []
    for action in order:
        group = groups[action]
        lines.append(f"{action} ({len(group)}):")
        for item in group:
            lines.append(f"  {item.slot_id} [{item.kind}] {item.reason}")
        lines.append("")

    counts = ", ".join(f"{action}={len(groups[action])}" for action in order)
    lines.append(f"{len(items)} slot(s) planned: {counts}")
    return "\n".join(lines)


def _plate_kind_for(slot: Slot) -> tuple[str, bool]:
    """The ffmpeg plate kind for a `plate` slot's `detail`.

    Returns `(kind, was_valid)`. `was_valid` is False when the slot's detail
    did not name a recognised `PLATE_KINDS` entry (e.g. "dark corridor" --
    plates.py can only render textures, not scenes), in which case `kind`
    falls back to `'grain'`. The caller records that fallback in the
    resulting AssetRecord's `notes` rather than defaulting silently.
    """
    stripped = slot.detail.strip()
    # ``build_slots`` authors this leading gap itself, so no script marker can
    # name its texture. The established opening treatment is moving grain;
    # treat that framework-authored default as explicit instead of rejecting
    # every final render that begins before its first [SHOT:] marker.
    if stripped == "implicit opening":
        return "grain", True
    candidate = stripped.split()[0] if stripped else ""
    if candidate in PLATE_KINDS:
        return candidate, True
    return "grain", False


def execute_plan(
    items: list[PlanItem],
    slots: list[Slot],
    out_dir: Path,
    *,
    grade: dict,
    claims: list[dict],
    archive_transport: archives.Transport | None = None,
    ytdlp_runner: ytdlp.Runner | None = None,
    ytdlp_prober: ytdlp.Prober | None = None,
    capture_runner: capture.Runner | None = None,
    capture_transport: capture.Transport | None = None,
    now: datetime | None = None,
    artifacts: dict[str, str] | None = None,
    typography: dict | None = None,
    palette: dict | None = None,
    quality: str = "animatic",
) -> tuple[list[AssetRecord], list[Finding]]:
    """Source everything the plan calls for, returning new records and any failures.

    `artifacts` (slot_id -> URL, as from `artifact_urls_by_slot(load_artifacts(...))`)
    supplies the URL a `fetch` item needs; `plan_assets` already proved one
    exists for every `fetch` item it produced, by construction.

    `typography` and `palette` are the style pack dicts a `draw` item needs to
    render a card; they are loaded from `style/` by the caller. A `draw` item
    with either missing is reported rather than rendered, so a caller that
    forgot to pass them gets a clear message instead of a card in default
    fonts.

    One slot failing (no archival hits, an unresolved hit, a refused
    download, a claims-ledger refusal) never aborts the run: it is recorded
    as a `Finding` with `gate="assets"` and `severity="error"`, and the loop
    moves on to the next item. An exception `execute_plan` does not
    specifically anticipate is not caught here and propagates, ending the run
    -- see the module's test suite and the caller's docs for what that means
    for records already produced earlier in the same call.
    """
    out_dir = Path(out_dir)
    quality = normalize_quality(quality)
    artifacts = artifacts or {}
    slots_by_id = {slot.slot_id: slot for slot in slots}
    resolved_now = now if now is not None else datetime.now(timezone.utc)

    records: list[AssetRecord] = []
    findings: list[Finding] = []

    for item in items:
        if item.action in _NO_OP_ACTIONS:
            continue

        slot = slots_by_id[item.slot_id]

        if item.action == "generate":
            plate_kind, was_valid = _plate_kind_for(slot)
            if quality == "final" and not was_valid:
                findings.append(
                    Finding(
                        gate="assets",
                        severity="error",
                        message=(
                            f"Refusing final-quality plate for slot {slot.slot_id!r}: "
                            f"{slot.detail!r} is a scene description, not one of the "
                            f"renderable texture plates ({', '.join(PLATE_KINDS)}). "
                            "Bind real footage or use --quality animatic for the "
                            "explicit grain fallback."
                        ),
                    )
                )
                continue
            out_path = out_dir / f"{slot.slot_id}-plate.mp4"
            spec = PlateSpec(kind=plate_kind, duration=slot.hold_seconds)
            build_plate(spec, out_path, grade=grade)

            notes = ""
            if not was_valid:
                notes = (
                    f"slot detail {slot.detail!r} did not name a valid plate kind "
                    f"({', '.join(PLATE_KINDS)}); defaulted to 'grain'"
                )

            records.append(
                AssetRecord(
                    asset_id=f"plate-{slot.slot_id}",
                    tier="atmospheric",
                    provider="ffmpeg-lavfi",
                    original_url="",
                    license="",
                    retrieved_at=_iso_utc(resolved_now),
                    local_path=str(out_path),
                    used_in_slots=(slot.slot_id,),
                    notes=notes,
                )
            )
            continue

        if item.action == "draw":
            spec, card_findings = cards.spec_for_slot(slot)
            findings.extend(card_findings)
            production_note = cards.production_note_reason(slot.detail, spec)
            if quality == "final" and production_note:
                findings.append(
                    Finding(
                        gate="assets",
                        severity="error",
                        message=(
                            f"Refusing final-quality card for slot {slot.slot_id!r}: "
                            f"{production_note}. Detail was {slot.detail!r}. Add "
                            "authored visible content or bind sourced footage; "
                            "--quality animatic keeps the labelled placeholder."
                        ),
                    )
                )
                continue

            if typography is None or palette is None:
                findings.append(
                    Finding(
                        gate="assets",
                        severity="error",
                        message=(
                            f"Slot {slot.slot_id!r} needs a card drawn but no "
                            f"typography/palette was supplied to execute_plan; "
                            f"cannot render it in the style pack's own fonts."
                        ),
                    )
                )
                continue

            out_path = out_dir / f"{slot.slot_id}-card.mp4"
            try:
                cards.build_card(
                    spec, out_path, typography, palette, grade, out_dir / ".cardwork"
                )
            except RuntimeError as exc:
                findings.append(
                    Finding(
                        gate="assets",
                        severity="error",
                        message=f"Card render failed for slot {slot.slot_id!r}: {exc}",
                    )
                )
                continue

            records.append(
                AssetRecord(
                    asset_id=f"card-{slot.slot_id}",
                    tier="atmospheric",
                    provider="rabbithole-cards",
                    original_url="",
                    license="",
                    retrieved_at=_iso_utc(resolved_now),
                    local_path=str(out_path),
                    used_in_slots=(slot.slot_id,),
                    notes=f"{spec.kind} card drawn from slot detail {slot.detail!r}",
                )
            )
            continue

        if item.action == "shoot":
            url = artifacts.get(slot.slot_id)
            out_path = out_dir / f"{slot.slot_id}-capture.mp4"
            try:
                result = capture.capture_to_video(
                    url, out_path, slot.hold_seconds, out_dir / ".capturework",
                    runner=capture_runner, transport=capture_transport,
                    quality=quality,
                )
            except RuntimeError as exc:
                findings.append(
                    Finding(
                        gate="assets",
                        severity="error",
                        message=f"Capture failed for slot {slot.slot_id!r}: {exc}",
                    )
                )
                continue

            for warning in result.warnings:
                findings.append(
                    Finding(gate="assets", severity="warning", message=warning)
                )

            inspection_note = ""
            if result.inspection is not None:
                inspection_note = (
                    f"; capture QA grey_stddev={result.inspection.grey_stddev:.2f}, "
                    f"distinct_grey_levels={result.inspection.distinct_grey_levels}"
                )

            records.append(
                AssetRecord(
                    asset_id=f"capture-{slot.slot_id}",
                    tier="primary",
                    provider=urlparse(url).netloc or "unknown",
                    original_url=url,
                    license="commentary-use",
                    retrieved_at=_iso_utc(resolved_now),
                    local_path=str(out_path),
                    used_in_slots=(slot.slot_id,),
                    notes=f"{result.kind} capture{inspection_note}",
                )
            )
            continue

        if item.action == "search":
            query = slot.queries[0] if slot.queries else f"{slot.kind} {slot.detail}".strip()
            hits, failures = archives.search_archives(query, archive_transport)
            if not hits:
                detail = f" Provider failures: {'; '.join(failures)}." if failures else ""
                findings.append(
                    Finding(
                        gate="assets",
                        severity="error",
                        message=(
                            f"No archival hits for slot {slot.slot_id!r} (query {query!r})."
                            f"{detail}"
                        ),
                    )
                )
                continue

            hit = hits[0]
            try:
                resolved = archives.resolve_media_url(hit, archive_transport)
            except RuntimeError as exc:
                findings.append(
                    Finding(
                        gate="assets",
                        severity="error",
                        message=(
                            f"Could not resolve a media URL for slot {slot.slot_id!r} "
                            f"(hit {hit.identifier!r}): {exc}"
                        ),
                    )
                )
                continue

            if not resolved.is_usable:
                findings.append(
                    Finding(
                        gate="assets",
                        severity="error",
                        message=(
                            f"Archival hit {resolved.identifier!r} for slot {slot.slot_id!r} "
                            f"never resolved to a usable asset (license={resolved.license!r}, "
                            f"media_url={resolved.media_url!r})."
                        ),
                    )
                )
                continue

            out_path = out_dir / f"{slot.slot_id}-{resolved.identifier}"
            try:
                archives.download(resolved, out_path, archive_transport)
            except RuntimeError as exc:
                findings.append(
                    Finding(
                        gate="assets",
                        severity="error",
                        message=f"Download refused for slot {slot.slot_id!r}: {exc}",
                    )
                )
                continue

            fields = archives.to_record_fields(
                resolved, f"archival-{slot.slot_id}", out_path, now=resolved_now
            )
            fields["used_in_slots"] = (slot.slot_id,)
            records.append(AssetRecord(**fields))
            continue

        if item.action == "fetch":
            url = artifacts.get(slot.slot_id)
            if not url:
                # plan_assets already blocks a fetch item with no artifact URL,
                # so this only fires if the caller passed a different
                # `artifacts` mapping to execute_plan than it planned against.
                findings.append(
                    Finding(
                        gate="assets",
                        severity="error",
                        message=(
                            f"No artifact URL available for slot {slot.slot_id!r} "
                            f"at execution time."
                        ),
                    )
                )
                continue

            out_path = out_dir / f"{slot.slot_id}-capture.mp4"
            try:
                fields = ytdlp.fetch_primary(
                    url, out_path, claims, runner=ytdlp_runner,
                    now=resolved_now, prober=ytdlp_prober,
                )
            except RuntimeError as exc:
                findings.append(
                    Finding(
                        gate="assets",
                        severity="error",
                        message=f"Primary fetch refused for slot {slot.slot_id!r}: {exc}",
                    )
                )
                continue

            fields = dict(fields)
            fields["used_in_slots"] = (slot.slot_id,)
            records.append(AssetRecord(**fields))
            continue

    return records, findings

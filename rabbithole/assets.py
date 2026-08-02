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
import hashlib
import json
import math
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from rabbithole import cards, source_text
from rabbithole.jsonio import read_json
from rabbithole.provenance import AssetRecord
from rabbithole.slots import Slot
from rabbithole.sources import archives, capture, frame_video, image_video, ytdlp
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
FINAL_MIN_SOURCE_BACKED_RATIO = 0.50
FINAL_MIN_SOURCE_PIXEL_RATIO = 0.35
# Backwards-compatible public name: the established 50% floor now measures
# source-backed editorial coverage. A separate source-pixel floor below keeps
# attributed paraphrase cards from satisfying the documentary gate alone.
FINAL_MIN_EVIDENCE_RATIO = FINAL_MIN_SOURCE_BACKED_RATIO
EVIDENCE_TIERS = frozenset({"primary", "archival"})
NON_SOURCE_PIXEL_EVIDENCE_PROVIDERS = frozenset(
    {
        # This provider deliberately burns "EDITORIAL PARAPHRASE" into the
        # frame. It is source-backed context, but it contains no retained
        # pixels from the cited page or media.
        "rabbithole-evidence-card",
        # This provider carries a short verbatim fragment plus visible source
        # metadata on an editorial reading surface. It is source-backed text,
        # not retained webpage imagery.
        "rabbithole-source-text-extract",
    }
)
SOURCE_REPLACEMENT_TIERS = frozenset({"primary", "archival", "illustrative"})
FLEXIBLE_VISUAL_KINDS = frozenset({"plate", "graphic"})

# A successful acquisition is not successful until its output can be opened.
# Keep this injectable because focused tests use tiny stand-in payloads, while
# the production default performs a real ffprobe decode check.
MediaProber = Callable[[Path], bool]
RecordCallback = Callable[[AssetRecord], None]


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
    """Source-backed and source-pixel coverage of the visual timeline.

    ``evidence_*`` retains the original public API and means source-backed:
    primary/archival media plus explicitly disclosed, attributed citation
    cards. ``source_pixel_*`` is the stricter subset containing retained source
    pixels. Final quality applies an independent floor to each measurement.
    """

    evidence_seconds: float
    total_seconds: float
    evidence_slots: int
    total_slots: int
    source_pixel_seconds: float
    source_pixel_slots: int

    @property
    def duration_ratio(self) -> float:
        return self.evidence_seconds / self.total_seconds if self.total_seconds else 1.0

    @property
    def slot_ratio(self) -> float:
        return self.evidence_slots / self.total_slots if self.total_slots else 1.0

    @property
    def source_backed_seconds(self) -> float:
        return self.evidence_seconds

    @property
    def source_backed_slots(self) -> int:
        return self.evidence_slots

    @property
    def source_backed_duration_ratio(self) -> float:
        return self.duration_ratio

    @property
    def source_backed_slot_ratio(self) -> float:
        return self.slot_ratio

    @property
    def source_pixel_duration_ratio(self) -> float:
        return (
            self.source_pixel_seconds / self.total_seconds
            if self.total_seconds
            else 1.0
        )

    @property
    def source_pixel_slot_ratio(self) -> float:
        return (
            self.source_pixel_slots / self.total_slots
            if self.total_slots
            else 1.0
        )


def normalize_quality(quality: str) -> str:
    """Validate and normalise a public quality-mode value."""
    value = (quality or "").strip().lower()
    if value not in QUALITY_MODES:
        raise ValueError(
            f"Unknown quality mode {quality!r}; expected one of {', '.join(QUALITY_MODES)}"
        )
    return value


def record_media_path(record: AssetRecord, project_root: Path) -> Path:
    """Resolve a ledger path on the current machine.

    Episode ledgers intentionally store project-relative POSIX paths so a
    project copied from Windows to macOS (or the reverse) remains portable.
    Absolute paths are still accepted for legacy/external media records.
    """
    raw = Path(record.local_path).expanduser()
    if raw.is_absolute():
        return raw
    return Path(project_root) / Path(*raw.parts)


def media_is_usable(path: Path, prober: MediaProber | None = None) -> bool:
    """Return whether *path* exists, is non-empty, and decodes as visual media."""
    candidate = Path(path)
    try:
        if not candidate.is_file() or candidate.stat().st_size <= 0:
            return False
    except OSError:
        return False

    active_prober = prober if prober is not None else ytdlp.media_decodes
    try:
        return bool(active_prober(candidate))
    except (OSError, RuntimeError, ValueError):
        return False


def usable_provenance_records(
    records: list[AssetRecord],
    project_root: Path,
    *,
    prober: MediaProber | None = None,
    severity: str = "warning",
) -> tuple[list[AssetRecord], list[Finding]]:
    """Separate ledger records backed by decodable local media from broken ones.

    A ledger entry alone must never suppress acquisition after a project is
    moved to another machine or after an interrupted writer leaves a partial
    file. Invalid entries remain in the append-only ledger; they simply do not
    satisfy planning until their deterministic local path has been restored.
    """
    usable: list[AssetRecord] = []
    findings: list[Finding] = []
    checked_paths: dict[Path, bool] = {}

    for record in records:
        path = record_media_path(record, project_root)
        try:
            cache_key = path.resolve()
        except OSError:
            cache_key = path.absolute()
        valid = checked_paths.get(cache_key)
        if valid is None:
            valid = media_is_usable(path, prober)
            checked_paths[cache_key] = valid
        if valid:
            usable.append(record)
            continue
        findings.append(
            Finding(
                gate="assets",
                severity=severity,
                message=(
                    f"Ledger asset {record.asset_id!r} does not have decodable "
                    f"local media at {str(path)!r}; it will not satisfy "
                    "acquisition on this machine."
                ),
            )
        )
    return usable, findings


def evidence_metrics(
    slots: list[Slot], records: list[AssetRecord]
) -> EvidenceMetrics:
    """Duration- and slot-weighted source-backed/source-pixel coverage.

    Primary and archival records are source-backed. A disclosed local citation
    card is useful source-backed context, but it is not a screenshot and cannot
    enter the source-pixel subset. A generated texture or ordinary explanatory
    card enters neither metric. Each slot counts at most once even if a
    malformed ledger has two claimants; :func:`check_provenance` separately
    rejects that ambiguity.
    """
    evidence_slot_ids = {
        slot_id
        for record in records
        if record.tier in EVIDENCE_TIERS
        for slot_id in record.used_in_slots
    }
    source_pixel_slot_ids = {
        slot_id
        for record in records
        if record.tier in EVIDENCE_TIERS
        and record.provider.strip().casefold()
        not in NON_SOURCE_PIXEL_EVIDENCE_PROVIDERS
        for slot_id in record.used_in_slots
    }
    total_seconds = sum(max(0.0, slot.hold_seconds) for slot in slots)
    evidence_seconds = sum(
        max(0.0, slot.hold_seconds)
        for slot in slots
        if slot.slot_id in evidence_slot_ids
    )
    source_pixel_seconds = sum(
        max(0.0, slot.hold_seconds)
        for slot in slots
        if slot.slot_id in source_pixel_slot_ids
    )
    return EvidenceMetrics(
        evidence_seconds=evidence_seconds,
        total_seconds=total_seconds,
        evidence_slots=sum(1 for slot in slots if slot.slot_id in evidence_slot_ids),
        total_slots=len(slots),
        source_pixel_seconds=source_pixel_seconds,
        source_pixel_slots=sum(
            1 for slot in slots if slot.slot_id in source_pixel_slot_ids
        ),
    )


def check_source_quality(
    slots: list[Slot],
    records: list[AssetRecord],
    *,
    quality: str = "final",
    min_evidence_ratio: float = FINAL_MIN_EVIDENCE_RATIO,
    min_source_pixel_ratio: float = FINAL_MIN_SOURCE_PIXEL_RATIO,
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
    if not 0.0 <= min_source_pixel_ratio <= 1.0:
        raise ValueError(
            "min_source_pixel_ratio must be between 0 and 1, got "
            f"{min_source_pixel_ratio}"
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
            spec, card_findings = cards.spec_for_slot(slot)
            findings.extend(card_findings)
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
                    f"(source-backed; {metrics.evidence_seconds:.2f}/"
                    f"{metrics.total_seconds:.2f}s; "
                    f"{metrics.evidence_slots}/{metrics.total_slots} slots, "
                    f"{metrics.slot_ratio:.1%}), below the final-quality minimum "
                    f"of {min_evidence_ratio:.0%}. Primary and archival ledger tiers "
                    "count as source-backed; generated atmospherics do not."
                ),
            )
        )
    if metrics.source_pixel_duration_ratio < min_source_pixel_ratio:
        findings.append(
            Finding(
                gate="source-quality",
                severity="error",
                message=(
                    "Source-pixel evidence ratio is "
                    f"{metrics.source_pixel_duration_ratio:.1%} by screen time "
                    f"({metrics.source_pixel_seconds:.2f}/"
                    f"{metrics.total_seconds:.2f}s; "
                    f"{metrics.source_pixel_slots}/{metrics.total_slots} slots, "
                    f"{metrics.source_pixel_slot_ratio:.1%}), below the "
                    f"final-quality minimum of {min_source_pixel_ratio:.0%}. "
                    "Retained primary/archival pixels count; locally authored "
                    "citation cards are source-backed only and cannot satisfy "
                    "this floor."
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
    acquisition_mode: str = ""
    max_use_seconds: float | None = None
    # JSON-friendly browser targeting passed to CaptureSpec. This is authored
    # per slot because one URL can support several different paragraphs, dates,
    # or detail crops.
    capture_spec: dict[str, object] | None = None
    capture_strategy: str = ""
    capture_note: str = ""
    # Screenshot-only slots may derive a silent frame from an already retained
    # source-video slot instead of opening the video page in a browser.
    source_video_slot: str = ""
    source_frame_timestamp: float | None = None
    source_frame_crop: list[int] | tuple[int, int, int, int] | None = None
    source_image_crop: list[int] | tuple[int, int, int, int] | None = None
    # SPDX-style identifier for a directly downloaded source image. This is
    # intentionally separate from free-form rights_note: acquisition must
    # never infer a licence from prose or from the source host.
    source_license: str = ""
    source_attribution: str = ""
    source_date_label: str = ""
    # Optional opt-in key for browser-source reuse. URL equality alone is not
    # enough: two slots may need different page locations, crops, or video
    # frames from the same URL. Only identical non-empty fingerprints share a
    # capture request.
    capture_spec_fingerprint: str = ""


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

    overlays: dict[str, tuple[Path, dict]] = {}
    for overlay_path in sorted(path.parent.glob("capture-targets*.json")):
        overlay = read_json(overlay_path)
        if not isinstance(overlay, dict):
            raise RuntimeError(
                f"{overlay_path}: expected an object keyed by slot id"
            )
        for slot_id, entry in overlay.items():
            if not isinstance(slot_id, str) or not slot_id:
                raise RuntimeError(
                    f"{overlay_path}: capture-target slot ids must be non-empty strings"
                )
            if not isinstance(entry, dict):
                raise RuntimeError(
                    f"{overlay_path}: capture target {slot_id!r} must be an object"
                )
            if slot_id in overlays:
                previous = overlays[slot_id][0]
                raise RuntimeError(
                    f"{overlay_path}: capture target {slot_id!r} is also defined "
                    f"in {previous}; each slot needs one authored target"
                )
            overlays[slot_id] = (overlay_path, entry)

    if not overlays:
        return artifacts

    by_slot = {
        artifact.slot_id: index
        for index, artifact in enumerate(artifacts)
        if artifact.slot_id
    }
    for slot_id, (overlay_path, entry) in overlays.items():
        index = by_slot.get(slot_id)
        if index is None:
            raise RuntimeError(
                f"{overlay_path}: capture target {slot_id!r} has no bound "
                "artifact in artifacts.json"
            )
        artifact = artifacts[index]
        expected_source = re.sub(r"-s\d{3}$", "", artifact.artifact_id)
        source = str(entry.get("source", "")).strip()
        if source and source != expected_source:
            raise RuntimeError(
                f"{overlay_path}: capture target {slot_id!r} names source "
                f"{source!r}, but the bound artifact belongs to "
                f"{expected_source!r}"
            )
        spec = entry.get("spec")
        if spec is not None and not isinstance(spec, dict):
            raise RuntimeError(
                f"{overlay_path}: capture target {slot_id!r} spec must be an object"
            )
        strategy = str(entry.get("strategy", "")).strip()
        note = str(entry.get("note", "")).strip()
        artifacts[index] = dataclasses.replace(
            artifact,
            capture_spec=dict(spec) if spec is not None else artifact.capture_spec,
            capture_strategy=strategy or artifact.capture_strategy,
            capture_note=note or artifact.capture_note,
        )
    return artifacts


def artifact_urls_by_slot(artifacts: list[Artifact]) -> dict[str, str]:
    """Slot bindings only: slot_id -> url, for artifacts that carry a slot_id.

    Binding is a separate, later step from cataloguing -- most artifacts
    have no `slot_id` until an editor assigns them to a shot in the slot
    plan, so this is almost always a strict subset of `artifacts`.
    """
    return {artifact.slot_id: artifact.url for artifact in artifacts if artifact.slot_id}


def artifact_bindings_by_slot(artifacts: list[Artifact]) -> dict[str, Artifact]:
    """Return complete bound catalogue records keyed by slot.

    Execution needs the complete record, rather than only its URL, so source
    identity and rights notes survive into the provenance ledger.  Keep
    :func:`artifact_urls_by_slot` for callers that only need URLs.
    """
    return {
        artifact.slot_id: artifact
        for artifact in artifacts
        if artifact.slot_id
    }


ACQUISITION_MODES = frozenset({"", "auto", "screenshot-only", "video-only"})
_ACQUISITION_ACTIONS = {
    "screenshot-only": "shoot",
    "video-only": "fetch",
}
SOURCE_IMAGE_STRATEGIES = frozenset({"licensed-direct-image-crop-deferred"})
# A syntactically plausible token is not enough here: accepting ``CCO-1.0``
# or ``copyrighted`` would turn a typo or prose label into a false licence
# assertion for retained source pixels.
SOURCE_IMAGE_SPDX_LICENSES = frozenset(
    {
        "CC-PDDC",
        "CC0-1.0",
        "CC-BY-1.0",
        "CC-BY-2.0",
        "CC-BY-2.5",
        "CC-BY-3.0",
        "CC-BY-4.0",
    }
)
_SOURCE_LICENSE_REF_RE = re.compile(
    r"^LicenseRef-[A-Za-z0-9][A-Za-z0-9.-]*$"
)
LOCAL_EVIDENCE_CARD_STRATEGIES = frozenset(
    {
        "manual-editorial-card-no-page-capture",
        "catalogue-metadata-card-no-page-or-media-access",
        "local-current-context-citation-card-after-fetch-failure",
    }
)
SOURCE_TEXT_EXTRACT_STRATEGY = "source-text-extract"
EVIDENCE_CARD_DISCLOSURE = "EDITORIAL PARAPHRASE · SOURCE-ATTRIBUTED"
CAPTURE_STRATEGIES = frozenset(
    {
        "",
        SOURCE_TEXT_EXTRACT_STRATEGY,
        *SOURCE_IMAGE_STRATEGIES,
        *LOCAL_EVIDENCE_CARD_STRATEGIES,
    }
)


def _citation_card_primary_text(slot_detail: str, capture_note: str = "") -> str:
    """Return reviewer-facing copy instead of raw marker-routing metadata.

    Screenshot slot details commonly begin with internal authoring syntax such
    as ``source=wt-guardian detail=...``.  That is useful to the acquisition
    planner but looks like a debug overlay when rendered as evidence.  Manual
    fallback notes are deliberately authored with a concise, attributable
    first sentence; prefer that sentence, then fall back to a cleaned detail.
    """
    note = str(capture_note or "").strip()
    if note:
        sentence = re.split(r"(?<=[.!?])\s+", note, maxsplit=1)[0].strip()
        if sentence:
            return sentence

    text = str(slot_detail or "").split(";", 1)[0].strip()
    detail_match = re.search(r"(?:^|\s)detail=(.+)$", text)
    if detail_match:
        text = detail_match.group(1).strip()
    text = re.sub(r"^source=\S+\s*", "", text).strip()
    return text or "Source context"


def _capture_exact_text(value: dict[str, object] | None) -> str:
    """Return the authored verbatim target, never a selector or page label."""

    spec = capture.CaptureSpec.from_value(value)
    direct = str(spec.text or "").strip()
    if direct:
        return direct
    if spec.scroll_target is not None:
        return str(spec.scroll_target.text or "").strip()
    return ""


def _artifact_policy_block(
    artifact: Artifact, slot: Slot, action: str
) -> str | None:
    """Return a fail-closed planning reason for an incompatible source policy."""
    mode = str(artifact.acquisition_mode or "").strip().lower()
    if mode not in ACQUISITION_MODES:
        expected = ", ".join(repr(value) for value in sorted(ACQUISITION_MODES))
        return (
            f"artifact {artifact.artifact_id!r} bound to slot {slot.slot_id!r} "
            f"has unsupported acquisition_mode {artifact.acquisition_mode!r}; "
            f"expected one of {expected}"
        )

    strategy = str(artifact.capture_strategy or "").strip().lower()
    if action == "shoot" and strategy not in CAPTURE_STRATEGIES:
        expected = ", ".join(repr(value) for value in sorted(CAPTURE_STRATEGIES))
        return (
            f"artifact {artifact.artifact_id!r} bound to slot {slot.slot_id!r} "
            f"has unsupported capture_strategy {artifact.capture_strategy!r}; "
            f"expected one of {expected}"
        )

    required_action = _ACQUISITION_ACTIONS.get(mode)
    if required_action is not None and action != required_action:
        return (
            f"artifact {artifact.artifact_id!r} is {mode!r}, but slot "
            f"{slot.slot_id!r} ({slot.kind!r}) requires acquisition action "
            f"{action!r}; rebind it to a compatible source slot"
        )

    if action == "shoot" and artifact.capture_spec is not None:
        try:
            capture.CaptureSpec.from_value(artifact.capture_spec)
        except (TypeError, ValueError) as exc:
            return (
                f"artifact {artifact.artifact_id!r} bound to slot "
                f"{slot.slot_id!r} has invalid capture_spec: {exc}"
            )

    if action == "shoot" and strategy == SOURCE_TEXT_EXTRACT_STRATEGY:
        if artifact.capture_spec is None:
            return (
                f"artifact {artifact.artifact_id!r} bound to slot {slot.slot_id!r} "
                "uses source-text-extract without capture_spec.text or "
                "capture_spec.scroll_target.text"
            )
        exact_text = _capture_exact_text(artifact.capture_spec)
        if not exact_text:
            return (
                f"artifact {artifact.artifact_id!r} bound to slot {slot.slot_id!r} "
                "uses source-text-extract without an exact authored text target; "
                "selectors and coordinates are not source text"
            )
        try:
            source_text.SourceTextExtractSpec(
                text=exact_text,
                publisher=source_text.publisher_from_url(artifact.url),
                title=artifact.title,
                date=artifact.date,
                url=artifact.url,
                duration=slot.hold_seconds,
            )
        except (TypeError, ValueError) as exc:
            return (
                f"artifact {artifact.artifact_id!r} bound to slot {slot.slot_id!r} "
                f"has invalid source-text-extract metadata: {exc}"
            )

    if artifact.source_video_slot:
        if action != "shoot":
            return (
                f"artifact {artifact.artifact_id!r} declares source_video_slot "
                f"{artifact.source_video_slot!r}, but only screenshot acquisition "
                "can derive a retained source frame"
            )
        if artifact.capture_spec is not None:
            return (
                f"artifact {artifact.artifact_id!r} cannot combine capture_spec "
                "with source_video_slot; choose browser evidence or a source frame"
            )
        timestamp = artifact.source_frame_timestamp
        if (
            timestamp is None
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(float(timestamp))
            or float(timestamp) < 0
        ):
            return (
                f"artifact {artifact.artifact_id!r} has invalid "
                f"source_frame_timestamp {timestamp!r}; it must be a finite "
                "non-negative number"
            )
        crop = artifact.source_frame_crop
        if crop is not None:
            valid_crop = (
                isinstance(crop, (list, tuple))
                and len(crop) == 4
                and all(
                    isinstance(value, int) and not isinstance(value, bool)
                    for value in crop
                )
                and crop[0] >= 0
                and crop[1] >= 0
                and crop[2] > 0
                and crop[3] > 0
            )
            if not valid_crop:
                return (
                    f"artifact {artifact.artifact_id!r} has invalid "
                    f"source_frame_crop {crop!r}; expected "
                    "[x, y, width, height] in source pixels"
                )

    if strategy in SOURCE_IMAGE_STRATEGIES:
        if artifact.capture_spec is not None or artifact.source_video_slot:
            return (
                f"artifact {artifact.artifact_id!r} source-image strategy cannot "
                "combine with capture_spec or source_video_slot"
            )
        if not isinstance(artifact.source_license, str):
            return (
                f"artifact {artifact.artifact_id!r} has invalid source_license "
                f"{artifact.source_license!r}; expected one SPDX-style licence "
                "identifier as a string"
            )
        source_license = artifact.source_license.strip()
        if not source_license:
            return (
                f"artifact {artifact.artifact_id!r} uses a direct source image "
                "but has no source_license; record an explicit machine-readable "
                "licence identifier such as 'CC0-1.0'"
            )
        if source_license in SOURCE_IMAGE_SPDX_LICENSES:
            pass
        elif _SOURCE_LICENSE_REF_RE.fullmatch(source_license):
            if not str(artifact.rights_note or "").strip():
                return (
                    f"artifact {artifact.artifact_id!r} uses custom "
                    f"source_license {source_license!r} but has no rights_note; "
                    "a LicenseRef-* must document the permission or legal basis"
                )
            if not str(artifact.source_attribution or "").strip():
                return (
                    f"artifact {artifact.artifact_id!r} uses custom "
                    f"source_license {source_license!r} but has no "
                    "source_attribution"
                )
        else:
            allowed = ", ".join(sorted(SOURCE_IMAGE_SPDX_LICENSES))
            return (
                f"artifact {artifact.artifact_id!r} has invalid source_license "
                f"{artifact.source_license!r}; expected one approved SPDX "
                f"identifier ({allowed}) or a documented LicenseRef-*"
            )
        crop = artifact.source_image_crop
        if crop is not None:
            valid_crop = (
                isinstance(crop, (list, tuple))
                and len(crop) == 4
                and all(
                    isinstance(value, int) and not isinstance(value, bool)
                    for value in crop
                )
                and crop[0] >= 0
                and crop[1] >= 0
                and crop[2] > 0
                and crop[3] > 0
            )
            if not valid_crop:
                return (
                    f"artifact {artifact.artifact_id!r} has invalid "
                    f"source_image_crop {crop!r}; expected "
                    "[x, y, width, height] in source pixels"
                )

    limit = artifact.max_use_seconds
    if limit is None:
        return None
    if (
        isinstance(limit, bool)
        or not isinstance(limit, (int, float))
        or not math.isfinite(float(limit))
        or float(limit) <= 0
    ):
        return (
            f"artifact {artifact.artifact_id!r} bound to slot {slot.slot_id!r} "
            f"has invalid max_use_seconds {limit!r}; it must be a positive "
            "finite number"
        )
    if slot.hold_seconds > float(limit) + 1e-6:
        return (
            f"artifact {artifact.artifact_id!r} permits at most "
            f"{float(limit):g}s of use, but slot {slot.slot_id!r} holds it for "
            f"{slot.hold_seconds:.1f}s"
        )
    return None


def _iso_utc(ts: datetime) -> str:
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _artifact_from_binding(
    slot_id: str, binding: Artifact | str
) -> Artifact:
    """Normalize the legacy slot->URL execution input to a full artifact."""
    if isinstance(binding, Artifact):
        return binding
    return Artifact(artifact_id="", url=str(binding), slot_id=slot_id)


def _join_notes(*notes: str) -> str:
    return " | ".join(note.strip() for note in notes if note and note.strip())


def _artifact_provenance_note(artifact: Artifact) -> str:
    """Serialize catalogue identity and rights context into ledger notes."""
    labelled = (
        ("artifact_id", artifact.artifact_id),
        ("title", artifact.title),
        ("date", artifact.date),
        ("source_role", artifact.source_role),
        ("rights_note", artifact.rights_note),
        (
            "capture_spec",
            json.dumps(
                capture.CaptureSpec.from_value(artifact.capture_spec).to_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
            if artifact.capture_spec is not None
            else "",
        ),
        ("capture_strategy", artifact.capture_strategy),
        ("capture_note", artifact.capture_note),
        ("source_video_slot", artifact.source_video_slot),
        (
            "source_frame_timestamp",
            f"{float(artifact.source_frame_timestamp):.6f}"
            if artifact.source_frame_timestamp is not None
            else "",
        ),
        (
            "source_frame_crop",
            json.dumps(list(artifact.source_frame_crop), separators=(",", ":"))
            if artifact.source_frame_crop is not None
            else "",
        ),
        (
            "source_image_crop",
            json.dumps(list(artifact.source_image_crop), separators=(",", ":"))
            if artifact.source_image_crop is not None
            else "",
        ),
        ("source_license", artifact.source_license),
    )
    parts = [f"{label}={value!r}" for label, value in labelled if value]
    return f"catalogue metadata: {'; '.join(parts)}" if parts else ""


def _capture_reuse_key(
    artifact: Artifact,
    quality: str,
    *,
    effective_capture_spec: dict[str, object] | None = None,
) -> tuple[str, str, str] | None:
    """Opt-in identity for a truly identical browser capture request.

    The same URL can legitimately need different scroll positions, DOM
    targets, crops, or video frames. Reuse is therefore disabled unless the
    research catalogue explicitly assigns the same non-empty capture-spec
    fingerprint to the requests.
    """
    fingerprint = str(artifact.capture_spec_fingerprint or "").strip()
    if not fingerprint:
        return None
    if effective_capture_spec is not None:
        effective_digest = hashlib.sha256(
            json.dumps(
                effective_capture_spec,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        fingerprint = f"{fingerprint}:{effective_digest}"
    return (artifact.url.strip(), fingerprint, quality)


def _browser_capture_specs_for_slots(
    slots: list[Slot],
    artifacts: dict[str, Artifact | str],
    *,
    quality: str,
) -> dict[str, dict[str, object]]:
    """Resolve editorial capture behavior against the complete slot order.

    Repeated evidence lines from one page form an episode-wide reading
    sequence.  Only the first browser slot for a source URL may perform the
    authored establish-to-target move; every later occurrence holds its target
    instead of replaying the same zoom, even after intervening graphics or
    other sources.  At final quality, exact text targets also receive the baked
    yellow evidence highlight.  Computing this from *all* slots keeps a partial
    ``--refresh --slot`` run consistent with a full acquisition.
    """

    resolved: dict[str, dict[str, object]] = {}
    seen_browser_urls: set[str] = set()

    for slot in slots:
        binding = artifacts.get(slot.slot_id)
        if KIND_TO_ACTION.get(slot.kind) != "shoot" or not binding:
            continue
        artifact = _artifact_from_binding(slot.slot_id, binding)
        strategy = str(artifact.capture_strategy or "").strip().lower()
        if strategy or artifact.source_video_slot or artifact.source_license:
            continue
        try:
            spec = capture.CaptureSpec.from_value(artifact.capture_spec)
        except (TypeError, ValueError):
            # Planning reports the authored validation error with slot context.
            continue
        if not spec.needs_browser_control:
            continue

        source_url = artifact.url.strip()
        changed = False
        if quality == "final" and (
            spec.text
            or (spec.scroll_target is not None and spec.scroll_target.text)
        ) and not spec.highlight:
            spec = dataclasses.replace(spec, highlight=True)
            changed = True
        if source_url in seen_browser_urls and spec.motion is not None:
            spec = dataclasses.replace(spec, motion=None)
            changed = True

        if changed:
            resolved[slot.slot_id] = spec.to_dict()
        seen_browser_urls.add(source_url)

    return resolved


def plan_assets(
    slots: list[Slot], records: list[AssetRecord], artifacts: list[Artifact]
) -> list[PlanItem]:
    """Decide what each slot needs.

    Checked in this order per slot, and the first match wins:
      1. Bound source conflicts with its acquisition/use policy -> `blocked`.
      2. Claimed by a ledger record of the wrong tier -> `stale`.
      3. Claimed by a ledger record of the right tier -> `satisfied`.
      4. Unknown kind -> `blocked`.
      5. Source slot with no artifact bound to it -> `blocked`.
      6. Otherwise, the kind's normal action from `KIND_TO_ACTION`.

    The stale-ledger check exists because slot ids are positional. Adding a
    `[SHOT:]` marker renumbers every slot after it, so a ledger written against
    the earlier plan still claims slot ids that now name different shots -- and
    a check of "is this slot id claimed" accepts that silently, putting a
    graphic card into an archival slot with no error raised anywhere.

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

    bindings = artifact_bindings_by_slot(artifacts)

    items: list[PlanItem] = []
    for slot in slots:
        tier = KIND_TO_TIER.get(slot.kind, "")
        action = KIND_TO_ACTION.get(slot.kind)
        artifact = bindings.get(slot.slot_id)

        if action in ("fetch", "shoot") and artifact is not None:
            policy_block = _artifact_policy_block(artifact, slot, action)
            if policy_block is not None:
                items.append(
                    PlanItem(
                        slot_id=slot.slot_id,
                        kind=slot.kind,
                        tier=tier,
                        action=_BLOCKED,
                        reason=policy_block,
                    )
                )
                continue

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

        if action in ("fetch", "shoot") and artifact is None:
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
            reason = (
                f"fetch {artifact.url!r} via yt-dlp, gated on the claims ledger"
            )
        elif action == "shoot":
            reason = (
                f"capture {artifact.url!r} with the headless browser "
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


def select_plan_items(
    items: list[PlanItem],
    *,
    slot_ids: tuple[str, ...] | list[str] = (),
    batch_size: int | None = None,
) -> list[PlanItem]:
    """Select exact slots and/or the next bounded batch of actionable work.

    Batches deliberately ignore no-op entries such as ``satisfied``. Running
    ``assets --batch-size 20`` repeatedly therefore advances through the next
    twenty missing assets after each atomic ledger checkpoint, without an
    offset that becomes stale as records are added.
    """
    if batch_size is not None and batch_size <= 0:
        raise ValueError(f"batch_size must be positive, got {batch_size}")

    selected_ids = set(slot_ids)
    selected = [
        item for item in items if not selected_ids or item.slot_id in selected_ids
    ]
    if batch_size is None:
        return selected
    actionable = [item for item in selected if item.action not in _NO_OP_ACTIONS]
    return actionable[:batch_size]


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
    artifacts: dict[str, Artifact | str] | None = None,
    typography: dict | None = None,
    palette: dict | None = None,
    quality: str = "animatic",
    media_prober: MediaProber | None = None,
    on_record: RecordCallback | None = None,
    source_media_by_slot: dict[str, Path] | None = None,
) -> tuple[list[AssetRecord], list[Finding]]:
    """Source everything the plan calls for, returning new records and any failures.

    `artifacts` accepts complete ``Artifact`` bindings (the CLI path) or the
    legacy slot_id -> URL mapping. Complete bindings preserve catalogue
    identity and rights context in the resulting provenance notes. Their
    acquisition and duration policies are rechecked immediately before I/O so
    a stale, externally constructed plan cannot bypass a catalogue change.

    `typography` and `palette` are the style pack dicts a `draw` item needs to
    render a card; they are loaded from `style/` by the caller. A `draw` item
    with either missing is reported rather than rendered, so a caller that
    forgot to pass them gets a clear message instead of a card in default
    fonts.

    Every produced path is required to exist and pass a visual-media decode
    probe before its record is accepted. ``on_record`` runs immediately after
    that probe for each slot. The CLI uses it to checkpoint the provenance
    ledger atomically, so an interruption resumes at the next missing slot
    instead of discarding an otherwise successful batch.

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
    resolved_source_media = {
        slot_id: Path(path).resolve()
        for slot_id, path in (source_media_by_slot or {}).items()
    }
    source_text_transport = capture.memoized_transport(capture_transport)
    source_image_cache: dict[str, Path] = {}
    slots_by_id = {slot.slot_id: slot for slot in slots}
    browser_capture_specs = _browser_capture_specs_for_slots(
        slots, artifacts, quality=quality
    )
    original_card_specs: dict[str, cards.CardSpec] = {}
    card_findings_by_slot: dict[str, list[Finding]] = {}
    grouped_card_specs: dict[str, cards.CardSpec] = {}
    graphic_run: list[tuple[str, cards.CardSpec]] = []

    def flush_graphic_run() -> None:
        if graphic_run:
            grouped_card_specs.update(cards.group_same_heading_specs(graphic_run))
            graphic_run.clear()

    # Group only truly adjacent graphics.  A source clip between two cards is
    # an editorial boundary even when their headings happen to match.
    for planned_slot in slots:
        if planned_slot.kind != "graphic":
            flush_graphic_run()
            continue
        card_spec, card_findings = cards.spec_for_slot(planned_slot)
        original_card_specs[planned_slot.slot_id] = card_spec
        card_findings_by_slot[planned_slot.slot_id] = card_findings
        graphic_run.append((planned_slot.slot_id, card_spec))
    flush_graphic_run()
    resolved_now = now if now is not None else datetime.now(timezone.utc)

    records: list[AssetRecord] = []
    findings: list[Finding] = []

    def accept_record(record: AssetRecord) -> bool:
        path = Path(record.local_path)
        if not media_is_usable(path, media_prober):
            slot_list = ", ".join(record.used_in_slots) or "no slot"
            findings.append(
                Finding(
                    gate="assets",
                    severity="error",
                    message=(
                        f"Produced asset {record.asset_id!r} for {slot_list} did "
                        f"not leave decodable media at {str(path)!r}; provenance "
                        "was not recorded."
                    ),
                )
            )
            return False
        records.append(record)
        for slot_id in record.used_in_slots:
            resolved_source_media[slot_id] = Path(record.local_path).resolve()
        if on_record is not None:
            on_record(record)
        return True

    def retained_source_image(url: str) -> Path:
        cached = source_image_cache.get(url)
        if cached is not None and cached.is_file() and cached.stat().st_size > 0:
            return cached
        cache_dir = out_dir / ".sourcecache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
        destination = cache_dir / f"{digest}.image"
        if not destination.is_file() or destination.stat().st_size <= 0:
            body = capture.fetch_source_bytes(url, capture_transport)
            temporary = cache_dir / f".{digest}.part"
            temporary.write_bytes(body)
            os.replace(temporary, destination)
        source_image_cache[url] = destination
        return destination

    def capture_browser_video(
        artifact: Artifact,
        slot: Slot,
        out_path: Path,
        *,
        transport: capture.Transport | None,
        targeted_capture: capture.TargetedCapture | None = None,
    ) -> capture.CaptureResult:
        """Capture one browser slot, preserving a static reading hold on retry.

        Some archived pages resolve a text target through the motion CDP path
        but return an invalid/blank native still crop.  For later lines in a
        same-source episode sequence, use the authored move only as an internal
        locator and derive a constant final-frame asset.  The timeline therefore
        never replays the move even when the browser needs it to obtain valid
        pixels.
        """

        effective_value = browser_capture_specs.get(
            slot.slot_id, artifact.capture_spec
        )
        effective_spec = capture.CaptureSpec.from_value(effective_value)
        original_spec = capture.CaptureSpec.from_value(artifact.capture_spec)
        try:
            return capture.capture_to_video(
                artifact.url,
                out_path,
                slot.hold_seconds,
                out_dir / ".capturework",
                runner=capture_runner,
                transport=transport,
                quality=quality,
                spec=effective_value,
                targeted_capture=targeted_capture,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as initial_error:
            if original_spec.motion is None or effective_spec.motion is not None:
                raise

            fallback_path = (
                out_dir / ".capturework" / f".{slot.slot_id}-motion-locator.mp4"
            )
            fallback_spec = dataclasses.replace(
                effective_spec, motion=original_spec.motion
            )
            try:
                motion_result = capture.capture_to_video(
                    artifact.url,
                    fallback_path,
                    slot.hold_seconds,
                    out_dir / ".capturework",
                    runner=capture_runner,
                    transport=transport,
                    quality=quality,
                    spec=fallback_spec.to_dict(),
                    targeted_capture=targeted_capture,
                )
                motion = (
                    motion_result.framing.motion
                    if motion_result.framing is not None
                    else None
                )
                if motion is None:
                    raise RuntimeError(
                        "motion locator returned no motion framing metadata"
                    )
                final_timestamp = max(
                    0.0, motion.duration_seconds - (1.0 / motion.fps)
                )
                frame_video.derive_source_frame_video(
                    fallback_path,
                    out_path,
                    timestamp=final_timestamp,
                    duration=slot.hold_seconds,
                )
            except (OSError, RuntimeError, TypeError, ValueError) as fallback_error:
                raise RuntimeError(
                    f"{initial_error}; static reading-hold fallback also failed: "
                    f"{fallback_error}"
                ) from fallback_error
            finally:
                fallback_path.unlink(missing_ok=True)

            fallback_framing = motion_result.framing
            if fallback_framing is not None:
                fallback_framing = dataclasses.replace(
                    fallback_framing,
                    mode="target-hold-fallback",
                    motion=None,
                )
            return dataclasses.replace(
                motion_result,
                path=out_path,
                warnings=(
                    *motion_result.warnings,
                    "Native static target capture failed; retained the exact "
                    "highlighted final frame from an internal motion locator. "
                    "No repeated move is present in the emitted asset.",
                ),
                framing=fallback_framing,
            )

    # Capturing a page is much more expensive than deriving a slot-length file
    # from an already captured still. Reuse is strictly opt-in through an
    # explicit capture-spec fingerprint; URL equality by itself is unsafe.
    # Each slot still receives a distinct output and asset_id.
    shoot_durations: dict[tuple[str, str, str], float] = {}
    for planned in items:
        if planned.action != "shoot":
            continue
        binding = artifacts.get(planned.slot_id)
        if not binding:
            continue
        artifact = _artifact_from_binding(planned.slot_id, binding)
        planned_slot = slots_by_id.get(planned.slot_id)
        if planned_slot is None:
            continue
        if _artifact_policy_block(artifact, planned_slot, planned.action) is not None:
            continue
        key = _capture_reuse_key(
            artifact,
            quality,
            effective_capture_spec=browser_capture_specs.get(planned.slot_id),
        )
        if key is None:
            continue
        shoot_durations[key] = max(
            shoot_durations.get(key, 0.0), planned_slot.hold_seconds
        )
    capture_cache: dict[tuple[str, str, str], tuple[Path, object, str]] = {}

    # Several evidence slots can point at different DOM targets on one archived
    # page.  They must not share pixels by URL -- every authored CaptureSpec is
    # still resolved and captured independently -- but opening that exact page
    # once is both faster and gentler on rate-limited archives.  Build batches
    # only for targeted browser captures without an explicit identical-spec
    # fingerprint; the existing fingerprint cache below keeps its established
    # semantics.
    targeted_slots_by_url: dict[str, list[str]] = {}
    for planned in items:
        if planned.action != "shoot":
            continue
        binding = artifacts.get(planned.slot_id)
        planned_slot = slots_by_id.get(planned.slot_id)
        if not binding or planned_slot is None:
            continue
        artifact = _artifact_from_binding(planned.slot_id, binding)
        if _artifact_policy_block(artifact, planned_slot, planned.action) is not None:
            continue
        strategy = str(artifact.capture_strategy or "").strip().lower()
        if (
            strategy
            or artifact.source_video_slot
            or artifact.capture_spec_fingerprint
        ):
            continue
        capture_spec = capture.CaptureSpec.from_value(
            browser_capture_specs.get(planned.slot_id, artifact.capture_spec)
        )
        if not capture_spec.needs_browser_control:
            continue
        targeted_slots_by_url.setdefault(artifact.url, []).append(planned.slot_id)
    targeted_slots_by_url = {
        url: slot_ids
        for url, slot_ids in targeted_slots_by_url.items()
        if len(slot_ids) > 1
    }
    shared_capture_started: set[str] = set()
    shared_capture_results: dict[str, capture.CaptureResult] = {}
    shared_capture_errors: dict[str, str] = {}

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

            accept_record(
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
            original_spec = original_card_specs[slot.slot_id]
            spec = grouped_card_specs.get(slot.slot_id, original_spec)
            card_findings = card_findings_by_slot[slot.slot_id]
            findings.extend(card_findings)
            if any(
                finding.severity == "error" for finding in card_findings
            ):
                continue
            production_note = cards.production_note_reason(slot.detail, original_spec)
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

            accept_record(
                AssetRecord(
                    asset_id=f"card-{slot.slot_id}",
                    tier="atmospheric",
                    provider="rabbithole-cards",
                    original_url="",
                    license="",
                    retrieved_at=_iso_utc(resolved_now),
                    local_path=str(out_path),
                    used_in_slots=(slot.slot_id,),
                    notes=_join_notes(
                        f"{spec.kind} card drawn from slot detail {slot.detail!r}",
                        (
                            "stable grouped reading slide; active row "
                            f"{spec.active_item_index + 1}/{len(spec.items)}"
                            if spec.active_item_index is not None
                            else ""
                        ),
                    ),
                )
            )
            continue

        if item.action == "shoot":
            binding = artifacts.get(slot.slot_id)
            if not binding:
                findings.append(
                    Finding(
                        gate="assets",
                        severity="error",
                        message=(
                            f"No artifact URL available for slot {slot.slot_id!r} "
                            "at execution time."
                        ),
                    )
                )
                continue
            artifact = _artifact_from_binding(slot.slot_id, binding)
            policy_block = _artifact_policy_block(artifact, slot, item.action)
            if policy_block is not None:
                findings.append(
                    Finding(
                        gate="assets",
                        severity="error",
                        message=f"Acquisition refused: {policy_block}.",
                    )
                )
                continue
            url = artifact.url
            out_path = out_dir / f"{slot.slot_id}-capture.mp4"
            strategy = str(artifact.capture_strategy or "").strip().lower()
            if strategy == SOURCE_TEXT_EXTRACT_STRATEGY:
                if typography is None or palette is None:
                    findings.append(
                        Finding(
                            gate="assets",
                            severity="error",
                            message=(
                                f"Source-text extract for slot {slot.slot_id!r} "
                                "needs the style typography and palette."
                            ),
                        )
                    )
                    continue
                exact_text = _capture_exact_text(artifact.capture_spec)
                extract_spec = source_text.SourceTextExtractSpec(
                    text=exact_text,
                    publisher=source_text.publisher_from_url(url),
                    title=artifact.title,
                    date=artifact.date,
                    url=url,
                    duration=slot.hold_seconds,
                )
                try:
                    fetched_source = capture.fetch_source_bytes(
                        url, source_text_transport
                    )
                except Exception as exc:
                    findings.append(
                        Finding(
                            gate="assets",
                            severity="error",
                            message=(
                                f"Source-text verification fetch failed for slot "
                                f"{slot.slot_id!r}: {exc}. No extract was rendered."
                            ),
                        )
                    )
                    continue
                if not capture.source_contains_exact_text(
                    fetched_source, exact_text
                ):
                    findings.append(
                        Finding(
                            gate="assets",
                            severity="error",
                            message=(
                                f"Source-text verification failed for slot "
                                f"{slot.slot_id!r}: exact target {exact_text!r} "
                                "was not found in fetched visible source text. "
                                "No extract was rendered."
                            ),
                        )
                    )
                    continue
                try:
                    source_text.build_source_text_extract(
                        extract_spec,
                        out_path,
                        typography,
                        palette,
                        grade,
                        out_dir / ".source-text-work",
                    )
                except RuntimeError as exc:
                    findings.append(
                        Finding(
                            gate="assets",
                            severity="error",
                            message=(
                                f"Source-text extract render failed for slot "
                                f"{slot.slot_id!r}: {exc}"
                            ),
                        )
                    )
                    continue
                accept_record(
                    AssetRecord(
                        asset_id=f"capture-{slot.slot_id}",
                        tier="primary",
                        provider="rabbithole-source-text-extract",
                        original_url=url,
                        license="commentary-use",
                        retrieved_at=_iso_utc(resolved_now),
                        local_path=str(out_path),
                        used_in_slots=(slot.slot_id,),
                        notes=_join_notes(
                            (
                                "verbatim source-text extract rendered as "
                                "editorial typesetting; no webpage image retained; "
                                "target verified against fetched visible source text; "
                                f"exact_target={exact_text!r}"
                            ),
                            _artifact_provenance_note(artifact),
                        ),
                    )
                )
                continue
            if strategy in LOCAL_EVIDENCE_CARD_STRATEGIES:
                if typography is None or palette is None:
                    findings.append(
                        Finding(
                            gate="assets",
                            severity="error",
                            message=(
                                f"Evidence-card fallback for slot "
                                f"{slot.slot_id!r} needs the style typography "
                                "and palette."
                            ),
                        )
                    )
                    continue
                primary_text = _citation_card_primary_text(
                    slot.detail, artifact.capture_note
                )
                context = (
                    artifact.date
                    or (
                        (
                            "CURRENT CONTEXT · ACCESSED "
                            f"{resolved_now.strftime('%d %b %Y').upper()}"
                        )
                        if "current" in strategy or "manual" in strategy
                        else artifact.source_role.replace("-", " ").upper()
                    )
                )
                spec = cards.CardSpec(
                    kind="document",
                    heading=artifact.title or urlparse(url).netloc or "SOURCE",
                    duration=slot.hold_seconds,
                    items=tuple(
                        value
                        for value in (
                            primary_text,
                            context,
                            f"SOURCE · {urlparse(url).netloc}",
                        )
                        if value
                    ),
                    disclosure=EVIDENCE_CARD_DISCLOSURE,
                )
                try:
                    cards.build_card(
                        spec,
                        out_path,
                        typography,
                        palette,
                        grade,
                        out_dir / ".cardwork",
                    )
                except RuntimeError as exc:
                    findings.append(
                        Finding(
                            gate="assets",
                            severity="error",
                            message=(
                                f"Evidence-card fallback failed for slot "
                                f"{slot.slot_id!r}: {exc}"
                            ),
                        )
                    )
                    continue
                accept_record(
                    AssetRecord(
                        asset_id=f"capture-{slot.slot_id}",
                        tier="primary",
                        provider="rabbithole-evidence-card",
                        original_url=url,
                        license="commentary-use",
                        retrieved_at=_iso_utc(resolved_now),
                        local_path=str(out_path),
                        used_in_slots=(slot.slot_id,),
                        notes=_join_notes(
                            "local attributed citation-card fallback; manual review required",
                            _artifact_provenance_note(artifact),
                        ),
                    )
                )
                continue
            if strategy in SOURCE_IMAGE_STRATEGIES:
                try:
                    source_path = retained_source_image(url)
                    image_video.derive_source_image_video(
                        source_path,
                        out_path,
                        duration=slot.hold_seconds,
                        crop=artifact.source_image_crop,
                        attribution=artifact.source_attribution or artifact.title,
                        date_label=artifact.source_date_label or artifact.date,
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    findings.append(
                        Finding(
                            gate="assets",
                            severity="error",
                            message=(
                                f"Source-image derivation failed for slot "
                                f"{slot.slot_id!r}: {exc}"
                            ),
                        )
                    )
                    continue
                accept_record(
                    AssetRecord(
                        asset_id=f"capture-{slot.slot_id}",
                        tier="primary",
                        provider="rabbithole-source-image",
                        original_url=url,
                        license=artifact.source_license.strip(),
                        retrieved_at=_iso_utc(resolved_now),
                        local_path=str(out_path),
                        used_in_slots=(slot.slot_id,),
                        notes=_join_notes(
                            "retained licensed source-image crop",
                            _artifact_provenance_note(artifact),
                        ),
                    )
                )
                continue
            if artifact.source_video_slot:
                source_path = resolved_source_media.get(artifact.source_video_slot)
                if source_path is None:
                    findings.append(
                        Finding(
                            gate="assets",
                            severity="error",
                            message=(
                                f"Source-frame derivation for slot {slot.slot_id!r} "
                                f"requires retained source-video slot "
                                f"{artifact.source_video_slot!r}, but no validated "
                                "media record for it is available."
                            ),
                        )
                    )
                    continue
                try:
                    frame_video.derive_source_frame_video(
                        source_path,
                        out_path,
                        timestamp=float(artifact.source_frame_timestamp),
                        duration=slot.hold_seconds,
                        crop=artifact.source_frame_crop,
                        attribution=artifact.source_attribution or artifact.title,
                        date_label=artifact.source_date_label or artifact.date,
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    findings.append(
                        Finding(
                            gate="assets",
                            severity="error",
                            message=(
                                f"Source-frame derivation failed for slot "
                                f"{slot.slot_id!r}: {exc}"
                            ),
                        )
                    )
                    continue
                accept_record(
                    AssetRecord(
                        asset_id=f"capture-{slot.slot_id}",
                        tier="primary",
                        provider="rabbithole-source-frame",
                        original_url=url,
                        license="commentary-use",
                        retrieved_at=_iso_utc(resolved_now),
                        local_path=str(out_path),
                        used_in_slots=(slot.slot_id,),
                        notes=_join_notes(
                            (
                                "silent retained source frame at "
                                f"{float(artifact.source_frame_timestamp):.3f}s"
                            ),
                            _artifact_provenance_note(artifact),
                        ),
                    )
                )
                continue
            reuse_note = ""
            shared_slot_ids = targeted_slots_by_url.get(url)
            if shared_slot_ids is not None:
                if url not in shared_capture_started:
                    shared_capture_started.add(url)
                    probe_transport = capture.memoized_transport(capture_transport)
                    with capture.shared_page_capture() as targeted_capture:
                        for shared_slot_id in shared_slot_ids:
                            shared_slot = slots_by_id[shared_slot_id]
                            shared_artifact = _artifact_from_binding(
                                shared_slot_id, artifacts[shared_slot_id]
                            )
                            shared_out_path = (
                                out_dir / f"{shared_slot_id}-capture.mp4"
                            )
                            try:
                                shared_capture_results[shared_slot_id] = (
                                    capture_browser_video(
                                        shared_artifact,
                                        shared_slot,
                                        shared_out_path,
                                        transport=probe_transport,
                                        targeted_capture=targeted_capture,
                                    )
                                )
                            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                                shared_capture_errors[shared_slot_id] = str(exc)
                shared_error = shared_capture_errors.get(slot.slot_id)
                if shared_error is not None:
                    findings.append(
                        Finding(
                            gate="assets",
                            severity="error",
                            message=(
                                f"Capture failed for slot {slot.slot_id!r}: "
                                f"{shared_error}"
                            ),
                        )
                    )
                    continue
                result = shared_capture_results.get(slot.slot_id)
                if result is None:
                    findings.append(
                        Finding(
                            gate="assets",
                            severity="error",
                            message=(
                                f"Capture failed for slot {slot.slot_id!r}: "
                                "shared page batch returned no result"
                            ),
                        )
                    )
                    continue
            else:
                capture_key = _capture_reuse_key(
                    artifact,
                    quality,
                    effective_capture_spec=browser_capture_specs.get(slot.slot_id),
                )
                cached = (
                    capture_cache.get(capture_key)
                    if capture_key is not None
                    else None
                )
                if cached is not None and cached[0].is_file():
                    cached_path, result, source_slot_id = cached
                    try:
                        if cached_path.resolve() != out_path.resolve():
                            out_path.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(cached_path, out_path)
                        reuse_note = (
                            f"source capture reused in-batch from slot "
                            f"{source_slot_id!r}"
                        )
                    except OSError as exc:
                        findings.append(
                            Finding(
                                gate="assets",
                                severity="error",
                                message=(
                                    f"Capture reuse failed for slot "
                                    f"{slot.slot_id!r}: {exc}"
                                ),
                            )
                        )
                        continue
                else:
                    try:
                        requested_duration = (
                            shoot_durations.get(capture_key, slot.hold_seconds)
                            if capture_key is not None
                            else slot.hold_seconds
                        )
                        if requested_duration == slot.hold_seconds:
                            result = capture_browser_video(
                                artifact,
                                slot,
                                out_path,
                                transport=capture_transport,
                            )
                        else:
                            result = capture.capture_to_video(
                                url,
                                out_path,
                                requested_duration,
                                out_dir / ".capturework",
                                runner=capture_runner,
                                transport=capture_transport,
                                quality=quality,
                                spec=browser_capture_specs.get(
                                    slot.slot_id, artifact.capture_spec
                                ),
                            )
                    except RuntimeError as exc:
                        findings.append(
                            Finding(
                                gate="assets",
                                severity="error",
                                message=(
                                    f"Capture failed for slot "
                                    f"{slot.slot_id!r}: {exc}"
                                ),
                            )
                        )
                        continue
                    if capture_key is not None:
                        capture_cache[capture_key] = (
                            out_path,
                            result,
                            slot.slot_id,
                        )

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
            framing_note = ""
            if result.framing is not None:
                framing_note = (
                    "browser_framing="
                    + json.dumps(
                        result.framing.to_dict(),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            effective_capture_note = ""
            effective_capture_spec = browser_capture_specs.get(slot.slot_id)
            if effective_capture_spec is not None:
                effective_capture_note = (
                    "effective_capture_spec="
                    + json.dumps(
                        effective_capture_spec,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )

            accept_record(
                AssetRecord(
                    asset_id=f"capture-{slot.slot_id}",
                    tier="primary",
                    provider=urlparse(url).netloc or "unknown",
                    original_url=url,
                    license="commentary-use",
                    retrieved_at=_iso_utc(resolved_now),
                    local_path=str(out_path),
                    used_in_slots=(slot.slot_id,),
                    notes=_join_notes(
                        f"{result.kind} capture{inspection_note}",
                        framing_note,
                        effective_capture_note,
                        reuse_note,
                        _artifact_provenance_note(artifact),
                    ),
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
            accept_record(AssetRecord(**fields))
            continue

        if item.action == "fetch":
            binding = artifacts.get(slot.slot_id)
            if not binding:
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

            artifact = _artifact_from_binding(slot.slot_id, binding)
            policy_block = _artifact_policy_block(artifact, slot, item.action)
            if policy_block is not None:
                findings.append(
                    Finding(
                        gate="assets",
                        severity="error",
                        message=f"Acquisition refused: {policy_block}.",
                    )
                )
                continue
            url = artifact.url
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
            fields["notes"] = _join_notes(
                str(fields.get("notes", "")),
                _artifact_provenance_note(artifact),
            )
            accept_record(AssetRecord(**fields))
            continue

    return records, findings

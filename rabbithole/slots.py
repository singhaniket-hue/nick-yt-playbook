"""The slot plan: which asset is needed when.

A slot is a narrative unit needing one asset, derived from a [SHOT:] marker. It is
NOT a cut. A later phase subdivides a slot into several cuts against the same asset
to reach the target average shot length, so a 30-second slot may become ten cuts.
"""

from __future__ import annotations

from dataclasses import dataclass

from rabbithole.markers import SHOT_KINDS
from rabbithole.validate import Finding

# The format's target average shot length (ASL) once a slot is subdivided into
# cuts. A slot that holds one asset for too long forces too many consecutive
# cuts against that single asset, which reads as visually monotonous no matter
# how it's reframed. MAX_CUTS_PER_ASSET is the editorial ceiling on how many
# cuts one asset should carry before a slot is considered too long; multiplying
# by the target ASL gives the hold duration, in seconds, at which that ceiling
# is reached.
TARGET_ASL_SECONDS = 3.12
MAX_CUTS_PER_ASSET = 5
MAX_SLOT_HOLD_SECONDS = TARGET_ASL_SECONDS * MAX_CUTS_PER_ASSET


@dataclass(frozen=True)
class Slot:
    slot_id: str
    kind: str
    detail: str
    start: float
    end: float
    queries: tuple[str, ...]
    marker_word_index: int

    @property
    def hold_seconds(self) -> float:
        return self.end - self.start


def build_slots(document: dict) -> list[Slot]:
    """Derive the slot plan from a timing document.

    One slot per [SHOT:] marker, in timeline order. A slot runs from its marker's
    `seconds` to the next SHOT marker's `seconds`, or to `duration_seconds` for the
    last slot. If narration begins before the first SHOT marker, an implicit leading
    slot is prepended spanning 0.0 to the first marker's time (kind "plate", detail
    "implicit opening") so atmospherics have something to fill it with -- and so the
    gap is visible in the plan rather than silently absorbed into the first real
    slot.

    `queries` is a seed for asset search, not a search strategy: it is a single
    string built from `kind` and `detail` and nothing more. Real query refinement
    (synonyms, date ranges, source-specific phrasing) is an authoring decision made
    later, by whatever retrieves the asset -- this function does no retrieval and
    does not attempt to.

    A document with no [SHOT:] markers yields an empty list, not an implicit slot
    covering the whole episode: an episode with no shot markers is an authoring
    failure for `check_slots` to report, not something this function should paper
    over.
    """
    shot_markers = [m for m in document.get("markers", []) if m["kind"] == "SHOT"]
    if not shot_markers:
        return []

    duration_seconds = document["duration_seconds"]

    parsed = []
    for marker in shot_markers:
        arg = marker.get("arg", "")
        parts = arg.split(None, 1)
        kind = parts[0] if parts else ""
        detail = parts[1] if len(parts) > 1 else ""
        parsed.append((marker["seconds"], kind, detail, marker["word_index"]))

    slots: list[Slot] = []
    counter = 1

    first_seconds = parsed[0][0]
    if first_seconds > 0.0:
        slots.append(
            Slot(
                slot_id=f"s{counter:03d}",
                kind="plate",
                detail="implicit opening",
                start=0.0,
                end=first_seconds,
                queries=("plate implicit opening",),
                marker_word_index=0,
            )
        )
        counter += 1

    for index, (seconds, kind, detail, word_index) in enumerate(parsed):
        end = parsed[index + 1][0] if index + 1 < len(parsed) else duration_seconds
        query = f"{kind} {detail}".strip()
        slots.append(
            Slot(
                slot_id=f"s{counter:03d}",
                kind=kind,
                detail=detail,
                start=seconds,
                end=end,
                queries=(query,),
                marker_word_index=word_index,
            )
        )
        counter += 1

    return slots


def check_slots(slots: list[Slot], document: dict) -> list[Finding]:
    """Verify the slot plan tiles the episode without gaps or overlaps.

    This gate mixes severities: every structural finding above (bad kind, a
    non-positive hold, a gap, an overlap, a plan that doesn't reach the
    episode's duration) is `severity="error"`, because each one means the plan
    is broken. The shot-density finding below is `severity="warning"`: a slot
    that holds one asset too long produces a watchable-but-dull episode, not a
    broken one, so it's an editorial judgement rather than a correctness
    failure. A future caller of this function must respect that distinction
    -- e.g. failing a build on any "error" finding while only surfacing
    "warning" findings as advisories -- rather than treating every finding
    the same way.
    """
    findings: list[Finding] = []
    duration_seconds = document.get("duration_seconds", 0.0)

    if not slots:
        if duration_seconds > 0:
            findings.append(
                Finding(
                    gate="slots",
                    severity="error",
                    message=(
                        f"No slots in a {duration_seconds}s episode; every episode "
                        f"needs at least one [SHOT:] marker."
                    ),
                )
            )
        return findings

    for slot in slots:
        if slot.kind not in SHOT_KINDS:
            findings.append(
                Finding(
                    gate="slots",
                    severity="error",
                    message=(
                        f"Slot {slot.slot_id!r} has kind {slot.kind!r}; expected "
                        f"one of {', '.join(SHOT_KINDS)}."
                    ),
                )
            )
        if slot.hold_seconds <= 0:
            findings.append(
                Finding(
                    gate="slots",
                    severity="error",
                    message=(
                        f"Slot {slot.slot_id!r} has hold_seconds "
                        f"{slot.hold_seconds}; it must be positive."
                    ),
                )
            )

    for earlier, later in zip(slots, slots[1:]):
        if earlier.end < later.start:
            findings.append(
                Finding(
                    gate="slots",
                    severity="error",
                    message=(
                        f"Gap between {earlier.slot_id!r} (ends {earlier.end}) and "
                        f"{later.slot_id!r} (starts {later.start}); slots must tile "
                        f"the episode contiguously."
                    ),
                )
            )
        elif earlier.end > later.start:
            findings.append(
                Finding(
                    gate="slots",
                    severity="error",
                    message=(
                        f"Overlap between {earlier.slot_id!r} (ends {earlier.end}) "
                        f"and {later.slot_id!r} (starts {later.start}); slots must "
                        f"tile the episode contiguously."
                    ),
                )
            )

    last = slots[-1]
    if abs(last.end - duration_seconds) > 0.05:
        findings.append(
            Finding(
                gate="slots",
                severity="error",
                message=(
                    f"Last slot {last.slot_id!r} ends at {last.end}, but the "
                    f"episode duration is {duration_seconds}; the plan must reach "
                    f"the end within 0.05s."
                ),
            )
        )

    for slot in slots:
        if slot.hold_seconds > MAX_SLOT_HOLD_SECONDS:
            implied_cuts = round(slot.hold_seconds / TARGET_ASL_SECONDS)
            findings.append(
                Finding(
                    gate="slots",
                    severity="warning",
                    message=(
                        f"Slot {slot.slot_id!r} holds for {slot.hold_seconds:.1f}s, "
                        f"which implies roughly {implied_cuts} cuts against a "
                        f"single asset at the {TARGET_ASL_SECONDS}s target average "
                        f"shot length; add more [SHOT:] markers in this span."
                    ),
                )
            )

    return findings

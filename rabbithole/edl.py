"""The edit decision list: slots plus the timing spine become an ordered cut list.

A slot is one narrative unit holding one asset. A cut is one camera framing on
that asset. This module subdivides each slot's hold into cuts at the format's
target average shot length (`TARGET_ASL_SECONDS`, imported from `slots`), so a
40-second slot against one image becomes roughly 13 reframed cuts.

**Cuts land on word boundaries, not on a metronome.** The obvious approach --
divide each slot into exact `TARGET_ASL_SECONDS` intervals -- is deterministic
but mechanically even, and a cut lands mid-word whenever the arithmetic doesn't
happen to line up with speech. Instead, every candidate cut boundary this module
invents is snapped to the nearest word START time in the timing document
(`snap_to_word`). Variation in shot length then comes from where words actually
fall -- i.e. from speech rhythm -- rather than from a clock, and the result is
still fully deterministic: same words, same boundaries, same EDL every run. If
you are looking at this code wondering whether the even-interval version would
be simpler: yes, and it was rejected on purpose. Do not "simplify" it back.

**Pinned boundaries are inviolable.** Every slot start, every [SILENCE:] marker
time, and every [CHAPTER:] marker time is placed by the script, not invented by
this module, and must appear in the EDL at exactly its marker second --
`pinned_boundaries` never snaps these to a word, and `build_edl` never merges
them away. This has a sharp consequence explored below.

**Merge direction for cuts under MIN_CUT_SECONDS: always merge-back.** Only the
*algorithmically chosen* interior split points (the ASL-fill boundaries inside a
gap between two pins) are subject to the minimum. When a candidate split would
create a cut shorter than `MIN_CUT_SECONDS` -- either too close to the boundary
before it, or too close to the gap's end -- that candidate is simply dropped,
and the cut before it runs on through that span instead. The short interval is
absorbed into its PREDECESSOR, never into its successor. This is a one-line rule
applied uniformly (see `_split_gap`), so a locally dense cluster of candidates
thins out predictably rather than by case analysis.

Pins are exempt from that minimum, deliberately. If two pins fall closer
together than `MIN_CUT_SECONDS` -- two [SILENCE:] markers 0.5s apart, or a slot
whose whole hold is under a second -- this module still emits the resulting
short cut rather than dropping or moving a pin to hide the problem. A pin is
author intent (a scripted silence, a scripted shot change); silently deleting
or relocating one to satisfy a pacing minimum would corrupt the cut list
without telling anyone. `check_edl` flags the resulting short cut as an error
instead, which is the correct owner of that judgement: a human decides whether
to move the markers, not this function.

**Animatic and editorial modes serve different review stages.** The default
`animatic` mode preserves the original pacing diagnostic: `FRAMINGS` cycles
continuously across the final cut list and long holds receive word-snapped
`asl-fill` cuts. `editorial` mode is evidence-led. It emits cuts only at
authored slot/SILENCE/CHAPTER boundaries, starts every distinct slot in `wide`,
and alternates to `push-in` only when an authored internal pin splits that same
slot. Two different assets may therefore meet on the same wide framing without
being mistaken for a jump cut.

**Transition priority when pins coincide:** CHAPTER > SILENCE > slot boundary.
[CHAPTER:] and [SILENCE:] both resolve to `dip-to-black`; a bare slot boundary
resolves to `glitch` in animatic mode and a hard `cut` in editorial mode. When
two pins land within 0.01s of each other (a [SHOT:] and a [CHAPTER:] on
adjacent script lines routinely resolve to the same second),
`pinned_boundaries` keeps the higher-priority one's reason and transition, but
always keeps the SLOT boundary's own second as the representative time when a
slot pin is in the cluster -- otherwise a coincident CHAPTER or SILENCE marker
with a slightly different float could nudge the boundary out of its slot's
`[start, end)` range and misattribute the cut.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from rabbithole.slots import TARGET_ASL_SECONDS, Slot
from rabbithole.validate import Finding

ASL_MIN_SECONDS = 2.95
ASL_MAX_SECONDS = 3.35
MIN_CUT_SECONDS = 1.0

FRAMINGS = ("wide", "push-in", "detail", "slow-pan")
TRANSITIONS = ("cut", "glitch", "dip-to-black")
EDL_MODES = ("animatic", "editorial")

_EDITORIAL_FRAMINGS = ("wide", "push-in")

_DEDUPE_SECONDS = 0.01

# Priority for which pin "wins" a coincidence: higher wins reason/transition.
_SLOT_PRIORITY = 0
_SILENCE_PRIORITY = 1
_CHAPTER_PRIORITY = 2


@dataclass(frozen=True)
class Cut:
    index: int
    start: float
    end: float
    slot_id: str
    origin: str  # "script" | "asl-fill"
    framing: str
    transition: str
    reason: str

    @property
    def duration(self) -> float:
        return self.end - self.start


def word_starts(document: dict) -> list[float]:
    """Every word's start time, ascending."""
    return sorted(w["start"] for w in document.get("words", []))


def snap_to_word(target: float, starts: list[float]) -> float:
    """The word start nearest `target`.

    Returns `target` unchanged when `starts` is empty -- this is what makes the
    ASL-fill split logic degrade gracefully to even subdivision on a document
    with no word timings (see `build_edl`), with no separate fallback branch
    needed.

    On an exact tie between the start just before and just after `target`, the
    earlier one wins. `bisect_left` plus a `<=` comparison encodes that: ties
    take the `before` branch.
    """
    if not starts:
        return target

    i = bisect.bisect_left(starts, target)
    if i == 0:
        return starts[0]
    if i == len(starts):
        return starts[-1]

    before, after = starts[i - 1], starts[i]
    if target - before <= after - target:
        return before
    return after


def _slot_pins(slots: list[Slot]) -> list[tuple[float, int, str, str]]:
    return [
        (slot.start, _SLOT_PRIORITY, f"slot boundary: {slot.slot_id} begins", "glitch")
        for slot in slots
    ]


def _marker_pins(document: dict) -> list[tuple[float, int, str, str]]:
    pins: list[tuple[float, int, str, str]] = []
    for marker in document.get("markers", []):
        if marker["kind"] == "SILENCE":
            pins.append(
                (
                    marker["seconds"],
                    _SILENCE_PRIORITY,
                    f"[SILENCE:{marker['arg']}] beat -- a deliberate pause, not to be straddled by a cut",
                    "dip-to-black",
                )
            )
        elif marker["kind"] == "CHAPTER":
            pins.append(
                (
                    marker["seconds"],
                    _CHAPTER_PRIORITY,
                    f"[CHAPTER:{marker['arg']}] boundary",
                    "dip-to-black",
                )
            )
    return pins


def pinned_boundaries(slots: list[Slot], document: dict) -> list[tuple[float, str, str]]:
    """Boundary times that subdivision may not move.

    Returns (seconds, origin_reason, transition) triples, ascending, deduplicated.

    Dedup groups pins within `_DEDUPE_SECONDS` of the previous group's
    representative time (adjacent-pair clustering, not a full pairwise
    comparison -- sufficient because pins are processed in ascending order and
    real coincidences are exact or near-exact floats derived from the same
    word). Within a group, the highest-priority pin's reason and transition
    win (CHAPTER > SILENCE > slot boundary), but the representative second is
    pinned to the slot boundary's own value whenever a slot pin participates,
    so a coincident marker never nudges a cut out of its slot's range.
    """
    raw = sorted(_slot_pins(slots) + _marker_pins(document), key=lambda r: r[0])

    groups: list[dict] = []
    for seconds, priority, reason, transition in raw:
        if groups and abs(seconds - groups[-1]["rep_seconds"]) <= _DEDUPE_SECONDS:
            group = groups[-1]
            if priority > group["priority"]:
                group["priority"] = priority
                group["reason"] = reason
                group["transition"] = transition
            if priority == _SLOT_PRIORITY:
                group["rep_seconds"] = seconds
            continue
        groups.append(
            {
                "rep_seconds": seconds,
                "priority": priority,
                "reason": reason,
                "transition": transition,
            }
        )

    result = [(g["rep_seconds"], g["reason"], g["transition"]) for g in groups]
    result.sort(key=lambda t: t[0])
    return result


def _split_gap(
    gap_start: float,
    gap_end: float,
    starts: list[float],
) -> list[float]:
    """Interior cut-start times inside (gap_start, gap_end), word-snapped.

    Ideal boundaries subdivide the gap into `max(1, round(gap / TARGET_ASL))`
    even pieces; each is snapped to the nearest word start (or left as the
    ideal time, unchanged, when there are no words -- see `snap_to_word`,
    which is where the no-words fallback actually lives).

    A candidate is dropped -- merged back into the cut before it -- if it
    would leave less than MIN_CUT_SECONDS on either side (from the previous
    accepted boundary, or from `gap_end`). Because candidates are generated in
    increasing order and `gap_end - candidate` only shrinks as candidates
    increase, once the tail-side check starts failing it fails for every
    later candidate too, so the drop is stable regardless of how many
    candidates are considered.
    """
    gap_seconds = gap_end - gap_start
    fills = max(1, round(gap_seconds / TARGET_ASL_SECONDS))
    if fills <= 1:
        return []

    accepted: list[float] = []
    last = gap_start
    for k in range(1, fills):
        ideal = gap_start + gap_seconds * k / fills
        candidate = snap_to_word(ideal, starts)
        if candidate - last >= MIN_CUT_SECONDS and gap_end - candidate >= MIN_CUT_SECONDS:
            accepted.append(candidate)
            last = candidate
    return accepted


_ASL_FILL_REASON = (
    f"ASL fill: subdivision toward the {TARGET_ASL_SECONDS}s target average shot length"
)


def build_edl(
    slots: list[Slot],
    document: dict,
    mode: str = "animatic",
) -> list[Cut]:
    """Turn slots plus the timing spine into an ordered cut list.

    Walks each slot, collects the pins that fall inside it (its own start pin
    plus any SILENCE/CHAPTER pin strictly inside).

    In ``animatic`` mode, each gap receives word-snapped ASL cuts via
    `_split_gap`; the authored cut at a pin has ``origin="script"`` and each
    generated interior cut has ``origin="asl-fill"``. Framing cycles across
    the complete list, preserving the original preview behaviour.

    In ``editorial`` mode no interior timing is invented. Every cut begins at
    an authored pin and therefore has ``origin="script"``. A distinct slot
    begins wide; if SILENCE/CHAPTER pins split that slot, its framing alternates
    wide/push-in across those authored spans. Bare slot changes are hard cuts,
    while authored SILENCE/CHAPTER transitions remain dip-to-black.
    """
    if mode not in EDL_MODES:
        raise ValueError(
            f"Unknown EDL mode {mode!r}; expected one of {', '.join(EDL_MODES)}"
        )
    if not slots:
        return []

    duration = document["duration_seconds"]
    starts = word_starts(document)
    pins = pinned_boundaries(slots, document)

    pre_cuts: list[dict] = []
    last_index = len(slots) - 1

    for i, slot in enumerate(slots):
        slot_pins = [p for p in pins if slot.start <= p[0] < slot.end]
        terminal = duration if i == last_index else slot.end

        for j, (seconds, reason, transition) in enumerate(slot_pins):
            gap_start = seconds
            gap_end = slot_pins[j + 1][0] if j + 1 < len(slot_pins) else terminal

            splits = (
                _split_gap(gap_start, gap_end, starts)
                if mode == "animatic"
                else []
            )
            boundaries = [gap_start, *splits, gap_end]

            for k in range(len(boundaries) - 1):
                start, end = boundaries[k], boundaries[k + 1]
                if k == 0:
                    authored_transition = (
                        "cut"
                        if mode == "editorial" and transition == "glitch"
                        else transition
                    )
                    pre_cuts.append(
                        {
                            "start": start,
                            "end": end,
                            "slot_id": slot.slot_id,
                            "origin": "script",
                            "transition": authored_transition,
                            "reason": reason,
                            "framing": (
                                _EDITORIAL_FRAMINGS[
                                    j % len(_EDITORIAL_FRAMINGS)
                                ]
                                if mode == "editorial"
                                else None
                            ),
                        }
                    )
                else:
                    pre_cuts.append(
                        {
                            "start": start,
                            "end": end,
                            "slot_id": slot.slot_id,
                            "origin": "asl-fill",
                            "transition": "cut",
                            "reason": _ASL_FILL_REASON,
                            "framing": None,
                        }
                    )

    cuts = [
        Cut(
            index=index,
            start=pc["start"],
            end=pc["end"],
            slot_id=pc["slot_id"],
            origin=pc["origin"],
            framing=(
                pc["framing"]
                if mode == "editorial"
                else FRAMINGS[index % len(FRAMINGS)]
            ),
            transition=pc["transition"],
            reason=pc["reason"],
        )
        for index, pc in enumerate(pre_cuts)
    ]
    return cuts


def check_edl(cuts: list[Cut], document: dict) -> list[Finding]:
    """Verify the EDL against the format's pacing rules.

    Every rule below is `severity="error"` except the average-shot-length band,
    which is `severity="warning"`: an EDL that drifts outside the target ASL
    band is editorially loose, not structurally broken, so a caller should fail
    a build on any error while only surfacing the ASL warning as an advisory.
    """
    findings: list[Finding] = []

    def error(message: str) -> None:
        findings.append(Finding(gate="edl", severity="error", message=message))

    duration = document.get("duration_seconds", 0.0)

    total = sum(c.duration for c in cuts)
    if abs(total - duration) > 1.0:
        error(
            f"EDL totals {total:.2f}s but the episode is {duration:.2f}s; "
            f"they must agree within 1.0s."
        )

    for a, b in zip(cuts, cuts[1:]):
        if abs(a.end - b.start) > 0.01:
            error(
                f"Cut {a.index} ends at {a.end:.3f}s but cut {b.index} starts at "
                f"{b.start:.3f}s; consecutive cuts must be contiguous."
            )

    for cut in cuts:
        if not cut.reason.strip():
            error(f"Cut {cut.index} has an empty reason.")

    for a, b in zip(cuts, cuts[1:]):
        if a.slot_id == b.slot_id and a.framing == b.framing:
            error(
                f"Cuts {a.index} and {b.index} are adjacent and share framing "
                f"{a.framing!r} on slot {a.slot_id!r}; adjacent cuts using the "
                f"same asset/slot must differ in framing or they read as a "
                f"jump cut."
            )

    distinct_transitions = {c.transition for c in cuts}
    if len(distinct_transitions) > 4:
        error(
            f"EDL uses {len(distinct_transitions)} distinct transition values "
            f"({sorted(distinct_transitions)}); at most 4 are allowed."
        )

    for cut in cuts:
        if cut.duration < MIN_CUT_SECONDS - 1e-9:
            error(
                f"Cut {cut.index} is {cut.duration:.3f}s, shorter than the "
                f"{MIN_CUT_SECONDS}s minimum."
            )

    if cuts:
        asl = total / len(cuts)
        if not (ASL_MIN_SECONDS <= asl <= ASL_MAX_SECONDS):
            findings.append(
                Finding(
                    gate="edl",
                    severity="warning",
                    message=(
                        f"Average shot length is {asl:.2f}s, outside the "
                        f"{ASL_MIN_SECONDS}-{ASL_MAX_SECONDS}s target band."
                    ),
                )
            )

    return findings

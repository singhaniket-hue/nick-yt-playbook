"""The five review gates from the spec, as pure functions over a ParsedScript."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from rabbithole.jsonio import read_json
from rabbithole.markers import SHOT_KINDS, ParsedScript, markers_of, word_count

WORD_COUNT_MIN = 4500
WORD_COUNT_MAX = 7400

ACT_BUDGETS = {1: 90, 2: 440, 3: 3360, 4: 1590, 5: 530}
ACT_TOLERANCE = 0.15

REHOOK_MIN_MINUTES = 3
REHOOK_MAX_MINUTES = 5


class ValidationProfileError(ValueError):
    """A project brief exists but cannot safely define validation targets."""


@dataclass(frozen=True)
class ValidationProfile:
    """All duration-sensitive script gates for one episode.

    A project brief can describe a shorter or longer final episode than the
    original 30-40 minute format.  The profile keeps the total word-count gate,
    the five act budgets, and re-hook timing on the same WPM-derived basis.
    Without an explicit brief target, ``default`` preserves the original hard
    gates exactly.
    """

    word_count_min: int
    word_count_max: int
    act_budgets: Mapping[int, int]
    wpm: int
    word_count_tolerance: float = ACT_TOLERANCE
    act_budget_tolerance: float = ACT_TOLERANCE
    target_duration_minutes: float | None = None
    target_word_count: int | None = None
    source: str = "default long-format gates"

    @classmethod
    def default(cls, wpm: int = 177) -> ValidationProfile:
        _require_positive_wpm(wpm)
        return cls(
            word_count_min=WORD_COUNT_MIN,
            word_count_max=WORD_COUNT_MAX,
            act_budgets=MappingProxyType(dict(ACT_BUDGETS)),
            wpm=wpm,
        )

    @classmethod
    def for_duration(
        cls,
        target_duration_minutes: float,
        wpm: int = 177,
        *,
        word_count_tolerance: float = ACT_TOLERANCE,
        act_budget_tolerance: float = ACT_TOLERANCE,
        act_budgets: Mapping[int, int] | None = None,
        source: str = "project brief",
    ) -> ValidationProfile:
        _require_positive_wpm(wpm)
        _require_tolerance(word_count_tolerance, "word_count_tolerance")
        _require_tolerance(act_budget_tolerance, "act_budget_tolerance")
        if (
            isinstance(target_duration_minutes, bool)
            or not isinstance(target_duration_minutes, (int, float))
            or not math.isfinite(target_duration_minutes)
            or target_duration_minutes <= 0
        ):
            raise ValidationProfileError(
                "target_duration_minutes must be a positive finite number."
            )

        target_words = int(round(target_duration_minutes * wpm))
        if target_words < len(ACT_BUDGETS):
            raise ValidationProfileError(
                "target_duration_minutes is too short to allocate words across all five acts."
            )

        # Decimal avoids turning an exact boundary such as 2640 * 1.15 into
        # 3035.9999999999995 before floor is applied.
        decimal_target = Decimal(target_words)
        decimal_tolerance = Decimal(str(word_count_tolerance))
        word_min = int(
            (decimal_target * (Decimal(1) - decimal_tolerance)).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        word_max = int(
            (decimal_target * (Decimal(1) + decimal_tolerance)).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        budgets = (
            _validated_act_word_budgets(act_budgets)
            if act_budgets is not None
            else _scale_act_budgets(target_words)
        )
        budget_total = sum(budgets.values())
        if not word_min <= budget_total <= word_max:
            raise ValidationProfileError(
                f"act budgets total {budget_total} words, outside the episode's "
                f"{word_min}-{word_max}-word range."
            )
        return cls(
            word_count_min=word_min,
            word_count_max=word_max,
            act_budgets=MappingProxyType(budgets),
            wpm=wpm,
            word_count_tolerance=word_count_tolerance,
            act_budget_tolerance=act_budget_tolerance,
            target_duration_minutes=float(target_duration_minutes),
            target_word_count=target_words,
            source=source,
        )

    @property
    def is_brief_aware(self) -> bool:
        return self.target_duration_minutes is not None


def _require_positive_wpm(wpm: int) -> None:
    if isinstance(wpm, bool) or not isinstance(wpm, int) or wpm <= 0:
        raise ValidationProfileError("WPM must be a positive integer.")


def _require_tolerance(value: float, field: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value < 1
    ):
        raise ValidationProfileError(
            f"{field} must be a finite number greater than or equal to 0 "
            f"and less than 1."
        )


def _validated_act_word_budgets(value: object) -> dict[int, int]:
    if not isinstance(value, Mapping):
        raise ValidationProfileError("act_word_budgets must be a JSON object.")

    budgets: dict[int, int] = {}
    for raw_number, raw_budget in value.items():
        if isinstance(raw_number, bool):
            raise ValidationProfileError(
                "act_word_budgets keys must be the act numbers 1 through 5."
            )
        try:
            number = int(raw_number)
        except (TypeError, ValueError):
            raise ValidationProfileError(
                "act_word_budgets keys must be the act numbers 1 through 5."
            ) from None
        if str(raw_number).strip() not in {str(number), f"{number}.0"}:
            raise ValidationProfileError(
                "act_word_budgets keys must be the act numbers 1 through 5."
            )
        if number in budgets:
            raise ValidationProfileError(
                f"act_word_budgets defines Act {number} more than once."
            )
        if (
            isinstance(raw_budget, bool)
            or not isinstance(raw_budget, int)
            or raw_budget <= 0
        ):
            raise ValidationProfileError(
                f"act_word_budgets Act {number} must be a positive integer."
            )
        budgets[number] = raw_budget

    expected = set(ACT_BUDGETS)
    missing = sorted(expected - set(budgets))
    extra = sorted(set(budgets) - expected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing acts {missing}")
        if extra:
            details.append(f"unexpected acts {extra}")
        raise ValidationProfileError(
            "act_word_budgets must define Acts 1-5 exactly ("
            + "; ".join(details)
            + ")."
        )
    return budgets


def _act_budgets_from_durations(value: object, wpm: int) -> dict[int, int]:
    if not isinstance(value, Mapping):
        raise ValidationProfileError("act_duration_seconds must be a JSON object.")

    seconds_by_act: dict[int, float] = {}
    for raw_number, raw_seconds in value.items():
        try:
            number = int(raw_number)
        except (TypeError, ValueError):
            raise ValidationProfileError(
                "act_duration_seconds keys must be the act numbers 1 through 5."
            ) from None
        if str(raw_number).strip() not in {str(number), f"{number}.0"}:
            raise ValidationProfileError(
                "act_duration_seconds keys must be the act numbers 1 through 5."
            )
        if number in seconds_by_act:
            raise ValidationProfileError(
                f"act_duration_seconds defines Act {number} more than once."
            )
        if (
            isinstance(raw_seconds, bool)
            or not isinstance(raw_seconds, (int, float))
            or not math.isfinite(raw_seconds)
            or raw_seconds <= 0
        ):
            raise ValidationProfileError(
                f"act_duration_seconds Act {number} must be a positive finite number."
            )
        seconds_by_act[number] = float(raw_seconds)

    expected = set(ACT_BUDGETS)
    missing = sorted(expected - set(seconds_by_act))
    extra = sorted(set(seconds_by_act) - expected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing acts {missing}")
        if extra:
            details.append(f"unexpected acts {extra}")
        raise ValidationProfileError(
            "act_duration_seconds must define Acts 1-5 exactly ("
            + "; ".join(details)
            + ")."
        )

    return {
        number: max(1, int(round(seconds * wpm / 60)))
        for number, seconds in seconds_by_act.items()
    }


def _scale_act_budgets(target_words: int) -> dict[int, int]:
    """Scale the corpus act shape while preserving an exact total.

    Largest-remainder allocation avoids losing or inventing words through five
    independent ``round`` calls. Ties stay in act order for deterministic
    output on every platform.
    """

    base_total = sum(ACT_BUDGETS.values())
    raw = {
        number: target_words * budget / base_total
        for number, budget in ACT_BUDGETS.items()
    }
    scaled = {number: math.floor(value) for number, value in raw.items()}
    remainder = target_words - sum(scaled.values())
    order = sorted(raw, key=lambda number: (-(raw[number] - scaled[number]), number))
    for number in order[:remainder]:
        scaled[number] += 1
    return scaled


def validation_profile_for_script(
    script_path: Path,
    wpm: int | None = None,
    *,
    fallback_wpm: int = 177,
) -> ValidationProfile:
    """Discover ``brief.json`` beside a project's ``script`` directory.

    ``projects/<slug>/script/<edition>.md`` maps to
    ``projects/<slug>/brief.json``. A missing brief, or a well-formed brief
    without ``target_duration_minutes``, deliberately falls back to the legacy
    long-format gates. A present but malformed brief fails closed: silently
    selecting unrelated duration gates could approve the wrong script or spend
    money narrating it.
    """

    if wpm is not None:
        _require_positive_wpm(wpm)
    _require_positive_wpm(fallback_wpm)

    path = Path(script_path)
    brief_path = path.parent.parent / "brief.json"
    if not brief_path.exists():
        return ValidationProfile.default(wpm=wpm or fallback_wpm)

    try:
        brief = read_json(brief_path)
    except (json.JSONDecodeError, OSError) as exc:
        raise ValidationProfileError(
            f"Cannot load validation target from {brief_path}: {exc}"
        ) from exc

    if not isinstance(brief, dict):
        raise ValidationProfileError(
            f"{brief_path} must contain a JSON object; got {type(brief).__name__}."
        )

    validation = brief.get("validation", {})
    if validation is None:
        validation = {}
    if not isinstance(validation, dict):
        raise ValidationProfileError(
            f"{brief_path}: validation must be a JSON object."
        )

    brief_wpm = brief.get("target_wpm")
    if brief_wpm is not None:
        try:
            _require_positive_wpm(brief_wpm)
        except ValidationProfileError as exc:
            raise ValidationProfileError(f"{brief_path}: target_wpm: {exc}") from exc
    selected_wpm = wpm if wpm is not None else (brief_wpm or fallback_wpm)

    target_present = (
        "target_duration_minutes" in brief
        and brief["target_duration_minutes"] is not None
    )
    duration_specific_fields = {
        "word_count_tolerance",
        "act_budget_tolerance",
        "act_word_budgets",
        "act_duration_seconds",
    }
    if not target_present:
        configured = sorted(duration_specific_fields.intersection(validation))
        if configured:
            raise ValidationProfileError(
                f"{brief_path}: validation fields {configured} require "
                f"target_duration_minutes."
            )
        return ValidationProfile.default(wpm=selected_wpm)

    word_tolerance = validation.get("word_count_tolerance", ACT_TOLERANCE)
    act_tolerance = validation.get("act_budget_tolerance", ACT_TOLERANCE)
    has_act_word_budgets = "act_word_budgets" in validation
    has_act_durations = "act_duration_seconds" in validation
    if has_act_word_budgets and has_act_durations:
        raise ValidationProfileError(
            f"{brief_path}: validation may define act_word_budgets or "
            f"act_duration_seconds, not both."
        )
    act_word_budgets = None
    if has_act_word_budgets:
        try:
            act_word_budgets = _validated_act_word_budgets(
                validation["act_word_budgets"]
            )
        except ValidationProfileError as exc:
            raise ValidationProfileError(f"{brief_path}: {exc}") from exc
    elif has_act_durations:
        try:
            act_word_budgets = _act_budgets_from_durations(
                validation["act_duration_seconds"], selected_wpm
            )
        except ValidationProfileError as exc:
            raise ValidationProfileError(f"{brief_path}: {exc}") from exc

    try:
        return ValidationProfile.for_duration(
            brief["target_duration_minutes"],
            wpm=selected_wpm,
            word_count_tolerance=word_tolerance,
            act_budget_tolerance=act_tolerance,
            act_budgets=act_word_budgets,
            source=str(brief_path),
        )
    except ValidationProfileError as exc:
        raise ValidationProfileError(f"{brief_path}: {exc}") from exc


@dataclass(frozen=True)
class Finding:
    gate: str
    severity: str
    message: str
    line: int | None = None


def check_word_count(
    parsed: ParsedScript, profile: ValidationProfile | None = None
) -> list[Finding]:
    selected = profile or ValidationProfile.default()
    total = word_count(parsed)
    if selected.word_count_min <= total <= selected.word_count_max:
        return []

    if selected.is_brief_aware:
        requirement = (
            f"the {selected.target_duration_minutes:g}-minute brief at "
            f"{selected.wpm} WPM requires {selected.word_count_min}-"
            f"{selected.word_count_max} words "
            f"(target {selected.target_word_count})"
        )
    else:
        requirement = (
            f"the format requires {selected.word_count_min}-"
            f"{selected.word_count_max}"
        )
    return [
        Finding(
            gate="word_count",
            severity="error",
            message=f"Script is {total} words; {requirement}.",
        )
    ]


def act_spans(parsed: ParsedScript) -> dict[int, tuple[int, int]]:
    """Map act number to (start_word_index, end_word_index).

    The final act runs to the end of the narration.
    """
    acts = markers_of(parsed, "ACT")
    total = word_count(parsed)
    spans: dict[int, tuple[int, int]] = {}
    for i, marker in enumerate(acts):
        try:
            number = int(marker.arg.split()[0])
        except (ValueError, IndexError):
            continue
        end = acts[i + 1].word_index if i + 1 < len(acts) else total
        spans[number] = (marker.word_index, end)
    return spans


def check_act_budgets(
    parsed: ParsedScript, profile: ValidationProfile | None = None
) -> list[Finding]:
    selected = profile or ValidationProfile.default()
    findings: list[Finding] = []
    acts = markers_of(parsed, "ACT")

    numbers: list[int] = []
    for marker in acts:
        try:
            numbers.append(int(marker.arg.split()[0]))
        except (ValueError, IndexError):
            findings.append(
                Finding(
                    gate="act_budget",
                    severity="error",
                    message=f"ACT marker {marker.raw!r} does not start with an act number.",
                    line=marker.line,
                )
            )

    if numbers != sorted(numbers):
        findings.append(
            Finding(
                gate="act_budget",
                severity="error",
                message=f"ACT markers must appear in ascending order; saw {numbers}.",
            )
        )

    spans = act_spans(parsed)
    for number, budget in selected.act_budgets.items():
        if number not in spans:
            findings.append(
                Finding(
                    gate="act_budget",
                    severity="error",
                    message=f"Act {number} is missing; all five acts are required.",
                )
            )
            continue

        start, end = spans[number]
        actual = end - start
        drift = (actual - budget) / budget
        if abs(drift) > selected.act_budget_tolerance:
            findings.append(
                Finding(
                    gate="act_budget",
                    severity="error",
                    message=(
                        f"Act {number} is {actual} words against a budget of {budget} "
                        f"({drift:+.0%}); tolerance is "
                        f"+-{selected.act_budget_tolerance:.0%}."
                    ),
                )
            )

    return findings


def check_rehook_spacing(parsed: ParsedScript, wpm: int = 177) -> list[Finding]:
    """Act III re-hooks must land every 3-5 minutes of narration."""
    spans = act_spans(parsed)
    if 3 not in spans:
        return []

    start, end = spans[3]
    min_words = REHOOK_MIN_MINUTES * wpm
    max_words = REHOOK_MAX_MINUTES * wpm

    # NOTE: deviates from the prescribed `start <= m.word_index <= end` range
    # filter. That filter is ambiguous when a REHOOK sits adjacent to an ACT
    # marker with zero narration words between them: a REHOOK immediately
    # before [ACT:3 ...] and a REHOOK immediately after it both get
    # word_index == start, so a pure word_index range cannot tell them apart
    # (confirmed empirically: both produce identical word_index values).
    # Attribution by source order (which act marker most recently preceded
    # this REHOOK) is unambiguous, so we use that instead.
    rehooks = []
    current_act: int | None = None
    for marker in parsed.markers:
        if marker.kind == "ACT":
            try:
                current_act = int(marker.arg.split()[0])
            except (ValueError, IndexError):
                pass
        elif marker.kind == "REHOOK" and current_act == 3:
            rehooks.append(marker)
    if not rehooks:
        return [
            Finding(
                gate="rehook_spacing",
                severity="error",
                message=(
                    f"Act III spans {end - start} words but has no [REHOOK] markers; "
                    f"one is required every {REHOOK_MIN_MINUTES}-{REHOOK_MAX_MINUTES} minutes."
                ),
            )
        ]

    findings: list[Finding] = []
    previous = start
    for marker in rehooks:
        gap = marker.word_index - previous
        if gap < min_words:
            findings.append(
                Finding(
                    gate="rehook_spacing",
                    severity="error",
                    message=(
                        f"[REHOOK] arrives too soon: {gap} words "
                        f"({gap / wpm:.1f} min) after the previous beat; minimum is "
                        f"{min_words} words ({REHOOK_MIN_MINUTES} min)."
                    ),
                    line=marker.line,
                )
            )
        elif gap > max_words:
            findings.append(
                Finding(
                    gate="rehook_spacing",
                    severity="error",
                    message=(
                        f"[REHOOK] arrives too late: {gap} words "
                        f"({gap / wpm:.1f} min) after the previous beat; maximum is "
                        f"{max_words} words ({REHOOK_MAX_MINUTES} min)."
                    ),
                    line=marker.line,
                )
            )
        previous = marker.word_index

    return findings


CASUAL_TOKENS = (
    "tum",
    "tumhara",
    "tumhari",
    "tumhare",
    "tumhe",
    "tumne",
    "yaar",
    "bhai",
    "arre",
    "socho",
    "dekho",
    "suno",
    "samjho",
    "karoge",
    "dekhoge",
    "samjhoge",
)

CASUAL_PHRASES = (
    "karte ho",
    "jaate ho",
    "dete ho",
    "lete ho",
    "chahte ho",
    "ho jaate ho",
    "soch lo",
    "sun lo",
    "samajh lo",
    "kar lo",
    "yaad rakho",
    "baith jao",
    "imagine kar lo",
    "socho zara",
)

_REGISTER_RE = re.compile(
    r"\b(" + "|".join([*CASUAL_PHRASES, *CASUAL_TOKENS]) + r")\b",
    re.IGNORECASE,
)


def check_register(parsed: ParsedScript) -> list[Finding]:
    """Flag any surviving casual-register token.

    Phrases are listed before single tokens in the alternation so that
    'karte ho' matches as a phrase rather than leaving a bare 'ho' behind.
    """
    findings: list[Finding] = []
    for match in _REGISTER_RE.finditer(parsed.text):
        line = parsed.text.count("\n", 0, match.start()) + 1
        findings.append(
            Finding(
                gate="register",
                severity="error",
                message=(
                    f"Casual-register {match.group(0)!r} survived stage 3; "
                    f"the narration voice is aap-register throughout."
                ),
                line=line,
            )
        )
    return findings


SILENCE_MIN_SECONDS = 0.5
SILENCE_MAX_SECONDS = 2.0

_SILENCE_RE = re.compile(r"^(\d+(?:\.\d+)?)s$")


def load_sfx_names(path: Path) -> set[str]:
    """Allowed [SFX:] names, read from the style pack registry."""
    return set(read_json(path).keys())


def check_marker_args(parsed: ParsedScript, sfx_names: set[str]) -> list[Finding]:
    findings: list[Finding] = []

    def flag(marker, message: str) -> None:
        findings.append(
            Finding(gate="marker_args", severity="error", message=message, line=marker.line)
        )

    for marker in parsed.markers:
        if marker.kind == "SILENCE":
            match = _SILENCE_RE.match(marker.arg)
            if not match:
                flag(marker, f"{marker.raw!r} must give seconds with an 's' suffix, e.g. [SILENCE:1.5s].")
                continue
            value = float(match.group(1))
            if value < SILENCE_MIN_SECONDS:
                flag(marker, f"{marker.raw!r} is below the {SILENCE_MIN_SECONDS}s minimum.")
            elif value > SILENCE_MAX_SECONDS:
                flag(marker, f"{marker.raw!r} is above the {SILENCE_MAX_SECONDS}s maximum.")

        elif marker.kind == "SFX":
            if marker.arg not in sfx_names:
                flag(marker, f"Unknown SFX name {marker.arg!r}; add it to style/sfx.json first.")

        elif marker.kind == "SHOT":
            kind = marker.arg.split()[0] if marker.arg.split() else ""
            if kind not in SHOT_KINDS:
                flag(marker, f"Unknown shot kind {kind!r}; expected one of {', '.join(SHOT_KINDS)}.")

        elif marker.kind == "ACT":
            head = marker.arg.split()[0] if marker.arg.split() else ""
            if not head.isdigit() or not 1 <= int(head) <= 5:
                flag(marker, f"{marker.raw!r} must start with an act number in 1-5.")

        elif marker.kind == "CHAPTER":
            head = marker.arg.split()[0] if marker.arg.split() else ""
            if not head.isdigit():
                flag(marker, f"{marker.raw!r} must start with a chapter number.")

    return findings


CONFIDENCE_TAGS = ("documented", "reported", "alleged", "speculation")
SOURCED_TAGS = ("documented", "reported")
ATTRIBUTED_TAGS = ("alleged", "speculation")

ATTRIBUTION_PHRASES = (
    "ke mutabik",
    "ke anusaar",
    "dava kiya",
    "ke hawale se",
    "report ke",
    "police ke",
)

_ATTRIBUTION_RE = re.compile(
    "|".join(re.escape(p) for p in ATTRIBUTION_PHRASES), re.IGNORECASE
)


def load_claims(path: Path) -> list[dict]:
    """Read the claims ledger. A missing ledger is an empty ledger, not an error."""
    if not path.exists():
        return []
    return read_json(path)


def check_claims(parsed: ParsedScript, claims: list[dict]) -> list[Finding]:
    """Enforce claims-ledger discipline for assertions about real people.

    This gate is mechanical, not semantic. It cannot read the narration and decide
    which sentences are assertions of wrongdoing, and it does not try to. It checks
    three things it CAN check: that the ledger is internally well formed, that
    claims which require sources have them, and that the script contains at least as
    many attribution phrases as there are claims requiring attribution.

    That last check is a deliberately weak countable proxy. It cannot prove a
    specific claim was attributed in the narration; it catches the realistic failure
    mode, which is a script that states allegations flatly with no hedging anywhere.
    Reviewing which claim each attribution belongs to remains a human job.
    """
    findings: list[Finding] = []

    def flag(message: str) -> None:
        findings.append(Finding(gate="claims", severity="error", message=message))

    if not isinstance(claims, list):
        flag(
            f"claims.json must contain a JSON array of records; "
            f"got {type(claims).__name__}."
        )
        return findings

    seen_ids: set[str] = set()
    needs_attribution = 0

    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            flag(f"Claim at record {index} is not a JSON object; got {type(claim).__name__}.")
            continue

        claim_id = str(claim.get("claim_id", "")).strip()
        label = claim_id or f"record {index}"

        if not claim_id:
            flag(f"Claim at record {index} has no claim_id.")
        elif claim_id in seen_ids:
            flag(f"Duplicate claim_id {claim_id!r}.")
        else:
            seen_ids.add(claim_id)

        if not str(claim.get("text", "")).strip():
            flag(f"Claim {label} has no text.")

        confidence = str(claim.get("confidence", "")).strip()
        if confidence not in CONFIDENCE_TAGS:
            flag(
                f"Claim {label} has confidence {confidence!r}; "
                f"expected one of {', '.join(CONFIDENCE_TAGS)}."
            )
            continue

        sources = claim.get("sources") or []
        if confidence in SOURCED_TAGS and not sources:
            flag(f"Claim {label} is tagged {confidence!r} but has no source.")

        if confidence == "alleged" and not str(claim.get("attributed_to", "")).strip():
            flag(
                f"Claim {label} is tagged 'alleged' but has no attributed_to; "
                f"an allegation must name who is making it."
            )

        if confidence in ATTRIBUTED_TAGS:
            needs_attribution += 1

    if needs_attribution:
        found = len(_ATTRIBUTION_RE.findall(parsed.text))
        if found < needs_attribution:
            flag(
                f"The ledger has {needs_attribution} claim(s) requiring attribution "
                f"but the script contains only {found} attribution phrase(s). "
                f"Unattributed allegations must not be voiced as fact."
            )

    return findings


def validate_all(
    parsed: ParsedScript,
    sfx_names: set[str],
    wpm: int = 177,
    claims: list[dict] | None = None,
    profile: ValidationProfile | None = None,
) -> list[Finding]:
    """Run every gate and return findings in gate order."""
    selected = profile or ValidationProfile.default(wpm=wpm)
    return [
        *check_word_count(parsed, profile=selected),
        *check_act_budgets(parsed, profile=selected),
        *check_rehook_spacing(parsed, wpm=selected.wpm),
        *check_register(parsed),
        *check_marker_args(parsed, sfx_names),
        *check_claims(parsed, claims or []),
    ]


TRANSLITERATED_ENGLISH: dict[str, str] = {
    "वीडियो": "video",
    "चैनल": "channel",
    "अकाउंट": "account",
    "इंटरनेट": "internet",
    "कंप्यूटर": "computer",
    "कमेंट": "comment",
    "सब्सक्राइबर": "subscriber",
    "डिस्क्रिप्शन": "description",
    "यूट्यूब": "YouTube",
    "वेबसाइट": "website",
    "ऑनलाइन": "online",
    "मोबाइल": "mobile",
    "कैमरा": "camera",
    "सर्वर": "server",
    "फ़ाइल": "file",
    "फोल्डर": "folder",
    "डाउनलोड": "download",
    "अपलोड": "upload",
    "स्क्रीन": "screen",
    "लिंक": "link",
    "पोस्ट": "post",
    "मैसेज": "message",
    "प्रोफ़ाइल": "profile",
    "बटन": "button",
    "मिनट": "minute",
    "सेकंड": "second",
    "ईमेल": "email",
    "पासवर्ड": "password",
    "डेटा": "data",
    "सिस्टम": "system",
    "ऐप": "app",
    "गेम": "game",
    "फ़ोन": "phone",
    "लैपटॉप": "laptop",
    "ब्राउज़र": "browser",
    "सॉफ़्टवेयर": "software",
}

# Codepoints that count as "inside a Devanagari word" for boundary purposes:
# the consonant/vowel letters (U+0900-U+0963) and the digit/extra-letter block
# (U+0966-U+097F), but not the danda / double-danda punctuation (U+0964-U+0965).
#
# Plain `\b` is unreliable here: Python's `\w` is defined via `str.isalnum()`,
# and dependent vowel signs (matras, e.g. the 'ो' in 'वीडियो') are Unicode
# category Mc/Mn, which is NOT alnum. So `\b` sees a "word boundary" between a
# matra and the consonant that follows it inside a single word, even though a
# reader sees one continuous akshara sequence. Verified empirically: on
# 'वीडियोग्राफर' ("videographer"), `re.compile(r'\bवीडियो\b')` matches the
# 'वीडियो' prefix, because the boundary between the trailing 'ो' matra
# (non-\w) and the following 'ग' consonant (\w) looks like a `\b` transition
# even though it sits mid-word. A lookaround against the full Devanagari
# letter/mark range (rather than `\w`) does not have this problem, and was
# checked against the same 'वीडियोग्राफर' case plus adjacent punctuation,
# parentheses, and Latin text: it does not match inside the longer word, and
# does match a standalone occurrence bounded by punctuation or Latin text.
_DEVANAGARI_WORD_CHAR = r"[ऀ-ॣ०-ॿ]"


def _devanagari_boundary_pattern(term: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<!{_DEVANAGARI_WORD_CHAR}){re.escape(term)}(?!{_DEVANAGARI_WORD_CHAR})"
    )


def check_transliterated_english(devanagari: ParsedScript) -> list[Finding]:
    """Flag English words written in Devanagari in the TTS edition.

    Devanagari script tells the TTS model to apply Hindi phonetics; an English
    word spelled in Devanagari (e.g. 'अकाउंट' for 'account') gets Hindi
    phonetics forced onto it and comes out mangled. English words and terms
    belong in Latin script instead (see `darkdoc-lexicon.md`).

    This is a heuristic check against a finite, hand-maintained blacklist of
    commonly-transliterated English terms -- it cannot catch every possible
    mistransliteration, only the ones it knows about -- so it reports
    `severity="warning"`, not `"error"`.

    One finding is emitted per distinct term found, not per occurrence: a
    script that says 'वीडियो' twelve times has one problem, not twelve.
    """
    findings: list[Finding] = []
    for term, latin in TRANSLITERATED_ENGLISH.items():
        if _devanagari_boundary_pattern(term).search(devanagari.text):
            findings.append(
                Finding(
                    gate="transliterated_english",
                    severity="warning",
                    message=(
                        f"{term!r} is an English word written in Devanagari; use "
                        f"{latin!r} instead. Devanagari script makes the TTS model "
                        f"apply Hindi phonetics to what is really an English word."
                    ),
                )
            )
    return findings


_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")

# A romanized script may legitimately quote a Devanagari word; a Devanagari script
# legitimately keeps English loanwords in Latin. So the script checks are about the
# dominant script, not purity. This threshold is what "dominant" means.
SCRIPT_DOMINANCE_MIN = 0.20


def _devanagari_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return len(_DEVANAGARI_RE.findall("".join(letters))) / len(letters)


def check_transliteration(
    romanized: ParsedScript, devanagari: ParsedScript | None
) -> list[Finding]:
    """Prove the Devanagari TTS edition faithfully mirrors the canonical script.

    The romanized script is canonical because every other gate depends on it: the
    register gate matches romanized tokens like 'tum' and 'karte ho', which match
    nothing in Devanagari, so validating a Devanagari script would silently pass
    everything rather than fail loudly.

    Parity is also load-bearing downstream. The edit stage anchors video cuts to
    word_index taken from the romanized script, while TTS character alignment comes
    from the Devanagari one. Those only correspond if word n is the same word in
    both files, which is exactly what this gate establishes.
    """
    findings: list[Finding] = []

    def flag(message: str) -> None:
        findings.append(
            Finding(gate="transliteration", severity="error", message=message)
        )

    if _devanagari_ratio(romanized.text) > SCRIPT_DOMINANCE_MIN:
        flag(
            "The canonical script must stay romanized; it reads as Devanagari. "
            "The register gate matches romanized tokens and would silently pass "
            "a Devanagari script instead of failing."
        )

    if devanagari is None:
        flag(
            "No 05-devanagari.md beside this script. The voice clone is a Hindi "
            "voice and mispronounces romanized Latin input; generate the Devanagari "
            "edition before narrating."
        )
        return findings

    if _devanagari_ratio(devanagari.text) < SCRIPT_DOMINANCE_MIN:
        flag(
            "05-devanagari.md contains no Devanagari script; it looks like an "
            "untransliterated copy of the canonical script."
        )

    roman_words = word_count(romanized)
    deva_words = word_count(devanagari)
    if roman_words != deva_words:
        flag(
            f"Word count differs between editions: canonical has {roman_words}, "
            f"Devanagari has {deva_words}. Transliteration must preserve every word "
            f"so marker word_index means the same thing in both."
        )

    roman_markers = romanized.markers
    deva_markers = devanagari.markers
    if len(roman_markers) != len(deva_markers):
        flag(
            f"Marker count differs between editions: canonical has "
            f"{len(roman_markers)}, Devanagari has {len(deva_markers)}."
        )
        return findings

    for position, (left, right) in enumerate(zip(roman_markers, deva_markers)):
        if left.kind != right.kind:
            flag(
                f"Marker {position} differs between editions: canonical has "
                f"{left.kind}, Devanagari has {right.kind}."
            )
        elif left.arg != right.arg:
            flag(
                f"Marker {position} ({left.kind}) argument differs between editions: "
                f"canonical has {left.arg!r}, Devanagari has {right.arg!r}."
            )

    return findings


def format_report(findings: list[Finding]) -> str:
    if not findings:
        return "PASSED - all gates clean."

    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity != "error"]

    if errors and warnings:
        headline = f"FAILED - {len(errors)} error(s), {len(warnings)} warning(s):"
    elif errors:
        # No warnings present: keep the plain "N finding(s)" phrasing rather than
        # padding the headline with a "0 warning(s)" count nobody needs.
        headline = f"FAILED - {len(errors)} finding(s):"
    else:
        headline = f"PASSED WITH WARNINGS - {len(warnings)} warning(s):"

    lines = [headline, ""]
    for finding in findings:
        where = f" (line {finding.line})" if finding.line is not None else ""
        lines.append(f"  [{finding.gate}] [{finding.severity.upper()}]{where} {finding.message}")
    return "\n".join(lines)

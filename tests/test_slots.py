from rabbithole.slots import MAX_SLOT_HOLD_SECONDS, Slot, build_slots, check_slots


def _document(duration, markers):
    return {
        "duration_seconds": duration,
        "word_count": 0,
        "words": [],
        "markers": markers,
    }


def _shot(arg, seconds, word_index=0, line=1):
    return {"kind": "SHOT", "arg": arg, "word_index": word_index, "line": line, "seconds": seconds}


# --- build_slots -----------------------------------------------------------


def test_one_shot_marker_at_zero_yields_one_slot_spanning_full_duration():
    document = _document(10.0, [_shot("screenshot push 105-115", 0.0)])

    slots = build_slots(document)

    assert len(slots) == 1
    assert slots[0].start == 0.0
    assert slots[0].end == 10.0


def test_two_shot_markers_yield_two_contiguous_slots_second_ending_at_duration():
    document = _document(
        10.0,
        [
            _shot("screenshot push 105-115", 0.0, word_index=0),
            _shot("archival newsroom", 4.0, word_index=20),
        ],
    )

    slots = build_slots(document)

    assert len(slots) == 2
    assert slots[0].start == 0.0
    assert slots[0].end == 4.0
    assert slots[1].start == 4.0
    assert slots[1].end == 10.0


def test_slot_ids_are_zero_padded():
    document = _document(
        10.0,
        [_shot("screenshot a", 0.0), _shot("archival b", 4.0)],
    )

    slots = build_slots(document)

    assert slots[0].slot_id == "s001"
    assert slots[1].slot_id == "s002"


def test_kind_and_detail_split_from_marker_arg():
    document = _document(10.0, [_shot("screenshot push 105-115", 0.0)])

    slots = build_slots(document)

    assert slots[0].kind == "screenshot"
    assert slots[0].detail == "push 105-115"


def test_marker_arg_with_only_kind_gives_empty_detail():
    document = _document(10.0, [_shot("plate", 0.0)])

    slots = build_slots(document)

    assert slots[0].kind == "plate"
    assert slots[0].detail == ""


def test_shot_marker_later_than_zero_produces_implicit_leading_plate_slot():
    document = _document(10.0, [_shot("screenshot push 105-115", 3.0, word_index=15)])

    slots = build_slots(document)

    assert len(slots) == 2
    implicit = slots[0]
    assert implicit.kind == "plate"
    assert implicit.detail == "implicit opening"
    assert implicit.start == 0.0
    assert implicit.end == 3.0
    assert implicit.marker_word_index == 0
    assert implicit.slot_id == "s001"
    assert slots[1].slot_id == "s002"


def test_implicit_slot_not_created_when_first_shot_marker_at_zero():
    document = _document(10.0, [_shot("screenshot push 105-115", 0.0)])

    slots = build_slots(document)

    assert len(slots) == 1
    assert slots[0].kind == "screenshot"


def test_queries_contains_kind_and_detail_joined():
    document = _document(10.0, [_shot("screenshot push 105-115", 0.0)])

    slots = build_slots(document)

    assert slots[0].queries == ("screenshot push 105-115",)


def test_hold_seconds_is_end_minus_start():
    document = _document(
        10.0,
        [_shot("screenshot a", 0.0), _shot("archival b", 4.0)],
    )

    slots = build_slots(document)

    assert slots[0].hold_seconds == 4.0
    assert slots[1].hold_seconds == 6.0


def test_document_with_no_shot_markers_yields_empty_list():
    document = _document(10.0, [])

    assert build_slots(document) == []


# --- check_slots -------------------------------------------------------


def test_check_slots_flags_empty_plan_against_nonzero_duration():
    document = _document(10.0, [])

    findings = check_slots([], document)

    assert any(f.gate == "slots" for f in findings)


def test_check_slots_passes_on_well_formed_contiguous_plan():
    document = _document(
        10.0,
        [_shot("screenshot a", 0.0), _shot("archival b", 4.0)],
    )
    slots = build_slots(document)

    assert check_slots(slots, document) == []


def test_check_slots_flags_unknown_shot_kind():
    document = _document(10.0, [])
    slot = Slot(
        slot_id="s001",
        kind="hologram",
        detail="spin",
        start=0.0,
        end=10.0,
        queries=("hologram spin",),
        marker_word_index=0,
    )

    findings = check_slots([slot], document)

    assert any(f.gate == "slots" and "hologram" in f.message for f in findings)


def test_check_slots_flags_zero_length_slot():
    document = _document(2.0, [])
    slot = Slot(
        slot_id="s001",
        kind="screenshot",
        detail="x",
        start=2.0,
        end=2.0,
        queries=("screenshot x",),
        marker_word_index=0,
    )

    findings = check_slots([slot], document)

    assert any(f.gate == "slots" and "s001" in f.message for f in findings)


def test_check_slots_flags_gap_between_slots():
    document = _document(10.0, [])
    slots = [
        Slot(
            slot_id="s001",
            kind="screenshot",
            detail="a",
            start=0.0,
            end=4.0,
            queries=("screenshot a",),
            marker_word_index=0,
        ),
        Slot(
            slot_id="s002",
            kind="archival",
            detail="b",
            start=5.0,
            end=10.0,
            queries=("archival b",),
            marker_word_index=10,
        ),
    ]

    findings = check_slots(slots, document)

    assert any(f.gate == "slots" and "s001" in f.message and "s002" in f.message for f in findings)


def test_check_slots_flags_last_slot_short_of_duration():
    document = _document(10.0, [])
    slots = [
        Slot(
            slot_id="s001",
            kind="screenshot",
            detail="a",
            start=0.0,
            end=8.0,
            queries=("screenshot a",),
            marker_word_index=0,
        ),
    ]

    findings = check_slots(slots, document)

    assert any(f.gate == "slots" and "s001" in f.message for f in findings)


# --- check_slots: shot density ----------------------------------------------


def test_check_slots_does_not_warn_at_exactly_the_max_slot_hold():
    document = _document(MAX_SLOT_HOLD_SECONDS, [])
    slot = Slot(
        slot_id="s001",
        kind="screenshot",
        detail="a",
        start=0.0,
        end=MAX_SLOT_HOLD_SECONDS,
        queries=("screenshot a",),
        marker_word_index=0,
    )

    findings = check_slots([slot], document)

    assert findings == []


def test_check_slots_warns_when_slot_hold_exceeds_max():
    document = _document(MAX_SLOT_HOLD_SECONDS + 1.0, [])
    slot = Slot(
        slot_id="s001",
        kind="screenshot",
        detail="a",
        start=0.0,
        end=MAX_SLOT_HOLD_SECONDS + 1.0,
        queries=("screenshot a",),
        marker_word_index=0,
    )

    findings = check_slots([slot], document)

    assert len(findings) == 1
    assert findings[0].gate == "slots"
    assert findings[0].severity == "warning"


def test_check_slots_overlong_warning_names_the_slot_id():
    document = _document(30.0, [])
    slot = Slot(
        slot_id="s007",
        kind="archival",
        detail="b",
        start=0.0,
        end=30.0,
        queries=("archival b",),
        marker_word_index=0,
    )

    findings = check_slots([slot], document)

    assert any(f.severity == "warning" and "s007" in f.message for f in findings)


def test_check_slots_plan_of_short_slots_has_no_findings():
    document = _document(
        10.0,
        [_shot("screenshot a", 0.0), _shot("archival b", 4.0)],
    )
    slots = build_slots(document)

    assert check_slots(slots, document) == []


def test_check_slots_overlong_warning_does_not_suppress_gap_error():
    document = _document(30.0, [])
    slots = [
        Slot(
            slot_id="s001",
            kind="screenshot",
            detail="a",
            start=0.0,
            end=4.0,
            queries=("screenshot a",),
            marker_word_index=0,
        ),
        Slot(
            slot_id="s002",
            kind="archival",
            detail="b",
            start=5.0,
            end=8.0,
            queries=("archival b",),
            marker_word_index=10,
        ),
        Slot(
            slot_id="s003",
            kind="plate",
            detail="c",
            start=8.0,
            end=30.0,
            queries=("plate c",),
            marker_word_index=20,
        ),
    ]

    findings = check_slots(slots, document)

    gap_findings = [f for f in findings if f.severity == "error" and "s001" in f.message and "s002" in f.message]
    warning_findings = [f for f in findings if f.severity == "warning" and "s003" in f.message]
    assert len(gap_findings) == 1
    assert len(warning_findings) == 1
    assert len(findings) == 2

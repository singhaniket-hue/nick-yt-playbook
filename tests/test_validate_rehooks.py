from rabbithole.markers import parse
from rabbithole.validate import check_rehook_spacing


def _act3(segments: list[int]) -> str:
    """Build an Act III with [REHOOK] markers separated by the given word counts."""
    parts = ["[ACT:3 Rabbit Hole]"]
    for i, count in enumerate(segments):
        parts.append(" ".join(["shabd"] * count))
        if i < len(segments) - 1:
            parts.append("[REHOOK]")
    return " ".join(parts)


def test_rehooks_pass_at_four_minute_spacing():
    # 708 words at 177 WPM is 4 minutes, inside the 3-5 minute band.
    parsed = parse(_act3([708, 708, 708, 708, 708]))

    assert check_rehook_spacing(parsed) == []


def test_rehooks_flag_gap_that_is_too_short():
    # 300 words is under 2 minutes.
    parsed = parse(_act3([708, 300, 708]))

    findings = check_rehook_spacing(parsed)

    assert len(findings) == 1
    assert findings[0].gate == "rehook_spacing"
    assert "too soon" in findings[0].message


def test_rehooks_flag_gap_that_is_too_long():
    # 1400 words is nearly 8 minutes.
    parsed = parse(_act3([708, 1400, 708]))

    findings = check_rehook_spacing(parsed)

    assert len(findings) == 1
    assert "too late" in findings[0].message


def test_rehooks_outside_act_three_are_ignored():
    source = "[ACT:2 Origin] " + " ".join(["shabd"] * 100) + " [REHOOK] " + _act3([708, 708])
    parsed = parse(source)

    assert check_rehook_spacing(parsed) == []


def test_missing_rehooks_in_act_three_is_flagged():
    parsed = parse("[ACT:3 Rabbit Hole] " + " ".join(["shabd"] * 3360))

    findings = check_rehook_spacing(parsed)

    assert any("no [REHOOK]" in f.message for f in findings)

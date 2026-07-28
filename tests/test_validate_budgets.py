from rabbithole.markers import parse
from rabbithole.validate import check_act_budgets, check_word_count


def _script_with_acts(counts: dict[int, int]) -> str:
    names = {1: "Cold Open", 2: "Origin", 3: "Rabbit Hole", 4: "Climax", 5: "Outro"}
    parts = []
    for act in sorted(counts):
        parts.append(f"[ACT:{act} {names[act]}]")
        parts.append(" ".join(["shabd"] * counts[act]))
    return "\n".join(parts)


def test_word_count_passes_inside_range():
    parsed = parse(" ".join(["shabd"] * 6000))

    assert check_word_count(parsed) == []


def test_word_count_flags_short_script():
    parsed = parse(" ".join(["shabd"] * 3000))

    findings = check_word_count(parsed)

    assert len(findings) == 1
    assert findings[0].gate == "word_count"
    assert findings[0].severity == "error"
    assert "3000" in findings[0].message


def test_word_count_flags_long_script():
    parsed = parse(" ".join(["shabd"] * 9000))

    assert [f.gate for f in check_word_count(parsed)] == ["word_count"]


def test_act_budgets_pass_at_target():
    parsed = parse(_script_with_acts({1: 90, 2: 440, 3: 3360, 4: 1590, 5: 530}))

    assert check_act_budgets(parsed) == []


def test_act_budgets_pass_inside_tolerance():
    # Act II budget is 440; +14% is 501, inside the +-15% band.
    parsed = parse(_script_with_acts({1: 90, 2: 501, 3: 3360, 4: 1590, 5: 530}))

    assert check_act_budgets(parsed) == []


def test_act_budgets_flag_overrun():
    # Act II at 700 words is +59%.
    parsed = parse(_script_with_acts({1: 90, 2: 700, 3: 3360, 4: 1590, 5: 530}))

    findings = check_act_budgets(parsed)

    assert len(findings) == 1
    assert findings[0].gate == "act_budget"
    assert "Act 2" in findings[0].message


def test_act_budgets_flag_missing_act():
    parsed = parse(_script_with_acts({1: 90, 2: 440, 3: 3360, 4: 1590}))

    findings = check_act_budgets(parsed)

    assert any("Act 5" in f.message and "missing" in f.message for f in findings)


def test_act_budgets_flag_out_of_order_acts():
    source = "[ACT:2 Origin] " + " ".join(["shabd"] * 440) + " [ACT:1 Cold Open] " + " ".join(["shabd"] * 90)
    parsed = parse(source)

    findings = check_act_budgets(parsed)

    assert any("ascending order" in f.message for f in findings)

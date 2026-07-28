from rabbithole.markers import parse
from rabbithole.validate import Finding, format_report, validate_all


def _minimal_valid_script() -> str:
    parts = [
        "[ACT:1 Cold Open]",
        " ".join(["shabd"] * 90),
        "[ACT:2 Origin]",
        " ".join(["shabd"] * 440),
        "[ACT:3 Rabbit Hole]",
    ]
    # Act III: 3360 words with re-hooks every 672 words (3.8 min at 177 WPM).
    for i in range(5):
        parts.append(" ".join(["shabd"] * 672))
        if i < 4:
            parts.append("[REHOOK]")
    parts += [
        "[ACT:4 Climax]",
        " ".join(["shabd"] * 1590),
        "[ACT:5 Outro]",
        " ".join(["shabd"] * 530),
    ]
    return " ".join(parts)


def test_valid_script_produces_no_findings():
    assert validate_all(parse(_minimal_valid_script()), sfx_names=set()) == []


def test_validate_all_collects_across_gates():
    parsed = parse("[ACT:1 Cold Open] tum yahan ho. [SILENCE:9s]")

    gates = {f.gate for f in validate_all(parsed, sfx_names=set())}

    assert "word_count" in gates
    assert "register" in gates
    assert "marker_args" in gates
    assert "act_budget" in gates


def test_format_report_says_passed_when_clean():
    assert "PASSED" in format_report([])


def test_format_report_lists_findings_with_line_numbers():
    parsed = parse("[ACT:1 Cold Open] tum yahan ho.")
    report = format_report(validate_all(parsed, sfx_names=set()))

    assert "FAILED" in report
    assert "register" in report
    assert "line 1" in report


def test_format_report_all_error_report_has_no_zero_warning_noise():
    report = format_report(
        [Finding(gate="register", severity="error", message="bad", line=1)]
    )

    assert "FAILED" in report
    assert "0 warning" not in report


def test_format_report_warnings_only_does_not_say_failed():
    report = format_report(
        [Finding(gate="slots", severity="warning", message="slot too long", line=3)]
    )

    assert "FAILED" not in report


def test_format_report_warnings_only_states_warning_count():
    report = format_report(
        [
            Finding(gate="slots", severity="warning", message="one", line=1),
            Finding(gate="slots", severity="warning", message="two", line=2),
        ]
    )

    assert "2 warning" in report


def test_format_report_mixed_report_says_failed():
    report = format_report(
        [
            Finding(gate="register", severity="error", message="bad", line=1),
            Finding(gate="slots", severity="warning", message="slot too long", line=3),
        ]
    )

    assert "FAILED" in report


def test_format_report_mixed_report_states_both_counts():
    report = format_report(
        [
            Finding(gate="register", severity="error", message="e1", line=1),
            Finding(gate="register", severity="error", message="e2", line=2),
            Finding(gate="slots", severity="warning", message="w1", line=3),
        ]
    )

    assert "2 error" in report
    assert "1 warning" in report


def test_format_report_every_line_shows_its_severity():
    report = format_report(
        [
            Finding(gate="register", severity="error", message="bad", line=1),
            Finding(gate="slots", severity="warning", message="slot too long", line=3),
        ]
    )

    lines = [line for line in report.splitlines() if line.strip().startswith("[")]
    assert len(lines) == 2
    error_line = next(line for line in lines if "register" in line)
    warning_line = next(line for line in lines if "slots" in line)
    assert "error" in error_line.lower()
    assert "warning" in warning_line.lower()

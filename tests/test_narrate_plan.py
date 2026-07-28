import pytest

from rabbithole.markers import parse
from rabbithole.narrate import SilenceGap, SpeechChunk, plan_narration


def test_plan_returns_one_chunk_for_short_script():
    plan = plan_narration(parse("Ek chhota sa script hai."))

    assert len(plan) == 1
    assert isinstance(plan[0], SpeechChunk)
    assert plan[0].text == "Ek chhota sa script hai."


def test_silence_marker_splits_the_plan():
    plan = plan_narration(parse("Pehla hissa. [SILENCE:1.5s] Dusra hissa."))

    assert [type(p).__name__ for p in plan] == ["SpeechChunk", "SilenceGap", "SpeechChunk"]
    assert plan[1] == SilenceGap(seconds=1.5)
    assert plan[0].text == "Pehla hissa."
    assert plan[2].text == "Dusra hissa."


def test_chunks_are_indexed_in_order_ignoring_gaps():
    plan = plan_narration(parse("Ek. [SILENCE:1.0s] Do. [SILENCE:0.5s] Teen."))

    chunks = [p for p in plan if isinstance(p, SpeechChunk)]
    assert [c.index for c in chunks] == [0, 1, 2]


def test_long_text_splits_on_paragraph_boundaries():
    para = " ".join(["shabd"] * 300)  # ~1800 chars
    plan = plan_narration(parse(f"{para}\n\n{para}"), max_chars=2000)

    chunks = [p for p in plan if isinstance(p, SpeechChunk)]
    assert len(chunks) == 2
    assert all(len(c.text) <= 2000 for c in chunks)


def test_oversized_paragraph_splits_on_sentences():
    sentence = " ".join(["shabd"] * 20) + "."
    para = " ".join([sentence] * 40)  # far over the limit, no blank lines
    plan = plan_narration(parse(para), max_chars=1000)

    chunks = [p for p in plan if isinstance(p, SpeechChunk)]
    assert len(chunks) > 1
    assert all(len(c.text) <= 1000 for c in chunks)


def test_previous_and_next_text_stitch_adjacent_chunks():
    plan = plan_narration(parse("Pehla hissa. [SILENCE:1.0s] Dusra hissa."))
    chunks = [p for p in plan if isinstance(p, SpeechChunk)]

    assert chunks[0].previous_text == ""
    assert chunks[0].next_text == "Dusra hissa."
    assert chunks[1].previous_text == "Pehla hissa."
    assert chunks[1].next_text == ""


def test_stitch_context_is_capped_in_length():
    long_para = " ".join(["shabd"] * 400)
    plan = plan_narration(parse(f"{long_para}\n\n{long_para}"), max_chars=2500)
    chunks = [p for p in plan if isinstance(p, SpeechChunk)]

    assert len(chunks[1].previous_text) <= 500


def test_empty_script_plans_nothing():
    assert plan_narration(parse("")) == []


def test_malformed_silence_arg_raises_with_line_number():
    parsed = parse("Pehla hissa.\n[SILENCE:abc]\nDusra hissa.")

    with pytest.raises(RuntimeError, match="line 2"):
        plan_narration(parsed)


def test_malformed_silence_arg_names_the_marker():
    parsed = parse("Ek. [SILENCE:1.5ssss] Do.")

    with pytest.raises(RuntimeError, match=r"\[SILENCE:1\.5ssss\]"):
        plan_narration(parsed)


def test_uppercase_silence_suffix_is_rejected_clearly():
    parsed = parse("Ek. [SILENCE:1.5S] Do.")

    with pytest.raises(RuntimeError):
        plan_narration(parsed)

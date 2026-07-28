import pytest

from rabbithole.narrate import SilenceGap, SpeechChunk, SynthResult
from rabbithole.timing import WordTime, build_word_times, marker_times
from rabbithole.markers import parse


def _synth(text: str, per_char: float = 0.1) -> SynthResult:
    return SynthResult(
        audio=b"",
        characters=tuple(text),
        start_times=tuple(i * per_char for i in range(len(text))),
        end_times=tuple((i + 1) * per_char for i in range(len(text))),
    )


def test_word_times_cover_every_word():
    chunk = SpeechChunk(index=0, text="Ek do teen", previous_text="", next_text="")

    times = build_word_times([(chunk, _synth("Ek do teen"))], [])

    assert len(times) == 3
    assert all(isinstance(t, WordTime) for t in times)


def test_word_times_start_at_first_character_of_the_word():
    chunk = SpeechChunk(index=0, text="Ek do", previous_text="", next_text="")

    times = build_word_times([(chunk, _synth("Ek do"))], [])

    # "Ek" spans chars 0-1, "do" spans chars 3-4.
    assert times[0].start == pytest.approx(0.0)
    assert times[1].start == pytest.approx(0.3)


def test_word_times_end_at_last_character_of_the_word():
    chunk = SpeechChunk(index=0, text="Ek do", previous_text="", next_text="")

    times = build_word_times([(chunk, _synth("Ek do"))], [])

    assert times[0].end == pytest.approx(0.2)
    assert times[1].end == pytest.approx(0.5)


def test_second_chunk_is_offset_by_the_first_chunk_duration():
    a = SpeechChunk(index=0, text="Ek", previous_text="", next_text="")
    b = SpeechChunk(index=1, text="do", previous_text="", next_text="")

    times = build_word_times([(a, _synth("Ek")), (b, _synth("do"))], [])

    assert times[0].start == pytest.approx(0.0)
    assert times[1].start == pytest.approx(0.2)


def test_silence_gap_shifts_everything_after_it():
    a = SpeechChunk(index=0, text="Ek", previous_text="", next_text="")
    b = SpeechChunk(index=1, text="do", previous_text="", next_text="")

    times = build_word_times(
        [(a, _synth("Ek")), (b, _synth("do"))],
        [(1, SilenceGap(seconds=1.5))],
    )

    assert times[0].start == pytest.approx(0.0)
    assert times[1].start == pytest.approx(1.7)


def test_marker_times_resolve_each_marker_to_a_timestamp():
    parsed = parse("Ek do [SFX:vhs-burst] teen chaar")
    chunk = SpeechChunk(index=0, text="Ek do teen chaar", previous_text="", next_text="")

    times = build_word_times([(chunk, _synth("Ek do teen chaar"))], [])
    resolved = marker_times(parsed, times)

    assert len(resolved) == 1
    marker, seconds = resolved[0]
    assert marker.kind == "SFX"
    # word_index 2 means the marker fires as "teen" begins.
    assert seconds == pytest.approx(times[2].start)


def test_marker_at_end_of_script_resolves_to_the_final_word_end():
    parsed = parse("Ek do [MUSIC:out]")
    chunk = SpeechChunk(index=0, text="Ek do", previous_text="", next_text="")

    times = build_word_times([(chunk, _synth("Ek do"))], [])
    resolved = marker_times(parsed, times)

    assert resolved[0][1] == pytest.approx(times[-1].end)


def test_empty_input_produces_no_times():
    assert build_word_times([], []) == []


def test_alignment_shorter_than_chunk_text_raises():
    chunk = SpeechChunk(index=0, text="Ek do teen", previous_text="", next_text="")
    truncated = SynthResult(
        audio=b"",
        characters=tuple("Ek do"),
        start_times=tuple(i * 0.1 for i in range(5)),
        end_times=tuple((i + 1) * 0.1 for i in range(5)),
    )

    with pytest.raises(RuntimeError, match="alignment"):
        build_word_times([(chunk, truncated)], [])


def test_alignment_length_mismatch_names_the_chunk():
    chunk = SpeechChunk(index=7, text="Ek do teen", previous_text="", next_text="")
    truncated = SynthResult(
        audio=b"",
        characters=tuple("Ek"),
        start_times=(0.0, 0.1),
        end_times=(0.1, 0.2),
    )

    with pytest.raises(RuntimeError, match="chunk 7"):
        build_word_times([(chunk, truncated)], [])


def test_alignment_longer_than_chunk_text_also_raises():
    chunk = SpeechChunk(index=0, text="Ek", previous_text="", next_text="")
    overlong = SynthResult(
        audio=b"",
        characters=tuple("Ek do teen"),
        start_times=tuple(i * 0.1 for i in range(10)),
        end_times=tuple((i + 1) * 0.1 for i in range(10)),
    )

    with pytest.raises(RuntimeError, match="alignment"):
        build_word_times([(chunk, overlong)], [])


def test_gap_keyed_to_an_unknown_chunk_raises():
    chunk = SpeechChunk(index=0, text="Ek", previous_text="", next_text="")

    with pytest.raises(RuntimeError, match="99"):
        build_word_times([(chunk, _synth("Ek"))], [(99, SilenceGap(seconds=5.0))])

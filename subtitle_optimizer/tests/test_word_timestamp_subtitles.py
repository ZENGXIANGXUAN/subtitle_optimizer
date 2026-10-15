from __future__ import annotations

from core import transcription as T


def _timed_words(text: str, start: float, end: float) -> list[dict]:
    pieces = text.split()
    step = (end - start) / len(pieces)
    return [
        {
            "start": start + index * step,
            "end": end if index == len(pieces) - 1 else start + (index + 0.82) * step,
            "text": piece,
        }
        for index, piece in enumerate(pieces)
    ]


def test_word_timestamps_replace_an_early_segment_end() -> None:
    text = (
        "You do that, you're on the path to setting not just successful goals, "
        "but actually finally for the first time in your life achieving those goals."
    )
    result = {
        # This is the bad segment boundary observed in UNIT 7: the cue ended
        # while the final words were still being spoken.
        "segments": [{"start": 424.412, "end": 432.012, "text": text}],
        "words": _timed_words(text, 424.412, 435.616),
        "_timestamp_granularity": "word",
    }

    segments = T._subtitle_segments_from_result(result)

    assert segments[-1]["end"] == 435.616
    assert all(
        current["start"] - previous["end"] < 0.5
        for previous, current in zip(segments, segments[1:])
    )
    assert "00:07:15,616" in T.generate_srt_content(result)


def test_mistral_word_segments_are_grouped_into_readable_cues() -> None:
    result = {
        "segments": _timed_words(
            "Word timestamps keep every spoken word visible until it ends.",
            10.0,
            14.2,
        ),
        "_timestamp_granularity": "word",
    }

    segments = T._subtitle_segments_from_result(result)

    assert 1 <= len(segments) < len(result["segments"])
    assert segments[0]["start"] == 10.0
    assert segments[-1]["end"] == 14.2
    assert "every spoken word visible" in " ".join(
        segment["text"] for segment in segments
    )


def test_chunked_transcription_keeps_the_last_word_end_after_offsetting() -> None:
    text = (
        "You do that, you're on the path to setting not just successful goals, "
        "but actually finally achieving those goals."
    )
    chunk_result = {
        "segments": _timed_words(text, 4.412, 15.616),
        "_timestamp_granularity": "word",
    }

    merged = T._merge_chunk_segments(
        [chunk_result],
        chunk_starts=[420.0],
        overlap=120.0,
    )

    assert merged[0]["start"] == 424.412
    assert merged[-1]["end"] == 435.616


def test_audio_silence_boundary_extends_the_unit_7_tail_words() -> None:
    segments = [
        {
            "start": 428.812,
            "end": 432.012,
            "text": "finally for the first time in your life achieving those goals.",
        },
        {
            "start": 439.512,
            "end": 444.212,
            "text": "So the strategy continues.",
        },
    ]

    aligned, changed = T._align_subtitle_ends_to_silences(
        segments,
        # The short 0.35s pause inside the sentence is deliberately absent;
        # only meaningful silences of at least 0.6s reach this helper.
        [(435.615896, 438.592041)],
    )

    assert changed == 1
    assert aligned[0]["end"] == 435.615896
    assert aligned[1]["end"] == 444.212


def test_audio_alignment_does_not_fill_a_real_silent_section() -> None:
    segments = [
        {"start": 0.0, "end": 1.0, "text": "Finished."},
        {"start": 4.0, "end": 5.0, "text": "Next sentence."},
    ]

    aligned, changed = T._align_subtitle_ends_to_silences(
        segments,
        [(1.0, 4.0)],
    )

    assert changed == 0
    assert aligned == segments


def test_audio_alignment_closes_overlapping_cue_at_next_word_start() -> None:
    segments = [
        {"start": 10.0, "end": 13.6, "text": "First cue."},
        {"start": 13.1, "end": 16.0, "text": "Next cue."},
    ]

    aligned, changed = T._align_subtitle_ends_to_silences(segments, [])

    assert changed == 1
    assert aligned[0]["end"] == 13.1
    assert aligned[1]["start"] == 13.1


def test_real_silence_stays_blank_between_word_cues() -> None:
    result = {
        "segments": [
            {"start": 0.0, "end": 0.4, "text": "Hello"},
            {"start": 0.5, "end": 0.9, "text": "world."},
            {"start": 4.0, "end": 4.4, "text": "Next"},
            {"start": 4.5, "end": 4.9, "text": "sentence."},
        ],
        "_timestamp_granularity": "word",
    }

    segments = T._subtitle_segments_from_result(result)

    assert [(segment["start"], segment["end"]) for segment in segments] == [
        (0.0, 0.9),
        (4.0, 4.9),
    ]


def test_legacy_segment_response_is_left_unchanged() -> None:
    original = [
        {"start": 1.0, "end": 2.5, "text": "Existing segment."},
    ]

    assert T._subtitle_segments_from_result({"segments": original}) == original

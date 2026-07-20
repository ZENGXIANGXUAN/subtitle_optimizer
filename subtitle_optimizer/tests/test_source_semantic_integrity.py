from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models import SubtitleEntry
from core.semantic_units import (
    ensure_source_cues,
    reflow_semantic_entries,
    source_integrity_errors,
)
from core.source_normalization import (
    LAYOUT_AUTO,
    LAYOUT_BILINGUAL,
    LAYOUT_MONOLINGUAL,
    normalize_source_entries,
)


def _entry(index: int, start: str, end: str, lines: list[str]) -> SubtitleEntry:
    entry = SubtitleEntry(index, start, end, lines)
    entry.original_lines = list(lines)
    entry.source_indexes = [index]
    return entry


def _visible(text: str) -> str:
    return re.sub(r"\s+", "", text)


def test_auto_layout_preserves_every_line_of_wrapped_monolingual_subtitle() -> None:
    source = [
        _entry(
            1,
            "00:00:00,000",
            "00:00:02,000",
            ["Trading is an individual", "endeavor at the end of the day."],
        )
    ]

    result = normalize_source_entries(
        source,
        source_line_index=1,
        input_lang_code="en",
        output_lang_code="zh",
        requested_layout=LAYOUT_AUTO,
    )

    assert result.detected_layout == LAYOUT_MONOLINGUAL
    assert result.entries[0].lines == [
        "Trading is an individual endeavor at the end of the day."
    ]
    assert result.entries[0].original_lines == source[0].original_lines
    assert result.entries[0].source_indexes == [1]
    assert result.entries[0].source_cues[0].source_text == result.entries[0].lines[0]


def test_auto_layout_extracts_source_from_unambiguous_bilingual_subtitle() -> None:
    source = [
        _entry(
            7,
            "00:00:10,000",
            "00:00:12,000",
            ["归根结底，交易是一项个人事业。", "Trading is an individual endeavor."],
        )
    ]

    result = normalize_source_entries(
        source,
        source_line_index=1,
        input_lang_code="en",
        output_lang_code="zh",
        requested_layout=LAYOUT_AUTO,
    )

    assert result.detected_layout == LAYOUT_BILINGUAL
    assert result.confidence == 1.0
    assert result.entries[0].lines == ["Trading is an individual endeavor."]
    assert result.entries[0].original_lines == source[0].original_lines


def test_auto_layout_preserves_mixed_anomaly_in_otherwise_bilingual_file() -> None:
    source = [
        _entry(1, "00:00:00,000", "00:00:01,000", ["你好。", "Hello."]),
        _entry(2, "00:00:01,000", "00:00:02,000", ["再见。", "Goodbye."]),
        _entry(
            3,
            "00:00:02,000",
            "00:00:04,000",
            ["This source cue wraps", "onto a second English line."],
        ),
    ]

    result = normalize_source_entries(
        source,
        source_line_index=1,
        input_lang_code="en",
        output_lang_code="zh",
        requested_layout=LAYOUT_AUTO,
    )

    assert result.detected_layout == LAYOUT_BILINGUAL
    assert result.entries[0].lines == ["Hello."]
    assert result.entries[1].lines == ["Goodbye."]
    assert result.entries[2].lines == [
        "This source cue wraps onto a second English line."
    ]
    assert any("#3" in warning for warning in result.warnings)


def test_forced_bilingual_layout_supports_same_script_languages() -> None:
    source = [
        _entry(
            4,
            "00:00:04,000",
            "00:00:06,000",
            ["Bonjour tout le monde.", "Hello everyone."],
        )
    ]

    automatic = normalize_source_entries(
        source,
        source_line_index=1,
        input_lang_code="en",
        output_lang_code="fr",
        requested_layout=LAYOUT_AUTO,
    )
    forced = normalize_source_entries(
        source,
        source_line_index=1,
        input_lang_code="en",
        output_lang_code="fr",
        requested_layout=LAYOUT_BILINGUAL,
    )

    assert automatic.detected_layout == LAYOUT_MONOLINGUAL
    assert automatic.entries[0].lines == ["Bonjour tout le monde. Hello everyone."]
    assert forced.detected_layout == LAYOUT_BILINGUAL
    assert forced.entries[0].lines == ["Hello everyone."]


def test_auto_bilingual_layout_keeps_all_source_language_continuation_lines() -> None:
    source = [
        _entry(
            5,
            "00:00:06,000",
            "00:00:09,000",
            [
                "归根结底，",
                "交易是一项个人事业。",
                "At the end of the day,",
                "trading is an individual endeavor.",
            ],
        )
    ]

    result = normalize_source_entries(
        source,
        source_line_index=2,
        input_lang_code="en",
        output_lang_code="zh",
        requested_layout=LAYOUT_AUTO,
    )

    assert result.detected_layout == LAYOUT_BILINGUAL
    assert result.entries[0].lines == [
        "At the end of the day, trading is an individual endeavor."
    ]


def test_semantic_translation_reflows_to_original_timeline_exactly_once() -> None:
    originals = [
        _entry(11, "00:00:10,000", "00:00:11,000", ["At the end"]),
        _entry(12, "00:00:11,000", "00:00:12,500", ["of the day, trading is"]),
        _entry(13, "00:00:12,500", "00:00:14,000", ["an individual endeavor."]),
    ]
    ensure_source_cues(originals, "en")
    original_cues = [cue for entry in originals for cue in entry.source_cues]

    semantic_unit = _entry(
        11,
        originals[0].start_time,
        originals[-1].end_time,
        ["归根结底，交易是一项个人事业。", "At the end of the day, trading is an individual endeavor."],
    )
    semantic_unit.optimized_lines = list(semantic_unit.lines)
    semantic_unit.source_indexes = [11, 12, 13]
    semantic_unit.source_cues = list(original_cues)

    flowed = reflow_semantic_entries(
        [semantic_unit],
        input_lang_code="en",
        output_lang_code="zh",
        use_optimized=True,
    )

    assert [(entry.index, entry.start_time, entry.end_time) for entry in flowed] == [
        (11, "00:00:10,000", "00:00:11,000"),
        (12, "00:00:11,000", "00:00:12,500"),
        (13, "00:00:12,500", "00:00:14,000"),
    ]
    assert [entry.lines[1] for entry in flowed] == [
        "At the end",
        "of the day, trading is",
        "an individual endeavor.",
    ]
    assert _visible("".join(entry.lines[0] for entry in flowed)) == _visible(
        semantic_unit.lines[0]
    )
    assert [cue.index for entry in flowed for cue in entry.source_cues] == [11, 12, 13]
    assert source_integrity_errors(original_cues, flowed, "en") == []


def test_source_integrity_rejects_duplicate_or_reordered_source_cues() -> None:
    originals = [
        _entry(1, "00:00:00,000", "00:00:01,000", ["one"]),
        _entry(2, "00:00:01,000", "00:00:02,000", ["two"]),
    ]
    ensure_source_cues(originals, "en")
    original_cues = [cue for entry in originals for cue in entry.source_cues]

    duplicate = _entry(1, "00:00:00,000", "00:00:02,000", ["一二", "one two"])
    duplicate.source_cues = [original_cues[0], original_cues[0], original_cues[1]]
    reordered = _entry(1, "00:00:00,000", "00:00:02,000", ["一二", "one two"])
    reordered.source_cues = [original_cues[1], original_cues[0]]

    assert source_integrity_errors(original_cues, [duplicate], "en")
    assert source_integrity_errors(original_cues, [reordered], "en")


def test_srt_output_order_is_canonical_for_chinese_to_english() -> None:
    entry = _entry(
        1,
        "00:00:00,000",
        "00:00:02,000",
        ["Trading is personal.", "交易是个人行为。"],
    )
    entry.optimized_lines = list(entry.lines)

    target_first = entry.to_srt_block(use_optimized=True, target_first=True)
    source_first = entry.to_srt_block(use_optimized=True, target_first=False)

    assert "Trading is personal.\n交易是个人行为。" in target_first
    assert "交易是个人行为。\nTrading is personal." in source_first

from __future__ import annotations

import pytest

from core.models import SourceCueSnapshot, SubtitleEntry
from core.srt_lint import finalize_entries


def _cue(
    index: int,
    start_time: str,
    end_time: str,
    source_text: str,
    original_line: str,
) -> SourceCueSnapshot:
    return SourceCueSnapshot(
        index=index,
        start_time=start_time,
        end_time=end_time,
        source_text=source_text,
        original_lines=(original_line,),
        source_indexes=(index,),
    )


def _entry(
    index: int,
    start_time: str,
    end_time: str,
    lines: list[str],
    cues: list[SourceCueSnapshot],
) -> SubtitleEntry:
    entry = SubtitleEntry(
        index=index,
        start_time=start_time,
        end_time=end_time,
        lines=list(lines),
    )
    entry.optimized_lines = list(lines)
    entry.original_lines = [line for cue in cues for line in cue.original_lines]
    entry.source_indexes = [source_index for cue in cues for source_index in cue.source_indexes]
    entry.source_cues = list(cues)
    return entry


def _assert_provenance(
    entry: SubtitleEntry,
    expected_cues: list[SourceCueSnapshot],
) -> None:
    assert entry.source_cues == expected_cues
    assert entry.source_indexes == [cue.index for cue in expected_cues]
    assert entry.original_lines == [line for cue in expected_cues for line in cue.original_lines]


def test_adjacent_duplicate_merge_deduplicates_overlapping_source_metadata() -> None:
    cue_1 = _cue(1, "00:00:00,000", "00:00:01,000", "same", "source line one")
    cue_2 = _cue(2, "00:00:01,100", "00:00:02,000", "same", "source line two")
    entries = [
        _entry(1, cue_1.start_time, cue_1.end_time, ["相同", "same"], [cue_1]),
        # Deliberately overlap cue_1 to exercise ordered de-duplication.
        _entry(2, cue_2.start_time, cue_2.end_time, ["相同", "same"], [cue_1, cue_2]),
    ]

    fixed, report = finalize_entries(entries, bilingual_mode=True)

    assert len(fixed) == 1
    assert report.duplicates_merged == 1
    _assert_provenance(fixed[0], [cue_1, cue_2])


@pytest.mark.parametrize("short_position", ["before", "after"])
def test_short_entry_merge_preserves_each_source_snapshot_once(short_position: str) -> None:
    cue_1 = _cue(1, "00:00:00,000", "00:00:01,000", "begin", "source line one")
    cue_2 = _cue(2, "00:00:01,050", "00:00:02,050", "continue", "source line two")

    if short_position == "before":
        cue_1 = _cue(1, "00:00:00,000", "00:00:00,200", "uh", "source line one")
        cue_2 = _cue(2, "00:00:00,250", "00:00:01,250", "continue", "source line two")
        entries = [
            _entry(1, cue_1.start_time, cue_1.end_time, ["嗯", "uh"], [cue_1]),
            _entry(2, cue_2.start_time, cue_2.end_time, ["继续", "continue"], [cue_2]),
        ]
    else:
        cue_2 = _cue(2, "00:00:01,050", "00:00:01,250", "uh", "source line two")
        entries = [
            _entry(1, cue_1.start_time, cue_1.end_time, ["开始", "begin"], [cue_1]),
            _entry(2, cue_2.start_time, cue_2.end_time, ["嗯", "uh"], [cue_2]),
        ]

    fixed, report = finalize_entries(entries, bilingual_mode=True)

    assert len(fixed) == 1
    assert report.short_entries_merged == 1
    _assert_provenance(fixed[0], [cue_1, cue_2])

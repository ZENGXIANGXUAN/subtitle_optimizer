"""segment() must not mutate the entry list its caller handed in.

Every path through ``_apply_llm_merge`` returns fresh entries, but the
``_llm_merge`` exception path used to return ``batch`` itself.  Since
``segment()`` renumbers its result in place, a failed merge rewrote the
caller's own indexes -- producing duplicates in the live list the UI maps by
index, and leaving ``verify_source_integrity()`` comparing the result against
ids it had already overwritten, so a genuine loss could no longer be detected.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models import SubtitleEntry, verify_source_integrity
from core.pipeline import SubtitlePipeline


class _MergeThenFailClient:
    """First batch merges into pairs; every later batch fails the merge call."""

    is_local_proxy = False

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, session, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return json.dumps([{"ids": [i, i + 1]} for i in range(1, 11, 2)])
        raise RuntimeError("simulated gateway failure")


def _entry(index: int) -> SubtitleEntry:
    entry = SubtitleEntry(
        index=index,
        start_time=f"00:00:{index:02d},000",
        end_time=f"00:00:{index:02d},900",
        lines=[f"line {index} ends with"],
    )
    entry.original_lines = list(entry.lines)
    entry.source_indexes = [index]
    return entry


def test_failed_merge_does_not_renumber_the_caller_list() -> None:
    entries = [_entry(i) for i in range(1, 16)]
    before = [entry.index for entry in entries]

    pipeline = SubtitlePipeline(
        _MergeThenFailClient(), "English", "中文", concurrency=1, batch_size=2
    )
    result = asyncio.run(pipeline.segment(entries, "context", ""))

    assert [entry.index for entry in entries] == before
    assert not any(produced is original for produced in result for original in entries)


def test_failed_merge_keeps_every_source_cue_exactly_once() -> None:
    entries = [_entry(i) for i in range(1, 16)]

    pipeline = SubtitlePipeline(
        _MergeThenFailClient(), "English", "中文", concurrency=1, batch_size=2
    )
    result = asyncio.run(pipeline.segment(entries, "context", ""))

    produced = sorted(i for entry in result for i in entry.source_indexes)
    assert produced == list(range(1, 16))
    # The integrity check must still see the caller's pristine ids.
    assert verify_source_integrity(entries, result) == set()


def test_result_is_renumbered_contiguously_from_one() -> None:
    entries = [_entry(i) for i in range(1, 16)]

    pipeline = SubtitlePipeline(
        _MergeThenFailClient(), "English", "中文", concurrency=1, batch_size=2
    )
    result = asyncio.run(pipeline.segment(entries, "context", ""))

    assert [entry.index for entry in result] == list(range(1, len(result) + 1))

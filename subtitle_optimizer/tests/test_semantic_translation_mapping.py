from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models import SubtitleEntry
from core.pipeline import SubtitlePipeline
from core.quality import fidelity_review_batch
from core.semantic_units import ensure_source_cues, reflow_semantic_entries
from core.workers import _llm_merge_group_is_safe


def _source(index: int, start: str, end: str, text: str) -> SubtitleEntry:
    entry = SubtitleEntry(index, start, end, [text])
    entry.original_lines = [text]
    entry.source_indexes = [index]
    return entry


def _semantic_entry() -> SubtitleEntry:
    first = _source(101, "00:00:00,000", "00:00:08,500", "The mass stays at rest")
    second = _source(102, "00:00:08,500", "00:00:18,000", "unless a new force acts on it.")
    ensure_source_cues([first, second], "en")

    semantic = SubtitleEntry(
        1,
        first.start_time,
        second.end_time,
        ["The mass stays at rest unless a new force acts on it."],
    )
    semantic.original_lines = [first.lines[0], second.lines[0]]
    semantic.source_indexes = [101, 102]
    semantic.source_cues = [first.source_cues[0], second.source_cues[0]]
    return semantic


def test_translation_merge_respects_display_duration_cap_without_mapping() -> None:
    left = _source(
        1,
        "00:00:00,000",
        "00:00:08,500",
        " ".join(["first"] * 30),
    )
    right = _source(
        2,
        "00:00:08,500",
        "00:00:18,000",
        " ".join(["second"] * 27),
    )

    # The normal pipeline fail-closes on unverified cue mappings, so a semantic
    # merge remains on its combined timeline. Long groups would therefore turn
    # into long displayed subtitles and must be rejected.
    assert not _llm_merge_group_is_safe([left, right], [left, right], "en")


def test_independently_verified_mapping_returns_translation_to_original_cue_timeline() -> None:
    semantic = _semantic_entry()
    semantic.lines = [
        "除非有新的力作用在它上面，否则该物体会保持静止。",
        "The mass stays at rest unless a new force acts on it.",
    ]

    translated = reflow_semantic_entries(
        [semantic],
        input_lang_code="en",
        cue_target_mappings={
            1: {
                101: "该物体会保持静止",
                102: "除非有新的力作用在它上面。",
            }
        },
        cue_target_mappings_verified=True,
        target_is_usable=lambda target, source: bool(target and source),
        use_optimized=False,
    )

    assert [(entry.index, entry.start_time, entry.end_time) for entry in translated] == [
        (101, "00:00:00,000", "00:00:08,500"),
        (102, "00:00:08,500", "00:00:18,000"),
    ]
    assert [entry.lines for entry in translated] == [
        ["该物体会保持静止", "The mass stays at rest"],
        ["除非有新的力作用在它上面。", "unless a new force acts on it."],
    ]
    assert all("semantic_cue_mapping_applied" in entry.quality_flags for entry in translated)


def test_invalid_mapping_keeps_complete_semantic_translation() -> None:
    semantic = _semantic_entry()
    semantic.lines = [
        "除非有新的力作用在它上面，否则该物体会保持静止。",
        "The mass stays at rest unless a new force acts on it.",
    ]

    flowed = reflow_semantic_entries(
        [semantic],
        input_lang_code="en",
        cue_target_mappings={1: {101: "该物体会保持静止"}},
        target_is_usable=lambda target, source: bool(target and source),
        use_optimized=False,
    )

    assert len(flowed) == 1
    assert flowed[0].start_time == "00:00:00,000"
    assert flowed[0].end_time == "00:00:18,000"
    assert flowed[0].lines[0] == "除非有新的力作用在它上面，否则该物体会保持静止。"


def test_fidelity_prompt_includes_duration_budget_and_coverage_check() -> None:
    entry = SubtitleEntry(
        1,
        "00:00:00,000",
        "00:00:09,500",
        ["它取决于质量及其与重心的距离", "how far away the mass is from the center of gravity or the point"],
    )

    class FidelityClient:
        def __init__(self) -> None:
            self.messages = []

        async def chat(self, messages, session, **kwargs):
            self.messages = messages
            return '[{"id": 1, "tgt": "它取决于质量及其与重心或指定参考点的距离。"}]'

    client = FidelityClient()
    result = asyncio.run(
        fidelity_review_batch(
            [entry], "physics", "", client, object(), "English", "中文"
        )
    )

    assert result[1].startswith("它取决于")
    assert '"sec": 9.5' in client.messages[1]["content"]
    assert '"budget": 85' in client.messages[1]["content"]
    assert "A or B" in client.messages[0]["content"]
    assert "信息完整" in client.messages[0]["content"]

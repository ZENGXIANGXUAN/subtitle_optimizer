from __future__ import annotations

import asyncio
import json

from core.models import SubtitleEntry
from core.pipeline import SubtitlePipeline
from core.semantic_units import ensure_source_cues


def _semantic_entry() -> SubtitleEntry:
    first = SubtitleEntry(
        101,
        "00:00:00,000",
        "00:00:02,000",
        ["Is it in value?"],
    )
    second = SubtitleEntry(
        102,
        "00:00:02,000",
        "00:00:06,000",
        ["Within the first hour, form a hypothesis."],
    )
    for entry in (first, second):
        entry.original_lines = list(entry.lines)
        entry.source_indexes = [entry.index]
    ensure_source_cues([first, second], "en")

    semantic = SubtitleEntry(
        1,
        first.start_time,
        second.end_time,
        ["Is it in value? Within the first hour, form a hypothesis."],
    )
    semantic.original_lines = [first.lines[0], second.lines[0]]
    semantic.source_indexes = [101, 102]
    semantic.source_cues = [first.source_cues[0], second.source_cues[0]]
    return semantic


def test_translation_does_not_reflow_unverified_cue_targets() -> None:
    class SwappedMappingClient:
        is_local_proxy = False

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, session, **kwargs):
            self.calls += 1
            return json.dumps(
                [
                    {
                        "id": 1,
                        "tgt": "它是在价值区间内吗？请在首小时内形成一个假设。",
                        "cue_targets": [
                            {"id": 101, "tgt": "请在首小时内形成一个假设。"},
                            {"id": 102, "tgt": "它是在价值区间内吗？"},
                        ],
                    }
                ],
                ensure_ascii=False,
            )

    client = SwappedMappingClient()
    translated = asyncio.run(
        SubtitlePipeline(client, "English", "中文", concurrency=1, batch_size=5).translate(
            [_semantic_entry()],
            context="",
            glossary="",
        )
    )

    # A structurally complete cue map can still be semantically swapped. Keep
    # the complete semantic unit until a dedicated semantic verifier approves
    # each cue-target pair; never place fluent but unrelated text on a cue.
    assert len(translated) == 1
    assert translated[0].start_time == "00:00:00,000"
    assert translated[0].end_time == "00:00:06,000"
    assert translated[0].lines == [
        "它是在价值区间内吗？请在首小时内形成一个假设。",
        "Is it in value? Within the first hour, form a hypothesis.",
    ]
    assert "semantic_cue_mapping_applied" not in translated[0].quality_flags
    assert client.calls == 1

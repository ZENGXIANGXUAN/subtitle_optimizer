from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models import SubtitleEntry
from core.workers import _translate_batch_impl


class _TrailingContentClient:
    async def chat(self, *_args, **_kwargs) -> str:
        payload = [
            {"id": index, "tgt": f"完整译文{index}"}
            for index in range(1, 31)
        ]
        return json.dumps(payload, ensure_ascii=False) + "\n说明：已完成翻译"


class _MalformedContentClient:
    async def chat(self, *_args, **_kwargs) -> str:
        return '[{"id": 1, "tgt": "损坏的译文}'


def _entry(index: int = 1) -> SubtitleEntry:
    return SubtitleEntry(
        index=index,
        start_time=f"00:00:{index - 1:02d},000",
        end_time=f"00:00:{index:02d},000",
        lines=[f"Complete source sentence {index}."],
    )


def _translate_with(client, entries: list[SubtitleEntry] | None = None) -> dict[int, str]:
    return asyncio.run(
        _translate_batch_impl(
            entries or [_entry()],
            context="test context",
            glossary="",
            client=client,
            session=object(),
            input_lang="English",
            output_lang="中文",
        )
    )


def test_translation_keeps_complete_json_before_trailing_content() -> None:
    entries = [_entry(index) for index in range(1, 31)]
    assert _translate_with(_TrailingContentClient(), entries) == {
        index: f"完整译文{index}"
        for index in range(1, 31)
    }


def test_translation_still_rejects_malformed_json() -> None:
    assert _translate_with(_MalformedContentClient()) == {}

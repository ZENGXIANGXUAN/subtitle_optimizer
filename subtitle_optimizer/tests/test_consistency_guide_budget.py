from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models import SubtitleEntry
from core.quality import (
    CONSISTENCY_REQUEST_BYTE_BUDGET,
    _candidate_messages,
    _consolidate_messages,
    _consistency_extra_payload,
    _merge_candidate_evidence,
    _pack_entry_candidate_chunks,
    _pack_reducer_payloads,
    _request_bytes,
    build_consistency_guide,
)


def _entry(index: int) -> SubtitleEntry:
    entry = SubtitleEntry(
        index=index,
        start_time="00:00:00,000",
        end_time="00:00:01,000",
        lines=[f"target wording {index}", f"source wording {index}"],
    )
    entry.optimized_lines = list(entry.lines)
    return entry


def test_consistency_budget_packs_1872_entries_without_global_reducer() -> None:
    entries = [_entry(index) for index in range(1, 1873)]
    chunks = _pack_entry_candidate_chunks(
        entries,
        input_code="en",
        context="",
        glossary="",
        max_items=80,
    )

    assert len(chunks) > 24  # Byte budget, rather than fixed 80-entry chunks.
    assert all(
        _request_bytes(
            _candidate_messages(chunk, "", ""), _consistency_extra_payload()
        ) <= CONSISTENCY_REQUEST_BYTE_BUDGET
        for chunk in chunks
    )

    candidates = [
        {
            "source": f"source wording {index}",
            "target": f"target wording {index}",
            "variants": [],
            "type": "term",
            "reason": "recurring",
        }
        for index in range(1, 1873)
    ]
    payloads = _pack_reducer_payloads(
        _merge_candidate_evidence(candidates), [], glossary=""
    )

    assert len(payloads) > 1
    assert all(
        _request_bytes(
            _consolidate_messages(payload, ""), _consistency_extra_payload()
        ) <= CONSISTENCY_REQUEST_BYTE_BUDGET
        for payload in payloads
    )


def test_consistency_retries_only_failed_reducer_when_state_is_reused() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.map_calls = 0
            self.reducer_calls = 0
            self.extra_payloads: list[dict] = []

        async def chat(self, messages, session, extra_payload=None, temperature=None):
            self.extra_payloads.append(extra_payload or {})
            payload = json.loads(messages[1]["content"])
            if isinstance(payload, list):
                self.map_calls += 1
                return json.dumps(
                    {
                        "terms": [
                            {
                                "source": payload[0]["src"],
                                "target": "canonical target",
                                "variants": [],
                                "type": "term",
                                "reason": "recurring",
                            }
                        ],
                        "style_rules": [],
                    }
                )
            self.reducer_calls += 1
            if self.reducer_calls <= 2:
                raise RuntimeError("temporary reducer failure")
            return json.dumps(
                {
                    "terms": payload["terms"],
                    "style_rules": payload["style_rules"],
                }
            )

    client = FakeClient()
    state: dict = {}
    entries = [_entry(1)]

    with pytest.raises(RuntimeError, match="reducer"):
        asyncio.run(
            build_consistency_guide(
                entries, "", "", client, object(), "English", "English",
                retry_state=state,
            )
        )

    guide = asyncio.run(
        build_consistency_guide(
            entries, "", "", client, object(), "English", "English",
            retry_state=state,
        )
    )

    assert client.map_calls == 1
    assert client.reducer_calls == 3
    assert "canonical target" in guide
    assert all(
        payload.get("response_format") == {"type": "json_object"}
        and payload.get("max_tokens")
        for payload in client.extra_payloads
    )

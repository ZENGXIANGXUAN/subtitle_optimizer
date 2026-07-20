from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models import SubtitleEntry
from core.pipeline import SubtitlePipeline
from core.quality_gate import evaluate_quality_gate


def _entry(index: int, source: str, target: str) -> SubtitleEntry:
    entry = SubtitleEntry(
        index=index,
        start_time=f"00:00:{index:02d},000",
        end_time=f"00:00:{index:02d},900",
        lines=[target, source],
    )
    entry.original_lines = [source]
    entry.source_indexes = [index]
    entry.optimized_lines = [target, source]
    return entry


def _run(coro_factory: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(coro_factory())


def test_fidelity_batch_failure_recovers_each_item(monkeypatch) -> None:
    entries = [
        _entry(1, "First source sentence.", "Old first target."),
        _entry(2, "Second source sentence.", "Old second target."),
    ]
    pipeline = SubtitlePipeline(
        client=object(),
        input_lang="English",
        output_lang="English",
        batch_size=2,
    )
    calls: list[list[int]] = []

    async def fake_fidelity_review(batch, *args, **kwargs):
        calls.append([entry.index for entry in batch])
        if len(batch) > 1:
            raise RuntimeError("simulated batch failure")
        entry = batch[0]
        return {entry.index: f"Recovered target {entry.index}."}

    monkeypatch.setattr(
        "core.pipeline.fidelity_review_batch",
        fake_fidelity_review,
    )

    _run(
        lambda: pipeline._run_fidelity_review(
            entries,
            context="test context",
            glossary="",
            session=object(),
        )
    )

    assert calls == [[1, 2], [1], [2]]
    assert [entry.lines[0] for entry in entries] == [
        "Recovered target 1.",
        "Recovered target 2.",
    ]
    assert all(entry.status == "done" for entry in entries)
    assert all(entry.stage_status == "fidelity_checked" for entry in entries)
    assert all("fidelity_review_failed" not in entry.quality_flags for entry in entries)


def test_consistency_guide_retries_then_runs_review(monkeypatch) -> None:
    entries = [
        _entry(1, "First source sentence.", "Initial target one."),
        _entry(2, "Second source sentence.", "Initial target two."),
    ]
    pipeline = SubtitlePipeline(
        client=object(),
        input_lang="English",
        output_lang="English",
        batch_size=2,
    )
    guide_calls = 0
    review_calls: list[list[int]] = []

    async def fake_build_guide(*args, **kwargs):
        nonlocal guide_calls
        guide_calls += 1
        if guide_calls == 1:
            raise RuntimeError("temporary guide failure")
        return "Use one consistent rendering for the recurring term."

    async def fake_consistency_review(batch, *args, **kwargs):
        review_calls.append([entry.index for entry in batch])
        return {
            entry.index: f"Consistent target {entry.index}."
            for entry in batch
        }

    monkeypatch.setattr(
        "core.pipeline.build_consistency_guide",
        fake_build_guide,
    )
    monkeypatch.setattr(
        "core.pipeline.consistency_review_batch",
        fake_consistency_review,
    )

    _run(
        lambda: pipeline._run_consistency_review(
            entries,
            context="test context",
            glossary="",
            session=object(),
        )
    )

    assert guide_calls == 2
    assert review_calls == [[1, 2]]
    assert [entry.optimized_lines[0] for entry in entries] == [
        "Consistent target 1.",
        "Consistent target 2.",
    ]
    assert all(entry.stage_status == "consistency_checked" for entry in entries)
    assert all("consistency_guide_failed" not in entry.quality_flags for entry in entries)


def test_consistency_final_failure_keeps_existing_translation_non_blocking(monkeypatch) -> None:
    entries = [
        _entry(1, "First source sentence.", "Original polished target one."),
        _entry(2, "Second source sentence.", "Original polished target two."),
    ]
    for entry in entries:
        entry.status = "done"
        entry.stage_status = "optimized"

    pipeline = SubtitlePipeline(
        client=object(),
        input_lang="English",
        output_lang="English",
        batch_size=2,
    )
    calls: list[list[int]] = []

    async def fake_build_guide(*args, **kwargs):
        return "Use one consistent rendering for the recurring term."

    async def fake_consistency_review(batch, *args, **kwargs):
        calls.append([entry.index for entry in batch])
        raise RuntimeError("simulated malformed consistency JSON")

    monkeypatch.setattr(
        "core.pipeline.build_consistency_guide",
        fake_build_guide,
    )
    monkeypatch.setattr(
        "core.pipeline.consistency_review_batch",
        fake_consistency_review,
    )

    _run(
        lambda: pipeline._run_consistency_review(
            entries,
            context="test context",
            glossary="",
            session=object(),
        )
    )

    assert calls == [[1, 2], [1], [1], [2], [2]]
    assert [entry.optimized_lines[0] for entry in entries] == [
        "Original polished target one.",
        "Original polished target two.",
    ]
    assert all(entry.status == "done" for entry in entries)
    assert all(entry.stage_status == "consistency_skipped_with_flags" for entry in entries)
    assert all(entry.quality_flags == ["consistency_review_unapplied"] for entry in entries)
    assert evaluate_quality_gate(entries).passed


def test_fidelity_final_failure_marks_only_the_failed_item(monkeypatch) -> None:
    entries = [
        _entry(1, "Recoverable source.", "Original recoverable target."),
        _entry(2, "Unrecoverable source.", "Original protected fallback."),
    ]
    pipeline = SubtitlePipeline(
        client=object(),
        input_lang="English",
        output_lang="English",
        batch_size=2,
    )
    calls: list[list[int]] = []

    async def fake_fidelity_review(batch, *args, **kwargs):
        calls.append([entry.index for entry in batch])
        if len(batch) > 1:
            raise RuntimeError("simulated batch failure")
        entry = batch[0]
        if entry.index == 2:
            raise RuntimeError("simulated permanent item failure")
        return {entry.index: "Recovered target."}

    monkeypatch.setattr(
        "core.pipeline.fidelity_review_batch",
        fake_fidelity_review,
    )

    _run(
        lambda: pipeline._run_fidelity_review(
            entries,
            context="test context",
            glossary="",
            session=object(),
        )
    )

    assert calls == [[1, 2], [1], [2], [2]]
    assert entries[0].lines[0] == "Recovered target."
    assert entries[0].status == "done"
    assert entries[0].stage_status == "fidelity_checked"
    assert "fidelity_review_failed" not in entries[0].quality_flags

    assert entries[1].lines[0] == "Original protected fallback."
    assert entries[1].status == "needs_review"
    assert entries[1].stage_status == "fidelity_failed_fallback"
    assert entries[1].quality_flags == ["fidelity_review_failed"]
    assert "permanent item failure" in entries[1].error_msg

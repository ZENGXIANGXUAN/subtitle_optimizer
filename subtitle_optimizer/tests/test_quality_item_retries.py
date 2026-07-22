from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models import SubtitleEntry
from core.pipeline import SubtitlePipeline
from core.quality import _parse_json
from core.quality_gate import evaluate_quality_gate
from core.srt_lint import finalize_entries
from core.workers import _translation_is_usable


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


def test_fidelity_final_failure_keeps_an_existing_usable_target_non_blocking(monkeypatch) -> None:
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
    assert entries[1].status == "done"
    assert entries[1].stage_status == "fidelity_skipped_with_flags"
    assert entries[1].quality_flags == ["fidelity_review_unapplied"]
    assert evaluate_quality_gate(entries).passed


def test_fidelity_final_failure_keeps_a_missing_translation_blocking(monkeypatch) -> None:
    entry = SubtitleEntry(
        index=1,
        start_time="00:00:01,000",
        end_time="00:00:01,900",
        lines=["A substantive untranslated source sentence."],
    )
    entry.original_lines = list(entry.lines)
    entry.source_indexes = [1]
    pipeline = SubtitlePipeline(
        client=object(),
        input_lang="English",
        output_lang="中文",
        batch_size=1,
    )

    async def fake_fidelity_review(*args, **kwargs):
        raise RuntimeError("simulated permanent item failure")

    monkeypatch.setattr(
        "core.pipeline.fidelity_review_batch",
        fake_fidelity_review,
    )

    _run(
        lambda: pipeline._run_fidelity_review(
            [entry],
            context="test context",
            glossary="",
            session=object(),
        )
    )

    assert entry.status == "needs_review"
    assert entry.stage_status == "fidelity_failed_fallback"
    assert entry.quality_flags == ["fidelity_review_failed"]
    assert not evaluate_quality_gate([entry]).passed


def test_fidelity_recovery_clears_stale_translation_failure_flags(monkeypatch) -> None:
    entry = SubtitleEntry(
        index=1,
        start_time="00:00:01,000",
        end_time="00:00:01,900",
        lines=["A substantive untranslated source sentence."],
        status="needs_review",
        stage_status="translation_failed_fallback",
        quality_flags=["translation_repair_failed"],
    )
    entry.original_lines = list(entry.lines)
    entry.source_indexes = [1]
    pipeline = SubtitlePipeline(
        client=object(),
        input_lang="English",
        output_lang="中文",
        batch_size=1,
    )

    async def fake_fidelity_review(batch, *args, **kwargs):
        return {batch[0].index: "一条完整的中文译文。"}

    monkeypatch.setattr(
        "core.pipeline.fidelity_review_batch",
        fake_fidelity_review,
    )

    _run(
        lambda: pipeline._run_fidelity_review(
            [entry],
            context="test context",
            glossary="",
            session=object(),
        )
    )

    assert entry.status == "done"
    assert entry.stage_status == "fidelity_checked"
    assert entry.quality_flags == []
    assert evaluate_quality_gate([entry]).passed


def test_nonverbal_source_identity_does_not_fail_bilingual_lint() -> None:
    entry = SubtitleEntry(
        index=1,
        start_time="00:00:01,000",
        end_time="00:00:01,900",
        lines=["1, 2, 3."],
        stage_status="source_identity_allowed",
        quality_flags=["source_identity_allowed"],
    )
    entry.optimized_lines = list(entry.lines)

    finalized, lint_report = finalize_entries(
        [entry],
        input_lang_code="en",
        output_lang_code="zh",
    )

    assert finalized[0].lines == ["1, 2, 3."]
    assert lint_report.incomplete_bilingual == []
    assert evaluate_quality_gate(finalized, lint_report).passed


def test_bilingual_nonverbal_targets_do_not_require_chinese_characters() -> None:
    entries = [
        SubtitleEntry(
            index=1,
            start_time="00:00:01,000",
            end_time="00:00:01,900",
            lines=["Lulu，Lulu，Lulu。", "Lulu, lulu, lulu, lulu, lulu."],
        ),
        SubtitleEntry(
            index=2,
            start_time="00:00:02,000",
            end_time="00:00:02,900",
            lines=["1、2、3。", "1, 2, 3."],
        ),
    ]
    for entry in entries:
        entry.optimized_lines = list(entry.lines)

    finalized, lint_report = finalize_entries(
        entries,
        input_lang_code="en",
        output_lang_code="zh",
    )

    assert [entry.lines for entry in finalized] == [
        ["Lulu，Lulu，Lulu。", "Lulu, lulu, lulu, lulu, lulu."],
        ["1、2、3。", "1, 2, 3."],
    ]
    assert lint_report.incomplete_bilingual == []
    assert evaluate_quality_gate(finalized, lint_report).passed


def test_lint_treats_first_content_line_as_the_configured_target_language() -> None:
    entry = SubtitleEntry(
        index=1,
        start_time="00:00:01,000",
        end_time="00:00:01,900",
        lines=["Bonjour tout le monde.", "Hello everyone."],
    )
    entry.optimized_lines = list(entry.lines)

    finalized, lint_report = finalize_entries(
        [entry],
        input_lang_code="en",
        output_lang_code="fr",
    )

    assert finalized[0].lines == ["Bonjour tout le monde.", "Hello everyone."]
    assert lint_report.incomplete_bilingual == []
    assert evaluate_quality_gate(finalized, lint_report).passed


def test_substantive_source_fallback_is_preserved_and_exportable() -> None:
    source = "A substantive source cue remains when the translation API returns nothing."
    entry = SubtitleEntry(
        index=1,
        start_time="00:00:01,000",
        end_time="00:00:01,900",
        lines=["stale target", source],
        status="needs_review",
        quality_flags=["translation_repair_failed"],
    )

    SubtitlePipeline._keep_source_fallback(entry, source)
    entry.optimized_lines = list(entry.lines)
    finalized, lint_report = finalize_entries(
        [entry],
        input_lang_code="en",
        output_lang_code="zh",
    )

    assert finalized[0].lines == [source]
    assert entry.status == "done"
    assert entry.stage_status == "translation_source_fallback"
    assert entry.error_msg == ""
    assert entry.quality_flags == ["source_fallback"]
    assert SubtitlePipeline._is_source_identity_allowed(entry)
    assert lint_report.incomplete_bilingual == []
    assert evaluate_quality_gate(finalized, lint_report).passed


def test_only_nonverbal_source_can_bypass_the_chinese_translation_gate() -> None:
    assert _translation_is_usable("", "1, 2, 3.", "中文")
    assert _translation_is_usable("Lulu", "Lulu", "中文")
    assert _translation_is_usable("", "Lulu, lulu, lulu, lulu, lulu.", "中文")
    assert not _translation_is_usable(
        "Okay, this is meaningful speech.",
        "Okay, this is meaningful speech.",
        "中文",
    )
    assert not _translation_is_usable("", "Okay, this is meaningful speech.", "中文")


def test_concise_chinese_translation_is_not_rejected_by_length_ratio() -> None:
    # These are the five model responses from the 2026-07-21 failed run.  They
    # were valid non-empty outputs, but the previous word-to-character ratio
    # discarded all of them and made the pipeline fall back to English source.
    assert _translation_is_usable(
        "5月初市场出现大幅下跌",
        "And in the beginning of May, what you can see is that the market had a strong move down.",
        "中文",
    )
    assert _translation_is_usable(
        "Andrea说，好区域。确实是个好区域",
        "Nice zone, Andrea says. Nice zone, Andrea says. Yeah, it's a nice area. Yeah, it's a nice area.",
        "中文",
    )
    assert _translation_is_usable(
        "我知道目标可能定得太高",
        "I know that maybe it's too much in target that I want to put on this one, but this could be",
        "中文",
    )
    assert _translation_is_usable("Lulu，Lulu", "Lulu, lulu, lulu, lulu, lulu.", "中文")
    assert _translation_is_usable("1、2、3。", "1, 2, 3.", "中文")
    assert not _translation_is_usable(
        "The market had a strong move down.",
        "And in the beginning of May, what you can see is that the market had a strong move down.",
        "中文",
    )


def test_quality_parser_keeps_complete_json_before_unsolicited_prose() -> None:
    assert _parse_json('[{"id": 1, "tgt": "完整译文"}]\n说明：已完成') == [
        {"id": 1, "tgt": "完整译文"}
    ]
    assert _parse_json('[{"id": 1, "tgt": "完整译文"}') == [
        {"id": 1, "tgt": "完整译文"}
    ]


def test_optimize_failure_keeps_a_usable_translation_non_blocking(monkeypatch) -> None:
    entry = _entry(1, "Source sentence.", "Existing usable target.")
    entry.status = "done"
    entry.stage_status = "translated"
    pipeline = SubtitlePipeline(
        client=object(),
        input_lang="English",
        output_lang="English",
        batch_size=1,
    )

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def no_fidelity(*args, **kwargs) -> None:
        return None

    async def failed_optimize(*args, **kwargs):
        raise RuntimeError("simulated malformed optimize JSON")

    async def no_consistency(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(pipeline, "_create_session", fake_session)
    monkeypatch.setattr(pipeline, "_run_fidelity_review", no_fidelity)
    monkeypatch.setattr(pipeline, "_run_consistency_review", no_consistency)
    monkeypatch.setattr("core.pipeline._optimize_batch_impl", failed_optimize)
    monkeypatch.setattr("core.pipeline.reflow_semantic_entries", lambda entries, **kwargs: entries)
    monkeypatch.setattr("core.pipeline.source_integrity_errors", lambda *args, **kwargs: [])

    result = asyncio.run(pipeline.optimize([entry], context="", glossary=""))

    assert result[0].status == "done"
    assert result[0].stage_status == "optimize_skipped_with_flags"
    assert result[0].quality_flags == ["optimize_unapplied"]
    assert evaluate_quality_gate(result, pipeline.last_lint_report).passed

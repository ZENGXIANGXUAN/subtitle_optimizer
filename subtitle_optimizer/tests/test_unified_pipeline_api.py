from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models import SubtitleEntry
from core.pipeline import SubtitlePipeline
from core.task_models import PipelineRunResult
from core.workers import PipelineExecutionWorker


def _entry(index: int, target: str = "target", source: str = "source") -> SubtitleEntry:
    entry = SubtitleEntry(
        index=index,
        start_time=f"00:00:{index:02d},000",
        end_time=f"00:00:{index:02d},900",
        lines=[f"{target} {index}", f"{source} {index}"],
    )
    entry.original_lines = [f"{source} {index}"]
    entry.source_indexes = [index]
    entry.optimized_lines = list(entry.lines)
    return entry


def test_pipeline_honors_requested_concurrency_and_clamps_to_max() -> None:
    pipeline = SubtitlePipeline(client=object(), concurrency=70, batch_size=5)
    assert pipeline.concurrency == 70

    pipeline_over_limit = SubtitlePipeline(client=object(), concurrency=150, batch_size=5)
    assert pipeline_over_limit.concurrency == 100


def test_translate_reads_snapshot_while_out_of_order_batches_write_live_entries(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        entries = [
            SubtitleEntry(
                index=index,
                start_time=f"00:00:0{index},000",
                end_time=f"00:00:0{index},900",
                lines=[f"Source sentence {index}."],
            )
            for index in range(1, 4)
        ]
        release_first_batch = asyncio.Event()
        observed: dict[str, object] = {}
        targets = {
            1: "第一条完整中文翻译",
            2: "第二条完整中文翻译",
            3: "第三条完整中文翻译",
        }

        async def fake_translate_batch(
            batch,
            context,
            glossary,
            client,
            session,
            input_lang,
            output_lang,
            prev_ctx,
            next_ctx,
            *,
            include_context_targets,
        ):
            entry_id = batch[0].index
            observed.setdefault("detached", []).append(
                batch[0] is not entries[entry_id - 1]
            )
            observed.setdefault("target_context_flags", []).append(
                include_context_targets
            )

            if entry_id == 1:
                await release_first_batch.wait()
            elif entry_id == 3:
                # Batch 2 has already completed and written to the live list.
                # Batch 3 must still see the original pre-translation snapshot.
                observed["live_previous"] = list(entries[1].lines)
                previous_snapshot = next(
                    item for item in prev_ctx if item.index == 2
                )
                observed["snapshot_previous"] = list(previous_snapshot.lines)
                release_first_batch.set()

            return {entry_id: targets[entry_id]}, []

        monkeypatch.setattr(
            "core.pipeline._translate_batch_with_repair",
            fake_translate_batch,
        )

        pipeline = SubtitlePipeline(
            client=object(),
            input_lang="English",
            output_lang="中文",
            concurrency=2,
            batch_size=1,
        )
        result = await asyncio.wait_for(
            pipeline.translate(entries, context="known context", glossary=""),
            timeout=2,
        )

        assert result is entries
        assert observed["live_previous"] == [
            targets[2],
            "Source sentence 2.",
        ]
        assert observed["snapshot_previous"] == ["Source sentence 2."]
        assert observed["detached"] == [True, True, True]
        assert observed["target_context_flags"] == [False, False, False]
        assert [entry.lines[0] for entry in entries] == [
            targets[1],
            targets[2],
            targets[3],
        ]

    asyncio.run(scenario())


class _RecordingPipeline(SubtitlePipeline):
    """Network-free pipeline double that exercises only execute orchestration."""

    def __init__(self, *, final_entries: list[SubtitleEntry], lint_report=None) -> None:
        super().__init__(
            client=object(),
            input_lang="English",
            output_lang="English",
            concurrency=2,
            batch_size=6,
        )
        self.final_entries = final_entries
        self.final_lint_report = lint_report
        self.calls: list[tuple[str, int]] = []

    async def segment(self, entries, context, glossary, **kwargs):
        self.calls.append(("segment", self.batch_size))
        kwargs["on_progress"](1, len(entries))
        return entries

    async def translate(self, entries, context, glossary, **kwargs):
        self.calls.append(("translate", self.batch_size))
        kwargs["on_progress"](len(entries), len(entries))
        return entries

    async def optimize(self, entries, context, glossary, **kwargs):
        self.calls.append(("optimize", self.batch_size))
        kwargs["on_progress"](len(self.final_entries), len(self.final_entries))
        self.last_lint_report = self.final_lint_report
        return self.final_entries


def test_execute_has_one_stage_contract_and_returns_the_complete_final_result() -> None:
    initial_entries = [_entry(1), _entry(2)]
    # Deliberately return a different-sized list.  This catches adapters that
    # mutate the old list by position instead of accepting the authoritative
    # final list produced by optimize/lint/reflow.
    final_entries = [_entry(11), _entry(12), _entry(13)]
    lint_issue = SimpleNamespace(index=12, message="missing target line")
    lint_report = SimpleNamespace(
        incomplete_bilingual=[lint_issue],
        large_overlaps=[],
        high_cps=[],
        bad_cps=[],
    )
    pipeline = _RecordingPipeline(
        final_entries=final_entries,
        lint_report=lint_report,
    )

    events: list[tuple] = []
    logs: list[str] = []
    result = asyncio.run(
        pipeline.execute(
            initial_entries,
            context="known context",
            glossary="known glossary",
            segment_batch_size=19,
            on_stage=lambda stage: events.append(("stage", stage)),
            on_progress=lambda stage, current, total: events.append(
                ("progress", stage, current, total)
            ),
            on_snapshot=lambda stage, entries, optimized: events.append(
                ("snapshot", stage, entries, optimized)
            ),
            on_log=logs.append,
        )
    )

    assert pipeline.calls == [
        ("segment", 19),
        ("translate", 6),
        ("optimize", 6),
    ]
    assert pipeline.batch_size == 6
    assert [event[:2] for event in events] == [
        ("stage", "segment"),
        ("progress", "segment"),
        ("snapshot", "segment"),
        ("stage", "translate"),
        ("progress", "translate"),
        ("snapshot", "translate"),
        ("stage", "optimize"),
        ("progress", "optimize"),
        ("snapshot", "optimize"),
    ]
    assert events[-1] == ("snapshot", "optimize", final_entries, True)

    assert isinstance(result, PipelineRunResult)
    assert result.entries is final_entries
    assert len(result.entries) == 3
    assert result.lint_report is lint_report
    assert not result.quality_report.passed
    assert [issue.kind for issue in result.quality_report.critical_issues] == [
        "incomplete_bilingual"
    ]
    assert logs == ["  ⚠️ 字幕质检提示：1 个关键项（不阻止导出）"]


def test_execute_restores_translation_batch_size_when_segmentation_fails() -> None:
    class _FailingSegmentPipeline(_RecordingPipeline):
        async def segment(self, entries, context, glossary, **kwargs):
            self.calls.append(("segment", self.batch_size))
            raise RuntimeError("segmentation failed")

    pipeline = _FailingSegmentPipeline(final_entries=[])

    try:
        asyncio.run(
            pipeline.execute(
                [_entry(1)],
                context="",
                glossary="",
                segment_batch_size=23,
            )
        )
    except RuntimeError as exc:
        assert str(exc) == "segmentation failed"
    else:
        raise AssertionError("execute() should propagate stage failures")

    assert pipeline.calls == [("segment", 23)]
    assert pipeline.batch_size == 6


def test_pipeline_execution_worker_forwards_callbacks_and_full_result(monkeypatch) -> None:
    initial_entries = [_entry(1), _entry(2)]
    final_entries = [_entry(21), _entry(22), _entry(23)]
    expected_result = PipelineRunResult(entries=final_entries)
    captured: dict[str, object] = {}

    class _FakePipeline:
        def __init__(self, client, input_lang, output_lang, concurrency, batch_size):
            captured["constructor"] = (
                client,
                input_lang,
                output_lang,
                concurrency,
                batch_size,
            )

        async def execute(self, entries, context, glossary, **kwargs):
            captured["execute"] = (
                entries,
                context,
                glossary,
                kwargs["segment_batch_size"],
            )
            kwargs["on_stage"]("segment")
            kwargs["on_progress"]("segment", 2, 2)
            kwargs["on_snapshot"]("optimize", final_entries, True)
            kwargs["on_log"]("quality gate checked")
            assert not kwargs["is_cancelled"]()
            return expected_result

    monkeypatch.setattr("core.pipeline.SubtitlePipeline", _FakePipeline)

    client = object()
    worker = PipelineExecutionWorker(
        client=client,
        entries=initial_entries,
        context="worker context",
        glossary="worker glossary",
        concurrency=3,
        batch_size=7,
        segment_batch_size=17,
        input_lang="English",
        output_lang="English",
    )
    started: list[str] = []
    progress: list[tuple[str, int, int]] = []
    snapshots: list[tuple[str, list[SubtitleEntry], bool]] = []
    logs: list[str] = []
    finished: list[PipelineRunResult] = []
    errors: list[str] = []
    worker.stage_started.connect(started.append)
    worker.stage_progress.connect(
        lambda stage, current, total: progress.append((stage, current, total))
    )
    worker.stage_snapshot.connect(
        lambda stage, entries, optimized: snapshots.append(
            (stage, entries, optimized)
        )
    )
    worker.log.connect(logs.append)
    worker.finished.connect(finished.append)
    worker.error.connect(errors.append)

    asyncio.run(worker._run())

    assert captured["constructor"] == (
        client,
        "English",
        "English",
        3,
        7,
    )
    assert captured["execute"] == (
        initial_entries,
        "worker context",
        "worker glossary",
        17,
    )
    assert started == ["segment"]
    assert progress == [("segment", 2, 2)]
    assert snapshots == [("optimize", final_entries, True)]
    assert logs == ["quality gate checked"]
    assert errors == []
    assert finished == [expected_result]
    assert finished[0].entries is final_entries
    assert len(finished[0].entries) == 3

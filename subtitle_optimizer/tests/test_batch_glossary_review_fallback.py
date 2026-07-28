from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.batch_glossary import (
    BatchGlossarySummary,
    GlossaryCandidate,
    GlossaryFileFailure,
)
from core.workers import BatchGlossaryWorker


def _summary(
    *,
    session_count: int,
    candidate_count: int,
) -> BatchGlossarySummary:
    return BatchGlossarySummary(
        candidates=[
            GlossaryCandidate(
                source=f"candidate-{index:02d}",
                target=f"候选译文-{index:02d}",
                files=(f"lesson-{index:02d}.srt",),
                status="译法冲突",
            )
            for index in range(1, candidate_count + 1)
        ],
        skipped_existing=["already-fixed"],
        failures=[],
        session_glossary="\n".join(
            f"session-{index:02d}->临时译文-{index:02d}"
            for index in range(1, session_count + 1)
        ),
    )


def _worker(
    client: object | None = None,
    concurrency: int = 2,
) -> BatchGlossaryWorker:
    return BatchGlossaryWorker(
        client=client,
        files=[],
        concurrency=concurrency,
        fixed_glossary="existing->已有译法",
        input_lang="English",
        output_lang="中文",
    )


class _FakeClientSession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def _disable_debug_request_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "mkdir", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(Path, "glob", lambda self, pattern: [])
    monkeypatch.setattr(Path, "write_text", lambda self, *args, **kwargs: 0)


def test_single_batch_review_failure_returns_original_summary_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _summary(session_count=12, candidate_count=4)
    worker = _worker()
    logs: list[str] = []
    worker.log.connect(logs.append)

    async def fail_review(*args, **kwargs):
        raise RuntimeError("502 All Retries Failed")

    monkeypatch.setattr(worker, "_review_single_batch", fail_review)

    reviewed = asyncio.run(worker._review_candidates_with_ai(summary))

    assert reviewed == summary
    assert reviewed.session_glossary == summary.session_glossary
    assert reviewed.candidates == summary.candidates
    assert reviewed.fixed_glossary == ""
    assert any(
        "AI 术语自审暂时不可用" in message and "继续" in message
        for message in logs
    )


def test_single_batch_log_uses_actual_session_and_candidate_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _summary(session_count=12, candidate_count=4)

    class FakeClient:
        async def chat(self, messages, session, extra_payload=None):
            return "{}"

    worker = _worker(FakeClient())
    logs: list[str] = []
    worker.log.connect(logs.append)
    monkeypatch.setattr("core.workers.aiohttp.ClientSession", _FakeClientSession)
    monkeypatch.setattr(
        worker,
        "_parse_ai_review_result",
        lambda raw, original_summary: original_summary,
    )

    reviewed = asyncio.run(worker._review_single_batch(summary))

    assert reviewed == summary
    assert any(
        "自审单批：session 12 + candidates 4" in message
        for message in logs
    )


def test_review_cancellation_is_not_downgraded_to_an_ordinary_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _summary(session_count=12, candidate_count=4)
    worker = _worker()

    async def cancel_review(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_review_single_batch", cancel_review)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker._review_candidates_with_ai(summary))


def test_multi_batch_failure_preserves_raw_session_and_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 31 session terms and 11 candidates form two review batches (30+10, 1+1).
    summary = _summary(session_count=31, candidate_count=11)
    failed_candidate = summary.candidates[-1]
    failed_session_line = summary.session_glossary.splitlines()[-1]
    worker = _worker()
    logs: list[str] = []
    worker.log.connect(logs.append)

    async def review_batch(
        batch_summary: BatchGlossarySummary,
        session_limit: int | None = None,
        candidates_limit: int | None = None,
    ) -> BatchGlossarySummary:
        if session_limit == 1 and candidates_limit == 1:
            raise RuntimeError("simulated 502")
        return BatchGlossarySummary(
            candidates=[],
            skipped_existing=batch_summary.skipped_existing,
            failures=batch_summary.failures,
            session_glossary="reviewed-session->已审核临时译法",
            fixed_glossary="approved-fixed->已审核固定译法",
        )

    monkeypatch.setattr(worker, "_review_single_batch", review_batch)

    reviewed = asyncio.run(worker._review_candidates_with_ai(summary))

    # Only a successful AI review may promote terms into fixed_glossary.
    assert reviewed.fixed_glossary == "approved-fixed->已审核固定译法"
    assert reviewed.session_glossary.splitlines() == [
        "reviewed-session->已审核临时译法",
        failed_session_line,
    ]
    assert reviewed.candidates == [failed_candidate]
    assert reviewed.skipped_existing == summary.skipped_existing
    assert reviewed.failures == summary.failures
    assert any("失败 1 批" in message for message in logs)


def test_local_proxy_unifier_runtime_failure_does_not_send_a_second_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _summary(session_count=1, candidate_count=1)

    class FailingLocalProxyClient:
        base_url = "http://127.0.0.1:8000/v1"
        is_local_proxy = True

        def __init__(self) -> None:
            self.calls: list[object] = []

        async def chat(self, messages, session, extra_payload=None):
            self.calls.append(extra_payload)
            raise RuntimeError("502 All Retries Failed")

    client = FailingLocalProxyClient()
    worker = _worker(client)
    _disable_debug_request_writes(monkeypatch)
    monkeypatch.setattr("core.workers.aiohttp.ClientSession", _FakeClientSession)

    with pytest.raises(RuntimeError, match="502 All Retries Failed"):
        asyncio.run(
            worker._unify_single_batch(summary, summary.candidates)
        )

    assert len(client.calls) == 1
    assert client.calls[0] == {"reasoning_effort": "none"}


def test_remote_unifier_keeps_reasoning_compatibility_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _summary(session_count=1, candidate_count=1)

    class RemoteClient:
        is_local_proxy = False

        def __init__(self) -> None:
            self.calls: list[object] = []

        async def chat(self, messages, session, extra_payload=None):
            self.calls.append(extra_payload)
            if len(self.calls) == 1:
                raise RuntimeError("reasoning_effort is unsupported")
            return "plain response"

    client = RemoteClient()
    worker = _worker(client)
    logs: list[str] = []
    worker.log.connect(logs.append)
    _disable_debug_request_writes(monkeypatch)
    monkeypatch.setattr("core.workers.aiohttp.ClientSession", _FakeClientSession)
    monkeypatch.setattr(
        worker,
        "_parse_global_unifier_result",
        lambda raw, original_summary: original_summary,
    )

    unified = asyncio.run(
        worker._unify_single_batch(summary, summary.candidates)
    )

    assert unified == summary
    assert len(client.calls) == 2
    assert client.calls[0] == {"reasoning_effort": "none"}
    assert client.calls[1] is None
    assert any("远端统一器思考模式不可用" in message for message in logs)


def test_local_proxy_unifier_cancellation_propagates_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _summary(session_count=1, candidate_count=1)

    class CancelledLocalProxyClient:
        base_url = "http://localhost:8000/v1"
        is_local_proxy = True

        def __init__(self) -> None:
            self.call_count = 0

        async def chat(self, messages, session, extra_payload=None):
            self.call_count += 1
            raise asyncio.CancelledError

    client = CancelledLocalProxyClient()
    worker = _worker(client)
    _disable_debug_request_writes(monkeypatch)
    monkeypatch.setattr("core.workers.aiohttp.ClientSession", _FakeClientSession)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            worker._unify_single_batch(summary, summary.candidates)
        )

    assert client.call_count == 1


def test_global_unifier_single_batch_timeout_splits_and_merges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _summary(session_count=0, candidate_count=4)
    worker = _worker()
    logs: list[str] = []
    calls: list[list[str]] = []
    worker.log.connect(logs.append)

    async def unify_batch(
        batch_summary: BatchGlossarySummary,
        batch_candidates: list[GlossaryCandidate],
    ) -> BatchGlossarySummary:
        calls.append([candidate.source for candidate in batch_candidates])
        if len(batch_candidates) > 2:
            raise RuntimeError("API错误 504: ProxyRequestBudgetExceeded")
        return BatchGlossarySummary(
            candidates=[],
            skipped_existing=batch_summary.skipped_existing,
            failures=[],
            session_glossary="\n".join(
                f"{candidate.source}->{candidate.target}"
                for candidate in batch_candidates
            ),
        )

    monkeypatch.setattr(worker, "_unify_single_batch", unify_batch)
    monkeypatch.setattr(
        "core.workers.batch_glossary_unifier_candidate_limit",
        lambda: 40,
    )

    unified = asyncio.run(worker._unify_candidates_globally(summary))

    assert [len(call) for call in calls] == [4, 2, 2]
    assert unified.session_glossary.splitlines() == [
        f"{candidate.source}->{candidate.target}"
        for candidate in summary.candidates
    ]
    assert unified.candidates == []
    assert unified.failures == []
    assert any("自动拆分为 2 + 2 条重试" in message for message in logs)
    assert any("最终降级 0 批" in message for message in logs)


def test_global_unifier_partitions_93_candidates_without_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _summary(session_count=0, candidate_count=93)
    worker = _worker()
    batch_sizes: list[int] = []

    async def unify_batch(
        batch_summary: BatchGlossarySummary,
        batch_candidates: list[GlossaryCandidate],
    ) -> BatchGlossarySummary:
        batch_sizes.append(len(batch_candidates))
        return BatchGlossarySummary(
            candidates=[],
            skipped_existing=batch_summary.skipped_existing,
            failures=[],
            session_glossary="\n".join(
                f"{candidate.source}->{candidate.target}"
                for candidate in batch_candidates
            ),
        )

    monkeypatch.setattr(worker, "_unify_single_batch", unify_batch)

    unified = asyncio.run(worker._unify_candidates_globally(summary))

    assert batch_sizes == [20, 20, 20, 20, 13]
    assert len(unified.session_glossary.splitlines()) == 93
    assert unified.candidates == []
    assert unified.failures == []


def test_global_unifier_limits_simultaneous_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _summary(session_count=0, candidate_count=6)
    worker = _worker(concurrency=16)
    active_requests = 0
    peak_requests = 0

    async def unify_batch(
        batch_summary: BatchGlossarySummary,
        batch_candidates: list[GlossaryCandidate],
    ) -> BatchGlossarySummary:
        nonlocal active_requests, peak_requests
        active_requests += 1
        peak_requests = max(peak_requests, active_requests)
        await asyncio.sleep(0.01)
        active_requests -= 1
        return BatchGlossarySummary(
            candidates=[],
            skipped_existing=batch_summary.skipped_existing,
            failures=[],
            session_glossary="\n".join(
                f"{candidate.source}->{candidate.target}"
                for candidate in batch_candidates
            ),
        )

    monkeypatch.setattr(worker, "_unify_single_batch", unify_batch)
    monkeypatch.setattr(
        "core.workers.batch_glossary_unifier_candidate_limit",
        lambda: 2,
    )
    monkeypatch.setattr(
        "core.workers.batch_glossary_unifier_concurrency_limit",
        lambda: 2,
        raising=False,
    )

    unified = asyncio.run(worker._unify_candidates_globally(summary))

    assert peak_requests == 2
    assert len(unified.session_glossary.splitlines()) == 6


def test_global_unifier_unsplittable_failure_preserves_conflicts_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conflicts = [
        GlossaryCandidate("order flow", "订单流", ("a.srt",), "译法冲突"),
        GlossaryCandidate("order flow", "委托流", ("b.srt",), "译法冲突"),
    ]
    existing_failure = GlossaryFileFailure("bad.srt", "无法解析字幕文件")
    summary = BatchGlossarySummary(
        candidates=conflicts,
        skipped_existing=["already-fixed"],
        failures=[existing_failure],
        session_glossary="",
    )
    worker = _worker()
    logs: list[str] = []
    worker.log.connect(logs.append)

    async def fail_unifier(*args, **kwargs):
        raise RuntimeError("API错误 504: ProxyRequestBudgetExceeded")

    monkeypatch.setattr(worker, "_unify_single_batch", fail_unifier)

    unified = asyncio.run(worker._unify_candidates_globally(summary))

    assert unified.session_glossary == ""
    assert unified.candidates == conflicts
    assert unified.skipped_existing == summary.skipped_existing
    assert unified.failures == [existing_failure]
    assert any("已保留本地需确认候选并继续批量处理" in message for message in logs)
    assert any("最终降级 1 批" in message for message in logs)


def test_global_unifier_multi_batch_partial_failure_uses_same_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    successful = [
        GlossaryCandidate("delta", "德尔塔", ("a.srt",), "新词"),
        GlossaryCandidate("gamma", "伽马", ("a.srt",), "新词"),
    ]
    conflicts = [
        GlossaryCandidate("TPO", "时间价格机会", ("a.srt",), "译法冲突"),
        GlossaryCandidate("TPO", "时间价格成交量印记", ("b.srt",), "译法冲突"),
    ]
    summary = BatchGlossarySummary(
        candidates=successful + conflicts,
        skipped_existing=[],
        failures=[],
        session_glossary="",
    )
    worker = _worker()

    async def unify_batch(
        batch_summary: BatchGlossarySummary,
        batch_candidates: list[GlossaryCandidate],
    ) -> BatchGlossarySummary:
        if batch_candidates[0].source == "TPO":
            raise RuntimeError("API错误 401: invalid API key")
        return BatchGlossarySummary(
            candidates=[],
            skipped_existing=batch_summary.skipped_existing,
            failures=[],
            session_glossary="\n".join(
                f"{candidate.source}->{candidate.target}"
                for candidate in batch_candidates
            ),
        )

    monkeypatch.setattr(worker, "_unify_single_batch", unify_batch)
    monkeypatch.setattr(
        "core.workers.batch_glossary_unifier_candidate_limit",
        lambda: 2,
    )

    unified = asyncio.run(worker._unify_candidates_globally(summary))

    assert unified.session_glossary.splitlines() == [
        "delta->德尔塔",
        "gamma->伽马",
    ]
    assert unified.candidates == conflicts
    assert unified.failures == []

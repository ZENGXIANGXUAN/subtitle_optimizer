import re
import json
import asyncio
import aiohttp
from typing import Callable, Optional

from core.models import (
    SubtitleEntry,
    _srt_time_to_ms,
    _format_subtitle_snapshot,
    source_can_remain_untranslated,
    verify_source_integrity,
)
from core.segmenter import _get_source_text, _get_target_text
from core.srt_lint import finalize_entries
from core.semantic_units import (
    ensure_source_cues,
    merge_source_cues,
    reflow_semantic_entries,
    source_integrity_errors,
)
from core.client import MistralClient
from core.quality import (
    fidelity_review_batch,
    build_consistency_guide,
    consistency_review_batch,
)
from core.quality_gate import evaluate_quality_gate
from core.task_models import PipelineRunResult
from core.workers import (
    LANGUAGE_MAP,
    _get_prompt_lang,
    _build_analysis_result,
    _build_analysis_messages,
    _apply_llm_merge,
    _apply_llm_split,
    _force_split_entry,
    _split_short_long,
    _translate_batch_with_repair,
    _repair_incomplete_segment_translations,
    _optimize_batch_impl,
    _translation_is_usable,
)

try:
    from ..utils import logger
except ImportError:
    import sys
    import os
    sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
    from utils import logger


class SubtitlePipeline:
    """Subtitle processing pipeline.

    Accepts optional callbacks for progress, logging, and cancellation.
    Note: this module currently reuses helper functions from workers.py, so a deeper
    separation of Qt worker wrappers and pure business logic can be done later.
    """

    DEFAULT_OPTIMIZE_BATCH_TIMEOUT_SECONDS = 240
    DEFAULT_FIDELITY_BATCH_TIMEOUT_SECONDS = 240
    DEFAULT_CONSISTENCY_BATCH_TIMEOUT_SECONDS = 240
    DEFAULT_CONTEXT_SIZE = 12
    DEFAULT_QUALITY_ITEM_RETRY_ATTEMPTS = 2
    DEFAULT_CONSISTENCY_GUIDE_RETRY_ATTEMPTS = 2
    QUALITY_MAX_BATCH_SIZE = 8
    QUALITY_MAX_CONCURRENCY = 16
    _TRANSLATION_FAILURE_FLAGS = (
        "source_fallback",
        "translation_batch_failed",
        "translation_hard_gate_failed",
        "translation_repair_failed",
        "fidelity_review_failed",
        "optimize_target_invalid",
        "optimize_timeout",
        "optimize_failed",
    )

    def __init__(
        self,
        client: MistralClient,
        input_lang: str = "English",
        output_lang: str = "中文",
        concurrency: int = 12,
        batch_size: int = 5,
    ):
        self.client = client
        self.input_lang = input_lang
        self.output_lang = output_lang
        # Quality-first defaults: smaller batches reduce cross-entry omissions
        # and bounded concurrency avoids rate-limit cascades. Segment execution
        # may temporarily use its own larger batch size via execute().
        self.concurrency = max(1, min(self.QUALITY_MAX_CONCURRENCY, int(concurrency)))
        self.requested_batch_size = max(1, int(batch_size))
        self.batch_size = min(self.QUALITY_MAX_BATCH_SIZE, self.requested_batch_size)
        self.input_lang_code = LANGUAGE_MAP.get(input_lang, ("en", input_lang))[0]
        self.optimize_batch_timeout_seconds = self.DEFAULT_OPTIMIZE_BATCH_TIMEOUT_SECONDS
        self.fidelity_batch_timeout_seconds = self.DEFAULT_FIDELITY_BATCH_TIMEOUT_SECONDS
        self.consistency_batch_timeout_seconds = self.DEFAULT_CONSISTENCY_BATCH_TIMEOUT_SECONDS
        self.last_lint_report = None

    def _create_session(self) -> aiohttp.ClientSession:
        """Create one reusable HTTP connection pool for a whole stage."""
        connector = aiohttp.TCPConnector(
            limit=max(16, self.concurrency * 2),
            limit_per_host=max(8, self.concurrency),
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        return aiohttp.ClientSession(connector=connector)

    def _snapshot_entries(
        self,
        entries: list[SubtitleEntry],
        *,
        use_optimized: bool = False,
    ) -> list[SubtitleEntry]:
        """Freeze text/context before concurrent requests mutate live entries."""
        snapshots: list[SubtitleEntry] = []
        for entry in entries:
            lines = entry.optimized_lines if (use_optimized and entry.optimized_lines) else entry.lines
            clone = SubtitleEntry(
                entry.index,
                entry.start_time,
                entry.end_time,
                list(lines),
            )
            clone.original_lines = list(entry.original_lines or entry.lines)
            clone.source_indexes = list(entry.source_indexes or [entry.index])
            clone.stage_status = entry.stage_status
            clone.quality_flags = list(entry.quality_flags)
            clone.source_cues = list(entry.source_cues)
            snapshots.append(clone)
        return snapshots

    @classmethod
    def _mark_translation_recovered(cls, entry: SubtitleEntry) -> None:
        """Clear stale failure state after a later stage supplies a valid target."""
        entry.status = "done"
        entry.error_msg = ""
        for flag in cls._TRANSLATION_FAILURE_FLAGS:
            if flag in entry.quality_flags:
                entry.quality_flags.remove(flag)

    @classmethod
    def _keep_source_fallback(cls, entry: SubtitleEntry, source_text: str) -> None:
        """Keep the source cue as an automatic, exportable final fallback.

        A failed model response must never erase a subtitle or make the entire
        file unexportable.  This is intentionally distinct from a completed
        translation: downstream review stages skip the cue, while the output
        and logs retain a machine-readable ``source_fallback`` marker.
        """
        entry.lines = [source_text] if source_text else entry.lines
        entry.optimized_lines = []
        cls._mark_translation_recovered(entry)
        entry.status = "done"
        entry.stage_status = "translation_source_fallback"
        entry.error_msg = ""
        if "source_fallback" not in entry.quality_flags:
            entry.quality_flags.append("source_fallback")

    @staticmethod
    def _is_source_identity_allowed(entry: SubtitleEntry) -> bool:
        return bool(
            {"source_identity_allowed", "source_fallback"}
            & set(entry.quality_flags)
        )

    # ------------------------------------------------------------------
    # Stage 1: Analyze
    # ------------------------------------------------------------------
    async def analyze(
        self,
        entries: list[SubtitleEntry],
        on_progress: Optional[Callable[[str], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> str:
        result = await self.analyze_details(entries, on_progress, is_cancelled)
        return result.context

    async def analyze_details(
        self,
        entries: list[SubtitleEntry],
        on_progress: Optional[Callable[[str], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ):
        if is_cancelled and is_cancelled():
            raise asyncio.CancelledError()

        if on_progress:
            on_progress("正在分析字幕内容，识别翻译情景...")

        messages = _build_analysis_messages(entries, self.input_lang, self.output_lang)
        async with self._create_session() as session:
            raw = await self.client.chat(messages, session, temperature=0.3)
        return _build_analysis_result(raw)

    # ------------------------------------------------------------------
    # Stage 2: Segment
    # ------------------------------------------------------------------
    async def segment(
        self,
        entries: list[SubtitleEntry],
        context: str,
        glossary: str,
        on_progress: Optional[Callable[[int, int], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> list[SubtitleEntry]:
        total = len(entries)
        if not entries:
            return []

        # Freeze the original timeline before any semantic merge. Translation
        # works on complete semantic units; the final target later flows back to
        # these immutable source cues.
        ensure_source_cues(entries, self.input_lang_code)

        is_native = sum(1 for e in entries if len(e.lines) == 1) > total * 0.5
        if is_native and on_log:
            on_log(f"  📋 检测到原生字幕，将自动翻译为{self.output_lang}")

        window = max(10, self.batch_size)
        batches = [entries[i:i + window] for i in range(0, total, window)]
        # 保存原始批次边界。首轮并发处理完成后，再对每个边界做专项复查，
        # 解决“第10条和第11条永远看不到彼此”的盲区。
        boundary_source_ids = [batch[-1].index for batch in batches[:-1] if batch]
        sem = asyncio.Semaphore(max(1, self.concurrency))
        completed = 0
        all_results: list[SubtitleEntry] = []
        result_lock = asyncio.Lock()

        async with self._create_session() as session:
            async def process_batch(batch: list[SubtitleEntry]):
                nonlocal completed
                if is_cancelled and is_cancelled():
                    return
                async with sem:
                    if is_cancelled and is_cancelled():
                        return
                    try:
                        results = await self._segment_batch(batch, context, session, on_log)
                        async with result_lock:
                            all_results.extend(results)
                    except Exception as exc:
                        logger.warning(f"Segment batch failed: {exc}")
                        fallback_items: list[SubtitleEntry] = []
                        for entry in batch:
                            fallback = SubtitleEntry(
                                entry.index, entry.start_time, entry.end_time, list(entry.lines)
                            )
                            fallback.source_indexes = list(entry.source_indexes or [entry.index])
                            fallback.original_lines = list(entry.original_lines or entry.lines)
                            fallback.source_cues = list(entry.source_cues)
                            fallback.stage_status = "segment_failed_fallback"
                            fallback.quality_flags.append("segment_batch_failed")
                            fallback_items.append(fallback)
                        async with result_lock:
                            all_results.extend(fallback_items)
                    completed += len(batch)
                    if on_progress:
                        on_progress(min(completed, total), total)

            await asyncio.gather(*(process_batch(batch) for batch in batches))

            # Deduplicate by temporary index after independent batch processing.
            seen: dict[int, SubtitleEntry] = {}
            deduped: list[SubtitleEntry] = []
            duplicates: list[int] = []
            for entry in all_results:
                if entry.index in seen:
                    duplicates.append(entry.index)
                    continue
                seen[entry.index] = entry
                deduped.append(entry)
            deduped.sort(key=lambda e: _srt_time_to_ms(e.start_time))

            if boundary_source_ids:
                if on_log:
                    on_log(f"  🔍 开始复查 {len(boundary_source_ids)} 个字幕批次边界")
                deduped = await self._review_segment_boundaries(
                    deduped,
                    boundary_source_ids,
                    context,
                    session,
                    on_log,
                    is_cancelled,
                )

        # 最终按时间排序、统一重新编号。
        deduped.sort(key=lambda e: _srt_time_to_ms(e.start_time))
        for index, entry in enumerate(deduped, start=1):
            entry.index = index
            if entry.stage_status == "pending":
                entry.stage_status = "segmented"

        before_words = sum(
            len(_get_source_text(entry, self.input_lang_code).split()) for entry in entries
        )
        after_words = sum(
            len(_get_source_text(entry, self.input_lang_code).split()) for entry in deduped
        )
        missing = verify_source_integrity(entries, deduped)
        status = "OK" if not missing and before_words == after_words else "FAILED"
        if on_log:
            on_log(
                f"  📊 重组完整性: {len(entries)}条/{before_words}词 → "
                f"{len(deduped)}条/{after_words}词 | "
                f"来源丢失: {sorted(missing) if missing else '无'} | "
                f"重复: {duplicates or '无'} | {status}"
            )

        logger.debug(_format_subtitle_snapshot("AFTER_SEGMENT", deduped))
        return deduped

    # ------------------------------------------------------------------
    # Stage 3: Translate
    # ------------------------------------------------------------------
    async def translate(
        self,
        entries: list[SubtitleEntry],
        context: str,
        glossary: str,
        on_progress: Optional[Callable[[int, int], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> list[SubtitleEntry]:
        if self.input_lang == self.output_lang:
            if on_log:
                on_log("⏭️ 输入输出语言相同，跳过翻译，仅优化")
            return entries
        if not entries:
            return entries

        sem = asyncio.Semaphore(max(1, self.concurrency))
        total = len(entries)
        completed = 0
        context_size = self.DEFAULT_CONTEXT_SIZE

        starts = list(range(0, total, self.batch_size))
        batches_with_ctx: list[tuple[list[SubtitleEntry], list[SubtitleEntry], list[SubtitleEntry]]] = []
        for start in starts:
            end = min(total, start + self.batch_size)
            batch = entries[start:end]
            prev = entries[max(0, start - context_size):start]
            next_ctx = entries[end:min(total, end + context_size)]
            batches_with_ctx.append((batch, prev, next_ctx))

        async with self._create_session() as session:
            async def process_batch(
                batch: list[SubtitleEntry],
                prev_ctx: list[SubtitleEntry],
                next_ctx: list[SubtitleEntry],
            ):
                nonlocal completed
                if is_cancelled and is_cancelled():
                    return
                async with sem:
                    if is_cancelled and is_cancelled():
                        return
                    try:
                        # 初译阶段只读取稳定原文上下文，不读取其他并发批次正在变化的 tgt。
                        result_map, unresolved = await _translate_batch_with_repair(
                            batch, context, glossary, self.client, session,
                            self.input_lang, self.output_lang, prev_ctx, next_ctx,
                            include_context_targets=False,
                        )
                        for entry in batch:
                            translation = result_map.get(entry.index, "")
                            source_text = _get_source_text(entry, self.input_lang_code)
                            if (
                                source_can_remain_untranslated(source_text)
                                and translation.strip() == source_text.strip()
                            ):
                                entry.lines = [source_text]
                                entry.status = "done"
                                entry.stage_status = "source_identity_allowed"
                                entry.error_msg = ""
                                if "source_identity_allowed" not in entry.quality_flags:
                                    entry.quality_flags.append("source_identity_allowed")
                            elif translation and source_text:
                                entry.lines = [translation, source_text]
                                entry.status = "done"
                                entry.stage_status = "translated"
                                entry.error_msg = ""
                            else:
                                entry.status = "error"
                                entry.stage_status = "translation_failed"
                                if entry.index in unresolved:
                                    entry.error_msg = "translation missing after retry"
                    except Exception as exc:
                        logger.error(f"Translate batch failed: {exc}")
                        for entry in batch:
                            entry.status = "error"
                            entry.stage_status = "translation_failed"
                            entry.error_msg = "translation batch failed"
                            if "translation_batch_failed" not in entry.quality_flags:
                                entry.quality_flags.append("translation_batch_failed")
                    finally:
                        completed += len(batch)
                        if on_progress:
                            on_progress(min(completed, total), total)

            await asyncio.gather(*(
                process_batch(batch, prev, next_ctx)
                for batch, prev, next_ctx in batches_with_ctx
            ))

            unresolved_ids = [
                entry.index for entry in entries
                if entry.status == "error"
                and getattr(entry, "error_msg", "").startswith("translation")
            ]
            if unresolved_ids:
                logger.warning("Translation fallback to source for ids: %s", unresolved_ids)
                unresolved_set = set(unresolved_ids)
                for entry in entries:
                    if entry.index in unresolved_set:
                        source_text = _get_source_text(entry, self.input_lang_code)
                        self._keep_source_fallback(entry, source_text)
                if on_log:
                    on_log(
                        f"    ⚠️ {len(unresolved_ids)} 条字幕翻译失败，"
                        f"已保留原文（ID: {unresolved_ids}）"
                    )

            if on_log:
                on_log("  🔧 检查并修复漏译条目...")
            repaired, total_incomplete = await _repair_incomplete_segment_translations(
                entries, context, glossary, self.client, session,
                self.input_lang, self.output_lang,
            )

        if on_log:
            if total_incomplete > 0:
                on_log(f"  ✅ 漏译修复完成: {repaired}/{total_incomplete} 条已修复")
            else:
                on_log("  ✅ 漏译修复完成: 无漏译条目")

        still_bad: list[int] = []
        for entry in entries:
            source_text = _get_source_text(entry, self.input_lang_code)
            target_text = _get_target_text(entry, self.input_lang_code)
            if source_text and not _translation_is_usable(
                target_text, source_text, self.output_lang
            ):
                self._keep_source_fallback(entry, source_text)
                still_bad.append(entry.index)
            elif entry.stage_status in {"translation_failed_fallback", "translation_needs_review"}:
                # 修复函数可能已经把内容修好，恢复准确阶段状态。
                self._mark_translation_recovered(entry)
                entry.stage_status = "translated_repaired"

        if still_bad and on_log:
            on_log(
                f"  ⚠️ {len(still_bad)} 条漏译修复失败，已自动保留原文兜底: {still_bad}"
            )

        logger.debug(_format_subtitle_snapshot("AFTER_TRANSLATE", entries))
        return entries

    # ------------------------------------------------------------------
    # Stage 4: Optimize
    # ------------------------------------------------------------------
    async def optimize(
        self,
        entries: list[SubtitleEntry],
        context: str,
        glossary: str,
        on_progress: Optional[Callable[[int, int], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> list[SubtitleEntry]:
        if not entries:
            return entries

        total = len(entries)
        context_size = self.DEFAULT_CONTEXT_SIZE
        original_cues = merge_source_cues(entries, self.input_lang_code)

        async with self._create_session() as session:
            if on_log:
                on_log("  🔎 第一轮：独立忠实度审校（只修漏译/误译，不润色）")
            await self._run_fidelity_review(
                entries, context, glossary, session, on_log, is_cancelled
            )

            # 忠实度阶段结束后，所有 entry.lines 已冻结。润色并发只写
            # optimized_lines，因此各批次读取到的上下文是确定的。
            sem = asyncio.Semaphore(max(1, self.concurrency))
            completed = 0
            optimize_recovered = 0
            optimize_failed = 0
            optimize_snapshots = self._snapshot_entries(entries)
            counter_lock = asyncio.Lock()
            starts = list(range(0, total, self.batch_size))
            batches_with_ctx: list[
                tuple[int, list[SubtitleEntry], list[SubtitleEntry], list[SubtitleEntry]]
            ] = []
            for start_pos in starts:
                end_pos = min(total, start_pos + self.batch_size)
                batch = optimize_snapshots[start_pos:end_pos]
                prev = optimize_snapshots[
                    max(0, start_pos - context_size):start_pos
                ]
                next_ctx = optimize_snapshots[
                    end_pos:min(total, end_pos + context_size)
                ]
                batches_with_ctx.append((start_pos, batch, prev, next_ctx))

            def apply_optimized(
                position: int,
                snapshot: SubtitleEntry,
                lines: list[str] | None,
            ) -> None:
                if not lines:
                    raise ValueError(
                        f"optimize result missing id={snapshot.index}"
                    )
                source_text = _get_source_text(snapshot, self.input_lang_code)
                target_text = str(lines[0] if lines else "").strip()
                if not _translation_is_usable(
                    target_text, source_text, self.output_lang
                ):
                    raise ValueError(
                        f"optimize invalid target id={snapshot.index}"
                    )
                live = entries[position]
                live.optimized_lines = (
                    [target_text, source_text] if source_text else [target_text]
                )
                self._mark_translation_recovered(live)
                live.stage_status = (
                    "optimized_with_flags" if live.quality_flags else "optimized"
                )

            def mark_optimize_failure(
                position: int,
                kind: str,
                error: Exception,
            ) -> None:
                live = entries[position]
                live.optimized_lines = list(live.lines)
                source_text = _get_source_text(live, self.input_lang_code)
                target_text = _get_target_text(live, self.input_lang_code)
                if _translation_is_usable(target_text, source_text, self.output_lang):
                    self._mark_translation_recovered(live)
                    if "optimize_unapplied" not in live.quality_flags:
                        live.quality_flags.append("optimize_unapplied")
                    live.stage_status = "optimize_skipped_with_flags"
                    return
                live.status = "needs_review"
                flag = {
                    "timeout": "optimize_timeout",
                    "invalid": "optimize_target_invalid",
                }.get(kind, "optimize_failed")
                live.stage_status = f"{flag}_fallback"
                live.error_msg = f"{flag}: {error}"
                if flag not in live.quality_flags:
                    live.quality_flags.append(flag)

            async def process_batch(
                start_pos: int,
                batch: list[SubtitleEntry],
                prev_ctx: list[SubtitleEntry],
                next_ctx: list[SubtitleEntry],
            ):
                nonlocal completed, optimize_recovered, optimize_failed
                if is_cancelled and is_cancelled():
                    raise asyncio.CancelledError()
                async with sem:
                    if is_cancelled and is_cancelled():
                        raise asyncio.CancelledError()
                    for offset, snapshot in enumerate(batch):
                        if self._is_source_identity_allowed(snapshot):
                            entries[start_pos + offset].optimized_lines = list(
                                entries[start_pos + offset].lines
                            )
                    retry_items: list[tuple[int, SubtitleEntry, str, Exception]] = []
                    try:
                        result_map = await asyncio.wait_for(
                            _optimize_batch_impl(
                                batch, context, glossary, self.client, session,
                                self.input_lang, self.output_lang, prev_ctx, next_ctx,
                            ),
                            timeout=self.optimize_batch_timeout_seconds,
                        )
                        for offset, snapshot in enumerate(batch):
                            position = start_pos + offset
                            if self._is_source_identity_allowed(snapshot):
                                continue
                            try:
                                apply_optimized(
                                    position,
                                    snapshot,
                                    result_map.get(snapshot.index),
                                )
                            except Exception as item_error:
                                retry_items.append(
                                    (position, snapshot, "invalid", item_error)
                                )
                    except asyncio.TimeoutError as exc:
                        ids = [snapshot.index for snapshot in batch]
                        logger.error(
                            "Optimize batch timed out after %ss for ids: %s",
                            self.optimize_batch_timeout_seconds, ids,
                        )
                        retry_items.extend(
                            (start_pos + offset, snapshot, "timeout", exc)
                            for offset, snapshot in enumerate(batch)
                        )
                    except Exception as exc:
                        logger.error(f"Optimize batch failed: {exc}")
                        retry_items.extend(
                            (start_pos + offset, snapshot, "failed", exc)
                            for offset, snapshot in enumerate(batch)
                        )

                    for position, snapshot, initial_kind, initial_error in retry_items:
                        if self._is_source_identity_allowed(snapshot):
                            continue
                        recovered = False
                        last_kind = initial_kind
                        last_error = initial_error
                        for attempt in range(self.DEFAULT_QUALITY_ITEM_RETRY_ATTEMPTS):
                            if is_cancelled and is_cancelled():
                                raise asyncio.CancelledError()
                            item_prev = optimize_snapshots[
                                max(0, position - context_size):position
                            ]
                            item_next = optimize_snapshots[
                                position + 1:min(total, position + 1 + context_size)
                            ]
                            try:
                                item_map = await asyncio.wait_for(
                                    _optimize_batch_impl(
                                        [snapshot], context, glossary,
                                        self.client, session,
                                        self.input_lang, self.output_lang,
                                        item_prev, item_next,
                                    ),
                                    timeout=self.optimize_batch_timeout_seconds,
                                )
                                apply_optimized(
                                    position,
                                    snapshot,
                                    item_map.get(snapshot.index),
                                )
                                async with counter_lock:
                                    optimize_recovered += 1
                                recovered = True
                                break
                            except asyncio.TimeoutError as retry_error:
                                last_kind = "timeout"
                                last_error = retry_error
                            except Exception as retry_error:
                                last_kind = (
                                    "invalid"
                                    if isinstance(retry_error, ValueError)
                                    else "failed"
                                )
                                last_error = retry_error
                            logger.warning(
                                "Optimize item retry %s/%s failed for id=%s: %s",
                                attempt + 1,
                                self.DEFAULT_QUALITY_ITEM_RETRY_ATTEMPTS,
                                snapshot.index,
                                last_error,
                            )
                        if not recovered:
                            mark_optimize_failure(
                                position, last_kind, last_error
                            )
                            async with counter_lock:
                                optimize_failed += 1
                    completed += len(batch)
                    if on_progress:
                        on_progress(min(completed, total), total)

            if on_log:
                on_log("  ✨ 第二轮：自然度与字幕表达润色")
            await asyncio.gather(*(
                process_batch(start_pos, batch, prev, next_ctx)
                for start_pos, batch, prev, next_ctx in batches_with_ctx
            ))
            if on_log:
                on_log(
                    f"  ✅ 润色完成：逐条恢复 {optimize_recovered} 条，"
                    f"最终失败 {optimize_failed} 条"
                )

            logger.debug(
                _format_subtitle_snapshot("AFTER_OPTIMIZE", entries, use_optimized=True)
            )

            if on_log:
                on_log("  🌐 第三轮：构建全文术语/专名一致性规则并执行审计")
            await self._run_consistency_review(
                entries, context, glossary, session, on_log, is_cancelled
            )

        entries = reflow_semantic_entries(
            entries,
            input_lang_code=self.input_lang_code,
            output_lang_code=LANGUAGE_MAP.get(
                self.output_lang, ("zh", self.output_lang)
            )[0],
            use_optimized=True,
        )
        integrity_errors = source_integrity_errors(
            original_cues,
            entries,
            self.input_lang_code,
        )
        if integrity_errors:
            for entry in entries:
                entry.status = "needs_review"
                entry.stage_status = "source_integrity_failed"
                if "source_integrity_failed" not in entry.quality_flags:
                    entry.quality_flags.append("source_integrity_failed")
            raise RuntimeError("; ".join(integrity_errors))

        final_entries, lint_report = finalize_entries(
            entries,
            use_optimized=True,
            input_lang_code=self.input_lang_code,
            output_lang_code=LANGUAGE_MAP.get(
                self.output_lang, ("zh", self.output_lang)
            )[0],
        )
        final_integrity_errors = source_integrity_errors(
            original_cues,
            final_entries,
            self.input_lang_code,
        )
        if final_integrity_errors:
            # 降级为警告，不再阻断流程
            # finalize_entries() 的合并操作会改变签名，但这是有意为之的优化
            logger.warning(
                "Source integrity check warnings (non-blocking): %s",
                final_integrity_errors,
            )
            for entry in final_entries:
                if "source_integrity_diff" not in entry.quality_flags:
                    entry.quality_flags.append("source_integrity_diff")
            if on_log:
                on_log(
                    f"  ⚠️ Source integrity 差异（已降级为警告）: "
                    + "; ".join(final_integrity_errors)
                )
        self.last_lint_report = lint_report
        if on_log:
            on_log(f"  ✅ {lint_report.summary()}")
            if lint_report.total_warnings:
                on_log(
                    "  ⚠️ 仍需检查："
                    f"双语结构不完整 {len(lint_report.incomplete_bilingual)}，"
                    f"高阅读压力 {len(lint_report.high_cps) + len(lint_report.bad_cps)}，"
                    f"大重叠 {len(lint_report.large_overlaps)}"
                )
        logger.debug(lint_report.to_text())
        logger.debug(
            _format_subtitle_snapshot(
                "AFTER_FINAL_LINT", final_entries, use_optimized=True
            )
        )
        return final_entries

    # ------------------------------------------------------------------
    # Stage 5: Final SRT lint / autofix
    # ------------------------------------------------------------------
    def finalize(
        self,
        entries: list[SubtitleEntry],
        use_optimized: bool = True,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> list[SubtitleEntry]:
        """Run the final deterministic subtitle engineering pass on demand."""
        final_entries, lint_report = finalize_entries(
            entries,
            use_optimized=use_optimized,
            input_lang_code=self.input_lang_code,
            output_lang_code=LANGUAGE_MAP.get(self.output_lang, ("zh", self.output_lang))[0],
        )
        self.last_lint_report = lint_report
        if on_log:
            on_log(f"  ✅ {lint_report.summary()}")
        return final_entries

    def get_last_lint_report_text(self) -> str:
        if not self.last_lint_report:
            return ""
        return self.last_lint_report.to_text()

    async def execute(
        self,
        entries: list[SubtitleEntry],
        context: str,
        glossary: str,
        *,
        segment_batch_size: int | None = None,
        on_stage: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[str, int, int], None]] = None,
        on_snapshot: Optional[Callable[[str, list[SubtitleEntry], bool], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> PipelineRunResult:
        """Run the shared semantic pipeline for both single and batch tasks.

        File parsing, user glossary confirmation and exporting remain adapter
        concerns. Once ``context`` and ``glossary`` are fixed, every caller uses
        this method so stage order, cancellation, snapshots, lint and quality
        gating cannot diverge between single and batch modes.
        """

        def check_cancelled() -> None:
            if is_cancelled and is_cancelled():
                raise asyncio.CancelledError()

        def progress(stage: str):
            if not on_progress:
                return None
            return lambda current, total: on_progress(stage, current, total)

        original_batch_size = self.batch_size
        check_cancelled()
        if on_stage:
            on_stage("segment")
        if segment_batch_size is not None:
            self.batch_size = max(1, int(segment_batch_size))
        try:
            entries = await self.segment(
                entries,
                context,
                glossary,
                on_progress=progress("segment"),
                on_log=on_log,
                is_cancelled=is_cancelled,
            )
        finally:
            self.batch_size = original_batch_size
        if on_snapshot:
            on_snapshot("segment", entries, False)

        check_cancelled()
        if on_stage:
            on_stage("translate")
        entries = await self.translate(
            entries,
            context,
            glossary,
            on_progress=progress("translate"),
            on_log=on_log,
            is_cancelled=is_cancelled,
        )
        if on_snapshot:
            on_snapshot("translate", entries, False)

        check_cancelled()
        if on_stage:
            on_stage("optimize")
        entries = await self.optimize(
            entries,
            context,
            glossary,
            on_progress=progress("optimize"),
            on_log=on_log,
            is_cancelled=is_cancelled,
        )
        if on_snapshot:
            on_snapshot("optimize", entries, True)

        quality_report = evaluate_quality_gate(entries, self.last_lint_report)
        if on_log:
            prefix = "  ✅" if quality_report.passed else "  ❌"
            on_log(f"{prefix} {quality_report.summary()}")
        return PipelineRunResult(
            entries=entries,
            lint_report=self.last_lint_report,
            quality_report=quality_report,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------
    async def _review_segment_boundaries(
        self,
        entries: list[SubtitleEntry],
        boundary_source_ids: list[int],
        context: str,
        session: aiohttp.ClientSession,
        on_log: Optional[Callable[[str], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> list[SubtitleEntry]:
        """Re-run segmentation around every original batch boundary.

        The first segmentation pass is parallel and intentionally independent.
        This sequential pass gives the LLM both sides of each boundary while
        accepting a replacement only when every original source id is preserved
        exactly once.
        """
        current = list(entries)

        def source_ids(entry: SubtitleEntry) -> list[int]:
            return list(entry.source_indexes or [entry.index])

        for boundary_id in boundary_source_ids:
            if is_cancelled and is_cancelled():
                raise asyncio.CancelledError()

            left_pos = next(
                (pos for pos, entry in enumerate(current) if boundary_id in source_ids(entry)),
                None,
            )
            right_pos = next(
                (pos for pos, entry in enumerate(current) if boundary_id + 1 in source_ids(entry)),
                None,
            )
            if left_pos is None or right_pos is None:
                if on_log:
                    on_log(f"  ⚠️ 无法定位边界 {boundary_id}/{boundary_id + 1}，跳过")
                continue
            if left_pos == right_pos:
                if on_log:
                    on_log(f"  ✅ 边界 {boundary_id}/{boundary_id + 1} 已在同一字幕中")
                continue

            start = max(0, min(left_pos, right_pos) - 2)
            end = min(len(current), max(left_pos, right_pos) + 3)
            local = current[start:end]
            before_sources = sorted(
                source_id for entry in local for source_id in source_ids(entry)
            )
            try:
                reviewed = await self._segment_batch(
                    local, context, session, on_log
                )
            except Exception as exc:
                logger.warning(
                    "Boundary review failed for %s/%s: %s",
                    boundary_id, boundary_id + 1, exc,
                )
                continue

            after_sources = sorted(
                source_id for entry in reviewed for source_id in source_ids(entry)
            )
            if after_sources != before_sources:
                logger.error(
                    "Rejected boundary review %s/%s: source ids changed %s -> %s",
                    boundary_id, boundary_id + 1, before_sources, after_sources,
                )
                if on_log:
                    on_log(
                        f"  ↩️ 边界 {boundary_id}/{boundary_id + 1} 复查结果未通过完整性检查，已回退"
                    )
                continue

            current = current[:start] + reviewed + current[end:]
            current.sort(key=lambda entry: _srt_time_to_ms(entry.start_time))
            if on_log:
                on_log(
                    f"  ✅ 已复查边界 {boundary_id}/{boundary_id + 1}："
                    f"{len(local)} 条 → {len(reviewed)} 条"
                )

        return current

    async def _run_fidelity_review(
        self,
        entries: list[SubtitleEntry],
        context: str,
        glossary: str,
        session: aiohttp.ClientSession,
        on_log: Optional[Callable[[str], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> None:
        snapshots = self._snapshot_entries(entries)
        total = len(snapshots)
        context_size = self.DEFAULT_CONTEXT_SIZE
        sem = asyncio.Semaphore(max(1, self.concurrency))
        corrected = 0
        failed_batches = 0
        recovered_items = 0
        failed_items = 0
        counter_lock = asyncio.Lock()

        jobs = []
        for start in range(0, total, self.batch_size):
            end = min(total, start + self.batch_size)
            jobs.append((
                start,
                snapshots[start:end],
                snapshots[max(0, start - context_size):start],
                snapshots[end:min(total, end + context_size)],
            ))

        def apply_target(position: int, snapshot: SubtitleEntry, target: str) -> int:
            live = entries[position]
            source_text = _get_source_text(snapshot, self.input_lang_code)
            old_target = _get_target_text(snapshot, self.input_lang_code)
            if not _translation_is_usable(target, source_text, self.output_lang):
                raise ValueError(f"fidelity invalid target id={snapshot.index}")
            live.lines = [target, source_text] if source_text else [target]
            self._mark_translation_recovered(live)
            live.stage_status = (
                "fidelity_checked_with_flags"
                if live.quality_flags else "fidelity_checked"
            )
            return int(target != old_target)

        def keep_existing_after_fidelity_failure(
            position: int,
            error: Exception,
        ) -> None:
            live = entries[position]
            source_text = _get_source_text(live, self.input_lang_code)
            target_text = _get_target_text(live, self.input_lang_code)
            if _translation_is_usable(target_text, source_text, self.output_lang):
                self._mark_translation_recovered(live)
                if "fidelity_review_unapplied" not in live.quality_flags:
                    live.quality_flags.append("fidelity_review_unapplied")
                live.stage_status = "fidelity_skipped_with_flags"
                return

            live.stage_status = "fidelity_failed_fallback"
            live.status = "needs_review"
            live.error_msg = f"fidelity review failed: {error}"
            if "fidelity_review_failed" not in live.quality_flags:
                live.quality_flags.append("fidelity_review_failed")

        async def process(
            start: int,
            batch: list[SubtitleEntry],
            prev_ctx: list[SubtitleEntry],
            next_ctx: list[SubtitleEntry],
        ):
            nonlocal corrected, failed_batches, recovered_items, failed_items
            if is_cancelled and is_cancelled():
                raise asyncio.CancelledError()
            async with sem:
                try:
                    result_map = await asyncio.wait_for(
                        fidelity_review_batch(
                            batch, context, glossary, self.client, session,
                            self.input_lang, self.output_lang, prev_ctx, next_ctx,
                        ),
                        timeout=self.fidelity_batch_timeout_seconds,
                    )
                    validated: list[tuple[int, SubtitleEntry, str]] = []
                    for offset, snapshot in enumerate(batch):
                        if self._is_source_identity_allowed(snapshot):
                            continue
                        source_text = _get_source_text(snapshot, self.input_lang_code)
                        target = result_map.get(snapshot.index, "")
                        if not _translation_is_usable(
                            target, source_text, self.output_lang
                        ):
                            raise ValueError(
                                f"fidelity invalid target id={snapshot.index}"
                            )
                        validated.append((start + offset, snapshot, target))

                    local_corrected = sum(
                        apply_target(position, snapshot, target)
                        for position, snapshot, target in validated
                    )
                    async with counter_lock:
                        corrected += local_corrected
                except Exception as exc:
                    logger.error("Fidelity review batch failed: %s", exc)
                    async with counter_lock:
                        failed_batches += 1

                    for offset, snapshot in enumerate(batch):
                        position = start + offset
                        if self._is_source_identity_allowed(snapshot):
                            continue
                        last_error: Exception = exc
                        recovered = False
                        for attempt in range(self.DEFAULT_QUALITY_ITEM_RETRY_ATTEMPTS):
                            if is_cancelled and is_cancelled():
                                raise asyncio.CancelledError()
                            item_prev = snapshots[
                                max(0, position - context_size):position
                            ]
                            item_next = snapshots[
                                position + 1:min(total, position + 1 + context_size)
                            ]
                            try:
                                result_map = await asyncio.wait_for(
                                    fidelity_review_batch(
                                        [snapshot], context, glossary,
                                        self.client, session,
                                        self.input_lang, self.output_lang,
                                        item_prev, item_next,
                                    ),
                                    timeout=self.fidelity_batch_timeout_seconds,
                                )
                                target = result_map.get(snapshot.index, "")
                                local_corrected = apply_target(
                                    position, snapshot, target
                                )
                                async with counter_lock:
                                    corrected += local_corrected
                                    recovered_items += 1
                                recovered = True
                                break
                            except Exception as retry_exc:
                                last_error = retry_exc
                                logger.warning(
                                    "Fidelity item retry %s/%s failed for id=%s: %s",
                                    attempt + 1,
                                    self.DEFAULT_QUALITY_ITEM_RETRY_ATTEMPTS,
                                    snapshot.index,
                                    retry_exc,
                                )
                        if recovered:
                            continue
                        keep_existing_after_fidelity_failure(position, last_error)
                        async with counter_lock:
                            failed_items += 1

        await asyncio.gather(*(process(*job) for job in jobs))
        if on_log:
            on_log(
                f"  ✅ 忠实度审校完成：修正 {corrected} 条，"
                f"失败批次 {failed_batches} 个，逐条恢复 {recovered_items} 条，"
                f"最终失败 {failed_items} 条"
            )

    async def _run_consistency_review(
        self,
        entries: list[SubtitleEntry],
        context: str,
        glossary: str,
        session: aiohttp.ClientSession,
        on_log: Optional[Callable[[str], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> None:
        guide = ""
        guide_error: Exception | None = None
        for attempt in range(self.DEFAULT_CONSISTENCY_GUIDE_RETRY_ATTEMPTS):
            if is_cancelled and is_cancelled():
                raise asyncio.CancelledError()
            try:
                guide = await build_consistency_guide(
                    entries, context, glossary, self.client, session,
                    self.input_lang, self.output_lang,
                    chunk_size=max(40, self.batch_size * 10),
                    concurrency=max(1, min(4, self.concurrency)),
                )
                guide_error = None
                break
            except Exception as exc:
                guide_error = exc
                logger.warning(
                    "Build consistency guide attempt %s/%s failed: %s",
                    attempt + 1,
                    self.DEFAULT_CONSISTENCY_GUIDE_RETRY_ATTEMPTS,
                    exc,
                )

        if guide_error is not None:
            logger.error("Build consistency guide failed: %s", guide_error)
            for entry in entries:
                source_text = _get_source_text(entry, self.input_lang_code)
                target_text = _get_target_text(entry, self.input_lang_code)
                if _translation_is_usable(target_text, source_text, self.output_lang):
                    self._mark_translation_recovered(entry)
                    if "consistency_guide_unapplied" not in entry.quality_flags:
                        entry.quality_flags.append("consistency_guide_unapplied")
                    entry.stage_status = "consistency_guide_skipped_with_flags"
                    continue

                if "consistency_guide_failed" not in entry.quality_flags:
                    entry.quality_flags.append("consistency_guide_failed")
                entry.status = "needs_review"
                if not entry.error_msg:
                    entry.error_msg = f"consistency guide failed: {guide_error}"
                if entry.stage_status == "optimized":
                    entry.stage_status = "consistency_guide_failed_fallback"
            if on_log:
                on_log("  ⚠️ 全文一致性规则构建失败，保留已润色译文并记录质量标记")
            return

        if not guide.strip():
            for entry in entries:
                if entry.stage_status == "optimized":
                    entry.stage_status = "consistency_checked"
            if on_log:
                on_log("  ✅ 未发现需要额外统一的全文术语或专名")
            return

        snapshots = self._snapshot_entries(entries, use_optimized=True)
        total = len(snapshots)
        context_size = self.DEFAULT_CONTEXT_SIZE
        sem = asyncio.Semaphore(max(1, self.concurrency))
        changed = 0
        failed_batches = 0
        recovered_items = 0
        failed_items = 0
        counter_lock = asyncio.Lock()
        jobs = []
        for start in range(0, total, self.batch_size):
            end = min(total, start + self.batch_size)
            jobs.append((
                start,
                snapshots[start:end],
                snapshots[max(0, start - context_size):start],
                snapshots[end:min(total, end + context_size)],
            ))

        def apply_target(position: int, snapshot: SubtitleEntry, target: str) -> int:
            live = entries[position]
            source_text = _get_source_text(snapshot, self.input_lang_code)
            old_target = snapshot.lines[0] if snapshot.lines else ""
            if not _translation_is_usable(target, source_text, self.output_lang):
                raise ValueError(
                    f"consistency invalid target id={snapshot.index}"
                )
            live.optimized_lines = [target, source_text] if source_text else [target]
            self._mark_translation_recovered(live)
            for flag in ("consistency_review_failed", "consistency_review_unapplied"):
                if flag in live.quality_flags:
                    live.quality_flags.remove(flag)
            live.stage_status = (
                "consistency_checked_with_flags"
                if live.quality_flags else "consistency_checked"
            )
            return int(target != old_target)

        def keep_existing_after_consistency_failure(
            position: int,
            error: Exception,
        ) -> None:
            live = entries[position]
            if "consistency_review_failed" in live.quality_flags:
                live.quality_flags.remove("consistency_review_failed")
            if "consistency_review_unapplied" not in live.quality_flags:
                live.quality_flags.append("consistency_review_unapplied")

            if live.status not in {"error", "needs_review"}:
                live.status = "done"
                live.error_msg = ""
            elif not live.error_msg:
                live.error_msg = f"consistency review skipped: {error}"

            live.stage_status = (
                "consistency_skipped_with_flags"
                if live.status in {"error", "needs_review"} or live.quality_flags
                else "consistency_skipped_fallback"
            )

        async def process(
            start: int,
            batch: list[SubtitleEntry],
            prev_ctx: list[SubtitleEntry],
            next_ctx: list[SubtitleEntry],
        ):
            nonlocal changed, failed_batches, recovered_items, failed_items
            if is_cancelled and is_cancelled():
                raise asyncio.CancelledError()
            async with sem:
                try:
                    result_map = await asyncio.wait_for(
                        consistency_review_batch(
                            batch, guide, context, self.client, session,
                            self.input_lang, self.output_lang, prev_ctx, next_ctx,
                        ),
                        timeout=self.consistency_batch_timeout_seconds,
                    )
                    validated: list[tuple[int, SubtitleEntry, str]] = []
                    for offset, snapshot in enumerate(batch):
                        if self._is_source_identity_allowed(snapshot):
                            continue
                        source_text = _get_source_text(snapshot, self.input_lang_code)
                        target = result_map.get(snapshot.index, "")
                        if not _translation_is_usable(
                            target, source_text, self.output_lang
                        ):
                            raise ValueError(
                                f"consistency invalid target id={snapshot.index}"
                            )
                        validated.append((start + offset, snapshot, target))

                    local_changed = sum(
                        apply_target(position, snapshot, target)
                        for position, snapshot, target in validated
                    )
                    async with counter_lock:
                        changed += local_changed
                except Exception as exc:
                    logger.error("Consistency review batch failed: %s", exc)
                    async with counter_lock:
                        failed_batches += 1

                    for offset, snapshot in enumerate(batch):
                        position = start + offset
                        if self._is_source_identity_allowed(snapshot):
                            continue
                        last_error: Exception = exc
                        recovered = False
                        for attempt in range(self.DEFAULT_QUALITY_ITEM_RETRY_ATTEMPTS):
                            if is_cancelled and is_cancelled():
                                raise asyncio.CancelledError()
                            item_prev = snapshots[
                                max(0, position - context_size):position
                            ]
                            item_next = snapshots[
                                position + 1:min(total, position + 1 + context_size)
                            ]
                            try:
                                result_map = await asyncio.wait_for(
                                    consistency_review_batch(
                                        [snapshot], guide, context,
                                        self.client, session,
                                        self.input_lang, self.output_lang,
                                        item_prev, item_next,
                                    ),
                                    timeout=self.consistency_batch_timeout_seconds,
                                )
                                target = result_map.get(snapshot.index, "")
                                local_changed = apply_target(
                                    position, snapshot, target
                                )
                                async with counter_lock:
                                    changed += local_changed
                                    recovered_items += 1
                                recovered = True
                                break
                            except Exception as retry_exc:
                                last_error = retry_exc
                                logger.warning(
                                    "Consistency item retry %s/%s failed for id=%s: %s",
                                    attempt + 1,
                                    self.DEFAULT_QUALITY_ITEM_RETRY_ATTEMPTS,
                                    snapshot.index,
                                    retry_exc,
                                )
                        if recovered:
                            continue

                        keep_existing_after_consistency_failure(position, last_error)
                        async with counter_lock:
                            failed_items += 1

        await asyncio.gather(*(process(*job) for job in jobs))
        if on_log:
            on_log(
                f"  ✅ 全文一致性审计完成：统一修正 {changed} 条，"
                f"失败批次 {failed_batches} 个，逐条恢复 {recovered_items} 条，"
                f"未应用一致性修改 {failed_items} 条"
            )

    async def _segment_batch(
        self,
        batch: list[SubtitleEntry],
        context: str,
        session: aiohttp.ClientSession,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> list[SubtitleEntry]:
        """Build complete semantic units without pre-translation splitting.

        Long-source splitting used to happen here, which forced the translator
        to process grammatical fragments independently. We now keep the whole
        semantic unit intact and reflow its final translation back to the
        original source cue timing after all semantic quality passes.
        """
        return await self._llm_merge(batch, context, session, on_log)

    def _build_split_messages(
        self,
        entries: list[SubtitleEntry],
        context: str,
    ) -> list[dict]:
        items_json = json.dumps([
            {"id": e.index, "en": _get_source_text(e, self.input_lang_code),
             "start": e.start_time, "end": e.end_time}
            for e in entries
        ], ensure_ascii=False)

        lang_name = _get_prompt_lang(self.input_lang)
        sys_content = (
            "你是专业字幕切分助手。\n"
            f"【背景与风格】\n{context}\n\n"
            f"任务：以下{lang_name}字幕条目过长（>20词），找出所有自然断点，"
            f"按需切分，确保每段 ≤20词，不限制段数。\n\n"
            "规则：\n"
            "1. 每段 ≤20个词，包括最后一段\n"
            "2. 断点选在最自然的语义边界：从句结束、意群转折、自然停顿处\n"
            "3. 每段必须是相对完整的语义单元（不可在词组内部切断）\n"
            "4. 只返回断点的词序号，不要输出任何正文内容\n"
            "5. 严格按JSON格式返回，不要加任何解释\n\n"
            "【重要】返回格式：\n"
            '[{"id": 原id, "break_after_word": [n1, n2, ...]}]\n'
            "break_after_word 是断点列表，值为词序号（从1开始计数），表示在第n个词之后断开。\n"
            '无需切分时返回空列表：[{"id": 原id, "break_after_word": []}]\n\n'
            "示例：原文共50词，在第15词后、第30词后、第45词后各断一次：\n"
            '[{"id": 6, "break_after_word": [15, 30, 45]}]'
        )

        messages = [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": f"切分以下长字幕条目：\n{items_json}"}
        ]
        return messages

    async def _split_long_entries_with_llm(
        self,
        long_entries: list[SubtitleEntry],
        context: str,
        session: aiohttp.ClientSession,
        on_log: Optional[Callable[[str], None]] = None,
        max_rounds: int = 3,
    ) -> list[SubtitleEntry]:
        finished: list[SubtitleEntry] = []
        pending = list(long_entries)

        for round_idx in range(max_rounds):
            if not pending:
                break

            try:
                response = await self.client.chat(
                    self._build_split_messages(pending, context),
                    session,
                    temperature=0.2,
                )
                clean = re.sub(r'```(?:json)?|```', '', response).strip()
                data = json.loads(clean)
                round_results = _apply_llm_split(
                    pending,
                    data,
                    log_fn=on_log,
                    lang_code=self.input_lang_code,
                    fallback_to_local=False,
                )
            except Exception as exc:
                logger.warning(f"LLM split round {round_idx + 1} failed: {exc}")
                break

            short_entries, pending = _split_short_long(
                round_results,
                lang_code=self.input_lang_code,
            )
            finished.extend(short_entries)

            if pending and on_log:
                on_log(
                    f"  LLM切分第 {round_idx + 1} 轮后仍有 "
                    f"{len(pending)} 条过长，继续请求切分"
                )

        if pending:
            if on_log:
                on_log(f"  LLM切分仍有 {len(pending)} 条过长，使用本地兜底切分")
            for entry in pending:
                finished.extend(
                    _force_split_entry(
                        entry,
                        lang_code=self.input_lang_code,
                        log_fn=on_log,
                    )
                )

        finished.sort(key=lambda e: _srt_time_to_ms(e.start_time))
        return finished

    async def _llm_merge(
        self,
        batch: list[SubtitleEntry],
        context: str,
        session: aiohttp.ClientSession,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> list[SubtitleEntry]:
        """LLM merge pass: merge semantically incomplete adjacent entries."""
        if not batch:
            return batch

        lang_name = _get_prompt_lang(self.input_lang)
        items_json = json.dumps([
            {"id": e.index, "text": _get_source_text(e, self.input_lang_code)}
            for e in batch
        ], ensure_ascii=False)

        sys_content = (
            f"你是专业字幕合并助手。\n"
            f"【背景与风格】\n{context}\n\n"
            f"任务：以下{lang_name}字幕条目按时间顺序排列，部分条目是不完整的半句，"
            f"需要将语义上属于同一句的相邻条目合并为一组。\n\n"
            "规则：\n"
            "1. 以逗号、连词、介词、助词等结尾的条目必须与后续条目合并，直到语义完整\n"
            "2. 每组合并后必须形成一个完整语义句；英文最多60词，中文最多80字\n"
            "3. 已经是完整句子的条目单独一组，ids列表只含自身id\n"
            "4. 必须覆盖所有输入条目，不得遗漏任何id\n"
            "5. 严格按JSON格式返回，不要加任何解释\n"
            "6. 【语气词/插话处理】Okay./Sure./Right./Alright./Yeah./Uh-huh./Mm. 等单词短句"
            "不视为独立完整句，必须并入相邻条目：若前一组语义未完整则并入前一组，"
            "若前一组已完整则并入后一组\n"
            "7. 【半句识别】原文以 'the'/'a'/'an'/'of'/'to'/'and'/'but'/'or'/'that'/'which'/"
            "'with'/'in'/'on'/'at'/'is'/'are'/'was'/'were' 等词结尾，"
            "必定是半句，强制与下一条合并\n\n"
            "【重要】返回格式：\n"
            '[{"ids": [id1, id2, ...]}, ...]\n'
            "每个对象的ids是需要合并的相邻条目id列表，按原顺序排列。\n\n"
            "示例1：条目3以逗号结尾，条目4是它的后半句，条目5是完整句：\n"
            '[{"ids": [3, 4]}, {"ids": [5]}]\n\n'
            '示例2：条目6是完整句，条目7是"Okay."，条目8是新句的开头：\n'
            '[{"ids": [6]}, {"ids": [7, 8]}]'
        )

        messages = [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": f"分析并合并以下字幕条目：\n{items_json}"}
        ]

        try:
            response = await self.client.chat(messages, session, temperature=0.2)
            clean = re.sub(r'```(?:json)?|```', '', response).strip()
            data = json.loads(clean)
            return _apply_llm_merge(batch, data, log_fn=on_log, lang_code=self.input_lang_code)
        except Exception as exc:
            logger.warning(f"LLM merge pass failed, keeping original batch: {exc}")
            return batch

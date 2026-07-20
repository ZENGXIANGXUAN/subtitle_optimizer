import re
import json
import asyncio
import aiohttp
from typing import Callable, Optional

from core.models import (
    SubtitleEntry,
    _srt_time_to_ms,
    _format_subtitle_snapshot,
    verify_source_integrity,
)
from core.segmenter import _get_source_text, _get_target_text
from core.srt_lint import finalize_entries
from core.client import MistralClient
from core.quality import (
    fidelity_review_batch,
    build_consistency_guide,
    consistency_review_batch,
)
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
    DEFAULT_CONTEXT_SIZE = 3

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
        self.concurrency = concurrency
        self.batch_size = batch_size
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
            snapshots.append(clone)
        return snapshots

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
                            fallback.stage_status = "segment_failed_fallback"
                            fallback.quality_flags.append("segment_batch_failed")
                            fallback_items.append(fallback)
                        async with result_lock:
                            all_results.extend(fallback_items)
                    finally:
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
                            if translation and source_text:
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
                        entry.lines = [source_text] if source_text else entry.lines
                        entry.status = "needs_review"
                        entry.stage_status = "translation_failed_fallback"
                        entry.error_msg = "translation_failed_fallback_source"
                        if "source_fallback" not in entry.quality_flags:
                            entry.quality_flags.append("source_fallback")
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
                entry.status = "needs_review"
                entry.stage_status = "translation_needs_review"
                if not entry.error_msg:
                    entry.error_msg = "translation_needs_review"
                if "translation_hard_gate_failed" not in entry.quality_flags:
                    entry.quality_flags.append("translation_hard_gate_failed")
                still_bad.append(entry.index)
            elif entry.stage_status in {"translation_failed_fallback", "translation_needs_review"}:
                # 修复函数可能已经把内容修好，恢复准确阶段状态。
                entry.status = "done"
                entry.stage_status = "translated_repaired"
                entry.error_msg = ""

        if still_bad and on_log:
            on_log(
                f"  ⚠️ 仍有 {len(still_bad)} 条未通过译文硬检查，"
                f"已标记 needs_review: {still_bad}"
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
            starts = list(range(0, total, self.batch_size))
            batches_with_ctx: list[tuple[list[SubtitleEntry], list[SubtitleEntry], list[SubtitleEntry]]] = []
            for start_pos in starts:
                end_pos = min(total, start_pos + self.batch_size)
                batch = entries[start_pos:end_pos]
                prev = entries[max(0, start_pos - context_size):start_pos]
                next_ctx = entries[end_pos:min(total, end_pos + context_size)]
                batches_with_ctx.append((batch, prev, next_ctx))

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
                        result_map = await asyncio.wait_for(
                            _optimize_batch_impl(
                                batch, context, glossary, self.client, session,
                                self.input_lang, self.output_lang, prev_ctx, next_ctx,
                            ),
                            timeout=self.optimize_batch_timeout_seconds,
                        )
                        for entry in batch:
                            lines = result_map.get(entry.index)
                            if not lines:
                                raise ValueError(
                                    f"optimize result missing id={entry.index}"
                                )
                            source_text = (
                                lines[1] if len(lines) >= 2
                                else _get_source_text(entry, self.input_lang_code)
                            )
                            target_text = lines[0] if lines else ""
                            if len(lines) >= 2 and _translation_is_usable(
                                target_text, source_text, self.output_lang
                            ):
                                entry.optimized_lines = list(lines)
                                entry.status = "done"
                                entry.stage_status = "optimized"
                                entry.error_msg = ""
                            else:
                                entry.optimized_lines = list(entry.lines)
                                entry.status = "needs_review"
                                entry.stage_status = "optimize_invalid_fallback"
                                entry.error_msg = "optimize_target_invalid"
                                if "optimize_target_invalid" not in entry.quality_flags:
                                    entry.quality_flags.append("optimize_target_invalid")
                    except asyncio.TimeoutError:
                        ids = [entry.index for entry in batch]
                        logger.error(
                            "Optimize batch timed out after %ss for ids: %s",
                            self.optimize_batch_timeout_seconds, ids,
                        )
                        for entry in batch:
                            entry.optimized_lines = list(entry.lines)
                            entry.status = "needs_review"
                            entry.stage_status = "optimize_timeout_fallback"
                            entry.error_msg = "optimize batch timed out"
                            if "optimize_timeout" not in entry.quality_flags:
                                entry.quality_flags.append("optimize_timeout")
                    except Exception as exc:
                        logger.error(f"Optimize batch failed: {exc}")
                        for entry in batch:
                            entry.optimized_lines = list(entry.lines)
                            entry.status = "needs_review"
                            entry.stage_status = "optimize_failed_fallback"
                            entry.error_msg = "optimize batch failed"
                            if "optimize_failed" not in entry.quality_flags:
                                entry.quality_flags.append("optimize_failed")
                    finally:
                        completed += len(batch)
                        if on_progress:
                            on_progress(min(completed, total), total)

            if on_log:
                on_log("  ✨ 第二轮：自然度与字幕表达润色")
            await asyncio.gather(*(
                process_batch(batch, prev, next_ctx)
                for batch, prev, next_ctx in batches_with_ctx
            ))

            logger.debug(
                _format_subtitle_snapshot("AFTER_OPTIMIZE", entries, use_optimized=True)
            )

            if on_log:
                on_log("  🌐 第三轮：构建全文术语/专名一致性规则并执行审计")
            await self._run_consistency_review(
                entries, context, glossary, session, on_log, is_cancelled
            )

        final_entries, lint_report = finalize_entries(
            entries,
            use_optimized=True,
            input_lang_code=self.input_lang_code,
            output_lang_code=LANGUAGE_MAP.get(
                self.output_lang, ("zh", self.output_lang)
            )[0],
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
        live_by_id = {entry.index: entry for entry in entries}
        total = len(snapshots)
        context_size = self.DEFAULT_CONTEXT_SIZE
        sem = asyncio.Semaphore(max(1, self.concurrency))
        corrected = 0
        failed_batches = 0
        counter_lock = asyncio.Lock()

        jobs = []
        for start in range(0, total, self.batch_size):
            end = min(total, start + self.batch_size)
            jobs.append((
                snapshots[start:end],
                snapshots[max(0, start - context_size):start],
                snapshots[end:min(total, end + context_size)],
            ))

        async def process(
            batch: list[SubtitleEntry],
            prev_ctx: list[SubtitleEntry],
            next_ctx: list[SubtitleEntry],
        ):
            nonlocal corrected, failed_batches
            if is_cancelled and is_cancelled():
                return
            async with sem:
                try:
                    result_map = await asyncio.wait_for(
                        fidelity_review_batch(
                            batch, context, glossary, self.client, session,
                            self.input_lang, self.output_lang, prev_ctx, next_ctx,
                        ),
                        timeout=self.fidelity_batch_timeout_seconds,
                    )
                    validated: list[tuple[SubtitleEntry, str, str, str]] = []
                    for snapshot in batch:
                        live = live_by_id[snapshot.index]
                        source_text = _get_source_text(snapshot, self.input_lang_code)
                        old_target = _get_target_text(snapshot, self.input_lang_code)
                        target = result_map.get(snapshot.index, "")
                        if not _translation_is_usable(
                            target, source_text, self.output_lang
                        ):
                            raise ValueError(
                                f"fidelity invalid target id={snapshot.index}"
                            )
                        validated.append((live, source_text, old_target, target))

                    local_corrected = 0
                    for live, source_text, old_target, target in validated:
                        live.lines = [target, source_text] if source_text else [target]
                        live.status = "done"
                        live.stage_status = "fidelity_checked"
                        live.error_msg = ""
                        if target != old_target:
                            local_corrected += 1
                    async with counter_lock:
                        corrected += local_corrected
                except Exception as exc:
                    logger.error("Fidelity review batch failed: %s", exc)
                    async with counter_lock:
                        failed_batches += 1
                    for snapshot in batch:
                        live = live_by_id[snapshot.index]
                        live.stage_status = "fidelity_failed_fallback"
                        if "fidelity_review_failed" not in live.quality_flags:
                            live.quality_flags.append("fidelity_review_failed")
                        source_text = _get_source_text(live, self.input_lang_code)
                        target_text = _get_target_text(live, self.input_lang_code)
                        if not _translation_is_usable(
                            target_text, source_text, self.output_lang
                        ):
                            live.status = "needs_review"
                            live.error_msg = "fidelity review failed and target invalid"

        await asyncio.gather(*(process(*job) for job in jobs))
        if on_log:
            on_log(
                f"  ✅ 忠实度审校完成：修正 {corrected} 条，失败批次 {failed_batches} 个"
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
        try:
            guide = await build_consistency_guide(
                entries, context, glossary, self.client, session,
                self.input_lang, self.output_lang,
                chunk_size=max(40, self.batch_size * 10),
                concurrency=max(1, min(4, self.concurrency)),
            )
        except Exception as exc:
            logger.error("Build consistency guide failed: %s", exc)
            for entry in entries:
                if "consistency_guide_failed" not in entry.quality_flags:
                    entry.quality_flags.append("consistency_guide_failed")
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
        live_by_id = {entry.index: entry for entry in entries}
        total = len(snapshots)
        context_size = self.DEFAULT_CONTEXT_SIZE
        sem = asyncio.Semaphore(max(1, self.concurrency))
        changed = 0
        failed_batches = 0
        counter_lock = asyncio.Lock()
        jobs = []
        for start in range(0, total, self.batch_size):
            end = min(total, start + self.batch_size)
            jobs.append((
                snapshots[start:end],
                snapshots[max(0, start - context_size):start],
                snapshots[end:min(total, end + context_size)],
            ))

        async def process(
            batch: list[SubtitleEntry],
            prev_ctx: list[SubtitleEntry],
            next_ctx: list[SubtitleEntry],
        ):
            nonlocal changed, failed_batches
            if is_cancelled and is_cancelled():
                return
            async with sem:
                try:
                    result_map = await asyncio.wait_for(
                        consistency_review_batch(
                            batch, guide, context, self.client, session,
                            self.input_lang, self.output_lang, prev_ctx, next_ctx,
                        ),
                        timeout=self.consistency_batch_timeout_seconds,
                    )
                    validated: list[tuple[SubtitleEntry, str, str, str]] = []
                    for snapshot in batch:
                        live = live_by_id[snapshot.index]
                        source_text = _get_source_text(snapshot, self.input_lang_code)
                        old_target = snapshot.lines[0] if snapshot.lines else ""
                        target = result_map.get(snapshot.index, "")
                        if not _translation_is_usable(
                            target, source_text, self.output_lang
                        ):
                            raise ValueError(
                                f"consistency invalid target id={snapshot.index}"
                            )
                        validated.append((live, source_text, old_target, target))

                    local_changed = 0
                    for live, source_text, old_target, target in validated:
                        live.optimized_lines = (
                            [target, source_text] if source_text else [target]
                        )
                        if live.status != "needs_review":
                            live.status = "done"
                            live.error_msg = ""
                        live.stage_status = (
                            "consistency_checked_with_flags"
                            if live.status == "needs_review" or live.quality_flags
                            else "consistency_checked"
                        )
                        if target != old_target:
                            local_changed += 1
                    async with counter_lock:
                        changed += local_changed
                except Exception as exc:
                    logger.error("Consistency review batch failed: %s", exc)
                    async with counter_lock:
                        failed_batches += 1
                    for snapshot in batch:
                        live = live_by_id[snapshot.index]
                        if "consistency_review_failed" not in live.quality_flags:
                            live.quality_flags.append("consistency_review_failed")
                        if live.stage_status == "optimized":
                            live.stage_status = "consistency_failed_fallback"

        await asyncio.gather(*(process(*job) for job in jobs))
        if on_log:
            on_log(
                f"  ✅ 全文一致性审计完成：统一修正 {changed} 条，失败批次 {failed_batches} 个"
            )

    async def _segment_batch(
        self,
        batch: list[SubtitleEntry],
        context: str,
        session: aiohttp.ClientSession,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> list[SubtitleEntry]:
        """LLM merge + split for one batch."""
        batch = await self._llm_merge(batch, context, session, on_log)

        short_entries, long_entries = _split_short_long(batch, lang_code=self.input_lang_code)
        if not long_entries:
            return short_entries

        split_results = await self._split_long_entries_with_llm(
            long_entries, context, session, on_log)
        return short_entries + split_results

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
            "2. 每组合并后的词数不超过25词（中文不超过40字）\n"
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

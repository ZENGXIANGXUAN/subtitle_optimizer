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
from core.segmenter import _get_source_text
from core.client import MistralClient
from core.workers import (
    LANGUAGE_MAP,
    _get_prompt_lang,
    _build_analysis_messages,
    _format_analysis_result,
    _apply_llm_merge,
    _apply_llm_split,
    _split_short_long,
    _translate_batch_with_repair,
    _repair_incomplete_segment_translations,
    _optimize_batch_impl,
)

try:
    from ..utils import logger
except ImportError:
    import sys
    import os
    sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
    from utils import logger


class SubtitlePipeline:
    """Pure business logic pipeline — no Qt dependencies.

    Accepts optional callbacks for progress, logging, and cancellation.
    """

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

    # ------------------------------------------------------------------
    # Stage 1: Analyze
    # ------------------------------------------------------------------
    async def analyze(
        self,
        entries: list[SubtitleEntry],
        on_progress: Optional[Callable[[str], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> str:
        if is_cancelled and is_cancelled():
            return ""

        if on_progress:
            on_progress("正在分析字幕内容，识别翻译情景...")

        messages = _build_analysis_messages(entries, self.input_lang, self.output_lang)

        async with aiohttp.ClientSession() as session:
            raw = await self.client.chat(messages, session)

        return _format_analysis_result(raw)

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
        is_native = sum(1 for e in entries if len(e.lines) == 1) > total * 0.5
        if is_native and on_log:
            on_log(f"  📋 检测到原生字幕，将自动翻译为{self.output_lang}")

        window = max(10, self.batch_size)
        batches = [entries[i:i + window] for i in range(0, total, window)]
        sem = asyncio.Semaphore(max(1, self.concurrency))
        completed = 0
        all_results: list[SubtitleEntry] = []

        async def process_batch(batch: list[SubtitleEntry]):
            nonlocal completed
            if is_cancelled and is_cancelled():
                return
            async with sem:
                if is_cancelled and is_cancelled():
                    return
                try:
                    async with aiohttp.ClientSession() as session:
                        results = await self._segment_batch(batch, context, session, on_log)
                    all_results.extend(results)
                except Exception as exc:
                    logger.warning(f"Segment batch failed: {exc}")
                    for e in batch:
                        fallback = SubtitleEntry(
                            e.index, e.start_time, e.end_time, list(e.lines))
                        fallback.source_indexes = list(e.source_indexes or [e.index])
                        all_results.append(fallback)
                finally:
                    completed += len(batch)
                    if on_progress:
                        on_progress(completed, total)

        await asyncio.gather(*[process_batch(b) for b in batches])

        # Deduplicate by index
        seen: dict[int, SubtitleEntry] = {}
        deduped: list[SubtitleEntry] = []
        duplicates: list[int] = []
        for e in all_results:
            if e.index in seen:
                duplicates.append(e.index)
                continue
            seen[e.index] = e
            deduped.append(e)

        # Sort by time, renumber
        deduped.sort(key=lambda e: _srt_time_to_ms(e.start_time))
        for i, entry in enumerate(deduped, start=1):
            entry.index = i

        # Verify source integrity
        before_words = sum(len(_get_source_text(e, self.input_lang_code).split()) for e in entries)
        after_words = sum(len(_get_source_text(e, self.input_lang_code).split()) for e in deduped)
        missing = verify_source_integrity(entries, deduped)
        status = "OK" if not missing and before_words == after_words else "FAILED"
        if on_log:
            on_log(
                f"  📊 重组完整性: {len(entries)}条/{before_words}词 → "
                f"{len(deduped)}条/{after_words}词 | "
                f"来源丢失: {sorted(missing) if missing else '无'} | "
                f"重复: {duplicates or '无'} | {status}"
            )

        logger.info(_format_subtitle_snapshot("AFTER_SEGMENT", deduped))
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

        sem = asyncio.Semaphore(self.concurrency)
        total = len(entries)
        completed = 0
        CONTEXT_SIZE = 3

        starts = list(range(0, total, self.batch_size))
        batches_with_ctx: list[tuple[list[SubtitleEntry], list[SubtitleEntry]]] = []
        for start in starts:
            batch = entries[start:start + self.batch_size]
            prev = entries[max(0, start - CONTEXT_SIZE):start]
            batches_with_ctx.append((batch, prev))

        async def process_batch(batch: list[SubtitleEntry], prev_ctx: list[SubtitleEntry]):
            nonlocal completed
            if is_cancelled and is_cancelled():
                return
            async with sem:
                if is_cancelled and is_cancelled():
                    return
                try:
                    async with aiohttp.ClientSession() as session:
                        result_map, unresolved = await _translate_batch_with_repair(
                            batch, context, glossary, self.client, session,
                            self.input_lang, self.output_lang, prev_ctx)
                    for entry in batch:
                        translation = result_map.get(entry.index, "")
                        source_text = _get_source_text(entry, self.input_lang_code)
                        if translation and source_text:
                            entry.lines = [translation, source_text]
                            entry.status = "done"
                            entry.error_msg = ""
                        else:
                            entry.status = "error"
                            if entry.index in unresolved:
                                entry.error_msg = "translation missing after retry"
                except Exception as exc:
                    logger.error(f"Translate batch failed: {exc}")
                    for entry in batch:
                        entry.status = "error"
                        entry.error_msg = "translation batch failed"
                finally:
                    completed += len(batch)
                    if on_progress:
                        on_progress(completed, total)

        await asyncio.gather(*[process_batch(b, ctx) for b, ctx in batches_with_ctx])

        # Fallback: unresolved entries keep source text
        unresolved_ids = [
            entry.index for entry in entries
            if entry.status == "error" and getattr(entry, "error_msg", "").startswith("translation")
        ]
        if unresolved_ids:
            logger.warning("Translation fallback to source for ids: %s", unresolved_ids)
            unresolved_set = set(unresolved_ids)
            for entry in entries:
                if entry.index in unresolved_set:
                    source_text = _get_source_text(entry, self.input_lang_code)
                    entry.lines = [source_text] if source_text else entry.lines
                    entry.status = "done"
                    entry.error_msg = "translation_fallback"
            if on_log:
                on_log(f"    ⚠️ {len(unresolved_ids)} 条字幕翻译失败，已保留原文（ID: {unresolved_ids}）")

        # 漏译修复
        if on_log:
            on_log("  🔧 检查并修复漏译条目...")
        async with aiohttp.ClientSession() as session:
            await _repair_incomplete_segment_translations(
                entries, context, glossary, self.client, session,
                self.input_lang, self.output_lang)
        if on_log:
            on_log("  ✅ 漏译修复完成")

        logger.info(_format_subtitle_snapshot("AFTER_TRANSLATE", entries))
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
        sem = asyncio.Semaphore(self.concurrency)
        total = len(entries)
        completed = 0
        CONTEXT_SIZE = 3

        starts = list(range(0, total, self.batch_size))
        batches_with_ctx: list[tuple[list[SubtitleEntry], list[SubtitleEntry]]] = []
        for start in starts:
            batch = entries[start:start + self.batch_size]
            prev = entries[max(0, start - CONTEXT_SIZE):start]
            batches_with_ctx.append((batch, prev))

        async def process_batch(batch: list[SubtitleEntry], prev_ctx: list[SubtitleEntry]):
            nonlocal completed
            if is_cancelled and is_cancelled():
                return
            async with sem:
                if is_cancelled and is_cancelled():
                    return
                try:
                    async with aiohttp.ClientSession() as session:
                        result_map = await _optimize_batch_impl(
                            batch, context, glossary, self.client, session,
                            self.input_lang, self.output_lang, prev_ctx)
                    for entry in batch:
                        if entry.index in result_map:
                            entry.optimized_lines = result_map[entry.index]
                            entry.status = "done"
                        else:
                            entry.status = "error"
                except Exception:
                    for entry in batch:
                        entry.status = "error"
                finally:
                    completed += len(batch)
                    if on_progress:
                        on_progress(completed, total)

        await asyncio.gather(*[process_batch(b, ctx) for b, ctx in batches_with_ctx])

        logger.info(_format_subtitle_snapshot("AFTER_OPTIMIZE", entries, use_optimized=True))
        return entries

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------
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

        items_json = json.dumps([
            {"id": e.index, "en": _get_source_text(e, self.input_lang_code),
             "start": e.start_time, "end": e.end_time}
            for e in long_entries
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

        response = await self.client.chat(messages, session)
        try:
            clean = re.sub(r'```(?:json)?|```', '', response).strip()
            data = json.loads(clean)
            split_results = _apply_llm_split(long_entries, data, log_fn=on_log, lang_code=self.input_lang_code)
        except Exception:
            split_results = [
                SubtitleEntry(e.index, e.start_time, e.end_time, list(e.lines))
                for e in long_entries
            ]

        return short_entries + split_results

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
            response = await self.client.chat(messages, session)
            clean = re.sub(r'```(?:json)?|```', '', response).strip()
            data = json.loads(clean)
            return _apply_llm_merge(batch, data, log_fn=on_log, lang_code=self.input_lang_code)
        except Exception as exc:
            logger.warning(f"LLM merge pass failed, keeping original batch: {exc}")
            return batch

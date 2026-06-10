# 字幕处理流水线统一重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统一单文件模式与批量模式的业务逻辑，消除 BatchWorker 中的重复代码，使未来修改只需改一处。

**Architecture:** 新增 `SubtitlePipeline` 类封装纯业务逻辑（analyze/segment/translate/optimize 4个阶段），单文件 Worker 和 BatchWorker 都委托给 Pipeline 调用。Worker 只负责 Qt Signal 发射和 UI 进度通知。

**Tech Stack:** Python 3.10+, PyQt6, aiohttp, asyncio

---

## File Structure

| 文件 | 操作 | 职责 |
|---|---|---|
| `subtitle_optimizer/core/pipeline.py` | **新建** | SubtitlePipeline 类：纯业务逻辑 |
| `subtitle_optimizer/core/workers.py` | **修改** | Worker 变壳，BatchWorker 删除重复方法 |

共用函数（`_translate_batch_impl`、`_optimize_batch_impl`、`_build_analysis_messages`、`_format_analysis_result`、`_apply_llm_merge`、`_apply_llm_split`、`_split_short_long`、`_translate_batch_with_repair`、`_repair_incomplete_segment_translations`、`_translation_is_usable`）保留在 `workers.py`，Pipeline import 它们。

---

### Task 1: 创建 pipeline.py — SubtitlePipeline 骨架 + analyze 方法

**Files:**
- Create: `subtitle_optimizer/core/pipeline.py`

- [ ] **Step 1: 创建 pipeline.py 文件，定义 SubtitlePipeline 类骨架和 analyze 方法**

```python
import asyncio
import aiohttp
import json
import re
from typing import Callable, Optional

from core.models import SubtitleEntry, _format_subtitle_snapshot
from core.segmenter import _get_source_text
from core.client import MistralClient

try:
    from ..utils import logger
except ImportError:
    import sys
    import os
    sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
    from utils import logger

from core.workers import (
    LANGUAGE_MAP,
    _get_prompt_lang,
    _build_analysis_messages,
    _format_analysis_result,
)


class SubtitlePipeline:
    """字幕处理流水线：纯业务逻辑，不依赖 Qt。"""

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
        self.input_lang_code = LANGUAGE_MAP.get(input_lang, ("en", input_lang))[0]
        self.concurrency = concurrency
        self.batch_size = batch_size

    async def analyze(
        self,
        entries: list[SubtitleEntry],
        on_progress: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> str:
        """阶段1：分析字幕确定翻译情景，返回 context 字符串。"""
        if is_cancelled and is_cancelled():
            raise asyncio.CancelledError()

        if on_progress:
            on_progress("正在分析字幕内容，识别翻译情景...")

        messages = _build_analysis_messages(entries, self.input_lang, self.output_lang)

        async with aiohttp.ClientSession() as session:
            raw = await self.client.chat(messages, session)

        logger.info("字幕分析完成")
        return _format_analysis_result(raw)
```

- [ ] **Step 2: 验证 import 正确**

Run: `cd subtitle_optimizer && python -c "from core.pipeline import SubtitlePipeline; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add subtitle_optimizer/core/pipeline.py
git commit -m "feat(pipeline): 创建 SubtitlePipeline 骨架，实现 analyze 方法"
```

---

### Task 2: Pipeline — 实现 segment 方法

**Files:**
- Modify: `subtitle_optimizer/core/pipeline.py`

- [ ] **Step 1: 添加 segment 方法所需的 import**

在 `pipeline.py` 顶部 import 区添加：

```python
from core.models import (
    SubtitleEntry,
    _format_subtitle_snapshot,
    _srt_time_to_ms,
    _source_text_length,
    verify_source_integrity,
    strip_annotation,
)
from core.segmenter import _get_source_text, _get_target_text
from core.workers import (
    LANGUAGE_MAP,
    _get_prompt_lang,
    _build_analysis_messages,
    _format_analysis_result,
    _apply_llm_merge,
    _apply_llm_split,
    _split_short_long,
)
```

- [ ] **Step 2: 添加 segment 方法**

在 `SubtitlePipeline` 类中添加：

```python
    async def segment(
        self,
        entries: list[SubtitleEntry],
        context: str,
        glossary: str,
        on_progress: Callable[[int, int], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> list[SubtitleEntry]:
        """阶段2：智能重组字幕 — LLM合并 + LLM语义切分长句。"""
        total = len(entries)
        is_native = sum(1 for e in entries if len(e.lines) == 1) > total * 0.5
        if is_native and on_log:
            on_log(f"  📋 检测到原生字幕，将自动翻译为{self.output_lang}")

        window = max(10, self.batch_size)
        batches = [entries[i:i + window] for i in range(0, total, window)]
        sem = asyncio.Semaphore(max(1, self.concurrency))
        completed = 0
        all_results = []

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
                except Exception as e:
                    if on_log:
                        on_log(f"    ⚠ 重组批次失败: {e}")
                    for e_item in batch:
                        fallback = SubtitleEntry(
                            e_item.index, e_item.start_time, e_item.end_time, list(e_item.lines))
                        fallback.source_indexes = list(e_item.source_indexes or [e_item.index])
                        all_results.append(fallback)
                finally:
                    completed += len(batch)
                    if on_progress:
                        on_progress(completed, total)

        await asyncio.gather(*[process_batch(b) for b in batches])

        # 去重
        seen = {}
        deduped = []
        duplicates = []
        for e_item in all_results:
            if e_item.index in seen:
                duplicates.append(e_item.index)
                continue
            seen[e_item.index] = e_item
            deduped.append(e_item)
        deduped.sort(key=lambda e: _srt_time_to_ms(e.start_time))
        for i, entry in enumerate(deduped, start=1):
            entry.index = i

        # 来源完整性校验
        before_words = sum(len(_get_source_text(e, self.input_lang_code).split()) for e in entries)
        after_words = sum(len(_get_source_text(e, self.input_lang_code).split()) for e in deduped)
        missing = verify_source_integrity(entries, deduped)
        status = "OK" if not missing and before_words == after_words else "FAILED"
        if on_log:
            on_log(f"  📊 重组完整性: {len(entries)}条/{before_words}词 → {len(deduped)}条/{after_words}词 | 来源丢失: {sorted(missing) if missing else '无'} | 重复: {duplicates or '无'} | {status}")

        logger.info(_format_subtitle_snapshot("AFTER_SEGMENT", deduped))
        return deduped

    async def _segment_batch(
        self,
        batch: list[SubtitleEntry],
        context: str,
        session: aiohttp.ClientSession,
        on_log: Callable[[str], None] | None = None,
    ) -> list[SubtitleEntry]:
        """单批次重组：LLM合并 → 短句直通/长句LLM切分。"""
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
            "无需切分时返回空列表：[{\"id\": 原id, \"break_after_word\": []}]\n\n"
            "示例：原文共50词，在第15词后、第30词后、第45词后各断一次：\n"
            '[{"id": 6, "break_after_word": [15, 30, 45]}]'
        )

        messages = [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": f"确定以下长字幕条目的断点位置：\n{items_json}"}
        ]

        response = await self.client.chat(messages, session)
        try:
            clean = re.sub(r'```(?:json)?|```', '', response).strip()
            data = json.loads(clean)
            split_results = _apply_llm_split(long_entries, data, log_fn=on_log, lang_code=self.input_lang_code)
        except Exception:
            split_results = [SubtitleEntry(e.index, e.start_time, e.end_time, list(e.lines))
                             for e in long_entries]

        return short_entries + split_results

    async def _llm_merge(
        self,
        batch: list[SubtitleEntry],
        context: str,
        session: aiohttp.ClientSession,
        on_log: Callable[[str], None] | None = None,
    ) -> list[SubtitleEntry]:
        """LLM合并pass：将语义不完整的相邻条目合并成完整句。"""
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
            "示例2：条目6是完整句，条目7是\"Okay.\"，条目8是新句的开头：\n"
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
        except Exception as e:
            logger.warning(f"LLM合并pass失败，保留原批次: {e}")
            return batch
```

- [ ] **Step 3: 验证 segment 方法可导入**

Run: `cd subtitle_optimizer && python -c "from core.pipeline import SubtitlePipeline; print(hasattr(SubtitlePipeline, 'segment'))"`
Expected: `True`

- [ ] **Step 4: Commit**

```bash
git add subtitle_optimizer/core/pipeline.py
git commit -m "feat(pipeline): 实现 segment 方法（LLM合并+切分+去重+校验）"
```

---

### Task 3: Pipeline — 实现 translate 方法

**Files:**
- Modify: `subtitle_optimizer/core/pipeline.py`

- [ ] **Step 1: 添加 translate 所需 import**

在 `pipeline.py` import 区添加：

```python
from core.workers import (
    # ... 已有 import ...
    _translate_batch_with_repair,
    _repair_incomplete_segment_translations,
)
```

- [ ] **Step 2: 添加 translate 方法**

在 `SubtitlePipeline` 类中添加：

```python
    async def translate(
        self,
        entries: list[SubtitleEntry],
        context: str,
        glossary: str,
        on_progress: Callable[[int, int], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> list[SubtitleEntry]:
        """阶段3：翻译字幕 + 漏译修复。"""
        # 检测是否需要翻译
        if self.input_lang == self.output_lang:
            if on_log:
                on_log("⏭️ 输入输出语言相同，跳过翻译，仅优化")
            return entries

        sem = asyncio.Semaphore(self.concurrency)
        total = len(entries)
        completed = 0
        CONTEXT_SIZE = 3

        starts = list(range(0, total, self.batch_size))
        batches_with_ctx = []
        for start in starts:
            batch = entries[start:start + self.batch_size]
            prev = entries[max(0, start - CONTEXT_SIZE):start]
            batches_with_ctx.append((batch, prev))

        async def process_batch(batch, prev_ctx):
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
                except Exception as e:
                    logger.error(f"翻译批次失败: {e}")
                    for entry in batch:
                        entry.status = "error"
                        entry.error_msg = "translation batch failed"
                finally:
                    completed += len(batch)
                    if on_progress:
                        on_progress(completed, total)

        await asyncio.gather(*[process_batch(b, ctx) for b, ctx in batches_with_ctx])

        # 兜底：翻译失败的条目保留原文
        unresolved_ids = [
            entry.index for entry in entries
            if entry.status == "error" and entry.error_msg.startswith("translation")
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
        async with aiohttp.ClientSession() as session:
            await _repair_incomplete_segment_translations(
                entries, context, glossary, self.client, session,
                self.input_lang, self.output_lang)

        return entries
```

- [ ] **Step 3: 验证 translate 方法可导入**

Run: `cd subtitle_optimizer && python -c "from core.pipeline import SubtitlePipeline; print(hasattr(SubtitlePipeline, 'translate'))"`
Expected: `True`

- [ ] **Step 4: Commit**

```bash
git add subtitle_optimizer/core/pipeline.py
git commit -m "feat(pipeline): 实现 translate 方法（翻译+漏译修复+兜底）"
```

---

### Task 4: Pipeline — 实现 optimize 方法

**Files:**
- Modify: `subtitle_optimizer/core/pipeline.py`

- [ ] **Step 1: 添加 optimize 所需 import**

在 `pipeline.py` import 区添加（如已有则跳过）：

```python
from core.workers import (
    # ... 已有 import ...
    _optimize_batch_impl,
)
```

- [ ] **Step 2: 添加 optimize 方法**

在 `SubtitlePipeline` 类中添加：

```python
    async def optimize(
        self,
        entries: list[SubtitleEntry],
        context: str,
        glossary: str,
        on_progress: Callable[[int, int], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> list[SubtitleEntry]:
        """阶段4：优化润色字幕。"""
        sem = asyncio.Semaphore(self.concurrency)
        total = len(entries)
        completed = 0
        CONTEXT_SIZE = 3

        starts = list(range(0, total, self.batch_size))
        batches_with_ctx = []
        for start in starts:
            batch = entries[start:start + self.batch_size]
            prev = entries[max(0, start - CONTEXT_SIZE):start]
            batches_with_ctx.append((batch, prev))

        async def process_batch(batch, prev_ctx):
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
        return entries
```

- [ ] **Step 3: 验证所有方法可导入**

Run: `cd subtitle_optimizer && python -c "from core.pipeline import SubtitlePipeline; print([m for m in dir(SubtitlePipeline) if not m.startswith('_')])"`
Expected: `['analyze', 'optimize', 'segment', 'translate']`

- [ ] **Step 4: Commit**

```bash
git add subtitle_optimizer/core/pipeline.py
git commit -m "feat(pipeline): 实现 optimize 方法，Pipeline 4个阶段全部完成"
```

---

### Task 5: 重构 AnalysisWorker → 调用 Pipeline

**Files:**
- Modify: `subtitle_optimizer/core/workers.py:382-420`

- [ ] **Step 1: 修改 AnalysisWorker 类**

将 `workers.py` 中的 `AnalysisWorker` 类（第382-420行）替换为：

```python
class AnalysisWorker(BaseWorker):
    """第一阶段：分析字幕确定翻译情景"""
    finished = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, client: MistralClient, entries: list[SubtitleEntry],
                 input_lang: str = "English", output_lang: str = "中文"):
        super().__init__()
        self.client = client
        self.entries = entries
        self.input_lang = input_lang
        self.output_lang = output_lang

    @pyqtSlot()
    def run(self):
        self._execute(self._run())

    async def _run(self):
        from core.pipeline import SubtitlePipeline
        try:
            pipeline = SubtitlePipeline(self.client, self.input_lang, self.output_lang)
            result = await pipeline.analyze(
                self.entries,
                on_progress=self.progress.emit,
                is_cancelled=lambda: self._cancelled,
            )
            self.finished.emit(result)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            error_msg = f"字幕分析失败: {str(e)}"
            logger.error(error_msg, exc_info=True)
            self.error.emit(error_msg)
```

- [ ] **Step 2: 验证 UI 仍能正常创建 AnalysisWorker**

Run: `cd subtitle_optimizer && python -c "from core.workers import AnalysisWorker; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add subtitle_optimizer/core/workers.py
git commit -m "refactor(workers): AnalysisWorker 改为调用 Pipeline.analyze()"
```

---

### Task 6: 重构 SegmentWorker → 调用 Pipeline

**Files:**
- Modify: `subtitle_optimizer/core/workers.py:928-1118`

- [ ] **Step 1: 将 SegmentWorker 类替换为薄壳版本**

将 `workers.py` 中的 `SegmentWorker` 类（第928-1118行）替换为：

```python
class SegmentWorker(BaseWorker):
    """第二阶段：智能重组字幕"""
    progress = pyqtSignal(int, int)
    all_done = pyqtSignal(object)  # list[SubtitleEntry]
    log = pyqtSignal(str)

    def __init__(self, client: MistralClient, entries: list[SubtitleEntry],
                 context: str, glossary: str, concurrency: int = 4, batch_size: int = 12,
                 input_lang: str = "English", output_lang: str = "中文"):
        super().__init__()
        self.client = client
        self.entries = entries
        self.context = context
        self.glossary = glossary
        self.concurrency = concurrency
        self.batch_size = batch_size
        self.input_lang = input_lang
        self.output_lang = output_lang

    @pyqtSlot()
    def run(self):
        self._execute(self._run())

    async def _run(self):
        from core.pipeline import SubtitlePipeline
        try:
            pipeline = SubtitlePipeline(
                self.client, self.input_lang, self.output_lang,
                self.concurrency, self.batch_size)
            result = await pipeline.segment(
                self.entries, self.context, self.glossary,
                on_progress=self.progress.emit,
                on_log=self.log.emit,
                is_cancelled=lambda: self._cancelled,
            )
            self.all_done.emit(result)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.error.emit(f"SegmentWorker 异常: {e}")
```

- [ ] **Step 2: 验证 SegmentWorker 可导入**

Run: `cd subtitle_optimizer && python -c "from core.workers import SegmentWorker; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add subtitle_optimizer/core/workers.py
git commit -m "refactor(workers): SegmentWorker 改为调用 Pipeline.segment()"
```

---

### Task 7: 重构 TranslateWorker → 调用 Pipeline

**Files:**
- Modify: `subtitle_optimizer/core/workers.py`

- [ ] **Step 1: 将 TranslateWorker 类替换为薄壳版本**

将 `workers.py` 中的 `TranslateWorker` 类替换为：

```python
class TranslateWorker(BaseWorker):
    """第三阶段：翻译字幕"""
    entry_done = pyqtSignal(int, list, str)
    all_done = pyqtSignal()
    progress = pyqtSignal(int, int)
    log = pyqtSignal(str)

    def __init__(self, client: MistralClient, entries: list[SubtitleEntry],
                 context: str, glossary: str, concurrency: int = 12, batch_size: int = 10,
                 input_lang: str = "English", output_lang: str = "中文"):
        super().__init__()
        self.client = client
        self.entries = entries
        self.context = context
        self.glossary = glossary
        self.concurrency = concurrency
        self.batch_size = batch_size
        self.input_lang = input_lang
        self.output_lang = output_lang

    @pyqtSlot()
    def run(self):
        self._execute(self._run())

    async def _run(self):
        from core.pipeline import SubtitlePipeline
        try:
            pipeline = SubtitlePipeline(
                self.client, self.input_lang, self.output_lang,
                self.concurrency, self.batch_size)
            result = await pipeline.translate(
                self.entries, self.context, self.glossary,
                on_progress=self.progress.emit,
                on_log=self.log.emit,
                is_cancelled=lambda: self._cancelled,
            )
            # 发射 entry_done 信号（保持 UI 兼容）
            for entry in result:
                self.entry_done.emit(entry.index, entry.lines, entry.status or "done")
            self.all_done.emit()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.error.emit(f"TranslateWorker 异常: {e}")
```

注意：原 `TranslateWorker` 没有 `log` signal，但 `main_window.py` 中可能没有连接它。新增 `log` signal 保持与 `SegmentWorker` 一致，不影响现有 UI。

- [ ] **Step 2: 验证 TranslateWorker 可导入**

Run: `cd subtitle_optimizer && python -c "from core.workers import TranslateWorker; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add subtitle_optimizer/core/workers.py
git commit -m "refactor(workers): TranslateWorker 改为调用 Pipeline.translate()"
```

---

### Task 8: 重构 OptimizeWorker → 调用 Pipeline

**Files:**
- Modify: `subtitle_optimizer/core/workers.py`

- [ ] **Step 1: 将 OptimizeWorker 类替换为薄壳版本**

将 `workers.py` 中的 `OptimizeWorker` 类替换为：

```python
class OptimizeWorker(BaseWorker):
    """第四阶段：优化润色字幕"""
    entry_done = pyqtSignal(int, list, str)
    all_done = pyqtSignal()
    progress = pyqtSignal(int, int)
    log = pyqtSignal(str)

    def __init__(self, client: MistralClient, entries: list[SubtitleEntry],
                 context: str, glossary: str, concurrency: int = 12, batch_size: int = 5,
                 input_lang: str = "English", output_lang: str = "中文"):
        super().__init__()
        self.client = client
        self.entries = entries
        self.context = context
        self.glossary = glossary
        self.concurrency = concurrency
        self.batch_size = batch_size
        self.input_lang = input_lang
        self.output_lang = output_lang

    def cancel(self):
        super().cancel()

    @pyqtSlot()
    def run(self):
        self._execute(self._run())

    async def _run(self):
        from core.pipeline import SubtitlePipeline
        try:
            pipeline = SubtitlePipeline(
                self.client, self.input_lang, self.output_lang,
                self.concurrency, self.batch_size)
            result = await pipeline.optimize(
                self.entries, self.context, self.glossary,
                on_progress=self.progress.emit,
                on_log=self.log.emit,
                is_cancelled=lambda: self._cancelled,
            )
            logger.info(_format_subtitle_snapshot("AFTER_OPTIMIZE", result, use_optimized=True))
            self.all_done.emit()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.error.emit(f"OptimizeWorker 异常: {e}")
```

- [ ] **Step 2: 验证 OptimizeWorker 可导入**

Run: `cd subtitle_optimizer && python -c "from core.workers import OptimizeWorker; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add subtitle_optimizer/core/workers.py
git commit -m "refactor(workers): OptimizeWorker 改为调用 Pipeline.optimize()"
```

---

### Task 9: 重构 BatchWorker → 删除重复方法，调用 Pipeline

**Files:**
- Modify: `subtitle_optimizer/core/workers.py`

- [ ] **Step 1: 修改 BatchWorker 的 `_run` 方法，使用 Pipeline**

将 `BatchWorker._run` 方法（从 `async def _run(self):` 开始到该方法结束）替换为：

```python
    async def _run(self):
        from core.pipeline import SubtitlePipeline
        pipeline = SubtitlePipeline(
            self.client, self.input_lang, self.output_lang,
            self.concurrency, self.batch_size)

        for idx, filepath in enumerate(self.files):
            if self._cancelled:
                self.log.emit("⏹ 处理已取消")
                break
            filename = Path(filepath).name
            self.file_started.emit(idx, filename)
            self.log.emit(f"📂 [{filename}] 开始处理...")
            try:
                with open(filepath, "r", encoding="utf-8-sig") as f:
                    content = f.read()
                entries = parse_srt(content)
                if not entries:
                    self.file_error.emit(idx, filename, "无法解析字幕文件")
                    continue

                # 自动检测双语字幕并提取原文行
                if entries and len(entries[0].lines) > 1:
                    original_count = len(entries)
                    entries = self._extract_original_line(entries)
                    line_num = self.original_line + 1
                    self.log.emit(f"  📋 [{filename}] 检测到双语字幕（{original_count}条），已提取原文行（第{line_num}行）")

                self.log.emit(f"  🔍 [{filename}] 分析情景（{len(entries)} 条字幕）...")
                context = await pipeline.analyze(
                    entries,
                    is_cancelled=lambda: self._cancelled,
                )
                self.file_analysis_done.emit(idx, filename, context)
                self.log.emit(f"  ✅ [{filename}] 情景分析完成")

                if self._cancelled:
                    break

                self.log.emit(f"  🔗 [{filename}] 开始智能重组 {len(entries)} 条字幕...")
                entries = await pipeline.segment(
                    entries, context, self.glossary_prompt,
                    on_progress=lambda c, t: self.file_progress.emit(idx, c, t),
                    on_log=self.log.emit,
                    is_cancelled=lambda: self._cancelled,
                )
                write_phase_snapshot(filename, "AFTER_SEGMENT", entries)
                self.log.emit(f"  ✅ [{filename}] 重组完成：{len(entries)} 条字幕")

                if self._cancelled:
                    break

                self.log.emit(f"  🌐 [{filename}] 开始翻译 {len(entries)} 条字幕...")
                entries = await pipeline.translate(
                    entries, context, self.glossary_prompt,
                    on_progress=lambda c, t: self.file_progress.emit(idx, c, t),
                    on_log=self.log.emit,
                    is_cancelled=lambda: self._cancelled,
                )
                write_phase_snapshot(filename, "AFTER_TRANSLATE", entries)
                self.log.emit(f"  ✅ [{filename}] 翻译完成")

                if self._cancelled:
                    break

                self.log.emit(f"  ✨ [{filename}] 开始优化润色 {len(entries)} 条字幕...")
                entries = await pipeline.optimize(
                    entries, context, self.glossary_prompt,
                    on_progress=lambda c, t: self.file_progress.emit(idx, c, t),
                    on_log=self.log.emit,
                    is_cancelled=lambda: self._cancelled,
                )
                write_phase_snapshot(filename, "AFTER_OPTIMIZE", entries, use_optimized=True)

                if self._cancelled:
                    break

                out_path = self._get_out_path(filepath)
                blocks = [e.to_srt_block(use_optimized=True, target_first=self.target_first) for e in entries]
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(blocks))
                self.file_done.emit(idx, filename, out_path)

            except Exception as e:
                self.file_error.emit(idx, filename, str(e))
```

- [ ] **Step 2: 删除 BatchWorker 中的重复方法**

删除以下方法（保留 `_extract_original_line` 和 `_get_out_path`）：
- `_analyze` 方法
- `_segment` 方法
- `_segment_batch` 方法
- `_llm_merge` 方法
- `_translate` 方法
- `_optimize` 方法
- `_optimize_batch` 方法

- [ ] **Step 3: 验证 BatchWorker 可导入**

Run: `cd subtitle_optimizer && python -c "from core.workers import BatchWorker; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add subtitle_optimizer/core/workers.py
git commit -m "refactor(workers): BatchWorker 删除重复方法，改用 Pipeline 调用"
```

---

### Task 10: 清理 workers.py 中不再需要的私有方法

**Files:**
- Modify: `subtitle_optimizer/core/workers.py`

- [ ] **Step 1: 确认哪些函数仍被 Pipeline import 使用**

以下函数保留在 `workers.py`（Pipeline 和其他模块 import 它们）：
- `_build_analysis_messages`
- `_format_analysis_result`
- `_optimize_batch_impl`
- `_translate_batch_impl`
- `_repair_incomplete_segment_translations`
- `_translate_batch_with_repair`
- `_translation_is_usable`
- `_apply_llm_merge`
- `_apply_llm_split`
- `_split_short_long`
- `_run_loop`
- `_get_prompt_lang`
- `LANGUAGE_MAP`
- `LANGUAGE_LABELS`
- `LANG_CODE_TO_PROMPT`

确认没有遗漏后，不需要删除任何共用函数——它们仍被 Pipeline import 使用。

- [ ] **Step 2: 检查 workers.py 中是否有仅被已删除方法调用的函数**

Run: `cd subtitle_optimizer && python -c "
from core.workers import (
    LANGUAGE_MAP, LANGUAGE_LABELS, LANG_CODE_TO_PROMPT,
    _run_loop, _get_prompt_lang, _build_analysis_messages,
    _format_analysis_result, _optimize_batch_impl, _translate_batch_impl,
    _repair_incomplete_segment_translations, _translate_batch_with_repair,
    _translation_is_usable, _apply_llm_merge, _apply_llm_split, _split_short_long,
    BaseWorker, AnalysisWorker, OptimizeWorker, SegmentWorker, TranslateWorker, BatchWorker,
)
print('All imports OK')
"`
Expected: `All imports OK`

- [ ] **Step 3: Commit（如有变更）**

```bash
git add subtitle_optimizer/core/workers.py
git commit -m "chore(workers): 确认共用函数保留完整，无遗漏删除"
```

---

### Task 11: 端到端验证 — 单文件模式与批量模式输出一致

**Files:**
- Test: 手动验证

- [ ] **Step 1: 准备测试字幕文件**

找一个已有的 `.srt` 测试文件（如 `logs/` 目录下的某个文件），或创建一个简单的测试字幕：

```srt
1
00:00:01,000 --> 00:00:03,000
Hello, welcome to this tutorial.

2
00:00:03,500 --> 00:00:06,000
Today we will learn about trading.

3
00:00:06,500 --> 00:00:09,000
Let me show you the first strategy.
```

- [ ] **Step 2: 使用单文件模式处理**

启动应用，用单文件模式处理测试文件，记录输出结果。

- [ ] **Step 3: 使用批量模式处理**

用批量模式处理同一测试文件，记录输出结果。

- [ ] **Step 4: 对比两种模式的输出**

对比两次输出的 `.srt` 文件内容。关键检查点：
- 字幕条目数量相同
- 每条的翻译内容相同
- 每条的时间戳相同
- 优化结果相同

如有差异，定位原因并修复。

- [ ] **Step 5: Commit（如有修复）**

```bash
git add -A
git commit -m "fix: 修复单文件/批量模式输出不一致问题"
```

---

### Task 12: 最终清理与确认

**Files:**
- Modify: `subtitle_optimizer/core/workers.py`
- Modify: `subtitle_optimizer/core/pipeline.py`

- [ ] **Step 1: 确认 workers.py 代码行数减少**

Run: `cd subtitle_optimizer && wc -l subtitle_optimizer/core/workers.py`

预期：从 ~1653 行减少到 ~800-900 行（删除了约 700-800 行重复业务逻辑）。

- [ ] **Step 2: 确认 pipeline.py 代码行数**

Run: `cd subtitle_optimizer && wc -l subtitle_optimizer/core/pipeline.py`

预期：~300-400 行。

- [ ] **Step 3: 运行现有测试（如有）**

Run: `cd subtitle_optimizer && python -m pytest tests/ -v 2>&1 || echo "No tests found"`

- [ ] **Step 4: 最终 Commit**

```bash
git add -A
git commit -m "refactor: 字幕处理流水线统一重构完成 - Pipeline 模式"
```

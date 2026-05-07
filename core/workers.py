import json
import re
import asyncio
import aiohttp
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from core.models import SubtitleEntry, parse_srt, strip_annotation
from core.client import MistralClient

# 尝试相对导入，如果失败则使用绝对导入
try:
    from ..utils import logger
except ImportError:
    import sys
    import os

    sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
    from utils import logger


def _run_loop(coro):
    """
    在新的事件循环中运行协程，并确保安全关闭。
    修复 Windows 上多线程并发创建 asyncio 事件循环导致的
    堆内存损坏崩溃 (0xC0000409)。
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(coro)
    finally:
        try:
            # 取消所有未完成的任务，防止资源泄漏
            pending = asyncio.all_tasks(loop)
            if pending:
                for task in pending:
                    task.cancel()
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            loop.close()
            # 清除线程本地的事件循环引用，避免被其他线程误用
            asyncio.set_event_loop(None)


class AnalysisWorker(QObject):
    """第一阶段：分析字幕确定翻译情景"""
    finished = pyqtSignal(str)  # 情景描述
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, client: MistralClient, entries: list[SubtitleEntry]):
        super().__init__()
        self.client = client
        self.entries = entries
        logger.debug(f"AnalysisWorker初始化完成，待处理条目数: {len(entries)}")

    @pyqtSlot()
    def run(self):
        logger.info("开始执行字幕分析任务")
        try:
            _run_loop(self._run())
        except Exception as e:
            error_msg = f"AnalysisWorker线程异常: {e}"
            logger.error(error_msg, exc_info=True)
            self.error.emit(error_msg)
        finally:
            logger.info("AnalysisWorker线程关闭")

    async def _run(self):
        logger.debug("开始分析字幕内容")
        # 取前30条作为样本
        sample = self.entries[:30]
        sample_text = "\n".join(
            f"[{e.index}] {e.chinese} | {e.english}"
            for e in sample
        )

        self.progress.emit("正在分析字幕内容，识别翻译情景...")
        logger.info("正在分析字幕内容，识别翻译情景...")

        messages = [
            {
                "role": "system",
                "content": (
                    "你是专业的字幕翻译顾问。分析双语字幕样本后，严格返回如下JSON，不加任何解释和代码块标记：\n"
                    '{"context": "...", "style_guide": "..."}\n\n'
                    "字段说明：\n"
                    "context：200字以内，描述内容领域、主题、受众、语言风格、常见翻译问题。\n"
                    "style_guide：150字以内，直接告诉翻译AI怎么译——用什么人称语气、"
                    "哪些高频词该怎么处理、句子长短节奏、需要避免什么。"
                    "要具体可执行，举1-2个这批字幕里的真实词汇作例子，适用于该领域任何视频。"
                )
            },
            {
                "role": "user",
                "content": f"字幕样本：\n{sample_text}"
            }
        ]

        try:
            async with aiohttp.ClientSession() as session:
                raw = await self.client.chat(messages, session)
            logger.info("字幕分析完成")
            # 解析 JSON，提取 context 和 style_guide，拼成统一字符串传递给下游
            try:
                clean_raw = re.sub(r'```(?:json)?|```', '', raw).strip()
                parsed = json.loads(clean_raw)
                context = parsed.get("context", raw)
                style_guide = parsed.get("style_guide", "")
                if style_guide:
                    result = f"{context}\n\n【译文风格指引】{style_guide}"
                else:
                    result = context
            except Exception:
                result = raw  # 解析失败降级，原逻辑不受影响
            self.finished.emit(result)
        except Exception as e:
            error_msg = f"字幕分析失败: {str(e)}"
            logger.error(error_msg, exc_info=True)
            self.error.emit(error_msg)


class OptimizeWorker(QObject):
    """第二阶段/第三阶段：并发优化字幕"""
    entry_done = pyqtSignal(int, list, str)  # index, optimized_lines, status
    all_done = pyqtSignal()
    error = pyqtSignal(str)
    progress = pyqtSignal(int, int)  # current, total

    def __init__(self, client: MistralClient, entries: list[SubtitleEntry],
                 context: str, glossary: str, concurrency: int = 12, batch_size: int = 5):
        super().__init__()
        self.client = client
        self.entries = entries
        self.context = context
        self.glossary = glossary
        self.concurrency = concurrency
        self.batch_size = batch_size
        self._cancelled = False
        logger.debug(
            f"OptimizeWorker初始化完成，待处理条目数: {len(entries)}, 并发数: {concurrency}, 批次大小: {batch_size}")

    def cancel(self):
        self._cancelled = True
        logger.info("OptimizeWorker任务已取消")

    @pyqtSlot()
    def run(self):
        logger.info("开始执行字幕优化任务")
        try:
            _run_loop(self._run())
        except Exception as e:
            error_msg = f"OptimizeWorker线程异常: {e}"
            logger.error(error_msg, exc_info=True)
            self.error.emit(error_msg)
        finally:
            logger.info("OptimizeWorker线程关闭")

    async def _run(self):
        logger.debug("开始批量处理字幕优化")
        sem = asyncio.Semaphore(self.concurrency)
        total = len(self.entries)
        completed = 0
        CONTEXT_SIZE = 3  # 跨批次携带的上文条数

        starts = list(range(0, total, self.batch_size))
        # 每批次附带其前 CONTEXT_SIZE 条作为上下文（不要求 AI 返回这些条目）
        batches_with_ctx: list[tuple[list[SubtitleEntry], list[SubtitleEntry]]] = []
        for start in starts:
            batch = self.entries[start:start + self.batch_size]
            prev = self.entries[max(0, start - CONTEXT_SIZE):start]
            batches_with_ctx.append((batch, prev))

        logger.info(f"准备处理 {len(batches_with_ctx)} 个批次，总共 {total} 条字幕")

        async def process_batch(batch: list[SubtitleEntry], prev_ctx: list[SubtitleEntry]):
            nonlocal completed
            if self._cancelled:
                logger.info(f"批次处理被取消，跳过批次（共{len(batch)}条）")
                return
            async with sem:
                if self._cancelled:
                    logger.info(f"批次处理被取消，跳过批次（共{len(batch)}条）")
                    return
                try:
                    logger.debug(f"开始处理批次，包含 {len(batch)} 条字幕")
                    async with aiohttp.ClientSession() as session:
                        result_map = await self._optimize_batch(batch, session, prev_ctx)
                    # 按 entry.index 对齐，不用 zip，绝不漏条
                    for entry in batch:
                        opt_lines = result_map.get(entry.index, entry.lines)
                        status = "done" if entry.index in result_map else "error"
                        if status == "error":
                            logger.warning(f"字幕条目 {entry.index} 优化失败")
                        self.entry_done.emit(entry.index, opt_lines, status)
                        completed += 1
                        self.progress.emit(completed, total)
                    logger.debug(f"批次处理完成，进度: {completed}/{total}")
                except Exception as e:
                    logger.error(f"批次处理失败: {str(e)}")
                    for entry in batch:
                        self.entry_done.emit(entry.index, entry.lines, "error")
                        completed += 1
                        self.progress.emit(completed, total)

        tasks = [process_batch(b, ctx) for b, ctx in batches_with_ctx]
        await asyncio.gather(*tasks)
        logger.info("所有字幕优化任务完成")
        self.all_done.emit()

    async def _optimize_batch(self, batch: list[SubtitleEntry],
                              session: aiohttp.ClientSession,
                              prev_ctx: list[SubtitleEntry] | None = None) -> dict[int, list[str]]:
        """
        返回 {entry.index: opt_lines} 字典，而非列表。
        调用方按 index 取值，AI 漏返哪条都不会静默丢失。
        prev_ctx 为上一批末尾若干条（只作上下文参考，不要求 AI 返回）。
        """
        items_json = json.dumps([
            {"id": e.index, "zh": e.chinese, "en": e.english}
            for e in batch
        ], ensure_ascii=False)

        system_content = (
            f"你是专业字幕翻译优化师。\n"
            f"【背景与风格】\n{self.context}\n\n"
            "任务：优化双语字幕的中文翻译，使其符合上述背景和风格指引，自然准确。\n"
            "规则：\n"
            "1. 保持与英文原意一致\n"
            "2. 严格遵循【译文风格指引】的语气、用词和节奏要求（如有）\n"
            "3. 【强制】凡英文原文中出现【术语表】中的词，zh字段必须使用术语表规定的译法，不得自行替换\n"
            "4. 专业术语保持一致性\n"
            "5. 【严禁】zh字段中禁止出现任何Markdown格式符号，包括但不限于：**加粗**、*斜体*、`代码`、#标题、>引用、-列表等，只允许纯文本\n"
            "6. 严格按JSON格式返回，不要加任何解释\n"
            "7. 【严禁】不得合并字幕条目，输入几条必须返回几条，id一一对应\n"
            "8. 【严禁】zh字段只能包含翻译正文，不得添加任何括号注释、标注或说明文字\n"
            "9. 【重要】若某条字幕没有中文（zh为空、None或缺失），必须根据en字段直接翻译成自然中文，填入zh字段，不得留空\n"
            "10. 【严禁】en字段必须与输入完全一致，一字不差原样返回，绝对不得修改、合并、润色或截断英文原文\n\n"
        )

        if self.glossary:
            system_content += f"【术语表】（强制执行，出现必须使用）\n{self.glossary}\n\n"

        system_content += "返回格式：[{\"id\": 1, \"zh\": \"优化后中文\", \"en\": \"英文原文原样返回\"}]"

        # 构造 user 消息：若有上文则先附带，保持术语/风格跨批一致
        if prev_ctx:
            ctx_json = json.dumps([
                {"id": e.index, "zh": e.chinese, "en": e.english}
                for e in prev_ctx
            ], ensure_ascii=False)
            user_content = (
                f"【上文参考（仅供风格/术语对齐，无需返回这些条目）】\n{ctx_json}\n\n"
                f"【待优化字幕】\n{items_json}"
            )
        else:
            user_content = f"优化以下字幕：\n{items_json}"

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content}
        ]

        response = await self.client.chat(messages, session)

        try:
            clean = re.sub(r'```(?:json)?|```', '', response).strip()
            data = json.loads(clean)
            api_map = {item["id"]: item for item in data}

            # 第一遍：找出 zh 缺失/无效的条目
            first_pass = []
            missing_zh_entries = []
            for entry in batch:
                if entry.index in api_map:
                    item = api_map[entry.index]
                    zh_raw = item.get("zh", "")
                    zh = strip_annotation(str(zh_raw).strip()) if zh_raw else ""
                    zh_valid = bool(zh) and zh.lower() not in ("none", "null", "无", "—", "-")
                    first_pass.append((entry, zh, zh_valid))
                    if not zh_valid:
                        missing_zh_entries.append(entry)
                else:
                    # ✅ AI 漏返了这条：记录下来，走二次补翻
                    first_pass.append((entry, "", False))
                    missing_zh_entries.append(entry)

            # 二次补翻译：对 zh 缺失/无效/漏返的条目单独请求
            retry_map: dict[int, str] = {}
            if missing_zh_entries:
                retry_items = json.dumps([
                    {"id": e.index, "en": e.english}
                    for e in missing_zh_entries
                ], ensure_ascii=False)

                retry_sys = (
                    f"你是专业字幕翻译师。\n【背景与风格】\n{self.context}\n\n"
                    "任务：将以下英文字幕翻译成中文，严格遵循上述风格指引（如有）。\n"
                    "规则：\n"
                    "1. 用词和语气与风格指引保持一致\n"
                    "2. 严格按JSON格式返回，不要加任何解释\n"
                    "3. 每条必须有zh字段，不得为空\n\n"
                )
                if self.glossary:
                    retry_sys += f"【术语表】（强制执行，出现必须使用）\n{self.glossary}\n\n"
                retry_sys += "返回格式：[{\"id\": 1, \"zh\": \"中文翻译\"}]"

                retry_messages = [
                    {"role": "system", "content": retry_sys},
                    {"role": "user", "content": f"翻译以下字幕：\n{retry_items}"}
                ]
                try:
                    retry_response = await self.client.chat(retry_messages, session)
                    retry_clean = re.sub(r'```(?:json)?|```', '', retry_response).strip()
                    retry_data = json.loads(retry_clean)
                    retry_map = {item["id"]: item.get("zh", "") for item in retry_data}
                except Exception:
                    pass

            # 组装最终结果为 {index: opt_lines} 字典
            result: dict[int, list[str]] = {}
            for entry, zh, zh_valid in first_pass:
                opt_lines = []
                if zh_valid:
                    opt_lines.append(zh)
                elif entry.index in retry_map:
                    fallback_zh = strip_annotation(str(retry_map[entry.index]).strip())
                    if fallback_zh and fallback_zh.lower() not in ("none", "null"):
                        opt_lines.append(fallback_zh)

                en = entry.english
                if en:
                    opt_lines.append(en)

                result[entry.index] = opt_lines if opt_lines else entry.lines

            return result

        except Exception:
            # JSON 解析彻底失败：所有条目 fallback 到原文
            return {e.index: e.lines for e in batch}


class CleanWorker(QObject):
    """专项重翻 None 条目 Worker"""
    entry_done = pyqtSignal(int, str)
    all_done = pyqtSignal(int)
    progress = pyqtSignal(int, int)
    error = pyqtSignal(str)

    def __init__(self, client: MistralClient, entries: list[SubtitleEntry],
                 context: str, glossary: str, concurrency: int = 12, batch_size: int = 10):
        super().__init__()
        self.client = client
        self.entries = entries
        self.context = context
        self.glossary = glossary
        self.concurrency = concurrency
        self.batch_size = batch_size
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @pyqtSlot()
    def run(self):
        try:
            _run_loop(self._run())
        except Exception as e:
            self.error.emit(f"清洗线程异常: {e}")

    async def _run(self):
        NONE_VALUES = {"none", "null", "无", "—", "-", ""}
        need_clean = [
            e for e in self.entries
            if not e.chinese or e.chinese.strip().lower() in NONE_VALUES
        ]
        total = len(need_clean)
        if total == 0:
            self.all_done.emit(0)
            return

        sem = asyncio.Semaphore(self.concurrency)
        completed = 0
        batches = [need_clean[i:i + self.batch_size]
                   for i in range(0, total, self.batch_size)]

        async def process_batch(batch: list[SubtitleEntry]):
            nonlocal completed
            if self._cancelled:
                return
            async with sem:
                if self._cancelled:
                    return
                try:
                    async with aiohttp.ClientSession() as session:
                        result_map = await self._translate_batch(batch, session)
                    # ✅ 修复：按 index 对齐，不用 zip
                    for entry in batch:
                        zh = result_map.get(entry.index, "")
                        if zh:
                            new_lines = []
                            replaced = False
                            for line in entry.lines:
                                stripped = line.strip()
                                if not replaced and stripped.lower() in NONE_VALUES:
                                    new_lines.append(zh)
                                    replaced = True
                                else:
                                    new_lines.append(line)
                            if not replaced:
                                new_lines.insert(0, zh)
                            entry.lines = new_lines
                            self.entry_done.emit(entry.index, zh)
                        completed += 1
                        self.progress.emit(completed, total)
                except Exception as e:
                    completed += len(batch)
                    self.progress.emit(completed, total)

        await asyncio.gather(*[process_batch(b) for b in batches])
        self.all_done.emit(total)

    async def _translate_batch(self, batch: list[SubtitleEntry],
                               session: aiohttp.ClientSession) -> dict[int, str]:
        """返回 {entry.index: zh} 字典"""
        items_json = json.dumps([
            {"id": e.index, "en": e.english} for e in batch
        ], ensure_ascii=False)

        system_content = (
            f"你是专业字幕翻译师。\n【背景与风格】\n{self.context}\n\n"
            "任务：将英文字幕翻译成中文，严格遵循上述风格指引（如有）。\n"
            "规则：\n"
            "1. 用词语气与风格指引一致，贴合视频语境\n"
            "2. 严格按JSON格式返回，不要加任何解释\n"
            "3. 每条必须有zh字段，不得为空或返回None\n"
            "4. 【严禁】不得合并条目，输入几条返回几条，id一一对应\n\n"
        )
        if self.glossary:
            system_content += f"【术语表】（强制执行，出现必须使用）\n{self.glossary}\n\n"
        system_content += "返回格式：[{\"id\": 1, \"zh\": \"中文翻译\"}]"

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": f"翻译以下字幕：\n{items_json}"}
        ]

        try:
            response = await self.client.chat(messages, session)
            clean = re.sub(r'```(?:json)?|```', '', response).strip()
            data = json.loads(clean)
            result: dict[int, str] = {}
            for item in data:
                zh = item.get("zh", "")
                zh = strip_annotation(str(zh).strip()) if zh else ""
                if zh.lower() in ("none", "null", "无", "—", "-", ""):
                    zh = ""
                result[item["id"]] = zh
            return result
        except Exception:
            # ✅ 失败时返回空字典，调用方会 fallback 到原文，不丢条目
            return {}


class BatchWorker(QObject):
    """批量处理：对每个文件独立执行「分析情景 → 清洗None → 优化字幕 → 导出」"""
    file_started = pyqtSignal(int, str)
    file_analysis_done = pyqtSignal(int, str, str)
    file_progress = pyqtSignal(int, int, int)
    file_done = pyqtSignal(int, str, str)
    file_error = pyqtSignal(int, str, str)
    all_done = pyqtSignal()
    log = pyqtSignal(str)

    def __init__(self, client: MistralClient, files: list[str],
                 out_dir: Optional[str], concurrency: int, batch_size: int,
                 overwrite: bool = False, chinese_first: bool = True,
                 glossary_prompt: str = ""):
        super().__init__()
        self.client = client
        self.files = files
        self.out_dir = out_dir
        self.concurrency = concurrency
        self.batch_size = batch_size
        self.overwrite = overwrite
        self.chinese_first = chinese_first
        self.glossary_prompt = glossary_prompt
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @pyqtSlot()
    def run(self):
        try:
            _run_loop(self._run())
        except Exception as e:
            self.log.emit(f"❌ 批量处理异常: {e}")
        finally:
            self.all_done.emit()

    async def _run(self):
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

                self.log.emit(f"  🔍 [{filename}] 分析情景（{len(entries)} 条字幕）...")

                context = await self._analyze(entries)
                self.file_analysis_done.emit(idx, filename, context)
                self.log.emit(f"  ✅ [{filename}] 情景分析完成")

                if self._cancelled:
                    break

                NONE_VALUES = {"none", "null", "无", "—", "-", ""}
                none_entries = [
                    e for e in entries
                    if not e.chinese or e.chinese.strip().lower() in NONE_VALUES
                ]
                if none_entries:
                    self.log.emit(f"  🧹 [{filename}] 清洗 {len(none_entries)} 条 None 字幕...")
                    await self._clean(idx, entries, none_entries, context, self.glossary_prompt)
                    self.log.emit(f"  ✅ [{filename}] 清洗完成")
                else:
                    self.log.emit(f"  ⏩ [{filename}] 无 None 条目，跳过清洗")

                if self._cancelled:
                    break

                self.log.emit(f"  ▶ [{filename}] 开始优化 {len(entries)} 条字幕...")
                entries = await self._optimize(idx, entries, context, self.glossary_prompt)

                if self._cancelled:
                    break

                out_path = self._get_out_path(filepath)
                blocks = [e.to_srt_block(use_optimized=True, chinese_first=self.chinese_first) for e in entries]
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(blocks))
                self.file_done.emit(idx, filename, out_path)

            except Exception as e:
                self.file_error.emit(idx, filename, str(e))

    async def _clean(self, file_idx: int, entries: list[SubtitleEntry],
                     none_entries: list[SubtitleEntry], context: str, glossary: str):
        sem = asyncio.Semaphore(self.concurrency)
        total = len(none_entries)
        completed = 0
        batches = [none_entries[i:i + self.batch_size]
                   for i in range(0, total, self.batch_size)]
        NONE_VALUES = {"none", "null", "无", "—", "-", ""}

        async def process_batch(batch):
            nonlocal completed
            if self._cancelled:
                return
            async with sem:
                if self._cancelled:
                    return
                try:
                    items_json = json.dumps([
                        {"id": e.index, "en": e.english} for e in batch
                    ], ensure_ascii=False)

                    sys_content = (
                        f"你是专业字幕翻译师。\n【背景与风格】\n{context}\n\n"
                        "任务：将英文字幕翻译成中文，严格遵循上述风格指引（如有）。\n"
                        "规则：\n"
                        "1. 用词语气与风格指引一致，贴合视频语境\n"
                        "2. 严格按JSON格式返回，不要加任何解释\n"
                        "3. 每条必须有zh字段，不得为空或返回None\n"
                        "4. 【严禁】不得合并条目，输入几条返回几条，id一一对应\n\n"
                    )
                    if glossary:
                        sys_content += f"【术语表】（强制执行，出现必须使用）\n{glossary}\n\n"
                    sys_content += "返回格式：[{\"id\": 1, \"zh\": \"中文翻译\"}]"

                    messages = [
                        {"role": "system", "content": sys_content},
                        {"role": "user", "content": f"翻译以下字幕：\n{items_json}"}
                    ]
                    async with aiohttp.ClientSession() as session:
                        response = await self.client.chat(messages, session)
                    clean_resp = re.sub(r'```(?:json)?|```', '', response).strip()
                    data = json.loads(clean_resp)
                    # ✅ 修复：用字典按 index 对齐，不用 zip
                    result_map = {item["id"]: item.get("zh", "") for item in data}
                    for entry in batch:
                        zh = result_map.get(entry.index, "")
                        zh = strip_annotation(str(zh).strip()) if zh else ""
                        if zh and zh.lower() not in NONE_VALUES:
                            new_lines = []
                            replaced = False
                            for line in entry.lines:
                                if not replaced and line.strip().lower() in NONE_VALUES:
                                    new_lines.append(zh)
                                    replaced = True
                                else:
                                    new_lines.append(line)
                            if not replaced:
                                new_lines.insert(0, zh)
                            entry.lines = new_lines
                            self.log.emit(f"    🔤 [{entry.index}] → {zh[:30]}{'...' if len(zh) > 30 else ''}")
                except Exception as e:
                    self.log.emit(f"    ⚠ 清洗批次失败: {e}")
                finally:
                    completed += len(batch)
                    self.file_progress.emit(file_idx, completed, total)

        await asyncio.gather(*[process_batch(b) for b in batches])

    async def _analyze(self, entries: list[SubtitleEntry]) -> str:
        sample = entries[:30]
        sample_text = "\n".join(
            f"[{e.index}] {e.chinese} | {e.english}" for e in sample
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "你是专业的字幕翻译顾问。分析双语字幕样本后，严格返回如下JSON，不加任何解释和代码块标记：\n"
                    '{"context": "...", "style_guide": "..."}\n\n'
                    "字段说明：\n"
                    "context：200字以内，描述内容领域、主题、受众、语言风格、常见翻译问题。\n"
                    "style_guide：150字以内，直接告诉翻译AI怎么译——用什么人称语气、"
                    "哪些高频词该怎么处理、句子长短节奏、需要避免什么。"
                    "要具体可执行，举1-2个这批字幕里的真实词汇作例子，适用于该领域任何视频。"
                )
            },
            {"role": "user", "content": f"字幕样本：\n{sample_text}"}
        ]
        async with aiohttp.ClientSession() as session:
            raw = await self.client.chat(messages, session)
        try:
            clean_raw = re.sub(r'```(?:json)?|```', '', raw).strip()
            parsed = json.loads(clean_raw)
            context = parsed.get("context", raw)
            style_guide = parsed.get("style_guide", "")
            if style_guide:
                return f"{context}\n\n【译文风格指引】{style_guide}"
            return context
        except Exception:
            return raw

    async def _optimize(self, file_idx: int, entries: list[SubtitleEntry],
                        context: str, glossary: str) -> list[SubtitleEntry]:
        sem = asyncio.Semaphore(self.concurrency)
        total = len(entries)
        completed = 0
        CONTEXT_SIZE = 3  # 跨批次携带的上文条数

        starts = list(range(0, total, self.batch_size))
        batches_with_ctx = []
        for start in starts:
            batch = entries[start:start + self.batch_size]
            prev = entries[max(0, start - CONTEXT_SIZE):start]
            batches_with_ctx.append((batch, prev))

        async def process_batch(batch, prev_ctx):
            nonlocal completed
            if self._cancelled:
                return
            async with sem:
                if self._cancelled:
                    return
                try:
                    async with aiohttp.ClientSession() as session:
                        result_map = await self._optimize_batch(batch, context, glossary, session, prev_ctx)
                    # 按 index 对齐，AI 漏返的条目保留原文
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
                    self.file_progress.emit(file_idx, completed, total)

        await asyncio.gather(*[process_batch(b, ctx) for b, ctx in batches_with_ctx])
        return entries

    async def _optimize_batch(self, batch: list[SubtitleEntry], context: str, glossary: str,
                              session: aiohttp.ClientSession,
                              prev_ctx: list[SubtitleEntry] | None = None) -> dict[int, list[str]]:
        """返回 {entry.index: opt_lines} 字典。prev_ctx 为上批末尾若干条，仅用于风格/术语对齐。"""
        items_json = json.dumps([
            {"id": e.index, "zh": e.chinese, "en": e.english} for e in batch
        ], ensure_ascii=False)

        sys_content = (
            f"你是专业字幕翻译优化师。\n"
            f"【背景与风格】\n{context}\n\n"
            "任务：优化双语字幕的中文翻译，使其符合上述背景和风格指引，自然准确。\n"
            "规则：\n"
            "1. 保持与英文原意一致\n"
            "2. 严格遵循【译文风格指引】的语气、用词和节奏要求（如有）\n"
            "3. 专业术语保持一致性\n"
            "4. 【严禁】zh字段中禁止出现任何Markdown格式符号，包括但不限于：**加粗**、*斜体*、`代码`、#标题、>引用、-列表等，只允许纯文本\n"
            "5. 严格按JSON格式返回，不要加任何解释\n"
            "6. 【严禁】不得合并字幕条目，输入几条必须返回几条，id一一对应\n"
            "7. 【严禁】zh字段只能包含翻译正文，不得添加任何括号注释、标注或说明文字\n"
            "8. 【重要】若某条字幕没有中文（zh为空、None或缺失），必须根据en字段直接翻译成自然中文，填入zh字段，不得留空\n"
            "9. 【严禁】en字段必须与输入完全一致，一字不差原样返回，绝对不得修改、合并、润色或截断英文原文\n\n"
        )
        if glossary:
            sys_content += f"【术语表】（强制执行，出现必须使用）\n{glossary}\n\n"
        sys_content += "返回格式：[{\"id\": 1, \"zh\": \"优化后中文\", \"en\": \"英文原文原样返回\"}]"

        if prev_ctx:
            ctx_json = json.dumps([
                {"id": e.index, "zh": e.chinese, "en": e.english}
                for e in prev_ctx
            ], ensure_ascii=False)
            user_content = (
                f"【上文参考（仅供风格/术语对齐，无需返回这些条目）】\n{ctx_json}\n\n"
                f"【待优化字幕】\n{items_json}"
            )
        else:
            user_content = f"优化以下字幕：\n{items_json}"

        messages = [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": user_content}
        ]

        response = await self.client.chat(messages, session)
        try:
            clean = re.sub(r'```(?:json)?|```', '', response).strip()
            data = json.loads(clean)
            api_map = {item["id"]: item for item in data}

            first_pass = []
            missing_zh_entries = []
            for entry in batch:
                if entry.index in api_map:
                    item = api_map[entry.index]
                    zh_raw = item.get("zh", "")
                    zh = strip_annotation(str(zh_raw).strip()) if zh_raw else ""
                    zh_valid = bool(zh) and zh.lower() not in ("none", "null", "无", "—", "-")
                    first_pass.append((entry, zh, zh_valid))
                    if not zh_valid:
                        missing_zh_entries.append(entry)
                else:
                    # AI 漏返了这条，走二次补翻
                    first_pass.append((entry, "", False))
                    missing_zh_entries.append(entry)

            retry_map: dict[int, str] = {}
            if missing_zh_entries:
                retry_items = json.dumps([
                    {"id": e.index, "en": e.english} for e in missing_zh_entries
                ], ensure_ascii=False)

                retry_sys = (
                    f"你是专业字幕翻译师。\n【背景与风格】\n{context}\n\n"
                    "任务：将以下英文字幕翻译成中文，严格遵循上述风格指引（如有）。\n"
                    "规则：\n1. 用词语气与风格指引一致\n"
                    "2. 严格按JSON格式返回，不要加任何解释\n"
                    "3. 每条必须有zh字段，不得为空\n\n"
                )
                if glossary:
                    retry_sys += f"【术语表】（强制执行，出现必须使用）\n{glossary}\n\n"
                retry_sys += "返回格式：[{\"id\": 1, \"zh\": \"中文翻译\"}]"

                retry_messages = [
                    {"role": "system", "content": retry_sys},
                    {"role": "user", "content": f"翻译以下字幕：\n{retry_items}"}
                ]
                try:
                    retry_response = await self.client.chat(retry_messages, session)
                    retry_clean = re.sub(r'```(?:json)?|```', '', retry_response).strip()
                    retry_data = json.loads(retry_clean)
                    retry_map = {item["id"]: item.get("zh", "") for item in retry_data}
                except Exception:
                    pass

            result: dict[int, list[str]] = {}
            for entry, zh, zh_valid in first_pass:
                opt_lines = []
                if zh_valid:
                    opt_lines.append(zh)
                elif entry.index in retry_map:
                    fallback_zh = strip_annotation(str(retry_map[entry.index]).strip())
                    if fallback_zh and fallback_zh.lower() not in ("none", "null"):
                        opt_lines.append(fallback_zh)
                en = entry.english
                if en:
                    opt_lines.append(en)
                result[entry.index] = opt_lines if opt_lines else entry.lines

            return result
        except Exception:
            return {e.index: e.lines for e in batch}

    def _get_out_path(self, filepath: str) -> str:
        p = Path(filepath)
        if self.overwrite:
            if self.out_dir:
                return str(Path(self.out_dir) / p.name)
            return str(p)
        stem = p.stem + "_optimized"
        if self.out_dir:
            return str(Path(self.out_dir) / (stem + p.suffix))
        return str(p.parent / (stem + p.suffix))
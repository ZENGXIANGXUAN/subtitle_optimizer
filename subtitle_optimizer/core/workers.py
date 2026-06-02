import json
import re
import asyncio
import aiohttp
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from core.models import (
    SubtitleEntry,
    parse_srt,
    strip_annotation,
    _format_subtitle_snapshot,
    _merge_candidate_is_safe,
    _translation_looks_incomplete,
    _english_word_count,
    _srt_time_to_ms,
    _ms_to_srt_time,
)
from core.segmenter import nlp_segment
from core.client import MistralClient


# 尝试相对导入，如果失败则使用绝对导入
try:
    from ..utils import logger, write_phase_snapshot
except ImportError:
    import sys
    import os

    sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
    from utils import logger, write_phase_snapshot


# 语言映射：显示标签 -> (语言代码, Prompt 名称)
LANGUAGE_MAP = {
    "中文": ("zh", "中文"),
    "English": ("en", "English"),
    "意大利语 (Italiano)": ("it", "Italian"),
    "日语 (日本語)": ("ja", "Japanese"),
    "韩语 (한국어)": ("ko", "Korean"),
    "法语 (Français)": ("fr", "French"),
    "德语 (Deutsch)": ("de", "German"),
    "西班牙语 (Español)": ("es", "Spanish"),
    "葡萄牙语 (Português)": ("pt", "Portuguese"),
    "俄语 (Русский)": ("ru", "Russian"),
}

# 语言代码 -> Prompt 名称
LANG_CODE_TO_PROMPT = {code: prompt for _, (code, prompt) in LANGUAGE_MAP.items()}

# 显示标签列表（用于 UI 下拉框）
LANGUAGE_LABELS = list(LANGUAGE_MAP.keys())
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


def _get_prompt_lang(language_label: str) -> str:
    """从显示标签获取 Prompt 中使用的语言名称"""
    if language_label in LANGUAGE_MAP:
        return LANGUAGE_MAP[language_label][1]
    return language_label


class BaseWorker(QObject):
    """Worker 基类：提供取消支持与事件循环安全封装"""
    error = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _execute(self, coro):
        """在专用事件循环中运行协程，自动捕获异常"""
        try:
            _run_loop(coro)
        except Exception as e:
            self.error.emit(f"{type(self).__name__} 异常: {e}")


def _build_analysis_messages(entries: list[SubtitleEntry],
                            input_lang: str = "English",
                            output_lang: str = "中文") -> list[dict]:
    """构造情景分析提示词，产出可直接约束后续翻译优化的 brief。"""
    sample = entries[:30]
    sample_text = "\n".join(
        f"[{e.index}] {e.english}"
        for e in sample
    )

    system_prompt = (
        "你是资深字幕翻译策略顾问。你的任务不是复述内容，而是从字幕样本中提炼一份"
        "可直接用于后续机器翻译优化的“翻译执行规约”。\n\n"
        "请严格返回 JSON，不加任何解释、Markdown 或代码块标记。格式如下：\n"
        "{\n"
        '  "domain": "内容领域，如交易课程/软件教程/访谈/纪录片",\n'
        '  "audience": "目标受众及其知识水平",\n'
        f'  "tone": "{output_lang}译文应采用的人称、语气、正式程度和教学节奏",\n'
        '  "context": "200字以内，说明主题、场景、核心概念、说话者意图和常见理解难点",\n'
        '  "translation_rules": ["3-5条可执行规则，直接指导逐条字幕怎么译"],\n'
        '  "term_rules": [\n'
        '    {"source": "样本中的英文术语或高频表达", "target": "建议译法", "note": "保留/意译/统一用法说明"}\n'
        '  ],\n'
        '  "avoid": ["2-4条需要避免的问题，如直译腔、误译方向、过度解释、术语不统一"],\n'
        f'  "style_guide": "150字以内，写给后续翻译AI的综合指令，必须具体、可执行，目标语言是{output_lang}"\n'
        "}\n\n"
        "要求：\n"
        "1. 优先识别对后续翻译有约束力的信息：领域术语、固定译法、语气、人称、句子长短、保留英文缩写的规则。\n"
        "2. 不要写空泛摘要，例如“语言自然流畅”；必须说明怎样才算自然流畅。\n"
        "3. term_rules 只收录样本中真实出现、且可能影响一致性的词或表达；没有把握时在 note 中说明。\n"
        "4. 如果前30条偏开场白，也要从样本里的术语和表达推断可迁移的翻译规则，不要只总结开场内容。\n"
        f"5. 所有字段必须使用{output_lang}填写；source 字段可保留{input_lang}原词。"
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"字幕样本：\n{sample_text}"}
    ]


def _format_analysis_result(raw: str) -> str:
    """把结构化分析结果压成下游 prompt 可直接使用的文本。"""
    try:
        clean_raw = re.sub(r'```(?:json)?|```', '', raw).strip()
        parsed = json.loads(clean_raw)
    except Exception:
        return raw

    if not isinstance(parsed, dict):
        return raw

    lines = []
    for label, key in (
        ("内容领域", "domain"),
        ("目标受众", "audience"),
        ("语气风格", "tone"),
        ("背景情景", "context"),
    ):
        value = parsed.get(key)
        if value:
            lines.append(f"【{label}】{value}")

    translation_rules = parsed.get("translation_rules") or []
    if translation_rules:
        lines.append("【翻译规则】" + "；".join(str(rule) for rule in translation_rules if rule))

    term_rules = parsed.get("term_rules") or []
    formatted_terms = []
    if isinstance(term_rules, list):
        for item in term_rules:
            if isinstance(item, dict):
                source = item.get("source", "")
                target = item.get("target", "")
                note = item.get("note", "")
                term = f"{source} -> {target}".strip()
                if note:
                    term = f"{term}（{note}）"
                if source or target:
                    formatted_terms.append(term)
            elif item:
                formatted_terms.append(str(item))
    if formatted_terms:
        lines.append("【术语与固定译法】" + "；".join(formatted_terms))

    avoid = parsed.get("avoid") or []
    if avoid:
        lines.append("【避免】" + "；".join(str(item) for item in avoid if item))

    style_guide = parsed.get("style_guide", "")
    if style_guide:
        lines.append(f"【译文风格指引】{style_guide}")

    return "\n\n".join(lines) if lines else raw


async def _optimize_batch_impl(
    batch: list[SubtitleEntry],
    context: str,
    glossary: str,
    client: MistralClient,
    session: aiohttp.ClientSession,
    input_lang: str = "English",
    output_lang: str = "中文",
    prev_ctx: list[SubtitleEntry] | None = None,
) -> dict[int, list[str]]:
    """
    共享：发送一批字幕给 AI 优化，返回 {index: [zh, en]}。
    prev_ctx 为上一批末尾若干条（只作上下文参考，不要求 AI 返回）。
    """
    items_json = json.dumps([
        {"id": e.index, "zh": e.chinese, "en": e.english}
        for e in batch
    ], ensure_ascii=False)

    sys_content = (
        f"你是专业字幕翻译优化师。\n"
        f"【背景与风格】\n{context}\n\n"
        f"任务：优化双语字幕的{output_lang}翻译，使译文准确、自然、符合背景风格。\n"
    )
    sys_content += (
        "\n【重要前提】SRT字幕按时间轴逐句切分，一个完整语义常被拆成多条。"
        f"输入的zh字段是初译，可能存在错误。"
        "因此：必须以en为唯一翻译依据，zh仅供用词风格参考，不得直接沿用。\n\n"
        "规则：\n"
        f"1. 【锚定en】每条zh必须且只能对应该条en的内容——en是完整句就译完整句，en是半句就译对应的半句{output_lang}\n"
        f"2. 在忠实en的前提下，使{output_lang}自然流畅，符合【背景与风格】的语气和用词\n"
        "3. 专业术语保持批次内一致性\n"
        f"4. 【严禁】zh只允许纯文本，禁止任何Markdown符号（**、*、`、#、>、-列表等）\n"
        "5. 【严禁】zh只含译文正文，不得添加括号注释、标注或说明文字\n"
        "6. 【严禁】不得合并条目，输入几条返回几条，id严格一一对应\n"
        "7. 【严禁】en字段原样返回，一字不差，不得修改或截断\n"
        "8. 若en为空，zh也留空\n"
        "9. 严格按JSON格式返回，不加任何解释\n"
        f"10. 【严禁漏译 — 逐词对照】zh必须完整翻译en中的每个语义单元：主语、谓语、宾语、状语、从句、插入语、过渡词（如okay/now/well/so）、强调词（如very/always/important）。不得跳过、省略、或合并任何信息点\n"
        f"11. 【严禁摘要】不得把长英文压缩成一句概括性{output_lang}。en有多长，zh就译多长。宁可使用较长{output_lang}，也不能遗漏信息点\n"
        "12. 【逐句自检 — 强制执行】返回前逐条朗读en，逐词检查zh是否覆盖了：谁（主语）→做了什么（动作）→为什么/怎么（条件/方式）→结果如何。每检查完一条在心里打勾，缺一不可\n\n"
        f'返回格式：[{{"id": 1, "zh": "优化后{output_lang}", "en": "英文原文原样返回"}}]'
    )

    glossary_note = f"\n\n【术语表 — 翻译时务必使用】\n{glossary}" if glossary else ""

    if prev_ctx:
        ctx_json = json.dumps([
            {"id": e.index, "zh": e.chinese, "en": e.english}
            for e in prev_ctx
        ], ensure_ascii=False)
        user_content = (
            f"【上文参考（仅供风格/术语对齐，无需返回这些条目）】\n{ctx_json}\n\n"
            f"【待优化字幕】\n{items_json}"
            f"{glossary_note}"
        )
    else:
        user_content = f"优化以下字幕：\n{items_json}{glossary_note}"

    messages = [
        {"role": "system", "content": sys_content},
        {"role": "user", "content": user_content}
    ]

    response = await client.chat(messages, session)

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
                zh_incomplete = _translation_looks_incomplete(zh, entry.english)
                zh_valid = bool(zh) and zh.lower() not in ("none", "null", "无", "—", "-")
                zh_valid = zh_valid and not zh_incomplete
                first_pass.append((entry, zh, zh_valid))
                if not zh_valid:
                    missing_zh_entries.append(entry)
            else:
                first_pass.append((entry, "", False))
                missing_zh_entries.append(entry)

        retry_map: dict[int, str] = {}
        if missing_zh_entries:
            retry_items = json.dumps([
                {"id": e.index, "en": e.english}
                for e in missing_zh_entries
            ], ensure_ascii=False)

            retry_sys = (
                f"你是专业字幕翻译师。\n【背景与风格】\n{context}\n\n"
                f"任务：将{input_lang}字幕翻译成{output_lang}，严格遵循上述风格指引（如有）。\n"
            )
            retry_sys += (
                "规则：\n1. 用词语气与风格指引一致\n"
                "2. 严格按JSON格式返回，不要加任何解释\n"
                "3. 每条必须有zh字段，不得为空\n"
                "4. 【严禁重复翻译】每条只译自己的en内容，en是半句就译半句，不得让相邻条目的zh相同或重复\n"
                f"5. 【严禁摘要】不得把长英文压缩成一句概括性{output_lang}，必须完整覆盖en中的全部信息点\n"
                "6. 【覆盖自检】返回前逐条检查：zh是否完整覆盖了en的主语、动作、条件、原因、结果和关键术语\n\n"
                f'返回格式：[{{"id": 1, "zh": "{output_lang}翻译"}}]'
            )

            retry_glossary_note = f"\n\n【术语表 — 翻译时务必使用】\n{glossary}" if glossary else ""

            retry_messages = [
                {"role": "system", "content": retry_sys},
                {"role": "user", "content": f"翻译以下字幕：\n{retry_items}{retry_glossary_note}"}
            ]
            try:
                retry_response = await client.chat(retry_messages, session)
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
                fallback_incomplete = _translation_looks_incomplete(fallback_zh, entry.english)
                if fallback_zh and fallback_zh.lower() not in ("none", "null") and not fallback_incomplete:
                    opt_lines.append(fallback_zh)
            en = entry.english
            if en:
                opt_lines.append(en)
            has_translation = len(opt_lines) > 0 and any(line.strip() for line in opt_lines)
            result[entry.index] = opt_lines if has_translation else entry.lines

        return result
    except Exception:
        return {e.index: e.lines for e in batch}


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
        logger.debug(f"AnalysisWorker初始化完成，待处理条目数: {len(entries)}")

    @pyqtSlot()
    def run(self):
        logger.info("开始执行字幕分析任务")
        self._execute(self._run())
        logger.info("AnalysisWorker线程关闭")

    async def _run(self):
        logger.debug("开始分析字幕内容")

        self.progress.emit("正在分析字幕内容，识别翻译情景...")
        logger.info("正在分析字幕内容，识别翻译情景...")

        messages = _build_analysis_messages(self.entries, self.input_lang, self.output_lang)

        try:
            async with aiohttp.ClientSession() as session:
                raw = await self.client.chat(messages, session)
            logger.info("字幕分析完成")
            result = _format_analysis_result(raw)
            self.finished.emit(result)
        except Exception as e:
            error_msg = f"字幕分析失败: {str(e)}"
            logger.error(error_msg, exc_info=True)
            self.error.emit(error_msg)


class OptimizeWorker(BaseWorker):
    """第二阶段/第三阶段：并发优化字幕"""
    entry_done = pyqtSignal(int, list, str)
    all_done = pyqtSignal()
    progress = pyqtSignal(int, int)

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
        logger.debug(
            f"OptimizeWorker初始化完成，待处理条目数: {len(entries)}, 并发数: {concurrency}, 批次大小: {batch_size}")

    def cancel(self):
        super().cancel()
        logger.info("OptimizeWorker任务已取消")

    @pyqtSlot()
    def run(self):
        logger.info("开始执行字幕优化任务")
        self._execute(self._run())
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
                        entry.optimized_lines = opt_lines
                        entry.status = status
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
        logger.info(_format_subtitle_snapshot("AFTER_OPTIMIZE", self.entries, use_optimized=True))
        logger.info("所有字幕优化任务完成")
        self.all_done.emit()

    async def _optimize_batch(self, batch: list[SubtitleEntry],
                              session: aiohttp.ClientSession,
                              prev_ctx: list[SubtitleEntry] | None = None) -> dict[int, list[str]]:
        return await _optimize_batch_impl(batch, self.context, self.glossary, self.client, session,
                                          self.input_lang, self.output_lang, prev_ctx)


async def _translate_batch_impl(
    batch: list[SubtitleEntry],
    context: str,
    glossary: str,
    client: MistralClient,
    session: aiohttp.ClientSession,
    input_lang: str = "English",
    output_lang: str = "中文",
) -> dict[int, str]:
    """共享：翻译一批字幕，返回 {index: zh}。失败返回空字典。"""
    items_json = json.dumps([
        {"id": e.index, "en": e.english} for e in batch
    ], ensure_ascii=False)

    system_content = (
        f"你是专业字幕翻译师。\n【背景与风格】\n{context}\n\n"
        f"任务：将{input_lang}字幕翻译成{output_lang}，严格遵循上述风格指引（如有）。\n"
        "规则：\n"
        "1. 用词语气与风格指引一致，贴合视频语境\n"
        "2. 严格按JSON格式返回，不要加任何解释\n"
        "3. 每条必须有zh字段，不得为空或返回None\n"
        "4. 【严禁】不得合并条目，输入几条返回几条，id一一对应\n"
        "5. 【严禁重复翻译】每条只译自己的en内容，en是半句就译半句，不得让相邻条目的zh相同或重复\n\n"
        f'返回格式：[{{"id": 1, "zh": "{output_lang}翻译"}}]'
    )

    glossary_note = f"\n\n【术语表 — 翻译时务必使用】\n{glossary}" if glossary else ""

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": f"翻译以下字幕：\n{items_json}{glossary_note}"}
    ]

    try:
        response = await client.chat(messages, session)
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
        return {}


async def _repair_incomplete_segment_translations(
    entries: list[SubtitleEntry],
    context: str,
    glossary: str,
    client: MistralClient,
    session: aiohttp.ClientSession,
) -> None:
    incomplete = [
        entry for entry in entries
        if _translation_looks_incomplete(entry.chinese, entry.english)
    ]
    if not incomplete:
        return

    logger.warning(
        "Segment translation coverage check found %s incomplete entries: %s",
        len(incomplete),
        [entry.index for entry in incomplete],
    )
    retry_map = await _translate_batch_impl(incomplete, context, glossary, client, session, input_lang, output_lang)
    for entry in incomplete:
        zh = strip_annotation(str(retry_map.get(entry.index, "")).strip())
        if not zh or zh.lower() in ("none", "null", "无", "—", "-"):
            continue
        en = entry.english
        entry.lines = [zh, en] if en else [zh]


def _split_short_long(batch: list[SubtitleEntry], max_words: int = 20):
    """分离短条目（直通）和长条目（需LLM切分）。"""
    short_entries = []
    long_entries = []
    for e in batch:
        if _english_word_count(e.english) <= max_words:
            short_entries.append(e)
        else:
            long_entries.append(e)
    return short_entries, long_entries


def _apply_llm_split(long_entries: list[SubtitleEntry], llm_response: list[dict],
                     log_fn=None) -> list[SubtitleEntry]:
    """将LLM切分建议应用到长条目，按词数比例分配时间戳。"""
    results = []
    by_id = {e.index: e for e in long_entries}

    for item in llm_response:
        entry_id = item.get("id")
        segments = item.get("segments", [])
        if not segments:
            continue

        orig = by_id.get(entry_id)
        if not orig:
            continue

        if len(segments) == 1:
            results.append(SubtitleEntry(
                orig.index, orig.start_time, orig.end_time, list(orig.lines)))
            continue

        if log_fn:
            log_fn(f"  ✂️ 切分 [{orig.index}] → {len(segments)} 段")

        total_words = sum(_english_word_count(s) for s in segments)
        total_ms = _srt_time_to_ms(orig.end_time) - _srt_time_to_ms(orig.start_time)
        if total_ms <= 0:
            total_ms = 1
        current_ms = _srt_time_to_ms(orig.start_time)

        for i, seg_en in enumerate(segments):
            sw = _english_word_count(seg_en)
            seg_duration = int(total_ms * sw / total_words) if total_words > 0 else int(total_ms / len(segments))
            if seg_duration < 200:
                seg_duration = int(total_ms / len(segments))

            seg_start = _ms_to_srt_time(current_ms)
            seg_end = _ms_to_srt_time(current_ms + seg_duration)
            current_ms += seg_duration

            lines = [seg_en] if seg_en else []
            if i == 0 and orig.chinese:
                lines.insert(0, orig.chinese)

            seg_entry = SubtitleEntry(
                index=orig.index + i,
                start_time=seg_start,
                end_time=seg_end,
                lines=lines,
            )
            seg_entry.original_lines = list(orig.original_lines or orig.lines)
            results.append(seg_entry)

    return results


class SegmentWorker(BaseWorker):
    """第二阶段：智能重组字幕 — NLP预断句合并 + LLM语义切分长句"""
    progress = pyqtSignal(int, int)
    all_done = pyqtSignal(object)  # list[SubtitleEntry]
    log = pyqtSignal(str)

    def __init__(self, client: MistralClient, entries: list[SubtitleEntry],
                 context: str, glossary: str, concurrency: int = 4, batch_size: int = 12):
        super().__init__()
        self.client = client
        self.entries = entries
        self.context = context
        self.glossary = glossary
        self.concurrency = concurrency
        self.batch_size = batch_size

    @pyqtSlot()
    def run(self):
        self._execute(self._run())

    async def _run(self):
        total = len(self.entries)
        # 检测原生字幕：大多数条目的文本行数为 1（只有英文，缺中文）
        self._is_native = sum(1 for e in self.entries if len(e.lines) == 1) > total * 0.5
        if self._is_native:
            self.log.emit("  📋 检测到原生字幕，将自动翻译为中文")

        window = max(10, self.batch_size)
        batches = [self.entries[i:i + window]
                   for i in range(0, total, window)]
        sem = asyncio.Semaphore(max(1, self.concurrency))
        completed = 0
        all_results = []

        async def process_batch(batch: list[SubtitleEntry]):
            nonlocal completed
            if self._cancelled:
                return
            async with sem:
                if self._cancelled:
                    return
                try:
                    async with aiohttp.ClientSession() as session:
                        results = await self._segment_batch(batch, session)
                    all_results.extend(results)
                except Exception:
                    for e in batch:
                        all_results.append(SubtitleEntry(
                            e.index, e.start_time, e.end_time, list(e.lines)))
                finally:
                    completed += len(batch)
                    self.progress.emit(completed, total)

        await asyncio.gather(*[process_batch(b) for b in batches])

        seen = {}
        deduped = []
        for e in all_results:
            if e.index not in seen:
                seen[e.index] = e
                deduped.append(e)
        deduped.sort(key=lambda e: e.index)
        logger.info(_format_subtitle_snapshot("AFTER_SEGMENT", deduped))
        self.all_done.emit(deduped)

    async def _segment_batch(self, batch: list[SubtitleEntry],
                              session: aiohttp.ClientSession) -> list[SubtitleEntry]:
        # NLP 预断句合并
        batch = nlp_segment(batch, log_fn=self.log.emit)
        if not batch:
            return []

        # 分离：短条目直通，长条目（>20词）送LLM语义切分
        short_entries, long_entries = _split_short_long(batch)
        if not long_entries:
            return short_entries

        items_json = json.dumps([
            {"id": e.index, "en": e.english,
             "start": e.start_time, "end": e.end_time}
            for e in long_entries
        ], ensure_ascii=False)

        sys_content = (
            "你是专业字幕切分助手。\n"
            f"【背景与风格】\n{self.context}\n\n"
            "任务：以下英文字幕条目过长（>20词），在语义最自然的断点切分为2-3段，"
            "每段 ≤20词，成为适合屏幕阅读的独立字幕。\n\n"
            "规则：\n"
            "1. 每段 ≤20个英文词\n"
            "2. 切分点选在最自然的语义边界：从句结束、意群转折、自然停顿处\n"
            "3. 每段必须是相对完整的语义单元（不可在词组内部切断）\n"
            "4. 不要修改英文原文，只确定断点位置\n"
            "5. 严格按JSON格式返回，不要加任何解释\n\n"
            '格式：[{"id": 原id, "segments": ["段1英文", "段2英文"]}]\n'
            "segments 长度1=无需切分，2-3=切分结果。"
        )

        messages = [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": f"切分以下长字幕条目：\n{items_json}"}
        ]

        response = await self.client.chat(messages, session)
        try:
            clean = re.sub(r'```(?:json)?|```', '', response).strip()
            data = json.loads(clean)
            split_results = _apply_llm_split(long_entries, data, log_fn=self.log.emit)
        except Exception:
            split_results = [SubtitleEntry(e.index, e.start_time, e.end_time, list(e.lines))
                             for e in long_entries]

        return short_entries + split_results


class TranslateWorker(BaseWorker):
    """第三阶段：原生字幕 en_合并 → zh_译；双语字幕跳过"""
    entry_done = pyqtSignal(int, list, str)
    all_done = pyqtSignal()
    progress = pyqtSignal(int, int)

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
        # 检测是否需要翻译（zh_合并 为空的是原生字幕）
        needs_translation = any(not e.chinese for e in self.entries)
        if not needs_translation:
            self.all_done.emit()
            return

        sem = asyncio.Semaphore(self.concurrency)
        total = len(self.entries)
        completed = 0

        batches = [self.entries[i:i + self.batch_size]
                   for i in range(0, total, self.batch_size)]

        async def process_batch(batch):
            nonlocal completed
            if self._cancelled:
                return
            async with sem:
                if self._cancelled:
                    return
                try:
                    async with aiohttp.ClientSession() as session:
                        result_map = await _translate_batch_impl(
                            batch, self.context, self.glossary, self.client, session,
                            self.input_lang, self.output_lang)
                    for entry in batch:
                        zh = result_map.get(entry.index, "")
                        if zh and entry.english:
                            entry.lines = [zh, entry.english]
                            entry.status = "done"
                            self.entry_done.emit(entry.index, entry.lines, "done")
                        else:
                            entry.status = "error"
                            self.entry_done.emit(entry.index, entry.lines, "error")
                        completed += 1
                        self.progress.emit(completed, total)
                except Exception:
                    for entry in batch:
                        entry.status = "error"
                        self.entry_done.emit(entry.index, entry.lines, "error")
                        completed += 1
                        self.progress.emit(completed, total)

        await asyncio.gather(*[process_batch(b) for b in batches])
        self.all_done.emit()


class BatchWorker(BaseWorker):
    """批量处理：对每个文件独立执行「分析情景 → 智能重组 → 优化润色 → 导出」"""
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
                 glossary_prompt: str = "", original_line: int = 1,
                 input_lang: str = "English", output_lang: str = "中文"):
        super().__init__()
        self.client = client
        self.files = files
        self.out_dir = out_dir
        self.concurrency = concurrency
        self.batch_size = batch_size
        self.overwrite = overwrite
        self.chinese_first = chinese_first
        self.glossary_prompt = glossary_prompt
        self.original_line = original_line  # 原文行号（0-based）
        self.input_lang = input_lang
        self.output_lang = output_lang

    def _extract_original_line(self, entries: list[SubtitleEntry]) -> list[SubtitleEntry]:
        """根据设置提取原文行，将双语字幕转换为单语言字幕"""
        line_idx = self.original_line

        extracted = []
        for entry in entries:
            if len(entry.lines) > 1:
                # 双语字幕：提取指定行
                if line_idx < len(entry.lines):
                    original_text = entry.lines[line_idx]
                    new_entry = SubtitleEntry(
                        entry.index, entry.start_time, entry.end_time,
                        [original_text]
                    )
                    new_entry.original_lines = list(entry.original_lines or entry.lines)
                    extracted.append(new_entry)
                else:
                    # 设置的行号超出范围，保留原样
                    extracted.append(entry)
            else:
                # 单语字幕：直接保留
                extracted.append(entry)

        return extracted

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

                # 自动检测双语字幕并提取原文行
                if entries and len(entries[0].lines) > 1:
                    original_count = len(entries)
                    entries = self._extract_original_line(entries)
                    line_num = self.original_line + 1
                    self.log.emit(f"  📋 [{filename}] 检测到双语字幕（{original_count}条），已提取原文行（第{line_num}行）")

                self.log.emit(f"  🔍 [{filename}] 分析情景（{len(entries)} 条字幕）...")

                context = await self._analyze(entries)
                self.file_analysis_done.emit(idx, filename, context)
                self.log.emit(f"  ✅ [{filename}] 情景分析完成")

                if self._cancelled:
                    break

                self.log.emit(f"  🔗 [{filename}] 开始智能重组 {len(entries)} 条字幕...")
                entries = await self._segment(idx, entries, context, self.glossary_prompt)
                write_phase_snapshot(filename, "AFTER_SEGMENT", entries)
                logger.info(_format_subtitle_snapshot(f"BATCH_AFTER_SEGMENT_{filename}", entries))
                self.log.emit(f"  ✅ [{filename}] 重组完成：{len(entries)} 条字幕")

                if self._cancelled:
                    break

                self.log.emit(f"  🌐 [{filename}] 开始翻译 {len(entries)} 条字幕...")
                entries = await self._translate(entries, context, self.glossary_prompt)
                write_phase_snapshot(filename, "AFTER_TRANSLATE", entries)
                logger.info(_format_subtitle_snapshot(f"BATCH_AFTER_TRANSLATE_{filename}", entries))
                self.log.emit(f"  ✅ [{filename}] 翻译完成")

                if self._cancelled:
                    break

                self.log.emit(f"  ✨ [{filename}] 开始优化润色 {len(entries)} 条字幕...")
                entries = await self._optimize(idx, entries, context, self.glossary_prompt)
                write_phase_snapshot(filename, "AFTER_OPTIMIZE", entries, use_optimized=True)
                logger.info(_format_subtitle_snapshot(f"BATCH_AFTER_OPTIMIZE_{filename}", entries, use_optimized=True))

                if self._cancelled:
                    break

                out_path = self._get_out_path(filepath)
                blocks = [e.to_srt_block(use_optimized=True, chinese_first=self.chinese_first) for e in entries]
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(blocks))
                self.file_done.emit(idx, filename, out_path)

            except Exception as e:
                self.file_error.emit(idx, filename, str(e))

    async def _segment(self, file_idx: int, entries: list[SubtitleEntry],
                        context: str, glossary: str) -> list[SubtitleEntry]:
        """重组阶段：智能断句合并 + 丢弃空条目，返回重构后的条目列表"""
        total = len(entries)
        is_native = sum(1 for e in entries if len(e.lines) == 1) > total * 0.5
        if is_native:
            self.log.emit(f"    📋 检测到原生字幕，将自动翻译为中文")

        seg_batch = max(10, min(30, self.batch_size * 2))
        batches = [entries[i:i + seg_batch]
                   for i in range(0, total, seg_batch)]
        sem = asyncio.Semaphore(max(1, self.concurrency))
        completed = 0
        all_results = []

        async def process_batch(batch):
            nonlocal completed
            if self._cancelled:
                return
            async with sem:
                if self._cancelled:
                    return
                try:
                    async with aiohttp.ClientSession() as session:
                        results = await self._segment_batch(batch, context, glossary, session, is_native)
                    all_results.extend(results)
                except Exception as e:
                    self.log.emit(f"    ⚠ 重组批次失败: {e}")
                    for e_item in batch:
                        all_results.append(SubtitleEntry(
                            e_item.index, e_item.start_time, e_item.end_time,
                            list(e_item.lines)))
                finally:
                    completed += len(batch)
                    self.file_progress.emit(file_idx, completed, total)

        await asyncio.gather(*[process_batch(b) for b in batches])

        seen = {}
        deduped = []
        for e_item in all_results:
            if e_item.index not in seen:
                seen[e_item.index] = e_item
                deduped.append(e_item)
        deduped.sort(key=lambda e: e.index)
        logger.info(_format_subtitle_snapshot("BATCH_AFTER_SEGMENT", deduped))
        return deduped

    async def _segment_batch(self, batch: list[SubtitleEntry], context: str, glossary: str,
                               session: aiohttp.ClientSession,
                               is_native: bool = False) -> list[SubtitleEntry]:
        # NLP 预断句合并
        batch = nlp_segment(batch, log_fn=self.log.emit)
        if not batch:
            return []

        # 分离：短条目直通，长条目（>20词）送LLM语义切分
        short_entries, long_entries = _split_short_long(batch)
        if not long_entries:
            return short_entries

        items_json = json.dumps([
            {"id": e.index, "en": e.english,
             "start": e.start_time, "end": e.end_time}
            for e in long_entries
        ], ensure_ascii=False)

        sys_content = (
            "你是专业字幕切分助手。\n"
            f"【背景与风格】\n{context}\n\n"
            "任务：以下英文字幕条目过长（>20词），在语义最自然的断点切分为2-3段，"
            "每段 ≤20词，成为适合屏幕阅读的独立字幕。\n\n"
            "规则：\n"
            "1. 每段 ≤20个英文词\n"
            "2. 切分点选在最自然的语义边界：从句结束、意群转折、自然停顿处\n"
            "3. 每段必须是相对完整的语义单元（不可在词组内部切断）\n"
            "4. 不要修改英文原文，只确定断点位置\n"
            "5. 严格按JSON格式返回，不要加任何解释\n\n"
            '格式：[{"id": 原id, "segments": ["段1英文", "段2英文"]}]\n'
            "segments 长度1=无需切分，2-3=切分结果。"
        )

        messages = [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": f"切分以下长字幕条目：\n{items_json}"}
        ]

        response = await self.client.chat(messages, session)
        try:
            clean = re.sub(r'```(?:json)?|```', '', response).strip()
            data = json.loads(clean)
            split_results = _apply_llm_split(long_entries, data, log_fn=self.log.emit)
        except Exception:
            split_results = [SubtitleEntry(e.index, e.start_time, e.end_time, list(e.lines))
                             for e in long_entries]

        return short_entries + split_results

    async def _translate(self, entries: list[SubtitleEntry],
                          context: str, glossary: str) -> list[SubtitleEntry]:
        """阶段3：原生字幕 en_合并 → zh_译；双语字幕跳过"""
        needs_translation = any(not e.chinese for e in entries)
        if not needs_translation:
            return entries

        sem = asyncio.Semaphore(self.concurrency)
        total = len(entries)
        batches = [entries[i:i + self.batch_size]
                   for i in range(0, total, self.batch_size)]

        async def process_batch(batch):
            if self._cancelled:
                return
            async with sem:
                if self._cancelled:
                    return
                try:
                    async with aiohttp.ClientSession() as session:
                        result_map = await _translate_batch_impl(
                            batch, context, glossary, self.client, session,
                            self.input_lang, self.output_lang)
                    for entry in batch:
                        zh = result_map.get(entry.index, "")
                        if zh and entry.english:
                            entry.lines = [zh, entry.english]
                except Exception:
                    pass

        await asyncio.gather(*[process_batch(b) for b in batches])
        return entries

    async def _analyze(self, entries: list[SubtitleEntry]) -> str:
        messages = _build_analysis_messages(entries, self.input_lang, self.output_lang)
        async with aiohttp.ClientSession() as session:
            raw = await self.client.chat(messages, session)
        return _format_analysis_result(raw)

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
        logger.info(_format_subtitle_snapshot("BATCH_AFTER_OPTIMIZE", entries, use_optimized=True))
        return entries

    async def _optimize_batch(self, batch: list[SubtitleEntry], context: str, glossary: str,
                              session: aiohttp.ClientSession,
                              prev_ctx: list[SubtitleEntry] | None = None) -> dict[int, list[str]]:
        return await _optimize_batch_impl(batch, context, glossary, self.client, session,
                                          self.input_lang, self.output_lang, prev_ctx)

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

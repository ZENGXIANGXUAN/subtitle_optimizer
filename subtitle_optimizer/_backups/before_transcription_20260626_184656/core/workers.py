import json
import re
import asyncio
import aiohttp
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from core.models import (
    SubtitleEntry,
    parse_srt,
    strip_annotation,
    _format_subtitle_snapshot,
    _translation_looks_incomplete,
    _english_word_count,
    _chinese_char_count,
    _srt_time_to_ms,
    _ms_to_srt_time,
    _source_text_length,
    verify_source_integrity,
)
from core.segmenter import _get_source_text, _get_target_text
from core.client import MistralClient


@dataclass
class AnalysisResult:
    context: str
    auto_glossary: str = ""
    raw: str = ""


@dataclass
class GlossaryMergeResult:
    raw: str
    added: list[tuple[str, str]]
    conflicts: list[tuple[str, str, str]]


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

MAX_SEGMENT_SOURCE_LENGTH = 20
MAX_LLM_MERGE_SOURCE_LENGTH = 60
MAX_LLM_MERGE_TIMESPAN_MS = 8000
MAX_LLM_MERGE_GAP_MS = 1200


def _clone_segment_entry(entry: SubtitleEntry) -> SubtitleEntry:
    clone = SubtitleEntry(entry.index, entry.start_time, entry.end_time, list(entry.lines))
    clone.source_indexes = list(entry.source_indexes or [entry.index])
    clone.original_lines = list(entry.original_lines or entry.lines)
    return clone


def _split_text_by_source_length(text: str, max_length: int, lang_code: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return [""]

    if lang_code in ("zh", "ja", "ko"):
        return [text[i:i + max_length] for i in range(0, len(text), max_length)]

    words = text.split()
    if not words:
        return [text]
    return [
        " ".join(words[i:i + max_length])
        for i in range(0, len(words), max_length)
    ]


def _force_split_entry(
    entry: SubtitleEntry,
    max_length: int = MAX_SEGMENT_SOURCE_LENGTH,
    lang_code: str = "en",
    log_fn=None,
) -> list[SubtitleEntry]:
    source_text = _get_source_text(entry, lang_code).strip()
    if _source_text_length(source_text, lang_code) <= max_length:
        return [_clone_segment_entry(entry)]

    chunks = [
        chunk for chunk in _split_text_by_source_length(source_text, max_length, lang_code)
        if chunk.strip()
    ]
    if len(chunks) <= 1:
        return [_clone_segment_entry(entry)]

    if log_fn:
        log_fn(
            f"  Auto split [{entry.index}] into {len(chunks)} parts "
            f"because it exceeded {max_length}"
        )

    start_ms = _srt_time_to_ms(entry.start_time)
    end_ms = _srt_time_to_ms(entry.end_time)
    total_ms = max(1, end_ms - start_ms)
    chunk_lengths = [
        max(1, _source_text_length(chunk, lang_code))
        for chunk in chunks
    ]
    total_length = max(1, sum(chunk_lengths))
    current_ms = start_ms
    consumed_length = 0
    results: list[SubtitleEntry] = []

    for i, (chunk, chunk_length) in enumerate(zip(chunks, chunk_lengths)):
        consumed_length += chunk_length
        if i == len(chunks) - 1:
            seg_end_ms = end_ms
        else:
            seg_end_ms = start_ms + int(total_ms * consumed_length / total_length)
            if seg_end_ms <= current_ms and current_ms < end_ms:
                seg_end_ms = current_ms + 1

        seg_entry = SubtitleEntry(
            index=-(abs(entry.index) * 10000 + i + 1),
            start_time=_ms_to_srt_time(current_ms),
            end_time=_ms_to_srt_time(seg_end_ms),
            lines=[chunk],
        )
        seg_entry.original_lines = list(entry.original_lines or entry.lines)
        seg_entry.source_indexes = list(entry.source_indexes or [entry.index])
        results.append(seg_entry)
        current_ms = seg_end_ms

    return results


def _llm_merge_group_is_safe(
    batch: list[SubtitleEntry],
    entries: list[SubtitleEntry],
    lang_code: str = "en",
) -> bool:
    if len(entries) <= 1:
        return True

    position_by_id = {e.index: i for i, e in enumerate(batch)}
    positions = [position_by_id[e.index] for e in entries]
    if positions != list(range(min(positions), max(positions) + 1)):
        return False

    start_ms = _srt_time_to_ms(entries[0].start_time)
    end_ms = _srt_time_to_ms(entries[-1].end_time)
    if end_ms - start_ms > MAX_LLM_MERGE_TIMESPAN_MS:
        return False

    for left, right in zip(entries, entries[1:]):
        gap_ms = _srt_time_to_ms(right.start_time) - _srt_time_to_ms(left.end_time)
        if gap_ms > MAX_LLM_MERGE_GAP_MS:
            return False

    merged_length = sum(
        _source_text_length(_get_source_text(e, lang_code), lang_code)
        for e in entries
    )
    return merged_length <= MAX_LLM_MERGE_SOURCE_LENGTH


def filter_glossary_by_entries(raw_glossary: str, entries: list[SubtitleEntry]) -> str:
    """过滤术语表：只保留字幕原文中实际出现的术语源词（大小写不敏感）。"""
    if not raw_glossary or not entries:
        return raw_glossary
    all_text = " ".join(
        line for e in entries for line in e.lines
    ).lower()
    kept = []
    for line in raw_glossary.splitlines():
        line_s = line.strip()
        if not line_s:
            continue
        for sep in ("->", "→"):
            if sep in line_s:
                src = line_s.split(sep, 1)[0].strip().lower()
                if src and src in all_text:
                    kept.append(line_s)
                break
        else:
            # 非术语行（如注释）保留
            kept.append(line_s)
    return "\n".join(kept)


def _format_glossary_prompt(raw: str) -> str:
    """将原始 glossary 文本（source -> target 格式）格式化为 prompt 字符串。"""
    if not raw:
        return ""
    pairs = []
    for line in raw.splitlines():
        line = line.strip()
        for sep in ("->", "→"):
            if sep in line:
                parts = line.split(sep, 1)
                if parts[0].strip() and parts[1].strip():
                    pairs.append(f'"{parts[0].strip()}" 必须译为 "{parts[1].strip()}"')
                break
    if not pairs:
        return ""
    return "★ 【强制术语表】遇到以下英文专业词汇，必须严格统一翻译为对应中文，绝不允许替换为同义词或意译：\n" + "\n".join(
        f"  - {p}" for p in pairs) + "\n"


def _parse_glossary_lines(raw: str) -> list[tuple[str, str]]:
    """Parse source->target glossary lines, preserving order."""
    pairs: list[tuple[str, str]] = []
    if not raw:
        return pairs
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        for sep in ("->", "→"):
            if sep in line:
                source, target = line.split(sep, 1)
                source = source.strip()
                target = target.strip()
                if source and target:
                    pairs.append((source, target))
                break
    return pairs


def _term_rules_to_glossary_text(term_rules) -> str:
    """Convert analysis term_rules into editable source->target text."""
    if not isinstance(term_rules, list):
        return ""

    seen: set[str] = set()
    lines: list[str] = []
    for item in term_rules:
        if isinstance(item, dict):
            source = str(item.get("source", "")).strip()
            target = str(item.get("target", "")).strip()
        elif isinstance(item, str):
            parsed = _parse_glossary_lines(item)
            if not parsed:
                continue
            source, target = parsed[0]
        else:
            continue

        key = source.casefold()
        if not source or not target or key in seen:
            continue
        seen.add(key)
        lines.append(f"{source}->{target}")
    return "\n".join(lines)


def _dedupe_glossary_text(raw: str) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for source, target in _parse_glossary_lines(raw):
        key = source.casefold()
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{source}->{target}")
    return "\n".join(lines)


def _extract_glossary_from_analysis_text(text: str) -> str:
    """Best-effort fallback for non-strict JSON or prose analysis output."""
    if not text:
        return ""

    lines: list[str] = []

    json_like_pairs = re.findall(
        r'"source"\s*:\s*"([^"]+)"\s*,\s*"target"\s*:\s*"([^"]+)"',
        text,
        flags=re.S,
    )
    for source, target in json_like_pairs:
        if source.strip() and target.strip():
            lines.append(f"{source.strip()}->{target.strip()}")

    arrow_pairs = re.findall(
        r'([A-Za-z][A-Za-z0-9 ._/+\-&()]{1,80}?)\s*(?:->|→)\s*([^；;\n，,（）()]{1,80})',
        text,
    )
    for source, target in arrow_pairs:
        source = source.strip(" -•\t")
        target = target.strip(" -•\t")
        if source and target:
            lines.append(f"{source}->{target}")

    return _dedupe_glossary_text("\n".join(lines))


def _analysis_json_from_raw(raw: str) -> dict:
    try:
        clean_raw = re.sub(r'```(?:json)?|```', '', raw).strip()
        parsed = json.loads(clean_raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _build_analysis_result(raw: str) -> AnalysisResult:
    parsed = _analysis_json_from_raw(raw)
    auto_glossary = _term_rules_to_glossary_text(parsed.get("term_rules"))
    if not auto_glossary:
        auto_glossary = _extract_glossary_from_analysis_text(raw)
    return AnalysisResult(
        context=_format_analysis_result(raw),
        auto_glossary=auto_glossary,
        raw=raw,
    )


def merge_glossaries(user_glossary: str, auto_glossary: str) -> GlossaryMergeResult:
    """Merge user and auto glossary text. User terms always win on conflict."""
    user_pairs = _parse_glossary_lines(user_glossary)
    auto_pairs = _parse_glossary_lines(auto_glossary)

    merged_lines = [f"{source}->{target}" for source, target in user_pairs]
    user_by_key = {source.casefold(): target for source, target in user_pairs}
    auto_seen: set[str] = set()
    added: list[tuple[str, str]] = []
    conflicts: list[tuple[str, str, str]] = []

    for source, target in auto_pairs:
        key = source.casefold()
        if key in auto_seen:
            continue
        auto_seen.add(key)

        if key in user_by_key:
            if user_by_key[key] != target:
                conflicts.append((source, user_by_key[key], target))
            continue

        merged_lines.append(f"{source}->{target}")
        added.append((source, target))

    return GlossaryMergeResult(
        raw="\n".join(merged_lines),
        added=added,
        conflicts=conflicts,
    )


def format_glossary_merge_log(result: GlossaryMergeResult) -> list[str]:
    lines: list[str] = []
    auto_count = len(result.added) + len(result.conflicts)
    lines.append(
        f"阶段一自动提取术语 {auto_count} 条，新增 {len(result.added)} 条；"
        f"冲突 {len(result.conflicts)} 条已按用户词汇表优先处理。"
    )
    if result.added:
        lines.append("自动新增术语：")
        for i, (source, target) in enumerate(result.added, start=1):
            lines.append(f"  {i}. {source} -> {target}")
    if result.conflicts:
        lines.append("因用户词汇表已指定而未覆盖：")
        for source, user_target, auto_target in result.conflicts:
            lines.append(f"  - {source}: 用户={user_target}；自动={auto_target}")
    return lines


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


def _sample_entries_for_analysis(entries: list[SubtitleEntry], max_items: int = 80) -> list[SubtitleEntry]:
    """Sample subtitles across the full file so stage 1 can see real domain terms."""
    if len(entries) <= max_items:
        return entries
    if max_items <= 1:
        return entries[:max_items]

    last = len(entries) - 1
    indexes = {
        round(i * last / (max_items - 1))
        for i in range(max_items)
    }
    return [entries[i] for i in sorted(indexes)]


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
    input_lang_code = LANGUAGE_MAP.get(input_lang, ("en", input_lang))[0]
    input_lang = _get_prompt_lang(input_lang)
    output_lang = _get_prompt_lang(output_lang)
    sample = _sample_entries_for_analysis(entries)
    sample_text = "\n".join(
        f"[{e.index}] {_get_source_text(e, input_lang_code)}"
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
        "4. 样本来自整部字幕的前中后位置；请跨样本识别可迁移的术语和固定译法，不要只总结开场内容。\n"
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
    共享：发送一批字幕给 AI 优化，返回 {index: [tgt, src]}。
    prev_ctx 为上一批末尾若干条（只作上下文参考，不要求 AI 返回）。
    """
    # 获取语言代码，用于获取正确的原文文本
    input_lang_code = LANGUAGE_MAP.get(input_lang, ("en", input_lang))[0]
    # 转换为 prompt 使用的语言名称
    input_lang_prompt = _get_prompt_lang(input_lang)
    output_lang_prompt = _get_prompt_lang(output_lang)
    # 统一使用 _get_source_text() 和 _get_target_text() 获取文本，消除语言分支
    items_json = json.dumps([
        {"id": e.index, "src": _get_source_text(e, input_lang_code), "tgt": _get_target_text(e, input_lang_code)}
        for e in batch
    ], ensure_ascii=False)

    sys_content = (
        f"你是专业出版级字幕编辑。\n"
        f"【背景与风格】\n{context}\n\n"
        f"任务：优化已有{output_lang_prompt}译文。\n\n"

        "注意：\n"
        "输入译文已经完成翻译。\n"
        "你的职责不是重新翻译，而是进行出版级润色。\n\n"

        "【目标】\n"
        "让译文像目标语言母语作者直接写出来的一样自然。\n\n"

        "【优先级】\n"
        "信息不变 > 逻辑不变 > 术语统一 > 自然流畅 > 文采。\n\n"

        "规则：\n"

        "1.【零增删】\n"
        "不得增加信息。\n"
        "不得删除信息。\n"
        "不得改变事实。\n\n"

        "2.【句法重构】\n"
        "允许调整语序。\n"
        "允许主动被动转换。\n"
        "允许拆句。\n"
        "允许合句。\n"
        "允许重写表达。\n\n"

        "3.【去机器翻译味】\n"
        "删除英文句法残留。\n"
        "删除逐词对应痕迹。\n"
        "避免中式英语表达。\n\n"

        "4.【术语一致】\n"
        "同一专业术语必须统一。\n"
        "一致性优先于文采。\n\n"

        "5.【领域适配】\n"
        "若是交易课程：\n"
        "采用专业交易导师授课风格。\n"
        "简洁。\n"
        "明确。\n"
        "结论优先。\n\n"

        "6.【字幕适配】\n"
        "避免过长书面句。\n"
        "保持口语可读性。\n"
        "符合视频字幕阅读节奏。\n\n"

        "7.【自然表达】\n"
        "优先使用目标语言母语者常用表达。\n"
        "不要机械对应英文结构。\n\n"

        "8.【格式与对应关系】\n"
        "严格按JSON格式返回，不加任何解释。\n"
        "不得合并条目，输入几条返回几条，id 严格一一对应。\n"
        "src 字段原样返回，一字不差，不得修改或截断。\n"
        "若 src 为空，tgt 也留空。\n\n"

        "9.【禁止】\n"
        "不得输出Markdown。\n"
        "不得输出解释。\n"
        "不得输出注释。\n\n"

        f'返回格式：[{{"id":1,"tgt":"润色后译文","src":"原文"}}]'
    )

    if glossary:
        sys_content += (
            "\n\n【术语表执行要求】\n"
            "术语表是硬性约束，优先级高于文采；同一 source 在全文必须使用同一 target。\n"
            "用户词汇表优先于阶段一自动术语；如果译文已偏离术语表，必须改回术语表译法。\n"
            "不要把术语表解释写进字幕，只使用指定译法完成润色。"
        )

    glossary_note = f"\n\n【术语表 — 翻译时务必使用】\n{glossary}" if glossary else ""

    if prev_ctx:
        # 上下文也使用统一的字段名
        ctx_json = json.dumps([
            {"id": e.index, "src": _get_source_text(e, input_lang_code), "tgt": _get_target_text(e, input_lang_code)}
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
        # 移除控制字符（除了换行和制表符）
        clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', clean)
        logger.debug(f"API响应: {clean[:500]}...")
        try:
            data = json.loads(clean)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON解析失败，尝试修复: {e}")
            # 尝试修复常见的 JSON 错误
            clean = re.sub(r',\s*}', '}', clean)
            clean = re.sub(r',\s*]', ']', clean)
            try:
                data = json.loads(clean)
            except json.JSONDecodeError:
                logger.error(f"JSON修复失败，原始响应: {response[:200]}")
                return {e.index: e.lines for e in batch}
        api_map = {item["id"]: item for item in data}
        logger.debug(f"API返回 {len(data)} 条，批次有 {len(batch)} 条")

        first_pass = []
        missing_tgt_entries = []
        for entry in batch:
            if entry.index in api_map:
                item = api_map[entry.index]
                tgt_raw = item.get("tgt", "")
                # 兼容旧格式：如果 tgt 为空，尝试 zh 字段
                if not tgt_raw:
                    tgt_raw = item.get("zh", "")
                tgt = strip_annotation(str(tgt_raw).strip()) if tgt_raw else ""
                tgt_incomplete = _translation_looks_incomplete(tgt, _get_source_text(entry, input_lang_code))
                tgt_valid = bool(tgt) and tgt.lower() not in ("none", "null", "无", "—", "-")
                tgt_valid = tgt_valid and not tgt_incomplete
                if not tgt_valid:
                    logger.debug(f"条目 {entry.index} tgt无效: tgt='{tgt}', incomplete={tgt_incomplete}")
                first_pass.append((entry, tgt, tgt_valid))
                if not tgt_valid:
                    missing_tgt_entries.append(entry)
            else:
                logger.debug(f"条目 {entry.index} 不在API响应中")
                first_pass.append((entry, "", False))
                missing_tgt_entries.append(entry)

        retry_map: dict[int, str] = {}
        if missing_tgt_entries:
            # 重试时使用统一的字段名
            retry_items = json.dumps([
                {"id": e.index, "src": _get_source_text(e, input_lang_code)}
                for e in missing_tgt_entries
            ], ensure_ascii=False)

            retry_sys = (
                f"你是专业字幕翻译师。\n【背景与风格】\n{context}\n\n"
                f"任务：将{input_lang_prompt}字幕翻译成{output_lang_prompt}，严格遵循上述风格指引（如有）。\n"
            )
            retry_sys += (
                "规则：\n1. 用词语气与风格指引一致\n"
                "2. 严格按JSON格式返回，不要加任何解释\n"
                "3. 每条必须有tgt字段，不得为空或返回None；若src为空，tgt也返回空字符串\n"
                "4. 【严禁重复翻译】每条只译自己的src内容，src是半句就译半句；除非相邻条目的src本身内容相同或重复，"
                "不得让相邻条目的tgt相同或重复\n"
                f"5. 【严禁摘要】不得把长原文压缩成一句概括性{output_lang_prompt}，必须完整覆盖src中的全部信息点\n"
                "6. 【覆盖自检】返回前逐条检查：tgt是否完整覆盖了src的主语、动作、条件、原因、结果和关键术语\n"
                "7. 【严禁】tgt只允许纯文本，禁止任何Markdown符号（**、*、`、#、>、-列表等）\n"
                "8. 【严禁】tgt只含译文正文，不得添加括号注释、标注或说明文字\n\n"
                f'返回格式：[{{"id": 1, "tgt": "{output_lang_prompt}翻译正文"}}]'
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
                # 兼容旧格式：优先使用 tgt，如果为空则使用 zh
                retry_map = {item["id"]: item.get("tgt", "") or item.get("zh", "") for item in retry_data}
            except Exception:
                pass

        result: dict[int, list[str]] = {}
        for entry, tgt, tgt_valid in first_pass:
            opt_lines = []
            if tgt_valid:
                opt_lines.append(tgt)
            elif entry.index in retry_map:
                fallback_tgt = strip_annotation(str(retry_map[entry.index]).strip())
                # 获取原文文本用于完整性检查
                source_text_for_check = _get_source_text(entry, input_lang_code)
                fallback_incomplete = _translation_looks_incomplete(fallback_tgt, source_text_for_check)
                if fallback_tgt and fallback_tgt.lower() not in ("none", "null") and not fallback_incomplete:
                    opt_lines.append(fallback_tgt)
            # 获取原文文本（支持任意语言）
            source_text = _get_source_text(entry, input_lang_code)
            if source_text:
                opt_lines.append(source_text)
            has_translation = len(opt_lines) > 0 and any(line.strip() for line in opt_lines)
            result[entry.index] = opt_lines if has_translation else entry.lines

        return result
    except Exception:
        return {e.index: e.lines for e in batch}


class AnalysisWorker(BaseWorker):
    """第一阶段：分析字幕确定翻译情景"""
    finished = pyqtSignal(object)
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
            result = await pipeline.analyze_details(
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
            for entry in result:
                self.entry_done.emit(entry.index, entry.lines, entry.status or "done")
            self.all_done.emit()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.error.emit(f"OptimizeWorker 异常: {e}")


async def _translate_batch_impl(
    batch: list[SubtitleEntry],
    context: str,
    glossary: str,
    client: MistralClient,
    session: aiohttp.ClientSession,
    input_lang: str = "English",
    output_lang: str = "中文",
    prev_ctx: list[SubtitleEntry] | None = None,
) -> dict[int, str]:
    """共享：翻译一批字幕，返回 {index: tgt}。失败返回空字典。"""
    # 获取输入语言代码，用于获取正确的原文文本
    input_lang_code = LANGUAGE_MAP.get(input_lang, ("en", input_lang))[0]
    # 转换为 prompt 使用的语言名称
    input_lang_prompt = _get_prompt_lang(input_lang)
    output_lang_prompt = _get_prompt_lang(output_lang)
    items_json = json.dumps([
        {"id": e.index, "src": _get_source_text(e, input_lang_code)} for e in batch
    ], ensure_ascii=False)

    system_content = (
        f"你是专业字幕翻译师。\n"
        f"【背景与风格】\n{context}\n\n"
        f"任务：将{input_lang_prompt}字幕翻译成{output_lang_prompt}。\n\n"

        "【核心原则】\n"
        "本阶段只追求『信』。\n"
        "信息完整性 > 术语准确性 > 逻辑关系 > 语言优美度。\n"
        "允许译文略显生硬，但绝不允许漏译、缩译、概括、改写原意。\n\n"

        "规则：\n"

        "1.【信息完整】\n"
        "原文中的事实、动作、条件、因果关系、结论、数字、术语必须完整保留。\n\n"

        "2.【逻辑保真】\n"
        "because、therefore、however、although、if、unless、instead、rather than 等逻辑关系必须体现。\n"
        "不得省略转折、条件、因果。\n\n"

        "3.【术语一致】\n"
        "专业术语必须统一译法。\n"
        "同一术语在同一项目中不得随意更换表达。\n\n"

        "4.【禁止摘要】\n"
        "不得把长句压缩成概括性表达。\n"
        "必须保留全部信息单元。\n\n"

        "5.【禁止提前翻译】\n"
        "每条仅翻译自己的 src。\n"
        "不得把下一条内容提前翻译进来。\n\n"

        "6.【允许不够优美】\n"
        "若自然表达与完整表达冲突，优先完整表达。\n\n"

        "7.【格式与对应关系】\n"
        "严格返回 JSON，不加任何解释。\n"
        "不得合并条目，输入几条必须返回几条，id 与输入一一对应。\n"
        "每条必须有 tgt 字段；若 src 为空或仅含空白，tgt 也返回空字符串。\n\n"

        "8.【严禁】\n"
        "不得输出 Markdown。\n"
        "不得输出解释。\n"
        "不得输出注释。\n\n"

        f'返回格式：[{{"id":1,"tgt":"{output_lang_prompt}译文"}}]'
    )

    if glossary:
        system_content += (
            "\n\n【术语表执行要求】\n"
            "术语表是硬性约束，优先级高于自然表达；同一 source 在全文必须使用同一 target。\n"
            "用户词汇表优先于阶段一自动术语；遇到术语表中的词必须使用指定译法。\n"
            "不要提前翻译上下文中的其他条目，也不要把术语解释写进字幕。"
        )

    glossary_note = f"\n\n【术语表 — 翻译时务必使用】\n{glossary}" if glossary else ""

    if prev_ctx:
        ctx_json = json.dumps([
            {"id": e.index, "src": _get_source_text(e, input_lang_code),
             "tgt": _get_target_text(e, input_lang_code)}
            for e in prev_ctx
        ], ensure_ascii=False)
        user_content = (
            f"【上文参考（仅供语义衔接与译法对齐，无需翻译这些条目）】\n{ctx_json}\n\n"
            f"【待翻译字幕】\n{items_json}{glossary_note}"
        )
    else:
        user_content = f"翻译以下字幕：\n{items_json}{glossary_note}"

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content}
    ]

    try:
        response = await client.chat(messages, session)
        clean = re.sub(r'```(?:json)?|```', '', response).strip()
        # 移除控制字符（除了换行和制表符）
        clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', clean)
        logger.warning(f"翻译API响应: {clean[:1000]}")
        try:
            data = json.loads(clean)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON解析失败，尝试修复: {e}")
            # 尝试修复常见的 JSON 错误
            # 1. 移除尾部逗号
            clean = re.sub(r',\s*}', '}', clean)
            clean = re.sub(r',\s*]', ']', clean)
            # 2. 补全缺失的 ] 或 }
            if clean.startswith('[') and not clean.rstrip().endswith(']'):
                clean = clean.rstrip() + ']'
                logger.warning("JSON修复: 补全缺失的 ']'")
            elif clean.startswith('{') and not clean.rstrip().endswith('}'):
                clean = clean.rstrip() + '}'
                logger.warning("JSON修复: 补全缺失的 '}'")
            # 3. 尝试再次解析
            try:
                data = json.loads(clean)
            except json.JSONDecodeError:
                logger.error(f"JSON修复失败，原始响应: {response[:200]}")
                return {}
        result: dict[int, str] = {}
        for item in data:
            tgt = item.get("tgt", "")
            # 兼容旧格式：如果 tgt 为空，尝试 zh 字段
            if not tgt:
                tgt = item.get("zh", "")
            tgt = strip_annotation(str(tgt).strip()).replace('\n', ' ') if tgt else ""
            if tgt.lower() in ("none", "null", "无", "—", "-", ""):
                tgt = ""
            result[item["id"]] = tgt
        empty_ids = [item["id"] for item in data if not (item.get("tgt") or item.get("zh", "")).strip()]
        logger.warning(f"翻译结果: {len(result)} 条，非空: {sum(1 for v in result.values() if v)} 条，空译文id: {empty_ids if empty_ids else '无'}")
        return result
    except Exception as e:
        logger.error(f"翻译解析失败: {e}")
        return {}


async def _repair_incomplete_segment_translations(
    entries: list[SubtitleEntry],
    context: str,
    glossary: str,
    client: MistralClient,
    session: aiohttp.ClientSession,
    input_lang: str = "English",
    output_lang: str = "中文",
) -> tuple[int, int]:
    """修复漏译条目，返回 (修复成功数, 漏译总数)。"""
    input_lang_code = LANGUAGE_MAP.get(input_lang, ("en", input_lang))[0]
    incomplete = [
        entry for entry in entries
        if _translation_looks_incomplete(entry.target, _get_source_text(entry, input_lang_code))
    ]
    if not incomplete:
        return 0, 0

    logger.warning(
        "Segment translation coverage check found %s incomplete entries: %s",
        len(incomplete),
        [entry.index for entry in incomplete],
    )

    # 记录修复前的状态
    for entry in incomplete:
        source_text = _get_source_text(entry, input_lang_code)
        logger.warning(
            "  [ID:%d] 漏译检测: 原文=%d词, 译文=%d字, 原译文='%s'",
            entry.index, _english_word_count(source_text), _chinese_char_count(entry.target), entry.target
        )

    CONTEXT_SIZE = 3
    index_pos = {entry.index: pos for pos, entry in enumerate(entries)}
    retry_map: dict[int, str] = {}
    for entry in incomplete:
        pos = index_pos[entry.index]
        prev_ctx = entries[max(0, pos - CONTEXT_SIZE):pos]
        single_map = await _translate_batch_impl(
            [entry], context, glossary, client, session, input_lang, output_lang, prev_ctx)
        retry_map.update(single_map)

    repaired_count = 0
    for entry in incomplete:
        tgt = strip_annotation(str(retry_map.get(entry.index, "")).strip()).replace('\n', ' ')
        if not tgt or tgt.lower() in ("none", "null", "无", "—", "-"):
            logger.warning("  [ID:%d] 修复失败: API返回空译文", entry.index)
            continue
        source_text = _get_source_text(entry, input_lang_code)
        old_tgt = entry.target
        entry.lines = [tgt, source_text] if source_text else [tgt]
        repaired_count += 1
        logger.warning(
            "  [ID:%d] 已修复: '%s' → '%s'",
            entry.index, old_tgt, tgt
        )

    logger.warning("漏译修复完成: %d/%d 条已修复", repaired_count, len(incomplete))
    return repaired_count, len(incomplete)


def _translation_is_usable(translation: str, source_text: str, output_lang: str = "zh") -> bool:
    translation = strip_annotation(str(translation or "").strip())
    if (
        not translation
        or translation.lower() in {"none", "null", "nan"}
        or translation in {"-", "\u2014", "\u65e0"}
    ):
        return False

    output_lang_code = LANGUAGE_MAP.get(output_lang, ("zh", output_lang))[0]
    if output_lang_code == "zh" and _translation_looks_incomplete(translation, source_text):
        return False
    return True


async def _translate_batch_with_repair(
    batch: list[SubtitleEntry],
    context: str,
    glossary: str,
    client: MistralClient,
    session: aiohttp.ClientSession,
    input_lang: str = "English",
    output_lang: str = "zh",
    prev_ctx: list[SubtitleEntry] | None = None,
) -> tuple[dict[int, str], list[int]]:
    input_lang_code = LANGUAGE_MAP.get(input_lang, ("en", input_lang))[0]
    first_map = await _translate_batch_impl(
        batch, context, glossary, client, session, input_lang, output_lang, prev_ctx
    )

    missing_entries = []
    for entry in batch:
        source_text = _get_source_text(entry, input_lang_code)
        translation = first_map.get(entry.index, "")
        if not _translation_is_usable(translation, source_text, output_lang):
            missing_entries.append(entry)

    retry_map: dict[int, str] = {}
    if missing_entries:
        logger.warning(
            "Translation coverage retry for ids: %s",
            [entry.index for entry in missing_entries],
        )
        retry_map = await _translate_batch_impl(
            missing_entries, context, glossary, client, session, input_lang, output_lang
        )

    result: dict[int, str] = {}
    unresolved: list[int] = []
    for entry in batch:
        source_text = _get_source_text(entry, input_lang_code)
        translation = first_map.get(entry.index, "")
        if not _translation_is_usable(translation, source_text, output_lang):
            translation = retry_map.get(entry.index, "")

        translation = strip_annotation(str(translation or "").strip())
        if _translation_is_usable(translation, source_text, output_lang):
            result[entry.index] = translation
        else:
            unresolved.append(entry.index)

    if unresolved:
        logger.error("Translation unresolved after retry for ids: %s", unresolved)
    return result, unresolved


def _apply_llm_merge(
    batch: list[SubtitleEntry],
    llm_response: list[dict],
    log_fn=None,
    lang_code: str = "en",
) -> list[SubtitleEntry]:
    """将LLM合并建议应用到条目列表。

    LLM 返回分组列表，每组指定哪些相邻 id 应合并为一条。
    合并后的文本直接由各条原文拼接而来（LLM不负责输出正文），
    时间戳取组内第一条 start ~ 最后一条 end。

    返回格式示例：
        [{"ids": [3, 4]}, {"ids": [5]}, {"ids": [6, 7, 8]}]
    """
    by_id = {e.index: e for e in batch}
    results = []
    covered_ids = set()
    rejected_merge_ids = set()

    for group in llm_response:
        ids = group.get("ids", [])
        if not ids:
            continue

        # 过滤掉不存在或已处理的 id
        valid_ids = [i for i in ids if i in by_id and i not in covered_ids]
        if not valid_ids:
            continue

        if len(valid_ids) == 1:
            # 单条，直接保留
            e = by_id[valid_ids[0]]
            result = SubtitleEntry(e.index, e.start_time, e.end_time, list(e.lines))
            result.source_indexes = list(e.source_indexes or [e.index])
            result.original_lines = list(e.original_lines or e.lines)
            results.append(result)
            covered_ids.add(valid_ids[0])
            continue

        # 多条合并：按 id 在原 batch 中的顺序排列，保证时间连续
        entries_in_group = sorted(
            [by_id[i] for i in valid_ids],
            key=lambda e: _srt_time_to_ms(e.start_time),
        )
        if not _llm_merge_group_is_safe(batch, entries_in_group, lang_code):
            if log_fn:
                log_fn(f"  ⚠️ 拒绝不安全合并组 {valid_ids}，组内条目将保留原样")
            rejected_merge_ids.update(valid_ids)
            continue

        merged_text = " ".join(
            _get_source_text(e, lang_code).strip() for e in entries_in_group
        )
        merged_text = merged_text.strip()

        merged = SubtitleEntry(
            index=entries_in_group[0].index,
            start_time=entries_in_group[0].start_time,
            end_time=entries_in_group[-1].end_time,
            lines=[merged_text],
        )
        merged.source_indexes = []
        for e in entries_in_group:
            merged.source_indexes.extend(e.source_indexes or [e.index])
        merged.original_lines = list(entries_in_group[0].original_lines or entries_in_group[0].lines)

        if log_fn:
            log_fn(f"  🔗 合并 {valid_ids} → [{entries_in_group[0].index}] \"{merged_text[:60]}{'...' if len(merged_text)>60 else ''}\"")

        results.append(merged)
        covered_ids.update(valid_ids)

    # 保留 LLM 未提及的条目，防止内容丢失
    for e in batch:
        if e.index not in covered_ids:
            if log_fn:
                if e.index in rejected_merge_ids:
                    log_fn(f"  ↩️ [{e.index}] 来自被拒绝的合并组，保留原样")
                else:
                    log_fn(f"  ⚠️ LLM未返回条目 [{e.index}]，保留原样")
            fallback = SubtitleEntry(e.index, e.start_time, e.end_time, list(e.lines))
            fallback.source_indexes = list(e.source_indexes or [e.index])
            results.append(fallback)

    # 按时间排序
    results.sort(key=lambda e: _srt_time_to_ms(e.start_time))
    return results


def _split_short_long(batch: list[SubtitleEntry], max_words: int = 20, lang_code: str = "en"):
    """分离短条目（直通）和长条目（需LLM切分）。"""
    short_entries = []
    long_entries = []
    for e in batch:
        source_text = _get_source_text(e, lang_code)
        if _source_text_length(source_text, lang_code) <= max_words:
            short_entries.append(e)
        else:
            long_entries.append(e)
    return short_entries, long_entries


def _apply_llm_split(long_entries: list[SubtitleEntry], llm_response: list[dict],
                     log_fn=None, lang_code: str = "en",
                     fallback_to_local: bool = True) -> list[SubtitleEntry]:
    """将LLM切分建议应用到长条目，按词数比例分配时间戳。

    LLM 只返回断点位置（break_after_word），代码用原文 words 切片构建各段文本。
    这样内容 100% 来自原始字符串，LLM 无法截断或遗漏任何词。
    """
    results = []
    by_id = {e.index: e for e in long_entries}
    processed_ids = set()

    for item in llm_response:
        entry_id = item.get("id")
        orig = by_id.get(entry_id)
        if not orig:
            continue

        processed_ids.add(entry_id)

        orig_text = _get_source_text(orig, lang_code)
        words = orig_text.split()
        total_word_count = len(words)

        # LLM 返回断点列表，例如 [10, 20] 表示在第10词后、第20词后各断一次
        # 不断点（空列表或缺失）= 无需切分，保留原条目
        raw_breaks = item.get("break_after_word", [])
        if not raw_breaks:
            if fallback_to_local:
                results.extend(_force_split_entry(orig, lang_code=lang_code, log_fn=log_fn))
            else:
                results.append(_clone_segment_entry(orig))
            continue

        # 过滤非法断点：必须在 1 ~ total_word_count-1 范围内，且严格递增
        breaks = []
        for bp in sorted(int(b) for b in raw_breaks if isinstance(b, (int, float))):
            if 1 <= bp < total_word_count:
                if not breaks or bp > breaks[-1]:
                    breaks.append(bp)

        if not breaks:
            # 断点全部非法，退回保留原条目
            if log_fn:
                log_fn(f"  ⚠️ [{orig.index}] LLM返回的断点无效 {raw_breaks}，保留原条目")
            if fallback_to_local:
                results.extend(_force_split_entry(orig, lang_code=lang_code, log_fn=log_fn))
            else:
                results.append(_clone_segment_entry(orig))
            continue

        # 用断点将 words 切成若干段，内容直接来自原文 words 切片
        boundaries = [0] + breaks + [total_word_count]
        seg_texts = [
            " ".join(words[boundaries[i]:boundaries[i + 1]])
            for i in range(len(boundaries) - 1)
        ]

        # 完整性校验：切分后文本拼接必须与原文一致
        rebuilt = " ".join(seg_texts)
        if rebuilt.strip() != orig_text.strip():
            if log_fn:
                log_fn(f"  ⚠️ [{orig.index}] 切分后文本不完整，回退原条目")
            if fallback_to_local:
                results.extend(_force_split_entry(orig, lang_code=lang_code, log_fn=log_fn))
            else:
                results.append(_clone_segment_entry(orig))
            continue

        if log_fn:
            log_fn(f"  ✂️ 切分 [{orig.index}] → {len(seg_texts)} 段")

        total_ms = _srt_time_to_ms(orig.end_time) - _srt_time_to_ms(orig.start_time)
        if total_ms <= 0:
            total_ms = 1
        current_ms = _srt_time_to_ms(orig.start_time)

        for i, seg_text in enumerate(seg_texts):
            sw = _source_text_length(seg_text, lang_code)
            seg_duration = int(total_ms * sw / total_word_count) if total_word_count > 0 else int(total_ms / len(seg_texts))
            if seg_duration < 200:
                seg_duration = int(total_ms / len(seg_texts))

            seg_start = _ms_to_srt_time(current_ms)
            seg_end = _ms_to_srt_time(current_ms + seg_duration)
            current_ms += seg_duration

            # 负数临时 index：SRT 自然 index 均为正整数，负数保证零碰撞。
            # segment() 末尾 renumber 步骤（enumerate(deduped, start=1)）会统一重赋为 1..N。
            seg_entry = SubtitleEntry(
                index=-(orig.index * 10000 + i + 1),
                start_time=seg_start,
                end_time=seg_end,
                lines=[seg_text],
            )
            seg_entry.original_lines = list(orig.original_lines or orig.lines)
            seg_entry.source_indexes = list(orig.source_indexes or [orig.index])
            if fallback_to_local:
                results.extend(_force_split_entry(seg_entry, lang_code=lang_code, log_fn=log_fn))
            else:
                results.append(seg_entry)

    # 保留 LLM 未处理的条目（防止内容丢失）
    for entry in long_entries:
        if entry.index not in processed_ids:
            if log_fn:
                log_fn(f"  ⚠️ LLM未返回条目 [{entry.index}]，保留原样")
            if fallback_to_local:
                results.extend(_force_split_entry(entry, lang_code=lang_code, log_fn=log_fn))
            else:
                results.append(_clone_segment_entry(entry))

    return results


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
            for entry in result:
                self.entry_done.emit(entry.index, entry.lines, entry.status or "done")
            self.all_done.emit()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.error.emit(f"TranslateWorker 异常: {e}")


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
                 overwrite: bool = False, target_first: bool = True,
                 glossary_prompt: str = "", raw_glossary: str = "",
                 original_line: int = 1,
                 input_lang: str = "English", output_lang: str = "中文"):
        super().__init__()
        self.client = client
        self.files = files
        self.out_dir = out_dir
        self.concurrency = concurrency
        self.batch_size = batch_size
        self.overwrite = overwrite
        self.target_first = target_first
        self.glossary_prompt = glossary_prompt
        self.raw_glossary = raw_glossary
        self.original_line = original_line  # 原文行号（0-based）
        self.input_lang = input_lang
        self.output_lang = output_lang
        self.input_lang_code = LANGUAGE_MAP.get(input_lang, ("en", input_lang))[0]

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

                # Filter user glossary first; auto terms are merged after stage 1 analysis.
                user_glossary = ""
                if self.raw_glossary:
                    user_glossary = filter_glossary_by_entries(self.raw_glossary, entries)

                self.log.emit(f"  🔍 [{filename}] 分析情景（{len(entries)} 条字幕）...")
                analysis = await pipeline.analyze_details(
                    entries,
                    is_cancelled=lambda: self._cancelled,
                )
                context = analysis.context
                glossary_merge = merge_glossaries(user_glossary, analysis.auto_glossary)
                glossary = _format_glossary_prompt(glossary_merge.raw)
                self.file_analysis_done.emit(idx, filename, context)
                self.log.emit(f"  ✅ [{filename}] 情景分析完成")
                if analysis.auto_glossary:
                    for line in format_glossary_merge_log(glossary_merge):
                        self.log.emit(f"  📖 [{filename}] {line}")
                elif user_glossary:
                    self.log.emit(f"  📖 [{filename}] 阶段一未提取自动术语，仅使用用户词汇表")

                if self._cancelled:
                    break

                self.log.emit(f"  🔗 [{filename}] 开始智能重组 {len(entries)} 条字幕... (并发: {self.concurrency}, 批次: {self.batch_size})")
                entries = await pipeline.segment(
                    entries, context, glossary,
                    on_progress=lambda c, t: self.file_progress.emit(idx, c, t),
                    on_log=self.log.emit,
                    is_cancelled=lambda: self._cancelled,
                )
                write_phase_snapshot(filename, "AFTER_SEGMENT", entries)
                self.log.emit(f"  ✅ [{filename}] 重组完成：{len(entries)} 条字幕")

                if self._cancelled:
                    break

                self.log.emit(f"  🌐 [{filename}] 开始翻译 {len(entries)} 条字幕... (并发: {self.concurrency}, 批次: {self.batch_size})")
                entries = await pipeline.translate(
                    entries, context, glossary,
                    on_progress=lambda c, t: self.file_progress.emit(idx, c, t),
                    on_log=self.log.emit,
                    is_cancelled=lambda: self._cancelled,
                )
                write_phase_snapshot(filename, "AFTER_TRANSLATE", entries)
                self.log.emit(f"  ✅ [{filename}] 翻译完成")

                if self._cancelled:
                    break

                self.log.emit(f"  ✨ [{filename}] 开始优化润色 {len(entries)} 条字幕... (并发: {self.concurrency}, 批次: {self.batch_size})")
                entries = await pipeline.optimize(
                    entries, context, glossary,
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

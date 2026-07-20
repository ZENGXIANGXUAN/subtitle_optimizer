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
from core.source_normalization import LAYOUT_AUTO, normalize_source_entries
from core.client import MistralClient
from core.transcription import (
    NO_TRANSCRIBABLE_AUDIO_ERROR,
    is_media_file,
    prepare_transcription_audio,
    should_use_chunked_transcription,
    transcribe_media_to_srt,
    transcribe_prepared_audio_to_srt,
    transcribe_chunked_to_srt,
)
from core.batch_glossary import (
    BatchGlossarySummary,
    GlossaryCandidate,
    GlossaryFileFailure,
    batch_glossary_review_reasoning_options,
    batch_glossary_reasoning_options,
    batch_glossary_unifier_candidate_limit,
    batch_glossary_unifier_timeout_seconds,
    filter_human_review_glossary_candidates,
    is_low_value_glossary_source,
    merge_batch_glossary_candidates,
    normalize_glossary_source_key,
    parse_batch_glossary_ai_review_result,
    parse_glossary_pairs,
    relevant_fixed_glossary,
)


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
    clone.source_cues = list(entry.source_cues)
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
        seg_entry.source_cues = list(entry.source_cues)
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
        if not source or not target or key in seen or is_low_value_glossary_source(source):
            continue
        seen.add(key)
        lines.append(f"{source}->{target}")
    return "\n".join(lines)


def _dedupe_glossary_text(raw: str) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for source, target in _parse_glossary_lines(raw):
        key = source.casefold()
        if key in seen or is_low_value_glossary_source(source):
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
    user_by_key = {normalize_glossary_source_key(source): target for source, target in user_pairs}
    auto_seen: set[str] = set()
    added: list[tuple[str, str]] = []
    conflicts: list[tuple[str, str, str]] = []

    for source, target in auto_pairs:
        key = normalize_glossary_source_key(source)
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
        return loop.run_until_complete(coro)
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


class TranscriptionWorker(BaseWorker):
    finished = pyqtSignal(str, str)  # srt_path, srt_content
    log = pyqtSignal(str)

    def __init__(self, client: MistralClient, media_path: str,
                 transcription_model: str = "", language: str = None,
                 transcription_audio_format: str = "mp3",
                 chunk_concurrency: int = 4):
        super().__init__()
        self.client = client
        self.media_path = media_path
        self.transcription_model = transcription_model
        self.language = language
        self.transcription_audio_format = transcription_audio_format
        self.chunk_concurrency = chunk_concurrency

    @pyqtSlot()
    def run(self):
        self._execute(self._run())

    async def _run(self):
        if self._cancelled:
            return
        self.log.emit(f"Transcribing media to SRT: {Path(self.media_path).name}")

        # 根据文件大小自动选择分段或普通转录
        use_chunked = should_use_chunked_transcription(self.media_path)
        if use_chunked:
            self.log.emit(f"Large file detected, using chunked transcription (concurrency={self.chunk_concurrency})")
            srt_path, content = await transcribe_chunked_to_srt(
                self.client,
                self.media_path,
                model=self.transcription_model or None,
                language=self.language,
                audio_format=self.transcription_audio_format,
                concurrency=self.chunk_concurrency,
                on_log=self.log.emit,
            )
        else:
            srt_path, content = await transcribe_media_to_srt(
                self.client,
                self.media_path,
                model=self.transcription_model or None,
                language=self.language,
                audio_format=self.transcription_audio_format,
                on_log=self.log.emit,
            )

        if not self._cancelled:
            self.finished.emit(srt_path, content)


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
        '  "domain": "内容领域，不确定时写通用内容/未明确",\n'
        '  "audience": "目标受众及其知识水平",\n'
        f'  "tone": "{output_lang}译文应采用的人称、语气、正式程度和教学节奏",\n'
        '  "context": "200字以内，说明主题、场景、核心概念、说话者意图和常见理解难点",\n'
        '  "translation_rules": ["3-5条可执行规则，直接指导逐条字幕怎么译"],\n'
        '  "term_rules": [\n'
        '    {"source": "样本中的专业术语、专有名词、缩写或固定短语", "target": "建议译法", "note": "保留/意译/统一用法说明"}\n'
        '  ],\n'
        '  "avoid": ["2-4条需要避免的问题，如直译腔、误译方向、过度解释、术语不统一"],\n'
        f'  "style_guide": "150字以内，写给后续翻译AI的综合指令，必须具体、可执行，目标语言是{output_lang}"\n'
        "}\n\n"
        "要求：\n"
        "1. 优先识别对后续翻译有约束力的信息：内容领域、语气、人称、句子长短、保留英文缩写的规则，以及候选术语。\n"
        "2. 不要写空泛摘要，例如“语言自然流畅”；必须说明怎样才算自然流畅。\n"
        "3. term_rules 只作为候选术语输出，后续会与用户词汇表合并；如果与用户词汇表冲突，必须以用户词汇表为准。\n"
        "   term_rules 只收录样本中真实出现、且会影响一致性的专业术语、专有名词、缩写、产品/功能名、领域短语或容易误译的固定表达。\n"
        "4. 不要收录普通常用词、泛泛动词/名词、无特殊译法的单词或只因高频出现而被重复提到的日常表达，例如 make、look、go、order 这类单词默认不要写入。\n"
        "5. 不要套用固定行业模板；必须根据样本实际内容判断领域和风格，样本没有体现的领域规则不要写入。\n"
        "6. 样本来自整部字幕的前中后位置；请跨样本识别可迁移的候选术语，不要只总结开场内容。\n"
        f"7. 所有字段必须使用{output_lang}填写；source 字段可保留{input_lang}原词。"
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

    # 注意：阶段一 term_rules 只作为“候选术语”用于生成最终 glossary，
    # 不再写入 context，避免后续翻译同时看到“阶段一术语”和“强制术语表”两套权威。
    # 下游所有阶段的术语约束只能来自 merge_glossaries() 合并后的唯一术语表。

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
    next_ctx: list[SubtitleEntry] | None = None,
) -> dict[int, list[str]]:
    """
    共享：发送一批字幕给 AI 优化，返回 {index: [tgt, src]}。
    prev_ctx/next_ctx 为待优化批次前后的若干条（只作上下文参考，不要求 AI 返回）。
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
        f"你是专业字幕审校与字幕本地化编辑。\n"
        f"【阶段一情境分析】\n{context}\n\n"
        f"任务：对照{input_lang_prompt}原文，审校并优化已有{output_lang_prompt}译文。\n\n"

        "【定位】\n"
        "这是第四阶段：不是重新做情境分析，也不是套用固定行业风格。\n"
        "你的唯一依据是：原文 src、已有译文 tgt、术语表、阶段一情境分析。\n"
        "阶段一判断是什么领域/受众/语气，你就按那个方向优化；阶段一没有判断出来的行业规则，不要自行添加。\n\n"

        "【优化目标】\n"
        "把“意思正确但像翻译稿”的译文，改成目标语言观众在该场景下自然听到的字幕。\n"
        "优化后应同时满足：信息完整、术语一致、语气贴合、句子顺口、字幕可读。\n\n"

        "【优先级】\n"
        "原文信息守恒 > 强制术语表 > 阶段一风格规约 > 目标语言自然表达 > 字幕节奏 > 文采。\n\n"

        "规则：\n"

        "1.【先核对原文】\n"
        "逐条对照 src 检查 tgt 是否漏译、误译、缩译、错译逻辑关系。\n"
        "发现问题必须修正；不得只在原译文表面替换几个词。\n\n"

        "2.【允许必要重写】\n"
        "如果原译文字面正确但不自然、翻译腔重、英文语序残留、搭配不符合目标语言习惯，必须重写。\n"
        "可调整语序、改写句型、拆分从句、改成更自然的口语/讲解表达。\n"
        "重写只为表达自然，不得增加、删除或改变原文信息。\n\n"

        "3.【消除出戏感】\n"
        "避免逐词对应、机械连接词、过度书面化、说明书腔、论文腔、AI腔。\n"
        "优先使用目标语言中符合该内容类型的真实表达：课程像课程，访谈像访谈，演示像演示，纪录片像纪录片。\n"
        "不要把所有内容都处理成同一种口吻。\n\n"

        "4.【使用阶段一，但不迷信阶段一】\n"
        "阶段一提供领域、受众、语气和避坑方向；术语只以【强制术语表】为准。\n"
        "如果阶段一为空、含糊或与当前 src 明显不匹配，以当前 src 的实际语义为准。\n"
        "不得因为阶段一提到某领域，就把当前条目没有的术语或行业说法强行塞进去。\n\n"

        "5.【术语一致】\n"
        "同一术语、专名、缩写、按钮名、功能名、概念名必须统一。\n"
        "如果术语表给了译法，必须使用术语表译法；不要把解释写进字幕。\n\n"

        "6.【参考上下文，但只改当前条目】\n"
        "你可以参考上文参考和下文参考来判断代词、承接关系、术语一致性、语气和半句的完整含义。\n"
        "参考区只用于理解和对齐，不得返回参考区条目，不得把参考区内容提前或滞后搬进待优化条目。\n"
        "每条只优化自己的 tgt，不要跨条合并。\n\n"

        "7.【字幕阅读节奏】\n"
        "译文要适合屏幕阅读：短、清楚、顺口。\n"
        "可以把过长的中文句子改短，但不能把信息概括掉。\n\n"

        "8.【保留必要形式】\n"
        "数字、时间、单位、人名、产品名、代码、命令、URL、文件名、专有名词要准确保留或按术语表处理。\n"
        "源文中的强调、否定、条件、转折、因果，译文必须体现。\n\n"

        "9.【格式与对应关系】\n"
        "严格按 JSON 返回，不加任何解释。\n"
        "不得合并条目，输入几条返回几条，id 严格一一对应。\n"
        "src 字段原样返回，一字不差，不得修改或截断。\n"
        "若 src 为空，tgt 也留空。\n\n"

        "10.【禁止】\n"
        "不得输出 Markdown。\n"
        "不得输出解释、注释、括号说明、译者说明。\n"
        "不得输出多个候选译文。\n\n"

        f'返回格式：[{{"id":1,"tgt":"优化后的{output_lang_prompt}字幕","src":"原文"}}]'
    )
    if glossary:
        sys_content += (
            "\n\n【术语表执行要求】\n"
            "术语表是硬性约束，优先级高于文采；同一 source 在全文必须使用同一 target。\n"
            "用户词汇表优先于阶段一自动术语；如果译文已偏离术语表，必须改回术语表译法。\n"
            "不要把术语表解释写进字幕，只使用指定译法完成润色。"
        )

    glossary_note = f"\n\n【术语表 — 翻译时务必使用】\n{glossary}" if glossary else ""

    reference_blocks: list[str] = []
    if prev_ctx:
        prev_json = json.dumps([
            {"id": e.index, "src": _get_source_text(e, input_lang_code), "tgt": _get_target_text(e, input_lang_code)}
            for e in prev_ctx
        ], ensure_ascii=False)
        reference_blocks.append(f"【上文参考（只用于风格/术语/语义衔接，无需返回这些条目）】\n{prev_json}")
    if next_ctx:
        next_json = json.dumps([
            {"id": e.index, "src": _get_source_text(e, input_lang_code), "tgt": _get_target_text(e, input_lang_code)}
            for e in next_ctx
        ], ensure_ascii=False)
        reference_blocks.append(f"【下文参考（只用于理解承接关系和术语一致性，无需返回这些条目）】\n{next_json}")

    if reference_blocks:
        user_content = (
            "\n\n".join(reference_blocks)
            + f"\n\n【待优化字幕】\n{items_json}"
            + f"{glossary_note}"
        )
    else:
        user_content = f"优化以下字幕：\n{items_json}{glossary_note}"

    messages = [
        {"role": "system", "content": sys_content},
        {"role": "user", "content": user_content}
    ]

    response = await client.chat(messages, session, temperature=0.2)

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
                raise ValueError("optimize_json_parse_failed")
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
                retry_response = await client.chat(retry_messages, session, temperature=0.1)
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
    except Exception as exc:
        logger.error("字幕优化批次失败: %s", exc)
        raise


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
    all_done = pyqtSignal(object)
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
            logger.debug(_format_subtitle_snapshot("AFTER_OPTIMIZE", result, use_optimized=True))
            for entry in result:
                lines = entry.optimized_lines if entry.optimized_lines else entry.lines
                self.entry_done.emit(entry.index, lines, entry.status or "done")
            self.all_done.emit(result)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.error.emit(f"OptimizeWorker 异常: {e}")


class PipelineExecutionWorker(BaseWorker):
    """Unified segment/translate/optimize worker used by single-task UI.

    BatchWorker calls the same ``SubtitlePipeline.execute`` coroutine directly.
    This Qt adapter only converts its callbacks into signals.
    """

    stage_started = pyqtSignal(str)
    stage_progress = pyqtSignal(str, int, int)
    stage_snapshot = pyqtSignal(str, object, bool)
    finished = pyqtSignal(object)
    cancelled = pyqtSignal()
    log = pyqtSignal(str)

    def __init__(
        self,
        client: MistralClient,
        entries: list[SubtitleEntry],
        context: str,
        glossary: str,
        concurrency: int = 4,
        batch_size: int = 5,
        segment_batch_size: int = 15,
        input_lang: str = "English",
        output_lang: str = "中文",
    ):
        super().__init__()
        self.client = client
        self.entries = entries
        self.context = context
        self.glossary = glossary
        self.concurrency = concurrency
        self.batch_size = batch_size
        self.segment_batch_size = segment_batch_size
        self.input_lang = input_lang
        self.output_lang = output_lang

    @pyqtSlot()
    def run(self):
        self._execute(self._run())

    async def _run(self):
        from core.pipeline import SubtitlePipeline

        try:
            pipeline = SubtitlePipeline(
                self.client,
                self.input_lang,
                self.output_lang,
                self.concurrency,
                self.batch_size,
            )
            result = await pipeline.execute(
                self.entries,
                self.context,
                self.glossary,
                segment_batch_size=self.segment_batch_size,
                on_stage=self.stage_started.emit,
                on_progress=self.stage_progress.emit,
                on_snapshot=self.stage_snapshot.emit,
                on_log=self.log.emit,
                is_cancelled=lambda: self._cancelled,
            )
            if not self._cancelled:
                self.finished.emit(result)
        except asyncio.CancelledError:
            self.cancelled.emit()
        except Exception as exc:
            self.error.emit(f"统一字幕任务异常: {exc}")


async def _translate_batch_impl(
    batch: list[SubtitleEntry],
    context: str,
    glossary: str,
    client: MistralClient,
    session: aiohttp.ClientSession,
    input_lang: str = "English",
    output_lang: str = "中文",
    prev_ctx: list[SubtitleEntry] | None = None,
    next_ctx: list[SubtitleEntry] | None = None,
    include_context_targets: bool = True,
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
        f"【阶段一情境分析】\n{context}\n\n"
        f"任务：将{input_lang_prompt}字幕翻译成{output_lang_prompt}。\n\n"

        "【定位】\n"
        "这是第三阶段：产出可直接观看、也可交给第四阶段继续润色的高质量初译。\n"
        "不要只做逐词直译；也不要自由改写。\n"
        "必须在准确完整的基础上，尽量使用目标语言自然表达。\n\n"

        "【核心原则】\n"
        "信息完整 > 原意准确 > 逻辑关系清楚 > 术语统一 > 阶段一风格匹配 > 目标语言自然。\n"
        "如果“自然”和“完整”冲突，优先完整；但不要故意保留英文语序或机器翻译腔。\n\n"

        "规则：\n"

        "1.【使用阶段一情境】\n"
        "根据阶段一提供的领域、受众、语气和避坑规则来选择用词；术语只以【强制术语表】为准。\n"
        "阶段一没有明确的内容，不要自行套用固定行业模板。\n"
        "如果阶段一与当前 src 明显不匹配，以当前 src 的实际语义为准。\n\n"

        "2.【信息完整】\n"
        "原文中的事实、动作、对象、条件、因果、转折、结论、数字、术语必须完整保留。\n"
        "不得漏译、缩译、摘要化、只翻大意。\n\n"

        "3.【逻辑保真】\n"
        "because、therefore、however、although、if、unless、instead、rather than 等逻辑关系必须体现。\n"
        "否定、限制、例外、比较、先后顺序不能丢。\n\n"

        "4.【自然但不加戏】\n"
        "允许调整语序、补出目标语言必要的主语或连接方式，使译文顺口。\n"
        "不得添加原文没有的新解释、新态度、新判断。\n"
        "避免逐词对应、英文语序残留、过度书面化和生硬连接词。\n\n"

        "5.【术语一致】\n"
        "专业术语、专名、缩写、按钮名、功能名、概念名必须统一。\n"
        "同一术语在同一项目中不得随意更换表达。\n\n"

        "6.【参考上下文，但只翻译当前条目】\n"
        "你可以参考上文参考和下文参考来判断代词、承接关系、术语一致性、语气和半句的完整含义。\n"
        "参考区只用于理解，不得翻译参考区条目，不得把参考区内容提前或滞后搬进当前条目。\n"
        "每条只翻译自己的 src；如果 src 是半句，就按半句翻译，保持可与上下文衔接。\n\n"

        "7.【字幕可读性】\n"
        "译文应适合屏幕阅读，尽量短句、清楚、顺口。\n"
        "不要为了文采拉长句子；不要输出解释性长句。\n\n"

        "8.【格式与对应关系】\n"
        "严格返回 JSON，不加任何解释。\n"
        "不得合并条目，输入几条必须返回几条，id 与输入一一对应。\n"
        "每条必须有 tgt 字段；若 src 为空或仅含空白，tgt 也返回空字符串。\n\n"

        "9.【严禁】\n"
        "不得输出 Markdown。\n"
        "不得输出解释、注释、括号说明、译者说明。\n"
        "不得输出多个候选译文。\n\n"

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

    reference_blocks: list[str] = []
    if prev_ctx:
        prev_json = json.dumps([
            ({"id": e.index, "src": _get_source_text(e, input_lang_code),
              "tgt": _get_target_text(e, input_lang_code)}
             if include_context_targets
             else {"id": e.index, "src": _get_source_text(e, input_lang_code)})
            for e in prev_ctx
        ], ensure_ascii=False)
        reference_blocks.append(f"【上文参考（只用于语义衔接与译法对齐，无需翻译这些条目）】\n{prev_json}")
    if next_ctx:
        next_json = json.dumps([
            ({"id": e.index, "src": _get_source_text(e, input_lang_code),
              "tgt": _get_target_text(e, input_lang_code)}
             if include_context_targets
             else {"id": e.index, "src": _get_source_text(e, input_lang_code)})
            for e in next_ctx
        ], ensure_ascii=False)
        reference_blocks.append(f"【下文参考（只用于理解承接关系、代词和术语一致性，无需翻译这些条目）】\n{next_json}")

    if reference_blocks:
        user_content = (
            "\n\n".join(reference_blocks)
            + f"\n\n【待翻译字幕】\n{items_json}{glossary_note}"
        )
    else:
        user_content = f"翻译以下字幕：\n{items_json}{glossary_note}"

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content}
    ]

    try:
        response = await client.chat(messages, session, temperature=0.1)
        clean = re.sub(r'```(?:json)?|```', '', response).strip()
        # 移除控制字符（除了换行和制表符）
        clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', clean)
        logger.debug(f"翻译API响应: {clean[:1000]}")
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
    """修复漏译条目，返回 (修复成功数, 漏译总数)。

    每条仍然使用独立精确上下文，但并发执行，避免大量漏译时完全串行。
    此阶段在首轮翻译全部结束后运行，所以 tgt 上下文已经冻结。
    """
    input_lang_code = LANGUAGE_MAP.get(input_lang, ("en", input_lang))[0]
    incomplete: list[SubtitleEntry] = []
    for entry in entries:
        source_text = _get_source_text(entry, input_lang_code)
        target_text = _get_target_text(entry, input_lang_code)
        if source_text and not _translation_is_usable(
            target_text, source_text, output_lang
        ):
            incomplete.append(entry)
    if not incomplete:
        return 0, 0

    logger.warning(
        "Segment translation coverage check found %s incomplete entries: %s",
        len(incomplete),
        [entry.index for entry in incomplete],
    )

    for entry in incomplete:
        source_text = _get_source_text(entry, input_lang_code)
        logger.warning(
            "  [ID:%d] 漏译检测: 原文=%d词, 译文=%d字, 原译文='%s'",
            entry.index,
            _english_word_count(source_text),
            _chinese_char_count(_get_target_text(entry, input_lang_code)),
            _get_target_text(entry, input_lang_code),
        )

    context_size = 3
    index_pos = {entry.index: pos for pos, entry in enumerate(entries)}
    sem = asyncio.Semaphore(4)

    async def repair_one(entry: SubtitleEntry) -> tuple[int, str]:
        pos = index_pos[entry.index]
        prev_ctx = entries[max(0, pos - context_size):pos]
        next_ctx = entries[pos + 1:min(len(entries), pos + 1 + context_size)]
        async with sem:
            result = await _translate_batch_impl(
                [entry], context, glossary, client, session,
                input_lang, output_lang, prev_ctx, next_ctx,
                include_context_targets=True,
            )
        return entry.index, result.get(entry.index, "")

    repair_results = await asyncio.gather(
        *(repair_one(entry) for entry in incomplete),
        return_exceptions=True,
    )
    retry_map: dict[int, str] = {}
    for item in repair_results:
        if isinstance(item, Exception):
            logger.warning("漏译修复请求失败: %s", item)
            continue
        entry_id, target = item
        retry_map[entry_id] = target

    repaired_count = 0
    for entry in incomplete:
        target = strip_annotation(
            str(retry_map.get(entry.index, "")).strip()
        ).replace("\n", " ")
        source_text = _get_source_text(entry, input_lang_code)
        if not _translation_is_usable(target, source_text, output_lang):
            logger.warning(
                "  [ID:%d] 修复失败: API返回译文仍不可用: '%s'",
                entry.index, target,
            )
            entry.status = "needs_review"
            entry.stage_status = "translation_repair_failed"
            entry.error_msg = "translation_repair_failed"
            if "translation_repair_failed" not in entry.quality_flags:
                entry.quality_flags.append("translation_repair_failed")
            continue

        old_target = _get_target_text(entry, input_lang_code)
        entry.lines = [target, source_text] if source_text else [target]
        entry.status = "done"
        entry.stage_status = "translated_repaired"
        entry.error_msg = ""
        for flag in (
            "source_fallback",
            "translation_batch_failed",
            "translation_hard_gate_failed",
            "translation_repair_failed",
        ):
            if flag in entry.quality_flags:
                entry.quality_flags.remove(flag)
        repaired_count += 1
        logger.warning(
            "  [ID:%d] 已修复: '%s' → '%s'",
            entry.index, old_target, target,
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
    if output_lang_code == "zh":
        # Chinese-target hard gate: a non-empty English source cannot be accepted
        # as translated unless the target line contains Chinese characters and
        # passes the conservative coverage check.
        if source_text.strip() and _english_word_count(source_text) > 0 and _chinese_char_count(translation) == 0:
            return False
        if _translation_looks_incomplete(translation, source_text):
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
    next_ctx: list[SubtitleEntry] | None = None,
    include_context_targets: bool = True,
) -> tuple[dict[int, str], list[int]]:
    input_lang_code = LANGUAGE_MAP.get(input_lang, ("en", input_lang))[0]
    first_map = await _translate_batch_impl(
        batch, context, glossary, client, session, input_lang, output_lang, prev_ctx, next_ctx,
        include_context_targets=include_context_targets,
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
            missing_entries, context, glossary, client, session, input_lang, output_lang, prev_ctx, next_ctx,
            include_context_targets=include_context_targets,
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
            result.source_cues = list(e.source_cues)
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
            merged.source_cues.extend(e.source_cues)
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
            fallback.original_lines = list(e.original_lines or e.lines)
            fallback.source_cues = list(e.source_cues)
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
            seg_entry.source_cues = list(orig.source_cues)
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


class BatchGlossaryWorker(BaseWorker):
    """批量正式处理前：逐文件并发提取可审核的词汇候选。"""

    finished = pyqtSignal(object)  # BatchGlossarySummary
    log = pyqtSignal(str)

    def __init__(
        self,
        client: MistralClient,
        files: list[str],
        concurrency: int,
        fixed_glossary: str,
        original_line: int = 1,
        transcription_audio_format: str = "mp3",
        input_lang: str = "English",
                 output_lang: str = "中文",
                 manual_review: bool = False,
                 source_layout: str = LAYOUT_AUTO,
    ):
        super().__init__()
        self.client = client
        self.files = files
        self.concurrency = max(1, concurrency)
        self.fixed_glossary = fixed_glossary
        self.original_line = original_line
        self.transcription_audio_format = transcription_audio_format
        self.input_lang = input_lang
        self.output_lang = output_lang
        self.manual_review = manual_review
        self.source_layout = source_layout
        self._ai_semaphore = asyncio.Semaphore(self.concurrency)

    def _extract_original_line(self, entries: list[SubtitleEntry]) -> list[SubtitleEntry]:
        input_code = LANGUAGE_MAP.get(self.input_lang, ("en", self.input_lang))[0]
        output_code = LANGUAGE_MAP.get(self.output_lang, ("zh", self.output_lang))[0]
        normalized = normalize_source_entries(
            entries,
            source_line_index=self.original_line,
            input_lang_code=input_code,
            output_lang_code=output_code,
            requested_layout=self.source_layout,
        )
        for warning in normalized.warnings:
            self.log.emit(f"  ⚠️ {warning}")
        return normalized.entries

    def _prepare_file_entries(self, filepath: str) -> list[SubtitleEntry]:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            content = f.read()
        entries = parse_srt(content)
        if not entries:
            raise ValueError("无法解析字幕文件")
        return self._extract_original_line(entries)

    def _sidecar_srt_for_media(self, filepath: str) -> str | None:
        sidecar = Path(filepath).with_suffix(".srt")
        if sidecar.exists() and sidecar.is_file():
            return str(sidecar)
        return None

    def _build_global_unifier_messages(self, summary: BatchGlossarySummary) -> list[dict]:
        candidate_limit = batch_glossary_unifier_candidate_limit()
        selected_candidates = summary.candidates[:candidate_limit]
        candidates = [
            {
                "source": candidate.source,
                "target": candidate.target,
                "file_count": len(candidate.files),
                "status": candidate.status,
            }
            for candidate in selected_candidates
        ]
        system_prompt = (
            "你是批量术语统一器。你的任务不是保留所有词汇，而是为了后续整批字幕翻译的一致性，"
            "把候选拆成“本次批量临时术语”和“建议永久保存的固定术语”。\n"
            "必须严格返回 JSON，不要返回解释、Markdown 或代码块。格式：\n"
            "{\n"
            "  \"session_glossary\": [{\"source\":\"本次批量可统一的原文\", \"target\":\"本次批量统一译文\"}],\n"
            "  \"fixed_candidates\": [{\"source\":\"需要人工裁决的原文\", \"target\":\"候选译文\", \"status\":\"译法冲突/低置信/语境不符\"}]\n"
            "}\n\n"
            "规则：\n"
            "1. session_glossary 用于当前这批文件，可以包含当前课程/当前资料语境下需要统一的表达，但不要超过 80 条。\n"
            "2. fixed_candidates 不是术语仓库，只放需要用户决定的问题项：多文件译法冲突、低置信、明显不符合当前语境。\n"
            "3. 如果某个术语译法稳定且看起来正确，只放入 session_glossary，不要放入 fixed_candidates 打扰用户。\n"
            "4. 普通常用词、泛用动词/名词、单个资料语境才成立的译法，绝不能进入 fixed_candidates。\n"
            "5. fixed_candidates 的 status 必须写具体原因，只能是“译法冲突”“低置信”或“语境不符”之一，不要写“新词”。\n"
            "6. fixed_candidates 最多返回 20 条；如果没有需要人工裁决的问题项，返回空列表。"
        )
        user_prompt = (
            f"输入语言：{self.input_lang}\n"
            f"输出语言：{self.output_lang}\n"
            f"本次只提供本地排序后的前 {len(selected_candidates)} 条候选；总候选数 {len(summary.candidates)}。\n"
            "候选词如下：\n"
            + json.dumps(candidates, ensure_ascii=False)
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _parse_global_unifier_result(
        self,
        raw: str,
        original_summary: BatchGlossarySummary,
    ) -> BatchGlossarySummary:
        clean = re.sub(r'```(?:json)?|```', '', raw or '').strip()
        data = json.loads(clean)
        has_session_glossary = isinstance(data, dict) and "session_glossary" in data
        session_items = data.get("session_glossary") if isinstance(data, dict) else None
        fixed_items = data.get("fixed_candidates") if isinstance(data, dict) else None
        if fixed_items is None and isinstance(data, dict):
            fixed_items = data.get("candidates")
        if not isinstance(session_items, list):
            session_items = []
        if not isinstance(fixed_items, list):
            raise ValueError("全局术语统一器返回格式无效")

        original_files_by_source: dict[str, list[str]] = {}
        for candidate in original_summary.candidates:
            key = normalize_glossary_source_key(candidate.source)
            files = original_files_by_source.setdefault(key, [])
            for filename in candidate.files:
                if filename not in files:
                    files.append(filename)

        session_lines: list[str] = []
        seen_session_sources: set[str] = set()
        for item in session_items[:80]:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source", "")).strip()
            target = str(item.get("target", "")).strip()
            source_key = normalize_glossary_source_key(source)
            if not source or not target or source_key in seen_session_sources:
                continue
            seen_session_sources.add(source_key)
            session_lines.append(f"{source}->{target}")

        candidates: list[GlossaryCandidate] = []
        seen_pairs: set[tuple[str, str]] = set()
        for item in fixed_items[:20]:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source", "")).strip()
            target = str(item.get("target", "")).strip()
            status = str(item.get("status", "新词")).strip() or "新词"
            if not source or not target or is_low_value_glossary_source(source):
                continue
            pair_key = (normalize_glossary_source_key(source), target.casefold())
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            files = tuple(original_files_by_source.get(normalize_glossary_source_key(source), []))
            candidates.append(GlossaryCandidate(source, target, files, status))

        review_candidates = filter_human_review_glossary_candidates(candidates)

        return BatchGlossarySummary(
            candidates=review_candidates,
            skipped_existing=original_summary.skipped_existing,
            failures=original_summary.failures,
            session_glossary="\n".join(session_lines) if has_session_glossary else original_summary.session_glossary,
        )

    REVIEW_SESSION_LIMIT = 30
    REVIEW_CANDIDATES_LIMIT = 10

    def _build_ai_review_messages(
        self,
        summary: BatchGlossarySummary,
        session_limit: int | None = None,
        candidates_limit: int | None = None,
    ) -> list[dict]:
        session_limit = (
            self.REVIEW_SESSION_LIMIT
            if session_limit is None
            else max(0, int(session_limit))
        )
        candidates_limit = (
            self.REVIEW_CANDIDATES_LIMIT
            if candidates_limit is None
            else max(0, int(candidates_limit))
        )
        session_pairs = [
            {"source": source, "target": target}
            for source, target in parse_glossary_pairs(summary.session_glossary)
        ][:session_limit]
        review_candidates = [
            {
                "source": candidate.source,
                "target": candidate.target,
                "file_count": len(candidate.files),
                "status": candidate.status,
            }
            for candidate in summary.candidates[:candidates_limit]
        ]
        fixed_context = relevant_fixed_glossary(
            self.fixed_glossary,
            [source for source, _target in session_pairs]
            + [candidate["source"] for candidate in review_candidates],
        )
        system_prompt = (
            "你是批量术语自审器。你要在不打扰用户的前提下，审查批量术语统一结果。"
            "只保留本批次真正需要统一的术语；普通词、语境短语、已经正确且无争议的项不要进入固定词汇。"
            "必须严格返回 JSON，不要返回解释、Markdown 或代码块。格式："
            "{\"session_glossary\":[{\"source\":\"term\",\"target\":\"translation\"}],"
            "\"fixed_candidates\":[{\"source\":\"term\",\"target\":\"translation\",\"reason\":\"stable cross-context term\"}],"
            "\"dropped\":[{\"source\":\"term\",\"reason\":\"common word/context-only/already correct\"}]}"
        )
        user_prompt = (
            f"输入语言：{self.input_lang}\n"
            f"输出语言：{self.output_lang}\n"
            f"固定词汇表与本批相关内容：\n{fixed_context or '（无相关项）'}\n\n"
            "本次批量临时术语：\n"
            + json.dumps(session_pairs, ensure_ascii=False)
            + "\n\n需要自审的冲突/可疑项：\n"
            + json.dumps(review_candidates, ensure_ascii=False)
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _parse_ai_review_result(
        self,
        raw: str,
        original_summary: BatchGlossarySummary,
    ) -> BatchGlossarySummary:
        return parse_batch_glossary_ai_review_result(
            raw,
            original_summary,
            fixed_glossary=self.fixed_glossary,
        )

    async def _review_candidates_with_ai(self, summary: BatchGlossarySummary) -> BatchGlossarySummary:
        if not summary.session_glossary and not summary.candidates:
            return summary

        session_count = len(parse_glossary_pairs(summary.session_glossary))
        candidates_count = len(summary.candidates)

        # 判断是否需要分批
        if session_count <= self.REVIEW_SESSION_LIMIT and candidates_count <= self.REVIEW_CANDIDATES_LIMIT:
            # AI 自审是可选的二次清理。网络或解析失败时保留统一器原始结果，
            # 不能阻断正式批量翻译，也不能把未经审核的候选写入固定词汇表。
            try:
                return await self._review_single_batch(summary)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.emit(
                    "  ⚠ AI 术语自审暂时不可用，已保留统一器结果并继续批量处理："
                    f"{exc}"
                )
                return summary

        # 需要分批：candidates 按 source 分组，session 按数量切分
        self.log.emit(
            f"  AI 自审分批：session {session_count} 条（上限 {self.REVIEW_SESSION_LIMIT}），"
            f"candidates {candidates_count} 条（上限 {self.REVIEW_CANDIDATES_LIMIT}）"
        )

        # 构建分批
        # candidates 按 source 分组
        from collections import OrderedDict
        groups: OrderedDict[str, list[GlossaryCandidate]] = OrderedDict()
        for c in summary.candidates:
            key = normalize_glossary_source_key(c.source)
            groups.setdefault(key, []).append(c)

        # 装箱
        candidate_batches: list[list[GlossaryCandidate]] = []
        current_batch: list[GlossaryCandidate] = []
        for group in groups.values():
            if len(current_batch) + len(group) > self.REVIEW_CANDIDATES_LIMIT and current_batch:
                candidate_batches.append(current_batch)
                current_batch = []
            current_batch.extend(group)
        if current_batch:
            candidate_batches.append(current_batch)

        # session 切分
        session_lines = parse_glossary_pairs(summary.session_glossary)
        session_batches = [
            session_lines[i:i + self.REVIEW_SESSION_LIMIT]
            for i in range(0, len(session_lines), self.REVIEW_SESSION_LIMIT)
        ]

        # 取最大批次数
        num_batches = max(len(candidate_batches), len(session_batches))
        if not candidate_batches:
            candidate_batches = [[]]
        if not session_batches:
            session_batches = [[]]

        # 补齐
        while len(candidate_batches) < num_batches:
            candidate_batches.append([])
        while len(session_batches) < num_batches:
            session_batches.append([])

        self.log.emit(f"  AI 自审并发：{num_batches} 批")

        async def process_review_batch(batch_idx: int, session_pairs: list, candidates: list):
            batch_summary = BatchGlossarySummary(
                candidates=candidates,
                skipped_existing=summary.skipped_existing,
                failures=[],
                session_glossary="\n".join(f"{s}->{t}" for s, t in session_pairs),
            )
            try:
                result = await self._review_single_batch(
                    batch_summary,
                    session_limit=len(session_pairs),
                    candidates_limit=len(candidates),
                )
                return (batch_idx, result, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.emit(f"  ⚠ 自审第 {batch_idx} 批失败：{exc}")
                # 失败批次必须以原始形式进入合并，避免临时术语和待确认项丢失。
                return (batch_idx, batch_summary, exc)

        results = await asyncio.gather(*[
            process_review_batch(i + 1, session_batches[i], candidate_batches[i])
            for i in range(num_batches)
        ])

        # 合并结果
        all_session_lines: list[str] = []
        all_fixed_lines: list[str] = []
        all_candidates: list[GlossaryCandidate] = []
        seen_session_sources: set[str] = set()
        seen_fixed_sources: set[str] = set()
        seen_candidate_pairs: set[tuple[str, str]] = set()
        failed_batches = 0
        for batch_idx, result, exc in results:
            if exc is not None:
                failed_batches += 1
            if result is None:
                continue

            for source, target in parse_glossary_pairs(result.session_glossary):
                source_key = normalize_glossary_source_key(source)
                if source_key in seen_session_sources:
                    continue
                seen_session_sources.add(source_key)
                all_session_lines.append(f"{source}->{target}")

            for source, target in parse_glossary_pairs(result.fixed_glossary):
                source_key = normalize_glossary_source_key(source)
                if source_key in seen_fixed_sources:
                    continue
                seen_fixed_sources.add(source_key)
                all_fixed_lines.append(f"{source}->{target}")

            for candidate in result.candidates:
                pair_key = (
                    normalize_glossary_source_key(candidate.source),
                    candidate.target.casefold(),
                )
                if pair_key in seen_candidate_pairs:
                    continue
                seen_candidate_pairs.add(pair_key)
                all_candidates.append(candidate)

        fixed_count = len([l for l in all_fixed_lines if l.strip()])
        self.log.emit(
            f"  AI 自审并发完成：失败 {failed_batches} 批，"
            f"保留 session {len(all_session_lines)} 条、"
            f"未审核 candidates {len(all_candidates)} 条、固定术语 {fixed_count} 条"
        )
        return BatchGlossarySummary(
            candidates=all_candidates,
            skipped_existing=summary.skipped_existing,
            failures=summary.failures,
            session_glossary="\n".join(all_session_lines),
            fixed_glossary="\n".join(all_fixed_lines),
        )

    async def _review_single_batch(
        self,
        summary: BatchGlossarySummary,
        session_limit: int | None = None,
        candidates_limit: int | None = None,
    ) -> BatchGlossarySummary:
        """处理单批自审"""
        timeout_seconds = batch_glossary_unifier_timeout_seconds()
        effective_session_limit = (
            self.REVIEW_SESSION_LIMIT
            if session_limit is None
            else max(0, int(session_limit))
        )
        effective_candidates_limit = (
            self.REVIEW_CANDIDATES_LIMIT
            if candidates_limit is None
            else max(0, int(candidates_limit))
        )
        actual_session_count = min(
            len(parse_glossary_pairs(summary.session_glossary)),
            effective_session_limit,
        )
        actual_candidates_count = min(
            len(summary.candidates),
            effective_candidates_limit,
        )
        messages = self._build_ai_review_messages(
            summary,
            effective_session_limit,
            effective_candidates_limit,
        )
        self.log.emit(
            f"    自审单批：session {actual_session_count} + "
            f"candidates {actual_candidates_count}"
        )
        async with self._ai_semaphore:
            async with aiohttp.ClientSession() as session:
                try:
                    raw = await asyncio.wait_for(
                        self.client.chat(
                            messages,
                            session,
                            extra_payload=batch_glossary_review_reasoning_options(),
                        ),
                        timeout=timeout_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except TypeError:
                    if getattr(self.client, "is_local_proxy", False):
                        raise
                    raw = await asyncio.wait_for(
                        self.client.chat(messages, session),
                        timeout=timeout_seconds,
                    )
        return self._parse_ai_review_result(raw, summary)

    async def _unify_candidates_globally(self, summary: BatchGlossarySummary) -> BatchGlossarySummary:
        if not summary.candidates:
            return summary

        candidate_limit = batch_glossary_unifier_candidate_limit()
        all_candidates = summary.candidates
        total = len(all_candidates)

        from collections import OrderedDict

        groups: OrderedDict[str, list[GlossaryCandidate]] = OrderedDict()
        for c in all_candidates:
            key = normalize_glossary_source_key(c.source)
            groups.setdefault(key, []).append(c)

        batches: list[list[GlossaryCandidate]] = []
        current_batch: list[GlossaryCandidate] = []
        for group in groups.values():
            if len(current_batch) + len(group) > candidate_limit and current_batch:
                batches.append(current_batch)
                current_batch = []
            current_batch.extend(group)
        if current_batch:
            batches.append(current_batch)
        num_batches = len(batches)

        self.log.emit(
            f"  全局术语统一器：共 {total} 条候选，按 source 分组后 "
            f"{len(groups)} 组，分 {num_batches} 批（每批最多 {candidate_limit} 条）"
        )

        def split_batch_by_source(
            batch_candidates: list[GlossaryCandidate],
        ) -> tuple[list[GlossaryCandidate], list[GlossaryCandidate]] | None:
            source_groups: list[list[GlossaryCandidate]] = []
            for candidate in batch_candidates:
                source_key = normalize_glossary_source_key(candidate.source)
                if (
                    source_groups
                    and normalize_glossary_source_key(source_groups[-1][0].source)
                    == source_key
                ):
                    source_groups[-1].append(candidate)
                else:
                    source_groups.append([candidate])

            # 同一 source 的冲突译法必须一起交给模型，不能为了重试而拆散。
            if len(source_groups) < 2:
                return None

            target_size = len(batch_candidates) / 2
            left: list[GlossaryCandidate] = []
            split_index = 0
            for split_index, group in enumerate(source_groups, start=1):
                if left and len(left) + len(group) > target_size:
                    split_index -= 1
                    break
                left.extend(group)
            else:
                split_index = len(source_groups)

            right = [
                candidate
                for group in source_groups[split_index:]
                for candidate in group
            ]
            if not left or not right:
                midpoint = max(1, len(source_groups) // 2)
                left = [
                    candidate
                    for group in source_groups[:midpoint]
                    for candidate in group
                ]
                right = [
                    candidate
                    for group in source_groups[midpoint:]
                    for candidate in group
                ]
            return (left, right) if left and right else None

        def should_split_after_failure(error: Exception) -> bool:
            if isinstance(error, (asyncio.TimeoutError, json.JSONDecodeError, ValueError)):
                return True
            message = str(error).casefold()
            return any(
                marker in message
                for marker in (
                    "api错误 500",
                    "api错误 502",
                    "api错误 503",
                    "api错误 504",
                    "all retries failed",
                    "proxyrequestbudgetexceeded",
                    "readtimeout",
                    "timed out",
                    "timeout",
                    "返回格式无效",
                    "响应结构异常",
                    "返回空内容",
                )
            )

        def merge_results(
            results: list[BatchGlossarySummary],
            *,
            include_original_failures: bool,
        ) -> BatchGlossarySummary:
            all_session_lines: list[str] = []
            all_fixed_candidates: list[GlossaryCandidate] = []
            all_failures = list(summary.failures) if include_original_failures else []

            for result in results:
                if result.session_glossary:
                    all_session_lines.extend(result.session_glossary.splitlines())
                all_fixed_candidates.extend(result.candidates)
                all_failures.extend(result.failures)

            seen_session_keys: set[str] = set()
            deduped_session_lines: list[str] = []
            for line in all_session_lines:
                line = line.strip()
                if not line:
                    continue
                source = line.split("->", 1)[0] if "->" in line else line
                key = normalize_glossary_source_key(source)
                if key and key not in seen_session_keys:
                    seen_session_keys.add(key)
                    deduped_session_lines.append(line)

            seen_fixed_keys: set[tuple[str, str]] = set()
            deduped_fixed: list[GlossaryCandidate] = []
            for candidate in all_fixed_candidates:
                pair_key = (
                    normalize_glossary_source_key(candidate.source),
                    candidate.target.casefold(),
                )
                if pair_key not in seen_fixed_keys:
                    seen_fixed_keys.add(pair_key)
                    deduped_fixed.append(candidate)

            return BatchGlossarySummary(
                candidates=deduped_fixed,
                skipped_existing=summary.skipped_existing,
                failures=all_failures,
                session_glossary="\n".join(deduped_session_lines),
            )

        failed_leaf_batches = 0
        split_retries = 0

        async def process_batch(
            batch_label: str,
            batch_candidates: list[GlossaryCandidate],
            split_depth: int = 0,
        ) -> BatchGlossarySummary:
            nonlocal failed_leaf_batches, split_retries
            self.log.emit(
                f"  ▶ 启动第 {batch_label} 批（{len(batch_candidates)} 条）"
            )
            batch_summary = BatchGlossarySummary(
                candidates=batch_candidates,
                skipped_existing=summary.skipped_existing,
                failures=[],
                session_glossary="",
            )
            try:
                return await self._unify_single_batch(
                    batch_summary,
                    batch_candidates,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                split = (
                    split_batch_by_source(batch_candidates)
                    if split_depth < 1 and should_split_after_failure(exc)
                    else None
                )
                if split is not None:
                    split_retries += 1
                    left, right = split
                    self.log.emit(
                        f"  ⚠ 第 {batch_label} 批失败，自动拆分为 "
                        f"{len(left)} + {len(right)} 条重试：{exc}"
                    )
                    child_results = await asyncio.gather(
                        process_batch(f"{batch_label}.1", left, split_depth + 1),
                        process_batch(f"{batch_label}.2", right, split_depth + 1),
                    )
                    return merge_results(
                        list(child_results),
                        include_original_failures=False,
                    )

                failed_leaf_batches += 1
                self.log.emit(
                    f"  ⚠ 第 {batch_label} 批统一失败，已保留本地需确认候选并继续批量处理：{exc}"
                )
                return BatchGlossarySummary(
                    candidates=filter_human_review_glossary_candidates(
                        batch_candidates
                    ),
                    skipped_existing=summary.skipped_existing,
                    # 统一器失败不是文件解析失败，不能混入 failures；UI 会
                    # 用文件失败数判断是否停止整批任务。详细原因已写入日志。
                    failures=[],
                    session_glossary="",
                )

        results = await asyncio.gather(*(
            process_batch(str(index), batch)
            for index, batch in enumerate(batches, start=1)
        ))
        unified = merge_results(
            list(results),
            include_original_failures=True,
        )

        self.log.emit(
            f"  全局术语统一器完成：{total} 条 → session "
            f"{len(unified.session_glossary.splitlines()) if unified.session_glossary else 0} 条，"
            f"需确认 {len(unified.candidates)} 条，拆分重试 {split_retries} 次，"
            f"最终降级 {failed_leaf_batches} 批"
        )
        return unified

    async def _unify_single_batch(
        self,
        summary: BatchGlossarySummary,
        batch_candidates: list[GlossaryCandidate],
    ) -> BatchGlossarySummary:
        """处理单批候选的统一逻辑"""
        messages = self._build_global_unifier_messages(summary)
        # DEBUG: 保存请求内容供调试
        try:
            from pathlib import Path
            debug_dir = Path(__file__).resolve().parent.parent / "logs" / "debug_requests"
            debug_dir.mkdir(parents=True, exist_ok=True)
            batch_idx = len(list(debug_dir.glob("unifier_request_batch_*.json"))) + 1
            debug_path = debug_dir / f"unifier_request_batch_{batch_idx}.json"
            debug_path.write_text(
                json.dumps(messages, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.log.emit(f"    [DEBUG] 请求内容已保存到 {debug_path}")
        except Exception as dbg_exc:
            self.log.emit(f"    [DEBUG] 保存请求内容失败: {dbg_exc}")
        timeout_seconds = batch_glossary_unifier_timeout_seconds()
        reasoning_options = batch_glossary_reasoning_options()
        self.log.emit(
            f"    候选 {len(batch_candidates)} 条，"
            f"reasoning_effort={reasoning_options.get('reasoning_effort', 'none')}，"
            f"超时 {timeout_seconds} 秒"
        )
        try:
            async with self._ai_semaphore:
                async with aiohttp.ClientSession() as session:
                    try:
                        raw = await asyncio.wait_for(
                            self.client.chat(
                                messages,
                                session,
                                extra_payload=reasoning_options,
                            ),
                            timeout=timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        self.log.emit("    单批统一器超时")
                        raise
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if getattr(self.client, "is_local_proxy", False):
                            raise
                        self.log.emit(
                            "    远端统一器思考模式不可用，改用普通请求："
                            f"{exc}"
                        )
                        raw = await asyncio.wait_for(
                            self.client.chat(messages, session),
                            timeout=timeout_seconds,
                        )
            unified = self._parse_global_unifier_result(raw, summary)
            self.log.emit(
                f"    单批结果：{len(batch_candidates)} 条 → session {len(unified.session_glossary.splitlines()) if unified.session_glossary else 0} 条，需确认 {len(unified.candidates)} 条"
            )
            return unified
        except Exception as exc:
            self.log.emit(f"    单批统一器失败：{exc}")
            raise

    @pyqtSlot()
    def run(self):
        try:
            from core.pipeline import SubtitlePipeline

            pipeline = SubtitlePipeline(
                self.client,
                self.input_lang,
                self.output_lang,
                self.concurrency,
                batch_size=5,
            )
            summary = _run_loop(self._analyze_files(pipeline))
            if not self._cancelled:
                self.finished.emit(summary)
        except Exception as exc:
            self.error.emit(str(exc))

    async def _analyze_files(self, pipeline) -> BatchGlossarySummary:
        semaphore = asyncio.Semaphore(self.concurrency)
        file_glossaries: list[tuple[str, str]] = []
        failures: list[GlossaryFileFailure] = []

        async def analyze_one(filepath: str):
            filename = Path(filepath).name
            async with semaphore:
                if self._cancelled:
                    return
                try:
                    working_path = filepath
                    if is_media_file(filepath):
                        sidecar = self._sidecar_srt_for_media(filepath)
                        if not sidecar:
                            reason = "未找到同名 SRT，已跳过词汇预分析"
                            self.log.emit(f"  [{filename}] {reason}")
                            failures.append(GlossaryFileFailure(filename, reason))
                            return
                        self.log.emit(f"  [{filename}] 使用同名字幕 {Path(sidecar).name} 提取词汇候选")
                        working_path = sidecar
                    entries = self._prepare_file_entries(working_path)
                    self.log.emit(f"  [{filename}] 提取词汇候选...")
                    analysis = await pipeline.analyze_details(
                        entries,
                        is_cancelled=lambda: self._cancelled,
                    )
                    if analysis.auto_glossary:
                        file_glossaries.append((filename, analysis.auto_glossary))
                except Exception as exc:
                    failures.append(GlossaryFileFailure(filename, str(exc)))

        await asyncio.gather(*(analyze_one(filepath) for filepath in self.files))
        summary = merge_batch_glossary_candidates(
            file_glossaries,
            fixed_glossary=self.fixed_glossary,
            failures=failures,
        )
        if self.client is None:
            self.log.emit("  未配置 LLM 客户端，保留本地合并的术语候选")
            return summary
        unified = await self._unify_candidates_globally(summary)
        if self.manual_review:
            self.log.emit("  手动审核术语已开启，跳过 AI 自审")
            return unified
        return await self._review_candidates_with_ai(unified)


class BatchWorker(BaseWorker):
    """批量处理：对每个文件独立执行「分析情景 → 智能重组 → 优化润色 → 导出」"""
    file_started = pyqtSignal(int, str)
    file_analysis_done = pyqtSignal(int, str, str)
    file_progress = pyqtSignal(int, int, int)
    file_done = pyqtSignal(int, str, str)
    file_skipped = pyqtSignal(int, str, str)
    file_error = pyqtSignal(int, str, str)
    all_done = pyqtSignal()
    log = pyqtSignal(str)

    def __init__(self, client: MistralClient, files: list[str],
                 out_dir: Optional[str], concurrency: int, batch_size: int,
                 overwrite: bool = False, target_first: bool = True,
                 glossary_prompt: str = "", raw_glossary: str = "",
                 original_line: int = 1,
                 transcription_audio_format: str = "mp3",
                 transcription_model: str = "",
                 language: str = None,
                 chunk_concurrency: int = 4,
                 transcribe_only: bool = False,
                 input_lang: str = "English", output_lang: str = "中文",
                 source_layout: str = LAYOUT_AUTO):
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
        self.transcription_audio_format = transcription_audio_format
        self.transcription_model = transcription_model
        self.language = language
        self.chunk_concurrency = chunk_concurrency
        self.transcribe_only = transcribe_only
        self.source_layout = source_layout

    def _extract_original_line(self, entries: list[SubtitleEntry]) -> list[SubtitleEntry]:
        """Normalize source cues through the same lossless path as single tasks."""
        output_code = LANGUAGE_MAP.get(self.output_lang, ("zh", self.output_lang))[0]
        normalized = normalize_source_entries(
            entries,
            source_line_index=self.original_line,
            input_lang_code=self.input_lang_code,
            output_lang_code=output_code,
            requested_layout=self.source_layout,
        )
        for warning in normalized.warnings:
            self.log.emit(f"  ⚠️ {warning}")
        self.log.emit(
            f"  📋 原文布局：{normalized.detected_layout} "
            f"(置信度 {normalized.confidence:.0%})"
        )
        return normalized.entries

    @pyqtSlot()
    def run(self):
        try:
            _run_loop(self._run())
        except Exception as e:
            self.log.emit(f"❌ 批量处理异常: {e}")
        finally:
            self.all_done.emit()

    async def _run(self):
        if self.transcribe_only:
            await self._run_transcribe_only()
            return

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
                working_path = filepath
                if is_media_file(filepath):
                    self.log.emit(f"  Transcribing media before subtitle optimization: {filename}")
                    try:
                        # 根据文件大小自动选择分段或普通转录
                        if should_use_chunked_transcription(filepath):
                            self.log.emit(f"  Large file, using chunked transcription")
                            working_path, _ = await transcribe_chunked_to_srt(
                                self.client,
                                filepath,
                                audio_format=self.transcription_audio_format,
                                on_log=lambda msg: self.log.emit(f"  {msg}"),
                            )
                        else:
                            working_path, _ = await transcribe_media_to_srt(
                                self.client,
                                filepath,
                                audio_format=self.transcription_audio_format,
                                on_log=lambda msg: self.log.emit(f"  {msg}"),
                            )
                    except RuntimeError as exc:
                        message = str(exc)
                        if message == NO_TRANSCRIBABLE_AUDIO_ERROR or "no audio track" in message.lower():
                            self.log.emit(f"  Skipping media: no transcribable audio detected")
                            self.file_skipped.emit(idx, filename, NO_TRANSCRIBABLE_AUDIO_ERROR)
                            continue
                        raise
                    self.log.emit(f"  Media transcription complete: {working_path}")

                with open(working_path, "r", encoding="utf-8-sig") as f:
                    content = f.read()
                entries = parse_srt(content)
                if not entries:
                    self.file_error.emit(idx, filename, "无法解析字幕文件")
                    continue

                # Single and batch tasks share the same lossless source-layout
                # normalization. Multi-line monolingual cues are joined instead
                # of being mistaken for bilingual subtitles.
                entries = self._extract_original_line(entries)

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

                stage_labels = {
                    "segment": "智能重组",
                    "translate": "AI翻译",
                    "optimize": "质量审校",
                }

                def on_stage(stage: str):
                    self.log.emit(
                        f"  ▶ [{filename}] {stage_labels.get(stage, stage)}"
                    )

                def on_snapshot(stage: str, stage_entries: list[SubtitleEntry], use_optimized: bool):
                    phase = {
                        "segment": "AFTER_SEGMENT",
                        "translate": "AFTER_TRANSLATE",
                        "optimize": "AFTER_OPTIMIZE",
                    }.get(stage, stage.upper())
                    write_phase_snapshot(
                        filename,
                        phase,
                        stage_entries,
                        use_optimized=use_optimized,
                    )

                run_result = await pipeline.execute(
                    entries,
                    context,
                    glossary,
                    segment_batch_size=max(10, min(30, self.batch_size)),
                    on_stage=on_stage,
                    on_progress=lambda _stage, c, t: self.file_progress.emit(idx, c, t),
                    on_snapshot=on_snapshot,
                    on_log=self.log.emit,
                    is_cancelled=lambda: self._cancelled,
                )
                entries = run_result.entries

                if not run_result.quality_report.passed:
                    previews = [
                        f"#{issue.entry_index or '?'} {issue.kind}"
                        for issue in run_result.quality_report.critical_issues[:12]
                    ]
                    raise RuntimeError(
                        "质量门禁未通过，已停止导出：" + ", ".join(previews)
                    )

                if self._cancelled:
                    break

                out_path = self._get_out_path(working_path)
                blocks = [e.to_srt_block(use_optimized=True, target_first=self.target_first) for e in entries]
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(blocks))
                self.file_done.emit(idx, filename, out_path)

            except Exception as e:
                self.file_error.emit(idx, filename, str(e))

    async def _run_transcribe_only(self):
        def is_no_audio_error(exc: Exception) -> bool:
            message = str(exc)
            return message == NO_TRANSCRIBABLE_AUDIO_ERROR or "no audio track" in message.lower()

        async def transcribe_one(idx: int, filepath: str, filename: str, audio_path: str, temp_audio: str | None):
            try:
                if self._cancelled:
                    return
                out_path = str(Path(filepath).with_suffix(".srt"))
                self.log.emit(f"  [{filename}] 开始转录 API → {out_path}")

                # 根据文件大小选择转录方式
                if should_use_chunked_transcription(audio_path):
                    self.log.emit(f"  [{filename}] 大文件，使用分段转录")
                    srt_path, _ = await transcribe_chunked_to_srt(
                        self.client,
                        audio_path,
                        output_path=out_path,
                        model=self.transcription_model or None,
                        language=self.language,
                        concurrency=self.chunk_concurrency,
                        on_log=lambda msg: self.log.emit(f"  {msg}"),
                    )
                else:
                    srt_path, _ = await transcribe_prepared_audio_to_srt(
                        self.client,
                        audio_path,
                        filepath,
                        output_path=out_path,
                        on_log=lambda msg: self.log.emit(f"  {msg}"),
                    )
                if not self._cancelled:
                    self.file_done.emit(idx, filename, srt_path)
            except RuntimeError as exc:
                if is_no_audio_error(exc):
                    self.log.emit(f"  [{filename}] 跳过：无可转录语音")
                    if not self._cancelled:
                        self.file_skipped.emit(idx, filename, NO_TRANSCRIBABLE_AUDIO_ERROR)
                    return
                if not self._cancelled:
                    self.file_error.emit(idx, filename, str(exc))
            except Exception as exc:
                if not self._cancelled:
                    self.file_error.emit(idx, filename, str(exc))
            finally:
                if temp_audio:
                    try:
                        Path(temp_audio).unlink()
                        self.log.emit(f"  Temporary audio removed: {temp_audio}")
                    except OSError:
                        pass

        for idx, filepath in enumerate(self.files):
            if self._cancelled:
                self.log.emit("⏹ 处理已取消")
                break
            filename = Path(filepath).name
            self.file_started.emit(idx, filename)
            if not is_media_file(filepath):
                self.file_skipped.emit(idx, filename, "仅转录模式只处理音频/视频文件")
                continue

            self.log.emit(f"📂 [{filename}] 准备仅转录...")
            try:
                audio_path, temp_audio = await asyncio.to_thread(
                    prepare_transcription_audio,
                    filepath,
                    audio_format=self.transcription_audio_format,
                    on_log=lambda msg: self.log.emit(f"  {msg}"),
                )
            except RuntimeError as exc:
                if is_no_audio_error(exc):
                    self.log.emit(f"  [{filename}] 跳过：无可转录语音")
                    self.file_skipped.emit(idx, filename, NO_TRANSCRIBABLE_AUDIO_ERROR)
                    continue
                self.file_error.emit(idx, filename, str(exc))
                continue
            except Exception as exc:
                self.file_error.emit(idx, filename, str(exc))
                continue

            if self._cancelled:
                if temp_audio:
                    try:
                        Path(temp_audio).unlink()
                    except OSError:
                        pass
                break

            self.log.emit(f"  [{filename}] 音频已准备，开始转录任务")
            await transcribe_one(idx, filepath, filename, audio_path, temp_audio)

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

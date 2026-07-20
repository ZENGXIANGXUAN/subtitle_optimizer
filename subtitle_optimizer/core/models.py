import re
from dataclasses import dataclass, field
from typing import Optional

# 尝试相对导入，如果失败则使用绝对导入
try:
    from ..utils import logger
except ImportError:
    import sys
    import os
    sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))
    from utils import logger

@dataclass(frozen=True)
class SourceCueSnapshot:
    """Immutable source cue retained while semantic units are translated.

    A semantic sentence can span several timestamp cues. Keeping the original
    cue text and timing here lets the final target text flow back to the source
    timeline without reconstructing metadata from mutable ``lines`` fields.
    """

    index: int
    start_time: str
    end_time: str
    source_text: str
    original_lines: tuple[str, ...] = ()
    source_indexes: tuple[int, ...] = ()


@dataclass
class SubtitleEntry:
    index: int
    start_time: str
    end_time: str
    lines: list[str]  # 原始文本行
    original_lines: Optional[list[str]] = None  # 加载时的原始行，用于对比视图
    optimized_lines: Optional[list[str]] = None
    status: str = "pending"  # pending / processing / done / error
    error_msg: str = ""
    source_indexes: Optional[list[int]] = None  # 来源原始条目ID列表，用于追踪完整性
    # 细粒度阶段状态；保留 status 字段兼容现有 UI。
    # translated / fidelity_checked / optimized / consistency_checked / *_fallback
    stage_status: str = "pending"
    quality_flags: list[str] = field(default_factory=list)
    source_cues: list[SourceCueSnapshot] = field(default_factory=list)

    @property
    def chinese(self) -> str:
        """获取中文文本（向后兼容）"""
        for line in self.lines:
            if any('一' <= c <= '鿿' for c in line):
                return line
        return ""

    @property
    def english(self) -> str:
        """获取英文文本（向后兼容）"""
        for line in self.lines:
            stripped = line.strip()
            # 跳过占位符行
            if stripped.lower() in ("none", "null", "—", "-", ""):
                continue
            if not any('一' <= c <= '鿿' for c in stripped):
                return stripped
        return ""

    @property
    def source(self) -> str:
        """获取原文文本（语言无关）- 别名，实际调用 _get_source_text()"""
        # 注意：这个属性需要知道语言代码才能正确判断
        # 建议直接使用 _get_source_text(entry, lang_code) 函数
        return self.english

    @property
    def target(self) -> str:
        """获取译文文本（语言无关）- 别名，实际调用 _get_target_text()"""
        # 注意：这个属性需要知道语言代码才能正确判断
        # 建议直接使用 _get_target_text(entry, lang_code) 函数
        return self.chinese

    def to_srt_block(self, use_optimized=False, target_first=True) -> str:
        selected = self.optimized_lines if (use_optimized and self.optimized_lines) else self.lines
        lines = list(selected)
        # The pipeline's language-independent canonical layout is always
        # [target, source]. Script detection breaks Chinese -> English and any
        # same-script language pair, so output ordering must use positions.
        if len(lines) >= 2 and not target_first:
            lines = lines[1:] + [lines[0]]
        text = "\n".join(lines)
        return f"{self.index}\n{self.start_time} --> {self.end_time}\n{text}\n"

    def to_srt_block_raw(self) -> str:
        """返回原始未清洗的字幕块（用于对比视图左侧）"""
        src = self.original_lines if self.original_lines is not None else self.lines
        text = "\n".join(src)
        return f"{self.index}\n{self.start_time} --> {self.end_time}\n{text}\n"

def parse_srt(content: str) -> list[SubtitleEntry]:
    logger.debug("开始解析SRT内容")
    entries = []
    blocks = re.split(r'\n\s*\n', content.strip())
    for i, block in enumerate(blocks):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        try:
            idx = int(lines[0].strip())
            time_match = re.match(
                r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})',
                lines[1].strip()
            )
            if not time_match:
                continue
            start, end = time_match.group(1), time_match.group(2)
            text_lines = [l for l in lines[2:] if l.strip()]
            entry = SubtitleEntry(idx, start, end, text_lines)
            entry.original_lines = list(text_lines)
            entry.source_indexes = [idx]
            entries.append(entry)
        except (ValueError, IndexError):
            logger.warning(f"解析第 {i+1} 个字幕块时出现错误，跳过该块")
            continue
    logger.info(f"SRT解析完成，共解析 {len(entries)} 个字幕条目")
    return entries

def strip_annotation(text: str) -> str:
    """清除 AI 在译文字段中留下的说明性注释。

    旧逻辑会删除所有被括号包住的内容，容易误删合法字幕，例如“（笑）”
    或角色台词。这里仅删除明确像 AI 注释/说明/无法翻译占位的整句。
    """
    s = text.strip()
    if not s:
        return s

    annotation_patterns = [
        r"^[（(【\[]\s*(?:注|注释|说明|备注|提示|译注|校注)\s*[:：].*?[）)】\]]?$",
        r"^[（(【\[]?\s*(?:无法翻译|未翻译|无译文|空字幕|无内容|原文缺失|内容缺失).*?[）)】\]]?$",
        r"^[（(【\[]?\s*(?:translation|translator|note|comment|annotation)\s*[:：].*?[）)】\]]?$",
    ]
    for pattern in annotation_patterns:
        if re.match(pattern, s, flags=re.IGNORECASE):
            return ""
    return s


def _format_subtitle_snapshot(title: str, entries: list[SubtitleEntry], use_optimized: bool = False) -> str:
    lines = [f"===== {title} ({len(entries)} entries) ====="]
    for entry in entries:
        text_lines = entry.optimized_lines if (use_optimized and entry.optimized_lines) else entry.lines
        lines.append(str(entry.index))
        lines.append(f"{entry.start_time} --> {entry.end_time}")
        lines.extend(text_lines)
        lines.append("")
    lines.append(f"===== END {title} =====")
    return "\n".join(lines)


def verify_source_integrity(original_entries: list, result_entries: list) -> set:
    """校验重组后来源完整性，返回丢失的原始条目ID集合。"""
    original_ids = {e.index for e in original_entries}
    all_sources = set()
    for e in result_entries:
        all_sources.update(e.source_indexes or [e.index])
    return original_ids - all_sources


def _srt_time_to_ms(value: str) -> int:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", str(value).strip())
    if not match:
        return 0
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _ms_to_srt_time(ms: int) -> str:
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _english_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)?", text or ""))


def _chinese_char_count(text: str) -> int:
    return sum(1 for char in text or "" if "一" <= char <= "鿿")


def _source_text_length(text: str, lang_code: str = "en") -> int:
    """获取原文文本长度（根据语言使用不同的计算方式）
    - 英文等拉丁语系：计算单词数
    - 中文、日文、韩文：计算字符数
    """
    if lang_code in ("zh", "ja", "ko"):
        # CJK 语言：计算字符数
        return len(text or "")
    else:
        # 其他语言：计算单词数
        return _english_word_count(text)


def _contains_chinese(text: str) -> bool:
    """Return True when text contains at least one CJK unified ideograph."""
    return any("一" <= char <= "鿿" for char in text or "")


def _latin_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)?", text or ""))


def _translation_looks_incomplete(zh: str, en: str) -> bool:
    """Conservative Chinese-target coverage check for English source subtitles.

    The old rule ignored every source shorter than 18 English words, which let
    short untranslated cues such as ``Hi guys, part one...`` pass through as
    completed.  For a Chinese target, a non-empty English source must produce a
    target line containing Chinese characters, no matter how short the cue is.
    The length-ratio check then catches obvious long-form shrinkage.
    """
    source = (en or "").strip()
    target = (zh or "").strip()
    if not source:
        return False
    if not target:
        return True

    en_words = _english_word_count(source)
    zh_chars = _chinese_char_count(target)

    # Hard gate: English source translated into Chinese cannot remain
    # English-only, even when the source cue is very short.
    if en_words > 0 and zh_chars == 0:
        return True

    # If the target is mostly copied Latin text with barely any Chinese, treat
    # it as suspicious. This catches source leakage such as ``Okay.`` or full
    # English lines being used as fallback.
    latin_words_in_target = _latin_word_count(target)
    if en_words >= 3 and zh_chars <= 1 and latin_words_in_target >= max(2, en_words // 2):
        return True

    # Short cues still need a small amount of Chinese. Keep this deliberately
    # lenient so single-word terms or fillers are not over-rejected.
    if en_words <= 3:
        min_chars = 1
    elif en_words < 18:
        min_chars = max(2, int(en_words * 0.35))
    else:
        min_chars = max(12, int(en_words * 0.45))
    return zh_chars < min_chars


def _merge_candidate_is_safe(batch: list[SubtitleEntry], item: dict) -> bool:
    merged_from = item.get("merged_from") or []
    if not isinstance(merged_from, list) or len(merged_from) <= 1:
        return True

    by_id = {entry.index: entry for entry in batch}
    sources = [by_id.get(src_id) for src_id in merged_from]
    if any(src is None for src in sources):
        return False

    positions = [batch.index(src) for src in sources if src is not None]
    if positions != list(range(min(positions), max(positions) + 1)):
        return False

    start_ms = _srt_time_to_ms(sources[0].start_time)
    end_ms = _srt_time_to_ms(sources[-1].end_time)
    if end_ms - start_ms > 8000:
        return False

    for left, right in zip(sources, sources[1:]):
        gap_ms = _srt_time_to_ms(right.start_time) - _srt_time_to_ms(left.end_time)
        if gap_ms > 1200:
            return False

    en = str(item.get("en", ""))
    if _english_word_count(en) > 42:
        return False

    # 如果任一来源条目本身已是长句（≥20词），不应参与合并
    for src in sources:
        src_en = src.english if src else ""
        if _english_word_count(src_en) >= 20:
            return False

    return True

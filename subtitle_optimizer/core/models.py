import re
from dataclasses import dataclass
from typing import Optional

# 尝试相对导入，如果失败则使用绝对导入
try:
    from ..utils import logger
except ImportError:
    import sys
    import os
    sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))
    from utils import logger

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

    @property
    def chinese(self) -> str:
        for line in self.lines:
            if any('\u4e00' <= c <= '\u9fff' for c in line):
                return line
        return ""

    @property
    def english(self) -> str:
        for line in self.lines:
            stripped = line.strip()
            # 跳过占位符行
            if stripped.lower() in ("none", "null", "—", "-", ""):
                continue
            if not any('\u4e00' <= c <= '\u9fff' for c in stripped):
                return stripped
        return ""

    def to_srt_block(self, use_optimized=False, chinese_first=True) -> str:
        lines = self.optimized_lines if (use_optimized and self.optimized_lines) else self.lines
        # 如果需要重新排列中英文顺序
        if len(lines) == 2:
            zh_line = next((l for l in lines if any('\u4e00' <= c <= '\u9fff' for c in l)), None)
            en_line = next((l for l in lines if not any('\u4e00' <= c <= '\u9fff' for c in l)), None)
            if zh_line and en_line:
                lines = [zh_line, en_line] if chinese_first else [en_line, zh_line]
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
            entries.append(entry)
        except (ValueError, IndexError):
            logger.warning(f"解析第 {i+1} 个字幕块时出现错误，跳过该块")
            continue
    logger.info(f"SRT解析完成，共解析 {len(entries)} 个字幕条目")
    return entries

def strip_annotation(text: str) -> str:
    """清除 AI 在 zh 字段中留下的注释标记"""
    s = text.strip()
    bracket_pairs = [('（', '）'), ('(', ')'), ('【', '】'), ('[', ']')]
    for open_b, close_b in bracket_pairs:
        if s.startswith(open_b) and s.endswith(close_b):
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


def _srt_time_to_ms(value: str) -> int:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", str(value).strip())
    if not match:
        return 0
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _english_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)?", text or ""))


def _chinese_char_count(text: str) -> int:
    return sum(1 for char in text or "" if "\u4e00" <= char <= "\u9fff")


def _translation_looks_incomplete(zh: str, en: str) -> bool:
    en_words = _english_word_count(en)
    if en_words < 18:
        return False
    zh_chars = _chinese_char_count(zh)
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

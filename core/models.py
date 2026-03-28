import re
from dataclasses import dataclass
from typing import Optional

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
    entries = []
    blocks = re.split(r'\n\s*\n', content.strip())
    for block in blocks:
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
            continue
    return entries

def strip_annotation(text: str) -> str:
    """清除 AI 在 zh 字段中留下的注释标记"""
    s = text.strip()
    bracket_pairs = [('（', '）'), ('(', ')'), ('【', '】'), ('[', ']')]
    for open_b, close_b in bracket_pairs:
        if s.startswith(open_b) and s.endswith(close_b):
            return ""
    return s
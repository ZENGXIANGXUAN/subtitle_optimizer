#!/usr/bin/env python3
"""字幕对比工具 — 对比两个 SRT 文件的差异。

用法:
    python subtitle_diff.py file1.srt file2.srt
    python subtitle_diff.py file1.srt file2.srt --html output.html
    python subtitle_diff.py file1.srt file2.srt --stats
"""

import re
import sys
import argparse
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SubtitleEntry:
    index: int
    start_time: str
    end_time: str
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def parse_srt(content: str) -> list[SubtitleEntry]:
    """解析 SRT 文件内容。"""
    entries = []
    # 跳过文件开头的统计行（如 ===== AFTER_SEGMENT (771 entries) =====）
    content = re.sub(r'^=====.*?=====\s*\n?', '', content.strip())
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
            entries.append(SubtitleEntry(idx, start, end, text_lines))
        except (ValueError, IndexError):
            continue
    return entries


@dataclass
class DiffResult:
    index: int
    time: str
    status: str  # "added", "removed", "modified", "unchanged"
    old_text: str
    new_text: str


def compute_diff(old_entries: list[SubtitleEntry], new_entries: list[SubtitleEntry]) -> list[DiffResult]:
    """计算两个字幕列表的差异。"""
    old_map = {e.index: e for e in old_entries}
    new_map = {e.index: e for e in new_entries}

    all_indices = sorted(set(old_map.keys()) | set(new_map.keys()))
    results = []

    for idx in all_indices:
        old = old_map.get(idx)
        new = new_map.get(idx)

        if old and not new:
            results.append(DiffResult(idx, f"{old.start_time} --> {old.end_time}", "removed", old.text, ""))
        elif new and not old:
            results.append(DiffResult(idx, f"{new.start_time} --> {new.end_time}", "added", "", new.text))
        elif old.text != new.text:
            results.append(DiffResult(idx, f"{new.start_time} --> {new.end_time}", "modified", old.text, new.text))
        else:
            results.append(DiffResult(idx, f"{new.start_time} --> {new.end_time}", "unchanged", old.text, new.text))

    return results


def print_terminal_diff(results: list[DiffResult], file1_name: str, file2_name: str):
    """在终端输出彩色差异。"""
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    RESET = "\033[0m"
    DIM = "\033[2m"

    print(f"\n{'='*60}")
    print(f"字幕对比: {file1_name} → {file2_name}")
    print(f"{'='*60}\n")

    stats = {"added": 0, "removed": 0, "modified": 0, "unchanged": 0}
    for r in results:
        stats[r.status] += 1

    print(f"统计: {GREEN}+{stats['added']} 新增{RESET} | "
          f"{RED}-{stats['removed']} 删除{RESET} | "
          f"{YELLOW}~{stats['modified']} 修改{RESET} | "
          f"{DIM}={stats['unchanged']} 未变{RESET}\n")

    for r in results:
        if r.status == "unchanged":
            continue

        if r.status == "added":
            print(f"{GREEN}[+] #{r.index} {r.time}{RESET}")
            for line in r.new_text.split("\n"):
                print(f"{GREEN}    + {line}{RESET}")
        elif r.status == "removed":
            print(f"{RED}[-] #{r.index} {r.time}{RESET}")
            for line in r.old_text.split("\n"):
                print(f"{RED}    - {line}{RESET}")
        elif r.status == "modified":
            print(f"{YELLOW}[~] #{r.index} {r.time}{RESET}")
            for line in r.old_text.split("\n"):
                print(f"{RED}    - {line}{RESET}")
            for line in r.new_text.split("\n"):
                print(f"{GREEN}    + {line}{RESET}")

        print()


def print_stats(results: list[DiffResult]):
    """输出详细统计信息。"""
    stats = {"added": 0, "removed": 0, "modified": 0, "unchanged": 0}
    for r in results:
        stats[r.status] += 1

    total = len(results)
    print(f"\n字幕条目统计:")
    print(f"  总计: {total}")
    print(f"  新增: {stats['added']} ({stats['added']/total*100:.1f}%)")
    print(f"  删除: {stats['removed']} ({stats['removed']/total*100:.1f}%)")
    print(f"  修改: {stats['modified']} ({stats['modified']/total*100:.1f}%)")
    print(f"  未变: {stats['unchanged']} ({stats['unchanged']/total*100:.1f}%)")

    # 字数变化统计
    old_chars = sum(len(r.old_text) for r in results if r.old_text)
    new_chars = sum(len(r.new_text) for r in results if r.new_text)
    print(f"\n字数变化:")
    print(f"  旧文本总字数: {old_chars}")
    print(f"  新文本总字数: {new_chars}")
    if old_chars > 0:
        change = (new_chars - old_chars) / old_chars * 100
        print(f"  变化: {change:+.1f}%")


def generate_html(results: list[DiffResult], file1_name: str, file2_name: str) -> str:
    """生成 HTML 差异报告。"""
    stats = {"added": 0, "removed": 0, "modified": 0, "unchanged": 0}
    for r in results:
        stats[r.status] += 1

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>字幕对比: {file1_name} → {file2_name}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 20px; background: #f5f5f5; }}
.container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
h1 {{ color: #333; border-bottom: 2px solid #eee; padding-bottom: 10px; }}
.stats {{ display: flex; gap: 20px; margin: 20px 0; padding: 15px; background: #f8f9fa; border-radius: 6px; }}
.stat {{ text-align: center; }}
.stat-value {{ font-size: 24px; font-weight: bold; }}
.stat-label {{ font-size: 12px; color: #666; }}
.added {{ color: #28a745; }}
.removed {{ color: #dc3545; }}
.modified {{ color: #ffc107; }}
.unchanged {{ color: #6c757d; }}
.diff-entry {{ margin: 10px 0; padding: 10px; border-radius: 4px; border-left: 4px solid; }}
.diff-added {{ background: #f0fff0; border-color: #28a745; }}
.diff-removed {{ background: #fff5f5; border-color: #dc3545; }}
.diff-modified {{ background: #fff8e1; border-color: #ffc107; }}
.diff-header {{ font-weight: bold; margin-bottom: 5px; }}
.diff-content {{ font-family: monospace; white-space: pre-wrap; }}
.diff-old {{ color: #dc3545; text-decoration: line-through; }}
.diff-new {{ color: #28a745; }}
.entry-index {{ color: #666; font-size: 12px; }}
</style>
</head>
<body>
<div class="container">
<h1>字幕对比报告</h1>
<p><strong>文件1:</strong> {file1_name}</p>
<p><strong>文件2:</strong> {file2_name}</p>

<div class="stats">
  <div class="stat"><div class="stat-value added">+{stats['added']}</div><div class="stat-label">新增</div></div>
  <div class="stat"><div class="stat-value removed">-{stats['removed']}</div><div class="stat-label">删除</div></div>
  <div class="stat"><div class="stat-value modified">~{stats['modified']}</div><div class="stat-label">修改</div></div>
  <div class="stat"><div class="stat-value unchanged">={stats['unchanged']}</div><div class="stat-label">未变</div></div>
</div>

<h2>差异详情</h2>
"""

    for r in results:
        if r.status == "unchanged":
            continue

        status_class = f"diff-{r.status}"
        status_label = {"added": "新增", "removed": "删除", "modified": "修改"}[r.status]

        html += f'<div class="diff-entry {status_class}">\n'
        html += f'  <div class="diff-header">#{r.index} <span class="entry-index">{r.time}</span> [{status_label}]</div>\n'
        html += '  <div class="diff-content">\n'

        if r.status == "removed":
            html += f'    <div class="diff-old">{r.old_text}</div>\n'
        elif r.status == "added":
            html += f'    <div class="diff-new">{r.new_text}</div>\n'
        elif r.status == "modified":
            html += f'    <div class="diff-old">{r.old_text}</div>\n'
            html += f'    <div class="diff-new">{r.new_text}</div>\n'

        html += '  </div>\n</div>\n'

    html += """
</div>
</body>
</html>"""

    return html


def main():
    parser = argparse.ArgumentParser(description="字幕对比工具")
    parser.add_argument("file1", help="第一个 SRT 文件")
    parser.add_argument("file2", help="第二个 SRT 文件")
    parser.add_argument("--html", help="输出 HTML 报告到指定文件")
    parser.add_argument("--stats", action="store_true", help="只显示统计信息")
    parser.add_argument("--no-color", action="store_true", help="禁用终端颜色")

    args = parser.parse_args()

    # 读取文件
    try:
        with open(args.file1, "r", encoding="utf-8") as f:
            content1 = f.read()
        with open(args.file2, "r", encoding="utf-8") as f:
            content2 = f.read()
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)

    # 解析 SRT
    entries1 = parse_srt(content1)
    entries2 = parse_srt(content2)

    if not entries1:
        print(f"警告: {args.file1} 解析结果为空", file=sys.stderr)
    if not entries2:
        print(f"警告: {args.file2} 解析结果为空", file=sys.stderr)

    # 计算差异
    results = compute_diff(entries1, entries2)

    # 输出
    if args.stats:
        print_stats(results)
    elif args.html:
        html = generate_html(results, Path(args.file1).name, Path(args.file2).name)
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"HTML 报告已生成: {args.html}")
    else:
        if args.no_color:
            # 简单文本输出
            for r in results:
                if r.status == "unchanged":
                    continue
                print(f"[{r.status}] #{r.index} {r.time}")
                if r.status == "removed":
                    print(f"  - {r.old_text}")
                elif r.status == "added":
                    print(f"  + {r.new_text}")
                elif r.status == "modified":
                    print(f"  - {r.old_text}")
                    print(f"  + {r.new_text}")
                print()
        else:
            print_terminal_diff(results, Path(args.file1).name, Path(args.file2).name)


if __name__ == "__main__":
    main()

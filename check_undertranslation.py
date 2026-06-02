"""
字幕漏译检查工具 — 检查 SRT 字幕中疑似漏译/半译的条目。

判定规则：英文词数 >= 阈值 且 中文字数 < 英文词数 × 比率阈值 时，判定为疑似漏译。

用法：
    python check_undertranslation.py              # 启动 GUI
    python check_undertranslation.py --cli ...    # 命令行模式
"""

import argparse
import re
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk


# ── 可调参数（GUI 中直接修改） ──

MIN_ENGLISH_WORDS = 18      # 英文至少多少个词才触发检查
ZH_EN_RATIO = 0.45          # 中文字数 / 英文词数 低于此值判为漏译
MIN_ZH_CHARS = 12           # 中文至少需要多少字（取此值与 ratio*en_words 的较大者）


# ═══════════════════════════════════════════════════════════════
# 核心逻辑
# ═══════════════════════════════════════════════════════════════

def parse_srt(content: str) -> list[dict]:
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

            zh, en = "", ""
            for line in text_lines:
                if any('一' <= c <= '鿿' for c in line):
                    zh = line.strip()
                else:
                    s = line.strip()
                    if s.lower() not in ("none", "null", "—", "-", ""):
                        en = s

            entries.append({"index": idx, "start": start, "end": end, "zh": zh, "en": en})
        except (ValueError, IndexError):
            continue
    return entries


def english_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)?", text or ""))


def chinese_char_count(text: str) -> int:
    return sum(1 for c in (text or "") if "一" <= c <= "鿿")


def check_entries(entries: list[dict], min_words: int, ratio: float, min_chars: int) -> list[dict]:
    """返回疑似漏译条目列表。"""
    incomplete = []
    for e in entries:
        en_words = english_word_count(e["en"])
        if en_words < min_words:
            continue
        zh_chars = chinese_char_count(e["zh"])
        threshold = max(min_chars, int(en_words * ratio))
        if zh_chars < threshold:
            e["en_words"] = en_words
            e["zh_chars"] = zh_chars
            e["ratio"] = zh_chars / en_words if en_words else 0
            incomplete.append(e)
    return incomplete


def check_file(filepath: Path, min_words: int, ratio: float, min_chars: int) -> tuple[list[dict], int]:
    try:
        content = filepath.read_text(encoding="utf-8-sig")
    except Exception as e:
        return [], 0
    entries = parse_srt(content)
    if not entries:
        return [], 0
    for e in entries:
        e["file"] = str(filepath)
    incomplete = check_entries(entries, min_words, ratio, min_chars)
    return incomplete, len(entries)


def format_report(results: list[dict], total_files: int, total_entries: int,
                  min_words: int, ratio: float) -> str:
    lines = [
        "=" * 80,
        "  字幕漏译检查报告",
        "=" * 80,
        f"  检查文件数: {total_files}",
        f"  总字幕条目: {total_entries}",
        f"  疑似漏译条目: {len(results)}",
        f"  判定规则: 英文 >= {min_words} 词 且 中文字数 < 英文词数 × {ratio}",
        "=" * 80,
    ]

    if not results:
        lines.append("\n[OK] 未发现疑似漏译条目。")
        return "\n".join(lines)

    by_file: dict[str, list[dict]] = {}
    for r in results:
        by_file.setdefault(r["file"], []).append(r)

    for fp, items in by_file.items():
        lines.append(f"\n{'─' * 80}")
        lines.append(f"  [文件] {fp}  ({len(items)} 条疑似漏译)")
        lines.append(f"{'─' * 80}")

        for item in items:
            lines.append(f"\n  [#{item['index']}]  {item['start']} --> {item['end']}")
            lines.append(f"  英文 ({item['en_words']} 词): {item['en'][:200]}")
            lines.append(f"  中文 ({item['zh_chars']} 字, 比率 {item['ratio']:.2f}): {item['zh'][:200]}")
            lines.append(f"  [!] 中/英字数比 {item['ratio']:.2f} < {ratio}，疑似漏译")

    lines.append(f"\n{'=' * 80}")
    lines.append(f"  总计 {len(results)} 条疑似漏译条目")
    lines.append(f"{'=' * 80}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# GUI
# ═══════════════════════════════════════════════════════════════

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("字幕漏译检查工具")
        self.root.geometry("960x700")
        self.root.minsize(800, 500)

        self._srt_files: list[Path] = []
        self._build_ui()

    def _build_ui(self):
        # ── 顶部：文件选择 ──
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill=tk.X)

        ttk.Label(top, text="SRT 文件/目录:").pack(side=tk.LEFT)

        self._file_var = tk.StringVar()
        file_entry = ttk.Entry(top, textvariable=self._file_var, width=60)
        file_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        ttk.Button(top, text="选文件", command=self._pick_files).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="选目录", command=self._pick_dir).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="清除", command=self._clear_files).pack(side=tk.LEFT, padx=2)

        # ── 参数设置 ──
        param_frame = ttk.LabelFrame(self.root, text="判定参数", padding=8)
        param_frame.pack(fill=tk.X, padx=10, pady=(0, 5))

        ttk.Label(param_frame, text="英文最少词数:").grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self._min_words_var = tk.IntVar(value=MIN_ENGLISH_WORDS)
        ttk.Spinbox(param_frame, from_=5, to=100, textvariable=self._min_words_var, width=6).grid(row=0, column=1, sticky=tk.W, padx=(0, 15))

        ttk.Label(param_frame, text="中/英比率阈值:").grid(row=0, column=2, sticky=tk.W, padx=(0, 5))
        self._ratio_var = tk.DoubleVar(value=ZH_EN_RATIO)
        ttk.Spinbox(param_frame, from_=0.10, to=1.00, increment=0.05, textvariable=self._ratio_var, width=6).grid(row=0, column=3, sticky=tk.W, padx=(0, 15))

        ttk.Label(param_frame, text="中文最少字数:").grid(row=0, column=4, sticky=tk.W, padx=(0, 5))
        self._min_chars_var = tk.IntVar(value=MIN_ZH_CHARS)
        ttk.Spinbox(param_frame, from_=3, to=50, textvariable=self._min_chars_var, width=6).grid(row=0, column=5, sticky=tk.W, padx=(0, 15))

        # 运行 & 保存按钮
        btn_frame = ttk.Frame(param_frame)
        btn_frame.grid(row=0, column=6, padx=(10, 0))
        self._run_btn = ttk.Button(btn_frame, text="开始检查", command=self._start_check)
        self._run_btn.pack(side=tk.LEFT, padx=2)
        self._save_btn = ttk.Button(btn_frame, text="保存报告", command=self._save_report, state=tk.DISABLED)
        self._save_btn.pack(side=tk.LEFT, padx=2)

        # ── 结果区 ──
        result_frame = ttk.LabelFrame(self.root, text="检查结果", padding=5)
        result_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self._result_text = scrolledtext.ScrolledText(
            result_frame, wrap=tk.WORD, font=("Microsoft YaHei", 10),
            state=tk.DISABLED
        )
        self._result_text.pack(fill=tk.BOTH, expand=True)

        # ── 状态栏 ──
        self._status_var = tk.StringVar(value="就绪 — 选择 SRT 文件或目录后点击「开始检查」")
        status_bar = ttk.Label(self.root, textvariable=self._status_var, relief=tk.SUNKEN, anchor=tk.W, padding=4)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        # 存储最后一次报告
        self._last_report = ""

    def _pick_files(self):
        paths = filedialog.askopenfilenames(
            title="选择 SRT 字幕文件",
            filetypes=[("SRT 字幕", "*.srt"), ("所有文件", "*.*")]
        )
        if paths:
            self._srt_files = [Path(p) for p in paths]
            self._file_var.set("; ".join(str(p) for p in paths))

    def _pick_dir(self):
        path = filedialog.askdirectory(title="选择包含 SRT 文件的目录")
        if path:
            dir_path = Path(path)
            self._srt_files = sorted(list(dir_path.glob("*.srt")) + list(dir_path.glob("*.SRT")))
            self._file_var.set(f"[目录] {path}  ({len(self._srt_files)} 个 .srt)")

    def _clear_files(self):
        self._srt_files = []
        self._file_var.set("")

    def _start_check(self):
        # 如果输入框里是分号分隔的路径，先解析
        if not self._srt_files:
            raw = self._file_var.get().strip()
            if raw:
                parts = [p.strip() for p in raw.split(";") if p.strip()]
                self._srt_files = [Path(p) for p in parts if Path(p).is_file()]
            if not self._srt_files:
                messagebox.showwarning("提示", "请先选择 SRT 文件或包含 .srt 的目录。")
                return

        min_words = self._min_words_var.get()
        ratio = self._ratio_var.get()
        min_chars = self._min_chars_var.get()

        self._run_btn.config(state=tk.DISABLED)
        self._save_btn.config(state=tk.DISABLED)
        self._status_var.set("正在检查...")
        self._set_result("")

        threading.Thread(target=self._do_check, args=(min_words, ratio, min_chars), daemon=True).start()

    def _do_check(self, min_words, ratio, min_chars):
        all_results = []
        total_entries = 0

        for fp in self._srt_files:
            self._update_status(f"正在检查: {fp.name} ...")
            incomplete, count = check_file(fp, min_words, ratio, min_chars)
            total_entries += count
            all_results.extend(incomplete)

        report = format_report(all_results, len(self._srt_files), total_entries, min_words, ratio)
        self._last_report = report

        self.root.after(0, lambda: self._show_result(report, len(all_results)))

    def _show_result(self, report: str, incomplete_count: int):
        self._set_result(report)
        self._run_btn.config(state=tk.NORMAL)
        if incomplete_count > 0:
            self._save_btn.config(state=tk.NORMAL)
        self._status_var.set(f"检查完成 — 发现 {incomplete_count} 条疑似漏译")

    def _set_result(self, text: str):
        self._result_text.config(state=tk.NORMAL)
        self._result_text.delete("1.0", tk.END)
        self._result_text.insert("1.0", text)
        self._result_text.config(state=tk.DISABLED)

    def _update_status(self, msg: str):
        self.root.after(0, lambda: self._status_var.set(msg))

    def _save_report(self):
        path = filedialog.asksaveasfilename(
            title="保存报告",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if path:
            Path(path).write_text(self._last_report, encoding="utf-8")
            self._status_var.set(f"报告已保存至: {path}")


# ═══════════════════════════════════════════════════════════════
# CLI 模式
# ═══════════════════════════════════════════════════════════════

def cli_main():
    parser = argparse.ArgumentParser(description="检查字幕文件中的疑似漏译/半译条目")
    parser.add_argument("files", nargs="*", help="SRT 字幕文件路径（可多个）")
    parser.add_argument("--dir", "-d", help="扫描目录下所有 .srt 文件")
    parser.add_argument("--output", "-o", help="将报告写入文件（默认输出到终端）")
    parser.add_argument("--min-words", "-w", type=int, default=MIN_ENGLISH_WORDS,
                        help=f"英文最少词数（默认 {MIN_ENGLISH_WORDS}）")
    parser.add_argument("--ratio", "-r", type=float, default=ZH_EN_RATIO,
                        help=f"中/英比率阈值（默认 {ZH_EN_RATIO}）")
    parser.add_argument("--min-chars", "-c", type=int, default=MIN_ZH_CHARS,
                        help=f"中文最少字数（默认 {MIN_ZH_CHARS}）")
    args = parser.parse_args()

    srt_files: list[Path] = []
    if args.dir:
        dir_path = Path(args.dir)
        if not dir_path.is_dir():
            print(f"[ERROR] 目录不存在: {args.dir}", file=sys.stderr)
            sys.exit(1)
        srt_files.extend(sorted(dir_path.glob("*.srt")))
        srt_files.extend(sorted(dir_path.glob("*.SRT")))
    for f in args.files:
        p = Path(f)
        if p.is_file():
            srt_files.append(p)
        else:
            print(f"[WARN] 文件不存在，跳过: {f}", file=sys.stderr)

    if not srt_files:
        print("用法: python check_undertranslation.py --cli <file.srt> [file2.srt ...]")
        print("      python check_undertranslation.py --cli --dir <directory>")
        sys.exit(1)

    all_results = []
    total_entries = 0
    for fp in srt_files:
        print(f"检查: {fp} ...")
        incomplete, count = check_file(fp, args.min_words, args.ratio, args.min_chars)
        total_entries += count
        all_results.extend(incomplete)
        print(f"  {'[!]' if incomplete else '[OK]'} 发现 {len(incomplete)} 条疑似漏译")

    report = format_report(all_results, len(srt_files), total_entries, args.min_words, args.ratio)

    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"\n报告已写入: {args.output}")
    else:
        print(f"\n{report}")


if __name__ == "__main__":
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
        cli_main()
    else:
        root = tk.Tk()
        App(root)
        root.mainloop()

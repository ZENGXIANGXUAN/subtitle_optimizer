#!/usr/bin/env python3
"""字幕对比工具 GUI 版本。

独立运行:
    python subtitle_diff_gui.py
"""

import re
import sys
from pathlib import Path
from dataclasses import dataclass
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QSplitter, QTextEdit,
    QGroupBox, QListWidget, QListWidgetItem, QCheckBox, QComboBox,
    QStatusBar, QFrame
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QColor, QTextCharFormat, QPalette


@dataclass
class SubtitleEntry:
    index: int
    start_time: str
    end_time: str
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class DiffItem:
    index: int
    time: str
    status: str  # "added", "removed", "modified", "unchanged"
    old_text: str
    new_text: str


def parse_srt(content: str) -> list[SubtitleEntry]:
    entries = []
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


def compute_diff(old_entries: list[SubtitleEntry], new_entries: list[SubtitleEntry]) -> list[DiffItem]:
    old_map = {e.index: e for e in old_entries}
    new_map = {e.index: e for e in new_entries}
    all_indices = sorted(set(old_map.keys()) | set(new_map.keys()))
    results = []
    for idx in all_indices:
        old = old_map.get(idx)
        new = new_map.get(idx)
        if old and not new:
            results.append(DiffItem(idx, f"{old.start_time} --> {old.end_time}", "removed", old.text, ""))
        elif new and not old:
            results.append(DiffItem(idx, f"{new.start_time} --> {new.end_time}", "added", "", new.text))
        elif old.text != new.text:
            results.append(DiffItem(idx, f"{new.start_time} --> {new.end_time}", "modified", old.text, new.text))
        else:
            results.append(DiffItem(idx, f"{new.start_time} --> {new.end_time}", "unchanged", old.text, new.text))
    return results


class DiffMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("字幕对比工具")
        self.resize(1200, 800)

        self._old_entries: list[SubtitleEntry] = []
        self._new_entries: list[SubtitleEntry] = []
        self._diff_items: list[DiffItem] = []

        self._setup_ui()
        self._apply_style()

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)

        # ── 文件选择区 ──
        file_group = QGroupBox("文件选择")
        file_layout = QVBoxLayout(file_group)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("旧版本:"))
        self._old_path = QLabel("未选择文件")
        self._old_path.setStyleSheet("color: #8a7b66; font-style: italic;")
        row1.addWidget(self._old_path, 1)
        btn_old = QPushButton("选择文件")
        btn_old.clicked.connect(lambda: self._pick_file("old"))
        row1.addWidget(btn_old)
        file_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("新版本:"))
        self._new_path = QLabel("未选择文件")
        self._new_path.setStyleSheet("color: #8a7b66; font-style: italic;")
        row2.addWidget(self._new_path, 1)
        btn_new = QPushButton("选择文件")
        btn_new.clicked.connect(lambda: self._pick_file("new"))
        row2.addWidget(btn_new)
        file_layout.addLayout(row2)

        row3 = QHBoxLayout()
        btn_compare = QPushButton("▶ 开始对比")
        btn_compare.setObjectName("btn_primary")
        btn_compare.clicked.connect(self._run_compare)
        row3.addWidget(btn_compare)
        row3.addStretch()

        self._filter_combo = QComboBox()
        self._filter_combo.addItems(["全部", "仅修改", "仅新增/删除", "仅差异"])
        self._filter_combo.currentIndexChanged.connect(self._apply_filter)
        row3.addWidget(QLabel("显示:"))
        row3.addWidget(self._filter_combo)

        self._sync_scroll = QCheckBox("同步滚动")
        self._sync_scroll.setChecked(True)
        row3.addWidget(self._sync_scroll)

        file_layout.addLayout(row3)
        root.addWidget(file_group)

        # ── 统计栏 ──
        self._stats_label = QLabel("")
        self._stats_label.setStyleSheet("font-weight: 600; padding: 4px 0;")
        root.addWidget(self._stats_label)

        # ── 差异列表 + 对比视图 ──
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左侧: 差异条目列表
        left_group = QGroupBox("差异条目")
        left_layout = QVBoxLayout(left_group)
        self._diff_list = QListWidget()
        self._diff_list.currentRowChanged.connect(self._on_item_selected)
        left_layout.addWidget(self._diff_list)
        splitter.addWidget(left_group)

        # 右侧: 对比详情
        right_group = QGroupBox("对比详情")
        right_layout = QVBoxLayout(right_group)

        detail_splitter = QSplitter(Qt.Orientation.Horizontal)

        old_panel = QWidget()
        old_layout = QVBoxLayout(old_panel)
        old_layout.setContentsMargins(0, 0, 0, 0)
        old_layout.addWidget(QLabel("旧版本"))
        self._old_text = QTextEdit()
        self._old_text.setReadOnly(True)
        old_layout.addWidget(self._old_text)
        detail_splitter.addWidget(old_panel)

        new_panel = QWidget()
        new_layout = QVBoxLayout(new_panel)
        new_layout.setContentsMargins(0, 0, 0, 0)
        new_layout.addWidget(QLabel("新版本"))
        self._new_text = QTextEdit()
        self._new_text.setReadOnly(True)
        new_layout.addWidget(self._new_text)
        detail_splitter.addWidget(new_panel)

        right_layout.addWidget(detail_splitter)
        splitter.addWidget(right_group)

        splitter.setSizes([300, 900])
        root.addWidget(splitter, 1)

        # ── 状态栏 ──
        self._status = QStatusBar()
        self.setStatusBar(self._status)

        # 同步滚动
        self._old_text.verticalScrollBar().valueChanged.connect(
            self._new_text.verticalScrollBar().setValue)
        self._new_text.verticalScrollBar().valueChanged.connect(
            self._old_text.verticalScrollBar().setValue)

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow { background: #f8f8f0; }
            QWidget { font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif; font-size: 13px; color: #725d42; }
            QGroupBox {
                border: 2px solid #c4b89e; border-radius: 12px;
                margin-top: 14px; padding: 12px;
                background: rgb(247, 243, 223);
                font-weight: 600; color: #8a7b66;
            }
            QGroupBox::title {
                subcontrol-origin: margin; left: 14px; padding: 0 8px;
                background: rgb(247, 243, 223); color: #794f27;
            }
            QPushButton {
                background: #f8f8f0; border: 2px solid #c4b89e; border-radius: 50px;
                color: #794f27; padding: 7px 20px; font-weight: 600; min-height: 28px;
            }
            QPushButton:hover { border-color: #a89878; background: #fffdf5; }
            QPushButton:pressed { background: #19c8b9; border-color: #11a89b; }
            QPushButton#btn_primary {
                background: #ffcc00; border-color: #d9a900; color: #5a3a1a; font-weight: 700;
            }
            QPushButton#btn_primary:hover { background: #ffd84d; }
            QTextEdit {
                background: #f8f8f0; border: 2px solid #c4b89e; border-radius: 8px;
                color: #725d42; font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 12px;
            }
            QListWidget {
                background: #f8f8f0; border: 2px solid #c4b89e; border-radius: 8px;
                outline: none;
            }
            QListWidget::item {
                padding: 6px 10px; border-bottom: 1px solid #e0d8c8;
            }
            QListWidget::item:selected { background: #e6f9f6; color: #0d8a7f; }
            QListWidget::item:hover:!selected { background: rgba(25, 200, 185, 0.06); }
            QComboBox {
                background: rgb(247, 243, 223); border: 2px solid #c4b89e;
                border-radius: 50px; padding: 5px 14px; min-height: 26px;
            }
            QComboBox:hover { border-color: #a89878; }
            QComboBox::drop-down { border: none; width: 20px; }
            QComboBox::down-arrow {
                image: none;
                border-left: 4px solid transparent; border-right: 4px solid transparent;
                border-top: 5px solid #a0936e;
            }
            QCheckBox { spacing: 6px; font-weight: 500; }
            QCheckBox::indicator {
                width: 16px; height: 16px; border-radius: 6px;
                border: 2px solid #c4b89e; background: rgb(247, 243, 223);
            }
            QCheckBox::indicator:checked { background: #19c8b9; border-color: #11a89b; }
            QStatusBar {
                background: rgb(247, 243, 223); color: #8a7b66;
                border-top: 2px solid #d4c9b4; font-size: 12px;
            }
            QSplitter::handle { background: #d4c9b4; width: 2px; }
            QSplitter::handle:hover { background: #19c8b9; }
            QScrollBar:vertical { background: transparent; width: 8px; }
            QScrollBar::handle:vertical { background: #c4b89e; border-radius: 4px; min-height: 20px; }
            QScrollBar::handle:vertical:hover { background: #a89878; }
            QScrollBar:horizontal { background: transparent; height: 8px; }
            QScrollBar::handle:horizontal { background: #c4b89e; border-radius: 4px; }
        """)

    def _pick_file(self, which: str):
        path, _ = QFileDialog.getOpenFileName(
            self, f"选择{'旧' if which == 'old' else '新'}版本字幕文件",
            "", "SRT 文件 (*.srt);;所有文件 (*.*)"
        )
        if not path:
            return
        if which == "old":
            self._old_path.setText(path)
            self._old_path.setStyleSheet("")
        else:
            self._new_path.setText(path)
            self._new_path.setStyleSheet("")

    def _run_compare(self):
        old_path = self._old_path.text()
        new_path = self._new_path.text()
        if "未选择" in old_path or "未选择" in new_path:
            self._status.showMessage("请先选择两个文件", 3000)
            return

        try:
            with open(old_path, "r", encoding="utf-8") as f:
                self._old_entries = parse_srt(f.read())
            with open(new_path, "r", encoding="utf-8") as f:
                self._new_entries = parse_srt(f.read())
        except Exception as e:
            self._status.showMessage(f"读取失败: {e}", 5000)
            return

        self._diff_items = compute_diff(self._old_entries, self._new_entries)
        self._update_stats()
        self._apply_filter()
        self._status.showMessage(f"对比完成: {len(self._diff_items)} 条字幕", 3000)

    def _update_stats(self):
        stats = {"added": 0, "removed": 0, "modified": 0, "unchanged": 0}
        for d in self._diff_items:
            stats[d.status] += 1
        total = len(self._diff_items)
        self._stats_label.setText(
            f"共 {total} 条 | "
            f"<span style='color:#28a745'>+{stats['added']} 新增</span> | "
            f"<span style='color:#dc3545'>-{stats['removed']} 删除</span> | "
            f"<span style='color:#d4a017'>~{stats['modified']} 修改</span> | "
            f"<span style='color:#8a7b66'>={stats['unchanged']} 未变</span>"
        )

    def _apply_filter(self):
        self._diff_list.clear()
        filter_mode = self._filter_combo.currentIndex()

        for item in self._diff_items:
            show = False
            if filter_mode == 0:  # 全部
                show = True
            elif filter_mode == 1:  # 仅修改
                show = item.status == "modified"
            elif filter_mode == 2:  # 仅新增/删除
                show = item.status in ("added", "removed")
            elif filter_mode == 3:  # 仅差异
                show = item.status != "unchanged"

            if not show:
                continue

            icon = {"added": "🟢", "removed": "🔴", "modified": "🟡", "unchanged": "⚪"}[item.status]
            label = f"{icon} #{item.index}  {item.time}"
            if item.status == "modified":
                old_preview = item.old_text.split("\n")[0][:30]
                label += f"  [{old_preview}…]"
            elif item.status == "added":
                preview = item.new_text.split("\n")[0][:30]
                label += f"  [+{preview}…]"
            elif item.status == "removed":
                preview = item.old_text.split("\n")[0][:30]
                label += f"  [-{preview}…]"

            list_item = QListWidgetItem(label)
            list_item.setData(Qt.ItemDataRole.UserRole, item)
            self._diff_list.addItem(list_item)

    def _on_item_selected(self, row: int):
        if row < 0:
            return
        item = self._diff_list.item(row)
        if not item:
            return
        diff: DiffItem = item.data(Qt.ItemDataRole.UserRole)
        if not diff:
            return

        self._old_text.clear()
        self._new_text.clear()

        # 旧版本
        old_fmt = QTextCharFormat()
        if diff.status == "removed":
            old_fmt.setForeground(QColor("#dc3545"))
        elif diff.status == "modified":
            old_fmt.setForeground(QColor("#dc3545"))
        else:
            old_fmt.setForeground(QColor("#725d42"))

        cursor = self._old_text.textCursor()
        if diff.old_text:
            cursor.insertText(diff.old_text, old_fmt)
        else:
            cursor.insertText("(无内容)", QTextCharFormat())

        # 新版本
        new_fmt = QTextCharFormat()
        if diff.status == "added":
            new_fmt.setForeground(QColor("#28a745"))
        elif diff.status == "modified":
            new_fmt.setForeground(QColor("#28a745"))
        else:
            new_fmt.setForeground(QColor("#725d42"))

        cursor = self._new_text.textCursor()
        if diff.new_text:
            cursor.insertText(diff.new_text, new_fmt)
        else:
            cursor.insertText("(无内容)", QTextCharFormat())

        # 高亮差异行
        if diff.status == "modified":
            self._highlight_diff_lines(diff.old_text, diff.new_text)

    def _highlight_diff_lines(self, old_text: str, new_text: str):
        """高亮显示有差异的行。"""
        old_lines = old_text.split("\n")
        new_lines = new_text.split("\n")

        # 简单逐行对比高亮
        yellow_fmt = QTextCharFormat()
        yellow_fmt.setBackground(QColor("#fff3cd"))

        # 在旧文本中高亮不同的行
        cursor = self._old_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        for i, line in enumerate(old_lines):
            if i < len(new_lines) and line != new_lines[i]:
                cursor.movePosition(cursor.MoveOperation.StartOfBlock)
                cursor.movePosition(cursor.MoveOperation.EndOfBlock, cursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(yellow_fmt)
            cursor.movePosition(cursor.MoveOperation.NextBlock)

        # 在新文本中高亮不同的行
        cursor = self._new_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        for i, line in enumerate(new_lines):
            if i < len(old_lines) and line != old_lines[i]:
                cursor.movePosition(cursor.MoveOperation.StartOfBlock)
                cursor.movePosition(cursor.MoveOperation.EndOfBlock, cursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(yellow_fmt)
            cursor.movePosition(cursor.MoveOperation.NextBlock)


def main():
    app = QApplication(sys.argv)
    window = DiffMainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

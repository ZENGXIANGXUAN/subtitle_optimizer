"""
UI 全局样式表 — Animal Island 主题
温暖大地色系 + 圆润立体感 + 游戏按键式交互
设计 token 参考 animal-island-ui SKILL.md
"""

APP_STYLE = """
/* ═══════════════════════════════════════════════════════
   Animal Island Theme
   ═══════════════════════════════════════════════════════ */

QMainWindow { background: #f8f8f0; }
QWidget {
    font-family: 'PingFang SC', 'Microsoft YaHei UI', 'Segoe UI', sans-serif;
    font-size: 13px;
    color: #725d42;
}

/* ── GroupBox ── */
QGroupBox {
    border: 2px solid #c4b89e;
    border-radius: 18px;
    margin-top: 14px;
    padding: 16px 12px 12px 12px;
    background: rgb(247, 243, 223);
    color: #8a7b66;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 8px;
    background: rgb(247, 243, 223);
    color: #794f27;
}

/* ── Buttons ── */
QPushButton {
    background: #f8f8f0;
    border: 2px solid #c4b89e;
    border-radius: 50px;
    color: #794f27;
    padding: 7px 20px;
    font-size: 13px;
    font-weight: 600;
    min-height: 30px;
    letter-spacing: 0.02em;
}
QPushButton:hover {
    border-color: #a89878;
    color: #5a3a1a;
    background: #fffdf5;
}
QPushButton:pressed {
    background: #19c8b9;
    border-color: #11a89b;
    color: #5a3a1a;
}
QPushButton:disabled {
    background: #f0ece2;
    color: #b0a38a;
    border-color: #d4c9b4;
}

QPushButton#btn_primary {
    background: #ffcc00;
    border: 2px solid #d9a900;
    color: #5a3a1a;
    font-weight: 700;
}
QPushButton#btn_primary:hover {
    background: #ffd84d;
    border-color: #c99800;
    color: #4a2f14;
}
QPushButton#btn_primary:pressed {
    background: #e6b800;
    border-color: #b88f00;
    color: #5a3a1a;
}
QPushButton#btn_primary:disabled {
    background: #d4d0c4;
    color: #9f927d;
    border-color: #c4b89e;
}

QPushButton#btn_danger {
    background: #fff;
    border: 2px solid #e05a5a;
    color: #e05a5a;
}
QPushButton#btn_danger:hover {
    background: #e05a5a;
    color: #FFFFFF;
    border-color: #c94444;
}

QPushButton#btn_success {
    background: #fff;
    border: 2px solid #6fba2c;
    color: #6fba2c;
}
QPushButton#btn_success:hover {
    background: #6fba2c;
    color: #21330d;
}

/* ── TextEdit / Log ── */
QTextEdit {
    background: #f8f8f0;
    border: 2px solid #c4b89e;
    border-radius: 12px;
    color: #725d42;
    selection-background-color: #e6f9f6;
    font-family: 'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace;
    font-size: 12px;
    line-height: 1.6;
    padding: 4px;
}

/* ── LineEdit ── */
QLineEdit {
    background: rgb(247, 243, 223);
    border: 2.5px solid #c4b89e;
    border-radius: 50px;
    color: #725d42;
    padding: 6px 18px;
    selection-background-color: #e6f9f6;
    min-height: 30px;
    font-weight: 500;
}
QLineEdit:focus {
    border-color: #ffcc00;
    background: rgb(250, 248, 240);
}
QLineEdit:hover { border-color: #a89878; }

/* ── ComboBox ── */
QComboBox {
    background: rgb(247, 243, 223);
    border: 2.5px solid #c4b89e;
    border-radius: 50px;
    color: #725d42;
    padding: 6px 16px;
    min-height: 30px;
    font-weight: 500;
}
QComboBox:hover { border-color: #a89878; }
QComboBox:focus { border-color: #ffcc00; }
QComboBox::drop-down {
    border: none;
    width: 24px;
    margin-right: 4px;
}
QComboBox::down-arrow {
    image: none;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid #a0936e;
}
QComboBox QAbstractItemView {
    background: rgb(247, 243, 223);
    border: 2px solid #c4b89e;
    border-radius: 12px;
    color: #725d42;
    selection-background-color: #e6f9f6;
    selection-color: #5a3a1a;
    outline: none;
    padding: 4px;
}

/* ── ProgressBar ── */
QProgressBar {
    background: #f0e8d8;
    border: 2px solid #d4c9b4;
    border-radius: 50px;
    text-align: center;
    color: #8a7b66;
    font-size: 11px;
    font-weight: 600;
    min-height: 20px;
    max-height: 20px;
}
QProgressBar::chunk {
    background: #19c8b9;
    border-radius: 50px;
}

/* ── Tabs ── */
QTabWidget::pane {
    border: 2px solid #c4b89e;
    background: #f8f8f0;
    border-radius: 0 18px 18px 18px;
}
QTabBar::tab {
    background: transparent;
    color: #8a7b66;
    padding: 8px 22px;
    border: none;
    border-bottom: 3px solid transparent;
    font-size: 13px;
    font-weight: 600;
    margin-right: 3px;
}
QTabBar::tab:selected {
    color: #794f27;
    border-bottom: 3px solid #19c8b9;
}
QTabBar::tab:hover:!selected {
    color: #725d42;
    background: rgba(25, 200, 185, 0.06);
}

/* ── Status Bar ── */
QStatusBar {
    background: rgb(247, 243, 223);
    color: #8a7b66;
    border-top: 2px solid #d4c9b4;
    font-size: 12px;
    padding: 2px 10px;
}

/* ── Labels ── */
QLabel { color: #725d42; background: transparent; }
QLabel#label_title {
    color: #794f27;
    font-size: 17px;
    font-weight: 700;
    letter-spacing: 0.5px;
}
QLabel#label_stat {
    color: #6fba2c;
    font-size: 12px;
    font-weight: 600;
}
QLabel.section_label {
    color: #8a7b66;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.5px;
}

/* ── Splitter ── */
QSplitter::handle {
    background: #d4c9b4;
    width: 2px;
    height: 2px;
}
QSplitter::handle:hover { background: #19c8b9; }

/* ── ScrollBar ── */
QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 0;
    border-radius: 4px;
}
QScrollBar::handle:vertical {
    background: #c4b89e;
    border-radius: 4px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover { background: #a89878; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal {
    background: transparent;
    height: 8px;
    border-radius: 4px;
}
QScrollBar::handle:horizontal {
    background: #c4b89e;
    border-radius: 4px;
}
QScrollBar::handle:horizontal:hover { background: #a89878; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

/* ── ListWidget ── */
QListWidget {
    background: #f8f8f0;
    border: 2px solid #c4b89e;
    border-radius: 12px;
    color: #725d42;
    font-size: 12px;
    font-weight: 500;
    outline: none;
}
QListWidget::item {
    padding: 10px 14px;
    border-bottom: 1px solid #d4c9b4;
    border-radius: 0;
}
QListWidget::item:selected {
    background: #e6f9f6;
    color: #0d8a7f;
    border-left: 3px solid #19c8b9;
}
QListWidget::item:hover:!selected { background: rgba(25, 200, 185, 0.06); }

/* ── CheckBox ── */
QCheckBox {
    color: #725d42;
    spacing: 8px;
    font-weight: 500;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 8px;
    border: 2.5px solid #c4b89e;
    background: rgb(247, 243, 223);
}
QCheckBox::indicator:checked {
    background: #19c8b9;
    border-color: #11a89b;
}
QCheckBox::indicator:hover { border-color: #19c8b9; }
"""

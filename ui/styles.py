"""
UI 全局样式表文件
采用类 GitHub Dark 主题的高级暗黑配色
"""

APP_STYLE = """
QMainWindow { background: #0D1117; }
QWidget {
    font-family: 'PingFang SC', 'Microsoft YaHei UI', 'Segoe UI', sans-serif;
    font-size: 13px;
    color: #C9D1D9;
}

/* ── GroupBox ── */
QGroupBox {
    border: 1px solid #21262D;
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 10px;
    background: #161B22;
    color: #8B949E;
    font-size: 11px;
    font-weight: bold;
    letter-spacing: 1px;
    text-transform: uppercase;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    background: #161B22;
}

/* ── Buttons ── */
QPushButton {
    background: #21262D;
    border: 1px solid #30363D;
    border-radius: 6px;
    color: #C9D1D9;
    padding: 7px 18px;
    font-size: 13px;
    font-weight: 500;
    min-height: 28px;
}
QPushButton:hover {
    background: #30363D;
    border-color: #58A6FF;
    color: #E6EDF3;
}
QPushButton:pressed {
    background: #388BFD;
    border-color: #388BFD;
    color: #FFFFFF;
}
QPushButton:disabled {
    background: #161B22;
    color: #484F58;
    border-color: #21262D;
}

QPushButton#btn_primary {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
        stop:0 #1F6FEB, stop:1 #388BFD);
    color: #FFFFFF;
    border: none;
    font-weight: 600;
}
QPushButton#btn_primary:hover {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
        stop:0 #388BFD, stop:1 #58A6FF);
}
QPushButton#btn_primary:disabled {
    background: #21262D;
    color: #484F58;
    border: 1px solid #30363D;
}

QPushButton#btn_danger {
    background: transparent;
    border: 1px solid #DA3633;
    color: #F85149;
}
QPushButton#btn_danger:hover {
    background: #DA3633;
    color: #FFFFFF;
    border-color: #DA3633;
}

QPushButton#btn_success {
    background: transparent;
    border: 1px solid #238636;
    color: #3FB950;
}
QPushButton#btn_success:hover {
    background: #238636;
    color: #FFFFFF;
}

/* ── TextEdit / Log ── */
QTextEdit {
    background: #0D1117;
    border: 1px solid #21262D;
    border-radius: 6px;
    color: #C9D1D9;
    selection-background-color: #264F78;
    font-family: 'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace;
    font-size: 12px;
    line-height: 1.6;
}

/* ── LineEdit ── */
QLineEdit {
    background: #0D1117;
    border: 1px solid #30363D;
    border-radius: 6px;
    color: #C9D1D9;
    padding: 6px 10px;
    selection-background-color: #264F78;
    min-height: 28px;
}
QLineEdit:focus {
    border-color: #388BFD;
    background: #0D1117;
}
QLineEdit:hover { border-color: #484F58; }

/* ── ComboBox ── */
QComboBox {
    background: #0D1117;
    border: 1px solid #30363D;
    border-radius: 6px;
    color: #C9D1D9;
    padding: 6px 10px;
    min-height: 28px;
}
QComboBox:hover { border-color: #484F58; }
QComboBox:focus { border-color: #388BFD; }
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #8B949E;
    margin-right: 6px;
}
QComboBox QAbstractItemView {
    background: #161B22;
    border: 1px solid #30363D;
    border-radius: 6px;
    color: #C9D1D9;
    selection-background-color: #1F6FEB;
    outline: none;
}

/* ── ProgressBar ── */
QProgressBar {
    background: #21262D;
    border: none;
    border-radius: 4px;
    text-align: center;
    color: #8B949E;
    font-size: 11px;
    min-height: 20px;
    max-height: 20px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
        stop:0 #1F6FEB, stop:0.6 #388BFD, stop:1 #58A6FF);
    border-radius: 4px;
}

/* ── Tabs ── */
QTabWidget::pane {
    border: 1px solid #21262D;
    background: #0D1117;
    border-radius: 0 6px 6px 6px;
}
QTabBar::tab {
    background: transparent;
    color: #8B949E;
    padding: 8px 20px;
    border: none;
    border-bottom: 2px solid transparent;
    font-size: 13px;
    font-weight: 500;
    margin-right: 2px;
}
QTabBar::tab:selected {
    color: #E6EDF3;
    border-bottom: 2px solid #388BFD;
}
QTabBar::tab:hover:!selected { color: #C9D1D9; }

/* ── Status Bar ── */
QStatusBar {
    background: #161B22;
    color: #484F58;
    border-top: 1px solid #21262D;
    font-size: 12px;
    padding: 2px 8px;
}

/* ── Labels ── */
QLabel { color: #C9D1D9; }
QLabel#label_title {
    color: #E6EDF3;
    font-size: 16px;
    font-weight: 700;
    letter-spacing: 0.5px;
}
QLabel#label_stat {
    color: #3FB950;
    font-size: 12px;
    font-weight: 500;
}
QLabel.section_label {
    color: #8B949E;
    font-size: 11px;
    font-weight: bold;
    letter-spacing: 1px;
}

/* ── Splitter ── */
QSplitter::handle {
    background: #21262D;
    width: 1px;
    height: 1px;
}
QSplitter::handle:hover { background: #388BFD; }

/* ── ScrollBar ── */
QScrollBar:vertical {
    background: transparent;
    width: 6px;
    margin: 0;
    border-radius: 3px;
}
QScrollBar::handle:vertical {
    background: #30363D;
    border-radius: 3px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover { background: #484F58; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal {
    background: transparent;
    height: 6px;
    border-radius: 3px;
}
QScrollBar::handle:horizontal {
    background: #30363D;
    border-radius: 3px;
}
QScrollBar::handle:horizontal:hover { background: #484F58; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

/* ── ListWidget ── */
QListWidget {
    background: #0D1117;
    border: 1px solid #21262D;
    border-radius: 6px;
    color: #C9D1D9;
    font-size: 12px;
    outline: none;
}
QListWidget::item {
    padding: 8px 12px;
    border-bottom: 1px solid #161B22;
    border-radius: 0;
}
QListWidget::item:selected {
    background: #1F6FEB22;
    color: #58A6FF;
    border-left: 2px solid #388BFD;
}
QListWidget::item:hover:!selected { background: #161B22; }

/* ── CheckBox ── */
QCheckBox {
    color: #C9D1D9;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid #30363D;
    background: #0D1117;
}
QCheckBox::indicator:checked {
    background: #1F6FEB;
    border-color: #1F6FEB;
}
QCheckBox::indicator:hover { border-color: #388BFD; }
"""
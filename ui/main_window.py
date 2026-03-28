import sys
import os
import json
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QTextEdit, QProgressBar,
    QSplitter, QFrame, QScrollArea, QComboBox,
    QGroupBox, QLineEdit, QStatusBar, QTabWidget, QMessageBox,
    QCheckBox, QGridLayout, QListWidget, QListWidgetItem,
    QSizePolicy
)
from PyQt6.QtCore import Qt, QThread, pyqtSlot, QTimer
from PyQt6.QtGui import QFont, QColor, QPalette

# 导入业务层逻辑和界面辅助
from core.models import SubtitleEntry, parse_srt
from core.client import MistralClient
from core.workers import AnalysisWorker, OptimizeWorker, CleanWorker, BatchWorker
from ui.styles import APP_STYLE
from ui.highlighter import SRTHighlighter


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.entries: list[SubtitleEntry] = []
        self.context_text = ""
        self.client: Optional[MistralClient] = None
        self._current_file_path: Optional[str] = None

        # 线程及 Worker 引用保持
        self._analysis_thread: Optional[QThread] = None
        self._analysis_worker: Optional[AnalysisWorker] = None
        self._clean_thread: Optional[QThread] = None
        self._clean_worker: Optional[CleanWorker] = None
        self._optimize_thread: Optional[QThread] = None
        self._optimize_worker: Optional[OptimizeWorker] = None
        self._batch_thread: Optional[QThread] = None
        self._batch_worker: Optional[BatchWorker] = None

        self._cmp_cells: dict = {}  # entry.index -> (left_label, right_label)
        self._batch_files: list = []

        self.setWindowTitle("字幕优化工具 ✦ Subtitle Optimizer")
        self.resize(1400, 900)
        self._setup_palette()
        self._build_ui()
        self._load_config()

    def _setup_palette(self):
        palette = QPalette()
        bg = QColor("#0D1117")
        surface = QColor("#161B22")
        palette.setColor(QPalette.ColorRole.Window, bg)
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#C9D1D9"))
        palette.setColor(QPalette.ColorRole.Base, surface)
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#21262D"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#C9D1D9"))
        palette.setColor(QPalette.ColorRole.Button, QColor("#21262D"))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#C9D1D9"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#388BFD"))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
        self.setPalette(palette)
        # 使用从独立文件导入的 QSS 样式
        self.setStyleSheet(APP_STYLE)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(0)
        root.setContentsMargins(0, 0, 0, 0)

        # Header
        header = self._build_header()
        root.addWidget(header)

        # Main tab: 单文件 / 批量处理 / API设置
        self._main_tabs = QTabWidget()
        self._main_tabs.setStyleSheet("""
            QTabWidget::pane { border: none; background: #0D1117; }
            QTabBar::tab {
                background: transparent;
                color: #8B949E;
                padding: 10px 28px;
                border: none;
                border-bottom: 2px solid transparent;
                font-size: 13px;
                font-weight: 500;
                margin-right: 4px;
            }
            QTabBar::tab:selected {
                color: #E6EDF3;
                border-bottom: 2px solid #388BFD;
            }
            QTabBar::tab:hover:!selected { color: #C9D1D9; }
        """)

        # 单文件页
        single_page = QWidget()
        single_layout = QHBoxLayout(single_page)
        single_layout.setSpacing(0)
        single_layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(2)
        left_panel = self._build_left_panel()
        right_panel = self._build_right_panel()
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([480, 920])
        single_layout.addWidget(splitter)

        # 批量处理页
        batch_page = self._build_batch_page()

        # API 设置页
        api_page = self._build_api_page()

        self._main_tabs.addTab(single_page, "单文件处理")
        self._main_tabs.addTab(batch_page, "批量处理")
        self._main_tabs.addTab(api_page, "⚙ API 设置")
        root.addWidget(self._main_tabs, 1)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪 · 请先配置 API Key 并加载字幕文件")

    def _build_header(self) -> QWidget:
        w = QFrame()
        w.setObjectName("header")
        w.setStyleSheet("""
            QFrame#header {
                background: #161B22;
                border-bottom: 1px solid #21262D;
                min-height: 56px; max-height: 56px;
            }
        """)
        layout = QHBoxLayout(w)
        layout.setContentsMargins(20, 0, 20, 0)
        layout.setSpacing(0)

        icon_lbl = QLabel("◆")
        icon_lbl.setStyleSheet("color: #388BFD; font-size: 18px; margin-right: 10px;")
        layout.addWidget(icon_lbl)

        title = QLabel("字幕优化工具")
        title.setObjectName("label_title")
        layout.addWidget(title)

        sep = QLabel("·")
        sep.setStyleSheet("color: #30363D; font-size: 16px; margin: 0 12px;")
        layout.addWidget(sep)

        subtitle = QLabel("Subtitle Optimizer  /  Mistral AI")
        subtitle.setStyleSheet("color: #484F58; font-size: 12px; font-weight: 400;")
        layout.addWidget(subtitle)
        layout.addStretch()

        self.lbl_stat = QLabel("未加载文件")
        self.lbl_stat.setObjectName("label_stat")
        self.lbl_stat.setStyleSheet("""
            color: #484F58;
            font-size: 12px;
            background: #21262D;
            border: 1px solid #30363D;
            border-radius: 12px;
            padding: 3px 12px;
        """)
        layout.addWidget(self.lbl_stat)

        return w

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setMinimumWidth(360)
        w.setMaximumWidth(500)
        w.setStyleSheet("QWidget { background: #0D1117; }")
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 16, 8, 16)
        layout.setSpacing(12)

        # API 配置提示
        api_hint = QFrame()
        api_hint.setStyleSheet("""
            QFrame {
                background: #161B22;
                border: 1px solid #30363D;
                border-radius: 8px;
            }
        """)
        ah = QHBoxLayout(api_hint)
        ah.setContentsMargins(12, 10, 12, 10)
        ah.setSpacing(10)
        icon_lbl2 = QLabel("⚙")
        icon_lbl2.setStyleSheet("color: #388BFD; font-size: 16px;")
        ah.addWidget(icon_lbl2)
        hint_text = QLabel("API Key、模型、并发数等\n请前往「⚙ API 设置」页面配置")
        hint_text.setStyleSheet("color: #8B949E; font-size: 12px; line-height: 1.5;")
        ah.addWidget(hint_text, 1)
        btn_goto_api = QPushButton("前往配置")
        btn_goto_api.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: 1px solid #388BFD;
                border-radius: 6px;
                color: #58A6FF;
                padding: 4px 12px;
                font-size: 12px;
            }
            QPushButton:hover { background: #1F6FEB22; }
        """)
        btn_goto_api.clicked.connect(lambda: self._main_tabs.setCurrentIndex(2))
        ah.addWidget(btn_goto_api)
        layout.addWidget(api_hint)

        # 文件操作
        file_group = QGroupBox("字幕文件")
        fg = QVBoxLayout(file_group)
        fg.setSpacing(8)

        self.lbl_file = QLabel("未选择文件")
        self.lbl_file.setStyleSheet("""
            color: #484F58;
            font-size: 12px;
            background: #161B22;
            border: 1px solid #21262D;
            border-radius: 6px;
            padding: 6px 10px;
        """)
        self.lbl_file.setWordWrap(True)
        fg.addWidget(self.lbl_file)

        btn_open = QPushButton("  打开 SRT 文件")
        btn_open.clicked.connect(self._open_file)
        fg.addWidget(btn_open)
        layout.addWidget(file_group)

        # 翻译情景
        ctx_group = QGroupBox("翻译情景")
        cg = QVBoxLayout(ctx_group)
        cg.setSpacing(8)

        self.text_context = QTextEdit()
        self.text_context.setPlaceholderText(
            "点击「分析字幕」自动识别翻译情景，\n或手动输入情景描述..."
        )
        self.text_context.setMaximumHeight(120)
        self.text_context.setStyleSheet("""
            QTextEdit {
                background: #0D1117;
                border: 1px solid #21262D;
                border-radius: 6px;
                color: #C9D1D9;
                font-size: 12px;
                padding: 4px;
            }
        """)
        cg.addWidget(self.text_context)

        btn_analyze = QPushButton("分析字幕情景")
        btn_analyze.clicked.connect(self._run_analysis)
        btn_analyze.setObjectName("btn_primary")
        cg.addWidget(btn_analyze)
        layout.addWidget(ctx_group)

        # 优化控制
        opt_group = QGroupBox("优化控制")
        og = QVBoxLayout(opt_group)
        og.setSpacing(8)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%v / %m 条  (%p%)")
        og.addWidget(self.progress_bar)

        self.lbl_progress_detail = QLabel("就绪")
        self.lbl_progress_detail.setStyleSheet("color: #484F58; font-size: 11px;")
        og.addWidget(self.lbl_progress_detail)

        # 阶段指示器
        stage_row = QHBoxLayout()
        stage_row.setSpacing(4)
        self._stage_labels = []
        stage_names = ["① 分析语境", "② 清洗None", "③ 优化字幕"]
        for name in stage_names:
            lbl = QLabel(name)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("""
                color: #484F58;
                font-size: 11px;
                background: #161B22;
                border: 1px solid #21262D;
                border-radius: 4px;
                padding: 2px 6px;
            """)
            stage_row.addWidget(lbl)
            self._stage_labels.append(lbl)
        og.addLayout(stage_row)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.btn_optimize = QPushButton("▶  开始优化")
        self.btn_optimize.setObjectName("btn_primary")
        self.btn_optimize.clicked.connect(self._run_optimize)
        btn_row.addWidget(self.btn_optimize)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("btn_danger")
        self.btn_stop.clicked.connect(self._stop_optimize)
        self.btn_stop.setEnabled(False)
        btn_row.addWidget(self.btn_stop)
        og.addLayout(btn_row)

        # Export area
        export_frame = QFrame()
        export_frame.setStyleSheet("""
            QFrame {
                background: #161B22;
                border: 1px solid #21262D;
                border-radius: 6px;
                padding: 2px;
            }
        """)
        ef = QVBoxLayout(export_frame)
        ef.setContentsMargins(8, 8, 8, 8)
        ef.setSpacing(6)

        self.chk_overwrite_single = QCheckBox("覆盖原文件（不加 _optimized 后缀）")
        self.chk_overwrite_single.setStyleSheet("font-size: 12px; color: #8B949E;")
        ef.addWidget(self.chk_overwrite_single)

        btn_save = QPushButton("  导出优化字幕")
        btn_save.setObjectName("btn_success")
        btn_save.clicked.connect(self._export_srt)
        ef.addWidget(btn_save)
        og.addWidget(export_frame)

        layout.addWidget(opt_group)
        layout.addStretch()

        return w

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(8, 16, 16, 16)
        layout.setSpacing(0)

        tabs = QTabWidget()

        # Tab 1: 原始字幕
        self.tab_original = QTextEdit()
        self.tab_original.setReadOnly(True)
        self.tab_original.setFont(QFont("Consolas", 12))
        SRTHighlighter(self.tab_original.document())
        tabs.addTab(self.tab_original, "原始字幕")

        # Tab 2: 对比查看
        compare_widget = QWidget()
        compare_layout = QVBoxLayout(compare_widget)
        compare_layout.setSpacing(0)
        compare_layout.setContentsMargins(0, 0, 0, 0)

        header_row = QWidget()
        header_row.setFixedHeight(36)
        header_row.setStyleSheet("""
            background: #161B22;
            border-bottom: 1px solid #21262D;
            border-radius: 6px 6px 0 0;
        """)
        hl = QHBoxLayout(header_row)
        hl.setContentsMargins(12, 0, 12, 0)
        lbl_orig = QLabel("原始字幕")
        lbl_orig.setStyleSheet("color:#8B949E; font-size:11px; font-weight:700; letter-spacing:1px;")
        lbl_opt = QLabel("优化后")
        lbl_opt.setStyleSheet("color:#3FB950; font-size:11px; font-weight:700; letter-spacing:1px;")
        hl.addWidget(lbl_orig)
        hl.addWidget(lbl_opt)
        compare_layout.addWidget(header_row)

        self._cmp_scroll = QScrollArea()
        self._cmp_scroll.setWidgetResizable(True)
        self._cmp_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._cmp_scroll.setStyleSheet("QScrollArea { border: none; background: #0D1117; }")

        self._cmp_container = QWidget()
        self._cmp_container.setStyleSheet("background: #0D1117;")
        self._cmp_grid = QGridLayout(self._cmp_container)
        self._cmp_grid.setContentsMargins(6, 6, 6, 6)
        self._cmp_grid.setSpacing(3)
        self._cmp_grid.setColumnStretch(0, 1)
        self._cmp_grid.setColumnStretch(1, 1)

        self._cmp_scroll.setWidget(self._cmp_container)
        compare_layout.addWidget(self._cmp_scroll)
        tabs.addTab(compare_widget, "对比查看")

        # Tab 3: 日志
        self.text_log = QTextEdit()
        self.text_log.setReadOnly(True)
        self.text_log.setStyleSheet("""
            QTextEdit {
                background: #0D1117;
                color: #484F58;
                border: none;
                font-family: 'JetBrains Mono', 'Consolas', monospace;
                font-size: 12px;
            }
        """)
        tabs.addTab(self.text_log, "运行日志")

        layout.addWidget(tabs)
        return w

    def _build_api_page(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("QWidget { background: #0D1117; }")
        outer = QVBoxLayout(w)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(16)

        title_row = QHBoxLayout()
        icon_l = QLabel("⚙")
        icon_l.setStyleSheet("color: #388BFD; font-size: 20px;")
        title_row.addWidget(icon_l)
        title_l = QLabel("API 设置")
        title_l.setStyleSheet("color: #E6EDF3; font-size: 16px; font-weight: 700; margin-left: 8px;")
        title_row.addWidget(title_l)
        title_row.addStretch()
        save_tip = QLabel("配置自动保存至本地，下次启动恢复。")
        save_tip.setStyleSheet("color: #484F58; font-size: 11px;")
        title_row.addWidget(save_tip)
        btn_save_cfg = QPushButton("  保存配置")
        btn_save_cfg.setObjectName("btn_primary")
        btn_save_cfg.clicked.connect(self._save_config)
        btn_save_cfg.setFixedHeight(32)
        title_row.addWidget(btn_save_cfg)
        outer.addLayout(title_row)

        sep_line = QFrame()
        sep_line.setFrameShape(QFrame.Shape.HLine)
        sep_line.setStyleSheet("color: #21262D;")
        outer.addWidget(sep_line)

        cols = QHBoxLayout()
        cols.setSpacing(16)
        outer.addLayout(cols, 1)

        left_card = QFrame()
        left_card.setStyleSheet("""
            QFrame {
                background: #161B22;
                border: 1px solid #21262D;
                border-radius: 10px;
            }
        """)
        lc = QVBoxLayout(left_card)
        lc.setContentsMargins(24, 20, 24, 20)
        lc.setSpacing(14)

        def _section_lbl(text):
            lb = QLabel(text)
            lb.setStyleSheet("color: #8B949E; font-size: 11px; font-weight: 600; letter-spacing: 0.5px;")
            return lb

        lc.addWidget(_section_lbl("API Key"))
        key_row = QHBoxLayout()
        self.input_apikey = QLineEdit()
        self.input_apikey.setPlaceholderText("sk-••••••••••••••••")
        self.input_apikey.setEchoMode(QLineEdit.EchoMode.Password)
        key_row.addWidget(self.input_apikey)
        btn_toggle_key = QPushButton("显示")
        btn_toggle_key.setMaximumWidth(52)
        btn_toggle_key.setStyleSheet("""
            QPushButton { background: #21262D; border: 1px solid #30363D;
                border-radius: 6px; color: #8B949E; padding: 4px 8px; font-size: 11px; }
            QPushButton:hover { border-color: #484F58; color: #C9D1D9; }
        """)

        def _toggle_key_vis():
            if self.input_apikey.echoMode() == QLineEdit.EchoMode.Password:
                self.input_apikey.setEchoMode(QLineEdit.EchoMode.Normal)
                btn_toggle_key.setText("隐藏")
            else:
                self.input_apikey.setEchoMode(QLineEdit.EchoMode.Password)
                btn_toggle_key.setText("显示")

        btn_toggle_key.clicked.connect(_toggle_key_vis)
        key_row.addWidget(btn_toggle_key)
        lc.addLayout(key_row)

        lc.addWidget(_section_lbl("Base URL  （留空使用官方默认）"))
        self.input_base_url = QLineEdit()
        self.input_base_url.setPlaceholderText("https://api.mistral.ai/v1")
        lc.addWidget(self.input_base_url)

        lc.addWidget(_section_lbl("模型"))
        self.combo_model = QComboBox()
        self.combo_model.addItems([
            "mistral-large-latest",
            "mistral-medium-latest",
            "mistral-small-latest",
            "open-mistral-7b",
        ])
        lc.addWidget(self.combo_model)

        lc.addWidget(_section_lbl("性能参数"))
        params_row = QHBoxLayout()
        params_row.setSpacing(12)

        conc_wrap = QVBoxLayout()
        conc_lbl = QLabel("并发数  (1 – 12)")
        conc_lbl.setStyleSheet("color: #484F58; font-size: 11px;")
        conc_wrap.addWidget(conc_lbl)
        self.input_concurrency = QLineEdit("6")
        self.input_concurrency.setPlaceholderText("1-12")
        self.input_concurrency.setValidator(__import__('PyQt6.QtGui', fromlist=['QIntValidator']).QIntValidator(1, 12))
        conc_wrap.addWidget(self.input_concurrency)
        params_row.addLayout(conc_wrap)

        batch_wrap = QVBoxLayout()
        batch_lbl2 = QLabel("批大小  (1 – 50)")
        batch_lbl2.setStyleSheet("color: #484F58; font-size: 11px;")
        batch_wrap.addWidget(batch_lbl2)
        self.input_batch = QLineEdit("5")
        self.input_batch.setPlaceholderText("1-50")
        self.input_batch.setValidator(__import__('PyQt6.QtGui', fromlist=['QIntValidator']).QIntValidator(1, 50))
        batch_wrap.addWidget(self.input_batch)
        params_row.addLayout(batch_wrap)
        lc.addLayout(params_row)

        lc.addWidget(_section_lbl("字幕行顺序"))
        order_row = QHBoxLayout()
        order_row.setSpacing(12)
        order_desc = QLabel("第三行语言：")
        order_desc.setStyleSheet("color: #484F58; font-size: 12px;")
        order_row.addWidget(order_desc)
        self.combo_chinese_line = QComboBox()
        self.combo_chinese_line.addItems(["中文（默认）", "英文"])
        self.combo_chinese_line.setToolTip("设置字幕第三行（第一文本行）是中文还是英文")
        order_row.addWidget(self.combo_chinese_line, 1)
        order_hint = QLabel("第四行自动为另一语言")
        order_hint.setStyleSheet("color: #484F58; font-size: 11px;")
        order_row.addWidget(order_hint)
        lc.addLayout(order_row)

        lc.addStretch()
        cols.addWidget(left_card, 1)

        right_card = QFrame()
        right_card.setStyleSheet("""
            QFrame {
                background: #161B22;
                border: 1px solid #21262D;
                border-radius: 10px;
            }
        """)
        rc = QVBoxLayout(right_card)
        rc.setContentsMargins(24, 20, 24, 20)
        rc.setSpacing(10)

        gloss_title_row = QHBoxLayout()
        gloss_icon = QLabel("📖")
        gloss_icon.setStyleSheet("font-size: 16px;")
        gloss_title_row.addWidget(gloss_icon)
        gloss_lbl = QLabel("固定词语翻译（术语表）")
        gloss_lbl.setStyleSheet(
            "color: #8B949E; font-size: 11px; font-weight: 600; letter-spacing: 0.5px; margin-left: 6px;")
        gloss_title_row.addWidget(gloss_lbl)
        gloss_title_row.addStretch()
        rc.addLayout(gloss_title_row)

        gloss_hint = QLabel("每行一条，格式：英文->中文　例：Algorithm->算法")
        gloss_hint.setStyleSheet("color: #484F58; font-size: 11px;")
        rc.addWidget(gloss_hint)

        self.input_glossary = QTextEdit()
        self.input_glossary.setPlaceholderText(
            "Algorithm->算法\nCSID->交付状态变化\nDeployment->部署\nPipeline->流水线\n..."
        )
        self.input_glossary.setStyleSheet("""
            QTextEdit {
                background: #0D1117;
                border: 1px solid #21262D;
                border-radius: 6px;
                color: #C9D1D9;
                font-size: 12px;
                font-family: 'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace;
                padding: 8px;
                line-height: 1.7;
            }
            QTextEdit:focus { border-color: #388BFD; }
        """)
        rc.addWidget(self.input_glossary, 1)

        gloss_note = QLabel("翻译时 AI 将强制使用上述术语，适用于单文件和批量处理。")
        gloss_note.setStyleSheet("color: #484F58; font-size: 11px;")
        gloss_note.setWordWrap(True)
        rc.addWidget(gloss_note)

        cols.addWidget(right_card, 1)
        return w

    def _build_batch_page(self) -> QWidget:
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        left = QWidget()
        left.setMinimumWidth(320)
        left.setMaximumWidth(440)
        ll = QVBoxLayout(left)
        ll.setSpacing(12)
        ll.setContentsMargins(0, 0, 0, 0)

        file_group = QGroupBox("待处理文件队列")
        fg = QVBoxLayout(file_group)
        fg.setSpacing(8)

        self.batch_list = QListWidget()
        self.batch_list.setMinimumHeight(260)
        fg.addWidget(self.batch_list)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        btn_add = QPushButton("添加文件")
        btn_add.clicked.connect(self._batch_add_files)
        btn_remove = QPushButton("移除选中")
        btn_remove.clicked.connect(self._batch_remove_file)
        btn_clear_list = QPushButton("清空")
        btn_clear_list.setObjectName("btn_danger")
        btn_clear_list.clicked.connect(self._batch_clear_files)
        btn_row.addWidget(btn_add)
        btn_row.addWidget(btn_remove)
        btn_row.addWidget(btn_clear_list)
        fg.addLayout(btn_row)
        ll.addWidget(file_group)

        out_group = QGroupBox("导出设置")
        og_out = QVBoxLayout(out_group)
        og_out.setSpacing(10)

        self.chk_same_dir = QCheckBox("输出到原文件所在目录")
        self.chk_same_dir.setChecked(True)
        self.chk_same_dir.stateChanged.connect(self._on_same_dir_changed)
        og_out.addWidget(self.chk_same_dir)

        self.out_dir_widget = QWidget()
        out_dir_layout = QHBoxLayout(self.out_dir_widget)
        out_dir_layout.setContentsMargins(0, 0, 0, 0)
        out_dir_layout.setSpacing(6)
        self.batch_out_dir = QLineEdit()
        self.batch_out_dir.setPlaceholderText("选择自定义输出目录...")
        out_dir_layout.addWidget(self.batch_out_dir)
        btn_out_dir = QPushButton("浏览")
        btn_out_dir.setMaximumWidth(54)
        btn_out_dir.clicked.connect(self._batch_pick_outdir)
        out_dir_layout.addWidget(btn_out_dir)
        self.out_dir_widget.setVisible(False)
        og_out.addWidget(self.out_dir_widget)

        self.chk_overwrite_batch = QCheckBox("覆盖原文件（不加 _optimized 后缀）")
        self.chk_overwrite_batch.setStyleSheet("color: #E3B341;")
        og_out.addWidget(self.chk_overwrite_batch)

        note = QLabel("API Key、模型、并发数等参数\n均使用「⚙ API 设置」页面中的配置。")
        note.setStyleSheet("color: #484F58; font-size: 11px; margin-top: 2px;")
        note.setWordWrap(True)
        og_out.addWidget(note)
        ll.addWidget(out_group)

        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(8)
        self.btn_batch_start = QPushButton("▶  开始批量处理")
        self.btn_batch_start.setObjectName("btn_primary")
        self.btn_batch_start.clicked.connect(self._batch_start)
        ctrl_row.addWidget(self.btn_batch_start)

        self.btn_batch_stop = QPushButton("停止")
        self.btn_batch_stop.setObjectName("btn_danger")
        self.btn_batch_stop.clicked.connect(self._batch_stop)
        self.btn_batch_stop.setEnabled(False)
        ctrl_row.addWidget(self.btn_batch_stop)
        ll.addLayout(ctrl_row)

        ll.addStretch()
        layout.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setSpacing(12)
        rl.setContentsMargins(0, 0, 0, 0)

        prog_group = QGroupBox("处理进度")
        pg = QVBoxLayout(prog_group)
        pg.setSpacing(8)

        overall_lbl = QLabel("整体进度")
        overall_lbl.setStyleSheet("color: #8B949E; font-size: 11px; font-weight: 600;")
        pg.addWidget(overall_lbl)
        self.batch_overall_bar = QProgressBar()
        self.batch_overall_bar.setFormat("文件  %v / %m  (%p%)")
        pg.addWidget(self.batch_overall_bar)

        cur_lbl = QLabel("当前文件")
        cur_lbl.setStyleSheet("color: #8B949E; font-size: 11px; font-weight: 600; margin-top: 4px;")
        pg.addWidget(cur_lbl)
        self.batch_cur_bar = QProgressBar()
        self.batch_cur_bar.setFormat("%v / %m 条  (%p%)")
        self.batch_cur_bar.setStyleSheet("""
            QProgressBar { background: #21262D; border: none; border-radius: 4px;
                text-align: center; color: #8B949E; font-size: 11px;
                min-height: 20px; max-height: 20px; }
            QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #238636, stop:1 #3FB950); border-radius: 4px; }
        """)
        pg.addWidget(self.batch_cur_bar)
        rl.addWidget(prog_group)

        log_group = QGroupBox("批量日志")
        lg = QVBoxLayout(log_group)
        self.batch_log = QTextEdit()
        self.batch_log.setReadOnly(True)
        self.batch_log.setStyleSheet("""
            QTextEdit {
                background: #0D1117;
                color: #8B949E;
                border: none;
                font-family: 'JetBrains Mono', 'Consolas', monospace;
                font-size: 12px;
            }
        """)
        lg.addWidget(self.batch_log)
        rl.addWidget(log_group, 1)

        layout.addWidget(right, 1)
        return w

    def _on_same_dir_changed(self, state):
        self.out_dir_widget.setVisible(state == 0)

    def _batch_add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择字幕文件", "", "SRT 字幕文件 (*.srt);;所有文件 (*)"
        )
        for p in paths:
            if p not in self._batch_files:
                self._batch_files.append(p)
                item = QListWidgetItem(f"⏳  {Path(p).name}")
                item.setData(Qt.ItemDataRole.UserRole, p)
                item.setToolTip(p)
                self.batch_list.addItem(item)

    def _batch_remove_file(self):
        for item in self.batch_list.selectedItems():
            path = item.data(Qt.ItemDataRole.UserRole)
            if path in self._batch_files:
                self._batch_files.remove(path)
            self.batch_list.takeItem(self.batch_list.row(item))

    def _batch_clear_files(self):
        self._batch_files.clear()
        self.batch_list.clear()

    def _batch_pick_outdir(self):
        d = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if d:
            self.batch_out_dir.setText(d)

    def _batch_log_msg(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.batch_log.append(f"[{ts}] {msg}")

    def _batch_set_item_status(self, row: int, status: str):
        item = self.batch_list.item(row)
        if not item:
            return
        icons = {"processing": "🔄", "done": "✅", "error": "❌", "pending": "⏳"}
        name = Path(item.data(Qt.ItemDataRole.UserRole)).name
        item.setText(f"{icons.get(status, '⏳')}  {name}")

    def _batch_start(self):
        if not self._batch_files:
            QMessageBox.warning(self, "提示", "请先添加要处理的文件")
            return
        client = self._get_client()
        if not client:
            return

        self.btn_batch_start.setEnabled(False)
        self.btn_batch_stop.setEnabled(True)
        self.batch_overall_bar.setMaximum(len(self._batch_files))
        self.batch_overall_bar.setValue(0)
        self.batch_log.clear()
        self._batch_log_msg(f"▶ 开始批量处理 {len(self._batch_files)} 个文件")

        for i in range(self.batch_list.count()):
            self._batch_set_item_status(i, "pending")

        out_dir = None
        if not self.chk_same_dir.isChecked():
            out_dir = self.batch_out_dir.text().strip() or None
        overwrite = self.chk_overwrite_batch.isChecked()
        concurrency = max(1, min(12, int(self.input_concurrency.text() or "6")))
        batch_size = max(1, min(50, int(self.input_batch.text() or "5")))

        self._batch_worker = BatchWorker(
            client=client,
            files=list(self._batch_files),
            out_dir=out_dir,
            concurrency=concurrency,
            batch_size=batch_size,
            overwrite=overwrite,
            chinese_first=self._chinese_first(),
            glossary_prompt=self._get_glossary_prompt(),
        )
        self._batch_thread = QThread()
        self._batch_worker.moveToThread(self._batch_thread)
        self._batch_thread.started.connect(self._batch_worker.run)

        self._batch_worker.file_started.connect(self._on_batch_file_started)
        self._batch_worker.file_analysis_done.connect(self._on_batch_analysis_done)
        self._batch_worker.file_progress.connect(self._on_batch_file_progress)
        self._batch_worker.file_done.connect(self._on_batch_file_done)
        self._batch_worker.file_error.connect(self._on_batch_file_error)
        self._batch_worker.all_done.connect(self._on_batch_all_done)
        self._batch_worker.log.connect(self._batch_log_msg)
        self._batch_worker.all_done.connect(self._batch_thread.quit)
        self._batch_thread.finished.connect(self._batch_thread.deleteLater)
        self._batch_thread.start()

    def _batch_stop(self):
        if self._batch_worker:
            self._batch_worker.cancel()
        self.btn_batch_start.setEnabled(True)
        self.btn_batch_stop.setEnabled(False)
        self._batch_log_msg("⏹ 已停止批量处理")

    @pyqtSlot(int, str)
    def _on_batch_file_started(self, file_idx: int, filename: str):
        self._batch_set_item_status(file_idx, "processing")
        self.batch_cur_bar.setValue(0)
        self.status_bar.showMessage(f"批量处理中：{filename}")

    @pyqtSlot(int, str, str)
    def _on_batch_analysis_done(self, file_idx: int, filename: str, context: str):
        self._batch_log_msg(f"  ✅ [{filename}] 情景分析完成")

    @pyqtSlot(int, int, int)
    def _on_batch_file_progress(self, file_idx: int, current: int, total: int):
        self.batch_cur_bar.setMaximum(total)
        self.batch_cur_bar.setValue(current)

    @pyqtSlot(int, str, str)
    def _on_batch_file_done(self, file_idx: int, filename: str, out_path: str):
        self._batch_set_item_status(file_idx, "done")
        self.batch_overall_bar.setValue(file_idx + 1)
        self._batch_log_msg(f"✅ [{filename}] 完成 → {out_path}")

    @pyqtSlot(int, str, str)
    def _on_batch_file_error(self, file_idx: int, filename: str, err: str):
        self._batch_set_item_status(file_idx, "error")
        self.batch_overall_bar.setValue(file_idx + 1)
        self._batch_log_msg(f"❌ [{filename}] 失败：{err}")

    def _on_batch_all_done(self):
        self.btn_batch_start.setEnabled(True)
        self.btn_batch_stop.setEnabled(False)
        total = self.batch_list.count()
        done = sum(
            1 for i in range(total) if self.batch_list.item(i) and self.batch_list.item(i).text().startswith("✅"))
        self._batch_log_msg(f"🎉 批量处理完成：{done}/{total} 个文件成功")
        self.status_bar.showMessage(f"批量处理完成：{done}/{total} 成功")

    def _get_client(self) -> Optional[MistralClient]:
        key = self.input_apikey.text().strip()
        if not key:
            QMessageBox.warning(self, "提示", "请先输入 Mistral API Key")
            return None
        base_url = self.input_base_url.text().strip()
        return MistralClient(key, self.combo_model.currentText(), base_url)

    def _chinese_first(self) -> bool:
        return self.combo_chinese_line.currentIndex() == 0

    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "打开字幕文件", "", "SRT 字幕文件 (*.srt);;所有文件 (*)"
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                content = f.read()
            self.entries = parse_srt(content)
            self._current_file_path = path
            self.lbl_file.setText(f"📄 {Path(path).name}")
            self.tab_original.setPlainText(content)
            n = len(self.entries)
            self.lbl_stat.setText(f"  {n} 条字幕")
            self.lbl_stat.setStyleSheet("""
                color: #3FB950; font-size: 12px; background: #12261E;
                border: 1px solid #238636; border-radius: 12px; padding: 3px 12px;
            """)
            self.progress_bar.setMaximum(n)
            self.progress_bar.setValue(0)
            self._log(f"✅ 已加载：{path}，共 {n} 条字幕")
            self.status_bar.showMessage(f"已加载 {n} 条字幕 · {Path(path).name}")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"无法读取文件：{e}")

    def _run_analysis(self):
        if not self.entries:
            QMessageBox.warning(self, "提示", "请先加载字幕文件")
            return
        client = self._get_client()
        if not client:
            return

        self._log("🔍 开始分析翻译情景...")
        self.text_context.setPlainText("分析中...")

        self._analysis_worker = AnalysisWorker(client, self.entries)
        self._analysis_thread = QThread()
        self._analysis_worker.moveToThread(self._analysis_thread)
        self._analysis_thread.started.connect(self._analysis_worker.run)
        self._analysis_worker.progress.connect(self._log)
        self._analysis_worker.finished.connect(self._on_analysis_done)
        self._analysis_worker.finished.connect(self._analysis_thread.quit)
        self._analysis_worker.error.connect(
            lambda e: (self._log(f"❌ 分析失败：{e}"),
                       self.text_context.setPlainText("分析失败，请手动输入情景"))
        )
        self._analysis_worker.error.connect(self._analysis_thread.quit)
        self._analysis_thread.finished.connect(self._analysis_thread.deleteLater)
        self._analysis_thread.start()

    def _on_analysis_done(self, result: str):
        self.context_text = result
        self.text_context.setPlainText(result)
        self._log("✅ 情景分析完成")

    def _run_optimize(self):
        if not self.entries:
            QMessageBox.warning(self, "提示", "请先加载字幕文件")
            return
        client = self._get_client()
        if not client:
            return

        self.btn_optimize.setEnabled(False)
        self.btn_stop.setEnabled(True)

        for e in self.entries:
            e.status = "pending"
            e.optimized_lines = None

        while self._cmp_grid.count():
            item = self._cmp_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cmp_cells.clear()

        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(len(self.entries))
        self._set_stage(0)

        self._log("🔍 [阶段1/3] 开始分析翻译情景...")
        self.text_context.setPlainText("分析中...")

        self._analysis_worker = AnalysisWorker(client, self.entries)
        self._analysis_thread = QThread()
        self._analysis_worker.moveToThread(self._analysis_thread)
        self._analysis_thread.started.connect(self._analysis_worker.run)
        self._analysis_worker.progress.connect(self._log)
        self._analysis_worker.finished.connect(lambda result: self._after_analysis(result, client))
        self._analysis_worker.finished.connect(self._analysis_thread.quit)
        self._analysis_worker.error.connect(lambda e: self._on_pipeline_error(f"分析失败：{e}"))
        self._analysis_worker.error.connect(self._analysis_thread.quit)
        self._analysis_thread.finished.connect(self._analysis_thread.deleteLater)
        self._analysis_thread.start()

    def _after_analysis(self, result: str, client: MistralClient):
        self.context_text = result
        self.text_context.setPlainText(result)
        self._log("✅ [阶段1/3] 情景分析完成")

        concurrency = max(1, min(12, int(self.input_concurrency.text() or "6")))
        batch_size = max(1, min(50, int(self.input_batch.text() or "5")))

        none_count = sum(
            1 for e in self.entries
            if not e.chinese or e.chinese.strip().lower() in {"none", "null", "无", "—", "-", ""}
        )

        # 将 glossary_prompt 单独提取出来，不再和 context 拼在一起
        glossary_prompt = self._get_glossary_prompt()

        if none_count == 0:
            self._log("⏩ [阶段2/3] 无 None 条目，跳过清洗")
            self._start_optimize_stage(client, concurrency, batch_size)
            return

        self._log(f"🧹 [阶段2/3] 开始清洗 {none_count} 条 None 字幕...")
        self._set_stage(1)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(none_count)
        self.lbl_progress_detail.setText(f"清洗 None：0/{none_count}")

        # 将 result (情景) 和 glossary_prompt (词汇表) 分开传入
        self._clean_worker = CleanWorker(client, self.entries, result, glossary_prompt, concurrency, batch_size)
        self._clean_thread = QThread()
        self._clean_worker.moveToThread(self._clean_thread)
        self._clean_thread.started.connect(self._clean_worker.run)
        self._clean_worker.entry_done.connect(self._on_clean_entry_done)
        self._clean_worker.progress.connect(self._on_clean_progress)
        self._clean_worker.all_done.connect(lambda count: self._after_clean(count, client, concurrency, batch_size))
        self._clean_worker.all_done.connect(self._clean_thread.quit)
        self._clean_worker.error.connect(lambda e: self._on_pipeline_error(f"清洗失败：{e}"))
        self._clean_thread.finished.connect(self._clean_thread.deleteLater)
        self._clean_thread.start()

    def _on_clean_entry_done(self, idx: int, zh: str):
        self._log(f"  🧹 [{idx}] → {zh[:20]}{'...' if len(zh) > 20 else ''}")

    def _on_clean_progress(self, current: int, total: int):
        self.progress_bar.setValue(current)
        self.lbl_progress_detail.setText(f"清洗 None：{current}/{total}")

    def _after_clean(self, count: int, client: MistralClient, concurrency: int, batch_size: int):
        self._log(f"✅ [阶段2/3] 清洗完成，共修复 {count} 条 None 字幕")
        self._start_optimize_stage(client, concurrency, batch_size)

    def _start_optimize_stage(self, client: MistralClient, concurrency: int, batch_size: int):
        base_context = self.text_context.toPlainText().strip() or "通用教育视频字幕"
        glossary_prompt = self._get_glossary_prompt()

        if glossary_prompt:
            self._log(f"📖 已加载固定术语表规则")

        self._log(f"▶ [阶段3/3] 开始优化 {len(self.entries)} 条字幕 · 并发:{concurrency} · 批:{batch_size}")
        self._set_stage(2)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(len(self.entries))

        # 将 base_context (情景) 和 glossary_prompt (词汇表) 分开传入
        self._optimize_worker = OptimizeWorker(client, self.entries, base_context, glossary_prompt, concurrency,
                                               batch_size)
        self._optimize_thread = QThread()
        self._optimize_worker.moveToThread(self._optimize_thread)
        self._optimize_thread.started.connect(self._optimize_worker.run)
        self._optimize_worker.entry_done.connect(self._on_entry_done)
        self._optimize_worker.progress.connect(self._on_progress)
        self._optimize_worker.all_done.connect(self._on_optimize_done)
        self._optimize_worker.all_done.connect(self._optimize_thread.quit)
        self._optimize_worker.error.connect(lambda e: self._log(f"❌ {e}"))
        self._optimize_thread.finished.connect(self._optimize_thread.deleteLater)
        self._optimize_thread.start()

    def _on_pipeline_error(self, msg: str):
        self._log(f"❌ {msg}")
        self.btn_optimize.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._set_stage(-1)

    def _set_stage(self, active: int):
        active_style = "color: #E6EDF3; font-size: 11px; background: #1F6FEB; border: 1px solid #388BFD; border-radius: 4px; padding: 2px 6px;"
        done_style = "color: #3FB950; font-size: 11px; background: #12261E; border: 1px solid #238636; border-radius: 4px; padding: 2px 6px;"
        idle_style = "color: #484F58; font-size: 11px; background: #161B22; border: 1px solid #21262D; border-radius: 4px; padding: 2px 6px;"
        for i, lbl in enumerate(self._stage_labels):
            if active == -1:
                lbl.setStyleSheet(idle_style)
            elif i < active:
                lbl.setStyleSheet(done_style)
            elif i == active:
                lbl.setStyleSheet(active_style)
            else:
                lbl.setStyleSheet(idle_style)

    @pyqtSlot(int, list, str)
    def _on_entry_done(self, idx: int, lines: list, status: str):
        entry = next((e for e in self.entries if e.index == idx), None)
        if entry:
            entry.optimized_lines = lines
            entry.status = status
            self._append_compare_entry(entry)

    @pyqtSlot(int, int)
    def _on_progress(self, current: int, total: int):
        self.progress_bar.setValue(current)
        self.lbl_progress_detail.setText(f"已完成 {current}/{total} 条")
        self.status_bar.showMessage(f"优化进行中 {current}/{total}...")

    def _on_optimize_done(self):
        done = sum(1 for e in self.entries if e.status == "done")
        err = sum(1 for e in self.entries if e.status == "error")
        self.btn_optimize.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._log(f"✅ [阶段3/3] 优化完成：成功 {done} 条，失败 {err} 条")
        self.status_bar.showMessage(f"全部完成：{done} 成功，{err} 失败")
        for lbl in self._stage_labels:
            lbl.setStyleSheet(
                "color: #3FB950; font-size: 11px; background: #12261E; border: 1px solid #238636; border-radius: 4px; padding: 2px 6px;")
        self._refresh_compare()

    def _stop_optimize(self):
        if self._clean_worker: self._clean_worker.cancel()
        if self._optimize_worker: self._optimize_worker.cancel()
        self.btn_optimize.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._set_stage(-1)
        self._log("⏹ 已停止")

    def _make_cmp_cell(self, text: str, is_opt: bool = False, changed: bool = False) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(QFont("Consolas", 12))
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        lbl.setContentsMargins(6, 4, 6, 4)

        if changed:
            bg, border_color, color = ("#12261E", "#238636", "#7EE787") if is_opt else ("#2D1B1B", "#DA3633", "#FF7B72")
        else:
            bg, border_color, color = ("#161B22", "#21262D", "#8B949E")

        lbl.setStyleSheet(
            f"QLabel {{ background: {bg}; border: 1px solid {border_color}; border-radius: 4px; color: {color}; padding: 6px 8px; font-family: monospace; font-size: 12px; line-height: 1.5; }}")
        return lbl

    def _append_compare_entry(self, entry):
        orig_text = entry.to_srt_block_raw().rstrip("\n")
        opt_text = (
            entry.to_srt_block(True, self._chinese_first()) if entry.optimized_lines else entry.to_srt_block(False,
                                                                                                             self._chinese_first())).rstrip(
            "\n")
        changed = entry.optimized_lines is not None and orig_text != opt_text

        sb = self._cmp_scroll.verticalScrollBar()
        at_bottom = (sb.value() >= sb.maximum() - 4) and sb.maximum() > 0
        scroll_val = sb.value()

        if entry.index in self._cmp_cells:
            left_lbl, right_lbl = self._cmp_cells[entry.index]
            left_lbl.setText(orig_text)
            right_lbl.setText(opt_text)
            left_lbl.setStyleSheet(self._make_cmp_cell(orig_text, False, changed).styleSheet())
            right_lbl.setStyleSheet(self._make_cmp_cell(opt_text, True, changed).styleSheet())
        else:
            row = self._cmp_grid.rowCount()
            left_lbl = self._make_cmp_cell(orig_text, False, changed)
            right_lbl = self._make_cmp_cell(opt_text, True, changed)
            self._cmp_grid.addWidget(left_lbl, row, 0)
            self._cmp_grid.addWidget(right_lbl, row, 1)
            self._cmp_cells[entry.index] = (left_lbl, right_lbl)

        if at_bottom:
            QTimer.singleShot(10, lambda: sb.setValue(sb.maximum()))
        else:
            sb.setValue(scroll_val)

    def _refresh_compare(self):
        while self._cmp_grid.count():
            item = self._cmp_grid.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        self._cmp_cells.clear()
        for entry in self.entries: self._append_compare_entry(entry)

    def _export_srt(self):
        if not self.entries: return QMessageBox.warning(self, "提示", "没有可导出的字幕")
        if sum(1 for e in self.entries if e.optimized_lines) == 0:
            return QMessageBox.warning(self, "提示", "尚未完成任何字幕优化")

        if self.chk_overwrite_single.isChecked() and self._current_file_path:
            path = self._current_file_path
            if QMessageBox.question(self, "确认覆盖", f"将覆盖：\n{path}",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        else:
            default_name = "optimized.srt"
            if self._current_file_path:
                p = Path(self._current_file_path)
                default_name = p.stem + "_optimized" + p.suffix
            path, _ = QFileDialog.getSaveFileName(self, "保存优化字幕", default_name, "SRT 字幕文件 (*.srt)")
            if not path: return

        blocks = [entry.to_srt_block(True, self._chinese_first()) for entry in self.entries]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(blocks))

        self._log(f"💾 已导出：{path}")
        QMessageBox.information(self, "导出完成", f"已导出优化字幕：\n{path}")

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        if msg.startswith("✅") or msg.startswith("💾") or msg.startswith("🎉"):
            color = "#3FB950"
        elif msg.startswith("❌"):
            color = "#F85149"
        elif msg.startswith("▶") or msg.startswith("🔍"):
            color = "#58A6FF"
        elif msg.startswith("⏹") or msg.startswith("⚠"):
            color = "#E3B341"
        else:
            color = "#8B949E"
        self.text_log.append(f'<span style="color:#484F58">[{ts}]</span> <span style="color:{color}">{msg}</span>')

    def _get_glossary_prompt(self) -> str:
        """解析术语表输入框，返回一个措辞强硬的独立规则字符串（空则返回空串）"""
        raw = self.input_glossary.toPlainText().strip()
        if not raw: return ""
        pairs = []
        for line in raw.splitlines():
            line = line.strip()
            for sep in ("->", "→"):
                if sep in line:
                    parts = line.split(sep, 1)
                    if parts[0].strip() and parts[1].strip():
                        pairs.append(f'"{parts[0].strip()}" 必须译为 "{parts[1].strip()}"')
                    break
        if not pairs: return ""
        # 改成强硬的指令块
        return "★ 【强制术语表】遇到以下英文专业词汇，必须严格统一翻译为对应中文，绝不允许替换为同义词或意译：\n" + "\n".join(
            f"  - {p}" for p in pairs) + "\n"

    def _config_path(self) -> Path:
        return Path.home() / ".subtitle_optimizer_config.json"

    def _save_config(self):
        cfg = {
            "api_key": self.input_apikey.text().strip(),
            "base_url": self.input_base_url.text().strip(),
            "model": self.combo_model.currentText(),
            "concurrency": self.input_concurrency.text(),
            "batch_size": self.input_batch.text(),
            "chinese_first": self.combo_chinese_line.currentIndex() == 0,
            "glossary": self.input_glossary.toPlainText().strip(),
        }
        try:
            with open(self._config_path(), "w") as f:
                json.dump(cfg, f)
            self.status_bar.showMessage("配置已保存")
        except Exception as e:
            self._log(f"⚠️ 保存配置失败：{e}")

    def _load_config(self):
        try:
            with open(self._config_path()) as f:
                cfg = json.load(f)
            self.input_apikey.setText(cfg.get("api_key", ""))
            self.input_base_url.setText(cfg.get("base_url", ""))
            idx = self.combo_model.findText(cfg.get("model", ""))
            if idx >= 0: self.combo_model.setCurrentIndex(idx)
            self.input_concurrency.setText(str(cfg.get("concurrency", "6")))
            self.input_batch.setText(str(cfg.get("batch_size", "5")))
            self.combo_chinese_line.setCurrentIndex(0 if cfg.get("chinese_first", True) else 1)
            self.input_glossary.setPlainText(cfg.get("glossary", ""))
        except Exception:
            pass
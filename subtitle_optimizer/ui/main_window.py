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
from PyQt6.QtCore import Qt, QThread, pyqtSlot, QTimer, QEvent
from PyQt6.QtGui import QFont, QColor, QPalette, QWheelEvent


class NoWheelComboBox(QComboBox):
    """禁用滚轮事件的 QComboBox，防止意外改变选项"""

    def wheelEvent(self, event: QWheelEvent):
        # 完全忽略滚轮事件，不传递给父类
        event.ignore()


# 导入业务层逻辑和界面辅助
from core.models import SubtitleEntry, parse_srt, _format_subtitle_snapshot
from core.client import MistralClient
from core.workers import AnalysisWorker, OptimizeWorker, SegmentWorker, TranslateWorker, BatchWorker, LANGUAGE_MAP, LANGUAGE_LABELS
from ui.styles import APP_STYLE
from ui.highlighter import SRTHighlighter
# 尝试相对导入，如果失败则使用绝对导入
try:
    from ..utils import logger, write_phase_snapshot
except ImportError:
    import sys
    import os
    sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
    from utils import logger, write_phase_snapshot


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        logger.info("MainWindow初始化开始")
        self.entries: list[SubtitleEntry] = []
        self.context_text = ""
        self.client: Optional[MistralClient] = None
        self._current_file_path: Optional[str] = None

        # 线程及 Worker 引用保持
        self._analysis_thread: Optional[QThread] = None
        self._analysis_worker: Optional[AnalysisWorker] = None
        self._segment_thread: Optional[QThread] = None
        self._segment_worker: Optional[SegmentWorker] = None
        self._translate_thread: Optional[QThread] = None
        self._translate_worker: Optional[TranslateWorker] = None
        self._optimize_thread: Optional[QThread] = None
        self._optimize_worker: Optional[OptimizeWorker] = None
        self._batch_thread: Optional[QThread] = None
        self._batch_worker: Optional[BatchWorker] = None
        self._pipeline_active = False  # 防止停止后旧信号继续推进流水线

        self._cmp_cells: dict = {}  # entry.index -> (left_label, right_label)
        self._batch_files: list = []
        # 存储各阶段的字幕数据，用于对比查看
        self._stage_data: dict[str, list] = {}  # stage_name -> entries
        self._pipeline_completed = False  # 标记流程是否全部完成

        self.setWindowTitle("字幕优化工具 ✦ Subtitle Optimizer")
        self.resize(1400, 900)
        self._setup_palette()
        self._build_ui()
        self._load_config()
        logger.info("MainWindow初始化完成")

    def _setup_palette(self):
        palette = QPalette()
        bg = QColor("#f8f8f0")
        surface = QColor(247, 243, 223)
        palette.setColor(QPalette.ColorRole.Window, bg)
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#794f27"))
        palette.setColor(QPalette.ColorRole.Base, surface)
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f0e8d8"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#725d42"))
        palette.setColor(QPalette.ColorRole.Button, QColor("#f8f8f0"))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#794f27"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#19c8b9"))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#5a3a1a"))
        self.setPalette(palette)
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
            QTabWidget::pane { border: none; background: #f8f8f0; }
            QTabBar::tab {
                background: transparent;
                color: #8a7b66;
                padding: 10px 28px;
                border: none;
                border-bottom: 2px solid transparent;
                font-size: 13px;
                font-weight: 500;
                margin-right: 4px;
            }
            QTabBar::tab:selected {
                color: #794f27;
                border-bottom: 3px solid #19c8b9;
            }
            QTabBar::tab:hover:!selected { color: #725d42; }
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
                background: rgb(247, 243, 223);
                border-bottom: 2px solid #c4b89e;
                min-height: 56px; max-height: 56px;
            }
        """)
        layout = QHBoxLayout(w)
        layout.setContentsMargins(20, 0, 20, 0)
        layout.setSpacing(0)

        icon_lbl = QLabel("◆")
        icon_lbl.setStyleSheet("color: #19c8b9; font-size: 18px; margin-right: 10px;")
        layout.addWidget(icon_lbl)

        title = QLabel("字幕优化工具")
        title.setObjectName("label_title")
        layout.addWidget(title)

        sep = QLabel("·")
        sep.setStyleSheet("color: #c4b89e; font-size: 16px; margin: 0 12px;")
        layout.addWidget(sep)

        subtitle = QLabel("Subtitle Optimizer  /  Mistral AI")
        subtitle.setStyleSheet("color: #8a7b66; font-size: 12px; font-weight: 400;")
        layout.addWidget(subtitle)
        layout.addStretch()

        self.lbl_stat = QLabel("未加载文件")
        self.lbl_stat.setObjectName("label_stat")
        self.lbl_stat.setStyleSheet("""
            color: #8a7b66;
            font-size: 12px;
            background: #f8f8f0;
            border: 2px solid #c4b89e;
            border-radius: 12px;
            padding: 3px 12px;
        """)
        layout.addWidget(self.lbl_stat)

        return w

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setMinimumWidth(360)
        w.setMaximumWidth(500)
        w.setStyleSheet("QWidget { background: #f8f8f0; }")
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 16, 8, 16)
        layout.setSpacing(12)

        # API 配置提示
        api_hint = QFrame()
        api_hint.setStyleSheet("""
            QFrame {
                background: rgb(247, 243, 223);
                border: 2px solid #c4b89e;
                border-radius: 8px;
            }
        """)
        ah = QHBoxLayout(api_hint)
        ah.setContentsMargins(12, 10, 12, 10)
        ah.setSpacing(10)
        icon_lbl2 = QLabel("⚙")
        icon_lbl2.setStyleSheet("color: #19c8b9; font-size: 16px;")
        ah.addWidget(icon_lbl2)
        hint_text = QLabel("API Key、模型、并发数等\n请前往「⚙ API 设置」页面配置")
        hint_text.setStyleSheet("color: #8a7b66; font-size: 12px; line-height: 1.5;")
        ah.addWidget(hint_text, 1)
        btn_goto_api = QPushButton("前往配置")
        btn_goto_api.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: 1px solid #388BFD;
                border-radius: 6px;
                color: #19c8b9;
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
            color: #8a7b66;
            font-size: 12px;
            background: rgb(247, 243, 223);
            border: 2px solid #c4b89e;
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
                background: #f8f8f0;
                border: 2px solid #c4b89e;
                border-radius: 6px;
                color: #725d42;
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
        self.lbl_progress_detail.setStyleSheet("color: #8a7b66; font-size: 11px;")
        og.addWidget(self.lbl_progress_detail)

        # 阶段指示器
        stage_row = QHBoxLayout()
        stage_row.setSpacing(4)
        self._stage_labels = []
        stage_names = ["① 分析语境", "② 智能重组", "③ AI翻译", "④ 优化润色"]
        for name in stage_names:
            lbl = QLabel(name)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("""
                color: #8a7b66;
                font-size: 11px;
                background: rgb(247, 243, 223);
                border: 2px solid #c4b89e;
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
        self.btn_optimize.setMinimumHeight(36)
        self.btn_optimize.clicked.connect(self._run_optimize)
        btn_row.addWidget(self.btn_optimize, 3)

        self.btn_stop = QPushButton("⏹  停止")
        self.btn_stop.setObjectName("btn_danger")
        self.btn_stop.setMinimumHeight(36)
        self.btn_stop.clicked.connect(self._stop_optimize)
        self.btn_stop.setEnabled(False)
        # 初始禁用时更暗淡，避免误操作
        self.btn_stop.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: 1px solid #3D2020;
                color: #5A3030;
                border-radius: 6px;
                font-size: 13px;
                font-weight: 500;
            }
        """)
        btn_row.addWidget(self.btn_stop, 2)
        og.addLayout(btn_row)

        # Export area
        export_frame = QFrame()
        export_frame.setStyleSheet("""
            QFrame {
                background: rgb(247, 243, 223);
                border: 2px solid #c4b89e;
                border-radius: 6px;
                padding: 2px;
            }
        """)
        ef = QVBoxLayout(export_frame)
        ef.setContentsMargins(8, 8, 8, 8)
        ef.setSpacing(6)

        self.chk_overwrite_single = QCheckBox("覆盖原文件（不加 _optimized 后缀）")
        self.chk_overwrite_single.setStyleSheet("font-size: 12px; color: #8a7b66;")
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

        # 对比选择器 - 左右分离布局
        selector_row = QWidget()
        selector_row.setFixedHeight(44)
        selector_row.setStyleSheet("""
            background: rgb(247, 243, 223);
            border-bottom: 2px solid #c4b89e;
        """)
        sr = QHBoxLayout(selector_row)
        sr.setContentsMargins(12, 4, 12, 4)
        sr.setSpacing(0)

        # 左侧选择器（靠左）
        self._cmp_left_combo = NoWheelComboBox()
        self._cmp_left_combo.setMinimumWidth(120)
        self._cmp_left_combo.setStyleSheet("""
            QComboBox {
                color: #8a7b66; font-size: 12px; font-weight: 700;
                background: transparent; border: none; padding: 4px 8px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox:hover { color: #794f27; }
        """)
        self._cmp_left_combo.addItems(["原始字幕", "阶段2 重组", "阶段3 翻译", "阶段4 优化"])
        self._cmp_left_combo.setCurrentIndex(0)
        self._cmp_left_combo.setEnabled(False)
        self._cmp_left_combo.currentIndexChanged.connect(self._on_compare_selection_changed)
        sr.addWidget(self._cmp_left_combo)

        # 中间弹性空间
        sr.addStretch()

        # 右侧选择器（靠右）
        self._cmp_right_combo = NoWheelComboBox()
        self._cmp_right_combo.setMinimumWidth(120)
        self._cmp_right_combo.setStyleSheet("""
            QComboBox {
                color: #3FB950; font-size: 12px; font-weight: 700;
                background: transparent; border: none; padding: 4px 8px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox:hover { color: #2ea043; }
        """)
        self._cmp_right_combo.addItems(["原始字幕", "阶段2 重组", "阶段3 翻译", "阶段4 优化"])
        self._cmp_right_combo.setCurrentIndex(3)
        self._cmp_right_combo.setEnabled(False)
        self._cmp_right_combo.currentIndexChanged.connect(self._on_compare_selection_changed)
        sr.addWidget(self._cmp_right_combo)

        compare_layout.addWidget(selector_row)

        self._cmp_scroll = QScrollArea()
        self._cmp_scroll.setWidgetResizable(True)
        self._cmp_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._cmp_scroll.setStyleSheet("QScrollArea { border: none; background: #f8f8f0; }")

        self._cmp_container = QWidget()
        self._cmp_container.setStyleSheet("background: #f8f8f0;")
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
                background: #f8f8f0;
                color: #8a7b66;
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
        w.setStyleSheet("QWidget { background: #f8f8f0; }")
        outer = QVBoxLayout(w)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(16)

        title_row = QHBoxLayout()
        icon_l = QLabel("⚙")
        icon_l.setStyleSheet("color: #19c8b9; font-size: 20px;")
        title_row.addWidget(icon_l)
        title_l = QLabel("API 设置")
        title_l.setStyleSheet("color: #794f27; font-size: 16px; font-weight: 700; margin-left: 8px;")
        title_row.addWidget(title_l)
        title_row.addStretch()
        save_tip = QLabel("配置自动保存至本地，下次启动恢复。")
        save_tip.setStyleSheet("color: #8a7b66; font-size: 11px;")
        title_row.addWidget(save_tip)
        btn_save_cfg = QPushButton("  保存配置")
        btn_save_cfg.setObjectName("btn_primary")
        btn_save_cfg.clicked.connect(self._save_config)
        btn_save_cfg.setFixedHeight(32)
        title_row.addWidget(btn_save_cfg)
        outer.addLayout(title_row)

        sep_line = QFrame()
        sep_line.setFrameShape(QFrame.Shape.HLine)
        sep_line.setStyleSheet("color: #c4b89e;")
        outer.addWidget(sep_line)

        cols = QHBoxLayout()
        cols.setSpacing(16)
        outer.addLayout(cols, 1)

        left_card = QFrame()
        left_card.setStyleSheet("""
            QFrame {
                background: rgb(247, 243, 223);
                border: 2px solid #c4b89e;
                border-radius: 10px;
            }
        """)
        lc = QVBoxLayout(left_card)
        lc.setContentsMargins(24, 20, 24, 20)
        lc.setSpacing(14)

        def _section_lbl(text):
            lb = QLabel(text)
            lb.setStyleSheet("color: #8a7b66; font-size: 11px; font-weight: 600; letter-spacing: 0.5px;")
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
            QPushButton { background: #f8f8f0; border: 2px solid #c4b89e;
                border-radius: 6px; color: #8a7b66; padding: 4px 8px; font-size: 11px; }
            QPushButton:hover { border-color: #8a7b66; color: #725d42; }
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

        lc.addWidget(_section_lbl("模型  （可下拉选择或直接输入自定义名称）"))
        self.combo_model = NoWheelComboBox()
        self.combo_model.setEditable(True)
        self.combo_model.addItems([
            "mistral-large-latest",
            "mistral-medium-latest",
            "mistral-small-latest",
            "open-mistral-7b",
        ])
        self.combo_model.setPlaceholderText("输入或选择模型名称")
        lc.addWidget(self.combo_model)

        lc.addWidget(_section_lbl("性能参数"))
        params_row = QHBoxLayout()
        params_row.setSpacing(12)

        conc_wrap = QVBoxLayout()
        conc_lbl = QLabel("并发数  (1 – 12)")
        conc_lbl.setStyleSheet("color: #8a7b66; font-size: 11px;")
        conc_wrap.addWidget(conc_lbl)
        self.input_concurrency = QLineEdit("6")
        self.input_concurrency.setPlaceholderText("1-12")
        self.input_concurrency.setValidator(__import__('PyQt6.QtGui', fromlist=['QIntValidator']).QIntValidator(1, 12))
        conc_wrap.addWidget(self.input_concurrency)
        params_row.addLayout(conc_wrap)

        batch_wrap = QVBoxLayout()
        batch_lbl2 = QLabel("批大小  (1 – 50)")
        batch_lbl2.setStyleSheet("color: #8a7b66; font-size: 11px;")
        batch_wrap.addWidget(batch_lbl2)
        self.input_batch = QLineEdit("5")
        self.input_batch.setPlaceholderText("1-50")
        self.input_batch.setValidator(__import__('PyQt6.QtGui', fromlist=['QIntValidator']).QIntValidator(1, 50))
        batch_wrap.addWidget(self.input_batch)
        params_row.addLayout(batch_wrap)
        lc.addLayout(params_row)

        lc.addWidget(_section_lbl("原文行数"))
        order_row = QHBoxLayout()
        order_row.setSpacing(12)
        order_desc = QLabel("原文在第几行：")
        order_desc.setStyleSheet("color: #8a7b66; font-size: 12px;")
        order_row.addWidget(order_desc)
        self.combo_original_line = NoWheelComboBox()
        self.combo_original_line.addItems(["第1行", "第2行", "第3行"])
        self.combo_original_line.setToolTip("双语字幕中，原文（如英文）所在的行号")
        order_row.addWidget(self.combo_original_line, 1)
        order_hint = QLabel("仅用于输入解析，不影响输出顺序")
        order_hint.setStyleSheet("color: #8a7b66; font-size: 11px;")
        order_row.addWidget(order_hint)
        lc.addLayout(order_row)

        lc.addWidget(_section_lbl("语言设置"))
        lang_row = QHBoxLayout()
        lang_row.setSpacing(12)

        # 输入语言
        in_lang_wrap = QVBoxLayout()
        in_lang_lbl = QLabel("输入语言")
        in_lang_lbl.setStyleSheet("color: #8a7b66; font-size: 11px;")
        in_lang_wrap.addWidget(in_lang_lbl)
        self.combo_input_lang = NoWheelComboBox()
        self.combo_input_lang.addItems(LANGUAGE_LABELS)
        self.combo_input_lang.setCurrentText("English")
        in_lang_wrap.addWidget(self.combo_input_lang)
        lang_row.addLayout(in_lang_wrap)

        # 输出语言
        out_lang_wrap = QVBoxLayout()
        out_lang_lbl = QLabel("输出语言")
        out_lang_lbl.setStyleSheet("color: #8a7b66; font-size: 11px;")
        out_lang_wrap.addWidget(out_lang_lbl)
        self.combo_output_lang = NoWheelComboBox()
        self.combo_output_lang.addItems(LANGUAGE_LABELS)
        self.combo_output_lang.setCurrentText("中文")
        out_lang_wrap.addWidget(self.combo_output_lang)
        lang_row.addLayout(out_lang_wrap)

        lc.addLayout(lang_row)

        lc.addStretch()
        cols.addWidget(left_card, 1)

        right_card = QFrame()
        right_card.setStyleSheet("""
            QFrame {
                background: rgb(247, 243, 223);
                border: 2px solid #c4b89e;
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
            "color: #8a7b66; font-size: 11px; font-weight: 600; letter-spacing: 0.5px; margin-left: 6px;")
        gloss_title_row.addWidget(gloss_lbl)
        gloss_title_row.addStretch()
        rc.addLayout(gloss_title_row)

        gloss_hint = QLabel("每行一条，格式：英文->中文　例：Algorithm->算法")
        gloss_hint.setStyleSheet("color: #8a7b66; font-size: 11px;")
        rc.addWidget(gloss_hint)

        self.input_glossary = QTextEdit()
        self.input_glossary.setPlaceholderText(
            "Algorithm->算法\nCSID->交付状态变化\nDeployment->部署\nPipeline->流水线\n..."
        )
        self.input_glossary.setStyleSheet("""
            QTextEdit {
                background: #f8f8f0;
                border: 2px solid #c4b89e;
                border-radius: 6px;
                color: #725d42;
                font-size: 12px;
                font-family: 'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace;
                padding: 8px;
                line-height: 1.7;
            }
            QTextEdit:focus { border-color: #19c8b9; }
        """)
        rc.addWidget(self.input_glossary, 1)

        gloss_note = QLabel("翻译时 AI 将强制使用上述术语，适用于单文件和批量处理。")
        gloss_note.setStyleSheet("color: #8a7b66; font-size: 11px;")
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
        note.setStyleSheet("color: #8a7b66; font-size: 11px; margin-top: 2px;")
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
        overall_lbl.setStyleSheet("color: #8a7b66; font-size: 11px; font-weight: 600;")
        pg.addWidget(overall_lbl)
        self.batch_overall_bar = QProgressBar()
        self.batch_overall_bar.setFormat("文件  %v / %m  (%p%)")
        pg.addWidget(self.batch_overall_bar)

        cur_lbl = QLabel("当前文件")
        cur_lbl.setStyleSheet("color: #8a7b66; font-size: 11px; font-weight: 600; margin-top: 4px;")
        pg.addWidget(cur_lbl)
        self.batch_cur_bar = QProgressBar()
        self.batch_cur_bar.setFormat("%v / %m 条  (%p%)")
        self.batch_cur_bar.setStyleSheet("""
            QProgressBar { background: #f8f8f0; border: none; border-radius: 4px;
                text-align: center; color: #8a7b66; font-size: 11px;
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
                background: #f8f8f0;
                color: #8a7b66;
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

        # ✅ 防止重复点击导致旧 worker 信号触发闪退
        self._cleanup_batch_pipeline()

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
            target_first=self._target_first(),
            glossary_prompt=self._get_glossary_prompt(),
            original_line=self._get_original_line_index(),
            input_lang=self.combo_input_lang.currentText(),
            output_lang=self.combo_output_lang.currentText(),
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

    def _cleanup_worker_thread(self, worker, thread, timeout_ms: int = 5000):
        """安全清理 worker 和 thread：取消任务 → 退出线程 → 等待结束"""
        if worker is not None:
            try:
                worker.cancel()
            except Exception:
                pass
            # 断开该 worker 与 MainWindow 的所有信号连接
            try:
                worker.disconnect()
            except Exception:
                pass
        if thread is not None:
            try:
                if thread.isRunning():
                    thread.quit()
                    thread.wait(timeout_ms)
            except RuntimeError:
                # thread 已经被删除，忽略错误
                pass

    def _cleanup_batch_pipeline(self):
        self._cleanup_worker_thread(self._batch_worker, self._batch_thread)
        self._batch_worker = None
        self._batch_thread = None

    def _cleanup_single_pipeline(self):
        self._cleanup_worker_thread(self._analysis_worker, self._analysis_thread)
        self._analysis_worker = None
        self._analysis_thread = None
        self._cleanup_worker_thread(self._segment_worker, self._segment_thread)
        self._segment_worker = None
        self._segment_thread = None
        self._cleanup_worker_thread(self._translate_worker, self._translate_thread)
        self._translate_worker = None
        self._translate_thread = None
        self._cleanup_worker_thread(self._optimize_worker, self._optimize_thread)
        self._optimize_worker = None
        self._optimize_thread = None

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
        # 清理 batch worker 和 thread，防止下次启动时闪退
        self._cleanup_batch_pipeline()

    def _get_client(self) -> Optional[MistralClient]:
        key = self.input_apikey.text().strip()
        if not key:
            QMessageBox.warning(self, "提示", "请先输入 Mistral API Key")
            return None
        base_url = self.input_base_url.text().strip()
        return MistralClient(key, self.combo_model.currentText(), base_url)

    def _target_first(self) -> bool:
        # 输出顺序：默认中文在前
        return True

    def _get_original_line_index(self) -> int:
        """获取原文行号索引（0-based）"""
        return self.combo_original_line.currentIndex()

    def _extract_original_line(self, entries: list[SubtitleEntry]) -> list[SubtitleEntry]:
        """根据设置提取原文行，将双语字幕转换为单语言字幕"""
        line_idx = self._get_original_line_index()

        extracted = []
        for entry in entries:
            if len(entry.lines) > 1:
                # 双语字幕：提取指定行
                if line_idx < len(entry.lines):
                    original_text = entry.lines[line_idx]
                    new_entry = SubtitleEntry(
                        entry.index, entry.start_time, entry.end_time,
                        [original_text]
                    )
                    new_entry.original_lines = list(entry.original_lines or entry.lines)
                    extracted.append(new_entry)
                else:
                    # 设置的行号超出范围，保留原样
                    extracted.append(entry)
            else:
                # 单语字幕：直接保留
                extracted.append(entry)

        return extracted

    def _open_file(self):
        logger.info("开始打开字幕文件")
        path, _ = QFileDialog.getOpenFileName(
            self, "打开字幕文件", "", "SRT 字幕文件 (*.srt);;所有文件 (*)"
        )
        if not path:
            logger.info("用户取消文件选择")
            return
        try:
            logger.debug(f"尝试读取文件: {path}")
            with open(path, "r", encoding="utf-8-sig") as f:
                content = f.read()
            logger.info(f"文件读取成功，开始解析SRT内容")
            self.entries = parse_srt(content)

            # 清理旧的 worker/thread，防止闪退
            self._cleanup_single_pipeline()
            # 重置阶段数据和对比选择状态
            self._stage_data.clear()
            self._pipeline_completed = False
            self._cmp_left_combo.setEnabled(False)
            self._cmp_right_combo.setEnabled(False)
            self._cmp_left_combo.setCurrentIndex(0)
            self._cmp_right_combo.setCurrentIndex(3)

            # 自动检测双语字幕并提取原文行
            if self.entries and len(self.entries[0].lines) > 1:
                original_count = len(self.entries)
                self.entries = self._extract_original_line(self.entries)
                line_num = self._get_original_line_index() + 1
                self._log(f"📋 检测到双语字幕（{original_count}条），已提取原文行（第{line_num}行）")

            self._current_file_path = path
            self.lbl_file.setText(f"📄 {Path(path).name}")
            self.tab_original.setPlainText(content)
            n = len(self.entries)
            self.lbl_stat.setText(f"  {n} 条字幕")
            # 保存原始字幕阶段数据
            self._save_stage_data("原始字幕", self.entries)
            self.lbl_stat.setStyleSheet("""
                color: #3FB950; font-size: 12px; background: #12261E;
                border: 1px solid #238636; border-radius: 12px; padding: 3px 12px;
            """)
            self.progress_bar.setMaximum(n)
            self.progress_bar.setValue(0)
            logger.info(f"成功加载文件: {path}，共 {n} 条字幕")
            self._log(f"✅ 已加载：{path}，共 {n} 条字幕")
            self.status_bar.showMessage(f"已加载 {n} 条字幕 · {Path(path).name}")
        except Exception as e:
            error_msg = f"无法读取文件: {e}"
            logger.error(error_msg, exc_info=True)  # 添加完整的异常信息
            QMessageBox.critical(self, "错误", error_msg)

    def _run_analysis(self):
        logger.info("开始执行翻译情景分析")
        if not self.entries:
            msg = "请先加载字幕文件"
            logger.warning(msg)
            QMessageBox.warning(self, "提示", msg)
            return
        client = self._get_client()
        if not client:
            logger.warning("客户端获取失败")
            return

        logger.info("开始分析翻译情景")
        self._log("🔍 开始分析翻译情景...")
        self.text_context.setPlainText("分析中...")

        self._analysis_worker = AnalysisWorker(
            client, self.entries,
            input_lang=self.combo_input_lang.currentText(),
            output_lang=self.combo_output_lang.currentText()
        )
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
        logger.debug("启动分析线程")
        self._analysis_thread.start()

    def _on_analysis_done(self, result: str):
        logger.info("翻译情景分析完成")
        self.context_text = result
        self.text_context.setPlainText(result)
        self._log("✅ 情景分析完成")

    def _run_optimize(self):
        logger.info("开始执行字幕优化流程")
        if not self.entries:
            msg = "请先加载字幕文件"
            logger.warning(msg)
            QMessageBox.warning(self, "提示", msg)
            return
        client = self._get_client()
        if not client:
            logger.warning("客户端获取失败")
            return

        # ✅ 防止重复点击导致旧 worker 信号触发闪退
        self._cleanup_single_pipeline()

        # 重置对比选择状态
        self._stage_data.clear()
        self._pipeline_completed = False
        self._cmp_left_combo.setEnabled(False)
        self._cmp_right_combo.setEnabled(False)
        self._cmp_left_combo.setCurrentIndex(0)
        self._cmp_right_combo.setCurrentIndex(3)

        logger.debug("设置运行状态")
        self._set_running_state(True)
        self._pipeline_active = True

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

        logger.info("开始第一阶段：分析翻译情景")
        self._log("🔍 [阶段1/4] 开始分析翻译情景...")
        self.text_context.setPlainText("分析中...")

        self._analysis_worker = AnalysisWorker(
            client, self.entries,
            input_lang=self.combo_input_lang.currentText(),
            output_lang=self.combo_output_lang.currentText()
        )
        self._analysis_thread = QThread()
        self._analysis_worker.moveToThread(self._analysis_thread)
        self._analysis_thread.started.connect(self._analysis_worker.run)
        self._analysis_worker.progress.connect(self._log)
        self._analysis_worker.finished.connect(lambda result: self._after_analysis(result, client))
        self._analysis_worker.finished.connect(self._analysis_thread.quit)
        self._analysis_worker.error.connect(lambda e: self._on_pipeline_error(f"分析失败：{e}"))
        self._analysis_worker.error.connect(self._analysis_thread.quit)
        self._analysis_thread.finished.connect(self._analysis_thread.deleteLater)
        logger.debug("启动分析线程")
        self._analysis_thread.start()

    def _after_analysis(self, result: str, client: MistralClient):
        if not self._pipeline_active:
            return
        logger.info("情景分析完成，进入第二阶段：智能重组")
        self.context_text = result
        self.text_context.setPlainText(result)
        self._log("✅ [阶段1/4] 情景分析完成")

        concurrency = max(1, min(12, int(self.input_concurrency.text() or "6")))
        batch_size = max(1, min(50, int(self.input_batch.text() or "5")))

        seg_batch_size = max(10, min(30, int(self.input_batch.text() or "15")))
        glossary_prompt = self._get_glossary_prompt()

        logger.info(f"开始智能重组 {len(self.entries)} 条字幕")
        self._log(f"🔗 [阶段2/4] 开始智能重组 {len(self.entries)} 条字幕...")
        self._set_stage(1)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(len(self.entries))
        self.lbl_progress_detail.setText(f"智能重组：0/{len(self.entries)}")

        self._segment_worker = SegmentWorker(
            client, self.entries, result, glossary_prompt, concurrency, seg_batch_size,
            input_lang=self.combo_input_lang.currentText(),
            output_lang=self.combo_output_lang.currentText()
        )
        self._segment_thread = QThread()
        self._segment_worker.moveToThread(self._segment_thread)
        self._segment_thread.started.connect(self._segment_worker.run)
        self._segment_worker.progress.connect(self._on_segment_progress)
        self._segment_worker.log.connect(self._log)
        self._segment_worker.all_done.connect(
            lambda new_entries: self._after_segment(new_entries, client, concurrency, batch_size)
        )
        self._segment_worker.all_done.connect(self._segment_thread.quit)
        self._segment_worker.error.connect(lambda e: self._on_pipeline_error(f"重组失败：{e}"))
        self._segment_thread.finished.connect(self._segment_thread.deleteLater)
        logger.debug("启动重组线程")
        self._segment_thread.start()

    def _on_segment_progress(self, current: int, total: int):
        self.progress_bar.setValue(current)
        self.lbl_progress_detail.setText(f"智能重组：{current}/{total}")

    def _after_segment(self, new_entries: list, client: MistralClient, concurrency: int, batch_size: int):
        if not self._pipeline_active:
            return
        old_count = len(self.entries)
        self.entries = new_entries

        logger.info(f"_after_segment: 条目数 {old_count} -> {len(self.entries)}")
        blocks = [e.to_srt_block_raw() for e in self.entries]
        self.tab_original.setPlainText("\n".join(blocks))
        self.lbl_stat.setText(f"  {len(self.entries)} 条字幕")
        self.progress_bar.setMaximum(len(self.entries))
        self._refresh_compare()

        if self._current_file_path:
            write_phase_snapshot(self._current_file_path, "AFTER_SEGMENT", self.entries)
            logger.info(_format_subtitle_snapshot("AFTER_SEGMENT", self.entries))
        # 保存阶段2数据
        self._save_stage_data("阶段2 重组", self.entries)
        logger.info(f"重组完成：{old_count} → {len(new_entries)} 条字幕")
        self._log(f"✅ [阶段2/4] 重组完成：{old_count} → {len(new_entries)} 条字幕")
        QTimer.singleShot(100, lambda: self._start_translate_stage(client, concurrency, batch_size))

    def _start_translate_stage(self, client: MistralClient, concurrency: int, batch_size: int):
        logger.info("开始第三阶段：AI翻译")
        base_context = self.text_context.toPlainText().strip() or "通用教育视频字幕"
        glossary_prompt = self._get_glossary_prompt()

        self._log(f"🌐 [阶段3/4] 开始AI翻译 {len(self.entries)} 条字幕 · 并发:{concurrency} · 批:{batch_size}")
        self._set_stage(2)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(len(self.entries))
        self.lbl_progress_detail.setText(f"AI翻译：0/{len(self.entries)}")

        self._translate_worker = TranslateWorker(
            client, self.entries, base_context, glossary_prompt, concurrency, batch_size,
            input_lang=self.combo_input_lang.currentText(),
            output_lang=self.combo_output_lang.currentText()
        )
        self._translate_thread = QThread()
        self._translate_worker.moveToThread(self._translate_thread)
        self._translate_thread.started.connect(self._translate_worker.run)
        self._translate_worker.entry_done.connect(self._on_translate_entry_done)
        self._translate_worker.progress.connect(self._on_translate_progress)
        self._translate_worker.all_done.connect(
            lambda: self._after_translate(client, concurrency, batch_size))
        self._translate_worker.all_done.connect(self._translate_thread.quit)
        self._translate_worker.error.connect(lambda e: self._log(f"❌ {e}"))
        self._translate_thread.finished.connect(self._translate_thread.deleteLater)
        logger.debug("启动翻译线程")
        self._translate_thread.start()

    def _on_translate_entry_done(self, idx: int, lines: list, status: str):
        entry = next((e for e in self.entries if e.index == idx), None)
        if entry:
            entry.lines = lines
            entry.status = status

    def _on_translate_progress(self, current: int, total: int):
        self.progress_bar.setValue(current)
        self.lbl_progress_detail.setText(f"AI翻译：{current}/{total}")

    def _after_translate(self, client: MistralClient, concurrency: int, batch_size: int):
        if not self._pipeline_active:
            return
        if self._current_file_path:
            write_phase_snapshot(self._current_file_path, "AFTER_TRANSLATE", self.entries)
            logger.info(_format_subtitle_snapshot("AFTER_TRANSLATE", self.entries))
        # 保存阶段3数据
        self._save_stage_data("阶段3 翻译", self.entries)
        translated = sum(1 for e in self.entries if e.status == "done")
        failed = sum(1 for e in self.entries if e.status == "error")
        if failed > 0:
            self._log(f"✅ [阶段3/4] 翻译完成：{translated} 条成功，{failed} 条失败")
        else:
            self._log(f"✅ [阶段3/4] 翻译完成：{translated} 条")
        QTimer.singleShot(100, lambda: self._start_optimize_stage(client, concurrency, batch_size))

    def _start_optimize_stage(self, client: MistralClient, concurrency: int, batch_size: int):
        logger.info("开始第四阶段：优化润色")
        base_context = self.text_context.toPlainText().strip() or "通用教育视频字幕"
        glossary_prompt = self._get_glossary_prompt()

        if glossary_prompt:
            logger.info("已加载固定术语表规则")
            self._log(f"📖 已加载固定术语表规则")

        logger.info(f"开始优化润色 {len(self.entries)} 条字幕，参数：并发={concurrency}，批次={batch_size}")
        self._log(f"✨ [阶段4/4] 开始优化润色 {len(self.entries)} 条字幕 · 并发:{concurrency} · 批:{batch_size}")
        self._set_stage(3)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(len(self.entries))

        self._optimize_worker = OptimizeWorker(client, self.entries, base_context, glossary_prompt, concurrency,
                                               batch_size,
                                               input_lang=self.combo_input_lang.currentText(),
                                               output_lang=self.combo_output_lang.currentText())
        self._optimize_thread = QThread()
        self._optimize_worker.moveToThread(self._optimize_thread)
        self._optimize_thread.started.connect(self._optimize_worker.run)
        self._optimize_worker.entry_done.connect(self._on_entry_done)
        self._optimize_worker.progress.connect(self._on_progress)
        self._optimize_worker.all_done.connect(self._on_optimize_done)
        self._optimize_worker.all_done.connect(self._optimize_thread.quit)
        self._optimize_worker.error.connect(lambda e: self._log(f"❌ {e}"))
        self._optimize_thread.finished.connect(self._optimize_thread.deleteLater)
        logger.debug("启动优化线程")
        self._optimize_thread.start()

    def _on_pipeline_error(self, msg: str):
        self._pipeline_active = False
        self._log(f"❌ {msg}")
        self._set_running_state(False)
        self._set_stage(-1)

    def _set_stage(self, active: int):
        active_style = "color: #5a3a1a; font-size: 11px; font-weight: 700; background: #19c8b9; border: 2px solid #11a89b; border-radius: 12px; padding: 3px 8px;"
        done_style = "color: #6fba2c; font-size: 11px; font-weight: 600; background: #e6f9f6; border: 2px solid #82d5bb; border-radius: 12px; padding: 3px 8px;"
        idle_style = "color: #8a7b66; font-size: 11px; background: rgb(247, 243, 223); border: 2px solid #c4b89e; border-radius: 12px; padding: 3px 8px;"
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
        self._pipeline_active = False
        if self._current_file_path:
            write_phase_snapshot(self._current_file_path, "AFTER_OPTIMIZE", self.entries, use_optimized=True)
            logger.info(_format_subtitle_snapshot("AFTER_OPTIMIZE", self.entries, use_optimized=True))
        # 保存阶段4数据
        self._save_stage_data("阶段4 优化", self.entries)
        logger.info("字幕优化完成")
        done = sum(1 for e in self.entries if e.status == "done")
        err = sum(1 for e in self.entries if e.status == "error")
        self._set_running_state(False)
        logger.info(f"优化统计：成功 {done} 条，失败 {err} 条")
        self._log(f"✅ [阶段4/4] 优化润色完成：成功 {done} 条，失败 {err} 条")
        self.status_bar.showMessage(f"全部完成：{done} 成功，{err} 失败")
        for lbl in self._stage_labels:
            lbl.setStyleSheet(
                "color: #5a3a1a; font-size: 11px; font-weight: 700; background: #e6f9f6; border: 2px solid #82d5bb; border-radius: 12px; padding: 3px 8px;")
        self._refresh_compare()
        # 启用对比选择功能
        self._enable_compare_selection()
        # 注意：不在这里清理 worker/thread，因为 thread 的 finished 信号已经连接了 deleteLater
        # 清理会在下次 _run_optimize 或 _open_file 时进行

    def _stop_optimize(self):
        self._pipeline_active = False
        if self._segment_worker: self._segment_worker.cancel()
        if self._translate_worker: self._translate_worker.cancel()
        if self._optimize_worker: self._optimize_worker.cancel()
        self._set_running_state(False)
        self._set_stage(-1)
        self._log("⏹ 已停止")

    @staticmethod
    def _cmp_cell_stylesheet(is_opt: bool = False, changed: bool = False) -> str:
        """纯函数：返回对比单元格的 QSS 样式字符串（不创建 widget）"""
        if changed:
            bg, border_color, color = ("#e6f9f6", "#6fba2c", "#5a9e1e") if is_opt else ("#fef0f0", "#e05a5a", "#c94444")
        else:
            bg, border_color, color = ("rgb(247, 243, 223)", "#c4b89e", "#725d42")
        return f"QLabel {{ background: {bg}; border: 2px solid {border_color}; border-radius: 12px; color: {color}; padding: 6px 10px; font-family: monospace; font-size: 12px; line-height: 1.5; }}"

    def _make_cmp_cell(self, text: str, is_opt: bool = False, changed: bool = False) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(QFont("Consolas", 12))
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        lbl.setContentsMargins(6, 4, 6, 4)
        lbl.setStyleSheet(self._cmp_cell_stylesheet(is_opt, changed))
        return lbl

    def _append_compare_entry(self, entry):
        orig_text = entry.to_srt_block_raw().rstrip("\n")
        opt_text = (
            entry.to_srt_block(True, self._target_first()) if entry.optimized_lines else entry.to_srt_block(False,
                                                                                                             self._target_first())).rstrip(
            "\n")
        changed = entry.optimized_lines is not None and orig_text != opt_text

        sb = self._cmp_scroll.verticalScrollBar()
        at_bottom = (sb.value() >= sb.maximum() - 4) and sb.maximum() > 0
        scroll_val = sb.value()

        if entry.index in self._cmp_cells:
            left_lbl, right_lbl = self._cmp_cells[entry.index]
            left_lbl.setText(orig_text)
            right_lbl.setText(opt_text)
            left_lbl.setStyleSheet(self._cmp_cell_stylesheet(False, changed))
            right_lbl.setStyleSheet(self._cmp_cell_stylesheet(True, changed))
        else:
            row = self._cmp_grid.rowCount()
            left_lbl = self._make_cmp_cell(orig_text, False, changed)
            right_lbl = self._make_cmp_cell(opt_text, True, changed)
            self._cmp_grid.addWidget(left_lbl, row, 0)
            self._cmp_grid.addWidget(right_lbl, row, 1)
            self._cmp_cells[entry.index] = (left_lbl, right_lbl)

        if at_bottom:
            QTimer.singleShot(50, lambda: sb.setValue(sb.maximum()))
        else:
            sb.setValue(scroll_val)

    def _save_stage_data(self, stage_name: str, entries: list):
        """保存阶段数据，用于对比查看"""
        self._stage_data[stage_name] = [
            {
                'index': e.index,
                'start_time': e.start_time,
                'end_time': e.end_time,
                'lines': list(e.lines),
                'original_lines': list(e.original_lines) if e.original_lines else None,
                'optimized_lines': list(e.optimized_lines) if e.optimized_lines else None,
            }
            for e in entries
        ]
        logger.info(f"已保存阶段数据: {stage_name}, {len(entries)} 条")

    def _enable_compare_selection(self):
        """启用对比选择下拉框（流程全部完成后调用）"""
        self._pipeline_completed = True
        self._cmp_left_combo.setEnabled(True)
        self._cmp_right_combo.setEnabled(True)
        logger.info("对比选择已启用")

    def _on_compare_selection_changed(self):
        """对比选择改变时更新视图"""
        if not self._pipeline_completed:
            return
        self._refresh_compare_with_selection()

    def _refresh_compare_with_selection(self):
        """根据选择的阶段刷新对比视图"""
        left_idx = self._cmp_left_combo.currentIndex()
        right_idx = self._cmp_right_combo.currentIndex()

        # 获取左侧数据
        left_entries = self._get_entries_for_stage(left_idx)
        # 获取右侧数据
        right_entries = self._get_entries_for_stage(right_idx)

        # 清空并重建对比视图
        while self._cmp_grid.count():
            item = self._cmp_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cmp_cells.clear()

        # 构建对比
        left_map = {e['index']: e for e in left_entries} if left_entries else {}
        right_map = {e['index']: e for e in right_entries} if right_entries else {}

        all_indices = sorted(set(list(left_map.keys()) + list(right_map.keys())))

        for idx in all_indices:
            left_data = left_map.get(idx)
            right_data = right_map.get(idx)

            left_text = self._format_entry_text(left_data, left_idx) if left_data else ""
            right_text = self._format_entry_text(right_data, right_idx) if right_data else ""

            changed = left_text != right_text and left_text and right_text

            row = self._cmp_grid.rowCount()
            left_lbl = self._make_cmp_cell(left_text, False, changed)
            right_lbl = self._make_cmp_cell(right_text, True, changed)
            self._cmp_grid.addWidget(left_lbl, row, 0)
            self._cmp_grid.addWidget(right_lbl, row, 1)
            self._cmp_cells[idx] = (left_lbl, right_lbl)

    def _get_entries_for_stage(self, stage_idx: int) -> list:
        """根据阶段索引获取字幕数据"""
        stage_names = ["原始字幕", "阶段2 重组", "阶段3 翻译", "阶段4 优化"]
        if stage_idx == 0:
            # 原始字幕：使用 original_lines 或 lines
            return self._stage_data.get("原始字幕", [])
        elif stage_idx == 1:
            return self._stage_data.get("阶段2 重组", [])
        elif stage_idx == 2:
            return self._stage_data.get("阶段3 翻译", [])
        elif stage_idx == 3:
            return self._stage_data.get("阶段4 优化", [])
        return []

    def _format_entry_text(self, data: dict, stage_idx: int) -> str:
        """格式化条目文本用于显示"""
        if not data:
            return ""
        # 根据阶段选择正确的文本
        if stage_idx == 0:
            # 原始字幕：优先使用 original_lines
            lines = data.get('original_lines') or data.get('lines', [])
        elif stage_idx == 3:
            # 阶段4优化：优先使用 optimized_lines
            lines = data.get('optimized_lines') or data.get('lines', [])
        else:
            # 其他阶段：使用 lines
            lines = data.get('lines', [])
        text = "\n".join(lines)
        return f"{data['index']}\n{data['start_time']} --> {data['end_time']}\n{text}"

    def _refresh_compare(self):
        while self._cmp_grid.count():
            item = self._cmp_grid.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        self._cmp_cells.clear()
        logger.info(f"_refresh_compare: 正在重建 {len(self.entries)} 条字幕的对比视图")
        for entry in self.entries: self._append_compare_entry(entry)
        logger.info(f"_refresh_compare: 完成，grid行数={self._cmp_grid.rowCount()}, cells数={len(self._cmp_cells)}")

    def _export_srt(self):
        logger.info("开始导出优化后的字幕文件")
        if not self.entries: 
            msg = "没有可导出的字幕"
            logger.warning(msg)
            return QMessageBox.warning(self, "提示", msg)
        has_optimized = sum(1 for e in self.entries if e.optimized_lines) > 0
        has_segmented = any(e.original_lines is not None for e in self.entries)
        if not has_optimized and not has_segmented:
            msg = "没有可导出的内容（请先执行优化或断句重组）"
            logger.warning(msg)
            return QMessageBox.warning(self, "提示", msg)
        if not has_optimized:
            if QMessageBox.question(self, "提示",
                "优化润色尚未完成，是否仅导出断句重组结果？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            ) != QMessageBox.StandardButton.Yes:
                return

        if self.chk_overwrite_single.isChecked() and self._current_file_path:
            path = self._current_file_path
            logger.info(f"用户选择覆盖原文件: {path}")
            if QMessageBox.question(self, "确认覆盖", f"将覆盖：\n{path}",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                logger.info("用户取消覆盖操作")
                return
        else:
            default_name = "optimized.srt"
            if self._current_file_path:
                p = Path(self._current_file_path)
                default_name = p.stem + "_optimized" + p.suffix
            path, _ = QFileDialog.getSaveFileName(self, "保存优化字幕", default_name, "SRT 字幕文件 (*.srt)")
            if not path: 
                logger.info("用户取消保存操作")
                return

        logger.debug("开始生成SRT块内容")
        blocks = [entry.to_srt_block(True, self._target_first()) for entry in self.entries]
        logger.info(f"写入文件: {path}，共 {len(blocks)} 个字幕块")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(blocks))

        logger.info(f"成功导出文件: {path}")
        self._log(f"💾 已导出：{path}")
        QMessageBox.information(self, "导出完成", f"已导出优化字幕：\n{path}")

    def _set_running_state(self, running: bool):
        """切换「运行中 / 空闲」两种按钮视觉状态"""
        if running:
            # 开始优化按钮：变灰禁用，文字改为"优化中…"
            self.btn_optimize.setEnabled(False)
            self.btn_optimize.setText("⏳  优化中…")
            self.btn_optimize.setStyleSheet("""
                QPushButton {
                    background: #f8f8f0;
                    border: 2px solid #c4b89e;
                    color: #8a7b66;
                    border-radius: 6px;
                    font-size: 13px;
                    font-weight: 500;
                    min-height: 36px;
                }
            """)
            # 停止按钮：亮红高亮，醒目可点
            self.btn_stop.setEnabled(True)
            self.btn_stop.setStyleSheet("""
                QPushButton {
                    background: #DA3633;
                    border: 1px solid #FF7B72;
                    color: #FFFFFF;
                    border-radius: 6px;
                    font-size: 13px;
                    font-weight: 700;
                    min-height: 36px;
                }
                QPushButton:hover {
                    background: #FF4A47;
                    border-color: #FF7B72;
                }
                QPushButton:pressed {
                    background: #B91C1C;
                }
            """)
        else:
            # 恢复开始优化按钮
            self.btn_optimize.setEnabled(True)
            self.btn_optimize.setText("▶  开始优化")
            self.btn_optimize.setStyleSheet("")          # 恢复 btn_primary QSS
            self.btn_optimize.setObjectName("btn_primary")
            # 停止按钮：暗淡禁用
            self.btn_stop.setEnabled(False)
            self.btn_stop.setStyleSheet("""
                QPushButton {
                    background: transparent;
                    border: 1px solid #3D2020;
                    color: #5A3030;
                    border-radius: 6px;
                    font-size: 13px;
                    font-weight: 500;
                    min-height: 36px;
                }
            """)

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        if msg.startswith("✅") or msg.startswith("💾") or msg.startswith("🎉"):
            color = "#6fba2c"
        elif msg.startswith("❌"):
            color = "#e05a5a"
        elif msg.startswith("▶") or msg.startswith("✨") or msg.startswith("🔍") or msg.startswith("🔗"):
            color = "#19c8b9"
        elif msg.startswith("⏹") or msg.startswith("⚠"):
            color = "#f5c31c"
        else:
            color = "#8a7b66"
        self.text_log.append(f'<span style="color:#9f927d">[{ts}]</span> <span style="color:{color}">{msg}</span>')

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
            "original_line": self.combo_original_line.currentIndex(),
            "glossary": self.input_glossary.toPlainText().strip(),
            "input_language": self.combo_input_lang.currentText(),
            "output_language": self.combo_output_lang.currentText(),
        }
        try:
            with open(self._config_path(), "w") as f:
                json.dump(cfg, f)
            from datetime import datetime
            current_time = datetime.now().strftime("%H:%M:%S")
            self.status_bar.showMessage(f"✅ 配置已保存 ({current_time})")
        except Exception as e:
            self._log(f"⚠️ 保存配置失败：{e}")

    def _load_config(self):
        try:
            with open(self._config_path()) as f:
                cfg = json.load(f)
            self.input_apikey.setText(cfg.get("api_key", ""))
            self.input_base_url.setText(cfg.get("base_url", ""))
            saved_model = cfg.get("model", "")
            if saved_model:
                idx = self.combo_model.findText(saved_model)
                if idx >= 0:
                    self.combo_model.setCurrentIndex(idx)
                else:
                    self.combo_model.setCurrentText(saved_model)
            self.input_concurrency.setText(str(cfg.get("concurrency", "6")))
            self.input_batch.setText(str(cfg.get("batch_size", "5")))
            self.combo_original_line.setCurrentIndex(cfg.get("original_line", 1))  # 默认第2行
            self.input_glossary.setPlainText(cfg.get("glossary", ""))
            # 加载语言设置（向后兼容）
            input_lang = cfg.get("input_language", "English")
            output_lang = cfg.get("output_language", "中文")
            if input_lang in LANGUAGE_LABELS:
                self.combo_input_lang.setCurrentText(input_lang)
            if output_lang in LANGUAGE_LABELS:
                self.combo_output_lang.setCurrentText(output_lang)
        except Exception:
            pass

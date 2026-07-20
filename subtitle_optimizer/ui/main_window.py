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
    QSizePolicy, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QDialog
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
from core.workers import (
    AnalysisResult,
    AnalysisWorker,
    OptimizeWorker,
    PipelineExecutionWorker,
    SegmentWorker,
    TranslateWorker,
    BatchWorker,
    BatchGlossaryWorker,
    TranscriptionWorker,
    LANGUAGE_MAP,
    LANGUAGE_LABELS,
    format_glossary_merge_log,
    merge_glossaries,
)
from core.transcription import is_media_file, media_file_filter
from core.source_normalization import (
    LAYOUT_AUTO,
    LAYOUT_BILINGUAL,
    LAYOUT_MONOLINGUAL,
    normalize_source_entries,
)
from core.batch_glossary import (
    BatchGlossarySummary,
    GlossaryCandidate,
    approved_candidates_to_text,
    merge_batch_glossary_candidates,
    unresolved_conflict_sources,
)
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


from core.workers import _format_glossary_prompt


def glossary_column_labels(input_lang: str, output_lang: str) -> tuple[str, str]:
    input_lang = (input_lang or "").strip()
    output_lang = (output_lang or "").strip()
    source_label = f"原文（{input_lang}）" if input_lang else "原文"
    target_label = f"译文（{output_lang}）" if output_lang else "译文"
    return source_label, target_label


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        logger.info("MainWindow初始化开始")
        self.entries: list[SubtitleEntry] = []
        self.context_text = ""
        self.auto_glossary_text = ""
        self.client: Optional[MistralClient] = None
        self._current_file_path: Optional[str] = None
        self._pending_media_path: Optional[str] = None
        self._transcription_auto_optimize = False

        # 线程及 Worker 引用保持
        self._analysis_thread: Optional[QThread] = None
        self._analysis_worker: Optional[AnalysisWorker] = None
        self._segment_thread: Optional[QThread] = None
        self._segment_worker: Optional[SegmentWorker] = None
        self._translate_thread: Optional[QThread] = None
        self._translate_worker: Optional[TranslateWorker] = None
        self._optimize_thread: Optional[QThread] = None
        self._optimize_worker: Optional[OptimizeWorker] = None
        self._pipeline_thread: Optional[QThread] = None
        self._pipeline_worker: Optional[PipelineExecutionWorker] = None
        self._transcription_thread: Optional[QThread] = None
        self._transcription_worker: Optional[TranscriptionWorker] = None
        self._batch_thread: Optional[QThread] = None
        self._batch_worker: Optional[BatchWorker] = None
        self._batch_glossary_thread: Optional[QThread] = None
        self._batch_glossary_worker: Optional[BatchGlossaryWorker] = None
        self._batch_glossary_settings: Optional[dict] = None
        self._pipeline_active = False  # 防止停止后旧信号继续推进流水线
        self._last_quality_report = None

        self._batch_files: list = []
        # 存储各阶段的字幕数据，用于对比查看
        self._stage_data: dict[str, list] = {}  # stage_name -> entries

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

        auto_group = QGroupBox("阶段一自动术语")
        ag = QVBoxLayout(auto_group)
        ag.setSpacing(8)
        self.text_auto_glossary = QTextEdit()
        self.text_auto_glossary.setPlaceholderText("阶段一分析后自动填入，可按 source->target 编辑")
        self.text_auto_glossary.setMaximumHeight(110)
        self.text_auto_glossary.setStyleSheet("""
            QTextEdit {
                background: #f8f8f0;
                border: 2px solid #c4b89e;
                border-radius: 6px;
                color: #725d42;
                font-size: 12px;
                font-family: 'JetBrains Mono', 'Consolas', monospace;
                padding: 4px;
            }
        """)
        ag.addWidget(self.text_auto_glossary)
        layout.addWidget(auto_group)

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

        self.btn_export = QPushButton("  导出优化字幕")
        self.btn_export.setObjectName("btn_success")
        self.btn_export.clicked.connect(self._export_srt)
        ef.addWidget(self.btn_export)
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

        # Tab 2: 对比查看 — 逐条对齐表格，适合长字幕审校
        compare_widget = QWidget()
        compare_layout = QVBoxLayout(compare_widget)
        compare_layout.setSpacing(8)
        compare_layout.setContentsMargins(6, 6, 6, 6)

        compare_tools = QHBoxLayout()
        compare_tools.setSpacing(8)
        compare_tools.addWidget(QLabel("阶段"))
        self.combo_compare_stage = NoWheelComboBox()
        self.combo_compare_stage.setMinimumWidth(130)
        self.combo_compare_stage.currentTextChanged.connect(self._refresh_compare)
        compare_tools.addWidget(self.combo_compare_stage)

        self.input_compare_filter = QLineEdit()
        self.input_compare_filter.setPlaceholderText("搜索序号、时间、原文或译文")
        self.input_compare_filter.textChanged.connect(self._refresh_compare)
        compare_tools.addWidget(self.input_compare_filter, 1)

        self.chk_compare_changed_only = QCheckBox("仅显示变化")
        self.chk_compare_changed_only.stateChanged.connect(self._refresh_compare)
        compare_tools.addWidget(self.chk_compare_changed_only)

        self.lbl_compare_stats = QLabel("0 条")
        self.lbl_compare_stats.setStyleSheet("color: #8a7b66; font-size: 11px;")
        compare_tools.addWidget(self.lbl_compare_stats)
        compare_layout.addLayout(compare_tools)

        self.table_compare = QTableWidget()
        self.table_compare.setColumnCount(2)
        self.table_compare.setHorizontalHeaderLabels(["原文", "处理后结果"])
        self.table_compare.setWordWrap(True)
        self.table_compare.setAlternatingRowColors(True)
        self.table_compare.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_compare.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_compare.verticalHeader().setVisible(False)
        self.table_compare.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table_compare.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table_compare.setStyleSheet("""
            QTableWidget {
                background: #f8f8f0;
                alternate-background-color: #f0e8d8;
                color: #5a3a1a;
                border: 1px solid #c4b89e;
                border-radius: 8px;
                gridline-color: #d7ccb6;
                font-size: 12px;
            }
            QHeaderView::section {
                background: #f0e8d8;
                color: #725d42;
                font-weight: 600;
                border: 1px solid #c4b89e;
                padding: 6px;
            }
        """)
        compare_layout.addWidget(self.table_compare, 1)

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

        lc.addWidget(_section_lbl("Transcription model"))
        self.input_transcription_model = QLineEdit("voxtral-mini-latest")
        self.input_transcription_model.setPlaceholderText("voxtral-mini-latest")
        lc.addWidget(self.input_transcription_model)
        self.chk_transcription_wav = QCheckBox("转录提取音频使用 WAV（质量优先，临时文件更大）")
        self.chk_transcription_wav.setToolTip("默认使用 MP3；勾选后视频转录会提取 16k mono WAV 再发送给转录 API。")
        lc.addWidget(self.chk_transcription_wav)

        lc.addWidget(_section_lbl("性能参数"))
        params_row = QHBoxLayout()
        params_row.setSpacing(12)

        conc_wrap = QVBoxLayout()
        conc_lbl = QLabel("并发数  (1 – 100)")
        conc_lbl.setStyleSheet("color: #8a7b66; font-size: 11px;")
        conc_wrap.addWidget(conc_lbl)
        self.input_concurrency = QLineEdit("6")
        self.input_concurrency.setPlaceholderText("1-12")
        self.input_concurrency.setValidator(__import__('PyQt6.QtGui', fromlist=['QIntValidator']).QIntValidator(1, 100))
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

        lc.addWidget(_section_lbl("输入字幕布局"))
        layout_row = QHBoxLayout()
        layout_row.setSpacing(12)
        layout_desc = QLabel("布局识别：")
        layout_desc.setStyleSheet("color: #8a7b66; font-size: 12px;")
        layout_row.addWidget(layout_desc)
        self.combo_source_layout = NoWheelComboBox()
        self.combo_source_layout.addItem("自动识别（推荐）", LAYOUT_AUTO)
        self.combo_source_layout.addItem("单语字幕（合并全部换行）", LAYOUT_MONOLINGUAL)
        self.combo_source_layout.addItem("双语字幕（提取原文行）", LAYOUT_BILINGUAL)
        self.combo_source_layout.setToolTip(
            "自动模式优先防止丢行；同文字体系的双语字幕请手动选择双语布局"
        )
        layout_row.addWidget(self.combo_source_layout, 1)
        lc.addLayout(layout_row)

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
        self.combo_input_lang.currentTextChanged.connect(lambda _text: self._refresh_glossary_headers())
        self.combo_output_lang.currentTextChanged.connect(lambda _text: self._refresh_glossary_headers())

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

        gloss_hint = QLabel("在表格中填写原文词汇和对应译文，点击「添加行」新增术语")
        gloss_hint.setStyleSheet("color: #8a7b66; font-size: 11px;")
        rc.addWidget(gloss_hint)

        # 术语表表格
        self.table_glossary = QTableWidget()
        self.table_glossary.setColumnCount(2)
        source_label, target_label = glossary_column_labels(
            self.combo_input_lang.currentText() if hasattr(self, "combo_input_lang") else "",
            self.combo_output_lang.currentText() if hasattr(self, "combo_output_lang") else "",
        )
        self.table_glossary.setHorizontalHeaderLabels([source_label, target_label])
        self.table_glossary.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_glossary.setStyleSheet("""
            QTableWidget {
                background: #f8f8f0;
                border: 2px solid #c4b89e;
                border-radius: 6px;
                color: #725d42;
                font-size: 12px;
                font-family: 'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace;
                gridline-color: #c4b89e;
            }
            QTableWidget:focus { border-color: #19c8b9; }
            QHeaderView::section {
                background: #f0e8d8;
                color: #8a7b66;
                font-weight: 600;
                border: 1px solid #c4b89e;
                padding: 6px;
            }
        """)
        rc.addWidget(self.table_glossary, 1)

        # 按钮行
        btn_row = QHBoxLayout()
        btn_add = QPushButton("+ 添加行")
        btn_del = QPushButton("- 删除行")
        btn_sort = QPushButton("去重排序")
        btn_add.setStyleSheet("""
            QPushButton {
                background: #19c8b9;
                color: #5a3a1a;
                border: none;
                border-radius: 4px;
                padding: 6px 12px;
                font-weight: 600;
            }
            QPushButton:hover { background: #15b0a3; }
        """)
        btn_del.setStyleSheet("""
            QPushButton {
                background: #e8a87c;
                color: #5a3a1a;
                border: none;
                border-radius: 4px;
                padding: 6px 12px;
                font-weight: 600;
            }
            QPushButton:hover { background: #d4956b; }
        """)
        btn_sort.setStyleSheet("""
            QPushButton {
                background: #f0e8d8;
                color: #5a3a1a;
                border: 1px solid #c4b89e;
                border-radius: 4px;
                padding: 6px 12px;
                font-weight: 600;
            }
            QPushButton:hover { background: #e6dcc8; }
        """)
        btn_add.clicked.connect(self._glossary_add_row)
        btn_del.clicked.connect(self._glossary_del_row)
        btn_sort.clicked.connect(self._glossary_normalize)
        btn_row.addWidget(btn_add)
        btn_row.addWidget(btn_del)
        btn_row.addWidget(btn_sort)
        btn_row.addStretch()
        rc.addLayout(btn_row)

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

        self.chk_batch_glossary_review = QCheckBox("手动审核术语")
        self.chk_batch_glossary_review.setChecked(False)
        self.chk_batch_glossary_review.setToolTip("关闭时由 AI 自动自审并继续；开启时弹出术语表供手动删除/确认。")
        self.chk_batch_glossary_review.stateChanged.connect(self._on_batch_glossary_review_changed)
        ll.addWidget(self.chk_batch_glossary_review)

        self.chk_batch_transcribe_only = QCheckBox("仅转录")
        self.chk_batch_transcribe_only.setToolTip("只将音频/视频转成原目录同名 .srt，不执行后续优化")
        self.chk_batch_transcribe_only.stateChanged.connect(self._on_batch_transcribe_only_changed)
        ll.addWidget(self.chk_batch_transcribe_only)

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

    def _on_batch_transcribe_only_changed(self, state):
        transcribe_only = state != 0
        if transcribe_only:
            self.chk_batch_glossary_review.setChecked(False)

    def _on_batch_glossary_review_changed(self, state):
        glossary_review = state != 0
        if glossary_review:
            self.chk_batch_transcribe_only.setChecked(False)

    def _batch_add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择字幕文件", "", media_file_filter()
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
        if status == "skipped":
            name = Path(item.data(Qt.ItemDataRole.UserRole)).name
            item.setText(f"SKIP  {name}")
            return
        icons = {"processing": "🔄", "done": "✅", "error": "❌", "pending": "⏳"}
        name = Path(item.data(Qt.ItemDataRole.UserRole)).name
        item.setText(f"{icons.get(status, '⏳')}  {name}")

    def _batch_settings(self) -> dict:
        out_dir = None
        if not self.chk_same_dir.isChecked():
            out_dir = self.batch_out_dir.text().strip() or None
        return {
            "client": self._get_client(),
            "out_dir": out_dir,
            "overwrite": self.chk_overwrite_batch.isChecked(),
            "concurrency": max(1, min(100, int(self.input_concurrency.text() or "6"))),
            "batch_size": max(1, min(50, int(self.input_batch.text() or "5"))),
            "raw_glossary": self._glossary_to_text(),
            "files": list(self._batch_files),
            "input_lang": self.combo_input_lang.currentText(),
            "output_lang": self.combo_output_lang.currentText(),
            "transcribe_only": self.chk_batch_transcribe_only.isChecked(),
            "manual_glossary_review": self.chk_batch_glossary_review.isChecked(),
            "source_layout": self._source_layout(),
            "session_glossary": "",
        }

    def _batch_start(self):
        if not self._batch_files:
            QMessageBox.warning(self, "提示", "请先添加要处理的文件")
            return

        settings = self._batch_settings()
        if not settings["client"]:
            return

        # ✅ 防止重复点击导致旧 worker 信号触发闪退
        self._cleanup_batch_pipeline()

        self.btn_batch_start.setEnabled(False)
        self.btn_batch_stop.setEnabled(True)
        self.batch_overall_bar.setMaximum(len(self._batch_files))
        self.batch_overall_bar.setValue(0)
        self._batch_completed_count = 0
        self.batch_log.clear()
        self._batch_log_msg(f"▶ 开始批量处理 {len(self._batch_files)} 个文件")

        for i in range(self.batch_list.count()):
            self._batch_set_item_status(i, "pending")

        if not settings["transcribe_only"]:
            self._start_batch_glossary_preanalysis(settings)
            return

        self._start_batch_worker(settings)

    def _batch_runtime_glossary(self, settings: dict) -> str:
        fixed_glossary = self._glossary_to_text()
        session_glossary = (settings.get("session_glossary") or "").strip()
        return "\n".join(part for part in (fixed_glossary, session_glossary) if part)

    def _start_batch_worker(self, settings: dict):
        runtime_glossary = self._batch_runtime_glossary(settings)
        self._batch_worker = BatchWorker(
            client=settings["client"],
            files=settings["files"],
            out_dir=settings["out_dir"],
            concurrency=settings["concurrency"],
            batch_size=settings["batch_size"],
            overwrite=settings["overwrite"],
            target_first=self._target_first(),
            glossary_prompt=_format_glossary_prompt(runtime_glossary),
            raw_glossary=runtime_glossary,
            original_line=self._get_original_line_index(),
            transcription_audio_format=self._transcription_audio_format(),
            transcription_model=self.input_transcription_model.text().strip(),
            language=self._get_transcription_language(),
            chunk_concurrency=settings["concurrency"],
            transcribe_only=settings["transcribe_only"],
            input_lang=settings["input_lang"],
            output_lang=settings["output_lang"],
            source_layout=settings["source_layout"],
        )
        self._batch_thread = QThread()
        self._batch_worker.moveToThread(self._batch_thread)
        self._batch_thread.started.connect(self._batch_worker.run)

        self._batch_worker.file_started.connect(self._on_batch_file_started)
        self._batch_worker.file_analysis_done.connect(self._on_batch_analysis_done)
        self._batch_worker.file_progress.connect(self._on_batch_file_progress)
        self._batch_worker.file_done.connect(self._on_batch_file_done)
        self._batch_worker.file_skipped.connect(self._on_batch_file_skipped)
        self._batch_worker.file_error.connect(self._on_batch_file_error)
        self._batch_worker.all_done.connect(self._on_batch_all_done)
        self._batch_worker.log.connect(self._batch_log_msg)
        self._batch_worker.all_done.connect(self._batch_thread.quit)
        self._batch_thread.finished.connect(self._batch_thread.deleteLater)
        self._batch_thread.start()

    def _start_batch_glossary_preanalysis(self, settings: dict):
        self._batch_log_msg("📖 开始批量术语统一...")
        self._batch_glossary_settings = settings
        self._batch_glossary_worker = BatchGlossaryWorker(
            client=settings["client"],
            files=settings["files"],
            concurrency=settings["concurrency"],
            fixed_glossary=settings["raw_glossary"],
            original_line=self._get_original_line_index(),
            transcription_audio_format=self._transcription_audio_format(),
            input_lang=settings["input_lang"],
            output_lang=settings["output_lang"],
            manual_review=settings.get("manual_glossary_review", False),
            source_layout=settings["source_layout"],
        )
        self._batch_glossary_thread = QThread()
        self._batch_glossary_worker.moveToThread(self._batch_glossary_thread)
        self._batch_glossary_thread.started.connect(self._batch_glossary_worker.run)
        self._batch_glossary_worker.log.connect(self._batch_log_msg)
        self._batch_glossary_worker.finished.connect(self._after_batch_glossary_preanalysis)
        self._batch_glossary_worker.finished.connect(self._batch_glossary_thread.quit)
        self._batch_glossary_worker.error.connect(self._on_batch_glossary_error)
        self._batch_glossary_worker.error.connect(self._batch_glossary_thread.quit)
        self._batch_glossary_thread.finished.connect(self._batch_glossary_thread.deleteLater)
        self._batch_glossary_thread.start()

    def _after_batch_glossary_preanalysis(self, summary: BatchGlossarySummary):
        settings = self._batch_glossary_settings
        if not settings:
            self._reset_batch_controls_after_preanalysis_stop()
            return

        manual_review = bool(settings.get("manual_glossary_review"))
        if summary.failures:
            self._batch_log_msg(f"⚠️ 词汇预分析跳过 {len(summary.failures)} 个文件")
        settings["session_glossary"] = summary.session_glossary
        session_count = len([line for line in summary.session_glossary.splitlines() if line.strip()])
        if session_count:
            self._batch_log_msg(f"📖 已生成本次批量临时术语 {session_count} 条，仅用于当前批量，不写入固定词汇表")
        if not manual_review and summary.fixed_glossary:
            self._append_fixed_glossary_text(summary.fixed_glossary)
            fixed_count = len([line for line in summary.fixed_glossary.splitlines() if line.strip()])
            self._batch_log_msg(f"✅ AI 自审保留固定术语 {fixed_count} 条，已加入固定词汇表")

        if not manual_review:
            if not session_count and len(summary.failures) >= len(settings["files"]):
                QMessageBox.warning(self, "批量术语统一失败", "所有文件都无法完成词汇预分析，已停止批量处理。")
                self._reset_batch_controls_after_preanalysis_stop()
                return
            self._start_batch_worker(settings)
            return

        if not summary.candidates:
            if not session_count and len(summary.failures) >= len(settings["files"]):
                QMessageBox.warning(self, "批量词汇整理失败", "所有文件都无法完成词汇预分析，已停止批量处理。")
                self._reset_batch_controls_after_preanalysis_stop()
                return
            self._batch_log_msg("📖 没有建议永久保存的固定术语，继续批量处理")
            self._start_batch_worker(settings)
            return

        approved = self._confirm_batch_glossary(summary)
        if approved is None:
            self._batch_log_msg("⏹ 已取消批量词汇整理，未开始正式批量处理")
            self._reset_batch_controls_after_preanalysis_stop()
            return
        if approved:
            self._append_fixed_glossary_text(approved)
            self._batch_log_msg("✅ 已加入固定词汇表并保存配置")
        self._start_batch_worker(settings)

    def _on_batch_glossary_error(self, message: str):
        QMessageBox.critical(self, "批量词汇整理失败", message)
        self._batch_log_msg(f"❌ 批量词汇整理失败：{message}")
        self._reset_batch_controls_after_preanalysis_stop()

    def _reset_batch_controls_after_preanalysis_stop(self):
        self.btn_batch_start.setEnabled(True)
        self.btn_batch_stop.setEnabled(False)

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
        self._cleanup_worker_thread(self._batch_glossary_worker, self._batch_glossary_thread)
        self._batch_glossary_worker = None
        self._batch_glossary_thread = None
        self._batch_glossary_settings = None
        self._cleanup_worker_thread(self._batch_worker, self._batch_thread)
        self._batch_worker = None
        self._batch_thread = None

    def _cleanup_single_pipeline(self):
        self._cleanup_worker_thread(self._transcription_worker, self._transcription_thread)
        self._transcription_worker = None
        self._transcription_thread = None
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
        self._cleanup_worker_thread(self._pipeline_worker, self._pipeline_thread)
        self._pipeline_worker = None
        self._pipeline_thread = None

    def _batch_stop(self):
        if self._batch_glossary_worker:
            self._batch_glossary_worker.cancel()
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
        self._batch_completed_count += 1
        self.batch_overall_bar.setValue(self._batch_completed_count)
        self._batch_log_msg(f"✅ [{filename}] 完成 → {out_path}")

    @pyqtSlot(int, str, str)
    def _on_batch_file_skipped(self, file_idx: int, filename: str, reason: str):
        self._batch_set_item_status(file_idx, "skipped")
        self._batch_completed_count += 1
        self.batch_overall_bar.setValue(self._batch_completed_count)
        self._batch_log_msg(f"SKIP [{filename}] 跳过：{reason}")

    @pyqtSlot(int, str, str)
    def _on_batch_file_error(self, file_idx: int, filename: str, err: str):
        self._batch_set_item_status(file_idx, "error")
        self._batch_completed_count += 1
        self.batch_overall_bar.setValue(self._batch_completed_count)
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
        transcription_model = self.input_transcription_model.text().strip()
        return MistralClient(key, self.combo_model.currentText(), base_url, transcription_model)

    def _target_first(self) -> bool:
        # 输出顺序：默认中文在前
        return True

    def _get_original_line_index(self) -> int:
        """获取原文行号索引（0-based）"""
        return self.combo_original_line.currentIndex()

    def _source_layout(self) -> str:
        """Return the source-layout policy shared by single and batch tasks."""
        return self.combo_source_layout.currentData() or LAYOUT_AUTO

    def _extract_original_line(self, entries: list[SubtitleEntry]) -> list[SubtitleEntry]:
        """Normalize source layout without dropping wrapped monolingual lines."""
        input_code = LANGUAGE_MAP.get(
            self.combo_input_lang.currentText(), ("en", "English")
        )[0]
        output_code = LANGUAGE_MAP.get(
            self.combo_output_lang.currentText(), ("zh", "中文")
        )[0]
        normalized = normalize_source_entries(
            entries,
            source_line_index=self._get_original_line_index(),
            input_lang_code=input_code,
            output_lang_code=output_code,
            requested_layout=self._source_layout(),
        )
        self._last_source_layout = normalized.detected_layout
        self._last_source_layout_confidence = normalized.confidence
        self._last_source_layout_warnings = normalized.warnings
        return normalized.entries

    def _load_srt_content(self, path: str, content: str, cleanup: bool = True):
        self.entries = parse_srt(content)
        self._last_quality_report = None
        if hasattr(self, "btn_export"):
            self.btn_export.setEnabled(True)
        if cleanup:
            self._cleanup_single_pipeline()
        self._pending_media_path = None
        self._set_auto_glossary_text("")
        self._stage_data.clear()

        if self.entries:
            self.entries = self._extract_original_line(self.entries)
            self._log(
                f"Detected source layout: {self._last_source_layout} "
                f"(confidence {self._last_source_layout_confidence:.0%})"
            )
            for warning in self._last_source_layout_warnings:
                self._log(f"⚠️ {warning}")

        self._current_file_path = path
        self.lbl_file.setText(f"馃搫 {Path(path).name}")
        self.tab_original.setPlainText(content)
        n = len(self.entries)
        self.lbl_stat.setText(f"  {n} subtitles")
        self._save_stage_data("原始字幕", self.entries)
        self._refresh_compare()
        self.lbl_stat.setStyleSheet("""
            color: #3FB950; font-size: 12px; background: #12261E;
            border: 1px solid #238636; border-radius: 12px; padding: 3px 12px;
        """)
        self.progress_bar.setMaximum(n)
        self.progress_bar.setValue(0)
        self._log(f"Loaded: {path}, {n} subtitles")
        self.status_bar.showMessage(f"Loaded {n} subtitles · {Path(path).name}")

    def _select_media_for_single(self, media_path: str):
        self._cleanup_single_pipeline()
        self._pending_media_path = media_path
        self._transcription_auto_optimize = False
        self.entries = []
        self._current_file_path = media_path
        self._set_auto_glossary_text("")
        self._stage_data.clear()
        self.lbl_file.setText(f"Media selected: {Path(media_path).name}")
        self.tab_original.setPlainText("")
        self.progress_bar.setMaximum(1)
        self.progress_bar.setValue(0)
        self.lbl_stat.setText("  media pending")
        self._log(f"Media selected; click Start Optimize to transcribe first: {media_path}")
        self.status_bar.showMessage(f"Media selected · click Start Optimize: {Path(media_path).name}")

    def _start_single_transcription(self, media_path: str, auto_optimize: bool = False):
        client = self._get_client()
        if not client:
            return
        self._cleanup_single_pipeline()
        self._transcription_auto_optimize = auto_optimize
        self._set_auto_glossary_text("")
        self._stage_data.clear()
        self.entries = []
        self._current_file_path = None
        self.lbl_file.setText(f"Transcribing {Path(media_path).name}")
        self.tab_original.setPlainText("")
        self.progress_bar.setMaximum(0)
        self.progress_bar.setValue(0)
        self.btn_optimize.setEnabled(False)
        self._log(f"Transcribing media to SRT: {media_path}")
        self.status_bar.showMessage(f"Transcribing media: {Path(media_path).name}")

        self._transcription_worker = TranscriptionWorker(
            client,
            media_path,
            transcription_model=self.input_transcription_model.text().strip(),
            transcription_audio_format=self._transcription_audio_format(),
        )
        self._transcription_thread = QThread()
        self._transcription_worker.moveToThread(self._transcription_thread)
        self._transcription_thread.started.connect(self._transcription_worker.run)
        self._transcription_worker.log.connect(self._log)
        self._transcription_worker.finished.connect(self._on_single_transcription_done)
        self._transcription_worker.error.connect(self._on_single_transcription_error)
        self._transcription_worker.finished.connect(self._transcription_worker.deleteLater)
        self._transcription_worker.error.connect(self._transcription_worker.deleteLater)
        self._transcription_worker.finished.connect(self._transcription_thread.quit)
        self._transcription_worker.error.connect(self._transcription_thread.quit)
        self._transcription_thread.finished.connect(self._on_single_transcription_thread_finished)
        self._transcription_thread.finished.connect(self._transcription_thread.deleteLater)
        self._transcription_thread.start()

    @pyqtSlot(str, str)
    def _on_single_transcription_done(self, srt_path: str, content: str):
        self.progress_bar.setMaximum(1)
        self.progress_bar.setValue(1)
        self.btn_optimize.setEnabled(True)
        self._log(f"Transcription complete: {srt_path}")
        self._load_srt_content(srt_path, content, cleanup=False)

    def _on_single_transcription_error(self, err: str):
        self.progress_bar.setMaximum(1)
        self.progress_bar.setValue(0)
        self.btn_optimize.setEnabled(True)
        self._log(f"Transcription failed: {err}")
        self.status_bar.showMessage("Transcription failed")
        QMessageBox.critical(self, "Transcription failed", err)

    def _on_single_transcription_thread_finished(self):
        should_auto_optimize = self._transcription_auto_optimize and bool(self.entries)
        self._transcription_worker = None
        self._transcription_thread = None
        self._transcription_auto_optimize = False
        if should_auto_optimize:
            QTimer.singleShot(0, self._run_optimize)

    def _open_file(self):
        logger.info("开始打开字幕文件")
        path, _ = QFileDialog.getOpenFileName(
            self, "打开字幕文件", "", media_file_filter()
        )
        if not path:
            logger.info("用户取消文件选择")
            return
        if is_media_file(path):
            self._select_media_for_single(path)
            return
        try:
            logger.debug(f"尝试读取文件: {path}")
            with open(path, "r", encoding="utf-8-sig") as f:
                content = f.read()
            logger.info(f"文件读取成功，开始解析SRT内容")
            self.entries = parse_srt(content)
            self._last_quality_report = None
            if hasattr(self, "btn_export"):
                self.btn_export.setEnabled(True)
            self._pending_media_path = None
            self._transcription_auto_optimize = False

            # 清理旧的 worker/thread，防止闪退
            self._cleanup_single_pipeline()
            self._set_auto_glossary_text("")
            # 重置阶段数据
            self._stage_data.clear()

            if self.entries:
                self.entries = self._extract_original_line(self.entries)
                self._log(
                    f"📋 原文布局：{self._last_source_layout} "
                    f"(置信度 {self._last_source_layout_confidence:.0%})"
                )
                for warning in self._last_source_layout_warnings:
                    self._log(f"⚠️ {warning}")

            self._current_file_path = path
            self.lbl_file.setText(f"📄 {Path(path).name}")
            self.tab_original.setPlainText(content)
            n = len(self.entries)
            self.lbl_stat.setText(f"  {n} 条字幕")
            # 保存原始字幕阶段数据
            self._save_stage_data("原始字幕", self.entries)
            self._refresh_compare()
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

    def _coerce_analysis_result(self, result) -> AnalysisResult:
        if isinstance(result, AnalysisResult):
            return result
        return AnalysisResult(context=str(result or ""))

    def _set_auto_glossary_text(self, text: str):
        self.auto_glossary_text = text or ""
        if hasattr(self, "text_auto_glossary"):
            self.text_auto_glossary.setPlainText(self.auto_glossary_text)

    def _get_auto_glossary_text(self) -> str:
        if hasattr(self, "text_auto_glossary"):
            self.auto_glossary_text = self.text_auto_glossary.toPlainText().strip()
        return self.auto_glossary_text

    def _get_merged_glossary_text(self, entries=None) -> str:
        user_glossary = self._glossary_to_text()
        if entries and user_glossary:
            from core.workers import filter_glossary_by_entries
            user_glossary = filter_glossary_by_entries(user_glossary, entries)
        return merge_glossaries(user_glossary, self._get_auto_glossary_text()).raw

    def _get_merged_glossary_prompt(self, entries=None) -> str:
        return _format_glossary_prompt(self._get_merged_glossary_text(entries=entries))

    def _log_auto_glossary_merge(self, auto_glossary: str):
        merge = merge_glossaries(self._glossary_to_text(), auto_glossary)
        if not auto_glossary:
            self._log("📖 阶段一未提取自动术语")
            return
        for line in format_glossary_merge_log(merge):
            self._log(f"📖 {line}")

    def _confirm_auto_glossary_for_single_run(self) -> bool:
        auto_glossary = self._get_auto_glossary_text()
        if not auto_glossary:
            return True

        filename = Path(self._current_file_path).name if self._current_file_path else "当前文件"
        summary = merge_batch_glossary_candidates(
            [(filename, auto_glossary)],
            fixed_glossary=self._glossary_to_text(),
        )
        if not summary.candidates:
            self._set_auto_glossary_text("")
            self._log("📖 阶段一未发现需要加入固定词汇表的新术语")
            return True

        approved = self._confirm_glossary_candidates(
            summary,
            title="确认阶段一术语候选",
            description="删除不合适的候选词；确认后剩余词汇会立即加入固定词汇表，并用于本次后续处理。",
        )
        if approved is None:
            return False

        self._set_auto_glossary_text("")
        if approved:
            self._append_fixed_glossary_text(approved)
            count = len([line for line in approved.splitlines() if line.strip()])
            self._log(f"📖 已加入固定词汇表：{count} 条")
        return True

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
        self._set_auto_glossary_text("")

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

    def _on_analysis_done(self, result):
        logger.info("翻译情景分析完成")
        analysis = self._coerce_analysis_result(result)
        self.context_text = analysis.context
        self.text_context.setPlainText(analysis.context)
        self._set_auto_glossary_text(analysis.auto_glossary)
        self._log("✅ 情景分析完成")
        self._log_auto_glossary_merge(analysis.auto_glossary)

    def _run_optimize(self):
        logger.info("开始执行字幕优化流程")
        if not self.entries:
            if self._pending_media_path:
                self._start_single_transcription(self._pending_media_path, auto_optimize=True)
                return
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
        self._last_quality_report = None

        # 重置阶段数据（保留原始字幕）
        original = self._stage_data.get("原始字幕")
        self._stage_data.clear()
        if original:
            self._stage_data["原始字幕"] = original

        logger.debug("设置运行状态")
        self._set_running_state(True)
        self._pipeline_active = True

        for e in self.entries:
            e.status = "pending"
            e.optimized_lines = None
            e.stage_status = "pending"
            e.error_msg = ""
            e.quality_flags.clear()

        if hasattr(self, "table_compare"):
            self.table_compare.setRowCount(0)
            self.lbl_compare_stats.setText("0 条")

        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(len(self.entries))
        self._set_stage(0)

        logger.info("开始第一阶段：分析翻译情景")
        self._log("🔍 [阶段1/4] 开始分析翻译情景...")
        self.text_context.setPlainText("分析中...")
        self._set_auto_glossary_text("")

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

    def _after_analysis(self, result, client: MistralClient):
        if not self._pipeline_active:
            return
        logger.info("情景分析完成，进入第二阶段：智能重组")
        analysis = self._coerce_analysis_result(result)
        self.context_text = analysis.context
        self.text_context.setPlainText(analysis.context)
        self._set_auto_glossary_text(analysis.auto_glossary)
        self._log("✅ [阶段1/4] 情景分析完成")
        if not self._confirm_auto_glossary_for_single_run():
            self._on_pipeline_error("已停止：阶段一自动术语未确认")
            return
        if self._get_auto_glossary_text():
            self._log_auto_glossary_merge(self._get_auto_glossary_text())

        concurrency = max(1, min(100, int(self.input_concurrency.text() or "6")))
        batch_size = max(1, min(50, int(self.input_batch.text() or "5")))
        seg_batch_size = max(10, min(30, int(self.input_batch.text() or "15")))
        glossary_prompt = self._get_merged_glossary_prompt(entries=self.entries)

        self._log("🔗 [统一任务] 已冻结情景与术语，开始语义重组 → 翻译 → 审校")
        self._pipeline_worker = PipelineExecutionWorker(
            client,
            self.entries,
            analysis.context,
            glossary_prompt,
            concurrency=concurrency,
            batch_size=batch_size,
            segment_batch_size=seg_batch_size,
            input_lang=self.combo_input_lang.currentText(),
            output_lang=self.combo_output_lang.currentText(),
        )
        self._pipeline_thread = QThread()
        self._pipeline_worker.moveToThread(self._pipeline_thread)
        self._pipeline_thread.started.connect(self._pipeline_worker.run)
        self._pipeline_worker.stage_started.connect(self._on_unified_stage_started)
        self._pipeline_worker.stage_progress.connect(self._on_unified_stage_progress)
        self._pipeline_worker.stage_snapshot.connect(self._on_unified_stage_snapshot)
        self._pipeline_worker.log.connect(self._log)
        self._pipeline_worker.finished.connect(self._on_unified_pipeline_done)
        self._pipeline_worker.finished.connect(self._pipeline_thread.quit)
        self._pipeline_worker.cancelled.connect(self._pipeline_thread.quit)
        self._pipeline_worker.error.connect(self._on_pipeline_error)
        self._pipeline_worker.error.connect(self._pipeline_thread.quit)
        self._pipeline_thread.finished.connect(self._pipeline_thread.deleteLater)
        self._pipeline_thread.start()

    @pyqtSlot(str)
    def _on_unified_stage_started(self, stage: str):
        mapping = {
            "segment": (1, "智能重组"),
            "translate": (2, "AI翻译"),
            "optimize": (3, "忠实度/自然度/一致性审校"),
        }
        stage_index, label = mapping.get(stage, (1, stage))
        self._set_stage(stage_index)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(max(1, len(self.entries)))
        self.lbl_progress_detail.setText(f"{label}：0/{len(self.entries)}")
        self._log(f"▶ [{label}] 开始")

    @pyqtSlot(str, int, int)
    def _on_unified_stage_progress(self, stage: str, current: int, total: int):
        labels = {
            "segment": "智能重组",
            "translate": "AI翻译",
            "optimize": "质量审校",
        }
        self.progress_bar.setMaximum(max(1, total))
        self.progress_bar.setValue(current)
        self.lbl_progress_detail.setText(
            f"{labels.get(stage, stage)}：{current}/{total}"
        )

    @pyqtSlot(str, object, bool)
    def _on_unified_stage_snapshot(self, stage: str, entries: list, use_optimized: bool):
        if not self._pipeline_active:
            return
        self.entries = entries
        stage_names = {
            "segment": "阶段2 重组",
            "translate": "阶段3 翻译",
            "optimize": "阶段4 优化",
        }
        snapshot_names = {
            "segment": "AFTER_SEGMENT",
            "translate": "AFTER_TRANSLATE",
            "optimize": "AFTER_OPTIMIZE",
        }
        if self._current_file_path:
            write_phase_snapshot(
                self._current_file_path,
                snapshot_names.get(stage, stage.upper()),
                entries,
                use_optimized=use_optimized,
            )
        self._save_stage_data(stage_names.get(stage, stage), entries)
        self.lbl_stat.setText(f"  {len(entries)} 条字幕")

    @pyqtSlot(object)
    def _on_unified_pipeline_done(self, result):
        self._pipeline_active = False
        self.entries = list(result.entries)
        self._last_quality_report = result.quality_report
        self._set_running_state(False)
        self._save_stage_data("阶段4 优化", self.entries)
        done = sum(1 for entry in self.entries if entry.status == "done")
        needs_review = sum(
            1 for entry in self.entries if entry.status in {"error", "needs_review"}
        )
        if result.quality_report.passed:
            self._log(f"✅ 统一任务完成：{done} 条，质量门禁通过")
        else:
            self._log(
                f"❌ 统一任务完成但未通过质量门禁："
                f"{len(result.quality_report.critical_issues)} 个关键问题"
            )
        self.status_bar.showMessage(
            f"处理完成：{len(self.entries)} 条，待复核 {needs_review} 条"
        )
        for label in self._stage_labels:
            label.setStyleSheet(
                "color: #5a3a1a; font-size: 11px; font-weight: 700; "
                "background: #e6f9f6; border: 2px solid #82d5bb; "
                "border-radius: 12px; padding: 3px 8px;"
            )
        self._refresh_compare()

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
        # 重组阶段不刷新对比，等全部流程结束后统一对比

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
        glossary_prompt = self._get_merged_glossary_prompt()

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
        glossary_prompt = self._get_merged_glossary_prompt()

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

    @pyqtSlot(int, int)
    def _on_progress(self, current: int, total: int):
        self.progress_bar.setValue(current)
        self.lbl_progress_detail.setText(f"已完成 {current}/{total} 条")
        self.status_bar.showMessage(f"优化进行中 {current}/{total}...")

    def _on_optimize_done(self, final_entries=None):
        self._pipeline_active = False
        if final_entries is not None:
            self.entries = list(final_entries)
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
        # 注意：不在这里清理 worker/thread，因为 thread 的 finished 信号已经连接了 deleteLater
        # 清理会在下次 _run_optimize 或 _open_file 时进行

    def _stop_optimize(self):
        self._pipeline_active = False
        if self._segment_worker: self._segment_worker.cancel()
        if self._translate_worker: self._translate_worker.cancel()
        if self._optimize_worker: self._optimize_worker.cancel()
        if self._pipeline_worker: self._pipeline_worker.cancel()
        self._set_running_state(False)
        self._set_stage(-1)
        self._log("⏹ 已停止")

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
                'status': e.status,
                'error_msg': e.error_msg,
                'source_indexes': list(e.source_indexes or [e.index]),
            }
            for e in entries
        ]
        logger.info(f"已保存阶段数据: {stage_name}, {len(entries)} 条")
        self._refresh_compare()

    def _entry_to_stage_record(self, entry: SubtitleEntry) -> dict:
        return {
            'index': entry.index,
            'start_time': entry.start_time,
            'end_time': entry.end_time,
            'lines': list(entry.lines),
            'original_lines': list(entry.original_lines) if entry.original_lines else None,
            'optimized_lines': list(entry.optimized_lines) if entry.optimized_lines else None,
            'status': entry.status,
            'error_msg': entry.error_msg,
            'source_indexes': list(entry.source_indexes or [entry.index]),
        }

    def _update_compare_stage_options(self):
        if not hasattr(self, "combo_compare_stage"):
            return

        current = self.combo_compare_stage.currentText()
        stages = ["当前结果"]
        preferred = ["阶段4 优化", "阶段3 翻译", "阶段2 重组", "原始字幕"]
        stages.extend(stage for stage in preferred if stage in self._stage_data)
        stages.extend(stage for stage in self._stage_data if stage not in stages)

        self.combo_compare_stage.blockSignals(True)
        self.combo_compare_stage.clear()
        self.combo_compare_stage.addItems(stages)
        if current in stages:
            self.combo_compare_stage.setCurrentText(current)
        elif "阶段4 优化" in stages:
            self.combo_compare_stage.setCurrentText("阶段4 优化")
        self.combo_compare_stage.blockSignals(False)

    def _stage_records_for_compare(self, stage_name: str) -> list[dict]:
        if stage_name == "当前结果":
            return [self._entry_to_stage_record(e) for e in self.entries]
        return list(self._stage_data.get(stage_name, []))

    def _join_compare_lines(self, lines) -> str:
        return "\n".join(str(line) for line in (lines or []) if str(line).strip())

    def _compare_original_text(self, record: dict) -> str:
        return self._join_compare_lines(record.get("lines") or record.get("original_lines") or [])

    def _compare_result_text(self, record: dict) -> str:
        lines = record.get("lines") or []
        optimized = record.get("optimized_lines") or []
        if optimized:
            return self._join_compare_lines(optimized)
        return self._join_compare_lines(lines)

    def _set_compare_row_item(self, row: int, col: int, text: str, background: str | None = None):
        item = QTableWidgetItem(text)
        item.setToolTip(text)
        if background:
            item.setBackground(QColor(background))
        self.table_compare.setItem(row, col, item)

    def _compare_original_cell_text(self, record: dict) -> str:
        meta = f"#{record.get('index', '')}  {record.get('start_time', '')} --> {record.get('end_time', '')}"
        body = self._compare_original_text(record)
        return f"{meta}\n{body}".strip()

    def _compare_result_cell_text(self, record: dict) -> str:
        meta = f"#{record.get('index', '')}  {record.get('start_time', '')} --> {record.get('end_time', '')}"
        body = self._compare_result_text(record)
        return f"{meta}\n{body}".strip()

    def _source_key_for_compare(self, record: dict) -> tuple[int, ...]:
        source_indexes = record.get("source_indexes") or [record.get("index")]
        cleaned = []
        for value in source_indexes:
            try:
                cleaned.append(int(value))
            except (TypeError, ValueError):
                continue
        return tuple(cleaned or [int(record.get("index", 0) or 0)])

    def _compare_groups(self, records: list[dict]) -> list[dict]:
        original_records = self._stage_data.get("原始字幕", [])
        original_by_id = {
            int(record.get("index")): record
            for record in original_records
            if record.get("index") is not None
        }

        groups: list[dict] = []
        for record in records:
            key = self._source_key_for_compare(record)
            if groups and groups[-1]["key"] == key:
                groups[-1]["results"].append(record)
            else:
                originals = [
                    original_by_id[source_id]
                    for source_id in key
                    if source_id in original_by_id
                ]
                if not originals:
                    originals = [record]
                groups.append({"key": key, "originals": originals, "results": [record]})
        return groups

    def _compare_group_type(self, originals: list[dict], results: list[dict]) -> str:
        statuses = [record.get("status") for record in results]
        errors = [record.get("error_msg") for record in results if record.get("error_msg")]
        if "error" in statuses:
            return errors[0] if errors else "错误"

        orig_count = len(originals)
        result_count = len(results)
        if orig_count == 1 and result_count > 1:
            return f"拆分 1→{result_count}"
        if orig_count > 1 and result_count == 1:
            return f"合并 {orig_count}→1"
        if orig_count != result_count:
            return f"重组 {orig_count}→{result_count}"

        original_text = "\n".join(self._compare_original_text(record) for record in originals).strip()
        result_text = "\n".join(self._compare_result_text(record) for record in results).strip()
        return "已变化" if original_text != result_text else "未变化"

    def _compare_group_haystack(self, group: dict, group_type: str) -> str:
        parts = [group_type, ",".join(str(i) for i in group["key"])]
        for record in group["originals"]:
            parts.extend([
                str(record.get("index", "")),
                str(record.get("start_time", "")),
                str(record.get("end_time", "")),
                self._compare_original_text(record),
            ])
        for record in group["results"]:
            parts.extend([
                str(record.get("index", "")),
                str(record.get("start_time", "")),
                str(record.get("end_time", "")),
                self._compare_result_text(record),
                str(record.get("status") or ""),
                str(record.get("error_msg") or ""),
            ])
        return "\n".join(parts).casefold()

    def _compare_background(self, group_type: str) -> str | None:
        if "错误" in group_type:
            return "#ffe1dc"
        if group_type.startswith("拆分"):
            return "#e8f1ff"
        if group_type.startswith("合并"):
            return "#e6f9f6"
        if group_type.startswith("重组"):
            return "#fff3cd"
        if group_type == "已变化":
            return "#f1f8e9"
        return None

    def _span_compare_cell(self, row: int, col: int, row_span: int):
        if row_span > 1:
            self.table_compare.setSpan(row, col, row_span, 1)

    def _refresh_compare(self, *args):
        """刷新合并单元格式的单文件对比表。"""
        if not hasattr(self, "table_compare"):
            return

        self._update_compare_stage_options()
        stage_name = self.combo_compare_stage.currentText() or "当前结果"
        records = self._stage_records_for_compare(stage_name)
        groups = self._compare_groups(records)
        query = self.input_compare_filter.text().strip().casefold()
        changed_only = self.chk_compare_changed_only.isChecked()

        visible_groups = []
        total_rows = 0
        for group in groups:
            group_type = self._compare_group_type(group["originals"], group["results"])
            if changed_only and group_type == "未变化":
                continue
            if query and query not in self._compare_group_haystack(group, group_type):
                continue
            row_span = max(len(group["originals"]), len(group["results"]), 1)
            visible_groups.append((group, group_type, row_span))
            total_rows += row_span

        self.table_compare.setUpdatesEnabled(False)
        self.table_compare.clearSpans()
        self.table_compare.setRowCount(total_rows)

        row = 0
        for group, group_type, row_span in visible_groups:
            originals = group["originals"]
            results = group["results"]
            background = self._compare_background(group_type)

            if len(originals) == 1 and row_span > 1:
                original = originals[0]
                self._set_compare_row_item(row, 0, self._compare_original_cell_text(original), background)
                self._span_compare_cell(row, 0, row_span)
            else:
                for offset, original in enumerate(originals):
                    current_row = row + offset
                    self._set_compare_row_item(current_row, 0, self._compare_original_cell_text(original), background)

            if len(results) == 1 and row_span > 1:
                result = results[0]
                self._set_compare_row_item(row, 1, self._compare_result_cell_text(result), background)
                self._span_compare_cell(row, 1, row_span)
            else:
                for offset, result in enumerate(results):
                    current_row = row + offset
                    self._set_compare_row_item(current_row, 1, self._compare_result_cell_text(result), background)

            row += row_span

        self.table_compare.resizeRowsToContents()
        self.table_compare.setUpdatesEnabled(True)
        self.lbl_compare_stats.setText(f"{len(visible_groups)} 组 / {len(groups)} 组")

    def _export_srt(self):
        logger.info("开始导出优化后的字幕文件")
        if not self.entries:
            msg = "没有可导出的字幕"
            logger.warning(msg)
            return QMessageBox.warning(self, "提示", msg)
        if (
            self._last_quality_report is not None
            and not self._last_quality_report.passed
        ):
            count = len(self._last_quality_report.critical_issues)
            msg = (
                f"质量门禁未通过（{count} 个关键问题），已阻止导出。"
                "请重新执行；失败条目会在日志中标出。"
            )
            logger.warning(msg)
            return QMessageBox.warning(self, "质量门禁未通过", msg)
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
            self.btn_export.setEnabled(False)
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
            self.btn_export.setEnabled(
                self._last_quality_report is None
                or self._last_quality_report.passed
            )
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

    def _refresh_glossary_headers(self):
        if not hasattr(self, "table_glossary"):
            return
        source_label, target_label = glossary_column_labels(
            self.combo_input_lang.currentText() if hasattr(self, "combo_input_lang") else "",
            self.combo_output_lang.currentText() if hasattr(self, "combo_output_lang") else "",
        )
        self.table_glossary.setHorizontalHeaderLabels([source_label, target_label])

    def _append_fixed_glossary_text(self, glossary_text: str):
        existing = self._glossary_to_text()
        combined = "\n".join(part for part in (existing, glossary_text.strip()) if part)
        self._glossary_from_text(combined)
        self._save_config()

    def _candidate_sources_text(self, candidate: GlossaryCandidate) -> str:
        if len(candidate.files) <= 3:
            return "、".join(candidate.files)
        return f"{len(candidate.files)} 个文件"

    def _batch_glossary_table_candidates(self, table: QTableWidget) -> list[GlossaryCandidate]:
        candidates: list[GlossaryCandidate] = []
        for row in range(table.rowCount()):
            source_item = table.item(row, 0)
            target_item = table.item(row, 1)
            files_item = table.item(row, 2)
            status_item = table.item(row, 3)
            source = source_item.text().strip() if source_item else ""
            target = target_item.text().strip() if target_item else ""
            files = tuple(files_item.data(Qt.ItemDataRole.UserRole) or ()) if files_item else ()
            status = status_item.text().strip() if status_item else "新词"
            if source or target:
                candidates.append(GlossaryCandidate(source, target, files, status))
        return candidates

    def _confirm_glossary_candidates(
        self,
        summary: BatchGlossarySummary,
        *,
        title: str,
        description: str,
    ) -> str | None:
        if not summary.candidates:
            return ""

        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(820, 520)
        layout = QVBoxLayout(dialog)

        label = QLabel(description)
        label.setWordWrap(True)
        label.setStyleSheet("color: #8a7b66; font-size: 12px;")
        layout.addWidget(label)

        if summary.failures:
            fail_label = QLabel(
                "预分析跳过："
                + "；".join(f"{failure.filename}: {failure.reason}" for failure in summary.failures[:5])
            )
            fail_label.setWordWrap(True)
            fail_label.setStyleSheet("color: #b15f3b; font-size: 12px;")
            layout.addWidget(fail_label)

        table = QTableWidget()
        table.setColumnCount(5)
        table.setMouseTracking(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.setStyleSheet("""
            QTableWidget {
                background: #f8f8f0;
                alternate-background-color: #f0e8d8;
                gridline-color: #d8ceb9;
                color: #5f4b31;
            }
            QHeaderView::section {
                background: #f0e8d8;
                color: #5a3a1a;
                font-weight: 700;
                border: 0;
                border-bottom: 1px solid #c4b89e;
                padding: 7px 8px;
            }
            QTableWidget::item {
                padding: 6px 8px;
            }
            QTableWidget::item:hover {
                background: #fff1bf;
                color: #3f2b15;
            }
            QTableWidget::item:selected {
                background: #d9f6ef;
                color: #2e3a31;
            }
        """)
        source_label, target_label = glossary_column_labels(
            self.combo_input_lang.currentText(),
            self.combo_output_lang.currentText(),
        )
        table.setHorizontalHeaderLabels([source_label, target_label, "来源", "状态", "操作"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setRowCount(len(summary.candidates))

        hover_state = {"row": -1}

        def clear_hover_row(row: int):
            if row < 0:
                return
            for col in range(table.columnCount()):
                item = table.item(row, col)
                if item:
                    item.setData(Qt.ItemDataRole.BackgroundRole, None)

        def set_hover_row(row: int):
            if row == hover_state["row"]:
                return
            clear_hover_row(hover_state["row"])
            hover_state["row"] = row
            for col in range(table.columnCount()):
                item = table.item(row, col)
                if item:
                    item.setBackground(QColor("#fff1bf"))

        def delete_row(button: QPushButton):
            for row in range(table.rowCount()):
                if table.cellWidget(row, 4) is button:
                    table.removeRow(row)
                    hover_state["row"] = -1
                    break

        for row, candidate in enumerate(summary.candidates):
            table.setItem(row, 0, QTableWidgetItem(candidate.source))
            table.setItem(row, 1, QTableWidgetItem(candidate.target))
            source_item = QTableWidgetItem(self._candidate_sources_text(candidate))
            source_item.setData(Qt.ItemDataRole.UserRole, candidate.files)
            table.setItem(row, 2, source_item)
            table.setItem(row, 3, QTableWidgetItem(candidate.status))
            table.setItem(row, 4, QTableWidgetItem(""))
            btn_delete = QPushButton("删除")
            btn_delete.clicked.connect(lambda _checked=False, b=btn_delete: delete_row(b))
            table.setCellWidget(row, 4, btn_delete)

        table.itemEntered.connect(lambda item: set_hover_row(item.row()))
        layout.addWidget(table, 1)

        btn_row = QHBoxLayout()
        btn_clear = QPushButton("全部删除")
        btn_cancel = QPushButton("取消")
        btn_ok = QPushButton("确认并加入固定词汇表")
        btn_row.addWidget(btn_clear)
        btn_row.addStretch()
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_ok)
        layout.addLayout(btn_row)

        btn_clear.clicked.connect(lambda: table.setRowCount(0))
        btn_cancel.clicked.connect(dialog.reject)

        approved_text = {"value": None}

        def accept_dialog():
            candidates = self._batch_glossary_table_candidates(table)
            conflicts = unresolved_conflict_sources(candidates)
            if conflicts:
                QMessageBox.warning(
                    dialog,
                    "仍有译法冲突",
                    "请先删除或修改这些原文的冲突译文：\n" + "\n".join(conflicts),
                )
                return
            approved_text["value"] = approved_candidates_to_text(candidates)
            dialog.accept()

        btn_ok.clicked.connect(accept_dialog)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return approved_text["value"] or ""

    def _confirm_batch_glossary(self, summary: BatchGlossarySummary) -> str | None:
        return self._confirm_glossary_candidates(
            summary,
            title="批量固定术语确认",
            description="本次批量临时术语会自动用于当前处理；下面仅是建议长期保存到设置里的固定术语。",
        )

    def _glossary_add_row(self):
        """在术语表末尾添加空行"""
        row = self.table_glossary.rowCount()
        self.table_glossary.insertRow(row)
        self.table_glossary.setItem(row, 0, QTableWidgetItem(""))
        self.table_glossary.setItem(row, 1, QTableWidgetItem(""))
        self.table_glossary.editItem(self.table_glossary.item(row, 0))

    def _glossary_dedup(self):
        """去除术语表中重复的英文词条（保留最后出现的条目）"""
        self._glossary_normalize()

    def _glossary_normalize(self):
        """去重并按 Source 字母排序；重复 Source 保留最后一次译法。"""
        entries = {}
        for row in range(self.table_glossary.rowCount()):
            en_item = self.table_glossary.item(row, 0)
            zh_item = self.table_glossary.item(row, 1)
            if en_item and zh_item and en_item.text().strip() and zh_item.text().strip():
                source = en_item.text().strip()
                target = zh_item.text().strip()
                key = source.casefold()
                entries[key] = (source, target)

        sorted_items = sorted(
            entries.values(),
            key=lambda pair: (pair[0].casefold(), pair[0]),
        )

        self.table_glossary.setRowCount(0)
        for source, target in sorted_items:
            row = self.table_glossary.rowCount()
            self.table_glossary.insertRow(row)
            self.table_glossary.setItem(row, 0, QTableWidgetItem(source))
            self.table_glossary.setItem(row, 1, QTableWidgetItem(target))

    def _glossary_del_row(self):
        """删除选中的术语表行"""
        row = self.table_glossary.currentRow()
        if row >= 0:
            self.table_glossary.removeRow(row)

    def _glossary_to_text(self) -> str:
        """将表格内容转换为 English->中文 格式文本（自动去重）"""
        self._glossary_dedup()
        lines = []
        for row in range(self.table_glossary.rowCount()):
            en = self.table_glossary.item(row, 0)
            zh = self.table_glossary.item(row, 1)
            if en and zh and en.text().strip() and zh.text().strip():
                lines.append(f"{en.text().strip()}->{zh.text().strip()}")
        return "\n".join(lines)

    def _glossary_from_text(self, text: str):
        """将 English->中文 格式文本加载到表格"""
        self.table_glossary.setRowCount(0)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            for sep in ("->", "→"):
                if sep in line:
                    parts = line.split(sep, 1)
                    if parts[0].strip() and parts[1].strip():
                        row = self.table_glossary.rowCount()
                        self.table_glossary.insertRow(row)
                        self.table_glossary.setItem(row, 0, QTableWidgetItem(parts[0].strip()))
                        self.table_glossary.setItem(row, 1, QTableWidgetItem(parts[1].strip()))
                    break
        self._glossary_dedup()

    def _get_glossary_prompt(self, entries=None) -> str:
        """解析术语表输入框，返回一个措辞强硬的独立规则字符串（空则返回空串）。
        entries 非空时，先过滤掉字幕中未出现的术语以节省 token。
        """
        raw = self._glossary_to_text()
        if not raw:
            return ""
        if entries:
            from core.workers import filter_glossary_by_entries
            raw = filter_glossary_by_entries(raw, entries)
        return _format_glossary_prompt(raw)

    def _config_path(self) -> Path:
        return Path.home() / ".subtitle_optimizer_config.json"

    def _transcription_audio_format(self) -> str:
        return "wav" if self.chk_transcription_wav.isChecked() else "mp3"

    def _get_transcription_language(self) -> str | None:
        """获取转录语言设置，返回语言代码或None"""
        lang = self.combo_input_lang.currentText()
        # 映射常见语言到ISO代码
        lang_map = {
            "English": "en",
            "中文": "zh",
            "Japanese": "ja",
            "Korean": "ko",
            "French": "fr",
            "German": "de",
            "Spanish": "es",
            "Russian": "ru",
            "Portuguese": "pt",
            "Italian": "it",
        }
        return lang_map.get(lang)

    def _save_config(self):
        cfg = {
            "api_key": self.input_apikey.text().strip(),
            "base_url": self.input_base_url.text().strip(),
            "model": self.combo_model.currentText(),
            "transcription_model": self.input_transcription_model.text().strip(),
            "transcription_audio_format": self._transcription_audio_format(),
            "concurrency": self.input_concurrency.text(),
            "batch_size": self.input_batch.text(),
            "original_line": self.combo_original_line.currentIndex(),
            "source_layout": self._source_layout(),
            "glossary": self._glossary_to_text(),
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
            self.input_transcription_model.setText(
                cfg.get("transcription_model", "voxtral-mini-latest")
            )
            self.chk_transcription_wav.setChecked(
                cfg.get("transcription_audio_format", "mp3") == "wav"
            )
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
            source_layout = cfg.get("source_layout", LAYOUT_AUTO)
            source_layout_index = self.combo_source_layout.findData(source_layout)
            self.combo_source_layout.setCurrentIndex(
                source_layout_index if source_layout_index >= 0 else 0
            )
            self._glossary_from_text(cfg.get("glossary", ""))
            # 加载语言设置（向后兼容）
            input_lang = cfg.get("input_language", "English")
            output_lang = cfg.get("output_language", "中文")
            if input_lang in LANGUAGE_LABELS:
                self.combo_input_lang.setCurrentText(input_lang)
            if output_lang in LANGUAGE_LABELS:
                self.combo_output_lang.setCurrentText(output_lang)
        except Exception:
            pass

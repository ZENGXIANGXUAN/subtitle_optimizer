import sys
import re
from difflib import SequenceMatcher
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QFileDialog, QTableWidget,
                             QHeaderView, QTextBrowser, QLabel)
from PyQt6.QtGui import QFont


class SrtDiffViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyQt6 字幕对比查看器")
        self.resize(1000, 700)

        # 存储两份字幕的数据结构：{ 序号(int): 文本内容(str) }
        self.srt1_data = {}
        self.srt2_data = {}

        self.init_ui()

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # 顶部操作栏
        btn_layout = QHBoxLayout()
        self.btn_load_left = QPushButton("加载左侧字幕 (旧/原版)")
        self.btn_load_right = QPushButton("加载右侧字幕 (新/修改版)")
        self.btn_load_left.clicked.connect(lambda: self.load_file(is_left=True))
        self.btn_load_right.clicked.connect(lambda: self.load_file(is_left=False))

        btn_layout.addWidget(self.btn_load_left)
        btn_layout.addWidget(self.btn_load_right)
        layout.addLayout(btn_layout)

        # 核心对比表格
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["左侧字幕", "右侧字幕"])
        # 让两列平分宽度
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        # 隐藏垂直序号，因为我们会自己处理
        self.table.verticalHeader().setVisible(False)
        # 取消表格的网格线，让视觉更干净一点（可选）
        # self.table.setShowGrid(False)

        layout.addWidget(self.table)

    def load_file(self, is_left):
        file_path, _ = QFileDialog.getOpenFileName(self, "选择字幕文件", "",
                                                   "Subtitle Files (*.srt *.txt);;All Files (*)")
        if not file_path:
            return

        # 读取并解析字幕
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            with open(file_path, 'r', encoding='gbk') as f:
                content = f.read()

        parsed_data = self.parse_srt(content)

        if is_left:
            self.srt1_data = parsed_data
            self.btn_load_left.setText(f"左侧: {file_path.split('/')[-1]}")
        else:
            self.srt2_data = parsed_data
            self.btn_load_right.setText(f"右侧: {file_path.split('/')[-1]}")

        # 如果两边都有数据了，就触发更新
        if self.srt1_data or self.srt2_data:
            self.render_diff()

    def parse_srt(self, text):
        """
        容错率较高的 SRT 解析器
        返回字典: {序号(int): 文本内容(str)}
        """
        blocks = text.strip().split('\n\n')
        parsed = {}

        for block in blocks:
            lines = block.split('\n')
            if len(lines) >= 3:
                # 寻找包含 '-->' 的行作为时间轴
                time_idx = -1
                for i, line in enumerate(lines):
                    if '-->' in line:
                        time_idx = i
                        break

                if time_idx > 0:
                    # 时间轴上面那行是序号
                    idx_str = lines[time_idx - 1]
                    # 提取数字序号（忽略你样例中的 这类标签）
                    nums = re.findall(r'\d+', idx_str)
                    if nums:
                        seq_num = int(nums[-1])  # 取最后一个数字作为序号
                        # 时间轴下面的是文本内容
                        content = '\n'.join(lines[time_idx + 1:])
                        parsed[seq_num] = content
        return parsed

    def generate_diff_html(self, text1, text2):
        """
        利用 difflib 逐字符比对，返回包裹了 HTML 颜色的富文本
        """
        sm = SequenceMatcher(None, text1, text2)
        html1, html2 = "", ""

        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            # 替换换行符以适应 HTML 显示
            part1 = text1[i1:i2].replace('\n', '<br>')
            part2 = text2[j1:j2].replace('\n', '<br>')

            if tag == 'equal':
                html1 += part1
                html2 += part2
            elif tag == 'delete':
                html1 += f'<span style="background-color: #ffeef0; color: #b31d28; text-decoration: line-through;">{part1}</span>'
            elif tag == 'insert':
                html2 += f'<span style="background-color: #e6ffed; color: #22863a;">{part2}</span>'
            elif tag == 'replace':
                html1 += f'<span style="background-color: #ffeef0; color: #b31d28; text-decoration: line-through;">{part1}</span>'
                html2 += f'<span style="background-color: #e6ffed; color: #22863a;">{part2}</span>'

        return html1, html2

    def create_cell_widget(self, html_content, seq_num):
        """创建一个用来显示富文本的 QTextBrowser"""
        browser = QTextBrowser()
        # 禁用组件本身的滚动条，把滚动权交还给外层的 TableWidget
        browser.setVerticalScrollBarPolicy(sys.modules['PyQt6.QtCore'].Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        browser.setHorizontalScrollBarPolicy(sys.modules['PyQt6.QtCore'].Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        browser.setFrameStyle(0)  # 无边框
        browser.setStyleSheet("background-color: transparent;")

        # 加上序号标识方便查看
        display_html = f"<div style='color: #666; font-size: 12px; margin-bottom: 4px;'>[段落 {seq_num}]</div>{html_content}"
        browser.setHtml(display_html)
        return browser

    def render_diff(self):
        self.table.setRowCount(0)

        # 获取所有出现过的序号，合并并排序
        all_keys = set(self.srt1_data.keys()).union(set(self.srt2_data.keys()))
        sorted_keys = sorted(list(all_keys))

        self.table.setRowCount(len(sorted_keys))

        for row_idx, seq_num in enumerate(sorted_keys):
            text1 = self.srt1_data.get(seq_num, "")
            text2 = self.srt2_data.get(seq_num, "")

            # 计算差异 HTML
            html1, html2 = self.generate_diff_html(text1, text2)

            # 填入左侧单元格
            browser_left = self.create_cell_widget(html1, seq_num)
            self.table.setCellWidget(row_idx, 0, browser_left)

            # 填入右侧单元格
            browser_right = self.create_cell_widget(html2, seq_num)
            self.table.setCellWidget(row_idx, 1, browser_right)

        # 根据内容自动调整每一行的高度，完美对齐！
        self.table.resizeRowsToContents()


if __name__ == '__main__':
    app = QApplication(sys.argv)

    # 设置一个好看点的全局字体
    font = QFont("Segoe UI", 10)
    app.setFont(font)

    viewer = SrtDiffViewer()
    viewer.show()
    sys.exit(app.exec())
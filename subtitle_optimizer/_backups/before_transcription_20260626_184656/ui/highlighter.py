import re
from PyQt6.QtGui import QSyntaxHighlighter, QTextCharFormat, QColor, QFont

class SRTHighlighter(QSyntaxHighlighter):
    def __init__(self, parent):
        super().__init__(parent)
        self._index_fmt = QTextCharFormat()
        self._index_fmt.setForeground(QColor("#58A6FF"))
        self._index_fmt.setFontWeight(QFont.Weight.Bold)

        self._time_fmt = QTextCharFormat()
        self._time_fmt.setForeground(QColor("#3FB950"))

        self._zh_fmt = QTextCharFormat()
        self._zh_fmt.setForeground(QColor("#E3B341"))

        self._en_fmt = QTextCharFormat()
        self._en_fmt.setForeground(QColor("#8B949E"))

    def highlightBlock(self, text: str):
        if re.fullmatch(r'\d+', text.strip()):
            self.setFormat(0, len(text), self._index_fmt)
        elif '-->' in text:
            self.setFormat(0, len(text), self._time_fmt)
        elif any('\u4e00' <= c <= '\u9fff' for c in text):
            self.setFormat(0, len(text), self._zh_fmt)
        else:
            self.setFormat(0, len(text), self._en_fmt)
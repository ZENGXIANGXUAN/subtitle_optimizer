# 术语表改为左右单元格形式 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将设置页面的术语表从 QTextEdit 自由文本改为 QTableWidget 表格布局，提供更直观的编辑体验

**Architecture:** 在 `ui/main_window.py` 中，将 QTextEdit 替换为 QTableWidget（两列：英文/中文），添加/删除按钮放在表格下方。新增辅助方法处理表格与 `English->中文` 格式的互转。配置文件格式保持不变。

**Tech Stack:** PyQt6 (QTableWidget, QTableWidgetItem, QPushButton)

## Global Constraints

- 配置文件格式保持 `English->中文` 不变，确保向后兼容
- 所有现有功能（单文件处理、批量处理）的术语表调用逻辑不变
- 不新增文件，仅修改 `ui/main_window.py`

---

### Task 1: 添加 QTableWidget 导入

**Files:**
- Modify: `subtitle_optimizer/ui/main_window.py:8-15`

**Interfaces:**
- Produces: QTableWidget 和 QTableWidgetItem 可用

- [ ] **Step 1: 修改导入语句**

在 `from PyQt6.QtWidgets import (...)` 中添加 `QTableWidget, QTableWidgetItem`：

```python
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QTextEdit, QProgressBar,
    QSplitter, QFrame, QScrollArea, QComboBox,
    QGroupBox, QLineEdit, QStatusBar, QTabWidget, QMessageBox,
    QCheckBox, QGridLayout, QListWidget, QListWidgetItem,
    QSizePolicy, QTableWidget, QTableWidgetItem
)
```

- [ ] **Step 2: 验证导入**

运行: `python -c "from PyQt6.QtWidgets import QTableWidget, QTableWidgetItem; print('OK')"`
预期: OK

- [ ] **Step 3: 提交**

```bash
git add subtitle_optimizer/ui/main_window.py
git commit -m "feat(ui): 添加 QTableWidget 导入"
```

---

### Task 2: 替换 QTextEdit 为 QTableWidget

**Files:**
- Modify: `subtitle_optimizer/ui/main_window.py:608-657`

**Interfaces:**
- Consumes: `_build_settings_page()` 中的 `rc` (QVBoxLayout)
- Produces: `self.table_glossary` (QTableWidget), 添加/删除按钮

- [ ] **Step 1: 替换术语表 UI 组件**

将 `right_card` 区域的 QTextEdit 替换为 QTableWidget + 按钮：

```python
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

# 术语表表格
self.table_glossary = QTableWidget()
self.table_glossary.setColumnCount(2)
self.table_glossary.setHorizontalHeaderLabels(["英文 (Source)", "中文 (Target)"])
self.table_glossary.horizontalHeader().setStretchLastSection(True)
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
btn_add.clicked.connect(self._glossary_add_row)
btn_del.clicked.connect(self._glossary_del_row)
btn_row.addWidget(btn_add)
btn_row.addWidget(btn_del)
btn_row.addStretch()
rc.addLayout(btn_row)

gloss_note = QLabel("翻译时 AI 将强制使用上述术语，适用于单文件和批量处理。")
gloss_note.setStyleSheet("color: #8a7b66; font-size: 11px;")
gloss_note.setWordWrap(True)
rc.addWidget(gloss_note)

cols.addWidget(right_card, 1)
```

- [ ] **Step 2: 验证 UI 渲染**

运行: `python -c "from ui.main_window import MainWindow; print('OK')"`
预期: OK（无导入错误）

- [ ] **Step 3: 提交**

```bash
git add subtitle_optimizer/ui/main_window.py
git commit -m "feat(ui): 替换 QTextEdit 为 QTableWidget 表格布局"
```

---

### Task 3: 添加辅助方法

**Files:**
- Modify: `subtitle_optimizer/ui/main_window.py` (在 `_get_glossary_prompt` 方法前)

**Interfaces:**
- Produces: `_glossary_add_row()`, `_glossary_del_row()`, `_glossary_to_text()`, `_glossary_from_text()`

- [ ] **Step 1: 添加辅助方法**

在 `_get_glossary_prompt` 方法前添加：

```python
def _glossary_add_row(self):
    """在术语表末尾添加空行"""
    row = self.table_glossary.rowCount()
    self.table_glossary.insertRow(row)
    self.table_glossary.setItem(row, 0, QTableWidgetItem(""))
    self.table_glossary.setItem(row, 1, QTableWidgetItem(""))
    self.table_glossary.editItem(self.table_glossary.item(row, 0))

def _glossary_del_row(self):
    """删除选中的术语表行"""
    row = self.table_glossary.currentRow()
    if row >= 0:
        self.table_glossary.removeRow(row)

def _glossary_to_text(self) -> str:
    """将表格内容转换为 English->中文 格式文本"""
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
```

- [ ] **Step 2: 验证方法存在**

运行: `python -c "from ui.main_window import MainWindow; print('OK')"`
预期: OK

- [ ] **Step 3: 提交**

```bash
git add subtitle_optimizer/ui/main_window.py
git commit -m "feat(ui): 添加术语表辅助方法"
```

---

### Task 4: 修改配置加载和保存

**Files:**
- Modify: `subtitle_optimizer/ui/main_window.py:1558-1604`

**Interfaces:**
- Consumes: `_glossary_to_text()`, `_glossary_from_text()`
- Produces: 配置加载/保存使用表格数据

- [ ] **Step 1: 修改 `_save_config()`**

将 `"glossary": self.input_glossary.toPlainText().strip()` 改为 `"glossary": self._glossary_to_text()`：

```python
def _save_config(self):
    cfg = {
        "api_key": self.input_apikey.text().strip(),
        "base_url": self.input_base_url.text().strip(),
        "model": self.combo_model.currentText(),
        "concurrency": self.input_concurrency.text(),
        "batch_size": self.input_batch.text(),
        "original_line": self.combo_original_line.currentIndex(),
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
```

- [ ] **Step 2: 修改 `_load_config()`**

将 `self.input_glossary.setPlainText(cfg.get("glossary", ""))` 改为 `self._glossary_from_text(cfg.get("glossary", ""))`：

```python
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
```

- [ ] **Step 3: 验证配置加载**

运行: `python -c "from ui.main_window import MainWindow; print('OK')"`
预期: OK

- [ ] **Step 4: 提交**

```bash
git add subtitle_optimizer/ui/main_window.py
git commit -m "feat(ui): 配置加载/保存改用表格数据"
```

---

### Task 5: 修改术语表调用点

**Files:**
- Modify: `subtitle_optimizer/ui/main_window.py:1543-1553`
- Modify: `subtitle_optimizer/ui/main_window.py:869`

**Interfaces:**
- Consumes: `_glossary_to_text()`
- Produces: `_get_glossary_prompt()` 返回格式化后的术语表字符串

- [ ] **Step 1: 修改 `_get_glossary_prompt()`**

将 `raw = self.input_glossary.toPlainText().strip()` 改为 `raw = self._glossary_to_text()`：

```python
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
```

- [ ] **Step 2: 修改 `_batch_start()`**

将 `raw_glossary = self.input_glossary.toPlainText().strip()` 改为 `raw_glossary = self._glossary_to_text()`：

```python
raw_glossary = self._glossary_to_text()
```

- [ ] **Step 3: 验证术语表调用**

运行: `python -c "from ui.main_window import MainWindow; print('OK')"`
预期: OK

- [ ] **Step 4: 提交**

```bash
git add subtitle_optimizer/ui/main_window.py
git commit -m "feat(ui): 术语表调用点改用表格数据"
```

---

### Task 6: 集成测试

**Files:**
- Test: `subtitle_optimizer/ui/main_window.py` (手动测试)

**Interfaces:**
- Consumes: 所有修改后的方法

- [ ] **Step 1: 启动应用验证 UI**

运行: `cd subtitle_optimizer && python -m ui.main_window`
预期: 应用启动，设置页面显示表格布局

- [ ] **Step 2: 测试添加行功能**

操作: 点击「添加行」按钮
预期: 表格末尾新增一行，光标自动聚焦到英文列

- [ ] **Step 3: 测试删除行功能**

操作: 选中某行，点击「删除行」按钮
预期: 选中行被删除

- [ ] **Step 4: 测试配置保存/加载**

操作: 添加几条术语，保存配置，重启应用
预期: 术语表内容正确加载

- [ ] **Step 5: 测试术语表功能**

操作: 添加术语，运行翻译任务
预期: 术语表被正确传递给 AI

- [ ] **Step 6: 提交最终版本**

```bash
git add subtitle_optimizer/ui/main_window.py
git commit -m "feat(ui): 术语表改为左右单元格形式完成"
```

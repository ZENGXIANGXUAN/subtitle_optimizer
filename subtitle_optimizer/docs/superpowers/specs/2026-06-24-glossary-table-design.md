# 术语表改为左右单元格形式

## 问题

当前术语表使用 QTextEdit 自由文本输入，用户需要手动输入 `English->中文` 格式。这种方式：
- 容易格式错误（漏写 `->`、使用错误分隔符）
- 不够直观，需要记住格式
- 无法方便地编辑单条术语

## 方案

将 QTextEdit 替换为 QTableWidget，提供左右两列表格布局，用户通过按钮添加/删除行。

### UI 设计

```
┌─────────────────────────────────────────────┐
│ 📖 固定词语翻译（术语表）                      │
├─────────────────────────────────────────────┤
│ │ 英文 (Source)    │ 中文 (Target)          │
│ ├──────────────────┼────────────────────────│
│ │ Algorithm        │ 算法                    │
│ │ CSID             │ 交付状态变化             │
│ │ Deployment       │ 部署                    │
│ │ Pipeline         │ 流水线                  │
│ └──────────────────┴────────────────────────│
│  [+ 添加行]  [- 删除行]                       │
│                                              │
│ 翻译时 AI 将强制使用上述术语，适用于单文件和批量处理。 │
└─────────────────────────────────────────────┘
```

### 数据格式

**存储格式**（配置文件）：保持现有 `English->中文` 格式
```
Algorithm->算法
CSID->交付状态变化
Deployment->部署
Pipeline->流水线
```

**内部转换**：
- 加载时：解析 `English->中文` 文本 → 填充表格行
- 保存时：遍历表格行 → 拼接为 `English->中文` 文本

### 交互设计

1. **添加行**：点击「添加行」按钮，在表格末尾新增一行空行，光标自动聚焦到新行的英文列
2. **删除行**：选中某行后，点击「删除行」按钮删除该行
3. **编辑**：直接双击单元格进行编辑
4. **空行处理**：保存时自动过滤空行

## 改动清单

### 1. 修改 `_build_settings_page()` — `ui/main_window.py`

将 QTextEdit 替换为 QTableWidget + 按钮：

```python
# 术语表表格
self.table_glossary = QTableWidget()
self.table_glossary.setColumnCount(2)
self.table_glossary.setHorizontalHeaderLabels(["英文 (Source)", "中文 (Target)"])
self.table_glossary.horizontalHeader().setStretchLastSection(True)
self.table_glossary.setStyleSheet("""...""")
rc.addWidget(self.table_glossary, 1)

# 按钮行
btn_row = QHBoxLayout()
btn_add = QPushButton("+ 添加行")
btn_del = QPushButton("- 删除行")
btn_add.clicked.connect(self._glossary_add_row)
btn_del.clicked.connect(self._glossary_del_row)
btn_row.addWidget(btn_add)
btn_row.addWidget(btn_del)
btn_row.addStretch()
rc.addLayout(btn_row)
```

### 2. 新增辅助方法 — `ui/main_window.py`

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

### 3. 修改 `_get_glossary_prompt()` — `ui/main_window.py:1543`

```python
def _get_glossary_prompt(self, entries=None) -> str:
    raw = self._glossary_to_text()  # 改为从表格获取
    if not raw:
        return ""
    if entries:
        from core.workers import filter_glossary_by_entries
        raw = filter_glossary_by_entries(raw, entries)
    return _format_glossary_prompt(raw)
```

### 4. 修改 `_save_config()` — `ui/main_window.py:1558`

```python
cfg = {
    ...
    "glossary": self._glossary_to_text(),  # 改为从表格获取
    ...
}
```

### 5. 修改 `_load_config()` — `ui/main_window.py:1590`

```python
self._glossary_from_text(cfg.get("glossary", ""))  # 改为加载到表格
```

### 6. 修改 `_batch_start()` — `ui/main_window.py:869`

```python
raw_glossary = self._glossary_to_text()  # 改为从表格获取
```

## 不改的部分

- `_format_glossary_prompt()` 格式化逻辑不变
- `filter_glossary_by_entries()` 过滤逻辑不变
- Worker 层代码不变
- 配置文件格式不变（仍为 `English->中文`）

## 预期效果

- 用户通过表格直观查看和编辑术语
- 减少格式错误
- 保持与现有配置文件的兼容性

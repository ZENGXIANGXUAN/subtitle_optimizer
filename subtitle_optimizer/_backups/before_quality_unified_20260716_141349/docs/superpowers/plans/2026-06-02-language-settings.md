# 语言设置功能实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在设置页面添加输入语言和输出语言配置，支持任意源语言到任意目标语言的翻译

**Architecture:** 最小改动方案，语言作为 Prompt 参数传递，修改 UI 和 prompt 函数

**Tech Stack:** PyQt6, Python, Mistral AI API

---

## 文件结构

| 文件 | 改动类型 | 职责 |
|-----|---------|------|
| `ui/main_window.py` | 修改 | 添加语言选择 UI、更新配置保存/加载 |
| `core/workers.py` | 修改 | 更新 prompt 中的语言引用 |

---

### Task 1: 添加语言常量和映射

**Files:**
- Modify: `core/workers.py:1-35`

- [ ] **Step 1: 在 workers.py 顶部添加语言常量**

```python
# 语言映射：显示标签 -> (语言代码, Prompt 名称)
LANGUAGE_MAP = {
    "中文": ("zh", "中文"),
    "English": ("en", "English"),
    "意大利语 (Italiano)": ("it", "Italian"),
    "日语 (日本語)": ("ja", "Japanese"),
    "韩语 (한국어)": ("ko", "Korean"),
    "法语 (Français)": ("fr", "French"),
    "德语 (Deutsch)": ("de", "German"),
    "西班牙语 (Español)": ("es", "Spanish"),
    "葡萄牙语 (Português)": ("pt", "Portuguese"),
    "俄语 (Русский)": ("ru", "Russian"),
}

# 语言代码 -> Prompt 名称
LANG_CODE_TO_PROMPT = {code: prompt for _, (code, prompt) in LANGUAGE_MAP.items()}

# 显示标签列表（用于 UI 下拉框）
LANGUAGE_LABELS = list(LANGUAGE_MAP.keys())
```

- [ ] **Step 2: 验证常量定义正确**

Run: `python -c "from core.workers import LANGUAGE_MAP, LANGUAGE_LABELS; print(len(LANGUAGE_LABELS), 'languages')"`
Expected: `10 languages`

- [ ] **Step 3: Commit**

```bash
git add core/workers.py
git commit -m "feat: 添加语言常量和映射定义"
```

---

### Task 2: 添加语言选择 UI

**Files:**
- Modify: `ui/main_window.py:565-578` (在「原文行数」下方添加)

- [ ] **Step 1: 导入语言常量**

在 `ui/main_window.py` 顶部的导入区域添加：

```python
from core.workers import LANGUAGE_MAP, LANGUAGE_LABELS
```

- [ ] **Step 2: 在 `_build_api_page` 方法中添加语言设置 UI**

在 `lc.addLayout(order_row)` 之后（约第 578 行）添加：

```python
lc.addWidget(_section_lbl("语言设置"))
lang_row = QHBoxLayout()
lang_row.setSpacing(12)

# 输入语言
in_lang_wrap = QVBoxLayout()
in_lang_lbl = QLabel("输入语言")
in_lang_lbl.setStyleSheet("color: #8a7b66; font-size: 11px;")
in_lang_wrap.addWidget(in_lang_lbl)
self.combo_input_lang = QComboBox()
self.combo_input_lang.addItems(LANGUAGE_LABELS)
self.combo_input_lang.setCurrentText("English")
in_lang_wrap.addWidget(self.combo_input_lang)
lang_row.addLayout(in_lang_wrap)

# 输出语言
out_lang_wrap = QVBoxLayout()
out_lang_lbl = QLabel("输出语言")
out_lang_lbl.setStyleSheet("color: #8a7b66; font-size: 11px;")
out_lang_wrap.addWidget(out_lang_lbl)
self.combo_output_lang = QComboBox()
self.combo_output_lang.addItems(LANGUAGE_LABELS)
self.combo_output_lang.setCurrentText("中文")
out_lang_wrap.addWidget(self.combo_output_lang)
lang_row.addLayout(out_lang_wrap)

lc.addLayout(lang_row)
```

- [ ] **Step 3: 验证 UI 显示正确**

Run: `python -c "from ui.main_window import MainWindow; print('Import OK')"`
Expected: `Import OK`

- [ ] **Step 4: Commit**

```bash
git add ui/main_window.py
git commit -m "feat: 添加语言选择 UI 组件"
```

---

### Task 3: 更新配置保存和加载

**Files:**
- Modify: `ui/main_window.py:1514-1549`

- [ ] **Step 1: 更新 `_save_config` 方法**

在 `_save_config` 方法的 `cfg` 字典中添加：

```python
"input_language": self.combo_input_lang.currentText(),
"output_language": self.combo_output_lang.currentText(),
```

完整方法：

```python
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
        self.status_bar.showMessage("配置已保存")
    except Exception as e:
        self._log(f"⚠️ 保存配置失败：{e}")
```

- [ ] **Step 2: 更新 `_load_config` 方法**

在 `_load_config` 方法中添加语言配置加载：

```python
# 加载语言设置（向后兼容）
input_lang = cfg.get("input_language", "English")
output_lang = cfg.get("output_language", "中文")
if input_lang in LANGUAGE_LABELS:
    self.combo_input_lang.setCurrentText(input_lang)
if output_lang in LANGUAGE_LABELS:
    self.combo_output_lang.setCurrentText(output_lang)
```

- [ ] **Step 3: 验证配置保存/加载**

Run: `python -c "from ui.main_window import MainWindow; print('Config methods OK')"`
Expected: `Config methods OK`

- [ ] **Step 4: Commit**

```bash
git add ui/main_window.py
git commit -m "feat: 更新配置保存/加载支持语言设置"
```

---

### Task 4: 添加语言获取辅助函数

**Files:**
- Modify: `core/workers.py:36-58` (在 `_run_loop` 函数之后)

- [ ] **Step 1: 添加获取 Prompt 语言名称的辅助函数**

```python
def _get_prompt_lang(language_label: str) -> str:
    """从显示标签获取 Prompt 中使用的语言名称"""
    if language_label in LANGUAGE_MAP:
        return LANGUAGE_MAP[language_label][1]
    return language_label
```

- [ ] **Step 2: 验证辅助函数**

Run: `python -c "from core.workers import _get_prompt_lang; print(_get_prompt_lang('意大利语 (Italiano)'))"`
Expected: `Italian`

- [ ] **Step 3: Commit**

```bash
git add core/workers.py
git commit -m "feat: 添加语言获取辅助函数"
```

---

### Task 5: 更新 `_build_analysis_messages` 函数

**Files:**
- Modify: `core/workers.py:79-114`

- [ ] **Step 1: 修改函数签名添加语言参数**

```python
def _build_analysis_messages(entries: list[SubtitleEntry], 
                            input_lang: str = "English",
                            output_lang: str = "中文") -> list[dict]:
```

- [ ] **Step 2: 更新 system_prompt 中的语言引用**

将硬编码的"中文"替换为 `{output_lang}`：

```python
system_prompt = (
    "你是资深字幕翻译策略顾问。你的任务不是复述内容，而是从字幕样本中提炼一份"
    "可直接用于后续机器翻译优化的"翻译执行规约"。\n\n"
    "请严格返回 JSON，不加任何解释、Markdown 或代码块标记。格式如下：\n"
    "{\n"
    '  "domain": "内容领域，如交易课程/软件教程/访谈/纪录片",\n'
    '  "audience": "目标受众及其知识水平",\n'
    f'  "tone": "{output_lang}译文应采用的人称、语气、正式程度和教学节奏",\n'
    '  "context": "200字以内，说明主题、场景、核心概念、说话者意图和常见理解难点",\n'
    '  "translation_rules": ["3-5条可执行规则，直接指导逐条字幕怎么译"],\n'
    '  "term_rules": [\n'
    '    {"source": "样本中的英文术语或高频表达", "target": "建议译法", "note": "保留/意译/统一用法说明"}\n'
    '  ],\n'
    '  "avoid": ["2-4条需要避免的问题，如直译腔、误译方向、过度解释、术语不统一"],\n'
    f'  "style_guide": "150字以内，写给后续翻译AI的综合指令，必须具体、可执行，目标语言是{output_lang}"\n'
    "}\n\n"
    "要求：\n"
    "1. 优先识别对后续翻译有约束力的信息：领域术语、固定译法、语气、人称、句子长短、保留英文缩写的规则。\n"
    "2. 不要写空泛摘要，例如"语言自然流畅"；必须说明怎样才算自然流畅。\n"
    "3. term_rules 只收录样本中真实出现、且可能影响一致性的词或表达；没有把握时在 note 中说明。\n"
    "4. 如果前30条偏开场白，也要从样本里的术语和表达推断可迁移的翻译规则，不要只总结开场内容。\n"
    f"5. 所有字段必须使用{output_lang}填写；source 字段可保留{input_lang}原词。"
)
```

- [ ] **Step 3: Commit**

```bash
git add core/workers.py
git commit -m "feat: 更新 _build_analysis_messages 支持语言参数"
```

---

### Task 6: 更新 `_optimize_batch_impl` 函数

**Files:**
- Modify: `core/workers.py:172-311`

- [ ] **Step 1: 修改函数签名添加语言参数**

```python
async def _optimize_batch_impl(
    batch: list[SubtitleEntry],
    context: str,
    glossary: str,
    client: MistralClient,
    session: aiohttp.ClientSession,
    input_lang: str = "English",
    output_lang: str = "中文",
    prev_ctx: list[SubtitleEntry] | None = None,
) -> dict[int, list[str]]:
```

- [ ] **Step 2: 更新 sys_content 中的语言引用**

```python
sys_content = (
    f"你是专业字幕翻译优化师。\n"
    f"【背景与风格】\n{context}\n\n"
    f"任务：优化双语字幕的{output_lang}翻译，使译文准确、自然、符合背景风格。\n"
)
sys_content += (
    "\n【重要前提】SRT字幕按时间轴逐句切分，一个完整语义常被拆成多条。"
    f"输入的zh字段是初译，可能存在错误。"
    "因此：必须以en为唯一翻译依据，zh仅供用词风格参考，不得直接沿用。\n\n"
    "规则：\n"
    f"1. 【锚定en】每条zh必须且只能对应该条en的内容——en是完整句就译完整句，en是半句就译对应的半句{output_lang}\n"
    f"2. 在忠实en的前提下，使{output_lang}自然流畅，符合【背景与风格】的语气和用词\n"
    "3. 专业术语保持批次内一致性\n"
    f"4. 【严禁】zh只允许纯文本，禁止任何Markdown符号（**、*、`、#、>、-列表等）\n"
    "5. 【严禁】zh只含译文正文，不得添加括号注释、标注或说明文字\n"
    "6. 【严禁】不得合并条目，输入几条返回几条，id严格一一对应\n"
    "7. 【严禁】en字段原样返回，一字不差，不得修改或截断\n"
    "8. 若en为空，zh也留空\n"
    "9. 严格按JSON格式返回，不加任何解释\n"
    f"10. 【严禁漏译 — 逐词对照】zh必须完整翻译en中的每个语义单元：主语、谓语、宾语、状语、从句、插入语、过渡词（如okay/now/well/so）、强调词（如very/always/important）。不得跳过、省略、或合并任何信息点\n"
    "11. 【严禁摘要】不得把长英文压缩成一句概括性中文。en有多长，zh就译多长。宁可使用较长中文，也不能遗漏信息点\n"
    "12. 【逐句自检 — 强制执行】返回前逐条朗读en，逐词检查zh是否覆盖了：谁（主语）→做了什么（动作）→为什么/怎么（条件/方式）→结果如何。每检查完一条在心里打勾，缺一不可\n\n"
    '返回格式：[{"id": 1, "zh": "优化后中文", "en": "英文原文原样返回"}]'
)
```

- [ ] **Step 3: 更新 retry_sys 中的语言引用**

```python
retry_sys = (
    f"你是专业字幕翻译师。\n【背景与风格】\n{context}\n\n"
    f"任务：将{input_lang}字幕翻译成{output_lang}，严格遵循上述风格指引（如有）。\n"
)
retry_sys += (
    "规则：\n1. 用词语气与风格指引一致\n"
    "2. 严格按JSON格式返回，不要加任何解释\n"
    "3. 每条必须有zh字段，不得为空\n"
    "4. 【严禁重复翻译】每条只译自己的en内容，en是半句就译半句，不得让相邻条目的zh相同或重复\n"
    "5. 【严禁摘要】不得把长英文压缩成一句概括性中文，必须完整覆盖en中的全部信息点\n"
    "6. 【覆盖自检】返回前逐条检查：zh是否完整覆盖了en的主语、动作、条件、原因、结果和关键术语\n\n"
    '返回格式：[{"id": 1, "zh": "中文翻译"}]'
)
```

- [ ] **Step 4: Commit**

```bash
git add core/workers.py
git commit -m "feat: 更新 _optimize_batch_impl 支持语言参数"
```

---

### Task 7: 更新 `_translate_batch_impl` 函数

**Files:**
- Modify: `core/workers.py:440-484`

- [ ] **Step 1: 修改函数签名添加语言参数**

```python
async def _translate_batch_impl(
    batch: list[SubtitleEntry],
    context: str,
    glossary: str,
    client: MistralClient,
    session: aiohttp.ClientSession,
    input_lang: str = "English",
    output_lang: str = "中文",
) -> dict[int, str]:
```

- [ ] **Step 2: 更新 system_content 中的语言引用**

```python
system_content = (
    f"你是专业字幕翻译师。\n【背景与风格】\n{context}\n\n"
    f"任务：将{input_lang}字幕翻译成{output_lang}，严格遵循上述风格指引（如有）。\n"
    "规则：\n"
    "1. 用词语气与风格指引一致，贴合视频语境\n"
    "2. 严格按JSON格式返回，不要加任何解释\n"
    "3. 每条必须有zh字段，不得为空或返回None\n"
    "4. 【严禁】不得合并条目，输入几条返回几条，id一一对应\n"
    "5. 【严禁重复翻译】每条只译自己的en内容，en是半句就译半句，不得让相邻条目的zh相同或重复\n\n"
    '返回格式：[{"id": 1, "zh": "中文翻译"}]'
)
```

- [ ] **Step 3: Commit**

```bash
git add core/workers.py
git commit -m "feat: 更新 _translate_batch_impl 支持语言参数"
```

---

### Task 8: 更新 Worker 类传递语言参数

**Files:**
- Modify: `core/workers.py:314-438` (AnalysisWorker, OptimizeWorker)
- Modify: `core/workers.py:698-762` (TranslateWorker)
- Modify: `core/workers.py:765-1083` (BatchWorker)

- [ ] **Step 1: 更新 AnalysisWorker**

修改 `__init__` 添加语言参数：

```python
def __init__(self, client: MistralClient, entries: list[SubtitleEntry],
             input_lang: str = "English", output_lang: str = "中文"):
    super().__init__()
    self.client = client
    self.entries = entries
    self.input_lang = input_lang
    self.output_lang = output_lang
    logger.debug(f"AnalysisWorker初始化完成，待处理条目数: {len(entries)}")
```

更新 `_run` 方法中的调用：

```python
messages = _build_analysis_messages(self.entries, self.input_lang, self.output_lang)
```

- [ ] **Step 2: 更新 OptimizeWorker**

修改 `__init__` 添加语言参数：

```python
def __init__(self, client: MistralClient, entries: list[SubtitleEntry],
             context: str, glossary: str, concurrency: int = 12, batch_size: int = 5,
             input_lang: str = "English", output_lang: str = "中文"):
    super().__init__()
    self.client = client
    self.entries = entries
    self.context = context
    self.glossary = glossary
    self.concurrency = concurrency
    self.batch_size = batch_size
    self.input_lang = input_lang
    self.output_lang = output_lang
    logger.debug(
        f"OptimizeWorker初始化完成，待处理条目数: {len(entries)}, 并发数: {concurrency}, 批次大小: {batch_size}")
```

更新 `_optimize_batch` 方法：

```python
async def _optimize_batch(self, batch: list[SubtitleEntry],
                          session: aiohttp.ClientSession,
                          prev_ctx: list[SubtitleEntry] | None = None) -> dict[int, list[str]]:
    return await _optimize_batch_impl(batch, self.context, self.glossary, 
                                      self.client, session, 
                                      self.input_lang, self.output_lang, prev_ctx)
```

- [ ] **Step 3: 更新 TranslateWorker**

修改 `__init__` 添加语言参数：

```python
def __init__(self, client: MistralClient, entries: list[SubtitleEntry],
             context: str, glossary: str, concurrency: int = 12, batch_size: int = 10,
             input_lang: str = "English", output_lang: str = "中文"):
    super().__init__()
    self.client = client
    self.entries = entries
    self.context = context
    self.glossary = glossary
    self.concurrency = concurrency
    self.batch_size = batch_size
    self.input_lang = input_lang
    self.output_lang = output_lang
```

更新 `_run` 方法中的调用：

```python
result_map = await _translate_batch_impl(
    batch, self.context, self.glossary, self.client, session,
    self.input_lang, self.output_lang)
```

- [ ] **Step 4: 更新 BatchWorker**

修改 `__init__` 添加语言参数：

```python
def __init__(self, client: MistralClient, files: list[str],
             out_dir: Optional[str], concurrency: int, batch_size: int,
             overwrite: bool = False, chinese_first: bool = True,
             glossary_prompt: str = "", original_line: int = 1,
             input_lang: str = "English", output_lang: str = "中文"):
    super().__init__()
    self.client = client
    self.files = files
    self.out_dir = out_dir
    self.concurrency = concurrency
    self.batch_size = batch_size
    self.overwrite = overwrite
    self.chinese_first = chinese_first
    self.glossary_prompt = glossary_prompt
    self.original_line = original_line
    self.input_lang = input_lang
    self.output_lang = output_lang
```

更新 `_analyze` 方法：

```python
async def _analyze(self, entries: list[SubtitleEntry]) -> str:
    messages = _build_analysis_messages(entries, self.input_lang, self.output_lang)
    async with aiohttp.ClientSession() as session:
        raw = await self.client.chat(messages, session)
    return _format_analysis_result(raw)
```

更新 `_translate` 方法中的调用：

```python
result_map = await _translate_batch_impl(
    batch, context, glossary, self.client, session,
    self.input_lang, self.output_lang)
```

更新 `_optimize_batch` 方法：

```python
async def _optimize_batch(self, batch: list[SubtitleEntry], context: str, glossary: str,
                          session: aiohttp.ClientSession,
                          prev_ctx: list[SubtitleEntry] | None = None) -> dict[int, list[str]]:
    return await _optimize_batch_impl(batch, context, glossary, 
                                      self.client, session,
                                      self.input_lang, self.output_lang, prev_ctx)
```

- [ ] **Step 5: Commit**

```bash
git add core/workers.py
git commit -m "feat: 更新所有 Worker 类支持语言参数传递"
```

---

### Task 9: 更新日志消息

**Files:**
- Modify: `core/workers.py:603-609` (SegmentWorker)
- Modify: `core/workers.py:895-897` (BatchWorker)

- [ ] **Step 1: 更新 SegmentWorker 日志**

```python
if self._is_native:
    self.log.emit(f"  📋 检测到原生字幕，将自动翻译为{self.output_lang}")
```

- [ ] **Step 2: 更新 BatchWorker 日志**

```python
if is_native:
    self.log.emit(f"    📋 检测到原生字幕，将自动翻译为{self.output_lang}")
```

- [ ] **Step 3: Commit**

```bash
git add core/workers.py
git commit -m "feat: 更新日志消息使用动态语言名称"
```

---

### Task 10: 更新 MainWindow 调用 Worker 时传递语言参数

**Files:**
- Modify: `ui/main_window.py` (查找创建 Worker 的地方)

- [ ] **Step 1: 添加获取语言设置的辅助方法**

在 `MainWindow` 类中添加：

```python
def _get_language_settings(self):
    """获取当前语言设置"""
    input_lang = self.combo_input_lang.currentText()
    output_lang = self.combo_output_lang.currentText()
    return input_lang, output_lang
```

- [ ] **Step 2: 更新 AnalysisWorker 创建**

找到创建 `AnalysisWorker` 的地方，添加语言参数：

```python
input_lang, output_lang = self._get_language_settings()
self._analysis_worker = AnalysisWorker(
    self.client, entries, input_lang, output_lang)
```

- [ ] **Step 3: 更新 OptimizeWorker 创建**

```python
input_lang, output_lang = self._get_language_settings()
self._optimize_worker = OptimizeWorker(
    self.client, entries, context, glossary,
    concurrency, batch_size, input_lang, output_lang)
```

- [ ] **Step 4: 更新 TranslateWorker 创建**

```python
input_lang, output_lang = self._get_language_settings()
self._translate_worker = TranslateWorker(
    self.client, entries, context, glossary,
    concurrency, batch_size, input_lang, output_lang)
```

- [ ] **Step 5: 更新 BatchWorker 创建**

```python
input_lang, output_lang = self._get_language_settings()
self._batch_worker = BatchWorker(
    self.client, files, out_dir, concurrency, batch_size,
    overwrite, chinese_first, glossary_prompt, original_line,
    input_lang, output_lang)
```

- [ ] **Step 6: Commit**

```bash
git add ui/main_window.py
git commit -m "feat: 更新 MainWindow 传递语言参数到 Worker"
```

---

### Task 11: 处理输入输出语言相同的特殊场景

**Files:**
- Modify: `core/workers.py:718-722` (TranslateWorker._run)
- Modify: `core/workers.py:989-994` (BatchWorker._translate)

- [ ] **Step 1: 更新 TranslateWorker._run 处理语言相同**

```python
async def _run(self):
    # 检测是否需要翻译（输入输出语言相同时跳过）
    if self.input_lang == self.output_lang:
        self.log.emit("⏭️ 输入输出语言相同，跳过翻译，仅优化")
        self.all_done.emit()
        return
    
    # 检测是否需要翻译（zh_合并 为空的是原生字幕）
    needs_translation = any(not e.chinese for e in self.entries)
    if not needs_translation:
        self.all_done.emit()
        return
    # ... 其余代码
```

- [ ] **Step 2: 更新 BatchWorker._translate 处理语言相同**

```python
async def _translate(self, entries: list[SubtitleEntry],
                      context: str, glossary: str) -> list[SubtitleEntry]:
    """阶段3：原生字幕 en_合并 → zh_译；双语字幕跳过"""
    # 检测是否需要翻译（输入输出语言相同时跳过）
    if self.input_lang == self.output_lang:
        self.log.emit(f"    ⏭️ 输入输出语言相同，跳过翻译，仅优化")
        return entries
    
    needs_translation = any(not e.chinese for e in entries)
    if not needs_translation:
        return entries
    # ... 其余代码
```

- [ ] **Step 3: Commit**

```bash
git add core/workers.py
git commit -m "feat: 处理输入输出语言相同的特殊场景"
```

---

### Task 12: 最终验证和测试

**Files:**
- None (手动测试)

- [ ] **Step 1: 验证 UI 显示**

运行程序，检查：
1. 语言选择下拉框正确显示 10 种语言
2. 默认值为 English → 中文
3. 配置保存/加载正常

- [ ] **Step 2: 验证翻译功能**

测试场景：
1. English → 中文（默认行为）
2. English → Italiano（新语言组合）
3. English → English（相同语言，跳过翻译）

- [ ] **Step 3: 验证配置兼容性**

1. 删除配置文件，重新启动，检查默认值
2. 使用旧配置文件启动，检查向后兼容

- [ ] **Step 4: Commit 最终版本**

```bash
git add -A
git commit -m "feat: 完成语言设置功能实现"
```

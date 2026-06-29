# Batch Glossary Confirmation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a default-on batch glossary review step that extracts per-file glossary candidates concurrently, lets the user resolve conflicts, immediately saves approved terms into the fixed glossary, and then starts the normal batch pipeline.

**Architecture:** Keep glossary merge/conflict logic in a small pure core module, run pre-analysis in a dedicated worker before `BatchWorker`, and keep the review UI in `ui/main_window.py` using the existing PyQt patterns. The fixed glossary table remains the single source of truth; after confirmation the normal batch worker receives the updated fixed glossary text.

**Tech Stack:** Python 3.10+, PyQt6, pytest, existing `MistralClient`, `SubtitlePipeline`, `parse_srt`, `transcribe_media_to_srt`, and glossary helpers in `core.workers`.

---

## File Structure

- Create `core/batch_glossary.py`
  - Pure dataclasses and helper functions for candidate rows, merge rules, fixed-glossary filtering, conflict detection, and source/target serialization.
- Modify `core/workers.py`
  - Add `BatchGlossaryWorker`, which parses/transcribes queued files, extracts original subtitle lines, calls stage-one analysis per file with configured concurrency, and emits a summary object.
- Modify `ui/main_window.py`
  - Add the default-on batch option, dynamic source/target glossary headers, review dialog, fixed-glossary append/save logic, and pre-analysis-to-batch orchestration.
- Create `test_batch_glossary.py`
  - Unit tests for pure merge/filter/conflict behavior.
- Create or extend a worker test file if practical, such as `test_batch_glossary_worker.py`
  - Focused tests for file parsing failure/empty file behavior with mocked analysis, if the existing worker structure allows isolated testing without live API.

## Task 1: Pure Candidate Merge Model

**Files:**
- Create: `core/batch_glossary.py`
- Test: `test_batch_glossary.py`

- [ ] **Step 1: Write failing tests for merge, conflict, and fixed glossary filtering**

Create `test_batch_glossary.py`:

```python
from core.batch_glossary import (
    GlossaryCandidate,
    approved_candidates_to_text,
    merge_batch_glossary_candidates,
    unresolved_conflict_sources,
)


def test_merge_candidates_combines_same_source_target_and_counts_files():
    result = merge_batch_glossary_candidates(
        [
            ("file_a.srt", "order flow->订单流\nliquidity sweep->流动性扫单"),
            ("file_b.srt", "order flow->订单流"),
        ],
        fixed_glossary="",
    )

    assert result.failures == []
    assert result.skipped_existing == []
    assert result.candidates == [
        GlossaryCandidate(
            source="order flow",
            target="订单流",
            files=("file_a.srt", "file_b.srt"),
            status="新词",
        ),
        GlossaryCandidate(
            source="liquidity sweep",
            target="流动性扫单",
            files=("file_a.srt",),
            status="新词",
        ),
    ]


def test_merge_candidates_keeps_conflict_variants_next_to_each_other():
    result = merge_batch_glossary_candidates(
        [
            ("file_a.srt", "order flow->订单流"),
            ("file_b.srt", "order flow->委托流"),
            ("file_c.srt", "delta->德尔塔"),
        ],
        fixed_glossary="",
    )

    assert [candidate.source for candidate in result.candidates] == [
        "order flow",
        "order flow",
        "delta",
    ]
    assert [candidate.target for candidate in result.candidates[:2]] == ["订单流", "委托流"]
    assert [candidate.status for candidate in result.candidates[:2]] == ["译法冲突", "译法冲突"]
    assert result.candidates[2].status == "新词"


def test_merge_candidates_filters_sources_already_in_fixed_glossary():
    result = merge_batch_glossary_candidates(
        [
            ("file_a.srt", "order flow->订单流\nliquidity sweep->流动性扫单"),
        ],
        fixed_glossary="order flow->订单流",
    )

    assert result.candidates == [
        GlossaryCandidate(
            source="liquidity sweep",
            target="流动性扫单",
            files=("file_a.srt",),
            status="新词",
        )
    ]
    assert result.skipped_existing == ["order flow"]


def test_unresolved_conflict_sources_detects_remaining_multi_target_sources():
    candidates = [
        GlossaryCandidate("order flow", "订单流", ("a.srt",), "译法冲突"),
        GlossaryCandidate("order flow", "委托流", ("b.srt",), "译法冲突"),
        GlossaryCandidate("delta", "德尔塔", ("c.srt",), "新词"),
    ]

    assert unresolved_conflict_sources(candidates) == ["order flow"]


def test_approved_candidates_to_text_ignores_blank_rows():
    candidates = [
        GlossaryCandidate("order flow", "订单流", ("a.srt",), "新词"),
        GlossaryCandidate("", "空", ("b.srt",), "新词"),
        GlossaryCandidate("empty", "", ("c.srt",), "新词"),
    ]

    assert approved_candidates_to_text(candidates) == "order flow->订单流"
```

- [ ] **Step 2: Run tests and verify they fail because the module does not exist**

Run:

```bash
rtk pytest test_batch_glossary.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'core.batch_glossary'`.

- [ ] **Step 3: Implement the pure merge module**

Create `core/batch_glossary.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GlossaryCandidate:
    source: str
    target: str
    files: tuple[str, ...] = field(default_factory=tuple)
    status: str = "新词"


@dataclass(frozen=True)
class GlossaryFileFailure:
    filename: str
    reason: str


@dataclass(frozen=True)
class BatchGlossarySummary:
    candidates: list[GlossaryCandidate]
    failures: list[GlossaryFileFailure] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)


def parse_glossary_pairs(raw: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for sep in ("->", "→"):
            if sep in line:
                source, target = line.split(sep, 1)
                source = source.strip()
                target = target.strip()
                if source and target:
                    pairs.append((source, target))
                break
    return pairs


def fixed_source_keys(fixed_glossary: str) -> set[str]:
    return {source.casefold() for source, _target in parse_glossary_pairs(fixed_glossary)}


def merge_batch_glossary_candidates(
    file_glossaries: list[tuple[str, str]],
    fixed_glossary: str,
    failures: list[GlossaryFileFailure] | None = None,
) -> BatchGlossarySummary:
    fixed_keys = fixed_source_keys(fixed_glossary)
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    skipped_existing: dict[str, str] = {}

    for filename, raw in file_glossaries:
        for source, target in parse_glossary_pairs(raw):
            source_key = source.casefold()
            if source_key in fixed_keys:
                skipped_existing.setdefault(source_key, source)
                continue

            pair_key = (source_key, target.casefold())
            if pair_key not in grouped:
                grouped[pair_key] = {
                    "source": source,
                    "target": target,
                    "files": set(),
                }
            grouped[pair_key]["files"].add(filename)

    source_to_targets: dict[str, set[str]] = {}
    for source_key, target_key in grouped:
        source_to_targets.setdefault(source_key, set()).add(target_key)

    candidates: list[GlossaryCandidate] = []
    for (source_key, target_key), data in grouped.items():
        status = "译法冲突" if len(source_to_targets[source_key]) > 1 else "新词"
        candidates.append(
            GlossaryCandidate(
                source=str(data["source"]),
                target=str(data["target"]),
                files=tuple(sorted(data["files"])),
                status=status,
            )
        )

    candidates.sort(
        key=lambda candidate: (
            0 if candidate.status == "译法冲突" else 1,
            candidate.source.casefold(),
            candidate.target.casefold(),
        )
    )

    return BatchGlossarySummary(
        candidates=candidates,
        failures=list(failures or []),
        skipped_existing=sorted(skipped_existing.values(), key=str.casefold),
    )


def unresolved_conflict_sources(candidates: list[GlossaryCandidate]) -> list[str]:
    source_to_targets: dict[str, set[str]] = {}
    source_labels: dict[str, str] = {}
    for candidate in candidates:
        source = candidate.source.strip()
        target = candidate.target.strip()
        if not source or not target:
            continue
        key = source.casefold()
        source_labels.setdefault(key, source)
        source_to_targets.setdefault(key, set()).add(target.casefold())

    conflicts = [
        source_labels[key]
        for key, targets in source_to_targets.items()
        if len(targets) > 1
    ]
    return sorted(conflicts, key=str.casefold)


def approved_candidates_to_text(candidates: list[GlossaryCandidate]) -> str:
    lines: list[str] = []
    seen_sources: set[str] = set()
    for candidate in candidates:
        source = candidate.source.strip()
        target = candidate.target.strip()
        if not source or not target:
            continue
        key = source.casefold()
        if key in seen_sources:
            continue
        seen_sources.add(key)
        lines.append(f"{source}->{target}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
rtk pytest test_batch_glossary.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
rtk git add core/batch_glossary.py test_batch_glossary.py
rtk git commit -m "feat: add batch glossary merge model"
```

## Task 2: Batch Glossary Pre-Analysis Worker

**Files:**
- Modify: `core/workers.py`
- Test: `test_batch_glossary_worker.py`

- [ ] **Step 1: Write focused worker tests for file handling**

Create `test_batch_glossary_worker.py`:

```python
from pathlib import Path

import pytest

from core.batch_glossary import GlossaryFileFailure
from core.models import SubtitleEntry
from core.workers import BatchGlossaryWorker


class DummyPipeline:
    def __init__(self, glossary_by_first_line):
        self.glossary_by_first_line = glossary_by_first_line

    async def analyze_details(self, entries, is_cancelled=None):
        first_line = entries[0].lines[0]
        return type("Analysis", (), {"auto_glossary": self.glossary_by_first_line[first_line]})()


def make_srt(text):
    return f"1\n00:00:00,000 --> 00:00:01,000\n{text}\n"


def test_prepare_file_entries_reads_srt_and_extracts_original_line(tmp_path):
    path = tmp_path / "sample.srt"
    path.write_text("1\n00:00:00,000 --> 00:00:01,000\n译文\nsource line\n", encoding="utf-8")
    worker = BatchGlossaryWorker(
        client=None,
        files=[str(path)],
        concurrency=3,
        fixed_glossary="",
        original_line=1,
        input_lang="English",
        output_lang="中文",
    )

    entries = worker._prepare_file_entries(str(path))

    assert [entry.lines for entry in entries] == [["source line"]]


def test_prepare_file_entries_rejects_empty_srt(tmp_path):
    path = tmp_path / "empty.srt"
    path.write_text("", encoding="utf-8")
    worker = BatchGlossaryWorker(
        client=None,
        files=[str(path)],
        concurrency=3,
        fixed_glossary="",
        original_line=0,
        input_lang="English",
        output_lang="中文",
    )

    with pytest.raises(ValueError, match="无法解析字幕文件"):
        worker._prepare_file_entries(str(path))


@pytest.mark.asyncio
async def test_analyze_files_merges_successes_and_failures(tmp_path):
    good = tmp_path / "good.srt"
    bad = tmp_path / "bad.srt"
    good.write_text(make_srt("good source"), encoding="utf-8")
    bad.write_text("", encoding="utf-8")
    worker = BatchGlossaryWorker(
        client=None,
        files=[str(good), str(bad)],
        concurrency=2,
        fixed_glossary="existing->已有",
        original_line=0,
        input_lang="English",
        output_lang="中文",
    )
    pipeline = DummyPipeline({"good source": "order flow->订单流\nexisting->已有"})

    summary = await worker._analyze_files(pipeline)

    assert [candidate.source for candidate in summary.candidates] == ["order flow"]
    assert summary.failures == [GlossaryFileFailure("bad.srt", "无法解析字幕文件")]
    assert summary.skipped_existing == ["existing"]
```

- [ ] **Step 2: Run worker tests and verify they fail because `BatchGlossaryWorker` does not exist**

Run:

```bash
rtk pytest test_batch_glossary_worker.py -q
```

Expected: FAIL with `ImportError` or `AttributeError` for `BatchGlossaryWorker`.

- [ ] **Step 3: Add `BatchGlossaryWorker` imports and class skeleton**

Modify the top imports in `core/workers.py`:

```python
from core.batch_glossary import (
    BatchGlossarySummary,
    GlossaryFileFailure,
    merge_batch_glossary_candidates,
)
```

Add this class before `BatchWorker`:

```python
class BatchGlossaryWorker(BaseWorker):
    """Pre-analyze queued files and collect batch glossary candidates."""

    finished = pyqtSignal(object)
    log = pyqtSignal(str)

    def __init__(
        self,
        client: MistralClient,
        files: list[str],
        concurrency: int,
        fixed_glossary: str,
        original_line: int = 1,
        transcription_audio_format: str = "mp3",
        input_lang: str = "English",
        output_lang: str = "中文",
    ):
        super().__init__()
        self.client = client
        self.files = files
        self.concurrency = max(1, concurrency)
        self.fixed_glossary = fixed_glossary
        self.original_line = original_line
        self.transcription_audio_format = transcription_audio_format
        self.input_lang = input_lang
        self.output_lang = output_lang

    def _prepare_file_entries(self, filepath: str) -> list[SubtitleEntry]:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            content = f.read()
        entries = parse_srt(content)
        if not entries:
            raise ValueError("无法解析字幕文件")
        if entries and len(entries[0].lines) > 1:
            helper = BatchWorker(
                client=self.client,
                files=[],
                out_dir=None,
                concurrency=self.concurrency,
                batch_size=1,
                original_line=self.original_line,
                transcription_audio_format=self.transcription_audio_format,
                input_lang=self.input_lang,
                output_lang=self.output_lang,
            )
            entries = helper._extract_original_line(entries)
        return entries

    @pyqtSlot()
    def run(self):
        try:
            from core.pipeline import SubtitlePipeline

            pipeline = SubtitlePipeline(
                self.client,
                self.input_lang,
                self.output_lang,
                self.concurrency,
                batch_size=5,
            )
            summary = _run_loop(self._analyze_files(pipeline))
            if not self._cancelled:
                self.finished.emit(summary)
        except Exception as exc:
            self.error.emit(str(exc))

    async def _analyze_files(self, pipeline) -> BatchGlossarySummary:
        semaphore = asyncio.Semaphore(self.concurrency)
        file_glossaries: list[tuple[str, str]] = []
        failures: list[GlossaryFileFailure] = []

        async def analyze_one(filepath: str):
            filename = Path(filepath).name
            async with semaphore:
                if self._cancelled:
                    return
                try:
                    working_path = filepath
                    if is_media_file(filepath):
                        self.log.emit(f"  [{filename}] 转写媒体以提取词汇候选...")
                        working_path, _content = await transcribe_media_to_srt(
                            self.client,
                            filepath,
                            audio_format=self.transcription_audio_format,
                            on_log=lambda msg: self.log.emit(f"  {msg}"),
                        )
                    entries = self._prepare_file_entries(working_path)
                    self.log.emit(f"  [{filename}] 提取词汇候选...")
                    analysis = await pipeline.analyze_details(
                        entries,
                        is_cancelled=lambda: self._cancelled,
                    )
                    if analysis.auto_glossary:
                        file_glossaries.append((filename, analysis.auto_glossary))
                except Exception as exc:
                    failures.append(GlossaryFileFailure(filename, str(exc)))

        await asyncio.gather(*(analyze_one(filepath) for filepath in self.files))
        return merge_batch_glossary_candidates(
            file_glossaries,
            fixed_glossary=self.fixed_glossary,
            failures=failures,
        )
```

- [ ] **Step 4: Run worker tests and adjust imports if needed**

Run:

```bash
rtk pytest test_batch_glossary.py test_batch_glossary_worker.py -q
```

Expected: PASS. If import style differs because `core.workers` sometimes runs both as package and script, use the same try/except import pattern already present in the file.

- [ ] **Step 5: Commit**

```bash
rtk git add core/workers.py test_batch_glossary_worker.py
rtk git commit -m "feat: add batch glossary preanalysis worker"
```

## Task 3: Dynamic Glossary Labels And Fixed Glossary Append Helpers

**Files:**
- Modify: `ui/main_window.py`

- [ ] **Step 1: Write a focused helper test if UI construction can be imported safely**

If importing `MainWindow` is already used in tests, add a test for the static label helper. If not, skip direct UI test and rely on manual PyQt verification in Task 6.

Preferred test shape:

```python
from ui.main_window import glossary_column_labels


def test_glossary_column_labels_uses_current_languages():
    assert glossary_column_labels("English", "中文") == ("原文（English）", "译文（中文）")


def test_glossary_column_labels_falls_back_when_blank():
    assert glossary_column_labels("", "") == ("原文", "译文")
```

- [ ] **Step 2: Add a language-agnostic label helper**

Near the top of `ui/main_window.py`, outside `MainWindow`, add:

```python
def glossary_column_labels(input_lang: str, output_lang: str) -> tuple[str, str]:
    input_lang = (input_lang or "").strip()
    output_lang = (output_lang or "").strip()
    source_label = f"原文（{input_lang}）" if input_lang else "原文"
    target_label = f"译文（{output_lang}）" if output_lang else "译文"
    return source_label, target_label
```

- [ ] **Step 3: Update settings glossary table headers**

Replace the fixed headers in `_build_settings_page()`:

```python
self.table_glossary.setHorizontalHeaderLabels(["英文 (Source)", "中文 (Target)"])
```

with:

```python
source_label, target_label = glossary_column_labels(
    self.combo_input_lang.currentText() if hasattr(self, "combo_input_lang") else "",
    self.combo_output_lang.currentText() if hasattr(self, "combo_output_lang") else "",
)
self.table_glossary.setHorizontalHeaderLabels([source_label, target_label])
```

Also change the hint:

```python
gloss_hint = QLabel("在表格中填写原文词汇和对应译文，点击「添加行」新增术语")
```

- [ ] **Step 4: Add a header refresh method and connect language changes**

Add methods near glossary helpers:

```python
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
```

After creating `self.combo_input_lang` and `self.combo_output_lang`, connect:

```python
self.combo_input_lang.currentTextChanged.connect(lambda _text: self._refresh_glossary_headers())
self.combo_output_lang.currentTextChanged.connect(lambda _text: self._refresh_glossary_headers())
```

- [ ] **Step 5: Run syntax/import checks**

Run:

```bash
rtk python -m py_compile ui/main_window.py
```

Expected: no output and exit 0.

- [ ] **Step 6: Commit**

```bash
rtk git add ui/main_window.py
rtk git commit -m "feat: use language-aware glossary labels"
```

## Task 4: Review Dialog UI

**Files:**
- Modify: `ui/main_window.py`

- [ ] **Step 1: Add imports for candidate helpers**

Add to existing imports:

```python
from core.batch_glossary import (
    BatchGlossarySummary,
    GlossaryCandidate,
    approved_candidates_to_text,
    unresolved_conflict_sources,
)
```

- [ ] **Step 2: Add row extraction helper for the dialog**

Add methods to `MainWindow`:

```python
def _candidate_sources_text(self, candidate: GlossaryCandidate) -> str:
    if len(candidate.files) <= 3:
        return "、".join(candidate.files)
    return f"{len(candidate.files)} 个文件"


def _batch_glossary_table_candidates(self, table: QTableWidget) -> list[GlossaryCandidate]:
    candidates: list[GlossaryCandidate] = []
    for row in range(table.rowCount()):
        source_item = table.item(row, 0)
        target_item = table.item(row, 1)
        source = source_item.text().strip() if source_item else ""
        target = target_item.text().strip() if target_item else ""
        files = tuple((table.item(row, 2).data(Qt.ItemDataRole.UserRole) or ()))
        status = table.item(row, 3).text().strip() if table.item(row, 3) else "新词"
        if source or target:
            candidates.append(GlossaryCandidate(source, target, files, status))
    return candidates
```

- [ ] **Step 3: Add the dialog builder**

Add to `MainWindow`:

```python
def _confirm_batch_glossary(self, summary: BatchGlossarySummary) -> str | None:
    if not summary.candidates:
        return ""

    dialog = QDialog(self)
    dialog.setWindowTitle("批量词汇整理与确认")
    dialog.resize(820, 520)
    layout = QVBoxLayout(dialog)

    label = QLabel("删除不合适的候选词；确认后剩余词汇会立即加入固定词汇表。")
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
    source_label, target_label = glossary_column_labels(
        self.combo_input_lang.currentText(),
        self.combo_output_lang.currentText(),
    )
    table.setHorizontalHeaderLabels([source_label, target_label, "来源", "状态", "操作"])
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    table.setRowCount(len(summary.candidates))

    def delete_row(button):
        for row in range(table.rowCount()):
            if table.cellWidget(row, 4) is button:
                table.removeRow(row)
                break

    for row, candidate in enumerate(summary.candidates):
        table.setItem(row, 0, QTableWidgetItem(candidate.source))
        table.setItem(row, 1, QTableWidgetItem(candidate.target))
        source_item = QTableWidgetItem(self._candidate_sources_text(candidate))
        source_item.setData(Qt.ItemDataRole.UserRole, candidate.files)
        table.setItem(row, 2, source_item)
        status_item = QTableWidgetItem(candidate.status)
        table.setItem(row, 3, status_item)
        btn_delete = QPushButton("删除")
        btn_delete.clicked.connect(lambda _checked=False, b=btn_delete: delete_row(b))
        table.setCellWidget(row, 4, btn_delete)

    layout.addWidget(table, 1)

    row = QHBoxLayout()
    btn_clear = QPushButton("全部删除")
    btn_cancel = QPushButton("取消")
    btn_ok = QPushButton("确认并加入固定词汇表")
    row.addWidget(btn_clear)
    row.addStretch()
    row.addWidget(btn_cancel)
    row.addWidget(btn_ok)
    layout.addLayout(row)

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
```

- [ ] **Step 4: Run syntax check**

Run:

```bash
rtk python -m py_compile ui/main_window.py
```

Expected: no output and exit 0.

- [ ] **Step 5: Commit**

```bash
rtk git add ui/main_window.py
rtk git commit -m "feat: add batch glossary review dialog"
```

## Task 5: Batch Start Orchestration

**Files:**
- Modify: `ui/main_window.py`
- Modify: `core/workers.py` only if signal types/imports need adjustment

- [ ] **Step 1: Add default-on batch option**

In `_build_batch_page()`, near the batch controls before `self.btn_batch_start`, add:

```python
self.chk_batch_glossary_review = QCheckBox("批量词汇整理与确认")
self.chk_batch_glossary_review.setChecked(True)
self.chk_batch_glossary_review.setToolTip("批量开始前提取新词，确认后加入固定词汇表再处理整批文件")
ctrl_row.addWidget(self.chk_batch_glossary_review)
```

- [ ] **Step 2: Split current `_batch_start` into preflight and worker-start methods**

Refactor `_batch_start()` so it gathers validated settings and then either starts pre-analysis or directly starts formal batch.

Add:

```python
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
    }
```

Update `_batch_start()` to:

```python
def _batch_start(self):
    if not self._batch_files:
        QMessageBox.warning(self, "提示", "请先添加要处理的文件")
        return

    settings = self._batch_settings()
    if not settings["client"]:
        return

    self._cleanup_batch_pipeline()
    self.btn_batch_start.setEnabled(False)
    self.btn_batch_stop.setEnabled(True)
    self.batch_overall_bar.setMaximum(len(self._batch_files))
    self.batch_overall_bar.setValue(0)
    self.batch_log.clear()
    self._batch_log_msg(f"▶ 开始批量处理 {len(self._batch_files)} 个文件")

    for i in range(self.batch_list.count()):
        self._batch_set_item_status(i, "pending")

    if self.chk_batch_glossary_review.isChecked():
        self._start_batch_glossary_preanalysis(settings)
        return

    self._start_batch_worker(settings)
```

- [ ] **Step 3: Move existing worker creation into `_start_batch_worker`**

Add:

```python
def _start_batch_worker(self, settings: dict):
    self._batch_worker = BatchWorker(
        client=settings["client"],
        files=settings["files"],
        out_dir=settings["out_dir"],
        concurrency=settings["concurrency"],
        batch_size=settings["batch_size"],
        overwrite=settings["overwrite"],
        target_first=self._target_first(),
        glossary_prompt=self._get_glossary_prompt(),
        raw_glossary=self._glossary_to_text(),
        original_line=self._get_original_line_index(),
        transcription_audio_format=self._transcription_audio_format(),
        input_lang=settings["input_lang"],
        output_lang=settings["output_lang"],
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
```

- [ ] **Step 4: Add pre-analysis orchestration**

Import `BatchGlossaryWorker` from `core.workers`.

Add:

```python
def _start_batch_glossary_preanalysis(self, settings: dict):
    self._batch_log_msg("📖 开始批量词汇整理与确认...")
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
```

Add:

```python
def _after_batch_glossary_preanalysis(self, summary: BatchGlossarySummary):
    settings = self._batch_glossary_settings
    if summary.failures:
        self._batch_log_msg(f"⚠️ 词汇预分析跳过 {len(summary.failures)} 个文件")
    if not summary.candidates:
        if len(summary.failures) >= len(settings["files"]):
            QMessageBox.warning(self, "批量词汇整理失败", "所有文件都无法完成词汇预分析，已停止批量处理。")
            self._reset_batch_controls_after_preanalysis_stop()
            return
        self._batch_log_msg("📖 没有发现固定词汇表之外的新词，继续批量处理")
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
```

Add:

```python
def _on_batch_glossary_error(self, message: str):
    QMessageBox.critical(self, "批量词汇整理失败", message)
    self._batch_log_msg(f"❌ 批量词汇整理失败：{message}")
    self._reset_batch_controls_after_preanalysis_stop()


def _reset_batch_controls_after_preanalysis_stop(self):
    self.btn_batch_start.setEnabled(True)
    self.btn_batch_stop.setEnabled(False)
```

- [ ] **Step 5: Update cleanup and stop behavior**

Extend `_cleanup_batch_pipeline()` and batch stop logic to cancel/clear `_batch_glossary_worker` and `_batch_glossary_thread` if those attributes exist:

```python
if getattr(self, "_batch_glossary_worker", None):
    self._batch_glossary_worker.cancel()
if getattr(self, "_batch_glossary_thread", None):
    self._batch_glossary_thread.quit()
```

Use the existing `_cleanup_worker_thread()` helper if the function already centralizes worker cleanup.

- [ ] **Step 6: Run syntax check**

Run:

```bash
rtk python -m py_compile ui/main_window.py core/workers.py
```

Expected: no output and exit 0.

- [ ] **Step 7: Commit**

```bash
rtk git add ui/main_window.py core/workers.py
rtk git commit -m "feat: run glossary review before batch processing"
```

## Task 6: Manual UI Verification And Regression Tests

**Files:**
- Modify: none unless fixes are found

- [ ] **Step 1: Run all relevant automated tests**

Run:

```bash
rtk pytest test_batch_glossary.py test_batch_glossary_worker.py test_batch_silent_media_skip.py test_transcription_audio_format.py test_transcription_retry.py -q
```

Expected: PASS.

- [ ] **Step 2: Compile main modules**

Run:

```bash
rtk python -m py_compile core/batch_glossary.py core/workers.py ui/main_window.py
```

Expected: no output and exit 0.

- [ ] **Step 3: Manual smoke test the UI**

Run:

```bash
rtk python main.py
```

Manual checks:

- Settings glossary headers show `原文（<input language>）` and `译文（<output language>）`.
- Changing input/output language updates fixed glossary headers.
- Batch page shows default-on `批量词汇整理与确认`.
- Starting batch opens the review dialog when new candidates exist.
- Conflict rows are adjacent and marked `译法冲突`.
- Deleting one conflicting row allows confirmation.
- Confirm appends terms to fixed glossary and saves config.
- Cancel does not append terms and does not start formal batch.
- If no new terms remain after filtering fixed glossary, formal batch starts.

- [ ] **Step 4: Commit final fixes if needed**

If any fixes were required:

```bash
rtk git add core/batch_glossary.py core/workers.py ui/main_window.py test_batch_glossary.py test_batch_glossary_worker.py
rtk git commit -m "fix: polish batch glossary confirmation flow"
```

If no fixes were required, do not create an empty commit.

## Self-Review

- Spec coverage:
  - Default-on batch glossary option: Task 5.
  - Per-file requests with configured concurrency: Task 2 and Task 5.
  - File handling for parsing, empty files, failures, media transcription: Task 2.
  - Global candidate pool, conflict-adjacent display, no file grouping: Task 1 and Task 4.
  - Fixed glossary filtering and priority: Task 1 and Task 5.
  - Language-agnostic labels: Task 3 and Task 4.
  - Immediate save and no rollback: Task 5.
  - Cancel/failure behavior: Task 4, Task 5, Task 6.
- Placeholder scan:
  - No `TBD` or `TODO` placeholders.
  - The only conditional note is in Task 3 Step 1 for UI import safety; implementation does not depend on that test existing.
- Type consistency:
  - `GlossaryCandidate`, `GlossaryFileFailure`, `BatchGlossarySummary`, `merge_batch_glossary_candidates`, `unresolved_conflict_sources`, and `approved_candidates_to_text` are defined in Task 1 and reused consistently.
  - `BatchGlossaryWorker.finished` emits `BatchGlossarySummary` as `object`, matching PyQt signal constraints.
  - UI methods use existing `QTableWidget`, `QTableWidgetItem`, `QHeaderView`, `QDialog`, `QMessageBox`, `QThread`, and existing glossary helpers.

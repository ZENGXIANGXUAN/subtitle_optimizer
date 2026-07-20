# Batch Transcribe-Only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a batch-page mode that transcribes media files to same-directory, same-stem `.srt` files while overlapping local audio extraction with remote transcription.

**Architecture:** Keep the existing batch page and `BatchWorker`, adding a `transcribe_only` branch. Split transcription helpers so the worker can prepare audio in the main loop and run API transcription in background asyncio tasks.

**Tech Stack:** Python, PyQt6 workers/signals, asyncio, unittest, existing `core.transcription` ffmpeg/API helpers.

---

## File Structure

- Modify `core/transcription.py`: add two reusable helpers for preparing audio and writing SRT from an existing audio path.
- Modify `core/workers.py`: add `transcribe_only` to `BatchWorker`, implement `_run_transcribe_only()`, and keep the existing optimization flow unchanged.
- Modify `ui/main_window.py`: add a `仅转录` checkbox, include it in batch settings, skip glossary preanalysis when enabled, and pass the flag to `BatchWorker`.
- Modify `test_batch_silent_media_skip.py`: keep existing skip behavior green with the new constructor.
- Add `test_batch_transcribe_only.py`: cover output path and extraction/transcription overlap.

### Task 1: Tests For Batch Transcribe-Only

**Files:**
- Create: `test_batch_transcribe_only.py`

- [ ] **Step 1: Write failing tests**

```python
import asyncio
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.modules.setdefault("aiohttp", types.SimpleNamespace(ClientSession=object))


class _Signal:
    def __init__(self, *args, **kwargs):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args):
        for callback in list(self._callbacks):
            callback(*args)


def _pyqt_slot(*args, **kwargs):
    def decorator(func):
        return func
    return decorator


sys.modules.setdefault("PyQt6", types.SimpleNamespace())
sys.modules.setdefault(
    "PyQt6.QtCore",
    types.SimpleNamespace(QObject=object, pyqtSignal=_Signal, pyqtSlot=_pyqt_slot),
)

from core.workers import BatchWorker


class BatchTranscribeOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcribe_only_writes_same_directory_same_stem_srt(self):
        fd, media_path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        done = []

        async def fake_transcribe_prepared_audio_to_srt(*args, **kwargs):
            output_path = kwargs["output_path"]
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("1\n00:00:00,000 --> 00:00:01,000\nhello\n")
            return output_path, "content"

        worker = BatchWorker(
            client=object(),
            files=[media_path],
            out_dir="ignored",
            concurrency=1,
            batch_size=1,
            transcribe_only=True,
        )
        worker.file_done.connect(lambda idx, name, path: done.append((idx, name, path)))

        with (
            patch("core.workers.is_media_file", return_value=True),
            patch("core.workers.prepare_transcription_audio", return_value=(media_path, None)),
            patch("core.workers.transcribe_prepared_audio_to_srt", side_effect=fake_transcribe_prepared_audio_to_srt),
        ):
            await worker._run()

        expected_path = os.path.splitext(media_path)[0] + ".srt"
        self.assertEqual([(0, os.path.basename(media_path), expected_path)], done)

    async def test_next_audio_extraction_starts_before_previous_transcription_finishes(self):
        files = ["first.mp4", "second.mp4"]
        events = []
        first_transcription_started = asyncio.Event()
        second_audio_prepared = asyncio.Event()
        allow_first_transcription = asyncio.Event()

        def fake_prepare(path, **kwargs):
            events.append(("prepare", path))
            if path == "second.mp4":
                second_audio_prepared.set()
            return path + ".audio", None

        async def fake_transcribe_prepared_audio_to_srt(*args, **kwargs):
            input_path = args[1]
            events.append(("transcribe-start", input_path))
            if input_path == "first.mp4.audio":
                first_transcription_started.set()
                await second_audio_prepared.wait()
                allow_first_transcription.set()
            await allow_first_transcription.wait()
            events.append(("transcribe-done", input_path))
            return kwargs["output_path"], "content"

        worker = BatchWorker(
            client=object(),
            files=files,
            out_dir=None,
            concurrency=1,
            batch_size=1,
            transcribe_only=True,
        )

        with (
            patch("core.workers.is_media_file", return_value=True),
            patch("core.workers.prepare_transcription_audio", side_effect=fake_prepare),
            patch("core.workers.transcribe_prepared_audio_to_srt", side_effect=fake_transcribe_prepared_audio_to_srt),
        ):
            await asyncio.wait_for(worker._run(), timeout=1)

        self.assertLess(
            events.index(("prepare", "second.mp4")),
            events.index(("transcribe-done", "first.mp4.audio")),
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_batch_transcribe_only.py -v`

Expected: fail because `BatchWorker.__init__()` does not accept `transcribe_only` and worker helpers do not exist yet.

### Task 2: Transcription Helper Split

**Files:**
- Modify: `core/transcription.py`

- [ ] **Step 1: Add helper functions**

Add:

```python
def prepare_transcription_audio(input_path: str, audio_format: str = "mp3", on_log=None) -> tuple[str, str | None]:
    if is_video_file(input_path):
        temp_audio = extract_audio_from_video(input_path, audio_format=audio_format, on_log=on_log)
        return temp_audio, temp_audio
    return input_path, None


async def transcribe_prepared_audio_to_srt(client, audio_path: str, source_path: str, output_path: str | None = None, model: str | None = None, language: str | None = None, on_log=None) -> tuple[str, str]:
    result = await client.transcribe_audio(audio_path, model=model, language=language, on_log=on_log)
    content = generate_srt_content(result)
    srt_path = output_path or default_srt_output_path(source_path)
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(content)
    return srt_path, content
```

Update `transcribe_media_to_srt()` to use both helpers and clean temp audio in `finally`.

- [ ] **Step 2: Run transcription tests**

Run: `python -m unittest test_transcription_audio_format.py test_transcription_retry.py -v`

Expected: pass.

### Task 3: BatchWorker Transcribe-Only Flow

**Files:**
- Modify: `core/workers.py`
- Test: `test_batch_transcribe_only.py`

- [ ] **Step 1: Implement worker branch**

Add imports for the new helpers, add `transcribe_only` to `BatchWorker.__init__`, dispatch from `_run()`, and implement `_run_transcribe_only()` with background tasks.

- [ ] **Step 2: Run focused tests**

Run: `python -m unittest test_batch_transcribe_only.py test_batch_silent_media_skip.py -v`

Expected: pass.

### Task 4: Batch UI Control

**Files:**
- Modify: `ui/main_window.py`

- [ ] **Step 1: Add checkbox and settings**

Add `self.chk_batch_transcribe_only = QCheckBox("仅转录")` near the batch glossary checkbox. Add `"transcribe_only": self.chk_batch_transcribe_only.isChecked()` to `_batch_settings()`.

- [ ] **Step 2: Skip glossary preanalysis in transcribe-only mode**

Change `_batch_start()` so `self.chk_batch_glossary_review.isChecked()` is considered only when `settings["transcribe_only"]` is false. Pass `transcribe_only=settings["transcribe_only"]` into `BatchWorker`.

- [ ] **Step 3: Run syntax check**

Run: `python -m py_compile ui/main_window.py core/workers.py core/transcription.py`

Expected: pass.

### Task 5: Verification

- [ ] **Step 1: Run focused regression tests**

Run: `python -m unittest test_batch_transcribe_only.py test_batch_silent_media_skip.py test_transcription_audio_format.py test_transcription_retry.py -v`

Expected: pass.

- [ ] **Step 2: Review diff**

Run: `git diff -- core/transcription.py core/workers.py ui/main_window.py test_batch_transcribe_only.py`

Expected: diff is limited to the transcribe-only feature.

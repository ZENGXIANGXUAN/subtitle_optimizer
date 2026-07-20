import asyncio
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class _FormData:
    def add_field(self, *args, **kwargs):
        pass


class _ClientTimeout:
    def __init__(self, total=None):
        self.total = total


class _ClientError(Exception):
    pass


sys.modules.setdefault(
    "aiohttp",
    types.SimpleNamespace(
        FormData=_FormData,
        ClientTimeout=_ClientTimeout,
        ClientSession=object,
        ClientError=_ClientError,
    ),
)


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

        for path in (media_path, expected_path):
            try:
                os.remove(path)
            except OSError:
                pass

    async def test_next_audio_extraction_waits_until_previous_transcription_finishes(self):
        files = ["first.mp4", "second.mp4"]
        events = []

        def fake_prepare(path, **kwargs):
            events.append(("prepare", path))
            return path + ".audio", None

        async def fake_transcribe_prepared_audio_to_srt(*args, **kwargs):
            audio_path = args[1]
            events.append(("transcribe-start", audio_path))
            await asyncio.sleep(0.01)
            events.append(("transcribe-done", audio_path))
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
            events.index(("transcribe-done", "first.mp4.audio")),
            events.index(("prepare", "second.mp4")),
        )


if __name__ == "__main__":
    unittest.main()

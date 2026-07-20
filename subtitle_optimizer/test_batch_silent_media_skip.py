import unittest
import sys
import types
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

from core.transcription import NO_TRANSCRIBABLE_AUDIO_ERROR
from core.workers import BatchWorker


class BatchSilentMediaSkipTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_transcribable_audio_is_skipped_not_failed(self):
        async def fake_transcribe(*args, **kwargs):
            raise RuntimeError(NO_TRANSCRIBABLE_AUDIO_ERROR)

        worker = BatchWorker(
            client=object(),
            files=["silent.mp4"],
            out_dir=None,
            concurrency=1,
            batch_size=1,
        )
        skipped = []
        errors = []
        worker.file_skipped.connect(lambda idx, name, reason: skipped.append((idx, name, reason)))
        worker.file_error.connect(lambda idx, name, err: errors.append((idx, name, err)))

        with (
            patch("core.workers.is_media_file", return_value=True),
            patch("core.workers.transcribe_media_to_srt", side_effect=fake_transcribe),
        ):
            await worker._run()

        self.assertEqual([(0, "silent.mp4", NO_TRANSCRIBABLE_AUDIO_ERROR)], skipped)
        self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()

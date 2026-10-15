from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from core.client import (
    MistralClient,
    TRANSLATION_PROVIDERS,
    TRANSCRIPTION_PROVIDERS,
)
from core.workers import BatchWorker


class TestIndependentProviders(unittest.TestCase):
    def test_provider_definitions_exist(self):
        self.assertIn("Mistral AI", TRANSLATION_PROVIDERS)
        self.assertIn("DeepSeek", TRANSLATION_PROVIDERS)
        self.assertIn("OpenAI", TRANSLATION_PROVIDERS)
        self.assertIn("SiliconFlow (硅基流动)", TRANSLATION_PROVIDERS)
        self.assertIn("自定义 (Custom)", TRANSLATION_PROVIDERS)

        self.assertIn("Mistral AI", TRANSCRIPTION_PROVIDERS)
        self.assertIn("Groq", TRANSCRIPTION_PROVIDERS)
        self.assertIn("OpenAI", TRANSCRIPTION_PROVIDERS)
        self.assertIn("SiliconFlow (硅基流动)", TRANSCRIPTION_PROVIDERS)
        self.assertIn("自定义 (Custom)", TRANSCRIPTION_PROVIDERS)

    def test_client_independent_urls_and_keys(self):
        client = MistralClient(
            api_key="deepseek_key_123",
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            translation_provider="DeepSeek",
            transcription_api_key="groq_key_456",
            transcription_base_url="https://api.groq.com/openai/v1",
            transcription_model="whisper-large-v3",
            transcription_provider="Groq",
        )

        self.assertEqual("deepseek_key_123", client.api_key)
        self.assertEqual("https://api.deepseek.com/v1", client.base_url)
        self.assertEqual("https://api.deepseek.com/v1/chat/completions", client.chat_url)
        self.assertEqual("deepseek-chat", client.model)
        self.assertEqual("DeepSeek", client.translation_provider)

        self.assertEqual("groq_key_456", client.transcription_api_key)
        self.assertEqual("https://api.groq.com/openai/v1", client.transcription_base_url)
        self.assertEqual(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            client.transcription_url,
        )
        self.assertEqual("whisper-large-v3", client.transcription_model)
        self.assertEqual("Groq", client.transcription_provider)

    def test_client_transcription_fallbacks(self):
        client = MistralClient(
            api_key="shared_key_789",
            model="mistral-large-latest",
            base_url="https://api.mistral.ai/v1",
        )

        self.assertEqual("shared_key_789", client.api_key)
        self.assertEqual("shared_key_789", client.transcription_api_key)
        self.assertEqual("https://api.mistral.ai/v1", client.transcription_base_url)
        self.assertEqual(
            "https://api.mistral.ai/v1/audio/transcriptions",
            client.transcription_url,
        )
        self.assertEqual("mistral-large-latest", client.transcription_model)

    def test_client_full_endpoint_url_not_duplicated(self):
        client = MistralClient(
            api_key="key",
            base_url="https://custom.host/v1/chat/completions",
            transcription_base_url="https://custom.host/v1/audio/transcriptions",
        )
        self.assertEqual("https://custom.host/v1/chat/completions", client.chat_url)
        self.assertEqual("https://custom.host/v1/audio/transcriptions", client.transcription_url)


class TestTranscriptionRequests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fd, self.audio_path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)

    def tearDown(self):
        try:
            os.remove(self.audio_path)
        except OSError:
            pass

    async def test_transcribe_audio_uses_transcription_credentials_and_groq_fields(self):
        recorded_url = []
        recorded_headers = []
        recorded_fields = []

        class MockResponse:
            status = 200
            async def text(self):
                return ""
            async def json(self):
                return {"text": "hello", "words": [{"word": "hello", "start": 0.0, "end": 0.5}]}
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def post(self, url, headers=None, data=None, **kwargs):
                recorded_url.append(url)
                recorded_headers.append(headers)
                recorded_fields.append(getattr(data, "_fields", []))
                return MockResponse()

        client = MistralClient(
            api_key="translate_secret",
            base_url="https://api.deepseek.com/v1",
            transcription_api_key="groq_secret",
            transcription_base_url="https://api.groq.com/openai/v1",
            transcription_model="whisper-large-v3",
            transcription_provider="Groq",
        )

        with patch("core.client.aiohttp.ClientSession", MockSession):
            result = await client.transcribe_audio(self.audio_path, retry_delay_seconds=0)

        self.assertEqual(1, len(recorded_url))
        self.assertEqual("https://api.groq.com/openai/v1/audio/transcriptions", recorded_url[0])
        self.assertEqual({"Authorization": "Bearer groq_secret"}, recorded_headers[0])
        self.assertEqual("hello", result["text"])

        # Check form fields for Groq
        field_names = [f[0]["name"] for f in recorded_fields[0]]
        self.assertIn("response_format", field_names)
        self.assertIn("timestamp_granularities[]", field_names)

    async def test_transcribe_audio_uses_mistral_fields(self):
        recorded_fields = []

        class MockResponse:
            status = 200
            async def text(self):
                return ""
            async def json(self):
                return {"text": "ok", "segments": [{"text": "ok", "start": 0.0, "end": 0.5}]}
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def post(self, url, headers=None, data=None, **kwargs):
                recorded_fields.append(getattr(data, "_fields", []))
                return MockResponse()

        client = MistralClient(
            api_key="mistral_secret",
            base_url="https://api.mistral.ai/v1",
            transcription_provider="Mistral AI",
        )

        with patch("core.client.aiohttp.ClientSession", MockSession):
            await client.transcribe_audio(self.audio_path, retry_delay_seconds=0)

        field_names = [f[0]["name"] for f in recorded_fields[0]]
        self.assertIn("timestamp_granularities", field_names)
        self.assertIn("diarize", field_names)


class TestBatchWorkerIndependentClient(unittest.TestCase):
    def test_batch_worker_accepts_separate_transcription_client(self):
        trans_client = MistralClient(api_key="trans_key", model="gpt-4o")
        transcribe_client = MistralClient(api_key="audio_key", transcription_model="whisper-1")

        worker = BatchWorker(
            client=trans_client,
            files=["test.mp4"],
            out_dir=None,
            concurrency=2,
            batch_size=5,
            transcription_client=transcribe_client,
        )

        self.assertIs(worker.client, trans_client)
        self.assertIs(worker.transcription_client, transcribe_client)

    def test_batch_worker_defaults_transcription_client_to_primary(self):
        unified_client = MistralClient(api_key="unified_key")

        worker = BatchWorker(
            client=unified_client,
            files=["test.mp4"],
            out_dir=None,
            concurrency=2,
            batch_size=5,
        )

        self.assertIs(worker.client, unified_client)
        self.assertIs(worker.transcription_client, unified_client)


class TestMainWindowIndependentProviders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_ui_provider_switching_and_clients(self):
        from ui.main_window import MainWindow
        win = MainWindow()

        # Test switching translation provider to DeepSeek
        win._on_translation_provider_changed("DeepSeek")
        self.assertEqual("https://api.deepseek.com/v1", win.input_base_url.text())
        self.assertEqual("deepseek-chat", win.combo_model.currentText())

        # Test switching transcription provider to Groq
        win._on_transcription_provider_changed("Groq")
        self.assertEqual("https://api.groq.com/openai/v1", win.input_transcription_base_url.text())
        self.assertEqual("whisper-large-v3", win.combo_transcription_model.currentText())

        # Test clients
        win.input_apikey.setText("deepseek_test_key")
        win.input_transcription_apikey.setText("groq_test_key")

        trans_client = win._get_translation_client()
        self.assertIsNotNone(trans_client)
        self.assertEqual("https://api.deepseek.com/v1/chat/completions", trans_client.chat_url)
        self.assertEqual("deepseek_test_key", trans_client.api_key)

        transcribe_client = win._get_transcription_client()
        self.assertIsNotNone(transcribe_client)
        self.assertEqual("https://api.groq.com/openai/v1/audio/transcriptions", transcribe_client.transcription_url)
        self.assertEqual("groq_test_key", transcribe_client.transcription_api_key)

    def test_legacy_config_backward_compatibility(self):
        import json
        from ui.main_window import MainWindow

        fd, temp_cfg = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            legacy_cfg = {
                "api_key": "old_key",
                "base_url": "http://127.0.0.1:8000/v1",
                "model": "mistral-medium",
                "transcription_model": "voxtral-mini",
            }
            with open(temp_cfg, "w", encoding="utf-8") as f:
                json.dump(legacy_cfg, f)

            win = MainWindow()
            with patch.object(win, "_config_path", return_value=Path(temp_cfg)):
                win._load_config()

            self.assertEqual("old_key", win.input_apikey.text())
            self.assertEqual("http://127.0.0.1:8000/v1", win.input_base_url.text())
            # transcription_base_url falls back to base_url for legacy configs
            self.assertEqual("http://127.0.0.1:8000/v1", win.input_transcription_base_url.text())
            self.assertEqual("voxtral-mini", win.combo_transcription_model.currentText())
        finally:
            try:
                os.remove(temp_cfg)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()

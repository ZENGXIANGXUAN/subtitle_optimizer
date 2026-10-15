"""Unit tests for subtitle transcription MCP server."""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp_server import (
    batch_transcribe,
    check_transcription_service,
    get_current_config,
    get_transcription_config,
    resolve_language,
    server,
    transcribe_media,
)


class TestMCPServer(unittest.IsolatedAsyncioTestCase):
    def test_server_metadata(self):
        """Test MCP server instance name and tool registration."""
        self.assertEqual(server.name, "subtitle-transcription")
        registered = [t.name for t in server._tool_manager.list_tools()]
        expected_tools = [
            "transcribe_media",
            "batch_transcribe",
            "check_transcription_service",
            "get_transcription_config",
        ]
        for name in expected_tools:
            self.assertIn(name, registered)

    def test_resolve_language(self):
        """Test language resolution helper."""
        self.assertEqual(resolve_language("English"), "en")
        self.assertEqual(resolve_language("en"), "en")
        self.assertEqual(resolve_language("中文"), "zh")
        self.assertEqual(resolve_language("zh"), "zh")
        self.assertIsNone(resolve_language("auto"))
        self.assertIsNone(resolve_language(""))
        self.assertIsNone(resolve_language(None, default_lang="auto"))
        self.assertEqual(resolve_language(None, default_lang="English"), "en")

    async def test_get_transcription_config(self):
        """Test config extraction and masking."""
        res = await get_transcription_config()
        self.assertIn("transcription_provider", res)
        self.assertIn("transcription_base_url", res)
        self.assertIn("transcription_model", res)
        self.assertIn("masked_api_key", res)

    async def test_transcribe_media_file_not_found(self):
        """Test file validation on non-existent media file."""
        res = await transcribe_media("non_existent_file_path.mp4")
        self.assertFalse(res["success"])
        self.assertIn("not found", res["error"].lower())

    async def test_transcribe_media_success_mock(self):
        """Test successful single-file transcription with mocked engine."""
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            temp_media = f.name

        sample_srt = (
            "1\n00:00:00,000 --> 00:00:02,000\nHello world\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\nThis is a test\n"
        )

        try:
            with patch(
                "mcp_server.transcribe_media_to_srt",
                new=AsyncMock(return_value=(temp_media.replace(".mp4", ".srt"), sample_srt)),
            ), patch("mcp_server.get_audio_duration", return_value=4.0):
                res = await transcribe_media(
                    media_path=temp_media,
                    chunked=False,
                )
                self.assertTrue(res["success"])
                self.assertEqual(res["cue_count"], 2)
                self.assertEqual(res["duration_seconds"], 4.0)
                self.assertIn("Hello world", res["preview"])
        finally:
            if os.path.exists(temp_media):
                os.remove(temp_media)

    async def test_batch_transcribe_empty(self):
        """Test batch transcription when no valid files are provided."""
        res = await batch_transcribe(media_paths=[])
        self.assertFalse(res["success"])
        self.assertEqual(res["total"], 0)

    async def test_batch_transcribe_mock(self):
        """Test batch transcription with mocked single-file transcriber."""
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f1, \
             tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f2:
            file1 = f1.name
            file2 = f2.name

        try:
            fake_result = {
                "success": True,
                "srt_path": "test.srt",
                "cue_count": 5,
            }
            with patch("mcp_server.transcribe_media", new=AsyncMock(return_value=fake_result)):
                res = await batch_transcribe(media_paths=[file1, file2])
                self.assertTrue(res["success"])
                self.assertEqual(res["total"], 2)
                self.assertEqual(res["succeeded"], 2)
                self.assertEqual(res["failed"], 0)
        finally:
            for p in (file1, file2):
                if os.path.exists(p):
                    os.remove(p)


if __name__ == "__main__":
    unittest.main()

import asyncio
import subprocess
import tempfile
import unittest
from unittest import mock

from core import transcription


class TranscriptionAudioGuardTests(unittest.TestCase):
    def test_extract_audio_rejects_video_without_audio_stream(self):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
            raise AssertionError("ffmpeg should not run when the video has no audio stream")

        with mock.patch.object(transcription.subprocess, "run", fake_run):
            with self.assertRaisesRegex(RuntimeError, "no audio track"):
                transcription.extract_audio_from_video("silent-video.mp4")

    def test_extract_audio_skips_local_silence_scan_for_audio_stream(self):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe" and "stream=codec_name" in cmd:
                raise AssertionError("audio codec probing should not run during extraction")
            if cmd[0] == "ffprobe" and "stream=index" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=b"0\n", stderr=b"")
            if cmd[0] == "ffmpeg" and "-af" in cmd:
                raise AssertionError("volumedetect should not run during extraction")
            if cmd[0] == "ffmpeg" and "copy" in cmd:
                raise AssertionError("stream copy should not run during extraction")
            if cmd[0] == "ffmpeg":
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
            raise AssertionError(f"unexpected command: {cmd}")

        with mock.patch.object(transcription.subprocess, "run", fake_run):
            with mock.patch.object(transcription.os.path, "exists", return_value=True):
                with mock.patch.object(transcription.os.path, "getsize", return_value=1024):
                    audio_path = transcription.extract_audio_from_video("video-with-audio.mp4")

        self.assertTrue(audio_path.endswith(".mp3"))

    def test_extract_audio_uses_transcode_without_stream_copy(self):
        commands = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            if cmd[0] == "ffprobe" and "stream=codec_name" in cmd:
                raise AssertionError("audio codec probing should not run during extraction")
            if cmd[0] == "ffprobe" and "stream=index" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=b"0\n", stderr=b"")
            if cmd[0] == "ffmpeg" and "copy" in cmd:
                raise AssertionError("stream copy should not run during extraction")
            if cmd[0] == "ffmpeg":
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
            raise AssertionError(f"unexpected command: {cmd}")

        with mock.patch.object(transcription.subprocess, "run", fake_run):
            with mock.patch.object(transcription.os.path, "exists", return_value=True):
                with mock.patch.object(transcription.os.path, "getsize", return_value=1024):
                    audio_path = transcription.extract_audio_from_video("video-with-audio.mp4")

        ffmpeg_commands = [cmd for cmd in commands if cmd[0] == "ffmpeg"]
        self.assertEqual(1, len(ffmpeg_commands))
        self.assertIn("libmp3lame", ffmpeg_commands[0])
        self.assertNotIn("copy", ffmpeg_commands[0])
        self.assertTrue(audio_path.endswith(".mp3"))

    def test_empty_transcription_result_reports_no_transcribable_audio(self):
        with self.assertRaisesRegex(RuntimeError, "No transcribable audio"):
            transcription.generate_srt_content({"segments": [], "text": ""})

    def test_transcribe_media_to_srt_does_not_call_api_for_video_without_audio(self):
        class Client:
            async def transcribe_audio(self, *args, **kwargs):
                raise AssertionError("transcribe_audio should not be called")

        with mock.patch.object(transcription, "has_audio_stream", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "no audio track"):
                asyncio.run(transcription.transcribe_media_to_srt(Client(), "silent-video.mp4"))

    def test_transcribe_media_to_srt_logs_detailed_transcription_stages(self):
        class Client:
            async def transcribe_audio(self, *args, **kwargs):
                return {"segments": [{"start": 0, "end": 1, "text": "hello"}]}

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffprobe" and "stream=codec_name" in cmd:
                raise AssertionError("audio codec probing should not run during extraction")
            if cmd[0] == "ffprobe" and "stream=index" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=b"0\n", stderr=b"")
            if cmd[0] == "ffmpeg":
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
            raise AssertionError(f"unexpected command: {cmd}")

        logs = []
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = f"{tmpdir}\\out.srt"
            with mock.patch.object(transcription.subprocess, "run", fake_run):
                with mock.patch.object(transcription.os.path, "exists", return_value=True):
                    with mock.patch.object(transcription.os.path, "getsize", return_value=2048):
                        with mock.patch.object(transcription.os, "remove"):
                            asyncio.run(transcription.transcribe_media_to_srt(
                                Client(),
                                "video.mp4",
                                output_path=output_path,
                                on_log=logs.append,
                            ))

        joined = "\n".join(logs)
        self.assertIn("Checking audio stream:", joined)
        self.assertIn("Audio stream check complete: found", joined)
        self.assertIn("Extracting audio with ffmpeg transcode:", joined)
        self.assertIn("Audio extraction complete:", joined)
        self.assertIn("Sending audio to transcription API:", joined)
        self.assertIn("Transcription API response received:", joined)
        self.assertIn("SRT write complete:", joined)
        self.assertIn("Temporary audio removed:", joined)


if __name__ == "__main__":
    unittest.main()

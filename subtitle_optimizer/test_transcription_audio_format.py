import os
import unittest
from unittest.mock import patch

from core import transcription


class TranscriptionAudioFormatTests(unittest.TestCase):
    def _extract_with_format(self, audio_format):
        commands = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            return type("Result", (), {"stdout": b"0\n", "stderr": b""})()

        with (
            patch("core.transcription.subprocess.run", side_effect=fake_run),
            patch("core.transcription._valid_audio_file", return_value=True),
            patch("core.transcription.os.path.getsize", return_value=1024),
        ):
            audio_path = transcription.extract_audio_from_video(
                "sample.mp4",
                audio_format=audio_format,
            )

        try:
            os.remove(audio_path)
        except OSError:
            pass
        return audio_path, commands[-1]

    def test_default_extraction_uses_mp3_transcode(self):
        audio_path, ffmpeg_cmd = self._extract_with_format("mp3")

        self.assertEqual(".mp3", os.path.splitext(audio_path)[1])
        self.assertIn("libmp3lame", ffmpeg_cmd)
        self.assertIn("64k", ffmpeg_cmd)

    def test_wav_extraction_uses_pcm_wav_transcode(self):
        audio_path, ffmpeg_cmd = self._extract_with_format("wav")

        self.assertEqual(".wav", os.path.splitext(audio_path)[1])
        self.assertIn("pcm_s16le", ffmpeg_cmd)
        self.assertIn("aresample=async=1", ffmpeg_cmd)
        self.assertNotIn("libmp3lame", ffmpeg_cmd)


if __name__ == "__main__":
    unittest.main()

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from core import transcription


class TranscriptionAudioFormatTests(unittest.TestCase):
    def test_default_chunk_overlap_prioritizes_boundary_context(self):
        self.assertEqual(2 * 60, transcription.CHUNK_OVERLAP)

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

    def test_chunked_transcription_logs_the_configured_worker_count(self):
        class FakeClient:
            async def transcribe_audio(self, *_args, **_kwargs):
                return {
                    "segments": [{"start": 0.0, "end": 0.5, "text": "hello"}],
                    "text": "hello",
                }

        logs = []
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "output.srt")
            with (
                patch.object(
                    transcription,
                    "prepare_transcription_audio",
                    return_value=("audio.mp3", None),
                ),
                patch.object(transcription, "get_audio_duration", return_value=2.0),
                patch.object(transcription, "extract_audio_chunk"),
                patch.object(transcription, "_file_size_label", return_value="1 B"),
            ):
                actual_path, content = asyncio.run(
                    transcription.transcribe_chunked_to_srt(
                        FakeClient(),
                        "input.mp3",
                        output_path=output_path,
                        chunk_duration=1.0,
                        overlap=0.0,
                        concurrency=2,
                        on_log=logs.append,
                    )
                )

        self.assertEqual(output_path, actual_path)
        self.assertIn("hello", content)
        self.assertIn("Starting concurrent transcription (2 workers)...", logs)

    def test_chunk_merge_assigns_each_overlap_timestamp_once(self):
        results = [
            {"segments": [
                {"start": 0, "end": 0.8, "text": "1"},
                {"start": 1, "end": 1.8, "text": "2"},
                {"start": 2, "end": 2.8, "text": "3"},
                {"start": 3, "end": 3.8, "text": "4"},
                {"start": 4, "end": 4.8, "text": "5"},
            ]},
            {"segments": [
                {"start": 0, "end": 0.8, "text": "3"},
                {"start": 1, "end": 1.8, "text": "4"},
                {"start": 2, "end": 2.8, "text": "5"},
                {"start": 3, "end": 3.8, "text": "6"},
                {"start": 4, "end": 4.8, "text": "7"},
            ]},
            {"segments": [
                {"start": 0, "end": 0.8, "text": "5"},
                {"start": 1, "end": 1.8, "text": "6"},
                {"start": 2, "end": 2.8, "text": "7"},
                {"start": 3, "end": 3.8, "text": "8"},
                {"start": 4, "end": 4.8, "text": "9"},
            ]},
        ]

        merged = transcription._merge_chunk_segments(
            results,
            chunk_starts=[0, 2, 4],
            overlap=3,
        )

        self.assertEqual(list("123456789"), [segment["text"] for segment in merged])
        self.assertEqual(sorted(segment["start"] for segment in merged), [
            segment["start"] for segment in merged
        ])

    def test_chunk_merge_assigns_boundary_segment_to_newer_chunk(self):
        merged = transcription._merge_chunk_segments(
            [
                {"segments": [{"start": 3, "end": 4, "text": "older"}]},
                {"segments": [{"start": 1, "end": 2, "text": "newer"}]},
            ],
            chunk_starts=[0, 2],
            overlap=3,
        )

        self.assertEqual(["newer"], [segment["text"] for segment in merged])

    def test_chunk_merge_keeps_a_complete_cue_when_the_next_chunk_splits_it(self):
        merged = transcription._merge_chunk_segments(
            [
                {"segments": [
                    {"start": 850, "end": 858, "text": "before"},
                    {"start": 860, "end": 885, "text": "complete sentence"},
                ]},
                {"segments": [
                    {"start": 20, "end": 30, "text": "complete"},
                    {"start": 30, "end": 45, "text": "sentence"},
                    {"start": 45, "end": 50, "text": "after"},
                ]},
            ],
            chunk_starts=[0, 840],
            overlap=60,
        )

        self.assertEqual(
            ["before", "complete sentence", "after"],
            [segment["text"] for segment in merged],
        )

    def test_chunk_merge_preserves_a_cross_boundary_cue_without_a_later_join(self):
        merged = transcription._merge_chunk_segments(
            [
                {"segments": [
                    {"start": 860, "end": 885, "text": "complete sentence"},
                ]},
                {"segments": [
                    {"start": 20, "end": 30, "text": "complete"},
                    {"start": 30, "end": 45, "text": "sentence"},
                ]},
            ],
            chunk_starts=[0, 840],
            overlap=60,
        )

        self.assertEqual(
            ["complete sentence"],
            [segment["text"] for segment in merged],
        )


if __name__ == "__main__":
    unittest.main()

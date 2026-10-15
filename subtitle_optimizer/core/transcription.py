import asyncio
from difflib import SequenceMatcher
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

try:
    from ..utils import logger
except ImportError:
    import sys

    sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
    from utils import logger


VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".ts", ".mts",
}
AUDIO_EXTENSIONS = {
    ".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".opus",
}
SRT_EXTENSIONS = {".srt"}
NO_TRANSCRIBABLE_AUDIO_ERROR = "No transcribable audio was detected in this media file."
SUPPORTED_AUDIO_EXTRACTION_FORMATS = {"mp3", "wav"}
WORD_CUE_PAUSE_SECONDS = 0.8
WORD_CUE_MAX_DURATION_SECONDS = 6.0
WORD_CUE_MAX_CHARACTERS = 84
WORD_CUE_MAX_WORDS = 18
AUDIO_ALIGNMENT_SILENCE_DB = -35
AUDIO_ALIGNMENT_MIN_SILENCE_SECONDS = 0.6
AUDIO_ALIGNMENT_MAX_EXTENSION_SECONDS = 6.0

_TERMINAL_PUNCTUATION_RE = re.compile(r'[.!?。！？…][\"\'”’）)\]】」』]*$')
_NO_SPACE_BEFORE_RE = re.compile(r'^[,.;:!?%。，、；：！？…\"\'”’）)\]】」』]')
_NO_SPACE_AFTER_RE = re.compile(r'[（(\[【「『\"\'“‘]$')
_CONTRACTION_RE = re.compile(r"^(?:n['’]t|['’](?:s|re|ve|ll|d|m))$", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")


def _emit_log(on_log: Callable[[str], None] | None, message: str):
    logger.info(message)
    if on_log:
        on_log(message)


def _elapsed(start: float) -> str:
    return f"{time.perf_counter() - start:.1f}s"


def _file_size_label(file_path: str) -> str:
    try:
        size = os.path.getsize(file_path)
    except OSError:
        return "unknown size"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.2f} MB"


def is_video_file(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in VIDEO_EXTENSIONS


def is_audio_file(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in AUDIO_EXTENSIONS


def is_media_file(file_path: str) -> bool:
    return is_video_file(file_path) or is_audio_file(file_path)


def is_srt_file(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in SRT_EXTENSIONS


def media_file_filter() -> str:
    return (
        "Supported files (*.srt *.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm "
        "*.m4v *.ts *.mts *.wav *.mp3 *.flac *.ogg *.m4a *.aac *.wma *.opus);;"
        "SRT subtitles (*.srt);;"
        "Audio / video (*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.ts *.mts "
        "*.wav *.mp3 *.flac *.ogg *.m4a *.aac *.wma *.opus);;"
        "All files (*)"
    )


def get_content_type(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    content_types = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".opus": "audio/opus",
    }
    return content_types.get(ext, "application/octet-stream")


def format_srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0))
    millis_total = int(round(seconds * 1000))
    h = millis_total // 3600000
    millis_total %= 3600000
    m = millis_total // 60000
    millis_total %= 60000
    s = millis_total // 1000
    ms = millis_total % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def default_srt_output_path(input_path: str) -> str:
    path = Path(input_path)
    return str(path.with_suffix(".srt"))


def _temp_audio_path(output_format: str) -> str:
    tmp = tempfile.NamedTemporaryFile(
        delete=False,
        prefix="subtitle_optimizer_audio_",
        suffix=f".{output_format}",
    )
    tmp.close()
    return tmp.name


def _media_probe_run_kwargs(check: bool = True) -> dict:
    run_kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "check": check,
    }
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        run_kwargs["startupinfo"] = startupinfo
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return run_kwargs


def has_audio_stream(video_path: str, on_log: Callable[[str], None] | None = None) -> bool:
    start = time.perf_counter()
    _emit_log(on_log, f"Checking audio stream: {Path(video_path).name}")
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, **_media_probe_run_kwargs())
    except FileNotFoundError as exc:
        raise RuntimeError("ffprobe is not installed or is not available in PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(f"ffprobe failed to inspect audio streams: {detail[:500]}") from exc
    found = bool(result.stdout.decode("utf-8", errors="ignore").strip())
    status = "found" if found else "missing"
    _emit_log(on_log, f"Audio stream check complete: {status} ({_elapsed(start)})")
    return found


def _valid_audio_file(audio_path: str) -> bool:
    return os.path.exists(audio_path) and os.path.getsize(audio_path) > 0


def _normalize_audio_format(audio_format: str | None) -> str:
    normalized = (audio_format or "mp3").strip().lower()
    if normalized not in SUPPORTED_AUDIO_EXTRACTION_FORMATS:
        return "mp3"
    return normalized


def _audio_transcode_args(audio_format: str) -> list[str]:
    if audio_format == "wav":
        return [
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-af",
            "aresample=async=1",
        ]
    return [
        "-acodec",
        "libmp3lame",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-b:a",
        "64k",
    ]


def _transcode_audio(
    video_path: str,
    audio_format: str = "mp3",
    on_log: Callable[[str], None] | None = None,
) -> str:
    audio_format = _normalize_audio_format(audio_format)
    temp_audio = _temp_audio_path(audio_format)
    codec_args = _audio_transcode_args(audio_format)
    cmd = [
        "ffmpeg",
        "-i",
        video_path,
        "-vn",
        *codec_args,
        "-y",
        temp_audio,
    ]
    start = time.perf_counter()
    _emit_log(on_log, f"Extracting audio with ffmpeg transcode: {Path(video_path).name} ({audio_format})")
    try:
        subprocess.run(cmd, **_media_probe_run_kwargs())
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is not installed or is not available in PATH") from exc
    except subprocess.CalledProcessError as exc:
        try:
            os.remove(temp_audio)
        except OSError:
            pass
        detail = exc.stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(f"ffmpeg failed to extract audio: {detail[:500]}") from exc

    if not _valid_audio_file(temp_audio):
        raise RuntimeError("ffmpeg did not create a valid audio file")
    _emit_log(
        on_log,
        f"Audio extraction complete: {temp_audio} ({_file_size_label(temp_audio)}, transcode, {_elapsed(start)})",
    )
    return temp_audio


def extract_audio_from_video(
    video_path: str,
    audio_format: str = "mp3",
    on_log: Callable[[str], None] | None = None,
    output_format: str | None = None,
) -> str:
    if not has_audio_stream(video_path, on_log=on_log):
        raise RuntimeError("Video has no audio track to transcribe")

    if output_format is not None:
        audio_format = output_format
    return _transcode_audio(video_path, audio_format=audio_format, on_log=on_log)


def prepare_transcription_audio(
    input_path: str,
    audio_format: str = "mp3",
    on_log: Callable[[str], None] | None = None,
) -> tuple[str, str | None]:
    if is_video_file(input_path):
        temp_audio = extract_audio_from_video(input_path, audio_format=audio_format, on_log=on_log)
        return temp_audio, temp_audio
    return input_path, None


def _word_chunk_text(chunk: dict) -> str:
    return str(chunk.get("text") or chunk.get("word") or "").strip()


def _join_word_chunk_text(chunks: list[dict]) -> str:
    """Join word timestamp chunks without corrupting punctuation or CJK text."""
    joined = ""
    for chunk in chunks:
        piece = _word_chunk_text(chunk)
        if not piece:
            continue
        if not joined:
            joined = piece
            continue
        if (
            _NO_SPACE_BEFORE_RE.search(piece)
            or _NO_SPACE_AFTER_RE.search(joined)
            or _CONTRACTION_RE.fullmatch(piece)
            or (_CJK_RE.search(joined[-1:]) and _CJK_RE.search(piece[:1]))
        ):
            joined += piece
        else:
            joined += f" {piece}"
    return joined.strip()


def _normalized_word_chunks(chunks: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        text = _word_chunk_text(chunk)
        if not text:
            continue
        try:
            start = float(chunk.get("start", 0) or 0)
            end = float(chunk.get("end", start) or start)
        except (TypeError, ValueError):
            continue
        if end < start:
            continue
        prepared = {"start": start, "end": end, "text": text}
        if chunk.get("speaker_id") is not None:
            prepared["speaker_id"] = chunk["speaker_id"]
        normalized.append(prepared)
    return sorted(normalized, key=lambda chunk: (chunk["start"], chunk["end"]))


def _group_word_timestamp_chunks(chunks: list[dict]) -> list[dict]:
    """Build readable cues whose boundaries come from actual spoken words."""
    words = _normalized_word_chunks(chunks)
    grouped: list[dict] = []
    current: list[dict] = []

    def flush() -> None:
        if not current:
            return
        cue = {
            "start": current[0]["start"],
            "end": max(word["end"] for word in current),
            "text": _join_word_chunk_text(current),
        }
        if current[0].get("speaker_id") is not None:
            cue["speaker_id"] = current[0]["speaker_id"]
        if cue["text"]:
            grouped.append(cue)
        current.clear()

    for word in words:
        if current:
            previous = current[-1]
            pause = max(0.0, word["start"] - previous["end"])
            projected_text = _join_word_chunk_text([*current, word])
            projected_duration = word["end"] - current[0]["start"]
            previous_speaker = previous.get("speaker_id")
            current_speaker = word.get("speaker_id")
            speaker_changed = (
                previous_speaker is not None
                and current_speaker is not None
                and previous_speaker != current_speaker
            )
            if (
                speaker_changed
                or pause >= WORD_CUE_PAUSE_SECONDS
                or projected_duration > WORD_CUE_MAX_DURATION_SECONDS
                or len(projected_text) > WORD_CUE_MAX_CHARACTERS
                or len(current) >= WORD_CUE_MAX_WORDS
            ):
                flush()

        current.append(word)
        if _TERMINAL_PUNCTUATION_RE.search(word["text"]):
            flush()

    flush()
    return grouped


def _subtitle_segments_from_result(result_data: dict) -> list[dict]:
    """Return subtitle-ready segments, preferring precise word timestamps."""
    words = result_data.get("words") or []
    if isinstance(words, list) and words:
        return _group_word_timestamp_chunks(words)

    segments = result_data.get("segments") or []
    if result_data.get("_timestamp_granularity") == "word":
        return _group_word_timestamp_chunks(segments)
    return segments


def _detect_long_silences(audio_path: str) -> list[tuple[float, float]]:
    """Return meaningful silence spans used to close subtitle cues."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        audio_path,
        "-vn",
        "-af",
        (
            f"silencedetect=noise={AUDIO_ALIGNMENT_SILENCE_DB}dB:"
            f"d={AUDIO_ALIGNMENT_MIN_SILENCE_SECONDS}"
        ),
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(cmd, **_media_probe_run_kwargs())
    output = result.stderr.decode("utf-8", errors="ignore")
    events = re.finditer(r"silence_(start|end):\s*([0-9.]+)", output)
    intervals: list[tuple[float, float]] = []
    pending_start: float | None = None
    for event in events:
        value = float(event.group(2))
        if event.group(1) == "start":
            pending_start = value
        elif pending_start is not None and value >= pending_start:
            intervals.append((pending_start, value))
            pending_start = None
    return intervals


def _align_subtitle_ends_to_silences(
    segments: list[dict],
    silence_intervals: list[tuple[float, float]],
    max_extension: float = AUDIO_ALIGNMENT_MAX_EXTENSION_SECONDS,
) -> tuple[list[dict], int]:
    """Close overlapping cues and extend early ends to the next real pause."""
    aligned = [dict(segment) for segment in segments]
    silences = sorted(silence_intervals)
    changed = 0

    for index, segment in enumerate(aligned):
        try:
            cue_start = float(segment.get("start", 0) or 0)
            cue_end = float(segment.get("end", 0) or 0)
            next_start = (
                float(aligned[index + 1].get("start", cue_end) or cue_end)
                if index + 1 < len(aligned)
                else None
            )
        except (TypeError, ValueError):
            continue

        # Word timestamps occasionally overlap by a few frames, and chunk
        # handoffs can retain a longer overlap when the two ASR requests split
        # the sentence differently.  A subtitle must never remain visible over
        # the next cue: close it exactly at the next cue's first spoken word
        # before considering any forward extension.
        if (
            next_start is not None
            and cue_start < next_start < cue_end
        ):
            segment["end"] = next_start
            cue_end = next_start
            changed += 1

        # A cue that already ends inside a meaningful silence is not early.
        if any(start <= cue_end < end for start, end in silences):
            continue

        limit = cue_end + max_extension
        if next_start is not None:
            limit = min(limit, next_start)
        if limit <= cue_end:
            continue

        target = next(
            (start for start, _end in silences if cue_end < start <= limit),
            None,
        )
        if target is None and next_start is not None and next_start <= limit:
            # No meaningful pause separates the adjacent ASR cues.
            target = next_start
        if target is not None and target > cue_end + 0.05:
            segment["end"] = target
            changed += 1

    return aligned, changed


def _align_subtitle_ends_to_audio(
    segments: list[dict],
    audio_path: str,
    on_log: Callable[[str], None] | None = None,
) -> list[dict]:
    if not segments:
        return segments
    start = time.perf_counter()
    try:
        silences = _detect_long_silences(audio_path)
    except (FileNotFoundError, subprocess.CalledProcessError, OSError) as exc:
        logger.warning("Audio boundary alignment skipped: %s", exc)
        _emit_log(on_log, "Audio boundary alignment skipped; keeping API timestamps")
        return segments

    aligned, changed = _align_subtitle_ends_to_silences(segments, silences)
    _emit_log(
        on_log,
        f"Audio boundary alignment complete: {changed}/{len(segments)} cue ends adjusted "
        f"({_elapsed(start)})",
    )
    return aligned


def generate_srt_content(result_data: dict) -> str:
    segments = _subtitle_segments_from_result(result_data)
    if not segments:
        text = str(result_data.get("text") or "").strip()
        if not text:
            raise RuntimeError(NO_TRANSCRIBABLE_AUDIO_ERROR)
        segments = [{"start": 0, "end": 1, "text": text}]

    lines: list[str] = []
    for index, segment in enumerate(segments, start=1):
        start = segment.get("start", 0)
        end = segment.get("end", 0)
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        lines.append(str(index))
        lines.append(f"{format_srt_time(start)} --> {format_srt_time(end)}")
        lines.append(text)
        lines.append("")

    if not lines:
        raise RuntimeError(NO_TRANSCRIBABLE_AUDIO_ERROR)
    return "\n".join(lines)


async def transcribe_media_to_srt(
    client,
    input_path: str,
    output_path: str | None = None,
    model: str | None = None,
    language: str | None = None,
    audio_format: str = "mp3",
    keep_temp: bool = False,
    on_log: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    audio_path, temp_audio = prepare_transcription_audio(
        input_path,
        audio_format=audio_format,
        on_log=on_log,
    )

    try:
        return await transcribe_prepared_audio_to_srt(
            client,
            audio_path,
            input_path,
            output_path=output_path,
            model=model,
            language=language,
            on_log=on_log,
        )
    finally:
        if temp_audio and not keep_temp:
            try:
                os.remove(temp_audio)
                _emit_log(on_log, f"Temporary audio removed: {temp_audio}")
            except OSError:
                pass


async def transcribe_prepared_audio_to_srt(
    client,
    audio_path: str,
    source_path: str,
    output_path: str | None = None,
    model: str | None = None,
    language: str | None = None,
    on_log: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    api_start = time.perf_counter()
    _emit_log(
        on_log,
        f"Sending audio to transcription API: {Path(audio_path).name} ({_file_size_label(audio_path)})",
    )
    result = await client.transcribe_audio(
        audio_path,
        model=model,
        language=language,
        on_log=on_log,
    )
    segments = result.get("segments") or []
    text_len = len(str(result.get("text") or ""))
    _emit_log(
        on_log,
        f"Transcription API response received: {len(segments)} segments, text {text_len} chars ({_elapsed(api_start)})",
    )
    subtitle_segments = _subtitle_segments_from_result(result)
    if subtitle_segments:
        subtitle_segments = _align_subtitle_ends_to_audio(
            subtitle_segments,
            audio_path,
            on_log=on_log,
        )
        content = generate_srt_content({"segments": subtitle_segments})
    else:
        content = generate_srt_content(result)
    srt_path = output_path or default_srt_output_path(source_path)
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(content)
    _emit_log(on_log, f"SRT write complete: {srt_path} ({len(content)} chars)")
    return srt_path, content


# ===== 分段转录（大文件支持）=====

# 分段参数
CHUNK_DURATION = 15 * 60       # 每段15分钟
CHUNK_OVERLAP = 2 * 60         # 重叠2分钟，交接点距两侧边缘各60秒
CHUNK_MAX_FILE_MB = 8          # 每段最大文件大小


def get_audio_duration(audio_path: str) -> float:
    """获取音频时长（秒）

    Failures are wrapped the same way ``has_audio_stream`` wraps them. The bare
    ``float(...)`` used to surface as ``could not convert string to float: 'N/A'``
    for containers that do not report a duration, which says nothing about what
    the user should do.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        audio_path,
    ]
    try:
        result = subprocess.run(cmd, **_media_probe_run_kwargs())
    except FileNotFoundError as exc:
        raise RuntimeError("ffprobe is not installed or is not available in PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(f"ffprobe failed to read audio duration: {detail[:500]}") from exc

    raw = result.stdout.decode("utf-8", errors="ignore").strip()
    try:
        duration = float(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"ffprobe reported no usable duration for {Path(audio_path).name!r} "
            f"(got {raw!r}); the file may be corrupt or still being written"
        ) from exc
    if duration <= 0:
        raise RuntimeError(
            f"ffprobe reported a non-positive duration ({duration}) for "
            f"{Path(audio_path).name!r}"
        )
    return duration


def extract_audio_chunk(
    audio_path: str,
    start: float,
    duration: float,
    output_path: str,
    audio_format: str = "mp3",
) -> str:
    """提取音频片段"""
    codec_args = _audio_transcode_args(audio_format)
    cmd = [
        "ffmpeg", "-i", audio_path,
        "-ss", str(start),
        "-t", str(duration),
        "-vn", *codec_args,
        "-y", output_path,
    ]
    subprocess.run(cmd, **_media_probe_run_kwargs())
    if not _valid_audio_file(output_path):
        raise RuntimeError(f"Failed to extract audio chunk: {start:.1f}s +{duration:.1f}s")
    return output_path


def _clamp_chunk_plan(chunk_duration: float, overlap: float, audio_format: str) -> tuple[float, float]:
    """Return a (chunk_duration, overlap) pair that is safe to iterate and upload.

    Two independent hazards are handled here:

    * ``overlap >= chunk_duration`` makes the stride zero or negative, so
      :func:`_compute_chunk_starts` never advances and appends forever.
    * ``CHUNK_MAX_FILE_MB`` was declared but never enforced.  A 15-minute chunk
      is 6.9 MB as 64 kbps mp3 but 27.5 MB as 16 kHz mono wav, well past the
      limit the constant exists to express.
    """
    chunk_duration = max(1.0, float(chunk_duration))
    overlap = max(0.0, float(overlap))

    bytes_per_second = _encoded_bytes_per_second(audio_format)
    max_duration = (CHUNK_MAX_FILE_MB * 1024 * 1024) / bytes_per_second
    chunk_duration = min(chunk_duration, max_duration)

    # Keep a real stride: the overlap may never consume the whole chunk.
    if overlap >= chunk_duration:
        overlap = chunk_duration / 2
    return chunk_duration, overlap


def _encoded_bytes_per_second(audio_format: str) -> float:
    if _normalize_audio_format(audio_format) == "wav":
        return 16000 * 2  # pcm_s16le, 16 kHz, mono
    return 64_000 / 8  # libmp3lame, 64 kbps


def _compute_chunk_starts(total_duration: float, chunk_duration: float, overlap: float) -> list[float]:
    """计算分段起始时间"""
    stride = chunk_duration - overlap
    if stride <= 0:
        # Callers should pass a clamped plan; refuse rather than loop forever.
        raise ValueError(
            f"chunk overlap ({overlap}) must be smaller than chunk duration "
            f"({chunk_duration})"
        )
    starts = []
    pos = 0.0
    while pos < total_duration:
        starts.append(pos)
        if pos + chunk_duration >= total_duration:
            break
        pos += stride
    return starts


def _merge_chunk_segments(
    all_results: list[dict],
    chunk_starts: list[float],
    overlap: float,
) -> list[dict]:
    """Merge overlapping chunk transcripts without cutting a cue in half.

    ASR splits are not stable across two overlapping requests.  A cue that is
    one item in the earlier chunk can be two items in the later chunk.  Choosing
    each item independently by its midpoint can therefore discard both halves
    of a sentence.  Instead, every handoff selects a *cue boundary*: a prefix
    of the earlier chunk and a suffix of the later chunk.  This keeps a whole
    ASR segmentation chain on either side of the handoff.
    """

    def absolute_segments(result: dict, chunk_start: float) -> list[dict]:
        prepared: list[dict] = []
        for segment in _subtitle_segments_from_result(result):
            text = (segment.get("text") or "").strip()
            if not text:
                continue
            start = chunk_start + float(segment.get("start", 0) or 0)
            end = chunk_start + float(segment.get("end", 0) or 0)
            if end < start:
                continue
            prepared.append({"start": start, "end": end, "text": text})
        return sorted(prepared, key=lambda segment: (segment["start"], segment["end"]))

    def bridge_index(segments: list[dict], handoff: float) -> int | None:
        for index, segment in enumerate(segments):
            if segment["start"] < handoff < segment["end"]:
                return index
        return None

    def select_handoff(
        left: list[dict],
        right: list[dict],
        overlap_start: float,
        overlap_end: float,
        handoff: float,
    ) -> tuple[int, int]:
        """Return ``(left_end, right_start)`` indices for one chunk boundary."""
        if not left:
            return 0, 0
        if not right:
            return len(left), 0

        # Prefer a real join between two complete ASR cues.  A tiny timestamp
        # overlap is normal rounding noise, but a larger overlap means that
        # selecting both cues would repeat speech.
        timing_tolerance = 0.25
        joins: list[tuple[tuple[float, float, float], int, int]] = []
        for left_end in range(1, len(left) + 1):
            left_time = left[left_end - 1]["end"]
            if not overlap_start <= left_time <= overlap_end:
                continue
            for right_start, segment in enumerate(right):
                right_time = segment["start"]
                if not overlap_start <= right_time <= overlap_end:
                    continue
                if right_time + timing_tolerance < left_time:
                    continue
                joins.append((
                    (
                        abs(right_time - left_time),
                        abs((left_time + right_time) / 2 - handoff),
                        abs(left_time - handoff) + abs(right_time - handoff),
                    ),
                    left_end,
                    right_start,
                ))
        if joins:
            _, left_end, right_start = min(joins, key=lambda candidate: candidate[0])
            return left_end, right_start

        # When both chunks describe the same cue across the handoff, retain one
        # complete copy.  The newer chunk wins for this exact-overlap case to
        # preserve the established half-open ownership convention.
        left_bridge = bridge_index(left, handoff)
        right_bridge = bridge_index(right, handoff)
        if left_bridge is not None and right_bridge is not None:
            left_segment = left[left_bridge]
            right_segment = right[right_bridge]
            same_timing = (
                abs(left_segment["start"] - right_segment["start"]) <= timing_tolerance
                and abs(left_segment["end"] - right_segment["end"]) <= timing_tolerance
            )
            if same_timing:
                return left_bridge, right_bridge

        # There is no safe common cue boundary.  Keep the complete earlier cue
        # that crosses the handoff, then resume from the first later cue in the
        # newer chunk.  This is deliberately biased toward preserving words:
        # a duplicate is preferable to dropping the first half of a sentence.
        if left_bridge is not None:
            left_end = left_bridge + 1
            resume_at = left[left_bridge]["end"]
            right_start = next(
                (
                    index
                    for index, segment in enumerate(right)
                    if segment["start"] + timing_tolerance >= resume_at
                ),
                len(right),
            )
            return left_end, right_start

        if right_bridge is not None:
            right_start = right_bridge
            resume_before = right[right_bridge]["start"]
            left_end = next(
                (
                    index
                    for index, segment in enumerate(left)
                    if segment["end"] + timing_tolerance >= resume_before
                ),
                len(left),
            )
            return left_end, right_start

        # No cue touches the handoff.  The chunks have a genuine pause there,
        # so the conventional time ownership is safe.
        left_end = next(
            (index for index, segment in enumerate(left) if segment["end"] > handoff),
            len(left),
        )
        right_start = next(
            (index for index, segment in enumerate(right) if segment["start"] >= handoff),
            len(right),
        )
        return left_end, right_start

    chunks = [
        absolute_segments(result, chunk_starts[index])
        for index, result in enumerate(all_results)
    ]
    if overlap <= 0 or len(chunks) < 2:
        return sorted(
            [segment for chunk in chunks for segment in chunk],
            key=lambda segment: (segment["start"], segment["end"]),
        )

    starts = [0] * len(chunks)
    ends = [len(chunk) for chunk in chunks]
    for index in range(len(chunks) - 1):
        overlap_start = chunk_starts[index + 1]
        overlap_end = overlap_start + overlap
        handoff = overlap_start + overlap / 2
        left_end, right_start = select_handoff(
            chunks[index],
            chunks[index + 1],
            overlap_start,
            overlap_end,
            handoff,
        )
        ends[index] = min(ends[index], left_end)
        starts[index + 1] = max(starts[index + 1], right_start)

    merged = [
        segment
        for index, chunk in enumerate(chunks)
        for segment in chunk[starts[index]:ends[index]]
    ]

    # A middle chunk is cut twice: once by the boundary on its left and once by
    # the boundary on its right.  Those two decisions are made independently, so
    # they can cross -- most easily when the ASR returns one long segment that
    # spans both overlap windows -- and the chunk then contributes nothing.
    #
    # Dropping a cue is *correct* when a neighbouring chunk already transcribes
    # the same speech, which is the normal case at a handoff.  It is data loss
    # when nothing else covers that span.  So rather than trust the index
    # arithmetic, check the timeline: any dropped cue whose span the kept cues
    # do not cover is restored.  This module's stated bias applies -- a
    # duplicate is preferable to losing words, and the final lint merges
    # adjacent duplicates again.
    dropped = [
        segment
        for index, chunk in enumerate(chunks)
        for position, segment in enumerate(chunk)
        if not starts[index] <= position < ends[index]
    ]
    if dropped:
        restored = _restore_uncovered_segments(dropped, merged)
        if restored:
            logger.warning(
                "Chunk merge would have dropped %s cue(s) covering time no other "
                "chunk transcribed; restoring them: %s",
                len(restored),
                [f"{s['start']:.1f}-{s['end']:.1f}s" for s in restored[:5]],
            )
            merged.extend(restored)

    merged.sort(key=lambda segment: (segment["start"], segment["end"]))
    return _dedupe_adjacent_segments(merged)


def _covered_intervals(segments: list[dict]) -> list[tuple[float, float]]:
    """Union of the time spans a set of segments occupies."""
    spans = sorted(
        (segment["start"], segment["end"])
        for segment in segments
        if segment["end"] > segment["start"]
    )
    union: list[tuple[float, float]] = []
    for start, end in spans:
        if union and start <= union[-1][1]:
            union[-1] = (union[-1][0], max(union[-1][1], end))
        else:
            union.append((start, end))
    return union


def _uncovered_duration(segment: dict, union: list[tuple[float, float]]) -> float:
    start, end = segment["start"], segment["end"]
    uncovered = max(0.0, end - start)
    for span_start, span_end in union:
        if span_end <= start:
            continue
        if span_start >= end:
            break
        uncovered -= min(end, span_end) - max(start, span_start)
    return max(0.0, uncovered)


def _restore_uncovered_segments(
    dropped: list[dict],
    kept: list[dict],
    tolerance_seconds: float = 1.0,
) -> list[dict]:
    """Return dropped cues whose speech no kept cue accounts for."""
    union = _covered_intervals(kept)
    restored: list[dict] = []
    for segment in dropped:
        if _uncovered_duration(segment, union) > tolerance_seconds:
            restored.append(segment)
            # Later drops must not be restored twice for the same silence.
            union = _covered_intervals(kept + restored)
    return restored


def _dedupe_adjacent_segments(
    segments: list[dict],
    tolerance_seconds: float = 0.25,
) -> list[dict]:
    """Collapse duplicate ASR speech retained at an overlapping handoff."""

    def tokens(text: str) -> list[str]:
        return re.findall(r"\w+", str(text or "").casefold())

    def repeated_speech(left: dict, right: dict) -> bool:
        overlap = min(left["end"], right["end"]) - max(
            left["start"], right["start"]
        )
        # Small overlaps are ordinary timestamp rounding. Only inspect speech
        # that materially overlaps in time.
        if overlap <= tolerance_seconds:
            return False

        left_tokens = tokens(left["text"])
        right_tokens = tokens(right["text"])
        if not left_tokens or not right_tokens:
            return False

        left_text = " ".join(left_tokens)
        right_text = " ".join(right_tokens)
        # A complete cue and its ASR-produced prefix/suffix are unambiguous
        # duplicates even when the short repeated cue has only a few words.
        if left_text in right_text or right_text in left_text:
            return True

        # Different chunk requests can vary slightly (for example, "fact that
        # there's" vs "fact is") while transcribing the same speech. Require
        # a near-total match of the shorter cue so distinct overlapping speech
        # is never collapsed merely because it shares common words.
        if min(len(left_tokens), len(right_tokens)) < 6:
            return False
        matcher = SequenceMatcher(None, left_tokens, right_tokens, autojunk=False)
        matching = sum(block.size for block in matcher.get_matching_blocks())
        return (
            matcher.ratio() >= 0.85
            and matching / min(len(left_tokens), len(right_tokens)) >= 0.9
        )

    deduped: list[dict] = []
    for segment in segments:
        previous = deduped[-1] if deduped else None
        if (
            previous is not None
            and previous["text"] == segment["text"]
            and abs(previous["start"] - segment["start"]) <= tolerance_seconds
            and abs(previous["end"] - segment["end"]) <= tolerance_seconds
        ):
            continue
        if previous is not None and repeated_speech(previous, segment):
            previous_tokens = tokens(previous["text"])
            current_tokens = tokens(segment["text"])
            winner = segment if len(current_tokens) > len(previous_tokens) else previous
            kept = dict(winner)
            kept["start"] = min(previous["start"], segment["start"])
            kept["end"] = max(previous["end"], segment["end"])
            deduped[-1] = kept
            continue
        deduped.append(segment)
    return deduped


async def _transcribe_chunk_concurrent(
    client,
    chunk_path: str,
    chunk_index: int,
    model: str | None,
    language: str | None,
    on_log: Callable[[str], None] | None,
) -> dict:
    """转录单个分段"""
    size_label = _file_size_label(chunk_path)
    _emit_log(on_log, f"Chunk {chunk_index + 1}: uploading ({size_label})...")
    start = time.perf_counter()
    result = await client.transcribe_audio(
        chunk_path,
        model=model,
        language=language,
        on_log=on_log,
    )
    segments = result.get("segments") or []
    _emit_log(
        on_log,
        f"Chunk {chunk_index + 1}: done ({len(segments)} segments, {_elapsed(start)})",
    )
    return result


async def transcribe_chunked_to_srt(
    client,
    input_path: str,
    output_path: str | None = None,
    model: str | None = None,
    language: str | None = None,
    audio_format: str = "mp3",
    chunk_duration: float = CHUNK_DURATION,
    overlap: float = CHUNK_OVERLAP,
    concurrency: int = 4,
    on_log: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    """
    分段转录大文件，支持并发

    Args:
        client: MistralClient 实例
        input_path: 音频/视频文件路径
        output_path: 输出 SRT 路径（可选）
        model: 转录模型
        language: 语言
        audio_format: 音频格式（mp3/wav）
        chunk_duration: 每段时长（秒）
        overlap: 重叠时长（秒）
        concurrency: 并发数
        on_log: 日志回调

    Returns:
        (srt_path, srt_content)
    """
    total_start = time.perf_counter()
    # Bind before the try: the finally block references it, and an early failure
    # in get_audio_duration() would otherwise raise NameError there.
    tmp_dir: str | None = None

    requested_chunk_duration, requested_overlap = chunk_duration, overlap
    chunk_duration, overlap = _clamp_chunk_plan(chunk_duration, overlap, audio_format)
    if (chunk_duration, overlap) != (requested_chunk_duration, requested_overlap):
        _emit_log(
            on_log,
            f"Chunk plan clamped for {_normalize_audio_format(audio_format)}: "
            f"{requested_chunk_duration / 60:.1f}min/{requested_overlap / 60:.1f}min "
            f"-> {chunk_duration / 60:.1f}min/{overlap / 60:.1f}min "
            f"(max {CHUNK_MAX_FILE_MB} MB per chunk)",
        )

    # 1. 准备音频
    audio_path, temp_audio = prepare_transcription_audio(
        input_path, audio_format=audio_format, on_log=on_log,
    )

    try:
        # 2. 获取时长
        total_duration = get_audio_duration(audio_path)
        _emit_log(on_log, f"Audio duration: {total_duration / 60:.1f} min")

        # 3. 计算分段
        chunk_starts = _compute_chunk_starts(total_duration, chunk_duration, overlap)
        num_chunks = len(chunk_starts)
        _emit_log(
            on_log,
            f"Chunked transcription: {num_chunks} chunks "
            f"({chunk_duration / 60:.0f}min each, {overlap / 60:.0f}min overlap, "
            f"concurrency={concurrency})",
        )

        # 4. 提取所有分段
        tmp_dir = tempfile.mkdtemp(prefix="subtitle_optimizer_chunk_")
        chunk_paths: list[str] = []
        for i, chunk_start in enumerate(chunk_starts):
            remaining = total_duration - chunk_start
            chunk_dur = min(chunk_duration, remaining)
            chunk_path = os.path.join(tmp_dir, f"chunk_{i:03d}.{audio_format}")
            _emit_log(
                on_log,
                f"Extracting chunk {i + 1}/{num_chunks}: "
                f"{chunk_start / 60:.1f} - {(chunk_start + chunk_dur) / 60:.1f} min",
            )
            extract_audio_chunk(audio_path, chunk_start, chunk_dur, chunk_path, audio_format)
            chunk_paths.append(chunk_path)

        # 5. 并发转录
        _emit_log(on_log, f"Starting concurrent transcription ({concurrency} workers)...")
        semaphore = asyncio.Semaphore(concurrency)

        async def _limited_transcribe(idx: int, path: str):
            async with semaphore:
                return await _transcribe_chunk_concurrent(
                    client, path, idx, model, language, on_log,
                )

        tasks = [_limited_transcribe(i, p) for i, p in enumerate(chunk_paths)]
        all_results = await asyncio.gather(*tasks, return_exceptions=True)

        # 检查是否有失败的分段
        failed = [i for i, r in enumerate(all_results) if isinstance(r, Exception)]
        if failed:
            _emit_log(
                on_log,
                f"WARNING: {len(failed)} chunks failed: {[i + 1 for i in failed]}; "
                "retrying them sequentially",
            )
            # client.transcribe_audio() already retried each chunk internally, so
            # retry once more one at a time: a burst of concurrent requests is a
            # common cause of the gateway errors that produced these failures.
            still_failed: list[int] = []
            for i in failed:
                if asyncio.iscoroutine(all_results[i]):  # pragma: no cover - defensive
                    continue
                try:
                    all_results[i] = await _transcribe_chunk_concurrent(
                        client, chunk_paths[i], i, model, language, on_log,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _emit_log(on_log, f"Chunk {i + 1} failed again: {exc}")
                    still_failed.append(i)

            if still_failed:
                # Substituting an empty transcript here used to return success
                # with a hole in it: the caller wrote an SRT that silently
                # omitted the whole span and then spent a full translation run
                # on it. Refuse instead, naming the spans that are missing.
                gaps = ", ".join(
                    f"{chunk_starts[i] / 60:.1f}-"
                    f"{min(chunk_starts[i] + chunk_duration, total_duration) / 60:.1f}min"
                    for i in still_failed
                )
                raise RuntimeError(
                    f"Transcription failed for {len(still_failed)} of {num_chunks} "
                    f"audio chunks after retry; the transcript would be missing "
                    f"these spans: {gaps}"
                )
            _emit_log(on_log, f"Sequential retry recovered all {len(failed)} chunks")

        # 6. 合并结果
        _emit_log(on_log, "Merging segments...")
        merged = _merge_chunk_segments(all_results, chunk_starts, overlap)
        _emit_log(on_log, f"Merged: {len(merged)} segments from {num_chunks} chunks")
        merged = _align_subtitle_ends_to_audio(merged, audio_path, on_log=on_log)

        # 7. 生成 SRT
        result_data = {"segments": merged}
        content = generate_srt_content(result_data)
        srt_path = output_path or default_srt_output_path(input_path)
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(content)

        _emit_log(
            on_log,
            f"Chunked transcription complete: {srt_path} "
            f"({len(content)} chars, {_elapsed(total_start)})",
        )
        return srt_path, content

    finally:
        # 清理临时文件
        if temp_audio:
            try:
                os.remove(temp_audio)
                _emit_log(on_log, f"Temporary audio removed: {temp_audio}")
            except OSError:
                pass
        # 清理分段文件
        if tmp_dir:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)


def should_use_chunked_transcription(input_path: str, chunk_threshold_mb: float = 20) -> bool:
    """判断是否应该使用分段转录"""
    try:
        size_mb = os.path.getsize(input_path) / (1024 * 1024)
        return size_mb > chunk_threshold_mb
    except OSError:
        return False

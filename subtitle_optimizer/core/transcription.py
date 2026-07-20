import os
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


def generate_srt_content(result_data: dict) -> str:
    segments = result_data.get("segments") or []
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
    content = generate_srt_content(result)
    srt_path = output_path or default_srt_output_path(source_path)
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(content)
    _emit_log(on_log, f"SRT write complete: {srt_path} ({len(content)} chars)")
    return srt_path, content


# ===== 分段转录（大文件支持）=====

# 分段参数
CHUNK_DURATION = 15 * 60       # 每段15分钟
CHUNK_OVERLAP = 1 * 60         # 重叠1分钟
CHUNK_MAX_FILE_MB = 8          # 每段最大文件大小


def get_audio_duration(audio_path: str) -> float:
    """获取音频时长（秒）"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        audio_path,
    ]
    result = subprocess.run(cmd, **_media_probe_run_kwargs())
    return float(result.stdout.decode("utf-8", errors="ignore").strip())


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


def _compute_chunk_starts(total_duration: float, chunk_duration: float, overlap: float) -> list[float]:
    """计算分段起始时间"""
    starts = []
    pos = 0.0
    while pos < total_duration:
        starts.append(pos)
        if pos + chunk_duration >= total_duration:
            break
        pos += chunk_duration - overlap
    return starts


def _merge_chunk_segments(
    all_results: list[dict],
    chunk_starts: list[float],
    overlap: float,
) -> list[dict]:
    """合并多段转录结果，处理重叠区域"""
    merged = []

    for i, result in enumerate(all_results):
        segments = result.get("segments") or []
        chunk_start = chunk_starts[i]

        for seg in segments:
            abs_start = chunk_start + seg.get("start", 0)
            abs_end = chunk_start + seg.get("end", 0)
            text = (seg.get("text") or "").strip()
            if not text:
                continue

            # 去重重叠区域：非首段的前半重叠区域跳过
            if i > 0:
                overlap_boundary = chunk_starts[i] + overlap / 2
                if abs_start < overlap_boundary:
                    continue

            merged.append({
                "start": abs_start,
                "end": abs_end,
                "text": text,
            })

    return merged


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
        _emit_log(on_log, f"Starting concurrent transcription ({concosity} workers)...")
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
            _emit_log(on_log, f"WARNING: {len(failed)} chunks failed: {[i+1 for i in failed]}")
            # 用空结果替代失败的分段
            for i in failed:
                all_results[i] = {"segments": [], "text": ""}

        # 6. 合并结果
        _emit_log(on_log, "Merging segments...")
        merged = _merge_chunk_segments(all_results, chunk_starts, overlap)
        _emit_log(on_log, f"Merged: {len(merged)} segments from {num_chunks} chunks")

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
        try:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def should_use_chunked_transcription(input_path: str, chunk_threshold_mb: float = 20) -> bool:
    """判断是否应该使用分段转录"""
    try:
        size_mb = os.path.getsize(input_path) / (1024 * 1024)
        return size_mb > chunk_threshold_mb
    except OSError:
        return False

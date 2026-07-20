"""
分段转录测试脚本
思路：音频分段时有重叠，避免切割点丢信息
例如 123456789：
  第一段：12345
  第二段：45678（与第一段重叠 45）
  第三段：789（与第二段重叠 78）

最终合并时根据时间戳去重重叠部分
"""

import subprocess
import requests
import json
import time
import os
import tempfile
from pathlib import Path


# ===== 配置 =====
API_URL = "http://127.0.0.1:8000/v1/audio/transcriptions"
API_KEY = "sk-cgpwjts6riuptd5j232od888yekc0h9dx7c3mbpvzkopiut5"
MODEL = "voxtral-mini-latest"
LANGUAGE = "en"

# 分段参数（秒）
CHUNK_DURATION = 15 * 60      # 每段15分钟
OVERLAP_DURATION = 1 * 60     # 重叠1分钟


def get_audio_duration(audio_path: str) -> float:
    """获取音频时长（秒）"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        audio_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return float(result.stdout.strip())


def extract_chunk(audio_path: str, start: float, duration: float, output_path: str):
    """提取音频片段"""
    cmd = [
        "ffmpeg", "-i", audio_path,
        "-ss", str(start),
        "-t", str(duration),
        "-vn", "-acodec", "libmp3lame",
        "-ar", "16000", "-ac", "1", "-b:a", "64k",
        "-y", output_path
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def transcribe_chunk(chunk_path: str, chunk_index: int) -> dict:
    """转录单个音频片段"""
    size_mb = os.path.getsize(chunk_path) / (1024 * 1024)
    print(f"  上传第 {chunk_index + 1} 段 ({size_mb:.1f} MB)...")

    with open(chunk_path, "rb") as f:
        files = {"file": (Path(chunk_path).name, f, "audio/mpeg")}
        data = {"model": MODEL, "language": LANGUAGE, "timestamp_granularities": "segment"}
        response = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {API_KEY}"},
            files=files,
            data=data,
            timeout=600
        )

    if response.status_code != 200:
        raise RuntimeError(f"API错误 {response.status_code}: {response.text[:300]}")

    return response.json()


def merge_segments(all_results: list[dict], chunk_starts: list[float], overlap: float) -> list[dict]:
    """
    合并多段转录结果，处理重叠部分的时间戳

    all_results: 每段的 API 返回（含 segments）
    chunk_starts: 每段在原音频中的起始时间（秒）
    overlap: 重叠时长（秒）
    """
    merged = []

    for i, result in enumerate(all_results):
        segments = result.get("segments") or []
        chunk_start = chunk_starts[i]

        for seg in segments:
            # 将相对时间戳转换为绝对时间戳
            abs_start = chunk_start + seg.get("start", 0)
            abs_end = chunk_start + seg.get("end", 0)
            text = (seg.get("text") or "").strip()

            if not text:
                continue

            # 去重重叠区域：如果是第一段，全部保留
            # 如果不是第一段，跳过重叠区域的前半部分
            if i > 0:
                overlap_boundary = chunk_starts[i] + overlap / 2
                if abs_start < overlap_boundary:
                    continue

            merged.append({
                "start": abs_start,
                "end": abs_end,
                "text": text
            })

    return merged


def format_srt_time(seconds: float) -> str:
    """格式化 SRT 时间戳"""
    seconds = max(0.0, seconds)
    millis = int(round(seconds * 1000))
    h = millis // 3600000
    millis %= 3600000
    m = millis // 60000
    millis %= 60000
    s = millis // 1000
    ms = millis % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments: list[dict]) -> str:
    """将 segments 转换为 SRT 格式"""
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{format_srt_time(seg['start'])} --> {format_srt_time(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines)


def chunked_transcribe(audio_path: str, output_path: str):
    """主函数：分段转录"""
    print(f"=== 分段转录测试 ===")
    print(f"音频文件: {audio_path}")

    # 1. 获取总时长
    total_duration = get_audio_duration(audio_path)
    print(f"总时长: {total_duration / 60:.1f} 分钟")

    # 2. 计算分段
    chunk_starts = []
    start = 0
    while start < total_duration:
        chunk_starts.append(start)
        if start + CHUNK_DURATION >= total_duration:
            break
        start += CHUNK_DURATION - OVERLAP_DURATION

    print(f"分段数: {len(chunk_starts)}")
    print(f"每段: {CHUNK_DURATION / 60:.0f} 分钟, 重叠: {OVERLAP_DURATION / 60:.0f} 分钟")
    print()

    # 3. 逐段提取并转录
    all_results = []
    tmp_dir = tempfile.mkdtemp(prefix="chunked_transcribe_")

    for i, chunk_start in enumerate(chunk_starts):
        # 计算本段时长
        remaining = total_duration - chunk_start
        chunk_dur = min(CHUNK_DURATION, remaining)

        # 提取
        chunk_path = os.path.join(tmp_dir, f"chunk_{i:03d}.mp3")
        print(f"[段 {i + 1}/{len(chunk_starts)}] 提取 {chunk_start / 60:.1f} - {(chunk_start + chunk_dur) / 60:.1f} 分钟...")
        extract_chunk(audio_path, chunk_start, chunk_dur, chunk_path)

        # 转录
        start_time = time.time()
        result = transcribe_chunk(chunk_path, i)
        elapsed = time.time() - start_time

        segments = result.get("segments") or []
        text_len = len(result.get("text", ""))
        print(f"  完成: {len(segments)} 段, {text_len} 字符, 耗时 {elapsed:.1f}s")

        all_results.append(result)

        # 清理临时文件
        os.remove(chunk_path)

    print()

    # 4. 合并结果
    print("合并结果...")
    merged_segments = merge_segments(all_results, chunk_starts, OVERLAP_DURATION)
    print(f"合并后共 {len(merged_segments)} 个片段")

    # 5. 生成 SRT
    srt_content = segments_to_srt(merged_segments)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(srt_content)
    print(f"SRT 已保存: {output_path} ({len(srt_content)} 字符)")

    # 6. 也保存完整 JSON
    json_path = output_path.replace(".srt", ".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"segments": merged_segments}, f, ensure_ascii=False, indent=2)
    print(f"JSON 已保存: {json_path}")

    # 清理临时目录
    try:
        os.rmdir(tmp_dir)
    except:
        pass

    print("\n=== 完成 ===")
    return merged_segments


if __name__ == "__main__":
    # 测试：转录第一个视频（2小时36分钟）
    input_audio = r"D:\BaiduNetdiskDownload\Q1\Live Charting Program 1.mp4"
    output_srt = r"D:\BaiduNetdiskDownload\Q1\Live Charting Program 1_chunked.srt"

    # 如果输入是视频，先提取 MP3
    if input_audio.endswith(".mp4"):
        mp3_path = input_audio.replace(".mp4", "_temp.mp3")
        if not os.path.exists(mp3_path):
            print("提取 MP3...")
            subprocess.run([
                "ffmpeg", "-i", input_audio,
                "-vn", "-acodec", "libmp3lame",
                "-ar", "16000", "-ac", "1", "-b:a", "64k",
                "-y", mp3_path
            ], capture_output=True, check=True)
        input_audio = mp3_path

    chunked_transcribe(input_audio, output_srt)

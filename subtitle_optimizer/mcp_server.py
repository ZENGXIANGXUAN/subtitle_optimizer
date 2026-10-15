"""Model Context Protocol (MCP) server for subtitle transcription.

Provides MCP tools to transcribe audio and video files to SRT subtitles
using the current subtitle_optimizer transcription configuration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, List, Optional

# Ensure standard streams are UTF-8 on Windows
if sys.platform == "win32":
    try:
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8")
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure the project directory is on sys.path
PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

# Logging to stderr so stdout remains clean for MCP JSON-RPC protocol
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("subtitle-transcription-mcp")

from mcp.server.mcpserver import MCPServer
import httpx

from core.client import MistralClient
from core.models import parse_srt
from core.transcription import (
    is_media_file,
    is_video_file,
    is_audio_file,
    transcribe_media_to_srt,
    transcribe_chunked_to_srt,
    should_use_chunked_transcription,
    default_srt_output_path,
    get_audio_duration,
)
from core.workers import LANGUAGE_MAP

server = MCPServer("subtitle-transcription")

CONFIG_PATH = Path.home() / ".subtitle_optimizer_config.json"


def get_current_config() -> dict[str, Any]:
    """Load configuration from ~/.subtitle_optimizer_config.json with safe defaults."""
    cfg: dict[str, Any] = {}
    if CONFIG_PATH.is_file():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as exc:
            logger.warning("Failed to load %s: %s", CONFIG_PATH, exc)

    return {
        "transcription_provider": cfg.get("transcription_provider")
        or cfg.get("translation_provider")
        or "Mistral AI",
        "transcription_api_key": cfg.get("transcription_api_key")
        or cfg.get("api_key")
        or "abc",
        "transcription_base_url": cfg.get("transcription_base_url")
        or cfg.get("base_url")
        or "http://127.0.0.1:8000/v1",
        "transcription_model": cfg.get("transcription_model")
        or cfg.get("model")
        or "voxtral-mini-latest",
        "transcription_audio_format": cfg.get("transcription_audio_format") or "mp3",
        "concurrency": int(cfg.get("concurrency", "3") or 3),
        "input_language": cfg.get("input_language") or "English",
        "config_file": str(CONFIG_PATH),
    }


def create_client(
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> tuple[MistralClient, dict[str, Any]]:
    """Create MistralClient configured for transcription using current settings."""
    cfg = get_current_config()
    t_key = (api_key or cfg["transcription_api_key"]).strip()
    t_base_url = (base_url or cfg["transcription_base_url"]).strip()
    t_model = (model or cfg["transcription_model"]).strip()
    t_provider = (provider or cfg["transcription_provider"]).strip()

    client = MistralClient(
        api_key=t_key,
        model=t_model,
        base_url=t_base_url,
        transcription_model=t_model,
        transcription_provider=t_provider,
        transcription_api_key=t_key,
        transcription_base_url=t_base_url,
    )
    return client, cfg


def resolve_language(
    lang: Optional[str], default_lang: Optional[str] = None
) -> Optional[str]:
    """Convert language code or name to standard language code for API."""
    target = (lang or default_lang or "").strip()
    if not target or target.lower() in ("auto", "none", ""):
        return None

    if target in LANGUAGE_MAP:
        return LANGUAGE_MAP[target][0]

    for _, (code, name) in LANGUAGE_MAP.items():
        if target.lower() == code.lower() or target.lower() == name.lower():
            return code

    return target


@server.tool(
    name="transcribe_media",
    description="Transcribe an audio or video file to SRT subtitle format using the current transcription configuration. Supports auto-chunking for large files. Returns output SRT path, cue count, and subtitle preview text.",
)
async def transcribe_media(
    media_path: str,
    output_path: Optional[str] = None,
    language: Optional[str] = None,
    model: Optional[str] = None,
    audio_format: Optional[str] = None,
    chunked: Optional[bool] = None,
    concurrency: Optional[int] = None,
    keep_temp_audio: bool = False,
) -> dict[str, Any]:
    """Transcribe an audio or video file to SRT format."""
    start_time = time.perf_counter()
    media_file = Path(media_path).resolve()
    if not media_file.is_file():
        return {
            "success": False,
            "error": f"Media file not found: {media_path}",
        }

    if not is_media_file(str(media_file)):
        return {
            "success": False,
            "error": f"Unsupported media format for file: {media_path}",
        }

    client, cfg = create_client(model=model)
    target_model = model or cfg["transcription_model"]
    fmt = (audio_format or cfg["transcription_audio_format"]).lower()
    if fmt not in ("mp3", "wav"):
        fmt = "mp3"

    lang_code = resolve_language(language, cfg.get("input_language"))
    dest_path = output_path or default_srt_output_path(str(media_file))
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)

    use_chunked = (
        chunked
        if chunked is not None
        else should_use_chunked_transcription(str(media_file))
    )
    parallel_limit = concurrency or cfg["concurrency"]

    logger.info(
        "Transcribing %s -> %s (model=%s, lang=%s, chunked=%s, format=%s)",
        media_file.name,
        dest_path,
        target_model,
        lang_code,
        use_chunked,
        fmt,
    )

    try:
        if use_chunked:
            final_srt_path, srt_content = await transcribe_chunked_to_srt(
                client=client,
                input_path=str(media_file),
                output_path=dest_path,
                model=target_model,
                language=lang_code,
                audio_format=fmt,
                concurrency=parallel_limit,
                on_log=lambda msg: logger.info("[transcribe] %s", msg),
            )
        else:
            final_srt_path, srt_content = await transcribe_media_to_srt(
                client=client,
                input_path=str(media_file),
                output_path=dest_path,
                model=target_model,
                language=lang_code,
                audio_format=fmt,
                keep_temp=keep_temp_audio,
                on_log=lambda msg: logger.info("[transcribe] %s", msg),
            )

        entries = parse_srt(srt_content)
        preview_entries = entries[:5]
        preview_text = "\n\n".join(e.to_srt_block_raw() for e in preview_entries)
        if len(entries) > 5:
            preview_text += f"\n\n... [{len(entries) - 5} more cues omitted]"

        duration = None
        try:
            duration = round(get_audio_duration(str(media_file)), 2)
        except Exception:
            pass

        elapsed = round(time.perf_counter() - start_time, 2)
        return {
            "success": True,
            "media_path": str(media_file),
            "srt_path": final_srt_path,
            "cue_count": len(entries),
            "duration_seconds": duration,
            "chunked": use_chunked,
            "model_used": target_model,
            "language": lang_code,
            "elapsed_seconds": elapsed,
            "preview": preview_text,
        }
    except Exception as exc:
        logger.error("Transcription failed for %s: %s", media_path, exc, exc_info=True)
        return {
            "success": False,
            "media_path": str(media_file),
            "error": str(exc),
            "elapsed_seconds": round(time.perf_counter() - start_time, 2),
        }


@server.tool(
    name="batch_transcribe",
    description="Batch transcribe multiple audio/video files or an entire folder to SRT subtitles using current configuration.",
)
async def batch_transcribe(
    media_paths: Optional[List[str]] = None,
    directory_path: Optional[str] = None,
    output_dir: Optional[str] = None,
    recursive: bool = False,
    language: Optional[str] = None,
    model: Optional[str] = None,
    audio_format: Optional[str] = None,
    concurrency: Optional[int] = None,
) -> dict[str, Any]:
    """Batch transcribe multiple audio or video files."""
    files_to_process: list[Path] = []

    if media_paths:
        for p in media_paths:
            resolved = Path(p).resolve()
            if resolved.is_file() and is_media_file(str(resolved)):
                files_to_process.append(resolved)

    if directory_path:
        dir_p = Path(directory_path).resolve()
        if dir_p.is_dir():
            pattern_walker = dir_p.rglob("*") if recursive else dir_p.iterdir()
            for item in pattern_walker:
                if item.is_file() and is_media_file(str(item)):
                    if item not in files_to_process:
                        files_to_process.append(item)

    if not files_to_process:
        return {
            "success": False,
            "error": "No valid audio or video files found to process.",
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "results": [],
        }

    cfg = get_current_config()
    batch_limit = concurrency or min(cfg["concurrency"], 4)
    sem = asyncio.Semaphore(batch_limit)

    out_folder = Path(output_dir).resolve() if output_dir else None
    if out_folder:
        out_folder.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []

    async def _process_one(media_f: Path) -> dict[str, Any]:
        async with sem:
            dest_srt = (
                str(out_folder / f"{media_f.stem}.srt")
                if out_folder
                else default_srt_output_path(str(media_f))
            )
            res = await transcribe_media(
                media_path=str(media_f),
                output_path=dest_srt,
                language=language,
                model=model,
                audio_format=audio_format,
            )
            return res

    tasks = [_process_one(f) for f in files_to_process]
    completed = await asyncio.gather(*tasks, return_exceptions=False)
    results.extend(completed)

    succeeded = sum(1 for r in results if r.get("success"))
    failed = len(results) - succeeded

    return {
        "success": failed == 0,
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }


@server.tool(
    name="check_transcription_service",
    description="Check connectivity and health of the transcription API service and verify system toolchain (FFmpeg).",
)
async def check_transcription_service(
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Check connectivity to transcription service and verify ffmpeg availability."""
    cfg = get_current_config()
    base_url = cfg["transcription_base_url"]
    provider = cfg["transcription_provider"]
    current_model = cfg["transcription_model"]

    # Check FFmpeg
    ffmpeg_bin = shutil.which("ffmpeg")
    ffmpeg_ok = False
    ffmpeg_version = "not found"
    if ffmpeg_bin:
        try:
            proc = subprocess.run(
                [ffmpeg_bin, "-version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc.returncode == 0:
                ffmpeg_ok = True
                first_line = proc.stdout.splitlines()[0] if proc.stdout else ""
                ffmpeg_version = first_line.strip()
        except Exception as exc:
            ffmpeg_version = f"error: {exc}"

    # Check API endpoint
    api_ok = False
    api_error = None
    available_models: list[str] = []
    models_url = f"{base_url.rstrip('/')}/models"

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            headers = {"Authorization": f"Bearer {cfg['transcription_api_key']}"}
            resp = await client.get(models_url, headers=headers)
            if resp.status_code == 200:
                api_ok = True
                data = resp.json()
                items = data.get("data", [])
                if isinstance(items, list):
                    available_models = [
                        m["id"]
                        for m in items
                        if isinstance(m, dict) and "id" in m
                    ]
            else:
                api_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
    except Exception as exc:
        api_error = str(exc)

    return {
        "ok": api_ok and ffmpeg_ok,
        "api_reachable": api_ok,
        "api_error": api_error,
        "base_url": base_url,
        "provider": provider,
        "configured_model": current_model,
        "available_models": available_models,
        "ffmpeg_installed": ffmpeg_ok,
        "ffmpeg_version": ffmpeg_version,
        "config": cfg,
    }


@server.tool(
    name="get_transcription_config",
    description="Retrieve the current active transcription configuration from ~/.subtitle_optimizer_config.json.",
)
async def get_transcription_config() -> dict[str, Any]:
    """Retrieve active configuration details."""
    cfg = get_current_config()
    # Mask api key partially for security
    key = cfg.get("transcription_api_key", "")
    masked_key = (key[:4] + "..." + key[-4:]) if len(key) > 8 else "***"
    return {
        "transcription_provider": cfg["transcription_provider"],
        "transcription_base_url": cfg["transcription_base_url"],
        "transcription_model": cfg["transcription_model"],
        "transcription_audio_format": cfg["transcription_audio_format"],
        "concurrency": cfg["concurrency"],
        "input_language": cfg["input_language"],
        "masked_api_key": masked_key,
        "config_file": cfg["config_file"],
    }


async def run_server_async() -> None:
    """Run the MCP server over stdio."""
    await server.run_stdio_async()


def main() -> None:
    """Console entrypoint."""
    try:
        asyncio.run(run_server_async())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()

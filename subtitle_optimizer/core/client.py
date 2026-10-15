from pathlib import Path
import asyncio
import ipaddress
import json
import random
from urllib.parse import urlparse

import aiohttp

from core.transcription import get_content_type


RETRYABLE_TRANSCRIPTION_STATUS_CODES = {408, 429, 500, 502, 503, 504}
RETRYABLE_CHAT_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
LOCAL_PROXY_CHAT_TIMEOUT_SECONDS = 360.0


def _is_loopback_url(value: str) -> bool:
    """Return whether an API base URL resolves syntactically to loopback."""
    raw = str(value or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    host = str(parsed.hostname or "").rstrip(".").casefold()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def _emit_log(on_log, message: str):
    logger.info(message)
    if on_log:
        on_log(message)


def _transcription_result_is_empty(result: dict) -> bool:
    segments = result.get("segments") or []
    text = str(result.get("text") or "").strip()
    return not segments and not text


class _RetryableTranscriptionError(RuntimeError):
    pass


class _NonRetryableChatError(RuntimeError):
    pass


def _redact_headers(headers: dict) -> dict:
    redacted = dict(headers)
    if "Authorization" in redacted:
        redacted["Authorization"] = "Bearer ***"
    return redacted


def _format_api_error(status: int, text: str, *, limit: int = 4000) -> str:
    raw = (text or "").strip()
    if not raw:
        return f"API错误 {status}: <empty response body>"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return f"API错误 {status}: {raw[:limit]}"

    if not isinstance(data, dict):
        return f"API错误 {status}: {raw[:limit]}"

    parts = [f"API错误 {status}"]
    if data.get("error"):
        parts.append(f"error={data['error']}")
    if data.get("last_error"):
        parts.append(f"last_error={data['last_error']}")

    details = data.get("details")
    if isinstance(details, list) and details:
        rendered = []
        for item in details[:5]:
            if not isinstance(item, dict):
                rendered.append(str(item))
                continue
            fields = []
            for key in ("node", "attempt", "max_retries", "endpoint", "type", "message", "status_code", "request_id", "hint"):
                if item.get(key) is not None:
                    fields.append(f"{key}={item[key]}")
            if item.get("body"):
                fields.append(f"body={str(item['body'])[:1200]}")
            rendered.append("; ".join(fields))
        parts.append("details=[" + " | ".join(rendered) + "]")
    elif isinstance(details, str) and details:
        parts.append(f"details={details[:limit]}")

    if len(parts) == 1:
        parts.append(raw[:limit])
    return ": ".join(parts[:2]) if len(parts) == 2 else " | ".join(parts)

# 尝试相对导入，如果失败则使用绝对导入
try:
    from ..utils import logger
except ImportError:
    import sys
    import os

    sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
    from utils import logger


TRANSLATION_PROVIDERS = {
    "Mistral AI": {
        "base_url": "https://api.mistral.ai/v1",
        "models": [
            "mistral-large-latest",
            "mistral-medium-latest",
            "mistral-small-latest",
            "open-mistral-7b",
        ],
        "default_model": "mistral-large-latest",
    },
    "DeepSeek": {
        "base_url": "https://api.deepseek.com/v1",
        "models": [
            "deepseek-chat",
            "deepseek-reasoner",
        ],
        "default_model": "deepseek-chat",
    },
    "OpenAI": {
        "base_url": "https://api.openai.com/v1",
        "models": [
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4.1",
            "o3-mini",
        ],
        "default_model": "gpt-4o",
    },
    "SiliconFlow (硅基流动)": {
        "base_url": "https://api.siliconflow.cn/v1",
        "models": [
            "deepseek-ai/DeepSeek-V3",
            "deepseek-ai/DeepSeek-R1",
            "Qwen/Qwen2.5-72B-Instruct",
            "THUDM/glm-4-9b-chat",
        ],
        "default_model": "deepseek-ai/DeepSeek-V3",
    },
    "自定义 (Custom)": {
        "base_url": "",
        "models": [],
        "default_model": "",
    },
}

TRANSCRIPTION_PROVIDERS = {
    "Mistral AI": {
        "base_url": "https://api.mistral.ai/v1",
        "models": [
            "voxtral-mini-latest",
        ],
        "default_model": "voxtral-mini-latest",
    },
    "Groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "models": [
            "whisper-large-v3",
            "whisper-large-v3-turbo",
            "distil-whisper-large-v3-en",
        ],
        "default_model": "whisper-large-v3",
    },
    "OpenAI": {
        "base_url": "https://api.openai.com/v1",
        "models": [
            "whisper-1",
        ],
        "default_model": "whisper-1",
    },
    "SiliconFlow (硅基流动)": {
        "base_url": "https://api.siliconflow.cn/v1",
        "models": [
            "FunAudioLLM/SenseVoiceSmall",
        ],
        "default_model": "FunAudioLLM/SenseVoiceSmall",
    },
    "自定义 (Custom)": {
        "base_url": "",
        "models": [],
        "default_model": "",
    },
}


class MistralClient:
    DEFAULT_BASE = "https://api.mistral.ai/v1"
    DEFAULT_CHAT_TEMPERATURE = 0.1

    def __init__(self, api_key: str = "", model: str = "mistral-large-latest",
                 base_url: str = "", transcription_model: str = "",
                 default_chat_temperature: float = DEFAULT_CHAT_TEMPERATURE,
                 *,
                 translation_provider: str = "Mistral AI",
                 transcription_provider: str = "Mistral AI",
                 transcription_api_key: str = "",
                 transcription_base_url: str = ""):
        self.api_key = api_key
        self.model = model
        self.default_chat_temperature = default_chat_temperature
        self.translation_provider = translation_provider

        base = base_url.strip().rstrip("/") if base_url.strip() else self.DEFAULT_BASE
        self.base_url = base
        self.is_local_proxy = _is_loopback_url(base)
        self.chat_url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"

        # Independent transcription configuration
        self.transcription_provider = transcription_provider
        self.transcription_api_key = (
            transcription_api_key.strip()
            if transcription_api_key and transcription_api_key.strip()
            else api_key
        )
        t_base_raw = transcription_base_url.strip() if transcription_base_url else ""
        t_base = t_base_raw.rstrip("/") if t_base_raw else base
        self.transcription_base_url = t_base
        self.transcription_model = (
            transcription_model.strip() if transcription_model else ""
        ) or model
        self.transcription_url = (
            t_base if t_base.endswith("/audio/transcriptions") else f"{t_base}/audio/transcriptions"
        )
        self.transcription_is_local_proxy = _is_loopback_url(t_base)
        logger.debug(
            f"MistralClient初始化完成，翻译服务商: {translation_provider}, 模型: {model}, URL: {self.chat_url}; "
            f"转录服务商: {transcription_provider}, 模型: {self.transcription_model}, URL: {self.transcription_url}"
        )

    async def chat(
        self,
        messages: list[dict],
        session: aiohttp.ClientSession,
        extra_payload: dict | None = None,
        temperature: float | None = None,
        *,
        max_retries: int = 4,
        base_retry_delay_seconds: float = 1.5,
        request_timeout_seconds: float = 360.0,
    ) -> str:
        """Call the chat-completions endpoint with bounded exponential retry.

        Retries transport errors, timeouts and transient HTTP statuses.  The
        caller keeps ownership of ``session`` so a whole pipeline stage can
        reuse one connection pool instead of creating a TCP/TLS connection per
        subtitle batch.
        """
        logger.debug(f"开始与Mistral API通信，消息数量: {len(messages)}")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.default_chat_temperature if temperature is None else temperature,
        }
        if extra_payload:
            payload.update(extra_payload)

        requested_attempts = max(1, int(max_retries or 1))
        attempts = 1 if self.is_local_proxy else requested_attempts
        base_delay = max(0.0, float(base_retry_delay_seconds or 0.0))
        requested_timeout = max(1.0, float(request_timeout_seconds))
        effective_timeout = (
            max(requested_timeout, LOCAL_PROXY_CHAT_TIMEOUT_SECONDS)
            if self.is_local_proxy
            else requested_timeout
        )
        timeout = aiohttp.ClientTimeout(total=effective_timeout)
        last_error: Exception | None = None

        if self.is_local_proxy:
            logger.debug(
                "本地代理模式：禁用客户端嵌套重试，单次请求预算 %.0f 秒",
                effective_timeout,
            )

        for attempt in range(1, attempts + 1):
            logger.info(
                f"发送API请求到: {self.chat_url}，模型: {self.model}，"
                f"尝试 {attempt}/{attempts}"
            )
            try:
                async with session.post(
                    self.chat_url,
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                ) as resp:
                    logger.debug(f"API响应状态码: {resp.status}")
                    if resp.status != 200:
                        text = await resp.text()
                        error_msg = _format_api_error(resp.status, text)
                        retryable = resp.status in RETRYABLE_CHAT_STATUS_CODES
                        logger.error(error_msg)
                        logger.error(f"请求URL: {self.chat_url}")
                        logger.error(f"请求头: {_redact_headers(headers)}")
                        # 不记录完整 messages，避免大字幕批次污染日志；仅记录关键元数据。
                        logger.error(
                            "请求负载元数据: model=%s, message_count=%s, temperature=%s",
                            payload.get("model"),
                            len(payload.get("messages") or []),
                            payload.get("temperature"),
                        )
                        if not retryable:
                            raise _NonRetryableChatError(error_msg)

                        # Some compatible gateways/test doubles omit response
                        # headers entirely.  Retry metadata is optional and
                        # must never mask the already formatted API error.
                        response_headers = getattr(resp, "headers", None)
                        retry_after = (
                            response_headers.get("Retry-After")
                            if response_headers is not None
                            else None
                        )
                        retry_after_seconds = None
                        if retry_after:
                            try:
                                retry_after_seconds = max(0.0, float(retry_after))
                            except (TypeError, ValueError):
                                retry_after_seconds = None
                        last_error = RuntimeError(error_msg)
                        if attempt < attempts:
                            delay = retry_after_seconds
                            if delay is None:
                                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)
                            logger.warning(
                                "聊天API临时失败，%.2f秒后重试 (%s/%s)",
                                delay,
                                attempt,
                                attempts,
                            )
                            await asyncio.sleep(delay)
                            continue
                        raise last_error

                    data = await resp.json(content_type=None)
                    logger.info(f"成功收到API响应，状态码: {resp.status}")
                    try:
                        content = data["choices"][0]["message"]["content"]
                    except (KeyError, IndexError, TypeError) as exc:
                        raise RuntimeError(
                            f"聊天API响应结构异常: {str(data)[:1000]}"
                        ) from exc

                    # content 偶尔以列表形式返回（多模态/thinking格式），统一提取为纯文本。
                    if isinstance(content, list):
                        content = "\n".join(
                            item.get("text", "") if isinstance(item, dict) else str(item)
                            for item in content
                        ).strip()
                    content = str(content or "").strip()
                    if not content:
                        raise RuntimeError("聊天API返回空内容")
                    return content

            except asyncio.CancelledError:
                # User cancellation is control flow, never a retryable failure.
                raise
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                last_error = exc
                logger.warning(
                    "聊天API网络/超时错误 (%s/%s): %s",
                    attempt,
                    attempts,
                    exc,
                )
                if attempt < attempts:
                    delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)
                    await asyncio.sleep(delay)
                    continue
                break
            except _NonRetryableChatError:
                raise
            except RuntimeError as exc:
                # 空内容、响应结构异常等也可能是网关瞬态问题，允许重试。
                last_error = exc
                logger.warning(
                    "聊天API响应异常 (%s/%s): %s",
                    attempt, attempts, exc,
                )
                if attempt < attempts:
                    delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)
                    await asyncio.sleep(delay)
                    continue
                break
            except Exception as exc:
                logger.error(f"Mistral API调用失败: {str(exc)}")
                # A retry/session failure must not replace a richer HTTP API
                # error already captured from an earlier response.
                if last_error is None:
                    last_error = exc
                break

        logger.error(f"Mistral API调用最终失败: {last_error}")
        logger.error(f"请求URL: {self.chat_url}")
        if isinstance(last_error, Exception):
            raise RuntimeError(str(last_error)) from last_error
        raise RuntimeError("Mistral API调用失败")

    async def transcribe_audio(self, audio_path: str, model: str = None,
                               language: str = None, max_retries: int = 3,
                               retry_delay_seconds: float = 3.0,
                               on_log=None) -> dict:
        request_count = max(1, int(max_retries or 1))
        retry_delay = max(0.0, float(retry_delay_seconds or 0.0))
        _emit_log(
            on_log,
            f"Starting transcription API request sequence "
            f"(max attempts: {request_count}, retry delay: {retry_delay:g}s)",
        )

        retryable_errors: list[str] = []

        for request_id in range(1, request_count + 1):
            try:
                result = await self._transcribe_audio_once(
                    audio_path,
                    request_id=request_id,
                    request_count=request_count,
                    model=model,
                    language=language,
                    on_log=on_log,
                )
            except _RetryableTranscriptionError as exc:
                retryable_errors.append(str(exc))
                _emit_log(
                    on_log,
                    f"Transcription API request {request_id}/{request_count} failed: {exc}",
                )
                if request_id < request_count and retry_delay:
                    await asyncio.sleep(retry_delay)
                continue

            if _transcription_result_is_empty(result):
                _emit_log(
                    on_log,
                    f"Transcription API request {request_id}/{request_count} returned empty transcript; using empty result",
                )
            else:
                _emit_log(
                    on_log,
                    f"Transcription API request {request_id}/{request_count} returned usable transcript; using it",
                )
            return result

        detail = "; ".join(retryable_errors) if retryable_errors else "all requests failed"
        raise RuntimeError(
            f"Transcription API request failed after {request_count} attempts: {detail}"
        )

    async def _transcribe_audio_once(self, audio_path: str, request_id: int,
                                     request_count: int, model: str = None,
                                     language: str = None, on_log=None,
                                     started_event: asyncio.Event | None = None) -> dict:
        _emit_log(
            on_log,
            f"Transcription API request {request_id}/{request_count} starting: "
            f"{Path(audio_path).name}",
        )

        headers = {"Authorization": f"Bearer {self.transcription_api_key}"}
        data = aiohttp.FormData()
        data.add_field("model", model or self.transcription_model)

        if self.transcription_provider in ("OpenAI", "Groq"):
            data.add_field("response_format", "verbose_json")
            data.add_field("timestamp_granularities[]", "word")
        elif self.transcription_provider == "SiliconFlow (硅基流动)":
            data.add_field("response_format", "verbose_json")
        elif self.transcription_provider == "自定义 (Custom)":
            current_m = (model or self.transcription_model).lower()
            if "voxtral" in current_m:
                data.add_field("timestamp_granularities", "word")
                data.add_field("diarize", "false")
            else:
                data.add_field("response_format", "verbose_json")
                data.add_field("timestamp_granularities[]", "word")
        else:
            # Mistral AI and default
            data.add_field("timestamp_granularities", "word")
            data.add_field("diarize", "false")

        if language:
            data.add_field("language", language)

        try:
            with open(audio_path, "rb") as f:
                data.add_field(
                    "file",
                    f,
                    filename=Path(audio_path).name,
                    content_type=get_content_type(audio_path),
                )
                timeout = aiohttp.ClientTimeout(total=900)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    _emit_log(
                        on_log,
                        f"Transcription API request {request_id}/{request_count} POST sent: "
                        f"{self.transcription_url}",
                    )
                    if started_event and not started_event.is_set():
                        started_event.set()
                    async with session.post(self.transcription_url, headers=headers, data=data) as resp:
                        _emit_log(
                            on_log,
                            f"Transcription API request {request_id}/{request_count} response status: {resp.status}",
                        )
                        if resp.status != 200:
                            text = await resp.text()
                            error_msg = _format_api_error(resp.status, text)
                            logger.error(error_msg)
                            logger.error(f"Transcription request URL: {self.transcription_url}")
                            if resp.status in RETRYABLE_TRANSCRIPTION_STATUS_CODES:
                                raise _RetryableTranscriptionError(error_msg)
                            raise RuntimeError(error_msg)
                        result = await resp.json()
                        if isinstance(result, dict):
                            # Mistral returns timestamp chunks in ``segments``
                            # for either requested granularity.  Preserve which
                            # one was requested so the transcription layer can
                            # group word chunks into readable subtitle cues.
                            result["_timestamp_granularity"] = "word"
                        return result
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            if started_event and not started_event.is_set():
                started_event.set()
            raise _RetryableTranscriptionError(str(exc)) from exc
        except Exception:
            if started_event and not started_event.is_set():
                started_event.set()
            raise

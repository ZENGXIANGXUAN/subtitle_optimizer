"""LLM-backed subtitle quality gates used after first-pass translation.

The functions in this module are deliberately independent from Qt workers.  They
operate on SubtitleEntry objects and a caller-owned aiohttp session so pipeline
stages can share a connection pool.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Iterable

import aiohttp

from core.client import MistralClient
from core.models import SubtitleEntry, strip_annotation
from core.segmenter import _get_source_text, _get_target_text
LANGUAGE_MAP = {
    "中文": ("zh", "中文"),
    "English": ("en", "English"),
    "意大利语 (Italiano)": ("it", "Italian"),
    "日语 (日本語)": ("ja", "Japanese"),
    "韩语 (한국어)": ("ko", "Korean"),
    "法语 (Français)": ("fr", "French"),
    "德语 (Deutsch)": ("de", "German"),
    "西班牙语 (Español)": ("es", "Spanish"),
    "葡萄牙语 (Português)": ("pt", "Portuguese"),
    "俄语 (Русский)": ("ru", "Russian"),
}


def _get_prompt_lang(value: str) -> str:
    return LANGUAGE_MAP.get(value, (value, value))[1]

try:
    from ..utils import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


def _strip_code_fences(value: str) -> str:
    clean = re.sub(r"```(?:json)?|```", "", str(value or ""), flags=re.IGNORECASE).strip()
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", clean)


def _parse_json(value: str):
    """Parse a model JSON response with conservative syntax repair.

    Semantic repair is intentionally not attempted.  We do, however, accept a
    complete JSON value followed by unsolicited prose; this is a transport-
    successful model response, not a reason to discard an entire subtitle
    batch.
    """
    clean = _strip_code_fences(value)
    attempts = [clean]
    repaired = re.sub(r",\s*([}\]])", r"\1", clean)
    if repaired != clean:
        attempts.append(repaired)

    # Some models omit only the final container character.  This is safe to
    # repair when the response otherwise ends in the matching item type.
    trimmed = repaired.rstrip()
    if trimmed.startswith("[") and trimmed.endswith("}"):
        attempts.append(trimmed + "]")
    elif trimmed.startswith("{") and trimmed.endswith("]"):
        attempts.append(trimmed + "}")

    # json.loads rejects valid JSON followed by prose (``Extra data``).  Parse
    # the first complete value instead, while still refusing malformed JSON.
    decoder = json.JSONDecoder()
    starts = [pos for pos in (clean.find("["), clean.find("{")) if pos >= 0]
    if starts:
        start = min(starts)
        try:
            data, _ = decoder.raw_decode(clean[start:])
            return data
        except json.JSONDecodeError:
            pass

    # Some providers prepend prose despite the prompt.  Extract the outermost
    # JSON object/array only when both boundaries are unambiguous.
    starts = [pos for pos in (clean.find("["), clean.find("{")) if pos >= 0]
    if starts:
        first = min(starts)
        end = max(clean.rfind("]"), clean.rfind("}"))
        if end > first:
            attempts.append(clean[first:end + 1])

    last_error: Exception | None = None
    for candidate in attempts:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
    raise ValueError(f"模型返回的JSON无法解析: {last_error}; response={clean[:500]}")


def _effective_target(entry: SubtitleEntry, input_lang_code: str) -> str:
    lines = entry.optimized_lines if entry.optimized_lines else entry.lines
    if len(lines) >= 2:
        return str(lines[0]).strip()
    return _get_target_text(entry, input_lang_code).strip()


def _clean_target(value: object) -> str:
    target = strip_annotation(str(value or "").strip()).replace("\n", " ")
    if target.lower() in {"none", "null", "nan"} or target in {"-", "—", "无"}:
        return ""
    return re.sub(r"\s+", " ", target).strip()


async def fidelity_review_batch(
    batch: list[SubtitleEntry],
    context: str,
    glossary: str,
    client: MistralClient,
    session: aiohttp.ClientSession,
    input_lang: str,
    output_lang: str,
    prev_ctx: list[SubtitleEntry] | None = None,
    next_ctx: list[SubtitleEntry] | None = None,
) -> dict[int, str]:
    """Correct only omissions/mistranslations; do not perform style polishing."""
    input_code = LANGUAGE_MAP.get(input_lang, ("en", input_lang))[0]
    input_prompt = _get_prompt_lang(input_lang)
    output_prompt = _get_prompt_lang(output_lang)

    items = [
        {
            "id": entry.index,
            "src": _get_source_text(entry, input_code),
            "tgt": _get_target_text(entry, input_code),
        }
        for entry in batch
    ]

    def context_items(values: Iterable[SubtitleEntry]) -> list[dict]:
        return [
            {
                "id": entry.index,
                "src": _get_source_text(entry, input_code),
                "tgt": _get_target_text(entry, input_code),
            }
            for entry in values
        ]

    references = []
    if prev_ctx:
        references.append("【稳定上文】\n" + json.dumps(context_items(prev_ctx), ensure_ascii=False))
    if next_ctx:
        references.append("【稳定下文】\n" + json.dumps(context_items(next_ctx), ensure_ascii=False))

    system = (
        "你是字幕忠实度审校员。你的任务不是润色，而是核对信息守恒。\n"
        f"原文语言：{input_prompt}；目标语言：{output_prompt}。\n\n"
        f"【情境分析】\n{context}\n\n"
        "逐条检查 src 与 tgt，只修复以下问题：漏译、误译、否定反转、条件丢失、"
        "因果/转折关系错误、数字/单位/人名/术语错误、指代错误。\n"
        "原译文正确时必须原样保留，不得为了文采改写，不得合并或跨条搬运信息。\n"
        "每个输入 id 必须返回一次。严格输出 JSON 数组，不要解释。\n"
        f'格式：[{{"id":1,"tgt":"核对后的{output_prompt}译文"}}]'
    )
    if glossary:
        system += "\n\n【强制术语表】\n" + glossary

    user_parts = references + ["【待核对字幕】\n" + json.dumps(items, ensure_ascii=False)]
    response = await client.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(user_parts)}],
        session,
        temperature=0.0,
    )
    data = _parse_json(response)
    if not isinstance(data, list):
        raise ValueError("忠实度审校结果不是JSON数组")

    result: dict[int, str] = {}
    valid_ids = {entry.index for entry in batch}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            entry_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if entry_id not in valid_ids:
            continue
        target = _clean_target(item.get("tgt") or item.get("zh"))
        if target:
            result[entry_id] = target

    missing = sorted(valid_ids - set(result))
    if missing:
        raise ValueError(f"忠实度审校缺少条目: {missing}")
    return result


async def _extract_consistency_candidates(
    chunk: list[SubtitleEntry],
    context: str,
    glossary: str,
    client: MistralClient,
    session: aiohttp.ClientSession,
    input_lang: str,
    output_lang: str,
) -> dict:
    input_code = LANGUAGE_MAP.get(input_lang, ("en", input_lang))[0]
    items = [
        {
            "id": entry.index,
            "src": _get_source_text(entry, input_code),
            "tgt": _effective_target(entry, input_code),
        }
        for entry in chunk
    ]
    system = (
        "你是字幕全文一致性分析员。只提取需要跨条统一的内容，不修改字幕。\n"
        "识别同一术语、专名、缩写、按钮名、产品名或固定表达是否出现多种译法；"
        "选择最符合强制术语表和上下文的一种标准译法。\n"
        "不要收集普通词，不要虚构冲突。严格输出JSON对象。\n"
        '格式：{"terms":[{"source":"原词","target":"标准译法","variants":["其他译法"],'
        '"type":"term|name|ui|product|abbreviation","reason":"简短原因"}],'
        '"style_rules":["确实需要全篇统一的简短规则"]}'
        f"\n\n【情境分析】\n{context}"
    )
    if glossary:
        system += "\n\n【强制术语表，优先级最高】\n" + glossary
    response = await client.chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": json.dumps(items, ensure_ascii=False)}],
        session,
        temperature=0.0,
    )
    data = _parse_json(response)
    if not isinstance(data, dict):
        raise ValueError("一致性候选结果不是JSON对象")
    return data


async def build_consistency_guide(
    entries: list[SubtitleEntry],
    context: str,
    glossary: str,
    client: MistralClient,
    session: aiohttp.ClientSession,
    input_lang: str,
    output_lang: str,
    *,
    chunk_size: int = 80,
    concurrency: int = 4,
) -> str:
    """Build a compact whole-file consistency guide using map/reduce calls."""
    if not entries:
        return glossary.strip()

    chunks = [entries[i:i + max(10, chunk_size)] for i in range(0, len(entries), max(10, chunk_size))]
    sem = asyncio.Semaphore(max(1, concurrency))

    async def run_chunk(chunk: list[SubtitleEntry]):
        async with sem:
            return await _extract_consistency_candidates(
                chunk, context, glossary, client, session, input_lang, output_lang
            )

    raw_results = await asyncio.gather(*(run_chunk(chunk) for chunk in chunks), return_exceptions=True)
    terms: list[dict] = []
    style_rules: list[str] = []
    seen_term_keys: set[tuple[str, str]] = set()
    for result in raw_results:
        if isinstance(result, Exception):
            logger.warning("一致性候选提取失败: %s", result)
            continue
        for item in result.get("terms") or []:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "").strip()
            target = str(item.get("target") or "").strip()
            if not source or not target:
                continue
            key = (source.casefold(), target.casefold())
            if key in seen_term_keys:
                continue
            seen_term_keys.add(key)
            terms.append(item)
        for rule in result.get("style_rules") or []:
            rule = str(rule or "").strip()
            if rule and rule not in style_rules:
                style_rules.append(rule)

    if not terms and not style_rules:
        return glossary.strip()

    candidate_payload = {"terms": terms, "style_rules": style_rules}
    consolidate_system = (
        "你是字幕一致性规则整合器。合并重复候选，解决冲突，并删除没有证据或过于宽泛的规则。\n"
        "强制术语表优先级最高。同一 source 最终只能保留一个 target。\n"
        "严格输出JSON对象，不要解释。\n"
        '格式：{"terms":[{"source":"原词","target":"统一译法","type":"类型"}],'
        '"style_rules":["简短规则"]}'
    )
    if glossary:
        consolidate_system += "\n\n【强制术语表】\n" + glossary
    response = await client.chat(
        [{"role": "system", "content": consolidate_system},
         {"role": "user", "content": json.dumps(candidate_payload, ensure_ascii=False)}],
        session,
        temperature=0.0,
    )
    final_data = _parse_json(response)
    if not isinstance(final_data, dict):
        raise ValueError("一致性规则整合结果不是JSON对象")

    lines: list[str] = []
    if glossary.strip():
        lines.append("【强制术语表】\n" + glossary.strip())
    final_terms = final_data.get("terms") or []
    if final_terms:
        lines.append("【全文统一译法】")
        for item in final_terms:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "").strip()
            target = str(item.get("target") or "").strip()
            kind = str(item.get("type") or "term").strip()
            if source and target:
                lines.append(f"- {source} => {target} ({kind})")
    rules = [str(rule).strip() for rule in (final_data.get("style_rules") or []) if str(rule).strip()]
    if rules:
        lines.append("【全文一致性规则】\n- " + "\n- ".join(rules))
    return "\n".join(lines).strip()


async def consistency_review_batch(
    batch: list[SubtitleEntry],
    consistency_guide: str,
    context: str,
    client: MistralClient,
    session: aiohttp.ClientSession,
    input_lang: str,
    output_lang: str,
    prev_ctx: list[SubtitleEntry] | None = None,
    next_ctx: list[SubtitleEntry] | None = None,
) -> dict[int, str]:
    """Apply only documented whole-file consistency corrections."""
    input_code = LANGUAGE_MAP.get(input_lang, ("en", input_lang))[0]
    output_prompt = _get_prompt_lang(output_lang)

    def item(entry: SubtitleEntry) -> dict:
        return {
            "id": entry.index,
            "src": _get_source_text(entry, input_code),
            "tgt": _effective_target(entry, input_code),
        }

    payload = [item(entry) for entry in batch]
    references = []
    if prev_ctx:
        references.append("【上文】\n" + json.dumps([item(entry) for entry in prev_ctx], ensure_ascii=False))
    if next_ctx:
        references.append("【下文】\n" + json.dumps([item(entry) for entry in next_ctx], ensure_ascii=False))

    system = (
        "你是字幕全文一致性执行审校员。只根据给定的一致性指南修复术语、专名、缩写、"
        "按钮名、产品名和明确的全篇风格冲突。\n"
        "没有冲突时必须原样返回 tgt；不得借机重新润色，不得改变信息，不得跨条搬运。\n"
        "每个输入id必须返回一次。严格输出JSON数组，不要解释。\n"
        f'格式：[{{"id":1,"tgt":"最终{output_prompt}字幕"}}]'
        f"\n\n【情境分析】\n{context}\n\n【一致性指南】\n{consistency_guide}"
    )
    user = "\n\n".join(references + ["【待检查字幕】\n" + json.dumps(payload, ensure_ascii=False)])
    response = await client.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        session,
        temperature=0.0,
    )
    data = _parse_json(response)
    if not isinstance(data, list):
        raise ValueError("一致性执行结果不是JSON数组")

    valid_ids = {entry.index for entry in batch}
    result: dict[int, str] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            entry_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        if entry_id not in valid_ids:
            continue
        target = _clean_target(row.get("tgt") or row.get("zh"))
        if target:
            result[entry_id] = target

    missing = sorted(valid_ids - set(result))
    if missing:
        raise ValueError(f"一致性执行缺少条目: {missing}")
    return result

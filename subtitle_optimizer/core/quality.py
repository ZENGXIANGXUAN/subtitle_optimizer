"""LLM-backed subtitle quality gates used after first-pass translation.

The functions in this module are deliberately independent from Qt workers.  They
operate on SubtitleEntry objects and a caller-owned aiohttp session so pipeline
stages can share a connection pool.
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from typing import Iterable

import aiohttp

from core.client import MistralClient
from core.models import SubtitleEntry, _srt_time_to_ms, strip_annotation
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


# Comfortable subtitle reading rates, used only to tell a reviewer how much
# room a cue still has. srt_lint's READING_WARNING_CPS is a much looser
# *warning* threshold and is deliberately not reused here.
_COMFORTABLE_CHARS_PER_SECOND = {"zh": 9.0, "ja": 9.0, "ko": 9.0}
_DEFAULT_CHARS_PER_SECOND = 17.0


def _cue_duration_seconds(entry: SubtitleEntry) -> float:
    return max(
        0.0,
        (_srt_time_to_ms(entry.end_time) - _srt_time_to_ms(entry.start_time)) / 1000.0,
    )


def _target_char_budget(entry: SubtitleEntry, output_lang_code: str) -> int:
    rate = _COMFORTABLE_CHARS_PER_SECOND.get(
        (output_lang_code or "").lower(), _DEFAULT_CHARS_PER_SECOND
    )
    return int(_cue_duration_seconds(entry) * rate)


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
    output_code = LANGUAGE_MAP.get(output_lang, ("zh", output_lang))[0]
    input_prompt = _get_prompt_lang(input_lang)
    output_prompt = _get_prompt_lang(output_lang)

    # ``sec``/``budget`` tell the reviewer how much screen time this cue has.
    # Without them it cannot tell an over-compressed cue from a correctly terse
    # one: a 9.5s cue carrying 18 characters dropped a whole "or ..." clause and
    # still looked fine to the model, because nothing said there was room.
    items = [
        {
            "id": entry.index,
            "src": _get_source_text(entry, input_code),
            "tgt": _get_target_text(entry, input_code),
            "sec": round(_cue_duration_seconds(entry), 1),
            "budget": _target_char_budget(entry, output_code),
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
        "原译文正确时必须原样保留，不得为了文采改写，不得合并或跨条搬运信息。\n\n"

        "【核对方法】不要凭整体印象判断。对每条先在心里列出 src 的信息点清单：\n"
        "主语、动作、对象、条件、原因、结果、数字与单位、以及所有限定语\n"
        "（or …／unless …／except …／including …／such as … 这类附加成分）。\n"
        "再逐项确认 tgt 是否都有对应，缺任何一项都必须补上。\n"
        "并列结构中的第二个分支（“A or B”里的 B、“X and Y”里的 Y）最容易被整段丢掉，"
        "务必单独确认。译文读起来通顺不等于信息完整。\n\n"

        "【篇幅判断】每条给出了 sec（该条画面时长，秒）和 budget（该时长下目标语言"
        "可容纳的大致字数上限）。\n"
        "  - 当前 tgt 明显短于 budget → 说明有余量，此时优先把漏掉的信息补全，不要为了简短而省略\n"
        "  - 当前 tgt 已接近或超过 budget → 保持精简，只修正错误，不要再扩写\n"
        "budget 是参考上限而非目标，不得为了凑字数添加原文没有的内容。\n\n"

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


CONSISTENCY_REQUEST_BYTE_BUDGET = 4096
CONSISTENCY_MAX_TERMS_PER_CALL = 12
CONSISTENCY_MAX_VARIANTS_PER_TERM = 3
CONSISTENCY_MAX_STYLE_RULES_PER_CALL = 5
CONSISTENCY_MAX_STYLE_RULES_TOTAL = 20
CONSISTENCY_MAX_RESPONSE_TOKENS = 768
CONSISTENCY_SUBREQUEST_ATTEMPTS = 2
# client.chat() already retries transport/5xx errors.  This outer attempt exists
# for response-shape failures, so it must not re-issue immediately: an instant
# retry against a rate-limited gateway turns one 429 into two.
CONSISTENCY_SUBREQUEST_RETRY_DELAY_SECONDS = 2.0


def _json_bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _request_bytes(messages: list[dict], extra_payload: dict) -> int:
    """Estimate the JSON body sent to the chat endpoint without logging text."""
    return _json_bytes({"messages": messages, "temperature": 0.0, **extra_payload})


def _normalise_consistency_key(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def _unique_strings(values: Iterable[object], *, limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = _normalise_consistency_key(text)
        if not text or not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _consistency_extra_payload() -> dict:
    return {
        "response_format": {"type": "json_object"},
        "max_tokens": CONSISTENCY_MAX_RESPONSE_TOKENS,
    }


def _candidate_system(context: str, glossary: str) -> str:
    system = (
        "你是字幕全文一致性分析员。只提取需要跨条统一的内容，不修改字幕。\n"
        "识别同一术语、专名、缩写、按钮名、产品名或固定表达是否出现多种译法；"
        "选择最符合强制术语表和上下文的一种标准译法。\n"
        f"最多返回 {CONSISTENCY_MAX_TERMS_PER_CALL} 个术语；每个术语最多 "
        f"{CONSISTENCY_MAX_VARIANTS_PER_TERM} 个变体；最多返回 "
        f"{CONSISTENCY_MAX_STYLE_RULES_PER_CALL} 条风格规则。\n"
        "不要收集普通词，不要虚构冲突。严格输出JSON对象，不要解释。\n"
        '格式：{"terms":[{"source":"原词","target":"标准译法","variants":["其他译法"],'
        '"type":"term|name|ui|product|abbreviation","reason":"简短原因"}],'
        '"style_rules":["确实需要全篇统一的简短规则"]}'
        f"\n\n【情境分析】\n{context}"
    )
    if glossary:
        system += "\n\n【强制术语表，优先级最高】\n" + glossary
    return system


def _candidate_messages(items: list[dict], context: str, glossary: str) -> list[dict]:
    return [
        {"role": "system", "content": _candidate_system(context, glossary)},
        {"role": "user", "content": json.dumps(items, ensure_ascii=False, separators=(",", ":"))},
    ]


def _consolidate_system(glossary: str) -> str:
    system = (
        "你是字幕一致性规则整合器。输入中的同一 source 已在本地归并为一条证据，"
        "请只在这一组中选择一个标准 target，并删除没有证据或过于宽泛的规则。\n"
        "强制术语表优先级最高。每个输入 source 最终最多返回一次。\n"
        f"最多返回 {CONSISTENCY_MAX_TERMS_PER_CALL} 个术语和 "
        f"{CONSISTENCY_MAX_STYLE_RULES_PER_CALL} 条规则。严格输出JSON对象，不要解释。\n"
        '格式：{"terms":[{"source":"原词","target":"统一译法","type":"类型"}],'
        '"style_rules":["简短规则"]}'
    )
    if glossary:
        system += "\n\n【强制术语表】\n" + glossary
    return system


def _consolidate_messages(payload: dict, glossary: str) -> list[dict]:
    return [
        {"role": "system", "content": _consolidate_system(glossary)},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def _entry_candidate_item(entry: SubtitleEntry, input_code: str) -> dict:
    return {
        "id": entry.index,
        "src": _get_source_text(entry, input_code),
        "tgt": _effective_target(entry, input_code),
    }


def _pack_entry_candidate_chunks(
    entries: list[SubtitleEntry],
    *,
    input_code: str,
    context: str,
    glossary: str,
    max_items: int,
    byte_budget: int = CONSISTENCY_REQUEST_BYTE_BUDGET,
) -> list[list[dict]]:
    """Pack map inputs by actual UTF-8 request size, never only by cue count."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for entry in entries:
        item = _entry_candidate_item(entry, input_code)
        proposed = current + [item]
        proposed_bytes = _request_bytes(
            _candidate_messages(proposed, context, glossary),
            _consistency_extra_payload(),
        )
        if current and (len(proposed) > max_items or proposed_bytes > byte_budget):
            chunks.append(current)
            current = [item]
        else:
            current = proposed
    if current:
        chunks.append(current)
    return chunks


def _sanitize_candidate_result(data: dict) -> tuple[list[dict], list[str]]:
    terms: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in data.get("terms") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()
        target = str(item.get("target") or "").strip()
        key = (_normalise_consistency_key(source), _normalise_consistency_key(target))
        if not all(key) or key in seen:
            continue
        seen.add(key)
        terms.append(
            {
                "source": source,
                "target": target,
                "variants": _unique_strings(
                    item.get("variants") or [],
                    limit=CONSISTENCY_MAX_VARIANTS_PER_TERM,
                ),
                "type": str(item.get("type") or "term").strip()[:48] or "term",
                "reason": str(item.get("reason") or "").strip()[:160],
            }
        )
        if len(terms) >= CONSISTENCY_MAX_TERMS_PER_CALL:
            break
    rules = _unique_strings(
        data.get("style_rules") or [],
        limit=CONSISTENCY_MAX_STYLE_RULES_PER_CALL,
    )
    return terms, rules


def _merge_candidate_evidence(candidates: Iterable[dict]) -> list[dict]:
    """Keep all translations for one source together before any LLM reducer."""
    grouped: dict[str, dict] = {}
    for candidate in candidates:
        source = str(candidate.get("source") or "").strip()
        target = str(candidate.get("target") or "").strip()
        source_key = _normalise_consistency_key(source)
        if not source_key or not target:
            continue
        record = grouped.setdefault(
            source_key,
            {
                "source": source,
                "target": target,
                "variants": [],
                "type": str(candidate.get("type") or "term").strip() or "term",
                "evidence_count": 0,
            },
        )
        values = [record["target"], target, *(candidate.get("variants") or [])]
        unique = _unique_strings(values, limit=1 + CONSISTENCY_MAX_VARIANTS_PER_TERM)
        record["target"] = unique[0]
        record["variants"] = unique[1:]
        record["evidence_count"] += 1
    return list(grouped.values())


def _pack_reducer_payloads(
    records: list[dict],
    style_rules: list[str],
    *,
    glossary: str,
    byte_budget: int = CONSISTENCY_REQUEST_BYTE_BUDGET,
) -> list[dict]:
    """Build independent bounded reducer calls; there is no global reducer."""
    payloads: list[dict] = []
    current: list[dict] = []
    for record in records:
        proposed = current + [record]
        payload = {"terms": proposed, "style_rules": []}
        proposed_bytes = _request_bytes(
            _consolidate_messages(payload, glossary), _consistency_extra_payload()
        )
        if current and (len(proposed) > CONSISTENCY_MAX_TERMS_PER_CALL or proposed_bytes > byte_budget):
            payloads.append({"terms": current, "style_rules": []})
            current = [record]
        else:
            current = proposed
    if current:
        payloads.append({"terms": current, "style_rules": []})

    for rule in style_rules:
        payload = {"terms": [], "style_rules": [rule]}
        payloads.append(payload)
    return payloads


async def _request_consistency_json(
    *,
    stage: str,
    messages: list[dict],
    item_count: int,
    client: MistralClient,
    session: aiohttp.ClientSession,
) -> dict:
    extra_payload = _consistency_extra_payload()
    request_bytes = _request_bytes(messages, extra_payload)
    last_error: Exception | None = None
    for attempt in range(1, CONSISTENCY_SUBREQUEST_ATTEMPTS + 1):
        logger.info(
            "Consistency %s request: bytes=%s, items=%s, attempt=%s/%s",
            stage,
            request_bytes,
            item_count,
            attempt,
            CONSISTENCY_SUBREQUEST_ATTEMPTS,
        )
        try:
            response = await client.chat(
                messages,
                session,
                extra_payload=extra_payload,
                temperature=0.0,
            )
            data = _parse_json(response)
            if not isinstance(data, dict):
                raise ValueError(f"{stage} response is not a JSON object")
            return data
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Consistency %s request failed: bytes=%s, items=%s, attempt=%s/%s, error=%s",
                stage,
                request_bytes,
                item_count,
                attempt,
                CONSISTENCY_SUBREQUEST_ATTEMPTS,
                exc,
            )
            if attempt < CONSISTENCY_SUBREQUEST_ATTEMPTS:
                await asyncio.sleep(CONSISTENCY_SUBREQUEST_RETRY_DELAY_SECONDS)
    raise RuntimeError(
        f"consistency {stage} request failed after "
        f"{CONSISTENCY_SUBREQUEST_ATTEMPTS} attempts: {last_error}"
    )


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
    retry_state: dict | None = None,
) -> str:
    """Build consistency rules with bounded map/reduce requests.

    Account scheduling/concurrency remains caller-owned.  This function limits
    the shape of each request and keeps successful map/reduce results in
    ``retry_state`` so a guide-level retry only repeats failed small requests.
    """
    del output_lang  # Kept for the stable public API.
    if not entries:
        return glossary.strip()

    state = retry_state if retry_state is not None else {}
    input_code = LANGUAGE_MAP.get(input_lang, ("en", input_lang))[0]
    max_items = max(1, int(chunk_size or 1))
    chunks = _pack_entry_candidate_chunks(
        entries,
        input_code=input_code,
        context=context,
        glossary=glossary,
        max_items=max_items,
    )
    map_cache: dict[str, dict] = state.setdefault("consistency_map_cache", {})
    sem = asyncio.Semaphore(max(1, concurrency))

    async def run_map(cache_key: str, items: list[dict]) -> tuple[str, dict]:
        async with sem:
            data = await _request_consistency_json(
                stage="map",
                messages=_candidate_messages(items, context, glossary),
                item_count=len(items),
                client=client,
                session=session,
            )
            return cache_key, data

    missing_maps: list[tuple[str, list[dict]]] = []
    for items in chunks:
        cache_key = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
        if cache_key not in map_cache:
            missing_maps.append((cache_key, items))
    if missing_maps:
        map_results = await asyncio.gather(
            *(run_map(cache_key, items) for cache_key, items in missing_maps),
            return_exceptions=True,
        )
        map_failures: list[Exception] = []
        for result in map_results:
            if isinstance(result, Exception):
                logger.warning("Consistency candidate extraction failed: %s", result)
                map_failures.append(result)
                continue
            cache_key, data = result
            map_cache[cache_key] = data
        if map_failures:
            raise RuntimeError(
                f"{len(map_failures)} bounded consistency map requests failed"
            )

    if not map_cache:
        raise RuntimeError("all bounded consistency map requests failed")

    candidates: list[dict] = []
    style_rules: list[str] = []
    for data in map_cache.values():
        terms, rules = _sanitize_candidate_result(data)
        candidates.extend(terms)
        style_rules = _unique_strings(
            [*style_rules, *rules], limit=CONSISTENCY_MAX_STYLE_RULES_TOTAL
        )
    records = _merge_candidate_evidence(candidates)
    if not records and not style_rules:
        return glossary.strip()

    reducer_payloads = _pack_reducer_payloads(records, style_rules, glossary=glossary)
    reducer_cache: dict[str, dict] = state.setdefault("consistency_reducer_cache", {})

    async def run_reducer(cache_key: str, payload: dict) -> tuple[str, dict]:
        async with sem:
            data = await _request_consistency_json(
                stage="reduce",
                messages=_consolidate_messages(payload, glossary),
                item_count=len(payload.get("terms") or []) + len(payload.get("style_rules") or []),
                client=client,
                session=session,
            )
            return cache_key, data

    missing_reducers: list[tuple[str, dict]] = []
    for payload in reducer_payloads:
        cache_key = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if cache_key not in reducer_cache:
            missing_reducers.append((cache_key, payload))
    if missing_reducers:
        reducer_results = await asyncio.gather(
            *(run_reducer(cache_key, payload) for cache_key, payload in missing_reducers),
            return_exceptions=True,
        )
        reducer_failures: list[Exception] = []
        for result in reducer_results:
            if isinstance(result, Exception):
                logger.warning("Consistency bounded reducer failed: %s", result)
                reducer_failures.append(result)
                continue
            cache_key, data = result
            reducer_cache[cache_key] = data
        if reducer_failures:
            raise RuntimeError(
                f"{len(reducer_failures)} bounded consistency reducer requests failed"
            )

    if not reducer_cache:
        raise RuntimeError("all bounded consistency reducer requests failed")

    final_terms: list[dict] = []
    final_rules: list[str] = []
    seen_sources: set[str] = set()
    for data in reducer_cache.values():
        terms, rules = _sanitize_candidate_result(data)
        for item in terms:
            source_key = _normalise_consistency_key(item["source"])
            if source_key and source_key not in seen_sources:
                seen_sources.add(source_key)
                final_terms.append(item)
        final_rules = _unique_strings(
            [*final_rules, *rules], limit=CONSISTENCY_MAX_STYLE_RULES_TOTAL
        )

    lines: list[str] = []
    if glossary.strip():
        lines.append("【强制术语表】\n" + glossary.strip())
    if final_terms:
        lines.append("【全文统一译法】")
        for item in final_terms:
            lines.append(f"- {item['source']} => {item['target']} ({item['type']})")
    if final_rules:
        lines.append("【全文一致性规则】\n- " + "\n- ".join(final_rules))
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

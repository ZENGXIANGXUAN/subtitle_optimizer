from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Iterable, Sequence


COMMON_LATIN_GLOSSARY_SOURCES = {
    "a",
    "about",
    "after",
    "again",
    "all",
    "also",
    "always",
    "am",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "back",
    "be",
    "because",
    "been",
    "before",
    "being",
    "but",
    "by",
    "can",
    "come",
    "could",
    "do",
    "does",
    "doing",
    "done",
    "down",
    "each",
    "entire",
    "even",
    "every",
    "feel",
    "few",
    "fill",
    "for",
    "from",
    "get",
    "go",
    "going",
    "good",
    "had",
    "has",
    "have",
    "he",
    "her",
    "here",
    "him",
    "his",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "just",
    "keep",
    "know",
    "like",
    "little",
    "look",
    "make",
    "many",
    "me",
    "more",
    "most",
    "move",
    "much",
    "need",
    "new",
    "no",
    "not",
    "now",
    "of",
    "off",
    "on",
    "one",
    "or",
    "order",
    "other",
    "our",
    "out",
    "over",
    "part",
    "put",
    "really",
    "right",
    "same",
    "say",
    "see",
    "show",
    "so",
    "some",
    "something",
    "still",
    "take",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "thing",
    "this",
    "those",
    "through",
    "to",
    "up",
    "use",
    "very",
    "want",
    "way",
    "we",
    "well",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "will",
    "with",
    "would",
    "you",
    "your",
}

CONTEXT_ONLY_HUMAN_REVIEW_SOURCE_KEYS = {
    "badhighorlow",
    "breakoutpoint",
    "failedbreak",
    "flush",
    "getthehangof",
    "lot",
    "lots",
    "prepitselfforamove",
    "pricediscoverymode",
    "responsivetrade",
    "responsivetypepriceaction",
    "rolls",
    "rotation",
    "rotationalbased",
    "shallowpullbacks",
    "snapinvolume",
    "stopouts",
    "stopplacement",
    "strategysession",
    "timeframeparticipants",
    "topofthehour",
    "trendingconsolidation",
    "twowaytrade",
}

GENERIC_NON_REVIEW_STATUS_KEYS = {
    "new",
    "newortranslationconflict",
    "\u65b0\u8bcd",
    "\u65b0\u8bcd\u6216\u8bd1\u6cd5\u51b2\u7a81",
}

HUMAN_REVIEW_STATUS_MARKERS = (
    "ambiguous",
    "conflict",
    "lowconfidence",
    "mismatch",
    "needconfirm",
    "needreview",
    "suspicious",
    "uncertain",
    "wrong",
    "\u4e0d\u7b26\u5408",
    "\u4f4e\u7f6e\u4fe1",
    "\u51b2\u7a81",
    "\u53ef\u7591",
    "\u5f02\u5e38",
    "\u5f85\u786e\u8ba4",
    "\u6b67\u4e49",
    "\u8bef\u8bd1",
    "\u8bed\u5883",
    "\u8bd1\u6cd5\u51b2\u7a81",
    "\u9700\u8981\u786e\u8ba4",
    "\u9519\u8bef",
)


@dataclass(frozen=True)
class GlossaryCandidate:
    source: str
    target: str
    files: tuple[str, ...]
    status: str


@dataclass(frozen=True)
class GlossaryFileFailure:
    filename: str
    reason: str


@dataclass(frozen=True)
class BatchGlossarySummary:
    candidates: list[GlossaryCandidate]
    skipped_existing: list[str]
    failures: list[GlossaryFileFailure]
    session_glossary: str = ""
    fixed_glossary: str = ""


def parse_glossary_pairs(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if "->" in line:
            source, target = line.split("->", 1)
        elif "→" in line:
            source, target = line.split("→", 1)
        else:
            continue

        source = source.strip()
        target = target.strip()
        if source and target:
            pairs.append((source, target))

    return pairs


def fixed_source_keys(fixed_glossary: str) -> set[str]:
    return {normalize_glossary_source_key(source) for source, _target in parse_glossary_pairs(fixed_glossary)}


def relevant_fixed_glossary(fixed_glossary: str, sources: Iterable[str]) -> str:
    """Return only fixed-glossary entries whose source appears in this AI batch."""
    relevant_keys = {
        normalize_glossary_source_key(source)
        for source in sources
        if str(source).strip()
    }
    if not relevant_keys:
        return ""

    return "\n".join(
        f"{source}->{target}"
        for source, target in parse_glossary_pairs(fixed_glossary)
        if normalize_glossary_source_key(source) in relevant_keys
    )


def batch_glossary_reasoning_options() -> dict[str, str]:
    # mistral-medium-3-5 only supports high/none.  Sending xhigh merely makes
    # the local proxy normalize the request, so use the effective value here.
    return {"reasoning_effort": "high"}


def batch_glossary_review_reasoning_options() -> dict[str, str]:
    # The review is a bounded JSON cleanup pass. High reasoning adds thousands
    # of hidden completion tokens and pushes otherwise small requests into the
    # local proxy's long-latency failure path without improving the schema task.
    return {"reasoning_effort": "none"}


def batch_glossary_unifier_candidate_limit() -> int:
    # Large structured glossary requests have a long latency tail even when
    # their token count is modest.  Smaller batches keep each request inside
    # the proxy's bounded request budget and are merged deterministically.
    return 40


def batch_glossary_unifier_timeout_seconds() -> int:
    return 300


def normalize_glossary_source_key(source: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", source.casefold())


def normalize_glossary_target_key(target: str) -> str:
    text = target.casefold().replace("（", "").replace("）", "").replace("(", "").replace(")", "")
    cjk = re.sub(r"[^\u4e00-\u9fff]+", "", text)
    if cjk:
        return cjk
    return re.sub(r"[^0-9a-z]+", "", text)


def _target_keys_compatible(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return left == right or left in right or right in left


def _source_display_score(source: str) -> tuple[int, int, int]:
    letters = re.findall(r"[A-Za-z]", source)
    compact_len = len(re.sub(r"\s+", "", source))
    if letters and all(letter.isupper() for letter in letters) and len(letters) > 1:
        return (4, compact_len, -len(source))
    if re.search(r"[a-z][A-Z]", source):
        return (3, compact_len, -len(source))
    if any(token[:1].isupper() for token in re.findall(r"[A-Za-z]+", source)):
        return (2, compact_len, -len(source))
    return (1, compact_len, -len(source))


def _better_source_display(current: str, candidate: str) -> str:
    return candidate if _source_display_score(candidate) > _source_display_score(current) else current


def _target_display_score(target: str) -> tuple[int, int]:
    cjk_len = len(re.findall(r"[\u4e00-\u9fff]", target))
    return (cjk_len, len(target))


def _better_target_display(current: str, candidate: str) -> str:
    return candidate if _target_display_score(candidate) > _target_display_score(current) else current


def is_low_value_glossary_source(source: str) -> bool:
    source = source.strip()
    if not source:
        return True
    if not source.isascii():
        return False

    tokens = re.findall(r"[A-Za-z]+", source)
    if not tokens:
        return False

    has_acronym = any(token.isupper() and len(token) > 1 for token in tokens)
    has_titlecase_concept = len(tokens) == 1 and tokens[0][:1].isupper() and len(tokens[0]) > 2
    if has_acronym or has_titlecase_concept:
        return False

    lowered = [token.casefold() for token in tokens]
    if len(lowered) == 1:
        token = lowered[0]
        return token in COMMON_LATIN_GLOSSARY_SOURCES

    return all(token in COMMON_LATIN_GLOSSARY_SOURCES for token in lowered)


def _normalize_status_key(status: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", status.casefold())


def _status_needs_human_review(status: str) -> bool:
    status_key = _normalize_status_key(status)
    if not status_key or status_key in GENERIC_NON_REVIEW_STATUS_KEYS:
        return False
    return any(marker in status_key for marker in HUMAN_REVIEW_STATUS_MARKERS)


def filter_human_review_glossary_candidates(
    candidates: Sequence[GlossaryCandidate],
) -> list[GlossaryCandidate]:
    targets_by_source: dict[str, set[str]] = {}
    for candidate in candidates:
        source_key = normalize_glossary_source_key(candidate.source)
        target_key = normalize_glossary_target_key(candidate.target)
        if source_key and target_key:
            targets_by_source.setdefault(source_key, set()).add(target_key)

    review_candidates: list[GlossaryCandidate] = []
    seen_pairs: set[tuple[str, str]] = set()
    for candidate in candidates:
        source = candidate.source.strip()
        target = candidate.target.strip()
        source_key = normalize_glossary_source_key(source)
        target_key = normalize_glossary_target_key(target)
        if not source or not target or not source_key or not target_key:
            continue
        if is_low_value_glossary_source(source):
            continue
        if source_key in CONTEXT_ONLY_HUMAN_REVIEW_SOURCE_KEYS:
            continue

        has_real_conflict = len(targets_by_source.get(source_key, set())) > 1
        if not has_real_conflict and not _status_needs_human_review(candidate.status):
            continue

        pair_key = (source_key, target_key)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        review_candidates.append(candidate)

    return review_candidates


def _glossary_items_to_lines(
    items: Sequence[object],
    *,
    limit: int,
    existing_keys: set[str] | None = None,
) -> list[str]:
    existing_keys = existing_keys or set()
    lines: list[str] = []
    seen_sources: set[str] = set()
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source", "")).strip()
        target = str(item.get("target", "")).strip()
        source_key = normalize_glossary_source_key(source)
        if (
            not source
            or not target
            or not source_key
            or source_key in seen_sources
            or source_key in existing_keys
            or is_low_value_glossary_source(source)
        ):
            continue
        seen_sources.add(source_key)
        lines.append(f"{source}->{target}")
    return lines


def parse_batch_glossary_ai_review_result(
    raw: str,
    original_summary: BatchGlossarySummary,
    *,
    fixed_glossary: str,
) -> BatchGlossarySummary:
    clean = re.sub(r'```(?:json)?|```', '', raw or '').strip()
    data = json.loads(clean)
    if not isinstance(data, dict):
        raise ValueError("AI 术语自审返回格式无效")

    session_items = data.get("session_glossary")
    fixed_items = data.get("fixed_candidates")
    if not isinstance(session_items, list):
        session_items = []
    if not isinstance(fixed_items, list):
        fixed_items = []

    session_lines = _glossary_items_to_lines(session_items, limit=80)
    fixed_lines = _glossary_items_to_lines(
        fixed_items,
        limit=20,
        existing_keys=fixed_source_keys(fixed_glossary),
    )

    return BatchGlossarySummary(
        candidates=[],
        skipped_existing=original_summary.skipped_existing,
        failures=original_summary.failures,
        session_glossary="\n".join(session_lines) if session_items else original_summary.session_glossary,
        fixed_glossary="\n".join(fixed_lines),
    )


def merge_batch_glossary_candidates(
    file_glossaries: Iterable[tuple[str, str]],
    *,
    fixed_glossary: str,
    failures: Iterable[GlossaryFileFailure] | None = None,
) -> BatchGlossarySummary:
    fixed_keys = fixed_source_keys(fixed_glossary)
    skipped_by_key: dict[str, str] = {}
    merged: dict[tuple[str, str], dict[str, object]] = {}
    source_order: dict[str, int] = {}
    source_targets: dict[str, set[str]] = {}
    next_source_index = 0

    for filename, glossary_text in file_glossaries:
        for source, target in parse_glossary_pairs(glossary_text):
            source_key = normalize_glossary_source_key(source)
            if is_low_value_glossary_source(source):
                continue
            if source_key in fixed_keys:
                skipped_by_key.setdefault(source_key, source)
                continue

            if source_key not in source_order:
                source_order[source_key] = next_source_index
                next_source_index += 1

            target_key = normalize_glossary_target_key(target)
            pair_key = (source_key, target_key)
            for existing_key, row in merged.items():
                if row["source_key"] == source_key and _target_keys_compatible(
                    str(existing_key[1]),
                    target_key,
                ):
                    pair_key = existing_key
                    break
            if pair_key not in merged:
                merged[pair_key] = {
                    "source": source,
                    "target": target,
                    "source_key": source_key,
                    "files": [],
                    "index": len(merged),
                }
            else:
                merged[pair_key]["source"] = _better_source_display(str(merged[pair_key]["source"]), source)
                merged[pair_key]["target"] = _better_target_display(str(merged[pair_key]["target"]), target)

            files = merged[pair_key]["files"]
            if isinstance(files, list) and filename not in files:
                files.append(filename)

    for source_key, target_key in merged:
        source_targets.setdefault(source_key, set()).add(target_key)

    conflict_keys = {
        source_key
        for source_key, targets in source_targets.items()
        if len(targets) > 1
    }

    candidates = [
        GlossaryCandidate(
            source=str(row["source"]),
            target=str(row["target"]),
            files=tuple(row["files"]) if isinstance(row["files"], list) else (),
            status="译法冲突" if row["source_key"] in conflict_keys else "新词",
        )
        for _pair_key, row in sorted(
            merged.items(),
            key=lambda item: (
                0 if item[1]["source_key"] in conflict_keys else 1,
                source_order[str(item[1]["source_key"])],
                int(item[1]["index"]),
            ),
        )
    ]

    return BatchGlossarySummary(
        candidates=candidates,
        skipped_existing=list(skipped_by_key.values()),
        failures=list(failures or []),
        session_glossary="\n".join(
            f"{candidate.source}->{candidate.target}"
            for candidate in candidates
            if candidate.source.strip() and candidate.target.strip()
        ),
    )


def unresolved_conflict_sources(candidates: Sequence[GlossaryCandidate]) -> list[str]:
    targets_by_source: dict[str, set[str]] = {}
    source_by_key: dict[str, str] = {}
    for candidate in candidates:
        source = candidate.source.strip()
        target = candidate.target.strip()
        if not source or not target:
            continue

        source_key = normalize_glossary_source_key(source)
        source_by_key.setdefault(source_key, source)
        targets_by_source.setdefault(source_key, set()).add(normalize_glossary_target_key(target))

    return [
        source_by_key[source_key]
        for source_key, targets in targets_by_source.items()
        if len(targets) > 1
    ]


def approved_candidates_to_text(candidates: Sequence[GlossaryCandidate]) -> str:
    lines: list[str] = []
    seen_sources: set[str] = set()
    for candidate in candidates:
        source = candidate.source.strip()
        target = candidate.target.strip()
        if not source or not target:
            continue

        source_key = normalize_glossary_source_key(source)
        if source_key in seen_sources:
            continue

        seen_sources.add(source_key)
        lines.append(f"{source}->{target}")

    return "\n".join(lines)

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


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
    return {source.casefold() for source, _target in parse_glossary_pairs(fixed_glossary)}


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
            source_key = source.casefold()
            if source_key in fixed_keys:
                skipped_by_key.setdefault(source_key, source)
                continue

            if source_key not in source_order:
                source_order[source_key] = next_source_index
                next_source_index += 1

            source_targets.setdefault(source_key, set()).add(target)
            pair_key = (source, target)
            if pair_key not in merged:
                merged[pair_key] = {
                    "source": source,
                    "target": target,
                    "source_key": source_key,
                    "files": [],
                    "index": len(merged),
                }

            files = merged[pair_key]["files"]
            if isinstance(files, list) and filename not in files:
                files.append(filename)

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
    )


def unresolved_conflict_sources(candidates: Sequence[GlossaryCandidate]) -> list[str]:
    targets_by_source: dict[str, set[str]] = {}
    source_by_key: dict[str, str] = {}
    for candidate in candidates:
        source = candidate.source.strip()
        target = candidate.target.strip()
        if not source or not target:
            continue

        source_key = source.casefold()
        source_by_key.setdefault(source_key, source)
        targets_by_source.setdefault(source_key, set()).add(target)

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

        source_key = source.casefold()
        if source_key in seen_sources:
            continue

        seen_sources.add(source_key)
        lines.append(f"{source}->{target}")

    return "\n".join(lines)

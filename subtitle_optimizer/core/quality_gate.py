"""Deterministic export gate shared by single and batch task adapters."""

from __future__ import annotations

from core.models import SubtitleEntry
from core.task_models import QualityGateReport, QualityIssue


CRITICAL_QUALITY_FLAGS = {
    "source_integrity_failed",
    "translation_batch_failed",
    "translation_hard_gate_failed",
    "translation_repair_failed",
    "fidelity_review_failed",
    "optimize_target_invalid",
    "optimize_timeout",
    "optimize_failed",
    "consistency_guide_failed",
    "consistency_review_failed",
}


def evaluate_quality_gate(entries: list[SubtitleEntry], lint_report=None) -> QualityGateReport:
    issues: list[QualityIssue] = []
    for entry in entries:
        if entry.status in {"error", "needs_review"}:
            issues.append(
                QualityIssue(
                    "entry_status",
                    entry.error_msg or f"条目状态为 {entry.status}",
                    entry.index,
                    True,
                )
            )
        for flag in sorted(set(entry.quality_flags) & CRITICAL_QUALITY_FLAGS):
            issues.append(QualityIssue(flag, f"关键质量标记：{flag}", entry.index, True))

    if lint_report is not None:
        for issue in getattr(lint_report, "incomplete_bilingual", []) or []:
            issues.append(
                QualityIssue(
                    "incomplete_bilingual",
                    issue.message,
                    issue.index,
                    True,
                )
            )
        for issue in getattr(lint_report, "large_overlaps", []) or []:
            issues.append(
                QualityIssue("large_overlap", issue.message, issue.index, False)
            )
        for issue in (
            list(getattr(lint_report, "high_cps", []) or [])
            + list(getattr(lint_report, "bad_cps", []) or [])
        ):
            issues.append(QualityIssue("reading_speed", issue.message, issue.index, False))

    # Avoid duplicate entries when a failed status and its quality flag describe
    # the same cue; keep the structured kinds for diagnostics.
    deduped: list[QualityIssue] = []
    seen: set[tuple[str, int | None, str]] = set()
    for issue in issues:
        key = (issue.kind, issue.entry_index, issue.message)
        if key not in seen:
            seen.add(key)
            deduped.append(issue)
    return QualityGateReport(deduped)

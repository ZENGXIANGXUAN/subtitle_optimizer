"""Stable result and callback-neutral models for single and batch tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.models import SubtitleEntry


@dataclass(frozen=True)
class QualityIssue:
    kind: str
    message: str
    entry_index: int | None = None
    critical: bool = True


@dataclass
class QualityGateReport:
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def critical_issues(self) -> list[QualityIssue]:
        return [issue for issue in self.issues if issue.critical]

    @property
    def passed(self) -> bool:
        return not self.critical_issues

    def summary(self) -> str:
        return (
            "质量门禁通过"
            if self.passed
            else f"质量门禁未通过：{len(self.critical_issues)} 个关键问题"
        )


@dataclass
class PipelineRunResult:
    entries: list[SubtitleEntry]
    lint_report: Any = None
    quality_report: QualityGateReport = field(default_factory=QualityGateReport)


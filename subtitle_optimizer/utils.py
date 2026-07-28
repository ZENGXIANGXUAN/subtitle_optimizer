"""Logging setup and phase-snapshot helpers.

The log root deliberately lives *outside* the source tree. It used to be
``<repo>/logs/<date>/``, created at import time, with a non-rotating
FileHandler. That accumulated 357 MB across 2106 files, all of them tracked by
git, so every run dirtied the working tree; the resulting path lengths also
exceeded Windows MAX_PATH and broke ordinary git operations.

Override the location with ``SUBTITLE_OPTIMIZER_LOG_DIR`` if you want the logs
somewhere specific.
"""

import logging
import logging.handlers
import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path


# One .log file may reach 10 MB and keeps 3 rotations -> at most 40 MB per day.
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 3
# Dated directories older than this are pruned on startup. They hold only
# self-generated debug artifacts (rotated logs and phase snapshots).
LOG_RETENTION_DAYS = 30

_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def log_root() -> Path:
    """Base directory for all logs and snapshots, outside the repository."""
    override = os.environ.get("SUBTITLE_OPTIMIZER_LOG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".subtitle_optimizer" / "logs"


def _logs_dir() -> Path:
    p = log_root() / _today_str()
    p.mkdir(parents=True, exist_ok=True)
    return p


def phase_snapshots_enabled() -> bool:
    """Whether to write per-stage SRT snapshots (3 files per processed file).

    On by default: these snapshots are this project's main tool for tracing a
    bad cue back to the stage that produced it. They are bounded by
    :func:`prune_old_logs` instead of being disabled.
    """
    value = os.environ.get("SUBTITLE_OPTIMIZER_PHASE_SNAPSHOTS", "").strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    return True


def prune_old_logs(retention_days: int = LOG_RETENTION_DAYS) -> list[str]:
    """Delete dated log directories older than ``retention_days``.

    Only directories this module creates are considered: the name must match
    ``YYYY-MM-DD`` exactly. Returns the names removed so the caller can report
    them. Never raises -- losing old debug output must not stop the app.
    """
    removed: list[str] = []
    if retention_days <= 0:
        return removed
    root = log_root()
    cutoff = datetime.now().date() - timedelta(days=retention_days)
    try:
        candidates = list(root.iterdir())
    except OSError:
        return removed
    for entry in candidates:
        if not entry.is_dir() or not _DATE_DIR_RE.match(entry.name):
            continue
        try:
            entry_date = datetime.strptime(entry.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if entry_date >= cutoff:
            continue
        try:
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
        except OSError:
            continue
    return removed


class _LazyDirRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Create the log directory on first write, not at import time."""

    def _open(self):
        Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
        return super()._open()


def setup_logger(name: str = __name__, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(level)
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 全局日志 → <log_root>/YYYY-MM-DD/subtitle_optimizer.log（带大小轮转）
    log_path = log_root() / _today_str() / "subtitle_optimizer.log"
    file_handler = _LazyDirRotatingFileHandler(
        str(log_path),
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding='utf-8',
        delay=True,
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def get_log_file_path() -> Path:
    return log_root() / _today_str() / "subtitle_optimizer.log"


def write_phase_snapshot(srt_filename: str, phase: str, entries, use_optimized: bool = False):
    """将字幕内容写入每个阶段的独立 SRT 文件，方便追踪问题。

    输出路径: <log_root>/YYYY-MM-DD/{srt_filename}_{phase}.srt
    可用 SUBTITLE_OPTIMIZER_PHASE_SNAPSHOTS=0 关闭。
    """
    if not phase_snapshots_enabled():
        return
    from core.models import _format_subtitle_snapshot
    stem = Path(srt_filename).stem
    srt_path = _logs_dir() / f"{stem}_{phase}.srt"
    content = _format_subtitle_snapshot(phase, entries, use_optimized)
    with open(str(srt_path), "w", encoding="utf-8") as f:
        f.write(content + "\n")


# 创建全局日志记录器
logger = setup_logger("subtitle_optimizer")

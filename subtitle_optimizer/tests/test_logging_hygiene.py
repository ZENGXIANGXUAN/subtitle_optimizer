"""Regression tests for the log-root / rotation / retention behaviour.

Before 2026-07-26 the log root was ``<repo>/logs/<date>/``, created at *import*
time, written by a non-rotating FileHandler and never pruned. That reached
357 MB across 2106 git-tracked files, dirtied the working tree on every run,
and produced paths long enough to break git on Windows.
"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import utils


@pytest.fixture()
def log_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBTITLE_OPTIMIZER_LOG_DIR", str(tmp_path))
    importlib.reload(utils)
    yield tmp_path
    monkeypatch.delenv("SUBTITLE_OPTIMIZER_LOG_DIR", raising=False)
    importlib.reload(utils)


def _dated(root: Path, days_ago: int) -> Path:
    name = (datetime.now().date() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_default_log_root_is_outside_the_repository(monkeypatch) -> None:
    monkeypatch.delenv("SUBTITLE_OPTIMIZER_LOG_DIR", raising=False)
    importlib.reload(utils)
    root = utils.log_root()
    repo = Path(__file__).resolve().parents[1]
    assert repo not in root.parents and root != repo
    assert root == Path.home() / ".subtitle_optimizer" / "logs"


def test_log_root_honours_the_environment_override(log_root) -> None:
    assert utils.log_root() == log_root


def test_importing_utils_does_not_create_directories(log_root) -> None:
    """_logs_dir() used to run at import via the module-level setup_logger()."""
    for child in log_root.iterdir():
        child.rmdir()
    importlib.reload(utils)
    assert list(log_root.iterdir()) == []


def test_the_file_handler_rotates(log_root) -> None:
    handler = next(
        h for h in utils.setup_logger("rotation_probe").handlers
        if hasattr(h, "maxBytes")
    )
    assert handler.maxBytes == utils.LOG_MAX_BYTES
    assert handler.backupCount == utils.LOG_BACKUP_COUNT
    assert handler.maxBytes > 0, "a non-rotating handler reached 30 MB per day"


def test_prune_removes_only_expired_dated_directories(log_root) -> None:
    fresh = [_dated(log_root, d) for d in (0, 5, utils.LOG_RETENTION_DAYS - 1)]
    expired = [_dated(log_root, d) for d in (utils.LOG_RETENTION_DAYS + 1, 400)]

    removed = utils.prune_old_logs()

    assert sorted(removed) == sorted(p.name for p in expired)
    assert all(p.exists() for p in fresh)
    assert not any(p.exists() for p in expired)


def test_prune_never_touches_anything_it_did_not_create(log_root) -> None:
    keepers = []
    for name in ("debug_requests", "my-notes", "2026-13-99", "backup"):
        path = log_root / name
        path.mkdir()
        keepers.append(path)
    loose_file = log_root / "important.txt"
    loose_file.write_text("keep me", encoding="utf-8")
    _dated(log_root, 400)

    utils.prune_old_logs()

    assert all(p.exists() for p in keepers)
    assert loose_file.read_text(encoding="utf-8") == "keep me"


def test_prune_is_a_no_op_when_retention_is_disabled(log_root) -> None:
    old = _dated(log_root, 400)
    assert utils.prune_old_logs(retention_days=0) == []
    assert old.exists()


def test_prune_survives_a_missing_log_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUBTITLE_OPTIMIZER_LOG_DIR", str(tmp_path / "nope"))
    importlib.reload(utils)
    assert utils.prune_old_logs() == []


def test_phase_snapshots_can_be_switched_off(log_root, monkeypatch) -> None:
    from core.models import SubtitleEntry

    entry = SubtitleEntry(1, "00:00:01,000", "00:00:02,000", ["target", "source"])

    monkeypatch.setenv("SUBTITLE_OPTIMIZER_PHASE_SNAPSHOTS", "0")
    utils.write_phase_snapshot("movie.srt", "AFTER_TRANSLATE", [entry])
    assert not list(log_root.rglob("*.srt"))

    monkeypatch.delenv("SUBTITLE_OPTIMIZER_PHASE_SNAPSHOTS")
    utils.write_phase_snapshot("movie.srt", "AFTER_TRANSLATE", [entry])
    written = list(log_root.rglob("*.srt"))
    assert [p.name for p in written] == ["movie_AFTER_TRANSLATE.srt"]

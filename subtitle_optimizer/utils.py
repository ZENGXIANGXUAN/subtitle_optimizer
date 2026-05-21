import logging
import os
from datetime import datetime
from pathlib import Path


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _logs_dir() -> Path:
    p = Path(__file__).resolve().parent / "logs" / _today_str()
    p.mkdir(parents=True, exist_ok=True)
    return p


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

    # 全局日志 → logs/YYYY-MM-DD/subtitle_optimizer.log
    log_path = _logs_dir() / "subtitle_optimizer.log"
    file_handler = logging.FileHandler(str(log_path), encoding='utf-8')
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def get_log_file_path() -> Path:
    return _logs_dir() / "subtitle_optimizer.log"


def write_phase_snapshot(srt_filename: str, phase: str, entries, use_optimized: bool = False):
    """将字幕内容写入每个文件的阶段日志，方便追踪问题。

    输出路径: logs/YYYY-MM-DD/{srt_filename}.log
    """
    from core.models import _format_subtitle_snapshot
    stem = Path(srt_filename).stem
    log_path = _logs_dir() / f"{stem}.log"
    content = _format_subtitle_snapshot(phase, entries, use_optimized)
    with open(str(log_path), "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}]\n")
        f.write(content + "\n")


# 创建全局日志记录器
logger = setup_logger("subtitle_optimizer")
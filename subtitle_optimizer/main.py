#!/usr/bin/env python3
import sys
import asyncio

# Fix for Windows: aiohttp with ProactorEventLoop causes heap corruption crashes
# when multiple asyncio loops are created concurrently in Qt worker threads.
# WindowsSelectorEventLoopPolicy uses a stable, thread-safe selector-based loop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from PyQt6.QtWidgets import QApplication
from ui.main_window import MainWindow
from utils import LOG_RETENTION_DAYS, log_root, logger, prune_old_logs

def main():
    logger.info("应用程序启动")
    # 日志目录在仓库之外；启动时打印位置，并清理超过保留期的旧日志。
    logger.info("日志目录: %s", log_root())
    removed = prune_old_logs()
    if removed:
        logger.info(
            "已清理 %d 个超过 %d 天的日志目录: %s",
            len(removed), LOG_RETENTION_DAYS, ", ".join(sorted(removed)),
        )
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    logger.info("主窗口显示完成")
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
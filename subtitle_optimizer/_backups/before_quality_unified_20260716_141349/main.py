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
from utils import logger

def main():
    logger.info("应用程序启动")
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    logger.info("主窗口显示完成")
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
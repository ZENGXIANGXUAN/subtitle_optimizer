import logging
import os
from datetime import datetime
from pathlib import Path


def setup_logger(name: str = __name__, level: int = logging.INFO) -> logging.Logger:
    """
    设置日志记录器
    
    Args:
        name: 记录器名称，默认为模块名
        level: 日志级别，默认INFO
    
    Returns:
        logging.Logger: 配置好的日志记录器
    """
    logger = logging.getLogger(name)
    
    # 避免重复添加处理器
    if logger.handlers:
        return logger
    
    logger.setLevel(level)
    
    # 创建格式器
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
    )
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 文件处理器
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    # 按日期创建日志文件
    today = datetime.now().strftime("%Y%m%d")
    log_filename = log_dir / f"subtitle_optimizer_{today}.log"
    
    file_handler = logging.FileHandler(log_filename, encoding='utf-8')
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    return logger


def get_log_file_path() -> Path:
    """
    获取当前日志文件路径
    
    Returns:
        Path: 日志文件路径
    """
    log_dir = Path("logs")
    today = datetime.now().strftime("%Y%m%d")
    return log_dir / f"subtitle_optimizer_{today}.log"


# 创建全局日志记录器
logger = setup_logger("subtitle_optimizer")
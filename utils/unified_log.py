"""
Unified Logging System - 统一日志管理器

支持每个 case 单独输出日志文件，避免日志文件过大。

使用方式：
    from utils.unified_log import get_logger, init_logging, switch_to_case_log
    
    # 在 main.py 中初始化
    init_logging()
    
    # 每个 case 开始时切换到新的日志文件
    switch_to_case_log(syzid)
    
    # 在其他模块中获取 logger
    logger = get_logger("ModuleName")
    logger.info("消息")
"""

import logging
import os
import sys
from datetime import datetime
from typing import Optional

from config import Config

_log_dir = Config.LOG_DIR
_log_file: Optional[str] = None
_initialized = False
_root_logger: Optional[logging.Logger] = None
_file_handler: Optional[logging.FileHandler] = None
_console_handler: Optional[logging.StreamHandler] = None

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

LEVEL_COLORS = {
    "DEBUG": "\033[36m",     # Cyan
    "INFO": "\033[32m",      # Green
    "WARNING": "\033[33m",   # Yellow
    "ERROR": "\033[31m",     # Red
    "CRITICAL": "\033[35m",  # Magenta
}
RESET_COLOR = "\033[0m"


class ColoredFormatter(logging.Formatter):
    """带颜色的控制台格式化器"""
    
    def format(self, record):
        levelname = record.levelname
        if levelname in LEVEL_COLORS:
            record.levelname = f"{LEVEL_COLORS[levelname]}{levelname}{RESET_COLOR}"
        result = super().format(record)
        record.levelname = levelname
        return result


def init_logging(log_dir: str = None, log_level: int = logging.DEBUG) -> str:
    """
    初始化统一日志系统。
    
    Args:
        log_dir: 日志目录，默认使用 Config.LOG_DIR
        log_level: 日志级别，默认 DEBUG
        
    Returns:
        日志文件路径
    """
    global _log_dir, _log_file, _initialized, _root_logger, _file_handler, _console_handler
    
    if _initialized:
        return _log_file
    
    if log_dir:
        _log_dir = log_dir
    
    os.makedirs(_log_dir, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _log_file = os.path.join(_log_dir, f"agent_{timestamp}.log")
    
    _root_logger = logging.getLogger()
    _root_logger.setLevel(log_level)
    
    for handler in _root_logger.handlers[:]:
        _root_logger.removeHandler(handler)
    
    _console_handler = logging.StreamHandler(sys.stdout)
    _console_handler.setLevel(logging.INFO)
    console_formatter = ColoredFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S"
    )
    _console_handler.setFormatter(console_formatter)
    _root_logger.addHandler(_console_handler)
    
    _file_handler = logging.FileHandler(_log_file, encoding='utf-8')
    _file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        fmt=LOG_FORMAT,
        datefmt=DATE_FORMAT
    )
    _file_handler.setFormatter(file_formatter)
    _root_logger.addHandler(_file_handler)
    
    _initialized = True
    
    _root_logger.info(f"=" * 60)
    _root_logger.info(f"Log system initialized: {_log_file}")
    _root_logger.info(f"Log level: {logging.getLevelName(log_level)}")
    _root_logger.info(f"=" * 60)
    
    return _log_file


def switch_to_case_log(syzid: str) -> str:
    """
    切换到指定 case 的日志文件。
    
    每个 case 单独一个日志文件，避免日志文件过大。
    
    Args:
        syzid: Case ID
        
    Returns:
        新的日志文件路径
    """
    global _log_file, _file_handler, _root_logger
    
    if not _initialized:
        init_logging()
        return _log_file
    
    if _file_handler:
        _root_logger.removeHandler(_file_handler)
        _file_handler.close()
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _log_file = os.path.join(_log_dir, f"{syzid}_{timestamp}.log")
    
    _file_handler = logging.FileHandler(_log_file, encoding='utf-8')
    _file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        fmt=LOG_FORMAT,
        datefmt=DATE_FORMAT
    )
    _file_handler.setFormatter(file_formatter)
    _root_logger.addHandler(_file_handler)
    
    _root_logger.info(f"=" * 60)
    _root_logger.info(f"Case log: {syzid}")
    _root_logger.info(f"Log file: {_log_file}")
    _root_logger.info(f"=" * 60)
    
    return _log_file


def get_logger(name: str) -> logging.Logger:
    """
    获取指定名称的 Logger。
    
    如果日志系统未初始化，会自动初始化。
    
    Args:
        name: 模块名称
        
    Returns:
        Logger 实例
    """
    if not _initialized:
        init_logging()
    
    return logging.getLogger(name)


def get_log_file() -> Optional[str]:
    """获取当前日志文件路径"""
    return _log_file


def set_log_level(level: int):
    """
    动态设置日志级别。
    
    Args:
        level: logging.DEBUG, logging.INFO, etc.
    """
    if _root_logger:
        _root_logger.setLevel(level)
        for handler in _root_logger.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.setLevel(level)


def log_separator(logger: logging.Logger, title: str = "", char: str = "-", width: int = 60):
    """
    输出分隔线。
    
    Args:
        logger: Logger 实例
        title: 标题
        char: 分隔字符
        width: 宽度
    """
    if title:
        padding = (width - len(title) - 2) // 2
        line = char * padding + f" {title} " + char * padding
        if len(line) < width:
            line += char * (width - len(line))
        logger.info(line)
    else:
        logger.info(char * width)


class LogContext:
    """
    日志上下文管理器，用于记录代码块的执行时间。
    
    Usage:
        with LogContext(logger, "Processing task"):
            # do something
    """
    
    def __init__(self, logger: logging.Logger, action: str, level: int = logging.INFO):
        self.logger = logger
        self.action = action
        self.level = level
        self.start_time = None
    
    def __enter__(self):
        self.start_time = datetime.now()
        self.logger.log(self.level, f"[START] {self.action}")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed = (datetime.now() - self.start_time).total_seconds()
        if exc_type:
            self.logger.error(f"[FAILED] {self.action} ({elapsed:.2f}s) - {exc_val}")
        else:
            self.logger.log(self.level, f"[DONE] {self.action} ({elapsed:.2f}s)")
        return False

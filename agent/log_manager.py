"""
Agent Log Manager - 代理日志管理器

代理层日志接口，使用统一日志系统。
"""

import sys
import os

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from utils.unified_log import get_logger as _get_unified_logger, init_logging

def get_logger(name: str) -> "logging.Logger":
    """获取 Logger 实例"""
    return _get_unified_logger(name)

def setup_logging(name: str, log_file: str = None) -> "logging.Logger":
    """兼容旧接口的设置函数"""
    return _get_unified_logger(name)

import logging

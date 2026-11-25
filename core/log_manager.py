"""
Core Log Manager - 核心层日志管理器

核心层日志接口，使用统一日志系统。
"""

import sys
import os

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from utils.unified_log import get_logger as _get_unified_logger, init_logging

class LogManager:
    """日志管理器类，兼容旧代码"""
    
    @staticmethod
    def setup_logging(log_file: str = None, name: str = None) -> "logging.Logger":
        """兼容旧接口"""
        if name is None:
            name = "Validator"
        return _get_unified_logger(name)
    
    @staticmethod
    def get_logger(name: str = "Validator") -> "logging.Logger":
        """获取 Logger 实例"""
        return _get_unified_logger(name)

def get_logger(name: str = "Validator") -> "logging.Logger":
    """获取 Logger 实例"""
    return _get_unified_logger(name)

import logging

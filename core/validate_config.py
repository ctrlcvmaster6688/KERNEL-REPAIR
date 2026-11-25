# lib/validate_config.py
from dataclasses import dataclass, field
from typing import List, Optional

import paths

@dataclass
class ValidationConfig:
    # ==========================================
    # 1. 必填字段 (无默认值，必须放在最前面)
    # ==========================================

    # === 核心路径 ===
    KERNEL_SOURCE_PATH: str
    SYZKALLER_TOOL_PATH: str
    LOG_FILE_PATH: str
    QEMU_IMAGE_PATH: str

    # === SSH 配置 ===
    SSH_PORT: int
    SSH_KEY: str

    # === 任务信息 ===
    KERNEL_VERSION: str
    TARGET_FILE: str
    TARGET_FUNC: str
    SYZ_REPRODUCER_FILE: str

    # ==========================================
    # 2. 选填字段 (有默认值，必须放在后面)
    # ==========================================

    # Syzkaller 版本 (Adapter 可能会传 None)
    SYZKALLER_COMMIT: Optional[str] = None

    # Bug Config
    BUG_CONFIG_FILE: Optional[str] = None

    # 关键词
    BUG_KEYWORDS: List[str] = field(default_factory=lambda: ["KASAN"])

    # 默认配置路径
    DEFAULT_CONFIG_PATH: str = "arch/x86/configs/x86_64_defconfig"
    VMLINUX_PATH: str = "vmlinux"

    # 环境参数
    CACHE_ROOT: str = paths.CACHE_ROOT
    LANG_SO_PATH: str = paths.LANG_SO_PATH
    NUM_CORES: int = 16
    QEMU_MEM: str = "4G"
    QEMU_TIMEOUT: int = 300
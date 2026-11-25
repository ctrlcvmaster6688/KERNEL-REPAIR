import os

import paths

# SSH port of validation worker i is SSH_PORT_BASE + i (kept from the original
# three-worker layout, where the ports were 10021/10022/10023).
SSH_PORT_BASE = 10021

class Config:
    # ==========================
    # 1. 基础数据路径
    # ==========================
    DATASETS_DIR = paths.DATASETS_DIR
    KERNEL_DIR_ORI = paths.KERNEL_DIR_ORI
    
    # ==========================
    # 2. 知识库资源
    # ==========================
    DB_PATH = paths.DB_PATH
    BM25_PATH = paths.BM25_PATH
    # BGE-M3 embedding service (Flask, POST /embed, JSON {"content": ...} ->
    # {"embedding": [...]}).  Overridable with the EMBED_API_URL environment
    # variable; see README section 6 for how to start the service.
    EMBED_API_URL = os.getenv("EMBED_API_URL", "http://127.0.0.1:6001/embed")
    LANG_SO_PATH = paths.LANG_SO_PATH
    
    # ==========================
    # 3. 输入任务数据
    # ==========================
    ROOT_CAUSE_JSONL = paths.ROOT_CAUSE_JSONL
    LOC_RESULT_FILE = paths.LOC_RESULT_FILE
    COMMIT_FILE = paths.COMMIT_FILE
    SYZ_BRANCH_FILE = paths.SYZ_BRANCH_FILE
    ORACLE_FILE = paths.ORACLE_FILE
    
    # ==========================
    # 4. 输出与归档
    # ==========================
    LOG_DIR = paths.LOG_DIR
    PATCH_ARCHIVE_DIR = paths.PATCH_ARCHIVE_DIR
    
    # ==========================
    # 5. LLM 配置
    # ==========================
    LLM_API_KEY = os.getenv("LLM_API_KEY")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://chatapi.zjt66.top/v1")
    LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v3.2")
    MAX_CONCURRENT_LLM_REQ = 10
    PATCH_SAMPLING_INIT = 10
    
    # ==========================
    # 6. 并发资源池 (Worker Pool)
    # ==========================
    # 每个 worker 一组 (内核树 / syzkaller / QEMU 镜像 / SSH key)，由 paths.py 从
    # COHIKER_*_WORKER_* 环境变量解析；长度已在 paths.py 启动期校验过。SSH 端口沿用
    # 历史约定 10021 + worker id（模块级 SSH_PORT_BASE）。
    WORKER_POOL_CONFIG = [
        {
            "id": index,
            "linux_dir": paths.KERNEL_WORKER_DIRS[index],
            "syz_work_dir": paths.SYZKALLER_WORKER_DIRS[index],
            "image_path": paths.QEMU_IMAGE_PATHS[index],
            "ssh_port": SSH_PORT_BASE + index,
            "ssh_key": paths.QEMU_SSH_KEYS[index],
        }
        for index in range(len(paths.KERNEL_WORKER_DIRS))
    ]
    
    # ==========================
    # 7. 验证器环境通用配置
    # ==========================
    CACHE_ROOT = paths.CACHE_ROOT
    # 内核编译/验证资源。COHIKER_NUM_CORES 控制 make -j 的并行度（内存不足时调小），
    # COHIKER_QEMU_MEM 控制每个 QEMU 验证虚拟机的内存。
    ENV_CONFIG = {
        "NUM_CORES": int(os.getenv("COHIKER_NUM_CORES", "20")),
        "QEMU_MEM": os.getenv("COHIKER_QEMU_MEM", "4G"),
        "CACHE_ROOT": CACHE_ROOT
    }
    
    # ==========================
    # 8. 数据文件路径模板
    # ==========================
    @classmethod
    def get_bug_report_path(cls, syzid: str) -> str:
        return os.path.join(paths.BUG_REPORT_DIR, f"{syzid}.txt")
    
    @classmethod
    def get_testcase_path(cls, syzid: str) -> str:
        return os.path.join(paths.TESTCASE_DIR, f"{syzid}.syz")
    
    @classmethod
    def get_bug_config_path(cls, syzid: str) -> str:
        return os.path.join(paths.BUG_CONFIG_DIR, f"{syzid}.txt")

from typing import Dict, Any, Optional, Tuple
import subprocess
import os
import json
import time
import sys

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.validate_config import ValidationConfig
from core.kbuild_manager import KbuildManager
from core.qemu_runner import QemuRunner
from core.syz_manager import SyzManager
from core.patch_applier import KernelPatchApplier
from config import Config

_imported_modules = {}

def _get_logger():
    if "logger" not in _imported_modules:
        from agent.log_manager import get_logger
        _imported_modules["logger"] = get_logger("SandboxSkill")
    return _imported_modules["logger"]

class SandboxValidator:
    """
    Heavyweight Skill for validating patches in an isolated sandbox environment.
    
    This skill integrates:
    - KbuildManager (incremental compilation with cache)
    - QemuRunner (sandbox execution)
    - SyzManager (testcase execution)
    
    CRITICAL: This skill MUST preserve the Kbuild cache. All side-effects
    are confined to the workspace directory only.
    """
    
    def __init__(self, worker_config: Dict = None):
        self.worker_config = worker_config or Config.WORKER_POOL_CONFIG[0]
        self.log = _get_logger()
        
        self.kernel_path = self.worker_config["linux_dir"]
        self.syz_path = self.worker_config["syz_work_dir"]
        self.image_path = self.worker_config["image_path"]
        self.ssh_port = self.worker_config["ssh_port"]
        self.ssh_key = self.worker_config["ssh_key"]
        
        self._init_components()
    
    def _init_components(self):
        self.patch_applier = KernelPatchApplier(
            kernel_root=self.kernel_path,
            language_so_path=Config.LANG_SO_PATH
        )
    
    def _create_validation_config(
        self,
        kernel_commit: str,
        target_file: str,
        target_func: str,
        syz_commit: str,
        bug_config_path: str,
        syz_reproducer_path: str,
        bug_keywords: list
    ) -> ValidationConfig:
        return ValidationConfig(
            KERNEL_SOURCE_PATH=self.kernel_path,
            SYZKALLER_TOOL_PATH=self.syz_path,
            LOG_FILE_PATH=os.path.join(self.kernel_path, "validation.log"),
            QEMU_IMAGE_PATH=self.image_path,
            SSH_PORT=self.ssh_port,
            SSH_KEY=self.ssh_key,
            KERNEL_VERSION=kernel_commit,
            TARGET_FILE=target_file,
            TARGET_FUNC=target_func,
            SYZ_REPRODUCER_FILE=syz_reproducer_path,
            SYZKALLER_COMMIT=syz_commit,
            BUG_CONFIG_FILE=bug_config_path,
            BUG_KEYWORDS=bug_keywords,
            CACHE_ROOT=Config.ENV_CONFIG.get("CACHE_ROOT", Config.CACHE_ROOT),
            LANG_SO_PATH=Config.LANG_SO_PATH,
            NUM_CORES=Config.ENV_CONFIG.get("NUM_CORES", 16),
            QEMU_MEM=Config.ENV_CONFIG.get("QEMU_MEM", "4G")
        )
    
    def _is_git_repository(self, path: str) -> bool:
        """检查目录是否是git仓库"""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=path,
                capture_output=True,
                text=True
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def _safe_git_reset(self):
        """
        Safely reset the workspace WITHOUT touching the cache.
        This is the ONLY cleanup operation allowed.
        """
        self.log.info("🔄 [Sandbox] Resetting workspace...")
        try:
            if self._is_git_repository(self.kernel_path):
                subprocess.run(
                    ["git", "reset", "--hard"],
                    cwd=self.kernel_path,
                    check=True,
                    capture_output=True
                )
                subprocess.run(
                    ["git", "clean", "-fdx"],
                    cwd=self.kernel_path,
                    check=False,
                    capture_output=True
                )
                self.log.info("✅ Workspace reset complete")
            else:
                self.log.warning(f"⚠️ Not a git repository: {self.kernel_path}")
                self.log.warning("⚠️ Skipping git reset operation")
        except subprocess.CalledProcessError as e:
            self.log.error(f"⚠️ Git reset failed: {e.stderr.decode() if e.stderr else e}")
    
    def _setup_environment(
        self,
        kbuild: KbuildManager,
        syz_manager: SyzManager,
        config: ValidationConfig
    ) -> bool:
        """Setup compilation environment with cache preservation."""
        self.log.info("📦 [Sandbox] Step 1: Environment Setup...")
        
        try:
            # 设置缓存路径
            kbuild.set_cache_path(config.KERNEL_VERSION)
            
            # 尝试加载缓存
            if kbuild.load_full_snapshot():
                self.log.info("✅ Cache loaded successfully")
                return syz_manager.prepare_syzkaller()
            
            # 缓存未命中，需要进行全新构建
            self.log.info("⚠️ Cache miss, performing fresh build...")
            
            # 清理工作区
            if self._is_git_repository(config.KERNEL_SOURCE_PATH):
                kbuild.clean_workspace_for_checkout()
            else:
                self.log.warning("⚠️ Not a git repository, skipping git cleanup")
            
            # 设置配置
            success, msg = kbuild.setup_config()
            if not success:
                self.log.error(f"Config setup failed: {msg}")
                return False
            
            # 准备Syzkaller
            if not syz_manager.prepare_syzkaller():
                return False
            
            # 完整编译
            success, log, _ = kbuild.compile_kernel()
            if not success:
                self.log.error(f"Full build failed: {log[-500:]}")
                return False
            
            # 保存缓存
            kbuild.save_full_snapshot()
            return True
            
        except Exception as e:
            self.log.error(f"Environment setup failed: {e}")
            return False
    
    def validate_patch(
        self,
        patch_content: str,
        patch_id: str,
        kernel_commit: str,
        target_file: str,
        target_func: str,
        syz_commit: str = None,
        bug_config_path: str = None,
        syz_reproducer_path: str = None,
        bug_keywords: list = None,
        patches: list = None
    ) -> Dict[str, Any]:
        """
        Main entry point for patch validation.
        
        Args:
            patch_content: The modified function code (pure C, no markdown)
            patch_id: Unique identifier for this patch
            kernel_commit: Target kernel commit hash
            target_file: Relative path to target file
            target_func: Function name to patch
            syz_commit: Optional Syzkaller version
            bug_config_path: Optional kernel config file
            syz_reproducer_path: Path to Syzkaller reproducer
            bug_keywords: Keywords to detect crash (default: ["KASAN"])
            patches: List of patches for multi-file validation
            
        Returns:
            Dict with status, log, and timing information
        """
        start_time = time.time()
        
        result = {
            "patch_id": patch_id,
            "status": "PENDING",
            "log": "",
            "total_time_s": 0.0,
            "compile_time_s": 0.0,
            "qemu_time_s": 0.0
        }
        
        self.log.info(f"\n{'='*60}")
        self.log.info(f"🚀 [Sandbox] Starting validation for patch: {patch_id}")
        self.log.info(f"   Target: {target_func} @ {target_file}")
        self.log.info(f"   Worker: {self.worker_config['id']} ({self.kernel_path})")
        self.log.info(f"{'='*60}")
        
        try:
            # 1. 环境设置
            config = self._create_validation_config(
                kernel_commit=kernel_commit,
                target_file=target_file,
                target_func=target_func,
                syz_commit=syz_commit,
                bug_config_path=bug_config_path,
                syz_reproducer_path=syz_reproducer_path,
                bug_keywords=bug_keywords or ["KASAN"]
            )
            
            kbuild = KbuildManager(config)
            qemu_runner = QemuRunner(config, self.log)
            syz_manager = SyzManager(config, self.log)
            
            if not self._setup_environment(kbuild, syz_manager, config):
                result["status"] = "ENV_SETUP_FAILED"
                result["log"] = "Failed to setup compilation environment"
                return result
            
            # 2. 应用补丁
            self.log.info("🔧 [Sandbox] Step 2: Applying Patch...")
            if patches:
                # 应用多文件补丁
                success, apply_log = self._apply_multi_file_patch_internal(patches)
            else:
                # 应用单文件补丁
                success, apply_log = self._apply_patch_internal(
                    target_file, target_func, patch_content
                )
            if not success:
                result["status"] = "PATCH_APPLY_FAILED"
                result["log"] = apply_log
                return result
            
            # 3. 快速编译检查
            self.log.info("⚡ [Sandbox] Step 3: Fast Compilation Check...")
            compile_start = time.time()
            success, compile_log, _ = kbuild.targeted_make(target_file)
            if not success:
                result["status"] = "FAST_COMPILE_FAILED"
                result["log"] = compile_log
                result["compile_time_s"] = time.time() - compile_start
                return result
            
            # 4. 完整内核构建
            self.log.info("🔨 [Sandbox] Step 4: Full Kernel Build...")
            success, compile_log, build_time = kbuild.compile_kernel()
            result["compile_time_s"] = time.time() - compile_start
            if not success:
                result["status"] = "FULL_COMPILE_FAILED"
                result["log"] = compile_log
                return result
            
            # 5. QEMU验证
            self.log.info("🖥️ [Sandbox] Step 5: QEMU Validation...")
            qemu_start = time.time()
            status, qemu_log = qemu_runner.run_validation()
            result["qemu_time_s"] = time.time() - qemu_start
            
            result["status"] = status
            result["log"] = qemu_log
            
            if status == "FIXED_OR_TIMEOUT":
                self.log.info(f"🎉 [Sandbox] PATCH VALIDATED SUCCESSFULLY!")
            else:
                self.log.info(f"❌ [Sandbox] Validation failed: {status}")
            
            return result
            
        except Exception as e:
            self.log.error(f"💥 [Sandbox] Exception during validation: {e}")
            result["status"] = "SYSTEM_ERROR"
            result["log"] = str(e)
            return result
            
        finally:
            self._safe_git_reset()
            result["total_time_s"] = time.time() - start_time
            self.log.info(f"⏱️ [Sandbox] Total time: {result['total_time_s']:.2f}s")
    
    def _apply_patch_internal(
        self,
        target_file: str,
        target_func: str,
        patch_input
    ) -> Tuple[bool, str]:
        """
        Internal patch application with JSON wrapping.
        
        支持多种输入格式：
        1. 字符串 - 直接是 modified_code
        2. dict with "patch" key - {"patch": {"modified_code": "..."}} 或 {"patch": "..."}
        3. dict with "modified_code" - {"modified_code": "..."}
        """
        if isinstance(patch_input, dict):
            if "patch" in patch_input:
                patch_data = patch_input["patch"]
                if isinstance(patch_data, dict):
                    patch_json = json.dumps(patch_data)
                else:
                    patch_json = patch_data
            elif "modified_code" in patch_input:
                patch_json = json.dumps({"modified_code": patch_input["modified_code"]})
            else:
                return False, f"Invalid patch dict format: missing 'patch' or 'modified_code'"
        else:
            patch_json = json.dumps({"modified_code": str(patch_input)})
        
        return self.patch_applier.apply(target_file, target_func, patch_json)
        
    def _apply_multi_file_patch_internal(
        self,
        patches: list
    ) -> Tuple[bool, str]:
        """
        应用多文件补丁 - 拆分成单函数分别应用，更稳定
        
        策略：
        1. 先 git reset --hard 确保环境干净
        2. 逐个应用每个函数的补丁
        3. 如果任何一个失败，回滚所有已应用的补丁
        """
        if not patches:
            return False, "No patches provided"
        
        self.log.info(f"🔧 [Sandbox] Applying {len(patches)} patches sequentially...")
        
        applied_results = []
        
        for i, patch in enumerate(patches):
            file_path = patch.get("file_path")
            target_name = patch.get("target_name")
            target_type = patch.get("target_type", "function")
            
            if not file_path or not target_name:
                return False, f"Invalid patch format at index {i}: missing file_path or target_name"
            
            self.log.info(f"   [{i+1}/{len(patches)}] Applying patch to {target_name} in {file_path}")
            
            if target_type != "function":
                self.log.warning(f"   Skipping non-function target: {target_type}")
                continue
            
            success, apply_log = self._apply_patch_internal(file_path, target_name, patch)
            
            if not success:
                self.log.error(f"   ❌ Failed to apply patch to {target_name}: {apply_log}")
                self._safe_git_reset()
                return False, f"Failed to apply patch to {target_name} in {file_path}: {apply_log}"
            
            self.log.info(f"   ✅ Successfully applied patch to {target_name}")
            applied_results.append({
                "file_path": file_path,
                "target_name": target_name,
                "log": apply_log
            })
        
        self.log.info(f"✅ All {len(applied_results)} patches applied successfully")
        return True, f"Applied {len(applied_results)} patches: " + ", ".join(r["target_name"] for r in applied_results)


def get_sandbox_validator(worker_config: Dict = None) -> SandboxValidator:
    """Get or create a SandboxValidator instance for the given worker config."""
    global _sandbox_validators
    # 使用worker_id作为key，如果没有worker_config则使用None作为key
    worker_key = worker_config.get('id') if worker_config else None
    if worker_key not in _sandbox_validators:
        _sandbox_validators[worker_key] = SandboxValidator(worker_config)
    return _sandbox_validators[worker_key]

def validate_patch_in_sandbox(
    patch_content: str,
    patch_id: str,
    kernel_commit: str,
    target_file: str,
    target_func: str,
    syz_commit: str = None,
    bug_config_path: str = None,
    syz_reproducer_path: str = None,
    bug_keywords: list = None,
    worker_config: Dict = None,
    patches: list = None
) -> Dict[str, Any]:
    """
    Heavyweight Skill: Validate a patch in the QEMU sandbox.
    
    This function integrates:
    - Incremental compilation (KbuildManager with cache preservation)
    - Sandbox execution (QemuRunner)
    - Testcase execution (SyzManager)
    
    CACHE PRESERVATION GUARANTEE:
    - The Kbuild cache is NEVER modified or deleted
    - All side-effects are confined to the workspace directory
    - Git reset is used for cleanup, never cache deletion
    
    Args:
        patch_content: The modified function code (pure C, no markdown)
        patch_id: Unique identifier for this patch
        kernel_commit: Target kernel commit hash
        target_file: Relative path to target file
        target_func: Function name to patch
        syz_commit: Optional Syzkaller version
        bug_config_path: Optional kernel config file
        syz_reproducer_path: Path to Syzkaller reproducer
        bug_keywords: Keywords to detect crash
        worker_config: Worker pool configuration
        patches: List of patches for multi-file validation
        
    Returns:
        Dict with keys:
        - status: FIXED_OR_TIMEOUT | CRASHED | COMPILE_FAILED | SYSTEM_ERROR
        - log: Detailed execution log
        - total_time_s: Total validation time
        - compile_time_s: Compilation time
        - qemu_time_s: QEMU execution time
    """
    validator = get_sandbox_validator(worker_config)
    return validator.validate_patch(
        patch_content=patch_content,
        patch_id=patch_id,
        kernel_commit=kernel_commit,
        target_file=target_file,
        target_func=target_func,
        syz_commit=syz_commit,
        bug_config_path=bug_config_path,
        syz_reproducer_path=syz_reproducer_path,
        bug_keywords=bug_keywords,
        patches=patches
    )

# 全局变量，使用字典存储不同worker配置的validator实例
def __init__():
    global _sandbox_validators
    _sandbox_validators = {}

__init__()
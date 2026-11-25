# lib/validator_main.py
import subprocess
import os
import time
from typing import Dict, Any

from .validate_config import ValidationConfig
from .kbuild_manager import KbuildManager
from .qemu_runner import QemuRunner
from .log_manager import LogManager
from .syz_manager import SyzManager
from .patch_applier import KernelPatchApplier

class KernelPatchValidator:
    def __init__(self, config: ValidationConfig):
        # 1. 初始化日志
        logger_name = f"Validator_{os.path.basename(config.LOG_FILE_PATH)}"
        self.log = LogManager.setup_logging(config.LOG_FILE_PATH, logger_name)

        self.config = config
        self.log.info(f"Validator initialized for {config.TARGET_FUNC}")

        # 2. 初始化子模块
        self.kbuild_manager = KbuildManager(config) # KbuildManager 不需要 log 参数

        # [关键] QemuRunner 和 SyzManager 需要 log 参数
        self.qemu_runner = QemuRunner(config, self.log)
        self.syz_manager = SyzManager(config, self.log)

        self.patch_applier = KernelPatchApplier(
            kernel_root=config.KERNEL_SOURCE_PATH,
            language_so_path=config.LANG_SO_PATH
        )

    def _git_checkout_and_config(self) -> bool:
        """环境初始化"""
        self.log.info(f"\n--- 1. Environment Setup ---")
        try:
            # 执行 git checkout
            subprocess.run(
                ["git", "checkout", "-f", self.config.KERNEL_VERSION],
                cwd=self.config.KERNEL_SOURCE_PATH,
                check=True,
                capture_output=True
            )

            self.kbuild_manager.set_cache_path(self.config.KERNEL_VERSION)

            # 加载缓存
            if self.kbuild_manager.load_full_snapshot():
                return self.syz_manager.prepare_syzkaller()

            # 缓存未命中：全量构建
            self.log.info("  [INIT] Cache missing. Performing fresh build...")
            self.kbuild_manager.clean_workspace_for_checkout()

            success, msg = self.kbuild_manager.setup_config()
            if not success:
                self.log.error(f"Config setup failed: {msg}")
                return False

            if not self.syz_manager.prepare_syzkaller():
                return False

            success, log, duration = self.kbuild_manager.compile_kernel()
            if not success:
                self.log.error(f"  [INIT] Full build failed: {log[-500:]}")
                return False

            self.kbuild_manager.save_full_snapshot()
            return True

        except subprocess.CalledProcessError as e:
            # [关键] 捕获详细的 Git 错误
            self.log.error(f"Git checkout failed: {e.stderr.decode() if e.stderr else 'unknown'}")
            return False
        except Exception as e:
            self.log.error(f"Init failed: {e}")
            return False

    def validate_single_patch(self, patch_content: str, patch_id: str) -> Dict[str, Any]:
        start_t = time.time()
        result = {
            "patch_id": patch_id, "status": "PENDING", "log": "", "total_time_s": 0.0
        }

        self.log.info(f"\n--- Validating Patch: {patch_id} ---")

        success, log = self._apply_patch(patch_content)
        if not success:
            self._revert_patch()
            result.update({"status": "APPLY_FAILED", "log": log})
            return result

        try:
            success, log, _ = self.kbuild_manager.targeted_make(self.config.TARGET_FILE)
            if not success:
                result.update({"status": "FAST_COMPILE_FAILED", "log": log})
                return result

            success, log, _ = self.kbuild_manager.compile_kernel()
            if not success:
                result.update({"status": "FULL_COMPILE_FAILED", "log": log})
                return result

            status, qemu_log = self.qemu_runner.run_validation()
            result.update({"status": status, "log": qemu_log})
            return result

        except Exception as e:
            self.log.error(f"Validation Error: {e}")
            result.update({"status": "SYSTEM_ERROR", "log": str(e)})
            return result

        finally:
            self._revert_patch()
            result["total_time_s"] = time.time() - start_t
            self.log.info(f"  [RESULT] {result['status']} ({result['total_time_s']:.2f}s)")

    def _apply_patch(self, json_str):
        return self.patch_applier.apply(self.config.TARGET_FILE, self.config.TARGET_FUNC, json_str)

    def _revert_patch(self):
        try:
            subprocess.run(["git", "checkout", self.config.TARGET_FILE], cwd=self.config.KERNEL_SOURCE_PATH, capture_output=True)
        except: pass
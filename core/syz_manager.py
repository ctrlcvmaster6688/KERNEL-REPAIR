import subprocess
import os
import logging
from .validate_config  import ValidationConfig  # 适配 lib 目录结构
from .log_manager import LogManager   # 适配 lib 目录结构

class SyzManager:
    """管理 Syzkaller 的版本切换和编译。"""

    # --- 配置区域：保底版本 ---
    # 如果目标版本切不过去或者编译不过，就用这个版本
    FALLBACK_COMMIT = "52d40fd2"

    # [修改点] 增加 logger 参数，以匹配 ValidatorMain 的调用
    def __init__(self, config: ValidationConfig, logger: logging.Logger):
        self.config = config
        self.syz_path = config.SYZKALLER_TOOL_PATH
        # 注意：这里 config 中是 SYZKALLER_COMMIT (字符串)，不是对象
        self.target_commit = getattr(config, "SYZKALLER_COMMIT", None)

        # 使用传入的 logger，确保日志能写入文件
        self.log = logger

    def _get_current_commit(self) -> str:
        """获取当前 Syzkaller 仓库的 Commit Hash。"""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.syz_path,
                capture_output=True,
                text=True,
                check=True
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def _build_syzkaller(self) -> bool:
        """执行 make 编译 Syzkaller。"""
        self.log.info("  [SYZ] Building Syzkaller (this may take a while)...")
        try:
            # 1. 执行 make clean
            clean_cmd = ["make", "clean"]
            self.log.info("  [SYZ] Cleaning previous build...")

            subprocess.run(
                clean_cmd,
                cwd=self.syz_path,
                check=False, # clean 失败不应该阻断流程
                capture_output=True
            )

            # 2. 执行 make
            self.log.info("  [SYZ] Starting make...")
            # 这里的 build_cmd 保持你原有的逻辑
            build_cmd = ["make"]

            result = subprocess.run(
                build_cmd,
                cwd=self.syz_path,
                check=True,
                capture_output=True,
                text=True
            )

            self.log.info("  [SYZ] Build successful.")
            return True

        except subprocess.CalledProcessError as e:
            self.log.error(f"  [SYZ] Build failed. Return Code: {e.returncode}")
            if e.stderr:
                self.log.error(f"  [SYZ] STDERR: {e.stderr[:500]}...") # 只打前500字符
            return False
        except Exception as e:
            self.log.error(f"  [SYZ] Build exception: {e}")
            return False

    def _checkout_and_build(self, commit: str) -> bool:
        """内部辅助：尝试切换到指定 commit 并编译。"""
        try:
            self.log.info(f"  [SYZ] Trying to checkout: {commit[:8]}...")
            subprocess.run(
                ["git", "checkout", commit],
                cwd=self.syz_path,
                check=True,
                capture_output=True,
                text=True
            )

            # 切换成功后，尝试编译
            return self._build_syzkaller()

        except subprocess.CalledProcessError as e:
            self.log.warning(f"  [SYZ] Checkout failed for {commit[:8]}: {e.stderr.strip()}")
            return False
        except Exception as e:
            self.log.warning(f"  [SYZ] Exception during checkout {commit[:8]}: {e}")
            return False

    def prepare_syzkaller(self) -> bool:
        """
        检查版本并准备二进制文件。
        逻辑：优先使用 target_commit -> 失败则使用 FALLBACK_COMMIT -> 再失败则返回 False
        """
        # 1. 如果没有指定 commit，直接检查二进制是否存在，不存在就当前版本编译
        if not self.target_commit:
            self.log.info("  [SYZ] No specific version required. Checking binaries...")
            bin_path = os.path.join(self.syz_path, "bin/linux_amd64/syz-executor")
            if not os.path.exists(bin_path):
                self.log.info("  [SYZ] Binaries missing, building current HEAD...")
                if not self._build_syzkaller():
                    self.log.warning("  [SYZ] Current HEAD build failed. Trying fallback...")
                    return self._checkout_and_build(self.FALLBACK_COMMIT)
            return True

        current = self._get_current_commit()

        # 2. 如果版本已匹配且二进制存在，直接通过
        bin_path = os.path.join(self.syz_path, "bin/linux_amd64/syz-executor")
        if current.startswith(self.target_commit) and os.path.exists(bin_path):
            self.log.info(f"  [SYZ] Version match ({self.target_commit[:8]}), skipping build.")
            return True

        self.log.info(f"  [SYZ] Switching version: {current[:8]} -> {self.target_commit[:8]}")

        # 3. 尝试切换到目标版本并编译
        if self._checkout_and_build(self.target_commit):
            return True

        # 4. 保底逻辑
        self.log.warning(f"  [SYZ] ⚠️ Target version {self.target_commit[:8]} failed. Attempting FALLBACK: {self.FALLBACK_COMMIT}")

        if self._checkout_and_build(self.FALLBACK_COMMIT):
            self.log.info(f"  [SYZ] ✅ Fallback version {self.FALLBACK_COMMIT} ready.")
            return True
        else:
            self.log.error("  [SYZ] ❌ Both target and fallback versions failed to build.")
            return False
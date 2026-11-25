# lib/kbuild_manager.py
import subprocess
import os
import shutil
import time
from typing import Tuple

from .validate_config  import ValidationConfig
from .log_manager import LogManager
import paths

class KbuildManager:
    """管理 Linux 内核的配置、编译，并处理完整的版本快照缓存。"""

    def __init__(self, config: ValidationConfig):
        self.config = config
        self.kernel_path = config.KERNEL_SOURCE_PATH
        self.num_cores = config.NUM_CORES
        self.default_config_path = config.DEFAULT_CONFIG_PATH
        self.vmlinux_path_in_source = config.VMLINUX_PATH
        self.cache_root = config.CACHE_ROOT
        self.cache_dir: str = ""
        self.log = LogManager.get_logger()
        # 添加原始内核目录路径
        self.kernel_dir_ori = paths.KERNEL_DIR_ORI

    def _detect_old_build_path(self) -> str:

        witness_file = os.path.join(self.kernel_path, "tools/objtool/.objtool-in.o.cmd")

        # 如果 objtool 没编译好，尝试找 auto.conf.cmd
        if not os.path.exists(witness_file):
            witness_file = os.path.join(self.kernel_path, "include/config/auto.conf.cmd")

        if not os.path.exists(witness_file):
            return ""

        try:
            with open(witness_file, "r", errors="ignore") as f:
                content = f.read()

            # 2. 定义嫌疑人名单 (见 paths.KERNEL_PATH_CANDIDATES)
            # 注意：必须把可能的最短路径放在最后检查，或者匹配时带上 '/'
            # 这里我们采用带 '/' 的策略，绝对安全
            candidates = list(paths.KERNEL_PATH_CANDIDATES)

            # 3. 逐个排查
            for path in candidates:
                # 关键技巧：加上斜杠匹配，防止前缀相同的路径互相误匹配
                search_term = f"{path}/"

                if search_term in content:
                    # self.log.info(f"  [DETECT] Identified old path: {path}")
                    return path

        except Exception as e:
            self.log.warning(f"  [DETECT] Failed to read witness: {e}")

        return ""
    def _perform_path_surgery(self):
        # 1. 穷举法侦测
        old_path = self._detect_old_build_path()

        # 2. 获取当前路径
        current_path = os.path.abspath(self.kernel_path).rstrip('/')

        # 3. 如果没找到，或者路径没变，不做手术
        if not old_path or old_path == current_path:
            return

        self.log.info(f"  [SURGERY] Path mismatch: '{old_path}' -> '{current_path}'")

        start_t = time.time()

        # 3. 构造替换命令
        # 使用管道符 | 作为分隔符，防止路径斜杠冲突
        sed_cmd = f"s|{old_path}|{current_path}|g"

        # 覆盖 .cmd, .config, 和其他可能的依赖文件
        cmd = (
            f"find . -type f \\( -name '.*.cmd' -o -name '.config' -o -name '*.cmd' \\) -print0 | "
            f"xargs -0 -P {self.num_cores} sed -i '{sed_cmd}'"
        )

        try:
            subprocess.run(cmd, shell=True, cwd=self.kernel_path, check=True)

            # [可选] 修正 .config 时间戳
            ref_file = os.path.join(self.kernel_path, "Makefile")
            config_file = os.path.join(self.kernel_path, ".config")
            if os.path.exists(ref_file) and os.path.exists(config_file):
                ref_time = os.path.getmtime(ref_file)
                os.utime(config_file, (ref_time, ref_time))

            duration = time.time() - start_t
            self.log.info(f"  [SURGERY] Done in {duration:.2f}s. Cache is now localized.")

        except subprocess.CalledProcessError as e:
            self.log.error(f"  [SURGERY] Failed: {e}")

    def _run_make_command(self, target: str = "all") -> Tuple[bool, str, float]:
        """执行 make 命令并捕获输出，并返回运行时间。"""
        start_time = time.time()
        # 确保使用绝对路径作为 CWD
        cwd_path = os.path.abspath(self.kernel_path)

        cmd = ["make", f"-j{self.num_cores}", target]
        # self.log.info(f"  [KBUILD] run: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                cwd=cwd_path,
                capture_output=True,
                text=True,
                check=True
            )
            return True, result.stdout, time.time() - start_time
        except subprocess.CalledProcessError as e:
            return False, e.stdout + e.stderr, time.time() - start_time
        except FileNotFoundError:
            return False, f"Error: Could not find 'make' command or kernel path {self.kernel_path}", 0.0

    def _run_rsync_command(self, source: str, destination: str, delete: bool = False) -> Tuple[bool, str]:
        """执行 rsync 命令"""
        # 源路径末尾加 / 表示同步目录下的内容
        src_path = source if source.endswith('/') else source + '/'

        # -aH: 归档模式 (保留时间戳) + 硬链接
        cmd = ["rsync", "-aH", "--exclude=.git", src_path, destination]

        if delete:
            cmd.insert(1, "--delete")

        try:
            cwd = None
            if not source.startswith('/'):
                cwd = self.kernel_path

            result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
            if result.returncode != 0:
                self.log.error(f"Rsync failed. STDERR:\n{result.stderr}")
                return False, result.stdout + result.stderr
            return True, "Rsync successful."
        except Exception as e:
            self.log.error(f"Rsync failed with exception: {e}")
            return False, f"Rsync failed with exception: {e}"

    def setup_config(self) -> Tuple[bool, str]:
        """设置内核配置 (.config 文件)。"""
        config_path = os.path.join(self.config.KERNEL_SOURCE_PATH, ".config")

        try:
            if self.config.BUG_CONFIG_FILE and os.path.exists(self.config.BUG_CONFIG_FILE):
                self.log.info(f"  [CONFIG] : {self.config.BUG_CONFIG_FILE} (replace .config)")
                shutil.copyfile(self.config.BUG_CONFIG_FILE, config_path)

            elif os.path.exists(os.path.join(self.config.KERNEL_SOURCE_PATH, self.config.DEFAULT_CONFIG_PATH)):
                self.log.info("  [CONFIG]  make defconfig...")
                subprocess.run(
                    ["make", "defconfig"],
                    cwd=self.config.KERNEL_SOURCE_PATH,
                    check=True,
                    capture_output=True
                )
            else:
                return False, "Configuration failed: Neither BUG_CONFIG_FILE nor default defconfig path found."

            self.log.info("  [CONFIG]  make olddefconfig ...")
            subprocess.run(["make", "olddefconfig"], cwd=self.config.KERNEL_SOURCE_PATH, check=True, capture_output=True)

            return True, "Configuration successful."

        except subprocess.CalledProcessError as e:
            error_log = e.stderr.decode() if e.stderr else str(e)
            self.log.error(f"Configuration failed. Log: {error_log}")
            return False, f"Configuration failed. Log: {error_log}"
        except Exception as e:
            self.log.error(f"Configuration failed during file operations: {e}")
            return False, f"Configuration failed during file operations: {e}"

    def clean_workspace_for_checkout(self):
        """清理内核工作目录"""
        self.log.info("  [KBUILD] clean kernel...")
        subprocess.run(["git", "reset", "--hard"], cwd=self.kernel_path, check=False, capture_output=True)
        subprocess.run(["git", "clean", "-fdx"], cwd=self.kernel_path, check=False, capture_output=True)

    def set_cache_path(self, version_sha: str):
        self.cache_dir = os.path.join(self.cache_root, version_sha)
        os.makedirs(self.cache_dir, exist_ok=True)
        self.log.info(f"  [CACHE] Cache directory set to: {self.cache_dir}")

    def _clear_workspace(self):
        """
        快速清空工作区，包括.git目录。
        """
        self.log.info(f"  [CLEAN] Clearing workspace {self.kernel_path} (including .git)...")
        if not os.path.exists(self.kernel_path):
            return

        try:
            for item in os.listdir(self.kernel_path):
                path = os.path.join(self.kernel_path, item)
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                    else:
                        os.unlink(path)
                except Exception as e:
                    self.log.warning(f"  [CLEAN] Failed to delete {item}: {e}")
        except Exception as e:
            self.log.error(f"  [CLEAN] Critical error during clean: {e}")

    def load_full_snapshot(self) -> bool:
        """使用 rsync 将快照从缓存复制到工作区。"""
        if not self.cache_dir or not os.path.exists(self.cache_dir) or not os.listdir(self.cache_dir):
            self.log.info("  [CACHE] cache missing, copying from original kernel directory")
            
            # 1. 先清空工作区
            self._clear_workspace()
            
            # 2. 从原始内核目录复制
            if os.path.exists(self.kernel_dir_ori) and os.listdir(self.kernel_dir_ori):
                self.log.info(f"  [CACHE] Copying from original kernel directory: {self.kernel_dir_ori}")
                success, log = self._run_rsync_command(
                    source=self.kernel_dir_ori,
                    destination=self.kernel_path,
                    delete=True
                )
                
                if not success:
                    self.log.error("  [CACHE] Failed to copy from original kernel directory")
                    return False
                
                # 3. 应用bug config
                self.log.info("  [CACHE] Applying bug config...")
                config_success, config_log = self.setup_config()
                if not config_success:
                    self.log.error(f"  [CACHE] Failed to apply bug config: {config_log}")
                    return False
                
                self.log.info("  [CACHE] Copied from original kernel directory and applied bug config")
                return True
            else:
                self.log.error(f"  [CACHE] Original kernel directory not found or empty: {self.kernel_dir_ori}")
                return False

        self.log.info(f"  [CACHE] 正在使用 rsync 将快照从 {self.cache_dir} 复制到工作区...")

        # 1. 先清空工作区，这样rsync速度更快
        self._clear_workspace()

        # 2. 再同步，使用delete=True确保完全一致
        success, log = self._run_rsync_command(
            source=self.cache_dir,
            destination=self.kernel_path,
            delete=True
        )

        if not success:
            return False
        self._perform_path_surgery()
        self.log.info("  [CACHE] cache loaded")
        return True

    def save_full_snapshot(self):
        """保存快照"""
        if not self.cache_dir:
            self.log.error("Cache path not set.")
            raise Exception("Cache path not set.")

        self.log.info(f"  [CACHE] Saving the workspace using rsync {self.cache_dir}...")

        # 保存时 delete=True
        success, log = self._run_rsync_command(
            source=self.kernel_path,
            destination=self.cache_dir,
            delete=True
        )

        if not success:
            self.log.error(f"Cache save failed: {log}")
            raise Exception(f"Cache save failed: {log}")

        self.log.info("  [CACHE] Cache saved。")

    def targeted_make(self, file_path: str) -> Tuple[bool, str, float]:
        """定向编译"""
        if not file_path.endswith('.c'):
            return False, f"Targeted make requires a .c file path, got {file_path}", 0.0

        abs_kernel_root = os.path.abspath(self.kernel_path)

        # 1. 相对路径计算
        try:
            if os.path.isabs(file_path):
                if os.path.abspath(file_path).startswith(abs_kernel_root):
                    relative_path = os.path.relpath(file_path, abs_kernel_root)
                else:
                    relative_path = file_path
            else:
                relative_path = file_path
        except Exception:
            relative_path = file_path

        # 2. 路径清洗
        if relative_path.startswith("..") or os.path.isabs(relative_path):
            self.log.warning(f"  [PATH FIX] Detected irregular path: {file_path}. Attempting to resolve...")
            kernel_prefixes = ["fs/", "drivers/", "mm/", "kernel/", "net/", "arch/", "lib/", "block/", "crypto/", "security/", "sound/", "virt/", "include/", "ipc/", "init/"]
            found = False
            for prefix in kernel_prefixes:
                idx = relative_path.find(prefix)
                if idx != -1:
                    relative_path = relative_path[idx:]
                    self.log.info(f"  [PATH FIX] Resolved to: {relative_path}")
                    found = True
                    break
            if not found:
                self.log.warning(f"  [PATH FIX] Could not resolve standard kernel path.")

        # 3. 转换为 .o
        target = relative_path.replace(".c", ".o")

        self.log.info(f"  [FAST CHECK] Compiling target: {target}")
        success, log, duration = self._run_make_command(target=target)
        self.log.info(f"  [FAST CHECK] compile cost: {duration:.2f} s")

        return success, log, duration

    def compile_kernel(self) -> Tuple[bool, str, float]:
        """执行增量编译"""
        self.log.info("[BUILD] Starting incremental compilation (bzImage)...")
        # [关键修改] 改为 bzImage，这是解决编译慢的核心
        success, log, duration = self._run_make_command(target="bzImage")
        self.log.info(f"[BUILD] compile cost: {duration:.2f} s")
        return success, log, duration
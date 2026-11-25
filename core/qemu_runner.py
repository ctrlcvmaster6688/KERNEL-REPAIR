# lib/qemu_runner.py
import subprocess
import os
import time
import logging
import re
import signal
from typing import Tuple

# [保持 Import 不变]
from .validate_config import ValidationConfig
import paths

class QemuRunner:
    def __init__(self, config: ValidationConfig, logger: logging.Logger):
        self.config = config
        self.log = logger

        # [吸收] 路径逻辑：日志和 PID 放在源码目录下 (kernel_source_path)
        self.kernel_source_path = config.KERNEL_SOURCE_PATH
        self.vm_log_path = os.path.join(self.kernel_source_path, "vm.log")
        self.pid_path = os.path.join(self.kernel_source_path, "vm.pid")

        # 动态端口 (必须保留动态性，不能写死 10022)
        self.ssh_port = config.SSH_PORT

        # 快捷引用
        self.ssh_key_path = config.SSH_KEY
        self.qemu_timeout_boot = getattr(config, 'QEMU_TIMEOUT_BOOT', 300)
        self.qemu_timeout_syz = getattr(config, 'QEMU_TIMEOUT_SYZ', 120)

    def _start_qemu(self):
        """启动 QEMU，使用 Popen + 文件重定向 (吸收好人代码逻辑)"""
        self.log.info(f"  [QEMU] Starting VM on port {self.ssh_port}...")

        # 清理旧日志
        if os.path.exists(self.vm_log_path):
            os.remove(self.vm_log_path)
        self._kill_qemu()

        # 构造命令 (保留你验证过正确的参数)
        kernel_bzimage = os.path.join(self.config.KERNEL_SOURCE_PATH, "arch/x86/boot/bzImage")

        cmd = [
            "qemu-system-x86_64",
            "-m", self.config.QEMU_MEM,
            "-smp", "2,sockets=1,cores=2,threads=1",
            "-cpu", "host",
            "-kernel", kernel_bzimage,
            "-append", "console=ttyS0 root=/dev/sda earlyprintk=serial net.ifnames=0",
            "-drive", f"file={self.config.QEMU_IMAGE_PATH},format=raw,snapshot=on",
            "-net", f"user,hostfwd=tcp:127.0.0.1:{self.ssh_port}-:22",
            "-net", "nic,model=e1000",
            "-enable-kvm",
            "-nographic", # 使用 Popen 时，-nographic 会把输出送到 stdout，方便我们重定向
            "-pidfile", self.pid_path
        ]

        # [Debug]
        # self.log.info(f"  [CMD] {' '.join(cmd)}")

        # [吸收] 核心改动：使用 Popen 将 stdout/stderr 重定向到 vm.log
        try:
            with open(self.vm_log_path, "w") as f:
                subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=self.kernel_source_path)
            self.log.info("  [QEMU] Process started. Logs -> vm.log")
        except Exception as e:
            self.log.error(f"  [QEMU] Failed to start process: {e}")
            raise e

    def _wait_vm_ready(self):
        """[吸收] 循环读取 vm.log 等待登录提示"""
        self.log.info("  [QEMU] Waiting for boot login prompt...")
        start = time.time()

        while time.time() - start < self.qemu_timeout_boot:
            time.sleep(3) # 轮询间隔
            if os.path.exists(self.vm_log_path):
                try:
                    with open(self.vm_log_path, "r", errors="ignore") as f:
                        # 优化：只读文件末尾 5KB，避免文件过大卡顿
                        f.seek(0, 2)
                        size = f.tell()
                        f.seek(max(size - 5000, 0), 0)
                        log_chunk = f.read()

                        # 检查标志
                        if "syzkaller login:" in log_chunk :
                            self.log.info("  [QEMU] qemu start...")
                            return True
                except: pass
        return False

    def _run_reproducer_in_vm(self) -> bool:
        """[吸收] 处理 Repro 文件并执行"""
        self.log.info("  [QEMU] Preparing reproducer...")

        # 1. 传输工具 (保持你的路径)
        syz_exec = os.path.join(self.config.SYZKALLER_TOOL_PATH, "bin/linux_amd64/syz-execprog")
        syz_executor = os.path.join(self.config.SYZKALLER_TOOL_PATH, "bin/linux_amd64/syz-executor")

        if not self._transfer_file(syz_exec, paths.GUEST_HOME_DIR + "/"): return False
        if not self._transfer_file(syz_executor, paths.GUEST_HOME_DIR + "/"): return False

        # 2. [吸收] 预处理 repro 文件 (加 # 号逻辑)
        src_repro = self.config.SYZ_REPRODUCER_FILE
        tmp_repro = os.path.join(self.kernel_source_path, "repro_processed.txt")

        try:
            with open(src_repro, "r") as fin, open(tmp_repro, "w") as fout:
                for line in fin:
                    s = line.strip()
                    if not s:
                        fout.write("\n")
                        continue
                    if s.startswith("#"):
                        fout.write(line)
                    else:
                        fout.write(f"{s}#\n") # 关键：补全 #

            self._transfer_file(tmp_repro, paths.GUEST_REPRO_FILE)
        except Exception as e:
            self.log.error(f"  [QEMU] Repro processing failed: {e}")
            self._transfer_file(src_repro, paths.GUEST_REPRO_FILE) # 兜底
        finally:
            if os.path.exists(tmp_repro): os.remove(tmp_repro)

        # 3. [吸收] 执行命令 (使用 ssh >> vm.log 方式)
        # 这样所有的输出（包括 Crash 信息）都会汇聚到 vm.log
        self.log.info("  [QEMU] Running syz-execprog...")

        run_cmd = (
            f"cd {paths.GUEST_HOME_DIR} && chmod +x syz-execprog syz-executor && "
            "timeout 60s ./syz-execprog -repeat 10 repro.txt"
        )

        ssh_cmd = (
            f'ssh -i {self.ssh_key_path} -p {self.ssh_port} '
            f'-o "StrictHostKeyChecking no" -o "UserKnownHostsFile=/dev/null" '
            f'-t root@localhost "{run_cmd}" '
            f'>> {self.vm_log_path} 2>&1' # 重定向到日志文件
        )
        self.log.info("  [QEMU] Running syz-execprog finished...")
        # 异步执行，不阻塞 Python，通过 sleep 等待
        subprocess.Popen(ssh_cmd, shell=True, cwd=self.kernel_source_path)

        # 等待执行 (好人代码是直接 sleep)
        time.sleep(self.qemu_timeout_syz)
        return True

    def run_validation(self) -> Tuple[str, str]:
        """主流程"""
        try:
            # 1. 启动
            self._start_qemu()

            # 2. 等待 Login
            if not self._wait_vm_ready():
                log_tail = self._read_log_tail()
                self._kill_qemu()
                return "QEMU_BOOT_TIMEOUT", f"Boot timeout. Tail:\n{log_tail}"

            # 3. 运行测试
            self._run_reproducer_in_vm()

            # 4. 检查结果 (读取 vm.log)
            if not os.path.exists(self.vm_log_path):
                return "QEMU_LOG_MISSING", "No vm.log generated"

            full_log = ""
            with open(self.vm_log_path, "r", errors="ignore") as f:
                full_log = f.read()

            self._kill_qemu()

            if self._check_for_crash(full_log):
                return "CRASHED", full_log
            else:
                return "FIXED_OR_TIMEOUT", full_log

        except Exception as e:
            self._kill_qemu()
            return "QEMU_ERROR", str(e)

    def _kill_qemu(self):
        # 1. PID 文件清理
        if os.path.exists(self.pid_path):
            try:
                with open(self.pid_path, 'r') as f:
                    pid = int(f.read().strip())
                os.kill(pid, signal.SIGKILL)
            except: pass
            try: os.remove(self.pid_path)
            except: pass

        # 2. 端口兜底清理
        subprocess.run(f"fuser -k -n tcp {self.ssh_port}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _transfer_file(self, local, remote):
        cmd = [
            "scp", "-P", str(self.ssh_port), "-i", self.ssh_key_path,
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            local, f"root@localhost:{remote}"
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except: return False

    def _check_for_crash(self, content):
        for kw in self.config.BUG_KEYWORDS:
            if kw in content:
                self.log.info(f"  [QEMU] Find keyword '{kw}' CRASH")
                return True
        return ("Rebooting" in content) or ("Kernel panic" in content)

    def _read_log_tail(self):
        try:
            with open(self.vm_log_path, "r", errors="ignore") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(size - 2000, 0), 0)
                return f.read()
        except: return "Unable to read log."

from typing import Dict, Any, List, Optional, Tuple
import subprocess
import os
import json
import time
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import Lock

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
        _imported_modules["logger"] = get_logger("ParallelValidator")
    return _imported_modules["logger"]

@dataclass
class ValidationTask:
    patch_id: str
    patch_content: str
    worker_id: int
    worker_config: Dict

@dataclass
class ValidationResult:
    patch_id: str
    worker_id: int
    status: str
    log: str
    total_time_s: float
    compile_time_s: float
    qemu_time_s: float
    error: Optional[str] = None

class WorkerSession:
    def __init__(self, worker_config: Dict):
        self.config = worker_config
        self.worker_id = worker_config["id"]
        self.kernel_path = worker_config["linux_dir"]
        self.syz_path = worker_config["syz_work_dir"]
        self.image_path = worker_config["image_path"]
        self.ssh_port = worker_config["ssh_port"]
        self.ssh_key = worker_config["ssh_key"]
        self.log = _get_logger()
        self.lock = Lock()
        
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
            LOG_FILE_PATH=os.path.join(self.kernel_path, f"validation_worker{self.worker_id}.log"),
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
    
    def _safe_git_reset(self):
        with self.lock:
            try:
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
            except subprocess.CalledProcessError as e:
                self.log.warning(f"[Worker-{self.worker_id}] Git reset warning: {e}")
    
    def validate(
        self,
        patch_content: str,
        patch_id: str,
        kernel_commit: str,
        target_file: str,
        target_func: str,
        syz_commit: str = None,
        bug_config_path: str = None,
        syz_reproducer_path: str = None,
        bug_keywords: list = None
    ) -> ValidationResult:
        start_time = time.time()
        
        self.log.info(f"🔧 [Worker-{self.worker_id}] Starting: {patch_id}")
        
        result = ValidationResult(
            patch_id=patch_id,
            worker_id=self.worker_id,
            status="PENDING",
            log="",
            total_time_s=0.0,
            compile_time_s=0.0,
            qemu_time_s=0.0
        )
        
        try:
            self._safe_git_reset()
            
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
                result.status = "ENV_SETUP_FAILED"
                result.log = "Failed to setup environment"
                return result
            
            success, apply_log = self._apply_patch_internal(
                target_file, target_func, patch_content
            )
            if not success:
                result.status = "PATCH_APPLY_FAILED"
                result.log = apply_log
                return result
            
            compile_start = time.time()
            success, compile_log, _ = kbuild.targeted_make(target_file)
            if not success:
                result.status = "FAST_COMPILE_FAILED"
                result.log = compile_log
                result.compile_time_s = time.time() - compile_start
                return result
            
            success, compile_log, _ = kbuild.compile_kernel()
            result.compile_time_s = time.time() - compile_start
            if not success:
                result.status = "FULL_COMPILE_FAILED"
                result.log = compile_log
                return result
            
            qemu_start = time.time()
            status, qemu_log = qemu_runner.run_validation()
            result.qemu_time_s = time.time() - qemu_start
            
            result.status = status
            result.log = qemu_log
            
            if status == "FIXED_OR_TIMEOUT":
                self.log.info(f"✅ [Worker-{self.worker_id}] SUCCESS: {patch_id}")
            else:
                self.log.info(f"❌ [Worker-{self.worker_id}] FAILED: {patch_id} - {status}")
            
            return result
            
        except Exception as e:
            self.log.error(f"💥 [Worker-{self.worker_id}] Exception: {e}")
            result.status = "SYSTEM_ERROR"
            result.log = str(e)
            result.error = str(e)
            return result
            
        finally:
            self._safe_git_reset()
            result.total_time_s = time.time() - start_time
    
    def _setup_environment(self, kbuild: KbuildManager, syz_manager: SyzManager, config: ValidationConfig) -> bool:
        try:
            subprocess.run(
                ["git", "checkout", "-f", config.KERNEL_VERSION],
                cwd=config.KERNEL_SOURCE_PATH,
                check=True,
                capture_output=True
            )
            
            kbuild.set_cache_path(config.KERNEL_VERSION)
            
            if kbuild.load_full_snapshot():
                self.log.info(f"[Worker-{self.worker_id}] Cache loaded")
                return syz_manager.prepare_syzkaller()
            
            self.log.info(f"[Worker-{self.worker_id}] Cache miss, building...")
            kbuild.clean_workspace_for_checkout()
            
            success, msg = kbuild.setup_config()
            if not success:
                return False
            
            if not syz_manager.prepare_syzkaller():
                return False
            
            success, log, _ = kbuild.compile_kernel()
            if not success:
                return False
            
            kbuild.save_full_snapshot()
            return True
            
        except Exception as e:
            self.log.error(f"[Worker-{self.worker_id}] Setup failed: {e}")
            return False
    
    def _apply_patch_internal(self, target_file: str, target_func: str, patch_content: str) -> Tuple[bool, str]:
        patch_json = json.dumps({"modified_code": patch_content})
        return self.patch_applier.apply(target_file, target_func, patch_json)


class ParallelValidator:
    """
    Parallel patch validator using multiple workers.
    
    Utilizes WORKER_POOL_CONFIG to run concurrent validations,
    maximizing CPU utilization across 48 cores with 3 workers
    each using 16 cores.
    """
    
    def __init__(self, max_workers: int = None):
        self.log = _get_logger()
        self.max_workers = max_workers or len(Config.WORKER_POOL_CONFIG)
        self.workers: Dict[int, WorkerSession] = {}
        self._init_workers()
    
    def _init_workers(self):
        for i, worker_config in enumerate(Config.WORKER_POOL_CONFIG[:self.max_workers]):
            self.workers[worker_config["id"]] = WorkerSession(worker_config)
            self.log.info(f"🏗️ Initialized Worker-{worker_config['id']}")
    
    def validate_single(
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
        worker_id: int = None
    ) -> ValidationResult:
        if worker_id is None:
            worker_id = list(self.workers.keys())[0]
        
        worker = self.workers.get(worker_id)
        if not worker:
            return ValidationResult(
                patch_id=patch_id,
                worker_id=-1,
                status="INVALID_WORKER",
                log=f"Worker {worker_id} not found",
                total_time_s=0,
                compile_time_s=0,
                qemu_time_s=0,
                error="Invalid worker ID"
            )
        
        return worker.validate(
            patch_content=patch_content,
            patch_id=patch_id,
            kernel_commit=kernel_commit,
            target_file=target_file,
            target_func=target_func,
            syz_commit=syz_commit,
            bug_config_path=bug_config_path,
            syz_reproducer_path=syz_reproducer_path,
            bug_keywords=bug_keywords
        )
    
    def validate_batch(
        self,
        patches: List[Dict],
        kernel_commit: str,
        target_file: str,
        target_func: str,
        syz_commit: str = None,
        bug_config_path: str = None,
        syz_reproducer_path: str = None,
        bug_keywords: list = None
    ) -> List[ValidationResult]:
        """
        Validate multiple patches in parallel.
        
        Args:
            patches: List of {"patch_id": str, "patch_content": str}
            
        Returns:
            List of ValidationResult, ordered by original patches order
        """
        self.log.info(f"\n{'='*60}")
        self.log.info(f"🚀 [ParallelValidator] Starting batch validation")
        self.log.info(f"   Patches: {len(patches)}")
        self.log.info(f"   Workers: {self.max_workers}")
        self.log.info(f"{'='*60}")
        
        results: List[ValidationResult] = [None] * len(patches)
        worker_ids = list(self.workers.keys())
        
        start_time = time.time()
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_idx = {}
            
            for idx, patch in enumerate(patches):
                worker_id = worker_ids[idx % len(worker_ids)]
                
                future = executor.submit(
                    self.validate_single,
                    patch_content=patch["modified_code"],
                    patch_id=patch["patch_id"],
                    kernel_commit=kernel_commit,
                    target_file=target_file,
                    target_func=target_func,
                    syz_commit=syz_commit,
                    bug_config_path=bug_config_path,
                    syz_reproducer_path=syz_reproducer_path,
                    bug_keywords=bug_keywords,
                    worker_id=worker_id
                )
                future_to_idx[future] = idx
            
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    result = future.result()
                    results[idx] = result
                except Exception as e:
                    self.log.error(f"Future exception: {e}")
                    results[idx] = ValidationResult(
                        patch_id=patches[idx]["patch_id"],
                        worker_id=-1,
                        status="EXECUTOR_ERROR",
                        log=str(e),
                        total_time_s=0,
                        compile_time_s=0,
                        qemu_time_s=0,
                        error=str(e)
                    )
        
        total_time = time.time() - start_time
        
        success_count = sum(1 for r in results if r.status == "FIXED_OR_TIMEOUT")
        
        self.log.info(f"\n{'='*60}")
        self.log.info(f"🏁 [ParallelValidator] Batch complete")
        self.log.info(f"   Total time: {total_time:.2f}s")
        self.log.info(f"   Success: {success_count}/{len(patches)}")
        self.log.info(f"{'='*60}")
        
        return results
    
    def validate_and_find_best(
        self,
        patches: List[Dict],
        kernel_commit: str,
        target_file: str,
        target_func: str,
        syz_commit: str = None,
        bug_config_path: str = None,
        syz_reproducer_path: str = None,
        bug_keywords: list = None
    ) -> Tuple[Optional[List[Dict]], List[ValidationResult]]:
        """
        Validate patches in parallel and return all successful ones.
        
        Returns:
            (success_patches, all_results) - success_patches is None if all failed
        """
        results = self.validate_batch(
            patches=patches,
            kernel_commit=kernel_commit,
            target_file=target_file,
            target_func=target_func,
            syz_commit=syz_commit,
            bug_config_path=bug_config_path,
            syz_reproducer_path=syz_reproducer_path,
            bug_keywords=bug_keywords
        )
        
        success_patches = []
        for i, result in enumerate(results):
            if result.status == "FIXED_OR_TIMEOUT":
                success_patches.append(patches[i])
        
        return success_patches if success_patches else None, results


_parallel_validator: Optional[ParallelValidator] = None

def get_parallel_validator(max_workers: int = None) -> ParallelValidator:
    global _parallel_validator
    if _parallel_validator is None:
        _parallel_validator = ParallelValidator(max_workers)
    return _parallel_validator

def validate_patches_parallel(
    patches: List[Dict],
    kernel_commit: str,
    target_file: str,
    target_func: str,
    syz_commit: str = None,
    bug_config_path: str = None,
    syz_reproducer_path: str = None,
    bug_keywords: list = None,
    max_workers: int = None
) -> Tuple[Optional[List[Dict]], List[ValidationResult]]:
    """
    Main API: Validate multiple patches in parallel.
    
    Args:
        patches: List of {"patch_id": str, "modified_code": str}
        max_workers: Number of parallel workers (default: all available)
        
    Returns:
        (success_patches, all_results) - success_patches is None if all failed
    """
    validator = get_parallel_validator(max_workers)
    return validator.validate_and_find_best(
        patches=patches,
        kernel_commit=kernel_commit,
        target_file=target_file,
        target_func=target_func,
        syz_commit=syz_commit,
        bug_config_path=bug_config_path,
        syz_reproducer_path=syz_reproducer_path,
        bug_keywords=bug_keywords
    )

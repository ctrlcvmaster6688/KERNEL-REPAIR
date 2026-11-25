"""
Fix Thread Graph - 使用并行修复线程的工作流

特点：
1. Planner 只在初始时调用一次，生成候选假设
2. 每个候选假设创建一个独立的修复线程
3. 每个线程包含完整的闭环逻辑：Generator → Reviewer → Validator → Judge → 循环
4. 线程共享 CaseSharedMemory，避免重复犯错
5. 全局尝试次数限制：MAX_TOTAL_ATTEMPTS = 8
"""

import os
import subprocess
import time
from typing import Literal, Dict, Any
from langgraph.graph import StateGraph, END

from agent.state import (
    BugFixState,
    create_initial_state,
    CaseSharedMemoryManager,
    MAX_TOTAL_ATTEMPTS,
    MAX_TOTAL_ATTEMPTS_WITH_FUNCTIONS,
)
from agent.parallel_nodes import (
    parallel_planner_node,
    exploration_planner_node,
    record_failure,
    generate_failure_report
)
from agent.fix_thread import run_parallel_fix_threads
from agent.log_manager import get_logger
from config import Config
import paths

logger = get_logger("FixThreadGraph")


def _warmup_kernel_cache(kernel_commit: str, bug_config_path: str, syz_commit: str):
    """
    预热内核缓存：在启动修复线程前确保内核已编译好
    
    这样可以避免多个线程同时发现缓存未命中，重复编译的问题
    """
    import shutil
    from core.validate_config import ValidationConfig
    from core.kbuild_manager import KbuildManager
    
    logger.info(f"\n{'='*60}")
    logger.info(f"🔥 Warming up kernel cache...")
    logger.info(f"   Kernel commit: {kernel_commit}")
    logger.info(f"{'='*60}\n")
    
    # 使用第一个 worker 的配置
    worker_config = Config.WORKER_POOL_CONFIG[0]
    worker_linux_dir = worker_config["linux_dir"]
    cache_root = Config.ENV_CONFIG.get("CACHE_ROOT", Config.CACHE_ROOT)
    cache_dir = os.path.join(cache_root, kernel_commit)
    kernel_dir_ori = Config.KERNEL_DIR_ORI
    
    # 检查缓存是否已存在且有效
    if os.path.exists(cache_dir) and os.listdir(cache_dir):
        vmlinux_path = os.path.join(cache_dir, "vmlinux")
        if os.path.exists(vmlinux_path):
            logger.info(f"✅ Cache already exists: {cache_dir}")
            return True
    
    logger.info(f"📦 Cache miss, building kernel...")
    
    # 创建验证配置
    validation_config = ValidationConfig(
        KERNEL_SOURCE_PATH=worker_linux_dir,
        SYZKALLER_TOOL_PATH=worker_config["syz_work_dir"],
        LOG_FILE_PATH=os.path.join(worker_linux_dir, "warmup.log"),
        QEMU_IMAGE_PATH=worker_config["image_path"],
        SSH_PORT=worker_config["ssh_port"],
        SSH_KEY=worker_config["ssh_key"],
        KERNEL_VERSION=kernel_commit,
        TARGET_FILE="",
        TARGET_FUNC="",
        SYZ_REPRODUCER_FILE="",
        SYZKALLER_COMMIT=syz_commit,
        BUG_CONFIG_FILE=bug_config_path,
        BUG_KEYWORDS=["KASAN"],
        CACHE_ROOT=cache_root,
        LANG_SO_PATH=Config.LANG_SO_PATH,
        NUM_CORES=Config.ENV_CONFIG.get("NUM_CORES", 16),
        QEMU_MEM=Config.ENV_CONFIG.get("QEMU_MEM", "4G")
    )
    
    kbuild = KbuildManager(validation_config)
    
    try:
        # 1. 清空 worker-0 目录（确保有完整的 git 目录）
        logger.info(f"   [1/5] Clearing worker-0 directory: {worker_linux_dir}")
        if os.path.exists(worker_linux_dir):
            for item in os.listdir(worker_linux_dir):
                item_path = os.path.join(worker_linux_dir, item)
                try:
                    if os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                    else:
                        os.unlink(item_path)
                except Exception as e:
                    logger.warning(f"   Failed to delete {item}: {e}")
        
        # 2. 从 linux-ori 复制到 worker-0
        logger.info(f"   [2/5] Copying from {kernel_dir_ori} to {worker_linux_dir}")
        if not os.path.exists(kernel_dir_ori):
            logger.error(f"   Original kernel directory not found: {kernel_dir_ori}")
            return False
        
        rsync_cmd = ["rsync", "-aH", f"{kernel_dir_ori}/", worker_linux_dir]
        result = subprocess.run(rsync_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"   Failed to copy: {result.stderr}")
            return False
        
        # 3. Checkout 到目标 commit
        logger.info(f"   [3/5] Checking out kernel commit: {kernel_commit}")
        subprocess.run(
            ["git", "checkout", "-f", kernel_commit],
            cwd=worker_linux_dir,
            check=True,
            capture_output=True
        )
        
        # 4. 设置缓存路径并配置内核
        kbuild.set_cache_path(kernel_commit)
        
        logger.info(f"   [4/5] Configuring kernel...")
        success, msg = kbuild.setup_config()
        if not success:
            logger.error(f"   Failed to configure kernel: {msg}")
            return False
        
        # 5. 编译内核（使用全部核心，提高效率）
        logger.info(f"   [5/5] Compiling kernel with full cores...")
        start_time = time.time()
        
        # 直接运行 make -j<NUM_CORES>（NUM_CORES 来自 COHIKER_NUM_CORES，默认 20；
        # 内核全量编译每个 job 约需 1.5-2 GB 内存，内存不足时请调小）
        compile_cmd = ["make", f"-j{validation_config.NUM_CORES}", "bzImage"]
        result = subprocess.run(
            compile_cmd,
            cwd=worker_linux_dir,
            capture_output=True,
            text=True
        )
        
        duration = time.time() - start_time
        
        if result.returncode != 0:
            logger.error(f"   Failed to compile kernel: {result.stderr[:500]}")
            return False
        
        logger.info(f"   ✅ Kernel compiled in {duration:.2f}s")
        
        # 7. 保存到缓存
        logger.info(f"   Saving to cache: {cache_dir}")
        kbuild.save_full_snapshot()
        
        logger.info(f"\n{'='*60}")
        logger.info(f"✅ Kernel cache warmed up successfully!")
        logger.info(f"   Total time: {time.time() - start_time:.2f}s")
        logger.info(f"{'='*60}\n")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Failed to warmup kernel cache: {e}")
        return False

def route_after_planner(state: BugFixState) -> Literal["fix_threads", "generate_report"]:
    """Planner后的路由"""
    candidate_hypotheses = state.get("candidate_hypotheses", [])
    
    if not candidate_hypotheses:
        logger.info("🔀 [Router] No candidate hypotheses -> generate_report")
        return "generate_report"
    
    logger.info(f"🔀 [Router] Processing {len(candidate_hypotheses)} candidate hypotheses -> fix_threads")
    return "fix_threads"

def route_after_fix_threads(state: BugFixState) -> Literal["end", "parallel_planner", "exploration_planner", "record_failure"]:
    """修复线程后的路由，支持保守阶段和探索阶段"""
    memory_manager = CaseSharedMemoryManager()
    
    if memory_manager.is_success():
        logger.info("🔀 [Router] Fix successful -> END")
        return "end"
    
    # 检查是否没有有效线程运行
    if state.get("no_valid_threads", False):
        logger.info("🔀 [Router] No valid threads were run -> record_failure")
        return "record_failure"

    if state.get("function_combination"):
        total_attempts = memory_manager.get_total_attempts()
        logger.info(
            f"🔀 [Router] Function-combination mode attempts: "
            f"{total_attempts}/{MAX_TOTAL_ATTEMPTS_WITH_FUNCTIONS}"
        )
        if total_attempts < MAX_TOTAL_ATTEMPTS_WITH_FUNCTIONS:
            logger.info("🔀 [Router] Generating another root-cause candidate for fixed functions -> parallel_planner")
            return "parallel_planner"
        logger.info("🔀 [Router] Function-combination candidate limit reached -> record_failure")
        return "record_failure"
    
    # 检查全局尝试限制
    if not memory_manager.can_continue():
        logger.info("🔀 [Router] Global attempt limit reached -> record_failure")
        return "record_failure"
    
    phase = state.get("phase", "conservative")
    candidate_hypotheses = state.get("candidate_hypotheses", [])
    
    if phase == "conservative":
        # 获取全局尝试次数
        total_attempts = memory_manager.get_total_attempts()
        
        logger.info(f"🔀 [Router] Phase: {phase}, Global attempts: {total_attempts}/4")
        
        if total_attempts < 3:
            # 继续保守阶段
            logger.info(f"🔀 [Router] Still in conservative phase, only {total_attempts}/4 tried -> parallel_planner")
            return "parallel_planner"
        else:
            # 保守阶段完成，切换到探索阶段
            logger.info(f"🔀 [Router] Conservative phase complete ({total_attempts} combinations tried) -> switching to exploration phase")
            state["phase"] = "exploration"
            return "exploration_planner"
    else:
        # 获取全局尝试次数
        total_attempts = memory_manager.get_total_attempts()
        # 计算探索阶段的尝试次数（全局尝试次数 - 保守阶段尝试次数）
        exploration_attempts = total_attempts - 3
        
        logger.info(f"🔀 [Router] Phase: {phase}, Exploration attempts: {exploration_attempts}/4")
        
        if exploration_attempts < 4:
            # 继续探索阶段
            logger.info(f"🔀 [Router] Still in exploration phase, only {exploration_attempts}/3 tried -> exploration_planner")
            return "exploration_planner"
        else:
            # 探索阶段完成
            logger.info(f"🔀 [Router] Exploration phase complete ({exploration_attempts} combinations tried) -> record_failure")
            return "record_failure"

def fix_threads_node(state: BugFixState) -> BugFixState:
    """运行并行修复线程"""
    syzid = state["syzid"]
    kernel_commit = state["kernel_commit"]
    syz_commit = state["syz_commit"]
    bug_config_path = state["bug_config_path"]
    bug_report = state["bug_report"]
    testcase = state["testcase"]
    candidate_hypotheses = state.get("candidate_hypotheses", [])
    task_queue = state.get("task_queue", [])
    
    memory_manager = CaseSharedMemoryManager()
    
    # 预热缓存：在启动线程前确保内核已编译好
    _warmup_kernel_cache(kernel_commit, bug_config_path, syz_commit)
    
    logger.info(f"\n{'='*60}")
    logger.info(f"🚀 Starting Parallel Fix Threads")
    logger.info(f"   Candidates: {len(candidate_hypotheses)}")
    logger.info(f"{'='*60}\n")
    
    # 获取禁用工具的参数
    no_golden_fixes = state.get("no_golden_fixes", False)
    no_historical_fixes = state.get("no_historical_fixes", False)
    results = run_parallel_fix_threads(
        candidate_hypotheses=candidate_hypotheses,
        syzid=syzid,
        kernel_commit=kernel_commit,
        syz_commit=syz_commit,
        bug_config_path=bug_config_path,
        bug_report=bug_report,
        testcase=testcase,
        memory_manager=memory_manager,
        task_queue=task_queue,
        no_golden_fixes=no_golden_fixes,
        no_historical_fixes=no_historical_fixes
    )
    
    state["fix_threads"] = results
    state["no_valid_threads"] = False
    
    # 检查是否有线程实际运行
    if not results:
        logger.warning("⚠️ No fix threads were actually run (all combinations were skipped)")
        # 标记为无有效线程运行
        state["no_valid_threads"] = True
    
    if memory_manager.is_success():
        state["final_status"] = "SUCCESS"
        success_patches = memory_manager.get_memory()["success_patches"]
        success_functions_list = memory_manager.get_memory()["success_functions_list"]
        
        # 存储所有成功的补丁
        state["success_patches"] = success_patches
        state["success_functions_list"] = success_functions_list
        
        # 为了保持向后兼容，仍然设置 success_patch 为第一个成功的补丁
        if success_patches:
            state["success_patch"] = success_patches[0]
        
        logger.info(f"\n{'='*60}")
        logger.info(f"🎉 Fix Thread Succeeded!")
        logger.info(f"   Number of successful patches: {len(success_patches)}")
        for i, (patch, functions) in enumerate(zip(success_patches, success_functions_list)):
            logger.info(f"   Patch {i+1}: {patch.get('patch_id', 'N/A')}")
            function_names = [f.get('name', 'unknown') for f in functions]
            logger.info(f"   Functions: {', '.join(function_names)}")
        logger.info(f"{'='*60}\n")
    else:
        state["final_status"] = "FAILED"
        total_attempts = memory_manager.get_total_attempts()
        abandoned = memory_manager.get_abandoned_combinations()
        
        logger.info(f"\n{'='*60}")
        logger.info(f"❌ All Fix Threads Failed")
        logger.info(f"   Total attempts: {total_attempts}")
        logger.info(f"   Abandoned combinations: {len(abandoned)}")
        logger.info(f"{'='*60}\n")
    
    return state

def build_fix_thread_agent_app():
    """
    构建并行修复线程的 Agent 工作流
    """
    logger.info("🏗️ Building Fix Thread Agent App...")
    
    workflow = StateGraph(BugFixState)
    
    workflow.add_node("parallel_planner", parallel_planner_node)
    workflow.add_node("exploration_planner", exploration_planner_node)
    workflow.add_node("fix_threads", fix_threads_node)
    workflow.add_node("record_failure", record_failure)
    workflow.add_node("generate_report", generate_failure_report)
    
    workflow.set_entry_point("parallel_planner")
    
    workflow.add_conditional_edges(
        "parallel_planner",
        route_after_planner,
        {
            "fix_threads": "fix_threads",
            "generate_report": "generate_report"
        }
    )
    
    workflow.add_conditional_edges(
        "exploration_planner",
        route_after_planner,
        {
            "fix_threads": "fix_threads",
            "generate_report": "generate_report"
        }
    )
    
    workflow.add_conditional_edges(
        "fix_threads",
        route_after_fix_threads,
        {
            "end": END,
            "parallel_planner": "parallel_planner",
            "exploration_planner": "exploration_planner",
            "record_failure": "record_failure"
        }
    )
    
    workflow.add_edge("record_failure", "generate_report")
    workflow.add_edge("generate_report", END)
    
    app = workflow.compile()
    
    logger.info("✅ Fix Thread Agent App built successfully")
    
    return app

def run_fix_thread_agent(
    syzid: str,
    kernel_commit: str,
    syz_commit: str,
    bug_config_path: str,
    bug_report: str,
    testcase: str,
    task_queue: list,
    oracle_keyword: str = "",
    recursion_limit: int = 50,
    no_golden_fixes: bool = False,
    no_historical_fixes: bool = False,
    function_combination: list = None
) -> Dict[str, Any]:
    """
    运行并行修复线程的 Bug 修复 Agent
    
    Args:
        syzid: Syzbot 案例 ID
        kernel_commit: 目标内核提交
        syz_commit: Syzkaller 版本
        bug_config_path: 内核配置路径
        bug_report: Bug 报告
        testcase: Syzkaller 测试用例
        task_queue: 嫌疑函数队列
        oracle_keyword: 用于验证补丁的关键词
        recursion_limit: 最大递归深度 (默认: 50)
        
    Returns:
        最终状态字典
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"🚀 Starting Fix Thread Linux Kernel Bug Fix Agent")
    logger.info(f"   SyzID: {syzid}")
    logger.info(f"   Kernel Commit: {kernel_commit}")
    logger.info(f"   Task Queue: {len(task_queue)} functions")
    logger.info(f"   Oracle Keyword: {oracle_keyword or 'N/A'}")
    logger.info(f"{'='*60}\n")
    
    initial_state = create_initial_state(
        syzid=syzid,
        kernel_commit=kernel_commit,
        syz_commit=syz_commit,
        bug_config_path=bug_config_path,
        bug_report=bug_report,
        testcase=testcase,
        task_queue=task_queue,
        oracle_keyword=oracle_keyword,
        no_golden_fixes=no_golden_fixes,
        no_historical_fixes=no_historical_fixes,
        function_combination=function_combination
    )
    
    app = build_fix_thread_agent_app()
    
    config = {"recursion_limit": recursion_limit}
    
    final_state = None
    for event in app.stream(initial_state, config=config):
        for node_name, node_state in event.items():
            logger.info(f"\n─── Node: {node_name} ───")
            if "candidate_hypotheses" in node_state:
                logger.info(f"   Candidate hypotheses: {len(node_state.get('candidate_hypotheses', []))}")
            if "fix_threads" in node_state:
                logger.info(f"   Fix threads: {len(node_state.get('fix_threads', []))}")
            if node_state.get("success_patch"):
                logger.info(f"   ✅ Success patch found!")
            final_state = node_state
    
    logger.info(f"\n{'='*60}")
    logger.info(f"🏁 Fix Thread Agent Execution Complete")
    logger.info(f"   Final Status: {final_state.get('final_status', 'UNKNOWN')}")
    if final_state.get("success_patch"):
        logger.info(f"   ✅ Successful Patch: {final_state['success_patch'].get('patch_id', 'N/A')}")
    logger.info(f"   Total failures: {len(final_state.get('failure_history', []))}")
    
    memory_manager = CaseSharedMemoryManager()
    logger.info(f"   Total attempts: {memory_manager.get_total_attempts()}")
    logger.info(f"   Abandoned combinations: {len(memory_manager.get_abandoned_combinations())}")
    logger.info(f"{'='*60}\n")
    
    return final_state

if __name__ == "__main__":
    test_task_queue = [
        {
            "func_name": "test_function_1",
            "file_path": "kernel/test.c",
            "root_cause": "NULL pointer dereference"
        },
        {
            "func_name": "test_function_2",
            "file_path": "kernel/test.c",
            "root_cause": "Buffer overflow"
        },
        {
            "func_name": "test_function_3",
            "file_path": "kernel/test.c",
            "root_cause": "Race condition"
        }
    ]
    
    result = run_fix_thread_agent(
        syzid="TEST_CASE",
        kernel_commit="abc123",
        syz_commit="def456",
        bug_config_path=paths.DEMO_BUG_CONFIG_PATH,
        bug_report="Test bug report",
        testcase="syz_testcase()",
        task_queue=test_task_queue
    )
    
    print(f"\nFinal Result: {result}")

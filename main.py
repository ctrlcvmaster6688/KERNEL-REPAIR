import os
import sys
import json
import argparse
import subprocess
import time
from datetime import datetime

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from utils.unified_log import init_logging, get_logger, log_separator, switch_to_case_log
from utils.llm_interface import get_token_stats, reset_token_stats
from agent.fix_thread_graph import run_fix_thread_agent
from agent.state import create_initial_state
from utils.file_ops import read_file
from config import Config

init_logging(log_dir=Config.LOG_DIR)
logger = get_logger("MainAgent")

def format_duration(seconds: float) -> str:
    """Format elapsed seconds as HH:MM:SS."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def cleanup_all_qemu_vms():
    """
    清理所有 QEMU 虚拟机进程
    
    在每个 case 结束后调用，确保资源释放
    """
    logger.info("🧹 Cleaning up all QEMU VMs...")
    
    cleaned_count = 0
    
    for worker_config in Config.WORKER_POOL_CONFIG:
        worker_id = worker_config["id"]
        linux_dir = worker_config["linux_dir"]
        ssh_port = worker_config["ssh_port"]
        pid_file = os.path.join(linux_dir, "vm.pid")
        
        if os.path.exists(pid_file):
            try:
                with open(pid_file, 'r') as f:
                    pid = int(f.read().strip())
                os.kill(pid, 9)
                logger.info(f"   [Worker-{worker_id}] Killed QEMU PID {pid}")
                cleaned_count += 1
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.debug(f"   [Worker-{worker_id}] PID file cleanup error: {e}")
            finally:
                try:
                    os.remove(pid_file)
                except:
                    pass
        
        try:
            result = subprocess.run(
                f"fuser -k -n tcp {ssh_port}",
                shell=True,
                capture_output=True,
                text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                logger.info(f"   [Worker-{worker_id}] Killed process on port {ssh_port}")
                cleaned_count += 1
        except Exception as e:
            logger.debug(f"   [Worker-{worker_id}] Port cleanup error: {e}")
    
    if cleaned_count > 0:
        logger.info(f"✅ Cleaned up {cleaned_count} QEMU processes")
    else:
        logger.info("   No QEMU processes to clean")
    
    return cleaned_count

def load_kernel_commit(syzid: str) -> str:
    """
    Load kernel commit hash for a given syzid from COMMIT_FILE.
    
    File format (case_commit.txt):
        syzid1:kernel_commit_hash1
        syzid2:kernel_commit_hash2
        ...
    """
    if not os.path.exists(Config.COMMIT_FILE):
        logger.warning(f"COMMIT_FILE not found: {Config.COMMIT_FILE}")
        return None
    
    with open(Config.COMMIT_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if ':' in line:
                parts = line.split(':')
                if len(parts) >= 2 and parts[0] == syzid:
                    return parts[1]
    
    return None

def load_syz_commit(syzid: str) -> str:
    """
    Load syzkaller commit hash for a given syzid from SYZ_BRANCH_FILE.
    
    File format (syz_branch.txt):
        syzid1:syz_commit_hash1
        syzid2:syz_commit_hash2
        ...
    """
    if not os.path.exists(Config.SYZ_BRANCH_FILE):
        logger.warning(f"SYZ_BRANCH_FILE not found: {Config.SYZ_BRANCH_FILE}")
        return None
    
    with open(Config.SYZ_BRANCH_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if ':' in line:
                parts = line.split(':')
                if len(parts) >= 2 and parts[0] == syzid:
                    return parts[1]
    
    return None

def load_oracle_keyword(syzid: str) -> str:
    """
    Load oracle keyword for a given syzid from ORACLE_FILE.
    
    The oracle keyword is used to verify if the patch is successful
    by checking if it appears in the kernel log.
    
    File format (oracle_content.txt):
        syzid1:oracle_keyword1
        syzid2:oracle_keyword2
        ...
    """
    if not os.path.exists(Config.ORACLE_FILE):
        logger.warning(f"ORACLE_FILE not found: {Config.ORACLE_FILE}")
        return None
    
    with open(Config.ORACLE_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if ':' in line:
                parts = line.split(':')
                if len(parts) >= 2 and parts[0] == syzid:
                    return parts[1]
    
    return None

def load_bug_config(syzid: str) -> str:
    """
    Load kernel config for a given syzid.
    
    File path: {DATASETS_DIR}/bug_config/{syzid}.txt
    """
    config_path = Config.get_bug_config_path(syzid)
    if not os.path.exists(config_path):
        logger.warning(f"Bug config file not found: {config_path}")
        return None
    
    return read_file(config_path)

def load_task_queue_from_jsonl(jsonl_path: str, syzid: str) -> list:
    """
    Load task queue from JSONL file containing root cause analysis results.
    
    Expected format per line:
    {
        "case_id": "xxx",
        "hypotheses": [
            {"rank": 1, "function_name": "func", "hypothesis": "..."},
            ...
        ]
    }
    
    Note: This file only contains root cause hypotheses, not func_name/file_path.
    We need to combine with localization results from func_file_result.txt.
    """
    task_queue = []
    
    if not os.path.exists(jsonl_path):
        logger.warning(f"JSONL file not found: {jsonl_path}")
        return task_queue
    
    with open(jsonl_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            
            try:
                item = json.loads(line)
                case_id = item.get("case_id", "")
                if case_id == syzid:
                    hypotheses = item.get("hypotheses", [])
                    for h in hypotheses:
                        func_name = h.get("function_name", "")
                        if func_name:
                            task_queue.append({
                                "func_name": func_name,
                                "file_path": "",
                                "root_cause": h.get("hypothesis", "Unknown root cause")
                            })
                    if task_queue:
                        logger.info(f"   Found {len(task_queue)} hypotheses in JSONL for {syzid}")
                    return task_queue
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse JSONL line: {e}")
    
    return task_queue

def load_task_queue_from_loc_result(loc_file: str, rootcause_file: str, syzid: str) -> list:
    """
    Load task queue by combining localization results with root cause hypotheses.
    
    Step 1: Load localization results from func_file_result.txt
        Format:
            Response for case 283ce5a46486d6acdbaf 
             1. __alloc_file:fs/file_table.c 
             2. alloc_empty_file:fs/file_table.c 
             ...
    
    Step 2: Load root cause hypotheses from rootcause_byrank.jsonl
        Format per line:
            {"case_id": "a76f6a6e524cf2080aa3", "hypotheses": [
                {"rank": 1, "function_name": "fscache_alloc_volume", "hypothesis": "..."},
                {"rank": 2, "function_name": "another_func", "hypothesis": "..."}
            ]}
    
    Step 3: Match and merge - combine localization results with hypotheses
    """
    task_queue = []
    
    hypotheses_map = {}
    if os.path.exists(rootcause_file):
        with open(rootcause_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    case_id = item.get("case_id", "")
                    hypotheses = item.get("hypotheses", [])
                    if case_id and hypotheses:
                        hypotheses_map[case_id] = hypotheses
                except json.JSONDecodeError:
                    pass
    
    loc_results = []
    if not os.path.exists(loc_file):
        logger.warning(f"LOC_RESULT_FILE not found: {loc_file}")
        return task_queue
    
    logger.info(f"   Parsing localization file: {loc_file}")
    
    current_syzid = None
    found_target = False
    with open(loc_file, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            original_line = line
            line = line.strip()
            
            if line.startswith("Response for case"):
                parts = line.split()
                if len(parts) >= 4:
                    current_syzid = parts[3].rstrip(':')
                    if current_syzid == syzid:
                        found_target = True
                        logger.debug(f"   Found target syzid at line {line_num}: {current_syzid}")
            elif line and current_syzid == syzid:
                if line[0].isdigit() and '.' in line:
                    func_match = line.split('. ', 1)
                    if len(func_match) == 2:
                        func_info = func_match[1].split(':')
                        if len(func_info) == 2:
                            func_name = func_info[0].strip()
                            file_path = func_info[1].strip()
                            loc_results.append((func_name, file_path))
                            logger.debug(f"   Parsed: {func_name} @ {file_path}")
                        else:
                            logger.warning(f"   Line {line_num}: Failed to split func:file - {func_match[1]}")
                    else:
                        logger.warning(f"   Line {line_num}: Failed to split by '. ' - {line}")
    
    if not found_target:
        logger.warning(f"   Target syzid '{syzid}' not found in localization file")
    
    if not loc_results:
        logger.warning(f"No localization results found for syzid: {syzid}")
        return task_queue
    
    hypotheses = hypotheses_map.get(syzid, [])
    hypothesis_by_func = {}
    for h in hypotheses:
        func_name = h.get("function_name", "")
        if func_name:
            hypothesis_by_func[func_name] = h.get("hypothesis", "")
    
    for func_name, file_path in loc_results:
        root_cause = hypothesis_by_func.get(func_name, "")
        if not root_cause and hypotheses:
            root_cause = hypotheses[0].get("hypothesis", "") if hypotheses else "Unknown root cause"
        
        task_queue.append({
            "func_name": func_name,
            "file_path": file_path,
            "root_cause": root_cause or "Unknown root cause"
        })
    
    logger.info(f"   Loaded {len(loc_results)} localization results")
    logger.info(f"   Found {len(hypotheses)} hypotheses for {syzid}")
    logger.info(f"   Matched {len(hypothesis_by_func)} function-specific hypotheses")
    
    return task_queue

def run_fix_task(
    syzid: str,
    kernel_commit: str = None,
    syz_commit: str = None,
    bug_config_path: str = None,
    task_queue: list = None,
    max_functions: int = 10,
    recursion_limit: int = 50,
    no_golden_fixes: bool = False,
    no_historical_fixes: bool = False,
    function_combination: list = None
):
    """
    Main entry point for running the bug fix agent.
    
    All required data is automatically loaded based on syzid:
    - kernel_commit: from COMMIT_FILE
    - syz_commit: from SYZ_BRANCH_FILE
    - bug_config: from {DATASETS_DIR}/bug_config/{syzid}.txt
    - bug_report: from {DATASETS_DIR}/bug_report/{syzid}.txt
    - testcase: from {DATASETS_DIR}/testcases/{syzid}.syz
    - task_queue: from ROOT_CAUSE_JSONL or LOC_RESULT_FILE
    
    Args:
        syzid: Syzbot case ID (required)
        kernel_commit: Override auto-loaded commit (optional)
        syz_commit: Override auto-loaded syzkaller commit (optional)
        bug_config_path: Override auto-loaded config path (optional)
        task_queue: Override auto-loaded task queue (optional)
        max_functions: Maximum number of functions to try
        recursion_limit: Maximum graph iterations (default: 50)
    """
    switch_to_case_log(syzid)
    
    from agent.state import CaseSharedMemoryManager
    CaseSharedMemoryManager().reset()
    
    logger.info(f"🚀 Starting Bug Fix Task: {syzid}")
    
    if kernel_commit is None:
        kernel_commit = load_kernel_commit(syzid)
        if kernel_commit:
            logger.info(f"   Auto-loaded kernel commit: {kernel_commit}")
        else:
            logger.error(f"❌ Kernel commit not found for {syzid}")
            return None
    
    import subprocess
    kernel_dir = Config.KERNEL_DIR_ORI
    try:
        logger.info(f"   Checking out kernel to {kernel_commit[:8]}...")
        subprocess.run(
            ["git", "checkout", "-f", kernel_commit],
            cwd=kernel_dir,
            check=True,
            capture_output=True
        )
        logger.info(f"   ✅ Kernel checkout complete")
    except subprocess.CalledProcessError as e:
        logger.error(f"   ❌ Git checkout failed: {e.stderr.decode() if e.stderr else e}")
        return None
    except Exception as e:
        logger.error(f"   ❌ Git checkout error: {e}")
        return None
    
    if syz_commit is None:
        syz_commit = load_syz_commit(syzid)
        if syz_commit:
            logger.info(f"   Auto-loaded syz commit: {syz_commit}")
    
    if bug_config_path is None:
        bug_config_content = load_bug_config(syzid)
        if bug_config_content:
            bug_config_path = Config.get_bug_config_path(syzid)
            logger.info(f"   Auto-loaded bug config: {bug_config_path}")
    
    report_path = Config.get_bug_report_path(syzid)
    testcase_path = Config.get_testcase_path(syzid)
    
    bug_report = read_file(report_path) or "Bug report not found"
    testcase = read_file(testcase_path) or "Testcase not found"
    
    # 加载 oracle_keyword（所有模式都需要）
    oracle_keyword = load_oracle_keyword(syzid)
    
    # 当使用函数组合时，跳过定位结果的加载
    if function_combination:
        logger.info(f"📌 Using provided function combination mode")
        logger.info(f"   Functions: {[f['name'] for f in function_combination]}")
        # 创建一个简单的 task_queue（不会被使用，但需要满足函数签名要求）
        task_queue = [{
            "func_name": f['name'],
            "file_path": f['file'],
            "root_cause": "Provided function combination"
        } for f in function_combination]
        if oracle_keyword:
            logger.info(f"   Auto-loaded oracle keyword: {oracle_keyword}")
    elif task_queue is None:
        jsonl_path = Config.ROOT_CAUSE_JSONL
        loc_file = Config.LOC_RESULT_FILE
        
        task_queue = load_task_queue_from_loc_result(loc_file, jsonl_path, syzid)
        
        if not task_queue:
            logger.warning("   No results from loc_result file, trying JSONL only...")
            task_queue = load_task_queue_from_jsonl(jsonl_path, syzid)
        
        if not task_queue:
            logger.error(f"❌ No task queue found for {syzid}")
            logger.info("   Please provide task_queue parameter or ensure data files exist")
            return None
        
        task_queue = task_queue[:max_functions]
        
        if oracle_keyword:
            logger.info(f"   Auto-loaded oracle keyword: {oracle_keyword}")
        
        logger.info(f"📋 Task Queue: {len(task_queue)} functions")
        for i, task in enumerate(task_queue):
            logger.info(f"   {i+1}. {task['func_name']} @ {task['file_path']}")
    else:
        if oracle_keyword:
            logger.info(f"   Auto-loaded oracle keyword: {oracle_keyword}")
        
        logger.info(f"📋 Task Queue: {len(task_queue)} functions")
        for i, task in enumerate(task_queue):
            logger.info(f"   {i+1}. {task['func_name']} @ {task['file_path']}")
    
    final_state = run_fix_thread_agent(
        syzid=syzid,
        kernel_commit=kernel_commit,
        syz_commit=syz_commit,
        bug_config_path=bug_config_path,
        bug_report=bug_report,
        testcase=testcase,
        task_queue=task_queue,
        oracle_keyword=oracle_keyword or "",
        recursion_limit=recursion_limit,
        no_golden_fixes=no_golden_fixes,
        no_historical_fixes=no_historical_fixes,
        function_combination=function_combination
    )
    
    if final_state:
        output_result(final_state, syzid)
    
    cleanup_all_qemu_vms()
    
    return final_state

def output_result(final_state: dict, syzid: str):
    """Output the final result to files and console."""
    final_status = final_state.get("final_status", "UNKNOWN")
    success_patch = final_state.get("success_patch")
    tool_calls_log = final_state.get("tool_calls_log", [])
    all_generated_patches = final_state.get("all_generated_patches", [])
    token_stats = get_token_stats()
    
    log_separator(logger, "FINAL RESULT")
    logger.info(f"Status: {final_status}")
    
    if token_stats["total_requests"] > 0:
        log_separator(logger, "TOKEN STATISTICS")
        logger.info(f"  Total Requests: {token_stats['total_requests']}")
        logger.info(f"  Total Prompt Tokens: {token_stats['total_prompt_tokens']}")
        logger.info(f"  Total Completion Tokens: {token_stats['total_completion_tokens']}")
        logger.info(f"  Total Tokens: {token_stats['total_prompt_tokens'] + token_stats['total_completion_tokens']}")
        for model, stats in token_stats["by_model"].items():
            logger.info(f"  Model [{model}]:")
            logger.info(f"    - Requests: {stats['requests']}")
            logger.info(f"    - Prompt: {stats['prompt_tokens']}, Completion: {stats['completion_tokens']}")
    
    if tool_calls_log:
        logger.info(f"Tool Calls: {len(tool_calls_log)} total")
        unique_tools = list(dict.fromkeys(tool_calls_log))
        for tool in unique_tools:
            count = tool_calls_log.count(tool)
            logger.info(f"  • {tool}: {count}x")
    
    patch_dir = Config.PATCH_ARCHIVE_DIR
    syzid_dir = os.path.join(patch_dir, syzid)
    os.makedirs(syzid_dir, exist_ok=True)
    
    if all_generated_patches:
        logger.info(f"📦 Saving {len(all_generated_patches)} generated patches...")
        for i, patch in enumerate(all_generated_patches, 1):
            patch_file = os.path.join(syzid_dir, f"patch_{i:02d}_{patch.get('patch_id', 'unknown')}.c")
            with open(patch_file, 'w', encoding='utf-8') as f:
                f.write(f"/* Patch ID: {patch.get('patch_id', 'unknown')} */\n")
                f.write(f"/* Temperature: {patch.get('temperature', 0)} */\n")
                f.write(f"/* Generation Round: {patch.get('generation_round', 0)} */\n")
                f.write(f"/* Reasoning: {patch.get('reasoning', 'N/A')} */\n\n")
                f.write(patch.get('modified_code', ''))
        logger.info(f"   Saved to: {syzid_dir}")
    
    if final_status == "SUCCESS" and success_patch:
        logger.info(f"✅ BUG FIXED SUCCESSFULLY!")
        
        success_patches = final_state.get("success_patches", [])
        
        if success_patches:
            logger.info(f"   Number of successful patch combinations: {len(success_patches)}")
            
            for patch_idx, success_patch in enumerate(success_patches, 1):
                logger.info(f"\n{'='*60}")
                logger.info(f"   Patch Combination {patch_idx}")
                logger.info(f"   Patch ID: {success_patch.get('patch_id', 'N/A')}")
                
                patches = success_patch.get("patches", [])
                
                if patches:
                    logger.info(f"   Contains {len(patches)} function(s)")
                    
                    for i, p in enumerate(patches, 1):
                        func_name = p.get("function_name", "unknown")
                        file_path = p.get("file_path", "unknown")
                        modified_code = p.get("modified_code", "")
                        
                        logger.info(f"\n   [{i}/{len(patches)}] Function: {func_name}")
                        logger.info(f"       File: {file_path}")
                        log_separator(logger, f"PATCH CODE {patch_idx}-{i}")
                        logger.info(modified_code[:500] + "..." if len(modified_code) > 500 else modified_code)
                        
                        patch_file = os.path.join(syzid_dir, f"{syzid}_fixed_{patch_idx}_{i}_{func_name}.patch")
                        with open(patch_file, 'w', encoding='utf-8') as f:
                            f.write(f"/* Function: {func_name} */\n")
                            f.write(f"/* File: {file_path} */\n")
                            f.write(f"/* Patch Combination: {patch_idx} */\n")
                            f.write(f"/* Approach: {success_patch.get('approach', 'N/A')} */\n")
                            f.write(f"/* Reasoning: {success_patch.get('reasoning', 'N/A')} */\n\n")
                            
                            f.write(modified_code)
                        logger.info(f"   💾 Patch saved to: {patch_file}")
                    
                    combined_patch_file = os.path.join(syzid_dir, f"{syzid}_fixed_combined_{patch_idx}.patch")
                    with open(combined_patch_file, 'w', encoding='utf-8') as f:
                        f.write(f"/* Combined patch for {syzid} */\n")
                        f.write(f"/* Patch Combination: {patch_idx} */\n")
                        f.write(f"/* Functions: {', '.join([p.get('function_name', 'unknown') for p in patches])} */\n")
                        f.write(f"/* Approach: {success_patch.get('approach', 'N/A')} */\n")
                        f.write(f"/* Reasoning: {success_patch.get('reasoning', 'N/A')} */\n\n")
                        
                        for p in patches:
                            f.write(f"/* ====== Function: {p.get('function_name', 'unknown')} @ {p.get('file_path', 'unknown')} ====== */\n\n")
                            f.write(p.get("modified_code", ""))
                            f.write("\n\n")
                    logger.info(f"   💾 Combined patch saved to: {combined_patch_file}")
                else:
                    modified_code = success_patch.get("modified_code", "")
                    log_separator(logger, f"PATCH CODE {patch_idx}")
                    logger.info(modified_code)
                    
                    patch_file = os.path.join(syzid_dir, f"{syzid}_fixed_{patch_idx}.patch")
                    with open(patch_file, 'w', encoding='utf-8') as f:
                        f.write(f"/* Patch Combination: {patch_idx} */\n")
                        f.write(f"/* Approach: {success_patch.get('approach', 'N/A')} */\n")
                        f.write(f"/* Reasoning: {success_patch.get('reasoning', 'N/A')} */\n\n")
                        f.write(modified_code)
                    logger.info(f"💾 Patch saved to: {patch_file}")
            
            # 为了保持向后兼容，仍然处理第一个成功的补丁
            logger.info(f"\n{'='*60}")
            logger.info("   Using first successful patch for final output")
            logger.info(f"{'='*60}")
            
            success_patch = success_patches[0]
            patches = success_patch.get("patches", [])
            
            if patches:
                combined_patch_file = os.path.join(syzid_dir, f"{syzid}_fixed_combined.patch")
                with open(combined_patch_file, 'w', encoding='utf-8') as f:
                    f.write(f"/* Combined patch for {syzid} */\n")
                    f.write(f"/* Functions: {', '.join([p.get('function_name', 'unknown') for p in patches])} */\n")
                    f.write(f"/* Approach: {success_patch.get('approach', 'N/A')} */\n")
                    f.write(f"/* Reasoning: {success_patch.get('reasoning', 'N/A')} */\n\n")
                    
                    for p in patches:
                        f.write(f"/* ====== Function: {p.get('function_name', 'unknown')} @ {p.get('file_path', 'unknown')} ====== */\n\n")
                        f.write(p.get("modified_code", ""))
                        f.write("\n\n")
                logger.info(f"   💾 Final combined patch saved to: {combined_patch_file}")
            else:
                modified_code = success_patch.get("modified_code", "")
                patch_file = os.path.join(syzid_dir, f"{syzid}_fixed.patch")
                with open(patch_file, 'w', encoding='utf-8') as f:
                    f.write(modified_code)
                logger.info(f"💾 Final patch saved to: {patch_file}")
        else:
            # 向后兼容：如果没有 success_patches，使用 success_patch
            patches = success_patch.get("patches", [])
            
            if patches:
                logger.info(f"   Successful patch contains {len(patches)} function(s)")
                
                for i, p in enumerate(patches, 1):
                    func_name = p.get("function_name", "unknown")
                    file_path = p.get("file_path", "unknown")
                    modified_code = p.get("modified_code", "")
                    
                    logger.info(f"\n   [{i}/{len(patches)}] Function: {func_name}")
                    logger.info(f"       File: {file_path}")
                    log_separator(logger, f"PATCH CODE {i}")
                    logger.info(modified_code[:500] + "..." if len(modified_code) > 500 else modified_code)
                    
                    patch_file = os.path.join(syzid_dir, f"{syzid}_fixed_{i}_{func_name}.patch")
                    with open(patch_file, 'w', encoding='utf-8') as f:
                        f.write(f"/* Function: {func_name} */\n")
                        f.write(f"/* File: {file_path} */\n")
                        f.write(f"/* Approach: {success_patch.get('approach', 'N/A')} */\n")
                        f.write(f"/* Reasoning: {success_patch.get('reasoning', 'N/A')} */\n\n")
                        
                        f.write(modified_code)
                    logger.info(f"   💾 Patch saved to: {patch_file}")
                
                combined_patch_file = os.path.join(syzid_dir, f"{syzid}_fixed_combined.patch")
                with open(combined_patch_file, 'w', encoding='utf-8') as f:
                    f.write(f"/* Combined patch for {syzid} */\n")
                    f.write(f"/* Functions: {', '.join([p.get('function_name', 'unknown') for p in patches])} */\n")
                    f.write(f"/* Approach: {success_patch.get('approach', 'N/A')} */\n")
                    f.write(f"/* Reasoning: {success_patch.get('reasoning', 'N/A')} */\n\n")
                    
                    for p in patches:
                        f.write(f"/* ====== Function: {p.get('function_name', 'unknown')} @ {p.get('file_path', 'unknown')} ====== */\n\n")
                        f.write(p.get("modified_code", ""))
                        f.write("\n\n")
                logger.info(f"   💾 Combined patch saved to: {combined_patch_file}")
            else:
                modified_code = success_patch.get("modified_code", "")
                logger.info(f"Patch ID: {success_patch.get('patch_id', 'N/A')}")
                log_separator(logger, "PATCH CODE")
                logger.info(modified_code)
                
                patch_file = os.path.join(syzid_dir, f"{syzid}_fixed.patch")
                with open(patch_file, 'w', encoding='utf-8') as f:
                    f.write(modified_code)
                logger.info(f"💾 Final patch saved to: {patch_file}")
        
    else:
        logger.info(f"❌ BUG FIX FAILED")
        
        failures = final_state.get("failure_history", [])
        if failures:
            log_separator(logger, "FAILURE SUMMARY")
            for fail in failures:
                logger.info(f"  • Function: {fail['func_name']} @ {fail['file_path']}")
                logger.info(f"    Failure Node: {fail['failure_node']}")
                logger.info(f"    Retries: code={fail['code_retry_count']}, rc={fail['rc_retry_count']}")
        
        report_file = os.path.join(syzid_dir, f"{syzid}_failure_report.json")
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump({
                "syzid": syzid,
                "final_status": final_status,
                "failure_history": failures,
                "tool_calls_log": tool_calls_log,
                "all_generated_patches": [
                    {
                        "patch_id": p.get("patch_id"),
                        "temperature": p.get("temperature"),
                        "generation_round": p.get("generation_round"),
                        "reasoning": p.get("reasoning"),
                        "code_length": len(p.get("modified_code", ""))
                    }
                    for p in all_generated_patches
                ],
                "timestamp": datetime.now().isoformat()
            }, f, indent=2, ensure_ascii=False)
        logger.info(f"📄 Failure report saved to: {report_file}")

def run_batch_fix_tasks(
    batch_file: str,
    output_file: str = None,
    max_functions: int = 10,
    recursion_limit: int = 50,
    no_golden_fixes: bool = False,
    no_historical_fixes: bool = False,
    function_combination_file: str = None
):
    """
    Run bug fix tasks for multiple syzids from a batch file.
    
    Args:
        batch_file: Path to file containing syzids (one per line)
        output_file: Path to output results file (default: batch_results_{timestamp}.json)
        max_functions: Maximum number of functions to try per case
        recursion_limit: Maximum graph iterations
    """
    if not os.path.exists(batch_file):
        logger.error(f"❌ Batch file not found: {batch_file}")
        return
    
    with open(batch_file, 'r', encoding='utf-8') as f:
        syzids = [line.strip() for line in f if line.strip()]
    
    if not syzids:
        logger.error(f"❌ No syzids found in batch file: {batch_file}")
        return
    
    # 读取函数组合文件
    function_combinations = {}
    if function_combination_file:
        try:
            with open(function_combination_file, 'r', encoding='utf-8') as f:
                function_combinations = json.load(f)
            logger.info(f"✅ Loaded function combinations from: {function_combination_file}")
        except Exception as e:
            logger.error(f"❌ Failed to load function combination file: {e}")
            function_combinations = {}
    
    total_cases = len(syzids)
    logger.info(f"\n{'='*60}")
    logger.info(f"🚀 Starting Batch Bug Fix Task")
    logger.info(f"   Total cases: {total_cases}")
    logger.info(f"{'='*60}\n")
    
    if output_file is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(Config.PATCH_ARCHIVE_DIR, f"batch_results_{timestamp}.json")
    
    results = []
    success_count = 0
    total_tokens = {
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_requests": 0
    }
    batch_start_time = time.time()

    for i, syzid in enumerate(syzids, 1):
        case_start_time = time.time()
        logger.info(f"\n{'='*60}")
        logger.info(f"📋 Progress: {i}/{total_cases} ({i*100//total_cases}%)")
        logger.info(f"   Current: {syzid}")
        logger.info(f"   Success so far: {success_count}/{i-1}")
        logger.info(f"{'='*60}\n")
        
        reset_token_stats()
        
        try:
            # 获取当前syzid的函数组合
            function_combination = None
            if syzid in function_combinations:
                func_info = function_combinations[syzid]
                function_combination = []
                for func in func_info.get('functions', []):
                    function_combination.append({
                        'name': func['func_name'],
                        'file': func['file_path']
                    })
                logger.info(f"   Using function combination for {syzid}: {[f['name'] for f in function_combination]}")
            
            final_state = run_fix_task(
                syzid=syzid,
                max_functions=max_functions,
                recursion_limit=recursion_limit,
                no_golden_fixes=no_golden_fixes,
                no_historical_fixes=no_historical_fixes,
                function_combination=function_combination
            )
            
            if final_state:
                status = final_state.get("final_status", "UNKNOWN")
                if status == "SUCCESS":
                    success_count += 1
            else:
                status = "ERROR"
            
            token_stats = get_token_stats()
            total_tokens["total_prompt_tokens"] += token_stats["total_prompt_tokens"]
            total_tokens["total_completion_tokens"] += token_stats["total_completion_tokens"]
            total_tokens["total_requests"] += token_stats["total_requests"]
            
            case_duration = time.time() - case_start_time
            result = {
                "syzid": syzid,
                "status": status,
                "duration_seconds": round(case_duration, 2),
                "duration_hms": format_duration(case_duration),
                "tokens": {
                    "prompt": token_stats["total_prompt_tokens"],
                    "completion": token_stats["total_completion_tokens"],
                    "total": token_stats["total_prompt_tokens"] + token_stats["total_completion_tokens"]
                }
            }
            
            if final_state and final_state.get("success_patch"):
                result["patch_id"] = final_state["success_patch"].get("patch_id")
                if "fix_threads" in final_state:
                    for thread in final_state["fix_threads"]:
                        if thread.get("status") == "SUCCESS":
                            selected_functions = thread.get("selected_functions", [])
                            if selected_functions:
                                result["success_functions"] = [
                                    f"{func['name']} @ {func['file']}"
                                    for func in selected_functions
                                ]
                            break
            
            results.append(result)
            
            logger.info(f"\n📊 Case Summary: {syzid}")
            logger.info(f"   Status: {status}")
            logger.info(f"   Duration: {result['duration_hms']} ({result['duration_seconds']}s)")
            logger.info(f"   Tokens: prompt={token_stats['total_prompt_tokens']}, completion={token_stats['total_completion_tokens']}")
            logger.info(f"   Running Total: success={success_count}/{i}")

        except Exception as e:
            logger.error(f"❌ Error processing {syzid}: {e}")
            case_duration = time.time() - case_start_time
            results.append({
                "syzid": syzid,
                "status": "ERROR",
                "error": str(e),
                "duration_seconds": round(case_duration, 2),
                "duration_hms": format_duration(case_duration),
                "tokens": {"prompt": 0, "completion": 0, "total": 0}
            })

        cleanup_all_qemu_vms()
        elapsed_total = time.time() - batch_start_time

        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump({
                "batch_file": batch_file,
                "total_cases": total_cases,
                "processed": i,
                "success_count": success_count,
                "total_tokens": total_tokens,
                "total_duration_seconds": round(elapsed_total, 2),
                "total_duration_hms": format_duration(elapsed_total),
                "results": results,
                "timestamp": datetime.now().isoformat()
            }, f, indent=2, ensure_ascii=False)
    
    logger.info(f"\n{'='*60}")
    logger.info(f"🏁 BATCH COMPLETE")
    logger.info(f"{'='*60}")
    logger.info(f"   Total cases: {total_cases}")
    logger.info(f"   Success: {success_count}")
    logger.info(f"   Failed: {total_cases - success_count}")
    logger.info(f"   Success rate: {success_count*100//total_cases}%")
    total_duration = time.time() - batch_start_time
    logger.info(f"   Total duration: {format_duration(total_duration)} ({round(total_duration, 2)}s)")
    logger.info(f"\n📊 TOTAL TOKENS:")
    logger.info(f"   Prompt: {total_tokens['total_prompt_tokens']}")
    logger.info(f"   Completion: {total_tokens['total_completion_tokens']}")
    logger.info(f"   Total: {total_tokens['total_prompt_tokens'] + total_tokens['total_completion_tokens']}")
    logger.info(f"\n📄 Results saved to: {output_file}")
    logger.info(f"{'='*60}\n")
    
    return results

def main():
    parser = argparse.ArgumentParser(
        description="Linux Kernel Bug Fix Agent - Parallel Fix Thread Workflow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single case
  python main.py --syzid 283ce5a46486d6acdbaf
  
  # Batch mode - process multiple cases from file
  python main.py --batch cases.txt --output results.json
  
  # Batch mode with custom settings
  python main.py --batch cases.txt --max-functions 5
  
  # Disable query_golden_fixes retrieval
  python main.py --syzid 283ce5a46486d6acdbaf --no-golden-fixes

  # Disable search_historical_fixes retrieval
  python main.py --syzid 283ce5a46486d6acdbaf --no-historical-fixes

Architecture:
  The agent uses parallel fix threads with closed-loop logic:
  
  1. Planner: Generates candidate hypotheses (function combinations + root causes)
  2. Fix Threads: Each thread runs independently with complete cycle:
     - Generator: Creates patches using LLM + tools
     - Reviewer: Evaluates patch quality
     - Validator: Tests patch in QEMU sandbox
     - Judge: Decides next action (tweak code / revise root cause / abandon)
  3. Shared Memory: Threads share failure history to avoid duplicate mistakes
  4. Limits: MAX_TOTAL_ATTEMPTS=8, MAX_THREAD_ATTEMPTS=4
        """
    )
    
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--syzid", help="Single Syzbot case ID")
    group.add_argument("--batch", help="Path to file containing syzids (one per line)")
    
    parser.add_argument("--commit", default=None, help="Override kernel commit hash")
    parser.add_argument("--syz-commit", default=None, help="Override syzkaller commit")
    parser.add_argument("--config", default=None, help="Override bug config path")
    parser.add_argument("--jsonl", default=None, help="Override task queue JSONL file")
    parser.add_argument("--max-functions", type=int, default=10, help="Max functions to try")
    parser.add_argument("--recursion-limit", type=int, default=500, help="Max graph iterations")
    parser.add_argument("--output", default=None, help="Output file for batch results")
    parser.add_argument("--no-golden-fixes", action="store_true", help="Disable golden fixes retrieval")
    parser.add_argument("--no-historical-fixes", action="store_true", help="Disable historical similar-fix retrieval")
    parser.add_argument("--function-combination-file", type=str, help="Path to JSON file containing function combinations for each syzid")
    
    args = parser.parse_args()
    
    # 处理函数组合文件
    function_combination = None
    if args.function_combination_file and args.syzid:
        # 只在非批量模式下处理函数组合
        try:
            with open(args.function_combination_file, 'r', encoding='utf-8') as f:
                function_combinations = json.load(f)
            if args.syzid in function_combinations:
                func_info = function_combinations[args.syzid]
                function_combination = []
                for func in func_info.get('functions', []):
                    function_combination.append({
                        'name': func['func_name'],
                        'file': func['file_path']
                    })
                logger.info(f"✅ Using function combination for {args.syzid}: {[f['name'] for f in function_combination]}")
            else:
                logger.warning(f"⚠️ No function combination found for {args.syzid} in {args.function_combination_file}")
        except Exception as e:
            logger.error(f"❌ Failed to load function combination file: {e}")
    
    if args.batch:
        run_batch_fix_tasks(
            batch_file=args.batch,
            output_file=args.output,
            max_functions=args.max_functions,
            recursion_limit=args.recursion_limit,
            no_golden_fixes=args.no_golden_fixes,
            no_historical_fixes=args.no_historical_fixes,
            function_combination_file=args.function_combination_file
        )
    else:
        task_queue = None
        if args.jsonl:
            task_queue = load_task_queue_from_jsonl(args.jsonl, args.syzid)
        
        run_fix_task(
            syzid=args.syzid,
            kernel_commit=args.commit,
            syz_commit=args.syz_commit,
            bug_config_path=args.config,
            task_queue=task_queue,
            max_functions=args.max_functions,
            recursion_limit=args.recursion_limit,
            no_golden_fixes=args.no_golden_fixes,
            no_historical_fixes=args.no_historical_fixes,
            function_combination=function_combination
        )

if __name__ == "__main__":
    main()

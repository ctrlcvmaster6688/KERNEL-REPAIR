from langchain_core.tools import tool
from typing import Dict, List, Optional
import sys
import os
import threading
import subprocess
from collections import Counter

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.struct_rag import StructuralContextExtractor
from core.Hybrid_patch_rag import KernelKnowledgeRetriever
from config import Config

_imported_modules = {}

def _get_logger():
    if "logger" not in _imported_modules:
        from agent.log_manager import get_logger
        _imported_modules["logger"] = get_logger("Tools")
    return _imported_modules["logger"]

logger = None

def _ensure_logger():
    global logger
    if logger is None:
        logger = _get_logger()
    return logger

_context_extractor: Optional[StructuralContextExtractor] = None
_kernel_fix_expert = None
_kernel_fix_expert_lock = threading.Lock()
_commit_timestamp_cache = {}
_commit_timestamp_lock = threading.Lock()

def _get_context_extractor() -> StructuralContextExtractor:
    global _context_extractor
    if _context_extractor is None:
        _context_extractor = StructuralContextExtractor(
            kernel_root_path=Config.KERNEL_DIR_ORI,
            language_so_path=Config.LANG_SO_PATH
        )
    return _context_extractor

def _get_knowledge_retriever() -> KernelKnowledgeRetriever:
    """创建新的 KernelKnowledgeRetriever 实例"""
    retriever = KernelKnowledgeRetriever()
    retriever.load_resources()
    return retriever


def _get_commit_timestamp(commit_hash: str) -> Optional[str]:
    """Resolve a kernel commit hash to its committer timestamp in ISO-8601 format."""
    if not commit_hash:
        return None

    with _commit_timestamp_lock:
        if commit_hash in _commit_timestamp_cache:
            return _commit_timestamp_cache[commit_hash]

    try:
        output = subprocess.check_output(
            ["git", "-C", Config.KERNEL_DIR_ORI, "show", "-s", "--format=%cI", commit_hash],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        resolved = output or None
    except Exception:
        resolved = None

    with _commit_timestamp_lock:
        _commit_timestamp_cache[commit_hash] = resolved

    return resolved

@tool
def get_function_context(file_path: str, func_name: str) -> Dict:
    """
    Extract structured context for a target function using Tree-sitter AST parsing.
    
    This is a READ-ONLY, stateless function call that returns:
    - Target function code
    - Related struct definitions
    - Related macro definitions
    - Similar functions in the same file
    
    Args:
        file_path: Relative path to the source file (e.g., "kernel/bpf/devmap.c")
        func_name: Name of the target function to extract
        
    Returns:
        Dict containing target_code, start_line, structs, macros, similar_funcs
    """
    log = _ensure_logger()
    log.info(f"📖 [Tool] get_function_context: {func_name} @ {file_path}")
    
    try:
        extractor = _get_context_extractor()
        result = extractor.extract(file_path, func_name)
        
        if not result or not result.get("target_code"):
            return {
                "success": False,
                "error": f"Function '{func_name}' not found in {file_path}",
                "target_code": None,
                "structs": {},
                "macros": {},
                "similar_funcs": []
            }
        
        log.info(f"✅ Extracted function context: {len(result['target_code'])} chars, "
                   f"{len(result['structs'])} structs, {len(result['macros'])} macros")
        
        return {
            "success": True,
            "error": None,
            **result
        }
        
    except Exception as e:
        log.error(f"❌ get_function_context failed: {e}")
        return {
            "success": False,
            "error": str(e),
            "target_code": None,
            "structs": {},
            "macros": {},
            "similar_funcs": []
        }

@tool
def get_function_body(file_path: str, func_name: str) -> Dict:
    """
    Extract only the function body for a target function.
    
    This is a READ-ONLY, stateless function call that returns:
    - Target function code
    
    Args:
        file_path: Relative path to the source file (e.g., "kernel/bpf/devmap.c")
        func_name: Name of the target function to extract
        
    Returns:
        Dict containing target_code
    """
    log = _ensure_logger()
    log.info(f"📖 [Tool] get_function_body: {func_name} @ {file_path}")
    
    try:
        extractor = _get_context_extractor()
        result = extractor.extract(file_path, func_name)
        
        if not result or not result.get("target_code"):
            return {
                "success": False,
                "error": f"Function '{func_name}' not found in {file_path}",
                "target_code": None
            }
        
        log.info(f"✅ Extracted function body: {len(result['target_code'])} chars")
        
        return {
            "success": True,
            "error": None,
            "target_code": result["target_code"],
            "start_line": result.get("start_line")
        }
        
    except Exception as e:
        log.error(f"❌ get_function_body failed: {e}")
        return {
            "success": False,
            "error": str(e),
            "target_code": None
        }

@tool
def get_function_related_info(file_path: str, func_name: str) -> Dict:
    """
    Extract related information for a target function.
    
    This is a READ-ONLY, stateless function call that returns:
    - Related struct definitions
    - Related macro definitions
    - Similar functions in the same file
    
    Args:
        file_path: Relative path to the source file (e.g., "kernel/bpf/devmap.c")
        func_name: Name of the target function to extract
        
    Returns:
        Dict containing structs, macros, similar_funcs
    """
    log = _ensure_logger()
    log.info(f"📖 [Tool] get_function_related_info: {func_name} @ {file_path}")
    
    try:
        extractor = _get_context_extractor()
        result = extractor.extract(file_path, func_name)
        
        if not result:
            return {
                "success": False,
                "error": f"Function '{func_name}' not found in {file_path}",
                "structs": {},
                "macros": {},
                "similar_funcs": []
            }
        
        log.info(f"✅ Extracted related info: {len(result.get('structs', {}))} structs, "
                   f"{len(result.get('macros', {}))} macros")
        
        return {
            "success": True,
            "error": None,
            "structs": result.get("structs", {}),
            "macros": result.get("macros", {}),
            "similar_funcs": result.get("similar_funcs", [])
        }
        
    except Exception as e:
        log.error(f"❌ get_function_related_info failed: {e}")
        return {
            "success": False,
            "error": str(e),
            "structs": {},
            "macros": {},
            "similar_funcs": []
        }

@tool
def search_historical_fixes(
    syzid: str,
    bug_report: str,
    testcase: str = "",
    rootcause: str = "",
    strict_time_filter: bool = False
) -> Dict:
    """
    Search for historically similar kernel bug fixes using multi-modal structured semantic alignment.
    
    This is a READ-ONLY, stateless function call that uses:
    - Multi-modal dense retrieval (semantic similarity via embeddings)
      * desc (0.3): Bug description semantic intent
      * trace (0.3): Execution path evolution trend
      * rootcause (0.4): Core logical alignment
    - Structured sparse retrieval (BM25 keyword matching)
      * Using cleaned Description + Trace + Testcase
    - Physical fingerprint sequence validation
      * Weighted trace alignment (Levenshtein distance)
      * Testcase syscall sequence LCS
    - Two-phase fusion scoring
      * RRF (Reciprocal Rank Fusion) for recall
      * Weighted re-ranking for final selection
    
    Args:
        syzid: The current bug's Syzbot ID (used for exclusion to prevent data leakage)
        bug_report: The crash report / stack trace text
        testcase: Optional Syzkaller testcase text
        rootcause: Optional root cause analysis text
        strict_time_filter: If True, only return fixes from before this bug's timestamp
        
    Returns:
        Dict containing formatted knowledge text and raw case list with similarity scores
    """

    import os
    import json
    import time
    import multiprocessing
    from datetime import datetime
    from config import Config

    log = _ensure_logger()
    
    # 准备日志数据
    log_data = {
        "tool": "search_historical_fixes",
        "timestamp": datetime.now().isoformat(),
        "input": {
            "syzid": syzid,
            "bug_report": bug_report[:1000] + "..." if len(bug_report) > 1000 else bug_report,
            "testcase": testcase[:500] + "..." if len(testcase) > 500 else testcase,
            "rootcause": rootcause[:500] + "..." if len(rootcause) > 500 else rootcause,
            "strict_time_filter": strict_time_filter
        }
    }
    
    # 定义子进程函数
    def worker(syzid, bug_report, testcase, rootcause, strict_time_filter, return_dict):
        """子进程函数，执行搜索操作"""
        try:
            retriever = _get_knowledge_retriever()
            
            # 不需要超时检查，因为主进程会强制终止超时的子进程
            cases = retriever.search_knowledge(
                ref_syz_id=syzid,
                report_text=bug_report,
                testcase_text=testcase,
                rootcause_text=rootcause,
                strict_time=strict_time_filter,
                timeout_check=None  # 不使用超时检查，由主进程控制
            )
            
            knowledge_text = retriever.format_knowledge_as_text(cases, timeout_check=None)
            
            return_dict["success"] = True
            return_dict["error"] = None
            return_dict["num_cases"] = len(cases)
            return_dict["knowledge_text"] = knowledge_text
            return_dict["cases"] = cases
        except Exception as e:
            return_dict["success"] = False
            return_dict["error"] = str(e)
            return_dict["num_cases"] = 0
            return_dict["knowledge_text"] = "Failed to retrieve historical fixes."
            return_dict["cases"] = []
        finally:
            if 'retriever' in locals():
                retriever.close()
    
    try:
        # 创建进程间通信的字典
        manager = multiprocessing.Manager()
        return_dict = manager.dict()
        
        # 创建子进程
        p = multiprocessing.Process(
            target=worker,
            args=(syzid, bug_report, testcase, rootcause, strict_time_filter, return_dict)
        )
        
        # 启动子进程
        log.info(f"🚀 [Tool] search_historical_fixes: 启动子进程处理 syzid={syzid}")
        start_time = time.time()
        p.start()
        
        # 等待子进程完成，设置超时时间为60秒
        p.join(timeout=60)
        
        # 检查子进程是否超时
        if p.is_alive():
            log.error(f"⚠️ search_historical_fixes 执行超时，强制终止子进程")
            p.terminate()
            p.join(timeout=5)  # 等待子进程被终止
            if p.is_alive():
                log.error(f"⚠️ 子进程终止失败，强制杀死")
                p.kill()
            
            result = {
                "success": False,
                "error": "Operation timed out after 60 seconds",
                "num_cases": 0,
                "knowledge_text": "Failed to retrieve historical fixes due to timeout.",
                "cases": []
            }
        else:
            # 子进程正常完成，获取结果
            result = dict(return_dict)
            # 确保结果包含所有必要的键
            if "success" not in result:
                result = {
                    "success": False,
                    "error": "Unknown error occurred",
                    "num_cases": 0,
                    "knowledge_text": "Failed to retrieve historical fixes.",
                    "cases": []
                }
    except Exception as e:
        log.error(f"❌ search_historical_fixes failed: {e}")
        result = {
            "success": False,
            "error": str(e),
            "num_cases": 0,
            "knowledge_text": "Failed to retrieve historical fixes.",
            "cases": []
        }
    
    # 添加输出到日志数据
    if result["success"]:
        cases = result["cases"]
        serialized_cases = []
        for case in cases:
            serialized_case = case.copy()
            if "similarity" in serialized_case and isinstance(serialized_case["similarity"], float):
                serialized_case["similarity"] = round(serialized_case["similarity"], 4)
            if "stack_similarity" in serialized_case and isinstance(serialized_case["stack_similarity"], float):
                serialized_case["stack_similarity"] = round(serialized_case["stack_similarity"], 4)
            if "tc_similarity" in serialized_case and isinstance(serialized_case["tc_similarity"], float):
                serialized_case["tc_similarity"] = round(serialized_case["tc_similarity"], 4)
            if "final_score" in serialized_case and isinstance(serialized_case["final_score"], float):
                serialized_case["final_score"] = round(serialized_case["final_score"], 4)
            if "patch" in serialized_case and len(serialized_case["patch"]) > 500:
                serialized_case["patch"] = serialized_case["patch"][:500] + "..."
            serialized_cases.append(serialized_case)
        
        log_data["output"] = {
            "success": result["success"],
            "num_cases": result["num_cases"],
            "knowledge_text": result["knowledge_text"][:1000] + "..." if len(result["knowledge_text"]) > 1000 else result["knowledge_text"],
            "cases_count": len(cases),
            "cases": serialized_cases
        }
        log.info(f"✅ Retrieved {len(cases)} historical cases")
    else:
        log_data["output"] = {
            "success": result["success"],
            "error": result["error"]
        }
    
    # 保存日志到文件
    try:
        log_dir = os.path.join(Config.PATCH_ARCHIVE_DIR, "retrieval_logs")
        os.makedirs(log_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"search_historical_fixes_{syzid}_{timestamp}.json"
        log_path = os.path.join(log_dir, filename)
        
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(log_data, f, ensure_ascii=False, indent=2)
        
        log.info(f"📝 Saved retrieval log to {log_path}")
    except Exception as e:
        log.error(f"❌ Failed to save retrieval log: {e}")
    
    return result

@tool
def read_kernel_file(file_path: str, start_line: int = 1, end_line: int = 500) -> Dict:
    """
    Read a kernel source file content with optional line pagination.
    
    This is a READ-ONLY function call for examining specific files.
    
    Args:
        file_path: Relative path from kernel root (e.g., "include/linux/slab.h")
        start_line: First line to read (inclusive, default: 1)
        end_line: Last line to read (inclusive, default: 500)
        
    Returns:
        Dict with file content or error message
    """
    log = _ensure_logger()
    if start_line > 1 or end_line != 500:
        log.info(f"📄 [Tool] read_kernel_file: {file_path} (lines {start_line}-{end_line})")
    else:
        log.info(f"📄 [Tool] read_kernel_file: {file_path}")
    
    try:
        full_path = os.path.join(Config.KERNEL_DIR_ORI, file_path)
        
        if not os.path.exists(full_path):
            return {
                "success": False,
                "error": f"File not found: {file_path}",
                "content": None,
                "file_path": file_path,
                "start_line": start_line,
                "end_line": end_line
            }
        
        with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        
        # 处理行号范围
        start_idx = max(0, start_line - 1)
        end_idx = min(len(lines), end_line)
        
        # 构建包含行号的内容
        selected_lines = []
        for i in range(start_idx, end_idx):
            line_num = i + 1
            line_content = lines[i]
            # 去掉末尾的换行符，手动加上，方便对齐
            line_content = line_content.rstrip('\n')
            selected_lines.append(f"{line_num:6d} | {line_content}")
        
        content = "\n".join(selected_lines)
        
        # 限制总字符数
        max_chars = 50000
        if len(content) > max_chars:
            content = content[:max_chars] + "\n... [TRUNCATED - use smaller line range]"
        
        log.info(f"✅ Read file: {len(content)} chars, lines {start_line}-{end_idx}")
        
        return {
            "success": True,
            "error": None,
            "content": content,
            "file_path": file_path,
            "start_line": start_line,
            "end_line": end_idx,
            "total_lines": len(lines)
        }
        
    except Exception as e:
        log.error(f"❌ read_kernel_file failed: {e}")
        return {
            "success": False,
            "error": str(e),
            "content": None,
            "file_path": file_path,
            "start_line": start_line,
            "end_line": end_line
        }

@tool
def get_bug_info(syzid: str) -> Dict:
    """
    Get comprehensive bug information for a given Syzbot ID.
    
    This aggregates bug report, testcase, and any available metadata.
    
    Args:
        syzid: The Syzbot case ID
        
    Returns:
        Dict with bug report, testcase, and metadata
    """
    log = _ensure_logger()
    log.info(f"🐛 [Tool] get_bug_info: {syzid}")
    
    from utils.file_ops import read_file
    
    try:
        report_path = os.path.join(Config.DATASETS_DIR, "bug_report", f"{syzid}.txt")
        testcase_path = os.path.join(Config.DATASETS_DIR, "testcases", f"{syzid}.syz")
        
        bug_report = read_file(report_path) or "Bug report not found"
        testcase = read_file(testcase_path) or "Testcase not found"
        
        return {
            "success": True,
            "syzid": syzid,
            "bug_report": bug_report,
            "testcase": testcase
        }
        
    except Exception as e:
        log.error(f"❌ get_bug_info failed: {e}")
        return {
            "success": False,
            "error": str(e),
            "syzid": syzid,
            "bug_report": "",
            "testcase": ""
        }

_kernel_fix_expert_lock = threading.Lock()


def _get_kernel_fix_expert():
    """Get or create the KernelFixExpert singleton."""
    global _kernel_fix_expert
    
    if _kernel_fix_expert is not None:
        return _kernel_fix_expert
    
    with _kernel_fix_expert_lock:
        if _kernel_fix_expert is None:
            try:
                from skill.kernel_fix_expert import KernelFixExpert
                _kernel_fix_expert = KernelFixExpert()
            except Exception as e:
                log = _ensure_logger()
                log.error(f"❌ Failed to init KernelFixExpert: {e}")
                return None
        return _kernel_fix_expert


@tool
def query_golden_fixes(
    buggy_code: str,
    description: str,
    file_path: str,
    syzid: str = "",
    kernel_commit: str = "",
) -> Dict:
    """
    Query historical golden fix patches for reference implementation patterns.
    
    This tool retrieves Top 3 most relevant historical fix patches based on:
    - Intent modeling (5-point structure: Context + Operation + Entity + Location + Goal)
    - Subsystem and core API extraction
    - Vector similarity matching (intent + code)
    - Structural ranking (AST + subsystem + API matching)
    - RRF fusion for final ranking
    
    Use this tool when you want to learn syntax patterns and implementation structures
    for specific types of code modifications, or when you need reference examples
    for how to implement certain fix patterns.
    
    Args:
        buggy_code: The code snippet to modify (should include function signature)
        description: Natural language description of what modification you want to make to the code
        file_path: The file path where the code is located (for subsystem identification)
        syzid: Optional Syzbot ID of the current bug (for logging purposes)
        kernel_commit: Current kernel commit hash used as the retrieval time cutoff
        
    Returns:
        Dict containing:
        - status: "success" or "error"
        - intent_summary: The generated intent description (5-point structure)
        - subsystem: The identified kernel subsystem
        - core_apis: Extracted core APIs from the code
        - results: List of Top 3 golden patches with rank, commit_message, diff_format, rrf_score, and various similarity scores
    """

    import os
    import json
    from datetime import datetime
    from config import Config

    log = _ensure_logger()
    log.info(f"🔧 [Tool] query_golden_fixes: file={file_path}")
    
    # 准备日志数据
    log_data = {
        "tool": "query_golden_fixes",
        "timestamp": datetime.now().isoformat(),
        "input": {
            "buggy_code": buggy_code[:1000] + "..." if len(buggy_code) > 1000 else buggy_code,
            "description": description[:500] + "..." if len(description) > 500 else description,
            "file_path": file_path,
            "kernel_commit": kernel_commit,
        }
    }
    
    try:
        expert = _get_kernel_fix_expert()
        
        if expert is None:
            result = {
                "status": "error",
                "message": "KernelFixExpert not initialized",
                "results": []
            }
        else:
            cutoff_author_date = _get_commit_timestamp(kernel_commit) if kernel_commit else None
            result = expert.run_sync(
                buggy_code,
                description,
                file_path,
                cutoff_author_date=cutoff_author_date,
            )
            if kernel_commit and cutoff_author_date:
                result["cutoff_author_date"] = cutoff_author_date
            
            if result.get("status") == "success":
                log.info(f"✅ Found {len(result.get('results', []))} golden fixes for intent: {result.get('intent_summary', '')[:100]}...")
            else:
                log.warning(f"⚠️ No golden fixes found: {result.get('message')}")
        
        # 添加输出到日志数据
        # 处理 results 数据，确保可序列化
        serialized_results = []
        for res in result.get("results", []):
            serialized_res = res.copy()
            # 截断长字符串
            if "diff_format" in serialized_res and len(serialized_res["diff_format"]) > 500:
                serialized_res["diff_format"] = serialized_res["diff_format"][:500] + "..."
            if "commit_message" in serialized_res and len(serialized_res["commit_message"]) > 200:
                serialized_res["commit_message"] = serialized_res["commit_message"][:200] + "..."
            # 四舍五入浮点数
            if "rrf_score" in serialized_res and isinstance(serialized_res["rrf_score"], float):
                serialized_res["rrf_score"] = round(serialized_res["rrf_score"], 4)
            if "intent_similarity" in serialized_res and isinstance(serialized_res["intent_similarity"], float):
                serialized_res["intent_similarity"] = round(serialized_res["intent_similarity"], 4)
            if "struct_score" in serialized_res and isinstance(serialized_res["struct_score"], float):
                serialized_res["struct_score"] = round(serialized_res["struct_score"], 4)
            if "ast_score" in serialized_res and isinstance(serialized_res["ast_score"], float):
                serialized_res["ast_score"] = round(serialized_res["ast_score"], 4)
            if "sub_score" in serialized_res and isinstance(serialized_res["sub_score"], float):
                serialized_res["sub_score"] = round(serialized_res["sub_score"], 4)
            if "api_score" in serialized_res and isinstance(serialized_res["api_score"], float):
                serialized_res["api_score"] = round(serialized_res["api_score"], 4)
            serialized_results.append(serialized_res)
        
        log_data["output"] = {
            "status": result.get("status"),
            "message": result.get("message"),
            "intent_summary": result.get("intent_summary"),
            "subsystem": result.get("subsystem"),
            "core_apis": result.get("core_apis"),
            "cutoff_author_date": result.get("cutoff_author_date"),
            "results_count": len(result.get("results", [])),
            "results": serialized_results
        }
        
    except Exception as e:
        log.error(f"❌ query_golden_fixes failed: {e}")
        result = {
            "status": "error",
            "message": str(e),
            "results": []
        }
        # 添加错误到日志数据
        log_data["output"] = {
            "status": result["status"],
            "message": result["message"]
        }
    
    # 保存日志到文件
    try:
        log_dir = os.path.join(Config.PATCH_ARCHIVE_DIR, "retrieval_logs")
        os.makedirs(log_dir, exist_ok=True)
        
        # 生成唯一的文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"query_golden_fixes_{syzid}_{timestamp}.json"
        log_path = os.path.join(log_dir, filename)
        
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(log_data, f, ensure_ascii=False, indent=2)
        
        log.info(f"📝 Saved retrieval log to {log_path}")
    except Exception as e:
        log.error(f"❌ Failed to save retrieval log: {e}")
    
    return result

@tool
def get_related_files(
    func_name: str,
    file_path: str
) -> Dict:
    """
    Query git history to find files that are frequently modified together with the given function.
    ...
    """
    
    log = _ensure_logger()
    log.info(f"🔍 [Tool] get_related_files: {func_name} @ {file_path}")
    
    try:
        full_path = os.path.join(Config.KERNEL_DIR_ORI, file_path)
        if not os.path.exists(full_path):
            return {
                "success": False, "error": f"File not found: {file_path}",
                "related_files": [], "commit_count": 0
            }
        
        # ==========================================
        # Step 1: 精准找到相关的 Commit Hashes
        # 使用 -S 搜索代码 Diff，比 --grep 更严谨，且限定必须修改了 file_path
        # ==========================================
        git_find_commits_cmd = [
            "git", "log", "--format=%H", f"-S{func_name}", "--", file_path
        ]
        
        commits_result = subprocess.run(
            git_find_commits_cmd,
            cwd=Config.KERNEL_DIR_ORI,
            capture_output=True,
            text=True,
            timeout=15,
            check=True
        )
        
        commit_hashes = commits_result.stdout.strip().splitlines()
        commit_count = len(commit_hashes)
        
        if commit_count == 0:
            log.info("ℹ️ No commits found matching the criteria.")
            return {
                "success": True, "error": None,
                "related_files": [], "commit_count": 0
            }

        # ==========================================
        # Step 2: 拿着 Hash 去查连带修改的文件名
        # 使用 git show --name-only 极速导出，不要具体的 diff 内容
        # ==========================================
        # 注意：如果 commit 数量极大，可能会超出命令行参数长度限制，
        # 在内核场景下通常一个函数的 commit 在几百以内，这里截取前 500 个保证安全。
        git_show_files_cmd = [
            "git", "show", "--name-only", "--format="
        ] + commit_hashes[:500] 
        
        files_result = subprocess.run(
            git_show_files_cmd,
            cwd=Config.KERNEL_DIR_ORI,
            capture_output=True,
            text=True,
            timeout=20,
            check=True
        )
        
        # ==========================================
        # Step 3: 极速统计频次 (利用 Python 内置的高性能 Counter)
        # ==========================================
        all_touched_files = files_result.stdout.strip().splitlines()
        
        # 过滤掉空行和原本的 file_path 本身
        related_files_counter = Counter(
            f for f in all_touched_files if f and f != file_path
        )
        
        # 提取 Top 10
        sorted_related = related_files_counter.most_common(10)
        
        related_files_list = [
            {"file_path": file, "count": count} 
            for file, count in sorted_related
        ]
        
        log.info(f"✅ Found {len(related_files_list)} related files from {commit_count} commits")
        
        return {
            "success": True,
            "error": None,
            "related_files": related_files_list,
            "commit_count": commit_count
        }
        
    except subprocess.TimeoutExpired:
        log.error("❌ get_related_files failed: Git command timed out")
        return {
            "success": False, "error": "Git command timed out",
            "related_files": [], "commit_count": 0
        }
    except subprocess.CalledProcessError as e:
        log.error(f"❌ get_related_files failed: Git execution error: {e.stderr}")
        return {
            "success": False, "error": f"Git error: {e.stderr}",
            "related_files": [], "commit_count": 0
        }
    except Exception as e:
        log.error(f"❌ get_related_files failed: {e}")
        return {
            "success": False, "error": str(e),
            "related_files": [], "commit_count": 0
        }

@tool
def explore_ast_semantics(
    file_path: str,
    target_field: str = None,
    param_types: list = None,
    target_callee: str = None,
    ops_callback: str = None
) -> Dict:
    """
    解析目标文件的抽象语法树 (AST)，根据指定的语义特征查找匹配的函数。
    
    支持四种查询模式：
    1. 基于数据结构的字段追踪：查找访问特定字段的函数
    2. 基于参数签名的兄弟探测：查找具有特定参数类型的函数
    3. 基于汇聚点的数据流追溯：查找调用特定目标函数的函数
    4. 基于结构体函数指针注册：查找实现了同一个 ops callback 的函数
    
    Args:
        file_path: 相对路径到源文件 (例如 "kernel/bpf/devmap.c")
        target_field: 目标字段名 (例如 "lock")，激活维度一
        param_types: 所需参数类型列表 (例如 ["struct inode *", "struct nilfs_palloc_req *"])，激活维度二
        target_callee: 目标被调用函数名 (例如 "__tipc_sendstream")，激活维度三
        ops_callback: 结构体 ops 字段名 (例如 "ioctl")，激活维度四
    
    Returns:
        Dict 包含匹配的函数列表，每个函数包含 name, line_start, line_end
    """
    log = _ensure_logger()
    log.info(f"🔍 [Tool] explore_ast_semantics: {file_path}")
    
    try:
        extractor = _get_context_extractor()
        full_path = os.path.join(Config.KERNEL_DIR_ORI, file_path)
        content = extractor._read_file_text(full_path)
        
        if not content:
            return {
                "success": False,
                "error": f"File not found or empty: {file_path}",
                "matched_functions": []
            }
        
        # 解析整个文件的 AST
        tree = extractor.c_parser.parse(bytes(content, "utf8"))
        root_node = tree.root_node
        
        # 收集所有函数定义
        function_definitions = []
        function_name_map = {}  # 用于快速查找
        stack = [root_node]
        
        while stack:
            current_node = stack.pop()
            if current_node.type == 'function_definition':
                declarator = current_node.child_by_field_name('declarator')
                if not declarator:
                    stack.extend(current_node.children)
                    continue
                
                # 提取函数名
                function_name_node = None
                temp_stack = [declarator]
                while temp_stack:
                    cur_node = temp_stack.pop()
                    for ch in cur_node.children:
                        if ch.type == 'identifier':
                            function_name_node = ch
                            temp_stack.clear()
                            break
                        if ch.end_byte <= declarator.end_byte:
                            temp_stack.append(ch)
                
                if not function_name_node:
                    stack.extend(current_node.children)
                    continue
                
                function_name = function_name_node.text.decode('utf8')
                start_line = current_node.start_point[0] + 1
                end_line = current_node.end_point[0] + 1
                
                # 提取参数列表
                parameter_list = declarator.child_by_field_name('parameters')
                params_text = ""
                if parameter_list:
                    params_text = content[parameter_list.start_byte:parameter_list.end_byte]
                    params_text = params_text.strip()[1:-1] if params_text.strip().startswith('(') else params_text
                    params_text = ' '.join(params_text.replace('\n', ' ').split())
                
                # 提取函数体
                function_body_node = current_node.child_by_field_name('body')
                body_code = ""
                if function_body_node:
                    body_code = content[function_body_node.start_byte:function_body_node.end_byte]
                
                func_info = {
                    'name': function_name,
                    'start_line': start_line,
                    'end_line': end_line,
                    'params_text': params_text,
                    'body_code': body_code,
                    'node': current_node
                }
                function_definitions.append(func_info)
                function_name_map[function_name] = func_info
            
            stack.extend(current_node.children)
        
        # 维度四：找出实现了同一个 ops callback 的函数
        ops_callback_funcs = set()
        if ops_callback:
            ops_callback_funcs = _find_ops_callback_functions(root_node, content, ops_callback)
        
        # 过滤匹配的函数
        matched_functions = []
        
        for func in function_definitions:
            match = False
            
            # 维度一：基于数据结构的字段追踪
            if target_field:
                if _has_target_field_access(func['node'], content, target_field):
                    match = True
            
            # 维度二：基于参数签名的兄弟探测
            if not match and param_types:
                if _has_required_params(func['params_text'], param_types):
                    match = True
            
            # 维度三：基于汇聚点的数据流追溯
            if not match and target_callee:
                if _calls_target_function(func['node'], content, target_callee):
                    match = True
            
            # 维度四：基于结构体函数指针注册
            if not match and ops_callback:
                if func['name'] in ops_callback_funcs:
                    match = True
            
            if match:
                matched_functions.append({
                    'name': func['name'],
                    'line_start': func['start_line'],
                    'line_end': func['end_line']
                })
        
        log.info(f"✅ Found {len(matched_functions)} matching functions")
        
        return {
            "success": True,
            "error": None,
            "matched_functions": matched_functions
        }
        
    except Exception as e:
        log.error(f"❌ explore_ast_semantics failed: {e}")
        return {
            "success": False,
            "error": str(e),
            "matched_functions": []
        }


def _has_target_field_access(node, content, target_field):
    """检查节点是否访问了目标字段"""
    stack = [node]
    
    while stack:
        current_node = stack.pop()
        
        # 检查字段访问 (a.b)
        if current_node.type == 'field_expression':
            field_node = current_node.child_by_field_name('field')
            if field_node and field_node.type == 'identifier':
                field_name = field_node.text.decode('utf8')
                if field_name == target_field:
                    return True
        
        # 检查指针字段访问 (a->b)
        elif current_node.type == 'pointer_expression':
            field_node = current_node.child_by_field_name('field')
            if field_node and field_node.type == 'identifier':
                field_name = field_node.text.decode('utf8')
                if field_name == target_field:
                    return True
        
        stack.extend(current_node.children)
    
    return False


def _has_required_params(params_text, required_param_types):
    """检查函数参数是否包含所有必需的参数类型"""
    if not params_text:
        return False
    
    # 清理参数文本
    params = params_text.split(',')
    param_types = []
    
    for param in params:
        param = param.strip()
        if not param:
            continue
        
        # 提取参数类型（去掉参数名）
        param_parts = param.split()
        if param_parts:
            # 处理指针类型
            type_parts = []
            for part in param_parts:
                if '*' in part:
                    type_parts.append(part)
                    # 指针后面的部分是参数名，停止
                    if part.endswith('*'):
                        break
                elif part != 'const' and part != 'volatile':
                    type_parts.append(part)
            
            if type_parts:
                param_type = ' '.join(type_parts)
                param_types.append(param_type)
    
    # 检查是否包含所有必需的参数类型
    for required_type in required_param_types:
        if required_type not in param_types:
            return False
    
    return True


def _calls_target_function(node, content, target_callee):
    """检查函数是否调用了目标函数"""
    stack = [node]
    
    while stack:
        current_node = stack.pop()
        
        # 检查函数调用
        if current_node.type == 'call_expression':
            function_node = current_node.child_by_field_name('function')
            if function_node:
                # 处理直接函数调用
                if function_node.type == 'identifier':
                    func_name = function_node.text.decode('utf8')
                    if func_name == target_callee:
                        return True
                # 处理间接函数调用（如通过指针）
                elif function_node.type == 'field_expression' or function_node.type == 'pointer_expression':
                    # 递归检查函数表达式
                    if _has_identifier(function_node, target_callee):
                        return True
        
        stack.extend(current_node.children)
    
    return False


def _has_identifier(node, target_name):
    """检查节点是否包含目标标识符"""
    stack = [node]
    
    while stack:
        current_node = stack.pop()
        if current_node.type == 'identifier':
            if current_node.text.decode('utf8') == target_name:
                return True
        stack.extend(current_node.children)
    
    return False


def _find_ops_callback_functions(root_node, content, ops_callback_field):
    """找出所有实现了同一个 ops callback 的函数"""
    callback_functions = set()
    stack = [root_node]
    
    def extract_func_from_value(value_node):
        """从值节点中提取函数名"""
        func_name_node = None
        func_stack = [value_node]
        while func_stack:
            v_node = func_stack.pop()
            if v_node.type == 'identifier':
                func_name_node = v_node
                break
            func_stack.extend(v_node.children)
        if func_name_node:
            return func_name_node.text.decode('utf8')
        return None
    
    while stack:
        current_node = stack.pop()
        
        # 模式1: 结构体初始化中的字段赋值
        # 例如: { .ioctl = my_ioctl_func, ... }
        if current_node.type == 'initializer_list':
            for child in current_node.children:
                if child.type == 'field_initializer':
                    # 查找字段名
                    designator = child.child_by_field_name('designator')
                    if designator:
                        field_name_node = None
                        temp_stack = [designator]
                        while temp_stack:
                            des_node = temp_stack.pop()
                            if des_node.type == 'identifier':
                                field_name_node = des_node
                                break
                            temp_stack.extend(des_node.children)
                        
                        if field_name_node:
                            field_name = field_name_node.text.decode('utf8')
                            if field_name == ops_callback_field:
                                # 找到匹配的字段，提取右边的函数名
                                value_node = child.child_by_field_name('value')
                                if value_node:
                                    func_name = extract_func_from_value(value_node)
                                    if func_name:
                                        callback_functions.add(func_name)
        
        # 模式2: 直接赋值
        # 例如: ops.ioctl = my_ioctl_func;
        elif current_node.type == 'assignment_expression':
            left = current_node.child_by_field_name('left')
            right = current_node.child_by_field_name('right')
            if left and right:
                # 检查左边是否是字段访问
                is_field_access = False
                stack_left = [left]
                while stack_left:
                    n = stack_left.pop()
                    if n.type == 'field_expression' or n.type == 'pointer_expression':
                        # 检查字段名
                        field_node = n.child_by_field_name('field')
                        if field_node and field_node.type == 'identifier':
                            field_name = field_node.text.decode('utf8')
                            if field_name == ops_callback_field:
                                is_field_access = True
                                break
                    stack_left.extend(n.children)
                
                if is_field_access:
                    func_name = extract_func_from_value(right)
                    if func_name:
                        callback_functions.add(func_name)
        
        stack.extend(current_node.children)
    
    return callback_functions


FUNCTION_CALL_TOOLS = [
    get_function_context,
    get_function_body,
    get_function_related_info,
    search_historical_fixes,
    read_kernel_file,
    get_bug_info,
    query_golden_fixes,
    get_related_files,
    explore_ast_semantics
]

def get_function_call_tools():
    """Return the list of available function call tools."""
    return FUNCTION_CALL_TOOLS

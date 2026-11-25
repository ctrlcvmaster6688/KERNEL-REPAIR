from typing import TypedDict, List, Dict, Optional, Annotated, Any
from operator import add
from threading import Lock

class TaskItem(TypedDict):
    func_name: str
    file_path: str
    root_cause: str

class PatchCandidate(TypedDict):
    patch_id: str
    modified_code: str
    temperature: float
    generation_round: int
    reasoning: str

class FailureRecord(TypedDict):
    func_name: str
    file_path: str
    failure_node: str
    final_log: str
    code_retry_count: int
    rc_retry_count: int

class AttemptRecord(TypedDict):
    patch_id: str
    strategy: str
    reasoning: str
    validation_status: str
    error_summary: str
    judge_decision: str

class CaseLevelLessons(TypedDict):
    failed_strategies: List[str]
    error_patterns: List[str]
    judge_insights: List[str]
    bug_type_hypothesis: str

class CaseMemory(TypedDict):
    current_function_attempts: List[AttemptRecord]
    case_level_lessons: CaseLevelLessons

class FixAttemptHistory(TypedDict):
    attempt_round: int
    candidate_id: int
    Functions: List[Dict]
    RootCauseHypothesis: str
    Patch: str
    Result: str
    ErrorLogExcerpt: str
    JudgeDecision: str
    JudgeSuggestion: str

class TaskState(TypedDict):
    task: TaskItem
    current_root_cause: str
    function_context: Dict
    historical_knowledge: str
    api_documentation: str
    generated_patches: List[PatchCandidate]
    selected_patch: Optional[PatchCandidate]
    current_validation_log: str
    validation_status: str
    code_retry: int
    rc_retry: int
    judge_decision: str
    judge_analysis: str
    worker_id: Optional[int]

class ThreadFailureRecord(TypedDict):
    attempt: int
    root_cause: str
    patch: str
    error: str
    decision: str
    analysis: str

class CaseSharedMemory(TypedDict):
    abandoned_combinations: List[tuple]
    combination_failures: Dict[str, List[ThreadFailureRecord]]
    tried_combinations: List[tuple]
    total_attempts: int
    success: bool
    success_patches: List[Dict]
    success_functions_list: List[List[Dict]]
    stop_signal: Any
    active_validators: int
    validator_lock: Any

class FixThreadState(TypedDict):
    thread_id: int
    candidate_id: Optional[str]
    attempt_key: tuple
    selected_functions: List[Dict]
    unified_root_cause: str
    fix_strategy: str
    reasoning: str
    attempts: int
    code_tweaks: int
    rc_revisions: int
    status: str
    current_patch: Optional[str]
    current_patches: Optional[List[Dict]]  # 多函数补丁列表
    current_patch_id: Optional[str]
    failure_history: List[ThreadFailureRecord]
    function_contexts: Dict
    judge_suggestion: Optional[str]
    judge_analysis: Optional[str]
    function_combination: bool

class BugFixState(TypedDict):
    syzid: str
    kernel_commit: str
    syz_commit: str
    bug_config_path: str
    bug_report: str
    testcase: str
    task_queue: List[TaskItem]
    active_tasks: List[TaskState]
    candidate_hypotheses: List[Dict]
    attempt_round: int
    total_attempts: int
    max_code_retry: int
    max_rc_retry: int
    failure_history: Annotated[List[FailureRecord], add]
    fix_attempt_history: Annotated[List[FixAttemptHistory], add]
    success_patch: Optional[PatchCandidate]
    success_patches: List[Dict]
    success_functions_list: List[List[Dict]]
    final_status: str
    parallel_validation_enabled: bool
    cot_enabled: bool
    tool_calls_log: List[str]
    all_generated_patches: Annotated[List[PatchCandidate], add]
    token_stats: Dict
    oracle_keyword: str
    case_memory: CaseMemory
    case_shared_memory: CaseSharedMemory
    fix_threads: List[FixThreadState]
    conservative_combinations_tried: int
    exploration_combinations_tried: int
    phase: str
    no_golden_fixes: bool
    no_historical_fixes: bool
    function_combination: Optional[list]
    no_valid_threads: bool

# 常规模式下的尝试次数限制
MAX_TOTAL_ATTEMPTS = 7
MAX_THREAD_ATTEMPTS = 5

# 函数组合模式下的尝试次数限制
MAX_TOTAL_ATTEMPTS_WITH_FUNCTIONS = 3
MAX_THREAD_ATTEMPTS_WITH_FUNCTIONS = 2

class CaseSharedMemoryManager:
    _instance = None
    _lock = Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._memory = None
        return cls._instance
    
    def init_memory(self):
        import threading
        self._memory = {
            "abandoned_combinations": [],
            "combination_failures": {},
            "tried_combinations": [],
            "total_attempts": 0,
            "success": False,
            "success_patches": [],
            "success_functions_list": [],
            "stop_signal": threading.Event(),
            "active_validators": 0,
            "validator_lock": threading.Lock()
        }
        return self._memory
    
    def get_memory(self) -> CaseSharedMemory:
        if self._memory is None:
            return self.init_memory()
        return self._memory
    
    def reset(self):
        self._memory = None
    
    def is_combination_abandoned(self, functions: tuple) -> bool:
        memory = self.get_memory()
        return functions in memory["abandoned_combinations"]
    
    def abandon_combination(self, functions: tuple):
        memory = self.get_memory()
        if functions not in memory["abandoned_combinations"]:
            memory["abandoned_combinations"].append(functions)
    
    def register_combination(self, functions: tuple) -> bool:
        memory = self.get_memory()
        if functions in memory["tried_combinations"]:
            return False
        memory["tried_combinations"].append(functions)
        memory["total_attempts"] += 1
        return True
    
    def is_combination_tried(self, functions: tuple) -> bool:
        memory = self.get_memory()
        return functions in memory["tried_combinations"]
    
    def add_failure(self, functions: tuple, record: ThreadFailureRecord):
        memory = self.get_memory()
        key = str(functions)
        if key not in memory["combination_failures"]:
            memory["combination_failures"][key] = []
        memory["combination_failures"][key].append(record)
    
    def get_failures(self, functions: tuple) -> List[ThreadFailureRecord]:
        memory = self.get_memory()
        key = str(functions)
        return memory["combination_failures"].get(key, [])
    
    def get_total_attempts(self) -> int:
        return self.get_memory()["total_attempts"]
    
    def mark_success(self, patch: Dict, functions: List[Dict]):
        memory = self.get_memory()
        memory["success"] = True
        memory["success_patches"].append(patch)
        memory["success_functions_list"].append(functions)
        # 不立即发送停止信号，允许其他验证完成
    
    def is_success(self) -> bool:
        return self.get_memory()["success"]
    
    def can_continue(self, function_combination: bool = False) -> bool:
        memory = self.get_memory()
        if function_combination:
            return (not memory["success"]) and (memory["total_attempts"] < MAX_TOTAL_ATTEMPTS_WITH_FUNCTIONS)
        else:
            return (not memory["success"]) and (memory["total_attempts"] < MAX_TOTAL_ATTEMPTS)
    
    def get_abandoned_combinations(self) -> List[tuple]:
        return self.get_memory()["abandoned_combinations"]
    
    def get_tried_combinations(self) -> List[tuple]:
        return self.get_memory()["tried_combinations"]
    
    def get_all_failures(self) -> Dict[str, List[ThreadFailureRecord]]:
        return self.get_memory()["combination_failures"]
    
    def signal_stop(self):
        """发送停止信号"""
        self.get_memory()["stop_signal"].set()
    
    def should_stop(self) -> bool:
        """检查是否应该停止"""
        return self.get_memory()["stop_signal"].is_set()
    
    def validator_start(self):
        """开始验证前调用"""
        memory = self.get_memory()
        with memory["validator_lock"]:
            memory["active_validators"] += 1
    
    def validator_end(self):
        """验证结束后调用"""
        memory = self.get_memory()
        with memory["validator_lock"]:
            memory["active_validators"] -= 1
    
    def can_stop_now(self) -> bool:
        """检查是否可以立即停止（没有活跃的验证器）"""
        memory = self.get_memory()
        with memory["validator_lock"]:
            return memory["active_validators"] == 0
    
    def get_active_validators(self) -> int:
        """获取活跃验证器数量"""
        memory = self.get_memory()
        with memory["validator_lock"]:
            return memory["active_validators"]

def create_initial_state(
    syzid: str,
    kernel_commit: str,
    syz_commit: str,
    bug_config_path: str,
    bug_report: str,
    testcase: str,
    task_queue: List[TaskItem],
    oracle_keyword: str = "",
    no_golden_fixes: bool = False,
    no_historical_fixes: bool = False,
    function_combination: list = None
) -> BugFixState:
    memory_manager = CaseSharedMemoryManager()
    memory_manager.reset()
    case_shared_memory = memory_manager.init_memory()
    
    # 根据是否使用函数组合调整尝试次数限制
    if function_combination:
        max_code_retry = 2
        max_rc_retry = 1
    else:
        max_code_retry = 3
        max_rc_retry = 2
    
    return {
            "syzid": syzid,
            "kernel_commit": kernel_commit,
            "syz_commit": syz_commit,
            "bug_config_path": bug_config_path,
            "bug_report": bug_report,
            "testcase": testcase,
            "task_queue": task_queue,
            "active_tasks": [],
            "candidate_hypotheses": [],
            "attempt_round": 1,
            "total_attempts": 0,
            "max_code_retry": max_code_retry,
            "max_rc_retry": max_rc_retry,
            "failure_history": [],
            "fix_attempt_history": [],
            "success_patch": None,
            "success_patches": [],
            "success_functions_list": [],
            "final_status": "RUNNING",
            "parallel_validation_enabled": True,
            "cot_enabled": True,
            "tool_calls_log": [],
            "all_generated_patches": [],
            "token_stats": {
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_requests": 0,
                "by_model": {}
            },
            "oracle_keyword": oracle_keyword,
            "case_memory": {
                "current_function_attempts": [],
                "case_level_lessons": {
                    "failed_strategies": [],
                    "error_patterns": [],
                    "judge_insights": [],
                    "bug_type_hypothesis": ""
                }
            },
            "case_shared_memory": case_shared_memory,
            "fix_threads": [],
            "conservative_combinations_tried": 0,
            "exploration_combinations_tried": 0,
            "phase": "conservative",
            "no_golden_fixes": no_golden_fixes,
            "no_historical_fixes": no_historical_fixes,
            "function_combination": function_combination
        }

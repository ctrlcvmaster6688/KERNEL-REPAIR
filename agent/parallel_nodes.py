"""
Parallel Planner Node - 规划器节点

只包含 Planner 相关的函数，用于生成候选修复假设。
"""

from typing import Dict, Any, List, Optional, TypedDict
import json
import sys
import os

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from agent.state import BugFixState, TaskItem, MAX_TOTAL_ATTEMPTS_WITH_FUNCTIONS
from agent.log_manager import get_logger
from config import Config
from utils.llm_interface import call_llm
from utils.file_ops import extract_json_from_text
from tool.function_calls import read_kernel_file, get_function_context, get_function_body, get_function_related_info, get_related_files, explore_ast_semantics
from tool.kernel_doc import query_kernel_api

TOOL_USAGE_LIMITS = {
    "conservative_planner": {
        "max_tool_calls": 5,
        "allowed_tools": ["read_kernel_file", "get_function_related_info", "query_kernel_api", "search_historical_fixes"]
    },
    "exploration_planner": {
        "max_tool_calls": 10,
        "allowed_tools": ["read_kernel_file", "get_function_context", "get_function_body", "get_function_related_info", "query_kernel_api", "get_related_files", "explore_ast_semantics", "search_historical_fixes"]
    }
}

def execute_tool_call(tool_name: str, params: Dict, tool_limits: Dict) -> Dict:
    """执行工具调用，检查工具使用限制"""
    if tool_name not in tool_limits["allowed_tools"]:
        return {"success": False, "error": f"Tool {tool_name} is not allowed in this context"}

    try:
        if tool_name == "read_kernel_file":
            return read_kernel_file.invoke(params)
        elif tool_name == "get_function_context":
            return get_function_context.invoke(params)
        elif tool_name == "get_function_body":
            return get_function_body.invoke(params)
        elif tool_name == "get_function_related_info":
            return get_function_related_info.invoke(params)
        elif tool_name == "query_kernel_api":
            return query_kernel_api.invoke(params)
        elif tool_name == "get_related_files":
            return get_related_files.invoke(params)
        elif tool_name == "explore_ast_semantics":
            return explore_ast_semantics.invoke(params)
        elif tool_name == "search_historical_fixes":
            from tool.function_calls import search_historical_fixes
            return search_historical_fixes.invoke(params)
        else:
            return {"success": False, "error": f"Tool {tool_name} not supported"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def truncate_tool_result(result: Dict, max_length: int = 2000) -> Dict:
    """截断工具结果，防止 token 爆炸"""
    if not isinstance(result, dict):
        return result

    truncated = result.copy()

    # 截断字符串类型的值
    for key, value in truncated.items():
        if isinstance(value, str):
            if len(value) > max_length:
                truncated[key] = value[:max_length] + f"\n... [TRUNCATED: {len(value) - max_length} more characters]"
        elif isinstance(value, dict):
            # 递归截断嵌套字典
            truncated[key] = truncate_tool_result(value, max_length)
        elif isinstance(value, list):
            # 截断列表
            if len(value) > 10:
                truncated[key] = value[:10] + [f"... [TRUNCATED: {len(value) - 10} more items]"]
            else:
                # 截断列表中的字符串
                truncated[key] = [
                    truncate_tool_result(item, max_length) if isinstance(item, dict)
                    else (item[:max_length] + f"... [TRUNCATED]" if isinstance(item, str) and len(item) > max_length else item)
                    for item in value
                ]

    return truncated


REACT_TOOL_PROMPT = """
## Tool Usage Instructions

You can use the following tools to gather additional information if needed:

1. **read_kernel_file(file_path: str, start_line: int = 1, end_line: int = 500)** - Read a kernel source file content with optional line pagination (default: lines 1-500)
2. **get_function_context(file_path: str, func_name: str)** - Extract structured context for a target function (target function code, related structs, macros, similar functions)
3. **get_function_body(file_path: str, func_name: str)** - Extract only the function body for a target function
4. **get_function_related_info(file_path: str, func_name: str)** - Extract related information for a target function (structs, macros, similar functions)
5. **query_kernel_api(func_name: str, file_path: str, include_implementation: bool)** - Query kernel API documentation
6. **get_related_files(func_name: str, file_path: str, page: int = 1, page_size: int = 5)** - Find related files from Git history that were modified together with the given function (supports pagination with page parameter)
7. **explore_ast_semantics(file_path: str, target_function: str = None, target_field: str = None, ops_callback: str = None, page: int = 1, page_size: int = 10)** - Explore AST semantics for related functions in the same file (supports pagination with page parameter)
8. **search_historical_fixes(syzid: str, bug_report: str, testcase: str = "", rootcause: str = "", strict_time_filter: bool = False)** - Search for historically similar kernel bug fixes
   - syzid: The current bug's Syzbot ID
   - bug_report: The crash report / stack trace text
   - testcase: Optional Syzkaller testcase text
   - rootcause: Optional root cause analysis text
   - strict_time_filter: If True, only return fixes from before this bug's timestamp

**Important:**
- You can use up to {max_tool_calls} tool calls
- Only use tools when necessary to improve your analysis
- Format tool calls as JSON objects with "tool_call" key

Example tool call:
{{
  "tool_call": {{
    "name": "read_kernel_file",
    "params": {{
      "file_path": "include/linux/kernel.h"
    }}
  }}
}}
"""

PLANNER_SYSTEM_PROMPT = """
You are an intelligent planner for Linux kernel bug fixes, with extensive kernel development experience and defect analysis capabilities.

Your task is to:
1. Analyze bug reports and stack information to understand the root cause of the defect
2. Based on the Top-10 localization results, select relevant functions for repair
3. Generate multiple distinct candidate hypotheses with different function combinations

Decision principles:
- Single-function priority: If the Top-1 function can explain all crash characteristics, prioritize single-function repair
- "80%" fault can be solved in single function. So, if the single function can explain the fault, prioritize single-function repair.
- Collaborative triggering: Only form a repair group of multiple functions when there are stack associations, synchronization associations, or resource lifecycle associations between functions
- Pitfall avoidance: Read historical failure records to avoid repeating previous failed attempts
- Historical learning: Learn from similar past fixes to improve fix quality
- Diversity: Generate multiple distinct candidate hypotheses with different function combinations to maximize the chances of finding a successful fix

Output format:
```json
[
  {
    "selected_functions": [
      {"name": "func1", "file": "path/to/file1.c"}
    ],
    "unified_root_cause": "Root cause analysis for candidate 1...",
    "fix_strategy": "Repair strategy for candidate 1...",
    "reasoning": "Step-by-step reasoning process for candidate 1..."
  },
  {
    "selected_functions": [
      {"name": "func2", "file": "path/to/file2.c"}
    ],
    "unified_root_cause": "Root cause analysis for candidate 2...",
    "fix_strategy": "Repair strategy for candidate 2...",
    "reasoning": "Step-by-step reasoning process for candidate 2..."
  },
  {
    "selected_functions": [
      {"name": "func1", "file": "path/to/file1.c"},
      {"name": "func2", "file": "path/to/file2.c"}
    ],
    "unified_root_cause": "Root cause analysis for candidate 3...",
    "fix_strategy": "Repair strategy for candidate 3...",
    "reasoning": "Step-by-step reasoning process for candidate 3..."
  }
]
```

Note: The example shows a mix of single-function and multi-function candidates. Choose the appropriate combination based on the actual bug characteristics.
"""

PLANNER_USER_TEMPLATE = """
## Bug Information
- **SyzID**: {syzid}
- **Bug Report**: {bug_report}
- **Test Case**: {testcase}

## Top-10 Localization Results
{task_queue}

## Historical Similar Fixes
{historical_fixes_info}

## Historical Failure Records
{fix_attempt_history}

## Abandoned Combinations
{abandoned_combinations}

## Tried Combinations
{tried_combinations}

Please analyze the above information and generate multiple distinct candidate hypotheses for fixing this bug. Each candidate should have a different function combination to maximize the chances of finding a successful fix.

**IMPORTANT**:
- DO NOT generate candidates using function combinations listed in "Abandoned Combinations" section
- These combinations have already been tried and failed, you must choose DIFFERENT combinations
- Avoid using function combinations listed in "Tried Combinations" section if possible
- These combinations have already been attempted, try to explore new combinations

For each candidate, provide:
1. A specific function combination (can be single-function or multi-function)
2. A unified root cause analysis for that combination
3. A repair strategy tailored to that combination
4. Step-by-step reasoning for why this combination was selected

Ensure that the candidate hypotheses are diverse and cover different potential root causes and fix approaches.
"""

class CandidateHypothesis(TypedDict):
    """候选修复假设"""
    candidate_id: int
    selected_functions: List[Dict]
    unified_root_cause: str
    fix_strategy: str
    reasoning: str
    generated_patch: Optional[str]
    result_status: str
    failure_feedback: str

logger = get_logger("ParallelAgentNodes")

MAX_CANDIDATES = 3


def generate_root_cause_analysis_from_functions(
    syzid: str,
    bug_report: str,
    testcase: str,
    function_combination: List[Dict],
    num_candidates: int = 1,
    previous_failures: List[Dict] = None,
    start_index: int = 1
) -> List[Dict]:
    """
    基于给定的函数组合生成根因分析

    Args:
        syzid: Syzbot case ID
        bug_report: Bug report content
        testcase: Testcase content
        function_combination: 给定的函数组合列表，每个元素包含name和file
        num_candidates: 需要生成的不同根因候选数量
        previous_failures: 已失败的根因/修复记录，用于避免重复
        start_index: candidate 编号起点

    Returns:
        包含根因分析的候选假设列表
    """
    from tool.function_calls import (
        read_kernel_file,
        get_function_context
    )

    logger.info(f"\n{'='*60}")
    logger.info(f"🧠 Generating Root Cause Analysis from Given Functions")
    logger.info(f"   Syzid: {syzid}")
    logger.info(f"   Functions: {[f['name'] for f in function_combination]}")
    logger.info(f"{'='*60}")

    # 收集函数信息
    function_infos = []
    for func_info in function_combination:
        func_name = func_info['name']
        file_path = func_info['file']

        logger.info(f"   📖 Analyzing function: {func_name} @ {file_path}")

        # 读取函数上下文
        try:
            context_result = get_function_context.invoke({
                "file_path": file_path,
                "func_name": func_name
            })
            function_infos.append({
                "func_name": func_name,
                "file_path": file_path,
                "context": context_result.get("content", ""),
                "signature": context_result.get("signature", "")
            })
        except Exception as e:
            logger.error(f"   ❌ Failed to get function context: {e}")
            # 尝试读取文件
            try:
                file_content = read_kernel_file.invoke({
                    "file_path": file_path,
                    "start_line": 1,
                    "end_line": 1000
                })
                function_infos.append({
                    "func_name": func_name,
                    "file_path": file_path,
                    "context": file_content.get("content", ""),
                    "signature": ""
                })
            except Exception as e2:
                logger.error(f"   ❌ Failed to read file: {e2}")
                function_infos.append({
                    "func_name": func_name,
                    "file_path": file_path,
                    "context": "",
                    "signature": ""
                })

    # 构建提示
    functions_info_str = ""
    for i, func_info in enumerate(function_infos):
        functions_info_str += f"\n{'='*80}\n"
        functions_info_str += f"Function {i+1}: {func_info['func_name']} @ {func_info['file_path']}\n"
        functions_info_str += f"Signature: {func_info['signature']}\n"
        functions_info_str += f"Content:\n{func_info['context']}\n"

    previous_failures = previous_failures or []
    failure_context = "No previous root-cause candidates have failed yet."
    if previous_failures:
        failure_context = ""
        for i, failure in enumerate(previous_failures[-6:], 1):
            failure_context += (
                f"\nFailed candidate {i}:\n"
                f"- Root cause: {failure.get('root_cause', '')[:1200]}\n"
                f"- Validation/Judge error: {failure.get('error', '')[:500]}\n"
                f"- Judge decision: {failure.get('decision', '')}\n"
                f"- Judge analysis: {failure.get('analysis', '')[:500]}\n"
            )

    user_prompt = f"""
You are an expert Linux kernel bug analyst. Your task is to analyze the given bug report and testcase,
and generate {num_candidates} distinct root-cause candidates based on the provided fixed function combination.

Important constraints:
1. The function combination is fixed by the user. Do NOT add, remove, or replace selected functions.
2. Generate different root-cause hypotheses or repair strategies for the same fixed functions.
3. Avoid repeating any failed candidates listed below.
4. Each candidate must be actionable by a patch-generation thread.

Bug Report:
{bug_report}

Testcase:
{testcase}

Analyzed Functions:{functions_info_str}

Previous Failed Candidates:
{failure_context}

Return ONLY valid JSON in this format:
[
  {{
    "unified_root_cause": "Detailed root cause analysis explaining what is wrong, how the bug manifests, and why it occurs.",
    "fix_strategy": "Concrete repair strategy for the fixed functions.",
    "reasoning": "Why this hypothesis is plausible and distinct from other candidates."
  }}
]

Return exactly {num_candidates} candidates.
"""

    # 调用项目统一的 LLM 接口生成根因分析。
    response = call_llm(
        prompt=user_prompt,
        system_prompt="You are an expert Linux kernel bug analyst.",
        temperature=0.1
    )

    candidates = []
    plan_data = extract_json_from_text(response)
    if isinstance(plan_data, dict):
        plan_data = [plan_data]
    if not isinstance(plan_data, list) or not plan_data:
        logger.warning("⚠️ Failed to parse structured root-cause candidates; using raw response as one candidate")
        plan_data = [{
            "unified_root_cause": response.strip(),
            "fix_strategy": "Based on root cause analysis",
            "reasoning": "Generated from raw LLM response"
        }]

    for offset, item in enumerate(plan_data[:num_candidates]):
        candidate_index = start_index + offset
        root_cause = item.get("unified_root_cause") or item.get("root_cause") or ""
        fix_strategy = item.get("fix_strategy") or "Based on root cause analysis"
        reasoning = item.get("reasoning") or "Generated from given function combination"
        candidates.append({
            "id": f"function_combination_rc_{candidate_index}",
            "function_combination_mode": True,
            "selected_functions": [
                {
                    "name": func['name'],
                    "file": func['file'],
                    "status": "given"
                }
                for func in function_combination
            ],
            "unified_root_cause": root_cause,
            "fix_strategy": fix_strategy,
            "confidence": item.get("confidence", 0.9),
            "reasoning": reasoning,
            "root_cause": root_cause,
            "rationale": reasoning
        })

    logger.info(f"✅ Generated {len(candidates)} root cause candidate(s) for {syzid}")
    return candidates


def generate_multiple_candidate_hypotheses(
    syzid: str,
    bug_report: str,
    testcase: str,
    task_queue: List[TaskItem],
    fix_attempt_history: List[Dict],
    num_candidates: int = MAX_CANDIDATES,
    failed_candidates: List[Dict] = None,
    abandoned_combinations: List[tuple] = None,
    tried_combinations: List[tuple] = None,
    no_golden_fixes: bool = False,
    no_historical_fixes: bool = False
) -> List[CandidateHypothesis]:
    """
    生成多个不同的候选修复假设

    Args:
        abandoned_combinations: 已放弃的函数组合列表，Planner 会避免生成这些组合
        tried_combinations: 已尝试的函数组合列表，Planner 会避免生成这些组合
    """
    logger.info(f"🧠 [Planner] Generating {num_candidates} distinct candidate hypotheses...")

    if not task_queue:
        logger.error("❌ Empty task queue, cannot generate candidate hypotheses")
        return []

    # 使用 Top-1 函数的 root cause
    top1_root_cause = task_queue[0].get("root_cause", "") if task_queue else ""
    logger.info(f"   Using Top-1 root cause: {top1_root_cause[:100]}...")

    historical_fixes_info = ""
    if no_historical_fixes:
        logger.info("🚫 [Planner] Historical similar-fix retrieval disabled by --no-historical-fixes")
        historical_fixes_info = "Historical similar-fix retrieval disabled by ablation setting."
    else:
        # 固定调用 search_historical_fixes 工具获取历史修复信息
        logger.info("🔍 [Planner] Calling search_historical_fixes to get historical fix information")
        from tool.function_calls import search_historical_fixes

        historical_fixes_result = search_historical_fixes.invoke({
            "syzid": syzid,
            "bug_report": bug_report,
            "testcase": testcase,
            "rootcause": top1_root_cause,
            "strict_time_filter": True
        })

        if historical_fixes_result.get('success'):
            num_cases = historical_fixes_result.get('num_cases', 0)
            knowledge_text = historical_fixes_result.get('knowledge_text', '')
            logger.info(f"✅ [Planner] Found {num_cases} historical fix cases")
            if knowledge_text:
                # 截断过长的知识文本
                if len(knowledge_text) > 1000:
                    historical_fixes_info = f"## Historical Similar Fixes (Found {num_cases} cases)\n{knowledge_text[:1000]}...\n"
                else:
                    historical_fixes_info = f"## Historical Similar Fixes (Found {num_cases} cases)\n{knowledge_text}\n"
        else:
            logger.warning("⚠️ [Planner] Failed to retrieve historical fixes")
            historical_fixes_info = "## Historical Similar Fixes\nNo historical fix information available.\n"

    task_queue_str = ""
    for i, task in enumerate(task_queue[:10]):
        task_queue_str += f"{i+1}. {task['func_name']} @ {task['file_path']}\n   Root cause: {task['root_cause']}\n"

    history_str = ""
    if fix_attempt_history:
        history_str = "\n".join([
            f"Round {h['attempt_round']}, Candidate {h['candidate_id']}: {[f['name'] for f in h['Functions']]}\n   Status: {h['Result']}\n   Failure reason: {h['ErrorLogExcerpt'][:200]}"
            for h in fix_attempt_history[-3:]
        ])
    else:
        history_str = "No historical failure records"

    abandoned_str = ""
    if abandoned_combinations:
        abandoned_str = "\n".join([
            f"- {list(combo)}" for combo in abandoned_combinations
        ])
        abandoned_str = f"⚠️ The following function combinations have been ABANDONED (DO NOT use these again):\n{abandoned_str}"
    else:
        abandoned_str = "No abandoned combinations yet"

    tried_str = ""
    if tried_combinations:
        tried_str = "\n".join([
            f"- {list(combo)}" for combo in tried_combinations
        ])
        tried_str = f"ℹ️ The following function combinations have already been TRIED (avoid using these again if possible):\n{tried_str}"
    else:
        tried_str = "No combinations have been tried yet"

    # 动态构建保守规划器的工具使用限制
    conservative_tools = ["read_kernel_file", "get_function_related_info"]
    if not no_historical_fixes:
        conservative_tools.append("search_historical_fixes")
    conservative_tools.append("query_kernel_api")

    # 动态构建工具使用限制
    conservative_tool_limits = {
        "max_tool_calls": TOOL_USAGE_LIMITS["conservative_planner"]["max_tool_calls"],
        "allowed_tools": conservative_tools
    }

    react_tool_prompt = REACT_TOOL_PROMPT.format(max_tool_calls=conservative_tool_limits["max_tool_calls"])
    if no_historical_fixes:
        react_tool_prompt = "\n".join(
            line for line in react_tool_prompt.splitlines()
            if "search_historical_fixes" not in line
        )

    user_prompt = PLANNER_USER_TEMPLATE.format(
        syzid=syzid,
        bug_report=bug_report[:2000],
        testcase=testcase[:1000],
        task_queue=task_queue_str,
        historical_fixes_info=historical_fixes_info,
        fix_attempt_history=history_str,
        abandoned_combinations=abandoned_str,
        tried_combinations=tried_str
    )

    user_prompt += f"\n\n{react_tool_prompt}"

    user_prompt += """

## Decision Guidance
1. **Single-function priority**: If the Top-1 function can logically explain the crash on its own, prioritize producing a single-function candidate.
2. **Collaborative triggering**: Only form a multi-function candidate when there are **stack associations**, **lock synchronization associations**, or **resource lifecycle associations** between functions.
3. **No blind combinations**: Strictly prohibit producing combinations that logically overlap with historical failed attempts.
4. **Mandatory function selection**: You MUST select at least one function from the provided task queue. If you cannot determine the best functions, default to the Top-1 function.
"""

    try:
        response = call_llm(
            prompt=user_prompt,
            system_prompt=PLANNER_SYSTEM_PROMPT,
            temperature=0.3
        )

        tool_calls = []
        current_response = response
        tool_call_count = 0
        max_tool_calls = TOOL_USAGE_LIMITS["conservative_planner"]["max_tool_calls"]
        tool_call_history = []  # 跟踪已调用的工具
        conversation_history = ""  # 累积对话历史

        while tool_call_count < max_tool_calls:
            try:
                tool_call_data = extract_json_from_text(current_response)
                if tool_call_data and isinstance(tool_call_data, dict) and "tool_call" in tool_call_data:
                    tool_call = tool_call_data["tool_call"]
                    tool_name = tool_call["name"]
                    tool_params = tool_call["params"]

                    # 检查是否已经调用过相同的工具和参数
                    tool_call_key = f"{tool_name}:{json.dumps(tool_params, sort_keys=True)}"
                    if tool_call_key in tool_call_history:
                        logger.info(f"   ⚠️  Skipping duplicate tool call: {tool_name}")
                        # 告诉 LLM 已经调用过这个工具
                        tool_response_prompt = f"\n\n## Tool Response\n\n{json.dumps({'tool_response': {'success': True, 'message': 'You have already called this tool with the same parameters. Please use the previous result and continue with your analysis.'}}, indent=2)}\n\nPlease continue with your analysis using the information you already have."
                        current_response = call_llm(
                            prompt=user_prompt + conversation_history + tool_response_prompt,
                            system_prompt=PLANNER_SYSTEM_PROMPT,
                            temperature=0.3
                        )
                        continue

                    logger.info(f"   🛠️  Planner tool call: {tool_name}")

                    tool_result = execute_tool_call(
                        tool_name,
                        tool_params,
                        conservative_tool_limits
                    )

                    tool_calls.append(tool_call)
                    tool_call_history.append(tool_call_key)
                    tool_call_count += 1

                    # 当前轮次：使用完整结果
                    tool_response_prompt = "\n\n## Tool Response\n\n" + json.dumps({"tool_response": tool_result}, indent=2) + "\n\nPlease continue with your analysis."

                    # 构建工具调用提示
                    tool_call_prompt = f"\n\n## Tool Call {tool_call_count}\n"

                    current_response = call_llm(
                        prompt=user_prompt + conversation_history + tool_call_prompt + json.dumps({"tool_call": tool_call}, indent=2) + tool_response_prompt,
                        system_prompt=PLANNER_SYSTEM_PROMPT,
                        temperature=0.3
                    )

                    # 添加到历史记录时：截断结果，防止 token 爆炸
                    truncated_result = truncate_tool_result(tool_result, max_length=2000)
                    conversation_history += tool_call_prompt + json.dumps({"tool_call": tool_call}, indent=2)
                    conversation_history += "\n\n## Tool Response\n\n" + json.dumps({"tool_response": truncated_result}, indent=2)
                else:
                    break
            except Exception as e:
                logger.error(f"   ❌ Tool call parsing error: {e}")
                break

        plan_data = extract_json_from_text(current_response)

        candidates = []

        if isinstance(plan_data, list):
            for i, item in enumerate(plan_data[:num_candidates]):
                selected_functions = item.get("selected_functions", [])
                unified_root_cause = item.get("unified_root_cause", "")
                fix_strategy = item.get("fix_strategy", "")
                reasoning = item.get("reasoning", "")

                if not selected_functions:
                    selected_functions = [{"name": task_queue[0]["func_name"], "file": task_queue[0]["file_path"]}]
                    unified_root_cause = unified_root_cause or "Default selection of Top-1 function"
                    fix_strategy = fix_strategy or "Single-function repair"
                    reasoning = reasoning or "No functions selected, defaulting to Top-1 function"

                candidates.append({
                    "candidate_id": i,
                    "selected_functions": selected_functions,
                    "unified_root_cause": unified_root_cause,
                    "fix_strategy": fix_strategy,
                    "reasoning": reasoning,
                    "generated_patch": None,
                    "result_status": "PENDING",
                    "failure_feedback": ""
                })

        if not candidates:
            available_tasks = []

            # 过滤掉已放弃的函数
            for task in task_queue:
                task_func = task["func_name"]
                is_abandoned = False

                if abandoned_combinations:
                    for combo in abandoned_combinations:
                        if task_func in combo:
                            is_abandoned = True
                            break

                if not is_abandoned:
                    available_tasks.append(task)

            # 如果没有可用的函数，返回空列表
            if not available_tasks:
                logger.warning("⚠️ No available tasks, all functions are abandoned")
                return []

            # 从可用函数中生成默认候选
            for i in range(num_candidates):
                task_idx = min(i, len(available_tasks) - 1)
                candidates.append({
                    "candidate_id": i,
                    "selected_functions": [{"name": available_tasks[task_idx]["func_name"], "file": available_tasks[task_idx]["file_path"]}],
                    "unified_root_cause": available_tasks[task_idx].get("root_cause", "Unknown root cause"),
                    "fix_strategy": "Single-function repair",
                    "reasoning": "Default candidate generated due to parsing failure",
                    "generated_patch": None,
                    "result_status": "PENDING",
                    "failure_feedback": ""
                })

        logger.info(f"✅ Generated {len(candidates)} candidate hypotheses")
        for i, candidate in enumerate(candidates):
            logger.info(f"   Candidate {i}: {[f['name'] for f in candidate['selected_functions']]}")

        if tool_calls:
            logger.info(f"   🛠️  Used {len(tool_calls)} tool calls")

        return candidates

    except Exception as e:
        logger.error(f"❌ Planner failed: {e}, generating default candidates")

        candidates = []
        available_tasks = []

        # 过滤掉已放弃的函数
        for task in task_queue:
            task_func = task["func_name"]
            is_abandoned = False

            if abandoned_combinations:
                for combo in abandoned_combinations:
                    if task_func in combo:
                        is_abandoned = True
                        break

            if not is_abandoned:
                available_tasks.append(task)

        # 如果没有可用的函数，返回空列表
        if not available_tasks:
            logger.warning("⚠️ No available tasks, all functions are abandoned")
            return []

        # 从可用函数中生成默认候选
        for i in range(num_candidates):
            task_idx = min(i, len(available_tasks) - 1)
            candidates.append({
                "candidate_id": i,
                "selected_functions": [{"name": available_tasks[task_idx]["func_name"], "file": available_tasks[task_idx]["file_path"]}],
                "unified_root_cause": available_tasks[task_idx].get("root_cause", "Unknown root cause"),
                "fix_strategy": "Single-function repair",
                "reasoning": f"Default candidate generated due to error: {str(e)}",
                "generated_patch": None,
                "result_status": "PENDING",
                "failure_feedback": str(e)
            })

        return candidates


def parallel_planner_node(state: BugFixState) -> Dict[str, Any]:
    """
    并行规划者节点：生成多个不同的修复假设
    """
    from agent.state import CaseSharedMemoryManager

    logger.info("🧠 [Parallel_Planner] Generating candidate hypotheses...")

    syzid = state.get("syzid", "")
    bug_report = state.get("bug_report", "")
    testcase = state.get("testcase", "")
    task_queue = state.get("task_queue", [])
    fix_attempt_history = state.get("fix_attempt_history", [])
    failed_candidates = state.get("failed_candidates", None)

    memory_manager = CaseSharedMemoryManager()
    abandoned_combinations = memory_manager.get_abandoned_combinations()
    tried_combinations = memory_manager.get_tried_combinations()

    if abandoned_combinations:
        logger.info(f"   📋 Abandoned combinations to avoid: {len(abandoned_combinations)}")
        for combo in abandoned_combinations:
            logger.info(f"      - {list(combo)}")

    if tried_combinations:
        logger.info(f"   📋 Tried combinations to avoid: {len(tried_combinations)}")
        for combo in tried_combinations:
            logger.info(f"      - {list(combo)}")

    # 根据 worker 数量生成相应数量的候选假设
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from config import Config
    num_candidates = len(Config.WORKER_POOL_CONFIG)
    if failed_candidates:
        logger.info(f"🔄 [Planner] Generating {num_candidates} new candidate hypotheses to replace failed ones")

    no_golden_fixes = state.get("no_golden_fixes", False)
    no_historical_fixes = state.get("no_historical_fixes", False)
    function_combination = state.get("function_combination", None)

    if function_combination:
        total_attempts = memory_manager.get_total_attempts()
        remaining_attempts = MAX_TOTAL_ATTEMPTS_WITH_FUNCTIONS - total_attempts
        if remaining_attempts <= 0:
            logger.info("🔀 [Parallel_Planner] Function-combination candidate limit reached")
            candidates = []
        else:
            num_to_generate = min(3, remaining_attempts)
            all_failures = memory_manager.get_all_failures()
            previous_failures = [
                failure
                for failures in all_failures.values()
                for failure in failures
            ]
            logger.info(
                f"🧠 [Parallel_Planner] Function-combination mode: generating "
                f"{num_to_generate} root-cause candidate(s), "
                f"{total_attempts}/{MAX_TOTAL_ATTEMPTS_WITH_FUNCTIONS} already tried"
            )
            candidates = generate_root_cause_analysis_from_functions(
                syzid,
                bug_report,
                testcase,
                function_combination,
                num_candidates=num_to_generate,
                previous_failures=previous_failures,
                start_index=total_attempts + 1
            )
    else:
        # 常规流程：生成多个候选假设
        candidates = generate_multiple_candidate_hypotheses(
            syzid,
            bug_report,
            testcase,
            task_queue,
            fix_attempt_history,
            num_candidates,
            failed_candidates,
            abandoned_combinations,
            tried_combinations,
            no_golden_fixes,
            no_historical_fixes
        )

    logger.info(f"✅ Generated {len(candidates)} candidate hypotheses")
    for i, candidate in enumerate(candidates):
        logger.info(f"   Candidate {i}: {[f['name'] for f in candidate['selected_functions']]}")

    previous_attempt_round = state.get("attempt_round", 0)
    new_attempt_round = previous_attempt_round + 1

    return {"candidate_hypotheses": candidates, "attempt_round": new_attempt_round}


def record_failure(state: BugFixState) -> Dict[str, Any]:
    """
    记录失败信息到状态
    """
    logger.info("📝 [Record_Failure] Recording failure information...")

    fix_threads = state.get("fix_threads", [])
    failure_history = list(state.get("failure_history", []))

    for thread in fix_threads:
        if thread.get("status") in ["ABANDONED", "ERROR"]:
            selected_functions = thread.get("selected_functions", [])
            if selected_functions:
                failure_record = {
                    "func_name": ", ".join([f['name'] for f in selected_functions]),
                    "file_path": selected_functions[0].get("file", ""),
                    "failure_node": "fix_thread",
                    "final_log": f"Abandoned after {thread.get('attempts', 0)} attempts. "
                                 f"Code tweaks: {thread.get('code_tweaks', 0)}, "
                                 f"RC revisions: {thread.get('rc_revisions', 0)}",
                    "code_retry_count": thread.get("code_tweaks", 0),
                    "rc_retry_count": thread.get("rc_revisions", 0)
                }
                failure_history.append(failure_record)

    return {"failure_history": failure_history}


def generate_failure_report(state: BugFixState) -> Dict[str, Any]:
    """
    生成失败报告
    """
    logger.info("📊 [Generate_Report] Generating failure report...")

    syzid = state.get("syzid", "")
    fix_threads = state.get("fix_threads", [])
    failure_history = state.get("failure_history", [])

    total_attempts = sum(t.get("attempts", 0) for t in fix_threads)
    abandoned_count = sum(1 for t in fix_threads if t.get("status") == "ABANDONED")

    logger.info(f"\n{'='*60}")
    logger.info(f"📊 FAILURE REPORT FOR {syzid}")
    logger.info(f"{'='*60}")
    logger.info(f"   Total fix threads: {len(fix_threads)}")
    logger.info(f"   Total attempts: {total_attempts}")
    logger.info(f"   Abandoned threads: {abandoned_count}")
    logger.info(f"   Failure records: {len(failure_history)}")

    for thread in fix_threads:
        thread_id = thread.get("thread_id", "?")
        status = thread.get("status", "UNKNOWN")
        attempts = thread.get("attempts", 0)
        functions = [f['name'] for f in thread.get("selected_functions", [])]

        logger.info(f"\n   Thread {thread_id}: {functions}")
        logger.info(f"      Status: {status}")
        logger.info(f"      Attempts: {attempts}")
        logger.info(f"      Code tweaks: {thread.get('code_tweaks', 0)}")
        logger.info(f"      RC revisions: {thread.get('rc_revisions', 0)}")

    logger.info(f"\n{'='*60}\n")

    return {"final_status": "FAILED"}


def generate_exploration_combinations(
    syzid: str,
    bug_report: str,
    testcase: str,
    task_queue: List[TaskItem],
    fix_attempt_history: List[Dict] = None,
    abandoned_combinations: List[tuple] = None,
    num_combinations: int = 2,
    no_golden_fixes: bool = False,
    no_historical_fixes: bool = False
) -> List[Dict]:
    """
    Generate exploration combinations using the test_planner_combinations logic.

    This uses the Top-1 function as an anchor to explore beyond the Top-10 list.

    Args:
        syzid: SyzID of the case
        bug_report: Bug report text
        testcase: Test case text
        task_queue: Top-10 localization results
        fix_attempt_history: History of failed attempts
        abandoned_combinations: Combinations already abandoned
        num_combinations: Number of exploration combinations to generate (default: 2)

    Returns:
        List of candidate hypotheses in the same format as generate_multiple_candidate_hypotheses
    """
    logger.info(f"🧠 [Exploration Planner] Generating {num_combinations} exploration combinations...")

    if not task_queue:
        logger.error("❌ Empty task queue, cannot generate exploration combinations")
        return []

    # --- Reuse test_planner_combinations.py's system prompt ---
    EXPLORATION_PLANNER_SYSTEM_PROMPT = """
You are an intelligent planner for Linux kernel bug fixes, with extensive kernel development experience.
Your primary goal is to think deeply about the bug and explore beyond the obvious to find the comprehensive Multi-Hunk solution.

Important Note:
- Top-10 localization results have a recall rate of < 100%. Complete fixes often require functions NOT in the Top-10.
- All test cases are multi-function defects.
- **CRITICAL: You MUST use only the TOP-1 function as your anchor for exploration.** Do NOT use Top-2 or other functions from the Top-10 list as anchors. The Top-1 function has the highest chance (57.58%) of being correct. Focus all your exploration efforts around the Top-1 function only.

# 1. Available Tools
You have the following tools available to gather information:

1. **read_kernel_file(file_path: str, start_line: int = 1, end_line: int = 500)** - Read a kernel source file content with optional line pagination (default: lines 1-500)
2. **get_function_context(file_path: str, func_name: str)** - Extract structured context for a target function (target function code, related structs, macros, similar functions)
3. **get_function_body(file_path: str, func_name: str)** - Extract only the function body for a target function
4. **get_function_related_info(file_path: str, func_name: str)** - Extract related information for a target function (structs, macros, similar functions)
5. **query_kernel_api(func_name: str, file_path: str, include_implementation: bool)** - Query kernel API documentation
6. **get_related_files(func_name: str, file_path: str, page: int = 1, page_size: int = 5)** - Find related files from Git history that were modified together with the given function (supports pagination with page parameter)
7. **explore_ast_semantics(file_path: str, target_function: str = None, target_field: str = None, ops_callback: str = None, page: int = 1, page_size: int = 10)** - Explore AST semantics for related functions in the same file (supports pagination with page parameter)
8. **search_historical_fixes(syzid: str, bug_report: str, testcase: str = "", rootcause: str = "", strict_time_filter: bool = False)** - Search for historically similar kernel bug fixes
   - syzid: The current bug's Syzbot ID
   - bug_report: The crash report / stack trace text
   - testcase: Optional Syzkaller testcase text
   - rootcause: Optional root cause analysis text
   - strict_time_filter: If True, only return fixes from before this bug's timestamp

# 2. Tool Usage Strategy & Advanced Heuristics (CRITICAL)
- **Strict Anchor Selection (For Git History)**: When using `get_related_files`, NEVER guess function names, and NEVER use generic kernel API functions (e.g., `kfree`, `dev_queue_xmit`, `skb_network_header`). You MUST select ONLY the TOP-1 function from the Top-10 list as your search anchor. DO NOT use Top-2 or any other functions as anchors.
- **Resource Genesis Rule (For AST)**: If a crash occurs due to an uninitialized/null pointer (e.g., empty `rxbuf` or `lock`), DO NOT just patch the symptom. Use `explore_ast_semantics` with `target_field` set to the victim variable (e.g., "rxbuf") to find the `_init` or `_setup` function in the same file.
- **Concurrency/Locking Rule (For AST)**: If you see a lock upgrade (e.g., `read_lock` to `read_lock_irqsave`), use `explore_ast_semantics` with `target_field` set to the lock variable to find ALL functions in the file that access this lock. They MUST be fixed together.
- **Interface Clone Rule (For AST)**: If you fix a protocol or polymorphic interface (e.g., `ops->ioctl`), use `explore_ast_semantics` with `ops_callback` to find sibling protocols in the same directory that share the same defect.
- **High Priority for Top-1**: The Top-1 function has a 57.58% chance of being correct (highest of all). **EACH combination MUST include the Top-1 function**. You are exploring BEYOND the Top-10 list, but your anchor MUST always be the Top-1 function.
- **Multi-function requirement**: MUST generate function combinations that include both functions from the Top-10 list AND functions discovered beyond the Top-10 list
- **Strict Reconnaissance Rule (NO GUESSING)**:
  NEVER guess or hallucinate variable names, struct fields, or lock names.
  If you realize you need to use `explore_ast_semantics` (e.g., to find a lock or an uninitialized resource) but you do not know the exact variable name, you MUST NOT guess.
  Your IMMEDIATELY NEXT step MUST be to call `read_kernel_file`, `get_function_context`, or `get_function_body` on the crashing function. Read the actual source code to identify the EXACT variable or field name (e.g., `priv->tx.txq.lock`) that caused the panic BEFORE calling the AST tool.

# 3. Expert Heuristics (CRITICAL MANDATES & EXACT MATCH RULES)
You are STRICTLY REQUIRED to apply the following multi-hunk patching rules. Failure to bundle these functions will result in a FATAL validation error. DO NOT ignore these exact match scenarios.

1. **Shared Sink Trap (Parallel Interfaces):**
   - **Trigger:** A crash in an interface function (e.g., `tipc_connect`) before calling a bottom-half helper (e.g., `__tipc_sendstream`).
   - **MANDATE:** You are FORBIDDEN from fixing only the crashing function. You MUST use `explore_ast_semantics(target_callee="__tipc_sendstream")` to find ALL parallel interfaces.
   - **EXACT MATCH RULE:** If you see `tipc_connect`, you MUST output `[tipc_connect, tipc_accept]`.

2. **Cross-Protocol Clones (Netfilter & Tunnels):**
   - **Trigger:** Fixing a bug in a protocol-specific file (e.g., IPv4, IPv6, DCCP, SCTP).
   - **MANDATE:** You MUST mirror the fix across parallel protocols using `get_related_files`. Partial fixes are REJECTED.
   - **EXACT MATCH RULE:** - If you fix `__do_replace` in IPv4, you MUST bundle it with IPv6 and Bridge equivalents.
     - If you fix `dccp_timeout_nlattr_to_obj`, you MUST bundle it with `sctp_timeout_nlattr_to_obj`.
     - If you fix `ip6erspan_tunnel_xmit` (IPv6), you MUST bundle it with its Flow-Based sibling `erspan_fb_xmit` (IPv4).

3. **Lock Symmetry (Concurrency Bugs):**
   - **Trigger:** Upgrading a lock (e.g., `read_lock` -> `irqsave` on `fown->lock`).
   - **MANDATE:** Lock upgrades are ALL-OR-NOTHING. You MUST use `explore_ast_semantics(target_field="lock")` to find EVERY function touching that lock in the file.
   - **EXACT MATCH RULE:** If you upgrade locks in `send_sigio`, you MUST bundle it with `send_sigurg`. Output `[send_sigio, send_sigurg]`.

4. **Caller Contract (Error Propagation):**
   - **Trigger:** Making a helper function (e.g., `true_sectors_per_clst`) return a new error code.
   - **MANDATE:** If you change a callee's return semantics, you MUST update its callers. Use `get_function_related_info`.
   - **EXACT MATCH RULE:** If you fix `true_sectors_per_clst` in NTFS3, you MUST bundle it with its caller `ntfs_init_from_boot`.

5. **Lifecycle Symmetry (Setup/Teardown Contract):**
   - **Trigger:** Adding a state flag during setup, or checking it during teardown to prevent UAF.
   - **MANDATE:** Setup and teardown are STRICTLY COUPLED.
   - **EXACT MATCH RULE:** If the bug involves Bluetooth HCI links and you see `hci_disconn_loglink_complete_evt`, you MUST NOT stop there. You MUST bundle it with its creator: `[hci_loglink_complete_evt, hci_disconn_loglink_complete_evt]`.

6. **Async Init Race (Interrupt vs. Setup):**
   - **Trigger:** A datapath callback crashes on an uninitialized lock/queue because it fired before setup finished.
   - **MANDATE:** You MUST find the specific setup function to add an "initialized" flag. DO NOT guess names.
   - **EXACT MATCH RULE:** - If crashing function is `ath9k_wmi_event_tasklet`, you MUST bundle it with `ath9k_tx_init`.
     - If crashing function is `ath9k_htc_rxep`, you MUST bundle it with `ath9k_rx_init`.
# 4. Interaction Format (Reasoning and Acting)
You must ALWAYS "think" before you act. You have two ways to respond:

**Option 1: Tool Call**
Write your analysis, then provide the tool call in a JSON block.

**Option 2: Final Answer**
After gathering enough information, provide exactly {num_combinations} candidate hypotheses.

CRITICAL RULE: You MUST make ONLY ONE tool call at a time. Wait for the result before making your next move!
You can use up to 10 tool calls. Once you have used all 10 tool calls, you MUST provide your final answer immediately.

Output format (for final answer):
```json
[
  {{
    "selected_functions": [
      {{ "name": "func1", "file": "path/to/file1.c" }}
    ],
    "unified_root_cause": "Root cause analysis for candidate 1...",
    "fix_strategy": "Repair strategy for candidate 1...",
    "reasoning": "Step-by-step reasoning process for candidate 1..."
  }}
]
```
"""

    # Build task queue string
    task_queue_str = ""
    for i, task in enumerate(task_queue[:10]):
        task_queue_str += f"{i+1}. {task['func_name']} @ {task['file_path']}\n   Root cause: {task['root_cause']}\n"

    history_str = ""
    if fix_attempt_history:
        history_str = "\n".join([
            f"Round {h['attempt_round']}, Candidate {h['candidate_id']}: {[f['name'] for f in h['Functions']]}\n   Status: {h['Result']}\n   Failure reason: {h['ErrorLogExcerpt'][:200]}"
            for h in fix_attempt_history[-3:]
        ])
    else:
        history_str = "No historical failure records"

    abandoned_str = ""
    if abandoned_combinations:
        abandoned_str = "\n".join([
            f"- {list(combo)}" for combo in abandoned_combinations
        ])
        abandoned_str = f"⚠️ The following function combinations have been ABANDONED (DO NOT use these again):\n{abandoned_str}"
    else:
        abandoned_str = "No abandoned combinations yet"

    historical_tool_guidance = ""
    if not no_historical_fixes:
        historical_tool_guidance = (
            "- **MANDATORY**: You MUST use the search_historical_fixes tool at least once "
            "to retrieve similar past fixes before generating candidates\n"
            "- The search_historical_fixes tool provides valuable insights from past fixes "
            "that will significantly improve the quality of your candidates"
        )

    # Build user prompt
    user_prompt = f"""
## Bug Information
- **SyzID**: {syzid}
- **Bug Report**: {bug_report[:2000]}
- **Test Case**: {testcase[:1000]}

## Top-10 Localization Results
{task_queue_str}

## Historical Failure Records
{history_str}

## Abandoned Combinations
{abandoned_str}

Please analyze the above information and generate {num_combinations} distinct candidate hypotheses for fixing this bug. Each candidate should have a different function combination to maximize the chances of finding a successful fix.

**IMPORTANT**:
- DO NOT generate candidates using function combinations listed in "Abandoned Combinations" section
- These combinations have already been tried and failed, you must choose DIFFERENT combinations
- Focus on exploring BEYOND the Top-10 list! Use the Top-1 function as your anchor to find related functions!
{historical_tool_guidance}

For each candidate, provide:
1. A specific function combination (can be single-function or multi-function)
2. A unified root cause analysis for that combination
3. A repair strategy tailored to that combination
4. Step-by-step reasoning for why this combination was selected

Ensure that the candidate hypotheses are diverse and cover different potential root causes and fix approaches.
"""

    # 动态构建探索规划器的工具使用限制
    exploration_tools = ["read_kernel_file", "get_function_context", "get_function_body", "get_function_related_info", "get_related_files", "explore_ast_semantics"]
    if not no_historical_fixes:
        exploration_tools.append("search_historical_fixes")
    exploration_tools.append("query_kernel_api")

    # 动态构建工具使用限制
    exploration_tool_limits = {
        "max_tool_calls": TOOL_USAGE_LIMITS["exploration_planner"]["max_tool_calls"],
        "allowed_tools": exploration_tools
    }

    # Build react tool prompt
    react_tool_prompt = REACT_TOOL_PROMPT.format(max_tool_calls=exploration_tool_limits["max_tool_calls"])
    if no_historical_fixes:
        react_tool_prompt = "\n".join(
            line for line in react_tool_prompt.splitlines()
            if "search_historical_fixes" not in line
        )

    base_prompt = user_prompt + f"\n\n{react_tool_prompt}"
    full_prompt = base_prompt

    conversation_history = []
    try:
        response = call_llm(
            prompt=full_prompt,
            system_prompt=EXPLORATION_PLANNER_SYSTEM_PROMPT.format(num_combinations=num_combinations),
            temperature=0.3
        )

        tool_calls = []
        current_response = response
        tool_call_count = 0
        max_tool_calls = TOOL_USAGE_LIMITS["exploration_planner"]["max_tool_calls"]
        tool_call_history = []  # 跟踪已调用的工具
        conversation_history = ""  # 累积对话历史

        while tool_call_count < max_tool_calls:
            try:
                tool_call_data = extract_json_from_text(current_response)
                if tool_call_data and isinstance(tool_call_data, dict) and "tool_call" in tool_call_data:
                    tool_call = tool_call_data["tool_call"]
                    tool_name = tool_call["name"]
                    tool_params = tool_call["params"]

                    # 检查是否已经调用过相同的工具和参数
                    tool_call_key = f"{tool_name}:{json.dumps(tool_params, sort_keys=True)}"
                    if tool_call_key in tool_call_history:
                        logger.info(f"   ⚠️  Skipping duplicate tool call: {tool_name}")
                        # 告诉 LLM 已经调用过这个工具
                        tool_response_prompt = f"\n\n## Tool Response\n\n{json.dumps({'tool_response': {'success': True, 'message': 'You have already called this tool with the same parameters. Please use the previous result and continue with your analysis.'}}, indent=2)}\n\nPlease continue with your analysis using the information you already have."
                        current_response = call_llm(
                            prompt=full_prompt + conversation_history + tool_response_prompt,
                            system_prompt=EXPLORATION_PLANNER_SYSTEM_PROMPT.format(num_combinations=num_combinations),
                            temperature=0.3
                        )
                        continue

                    logger.info(f"   🛠️  Exploration Planner tool call: {tool_name}")

                    tool_result = execute_tool_call(
                        tool_name,
                        tool_params,
                        exploration_tool_limits
                    )

                    tool_calls.append(tool_call)
                    tool_call_history.append(tool_call_key)
                    tool_call_count += 1

                    # 当前轮次：使用完整结果
                    tool_response_prompt = "\n\n## Tool Response\n\n" + json.dumps({"tool_response": tool_result}, indent=2) + "\n\nPlease continue with your analysis."

                    # 构建工具调用提示
                    tool_call_prompt = f"\n\n## Tool Call {tool_call_count}\n"

                    current_response = call_llm(
                        prompt=full_prompt + conversation_history + tool_call_prompt + json.dumps({"tool_call": tool_call}, indent=2) + tool_response_prompt,
                        system_prompt=EXPLORATION_PLANNER_SYSTEM_PROMPT.format(num_combinations=num_combinations),
                        temperature=0.3
                    )

                    # 添加到历史记录时：截断结果，防止 token 爆炸
                    truncated_result = truncate_tool_result(tool_result, max_length=2000)
                    conversation_history += tool_call_prompt + json.dumps({"tool_call": tool_call}, indent=2)
                    conversation_history += "\n\n## Tool Response\n\n" + json.dumps({"tool_response": truncated_result}, indent=2)
                else:
                    break
            except Exception as e:
                logger.error(f"   ❌ Tool call parsing error: {e}")
                break

        plan_data = extract_json_from_text(current_response)

        candidates = []

        if isinstance(plan_data, list):
            for i, item in enumerate(plan_data[:num_combinations]):
                selected_functions = item.get("selected_functions", [])
                unified_root_cause = item.get("unified_root_cause", "")
                fix_strategy = item.get("fix_strategy", "")
                reasoning = item.get("reasoning", "")

                if not selected_functions:
                    selected_functions = [{"name": task_queue[0]["func_name"], "file": task_queue[0]["file_path"]}]
                    unified_root_cause = unified_root_cause or "Default selection of Top-1 function"
                    fix_strategy = fix_strategy or "Single-function repair"
                    reasoning = reasoning or "No functions selected, defaulting to Top-1 function"

                candidates.append({
                    "candidate_id": i,
                    "selected_functions": selected_functions,
                    "unified_root_cause": unified_root_cause,
                    "fix_strategy": fix_strategy,
                    "reasoning": reasoning,
                    "generated_patch": None,
                    "result_status": "PENDING",
                    "failure_feedback": ""
                })

        if not candidates:
            available_tasks = []

            # 过滤掉已放弃的函数
            for task in task_queue:
                task_func = task["func_name"]
                is_abandoned = False

                if abandoned_combinations:
                    for combo in abandoned_combinations:
                        if task_func in combo:
                            is_abandoned = True
                            break

                if not is_abandoned:
                    available_tasks.append(task)

            # 如果没有可用的函数，返回空列表
            if not available_tasks:
                logger.warning("⚠️ No available tasks, all functions are abandoned")
                return []

            # 从可用函数中生成默认候选
            for i in range(num_combinations):
                task_idx = min(i, len(available_tasks) - 1)
                candidates.append({
                    "candidate_id": i,
                    "selected_functions": [{"name": available_tasks[task_idx]["func_name"], "file": available_tasks[task_idx]["file_path"]}],
                    "unified_root_cause": available_tasks[task_idx].get("root_cause", "Unknown root cause"),
                    "fix_strategy": "Single-function repair",
                    "reasoning": "Default candidate generated due to parsing failure",
                    "generated_patch": None,
                    "result_status": "PENDING",
                    "failure_feedback": ""
                })

        logger.info(f"✅ Generated {len(candidates)} exploration candidate hypotheses")
        for i, candidate in enumerate(candidates):
            logger.info(f"   Candidate {i}: {[f['name'] for f in candidate['selected_functions']]}")

        if tool_calls:
            logger.info(f"   🛠️  Used {len(tool_calls)} tool calls")

        return candidates

    except Exception as e:
        logger.error(f"❌ Exploration Planner failed: {e}, generating default candidates")

        candidates = []
        available_tasks = []

        # 过滤掉已放弃的函数
        for task in task_queue:
            task_func = task["func_name"]
            is_abandoned = False

            if abandoned_combinations:
                for combo in abandoned_combinations:
                    if task_func in combo:
                        is_abandoned = True
                        break

            if not is_abandoned:
                available_tasks.append(task)

        # 如果没有可用的函数，返回空列表
        if not available_tasks:
            logger.warning("⚠️ No available tasks, all functions are abandoned")
            return []

        # 从可用函数中生成默认候选
        for i in range(num_combinations):
            task_idx = min(i, len(available_tasks) - 1)
            candidates.append({
                "candidate_id": i,
                "selected_functions": [{"name": available_tasks[task_idx]["func_name"], "file": available_tasks[task_idx]["file_path"]}],
                "unified_root_cause": available_tasks[task_idx].get("root_cause", "Unknown root cause"),
                "fix_strategy": "Single-function repair",
                "reasoning": f"Default candidate generated due to error: {str(e)}",
                "generated_patch": None,
                "result_status": "PENDING",
                "failure_feedback": str(e)
            })

        return candidates


def exploration_planner_node(state: BugFixState) -> Dict[str, Any]:
    """
    探索规划者节点：生成超越 Top-10 的探索组合
    """
    from agent.state import CaseSharedMemoryManager

    logger.info("🧠 [Exploration_Planner] Generating exploration candidate hypotheses...")

    syzid = state.get("syzid", "")
    bug_report = state.get("bug_report", "")
    testcase = state.get("testcase", "")
    task_queue = state.get("task_queue", [])
    fix_attempt_history = state.get("fix_attempt_history", [])

    memory_manager = CaseSharedMemoryManager()
    abandoned_combinations = memory_manager.get_abandoned_combinations()

    if abandoned_combinations:
        logger.info(f"   📋 Abandoned combinations to avoid: {len(abandoned_combinations)}")
        for combo in abandoned_combinations:
            logger.info(f"      - {list(combo)}")

    no_golden_fixes = state.get("no_golden_fixes", False)
    no_historical_fixes = state.get("no_historical_fixes", False)

    candidates = generate_exploration_combinations(
        syzid,
        bug_report,
        testcase,
        task_queue,
        fix_attempt_history,
        abandoned_combinations,
        num_combinations=2,
        no_golden_fixes=no_golden_fixes,
        no_historical_fixes=no_historical_fixes
    )

    logger.info(f"✅ Generated {len(candidates)} exploration candidate hypotheses")
    for i, candidate in enumerate(candidates):
        logger.info(f"   Candidate {i}: {[f['name'] for f in candidate['selected_functions']]}")

    previous_attempt_round = state.get("attempt_round", 0)
    new_attempt_round = previous_attempt_round + 1

    return {"candidate_hypotheses": candidates, "attempt_round": new_attempt_round}

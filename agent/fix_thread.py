"""
Fix Thread - 单个修复线程的完整闭环逻辑

每个修复线程包含完整的修复周期：
Generator → Reviewer → Validator → Judge → 循环或终止

线程状态：
- RUNNING: 正在运行
- SUCCESS: 修复成功
- ABANDONED: 放弃该函数组合
"""

import os
import json
import re
import time
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from agent.state import (
    FixThreadState, ThreadFailureRecord, CaseSharedMemoryManager,
    MAX_THREAD_ATTEMPTS, MAX_TOTAL_ATTEMPTS,
    MAX_THREAD_ATTEMPTS_WITH_FUNCTIONS, MAX_TOTAL_ATTEMPTS_WITH_FUNCTIONS
)
from agent.log_manager import get_logger
from utils.tool_text_parser import parse_text_tool_calls
from agent.prompts import (
    GENERATOR_SYSTEM_PROMPT, REVIEWER_SYSTEM_PROMPT, JUDGE_SYSTEM_PROMPT
)
from tool.function_calls import get_function_body, get_function_related_info, read_kernel_file, get_function_context
from tool.kernel_doc import query_kernel_api
from skill.sandbox_validator import validate_patch_in_sandbox
from utils.llm_interface import call_llm
from utils.file_ops import extract_json_from_text
from utils.patch_manager import save_patch_immediately
from config import Config

logger = get_logger("FixThread")

REACT_TOOL_PROMPT = """
## Tool Usage Instructions

You can use up to {max_tool_calls} tool calls to gather additional information.

Available tools:
1. **read_kernel_file(file_path, start_line=1, end_line=500)** - Read kernel source file
2. **get_function_context(file_path, func_name)** - Get function code and context
3. **query_kernel_api(func_name, file_path, include_implementation)** - Query kernel API docs
4. **query_golden_fixes(buggy_code, description, file_path, syzid="", kernel_commit="")** - Query historical golden fixes

**IMPORTANT**: Use query_golden_fixes if you need additional historical fix information.

Format: {{"tool_call": {{"name": "tool_name", "params": {{...}}}}}}
"""

TOOL_USAGE_LIMITS = {
    "generator": {"max_tool_calls": 6, "allowed_tools": ["read_kernel_file", "get_function_context", "query_kernel_api", "query_golden_fixes"]},
    "reviewer": {"max_tool_calls": 5, "allowed_tools": ["read_kernel_file", "get_function_context"]},
    "judge": {"max_tool_calls": 3, "allowed_tools": ["read_kernel_file", "get_function_context"]}
}

def execute_tool_call(tool_name: str, params: Dict, tool_limits: Dict) -> Dict:
    """Execute tool call with limits check"""
    if tool_name not in tool_limits["allowed_tools"]:
        return {"success": False, "error": f"Tool {tool_name} not allowed"}

    try:
        if tool_name == "read_kernel_file":
            from tool.function_calls import read_kernel_file
            return read_kernel_file.invoke(params)
        elif tool_name == "get_function_context":
            from tool.function_calls import get_function_context
            return get_function_context.invoke(params)
        elif tool_name == "query_kernel_api":
            return query_kernel_api.invoke(params)
        elif tool_name == "query_golden_fixes":
            from tool.function_calls import query_golden_fixes
            return query_golden_fixes.invoke(params)
        elif tool_name == "search_historical_fixes":
            from tool.function_calls import search_historical_fixes
            return search_historical_fixes.invoke(params)
        else:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def build_function_contexts(selected_functions: List[Dict], syzid: str, bug_report: str, testcase: str) -> Dict:
    """Build function contexts for all selected functions"""
    contexts = {}

    for func_info in selected_functions:
        func_name = func_info["name"]
        file_path = func_info["file"]

        try:
            func_body_result = get_function_body.invoke({
                "file_path": file_path,
                "func_name": func_name
            })
        except Exception as e:
            logger.error(f"Failed to get function body for {func_name}: {e}")
            func_body_result = {"target_code": "", "start_line": None}

        try:
            related_info_result = get_function_related_info.invoke({
                "file_path": file_path,
                "func_name": func_name
            })
        except Exception as e:
            logger.error(f"Failed to get related info for {func_name}: {e}")
            related_info_result = {"structs": {}, "macros": {}, "similar_funcs": []}

        try:
            api_result = query_kernel_api.invoke({
                "func_name": func_name,
                "file_path": file_path,
                "include_implementation": True
            })
        except Exception as e:
            logger.error(f"Failed to query kernel API for {func_name}: {e}")
            api_result = {"has_documentation": False, "documentation": "", "implementation": ""}

        contexts[func_name] = {
            "function_context": {
                "target_code": func_body_result.get("target_code", ""),
                "start_line": func_body_result.get("start_line"),
                "structs": related_info_result.get("structs", {}),
                "macros": related_info_result.get("macros", {}),
                "similar_funcs": related_info_result.get("similar_funcs", [])
            },
            "api_documentation": api_result.get("documentation", "") if api_result.get("has_documentation") else ""
        }

    return contexts


def generate_patch(thread_state: FixThreadState, syzid: str, bug_report: str, testcase: str,
                   kernel_commit: str,
                   memory_manager: CaseSharedMemoryManager, no_golden_fixes: bool = False,
                   no_historical_fixes: bool = False) -> Optional[Dict]:
    """Generate patch for the thread"""
    thread_id = thread_state["thread_id"]
    selected_functions = thread_state["selected_functions"]
    unified_root_cause = thread_state["unified_root_cause"]
    fix_strategy = thread_state["fix_strategy"]
    function_contexts = thread_state["function_contexts"]
    judge_suggestion = thread_state.get("judge_suggestion", "")
    judge_analysis = thread_state.get("judge_analysis", "")
    previous_patch = thread_state.get("current_patch", "")

    logger.info(f"   [Thread {thread_id}] Generating patch...")

    if not selected_functions:
        logger.error(f"   [Thread {thread_id}] No selected functions, cannot generate patch")
        return None

    historical_fixes_info = ""
    if no_historical_fixes:
        logger.info("🚫 [Generator] Historical similar-fix retrieval disabled by --no-historical-fixes")
        historical_fixes_info = "Historical similar-fix retrieval disabled by ablation setting.\n"
    else:
        # 固定调用 search_historical_fixes 工具获取历史修复信息
        logger.info("🔍 [Generator] Calling search_historical_fixes to get historical fix information")
        from tool.function_calls import search_historical_fixes
        historical_fixes_result = search_historical_fixes.invoke({
            "syzid": syzid,
            "bug_report": bug_report,
            "testcase": testcase,
            "rootcause": unified_root_cause,
            "strict_time_filter": True
        })

        if historical_fixes_result.get('success'):
            num_cases = historical_fixes_result.get('num_cases', 0)
            knowledge_text = historical_fixes_result.get('knowledge_text', '')
            logger.info(f"✅ [Generator] Found {num_cases} historical fix cases")
            if knowledge_text:
                # 截断过长的知识文本
                if len(knowledge_text) > 1500:
                    historical_fixes_info = f"## Historical Similar Fixes (Found {num_cases} cases)\n{knowledge_text[:1500]}...\n"
                else:
                    historical_fixes_info = f"## Historical Similar Fixes (Found {num_cases} cases)\n{knowledge_text}\n"
        else:
            logger.warning("⚠️ [Generator] Failed to retrieve historical fixes")
            historical_fixes_info = "## Historical Similar Fixes\nNo historical fix information available.\n"

    functions_context_text = ""
    for func_info in selected_functions:
        func_name = func_info["name"]
        file_path = func_info["file"]
        context_data = function_contexts.get(func_name, {})
        function_context = context_data.get("function_context", {})
        api_documentation = context_data.get("api_documentation", "")

        target_code = function_context.get("target_code", "")
        structs = function_context.get("structs", {})
        macros = function_context.get("macros", {})

        structs_text = "\n".join([f"// {name}\n{code}" for name, code in structs.items()])
        macros_text = "\n".join([f"#define {name} ..." for name in macros.keys()])

        functions_context_text += f"## Function: {func_name} @ {file_path}\n\n"
        functions_context_text += f"### Target Code\n```c\n{target_code}\n```\n\n"
        functions_context_text += f"### Structs\n{structs_text or '// No relevant structs'}\n\n"
        functions_context_text += f"### Macros\n{macros_text or '// No relevant macros'}\n\n"
        functions_context_text += f"### API Documentation\n{api_documentation or '// No API documentation'}\n\n"

    # 动态构建允许的工具列表
    allowed_tools = ["read_kernel_file", "get_function_context", "query_kernel_api"]
    if not no_golden_fixes:
        allowed_tools.append("query_golden_fixes")

    # 动态构建工具使用提示
    tool_list = ""
    tool_list += "1. **read_kernel_file(file_path, start_line=1, end_line=500)** - Read kernel source file\n"
    tool_list += "2. **get_function_context(file_path, func_name)** - Get function code and context\n"
    tool_list += "3. **query_kernel_api(func_name, file_path, include_implementation)** - Query kernel API docs\n"
    if not no_golden_fixes:
        tool_list += "4. **query_golden_fixes(buggy_code, description, file_path, syzid=\"\", kernel_commit=\"\")** - Query historical golden fixes with time filtering\n"

    # 构建工具调用格式示例
    tool_format_example = '''
Format: {
  "tool_call": {
    "name": "tool_name",
    "params": {...}
  }
}
'''

    golden_tool_note = ""
    if not no_golden_fixes:
        golden_tool_note = (
            "**IMPORTANT**: Use query_golden_fixes if you need additional historical fix information. "
            f"Always pass `kernel_commit=\"{kernel_commit}\"` so the tool only retrieves patches from earlier history."
        )

    react_tool_prompt = f"""
## Tool Usage Instructions

You can use up to {TOOL_USAGE_LIMITS['generator']['max_tool_calls']} tool calls to gather additional information.

Available tools:
{tool_list}

{golden_tool_note}

{tool_format_example}
"""

    # 动态构建工具使用限制
    tool_limits = {
        "max_tool_calls": TOOL_USAGE_LIMITS["generator"]["max_tool_calls"],
        "allowed_tools": allowed_tools
    }

    # 构建选中函数列表
    selected_functions_text = ""
    for i, func_info in enumerate(selected_functions, 1):
        selected_functions_text += f"{i}. **{func_info['name']}** @ `{func_info['file']}`\n"

    # 构建JSON格式示例
    json_example = '''
```json
{
  "approach": "<brief name for this fix approach>",
  "reasoning": "<brief explanation of the fix>",
  "patches": {
    "<function_name_1>": {
      "file_path": "<file path>",
      "modified_code": "<complete modified function code in C>"
    },
    "<function_name_2>": {
      "file_path": "<file path>",
      "modified_code": "<complete modified function code in C>"
    }
  }
}
```
'''

    # 构建Previous Attempt Information部分
    previous_attempt_info = ""
    if judge_suggestion or judge_analysis:
        previous_attempt_info = f"### Judge Suggestions\n{judge_suggestion or 'N/A'}\n\n### Judge Analysis\n{judge_analysis}\n\n### Previous Patch\n```c\n{previous_patch[:1000] if previous_patch else 'N/A'}\n```\n\n"

    # 构建user_prompt
    user_prompt = f"""
## Bug Information
- **SyzID**: {syzid}
- **Bug Report**: {bug_report[:2000]}
- **Test Case**: {testcase[:1000]}

## Unified Root Cause Analysis
{unified_root_cause}

## Fix Strategy
{fix_strategy}

## Selected Functions to Patch
{selected_functions_text}

## Historical Similar Fixes
{historical_fixes_info}

## Function Contexts
{functions_context_text}

## Previous Attempt Information
{previous_attempt_info}

Based on the above information, please generate a patch to fix the bug.

**CRITICAL REQUIREMENTS:**
1. Generate patches for ALL {len(selected_functions)} selected function(s)
2. Use the EXACT function name as the key in the "patches" object
3. Each patch must include file_path and modified_code

Output format:
{json_example}

JSON FORMAT REQUIREMENTS:
1. Output MUST be a valid JSON object ONLY - no other text
2. The "patches" object MUST contain EXACTLY {len(selected_functions)} function(s)
3. Use the EXACT function names from the "Selected Functions to Patch" list above as keys
4. The modified_code should be complete, compilable C code
"""

    user_prompt += f"\n\n{react_tool_prompt}"

    # 三次调用不同温度生成三个候选补丁
    temperatures = [0.3, 0.5, 0.7]
    all_candidates = []

    for i, temp in enumerate(temperatures):
        logger.info(f"   [Thread {thread_id}] Generating candidate {i+1}/3 with temperature={temp}")

        max_retries = 2
        for retry in range(max_retries):
            try:
                response = call_llm(
                    prompt=user_prompt,
                    system_prompt=GENERATOR_SYSTEM_PROMPT,
                    temperature=temp
                )

                logger.info(f"   [Thread {thread_id}] Candidate {i+1} response length: {len(response)}")

                # 处理工具调用
                current_response = response
                tool_call_count = 0
                max_tool_calls = TOOL_USAGE_LIMITS["generator"]["max_tool_calls"]
                tool_call_history = []  # 工具调用历史

                while tool_call_count < max_tool_calls:
                    patch_data = extract_json_from_text(current_response)
                    _tool_call = None
                    if patch_data and "tool_call" in patch_data:
                        _tc = patch_data["tool_call"]
                        _tool_call = {
                            "name": _tc.get("name") or _tc.get("tool", ""),
                            "params": _tc.get("params") or _tc.get("args", {}),
                        }
                    else:
                        # deepseek-flash and friends write the chat-template tool syntax as
                        # plain text; accept that too (utils/tool_text_parser.py)
                        _text_calls = parse_text_tool_calls(current_response)
                        if _text_calls:
                            _tool_call = _text_calls[0]

                    if _tool_call:
                        tool_name = _tool_call["name"]
                        tool_params = _tool_call["params"]

                        if tool_name == "query_golden_fixes" and "kernel_commit" not in tool_params:
                            tool_params["kernel_commit"] = kernel_commit

                        logger.info(f"   [Thread {thread_id}] Candidate {i+1} tool call: {tool_name}")

                        # 检查是否重复调用
                        duplicate_call = False
                        for history_call in tool_call_history:
                            if history_call["name"] == tool_name and history_call["params"] == tool_params:
                                # 重复调用，使用之前的结果
                                tool_result = history_call["result"]
                                duplicate_call = True
                                break

                        if not duplicate_call:
                            # 执行工具调用
                            tool_result = execute_tool_call(
                                tool_name,
                                tool_params,
                                tool_limits
                            )
                            # 记录到历史
                            tool_call_history.append({
                                "name": tool_name,
                                "params": tool_params,
                                "result": tool_result
                            })
                            tool_call_count += 1

                        # 构建工具调用历史提示（保留完整历史，突出显示结果）
                        history_prompt = "\n\n## Tool Call History\n\n"
                        if tool_call_history:
                            history_prompt += "**IMPORTANT: You have already called the following tools. DO NOT call them again. Use the results below instead.**\n\n"
                            history_prompt += f"Total tools called: {len(tool_call_history)}\n\n"

                            for i_call, call in enumerate(tool_call_history, 1):
                                tool_name = call['name']
                                tool_params = call.get('params', {})
                                tool_result = call.get('result', {})

                                # 突出显示工具名称
                                history_prompt += f"### {i_call}. {tool_name}\n"

                                # 显示参数
                                if tool_params:
                                    history_prompt += "**Parameters:**\n"
                                    for k, v in tool_params.items():
                                        history_prompt += f"- {k}: {v}\n"

                                # 突出显示工具返回结果
                                if tool_result:
                                    history_prompt += "**Result:**\n"

                                    # 对于搜索类工具，特别突出显示结果
                                    if tool_name in ['search_historical_fixes', 'query_golden_fixes']:
                                        if tool_result.get('success'):
                                            num_cases = tool_result.get('num_cases', 0)
                                            knowledge_text = tool_result.get('knowledge_text', '')
                                            history_prompt += f"- Found {num_cases} cases\n"
                                            if knowledge_text:
                                                # 截断过长的知识文本，但保留足够的信息
                                                if len(knowledge_text) > 3000:
                                                    history_prompt += f"- Key insights: {knowledge_text[:3000]}...\n"
                                                else:
                                                    history_prompt += f"- Key insights: {knowledge_text}\n"
                                        else:
                                            history_prompt += f"- Error: {tool_result.get('error', 'Unknown error')}\n"
                                    else:
                                        # 其他工具的结果
                                        result_str = json.dumps(tool_result, indent=2, ensure_ascii=False)
                                        # 只截断异常大的结果（超过2000字符）
                                        if len(result_str) > 3000:
                                            result_str = result_str[:3000] + "\n... [Content truncated due to length]\n"
                                        history_prompt += f"```json\n{result_str}\n```\n"
                                history_prompt += "\n"
                        else:
                            history_prompt += "You have not called any tools yet.\n"

                        # 构建剩余调用次数提示
                        remaining_calls = max_tool_calls - tool_call_count
                        history_prompt += f"\n**Remaining tool calls: {remaining_calls}**\n"
                        history_prompt += "\n**Tool Usage Guide:**\n"
                        history_prompt += "- DO NOT repeat the same tool call with the same parameters\n"
                        history_prompt += "- Use the results from previous tool calls\n"
                        history_prompt += "- Only call new tools if you need additional information\n"
                        if not no_golden_fixes:
                            history_prompt += "- Prioritize query_golden_fixes first when historical golden patch patterns are needed\n"

                        if tool_call_count >= max_tool_calls:
                            tool_response_prompt = "\n\n## Tool Response\n\n" + json.dumps({"tool_response": tool_result}, indent=2) + history_prompt + "\n\n**IMPORTANT: You have reached the maximum number of tool calls. You MUST now generate the patches directly.**"
                        else:
                            tool_response_prompt = "\n\n## Tool Response\n\n" + json.dumps({"tool_response": tool_result}, indent=2) + history_prompt + "\n\nPlease continue and generate the patches."

                        current_response = call_llm(
                            prompt=user_prompt + tool_response_prompt,
                            system_prompt=GENERATOR_SYSTEM_PROMPT,
                            temperature=temp
                        )
                    else:
                        break

                # 解析补丁数据
                patch_data = extract_json_from_text(current_response)

                if patch_data and "patches" in patch_data:
                    patches_dict = patch_data.get("patches", {})
                    approach = patch_data.get("approach", f"Strategy {i+1}")
                    reasoning = patch_data.get("reasoning", "")

                    # 验证 patches 是否包含所有选中的函数
                    valid_patches = {}
                    for func_info in selected_functions:
                        func_name = func_info["name"]
                        if func_name in patches_dict:
                            patch_info = patches_dict[func_name]
                            modified_code = patch_info.get("modified_code", "")

                            if modified_code:
                                if "```" in modified_code:
                                    modified_code = re.sub(r'```(?:c|C)?\s*', '', modified_code)
                                    modified_code = re.sub(r'```\s*$', '', modified_code)

                                valid_patches[func_name] = {
                                    "file_path": patch_info.get("file_path", func_info["file"]),
                                    "modified_code": modified_code.strip()
                                }

                    if valid_patches:
                        all_candidates.append({
                            "id": i + 1,
                            "approach": approach,
                            "reasoning": reasoning,
                            "patches": valid_patches,
                            "temperature": temp
                        })
                        logger.info(f"   [Thread {thread_id}] Candidate {i+1} valid: approach='{approach}', functions={list(valid_patches.keys())}")
                        break  # 成功，跳出重试循环
                    else:
                        logger.warning(f"   [Thread {thread_id}] Candidate {i+1} no valid patches, retrying...")
                else:
                    logger.warning(f"   [Thread {thread_id}] Candidate {i+1} parse failed, retrying...")

            except Exception as e:
                logger.error(f"   [Thread {thread_id}] Candidate {i+1} error: {e}")

    if all_candidates:
        logger.info(f"   [Thread {thread_id}] Generated {len(all_candidates)} valid candidates")
        return {
            "candidates": all_candidates,
            "is_multi_candidate": True
        }

    logger.warning(f"   [Thread {thread_id}] No valid candidates generated")
    return None


def review_patch(thread_state: FixThreadState, patch: Dict, bug_report: str) -> Dict:
    """
    Review the generated patch candidates and select the best one.
    Returns a dict with 'approved' and 'best_candidate' keys.
    """
    thread_id = thread_state["thread_id"]
    unified_root_cause = thread_state["unified_root_cause"]
    selected_functions = thread_state["selected_functions"]

    candidates = patch.get("candidates", [])
    is_multi_candidate = patch.get("is_multi_candidate", False)

    if not candidates:
        logger.warning(f"   [Thread {thread_id}] No candidates to review")
        return {"approved": False, "best_candidate": None}

    logger.info(f"   [Thread {thread_id}] Reviewing {len(candidates)} candidate(s)...")

    # 如果只有一个候选，直接审阅
    if len(candidates) == 1:
        candidate = candidates[0]
        patches = candidate.get("patches", {})  # 现在是字典格式
        patches_text = ""
        for func_name, patch_info in patches.items():
            patches_text += f"### Function: {func_name} @ {patch_info.get('file_path', 'unknown')}\n"
            patches_text += f"```c\n{patch_info.get('modified_code', '')}\n```\n\n"

        # 构建Selected Functions部分
        selected_functions_list = "\n".join([f"- {f['name']} @ {f['file']}" for f in selected_functions])

        review_prompt = f"""
## Bug Report
{bug_report[:2000]}

## Unified Root Cause Analysis
{unified_root_cause}

## Selected Functions
{selected_functions_list}

## Generated Patch
{patches_text}

## Patch Reasoning
{candidate.get('reasoning', 'N/A')[:500]}

Please review the above patch and evaluate whether it can effectively fix the bug.

Output format:
```json
{{
  "score": 0-100,
  "approved": true/false,
  "analysis": "Detailed review analysis"
}}
```
"""

        try:
            response = call_llm(
                prompt=review_prompt,
                system_prompt=REVIEWER_SYSTEM_PROMPT,
                temperature=0.1
            )

            review_data = extract_json_from_text(response)
            if review_data:
                score = review_data.get("score", 50)
                approved = review_data.get("approved", True)

                if score <= 10 and score > 0:
                    score = score * 10

                logger.info(f"   [Thread {thread_id}] Single candidate review: score={score}, approved={approved}")

                return {
                    "approved": approved or score >= 40,
                    "best_candidate": candidate,
                    "score": score
                }
        except Exception as e:
            logger.error(f"   [Thread {thread_id}] Review error: {e}")

        # 默认通过
        return {"approved": True, "best_candidate": candidate, "score": 50}

    # 多个候选：让 LLM 选出最好的一个
    candidates_text = ""
    for candidate in candidates:
        candidate_id = candidate.get("id", 0)
        approach = candidate.get("approach", "unknown")
        reasoning = candidate.get("reasoning", "N/A")
        patches = candidate.get("patches", {})  # 现在是字典格式

        candidates_text += f"\n### Candidate {candidate_id}: {approach}\n"
        candidates_text += f"**Reasoning**: {reasoning[:300]}\n\n"

        for func_name, patch_info in patches.items():
            file_path = patch_info.get("file_path", "unknown")
            code = patch_info.get("modified_code", "")
            # 只显示代码的前 500 字符
            code_preview = code[:500] + "..." if len(code) > 500 else code
            candidates_text += f"**Function**: {func_name} @ {file_path}\n"
            candidates_text += f"```c\n{code_preview}\n```\n\n"

    # 构建Selected Functions部分
    multi_review_selected_functions = "\n".join([f"- {f['name']} @ {f['file']}" for f in selected_functions])

    review_prompt = f"""
## Bug Report
{bug_report[:1500]}

## Unified Root Cause Analysis
{unified_root_cause[:1000]}

## Selected Functions
{multi_review_selected_functions}

## Candidate Patches ({len(candidates)} candidates)
{candidates_text}

Please evaluate ALL {len(candidates)} candidates and select the BEST one.

Output format:
```json
{{
  "best_candidate_id": <id of the best candidate>,
  "scores": {{
    "1": <score for candidate 1>,
    "2": <score for candidate 2>,
    "3": <score for candidate 3>
  }},
  "analysis": "Brief comparison and reasoning for selection"
}}
```

IMPORTANT:
- You MUST select ONE best candidate by its id
- Score each candidate from 0-100
- Consider: correctness, minimal changes, kernel coding style, safety
"""

    try:
        response = call_llm(
            prompt=review_prompt,
            system_prompt=REVIEWER_SYSTEM_PROMPT,
            temperature=0.1
        )

        review_data = extract_json_from_text(response)
        if review_data:
            best_id = review_data.get("best_candidate_id", 1)
            scores = review_data.get("scores", {})

            logger.info(f"   [Thread {thread_id}] Multi-candidate review: best_id={best_id}, scores={scores}")
            logger.info(f"   [Thread {thread_id}] Analysis: {review_data.get('analysis', 'N/A')[:200]}")

            # 找到最佳候选
            best_candidate = None
            for candidate in candidates:
                if candidate.get("id") == best_id:
                    best_candidate = candidate
                    break

            if not best_candidate:
                best_candidate = candidates[0]

            best_score = scores.get(str(best_id), 50)
            if best_score <= 10 and best_score > 0:
                best_score = best_score * 10

            return {
                "approved": best_score >= 40,
                "best_candidate": best_candidate,
                "score": best_score,
                "all_scores": scores
            }
    except Exception as e:
        logger.error(f"   [Thread {thread_id}] Multi-candidate review error: {e}")

    # 默认选择第一个候选
    logger.warning(f"   [Thread {thread_id}] Review failed, selecting first candidate by default")
    return {"approved": True, "best_candidate": candidates[0], "score": 50}


def validate_patch(thread_state: FixThreadState, patch: Dict, syzid: str, kernel_commit: str,
                   syz_commit: str, bug_config_path: str, worker_config: Dict,
                   memory_manager: CaseSharedMemoryManager) -> Dict:
    """Validate the patch in sandbox"""
    thread_id = thread_state["thread_id"]
    selected_functions = thread_state["selected_functions"]

    logger.info(f"   [Thread {thread_id}] Validating patch...")

    patches_list = patch.get("patches", [])
    if not patches_list:
        modified_code = patch.get("modified_code", "")
        if modified_code and selected_functions:
            patches_list = [{
                "function_name": selected_functions[0]["name"],
                "file_path": selected_functions[0]["file"],
                "modified_code": modified_code
            }]

    if not patches_list:
        return {"status": "NO_PATCH", "log": "No patches found"}

    memory_manager.validator_start()

    try:
        testcase_path = os.path.join(Config.DATASETS_DIR, "testcases", f"{syzid}.syz")

        validation_patches = []
        for p in patches_list:
            validation_patches.append({
                "file_path": p.get("file_path", ""),
                "target_name": p.get("function_name", ""),
                "target_type": "function",
                "patch": {
                    "modified_code": p.get("modified_code", "")
                }
            })

        first_patch = patches_list[0]
        target_file = first_patch["file_path"]
        target_func = first_patch["function_name"]

        if len(validation_patches) == 1:
            patches_param = None
            patch_content = first_patch["modified_code"]
        else:
            patches_param = validation_patches
            patch_content = first_patch["modified_code"]

        bug_keywords = ["KASAN"]
        oracle_file = os.path.join(Config.DATASETS_DIR, "oracle_content.txt")
        if os.path.exists(oracle_file):
            try:
                with open(oracle_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and ':' in line:
                            parts = line.split(':', 1)
                            if parts[0].strip() == syzid:
                                bug_keywords.append(parts[1].strip())
            except:
                pass

        patch_id = patch.get("patch_id", f"thread_{thread_id}")

        result = validate_patch_in_sandbox(
            patch_content=patch_content,
            patch_id=patch_id,
            kernel_commit=kernel_commit,
            target_file=target_file,
            target_func=target_func,
            syz_commit=syz_commit,
            bug_config_path=bug_config_path,
            syz_reproducer_path=testcase_path,
            bug_keywords=bug_keywords,
            worker_config=worker_config,
            patches=patches_param
        )

        status = result.get("status", "UNKNOWN")
        log = result.get("log", "")[:500]

        logger.info(f"   [Thread {thread_id}] Validation result: {status}")

        return {"status": status, "log": log}
    except Exception as e:
        logger.error(f"   [Thread {thread_id}] Validation error: {e}")
        return {"status": "VALIDATION_ERROR", "log": str(e)}
    finally:
        memory_manager.validator_end()


def judge_failure(thread_state: FixThreadState, validation_result: Dict, syzid: str,
                  bug_report: str, memory_manager: CaseSharedMemoryManager) -> Dict:
    """
    Judge the failure and decide next action

    Returns:
        Dict with keys:
        - decision: "DECISION_A" (tweak code), "DECISION_B" (revise root cause), "DECISION_C" (abandon)
        - analysis: Detailed analysis
        - suggestion: For DECISION_A: code changes needed
        - new_root_cause: For DECISION_B: revised root cause
    """
    thread_id = thread_state["thread_id"]
    selected_functions = thread_state["selected_functions"]
    unified_root_cause = thread_state["unified_root_cause"]
    validation_status = validation_result.get("status", "UNKNOWN")
    validation_log = validation_result.get("log", "")
    attempts = thread_state["attempts"]

    functions = tuple(sorted([f['name'] for f in selected_functions]))
    attempt_key = thread_state.get("attempt_key", functions)
    failures = memory_manager.get_failures(attempt_key)

    logger.info(f"   [Thread {thread_id}] Judging failure...")

    history_info = ""
    if failures:
        history_info = "## Previous Attempts\n"
        for i, f in enumerate(failures[-3:]):
            history_info += f"### Attempt {i+1}\n"
            history_info += f"- Decision: {f.get('decision', 'N/A')}\n"
            history_info += f"- Error: {f.get('error', 'N/A')[:100]}\n"

    # 处理多函数补丁格式
    patches_list = thread_state.get("current_patches", [])
    if patches_list:
        patches_text = "## Generated Patches\n"
        for p in patches_list:
            patches_text += f"### Function: {p['function_name']} @ {p['file_path']}\n"
            patches_text += f"```c\n{p['modified_code'][:1000]}\n```\n\n"
    else:
        current_patch = thread_state.get("current_patch", "")
        patches_text = f"## Generated Patch\n```c\n{current_patch[:1500]}\n```\n"

    react_tool_prompt = REACT_TOOL_PROMPT.format(max_tool_calls=TOOL_USAGE_LIMITS["judge"]["max_tool_calls"])

    # 构建Selected Functions部分
    judge_selected_functions = "\n".join([f"- {f['name']} @ {f['file']}" for f in selected_functions])

    judge_prompt = f"""
## Bug Information
- **SyzID**: {syzid}
- **Bug Report**: {bug_report[:2000]}

## Current Attempt
- **Selected Functions**: {judge_selected_functions}
- **Root Cause Hypothesis**: {unified_root_cause}
- **Validation Status**: {validation_status}
- **Error Log**: {validation_log[:500]}
- **Attempt Number**: {attempts}/{MAX_THREAD_ATTEMPTS}

{history_info}

{patches_text}

Please analyze the failure and decide the next action.

DECISION OPTIONS:
- **DECISION_A (TWEAK_CODE)**: The root cause is correct, but the patch implementation has issues.
  Examples: syntax errors, wrong variable names, missing edge cases.
  Action: Generate a new patch with fixes.

- **DECISION_B (REVISE_ROOT_CAUSE)**: The root cause hypothesis is fundamentally wrong.
  Examples: patch compiles and runs but crash still occurs; the fix addresses wrong issue.
  Action: Propose a new root cause hypothesis.

- **DECISION_C (ABANDON_FUNCTION)**: This function combination is not the right target.
  Examples: multiple failed attempts; crash location doesn't match function.
  Action: Give up on this function combination.

Decision Strategy:
1. If the patch has syntax/compilation errors → DECISION_A
2. If the patch compiles but crash still occurs with same pattern → DECISION_B
3. If multiple attempts have failed with different approaches → DECISION_C
4. Consider the attempt limit: {MAX_THREAD_ATTEMPTS} attempts max per combination

Output format:
```json
{{
  "decision": "DECISION_A" or "DECISION_B" or "DECISION_C",
  "reasoning": "Step-by-step analysis",
  "analysis": "Detailed explanation",
  "suggested_fix": "For DECISION_A: specific code changes",
  "new_root_cause": "For DECISION_B: revised root cause"
}}
```
"""

    judge_prompt += f"\n\n{react_tool_prompt}"

    try:
        response = call_llm(
            prompt=judge_prompt,
            system_prompt=JUDGE_SYSTEM_PROMPT,
            temperature=0.1
        )

        judge_data = extract_json_from_text(response)
        if judge_data:
            decision = judge_data.get("decision", "DECISION_C")
            analysis = judge_data.get("analysis", "")
            suggestion = judge_data.get("suggested_fix", "")
            new_root_cause = judge_data.get("new_root_cause", "")

            logger.info(f"   [Thread {thread_id}] Decision: {decision}")

            return {
                "decision": decision,
                "analysis": analysis,
                "suggestion": suggestion,
                "new_root_cause": new_root_cause
            }
        else:
            return {"decision": "DECISION_C", "analysis": "Failed to parse judge response"}
    except Exception as e:
        logger.error(f"   [Thread {thread_id}] Judge error: {e}")
        return {"decision": "DECISION_C", "analysis": str(e)}


def run_fix_thread(
    thread_state: FixThreadState,
    syzid: str,
    kernel_commit: str,
    syz_commit: str,
    bug_config_path: str,
    bug_report: str,
    testcase: str,
    worker_config: Dict,
    memory_manager: CaseSharedMemoryManager,
    no_golden_fixes: bool = False,
    no_historical_fixes: bool = False
) -> FixThreadState:
    """
    Run a single fix thread with complete closed-loop logic

    The thread runs: Generator → Reviewer → Validator → Judge → Loop or Terminate
    """
    thread_id = thread_state["thread_id"]
    selected_functions = thread_state["selected_functions"]
    functions = tuple(sorted([f['name'] for f in selected_functions]))
    attempt_key = thread_state.get("attempt_key", functions)

    # 注意：组合已经在 run_parallel_fix_threads 中注册过了
    # 这里只需要检查是否被放弃
    if memory_manager.is_combination_abandoned(attempt_key):
        logger.warning(f"   [Thread {thread_id}] Attempt {attempt_key} already abandoned, skipping")
        thread_state["status"] = "SKIPPED"
        return thread_state

    logger.info(f"🔄 [Thread {thread_id}] Starting fix thread for: {[f['name'] for f in selected_functions]}")
    # 根据是否使用函数组合选择尝试次数限制
    function_combination = thread_state.get("function_combination", False)
    if function_combination:
        max_thread_attempts = MAX_THREAD_ATTEMPTS_WITH_FUNCTIONS
        max_total_attempts = MAX_TOTAL_ATTEMPTS_WITH_FUNCTIONS
    else:
        max_thread_attempts = MAX_THREAD_ATTEMPTS
        max_total_attempts = MAX_TOTAL_ATTEMPTS

    logger.info(f"   [Thread {thread_id}] Total combinations tried so far: {memory_manager.get_total_attempts()}/{max_total_attempts}")

    thread_state["function_contexts"] = build_function_contexts(
        selected_functions, syzid, bug_report, testcase
    )

    while True:
        if memory_manager.should_stop():
            logger.info(f"   [Thread {thread_id}] Stop signal received, stopping")
            thread_state["status"] = "STOPPED"
            return thread_state

        if memory_manager.is_success():
            logger.info(f"   [Thread {thread_id}] Another thread succeeded, stopping")
            thread_state["status"] = "STOPPED"
            return thread_state

        if not function_combination and not memory_manager.can_continue(False):
            logger.info(f"   [Thread {thread_id}] Global attempt limit reached, stopping")
            thread_state["status"] = "STOPPED"
            return thread_state

        if thread_state["attempts"] >= max_thread_attempts:
            logger.info(f"   [Thread {thread_id}] Thread attempt limit reached, abandoning")
            thread_state["status"] = "ABANDONED"
            memory_manager.abandon_combination(attempt_key)
            return thread_state

        thread_state["attempts"] += 1
        logger.info(f"   [Thread {thread_id}] Attempt {thread_state['attempts']}/{max_thread_attempts}")

        patch = generate_patch(
            thread_state, syzid, bug_report, testcase, kernel_commit, memory_manager,
            no_golden_fixes, no_historical_fixes
        )
        if not patch:
            logger.warning(f"   [Thread {thread_id}] Failed to generate patch")
            thread_state["attempts"] -= 1
            thread_state["status"] = "ABANDONED"
            memory_manager.abandon_combination(attempt_key)
            return thread_state

        # 存储补丁信息
        # 审阅补丁候选，选出最佳的一个
        review_result = review_patch(thread_state, patch, bug_report)
        approved = review_result.get("approved", False)
        best_candidate = review_result.get("best_candidate")

        if not approved or not best_candidate:
            logger.warning(f"   [Thread {thread_id}] All candidates rejected by reviewer, retrying")
            continue

        # 使用最佳候选进行验证
        best_patches = best_candidate.get("patches", {})  # 现在是字典格式
        if best_patches:
            # 将字典格式转换为列表格式用于验证
            patches_list = []
            for func_name, patch_info in best_patches.items():
                patches_list.append({
                    "function_name": func_name,
                    "file_path": patch_info.get("file_path", ""),
                    "modified_code": patch_info.get("modified_code", "")
                })
            thread_state["current_patches"] = patches_list
            thread_state["current_patch"] = patches_list[0].get("modified_code", "") if len(patches_list) == 1 else None
            thread_state["current_patch_id"] = f"thread_{thread_id}_attempt_{thread_state['attempts']}_{int(time.time())}"

            # 保存补丁到文件
            patch_id = thread_state["current_patch_id"]
            for i, p in enumerate(patches_list):
                func_name = p.get("function_name", "unknown")
                file_path = p.get("file_path", "unknown")
                modified_code = p.get("modified_code", "")

                patch_record = {
                    "patch_id": f"{patch_id}_{func_name}",
                    "modified_code": modified_code,
                    "temperature": best_candidate.get("temperature", 0.5),
                    "generation_round": thread_state["attempts"],
                    "reasoning": best_candidate.get("reasoning", ""),
                    "approach": best_candidate.get("approach", ""),
                    "function_name": func_name,
                    "file_path": file_path
                }

                save_patch_immediately(
                    syzid=syzid,
                    patch_id=patch_record["patch_id"],
                    func_name=func_name,
                    file_path=file_path,
                    modified_code=modified_code,
                    reasoning=best_candidate.get("reasoning", ""),
                    temperature=best_candidate.get("temperature", 0.5),
                    generation_round=thread_state["attempts"]
                )

                if "generated_patches" not in thread_state:
                    thread_state["generated_patches"] = []
                thread_state["generated_patches"].append(patch_record)

        logger.info(f"   [Thread {thread_id}] Selected best candidate: {best_candidate.get('approach', 'unknown')} (score: {review_result.get('score', 'N/A')})")

        # 构建用于验证的 patch 对象
        patch_for_validation = {
            "patches": patches_list if best_patches else [],
            "reasoning": best_candidate.get("reasoning", ""),
            "approach": best_candidate.get("approach", "")
        }

        validation_result = validate_patch(
            thread_state, patch_for_validation, syzid, kernel_commit,
            syz_commit, bug_config_path, worker_config, memory_manager
        )

        status = validation_result.get("status", "UNKNOWN")

        if status in ["FIXED", "FIXED_OR_TIMEOUT"]:
            logger.info(f"   [Thread {thread_id}] ✅ SUCCESS!")
            thread_state["status"] = "SUCCESS"
            memory_manager.mark_success(patch_for_validation, selected_functions)
            return thread_state

        judge_result = judge_failure(
            thread_state, validation_result, syzid, bug_report, memory_manager
        )

        decision = judge_result.get("decision", "DECISION_C")
        analysis = judge_result.get("analysis", "")
        suggestion = judge_result.get("suggested_fix", "")
        new_root_cause = judge_result.get("new_root_cause", "")

        # 构建补丁摘要用于失败记录
        patch_summary = ""
        if best_patches:
            # best_patches 是字典格式，需要正确遍历
            func_summaries = []
            for func_name, patch_info in list(best_patches.items())[:3]:
                func_summaries.append(f"{func_name}@{patch_info.get('file_path', 'unknown')}")
            patch_summary = "; ".join(func_summaries)
        else:
            patch_summary = (patch_for_validation.get("reasoning", "") or "")[:500]

        failure_record = {
            "attempt": thread_state["attempts"],
            "root_cause": thread_state["unified_root_cause"],
            "patch": patch_summary,
            "error": validation_result.get("log", "")[:200],
            "decision": decision,
            "analysis": analysis
        }
        thread_state["failure_history"].append(failure_record)
        memory_manager.add_failure(attempt_key, failure_record)

        if decision == "DECISION_A":
            thread_state["code_tweaks"] += 1
            thread_state["judge_suggestion"] = suggestion
            thread_state["judge_analysis"] = analysis
            logger.info(f"   [Thread {thread_id}] Tweaking code (attempt {thread_state['code_tweaks']})")
            continue

        elif decision == "DECISION_B":
            thread_state["rc_revisions"] += 1
            if new_root_cause:
                thread_state["unified_root_cause"] = new_root_cause
            thread_state["judge_suggestion"] = ""
            thread_state["judge_analysis"] = analysis
            logger.info(f"   [Thread {thread_id}] Revising root cause (attempt {thread_state['rc_revisions']})")
            continue

        else:
            logger.info(f"   [Thread {thread_id}] Abandoning function combination")
            thread_state["status"] = "ABANDONED"
            memory_manager.abandon_combination(attempt_key)
            return thread_state


def _is_function_combination_candidate(candidate: Dict, selected_functions: List[Dict]) -> bool:
    if candidate.get("function_combination_mode"):
        return True
    return bool(selected_functions and selected_functions[0].get("status") == "given")


def _candidate_attempt_key(candidate: Dict, selected_functions: List[Dict], fallback_index: int) -> tuple:
    functions = tuple(sorted([f["name"] for f in selected_functions]))
    if _is_function_combination_candidate(candidate, selected_functions):
        candidate_id = candidate.get("id") or f"candidate_{fallback_index}"
        return ("function_combination", functions, candidate_id)
    return functions


def run_parallel_fix_threads(
    candidate_hypotheses: List[Dict],
    syzid: str,
    kernel_commit: str,
    syz_commit: str,
    bug_config_path: str,
    bug_report: str,
    testcase: str,
    memory_manager: CaseSharedMemoryManager,
    task_queue: List[Dict] = None,
    no_golden_fixes: bool = False,
    no_historical_fixes: bool = False
) -> List[FixThreadState]:
    """
    Run multiple fix threads in parallel with dynamic thread filling

    When a thread is abandoned, a new candidate will be generated to fill the slot.
    This maximizes resource utilization.
    """
    from agent.parallel_nodes import generate_multiple_candidate_hypotheses

    logger.info(f"🚀 Starting parallel fix threads with dynamic filling")
    logger.info(f"   Initial candidates: {len(candidate_hypotheses)}")
    logger.info(f"   Max total attempts: {MAX_TOTAL_ATTEMPTS}")

    thread_states = []
    for i, candidate in enumerate(candidate_hypotheses):
        selected_functions = candidate.get("selected_functions", [])
        functions = tuple(sorted([f['name'] for f in selected_functions]))
        function_combination = _is_function_combination_candidate(candidate, selected_functions)
        attempt_key = _candidate_attempt_key(candidate, selected_functions, i)

        if memory_manager.is_combination_abandoned(attempt_key):
            logger.info(f"   Thread {i}: Attempt already abandoned, skipping: {attempt_key}")
            continue

        if memory_manager.is_combination_tried(attempt_key):
            logger.info(f"   Thread {i}: Attempt already tried, skipping: {attempt_key}")
            continue

        memory_manager.register_combination(attempt_key)

        thread_state = {
            "thread_id": i,
            "selected_functions": selected_functions,
            "unified_root_cause": candidate.get("unified_root_cause", ""),
            "fix_strategy": candidate.get("fix_strategy", ""),
            "reasoning": candidate.get("reasoning", ""),
            "attempts": 0,
            "code_tweaks": 0,
            "rc_revisions": 0,
            "status": "RUNNING",
            "current_patch": None,
            "function_combination": function_combination,
            "attempt_key": attempt_key,
            "candidate_id": candidate.get("id"),
            "current_patches": None,
            "current_patch_id": None,
            "failure_history": [],
            "function_contexts": {},
            "judge_suggestion": None,
            "judge_analysis": None
        }
        thread_states.append(thread_state)

    if not thread_states:
        logger.warning("No valid threads to run")
        return []

    results = []
    thread_id_counter = len(thread_states)
    pending_candidates = []

    num_workers = min(len(thread_states), len(Config.WORKER_POOL_CONFIG))

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {}
        worker_assignments = {}
        idle_workers = []  # 空闲 worker 列表

        for i, thread_state in enumerate(thread_states):
            worker_idx = i % len(Config.WORKER_POOL_CONFIG)
            worker_config = Config.WORKER_POOL_CONFIG[worker_idx]

            future = executor.submit(
                    run_fix_thread,
                    thread_state,
                    syzid,
                    kernel_commit,
                    syz_commit,
                    bug_config_path,
                    bug_report,
                    testcase,
                    worker_config,
                    memory_manager,
                    no_golden_fixes,
                    no_historical_fixes
                )
            futures[future] = (thread_state, worker_idx)
            worker_assignments[worker_idx] = future
            logger.info(f"   Submitted Thread {thread_state['thread_id']}: {[f['name'] for f in thread_state['selected_functions']]} -> Worker-{worker_idx}")

        for future in as_completed(futures):
            thread_state, worker_idx = futures[future]

            try:
                result = future.result()
                results.append(result)

                if result["status"] == "SUCCESS":
                    logger.info(f"🎉 Thread {result['thread_id']} succeeded!")
                    memory_manager.signal_stop()

                    for f in list(futures.keys()):
                        if f != future:
                            f.cancel()

                    active_validators = memory_manager.get_active_validators()
                    if active_validators > 0:
                        logger.info(f"   ⏳ Waiting for {active_validators} active validators to finish...")
                        wait_start = time.time()
                        max_wait = 180
                        while not memory_manager.can_stop_now():
                            time.sleep(1)
                            if time.time() - wait_start > max_wait:
                                logger.warning(f"   ⚠️ Wait timeout ({max_wait}s), forcing stop")
                                break
                    logger.info(f"   ✅ All validators finished, stopping")
                    break

                elif result["status"] == "ABANDONED":
                    logger.info(f"   Thread {result['thread_id']} abandoned, Worker-{worker_idx} is now idle")
                    if result.get("function_combination"):
                        logger.info("   Function-combination mode: graph planner will decide whether to generate the next root-cause candidate")
                        continue

                    if memory_manager.can_continue():
                        idle_workers.append(worker_idx)
                        logger.info(f"   🔄 Current idle workers: {len(idle_workers)}")

                        original_task_queue = task_queue if task_queue else [
                            {"func_name": f["name"], "file_path": f["file"], "root_cause": ""}
                            for f in result["selected_functions"]
                        ]

                        all_failures = memory_manager.get_all_failures()
                        fix_attempt_history = []
                        for combo_key, failures in all_failures.items():
                            for fail in failures:
                                func_names = [n.strip() for n in combo_key.strip("()").replace("'", "").split(",") if n.strip()]
                                fix_attempt_history.append({
                                    "attempt_round": fail.get("attempt", 0),
                                    "candidate_id": 0,
                                    "Functions": [{"name": n, "file": ""} for n in func_names],
                                    "RootCauseHypothesis": fail.get("root_cause", ""),
                                    "Patch": fail.get("patch", ""),
                                    "Result": "FAILED",
                                    "ErrorLogExcerpt": fail.get("error", "")[:200],
                                    "JudgeDecision": fail.get("decision", ""),
                                    "JudgeSuggestion": fail.get("analysis", "")
                                })

                        num_new_candidates = len(idle_workers)
                        logger.info(f"   🚀 Generating {num_new_candidates} new candidates for idle workers")

                        new_candidates = generate_multiple_candidate_hypotheses(
                            syzid=syzid,
                            bug_report=bug_report,
                            testcase=testcase,
                            task_queue=original_task_queue,
                            fix_attempt_history=fix_attempt_history,
                            num_candidates=num_new_candidates,
                            abandoned_combinations=memory_manager.get_abandoned_combinations(),
                            tried_combinations=memory_manager.get_tried_combinations(),
                            no_golden_fixes=no_golden_fixes,
                            no_historical_fixes=no_historical_fixes
                        )

                        if new_candidates:
                            logger.info(f"   ✅ Generated {len(new_candidates)} new candidates")

                            for i, new_candidate in enumerate(new_candidates):
                                if not idle_workers:
                                    logger.info(f"   ⏭️ No more idle workers available")
                                    break

                                worker_to_use = idle_workers.pop(0)
                                new_functions = new_candidate.get("selected_functions", [])
                                new_functions_tuple = tuple(sorted([f['name'] for f in new_functions]))

                                if not memory_manager.is_combination_abandoned(new_functions_tuple) and not memory_manager.is_combination_tried(new_functions_tuple):
                                    memory_manager.register_combination(new_functions_tuple)

                                    new_thread_state = {
                                        "thread_id": thread_id_counter,
                                        "selected_functions": new_functions,
                                        "unified_root_cause": new_candidate.get("unified_root_cause", ""),
                                        "fix_strategy": new_candidate.get("fix_strategy", ""),
                                        "reasoning": new_candidate.get("reasoning", ""),
                                        "attempts": 0,
                                        "code_tweaks": 0,
                                        "rc_revisions": 0,
                                        "status": "RUNNING",
                                        "current_patch": None,
                                        "current_patches": None,
                                        "current_patch_id": None,
                                        "failure_history": [],
                                        "function_contexts": {},
                                        "judge_suggestion": None,
                                        "judge_analysis": None
                                    }

                                    thread_id_counter += 1
                                    worker_config = Config.WORKER_POOL_CONFIG[worker_to_use]

                                    new_future = executor.submit(
                                        run_fix_thread,
                                        new_thread_state,
                                        syzid,
                                        kernel_commit,
                                        syz_commit,
                                        bug_config_path,
                                        bug_report,
                                        testcase,
                                        worker_config,
                                        memory_manager,
                                        no_golden_fixes,
                                        no_historical_fixes
                                    )
                                    futures[new_future] = (new_thread_state, worker_to_use)
                                    logger.info(f"   ✅ Filled Worker-{worker_to_use} with Thread {new_thread_state['thread_id']}: {[f['name'] for f in new_functions]}")
                                else:
                                    logger.info(f"   ⏭️ Candidate already tried or abandoned, skipping")
                                    idle_workers.append(worker_to_use)
                        else:
                            logger.info(f"   ⏭️ No new candidates generated")
                    else:
                        logger.info(f"   ⏹️ Cannot continue (attempts limit reached)")

            except Exception as e:
                logger.error(f"❌ Thread {thread_state['thread_id']} failed: {e}")
                thread_state["status"] = "ERROR"
                results.append(thread_state)

    logger.info(f"🏁 All threads completed. Total threads run: {len(results)}")
    return results

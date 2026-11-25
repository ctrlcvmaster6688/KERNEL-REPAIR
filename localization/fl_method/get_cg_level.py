import subprocess
import re
from collections import defaultdict
from typing import List, Dict, Tuple


def analyze_function_hierarchy(file_path: str, target_funcs: List[str]) -> Dict[str, int]:

    try:
        result = subprocess.run(
            ["cflow", "--all", "--print-level", file_path],
            capture_output=True,
            text=True,
            check=True
        )
        cflow_output = result.stdout
    except subprocess.CalledProcessError as e:
        print(f"cflow 执行失败: {e}")
        return {func: 0 for func in target_funcs}

    reverse_call_graph = defaultdict(list)  # callee -> list of callers
    level_func_list = [] 

    level_func_pattern = re.compile(r"^\{\s*(\d+)\}\s+([\w\d_]+)")

    for line in cflow_output.splitlines():
        match = level_func_pattern.match(line)
        if not match:
            continue
        level = int(match.group(1))
        func = match.group(2)
        level_func_list.append((level, func))

    call_stack = []
    for level, func in level_func_list:
        if level == 0:
            call_stack = [func]
        else:
            
            call_stack = call_stack[:level]
            if level >= 1:
                caller = call_stack[level - 1]
                reverse_call_graph[func].append(caller)
            call_stack.append(func)
    level_cache = {}
    visiting = set()

    def get_func_level(func: str) -> int:
        if func in visiting:
            return 0  
        if func in level_cache:
            return level_cache[func]
        callers = reverse_call_graph.get(func, [])
        if not callers:
            level_cache[func] = 1
            return 1
        visiting.add(func)
        max_parent_level = 0
        for caller in callers:
            caller_level = get_func_level(caller)
            if caller_level == 0:
                max_parent_level = 0
                break
            max_parent_level = max(max_parent_level, caller_level)
        visiting.remove(func)
        level_cache[func] = max_parent_level + 1 if max_parent_level > 0 else 0
        return level_cache[func]

 
    all_funcs = {func for _, func in level_func_list}
    result = {}
    for func in target_funcs:
        if func not in all_funcs:
            result[func] = 0 
        else:
            result[func] = get_func_level(func)

    return result
def get_called_count_from_reverse_cflow(lines_reverse: List[str], func: str) -> int:

    try:
        called_count = 0
        func_line_idx = None

       
        for i, line in enumerate(lines_reverse):
            if line.lstrip() == line and re.search(rf'\b{re.escape(func)}\b', line):
                func_line_idx = i
                break

        if func_line_idx is None:
            return 0  
        base_indent = 0

     
        for j in range(func_line_idx + 1, len(lines_reverse)):
            line = lines_reverse[j]
            indent = len(line) - len(line.lstrip())
            if indent == base_indent:
                break  
            if indent > base_indent:
                called_count += 1

        return called_count
    except Exception:
        return 0

def get_call_counts(filepath: str, function_names: List[str]) -> Dict[str, Tuple[int, int]]:
    
    try:
        proc_forward = subprocess.run(['cflow', filepath], capture_output=True, text=True, check=True)
        lines_forward = proc_forward.stdout.splitlines()
    except subprocess.CalledProcessError:
        lines_forward = []

    try:
        proc_reverse = subprocess.run(['cflow', '--reverse', filepath], capture_output=True, text=True, check=True)
        lines_reverse = proc_reverse.stdout.splitlines()
    except subprocess.CalledProcessError:
        lines_reverse = []

    results = {}

    for func in function_names:
        try:
         
            call_count = 0
            found = False
            base_indent = None
            pattern_func = re.compile(rf"^\s*\d*\s*{re.escape(func)}\(\)")

            for line in lines_forward:
                if not found:
                    if pattern_func.search(line):
                        found = True
                        base_indent = len(line) - len(line.lstrip())
                        continue
                else:
                    indent = len(line) - len(line.lstrip())
                    if indent > base_indent:
                        if re.search(r'\b\w+\(\)', line.strip()):
                            call_count += 1
                    else:
                        break
        except Exception:
            call_count = 0

        called_count = get_called_count_from_reverse_cflow(lines_reverse, func)

        results[func] = (call_count, called_count)

    return results


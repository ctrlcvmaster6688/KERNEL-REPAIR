from langchain_core.tools import tool
from typing import Dict, Optional
import subprocess
import os
import re
import sys

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config import Config

_imported_modules = {}

def _get_logger():
    if "logger" not in _imported_modules:
        from agent.log_manager import get_logger
        _imported_modules["logger"] = get_logger("KernelDocTool")
    return _imported_modules["logger"]

MAX_FUNCTION_CODE_LENGTH = 8000
MAX_DOC_LENGTH = 5000

def _run_kernel_doc(kernel_root: str, func_name: str, file_path: str) -> tuple:
    """
    Execute kernel-doc script to extract documentation.
    
    Returns:
        (success, output)
    """
    script_path = os.path.join(kernel_root, "scripts", "kernel-doc")
    full_file_path = os.path.join(kernel_root, file_path)
    
    if not os.path.exists(script_path):
        return False, "kernel-doc script not found"
    
    if not os.path.exists(full_file_path):
        return False, f"Source file not found: {file_path}"
    
    try:
        result = subprocess.run(
            ["perl", script_path, "-rst", "-function", func_name, file_path],
            cwd=kernel_root,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode == 0 and result.stdout.strip():
            return True, result.stdout
        else:
            return False, result.stderr or "No documentation found"
            
    except subprocess.TimeoutExpired:
        return False, "kernel-doc execution timed out"
    except Exception as e:
        return False, str(e)

def _extract_function_code(file_path: str, func_name: str, kernel_root: str) -> tuple:
    """
    Extract function implementation from source file using regex.
    
    Returns:
        (success, code, start_line, end_line)
    """
    full_path = os.path.join(kernel_root, file_path)
    
    if not os.path.exists(full_path):
        return False, None, 0, 0
    
    try:
        with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            lines = content.split('\n')
    except Exception as e:
        return False, None, 0, 0
    
    patterns = [
        rf'^\s*(?:static\s+|inline\s+|__init\s+|__exit\s+)*'
        rf'(?:\w+\s+)+\**\s*{re.escape(func_name)}\s*\([^)]*\)\s*(?:\{{|__attribute__)',
        rf'^\s*(?:static\s+)?(?:inline\s+)?\w+\s+\**\s*{re.escape(func_name)}\s*\(',
    ]
    
    for pattern in patterns:
        for match in re.finditer(pattern, content, re.MULTILINE):
            start_pos = match.start()
            start_line = content[:start_pos].count('\n') + 1
            
            brace_count = 0
            in_function = False
            end_pos = start_pos
            
            for i, char in enumerate(content[start_pos:], start_pos):
                if char == '{':
                    brace_count += 1
                    in_function = True
                elif char == '}':
                    brace_count -= 1
                    if in_function and brace_count == 0:
                        end_pos = i + 1
                        break
            
            if end_pos > start_pos:
                func_code = content[start_pos:end_pos]
                end_line = content[:end_pos].count('\n') + 1
                
                if len(func_code) > MAX_FUNCTION_CODE_LENGTH:
                    func_code = func_code[:MAX_FUNCTION_CODE_LENGTH]
                    func_code += "\n\n... [TRUNCATED - Function too long]"
                
                return True, func_code, start_line, end_line
    
    return False, None, 0, 0

def _truncate_function_smart(code: str, max_length: int = MAX_FUNCTION_CODE_LENGTH) -> str:
    """
    Smart truncation: keep function signature and important parts.
    """
    if len(code) <= max_length:
        return code
    
    lines = code.split('\n')
    
    signature_lines = []
    brace_found = False
    for i, line in enumerate(lines):
        signature_lines.append(line)
        if '{' in line:
            brace_found = True
            break
    
    remaining_lines = lines[len(signature_lines):]
    
    closing_brace_idx = -1
    for i in range(len(remaining_lines) - 1, -1, -1):
        if '}' in remaining_lines[i]:
            closing_brace_idx = i
            break
    
    closing_lines = []
    if closing_brace_idx >= 0:
        closing_lines = remaining_lines[closing_brace_idx:]
    
    middle_lines = remaining_lines[:closing_brace_idx] if closing_brace_idx >= 0 else remaining_lines
    
    available_for_middle = max_length - sum(len(l) + 1 for l in signature_lines) - sum(len(l) + 1 for l in closing_lines) - 200
    
    if available_for_middle > 500:
        kept_middle = []
        current_length = 0
        
        important_patterns = [
            r'\b(if|else|switch|case|for|while|return)\b',
            r'\b(kmalloc|kfree|kmalloc_array|kzalloc)\b',
            r'\b(spin_lock|spin_unlock|mutex_lock|mutex_unlock)\b',
            r'\b(goto|break|continue)\b',
            r'\b(NULL|ERR_PTR|IS_ERR)\b',
        ]
        
        for line in middle_lines:
            is_important = any(re.search(p, line) for p in important_patterns)
            
            if is_important or current_length < available_for_middle * 0.3:
                kept_middle.append(line)
                current_length += len(line) + 1
            elif current_length < available_for_middle:
                kept_middle.append("    // ... [code omitted]")
                current_length += 30
        
        result_lines = signature_lines + kept_middle + closing_lines
    else:
        result_lines = signature_lines + ["    // ... [function body truncated]"] + closing_lines
    
    return '\n'.join(result_lines)

@tool
def query_kernel_api(
    func_name: str,
    file_path: str,
    include_implementation: bool = True
) -> Dict:
    """
    Query kernel API documentation and optionally the function implementation.
    
    This tool first attempts to extract documentation using the kernel-doc script.
    If no documentation is available, it falls back to extracting the function
    implementation from the source code.
    
    Args:
        func_name: Name of the kernel function (e.g., "atusb_alloc_urbs")
        file_path: Relative path to the source file (e.g., "drivers/net/ieee802154/atusb.c")
        include_implementation: If True, include function code when no doc available
        
    Returns:
        Dict containing:
        - has_documentation: bool
        - documentation: str (RST formatted documentation)
        - implementation: str (function code if available)
        - file_path: str
        - start_line: int
        - end_line: int
    """
    log = _get_logger()
    log.info(f"📚 [Tool] query_kernel_api: {func_name} @ {file_path}")
    
    kernel_root = Config.KERNEL_DIR_ORI
    
    result = {
        "func_name": func_name,
        "file_path": file_path,
        "has_documentation": False,
        "documentation": None,
        "implementation": None,
        "start_line": 0,
        "end_line": 0,
        "truncated": False
    }
    
    success, doc_output = _run_kernel_doc(kernel_root, func_name, file_path)
    
    if success and doc_output.strip():
        result["has_documentation"] = True
        if len(doc_output) > MAX_DOC_LENGTH:
            doc_output = doc_output[:MAX_DOC_LENGTH] + "\n\n... [Documentation truncated]"
            result["truncated"] = True
        result["documentation"] = doc_output
        log.info(f"   ✅ Found kernel-doc documentation ({len(doc_output)} chars)")
    
    if include_implementation or not result["has_documentation"]:
        success, code, start_line, end_line = _extract_function_code(file_path, func_name, kernel_root)
        
        if success and code:
            if len(code) > MAX_FUNCTION_CODE_LENGTH:
                code = _truncate_function_smart(code, MAX_FUNCTION_CODE_LENGTH)
                result["truncated"] = True
            
            result["implementation"] = code
            result["start_line"] = start_line
            result["end_line"] = end_line
            log.info(f"   ✅ Extracted function implementation (lines {start_line}-{end_line})")
    
    if not result["has_documentation"] and not result["implementation"]:
        result["error"] = f"Function '{func_name}' not found in {file_path}"
        log.warning(f"   ⚠️ {result['error']}")
    
    return result

@tool
def get_function_signature(func_name: str, file_path: str) -> Dict:
    """
    Get only the function signature (declaration) without the body.
    
    Useful for understanding function parameters and return type without
    reading the full implementation.
    
    Args:
        func_name: Name of the kernel function
        file_path: Relative path to the source or header file
        
    Returns:
        Dict with signature, parameters, and return type
    """
    log = _get_logger()
    log.info(f"📝 [Tool] get_function_signature: {func_name}")
    
    kernel_root = Config.KERNEL_DIR_ORI
    full_path = os.path.join(kernel_root, file_path)
    
    result = {
        "func_name": func_name,
        "file_path": file_path,
        "signature": None,
        "parameters": [],
        "return_type": None
    }
    
    if not os.path.exists(full_path):
        result["error"] = f"File not found: {file_path}"
        return result
    
    try:
        with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception as e:
        result["error"] = str(e)
        return result
    
    sig_pattern = rf'^\s*(?P<modifiers>(?:static\s+|inline\s+|__init\s+|__exit\s+|__must_check\s+)*)' \
                  rf'(?P<return_type>\w+(?:\s*\*)?)\s+' \
                  rf'(?P<func_name>{re.escape(func_name)})\s*' \
                  rf'\((?P<params>[^)]*)\)'
    
    for match in re.finditer(sig_pattern, content, re.MULTILINE):
        result["signature"] = match.group(0).strip()
        result["return_type"] = match.group("return_type").strip()
        
        params_str = match.group("params").strip()
        if params_str and params_str != "void":
            params = []
            for param in params_str.split(','):
                param = param.strip()
                if param:
                    parts = param.rsplit(None, 1)
                    if len(parts) == 2:
                        params.append({
                            "type": parts[0],
                            "name": parts[1].lstrip('*')
                        })
                    else:
                        params.append({"raw": param})
            result["parameters"] = params
        
        log.info(f"   ✅ Found signature: {result['return_type']} {func_name}(...)")
        return result
    
    result["error"] = f"Function signature not found for '{func_name}'"
    return result

KERNEL_DOC_TOOLS = [
    query_kernel_api,
    get_function_signature
]

def get_kernel_doc_tools():
    """Return the list of kernel documentation tools."""
    return KERNEL_DOC_TOOLS

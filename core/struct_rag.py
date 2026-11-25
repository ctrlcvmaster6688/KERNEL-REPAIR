#!/usr/bin/env python3
import os
import re
import argparse
import sys
from collections import defaultdict

import paths

# 尝试导入 tree-sitter，如果没有安装则会在类初始化时报错，而不是脚本加载时
try:
    from tree_sitter import Language, Parser
except ImportError:
    Language = None
    Parser = None

class StructuralContextExtractor:
    def __init__(self, kernel_root_path, language_so_path=paths.LANG_SO_PATH):
        """
        初始化提取器。
        :param kernel_root_path: Linux Kernel 源码根目录
        :param language_so_path: 编译好的 tree-sitter 语言库路径
        """
        self.kernel_root = kernel_root_path.rstrip(os.sep)
        self.language_so_path = language_so_path
        self.c_parser = None
        self.c_language = None

        # 配置
        self.common_words = {
            'get', 'set', 'do', 'init', 'exit', 'alloc', 'free', 'cleanup', 'start',
            'end', 'find', 'try', 'test', 'op', 'ops', 'handler', 'entry', 'data',
            'info', 'lock', 'unlock', 'read', 'write', 'vma', 'page', 'ptr',
            'thp', 'pmd', 'pte', 'hugetlb'
        }
        self.skip_dirs = {'Documentation', '.git', 'tools', 'scripts', 'samples', 'drivers/staging', 'firmware'}

        # 初始化检查
        if not os.path.isdir(self.kernel_root):
            raise ValueError(f"Kernel root path '{self.kernel_root}' is not a valid directory.")

        self._initialize_tree_sitter()

    def _initialize_tree_sitter(self):
        if Language is None or Parser is None:
            raise ImportError("tree-sitter library is not installed. Please run 'pip install tree-sitter'.")

        try:
            self.c_language = Language(self.language_so_path, 'c')
            self.c_parser = Parser()
            self.c_parser.set_language(self.c_language)
        except Exception as e:
            error_msg = (
                f"Failed to initialize Tree-sitter using {self.language_so_path}.\n"
                f"Details: {e}\n"
                "Please ensure the .so file exists and is compiled correctly."
            )
            raise RuntimeError(error_msg)

    def _read_file_text(self, path):
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read()
        except Exception:
            return None

    def _get_core_tokens(self, func_name):
        tokens = func_name.lower().split('_')
        core_tokens = {t for t in tokens if len(t) > 1 and t not in self.common_words}
        return core_tokens

    def _calculate_jaccard_similarity(self, set_a, set_b):
        if not set_a or not set_b:
            return 0.0
        intersection = len(set_a.intersection(set_b))
        union = len(set_a.union(set_b))
        return intersection / union

    def _extract_function_info(self, file_content, file_path, target_func_name=None):
        """核心 Tree-sitter 解析逻辑"""
        if not self.c_parser:
            raise RuntimeError("Parser not initialized")

        try:
            tree = self.c_parser.parse(bytes(file_content, "utf8"))
        except Exception:
            return []

        root_node = tree.root_node
        function_definitions = []
        stack = [root_node]

        while stack:
            current_node = stack.pop()
            if current_node.type == 'function_definition':
                declarator = current_node.child_by_field_name('declarator')
                if not declarator: continue

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

                if not function_name_node: continue
                function_name = function_name_node.text.decode('utf8')

                if target_func_name and function_name != target_func_name:
                    stack.extend(current_node.children)
                    continue

                parameter_list = declarator.child_by_field_name('parameters')
                params_text = ""
                if parameter_list:
                    params_text = file_content[parameter_list.start_byte:parameter_list.end_byte]
                    params_text = params_text.strip()[1:-1] if params_text.strip().startswith('(') else params_text
                    params_text = ' '.join(params_text.replace('\n', ' ').split())

                function_body_node = current_node.child_by_field_name('body')
                if function_body_node:
                    body_code = file_content[function_body_node.start_byte:function_body_node.end_byte]
                    num_lines = body_code.count('\n')

                    if num_lines >= 1:
                        # [修改点] 获取起始行号 (Tree-sitter 是 0-indexed，所以 +1)
                        start_line = current_node.start_point[0] + 1

                        func_data = {
                            'name': function_name,
                            'file_path': file_path,
                            'start_line': start_line,  # <--- 新增字段
                            'params_text': params_text,
                            'full_code': file_content[current_node.start_byte:current_node.end_byte].strip(),
                            'body_code': body_code
                        }
                        function_definitions.append(func_data)
                        if target_func_name and function_name == target_func_name:
                            return func_data

            stack.extend(current_node.children)

        return function_definitions

    def _iter_source_files(self, start_dir):
        for root, dirs, files in os.walk(start_dir):
            dirs[:] = [d for d in dirs if d not in self.skip_dirs and not d.startswith('.')]
            for f in files:
                if f.endswith(('.c', '.h', '.S')):
                    yield os.path.join(root, f)

    def _find_related_definitions(self, target_file_path, definition_names):
        definitions = {}
        if not self.kernel_root: return definitions

        def simple_extract_block(text, start_pos):
            n = len(text)
            if start_pos >= n or text[start_pos] != '{': return None
            i = start_pos
            depth = 0
            while i < n:
                ch = text[i]
                if ch == '{': depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0: return text[start_pos:i+1]
                i += 1
            return None

        target_dir = os.path.dirname(os.path.join(self.kernel_root, target_file_path))
        candidates = [
            target_dir,
            os.path.join(self.kernel_root, 'include'),
            os.path.join(self.kernel_root, 'include', 'uapi')
        ]

        visited = set()
        structs_to_find = set(definition_names.get('structs', []))
        flags_to_find = set(definition_names.get('flags', []))

        for cdir in candidates:
            if not os.path.isdir(cdir): continue
            for filepath in self._iter_source_files(cdir):
                if filepath in visited: continue
                visited.add(filepath)
                txt = self._read_file_text(filepath)
                if not txt: continue

                # Find Structs
                for sname in list(structs_to_find):
                    if definitions.get(sname): continue
                    for m in re.finditer(r'\b(struct|union)\s+' + re.escape(sname) + r'\b', txt):
                        idx = txt.find('{', m.end())
                        if idx == -1: continue
                        block = simple_extract_block(txt, idx)
                        if block:
                            tail_start = idx + len(block)
                            tail_snip = txt[tail_start:tail_start+80]
                            typedef_tail = ''
                            tm = re.search(r'\}\s*([A-Za-z0-9_,\s]+);', tail_snip)
                            if tm: typedef_tail = tm.group(0)
                            header = m.group(1) + " " + sname + " "
                            definitions[sname] = (header + block + "\n" + typedef_tail).strip()
                            structs_to_find.discard(sname)
                            break

                # Find Macros
                for fname in list(flags_to_find):
                    if definitions.get(fname): continue
                    pat = r'^[ \t]*#\s*define\s+' + re.escape(fname) + r'\b.*$'
                    mm = re.search(pat, txt, re.MULTILINE)
                    if mm:
                        definitions[fname] = mm.group(0).strip()
                        flags_to_find.discard(fname)

                if not structs_to_find and not flags_to_find:
                    return definitions
        return definitions

    def extract(self, file_path, func_name):
        """
        [Public API] 提取目标函数的结构化上下文。

        Returns:
            dict: {
                "target_code": str,
                "start_line": int,
                "structs": dict,
                "macros": dict,
                "similar_funcs": list
            }
        """
        full_path = os.path.join(self.kernel_root, file_path)
        content = self._read_file_text(full_path)

        result_payload = {
            "target_code": None,
            "start_line": None,
            "structs": {},
            "macros": {},
            "similar_funcs": []
        }

        if not content:
            print(f"Error: File not found {full_path}")
            return result_payload

        target_data = self._extract_function_info(content, file_path, target_func_name=func_name)

        if not target_data or not isinstance(target_data, dict):
            print(f"Tree-sitter failed for '{func_name}', trying regex fallback...")
            target_data = self._extract_function_regex(content, func_name)

        if not target_data or not isinstance(target_data, dict):
            print(f"Error: Function '{func_name}' definition not found in {file_path}")
            return result_payload

        result_payload["target_code"] = target_data.get('full_code', '')
        result_payload["start_line"] = target_data.get('start_line')

        params = target_data.get('params_text', '')
        found_structs = set()
        for mm in re.finditer(r'\b(?:struct|union)\s+([A-Za-z0-9_]+)\b', params):
            found_structs.add(mm.group(1))
        critical_structs = sorted(found_structs)

        function_body = target_data.get('body_code', '')
        cand = set(re.findall(r'\b([A-Z][A-Z0-9_]{2,})\b', function_body))
        exclusion = {'INIT', 'MAX', 'MIN', 'TRUE', 'FALSE', 'NULL', 'RETURN', 'likely', 'unlikely', 'WARN_ON'}
        critical_flags = sorted(list(cand - exclusion))

        definitions = self._find_related_definitions(
            file_path,
            {'structs': critical_structs, 'flags': critical_flags}
        )

        for s in critical_structs:
            if s in definitions:
                result_payload["structs"][s] = definitions[s]

        for f in critical_flags:
            if f in definitions:
                result_payload["macros"][f] = definitions[f]

        target_core_tokens = self._get_core_tokens(func_name)
        if target_core_tokens:
            all_funcs = self._extract_function_info(content, file_path, target_func_name=None)
            similarity_candidates = []

            for f_data in all_funcs:
                if f_data['name'] == func_name: continue
                candidate_tokens = self._get_core_tokens(f_data['name'])
                sim = self._calculate_jaccard_similarity(target_core_tokens, candidate_tokens)
                if sim >= 0.4:
                    similarity_candidates.append({
                        "name": f_data['name'],
                        "code": f_data['full_code'],
                        "similarity": sim
                    })

            similarity_candidates.sort(key=lambda x: x['similarity'], reverse=True)
            result_payload["similar_funcs"] = similarity_candidates[:2]

        return result_payload

    def _extract_function_regex(self, content, func_name):
        """
        正则表达式 fallback：当 Tree-sitter 无法解析时使用。
        支持普通函数定义和宏定义函数。
        """
        patterns = [
            r'((?:static\s+)?(?:inline\s+)?(?:const\s+)?\w+(?:\s*\*)?\s+' + re.escape(func_name) + r'\s*\([^)]*\)\s*\{)',
            r'((?:static\s+)?(?:inline\s+)?(?:const\s+)?\w+(?:\s*\*)?\s+' + re.escape(func_name) + r'\s*\([^)]*\)\s*;)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, content)
            if match:
                start_pos = match.start()
                start_line = content[:start_pos].count('\n') + 1
                
                func_header = match.group(1)
                
                if '{' in func_header:
                    brace_count = 0
                    i = start_pos
                    while i < len(content):
                        if content[i] == '{':
                            brace_count += 1
                        elif content[i] == '}':
                            brace_count -= 1
                            if brace_count == 0:
                                full_code = content[start_pos:i+1]
                                body_start = content.find('{', start_pos)
                                body_code = content[body_start:i+1]
                                
                                params_match = re.search(r'\(([^)]*)\)', func_header)
                                params_text = params_match.group(1) if params_match else ''
                                
                                return {
                                    'name': func_name,
                                    'start_line': start_line,
                                    'params_text': params_text.strip(),
                                    'full_code': full_code.strip(),
                                    'body_code': body_code.strip()
                                }
                        i += 1
                else:
                    return {
                        'name': func_name,
                        'start_line': start_line,
                        'params_text': '',
                        'full_code': func_header.strip(),
                        'body_code': ''
                    }
        
        return None

# -------------------------
# CLI Support (保持向后兼容)
# -------------------------
# if __name__ == '__main__':
#     parser = argparse.ArgumentParser(description="Tree-sitter Driven Structural Context RAG Extractor")
#     parser.add_argument('--repo_path', type=str, required=True, help="Path to kernel repo root")
#     parser.add_argument('--file_path', type=str, required=True, help="Relative path to source file")
#     parser.add_argument('--func_name', type=str, required=True, help="Target function name")
#     args = parser.parse_args()
#
#     try:
#         extractor = StructuralContextExtractor(kernel_root_path=args.repo_path)
#         result = extractor.extract(args.file_path, args.func_name)
#
#         if not result["target_code"]:
#             sys.exit(1)
#
#         print("=========================================")
#         print(f"## 1. Target Function Body (Start Line: {result['start_line']})")
#         print("=========================================")
#         print(result["target_code"])
#
#         print("\n=========================================")
#         print("## 2. Critical Struct Definitions")
#         print("=========================================")
#         if result["structs"]:
#             for name, code in result["structs"].items():
#                 print(code)
#                 print("")
#         else:
#             print("// (none inferred/found)")
#
#         print("\n=========================================")
#         print(f"## 3. Critical Flag/Macro Definitions")
#         print("=========================================")
#         if result["macros"]:
#             for name, code in result["macros"].items():
#                 print(code)
#         else:
#             print("// (none found)")
#
#         print("\n=========================================")
#         print("## 4. Similar Functions")
#         print("=========================================")
#         if result["similar_funcs"]:
#             for func in result["similar_funcs"]:
#                 print(f"// Similar: {func['name']} (Sim: {func['similarity']:.2f})")
#                 print(func['code'])
#                 print("")
#         else:
#             print("// (none found)")
#
#     except Exception as e:
#         print(f"Execution Error: {e}", file=sys.stderr)
import re
import json
import re
import subprocess
import sys
import os
import math
from pathlib import Path
from tree_sitter import Language, Parser

# The tree-sitter C grammar ships with the package and is shared with the repair
# half; it is loaded (never rebuilt) through the path declared in paths.py.
sys.path.append(str(Path(os.path.abspath(__file__)).parent.parent))
import settings  # noqa: E402

C_LANGUAGE = Language(str(settings.LANG_LIBRARY_PATH), 'c')
c_parser = Parser()
c_parser.set_language(C_LANGUAGE)
def get_file_names_for_syzid(file_path, syzid):
    with open(file_path, 'r') as f:
        lines = f.readlines()
    found = False
    file_names = []

    for line in lines:
        line = line.strip()
        if line.startswith("Response for case"):
            if line.split()[-1] == syzid:
                found = True
            else:
                found = False
        elif found and line.startswith(("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.", "10.")):
            file_name = line.split(maxsplit=1)[1]
            file_names.append(file_name)

    return file_names
def extract_function_bodies(file_path):
    with open(file_path, 'r') as f:
        code = f.read()

    tree = c_parser.parse(bytes(code, "utf8"))
    root_node = tree.root_node

    function_map = {} 
    def walk(node):
        stack = [node]
        while stack:
            current_node = stack.pop()
            if current_node.type == 'function_definition':
                declarator = current_node.child_by_field_name('declarator')
                function_name_node = None
                temp_stack = [declarator]
                while temp_stack:
                    cur_node = temp_stack.pop()
                    for ch in cur_node.children:
                        if ch.type == 'identifier':
                            function_name_node = ch
                            temp_stack.clear()
                            break
                        if ch.end_byte <= current_node.end_byte:
                            temp_stack.append(ch)

                if function_name_node:
                    function_name = function_name_node.text.decode('utf-8')

                   
                    function_body = current_node.child_by_field_name('body')
                    if function_body:
                        body_code = code[function_body.start_byte:function_body.end_byte]
                        num_lines = body_code.count('\n')
   
                        if num_lines >= 5 and '_' in function_name:
                            function_map[function_name] = body_code  

            stack.extend(current_node.children)

    walk(root_node)
    return function_map  
def calculate_cyclomatic_complexity(code: str) -> int:

    complexity = 1

    keywords = [
        r'\bif\b', r'\belse\s+if\b', r'\bfor\b', r'\bwhile\b', r'\bdo\b',
        r'\bcase\b', r'\bcatch\b', r'\bgoto\b'
    ]

    logical_operators = [r'&&', r'\|\|']

    for keyword in keywords:
        matches = re.findall(keyword, code)
        complexity += len(matches)

    for op in logical_operators:
        matches = re.findall(op, code)
        complexity += len(matches)

    return complexity
def halstead_effort(code):
    tokens = re.findall(r'\b\w+\b|[-+*/=(){}[\],.;]', code)

    operators = set(["+", "-", "*", "/", "=", "==", "!=", "<", ">", "<=", ">=", "&&", "||", "!", "(", ")", "{", "}", "[", "]", ";", ","])
    operands = set()

    N1, N2 = 0, 0
    n1_set, n2_set = set(), set()

    for token in tokens:
        if token in operators:
            N1 += 1
            n1_set.add(token)
        else:
            N2 += 1
            n2_set.add(token)

    n1 = len(n1_set)
    n2 = len(n2_set)
    N = N1 + N2
    n = n1 + n2

    if n2 == 0 or n == 0:
        return 0 

    V = N * math.log2(n) 
    D = (n1 / 2) * (N2 / n2)  
    E = D * V  

    return E
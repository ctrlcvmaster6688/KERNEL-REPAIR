# utils/file_ops.py
import json
import re
import os

def read_file(file_path: str) -> str:
    """读取文件，强制使用 UTF-8，忽略错误"""
    if not os.path.exists(file_path):
        return ""
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except Exception as e:
        print(f"[FileOps] Error reading {file_path}: {e}")
        return ""

def save_file(file_path: str, content: str):
    """保存文件，强制使用 UTF-8"""
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        print(f"[FileOps] Error writing {file_path}: {e}")

def extract_json_from_text(text: str):
    """
    [增强版] 从文本中暴力提取 JSON 对象或数组。
    无论 LLM 是否加了 markdown 标记，都能提取出干净的 JSON。
    支持处理 ```json []``` 这种 Markdown 包裹的格式。
    """
    if not text:
        return None

    # 1. 尝试直接解析 (最理想情况)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. 针对 Markdown 代码块的特化处理
    # 处理各种 Markdown 代码块格式，包括 ```json []``` 和多行格式
    patterns = [
        # 匹配 ```json []``` 格式（单行）
        r"```(?:json)?\s*((?:\{[\s\S]*\}|\[[\s\S]*\]))\s*```",
        # 匹配 ```json 开头，``` 结尾的多行格式
        r"```(?:json)?\s*((?:[\s\S]*?))\s*```"
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            json_candidate = match.group(1).strip()
            try:
                return json.loads(json_candidate)
            except json.JSONDecodeError:
                pass

    # 3. 正则提取：寻找最外层的 {} 或 []
    # 贪婪匹配：从第一个 { 或 [ 到最后一个 } 或 ]
    match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)

    if match:
        json_candidate = match.group(1)
        try:
            return json.loads(json_candidate)
        except json.JSONDecodeError:
            pass

    # 4. 尝试提取所有可能的 JSON 片段
    # 这是一个兜底策略，尝试找到任何有效的 JSON
    import ast
    try:
        # 尝试使用 ast.literal_eval 解析
        # 找到第一个 { 或 [ 开始的位置
        start_idx = text.find('{') if text.find('{') != -1 else text.find('[')
        if start_idx != -1:
            # 尝试从这个位置开始解析
            for end_idx in range(len(text), start_idx, -1):
                try:
                    json_obj = ast.literal_eval(text[start_idx:end_idx])
                    if isinstance(json_obj, (dict, list)):
                        return json_obj
                except:
                    continue
    except:
        pass

    # 5. 最后尝试：移除所有非 JSON 字符，只保留花括号和方括号内的内容
    try:
        # 找到第一个 { 或 [
        start_brace = text.find('{')
        start_bracket = text.find('[')
        start_idx = min([i for i in [start_brace, start_bracket] if i != -1]) if any(i != -1 for i in [start_brace, start_bracket]) else -1
        
        if start_idx != -1:
            # 找到匹配的结束符
            open_char = text[start_idx]
            close_char = '}' if open_char == '{' else ']'
            count = 1
            end_idx = start_idx + 1
            
            while end_idx < len(text) and count > 0:
                if text[end_idx] == open_char:
                    count += 1
                elif text[end_idx] == close_char:
                    count -= 1
                end_idx += 1
            
            if count == 0:
                json_candidate = text[start_idx:end_idx]
                try:
                    return json.loads(json_candidate)
                except json.JSONDecodeError:
                    pass
    except:
        pass

    # 如果都失败了，打印前100个字符方便调试
    print(f"[Extract] Failed to extract JSON from: {text[:100]}...")
    return None

if __name__ == "__main__":
    """
    测试 extract_json_from_text 函数
    """
    test_cases = [
        # 测试用例 1: 直接的 JSON 对象
        '''{
            "name": "test",
            "value": 123
        }''',
        
        # 测试用例 2: 直接的 JSON 数组
        '''[
            {"name": "item1"},
            {"name": "item2"}
        ]''',
        
        # 测试用例 3: Markdown 包裹的 JSON 对象
        '''```json
        {
            "name": "test",
            "value": 123
        }
        ```''',
        
        # 测试用例 4: Markdown 包裹的 JSON 数组
        '''```json
        [
            {"name": "item1"},
            {"name": "item2"}
        ]
        ```''',
        
        # 测试用例 5: 简单的 Markdown 包裹 JSON 数组（单行）
        '```json []```',
        
        # 测试用例 6: 包含其他文本的 JSON
        '''Some text before {
            "name": "test",
            "value": 123
        } some text after''',
        
        # 测试用例 7: 包含代码块的 JSON
        '''```
        {
            "code": "function() {}"
        }
        ```'''
    ]
    
    print("=== 测试 JSON 提取功能 ===")
    for i, test_case in enumerate(test_cases):
        print(f"\n测试用例 {i+1}:")
        print(f"输入: {test_case[:100]}...")
        result = extract_json_from_text(test_case)
        print(f"输出: {result}")
        print(f"类型: {type(result).__name__}")
        print("-" * 50)
    
    print("\n=== 测试完成 ===")
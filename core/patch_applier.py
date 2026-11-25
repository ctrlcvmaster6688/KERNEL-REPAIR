# lib/patch_applier.py
import os
import sys
import json
import shutil
import difflib
import re
import subprocess

import paths

# 尝试导入 Tree-sitter
try:
    from tree_sitter import Language, Parser
except ImportError:
    print("Error: pip install tree-sitter", file=sys.stderr)
    sys.exit(1)

class KernelPatchApplier:
    def __init__(self, kernel_root: str, language_so_path: str = paths.LANG_SO_PATH):
        self.kernel_root = kernel_root.rstrip(os.sep)
        self.language_so_path = language_so_path
        self.parser = None
        self._init_parser()

    def _init_parser(self):
        if not os.path.exists(self.language_so_path):
            raise FileNotFoundError(f"Tree-sitter so not found: {self.language_so_path}")
        try:
            # 初始化 C 语言解析器
            c_lang = Language(self.language_so_path, 'c')
            self.parser = Parser()
            self.parser.set_language(c_lang)
        except Exception as e:
            raise RuntimeError(f"Parser init failed: {e}")

    def _find_function_range(self, content_bytes: bytes, target_func_name: str):
        """使用 Tree-sitter 查找函数在文件中的字节范围"""
        tree = self.parser.parse(content_bytes)
        root = tree.root_node
        stack = [root]

        while stack:
            node = stack.pop()
            if node.type == 'function_definition':
                declarator = node.child_by_field_name('declarator')
                # 递归查找函数名
                func_name = self._extract_func_name(declarator)
                if func_name == target_func_name:
                    return node.start_byte, node.end_byte

            # 继续遍历子节点
            if node.children:
                stack.extend(node.children)
        return None, None

    def _extract_func_name(self, node):
        if not node: return None
        stack = [node]
        while stack:
            curr = stack.pop()
            if curr.type == 'identifier':
                return curr.text.decode('utf-8')
            for child in curr.children:
                # 跳过参数列表，只看声明部分
                if child.type != 'parameter_list':
                    stack.append(child)
        return None
    def _apply_global_workarounds(self):
        """
        [修正版] 注入全局热修补丁。
        不依赖当前 AI 正在修改的文件，而是主动去修正内核中已知的噪音文件。
        该操作直接修改磁盘上的文件。
        """
        # === 规则 1: cgroup panic suppression ===
        # 无论 AI 现在修哪个文件，cgroup.c 的这个问题都必须屏蔽，否则机器会重启
        cgroup_rel_path = "kernel/cgroup/cgroup.c"
        cgroup_full_path = os.path.join(self.kernel_root, cgroup_rel_path)

        if os.path.exists(cgroup_full_path):
            try:
                with open(cgroup_full_path, 'rb') as f:
                    content = f.read()

                # 定义要屏蔽的模式
                # 注意：这里使用 replace，如果文件已经被改过（pattern不存在），则不会重复修改，是安全的
                pattern = b"WARN_ON_ONCE(percpu_ref_is_dying(&css->refcnt));"
                replacement = b"/*if (percpu_ref_is_dying(&css->refcnt)) return;  CI WORKAROUND */"

                if pattern in content:
                    print(f"[PatchApplier] 🔧 Injecting global workaround into {cgroup_rel_path}")
                    new_content = content.replace(pattern, replacement)

                    # 写回磁盘
                    with open(cgroup_full_path, 'wb') as f:
                        f.write(new_content)
            except Exception as e:
                print(f"[PatchApplier] ⚠️ Failed to inject workaround for {cgroup_rel_path}: {e}", file=sys.stderr)
    def apply(self, file_rel_path: str, target_func_name: str, patch_input):
        """
        应用补丁的核心方法
        :param patch_input: 可以是 JSON 字符串，也可以是 Python Dict
        """
        full_path = os.path.join(self.kernel_root, file_rel_path)
        if not os.path.exists(full_path):
            return False, f"File not found: {full_path}"
        self._apply_global_workarounds()
        # === 1. 解析与清洗 (最后一道防线) ===
        try:
            data = None
            if isinstance(patch_input, dict):
                data = patch_input
            else:
                # 如果是字符串，先尝试暴力提取 JSON，防止 markdown 干扰
                json_str = patch_input
                if "```" in json_str:
                    # 再次尝试提取 {} 包裹的内容
                    m = re.search(r"(\{.*\})", json_str, re.DOTALL)
                    if m: json_str = m.group(1)
                data = json.loads(json_str)

            new_code = data.get("modified_code")
            if not new_code:
                return False, "JSON missing 'modified_code'"

            # [关键检查] 绝对禁止 markdown 标记进入 C 文件
            if "```" in new_code:
                return False, "Error: Markdown tags detected INSIDE modified_code. Aborting to protect file."

        except Exception as e:
            return False, f"JSON Parse Error: {e}"

        # === 2. 读取原始文件 ===
        try:
            with open(full_path, 'rb') as f:
                original_bytes = f.read()
        except Exception as e:
            return False, f"Read error: {e}"

        # === 3. Tree-sitter 定位 ===
        start, end = self._find_function_range(original_bytes, target_func_name)
        if start is None:
            return False, f"Target function '{target_func_name}' not found in AST."

        # === 4. 构造新文件内容 ===
        try:
            # 确保编码一致
            new_code_bytes = new_code.encode('utf-8')
            patched_bytes = original_bytes[:start] + new_code_bytes + original_bytes[end:]
        except Exception as e:
            return False, f"Encoding error: {e}"

        # === 5. 安全写入 (先备份) ===
        backup_path = full_path + ".bak"
        try:
            shutil.copy(full_path, backup_path)
        except Exception as e:
            return False, f"Backup creation failed: {e}"

        try:
            with open(full_path, 'wb') as f:
                f.write(patched_bytes)
        except Exception as e:
            # 如果写入失败，尝试从备份恢复，保持环境清洁
            shutil.copy(backup_path, full_path)
            return False, f"Write failed: {e}"

        # === 6. 生成 Diff (用于日志展示) ===
        try:
            diff = difflib.unified_diff(
                original_bytes.decode('utf-8', 'ignore').splitlines(),
                patched_bytes.decode('utf-8', 'ignore').splitlines(),
                fromfile=f"a/{file_rel_path}",
                tofile=f"b/{file_rel_path}",
                lineterm=""
            )
            return True, "\n".join(diff)
        except:
            return True, "Patch applied (Diff generation skipped)."

    def revert(self, file_rel_path: str):
        """
        从 .bak 恢复文件。
        注意：ValidatorMain 现在主要使用 git checkout 回滚，这个方法作为备选。
        """
        full_path = os.path.join(self.kernel_root, file_rel_path)
        backup_path = full_path + ".bak"

        if os.path.exists(backup_path):
            try:
                shutil.move(backup_path, full_path)
                return True, "Restored from .bak"
            except Exception as e:
                return False, f"Revert move failed: {e}"
        return False, "No .bak file found"

    def reset_git_hard(self):
        """
        执行 git reset --hard 来完全回滚环境。
        用于多文件补丁应用失败时的原子性回滚。
        """
        try:
            # 检查是否在git仓库中
            result = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=self.kernel_root,
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                return False, "Not in a git repository"
            
            # 执行 git reset --hard
            result = subprocess.run(
                ["git", "reset", "--hard"],
                cwd=self.kernel_root,
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                return False, f"git reset --hard failed: {result.stderr}"
            
            # 执行 git clean -fd
            result = subprocess.run(
                ["git", "clean", "-fd"],
                cwd=self.kernel_root,
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                return False, f"git clean -fd failed: {result.stderr}"
            
            return True, "Git reset --hard and clean completed successfully"
        except Exception as e:
            return False, f"Error during git reset: {e}"
            
    def _find_struct_range(self, content_bytes: bytes, struct_name: str):
        """
        使用 Tree-sitter 查找结构体在文件中的字节范围
        """
        tree = self.parser.parse(content_bytes)
        root = tree.root_node
        stack = [root]

        while stack:
            node = stack.pop()
            if node.type == 'struct_specifier':
                # 查找结构体名称
                for child in node.children:
                    if child.type == 'identifier' and child.text.decode('utf-8') == struct_name:
                        return node.start_byte, node.end_byte

            # 继续遍历子节点
            if node.children:
                stack.extend(node.children)
        return None, None
        
    def apply_multi_file_patch(self, patches):
        """
        应用多文件补丁，确保原子性
        
        Args:
            patches: 包含多个文件补丁的列表，每个元素为 dict:
                {
                    "file_path": str,
                    "target_name": str,  # 函数名或结构体名
                    "target_type": str,  # "function" 或 "struct"
                    "patch": dict or str
                }
        
        Returns:
            (success, message): 成功返回(True, diffs)，失败返回(False, error_message)
        """
        if not patches:
            return False, "No patches provided"
        
        # 先执行git reset --hard，确保环境干净
        reset_result = self.reset_git_hard()
        if not reset_result[0]:
            return False, f"Failed to reset git: {reset_result[1]}"
        
        applied_files = []
        diffs = []
        
        try:
            for patch in patches:
                file_path = patch.get("file_path")
                target_name = patch.get("target_name")
                target_type = patch.get("target_type", "function")
                patch_input = patch.get("patch")
                
                if not file_path or not target_name or not patch_input:
                    return False, f"Invalid patch format for file {file_path}"
                
                full_path = os.path.join(self.kernel_root, file_path)
                if not os.path.exists(full_path):
                    return False, f"File not found: {full_path}"
                
                # 解析补丁输入
                try:
                    data = None
                    if isinstance(patch_input, dict):
                        data = patch_input
                    else:
                        # 如果是字符串，先尝试暴力提取 JSON，防止 markdown 干扰
                        json_str = patch_input
                        if "```" in json_str:
                            # 再次尝试提取 {} 包裹的内容
                            m = re.search(r"(\{.*\})", json_str, re.DOTALL)
                            if m: json_str = m.group(1)
                        data = json.loads(json_str)

                    new_code = data.get("modified_code")
                    if not new_code:
                        return False, f"JSON missing 'modified_code' for {file_path}"

                    # [关键检查] 绝对禁止 markdown 标记进入 C 文件
                    if "```" in new_code:
                        return False, f"Error: Markdown tags detected INSIDE modified_code for {file_path}. Aborting to protect file."

                except Exception as e:
                    return False, f"JSON Parse Error for {file_path}: {e}"
                
                # 读取原始文件
                try:
                    with open(full_path, 'rb') as f:
                        original_bytes = f.read()
                except Exception as e:
                    return False, f"Read error for {file_path}: {e}"
                
                # 定位目标（函数或结构体）
                if target_type == "function":
                    start, end = self._find_function_range(original_bytes, target_name)
                elif target_type == "struct":
                    start, end = self._find_struct_range(original_bytes, target_name)
                else:
                    return False, f"Invalid target_type: {target_type}"
                
                if start is None:
                    return False, f"Target {target_type} '{target_name}' not found in AST for {file_path}"
                
                # 构造新文件内容
                try:
                    # 确保编码一致
                    new_code_bytes = new_code.encode('utf-8')
                    patched_bytes = original_bytes[:start] + new_code_bytes + original_bytes[end:]
                except Exception as e:
                    return False, f"Encoding error for {file_path}: {e}"
                
                # 安全写入 (先备份)
                backup_path = full_path + ".bak"
                try:
                    shutil.copy(full_path, backup_path)
                except Exception as e:
                    return False, f"Backup creation failed for {file_path}: {e}"
                
                try:
                    with open(full_path, 'wb') as f:
                        f.write(patched_bytes)
                except Exception as e:
                    # 如果写入失败，尝试从备份恢复，保持环境清洁
                    shutil.copy(backup_path, full_path)
                    # 回滚所有已应用的文件
                    self.reset_git_hard()
                    return False, f"Write failed for {file_path}: {e}"
                
                # 生成 Diff (用于日志展示)
                try:
                    diff = difflib.unified_diff(
                        original_bytes.decode('utf-8', 'ignore').splitlines(),
                        patched_bytes.decode('utf-8', 'ignore').splitlines(),
                        fromfile=f"a/{file_path}",
                        tofile=f"b/{file_path}",
                        lineterm=""
                    )
                    diffs.append("\n".join(diff))
                except:
                    diffs.append(f"Patch applied to {file_path} (Diff generation skipped).")
                
                applied_files.append(file_path)
            
            return True, "\n".join(diffs)
        except Exception as e:
            # 发生异常，回滚所有已应用的文件
            self.reset_git_hard()
            return False, f"Error applying multi-file patch: {str(e)}"
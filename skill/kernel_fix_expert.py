"""
KernelFixExpert - 内核修复专家工具

输入一段 Linux 内核 C 代码及修改意图描述，输出 3 个最匹配的历史黄金修复补丁。

执行流程:
1. Intent Diagnosis - 识别 16 种修复模式标签
2. Context Extraction - 提取子系统标签和词表关键词
3. Embedding Generation - 获取代码向量
4. Hybrid Retrieval - 意图硬过滤 + 向量匹配
5. AST Reranking - Tree-sitter 结构比对
6. Final Response - 输出 Top 3 结果

注意:
- 输入的 description 应描述您想对代码做怎样的修改，而非 bug 本身
- 检索结果用于学习借鉴语法结构和修复模式，而非语义层面的关联
"""

import os
import re
import json
import sqlite3
import asyncio
import threading
import numpy as np
import requests
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from config import Config
import paths

EMBED_API_URL = Config.EMBED_API_URL
DB_PATH = paths.GOLDEN_DB_PATH
VOCAB_PATH = paths.VOCAB_PATH

_db_connection = None
_db_lock = threading.Lock()


@dataclass
class GoldenPatch:
    commit_id: str
    similarity_score: float
    intent_match: str
    subsystem: str
    object_types: List[str]
    buggy_code: str
    fixed_code: str
    ast_score: float = 0.0


class IntentModeler:
    """Intent Modeler: 使用5点结构生成自然语言意图描述，并提取subsystem和核心API"""
    
    def model_intent(self, buggy_code: str, description: str) -> Dict:
        """
        使用5点结构生成自然语言意图描述
        
        Args:
            buggy_code: 待修复的代码片段
            description: 修复意图描述
            
        Returns:
            包含意图描述、subsystem、核心API的字典
        """
        from utils.llm_interface import call_llm
        import json
        import re
        
        system_prompt = """You are a Principal Linux Kernel Architect. Analyze the patch and distill its "Fix Paradigm" into a SINGLE sentence using the 5-point structure.

### THE SURGICAL INTENT PARADIGM:
1. **Context**: Condition/Environment (e.g., During netlink message parsing).
2. **Operation**: Technical action (e.g., Validated attribute length).
3. **Entity**: Target variable/struct (e.g., The 'nla_len').
4. **Location**: Precise logic point (e.g., Before accessing the attribute payload).
5. **Goal**: Restored invariant (e.g., Prevented an out-of-bounds memory access).

### OUTPUT SCHEMA:
{
  "intent_summary": "[Context] + [Operation] + [Entity] + [Location] + [Goal]",
  "subsystem": "net/block/fs/mm/kernel/security/block/crypto",
  "core_apis": ["api1", "api2"]
}

### EXAMPLE:
{
  "intent_summary": "During report raw event processing, implemented a value-clamping operation on the 'n' integer parameter before the snto32() bit-shift logic to ensure the exponent never exceeds 32 bits, preventing a shift-out-of-bounds crash.",
  "subsystem": "net",
  "core_apis": ["snto32", "report_raw_event"]
}"""

        user_prompt = f"""Analyze the following code snippet and fix description to generate a structured intent description.

Code Snippet:
```c
{buggy_code[:2000]}
```

Fix Description:
{description}

Generate a JSON object with:
1. intent_summary: A single sentence combining Context + Operation + Entity + Location + Goal
2. subsystem: The Linux kernel subsystem (net, block, fs, mm, kernel, security, crypto, etc.)
3. core_apis: List of core APIs/functions involved in the fix

Output only the JSON object."""
        
        try:
            response = call_llm(user_prompt, system_prompt=system_prompt, temperature=0.1)
            response = response.strip()
            
            print(f"[IntentModeler] LLM 响应: {response}")
            
            if not response:
                print("[IntentModeler] LLM 响应为空，使用默认意图")
                return self._default_intent()
            
            # 尝试提取 JSON
            json_match = re.search(r'\{[^{}]*"intent_summary"[^{}]*\}', response, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group(0))
                    print(f"[IntentModeler] 提取到意图: {result.get('intent_summary', '')[:100]}...")
                    return result
                except json.JSONDecodeError:
                    pass
            
            # 尝试直接解析整个响应
            try:
                result = json.loads(response)
                if 'intent_summary' in result:
                    print(f"[IntentModeler] 解析到意图: {result.get('intent_summary', '')[:100]}...")
                    return result
            except json.JSONDecodeError:
                pass
            
            print("[IntentModeler] 无法解析意图，使用默认意图")
            return self._default_intent()
            
        except Exception as e:
            print(f"[IntentModeler] 建模失败: {e}")
            return self._default_intent()
    
    def _default_intent(self) -> Dict:
        """返回默认意图"""
        return {
            "intent_summary": "During kernel code execution, implemented a safety check to prevent potential vulnerabilities or crashes.",
            "subsystem": "kernel",
            "core_apis": []
        }


class ContextExtractor:
    """上下文提取器：提取子系统标签和词表关键词"""
    
    SUBSYSTEM_MAP = {
        "net": ["net", "socket", "sock", "sk_buff", "netlink", "tcp", "udp", "ip"],
        "fs": ["fs", "file", "inode", "dentry", "super_block", "vfs"],
        "mm": ["mm", "page", "vmalloc", "kmalloc", "slab", "mmap"],
        "drivers": ["driver", "device", "pci", "usb", "platform"],
        "kernel": ["sched", "task", "process", "signal", "cpu"],
        "security": ["security", "selinux", "apparmor", "capability"],
        "block": ["block", "bio", "request_queue", "gendisk"],
        "crypto": ["crypto", "cipher", "hash", "aes", "sha"],
    }
    
    def __init__(self, vocab_path: str = VOCAB_PATH):
        self.master_vocab = set()
        if os.path.exists(vocab_path):
            try:
                with open(vocab_path, 'r') as f:
                    self.master_vocab = set(json.load(f))
            except Exception:
                pass
    
    def get_subsystem(self, files_changed: str) -> str:
        """从 files_changed 字段提取 Linux 子系统"""
        if not files_changed:
            return "unknown"
        
        # 对齐用户提供的逻辑：取第一个文件的第一级目录作为子系统
        parts = files_changed.split('\n')[0].split('/')
        return parts[0] if parts else "unknown"
    
    def get_keywords(self, code: str) -> List[str]:
        """基于核心词表提取代码中的操作对象"""
        if not code:
            return []
        
        # 提取代码中所有的标识符
        words_in_code = set(re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', code))
        
        # 取交集：找出代码中出现的词表关键词
        matched_objs = words_in_code.intersection(self.master_vocab)
        
        # 排序：保持一定的确定性，并限制长度
        return list(matched_objs)[:15]
    
    def extract_struct_types(self, code: str) -> List[str]:
        """提取结构体类型"""
        struct_pattern = re.compile(r'\bstruct\s+([a-zA-Z_][a-zA-Z0-9_]*)\b')
        return list(set(struct_pattern.findall(code)))[:5]
    
    def extract_function_calls(self, code: str) -> List[str]:
        """提取真实的函数调用（摒除 C 原生语法词汇）"""
        func_pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(')
        calls = func_pattern.findall(code)
        
        # 必须排除这些伪函数，否则会挤占宝贵的额度
        ignore_list = {'if', 'for', 'while', 'switch', 'sizeof', 'return', 'likely', 'unlikely', 'IS_ERR'}
        real_funcs = [c for c in calls if c not in ignore_list]
        
        # 额度适当放宽到 10，确保拿到所有业务 API
        return list(set(real_funcs))[:10]
    
    def tokenize(self, code):
        """
        将代码片段分词
        
        Args:
            code: 代码片段
            
        Returns:
            分词后的词列表
        """
        # 提取代码中的所有标识符和关键词
        return re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', code)


class EmbeddingClient:
    """向量嵌入客户端"""
    
    def __init__(self, api_url: str = EMBED_API_URL):
        self.api_url = api_url
        self.timeout = 10
    
    def get_embedding(self, text: str) -> Optional[np.ndarray]:
        """获取文本的向量表示"""
        try:
            session = requests.Session()
            session.trust_env = False  # 确保不使用环境代理
            response = session.post(
                self.api_url,
                json={"content": text[:8000]},
                timeout=self.timeout
            )
            if response.status_code == 200:
                data = response.json()
                if "embedding" in data:
                    return np.array(data["embedding"])
            return None
        except Exception as e:
            print(f"❌ Embedding API 连接失败: {e}")
            return None
    
    def get_code_embedding(self, code: str, description: str = "") -> Optional[np.ndarray]:
        """获取代码的向量表示"""
        combined = f"{description}\n\n{code}" if description else code
        return self.get_embedding(combined)


class HybridRetriever:
    """混合检索器：意图向量匹配 + 加权重排"""
    
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.embedding_client = EmbeddingClient()
        self.context_extractor = ContextExtractor()
    
    def _get_connection(self) -> sqlite3.Connection:
        global _db_connection
        with _db_lock:
            if _db_connection is None:
                _db_connection = sqlite3.connect(self.db_path, check_same_thread=False)
            return _db_connection
    
    def search_by_intent_vec(self, intent_vec: np.ndarray, user_code_vec: np.ndarray,
                            user_code: str, user_apis: List[str], user_subsystem: str,
                            user_file_path: str, top_k: int = 100,
                            cutoff_author_date: Optional[str] = None) -> List[Dict]:
        """
        基于意图向量的检索 - 选出Top-100候选
        
        Args:
            intent_vec: 用户意图的向量表示
            user_code_vec: 用户代码的向量表示
            user_code: 用户代码文本
            user_apis: 用户代码中的核心API列表
            user_subsystem: 用户代码所属子系统
            user_file_path: 用户代码的文件路径
            top_k: 返回候选数量
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # 查询所有候选（包含intent_vec）
        if cutoff_author_date:
            query = """
                SELECT commit_id, author_date, files_changed, intent_summary, intent_vec,
                       subject, body, raw_patch
                FROM golden_knowledge
                WHERE intent_vec IS NOT NULL
                  AND author_date IS NOT NULL
                  AND datetime(author_date) < datetime(?)
            """
            cursor.execute(query, (cutoff_author_date,))
        else:
            query = """
                SELECT commit_id, author_date, files_changed, intent_summary, intent_vec,
                       subject, body, raw_patch
                FROM golden_knowledge
                WHERE intent_vec IS NOT NULL
            """
            cursor.execute(query)
        candidates = cursor.fetchall()
        
        # 收集所有候选补丁的信息
        candidates_info = []
        
        for row in candidates:
            # 解析数据库字段
            commit_id, author_date, files_changed, intent_summary, intent_vec_blob, subject, body, raw_patch = row
            commit_msg = f"{subject} {body}" if body else subject
            
            # 由于新表结构没有这些字段，使用默认值
            code_vec_blob = None  # 新表没有 embedding 字段
            buggy_code = ""  # 新表没有 buggy_code 字段
            obj_types = ""  # 新表没有 object_types 字段
            intents = ""  # 新表没有 intent_id 字段
            sub = ""  # 新表没有 subsystem 字段
            
            # 从 raw_patch 中提取 fixed_code
            fixed_code = ""
            if raw_patch:
                lines = raw_patch.split('\n')
                fixed_lines = [line[1:].strip() for line in lines if line.startswith('+') and not line.startswith('+++')]
                fixed_code = '\n'.join(fixed_lines)
            
            # 跳过没有意图向量的候选
            if intent_vec_blob is None:
                continue
            
            # 计算意图向量相似度（核心）
            cand_intent_vec = np.frombuffer(intent_vec_blob, dtype=np.float32)
            intent_norm = np.linalg.norm(intent_vec)
            cand_intent_norm = np.linalg.norm(cand_intent_vec)
            if intent_norm == 0 or cand_intent_norm == 0:
                intent_similarity = 0
            else:
                intent_similarity = np.dot(intent_vec, cand_intent_vec) / (intent_norm * cand_intent_norm)
            
            # 计算代码向量相似度（新表没有 embedding 字段，使用默认值）
            code_similarity = 0.0
            
            # 子系统匹配（基于文件路径匹配）
            sub_score = self._match_subsystem(user_file_path, files_changed)
            
            # API匹配（从raw_patch中提取API）
            api_score = self._match_apis(user_apis, raw_patch)
            
            candidates_info.append({
                "commit_id": commit_id,
                "intent_similarity": intent_similarity,
                "code_similarity": code_similarity,
                "sub_score": sub_score,
                "api_score": api_score,
                "buggy_code": buggy_code,
                "fixed_code": fixed_code,
                "object_types": obj_types,
                "intents": intents,
                "subsystem": sub,
                "commit_message": commit_msg,
                "files_changed": files_changed,
                "raw_patch": raw_patch
            })
        
        # 第一阶段排序：基于意图向量相似度选出Top-100
        candidates_info.sort(key=lambda x: x['intent_similarity'], reverse=True)
        top_candidates = candidates_info[:top_k]
        
        return top_candidates
    
    def _match_subsystem(self, user_file_path: str, cand_files_changed: str) -> float:
        """
        子系统匹配：基于文件路径匹配
        只要除了具体文件名之外的路径可以匹配，就认为子系统相同
        
        Args:
            user_file_path: 用户代码的文件路径（如 "net/netlink/af_netlink.c"）
            cand_files_changed: 候选补丁的文件变更（如 "net/netlink/af_netlink.c"）
            
        Returns:
            匹配得分（1.0 表示匹配，0.0 表示不匹配）
        """
        if not user_file_path or not cand_files_changed:
            return 0.0
        
        try:
            # 提取用户文件的目录路径（去除文件名）
            user_dir = os.path.dirname(user_file_path).strip()
            if not user_dir:
                return 0.0
            
            # 提取候选文件的目录路径（去除文件名）
            # 处理可能的多个文件变更（用逗号分隔）
            cand_files = cand_files_changed.split(',')
            for cand_file in cand_files:
                cand_file = cand_file.strip()
                if not cand_file:
                    continue
                cand_dir = os.path.dirname(cand_file).strip()
                if not cand_dir:
                    continue
                
                # 检查目录路径是否匹配
                # 不区分大小写，忽略前后空格
                if user_dir.lower() == cand_dir.lower():
                    return 1.0
            
            return 0.0
        except Exception:
            return 0.0
    
    def _match_apis(self, user_apis: List[str], raw_patch: str) -> float:
        """
        API匹配：从raw_patch中提取API并计算匹配度
        
        Args:
            user_apis: 用户提供的API列表
            raw_patch: 原始补丁内容
            
        Returns:
            匹配得分（0.0-1.0）
        """
        if not user_apis or not raw_patch:
            return 0.0
        
        # 从raw_patch中提取API
        patch_apis = set()
        # 匹配函数调用模式：函数名(
        matches = re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*\(', raw_patch)
        for api in matches:
            # 过滤掉常见的辅助函数和关键字
            if len(api) > 2 and api not in ['if', 'for', 'while', 'return', 'break', 'continue', 'sizeof', 'typeof']:
                patch_apis.add(api.lower())
        
        # 计算用户API与补丁API的交集
        user_api_set = set(api.lower() for api in user_apis)
        intersection = user_api_set.intersection(patch_apis)
        
        if not user_api_set:
            return 0.0
        
        return 1


class ASTReranker:
    """AST 重排序器：基于 Tree-sitter 结构比对和 Tree Edit Distance"""
    
    def __init__(self, language_so_path: str = paths.LANG_SO_PATH):
        self.parser = None
        self.language_so_path = language_so_path
        self._init_parser()
    
    def _init_parser(self):
        if self.parser is None:
            try:
                from tree_sitter import Language, Parser
                
                if not os.path.exists(self.language_so_path):
                    print(f"警告: Tree-sitter so 文件不存在: {self.language_so_path}")
                    return
                
                # 初始化 C 语言解析器
                c_lang = Language(self.language_so_path, 'c')
                self.parser = Parser()
                self.parser.set_language(c_lang)
            except ImportError:
                print("警告: 未安装 tree-sitter")
            except Exception as e:
                print(f"警告: 解析器初始化失败: {e}")
    
    def parse_code(self, code: str) -> Optional[Dict]:
        """解析代码获取 AST 结构"""
        self._init_parser()
        if self.parser is None:
            return None
        
        try:
            tree = self.parser.parse(bytes(code, "utf8"))
            return self._extract_structure(tree.root_node)
        except Exception:
            return None
    
    def _extract_structure(self, node) -> Dict:
        """提取 AST 结构特征（改进版本，保留谓词语义）"""
        # 泛化处理
        node_type = node.type
        
        # 对于函数调用，保留真实的函数名作为节点类型
        if node_type == 'call_expression':
            # 尝试获取函数名
            for i in range(node.child_count):
                child = node.child(i)
                if child.type == 'identifier':
                    node_type = f"CALL_{child.text.decode('utf-8')}"
                    break
        elif node_type == 'identifier':
            node_type = 'VAR'
        elif node_type in ['number_literal', 'string_literal']:
            node_type = 'CONST'
        
        structure = {
            "type": node_type,
            "children": []
        }
        
        # 增加对表达式细节的捕获
        relevant_types = ['if_statement', 'binary_expression', 'unary_expression', 'call_expression',
                         'function_definition', 'declaration', 'for_statement', 'while_statement',
                         'return_statement', 'expression_statement', 'compound_statement']
        
        for i in range(node.child_count):
            child = node.child(i)
            if child.type in relevant_types or child.child_count > 0:
                structure["children"].append(self._extract_structure(child))
        
        return structure
    
    def compute_tree_edit_distance(self, struct1: Dict, struct2: Dict) -> float:
        """计算两个 AST 结构的树编辑距离"""
        if struct1 is None or struct2 is None:
            return 0.0
        
        # 计算两个树的节点数量
        nodes1 = self._count_nodes(struct1)
        nodes2 = self._count_nodes(struct2)
        max_nodes = max(nodes1, nodes2)
        
        if max_nodes == 0:
            return 1.0
        
        # 简化的树编辑距离计算
        def ted(node1, node2):
            if node1 is None:
                return len(node2.get("children", [])) if node2 else 0
            if node2 is None:
                return len(node1.get("children", [])) if node1 else 0
            
            # 节点类型不同，增加编辑成本
            if node1.get("type") != node2.get("type"):
                cost = 1
            else:
                cost = 0
            
            children1 = node1.get("children", [])
            children2 = node2.get("children", [])
            
            # 计算子节点的编辑距离
            len1, len2 = len(children1), len(children2)
            dp = [[0] * (len2 + 1) for _ in range(len1 + 1)]
            
            # 初始化边界条件
            for i in range(len1 + 1):
                dp[i][0] = i
            for j in range(len2 + 1):
                dp[0][j] = j
            
            # 动态规划计算最小编辑距离
            for i in range(1, len1 + 1):
                for j in range(1, len2 + 1):
                    if children1[i-1].get("type") == children2[j-1].get("type"):
                        # 子节点类型相同，递归计算
                        dp[i][j] = dp[i-1][j-1] + ted(children1[i-1], children2[j-1])
                    else:
                        # 子节点类型不同，取最小编辑操作
                        dp[i][j] = min(dp[i-1][j] + 1,  # 删除
                                      dp[i][j-1] + 1,  # 插入
                                      dp[i-1][j-1] + 1)  # 替换
            
            return cost + dp[len1][len2]
        
        distance = ted(struct1, struct2)
        
        # 转换为相似度（0-1之间）
        # 确保相似度在合理范围内
        similarity = 1.0 - (distance / max_nodes)
        return max(0.0, min(1.0, similarity))
    
    def _count_nodes(self, struct: Dict) -> int:
        """计算 AST 结构中的节点数量"""
        if struct is None:
            return 0
        count = 1
        for child in struct.get("children", []):
            count += self._count_nodes(child)
        return count
    
    def rerank(self, candidates: List[Dict], user_code: str, intent_vec: np.ndarray = None) -> List[Dict]:
        """
        对候选补丁进行结构化重排（第二阶段）
        
        计算物理匹配分：
        S_struct = (0.2 + 0.8 × AST_sim) × (Hit_subsystem ? 1.5 : 1.0) × (Hit_api ? 1.2 : 1.0)
        """
        user_struct = self.parse_code(user_code)
        
        # 对所有候选补丁进行 AST 解析
        for candidate in candidates:
            # AST相似度计算
            if user_struct is not None:
                cand_struct = self.parse_code(candidate.get("buggy_code", ""))
                ast_score = self.compute_tree_edit_distance(user_struct, cand_struct)
            else:
                ast_score = 0.0
            
            candidate["ast_score"] = ast_score
            
            # 防归零机制：为AST加基底
            ast_base = 0.2 + 0.8 * ast_score
            
            # 子系统命中×1.5
            sub_multiplier = 1.5 if candidate.get("sub_score", 0) > 0 else 1.0
            
            # 核心API命中×1.2
            api_multiplier = 1.2 if candidate.get("api_score", 0) > 0 else 1.0
            
            # 物理匹配分
            struct_score = ast_base * sub_multiplier * api_multiplier
            candidate["struct_score"] = struct_score
        
        # 按物理匹配分排序，生成Rank_B
        candidates.sort(key=lambda x: x.get("struct_score", 0), reverse=True)
        for i, candidate in enumerate(candidates, 1):
            candidate["rank_b"] = i
        
        return candidates


class KernelFixExpert:
    """内核修复专家：整合所有组件"""
    
    def __init__(self):
        self.intent_modeler = IntentModeler()
        self.context_extractor = ContextExtractor()
        self.retriever = HybridRetriever()
        self.reranker = ASTReranker()
    
    async def run(self, buggy_code: str, description: str, file_path: str,
                  cutoff_author_date: Optional[str] = None) -> Dict:
        """
        执行修复建议检索（新架构：意图建模器 + 向量检索 + 加权重排）
        
        Args:
            buggy_code: 待修复的代码片段
            description: 对该代码片段想要进行的修复操作描述
            file_path: 代码所在文件路径
            
        Returns:
            包含 Top 3 黄金补丁的字典
        """
        import time
        start_time = time.time()
        
        # 步骤1：意图建模（生成自然语言意图描述 + subsystem + 核心API）
        intent_result = await asyncio.to_thread(
            self.intent_modeler.model_intent, buggy_code, description
        )
        
        intent_summary = intent_result.get("intent_summary", "")
        subsystem = intent_result.get("subsystem", "kernel")
        core_apis = intent_result.get("core_apis", [])
        
        # 步骤2：生成向量表示
        # 意图向量
        intent_vec = await asyncio.to_thread(
            self.retriever.embedding_client.get_embedding, intent_summary
        )
        
        # 代码向量
        user_code_vec = await asyncio.to_thread(
            self.retriever.embedding_client.get_code_embedding, buggy_code, description
        )
        
        if intent_vec is None or user_code_vec is None:
            return {
                "status": "error",
                "message": "Failed to generate embeddings",
                "intent_summary": intent_summary,
                "subsystem": subsystem,
                "core_apis": core_apis,
                "results": []
            }
        
        # 步骤3：基于意图向量的检索（选出Top-100）
        candidates = await asyncio.to_thread(
            self.retriever.search_by_intent_vec,
            intent_vec, user_code_vec, buggy_code, core_apis, subsystem,
            file_path, top_k=100, cutoff_author_date=cutoff_author_date
        )
        
        if not candidates:
            return {
                "status": "no_match",
                "message": "No matching patches found",
                "intent_summary": intent_summary,
                "subsystem": subsystem,
                "results": []
            }
        
        # 步骤4：计算Rank_A（基于意图向量相似度的排名）
        candidates.sort(key=lambda x: x.get("intent_similarity", 0), reverse=True)
        for i, candidate in enumerate(candidates, 1):
            candidate["rank_a"] = i
        
        # 步骤5：结构化重排，获取Rank_B
        reranked = await asyncio.to_thread(
            self.reranker.rerank, candidates, buggy_code, intent_vec
        )
        
        # 步骤6：RRF融合计算最终得分（k=60）
        k = 60
        for candidate in reranked:
            rank_a = candidate.get("rank_a", 100)
            rank_b = candidate.get("rank_b", 100)
            # RRF公式：1/(k + rank)
            rrf_score = 1/(k + rank_a) + 1/(k + rank_b)
            candidate["rrf_score"] = rrf_score
        
        # 按RRF得分排序
        reranked.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)
        top_3 = reranked[:3]
        
        results = []
        for i, patch in enumerate(top_3, 1):
            # 提取 commit message 和 raw patch
            commit_msg = patch.get("commit_message", "")
            raw_patch = patch.get("raw_patch", "")
            
            # 直接使用 raw_patch 作为 diff 格式
            diff_content = raw_patch
            
            results.append({
                "rank": i,
                "commit_message": commit_msg,
                "diff_format": diff_content,
                "rrf_score": patch.get("rrf_score", 0),
                "intent_similarity": patch.get("intent_similarity", 0),
                "struct_score": patch.get("struct_score", 0),
                "ast_score": patch.get("ast_score", 0),
                "sub_score": patch.get("sub_score", 0),
                "api_score": patch.get("api_score", 0),
                "rank_a": patch.get("rank_a", 0),
                "rank_b": patch.get("rank_b", 0)
            })
        
        return {
            "status": "success",
            "intent_summary": intent_summary,
            "subsystem": subsystem,
            "core_apis": core_apis,
            "usage_guide": "ATTENTION REPAIR AGENT: The following 'results' are historical reference patches (Diff RAG). DO NOT blindly copy the exact variable names or subsystem details. You MUST use Contrastive Reasoning: Analyze the STRUCTURAL PARADIGM of these fixes (e.g., How was the API migrated? Were new local variables introduced for pointers? How did the error code check change?). Apply this structural transformation logic strictly to the original buggy code.",
            "results": results
        }
    
    def run_sync(self, buggy_code: str, description: str, file_path: str,
                 cutoff_author_date: Optional[str] = None) -> Dict:
        """同步版本的 run 方法"""
        return asyncio.run(self.run(buggy_code, description, file_path, cutoff_author_date=cutoff_author_date))


_expert_instance = None
_expert_lock = threading.Lock()


def get_kernel_fix_expert() -> KernelFixExpert:
    """获取 KernelFixExpert 单例"""
    global _expert_instance
    with _expert_lock:
        if _expert_instance is None:
            _expert_instance = KernelFixExpert()
        return _expert_instance


def query_golden_fixes(buggy_code: str, description: str, file_path: str,
                       cutoff_author_date: Optional[str] = None) -> Dict:
    """
    查询黄金修复补丁（同步接口）
    
    Args:
        buggy_code: 待修复的代码片段
        description: Bug 描述
        file_path: 代码所在文件路径
        
    Returns:
        包含 Top 3 黄金补丁的字典
    """
    expert = get_kernel_fix_expert()
    return expert.run_sync(buggy_code, description, file_path, cutoff_author_date=cutoff_author_date)

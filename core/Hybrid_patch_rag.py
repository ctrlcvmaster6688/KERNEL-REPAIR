import sqlite3
import pickle
import numpy as np
import requests
import re
import os
import sys
import threading
from datetime import datetime
from rank_bm25 import BM25Okapi

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config import Config

_load_lock = threading.Lock()

DB_PATH = Config.DB_PATH
BM25_PATH = Config.BM25_PATH
EMBED_API_URL = Config.EMBED_API_URL

# 全局数据缓存，所有线程共享
_global_cache = {
    'syz_ids': None,
    'timestamps': None,
    'desc_vector_matrix': None,
    'trace_vector_matrix': None,
    'commit_vector_matrix': None,
    'id_to_idx': None,
    'bm25': None,
    'is_loaded': False
}

class MasterOmniKernelParser:
    def __init__(self):
        # 1. 优先级最高：描述性结论 (判定为 Description)
        self.desc_priority_re = re.compile(
            r"^(BUG:|WARNING:|KASAN:|ODEBUG:|Kernel panic|inconsistent|possible|"
            r"corruption|invalid opcode|unreferenced object|Looking for class|"
            r"\*\*\* DEADLOCK \*\*\*|RIP:|Hardware name:)", 
            re.IGNORECASE
        )

        # 2. 机器元数据 (判定为 Metadata)
        self.meta_re = re.compile(
            r"^(pc|lr|sp|x[0-9]+|RSP|RAX|RBX|RCX|RDX|RSI|RDI|RBP|R[0-9]{2}|FS|CS|DS|ES|CR[0-9]|DR[0-9]|pstate|"
            r"irq event stamp|hardirqs last|softirqs last|Code|Kernel Offset|Rebooting in|hex dump|Stack:|"
            r"[\s\-]*[0-9a-fA-F]{2}(\s[0-9a-fA-F]{2}){5,}|[\=\-]{10,})",
            re.IGNORECASE
        )

        # 3. 堆栈上下文 (CPU, PID, Workqueue 等)
        self.stack_ctx_re = re.compile(r"^(CPU:|PID:|Comm:|Hardware name:|Workqueue:|Modules linked in:)")

        # 4. 堆栈引导词
        self.trace_headers = ["Call Trace:", "Call trace:", "stack backtrace:", "backtrace:"]
        
        # 5. 函数偏移特征 (堆栈行核心)
        self.stack_line_re = re.compile(r"(\+0x[0-9a-fA-F]+|\[inline\]|\[<[0-9a-fA-F]+>\])")

        # 6. 【堆栈清洗模块】去除偏移量、地址、编译器后缀等噪音
        self.trace_cleaner_re = re.compile(
            r"(\+0x[0-9a-fA-F/]+)|"            # +0x123/0x456
            r"(\[<[0-9a-fA-F]+>\])|"          # [<ffff...>]
            r"(\.(part|isra|constprop)\.[0-9]+)|" # .part.0
            r"(x[0-9a-fA-F]{2,}(?=\s|$))|"    # 粘连在末尾的偏移量噪音
            r"\?"                             # 问号
        )

    def parse(self, raw_report: str):
        lines = raw_report.splitlines()
        description, calltraces, metadata = [], [], []
        current_trace = []
        
        for i, line in enumerate(lines):
            clean_line = line.strip()
            if not clean_line or clean_line.startswith("executing program"): continue

            is_desc_priority = bool(self.desc_priority_re.match(clean_line))
            is_meta = bool(self.meta_re.match(clean_line))
            is_stack_ctx = bool(self.stack_ctx_re.match(clean_line))
            is_stack_line = bool(self.stack_line_re.search(line))
            is_trace_header = any(h in line for h in self.trace_headers)
            is_irq_tag = (clean_line.startswith("<") and clean_line.endswith(">"))

            if is_desc_priority:
                self._finalize_trace(current_trace, calltraces)
                description.append(line)
            
            elif is_trace_header:
                if current_trace and not any(h in current_trace[-1] for h in self.trace_headers):
                    self._finalize_trace(current_trace, calltraces)
                current_trace.append(line)

            elif is_stack_line or is_irq_tag:
                current_trace.append(line)

            elif is_stack_ctx:
                self._finalize_trace(current_trace, calltraces)
                description.append(line)

            elif is_meta:
                self._finalize_trace(current_trace, calltraces)
                metadata.append(line)

            else:
                if current_trace:
                    self._finalize_trace(current_trace, calltraces)
                description.append(line)

        self._finalize_trace(current_trace, calltraces)

        return {
            "Description": "\n".join(description).strip(),
            "Calltraces": calltraces,
            "Metadata": "\n".join(metadata).strip()
        }

    def _finalize_trace(self, current_trace, calltraces):
        if current_trace:
            processed_lines = []
            for line in current_trace:
                clean_line = line.strip()
                # --- 【核心改进：双重过滤】 ---
                # 1. 检查是否是引导词 (Call Trace: 等)
                is_header = any(h in line for h in self.trace_headers)
                # 2. 检查是否是标签 (<TASK>, <IRQ>, </TASK>, </IRQ> 等)
                is_tag = (clean_line.startswith("<") and clean_line.endswith(">"))
                
                if is_header or is_tag:
                    # 如果是引导词或标签，直接跳过，不存入 processed_lines
                    continue
                
                if self.stack_line_re.search(line):
                    # 仅对真实的函数行执行深度清洗
                    cleaned = self.trace_cleaner_re.sub("", line)
                    cleaned = " ".join(cleaned.split()).strip()
                    processed_lines.append(cleaned)
            
            # 只有当清洗后确实有内容时才添加
            if processed_lines:
                calltraces.append("\n".join(processed_lines))
            current_trace.clear()
class KernelKnowledgeRetriever:
    def __init__(self):
        self.is_loaded = False
        self.syz_ids = []
        self.timestamps = []
        self.vector_matrix = None
        self.id_to_idx = {}
        self.bm25 = None

    # ---------------- 核心预处理工具 ----------------

    def _distill_testcase(self, text, timeout_check=None):
        """语义蒸馏：从 Syzkaller 脚本中提取核心系统调用和路径"""
        if not text: return ""
        distilled = []
        for i, line in enumerate(text.splitlines()):
            # 每 100 行检查一次超时
            if timeout_check and i % 100 == 0:
                timeout_check()
            
            line = line.strip()
            if not line or line.startswith('#'): continue
            m = re.search(r'([a-zA-Z0-9_$]+)\(', line)
            if m: distilled.append(m.group(1))
            paths = re.findall(r"['\"](/[^'\"]+)['\"]", line)
            distilled.extend(paths)
        return " ".join(distilled)

    def _kernel_tokenizer(self, text, timeout_check=None):
        """内核特征提取：过滤内存地址，保留函数名和标识符"""
        if not text: return []
        
        # 检查超时
        if timeout_check:
            timeout_check()
        
        tokens = re.findall(r'[a-zA-Z_/][a-zA-Z0-9_$./]{2,}', text)
        
        # 过滤 token，添加超时检查
        result = []
        for i, t in enumerate(tokens):
            # 每 100 个 token 检查一次超时
            if timeout_check and i % 100 == 0:
                timeout_check()
            
            t_lower = t.lower()
            if not (t.startswith(('0x', 'ffff')) or re.match(r'^[0-9a-fA-F]+$', t)):
                result.append(t_lower)
        
        return result

    def _get_embedding(self, text):
        """调用 BGE-M3 API 获取高维向量"""
        # 如果文本为空，直接返回 None，不调用 API
        if not text or not text.strip():
            print(f"⚠️ Embedding 输入为空，跳过向量计算")
            return None
        
        max_retries = 2  # 最多重试 2 次
        for attempt in range(max_retries):
            session = None
            try:
                session = requests.Session()
                session.trust_env = False  # 再次确保不使用环境代理
                resp = session.post(EMBED_API_URL, json={"content": text[:8000]}, timeout=20)
                if resp.status_code == 200:
                    return np.array(resp.json()['embedding'])
                elif resp.status_code == 400:
                    print(f"⚠️ Embedding API 返回 400 错误 (尝试 {attempt + 1}/{max_retries}): {resp.text[:200]}")
                    if attempt < max_retries - 1:
                        print(f"   重试中...")
                        continue
                    else:
                        print(f"❌ Embedding API 重试失败，跳过此向量")
                        return None
                else:
                    print(f"❌ Embedding API 返回错误: {resp.status_code} - {resp.text[:200]}")
                    return None
            except Exception as e:
                print(f"❌ Embedding API 连接失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    print(f"   重试中...")
                    continue
                else:
                    return None
            finally:
                if session:
                    session.close()
        return None

    # ---------------- 序列校验算法 (Physical DNA Match) ----------------

    def _extract_trace_functions(self, trace_text, timeout_check=None, max_items=None):
        """从清洗后的堆栈文本中提取函数名和文件名列表"""
        if not trace_text:
            return []
        sequence = []
        for i, line in enumerate(trace_text.split('\n')):
            # 每 100 行检查一次超时
            if timeout_check and i % 100 == 0:
                timeout_check()
            
            line = line.strip()
            if not line:
                continue
            
            # 提取函数名（允许带点，如 func.isra.0 或 .part.0）
            # 匹配模式：字母下划线开头，后跟字母数字下划线点
            func_match = re.search(r'[a-zA-Z_][a-zA-Z0-9_\.]*', line)
            if func_match:
                func_name = func_match.group(0)
                # 过滤掉常见的非函数标识符
                if func_name not in ['inline', 'const', 'static', 'void', 'int', 'char']:
                    # 提取文件名（如 fs/btrfs/ctree.c）
                    file_match = re.search(r'\b[a-zA-Z0-9_/]+\.[ch]\b', line)
                    if file_match:
                        file_name = file_match.group(0)
                        # 将函数名和文件名组合，增强物理唯一性
                        sequence.append(f"{func_name}@{file_name}")
                    else:
                        sequence.append(func_name)
                    if max_items is not None and len(sequence) >= max_items:
                        break
        return sequence

    def _weighted_levenshtein_distance(self, seq1, seq2, timeout_check=None):
        """
        计算加权编辑距离
        顶部函数（靠近 Crash Site）权重更高
        """
        if not seq1 or not seq2:
            return 0.0
        
        m, n = len(seq1), len(seq2)
        # 动态规划矩阵
        dp = [[0] * (n + 1) for _ in range(m + 1)]
        
        # 初始化
        for i in range(m + 1):
            dp[i][0] = i
            # 每 10 行检查一次超时
            if timeout_check and i % 10 == 0:
                timeout_check()
        
        for j in range(n + 1):
            dp[0][j] = j
        
        # 填充矩阵
        for i in range(1, m + 1):
            for j in range(1, n + 1):
                if seq1[i-1] == seq2[j-1]:
                    dp[i][j] = dp[i-1][j-1]
                else:
                    dp[i][j] = 1 + min(dp[i-1][j], dp[i][j-1], dp[i-1][j-1])
            
            # 每处理完一行检查一次超时
            if timeout_check and i % 5 == 0:
                timeout_check()
        
        # 计算加权相似度
        max_len = max(m, n)
        if max_len == 0:
            return 1.0
        
        # 基础编辑距离
        base_distance = dp[m][n]
        base_similarity = 1 - base_distance / max_len
        
        # 位置敏感加权：顶部函数权重更高
        weighted_matches = 0
        total_weight = 0
        
        for i in range(min(m, n)):
            # 权重随位置指数衰减，前5个函数权重最高
            weight = max(1.0, 3.0 * (0.7 ** i))
            total_weight += weight
            if seq1[i] == seq2[i]:
                weighted_matches += weight
        
        # 加权相似度
        if total_weight > 0:
            weighted_similarity = weighted_matches / total_weight
        else:
            weighted_similarity = 0.0
        
        # 融合基础相似度和加权相似度
        final_similarity = 0.4 * base_similarity + 0.6 * weighted_similarity
        
        return final_similarity

    def _extract_syscalls(self, testcase_text, timeout_check=None, max_items=None):
        """从 testcase 中提取系统调用序列"""
        if not testcase_text:
            return []
        syscalls = []
        for i, line in enumerate(testcase_text.splitlines()):
            # 每 100 行检查一次超时
            if timeout_check and i % 100 == 0:
                timeout_check()
            
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # syzkaller testcase 的 syscall 名通常位于行首。
            # 对于 syz_mount_image$xxx 这类超长单行，只提取行首 syscall，
            # 避免在巨大参数 blob 上做全文正则扫描。
            syscall_match = re.match(r'([a-zA-Z_][a-zA-Z0-9_$]*)\s*\(', line)
            if syscall_match:
                syscalls.append(syscall_match.group(1))
            if max_items is not None and len(syscalls) >= max_items:
                return syscalls[:max_items]
        return syscalls

    def _lcs_similarity(self, seq1, seq2, timeout_check=None):
        """
        计算最长公共子序列 (LCS) 相似度
        优化：考虑长度差异的惩罚，避免过度泛化
        """
        if not seq1 or not seq2:
            return 0.0
        
        m, n = len(seq1), len(seq2)
        
        # 动态规划计算 LCS 长度
        dp = [[0] * (n + 1) for _ in range(m + 1)]
        
        for i in range(1, m + 1):
            for j in range(1, n + 1):
                if seq1[i-1] == seq2[j-1]:
                    dp[i][j] = dp[i-1][j-1] + 1
                else:
                    dp[i][j] = max(dp[i-1][j], dp[i][j-1])
            
            # 每处理完一行检查一次超时
            if timeout_check and i % 5 == 0:
                timeout_check()
        
        lcs_length = dp[m][n]
        min_len = min(m, n)
        max_len = max(m, n)
        
        if min_len == 0:
            return 0.0
        
        # 基础 LCS 相似度
        base_similarity = lcs_length / min_len
        
        # 长度差异惩罚因子：长度差异越大，惩罚越大
        # 当长度接近时，惩罚因子接近 1；当长度差异大时，惩罚因子减小
        if max_len > 0:
            length_ratio = min_len / max_len
            length_penalty = 0.7 + 0.3 * length_ratio
        else:
            length_penalty = 1.0
        
        # 最终相似度：基础相似度 * 长度惩罚因子
        final_similarity = base_similarity * length_penalty
        
        return final_similarity

    # ---------------- 资源加载 ----------------

    def load_resources(self):
        """将索引和向量加载到内存，实现极速检索"""
        global _global_cache
        
        with _load_lock:
            if _global_cache['is_loaded']:
                # 使用全局缓存的数据
                print("✅ 使用全局缓存数据，无需重新加载")
                self.syz_ids = _global_cache['syz_ids']
                self.timestamps = _global_cache['timestamps']
                self.desc_vector_matrix = _global_cache['desc_vector_matrix']
                self.trace_vector_matrix = _global_cache['trace_vector_matrix']
                self.commit_vector_matrix = _global_cache['commit_vector_matrix']
                self.id_to_idx = _global_cache['id_to_idx']
                self.bm25 = _global_cache['bm25']
                self.is_loaded = True
                return
            
            print("⏳ 正在初始化内核知识检索系统...")
            if not os.path.exists(DB_PATH) or not os.path.exists(BM25_PATH):
                raise FileNotFoundError("数据库或 BM25 索引文件不存在，请先运行数据预处理脚本。")

            syz_ids = []
            timestamps = []
            desc_vector_matrix = None
            trace_vector_matrix = None
            commit_vector_matrix = None
            id_to_idx = {}
            bm25 = None

            # 使用 WAL 模式和超时设置
            conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            
            cursor.execute("SELECT syzbot_id, author_date, desc_vec, trace_vec, commit_vec FROM commits_rag")
            data = cursor.fetchall()

            desc_vec_list = []
            trace_vec_list = []
            commit_vec_list = []
            timestamps_list = []
            for i, (sid, date_str, desc_vec_blob, trace_vec_blob, commit_vec_blob) in enumerate(data):
                syz_ids.append(sid)
                try:
                    ts = datetime.strptime(date_str[:19], "%Y-%m-%d %H:%M:%S").timestamp()
                except:
                    ts = 0
                timestamps_list.append(ts)
                id_to_idx[sid] = i
                
                if desc_vec_blob:
                    desc_vec_list.append(np.frombuffer(desc_vec_blob, dtype=np.float32))
                else:
                    desc_vec_list.append(np.zeros(1024))
                
                if trace_vec_blob:
                    trace_vec_list.append(np.frombuffer(trace_vec_blob, dtype=np.float32))
                else:
                    trace_vec_list.append(np.zeros(1024))
                
                if commit_vec_blob:
                    commit_vec_list.append(np.frombuffer(commit_vec_blob, dtype=np.float32))
                else:
                    commit_vec_list.append(np.zeros(1024))

            desc_vector_matrix = np.vstack(desc_vec_list)
            trace_vector_matrix = np.vstack(trace_vec_list)
            commit_vector_matrix = np.vstack(commit_vec_list)
            timestamps = np.array(timestamps_list)

            print(f"📊 数据结构长度检查:")
            print(f"   - syz_ids: {len(syz_ids)}")
            print(f"   - desc_vector_matrix rows: {desc_vector_matrix.shape[0]}")
            print(f"   - trace_vector_matrix rows: {trace_vector_matrix.shape[0]}")
            print(f"   - commit_vector_matrix rows: {commit_vector_matrix.shape[0]}")
            print(f"   - timestamps: {len(timestamps)}")
            print(f"   - id_to_idx: {len(id_to_idx)}")

            with open(BM25_PATH, "rb") as f:
                bm25 = pickle.load(f)["model"]

            conn.close()
            
            # 保存到全局缓存
            _global_cache['syz_ids'] = syz_ids
            _global_cache['timestamps'] = timestamps
            _global_cache['desc_vector_matrix'] = desc_vector_matrix
            _global_cache['trace_vector_matrix'] = trace_vector_matrix
            _global_cache['commit_vector_matrix'] = commit_vector_matrix
            _global_cache['id_to_idx'] = id_to_idx
            _global_cache['bm25'] = bm25
            _global_cache['is_loaded'] = True
            
            # 同时设置实例变量
            self.syz_ids = syz_ids
            self.timestamps = timestamps
            self.desc_vector_matrix = desc_vector_matrix
            self.trace_vector_matrix = trace_vector_matrix
            self.commit_vector_matrix = commit_vector_matrix
            self.id_to_idx = id_to_idx
            self.bm25 = bm25
            self.is_loaded = True
            
            print(f"✅ 系统就绪！共加载 {len(syz_ids)} 条内核历史案例。")

    def close(self):
        """释放实例资源（不释放全局缓存）"""
        # 只释放实例变量，不释放全局缓存
        # 这样其他线程可以继续使用全局缓存
        self.is_loaded = False
        print("🔄 实例资源已释放（全局缓存保留）")

    # ---------------- 知识检索逻辑 ----------------

    def search_knowledge(self, ref_syz_id, report_text, testcase_text="", rootcause_text="", strict_time=False,
                        timeout_check=None):
        """
        多模态结构化语义对齐检索：
        1. 三路稠密语义对齐 (Dense): desc(0.3) + trace(0.3) + rootcause(0.4)
        2. 结构化稀疏检索 (BM25): Description + Trace + Testcase
        3. 物理指纹序列校验 (Rerank): 加权编辑距离 + LCS
        4. 两阶段融合评分: RRF召回 + 精排加权
        """
        if not self.is_loaded: self.load_resources()

        # 检查数据结构长度是否匹配
        print(f"🔍 搜索前数据结构长度检查:")
        print(f"   - syz_ids: {len(self.syz_ids)}")
        print(f"   - desc_vector_matrix rows: {self.desc_vector_matrix.shape[0]}")
        print(f"   - trace_vector_matrix rows: {self.trace_vector_matrix.shape[0]}")
        print(f"   - commit_vector_matrix rows: {self.commit_vector_matrix.shape[0]}")
        print(f"   - timestamps: {len(self.timestamps)}")
        print(f"   - id_to_idx: {len(self.id_to_idx)}")

        # 确保所有数据结构长度匹配（使用局部变量，避免修改实例状态）
        min_length = min(len(self.syz_ids), self.desc_vector_matrix.shape[0], 
                        self.trace_vector_matrix.shape[0], self.commit_vector_matrix.shape[0], 
                        len(self.timestamps))
        
        # 使用局部变量存储截断后的数据，避免修改实例状态
        syz_ids = self.syz_ids
        desc_vector_matrix = self.desc_vector_matrix
        trace_vector_matrix = self.trace_vector_matrix
        commit_vector_matrix = self.commit_vector_matrix
        timestamps = self.timestamps
        id_to_idx = self.id_to_idx
        
        if min_length < len(syz_ids):
            print(f"⚠️ 数据结构长度不匹配，截断到最小长度: {min_length}")
            syz_ids = syz_ids[:min_length]
            desc_vector_matrix = desc_vector_matrix[:min_length]
            trace_vector_matrix = trace_vector_matrix[:min_length]
            commit_vector_matrix = commit_vector_matrix[:min_length]
            timestamps = timestamps[:min_length]
            # 重新构建 id_to_idx
            id_to_idx = {sid: i for i, sid in enumerate(syz_ids)}

        # 1. 构造掩码（防止数据泄露）
        identity_mask = np.array([sid != ref_syz_id for sid in syz_ids], dtype=bool)
        
        final_mask = identity_mask.copy()
        if strict_time and ref_syz_id in id_to_idx:
            try:
                ref_ts = timestamps[id_to_idx[ref_syz_id]]
                if len(identity_mask) == len(timestamps):
                    timestamps_arr = np.array(timestamps)
                    time_mask = timestamps_arr < ref_ts
                    final_mask = identity_mask & time_mask
                else:
                    print(f"❌ 掩码长度不匹配: identity_mask={len(identity_mask)}, timestamps={len(timestamps)}")
            except Exception as e:
                print(f"❌ 时间戳过滤失败: {e}")
                final_mask = identity_mask.copy()
        # 2. 使用 MasterOmniKernelParser 清洗 report
        parser = MasterOmniKernelParser()
        parsed_report = parser.parse(report_text)
        
        query_desc = parsed_report.get("Description", "")
        query_traces = parsed_report.get("Calltraces", [])  # 可能有多个堆栈
        
        # 智能处理多个 trace：使用第一个 trace（通常是主要的崩溃堆栈）
        # 同时考虑合并所有 trace 作为补充
        if query_traces:
            # 第一个 trace 通常是主要的崩溃堆栈，优先级最高
            primary_trace = query_traces[0]
            # 合并所有 trace 作为补充信息
            merged_traces = "\n".join(query_traces)
            # 使用主要 trace 进行向量计算（更准确）
            query_trace = primary_trace
        else:
            query_trace = ""
        # 3. 三路稠密语义对齐 (Dense Semantic Alignment)
        # S_dense = ω1·Sim(Q_desc, H_desc) + ω2·Sim(Q_trace, H_trace) + ω3·Sim(Q_rc, H_commit)
        dense_weights = {
            'desc': 0.3,      # Sim(Q_desc, H_desc): 故障类型语义意图
            'trace': 0.3,     # Sim(Q_trace, H_trace): 执行路径演化趋势
            'rootcause': 0.4  # Sim(Q_rc, H_commit): 逻辑对齐核心
        }
        
        # 计算 Q_desc 与 H_desc 的相似度
        desc_query_vec = self._get_embedding(query_desc)
        
        desc_scores = None
        if desc_query_vec is not None:
            desc_scores = np.dot(desc_vector_matrix, desc_query_vec)
            desc_scores[~final_mask] = -1e9
        
        # 计算 Q_trace 与 H_trace 的相似度
        trace_query_vec = self._get_embedding(query_trace)
        
        trace_scores = None
        if trace_query_vec is not None:
            trace_scores = np.dot(trace_vector_matrix, trace_query_vec)
            trace_scores[~final_mask] = -1e9
        
        # 计算 Q_rc 与 H_commit 的相似度
        rootcause_query_vec = self._get_embedding(rootcause_text)
        
        rootcause_scores = None
        if rootcause_query_vec is not None:
            rootcause_scores = np.dot(commit_vector_matrix, rootcause_query_vec)
            rootcause_scores[~final_mask] = -1e9
        
        # 融合三路相似度得分
        dense_ranks = {}
        max_sim = 0
        
        # 构建融合得分矩阵
        fused_scores = np.zeros(len(syz_ids))
        valid_count = 0
        
        if desc_scores is not None:
            fused_scores += desc_scores * dense_weights['desc']
            valid_count += 1
        if trace_scores is not None:
            fused_scores += trace_scores * dense_weights['trace']
            valid_count += 1
        if rootcause_scores is not None:
            fused_scores += rootcause_scores * dense_weights['rootcause']
            valid_count += 1
        
        if valid_count > 0:
            max_sim = np.max(fused_scores[final_mask]) if np.any(final_mask) else 0
            dense_indices = np.argsort(fused_scores)[-100:][::-1]
            for r, idx in enumerate(dense_indices):
                if fused_scores[idx] < -1e8: break
                dense_ranks[syz_ids[idx]] = r + 1

        # 4. 结构化稀疏检索 (BM25)
        # 输入：清洗后的 Description + Normalized Trace + Distilled Testcase
        # BM25 使用所有 trace 拼接，因为索引时使用的是拼接的 trace
        if query_traces:
            # 所有 trace 拼接，用于 BM25 检索
            all_traces = "\n".join(query_traces)
            bm25_input = f"{query_desc} {all_traces} {self._distill_testcase(testcase_text, timeout_check=timeout_check)}"
        else:
            bm25_input = f"{query_desc} {self._distill_testcase(testcase_text, timeout_check=timeout_check)}"
        
        query_tokens = self._kernel_tokenizer(bm25_input, timeout_check=timeout_check)
        
        # 检查超时
        if timeout_check:
            timeout_check()
        
        bm25_scores = np.array(self.bm25.get_scores(query_tokens))
        
        # 检查超时
        if timeout_check:
            timeout_check()
        
        if len(final_mask) != len(bm25_scores):
            bm25_mask = np.ones(len(bm25_scores), dtype=bool)
        else:
            bm25_mask = np.array(final_mask, dtype=bool)
        
        bm25_scores[~bm25_mask] = -1e9
        sparse_indices = np.argsort(bm25_scores)[-100:][::-1]
        valid_sparse_indices = [idx for idx in sparse_indices if idx < len(syz_ids) and bm25_scores[idx] > -1e8]
        sparse_ranks = {syz_ids[idx]: i+1 for i, idx in enumerate(valid_sparse_indices)}
        
        # 5. 第一阶段：RRF 召回 (Recall Phase)
        # 选出 Top-50 候选
        rrf_scores = {}
        k_const = 60
        for sid in (set(dense_ranks.keys()) | set(sparse_ranks.keys())):
            rrf_scores[sid] = (1.0/(k_const + dense_ranks.get(sid, 999))) + \
                              (1.0/(k_const + sparse_ranks.get(sid, 999)))

        # 按 RRF 得分排序，取 Top-50
        sorted_rrf = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
        top_50_candidates = sorted_rrf[:50]

        # 6. 第二阶段：物理指纹序列校验 (Rerank Phase)
        # 从数据库获取候选详情进行序列比对
        conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
        cursor = conn.cursor()
        # 启用 WAL 模式，支持并发读取
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")  # 30秒超时
        
        try:
            # 提取查询的函数序列和系统调用序列
            # 只使用主要 trace 进行函数提取，确保序列比对的准确性
            query_trace_funcs = self._extract_trace_functions(query_trace, timeout_check=timeout_check, max_items=30)
            query_syscalls = self._extract_syscalls(testcase_text, timeout_check=timeout_check, max_items=30)
            
            # 批量查询所有候选的数据（优化：从 50 次查询改为 1 次查询）
            candidate_ids = [sid for sid, _ in top_50_candidates]
            placeholders = ','.join('?' * len(candidate_ids))
            query = f"SELECT syzbot_id, subject, body, testcase_text, raw_patch, report_trace FROM commits_rag WHERE syzbot_id IN ({placeholders})"
            cursor.execute(query, candidate_ids)
            all_results = cursor.fetchall()
            
            # 将结果转换为字典，方便快速查找
            results_dict = {}
            for res in all_results:
                results_dict[res[0]] = res
            
            reranked_results = []
            
            for i, (sid, rrf_score) in enumerate(top_50_candidates):
                # 检查是否超时
                if timeout_check:
                    timeout_check()
                
                # 从字典中获取结果
                res = results_dict.get(sid)
                
                if not res or len(res[4]) >= 5000:
                    continue
                
                hist_trace = res[5] if res[5] else ""
                hist_testcase = res[3] if res[3] else ""
                
                # A. Calltrace 位置敏感对齐 (Weighted Trace Alignment)
                hist_trace_funcs = self._extract_trace_functions(hist_trace, timeout_check=timeout_check, max_items=30)
                
                # 只比对前30个元素，避免过长序列导致性能问题
                trace_seq_score = self._weighted_levenshtein_distance(
                    query_trace_funcs[:30], 
                    hist_trace_funcs[:30],
                    timeout_check=timeout_check
                )
                
                # B. Testcase 触发基因比对 (Syscall Sequence LCS)
                hist_syscalls = self._extract_syscalls(hist_testcase, timeout_check=timeout_check, max_items=30)
                
                # 只比对前30个元素，避免过长序列导致性能问题
                tc_seq_score = self._lcs_similarity(
                    query_syscalls[:30], 
                    hist_syscalls[:30],
                    timeout_check=timeout_check
                )
                
                # 计算最终精排得分
                # Final_Score = RRF_Score + 0.2 * (0.7 * S_trace_seq + 0.3 * S_tc_seq)
                sequence_score = 0.7 * trace_seq_score + 0.3 * tc_seq_score
                final_score = rrf_score + 0.2 * sequence_score
                
                reranked_results.append({
                    "id": sid,
                    "rrf_score": rrf_score,
                    "trace_seq_score": trace_seq_score,
                    "tc_seq_score": tc_seq_score,
                    "final_score": final_score,
                    "subject": res[1],
                    "body": res[2],
                    "testcase": res[3],
                    "patch": res[4]
                })
        finally:
            conn.close()
        
        # 按最终得分排序
        reranked_results.sort(key=lambda x: x["final_score"], reverse=True)
        
        # 7. 动态 K 值策略
        # 基于最终得分的动态 K 值调整
        dynamic_k = 4
        if reranked_results:
            # 获取最高的最终得分
            top_score = reranked_results[0].get('final_score', 0)
            # 根据最终得分调整返回数量
            if top_score > 0.10:  # 非常相似的案例
                dynamic_k = 1
            elif top_score > 0.07:  # 比较相似的案例
                dynamic_k = 3
        
        # 返回 Top-K 结果
        return reranked_results[:dynamic_k]

    # ---------------- 知识格式化工具 ----------------

    def format_knowledge_as_text(self, cases, timeout_check=None):
        """
        将检索到的生数据转化为结构化的自然语言知识，方便插入 Prompt
        适配新的多模态检索结果格式，包含序列相似度信息
        """
        if not cases:
            return "No relevant historical cases found within the current knowledge base."

        output = f"I have retrieved {len(cases)} relevant historical case(s) for your reference:\n\n"

        for i, case in enumerate(cases):
            # 检查超时
            if timeout_check:
                timeout_check()
            
            output += f"--- [Knowledge Base Item #{i+1}] ---\n"
            output += f"**Case Subject**: {case['subject']} \n"
            
            # 显示相似度得分（如果存在）
            if 'final_score' in case:
                output += f"**Overall Similarity**: {case['final_score']:.4f}\n"
            if 'trace_seq_score' in case:
                output += f"**Stack Trace Similarity**: {case['trace_seq_score']:.4f}\n"
            if 'tc_seq_score' in case:
                output += f"**Syscall Sequence Similarity**: {case['tc_seq_score']:.4f}\n"

            # 提取修复逻辑 (从 Commit Body 提取前 3 行有效描述)
            logic_lines = [l for l in case['body'].splitlines() if l.strip() and "Reported-by" not in l][:3]
            output += f"**Repair Logic**: {' '.join(logic_lines)}\n"

            # 提取触发基因
            if case['testcase']:
                pattern = self._distill_testcase(case['testcase'], timeout_check=timeout_check)
                output += f"**Trigger Pattern**: Involves syscalls `{pattern}`\n"

            output += f"**Reference Implementation (Diff)**:\n```diff\n{case['patch']}\n```\n\n"

        return output

# ================= 运行示例 =================
# if __name__ == "__main__":
#     # 实例化检索器
#     retriever = KernelKnowledgeRetriever()
#
#     # 模拟输入（实际运行时请传入真实的报错文本）
#     sample_report = '''
#     BUG: memory leak
# unreferenced object 0xffff88810d920240 (size 192):
#   comm "kworker/0:2", pid 3658, jiffies 4294943075 (age 13.270s)
#   hex dump (first 32 bytes):
#     01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00  ................
#     00 00 00 00 00 00 00 00 58 02 92 0d 81 88 ff ff  ........X.......
#   backtrace:
#     [<ffffffff82ba0466>] kmalloc include/linux/slab.h:559 [inline]
#     [<ffffffff82ba0466>] usb_alloc_urb+0x66/0xe0 drivers/usb/core/urb.c:74
#     [<ffffffff82ad3888>] atusb_alloc_urbs drivers/net/ieee802154/atusb.c:362 [inline]
#     [<ffffffff82ad3888>] atusb_probe+0x158/0x820 drivers/net/ieee802154/atusb.c:1038
#     [<ffffffff82ba7b77>] usb_probe_interface+0x177/0x370 drivers/usb/core/driver.c:396
#     [<ffffffff825f6019>] really_probe+0x159/0x4a0 drivers/base/dd.c:554
#     [<ffffffff825f63e4>] driver_probe_device+0x84/0x100 drivers/base/dd.c:740
#     [<ffffffff825f6afe>] __device_attach_driver+0xee/0x110 drivers/base/dd.c:846
#     [<ffffffff825f2f97>] bus_for_each_drv+0xb7/0x100 drivers/base/bus.c:431
#     [<ffffffff825f6692>] __device_attach+0x122/0x250 drivers/base/dd.c:914
#     [<ffffffff825f4c36>] bus_probe_device+0xc6/0xe0 drivers/base/bus.c:491
#     [<ffffffff825f1185>] device_add+0x5d5/0xc40 drivers/base/core.c:3242
#     [<ffffffff82ba5119>] usb_set_configuration+0x9d9/0xb90 drivers/usb/core/message.c:2164
#     [<ffffffff82bb557c>] usb_generic_driver_probe+0x8c/0xc0 drivers/usb/core/generic.c:238
#     [<ffffffff82ba72dc>] usb_probe_device+0x5c/0x140 drivers/usb/core/driver.c:293
#     [<ffffffff825f6019>] really_probe+0x159/0x4a0 drivers/base/dd.c:554
#     [<ffffffff825f63e4>] driver_probe_device+0x84/0x100 drivers/base/dd.c:740
#     [<ffffffff825f6afe>] __device_attach_driver+0xee/0x110 drivers/base/dd.c:846
#     '''
#     sample_id = "28a246747e0a465127f3"
#     testcase_text='''
#     # https://syzkaller.appspot.com/bug?id=2482c46517c04822f40780fde741031bf9493450
# # See https://goo.gl/kgGztJ for information about syzkaller reproducers.
# #{"repeat":true,"procs":1,"slowdown":1,"sandbox":"","fault_call":-1,"leak":true,"close_fds":false}
# syz_usb_connect(0x0, 0x24, &(0x7f0000000ac0)=ANY=[@ANYBLOB="12010000c0bb9b40b72040158fef000000010902120001000000000904000000ff"], 0x0)
#     '''
#     # 1. 获取结构化案例
#     matched_cases = retriever.search_knowledge(ref_syz_id=sample_id, report_text=sample_report,testcase_text=testcase_text)
#
#     # 2. 转化为自然语言知识块
#     knowledge_context = retriever.format_knowledge_as_text(matched_cases)
#
#     print("\n" + "🚀 检索到的知识内容如下：" + "\n")
#     print(knowledge_context)

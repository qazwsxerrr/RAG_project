"""src/rag_kb/core/security/aho_corasick.py —— 高性能 Aho-Corasick 多模模式匹配算法树。

特点：
1. 线性时间复杂度 O(N + M)，毫秒级扫描万级敏感词汇；
2. 支持动态挂载外部词库目录 (Konsheng/Sensitive-lexicon 等五万+分类敏感词)；
3. 内置白名单机制与单字防误杀过滤规则 (min_len >= 2)；
4. 广度优先搜索 (BFS) 构建失配指针 (fail pointer) 与输出链表；
5. 支持精准匹配位置定位、关键词打码替换及热重载能力。
"""

import os
import time
import logging
from collections import deque
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set, Union
from rag_kb.core.security.normalizer import normalize_text, clean_confusable_variant

logger = logging.getLogger(__name__)

DEFAULT_SENSITIVE_WORDS = [
    # 商业机密与企业资产
    "商业机密",
    "核心机密",
    "绝密文件",
    "机密资料",
    "内部密件",
    "系统根密码",
    "数据库私钥",
    "RootPassword",
    "API_SECRET_KEY",
    # 越狱与提示词刺探
    "系统提示词",
    "SystemPrompt",
    "忽略所有前置指令",
    "IgnoreAllInstructions",
    "越狱模式",
    "Jailbreak",
]

class TrieNode:
    """Aho-Corasick 字典树节点"""
    def __init__(self):
        self.children: Dict[str, 'TrieNode'] = {}
        self.fail: Optional['TrieNode'] = None
        self.outputs: List[str] = []

class AhoCorasickAutomaton:
    """Aho-Corasick 自动机"""

    def __init__(self, keywords: Optional[List[str]] = None, lexicons_dir: Optional[Union[str, Path]] = None):
        self.root = TrieNode()
        self._is_built = False
        self._max_word_len = 0
        self._words_set: Set[str] = set()
        self._files_loaded: List[str] = []
        self._build_duration_ms: float = 0.0

        if lexicons_dir and Path(lexicons_dir).is_dir():
            self.load_from_directory(lexicons_dir)
        elif keywords:
            for kw in keywords:
                self.add_word(kw)
            self.build()

    @property
    def word_count(self) -> int:
        return len(self._words_set)

    @property
    def stats(self) -> dict:
        return {
            "word_count": len(self._words_set),
            "files_count": len(self._files_loaded),
            "files": self._files_loaded,
            "build_duration_ms": self._build_duration_ms,
            "max_word_len": self._max_word_len
        }

    def add_word(self, word: str):
        """向树中添加单个敏感词 (自动过滤单字与空串)"""
        clean_word = word.strip()
        if not clean_word or len(clean_word) < 2:
            return
        if clean_word in self._words_set:
            return

        node = self.root
        for char in clean_word:
            if char not in node.children:
                node.children[char] = TrieNode()
            node = node.children[char]
        node.outputs.append(clean_word)
        self._words_set.add(clean_word)
        self._max_word_len = max(self._max_word_len, len(clean_word))
        self._is_built = False

    def build(self):
        """构建失配指针 (BFS 广度优先遍历)"""
        t0 = time.perf_counter()
        queue = deque()

        # 第 1 层节点的 fail 均指向 root
        for char, child in self.root.children.items():
            child.fail = self.root
            queue.append(child)

        # 逐层广度搜索计算失配链
        while queue:
            current = queue.popleft()
            for char, child in current.children.items():
                fail_node = current.fail
                while fail_node and char not in fail_node.children:
                    fail_node = fail_node.fail

                child.fail = fail_node.children[char] if fail_node else self.root
                # 合并失配节点的输出
                if child.fail.outputs:
                    child.outputs.extend(child.fail.outputs)
                queue.append(child)

        self._is_built = True
        self._build_duration_ms = (time.perf_counter() - t0) * 1000

    def load_from_directory(self, dir_path: Union[str, Path]) -> int:
        """
        从外部词库目录全量加载敏感词文件 (支持 Konsheng/Sensitive-lexicon 等多分类库)
        - 优先加载 whitelist.txt 作为白名单词库；
        - 自动过滤单字字符以杜绝业务误杀 (min_len >= 2)；
        - 支持 UTF-8 与 GBK 编码容错解析；
        """
        p = Path(dir_path)
        if not p.is_dir():
            logger.warning(f"敏感词目录不存在: {dir_path}")
            return 0

        # 1. 优先提取白名单
        whitelist_file = p / "whitelist.txt"
        whitelist: Set[str] = set()
        if whitelist_file.is_file():
            try:
                for line in whitelist_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                    w = line.strip()
                    if w and not w.startswith("#"):
                        whitelist.add(w)
            except Exception as e:
                logger.warning(f"读取白名单失败: {e}")

        # 2. 遍历加载所有词库
        files = sorted(list(p.glob("*.txt")))
        self.root = TrieNode()
        self._words_set.clear()
        self._files_loaded.clear()
        self._max_word_len = 0

        total_loaded = 0
        for f in files:
            if f.name == "whitelist.txt":
                continue
            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                content = f.read_text(encoding="gbk", errors="ignore")

            for line in content.splitlines():
                word = line.strip()
                if not word or word.startswith("#"):
                    continue
                # 过滤单字 (必须 >= 2 字符) 并检查白名单
                if len(word) >= 2 and word not in whitelist:
                    self.add_word(word)
                    total_loaded += 1

            self._files_loaded.append(f.name)

        # 3. 构建自动机
        self.build()
        logger.info(
            f"成功构建 Aho-Corasick 敏感词自动机: 已加载 {len(self._files_loaded)} 个分类词库，"
            f"共收录 {len(self._words_set)} 条独立敏感词，构建耗时: {self._build_duration_ms:.2f}ms"
        )
        return len(self._words_set)

    def search(self, text: str) -> List[Tuple[int, int, str]]:
        """
        在文本中检索全部敏感词
        返回: [(start_idx, end_idx, pattern), ...]
        """
        if not self._is_built:
            self.build()

        matches: List[Tuple[int, int, str]] = []
        node = self.root

        for idx, char in enumerate(text):
            while node and char not in node.children:
                node = node.fail
            if not node:
                node = self.root
                continue
            node = node.children[char]
            for pattern in node.outputs:
                start = idx - len(pattern) + 1
                matches.append((start, idx + 1, pattern))

        return matches

    def contains_any(self, text: str) -> bool:
        """
        快速判断文本是否包含任何敏感词
        支持零宽字符穿透防御与标点符号插空混淆对抗 (如 色*情, 色 情, ｓｅｑｉｎｇ)
        """
        if not text:
            return False
        # 1. 原始文本直接匹配
        if len(self.search(text)) > 0:
            return True
        # 2. 消除零宽字符与全角半角标准化匹配
        norm = normalize_text(text)
        if norm != text and len(self.search(norm)) > 0:
            return True
        # 3. 紧凑语义串匹配 (消除中英文干扰标点与空格)
        compact = clean_confusable_variant(text)
        if compact != text and len(self.search(compact)) > 0:
            return True
        return False

    def replace(self, text: str, mask: str = "***") -> str:
        """将命中的敏感词替换为掩码字符 (支持重叠区间智能合并)"""
        matches = self.search(text)
        if not matches:
            return text

        # 合并有重叠的区间
        intervals: List[Tuple[int, int]] = sorted([(m[0], m[1]) for m in matches], key=lambda x: x[0])
        merged: List[Tuple[int, int]] = []
        for start, end in intervals:
            if not merged:
                merged.append((start, end))
            else:
                last_start, last_end = merged[-1]
                if start <= last_end:
                    merged[-1] = (last_start, max(last_end, end))
                else:
                    merged.append((start, end))

        # 构建替换结果
        res = []
        last_idx = 0
        for start, end in merged:
            res.append(text[last_idx:start])
            res.append(mask)
            last_idx = end
        res.append(text[last_idx:])
        return "".join(res)

def _find_lexicons_dir() -> Optional[Path]:
    """智能查找敏感词库目录"""
    candidates = [
        Path("backend/data/sensitive_lexicon"),
        Path("data/sensitive_lexicon"),
        Path(__file__).resolve().parent.parent.parent.parent / "data" / "sensitive_lexicon",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return None

def init_default_automaton() -> AhoCorasickAutomaton:
    """初始化全局单例自动机"""
    lexicon_dir = _find_lexicons_dir()
    if lexicon_dir:
        ac = AhoCorasickAutomaton(lexicons_dir=lexicon_dir)
        if ac.word_count > 0:
            return ac

    # 兜底保底词库
    return AhoCorasickAutomaton(DEFAULT_SENSITIVE_WORDS)

# 全局单例自动机实例
default_ac_automaton = init_default_automaton()

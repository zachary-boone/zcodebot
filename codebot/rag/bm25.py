"""BM25 关键词召回（第二期 RAG 混合检索的关键词这一路）。

为什么混合检索需要 BM25（详见 docs/rag-optimization-plan.md §6.3 决策二）：
  - 纯向量检索会漏掉精确关键词（如变量名 user_id、函数名 verify_token）
  - 纯关键词漏语义（用户说"登录"，代码叫 verify_token）
  - 两路召回 + RRF 融合，取长补短

用纯 Python 实现的轻量 BM25（rank-bm25 风格），不引入 Elasticsearch 重依赖。
只存内存——代码块通常几千到几万个，内存 BM25 完全够用。
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# BM25 参数（业界标准默认值）
K1 = 1.5
B = 0.75

# 简单分词：按非字母数字分割，转小写
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """简单分词：提取字母数字下划线 token，转小写。

    代码搜索不需要复杂分词（中文分词、词干化），按符号切就够——
    变量名 user_id、函数名 verifyToken 都能正确切出。
    """
    return [t.lower() for t in _TOKEN_RE.findall(text)]


@dataclass
class BM25Document:
    """BM25 文档。"""

    doc_id: str           # 和 CodeChunk.id 一致
    tokens: list[str]
    token_freqs: dict[str, int] = field(default_factory=dict)
    length: int = 0


class BM25Index:
    """内存 BM25 索引。

    用法：
        index = BM25Index()
        index.add_docs([(doc_id, text), ...])
        results = index.search(query, top_k=20)  # [(doc_id, score), ...]
    """

    def __init__(self, k1: float = K1, b: float = B) -> None:
        self._k1 = k1
        self._b = b
        self._docs: dict[str, BM25Document] = {}
        self._doc_freqs: dict[str, int] = {}  # token -> 出现该 token 的文档数
        self._avg_length: float = 0.0
        self._corpus_size: int = 0

    def add_docs(self, docs: list[tuple[str, str]]) -> None:
        """批量添加文档。doc_id 重复会覆盖。"""
        for doc_id, text in docs:
            tokens = tokenize(text)
            freqs: dict[str, int] = {}
            for t in tokens:
                freqs[t] = freqs.get(t, 0) + 1

            # 如果是覆盖旧文档，先回退 doc_freqs
            if doc_id in self._docs:
                old = self._docs[doc_id]
                for tok in old.token_freqs:
                    self._doc_freqs[tok] = max(0, self._doc_freqs.get(tok, 0) - 1)

            self._docs[doc_id] = BM25Document(
                doc_id=doc_id, tokens=tokens,
                token_freqs=freqs, length=len(tokens),
            )
            for tok in freqs:
                self._doc_freqs[tok] = self._doc_freqs.get(tok, 0) + 1

        self._corpus_size = len(self._docs)
        total_length = sum(d.length for d in self._docs.values())
        self._avg_length = total_length / self._corpus_size if self._corpus_size else 0.0

    def search(self, query: str, top_k: int = 20) -> list[tuple[str, float]]:
        """检索：返回 [(doc_id, score)] 按分数降序，最多 top_k 个。"""
        if not self._docs or not query.strip():
            return []

        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        scored: list[tuple[str, float]] = []
        for doc_id, doc in self._docs.items():
            score = self._score(doc, query_tokens)
            if score > 0:
                scored.append((doc_id, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def _score(self, doc: BM25Document, query_tokens: list[str]) -> float:
        """BM25 打分。

        score = sum over query tokens t:
            IDF(t) * (f(t,d) * (k1+1)) / (f(t,d) + k1*(1 - b + b*|d|/avgdl))

        IDF(t) = ln((N - df(t) + 0.5) / (df(t) + 0.5) + 1)
        """
        if self._avg_length <= 0:
            return 0.0

        score = 0.0
        for t in query_tokens:
            if t not in doc.token_freqs:
                continue
            f = doc.token_freqs[t]
            df = self._doc_freqs.get(t, 0)
            # BM25 IDF（+1 平滑保证非负）
            idf = math.log((self._corpus_size - df + 0.5) / (df + 0.5) + 1)
            # BM25 TF
            tf = (f * (self._k1 + 1)) / (
                f + self._k1 * (1 - self._b + self._b * doc.length / self._avg_length)
            )
            score += idf * tf
        return score

    def is_available(self) -> bool:
        """是否有文档。"""
        return len(self._docs) > 0

    @property
    def size(self) -> int:
        return self._corpus_size

"""BM25 关键词召回（第二期 RAG 混合检索的关键词这一路）。

为什么混合检索需要 BM25（详见 docs/RAG优化方案.md §6.3 决策二）：
  - 纯向量检索会漏掉精确关键词（如变量名 user_id、函数名 verify_token）
  - 纯关键词漏语义（用户说"登录"，代码叫 verify_token）
  - 两路召回 + RRF 融合，取长补短

用纯 Python 实现的轻量 BM25（rank-bm25 风格），不引入 Elasticsearch 重依赖。
索引在内存中维护倒排表，查询只对命中查询词的文档打分。
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
        # token -> 包含该 token 的文档 ID。查询时通过它得到候选集，
        # 避免每次遍历整个语料库。
        self._postings: dict[str, set[str]] = {}
        self._doc_order: dict[str, int] = {}
        self._next_order = 0
        self._avg_length: float = 0.0
        self._corpus_size: int = 0

    def add_docs(self, docs: list[tuple[str, str]]) -> None:
        """批量添加文档。doc_id 重复会覆盖。"""
        for doc_id, text in docs:
            tokens = tokenize(text)
            freqs: dict[str, int] = {}
            for t in tokens:
                freqs[t] = freqs.get(t, 0) + 1

            # 如果是覆盖旧文档，先从倒排表移除旧版本。
            self._remove_doc(doc_id)

            self._docs[doc_id] = BM25Document(
                doc_id=doc_id,
                token_freqs=freqs, length=len(tokens),
            )
            for tok in freqs:
                self._postings.setdefault(tok, set()).add(doc_id)
            self._doc_order[doc_id] = self._next_order
            self._next_order += 1

        self._corpus_size = len(self._docs)
        total_length = sum(d.length for d in self._docs.values())
        self._avg_length = total_length / self._corpus_size if self._corpus_size else 0.0

    def delete_docs(self, doc_ids: list[str] | set[str]) -> None:
        """删除文档及其倒排项；不存在的 ID 会被忽略。"""
        for doc_id in doc_ids:
            self._remove_doc(doc_id)

        self._corpus_size = len(self._docs)
        total_length = sum(d.length for d in self._docs.values())
        self._avg_length = total_length / self._corpus_size if self._corpus_size else 0.0

    def _remove_doc(self, doc_id: str) -> None:
        """移除单个文档；调用方负责在批量操作后更新统计量。"""
        doc = self._docs.pop(doc_id, None)
        if doc is None:
            return
        for token in doc.token_freqs:
            posting = self._postings.get(token)
            if posting is None:
                continue
            posting.discard(doc_id)
            if not posting:
                del self._postings[token]
        self._doc_order.pop(doc_id, None)

    def search(self, query: str, top_k: int = 20) -> list[tuple[str, float]]:
        """检索：返回 [(doc_id, score)] 按分数降序，最多 top_k 个。"""
        if not self._docs or not query.strip():
            return []

        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        # set 去重后只保留至少命中一个查询词的文档。
        candidate_ids: set[str] = set()
        for token in set(query_tokens):
            candidate_ids.update(self._postings.get(token, ()))

        scored: list[tuple[str, float]] = []
        for doc_id in candidate_ids:
            doc = self._docs[doc_id]
            score = self._score(doc, query_tokens)
            if score > 0:
                scored.append((doc_id, score))

        # 用插入顺序打破同分，避免 set 遍历造成结果抖动。
        scored.sort(key=lambda x: (-x[1], self._doc_order[x[0]]))
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
            df = len(self._postings.get(t, ()))
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

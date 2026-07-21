"""记忆语义检索（第一期）。

用 embedding 余弦相似度替代 recall.py 里原本的 LLM 选择器。
记忆正文做向量化，user query 做向量化，取 Top-K 最相似的记忆。

相比 LLM 选择器的优势：
  1. 省一次 LLM 调用（延迟 + 成本）
  2. 基于记忆正文语义，不只看 frontmatter description
  3. 可解释——有具体相似度分数，可设阈值过滤

降级策略（与项目"渐进式降级"哲学一致）：
  - EmbeddingProvider 不可用（NullEmbedding）→ 返回空，上层回退到 LLM 选择器
  - embedding 调用失败 → 返回空，上层回退
  - 记忆文件读取失败 → 跳过该条，不阻塞

增量索引：
  - 按 mtime_ms 判断记忆是否变化，变了才重新 embedding
  - 避免每次对话都全量向量化所有记忆
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from codebot.memory.recall import MemoryHeader
from codebot.rag.embedding import EmbeddingProvider, EmbeddingError, cosine_similarity

log = logging.getLogger(__name__)


# 相似度阈值：低于此值认为不相关，过滤掉。
# 0.3 是经验值——代码/技术语义下，真正相关的记忆通常在 0.4+，
# 0.3 给一点余量避免漏召回，又不会把明显不相关的塞进来。
DEFAULT_SIMILARITY_THRESHOLD = 0.3

# 单次最多返回的记忆数（与原 LLM 选择器的 5 一致）
DEFAULT_TOP_K = 5

# 单条记忆正文的截断长度，避免超长记忆撑爆 embedding 输入
MAX_MEMORY_CHARS = 4000


@dataclass
class MemoryVector:
    """一条记忆的向量缓存。"""

    file_path: str
    mtime_ms: int
    content_hash: str
    vector: list[float] = field(default_factory=list)


class SemanticMemoryIndex:
    """记忆语义索引：build() 建索引，search() 检索。

    索引存在内存里（记忆文件通常几十到几百个，不需要持久化向量库）。
    每次 find_relevant_memories 调用前 ensure() 一下，增量更新变化的条目。
    """

    def __init__(
        self,
        embedder: EmbeddingProvider,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        top_k: int = DEFAULT_TOP_K,
    ) -> None:
        self._embedder = embedder
        self._threshold = similarity_threshold
        self._top_k = top_k
        # file_path -> MemoryVector
        self._index: dict[str, MemoryVector] = {}
        # 已索引过的记忆（用于检测删除）
        self._known_paths: set[str] = set()

    def is_available(self) -> bool:
        """embedding 是否可用。上层据此决定是否走语义检索。"""
        return self._embedder.is_available()

    async def ensure(self, headers: list[MemoryHeader]) -> None:
        """增量更新索引：新记忆 embedding，变化的重新 embedding，删除的移除。

        用 mtime + content_hash 两级判断（和 CodeSearch 的增量索引同思路）：
          - mtime 没变 → 肯定没改，跳过
          - mtime 变了 → 读正文算 hash 确认，避免 touch 误报
        """
        if not self.is_available():
            return

        current_paths = {h.file_path for h in headers}

        # 1. 移除已删除的记忆
        for path in list(self._known_paths):
            if path not in current_paths:
                self._index.pop(path, None)
                self._known_paths.discard(path)

        # 2. 增量 embedding 新增/变化的记忆
        to_embed: list[tuple[str, str]] = []  # (file_path, text)
        pending: list[MemoryVector] = []
        for h in headers:
            existing = self._index.get(h.file_path)
            if existing and existing.mtime_ms == h.mtime_ms:
                continue  # mtime 没变，跳过

            text = _read_memory_body(h.file_path)
            if not text.strip():
                continue
            content_hash = _hash_text(text)

            # mtime 变了但内容没变（如 touch），只更新 mtime
            if existing and existing.content_hash == content_hash:
                existing.mtime_ms = h.mtime_ms
                continue

            to_embed.append((h.file_path, text))
            pending.append(
                MemoryVector(
                    file_path=h.file_path,
                    mtime_ms=h.mtime_ms,
                    content_hash=content_hash,
                )
            )

        if not to_embed:
            return

        try:
            vectors = await self._embedder.embed([t for _, t in to_embed])
        except EmbeddingError as e:
            log.warning("记忆 embedding 失败，索引保持现状: %s", e)
            return

        for mv, vec in zip(pending, vectors):
            mv.vector = vec
            self._index[mv.file_path] = mv
            self._known_paths.add(mv.file_path)

    async def search(self, query: str) -> list[str]:
        """语义检索：返回最相关的 top_k 个记忆 file_path。

        embedding 不可用或失败时返回空列表，上层应回退到 LLM 选择器。
        """
        if not self.is_available() or not self._index:
            return []

        try:
            q_vec = await self._embedder.embed_one(query)
        except EmbeddingError as e:
            log.warning("query embedding 失败: %s", e)
            return []

        scored: list[tuple[float, str]] = []
        for path, mv in self._index.items():
            score = cosine_similarity(q_vec, mv.vector)
            if score >= self._threshold:
                scored.append((score, path))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [path for _, path in scored[: self._top_k]]


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _read_memory_body(file_path: str) -> str:
    """读取记忆正文（去掉 frontmatter），并截断超长内容。"""
    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # 去掉 frontmatter（--- ... --- 包裹的部分）
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            content = content[end + 4 :].lstrip()
    return content[:MAX_MEMORY_CHARS]


def _hash_text(text: str) -> str:
    """对正文算 hash，用于增量索引判断内容是否真变了。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 语义选择器：作为 recall.py 里 SelectorFn 的替代实现
# ---------------------------------------------------------------------------


async def semantic_selector(
    index: SemanticMemoryIndex,
    query: str,
    candidates: list[MemoryHeader],
) -> list[str]:
    """语义选择器：返回相关记忆的 file_path 列表。

    与 recall.py 的 LLM 选择器接口对齐——都是"给 query + 候选，返回选中的"。
    上层用法：
        if semantic_index.is_available():
            paths = await semantic_selector(semantic_index, query, candidates)
        else:
            paths = await llm_selector(...)  # 回退

    注意：candidates 参数这里用于确保索引和当前候选集一致（ensure），
    实际检索结果由 index.search 给出。
    """
    await index.ensure(candidates)
    return await index.search(query)

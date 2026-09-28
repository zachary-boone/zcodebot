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

# 单次 ensure() 最多新索引多少条。
# 冷启动时若一次性 embed 全部记忆，会直接撑爆调用方的超时（app.py 给这次召回
# 只留了 8 秒，实测 200 条需 ~2.8s，上千条必然超时返回空）。改为每次最多 40 条、
# 按 mtime 从新到旧优先——最新的记忆立刻可用，其余在后续几轮里补齐。
MAX_EMBED_PER_ENSURE = 40

# embedding 失败后的冷却秒数。
# ensure() 是在「每条用户消息」的召回路径上的：失败后若不让 is_available() 变 False，
# 每条消息都会先白跑一次注定失败的请求（含读取全部记忆正文），再回退 LLM 选择器。
EMBED_FAILURE_COOLDOWN = 120.0


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
        cooldown_seconds: float = EMBED_FAILURE_COOLDOWN,
        max_embed_per_ensure: int = MAX_EMBED_PER_ENSURE,
    ) -> None:
        self._embedder = embedder
        self._threshold = similarity_threshold
        self._top_k = top_k
        self._cooldown_seconds = cooldown_seconds
        self._max_embed_per_ensure = max_embed_per_ensure
        # file_path -> MemoryVector
        self._index: dict[str, MemoryVector] = {}
        # 已索引过的记忆（用于检测删除）
        self._known_paths: set[str] = set()
        # embedding 失败后的降级截止时间（monotonic）。0 表示当前健康。
        self._degraded_until: float = 0.0

    def is_available(self) -> bool:
        """embedding 是否可用。上层据此决定是否走语义检索。

        冷却期内返回 False，让上层直接走 LLM 选择器，不再白跑注定失败的请求。
        """
        if time.monotonic() < self._degraded_until:
            return False
        return self._embedder.is_available()

    async def ensure(self, headers: list[MemoryHeader]) -> None:
        """增量更新索引：新记忆 embedding，变化的重新 embedding，删除的移除。

        用 mtime + content_hash 两级判断（和 CodeSearch 的增量索引同思路）：
          - mtime 没变 → 肯定没改，跳过
          - mtime 变了 → 读正文算 hash 确认，避免 touch 误报

        单次最多新索引 max_embed_per_ensure 条，按 mtime 从新到旧优先，
        避免冷启动一次性 embed 全部记忆撑爆调用方超时。
        """
        if not self.is_available():
            return

        current_paths = {h.file_path for h in headers}

        # 1. 移除已删除的记忆
        for path in list(self._known_paths):
            if path not in current_paths:
                self._index.pop(path, None)
                self._known_paths.discard(path)

        # 2. 挑出需要（重新）embedding 的记忆
        #    todo 元素： (mtime_ms, file_path, text, content_hash)
        todo: list[tuple[int, str, str, str]] = []
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

            todo.append((h.mtime_ms, h.file_path, text, content_hash))

        if not todo:
            return

        # 按 mtime 从新到旧优先，单次只处理前 max_embed_per_ensure 条
        todo.sort(key=lambda x: x[0], reverse=True)
        todo = todo[: self._max_embed_per_ensure]

        try:
            vectors = await self._embedder.embed([t[2] for t in todo])
        except EmbeddingError as e:
            was_healthy = self._degraded_until <= 0.0
            self._degraded_until = time.monotonic() + self._cooldown_seconds
            if was_healthy:  # 只在「进入冷却」时打一条日志，避免刷屏
                log.warning(
                    "记忆 embedding 失败，进入 %.0f 秒冷却: %s",
                    self._cooldown_seconds, e,
                )
            return

        # 成功：解除冷却
        self._degraded_until = 0.0

        for (mtime_ms, file_path, _text, content_hash), vec in zip(todo, vectors):
            self._index[file_path] = MemoryVector(
                file_path=file_path,
                mtime_ms=mtime_ms,
                content_hash=content_hash,
                vector=vec,
            )
            self._known_paths.add(file_path)

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

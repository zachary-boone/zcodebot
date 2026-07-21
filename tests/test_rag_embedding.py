"""第一期 RAG 测试：embedding 工具函数 + 记忆语义检索。

用 mock embedder 避免真实 API 调用，验证：
  - cosine_similarity 的正确性（正交、同向、反向）
  - SemanticMemoryIndex 的增量索引（mtime 未变跳过、变化重 embed）
  - search 的 Top-K + 阈值过滤
  - NullEmbedding 的降级行为
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from codebot.rag.embedding import (
    EmbeddingError,
    NullEmbedding,
    cosine_similarity,
)
from codebot.memory.recall import MemoryHeader
from codebot.memory.semantic_recall import (
    SemanticMemoryIndex,
    _read_memory_body,
    DEFAULT_SIMILARITY_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Mock embedder：可控的假 embedding，用于测试
# ---------------------------------------------------------------------------


class MockEmbedder:
    """把文本映射成可控向量的假 embedder。

    用文本的 hash 决定向量方向，让"语义相近"的文本向量也相近（同 prefix）。
    测试里直接注入预设向量更精确。
    """

    def __init__(self, preset: dict[str, list[float]] | None = None) -> None:
        self._preset = preset or {}
        self._calls = 0

    def is_available(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self._calls += 1
        result = []
        for t in texts:
            if t in self._preset:
                result.append(self._preset[t])
            else:
                # 未知文本给一个和 query 正交的向量，确保不相关
                result.append([0.0, 0.0, 0.0, 0.0])
        return result

    async def embed_one(self, text: str) -> list[float]:
        vecs = await self.embed([text])
        return vecs[0]


class FailingEmbedder:
    """永远失败的 embedder，测试降级。"""

    def is_available(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError("mock failure")


# ---------------------------------------------------------------------------
# cosine_similarity 测试
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors(self):
        a = [1.0, 2.0, 3.0]
        assert cosine_similarity(a, a) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_empty_vectors(self):
        assert cosine_similarity([], []) == 0.0

    def test_different_length(self):
        assert cosine_similarity([1.0, 2.0], [1.0]) == 0.0

    def test_zero_vector(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


# ---------------------------------------------------------------------------
# NullEmbedding 测试
# ---------------------------------------------------------------------------


class TestNullEmbedding:
    def test_not_available(self):
        null = NullEmbedding("test reason")
        assert null.is_available() is False

    @pytest.mark.asyncio
    async def test_embed_raises(self):
        null = NullEmbedding("test reason")
        with pytest.raises(EmbeddingError):
            await null.embed(["text"])


# ---------------------------------------------------------------------------
# SemanticMemoryIndex 测试
# ---------------------------------------------------------------------------


def make_header(file_path: str, mtime_ms: int, description: str = "") -> MemoryHeader:
    return MemoryHeader(
        filename=Path(file_path).name,
        file_path=file_path,
        scope="project",
        mtime_ms=mtime_ms,
        description=description,
        type="project",
    )


def write_memory(path: Path, body: str, frontmatter: bool = True) -> None:
    """写一个带 frontmatter 的记忆文件。"""
    if frontmatter:
        content = f"---\nname: test\ndescription: test memory\n---\n{body}"
    else:
        content = body
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestSemanticMemoryIndex:
    @pytest.mark.asyncio
    async def test_search_returns_relevant_memories(self, tmp_path):
        """语义检索：query 向量和某条记忆向量相近时，应该命中。"""
        mem1 = tmp_path / "auth.md"
        mem2 = tmp_path / "db.md"
        write_memory(mem1, "用户偏好简洁代码风格")
        write_memory(mem2, "项目用 PostgreSQL 数据库")

        # 预设向量：query 和 mem1 相近（同方向），和 mem2 正交
        query_vec = [1.0, 0.0, 0.0, 0.0]
        mem1_vec = [0.9, 0.1, 0.0, 0.0]  # 和 query 相似度 ~0.99
        mem2_vec = [0.0, 1.0, 0.0, 0.0]  # 和 query 相似度 0

        embedder = MockEmbedder({
            "用户偏好简洁代码风格": mem1_vec,
            "项目用 PostgreSQL 数据库": mem2_vec,
            "用户喜欢什么样的代码": query_vec,
        })
        index = SemanticMemoryIndex(embedder)

        headers = [
            make_header(str(mem1), 1000),
            make_header(str(mem2), 1000),
        ]
        await index.ensure(headers)
        results = await index.search("用户喜欢什么样的代码")

        assert str(mem1) in results
        assert str(mem2) not in results  # 相似度 0 < 阈值，被过滤

    @pytest.mark.asyncio
    async def test_incremental_index_skips_unchanged(self, tmp_path):
        """mtime 未变的记忆不应重新 embedding。"""
        mem = tmp_path / "mem.md"
        write_memory(mem, "内容不变的记忆")

        embedder = MockEmbedder({"内容不变的记忆": [1.0, 0.0]})
        index = SemanticMemoryIndex(embedder)

        headers = [make_header(str(mem), 1000)]
        await index.ensure(headers)
        first_calls = embedder._calls

        # 第二次 ensure，mtime 没变
        await index.ensure(headers)
        assert embedder._calls == first_calls  # 没有新的 embed 调用

    @pytest.mark.asyncio
    async def test_incremental_index_reembeds_on_mtime_change(self, tmp_path):
        """mtime 变了且内容也变了 → 重新 embedding。"""
        mem = tmp_path / "mem.md"
        write_memory(mem, "旧内容")
        embedder = MockEmbedder({
            "旧内容": [1.0, 0.0],
            "新内容": [0.0, 1.0],
        })
        index = SemanticMemoryIndex(embedder)

        await index.ensure([make_header(str(mem), 1000)])
        first_calls = embedder._calls

        # 内容改了，mtime 也变了
        write_memory(mem, "新内容")
        await index.ensure([make_header(str(mem), 2000)])
        assert embedder._calls > first_calls  # 有新的 embed 调用

    @pytest.mark.asyncio
    async def test_search_returns_empty_when_unavailable(self):
        """NullEmbedding 不可用时，search 返回空。"""
        index = SemanticMemoryIndex(NullEmbedding())
        assert index.is_available() is False
        results = await index.search("anything")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_returns_empty_on_embedding_failure(self, tmp_path):
        """embedding 调用失败时，search 静默返回空。"""
        mem = tmp_path / "mem.md"
        write_memory(mem, "内容")
        index = SemanticMemoryIndex(FailingEmbedder())
        # ensure 失败时不抛异常，index 保持空
        await index.ensure([make_header(str(mem), 1000)])
        results = await index.search("query")
        assert results == []

    @pytest.mark.asyncio
    async def test_threshold_filters_low_similarity(self, tmp_path):
        """相似度低于阈值的记忆被过滤。"""
        mem = tmp_path / "mem.md"
        write_memory(mem, "内容")
        # query 和 mem 相似度 0.2 < 默认阈值 0.3
        embedder = MockEmbedder({
            "内容": [1.0, 0.0],
            "query": [0.2, 0.98],  # 相似度 ≈ 0.2
        })
        index = SemanticMemoryIndex(embedder)
        await index.ensure([make_header(str(mem), 1000)])
        results = await index.search("query")
        assert results == []  # 被阈值过滤


# ---------------------------------------------------------------------------
# 辅助函数测试
# ---------------------------------------------------------------------------


class TestReadMemoryBody:
    def test_strips_frontmatter(self, tmp_path):
        mem = tmp_path / "mem.md"
        mem.write_text(
            "---\nname: test\ndescription: x\n---\n正文内容", encoding="utf-8"
        )
        body = _read_memory_body(str(mem))
        assert body == "正文内容"

    def test_no_frontmatter(self, tmp_path):
        mem = tmp_path / "mem.md"
        mem.write_text("纯正文", encoding="utf-8")
        body = _read_memory_body(str(mem))
        assert body == "纯正文"

    def test_truncates_long_content(self, tmp_path):
        mem = tmp_path / "mem.md"
        long_body = "x" * 10000
        mem.write_text(long_body, encoding="utf-8")
        body = _read_memory_body(str(mem))
        assert len(body) <= 4000

    def test_missing_file_returns_empty(self):
        assert _read_memory_body("/nonexistent/path.md") == ""

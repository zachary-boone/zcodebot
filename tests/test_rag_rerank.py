"""第三期 RAG 测试：轻量重排 + ToolSearch 语义匹配。

用 mock embedder 验证：
  - embed_rerank：按余弦相似度重排，返回 top_k
  - search_deferred_semantic：语义匹配优于关键词，失败回退
"""

from __future__ import annotations

import pytest

from codebot.rag.embedding import EmbeddingError, NullEmbedding
from codebot.rag.reranker import embed_rerank
from codebot.tools import ToolRegistry, create_default_registry
from codebot.tools.impl.tool_search import ToolSearchTool
from codebot.tools.base import Tool, ToolResult
from pydantic import BaseModel


class MockEmbedder:
    """预设向量的 mock embedder。"""

    def __init__(self, preset: dict[str, list[float]] | None = None) -> None:
        self._preset = preset or {}

    def is_available(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._preset.get(t, [0.0, 0.0, 0.0, 0.0]) for t in texts]

    async def embed_one(self, text: str) -> list[float]:
        return self._preset.get(text, [0.0, 0.0, 0.0, 0.0])


# ---------------------------------------------------------------------------
# 重排测试
# ---------------------------------------------------------------------------


class TestEmbedRerank:
    @pytest.mark.asyncio
    async def test_rerank_by_similarity(self):
        """重排后最相似的候选排第一。"""
        embedder = MockEmbedder({
            "登录鉴权": [1.0, 0.0, 0.0, 0.0],
            "verify_token 函数": [0.95, 0.05, 0.0, 0.0],  # 和 query 很像
            "数据库连接": [0.0, 1.0, 0.0, 0.0],           # 不相关
        })
        candidates = [
            {"code": "数据库连接", "name": "db"},
            {"code": "verify_token 函数", "name": "auth"},
        ]
        reranked = await embed_rerank(
            "登录鉴权", candidates, embedder, content_key="code", top_k=2
        )
        assert reranked[0]["name"] == "auth"  # 更相似的排前
        assert "rerank_score" in reranked[0]

    @pytest.mark.asyncio
    async def test_rerank_respects_top_k(self):
        embedder = MockEmbedder({"q": [1.0, 0.0]})
        candidates = [
            {"code": "a", "name": "a"},
            {"code": "b", "name": "b"},
            {"code": "c", "name": "c"},
        ]
        reranked = await embed_rerank("q", candidates, embedder, top_k=2)
        assert len(reranked) == 2

    @pytest.mark.asyncio
    async def test_rerank_returns_original_on_failure(self):
        """embedding 失败时原样返回（不阻塞）。"""

        class FailingEmbedder:
            def is_available(self) -> bool:
                return True

            async def embed(self, texts):
                raise EmbeddingError("fail")

        candidates = [{"code": "a", "name": "a"}, {"code": "b", "name": "b"}]
        reranked = await embed_rerank("q", candidates, FailingEmbedder(), top_k=5)
        assert len(reranked) == 2  # 原样返回

    @pytest.mark.asyncio
    async def test_rerank_empty_candidates(self):
        embedder = MockEmbedder()
        assert await embed_rerank("q", [], embedder) == []

    @pytest.mark.asyncio
    async def test_rerank_null_embedder_returns_original(self):
        """NullEmbedder 不可用时返回原序前 top_k。"""
        candidates = [{"code": "a"}, {"code": "b"}, {"code": "c"}]
        reranked = await embed_rerank("q", candidates, NullEmbedding(), top_k=2)
        assert len(reranked) == 2


# ---------------------------------------------------------------------------
# ToolSearch 语义匹配测试
# ---------------------------------------------------------------------------


class DummyParams(BaseModel):
    q: str = ""


class DummyDeferredTool(Tool):
    """用于测试的延迟工具。"""
    name = "SemanticCodeSearch"
    description = "build and query codebase embeddings for semantic search"
    params_model = DummyParams
    category = "read"
    should_defer = True

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(output="ok")


class TestToolSearchSemantic:
    @pytest.mark.asyncio
    async def test_semantic_match_finds_relevant(self):
        """语义匹配：query 语义相近的工具能被找到，即使无字面重叠。"""
        registry = ToolRegistry()
        registry.register(DummyDeferredTool())

        # query "index codebase" 和工具描述语义相近
        embedder = MockEmbedder({
            "index codebase": [1.0, 0.0, 0.0, 0.0],
            "SemanticCodeSearch: build and query codebase embeddings for semantic search": [0.9, 0.1, 0.0, 0.0],
        })

        results = await registry.search_deferred_semantic(
            "index codebase", max_results=5, protocol="anthropic", embedder=embedder
        )
        assert len(results) == 1
        assert results[0]["name"] == "SemanticCodeSearch"

    @pytest.mark.asyncio
    async def test_semantic_falls_back_on_null_embedder(self):
        """embedder 不可用时回退到关键词匹配。"""
        registry = ToolRegistry()
        registry.register(DummyDeferredTool())

        results = await registry.search_deferred_semantic(
            # 关键词能命中 description 里的 "codebase"
            "codebase", max_results=5, protocol="anthropic",
            embedder=NullEmbedding(),
        )
        assert len(results) == 1
        assert results[0]["name"] == "SemanticCodeSearch"

    @pytest.mark.asyncio
    async def test_semantic_falls_back_on_embedding_error(self):
        """embedding 调用失败时回退关键词。"""
        registry = ToolRegistry()
        registry.register(DummyDeferredTool())

        class FailingEmbedder:
            def is_available(self) -> bool:
                return True

            async def embed(self, texts):
                raise EmbeddingError("fail")

        results = await registry.search_deferred_semantic(
            "codebase", max_results=5, protocol="anthropic",
            embedder=FailingEmbedder(),
        )
        assert len(results) == 1  # 回退关键词后命中

    @pytest.mark.asyncio
    async def test_toolsearch_tool_uses_embedder_when_provided(self):
        """ToolSearchTool 注入 embedder 后走语义路径。"""
        registry = ToolRegistry()
        registry.register(DummyDeferredTool())

        embedder = MockEmbedder({
            "query": [1.0, 0.0],
            "SemanticCodeSearch: build and query codebase embeddings for semantic search": [0.9, 0.1],
        })
        tool = ToolSearchTool(registry, protocol="anthropic", embedder=embedder)

        from codebot.tools.impl.tool_search import ToolSearchParams
        result = await tool.execute(ToolSearchParams(query="query", max_results=5))
        assert "SemanticCodeSearch" in result.output

    @pytest.mark.asyncio
    async def test_toolsearch_tool_fallback_without_embedder(self):
        """ToolSearchTool 没注入 embedder 时走关键词路径（原行为）。"""
        registry = ToolRegistry()
        registry.register(DummyDeferredTool())
        tool = ToolSearchTool(registry, protocol="anthropic")  # 无 embedder

        from codebot.tools.impl.tool_search import ToolSearchParams
        result = await tool.execute(ToolSearchParams(query="codebase", max_results=5))
        assert "SemanticCodeSearch" in result.output

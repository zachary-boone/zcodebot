"""第二期 RAG 测试：代码分块 + BM25 + RRF 融合 + CodeSearch 降级。

不依赖真实 Qdrant / embedding API，用 mock 和纯算法测试验证：
  - chunker：Python AST 分块、滑窗兜底、超长函数二次切分
  - BM25：打分正确性、IDF/TF 行为
  - RRF：融合排序、重复文档去重
  - CodeSearch：降级行为（依赖未装时返回提示）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebot.rag.chunker import (
    CodeChunk,
    chunk_file,
    walk_indexable_files,
    MAX_CHUNK_LINES,
)
from codebot.rag.bm25 import BM25Index, tokenize
from codebot.rag.fusion import reciprocal_rank_fusion
from codebot.tools.code_search import CodeSearch, CodeSearchParams


# ---------------------------------------------------------------------------
# Chunker 测试
# ---------------------------------------------------------------------------


class TestChunker:
    def test_python_ast_chunking(self, tmp_path):
        """Python 文件按函数/类切分。"""
        f = tmp_path / "mod.py"
        f.write_text(
            "def foo():\n"
            "    return 1\n"
            "\n"
            "class Bar:\n"
            "    def method(self):\n"
            "        return 2\n",
            encoding="utf-8",
        )
        chunks = chunk_file(f, tmp_path)
        # 应该有 foo、Bar、method 三个块
        names = {c.name for c in chunks}
        assert "foo" in names
        assert "Bar" in names
        assert "method" in names
        # 每个块的 type 正确
        types = {c.name: c.type for c in chunks}
        assert types["foo"] == "FunctionDef"
        assert types["Bar"] == "ClassDef"
        assert types["method"] == "FunctionDef"

    def test_long_function_sliding_window(self, tmp_path):
        """超长函数被滑窗二次切分。"""
        f = tmp_path / "big.py"
        # 写一个超过 MAX_CHUNK_LINES 行的函数
        body = "\n".join(f"    x{i} = {i}" for i in range(MAX_CHUNK_LINES + 20))
        f.write_text(f"def big():\n{body}\n", encoding="utf-8")

        chunks = chunk_file(f, tmp_path)
        big_chunks = [c for c in chunks if c.name.startswith("big")]
        assert len(big_chunks) >= 2  # 被切成多块

    def test_non_python_sliding(self, tmp_path):
        """非 Python 文件走滑窗分块。"""
        f = tmp_path / "code.js"
        lines = "\n".join(f"const x{i} = {i};" for i in range(MAX_CHUNK_LINES + 5))
        f.write_text(lines, encoding="utf-8")

        chunks = chunk_file(f, tmp_path)
        assert len(chunks) >= 2
        assert all(c.type == "block" for c in chunks)

    def test_syntax_error_falls_back_to_sliding(self, tmp_path):
        """Python 语法错误时滑窗兜底，不抛异常。"""
        f = tmp_path / "broken.py"
        f.write_text("def broken(:\n    pass\n", encoding="utf-8")
        chunks = chunk_file(f, tmp_path)
        # 不抛异常，返回滑窗块
        assert len(chunks) >= 1

    def test_stable_id(self, tmp_path):
        """同位置同名的函数，ID 稳定（增量 upsert 的基础）。"""
        f = tmp_path / "mod.py"
        f.write_text("def foo():\n    return 1\n", encoding="utf-8")
        chunks1 = chunk_file(f, tmp_path)
        chunks2 = chunk_file(f, tmp_path)
        assert chunks1[0].id == chunks2[0].id

    def test_walk_skips_directories(self, tmp_path):
        """walk_indexable_files 跳过 .git/.venv 等。"""
        (tmp_path / "good.py").write_text("x = 1", encoding="utf-8")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config.py").write_text("x = 1", encoding="utf-8")
        (tmp_path / ".codebot").mkdir()
        (tmp_path / ".codebot" / "session.py").write_text("x = 1", encoding="utf-8")

        files = walk_indexable_files(tmp_path)
        rel_paths = [str(f.relative_to(tmp_path)) for f in files]
        assert "good.py" in rel_paths
        assert not any(".git" in p for p in rel_paths)
        assert not any(".codebot" in p for p in rel_paths)


# ---------------------------------------------------------------------------
# BM25 测试
# ---------------------------------------------------------------------------


class TestBM25:
    def test_tokenize(self):
        tokens = tokenize("verifyToken user_id authMiddleware")
        assert tokens == ["verifytoken", "user_id", "authmiddleware"]

    def test_exact_match_ranks_higher(self):
        """精确关键词命中的文档分数更高。"""
        index = BM25Index()
        index.add_docs([
            ("d1", "def verify_token(token): check auth"),
            ("d2", "def login(user): handle user login"),
            ("d3", "class Database: connect to db"),
        ])
        results = index.search("verify_token", top_k=3)
        assert results[0][0] == "d1"  # 精确命中 verify_token

    def test_multiple_terms(self):
        """多词查询，命中多个词的文档排前。"""
        index = BM25Index()
        index.add_docs([
            ("d1", "user login auth"),
            ("d2", "user data"),
            ("d3", "login page"),
        ])
        results = index.search("user login", top_k=3)
        # d1 同时命中 user 和 login，应该排第一
        assert results[0][0] == "d1"

    def test_empty_query_returns_empty(self):
        index = BM25Index()
        index.add_docs([("d1", "some content")])
        assert index.search("", top_k=5) == []

    def test_empty_index_returns_empty(self):
        index = BM25Index()
        assert index.search("anything", top_k=5) == []
        assert index.is_available() is False

    def test_overwrite_doc(self):
        """同 doc_id 重复 add 应覆盖，不重复计数。"""
        index = BM25Index()
        index.add_docs([("d1", "old content")])
        index.add_docs([("d1", "new content")])
        assert index.size == 1
        results = index.search("new", top_k=5)
        assert results[0][0] == "d1"
        # old content 应该被覆盖，搜不到 "old"
        results = index.search("old", top_k=5)
        assert results == []


# ---------------------------------------------------------------------------
# RRF 融合测试
# ---------------------------------------------------------------------------


class TestRRF:
    def test_basic_fusion(self):
        """两路召回融合，两路都靠前的文档排第一。"""
        vec = ["a", "b", "c"]
        kw = ["b", "a", "d"]
        fused = reciprocal_rank_fusion(vec, kw)
        # "b" 在 vec 第2名、kw 第1名；"a" 在 vec 第1名、kw 第2名
        # 但 "b" 总排名更靠前（两路都在前2），应排第一
        assert fused[0] in ("a", "b")
        assert "d" in fused  # 只在 kw 命中也保留

    def test_unique_docs_merged(self):
        """两路不同的文档都保留。"""
        vec = ["a", "b"]
        kw = ["c", "d"]
        fused = reciprocal_rank_fusion(vec, kw)
        assert set(fused) == {"a", "b", "c", "d"}

    def test_single_list(self):
        """单路召回，RRF 退化为原顺序。"""
        vec = ["a", "b", "c"]
        fused = reciprocal_rank_fusion(vec)
        assert fused == vec

    def test_empty_lists(self):
        assert reciprocal_rank_fusion([], []) == []


# ---------------------------------------------------------------------------
# CodeSearch 降级测试
# ---------------------------------------------------------------------------


class TestCodeSearchFallback:
    @pytest.mark.asyncio
    async def test_unavailable_returns_fallback(self):
        """RAG 依赖未装时，CodeSearch 返回降级提示（不抛异常）。"""
        tool = CodeSearch(indexer=None, embedder=None)  # 都不可用
        assert tool.is_available() is False

        params = CodeSearchParams(query="登录鉴权")
        result = await tool.execute(params)
        assert result.is_error is False
        assert "Grep" in result.output  # 引导改用 Grep

    @pytest.mark.asyncio
    async def test_fallback_mentions_install_hint(self):
        """降级提示包含安装指引。"""
        tool = CodeSearch(indexer=None, embedder=None)
        params = CodeSearchParams(query="test")
        result = await tool.execute(params)
        assert "rag" in result.output.lower() or "pip install" in result.output

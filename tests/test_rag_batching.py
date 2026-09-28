"""RAG 修复回归测试：embedding 批量切分 + 记忆索引健壮性。

覆盖 2026-09-28 基准测试暴露出的四个问题：
  1. embedding 请求不分批 —— 超过服务商批量上限时整批失败，语义召回索引恒为空
  2. app.py 用聊天 provider 造 embedder（本文件不覆盖，见 test_app_embedding_provider）
  3. ensure() 失败后每条消息都重试一次注定失败的请求 —— 需要失败冷却
  4. 候选集按目录截断导致记忆进不了索引 / 冷启动一次性 embed 全部记忆

全部使用假客户端与假 embedder，不产生网络请求。
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from codebot.memory.recall import (
    MAX_MEMORY_FILES,
    MemoryHeader,
    _select_relevant_memories,
    scan_memory_files,
)
from codebot.memory.semantic_recall import SemanticMemoryIndex
from codebot.rag.embedding import (
    MAX_BATCH_ITEMS,
    MAX_BATCH_TOKENS,
    EmbeddingError,
    OpenAIEmbedding,
    estimate_tokens,
    plan_batches,
)


# ---------------------------------------------------------------------------
# 假对象
# ---------------------------------------------------------------------------


class FakeEmbeddingsAPI:
    """假 /v1/embeddings 端点。

    ``reject_over`` 模拟服务商的批量上限：超过该条数就报一个带 "batch size"
    字样的错误，用来验证折半重试。
    """

    def __init__(self, reject_over: int | None = None) -> None:
        self.reject_over = reject_over
        self.batch_sizes: list[int] = []  # 所有尝试过的批次（含被拒的）
        self.ok_sizes: list[int] = []     # 只有真正被接受的批次

    async def create(self, input, model):  # noqa: A002 - 对齐 SDK 参数名
        texts = list(input)
        self.batch_sizes.append(len(texts))
        if self.reject_over is not None and len(texts) > self.reject_over:
            raise RuntimeError(
                f"Error code: 400 - Value error, batch size is invalid, "
                f"it should not be larger than {self.reject_over}"
            )
        self.ok_sizes.append(len(texts))
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[float(len(t)), 1.0]) for t in texts]
        )


class FakeClient:
    def __init__(self, reject_over: int | None = None) -> None:
        self.embeddings = FakeEmbeddingsAPI(reject_over)


def make_embedding(reject_over: int | None = None) -> OpenAIEmbedding:
    emb = OpenAIEmbedding(
        api_key="test-key", base_url="http://localhost:1", model="fake-embed"
    )
    emb._client = FakeClient(reject_over)
    return emb


class MockEmbedder:
    """按预设返回向量的假 embedder。"""

    def __init__(self, preset: dict[str, list[float]] | None = None) -> None:
        self._preset = preset or {}
        self.calls = 0

    def is_available(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._preset.get(t, [0.0, 0.0, 0.0, 0.0]) for t in texts]

    async def embed_one(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]


class FailingEmbedder:
    def is_available(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError("mock failure")


def write_memory(path: Path, body: str, mtime: int = 1000) -> MemoryHeader:
    """写一个记忆文件，并把文件系统 mtime 也设成同一个值。

    scan_memory_files 是按真实 stat().st_mtime 排序的，所以只构造 MemoryHeader
    不够，得把落盘时间也控制住。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: t\ndescription: d\ntype: project\n---\n{body}", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return MemoryHeader(
        filename=path.name,
        file_path=str(path.resolve()),
        scope="project",
        mtime_ms=mtime * 1000,
        description="d",
        type="project",
    )


# ---------------------------------------------------------------------------
# 1. estimate_tokens / plan_batches
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    def test_empty_text(self):
        assert estimate_tokens("") == 1

    def test_cjk_heavier_than_latin(self):
        assert estimate_tokens("数据库连接配置") > estimate_tokens("abcdefgh")

    def test_never_zero(self):
        assert estimate_tokens("a") >= 1


class TestPlanBatches:
    def test_empty_input(self):
        assert plan_batches([]) == []

    def test_respects_item_limit(self):
        texts = [f"t{i}" for i in range(MAX_BATCH_ITEMS * 2 + 3)]
        batches = plan_batches(texts)
        assert all(len(b) <= MAX_BATCH_ITEMS for b in batches)
        assert sum(len(b) for b in batches) == len(texts)

    def test_respects_token_budget(self):
        # 每条 2500 个汉字 ≈ 3000 估算 token，两条就超单批 5000 的预算
        texts = ["索" * 2500 for _ in range(3)]
        batches = plan_batches(texts)
        assert all(
            len(b) == 1 or sum(estimate_tokens(t) for t in b) <= MAX_BATCH_TOKENS
            for b in batches
        )
        assert sum(len(b) for b in batches) == 3

    def test_merges_small_texts_up_to_budget(self):
        # 短文本应尽量合并，避免把请求数放大
        texts = ["短文本" for _ in range(MAX_BATCH_ITEMS * 2)]
        batches = plan_batches(texts)
        assert [len(b) for b in batches] == [MAX_BATCH_ITEMS, MAX_BATCH_ITEMS]

    def test_preserves_order(self):
        texts = [str(i) for i in range(7)]
        assert [t for b in plan_batches(texts) for t in b] == texts

    def test_oversized_single_text_kept(self):
        # 单条超过预算也不能被丢弃，应独占一批交给服务端判断
        batches = plan_batches(["超" * 20000])
        assert batches == [["超" * 20000]]


# ---------------------------------------------------------------------------
# 2. OpenAIEmbedding 批量切分
# ---------------------------------------------------------------------------


class TestOpenAIEmbeddingBatching:
    @pytest.mark.asyncio
    async def test_splits_large_input(self):
        emb = make_embedding()
        texts = [f"文本{i}" for i in range(MAX_BATCH_ITEMS * 3)]
        vectors = await emb.embed(texts)

        assert len(vectors) == len(texts)
        # 没有任何一次请求超过条数上限
        assert max(emb._client.embeddings.batch_sizes) <= MAX_BATCH_ITEMS
        # 结果顺序与输入一致（向量首元素编码了原文本长度）
        assert [v[0] for v in vectors] == [float(len(t)) for t in texts]

    @pytest.mark.asyncio
    async def test_empty_input_skips_request(self):
        emb = make_embedding()
        assert await emb.embed([]) == []
        assert emb._client.embeddings.batch_sizes == []

    @pytest.mark.asyncio
    async def test_halves_batch_when_server_rejects(self):
        """服务端嫌批量太大时折半重试，而不是整批失败。"""
        emb = make_embedding(reject_over=4)
        texts = [f"x{i}" for i in range(20)]
        vectors = await emb.embed(texts)

        assert len(vectors) == 20
        # 首次尝试可以超标，但**被接受的**请求都不能超过服务端上限
        assert max(emb._client.embeddings.ok_sizes) <= 4
        assert 20 in emb._client.embeddings.batch_sizes  # 确实先按计划发过一整批

    @pytest.mark.asyncio
    async def test_other_errors_still_raise(self):
        """非批量问题（如鉴权）不折半，直接上抛为 EmbeddingError。"""

        class Boom(FakeEmbeddingsAPI):
            async def create(self, input, model):  # noqa: A002
                raise RuntimeError("Error code: 401 - invalid api key")

        emb = make_embedding()
        emb._client.embeddings = Boom()
        with pytest.raises(EmbeddingError):
            await emb.embed(["a", "b"])


# ---------------------------------------------------------------------------
# 3. ensure() 失败冷却
# ---------------------------------------------------------------------------


class TestEmbedFailureCooldown:
    @pytest.mark.asyncio
    async def test_failure_marks_unavailable(self, tmp_path):
        header = write_memory(tmp_path / "m.md", "内容")
        index = SemanticMemoryIndex(FailingEmbedder(), cooldown_seconds=60.0)
        await index.ensure([header])
        # 失败后进入冷却：上层会直接走 LLM 选择器，不再重试
        assert index.is_available() is False

    @pytest.mark.asyncio
    async def test_zero_cooldown_recovers(self, tmp_path):
        header = write_memory(tmp_path / "m.md", "内容")
        index = SemanticMemoryIndex(FailingEmbedder(), cooldown_seconds=0.0)
        await index.ensure([header])
        assert index.is_available() is True

    @pytest.mark.asyncio
    async def test_success_clears_degraded(self, tmp_path):
        header = write_memory(tmp_path / "m.md", "内容")
        embedder = MockEmbedder({"内容": [1.0, 0.0]})
        index = SemanticMemoryIndex(embedder)
        await index.ensure([header])
        assert index.is_available() is True


# ---------------------------------------------------------------------------
# 4. 单次 ensure 的条数上限（冷启动分批）
# ---------------------------------------------------------------------------


class TestBoundedEnsure:
    @pytest.mark.asyncio
    async def test_respects_per_call_cap(self, tmp_path):
        headers = [write_memory(tmp_path / f"m{i}.md", f"内容{i}", mtime=i) for i in range(5)]
        index = SemanticMemoryIndex(MockEmbedder(), max_embed_per_ensure=2)

        await index.ensure(headers)
        assert len(index._index) == 2
        await index.ensure(headers)
        assert len(index._index) == 4
        await index.ensure(headers)
        assert len(index._index) == 5

    @pytest.mark.asyncio
    async def test_indexes_newest_first(self, tmp_path):
        headers = [write_memory(tmp_path / f"m{i}.md", f"内容{i}", mtime=1000 + i) for i in range(5)]
        index = SemanticMemoryIndex(MockEmbedder(), max_embed_per_ensure=2)
        await index.ensure(headers)
        # mtime 最大的两条应最先进入索引
        assert set(index._index) == {h.file_path for h in headers if h.mtime_ms in (1004 * 1000, 1003 * 1000)}


# ---------------------------------------------------------------------------
# 5. 扫描上限与清单截断
# ---------------------------------------------------------------------------


class TestScanLimit:
    def test_limit_truncates_newest_first(self, tmp_path):
        for i in range(5):
            write_memory(tmp_path / f"m{i}.md", f"内容{i}", mtime=1000 + i)
        headers = scan_memory_files(tmp_path, "project", limit=2)
        assert len(headers) == 2
        assert [h.filename for h in headers] == ["m4.md", "m3.md"]

    def test_default_limit_is_memory_files(self, tmp_path):
        write_memory(tmp_path / "m.md", "内容", mtime=1000)
        headers = scan_memory_files(tmp_path, "project")
        assert len(headers) == 1


class TestManifestTruncation:
    @pytest.mark.asyncio
    async def test_manifest_capped_but_candidates_untouched(self, tmp_path):
        """候选集可以很大（供向量索引），但喂给 LLM 的清单必须有界。"""
        headers = [
            write_memory(tmp_path / f"m{i:04d}.md", f"内容{i}", mtime=1000 + i)
            for i in range(MAX_MEMORY_FILES + 30)
        ]
        captured: dict[str, str] = {}

        async def selector(system_prompt: str, user_message: str) -> str:
            captured["message"] = user_message
            return '{"selected_memories": []}'

        await _select_relevant_memories("随便问问", headers, None, selector)

        manifest_lines = [
            line for line in captured["message"].splitlines() if line.startswith("- ")
        ]
        assert len(manifest_lines) == MAX_MEMORY_FILES

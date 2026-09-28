"""P0 修复的单元测试：embedding 分批（P0-1）+ embedding provider 解析（P0-2）。

用假 client 记录每次实际发出的批次大小，验证"所有被服务端接受的请求都不超上限"，
而不是只看最终有没有报错。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from codebot.rag.embedding import (
    MAX_BATCH_ITEMS,
    MAX_BATCH_TOKENS,
    EmbeddingError,
    OpenAIEmbedding,
    estimate_tokens,
    plan_batches,
)
from codebot.config import ProviderConfig


# ---------------------------------------------------------------------------
# 假 client
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, n: int) -> None:
        self.data = [SimpleNamespace(embedding=[float(n)] * 3) for _ in range(n)]


class _FakeEmbeddings:
    def __init__(self, outer: "_FakeClient") -> None:
        self._outer = outer

    async def create(self, input, model):  # noqa: A002 - 对齐 SDK 签名
        n = len(input)
        self._outer.calls.append(n)
        o = self._outer
        if o.error is not None:
            raise RuntimeError(o.error)
        if o.reject_over is not None and n > o.reject_over:
            raise RuntimeError(
                f"batch size is invalid, it should not be larger than {o.reject_over}"
            )
        o.accepted.append(n)
        return _Resp(n)


class _FakeClient:
    """记录每次请求的批次大小；可配置"超过 N 条就拒绝"或"永远报某个错"。"""

    def __init__(self, reject_over: int | None = None, error: str | None = None) -> None:
        self.calls: list[int] = []
        self.accepted: list[int] = []
        self.reject_over = reject_over
        self.error = error
        self.embeddings = _FakeEmbeddings(self)


def _make_embedder(client: _FakeClient) -> OpenAIEmbedding:
    emb = OpenAIEmbedding(api_key="test", base_url="http://localhost:1/v1")
    emb._client = client  # 注入假 client，避免真实网络
    return emb


# ---------------------------------------------------------------------------
# plan_batches
# ---------------------------------------------------------------------------


def test_plan_batches_empty() -> None:
    assert plan_batches([]) == []


def test_plan_batches_single_short_text() -> None:
    assert plan_batches(["短文本"]) == [["短文本"]]


def test_plan_batches_respects_item_limit() -> None:
    texts = [f"t{i}" for i in range(45)]
    batches = plan_batches(texts, max_items=20)
    assert [len(b) for b in batches] == [20, 20, 5]
    assert all(len(b) <= 20 for b in batches)


def test_plan_batches_respects_token_budget() -> None:
    """构造每条约 100 token 的中文文本，预算 250 → 每批最多 2 条。"""
    text = "中文测试内容" * 20          # 120 个 CJK 字符
    per = estimate_tokens(text)
    assert per > 100
    batches = plan_batches([text] * 10, max_items=1000, max_tokens=per * 2 + 1)
    assert all(len(b) <= 2 for b in batches)
    assert sum(len(b) for b in batches) == 10


def test_plan_batches_preserves_order_and_drops_nothing() -> None:
    texts = [f"text-{i}" for i in range(37)]
    batches = plan_batches(texts, max_items=7)
    flat = [t for b in batches for t in b]
    assert flat == texts
    assert all(b for b in batches)          # 不产生空批次


def test_plan_batches_keeps_oversized_single_item() -> None:
    """单条自身就超 token 预算时，仍单独成批——绝不静默丢弃。"""
    huge = "超长文本" * 5000
    assert estimate_tokens(huge) > MAX_BATCH_TOKENS
    batches = plan_batches([huge, "短"])
    assert [len(b) for b in batches] == [1, 1]
    assert batches[0][0] == huge


def test_estimate_tokens_cjk_vs_ascii() -> None:
    assert estimate_tokens("") >= 1
    assert estimate_tokens("中文测试") > estimate_tokens("abcd")
    # 粗估：中文约 1 token/字（含余量），英文约 3 字符/token
    assert 5 <= estimate_tokens("中文测试") <= 8


# ---------------------------------------------------------------------------
# OpenAIEmbedding 分批
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_splits_into_batches_and_keeps_all_requests_under_limit() -> None:
    """100 条 → 所有被接受的请求批次都不超过 MAX_BATCH_ITEMS。"""
    client = _FakeClient()
    emb = _make_embedder(client)

    vectors = await emb.embed([f"t{i}" for i in range(100)])

    assert len(vectors) == 100
    assert client.accepted, "应该至少发出一次请求"
    assert max(client.accepted) <= MAX_BATCH_ITEMS
    assert sum(client.accepted) == 100


@pytest.mark.asyncio
async def test_embed_halves_batch_when_server_rejects() -> None:
    """服务端在 >4 条时报 batch size 错误 → 仍能全部成功，且被接受的批次 ≤4。"""
    client = _FakeClient(reject_over=4)
    emb = _make_embedder(client)

    vectors = await emb.embed([f"t{i}" for i in range(30)])

    assert len(vectors) == 30
    assert client.accepted, "应该有过成功的请求"
    assert max(client.accepted) <= 4
    assert sum(client.accepted) == 30
    assert any(n > 4 for n in client.calls), "应该确实发生过被拒后折半"


@pytest.mark.asyncio
async def test_embed_does_not_halve_on_auth_error() -> None:
    """鉴权错误必须立刻上抛，不能折半——否则一个可重试错误会被拖成 N 次半量请求。"""
    client = _FakeClient(error="401 invalid api key")
    emb = _make_embedder(client)

    with pytest.raises(EmbeddingError):
        await emb.embed([f"t{i}" for i in range(100)])

    assert len(client.calls) == 1, f"只应发一次请求，实际 {client.calls}"
    assert client.accepted == []


@pytest.mark.asyncio
async def test_embed_empty_returns_empty_without_request() -> None:
    client = _FakeClient()
    emb = _make_embedder(client)
    assert await emb.embed([]) == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_embed_raises_when_single_item_rejected() -> None:
    """折到单条仍被拒 → 当真实错误上抛（而不是无限折半）。"""
    client = _FakeClient(reject_over=0)
    emb = _make_embedder(client)

    with pytest.raises(EmbeddingError):
        await emb.embed(["只有一条"])


# ---------------------------------------------------------------------------
# P0-2：embedding provider 解析
# ---------------------------------------------------------------------------


def _provider(name: str, model: str) -> ProviderConfig:
    return ProviderConfig(
        name=name, protocol="openai-compat",
        base_url=f"http://{name}.test/v1", model=model,
    )


class _StubApp:
    def __init__(self, embedding_provider=None) -> None:
        self._embedding_provider = embedding_provider


def test_resolve_embedding_provider_prefers_configured() -> None:
    """配了 embedding_provider 时必须用它，而不是聊天 provider。"""
    from codebot.app import CodeBotApp

    chat = _provider("chat", "deepseek-flash")
    emb = _provider("emb", "qwen3.7-text-embedding-flash")

    assert CodeBotApp._resolve_embedding_provider(_StubApp(emb), chat) is emb


def test_resolve_embedding_provider_falls_back_to_chat() -> None:
    """未配置 embedding_provider 时回退到传入的聊天 provider。"""
    from codebot.app import CodeBotApp

    chat = _provider("chat", "deepseek-flash")
    assert CodeBotApp._resolve_embedding_provider(_StubApp(None), chat) is chat


def test_codebot_app_accepts_embedding_provider_param() -> None:
    """CodeBotApp.__init__ 必须暴露 embedding_provider 参数（供 __main__ 注入）。"""
    from codebot.app import CodeBotApp

    params = inspect.signature(CodeBotApp.__init__).parameters
    assert "embedding_provider" in params
    assert params["embedding_provider"].default is None

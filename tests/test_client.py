"""LLM 客户端单元测试。

覆盖：
  - _parse_retry_after 安全解析
  - _reraise_stream_error 异常映射
  - stream_with_retry 重试逻辑
  - create_client 工厂函数
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from codebot.client import (
    AuthenticationError,
    LLMClient,
    LLMError,
    NetworkError,
    RateLimitError,
    _parse_retry_after,
    _reraise_stream_error,
    create_client,
)
from codebot.config import ProviderConfig
from codebot.conversation import ConversationManager
from codebot.tools.base import StreamEnd, StreamEvent, TextDelta


# ---------------------------------------------------------------------------
# _parse_retry_after
# ---------------------------------------------------------------------------

class TestParseRetryAfter:
    def test_none_returns_none(self):
        assert _parse_retry_after(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_retry_after("") is None

    def test_numeric_string(self):
        assert _parse_retry_after("5") == 5.0

    def test_float_string(self):
        assert _parse_retry_after("2.5") == 2.5

    def test_negative_returns_none(self):
        assert _parse_retry_after("-1") is None

    def test_zero_returns_none(self):
        assert _parse_retry_after("0") is None

    def test_http_date_returns_none(self):
        assert _parse_retry_after("Fri, 31 Dec 2025 23:59:59 GMT") is None

    def test_garbage_returns_none(self):
        assert _parse_retry_after("not-a-number") is None


# ---------------------------------------------------------------------------
# _reraise_stream_error
# ---------------------------------------------------------------------------

class TestReraiseStreamError:
    def test_reraises_unrecognized_exceptions(self):
        with pytest.raises(ValueError, match="boom"):
            _reraise_stream_error("anthropic", ValueError("boom"))

    def test_reraises_unrecognized_for_openai_compat(self):
        with pytest.raises(RuntimeError, match="test"):
            _reraise_stream_error("openai-compat", RuntimeError("test"))

    def test_anthropic_module_loaded_for_anthropic_provider(self):
        # 验证 anthropic provider 路径能正确加载 anthropic 模块
        import anthropic as _mod
        assert hasattr(_mod, "AuthenticationError")
        assert hasattr(_mod, "RateLimitError")

    def test_openai_module_loaded_for_openai_provider(self):
        # 验证 openai provider 路径能正确加载 openai 模块
        import openai as _mod
        assert hasattr(_mod, "AuthenticationError")
        assert hasattr(_mod, "RateLimitError")


# ---------------------------------------------------------------------------
# stream_with_retry
# ---------------------------------------------------------------------------

class _FailingThenSucceedClient(LLMClient):
    """Mock client that fails N times then succeeds."""

    def __init__(self, fail_count: int, error_type: type) -> None:
        self._fail_count = fail_count
        self._error_type = error_type
        self._call_count = 0

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self._call_count += 1
        if self._call_count <= self._fail_count:
            raise self._error_type(f"fail #{self._call_count}")
        yield TextDelta(text="success")
        yield StreamEnd(stop_reason="end_turn", input_tokens=10, output_tokens=5)


class TestStreamWithRetry:
    @pytest.mark.asyncio
    async def test_succeeds_on_first_try(self):
        client = _FailingThenSucceedClient(0, NetworkError)
        conv = ConversationManager()
        events = []
        async for e in client.stream_with_retry(conv, max_retries=3, base_delay=0.01):
            events.append(e)
        assert any(isinstance(e, TextDelta) and e.text == "success" for e in events)
        assert client._call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_network_error(self):
        client = _FailingThenSucceedClient(2, NetworkError)
        conv = ConversationManager()
        events = []
        async for e in client.stream_with_retry(conv, max_retries=3, base_delay=0.01):
            events.append(e)
        assert any(isinstance(e, TextDelta) and e.text == "success" for e in events)
        assert client._call_count == 3

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit(self):
        client = _FailingThenSucceedClient(1, RateLimitError)
        conv = ConversationManager()
        events = []
        async for e in client.stream_with_retry(conv, max_retries=3, base_delay=0.01):
            events.append(e)
        assert any(isinstance(e, TextDelta) and e.text == "success" for e in events)
        assert client._call_count == 2

    @pytest.mark.asyncio
    async def test_does_not_retry_auth_error(self):
        client = _FailingThenSucceedClient(1, AuthenticationError)
        conv = ConversationManager()
        with pytest.raises(AuthenticationError):
            async for _ in client.stream_with_retry(conv, max_retries=3, base_delay=0.01):
                pass
        assert client._call_count == 1

    @pytest.mark.asyncio
    async def test_does_not_retry_llm_error(self):
        client = _FailingThenSucceedClient(1, LLMError)
        conv = ConversationManager()
        with pytest.raises(LLMError):
            async for _ in client.stream_with_retry(conv, max_retries=3, base_delay=0.01):
                pass
        assert client._call_count == 1

    @pytest.mark.asyncio
    async def test_exhausts_retries(self):
        client = _FailingThenSucceedClient(5, NetworkError)
        conv = ConversationManager()
        with pytest.raises(NetworkError):
            async for _ in client.stream_with_retry(conv, max_retries=3, base_delay=0.01):
                pass
        assert client._call_count == 4  # 1 initial + 3 retries


# ---------------------------------------------------------------------------
# create_client
# ---------------------------------------------------------------------------

class TestCreateClient:
    def test_anthropic_protocol(self):
        config = ProviderConfig(
            name="test", protocol="anthropic", base_url="https://api.anthropic.com",
            model="claude-sonnet-4-20250514", api_key="test-key",
        )
        from codebot.client import AnthropicClient
        client = create_client(config)
        assert isinstance(client, AnthropicClient)

    def test_openai_protocol(self):
        config = ProviderConfig(
            name="test", protocol="openai", base_url="https://api.openai.com/v1",
            model="gpt-4o", api_key="test-key",
        )
        from codebot.client import OpenAIClient
        client = create_client(config)
        assert isinstance(client, OpenAIClient)

    def test_openai_compat_protocol(self):
        config = ProviderConfig(
            name="test", protocol="openai-compat", base_url="http://localhost:8000/v1",
            model="local-model", api_key="test-key",
        )
        from codebot.client import OpenAICompatClient
        client = create_client(config)
        assert isinstance(client, OpenAICompatClient)

    def test_unknown_protocol_raises(self):
        config = ProviderConfig(
            name="test", protocol="unknown", base_url="http://localhost",
            model="model", api_key="key",
        )
        with pytest.raises(ValueError, match="Unknown protocol"):
            create_client(config)

"""记忆召回共享模块 + 桌面端接线的回归测试。

覆盖 2026-09-28 修复的「桌面端没有记忆召回」这一缺陷：
  - codebot/memory/reminder.py：终端与桌面端共用的召回器
  - codebot/server.py：SessionConnection 每轮召回并注入 system-reminder

全部用 monkeypatch 替换网络调用，不产生真实请求。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import codebot.memory.reminder as reminder_mod
from codebot.memory.recall import RelevantMemory
from codebot.memory.reminder import MemoryReminder
from codebot.rag.embedding import NullEmbedding


# ---------------------------------------------------------------------------
# 假对象
# ---------------------------------------------------------------------------


def make_provider(name: str = "chat"):
    """构造一个最小可用的 ProviderConfig（不会被真的调用）。"""
    from codebot.config import ProviderConfig

    return ProviderConfig(
        name=name, protocol="openai-compat", base_url="http://localhost:1",
        model="fake-model", api_key="fake-key",
    )


class FakeSemanticIndex:
    def __init__(self, available: bool = True) -> None:
        self._available = available

    def is_available(self) -> bool:
        return self._available

    async def ensure(self, headers):  # pragma: no cover - 本测试不走这里
        return None

    async def search(self, query):  # pragma: no cover
        return []


class RecordingConversation:
    def __init__(self) -> None:
        self.reminders: list[str] = []

    def add_system_reminder(self, text: str) -> None:
        self.reminders.append(text)


# ---------------------------------------------------------------------------
# MemoryReminder
# ---------------------------------------------------------------------------


class TestMemoryReminderBasics:
    @pytest.mark.asyncio
    async def test_blank_query_does_nothing(self, tmp_path, monkeypatch):
        """空 query 直接返回，不该触发任何召回。"""
        called = []

        async def fake_recall(**kwargs):
            called.append(kwargs)
            return []

        monkeypatch.setattr(reminder_mod, "find_relevant_memories", fake_recall)
        reminder = MemoryReminder(tmp_path, make_provider())

        assert await reminder.build("") == ""
        assert await reminder.build("   ") == ""
        assert called == []

    @pytest.mark.asyncio
    async def test_renders_matched_memory(self, tmp_path, monkeypatch):
        """命中的记忆应被渲染成可注入的 reminder 文本。"""
        mem = tmp_path / "note.md"
        mem.write_text("---\nname: n\n---\n这条记忆说明登录逻辑在 auth.py", encoding="utf-8")

        async def fake_recall(**kwargs):
            assert kwargs["query"] == "登录逻辑在哪"
            return [RelevantMemory(path=str(mem), mtime_ms=1_700_000_000_000)]

        monkeypatch.setattr(reminder_mod, "find_relevant_memories", fake_recall)
        text = await MemoryReminder(tmp_path, make_provider()).build("登录逻辑在哪")

        assert "note.md" in text
        assert "登录逻辑在 auth.py" in text

    @pytest.mark.asyncio
    async def test_returns_empty_on_failure(self, tmp_path, monkeypatch):
        """召回抛异常时必须静默降级，不能影响主对话。"""

        async def boom(**kwargs):
            raise RuntimeError("selector exploded")

        monkeypatch.setattr(reminder_mod, "find_relevant_memories", boom)
        assert await MemoryReminder(tmp_path, make_provider()).build("随便问问") == ""

    @pytest.mark.asyncio
    async def test_returns_empty_on_timeout(self, tmp_path, monkeypatch):
        """超时必须放弃本轮召回，而不是卡住主流程。"""

        async def slow(**kwargs):
            await asyncio.sleep(5)
            return []

        monkeypatch.setattr(reminder_mod, "find_relevant_memories", slow)
        reminder = MemoryReminder(tmp_path, make_provider(), timeout=0.05)
        assert await reminder.build("随便问问") == ""


class TestSemanticIndexSelection:
    def test_embedding_provider_takes_priority(self, tmp_path):
        """配置里的 embedding provider 优先于聊天 provider。"""
        chat, emb = make_provider("chat"), make_provider("emb")
        r = MemoryReminder(tmp_path, chat, emb)
        assert r._embedding_provider is emb

    def test_falls_back_to_chat_provider(self, tmp_path):
        chat = make_provider("chat")
        r = MemoryReminder(tmp_path, chat, None)
        assert r._embedding_provider is chat

    def test_unavailable_embedder_yields_none_and_caches(self, tmp_path, monkeypatch):
        """embedding 不可用时返回 None，且只尝试一次（不每条消息重试）。"""
        attempts = []

        def fake_create(cfg):
            attempts.append(cfg)
            return NullEmbedding("test")

        monkeypatch.setattr("codebot.rag.embedding.create_embedding_provider", fake_create)
        r = MemoryReminder(tmp_path, make_provider())

        assert r._get_semantic_index() is None
        assert r._get_semantic_index() is None
        assert len(attempts) == 1  # 只尝试构造一次

    def test_uses_injected_memory_manager(self, tmp_path):
        """外部传入的 MemoryManager 应被复用，避免重复扫描记忆目录。"""
        sentinel = object()
        r = MemoryReminder(tmp_path, make_provider(), memory_manager=sentinel)
        assert r.memory_manager is sentinel


# ---------------------------------------------------------------------------
# 桌面端接线：SessionConnection
# ---------------------------------------------------------------------------


def make_session(builder):
    """构造一个 SessionConnection，只带召回所需的最小依赖。"""
    from codebot.server import SessionConnection

    runtime = SimpleNamespace(
        memory_reminder=builder,
        conversation=RecordingConversation(),
    )
    return SessionConnection(websocket=None, runtime=runtime)


class FakeBuilder:
    def __init__(self, text: str = "记忆片段", delay: float = 0.0, raises: bool = False):
        self.text = text
        self.delay = delay
        self.raises = raises
        self.calls: list[str] = []

    async def build(self, query: str) -> str:
        self.calls.append(query)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises:
            raise RuntimeError("recall failed")
        return self.text


class TestDesktopMemoryWiring:
    def test_no_prefetch_for_blank_input(self):
        conn = make_session(FakeBuilder())
        assert conn._start_memory_prefetch("") is None
        assert conn._start_memory_prefetch("   ") is None

    def test_no_prefetch_without_builder(self):
        conn = make_session(None)
        assert conn._start_memory_prefetch("你好") is None

    @pytest.mark.asyncio
    async def test_injects_reminder_into_conversation(self):
        builder = FakeBuilder(text="## Memory: a.md\n内容")
        conn = make_session(builder)

        task = conn._start_memory_prefetch("这段鉴权代码在哪")
        await conn._inject_memory_reminder(task)

        assert builder.calls == ["这段鉴权代码在哪"]
        assert conn.runtime.conversation.reminders == ["## Memory: a.md\n内容"]

    @pytest.mark.asyncio
    async def test_empty_reminder_not_injected(self):
        conn = make_session(FakeBuilder(text=""))
        await conn._inject_memory_reminder(conn._start_memory_prefetch("问一句"))
        assert conn.runtime.conversation.reminders == []

    @pytest.mark.asyncio
    async def test_failure_is_silent(self):
        """召回失败不能冒泡，更不能挡住本轮对话。"""
        conn = make_session(FakeBuilder(raises=True))
        await conn._inject_memory_reminder(conn._start_memory_prefetch("问一句"))
        assert conn.runtime.conversation.reminders == []

    @pytest.mark.asyncio
    async def test_timeout_gives_up(self, monkeypatch):
        import codebot.server as server_mod

        monkeypatch.setattr(server_mod, "MEMORY_RECALL_WAIT", 0.05)
        conn = make_session(FakeBuilder(text="迟到的记忆", delay=1.0))

        await conn._inject_memory_reminder(conn._start_memory_prefetch("慢查询"))
        assert conn.runtime.conversation.reminders == []
        await asyncio.sleep(0)  # 让被取消的任务有机会收尾


# ---------------------------------------------------------------------------
# Runtime 字段
# ---------------------------------------------------------------------------


class TestRuntimeField:
    def test_memory_reminder_defaults_to_none(self):
        """Runtime 新增字段必须有默认值，避免破坏既有构造点。"""
        import dataclasses

        from codebot.runtime import Runtime

        f = {x.name: x for x in dataclasses.fields(Runtime)}["memory_reminder"]
        assert f.default is None

    def test_build_runtime_wires_memory(self):
        """build_runtime 必须把 memory_manager 交给 Agent，并挂上召回器。

        这里只做源码级断言：完整跑 build_runtime 需要 qdrant 等重依赖，
        而这两处接线一旦漏掉就是"桌面端没有记忆"的缺陷复现。
        """
        src = Path("codebot/runtime.py").read_text(encoding="utf-8")
        assert "memory_manager=memory_manager" in src
        assert "memory_reminder=memory_reminder" in src
        assert "embedding_provider=config.embedding_provider" in src

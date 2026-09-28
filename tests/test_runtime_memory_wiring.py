"""桌面端（runtime.build_runtime）接入第一层四类记忆的接线测试。

背景：`agent.py` 里长期记忆注入与自动抽取都以 `self.memory_manager` 为开关，
而 runtime 原先没有传它 —— 导致桌面端的记忆子系统整体关闭（计划里的 P2-7）。
这里锁住这个接线，避免以后再被误删。

网络与 RAG 都被屏蔽，测试完全自包含（不依赖本机 ~/.codebot/config.yaml）。
"""

from __future__ import annotations

import pytest

from codebot.config import AppConfig, ProviderConfig
from codebot.permissions import PermissionMode
from codebot.rag.embedding import NullEmbedding

MEMORIES = "### 项目知识\n- 测试知识：runtime 必须把记忆注入会话\n"


def _config() -> AppConfig:
    return AppConfig(
        providers=[
            ProviderConfig(
                name="fake",
                protocol="openai-compat",
                base_url="http://127.0.0.1:1/v1",
                model="fake-model",
                api_key="sk-fake",
            )
        ]
    )


def _patch(monkeypatch) -> None:
    """屏蔽一切网络：RAG 直接降级，context window 不拉取。"""
    monkeypatch.setattr(
        "codebot.rag.create_embedding_provider",
        lambda provider: NullEmbedding("测试环境禁用 embedding"),
    )

    async def _noop(provider) -> None:
        return None

    monkeypatch.setattr("codebot.client.resolve_context_window", _noop)


def _write_memories(work_dir) -> None:
    d = work_dir / ".codebot"
    d.mkdir(parents=True, exist_ok=True)
    (d / "memories.md").write_text(MEMORIES, encoding="utf-8")


@pytest.mark.asyncio
async def test_build_runtime_wires_memory_manager(monkeypatch, tmp_path) -> None:
    """build_runtime 必须构造 MemoryManager 并传给 Agent。"""
    _patch(monkeypatch)
    _write_memories(tmp_path)

    from codebot.runtime import build_runtime

    rt = await build_runtime(
        _config(), PermissionMode.DEFAULT, None, work_dir=str(tmp_path)
    )

    assert rt.memory_manager is not None
    assert rt.agent.memory_manager is rt.memory_manager
    assert "测试知识" in rt.memory_manager.load()


@pytest.mark.asyncio
async def test_build_runtime_memory_manager_bound_to_work_dir(monkeypatch, tmp_path) -> None:
    """memory_manager 必须绑定到传入的 work_dir（切目录时要跟着换）。"""
    _patch(monkeypatch)
    _write_memories(tmp_path)

    from codebot.runtime import build_runtime

    rt = await build_runtime(
        _config(), PermissionMode.DEFAULT, None, work_dir=str(tmp_path)
    )

    assert str(tmp_path) in str(rt.memory_manager.project_path)


@pytest.mark.asyncio
async def test_desktop_agent_injects_memories(monkeypatch, tmp_path) -> None:
    """端到端：agent.run() 启动时应把四类记忆注入会话。

    agent.run() 在进入主循环之前就完成注入，把 max_iterations 设为 0
    即可在不发任何 LLM 请求的情况下跑完注入流程。
    """
    _patch(monkeypatch)
    _write_memories(tmp_path)

    from codebot.runtime import build_runtime

    rt = await build_runtime(
        _config(), PermissionMode.DEFAULT, None, work_dir=str(tmp_path)
    )

    rt.agent.max_iterations = 0
    gen = rt.agent.run(rt.conversation)
    try:
        await gen.__anext__()
    except StopAsyncIteration:
        pass
    finally:
        await gen.aclose()

    assert rt.conversation.ltm_injected is True
    joined = "\n".join(m.content or "" for m in rt.conversation.history)
    assert "# autoMemory" in joined
    assert "测试知识" in joined


@pytest.mark.asyncio
async def test_agent_without_memory_manager_does_not_inject(monkeypatch, tmp_path) -> None:
    """对照组：摘掉 memory_manager（模拟修复前的行为）时不应有 autoMemory 段。"""
    _patch(monkeypatch)
    _write_memories(tmp_path)

    from codebot.runtime import build_runtime

    rt = await build_runtime(
        _config(), PermissionMode.DEFAULT, None, work_dir=str(tmp_path)
    )
    rt.agent.memory_manager = None

    rt.agent.max_iterations = 0
    gen = rt.agent.run(rt.conversation)
    try:
        await gen.__anext__()
    except StopAsyncIteration:
        pass
    finally:
        await gen.aclose()

    joined = "\n".join(m.content or "" for m in rt.conversation.history)
    assert "# autoMemory" not in joined


def test_runtime_dataclass_exposes_memory_manager() -> None:
    from dataclasses import fields

    from codebot.runtime import Runtime

    names = {f.name for f in fields(Runtime)}
    assert "memory_manager" in names

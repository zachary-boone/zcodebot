"""按用户 query 召回相关记忆，并渲染成可注入对话的 system-reminder 文本。

为什么单独抽一个模块：
    终端（app.py）和桌面端（server.py）都需要「每轮对话前按 query 召回相关记忆」
    这件事，但两端 UI 形态不同（Textual / WebSocket）。此前这段逻辑只写在 app.py
    里，桌面端没有——于是"记忆召回"这项能力只在终端生效。更早的一次事故也是同类
    问题：provider 解析在 app.py 和 runtime.py 各写了一份，其中一份把 embedding
    provider 传成了聊天 provider，导致语义召回长期静默失效。收在一处可以避免第三份。

召回优先级（与 memory/semantic_recall.py 保持一致）：
    embedding 语义检索 → 相似度全部低于阈值 / 不可用 / 失败 → LLM 选择器

降级策略：
    整段召回被 timeout 包住，任何异常都返回空字符串。记忆召回是增强，
    绝不阻塞或拖慢主对话。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from codebot.config import ProviderConfig
from codebot.memory.auto_memory import MemoryManager
from codebot.memory.recall import find_relevant_memories, render_reminder

log = logging.getLogger(__name__)

# 单次记忆召回的超时上限（秒）。
# 语义检索命中时实测约 0.15 秒；退化成 LLM 选择器时约 2.5 秒。
# 超过这个上限就放弃本轮召回——宁可不召回，也不能让首字延迟失控。
DEFAULT_TIMEOUT = 8.0


class MemoryReminder:
    """相关记忆召回器。构造一次后可反复复用（内部缓存语义索引与向量）。

    用法：
        reminder = MemoryReminder(work_dir, chat_provider, embedding_provider)
        text = await reminder.build("这段代码的鉴权逻辑在哪")
        if text:
            conversation.add_system_reminder(text)
    """

    def __init__(
        self,
        work_dir: str | Path,
        chat_provider: ProviderConfig,
        embedding_provider: ProviderConfig | None = None,
        *,
        memory_manager: MemoryManager | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """chat_provider 用于 LLM 选择器兜底；embedding_provider 用于语义检索。

        embedding_provider 未配置时回退到 chat_provider——但这种回退通常无效
        （多数厂商的聊天端点不提供 /v1/embeddings），所以配置里应显式给出。
        """
        self._work_dir = Path(work_dir)
        self._chat_provider = chat_provider
        self._embedding_provider = embedding_provider or chat_provider
        self._timeout = timeout
        self._memory_manager = memory_manager or MemoryManager(str(work_dir))
        self._semantic_index: Any | None = None
        self._semantic_initialized = False

    @property
    def memory_manager(self) -> MemoryManager:
        return self._memory_manager

    # ------------------------------------------------------------------
    # 语义索引（懒加载）
    # ------------------------------------------------------------------

    def _get_semantic_index(self):
        """懒加载记忆语义索引；不可用时返回 None（上层走 LLM 选择器）。

        只尝试一次——失败后不重复构造，避免每条消息都白跑一次初始化。
        """
        if self._semantic_initialized:
            return self._semantic_index
        self._semantic_initialized = True
        try:
            from codebot.memory.semantic_recall import SemanticMemoryIndex
            from codebot.rag.embedding import create_embedding_provider

            if self._embedding_provider is None:
                return None
            embedder = create_embedding_provider(self._embedding_provider)
            if not embedder.is_available():
                log.debug(
                    "记忆召回：embedding 不可用，退化到 LLM 选择器（provider=%s）",
                    getattr(self._embedding_provider, "name", "?"),
                )
                return None
            self._semantic_index = SemanticMemoryIndex(embedder)
        except Exception as e:
            log.debug("记忆语义索引初始化失败（退化到 LLM 选择器）: %s", e)
            self._semantic_index = None
        return self._semantic_index

    # ------------------------------------------------------------------
    # LLM 选择器（兜底路径）
    # ------------------------------------------------------------------

    def _make_selector(self):
        """构造 LLM 选择器。

        用独立的 client 与独立的 system prompt——选择器是"侧查询"，
        不能受主对话上下文影响。
        """

        async def selector(system_prompt: str, user_message: str) -> str:
            from codebot.client import create_client
            from codebot.conversation import ConversationManager, Message
            from codebot.tools.base import TextDelta

            side_client = create_client(self._chat_provider)
            mini_conv = ConversationManager()
            mini_conv.history = [Message(role="user", content=user_message)]
            collected = ""
            async for event in side_client.stream(mini_conv, system=system_prompt):
                if isinstance(event, TextDelta):
                    collected += event.text
            return collected

        return selector

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    async def build(self, query: str, already_surfaced: set[str] | None = None) -> str:
        """返回可直接注入对话的 system-reminder 文本。

        没有相关记忆、超时或任何异常都返回 ""（调用方据此决定是否注入）。
        """
        if not query or not query.strip():
            return ""

        try:
            results = await asyncio.wait_for(
                find_relevant_memories(
                    query=query,
                    user_mem_dir=self._memory_manager.user_mem_dir,
                    project_mem_dir=self._memory_manager.project_mem_dir,
                    recent_tools=None,
                    already_surfaced=already_surfaced,
                    selector=self._make_selector(),
                    semantic_index=self._get_semantic_index(),
                ),
                timeout=self._timeout,
            )
            return render_reminder(results)
        except Exception as e:
            log.debug("记忆召回失败（不影响主流程）: %s", e)
            return ""

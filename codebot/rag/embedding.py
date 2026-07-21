"""Embedding 抽象层。

复用项目已有的 ProviderConfig（Anthropic / OpenAI 协议），不引入新的鉴权配置。
为 RAG 各场景（记忆检索、代码语义搜索、工具匹配）提供统一的文本向量化能力。

设计要点：
  - 抽象接口只有一个 ``embed`` 方法（和 LLMClient 的 ``stream`` 一样走策略模式）
  - 三种实现：OpenAIEmbedding / OpenAICompatEmbedding / 不可用时的 NullEmbedding
  - Anthropic 暂无官方 embedding 端点，走 OpenAI 兼容协议兜底
    （多数 Anthropic 用户会另配一个 OpenAI 兼容的 embedding 来源，或用本地模型）
  - 失败永不抛出阻断主流程——上层用 is_available() 判断后决定是否降级
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from typing import Any

from codebot.config import ProviderConfig

log = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """embedding 调用失败。上层应捕获并降级。"""


# ---------------------------------------------------------------------------
# 抽象接口
# ---------------------------------------------------------------------------


class EmbeddingProvider(ABC):
    """文本 embedding 的统一接口。"""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """对一批文本做 embedding。返回与 texts 等长的向量列表。"""

    async def embed_one(self, text: str) -> list[float]:
        """单文本 embedding 的便捷封装。"""
        vectors = await self.embed([text])
        return vectors[0]

    def is_available(self) -> bool:
        """是否可用。Null 实现返回 False，上层据此降级。"""
        return True


# ---------------------------------------------------------------------------
# 工具函数：余弦相似度
# ---------------------------------------------------------------------------


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度：dot(a,b) / (|a| * |b|)。

    语义检索的标准度量——比较方向而非长度，对文本长度不敏感。
    归一化向量上等价于点积；这里不假设已归一化，自行计算范数。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ---------------------------------------------------------------------------
# OpenAI 系列实现（也用于 openai-compat 协议：DeepSeek/Qwen/Ollama 等）
# ---------------------------------------------------------------------------


class OpenAIEmbedding(EmbeddingProvider):
    """基于 OpenAI / OpenAI 兼容协议的 embedding。

    DeepSeek、Qwen、Ollama、vLLM 等只要兼容 OpenAI 的 /v1/embeddings 端点都能用。
    Ollama 本地模型可省 api_key（随便填），base_url 指向 http://localhost:11434/v1。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "text-embedding-3-small",
        protocol: str = "openai-compat",
    ) -> None:
        self._model = model
        self._protocol = protocol
        self._client = self._build_client(api_key, base_url)
        self._dim: int | None = None

    def _build_client(self, api_key: str, base_url: str) -> Any:
        try:
            from openai import AsyncOpenAI
        except ImportError as e:  # pragma: no cover - 依赖缺失分支
            raise EmbeddingError(
                "openai 包未安装，无法使用 embedding。请安装: uv pip install -e '.[rag]'"
            ) from e
        return AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            resp = await self._client.embeddings.create(
                input=texts,
                model=self._model,
            )
            vectors = [d.embedding for d in resp.data]
            if self._dim is None and vectors:
                self._dim = len(vectors[0])
            return vectors
        except Exception as e:  # 网络 / 鉴权 / 限流统一降级
            raise EmbeddingError(f"embedding 调用失败: {e}") from e


# ---------------------------------------------------------------------------
# Null 实现：不可用时优雅降级
# ---------------------------------------------------------------------------


class NullEmbedding(EmbeddingProvider):
    """embedding 不可用时的占位实现。

    上层通过 is_available() 判断后走降级路径（如 CodeSearch 回退到 Grep 提示、
    记忆检索回退到 LLM 选择器）。调用 embed 会抛 EmbeddingError，确保问题早暴露。
    """

    def __init__(self, reason: str = "embedding 未配置") -> None:
        self._reason = reason

    def is_available(self) -> bool:
        return False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError(self._reason)


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------


def create_embedding_provider(provider: ProviderConfig) -> EmbeddingProvider:
    """根据 provider 配置创建 embedding 实例。

    策略：
      - openai / openai-compat 协议 → OpenAIEmbedding（兼容 DeepSeek/Qwen/Ollama）
      - anthropic 协议 → 暂无官方 embedding 端点，返回 NullEmbedding
        （Anthropic 用户建议额外配一个 openai-compat 的 embedding provider）
      - 任何导入失败 / key 缺失 → NullEmbedding，不抛异常

    返回 NullEmbedding 时上层应走降级路径，而不是中断启动。
    """
    protocol = provider.protocol
    api_key = provider.resolve_api_key()

    if protocol in ("openai", "openai-compat"):
        if not api_key:
            log.warning("embedding: %s 协议缺少 api_key，降级为 NullEmbedding", protocol)
            return NullEmbedding(f"{protocol} 协议缺少 api_key")
        # embedding 模型默认用 text-embedding-3-small；Ollama 等本地模型
        # 用户应在 provider.model 里显式指定（如 nomic-embed-text）。
        model = _pick_embedding_model(provider.model, provider.base_url)
        try:
            return OpenAIEmbedding(
                api_key=api_key,
                base_url=provider.base_url,
                model=model,
                protocol=protocol,
            )
        except EmbeddingError as e:
            log.warning("embedding 初始化失败，降级为 NullEmbedding: %s", e)
            return NullEmbedding(str(e))

    # anthropic 协议暂无官方 embedding 端点
    log.info(
        "embedding: %s 协议不支持 embedding，降级为 NullEmbedding "
        "（建议额外配置一个 openai-compat 的 embedding provider）",
        protocol,
    )
    return NullEmbedding(f"{protocol} 协议暂不支持 embedding")


def _pick_embedding_model(chat_model: str, base_url: str) -> str:
    """选择 embedding 模型。

    优先用 provider 配置里的 model（如果它看起来像 embedding 模型），
    否则给一个保守默认。Ollama 等本地服务用户应在配置里显式写 embedding 模型名。
    """
    # 明显是 embedding 模型的名字，直接用
    lowered = chat_model.lower()
    if "embed" in lowered or "bge" in lowered or "nomic" in lowered:
        return chat_model
    # 默认走 OpenAI 标准模型（兼容服务大多也支持）
    return "text-embedding-3-small"

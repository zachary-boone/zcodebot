"""Embedding 抽象层。

复用项目已有的 ProviderConfig（Anthropic / OpenAI 协议），不引入新的鉴权配置。
为 RAG 各场景（记忆检索、代码语义搜索、工具匹配）提供统一的文本向量化能力。

设计要点：
  - 抽象接口只有一个 ``embed`` 方法（和 LLMClient 的 ``stream`` 一样走策略模式）
  - 三种实现：OpenAIEmbedding / OpenAICompatEmbedding / 不可用时的 NullEmbedding
  - Anthropic 暂无官方 embedding 端点，走 OpenAI 兼容协议兜底
    （多数 Anthropic 用户会另配一个 OpenAI 兼容的 embedding 来源，或用本地模型）
  - 失败永不抛出阻断主流程——上层用 is_available() 判断后决定是否降级
  - 批量切分在 ``OpenAIEmbedding`` 内部完成：调用方可以放心把「一个文件的所有块」
    或「全部记忆正文」一次性传进来，不需要自己关心服务商的批量上限
"""

from __future__ import annotations

import logging
import math
import re
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
# 批量切分：单次 embedding 请求的规模保护
# ---------------------------------------------------------------------------
#
# 为什么要在这里切分（而不是让调用方自己切）：
# 各家 /v1/embeddings 对单次请求的规模都有上限，而且这个上限**通常是按总 token
# 动态算的，不是固定条数**——DashScope 的 qwen3.7-text-embedding-flash 在同一批
# 语料上先后返回过 "should not be larger than 25" 和 "should not be larger than 20"。
# 调用方（记忆索引、代码索引）各自去猜上限既重复又容易漏，所以在 provider 层
# 统一处理：既按条数切、也按估算 token 切，取更严的那个。
MAX_BATCH_ITEMS = 20
MAX_BATCH_TOKENS = 5000

# 判定「服务端嫌批量太大」的错误特征。只有命中才折半重试；其他错误
# （鉴权 / 网络 / 限流）直接上抛，避免把可重试错误拖成 N 次半量请求。
_BATCH_TOO_LARGE_RE = re.compile(
    r"batch\s*size|too\s*large|too\s*many|exceeds?\b|maximum\s*batch",
    re.IGNORECASE,
)


def estimate_tokens(text: str) -> int:
    """粗略估算文本的 token 数，只用于切分批次。

    中文按约 1 token/字、其他字符按约 3 字符/token，并额外留 20% 余量。
    宁可高估——高估只会让批次更小（代价是请求数变多），低估则会让整批被拒。
    """
    cjk = 0
    for ch in text:
        if "\u2e80" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff":
            cjk += 1
    other = len(text) - cjk
    return int(cjk * 1.2) + other // 3 + 1


def plan_batches(
    texts: list[str],
    max_items: int = MAX_BATCH_ITEMS,
    max_tokens: int = MAX_BATCH_TOKENS,
) -> list[list[str]]:
    """按「条数 + 估算 token」双约束把文本切成批次。

    单条文本即使本身就超过 max_tokens 也会独占一批（交给服务端返回真实错误，
    而不是在这里静默丢弃）。
    """
    batches: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0
    for text in texts:
        n = estimate_tokens(text)
        if current and (len(current) >= max_items or current_tokens + n > max_tokens):
            batches.append(current)
            current, current_tokens = [], 0
        current.append(text)
        current_tokens += n
    if current:
        batches.append(current)
    return batches


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
        """对一批文本做 embedding。

        内部按 plan_batches 切分后依次请求，返回顺序与输入一致。
        调用方可以直接传入整批（哪怕几百条），不需要自己分批。
        """
        if not texts:
            return []
        vectors: list[list[float]] = []
        batches = plan_batches(texts)
        for batch in batches:
            vectors.extend(await self._embed_batch(batch))
        if self._dim is None and vectors:
            self._dim = len(vectors[0])
        return vectors

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        """发一个批次的请求。

        服务端若因批量过大而拒绝，对半折分重试——不同服务商、甚至同一服务商
        不同入参长度下的上限都不一样，硬编码单一条数不可靠。折分到单条仍然
        失败时，才当作真实错误上抛。
        """
        try:
            resp = await self._client.embeddings.create(
                input=batch,
                model=self._model,
            )
            return [d.embedding for d in resp.data]
        except Exception as e:  # 网络 / 鉴权 / 限流 / 批量过大 统一在这里分流
            if len(batch) > 1 and _BATCH_TOO_LARGE_RE.search(str(e)):
                mid = len(batch) // 2
                log.debug(
                    "embedding 批次（%d 条）被服务端拒绝，折半重试：%s",
                    len(batch), str(e)[:140],
                )
                head = await self._embed_batch(batch[:mid])
                tail = await self._embed_batch(batch[mid:])
                return head + tail
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
    # 名字不像 embedding 模型 → 说明这里很可能拿到的是聊天 provider（配置里
    # 的 embedding_provider 缺失或被忽略）。默认值只对 OpenAI 及其同构服务有效，
    # 对上其他厂商会直接 404，所以这里留一条 warning，别让问题静默。
    log.warning(
        "embedding 模型名 %r 不像 embedding 模型，回退到 text-embedding-3-small"
        "（base_url=%s）。如果 404，请在配置里补 embedding_provider。",
        chat_model, base_url,
    )
    return "text-embedding-3-small"

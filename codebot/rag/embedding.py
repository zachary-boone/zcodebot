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
import re
from abc import ABC, abstractmethod
from typing import Any

from codebot.config import ProviderConfig

log = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """embedding 调用失败。上层应捕获并降级。"""


# ---------------------------------------------------------------------------
# 分批参数
# ---------------------------------------------------------------------------

# 条数上限：主流服务端普遍限制在 10~25 条/请求，取 20 留余量。
MAX_BATCH_ITEMS = 20

# 估算 token 上限：上限同时受「条数」和「总 token」两个约束，
# 只按条数切分仍可能被拒，所以再加一道 token 预算（保守值）。
MAX_BATCH_TOKENS = 5000

# 仅用于识别「批量过大」这一类可折半重试的错误。
# 鉴权 / 网络错误绝不能匹配到这里，否则会把一个可重试错误拖成 N 次半量请求。
_BATCH_TOO_LARGE_RE = re.compile(
    r"batch\s*size|too\s*large|too\s*many|exceeds?\b|maximum\s*batch",
    re.IGNORECASE,
)


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数。

    中文约 1 token/字、其他约 3 字符/token，另留 20% 余量。
    宁可高估——高估只是批次更小，低估会让整批被服务端拒绝。
    """
    cjk = sum(
        1 for ch in text
        if "\u2e80" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff"
    )
    return int(cjk * 1.2) + (len(text) - cjk) // 3 + 1


def plan_batches(
    texts: list[str],
    max_items: int = MAX_BATCH_ITEMS,
    max_tokens: int = MAX_BATCH_TOKENS,
) -> list[list[str]]:
    """把待 embedding 的文本切成批次：同时受条数与 token 预算约束。

    保证：顺序不变、不丢文本、不产生空批次。
    单条文本自身超出 token 预算时仍单独成批（由服务端决定是否接受），
    绝不静默丢弃。
    """
    batches: list[list[str]] = []
    cur: list[str] = []
    cur_tokens = 0
    for t in texts:
        n = estimate_tokens(t)
        if cur and (len(cur) >= max_items or cur_tokens + n > max_tokens):
            batches.append(cur)
            cur, cur_tokens = [], 0
        cur.append(t)
        cur_tokens += n
    if cur:
        batches.append(cur)
    return batches


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
        """分批 embedding。

        单次请求超过服务端批量上限时会被**整批**拒绝（实测 DashScope 上限
        同时受条数与总 token 约束，且提示值会变），所以先按预算切分再逐批提交。
        """
        if not texts:
            return []
        vectors: list[list[float]] = []
        for batch in plan_batches(texts):
            vectors.extend(await self._embed_batch(batch))
        if self._dim is None and vectors:
            self._dim = len(vectors[0])
        return vectors

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        """提交单个批次；服务端因批量过大而拒绝时对半折分。

        折半**只在错误特征匹配 _BATCH_TOO_LARGE_RE 时**触发——鉴权 / 网络
        错误必须立刻上抛，否则一个可重试错误会被拖成 N 次半量请求。
        折到单条仍失败，才当真实错误上抛。
        """
        try:
            resp = await self._client.embeddings.create(
                input=batch,
                model=self._model,
            )
            return [d.embedding for d in resp.data]
        except Exception as e:
            if len(batch) > 1 and _BATCH_TOO_LARGE_RE.search(str(e)):
                mid = len(batch) // 2
                log.debug(
                    "embedding 批次（%d 条）被拒，折半重试：%s",
                    len(batch), str(e)[:140],
                )
                return (
                    await self._embed_batch(batch[:mid])
                    + await self._embed_batch(batch[mid:])
                )
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
    # 默认走 OpenAI 标准模型（兼容服务大多也支持）。
    # 这里必须留一条 warning：走到这个分支通常意味着调用方把「聊天 provider」
    # 当成 embedding provider 传了进来（模型名不是 embedding 模型），随后请求
    # 会打到聊天厂商的 base_url 上并返回 404，而且被上层静默降级成"没命中"。
    # 把两个关键线索打出来，便于定位。
    log.warning(
        "embedding 模型名 %r 不像 embedding 模型，回退到默认 %r。"
        "请检查是否误把聊天 provider 当成了 embedding provider"
        "（base_url=%s）",
        chat_model, "text-embedding-3-small", base_url,
    )
    return "text-embedding-3-small"

"""轻量重排（第三期 RAG）。

两阶段检索思想：
  - 召回阶段（粗排）：双塔模型（embedding）快速召回 Top-K，保证召回率
  - 重排阶段（精排）：用更强的模型对召回结果精排，提升精确率

业界用 CrossEncoder 做重排（如 sentence-transformers 的 cross-encoder），
但它需要额外重依赖（torch + 模型下载）。本项目定位轻量，所以这里用
"embedding 余弦相似度"做轻量重排——演示两阶段检索思想，不引重依赖。

原理：召回阶段用 Qdrant 批量检索（快），重排阶段对 query 和每个候选
重新算一次余弦相似度（和召回用的是同一组向量，但可以在这里加入
query 改写、上下文加权等逻辑，作为进阶扩展点）。

进阶路线（文档里提过）：将来想更强，可接入 CrossEncoder：
    from sentence_transformers import CrossEncoder
    model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
    scores = model.predict([(query, doc) for doc in docs])
"""

from __future__ import annotations

import logging
from typing import Any

from codebot.rag.embedding import EmbeddingProvider, EmbeddingError, cosine_similarity

log = logging.getLogger(__name__)


async def embed_rerank(
    query: str,
    candidates: list[dict[str, Any]],
    embedder: EmbeddingProvider,
    content_key: str = "code",
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """基于 embedding 余弦相似度的轻量重排。

    candidates: 召回阶段的结果列表，每个 dict 里有 content_key 字段（默认 "code"）
    返回：按重排分数降序的前 top_k 个，附带 "rerank_score" 字段

    失败时原样返回 candidates（不阻塞，重排是优化不是依赖）。
    """
    if not embedder.is_available() or not candidates:
        return candidates[:top_k]

    # 提取每个候选的文本用于重排
    texts = [c.get(content_key, "") or c.get("code", "") or "" for c in candidates]

    try:
        # query 和所有候选一起 embedding（batch，省 API 调用）
        all_texts = [query] + texts
        vectors = await embedder.embed(all_texts)
        q_vec = vectors[0]
        cand_vecs = vectors[1:]
    except EmbeddingError as e:
        log.warning("重排 embedding 失败，返回原序: %s", e)
        return candidates[:top_k]

    scored: list[tuple[float, dict[str, Any]]] = []
    for vec, cand in zip(cand_vecs, candidates):
        score = cosine_similarity(q_vec, vec)
        cand_copy = dict(cand)
        cand_copy["rerank_score"] = score
        scored.append((score, cand_copy))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:top_k]]

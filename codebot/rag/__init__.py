"""RAG（检索增强生成）子系统。

按 docs/RAG优化方案.md 的分期路线图落地：
  - 第一期：记忆语义检索（memory/semantic_recall.py 复用本模块）
  - 第二期：语义 CodeSearch（tools/code_search.py + rag/qdrant_store.py）
  - 第三期：ToolSearch 语义增强 + 轻量重排（rag/reranker.py）

设计原则（与项目整体调性一致）：
  - 轻量：Qdrant 默认嵌入式、可选依赖放 [rag] 组，没装则降级到 Grep
  - 可插拔：通过 EmbeddingProvider 抽象屏蔽不同协议差异
  - 渐进式降级：embedding 服务挂了 / 未装依赖，全部优雅退化，不阻塞主流程
"""

from __future__ import annotations

from codebot.rag.embedding import (
    EmbeddingProvider,
    EmbeddingError,
    create_embedding_provider,
    cosine_similarity,
)

__all__ = [
    "EmbeddingProvider",
    "EmbeddingError",
    "create_embedding_provider",
    "cosine_similarity",
]

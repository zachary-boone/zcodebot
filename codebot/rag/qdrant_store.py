"""Qdrant 向量存储（第二期 RAG）。

Qdrant 选型的三个理由（详见 docs/RAG优化方案.md §8b）：
  1. payload 元数据过滤——能按 file/type 过滤，代码 RAG 刚需
  2. 稳定点 ID + upsert 做增量——改了的块覆盖，删了的块 delete_by_file
  3. COSINE 距离内置归一化——不用手算相似度

部署形态的关键决策（面试必问）：
  - 默认嵌入式：QdrantClient(path=".codebot/qdrant")，无需起服务，clone 即用
  - 配置留 qdrant_url 开关：填了连远程 Server，渐进式可伸缩
  - client API 完全一致，切换零代码改动

降级策略：
  - qdrant-client 未安装 → QdrantCodeStore 不可用，CodeSearch 降级到 Grep 提示
  - 任何 Qdrant 操作失败 → 抛 EmbeddingError，上层捕获降级
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from codebot.rag.chunker import CodeChunk
from codebot.rag.embedding import EmbeddingProvider, EmbeddingError

log = logging.getLogger(__name__)

COLLECTION = "code_chunks"


def _qdrant_available() -> bool:
    """qdrant-client 是否已安装。"""
    try:
        import qdrant_client  # noqa: F401
        return True
    except ImportError:
        return False


class QdrantCodeStore:
    """代码块向量存储。

    默认嵌入式（本地文件持久化），可选连远程 Server：
        store = QdrantCodeStore(embedder, path=".codebot/qdrant")         # 嵌入式
        store = QdrantCodeStore(embedder, url="http://localhost:6333")    # Server

    不可用时（依赖缺失/embedder 不可用）is_available() 返回 False，
    上层应降级（如 CodeSearch 回退到 Grep）。
    """

    def __init__(
        self,
        embedder: EmbeddingProvider,
        path: str = ".codebot/qdrant",
        url: str | None = None,
        dim: int = 1536,
    ) -> None:
        self._embedder = embedder
        self._dim = dim
        self._url = url
        self._path = path
        self._client: Any | None = None
        self._init_client()

    def _init_client(self) -> None:
        if not _qdrant_available():
            log.info("qdrant-client 未安装，QdrantCodeStore 不可用（将降级）")
            return
        if not self._embedder.is_available():
            log.info("embedder 不可用，QdrantCodeStore 不可用（将降级）")
            return
        try:
            from qdrant_client import QdrantClient, models
            if self._url:
                self._client = QdrantClient(url=self._url)
            else:
                self._client = QdrantClient(path=self._path)
            self._models = models
            self._ensure_collection()
        except Exception as e:
            log.warning("Qdrant 初始化失败，降级: %s", e)
            self._client = None

    def _ensure_collection(self) -> None:
        if self._client is None:
            return
        existing = [c.name for c in self._client.get_collections().collections]
        if COLLECTION not in existing:
            self._client.create_collection(
                collection_name=COLLECTION,
                vectors_config=self._models.VectorParams(
                    size=self._dim,
                    distance=self._models.Distance.COSINE,
                ),
            )

    def is_available(self) -> bool:
        """是否可用（依赖装了 + embedder 可用 + client 初始化成功）。"""
        return self._client is not None

    async def upsert_chunks(self, chunks: list[CodeChunk]) -> None:
        """写入/更新代码块。已存在的 ID 会被覆盖（upsert 语义）。

        先 embed 所有块，再批量 upsert。失败的块不影响其他块。
        """
        if not self.is_available() or not chunks:
            return

        try:
            vectors = await self._embedder.embed([c.code for c in chunks])
        except EmbeddingError as e:
            raise EmbeddingError(f"代码块 embedding 失败: {e}") from e

        # 维度对齐检查
        if vectors and len(vectors[0]) != self._dim:
            self._dim = len(vectors[0])
            # 维度变了需要重建 collection
            self._recreate_collection()

        points = [
            self._models.PointStruct(
                id=c.id,
                vector=vec,
                payload={
                    "file": c.file,
                    "name": c.name,
                    "type": c.type,
                    "start_line": c.start_line,
                    "end_line": c.end_line,
                    "code": c.code,
                    "content_hash": c.content_hash,
                },
            )
            for c, vec in zip(chunks, vectors)
        ]

        # 分批 upsert，避免单次过大
        batch_size = 64
        for i in range(0, len(points), batch_size):
            self._client.upsert(
                collection_name=COLLECTION,
                points=points[i : i + batch_size],
            )

    def delete_by_file(self, file_path: str) -> None:
        """删除某文件的所有块（文件被删/大改时用）。"""
        if not self.is_available():
            return
        self._client.delete(
            collection_name=COLLECTION,
            points_selector=self._models.FilterSelector(
                filter=self._models.Filter(
                    must=[
                        self._models.FieldCondition(
                            key="file",
                            match=self._models.MatchValue(value=file_path),
                        )
                    ]
                )
            ),
        )

    async def search(
        self,
        query_vec: list[float],
        top_k: int = 20,
        file_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """向量检索。返回 payload + score 的列表。

        file_filter：按文件路径前缀过滤（如 "tools/" 只搜 tools 目录下的块）。
        这是 Qdrant payload 过滤的用武之地——代码 RAG 的刚需。
        """
        if not self.is_available():
            return []

        query_filter = None
        if file_filter:
            query_filter = self._models.Filter(
                must=[
                    self._models.FieldCondition(
                        key="file",
                        match=self._models.MatchText(text=file_filter),
                    )
                ]
            )

        try:
            result = self._client.query_points(
                collection_name=COLLECTION,
                query=query_vec,
                limit=top_k,
                query_filter=query_filter,
                with_payload=True,
            )
            return [
                {**p.payload, "score": p.score, "id": p.id}
                for p in result.points
            ]
        except Exception as e:
            log.warning("Qdrant 检索失败: %s", e)
            return []

    def _recreate_collection(self) -> None:
        """维度变化时重建 collection。"""
        if not self.is_available():
            return
        try:
            self._client.delete_collection(COLLECTION)
        except Exception:
            pass
        self._ensure_collection()


class NullCodeStore:
    """不可用时的占位实现。所有操作都是空操作/空返回。"""

    def is_available(self) -> bool:
        return False

    async def upsert_chunks(self, chunks: list[CodeChunk]) -> None:
        return

    def delete_by_file(self, file_path: str) -> None:
        return

    async def search(
        self,
        query_vec: list[float],
        top_k: int = 20,
        file_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        return []


def create_code_store(
    embedder: EmbeddingProvider,
    project_root: str | Path,
    qdrant_url: str | None = None,
) -> QdrantCodeStore | NullCodeStore:
    """创建代码向量存储。

    优先用 Qdrant；依赖缺失/embedder 不可用时返回 NullCodeStore（上层降级）。
    """
    if not _qdrant_available() or not embedder.is_available():
        return NullCodeStore()

    path = str(Path(project_root) / ".codebot" / "qdrant")
    try:
        return QdrantCodeStore(embedder, path=path, url=qdrant_url)
    except Exception as e:
        log.warning("创建 QdrantCodeStore 失败，降级为 NullCodeStore: %s", e)
        return NullCodeStore()

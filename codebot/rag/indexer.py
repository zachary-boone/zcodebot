"""增量索引管理器（第二期 RAG）。

把 chunker + embedder + qdrant_store 串起来，实现"只重新索引变化的文件"。

增量判断（两级，和 semantic_recall 同思路）：
  - mtime 没变 → 肯定没改，跳过（快速 stat）
  - mtime 变了 → 算文件内容 hash 确认（避免 touch 误报）

变更处理：
  - 新文件 → 分块 + embedding + upsert
  - 内容变了的文件 → delete_by_file 清旧块 + 重新分块 upsert
  - 被删的文件 → delete_by_file

索引元数据持久化在 .codebot/rag/file_index.json，记录每个文件的 mtime + hash，
下次启动时增量更新。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from codebot.rag.chunker import CodeChunk, chunk_file, walk_indexable_files
from codebot.rag.embedding import EmbeddingError
from codebot.rag.qdrant_store import NullCodeStore, QdrantCodeStore

log = logging.getLogger(__name__)

INDEX_META_FILE = "rag/file_index.json"


def _bm25_available() -> bool:
    """rank-bm25 是否已装。我们用自己实现的 bm25.py，不依赖外部包，所以始终 True。
    保留这个钩子是为了将来想换 rank-bm25 库时不动调用方。"""
    return True


@dataclass
class FileIndexEntry:
    """单个文件的索引元数据。"""

    mtime: float
    content_hash: str
    chunk_ids: list[str] = field(default_factory=list)


class IncrementalIndexer:
    """增量索引管理器。

    用法：
        indexer = IncrementalIndexer(project_root, store)
        await indexer.rebuild_if_needed()  # 增量更新变化的文件
    """

    def __init__(
        self,
        project_root: str | Path,
        store: QdrantCodeStore | NullCodeStore,
    ) -> None:
        self._root = Path(project_root)
        self._store = store
        self._meta_path = self._root / ".codebot" / INDEX_META_FILE
        # file_path -> FileIndexEntry
        self._meta: dict[str, FileIndexEntry] = self._load_meta()
        # BM25 关键词索引（内存，每次启动从已索引文件重建）
        from codebot.rag.bm25 import BM25Index
        self._bm25 = BM25Index()
        self._bm25_dirty = True  # 是否需要重建 BM25

    def is_available(self) -> bool:
        return self._store.is_available()

    def _load_meta(self) -> dict[str, FileIndexEntry]:
        if not self._meta_path.is_file():
            return {}
        try:
            data = json.loads(self._meta_path.read_text(encoding="utf-8"))
            return {
                k: FileIndexEntry(
                    mtime=v["mtime"],
                    content_hash=v["content_hash"],
                    chunk_ids=v.get("chunk_ids", []),
                )
                for k, v in data.items()
            }
        except (json.JSONDecodeError, KeyError, OSError):
            return {}

    def _save_meta(self) -> None:
        self._meta_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            k: {
                "mtime": v.mtime,
                "content_hash": v.content_hash,
                "chunk_ids": v.chunk_ids,
            }
            for k, v in self._meta.items()
        }
        self._meta_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _file_hash(self, path: Path) -> str:
        """算文件内容 hash。"""
        try:
            return hashlib.md5(path.read_bytes()).hexdigest()
        except OSError:
            return ""

    def _needs_reindex(self, path: Path, rel: str) -> bool:
        """判断文件是否需要重新索引（mtime 粗筛 + hash 确认）。"""
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False

        entry = self._meta.get(rel)
        if entry is None:
            return True  # 新文件
        if entry.mtime != mtime:
            # mtime 变了，用 hash 确认内容是否真变
            new_hash = self._file_hash(path)
            if new_hash != entry.content_hash:
                return True
            # 只是 touch，更新 mtime 即可
            entry.mtime = mtime
            return False
        return False

    async def rebuild_if_needed(self) -> dict[str, int]:
        """增量更新索引。

        返回统计：{"indexed": N, "skipped": M, "deleted": K}
        indexed = 新建/更新的文件数
        skipped = 未变的文件数
        deleted = 被删的文件数
        """
        if not self.is_available():
            return {"indexed": 0, "skipped": 0, "deleted": 0}

        stats = {"indexed": 0, "skipped": 0, "deleted": 0}

        # 1. 扫描当前所有可索引文件
        current_files = {str(p.relative_to(self._root)).replace("\\", "/"): p
                         for p in walk_indexable_files(self._root)}

        # 2. 删除已不存在的文件的索引
        for rel in list(self._meta.keys()):
            if rel not in current_files:
                self._store.delete_by_file(rel)
                self._meta.pop(rel, None)
                stats["deleted"] += 1

        # 3. 增量索引新增/变化的文件
        to_index: list[tuple[str, Path]] = []
        for rel, path in current_files.items():
            if self._needs_reindex(path, rel):
                to_index.append((rel, path))
            else:
                stats["skipped"] += 1

        for rel, path in to_index:
            try:
                chunks = chunk_file(path, self._root)
                if not chunks:
                    continue

                # 先删旧块（文件内容变了，旧块可能过时）
                if rel in self._meta:
                    self._store.delete_by_file(rel)

                await self._store.upsert_chunks(chunks)

                # 同步加入 BM25 索引（doc_id, code）
                if self._bm25 is not None:
                    self._bm25.add_docs([(c.id, c.code) for c in chunks])

                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    mtime = 0.0

                self._meta[rel] = FileIndexEntry(
                    mtime=mtime,
                    content_hash=self._file_hash(path),
                    chunk_ids=[c.id for c in chunks],
                )
                stats["indexed"] += 1
            except EmbeddingError as e:
                log.warning("索引 %s 失败（embedding）: %s", rel, e)
            except Exception as e:
                log.warning("索引 %s 失败: %s", rel, e)

        self._save_meta()
        self._bm25_dirty = False
        return stats

    def get_stats(self) -> dict[str, int]:
        """返回当前索引概况。"""
        total_chunks = sum(len(e.chunk_ids) for e in self._meta.values())
        return {
            "files_indexed": len(self._meta),
            "total_chunks": total_chunks,
        }

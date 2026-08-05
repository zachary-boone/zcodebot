"""后台预热功能测试。

验证：
  - start_background_warmup 启动后台任务，不阻塞当前调用
  - is_warming 正确反映预热状态
  - wait_for_warmup 等预热完成
  - 预热失败不抛异常（静默降级）
  - 重复调 start_background_warmup 不重复启动
"""

from __future__ import annotations

import asyncio

import pytest

from codebot.rag.indexer import IncrementalIndexer
from codebot.rag.qdrant_store import NullCodeStore


class _MockStore:
    """可控的 mock store，让 rebuild_if_needed 可追踪。"""

    def __init__(self) -> None:
        self.is_available_val = True
        self.upsert_calls = 0
        self.delete_calls = 0

    def is_available(self) -> bool:
        return self.is_available_val

    async def upsert_chunks(self, chunks):
        self.upsert_calls += 1

    def delete_by_file(self, file_path: str) -> None:
        self.delete_calls += 1

    async def search(self, query_vec, top_k=20, file_filter=None):
        return []


class TestBackgroundWarmup:
    def test_not_warming_initially(self, tmp_path):
        """刚创建的 indexer 不在预热。"""
        indexer = IncrementalIndexer(tmp_path, _MockStore())
        assert indexer.is_warming() is False

    @pytest.mark.asyncio
    async def test_start_warmup_runs_in_background(self, tmp_path):
        """start_background_warmup 启动后台任务，不阻塞当前协程。"""
        indexer = IncrementalIndexer(tmp_path, _MockStore())
        indexer.start_background_warmup()

        # 启动后应该处于 warming 状态
        assert indexer.is_warming() is True

        # 等后台任务完成
        await indexer.wait_for_warmup()

        # 完成后不再 warming
        assert indexer.is_warming() is False

    @pytest.mark.asyncio
    async def test_wait_for_warmup_no_op_when_not_warming(self, tmp_path):
        """没有预热时 wait_for_warmup 立即返回。"""
        indexer = IncrementalIndexer(tmp_path, _MockStore())
        # 没启动预热，wait 应立即返回
        await indexer.wait_for_warmup()
        assert indexer.is_warming() is False

    @pytest.mark.asyncio
    async def test_duplicate_start_does_not_restart(self, tmp_path):
        """已在预热时，重复调 start_background_warmup 不重复启动。"""
        indexer = IncrementalIndexer(tmp_path, _MockStore())
        indexer.start_background_warmup()
        first_task = indexer._warm_task

        indexer.start_background_warmup()  # 应该不重启
        assert indexer._warm_task is first_task

        await indexer.wait_for_warmup()

    @pytest.mark.asyncio
    async def test_warmup_failure_is_silent(self, tmp_path):
        """rebuild 失败时预热静默降级，不抛异常。"""

        class FailingStore(_MockStore):
            async def upsert_chunks(self, chunks):
                raise RuntimeError("mock store failure")

        indexer = IncrementalIndexer(tmp_path, FailingStore())
        indexer.start_background_warmup()

        # 等完成——不应抛异常
        await indexer.wait_for_warmup()
        assert indexer.is_warming() is False

    @pytest.mark.asyncio
    async def test_start_warmup_when_unavailable_is_noop(self, tmp_path):
        """store 不可用时 start_background_warmup 是空操作。"""
        store = _MockStore()
        store.is_available_val = False
        indexer = IncrementalIndexer(tmp_path, store)

        indexer.start_background_warmup()
        assert indexer.is_warming() is False  # 没启动
        assert indexer._warm_task is None

    @pytest.mark.asyncio
    async def test_wait_for_warmup_swallows_failure(self, tmp_path):
        """wait_for_warmup 吞掉预热异常，不传播给调用方。"""

        class FailingStore(_MockStore):
            async def upsert_chunks(self, chunks):
                raise RuntimeError("boom")

        indexer = IncrementalIndexer(tmp_path, FailingStore())
        indexer.start_background_warmup()

        # wait 不应抛异常
        await indexer.wait_for_warmup()
        # 清理 _warm_task
        assert indexer._warm_task is None

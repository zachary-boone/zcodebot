"""P1 修复的单元测试。

  P1-4  ensure() 失败后的冷却（不再每条消息白跑一次注定失败的请求）
  P1-5  扫描上限与「喂给 LLM 的清单上限」解耦 + 两目录全局按 mtime 排序
  P1-6  ensure() 单次新索引条数上限，按 mtime 从新到旧优先
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from codebot.memory.recall import (
    MAX_MEMORY_FILES,
    MAX_SCAN_FILES,
    MemoryHeader,
    _SCAN_CACHE_TTL,
    _scan_cache,
    _select_relevant_memories,
    find_relevant_memories,
    scan_memory_files,
)
from codebot.memory.semantic_recall import (
    EMBED_FAILURE_COOLDOWN,
    SemanticMemoryIndex,
)
from codebot.rag.embedding import EmbeddingError


# ---------------------------------------------------------------------------
# 假组件
# ---------------------------------------------------------------------------


class RecordingEmbedder:
    """恒成功的假 embedder，记录每批条数。"""

    def __init__(self) -> None:
        self.batches: list[int] = []

    def is_available(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(len(texts))
        return [[1.0, 0.0] for _ in texts]

    async def embed_one(self, text: str) -> list[float]:
        return [1.0, 0.0]


class FailingEmbedder:
    """恒失败的假 embedder。"""

    def __init__(self) -> None:
        self.calls = 0

    def is_available(self) -> bool:
        return True

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        raise EmbeddingError("模拟 embedding 服务不可用")

    async def embed_one(self, text: str) -> list[float]:
        raise EmbeddingError("模拟 embedding 服务不可用")


def _make_memories(directory: Path, count: int, prefix: str = "m",
                   base_time: float | None = None) -> list[MemoryHeader]:
    """造 count 个真实记忆文件并返回 headers（mtime 递增，便于验证排序）。"""
    directory.mkdir(parents=True, exist_ok=True)
    base = base_time if base_time is not None else time.time()
    headers: list[MemoryHeader] = []
    for i in range(count):
        p = directory / f"{prefix}{i:04d}.md"
        p.write_text(
            f"---\nname: {prefix}{i}\ndescription: 第 {i} 条\ntype: reference\n---\n"
            f"这是第 {i} 条记忆正文。" * 3,
            encoding="utf-8",
        )
        ts = base + i
        import os
        os.utime(p, (ts, ts))
        headers.append(
            MemoryHeader(
                filename=p.name,
                file_path=str(p.resolve()),
                scope="project",
                mtime_ms=int(ts * 1000),
                description=f"第 {i} 条",
                type="reference",
            )
        )
    return headers


# ---------------------------------------------------------------------------
# P1-4：失败冷却
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_failure_enters_cooldown(tmp_path: Path) -> None:
    """ensure() 失败后 is_available() 必须变 False，上层才不会再白跑请求。"""
    headers = _make_memories(tmp_path, 3)
    idx = SemanticMemoryIndex(FailingEmbedder())

    assert idx.is_available() is True
    await idx.ensure(headers)
    assert idx.is_available() is False


@pytest.mark.asyncio
async def test_cooldown_zero_recovers_immediately(tmp_path: Path) -> None:
    headers = _make_memories(tmp_path, 3)
    idx = SemanticMemoryIndex(FailingEmbedder(), cooldown_seconds=0)

    await idx.ensure(headers)
    assert idx.is_available() is True


@pytest.mark.asyncio
async def test_in_cooldown_ensure_does_not_call_embedder(tmp_path: Path) -> None:
    """冷却期内 ensure() 应直接返回，不再触发新的失败请求。"""
    headers = _make_memories(tmp_path, 3)
    emb = FailingEmbedder()
    idx = SemanticMemoryIndex(emb)

    await idx.ensure(headers)
    assert emb.calls == 1

    await idx.ensure(headers)
    assert emb.calls == 1, "冷却期内不应再次调用 embedder"


@pytest.mark.asyncio
async def test_success_resets_cooldown(tmp_path: Path) -> None:
    headers = _make_memories(tmp_path, 3)
    idx = SemanticMemoryIndex(RecordingEmbedder())

    await idx.ensure(headers)
    assert idx.is_available() is True
    assert idx._degraded_until == 0.0


def test_default_cooldown_is_positive() -> None:
    assert EMBED_FAILURE_COOLDOWN > 0


# ---------------------------------------------------------------------------
# P1-5：扫描上限解耦
# ---------------------------------------------------------------------------


def test_scan_limit_decoupled_from_manifest_limit(tmp_path: Path) -> None:
    """limit=1000 时必须返回全部 250 条，而不是被 MAX_MEMORY_FILES 截断。"""
    _make_memories(tmp_path, 250)
    assert MAX_MEMORY_FILES == 200
    assert len(scan_memory_files(tmp_path, "project", limit=1000)) == 250


def test_scan_default_limit_is_manifest_limit(tmp_path: Path) -> None:
    """不传 limit 时保持向后兼容：仍是 MAX_MEMORY_FILES。"""
    _make_memories(tmp_path, 250)
    assert len(scan_memory_files(tmp_path, "project")) == MAX_MEMORY_FILES


def test_scan_cache_key_includes_limit(tmp_path: Path) -> None:
    """不同 limit 不能命中同一份缓存。"""
    _make_memories(tmp_path, 250)
    _scan_cache.clear()
    assert len(scan_memory_files(tmp_path, "project", limit=50)) == 50
    # 同一目录、不同 limit：若缓存 key 没带 limit，这里会错误地返回 50
    assert len(scan_memory_files(tmp_path, "project", limit=1000)) == 250
    assert len(scan_memory_files(tmp_path, "project", limit=50)) == 50


def test_max_scan_files_larger_than_manifest_limit() -> None:
    assert MAX_SCAN_FILES > MAX_MEMORY_FILES


@pytest.mark.asyncio
async def test_candidates_sorted_globally_by_mtime(tmp_path: Path) -> None:
    """user 目录整体较旧、project 目录整体较新时，project 的记忆必须排在前面。

    修复前是"两目录结果直接拼接"，user 会整体挤掉 project。
    """
    now = time.time()
    user_dir = tmp_path / "user"
    proj_dir = tmp_path / "project"
    _make_memories(user_dir, 5, prefix="u", base_time=now - 100_000)
    _make_memories(proj_dir, 5, prefix="p", base_time=now)

    captured: dict[str, str] = {}

    async def selector(system_prompt: str, user_message: str) -> str:
        captured["msg"] = user_message
        return '{"selected_memories": []}'

    await find_relevant_memories(
        query="任意", user_mem_dir=user_dir, project_mem_dir=proj_dir,
        recent_tools=None, already_surfaced=None, selector=selector,
    )

    rows = [l for l in captured["msg"].splitlines() if l.startswith("- ")]
    assert len(rows) == 10
    # 最新的 p0004 应该排在第一行
    assert "p0004.md" in rows[0], rows[0]


@pytest.mark.asyncio
async def test_manifest_truncated_to_max_memory_files(tmp_path: Path) -> None:
    """喂给 LLM 的清单行数必须恰为 MAX_MEMORY_FILES。"""
    headers = _make_memories(tmp_path, MAX_MEMORY_FILES + 30)

    captured: dict[str, str] = {}

    async def selector(system_prompt: str, user_message: str) -> str:
        captured["msg"] = user_message
        return '{"selected_memories": []}'

    await _select_relevant_memories("q", headers, None, selector)
    rows = [l for l in captured["msg"].splitlines() if l.startswith("- ")]
    assert len(rows) == MAX_MEMORY_FILES


@pytest.mark.asyncio
async def test_selector_cannot_return_filename_outside_manifest(tmp_path: Path) -> None:
    """valid_filenames 必须基于截断后的清单计算——清单外的文件名要被丢弃。"""
    headers = _make_memories(tmp_path, MAX_MEMORY_FILES + 30)
    outside = headers[-1].filename  # 被截断掉的那批

    async def selector(system_prompt: str, user_message: str) -> str:
        import json
        return json.dumps({"selected_memories": [outside]})

    assert await _select_relevant_memories("q", headers, None, selector) == []


# ---------------------------------------------------------------------------
# P1-6：单次索引条数上限
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_caps_batch_per_call(tmp_path: Path) -> None:
    """5 条 headers + 上限 2：第一次 2 条、第二次 4 条、第三次 5 条。"""
    headers = _make_memories(tmp_path, 5)
    emb = RecordingEmbedder()
    idx = SemanticMemoryIndex(emb, max_embed_per_ensure=2)

    await idx.ensure(headers)
    assert len(idx._index) == 2
    await idx.ensure(headers)
    assert len(idx._index) == 4
    await idx.ensure(headers)
    assert len(idx._index) == 5
    # 之后没有新内容，不应再发请求
    await idx.ensure(headers)
    assert len(idx._index) == 5


@pytest.mark.asyncio
async def test_ensure_indexes_newest_first(tmp_path: Path) -> None:
    """第一次进索引的必须是 mtime 最大的那两条。"""
    headers = _make_memories(tmp_path, 5)          # m0000 .. m0004，mtime 递增
    idx = SemanticMemoryIndex(RecordingEmbedder(), max_embed_per_ensure=2)

    await idx.ensure(headers)

    indexed = {Path(p).name for p in idx._index}
    assert indexed == {"m0004.md", "m0003.md"}, indexed


@pytest.mark.asyncio
async def test_ensure_batches_never_exceed_cap(tmp_path: Path) -> None:
    headers = _make_memories(tmp_path, 25)
    emb = RecordingEmbedder()
    idx = SemanticMemoryIndex(emb, max_embed_per_ensure=7)

    await idx.ensure(headers)
    assert emb.batches == [7]
    assert len(idx._index) == 7

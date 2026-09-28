# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from codebot.memory.semantic_recall import SemanticMemoryIndex


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 喂给 LLM 选择器的清单上限（也是 scan_memory_files 的默认 limit）。
MAX_MEMORY_FILES = 200

# 扫描阶段的候选上限。与 MAX_MEMORY_FILES 解耦：
# 候选集若在扫描阶段就被截断，排在后面的记忆连向量索引都进不去
#（实测 500 条记忆时候选只有 205 条，295 条永远不在召回范围内）。
MAX_SCAN_FILES = 2000

FRONTMATTER_MAX_LINES = 30
ENTRYPOINT_NAME = "MEMORY.md"
VALID_TYPES = {"user", "feedback", "project", "reference"}

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

SELECTOR_SYSTEM_PROMPT = (
    "You are selecting memories that will be useful to CodeBot as it processes "
    "a user's query. You will be given the user's query and a list of available "
    "memory files with their filenames and descriptions.\n\n"
    "Return a list of filenames for the memories that will clearly be useful to "
    "CodeBot as it processes the user's query (up to 5). Only include memories "
    "that you are certain will be helpful based on their name and description.\n"
    "- If you are unsure if a memory will be useful in processing the user's "
    "query, then do not include it in your list. Be selective and discerning.\n"
    "- If there are no memories in the list that would clearly be useful, feel "
    "free to return an empty list.\n"
    "- If a list of recently-used tools is provided, do not select memories "
    "that are usage reference or API documentation for those tools (CodeBot is "
    "already exercising them). DO still select memories containing warnings, "
    "gotchas, or known issues about those tools — active use is exactly when "
    "those matter.\n\n"
    'Respond with valid JSON only, no markdown, in this exact shape: '
    '{"selected_memories": ["filename1.md", "filename2.md"]}'
)

# Type alias for the side-query selector function.
SelectorFn = Callable[[str, str], Awaitable[str]]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MemoryHeader:
    filename: str      # path relative to memory_dir
    file_path: str     # absolute path
    scope: str         # "user" or "project"
    mtime_ms: int      # modification time, ms since epoch
    description: str   # frontmatter description; "" if absent
    type: str          # frontmatter type; "" if unrecognized


@dataclass
class RelevantMemory:
    path: str
    mtime_ms: int


# ---------------------------------------------------------------------------
# Memory age helpers
# ---------------------------------------------------------------------------

def memory_age_days(mtime_ms: int) -> int:
    """Floor-rounded days since mtime. 0 for today, 1 for yesterday, etc."""
    d = (int(time.time() * 1000) - mtime_ms) // 86_400_000
    return max(d, 0)


def memory_age(mtime_ms: int) -> str:
    """Human-readable age: 'today', 'yesterday', or 'N days ago'."""
    d = memory_age_days(mtime_ms)
    if d == 0:
        return "today"
    if d == 1:
        return "yesterday"
    return f"{d} days ago"


def memory_freshness_text(mtime_ms: int) -> str:
    """Staleness warning for memories older than 1 day. Returns '' for fresh."""
    d = memory_age_days(mtime_ms)
    if d <= 1:
        return ""
    return (
        f"This memory is {d} days old. "
        "Memories are point-in-time observations, not live state — "
        "claims about code behavior or file:line citations may be outdated. "
        "Verify against current code before asserting as fact."
    )


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def parse_frontmatter(content: str) -> dict[str, str]:
    """Extract name/description/type from YAML-ish frontmatter.

    Only the three known fields are read; everything else is ignored.
    Files without frontmatter return empty fields.
    """
    m = FRONTMATTER_RE.match(content)
    if not m:
        return {"name": "", "description": "", "type": ""}

    block = m.group(1)
    result: dict[str, str] = {"name": "", "description": "", "type": ""}
    for line in block.split("\n"):
        colon = line.find(":")
        if colon < 0:
            continue
        key = line[:colon].strip()
        val = line[colon + 1 :].strip()
        # Strip quotes.
        if len(val) >= 2 and (
            (val.startswith('"') and val.endswith('"'))
            or (val.startswith("'") and val.endswith("'"))
        ):
            val = val[1:-1]
        if key == "name":
            result["name"] = val
        elif key == "description":
            result["description"] = val
        elif key == "type":
            if val in VALID_TYPES:
                result["type"] = val
    return result


# ---------------------------------------------------------------------------
# Scanning (with mtime-based cache)
# ---------------------------------------------------------------------------

# 缓存：key=(str(memory_dir), scope), value=(dir_mtime, results, timestamp)
_scan_cache: dict[tuple[str, str], tuple[float, list[MemoryHeader], float]] = {}
_SCAN_CACHE_TTL = 60.0  # 秒，兜底过期时间


def scan_memory_files(
    memory_dir: Path, scope: str, limit: int | None = None
) -> list[MemoryHeader]:
    """Walk memory_dir for .md files (excluding MEMORY.md), read frontmatter
    from each, and return a header list sorted newest-first, capped at `limit`.

    limit 默认 MAX_MEMORY_FILES（保持向后兼容）。语义检索路径下调用方会传
    MAX_SCAN_FILES，让候选集不再被「喂给 LLM 的清单长度」误伤。

    结果按目录 mtime 缓存：目录未变化时直接返回缓存，避免重复扫描。
    cache key 必须带上 limit，否则不同 limit 会命中同一份缓存。
    """
    if not memory_dir.is_dir():
        return []
    if limit is None:
        limit = MAX_MEMORY_FILES

    cache_key = (str(memory_dir), scope, limit)
    now = time.time()

    # 检查缓存：目录 mtime 未变且未过 TTL → 直接返回
    try:
        dir_mtime = memory_dir.stat().st_mtime
    except OSError:
        dir_mtime = 0.0

    cached = _scan_cache.get(cache_key)
    if cached is not None:
        cached_mtime, cached_results, cached_time = cached
        if cached_mtime == dir_mtime and (now - cached_time) < _SCAN_CACHE_TTL:
            return cached_results

    md_files: list[Path] = []
    try:
        for fp in memory_dir.rglob("*.md"):
            if fp.is_file() and fp.name != ENTRYPOINT_NAME:
                md_files.append(fp)
    except OSError:
        return []

    results: list[MemoryHeader] = []
    for fp in md_files:
        hdr = _read_memory_header(fp, memory_dir, scope)
        if hdr is not None:
            results.append(hdr)

    # Sort newest-first.
    results.sort(key=lambda h: h.mtime_ms, reverse=True)
    if len(results) > limit:
        results = results[:limit]

    # 更新缓存
    _scan_cache[cache_key] = (dir_mtime, results, now)
    return results


def _read_memory_header(
    file_path: Path, memory_dir: Path, scope: str
) -> MemoryHeader | None:
    try:
        mtime_ms = int(file_path.stat().st_mtime * 1000)
    except OSError:
        return None

    # Read first FRONTMATTER_MAX_LINES for frontmatter parsing.
    try:
        lines: list[str] = []
        with file_path.open(encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= FRONTMATTER_MAX_LINES:
                    break
                lines.append(line)
        content = "".join(lines)
    except OSError:
        return None

    fm = parse_frontmatter(content)
    try:
        rel = str(file_path.relative_to(memory_dir))
    except ValueError:
        rel = file_path.name

    return MemoryHeader(
        filename=rel,
        file_path=str(file_path.resolve()),
        scope=scope,
        mtime_ms=mtime_ms,
        description=fm["description"],
        type=fm["type"],
    )


# ---------------------------------------------------------------------------
# Manifest formatting
# ---------------------------------------------------------------------------

def format_memory_manifest(memories: list[MemoryHeader]) -> str:
    """Format memory headers as a text manifest for the selector prompt."""
    if not memories:
        return ""
    lines: list[str] = []
    for m in memories:
        scope_tag = f"[{m.scope}-scope] " if m.scope else ""
        type_tag = f"[{m.type}] " if m.type else ""
        ts = datetime.fromtimestamp(
            m.mtime_ms / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S.") + f"{m.mtime_ms % 1000:03d}Z"
        path = m.file_path if m.file_path else m.filename
        if m.description:
            lines.append(f"- {scope_tag}{type_tag}{path} ({ts}): {m.description}")
        else:
            lines.append(f"- {scope_tag}{type_tag}{path} ({ts})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Find relevant memories
# ---------------------------------------------------------------------------

async def find_relevant_memories(
    query: str,
    user_mem_dir: Path | None,
    project_mem_dir: Path | None,
    recent_tools: list[str] | None,
    already_surfaced: set[str] | None,
    selector: SelectorFn,
    semantic_index: "SemanticMemoryIndex | None" = None,
) -> list[RelevantMemory]:
    """Scan both dirs, filter already-surfaced, ask selector to pick up to 5
    relevant filenames, and return the corresponding paths + mtimes.

    Selector failures are silent — recall is best-effort and must never block
    the main conversation.

    语义检索（第一期 RAG 优化）：
      若传入 semantic_index 且可用，优先走 embedding 相似度检索，
      省一次 LLM 调用且基于正文语义；不可用或失败则回退到 LLM 选择器。
    """
    # 语义索引可用时用更大的扫描上限；否则沿用原来较小的上限
    #（LLM 选择器的清单长度有限，扫太多只是浪费）。
    semantic_ready = semantic_index is not None and semantic_index.is_available()
    scan_limit = MAX_SCAN_FILES if semantic_ready else MAX_MEMORY_FILES

    all_headers: list[MemoryHeader] = []
    if user_mem_dir is not None:
        all_headers.extend(scan_memory_files(user_mem_dir, "user", limit=scan_limit))
    if project_mem_dir is not None:
        all_headers.extend(scan_memory_files(project_mem_dir, "project", limit=scan_limit))

    # 两个目录的结果直接拼接会让 user 整体挤掉 project——一旦清单被截断，
    # project 侧就永远进不了候选。这里按 mtime 做一次全局倒序。
    all_headers.sort(key=lambda h: h.mtime_ms, reverse=True)

    surfaced = already_surfaced or set()
    candidates = [m for m in all_headers if m.file_path not in surfaced]
    if not candidates:
        return []

    # 优先语义检索（embedding 可用时）
    if semantic_ready:
        try:
            await semantic_index.ensure(candidates)
            selected_paths = await semantic_index.search(query)
            if selected_paths:
                by_path = {m.file_path: m for m in candidates}
                result = [
                    RelevantMemory(path=p, mtime_ms=by_path[p].mtime_ms)
                    for p in selected_paths
                    if p in by_path
                ]
                if result:
                    return result
            # 语义检索没命中任何相关记忆 → 回退到 LLM 选择器
        except Exception:
            pass  # 任何失败都静默回退

    selected_filenames = await _select_relevant_memories(
        query, candidates, recent_tools, selector
    )

    # Build lookup from both file_path and filename to header.
    by_key: dict[str, MemoryHeader] = {}
    for m in candidates:
        by_key[m.file_path] = m
        by_key.setdefault(m.filename, m)

    result: list[RelevantMemory] = []
    for fn in selected_filenames:
        m = by_key.get(fn)
        if m is not None:
            result.append(RelevantMemory(path=m.file_path, mtime_ms=m.mtime_ms))
    return result


async def _select_relevant_memories(
    query: str,
    memories: list[MemoryHeader],
    recent_tools: list[str] | None,
    selector: SelectorFn,
) -> list[str]:
    """Format manifest, call selector, parse JSON, return valid filenames."""
    # 先截断到清单上限，再据此计算 valid_filenames——顺序不能反：
    # 若先算 valid_filenames 再截断，模型返回清单外的文件名时我们仍会当作合法选择。
    memories = memories[:MAX_MEMORY_FILES]
    valid_filenames = {m.filename for m in memories}

    manifest = format_memory_manifest(memories)

    tools_section = ""
    if recent_tools:
        tools_section = "\n\nRecently used tools: " + ", ".join(recent_tools)

    user_message = f"Query: {query}\n\nAvailable memories:\n{manifest}{tools_section}"

    try:
        raw = await selector(SELECTOR_SYSTEM_PROMPT, user_message)
    except Exception:
        return []

    clean = _extract_json_object(raw)
    if not clean:
        return []

    try:
        parsed = json.loads(clean)
        arr = parsed.get("selected_memories", [])
        if not isinstance(arr, list):
            return []
        return [f for f in arr if isinstance(f, str) and f in valid_filenames]
    except (json.JSONDecodeError, AttributeError):
        return []


def _extract_json_object(raw: str) -> str:
    """Return the first {...} substring found in raw. Tolerates markdown
    fences or prose around the JSON.
    """
    trimmed = raw.strip()
    if trimmed.startswith("{"):
        return trimmed
    start = trimmed.find("{")
    if start < 0:
        return ""
    end = trimmed.rfind("}")
    if end < start:
        return ""
    return trimmed[start : end + 1]


# ---------------------------------------------------------------------------
# Reminder rendering
# ---------------------------------------------------------------------------

def render_reminder(memories: list[RelevantMemory]) -> str:
    """Read each selected memory file's full content and format a single
    system-reminder body with freshness headers.
    """
    if not memories:
        return ""

    parts: list[str] = []
    parts.append("The following relevant memories from prior conversations may help:\n")
    for mem in memories:
        try:
            content = Path(mem.path).read_text(encoding="utf-8")
        except OSError:
            continue  # skip unreadable files
        basename = Path(mem.path).name
        parts.append(f"## Memory: {basename} (saved {memory_age(mem.mtime_ms)})\n")
        note = memory_freshness_text(mem.mtime_ms)
        if note:
            parts.append(note + "\n")
        parts.append(content + "\n\n---\n")
    return "\n".join(parts)

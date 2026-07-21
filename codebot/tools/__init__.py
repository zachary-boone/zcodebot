from __future__ import annotations

from typing import TYPE_CHECKING, Any

from codebot.tools.base import Tool

if TYPE_CHECKING:
    from codebot.cache import FileCache


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._disabled: set[str] = set()
        self._discovered: set[str] = set()

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)


    def is_enabled(self, name: str) -> bool:
        return name in self._tools and name not in self._disabled

    def enable(self, name: str) -> None:
        self._disabled.discard(name)


    def disable(self, name: str) -> None:
        if name in self._tools:
            self._disabled.add(name)

    def enable_all(self) -> None:
        self._disabled.clear()


    def mark_discovered(self, name: str) -> None:
        self._discovered.add(name)

    def is_discovered(self, name: str) -> bool:
        return name in self._discovered


    def get_deferred_tool_names(self) -> list[str]:
        return [
            name
            for name, tool in self._tools.items()
            if getattr(tool, "should_defer", False)
            and name not in self._discovered
            and name not in self._disabled
        ]

    def search_deferred(
        self, query: str, max_results: int, protocol: str = "anthropic"
    ) -> list[dict[str, Any]]:
        query_lower = query.lower()
        scored: list[tuple[int, str, Tool]] = []
        for name, tool in self._tools.items():
            if not getattr(tool, "should_defer", False):
                continue
            if name in self._disabled:
                continue
            score = 0
            name_lower = name.lower()
            desc_lower = (tool.description or "").lower()
            if query_lower in name_lower:
                score += 10
            if query_lower in desc_lower:
                score += 5
            for word in query_lower.split():
                if word in name_lower:
                    score += 3
                if word in desc_lower:
                    score += 1
            if score > 0:
                scored.append((score, name, tool))
        scored.sort(key=lambda x: x[0], reverse=True)
        results: list[dict[str, Any]] = []
        for _, _name, tool in scored[:max_results]:
            base = tool.get_schema()
            if protocol in ("openai", "openai-compat"):
                results.append({
                    "type": "function",
                    "name": base["name"],
                    "description": base["description"],
                    "parameters": base["input_schema"],
                })
            else:
                results.append(base)
        return results

    async def search_deferred_semantic(
        self,
        query: str,
        max_results: int,
        protocol: str = "anthropic",
        embedder: Any = None,
    ) -> list[dict[str, Any]]:
        """语义增强的工具搜索（第三期 RAG）。

        若传入 embedder 且可用，用 embedding 余弦相似度匹配工具；
        否则回退到关键词版 search_deferred（保留 fallback）。

        语义匹配的优势：用户搜 "build codebase" 能匹配到 "CodeSearch"
        （即使字面没有 build/codebase 关键词）。
        工具数量少时收益有限，这里更多是展示"RAG 思路复用"。
        """
        # embedder 不可用 → 回退关键词
        if embedder is None or not embedder.is_available():
            return self.search_deferred(query, max_results, protocol)

        # 收集所有延迟工具的 name+description
        deferred: list[tuple[str, Tool]] = []
        for name, tool in self._tools.items():
            if not getattr(tool, "should_defer", False):
                continue
            if name in self._disabled:
                continue
            deferred.append((name, tool))
        if not deferred:
            return []

        from codebot.rag.embedding import EmbeddingError, cosine_similarity

        try:
            texts = [f"{name}: {tool.description}" for name, tool in deferred]
            all_vecs = await embedder.embed([query] + texts)
            q_vec = all_vecs[0]
            cand_vecs = all_vecs[1:]
        except EmbeddingError:
            return self.search_deferred(query, max_results, protocol)

        scored: list[tuple[float, str, Tool]] = []
        for vec, (name, tool) in zip(cand_vecs, deferred):
            score = cosine_similarity(q_vec, vec)
            scored.append((score, name, tool))
        scored.sort(key=lambda x: x[0], reverse=True)

        results: list[dict[str, Any]] = []
        for _, _name, tool in scored[:max_results]:
            base = tool.get_schema()
            if protocol in ("openai", "openai-compat"):
                results.append({
                    "type": "function",
                    "name": base["name"],
                    "description": base["description"],
                    "parameters": base["input_schema"],
                })
            else:
                results.append(base)
        return results

    def find_deferred_by_names(
        self, names: list[str], protocol: str = "anthropic"
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for name in names:
            tool = self._tools.get(name)
            if tool is None:
                continue
            if not getattr(tool, "should_defer", False):
                continue
            base = tool.get_schema()
            if protocol in ("openai", "openai-compat"):
                results.append({
                    "type": "function",
                    "name": base["name"],
                    "description": base["description"],
                    "parameters": base["input_schema"],
                })
            else:
                results.append(base)
        return results

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())


    def get_all_schemas(self, protocol: str = "anthropic") -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for name, tool in self._tools.items():
            if name in self._disabled:
                continue
            if getattr(tool, "should_defer", False) and name not in self._discovered:
                continue
            base = tool.get_schema()
            if protocol in ("openai", "openai-compat"):
                schemas.append({
                    "type": "function",
                    "name": base["name"],
                    "description": base["description"],
                    "parameters": base["input_schema"],
                })
            else:
                schemas.append(base)
        return schemas


def create_default_registry(file_cache: FileCache | None = None, file_history: Any = None) -> ToolRegistry:
    from codebot.tools.bash import Bash
    from codebot.tools.edit_file import EditFile
    from codebot.tools.file_state_cache import FileStateCache
    from codebot.tools.glob import Glob
    from codebot.tools.grep import Grep
    from codebot.tools.read_file import ReadFile
    from codebot.tools.write_file import WriteFile

    file_state_cache = FileStateCache()

    registry = ToolRegistry()
    registry.register(ReadFile(file_cache=file_cache, file_state_cache=file_state_cache))
    registry.register(WriteFile(file_cache=file_cache, file_history=file_history, file_state_cache=file_state_cache))
    registry.register(EditFile(file_cache=file_cache, file_history=file_history, file_state_cache=file_state_cache))
    registry.register(Bash())
    registry.register(Glob())
    registry.register(Grep())

    # RAG 第二期：语义代码搜索（依赖未装时自动降级，不影响其他工具）
    from codebot.tools.code_search import CodeSearch
    registry.register(CodeSearch())  # 默认 indexer/embedder 为 None → 降级提示用 Grep

    return registry

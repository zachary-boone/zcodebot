"""语义代码搜索工具（第二期 RAG 主力）。

和 Grep 并列注册，给 Agent 提供"按意思找代码"的能力：
  - Grep：关键词/正则精确匹配（找变量名、精确字符串）
  - CodeSearch：语义搜索（"找做登录鉴权的代码"——代码可能叫 verify_token）

完整链路（详见 docs/RAG优化方案.md §6）：
  query → embedding → Qdrant 向量召回 Top-20 ─┐
       → BM25 关键词召回 Top-20 ──────────────┤→ RRF 融合 → Top-5 → 返回 LLM
                                                │

降级策略（符合项目 '渐进式降级' 哲学）：
  - RAG 依赖未装（qdrant-client/rank-bm25）→ 提示用 Grep
  - embedder 不可用 → 提示用 Grep
  - 索引未建立 → 提示用 Grep
  - 任何环节失败 → 提示用 Grep
  RAG 是增强，不是依赖，绝不阻塞主流程。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from codebot.tools.base import Tool, ToolResult

log = logging.getLogger(__name__)

# 默认每路召回数，融合后取 top_k
DEFAULT_RECALL = 20
DEFAULT_TOP_K = 5


class CodeSearchParams(BaseModel):
    query: str = Field(..., description="用自然语言描述你要找的代码功能，如'处理用户登录鉴权'")
    top_k: int = Field(DEFAULT_TOP_K, description="返回结果数量", ge=1, le=20)
    file_filter: str = Field("", description="按文件路径前缀过滤，如 'tools/' 只搜 tools 目录")


class CodeSearch(Tool):
    """语义代码搜索工具。

    需要注入 IncrementalIndexer（管理 Qdrant + BM25 索引）。
    不可用时 is_available() 返回 False，execute 返回降级提示。
    """

    name = "CodeSearch"
    description = (
        "用自然语言语义搜索代码库，适合'找做某件事的代码'（如'处理登录鉴权'）。"
        "按意思而非关键词匹配。需要精确关键词/正则匹配时改用 Grep。"
    )
    params_model = CodeSearchParams
    category = "read"
    is_concurrency_safe = True
    # 基础工具，初始就暴露（语义搜索是 Agent 的核心能力）
    should_defer = False

    def __init__(
        self,
        indexer: Any = None,
        embedder: Any = None,
        query_rewriter: Any = None,  # 新增：查询重写器
    ) -> None:
        """indexer: IncrementalIndexer（含 store + bm25）。
        embedder: EmbeddingProvider（用于 query 向量化）。
        query_rewriter: QueryRewriter（用于中文查询重写）。
        三者任一为 None 或不可用 → 工具降级。
        """
        self._indexer = indexer
        self._embedder = embedder
        self._rewriter = query_rewriter  # 新增

    def is_available(self) -> bool:
        """RAG 链路是否完整可用。"""
        return (
            self._indexer is not None
            and self._embedder is not None
            and self._indexer.is_available()
            and self._embedder.is_available()
        )

    async def execute(self, params: BaseModel) -> ToolResult:
        assert isinstance(params, CodeSearchParams)

        if not self.is_available():
            return self._fallback_message(params.query)

        try:
            from codebot.rag.fusion import reciprocal_rank_fusion
            from codebot.rag.embedding import EmbeddingError

            # 1. 等后台预热完成（如果启动时触发了预热且还没跑完）
            #    避免和预热任务并发 rebuild 冲突。
            await self._indexer.wait_for_warmup()

            # 2. 增量更新索引（兜底：预热没覆盖到的变化，或预热失败的情况）
            await self._indexer.rebuild_if_needed()

            # 3. 查询重写（新增：优化中文查询匹配英文代码）
            original_query = params.query
            bm25_query = params.query  # BM25使用重写后的查询
            
            if self._rewriter:
                try:
                    rewrite_result = await self._rewriter.rewrite(params.query)
                    bm25_query = rewrite_result.rewritten
                    
                    # 记录重写日志
                    if rewrite_result.method != "no_rewrite":
                        log.info(
                            "查询重写: '%s' -> '%s' (method=%s, cached=%s, %.1fms)",
                            original_query,
                            bm25_query,
                            rewrite_result.method,
                            rewrite_result.cached,
                            rewrite_result.rewrite_time_ms,
                        )
                except Exception as e:
                    log.warning("查询重写失败，使用原始查询: %s", e)
                    bm25_query = params.query

            # 4. query embedding（用原始查询，保留语义）
            try:
                q_vec = await self._embedder.embed_one(original_query)
            except EmbeddingError as e:
                log.warning("CodeSearch query embedding 失败: %s", e)
                return self._fallback_message(original_query)

            # 5. 向量召回（Qdrant）
            store = self._indexer._store  # noqa: SLF001 — 内部访问
            vec_hits = await store.search(
                q_vec,
                top_k=DEFAULT_RECALL,
                file_filter=params.file_filter or None,
            )
            vec_ids = [h["id"] for h in vec_hits]

            # 6. BM25 关键词召回（用重写后的查询）
            bm25_ids: list[str] = []
            bm25 = getattr(self._indexer, "_bm25", None)
            if bm25 and bm25.is_available():
                bm25_results = bm25.search(bm25_query, top_k=DEFAULT_RECALL)
                bm25_ids = [doc_id for doc_id, _ in bm25_results]

            # 7. RRF 融合
            if vec_ids and bm25_ids:
                fused_ids = reciprocal_rank_fusion(vec_ids, bm25_ids)
            elif vec_ids:
                fused_ids = vec_ids
            else:
                fused_ids = bm25_ids

            if not fused_ids:
                return ToolResult(
                    output=f'语义搜索未找到相关代码（query: "{original_query}"）。\n'
                    f"建议用 Grep 工具做关键词搜索。"
                )

            # 8. 取 top_k，组装输出（用向量召回的 payload）
            top_ids = fused_ids[: params.top_k]
            payload_by_id = {h["id"]: h for h in vec_hits}
            
            # 构建输出头
            header = f'语义搜索结果（query: "{original_query}"'
            if bm25_query != original_query:
                header += f', BM25: "{bm25_query}"'
            header += f'，融合 {len(fused_ids)} 条召回）：'
            
            lines = [header]
            
            for i, doc_id in enumerate(top_ids, 1):
                payload = payload_by_id.get(doc_id)
                if payload:
                    score = payload.get("score", 0.0)
                    lines.append(
                        f"  {i}. {payload['name']} @ {payload['file']}:"
                        f"{payload['start_line']}-{payload['end_line']} (score {score:.2f})"
                    )
                    # 附带代码片段（截断，避免输出过长）
                    code = payload.get("code", "")
                    if code:
                        snippet = code[:300].rstrip()
                        if len(code) > 300:
                            snippet += " ..."
                        lines.append(f"     {snippet.replace(chr(10), chr(10) + '     ')}")
                else:
                    lines.append(f"  {i}. {doc_id}（仅 BM25 命中，无向量 payload）")

            return ToolResult(output="\n".join(lines))

        except Exception as e:
            log.warning("CodeSearch 执行失败，降级: %s", e)
            return self._fallback_message(original_query)

    def _fallback_message(self, query: str) -> ToolResult:
        """降级提示：引导 LLM 改用 Grep。"""
        return ToolResult(
            output=(
                f"语义搜索不可用（RAG 依赖未安装或 embedding 未配置）。\n"
                f"请改用 Grep 工具对\"{query}\"做关键词搜索。"
                f"\n\n启用语义搜索：uv pip install -e '.[rag]' 并配置 OpenAI 兼容 provider。"
            )
        )

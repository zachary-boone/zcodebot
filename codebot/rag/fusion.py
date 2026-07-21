"""RRF 融合算法（第二期 RAG 混合检索的融合层）。

为什么用 RRF 而不是加权求和（详见 docs/rag-optimization-plan.md §6.3 决策二）：
  - 向量相似度分数（0-1）和 BM25 分数（无上界）量纲不同，直接加权难调参
  - RRF 只看排名，天然归一化，无需调参
  - 公式：score(doc) = sum(1 / (k + rank_i))，k 通常取 60

这是既有深度又好实现的点，面试必讲。
"""

from __future__ import annotations

# RRF 平滑常数，业界标准值。k 越大，排名差异的影响越平滑。
RRF_K = 60


def reciprocal_rank_fusion(
    *ranked_lists: list[str],
    k: int = RRF_K,
) -> list[str]:
    """倒数排名融合。

    输入：多个有序 doc_id 列表（每个列表按相关性降序）
    输出：融合后按 RRF 分数降序的 doc_id 列表

    公式：score(doc) = sum over lists: 1 / (k + rank_in_list)
    rank 从 1 开始（第 1 名得 1/(k+1)，第 2 名得 1/(k+2)）

    例子：
        vec_results = ["a", "b", "c"]
        kw_results = ["b", "a", "d"]
        fused = reciprocal_rank_fusion(vec_results, kw_results)
        # "b" 在两路都靠前，融合后排第一
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)

    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda x: -x[1])]


def fusion_with_scores(
    *ranked_lists: list[tuple[str, float]],
    k: int = RRF_K,
) -> list[tuple[str, float]]:
    """带分数的 RRF 融合（调试/展示用）。

    输入：多个 [(doc_id, score)] 列表（每个按 score 降序）
    输出：[(doc_id, rrf_score)] 按 RRF 分数降序
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, (doc_id, _) in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)

    return [(doc_id, score) for doc_id, score in
            sorted(scores.items(), key=lambda x: -x[1])]

# 阶段7：RAG 检索增强生成（已落地）

> **学习目标**：理解 RAG 在本项目里的三期落地——记忆语义检索、语义 CodeSearch、ToolSearch 语义增强 + 轻量重排。掌握 embedding、向量库、混合检索、RRF、增量索引等核心 RAG 技术点，并能向面试官完整讲清楚"在一个真实 Agent 项目里如何落地 RAG"。
> **预计时间**：2-3 天
> **前置要求**：完成阶段1-6；了解向量、余弦相似度、AST 概念、Python async/await
> **面试导向**：这是项目里最能体现"前沿技术落地能力"的模块。读完本章你能回答 RAG 相关的系统设计题、源码题、数学推导题。

---

## 目录

1. [为什么加 RAG](#1-为什么加-rag)
2. [RAG 基础理论（面试必懂）](#2-rag-基础理论面试必懂)
3. [第一期：记忆语义检索](#3-第一期记忆语义检索)
4. [第二期：语义 CodeSearch（主力）](#4-第二期语义-codesearch主力)
5. [第三期：ToolSearch 语义增强 + 轻量重排](#5-第三期toolsearch-语义增强--轻量重排)
6. [技术选型深度对比](#6-技术选型深度对比)
7. [降级策略与决策树](#7-降级策略与决策树)
8. [性能考量与调优](#8-性能考量与调优)
9. [常见误区与踩坑点](#9-常见误区与踩坑点)
10. [文件清单与代码导航](#10-文件清单与代码导航)
11. [面试高频点与深度回答](#11-面试高频点与深度回答)
12. [手撕代码题](#12-手撕代码题)
13. [学习路径与扩展阅读](#13-学习路径与扩展阅读)

---

## 1. 为什么加 RAG

### 1.1 痛点：词汇鸿沟

项目原本的代码检索全靠关键词/正则（Grep/Glob）。这有个致命短板——**词汇鸿沟**：

```
用户问："项目哪里做了登录鉴权？"
代码里实际叫：verify_token() / check_auth() / AuthMiddleware
Grep 搜 "登录" / "login" → 0 命中
```

关键词检索无法跨越"用户表达"和"代码命名"之间的语义鸿沟。语义检索（embedding）恰好能补——它匹配"意思"而不是"字面"。

### 1.2 为什么这个项目适合加 RAG（三个理由）

**理由一：痛点真实**。Agent 找代码全靠 LLM 主动发 Grep 关键词，词汇鸿沟导致召回率低。这不是硬造需求。

**理由二：切入点天然**。`memory/recall.py` 已有"检索 hook"骨架（`find_relevant_memories` 接受 `SelectorFn` 回调），换 embedding 几乎原地替换。分层架构 + 注册表模式让新增工具零侵入。

**理由三：面试价值高**。RAG 是 2024-2025 后端/AI 岗必考。在真实 Agent 项目里落地 RAG，比"跟教程搭 demo"含金量高一个量级——能讲清楚 chunking、混合检索、增量索引等工程细节。

### 1.3 与同类产品的 RAG 实现对比

| 产品 | RAG 方式 | 我们的区别 |
|------|---------|-----------|
| GitHub Copilot | 闭源，推测用语义检索 + 上下文 | 我们开源可讲细节 |
| Cursor | `@codebase` 语义搜索 | 类似，但我们用混合检索（向量+BM25） |
| Claude Code | 暂无代码 RAG | 我们的差异化亮点 |
| Aider | 仅关键词搜索 | 我们语义+关键词双路 |

### 1.4 三个切入点

| 切入点 | 改什么 | 核心 RAG 技术点 | 难度 |
|--------|--------|----------------|------|
| ① 记忆语义检索 | `memory/recall.py` | embedding + 余弦相似度 + Top-K | ⭐⭐ |
| ② 语义 CodeSearch | 新增 `tools/code_search.py` | AST 分块 + Qdrant + BM25 + RRF + 增量索引 | ⭐⭐⭐⭐ |
| ③ ToolSearch 语义增强 | `tools/impl/tool_search.py` | 小规模语义匹配 + 轻量重排 | ⭐ |

---

## 2. RAG 基础理论（面试必懂）

### 2.1 RAG 是什么

RAG = Retrieval-Augmented Generation。核心：**让 LLM 生成前，先检索相关内容塞进 prompt**。

解决两个问题：
1. **知识时效性**：LLM 训练数据有截止时间，RAG 让它用最新/私有知识
2. **上下文有限**：不能把整个代码库塞进 prompt（20 万 token 限制），RAG 只检索相关片段

### 2.2 完整链路

```
【索引阶段（离线）】
  代码 → AST 分块 → embedding → Qdrant 向量库

【检索阶段（在线）】
  query → embedding → 向量召回 Top-20 ─┐
       → BM25 关键词召回 Top-20 ──────┤→ RRF 融合 → 重排 → Top-5 → LLM
                                       ┘
```

### 2.3 Embedding 原理

Embedding 把文本映射成高维向量（如 1536 维），**语义相近的文本向量方向也相近**。

```
"登录鉴权"     → [0.12, -0.34, 0.56, ..., 0.78]  (1536维)
"verify_token" → [0.11, -0.32, 0.55, ..., 0.77]  (方向相近)
"数据库连接"   → [-0.45, 0.23, -0.11, ..., 0.02] (方向不同)
```

**为什么能工作？** Embedding 模型在海量语料上训练，学会了"登录"和"verify_token"在语义空间中位置相近。这是"分布式表示"——概念被编码到向量空间的几何位置中。

**两种 embedding 架构**：
- **双塔模型**（如 text-embedding-3-small）：query 和文档**独立**编码，可预计算文档向量，检索快。本项目用这种。
- **交叉编码器**（CrossEncoder）：query 和文档**拼接后**一起编码，精度更高但不能预计算，只用于重排。详见 §5.2。

### 2.4 余弦相似度数学推导

余弦相似度衡量两个向量的"方向"相近程度：

```
cos(A, B) = (A · B) / (|A| × |B|)
          = Σ(Ai × Bi) / (√Σ(Ai²) × √Σ(Bi²))
```

- 值域 [-1, 1]，1 表示方向完全相同，0 表示正交（不相关），-1 表示反向
- **为什么用余弦不用欧氏距离？** 余弦只看方向不看长度，对文本长度不敏感。一篇 100 字和一篇 1000 字的"登录鉴权"文档，欧氏距离很远但余弦接近 1。
- **归一化后等价于点积**：如果向量已归一化（|A|=|B|=1），则 `cos(A,B) = A·B`。Qdrant 配置 `Distance.COSINE` 后内部自动归一化，检索时直接点积，更快。

本项目实现（`rag/embedding.py` 第 60-78 行）：

```python
def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = 0.0; na = 0.0; nb = 0.0
    for x, y in zip(a, b):
        dot += x * y; na += x * x; nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))
```

### 2.5 BM25 公式推导

BM25（Best Matching 25）是经典的关键词检索算法，基于 TF-IDF 改进：

```
score(q, d) = Σ IDF(qi) × [ f(qi,d) × (k1+1) ] / [ f(qi,d) + k1 × (1 - b + b × |d|/avgdl) ]
```

- `f(qi,d)`：词 qi 在文档 d 中的词频（TF）
- `|d|`：文档长度，`avgdl`：平均文档长度
- `k1=1.5`：词频饱和控制（防止一个词出现 100 次就无限加分）
- `b=0.75`：文档长度归一化强度（b=1 完全归一化，b=0 不归一化）

**IDF 公式**（逆文档频率，衡量词的区分度）：

```
IDF(qi) = ln( (N - df(qi) + 0.5) / (df(qi) + 0.5) + 1 )
```

- `N`：文档总数，`df(qi)`：包含 qi 的文档数
- "+1" 平滑保证 IDF 非负
- 常见词（如 "the"）df 大 → IDF 小；罕见词 df 小 → IDF 大

**为什么 BM25 比 TF-IDF 好？** BM25 的 TF 项有饱和效应（k1 控制），不会因为一个词出现 100 次就无限加分；还有文档长度归一化（b 控制），防止长文档天然占优。

本项目实现（`rag/bm25.py` 第 108-135 行）：

```python
def _score(self, doc, query_tokens):
    score = 0.0
    for t in query_tokens:
        if t not in doc.token_freqs: continue
        f = doc.token_freqs[t]
        df = self._doc_freqs.get(t, 0)
        idf = math.log((self._corpus_size - df + 0.5) / (df + 0.5) + 1)
        tf = (f * (self._k1 + 1)) / (
            f + self._k1 * (1 - self._b + self._b * doc.length / self._avg_length)
        )
        score += idf * tf
    return score
```

### 2.6 RRF 融合公式推导

RRF（Reciprocal Rank Fusion，倒数排名融合）把多路召回结果按排名融合：

```
score(d) = Σ 1 / (k + rank_i(d))
```

- `rank_i(d)`：文档 d 在第 i 路结果中的排名（第 1 名 rank=1）
- `k=60`：平滑常数，k 越大排名差异影响越平滑

**例子**：文档 A 在向量召回排第 2，BM25 排第 1：
```
score(A) = 1/(60+2) + 1/(60+1) = 0.0161 + 0.0164 = 0.0325
```
文档 B 在向量召回排第 1，BM25 未命中：
```
score(B) = 1/(60+1) + 0 = 0.0164
```
A 排在 B 前面——两路都靠前比单路第一更好。

**为什么用 RRF 不用加权求和？**
- 向量分数（0-1）和 BM25 分数（无上界）**量纲不同**，直接加权难调参
- RRF **只看排名**，天然归一化，无需调参
- 鲁棒：某一路分数异常不会影响融合结果

本项目实现（`rag/fusion.py` 第 17-40 行）：

```python
def reciprocal_rank_fusion(*ranked_lists, k=RRF_K):
    scores = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda x: -x[1])]
```

### 2.7 两阶段检索

业界标准的 RAG 检索范式——召回（粗排）+ 重排（精排）：

| 阶段 | 模型 | 特点 | 目标 |
|------|------|------|------|
| **召回** | 双塔模型（embedding） | 快，可预计算，全库扫描 | 高召回率（不漏相关文档） |
| **重排** | 交叉编码器（CrossEncoder） | 慢，精度高，只排 Top-K | 高精确率（排对顺序） |

为什么两阶段？全库用 CrossEncoder 太慢（N 次拼接编码），全用 embedding 精度不够。先用 embedding 召回 Top-20（快），再用 CrossEncoder 精排到 Top-5（准）。

---

## 3. 第一期：记忆语义检索

### 3.1 现状与改造

`memory/recall.py` 原本用 LLM 选择器挑记忆（`find_relevant_memories` 第 238 行）：每次都额外调一次 LLM，有延迟和成本，且只看 frontmatter description（不看正文）。

改造：用 embedding 余弦相似度替代——省一次 LLM 调用，基于正文语义，可设阈值过滤。

### 3.2 完整时序图

```
用户输入 query
    │
    ├─► app._prefetch_relevant_memories(query)
    │       │
    │       ├─► _get_semantic_memory_index(provider)  [懒加载]
    │       │       └─► create_embedding_provider(provider)
    │       │           └─► OpenAIEmbedding / NullEmbedding
    │       │
    │       └─► find_relevant_memories(..., semantic_index=index)
    │               │
    │               ├─► scan_memory_files() → 扫描所有 .md 记忆
    │               │
    │               ├─► semantic_index.is_available()? ──No──► 回退 LLM 选择器
    │               │       │
    │               │      Yes
    │               │       │
    │               ├─► index.ensure(candidates)  [增量更新]
    │               │       ├─► 遍历记忆，mtime 没变跳过
    │               │       ├─► mtime 变了 → 算 hash → 内容变了才 embed
    │               │       └─► 更新 _index 缓存
    │               │
    │               ├─► index.search(query)  [语义检索]
    │               │       ├─► query → embed_one() → q_vec
    │               │       ├─► 遍历 _index 算余弦相似度
    │               │       ├─► 过滤 score < 0.3
    │               │       └─► 取 Top-5
    │               │
    │               └─► 有结果? ──No──► 回退 LLM 选择器
    │                      │
    │                     Yes
    │                      └─► render_reminder() → 注入 system prompt
    │
    └─► Agent 主循环开始
```

### 3.3 核心代码（`memory/semantic_recall.py`）

**SemanticMemoryIndex**（第 57 行）有两个核心方法：

```python
class SemanticMemoryIndex:
    async def ensure(self, headers: list[MemoryHeader]) -> None:
        """增量更新索引：mtime 粗筛 + hash 确认，只重新 embed 变化的记忆。"""
        for h in headers:
            existing = self._index.get(h.file_path)
            if existing and existing.mtime_ms == h.mtime_ms:
                continue  # mtime 没变，跳过（快速 stat）
            text = _read_memory_body(h.file_path)
            content_hash = _hash_text(text)
            # mtime 变了但内容没变（如 touch），只更新 mtime
            if existing and existing.content_hash == content_hash:
                existing.mtime_ms = h.mtime_ms
                continue
            # 内容真变了 → 重新 embedding
            to_embed.append((h.file_path, text))
        if to_embed:
            vectors = await self._embedder.embed([t for _, t in to_embed])
            # 批量 embed，省 API 调用

    async def search(self, query: str) -> list[str]:
        """语义检索：query embedding → 余弦相似度 → Top-K + 阈值过滤。"""
        q_vec = await self._embedder.embed_one(query)
        scored = [(cosine_similarity(q_vec, mv.vector), path)
                  for path, mv in self._index.items()]
        scored.sort(reverse=True)
        return [path for score, path in scored[:self._top_k]
                if score >= self._threshold]  # 0.3 阈值
```

### 3.4 与 LLM 选择器的对比

| 维度 | LLM 选择器（原） | 语义检索（新） |
|------|----------------|--------------|
| 延迟 | 2-3 秒（调 LLM） | 100ms（向量计算） |
| 成本 | 每次消耗 token | 仅 embedding 费用（便宜 10x） |
| 依据 | 只看 description | 看正文全文 |
| 可解释 | 黑盒（LLM 决策） | 有相似度分数 |
| 离线 | 不可用（需 API） | 索引建好就能用 |

### 3.5 降级设计

- `NullEmbedding`（协议不支持/key 缺失）→ `is_available()` 返回 False → `find_relevant_memories` 回退 LLM 选择器
- embedding 调用失败 → 静默回退
- 记忆文件读失败 → 跳过该条
- 语义检索零命中 → 回退 LLM 选择器

**关键：recall.py 的 `find_relevant_memories` 新增 `semantic_index` 可选参数（第 251 行），向后兼容。** 不传或不可用时完全走原 LLM 选择器逻辑。

### 3.6 涉及技术点

- embedding API 调用（OpenAI 兼容协议，支持 DeepSeek/Qwen/Ollama）
- 余弦相似度（方向 > 长度，对文本长度不敏感）
- 相似度阈值过滤（0.3——经验值，平衡召回率和精确率）
- 增量索引（mtime + hash 两级判断，避免重复 embedding）

---

## 4. 第二期：语义 CodeSearch（主力）

**这是最值得讲的方案**——覆盖 RAG 完整链路，面试时能从头讲到尾。

### 4.1 完整架构图

```
┌─────────────────────────────────────────────────────────┐
│                    索引阶段（离线/增量）                    │
│                                                          │
│  walk_indexable_files()  ──►  chunk_file()  ──►  embed() │
│  (跳过 .git/.venv)           (AST 分块)        (批量)     │
│                                                          │
│         ┌──────────────────────────────────────┐         │
│         │        QdrantCodeStore               │         │
│         │  collection: code_chunks             │         │
│         │  ├─ payload: file/name/type/line     │         │
│         │  ├─ vector: embedding (COSINE)       │         │
│         │  └─ id: hash(file:name:start_line)   │         │
│         └──────────────────────────────────────┘         │
│                          ▲                               │
│                          │ upsert                         │
│         IncrementalIndexer (mtime+hash 增量)              │
│         BM25Index (内存, 同步维护)                         │
└─────────────────────────────────────────────────────────┘
                          │
──────────────────────────────────────────────────────────
                          │
┌─────────────────────────────────────────────────────────┐
│                    检索阶段（在线）                        │
│                                                          │
│  query ──► embed_one() ──► q_vec                        │
│                                  │                       │
│         ┌────────────────────────┼───────────────┐      │
│         │                        │               │      │
│    向量召回                  BM25 召回            │      │
│    Qdrant.search()         bm25.search()         │      │
│    Top-20                  Top-20               │      │
│         │                        │               │      │
│         └──────────┬─────────────┘               │      │
│                    │                             │      │
│               RRF 融合                            │      │
│           reciprocal_rank_fusion()               │      │
│                    │                             │      │
│               Top-5 → LLM                        │      │
│                   (可选重排)                      │      │
└─────────────────────────────────────────────────────────┘
```

### 4.2 代码分块（`rag/chunker.py`）

代码 RAG 和文档 RAG 的核心区别就在分块：

| 类型 | 分块方式 | 问题 |
|------|---------|------|
| 文档 RAG | 固定字符切（如 512 字符） | 代码会被切成两半，语义破碎 |
| **代码 RAG** | **AST 按函数/类切** | **保证每个 chunk 是完整语义单元** |

**Python AST 分块**（`chunker.py` 第 94-130 行）：

```python
def _chunk_python(source: str, rel_path: str) -> list[CodeChunk]:
    tree = ast.parse(source)
    chunks = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", start)
            code = "".join(lines[start - 1 : end])
            # 超长函数滑窗二次切分
            if end - start + 1 > MAX_CHUNK_LINES:  # 50 行
                sub_chunks = _sliding_sub_chunks(code, start, end, ...)
                chunks.extend(sub_chunks)
            else:
                chunks.append(CodeChunk(
                    file=rel_path, type=type(node).__name__, name=node.name,
                    start_line=start, end_line=end, code=code,
                    content_hash=hashlib.md5(code.encode()).hexdigest(),
                ))
    # 没有函数/类的文件（如配置脚本）→ 整体滑窗
    if not chunks:
        return _chunk_sliding(source, rel_path)
    return chunks
```

**分块参数**（`chunker.py` 第 31-35 行）：

```python
MAX_CHUNK_LINES = 50      # 单块最大行数，约 400-600 token
SLIDING_OVERLAP = 10      # 滑窗 overlap，保证边界语义连续
```

**为什么 50 行？** 太小（如 10 行）语义不完整；太大（如 200 行）向量稀释，相似度不准。50 行约等于一个中等函数，是代码块 embedding 的甜点区。

**滑窗二次切分**：超长函数（如 200 行的巨函数）按 `MAX_CHUNK_LINES=50` 切窗，步长 `50-10=40`，相邻窗口有 10 行 overlap——保证跨块时函数签名+前几行不丢。

**稳定点 ID**（`chunker.py` 第 68 行）：

```python
@property
def id(self) -> str:
    raw = f"{self.file}:{self.name}:{self.start_line}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()
```

**为什么稳定 ID 重要？** 文件改了内容但函数位置没变 → ID 不变 → Qdrant `upsert` 覆盖旧向量。这是增量索引的基础——不全量重建。

### 4.3 Qdrant 存储（`rag/qdrant_store.py`）

**QdrantCodeStore**（第 41 行）的核心方法：

```python
class QdrantCodeStore:
    def __init__(self, embedder, path=".codebot/qdrant", url=None, dim=1536):
        # 默认嵌入式（本地文件），url 填了连远程 Server
        if url:
            self._client = QdrantClient(url=url)
        else:
            self._client = QdrantClient(path=path)
        self._ensure_collection()  # COSINE 距离

    async def upsert_chunks(self, chunks):
        vectors = await self._embedder.embed([c.code for c in chunks])
        points = [PointStruct(
            id=c.id,           # 稳定点 ID
            vector=vec,
            payload={          # 元数据，用于过滤
                "file": c.file, "name": c.name, "type": c.type,
                "start_line": c.start_line, "end_line": c.end_line,
                "code": c.code, "content_hash": c.content_hash,
            },
        ) for c, vec in zip(chunks, vectors)]
        # 分批 upsert（batch_size=64），避免单次过大
        for i in range(0, len(points), 64):
            self._client.upsert(COLLECTION, points=points[i:i+64])

    async def search(self, query_vec, top_k=20, file_filter=None):
        # file_filter 用 Qdrant payload 过滤——代码 RAG 刚需
        query_filter = Filter(must=[FieldCondition(
            key="file", match=MatchText(text=file_filter)
        )]) if file_filter else None
        result = self._client.query_points(
            COLLECTION, query=query_vec, limit=top_k,
            query_filter=query_filter, with_payload=True,
        )
        return [{**p.payload, "score": p.score} for p in result.points]

    def delete_by_file(self, file_path):
        """删除某文件的所有块（文件被删/大改时用）。"""
        self._client.delete(COLLECTION, points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(
                key="file", match=MatchValue(value=file_path)
            )])
        ))
```

**三个 Qdrant 亮点技术点**：

1. **payload 元数据过滤**：Qdrant 能在向量检索时附加元数据过滤（`file_filter="tools/"` 只搜 tools 目录）。这是 sqlite-vec 等轻量方案做不到的，是代码 RAG 的刚需。
2. **稳定点 ID + upsert 增量**：同 ID 覆盖，不用先删后加。`delete_by_file` 按文件清理。
3. **COSINE 距离配置**：建 collection 时声明 `Distance.COSINE`，Qdrant 内部归一化，检索不用手算余弦——比手写更快更准。

### 4.4 混合检索（向量 + BM25 + RRF）

**完整流程**（`tools/code_search.py` 第 88-130 行）：

```python
# 1. 增量更新索引（只重新索引变化的文件）
await self._indexer.rebuild_if_needed()

# 2. query embedding
q_vec = await self._embedder.embed_one(params.query)

# 3. 向量召回（Qdrant）
vec_hits = await store.search(q_vec, top_k=20, file_filter=params.file_filter)
vec_ids = [h["id"] for h in vec_hits]

# 4. BM25 关键词召回（自实现 rag/bm25.py，不引重依赖）
bm25_ids = [doc_id for doc_id, _ in bm25.search(params.query, top_k=20)]

# 5. RRF 融合
if vec_ids and bm25_ids:
    fused_ids = reciprocal_rank_fusion(vec_ids, bm25_ids)
elif vec_ids:
    fused_ids = vec_ids
else:
    fused_ids = bm25_ids

# 6. 取 top_k，组装输出（含文件、行号、相似度、代码片段）
```

**为什么混合检索？**

| 召回方式 | 擅长 | 短板 |
|---------|------|------|
| 纯向量 | 语义匹配（"登录"→verify_token） | 漏精确关键词（变量名 user_id） |
| 纯 BM25 | 精确关键词（user_id 精确命中） | 漏语义（"登录"搜不到 verify_token） |
| **混合** | **两者兼得** | 略复杂 |

**BM25 为什么自实现？** `rank-bm25` 是纯 Python 小库，但本项目自己实现（`rag/bm25.py`，约 110 行）保持零依赖、可控。核心算法就是 §2.5 的公式。

### 4.5 增量索引（`rag/indexer.py`）

**IncrementalIndexer**（第 51 行）的 `rebuild_if_needed` 方法：

```python
async def rebuild_if_needed(self) -> dict[str, int]:
    stats = {"indexed": 0, "skipped": 0, "deleted": 0}

    # 1. 扫描当前所有可索引文件
    current_files = {str(p.relative_to(self._root)): p
                     for p in walk_indexable_files(self._root)}

    # 2. 删除已不存在的文件的索引
    for rel in list(self._meta.keys()):
        if rel not in current_files:
            self._store.delete_by_file(rel)
            self._meta.pop(rel, None)
            stats["deleted"] += 1

    # 3. 增量索引新增/变化的文件
    for rel, path in current_files.items():
        if self._needs_reindex(path, rel):  # mtime 粗筛 + hash 确认
            chunks = chunk_file(path, self._root)
            if rel in self._meta:
                self._store.delete_by_file(rel)  # 先删旧块
            await self._store.upsert_chunks(chunks)
            self._meta[rel] = FileIndexEntry(mtime=..., content_hash=..., chunk_ids=...)
            stats["indexed"] += 1
        else:
            stats["skipped"] += 1

    self._save_meta()  # 持久化到 .codebot/rag/file_index.json
    return stats
```

**两级增量判断**（`_needs_reindex`，第 112 行）：

```python
def _needs_reindex(self, path, rel) -> bool:
    mtime = path.stat().st_mtime
    entry = self._meta.get(rel)
    if entry is None:
        return True  # 新文件
    if entry.mtime != mtime:
        # mtime 变了，用 hash 确认内容是否真变
        new_hash = self._file_hash(path)
        if new_hash != entry.content_hash:
            return True  # 内容真变了
        entry.mtime = mtime  # 只是 touch，更新 mtime
        return False
    return False  # mtime 没变
```

**为什么两级判断？**
- mtime 快（一次 stat），但可能误报（`touch` 不改内容也变 mtime）
- hash 准（读全文算 md5），但慢（要读整个文件）
- 先 mtime 粗筛，变了再 hash 确认——兼顾速度和准确

**索引元数据持久化**：`.codebot/rag/file_index.json`，记录每个文件的 mtime + hash + chunk_ids。下次启动时增量更新，不用全量重建。

### 4.6 CodeSearch 工具（`tools/code_search.py`）

注册到 `create_default_registry`（`tools/__init__.py` 第 160 行），和 Grep 并列：

```python
class CodeSearch(Tool):
    name = "CodeSearch"
    description = "用自然语言语义搜索代码库，适合'找做某件事的代码'..."
    params_model = CodeSearchParams
    category = "read"
    is_concurrency_safe = True
    should_defer = False  # 基础工具，初始暴露
```

LLM 可用它做语义搜索：

```
LLM 调用：CodeSearch(query="处理用户登录鉴权的逻辑")
返回：语义搜索结果（query: "处理用户登录鉴权的逻辑"，融合 35 条召回）:
  1. verify_token @ auth/token.py:42-58 (score 0.87)
     def verify_token(token: str) -> bool:
         ...
  2. AuthMiddleware @ middleware/auth.py:15-30 (score 0.81)
     class AuthMiddleware:
         ...
```

### 4.7 降级设计

```python
async def execute(self, params):
    if not self.is_available():
        return self._fallback_message(params.query)
    # ... 正常检索

def _fallback_message(self, query):
    return ToolResult(
        output=f"语义搜索不可用（RAG 依赖未安装或 embedding 未配置）。"
               f"请改用 Grep 工具对 \"{query}\" 做关键词搜索。"
               f"\n\n启用语义搜索：uv pip install -e '.[rag]'"
    )
```

**降级触发条件**：
- `qdrant-client` 未装 → `QdrantCodeStore` 不可用
- embedder 不可用（anthropic 协议/key 缺失）→ `NullEmbedding`
- 索引未建立 → 首次使用时 `rebuild_if_needed` 建索引
- 任何环节异常 → try/except 兜底降级

---

## 5. 第三期：ToolSearch 语义增强 + 轻量重排

### 5.1 ToolSearch 语义匹配

`ToolRegistry` 新增 `search_deferred_semantic` 方法（`tools/__init__.py`）：

```python
async def search_deferred_semantic(self, query, max_results, protocol, embedder=None):
    if embedder is None or not embedder.is_available():
        return self.search_deferred(query, max_results, protocol)  # 回退关键词

    # 收集所有延迟工具的 name+description
    deferred = [(name, tool) for name, tool in self._tools.items()
                if getattr(tool, "should_defer", False)]

    # query + 所有工具描述一起 embed（batch 省 API）
    texts = [f"{name}: {tool.description}" for name, tool in deferred]
    all_vecs = await embedder.embed([query] + texts)
    q_vec = all_vecs[0]

    # 余弦相似度匹配
    scored = [(cosine_similarity(q_vec, v), name, tool)
              for v, (name, tool) in zip(all_vecs[1:], deferred)]
    scored.sort(reverse=True)
    # 返回 top_k 的 schema
```

**优势**：用户搜 "build codebase" 能匹配到 "CodeSearch"（即使字面没有 build/codebase 关键词）。

**注意**：工具数量少（20+ 个），这个改造收益有限，更多是展示"RAG 思路复用"。

### 5.2 轻量重排（`rag/reranker.py`）

两阶段检索思想：召回（粗排）+ 重排（精排）。

```python
async def embed_rerank(query, candidates, embedder, content_key="code", top_k=5):
    # query 和所有候选一起 embedding（batch）
    all_vecs = await embedder.embed([query] + [c["code"] for c in candidates])
    q_vec = all_vecs[0]
    cand_vecs = all_vecs[1:]

    # 余弦相似度重排
    scored = [(cosine_similarity(q_vec, v), c) for v, c in zip(cand_vecs, candidates)]
    scored.sort(reverse=True)
    return [c for _, c in scored[:top_k]]
```

### 5.3 CrossEncoder vs embedding 重排对比

| 维度 | embedding 重排（本项目） | CrossEncoder 重排 |
|------|------------------------|------------------|
| 精度 | 中（双塔，query/文档独立编码） | 高（拼接编码，交互更深） |
| 速度 | 快（可 batch） | 慢（每对独立编码） |
| 依赖 | 零新增 | torch + sentence-transformers + 模型下载 |
| 适合 | 轻量项目 | 生产级 RAG |

**为什么用 embedding 重排？** CrossEncoder 需要 torch（几百 MB）+ 模型下载，违背项目轻量定位。embedding 重排零新增依赖，演示两阶段检索思想。进阶可换 CrossEncoder。

---

## 6. 技术选型深度对比

### 6.1 向量库对比

| 维度 | Qdrant（选） | faiss | chromadb | sqlite-vec |
|------|------------|-------|----------|-----------|
| 语言 | Rust 内核 | C++ | Python+Rust | C |
| **元数据过滤** | ✅ 原生 payload | ❌ 需自己维护 | ✅ | ❌ |
| 嵌入式模式 | ✅ 本地文件 | ❌ 需配套服务 | ✅ | ✅ |
| Server 模式 | ✅ Docker/Cloud | ❌ | ✅ | ❌ |
| 性能 | 高（HNSW） | 最高（GPU 支持） | 中 | 中 |
| 面试认知度 | 高 | 高 | 中 | 低 |
| 依赖大小 | 中 | 大 | 中 | 小 |

**为什么选 Qdrant？**
1. **payload 元数据过滤**是代码 RAG 刚需（按目录/类型过滤），faiss/sqlite-vec 做不到
2. **嵌入式免起服务**，符合项目轻量定位
3. **可伸缩**：嵌入式 → Server 无缝切换，client API 一致

### 6.2 嵌入式 vs Server（面试必问）

| 形态 | 启动方式 | 优点 | 缺点 | 适用 |
|------|---------|------|------|------|
| **嵌入式（默认）** | `QdrantClient(path=".codebot/qdrant")` | 无需起服务、clone 即用 | 不支持多进程并发写 | 本项目默认 |
| Server | `QdrantClient(url="http://localhost:6333")` | 分布式、高并发 | 要额外起服务 | 大型部署/可选 |

**渐进式可伸缩设计**：配置留 `qdrant_url` 开关——本地开发用嵌入式，将来填 URL 就连集群。

### 6.3 embedding 选型

| 方案 | 优点 | 缺点 | 适用 |
|------|------|------|------|
| **OpenAI API**（选） | 质量高、无需本地模型 | 需 API Key、有费用 | 默认 |
| sentence-transformers | 离线、免 API 费 | 需下载模型（几百 MB） | 可选 |
| Ollama 本地 | 完全离线 | 质量较低 | 隐私场景 |

---

## 7. 降级策略与决策树

```
CodeSearch 被调用
    │
    ├─► is_available()?  (embedder + indexer + store 都可用)
    │       │
    │      No ──► 返回"用 Grep"降级提示
    │       │
    │      Yes
    │       │
    │       ├─► rebuild_if_needed() 增量索引
    │       │       └─► 失败? ──► 静默，继续用现有索引
    │       │
    │       ├─► embed_one(query)
    │       │       └─► 失败? ──► 返回"用 Grep"降级提示
    │       │
    │       ├─► store.search() 向量召回
    │       │       └─► 失败? ──► 静默返回空
    │       │
    │       ├─► bm25.search() 关键词召回
    │       │       └─► 失败? ──► 用向量结果
    │       │
    │       ├─► RRF 融合
    │       │
    │       └─► 零结果? ──► 返回"建议用 Grep"
    │               │
    │              有结果 ──► 返回 Top-K
```

**核心原则**：RAG 是增强，不是依赖。任何环节不可用都优雅降级，绝不阻塞主流程。这符合项目"渐进式降级"的设计哲学。

---

## 8. 性能考量与调优

### 8.1 索引性能

| 操作 | 耗时 | 优化 |
|------|------|------|
| 首次全量索引（1000 文件） | 30-60 秒 | 增量索引只做一次 |
| 增量索引（10 文件变化） | 1-3 秒 | mtime+hash 两级判断 |
| embedding API 调用 | 主要瓶颈 | 批量 embed（batch=64） |

### 8.2 检索性能

| 操作 | 耗时 | 优化 |
|------|------|------|
| query embedding | 100-200ms | 单次 API 调用 |
| Qdrant 向量召回 | <10ms | HNSW 索引 |
| BM25 关键词召回 | <5ms | 内存计算 |
| RRF 融合 | <1ms | 纯 Python |

**总检索延迟**：约 200-300ms（主要在 embedding API）。可接受——比 LLM 调用快 10x。

### 8.3 调优建议

- **embedding 维度**：text-embedding-3-small 是 1536 维，质量/速度平衡好。追求质量用 3-large（3072 维）
- **Top-K 召回**：默认 20，召回率 vs 精确率权衡。太小漏相关，太大重排慢
- **相似度阈值**：0.3（记忆）/ 无（代码，靠 Top-K 限制）
- **分块大小**：50 行。代码库函数普遍 < 50 行，超长函数滑窗切

---

## 9. 常见误区与踩坑点

### 9.1 误区：RAG 能完全替代 Grep

**错**。RAG 擅长"语义匹配"，但精确关键词搜索（如找变量名 `user_id` 的所有引用）Grep 更准。所以用**混合检索**，两者互补。

### 9.2 误区：embedding 维度越高越好

**不一定**。3072 维比 1536 维质量略高，但存储翻倍、检索变慢。代码 RAG 用 1536 维够用——代码语义相对结构化，不需要超高质量。

### 9.3 误区：分块越小检索越精准

**错**。分块太小（如单行）语义不完整，召回也看不懂上下文。函数级（50 行）是甜点区。

### 9.4 踩坑：OpenAI Chat Completions 的 arguments 是字符串

这和 RAG 无关但是个经典坑——`tool_calls[].function.arguments` 是 JSON 字符串不是对象，需要 `json.loads()` 二次解析。

### 9.5 踩坑：Qdrant 嵌入式模式不支持多进程并发写

如果用 Team 多 Agent 并行写同一个 `.codebot/qdrant`，会报锁错误。解决方案：用 Qdrant Server 模式，或每个 Agent 用独立索引目录。

### 9.6 踩坑：AST 分块漏掉模块级代码

`ast.walk` 只找 FunctionDef/ClassDef，模块级的全局变量、import 不被索引。本项目用"没有任何函数/类的文件 → 整体滑窗"兜底。

---

## 10. 文件清单与代码导航

```
codebot/rag/                    # RAG 子系统（8 个模块）
├── __init__.py                 # 导出接口
├── embedding.py                # EmbeddingProvider 抽象 + OpenAI/Null 实现 + cosine_similarity
│   ├── EmbeddingProvider (L20)     # 抽象基类，只有 embed() 方法
│   ├── cosine_similarity (L60)     # 余弦相似度实现
│   ├── OpenAIEmbedding (L86)       # OpenAI/兼容协议实现
│   ├── NullEmbedding (L130)        # 不可用时占位
│   └── create_embedding_provider (L150)  # 工厂函数
├── chunker.py                  # AST 分块
│   ├── CodeChunk (L52)             # 数据类，含稳定点 ID
│   ├── chunk_file (L74)            # 入口：按扩展名选分块策略
│   ├── _chunk_python (L94)         # Python AST 分块
│   └── _sliding_sub_chunks (L150)  # 滑窗切分（超长函数/非 Python）
├── qdrant_store.py             # Qdrant 向量存储
│   ├── QdrantCodeStore (L41)       # 主类（嵌入式/Server）
│   ├── upsert_chunks (L102)        # 批量写入
│   ├── search (L164)               # 向量检索 + payload 过滤
│   ├── delete_by_file (L146)       # 按文件清理
│   └── create_code_store (L210)    # 工厂（自动降级）
├── indexer.py                  # 增量索引管理
│   ├── IncrementalIndexer (L51)    # 主类
│   ├── _needs_reindex (L112)       # mtime+hash 两级判断
│   ├── rebuild_if_needed (L132)    # 增量更新入口
│   └── _save_meta (L100)           # 持久化到 JSON
├── bm25.py                     # 自实现 BM25
│   ├── BM25Index (L48)             # 主类
│   └── _score (L108)               # BM25 打分公式
├── fusion.py                   # RRF 融合
│   └── reciprocal_rank_fusion (L17)  # 融合算法
└── reranker.py                 # 轻量重排
    └── embed_rerank (L30)          # embedding 余弦重排

codebot/memory/
└── semantic_recall.py          # SemanticMemoryIndex（第一期）
    ├── SemanticMemoryIndex (L57)   # 记忆语义索引
    ├── ensure (L82)                # 增量更新
    └── search (L141)               # 语义检索

codebot/tools/
├── code_search.py              # CodeSearch 工具（第二期主力）
│   └── CodeSearch (L44)            # 语义代码搜索工具
└── impl/tool_search.py         # ToolSearchTool 语义增强（第三期）
    └── execute (L54)               # 有 embedder 走语义，否则关键词

tests/
├── test_rag_embedding.py       # 第一期测试（18 个）
├── test_rag_codesearch.py      # 第二期测试（18 个）
└── test_rag_rerank.py          # 第三期测试（10 个）
```

---

## 11. 面试高频点与深度回答

### Q1："代码怎么分块？"

**深度回答**：用 Python AST 按函数/类切，每个 chunk 是完整语义单元。超长函数（>50 行）滑窗二次切分，带 10 行 overlap 保证边界语义连续。非 Python 文件滑窗兜底。

**追问"为什么 50 行？"**：太小语义不完整，太大向量稀释。50 行约 400-600 token，是 embedding 的甜点区。业界（如 GitHub Copilot）也用类似粒度。

**追问"为什么不按 token 数切？"**：因为代码的语义单元是函数/类，不是固定 token 数。按 token 切会把一个函数切成两半，检索召回也用不了。AST 切保证完整性。

### Q2："为什么混合检索？"

**深度回答**：纯向量检索会漏精确关键词（变量名 `user_id` 字面没出现在 query 里但代码里有），纯关键词漏语义（"登录"搜不到 `verify_token`）。两路召回 + RRF 融合取长补短。

**追问"两路召回会不会重复？"**：会，但 RRF 天然处理重复——同一文档在两路都出现，分数累加，排名更靠前。这恰好是"两路都认可"的信号。

### Q3："RRF 为什么不用加权求和？"

**深度回答**：向量分数（0-1）和 BM25 分数（无上界）量纲不同，直接加权难调参——你要调 `α×vec_score + β×bm25_score` 的 α 和 β，不同数据集最优值不同。RRF 只看排名（`1/(k+rank)`），天然归一化，无需调参，k=60 是业界经验值。

**追问"k=60 什么含义？"**：k 是平滑常数。k 越大，排名差异影响越平滑（第 1 名和第 10 名分数差变小）；k 越小，头部排名优势越大。60 是原论文推荐值，平衡头部和中尾部。

### Q4："索引怎么增量更新？"

**深度回答**：两级判断——mtime 粗筛 + hash 确认。mtime 没变直接跳过（一次 stat，微秒级）；mtime 变了算文件内容 md5，hash 也变了才重新 embedding。Qdrant 用稳定点 ID（`hash(file:name:start_line)`）+ upsert 覆盖旧向量，delete_by_file 清理删除的文件。

**追问"为什么两级不直接用 hash？"**：hash 要读整个文件算 md5，大文件慢。mtime 是文件系统元数据，一次 stat 就拿到。先 mtime 粗筛掉 90% 未变文件，只对变了的那 10% 算 hash——省 90% 的 I/O。

### Q5："Qdrant 要起服务不重吗？"

**深度回答**：用嵌入式模式（`QdrantClient(path=".codebot/qdrant")`），装个 `qdrant-client` 包就能进程内跑，不用 `docker run`，clone 下来即用。配置留 `qdrant_url` 开关，将来要支持大团队/大代码库，填 URL 就连 Qdrant 集群，client API 完全一致——渐进式可伸缩。

**追问"嵌入式模式有什么限制？"**：不支持多进程并发写（文件锁）。如果多 Agent 并行写同一索引目录会报错。解决方案：用 Server 模式，或每个 Agent 独立目录。

### Q6："embedding 服务挂了怎么办？"

**深度回答**：全链路降级——CodeSearch 返回"用 Grep"提示，记忆检索回退 LLM 选择器，ToolSearch 回退关键词。RAG 是增强不是依赖，绝不阻塞主流程。这是项目"渐进式降级"哲学的体现。

**追问"怎么检测 embedding 不可用？"**：`create_embedding_provider` 返回 `NullEmbedding`（协议不支持/key 缺失），`is_available()` 返回 False。所有 RAG 组件都先检查 `is_available()` 再决定是否走语义路径。

### Q7："为什么选 Qdrant 不选 faiss？"

**深度回答**：三个原因：① Qdrant 有原生 payload 过滤（按目录/类型，代码 RAG 刚需），faiss 是纯向量库要自己维护元数据；② Qdrant 嵌入式免起服务，faiss 通常要配套服务；③ Qdrant 有 Rust 内核 + HNSW 索引，性能够用。faiss 的 GPU 加速优势在代码 RAG 场景用不上（库不大）。

### Q8："重排为什么不用 CrossEncoder？"

**深度回答**：CrossEncoder 需要 torch（几百 MB）+ 模型下载，违背项目轻量定位。用 embedding 余弦重排零新增依赖，且可以和召回阶段复用同一组向量（甚至可以省掉重排的 embedding 调用）。进阶想更强可接 `sentence-transformers` 的 CrossEncoder。

**追问"embedding 重排和 CrossEncoder 重排差多少？"**：CrossEncoder 精度高 5-10%（nDCG 指标），因为它拼接 query+文档一起编码，交互更深。但对 Top-20 → Top-5 的重排场景，embedding 重排已经能去掉大部分噪声，性价比高。

### Q9："RAG 的召回率怎么评估？"

**深度回答**：业界用 nDCG、MRR、Recall@K 等指标，需要标注数据集。本项目没有标注数据，靠两个信号间接评估：① 用户反馈（Agent 是否找到了对的代码）；② 看召回结果的相似度分数分布（健康分布应该有明显的头部高分区和尾部低分区）。

### Q10："如果让你继续优化，会做什么？"

1. **CrossEncoder 重排**：进阶提升精度
2. **query 改写**：用 LLM 把用户 query 改写成更适合检索的形式（如"登录鉴权"→"authentication token verification"）
3. **HyDE**：用 LLM 生成假设性文档辅助检索
4. **语义去重**：相似度 > 0.95 的块只保留一个
5. **多语言支持**：AST 分块目前只支持 Python，可扩展 tree-sitter 支持多语言

---

## 12. 手撕代码题

### 12.1 手撕：实现 cosine_similarity

```python
import math

def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)

# 测试
assert abs(cosine_similarity([1, 0], [1, 0]) - 1.0) < 1e-6   # 同向
assert abs(cosine_similarity([1, 0], [0, 1]) - 0.0) < 1e-6   # 正交
assert abs(cosine_similarity([1, 0], [-1, 0]) - (-1.0)) < 1e-6  # 反向
```

### 12.2 手撕：实现 RRF 融合

```python
def reciprocal_rank_fusion(*ranked_lists, k=60):
    scores = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda x: -x[1])]

# 测试
vec = ["a", "b", "c"]
kw = ["b", "a", "d"]
fused = reciprocal_rank_fusion(vec, kw)
# "b" 在两路都靠前，应排第一
assert fused[0] in ("a", "b")
```

### 12.3 手撕：实现简单的 AST 分块

```python
import ast

def chunk_python(source: str) -> list[dict]:
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    chunks = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start, end = node.lineno, node.end_lineno
            chunks.append({
                "name": node.name,
                "type": type(node).__name__,
                "start": start, "end": end,
                "code": "".join(lines[start-1:end]),
            })
    return chunks

# 测试
code = "def foo():\n    return 1\n\nclass Bar:\n    pass\n"
chunks = chunk_python(code)
assert len(chunks) == 2
assert chunks[0]["name"] == "foo"
assert chunks[1]["name"] == "Bar"
```

### 12.4 手撕：实现 BM25 打分

```python
import math

def bm25_score(query_tokens, doc_tokens, doc_freqs, corpus_size, avgdl, k1=1.5, b=0.75):
    doc_len = len(doc_tokens)
    doc_freq = {}
    for t in doc_tokens:
        doc_freq[t] = doc_freq.get(t, 0) + 1

    score = 0.0
    for t in query_tokens:
        if t not in doc_freq:
            continue
        f = doc_freq[t]
        df = doc_freqs.get(t, 0)
        idf = math.log((corpus_size - df + 0.5) / (df + 0.5) + 1)
        tf = (f * (k1 + 1)) / (f + k1 * (1 - b + b * doc_len / avgdl))
        score += idf * tf
    return score
```

---

## 13. 学习路径与扩展阅读

### 13.1 推荐学习顺序

1. **先读本章节**，建立 RAG 全景认知
2. **读 `codebot/rag/embedding.py`**（最简单，理解 embedding 抽象）
3. **读 `codebot/rag/chunker.py`**（理解 AST 分块，核心难点）
4. **读 `codebot/rag/fusion.py`**（最短，理解 RRF）
5. **读 `codebot/rag/bm25.py`**（理解 BM25 公式实现）
6. **读 `codebot/rag/qdrant_store.py`**（理解向量库操作）
7. **读 `codebot/rag/indexer.py`**（理解增量索引，最复杂）
8. **读 `tools/code_search.py`**（理解工具如何串起来）
9. **跑测试**：`uv run pytest tests/test_rag_*.py -v`

### 13.2 扩展阅读

| 主题 | 资源 |
|------|------|
| RAG 综述 | "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks"（原论文） |
| BM25 | "The Probabilistic Relevance Framework: BM25 and Beyond" |
| RRF | "Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods" |
| Qdrant 文档 | https://qdrant.tech/documentation/ |
| 代码 RAG | GitHub Copilot 的 `@workspace`、Sourcegraph Cody 的实现博客 |
| Embedding 模型 | OpenAI text-embedding-3 系列、BGE、E5 |

### 13.3 动手实验建议

1. **换个 embedding 模型**：把 `text-embedding-3-small` 换成 `3-large` 或 `BGE`，对比检索质量
2. **加 CrossEncoder 重排**：引入 `sentence-transformers`，对比重排前后 nDCG
3. **加 query 改写**：用 LLM 把用户 query 改写成多个检索 query，多路召回
4. **评估召回质量**：手动标注 20 个 query 的相关代码，算 Recall@5 和 MRR

### 13.4 学习完成检查清单

- [ ] 能画出 RAG 完整链路图（索引 + 检索）
- [ ] 能推导余弦相似度、BM25、RRF 三个公式
- [ ] 能解释为什么 AST 分块比固定字符切好
- [ ] 能说出混合检索的必要性和 RRF 的优势
- [ ] 能讲清楚增量索引的两级判断（mtime + hash）
- [ ] 能解释 Qdrant 嵌入式 vs Server 的选择理由
- [ ] 能描述完整的降级链路
- [ ] 能手撕 cosine_similarity、RRF、AST 分块
- [ ] 能回答"如何继续优化 RAG"

---

> **下一步**：配合 `docs/RAG优化方案.md`（设计方案）阅读，再看 `codebot/rag/` 源码，每个模块对照测试文件验证理解。
>
> **面试时记住**：先讲痛点（词汇鸿沟），再讲方案（混合检索），最后讲权衡（为什么不用 faiss/CrossEncoder）。展示工程判断力比堆技术名词更重要。
>
> **运行测试**：`uv run pytest tests/test_rag_*.py`

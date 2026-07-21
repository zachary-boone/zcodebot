# CodeBot RAG 优化方案

> 本文档评估 terminal-codebot 引入 RAG（检索增强生成）的可行性，并给出分期落地方案。
> 目标读者：想在真实 Agent 项目中落地 RAG、并把它作为简历/面试亮点的开发者。
> 定位：优化方案设计文档，不含已实现代码——代码骨架为落地参考。

---

## 目录

1. [为什么这个项目适合加 RAG](#1-为什么这个项目适合加-rag)
2. [现状分析：项目当前的"检索"能力](#2-现状分析项目当前的检索能力)
3. [RAG 基础回顾（面试必备）](#3-rag-基础回顾面试必备)
4. [三个切入点总览](#4-三个切入点总览)
5. [方案一：记忆语义检索（入门）](#5-方案一记忆语义检索入门)
6. [方案二：语义 CodeSearch 工具（主力）](#6-方案二语义-codesearch-工具主力)
7. [方案三：工具语义匹配（进阶点缀）](#7-方案三工具语义匹配进阶点缀)
8. [技术选型建议](#8-技术选型建议)
9. [分期落地路线图](#9-分期落地路线图)
10. [面试话术：怎么讲这个 RAG 项目](#10-面试话术怎么讲这个-rag-项目)

---

## 1. 为什么这个项目适合加 RAG

### 1.1 结论

**非常适合。** RAG 是这个项目当前最缺、又最自然契合的一块拼图。

### 1.2 三个理由

**理由一：痛点真实存在——"词汇鸿沟"**

现在 Agent 找代码完全靠 LLM 主动发 `Grep`/`Glob` 关键词。这有个致命短板：

```
用户问："项目哪里做了登录鉴权？"
代码里实际叫：verify_token() / check_auth() / AuthMiddleware
Grep 搜 "登录" / "login" → 0 命中
```

关键词检索无法跨越"用户表达"和"代码命名"之间的语义鸿沟。这正是语义检索（embedding）的用武之地——它匹配的是"意思"而不是"字面"。

**理由二：切入点天然，架构友好**

- `memory/recall.py` 已经有一个"检索 hook"骨架（`find_relevant_memories` 接受一个 `SelectorFn` 回调，现在用 LLM 当选择器）。把选择器换成 embedding 相似度，几乎是原地替换。
- 项目的**分层架构 + 注册表模式**让新增一个 `CodeSearch` 工具零侵入——继承 `Tool` 基类、实现 `execute`、注册即可，不用动核心循环。
- 项目已有 `LLMClient` 抽象，复用它的 embedding 能力零新增重依赖。

**理由三：对个人成长价值最高**

RAG 是 2024-2025 后端/AI 岗面试的高频考点。而"在真实 Agent 项目里落地 RAG"这个故事，比"跟教程搭 RAG demo"含金量高一个量级——你能讲清楚 chunking 策略、混合检索、增量索引等工程细节，这正是面试官想深挖的。

### 1.3 什么情况下不该加？（反向思考，面试加分）

诚实地说，RAG 不是万金油。以下情况要慎重：

- **小项目**：如果代码库只有几十个文件，Grep 完全够用，RAG 是过度设计。
- **强依赖精确匹配**：找变量定义、精确函数名，关键词/AST 检索比语义更准。
- **不能引重依赖**：如果项目要求零额外依赖，向量库和 embedding 模型会破坏轻量定位。

**正确姿势**：RAG 作为 Grep 的**补充**而非替代，用"混合检索"两者兼得，并保留降级到 Grep 的能力。这符合项目"渐进式降级"的设计哲学。

---

## 2. 现状分析：项目当前的"检索"能力

| 模块 | 文件 | 现在怎么做 | 是否语义 |
|------|------|-----------|---------|
| 代码搜索 | `tools/grep.py` | `re.compile` 逐行正则匹配 | ❌ 关键词 |
| 文件查找 | `tools/glob.py` | `Path.glob` 文件名匹配 | ❌ 关键词 |
| 文件读取 | `tools/read_file.py` | offset/limit 读原文 + FileCache | ❌ 无检索 |
| 记忆召回 | `memory/recall.py` | 扫 frontmatter → **LLM 选择器**挑 ≤5 个 | ⚠️ LLM 选，非向量 |
| 记忆加载 | `memory/auto_memory.py` | `memories.md` 全量注入 | ❌ 全量 |
| 上下文压缩 | `context/manager.py` | 按 token 累计保留近期 + LLM 摘要前缀 | ❌ 无检索 |
| 工具发现 | `tools/impl/tool_search.py` | 关键词打分（name+10/desc+5） | ❌ 关键词 |

**依赖现状**（`pyproject.toml`）：textual / anthropic / openai / pyyaml / pydantic / mcp / httpx。**没有任何向量/embedding 依赖**。

**一句话**：整个项目零语义检索能力，RAG 是一片空白，切入空间大。

---

## 3. RAG 基础回顾（面试必备）

### 3.1 RAG 是什么

RAG = Retrieval-Augmented Generation。核心思想：**在让 LLM 生成前，先从外部知识库检索相关内容，塞进 prompt，让 LLM"看着资料回答"**。

解决两个问题：
1. **知识时效性**：LLM 训练数据有截止时间，RAG 让它能用最新/私有知识
2. **上下文有限**：不可能把整个代码库塞进 prompt，RAG 只检索相关片段

### 3.2 RAG 完整链路

```
【索引阶段（离线）】
文档 → 分块(chunking) → embedding → 存入向量库

【检索阶段（在线）】
query → embedding → 向量相似度召回 Top-K → （可选）重排 → 拼进 prompt → LLM 生成
```

### 3.3 六个必懂概念

| 概念 | 一句话 | 面试追问点 |
|------|--------|-----------|
| **Embedding** | 把文本映射成向量，语义近的向量也近 | 维度、模型选择、归一化 |
| **Chunking** | 把长文档切成小块 | 固定大小 vs 语义切分、overlap、代码怎么切 |
| **向量相似度** | 余弦相似度 / 点积 / 欧氏距离 | 为什么用余弦、归一化后点积=余弦 |
| **Top-K 召回** | 取最相似的 K 个块 | K 怎么选、召回率 vs 精确率 |
| **混合检索** | 向量 + 关键词（BM25）两路召回融合 | 为什么要混合、RRF 融合算法 |
| **重排（Rerank）** | 用更强的交叉编码器对召回结果精排 | 双塔 vs 交叉编码器、延迟权衡 |

---

## 4. 三个切入点总览

| 方案 | 改什么 | 核心 RAG 技术点 | 难度 | 面试价值 |
|------|--------|----------------|------|----------|
| **① 记忆语义检索** | 改造 `memory/recall.py` | embedding、余弦相似度、Top-K | ⭐⭐ | 讲"检索替代 LLM 选择器" |
| **② 语义 CodeSearch** | 新增 `tools/code_search.py` | 代码分块、向量库、混合检索、重排、增量索引 | ⭐⭐⭐⭐ | 完整 RAG 链路，最能打 |
| **③ 工具语义匹配** | 增强 `tools/impl/tool_search.py` | 小规模语义匹配、降级 | ⭐ | 锦上添花 |

建议顺序：先做 ① 热身（理解 embedding + 相似度），再做 ② 主攻（完整链路），③ 有余力再做。

---

## 5. 方案一：记忆语义检索（入门）

### 5.1 现状

`recall.py::find_relevant_memories` 现在的做法：
1. 扫描所有记忆文件的 frontmatter（filename + description）
2. 生成清单，调用一次 LLM（side-query）让它挑 ≤5 个相关记忆
3. 注入选中记忆的全文

**问题**：每次都要额外调一次 LLM，有延迟和成本；且 LLM 只看 description，看不到记忆正文。

### 5.2 改造思路

把"LLM 选择器"替换成"embedding 相似度检索"：
1. 索引时：对每个记忆的正文做 embedding，存起来
2. 检索时：对 user query 做 embedding，算余弦相似度，取 Top-K
3. 好处：省一次 LLM 调用、基于正文语义（不只是 description）、可解释（有相似度分数）

### 5.3 代码骨架（参考）

```python
# codebot/memory/semantic_recall.py（新增）
from __future__ import annotations
import math
from dataclasses import dataclass

@dataclass
class MemoryEmbedding:
    path: str
    vector: list[float]
    mtime_ms: int

def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度：dot(a,b) / (|a|*|b|)。归一化后等价于点积。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)

class SemanticMemoryIndex:
    def __init__(self, embed_fn):
        # embed_fn: async (text) -> list[float]，复用项目的 LLMClient embedding
        self._embed_fn = embed_fn
        self._index: list[MemoryEmbedding] = []

    async def build(self, headers: list) -> None:
        """索引阶段：对每个记忆正文做 embedding。
        增量优化：按 mtime + hash 判断是否需要重新 embedding。"""
        for h in headers:
            text = _read_body(h.file_path)
            vec = await self._embed_fn(text)
            self._index.append(MemoryEmbedding(h.file_path, vec, h.mtime_ms))

    async def search(self, query: str, top_k: int = 5) -> list[str]:
        """检索阶段：query embedding → 余弦相似度 → Top-K。"""
        q_vec = await self._embed_fn(query)
        scored = [
            (cosine_similarity(q_vec, m.vector), m.path)
            for m in self._index
        ]
        scored.sort(reverse=True)
        return [path for score, path in scored[:top_k] if score > 0.3]
        # 0.3 是相似度阈值，过滤明显不相关的
```

然后在 `recall.py` 里把 `SelectorFn` 的实现从"调 LLM"换成"调 `SemanticMemoryIndex.search`"，接口不变，改动面最小。

### 5.4 涉及的技术点（面试可讲）

- embedding 的原理与选型
- 余弦相似度为什么适合语义检索（方向 > 长度）
- 相似度阈值过滤（0.3）
- 增量索引（mtime + hash）避免重复 embedding

---

## 6. 方案二：语义 CodeSearch 工具（主力）

**这是最值得做的方案**——它覆盖 RAG 的完整链路，面试时能从头讲到尾。

### 6.1 目标

新增一个 `CodeSearch` 工具，和 `Grep` 并列注册。LLM 可以用它做**语义搜索**：

```
LLM 调用：CodeSearch(query="处理用户登录鉴权的逻辑")
返回：verify_token() @ auth/token.py:42  (相似度 0.87)
     AuthMiddleware @ middleware/auth.py:15  (相似度 0.81)
```

### 6.2 完整架构

```
【索引阶段（离线/首次+增量）】
代码文件 → AST 分块（按函数/类） → 超长块滑窗切
        → embedding → 存入向量库（本地持久化）
                          ↑
                   增量更新（mtime+hash）

【检索阶段（在线）】
query → embedding → 向量召回 Top-20 ─┐
     → BM25 关键词召回 Top-20 ───────┤→ RRF 融合去重 → 重排 Top-5 → 返回给 LLM
                                     ┘
```

### 6.3 五个关键工程决策（面试深度所在）

**决策一：代码分块策略——AST 切分**

代码 RAG 和文档 RAG 最大的区别在这里。不能按固定字符切（会把一个函数切成两半，语义破碎）。正确做法：

```python
import ast

def chunk_python_file(source: str, file_path: str) -> list[dict]:
    """按 AST 切分：每个函数/类是一个 chunk。"""
    tree = ast.parse(source)
    chunks = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno
            end = node.end_lineno
            code = "\n".join(source.splitlines()[start-1:end])
            chunks.append({
                "type": type(node).__name__,
                "name": node.name,
                "file": file_path,
                "start_line": start,
                "end_line": end,
                "code": code,
            })
    return chunks
    # 超长函数（>512 token）再用滑窗切分，带 overlap
```

**为什么？** 因为检索的最小语义单元是"函数/类"，而不是"随机 512 字符"。按 AST 切能保证每个 chunk 是完整的语义单元。这是代码 RAG 的核心难点，面试必讲。

**决策二：混合检索——向量 + BM25，RRF 融合**

纯向量检索会漏掉精确关键词（如变量名 `user_id`），纯关键词漏语义。混合检索两者兼得：

```python
def reciprocal_rank_fusion(
    vector_results: list[str],
    keyword_results: list[str],
    k: int = 60,
) -> list[str]:
    """RRF（倒数排名融合）：不依赖分数绝对值，只看排名。
    score(doc) = sum(1 / (k + rank_i))"""
    scores = {}
    for rank, doc in enumerate(vector_results):
        scores[doc] = scores.get(doc, 0) + 1 / (k + rank + 1)
    for rank, doc in enumerate(keyword_results):
        scores[doc] = scores.get(doc, 0) + 1 / (k + rank + 1)
    return [doc for doc, _ in sorted(scores.items(), key=lambda x: -x[1])]
```

**为什么用 RRF 而不是加权求和？** 因为向量相似度分数（0-1）和 BM25 分数（无上界）量纲不同，直接加权难调参。RRF 只看排名，天然归一化，无需调参。这是既有深度又好实现的点。

**决策三：增量索引——mtime + hash**

代码天天改，不能每次全量重建索引（慢且费钱）。

```python
def needs_reindex(file_path: str, cache: dict) -> bool:
    """用 mtime 快速判断，mtime 变了再用内容 hash 确认。"""
    mtime = os.path.getmtime(file_path)
    cached = cache.get(file_path)
    if cached and cached["mtime"] == mtime:
        return False  # mtime 没变，肯定没改
    content_hash = hashlib.md5(open(file_path, "rb").read()).hexdigest()
    if cached and cached["hash"] == content_hash:
        cache[file_path]["mtime"] = mtime  # 只是 mtime 变了（如 touch）
        return False
    return True  # 内容确实变了，需要重新索引
```

**为什么两级判断？** mtime 判断快（一次 stat），但可能误报（touch 不改内容也变 mtime）。hash 准确但慢（要读全文）。先 mtime 粗筛，变了再 hash 确认，兼顾速度和准确。

**决策四：降级策略——符合项目哲学**

```python
class CodeSearch(Tool):
    async def execute(self, params):
        if not self._index_available():
            # embedding 服务挂了 / 没配 Key / 索引未建
            return ToolResult(
                output="语义索引不可用，建议改用 Grep 工具做关键词搜索",
                is_error=False,
            )
        # 正常语义检索...
```

这符合项目"渐进式降级"的设计哲学——RAG 是增强，不是依赖。挂了也不影响主流程。

**决策五：重排（可选，进阶）**

召回的 Top-20 用交叉编码器精排到 Top-5。双塔模型（embedding）快但粗，交叉编码器慢但准。用双塔召回、交叉编码器精排是业界标准的"两阶段检索"。

### 6.4 工具骨架

```python
# codebot/tools/code_search.py（新增）
from pydantic import BaseModel, Field
from codebot.tools.base import Tool, ToolResult

class CodeSearchParams(BaseModel):
    query: str = Field(..., description="用自然语言描述你要找的代码功能")
    top_k: int = Field(5, description="返回结果数量")

class CodeSearch(Tool):
    name = "CodeSearch"
    description = "用自然语言语义搜索代码库，适合'找做某件事的代码'（关键词搜索用 Grep）"
    params_model = CodeSearchParams
    category = "read"
    is_concurrency_safe = True
    should_defer = False  # 高频工具，初始暴露

    def __init__(self, index, embed_fn):
        self._index = index
        self._embed_fn = embed_fn

    async def execute(self, params: CodeSearchParams) -> ToolResult:
        if not self._index.available():
            return ToolResult(output="语义索引不可用，请改用 Grep", is_error=False)
        # 1. query embedding
        q_vec = await self._embed_fn(params.query)
        # 2. 向量召回 + BM25 召回
        vec_hits = self._index.vector_search(q_vec, top_k=20)
        kw_hits = self._index.bm25_search(params.query, top_k=20)
        # 3. RRF 融合
        fused = reciprocal_rank_fusion(vec_hits, kw_hits)[:params.top_k]
        # 4. 格式化输出（含文件、行号、相似度）
        output = "\n".join(
            f"{h.name} @ {h.file}:{h.start_line}  (score {h.score:.2f})"
            for h in fused
        )
        return ToolResult(output=output)
```

注册到默认工具表（`tools/__init__.py::create_default_registry`）即可，Agent 主循环零改动。

### 6.5 涉及的技术点（面试可讲一大串）

代码分块（AST）、embedding、向量库、余弦相似度、BM25、RRF 混合检索、增量索引、两阶段检索（召回+重排）、降级策略。**这一个方案能撑起半场 RAG 面试。**

---

## 7. 方案三：工具语义匹配（进阶点缀）

### 7.1 现状

`tools/impl/tool_search.py` 现在用关键词打分（name 命中+10、desc+5、分词+3/+1）匹配延迟工具。

### 7.2 改造

工具数量多时，关键词匹配质量下降。可以对工具的 name+description 做 embedding，用户 query 语义匹配。

**注意**：工具数量少（20+ 个），这个改造收益有限，属于"锦上添花"。做的价值在于展示"同一套 RAG 思路可以复用到不同场景"。保留关键词作为 fallback。

---

## 8. 技术选型建议

贴合项目"轻量、可插拔、渐进式降级"的调性：

| 组件 | 推荐 | 理由 | 不推荐 |
|------|------|------|--------|
| **embedding** | 复用现有 provider 的 embedding API（OpenAI/Anthropic） | 零新增重依赖，和现有 LLMClient 一致 | 大模型本地 embedding（重） |
| embedding（本地可选） | `sentence-transformers` | 离线、免 API 费用 | - |
| **向量库** | **Qdrant（嵌入式模式）** | 生产级、Rust 内核性能强、支持元数据过滤、嵌入式免起服务 | Qdrant Server（起服务，破坏轻量定位） |
| **BM25** | `rank-bm25`（纯 Python 小库） | 轻量、无依赖冲突 | Elasticsearch（重） |
| **重排（可选）** | `sentence-transformers` 的 CrossEncoder | 需要时再加 | - |

**核心原则**：
- 优先复用项目已有能力（LLMClient、SQLite 风格）
- 向量库用 Qdrant 的**嵌入式模式**，不引入独立服务
- 一切可降级——没配 embedding 就 fallback 到 Grep
- **切勿**默认要求起 Qdrant Server / Docker，那会破坏"clone 下来就能跑"的体验

---

## 8b. Qdrant 集成方案（选定向量库）

### 8b.1 为什么选 Qdrant

| 维度 | Qdrant 的优势 |
|------|--------------|
| 性能 | Rust 内核，HNSW 索引，检索快、内存效率高 |
| **元数据过滤** | 原生支持 payload 过滤（如"只搜 .py 文件""只搜某目录"），这是代码 RAG 的刚需 |
| 部署灵活 | 嵌入式（本地文件）/ Docker / Cloud 三种形态，同一套 client API |
| 面试认知度 | 生产级主流向量库，面试官熟悉，好展开 |
| 混合检索 | 新版原生支持稀疏向量（BM25 风格）+ 稠密向量，可在库内做混合 |

### 8b.2 关键决策：嵌入式 vs Server（面试必问）

Qdrant 有两种形态，选择直接影响项目定位：

| 形态 | 启动方式 | 优点 | 缺点 | 适用 |
|------|---------|------|------|------|
| **嵌入式（推荐默认）** | `QdrantClient(path="./.codebot/qdrant")` | 无需起服务、单机持久化、clone 即用 | 不支持并发多进程写、无分布式 | 本项目默认 |
| Server / Docker | `QdrantClient(url="http://localhost:6333")` | 分布式、高并发、生产稳定 | 要额外起服务，破坏轻量定位 | 大型部署/可选 |

**给本项目的方案**：
- 默认用**嵌入式模式**，索引存 `.codebot/qdrant/`（和项目已有的 `.codebot/sessions/` 等风格一致）
- 配置里留一个可选 `qdrant_url` 字段——不填走嵌入式，填了连远程 Server
- 这样既轻量、又展示"可伸缩架构"的设计意识

**面试话术**：
> "我用 Qdrant 的嵌入式模式做默认，因为这是个终端工具，不能要求用户额外起服务，得保证 clone 下来就能跑。但我在配置里留了 qdrant_url 开关——本地开发用嵌入式，将来要支持大团队/大代码库，填个 URL 就能无缝切到 Qdrant 集群，client API 完全一致。这是'渐进式可伸缩'的设计。"

### 8b.3 依赖

```toml
# pyproject.toml 新增（可选依赖组，不污染核心依赖）
[project.optional-dependencies]
rag = [
    "qdrant-client>=1.7.0",   # 嵌入式模式已内置，无需额外装 server
    "rank-bm25>=0.2.2",       # BM25 关键词召回
]
```

安装：`uv pip install -e ".[rag]"`（没装则 CodeSearch 自动降级到 Grep）。

### 8b.4 Qdrant 索引骨架（参考）

```python
# codebot/rag/qdrant_store.py（新增）
from __future__ import annotations
from qdrant_client import QdrantClient, models

class QdrantCodeStore:
    """代码块向量存储。默认嵌入式（本地文件），可选连远程 Server。"""

    COLLECTION = "code_chunks"

    def __init__(self, path: str = ".codebot/qdrant", url: str | None = None, dim: int = 1536):
        # url 优先：填了连 Server，否则嵌入式本地持久化
        if url:
            self._client = QdrantClient(url=url)
        else:
            self._client = QdrantClient(path=path)
        self._dim = dim
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        existing = [c.name for c in self._client.get_collections().collections]
        if self.COLLECTION not in existing:
            self._client.create_collection(
                collection_name=self.COLLECTION,
                vectors_config=models.VectorParams(
                    size=self._dim,
                    distance=models.Distance.COSINE,  # 余弦相似度
                ),
            )

    def upsert_chunks(self, chunks: list[dict], vectors: list[list[float]]) -> None:
        """写入/更新代码块。payload 存元数据用于过滤。"""
        points = [
            models.PointStruct(
                id=chunk["id"],           # 用 file:name:start_line 的 hash 做稳定 ID
                vector=vec,
                payload={
                    "file": chunk["file"],
                    "name": chunk["name"],
                    "type": chunk["type"],       # FunctionDef / ClassDef
                    "start_line": chunk["start_line"],
                    "end_line": chunk["end_line"],
                    "code": chunk["code"],
                    "content_hash": chunk["content_hash"],  # 增量索引用
                },
            )
            for chunk, vec in zip(chunks, vectors)
        ]
        self._client.upsert(collection_name=self.COLLECTION, points=points)

    def search(self, query_vec: list[float], top_k: int = 20,
               file_filter: str | None = None) -> list[dict]:
        """向量检索，支持按文件路径前缀过滤（Qdrant payload 过滤的用武之地）。"""
        query_filter = None
        if file_filter:
            query_filter = models.Filter(
                must=[models.FieldCondition(
                    key="file",
                    match=models.MatchText(text=file_filter),
                )]
            )
        hits = self._client.query_points(
            collection_name=self.COLLECTION,
            query=query_vec,
            limit=top_k,
            query_filter=query_filter,
            with_payload=True,
        ).points
        return [
            {**h.payload, "score": h.score}
            for h in hits
        ]

    def delete_by_file(self, file_path: str) -> None:
        """删除某文件的所有块（文件被删/大改时增量更新用）。"""
        self._client.delete(
            collection_name=self.COLLECTION,
            points_selector=models.FilterSelector(
                filter=models.Filter(must=[
                    models.FieldCondition(key="file", match=models.MatchValue(value=file_path))
                ])
            ),
        )
```

### 8b.5 Qdrant 的三个亮点技术点（面试可讲）

1. **payload 过滤**：Qdrant 能在向量检索时附加元数据过滤条件（`file`/`type`），实现"只在 tools/ 目录里语义搜索""只搜类不搜函数"。这是 sqlite-vec 等轻量方案做不到的，也是代码 RAG 的刚需——`search(..., file_filter="tools/")`。

2. **稳定点 ID + upsert 做增量**：用 `hash(file:name:start_line)` 做点 ID，配合 `content_hash` payload。文件改了：算新 hash，变了的块 `upsert`（同 ID 覆盖）、删掉的块 `delete_by_file`。避免全量重建。

3. **COSINE 距离配置**：建 collection 时直接声明 `Distance.COSINE`，Qdrant 内部会归一化，检索时不用自己算余弦——比手写相似度更快更准。

### 8b.6 与 §6 CodeSearch 工具的衔接

把 §6.4 的 `CodeSearch.execute` 里的 `self._index.vector_search(...)` 替换为 `QdrantCodeStore.search(...)`，BM25 召回仍用 `rank-bm25` 在内存跑，最后 RRF 融合。Qdrant 只负责稠密向量这一路（也可用 Qdrant 新版稀疏向量把 BM25 也搬进库里，作为进阶）。

---

## 9. 分期落地路线图

### 第一期：热身（1-2 天）
- 目标：理解 embedding + 相似度
- 做方案① 记忆语义检索
- 交付：`memory/semantic_recall.py`，能用向量相似度替代 LLM 选择器
- 学到：embedding API 调用、余弦相似度、Top-K、阈值过滤

### 第二期：主攻（3-5 天）
- 目标：完整 RAG 链路
- 做方案② 语义 CodeSearch（向量库用 Qdrant 嵌入式，见 §8b）
- 交付：`tools/code_search.py` + `rag/qdrant_store.py` + 增量更新
- 学到：AST 分块、Qdrant（嵌入式/payload 过滤/upsert 增量）、混合检索、RRF、降级

### 第三期：完善（1-2 天，可选）
- 目标：工程化 + 复用
- 做方案③ 工具语义匹配 + 重排优化
- 交付：ToolSearch 语义增强 + CrossEncoder 重排
- 学到：两阶段检索、场景复用、性能权衡

### 配套：写文档 + 测试
- 每期给核心模块写单测（项目有 17 个测试文件的传统）
- 更新 `docs/` 加一篇 RAG 专题（呼应现有 phase 文档风格）

---

## 10. 面试话术：怎么讲这个 RAG 项目

### 10.1 一句话介绍

> "我在自己的终端 AI Coding Agent 项目里落地了一套代码 RAG 系统。原本 Agent 找代码只能靠关键词 Grep，存在'词汇鸿沟'——用户说'登录鉴权'但代码叫 verify_token，搜不到。我加了语义 CodeSearch 工具，用 AST 分块 + 向量检索 + BM25 混合召回 + RRF 融合，让 Agent 能按'意思'找代码，同时保留降级到 Grep 的能力。"

### 10.2 能深挖的追问链

| 追问 | 你的答案要点 |
|------|-------------|
| "代码怎么分块？" | AST 按函数/类切，超长函数滑窗+overlap，保证语义完整 |
| "为什么要混合检索？" | 纯向量漏精确关键词，纯关键词漏语义，RRF 融合两路 |
| "RRF 为什么不用加权求和？" | 向量分数和 BM25 分数量纲不同，RRF 只看排名天然归一化 |
| "代码天天改，索引怎么更新？" | mtime 粗筛 + hash 确认；Qdrant 用稳定点 ID + upsert 覆盖，delete_by_file 清理 |
| "embedding 服务挂了怎么办？" | 降级到 Grep，RAG 是增强不是依赖 |
| "召回质量不够怎么优化？" | 两阶段检索：双塔召回 + 交叉编码器重排 |
| "为什么选 Qdrant？" | 生产级、Rust 内核、原生 payload 过滤（能按目录/类型过滤，代码 RAG 刚需）、嵌入式免起服务 |
| "Qdrant 要起服务不重吗？" | 用嵌入式模式（本地文件持久化）保证 clone 即用；配置留 qdrant_url 开关，将来无缝切集群，渐进式可伸缩 |
| "Qdrant 怎么做增量索引？" | 稳定点 ID（hash file:name:line）+ content_hash payload；变了的块 upsert 覆盖，删了的块 delete_by_file，不全量重建 |

### 10.3 亮点提炼公式

**技术深度**（AST 分块 / RRF / 两阶段检索）× **工程判断**（降级策略 / 轻量选型 / 不过度设计）× **真实价值**（解决词汇鸿沟痛点）

这三点凑齐，就是一个能打的 RAG 项目故事。

---

> **最后提醒**：面试官最反感"为了用技术而用技术"。讲这个 RAG 项目时，一定要先讲**痛点**（词汇鸿沟），再讲**方案**（混合检索），最后讲**权衡**（为什么保留 Grep、为什么不用 faiss）。展示你的工程判断力，比堆砌技术名词更重要。

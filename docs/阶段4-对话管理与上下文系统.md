# 阶段4：对话管理与上下文系统

> **学习目标**：理解对话历史的数据结构、Token 估算的锚定策略、双层压缩的完整流程、RecoveryState 的文件快照恢复、自动记忆提取的跨对话积累、会话持久化的 JSONL 格式。
> **预计时间**：3-4 天
> **前置要求**：完成阶段2（Agent 主循环中 auto_compact 被调用）；理解 LLM Context Window 的概念、Python dataclass
> **面试导向**：上下文管理是 Agent 系统的核心难点。读完本章你能回答"长对话怎么处理""Token 怎么算""跨对话怎么记忆"等核心问题。

---

## 目录

1. [对话管理器：Agent 的短期记忆](#1-对话管理器agent-的短期记忆)
2. [Token 估算：锚定策略](#2-token-估算锚定策略)
3. [双层压缩策略](#3-双层压缩策略)
4. [RecoveryState：压缩后不丢上下文](#4-recoverystate压缩后不丢上下文)
5. [断路器：防止频繁压缩](#5-断路器防止频繁压缩)
6. [自动记忆提取：跨对话的长期记忆](#6-自动记忆提取跨对话的长期记忆)
7. [会话持久化](#7-会话持久化)
8. [面试高频点与追问](#8-面试高频点与追问)

---

## 1. 对话管理器：Agent 的短期记忆

### 1.1 Message 数据结构

Agent 说的每句话、执行的每个工具，都记录在 `Message` 对象里（`conversation.py` 第 28-34 行）：

```python
@dataclass
class Message:
    role: str           # "user" | "assistant"
    content: str        # 文本内容
    tool_uses: list[ToolUseBlock] = field(default_factory=list)        # 工具调用列表
    tool_results: list[ToolResultBlock] = field(default_factory=list)  # 工具结果列表
    thinking_blocks: list[ThinkingBlock] = field(default_factory=list) # 思考块
```

一条消息不是简单的"字符串"。它可能同时包含：

| 字段 | 含义 | 例子 |
|------|------|------|
| `content` | 文本内容 | "我来读取 main.py" |
| `tool_uses` | 工具调用列表 | `[{tool_use_id: "tool_001", tool_name: "ReadFile", arguments: {file_path: "main.py"}}]` |
| `tool_results` | 工具结果列表 | `[{tool_use_id: "tool_001", content: "import os...", is_error: False}]` |
| `thinking_blocks` | 思考块 | `[{thinking: "用户要修 bug，先读文件", signature: "..."}]` |

### 1.2 对话历史就是一条"消息列表"

```
[0] user:    帮我分析代码
[1] system:  当前工作目录是 /project/src
[2] system:  用户偏好简洁代码
[3] assistant: 我来读取 main.py → [tool_uses: ReadFile]
[4] user:    [tool_results: import os...]
[5] assistant: 代码中第 42 行有问题 → [tool_uses: EditFile]
[6] user:    [tool_results: 修改成功]
[7] assistant: 修好了，第 42 行的死循环已修复
```

### 1.3 为什么所有东西都伪装成"用户消息"？

Anthropic 的 Messages API 要求消息的 role 严格交替：user → assistant → user → assistant...。

工具执行结果和系统提醒的 role 也是 user——因为它们的本质都是"给 LLM 提供新信息"，归入 user 侧最自然。

系统提醒（Plan 模式提示、延迟工具列表）会用 XML 标签 `<system-reminder>` 包裹，和普通用户输入区分开。

### 1.4 三个特殊注入

| 注入类型 | 位置 | 内容 |
|---------|------|------|
| **环境上下文** | `history[0]`（LLM 最先看到） | "你运行在 Windows 上，当前目录是 /project/src" |
| **长期记忆** | `history[0]` 或 `history[1]` | memories.md 的内容 |
| **系统提醒** | 追加在 `history` 末尾（最近一条） | 当前轮次的临时提示（如"Plan 模式：只能读"） |

**为什么环境上下文放最前面？** 因为 LLM 的注意力机制对开头和结尾的内容更敏感（primacy effect + recency effect）。环境信息是"全局背景"，放开头让 LLM 始终记住。

### 1.5 ConversationManager 的核心字段

```python
@dataclass
class ConversationManager:
    history: list[Message] = field(default_factory=list)
    env_injected: bool = field(default=False, init=False)      # 环境是否已注入
    ltm_injected: bool = field(default=False, init=False)      # 长期记忆是否已注入

    # Token 估算的"锚点"（详见 §2）
    last_input_tokens: int = field(default=0, init=False)      # API 报告的上一轮 prompt 大小
    baseline_tokens: int = field(default=0, init=False)        # 真实用量锚点
    anchor_count: int = field(default=0, init=False)           # 记录锚点时的消息数
```

**`env_injected` 和 `ltm_injected` 为什么是 bool 而不是每次都注入？** 因为环境上下文和长期记忆在对话过程中不变，注入一次就够。重复注入会浪费 token。但压缩后历史被替换，需要重新注入——所以这两个标志位会被重置。

---

## 2. Token 估算：锚定策略

### 2.1 问题

每次 LLM API 调用后才返回精确的 token 用量。但在两次调用之间，我们追加了工具结果和系统提醒——这些新消息的 token 数未知。

如果不知道当前总 token 数，就不知道什么时候该触发压缩。

### 2.2 混合估算策略

**核心思路**：精确值 + 估算值 = 足够用的近似值。

```python
_CHARS_PER_TOKEN = 3.5  # 经验值

def estimate_tokens(messages: list[Message]) -> int:
    """基于字符数对一组消息做 token 估算。"""
    total = sum(_message_chars(m) for m in messages)
    return int(total / _CHARS_PER_TOKEN)

def _message_chars(m: Message) -> int:
    n = len(m.content)
    for tb in m.thinking_blocks:
        n += len(tb.thinking)
    for tu in m.tool_uses:
        n += len(tu.tool_name) + len(json.dumps(tu.arguments, ensure_ascii=False))
    for tr in m.tool_results:
        n += len(tr.content)
    return n
```

- **精确部分**：最近一次 LLM API 返回的 token 用量（作为"锚点"）
- **估算部分**：锚点之后追加的消息，用简单公式估算（总字符数 ÷ 3.5 ≈ token 数）

### 2.3 为什么是 3.5？

经验值，基于以下观察：
- 英文约 4 字符/token（每个单词约 4-5 字符，1 个 token）
- 中文约 1-2 字符/token（每个汉字约 1-2 个 token）
- 代码混着用，取 3.5 是个实用的折中

**这个估算只用于"锚点后的增量"**，最坏情况也就差几个百分点，够用。

### 2.4 锚定机制（关键设计）

`ConversationManager` 有两个字段做锚定：

```python
baseline_tokens: int = 0   # API 报告的真实 prompt 大小
anchor_count: int = 0      # 记录锚点时的消息数
```

**`record_usage_anchor()` 方法**：每次 LLM 返回后，立刻"钉下锚点"：

```python
def record_usage_anchor(self, input_tokens, cache_read, cache_creation, output_tokens, msg_count):
    # baseline = input + cache_read + cache_creation + output
    # （API 报告的完整 prompt+output 大小）
    self.baseline_tokens = input_tokens + cache_read + cache_creation + output_tokens
    self.anchor_count = msg_count
```

**`current_tokens()` 方法**：估算当前总 token 数：

```python
def current_tokens(self) -> int:
    if self.baseline_tokens == 0:
        # 冷启动：无锚点，全字符估算
        return estimate_tokens(self.history)
    # 有锚点：锚点前的用 API 数据，锚点后的用字符估算
    after_anchor = self.history[self.anchor_count:]
    return self.baseline_tokens + estimate_tokens(after_anchor)
```

### 2.5 锚点失效的场景

如果对话被压缩（历史被替换），锚点失效——因为 `baseline_tokens` 对应的消息已经被替换掉了。此时退化为全字符估算。

但压缩后的对话通常很短（只有摘要 + 尾部），全字符估算也差不了多少。

**这是"优雅降级"的设计**——精确数据失效时退化为粗略估算，而不是崩溃。

---

## 3. 双层压缩策略

当对话太长（接近 context window 上限），必须压缩。CodeBot 用了两层：

### 3.1 Layer 1：工具结果裁剪（每次 LLM 调用前）

**问题**：一次工具调用可能返回巨大的输出（10MB 日志文件）。太多大结果会快速塞满上下文。

**常量**（`context/manager.py` 第 24-30 行）：

```python
SINGLE_RESULT_CHAR_LIMIT = 50_000    # 单条结果超过 5 万字符 → 截断
AGGREGATE_CHAR_LIMIT = 200_000       # 本轮所有结果合计超过 20 万字符 → 替换最早的
PREVIEW_CHARS = 2_000                # 截断时保留的预览字符数
KEEP_RECENT_TURNS = 10               # 保留最近 10 轮的工具结果
OLD_RESULT_SNIP_CHARS = 2_000        # 旧结果截断保留的字符数
```

**裁剪算法**：

1. **单条裁剪**：任何单条工具结果超过 5 万字符，截断为前 2000 字符 + `<snipped>`
2. **聚合裁剪**：本轮所有工具结果合计超过 20 万字符，最早的几条替换为"结果已保存在磁盘，路径是 xxx"

**关键设计**：截断只作用于发送给 LLM 的副本（`api_conv`），原始对话（`conversation`）保持完整，用于持久化。

**为什么要这样设计？** 因为 LLM 不需要看完整的 10MB 日志——它只需要知道"有这个结果"和"大概内容"。但持久化要保留完整结果，方便用户事后查看。

### 3.2 Layer 2：自动对话压缩（token 快超时才触发）

**触发条件**：估算的 token 数 ≥ `context_window - AUTO_COMPACT_SAFETY_MARGIN`（13K，给输出留安全边距）

```python
AUTO_COMPACT_SAFETY_MARGIN = 13_000    # 自动压缩的安全边距
MANUAL_COMPACT_SAFETY_MARGIN = 3_000   # 手动压缩（/compact 命令）的安全边距
```

**为什么自动是 13K，手动是 3K？** 因为自动压缩是 LLM 触发的，要给 LLM 足够余量避免在压缩过程中就超限。手动压缩是用户主动触发的，不需要这么大余量。

### 3.3 压缩算法分三步

**第一步：分割**

把消息列表从后往前切成两段：
- **prefix（前缀）**：老消息，需要被压缩成摘要
- **keep（尾部）**：最近的消息，保留原文

```python
KEEP_RECENT_TOKENS = 10_000   # 保留近期 1 万 token
MIN_KEEP_MESSAGES = 5         # 最少保留 5 条
KEEP_MAX_TOKENS = 40_000      # 最多保留 4 万 token（防单条超大消息吞掉窗口）
```

保留的尾部约 10K tokens（通常 5-15 条消息），因为最近几轮对话的细节对后续推理至关重要。

**为什么是 1 万 token？** 经验值。太少会让 LLM 忘记最近的工作（如刚读的文件、刚改的代码）；太多会浪费压缩空间。1 万 token 约等于 5-15 条消息，能覆盖最近 2-3 轮交互。

**为什么有 KEEP_MAX_TOKENS？** 防止单条超大消息（如读了一个 3 万 token 的大文件）吞掉整个保留窗口。如果单条消息超过 4 万 token，就停止保留，把它纳入摘要。

**第二步：生成摘要**

调用 LLM，用特定的结构化 Prompt 把 prefix 压缩成一个摘要。

**第三步：组装**

把 摘要 + 恢复附件 + 保留的尾部 拼成新的对话历史，替换旧的。

### 3.4 结构化摘要 Prompt

摘要 Prompt 要求 LLM 按 9 段结构输出（参考 Claude Code 的设计）：

1. **请求意图**：用户想做什么
2. **关键技术**：涉及的技术栈、框架
3. **文件代码**：修改了哪些文件、改了什么
4. **错误修复**：修了什么 bug、怎么修的
5. **用户消息原文**：用户的关键指令（保留原文）
6. **待办**：还没做的事
7. **当前工作**：现在进行到哪一步
8. **下一步**：接下来要做什么
9. **关键决策**：做了哪些技术选择

**为什么是结构化而不是自由文本？** 因为：
1. 结构化让 LLM 不会遗漏关键信息
2. 结构化便于 LLM 压缩后快速定位（"待办"在第几段）
3. 结构化让摘要质量可评估（每段是否填了）

### 3.5 MIN_SUMMARIZE_PREFIX_TOKENS 的设计

```python
MIN_SUMMARIZE_PREFIX_TOKENS = 2_000
```

**含义**：前缀 token 数低于 2000 时不做摘要。

**为什么？** 因为摘要要调用一次 LLM（成本和延迟），如果前缀太小（如 500 token），摘要往返的开销比回收的空间还大——"压了个寂寞"。

这是工程上的"成本效益分析"——只在值得的时候才压缩。

### 3.6 CompactBoundary：压缩结果的结构化

```python
@dataclass
class CompactBoundary:
    """Layer 2 压缩的结构化结果。"""
    summary: str              # LLM 对前缀生成的摘要
    keep: list[Message]       # 原样保留的近期尾部消息
```

**为什么独立成结构？** 因为 session 层（负责持久化）需要把 summary 和 keep 一起保存，这样 resume 时能重建压缩后的状态。`auto_compact` 保持纯粹——只负责压缩，不依赖任何 session。

---

## 4. RecoveryState：压缩后不丢上下文

### 4.1 问题

压缩后 LLM 失去了之前的文件内容。如果它刚读了 5 个文件，压缩后全忘了，就要重读——浪费时间和 token。

### 4.2 解法：文件快照缓存

每次读文件时，把文件内容快照缓存在 `RecoveryState` 里。压缩时把这些快照作为附件加到摘要后面。

```python
class RecoveryState:
    """保存重建工作上下文所需的快照。
    每次 ReadFile / skill 调用时记录，
    auto_compact 触发阈值时消费。"""
    file_snapshots: dict[str, str]  # {file_path: content}
    skill_snapshots: dict[str, str]  # {skill_name: prompt_body}
```

### 4.3 压缩时的恢复流程

```
压缩前：
  history = [msg0, msg1, ..., msg50]
  recovery_state = {
    "main.py": "import os...",      # 之前读过的文件
    "config.py": "DEBUG = True...",  # 之前读过的文件
  }

压缩后：
  history = [
    {role: user, content: "## 摘要\n用户要修 bug..."},  # 摘要
    {role: user, content: "## 旧文件快照（需最新内容请重新读取）\n
                           ### main.py\nimport os...\n
                           ### config.py\nDEBUG = True..."},  # 恢复附件
    msg45, msg46, ..., msg50,  # 保留的尾部
  ]
```

LLM 压缩后仍然保有工作上下文——它知道之前读过什么文件、内容是什么。如果需要最新内容，再调 ReadFile。

### 4.4 为什么标注"旧快照"？

因为文件可能在压缩后被外部修改（如用户手动改了）。标注"这是旧快照，需要最新内容请重新读取"让 LLM 知道这些内容可能过期，必要时重读。

**这是"乐观一致性"的设计**——默认文件没变（多数情况），但提示 LLM 可能变了。比"悲观一致性"（每次都重读）更高效。

---

## 5. 断路器：防止频繁压缩

### 5.1 问题

如果对话刚好在阈值附近，可能在压缩和继续对话之间反复横跳：

```
对话 100K → 压缩到 50K → 加一条消息到 51K → ... → 加到 100K → 又压缩...
```

每次压缩都调用一次 LLM（成本和延迟），频繁压缩会严重影响体验。

### 5.2 解法：CompactCircuitBreaker

```python
class CompactCircuitBreaker:
    """短时间内阻止第二次压缩。"""
    def __init__(self, cooldown_seconds: float = 60.0):
        self.cooldown = cooldown_seconds
        self.last_compact_time: float = 0

    def can_compact(self) -> bool:
        now = time.time()
        return (now - self.last_compact_time) > self.cooldown

    def record_compact(self):
        self.last_compact_time = time.time()
```

压缩后 60 秒内不再触发压缩，给对话足够的"增长空间"。

### 5.3 断路器模式

这是"断路器模式"（Circuit Breaker）的应用——在系统过载时主动熔断，防止雪崩。类似的例子：
- 电路的保险丝（电流过大时熔断）
- 微服务的断路器（依赖服务挂了时快速失败）
- 限流算法（请求过密时拒绝）

**核心思想**：宁可暂时不压缩（多花点 token），也不要频繁压缩（多花钱多延迟）。

---

## 6. 自动记忆提取：跨对话的长期记忆

### 6.1 怎么工作的？

Agent 每完成 5 轮对话（`MEMORY_EXTRACTION_INTERVAL = 5`），就在后台异步触发一次记忆提取：

```python
async def extract(self, client, conversation, protocol):
    # 1. 格式化最近对话
    formatted = self._format_conversation(conversation)

    # 2. 调 LLM 提取记忆
    response = await client.stream(...)
    # LLM 分析对话，按 4 分类提取：
    # - 用户偏好
    # - 纠正反馈
    # - 项目知识
    # - 参考资料

    # 3. 和已有记忆合并去重
    new_memories = self._parse_response(response)
    self._merge_memories(new_memories)

    # 4. 写入文件
    self._save()
```

### 6.2 记忆提取 Prompt

完整的 Prompt（`memory/auto_memory.py` 第 11-37 行）：

```
你是一个记忆提取助手。分析下面的对话，提取值得长期记忆的信息，更新 memories.md。

分类规则：
- **用户偏好**：用户的编码习惯和风格要求（如缩进、命名规范、语言偏好）
- **纠正反馈**：用户明确指出的错误和正确做法
- **项目知识**：当前项目的具体技术信息（技术栈、目录结构、部署方式）
- **参考资料**：外部链接和文档地址

规则：
1. 已有相同含义的条目不要重复添加
2. 没有值得记忆的内容，该分类下留空
3. 每条记忆用一行 `- ` 开头，必须是具体内容
4. 输出完整的 memories.md 内容

输出格式：
### 用户偏好
- 用户偏好简洁代码风格

### 纠正反馈

### 项目知识
- 项目使用 PostgreSQL 15

### 参考资料
```

### 6.3 两个存储位置

```python
USER_MEMORIES_RELPATH = ".codebot/memories.md"       # 用户级
PROJECT_MEMORIES_RELPATH = ".codebot/memories.md"    # 项目级
```

| 位置 | 路径 | 作用域 | 内容 |
|------|------|--------|------|
| 用户级 | `~/.codebot/memories.md` | 跨所有项目共享 | "用户偏好简洁代码" |
| 项目级 | `项目/.codebot/memories.md` | 只在此项目生效 | "这个项目用 PostgreSQL 15" |

**分类与位置的映射**：

```python
_USER_LEVEL_HEADERS = {"用户偏好", "纠正反馈"}      # 这两类放用户级
_PROJECT_LEVEL_HEADERS = {"项目知识", "参考资料"}   # 这两类放项目级
```

用户偏好和纠正反馈是跨项目的（用户的习惯不会因项目而变），放用户级。项目知识和参考资料是项目特定的，放项目级。

两个文件的内容在下次新对话开始时一并注入到 `history[0]` 位置。

### 6.4 关键设计：异步非阻塞

记忆提取不用 `await`，而是用 `asyncio.ensure_future()` 在后台执行：

```python
def _trigger_memory_extraction(self, conversation):
    if self._extracting:  # 防重入
        return
    self._extracting = True
    # 后台执行，不阻塞主循环
    asyncio.ensure_future(self._do_extract(conversation))
```

Agent 主循环不等它完成就继续处理下一轮。否则用户每 5 轮就要多等 2-3 秒等待 LLM 生成记忆摘要。

**为什么用 `ensure_future` 而不是 `create_task`？** 两者效果类似，但 `ensure_future` 更老式，兼容性更好。在现代 Python（3.7+）中推荐 `create_task`。

### 6.5 防重入设计

```python
if self._extracting:
    return  # 已经在提取了，跳过这次
self._extracting = True
```

**为什么要防重入？** 因为记忆提取是异步的，可能上一次还没完成就到了下一次触发点（第 10 轮）。如果不防重入，会启动多个并发的提取任务，可能互相覆盖写入文件。

**这是"幂等性"的设计**——即使触发多次，效果和触发一次一样。

### 6.6 记忆召回的语义检索（RAG 第一期）

`memory/recall.py::find_relevant_memories` 原本用 **LLM 选择器**挑记忆——每次都额外调一次 LLM，有延迟和成本，且只看 frontmatter description。`app.py::_prefetch_relevant_memories` 每轮都要等这个 side-query。

**RAG 第一期改造**：新增 `memory/semantic_recall.py::SemanticMemoryIndex`，用 embedding 余弦相似度替代 LLM 选择器：

| 维度 | LLM 选择器（原） | 语义检索（新，RAG 第一期） |
|------|----------------|------------------------|
| 延迟 | 2-3 秒（调 LLM） | 100ms（向量计算） |
| 成本 | 每次消耗 token | 仅 embedding 费用（便宜 10x） |
| 依据 | 只看 description | 看正文全文 |
| 可解释 | 黑盒（LLM 决策） | 有相似度分数 |

**接入方式**：`find_relevant_memories` 新增可选参数 `semantic_index`，优先走语义检索，失败/不可用/零命中时回退到 LLM 选择器——**向后兼容，渐进式降级**。

**增量索引**：和 CodeSearch 同思路，mtime 粗筛 + hash 确认，只重新 embedding 变化的记忆。

详见阶段7 §3。

---

## 7. 会话持久化

### 7.1 存储格式

对话以 JSONL（每行一个 JSON 对象）格式存在 `.codebot/sessions/{session_id}.jsonl`。

```jsonl
{"type": "user", "content": "帮我分析代码", "timestamp": "2025-07-20T10:00:00"}
{"type": "assistant", "content": "我来读取文件", "tool_uses": [...], "timestamp": "..."}
{"type": "tool_result", "tool_use_id": "tool_001", "content": "import os...", "timestamp": "..."}
{"type": "compact_boundary", "summary": "...", "keep": [...], "timestamp": "..."}
```

每条记录有类型标记（user / assistant / tool_result / compact_boundary）。

### 7.2 为什么用 JSONL 而不是 JSON？

| 格式 | 优点 | 缺点 |
|------|------|------|
| JSON | 结构清晰 | 整个文件一个 JSON，写入要全量重写 |
| JSONL | **追加写**（高效）、流式读取 | 每行独立，不能跨行引用 |

对话是**只追加**的数据流——每轮加几条记录，不修改历史。JSONL 的追加写特性完美匹配这个场景。

### 7.3 恢复机制

重新打开 CodeBot 时，可以恢复上次的会话。恢复流程：

1. 读取 `.codebot/sessions/{session_id}.jsonl`
2. 逐行解析 JSON
3. 重建 `ConversationManager.history`
4. 如果遇到 `compact_boundary` 记录，恢复压缩后的状态（摘要 + 尾部），不重放被压缩掉的前缀

**为什么只恢复压缩后的状态？** 因为被压缩掉的前缀已经"浓缩"进摘要了，重放它会造成重复。恢复摘要 + 尾部就够 LLM 理解上下文。

### 7.4 会话摘要

每次会话结束时，用 LLM 生成一句话摘要（"用户重构了 auth 模块，将 token 逻辑提取到独立服务"），作为会话列表的标题。

用户在 TUI 里可以查看历史会话列表，通过标题快速找到想要的会话。

### 7.5 ContentReplacementState 的持久化

`ContentReplacementState` 记录了哪些工具结果被替换成了磁盘引用：

```python
@dataclass
class ContentReplacementState:
    seen_ids: set[str] = field(default_factory=set)              # 已处理过的 tool_use_id
    replacements: dict[str, str] = field(default_factory=dict)   # {tool_use_id: 磁盘路径}
```

这些记录也持久化到 `replacement_records.jsonl`，恢复会话时重建替换状态——这样被替换的结果在恢复后还是磁盘引用，不会变成"找不到文件"。

---

## 8. 面试高频点与追问

### 8.1 "Context Window 有限，怎么管理长对话？"

双层压缩策略：

**Layer 1**（每次 LLM 调用前）：裁剪过大的工具结果
- 单条 > 5 万字符 → 截断为前 2000 字符
- 累计 > 20 万字符 → 最早的几条替换为"结果已保存在磁盘，路径是 xxx"

**Layer 2**（token 快超时才触发）：用 LLM 生成历史对话的结构化摘要
- 触发条件：token 数 ≥ context_window - 13K
- 保留近期约 1 万 token 原文
- 9 段结构化摘要（请求意图、关键技术、文件代码、错误修复等）

另外通过 RecoveryState 保存最近读取的文件快照，压缩后 LLM 仍保有工作上下文。

**追问：为什么是双层而不是单层？**

因为两层解决的问题不同：
- Layer 1 解决"单条工具结果过大"——不需要调 LLM，纯本地裁剪，快
- Layer 2 解决"对话整体太长"——需要调 LLM 做摘要，慢但效果好

只 Layer 1 不够（工具结果裁剪了，对话轮数还是多）；只 Layer 2 不够（每次都调 LLM 太慢）。两层配合，多数情况 Layer 1 就够，少数情况触发 Layer 2。

### 8.2 "Token 数怎么估算？不调用 tokenizer 能准吗？"

混合策略：

1. **精确锚点**：API 返回的 token 用量作为锚点（baseline_tokens + anchor_count）
2. **估算增量**：锚点之后的消息用字符数 ÷ 3.5 估算

误差仅在锚点后的小增量上，完全够用。不需要额外调 API 也不用引 tokenizer 库。

**追问：为什么是 3.5？**

经验值。英文约 4 字符/token，中文约 1-2 字符/token，混着用取 3.5 是折中。

**追问：3.5 这个值会不准吗？**

会，但影响很小。因为估算只用于"锚点后的增量"——这部分通常是几条消息，即使误差 20% 也只是几千 token。而锚点前用的是 API 精确值，完全准。最坏情况是提前/延后触发压缩，不会造成错误。

### 8.3 "跨对话怎么能记住用户偏好？"

自动记忆提取：每 5 轮对话，后台异步让 LLM 分析对话，按四类提取记忆：
- 用户偏好（如"喜欢 snake_case"）
- 纠正反馈（如"不准用 type: ignore"）
- 项目知识（如"用 PostgreSQL 15"）
- 参考资料（如"API 文档地址"）

写入 `~/.codebot/memories.md`（用户级）和 `项目/.codebot/memories.md`（项目级）。下次新对话启动时注入到开头。

**追问：为什么异步而不是同步？**

因为记忆提取要调 LLM，耗时 2-3 秒。如果同步，用户每 5 轮就要等。异步让主循环继续跑，提取在后台进行，用户无感。

**追问：为什么每 5 轮不是每轮？**

因为：
1. 每轮提取太频繁，浪费 token
2. 单轮对话信息量少，不值得提取
3. 5 轮通常能覆盖一个完整的工作单元（如"读文件→改代码→跑测试"）

### 8.4 "压缩时为什么保留近期原文？"

全部压缩会丢失精确信息（代码片段、文件路径、错误信息），导致无法继续 EditFile（old_string 必须精确）。保留最近约 1 万 token 的原文确保 LLM 仍有足够的上文进行精确操作。

**追问：1 万 token 是怎么定的？**

经验值。太少（如 2K）会让 LLM 忘记最近的工作；太多（如 5 万）会浪费压缩空间。1 万 token 约等于 5-15 条消息，能覆盖最近 2-3 轮交互——这是 LLM 最需要的上下文。

**追问：如果单条消息超过 1 万 token 怎么办？**

有 `KEEP_MAX_TOKENS = 40_000` 兜底——单条消息超过 4 万 token 时停止保留，把它纳入摘要。防止单条超大消息吞掉整个保留窗口。

### 8.5 "RecoveryState 是干什么的？"

压缩后 LLM 失去了之前读过的文件内容。RecoveryState 在每次读文件时缓存快照，压缩时把快照作为附件加到摘要后。这样 LLM 压缩后仍知道之前读过什么文件、内容是什么。

**追问：为什么标注'旧快照'？**

因为文件可能在压缩后被外部修改。标注让 LLM 知道内容可能过期，必要时重读。这是"乐观一致性"——默认没变，但提示可能变了。

### 8.6 "断路器是干什么的？"

防止频繁压缩。如果对话在阈值附近，可能反复触发压缩。断路器在压缩后 60 秒内不再触发，给对话足够的"增长空间"。

**追问：为什么是 60 秒？**

经验值。60 秒内用户通常会继续对话几轮，对话会增长到远离阈值。比 60 秒短可能不够增长，比 60 秒长可能错过必要的压缩。

### 8.7 "JSONL 比 JSON 好在哪？"

对话是只追加的数据流——每轮加几条记录，不修改历史。JSONL 的追加写特性完美匹配：
- **写入高效**：直接 append，不需要全量重写
- **流式读取**：可以只读最近 N 条，不用加载整个文件
- **容错好**：即使某一行损坏，其他行不受影响

JSON 需要把整个文件读进来、解析、修改、写回——对于长对话来说太慢。

### 8.8 "如果让你改进记忆系统，会怎么做？"

参考答案：

1. **向量检索**：目前记忆是全量注入，长记忆会占很多 token。可以用 embedding 做语义检索，只注入相关的记忆
2. **记忆衰减**：长期不用的记忆逐渐降低权重，避免积累太多过时信息
3. **记忆分类**：更细粒度的分类（如"用户的代码风格""项目架构""历史决策"）
4. **记忆审计**：让用户能查看、编辑、删除记忆
5. **跨项目记忆迁移**：从一个项目学到的通用知识迁移到新项目

---

> **下一步**：阶段5深入 Skill 技能包、子 Agent、Team 团队协作、MCP 协议和 Hook 系统。

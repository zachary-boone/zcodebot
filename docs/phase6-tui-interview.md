# 阶段6：TUI 交互层与面试专题

> **学习目标**：理解终端 UI 的架构、Worktree 隔离机制、项目架构精华总结、系统设计题回答框架、源码阅读题答案、行为问题 STAR 法则、速查手册。
> **预计时间**：3-4 天
> **前置要求**：完成阶段1-5
> **面试导向**：这是面试冲刺章。读完本章你能应对秋招面试官对 AI Agent 项目的全方位拷打——系统设计、源码、行为、手撕。

---

## 目录

1. [TUI 交互层：如何用 CSS 写终端应用](#1-tui-交互层如何用-css-写终端应用)
2. [Worktree：在隔离环境中安全操作](#2-worktree在隔离环境中安全操作)
3. [项目架构精华：一张图装下全部知识](#3-项目架构精华一张图装下全部知识)
4. [面试准备：系统设计题](#4-面试准备系统设计题)
5. [面试准备：源码阅读题](#5-面试准备源码阅读题)
6. [面试准备：行为问题（STAR 法则）](#6-面试准备行为问题star-法则)
7. [面试准备：手撕代码题](#7-面试准备手撕代码题)
8. [模拟面试：20 道高频题速答](#8-模拟面试20-道高频题速答)
9. [速查手册：核心类型 + 核心流程](#9-速查手册核心类型--核心流程)

---

## 1. TUI 交互层：如何用 CSS 写终端应用

### 1.1 Textual 是什么？

Textual 让你用**写 Web 应用的方式**写终端应用：

- **组件**：Button、TextArea、Markdown、OptionList... 像 HTML 标签一样
- **CSS**：用 TCSS 文件写样式（颜色、边框、布局），语法几乎和 CSS 一样
- **消息系统**：组件间通过消息通信（类似 DOM 事件）
- **响应式布局**：Vertical、Horizontal 等容器自动排列子组件
- **异步**：基于 asyncio，不阻塞 UI

### 1.2 为什么选 Textual 而不是 Rich/urwid？

| 框架 | 交互能力 | CSS 样式 | 异步 | 维护 |
|------|---------|---------|------|------|
| **Textual** | ✅ 完整（按钮、输入框、对话框） | ✅ TCSS | ✅ asyncio | ✅ 活跃 |
| Rich | ❌ 仅展示（无交互） | ❌ | ❌ | ✅ |
| urwid | ✅ | ❌ 自己造样式 | ❌ | ⚠️ 老旧 |

Textual 是 Rich 作者的后续作品，专门为"终端应用"设计。它的 CSS 系统让前端工程师能快速上手。

### 1.3 CodeBot 的 UI 结构

```
┌─────────────────────────────────────┐
│  Header (顶部栏，显示模式/Token用量)  │
├─────────────────────────────────────┤
│                                     │
│  聊天区 (可滚动)                      │
│  ┌─────────────────────────────┐    │
│  │ 用户: 帮我修复这个 bug        │    │
│  ├─────────────────────────────┤    │
│  │ Agent: 我来分析这个文件...     │    │
│  │ 🤔 思考过程 (可折叠)          │    │
│  │ 📋 ReadFile main.py ✅ 0.02s │    │
│  │ 📋 EditFile main.py ✅ 0.01s │    │
│  ├─────────────────────────────┤    │
│  │ Agent: 修好了！第42行死循环    │    │
│  └─────────────────────────────┘    │
│                                     │
├─────────────────────────────────────┤
│  ChatInput (输入框)                  │
│  [你的问题...]               [发送]  │
└─────────────────────────────────────┘
```

### 1.4 核心组件

| 组件 | 作用 | 对应 Web 概念 |
|------|------|-------------|
| `Header` | 顶部状态栏 | `<header>` |
| `ChatLog` | 聊天消息列表（可滚动） | `<div class="messages">` |
| `MessageBubble` | 单条消息气泡 | `<div class="message">` |
| `ToolCallBlock` | 工具调用卡片 | `<div class="tool-card">` |
| `ThinkingBlock` | 思考过程（可折叠） | `<details>` |
| `ChatInput` | 输入框 | `<textarea>` |
| `PermissionDialog` | 权限确认弹窗 | `<dialog>` |

### 1.5 输入框的贴心设计

- **@文件引用**：输入 `@src/main.py` 会自动读取文件内容并嵌入消息，LLM 直接看到代码
- **/命令补全**：输入 `/com` 按 Tab → 自动补全为 `/compact`
- **历史回溯**：按 ↑ 翻之前的输入
- **Shift+Enter** 换行，**Enter** 发送（符合聊天习惯）
- **Shift+Tab** 切换权限模式（default → acceptEdits → plan → bypass 循环）

### 1.6 流式渲染：打字机效果

Agent 不是一次性返回全部内容。每生成一个字，TUI 就在消息气泡末尾追加一个字——就像 AI 在跟你打字聊天。工具调用也实时显示："正在读 main.py..." → 完成后显示 ✅ 和耗时。

**实现原理**：

```python
# TUI 侧消费 Agent 事件
async for event in agent.run(conversation):
    if isinstance(event, StreamText):
        # 追加文字到当前消息气泡
        self.current_bubble.append_text(event.text)
    elif isinstance(event, ToolUseEvent):
        # 显示工具卡片
        self.add_tool_card(event.tool_name, event.arguments)
    elif isinstance(event, ToolResultEvent):
        # 更新工具卡片状态
        self.update_tool_card(event.tool_id, event.output, event.elapsed)
    elif isinstance(event, PermissionRequest):
        # 弹权限对话框
        self.show_permission_dialog(event)
```

### 1.7 @文件引用的实现

```python
def parse_input(self, text: str) -> str:
    """解析 @文件引用，把文件内容嵌入消息。"""
    import re
    pattern = r'@([\w/.-]+)'

    def replace_match(m):
        file_path = m.group(1)
        if os.path.exists(file_path):
            content = open(file_path).read()
            return f'```\n{file_path}:\n{content}\n```'
        return m.group(0)

    return re.sub(pattern, replace_match, text)
```

用户输入 `@main.py` → 自动替换为文件内容（带代码块标记）→ LLM 直接看到代码。

---

## 2. Worktree：在隔离环境中安全操作

### 2.1 什么是 Git Worktree？

Git Worktree 让你在**同一个仓库**里同时拥有多个工作目录。比如主目录在 main 分支上，另开一个 worktree 在 feature-x 分支上——互不影响。

```
主目录:    /project/                     (branch: main, 你正常工作的位置)
Worktree:  /project/.codebot/worktrees/  (branch: feature-x, Agent 工作的位置)
```

### 2.2 Agent 什么时候进 Worktree？

当你让 Agent 做可能有副作用的操作时（大规模重构、实验性改动），它可以调用 `EnterWorktree` 进入隔离的工作树。操作完成后调用 `ExitWorktree` 回来。

**典型场景**：
- "帮我重构这个模块"——重构可能搞砸，在 worktree 里试，不行就丢
- "试试用新框架重写"——实验性，在 worktree 里做
- "修复这个 bug，但不要影响我现在的工作"——隔离

### 2.3 Worktree 怎么创建的？

1. `git worktree add` 创建新工作目录
2. 复制本地配置文件（.env 等）
3. 设置 Git hooks
4. 软链接大目录（.venv、node_modules），避免重复安装依赖

### 2.4 为什么要软链接 .venv 和 node_modules？

这些目录：
- 体积大（几百 MB 到几 GB）
- 安装慢（npm install 可能要几分钟）
- 和工作目录无关（依赖是全局的）

软链接让 worktree 共享主目录的依赖，不用重新安装。**秒级创建 worktree 而不是分钟级。**

### 2.5 自动清理

有一个后台任务每小时检查所有 worktree。超过 24 小时没用的自动删除——防止积累一堆废弃工作树。

```python
# worktree/cleanup.py
async def cleanup_stale_worktrees(interval: int = 3600, cutoff_hours: int = 24):
    while True:
        await asyncio.sleep(interval)
        for wt in list_all_worktrees():
            if wt.last_used < datetime.now() - timedelta(hours=cutoff_hours):
                wt.remove()  # git worktree remove
```

**为什么 24 小时？** 经验值。太短（如 1 小时）可能 Agent 还在用就被清理了；太长（如 7 天）会积累太多废弃 worktree。24 小时覆盖一个工作日，够用。

---

## 3. 项目架构精华：一张图装下全部知识

### 3.1 五层 + 数据流

```
你输入文字
    ↓
交互层（Textual TUI）: 展开@引用、识别/命令、流式渲染
    ↓
引擎层（Agent）: ReAct Think→Act→Observe→Loop
    ↓                   ↓
工具层（Tools）:    安全层（Permissions）:
  ReadFile              Plan模式例外
  EditFile              安全白名单
  Bash                  危险黑名单
  Grep/Glob             路径沙箱
  Agent/Team            规则引擎
  LoadSkill             模式矩阵
  MCP工具               人工确认
    ↓                   ↑
记忆层（Memory）: 长期记忆注入 + 上下文压缩 + 会话持久化
```

### 3.2 关键数字

| 指标 | 值 |
|------|-----|
| 总代码量 | ~16,000 行 Python（含 RAG 子系统） |
| 核心文件 | agent.py (1300行), app.py (1900行), client.py (800行) |
| 内置工具 | 21+（含 CodeSearch 语义搜索） |
| 内置命令 | 14 个斜杠命令 |
| 内置 Skill | 4 个（commit, review, test, backend-interview） |
| 内置子 Agent | 4 个（explore, plan, verification, general-purpose） |
| 安全层数 | 6 层（Plan例外 → 白名单 → 黑名单 → 沙箱 → 规则 → 模式 → 确认） |
| 支持协议 | 3 种（Anthropic Messages / OpenAI Responses / OpenAI Chat Completions） |
| 生命周期事件 | 12 种（Hook） |
| RAG 子系统 | 8 模块（embedding/chunker/qdrant_store/indexer/bm25/fusion/reranker + semantic_recall） |
| 测试文件 | 20 个（17 核心 + 3 RAG，共 63 个测试用例） |

### 3.3 九个关键技术决策

| 决策 | 理由 | 替代方案与不选的原因 |
|------|------|---------------------|
| 分层而非扁平 | 每层可独立开发测试，修改不影响其他层 | 扁平（耦合严重） |
| 策略模式做多协议 | 新增模型只需加一个客户端类 | litellm（牺牲 Anthropic 特性） |
| Pydantic 做参数校验 | 一套代码同时生成 Schema（给 LLM）+ 校验参数（给自己） | 手写 JSON Schema（易错） |
| 注册表模式做扩展 | 工具/Skill/Agent/命令都可以运行时注册和发现 | 硬编码（不可扩展） |
| Future + await 做权限确认 | Agent 代码自然流动，不阻塞 TUI | 回调（回调地狱）、同步阻塞（卡死） |
| 文件系统做团队通信 | 本地 Agent 无需网络开销，消息不丢失 | Redis（重依赖） |
| 双层压缩做长对话 | Layer1 裁剪结果 + Layer2 摘要历史 | 单层（不够灵活） |
| **AST 分块做代码 RAG** | 按函数/类切保证语义完整，是代码 RAG 区别于文档 RAG 的核心 | 固定字符切（切碎函数，语义破碎） |
| **RRF 做混合检索融合** | 只看排名天然归一化，向量分数（0-1）和 BM25 分数（无上界）量纲不同无需调参 | 加权求和（难调参） |
| **Qdrant 嵌入式做向量库** | 免起服务 clone 即用，payload 过滤是代码 RAG 刚需，留 url 开关可伸缩 | faiss（无元数据过滤+要起服务） |

### 3.4 五大正交扩展机制

| 机制 | 解决问题 | 实现方式 |
|------|---------|---------|
| 工具 | Agent 能做什么 | Tool 基类 + 注册表 |
| Skill | Agent 怎么做 | SOP 注入提示词 |
| MCP | 接入外部生态 | 标准协议 + ToolWrapper |
| Hook | 自动触发 | 生命周期事件 + Shell 命令 |
| 子 Agent | 任务拆分 | Fork/SubAgent/Teammate |

---

## 4. 面试准备：系统设计题

### 4.1 "设计一个 AI Coding Agent"

**这是必考题，回答框架如下：**

**第一层：需求澄清（1 分钟）**

- 功能需求：终端运行，接受自然语言任务，自主推理 + 执行工具 + 观察结果 + 循环迭代
- 非功能需求：安全第一（不能 rm -rf /）、多模型支持、处理长对话（context window 有限）
- 约束：Python 实现、终端 UI

**第二层：架构（画五层图，2 分钟）**

```
交互层：TUI（Textual — CSS 样式终端框架）
引擎层：ReAct 循环（Think → Act → Observe → Loop）+ Plan 模式
工具层：统一 Tool 接口 + 注册表 + 延迟加载
记忆层：自动记忆提取 + 上下文压缩 + 会话持久化
安全层：六道防线 + 人工确认
```

**第三层：核心流程（2 分钟）**

```
用户输入 → 注入环境上下文和长期记忆 → ReAct 循环
  → LLM 思考和输出 → 安全检查和工具执行 → 结果返回 → 循环
  → 长对话自动压缩 → 任务完成
```

**第四层：关键挑战与解法（2 分钟）**

| 挑战 | 解法 |
|------|------|
| Context Window 管理 | 双层压缩（tool result budget + 摘要）+ RecoveryState |
| 工具安全性 | 六层纵深防御 + 人工确认 |
| 多协议适配 | 策略模式 + 自己实现抽象层 |
| 扩展性 | 五种正交扩展机制 |
| 流式输出 | 异步生成器 + StreamCollector |
| 权限确认不阻塞 | Future + await |

**第五层：扩展性（1 分钟）**

五大正交扩展：工具（加能力）、Skill（改行为）、MCP（接生态）、Hook（自动化）、子 Agent（分治）。

### 4.2 "怎么处理 LLM 的幻觉问题？"

六层防线：

1. **强制执行工具**：System prompt 强调 "不要假设代码内容，先读文件"
2. **唯一性约束**：EditFile 的 old_string 必须唯一匹配
3. **文件状态检查**：编辑前验证文件未被外部修改
4. **幻觉保护**：连续 3 次调用不存在的工具 → 强制终止
5. **人工确认**：高风险操作需要用户审核

**追问：为什么是 3 次不是 1 次？**

1 次可能是笔误，2 次可能是重试，3 次基本确定是幻觉。给 LLM 一定的容错空间，避免误杀。

### 4.3 "怎么保证系统安全？"

**纵深防御**：六层各有职责，任一层说 "不" 就停止。

1. Plan 模式例外（拦截大部分写操作）
2. 安全命令白名单 + 危险命令黑名单
3. 路径沙箱（只能在项目目录和临时目录操作）
4. 用户自定义规则
5. 权限模式控制（default/acceptEdits/plan/bypass）
6. 人工确认（HITL）

**关键原则**：纵深防御——即使某一层有漏洞，后续层仍能拦截。

### 4.4 "如果让你设计多 Agent 协作系统，怎么设计？"

**架构**：Coordinator 模式

```
Coordinator（和用户对话）
    ├── Worker1（写代码）
    ├── Worker2（测试）
    └── Worker3（审查）
         ↓
    Mailbox（文件系统消息队列）
```

**关键设计**：
1. **角色分工**：每个 Worker 有明确的工具集和职责
2. **消息队列**：文件系统实现，无依赖、跨进程、消息不丢失
3. **任务分配**：Coordinator 拆分任务，派发给 Worker
4. **结果合并**：Coordinator 收集 Worker 结果，合并后汇报用户

**追问：为什么不用 Actor 模型？**

Actor 模型（如 Erlang/Akka）更强大，但：
1. 引入重依赖（Akka 需要 JVM）
2. Actor 模型的信件箱语义和我们的需求不匹配
3. 文件系统方案更简单，够用

YAGNI 原则——简单方案能解决就不要过度设计。

### 4.5 "怎么给 Coding Agent 加语义代码搜索？"

**回答框架**（详见阶段7）：

**第一层：痛点**
- 原本靠 Grep 关键词搜索，存在"词汇鸿沟"——用户说"登录鉴权"，代码叫 `verify_token`，搜不到

**第二层：方案（RAG 完整链路）**
- 索引：AST 按函数/类分块 → embedding → Qdrant 向量库
- 检索：向量召回 Top-20 + BM25 关键词召回 Top-20 → RRF 融合 → Top-5

**第三层：关键决策**
- AST 分块（不是固定字符切）——保证语义完整
- 混合检索（不是纯向量）——向量漏精确关键词，BM25 漏语义，RRF 融合取长补短
- Qdrant 嵌入式（不是 faiss+服务）——免起服务，payload 过滤是代码 RAG 刚需
- 增量索引（mtime+hash 两级）——不全量重建
- 全链路降级——RAG 是增强不是依赖

**追问"RRF 为什么不用加权求和"**：向量分数（0-1）和 BM25 分数（无上界）量纲不同，RRF 只看排名天然归一化，无需调参。详见阶段7 §11 Q3。

---

## 5. 面试准备：源码阅读题

### 5.1 "ReAct 循环是怎么实现的？"

`Agent.run()` 是一个异步生成器，主循环是 `while True`：

```python
async def run(self, conversation) -> AsyncIterator[AgentEvent]:
    # 1. 装载上下文（循环前）
    conversation.inject_environment(env_context)
    conversation.inject_long_term_memory(...)

    while True:
        # 2. 循环头：Hook + 邮箱 + 自动压缩
        await self.hook_engine.run_hooks("turn_start", ...)
        self._consume_mailbox(conversation)
        await auto_compact(...)

        # 3. Think：调 LLM
        stream = self.client.stream(...)
        collector = StreamCollector()
        async for event in collector.consume(stream):
            yield event  # 实时推给 TUI

        # 4. 处理响应：max_tokens 恢复 / 无工具调用则结束
        if collector.response.stop_reason == "max_tokens":
            # 三层恢复...
            continue
        if not collector.response.tool_calls:
            break  # 任务完成

        # 5. Act：执行工具（分区并发）
        batches = partition_tool_calls(collector.response.tool_calls, registry)
        for batch in batches:
            results = await self._execute_batch(batch)
            conversation.add_tool_results(results)

        # 6. Loop：turn_end Hook，回到第 2 步
        await self.hook_engine.run_hooks("turn_end", ...)
        yield TurnComplete(turn=iteration)
```

四种终止条件：无工具调用 / 超 50 轮 / 连续 3 次幻觉 / 退出 Plan 模式。

### 5.2 "三种 LLM 客户端怎么统一？"

定义 `LLMClient` 抽象基类，只有一个 `stream()` 方法。三个实现类各自负责：
- 把内部 Message 格式转成自己 API 的请求格式（序列化）
- 把自己 API 的流式事件映射为内部统一的 7 种 StreamEvent（反序列化）
- 把原生异常映射为内部统一的 4 种异常

Agent 只调 `stream()`，完全不知道底层是谁。

**关键代码**：

```python
class LLMClient(ABC):
    @abstractmethod
    async def stream(self, conversation, system, tools) -> AsyncIterator[StreamEvent]:
        ...
```

### 5.3 "长对话怎么压缩？"

**触发条件**：token 数 ≥ context_window - 13K

**步骤**：
1. **分割**：prefix（待压缩）+ keep（保留原文约 1 万 token）
2. **生成摘要**：LLM 用 9 段结构化 Prompt 压缩 prefix
3. **附件恢复**：文件快照 + 激活技能
4. **替换历史**：摘要 + 附件 + keep 替换旧历史

**保护机制**：断路器防频繁压缩（60 秒内不重复压缩）。

### 5.4 "权限检查的六层是怎么走的？"

```python
def check(self, tool, arguments) -> Decision:
    # Layer 0: Plan 模式例外
    if self.mode == PLAN: ...

    # Layer 1: 安全白名单
    if is_safe_command(content): return ALLOW

    # Layer 1b: 危险黑名单
    if self.detector.detect(content): return DENY

    # Layer 2: 路径沙箱
    if not self.sandbox.check(content): return DENY

    # Layer 3: 规则引擎
    if rule_result := self.rule_engine.evaluate(...): return rule_result

    # Layer 4: 模式矩阵
    if effect := mode_decide(self.mode, tool.category) != "ask": return effect

    # Layer 5: 人工确认
    return ASK
```

**核心设计**：短路求值——任一层做出决定就立即返回。

---

## 6. 面试准备：行为问题（STAR 法则）

### 6.1 "项目中最大的技术挑战？"

**STAR 法则回答**：

- **S (Situation)**：Context Window 只有 20 万 token，长对话会爆。这是所有 LLM 应用的核心难题。
- **T (Task)**：让 Agent 能处理长对话不中断，同时保留工作上下文。
- **A (Action)**：设计了三层方案：
  1. **双层压缩**：Layer 1 裁剪工具结果（每次调用前），Layer 2 LLM 摘要历史（接近上限时）
  2. **RecoveryState**：压缩前缓存文件快照，压缩后作为附件恢复
  3. **断路器**：60 秒内不重复压缩，防止频繁压缩影响体验
- **R (Result)**：单次对话能处理上千轮交互，压缩后 LLM 仍保有工作上下文，用户无感。

**追问：压缩质量怎么保证？**

9 段结构化摘要 Prompt（请求意图、关键技术、文件代码、错误修复、用户消息原文、待办、当前工作、下一步、关键决策）。结构化让 LLM 不遗漏关键信息，也便于压缩后快速定位。

### 6.2 "怎么保证代码质量？"

- 全项目类型标注（PEP 604 `X | Y` 语法）
- Pydantic 自动参数校验
- 20 个测试文件覆盖核心模块与 RAG 链路（63 个测试用例）
- LLM 异常全映射为内部异常（AuthenticationError/RateLimitError/NetworkError）
- Hook 自动跑 linter（ruff check + ruff format）
- dataclass + Enum 让数据结构清晰

**追问：测试覆盖率多少？**

核心模块（agent、client、permissions、context、memory）覆盖率约 70%。UI 层（app.py）因为依赖 Textual 运行时，测试较难，覆盖率较低。

### 6.3 "从项目中学到了什么？"

1. **分层架构让每层独立开发和测试**——改 UI 不影响引擎，加工具不影响安全
2. **注册表模式让扩展变得极其简单**——加新工具只需新增类 + 一行注册
3. **异步编程在 Agent 场景不可或缺**——流式 LLM + 并发工具 + 异步权限确认
4. **安全是 Agent 系统的第一优先级**——不能事后打补丁，要从架构层面设计
5. **多协议适配的关键是"自己定义接口，让三方适配"**——而不是用第三方统一层
6. **正交扩展是系统可维护性的关键**——五个维度独立演进，互不影响

### 6.4 "遇到最难的 bug 是什么？"

**STAR 回答**：

- **S**：早期版本，长对话压缩后 LLM 突然"失忆"——忘记刚读过的文件，重复读
- **T**：找出失忆原因并修复
- **A**：
  1. 日志发现：压缩后 LLM 反复读同一个文件
  2. 分析：压缩把文件内容丢了，LLM 不知道之前读过
  3. 设计 RecoveryState：压缩前缓存文件快照，压缩后作为附件恢复
  4. 调试：发现快照太多会占满 context，加 LRU 淘汰
- **R**：压缩后 LLM 保有工作上下文，不再重复读文件

**学到的**：异步系统的 bug 难调——状态在多个协程间流转。需要好的日志和可观测性。

### 6.5 "为什么做这个项目？"

**真诚版回答**：

> "我对 AI Agent 这个方向很感兴趣，但市面上的工具要么不开源（Claude Code）、要么功能单一（Aider）。我想自己实现一个完整的 Agent 系统，深入理解 ReAct 循环、工具系统、安全模型、上下文管理这些核心技术。做完后发现不仅学到了 AI 应用开发的精髓，还练了异步编程、设计模式、系统架构。这个项目是我秋招的核心作品集。"

---

## 7. 面试准备：手撕代码题

### 7.1 手撕：实现一个简单的 Tool 基类

```python
from abc import ABC, abstractmethod
from pydantic import BaseModel
from dataclasses import dataclass
from typing import Any, Literal

ToolCategory = Literal["read", "write", "command"]

@dataclass
class ToolResult:
    output: str
    is_error: bool = False

class Tool(ABC):
    name: str
    description: str
    params_model: type[BaseModel]
    category: ToolCategory = "read"
    is_concurrency_safe: bool = False

    def get_schema(self) -> dict[str, Any]:
        schema = self.params_model.model_json_schema()
        schema.pop("title", None)
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
        }

    @abstractmethod
    async def execute(self, params: BaseModel) -> ToolResult: ...


# 具体工具示例
from pydantic import Field

class ReadFileParams(BaseModel):
    file_path: str = Field(..., description="要读的文件路径")
    offset: int = Field(0, description="从第几行开始")
    limit: int = Field(2000, description="最多读几行")

class ReadFile(Tool):
    name = "ReadFile"
    description = "读取文件内容"
    params_model = ReadFileParams
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: ReadFileParams) -> ToolResult:
        try:
            with open(params.file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            # 行号标注
            start = params.offset
            end = start + params.limit
            output = ''.join(f"{i+1}→{line}" for i, line in enumerate(lines[start:end], start))
            return ToolResult(output=output)
        except FileNotFoundError:
            return ToolResult(output=f"文件不存在: {params.file_path}", is_error=True)
```

### 7.2 手撕：实现工具分区并发

```python
from dataclasses import dataclass
import asyncio

@dataclass
class ToolCall:
    tool_name: str
    arguments: dict
    is_concurrency_safe: bool

@dataclass
class ToolBatch:
    concurrent: bool
    calls: list[ToolCall]

def partition_tool_calls(calls: list[ToolCall]) -> list[ToolBatch]:
    """把工具调用分成可并发批和串行批。"""
    batches: list[ToolBatch] = []
    for call in calls:
        if call.is_concurrency_safe and batches and batches[-1].concurrent:
            batches[-1].calls.append(call)
        else:
            batches.append(ToolBatch(concurrent=call.is_concurrency_safe, calls=[call]))
    return batches

# 测试
calls = [
    ToolCall("ReadFile", {"path": "a.py"}, True),
    ToolCall("ReadFile", {"path": "b.py"}, True),
    ToolCall("Bash", {"cmd": "rm xxx"}, False),
    ToolCall("ReadFile", {"path": "c.py"}, True),
]
batches = partition_tool_calls(calls)
# 输出：
# [ToolBatch(concurrent=True, calls=[ReadFile(a.py), ReadFile(b.py)]),
#  ToolBatch(concurrent=False, calls=[Bash(rm xxx)]),
#  ToolBatch(concurrent=True, calls=[ReadFile(c.py)])]
```

### 7.3 手撕：实现 Future 权限确认

```python
import asyncio

class PermissionRequest:
    def __init__(self, tool_name: str, future: asyncio.Future):
        self.tool_name = tool_name
        self.future = future

async def agent_execute_with_permission(tool_name: str):
    """Agent 侧：需要权限确认时挂起。"""
    future = asyncio.Future()
    # 这里应该 yield PermissionRequest 给 TUI
    # 模拟 TUI 响应
    asyncio.ensure_future(simulate_tui_respond(future))
    # 等待用户确认
    response = await future
    if response == "allow":
        print(f"执行 {tool_name}")
    else:
        print(f"拒绝 {tool_name}")

async def simulate_tui_respond(future: asyncio.Future):
    """模拟 TUI 侧：用户点击 Allow。"""
    await asyncio.sleep(1)  # 模拟用户思考
    future.set_result("allow")

# 测试
asyncio.run(agent_execute_with_permission("EditFile"))
```

### 7.4 手撕：实现简单的 Token 估算

```python
def estimate_tokens(text: str) -> int:
    """基于字符数估算 token 数。"""
    CHARS_PER_TOKEN = 3.5
    return int(len(text) / CHARS_PER_TOKEN)

class ConversationManager:
    def __init__(self):
        self.history = []
        self.baseline_tokens = 0  # API 报告的真实值
        self.anchor_count = 0     # 锚点时的消息数

    def record_anchor(self, tokens: int, count: int):
        """API 返回后锚定。"""
        self.baseline_tokens = tokens
        self.anchor_count = count

    def current_tokens(self) -> int:
        """估算当前总 token 数。"""
        if self.baseline_tokens == 0:
            # 冷启动：全字符估算
            total_chars = sum(len(m) for m in self.history)
            return estimate_tokens(total_chars)
        # 锚点前用 API 数据，锚点后用字符估算
        after_anchor = self.history[self.anchor_count:]
        after_chars = sum(len(m) for m in after_anchor)
        return self.baseline_tokens + estimate_tokens(after_chars)
```

---

## 8. 模拟面试：20 道高频题速答

### 8.1 基础概念题

**Q1：ReAct 是什么？**
A：Reasoning + Acting 的缩写，LLM 边推理边调用工具的范式。CodeBot 的默认模式。四个阶段：Think → Act → Observe → Loop。

**Q2：Plan Mode 和 ReAct 的区别？**
A：ReAct 直接执行，Plan Mode 先规划再执行。Plan Mode 下 Agent 只能读代码和写计划文件，其他写操作全被拦截。

**Q3：Context Window 是什么？**
A：LLM 一次能"看到"的最大 token 数。Claude 是 20 万，GPT-4o 是 12.8 万。超了就要压缩。

**Q4：Function Calling 是什么？**
A：LLM 调用外部工具的机制。LLM 收到工具的 JSON Schema，决定调用哪个工具、传什么参数，返回结构化的工具调用请求。

### 8.2 架构题

**Q5：为什么分五层？**
A：关注点分离。每层独立开发测试，修改不影响其他层。不分层会导致 UI、业务、工具逻辑混在一起，改一点牵动全身。

**Q6：层间怎么通信？**
A：通过明确的数据结构（AgentEvent/ToolResult/Decision/Message），不直接调用对方内部方法。数据驱动通信，便于独立测试。

**Q7：为什么用策略模式做多协议？**
A：新增模型只需加一个客户端类，核心代码不动。如果用 litellm 会牺牲 Anthropic 的 Prompt Caching 和 Extended Thinking。

**Q8：为什么用 Future 做权限确认？**
A：代码自然流动（确认前后在同一个函数），变量都能直接访问。用回调会拆成两段，用同步阻塞会卡死 TUI。

### 8.3 安全题

**Q9：怎么防止 rm -rf /？**
A：六层纵深防御——白名单（rm 不在内）+ 黑名单（正则匹配）+ 沙箱（/ 不在允许路径）+ 规则引擎 + 模式矩阵 + 人工确认。

**Q10：沙箱怎么防符号链接越狱？**
A：`Path.resolve(strict=True)` 解析所有符号链接成真实路径，再检查是否在允许目录内。

**Q11：EditFile 为什么用 search/replace？**
A：安全（精确匹配不误伤）+ 冲突检测（找不到说明文件被外部修改）+ 可审计（diff 清晰）。

**Q12：权限模式有几种？**
A：6 种——default、acceptEdits、plan、bypass、custom、dontAsk。日常用 default，信任编辑用 acceptEdits，高风险用 plan。

### 8.4 上下文题

**Q13：长对话怎么处理？**
A：双层压缩。Layer 1 每次调用前裁剪工具结果（单条 > 5 万字符截断）。Layer 2 接近上限时 LLM 摘要历史，保留近期 1 万 token 原文。

**Q14：Token 怎么估算？**
A：混合策略。API 返回的精确值作为锚点，锚点后的消息用字符数 ÷ 3.5 估算。误差仅在增量上，够用。

**Q15：压缩后怎么不丢上下文？**
A：RecoveryState。压缩前缓存文件快照，压缩后作为附件加到摘要后。LLM 仍知道之前读过什么文件。

**Q16：跨对话怎么记忆？**
A：自动记忆提取。每 5 轮后台异步让 LLM 提取记忆，分 4 类（用户偏好/纠正反馈/项目知识/参考资料），写入文件。下次对话注入开头。

### 8.5 扩展题

**Q17：五大扩展机制是什么？**
A：工具（加能力）、Skill（改行为）、MCP（接生态）、Hook（自动化）、子 Agent（分治）。正交设计，互不影响。

**Q18：MCP 和内置工具有什么区别？**
A：内置工具是 Python 代码写死在项目里，MCP 工具来自外部进程（任何语言）。对 LLM 来说两者地位完全平等。

**Q19：子 Agent 的三种模式？**
A：SubAgent（全新空历史）、Fork（复制父 Agent 历史）、Teammate（长期运行，非阻塞）。

**Q20：Hook 能做什么？**
A：在 12 个生命周期事件自动执行脚本。pre_tool_use 甚至能拒绝执行——给用户自定义安全网。

---

## 9. 速查手册：核心类型 + 核心流程

### 9.1 核心类型速查

| 类型 | 文件 | 一句话 |
|------|------|--------|
| `Agent` | agent.py | 核心循环 |
| `AgentEvent` | agent.py | 12 种事件的联合类型 |
| `LLMClient` | client.py | LLM 的抽象接口（只有 stream()） |
| `StreamEvent` | tools/base.py | 7 种流式事件的联合类型 |
| `Tool` | tools/base.py | 工具模板（6 个属性） |
| `ToolResult` | tools/base.py | 工具返回（output + is_error） |
| `ToolRegistry` | tools/__init__.py | 工具注册表 |
| `ConversationManager` | conversation.py | 消息历史管理 + Token 锚定 |
| `Message` | conversation.py | 单条消息（content + tool_uses + tool_results + thinking_blocks） |
| `PermissionChecker` | permissions/checker.py | 六层安全检查 |
| `PermissionMode` | permissions/modes.py | 6 种权限模式 |
| `Decision` | permissions/checker.py | 检查结果（effect + reason） |
| `SkillLoader` | skills/loader.py | 技能加载与热重载 |
| `HookEngine` | hooks/engine.py | 生命周期钩子 |
| `MCPManager` | mcp/manager.py | MCP 连接管理 |
| `TeamManager` | teams/manager.py | 团队管理 |
| `Mailbox` | teams/mailbox.py | 文件系统消息队列 |
| `WorktreeManager` | worktree/manager.py | 工作树隔离 |
| `CompactBoundary` | context/manager.py | 压缩结果（summary + keep） |
| `RecoveryState` | context/manager.py | 文件快照恢复 |
| `MemoryManager` | memory/auto_memory.py | 自动记忆提取 |
| **RAG 子系统（详见阶段7）** | | |
| `EmbeddingProvider` | rag/embedding.py | embedding 抽象接口（策略模式） |
| `cosine_similarity` | rag/embedding.py | 余弦相似度 |
| `CodeChunk` | rag/chunker.py | 代码块（含稳定点 ID） |
| `QdrantCodeStore` | rag/qdrant_store.py | Qdrant 向量存储（嵌入式/Server） |
| `IncrementalIndexer` | rag/indexer.py | mtime+hash 两级增量索引 |
| `BM25Index` | rag/bm25.py | 自实现 BM25 关键词召回 |
| `reciprocal_rank_fusion` | rag/fusion.py | RRF 融合算法 |
| `SemanticMemoryIndex` | memory/semantic_recall.py | 记忆语义检索（第一期） |
| `CodeSearch` | tools/code_search.py | 语义代码搜索工具（第二期） |

### 9.2 6 条核心流程

| 流程 | 入口 | 关键步骤 |
|------|------|---------|
| 用户请求 | `app._on_chat_submitted()` | @展开 → /命令 → Agent.run() |
| Agent 循环 | `Agent.run()` | 注入 → LLM 调用 → 工具执行 → 循环 |
| LLM 调用 | `client.stream()` | 序列化 → API 调用 → 事件映射 |
| 工具执行 | `Agent._execute_tool()` | 安全检查 → 确认 → 执行 → Hook |
| 上下文压缩 | `auto_compact()` | 判断 → 分割 → 摘要 → 附件 → 替换 |
| 记忆提取 | `MemoryManager.extract()` | 格式化 → LLM 分析 → 写入 |

### 9.3 关键常量速查

| 常量 | 值 | 含义 |
|------|-----|------|
| `max_iterations` | 50 | Agent 最大循环轮次 |
| `MEMORY_EXTRACTION_INTERVAL` | 5 | 每 5 轮触发记忆提取 |
| `MAX_TOKENS_CEILING` | 64000 | max_tokens 恢复上限 |
| `MAX_OUTPUT_TOKENS_RECOVERIES` | 3 | max_tokens 恢复重试次数 |
| `SINGLE_RESULT_CHAR_LIMIT` | 50000 | 单条工具结果截断阈值 |
| `AGGREGATE_CHAR_LIMIT` | 200000 | 合计工具结果截断阈值 |
| `KEEP_RECENT_TOKENS` | 10000 | 压缩时保留近期 token 数 |
| `AUTO_COMPACT_SAFETY_MARGIN` | 13000 | 自动压缩安全边距 |
| `_CHARS_PER_TOKEN` | 3.5 | Token 估算的字符比 |

### 9.4 学习完成检查清单

完成全部 6 个阶段后，你应该能够：

- [ ] 画出五层架构图并解释每层职责
- [ ] 讲清楚 ReAct 循环的四个阶段和四种终止条件
- [ ] 说明多协议适配的设计思路（策略模式 + 序列化独立）
- [ ] 描述一个 Tool 的完整创建流程（继承基类 + 实现 execute + 注册）
- [ ] 说出六层安全检查的顺序和职责
- [ ] 解释双层压缩的触发条件和执行步骤
- [ ] 说明 Token 混合估算的原理（锚定 + 字符估算）
- [ ] 区分三种子 Agent 模式的适用场景
- [ ] 解释 Skill 的加载和激活流程
- [ ] 说明 MCP 协议在项目中的角色
- [ ] 回答"设计一个 AI Coding Agent"系统设计题
- [ ] 讲出项目中最复杂的技术挑战和解决思路
- [ ] 手撕 Tool 基类、工具分区、Future 权限确认
- [ ] 说出五大正交扩展机制各自解决的问题
- [ ] 解释 RecoveryState 和断路器的作用
- [ ] 讲清楚 RAG 完整链路（索引 + 检索），能推导余弦相似度/BM25/RRF 三个公式（阶段7）
- [ ] 解释为什么 AST 分块、为什么混合检索、为什么 Qdrant 嵌入式（阶段7）
- [ ] 手撕 cosine_similarity、RRF 融合、AST 分块（阶段7 §12）

### 9.5 面试时记住三句话

1. **先讲架构，再讲细节，始终和实际场景关联。**
2. **每个设计决策都要能说出"为什么这么做"和"不这么做会怎样"。**
3. **项目亮点 = 技术深度 × 业务价值 × 你的贡献。**

---

> 🎉 **恭喜！** 六个阶段全部完成。下一步：打开源码配合文档一起阅读，每读完一个模块尝试写一段总结。
>
> **面试时记住：先讲架构，再讲细节，始终和实际场景关联。**
>
> **秋招加油！**

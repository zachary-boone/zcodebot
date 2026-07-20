# 阶段2：核心引擎深入 — ReAct 循环与 LLM 客户端

> **学习目标**：理解 Agent 主循环的每一步代码实现、12 种事件如何驱动 UI、三种 LLM 协议如何统一适配、流式输出与权限确认的异步机制。读完本章你能讲清楚"Agent 是怎么跑起来的"。
> **预计时间**：3-4 天（每天 2-3 小时）
> **前置要求**：完成阶段1，理解整体架构；熟悉 Python async/await、asyncio.Future、异步生成器（`async def` + `yield`）
> **面试导向**：这是项目的"心脏"，面试官最爱深挖。读完本章你能回答"ReAct 循环怎么实现的""多协议怎么适配""流式输出怎么做"等核心问题。

---

## 目录

1. [Agent 的初始化与状态](#1-agent-的初始化与状态)
2. [ReAct 主循环逐步分解（带源码）](#2-react-主循环逐步分解带源码)
3. [事件驱动架构：Agent 如何和 UI 对话](#3-事件驱动架构agent-如何和-ui-对话)
4. [三协议适配：三种 API 如何统一](#4-三协议适配三种-api-如何统一)
5. [工具调用如何分区并发执行](#5-工具调用如何分区并发执行)
6. [max_tokens 截断的恢复策略](#6-max_tokens-截断的恢复策略)
7. [Anthropic 独有功能深度剖析](#7-anthropic-独有功能深度剖析)
8. [面试高频点与追问](#8-面试高频点与追问)

---

## 1. Agent 的初始化与状态

### 1.1 Agent 创建时需要什么？

创建一个 Agent 就像组装一台智能机器人，你需要告诉它（对应 `Agent.__init__` 参数）：

| 参数 | 作用 | 通俗理解 | 默认值 |
|------|------|---------|--------|
| `client` | LLM 客户端 | **大脑**——用哪个大模型 | 必填 |
| `registry` | 工具注册表 | **工具箱**——有哪些工具可用 | 必填 |
| `protocol` | 通信协议 | **语言**——Anthropic方言还是OpenAI方言 | 必填 |
| `work_dir` | 工作目录 | **工位**——在哪个目录里干活 | `"."` |
| `max_iterations` | 最大轮次 | **工期**——最多干50轮，防死循环 | `50` |
| `permission_checker` | 权限检查器 | **安监**——哪些操作需要审批 | `None` |
| `context_window` | 上下文窗口 | **记忆力**——一次能记住多少东西 | `200_000` |
| `memory_manager` | 记忆管理器 | **笔记本**——跨对话的长期记忆 | `None` |
| `hook_engine` | Hook 引擎 | **秘书**——在关键时刻触发脚本 | `None` |

### 1.2 Agent 记了哪些状态？

Agent 在运行时会追踪这些数据（`agent.py` 第 300-350 行）：

```python
class Agent:
    def __init__(self, ...):
        # 核心组件
        self.client = client
        self.registry = registry
        self.permission_checker = permission_checker
        self.memory_manager = memory_manager
        self.hook_engine = hook_engine

        # 运行时状态
        self.total_input_tokens = 0      # 累计输入 token（算钱用）
        self.total_output_tokens = 0     # 累计输出 token（算钱用）
        self._loop_count = 0             # 已完成的循环数（每5轮触发记忆提取）
        self._extracting = False         # 记忆提取是否在后台跑（防重入）
        self.session_id: str = ""        # 会话 ID，用于持久化
        self.parent_id: str | None       # 如果是子Agent，父Agent是谁
        self.agent_id: str = uuid.uuid4().hex[:12]  # 自己的唯一ID
        self.active_skills: dict[str, str] = {}     # 当前激活的技能包
        self.permission_mode: PermissionMode        # 当前权限模式

        # 上下文管理相关
        self.compact_breaker = CompactCircuitBreaker()           # 断路器
        self.replacement_state = create_replacement_state()      # 工具结果替换记录
        self.recovery_state = RecoveryState()                    # 文件快照（压缩后恢复用）
```

**为什么要把这些都放在 Agent 实例上？** 因为 Agent 是一个**有状态的长期运行对象**——它要在多轮循环中保持状态。如果把状态散落在函数局部变量里，循环间就丢失了。

### 1.3 Agent 的核心属性

| 属性 | 类型 | 作用 |
|------|------|------|
| `plan_mode` | `bool` | 是否处于 Plan 模式（property，从 permission_mode 推导） |
| `_transcript_path` | `str` | 会话日志文件路径（property，从 session_id 推导） |
| `agent_id` | `str` | 12 位 hex，唯一标识这个 Agent 实例 |
| `parent_id` | `str \| None` | 父 Agent ID（用于子 Agent 场景） |
| `team_name` | `str` | 所属团队名（用于 Team 协作） |
| `coordinator_mode` | `bool` | 是否是 Coordinator（工头） |

---

## 2. ReAct 主循环逐步分解（带源码）

`Agent.run()` 是整个项目最核心的代码（约 180 行，`agent.py` 第 427 行起）。它是一个**异步生成器**，把事件逐个 `yield` 给 TUI。把它看成一条流水线：

### 2.1 第一步：装载上下文（Loop 开始前）

这不在循环里，只在 Agent 刚启动时做一次（`agent.py` 第 427-443 行）：

```python
async def run(self, conversation: ConversationManager) -> AsyncIterator[AgentEvent]:
    self._current_conversation = conversation

    # 1. 注入环境上下文：告诉 LLM "你在哪个目录、激活了哪些 Skill"
    env_context = build_environment_context(
        self.work_dir, self.active_skills, self._skill_catalog, self._agent_catalog
    )
    conversation.inject_environment(env_context)

    # 2. 注入长期记忆：把 memories.md 的内容注入到对话开头
    memory_content = self.memory_manager.load() if self.memory_manager else ""
    conversation.inject_long_term_memory(self.instructions_content, memory_content)

    # 3. 触发 session_start 钩子
    if self.hook_engine:
        ctx = self._build_hook_context("session_start")
        await self.hook_engine.run_hooks("session_start", ctx)
        for he in self._drain_hook_events():
            yield he
```

**关键设计**：
- 环境上下文注入到 `history[0]`——LLM 最先看到
- 长期记忆注入到 `history[0]` 或 `history[1]`——紧随环境
- Hook 的事件用 `yield` 推送给 TUI，保持事件流的一致性

### 2.2 第二步：每轮循环的开头（Turn Start）

每轮 "用户说完 → Agent 回应" 算一个 Turn。每轮开始做（`agent.py` 第 450-500 行）：

```python
while True:
    iteration += 1

    # 安全阀：超过 max_iterations 就停（防死循环）
    if iteration > self.max_iterations:
        yield ErrorEvent(message=f"Agent reached maximum iterations ({self.max_iterations})")
        break

    # 触发 turn_start 钩子
    if self.hook_engine:
        ctx = self._build_hook_context("turn_start")
        await self.hook_engine.run_hooks("turn_start", ctx)
        for he in self._drain_hook_events():
            yield he

    # 消费团队邮箱（Team 协作时其他 Agent 发来的消息）
    self._consume_mailbox(conversation)
    if self.notification_fn:
        for note in self.notification_fn():
            conversation.add_system_reminder(note)

    # Layer 2: 接近 context window 上限时自动 compact
    compact_result = await auto_compact(
        conversation, self.client, self.context_window, self.session_dir,
        protocol=self.protocol, breaker=self.compact_breaker,
        recovery=self.recovery_state,
        tool_schemas=self.registry.get_all_schemas(self.protocol),
        transcript_path=self._transcript_path,
    )
    if isinstance(compact_result, CompactEvent):
        yield CompactNotification(...)
        # 压缩后重新注入环境上下文和记忆（因为对话历史被替换了）
        conversation.inject_environment(env_context)
        mem = self.memory_manager.load() if self.memory_manager else ""
        conversation.inject_long_term_memory(self.instructions_content, mem)
```

**关键设计**：
- **max_iterations=50 是安全阀**——防止 LLM 陷入死循环烧钱
- **compact 后必须重新注入环境上下文**——因为压缩把整个对话历史替换了，环境信息丢了
- **邮箱消费在循环开头**——保证本轮 LLM 能看到其他 Agent 发来的最新消息

### 2.3 第三步：调用 LLM（Think 阶段）

这是 ReAct 的"推理"环节。代码做了三件事：

```python
# 1. 构建 system prompt（注入 Hook 提示、Plan 模式提示、延迟工具提示）
system_prompt = build_system_prompt(...)
# 2. 把对话历史裁剪工具结果（Layer 1 压缩）
api_conv = apply_tool_result_budget(conversation, self.replacement_state)
# 3. 调用 LLM 的流式 API，用 StreamCollector 收集事件
stream = self.client.stream(api_conv, system_prompt, tools)
collector = StreamCollector()
async for event in collector.consume(stream):
    yield event  # 实时推给 TUI（打字机效果）
```

**StreamCollector 的工作原理**（`agent.py` 第 187-220 行）：

```python
class StreamCollector:
    def __init__(self) -> None:
        self.response = LLMResponse()

    async def consume(self, stream: AsyncIterator[StreamEvent]) -> AsyncIterator[AgentEvent]:
        async for event in stream:
            if isinstance(event, TextDelta):
                self.response.text += event.text       # 累积文本
                yield StreamText(text=event.text)       # 实时推给 TUI
            elif isinstance(event, ThinkingDelta):
                yield ThinkingText(text=event.text)     # 思考过程实时推
            elif isinstance(event, ThinkingComplete):
                self.response.thinking_blocks.append(...)  # 保存完整思考块
            elif isinstance(event, ToolCallComplete):
                self.response.tool_calls.append(event)   # 保存工具调用
                yield ToolUseEvent(...)                  # 推工具卡片事件
            elif isinstance(event, StreamEnd):
                # 保存 token 用量、stop_reason
                self.response.stop_reason = event.stop_reason
                self.response.input_tokens = event.input_tokens
                self.response.output_tokens = event.output_tokens
                self.response.cache_read = event.cache_read
                self.response.cache_creation = event.cache_creation
```

**这是双职责设计**：
1. **实时推送**：每收到一个 `TextDelta`，立即 `yield StreamText`——TUI 就能显示打字机效果
2. **累积完整响应**：把碎片拼成完整的 `LLMResponse`——后续判断 stop_reason 用

**为什么不用两套机制？** 因为流式处理需要"边收边推"，但又需要完整响应做后续判断。StreamCollector 把两个职责合一，避免代码重复。

### 2.4 第四步：处理 LLM 返回的结果

LLM 响应收集完后，根据 `stop_reason` 分三种情况处理：

**情况 A：LLM 被 max_tokens 截断了**

`stop_reason == "max_tokens"`（Anthropic）或 `length`（OpenAI）。这就像 LLM 话说到一半被"字数限制"掐断了。恢复流程很精妙（详见 §6）：

```python
if collector.response.stop_reason == "max_tokens":
    if not max_tokens_escalated:
        # 第一次截断：提升上限 8K → 64K
        self.client.set_max_output_tokens(MAX_TOKENS_CEILING)  # 64000
        max_tokens_escalated = True
        # 注入"从断掉的地方继续"提示
        conversation.add_assistant_message(...)
        continue  # 重新进入循环
    elif output_recoveries < MAX_OUTPUT_TOKENS_RECOVERIES:  # 3
        # 后续截断：提示"拆成小块"
        output_recoveries += 1
        conversation.add_system_reminder("把工作拆成更小的步骤")
        continue
    # 3 次后还截断 → 放弃，走正常流程
```

**情况 B：没有工具调用**

任务完成！把 LLM 的回复加入历史，视情况触发记忆提取，退出循环。

```python
if not collector.response.tool_calls:
    # 加入对话历史
    conversation.add_assistant_message(text=collector.response.text, ...)
    # 累计 token 用量
    self.total_input_tokens += collector.response.input_tokens
    self.total_output_tokens += collector.response.output_tokens
    yield UsageEvent(input_tokens=..., output_tokens=...)
    # 触发记忆提取（每5轮）
    self._loop_count += 1
    if self._loop_count % MEMORY_EXTRACTION_INTERVAL == 0:  # 5
        self._trigger_memory_extraction(conversation)
    # 触发 turn_end 钩子
    # ...
    yield TurnComplete(turn=iteration)
    # 退出循环
    break
```

**情况 C：有工具调用**

继续下一步——执行工具。

### 2.5 第五步：执行工具（Act 阶段）

这是 ReAct 的"行动"环节。代码做了五件事（详见 §5）：

1. **分区**：把工具调用分成"可以并发"和"必须串行"两组
2. **安全检查**：每个工具调用都要过六层防线
3. **人工确认**：如果需要确认，yield 一个 `PermissionRequest`，等用户点允许/拒绝
4. **执行工具**：pre_tool_use 钩子 → 真正执行 → post_tool_use 钩子
5. **结果返回**：把工具输出加入对话历史

### 2.6 第六步：循环回去（Observe → Loop）

把工具结果加入对话 → 触发 `turn_end` 钩子 → 回到第二步。LLM 看到工具结果后继续推理下一步。

```python
# 触发 turn_end 钩子
if self.hook_engine:
    ctx = self._build_hook_context("turn_end")
    await self.hook_engine.run_hooks("turn_end", ctx)
    for he in self._drain_hook_events():
        yield he

yield TurnComplete(turn=iteration)
# 不 break，继续 while True 下一轮
```

### 2.7 循环什么时候结束？

四种终止条件：

| 终止条件 | 触发位置 | 原因 |
|---------|---------|------|
| LLM 不再产生工具调用 | 第四步情况B | 任务正常完成 |
| 达到 max_iterations (50) | 第二步开头 | 安全阀，防死循环 |
| 连续 3 次调用不存在的工具 | 第五步 | 幻觉保护 |
| Plan 模式下调用 ExitPlanMode | 第五步 | 用户审核通过计划 |

**幻觉保护的设计**：

```python
# 执行工具时检查
tool = registry.get(tc.tool_name)
if tool is None:
    consecutive_unknown += 1
    if consecutive_unknown >= 3:
        yield ErrorEvent(message="Agent 似乎在调用不存在的工具，可能产生了幻觉")
        break
else:
    consecutive_unknown = 0  # 重置
```

**为什么要这个保护？** LLM 有时会"幻觉"出不存在的工具名（比如把 `ReadFile` 写成 `read_file` 或 `Read_File`）。如果不拦截，LLM 会陷入"调用→失败→再调用同样的"死循环，烧 token 烧钱。

---

## 3. 事件驱动架构：Agent 如何和 UI 对话

### 3.1 Agent 不直接操作 UI

Agent 是一个异步生成器——它不画界面，只产出**事件**。TUI 收到事件后自己决定怎么渲染：

```
Agent.run() → yield 事件 → TUI 收到 → TUI 渲染
```

**这是"关注点分离"的极致体现**：
- Agent 只关心"发生了什么"（产出事件）
- TUI 只关心"怎么显示"（消费事件）
- 换个 UI（比如改成 Web 界面），Agent 一行不用改

### 3.2 十二种事件一览

| 事件 | 触发时机 | 携带数据 | TUI 怎么处理 |
|------|---------|---------|------------|
| `StreamText` | LLM 输出一个字 | `text` | 追加到当前消息气泡末尾（打字机） |
| `ThinkingText` | LLM 思考内容 | `text` | 追加到可折叠的思考块 |
| `ToolUseEvent` | 要调用工具了 | `tool_name, tool_id, arguments` | 显示工具卡片（名称+参数） |
| `ToolResultEvent` | 工具执行完了 | `tool_id, tool_name, output, is_error, elapsed` | 更新卡片状态（成功/失败+耗时） |
| `TurnComplete` | 本轮结束 | `turn` | 重置输入状态 |
| `LoopComplete` | 全部完成 | `total_turns` | 显示"完成，共 X 轮" |
| `UsageEvent` | Token 统计更新 | `input_tokens, output_tokens` | 更新状态栏 |
| `ErrorEvent` | 出错了 | `message` | 显示错误提示 |
| `PermissionRequest` | 需要确认 | `tool_name, description, future` | 弹出权限对话框 |
| `CompactNotification` | 对话被压缩了 | `before_tokens, message, boundary` | 显示压缩提示 |
| `RetryEvent` | 重试 | `reason, wait` | 显示重试提示 |
| `HookEvent` | Hook 触发了 | `hook_id, event, output, success` | 显示 Hook 执行结果 |

**事件类型定义**（`agent.py` 第 149-162 行）用 Python 的联合类型：

```python
AgentEvent = (
    StreamText
    | ThinkingText
    | RetryEvent
    | ToolUseEvent
    | ToolResultEvent
    | TurnComplete
    | LoopComplete
    | UsageEvent
    | ErrorEvent
    | PermissionRequest
    | CompactNotification
    | HookEvent
)
```

**为什么用联合类型而不是继承？** 因为事件之间没有共性（数据完全不同），继承会引入无意义的基类。联合类型 + `isinstance` 匹配是 Python 3.10+ 的现代做法，类型安全且性能好。

### 3.3 关键设计：PermissionRequest 的异步确认

这是整个项目最精妙的设计之一。完整的时序图：

```
Agent 侧：                                    TUI 侧：
发现需要用户确认                                收到 PermissionRequest
  ↓                                            ↓
创建 asyncio.Future 对象                       弹出对话框 [Allow] [Deny] [Always]
（一个"空盒子"，等着被填）                        ↓
  ↓                                          用户点击 Allow
yield PermissionRequest                         ↓
（把空盒子寄给 TUI）                           future.set_result(ALLOW)
  ↓                                            ↓
await future ← 协程挂起                         （把盒子填满，寄回给 Agent）
  ↓                                            ↓
（事件循环调度 TUI 动画、用户交互）              （TUI 继续响应）
  ↓                                            ↓
future 完成 → 拿到 ALLOW                       （Agent 协程恢复）
  ↓
继续执行工具
```

**核心代码**（`agent.py`）：

```python
# Agent 侧
future: asyncio.Future[PermissionResponse] = asyncio.Future()
yield PermissionRequest(
    tool_name="EditFile",
    description="允许修改 main.py？",
    future=future,
)
# 这里协程挂起，事件循环继续调度 TUI
response = await future  # 等 TUI 把结果填进 future
if response == PermissionResponse.ALLOW:
    # 执行工具
```

```python
# TUI 侧（app.py）
def on_permission_request(self, event: PermissionRequest):
    # 弹对话框，用户点击后：
    event.future.set_result(PermissionResponse.ALLOW)
```

### 3.4 为什么用 Future 而不是回调函数？

**用回调函数的话**：

```python
# 假设的回调版（不好）
def on_permission_response(response):
    # 确认后的逻辑要拆到这里
    if response == ALLOW:
        execute_tool(...)
        continue_loop(...)
    # 还要把当前状态打包传过来
```

问题：
- 确认前和确认后的逻辑被拆成两段，可读性差
- 中间状态（工具参数、循环上下文）要打包传递
- 多个嵌套的回调会变成"回调地狱"

**用 Future 的话**：

```python
# Future 版（好）
future = asyncio.Future()
yield PermissionRequest(..., future=future)
response = await future  # 代码从上到下自然流动
if response == ALLOW:
    execute_tool(...)  # 变量都在同一个作用域
```

优势：
- Agent 代码从上到下自然流动——确认前的代码和确认后的代码在同一个函数里
- 变量都能直接访问，不需要打包传递
- 配合 `await` 语义清晰

### 3.5 为什么是 await 而不是同步阻塞？

`await future` 只挂起当前协程，事件循环照常运转——TUI 动画还在播、用户还能点按钮。

如果用同步阻塞（`future.result()`）：

```python
# 错误做法（会卡死）
response = future.result()  # 阻塞整个事件循环！
```

整个程序冻住——连"Allow"按钮都点不了，因为事件循环被这个阻塞调用占着，没法处理 TUI 的点击事件。**这是 asyncio 编程的经典陷阱**。

---

## 4. 三协议适配：三种 API 如何统一

### 4.1 抽象接口只有一个方法

LLM 客户端基类极其简洁——只要求实现一个 `stream()` 方法（`client.py` 第 102-113 行）：

```python
class LLMClient(ABC):
    @abstractmethod
    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield TextDelta("")

    def set_max_output_tokens(self, tokens: int) -> None:
        pass
```

接收：对话历史 + system prompt + 工具定义
返回：异步流式事件（7 种 StreamEvent）

Agent 代码只需要：

```python
async for event in client.stream(conversation, system_prompt, tools):
    处理 event
```

完全不知道底层是哪个模型。**这就是策略模式的威力——抽象接口只有一个方法，但能容纳完全不同的实现。**

### 4.2 三种具体实现

| 实现 | 用的 API | 适用模型 | 端点 |
|------|---------|---------|------|
| `AnthropicClient` | `/v1/messages` | Claude 全系列 | `https://api.anthropic.com/v1/messages` |
| `OpenAIClient` | `/v1/responses` | GPT-4o 等（OpenAI 新版 API） | `https://api.openai.com/v1/responses` |
| `OpenAICompatClient` | `/v1/chat/completions` | DeepSeek、Qwen、Ollama、vLLM... | 兼容 OpenAI 协议的任何端点 |

### 4.3 同一个概念，三种 API 表达完全不同

以 "LLM 回复文字 + 调用一个工具" 这一个简单操作为例，三种 API 的请求体差异巨大：

**Anthropic 方式**（`/v1/messages`）：内容是一个数组，文字和工具调用平铺

```json
{
  "role": "assistant",
  "content": [
    {"type": "text", "text": "我来读文件"},
    {"type": "tool_use", "id": "tool_001", "name": "ReadFile", "input": {"file_path": "main.py"}}
  ]
}
```

**OpenAI Responses 方式**（`/v1/responses`）：文字和工具调用是分开的两个输出

```json
{
  "role": "assistant",
  "content": "我来读文件"
},
{
  "type": "function_call",
  "call_id": "tool_001",
  "name": "ReadFile",
  "arguments": {"file_path": "main.py"}
}
```

**OpenAI Chat Completions 方式**（`/v1/chat/completions`）：文字直接是字符串，工具调用在独立字段里，还多一层 "function" 嵌套

```json
{
  "role": "assistant",
  "content": "我来读文件",
  "tool_calls": [
    {
      "id": "tool_001",
      "type": "function",
      "function": {
        "name": "ReadFile",
        "arguments": "{\"file_path\": \"main.py\"}"  // 注意：这里是字符串！
      }
    }
  ]
}
```

**注意第三个的差异**：`arguments` 是一个 **JSON 字符串**（需要二次解析），而不是 JSON 对象。这是 OpenAI Chat Completions 协议的一个坑——很多开发者第一次踩。

**每种客户端的工作就是**：
1. 把内部统一的消息格式，转换成自己 API 要的格式（序列化）
2. 把 API 返回的流式事件，映射为内部统一的 7 种事件类型（反序列化）
3. 把原生异常，映射为内部统一的 4 种异常

### 4.4 序列化层：serialization.py

为了不污染客户端类，序列化逻辑独立到 `serialization.py`：

| 函数 | 作用 |
|------|------|
| `build_anthropic_messages(conv)` | 内部格式 → Anthropic 请求体 |
| `build_openai_input(conv)` | 内部格式 → OpenAI Responses 请求体 |
| `build_chat_completion_messages(conv)` | 内部格式 → OpenAI Chat Completions 请求体 |

每个客户端在 `stream()` 方法里调对应的序列化函数，把内部 `ConversationManager` 转成 API 请求体。

### 4.5 流式事件的统一映射

三种 API 的流式事件名都不一样，但内部只有 7 种（`tools/base.py`）：

| 内部事件 | Anthropic 事件 | OpenAI Responses 事件 | OpenAI Chat 事件 |
|---------|---------------|---------------------|-----------------|
| `TextDelta` | `content_block_delta` (text) | `response.output_text.delta` | `chunk.choices[0].delta.content` |
| `ToolCallStart` | `content_block_start` (tool_use) | `response.function_call` 开始 | `chunk.choices[0].delta.tool_calls` 开始 |
| `ToolCallDelta` | `content_block_delta` (input_json) | `response.function_call_arguments.delta` | `chunk.choices[0].delta.tool_calls.arguments` |
| `ToolCallComplete` | `content_block_stop` | `response.function_call.done` | `chunk.choices[0].finish_reason="tool_calls"` |
| `ThinkingDelta` | `content_block_delta` (thinking) | （不支持） | （不支持） |
| `ThinkingComplete` | `content_block_stop` (thinking) | （不支持） | （不支持） |
| `StreamEnd` | `message_stop` | `response.completed` | `chunk.choices[0].finish_reason="stop"` |

每个客户端的 `stream()` 方法就是一个**事件翻译器**——把原生事件流转成内部事件流。

### 4.6 错误处理统一

三种 SDK 各自抛出不同的异常，但 CodeBot 把它们映射为内部统一的 4 种异常（`client.py` 第 82-99 行）：

```python
class LLMError(Exception): pass
class AuthenticationError(LLMError): pass  # API Key 不对

class RateLimitError(LLMError):  # 请求太频繁
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after  # 多少秒后重试

class NetworkError(LLMError): pass  # 网络不通
```

**映射规则**：

| SDK 原生异常 | 内部异常 | 处理方式 |
|------------|---------|---------|
| `anthropic.AuthenticationError` | `AuthenticationError` | 提示用户检查 API Key |
| `anthropic.RateLimitError` | `RateLimitError(retry_after=...)` | 等待后重试 |
| `openai.APIConnectionError` | `NetworkError` | 提示网络问题 |
| 其他 | `LLMError` | 通用错误处理 |

上层代码只需要 `try...except AuthenticationError`，不用管底层是 Anthropic 还是 OpenAI。

---

## 5. 工具调用如何分区并发执行

### 5.1 不是所有工具都能同时跑

LLM 可能一次返回多个工具调用。但有些能并发，有些必须串行：

- ✅ **可以并发**：读文件、搜代码（只读，不影响文件系统）
- ❌ **必须串行**：写文件、跑命令（有副作用，有依赖关系）

**为什么写文件不能并发？** 想象 LLM 同时调 `WriteFile(a.py)` 和 `EditFile(a.py)`——两个并发执行会互相覆盖，结果不可预测。

### 5.2 分区算法（`agent.py` 第 233-246 行）

把工具调用列表从头到尾遍历一遍，遇到"可并发"的就放进当前批次，遇到"必须串行"的就开始新批次：

```python
@dataclass
class ToolBatch:
    concurrent: bool
    calls: list[ToolCallComplete]

def partition_tool_calls(
    tool_calls: list[ToolCallComplete],
    registry: ToolRegistry,
) -> list[ToolBatch]:
    batches: list[ToolBatch] = []
    for tc in tool_calls:
        tool = registry.get(tc.tool_name)
        # 判断能否并发：工具存在 + is_concurrency_safe + 已启用
        safe = (tool is not None
                and tool.is_concurrency_safe
                and registry.is_enabled(tc.tool_name))

        if safe and batches and batches[-1].concurrent:
            # 可并发 + 上一批也是并发批 → 加入当前批
            batches[-1].calls.append(tc)
        else:
            # 否则开新批（即使可并发也开新批，因为前一批是串行批）
            batches.append(ToolBatch(concurrent=safe, calls=[tc]))
    return batches
```

### 5.3 分区示例

```
输入: [ReadFile(a.py), ReadFile(b.py), Bash(rm xxx), ReadFile(c.py)]

分析:
  - ReadFile(a.py): safe=True, 当前无批次 → 开新批 (concurrent=True)
  - ReadFile(b.py): safe=True, 上一批是并发批 → 加入当前批
  - Bash(rm xxx):   safe=False → 开新批 (concurrent=False)
  - ReadFile(c.py): safe=True, 但上一批是串行批 → 开新批 (concurrent=True)

输出:
  批次1 (并发): ReadFile(a.py) + ReadFile(b.py)  ← 两个同时读
  批次2 (串行): Bash(rm xxx)                     ← 等批次1完成，单独执行
  批次3 (并发): ReadFile(c.py)                   ← 等批次2完成，单独执行
```

**注意第三个 ReadFile 被分到了单独批次**——因为它前面的 Bash 可能修改了文件，必须先等 Bash 完成再读。**这是分区算法的关键洞察：可并发不是绝对属性，而是相对于"前一个操作"的属性。**

### 5.4 并发执行：StreamingExecutor

`StreamingExecutor`（`agent.py` 第 262-292 行）负责并发执行工具：

```python
class StreamingExecutor:
    def __init__(self) -> None:
        self._tasks: list[tuple[int, asyncio.Task[_ToolExecResult]]] = []
        self._order = 0

    def submit(self, coro: Any) -> None:
        task = asyncio.create_task(coro)  # 立即调度，不阻塞
        self._tasks.append((self._order, task))
        self._order += 1

    async def collect_results(self) -> list[_ToolExecResult]:
        if not self._tasks:
            return []
        # 按提交顺序排序（保证结果顺序）
        tasks = [t for _, t in sorted(self._tasks, key=lambda x: x[0])]
        # asyncio.gather 并发等待所有任务
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # 处理异常（某个工具失败不影响其他）
        out: list[_ToolExecResult] = []
        for r in results:
            if isinstance(r, Exception):
                out.append(_ToolExecResult(
                    tool_id="", tool_name="",
                    result=ToolResult(output=f"Tool execution error: {r}", is_error=True),
                    elapsed=0.0, is_unknown=False,
                ))
            else:
                out.append(r)
        return out
```

**关键设计**：
- `asyncio.create_task(coro)` 立即调度任务，不阻塞当前协程
- `asyncio.gather(*tasks)` 并发等待，等全部完成
- `return_exceptions=True` 让 gather 不抛异常，把异常作为结果返回——避免一个工具失败影响其他

### 5.5 串行执行

对于不能并发的批次，逐个执行：

```python
for tc in batch.calls:
    result = await self._execute_single_tool(tc)
    # 立即把结果加入对话，下一个工具能看到
    conversation.add_tool_result(tc.tool_id, result.output)
```

---

## 6. max_tokens 截断的恢复策略

### 6.1 什么是 max_tokens 截断？

LLM 有最大输出长度限制（如 8K tokens）。如果 LLM 的回复超过这个限制，会被"硬截断"——话说到一半就断了，工具调用的 JSON 可能不完整。

**这会引发严重问题**：如果 LLM 正在生成工具调用的参数，被截断后 JSON 不完整，解析失败，工具无法执行。

### 6.2 三层恢复策略

CodeBot 用三层策略恢复（`agent.py` 第 447-448 行 + 后续逻辑）：

```python
# 状态变量
max_tokens_escalated = False      # 是否已经提升过上限
output_recoveries = 0             # 后续重试次数

# 在处理 LLM 响应时
if collector.response.stop_reason == "max_tokens":
    if not max_tokens_escalated:
        # 第一层：提升上限 8K → 64K
        self.client.set_max_output_tokens(MAX_TOKENS_CEILING)  # 64000
        max_tokens_escalated = True
        # 注入"请从你刚才断掉的地方继续"
        conversation.add_system_reminder("继续你刚才未完成的回复")
        continue  # 重新进入循环

    elif output_recoveries < MAX_OUTPUT_TOKENS_RECOVERIES:  # 3
        # 第二层：提示"拆成小块"
        output_recoveries += 1
        conversation.add_system_reminder(
            "你上次的回复被截断了。请把工作拆成更小的步骤，每次只做一步。"
        )
        continue

    # 第三层：3 次后还截断 → 放弃，走正常流程
    # （可能丢失未完成的工具调用，但至少不卡死）
```

### 6.3 为什么是三层？

| 层次 | 策略 | 解决的问题 | 副作用 |
|------|------|----------|--------|
| 1 | 提升上限 8K→64K | 单次输出确实长 | 多花钱 |
| 2 | 提示拆分步骤 | 任务太复杂一次做不完 | 多一轮 LLM 调用 |
| 3 | 放弃 | 防止无限重试烧钱 | 可能丢工具调用 |

**这是"渐进式降级"的典型设计**——先尝试代价小的方案（提升上限），不行再尝试代价大的（拆分），最后兜底（放弃）。

---

## 7. Anthropic 独有功能深度剖析

### 7.1 Prompt Caching（提示缓存）

**问题**：多轮对话中，system prompt、工具定义、环境上下文几乎不变。每次都全量发送，浪费 token。

**Anthropic 的解法**：允许标记这些内容为"可缓存"，命中后只收 10% 费用。

**CodeBot 的实现**（`client.py` 第 38-79 行）：

```python
_EPHEMERAL = {"type": "ephemeral"}

def _mark_last_user_tail_for_cache(messages: list[dict]) -> None:
    """给最后一条 user 消息的最后一个 block 附加 cache_control。"""
    if not messages:
        return
    # 从后往前找到最后一条 user 角色消息
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            # 升级为 block 形式
            msg["content"] = [{
                "type": "text",
                "text": content,
                "cache_control": _EPHEMERAL,
            }]
        elif isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict):
                last["cache_control"] = _EPHEMERAL
        return

def _mark_last_tool_for_cache(tools: list[dict]) -> list[dict]:
    """返回浅拷贝的 tools 列表，在最后一个 tool 上标记 cache_control。"""
    if not tools:
        return tools
    marked = list(tools)  # 浅拷贝，不改原始列表（因为 tool schema 是单例）
    last = dict(marked[-1])
    last["cache_control"] = _EPHEMERAL
    marked[-1] = last
    return marked
```

### 7.2 Prompt Caching 的工作原理

```
第1次请求：
  system + tools + history[0..5] + new_msg
  → Anthropic 缓存 system + tools + history[0..5]（cache_creation 计费）
  → 返回响应

第2次请求：
  system + tools + history[0..5] + history[6] + new_msg2
  → Anthropic 命中缓存（system + tools + history[0..5]）
  → 只对未命中部分（history[6] + new_msg2）计费
  → 命中的部分只收 10% 费用！
```

**为什么标记最后一条 user 消息？** 因为 Anthropic 的缓存是"前缀缓存"——缓存到标记位置为止的所有内容。最后一条 user 消息是前缀的终点，标记它能让前面的所有内容都被缓存。

### 7.3 成本计算

`StreamEnd` 事件携带缓存用量（`tools/base.py` 第 86-98 行）：

```python
@dataclass
class StreamEnd:
    stop_reason: str
    input_tokens: int = 0       # 未缓存的输入 token
    output_tokens: int = 0
    # Anthropic 把缓存前缀分为：
    cache_read: int = 0         # cache 命中，按 10% 计费
    cache_creation: int = 0     # cache 写入（首次）
    # OpenAI 系列只暴露 cache_read（通过 *_tokens_details.cached_tokens）
    # 没有 creation 计数，所以那边 cache_creation 始终为 0
```

**实际 prompt 大小** = `input_tokens + cache_read + cache_creation`

### 7.4 Extended Thinking（扩展思考）

**问题**：复杂任务需要 LLM 在回复前深度思考（类似 "让我们一步步分析" 但更强大）。

**Anthropic 的解法**：Extended Thinking 让 Claude 在回复前生成思考块，思考过程带数字签名防止篡改。

**CodeBot 的实现**：

```python
def _supports_adaptive_thinking(model: str) -> bool:
    """判断模型是否支持自适应思考（Claude Opus 4.6+ / Sonnet 4.6+）。"""
    for family in ("claude-opus-4-", "claude-sonnet-4-"):
        if model.startswith(family):
            rest = model[len(family):]
            if rest and rest[0].isdigit() and int(rest[0]) >= 6:
                return True
    return False
```

思考块通过 `ThinkingDelta` 和 `ThinkingComplete` 事件流式推送，TUI 显示在可折叠的"思考块"中。

### 7.5 为什么 OpenAI 系列不支持这些？

- **Prompt Caching**：OpenAI 也有自己的缓存机制，但通过 `cached_tokens` 字段返回，不需要客户端标记
- **Extended Thinking**：OpenAI 的 o1 系列有内部思考，但不暴露思考过程给客户端

**这就是不用 litellm 的核心原因**——如果用 litellm 的统一接口，这些差异化能力就享受不到。

---

## 8. 面试高频点与追问

### 8.1 "ReAct 循环的每一步是干什么的？"

**核心思路**：不要背代码，讲清楚四个阶段和一个循环。

- **Think**：LLM 分析当前状态，决定下一步做什么（可能产生文字+工具调用）
- **Act**：执行工具调用（先安全检查，再实际执行）
- **Observe**：把工具返回的结果交由 LLM 分析
- **Loop**：如果没完成就回到 Think，如果 LLM 不再产生工具调用就结束

**追问：循环结束条件有几种？**

四种：
1. LLM 不再产生工具调用（正常完成）
2. 超过 50 轮（安全阀，防死循环）
3. 连续 3 次调用不存在的工具（幻觉保护）
4. Plan 模式下调用 ExitPlanMode（用户审核通过计划）

**追问：为什么要幻觉保护？**

LLM 有时会"幻觉"出不存在的工具名。如果不拦截，LLM 会陷入"调用→失败→再调用"死循环，烧 token 烧钱。连续 3 次的阈值是经验值——1 次可能是笔误，2 次可能是重试，3 次基本确定是幻觉。

### 8.2 "max_tokens 截断怎么恢复？"

三层恢复策略：

1. **提升上限**：8K → 64K，注入"从断掉的地方继续"，重试
2. **分解工作**：注入"拆成小块"，最多再试 3 次
3. **放弃**：3 次后还截断，走正常流程（可能丢失未完成的工具调用）

**追问：为什么是 64K 不是无限？**

因为 Anthropic API 的硬限制是 64K。再大模型也生成不出来。

**追问：为什么是 3 次不是 5 次？**

经验值。3 次失败基本说明任务本身不适合一次做完，再多试也是浪费 token。这是"快速失败"原则。

### 8.3 "怎么做到切换模型不改代码？"

策略模式 + 工厂函数：

1. **定义抽象接口**：`LLMClient` 只要求实现 `stream()` 方法
2. **三个具体实现**：AnthropicClient、OpenAIClient、OpenAICompatClient
3. **工厂函数**：配置里写 `protocol: openai-compat` 就自动选对应实现
4. **序列化独立**：`serialization.py` 负责内部格式 ↔ 各 API 格式转换

新增一个模型只需要：
- 新增一个客户端类（继承 LLMClient）
- 新增一个序列化函数
- 在工厂函数里加一行映射

**核心代码一行不动。**

**追问：序列化为什么独立到 serialization.py？**

单一职责原则。客户端类只负责"调用 API + 翻译事件流"，序列化是"数据格式转换"——两个关注点分离。这样客户端类更小，序列化逻辑可独立测试。

### 8.4 "工具怎么决定并发还是串行执行？"

关键依据：工具是否 `is_concurrency_safe`。

- 只读工具（ReadFile、Grep、Glob）：`is_concurrency_safe=True`，可以并发
- 有副作用的工具（WriteFile、EditFile、Bash）：`is_concurrency_safe=False`，必须串行

**分区算法**：遍历工具调用列表，可并发的连续工具放一批，遇到串行工具开新批。

**追问：为什么连续两个可并发工具在串行工具后要开新批？**

因为串行工具（如 Bash）可能修改了文件，后续的读操作必须看到修改后的状态。如果和前面的读操作并发，会读到旧内容。**可并发是相对属性，不是绝对属性——相对于前一个操作。**

**追问：asyncio.gather 怎么保证结果顺序？**

`StreamingExecutor` 用 `self._order` 记录提交顺序，`collect_results` 时按 order 排序。即使任务完成顺序不同，结果顺序和提交顺序一致。

### 8.5 "权限确认为什么用 Future 而不是回调？"

三个原因：

1. **代码自然流动**：用 `await future` 的话，确认前后的代码在同一个函数里，变量都能直接访问。用回调的话要拆成两段，状态要打包传递。
2. **避免回调地狱**：多个嵌套的权限确认会变成回调地狱。
3. **协程友好**：`await` 只挂起当前协程，事件循环继续运转——TUI 还能响应用户点击。如果用同步阻塞（`future.result()`），整个程序冻住。

**追问：Future 是什么？**

`asyncio.Future` 是一个"占位符"对象——表示一个未来才会有的结果。一个协程 `await future` 挂起等待，另一个协程 `future.set_result(value)` 把结果填进去，前者恢复执行。这是异步编程中"跨协程通信"的标准模式。

### 8.6 "为什么 OpenAI Chat Completions 的 arguments 是字符串？"

这是 OpenAI 协议的一个设计怪癖——`tool_calls[].function.arguments` 是一个 JSON 字符串，而不是 JSON 对象。需要二次解析：

```python
import json
args = json.loads(tool_call.function.arguments)  # 字符串 → dict
```

**为什么 OpenAI 这么设计？** 可能是为了兼容流式输出——流式时 arguments 是逐字符到达的，字符串拼接比 JSON 解析更安全（不会因为不完整 JSON 报错）。

### 8.7 "Prompt Caching 怎么省钱？"

**原理**：多轮对话中 system prompt + tools + 历史 messages 几乎不变。Anthropic 允许标记这些为"可缓存"，命中后只收 10% 费用。

**实现**：给最后一条 user 消息和最后一个 tool 附加 `cache_control: {type: ephemeral}`。Anthropic 会缓存到这个位置为止的所有内容。

**效果**：长对话场景下，缓存命中率可达 90%+，节省 80%+ 的 input token 费用。

**追问：为什么标记最后一条 user 消息？**

因为 Anthropic 是"前缀缓存"——缓存到标记位置为止。最后一条 user 消息是"不变前缀"的终点，标记它能让前面的所有内容都被缓存。如果标记更早的位置，后面的内容每次都要重发。

### 8.8 "如果让你重新设计 Agent 循环，会改什么？"

这是开放题，参考答案：

1. **可观测性**：加 OpenTelemetry 追踪，每轮循环是一个 span，工具调用是子 span
2. **可中断**：目前用户按 ESC 取消，但循环内部不感知。可以加 cancellation token
3. **重试策略**：目前 max_tokens 截断有重试，但网络错误没有。可以加指数退避
4. **并行 LLM 调用**：目前是串行调用 LLM，复杂任务可以并行调用多个 LLM 分头思考
5. **流式工具执行**：目前工具执行完才返回结果，长任务可以流式返回进度

---

> **下一步**：阶段3深入工具系统的标准化设计、注册表机制、六层安全模型的源码实现。

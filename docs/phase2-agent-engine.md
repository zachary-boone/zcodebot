# 阶段2：核心引擎深入 — ReAct 循环与 LLM 客户端

> **学习目标**：理解 Agent 主循环的每一步、事件流如何驱动 UI、不同 LLM 协议如何统一。
> **预计时间**：3-4 天（每天 2-3 小时）
> **前置要求**：完成阶段1，理解整体架构

---

## 目录

1. [Agent 的初始化与状态](#1-agent-的初始化与状态)
2. [ReAct 主循环逐步分解](#2-react-主循环逐步分解)
3. [事件驱动架构：Agent 如何和 UI 对话](#3-事件驱动架构agent-如何和-ui-对话)
4. [三协议适配：三种 API 如何统一](#4-三协议适配三种-api-如何统一)
5. [工具调用如何分区并发执行](#5-工具调用如何分区并发执行)
6. [面试高频点](#6-面试高频点)

---

## 1. Agent 的初始化与状态

### 1.1 Agent 创建时需要什么？

创建一个 Agent 就像组装一台智能机器人，你需要告诉它：

| 参数 | 作用 | 通俗理解 |
|------|------|---------|
| `client` | LLM 客户端 | **大脑**——用哪个大模型 |
| `registry` | 工具注册表 | **工具箱**——有哪些工具可用 |
| `protocol` | 通信协议 | **语言**——Anthropic方言还是OpenAI方言 |
| `work_dir` | 工作目录 | **工位**——在哪个目录里干活 |
| `max_iterations` | 最大轮次（默认50） | **工期**——最多干50轮，防死循环 |
| `permission_checker` | 权限检查器 | **安监**——哪些操作需要审批 |
| `context_window` | 上下文窗口 | **记忆力**——一次能记住多少东西 |
| `memory_manager` | 记忆管理器 | **笔记本**——跨对话的长期记忆 |

### 1.2 Agent 记了哪些状态？

Agent 在运行时会追踪这些数据：

- **输入/输出 token 累计**——用来告诉你这轮对话花了多少钱
- **已完成的循环数**（`_loop_count`）——每 5 轮触发一次记忆提取
- **session_id**——用于会话持久化，下次打开能恢复
- **parent_id**——如果这是子Agent，它的父Agent是谁
- **active_skills**——当前激活了哪些技能包

---

## 2. ReAct 主循环逐步分解

`Agent.run()` 是整个项目最核心的代码（约 180 行）。把它看成一条流水线：

### 第一步：装载上下文（Loop 开始前）

这不在循环里，只在 Agent 刚启动时做一次：

1. **注入环境上下文**：告诉 LLM "你现在在 `E:/project/src/` 目录下，可用技能有 commit 和 review"
2. **注入长期记忆**：把 memories.md 里的内容（"用户偏好简洁代码""项目用 PostgreSQL 15"）注入到对话开头
3. **触发 session_start 钩子**：执行你配置的启动脚本

### 第二步：每轮循环的开头（Turn Start）

每轮 "用户说完 → Agent 回应" 算一个 Turn。每轮开始做：

1. **消费团队邮箱**——如果是团队协作，看看其他 Agent 有没有发消息
2. **通知检查**——子Agent 完成了吗？
3. **自动压缩检查**——对话太长了吗？需要压缩吗？
4. **构建系统提示词**——把 Hook 注入的提示、Plan 模式提示、延迟工具提示拼上去
5. **工具结果裁剪**——如果上一轮的工具结果太大，裁剪或替换为文件引用

### 第三步：调用 LLM（Think 阶段）

1. 把所有消息序列化成对应 API 的格式
2. 调用 LLM 的流式 API
3. 用 `StreamCollector` 收集流式事件：
   - 每收到一个文字 → 立即 yield 给 TUI 显示（打字机效果）
   - 每收到一个工具调用 → yield 工具卡片
   - 等流结束了 → 拿到完整的响应（文本 + 工具调用列表 + token 用量）

### 第四步：处理 LLM 返回的结果

情况分三种：

**情况 A：LLM 被 max_tokens 截断了**

就像 LLM 话说到一半被"字数限制"掐断了。恢复流程很精妙：

- 第一次截断 → 把输出上限从 8K 提到 64K，注入 "请从你刚才断掉的地方继续"，重试
- 第二次截断 → 最多再试 3 次，每次注入 "把工作拆成小块"，让 LLM 自己分步
- 3 次后还截断 → 放弃，走正常流程（可能丢失未完成的工具调用）

**情况 B：没有工具调用**

任务完成！把 LLM 的回复加入历史，视情况触发记忆提取，退出循环。

**情况 C：有工具调用**

继续下一步。

### 第五步：执行工具（Act 阶段）

1. **分区**：把工具调用分成"可以并发"和"必须串行"两组
   - 读文件、搜代码 → 可以并发（`asyncio.gather` 同时执行）
   - 写文件、跑命令 → 必须串行（一个完成才能下一个）
2. **安全检查**：每个工具调用都要过五道防线
3. **人工确认**：如果需要确认，yield 一个 `PermissionRequest`，等用户点允许/拒绝
4. **执行工具**：pre_tool_use 钩子 → 真正执行 → post_tool_use 钩子
5. **结果返回**：把工具输出加入对话历史

### 第六步：循环回去（Observe → Loop）

把工具结果加入对话 → 触发 turn_end 钩子 → 回到第二步。LLM 看到工具结果后继续推理下一步。

### 循环什么时候结束？

四种终止条件：
- LLM 不再产生工具调用（任务完成）
- 达到 max_iterations（50 轮，安全阀）
- 连续 3 次调用了不存在的工具（幻觉保护）
- Plan 模式下调用 ExitPlanMode

---

## 3. 事件驱动架构：Agent 如何和 UI 对话

### 3.1 Agent 不直接操作 UI

Agent 是一个异步生成器——它不画界面，只产出**事件**。TUI 收到事件后自己决定怎么渲染：

```
Agent.run() → yield 事件 → TUI 收到 → TUI 渲染
```

### 3.2 十种事件一览

| 事件 | 含义 | TUI 怎么处理 |
|------|------|------------|
| `StreamText` | LLM 输出一个字 | 追加到当前消息气泡末尾 |
| `ThinkingText` | LLM 思考内容 | 追加到可折叠的思考块 |
| `ToolUseEvent` | 要调用工具了 | 显示工具卡片（名称+参数） |
| `ToolResultEvent` | 工具执行完了 | 更新卡片状态（成功/失败+耗时） |
| `TurnComplete` | 本轮结束 | 重置输入状态 |
| `LoopComplete` | 全部完成 | 显示"完成，共 X 轮" |
| `UsageEvent` | Token 统计更新 | 更新状态栏 |
| `ErrorEvent` | 出错了 | 显示错误提示 |
| `PermissionRequest` | 需要确认 | 弹出权限对话框 |
| `CompactNotification` | 对话被压缩了 | 显示压缩提示 |

### 3.3 关键设计：PermissionRequest 的异步确认

这是整个项目最精妙的设计之一：

```
Agent 侧：                         TUI 侧：
发现需要用户确认                   收到 PermissionRequest
  ↓                                  ↓
创建 Future 对象                  弹出对话框 [Allow] [Deny]
（一个"空盒子"，等着被填）            ↓
  ↓                               用户点击 Allow
yield PermissionRequest             ↓
（把空盒子寄给 TUI）               future.set_result(ALLOW)
  ↓                                  ↓
await future ← 卡住等待            （把盒子填满，寄回给 Agent）
  ↓
拿到结果 → 继续执行
```

**为什么用 Future 而不是回调函数？**

用 Future 的话，Agent 代码从上到下自然流动——确认前的代码和确认后的代码在同一个函数里，变量都能直接访问。用回调的话，确认后的逻辑要拆出来单独写，状态要打包传递，复杂很多。

**为什么是 await 而不是同步阻塞？**

`await future` 只挂起当前协程，事件循环照常运转——TUI 动画还在播、用户还能点按钮。如果用同步阻塞（`.result()`），整个程序冻住，连"Allow"按钮都点不了。

---

## 4. 三协议适配：三种 API 如何统一

### 4.1 抽象接口只有一个方法

LLM 客户端基类极其简洁——只要求实现一个 `stream()` 方法：

接收：对话历史 + system prompt + 工具定义
返回：异步流式事件（文字/工具调用/思考/结束）

Agent 代码只需要：

```
async for event in client.stream(对话, system_prompt, 工具列表):
    处理 event
```

完全不知道底层是哪个模型。

### 4.2 三种具体实现

| 实现 | 用的 API | 适用模型 |
|------|---------|---------|
| AnthropicClient | `/v1/messages` | Claude 全系列 |
| OpenAIClient | `/v1/responses` | GPT-4o 等（OpenAI 新版 API） |
| OpenAICompatClient | `/v1/chat/completions` | DeepSeek、Qwen、Ollama、vLLM... |

### 4.3 同一个概念，三种 API 表达完全不同

以 "LLM 回复文字 + 调用一个工具" 这一个简单操作为例：

**Anthropic 方式**：内容是一个数组，文字和工具调用平铺：
```
role: assistant
content: [
  {type: text, text: "我来读文件"},
  {type: tool_use, id: tool_001, name: ReadFile, input: {file_path: main.py}}
]
```

**OpenAI Responses 方式**：文字和工具调用是分开的两个输出：
```
role: assistant, content: "我来读文件"
外加一条独立的 function_call 输出：
  {type: function_call, call_id: tool_001, name: ReadFile, arguments: {file_path: main.py}}
```

**OpenAI Chat Completions 方式**：文字直接是字符串，工具调用在独立字段里，还多一层 "function" 嵌套：
```
role: assistant, content: "我来读文件"
tool_calls: [
  {id: tool_001, type: function, function: {name: ReadFile, arguments: '{"file_path": "main.py"}'}}
]
```

**每种客户端的工作就是**：把内部统一的消息格式，转换成自己 API 要的格式；把 API 返回的流式事件，映射为内部统一的 7 种事件类型。

### 4.4 Anthropic 独有功能

**Prompt Caching（提示缓存）**：system prompt、工具定义、环境上下文在多轮对话中几乎不变。Anthropic 允许标记这些内容为"可缓存"，命中后只收 10% 费用。对于长对话非常省钱。

**Extended Thinking（扩展思考）**：让 Claude 在回复前深度思考（类似 "让我们一步步分析" 但更强大）。思考过程带数字签名防止篡改。

### 4.5 错误处理统一

三种 SDK 各自抛出不同的异常，但 CodeBot 把它们映射为内部统一的 4 种异常：
- `AuthenticationError` → API Key 不对
- `RateLimitError` → 请求太频繁（含"多少秒后重试"）
- `NetworkError` → 网络不通
- `LLMError` → 其他错误

上层代码只需要 `try...except AuthenticationError`，不用管底层是 Anthropic 还是 OpenAI。

---

## 5. 工具调用如何分区并发执行

### 5.1 不是所有工具都能同时跑

LLM 可能一次返回多个工具调用。但有些能并发，有些必须串行：

- ✅ **可以并发**：读文件、搜代码（只读，不影响文件系统）
- ❌ **必须串行**：写文件、跑命令（有副作用，有依赖关系）

### 5.2 分区逻辑

把工具调用列表从头到尾遍历一遍，遇到"可并发"的就放进当前批次，遇到"必须串行"的就开始新批次：

```
输入: [ReadFile(a.py), ReadFile(b.py), Bash(rm xxx), ReadFile(c.py)]

输出:
  批次1 (并发): ReadFile(a.py) + ReadFile(b.py)  ← 两个同时读
  批次2 (串行): Bash(rm xxx)                     ← 等批次1完成，单独执行
  批次3 (串行): ReadFile(c.py)                   ← 等批次2完成，单独执行
```

注意第三个 ReadFile 被分到了单独批次——因为它前面的 Bash 可能修改了文件，必须先等 Bash 完成再读。

### 5.3 并发执行

`asyncio.gather(*tasks)` 同时启动多个异步任务，等全部完成再返回结果。读 3 个文件只需最慢那个的时间。

---

## 6. 面试高频点

### 6.1 "ReAct 循环的每一步是干什么的？"

**核心思路**：不要背代码，讲清楚四个阶段和一个循环。

- **Think**：LLM 分析当前状态，决定下一步做什么（可能产生文字+工具调用）
- **Act**：执行工具调用（先安全检查，再实际执行）
- **Observe**：把工具返回的结果交由 LLM 分析
- **Loop**：如果没完成就回到 Think，如果 LLM 不再产生工具调用就结束

**追问**：循环结束条件有几种？
- LLM 不再产生工具调用（正常完成）
- 超过 50 轮（安全阀）
- 连续 3 次调用不存在的工具（幻觉保护）

### 6.2 "max_tokens 截断怎么恢复？"

三层恢复策略：
1. 提升上限：8K → 64K
2. 提示继续：注入 "从断掉的地方继续"
3. 分解工作：提示 "拆成小块"，最多再试 3 次

### 6.3 "怎么做到切换模型不改代码？"

策略模式 + 工厂函数：定义统一接口 `stream()`，三个客户端各自实现，配置里写 `protocol: openai-compat` 就自动选对应实现。新增一个模型只需要新增一个客户端类，核心代码一行不动。

### 6.4 "工具怎么决定并发还是串行执行？"

关键依据：工具是否 `is_concurrency_safe`。只读工具（读文件、搜代码）可以并发，有副作用的工具（写文件、跑命令）必须串行。分区时还要考虑工具之间的逻辑顺序——LLM 先调 ReadFile 再调 EditFile，它们就不能在同一个并发批次里。

---

> **下一步**：阶段3深入工具系统的设计与五层安全模型。

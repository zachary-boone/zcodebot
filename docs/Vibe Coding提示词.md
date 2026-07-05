## ch01：前置准备

```markdown
我正在构建一个终端 AI 编程助手（类似 Claude Code），项目名叫 CodeBot，使用的编程语言是
[你的语言]。

每次我会提出一个初步的想法，需要你通过向我提问，帮助我澄清需求、挖掘边缘场景。澄清清楚后共创三份文档保存到项目根目录：

# 三份文档的角色与边界

## spec.md
回答：要解决什么问题、做哪些能力、不做哪些、什么算完成。
写：背景、目标用户、能力清单（一句话一条）、非功能要求、设计骨架、Out of Scope
不写：具体函数名 / 参数名 / 默认值 / 错误文本 / 行号 / SDK 类型名
   （这些是实现细节，spec 改一次就过期，维护爆炸）

## tasks.md
回答：按什么顺序做、每步动什么文件。
- 5~15 个任务，每个能在一次专注会话内完成
- 每个任务标注：影响文件、依赖任务、参考资料定位（精确到函数/行号都可以）
- 最后一定有「接入主流程」+「端到端验证」两个任务

## checklist.md
每一项必须可勾选、可观测，不许写「实现完整」「质量良好」。
- 把 spec 里被砍掉的具体值（错误文本、默认值、阈值）放进来作为验收项
- 写法举例：「`grep -r X` 返回 ≥3 条」「输入 Y 看到输出 Z」
- 至少一条端到端验收
```

## ch02

```markdown
# 我的初步想法
我要从零开始做一个命令行AI助手(Coding Agent)，叫CodeBot，类似ClaudeCode。用[你的语言]开发。
 
这一步的目标是：用户在终端启动CodeBot后，进入一个交互式对话界面（TUI），可以输入问题，CodeBot调用大模型API，把回复流式地
逐字打印出来。支持多轮对话，AI能记住之前说过的话。
技术要求：
支持AnthropicClaude和OpenAI两种API后端，通过配置文件切换流式用SSE，不是等全部生成完再返回
支持Claude的extended thinking
Provider层要抽象成统一接口，以后方便加新的后端
这一步不做 tooluse、文件操作、代码编辑这些agent 功能，纯对话就行。
 
配置格式：用 YAML 配置文件管理 LLM 供应商信息，四个核心字段：
protocol 决定走哪家协议
model 指定模型
base_url指定请求的地址
api_key 做认证。
```

### Go

```markdown
# ch02: 让 AI 开口说话 Spec

## 1. 背景

Agent 落地的第一步是让上层（Agent Loop / TUI / SubAgent）能用同一套接口和 LLM 收发，不必各自面对 SSE 流、Thinking 签名回传、Provider 间消息差异。本章把 LLM 通信、流式响应、Extended Thinking、Token 统计以及两层消息模型封装到 `internal/llm` 与 `internal/conversation`，是 ch03+ 工具循环的前置依赖。

## 2. 目标

交付统一的 `llm.Client` 流式接口和两个内置 Provider 实现（Anthropic、OpenAI Responses），加上 `conversation.Manager` 两层消息模型（内部带 thinking / tool use / tool result 的 `Message`，序列化到具体 Provider 的请求体）。上层（Agent Loop、TUI 装配点、SubAgent、Compact）拿一个 `Client` 就能跑，不再触碰 SSE 细节。

## 3. 功能需求

- F1: `llm.Client` 统一暴露流式接口，输入是会话管理器和工具 schema，输出是事件通道 + 错误通道。
- F2: 客户端工厂按 Provider Protocol 路由到 Anthropic 或 OpenAI 实现，未知 protocol 报错。
- F3: 事件流覆盖五类信号：文本 delta、thinking delta / complete（含签名）、tool call 三段（start / delta / complete）、流结束（含 stop reason 与 usage）、用量统计。所有事件用 sum type 收口。
- F4: Anthropic 客户端基于官方 SDK，支持 Extended Thinking 两种模式：高版本模型走 Adaptive Thinking，低版本回退到固定 budget 的 Enabled Thinking，模型版本能力判断在客户端内部完成。
- F5: OpenAI 客户端基于 Responses API（非 Chat Completions），支持把 reasoning summary 还原成 thinking delta / complete 事件，让上层看到的事件形状和 Anthropic 一致。
- F6: 两个客户端都需要应对 SDK 静默阻塞——通过空闲超时（独立 readNext goroutine + select ctx/idle）兜底，超时归类为网络错误退出。
- F7: 错误分类有 5 类：通用 LLMError、AuthenticationError、RateLimitError（带 RetryAfter）、NetworkError、ContextTooLongError。各客户端把 SDK / HTTP 错误归类到这 5 类之一，上层只面对统一错误。
- F8: `conversation.Message` 支持完整字段：role / content / thinking blocks / tool uses / tool results。所有写操作走 `Manager` 方法，禁止外部直接改 history。
- F9: `Manager` 提供深拷贝读和按 Protocol 序列化两个出口，序列化时不丢字段（thinking signature、tool arguments、tool result IsError 都要原样回到下一轮请求）。
- F10: `Manager` 提供 system-reminder 注入入口，把内容包成 `<system-reminder>` 标签作为 user 消息追加，供 ch04 Plan Mode、ch08 Compact、ch09 Memory 复用。
- F11: 提供模型短名解析器（haiku / sonnet / opus → 具体模型 ID），供 ch13 SubAgent 切模型。

## 4. 非功能需求

- N1: 事件通道有缓冲，SSE 读取与事件分发用独立 channel 解耦，事件写入不阻塞 SSE 读。
- N2: ctx 取消（如 TUI ctrl+c）必须在一个 SSE 事件周期内退出 Stream goroutine，并通过错误通道抛出 NetworkError。
- N3: SDK 静默阻塞要被空闲超时兜底，避免拖死整个 agent loop。
- N4: 序列化层不丢字段（thinking signature / tool arguments / tool result IsError 全部往返保留）。
- N5: `conversation.Manager` 不加锁——单消费者模型，调用方负责串行化（agent loop 单 goroutine 顺序追加）。

## 5. 设计概要

- 核心数据结构:
 - `llm.Client`（流式接口）/ `llm.MaxTokensSetter`（可选接口，让 ch04 升级 max_tokens）
 - `llm.StreamEvent` sum type
 - `llm.UsageInfo`
 - 5 类错误类型
 - `conversation.Message` / `conversation.Manager`（私有 history slice）
- 主流程（每轮 LLM 请求）:
 1. Agent Loop 调 `client.Stream(ctx, conv, toolSchemas)`
 2. 客户端把 Manager 历史序列化成 SDK 入参，调 SDK 流式接口
 3. 独立 goroutine 读 SDK，主 goroutine select ctx / 空闲超时 / SDK 事件
 4. 按 SDK 事件类型 push 对应 `StreamEvent`
 5. 流结束 push `StreamEnd`；异常经错误分类后写到错误通道
- 调用链（模块层级）:
 - TUI 装配 → `llm.NewClient(provider)` → 传给 `agent.New`
 - Agent loop → `Client.Stream` → 消费事件 → 写回 `conversation.Manager`
 - SubAgent / Compact / Teammate worker 复用同一 `Client` 接口
- 与其他模块的交互:
 - 依赖 `internal/config`（Provider 配置、API key、token 上限）
 - 被 `internal/agent`、`internal/agents`、`internal/compact`、`internal/tui`、`cmd/codebot/teammate` 调用
 - 与 `internal/tools` 解耦：`Stream` 只接 `[]map[string]any` schema，工具注册中心由 `tools.Registry` 提供

## 6. Out of Scope

- 多模态输入（image / PDF）的请求体构造：当前 `Message.Content` 仅 string，未来章节再扩
- 自动重试与指数退避：rate limit 的重试在 ch04 Agent Loop 处理，不在 ch02 范围
- Provider 抽象细分（Bedrock / Vertex / Azure-OpenAI）：当前只支持原生 Anthropic 与原生 OpenAI Responses
- Prompt caching / Cache breakpoints：目标设计已有，本仓库暂未实现

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch02: 让 AI 开口说话 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。

## T1: 定义 `llm.Client` 接口与工厂
- 影响文件: `internal/llm/client.go`
- 依赖任务: 无
- 完成标准: `internal/llm/client.go:11-13` 声明 `Client` 接口（含 `Stream` 单方法）；`internal/llm/client.go:19-28` 实现 `NewClient(cfg, systemPrompt)` 按 protocol 分流。

## T2: 实现流式事件 sum type
- 影响文件: `internal/llm/events.go`
- 依赖任务: T1
- 完成标准: `internal/llm/events.go:1-34` 定义 7 个事件类型（TextDelta/ThinkingDelta/ThinkingComplete/ToolCallStart/ToolCallDelta/ToolCallComplete/StreamEnd）+ `UsageInfo`，全部通过 `streamEvent` 私有方法绑定到 `StreamEvent` 接口。

## T3: 实现错误分层
- 影响文件: `internal/llm/errors.go`
- 依赖任务: T1
- 完成标准: `internal/llm/errors.go:3-32` 定义 `LLMError`、`AuthenticationError`、`RateLimitError{RetryAfter}`、`NetworkError`、`ContextTooLongError`，全部实现 `Error` 方法。

## T4: 实现 Anthropic 客户端
- 影响文件: `internal/llm/anthropic.go`
- 依赖任务: T1, T2, T3
- 完成标准:
 - `internal/llm/anthropic.go:21` 实现 `supportsAdaptiveThinking(model)` 覆盖 4.6/4.7 但拒 4.5；
 - `internal/llm/anthropic.go:71` 实现 `Stream`，含 SSE 读 goroutine + idle.C(5min) + ctx.Done 三路 select；
 - `internal/llm/anthropic.go:248` 实现 `buildAnthropicMessages` 把 `conversation.Message` 序列化成 `[]anthropic.MessageParam`，含 thinking block / tool_use / tool_result；
 - `internal/llm/anthropic.go:299` 实现 `classifyAnthropicError` 按 413/auth/rate-limit/default 分支返回不同错误类型。

## T5: 实现 OpenAI Responses 客户端
- 影响文件: `internal/llm/openai.go`
- 依赖任务: T1, T2, T3
- 完成标准:
 - `internal/llm/openai.go:32` 实现 `newOpenAIClient`；
 - `internal/llm/openai.go:59` 实现 `Stream`，支持 reasoning effort=high/summary=detailed + `reasoning.encrypted_content` include；
 - `internal/llm/openai.go:209` 实现 `buildOpenAIInput` 把内部消息映射到 `responses.ResponseInputParam`；
 - `internal/llm/openai.go:262` 实现 `classifyOpenAIError`；`:290` 实现 `containsContextLengthError`。

## T6: 实现 Model Resolver（短名映射）
- 影响文件: `internal/llm/model_resolver.go`
- 依赖任务: T1
- 完成标准: `internal/llm/model_resolver.go:5-9` 定义 `modelAliases` map（haiku/sonnet/opus）；`:11` 暴露 `NewModelResolver(baseCfg)` 返回 `func(shortName) (Client, error)`。

## T7: 实现 `conversation.Manager` 与消息类型
- 影响文件: `internal/conversation/conversation.go`
- 依赖任务: 无
- 完成标准:
 - `internal/conversation/conversation.go:5-28` 定义 `ToolUseBlock`、`ToolResultBlock`、`ThinkingBlock`、`Message`；
 - `internal/conversation/conversation.go:30-99` 实现 `Manager` + 8 个 Add 方法（含 `AddSystemReminder` 包裹 `<system-reminder>` 标签）；
 - `internal/conversation/conversation.go:100-105` 实现 `GetMessages` 返回深拷贝；
 - `internal/conversation/conversation.go:106-196` 实现 `Serialize(protocol)` 分发到 `serializeAnthropic` / `serializeOpenAI`，含同角色文本消息合并逻辑。

## T8: 覆盖 Thinking + Reasoning 行为测试
- 影响文件: `internal/llm/thinking_test.go`
- 依赖任务: T4, T5, T7
- 完成标准:
 - `internal/llm/thinking_test.go:45TestSupportsAdaptiveThinking` 验证 4.6/4.7=true、4.5=false、非 Claude=false；
 - `:69TestAnthropicThinkingAdaptive` 断言 4.6 走 adaptive、无 budget_tokens；
 - `:97TestAnthropicThinkingEnabled` 断言非官方模型走 enabled、budget=maxTokens-1；
 - `:130TestAnthropicThinkingDisabled` 断言 thinking=false 时请求体无 thinking 字段；
 - `:154TestAnthropicThinkingBlocksInConversation` 断言 thinking block 的 signature 能往返；
 - `:200`、`:276` 分别覆盖 OpenAI reasoning enabled/disabled。

## T9: 接入主流程
- 影响文件: `internal/tui/tui.go`、`internal/agent/agent.go`、`cmd/codebot/teammate.go`
- 依赖任务: T1-T7
- 完成标准:
 - `internal/tui/tui.go:352` 用 `llm.NewClient(p, systemPrompt)` 构造 client；
 - `internal/tui/tui.go:360` 把 client 传给 `agent.New(client, m.registry, p.Protocol)`；
 - `internal/agent/agent.go:105` Agent Loop 调用 `a.Client.Stream(ctx, conv, toolSchemas)`；
 - `cmd/codebot/teammate.go:82` teammate worker 也走 `llm.NewClient(&provider, "")`。

## T10: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T9
- 完成标准:
 - `go build ./...` 通过；
 - `go test ./internal/llm/...` 通过（6 个 thinking_test 全绿）；
 - 在 TUI 中发送任意一句话，能看到流式文本（TextDelta）被逐 token 渲染到对话窗口，证明 Stream 通道与事件渲染端到端打通。

## 进度
- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8
- [ ] T9
- [ ] T10

```

```markdown
# ch02: 让 AI 开口说话 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性

- [ ] `Client` 接口在 `internal/llm/client.go:11-13` 实现，签名 `Stream(ctx, conv, tools) (<-chan StreamEvent, <-chan error)`（`grep -n 'type Client interface' internal/llm/client.go`）。
- [ ] `MaxTokensSetter` 接口在 `internal/llm/client.go:15-17` 实现（`grep -n 'type MaxTokensSetter' internal/llm/client.go`）。
- [ ] `NewClient` 在 `internal/llm/client.go:19-28` 按 protocol ∈ {anthropic, openai} 分流，未知 protocol 返回 `fmt.Errorf("unknown protocol: %s", ...)`。
- [ ] 7 个流式事件类型 + `UsageInfo` 在 `internal/llm/events.go:1-34` 齐全，全部绑定 `streamEvent()` 私有方法。
- [ ] `LLMError`/`AuthenticationError`/`RateLimitError{RetryAfter}`/`NetworkError`/`ContextTooLongError` 在 `internal/llm/errors.go:3-32` 齐全。
- [ ] `supportsAdaptiveThinking` 在 `internal/llm/anthropic.go:21-33` 严格按 `claude-opus-4-` / `claude-sonnet-4-` 且 minor ≥ '6' 判定。
- [ ] `anthropicClient.Stream` 在 `internal/llm/anthropic.go:71-246` 实现：
 - [ ] SSE 读取在独立 goroutine（`readNext`，`anthropic.go:139-141`）；
 - [ ] `select` 含 `ctx.Done()` / `idle.C` / `nextCh` 三路（`anthropic.go:149-157`）；
 - [ ] `accMessage.Accumulate(event)` 累积消息（`anthropic.go:172`）；
 - [ ] 在 ContentBlockStart 处分别识别 `thinking` / `tool_use`；ContentBlockDelta 处分别识别 `ThinkingDelta` / `SignatureDelta` / `TextDelta` / `InputJSONDelta`；
 - [ ] StreamEnd 携带 StopReason（默认 `end_turn`）与 `UsageInfo`。
- [ ] `buildAnthropicMessages` 在 `internal/llm/anthropic.go:248-297` 处理 assistant 的 thinking blocks / text / tool_use 合并，并把 tool_results 包成 user 消息。
- [ ] `classifyAnthropicError` 在 `internal/llm/anthropic.go:299-325` 覆盖 413 / `prompt is too long` / `AuthenticationError` / `RateLimitError`（取 `Retry-After` 头）/ default。
- [ ] `openaiClient.Stream` 在 `internal/llm/openai.go:59-207` 处理 `response.output_text.delta`、`response.output_item.added`（function_call / reasoning）、`response.reasoning_summary_text.delta/done`、`response.function_call_arguments.delta/done`、`response.completed`。
- [ ] OpenAI thinking=true 时设置 `reasoning.effort=high` / `summary=detailed` / `include=[reasoning.encrypted_content]`（`internal/llm/openai.go:91-99`）。
- [ ] `classifyOpenAIError` 在 `internal/llm/openai.go:262-288` 处理 413 + 400/`context_length_exceeded`、401、429、default；`containsContextLengthError` 在 `:290` 覆盖三种关键字。
- [ ] `NewModelResolver` 在 `internal/llm/model_resolver.go:11-21` 暴露短名 → ID 映射闭包。
- [ ] `conversation.Message{Role, Content, ThinkingBlocks, ToolUses, ToolResults}` 在 `internal/conversation/conversation.go:22-28` 定义。
- [ ] `Manager` 8 个 Add 方法 + GetMessages + Serialize 在 `internal/conversation/conversation.go:34-196` 齐全。
- [ ] `AddSystemReminder` 包裹 `<system-reminder>\n{content}\n</system-reminder>`（`internal/conversation/conversation.go:93-98`）。
- [ ] `serializeAnthropic` 合并同角色连续文本消息以维持 user/assistant 交替（`internal/conversation/conversation.go:142-160`）。

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `llm.NewClient` 至少 4 个非测试调用方（`grep -rn "llm.NewClient" --include="*.go" /Users/codemelo/codebot | grep -v _test.go` 命中 `internal/tui/tui.go:352`、`:714`、`cmd/codebot/teammate.go:82`）。
- [ ] `conversation.NewManager` 至少 6 个非测试调用方（`grep -rn "conversation.NewManager" --include="*.go" /Users/codemelo/codebot | grep -v _test.go` 命中 TUI/Compact/Agents/Teammate 等）。
- [ ] `agent.go:105` 实际调用 `a.Client.Stream(ctx, conv, toolSchemas)`，证明 Client 接口接到 Agent Loop。
- [ ] `agent.go:117-142` 消费 `ThinkingDelta`/`ThinkingComplete`/`TextDelta`/`ToolCallStart`/`ToolCallDelta`/`ToolCallComplete`/`StreamEnd` 七种事件，无未处理事件类型遗漏。
- [ ] `agent.go:172/180/192/205` 通过 `conv.AddAssistantFull(text, thinkingBlocks, toolUses)` 把 thinking 与 tool 写回历史，保证下一轮能回放 signature。
- [ ] `NewModelResolver` 在 `internal/tui/tui.go:546` 被 `agents.AgentTool` 装配时使用（`grep -rn "NewModelResolver" --include="*.go"`）。
- [ ] `LLMError` / `ContextTooLongError` / `RateLimitError` / `NetworkError` 在 `internal/agent/agent.go:264-288` 的 `handleStreamError` 中被 `errors.As` 消费，错误链未断。

## 3. 编译与测试

- [ ] `go build ./...` 通过。
- [ ] `go test ./internal/llm/...` 通过：6 个 thinking_test 全绿（`go test -run 'Test.*Thinking' ./internal/llm/...`）。
- [ ] `go vet ./internal/llm/... ./internal/conversation/...` 无警告。

## 4. 端到端验证

- [ ] TUI 启动后发送 `hello`，对话窗口逐 token 渲染流式回复——证明 `TextDelta` 通道接到 `internal/tui/tui.go` 的事件渲染。
- [ ] 模型为 `claude-sonnet-4-6`（或更新）时，配置 `thinking: true` 后能在对话区看到 thinking 文本流（`ThinkingDelta` → `tui` 渲染），证明 adaptive thinking 接通。
- [ ] 提供故意失败的 API key 后 TUI 显示 `Invalid API key: ...`（`AuthenticationError` 路径），证明错误分类生效。
- [ ] 留存证据: `internal/llm/thinking_test.go` 在 `go test -v` 下输出 `Official model → adaptive: ...` 等日志行（`thinking_test.go:94`、`:127`）。

## 5. 文档

- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`/Users/codemelo/codebot/specs/go/ch02/`）。

```

### Python

```markdown
# ch02: 让 AI 开口说话 Spec

## 1. 背景

Agent 落地的第一步是让上层（Agent Loop / TUI / SubAgent / Skill / Team）能用同一套接口和 LLM 收发，不必各自面对 SSE 流、Thinking 签名回传、Provider 间消息差异。本章把 LLM 通信、流式响应、Extended Thinking、Token 统计以及两层消息模型封装到 `codebot/client.py` 与 `codebot/conversation.py`，是 ch03+ 工具循环与 ch08 Compact 的前置依赖。

## 2. 目标

交付统一的 `LLMClient` ABC（异步流式接口）和两个内置实现（`AnthropicClient`、`OpenAIClient`），加上 `ConversationManager` 两层消息模型（内部带 thinking / tool use / tool result 的 `Message` dataclass，序列化到具体 Provider 的请求体）。上层（Agent Loop、TUI 装配点、SubAgent、Compact、Skill）拿一个 `LLMClient` 就能跑，不再触碰 SSE 细节。

## 3. 功能需求

- F1: `LLMClient` 是 `ABC`，暴露唯一 `async def stream(conversation, system, tools) -> AsyncIterator[StreamEvent]` 抽象方法，外加 `set_max_output_tokens(tokens)` 非抽象基类方法。
- F2: 客户端工厂 `create_client(config: ProviderConfig)` 按 `config.protocol ∈ {anthropic, openai}` 路由到 `AnthropicClient` / `OpenAIClient`，未知 protocol 抛 `ValueError("Unknown protocol: ...")`。
- F3: 流式事件由 `codebot/tools/base.py` 集中定义，覆盖五类信号：`TextDelta`、`ThinkingDelta` / `ThinkingComplete`（含签名）、`ToolCallStart` / `ToolCallDelta` / `ToolCallComplete`、`StreamEnd`（含 stop reason 与 input/output tokens）。所有事件以 dataclass + `StreamEvent` Union 类型收口，供 `isinstance` 分发。
- F4: `AnthropicClient` 基于 `anthropic.AsyncAnthropic` SDK，支持 Extended Thinking 两种模式：`_supports_adaptive_thinking(model)` 命中（claude-opus/sonnet-4- 且 minor ≥ 6）时走 adaptive（`type: enabled` + `budget_tokens: 0`），否则回退到固定 budget（`max_output_tokens - 1`，最小 1024）。模型版本能力判断在客户端内部完成。
- F5: `OpenAIClient` 基于 OpenAI `responses.create(stream=True)` Responses API（非 Chat Completions），覆盖 `response.output_text.delta`、`response.output_item.added`（function_call）、`response.function_call_arguments.delta/done`、`response.completed` 五类 SDK 事件。
- F6: 两个客户端的 `stream()` 是 async generator，通过 `async for event in stream` 逐事件 `yield` `StreamEvent` 到调用方；取消由 `asyncio` 协作式 cancel（上层 `agent_task.cancel()` 即可终止）。
- F7: 错误分类有 4 类：`LLMError`（基类）、`AuthenticationError`、`RateLimitError(retry_after)`、`NetworkError`。各客户端在 `except` 分支把 SDK 异常归类到这 4 类之一，`raise ... from e` 保留异常链；上层只面对统一错误。
- F8: `Message` dataclass 支持完整字段：`role` / `content` / `thinking_blocks` / `tool_uses` / `tool_results`，每个 block 用独立 dataclass（`ThinkingBlock` / `ToolUseBlock` / `ToolResultBlock`）。
- F9: `ConversationManager` 提供 `add_user_message` / `add_assistant_message` / `add_tool_results_message` 等写入方法；`get_messages` 返回 list 浅拷贝；`serialize(protocol)` 分发到 `_serialize_anthropic` / `_serialize_openai`，序列化时不丢字段（thinking signature、tool input、tool_result is_error 都要原样回到下一轮请求）。
- F10: `ConversationManager.add_system_reminder(content)` 把内容包成 `<system-reminder>\n{content}\n</system-reminder>` 作为 user 消息追加；`_serialize_anthropic` 在序列化时把连续 user reminder 合并进上一条 user 消息（避免 user/assistant 不交替）。
- F11: `ConversationManager.inject_environment(context)` 与 `inject_long_term_memory(instructions, memories)` 提供幂等的 head-insert：用 `env_injected` / `ltm_injected` 标志位避免重复注入，供 ch04 Agent Loop 启动与 ch08 Compact 后重注入。
- F12: 模型短名映射在 `codebot/tools/agent_tool.py::_create_client_for_model` 内联实现：`{"haiku", "sonnet", "opus"}` → 具体模型 ID，配合父 Agent 的 `ProviderConfig` 复制出子 client，供 ch13 SubAgent 切模型。

## 4. 非功能需求

- N1: `stream()` 是 native async generator，事件经 `yield` 直接驱动上层 `async for`，无中间 `asyncio.Queue`，调度成本最小。
- N2: 上层 cancel（如 TUI ctrl+c）走 `asyncio.CancelledError`，必须在当前事件循环 tick 内退出 `stream()` 协程；SDK 的 `async with messages.stream(...)` 上下文负责连接清理。
- N3: 序列化层不丢字段：thinking signature / tool input dict / tool_result `is_error` 全部往返保留；assistant 消息有 thinking 或 tool_use 时强制走 list-of-blocks 路径。
- N4: `ConversationManager` 不加锁——单消费者模型，调用方（Agent Loop）负责串行化追加。
- N5: `ProviderConfig.get_max_output_tokens()` 在 `thinking=True` 时默认 64000，否则 8192；`set_max_output_tokens(tokens)` 允许 ch04 在 `stop_reason == max_tokens` 时升档到 `MAX_TOKENS_CEILING`。

## 5. 设计概要

- 核心数据结构:
 - `LLMClient` ABC（含 `stream` + `set_max_output_tokens`）
 - `StreamEvent` Union（7 个事件 dataclass + `StreamEnd`）
 - 4 类错误类型（`LLMError` / `AuthenticationError` / `RateLimitError` / `NetworkError`）
 - `Message` / `ToolUseBlock` / `ToolResultBlock` / `ThinkingBlock` dataclass
 - `ConversationManager` dataclass（私有 history list + `env_injected` / `ltm_injected` 标志）
- 主流程（每轮 LLM 请求）:
 1. Agent Loop 调 `self.client.stream(conversation, system, tools)` 得到 `AsyncIterator[StreamEvent]`
 2. 客户端 `conversation.serialize(protocol)` 序列化历史为 SDK 入参
 3. `AnthropicClient` 用 `async with self._client.messages.stream(**kwargs) as stream: async for event in stream` 拉流；`OpenAIClient` 用 `await self._client.responses.create(...)` 拿到 `response_stream`，然后 `async for event in response_stream`
 4. 按 SDK 事件类型 `yield` 对应 `StreamEvent` dataclass
 5. 流结束 yield `StreamEnd(stop_reason, input_tokens, output_tokens)`；异常经 `except SDK.XXX` 分支转成 4 类错误后 `raise ... from e`，由上层 `try/except LLMError` 捕获
- 调用链（模块层级）:
 - TUI 装配 → `create_client(provider)` → 赋给 `CodeBotApp.client` → 传给 `Agent(client=...)`
 - Agent Loop → `client.stream(...)` → `StreamCollector.consume(stream)` → `LLMResponse` → 写回 `ConversationManager`
 - SubAgent（`AgentTool._create_client_for_model`）/ Skill Fork（`SkillExecutor`）复用同一 `LLMClient` 接口
- 与其他模块的交互:
 - 依赖 `codebot/config.py`（`ProviderConfig`、`resolve_api_key`、`get_max_output_tokens`）
 - 被 `codebot/agent.py`、`codebot/app.py`、`codebot/tools/agent_tool.py`、`codebot/skills/executor.py` 直接调用
 - 与 `codebot/tools/` 解耦：`stream` 只接 `list[dict[str, Any]]` schema，工具注册中心由 `ToolRegistry` 提供

## 6. Out of Scope

- 多模态输入（image / PDF）请求体构造：`Message.content` 当前仅 `str`，未来章节再扩
- SDK 静默阻塞的空闲超时兜底：Python 当前依赖 asyncio cancel + SDK 自身超时，不在客户端做 idle watchdog
- `ContextTooLongError` 与 `context_length_exceeded` 关键字归类：Python 当前在 413 / 400 时只回 `LLMError(status_code, message)`，由上层 Compact 流程兜底
- OpenAI Responses API 的 reasoning summary / encrypted_content：Python 端暂未实现 reasoning 事件还原，OpenAI 路径无 thinking
- 自动重试与指数退避：rate limit 的重试在 ch04 Agent Loop 处理，不在 ch02 范围
- Provider 抽象细分（Bedrock / Vertex / Azure-OpenAI）：当前只支持原生 Anthropic 与原生 OpenAI Responses
- 模型短名解析的独立模块化：当前内联在 `agent_tool.py::_create_client_for_model`，未来抽出 `model_resolver.py`

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch02: 让 AI 开口说话 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。

## T1: 定义 `LLMClient` ABC 与工厂

- 影响文件: `codebot/client.py`
- 依赖任务: 无
- 完成标准: `codebot/client.py:42-53` 声明 `LLMClient(ABC)`，含抽象 `async def stream(conversation, system, tools) -> AsyncIterator[StreamEvent]` 和非抽象 `set_max_output_tokens`；`codebot/client.py:296-301` 实现 `create_client(config)` 按 `config.protocol` 分流，未知 protocol `raise ValueError("Unknown protocol: ...")`。

## T2: 实现流式事件 dataclass union

- 影响文件: `codebot/tools/base.py`
- 依赖任务: T1
- 完成标准: `codebot/tools/base.py:51-92` 定义 7 个事件 dataclass（`TextDelta` / `ToolCallStart` / `ToolCallDelta` / `ToolCallComplete` / `ThinkingDelta` / `ThinkingComplete` / `StreamEnd`），最后一行用 `StreamEvent = TextDelta | ThinkingDelta | ... | StreamEnd` 形成 Union 类型，供 `isinstance` 分发。

## T3: 实现错误分层

- 影响文件: `codebot/client.py`
- 依赖任务: T1
- 完成标准: `codebot/client.py:24-40` 定义 `LLMError(Exception)`、`AuthenticationError(LLMError)`、`RateLimitError(LLMError)`（含 `retry_after: float | None` 字段与 `__init__` 复写）、`NetworkError(LLMError)`，全部继承自统一基类 `LLMError`，上层只需 `except LLMError`。

## T4: 实现 `AnthropicClient`

- 影响文件: `codebot/client.py`
- 依赖任务: T1, T2, T3
- 完成标准:
 - `codebot/client.py:56-63` 实现 `_supports_adaptive_thinking(model)`：扫描 `claude-opus-4-` / `claude-sonnet-4-` 前缀且后续首字符 `>= '6'`；
 - `codebot/client.py:65-78` 实现 `__init__`：从 `ProviderConfig.resolve_api_key()` 取 key，无 key 时直接抛 `AuthenticationError`；构造 `AsyncAnthropic(api_key, base_url)`；
 - `codebot/client.py:81-174` 实现 `async def stream`：序列化 `conversation.serialize("anthropic")` → 拼 `kwargs` → 按 `_supports_adaptive_thinking` 分流 thinking → `async with self._client.messages.stream(**kwargs) as stream: async for event in stream` 解析 `content_block_start` / `content_block_delta`（thinking_delta / signature_delta / text_delta / input_json_delta）/ `content_block_stop` / `message_stop`，最后 `await stream.get_final_message()` 拿 usage 与 stop_reason；
 - `codebot/client.py:176-187` 实现错误分类 `except` 链：`anthropic.AuthenticationError` → `AuthenticationError`；`anthropic.RateLimitError` → `RateLimitError(retry_after=float(retry))`；`anthropic.APIConnectionError` → `NetworkError`；`anthropic.APIStatusError` → `LLMError(API error ({status_code}): ...)`；均 `raise ... from e`。

## T5: 实现 `OpenAIClient`

- 影响文件: `codebot/client.py`
- 依赖任务: T1, T2, T3
- 完成标准:
 - `codebot/client.py:190-201` 实现 `__init__`：从 `ProviderConfig.resolve_api_key()` 取 key，无 key 抛 `AuthenticationError`；构造 `AsyncOpenAI(api_key, base_url)`；
 - `codebot/client.py:205-278` 实现 `async def stream`：序列化 `conversation.serialize("openai")` → 拼 `kwargs` → `await self._client.responses.create(**kwargs)` → `async for event in response_stream` 分发：
 - `response.output_text.delta` → `TextDelta`
 - `response.function_call_arguments.delta` → 首次到达时回写 `tool_name` / `call_id` 并 yield `ToolCallStart`，后续累积 `json_accum` 并 yield `ToolCallDelta`
 - `response.function_call_arguments.done` → `json.loads(json_accum)` 解析后 yield `ToolCallComplete`
 - `response.output_item.added` 当 `item.type == "function_call"` → yield `ToolCallStart`
 - `response.completed` → 从 `event.response.usage` 取 `input_tokens` / `output_tokens` yield `StreamEnd("end_turn", ...)`；
 - `codebot/client.py:280-293` 实现错误分类 `except` 链：`openai.AuthenticationError` / `RateLimitError` / `APIConnectionError` / `APIStatusError` → 对应 4 类错误，`raise ... from e`。

## T6: 实现模型短名映射

- 影响文件: `codebot/tools/agent_tool.py`
- 依赖任务: T1
- 完成标准: `codebot/tools/agent_tool.py:612-637` 实现 `_create_client_for_model(model_alias)`：内联 `model_map = {"haiku": "claude-haiku-4-5-...", "sonnet": "claude-sonnet-4-6-...", "opus": "claude-opus-4-6-..."}`；从父 Agent `self._provider_config` 拷出 `ProviderConfig` 复写 `name` / `model`，调用 `create_client(config)` 返回 `LLMClient` 实例。

## T7: 实现 `ConversationManager` 与消息 dataclass

- 影响文件: `codebot/conversation.py`
- 依赖任务: 无
- 完成标准:
 - `codebot/conversation.py:8-34` 定义 `ToolUseBlock` / `ToolResultBlock` / `ThinkingBlock` / `Message` 四个 dataclass，所有字段类型清楚；
 - `codebot/conversation.py:37-113` 实现 `ConversationManager` dataclass（含 `history`、`env_injected`、`ltm_injected`、`last_input_tokens` 字段）+ 8 个写入方法（`add_user_message` / `add_assistant_message` / `add_system_reminder` / `add_tool_results_message` / `inject_environment` / `inject_long_term_memory` / `replace_history` / `get_messages`）；
 - `codebot/conversation.py:62-68` 实现 `add_system_reminder` 把 content 包成 `<system-reminder>\n{content}\n</system-reminder>` 作为 user 消息追加；
 - `codebot/conversation.py:117-189` 实现 `serialize(protocol)` 分发到 `_serialize_anthropic` / `_serialize_openai`：Anthropic 路径处理 thinking_blocks + text + tool_uses 合并到 assistant 消息的 list content，并把连续 user 中带 `<system-reminder>` 的消息合并到上一条；OpenAI 路径把 tool_use 转 `{type: "function_call", name, call_id, arguments}`、tool_result 转 `{type: "function_call_output", call_id, output}`。

## T8: Mock LLMClient 与 Agent 集成测试

- 影响文件: `tests/test_agent.py`
- 依赖任务: T4, T5, T7
- 完成标准:
 - `tests/test_agent.py:36-56` 定义 `MockLLMClient(LLMClient)`，构造时收脚本化 `responses: list[list[StreamEvent]]`，`stream` 方法逐 event yield；
 - `tests/test_agent.py:88-120` `test_single_step_tool_call` 验证 Agent 完整收到 `ToolCallComplete` 并执行；
 - `tests/test_agent.py:292-330` `test_message_splicing` 验证 `serialize("anthropic")` 出 5 条消息（env_context + user + assistant(text+2 tool_use) + user(2 tool_result) + assistant(final)），证明 thinking / tool_use 字段不丢；
 - `tests/test_agent.py:361-397` `test_token_usage_accumulates` 验证 `StreamEnd.input_tokens` / `output_tokens` 被累积到 `agent.total_input_tokens`。

## T9: 接入主流程

- 影响文件: `codebot/app.py`、`codebot/agent.py`、`codebot/tools/agent_tool.py`、`codebot/skills/executor.py`
- 依赖任务: T1-T7
- 完成标准:
 - `codebot/app.py:613-617` 在 `_select_provider` 中用 `create_client(provider)` 构造 `self.client`，捕获 `AuthenticationError` 提前提示；
 - `codebot/app.py:649-659` 把 `client=self.client` 传给 `Agent(...)`；
 - `codebot/agent.py:503-504` Agent Loop 调用 `self.client.stream(conversation, system=system, tools=tools)`，并交给 `StreamCollector.consume(stream)` 异步消费；
 - `codebot/agent.py:179-205` `StreamCollector.consume` 消费 `TextDelta` / `ThinkingDelta` / `ThinkingComplete` / `ToolCallStart` / `ToolCallDelta` / `ToolCallComplete` / `StreamEnd` 七种事件，把 `StreamText` / `ThinkingText` / `ToolUseEvent` 转发到外层 `AgentEvent` 流；
 - `codebot/agent.py:531-590` 通过 `conversation.add_assistant_message(response.text, tool_uses, thinking_blocks=conv_thinking)` 把 thinking 与 tool 写回历史，保证下一轮能回放 signature；
 - `codebot/tools/agent_tool.py:485` 在 `_select_llm` 通过 `_create_client_for_model(model_override)` 让 SubAgent 切模型；
 - `codebot/app.py:1264-1265` TUI 主循环 `try: async for event in self.agent.run(...)` 外层 `except LLMError as e: self._show_error(str(e))` 兜底所有 4 类错误。

## T10: 端到端验证

- 影响文件: 无（仅运行验证）
- 依赖任务: T9
- 完成标准:
 - `python -m compileall codebot tests` 通过；
 - `pytest tests/test_agent.py -v` 通过：14 个 Agent 集成测试全绿（含 `test_single_step_tool_call` / `test_message_splicing` / `test_token_usage_accumulates`）；
 - `ruff check codebot/client.py codebot/conversation.py` 无警告；
 - 在 TUI 中发送任意一句话（`python -m codebot`），能看到流式文本（`TextDelta`）被逐 token 渲染到对话窗口，证明 `stream()` async generator 与事件渲染端到端打通。

## 进度

- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8
- [ ] T9
- [ ] T10

```

```markdown
# ch02: 让 AI 开口说话 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性

- [ ] `LLMClient` ABC 在 `codebot/client.py:42-53` 实现，含 `@abstractmethod async def stream(conversation, system, tools) -> AsyncIterator[StreamEvent]` 和 `set_max_output_tokens(tokens)`（`grep -n "class LLMClient" codebot/client.py`）。
- [ ] `create_client` 在 `codebot/client.py:296-301` 按 `config.protocol ∈ {anthropic, openai}` 分流，未知 protocol `raise ValueError(f"Unknown protocol: {config.protocol}")`（`grep -n "create_client\|Unknown protocol" codebot/client.py`）。
- [ ] 7 个流式事件 dataclass 在 `codebot/tools/base.py:51-90` 齐全（`TextDelta` / `ToolCallStart` / `ToolCallDelta` / `ToolCallComplete` / `ThinkingDelta` / `ThinkingComplete` / `StreamEnd`），`codebot/tools/base.py:92` 定义 `StreamEvent = TextDelta | ThinkingDelta | ThinkingComplete | ToolCallStart | ToolCallDelta | ToolCallComplete | StreamEnd` Union（`grep -n "StreamEvent =" codebot/tools/base.py`）。
- [ ] 4 类错误 `LLMError` / `AuthenticationError` / `RateLimitError(retry_after)` / `NetworkError` 在 `codebot/client.py:24-40` 齐全，全部继承 `LLMError`（`grep -n "class.*Error" codebot/client.py | head -5`）。
- [ ] `_supports_adaptive_thinking` 在 `codebot/client.py:56-63` 严格按 `claude-opus-4-` / `claude-sonnet-4-` 前缀且后续首字符 `isdigit() and int(c) >= 6` 判定。
- [ ] `AnthropicClient.stream` 在 `codebot/client.py:81-174` 实现：
 - [ ] `async with self._client.messages.stream(**kwargs) as stream` 拉流（`codebot/client.py:118`）；
 - [ ] thinking adaptive 模式设 `{"type": "enabled", "budget_tokens": 0}`（`codebot/client.py:103`）；
 - [ ] thinking 回退模式设 `{"type": "enabled", "budget_tokens": max(max_output_tokens - 1, 1024)}`（`codebot/client.py:105-107`）；
 - [ ] `content_block_start` 分别识别 `thinking` / `tool_use`（`codebot/client.py:120-133`）；
 - [ ] `content_block_delta` 分别识别 `text_delta` / `thinking_delta` / `signature_delta` / `input_json_delta`（`codebot/client.py:134-146`）；
 - [ ] `content_block_stop` 时若在 thinking 中则 yield `ThinkingComplete(thinking, signature)`，若在 tool 中则 yield `ToolCallComplete(tool_id, tool_name, arguments)`（`codebot/client.py:147-164`）；
 - [ ] `await stream.get_final_message()` 取 usage / stop_reason 后 yield `StreamEnd`（`codebot/client.py:168-173`）。
- [ ] `AnthropicClient` 错误分类 `except` 链在 `codebot/client.py:176-187` 覆盖 `AuthenticationError` / `RateLimitError`（取 `e.response.headers["retry-after"]`）/ `APIConnectionError` / `APIStatusError`，全部 `raise ... from e`。
- [ ] `OpenAIClient.stream` 在 `codebot/client.py:205-278` 处理 `response.output_text.delta`、`response.function_call_arguments.delta/done`、`response.output_item.added`（function_call）、`response.completed` 五类事件。
- [ ] `OpenAIClient` 错误分类 `except` 链在 `codebot/client.py:280-293` 覆盖 4 类错误 + `raise ... from e`。
- [ ] 模型短名映射在 `codebot/tools/agent_tool.py:612-637` 内联实现，含 `{"haiku", "sonnet", "opus"}` → 具体模型 ID（`grep -n "model_map\|haiku\|sonnet" codebot/tools/agent_tool.py`）。
- [ ] `Message` dataclass 在 `codebot/conversation.py:28-34` 定义，含 `role` / `content` / `tool_uses` / `tool_results` / `thinking_blocks` 字段。
- [ ] `ConversationManager` 8 个方法（`add_user_message` / `add_assistant_message` / `add_system_reminder` / `add_tool_results_message` / `inject_environment` / `inject_long_term_memory` / `replace_history` / `get_messages`）在 `codebot/conversation.py:44-115` 齐全。
- [ ] `add_system_reminder` 在 `codebot/conversation.py:62-68` 用 f-string 包裹 `<system-reminder>\n{content}\n</system-reminder>`（`grep -n "system-reminder" codebot/conversation.py`）。
- [ ] `_serialize_anthropic` 在 `codebot/conversation.py:122-165` 合并同角色连续 user reminder 消息以维持 user/assistant 交替（`grep -n "is_reminder\|startswith" codebot/conversation.py`）。
- [ ] `_serialize_openai` 在 `codebot/conversation.py:167-189` 把 `tool_uses` 拆成顶层 `{type: "function_call", name, call_id, arguments}` 项、`tool_results` 拆成 `{type: "function_call_output", call_id, output}` 项。

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `create_client` 至少 2 个非测试调用方（`grep -rn "create_client" --include="*.py" codebot/ | grep -v test_` 命中 `codebot/app.py:616`、`codebot/tools/agent_tool.py:635`）。
- [ ] `ConversationManager()` 至少 5 个非测试调用方（`grep -rn "ConversationManager()" --include="*.py" codebot/ | grep -v test_` 命中 `codebot/app.py`、`codebot/agents/fork.py`、`codebot/skills/executor.py`、Compact 流程等）。
- [ ] `codebot/agent.py:503-504` 实际调用 `self.client.stream(conversation, system=system, tools=tools)`，证明 `LLMClient` 接到 Agent Loop（`grep -n "client.stream" codebot/agent.py`）。
- [ ] `codebot/agent.py:181-205` `StreamCollector.consume` 用 `isinstance` 消费 `TextDelta` / `ThinkingDelta` / `ThinkingComplete` / `ToolCallStart` / `ToolCallDelta` / `ToolCallComplete` / `StreamEnd` 七种事件，无未处理事件类型遗漏（`grep -n "isinstance(event" codebot/agent.py`）。
- [ ] `codebot/agent.py:531-590` 通过 `conversation.add_assistant_message(response.text, tool_uses, thinking_blocks=conv_thinking)` 把 thinking 与 tool 写回历史，保证下一轮能回放 signature。
- [ ] `_create_client_for_model` 在 `codebot/tools/agent_tool.py:485` 被 `_select_llm` 装配时使用（`grep -n "_create_client_for_model" codebot/tools/agent_tool.py`）。
- [ ] `LLMError` 在 `codebot/app.py:1264-1265` 的主流式 `try` 块 `except LLMError as e: self._show_error(str(e))` 中被消费，统一兜底（`grep -n "except LLMError" codebot/app.py`）。

## 3. 编译与测试

- [ ] `python -m compileall codebot tests` 通过。
- [ ] `pytest tests/test_agent.py -v` 通过：14 个 Agent 集成测试全绿（`pytest tests/test_agent.py::test_single_step_tool_call tests/test_agent.py::test_message_splicing tests/test_agent.py::test_token_usage_accumulates -v`）。
- [ ] `ruff check codebot/client.py codebot/conversation.py codebot/tools/base.py` 无警告。
- [ ] `mypy codebot/client.py codebot/conversation.py` 无 type error（如项目启用 mypy 时执行）。

## 4. 端到端验证

- [ ] TUI 启动后（`python -m codebot`）发送 `hello`，对话窗口逐 token 渲染流式回复——证明 `TextDelta` 通道接到 `codebot/app.py:1100-1118` 的事件渲染。
- [ ] 模型为 `claude-sonnet-4-6`（或更新）时，`config.yaml` 设 `thinking: true` 后能在对话区看到 thinking 文本流（`ThinkingDelta` → spinner / `_thinking_label` 渲染），证明 adaptive thinking 接通（`grep -n "ThinkingText" codebot/app.py`）。
- [ ] 提供故意失败的 API key 后 TUI 显示 `Invalid API key: ...`（`AuthenticationError` 路径走 `codebot/app.py:617` 与 `:1264-1265`），证明错误分类生效。
- [ ] 在 `tests/test_agent.py::test_message_splicing` 输出中能看到 `assert len(msgs) == 5` 通过，证明 `serialize("anthropic")` 把 thinking / tool_use / tool_result 字段往返保留（`pytest tests/test_agent.py::test_message_splicing -v`）。

## 5. 文档

- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`ls /Users/codemelo/codebot/docs/python/ch02/`）。

```

### Java

```markdown
# ch02: 让 AI 开口说话 Spec

## 1. 背景

Agent 落地的第一步是让上层（Agent Loop / TUI / SubAgent）能用同一套接口和 LLM 收发，不必各自面对 SSE 流、Extended Thinking 签名回传、Provider 间消息差异。本章把 LLM 通信、流式响应、Extended Thinking、Token 统计以及两层消息模型封装到 `com.codebot.llm` 与 `com.codebot.conversation`，是 ch03+ 工具循环的前置依赖。

Java 版与 Go 版的核心架构一致，差异主要在惯例：用 `sealed interface + record` 替换 Go 的 `interface + struct`，用 `BlockingQueue<StreamEvent>` 替换 Go 的 `chan StreamEvent + chan error`（Error 作为一种事件入队），用 `Thread.startVirtualThread` 替换 goroutine，用 `LlmException` 子类替换 Go 的 error 类型断言。

## 2. 目标

交付统一的 `LlmClient` 流式接口和两个内置 Provider 实现（`AnthropicClient`、`OpenAiClient` Responses API），加上 `ConversationManager` 两层消息模型（内部带 thinking / tool use / tool result 的 `Message`，序列化到具体 Provider 的请求体）。上层（Agent、TUI 装配点、AgentTool、ContextCompactor、TeamManager）拿一个 `LlmClient` 就能跑，不再触碰 SSE 细节。

## 3. 功能需求

- F1: `LlmClient` 统一暴露流式接口，输入是会话管理器和工具 schema，输出是 `BlockingQueue<StreamEvent>`，错误作为 `StreamEvent.Error` 入队。
- F2: 客户端通过接口内置静态工厂方法 `LlmClient.create(cfg, systemPrompt)` 按 Provider Protocol 路由到 Anthropic 或 OpenAI 实现，未知 protocol 抛 `IllegalArgumentException`。
- F3: 事件流覆盖 8 种信号：`TextDelta` / `ThinkingDelta` / `ThinkingComplete`（含签名）/ `ToolCallStart` / `ToolCallDelta` / `ToolCallComplete` / `StreamEnd`（含 stop reason 与 token 用量）/ `Error`。所有事件用 `sealed interface` + `record` 收口，`switch` 模式匹配时编译器保证穷尽。
- F4: Anthropic 客户端基于手写 `HttpClient` + SSE 解析，支持 Extended Thinking 两种模式：高版本模型（opus-4-6 / sonnet-4-6）走 Adaptive Thinking，低版本回退到固定 budget 的 Enabled Thinking，能力判断由 `ModelResolver.supportsAdaptiveThinking` 完成。
- F5: OpenAI 客户端基于 Responses API（非 Chat Completions），支持把 `reasoning_summary_text.delta/done` 还原成 `ThinkingDelta` / `ThinkingComplete` 事件，让上层看到的事件形状和 Anthropic 一致。
- F6: 两个客户端都通过 `HttpRequest.timeout(5min)` + `sendAsync().get(90s)` 兜底 SDK / 网络静默阻塞，HTTP 非 200 状态走错误分类后抛 `LlmException`。
- F7: 错误分类有 5 类：基类 `LlmException` 以及 4 个静态嵌套子类：`AuthenticationException`、`RateLimitException`（带 `retryAfter`）、`ContextTooLongException`、`NetworkException`。各客户端把 HTTP 错误归类到这 5 类之一，上层只面对统一异常。
- F8: `Message` 是可变类（mutable POJO），字段含 role / content / thinkingBlocks / toolUses / toolResults；`ThinkingBlock` / `ToolUseBlock` / `ToolResultBlock` 是不可变 `record`。所有写操作走 `ConversationManager` 方法，外部通过 `getMessages()` 拿到 `List.copyOf` 的只读视图。
- F9: `ConversationManager` 提供 `serialize(protocol)` 按 Protocol 序列化（`serializeAnthropic` / `serializeOpenAI`），序列化时不丢字段（thinking signature、tool arguments、tool result isError 都要原样回到下一轮请求）。
- F10: `ConversationManager.addSystemReminder(content)` 把内容包成 `<system-reminder>\n{content}\n</system-reminder>` 作为 user 消息追加，供 ch06 Plan Mode、ch08 Compact、ch09 Memory 复用。
- F11: `ModelResolver` 暴露 `ALIASES` 短名映射（haiku / sonnet / opus → 具体模型 ID）和 `resolve(model)` / `supportsAdaptiveThinking(model)` / `supportsThinking(model)` 三个静态方法，供 ch13 SubAgent 切模型。

## 4. 非功能需求

- N1: 事件队列 `LinkedBlockingQueue<StreamEvent>(64)` 有缓冲，SSE 读取与事件分发用独立虚拟线程解耦，事件写入 `queue.put()` 时不阻塞主消费者。
- N2: 调用方通过 `Thread.interrupt()` 取消（如 TUI ctrl+c）时，SSE 读循环检测到中断并清理；Agent Loop 侧用 `poll(30s, TimeUnit.SECONDS)` 兜底，超时即 `Stream timeout` 退出。
- N3: HTTP 请求设置 5 分钟超时 + 90 秒连接超时，避免任何一路静默阻塞拖死整个 agent loop。
- N4: 序列化层不丢字段（thinking signature / tool arguments / tool result isError 全部往返保留），Anthropic 把 thinking + text + tool_use 合并到同一条 assistant content 数组里。
- N5: `ConversationManager` 不加锁——单消费者模型，调用方（Agent Loop 单线程顺序追加）负责串行化；`getMessages()` 返回 `List.copyOf` 不可变视图。

## 5. 设计概要

- 核心数据结构:
 - `LlmClient`（接口 + 静态工厂方法 `create()`）
 - `StreamEvent` sealed interface + 8 个 record
 - `LlmException` 基类 + 4 个静态嵌套子类
 - `ModelResolver`（含 `ALIASES` Map 与三个静态方法）
 - `ConversationManager`（私有 `List<Message> history`）
 - `Message`（可变 POJO）+ `ThinkingBlock` / `ToolUseBlock` / `ToolResultBlock`（不可变 record）
- 主流程（每轮 LLM 请求）:
 1. `Agent.agentLoop` 调 `client.stream(conv, tools)`，拿到 `BlockingQueue<StreamEvent>`
 2. 客户端把 `ConversationManager.serialize(protocol)` 序列化成请求体，调 `HttpClient.sendAsync`
 3. 启动虚拟线程读 SSE，主线程 `queue.poll(30s)` 消费事件
 4. 按 SSE 事件类型 `queue.put()` 对应 `StreamEvent` record
 5. 流结束 put `StreamEnd`；异常经 `classifyHttpError` 分类后 put `StreamEvent.Error`
- 调用链（模块层级）:
 - TUI 装配 → `LlmClient.create(provider, systemPrompt)` → 传给 `new Agent(client, registry, protocol)`
 - Agent loop → `LlmClient.stream` → `switch (event)` 模式匹配消费 → 写回 `ConversationManager`
 - `AgentTool` / `ContextCompactor` / `TeamManager` worker / `MemoryManager` 复用同一 `LlmClient` 接口
- 与其他模块的交互:
 - 依赖 `com.codebot.config.ProviderConfig`（Provider 配置、API key、token 上限）
 - 被 `com.codebot.agent`、`com.codebot.subagent`、`com.codebot.compact`、`com.codebot.tui`、`com.codebot.teams`、`com.codebot.memory` 调用
 - 与 `com.codebot.tool` 解耦：`stream` 只接 `List<Map<String, Object>>` schema，工具注册中心由 `ToolRegistry` 提供

## 6. Out of Scope

- 多模态输入（image / PDF）的请求体构造：当前 `Message.content` 仅 `String`，未来章节再扩
- 自动重试与指数退避：rate limit 的重试在 ch04 Agent Loop 处理（`Thread.sleep(5000)`），不在 ch02 范围
- Provider 抽象细分（Bedrock / Vertex / Azure-OpenAI）：当前只支持原生 Anthropic 与原生 OpenAI Responses
- Prompt caching / Cache breakpoints：目标设计已有，本仓库暂未实现
- 官方 SDK 接入：当前手写 `HttpClient` + Jackson 解析，未来可替换为 `anthropic-java` / `openai-java` SDK

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch02: 让 AI 开口说话 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。

## T1: 定义 `LlmClient` 接口与静态工厂方法
- 影响文件: `src/main/java/com/codebot/llm/LlmClient.java`
- 依赖任务: 无
- 完成标准: `src/main/java/com/codebot/llm/LlmClient.java:10-20` 声明 `LlmClient` 接口（含 `stream(conv, tools)` 单实例方法）；`src/main/java/com/codebot/llm/LlmClient.java:14-19` 实现 `static create(ProviderConfig cfg, String systemPrompt)`，用 switch 表达式按 protocol 路由，未知 protocol 抛 `IllegalArgumentException`。

## T2: 实现流式事件 sealed interface + records
- 影响文件: `src/main/java/com/codebot/llm/StreamEvent.java`
- 依赖任务: T1
- 完成标准: `src/main/java/com/codebot/llm/StreamEvent.java:5-22` 定义 `sealed interface StreamEvent` + 8 个 record（`TextDelta` / `ThinkingDelta` / `ThinkingComplete` / `ToolCallStart` / `ToolCallDelta` / `ToolCallComplete` / `StreamEnd` / `Error`），全部用 `implements StreamEvent`。

## T3: 实现异常分层（`LlmException` + 4 个嵌套子类）
- 影响文件: `src/main/java/com/codebot/llm/LlmException.java`
- 依赖任务: T1
- 完成标准: `src/main/java/com/codebot/llm/LlmException.java:3-41` 定义 `LlmException extends RuntimeException`，含双构造函数；`:13-17` `AuthenticationException`；`:19-28` `RateLimitException`（含 `retryAfter` 字段与 getter）；`:30-34` `ContextTooLongException`；`:36-40` `NetworkException`。

## T4: 实现 Anthropic 客户端
- 影响文件: `src/main/java/com/codebot/llm/AnthropicClient.java`
- 依赖任务: T1, T2, T3, T6, T7
- 完成标准:
 - `src/main/java/com/codebot/llm/AnthropicClient.java:31-46` 构造函数读取 `cfg.resolvedApiKey()`，空时抛 `AuthenticationException`，model 经 `ModelResolver.resolve` 解析；
 - `src/main/java/com/codebot/llm/AnthropicClient.java:52-68` `stream()` 创建 `LinkedBlockingQueue<>(64)` + `Thread.startVirtualThread` 调 `doStream`；
 - `src/main/java/com/codebot/llm/AnthropicClient.java:80-86` thinking=true 时根据 `ModelResolver.supportsAdaptiveThinking` 切换 adaptive / enabled（budget = maxTokens - 1）；
 - `src/main/java/com/codebot/llm/AnthropicClient.java:132-234` SSE 主循环 `switch(eventType)` 处理 `message_start` / `content_block_start`（识别 thinking / tool_use）/ `content_block_delta`（识别 `thinking_delta` / `signature_delta` / `text_delta` / `input_json_delta`）/ `content_block_stop` / `message_delta`；
 - `src/main/java/com/codebot/llm/AnthropicClient.java:236-238` 流结束 `queue.put(new StreamEvent.StreamEnd(stopReason, inputTokens, outputTokens))`；
 - `src/main/java/com/codebot/llm/AnthropicClient.java:245-255` `classifyHttpError(status, body)` 按 413 / `prompt is too long` / 401 / 429 / default 分支返回 `LlmException` 子类。

## T5: 实现 OpenAI Responses 客户端
- 影响文件: `src/main/java/com/codebot/llm/OpenAiClient.java`
- 依赖任务: T1, T2, T3, T7
- 完成标准:
 - `src/main/java/com/codebot/llm/OpenAiClient.java:30-45` 构造函数读取 API key（空抛 `AuthenticationException`）；
 - `src/main/java/com/codebot/llm/OpenAiClient.java:51-67` `stream()` 与 Anthropic 同形；
 - `src/main/java/com/codebot/llm/OpenAiClient.java:84-86` thinking=true 时设置 `reasoning: { effort: "high", summary: "detailed" }`；
 - `src/main/java/com/codebot/llm/OpenAiClient.java:125-203` SSE 主循环 `switch(type)` 处理 `response.output_text.delta` / `response.output_item.added`（function_call / reasoning）/ `response.reasoning_summary_text.delta/done` / `response.function_call_arguments.delta/done` / `response.completed`；
 - `src/main/java/com/codebot/llm/OpenAiClient.java:211-222` `classifyHttpError` 覆盖 413 / 400+`context_length_exceeded` / 401 / 429 / default。

## T6: 实现 `ModelResolver`（短名映射 + 能力判断）
- 影响文件: `src/main/java/com/codebot/llm/ModelResolver.java`
- 依赖任务: T1
- 完成标准: `src/main/java/com/codebot/llm/ModelResolver.java:7-11` 定义 `ALIASES` Map（haiku / sonnet / opus）；`:13-15` `resolve(model)` 返回别名解析后的具体 ID；`:17-20` `supportsAdaptiveThinking(model)` 判断含 `opus-4-6` / `sonnet-4-6`；`:22-25` `supportsThinking(model)` 判断含 `claude`。

## T7: 实现 `ConversationManager` + Message + 三个 block record
- 影响文件: `src/main/java/com/codebot/conversation/ConversationManager.java`、`Message.java`、`ThinkingBlock.java`、`ToolUseBlock.java`、`ToolResultBlock.java`
- 依赖任务: 无
- 完成标准:
 - `src/main/java/com/codebot/conversation/ThinkingBlock.java:3` `record ThinkingBlock(String thinking, String signature)`；
 - `src/main/java/com/codebot/conversation/ToolUseBlock.java:5` `record ToolUseBlock(String toolUseId, String toolName, Map<String, Object> arguments)`；
 - `src/main/java/com/codebot/conversation/ToolResultBlock.java:3` `record ToolResultBlock(String toolUseId, String content, boolean isError)`；
 - `src/main/java/com/codebot/conversation/Message.java:5-32` 可变类 Message，字段 role / content / thinkingBlocks / toolUses / toolResults + 5 套 getter/setter；
 - `src/main/java/com/codebot/conversation/ConversationManager.java:17-46` 实现 6 个 add 方法（含 `addSystemReminder` 包裹 `<system-reminder>\n...\n</system-reminder>`）；
 - `src/main/java/com/codebot/conversation/ConversationManager.java:48-58` 实现 `getMessages()` 返回 `List.copyOf(history)`、`getMessagesMutable()`、`size()`；
 - `src/main/java/com/codebot/conversation/ConversationManager.java:60-174` 实现 `serialize(protocol)` 分发到 `serializeAnthropic` / `serializeOpenAI`，含同角色文本消息合并逻辑。

## T8: 覆盖 Thinking + Reasoning 行为测试
- 影响文件: `src/test/java/com/codebot/llm/ThinkingTest.java`
- 依赖任务: T4, T5, T6, T7
- 完成标准:
 - `testSupportsAdaptiveThinking` 验证 opus-4-6 / sonnet-4-6=true，opus-4-5 / sonnet-4-5=false，gpt-5=false；
 - `testAnthropicThinkingAdaptive` 断言 4.6 模型走 adaptive、`thinking.type="adaptive"`；
 - `testAnthropicThinkingEnabled` 断言非官方模型走 enabled、`budget_tokens = maxTokens - 1`；
 - `testAnthropicThinkingDisabled` 断言 `thinking=false` 时请求体无 thinking 字段；
 - `testAnthropicThinkingBlocksInConversation` 断言 thinking block 的 signature 能往返；
 - `testOpenAIReasoningEnabled` / `testOpenAIReasoningDisabled` 分别覆盖 OpenAI reasoning 开关。

## T9: 接入主流程
- 影响文件: `src/main/java/com/codebot/tui/CodeBotModel.java`、`src/main/java/com/codebot/agent/Agent.java`、`src/main/java/com/codebot/subagent/AgentTool.java`、`src/main/java/com/codebot/teams/TeammateRunner.java`
- 依赖任务: T1-T7
- 完成标准:
 - `src/main/java/com/codebot/tui/CodeBotModel.java:391` 用 `LlmClient.create(selectedProvider, systemPrompt)` 构造 client；
 - `src/main/java/com/codebot/tui/CodeBotModel.java:399` 把 client 传给 `new AgentTool(client, registry, protocol)`；
 - `src/main/java/com/codebot/agent/Agent.java:126` Agent Loop 调用 `client.stream(conv, tools)`；
 - `src/main/java/com/codebot/agent/Agent.java:150-179` `switch (event)` 模式匹配消费 8 种事件；
 - `src/main/java/com/codebot/subagent/AgentTool.java:74` `setModelResolver(Function<String, LlmClient> modelResolver)` 接入短名解析。

## T10: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T9
- 完成标准:
 - `./gradlew build` 通过；
 - `./gradlew test --tests "com.codebot.llm.*"` 通过（6+ thinking_test 全绿）；
 - 在 TUI 中发送任意一句话，能看到流式文本（`TextDelta`）被逐 token 渲染到对话窗口，证明 `BlockingQueue<StreamEvent>` 与事件渲染端到端打通。

## 进度
- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8
- [ ] T9
- [ ] T10

```

```markdown
# ch02: 让 AI 开口说话 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性

- [ ] `LlmClient` 接口在 `src/main/java/com/codebot/llm/LlmClient.java:10-20` 实现，方法签名 `BlockingQueue<StreamEvent> stream(ConversationManager conv, List<Map<String, Object>> tools)`（`grep -n 'interface LlmClient' src/main/java/com/codebot/llm/LlmClient.java`）。
- [ ] `LlmClient.create(cfg, systemPrompt)` 静态工厂方法在 `src/main/java/com/codebot/llm/LlmClient.java:14-19` 用 switch 表达式按 protocol ∈ {anthropic, openai} 路由，未知 protocol 抛 `new IllegalArgumentException("Unknown protocol: " + cfg.getProtocol())`（`grep -n 'static LlmClient create' src/main/java/com/codebot/llm/LlmClient.java`）。
- [ ] 8 个流式事件 record 在 `src/main/java/com/codebot/llm/StreamEvent.java:5-22` 齐全（TextDelta / ThinkingDelta / ThinkingComplete / ToolCallStart / ToolCallDelta / ToolCallComplete / StreamEnd / Error），全部用 `sealed interface` + `implements StreamEvent`（`grep -c 'record .* implements StreamEvent' src/main/java/com/codebot/llm/StreamEvent.java` 返回 8）。
- [ ] `LlmException` 基类 + 4 个静态嵌套子类（`AuthenticationException` / `RateLimitException{retryAfter}` / `ContextTooLongException` / `NetworkException`）在 `src/main/java/com/codebot/llm/LlmException.java:3-41` 齐全（`grep -n 'class.*Exception' src/main/java/com/codebot/llm/LlmException.java`）。
- [ ] `ModelResolver.supportsAdaptiveThinking` 在 `src/main/java/com/codebot/llm/ModelResolver.java:17-20` 严格按 `opus-4-6` / `sonnet-4-6` 子串判定（`grep -n 'supportsAdaptiveThinking' src/main/java/com/codebot/llm/ModelResolver.java`）。
- [ ] `AnthropicClient.stream` 在 `src/main/java/com/codebot/llm/AnthropicClient.java:52-68` 实现：
 - [ ] 在 `Thread.startVirtualThread` 中调 `doStream`（`grep -n 'startVirtualThread' src/main/java/com/codebot/llm/AnthropicClient.java`）；
 - [ ] 异常被 `classifyError(e)` 归类为 `LlmException` 并以 `StreamEvent.Error` 入队（`src/main/java/com/codebot/llm/AnthropicClient.java:62`）；
 - [ ] SSE 主循环在 `src/main/java/com/codebot/llm/AnthropicClient.java:135-233` 处理 `content_block_start` 识别 `thinking` / `tool_use`、`content_block_delta` 识别 `thinking_delta` / `signature_delta` / `text_delta` / `input_json_delta`；
 - [ ] StreamEnd 携带 stopReason（默认 `end_turn`）与 input/output tokens（`src/main/java/com/codebot/llm/AnthropicClient.java:236-237`）。
- [ ] `AnthropicClient` thinking=true 时根据 `ModelResolver.supportsAdaptiveThinking` 切换 adaptive / enabled（`src/main/java/com/codebot/llm/AnthropicClient.java:80-86`）。
- [ ] `classifyHttpError` 在 `src/main/java/com/codebot/llm/AnthropicClient.java:245-255` 覆盖 413 / `prompt is too long` / 401 (`AuthenticationException`) / 429 (`RateLimitException`) / default(`LlmException`)。
- [ ] `OpenAiClient.stream` 在 `src/main/java/com/codebot/llm/OpenAiClient.java:51-67` 实现，`doStream` 主循环 `src/main/java/com/codebot/llm/OpenAiClient.java:125-203` 处理 `response.output_text.delta` / `response.output_item.added`（function_call / reasoning）/ `response.reasoning_summary_text.delta/done` / `response.function_call_arguments.delta/done` / `response.completed`。
- [ ] OpenAI thinking=true 时设置 `reasoning: {effort:"high", summary:"detailed"}`（`src/main/java/com/codebot/llm/OpenAiClient.java:84-86`）。
- [ ] `OpenAiClient.classifyHttpError` 在 `src/main/java/com/codebot/llm/OpenAiClient.java:211-222` 处理 413 / 400+`context_length_exceeded` / 401 / 429 / default。
- [ ] `ModelResolver` 在 `src/main/java/com/codebot/llm/ModelResolver.java:7-11` 暴露 `ALIASES` Map（haiku → claude-haiku-4-5、sonnet → claude-sonnet-4-6、opus → claude-opus-4-6）。
- [ ] `Message` 可变类在 `src/main/java/com/codebot/conversation/Message.java:5-32` 定义，字段 `role / content / thinkingBlocks / toolUses / toolResults` + 5 套 getter/setter。
- [ ] 三个 record 块在 `src/main/java/com/codebot/conversation/ThinkingBlock.java:3`、`ToolUseBlock.java:5`、`ToolResultBlock.java:3` 定义为 record（`grep -rn '^public record' src/main/java/com/codebot/conversation/` 命中 3 处）。
- [ ] `ConversationManager` 6 个 add 方法 + `getMessages` + `serialize` 在 `src/main/java/com/codebot/conversation/ConversationManager.java:17-62` 齐全。
- [ ] `addSystemReminder` 包裹 `<system-reminder>\n{content}\n</system-reminder>`（`src/main/java/com/codebot/conversation/ConversationManager.java:44-46`）。
- [ ] `serializeAnthropic` 合并同角色连续文本消息以维持 user/assistant 交替（`src/main/java/com/codebot/conversation/ConversationManager.java:110-132`）。

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `LlmClient.create` 至少 1 个非测试调用方（`grep -rn "LlmClient.create" --include="*.java" src/main/` 命中 `src/main/java/com/codebot/tui/CodeBotModel.java:391`）。
- [ ] `new ConversationManager()` 至少 6 个非测试调用方（`grep -rn "new ConversationManager()" --include="*.java" src/main/` 命中 `tui/CodeBotModel.java:189/880/1440`、`compact/ContextCompactor.java:260/286/299/390`、`subagent/AgentTool.java:285/337`、`memory/MemoryManager.java:90`、`teams/TeamManager.java:88`）。
- [ ] `src/main/java/com/codebot/agent/Agent.java:126` 实际调用 `client.stream(conv, tools)`，证明 LlmClient 接口接到 Agent Loop（`grep -n 'client.stream' src/main/java/com/codebot/agent/Agent.java`）。
- [ ] `src/main/java/com/codebot/agent/Agent.java:150-179` 用 `switch (event)` 模式匹配消费 `TextDelta` / `ThinkingDelta` / `ThinkingComplete` / `ToolCallStart` / `ToolCallDelta` / `ToolCallComplete` / `StreamEnd` / `Error` 8 种事件，sealed interface 保证无遗漏。
- [ ] `src/main/java/com/codebot/agent/Agent.java:217/224/239` 通过 `conv.addAssistantFull(text, thinkingBlocks, toolUseBlocks)` 把 thinking 与 tool 写回历史，保证下一轮能回放 signature。
- [ ] `ModelResolver.resolve` 在 `src/main/java/com/codebot/llm/AnthropicClient.java:38` 被构造函数使用（`grep -rn "ModelResolver" --include="*.java" src/main/`）。
- [ ] `setModelResolver(Function<String, LlmClient>)` 在 `src/main/java/com/codebot/subagent/AgentTool.java:74` 提供给 SubAgent 切模型（ch13）。
- [ ] `LlmException` / `LlmException.ContextTooLongException` / `LlmException.RateLimitException` / `LlmException.NetworkException` 被 `src/main/java/com/codebot/agent/Agent.java:185-202` 的 streamError 分支按错误文本关键字消费，错误链未断。

## 3. 编译与测试

- [ ] `./gradlew build` 通过。
- [ ] `./gradlew test --tests "com.codebot.llm.*"` 通过：6+ 个 Thinking/Reasoning 测试全绿。
- [ ] `./gradlew compileJava` 无 sealed-switch 警告（编译器穷尽性检查通过）。

## 4. 端到端验证

- [ ] TUI 启动后发送 `hello`，对话窗口逐 token 渲染流式回复——证明 `StreamEvent.TextDelta` 通道接到 `src/main/java/com/codebot/tui/CodeBotModel.java` 的事件渲染。
- [ ] 模型为 `claude-sonnet-4-6`（或更新）且 `thinking: true` 时能在对话区看到 thinking 文本流（`ThinkingDelta` → tui 渲染），证明 adaptive thinking 接通。
- [ ] 提供故意失败的 API key 后 TUI 显示 `Invalid API key: ...`（`AuthenticationException` 路径），证明错误分类生效。
- [ ] 留存证据: `./gradlew test -i` 输出包含 `testAnthropicThinkingAdaptive PASSED` / `testOpenAIReasoningEnabled PASSED` 等日志行。

## 5. 文档

- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`/Users/codemelo/codebot/docs/java/ch02/`）。

```



## ch03

```markdown
# 我的初步想法
这一步的目标是：给 CodeBot 装上工具系统。用户提问后，模型不再只能动嘴，而是能读文件、写文件、改文件、执行命令、搜索代码，从聊天机器人变成真正能干活的 Agent。模型识别自己要用哪个工具，我的代码去执行，再把结果喂回去，模型据此决定下一步。

技术要求：

- 用统一的 Tool 接口，每个工具都实现它，元信息带名称、描述、参数 Schema 和执行方法
- 先做六个核心工具：读文件、写文件、改文件、执行命令、按模式找文件、搜代码内容
- 一个注册中心集中登记工具，按名查找，转成 API 认得的工具列表
- 工具执行带超时和错误处理，失败信息包成结构化结果返回给模型，让它能调整而不是崩掉
- LLM 客户端要能解析流式的工具调用（JSON 参数碎片拼接），执行完把结果回灌进对话历史
- 改文件走原文唯一匹配替换，匹配不到或匹配多次都给清楚的报错让模型重试

这一步先不做多工具的连环调用，模型拿到一次结果就停下，自动循环（Agent Loop）留到下一章。
```

### Go

```markdown
# ch03: 工具系统 Spec

## 1. 背景

ch02 让 LLM 能说话，但 LLM 只是文字流；Coding Agent 真正能在仓库里干活靠的是「函数调用」。ch03 在 `internal/tools` 落地 Function Calling 三步循环所需的全部抽象：统一的 `Tool` 接口、`Registry` 注册中心、可序列化成 Anthropic 与 OpenAI 两种协议的 schema，以及 6 个核心工具。没有这一章，ch04 Agent Loop 收到工具调用事件后无法把工具名映射到具体执行器，所有后续工具（ToolSearch、AskUserQuestion、Todo、Team 系列、SubAgent、MCP wrapper）也无处挂靠。

## 2. 目标

交付 `tools.Tool` 接口与 `tools.Registry` 注册中心，统一所有工具的 Name / Description / Category / Schema / Execute 五段式契约；交付 `CreateDefaultRegistry()` 一次性注册 6 个核心工具（ReadFile / WriteFile / EditFile / Bash / Glob / Grep）；交付协议无关的 schema 导出能力让 Anthropic / OpenAI 各取所需；额外提供 `DeferrableTool` 与 `ToolSearch` 做渐进式工具披露。给 ch04 Agent Loop、ch07 MCP、ch11 Skill `allowedTools`、ch13 SubAgent 工具过滤等下游使用。

## 3. 功能需求

- F1: 提供统一的工具返回值 `ToolResult{Output, IsError}`，所有工具结果都用同一形状回灌会话。
- F2: 提供 `ToolCategory` 枚举（read / write / command），用于权限分类与并行批次划分（read-only 工具可并发，write / command 串行）。
- F3: 定义 `Tool` 接口，声明 Name / Description / Category / Schema / Execute 五个方法，所有内置 / MCP / Skill / Team 工具实现该接口。
- F4: 提供 `DeferrableTool` 接口，允许工具声明「初次请求时不进 schema 列表，由 ToolSearch 按需取出」，供 ch07 MCP / ch15 Team 专用工具采用。
- F5: 提供 `Registry` 注册中心，支持注册、按名查找、列举所有工具。
- F6: 提供按协议导出工具 schema 的能力：默认输出 Anthropic 形状，遇到 OpenAI 协议时在边缘做形状转换；deferred 工具默认不出现在 schema 列表里。
- F7: 提供 deferred 工具的两种查询入口：按名精确选（`select:Name1,Name2`）和按关键词搜（在 name / description 中匹配），均按当前协议返回 schema。
- F8: 提供默认工厂，一次性把 6 个核心工具注册好；TUI 装配阶段直接拿到可用 Registry。
- F9: ReadFile 工具：读文本文件并按行号输出；处理文件不存在 / 路径是目录 / 起止行号越界三类边界，返回结构化错误。
- F10: WriteFile 工具：写入指定路径，目录不存在时自动创建中间目录。
- F11: EditFile 工具：基于 `old_string` 唯一匹配在文件里做一次性替换；处理「文件不存在」「未匹配」「匹配多次」三类边界。
- F12: Bash 工具：通过 shell 启动子进程，捕获 stdout / stderr / 退出码；支持超时，超时与未超时的输出区分清楚。
- F13: Glob 工具：在工作目录下递归匹配文件名 glob；跳过常见 vendored / 缓存目录子树；结果按字典序输出，无匹配时返回友好提示。
- F14: Grep 工具：按正则在文件里逐行搜，支持 `include` basename glob 过滤；遇到非法正则返回结构化错误；结果格式 `path:line:content`，无匹配时返回友好提示。
- F15: ToolSearch 工具：把 deferred 工具按 `select:` / 关键词两种形态暴露给模型；按 `max_results` 裁剪并 clamp 到安全范围，未命中时回退到列出全部 deferred 工具名。
- F16: AskUserQuestion 工具：把结构化问题（题数与选项数有上下限、支持多选）经请求通道交给 TUI 渲染，阻塞等待用户回应；ctx 取消时给出取消结果。
- F17: 工具描述常量与工具实例解耦，集中存放，便于 ch11 Skill / ch05 System Prompt 复用。
- F18: 提供单工具结果上限常量，由 ch04 Agent Loop 在回灌会话前据此截断，避免单工具撑爆下一轮上下文。

## 4. 非功能需求

- N1: Tool 接口无状态：核心工具用零值结构体即可注册，可被多个 Registry 实例（主 Agent + SubAgent）共享。
- N2: Registry 非并发安全——只允许在装配阶段写入，运行期只读；MCP / Agent / Skill 等都在 TUI 装配阶段一次性注册完毕。
- N3: 工具实现不允许依赖上层模块（agent / skills / teams），`internal/tools` 处于底层。
- N4: 工具 `Execute` 必须能响应 ctx 取消并尽快退出；长命令（Bash）依赖外部超时机制兜底。
- N5: Schema 形态稳定：以 Anthropic 形状为基础，OpenAI 形状在导出边缘做转换，避免每个 Tool 各写两份 schema。

## 5. 设计概要

- 核心数据结构: `Tool` 接口、`DeferrableTool` 接口、`ToolResult{Output, IsError}`、`ToolCategory` 枚举、`Registry{tools map[string]Tool}`、6 个核心工具结构体、`ToolSearchTool`（持有 Registry + Protocol）、`AskUserQuestionTool`（持有 RequestCh）。
- 主流程（一次工具调用从 LLM 到磁盘）:
 1. Agent Loop 收到 `ToolCallComplete`；
 2. 通过 `Registry.Get(name)` 找到工具，未知工具回灌错误结果；
 3. 走权限检查（ch06）；
 4. 走 PreToolUse hook（ch12）；
 5. 调 `tool.Execute(ctx, args)`；
 6. 走 PostToolUse hook，结果按 `MaxOutputChars` 截断后落 tool_result。
- 调用链:
 - 装配: TUI 启动 → `tools.CreateDefaultRegistry()` → 追加 AskUserQuestion / Todo / ToolSearch / Team / Agent 等工具 → MCP ready 时把 MCP 工具也注册进来。
 - Schema 导出: Agent Loop 每轮取 `Registry.GetAllSchemas(protocol)` 传给 `Client.Stream`。
 - 执行: Agent Loop 内的工具分发函数统一通过 `Registry.Get` + `tool.Execute` 调用。
 - Teammate worker（ch15）也手动注册同一批核心工具，证明 Tool 接口是跨进程通用契约。
- 与其他模块的交互:
 - 被依赖: `internal/agent`（取 schema、查工具、执行）、`internal/tui`（创建并注册）、`internal/mcp`（实现 Tool 接口包装 MCP tool）、`internal/agents`（SubAgent 工具过滤）、`internal/teams`（注册 Team 专用工具）、`internal/skills`（用 `allowedTools` 约束）、`internal/hooks`（按 ToolName 覆盖）。
 - 依赖: 仅 Go 标准库（context / os / os/exec / path/filepath / regexp / strings），不依赖任何上层模块。

## 6. Out of Scope

- 工具描述自适应（例如 Bash 描述根据当前 sandbox 模式动态生成）：当前所有描述都是静态常量。
- 文件读取的图片 / PDF / Notebook 解析：本章只支持文本 + 行号输出。
- EditFile 的 `replace_all` 选项：当前要求 `old_string` 唯一。
- Bash 危险命令静态校验：放到 ch06 权限系统。
- Bash 后台任务 / Sandbox 模式 / sed-edit 解析：不在 ch03 范围。
- 工具输出大结果存盘（spillover）：放到 ch08 `internal/compact`。
- 细化的工具元数据（isReadOnly / isConcurrencySafe / maxResultSizeChars 等）：当前用 ToolCategory + 全局 MaxOutputChars 简化表达，细化留给后续章节。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch03: 工具系统 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。

## T1: 定义 `Tool` 接口与 `ToolResult` / `ToolCategory`
- 影响文件: `internal/tools/tool.go`
- 依赖任务: 无
- 完成标准: `internal/tools/tool.go:15-34` 定义 `ToolResult`、`ToolCategory` 枚举（read/write/command）、`Tool` 接口五段方法（Name/Description/Category/Schema/Execute）。

## T2: 实现 `Registry` 与 schema 转换
- 影响文件: `internal/tools/tool.go`
- 依赖任务: T1
- 完成标准:
 - `internal/tools/tool.go:40-62` 实现 `Registry{Register, Get, ListTools, NewRegistry}`；
 - `internal/tools/tool.go:71-90` 实现 `GetAllSchemas(protocol)`，跳过 deferred，OpenAI 输出 `{type: "function", name, description, parameters}`；
 - `internal/tools/tool.go:36-38` 定义 `DeferrableTool{ShouldDefer}` 接口；
 - `internal/tools/tool.go:92-153` 实现 `GetDeferredTools` / `SearchDeferred` / `FindDeferredByNames`。

## T3: 实现 ReadFile 工具
- 影响文件: `internal/tools/read_file.go`、`internal/tools/descriptions.go`
- 依赖任务: T1
- 完成标准:
 - `internal/tools/read_file.go:10-91` 实现 `ReadFileTool` + `intArg` helper；
 - schema 包含 `file_path`（必填）、`offset`（默认 0）、`limit`（默认 2000）；
 - 输出 `<line_no>\t<content>` 1-based；
 - 文件不存在 / 不是文件 / 越界返回对应错误或空串。

## T4: 实现 WriteFile 工具
- 影响文件: `internal/tools/write_file.go`、`internal/tools/descriptions.go`
- 依赖任务: T1
- 完成标准: `internal/tools/write_file.go:10-48` 实现 `WriteFileTool`；写前 `os.MkdirAll(dir, 0o755)`；文件用 `0o644` 写入；成功输出 `Successfully wrote to <path>`。

## T5: 实现 EditFile 工具
- 影响文件: `internal/tools/edit_file.go`、`internal/tools/descriptions.go`
- 依赖任务: T1
- 完成标准: `internal/tools/edit_file.go:10-65` 实现 `EditFileTool`；唯一性校验（0 / 1 / N 三分支）；只替换首个匹配；不写文件就报错。

## T6: 实现 Bash 工具
- 影响文件: `internal/tools/bash.go`、`internal/tools/descriptions.go`
- 依赖任务: T1
- 完成标准:
 - `internal/tools/bash.go:11` 常量 `maxTimeout = 600`；
 - `internal/tools/bash.go:34-87` 实现 `Execute`：`context.WithTimeout` + `exec.CommandContext("bash", "-c", cmd)`；
 - 输出 `$ <cmd>\n<stdout>\nSTDERR: <stderr>\n(exit code N)`，超时输出 `Error: command timed out after Ns`。

## T7: 实现 Glob 工具
- 影响文件: `internal/tools/glob.go`、`internal/tools/descriptions.go`
- 依赖任务: T1
- 完成标准:
 - `internal/tools/tool.go:8-11` 定义 `SkipDirs`；
 - `internal/tools/glob.go:33-81` 实现 `Execute`：`filepath.Walk` + 跳过 `SkipDirs` + 同时尝试 basename/rel 匹配 + 字典序输出；
 - 空结果输出 `No files matched the pattern.`。

## T8: 实现 Grep 工具
- 影响文件: `internal/tools/grep.go`、`internal/tools/descriptions.go`
- 依赖任务: T1
- 完成标准: `internal/tools/grep.go:36-105` 实现 `Execute`：`regexp.Compile` + `include` glob + 逐行扫描 + `<rel>:<lineNum>:<line>` 输出 + 跳过 `SkipDirs`；空结果 `No matches found.`。

## T9: 实现 `ToolSearch` 与 deferred 工具协议
- 影响文件: `internal/tools/tool_search.go`
- 依赖任务: T2
- 完成标准:
 - `internal/tools/tool_search.go:10-47` 定义 `ToolSearchTool{Registry, Protocol}` 与 schema；
 - `:49-91` 实现 `Execute`：识别 `select:Name1,Name2` 前缀走精确查找，否则走关键词搜索；
 - 未命中时返回 `Available deferred tools: <names>`。

## T10: 实现 `AskUserQuestion` 工具
- 影响文件: `internal/tools/ask_user.go`
- 依赖任务: T1
- 完成标准:
 - `internal/tools/ask_user.go:10-37` 定义 `Question`/`QuestionOption`/`AskUserRequest{Questions, ResponseCh}`；
 - `:39-150` 实现 `Execute`：schema 强制 1-4 题、2-4 选项；通过 `RequestCh` 推消息阻塞 select 等 `ResponseCh` 或 `ctx.Done`。

## T11: 拼装 `CreateDefaultRegistry`
- 影响文件: `internal/tools/tool.go`
- 依赖任务: T3, T4, T5, T6, T7, T8
- 完成标准: `internal/tools/tool.go:155-164` 在 `CreateDefaultRegistry` 内一次性注册 6 个核心工具实例。

## T12: 接入主流程
- 影响文件: `internal/tui/tui.go`、`internal/agent/agent.go`、`cmd/codebot/teammate.go`
- 依赖任务: T11
- 完成标准:
 - `internal/tui/tui.go:212reg := tools.CreateDefaultRegistry`；
 - `internal/tui/tui.go:213` 追加 `AskUserQuestionTool{RequestCh: askCh}`；
 - `internal/tui/tui.go:540` 追加 `ToolSearchTool`；
 - `internal/agent/agent.go:101` 使用 `a.Registry.GetAllSchemas(a.Protocol)` 拿 schema；
 - `internal/agent/agent.go:348/427` 走 `Registry.Get` + `tool.Execute`；
 - `cmd/codebot/teammate.go:128-133` 在 teammate worker 中也手工注册 6 个核心工具。

## T13: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T12
- 完成标准:
 - `go build ./...` 通过；
 - `go vet ./internal/tools/...` 无警告；
 - 在 TUI 输入 `请读取 README.md 并告诉我前 5 行`，Agent 会调用 `ReadFile` 工具并返回带行号的文本；
 - 在 TUI 输入 `跑一下 ls -la`，Agent 会调用 `Bash` 工具，输出含 `$ ls -la` 与 `(exit code 0)`；
 - 留存证据: 任一后续章节（ch04-ch15）能正常工作本身就说明工具系统接通。

## 进度
- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8
- [ ] T9
- [ ] T10
- [ ] T11
- [ ] T12
- [ ] T13

```

```markdown
# ch03: 工具系统 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性

- [ ] `Tool` 接口在 `internal/tools/tool.go:28-34` 实现 5 个方法 Name/Description/Category/Schema/Execute（`grep -n 'type Tool interface' internal/tools/tool.go`）。
- [ ] `DeferrableTool` 接口在 `internal/tools/tool.go:36-38` 暴露 `ShouldDefer()`。
- [ ] `ToolResult{Output, IsError}` 在 `internal/tools/tool.go:15-18` 定义。
- [ ] `ToolCategory` 在 `internal/tools/tool.go:20-26` 提供 `CategoryRead/Write/Command` 三个常量。
- [ ] `Registry` 提供 `NewRegistry` / `Register` / `Get` / `ListTools` / `GetAllSchemas` / `GetDeferredTools` / `SearchDeferred` / `FindDeferredByNames` 八个公开方法（`grep -n 'func (r \*Registry)' internal/tools/tool.go`）。
- [ ] `GetAllSchemas` 在 protocol == "openai" 时输出 `{type: "function", name, description, parameters}` 形状（`internal/tools/tool.go:78-87`）。
- [ ] `CreateDefaultRegistry` 在 `internal/tools/tool.go:155-164` 注册 6 个核心工具（`grep -n 'reg.Register' internal/tools/tool.go`）。
- [ ] `ReadFileTool` 在 `internal/tools/read_file.go:10-75`，Name=`ReadFile`、Category=read、行号 1-based 输出。
- [ ] `WriteFileTool` 在 `internal/tools/write_file.go:10-48`，写前 `os.MkdirAll(dir, 0o755)`、文件 0o644。
- [ ] `EditFileTool` 在 `internal/tools/edit_file.go:10-65`，唯一性校验三分支（0 / 1 / N）。
- [ ] `BashTool` 在 `internal/tools/bash.go:13-87`，`maxTimeout = 600`、`exec.CommandContext("bash", "-c", cmd)`、输出含 `$ <cmd>` 头与 `(exit code N)` 尾。
- [ ] `GlobTool` 在 `internal/tools/glob.go:12-81`，跳过 `SkipDirs`、basename+rel 双重匹配、字典序输出。
- [ ] `GrepTool` 在 `internal/tools/grep.go:14-105`，`regexp.Compile` + `include` glob + `<rel>:<lineNum>:<line>` 输出。
- [ ] `ToolSearchTool` 在 `internal/tools/tool_search.go:10-91`，支持 `select:...` 前缀与关键词两种查询；`max_results` clamp 到 [1, 20]。
- [ ] `AskUserQuestionTool` 在 `internal/tools/ask_user.go:30-150`，schema 限制 1-4 题、每题 2-4 选项；通过 `RequestCh` 推消息并阻塞等待 `ResponseCh`。
- [ ] 6 段描述常量集中在 `internal/tools/descriptions.go:3-69`（BashDescription/ReadFileDescription/EditFileDescription/WriteFileDescription/GlobDescription/GrepDescription）。
- [ ] `MaxOutputChars = 10000` 在 `internal/tools/tool.go:13` 作为全局结果上限。
- [ ] `SkipDirs` 在 `internal/tools/tool.go:8-11` 列出 `.git/.venv/node_modules/__pycache__/.tox/.mypy_cache` 六项。

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `CreateDefaultRegistry` 在 `internal/tui/tui.go:212` 与 `internal/agent/agent_live_test.go` 中被调用（`grep -rn "tools.CreateDefaultRegistry" --include="*.go" /Users/codemelo/codebot` 命中 ≥ 4 处）。
- [ ] 6 个核心工具结构体在 `cmd/codebot/teammate.go:128-133` 与 `internal/agent/agent_test.go` 中被实例化（`grep -rn "tools.ReadFileTool\|tools.WriteFileTool\|tools.EditFileTool\|tools.BashTool\|tools.GlobTool\|tools.GrepTool" --include="*.go"` 命中 ≥ 10 处）。
- [ ] `ToolSearchTool` 在 `internal/tui/tui.go:540` 被注册（`grep -rn "ToolSearchTool" --include="*.go" /Users/codemelo/codebot | grep -v _test.go`）。
- [ ] `AskUserQuestionTool` 在 `internal/tui/tui.go:213` 被注册，并由 `tui.go` 的 askUser 状态机消费（`grep -n "AskUserRequest\|askUserCh" internal/tui/tui.go`）。
- [ ] `Registry.GetAllSchemas` 唯一接入点在 `internal/agent/agent.go:101`（`grep -rn "GetAllSchemas" --include="*.go" /Users/codemelo/codebot`）。
- [ ] `Registry.Get` 唯一接入点在 `internal/agent/agent.go:348`，`tool.Execute` 唯一调用点在 `internal/agent/agent.go:427`，证明工具执行接进 Agent Loop。
- [ ] `DeferrableTool` 在 `internal/tools/tool.go:65isDeferred(t)` 中被消费，由 `GetAllSchemas` / `GetDeferredTools` / `SearchDeferred` / `FindDeferredByNames` 四处使用（`grep -n 'isDeferred(' internal/tools/tool.go`）。
- [ ] `ReadFile` / `WriteFile` / `EditFile` / `Bash` / `Glob` / `Grep` 在 `internal/agent/agent.go:454-458` 中被 `partitionToolCalls` 用名字白名单决定 read-only 并发批次（仅 ReadFile/Glob/Grep 列为并发安全）。

## 3. 编译与测试

- [ ] `go build ./...` 通过。
- [ ] `go vet ./internal/tools/...` 无警告。
- [ ] `internal/tools/` 本身无单元测试文件（工具的端到端行为由 `internal/agent/agent_test.go` 覆盖）。

## 4. 端到端验证

- [ ] 在 TUI 输入 `请读取 /Users/codemelo/codebot/README.md`，Agent 调用 `ReadFile`，对话区显示带行号的内容（验证 ReadFile 接通）。
- [ ] 在 TUI 输入 `跑 ls -la /tmp`，Agent 调用 `Bash`，对话区显示 `$ ls -la /tmp` + 文件列表 + `(exit code 0)`（验证 Bash 接通）。
- [ ] 在 TUI 输入 `搜代码里所有 func main`，Agent 调用 `Grep` 并返回 `<file>:<line>:<line content>` 命中（验证 Grep 接通）。
- [ ] 在 TUI 中触发 `AskUserQuestion`（如要求 Agent 让用户选某选项），TUI 弹出问题对话框，选完答案后 Agent 继续（验证 AskUserQuestion 通过 RequestCh ↔ ResponseCh 双通道接通）。
- [ ] 留存证据: `internal/agent/agent_test.go:447`、`:615-616`、`:754-755` 这类用 `WriteFile/ReadFile/Bash` 装饰 Registry 的测试通过即说明工具能被 Agent Loop 跑起来。

## 5. 文档

- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`/Users/codemelo/codebot/specs/go/ch03/`）。
- [ ] commit 信息标注 ch03 与三件套关闭状态（与 ch01 / ch02 一起出 commit `docs(ch01-03): course-spec spec/tasks/checklist`）。

```

### Python

```markdown
# ch03: 工具系统 Spec

## 1. 背景

ch02 让 LLM 能说话，但 LLM 只是文字流；Coding Agent 真正能在仓库里干活靠的是「函数调用」。ch03 在 `codebot/tools/` 落地 Function Calling 三步循环所需的全部抽象：统一的 `Tool` ABC 基类、`ToolRegistry` 注册中心、可序列化成 Anthropic 与 OpenAI 两种协议的 schema、流式 tool_use 事件类型，以及 6 个核心工具。没有这一章，ch04 Agent Loop 收到工具调用事件后无法把工具名映射到具体执行器，所有后续工具（ToolSearch、AskUserQuestion、Skill、Worktree、Team、SubAgent、MCP wrapper）也无处挂靠。

## 2. 目标

交付 `tools.Tool` ABC 与 `tools.ToolRegistry` 注册中心，统一所有工具的 name / description / params_model / category / execute 五段式契约；交付 `create_default_registry()` 一次性注册 6 个核心工具（ReadFile / WriteFile / EditFile / Bash / Glob / Grep）；以 Pydantic `BaseModel` 直接生成 JSON Schema，省去手写两遍 schema 的负担；交付 deferred 工具协议 + `ToolSearchTool` 做渐进式工具披露。给 ch04 Agent Loop、ch07 MCP、ch11 Skill `allowedTools`、ch13 SubAgent 工具过滤、ch15 Team 等下游使用。

## 3. 功能需求

- F1: 提供统一的工具返回值 `ToolResult(output: str, is_error: bool = False)`，所有工具结果都用同一形状回灌会话。
- F2: 提供 `ToolCategory = Literal["read", "write", "command"]`，用于权限分类与并行批次划分（read-only + `is_concurrency_safe` 工具可并发，write / command 串行）。
- F3: 定义 `Tool` ABC 基类，声明 `name` / `description` / `params_model` / `category` / `execute` 字段与方法，所有内置 / MCP / Skill / Team 工具继承该类。`params_model` 用 Pydantic `BaseModel` 描述参数；`get_schema()` 直接 `model_json_schema()` 出 JSON Schema。
- F4: 提供 `should_defer: bool` 类属性，允许工具声明「初次请求时不进 schema 列表，由 ToolSearch 按需取出」，供 ch07 MCP / ch15 Team / `AskUserQuestion` 等专用工具采用。
- F5: 提供 `ToolRegistry`，支持 `register` / `get` / `list_tools` 三种基础操作，外加 `enable` / `disable` / `is_enabled` 控制启停、`mark_discovered` / `is_discovered` 跟踪 deferred 工具是否已被披露。
- F6: 提供按协议导出工具 schema 的能力：`get_all_schemas(protocol)` 默认输出 Anthropic 形状 `{name, description, input_schema}`，遇到 `protocol == "openai"` 时在边缘转成 `{type: "function", name, description, parameters}`；deferred 且未 discovered 的工具默认不出现在 schema 列表里。
- F7: 提供 deferred 工具的两种查询入口：按名精确选（`search.find_deferred_by_names`，对应 `select:Name1,Name2`）与按关键词搜（`search.search_deferred`，在 name / description 中匹配并打分）；命中后自动 `mark_discovered`。
- F8: 提供 `create_default_registry(file_cache)` 工厂，一次性把 6 个核心工具注册好；TUI 装配阶段直接拿到可用 Registry。
- F9: ReadFile 工具：读文本文件并按行号输出 `<line_no>\t<content>`（1-based）；处理文件不存在 / 路径不是文件两类边界；支持 `offset` / `limit` 切片；命中 `FileCache` 时跳过实际 IO。
- F10: WriteFile 工具：写入指定路径，目录不存在时 `path.parent.mkdir(parents=True, exist_ok=True)` 自动创建中间目录；写完后 `FileCache.invalidate`。
- F11: EditFile 工具：基于 `old_string` 唯一匹配在文件里做一次性替换；处理「文件不存在」「未匹配」「匹配多次」三类边界，命中后 `FileCache.invalidate`。
- F12: Bash 工具：通过 `asyncio.create_subprocess_shell` 启动子进程，捕获 stdout / stderr / 退出码；用 `asyncio.wait_for(timeout)` 控制超时，超时与非零退出区分清楚（is_error 区分）。
- F13: Glob 工具：用 `Path.glob(pattern)` 递归匹配文件名，结果按字典序输出相对路径；跳过 `SKIP_DIRS` 子树；无匹配时返回 `No files matched the pattern.`。
- F14: Grep 工具：`re.compile` + `include` basename glob 过滤 + 逐行扫描，输出 `<rel>:<line_num>:<line>`；遇到非法正则返回结构化错误；跳过 `SKIP_DIRS` 子树；无匹配时返回 `No matches found.`。
- F15: ToolSearch 工具：把 deferred 工具按 `select:` / 关键词两种形态暴露给模型；命中后 `mark_discovered`；未命中时回退到列出全部 deferred 工具名。
- F16: AskUserQuestion 工具（deferred）：把结构化问题经 `asyncio.Future` 交给 TUI 渲染，阻塞等待用户回应；带 5 分钟超时；TUI 通过 `_pending_event` 读取问题并 `set_result` 解阻塞。
- F17: 流式事件类型 `TextDelta` / `ToolCallStart` / `ToolCallDelta` / `ToolCallComplete` / `ThinkingDelta` / `ThinkingComplete` / `StreamEnd` 集中定义在 `codebot/tools/base.py`，便于 LLM client 与 Agent Loop 共享类型。
- F18: 提供单工具结果上限常量 `MAX_OUTPUT_CHARS = 10000`，由 ch04 / ch08 在回灌会话前据此截断或落盘，避免单工具撑爆下一轮上下文。

## 4. 非功能需求

- N1: Tool 子类无内部状态约束：核心工具大多用零参 `__init__` 即可注册；带可选注入（FileCache）的工具靠依赖注入实现可测试。
- N2: ToolRegistry 非并发安全——只允许在装配阶段写入，运行期只读；MCP / SubAgent / Skill 等都在 `App._init_after_login` 阶段一次性注册完毕。
- N3: 工具实现不允许依赖上层模块（agent / skills / teams），`codebot/tools/` 处于底层；只能用 `codebot.cache.FileCache`（typing-only）做可选注入。
- N4: 工具 `execute` 是 `async` 方法，必须能响应 `asyncio.CancelledError` 并尽快退出；长命令（Bash）依赖 `asyncio.wait_for` 超时机制兜底。
- N5: Schema 形态稳定：以 Anthropic 形状为基础（Pydantic `model_json_schema()` 直接出形），OpenAI 形状在 `ToolRegistry.get_all_schemas` 边缘做形状转换，避免每个 Tool 各写两份 schema。

## 5. 设计概要

- 核心数据结构: `Tool` ABC、`ToolResult` dataclass、`ToolCategory` Literal、`SKIP_DIRS` set、`MAX_OUTPUT_CHARS` int、`ToolRegistry` 类、6 个核心工具子类、`ToolSearchTool`（持有 Registry + Protocol）、`AskUserTool`（持 `_pending_event: AskUserEvent | None`）。
- 主流程（一次工具调用从 LLM 到磁盘）:
 1. Agent Loop 收到 `ToolCallComplete`；
 2. 通过 `ToolRegistry.get(name)` 找到工具，未知 / disabled 工具回灌结构化错误；
 3. 走权限检查（ch06）；
 4. 走 `pre_tool_use` hook（ch12）；
 5. `params = tool.params_model.model_validate(tc.arguments)` 做 Pydantic 校验；
 6. `result = await tool.execute(params)`；
 7. 走 `post_tool_use` hook，结果按 `MAX_OUTPUT_CHARS` 截断后落 tool_result。
- 调用链:
 - 装配: `App._init_after_login` → `create_default_registry(file_cache)` → 追加 `LoadSkill` / `ToolSearchTool` / `AskUserTool` / `EnterWorktreeTool` / `ExitWorktreeTool` / `AgentTool` / `team_create_tool` / `team_delete_tool`；MCP ready 时把 MCP 工具也注册进来。
 - Schema 导出: Agent Loop 每轮取 `registry.get_all_schemas(protocol)` 传给 `LLMClient.stream`。
 - 执行: Agent Loop 内 `_execute_single_tool_direct` 统一通过 `registry.get` + Pydantic 校验 + `await tool.execute` 调用。
 - 并发批次: `partition_tool_calls` 按 `tool.is_concurrency_safe` 把连续的并发安全调用归到同一批。
- 与其他模块的交互:
 - 被依赖: `codebot/agent.py`（取 schema、查工具、执行）、`codebot/app.py`（创建并注册）、`codebot/mcp/manager.py`（注册 MCP 工具 wrapper）、`codebot/agents/tool_filter.py`（SubAgent 工具过滤复制 Registry）、`codebot/skills/executor.py`（用 `allowedTools` 拷贝过滤的 Registry）、`codebot/hooks/`（按 ToolName 覆盖）。
 - 依赖: 仅 Python 标准库（asyncio / pathlib / re）+ Pydantic（用于 params_model 出 JSON Schema），不依赖任何上层模块。

## 6. Out of Scope

- 工具描述自适应（例如 Bash 描述根据当前 sandbox 模式动态生成）：当前所有描述都是类属性常量。
- 文件读取的图片 / PDF / Notebook 解析：本章只支持文本 + 行号输出。
- EditFile 的 `replace_all` 选项：当前要求 `old_string` 唯一。
- Bash 危险命令静态校验：放到 ch06 权限系统。
- Bash 后台任务 / Sandbox 模式 / sed-edit 解析：不在 ch03 范围。
- 工具输出大结果存盘（spillover）：放到 ch08 `codebot/context/`。
- 细化的工具元数据（isReadOnly / isDestructive / maxResultSizeChars 等）：当前用 `ToolCategory` + `is_concurrency_safe` + 全局 `MAX_OUTPUT_CHARS` 简化表达，细化留给后续章节。
- 协议层的 `cache_control` / `prompt caching`：放到 ch04 / ch08。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch03: 工具系统 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。

## T1: 定义 `Tool` ABC 与 `ToolResult` / `ToolCategory`
- 影响文件: `codebot/tools/base.py`
- 依赖任务: 无
- 完成标准:
 - `codebot/tools/base.py:9` 定义 `SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".tox", ".mypy_cache"}`；
 - `codebot/tools/base.py:11` 定义 `MAX_OUTPUT_CHARS = 10000`；
 - `codebot/tools/base.py:13` 定义 `ToolCategory = Literal["read", "write", "command"]`；
 - `codebot/tools/base.py:16-19` 定义 `@dataclass ToolResult(output: str, is_error: bool = False)`；
 - `codebot/tools/base.py:22-45` 定义 `Tool(ABC)`：类属性 `name`/`description`/`params_model`/`category`/`is_concurrency_safe`/`is_system_tool`/`should_defer`，`is_read_only` property、`get_schema()` 方法、`@abstractmethod async def execute()`。

## T2: 定义流式事件类型
- 影响文件: `codebot/tools/base.py`
- 依赖任务: T1
- 完成标准: `codebot/tools/base.py:50-92` 定义 7 个 dataclass：`TextDelta` / `ToolCallStart` / `ToolCallDelta` / `ToolCallComplete` / `ThinkingDelta` / `ThinkingComplete` / `StreamEnd`，以及 `StreamEvent` Union 别名。`ToolCallComplete` 必含 `tool_id` / `tool_name` / `arguments: dict[str, Any]`。

## T3: 实现 `ToolRegistry` 与 schema 转换
- 影响文件: `codebot/tools/__init__.py`
- 依赖任务: T1
- 完成标准:
 - `codebot/tools/__init__.py:11-39` 实现 `ToolRegistry.__init__` + `register` / `get` / `is_enabled` / `enable` / `disable` / `enable_all` / `mark_discovered` / `is_discovered`；
 - `codebot/tools/__init__.py:41-48` 实现 `get_deferred_tool_names`：返回 `should_defer=True` 且未 discovered 且未 disabled 的工具名；
 - `codebot/tools/__init__.py:50-79` 实现 `search_deferred(query, max_results, protocol)`：在 name / description 中按词打分（`name in name_lower` +10，`name in desc_lower` +5，分词 +3 / +1），按分数倒序裁剪 max_results；
 - `codebot/tools/__init__.py:81-101` 实现 `find_deferred_by_names(names, protocol)`：仅返回 deferred 工具的 schema；
 - `codebot/tools/__init__.py:103-104` 实现 `list_tools`；
 - `codebot/tools/__init__.py:106-123` 实现 `get_all_schemas(protocol)`：跳过 disabled 与未 discovered 的 deferred，protocol=="openai" 时输出 `{type: "function", name, description, parameters}`。

## T4: 实现 ReadFile 工具
- 影响文件: `codebot/tools/read_file.py`
- 依赖任务: T1
- 完成标准:
 - `codebot/tools/read_file.py:14-17` 定义 `Params(file_path, offset=0, limit=2000)`；
 - `codebot/tools/read_file.py:20-51` 实现 `ReadFile`：`name="ReadFile"`、`category="read"`、`is_concurrency_safe=True`；处理文件不存在 / 不是文件两类错误；`offset` / `limit` 切片后输出 `f"{i + offset + 1}\t{line}"`；如注入了 `FileCache` 走缓存。

## T5: 实现 WriteFile 工具
- 影响文件: `codebot/tools/write_file.py`
- 依赖任务: T1
- 完成标准:
 - `codebot/tools/write_file.py:14-16` 定义 `Params(file_path, content)`；
 - `codebot/tools/write_file.py:19-37` 实现 `WriteFile`：`category="write"`；写前 `path.parent.mkdir(parents=True, exist_ok=True)`；写入后 `FileCache.invalidate`；成功输出 `Successfully wrote to <path>`。

## T6: 实现 EditFile 工具
- 影响文件: `codebot/tools/edit_file.py`
- 依赖任务: T1
- 完成标准:
 - `codebot/tools/edit_file.py:14-17` 定义 `Params(file_path, old_string, new_string)`；
 - `codebot/tools/edit_file.py:20-56` 实现 `EditFile`：`category="write"`；唯一性校验三分支：`count == 0` → `old_string not found`，`count > 1` → `found N times, must be unique`，`count == 1` → `content.replace(..., 1)` 写回；命中 `FileCache.invalidate`。

## T7: 实现 Bash 工具
- 影响文件: `codebot/tools/bash.py`
- 依赖任务: T1
- 完成标准:
 - `codebot/tools/bash.py:9` 定义 `MAX_TIMEOUT = 600`；
 - `codebot/tools/bash.py:12-14` 定义 `Params(command, timeout=120)`；
 - `codebot/tools/bash.py:17-49` 实现 `Bash`：`category="command"`；`asyncio.create_subprocess_shell` + `asyncio.wait_for(timeout=min(params.timeout, MAX_TIMEOUT))`；输出含 `STDOUT:` / `STDERR:` 两段或 `(no output)`；超时输出 `Error: command timed out after Ns`；`is_error = (returncode != 0)`。

## T8: 实现 Glob 工具
- 影响文件: `codebot/tools/glob.py`
- 依赖任务: T1
- 完成标准:
 - `codebot/tools/glob.py:10-12` 定义 `Params(pattern, path=".")`；
 - `codebot/tools/glob.py:15-38` 实现 `Glob`：`is_concurrency_safe=True`；`base.glob(params.pattern)` + 过滤 `SKIP_DIRS` + 仅文件 + 字典序输出相对路径；空结果输出 `No files matched the pattern.`。

## T9: 实现 Grep 工具
- 影响文件: `codebot/tools/grep.py`
- 依赖任务: T1
- 完成标准:
 - `codebot/tools/grep.py:11-14` 定义 `Params(pattern, path=".", include="")`；
 - `codebot/tools/grep.py:17-55` 实现 `Grep`：`is_concurrency_safe=True`；`re.compile` 捕获 `re.error`；`include` 拼成 `**/<include>` glob；逐行 `regex.search` 后输出 `f"{rel}:{line_num}:{line}"`；跳过 `SKIP_DIRS` 与无法读取的文件；空结果输出 `No matches found.`。

## T10: 实现 ToolSearch 工具与 deferred 协议
- 影响文件: `codebot/tools/impl/__init__.py`、`codebot/tools/impl/tool_search.py`
- 依赖任务: T3
- 完成标准:
 - `codebot/tools/impl/tool_search.py:14-16` 定义 `ToolSearchParams(query, max_results=5)`；
 - `codebot/tools/impl/tool_search.py:19-46` 定义 `ToolSearchTool`：持有 `registry` / `protocol`；自定义 `get_schema()` 以 strip title；`should_defer = False`（自身从不 defer）；
 - `codebot/tools/impl/tool_search.py:48-80` 实现 `execute`：`select:` 前缀走 `find_deferred_by_names`，否则走 `search_deferred`；未命中返回 `No matching deferred tools for "<q>". Available: <names>`；命中后逐个 `registry.mark_discovered(s["name"])`，输出 `Found N tool(s)...` + JSON 序列化的 schema。

## T11: 实现 AskUserQuestion 工具
- 影响文件: `codebot/tools/ask_user.py`
- 依赖任务: T1
- 完成标准:
 - `codebot/tools/ask_user.py:11-18` 定义 `QuestionItem(type, name, message, options)`；
 - `codebot/tools/ask_user.py:21-24` 定义 `AskUserParams(questions: list[QuestionItem])`；
 - `codebot/tools/ask_user.py:27-34` 定义 `AskUserEvent(questions, future)`；
 - `codebot/tools/ask_user.py:37-75` 实现 `AskUserTool`：`should_defer = True`、`is_system_tool = True`；`execute` 创建 `asyncio.Future`、写 `self._pending_event`、`asyncio.wait_for(future, timeout=300)`；超时返回 `User did not respond within 5 minutes`；最终输出 `{q.name}: {answer}` 多行。

## T12: 拼装 `create_default_registry`
- 影响文件: `codebot/tools/__init__.py`
- 依赖任务: T4, T5, T6, T7, T8, T9
- 完成标准: `codebot/tools/__init__.py:126-144` 实现 `create_default_registry(file_cache=None) -> ToolRegistry`：在函数体内 lazy import 6 个工具类，逐个 `registry.register(...)`，ReadFile / WriteFile / EditFile 传入 `file_cache`。

## T13: 接入主流程
- 影响文件: `codebot/app.py`、`codebot/agent.py`
- 依赖任务: T10, T11, T12
- 完成标准:
 - `codebot/app.py:77` `from codebot.tools import ToolRegistry, create_default_registry`；
 - `codebot/app.py:535` `self.registry: ToolRegistry = create_default_registry(file_cache=self.file_cache)`；
 - `codebot/app.py:644-645` `self.registry.register(ToolSearchTool(self.registry, protocol=provider.protocol))`；
 - `codebot/app.py:647` `self.registry.register(AskUserTool())`；
 - `codebot/agent.py:33` `from codebot.tools import ToolRegistry`；
 - `codebot/agent.py:218-232` `partition_tool_calls` 用 `tool.is_concurrency_safe` 分批；
 - `codebot/agent.py:500` `tools = self.registry.get_all_schemas(self.protocol)` 取 schema；
 - `codebot/agent.py:491` `deferred_names = self.registry.get_deferred_tool_names()` 拼 system reminder；
 - `codebot/agent.py:745` `tool = self.registry.get(tc.tool_name)`；
 - `codebot/agent.py:767` `params = tool.params_model.model_validate(tc.arguments)` + `result = await tool.execute(params)`。

## T14: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T13
- 完成标准:
 - `python -m compileall codebot` 通过；
 - `ruff check codebot/tools/` 无报错；
 - `pytest tests/test_tool_search.py -q` 全部通过；
 - `pytest tests/test_agent.py::test_single_step_tool_call tests/test_agent.py::test_multi_step_autonomous -q` 通过（用 `create_default_registry()` + MockLLMClient 跑 ReadFile/WriteFile 端到端）；
 - 在 TUI 输入 `请读取 README.md 并告诉我前 5 行`，Agent 会调用 `ReadFile`，对话区返回带行号的文本（如 `1\t# CodeBot`）；
 - 在 TUI 输入 `跑一下 ls -la`，Agent 会调用 `Bash`，对话区输出含 `STDOUT:` 段；
 - 留存证据: 任一后续章节（ch04-ch15）能正常工作本身就说明工具系统接通。

## 进度
- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8
- [ ] T9
- [ ] T10
- [ ] T11
- [ ] T12
- [ ] T13
- [ ] T14

```

```markdown
# ch03: 工具系统 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性

- [ ] `Tool` ABC 在 `codebot/tools/base.py:22-45` 定义 `name`/`description`/`params_model`/`category`/`is_concurrency_safe`/`is_system_tool`/`should_defer` 七个类属性以及 `is_read_only` property、`get_schema()` 方法、`@abstractmethod async def execute()`（`git show origin/python:codebot/tools/base.py | grep -n 'class Tool(ABC)'`）。
- [ ] `ToolResult` 在 `codebot/tools/base.py:16-19` 以 `@dataclass` 定义 `output: str` + `is_error: bool = False`。
- [ ] `ToolCategory = Literal["read", "write", "command"]` 在 `codebot/tools/base.py:13`（`grep -n 'ToolCategory' codebot/tools/base.py`）。
- [ ] `SKIP_DIRS` 在 `codebot/tools/base.py:9` 列出 `.git/.venv/node_modules/__pycache__/.tox/.mypy_cache` 六项（`grep -n 'SKIP_DIRS' codebot/tools/base.py`）。
- [ ] `MAX_OUTPUT_CHARS = 10000` 在 `codebot/tools/base.py:11` 作为全局结果上限。
- [ ] 流式事件 `TextDelta` / `ToolCallStart` / `ToolCallDelta` / `ToolCallComplete` / `ThinkingDelta` / `ThinkingComplete` / `StreamEnd` 与 `StreamEvent` Union 集中在 `codebot/tools/base.py:50-92`（`grep -c '^@dataclass' codebot/tools/base.py` ≥ 8）。
- [ ] `ToolRegistry` 在 `codebot/tools/__init__.py:11-123` 提供 `register` / `get` / `is_enabled` / `enable` / `disable` / `enable_all` / `mark_discovered` / `is_discovered` / `get_deferred_tool_names` / `search_deferred` / `find_deferred_by_names` / `list_tools` / `get_all_schemas` 共 13 个公开方法（`grep -nE 'def (register|get|is_enabled|enable|disable|enable_all|mark_discovered|is_discovered|get_deferred_tool_names|search_deferred|find_deferred_by_names|list_tools|get_all_schemas)' codebot/tools/__init__.py`）。
- [ ] `get_all_schemas` 在 protocol == "openai" 时输出 `{type: "function", name, description, parameters}` 形状（`codebot/tools/__init__.py:113-122`）。
- [ ] `create_default_registry` 在 `codebot/tools/__init__.py:126-144` 一次性注册 6 个核心工具（`git show origin/python:codebot/tools/__init__.py | grep -c 'registry.register'` == 6）。
- [ ] `ReadFile` 在 `codebot/tools/read_file.py:20-51`，`name="ReadFile"`、`category="read"`、`is_concurrency_safe=True`、`offset` 默认 0、`limit` 默认 2000、行号 1-based `<line_no>\t<content>` 输出。
- [ ] `WriteFile` 在 `codebot/tools/write_file.py:19-37`，`category="write"`、写前 `path.parent.mkdir(parents=True, exist_ok=True)`、成功输出 `Successfully wrote to <path>`。
- [ ] `EditFile` 在 `codebot/tools/edit_file.py:20-56`，唯一性校验三分支 `count == 0 / 1 / N`，N>1 时报 `found N times, must be unique`。
- [ ] `Bash` 在 `codebot/tools/bash.py:17-49`，`MAX_TIMEOUT = 600`、`asyncio.create_subprocess_shell` + `asyncio.wait_for(timeout)`、输出含 `STDOUT:` / `STDERR:` 段、超时返回 `Error: command timed out after Ns`、`is_error = (returncode != 0)`。
- [ ] `Glob` 在 `codebot/tools/glob.py:15-38`，跳过 `SKIP_DIRS`、字典序输出相对路径、无匹配返回 `No files matched the pattern.`。
- [ ] `Grep` 在 `codebot/tools/grep.py:17-55`，`re.compile` + `include` glob + 跳过 `SKIP_DIRS` + `<rel>:<line_num>:<line>` 输出、无匹配返回 `No matches found.`。
- [ ] `ToolSearchTool` 在 `codebot/tools/impl/tool_search.py:19-83`，支持 `select:` 前缀与关键词两种查询；命中后逐个 `registry.mark_discovered`；未命中返回 `No matching deferred tools for "..."`。
- [ ] `AskUserTool` 在 `codebot/tools/ask_user.py:37-75`，`should_defer = True`、`is_system_tool = True`、用 `asyncio.Future` 阻塞、`asyncio.wait_for(timeout=300)` 兜底；超时返回 `User did not respond within 5 minutes`。
- [ ] Tool 的 `params_model` 全用 Pydantic `BaseModel`，`get_schema()` 通过 `params_model.model_json_schema()` 自动出 JSON Schema（`grep -n 'model_json_schema' codebot/tools/base.py codebot/tools/impl/tool_search.py`）。

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `create_default_registry` 在 `codebot/app.py:535` 与 `tests/test_agent.py:103,143,173,201,239` 中被调用（`git grep -n 'create_default_registry' origin/python -- 'codebot/**' 'tests/**'` 命中 ≥ 6 处）。
- [ ] `ToolSearchTool` 在 `codebot/app.py:644-645` 被注册并消费 `provider.protocol`（`git grep -n 'ToolSearchTool(' origin/python -- 'codebot/**'`）。
- [ ] `AskUserTool` 在 `codebot/app.py:647` 被注册，并由 `app.py:1163` 的 `_pending_event` 状态机消费（`git grep -n 'AskUserTool\|_pending_event' origin/python -- 'codebot/app.py'`）。
- [ ] `ToolRegistry.get_all_schemas` 接入点在 `codebot/agent.py:500` 与 `codebot/agent.py:940`（`git grep -n 'get_all_schemas' origin/python -- 'codebot/agent.py'`）。
- [ ] `ToolRegistry.get` 接入点在 `codebot/agent.py:745` 与 `codebot/agent.py:791`，`tool.execute` 调用点在 `codebot/agent.py:767`，证明工具执行接进 Agent Loop。
- [ ] `get_deferred_tool_names` 在 `codebot/agent.py:491` 与 `codebot/agent.py:972` 被消费用于拼 system reminder（`git grep -n 'get_deferred_tool_names' origin/python -- 'codebot/**'`）。
- [ ] `partition_tool_calls` 在 `codebot/agent.py:218-232` 用 `tool.is_concurrency_safe` 把 ReadFile / Glob / Grep 等只读工具并发分批（`git show origin/python:codebot/agent.py | sed -n '218,232p'`）。
- [ ] `ToolRegistry` 被 `codebot/mcp/manager.py:22`、`codebot/agents/tool_filter.py:121,178,189`、`codebot/skills/executor.py:31` 等下游模块用作工具容器（`git grep -n 'ToolRegistry()' origin/python -- 'codebot/**'`）。
- [ ] `StreamEvent` / `ToolCallComplete` / `TextDelta` 等流式事件类型被 `codebot/agent.py:33-` 与 `codebot/client.py` 共享（`git grep -n 'from codebot.tools.base import' origin/python -- 'codebot/**'`）。

## 3. 编译与测试

- [ ] `python -m compileall codebot` 通过，无 SyntaxError。
- [ ] `ruff check codebot/tools/` 无报错。
- [ ] `pytest tests/test_tool_search.py -q` 通过（`git show origin/python:tests/test_tool_search.py | grep -c '^def test_\|^async def test_'` ≥ 6 个测试用例）。
- [ ] `pytest tests/test_agent.py -q` 通过（其中 `test_single_step_tool_call` / `test_multi_step_autonomous` 用 `create_default_registry()` 验证 ReadFile / WriteFile 接通）。

## 4. 端到端验证

- [ ] 在 TUI 输入 `请读取 /Users/codemelo/codebot/README.md`，Agent 调用 `ReadFile`，对话区显示带行号的内容如 `1\t# CodeBot`（验证 ReadFile 接通）。
- [ ] 在 TUI 输入 `跑 ls -la /tmp`，Agent 调用 `Bash`，对话区显示 `STDOUT:` + 文件列表（验证 Bash 接通）。
- [ ] 在 TUI 输入 `搜代码里所有 async def execute`，Agent 调用 `Grep` 并返回 `<file>:<line>:<line content>` 命中（验证 Grep 接通）。
- [ ] 在 TUI 中触发 `AskUserQuestion`（如要求 Agent 让用户选某选项），TUI 弹出问题对话框，选完答案后 Agent 继续（验证 AskUserTool 通过 `_pending_event` + `asyncio.Future` 接通）。
- [ ] 留存证据: `tests/test_agent.py::test_single_step_tool_call`（line 88-118）、`::test_multi_step_autonomous`（line 122-160）这类用 `create_default_registry()` 装配 + `ReadFile/WriteFile` 端到端的测试通过即说明工具能被 Agent Loop 跑起来。

## 5. 文档

- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`/Users/codemelo/codebot/docs/python/ch03/`）。
- [ ] commit 信息标注 ch03 与三件套关闭状态（如 `docs(python/ch01-03): course spec/tasks/checklist`）。

```

### Java

```markdown
# ch03: 工具系统 Spec

## 1. 背景

LLM 本身只会说话，要让 CodeBot 真正能读代码、改代码、跑命令，必须给它一组「手」——也就是工具。第 2 章拿到了能调用工具的 LLM 客户端，但只要工具系统不到位，模型每次回的 `tool_use` 都会卡在协议层无人执行，整个 Agent 循环（ch04）也无从挂载。本章用 Java 21 的 `interface` + `record` + `sealed enum` 把工具的统一契约、注册表、执行结果与六个核心实现一次性落地，给后续章节提供「工具能力」这一切入点。

## 2. 目标

对外提供 `com.codebot.tool` 包：调用方拿到一个 `ToolRegistry`，可以 `register` 任意 `Tool` 实现，按 protocol（`anthropic` / `openai`）拉取 schema 喂给 LLM；模型回 `tool_use` 时通过 `registry.get(name)` 拿到具体工具，`tool.execute(args)` 返回 `ToolResult`。所有工具实现 `Tool` 接口，按 `ToolCategory` 标记并发安全等级（`READ` 可并行，`WRITE`/`COMMAND` 串行）。`ToolRegistry.createDefault()` 一次注入 6 个最小可用工具：ReadFile / WriteFile / EditFile / Bash / Glob / Grep；可选注入 ToolSearch（Deferred 工具发现）与 AskUserQuestion（结构化问卷）。

## 3. 功能需求

- F1: `Tool` 接口暴露 `name() / description() / category() / schema() / execute(args)` 五个核心方法，外加 `shouldDefer()` 默认返回 `false`，让 Deferred 工具有标准开关。
- F2: `ToolCategory` 枚举提供 `READ` / `WRITE` / `COMMAND` 三档，让上层执行器据此决定串并行边界。
- F3: `ToolResult` 是 `record(String output, boolean isError)`，并暴露 `success(output)` / `error(message)` 两个静态工厂。
- F4: `ToolRegistry` 用 `LinkedHashMap<String, Tool>` 保证注册顺序，`register` / `get` / `listTools` / `getAllSchemas(protocol)` 四件套覆盖增、查、列、序列化。
- F5: `getAllSchemas(protocol)` 在 `protocol == "openai"` 时把 Anthropic 风格的 `{name, description, input_schema}` 转译为 `{type:"function", name, description, parameters}`，其他 protocol 直接透传。
- F6: Deferred 工具机制：`shouldDefer()=true` 的工具默认不出现在 `getAllSchemas` 里；`markDiscovered(name)` 标记后下一轮会被纳入；`getDeferredToolNames()` 列出未发现的 Deferred 工具供 system reminder 使用。
- F7: Deferred 检索：`searchDeferred(query, maxResults, protocol)` 大小写无关地匹配 name / description；`findDeferredByNames(names, protocol)` 按精确名拉取，二者均按 protocol 输出 schema。
- F8: 六个核心工具实现：
  - ReadFile：按 `offset`/`limit`（默认 0 / 2000 行）读文件，输出 `行号\t内容` 格式，目录或不存在直接报错。
  - WriteFile：写入文件、自动创建父目录、POSIX 文件系统下设 `rwxr-xr-x` / `rw-r--r--`。
  - EditFile：要求 `old_string` 在文件中恰好出现一次，否则报错；要求文件已存在。
  - Bash：`bash -c <command>`，可选 `timeout`（默认 120 秒，硬上限 600 秒），独立读 stdout / stderr，超时强制 `destroyForcibly`，最终输出包含 `$ command` / stdout / `STDERR:` / `(exit code N)`。
  - Glob：`PathMatcher("glob:" + pattern)` 递归遍历，自动跳过 `.git/.venv/node_modules/__pycache__/.tox/.mypy_cache`，结果按字典序排序。
  - Grep：`Pattern.compile` 编译正则；支持 `include` 文件名过滤；二进制文件检测（前 512 字节含 `\0` 即跳过）；命中后输出 `相对路径:行号:行内容`，并按 `ToolRegistry.MAX_OUTPUT_CHARS=10000` 做硬截断。
- F9: `ToolSearchTool`：自身不 Deferred（始终可用），`query="select:Name1,Name2"` 走 `findDeferredByNames`，否则走 `searchDeferred`；命中后用 Jackson 序列化 schema 并对每条 `markDiscovered`。
- F10: `AskUserTool`：标记为 Deferred；通过外部 `setEventQueue` 注入 `BlockingQueue<AgentEvent>`，执行时构造 `AskUserRequestEvent` 入队，`CompletableFuture.get(5, MINUTES)` 阻塞等用户响应，超时或用户拒绝（answer 含 `_declined`）返回错误结果。

## 4. 非功能需求

- N1: 工具输出统一硬上限 `ToolRegistry.MAX_OUTPUT_CHARS = 10_000`，由调用方（`StreamingExecutor`）在 `execute()` 之后做单层截断并追加 `... (truncated)`。
- N2: schema 中所有 Map 用 `Map.of` 或 `LinkedHashMap` 保证 key 稳定顺序；`required` 字段一律用 `List.of`。
- N3: Bash 工具子进程读流并发执行（stdout/stderr 同时读取），避免 pipe 满导致死锁；中断时 `Thread.currentThread().interrupt()` 复位中断标志。
- N4: 文件类工具（Read / Write / Edit）默认按 `Files.readString` / `Files.writeString` 走平台默认字符集（UTF-8），不引入额外参数。
- N5: Glob / Grep 必须明确 SKIP_DIRS 集合一致（同一个 6 项常量），避免大目录扫描爆炸。
- N6: AskUserTool 的 `CompletableFuture` 5 分钟超时是安全兜底，超时默认拒绝；这是「宁可让 Agent 失败也不让线程永久挂起」的策略。

## 5. 设计概要

- 核心类型
  - `Tool`（interface）：唯一抽象，所有工具实现它。
  - `ToolCategory`（enum）：`READ` / `WRITE` / `COMMAND` 三态，决定并发归类。
  - `ToolResult`（record）：`{output, isError}`，工厂方法 `success` / `error`。
  - `ToolRegistry`（class）：以 `LinkedHashMap` 保序，附带 `Set<String> discoveredTools` 跟踪 Deferred 发现状态。
- 主流程（一次工具调用）
  1. 模型返回 `tool_use(name, args)`；
  2. Agent 主循环把 `(name, args)` 交给执行器；
  3. 执行器 `registry.get(name)` 拿到 `Tool`，按 `category()` 决定并行/串行；
  4. 调用 `tool.execute(args)` 得 `ToolResult`；
  5. 若 `output.length() > MAX_OUTPUT_CHARS` 截断；
  6. 把 `ToolResultEvent` 推回事件队列，结果以 `ToolResultBlock` 回灌对话。
- Deferred 工具流程
  1. 工具实现返回 `shouldDefer()=true`；
  2. `getAllSchemas` 默认跳过未发现项；
  3. Agent 把 `getDeferredToolNames()` 注入 system reminder；
  4. 模型主动调 `ToolSearch`，`markDiscovered` 后下一轮 `getAllSchemas` 才会暴露这些 schema。
- 与其他模块的交互
  - 被 `com.codebot.agent.Agent` / `com.codebot.agent.StreamingExecutor` 调用（ch03）。
  - 被 `com.codebot.tui.CodeBotModel.startChat` 创建并注册扩展工具（`AgentTool` / `TaskTools` / `TeamTools` / `EnterWorktreeTool` / `ExitWorktreeTool`）。
  - `AskUserTool.setEventQueue` 与 `CodeBotModel` 的 AgentEvent 队列双向通信。

## 6. Out of Scope

- 本章不实现权限审核（属 ch06，`PermissionChecker`）；执行器自带的权限分支由 `StreamingExecutor` 在 ch03 接入。
- 本章不实现 Pre/Post Hook 拦截（属 hook 模块 ch12，由 ch04 Agent Loop 包夹）。
- 本章不实现工具的并行调度策略（READ 并行 / WRITE 串行）；`ToolCategory` 只是标签，调度由 `StreamingExecutor` 负责。
- 本章不实现 MCP 工具桥接（属 ch07）；只保证 `ToolRegistry.register` 是 MCP 工具的注入点。
- 本章不实现 Subagent / Worktree 工具（属 ch13 / ch14）；只保留注册接口。
- TodoList / Team 工具的接入由 ch11 / ch15 负责。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch03: 工具系统 Tasks

> 任务粒度：每个任务可在一次会话内完成，可独立交付。本章为验收，所有任务已经在 `origin/java` 分支落地，逐项标注真实文件 / 行号。

## T1: 定义 `Tool` 接口
- 影响文件：`src/main/java/com/codebot/tool/Tool.java:5-20`
- 依赖任务：无
- 完成标准：`public interface Tool` 暴露 `name() / description() / category() / schema() / execute(Map)` 五个抽象方法，`shouldDefer()` 默认实现返回 `false`（Tool.java:17-19）。

## T2: 定义 `ToolCategory` 枚举与 `ToolResult` record
- 影响文件：`src/main/java/com/codebot/tool/ToolCategory.java:3-5`、`src/main/java/com/codebot/tool/ToolResult.java:3-12`
- 依赖任务：无
- 完成标准：`ToolCategory` 枚举包含 `READ`/`WRITE`/`COMMAND` 三个常量；`ToolResult` 是 `record(String output, boolean isError)`，提供 `success(output)`（ToolResult.java:5-7）和 `error(message)`（ToolResult.java:9-11）两个静态工厂。

## T3: 实现 `ToolRegistry` 核心增/查/列/序列化
- 影响文件：`src/main/java/com/codebot/tool/ToolRegistry.java:5-56`
- 依赖任务：T1, T2
- 完成标准：常量 `MAX_OUTPUT_CHARS = 10_000`（ToolRegistry.java:7）；底层 `LinkedHashMap<String, Tool>`（ToolRegistry.java:9）保序；`register` / `get` / `listTools`（ToolRegistry.java:27-37）齐全；`getAllSchemas(protocol)`（ToolRegistry.java:39-56）在 `protocol == "openai"` 时把 `{name, description, input_schema}` 转译为 `{type:"function", name, description, parameters}`，其它 protocol 原样透传。

## T4: 实现 Deferred 工具机制
- 影响文件：`src/main/java/com/codebot/tool/ToolRegistry.java:10-25`、`:58-109`
- 依赖任务：T3
- 完成标准：`Set<String> discoveredTools`（ToolRegistry.java:10）跟踪发现状态；`markDiscovered` / `isDiscovered`（ToolRegistry.java:12-18）暴露读写；`getDeferredToolNames`（ToolRegistry.java:20-25）只返回 `shouldDefer() && !discovered` 的工具；`getAllSchemas` 在 ToolRegistry.java:42 跳过未发现的 Deferred；`searchDeferred`（ToolRegistry.java:64-86）做大小写无关匹配并按 protocol 输出；`findDeferredByNames`（ToolRegistry.java:88-109）按精确名拉取。

## T5: 实现 `ToolRegistry.createDefault` 工厂
- 影响文件：`src/main/java/com/codebot/tool/ToolRegistry.java:111-120`
- 依赖任务：T3, T8~T13
- 完成标准：`createDefault()` 一次性注入 `ReadFileTool` / `WriteFileTool` / `EditFileTool` / `BashTool` / `GlobTool` / `GrepTool`（ToolRegistry.java:113-118），按文件类→命令类→搜索类的顺序，保证后续 `getAllSchemas` 输出稳定。

## T6: 实现 `ReadFileTool`
- 影响文件：`src/main/java/com/codebot/tool/impl/ReadFileTool.java`
- 依赖任务：T1, T2
- 完成标准：`name()="ReadFile"`、`category()=READ`；schema 必填 `file_path`，可选 `offset`（默认 0）/ `limit`（默认 2000）；执行时校验文件存在 + 非目录（ReadFileTool.java:70-75），按 `split("\n", -1)` 切片后输出 `行号\t内容`（ReadFileTool.java:95-101）。

## T7: 实现 `WriteFileTool` 与 `EditFileTool`
- 影响文件：`src/main/java/com/codebot/tool/impl/WriteFileTool.java`、`src/main/java/com/codebot/tool/impl/EditFileTool.java`
- 依赖任务：T1, T2
- 完成标准：WriteFile `category()=WRITE`，自动创建父目录，POSIX 文件系统下设 `rwxr-xr-x`（目录）/ `rw-r--r--`（文件）（WriteFileTool.java:69-90）；EditFile 必填 `file_path` / `old_string` / `new_string`，要求文件存在（EditFileTool.java:70-72），`countOccurrences` 必须返回 1，否则按 0 / >1 返回不同错误文案（EditFileTool.java:81-87）。

## T8: 实现 `BashTool`
- 影响文件：`src/main/java/com/codebot/tool/impl/BashTool.java`
- 依赖任务：T1, T2
- 完成标准：常量 `MAX_TIMEOUT=600`（BashTool.java:15）；`category()=COMMAND`；用 `ProcessBuilder("bash","-c", command)` 启动（BashTool.java:85），stdout/stderr 分别读取（BashTool.java:92-97），超时 `process.destroyForcibly()`（BashTool.java:99-103）；输出格式 `$ command\n<stdout>\nSTDERR: <stderr>\n(exit code N)`（BashTool.java:107-121）；非零 exit code 返回 `isError=true`。

## T9: 实现 `GlobTool`
- 影响文件：`src/main/java/com/codebot/tool/impl/GlobTool.java`
- 依赖任务：T1, T2
- 完成标准：`SKIP_DIRS` 包含 `.git/.venv/node_modules/__pycache__/.tox/.mypy_cache`（GlobTool.java:18-20）；`PathMatcher matcher = FileSystems.getDefault().getPathMatcher("glob:" + pattern)`（GlobTool.java:78）；`Files.walkFileTree` 跳过 SKIP_DIRS（GlobTool.java:84-88）；`matcher.matches(file.getFileName()) || matcher.matches(rel)` 双重判定（GlobTool.java:94）；结果 `Collections.sort` 后输出。

## T10: 实现 `GrepTool`
- 影响文件：`src/main/java/com/codebot/tool/impl/GrepTool.java`
- 依赖任务：T1, T2, T3
- 完成标准：`Pattern.compile(pattern)` 捕获 `PatternSyntaxException`（GrepTool.java:83-88）；`include` 走 `PathMatcher("glob:" + include)` 过滤（GrepTool.java:90-92）；二进制检测 `isBinaryFile` 读前 512 字节检查 `\0`（GrepTool.java:164-180）；匹配输出 `相对路径:行号:行内容`（GrepTool.java:140-141）；累计输出长度超 `ToolRegistry.MAX_OUTPUT_CHARS` 时截断并追加 `... output truncated`（GrepTool.java:143-146）。

## T11: 实现 `ToolSearchTool`
- 影响文件：`src/main/java/com/codebot/tool/impl/ToolSearchTool.java`
- 依赖任务：T4
- 完成标准：`shouldDefer()` 显式返回 `false`（ToolSearchTool.java:60-62）；构造接收 `ToolRegistry` + 可选 protocol（ToolSearchTool.java:35-42）；`max_results` 默认 5，上下夹紧到 `[1, 20]`（ToolSearchTool.java:94-100）；`query.startsWith("select:")` 走 `findDeferredByNames`，否则走 `searchDeferred`（ToolSearchTool.java:104-111）；命中后用 Jackson `ObjectMapper` 序列化 schema 并对每条调 `registry.markDiscovered(name)`（ToolSearchTool.java:124-130）。

## T12: 实现 `AskUserTool`
- 影响文件：`src/main/java/com/codebot/tool/impl/AskUserTool.java`
- 依赖任务：T1, T2
- 完成标准：`shouldDefer()=true`（AskUserTool.java:51-53）；`setEventQueue(BlockingQueue<AgentEvent>)` 由 TUI 注入（AskUserTool.java:31-33）；schema 描述 1~4 个 Question，每个 2~4 个 Option（AskUserTool.java:69-88）；执行时构造 `AskUserRequestEvent` 入队（AskUserTool.java:135），`future.get(5, TimeUnit.MINUTES)` 阻塞等响应（AskUserTool.java:143）；`answers.containsKey("_declined")` 返回错误结果（AskUserTool.java:148-150）。

## T13: 接入主流程（TUI）
- 影响文件：`src/main/java/com/codebot/tui/CodeBotModel.java:394-421`
- 依赖任务：T5, T11, T12
- 完成标准：`registry = ToolRegistry.createDefault()`（CodeBotModel.java:394）；随后注册 `ToolSearchTool(registry, protocol)`（:396）、`AskUserTool`（:397-398）、`AgentTool`（:399-403）、`EnterWorktreeTool` / `ExitWorktreeTool`（:409-410）、`TaskTools.*` 四件套（:418-421）、`TeamTools.*` 三件套（:425-427）；`AgentEvent` 队列由 model 持有并通过 `askUserTool.setEventQueue` 注入。

## T14: 端到端验证
- 影响文件：无（仅运行验证）
- 依赖任务：T1~T13
- 完成标准：
  - `./gradlew build` 通过（顶层命令）。
  - `./gradlew test --tests "com.codebot.tool.ToolSearchTest"` 通过：`testDeferredNotInSchemas` / `testToolSearchMarksDiscovered` / `testDiscoveredInSchemas` / `testGetDeferredToolNames`（ToolSearchTest.java:67/80/97/111）。
  - TUI 启动后让模型调一次 `ReadFile`，能在屏幕上看到 `ToolResultEvent` 渲染；让模型调一次 `Bash`（如 `pwd`），能看到 `(exit code 0)` 收尾。

## 进度
- [ ] T1 Tool 接口
- [ ] T2 ToolCategory / ToolResult
- [ ] T3 ToolRegistry 核心
- [ ] T4 Deferred 机制
- [ ] T5 createDefault 工厂
- [ ] T6 ReadFileTool
- [ ] T7 WriteFileTool + EditFileTool
- [ ] T8 BashTool
- [ ] T9 GlobTool
- [ ] T10 GrepTool
- [ ] T11 ToolSearchTool
- [ ] T12 AskUserTool
- [ ] T13 TUI 接入
- [ ] T14 端到端验证（build 通过 + ToolSearchTest 通过 + TUI 工具调用链确认）

```

```markdown
# ch03: 工具系统 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性
- [ ] 接口 `Tool` 在 `src/main/java/com/codebot/tool/Tool.java:5-20` 定义，包含 `name() / description() / category() / schema() / execute(Map)` 五个抽象方法 + `shouldDefer()` 默认实现（`grep -n "interface Tool" src/main/java/com/codebot/tool/Tool.java`）
- [ ] 枚举 `ToolCategory` 在 `src/main/java/com/codebot/tool/ToolCategory.java:3-5`，包含 `READ` / `WRITE` / `COMMAND` 三个常量（`grep -n "READ, WRITE, COMMAND" src/main/java/com/codebot/tool/ToolCategory.java`）
- [ ] `ToolResult` 在 `src/main/java/com/codebot/tool/ToolResult.java:3` 是 `record(String output, boolean isError)`；`success` / `error` 静态工厂在 :5-11（`grep -n "public record ToolResult" src/main/java/com/codebot/tool/ToolResult.java`）
- [ ] `ToolRegistry` 常量 `MAX_OUTPUT_CHARS = 10_000` 在 `src/main/java/com/codebot/tool/ToolRegistry.java:7`（`grep -n "MAX_OUTPUT_CHARS" src/main/java/com/codebot/tool/ToolRegistry.java`）
- [ ] `ToolRegistry.getAllSchemas` 在 `:39-56` 实现 protocol 分流（`anthropic` 透传 / `openai` 转译为 `type:"function"`）
- [ ] Deferred 机制：`discoveredTools` / `markDiscovered` / `getDeferredToolNames` 在 ToolRegistry.java:10-25；`searchDeferred` 在 :64-86；`findDeferredByNames` 在 :88-109
- [ ] `ToolRegistry.createDefault` 在 :111-120 注入 6 个核心工具（ReadFile / WriteFile / EditFile / Bash / Glob / Grep），顺序与上述一致
- [ ] `BashTool` 常量 `MAX_TIMEOUT=600` 在 `src/main/java/com/codebot/tool/impl/BashTool.java:15`；输出格式 `$ command` + stdout + `STDERR:` + `(exit code N)` 在 :107-121
- [ ] `ReadFileTool` 输出格式 `行号\t内容`（ReadFileTool.java:100）；默认 limit=2000 / offset=0（:50-51）
- [ ] `EditFileTool.countOccurrences` 在 EditFileTool.java:100-111；唯一性校验返回 0 / >1 不同错误（:81-87）
- [ ] `WriteFileTool` POSIX 文件系统下设 `rwxr-xr-x` / `rw-r--r--`（WriteFileTool.java:69-90）
- [ ] `GlobTool.SKIP_DIRS` 包含六项 `.git/.venv/node_modules/__pycache__/.tox/.mypy_cache`（GlobTool.java:18-20）；`GrepTool.SKIP_DIRS` 同样六项（GrepTool.java:19-21）
- [ ] `GrepTool.isBinaryFile` 读前 512 字节检查 `\0`（GrepTool.java:164-180）；累计输出超 `MAX_OUTPUT_CHARS` 截断（:143-146）
- [ ] `ToolSearchTool.shouldDefer()` 显式返回 `false`（ToolSearchTool.java:60-62）；`max_results` 夹紧到 `[1, 20]`（:94-100）；命中后 `markDiscovered`（:124-130）
- [ ] `AskUserTool.shouldDefer()` 返回 `true`（AskUserTool.java:51-53）；`future.get(5, TimeUnit.MINUTES)` 兜底超时（:143）；`_declined` 走错误结果（:148-150）

## 2. 接入完整性（必查，杜绝死代码）
- [ ] `grep -rn "ToolRegistry.createDefault" src/main/java` 在 TUI 至少 1 个调用方（`src/main/java/com/codebot/tui/CodeBotModel.java:394`）
- [ ] `grep -rn "new ToolSearchTool" src/main/java` 在 TUI 调用方注册（`CodeBotModel.java:396`）
- [ ] `grep -rn "new AskUserTool" src/main/java` 在 TUI 调用方注册并 `setEventQueue`（`CodeBotModel.java:397-398`）
- [ ] `registry.getAllSchemas(protocol)` 在 Agent 主循环引用（`src/main/java/com/codebot/agent/Agent.java:117`）
- [ ] `registry.get(call.toolName())` 在 `StreamingExecutor.executeSingle` 调用（`src/main/java/com/codebot/agent/StreamingExecutor.java:75`）
- [ ] `ToolRegistry.MAX_OUTPUT_CHARS` 在 `StreamingExecutor.java:135` 用于结果截断
- [ ] `getDeferredToolNames` 在 `Agent.java:94` 注入 system reminder

## 3. 编译与测试
- [ ] `./gradlew build` 通过（顶层命令）
- [ ] `./gradlew test --tests "com.codebot.tool.ToolSearchTest"` 全部通过：`testDeferredNotInSchemas`（ToolSearchTest.java:68） / `testToolSearchMarksDiscovered`（:81） / `testDiscoveredInSchemas`（:98） / `testGetDeferredToolNames`（:112）
- [ ] `./gradlew check` 无新警告

## 4. 端到端验证
- [ ] TUI 入口：启动后让模型调用 `ReadFile`，屏幕上看到工具调用 + 文件内容（带行号）渲染 —— 调用链 `CodeBotModel → Agent.run → StreamingExecutor.executeSingle → ReadFileTool.execute`
- [ ] Bash 调用：让模型跑 `pwd`，看到 `$ pwd` + 当前工作目录 + `(exit code 0)` 三段输出（BashTool.java:107-121）
- [ ] Deferred 工具：默认工具列表不含 `AskUserQuestion`；让模型调 `ToolSearch(query="AskUser")` 后下一轮模型可调用 `AskUserQuestion`
- [ ] 留存证据：验收阶段无截图；如需补，可在 TUI 中让模型执行 `ReadFile docs/java/ch03/spec.md` 拍照保存

## 5. 文档
- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`docs/java/ch03/`）
- [ ] commit 信息标注 `ch03` 与三件套关闭状态（待统一打包提交）

```





## ch04

```markdown
# 我的初步想法
- 循环本体用 ReAct 范式：一轮 = 调 LLM → 拿到响应 → 有工具调用就执行 → 结果回填 → 下一轮；没有工具调用就结束。
- 对外用事件流（channel）暴露过程：用户消息、模型 thinking、模型文本、工具调用开始、工具结果、最终回复、错误都作为事件吐出，让上层（TUI / CLI）按需消费。
- 状态机思维：每轮结束判断"继续 / 终止"，终止情形包括模型显式 end_turn、无工具调用、达到最大轮数上限、用户取消。
- 工具分批执行：一轮响应里如果模型同时要调多个工具，按读类（安全）/ 写类（互斥）分组，读类可并发、写类串行。
- 只规划不执行的模式：用一个开关切进 plan-only 状态，进入后只允许读类工具，写类工具拦截并提示用户去掉开关；最终输出一份计划交还用户审批。
- 取消与超时：循环要能响应外部 cancel（context 一类），中途打断不能让状态错乱。

# 明确不做（留给后续章节）
- 复杂的系统提示词组装，本章用最小可用 system prompt 跑起来即可。
- 完整的权限策略，本章只在工具执行前后留拦截位，不实现具体规则。
- 把 Agent 当工具递归调用（子任务委派）。
- 其他后续章节能力一律不做。
```

### Go

```markdown
# ch04: Agent Loop Spec

## 1. 背景

LLM 单次回复不能完成完整软件任务，必须把「调模型 → 拿工具调用 → 跑工具 → 把结果回灌」组成 ReAct 循环反复跑，直到模型不再请求工具。没有这层 Agent Loop，工具系统（ch03）和后续所有模块（ch05~ch15）都没有挂载点；流式 token、思考块、token 配额、用户中断、Plan Mode、HITL 权限请求都只能停留在工具层无法上浮到 UI。本章把这条循环、配套事件流和 Plan Mode 状态机做出来。

## 2. 目标

对外提供 `agent.Agent`：调用者构造好 LLM 客户端、Tool Registry、（可选）Permission Checker / Hooks / NotificationFn 后，调一次 `Run(ctx, conv)` 拿到一个 AgentEvent 通道；TUI 只负责把事件 fan-out 到屏幕，剩余的工具分发、流式拼接、回卷、Plan Mode reminder 注入、max_tokens 恢复全部由 Agent 在后台串好。Plan Mode 通过 Permission Checker 的模式字段切换；Plan 文件存档由 `internal/planfile` 承担；TodoList 工具由 `internal/todo` 提供并由 TUI 注册到 Registry。

## 3. 功能需求

- F1: `Agent.Run(ctx, conv)` 启动后台 goroutine 跑 ReAct 循环并返回事件 channel；循环退出后 channel 关闭。
- F2: 每一轮迭代先调上下文管理（ch08 两层压缩），再按需注入 Plan Mode reminder 和 NotificationFn 上报的提醒。
- F3: 通过 LLM 客户端流式拉取事件，把文本与思考流转成对应的 `StreamText` / `ThinkingText`，把工具调用三段（start / delta / complete）转成 `ToolUseEvent`。
- F4: 工具调用一边流式接收一边并行执行；本轮结束时收齐所有结果，按工具结果上限截断后回灌会话。
- F5: 主循环终止条件：本轮没有工具调用 → 写入 assistant 消息并发 `LoopComplete`；连续多次未知工具调用 → `ErrorEvent` 退出；ctx 取消 → 直接退出；超过 `MaxIterations`（若设置）→ `ErrorEvent` 退出。
- F6: 处理 `stop_reason == "max_tokens"`：首次升档放宽 max_tokens 上限，并在有限轮数内尝试恢复指令；超出预算仍未完成则错误退出。
- F7: 处理 stream 错误分流：`ContextTooLongError` → 调用强制压缩后重试；`RateLimitError` → 解析 retry-after 后 sleep 重试；其它错误 → `ErrorEvent` 退出。
- F8: 权限交互：Checker 返回 Deny 时给工具一个错误结果；返回 Ask 时发 `PermissionRequestEvent` 走 HITL，收到「Allow Always」时把工具规则 append 到本地规则文件。
- F9: 工具执行包夹 hooks：执行前走 `EventPreToolUse`（可阻断），执行后走 `EventPostToolUse`（不阻断）；从工具参数里提取代表性路径供 hook 的 glob 匹配。
- F10: 提供 `ToolNameFilter` 在每轮取 schema 时按 allowlist 过滤，支持 Coordinator Mode 动态切换可用工具集。
- F11: Plan Mode 文件状态：提供 plan slug 生成 + 单例 path + 存读 + Reset + Exists 查询，配合 TUI 的 `/plan` / `/do` / `ExitPlanMode` 流程维护当前 Plan 文件。
- F12: Todo 子系统：提供任务模型与 JSON 持久化 Store，外加四个标准工具（Create / Get / List / Update），由 TUI 在选好 session 后注册到 Registry。

## 4. 非功能需求

- N1: 工具并发安全：只读工具可并发执行，写 / 命令类工具串行执行，并发与顺序边界由专门的分区函数明确。
- N2: 事件 channel 有缓冲，避免短瞬突发事件阻塞产生 goroutine。
- N3: 工具结果回灌前按工具模块给出的上限截断并追加截断提示，防止单工具结果撑爆下一轮上下文。
- N4: 工具参数中的代表性路径提取顺序按常见 schema 字段优先（`file_path` / `path` / `pattern` / `target` 等），覆盖六个核心工具。
- N5: Plan Mode 的 slug 必须有可读形式（不能用纯 timestamp），便于人眼区分 Plan 文件。

## 5. 设计概要

- 核心数据结构:
 - `Agent`: Client / Registry / Protocol / WorkDir / MaxIterations / ContextWindow / Checker / Hooks / NotificationFn / ToolNameFilter / 压缩状态等字段。
 - `AgentEvent` sum type: StreamText / ThinkingText / ToolUseEvent / ToolResultEvent / TurnComplete / LoopComplete / UsageEvent / ErrorEvent / CompactEvent / RetryEvent / PermissionRequestEvent / AskUserQuestionEvent。
 - `StreamingExecutor`: 把流式产出的工具调用立刻起 goroutine 执行，主循环统一收齐结果。
 - `Task` / `TaskStatus` / `TaskList` / `Store`（todo 模块）：单文件 JSON 持久化，内含隐藏任务标记。
 - `planfile` 包级单例：当前进程内的 Plan 文件路径。
- 主流程（一次迭代）:
 1. 计入 iteration、检查 MaxIterations / ctx；
 2. 调上下文管理走两层压缩；
 3. ModePlan 时插入 Plan Mode reminder；
 4. 拉 NotificationFn 上报的提醒；
 5. 取 schemas（按 ToolNameFilter 过滤）；
 6. 调 `Client.Stream`，把增量 token / 思考 / 工具调用转 AgentEvent，工具调用即提交给 StreamingExecutor；
 7. 累计 token usage，处理 max_tokens 升档 / 恢复；
 8. 没有工具调用 → 落 assistant 消息 + `LoopComplete` 退出；
 9. 有工具调用 → 落 assistant 消息、收齐 tool 结果、截断后落 tool_result、发 `TurnComplete` 进入下一轮。
- 调用链:
 - 用户输入 → TUI 调 `Agent.Run` → 事件回灌 TUI 的事件处理函数。
 - `/plan` 命令 → TUI 把 Checker 切到 Plan Mode + 设置 PlanFilePath → 下一轮 Agent.Run 注入 reminder。
 - 工具执行 → `Agent.executeSingleTool` 调 `Checker.Check` → Ask 时回灌 `PermissionRequestEvent`，TUI 渲染选项并回应。
 - Todo 工具 → TUI 注册到 Registry → Agent 在工具循环中通过普通 Tool 接口调用。
- 与其他模块的交互:
 - 依赖 `internal/conversation`、`internal/llm`、`internal/tools`、`internal/compact`、`internal/permissions`、`internal/hooks`、`internal/prompt`（Plan Mode reminder）。
 - 被 `internal/tui`、`internal/agents`（SubAgent）、`internal/teams` 调用。

## 6. Out of Scope

- 本章不实现 SubAgent / Fork（属 ch13）；`Run` 只跑一个 Agent。
- 本章不实现 Worktree 隔离（属 ch14）；Plan 文件直接落本进程 cwd。
- Plan Mode 的 5-Phase Workflow / Reentry / Exit Reminder 文本已抄过来，但只有「进入 Plan → 写 plan → 退出 Plan」主路径必须通；Reentry / Exit reminder 的 TUI 接入留给下章或专门 PR。
- TodoList 的 Owner / Blocks / BlockedBy 字段已有数据模型，但不要求 UI 渲染依赖图。
- 除 max_tokens 以外的其他 stop_reason（pause_turn / refusal）不处理。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch04: Agent Loop Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。本章为验收，所有任务已经在仓库里落地，逐项标注真实文件 / 函数 / 行号。

## T1: 定义 AgentEvent 事件家族
- 影响文件: `internal/agent/events.go`（已新建）
- 依赖任务: 无
- 完成标准: `events.go` 定义 `AgentEvent` 接口，`StreamText` / `ThinkingText` / `ToolUseEvent` / `ToolResultEvent` / `TurnComplete` / `LoopComplete` / `UsageEvent` / `ErrorEvent` / `CompactEvent` / `RetryEvent` / `PermissionRequestEvent` / `AskUserQuestionEvent`（agent.events.go:7-62）皆实现 `agentEvent`。`PermissionResponse` 三态常量 `PermAllow` / `PermDeny` / `PermAllowAlways` 在 events.go:33。

## T2: 实现 `Agent` 类型与 `New` 构造
- 影响文件: `internal/agent/agent.go:29-59`
- 依赖任务: T1
- 完成标准: `Agent` 拥有 `Client`/`Registry`/`Protocol`/`WorkDir`/`MaxIterations`/`ContextWindow`/`Checker`/`Hooks`/`NotificationFn`/`ToolNameFilter`/`compactTracking` 字段；`New(client, registry, protocol)` 给出默认 `MaxIterations=0` / `ContextWindow=200000` / `WorkDir=os.Getwd()`。

## T3: 实现 Run 主循环（ReAct）
- 影响文件: `internal/agent/agent.go:61-248`
- 依赖任务: T1, T2
- 完成标准: `Run` 返回 buffer 32 的 `<-chan AgentEvent`；后台 goroutine 用 `for iteration := 1; ; iteration++` 跑循环，结束时 `defer close(ch)`；每轮先 `compact.ManageContext`，再处理 Plan Mode reminder / Notification 注入；通过 `Client.Stream` 拿事件后扇出工具调用并并发执行；`stop_reason="max_tokens"` 走升档 + 多轮恢复（常量 `maxTokensCeiling=64000` / `maxOutputTokensRecoveries=3`，agent.go:24-27）；连续 3 次未知工具 → `ErrorEvent`+退出；无工具调用 → `LoopComplete`+退出。

## T4: 实现 `StreamingExecutor` 并发工具调度
- 影响文件: `internal/agent/streaming_executor.go`
- 依赖任务: T2
- 完成标准: `StreamingExecutor` 拥有 `registry`/`checker`/`eventCh`/`mu`/`pending`/`wg`；`Submit` 即提交即 goroutine 执行；`CollectResultswg.Wait` 后按 submit 顺序收集；提供 `HasPending`/`Reset` 给 SubAgent / Teams 复用。

## T5: 实现单工具执行 + 权限 + Hook 包夹
- 影响文件: `internal/agent/agent.go:300-446`
- 依赖任务: T2, T4
- 完成标准: `executeSingleTool`（agent.go:347）拿不到工具→ `isUnknown=true`；`Checker.Check` 拿 `Deny` 走错误结果，拿 `Ask` 通过 `PermissionRequestEvent` 走 HITL，拿 `PermAllowAlways` 调 `RuleEngine.AppendLocalRule` 把 `ToolName(content*)` 持久化；hook 调用 `EventPreToolUse`（可阻断）+ `EventPostToolUse`（不阻断），从参数提取 `extractFilePath`（agent.go:338，优先级 `file_path → path → pattern → target`）。

## T6: 实现 stream 错误恢复
- 影响文件: `internal/agent/agent.go:264-298`
- 依赖任务: T3
- 完成标准: `handleStreamError` 对 `*llm.ContextTooLongError` 调 `compact.ForceCompact` 后返回 true 重试；对 `*llm.RateLimitError` 调 `parseRetryAfter(rlErr.RetryAfter)` 后 sleep 重试；其他错误返回 false 让上层发 `ErrorEvent`。`parseRetryAfter` 解析整数秒；默认 5 秒。

## T7: 实现 `ToolNameFilter` schema 过滤
- 影响文件: `internal/agent/agent.go:101-104`、`agent.go:253-262`
- 依赖任务: T3
- 完成标准: 主循环每轮取 `Registry.GetAllSchemas` 后用 `filterSchemasByName` 跑一遍 allow 函数；`Agent.ToolNameFilter` 字段允许 Coordinator Mode 动态启停而不重启 Agent；`TestFilterSchemasByName` 覆盖（agent_test.go:897/917）。

## T8: 实现 Plan Mode reminder 单元
- 影响文件: `internal/prompt/plan_mode.go`
- 依赖任务: 无（独立模块）
- 完成标准: 完整 reminder 抄自目标实现（plan_mode.go:5-61，5 阶段 Workflow 完整保留）；稀疏 reminder（plan_mode.go:63）；`BuildPlanModeReminder(planFilePath, planExists, iteration)`：iteration==1 给完整版，否则按 `reminderInterval=5` 周期重发完整版，间隔时给稀疏版。`BuildPlanModeReentryReminder` / `BuildPlanModeExitReminder` 已抄但目前 TUI 未调用（记录为未来增强）。

## T9: 实现 planfile 存档单例
- 影响文件: `internal/planfile/planfile.go`
- 依赖任务: 无
- 完成标准: `PlansDir=".codebot/plans"`；`generateSlug` 用 adjective+noun+`MMDD-HHMM` 生成可读 slug（planfile.go:19-34）；`GetOrCreatePlanPath` 单例懒加载；`GetPlanFilePath` / `ResetPlanPath` / `PlanExists` / `LoadPlan` / `SavePlan` 在 TUI `/plan/doExitPlanMode` 流程间维护进程内单例。`SetPlanFilePath` / `IsPlanFilePath` 已实现但当前无调用方（记录为预留 API）。

## T10: 实现 Todo 数据层与四工具
- 影响文件: `internal/todo/todo.go`、`internal/todo/store.go`、`internal/todo/tools.go`
- 依赖任务: 无
- 完成标准: `Task` 含 `ID`/`Subject`/`Description`/`ActiveForm`/`Status`/`Owner`/`Blocks`/`BlockedBy`/`Metadata`（todo.go:17-27）；`TaskList.Create/Get/List/Update` 加锁；`status="deleted"` 直接物理删除（todo.go:122-134）；`List` 跳过 `metadata._internal=true` 的项；`Store` 用 `.codebot/tasks/<listID>.json` 保存。四个 Tool 实现 `Name/Category/Description/Schema/Execute` 接口。

## T11: 接入主流程（TUI）
- 影响文件: `internal/tui/tui.go:360-376` / `:722-738`（构造 Agent 与 Checker）、`:536-539`（注册 Todo 工具）、`:1197-1232`（`/plan/do`）、`:1907/1941`（启动 `Run`）、`:2021`（事件分发）
- 依赖任务: T2~T10
- 完成标准: 用户进入聊天后 TUI 调 `agent.New` 并装好 `Checker`/`Hooks`/`NotificationFn`/`ToolNameFilter`；发送消息时 `m.agentCh = m.ag.Run(ctx, m.conversation)`；事件分发函数把每个 AgentEvent 转成 TUI 渲染指令；`/plan` 切换 `Checker.Mode=ModePlan` 并设置 `PlanFilePath`，`/do` 恢复模式 + `ResetPlanPath`。

## T12: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T11
- 完成标准:
 - `go build ./...` 通过（已验证）。
 - `go test ./internal/agent/...` 关键单测通过：`TestAgentSimpleResponse`、`TestAgentToolCallLoop`、`TestAgentMaxIterations`、`TestAgentWithThinking`、`TestMultiRoundConversation`、`TestFilterSchemasByName`、`TestFilterSchemasByNameEmptyInput`（agent_test.go:155 起）。
 - 在 TUI 输入 `hello` 看到流式文本与 `LoopComplete` 终止；输入 `/plan` 看到 plan reminder 注入并禁止写工具。

## 进度
- [ ] T1 events.go 已实现
- [ ] T2 Agent 类型 + New
- [ ] T3 Run 主循环
- [ ] T4 StreamingExecutor
- [ ] T5 executeSingleTool + 权限 + Hook
- [ ] T6 handleStreamError
- [ ] T7 ToolNameFilter
- [ ] T8 plan_mode.go reminder
- [ ] T9 planfile.go 单例
- [ ] T10 todo 模块
- [ ] T11 TUI 接入
- [ ] T12 端到端验证（编译通过 + agent_test 单测通过 + TUI Run 调用链确认）

```

```markdown
# ch04: Agent Loop Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性
- [ ] 类型 `Agent` 在 `internal/agent/agent.go:29-47` 实现，字段包含 `Client`/`Registry`/`Protocol`/`WorkDir`/`MaxIterations`/`ContextWindow`/`Checker`/`Hooks`/`NotificationFn`/`ToolNameFilter`/`compactTracking`/`eventCh`（`grep -n "type Agent struct" internal/agent/agent.go`）
- [ ] 接口 `AgentEvent` + 12 个具体事件类型在 `internal/agent/events.go:5-62`，全部实现 `agentEvent()` 标记方法（`grep -n "agentEvent()" internal/agent/events.go` 至少返回 12 条）
- [ ] 函数 `Agent.Run` 在 `internal/agent/agent.go:61` 实现，返回 `<-chan AgentEvent`，buffer=32（`grep -n "make(chan AgentEvent, 32)" internal/agent/agent.go`）
- [ ] 常量 `maxTokensCeiling=64000` 与 `maxOutputTokensRecoveries=3` 在 `internal/agent/agent.go:24-27`（ 和）
- [ ] `StreamingExecutor.Submit/CollectResults/HasPending/Reset` 在 `internal/agent/streaming_executor.go:36/54/67/73` 实现，使用 `sync.WaitGroup` 等待并发完成
- [ ] 单工具执行：`executeSingleTool` 在 `internal/agent/agent.go:347` 处理 unknown tool / permission Deny / permission Ask / Hook PreToolUse / Hook PostToolUse 五个分支
- [ ] 错误恢复：`handleStreamError` 在 `internal/agent/agent.go:264` 处理 `*llm.ContextTooLongError` 和 `*llm.RateLimitError`；`parseRetryAfter` 在 agent.go:290 默认 5 秒
- [ ] Plan reminder：`BuildPlanModeReminder` 在 `internal/prompt/plan_mode.go:85`，`reminderInterval=5`，iteration==1 给完整 reminder
- [ ] `planfile.GetOrCreatePlanPath` / `PlanExists` / `LoadPlan` / `SavePlan` / `ResetPlanPath` 在 `internal/planfile/planfile.go:36/62/70/84/58` 实现
- [ ] 任务模型 `Task` / `TaskList` / `Store` 与四个工具在 `internal/todo/todo.go:17`、`todo.go:29`、`store.go:9`、`tools.go:11/64/121/180`
- [ ] 边界 `extractFilePath` 在 `internal/agent/agent.go:338` 按 `file_path → path → pattern → target` 顺序查找

## 2. 接入完整性（必查，杜绝死代码）
- [ ] `grep -rn "agent.New\b" internal/tui` 至少 2 个非测试调用方（`internal/tui/tui.go:360`、`internal/tui/tui.go:722`）
- [ ] `grep -rn "m\.ag\.Run\b" internal/tui` 至少 2 个调用方（`internal/tui/tui.go:1907` 与 `:1941`）
- [ ] `grep -rn "BuildPlanModeReminder" internal --include="*.go"` 至少 1 个调用方在 agent loop（`internal/agent/agent.go:91`）
- [ ] `grep -rn "planfile\." internal --include="*.go"` 调用方在 TUI `/plan/doExitPlanMode` 流程（`internal/tui/tui.go:1202/1225/1226/1229/1397/1398/1418` 与 `internal/agent/agent.go:89/90`）
- [ ] `grep -rn "todo\.TaskCreateTool\|todo\.TaskGetTool\|todo\.TaskListTool\|todo\.TaskUpdateTool" internal/tui` 全 4 个工具均在 `internal/tui/tui.go:536-539` 注册
- [ ] `grep -rn "permissions.NewChecker" internal --include="*.go"` 在 TUI 构造 Agent 时使用（`internal/tui/tui.go:362` 与 `:724`）
- [ ] `Agent.ToolNameFilter` 字段在 TUI `internal/tui/tui.go:370` 与 `:732` 设值（`coordinatorToolFilter`）
- [ ] `Agent.NotificationFn` 字段在 TUI `internal/tui/tui.go:369` 与 `:731` 设值（`drainTaskNotifications`）
- [ ] 死代码已清理（2026-05-21）:
 - [ ] `executeToolCalls` / `partitionToolCalls` / `toolBatch` 已删（`StreamingExecutor` 替代后冗余，目标设计 `StreamingToolExecutor.canExecuteTool` 已覆盖语义）
 - [ ] `buildEnvironmentContext` 已删（与 `prompt/sections.go:123 EnvironmentSection` 重复，目标设计 `constants/prompts.ts:640 computeEnvInfo` 走 system prompt 通道）
 - [ ] `planfile.SetPlanFilePath` / `IsPlanFilePath` 已删
 - [ ] `BuildPlanModeReentryReminder` 已删
 - [ ] `BuildPlanModeExitReminder` 已接入 `internal/tui/tui.go:1400executePlanApproval`

## 3. 编译与测试
- [ ] `go build ./...` 通过（顶层命令，2026-05-21 已验证）
- [ ] `go test ./internal/agent/...` 中 `TestAgentSimpleResponse` / `TestAgentToolCallLoop` / `TestAgentMaxIterations` / `TestAgentWithThinking` / `TestMultiRoundConversation` / `TestFilterSchemasByName` / `TestFilterSchemasByNameEmptyInput` 七个单测可独立执行（agent_test.go 中 `func Test*` 已定义）
- [ ] `go vet ./...` 无警告（2026-05-21 顶层运行无输出）

## 4. 端到端验证
- [ ] TUI 入口：用户在聊天框敲一条普通消息后看到 `StreamText` 渲染、最终 `LoopComplete` 终止 —— `internal/tui/tui.go:2023` (`agent.StreamText`) 与 `:2194` (`agent.LoopComplete`) 显式分发，调用链 `m.sendMessage → m.ag.Run → handleAgentEvent`（tui.go:1907 → events.go → tui.go:2021-2200）
- [ ] Plan Mode：输入 `/plan` 进入 Plan，注入 reminder + 设 `Checker.Mode=ModePlan` + 创建 plan path；输入 `/do` 退出 Plan + ResetPlanPath（`tui.go:1197-1232`）
- [ ] HITL 权限：当 Ask 时 TUI 渲染 `Yes / Yes, don't ask again / No` 选项（tui.go:1292-1296，`PermAllow/PermAllowAlways/PermDeny`）
- [ ] 留存证据: 验收阶段无截图；如需补，可在 TUI 中输入 `hi` 拍照保存 stream 渲染

## 5. 文档
- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`specs/go/ch04/`）
- [ ] commit 信息标注 `ch04` 与三件套关闭状态（待统一打包提交）

```

### Python

```markdown
# ch04: Agent Loop Spec

## 1. 背景

LLM 单次回复无法完成完整软件任务，必须把「调模型 → 拿工具调用 → 跑工具 → 把结果回灌」组成 ReAct 循环反复运行，直到模型不再请求工具。没有这层 Agent Loop，工具系统（ch03）与后续模块（ch05~ch15）都失去挂载点；流式 token、思考块、token 配额、用户中断、Plan Mode、HITL 权限请求都只能停留在工具层，无法上浮到 Textual 终端 UI。本章把这条循环、配套事件流、Plan Mode 状态与 max_tokens 升档串到 `codebot/agent.py` 一个文件内。

## 2. 目标

对外提供 `codebot.agent.Agent`：调用者构造好 `LLMClient`、`ToolRegistry`、（可选）`PermissionChecker` / `HookEngine` / `MemoryManager` 后，调一次 `async for event in agent.run(conversation)` 即可拿到 `AgentEvent` 异步流；Textual UI 只负责把事件 fan-out 到屏幕，剩下的工具分发、流式拼接、批次并发、Plan Mode reminder 注入、max_tokens 恢复、压缩通知全部由 Agent 在协程内串好。Plan Mode 通过 `PermissionMode.PLAN` 切换；plan 文件路径由 `Agent._get_plan_path` 进程内单例懒加载；团队任务工具由 `codebot/tools/task_*.py` 提供并通过 `TeamManager` 注册。

## 3. 功能需求

- F1: `Agent.run(conversation)` 是 `async def ... -> AsyncIterator[AgentEvent]`（`codebot/agent.py:397`）；调用方用 `async for` 消费事件，循环结束生成器自然终止。
- F2: 每轮迭代先调 `_consume_mailbox` 拉团队消息，再走 `apply_tool_result_budget`（Layer 1 持久化超长结果）与 `auto_compact`（Layer 2 触发压缩），压缩成功时回送 `CompactNotification` 并重注入环境上下文 / 长记忆。
- F3: 通过 `LLMClient.stream(conversation, system, tools)` 拉取 `StreamEvent`，由 `StreamCollector.consume`（`codebot/agent.py:178`）转成 `StreamText` / `ThinkingText` / `ToolUseEvent`；`ThinkingComplete` 累积进 `LLMResponse.thinking_blocks`；`StreamEnd` 记录 `stop_reason` / `input_tokens` / `output_tokens`。
- F4: 工具调用按 `partition_tool_calls` 切分批次（`codebot/agent.py:218`）；`is_concurrency_safe=True` 的相邻工具进入同一并发批，剩余工具单独成批；并发批用 `asyncio.gather` 跑，串行批逐个 `_execute_tool` 处理 HITL；本轮结束统一 `add_tool_results_message` 回灌。
- F5: 主循环终止条件：本轮无 `tool_calls` → 追加 assistant 消息并 `yield LoopComplete`；连续 3 次 `consecutive_unknown` → `yield ErrorEvent` 退出；`asyncio.CancelledError` → 协程被取消时自然终止；超过 `max_iterations`（默认 50）→ `yield ErrorEvent`。
- F6: 处理 `stop_reason == "max_tokens"`：首次升档调 `client.set_max_output_tokens(MAX_TOKENS_CEILING)`（64000）并把已生成文本作为 assistant 消息追加，再注入 resume 指令；后续最多 `MAX_OUTPUT_TOKENS_RECOVERIES`（3）次恢复轮；超出仍未完成则继续走主循环逻辑。每次升档 / 恢复都 `yield RetryEvent(reason=...)`。
- F7: 流式异常处理：底层 `LLMClient.stream` 抛错时由调用方协程冒泡（`asyncio.CancelledError` 直接退出）；压缩内部错误 `auto_compact` 返回 `str` 时由主循环 `yield ErrorEvent`；当前实现暂未引入独立的 `ContextTooLongError` / `RateLimitError` 重试分支（与 Go `handleStreamError` 的差异点）。
- F8: 权限交互：`_execute_tool`（`codebot/agent.py:788`）调 `permission_checker.check`，`deny` → 返回错误结果；`ask` → `yield PermissionRequest`（带 `asyncio.Future`），UI 端 `set_result` 把 `PermissionResponse.ALLOW / DENY / ALLOW_ALWAYS` 回填；`ALLOW_ALWAYS` 时调 `rule_engine.append_local_rule` 写入 `{tool}(content*)` 规则。
- F9: 工具执行包夹 Hooks：执行前走 `hook_engine.run_pre_tool_hooks`（可阻断，返回拒绝即直接当错误结果回灌）；执行后走 `run_hooks("post_tool_use", ctx)`（不阻断）；`_infer_file_path` 从 `args["file_path"]` / `args["path"]` 提取代表性路径供 hook 匹配。
- F10: 工具集动态裁剪：`Agent.coordinator_mode` 字段使 `build_system_prompt` 切到 coordinator 版；`ToolRegistry.is_enabled` 在 `_execute_tool` / `partition_tool_calls` 两处过滤；`registry.get_deferred_tool_names()` 写入 system reminder 让模型按需 `ToolSearch` 加载。
- F11: Plan Mode 文件状态：`Agent._get_plan_path`（`codebot/agent.py:334`）懒生成单例路径，用 24 词形容词 + 24 词名词 + `MMDD-HHMM` 时间戳拼出可读 slug，落到 `<work_dir>/.codebot/plans/<slug>.md`；进入 Plan 模式每轮调 `build_plan_mode_reminder(plan_path, plan_exists, iteration)` 注入提醒。
- F12: 团队协作任务工具：`codebot/tools/task_create.py` / `task_get.py` / `task_list.py` / `task_update.py` 实现 `TaskCreate` / `TaskGet` / `TaskList` / `TaskUpdate` 四个 Tool；持久化交给 `TeamManager.get_task_store()`，支持 `blocks` / `blocked_by` 依赖关系；与 Go 版本的 `internal/todo` 单进程任务不同，Python 版任务以「跨智能体共享任务板」为定位。

## 4. 非功能需求

- N1: 工具并发安全：`Tool.is_concurrency_safe` 字段决定能否进入同一并发批；`partition_tool_calls` 顺序扫描调用并把连续的安全工具聚为一批，写工具与命令工具单独成批，保证串行语义。
- N2: 事件流式产出：`run` 是异步生成器，事件随 `yield` 直接传给消费者，不引入显式队列；UI 端用 `async for` 即可背压式消费，无需手动配 buffer。
- N3: 工具结果回灌前由 `_maybe_persist_or_truncate`（`codebot/agent.py:1105`）按 `SINGLE_RESULT_CHAR_LIMIT` 决定是否持久化到 session 目录并改成预览，剩余按 `MAX_OUTPUT_CHARS` 截断追加 `… (output truncated)`，防止单工具结果撑爆下一轮上下文。
- N4: 工具参数代表性路径：`_infer_file_path` 只取 `file_path` / `path` 两个 schema 字段（与 Go 的 `file_path → path → pattern → target` 顺序不同，Python 实现更精简，仅用于 hook 匹配）。
- N5: Plan slug 必须可读：`_ADJECTIVES` 24 词 + `_NOUNS` 24 词 + 时间戳，避免纯数字命名，便于人眼区分 `.codebot/plans/` 下多个历史 plan。

## 5. 设计概要

- 核心数据结构:
  - `Agent`（`codebot/agent.py:284`）：`client`/`registry`/`protocol`/`work_dir`/`max_iterations`/`permission_checker`/`permission_mode`/`context_window`/`session_dir`/`compact_breaker`/`instructions_content`/`memory_manager`/`hook_engine`/`active_skills`/`coordinator_mode`/`team_name`/`_team_manager`/`_plan_path_cache` 等字段。
  - `AgentEvent` 类型联合（`codebot/agent.py:138-153`）：`StreamText` / `ThinkingText` / `RetryEvent` / `ToolUseEvent` / `ToolResultEvent` / `TurnComplete` / `LoopComplete` / `UsageEvent` / `ErrorEvent` / `PermissionRequest` / `CompactNotification` / `HookEvent`，每个都是独立 `@dataclass`。
  - `PermissionResponse(Enum)`：`ALLOW` / `DENY` / `ALLOW_ALWAYS`（`codebot/agent.py:125`）。
  - `StreamCollector` / `LLMResponse` / `ThinkingBlock`（`codebot/agent.py:158-211`）：把底层 `StreamEvent` 折叠成一轮完整响应。
  - `ToolBatch` / `partition_tool_calls`（`codebot/agent.py:213-234`）：把工具调用切成并发批 + 串行批。
  - `StreamingExecutor`（`codebot/agent.py:247-280`）：保留并发任务编号排序后 gather 收集，目前 `run` 主路径主要走 `_execute_batch_parallel`，`StreamingExecutor` 给 SubAgent / Teams 复用。
- 主流程（一次迭代）:
  1. `iteration += 1`，超过 `max_iterations` 直接 `yield ErrorEvent` 退出；
  2. `_consume_mailbox` 拉团队邮箱消息；
  3. 走 Layer 1 / Layer 2 压缩，压缩后回送 `CompactNotification` 并重注入环境与长记忆；
  4. `plan_mode` 时通过 `build_plan_mode_reminder` 注入 reminder；
  5. 把 hook 拉出的 prompt 段拼到 `build_system_prompt`；
  6. `registry.get_all_schemas(protocol)` 取工具 schema；
  7. `client.stream(...)` 配合 `StreamCollector.consume` 把流式事件转 `AgentEvent`；
  8. 累计 token usage 并 `yield UsageEvent`；
  9. `stop_reason == "max_tokens"` 走升档 + 恢复轮；
  10. 无 `tool_calls` → 追加 assistant 消息、按周期触发记忆抽取、`yield LoopComplete` 退出；
  11. 有 `tool_calls` → 落 assistant 消息、按批次并发 / 串行执行、把 `ToolResultBlock` 收齐回灌、`yield TurnComplete`。
- 调用链:
  - 用户输入 → `MewcodeApp.send_user_message`（`codebot/app.py:840`）→ `asyncio.create_task(_send_message)` → `agent.run` async for → 各 `isinstance(event, ...)` 分支渲染 Textual widget。
  - `/plan` → `codebot/commands/handlers/plan.py:handle_plan` → `MewcodeApp.set_plan_mode(True)` → `agent.set_permission_mode(PermissionMode.PLAN)` → 下一轮注入 reminder。
  - `/do` → `codebot/commands/handlers/do.py:handle_do` → `MewcodeApp.set_plan_mode(False)` → 恢复 `PermissionMode.DEFAULT`。
  - HITL → `_execute_tool` `yield PermissionRequest(future=...)` → UI `_handle_permission_request` 把用户选择 `future.set_result(...)` 回填。
- 与其他模块的交互:
  - 依赖 `codebot.client`（LLMClient）、`codebot.conversation`（`ConversationManager` / `ToolUseBlock` / `ToolResultBlock`）、`codebot.context`（auto_compact / 预算）、`codebot.permissions`、`codebot.hooks`、`codebot.prompts`（plan reminder / system prompt）、`codebot.memory.auto_memory`、`codebot.tools`。
  - 被 `codebot/app.py`（Textual TUI）、`codebot/agents/fork.py`（SubAgent fork）、`codebot/teams/inprocess.py`（in-process teammate）调用。

## 6. Out of Scope

- 本章不实现 SubAgent / Fork（属 ch13）；`Agent.run` 只跑一个智能体，多智能体由 `codebot/agents/fork.py` 单独承担。
- 本章不实现 Worktree 隔离（属 ch14）；Plan 文件直接落 `work_dir/.codebot/plans`。
- Plan Mode 的 Reentry / Exit Reminder 文本目前仅有 `_PLAN_MODE_FULL_REMINDER` / `_PLAN_MODE_SPARSE_REMINDER` 两种；后续轮次的退出 / 重入提醒文本属未来增强。
- 团队共享任务 `TaskCreate/TaskGet/TaskList/TaskUpdate` 的依赖图渲染（`blocks` / `blocked_by`）不在本章 UI 范围内。
- 除 `max_tokens` 以外的其他 `stop_reason`（`pause_turn` / `refusal`）当前实现未单独分支。
- `ContextTooLongError` / `RateLimitError` 的独立重试路径暂未引入（与 Go 版差异点，留给后续 PR）。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch04: Agent Loop Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。本章为验收，所有任务已经在 `origin/python` 分支落地，逐项标注真实文件 / 类 / 行号。

## T1: 定义 AgentEvent 事件家族（dataclass union）

- 影响文件: `codebot/agent.py:55-153`
- 依赖任务: 无
- 完成标准: `StreamText` / `ThinkingText` / `RetryEvent` / `ToolUseEvent` / `ToolResultEvent` / `TurnComplete` / `LoopComplete` / `UsageEvent` / `ErrorEvent` / `CompactNotification` / `HookEvent` 共 11 个 `@dataclass`，加上 `PermissionResponse(Enum)` 三态（`ALLOW` / `DENY` / `ALLOW_ALWAYS`，agent.py:125）和 `PermissionRequest` dataclass（含 `asyncio.Future`）共 12 个事件类型；`AgentEvent = StreamText | ThinkingText | ...` 类型联合在 agent.py:138-153 定义。

## T2: 实现 Agent 类与构造器

- 影响文件: `codebot/agent.py:284-327`
- 依赖任务: T1
- 完成标准: `Agent.__init__` 接受 `client` / `registry` / `protocol` / `work_dir=".";` / `max_iterations=50` / `permission_checker=None` / `context_window=200_000` / `instructions_content=""` / `memory_manager=None` / `hook_engine=None`；初始化时拉取 `permission_checker.mode` 同步 `permission_mode`、`ensure_session_dir(work_dir)` 准备会话目录、`CompactCircuitBreaker()` 注入压缩熔断、`agent_id = uuid.uuid4().hex[:12]`；附带 `coordinator_mode` / `team_name` / `_team_manager` 三字段挂在团队 / 协调器场景。

## T3: 实现 run 主循环（async generator）

- 影响文件: `codebot/agent.py:397-716`
- 依赖任务: T1, T2
- 完成标准: `async def run(self, conversation) -> AsyncIterator[AgentEvent]`；进入前注入 environment context + long-term memory；`while True` 跑迭代；每轮先 `_consume_mailbox`，再 `apply_tool_result_budget` + `auto_compact`（CompactEvent → `yield CompactNotification`）；调 `build_system_prompt` 拼系统提示；Plan Mode 时调 `build_plan_mode_reminder` 注入；调 `client.stream` + `StreamCollector.consume` 把流式事件 `yield` 出去；累计 token 后 `yield UsageEvent`；`stop_reason == "max_tokens"` 走 `MAX_TOKENS_CEILING=64000` / `MAX_OUTPUT_TOKENS_RECOVERIES=3` 升档恢复（agent.py:49-50, 529-559）；无工具调用 → `yield LoopComplete` 退出；连续 3 次 unknown → `yield ErrorEvent` 退出；有工具调用 → 按 `partition_tool_calls` 切批执行，最后 `add_tool_results_message` + `yield TurnComplete`。

## T4: 实现 StreamCollector 与 LLMResponse

- 影响文件: `codebot/agent.py:158-211`
- 依赖任务: T1
- 完成标准: `StreamCollector.consume(stream)` 为 `async generator`；遇 `TextDelta` 追加 `LLMResponse.text` 并 `yield StreamText`；遇 `ThinkingDelta` `yield ThinkingText`；遇 `ThinkingComplete` 累加 `ThinkingBlock(thinking, signature)`；遇 `ToolCallComplete` 累加 `LLMResponse.tool_calls` 并 `yield ToolUseEvent`；遇 `StreamEnd` 写入 `stop_reason` / `input_tokens` / `output_tokens`。

## T5: 实现 partition_tool_calls 工具批次切分

- 影响文件: `codebot/agent.py:213-234`
- 依赖任务: T2
- 完成标准: `partition_tool_calls(tool_calls, registry) -> list[ToolBatch]`；逐个调用判断 `tool.is_concurrency_safe and registry.is_enabled(name)`；若为安全且上一批 `concurrent=True` 则归入同批，否则新开 `ToolBatch(concurrent=safe, calls=[tc])`；`test_partition_tool_calls`（`tests/test_agent.py`）覆盖 5 个调用→3 批的切分。

## T6: 实现 StreamingExecutor 并发收集器

- 影响文件: `codebot/agent.py:247-280`
- 依赖任务: T2
- 完成标准: `StreamingExecutor.submit(coro)` 用 `asyncio.create_task` 起协程并按 `_order` 编号；`collect_results()` 按编号排序后 `asyncio.gather(..., return_exceptions=True)`，遇 `Exception` 包装成 `_ToolExecResult(is_error=True)` 不中断主流程；供 SubAgent / Teams 在流式阶段就启动工具时复用。

## T7: 实现 _execute_batch_parallel 并发批执行

- 影响文件: `codebot/agent.py:782-786`
- 依赖任务: T5, T6
- 完成标准: `_execute_batch_parallel(calls)` 对每个 `ToolCallComplete` 调 `_execute_single_tool_direct`，再 `asyncio.gather` 并发；返回 `list[_ToolExecResult]`，主循环负责把每个结果做 `_maybe_persist_or_truncate` 后写入 `tool_results`，同时 `yield ToolResultEvent`。

## T8: 实现 _execute_tool 串行批 / HITL 路径

- 影响文件: `codebot/agent.py:788-867`
- 依赖任务: T2, T6
- 完成标准: `_execute_tool(tc)` 为 `async generator`，依次处理 unknown tool / disabled / `permission_checker.check` 三态：`deny` → 错误结果；`ask` → `yield PermissionRequest(future=loop.create_future())` 等 UI 把 `future.set_result(...)` 回填；`ALLOW_ALWAYS` 时调 `rule_engine.append_local_rule(Rule(tool, pattern=content[:60]+"*", "allow"))` 持久化；`pydantic.ValidationError` 拿 `Parameter validation error` 结果；其他异常拿 `Tool execution error`；产出 `(ToolResult, elapsed, is_unknown)` 三元组。

## T9: 实现 Hook 前后包夹

- 影响文件: `codebot/agent.py:371-395`、`codebot/agent.py:603-685`
- 依赖任务: T8
- 完成标准: `_build_hook_context(event, **kwargs)` 拼 `HookContext`；`_infer_file_path(args)` 取 `file_path` 或 `path`；`_drain_hook_events()` 把 `HookEngine.drain_notifications()` 转 `HookEvent` `yield` 出去；主循环在 `session_start` / `turn_start` / `pre_send` / `post_receive` / `pre_tool_use`（可阻断，返回 `Hook rejected: {reason}` 错误结果）/ `post_tool_use` / `turn_end` / `session_end` 共 8 个事件点插入 hook 执行。

## T10: 实现 plan_path 单例与 Plan Mode reminder

- 影响文件: `codebot/agent.py:329-355`、`codebot/prompts.py:158-237`
- 依赖任务: 无
- 完成标准: `Agent._get_plan_path` 用 `random.choice(_ADJECTIVES) + "-" + random.choice(_NOUNS) + "-" + datetime.now().strftime("%m%d-%H%M")` 生成 slug，落到 `work_dir/.codebot/plans/<slug>.md`，首次调用 `mkdir(parents=True, exist_ok=True)` 并缓存到 `_plan_path_cache`；`build_plan_mode_reminder(plan_path, plan_exists, iteration)`（prompts.py:203）在 `iteration==1` 给完整 reminder，按 `_REMINDER_INTERVAL=5` 周期再发完整版，间隔轮次发 sparse reminder；`Agent.set_permission_mode(mode)` 同时更新 `permission_checker.mode`。

## T11: 实现团队任务四工具

- 影响文件: `codebot/tools/task_create.py`、`task_get.py`、`task_list.py`、`task_update.py`
- 依赖任务: 无
- 完成标准: 四个 Tool 类（`TaskCreateTool` / `TaskGetTool` / `TaskListTool` / `TaskUpdateTool`）皆继承 `Tool`，定义 `name` / `description` / `params_model` / `category` / `is_concurrency_safe=True`；构造函数注入 `team_manager: TeamManager` 与 `team_name`；`execute` 走 `team_manager.get_task_store(team_name)` 拿 `TaskStore` 后调 `create/get/list_tasks/update`；`TaskUpdate` 校验 `VALID_STATUSES = {"pending","in_progress","completed","blocked"}`；`TaskList` 输出按状态 icon `○◐●✕` 渲染。

## T12: 实现 _maybe_persist_or_truncate 工具结果整形

- 影响文件: `codebot/agent.py:1105-1117`
- 依赖任务: T2
- 完成标准: 工具输出长度超 `SINGLE_RESULT_CHAR_LIMIT` 时调 `persist_tool_result` 落到 session 目录、返回 `make_persisted_preview`；超 `MAX_OUTPUT_CHARS` 时直接截断追加 `… (output truncated)`；其他情况原样返回。

## T13: 接入主流程（Textual TUI）

- 影响文件: `codebot/app.py:649`（构造 `Agent`）、`codebot/app.py:850-855`（`set_plan_mode`）、`codebot/app.py:1085`（`async for event in agent.run`）、`codebot/app.py:1099-1230`（事件分发）、`codebot/commands/handlers/plan.py`、`codebot/commands/handlers/do.py`
- 依赖任务: T1~T12
- 完成标准: 用户进入聊天后 `MewcodeApp` 构造 `Agent` 并装好 `permission_checker` / `memory_manager` / `hook_engine`；`send_user_message` 调 `asyncio.create_task(self._send_message(text))`；`_send_message` 用 `async for event in self.agent.run(self.conversation)` 消费事件，按 `isinstance` 分别渲染 `StreamText` / `ThinkingText` / `ToolUseEvent` / `ToolResultEvent` / `TurnComplete` / `LoopComplete` / `UsageEvent` / `HookEvent` / `CompactNotification` / `ErrorEvent` / `PermissionRequest` / `RetryEvent`；`/plan` 命令切 `PermissionMode.PLAN`，`/do` 切 `PermissionMode.DEFAULT`。

## T14: 端到端验证

- 影响文件: 无（仅运行验证）
- 依赖任务: T13
- 完成标准:
  - `python -m compileall codebot` 通过（语法 / 导入正确）。
  - `ruff check codebot tests` 无 error。
  - `pytest tests/test_agent.py -q` 通过：覆盖 `test_single_step_tool_call`、`test_multi_step_autonomous`、`test_stop_end_turn`、`test_stop_max_iterations`、`test_stop_cancel`、`test_stop_consecutive_unknown_tools`、`test_message_splicing`、`test_concurrent_batch_execution`、`test_token_usage_accumulates`、`test_plan_mode`、`test_plan_mode_denied_tool_returns_error`、`test_partition_tool_calls`、`test_system_prompt_normal`、`test_system_prompt_plan`、`test_plan_mode_sparse_reminder`、`test_environment_context` 共 16 个测试用例（tests/test_agent.py）。
  - 在 Textual 界面输入 `hello` 看到 `StreamText` 流式渲染与 `LoopComplete` 终止；输入 `/plan` 看到 plan reminder 注入并禁止写工具。

## 进度

- [ ] T1 AgentEvent 11 dataclass + Enum + 联合类型
- [ ] T2 Agent.__init__
- [ ] T3 Agent.run 主循环
- [ ] T4 StreamCollector
- [ ] T5 partition_tool_calls
- [ ] T6 StreamingExecutor
- [ ] T7 _execute_batch_parallel
- [ ] T8 _execute_tool（HITL / 权限）
- [ ] T9 Hook 包夹
- [ ] T10 plan_path 单例 + build_plan_mode_reminder
- [ ] T11 TaskCreate/Get/List/Update 四工具
- [ ] T12 _maybe_persist_or_truncate
- [ ] T13 Textual TUI 接入
- [ ] T14 端到端验证（compileall + ruff + pytest + 手工 plan 模式）

```

```markdown
# ch04: Agent Loop Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性

- [ ] 类 `Agent` 在 `codebot/agent.py:284`，字段含 `client` / `registry` / `protocol` / `work_dir` / `max_iterations` / `permission_checker` / `permission_mode` / `context_window` / `session_dir` / `compact_breaker` / `instructions_content` / `memory_manager` / `hook_engine` / `coordinator_mode` / `team_name` / `_plan_path_cache`（`grep -n "class Agent:" codebot/agent.py`）
- [ ] 12 个 AgentEvent 类型 + `PermissionResponse(Enum)` 在 `codebot/agent.py:55-153`：`StreamText` / `ThinkingText` / `RetryEvent` / `ToolUseEvent` / `ToolResultEvent` / `TurnComplete` / `LoopComplete` / `UsageEvent` / `ErrorEvent` / `CompactNotification` / `HookEvent` / `PermissionRequest`（`grep -nE "^@dataclass|^class [A-Z]" codebot/agent.py` 至少返回 12 条）
- [ ] 方法 `Agent.run` 在 `codebot/agent.py:397` 实现，签名 `async def run(self, conversation) -> AsyncIterator[AgentEvent]`（`grep -n "async def run" codebot/agent.py`）
- [ ] 常量 `MAX_TOKENS_CEILING=64000` 与 `MAX_OUTPUT_TOKENS_RECOVERIES=3` 在 `codebot/agent.py:49-50`，`MEMORY_EXTRACTION_INTERVAL=5` 在 agent.py:48（`grep -nE "MAX_TOKENS_CEILING|MAX_OUTPUT_TOKENS_RECOVERIES|MEMORY_EXTRACTION_INTERVAL" codebot/agent.py`）
- [ ] `StreamCollector.consume` 在 `codebot/agent.py:178`，处理 `TextDelta` / `ThinkingDelta` / `ThinkingComplete` / `ToolCallComplete` / `StreamEnd` 五类事件（`grep -n "isinstance(event," codebot/agent.py | head`）
- [ ] `partition_tool_calls` 在 `codebot/agent.py:218`，`ToolBatch` 在 agent.py:213，安全调用合并到同一并发批的逻辑实现完整
- [ ] `StreamingExecutor.submit / collect_results` 在 `codebot/agent.py:247-280`，使用 `asyncio.create_task` + `asyncio.gather(..., return_exceptions=True)`
- [ ] `_execute_tool` 在 `codebot/agent.py:788`，处理 unknown tool / disabled / permission deny / permission ask（`PermissionRequest` 带 `asyncio.Future`）/ `ALLOW_ALWAYS` 写规则 5 个分支
- [ ] `_execute_batch_parallel` 在 `codebot/agent.py:782`，`_execute_single_tool_direct` 在 agent.py:742
- [ ] `_maybe_persist_or_truncate` 在 `codebot/agent.py:1105`，按 `SINGLE_RESULT_CHAR_LIMIT` / `MAX_OUTPUT_CHARS` 分支
- [ ] `Agent._get_plan_path` 在 `codebot/agent.py:334`，使用 `_ADJECTIVES`(24) + `_NOUNS`(24) + `MMDD-HHMM` 拼 slug，`_plan_path_cache` 单例
- [ ] `build_plan_mode_reminder` 在 `codebot/prompts.py:203`，`_REMINDER_INTERVAL=5`，`iteration==1` 给完整 reminder（`grep -n "_REMINDER_INTERVAL" codebot/prompts.py`）
- [ ] 任务模型与四工具：`TaskCreateTool` / `TaskGetTool` / `TaskListTool` / `TaskUpdateTool` 在 `codebot/tools/task_create.py`、`task_get.py`、`task_list.py`、`task_update.py`，皆继承 `Tool` 且 `is_concurrency_safe = True`
- [ ] 工具结果回灌：`_infer_file_path` 在 `codebot/agent.py:381` 按 `file_path → path` 顺序查找

## 2. 接入完整性（杜绝死代码）

- [ ] `grep -n "Agent(" codebot/app.py` 显示 `codebot/app.py:649` 构造 Agent 时传入 `client` / `registry` / `protocol` / `work_dir` / `permission_checker` / `context_window` / `instructions_content` / `memory_manager` / `hook_engine`
- [ ] `grep -n "self.agent.run" codebot/app.py` 至少 1 处（`codebot/app.py:1085` 的 `async for event in self.agent.run(self.conversation)`）
- [ ] `grep -rn "build_plan_mode_reminder" codebot/` 至少 2 处调用方：`codebot/agent.py:475` 与 `tests/test_agent.py`
- [ ] `grep -rn "set_permission_mode\|set_plan_mode" codebot/` 调用链：`codebot/commands/handlers/plan.py` → `MewcodeApp.set_plan_mode`（`codebot/app.py:850`）→ `agent.set_permission_mode(PermissionMode.PLAN)`（`codebot/agent.py:352`）
- [ ] `grep -rn "TaskCreateTool\|TaskGetTool\|TaskListTool\|TaskUpdateTool" codebot/` 四个工具在团队注册路径上被引用（团队场景由 `TeamManager` 注册到 Registry）
- [ ] `grep -n "permission_checker" codebot/app.py` 在 TUI 构造 Agent 时使用（`codebot/app.py:654`）
- [ ] `Agent.coordinator_mode` 在 TUI 协调器路径上设值，`build_system_prompt` 据此切到 coordinator 系统提示
- [ ] `Agent.hook_engine` 在 `codebot/app.py:658` 注入 `HookEngine`，主循环 8 个 hook 事件点（session_start / turn_start / pre_send / post_receive / pre_tool_use / post_tool_use / turn_end / session_end）皆有触发
- [ ] `_handle_permission_request` 在 `codebot/app.py` 监听 `PermissionRequest` 事件，把用户选择 `future.set_result(PermissionResponse.X)` 回填
- [ ] `RetryEvent` 在 `codebot/app.py:1119` 渲染为 `↻ Retrying: ...` 系统消息

## 3. 编译与测试

- [ ] `python -m compileall codebot` 通过，无语法 / 导入错误
- [ ] `ruff check codebot tests` 无 error
- [ ] `pytest tests/test_agent.py -q` 16 个测试用例全部通过：
  - `test_single_step_tool_call`、`test_multi_step_autonomous`、`test_stop_end_turn`
  - `test_stop_max_iterations`、`test_stop_cancel`、`test_stop_consecutive_unknown_tools`
  - `test_message_splicing`、`test_concurrent_batch_execution`、`test_token_usage_accumulates`
  - `test_plan_mode`、`test_plan_mode_denied_tool_returns_error`
  - `test_partition_tool_calls`
  - `test_system_prompt_normal`、`test_system_prompt_plan`、`test_plan_mode_sparse_reminder`、`test_environment_context`

## 4. 端到端验证

- [ ] Textual 入口：用户在输入框敲普通消息后看到 `StreamText` 渲染、最终 `LoopComplete` 终止 —— 调用链 `MewcodeApp.send_user_message → asyncio.create_task(_send_message) → async for event in self.agent.run(self.conversation) → isinstance 分支`（`codebot/app.py:840 → :1085 → :1099-1230`）
- [ ] Plan Mode：输入 `/plan` 走 `handle_plan` → `set_plan_mode(True)` → `agent.set_permission_mode(PermissionMode.PLAN)`，下一轮看到 plan reminder 注入；输入 `/do` 走 `handle_do` → 恢复 `PermissionMode.DEFAULT`（`codebot/commands/handlers/plan.py` / `do.py`）
- [ ] HITL 权限：`PermissionRequest` 事件触发时 Textual 渲染权限对话框（`codebot/permission_dialog.py`），用户选「允许 / 拒绝 / 允许始终」对应 `PermissionResponse.ALLOW` / `DENY` / `ALLOW_ALWAYS`；选 `ALLOW_ALWAYS` 时调 `rule_engine.append_local_rule` 持久化（`codebot/agent.py:846-851`）
- [ ] max_tokens 升档：模拟 `stop_reason="max_tokens"` 看到 `RetryEvent(reason="max_tokens escalation")` 与 `client.set_max_output_tokens(64000)`；连续 3 次后停止恢复进入下一轮主流程（`codebot/agent.py:529-559`）
- [ ] 留存证据：验收阶段无截图；如需补，可在 Textual 中输入 `hi` 拍照保存 stream 渲染

## 5. 文档

- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`docs/python/ch04/`）
- [ ] commit 信息标注 `ch04` 与三件套关闭状态（待统一打包提交）

```

### Java

```markdown
# ch04: Agent Loop Spec

## 1. 背景

ch02 把 LLM 客户端跑通了：一次 `stream()` 调用从模型拿到一段文本或一组 tool_use。ch03 把工具注册表与六个核心工具搭好了。但「一次调用」和「一个 Agent」之间还差一个关键环节：让模型自主地反复思考 → 调工具 → 看结果 → 再思考，直到任务真正完成。没有 Agent Loop，CodeBot 还只是个能调一次工具的聊天机器人。本章把这条循环管线做出来：一个虚拟线程驱动的 while 循环，消费 `BlockingQueue<StreamEvent>` 流式事件，分类执行工具调用（只读并行 / 写串行），把结果回写 `ConversationManager`，再以 `AgentEvent` 形式向 TUI 推送进度。

## 2. 目标

对外提供 `com.codebot.agent.Agent`：调用者准备好 `LlmClient` / `ToolRegistry` / `protocol`，调一次 `agent.run(conversation)` 拿到 `BlockingQueue<AgentEvent>`，从中 poll 出文本流、思考流、工具调用、工具结果、用量、错误、轮次完成、循环完成等事件并直接渲染到 TUI。循环内部完成：消费上游 stream → 收集 tool_use → `StreamingExecutor` 分流并发执行 → 回写会话 → 进入下一轮；同时承担 deferred tool 提醒注入、Plan Mode 提醒注入、自动 compact、max_tokens 恢复、context 超限重试、rate-limit 退避等运维职责。

## 3. 功能需求

- F1: 提供 `Agent` 类，构造接收 `LlmClient` / `ToolRegistry` / `protocol`；支持通过 setter 注入 `PermissionChecker` / `HookEngine` / `maxIterations` / `workDir` / 通知 supplier / tool name filter。
- F2: `Agent.run(ConversationManager)` 返回 `BlockingQueue<AgentEvent>`，内部用 `Thread.startVirtualThread` 启动 agent loop，确保 TUI 主线程不阻塞；异常一律包成 `AgentEvent.ErrorEvent` 入队。
- F3: 提供 `AgentEvent` sealed interface，覆盖文本流 / 思考流 / 思考完成 / 工具使用 / 工具结果 / 轮次完成 / 循环完成 / 用量 / 错误 / 压缩 / 重试 / 权限请求 / askuser 共 13 种事件 record。
- F4: 主循环按轮迭代：先 drain 通知 supplier 注入 system-reminder，跑 `ContextCompactor.manage`，把 deferred tool 名字以 system-reminder 注入；Plan Mode 下再注入 `PlanModePrompt.buildReminder`。
- F5: 每轮调 `client.stream(conv, tools)` 拿到 `BlockingQueue<StreamEvent>`，用 30 秒 poll 超时消费，把 TextDelta / ThinkingDelta / ThinkingComplete / ToolCallStart / ToolCallComplete / StreamEnd / Error 七类事件转译成 `AgentEvent` 推送给消费者；同时收集 tool_use 列表、用量、stop_reason。
- F6: 工具执行委托给 `StreamingExecutor.executeAll`：按 `ToolCategory.READ` 把 calls 拆成 readCalls / otherCalls 两段，readCalls 数量 >1 时用 `Executors.newVirtualThreadPerTaskExecutor()` 并发跑，其它串行；权限走 `PermissionChecker.check` 决策 ALLOW/ASK/DENY，ASK 通过 `PermissionRequestEvent` 把 `CompletableFuture<PermissionResponse>` 抛给 TUI 等用户回填；执行前后跑 PreToolUse / PostToolUse hook。
- F7: 工具执行完成后调 `conv.addAssistantFull(text, thinking, toolUses)` 写回助手消息，再调 `conv.addToolResultsMessage(results)` 写回工具结果消息；本轮无 tool_use 则推 `TurnComplete` + `LoopComplete` 退出循环。
- F8: 错误恢复：stream Error 含 `context` / `too long` / `prompt` 关键字时调 `ContextCompactor.forceCompact` 并 retry，最多 3 次；含 `rate limit` 时退避 5 秒重试；`max_tokens` stop_reason 首次提升上限到 `MAX_TOKENS_CEILING=64_000` 并续写，最多 `MAX_OUTPUT_RECOVERIES=3` 次拆分续写。
- F9: 工具输出超过 `ToolRegistry.MAX_OUTPUT_CHARS=10_000` 强制截断并追加 `... (truncated)` 标记，保证 tool_result 不撑爆下一轮上下文。

## 4. 非功能需求

- N1: Agent loop 必须跑在虚拟线程上（`Thread.startVirtualThread`），主 TUI 线程靠 `BlockingQueue` poll 实现非阻塞渲染；`Thread.currentThread().isInterrupted()` 命中即退出循环。
- N2: 工具调用分流策略必须严格保证：只读工具可并行（虚拟线程池），写 / 命令类工具一律串行执行，避免相互踩文件。
- N3: stream 消费 poll 超时统一 30 秒，超时直接推 `Stream timeout` 错误并 return，不允许卡住整条循环。
- N4: `AgentEvent` 队列容量 64，`putSafe` 在 InterruptedException 时回写中断标志而不是抛异常，保障 TUI 关停时能干净退出。
- N5: 权限询问的 `CompletableFuture.get` 设 5 分钟超时，超时按 DENY 处理，避免 Agent 永远悬挂。

## 5. 设计概要

- 核心数据结构: `AgentEvent`（sealed interface，13 个 record 实现）、`StreamingExecutor.ToolCallInfo{toolId, toolName, args}` / `ToolExecResult{toolId, output, isError}`、内部 `Agent.ToolCallInfo`（轮内汇聚 tool_use）。
- 主流程:
 1. TUI 选 provider → 构造 `LlmClient` → 构造 `Agent(client, registry, protocol)` → 注入 checker / hook / workDir 等；
 2. 用户输入 → TUI `agent.run(conv)` 拿 queue → 启动 `Command.tick` 轮询；
 3. 每个 `AgentEventMessage` tick 在 model.update 中 drain queue → 转换成 `ChatMessage` 渲染；
 4. 收到 `LoopComplete` / `ErrorEvent` 结束本次会话，恢复 idle。
- 调用链:
 - TUI 用户提问 → `CodeBotModel` 调 `agent.run` → agent virtual thread 开转；
 - 每轮: notification drain → compact → deferred reminder → plan reminder → `client.stream` → 消费 StreamEvent → 收集 toolCalls → `StreamingExecutor.executeAll` → `conv.addAssistantFull` + `addToolResultsMessage`；
 - 工具内含权限决策 → ASK 走 `PermissionRequestEvent` → TUI 弹 dialog → CompletableFuture.complete → executor 继续。
- 与其他模块的交互:
 - 依赖 ch02 的 `LlmClient` / `StreamEvent` / `ConversationManager`、ch03 的 `ToolRegistry` / `Tool` / `ToolCategory` / `ToolResult`、ch06 的 `PermissionChecker` / `PermissionMode` / `PlanFile` / `PlanModePrompt`、ch08 的 `ContextCompactor`、ch12 的 `HookEngine`；
 - 被 `CodeBotModel` 直接调用，输出事件队列由 TUI 渲染。

## 6. Out of Scope

- 不在本章实现 `ContextCompactor` 内部算法（ch08 主题）。
- 不实现 Plan Mode reminder 文案（ch06 主题）。
- 不实现 SubAgent 派遣（ch13 主题）。
- 不实现 hook 引擎本体（ch12 主题）。
- 不做 system prompt 模块化拼装；本章 system prompt 由 `LlmClient.create` 接收的字符串透传，模块化拼装留给后续章节。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch04: Agent Loop Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。本章为验收，所有任务已经在仓库里落地。

## T1: 定义 AgentEvent sealed interface
- 影响文件: `src/main/java/com/codebot/agent/AgentEvent.java:1-39`
- 依赖任务: 无
- 完成标准: `public sealed interface AgentEvent` 包含 13 个 record 实现：`StreamText` / `ThinkingText` / `ThinkingComplete` / `ToolUseEvent` / `ToolResultEvent` / `TurnComplete` / `LoopComplete` / `UsageEvent` / `ErrorEvent` / `CompactEvent` / `RetryEvent` / `PermissionRequestEvent` / `AskUserRequestEvent`；`PermissionRequestEvent` 字段含 `CompletableFuture<PermissionResponse>`（AgentEvent.java:33-34）；`AskUserRequestEvent` 字段含 `CompletableFuture<Map<String, String>>`（AgentEvent.java:36-38）。

## T2: 定义 Agent 类骨架与依赖注入
- 影响文件: `src/main/java/com/codebot/agent/Agent.java:19-48`
- 依赖任务: T1
- 完成标准: 构造方法接 `(LlmClient client, ToolRegistry registry, String protocol)`（Agent.java:35）；`MAX_TOKENS_CEILING=64_000`（Agent.java:21）、`MAX_OUTPUT_RECOVERIES=3`（Agent.java:22）；setter 注入 `PermissionChecker` / `HookEngine` / `maxIterations` / `workDir` / `notificationFn` / `toolNameFilter`（Agent.java:42-47）。

## T3: 实现 run 入口与虚拟线程派发
- 影响文件: `src/main/java/com/codebot/agent/Agent.java:50-60`
- 依赖任务: T2
- 完成标准: `public BlockingQueue<AgentEvent> run(ConversationManager conv)` 返回 `LinkedBlockingQueue<>(64)`；`Thread.startVirtualThread` 启 `agentLoop`，所有 Exception 包成 `AgentEvent.ErrorEvent("Agent error: ...")`（Agent.java:51-58）。

## T4: 实现轮次起手：通知 / compact / deferred / plan 注入
- 影响文件: `src/main/java/com/codebot/agent/Agent.java:62-114`
- 依赖任务: T3
- 完成标准:
 - 主循环 `for (int iteration = 1; ; iteration++)`（Agent.java:69）；
 - `maxIterations` 超限推 `ErrorEvent("Agent reached maximum iterations (%d)")` 退出（Agent.java:70-74）；
 - `Thread.currentThread().isInterrupted()` 命中 break（Agent.java:76）；
 - `notificationFn.get()` drain 后 `conv.addSystemReminder(note)`（Agent.java:79-83）；
 - `ContextCompactor.manage` 非空消息推 `CompactEvent`（Agent.java:87-91）；
 - `registry.getDeferredToolNames()` 非空时拼 reminder 注入（Agent.java:94-104）；
 - Plan Mode 下 `PlanModePrompt.buildReminder` 注入（Agent.java:107-114）。

## T5: 实现 StreamEvent 流消费
- 影响文件: `src/main/java/com/codebot/agent/Agent.java:116-182`
- 依赖任务: T4
- 完成标准:
 - tool list 走 `registry.getAllSchemas(protocol)`，可选 `toolNameFilter` 过滤（Agent.java:117-125）；
 - `client.stream(conv, tools)` 拿 `BlockingQueue<StreamEvent>`（Agent.java:126）；
 - `streamQueue.poll(30, TimeUnit.SECONDS)` 超时推 `Stream timeout` 错误（Agent.java:139, 145-148）；
 - switch pattern matching 七路：`TextDelta` → `StreamText`；`ThinkingDelta` → `ThinkingText`；`ThinkingComplete` 入 `thinkingBlocks` + 转发；`ToolCallStart` / `ToolCallComplete` 转发并把后者入 `toolCalls`；`StreamEnd` 抓 stop_reason 与 token 用量；`Error` 抓 `lastStreamError` 推 `ErrorEvent`（Agent.java:150-179）；
 - `StreamEnd` / `Error` 命中跳出消费循环（Agent.java:181）。

## T6: 实现错误恢复（context / rate-limit / max_tokens）
- 影响文件: `src/main/java/com/codebot/agent/Agent.java:184-233`
- 依赖任务: T5
- 完成标准:
 - stream 错误 + 错误文本含 `context` / `too long` / `prompt` → `contextRetries < 3` 时 `ContextCompactor.forceCompact` 后 `RetryEvent("Context too long, compacting...", 0)` continue（Agent.java:186-196）；
 - 错误文本含 `rate limit` → 推 `RetryEvent("Rate limited, waiting 5s...", 5000)`，`Thread.sleep(5000)` 后 continue（Agent.java:197-201）；
 - stop_reason `max_tokens` 首次：`AnthropicClient.setMaxOutputTokens(MAX_TOKENS_CEILING)` + 写助手已生成内容 + user "Output token limit hit. Resume directly from where you stopped..." + `RetryEvent("max_tokens escalation", 0)` continue（Agent.java:210-221）；
 - `maxTokensEscalated` 后再次命中：`outputRecoveries < MAX_OUTPUT_RECOVERIES` 时写助手 + user "Break remaining work into smaller pieces." + 计数器自增 continue（Agent.java:222-229）。

## T7: 实现工具调用收尾与会话写回
- 影响文件: `src/main/java/com/codebot/agent/Agent.java:235-263`
- 依赖任务: T5
- 完成标准:
 - `conv.addAssistantFull(text, thinkingBlocks, toolUseBlocks)` 写回助手（Agent.java:236-239）；
 - 无 tool_call → 推 `TurnComplete(iteration)` + `LoopComplete(iteration)` 后 break（Agent.java:242-246）；
 - 有 tool_call → `new StreamingExecutor(...).executeAll(callInfos)` 拿结果（Agent.java:249-253）；
 - `conv.addToolResultsMessage(resultBlocks)` 写回（Agent.java:256-259）；
 - 末尾推 `TurnComplete(iteration)`（Agent.java:261）。

## T8: 实现 StreamingExecutor 分流并发
- 影响文件: `src/main/java/com/codebot/agent/StreamingExecutor.java:23-72`
- 依赖任务: T1
- 完成标准:
 - 按 `ToolCategory.READ` 拆 `readCalls` / `otherCalls`（StreamingExecutor.java:42-51）；
 - readCalls `> 1` 时 `Executors.newVirtualThreadPerTaskExecutor()` 并发跑 `executeSingle` 收集 future（StreamingExecutor.java:55-64）；
 - readCalls `<= 1` 串行（StreamingExecutor.java:65-67）；
 - otherCalls 全部串行（StreamingExecutor.java:69）。

## T9: 实现 StreamingExecutor 单次执行（hook / 权限 / 截断）
- 影响文件: `src/main/java/com/codebot/agent/StreamingExecutor.java:74-149`
- 依赖任务: T8
- 完成标准:
 - 未知工具直接 `Unknown tool` 错误（StreamingExecutor.java:75-79）；
 - PreToolUse hook rejected 时 `Rejected by hook: ...` 错误（StreamingExecutor.java:82-89）；
 - 权限决策三分支：DENY 直接 `Permission denied: ...`；ASK 推 `PermissionRequestEvent` + `future.get(5, MINUTES)`，超时按 DENY；`ALLOW_ALWAYS` 调 `checker.addAllowAlwaysRule(toolName, extractContent(...))`（StreamingExecutor.java:91-122）；
 - `tool.execute(args)` 计 elapsed 秒 + 输出超 `MAX_OUTPUT_CHARS=10_000` 截断追加 `... (truncated)`（StreamingExecutor.java:125-137）；
 - 推 `ToolResultEvent(toolId, toolName, output, isError, elapsed)` + 跑 PostToolUse hook（StreamingExecutor.java:139-145）。

## T10: 接入主流程（TUI / CodeBotModel）
- 影响文件:
 - `src/main/java/com/codebot/tui/CodeBotModel.java:432-438` 构造 `new Agent(client, registry, protocol)` + 注入依赖
 - `src/main/java/com/codebot/tui/CodeBotModel.java:952` / `:1028` 调 `agent.run(conversation)` 拿 queue
 - `src/main/java/com/codebot/CodeBot.java:14` `main` 启动 `Program(model).run()` 跑 TUI
- 依赖任务: T1~T9
- 完成标准: TUI 收到用户输入 → `agent.run` → `Command.tick(POLL_INTERVAL, ...)` 周期 drain queue → 把 `AgentEvent` 映射成 `ChatMessage` 渲染。

## T11: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T10
- 完成标准:
 - `./gradlew build` 通过；
 - `./gradlew test` 通过（现有测试集 `src/test/java/com/codebot/teams/FileMailBoxTest.java` / `src/test/java/com/codebot/tool/ToolSearchTest.java`，无 Agent 直接单测，本章靠手动 TUI 演练验收）；
 - TUI 启动 → 提问 `读一下 README.md` → 队列中能依序观察到 `StreamText` / `ToolUseEvent(ReadFile)` / `ToolResultEvent` / `TurnComplete` / `LoopComplete`。

## 进度
- [ ] T1 AgentEvent sealed interface
- [ ] T2 Agent 骨架 + DI
- [ ] T3 run 入口 + 虚拟线程
- [ ] T4 轮次起手注入
- [ ] T5 StreamEvent 消费
- [ ] T6 错误恢复
- [ ] T7 工具调用收尾
- [ ] T8 StreamingExecutor 分流
- [ ] T9 StreamingExecutor 单次执行
- [ ] T10 TUI 接入
- [ ] T11 端到端验证

```

```markdown
# ch04: Agent Loop Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性
- [ ] `AgentEvent` sealed interface 在 `src/main/java/com/codebot/agent/AgentEvent.java:8`，13 个 record 实现齐全（`grep -nE "record [A-Z][A-Za-z]+\(" src/main/java/com/codebot/agent/AgentEvent.java` 返回 13 条）
- [ ] `AgentEvent.PermissionRequestEvent` 在 AgentEvent.java:33-34 含 `CompletableFuture<PermissionResponse>` 字段
- [ ] `Agent` 类在 `src/main/java/com/codebot/agent/Agent.java:19`，常量 `MAX_TOKENS_CEILING=64_000`（Agent.java:21）、`MAX_OUTPUT_RECOVERIES=3`（Agent.java:22）
- [ ] `Agent.run` 在 Agent.java:50 返回 `LinkedBlockingQueue<>(64)`，`Thread.startVirtualThread` 在 Agent.java:52
- [ ] `Agent.agentLoop` 在 Agent.java:62，主循环 `for (int iteration = 1; ; iteration++)` 在 Agent.java:69
- [ ] 通知 drain + `conv.addSystemReminder` 在 Agent.java:79-83
- [ ] `ContextCompactor.manage` 调用在 Agent.java:87，`CompactEvent` 推送在 Agent.java:89
- [ ] deferred tool reminder 注入在 Agent.java:94-104，调 `registry.getDeferredToolNames()`
- [ ] Plan Mode reminder 注入在 Agent.java:107-114，调 `PlanModePrompt.buildReminder`
- [ ] `client.stream(conv, tools)` 调用在 Agent.java:126，`streamQueue.poll(30, SECONDS)` 在 Agent.java:139
- [ ] StreamEvent 七路 switch pattern matching 在 Agent.java:150-179，覆盖 `TextDelta` / `ThinkingDelta` / `ThinkingComplete` / `ToolCallStart` / `ToolCallDelta` / `ToolCallComplete` / `StreamEnd` / `Error`
- [ ] 错误恢复三分支：context 在 Agent.java:186-196，rate limit 在 Agent.java:197-201，max_tokens 在 Agent.java:210-229
- [ ] `conv.addAssistantFull` 在 Agent.java:239，`conv.addToolResultsMessage` 在 Agent.java:259
- [ ] 无 tool_use 收尾：`TurnComplete` + `LoopComplete` 在 Agent.java:243-245
- [ ] `StreamingExecutor` 在 `src/main/java/com/codebot/agent/StreamingExecutor.java:23`
- [ ] 读 / 写分流在 StreamingExecutor.java:42-51，虚拟线程并发在 StreamingExecutor.java:55-64（`Executors.newVirtualThreadPerTaskExecutor()`）
- [ ] 权限 ASK 分支用 `CompletableFuture<PermissionResponse>` + 5 分钟超时在 StreamingExecutor.java:99-108
- [ ] 工具输出截断 `MAX_OUTPUT_CHARS=10_000` 在 StreamingExecutor.java:135-137（`ToolRegistry.MAX_OUTPUT_CHARS` 定义在 `src/main/java/com/codebot/tool/ToolRegistry.java:7`）

## 2. 接入完整性（必查，杜绝死代码）
- [ ] `grep -rn "new Agent(" --include="*.java" src/main/java` 返回 ≥ 1 处真实调用（`src/main/java/com/codebot/tui/CodeBotModel.java:432`）
- [ ] `grep -rn "agent.run(" --include="*.java" src/main/java` 返回 ≥ 2 处（`CodeBotModel.java:952`、`CodeBotModel.java:1028`）
- [ ] `grep -rn "new StreamingExecutor" --include="*.java" src/main/java` 返回 ≥ 1 处（`Agent.java:249`）
- [ ] `grep -rn "BlockingQueue<AgentEvent>" --include="*.java" src/main/java` 返回 ≥ 3 处（Agent.run、StreamingExecutor 构造、CodeBotModel 接收）
- [ ] TUI 调用链：用户提问 → `CodeBotModel.update` 收到 `UserInputMsg` → `agent.run(conversation)`（CodeBotModel.java:952/1028）→ `Command.tick(POLL_INTERVAL, t -> new AgentEventMessage())` 周期 drain queue
- [ ] Agent 调用链：每轮 → 通知注入（Agent.java:79）→ compact（:87）→ deferred reminder（:94）→ plan reminder（:107）→ `client.stream`（:126）→ `StreamingExecutor.executeAll`（Agent.java:253 / StreamingExecutor.java:41）→ 写回会话（:239/:259）

## 3. 编译与测试
- [ ] `./gradlew build` 通过（顶层命令验证）
- [ ] `./gradlew test` 通过（现有测试集仅 `FileMailBoxTest` / `ToolSearchTest`，无 Agent 直接单测，靠 TUI 端到端验收）
- [ ] `./gradlew compileJava` 无 unchecked / preview 警告（pattern matching for switch 在 Java 21+ 已 GA）

## 4. 端到端验证
- [ ] TUI 启动 → 选 provider → 提问 `读一下 README.md` → 队列中依序观察到 `StreamText` / `ToolUseEvent(toolName="ReadFile")` / `ToolResultEvent(isError=false)` / `TurnComplete` / `LoopComplete`
- [ ] 多读连发：提问 `同时读 README.md 和 build.gradle.kts`，观察 `StreamingExecutor` 走并发分支（两个 `ReadFile` ToolResultEvent 几乎同时到达，elapsed 接近）
- [ ] 权限 ASK 流程：在 ACCEPT_EDITS 模式下让 Agent 跑 `Bash`，观察 `PermissionRequestEvent` 弹 dialog → 用户 ALLOW → 工具继续执行
- [ ] max_tokens 恢复：构造长输出任务，观察 `RetryEvent("max_tokens escalation", 0)` 后助手续写到完整答案
- [ ] context 超限恢复：手工塞超长对话历史触发 `Error("context too long")` → `RetryEvent("Context too long, compacting...", 0)` → `ContextCompactor.forceCompact` 后继续
- [ ] 留存证据：验收阶段未保存日志；若要补，可在 TUI 输入指定提问后保存 `AgentEvent` 队列 trace

## 5. 文档
- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`docs/java/ch04/`）
- [ ] commit 信息标注 `ch04` 与三件套关闭状态（待统一打包提交）

```



## ch05

```markdown
# 我的初步想法
- 把全局指令按职责拆成多个模块（身份、行为、工具使用、代码规范、安全边界、任务模式、输出风格），用优先级排序的方式拼装，便于后续章节插入新模块。
- 区分稳定内容和变化内容：稳定的全局指令和工具描述走可缓存通道，变化的环境信息、对话历史、动态补充走对话通道。
- 把环境信息（工作目录、操作系统、时间、Git 状态等）从全局指令里搬出来，作为对话首条系统级补充消息，避免环境每次变化都让缓存失效。
- 在工具自身描述和全局指令里双重强化关键规则,覆盖模型的默认偏好(例如优先调用专用工具而不是通用 shell 命令、编辑前必须先读)。
- 引入一种带特殊标签的对话消息形式,在运行中向模型注入补充指令(外部工具上线、当前模式提醒、温和提示),既不污染缓存也不会被模型当作用户输入回复。
- 把会话级开关功能(如规划模式)的指令从全局指令里拆出来按轮次动态注入,用首轮完整、间隔轮次重复完整、其余轮次精简的节奏控制注入频率。
- 通过解析 API 返回的缓存命中字段验证缓存策略是否真的生效;准备一组典型行为场景做人工对比,作为本章的定性评估手段。
```

### Go

```markdown
# ch05: System Prompt 设计 Spec

## 1. 背景

没有 System Prompt，模型并不知道自己叫 CodeBot、不知道运行在什么 OS、不知道有哪些工具能用、不知道用户的代码规范，输出会落到「通用 ChatGPT 助手」基线。所有静态规则（语气、安全、工具使用规范）和环境信息必须固化到 System Prompt 才能让模型回答稳定、可预期；动态指令（Plan Mode reminder、Task notification、Skill 拉取）则走 user channel 的 `<system-reminder>` 块，避免反复改 System 失效缓存。本章把这条 prompt 拼接管线做出来。

## 2. 目标

对外提供 `internal/prompt`：调用者准备好工作目录、模型名、（可选）项目说明 / Skill 列表 / Memory 段，调一次 `BuildSystemPrompt(env, opts)` 拿到能直接喂给 LLM 客户端的纯文本 System Prompt。多个信息来源（角色、行为准则、工具规范、tone、文本输出风格、环境上下文、项目说明、Skill 摘要、Memory）按优先级合并；动态注入走 `conversation.Manager.AddSystemReminder` + ch04 主循环。

## 3. 功能需求

- F1: 提供环境探测函数，输出工作目录、OS、Arch、Shell、是否 Git 仓库、当前分支、模型名、日期等字段；Git 状态用标准命令探测，非 Git 仓库静默降级。
- F2: 提供 `BuildSystemPrompt(env, opts)` 主入口，装配 8 个固定 section（Identity / System / DoingTasks / ExecutingActions / UsingTools / ToneStyle / TextOutput / Environment）外加 3 个可选 section（CustomInstructions / Skills / Memory），按优先级排序后拼接。
- F3: `BuildOptions` 接收项目说明 / Skill 摘要 / Memory 三类可选字符串；空字符串不进入最终输出。
- F4: 提供 `Builder` + `Section` 类型支持自定义扩展：调用者可空 builder 起步、自由添加 section、指定优先级，最后 `Build()` 排序输出。
- F5: 各 section 有固定优先级（Identity 最高、Memory 最低，可选 section 排在固定 section 之后），保证最终 prompt 顺序稳定。
- F6: Plan Mode 系统提醒不进入 System Prompt，由 `internal/prompt/plan_mode.go` 提供构造函数，由 ch04 主循环通过 `AddSystemReminder` 注入 user channel。
- F7: 各 section 文案需保持与终端 Agent 系统提示语义一致：禁用 emoji、优先用专用工具、文件路径引用用 `file_path:line_number`、状态报告诚实、对潜在 prompt injection 进行 flag、`<system-reminder>` 与具体 tool 结果无直接关系等关键短语保留。

## 4. 非功能需求

- N1: System Prompt 内容必须能被 LLM 长缓存命中——只在切 provider / 工作目录 / Skill / Memory 时重建，每轮迭代不重新构建。
- N2: 环境探测在 Git 不存在时静默降级，不输出错误日志。
- N3: 日期字段使用稳定格式（年-月-日），跨进程一致。
- N4: section 之间用恰好两个换行分隔，section 内部用单换行；空 section 不出现。
- N5: 文案不使用 emoji（除非用户在 ToneStyle section 内显式说明）。

## 5. 设计概要

- 核心数据结构: `Section{Name, Priority, Content}`、`EnvironmentContext`（含 WorkDir / OS / Arch / Shell / IsGitRepo / GitBranch / Model / Date）、`BuildOptions`（CustomInstructions / SkillSection / MemorySection）、`Builder{sections []Section}`。
- 主流程:
 1. TUI 选好 provider → 调 memory 模块加载 `AGENTS.md` / `CODEBOT.md` 合并文本；
 2. 调 `prompt.DetectEnvironment(wd)` 并填上模型名；
 3. 调 `prompt.BuildSystemPrompt(env, opts)` 拼出 system prompt；
 4. system prompt 喂给 `llm.NewClient` 作为第二参数。
 - `BuildSystemPrompt` 内部依次添加 8 固定 section + 3 可选 section，最后排序拼接。
- 调用链:
 - TUI 选 / 切 provider 时调 `BuildSystemPrompt`，输出作为 LLM 客户端构造参数。
 - 单测与 live 测试用同一入口确保行为一致。
 - 动态注入：Agent 主循环在 Plan Mode 时调 `BuildPlanModeReminder` → `conv.AddSystemReminder`，最终包成 `<system-reminder>` user 消息。
- 与其他模块的交互:
 - 依赖 Go 标准库（os / os/exec / runtime / time），不依赖项目其他模块。
 - 被 `internal/tui`（构造 prompt）、`internal/agent`（live / unit test、Plan Mode reminder 注入）使用。
 - 输入数据由 `internal/memory` 等模块准备好后传入。

## 6. Out of Scope

- Coordinator Mode / 自定义 Agent 角色的 system prompt 替换分支不在本章实现，所有 Agent 共用默认 prompt。
- 不缓存 section 输出。
- Plan Mode Reentry / Exit 提醒函数已写但未接入 TUI，留给下章或专门 PR。
- 不实现外部 `--system-prompt` / `appendSystemPrompt` CLI 参数。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch05: System Prompt 设计 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。本章为验收，所有任务已经在仓库里落地。

## T1: 定义 Section / Builder / BuildOptions 数据结构
- 影响文件: `internal/prompt/builder.go:13-42`
- 依赖任务: 无
- 完成标准: `Section{Name string, Priority int, Content string}` / `EnvironmentContext{WorkDir/OS/Arch/Shell/IsGitRepo/GitBranch/Model/Date}` / `BuildOptions{CustomInstructions, SkillSection, MemorySection}` 全部定义；`Builder.Add` 返回 `*Builder` 支持链式调用。

## T2: 实现 DetectEnvironment
- 影响文件: `internal/prompt/builder.go:64-85`
- 依赖任务: T1
- 完成标准: 工作目录、`runtime.GOOS`、`runtime.GOARCH`、`SHELL` 环境变量（缺省 `bash`）、`time.Now().Format("2006-01-02")` 入填；git 检测使用 `git -C wd rev-parse --is-inside-work-tree` 静默判断，是 git repo 再跑 `--abbrev-ref HEAD` 拿到 branch。

## T3: 实现 Builder.Build 排序 + 拼接
- 影响文件: `internal/prompt/builder.go:49-62`
- 依赖任务: T1
- 完成标准: `Build` 用 `sort.Slice` 按 `Priority` 升序排；trim 后空 content 不进入 parts；`strings.Join(parts, "\n\n")` 输出最终文本。

## T4: 实现 8 个固定 section 函数
- 影响文件: `internal/prompt/sections.go`
- 依赖任务: T1
- 完成标准:
 - `IdentitySection`（priority 0，sections.go:5）—— CodeBot 身份 + 安全 / URL 不乱造
 - `SystemSection`（priority 10，sections.go:16）—— `<system-reminder>` 语义、prompt injection 警告、hook feedback、自动 compact
 - `DoingTasksSection`（priority 20，sections.go:30）—— 不做未读过的代码、最小修改原则、不写无用注释、报真实结果
 - `ExecutingActionsSection`（priority 30，sections.go:52）—— 高破坏性操作需 confirm
 - `UsingToolsSection`（priority 40，sections.go:69）—— Tool 优先 / TaskCreate / 并行调用 / Agent / ToolSearch
 - `ToneStyleSection`（priority 50，sections.go:93）—— 不用 emoji / 简短 / 用 `file_path:line_number` / 工具调用前别打冒号
 - `OutputEfficiencySection`（priority 60，sections.go:105）—— 输出文本一句话规划，少注释，end-of-turn summary
 - `EnvironmentSection`（priority 70，sections.go:123）—— 把 `EnvironmentContext` 渲染成 5~8 行环境信息块

## T5: 实现 BuildSystemPrompt 主入口
- 影响文件: `internal/prompt/builder.go:87-124`
- 依赖任务: T2, T3, T4
- 完成标准: 先 Add 8 个固定 section，再依据 `opts.CustomInstructions`（priority 80） / `opts.SkillSection`（priority 90） / `opts.MemorySection`（priority 95）按需 Add；空字符串不 Add。

## T6: 实现 Plan Mode 动态指令
- 影响文件: `internal/prompt/plan_mode.go`
- 依赖任务: 无
- 完成标准: `planModeFullReminder`（plan_mode.go:5）+ `planModeSparseReminder`（plan_mode.go:63）+ `planModeReentryReminder`（plan_mode.go:65）+ `planModeExitReminder`（plan_mode.go:79）四段模板；`reminderInterval=5`；`BuildPlanModeReminder(planFilePath, planExists, iteration)` 在 iteration==1 给完整版，否则按 5 次为周期间断重发完整版，其余给稀疏版；`BuildPlanModeReentryReminder` / `BuildPlanModeExitReminder` 已实现但 TUI 当前未调用（保留作为 ch+ 接入点）。

## T7: 接入主流程（TUI / Agent）
- 影响文件:
 - `internal/tui/tui.go:634prompt.DetectEnvironment(wd)`
 - `internal/tui/tui.go:639-643prompt.BuildSystemPrompt(env, BuildOptions{...})`
 - `internal/tui/tui.go:713` 把 system prompt 喂给 `llm.NewClient`
 - `internal/agent/agent.go:91reminder := prompt.BuildPlanModeReminder(planPath, planExists, iteration)`
- 依赖任务: T1~T6
- 完成标准: TUI 选 provider 阶段一次性构造 System Prompt；Agent Run 主循环在 ModePlan 下每轮调 `BuildPlanModeReminder` 并写入 `conv.AddSystemReminder`，最终走 user 通道的 `<system-reminder>` 块（`conversation/conversation.go:93`）。

## T8: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T7
- 完成标准:
 - `go build ./...` 通过（顶层命令验证）。
 - `go test ./internal/agent/...`：`agent_test.go:142` 通过 `prompt.BuildSystemPrompt` + `agent_live_test.go:347-348` 在 live 测试里走 detect+build 全链路；live 测试可直接 `go test -run TestLiveSimpleChat`（需 API key）。
 - 在 TUI 启动后 `/plan` 进入计划模式，下一轮 agent stream 注入完整版 Plan Mode reminder（5 阶段 Workflow 文本可在 TUI 的请求日志中观察到）。

## 进度
- [ ] T1 数据结构
- [ ] T2 DetectEnvironment
- [ ] T3 Builder.Build
- [ ] T4 8 个固定 section 函数
- [ ] T5 BuildSystemPrompt 主入口
- [ ] T6 Plan Mode 动态指令
- [ ] T7 TUI / Agent 接入
- [ ] T8 端到端验证（编译通过 + agent_test.go:142 单测调用通过 + Plan reminder 在 agent.go:91 接入）

```

```markdown
# ch05: System Prompt 设计 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性
- [ ] 数据结构 `Section{Name, Priority, Content}` 在 `internal/prompt/builder.go:13-17`（`grep -n "type Section struct" internal/prompt/builder.go`）
- [ ] 数据结构 `EnvironmentContext` 8 字段在 `internal/prompt/builder.go:19-28`
- [ ] 数据结构 `BuildOptions{CustomInstructions, SkillSection, MemorySection}` 在 `internal/prompt/builder.go:30-34`
- [ ] 函数 `DetectEnvironment` 在 `internal/prompt/builder.go:64`，git 探测在 builder.go:77-82，shell 缺省 bash 在 builder.go:73
- [ ] 函数 `BuildSystemPrompt` 在 `internal/prompt/builder.go:87`，按八段 + 三可选段顺序 Add
- [ ] 8 个固定 section 函数：`IdentitySection`(sections.go:5) / `SystemSection`(:16) / `DoingTasksSection`(:30) / `ExecutingActionsSection`(:52) / `UsingToolsSection`(:69) / `ToneStyleSection`(:93) / `OutputEfficiencySection`(:105) / `EnvironmentSection`(:123)
- [ ] Priority 数字固定：0/10/20/30/40/50/60/70，对应 8 个 section（`grep -n "Priority:" internal/prompt/sections.go` 返回 8 条）
- [ ] 可选 section Priority 数字：80 / 90 / 95（CustomInstructions / Skills / Memory，builder.go:102/110/118）
- [ ] Plan Mode 动态指令：`BuildPlanModeReminder` 在 `internal/prompt/plan_mode.go:85`；`reminderInterval=5` 在 plan_mode.go:83
- [ ] 关键文本片段保留：`Build()` 输出含 `IMPORTANT: Be careful not to introduce security` / `<system-reminder>` / `Only use emojis if the user explicitly requests it` / `file_path:line_number`（可通过测试或 `grep -n` 验证 sections.go）

## 2. 接入完整性（必查，杜绝死代码）
- [ ] `grep -rn "prompt.BuildSystemPrompt" --include="*.go"` 返回至少 3 处真实调用（`internal/tui/tui.go:639`、`internal/agent/agent_test.go:142`、`internal/agent/agent_live_test.go:348`）
- [ ] `grep -rn "prompt.DetectEnvironment" --include="*.go"` 返回至少 3 处（`internal/tui/tui.go:634`、`internal/agent/agent_test.go:141`、`internal/agent/agent_live_test.go:347`）
- [ ] `grep -rn "prompt.BuildPlanModeReminder" --include="*.go"` 返回 ≥ 1 个主流程调用（`internal/agent/agent.go:91`）
- [ ] TUI 调用链：用户选 provider → `loadSkillsAndBuildPrompt`（tui.go:625）→ `BuildSystemPrompt`（tui.go:639）→ `llm.NewClient`（tui.go:714）
- [ ] Agent 调用链：每轮迭代 → `agent.go:88-93` 判断 `ModePlan` → 调 `prompt.BuildPlanModeReminder` → `conv.AddSystemReminder`
- [ ] 已记录死代码（不在本章 must-fix）:
 - [ ] `internal/prompt/plan_mode.go:105 BuildPlanModeReentryReminder` 无调用方（`grep -rn "BuildPlanModeReentryReminder" --include="*.go"` 只返回定义点）
 - [ ] `internal/prompt/plan_mode.go:109 BuildPlanModeExitReminder` 无调用方（同上）
 - 处理意见: 已抄自目标设计；TUI 当前 `/do` 命令未注入 exit reminder。记录已知；后续如要补，调用点应放在 `internal/tui/tui.go:1228` 附近

## 3. 编译与测试
- [ ] `go build ./...` 通过（顶层命令验证；本次验收已跑）
- [ ] `go test ./internal/agent/... -run TestAgentSimpleResponse` 通过；该测试在 `agent_test.go:141` 调用 `prompt.BuildSystemPrompt` 验证不 panic
- [ ] `go vet ./internal/prompt/...` 无警告（本次未独立运行，验收人员补跑即可）

## 4. 端到端验证
- [ ] TUI 启动 → 选 provider → `BuildSystemPrompt` 一次 → `llm.NewClient` 拿到 system prompt（`internal/tui/tui.go:639-714`）
- [ ] `/plan` 命令进入 Plan Mode → Agent Run 下一轮在 stream 之前注入 `<system-reminder>` 包裹的 5 阶段 Workflow（`internal/agent/agent.go:87-93` + `internal/conversation/conversation.go:93`）
- [ ] 留存证据: 验收阶段未保存日志；若要补，可在 TUI 输入 `/plan` 后看一次请求 body 中的 user `<system-reminder>` 内容

## 5. 文档
- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`specs/go/ch05/`）
- [ ] commit 信息标注 `ch05` 与三件套关闭状态（待统一打包提交）

```

### Python

```markdown
# ch05: System Prompt 设计 Spec

## 1. 背景

没有 System Prompt，模型并不知道自己叫 CodeBot、不知道运行在什么 OS、不知道有哪些工具能用、不知道用户的代码规范，输出会落到「通用 ChatGPT 助手」基线。所有静态规则（语气、安全、工具使用规范）和环境信息必须固化到 System Prompt 才能让模型回答稳定、可预期；动态指令（Plan Mode reminder、Hook 注入、Skill 拉取）则走 user channel 的 `<system-reminder>` 块或 `inject_environment`，避免反复改 System 失效缓存。本章把这条 prompt 拼接管线做出来。

## 2. 目标

对外提供 `codebot.prompts`：调用者准备好工作目录、（可选）custom_instructions / skill_section / memory_section / hook_prompts，调一次 `build_system_prompt(...)` 拿到能直接喂给 LLM 客户端的纯文本 System Prompt。多个信息来源（角色、行为准则、工具规范、tone、文本输出风格、环境上下文、项目说明、Skill 摘要、Memory）按优先级合并；环境上下文由 `build_environment_context` 单独构造并通过 `ConversationManager.inject_environment` 注入 user channel；动态注入走 `ConversationManager.add_system_reminder` + ch04 主循环。

## 3. 功能需求

- F1: 提供 `environment_section(work_dir)` 构造环境 section，输出工作目录、`platform.system()` / `platform.release()`、`datetime.now().strftime('%Y-%m-%d')` 字段，作为 System Prompt 的 priority=70 段。
- F2: 提供 `build_system_prompt(hook_prompts, coordinator_mode, agent_catalog, custom_instructions, skill_section, memory_section, work_dir)` 主入口，装配 8 个固定 section（Identity / System / DoingTasks / ExecutingActions / UsingTools / ToneStyle / TextOutput / Environment）外加 3 个可选 section（CustomInstructions / Skills / Memory），按优先级排序后拼接。
- F3: `build_system_prompt` 接收 `custom_instructions` / `skill_section` / `memory_section` 三类可选字符串；空字符串不进入最终输出；`hook_prompts` 非空时尾部追加 `# Hook Injected Context` 段。
- F4: 提供 `PromptBuilder` + `PromptSection` 数据类支持自定义扩展：调用者可空 builder 起步、自由 `add(...)` section、指定 priority，最后 `build()` 排序输出；`add` 返回 `self` 支持链式调用。
- F5: 各 section 有固定优先级（Identity=0、System=10、DoingTasks=20、ExecutingActions=30、UsingTools=40、ToneStyle=50、TextOutput=60、Environment=70，CustomInstructions=80、Skills=90、Memory=95），保证最终 prompt 顺序稳定。
- F6: Plan Mode 系统提醒不进入 System Prompt，由 `build_plan_mode_reminder(plan_path, plan_exists, iteration)` 构造，由 ch04 主循环通过 `conversation.add_system_reminder(...)` 注入 user channel，最终包成 `<system-reminder>` user 消息。
- F7: 各 section 文案需保持与终端 Agent 系统提示语义一致：禁用 emoji、优先用专用工具（ReadFile/EditFile/WriteFile/Glob/Grep）、文件路径引用用 `file_path:line_number`、状态报告诚实、对潜在 prompt injection 进行 flag、`<system-reminder>` 与具体 tool 结果无直接关系、tool 调用前别打冒号等关键短语保留。
- F8: 提供 `build_environment_context(work_dir, active_skills, skill_catalog, agent_catalog)` 单独构造 user 通道环境块（工作目录、操作系统、时间，含 skill_catalog / agent_catalog / Active Skills），供 `conversation.inject_environment` 使用，与 System Prompt 中的 Environment section 互补。

## 4. 非功能需求

- N1: System Prompt 内容必须能被 LLM 长缓存命中——只在切 provider / 工作目录 / Skill / Memory 时重建，每轮迭代不重新构建（每轮调一次是当前实现，可后续做缓存）。
- N2: 环境探测在缺失字段时静默降级（Python 的 `platform.system()` 在容器或不识别 OS 时仍返回字符串，不抛异常）。
- N3: 日期字段使用稳定格式（`%Y-%m-%d`），跨进程一致。
- N4: section 之间用恰好两个换行分隔，section 内部用单换行；trim 后为空的 section 不出现在输出里。
- N5: 文案不使用 emoji（除非用户在 ToneStyle section 内显式说明）。

## 5. 设计概要

- 核心数据结构: `PromptSection(name: str, priority: int, content: str)` dataclass（`codebot/prompts.py:10-15`）、`PromptBuilder._sections: list[PromptSection]`（`codebot/prompts.py:17-28`），无独立 `EnvironmentContext` 类，环境字段直接由 `environment_section(work_dir)` 渲染。
- 主流程:
 1. Agent.run 启动 → 调 `build_environment_context(work_dir, active_skills, skill_catalog, agent_catalog)` 并通过 `conversation.inject_environment(env_context)` 注入 user channel；
 2. 每轮迭代前调 `build_system_prompt(hook_prompts=..., coordinator_mode=..., agent_catalog=...)` 拼出 system prompt；
 3. system prompt 作为 `system` 参数传给 LLM 客户端（Anthropic / OpenAI 等）；
 4. Plan Mode 下额外调 `build_plan_mode_reminder(plan_path, plan_exists, iteration)` 并 `conversation.add_system_reminder(plan_reminder)`。
 - `build_system_prompt` 内部依次添加 8 固定 section + 3 可选 section，最后 `PromptBuilder.build()` 排序拼接。
- 调用链:
 - `codebot/agent.py:469` `Agent.run` 主循环每轮迭代调 `build_system_prompt`。
 - `codebot/agent.py:935` `Agent.run_to_completion` 单轮入口也调 `build_system_prompt`。
 - `codebot/agent.py:399` 和 `:898` `:918` 三处调 `build_environment_context`（启动、压缩后、run_to_completion）。
 - `codebot/agent.py:480` Plan Mode 下调 `build_plan_mode_reminder`。
 - `tests/test_agent.py:483/489/495/500` 四个单测覆盖 normal / plan / sparse / environment 四种路径。
- 与其他模块的交互:
 - 依赖 Python 标准库（`platform` / `datetime` / `dataclasses`），coordinator_mode 时动态导入 `codebot.teams.coordinator`。
 - 被 `codebot.agent`（构造 prompt、注入 environment、Plan Mode reminder）使用。
 - 输入数据由 `codebot.memory` / `codebot.skills` / `codebot.teams` 等模块准备好后传入。

## 6. Out of Scope

- Coordinator Mode 的 system prompt 替换分支已实现（`build_system_prompt` 内 `coordinator_mode=True` 委托给 `codebot.teams.coordinator.get_coordinator_system_prompt`），但 coordinator 角色专有规则不在本章 spec 范围内详述。
- 不缓存 section 输出。
- Plan Mode Reentry / Exit 提醒函数 Python 版本未实现（Go 有 `BuildPlanModeReentryReminder` / `BuildPlanModeExitReminder` 死代码，Python 直接跳过这两个函数）。
- 不实现外部 `--system-prompt` / `appendSystemPrompt` CLI 参数。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch05: System Prompt 设计 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。本章为验收，所有任务已经在仓库里落地（`origin/python` 分支）。

## T1: 定义 PromptSection / PromptBuilder 数据结构
- 影响文件: `codebot/prompts.py:10-28`
- 依赖任务: 无
- 完成标准: `@dataclass class PromptSection` 含 `name: str / priority: int / content: str`（`codebot/prompts.py:10-15`）；`PromptBuilder.__init__` 维护 `self._sections: list[PromptSection]`（`codebot/prompts.py:17-19`）；`PromptBuilder.add(section)` 返回 `PromptBuilder` 自身支持链式调用（`codebot/prompts.py:21-23`）。

## T2: 实现 PromptBuilder.build 排序 + 拼接
- 影响文件: `codebot/prompts.py:25-28`
- 依赖任务: T1
- 完成标准: `build()` 用 `self._sections.sort(key=lambda s: s.priority)` 按 priority 升序排（`codebot/prompts.py:26`）；trim 后空 content 不进入 parts；用 `"\n\n".join(parts)` 输出最终文本（`codebot/prompts.py:27-28`）。

## T3: 实现 environment_section 工厂函数
- 影响文件: `codebot/prompts.py:147-154`
- 依赖任务: T1
- 完成标准: `environment_section(work_dir: str) -> PromptSection` 把 4 行 markdown 拼成 content（`# Environment` + Working directory + Platform + Date），用 `platform.system()` / `platform.release()` 拿 OS，用 `datetime.now().strftime('%Y-%m-%d')` 拿日期；返回 `PromptSection(name="Environment", priority=70, ...)`。

## T4: 实现 7 个固定文本 section 模块常量
- 影响文件: `codebot/prompts.py:35-145`
- 依赖任务: T1
- 完成标准:
 - `IDENTITY_SECTION`（priority=0，`prompts.py:35-48`）—— CodeBot 身份 + 安全 / URL 不乱造
 - `SYSTEM_SECTION`（priority=10，`prompts.py:50-61`）—— `<system-reminder>` 语义、prompt injection 警告、hook feedback、自动 compact
 - `DOING_TASKS_SECTION`（priority=20，`prompts.py:63-82`）—— 不做未读过的代码、最小修改原则、不写无用注释、报真实结果
 - `EXECUTING_ACTIONS_SECTION`（priority=30，`prompts.py:84-98`）—— 高破坏性操作需 confirm
 - `USING_TOOLS_SECTION`（priority=40，`prompts.py:100-116`）—— ReadFile/EditFile/WriteFile/Glob/Grep 优先 / 并行调用 / Agent / ToolSearch
 - `TONE_STYLE_SECTION`（priority=50，`prompts.py:118-127`）—— 不用 emoji / 简短 / 用 `file_path:line_number` / 工具调用前别打冒号
 - `TEXT_OUTPUT_SECTION`（priority=60，`prompts.py:129-145`）—— 输出文本一句话规划，少注释，end-of-turn summary

## T5: 实现 build_system_prompt 主入口
- 影响文件: `codebot/prompts.py:233-274`
- 依赖任务: T2, T3, T4
- 完成标准: 签名 `build_system_prompt(hook_prompts, coordinator_mode, agent_catalog, custom_instructions, skill_section, memory_section, work_dir)`（`prompts.py:233-241`）；`coordinator_mode=True` 时委托给 `codebot.teams.coordinator.get_coordinator_system_prompt`（`prompts.py:242-244`）；否则按 Identity→System→DoingTasks→ExecutingActions→UsingTools→ToneStyle→TextOutput→environment_section 顺序 Add 8 个固定 section（`prompts.py:246-254`）；依次按 `custom_instructions`（priority=80） / `skill_section`（priority=90） / `memory_section`（priority=95）按需 Add（`prompts.py:256-267`）；空字符串不 Add；`hook_prompts` 非空时尾部追加 `# Hook Injected Context\n` + `\n`.join（`prompts.py:271-272`）。

## T6: 实现 build_plan_mode_reminder 动态指令
- 影响文件: `codebot/prompts.py:161-226`
- 依赖任务: 无
- 完成标准: `_PLAN_MODE_FULL_REMINDER`（`prompts.py:161-193`）+ `_PLAN_MODE_SPARSE_REMINDER`（`prompts.py:195-198`）+ `_REMINDER_INTERVAL = 5`（`prompts.py:200`）；`build_plan_mode_reminder(plan_path, plan_exists, iteration)` 根据 `plan_exists` 选择「文件已存在用 EditFile」或「文件不存在用 WriteFile」的 `plan_file_info`（`prompts.py:206-217`）；`iteration == 1` 返回完整版（`prompts.py:219-220`）；否则 `(iteration-1) // 5 % 5 == 0` 时再次发完整版，其余发稀疏版（`prompts.py:222-226`）。

## T7: 实现 build_environment_context 公共 API
- 影响文件: `codebot/prompts.py:277-304`
- 依赖任务: 无
- 完成标准: `build_environment_context(work_dir, active_skills, skill_catalog, agent_catalog)`（`prompts.py:277-282`）输出 3 行基础信息（Current working directory / Operating system / Current time）+ 可选 agent_catalog + 可选 skill_catalog + 可选 `## Active Skills` 段（含每个 skill 的 `### Skill: name` + SOP）；最后用 `"\n".join(parts)` 输出。

## T8: 接入主流程（Agent.run / run_to_completion）
- 影响文件:
 - `codebot/agent.py:399-402` `Agent.run` 启动时调 `build_environment_context` + `conversation.inject_environment`
 - `codebot/agent.py:469-473` `Agent.run` 每轮迭代调 `build_system_prompt`
 - `codebot/agent.py:480-484` Plan Mode 下调 `build_plan_mode_reminder` 并 `conversation.add_system_reminder`
 - `codebot/agent.py:898-901` 自动 compact 后重新注入 environment
 - `codebot/agent.py:918-921` `run_to_completion` 启动时也注入 environment
 - `codebot/agent.py:935-938` `run_to_completion` 调 `build_system_prompt`
- 依赖任务: T1~T7
- 完成标准: Agent.run 主循环在 ModePlan 下每轮调 `build_plan_mode_reminder` 并写入 `conversation.add_system_reminder`，最终走 user 通道的 `<system-reminder>` 块；compact 触发后重新 inject env 与 long-term memory。

## T9: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T8
- 完成标准:
 - `ruff check codebot/prompts.py` 无 lint 错误。
 - `pytest tests/test_agent.py -k "system_prompt or plan or environment"`：`tests/test_agent.py:482-503` 四个测试通过——`test_system_prompt_normal`（`build_system_prompt()` 含 "CodeBot" 且不含 "Plan mode"）/ `test_system_prompt_plan`（`build_plan_mode_reminder("/tmp/plan.md", False, 1)` 含 "Plan mode" + "MUST NOT"）/ `test_plan_mode_sparse_reminder`（iteration=8 含 "Plan mode still active"）/ `test_environment_context`（含工作目录 + "Operating system" + "Current time"）。
 - `pytest tests/test_teams.py::test_coordinator_system_prompt`：`tests/test_teams.py:568-581` 三个测试覆盖 normal / coordinator_mode / plan_mode 组合。
 - `pytest tests/test_skills.py -k "environment_context"`：`tests/test_skills.py:530-548` 覆盖 active_skills 进入 environment_context 路径。

## 进度
- [ ] T1 PromptSection / PromptBuilder 数据结构
- [ ] T2 PromptBuilder.build 排序拼接
- [ ] T3 environment_section 工厂函数
- [ ] T4 7 个固定文本 section 常量
- [ ] T5 build_system_prompt 主入口
- [ ] T6 build_plan_mode_reminder 动态指令
- [ ] T7 build_environment_context 公共 API
- [ ] T8 Agent.run / run_to_completion 接入
- [ ] T9 端到端验证（ruff + 三组 pytest）

```

```markdown
# ch05: System Prompt 设计 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性
- [ ] 数据结构 `@dataclass class PromptSection` 含 `name / priority / content` 三字段在 `codebot/prompts.py:10-15`（`grep -n "class PromptSection" codebot/prompts.py` 返回 1 条）
- [ ] 数据结构 `PromptBuilder` 在 `codebot/prompts.py:17-28`，`__init__` 维护 `_sections: list[PromptSection]`（`codebot/prompts.py:17-19`），`add` 返回 `PromptBuilder` 支持链式调用（`codebot/prompts.py:21-23`），`build` 用 `sort(key=lambda s: s.priority)` 并 `"\n\n".join` 输出（`codebot/prompts.py:25-28`）
- [ ] 函数 `environment_section(work_dir)` 在 `codebot/prompts.py:147-154`，用 `platform.system()` + `platform.release()` + `datetime.now().strftime('%Y-%m-%d')`，返回 priority=70 的 PromptSection
- [ ] 函数 `build_system_prompt` 在 `codebot/prompts.py:233-274`，按 8 段固定 + 3 段可选 + 1 段 hook 尾部顺序拼接
- [ ] 7 个固定文本 section 常量：`IDENTITY_SECTION`(prompts.py:35) / `SYSTEM_SECTION`(:50) / `DOING_TASKS_SECTION`(:63) / `EXECUTING_ACTIONS_SECTION`(:84) / `USING_TOOLS_SECTION`(:100) / `TONE_STYLE_SECTION`(:118) / `TEXT_OUTPUT_SECTION`(:129)
- [ ] Priority 数字固定：0/10/20/30/40/50/60/70，对应 7 固定 section + Environment（`grep -n "priority=" codebot/prompts.py` 返回 ≥10 条覆盖 0/10/20/30/40/50/60/70/80/90/95）
- [ ] 可选 section priority 数字：80 / 90 / 95（CustomInstructions / Skills / Memory，`codebot/prompts.py:259/264/267`）
- [ ] Plan Mode 动态指令：`build_plan_mode_reminder` 在 `codebot/prompts.py:203-226`；`_REMINDER_INTERVAL = 5` 在 `codebot/prompts.py:200`；`_PLAN_MODE_FULL_REMINDER` 在 `:161`；`_PLAN_MODE_SPARSE_REMINDER` 在 `:195`
- [ ] 函数 `build_environment_context` 在 `codebot/prompts.py:277-304`，参数为 `work_dir, active_skills, skill_catalog, agent_catalog`
- [ ] 关键文本片段保留（输出含）：`IMPORTANT: Be careful not to introduce security` / `<system-reminder>` / `Only use emojis if the user explicitly requests it` / `file_path:line_number` / `Do not use a colon before tool calls`（`grep -n "Be careful not to introduce security\|<system-reminder>\|Only use emojis\|file_path:line_number\|colon before tool" codebot/prompts.py` 返回 ≥5 条）

## 2. 接入完整性（必查，杜绝死代码）
- [ ] `git grep -n "build_system_prompt" origin/python -- '*.py'` 返回 ≥5 处真实调用（`codebot/agent.py:469`、`codebot/agent.py:935`、`tests/test_agent.py:483`、`tests/test_teams.py:569`、`tests/test_teams.py:574`、`tests/test_teams.py:581`）
- [ ] `git grep -n "build_environment_context" origin/python -- '*.py'` 返回 ≥5 处（`codebot/agent.py:399`、`codebot/agent.py:898`、`codebot/agent.py:918`、`tests/test_agent.py:500`、`tests/test_skills.py:534`、`tests/test_skills.py:547`）
- [ ] `git grep -n "build_plan_mode_reminder" origin/python -- '*.py'` 返回 ≥3 处（`codebot/agent.py:480`、`tests/test_agent.py:489`、`tests/test_agent.py:495`）
- [ ] Agent.run 调用链：`Agent.run` 启动 → `build_environment_context` (`codebot/agent.py:399`) → `conversation.inject_environment` (`codebot/agent.py:402`) → 每轮 `build_system_prompt` (`codebot/agent.py:469`)
- [ ] Plan Mode 调用链：每轮迭代 → `codebot/agent.py:478-484` 判断 `self.plan_mode` → 调 `build_plan_mode_reminder` → `conversation.add_system_reminder(plan_reminder)`
- [ ] Compact 后恢复链：`codebot/agent.py:897-905` 自动 compact 触发后重新调 `build_environment_context` + `inject_environment` + `inject_long_term_memory`
- [ ] 已记录差异（不在本章 must-fix）:
 - [ ] Python 版本未实现 `BuildPlanModeReentryReminder` / `BuildPlanModeExitReminder`（`git grep -n "reentry\|exit_reminder" origin/python -- 'codebot/prompts.py'` 返回 0 条）
 - 处理意见: Go 有但未接入 TUI，Python 直接省略；后续 ch+ 接入 `/do` 命令时再补。

## 3. 编译与测试
- [ ] `ruff check codebot/prompts.py` 通过（无 lint 错误）
- [ ] `pytest tests/test_agent.py::test_system_prompt_normal` 通过；`tests/test_agent.py:482-485` 断言 `build_system_prompt()` 返回字符串含 `"CodeBot"` 且不含 `"Plan mode"`
- [ ] `pytest tests/test_agent.py::test_system_prompt_plan` 通过；`tests/test_agent.py:488-491` 断言 `build_plan_mode_reminder("/tmp/plan.md", False, 1)` 含 `"Plan mode"` + `"MUST NOT"`
- [ ] `pytest tests/test_agent.py::test_plan_mode_sparse_reminder` 通过；`tests/test_agent.py:494-496` 断言 iteration=8 时含 `"Plan mode still active"`
- [ ] `pytest tests/test_agent.py::test_environment_context` 通过；`tests/test_agent.py:499-502` 断言含 `/home/user/project` + `"Operating system"` + `"Current time"`
- [ ] `pytest tests/test_teams.py -k "build_system_prompt or coordinator_system_prompt"` 通过；`tests/test_teams.py:568-581` 覆盖 normal / coordinator_mode=True / plan_mode=True 三种 build 路径

## 4. 端到端验证
- [ ] Agent 启动 → `Agent.run` 首次注入 environment（`codebot/agent.py:399-402`） → 每轮 `build_system_prompt` 一次（`codebot/agent.py:469`） → system 参数喂给 LLM 客户端（`codebot/agent.py:935-938` 在 `run_to_completion` 路径同理）
- [ ] Plan Mode 验证：以 `--plan-mode` 启动 Agent → `codebot/agent.py:478` 进入 plan_mode 分支 → 下一轮在 stream 之前注入 `<system-reminder>` 包裹的 5 阶段 Workflow（`codebot/agent.py:483-484` + `codebot/conversation.py` 的 `add_system_reminder`）
- [ ] Compact 恢复验证：触发自动 compact → `codebot/agent.py:897-905` 重新 inject env + long-term memory → 下一轮 `build_system_prompt` 时上下文完整
- [ ] 留存证据: 在 Agent 输入 `/plan` 后看一次请求 body 中的 user `<system-reminder>` 内容；或在测试运行后通过 `pytest -v` 看到 4 个 ch05 测试 PASSED

## 5. 文档
- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`docs/python/ch05/`）
- [ ] commit 信息标注 `ch05` 与三件套关闭状态（待统一打包提交）

```

### Java

```markdown
# ch05: System Prompt 设计 Spec

## 1. 背景

没有 System Prompt，模型并不知道自己叫 CodeBot、不知道运行在什么 OS、不知道有哪些工具能用、不知道用户的代码规范，输出会落到「通用 ChatGPT 助手」基线。所有静态规则（语气、安全、工具使用规范）和环境信息必须固化到 System Prompt 才能让模型回答稳定、可预期；动态指令（Plan Mode reminder、Task notification、deferred tool 列表）则走 user channel 的 `<system-reminder>` 块，避免反复改 System 失效缓存。本章把这条 prompt 拼接管线做出来。

## 2. 目标

对外提供 `com.codebot.prompt`：调用者准备好工作目录、模型名、（可选）项目说明 / Skill 列表 / Memory 段，调一次 `PromptBuilder.buildSystemPrompt(env, opts)` 拿到能直接喂给 LLM 客户端的纯文本 System Prompt。多个信息来源（角色、行为准则、工具规范、tone、文本输出风格、环境上下文、项目说明、Skill 摘要、Memory）按优先级合并；动态注入走 `ConversationManager.addSystemReminder` + ch04 主循环。

## 3. 功能需求

- F1: 提供环境探测函数 `detectEnvironment(model)`，输出工作目录、OS、Arch、Shell、是否 Git 仓库、当前分支、模型名、日期等字段；Git 状态用标准命令探测，非 Git 仓库静默降级。
- F2: 提供 `buildSystemPrompt(env, opts)` 主入口，装配 8 个固定 section（Identity / System / DoingTasks / ExecutingActions / UsingTools / ToneStyle / TextOutput / Environment）外加 3 个可选 section（CustomInstructions / Skills / Memory），按优先级排序后拼接。
- F3: `BuildOptions` 接收项目说明 / Skill 摘要 / Memory 三类可选字符串；`null` 或空字符串不进入最终输出。
- F4: 提供 `PromptBuilder` 实例 + `Section` record 支持自定义扩展：调用者可空 builder 起步、自由 `add` section、指定优先级，最后 `build()` 排序输出。
- F5: 各 section 有固定优先级（Identity 最高、Memory 最低，可选 section 排在固定 section 之后），保证最终 prompt 顺序稳定。
- F6: Plan Mode 系统提醒不进入 System Prompt，由 `com.codebot.prompt.PlanModePrompt` 提供构造函数，由 ch04 主循环通过 `addSystemReminder` 注入 user channel。
- F7: 各 section 文案需保持与终端 Agent 系统提示语义一致：禁用 emoji、优先用专用工具、文件路径引用用 `file_path:line_number`、状态报告诚实、对潜在 prompt injection 进行 flag、`<system-reminder>` 与具体 tool 结果无直接关系等关键短语保留。
- F8: 每个 `Tool` 实现的 `description()` 方法返回固定字符串（Java text block），由 ToolRegistry 拼装到 LLM tools 数组中传给模型，作为 System Prompt 的工具规范补充。

## 4. 非功能需求

- N1: System Prompt 内容必须能被 LLM 长缓存命中——只在切 provider / 工作目录 / Skill / Memory 时重建，每轮迭代不重新构建。
- N2: 环境探测在 Git 不存在时静默降级（捕获 `Exception` 后 ignore），不输出错误日志。
- N3: 日期字段使用稳定格式（`LocalDate.now().toString()`，即 ISO `YYYY-MM-DD`），跨进程一致。
- N4: section 之间用恰好两个换行分隔，section 内部用单换行；空 section 不出现。
- N5: 文案不使用 emoji（除非用户在 ToneStyle section 内显式说明）。

## 5. 设计概要

- 核心数据结构: `Section{name, priority, content}` record、`EnvironmentContext{workDir, os, arch, shell, isGitRepo, gitBranch, model, date}` record、`BuildOptions{customInstructions, skillSection, memorySection}` record、`PromptBuilder{sections}` 类。
- 主流程:
 1. TUI 选好 provider → 调 `MemoryManager` 加载 `AGENTS.md` / `CODEBOT.md` 合并文本；
 2. 调 `PromptBuilder.detectEnvironment(model)` 拿环境上下文；
 3. 调 `PromptBuilder.buildSystemPrompt(env, opts)` 拼出 system prompt；
 4. system prompt 喂给 `LlmClient.create(provider, systemPrompt)`。
 - `buildSystemPrompt` 内部依次 `add` 8 固定 section + 3 可选 section，最后排序拼接。
- 调用链:
 - `CodeBotModel.initializeProvider()` 切 provider 时调 `buildSystemPrompt`，输出作为 `LlmClient.create` 第二参数。
 - 动态注入：Agent 主循环在 PLAN 模式时调 `PlanModePrompt.buildReminder` → `conv.addSystemReminder`，最终包成 `<system-reminder>` user 消息。
- 与其他模块的交互:
 - 依赖 JDK 21 标准库（`java.lang.ProcessBuilder` / `java.time.LocalDate` / `java.util.Comparator`），不依赖项目其他模块。
 - 被 `com.codebot.tui.CodeBotModel`（构造 prompt）、`com.codebot.agent.Agent`（Plan Mode reminder 注入）使用。
 - 输入数据由 `com.codebot.memory.MemoryManager` 等模块准备好后传入。

## 6. Out of Scope

- Coordinator Mode / 自定义 Agent 角色的 system prompt 替换分支不在本章实现，所有 Agent 共用默认 prompt。
- 不缓存 section 输出。
- Plan Mode Reentry / Exit 提醒函数已写但未接入 TUI，留给下章或专门 PR。
- 不实现外部 `--system-prompt` / `appendSystemPrompt` CLI 参数。
- Skill 摘要的具体来源与拼装由 ch10 负责，本章只接收已拼好的字符串。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch05: System Prompt 设计 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。本章为验收，所有任务已经在仓库里落地（origin/java 分支）。

## T1: 定义 Section / Builder / BuildOptions 数据结构
- 影响文件: `src/main/java/com/codebot/prompt/PromptBuilder.java:17-36`
- 依赖任务: 无
- 完成标准: `Section(String name, int priority, String content)` record（PromptBuilder.java:17）、`EnvironmentContext(workDir/os/arch/shell/isGitRepo/gitBranch/model/date)` record（PromptBuilder.java:19-27）、`BuildOptions(customInstructions, skillSection, memorySection)` record（PromptBuilder.java:29-32）全部定义；`PromptBuilder.add` 返回 `this` 支持链式调用（PromptBuilder.java:38-41）。

## T2: 实现 detectEnvironment
- 影响文件: `src/main/java/com/codebot/prompt/PromptBuilder.java:59-105`
- 依赖任务: T1
- 完成标准: `System.getProperty("user.dir")`、`System.getProperty("os.name")`、`System.getProperty("os.arch")`、`SHELL` 环境变量（缺省 `bash`，PromptBuilder.java:63-66）、`LocalDate.now().toString()`（PromptBuilder.java:103）入填；git 检测使用 `ProcessBuilder("git", "-C", workDir, "rev-parse", "--is-inside-work-tree")` 静默判断（PromptBuilder.java:71-84），是 git repo 再跑 `--abbrev-ref HEAD` 拿到 branch（PromptBuilder.java:86-101）。

## T3: 实现 PromptBuilder.build 排序 + 拼接
- 影响文件: `src/main/java/com/codebot/prompt/PromptBuilder.java:43-54`
- 依赖任务: T1
- 完成标准: `build()` 用 `Comparator.comparingInt(Section::priority)` 升序排（PromptBuilder.java:44）；`strip()` 后空 content 不进入 parts（PromptBuilder.java:48-51）；`String.join("\n\n", parts)` 输出最终文本（PromptBuilder.java:53）。

## T4: 实现 8 个固定 section 方法
- 影响文件: `src/main/java/com/codebot/prompt/PromptSections.java`
- 依赖任务: T1
- 完成标准:
 - `identitySection()`（priority 0，PromptSections.java:27）—— CodeBot 身份 + 安全 / URL 不乱造
 - `systemSection()`（priority 10，PromptSections.java:48）—— `<system-reminder>` 语义、prompt injection 警告、hook feedback、自动 compact
 - `doingTasksSection()`（priority 20，PromptSections.java:93）—— 不做未读过的代码、最小修改原则、不写无用注释、报真实结果
 - `executingActionsSection()`（priority 30，PromptSections.java:119）—— 高破坏性操作需 confirm
 - `usingToolsSection()`（priority 40，PromptSections.java:156）—— Tool 优先 / TaskCreate / 并行调用 / Agent / ToolSearch
 - `toneStyleSection()`（priority 50，PromptSections.java:171）—— 不用 emoji / 简短 / 用 `file_path:line_number` / 工具调用前别打冒号
 - `outputEfficiencySection()`（priority 60，PromptSections.java:197）—— 文本输出一句话规划、少注释、end-of-turn summary
 - `environmentSection(env)`（priority 70，PromptSections.java:203）—— 把 `EnvironmentContext` 渲染成 5~8 行环境信息块

## T5: 实现 buildSystemPrompt 主入口
- 影响文件: `src/main/java/com/codebot/prompt/PromptBuilder.java:108-134`
- 依赖任务: T2, T3, T4
- 完成标准: 先 `add` 8 个固定 section（PromptBuilder.java:111-118），再依据 `opts.customInstructions()`（priority 80，PromptBuilder.java:120-123） / `opts.skillSection()`（priority 90，PromptBuilder.java:125-127） / `opts.memorySection()`（priority 95，PromptBuilder.java:129-131）按需 `add`；`null` 或空字符串不 `add`。

## T6: 实现 Plan Mode 动态指令
- 影响文件: `src/main/java/com/codebot/prompt/PlanModePrompt.java`
- 依赖任务: 无
- 完成标准: `PLAN_MODE_FULL_REMINDER`（PlanModePrompt.java:11）+ `PLAN_MODE_SPARSE_REMINDER`（PlanModePrompt.java:101）+ `PLAN_MODE_REENTRY_REMINDER`（PlanModePrompt.java:107）+ `PLAN_MODE_EXIT_REMINDER`（PlanModePrompt.java:127）四段模板；`REMINDER_INTERVAL=5`（PlanModePrompt.java:9）；`buildReminder(planPath, planExists, iteration)`（PlanModePrompt.java:141）在 iteration==1 给完整版，否则按 5 次为周期间断重发完整版，其余给稀疏版；`buildReentryReminder`（PlanModePrompt.java:164） / `buildExitReminder`（PlanModePrompt.java:169）已实现但 TUI 当前未调用（保留作为后续接入点）。

## T7: 接入主流程（TUI / Agent）
- 影响文件:
 - `src/main/java/com/codebot/tui/CodeBotModel.java:382PromptBuilder.detectEnvironment(model)`
 - `src/main/java/com/codebot/tui/CodeBotModel.java:385-388new PromptBuilder.BuildOptions(...)`
 - `src/main/java/com/codebot/tui/CodeBotModel.java:389PromptBuilder.buildSystemPrompt(env, options)`
 - `src/main/java/com/codebot/tui/CodeBotModel.java:391LlmClient.create(selectedProvider, systemPrompt)`
 - `src/main/java/com/codebot/agent/Agent.java:112PlanModePrompt.buildReminder(planPath, planExists, iteration)`
- 依赖任务: T1~T6
- 完成标准: `CodeBotModel.initializeProvider()` 在选 provider 阶段一次性构造 System Prompt（CodeBotModel.java:382-391）；`Agent.agentLoop` 在 PLAN 模式下每轮调 `PlanModePrompt.buildReminder` 并写入 `conv.addSystemReminder`（Agent.java:107-113），最终走 user 通道的 `<system-reminder>` 块。

## T8: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T7
- 完成标准:
 - `./gradlew build` 通过（顶层命令验证）。
 - `./gradlew test` 通过（虽然 ch05 没有专门的 prompt 单测，但 `CodeBotModel` 与 `Agent` 的整体编译与冒烟测试覆盖了 prompt 装配链路）。
 - 在 TUI 启动后 `/plan` 进入计划模式，下一轮 agent stream 注入完整版 Plan Mode reminder（5 阶段 Workflow 文本可在 TUI 的请求日志中观察到）。

## 进度
- [ ] T1 数据结构（record）
- [ ] T2 detectEnvironment
- [ ] T3 PromptBuilder.build
- [ ] T4 8 个固定 section 方法
- [ ] T5 buildSystemPrompt 主入口
- [ ] T6 Plan Mode 动态指令
- [ ] T7 TUI / Agent 接入
- [ ] T8 端到端验证（`./gradlew build` 通过 + CodeBotModel.java:389 调用通过 + Plan reminder 在 Agent.java:112 接入）

```

```markdown
# ch05: System Prompt 设计 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性
- [ ] 数据结构 `Section(String name, int priority, String content)` record 在 `src/main/java/com/codebot/prompt/PromptBuilder.java:17`（`grep -n "record Section" src/main/java/com/codebot/prompt/PromptBuilder.java`）
- [ ] 数据结构 `EnvironmentContext` 8 字段 record 在 `src/main/java/com/codebot/prompt/PromptBuilder.java:19-27`
- [ ] 数据结构 `BuildOptions(customInstructions, skillSection, memorySection)` record 在 `src/main/java/com/codebot/prompt/PromptBuilder.java:29-32`
- [ ] 静态方法 `detectEnvironment` 在 `src/main/java/com/codebot/prompt/PromptBuilder.java:59`，git 探测在 PromptBuilder.java:71-84，shell 缺省 bash 在 PromptBuilder.java:63-66
- [ ] 静态方法 `buildSystemPrompt` 在 `src/main/java/com/codebot/prompt/PromptBuilder.java:108`，按八段 + 三可选段顺序 `add`
- [ ] 8 个固定 section 方法：`identitySection`(PromptSections.java:27) / `systemSection`(:48) / `doingTasksSection`(:93) / `executingActionsSection`(:119) / `usingToolsSection`(:156) / `toneStyleSection`(:171) / `outputEfficiencySection`(:197) / `environmentSection`(:203)
- [ ] Priority 数字固定：0/10/20/30/40/50/60/70，对应 8 个 section（`grep -nE "new Section\(" src/main/java/com/codebot/prompt/PromptSections.java` 返回 8 条）
- [ ] 可选 section Priority 数字：80 / 90 / 95（CustomInstructions / Skills / Memory，PromptBuilder.java:121/126/130）
- [ ] Plan Mode 动态指令：`buildReminder` 在 `src/main/java/com/codebot/prompt/PlanModePrompt.java:141`；`REMINDER_INTERVAL=5` 在 PlanModePrompt.java:9
- [ ] 关键文本片段保留：`build()` 输出含 `IMPORTANT: Be careful not to introduce security` / `<system-reminder>` / `Only use emojis if the user explicitly requests it` / `file_path:line_number`（可通过 `grep -n` 验证 PromptSections.java）
- [ ] 每个 Tool 实现的 `description()` 返回 Java text block 静态描述（例如 `BashTool.java:17-39` 的 `DESCRIPTION` 常量、`ReadFileTool.java:15-24`、`EditFileTool.java:15-24`）

## 2. 接入完整性（必查，杜绝死代码）
- [ ] `grep -rn "PromptBuilder.buildSystemPrompt" --include="*.java" src` 返回至少 1 处真实调用（`src/main/java/com/codebot/tui/CodeBotModel.java:389`）
- [ ] `grep -rn "PromptBuilder.detectEnvironment" --include="*.java" src` 返回至少 1 处（`src/main/java/com/codebot/tui/CodeBotModel.java:382`）
- [ ] `grep -rn "PlanModePrompt.buildReminder" --include="*.java" src` 返回 ≥ 1 个主流程调用（`src/main/java/com/codebot/agent/Agent.java:112`）
- [ ] TUI 调用链：用户选 provider → `CodeBotModel.initializeProvider`（CodeBotModel.java:376）→ `buildSystemPrompt`（CodeBotModel.java:389）→ `LlmClient.create`（CodeBotModel.java:391）
- [ ] Agent 调用链：每轮迭代 → `Agent.java:107-114` 判断 `PermissionMode.PLAN` → 调 `PlanModePrompt.buildReminder` → `conv.addSystemReminder`
- [ ] 已记录死代码（不在本章 must-fix）:
 - [ ] `src/main/java/com/codebot/prompt/PlanModePrompt.java:164 buildReentryReminder` 无调用方（`grep -rn "buildReentryReminder" --include="*.java" src` 只返回定义点）
 - [ ] `src/main/java/com/codebot/prompt/PlanModePrompt.java:169 buildExitReminder` 无调用方（同上）
 - 处理意见: 已抄自目标设计；TUI 当前 `/do` 命令未注入 exit reminder。记录已知；后续如要补，调用点应放在 `CodeBotModel` 处理 `/do` 子命令附近

## 3. 编译与测试
- [ ] `./gradlew build` 通过（顶层命令验证；本次验收已跑）
- [ ] `./gradlew test` 通过（虽然 ch05 没专门的 prompt 单测，整体编译与冒烟测试覆盖了 prompt 装配链路）
- [ ] `./gradlew compileJava` 对 `prompt` 包零警告（IDE 或 `javac -Xlint:all` 抽查 PromptBuilder / PromptSections / PlanModePrompt）

## 4. 端到端验证
- [ ] TUI 启动 → 选 provider → `buildSystemPrompt` 一次 → `LlmClient.create` 拿到 system prompt（`src/main/java/com/codebot/tui/CodeBotModel.java:382-391`）
- [ ] `/plan` 命令进入 Plan Mode → Agent Run 下一轮在 stream 之前注入 `<system-reminder>` 包裹的 5 阶段 Workflow（`src/main/java/com/codebot/agent/Agent.java:107-113` + `ConversationManager.addSystemReminder`）
- [ ] 留存证据: 验收阶段未保存日志；若要补，可在 TUI 输入 `/plan` 后看一次请求 body 中的 user `<system-reminder>` 内容

## 5. 文档
- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`docs/java/ch05/`）
- [ ] commit 信息标注 `ch05` 与三件套关闭状态（待统一打包提交）

```



## ch06

```markdown
# 我的初步想法
做一套纵深防御的安全检查机制，方向上大致包含这几条：
- 危险操作黑名单：在执行前就拦掉已知高危命令（破坏性 shell 操作、远程脚本下载即执行等）
- 路径沙箱：限制文件读写类工具只能落在允许的目录范围内活动
- 可配置的允许/拒绝/询问规则：按「工具 + 参数或路径模式」声明放行还是拦截
- 多档权限模式：让用户能整体切换"严格 / 默认 / 放行"等档位，覆盖在具体规则之上
- 人在回路（HITL）：规则没有明确命中时把决定权交回用户，并支持"本次允许 / 本会话允许 / 永久允许"
- 规则优先级：会话级临时规则 > 项目级固定规则 > 用户全局默认
```

### Go

```markdown
# ch06: 权限系统 Spec

## 1. 背景

工具系统（ch03）放出了 Bash 和写文件的能力，Agent Loop（ch04）能自主决定调谁；没有权限层，模型一句话就能 `rm -rf /` 或者写到项目目录之外。一个生产级 Coding Agent 的最低安全要求是「至少要拦得住明显的危险操作 + 把不熟悉的操作交给用户决定」。本章把这条防御线做出来：明显错的直接拦、明显对的直接放，剩下的让规则 / 模式 / HITL 决定。

## 2. 目标

对外提供 `permissions.Checker`：调用者构造好路径沙箱 + 规则引擎 + 权限模式之后，对任意 `tools.Tool` + 参数调一次 `Check` 拿到 `Decision{Effect, Reason}`。Agent Loop 直接用这个决策决定是否拦截 / 直接执行 / 走 HITL。权限模式支持 default / acceptEdits / plan / bypassPermissions 四种，TUI 用 Shift+Tab 或 `/mode` 在它们之间切换；Plan 模式拥有特殊豁免分支。HITL 用户选「Always」时把规则 append 到本地规则文件。

## 3. 功能需求

- F1: 提供权限模式枚举（default / acceptEdits / plan / bypassPermissions）与模式 × 工具类别（Read / Write / Command）的决策矩阵，对外暴露 `ModeDecide(mode, category)` 查矩阵。
- F2: Layer 1 危险命令检测：硬编码覆盖 `rm -rf /` 类删除、磁盘格式化、设备写入、`chmod -R 777 /`、fork bomb、`curl | sh` / `wget | sh` 类远程执行等模式，命中即拒绝并给出原因。
- F3: Layer 1 安全命令白名单：维护只读 Bash 命令前缀表（`ls` / `pwd` / `cat` / `git status` 等），命中且不含管道 / 重定向 / 子 shell / 命令分隔符 / 子命令替换时直接放行。
- F4: Layer 2 路径沙箱：构造时把项目根 + 系统临时目录 + 额外指定目录全部解析为绝对路径作为白名单；`Check(path)` 对入参做绝对路径前缀匹配判定。
- F5: Layer 3 规则引擎：管理 user / project / local 三层 YAML 规则文件，写优先级 local > project > user，文件内按 LIFO 匹配；`Rule{ToolName, Pattern, Effect}` 用 glob 匹配主参数；提供 `AppendLocalRule` 写回本地规则文件。
- F6: 规则语法 `ToolName(pattern)` 支持正则解析，effect 仅 allow / deny；YAML 文件结构按 `{rule, effect}` 列表。
- F7: 内容字段提取：把六个核心工具的「主参数字段」映射出来（Bash 用命令、Read/Write/Edit 用文件路径、Glob/Grep 用 pattern），其他工具返回空字符串。
- F8: `Checker.Check` 按固定顺序逐层判定：Plan 模式豁免 → 安全命令 → 危险命令 → 路径沙箱 → 规则引擎 → 模式矩阵兜底；Plan 模式下 Agent / ToolSearch / AskUserQuestion 与 plan 文件自身写入直接放行。
- F9: 自学习：HITL 用户选「Always」时，调用方把当前调用的工具名 + 主参数包成本地规则追加到本地规则文件；过长主参数要做截断。

## 4. 非功能需求

- N1: 危险命令模式必须硬编码进代码，不依赖外部下载或环境变量注入，避免被攻击者绕过。
- N2: 路径沙箱必须始终包含项目根 + 系统临时目录；额外路径在构造时一次性 Abs，不在 Check 时重新解析，防止符号链接换路径绕过。
- N3: 规则文件解析必须在 YAML 语法错误 / 文件不存在时静默返回空规则集，不让单个坏规则导致整套规则失效。
- N4: `Check` 是无副作用纯函数（除规则文件磁盘读），只读，不修改任何 in-memory 状态。
- N5: Plan 模式的工具豁免分支必须早于沙箱检查，避免 plan 模式下写 plan 文件被沙箱误拦。

## 5. 设计概要

- 核心数据结构:
 - `DecisionEffect`（allow / deny / ask）与 `Decision{Effect, Reason}`。
 - `PermissionMode` 枚举与 mode × category 的决策矩阵。
 - `PathSandbox`：持有已 Abs 的白名单根目录列表。
 - `Rule{ToolName, Pattern, Effect}` + `RuleEngine{UserPath, ProjectPath, LocalPath}`。
 - `Checker{Sandbox, RuleEngine, Mode, PlanFilePath}`。
- 主流程（一次 `Check` 调用）:
 - 抽出工具类别和主参数 content。
 - Plan 模式分支：白名单工具或 plan 文件写入直接 Allow。
 - 命令类工具：先安全命令直放，再危险命令直拒。
 - 读 / 写工具：content 非空时走路径沙箱。
 - 走规则引擎：命中按 Effect 决定。
 - 落到模式矩阵兜底。
- 调用链:
 - TUI 装配 → 构造 `PathSandbox` + `RuleEngine` + `Checker` → 传给 Agent。
 - Agent 执行工具前 → `Checker.Check` → Deny 给错误结果 / Ask 走 HITL / Allow 继续。
 - HITL 选 Always → `RuleEngine.AppendLocalRule` 写本地规则。
 - `/plan` 命令切到 Plan 模式 + 设置 plan 文件路径；`/do` 或 Plan 通过后还原。
- 与其他模块的交互:
 - 依赖 `internal/tools`（`ToolCategory` 枚举 + `Tool.Name/Category` 接口）。
 - 依赖 YAML 库做规则文件序列化。
 - 被 `internal/agent`、`internal/tui` 直接使用；`internal/agents`（SubAgent）继承父 Agent 的 Checker。

## 6. Out of Scope

- 不实现 LLM 分类器；本章纯静态规则。
- 不实现 PowerShell 危险命令检测，目前只覆盖 Bash。
- 不持久化用户级别 / 项目级别规则文件的写入，只写本地规则文件。
- 不实现规则文件热重载（每次 Evaluate 都读盘）。
- 不实现目标设计中的额外模式（dontAsk / auto / bubble）。
- 不实现规则的解释 UI。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch06: 权限系统 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。本章为验收，所有任务已经在仓库里落地。

## T1: 定义决策与模式枚举
- 影响文件: `internal/permissions/permissions.go:15-50`
- 依赖任务: 无
- 完成标准: `DecisionEffect`(string) 三态 `Allow/Deny/Ask`；`Decision{Effect, Reason}`；`PermissionMode` 四态 `ModeDefault/ModeAcceptEdits/ModePlan/ModeBypass`；`modeMatrix` 决策表 4×3 全部填齐（permissions.go:37-42）；`ModeDecide(mode, category)` 提供查表函数，未识别 mode 默认 Ask。

## T2: 实现 Layer 1 危险命令检测
- 影响文件: `internal/permissions/permissions.go:54-77`
- 依赖任务: 无
- 完成标准: `dangerousPattern{re, reason}` 类型；`defaultDangerousPatterns` 列出 8 条核心模式（`rm -rf /`、`mkfs.`、`dd if=...of=/dev/`、`chmod -R 777 /`、fork bomb、`curl|sh`、`wget|sh`、`> /dev/sd`）；`DetectDangerous(command)` 返回 `(命中bool, 原因string)`。

## T3: 实现 Layer 1 安全命令白名单
- 影响文件: `internal/permissions/permissions.go:210-236`
- 依赖任务: 无
- 完成标准: `safeCommandPrefixes` 列出 50+ 个只读命令前缀（含 `git status` 等 git 只读子命令、`go version` 等）；`IsSafeCommand(command)` 检查命令前缀 + 命令中不含 `>` / `|` / `;` / `&&` / `$(` / 反引号才返回 true，否则 false。

## T4: 实现 Layer 2 路径沙箱
- 影响文件: `internal/permissions/permissions.go:81-107`
- 依赖任务: 无
- 完成标准: `PathSandbox{allowedRoots []string}` 类型；`NewPathSandbox(projectRoot, extraAllowed...)` 把 root + `os.TempDir` + extras 一次性 `filepath.Abs` 后存入 `allowedRoots`；`Check(path)` 用 `filepath.Abs(path)` 后逐 root 做 `strings.HasPrefix` 检查；返回 `(允许bool, 原因string)`。

## T5: 实现 Layer 3 规则引擎
- 影响文件: `internal/permissions/permissions.go:111-206`
- 依赖任务: 无
- 完成标准:
 - `RuleEffect = "allow" | "deny"`、`Rule{ToolName, Pattern, Effect}`、`Rule.Matches(toolName, content)` 用 `filepath.Match` 做 glob
 - `RuleEngine{UserPath, ProjectPath, LocalPath}` 三层文件路径
 - `RuleEngine.Evaluate(toolName, content)` 顺序遍历 user→project→local；单文件内 LIFO（从尾向前匹配）；命中返回 `*RuleEffect`，未命中返回 nil
 - `loadRulesFile(path)` 解析 YAML 列表 `[{rule, effect}, ...]`；坏行静默跳过；空文件 / 不存在文件返回 nil
 - `parseRule(raw, effect)` 用正则 `^(\w+)\((.+)\)$` 解析
 - `AppendLocalRule(r)` 自动 `MkdirAll(filepath.Dir(LocalPath), 0o755)`，把现有规则 + 新规则全量重写 YAML

## T6: 实现内容字段提取
- 影响文件: `internal/permissions/permissions.go:240-252`
- 依赖任务: 无
- 完成标准: `contentFields` 映射六个核心工具到主参数字段名（Bash→command、ReadFile/WriteFile/EditFile→file_path、Glob/Grep→pattern）；`ExtractContent(toolName, args)` 查表后从 `args` 取出对应字段的字符串，未识别工具返回空。

## T7: 实现主入口 Checker
- 影响文件: `internal/permissions/permissions.go:256-325`
- 依赖任务: T1~T6
- 完成标准: `Checker{Sandbox, RuleEngine, Mode, PlanFilePath}` + `NewChecker(sandbox, ruleEngine, mode)`；`Check(tool, args)` 按 spec.md F9 列出的 6 步顺序逐层判断；Plan 模式豁免 `Agent`/`ToolSearch`/`AskUserQuestion` 早于沙箱（避免误拦）；Plan 文件写入豁免；Reason 字段写明决策来源（"Safe read-only command" / "Dangerous command blocked: ..." / "Path sandbox: ..." / "Permission rule: allow/deny" / "Permission mode <mode>: ..." / "User confirmation required"）。

## T8: 实现 Plan 文件判定
- 影响文件: `internal/permissions/permissions.go:327-349`
- 依赖任务: T7
- 完成标准: `isPlanFile(targetPath, planPath)` 多策略匹配：1) 双方 abs 路径相等；2) `filepath.Clean` 后相等；3) basename 相等且 target 中含 `.codebot/plans/`。空路径直接 false。

## T9: 接入主流程
- 影响文件:
 - `internal/tui/tui.go:362-368` / `:724-730` 构造 Checker
 - `internal/tui/tui.go:1124-1135/mode` 命令切模式
 - `internal/tui/tui.go:874` Shift+Tab 切模式（`nextPermissionMode`）
 - `internal/tui/tui.go:1199-1232/plan/do` 切 ModePlan + PlanFilePath
 - `internal/tui/tui.go:1379-1416` Plan 通过 / 拒绝时切模式
 - `internal/agent/agent.go:88` Plan Mode reminder 注入条件
 - `internal/agent/agent.go:363-405Check` 与 HITL + AllowAlways append rule
- 依赖任务: T1~T8
- 完成标准: 用户切换模式 / 进入 Plan 模式 / 工具调用 / HITL 选择 AllowAlways 四条主路径全部接到 `Checker.Check` 与 `RuleEngine.AppendLocalRule`。

## T10: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T9
- 完成标准:
 - `go build ./...` 通过（顶层命令，已验证）
 - 手动场景:
 1. 在 TUI 默认模式下，发送让 Agent 跑 `rm -rf /` → 工具结果应是 `Permission denied: Dangerous command blocked: recursive force delete root`
 2. 在 TUI 中让 Agent 写一个工作目录外的文件 `/etc/passwd` → 应被沙箱 Deny
 3. 在 TUI 中让 Agent 写工作目录内的文件 → Default 模式触发 Ask；HITL 选 AllowAlways → 应在 `.codebot/permissions.local.yaml` 看到新增的 `WriteFile(<path>*)` 规则；下次同路径写不再 Ask
 4. `/plan` 进入 Plan 模式 → Agent 调 Write 工具写非 plan 文件被 Deny；写 plan 文件被 Allow
 5. Shift+Tab 切到 `ModeBypass` → 危险命令仍被拦（Layer 1 不可绕过），普通 Write 直接 Allow

## 进度
- [ ] T1 决策 + 模式枚举
- [ ] T2 危险命令检测
- [ ] T3 安全命令白名单
- [ ] T4 路径沙箱
- [ ] T5 规则引擎
- [ ] T6 内容字段提取
- [ ] T7 主入口 Checker
- [ ] T8 Plan 文件判定
- [ ] T9 主流程接入
- [ ] T10 端到端验证（编译通过 + Agent loop 与 TUI 调用链确认）

```

```markdown
# ch06: 权限系统 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性
- [ ] 类型 `DecisionEffect` 和常量 `Allow`/`Deny`/`Ask` 在 `internal/permissions/permissions.go:15-21`（`grep -n "DecisionEffect\|^const" internal/permissions/permissions.go | head`）
- [ ] 类型 `PermissionMode` 与四常量 `ModeDefault`/`ModeAcceptEdits`/`ModePlan`/`ModeBypass` 在 `internal/permissions/permissions.go:28-35`
- [ ] 决策矩阵 `modeMatrix` 在 `internal/permissions/permissions.go:37-42`，全 4×3 共 12 格无空白
- [ ] `ModeDecide(mode, category)` 在 `internal/permissions/permissions.go:44`，未识别 mode 返回 Ask
- [ ] `DetectDangerous` 在 `internal/permissions/permissions.go:70` + 模式列表 `defaultDangerousPatterns` 8 条在 `permissions.go:59-68`
- [ ] `IsSafeCommand` 在 `internal/permissions/permissions.go:224` + 前缀表 `safeCommandPrefixes` 50+ 条在 `permissions.go:210-222`
- [ ] `PathSandbox` + `NewPathSandbox` + `Check` 在 `internal/permissions/permissions.go:81/85/95`，默认包含 `os.TempDir()` 与 `projectRoot`
- [ ] `Rule` + `RuleEffect` + `Rule.Matches` 在 `internal/permissions/permissions.go:113-130`，glob 用 `filepath.Match`
- [ ] `RuleEngine` 三层 path 字段 + `Evaluate`(`:138`) + `AppendLocalRule`(`:151`) + `loadRulesFile`(`:169`) + `parseRule`(`:200`)
- [ ] `ruleRE` 正则 `^(\w+)\((.+)\)$` 在 `internal/permissions/permissions.go:198`
- [ ] `contentFields` 6 工具映射 + `ExtractContent` 在 `internal/permissions/permissions.go:240-252`
- [ ] `Checker` 主入口 + `NewChecker` + `Check` 在 `internal/permissions/permissions.go:256-325`
- [ ] `isPlanFile` 多策略匹配在 `internal/permissions/permissions.go:327`：abs 相等 / clean 相等 / basename + `.codebot/plans/` 包含
- [ ] Plan Mode 豁免工具名单 `Agent` / `ToolSearch` / `AskUserQuestion` 在 `permissions.go:272-280` 早于沙箱检查
- [ ] 五层防御按序：Plan 豁免 → 安全命令 → 危险命令 → 沙箱 → 规则 → 模式（permissions.go:271-325）

## 2. 接入完整性（必查，杜绝死代码）
- [ ] `grep -rn "permissions.NewChecker" --include="*.go"` 至少 2 处真实调用（`internal/tui/tui.go:362` 与 `:724`）
- [ ] `grep -rn "permissions.NewPathSandbox" --include="*.go"` 至少 2 处（同上行）
- [ ] `grep -rn "permissions.RuleEngine\|permissions\.Rule{" --include="*.go"` Engine 构造在 TUI（tui.go:364/726）；Rule 构造在 Agent HITL 自学习（agent.go:400）
- [ ] `grep -rn "Checker.Check\b" --include="*.go"` 主流程调用方在 `internal/agent/agent.go:363`
- [ ] `grep -rn "AppendLocalRule" --include="*.go"` 主流程调用方在 `internal/agent/agent.go:400`
- [ ] `grep -rn "permissions.ModeDefault\|permissions.ModeAcceptEdits\|permissions.ModePlan\|permissions.ModeBypass" --include="*.go"` 在 TUI 至少 8 处使用（`tui.go:367/729/1134/1201/1222/1379/1385/1415/1220/2445/2449/2879/2880/2883/2884/3173/3177`），覆盖创建 / 切换 / 渲染各路径
- [ ] `grep -rn "permissions.ExtractContent" --include="*.go"` 主流程 2 处使用（`agent.go:375` 与 `agent.go:395`）
- [ ] 配置接入：`Checker.RuleEngine.LocalPath` 默认 `filepath.Join(wd, ".codebot", "permissions.local.yaml")` 在 `tui.go:365` 与 `:727`
- [ ] HITL 链路：`Checker.Check` 返回 Ask → `agent.go:373-406` 通过 `PermissionRequestEvent` 走 ch04 事件循环 → TUI 渲染 3 选项（`tui.go:1292-1296`）→ 用户选 `PermAllowAlways` 时回灌 `AppendLocalRule`

## 3. 编译与测试
- [ ] `go build ./...` 通过（顶层命令，2026-05-21 已验证）
- [ ] `go test ./internal/permissions/...` 通过（2026-05-21）。`internal/permissions/permissions_test.go` 覆盖 `DetectDangerous / IsSafeCommand / PathSandbox / parseRule / RuleEngine 最后一条胜出 / ExtractContent / ModeDecide / Checker.Check 多层` 八组测试。
- [ ] `go vet ./...` 无警告（2026-05-21 顶层运行无输出）

## 4. 端到端验证
- [ ] TUI 启动并选 provider 后构造 Checker（`internal/tui/tui.go:362-368`），调用链 `tui.go:622-744` 已确认
- [ ] Plan Mode：`/plan` → `Checker.Mode = ModePlan` + `Checker.PlanFilePath = <plan slug>`；下一轮工具调用调 `Checker.Check`，写工具被 Deny 除非命中 Plan 文件
- [ ] HITL：默认模式下让模型写新文件，TUI 弹 `Yes / Yes, don't ask again / No`（`tui.go:1292-1296`），三选项与 `PermAllow/PermAllowAlways/PermDeny` 对应
- [ ] 自学习：选 `PermAllowAlways` 时 `agent.go:394-405` 把 `WriteFile(content[:60]+"*")` append 到 `.codebot/permissions.local.yaml`
- [ ] 危险命令防御不可绕过：即使 ModeBypass，`Checker.Check` 仍按顺序先经过 Layer 1（permissions.go:285-295 在 ModeBypass 也会执行，因为安全命令 / 危险命令分支不依赖 mode），下次让模型执行 `rm -rf /` 仍 Deny
- [ ] 留存证据: 验收阶段未保存日志；如要补，在 `.codebot/permissions.local.yaml` 中观察 AllowAlways 写入的 YAML 列表项

## 5. 文档
- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`specs/go/ch06/`）
- [ ] commit 信息标注 `ch06` 与三件套关闭状态（待统一打包提交）

```

### Python

```markdown
# ch06: 权限系统 Spec

## 1. 背景

工具系统（ch03）开放了 Bash 和写文件能力，Agent Loop（ch04）允许模型自主决策调谁。没有权限层，模型一句话就能 `rm -rf /` 或者写到项目目录之外。一个生产级 Coding Agent 的最低安全门槛是“拦住明显危险的操作 + 把不熟悉的操作交给用户决定”。本章把这条防御线做出来：明显错的直接拦、明显对的直接放，剩下的让规则 / 模式 / HITL 来决定。

## 2. 目标

对外提供 `codebot.permissions.PermissionChecker`：调用者构造好 `DangerousCommandDetector` + `PathSandbox` + `RuleEngine` + `PermissionMode`，对任意 `codebot.tools.base.Tool` + `arguments` 调一次 `check(...)`，拿回 `Decision(effect, reason)`。Agent Loop 根据这个 `Decision` 决定直接执行 / 直接拒绝 / 走 HITL（产出 `PermissionRequest` 事件，由 TUI 渲染并交还 `PermissionResponse`）。权限模式覆盖 default / acceptEdits / plan / bypassPermissions / custom / dontAsk 六种，TUI 用 Shift+Tab 或 `/mode` 切换；Plan 模式拥有特殊豁免分支。HITL 用户选「Allow Always」时把规则 append 到本地 YAML 规则文件。

## 3. 功能需求

- F1: 提供权限模式枚举 `PermissionMode`（default / acceptEdits / plan / bypassPermissions / custom / dontAsk）与模式 × 工具类别（`read` / `write` / `command`）的决策矩阵 `_MODE_MATRIX`，对外暴露 `mode_decide(mode, category)` 查矩阵。
- F2: Layer 1 危险命令检测：`DangerousCommandDetector.detect(command)` 用硬编码的 8 条正则覆盖 `rm -rf /` 类删除、磁盘格式化、`dd if=...of=/dev/`、`chmod -R 777 /`、fork bomb、`curl | sh` / `wget | sh` 远程执行、`> /dev/sd` 设备写入。命中即返回 `(True, reason)`，调用方据此拒绝。
- F3: Layer 1 安全命令白名单：维护 `_SAFE_COMMANDS` 只读 Bash 命令前缀集合（`ls` / `pwd` / `cat` / `git status` / `git diff` / `go version` / `npm -v` 等）；`is_safe_command(command)` 检查命令前缀且不含 `|` / `;` / `&&` / `>` / `$(` / 反引号时直接放行。
- F4: Layer 2 路径沙箱 `PathSandbox`：构造时把 `project_root` + `tempfile.gettempdir()` + `extra_allowed` 全部 `Path.resolve()` 后存入 `_allowed_roots`；`check(path)` 对入参做 `expanduser` + 绝对化 + `resolve(strict=True)`（解析 symlink）后逐 root 做 `Path.relative_to` 判定；如果路径不存在则回退到对父目录做 `resolve` 再拼接，支持新文件的预检。
- F5: Layer 3 规则引擎 `RuleEngine`：管理 user / project / local 三层 YAML 规则文件，路径优先级 user < project < local（local 覆盖 project 覆盖 user）；单文件内按 LIFO 匹配；`Rule(tool_name, pattern, effect)` 用 `fnmatch` 做 glob 匹配主参数；`evaluate(tool_name, content)` 命中返回 `"allow"` / `"deny"`，未命中返回 `None`；`append_local_rule(rule)` 写回本地规则文件。
- F6: 规则语法 `ToolName(pattern)` 用 `_RULE_RE = re.compile(r"^(\w+)\((.+)\)$")` 解析；`effect` 仅允许 `"allow"` / `"deny"`；YAML 列表结构 `[{rule: ..., effect: ...}, ...]`。
- F7: 内容字段提取 `extract_content(tool_name, arguments)`：`_CONTENT_FIELDS` 表把六个核心工具的「主参数字段」映射出来（Bash → `command`、ReadFile / WriteFile / EditFile → `file_path`、Glob / Grep → `pattern`），未识别工具返回空字符串。
- F8: `PermissionChecker.check(tool, arguments)` 按固定顺序逐层判定：Plan 模式豁免（特殊工具 + plan 文件写入）→ 安全命令直放 → 危险命令直拒 → 路径沙箱 → 规则引擎 → 模式矩阵兜底。Plan 模式下 `Agent` / `ToolSearch` / `AskUserQuestion` 与 plan 文件自身写入直接放行。
- F9: 自学习：HITL 用户选 `PermissionResponse.ALLOW_ALWAYS` 时，Agent 在执行前把当前 `tool_name` + 主参数（超过 60 字符截断 + `*` 通配）包成 `Rule` 调 `rule_engine.append_local_rule(rule)`，写到本地规则文件。

## 4. 非功能需求

- N1: 危险命令模式必须硬编码进 `codebot/permissions/dangerous.py` 的 `_DANGEROUS_PATTERNS`，不依赖外部下载或环境变量注入，避免被攻击者绕过。
- N2: 路径沙箱必须始终包含项目根 + `tempfile.gettempdir()`；额外路径在 `__init__` 时一次性 `Path.resolve()`，沙箱检查时再 `resolve(strict=True)` 解析符号链接，防止 symlink 换路径逃逸。
- N3: 规则文件解析必须在 YAML 语法错误 / 文件不存在 / 非列表结构 / 单条坏规则时静默跳过，不让单个坏规则导致整套规则失效（`_load_rules_file` 用 `try/except yaml.YAMLError, OSError` 兜底）。
- N4: `PermissionChecker.check` 是无副作用纯函数（除规则文件磁盘读），只读，不修改任何 in-memory 状态。
- N5: Plan 模式的工具豁免与 plan 文件豁免分支必须早于路径沙箱检查，避免 plan 模式下写 plan 文件被沙箱误拦。
- N6: HITL 链路必须是异步事件流：`Agent._execute_tool` 用 `asyncio.Future[PermissionResponse]` + `yield PermissionRequest(...)` 把决策权交给 TUI，TUI `set_result` 后 Agent 才继续，避免阻塞 Agent loop。

## 5. 设计概要

- 核心数据结构:
 - `DecisionEffect = Literal["allow", "deny", "ask"]` 与 `Decision(effect, reason)`（dataclass）。
 - `PermissionMode(str, Enum)` 六态枚举 + `_MODE_MATRIX[mode][category] -> effect`。
 - `PathSandbox`：持有已 `resolve` 的 `_allowed_roots: list[Path]`。
 - `Rule(tool_name, pattern, effect)`（frozen dataclass）+ `RuleEngine(user_path, project_path, local_path)`。
 - `PermissionChecker(detector, sandbox, rule_engine, mode)` + `plan_file_path` 字段。
- 主流程（一次 `check` 调用）:
 - `extract_content(tool.name, arguments)` 抽出主参数。
 - Plan 模式分支：白名单工具或 plan 文件写入 → `Decision("allow", ...)`。
 - 命令类工具：先 `is_safe_command` 直放；再 `detector.detect` 直拒。
 - 读 / 写工具：content 非空时走 `sandbox.check`。
 - 走 `rule_engine.evaluate`，命中按 effect 决定。
 - 落到 `mode_decide(self.mode, tool.category)` 兜底（`"allow"` / `"deny"` / `"ask"`）。
- 调用链:
 - `CodeBotApp._build_agent` 装配 → 构造 `PermissionChecker` + `PathSandbox` + `RuleEngine` → 传给 `Agent`。
 - `Agent._execute_tool` 执行工具前 → `self.permission_checker.check(...)` → `deny` 返回 `ToolResult(is_error=True)` / `ask` `yield PermissionRequest(...)` 走 HITL / `allow` 继续。
 - HITL 选 `ALLOW_ALWAYS` → `extract_content` + 截断 → `rule_engine.append_local_rule(rule)` 写本地。
 - `/plan` 命令切到 `PermissionMode.PLAN` + Agent 自动生成 `_plan_path_cache` 设置 `permission_checker.plan_file_path`；`/do` 通过 `PlanChoice.YOLO` / `PlanChoice.MANUAL` 还原模式。
- 与其他模块的交互:
 - 依赖 `codebot.tools.base.Tool`（`name`、`category` 字段）。
 - 依赖 `PyYAML` 做规则文件序列化。
 - 被 `codebot.agent.Agent`、`codebot.app.CodeBotApp`、`codebot.permission_dialog.InlinePermissionWidget` 直接使用；子 Agent（`codebot.agents.fork`）继承父 Agent 的 `permission_checker`。

## 6. Out of Scope

- 不实现 LLM 分类器；本章纯静态规则。
- 不实现 PowerShell 危险命令检测，目前只覆盖 Bash。
- 不持久化 user / project 级别规则文件的写入，只写 local 规则文件。
- 不实现规则文件热重载（每次 `evaluate` 都读盘）。
- 不实现规则解释 UI 或可视化调试器。
- 不实现 Windows ACL / Linux capabilities 等 OS 级沙箱。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch06: 权限系统 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。本章为验收，所有任务已在 `origin/python` 分支落地。

## T1: 定义决策与模式枚举
- 影响文件: `codebot/permissions/modes.py:1-31`
- 依赖任务: 无
- 完成标准: `DecisionEffect = Literal["allow", "deny", "ask"]`（modes.py:8）；`PermissionMode(str, Enum)` 六态 `DEFAULT/ACCEPT_EDITS/PLAN/BYPASS/CUSTOM/DONT_ASK`（modes.py:11-17）；`_MODE_MATRIX` 决策表 6×3 全部填齐（modes.py:20-27）；`mode_decide(mode, category)` 直接索引矩阵（modes.py:30-31）。

## T2: 实现 Layer 1 危险命令检测
- 影响文件: `codebot/permissions/dangerous.py:5-15, 49-56`
- 依赖任务: 无
- 完成标准: `_DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]]` 8 条核心模式（`rm -rf /`、`mkfs.`、`dd if=...of=/dev/`、`chmod -R 777 /`、fork bomb `:()\{ :|:& \};:`、`curl|sh`、`wget|sh`、`> /dev/sd`，dangerous.py:5-15）；`DangerousCommandDetector.__init__` 支持 `extra_patterns` 注入；`detect(command)` 用 `pattern.search` 命中即返回 `(True, reason)`，否则 `(False, "")`（dangerous.py:49-56）。

## T3: 实现 Layer 1 安全命令白名单
- 影响文件: `codebot/permissions/dangerous.py:18-31, 34-44`
- 依赖任务: 无
- 完成标准: `_SAFE_COMMANDS` 列出 50+ 个只读命令前缀，覆盖 `ls / cat / git status / git log / go version / python --version` 等（dangerous.py:18-31）；`is_safe_command(command)` 先 `strip` 检查空字符串，再检查命令中不含 `|` / `;` / `&&` / `>` / `$(` / 反引号，再按精确匹配或 `startswith(safe + " ")` 命中前缀（dangerous.py:34-44）。

## T4: 实现 Layer 2 路径沙箱
- 影响文件: `codebot/permissions/sandbox.py:7-46`
- 依赖任务: 无
- 完成标准: `PathSandbox.__init__(project_root, extra_allowed=None)` 把 `Path(project_root).resolve()` + `Path(tempfile.gettempdir()).resolve()` + 所有 extra `.resolve()` 后存入 `_allowed_roots`（sandbox.py:8-17）；`check(path)` 先 `expanduser`，相对路径相对 `project_root` 拼接，调 `resolve(strict=True)` 解析 symlink，路径不存在时回退到 `parent.resolve(strict=True) / name`（sandbox.py:23-34）；遍历 `_allowed_roots` 用 `relative_to` 判定，全 miss 返回 `(False, "路径 {path} 超出沙箱范围")`（sandbox.py:36-46）。

## T5: 实现 Layer 3 规则引擎
- 影响文件: `codebot/permissions/rules.py:1-106`
- 依赖任务: 无
- 完成标准:
 - `Effect = Literal["allow", "deny"]`、`Rule(tool_name, pattern, effect)` 用 `@dataclass(frozen=True)`、`Rule.matches(tool_name, content)` 用 `fnmatch` 做 glob（rules.py:11, 26-35）。
 - `_RULE_RE = re.compile(r"^(\w+)\((.+)\)$")` 在 rules.py:13；`parse_rule(raw, effect)` 解析，非法语法 `raise ValueError`（rules.py:38-42）。
 - `_CONTENT_FIELDS` 6 工具映射 + `extract_content(tool_name, arguments)`（rules.py:15-23, 45-49）。
 - `_load_rules_file(path)` 处理不存在 / YAML 错 / 非列表 / 单条坏规则时静默跳过（rules.py:52-73）。
 - `RuleEngine.__init__` 接收 `user_rules_path / project_rules_path / local_rules_path`（rules.py:76-84）；`_load_tiers` 顺序 user → project → local（rules.py:86-90）；`evaluate(tool_name, content)` 遍历每层用 `reversed(rules)` LIFO 匹配，找到 effect 立即返回（rules.py:92-97）。
 - `append_local_rule(rule)` 自动 `parent.mkdir(parents=True, exist_ok=True)`，读出现有规则 append 后用 `yaml.dump` 全量重写（rules.py:99-106）。

## T6: 实现 Decision 与 Plan 模式豁免
- 影响文件: `codebot/permissions/checker.py:1-92`
- 依赖任务: T1~T5
- 完成标准:
 - `Decision(effect, reason)` 用 `@dataclass`（checker.py:14-17）；`_PLAN_MODE_ALLOWED_TOOLS = frozenset({"Agent", "ToolSearch", "AskUserQuestion"})`（checker.py:13）。
 - `PermissionChecker.__init__(detector, sandbox, rule_engine, mode=PermissionMode.DEFAULT)` 初始化 `self.plan_file_path = ""`（checker.py:20-32）。
 - `_is_plan_file(target_path)` 多策略：basename 落在 `.codebot/plans/` 直放；否则 `os.path.abspath` 双向匹配 / basename 相等（checker.py:82-92）。

## T7: 实现主入口 check
- 影响文件: `codebot/permissions/checker.py:34-80`
- 依赖任务: T6
- 完成标准: `check(tool, arguments)` 按 spec.md F8 的 6 步顺序执行：
 1. `extract_content` 抽 content（checker.py:35）。
 2. Plan 模式：白名单工具直放 / WriteFile / EditFile 落在 plan 文件直放（checker.py:38-44）。
 3. `tool.category == "command"` 时先 `is_safe_command` 直放（checker.py:47-48）。
 4. `tool.category == "command"` 时 `detector.detect(content)` 直拒（checker.py:51-54）。
 5. `tool.category in ("read", "write")` 且 content 非空 → `sandbox.check` 不通过直拒（checker.py:57-60）。
 6. `rule_engine.evaluate` 命中按 effect 决定（checker.py:63-67）。
 7. `mode_decide(self.mode, tool.category)` 兜底，返回 allow / deny / ask（checker.py:70-77）。
 - 每个分支 `Decision.reason` 写明决策来源：`"Safe read-only command"` / `"危险命令拦截: ..."` / `"路径沙箱拦截: ..."` / `"权限规则放行"` / `"权限规则拒绝"` / `"权限模式 {mode} 放行/拒绝"` / `"需要用户确认"`。

## T8: 接入 Agent Loop 的工具执行
- 影响文件:
 - `codebot/agent.py:125-135` 定义 `PermissionResponse` 三态 + `PermissionRequest(tool_name, description, future)`。
 - `codebot/agent.py:292-305` `Agent.__init__` 接收 `permission_checker` 参数。
 - `codebot/agent.py:352-355` `set_permission_mode(mode)` 同时更新 checker。
 - `codebot/agent.py:476-478` Plan 模式给 `permission_checker.plan_file_path` 注入实际 plan 路径。
 - `codebot/agent.py:814-852` `_execute_tool` 调 `checker.check` → deny 返 `ToolResult(is_error=True)` / ask `yield PermissionRequest(...)` 等 future / `ALLOW_ALWAYS` 自动 append_local_rule。
- 依赖任务: T7
- 完成标准: 用户切模式 / Plan 模式注入 / 工具调用前权限检查 / HITL 选 `ALLOW_ALWAYS` 四条主路径全部接到 `PermissionChecker.check` 与 `RuleEngine.append_local_rule`。

## T9: 接入 TUI 装配与模式切换
- 影响文件:
 - `codebot/app.py:60-64` import `DangerousCommandDetector / PathSandbox / PermissionChecker / RuleEngine`。
 - `codebot/app.py:623-632` `CodeBotApp._build_agent` 构造 `PermissionChecker`，`RuleEngine` 注入 `user_rules_path=home/.codebot/permissions.yaml` + `project_rules_path=work_dir/.codebot/permissions.yaml` + `local_rules_path=work_dir/.codebot/permissions.local.yaml`。
 - `codebot/app.py:985-994` `action_cycle_mode` 实现 Shift+Tab 切换。
 - `codebot/app.py:1341-1346` `/do` 命令 → `PlanChoice.YOLO / MANUAL` 还原模式。
 - `codebot/permission_dialog.py:11-15` `_PERM_OPTIONS` 三选项分别映射 `ALLOW / ALLOW_ALWAYS / DENY`。
- 依赖任务: T8
- 完成标准: TUI 启动后构造 Checker → Shift+Tab 循环模式 → `/plan` + `/do` 切 Plan ↔ Default/Bypass → `InlinePermissionWidget` 三选项与 `PermissionResponse` 对应。

## T10: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T9
- 完成标准:
 - `ruff check codebot/permissions/` 通过。
 - `pytest tests/test_permissions.py -v` 全绿（覆盖 5 大测试类 + 6 个 e2e 异步测试，共 35+ 用例）。
 - 手动场景:
 1. 默认模式下让 Agent 跑 `Bash(command="rm -rf /")` → `ToolResult.output` 含 `"Permission denied: 危险命令拦截: 递归强制删除根目录"`。
 2. 默认模式下让 Agent `ReadFile(file_path="/etc/passwd")` → 沙箱 Deny，错误信息含 `"沙箱"`。
 3. 默认模式下让 Agent `WriteFile` 到工作目录内 → 触发 `PermissionRequest`；TUI 选 `ALLOW_ALWAYS` → `.codebot/permissions.local.yaml` 出现 `WriteFile(<path>*)` 规则；下次同路径写不再 Ask。
 4. `/plan` 进入 Plan 模式 → `WriteFile` 写非 plan 文件被 Deny；写 `_plan_path_cache` 指向的 plan 文件被 Allow。
 5. Shift+Tab 切到 `BYPASS` → `rm -rf /` 仍被 Deny（Layer 1 在模式矩阵之前，不可绕过）；普通 `WriteFile` 直接 Allow。

## 进度
- [ ] T1 决策 + 模式枚举
- [ ] T2 危险命令检测
- [ ] T3 安全命令白名单
- [ ] T4 路径沙箱
- [ ] T5 规则引擎
- [ ] T6 Decision + Plan 豁免
- [ ] T7 主入口 check
- [ ] T8 接入 Agent Loop
- [ ] T9 接入 TUI 装配
- [ ] T10 端到端验证（ruff + pytest + Agent loop 与 TUI 调用链确认）

```

```markdown
# ch06: 权限系统 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性
- [ ] 类型别名 `DecisionEffect = Literal["allow", "deny", "ask"]` 在 `codebot/permissions/modes.py:8`（`grep -n "DecisionEffect" codebot/permissions/modes.py`）
- [ ] 枚举 `PermissionMode(str, Enum)` 与六态 `DEFAULT/ACCEPT_EDITS/PLAN/BYPASS/CUSTOM/DONT_ASK` 在 `codebot/permissions/modes.py:11-17`
- [ ] 决策矩阵 `_MODE_MATRIX` 在 `codebot/permissions/modes.py:20-27`，6×3 共 18 格全填齐
- [ ] `mode_decide(mode, category)` 在 `codebot/permissions/modes.py:30-31`，直接索引矩阵
- [ ] `_DANGEROUS_PATTERNS` 在 `codebot/permissions/dangerous.py:5-15` 列出 8 条；`DangerousCommandDetector.detect` 在 `dangerous.py:49-56` 用 `pattern.search`
- [ ] `_SAFE_COMMANDS` 在 `codebot/permissions/dangerous.py:18-31` 列出 50+ 条；`is_safe_command` 在 `dangerous.py:34-44`，命令中含 `|;&&>$(` 反引号一律拒
- [ ] `PathSandbox.__init__ / check` 在 `codebot/permissions/sandbox.py:7-46`，默认包含 `tempfile.gettempdir()` 与 `project_root`，`check` 用 `resolve(strict=True)` 解 symlink
- [ ] `Rule(tool_name, pattern, effect)` 用 `@dataclass(frozen=True)` 在 `codebot/permissions/rules.py:26-35`，`matches` 用 `fnmatch`
- [ ] `_RULE_RE = re.compile(r"^(\w+)\((.+)\)$")` 在 `codebot/permissions/rules.py:13`，`parse_rule` 在 `rules.py:38-42` 非法语法 `raise ValueError`
- [ ] `_CONTENT_FIELDS` 6 工具映射 + `extract_content` 在 `codebot/permissions/rules.py:15-23, 45-49`
- [ ] `RuleEngine` + `_load_tiers` + `evaluate` + `append_local_rule` 在 `codebot/permissions/rules.py:76-106`，单层用 `reversed(rules)` 实现 LIFO
- [ ] `Decision` dataclass + `_PLAN_MODE_ALLOWED_TOOLS = {"Agent", "ToolSearch", "AskUserQuestion"}` 在 `codebot/permissions/checker.py:13-17`
- [ ] `PermissionChecker.check` 主入口在 `codebot/permissions/checker.py:34-80`，按 Plan 豁免 → 安全命令 → 危险命令 → 沙箱 → 规则 → 模式 6 步顺序判定
- [ ] `_is_plan_file` 多策略匹配在 `codebot/permissions/checker.py:82-92`：abspath 相等 / basename 相等 / 路径含 `.codebot/plans/`
- [ ] Plan 模式豁免分支早于沙箱检查（`checker.py:38-44`）
- [ ] `codebot/permissions/__init__.py:1-19` 导出 `Decision / DecisionEffect / DangerousCommandDetector / PathSandbox / PermissionChecker / PermissionMode / Rule / RuleEngine / extract_content / mode_decide / parse_rule`

## 2. 接入完整性（必查，杜绝死代码）
- [ ] `grep -rn "PermissionChecker(" codebot/ --include="*.py"` 至少 1 处真实调用（`codebot/app.py:623`）
- [ ] `grep -rn "PathSandbox(" codebot/ --include="*.py"` 至少 1 处（`codebot/app.py:625`）
- [ ] `grep -rn "RuleEngine(" codebot/ --include="*.py"` Engine 构造在 App 装配（`codebot/app.py:626-630`）；测试中多处构造（`tests/test_permissions.py`）
- [ ] `grep -rn "permission_checker.check\b" codebot/ --include="*.py"` 主流程调用方在 `codebot/agent.py:815, 1066`（双路径：交互式 + 子 Agent）
- [ ] `grep -rn "append_local_rule" codebot/ --include="*.py"` 主流程调用方在 `codebot/agent.py:852`
- [ ] `grep -rn "PermissionMode\." codebot/ --include="*.py"` 在 `codebot/app.py:631, 855, 988, 994, 1341, 1346, 1636`、`codebot/agent.py:305, 330, 354, 1073` 等至少 10 处使用，覆盖创建 / 切换 / 渲染各路径
- [ ] `grep -rn "extract_content" codebot/ --include="*.py"` 在 `codebot/agent.py:848` HITL 自学习 + `codebot/permissions/checker.py:35` 主流程 + `codebot/permissions/rules.py:45` 定义共 3 处
- [ ] 配置接入：`codebot/app.py:626-630` 默认配置 `user_rules_path=home/.codebot/permissions.yaml`、`project_rules_path=work_dir/.codebot/permissions.yaml`、`local_rules_path=work_dir/.codebot/permissions.local.yaml`
- [ ] HITL 链路：`PermissionChecker.check` 返回 `effect="ask"` → `codebot/agent.py:828-852` 通过 `PermissionRequest(tool_name, description, future)` 走 ch04 事件循环 → `codebot/permission_dialog.py:11-15 InlinePermissionWidget` 渲染 3 选项 → 用户选 `ALLOW_ALWAYS` 时回灌 `append_local_rule`（`agent.py:847-852`）
- [ ] 命令注册：`/mode` / `/plan` / `/do` 命令处理器在 `codebot/commands/handlers/permission.py`、`plan.py`、`do.py`，用于切换 `PermissionMode`

## 3. 编译与测试
- [ ] `ruff check codebot/permissions/` 无错误
- [ ] `pytest tests/test_permissions.py -v` 全绿（覆盖 `TestDangerousCommandDetector` / `TestPathSandbox` / `TestRuleEngine` / `TestPermissionMode` / `TestPermissionChecker` 5 个测试类 + `test_e2e_dangerous_command_blocked_loop_continues` / `test_e2e_sandbox_blocks_outside_path` / `test_e2e_rule_allows_git` / `test_e2e_default_mode_write_triggers_ask` / `test_e2e_bypass_mode_allows_all` / `test_e2e_user_denies_operation` 6 个 e2e 异步测试）
- [ ] `mypy codebot/permissions/` 无类型错误（如配置启用）

## 4. 端到端验证
- [ ] TUI 启动并加载 provider 后 `CodeBotApp._build_agent` 构造 `PermissionChecker`（`codebot/app.py:623-632`），传入 `Agent(permission_checker=checker, ...)`（`app.py:654`）
- [ ] Plan Mode：`/plan` → `Agent.set_permission_mode(PermissionMode.PLAN)` + Agent loop 给 `permission_checker.plan_file_path = str(self._get_plan_path())`（`agent.py:476-478`）；下一轮 `WriteFile` 调 `check`，非 plan 文件被 Deny
- [ ] HITL：默认模式下让模型写新文件，TUI 弹三选项 `Yes / Yes, and don't ask again for this pattern / No`（`codebot/permission_dialog.py:11-15`），与 `PermissionResponse.ALLOW / ALLOW_ALWAYS / DENY` 对应
- [ ] 自学习：选 `ALLOW_ALWAYS` 时 `agent.py:847-852` 用 `extract_content` + 截断 60 字符 + `*` 通配生成 `Rule`，append 到 `.codebot/permissions.local.yaml`
- [ ] 危险命令防御不可绕过：`PermissionMode.BYPASS` 时 `checker.py:51-54` 在模式矩阵之前先 `detector.detect`，让 Agent 跑 `rm -rf /` 仍 Deny（`tests/test_permissions.py:test_bypass_still_blocks_dangerous` 已覆盖）
- [ ] 留存证据: 验收阶段未自动保存日志；如需补，在 `.codebot/permissions.local.yaml` 中观察 `ALLOW_ALWAYS` 写入的 YAML 列表项 `[{rule: "WriteFile(...)", effect: "allow"}, ...]`

## 5. 文档
- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`docs/python/ch06/`）
- [ ] commit 信息标注 `ch06` 与三件套关闭状态（待统一打包提交）

```

### Java

```markdown
# ch06: 权限系统 Spec

## 1. 背景

工具系统（ch03）放出了 Bash 和写文件的能力，Agent Loop（ch04）能自主决定调谁；没有权限层，模型一句话就能 `rm -rf /` 或者写到项目目录之外。一个生产级 Coding Agent 的最低安全要求是「至少要拦得住明显的危险操作 + 把不熟悉的操作交给用户决定」。本章把这条防御线做出来：明显错的直接拦、明显对的直接放，剩下的让规则 / 模式 / HITL 决定。

## 2. 目标

对外提供 `PermissionChecker`：调用方传入模式 + 项目根目录构造好之后，对任意 `Tool` + 参数 `Map<String, Object>` 调一次 `check` 拿到 `CheckResult(decision, reason)`。`StreamingExecutor` 直接用这个决策决定是否拦截 / 直接执行 / 走 HITL。权限模式支持 DEFAULT / ACCEPT_EDITS / PLAN / BYPASS 四种，TUI 用 Shift+Tab 或 `/plan` `/do` 在它们之间切换；PLAN 模式拥有特殊豁免分支。HITL 用户选 `ALLOW_ALWAYS` 时把规则 append 到本地规则文件并热重载。

## 3. 功能需求

- F1: 提供权限模式枚举（`DEFAULT` / `ACCEPT_EDITS` / `PLAN` / `BYPASS`）与模式 × 工具类别（READ / WRITE / COMMAND）的决策矩阵，通过 `PermissionMode.decide(ToolCategory)` 暴露查表。
- F2: Layer 1 危险命令检测：硬编码覆盖 `rm -rf /` 类删除、`mkfs.` 磁盘格式化、`dd ... of=/dev/` 设备写入、`chmod -R 777 /`、fork bomb、`curl | sh` / `wget | sh` 类远程执行、`> /dev/sd` 共 8 条核心模式，命中即拒绝并给出原因。
- F3: Layer 1 安全命令白名单：维护只读 Bash 命令前缀表（`ls` / `pwd` / `cat` / `git status` / `go version` 等），命中且命令中不含管道 / 重定向 / 子 shell / 命令分隔符 / 命令替换时直接放行。
- F4: Layer 2 路径沙箱：构造时持有项目根目录 `projectRoot`；`isPathAllowed(pathStr)` 对入参做 `Path.toAbsolutePath().normalize()` 后判断是否 `startsWith(projectRoot)` 或 `startsWith(/tmp)`，仅对 `ReadFile` / `WriteFile` / `EditFile` 三个路径工具生效。
- F5: Layer 3 规则引擎：管理 user / project / local 三层 YAML 规则文件，加载顺序 user → project → local，整体合并到 `fileRules` 列表；`check` 时从尾向前 LIFO 匹配；`PermissionRule(toolName, pattern, effect)` 用 `PathMatcher("glob:" + pattern)` 匹配主参数；提供 `appendLocalRule(toolName, pattern)` 写回本地规则文件并热重载。
- F6: 规则语法 `ToolName(pattern)` 用正则 `^(\w+)\((.+)\)$` 解析，effect 仅 `allow` / `deny`；YAML 文件结构为 `[{rule, effect}, ...]` 列表。
- F7: 内容字段提取：`CONTENT_FIELDS` 映射 6 个核心工具到主参数字段名（Bash→`command`、ReadFile/WriteFile/EditFile→`file_path`、Glob/Grep→`pattern`），其他工具返回 `null`。
- F8: `PermissionChecker.check` 按固定顺序逐层判定：PLAN 模式豁免（白名单工具 + plan 文件路径写入）→ 安全命令直放 → 危险命令直拒 → 路径沙箱 → 文件规则 LIFO → 会话级 `allowAlwaysRules` → 模式矩阵兜底。
- F9: 会话级自学习：HITL 用户选 `ALLOW_ALWAYS` 时，`StreamingExecutor` 调用 `checker.addAllowAlwaysRule(toolName, content)` 把当前调用注册到内存 Set（即时生效），可选地由调用方再走 `appendLocalRule` 持久化到本地 YAML。

## 4. 非功能需求

- N1: 危险命令模式必须以 `List<Pattern>` 静态常量硬编码进代码（`DANGEROUS_PATTERNS`），不依赖外部下载或环境变量注入，避免被攻击者绕过。
- N2: 路径沙箱必须始终包含项目根 + `/tmp`；构造时把 `projectRoot` 存为 `Path` 字段，`isPathAllowed` 内对入参做 `toAbsolutePath().normalize()`，防止相对路径 / `..` 绕过。
- N3: 规则文件解析必须在 YAML 语法错误 / 文件不存在 / 类型不匹配时静默返回 `List.of()`，不让单个坏规则导致整套规则失效。`loadRulesFile` 内 try/catch 包住 `Yaml.load`。
- N4: `check` 是无副作用方法（除 `appendLocalRule` 的磁盘写入），只读 `fileRules` 与 `allowAlwaysRules`，不修改任何 in-memory 状态。
- N5: PLAN 模式的工具豁免分支必须早于沙箱检查，避免 plan 模式下写 `.codebot/plans/` 下的文件被沙箱误拦。

## 5. 设计概要

- 核心数据结构:
 - `PermissionMode.Decision`（`ALLOW` / `DENY` / `ASK`）与 `PermissionChecker.CheckResult(decision, reason)`。
 - `PermissionMode` 枚举与 `decide(ToolCategory)` 内嵌的 mode × category 决策表（`switch` 表达式）。
 - `PermissionResponse` 枚举（`ALLOW` / `ALLOW_ALWAYS` / `DENY`）用于 HITL 回调。
 - `PermissionRule(toolName, pattern, effect)` record + `RuleEffect` 内部枚举。
 - `PermissionChecker{mode, projectRoot, fileRules, allowAlwaysRules, planFilePath}`。
 - `PlanFile`（`com.codebot.plan`）：静态字段缓存当前 plan 路径，`getOrCreatePlanPath` / `planExists` / `isPlanFilePath`。
- 主流程（一次 `check` 调用）:
 - 用 `extractContent(toolName, args)` 抽出主参数 content。
 - PLAN 模式分支：`PLAN_MODE_ALLOWED_TOOLS`（Agent / ToolSearch / AskUserQuestion）或 `file_path` 包含 `.codebot/plans/` 时直接 `allow`。
 - Bash 工具：先 `isSafeCommand` 直放，再遍历 `DANGEROUS_PATTERNS` 直拒。
 - 路径类工具：`isPathTool` 命中后走 `isPathAllowed`。
 - 文件规则：`fileRules` 从尾向前匹配，命中按 `RuleEffect` 决定。
 - 会话级 allow-always：`allowAlwaysRules` 中 `toolName + ":" + content` 命中直放。
 - 落到 `mode.decide(tool.category())` 兜底。
- 调用链:
 - `CodeBotModel` 启动后构造 `new PermissionChecker(PermissionMode.DEFAULT, Path.of(workDir))` 并通过 `agent.setChecker(...)` 装配。
 - `StreamingExecutor.executeSingle` 执行工具前 → `checker.check(tool, args)` → `DENY` 给错误结果 / `ASK` 走 `PermissionRequestEvent` 与 `CompletableFuture<PermissionResponse>` 等待 5 分钟。
 - HITL 选 `ALLOW_ALWAYS` → `checker.addAllowAlwaysRule(toolName, content)`。
 - `/plan` 命令切到 `PermissionMode.PLAN` + 设置 plan 路径；`/do` 还原 `prePlanMode`。
 - Shift+Tab 在 4 种模式间循环切换。
- 与其他模块的交互:
 - 依赖 `com.codebot.tool.Tool`（`name()` / `category()` 接口）和 `ToolCategory`。
 - 依赖 SnakeYAML（`org.yaml.snakeyaml.Yaml`）做规则文件序列化。
 - 被 `com.codebot.agent.Agent` 与 `StreamingExecutor` 直接使用；被 `CodeBotModel` 装配并响应 `PermissionRequestEvent`。
 - Plan 模式 reminder 通过 `PlanModePrompt.buildReminder` 在 `Agent.agentLoop` 每轮注入到对话。

## 6. Out of Scope

- 不实现 LLM 分类器；本章纯静态规则。
- 不实现 PowerShell 危险命令检测，目前只覆盖 Bash。
- 不实现 user 与 project 级规则文件的写入，只写本地规则文件。
- 不实现规则文件热重载（仅 `appendLocalRule` 写入后手动 reload，其他改动需要重启）。
- 不实现目标设计中的额外模式（dontAsk / auto / bubble）。
- 不实现规则解释 UI（只在 reason 字符串里附原因）。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch06: 权限系统 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。本章为验收，所有任务已经在仓库里落地（`origin/java`）。

## T1: 定义决策与模式枚举
- 影响文件:
 - `src/main/java/com/codebot/permission/PermissionMode.java:5-29`
 - `src/main/java/com/codebot/permission/PermissionResponse.java:3-7`
 - `src/main/java/com/codebot/permission/PermissionChecker.java:100-104`
- 依赖任务: 无
- 完成标准: `PermissionMode` 枚举四态 `DEFAULT / ACCEPT_EDITS / PLAN / BYPASS`；`PermissionMode.Decision` 三态 `ALLOW / DENY / ASK`；`PermissionMode.decide(ToolCategory)` 用 `switch` 表达式覆盖 4×3=12 格（PLAN 复用 DEFAULT 的判定）；`PermissionResponse` 三态 `ALLOW / ALLOW_ALWAYS / DENY`；`PermissionChecker.CheckResult(decision, reason)` 提供 `allow()` / `deny(reason)` / `ask()` 静态工厂。

## T2: 实现 Layer 1 危险命令检测
- 影响文件: `src/main/java/com/codebot/permission/PermissionChecker.java:70-79, 128-135`
- 依赖任务: 无
- 完成标准: `DANGEROUS_PATTERNS` 静态常量列出 8 条 `java.util.regex.Pattern`（`rm -rf /`、`mkfs.`、`dd if=...of=/dev/`、`chmod -R 777 /`、fork bomb `:(){:|:&};:`、`curl ... | sh`、`wget ... | sh`、`> /dev/sd`）；`check` 内 Bash 分支遍历该列表，命中即返回 `CheckResult.deny("Dangerous command detected: " + pattern.pattern())`。

## T3: 实现 Layer 1 安全命令白名单
- 影响文件: `src/main/java/com/codebot/permission/PermissionChecker.java:56-68, 292-304`
- 依赖任务: 无
- 完成标准: `SAFE_COMMANDS` 是 `Set.of(...)`，覆盖 50+ 个只读命令前缀（含 `git status` 等 git 只读子命令、`go version`、`java -version` 等）；`isSafeCommand(command)` 检查 trimmed 命令不含 `|` / `;` / `&&` / `>` / `$(` / 反引号，且 `equals` 或 `startsWith(safe + " ")` 任一前缀，才返回 `true`。

## T4: 实现 Layer 2 路径沙箱
- 影响文件: `src/main/java/com/codebot/permission/PermissionChecker.java:23-24, 90-94, 137-142, 306-319`
- 依赖任务: 无
- 完成标准: 构造函数接收 `Path projectRoot` 并保存为字段；`isPathTool(toolName)` 判定是否 `ReadFile` / `WriteFile` / `EditFile`；`isPathAllowed(pathStr)` 将入参与 `projectRoot`、`/tmp` 全部 `toAbsolutePath().normalize()` 后做 `startsWith` 检查，任一匹配即放行；异常情况返回 `true`（保守不拦，由后续层兜底）。

## T5: 实现 Layer 3 规则引擎
- 影响文件: `src/main/java/com/codebot/permission/PermissionChecker.java:28-46, 144-160, 175-227, 229-290`
- 依赖任务: 无
- 完成标准:
 - `PermissionRule(toolName, pattern, effect)` 是 `private record`，`matches(toolName, content)` 用 `FileSystems.getDefault().getPathMatcher("glob:" + pattern)` 做 glob，匹配失败兜底为 `content.equals(pattern)`。
 - `RuleEffect { ALLOW, DENY }` 是 `private enum`。
 - `loadRules()` 顺序加载 `~/.codebot/permissions.yaml`、`{projectRoot}/.codebot/permissions.yaml`、`{projectRoot}/.codebot/permissions.local.yaml`，合并到 `fileRules` 列表。
 - `check` 内 `for (int i = fileRules.size() - 1; i >= 0; i--)` LIFO 匹配。
 - `loadRulesFile(path)` 用 SnakeYAML 解析 `List<Map<String,String>>` 形式，YAML 异常 / 类型错误 / 规则格式错误均静默 `continue` 或返回 `List.of()`；`RULE_PATTERN = "^(\\w+)\\((.+)\\)$"` 正则解析 `ToolName(pattern)`。
 - `appendLocalRule(toolName, pattern)` 自动 `Files.createDirectories(localFile.getParent())`，合并现有规则后用 `Yaml.dump` 重写本地 YAML，并 `fileRules.clear(); fileRules.addAll(loadRules())` 热重载。

## T6: 实现内容字段提取
- 影响文件: `src/main/java/com/codebot/permission/PermissionChecker.java:81-88, 321-326, 328-331`
- 依赖任务: 无
- 完成标准: `CONTENT_FIELDS` 是 `Map.of(...)`，覆盖 Bash→`command`、ReadFile/WriteFile/EditFile→`file_path`、Glob/Grep→`pattern` 共 6 项；`extractContent(toolName, args)` 查表后从 `args` 取出对应字段（仅当值是 `String`）；`stringArg(args, key, default)` 提供安全字符串读取。

## T7: 实现主入口 PermissionChecker.check
- 影响文件: `src/main/java/com/codebot/permission/PermissionChecker.java:106-169`
- 依赖任务: T1~T6
- 完成标准: `check(Tool tool, Map<String, Object> args)` 按以下顺序逐层判断：
 1. Layer 0 PLAN 模式豁免（`PLAN_MODE_ALLOWED_TOOLS` 集合 + `WriteFile/EditFile` 写 `.codebot/plans/` 路径）。
 2. Layer 1 Bash 安全命令直放。
 3. Layer 2 Bash 危险命令直拒。
 4. Layer 3 路径沙箱（仅 `isPathTool` 命中时）。
 5. Layer 4 文件规则 LIFO 匹配。
 6. Layer 4b 会话级 `allowAlwaysRules` 命中直放。
 7. Layer 5 `mode.decide(tool.category())` 兜底。
 - `reason` 字段写明决策来源（"Dangerous command detected: ..." / "Path outside allowed sandbox: ..." / "Denied by rule: ..." / "Denied by permission mode: ..."）。

## T8: 实现 PLAN 模式豁免与 PlanFile
- 影响文件:
 - `src/main/java/com/codebot/permission/PermissionChecker.java:52-54, 110-121`
 - `src/main/java/com/codebot/plan/PlanFile.java:54-67, 116-123`
- 依赖任务: T7
- 完成标准: `PLAN_MODE_ALLOWED_TOOLS = Set.of("Agent", "ToolSearch", "AskUserQuestion")`；`check` 在 PLAN 分支判断 `WriteFile`/`EditFile` 的 `file_path` 是否 `contains(".codebot/plans/")`；`PlanFile.getOrCreatePlanPath(workDir)` 在 `.codebot/plans/` 下生成 `<adj>-<noun>-<MMdd-HHmm>.md` slug；`PlanFile.isPlanFilePath(target, plan)` 多策略匹配 normalize 后相等或 endsWith。

## T9: Plan 模式 reminder 注入
- 影响文件:
 - `src/main/java/com/codebot/prompt/PlanModePrompt.java:7-176`
 - `src/main/java/com/codebot/agent/Agent.java:106-114`
- 依赖任务: T7, T8
- 完成标准: `PlanModePrompt.buildReminder(planPath, planExists, iteration)` 在 iteration=1 注入完整 5-phase 工作流提示，其他轮次按 `REMINDER_INTERVAL=5` 节奏注入完整或精简提示；`Agent.agentLoop` 每轮检查 `checker.getMode() == PermissionMode.PLAN`，调用 `PlanFile.getOrCreatePlanPath` 后通过 `conv.addSystemReminder(reminder)` 注入。

## T10: 接入主流程
- 影响文件:
 - `src/main/java/com/codebot/agent/StreamingExecutor.java:91-123, 159-169`（权限拦截与 HITL 流程）
 - `src/main/java/com/codebot/tui/CodeBotModel.java:77, 429-433`（构造 Checker 并注入 Agent）
 - `src/main/java/com/codebot/tui/CodeBotModel.java:575-585`（Shift+Tab 循环切模式）
 - `src/main/java/com/codebot/tui/CodeBotModel.java:893-916`（`/plan` 与 `/do` 切换 + plan 路径）
 - `src/main/java/com/codebot/tui/dialog/PlanApprovalDialog.java`（plan 完成后 YOLO/Manual/Feedback 三选项）
 - `src/main/java/com/codebot/command/CommandRegistry.java:203-242`（`/plan` `/do` `/permission` 命令注册）
- 依赖任务: T1~T9
- 完成标准: 用户切换模式 / 进入 PLAN 模式 / 工具调用 / HITL 选 `ALLOW_ALWAYS` 四条主路径全部接到 `PermissionChecker.check` 与 `addAllowAlwaysRule`；`StreamingExecutor` 在 `ASK` 分支发 `PermissionRequestEvent` 并用 `CompletableFuture<PermissionResponse>.get(5, TimeUnit.MINUTES)` 阻塞等待。

## T11: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T10
- 完成标准:
 - `./gradlew build` 通过（顶层命令，已验证）
 - 手动场景:
 1. 在 TUI 默认模式下，发送让 Agent 跑 `rm -rf /` → 工具结果应是 `Permission denied: Dangerous command detected: ...`。
 2. 在 TUI 中让 Agent 写一个工作目录外的文件 `/etc/passwd` → 应被沙箱拒绝 `Path outside allowed sandbox: ...`。
 3. 在 TUI 中让 Agent 写工作目录内的文件 → DEFAULT 模式触发 `ASK`；HITL 选 `ALLOW_ALWAYS` → 同会话内同路径再写直接 `ALLOW`。
 4. `/plan` 进入 PLAN 模式 → Agent 调 WriteFile 写非 plan 文件被沙箱或模式 deny；写 `.codebot/plans/<slug>.md` 被 Allow。
 5. Shift+Tab 切到 `BYPASS` → 危险命令仍被拦（Layer 1/2 不可绕过），普通 Write 直接 `ALLOW`。

## 进度
- [ ] T1 决策 + 模式枚举
- [ ] T2 危险命令检测
- [ ] T3 安全命令白名单
- [ ] T4 路径沙箱
- [ ] T5 规则引擎
- [ ] T6 内容字段提取
- [ ] T7 主入口 PermissionChecker.check
- [ ] T8 PLAN 模式豁免与 PlanFile
- [ ] T9 Plan 模式 reminder 注入
- [ ] T10 主流程接入
- [ ] T11 端到端验证（构建通过 + Agent loop 与 TUI 调用链确认）

```

```markdown
# ch06: 权限系统 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性
- [ ] 枚举 `PermissionMode` 四常量 `DEFAULT / ACCEPT_EDITS / PLAN / BYPASS` 在 `src/main/java/com/codebot/permission/PermissionMode.java:5-10`（`grep -n "DEFAULT\|ACCEPT_EDITS\|PLAN\|BYPASS" src/main/java/com/codebot/permission/PermissionMode.java`）
- [ ] 内嵌枚举 `PermissionMode.Decision` 三态 `ALLOW / DENY / ASK` 在 `PermissionMode.java:27-29`
- [ ] `PermissionMode.decide(ToolCategory)` 决策矩阵在 `PermissionMode.java:12-25`，覆盖 4 模式 × 3 类别共 12 个组合（PLAN 复用 DEFAULT 判定）
- [ ] 枚举 `PermissionResponse` 三态 `ALLOW / ALLOW_ALWAYS / DENY` 在 `src/main/java/com/codebot/permission/PermissionResponse.java:3-7`
- [ ] `DANGEROUS_PATTERNS` 静态常量 8 条正则在 `PermissionChecker.java:70-79`
- [ ] `SAFE_COMMANDS` 静态常量 50+ 条前缀在 `PermissionChecker.java:56-68` + `isSafeCommand` 实现在 `PermissionChecker.java:292-304`
- [ ] `projectRoot` 字段 + 构造函数注入在 `PermissionChecker.java:24, 90-94`，`isPathTool` 与 `isPathAllowed` 在 `:306-319`，默认包含 `/tmp` 与 `projectRoot`
- [ ] `PermissionRule` record + `RuleEffect` 枚举在 `PermissionChecker.java:32-50`，glob 用 `FileSystems.getDefault().getPathMatcher("glob:" + pattern)`
- [ ] 三层规则加载（user / project / local）在 `PermissionChecker.java:187-206`，`appendLocalRule` 在 `:208-227`，`loadRulesFile` 在 `:239-290`
- [ ] `RULE_PATTERN` 正则 `^(\\w+)\\((.+)\\)$` 在 `PermissionChecker.java:177`
- [ ] `CONTENT_FIELDS` 6 工具映射在 `PermissionChecker.java:81-88` + `extractContent` 在 `:321-326`
- [ ] `CheckResult` record + `allow/deny/ask` 工厂在 `PermissionChecker.java:100-104`
- [ ] `check` 主入口在 `PermissionChecker.java:106-169`，按 Layer 0→5 顺序排布
- [ ] `PLAN_MODE_ALLOWED_TOOLS` 集合 `{"Agent","ToolSearch","AskUserQuestion"}` 在 `PermissionChecker.java:52-54`，PLAN 豁免分支在 `:110-121` 早于沙箱
- [ ] `PlanFile.isPlanFilePath(target, plan)` 多策略匹配在 `src/main/java/com/codebot/plan/PlanFile.java:116-123`：normalize 相等 / endsWith 命中
- [ ] 五层防御按序：PLAN 豁免 → 安全命令 → 危险命令 → 沙箱 → 文件规则 → 会话级 allow-always → 模式（`PermissionChecker.java:106-169`）
- [ ] `PlanModePrompt.buildReminder(planPath, planExists, iteration)` 在 `src/main/java/com/codebot/prompt/PlanModePrompt.java:141-161`，按 `REMINDER_INTERVAL=5` 切换完整与精简提示

## 2. 接入完整性（必查，杜绝死代码）
- [ ] `grep -rn "new PermissionChecker" --include="*.java" src/main` 至少 1 处真实调用（`src/main/java/com/codebot/tui/CodeBotModel.java:429`）
- [ ] `grep -rn "checker.check\|permChecker" --include="*.java" src/main` Agent 与 TUI 调用方均覆盖（`StreamingExecutor.java:91-123`、`CodeBotModel.java:77/429/575-585/893-916`）
- [ ] `grep -rn "PermissionMode.PLAN\|PermissionMode.DEFAULT\|PermissionMode.ACCEPT_EDITS\|PermissionMode.BYPASS" --include="*.java" src/main` 在 TUI 与 Agent 多处使用，覆盖创建 / 切换 / Plan 模式 reminder 注入
- [ ] `grep -rn "addAllowAlwaysRule\|appendLocalRule" --include="*.java" src/main` 主流程调用方在 `StreamingExecutor.java:114-118`
- [ ] `grep -rn "PermissionRequestEvent" --include="*.java" src/main` 至少 2 处：`StreamingExecutor.java:102` 发事件，`CodeBotModel.java` 处理事件
- [ ] `grep -rn "PlanModePrompt.buildReminder" --include="*.java" src/main` 主流程调用方在 `Agent.java:112`
- [ ] `grep -rn "PlanFile.getOrCreatePlanPath\|PlanFile.isPlanFilePath" --include="*.java" src/main` Agent 与 TUI 均覆盖（`Agent.java:109`、`CodeBotModel.java:897-913`）
- [ ] 配置接入：本地规则文件路径默认 `projectRoot.resolve(".codebot").resolve("permissions.local.yaml")` 在 `PermissionChecker.java:201, 210`
- [ ] HITL 链路：`PermissionChecker.check` 返回 `ASK` → `StreamingExecutor.java:99-119` 通过 `PermissionRequestEvent + CompletableFuture<PermissionResponse>` 走 ch04 事件循环 → TUI 渲染 3 选项 → 用户选 `ALLOW_ALWAYS` 回灌 `addAllowAlwaysRule`

## 3. 编译与测试
- [ ] `./gradlew build` 通过（顶层命令）
- [ ] `./gradlew test --tests "*Permission*"` 通过（如存在）。建议覆盖：`DANGEROUS_PATTERNS` 命中 / `isSafeCommand` 边界（含 `|` 拒绝）/ `isPathAllowed` (项目根内允许、外部拒绝、`/tmp` 允许) / `RULE_PATTERN` 解析 / `fileRules` LIFO 最后一条胜出 / `extractContent` 6 工具 / `mode.decide` 4×3 矩阵 / `check` 多层组合。
- [ ] `./gradlew check` 无警告

## 4. 端到端验证
- [ ] TUI 启动后构造 Checker（`CodeBotModel.java:429-433`），`agent.setChecker(permChecker)` 注入 Agent
- [ ] PLAN 模式：`/plan` → `permChecker.setMode(PermissionMode.PLAN)` + `PlanFile.getOrCreatePlanPath(workDir)`；下一轮工具调用调 `check`，`WriteFile` 写非 plan 文件被 Deny 除非命中 `.codebot/plans/`
- [ ] HITL：DEFAULT 模式下让模型写新文件，TUI 弹出三选项（YOLO / Manual / Feedback 见 `PlanApprovalDialog.java:47-51`，普通 HITL 见 `AskUserDialog`），三选项对应 `ALLOW / ALLOW_ALWAYS / DENY`
- [ ] 自学习：选 `ALLOW_ALWAYS` 时 `StreamingExecutor.java:114-118` 调用 `checker.addAllowAlwaysRule(toolName, content)`，同会话内同 `toolName:content` 直接放行
- [ ] 危险命令防御不可绕过：即使 `BYPASS`，`check` 仍按顺序经过 Layer 1/2（Bash 安全命令与危险命令分支不依赖 mode，见 `PermissionChecker.java:123-135`），让模型执行 `rm -rf /` 仍 Deny
- [ ] 留存证据: 验收阶段可在 `.codebot/permissions.local.yaml` 中观察 `appendLocalRule` 写入的 YAML 列表项

## 5. 文档
- [ ] spec.md / tasks.md / checklist.md 三件套齐全（`docs/java/ch06/`）
- [ ] commit 信息标注 `ch06` 与三件套关闭状态（待统一打包提交）

```



## ch07

```markdown
# 我的初步想法
- 实现一个客户端，按 JSON-RPC 2.0 的消息格式跟外部 server 通信
- 至少支持两种传输方式：本地子进程 stdio、远程 Streamable HTTP
- 一次会话分三个阶段：连接初始化握手 → 工具列表发现 → 工具调用
- 消息是双向的，需要处理请求-响应的异步匹配（每个请求带 id，回包按 id 关联）
- 写一个适配层把发现到的远端工具包装成 CodeBot 已有的 Tool 接口，注册进工具中心，Agent 调用时无感
- 多个 server 的连接做缓存或池化，避免每次工具调用都重连
- 配置在哪里声明 server 列表（命令、URL、env、超时）需要在 spec 阶段定下来
```

### Go

```markdown
# ch07: MCP Protocol Spec

## 1. 背景

外部能力（Context7、Atlassian、Slack 等）通过 Model Context Protocol（MCP）暴露给 Agent。如果没有 MCP 客户端实现，CodeBot 就只能依赖内置的六个工具，无法接入生态里已有的几百个 MCP server，等于砍掉一大块工具生态。MCP 规范定义了 JSON-RPC 2.0 之上的握手 → 工具发现 → 工具调用三阶段会话，需要本章把这三阶段、两种传输（stdio / Streamable HTTP，含兼容 SSE）以及到 `tools.Tool` 接口的适配器实现，并接到 TUI 的启动流程里。

## 2. 目标

交付一个能在 CodeBot 启动时按配置批量连接外部 MCP server、把每个 server 暴露的工具注册到全局 tool registry 的客户端。具体能力：单服务器 `Client` 封装（Connect / ListTools / CallTool / Close）；多服务器 `Manager` 封装（LoadConfigs / ConnectAll / RegisterAllTools / Shutdown）；`MCPToolWrapper` 把每个 MCP tool 适配到 CodeBot 的 `tools.Tool` 接口；工具名做命名空间消毒。最终效果是用户在 TUI 里看到 MCP server 的工具与内置工具并列，能直接被 LLM 调用。

## 3. 功能需求

- F1: 服务器配置同时支持 stdio（命令 + 参数 + 环境变量）和 HTTP（URL + 传输类型 + 头部）两种传输。
- F2: HTTP 传输按 transport 字段路由到 Streamable HTTP 或兼容 SSE。
- F3: stdio 子进程的 stderr 必须重定向丢弃，避免 OSC 颜色查询污染父 TTY 输入。
- F4: HTTP 请求头通过自定义 RoundTripper 注入，并对 header 值做环境变量展开，方便从 ENV 取 API key。
- F5: 单服务器客户端实现 Connect → ListTools → CallTool → Close 四阶段，所有调用复用同一个 SDK session。
- F6: 多服务器连接做批量并入，单个失败收集错误但不阻塞其他 server。
- F7: 工具名按 `mcp__<server>__<tool>` 命名，server 名和 tool 名都做命名空间消毒（非 `[A-Za-z0-9_]` 字符替换为下划线），保证 LLM API 的 tool name 合法。
- F8: 工具包装器 Execute 把 MCP 返回的文本内容列表拼成字符串，把 `IsError` 透传到 `tools.ToolResult.IsError`，无输出时回填占位文本。
- F9: 提供 RegisterAllTools 把所有 wrapper 注册到 `tools.Registry`，并返回连接错误清单供 TUI 显示。
- F10: TUI 启动时走异步连接，连接结果通过事件回到主线程注册到 registry。

## 4. 非功能需求

- N1: 连接是异步的（在 TUI 后台 goroutine 里执行），不阻塞 TUI 启动。
- N2: 单个 server 连接失败要打日志并写入错误清单，其他 server 继续连。
- N3: 工具名转换必须保证 LLM API 的 tool name 合法性（只允许字母数字和下划线）。
- N4: 复用官方 MCP Go SDK，不要手写 JSON-RPC 帧格式。
- N5: Shutdown 必须幂等，能在 TUI 退出时清理所有连接。

## 5. 设计概要

- 核心数据结构:
 - `ServerConfig`：YAML 反序列化结构体，承载 name / command / args / env / url / transport / headers。
 - `Client`：单 server 的会话句柄，持有配置 + SDK session + SDK client。
 - `Manager`：多 server 调度，持有配置集合与已连接客户端集合。
 - `ConnectResult`：`ConnectAll` 的返回类型，含 manager / 工具列表 / server 列表 / 错误清单。
 - `MCPToolWrapper`：把 MCP tool 适配到 CodeBot 的 `tools.Tool` 接口。
- 主流程（调用链）:
 - TUI 启动读 config 拿到 MCP server 列表 → 异步走 `Manager.LoadConfigs` + `ConnectAll`。
 - 对每个 server `NewClient(cfg).Connect`：按 stdio / Streamable HTTP / SSE 三种 transport 选择 SDK transport，发起握手，拿 session。
 - `ListTools` 拿工具列表，包成 `MCPToolWrapper`。
 - TUI 收到完成事件后把工具批量注册到 `tools.Registry`。
 - LLM 调用工具时按 `mcp__<server>__<tool>` 找到 wrapper，`Execute` 走 session 上的 `tools/call`。
- 与其他模块的交互:
 - 依赖 `internal/tools`（注册到全局 registry、实现 `Tool` 接口）。
 - 依赖官方 MCP Go SDK。
 - 被 `internal/tui` 在启动流程中调用。
 - 依赖 `internal/config` 提供反序列化目标，TUI 把 config 字段拷到 `ServerConfig`。

## 6. Out of Scope

- OAuth / 鉴权刷新：本仓库只做静态 header 注入，不实现 OAuth step-up 401 处理。
- 连接缓存：每次启动重新连接，不做跨进程缓存。
- IDE 集成（双向 SSE / WebSocket / 进程内 transport）。
- MCP resources / prompts / sampling 三种非 tool 能力：只暴露 `tools/list` + `tools/call`。
- 服务器健康检查与自动重连：断了由用户重启 CodeBot。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch07: MCP Protocol Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。本章已课程核对完成，所有 T 任务标记 [x]，每条任务记录实际落地的文件与行号。

## T1: 定义 `ServerConfig` 与传输选择
- 影响文件: `internal/mcp/mcp.go`（行 21~44）
- 依赖任务: 无
- 完成标准: `ServerConfig` 字段含 `Name / Command / Args / URL / Transport / Headers / Env`，`IsStdio` 与 `transportKind` 分流逻辑存在。

## T2: 实现 HTTP 头部注入
- 影响文件: `internal/mcp/mcp.go`（行 47~70）
- 依赖任务: T1
- 完成标准: `headerRoundTripper.RoundTrip` 对每个 header 值跑 `os.ExpandEnv`，`newHTTPClient` 在无 header 时返回 `http.DefaultClient`。

## T3: 实现单服务器 `Client`（Connect / ListTools / CallTool / Close）
- 影响文件: `internal/mcp/mcp.go`（行 72~151）
- 依赖任务: T1, T2
- 完成标准: `Client.Connect` 根据 `IsStdio` / `URL != ""` 分别选 `CommandTransport` / `StreamableClientTransport` / `SSEClientTransport`；stdio 把 `cmd.Stderr = io.Discard`；`CallTool` 把 `TextContent` 拼成字符串并透传 `IsError`。

## T4: 实现多服务器 `Manager`
- 影响文件: `internal/mcp/mcp.go`（行 155~237）
- 依赖任务: T3
- 完成标准: `Manager.LoadConfigs` 接受 `[]ServerConfig`；`Manager.ConnectAll` 收集 `Tools / Servers / Errors`；`Manager.Shutdown` 关闭所有 `Client.session`。

## T5: 实现 `MCPToolWrapper` 适配器
- 影响文件: `internal/mcp/mcp.go`（行 241~275）
- 依赖任务: T4
- 完成标准: `Name` 输出 `mcp__<sanitized-server>__<sanitized-tool>`；`SanitizeName` 把非 `[A-Za-z0-9_]` 全部替换为 `_`；`Execute` 失败时返回 `ToolResult{Output: "...", IsError: true}`。

## T6: 实现 `Manager.RegisterAllTools`
- 影响文件: `internal/mcp/mcp.go`（行 224~230）
- 依赖任务: T5
- 完成标准: 把 `ConnectResult.Tools` 全部 `registry.Register(...)`，返回 `Errors`。

## T7: 接入 TUI 启动流程
- 影响文件: `internal/tui/tui.go`（行 558~583 `initMCPServersCmd`、行 86 `mcpReadyMsg`、行 148 `mcpMgr` 字段、行 423~430 工具集查找）
- 依赖任务: T6
- 完成标准: TUI 启动时把 `config.yaml` 里的 `mcp_servers` 拷成 `[]mcp.ServerConfig` 调 `ConnectAll`，把 `result.Tools` 注册到 `m.registry`；MCP 工具能与内置工具并列被 LLM 调用；`grep -r "mcp\." internal/tui --include="*.go"` 至少 5 处非测试调用方。

## T8: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T7
- 完成标准: 在 `config.yaml` 加入 context7 server（`command: npx, args: [-y, @upstash/context7-mcp]`），启动 TUI，提示 LLM 调 `mcp__context7__resolve_library_id`，能看到工具命中并返回结果；`go test ./internal/mcp/ -run TestContext7MCP -v` 通过（需要 npx 可用）。

## 进度
- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8（受外部 `npx` 依赖，开发者本机已验证；CI 默认跳过）

```

```markdown
# ch07: MCP Protocol Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性
- [ ] 数据结构 `ServerConfig` 在 `/Users/codemelo/codebot/internal/mcp/mcp.go:21-29` 实现，字段含 `Name / Command / Args / URL / Transport / Headers / Env`（grep `type ServerConfig struct` 命中）。
- [ ] 数据结构 `Client` 在 `/Users/codemelo/codebot/internal/mcp/mcp.go:72-76` 实现，含 `config / session / sdkClient` 三个字段。
- [ ] 数据结构 `Manager` 在 `/Users/codemelo/codebot/internal/mcp/mcp.go:155-158` 实现，含 `configs / clients` 两张 map。
- [ ] 数据结构 `MCPToolWrapper` 在 `/Users/codemelo/codebot/internal/mcp/mcp.go:241-245` 实现，把 MCP tool 包装成 `tools.Tool`。
- [ ] 函数 `(*Client).Connect` 在 `/Users/codemelo/codebot/internal/mcp/mcp.go:82-116` 实现，支持 stdio / Streamable HTTP / SSE 三条分支。
- [ ] 函数 `(*Client).ListTools` 在 `/Users/codemelo/codebot/internal/mcp/mcp.go:118-124` 实现，调 `session.ListTools(ctx, nil)`。
- [ ] 函数 `(*Client).CallTool` 在 `/Users/codemelo/codebot/internal/mcp/mcp.go:126-145` 实现，把 `TextContent` 拼成字符串、透传 `IsError`、无输出回填 `(no output)`。
- [ ] 函数 `(*Manager).ConnectAll` 在 `/Users/codemelo/codebot/internal/mcp/mcp.go:185-222` 实现，按 server 维度收集 `Tools / Servers / Errors`。
- [ ] 函数 `(*Manager).RegisterAllTools` 在 `/Users/codemelo/codebot/internal/mcp/mcp.go:224-230` 实现，把 wrapper 注册到 `tools.Registry`。
- [ ] 函数 `SanitizeName` 在 `/Users/codemelo/codebot/internal/mcp/mcp.go:251-253` 实现，把非 `[A-Za-z0-9_]` 替换为 `_`（`nonAlphanumeric` 正则在第 19 行）。
- [ ] 边界处理 `IsStdio() == false && URL == ""` 已覆盖（`mcp.go:106-108` 返回明确错误 `neither command nor url configured`）。
- [ ] 边界处理 stdio stderr 重定向到 `io.Discard`（`mcp.go:97`，注释解释 OSC 颜色查询污染 TTY 的原因）。
- [ ] 边界处理 HTTP header 值的 `os.ExpandEnv` 展开（`mcp.go:55`）。

## 2. 接入完整性（必查，杜绝死代码）
- [ ] `grep -rn "mcp\." /Users/codemelo/codebot --include="*.go" | grep -v "_test.go" | grep -v "internal/mcp/"` 至少 5 处非测试调用方（实测命中 6 处，均位于 `internal/tui/tui.go`）。
- [ ] 调用入口位于 TUI 模块的 `/Users/codemelo/codebot/internal/tui/tui.go:558-583`（`initMCPServersCmd`）。
- [ ] 工具注册中心已更新: `result.Tools` 经 `m.registry.Register(...)` 注入 `tools.Registry`（参见 TUI 收到 `mcpReadyMsg` 后的处理路径）。
- [ ] 配置项 `mcp_servers` 已暴露到 `config.yaml`：`internal/config` 反序列化为 `[]MCPConfig`，TUI 在 `initMCPServersCmd` 内转成 `[]mcp.ServerConfig`（`tui.go:568-579`）。
- [ ] 用户输入到本模块的路径可一句话描述: TUI 启动 → 读 `config.yaml.mcp_servers` → `mcp.NewManager().LoadConfigs(...) → ConnectAll(ctx) → result.Tools → registry.Register → LLM 把 mcp__xxx 当成普通工具调用`。

## 3. 编译与测试
- [ ] `go build ./...` 通过（章节交付前已执行）。
- [ ] `go vet ./internal/mcp/` 无警告。
- [ ] `go test ./internal/mcp/ -run TestContext7MCP -v` 通过（需要 `npx` 可用，活跃集成测试）。

## 4. 端到端验证
- [ ] 在 `config.yaml` 添加 context7 server，启动 TUI 后看到日志 `Connected successfully` 与工具列表中出现 `mcp__context7__resolve-library-id` 类工具（验证方式：手工启动 TUI 观察）。
- [ ] 在 TUI 中提示 LLM 调 context7 工具，模型返回结果而非 `Tool not found`。
- [ ] 留存证据: `/Users/codemelo/codebot/internal/mcp/mcp_test.go` 包含活跃集成测试，可重复运行。

## 5. 文档
- [ ] spec.md / tasks.md / checklist.md 三件套齐全且最新（位于 `/Users/codemelo/codebot/specs/go/ch07/`）。
- [ ] commit 信息标注 `ch07` 与三件套关闭状态（验收阶段产物，待用户审阅后随后续 commit 一并打标）。

```

### Python

```markdown
# ch07: MCP Protocol Spec

## 1. 背景

外部能力（Context7、GitHub、Slack、数据库等）通过 Model Context Protocol（MCP）暴露给 Agent。如果没有 MCP 客户端实现，CodeBot 就只能依赖内置工具，无法接入生态里已有的几百个 MCP server，等于砍掉一大块工具生态。MCP 规范定义了 JSON-RPC 2.0 之上的握手 → 工具发现 → 工具调用三阶段会话，需要本章把这三阶段、两种传输（stdio / Streamable HTTP）以及到 `Tool` 抽象基类的适配器实现，并接到 Textual TUI 的启动流程里。Python 版基于官方 `mcp` SDK（`ClientSession`、`stdio_client`、`streamable_http_client`），传输生命周期用 `AsyncExitStack` 统一收尾。

## 2. 目标

交付一个能在 CodeBot 启动时按配置批量连接外部 MCP server、把每个 server 暴露的工具注册到全局 `ToolRegistry` 的异步客户端。具体能力：单服务器 `MCPClient` 封装（`connect` / `list_tools` / `call_tool` / `close`）；多服务器 `MCPManager` 封装（`load_configs` / `register_all_tools` / `get_client` / `shutdown`）；`MCPToolWrapper` 把每个 MCP tool 适配到 CodeBot 的 `Tool` 抽象基类，并用 `pydantic.create_model` 动态生成参数模型；工具名按 `mcp_<server>_<tool>` 命名。最终效果是用户在 Textual TUI 里看到 MCP server 的工具与内置工具并列，能直接被 LLM 调用。

## 3. 功能需求

- F1: `MCPServerConfig`（`codebot/config.py:67-78`）同时支持 stdio（`command + args + env`）和 HTTP（`url + headers`）两种传输，`is_stdio` 属性通过 `command is not None` 区分。
- F2: HTTP 传输用 `mcp.client.streamable_http.streamable_http_client` 建立 Streamable HTTP 会话，外部 `httpx.AsyncClient` 注入 header。
- F3: stdio 子进程通过 `StdioServerParameters` 启动，环境用 `build_child_env` 白名单，避免泄露宿主机 API key。
- F4: HTTP 请求头通过 `resolve_env_vars` 在客户端层做 `${VAR}` 展开，方便从 ENV 取 API key。
- F5: 单服务器客户端 `MCPClient.connect` → `list_tools` → `call_tool` → `close` 四阶段，所有调用复用同一个 `ClientSession`，整套生命周期挂在 `AsyncExitStack` 上。
- F6: 多服务器连接 `MCPManager.register_all_tools` 顺序遍历配置，单个失败只 append 到 `errors` 列表，不阻塞其他 server。
- F7: 工具名按 `mcp_<server>_<tool>` 命名（`tool_wrapper.py:67`），简单字符串拼接，避免与内置工具冲突。
- F8: `MCPToolWrapper.execute` 把 MCP 返回的 `TextContent / ImageContent / EmbeddedResource` 块按规则拼成字符串，把 `isError` 透传到 `ToolResult.is_error`，无输出时回填 `(no output)`。
- F9: `MCPToolWrapper` 用 `pydantic.create_model` 把 MCP 的 `inputSchema` 动态翻译成 `BaseModel`，作为 `params_model` 供工具调度层使用；`get_schema` 仍直接返回原始 `inputSchema`，避免 pydantic 转换破坏 schema 语义。
- F10: Textual TUI 启动时走 `asyncio.create_task(self._init_mcp())` 异步连接，连接结果回到主线程注册到 registry；用户按 enter 发消息前若 task 未完成，则等待 task 完成再发送。

## 4. 非功能需求

- N1: 连接是异步的（`asyncio.create_task` 派生），不阻塞 TUI 启动；连接中显示 "Waiting for MCP servers to connect..." 占位。
- N2: 单个 server 连接失败要打 `logger.warning` 并追加到 `errors` 列表，其他 server 继续连。
- N3: 工具名只允许 ASCII 字母数字下划线；server 名与 tool 名按 `mcp_<server>_<tool>` 直拼，依赖配置层校验合法性。
- N4: 复用官方 `mcp` Python SDK，不要手写 JSON-RPC 帧格式或 stdio 流解码。
- N5: `MCPManager.shutdown` 必须幂等，遍历 `self._clients` 调每个 `client.close()`，异常仅记录日志；`_cleanup_stack` 对 anyio 的 "cancel scope" RuntimeError 做静默吞没（这是已知的 SDK shutdown race）。
- N6: 进程退出时 Textual 的 `_shutdown_mcp` 先取消 `_mcp_init_task` 再调 `manager.shutdown`，保证未完成的连接任务被回收。

## 5. 设计概要

- 核心数据结构（Python 类型）:
  - `MCPServerConfig`（`codebot/config.py:67`，dataclass）：承载 `name / command / args / url / headers / env`，`is_stdio` property。
  - `MCPClient`（`codebot/mcp/client.py:17`）：单 server 的会话句柄，持有 `config / _session / _stack / _alive`。
  - `MCPManager`（`codebot/mcp/manager.py:13`）：多 server 调度，持有 `_configs / _clients` 两张 dict。
  - `MCPToolWrapper`（`codebot/mcp/tool_wrapper.py:57`）：把 MCP tool 适配到 `Tool` 抽象基类，动态生成 `params_model`。
- 主流程（调用链）:
  - `codebot/__main__.py:49` 启动 `CodeBotApp` 时把 `config.mcp_servers` 传进去。
  - `codebot/app.py:810-811` `on_mount` 在 `self._mcp_server_configs` 非空时 `asyncio.create_task(self._init_mcp())`。
  - `_init_mcp`（`app.py:1496-1532`）实例化 `MCPManager`，`load_configs` + `register_all_tools(self.registry)`，把每个 server 的 tool 包成 `MCPToolWrapper` 注册。
  - 对每个 server `MCPClient(config).connect()`：按 `is_stdio` 分流到 `_connect_stdio` 或 `_connect_http`，握手得到 `ClientSession`，把 transport 和 session 都丢进 `AsyncExitStack`。
  - LLM 调用工具时按 `mcp_<server>_<tool>` 找到 wrapper，`execute` 走 session 上的 `call_tool`，把 `inputSchema` 校验后的 `BaseModel.model_dump(exclude_none=True)` 作为参数。
- 与其他模块的交互:
  - 依赖 `codebot/tools`（注册到 `ToolRegistry`、继承 `Tool` 基类）。
  - 依赖官方 `mcp` Python SDK（`ClientSession` / `stdio_client` / `streamable_http_client` / `types`）。
  - 依赖 `httpx.AsyncClient` 作为 HTTP transport 的底层连接池。
  - 被 `codebot/app.py`（Textual TUI 主类）在启动流程中调用。
  - 依赖 `codebot/config.py` 提供 `MCPServerConfig` 反序列化目标及 `resolve_env_vars / build_child_env` 工具。

## 6. Out of Scope

- OAuth / 鉴权刷新：只做静态 header `${VAR}` 注入，不实现 OAuth step-up 401 处理。
- 连接缓存：每次启动重新连接，不做跨进程缓存或持久化 session。
- IDE 集成（双向 SSE / WebSocket / 进程内 transport）。
- MCP `resources / prompts / sampling` 三种非 tool 能力：只暴露 `tools/list` + `tools/call`；`EmbeddedResource` 在 wrapper 里仅做文本透传。
- 服务器健康检查与自动重连：当前实现仅在工具调用时 lazy 重连（`tool_wrapper.py:88-95`），不做后台 ping/heartbeat。
- 工具名 sanitization 正则：Python 版不像 Go 版做 `[A-Za-z0-9_]` 正则替换，直接信任 server / tool 命名。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch07: MCP Protocol Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。本章基于 `origin/python` 分支已落地的实现产出，每条任务记录实际文件与行号。

## T1: 定义 `MCPServerConfig` 与 ENV 工具
- 影响文件: `codebot/config.py:67-78`（dataclass 与 `is_stdio`），`codebot/config.py:50-64`（`resolve_env_vars` / `build_child_env`）
- 依赖任务: 无
- 完成标准: `MCPServerConfig` 字段含 `name / command / args / url / headers / env`，`is_stdio` 用 `command is not None` 判定；`resolve_env_vars` 把 `${VAR}` 展开成 env value，缺失变量保留占位符；`build_child_env` 仅注入 `PATH` 加白名单 env，不携带宿主机敏感变量。

## T2: 在 `load_config` 中反序列化 `mcp_servers`
- 影响文件: `codebot/config.py:129-139`（构造 list），`codebot/validator.py`（校验同时给 command 和 url、两者都缺时抛 `ConfigError`）
- 依赖任务: T1
- 完成标准: YAML 中 `mcp_servers` map（key 为 server name）能正确解析成 `list[MCPServerConfig]`；测试 `tests/test_mcp.py::TestLoadConfigMCP` 全绿，其中包含 stdio、HTTP、both/neither 错误三类。

## T3: 实现单服务器 `MCPClient.connect` 分流
- 影响文件: `codebot/mcp/client.py:17-65`
- 依赖任务: T1
- 完成标准: `MCPClient.connect`（client.py:29-51）根据 `config.is_stdio` 分别走 `_connect_stdio`（53-65，用 `StdioServerParameters` + `stdio_client`）或 `_connect_http`（67-84，用 `httpx.AsyncClient` + `streamable_http_client`）；连接全部通过 `AsyncExitStack` 管理；连接失败时 `_cleanup_stack` 兜底回滚。

## T4: 实现 `list_tools` / `call_tool` / `close` / `_cleanup_stack`
- 影响文件: `codebot/mcp/client.py:86-113`
- 依赖任务: T3
- 完成标准: `list_tools`（86-89）调 `self._session.list_tools()` 返回 `list[types.Tool]`；`call_tool`（91-95）透传 `CallToolResult`；`close`（97-100）置 `_alive = False` 并交还 stack；`_cleanup_stack`（102-113）静默吞掉 anyio 的 "cancel scope" `RuntimeError`，其他异常仅打 debug 日志。

## T5: 实现 `MCPToolWrapper` 适配器
- 影响文件: `codebot/mcp/tool_wrapper.py:57-109`
- 依赖任务: T4
- 完成标准: `MCPToolWrapper.__init__`（58-74）赋值 `self.name = f"mcp_{server_name}_{tool_def.name}"`，`category = "command"`，`should_defer = True`，调 `_build_params_model` 生成 pydantic `BaseModel`；`get_schema`（80-85）直接返回原始 `inputSchema`，不走 pydantic 转换；`execute`（87-109）失败时返回 `ToolResult(output="...", is_error=True)`，并把 `result.isError` 透传。

## T6: 实现 `_build_params_model` 与 `_extract_text`
- 影响文件: `codebot/mcp/tool_wrapper.py:12-54`
- 依赖任务: T5
- 完成标准: `_build_params_model`（12-26）用 `pydantic.create_model` 动态生成 `<tool_name>Params` 模型，required 字段标 `...`、optional 字段标 `None`；`_json_type_to_python`（29-38）覆盖 string/integer/number/boolean/object/array 六类；`_extract_text`（41-54）把 `TextContent` / `ImageContent` / `EmbeddedResource` 三种 block 类型按规则拼接，无 block 时回填 `(no output)`。

## T7: 实现 `MCPManager` 调度与重连
- 影响文件: `codebot/mcp/manager.py:13-70`
- 依赖任务: T5, T6
- 完成标准: `load_configs`（18-20）把 `list[MCPServerConfig]` 按 name 灌进 `_configs` dict；`register_all_tools`（22-41）遍历 connect + list_tools + register，单个失败 append 到 `errors` 列表不阻塞；`get_client`（43-61）支持 lazy connect 与 `is_alive=False` 时的重连；`shutdown`（63-70）遍历 `_clients` 调 `close()`，异常仅 debug 记录。

## T8: 暴露 `MCPManager` 出包
- 影响文件: `codebot/mcp/__init__.py:1-5`
- 依赖任务: T7
- 完成标准: `__init__.py` 通过 `__all__ = ["MCPManager"]` 暴露，调用方写 `from codebot.mcp import MCPManager` 即可。

## T9: 接入 Textual TUI 启动流程
- 影响文件: `codebot/app.py:50`（import），`codebot/app.py:514-525`（构造参数），`codebot/app.py:537-538`（实例字段），`codebot/app.py:810-811`（`on_mount` 派任务），`codebot/app.py:1042-1044`（发消息前 await），`codebot/app.py:1068-1070`（追加 system reminder），`codebot/app.py:1496-1532`（`_init_mcp`），`codebot/app.py:1534-1544`（`_shutdown_mcp`）
- 依赖任务: T8
- 完成标准: TUI 启动时把 `config.mcp_servers` 拷给 `CodeBotApp`，`on_mount` 派 `asyncio.create_task(self._init_mcp())`；`_init_mcp` 实例化 `MCPManager` + `load_configs` + `register_all_tools(self.registry)`，把 server 名与可用工具列表拼成 `_mcp_instructions` 用 `add_system_reminder` 注入；用户发消息时若 task 未完成则 `await self._mcp_init_task`；退出时 `_shutdown_mcp` 取消 task 并调 `manager.shutdown`。

## T10: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T9
- 完成标准: `pytest tests/test_mcp.py -v` 全绿；在 `config.yaml` 加入 context7 server（`command: npx, args: [-y, "@upstash/context7-mcp"]`），启动 TUI，提示 LLM 调 `mcp_context7_resolve_library_id`，能看到工具命中并返回结果；TUI 顶部状态条应出现 "Connected to N MCP server(s), M tools registered" 提示。

## 进度
- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8
- [ ] T9
- [ ] T10（受外部 `npx` / context7 依赖，开发者本机已验证；CI 默认跳过）

```

```markdown
# ch07: MCP Protocol Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。文件路径基于 `origin/python` 分支。

## 1. 实现完整性

- [ ] 数据结构 `MCPServerConfig` 在 `codebot/config.py:67-78` 实现，字段含 `name / command / args / url / headers / env`，`is_stdio` property 在第 76-78 行（`git show origin/python:codebot/config.py | grep -n "class MCPServerConfig"` 命中第 68 行）。
- [ ] 数据结构 `MCPClient` 在 `codebot/mcp/client.py:17-23` 实现，含 `config / name / _session / _stack / _alive` 五个属性（`git show origin/python:codebot/mcp/client.py | grep -n "class MCPClient"` 命中第 17 行）。
- [ ] 数据结构 `MCPManager` 在 `codebot/mcp/manager.py:13-16` 实现，含 `_configs / _clients` 两张 dict（`git show origin/python:codebot/mcp/manager.py | grep -n "class MCPManager"` 命中第 13 行）。
- [ ] 数据结构 `MCPToolWrapper` 在 `codebot/mcp/tool_wrapper.py:57-74` 实现，继承 `Tool` 基类，赋值 `name / description / category / should_defer / params_model`（`git show origin/python:codebot/mcp/tool_wrapper.py | grep -n "class MCPToolWrapper"` 命中第 57 行）。
- [ ] 函数 `MCPClient.connect` 在 `codebot/mcp/client.py:29-51` 实现，按 `config.is_stdio` 分流到 `_connect_stdio` / `_connect_http`，握手通过 `ClientSession.initialize()`，失败回滚 `AsyncExitStack`。
- [ ] 函数 `MCPClient._connect_stdio` 在 `client.py:53-65` 实现，用 `StdioServerParameters` + `mcp.client.stdio.stdio_client`，env 通过 `build_child_env` 白名单。
- [ ] 函数 `MCPClient._connect_http` 在 `client.py:67-84` 实现，用 `httpx.AsyncClient` + `mcp.client.streamable_http.streamable_http_client`，header 通过 `resolve_env_vars` 展开。
- [ ] 函数 `MCPClient.list_tools` 在 `client.py:86-89` 实现，调 `self._session.list_tools()` 返回 `list[types.Tool]`。
- [ ] 函数 `MCPClient.call_tool` 在 `client.py:91-95` 实现，透传 `CallToolResult`。
- [ ] 函数 `MCPClient._cleanup_stack` 在 `client.py:102-113` 实现，对 anyio `RuntimeError("cancel scope")` 静默吞没（这是 SDK shutdown race 的已知行为）。
- [ ] 函数 `MCPManager.load_configs` 在 `manager.py:18-20` 实现，按 `cfg.name` 灌进 `_configs` dict。
- [ ] 函数 `MCPManager.register_all_tools` 在 `manager.py:22-41` 实现，按 server 维度收集 `errors`，单个失败 `logger.warning` 后 append 不阻塞其他 server；返回 `list[str]`。
- [ ] 函数 `MCPManager.get_client` 在 `manager.py:43-61` 实现，支持 lazy connect 与 `is_alive=False` 时重新实例化客户端。
- [ ] 函数 `MCPManager.shutdown` 在 `manager.py:63-70` 实现，遍历调 `client.close()`，异常仅 `logger.debug` 记录，清空 `_clients`。
- [ ] 函数 `_build_params_model` 在 `tool_wrapper.py:12-26` 实现，用 `pydantic.create_model` 动态生成 `<ToolName>Params`，required 标 `...`、optional 标 `None`。
- [ ] 函数 `_extract_text` 在 `tool_wrapper.py:41-54` 实现，处理 `TextContent / ImageContent / EmbeddedResource`，无 block 回填 `(no output)`。
- [ ] 函数 `MCPToolWrapper.execute` 在 `tool_wrapper.py:87-109` 实现，`is_alive=False` 时 lazy reconnect；失败返回 `ToolResult(output="...", is_error=True)`；透传 `result.isError`。
- [ ] 工具名格式为 `mcp_<server>_<tool>`（`tool_wrapper.py:67` `f"mcp_{server_name}_{tool_def.name}"`）。
- [ ] 边界 `MCPServerConfig` 同时给 `command` 和 `url` 时 `load_config` 抛 `ConfigError`，错误信息包含 `cannot have both`（`pytest tests/test_mcp.py::TestLoadConfigMCP::test_both_command_and_url_errors -v`）。
- [ ] 边界 `MCPServerConfig` 两者都不给时抛 `ConfigError`，包含 `must have either`（`pytest tests/test_mcp.py::TestLoadConfigMCP::test_neither_command_nor_url_errors -v`）。
- [ ] 边界 stdio 子进程 env 通过 `build_child_env` 白名单（`tests/test_mcp.py::TestBuildChildEnv::test_excludes_host_vars` 通过，确认宿主机 `ANTHROPIC_API_KEY` 不被泄漏）。
- [ ] 边界 HTTP header 值的 `${VAR}` 展开走 `resolve_env_vars`（`client.py:71-72` 字典推导式）。

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `git show origin/python:codebot/app.py | grep -nE "mcp_|MCPManager|_init_mcp"` 至少 12 处命中（实测含 import、字段、`on_mount` 派任务、`_init_mcp` / `_shutdown_mcp`、发消息 await、system reminder 注入）。
- [ ] 调用入口位于 Textual TUI 的 `codebot/app.py:810-811`：`if self._mcp_server_configs: self._mcp_init_task = asyncio.create_task(self._init_mcp())`。
- [ ] 工具注册中心已更新：`_init_mcp`（`app.py:1496-1532`）调 `await manager.register_all_tools(self.registry)`，把 wrapper 注入 `ToolRegistry`。
- [ ] System reminder 注入：连接成功后构造 `_mcp_instructions`（`app.py:1515-1532`，含 server 名与工具列表），发消息时若未注入则用 `conversation.add_system_reminder` 写入一次（`app.py:1068-1070`）。
- [ ] 配置项 `mcp_servers` 已从 YAML 反序列化到 `AppConfig.mcp_servers`（`codebot/config.py:129-139`），`__main__.py:52` 把 `config.mcp_servers` 传给 `CodeBotApp`。
- [ ] 用户输入到本模块的路径可一句话描述: Textual TUI 启动 → 读 `config.yaml.mcp_servers` → `MCPManager().load_configs(...) → register_all_tools(self.registry)` → 工具变成 `mcp_<server>_<tool>` → LLM 把它当普通工具调用 → `MCPToolWrapper.execute` 走 `MCPClient.call_tool` → MCP server 返回 `CallToolResult` → `_extract_text` 拼成字符串。
- [ ] 退出时 `_shutdown_mcp`（`app.py:1534-1544`）取消 `_mcp_init_task` 并 await，再调 `manager.shutdown()` 清理所有 client。

## 3. 编译与测试

- [ ] `ruff check codebot/mcp/` 无报错（章节交付前已执行）。
- [ ] `mypy codebot/mcp/` 类型检查通过（若项目启用 mypy）。
- [ ] `pytest tests/test_mcp.py -v` 全绿，至少 14 个测试（`TestResolveEnvVars`、`TestBuildChildEnv`、`TestLoadConfigMCP`、`TestMCPToolWrapper`、`TestExtractText`、`TestMCPManagerPartialFailure` 六组）。
- [ ] `pytest tests/test_mcp.py::TestMCPManagerPartialFailure -v` 单跑通过，验证单 server 失败不阻塞其他 server。

## 4. 端到端验证

- [ ] 在 `config.yaml` 添加 context7 server（`command: npx, args: ["-y", "@upstash/context7-mcp"]`），启动 `python -m codebot`，观察日志出现 `MCP server 'context7' connected` 与 `Registered MCP tool: mcp_context7_resolve_library_id` 类条目。
- [ ] TUI 状态条 / 系统消息出现 `Connected to 1 MCP server(s), N tools registered`（`app.py:1512-1514`）。
- [ ] 在 TUI 中提示 LLM 调 context7 工具（例：`use mcp_context7_resolve_library_id for "next.js"`），模型返回结果而非 `Tool not found`。
- [ ] 留存证据: `tests/test_mcp.py` 包含 `TestMCPManagerPartialFailure::test_single_server_failure_does_not_block_others`，可重复运行。

## 5. 文档

- [ ] `docs/python/ch07/spec.md` / `tasks.md` / `checklist.md` 三件套齐全且最新。
- [ ] commit 信息标注 `ch07` 与三件套关闭状态（验收阶段产物，待用户审阅后随后续 commit 一并打标）。

```

### Java

```markdown
# ch07: MCP Protocol Spec

## 1. 背景

外部能力（Context7、Atlassian、Slack 等）通过 Model Context Protocol（MCP）暴露给 Agent。如果没有 MCP 客户端实现，CodeBot 就只能依赖内置的六个工具（ReadFile / WriteFile / EditFile / Bash / Glob / Grep），无法接入生态里已有的几百个 MCP server，等于砍掉一大块工具生态。MCP 规范定义了 JSON-RPC 2.0 之上的握手 → 工具发现 → 工具调用三阶段会话，需要本章在 Java 侧把这三阶段、两种传输（stdio 子进程 / Streamable HTTP，含兼容 SSE 解析）以及到 `com.codebot.tool.Tool` 接口的适配器实现，并接到 TUI 的启动流程里。

## 2. 目标

交付一个能在 CodeBot 启动时按配置批量连接外部 MCP server、把每个 server 暴露的工具注册到全局 `ToolRegistry` 的客户端。具体能力：单服务器 `McpTransport` 抽象（connect / getInstructions / listTools / callTool / close）；多服务器调度类 `McpManager`（构造、`connectAll`、`registerAllTools`、`shutdown`）；`McpToolWrapper` 把每个 MCP tool 适配到 `com.codebot.tool.Tool` 接口；工具名做命名空间消毒。最终效果是用户在 TUI 里看到 MCP server 的工具与内置工具并列，能被 LLM 调用，且默认走 deferred 通道按需披露。

## 3. 功能需求

- F1: 服务器配置 `McpServerConfig` 同时承载 stdio（`command + args + env`）和 HTTP（`url + headers`）两种传输，POJO + getter/setter，YAML 反序列化兼容。
- F2: `McpManager.connectAll` 在 `command` 非空时构造 `McpStdioClient`，否则在 `url` 非空时构造 `McpHttpClient`，两者皆空时把错误写入 `errors` 列表并跳过该 server。
- F3: stdio 子进程的 stderr 用一个 virtual thread 持续 drain 丢弃，避免 OSC 颜色查询污染父 TTY 输入。
- F4: HTTP 请求头通过 `HttpRequest.Builder.header` 注入，并对 header 值做 `${VAR}` 占位符替换（`resolveEnvVars`），方便从环境变量取 API key；stdio 子进程的 `env` 同样做替换。
- F5: 单服务器实现 `connect` → `listTools` → `callTool` → `close` 四阶段：`connect` 发 `initialize` 请求并紧跟一条 `notifications/initialized`；`listTools` 调 `tools/list`；`callTool` 调 `tools/call`；HTTP 复用同一个 `HttpClient` 实例，stdio 复用同一对 `BufferedReader / BufferedWriter`。
- F6: `McpManager.connectAll` 把多 server 批量并入，单个 server 抛异常时把 `errors.add("MCP server '<name>': <message>")` 收集但不阻塞其他 server；返回 `ConnectResult(tools, servers, errors)` 三元组。
- F7: 工具名按 `mcp__<server>__<tool>` 命名，server 名和 tool 名都过 `sanitizeName`（非 `[A-Za-z0-9_]` 字符替换为下划线），保证 LLM API 的 tool name 合法。
- F8: `McpToolWrapper.execute` 透过 transport 调真实工具，把 MCP 响应里的 `result.content` 列表中所有 `type == "text"` 块拼成字符串；JSON-RPC 错误（`response.error` 非空）返回 `"MCP error: <message>"`；无输出回填 `(no output)`；任何异常包成 `ToolResult.error(...)`。
- F9: `McpManager.registerAllTools(ToolRegistry registry)` 把所有 wrapper 注册到 `ToolRegistry`，返回 `errors` 列表供 TUI 显示。
- F10: 所有 wrapper 实现 `Tool.shouldDefer() == true`，类别 `ToolCategory.COMMAND`，让 TUI / Agent 把 MCP 工具放进 deferred 通道，靠 `ToolRegistry.getDeferredTools / searchDeferred / findDeferredByNames` 按需披露给 LLM。
- F11: HTTP transport 支持 `Mcp-Session-Id` 会话头：首次响应里若带回 `mcp-session-id` 则保存到客户端实例字段，后续每个请求自动带上。
- F12: HTTP transport 同时支持 `application/json` 与 `text/event-stream`：响应 `Content-Type` 含 `text/event-stream` 时走 `parseSseResponse`，从 `data:` 行里挑出匹配 `id` 的 JSON-RPC 帧；纯 JSON 走 `ObjectMapper.readValue`。
- F13: TUI 启动时（`CodeBotModel` 初始化阶段或专用 init 命令）读 `config.yaml` 的 `mcp_servers`，构造 `McpManager` 实例，异步调 `connectAll` / `registerAllTools`，把结果汇回主线程后注册到全局 registry。

## 4. 非功能需求

- N1: 连接是异步执行的（在 TUI 后台 virtual thread / executor 里执行），不阻塞 TUI 启动渲染。
- N2: 单个 server 连接失败要被收集到 `ConnectResult.errors`，其他 server 继续连。
- N3: 工具名转换必须保证 LLM API 的 tool name 合法性（只允许字母数字和下划线），由 `NON_ALNUM` 正则 + `sanitizeName` 保证。
- N4: 不要手写 JSON-RPC 帧格式以外的协议细节；JSON 编解码统一走单例 `ObjectMapper`。
- N5: `shutdown` 必须幂等：stdio 客户端调 `process.destroyForcibly`，HTTP 客户端无连接可关；多次调用不抛异常。
- N6: stdio 客户端的 `connect` 必须在 `initialize` 之后立刻发 `notifications/initialized`，否则有些 server 拒绝继续会话。

## 5. 设计概要

- 核心数据结构:
 - `com.codebot.config.McpServerConfig`：POJO，字段 `name / command / args / url / headers / env`，YAML 反序列化目标。
 - `McpManager`：多 server 调度，持有 `Map<String, McpServerConfig> configs` 与 `Map<String, McpTransport> clients` 两张 `LinkedHashMap`，对外暴露 `connectAll / registerAllTools / shutdown`。
 - `McpManager.McpTransport`：传输抽象接口（5 个方法），由 `McpStdioClient` / `McpHttpClient` 两个内部类实现。
 - `McpManager.ConnectResult`：record，含 `List<Tool> tools / List<ServerInfo> servers / List<String> errors`，作为 `connectAll` 的返回类型。
 - `McpManager.ServerInfo`：record，含 `name / instructions`，承载 `initialize` 响应里的 server `instructions` 文本。
 - `McpManager.McpToolDef`：record，含 `name / description / inputSchema`，承载 `tools/list` 单条结果。
 - `McpManager.McpToolWrapper`：把 `McpToolDef + 服务端 name + transport` 适配到 `com.codebot.tool.Tool`。
- 主流程（调用链）:
 - TUI 启动读 config 拿到 MCP server 列表 → 用 `new McpManager(configs)` 构造 → 异步走 `manager.connectAll()` / `manager.registerAllTools(registry)`。
 - 对每个 server 按 `command / url` 选择 `McpStdioClient` 或 `McpHttpClient`：构造 → `connect()` 发 `initialize` + `notifications/initialized` → `getInstructions()` 取握手返回的 server 指令 → `listTools()` 拿工具列表 → 包成 `McpToolWrapper`。
 - TUI 收到完成事件后把 `ConnectResult.tools` 批量 `registry.register(tool)`；`errors` 渲染到对话顶部告知用户。
 - LLM 调用工具时按 `mcp__<server>__<tool>` 在 registry 命中 wrapper，`execute(args)` 调 transport 的 `callTool` 走 session 上的 `tools/call`。
- 与其他模块的交互:
 - 依赖 `com.codebot.tool`：实现 `Tool` 接口、注册到 `ToolRegistry`、返回 `ToolResult`。
 - 依赖 `com.fasterxml.jackson.databind.ObjectMapper`：所有 JSON-RPC 帧编解码。
 - 依赖 JDK 自带的 `java.net.http.HttpClient`：HTTP 传输；stdio 走 `ProcessBuilder` + `BufferedReader / BufferedWriter`。
 - 被 `com.codebot.CodeBot` / `com.codebot.tui.CodeBotModel` 调用：在主启动入口把 `config.getMcpServers()` 传进 model；model 内构造 `McpManager` 并调 `registerAllTools`。

## 6. Out of Scope

- OAuth / 鉴权刷新：本仓库只做静态 header 注入与环境变量展开，不实现 OAuth step-up 401 处理。
- 连接缓存：每次启动重新连接，不做跨进程缓存。
- IDE 集成（双向 SSE / WebSocket / 进程内 transport）。
- MCP resources / prompts / sampling 三种非 tool 能力：只暴露 `tools/list` + `tools/call`。
- 服务器健康检查与自动重连：断了由用户重启 CodeBot。
- stdio 端的 stderr 内容回流：当前直接 drain 丢弃，不做日志聚合。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch07: MCP Protocol Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。每完成一个任务跑 `./gradlew build` 确保编译过；接入主流程的任务（T7、T8）做完后立刻补一次端到端验证再进下一项。

## T1: 定义 `McpServerConfig` 配置 POJO
- 影响文件: `src/main/java/com/codebot/config/McpServerConfig.java`（行 1~32）
- 依赖任务: 无
- 完成标准: 类含字段 `name / command / args / url / headers / env`，全部走 `private` + getter/setter，类型分别为 `String / String / List<String> / String / Map<String,String> / Map<String,String>`，可被 YAML 反序列化为 `mcp_servers` 列表项。

## T2: 抽出 `McpTransport` 接口与共享工具
- 影响文件: `src/main/java/com/codebot/mcp/McpManager.java`（行 19~30 类骨架与字段；行 86~96 `sanitizeName` / `resolveEnvVars`；行 100~106 `McpTransport` 接口；行 401~419 `extractTextContent`；行 421~423 `McpToolDef` record）
- 依赖任务: T1
- 完成标准: `McpManager` 持有 `ObjectMapper MAPPER`、`Pattern NON_ALNUM`、`Pattern ENV_VAR` 三个静态常量；定义内嵌 `interface McpTransport`，5 个方法 `connect / getInstructions / listTools / callTool / close`；静态助手 `sanitizeName` / `resolveEnvVars` / `extractTextContent` 实现；`McpToolDef(name, description, inputSchema)` record 定义齐全。

## T3: 实现 `McpStdioClient`（JSON-RPC over stdio）
- 影响文件: `src/main/java/com/codebot/mcp/McpManager.java`（行 108~239）
- 依赖任务: T2
- 完成标准:
 - `connect()` 用 `ProcessBuilder` 拉起子进程，把 `config.getEnv()` 跑 `resolveEnvVars` 后写入 `pb.environment()`；启动一个 virtual thread drain stderr（行 142~146）。
 - 发 `initialize` 请求（protocolVersion `2024-11-05`、clientInfo `{name: codebot, version: 0.1.0}`），从 `result.instructions` 取 server 指令。
 - 紧跟发 `notifications/initialized` 通知（行 159）。
 - `sendRequest` 用 `idCounter` 自增 + `MAPPER.writeValueAsString` 拼帧 + `writer.write + newLine + flush`；读响应循环 `readLine`，丢空行，遇到含 `id` 的帧返回。
 - `listTools` 把 `result.tools` 解析为 `List<McpToolDef>`。
 - `callTool(name, args)` 调 `tools/call`，错误透传 `MCP error: <message>`，否则调 `extractTextContent`。
 - `close()` 在 `process != null && process.isAlive()` 时 `destroyForcibly()`。

## T4: 实现 `McpHttpClient`（JSON-RPC over Streamable HTTP，兼容 SSE）
- 影响文件: `src/main/java/com/codebot/mcp/McpManager.java`（行 241~399）
- 依赖任务: T2
- 完成标准:
 - 类持有单例 `HttpClient.newHttpClient()` 与 `String sessionId` 字段。
 - `connect()` 发 `initialize`，从响应里读 `instructions`；紧跟发 `notifications/initialized`（行 268）。
 - `sendHttpRequest` 构建 `HttpRequest.newBuilder().uri(config.getUrl())`，必带 `Content-Type: application/json` 与 `Accept: application/json, text/event-stream`；`sessionId` 不空则带 `Mcp-Session-Id`；config 的 `headers` 走 `resolveEnvVars` 后逐条 `.header(key, value)`。
 - 响应 `mcp-session-id` 头若存在则赋值到 `sessionId`（行 337）。
 - `Content-Type` 含 `text/event-stream` 时走 `parseSseResponse`：按行解析 `data: ` 前缀，跳过空行与 `[DONE]`，匹配 `id` 后返回；否则 `MAPPER.readValue` 当成单个 JSON 帧。
 - `sendHttpNotification` 不带 `id` 字段，响应丢弃（`BodyHandlers.discarding()`）。
 - `listTools` / `callTool` 复用与 stdio 一致的语义。
 - `close()` 无连接需关，方法体为空注释。

## T5: 实现 `McpToolWrapper` 适配器
- 影响文件: `src/main/java/com/codebot/mcp/McpManager.java`（行 425~460）
- 依赖任务: T3, T4
- 完成标准:
 - 实现 `com.codebot.tool.Tool` 接口。
 - `name()` 返回 `"mcp__" + sanitizeName(serverName) + "__" + sanitizeName(toolDef.name())`（行 438~440）。
 - `description()` 直接透传 `toolDef.description()`。
 - `category()` 返回 `ToolCategory.COMMAND`；`shouldDefer()` 返回 `true`（让 deferred 通道接管）。
 - `schema()` 返回 `Map.of("name", name(), "description", description(), "input_schema", input)`，`input` 为 `toolDef.inputSchema()`，空则回退到 `{"type":"object","properties":{}}`。
 - `execute(args)` 调 `transport.callTool(toolDef.name(), args)`，捕获异常包成 `ToolResult.error("MCP tool call failed: " + e.getMessage())`，成功包 `ToolResult.success(output)`。

## T6: 实现 `McpManager.connectAll` / `registerAllTools` / `shutdown`
- 影响文件: `src/main/java/com/codebot/mcp/McpManager.java`（行 31~84）
- 依赖任务: T5
- 完成标准:
 - 构造函数接收 `List<McpServerConfig>`，按 name 装进 `configs` `LinkedHashMap`，null 安全。
 - `connectAll()` 遍历 `configs`：根据 `command` / `url` 选 `McpStdioClient` / `McpHttpClient`，两者皆空则错误清单加 `"MCP server '<name>': neither command nor url configured"` 并 continue。
 - 单个 server 走 `try { connect; listTools; tools.add(new McpToolWrapper(...)) } catch (Exception e) { errors.add(...) }`，不阻塞其他 server。
 - 返回 `new ConnectResult(List.copyOf(tools), List.copyOf(servers), List.copyOf(errors))`。
 - `registerAllTools(ToolRegistry registry)` 调一次 `connectAll`，对 `result.tools()` 逐个 `registry.register(t)`，返回 `result.errors()`。
 - `shutdown()` 遍历 `clients.values()` 调 `client.close()`，最后 `clients.clear()`，幂等。

## T7: 接入 TUI 启动流程
- 影响文件: `src/main/java/com/codebot/CodeBot.java`（行 35~39 把 `config.getMcpServers()` 传进 model 构造）、`src/main/java/com/codebot/tui/CodeBotModel.java`（在初始化阶段构造 `McpManager` 并异步调 `connectAll` / `registerAllTools`，把结果汇回 update / Msg 通道）
- 依赖任务: T6
- 完成标准:
 - TUI 启动时把 `config.getMcpServers()` 拷成 `List<McpServerConfig>` 传给 model；model 内构造 `new McpManager(configs)` 与默认 `ToolRegistry.createDefault()` 并存。
 - 异步线程（`Thread.ofVirtual().start(...)` 或 executor）执行 `registerAllTools`，错误列表通过自定义 `McpReadyMsg` 回主线程渲染。
 - MCP 工具能与内置 6 个工具并列被 LLM 调用（通过 `getDeferredTools` / `searchDeferred` / `findDeferredByNames` 披露）。
 - 退出钩子（如 `program.run()` 的 `finally`）调 `manager.shutdown()`。

## T8: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T7
- 完成标准:
 - `./gradlew build` 通过。
 - `./gradlew test` 全过（含 `McpManagerTest` 之类单测）。
 - 在 `config.yaml` 添加 context7 server（`command: npx, args: [-y, @upstash/context7-mcp]`），启动 TUI 后看到 MCP 工具列表（含 `mcp__context7__resolve_library_id` 等）能被 LLM 调用并返回结果；启动日志或错误面板看到 `Connected successfully`/无错误。
 - HTTP 路径用一台公开 MCP server（或自起 `mcp-server-stdio` 套 HTTP wrapper）验证 SSE 与 Mcp-Session-Id 头能跑通。
 - 截图或日志留证。

## 进度
- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8（受外部 `npx` / 公开 MCP server 依赖，本机已验证；CI 默认跳过）

```

```markdown
# ch07: MCP Protocol Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性

### 1.1 配置 POJO
- [ ] `McpServerConfig` 在 `src/main/java/com/codebot/config/McpServerConfig.java:6-32` 实现，字段 `name / command / args / url / headers / env` 齐全（grep `class McpServerConfig` 命中）。
- [ ] 全部字段走 `private` + 公开 `getXxx / setXxx`，类型分别为 `String / String / List<String> / String / Map<String,String> / Map<String,String>`（验证：肉眼检查 `McpServerConfig.java:15-31`）。

### 1.2 McpManager 骨架与共享工具
- [ ] 类 `McpManager` 位于 `src/main/java/com/codebot/mcp/McpManager.java:19`，含静态常量 `MAPPER`（行 21）、`NON_ALNUM`（行 22）、`ENV_VAR`（行 23）。
- [ ] record `ServerInfo(String name, String instructions)` 在 `McpManager.java:25` 定义。
- [ ] record `ConnectResult(List<Tool> tools, List<ServerInfo> servers, List<String> errors)` 在 `McpManager.java:26` 定义。
- [ ] record `McpToolDef(String name, String description, Map<String, Object> inputSchema)` 在 `McpManager.java:423` 定义。
- [ ] 接口 `McpTransport` 在 `McpManager.java:100-106` 定义，5 个方法 `connect / getInstructions / listTools / callTool / close` 齐全。
- [ ] 静态助手 `sanitizeName` 在 `McpManager.java:86-88` 实现，正则替换非 `[A-Za-z0-9_]` 为 `_`。
- [ ] 静态助手 `resolveEnvVars` 在 `McpManager.java:90-96` 实现，对 `${VAR}` 占位符做 `System.getenv` 替换，匹配不到时保留原样。
- [ ] 静态助手 `extractTextContent` 在 `McpManager.java:403-419` 实现，把 `result.content` 中 `type == "text"` 的块拼成字符串，空时返回 `(no output)`。

### 1.3 stdio 传输
- [ ] `McpStdioClient` 在 `McpManager.java:110-239` 实现，含 `process / writer / reader / idCounter / instructions` 五个字段。
- [ ] `connect()` 在 `McpManager.java:124-160` 实现，用 `ProcessBuilder` 启动子进程并 `redirectErrorStream(false)`。
- [ ] stderr drain 在 `McpManager.java:142-146`，用 `Thread.startVirtualThread` 持续 `readLine`（避免 OSC 颜色查询污染 TTY）。
- [ ] `initialize` 请求体 `protocolVersion=2024-11-05`、`clientInfo={name:codebot,version:0.1.0}` 在 `McpManager.java:148-152`。
- [ ] `notifications/initialized` 在 `McpManager.java:159` 发出。
- [ ] `sendRequest` 在 `McpManager.java:201-221` 实现：`idCounter.incrementAndGet`、`writer.write + newLine + flush`、读循环跳过空行、遇到含 `id` 的 JSON 帧返回。
- [ ] `listTools` 在 `McpManager.java:167-184` 解析 `result.tools` 为 `List<McpToolDef>`。
- [ ] `callTool` 在 `McpManager.java:188-198`，JSON-RPC `error` 非空时返回 `MCP error: <message>`；否则调 `extractTextContent`。
- [ ] `close` 在 `McpManager.java:234-238`，`process.isAlive()` 时 `destroyForcibly()`，幂等。
- [ ] env 变量替换：`config.getEnv()` 的值在 `McpManager.java:131-136` 走 `resolveEnvVars` 后写入 `pb.environment()`。

### 1.4 HTTP 传输
- [ ] `McpHttpClient` 在 `McpManager.java:243-399` 实现，含 `config / httpClient / idCounter / instructions / sessionId` 五个字段。
- [ ] `connect()` 在 `McpManager.java:256-269` 发 `initialize` 与 `notifications/initialized`。
- [ ] `sendHttpRequest` 在 `McpManager.java:310-347`：必带 `Content-Type: application/json` 与 `Accept: application/json, text/event-stream`；`sessionId` 不空时带 `Mcp-Session-Id` 头；config `headers` 走 `resolveEnvVars` 注入。
- [ ] 响应头 `mcp-session-id` 自动赋值到 `sessionId` 字段（`McpManager.java:337`）。
- [ ] SSE 解析在 `McpManager.java:350-368`：按行扫 `data: ` 前缀，跳过空行与 `[DONE]`，匹配 `id` 后返回对应 JSON-RPC 帧；找不到则抛 `IOException("No JSON-RPC response found in SSE stream")`。
- [ ] `sendHttpNotification` 在 `McpManager.java:370-393` 不带 `id` 字段，响应走 `BodyHandlers.discarding()`。
- [ ] `close()` 在 `McpManager.java:395-398` 是空实现 + 注释 `// HTTP is stateless; nothing to close`。

### 1.5 Tool Wrapper
- [ ] `McpToolWrapper` 在 `McpManager.java:427-460` 实现 `com.codebot.tool.Tool`。
- [ ] `name()` 在 `McpManager.java:438-440` 输出 `mcp__<sanitized-server>__<sanitized-tool>`。
- [ ] `description()` 透传 `toolDef.description()`（行 442）。
- [ ] `category()` 返回 `ToolCategory.COMMAND`、`shouldDefer()` 返回 `true`（行 443~444）。
- [ ] `schema()` 在 `McpManager.java:446-450` 返回 `{name, description, input_schema}`；`inputSchema` 为 null 时回退 `{type: object, properties: {}}`。
- [ ] `execute(args)` 在 `McpManager.java:452-459`：成功 `ToolResult.success(output)`、异常 `ToolResult.error("MCP tool call failed: " + e.getMessage())`。

### 1.6 Manager 调度
- [ ] 构造函数 `McpManager(List<McpServerConfig>)` 在 `McpManager.java:31-35` 实现，null 安全按 `name` 装进 `LinkedHashMap`。
- [ ] `connectAll()` 在 `McpManager.java:37-73`，按 `command / url` 选传输；两者皆空时 `errors.add("MCP server '<name>': neither command nor url configured")` 并 `continue`。
- [ ] 单 server 失败收集到 `errors`，其他 server 继续连：见 `McpManager.java:67-69` 的 `try/catch`。
- [ ] `connectAll` 返回的 `ConnectResult` 三个列表均通过 `List.copyOf` 包裹，避免外部修改（行 72）。
- [ ] `registerAllTools(ToolRegistry registry)` 在 `McpManager.java:75-79`，遍历 `result.tools()` 调 `registry.register(t)`，返回 `result.errors()`。
- [ ] `shutdown()` 在 `McpManager.java:81-84`：遍历 `clients.values()` 调 `close()`，再 `clients.clear()`，幂等。

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `grep -rn "McpManager\|McpServerConfig" --include="*.java" src/main` 至少 5 处非测试调用方（实测应命中 `config/McpServerConfig.java` 定义 + `mcp/McpManager.java` 定义 + `CodeBot.java` 传参 + `tui/CodeBotModel.java` 构造与生命周期）。
- [ ] 启动入口 `src/main/java/com/codebot/CodeBot.java:35-39` 把 `config.getMcpServers()` 透传给 `CodeBotModel`。
- [ ] `CodeBotModel` 内构造 `new McpManager(...)` 并在异步线程（virtual thread / executor）调用 `registerAllTools(toolRegistry)`，错误清单通过 Msg 回主循环渲染。
- [ ] 退出路径调 `manager.shutdown()`（在 `program.run()` 的 `finally` 或 model 的清理钩子里）。
- [ ] 配置项 `mcp_servers` 已暴露到 `config.yaml`：`AppConfig.getMcpServers()` 返回 `List<McpServerConfig>`，YAML 反序列化能解析 `command / args / env / url / headers` 字段。
- [ ] 用户输入到本模块的路径可一句话描述: 启动 `CodeBot.main` → `ConfigLoader.load` → `config.getMcpServers()` → `new CodeBotModel(..., mcpServers, ...)` → 异步 `new McpManager(mcpServers).registerAllTools(toolRegistry)` → LLM 把 `mcp__xxx` 当 deferred 工具按需取出。

## 3. 编译与测试

- [ ] `./gradlew build` 通过。
- [ ] `./gradlew test` 全过。
- [ ] `./gradlew test --tests "com.codebot.mcp.*"` 全过（含 sanitizeName / resolveEnvVars / extractTextContent 单测）。

## 4. 端到端验证

- [ ] 在 `config.yaml` 添加 context7 server（`command: npx, args: [-y, @upstash/context7-mcp]`），启动 TUI 后能看到 `mcp__context7__resolve_library_id` 出现在 deferred 工具列表中。
- [ ] 在 TUI 中提示 LLM 调 context7 工具，模型返回结果而非 `Tool not found`。
- [ ] 配置一个故意写错的 server（command 与 url 都不填），启动后看到错误清单含 `MCP server '<name>': neither command nor url configured`，其他 server 仍正常连上。
- [ ] HTTP MCP server（支持 SSE 响应）能跑通：返回 `text/event-stream` 时 `parseSseResponse` 解析得到 JSON-RPC 响应，`Mcp-Session-Id` 在后续请求里自动带上。
- [ ] 退出 TUI 后无 stdio 子进程残留（`ps aux | grep mcp` 看不到僵尸进程）。

## 5. 文档

- [ ] `docs/java/ch07/spec.md` 与本 checklist / tasks 三件套齐全且最新。
- [ ] commit 信息标注 `ch07` 与三件套关闭状态（验收阶段产物，待用户审阅后随后续 commit 一并打标）。

```



## ch08

```markdown
# 我的初步想法
- Token 消耗的大头是工具结果，压缩从这里下手；用户的原始消息要尽量原文保留，不能被摘要改写
- 第一层做预防：单个工具结果超阈值时把完整内容写到磁盘，对话里只留预览和文件路径；同时控制单条消息内所有工具结果的合计大小，超了挑大的依次存盘
- 第二层做兜底：整体对话逼近窗口上限时，调 LLM 生成结构化摘要替换旧消息；摘要按多个固定部分组织（主要请求、关键概念、文件代码、错误修复、解决过程、用户原话、待办、当前工作、下一步）
- 摘要 Prompt 必须明确禁止 LLM 调用任何工具（首尾各强调一次），并要求先输出分析草稿再写正式摘要，草稿用完即弃
- 压缩后要附加一条边界消息，提示模型如需文件细节请重新读取，避免根据摘要脑补出不存在的代码
- 用户可以通过命令手动触发压缩；摘要连续失败要熔断，停止自动触发避免死循环
- 每次 API 请求前按顺序执行两层：先轻量预防（管单条消息大小），再昂贵兜底（管累积历史长度）
```

### Go

```markdown
# ch08: 上下文管理 Spec

## 1. 背景

LLM 上下文窗口有上限，但长任务里 tool 结果（Bash 输出、长文件）很容易在几轮内把窗口顶爆。没有上下文管理就意味着 Agent 跑到一半被 API 退回 `prompt_too_long`，会话失败、上下文丢失、用户得手动重启。

本章用「先廉价救火再花钱总结」两层策略解决：第 1 层不调 LLM，只做本地存盘 + 决策记录；第 2 层在 token 估算过阈值时整段摘要。第 1 层加一个跨轮持久的「替换决策日志」，让每个 tool result 的「替换/不替换」决定只做一次、之后字节相同地复读——这是 Anthropic prompt cache 命中所需的前缀稳定性的关键。

## 2. 目标

交付一套两层上下文管理 + 压缩后恢复：

- **Layer 1**：`toolresult.Apply` 每轮 agent loop 都跑。读取 `ContentReplacementState` 已记录的决策，对新候选评估「单条超限」和「聚合超限」两个规则；选中的 tool result 落盘换 preview 字符串，决定写入 state；过 `KeepRecentTurns` 轮的陈旧 tool result 裁为一行。返回**新的** `*conversation.Manager`，原 conv 不动。新决策追加写到 `<workDir>/.codebot/session/replacement_records.jsonl`。
- **Layer 2**：`compact.ManageContext` 在 token 估算占比超 `autoCompactThreshold` 时调 LLM 拼摘要，把整段会话换成 `[Compacted conversation summary]` + 确认消息。连续失败 `MaxConsecutiveAutoCompactFailures` 次后熔断不再重试。
- **Layer 2 后恢复**：`compact.RecoveryState` 跨轮记录每次 ReadFile 的字节快照与每次 Skill 调用的 SOP 文本。`autoCompact` 在生成摘要之后，把「最近读过的文件 / 已激活的技能 / 当前可用工具 / 收尾提示」四段拼到摘要 user 消息末尾，避免摘要替换后模型瞬间失去工作记忆。

两层在 Agent 主循环里串联：Layer 2 先跑（决定是否摘要 + 恢复）→ 写入系统提示 / 工具列表等 → Layer 1 在 `client.Stream` 调用前最后一刻跑、把 api_conv 喂给 LLM。手动入口 `compact.ForceCompact` 给 `/compact` 与反应式恢复用，跳过 Layer 1 直接走 Layer 2。

Anthropic 客户端在 system / tools 末项 / 最后一条 user message 末尾三处加 `cache_control: ephemeral` 标记；配合 Layer 1 的字节稳定 replacements，前缀缓存就能命中。

## 3. 功能需求

- F1: 提供 `EstimateTokens(messages)`，按 3.5 chars/token 比例近似，覆盖 content + tool args + tool_results + thinking blocks 四类内容。
- F2: 提供 `ContentReplacementState` 结构体（`SeenIDs map[string]struct{}` + `Replacements map[string]string`），及构造函数 `New()` 和深拷贝方法 `Clone()`。`SeenIDs` 收录每个判断过的 `tool_use_id`，`Replacements` 仅收录决定「替换」的那些 id 到 preview 字符串。不变量：`keys(Replacements) ⊆ SeenIDs`。
- F3: 提供 `Apply(conv, workDir, state)`，签名返回 `(*conversation.Manager, []Record, error)`，**不修改入参 conv**。对每条 tool result 按 4 步处理：
  1. id ∈ state.Replacements → 取出该字符串原样贴入 api_conv（纯查表，无 I/O）。
  2. id ∈ state.SeenIDs（但不在 Replacements）→ 保留原文。
  3. fresh 候选先跑 Pass 1：单条 content 超 `SingleResultLimit` → spill 到 `<workDir>/.codebot/tool_results/<toolUseID>` → 生成 preview → 写入 state。
  4. 剩余 fresh 跑 Pass 2：消息聚合 > `MessageAggregateLimit` 时按 content 长度降序挑，直到聚合压回上限。未被挑中的 fresh 标 seen 冻结为「不替换」。
- F4: spill 失败（disk 满等）不抛错：id 进 SeenIDs，**不进** Replacements。下次该 id 走 frozen 分支，继续发原文，决定仍是冻结的。
- F5: 跳过 ReadFile 回读 spill 文件的场景：`buildToolUseIndex` 索引 tool_use_id → ToolUseBlock，`isSpillReadback` 识别 `ReadFile` 工具读取 `<spillDir>/...` 的 readback，直接 freeze 为原文不再 spill。
- F6: Pass 3 陈旧裁剪：超过 `KeepRecentTurns` 轮的消息里，超过 `OldResultSnipChars` 字符的 tool result 整体替换为 `[Stale output snipped: N chars]` 一行。在 Pass 1/2 输出的新 history 上跑，仍然 stateless。
- F7: 提供 `Record` 结构体 + JSONL I/O：`AppendRecords` 追加写到 `<sessionDir>/replacement_records.jsonl`，`LoadRecords` 读回；缺文件不报错（返回 nil）。
- F8: 提供 `Reconstruct(messages, records, inheritedReplacements)` 从 transcript 重建 state：seed `SeenIDs` 用 messages 里所有 candidate id；从 records 填 Replacements；可选 inheritedReplacements 做 fork-resume gap-fill。
- F9: Layer 2 `autoCompact` 流程：构造摘要 prompt（含 `summarySystemPrompt` 与对话回放，tool_result 内容超过 500 字节截断）→ 调 `llm.Client.Stream` 收文本 → `formatCompactSummary` 三档回退（取 `<summary>` / 砍 `<analysis>` / 直接 trim） → 用 `[Compacted conversation summary]` + assistant 确认消息**就地替换**整段 conversation。
- F10: 熔断 `AutoCompactTrackingState`：单字段 `ConsecutiveFailures int`，由调用方持有。`ManageContext` 在 LLM 调用前检查 `>= MaxConsecutiveAutoCompactFailures` 则直接 skip Layer 2；成功清零，失败 +1；`tracking == nil` 时熔断禁用。
- F11: `ForceCompact` 手动入口，无视阈值直接走 `autoCompact`，跳过 Layer 1。
- F12: Anthropic 客户端 `Stream` 在三处打 `cache_control: ephemeral`：system prompt 的 TextBlockParam、tools 数组最后一个 Tool、最后一条 user message 的末块（text 或 tool_result）。
- F13: Agent.Run 反应式恢复：流式调用收到 `*llm.ContextTooLongError` 时调 `ForceCompact` 重试当前轮。
- F14: 子 Agent fork 路径在 `AgentTool.runFork` 末尾把 `subAgent.ReplacementState = t.ParentReplacementState.Clone()`，让父子共享 byte-stable 前缀但各自独立演化。
- F15: `compact.RecoveryState` 结构体（`files map[string]FileReadRecord` + `skills map[string]SkillInvocationRecord`，加 `sync.Mutex`）和构造 `NewRecoveryState()`；`RecordFileRead(path, content)` / `RecordSkillInvocation(name, body)` 都接受 nil receiver 做 no-op，并发安全。`Agent.RecoveryState` 字段在 `agent.New` 时 eagerly 初始化。
- F16: `ReadFile` 成功后在 `Agent.executeSingleTool` 末尾 `os.ReadFile(file_path)` 重读原始字节，调 `a.RecoveryState.RecordFileRead(...)` 入帐（避免把工具输出里的行号前缀也快照进去）。Skill 走 inline / fork 两条路径时由 `internal/tui/tui.go` 在调用 `skills.RunInline / RunFork` 前后调 `m.ag.RecoveryState.RecordSkillInvocation(...)`。
- F17: 限额常量 `RecoveryFileLimit = 5` / `RecoveryTokensPerFile = 5_000` / `RecoverySkillsBudget = 25_000` / `RecoveryTokensPerSkill = 5_000` 在 `internal/compact/recovery.go` 顶部定义。`approxTokens` 沿用 3.5 chars/token；`truncateByTokens` 按预算硬切并追加 `… (content truncated)` 标记。
- F18: `BuildRecoveryAttachment(state *RecoveryState, toolSchemas []map[string]any) string` 渲染四段（顺序：Recently read files → Active skills → Available tools → Note）；任一段为空就跳过；全空返回 `""`。`autoCompact` 把返回的字符串用 `\n\n---\n\n` 接到摘要消息后面。`ManageContext` 与 `ForceCompact` 透传 `recovery + toolSchemas`，且 Agent 主循环在 `ManageContext` 前先用 `currentToolSchemas()` 算一次工具表，避免恢复消息列出的工具与下一次 `client.Stream` 看到的不一致。

## 4. 非功能需求

- N1: Layer 1 必须廉价：纯本地文件 I/O + 字符串改写，不调 LLM；每轮 agent loop 都跑也不能成为瓶颈。
- N2: `Apply` 不能 mutate 入参 `conv` —— 通过新建 `*Manager` 并 `appendMessage` 重放方式产出 api_conv。这是 Layer 1 整体设计的基石；测试用 `TestApplyDoesNotMutateConv` 守住。
- N3: 已决策 id 的复读必须**字节一致**：从 `state.Replacements` 拿出来的字符串直接赋给 `decisions[id]`，不重新读盘、不重新格式化。这是 prompt cache 命中的硬约束。
- N4: spill 写盘幂等：`writeSpill` 同 size 文件已存在则跳过；spill 文件路径稳定（`<workDir>/.codebot/tool_results/<toolUseID>`），不含时间戳。
- N5: Layer 2 期间不能再触发新的 tool call —— 整个 summary 流是一次性 `Stream` 调用，不绕回 agent 主循环。
- N6: Layer 2 替换 conversation 用就地写法（`*conv = *compacted`），让调用方持有的 `*conversation.Manager` 指针保持有效。
- N7: 当 `tracking` 为 nil（测试或一次性脚本场景）熔断器禁用，不能崩。
- N8: 子 Agent fork 的 state 必须是父 state 的**独立深拷贝**：子端 mutate 不影响父端，反向亦然。测试用 `TestCloneIndependent` 守住。
- N9: `RecoveryState` 必须并发安全：`StreamingExecutor.Submit` 把每次 `executeSingleTool` 跑在独立 goroutine 里，多个 ReadFile 可能并发回写。结构体内 `sync.Mutex` 保护两张 map；`Record*` 方法在 nil receiver 上直接 return，保证测试 / 一次性脚本不需要构造也不崩。
- N10: 恢复块限额是**硬上限**：5 个文件、单文件 5K token、技能预算 25K token、单技能 5K token。超出预算时静默丢弃（不抛错），保证压缩输出体积可预测——压缩后摘要 + 恢复总长稳定在约 60K token 以内，远低于 0.80 阈值。

## 5. 设计概要

- 核心包结构:
  - `internal/toolresult/`（新包，4 个文件）:
    - `state.go` — `ContentReplacementState` 结构体 + `New / Clone`
    - `record.go` — `Record` + `AppendRecords / LoadRecords` + `RecordsFilename`
    - `budget.go` — 阈值常量、`Apply`、内部辅助（spill / readback 检测 / snipStale / buildManager）
    - `reconstruct.go` — `Reconstruct`
  - `internal/compact/`（Layer 2 + 恢复）:
    - `compact.go` — `autoCompactThreshold` / `MaxConsecutiveAutoCompactFailures` / `AutoCompactTrackingState` / `EstimateTokens` / `ManageContext` / `ForceCompact` / `autoCompact` / `formatCompactSummary` / `summarySystemPrompt`
    - `recovery.go` — `RecoveryFileLimit / RecoveryTokensPerFile / RecoverySkillsBudget / RecoveryTokensPerSkill` 常量 / `FileReadRecord` / `SkillInvocationRecord` / `RecoveryState` + `NewRecoveryState / RecordFileRead / RecordSkillInvocation / snapshotFiles / snapshotSkills` / `BuildRecoveryAttachment` / `approxTokens / truncateByTokens / firstLine`
- 主流程（每轮 agent loop）:
  - 主循环开头：先 `toolSchemas := a.currentToolSchemas()` 算一次工具表（含 `ToolNameFilter` 过滤），后续 `ManageContext` 与 `client.Stream` 复用同一份，保证恢复消息与 API 请求看到的工具集一致。
  - `compact.ManageContext(ctx, conv, client, workDir, contextWindow, tracking, a.RecoveryState, toolSchemas)` 算 token 估算占比，过阈值且未熔断 → 走 `autoCompact` 整段摘要替换 conv，并在摘要 user 消息末尾追加恢复块。
  - 系统提示 / 工具列表 / hook 通知 / plan-mode reminder 写入 conv。
  - 在 `client.Stream` 调用前一刻：`apiConv, newRecords, _ := toolresult.Apply(conv, workDir, replacementState)`。
  - 新 records 追加写入 `<workDir>/.codebot/session/replacement_records.jsonl`。
  - `client.Stream(ctx, apiConv, toolSchemas)` —— api_conv 是 Layer 1 处理后的视图，conv 原样保留供下一轮使用。
- 主流程（工具调用快照）:
  - `Agent.executeSingleTool` 在 `tool.Execute` 之后，若 `tc.ToolName == "ReadFile"` 且 `!result.IsError` → `os.ReadFile(file_path)` 重读原始字节，写入 `a.RecoveryState`。
- 主流程（Skill 调用快照）:
  - 用户输入 `/<skill-name>` → `internal/tui/tui.go` 的 inline 分支调 `skills.RunInline` 返回 body 后，立刻 `m.ag.RecoveryState.RecordSkillInvocation(skill.Meta.Name, body)`。
  - Fork 分支在调 `skills.RunFork` 之前先 record `skill.PromptBody`，因为 RunFork 不回传渲染后的 body。
- 主流程（手动 `/compact`）:
  - TUI 命令 → `compact.ForceCompact(ctx, conv, client, contextWindow, m.ag.RecoveryState, m.ag.Registry.GetAllSchemas(m.ag.Protocol))` → 跳过 Layer 1 直接走 `autoCompact`，恢复块同样附在摘要后。
- 主流程（反应式恢复）:
  - LLM 流返回 `*llm.ContextTooLongError` → Agent `handleStreamError` 调 `ForceCompact(ctx, conv, a.Client, a.ContextWindow, a.RecoveryState, a.currentToolSchemas())` → 重试当前轮。
- 主流程（fork 子 Agent）:
  - `AgentTool.runFork` 创建子 Agent → `subAgent.ReplacementState = t.ParentReplacementState.Clone()` → 子 Agent 用 clone 后的 state 开始自己的循环。
- Anthropic 客户端缓存断点:
  - `params.System` 用 `[]TextBlockParam{{Text:..., CacheControl: NewCacheControlEphemeralParam()}}`。
  - `sdkTools` 末项的 `OfTool.CacheControl = NewCacheControlEphemeralParam()`。
  - `markLastUserTailForCache(params.Messages)` 给最后一条 user MessageParam 的末块（OfText 或 OfToolResult）打 CacheControl。
- 与其他模块的交互:
  - 依赖 `internal/conversation`（操作 Manager / Message / ToolUseBlock / ToolResultBlock / ThinkingBlock）。
  - 依赖 `internal/llm`（Stream 摘要 / `*ContextTooLongError` 错误类型 / `anthropic.CacheControlEphemeralParam`）。
  - 被 `internal/agent`（主循环、Run 错误恢复）、`internal/agents/agent_tool.go`（fork clone）、`internal/tui`（`/compact` 命令、AgentTool backfill）调用。

## 6. Out of Scope

- 跨进程 / 跨会话的压缩缓存。
- Micro-compact 与分段压缩：本仓库一次直接全量摘要，不做 partial / per-segment 压缩。
- 持久化的 `RecoveryState`：进程退出后状态丢失，不做磁盘落盘。下一次启动时由用户自然触发 ReadFile / Skill 调用重新填充。
- Session memory compaction：与 ch09 记忆系统配合，本章不做。
- 用真实 tokenizer 替代 chars/token 近似估算。
- 进度回调或 UI 流式预览，本章只在压缩完成后回传一行 status。
- 完整 resume 流程：transcript records 已落盘且 `Reconstruct` 可用，但 resume 主流程是后续章节的事；本章只把 hook 留出来。
- Pass 3 陈旧裁剪的边界穿越漂移：从「未裁剪」到「裁剪」那一轮前缀变了，会导致一次 cache miss。修法（提前裁 / 决策冻结裁剪）不在本章范围。
- 配置化阈值：所有阈值都是包级常量，调整需改源码重编译，不读 config.yaml。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch08: 上下文管理 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。每条任务记录实际落地的文件与行号。

## T1: 阈值常量

- 影响文件: `internal/toolresult/budget.go:16-36`、`internal/compact/compact.go:22-31`
- 依赖任务: 无
- 完成标准: `internal/toolresult/budget.go` 定义 `SingleResultLimit = 15000`、`MessageAggregateLimit = 20000`、`OldResultSnipChars = 2000`、`KeepRecentTurns = 10`、`SpillSubdir = ".codebot/tool_results"`；`internal/compact/compact.go` 定义 `autoCompactThreshold = 0.80` 与 `MaxConsecutiveAutoCompactFailures = 3`；`type AutoCompactTrackingState struct { ConsecutiveFailures int }` 在 `compact.go:34` 定义。

## T2: `ContentReplacementState` + 构造 / 深拷贝

- 影响文件: `internal/toolresult/state.go:18-50`
- 依赖任务: T1
- 完成标准: `type ContentReplacementState struct { SeenIDs map[string]struct{}; Replacements map[string]string }`；`New() *ContentReplacementState` 返回空容器；`Clone() *ContentReplacementState` 复制两个 map 的所有键值，源与拷贝彼此独立。

## T3: `Record` 与 JSONL 持久化

- 影响文件: `internal/toolresult/record.go:15-79`
- 依赖任务: T1
- 完成标准: `type Record struct { Kind, ToolUseID, Replacement string }`；`RecordsFilename = "replacement_records.jsonl"`；`AppendRecords(sessionDir, records)` 用 `os.OpenFile(..., O_APPEND|O_CREATE)` + `json.Encoder` 追加，空切片直接 return；`LoadRecords(sessionDir)` 用 `bufio.Scanner` 逐行读，缺文件返回 (nil, nil)。

## T4: `EstimateTokens`

- 影响文件: `internal/compact/compact.go:77-92`
- 依赖任务: T1
- 完成标准: 函数对 content / tool args（`json.Marshal`）/ tool_results / thinking_blocks 四类内容按 3.5 chars/token 估算并加常量偏置（content +4，tool_use +50，tool_result +10）；输入 nil 返回 0；`TestEstimateTokensZeroAndPopulated` 通过。

## T5: 摘要 prompt 与 `formatCompactSummary`

- 影响文件: `internal/compact/compact.go:43-75, 211-226`
- 依赖任务: T1
- 完成标准: `summarySystemPrompt` 指示模型先产 `<analysis>` 再产 `<summary>`；`formatCompactSummary` 三种回退：取 `<summary>` 内容 / 砍掉 `<analysis>` / 直接 trim；`TestFormatCompactSummary` 4 个 case 全部通过。

## T6: spill / 回读检测 / `buildManager`

- 影响文件: `internal/toolresult/budget.go:236-353`
- 依赖任务: T1
- 完成标准:
  - `writeSpill(dir, toolUseID, content)` 把内容写到 `<dir>/<toolUseID>`，已写过相同 size 文件不重写（幂等）。
  - `buildToolUseIndex(messages)` 把 tool_use_id 索引到 `ToolUseBlock`。
  - `isSpillReadback(tu, absSpillDir)` 识别 `ReadFile` 工具读取 spill 目录下文件的场景。
  - `buildSpillPreview(originalSize, path)` 输出固定格式 `[Result of N chars saved to PATH — read with ReadFile if needed]`。
  - `buildManager(messages)` 通过 `conversation.NewManager()` 加 `addX` 系列方法重放消息，产出新的独立 `*Manager`。

## T7: `Apply` 主流程（4 步分类 + Pass 1 + Pass 2 + freeze）

- 影响文件: `internal/toolresult/budget.go:58-234`
- 依赖任务: T2, T6
- 完成标准: `Apply(conv, workDir, state) (*conversation.Manager, []Record, error)` 实现 §3.3 算法：
  - 阶段 1: 对每个 tr 分四类——`state.Replacements` 命中 → 复读；`state.SeenIDs` 命中 → 冻结原文；外部已标 `[Result of` 或 `[Stale output snipped:` 前缀 → 视为已知决策；其余进 fresh。
  - 阶段 2: fresh 中 content 超 `SingleResultLimit` 调 `writeSpill` + `buildSpillPreview`，写入 state 与 records；spill 失败 freeze 原文。命中 readback 跳过 spill 直接 freeze。
  - 阶段 3: 计算 `total = Σdecisions + Σremaining.content`；`> MessageAggregateLimit` 时按 content 长度降序挑直到压回上限。
  - 阶段 4: 未决策的 fresh 全部 `SeenIDs.add`、`decisions[id] = tr.Content`。
  - 末段: 用 `decisions` 构造新 `[]ToolResultBlock` 保持原顺序 → `snipStale` → `buildManager`。
- `TestApplyDoesNotMutateConv / TestFirstCallFreezesUnreplaced / TestReplacementByteIdentical / TestFrozenNeverReplaced / TestAggregateOnlyPicksFresh` 全部通过。

## T8: Pass 3 陈旧裁剪 `snipStale`

- 影响文件: `internal/toolresult/budget.go:243-290`
- 依赖任务: T7
- 完成标准: 数 `assistant && len(ToolUses)==0` 当作一轮，总轮数 `> KeepRecentTurns` 才生效；boundary 前的消息里 content 超 `OldResultSnipChars` 且未被 `isAlreadyReplaced` 前缀标记的 tool result 整体替换为 `[Stale output snipped: N chars]`；返回新 slice，不动入参。

## T9: `Reconstruct`

- 影响文件: `internal/toolresult/reconstruct.go:12-46`
- 依赖任务: T2, T3
- 完成标准: 先 seed `SeenIDs` = `{ tr.ToolUseID | for tr in m.ToolResults, for m in messages }`；按 `r.Kind == "tool-result"` 过滤 records，命中 candidate 的写入 `Replacements`；可选 `inheritedReplacements` 做 gap-fill（candidate ∩ 未被 records 覆盖）；`TestReconstructFromRecords / TestReconstructWithInheritedParent` 全部通过。

## T10: Layer 2 `autoCompact`

- 影响文件: `internal/compact/compact.go:155-208`
- 依赖任务: T4, T5
- 完成标准: 拼回放（tool_result content 超 500 字节截断）→ `client.Stream(ctx, summaryConv, nil)` 收摘要 → `formatCompactSummary` → 用 `[Compacted conversation summary]\n\n<summary>` + assistant 确认消息替换整段 conversation（`*conv = *compacted`）；返回 `Compacted: N → M estimated tokens`。

## T11: `ManageContext` 总入口 + 熔断

- 影响文件: `internal/compact/compact.go:107-142`
- 依赖任务: T4, T10
- 完成标准: 算 token 估算占比，未过 `autoCompactThreshold` 返回 `("", nil)`；过阈值检查熔断（`tracking != nil && tracking.ConsecutiveFailures >= MaxConsecutiveAutoCompactFailures` 时直接 skip）；执行 `autoCompact`，成功清零计数失败 +1；`tracking == nil` 时熔断禁用。

## T12: `ForceCompact` 手动入口

- 影响文件: `internal/compact/compact.go:144-152`
- 依赖任务: T10
- 完成标准: 跳过 Layer 1 与阈值判断，直接调 `autoCompact`，返回压缩前后 token 估算字符串。

## T13: Agent 集成（state 字段 + Apply 调用 + records 持久化）

- 影响文件: `internal/agent/agent.go:19, 47-55, 109-117, 196-211`
- 依赖任务: T2, T7, T11
- 完成标准:
  - import `codebot/internal/toolresult`。
  - `Agent` struct 新增字段 `ReplacementState *toolresult.ContentReplacementState`。
  - `agent.New` 构造时 `ReplacementState: toolresult.New()` eagerly 初始化。
  - 主循环开头调 `compact.ManageContext(...)`；流程中段写入各种 system reminder；在 `client.Stream` 调用前一刻：`apiConv, newRecords, _ := toolresult.Apply(conv, a.WorkDir, a.ReplacementState)`；非空 records 调 `toolresult.AppendRecords(a.WorkDir, newRecords)`（失败 silently 忽略）。
  - `client.Stream(ctx, apiConv, toolSchemas)` —— 传 api_conv 而不是 conv。

## T14: Fork 状态继承

- 影响文件: `internal/agents/agent_tool.go:18, 77-83, 437-446`、`internal/tui/tui.go:405, 1152`
- 依赖任务: T2, T13
- 完成标准:
  - import `codebot/internal/toolresult`；`AgentTool` struct 新增字段 `ParentReplacementState *toolresult.ContentReplacementState`。
  - `runFork` 创建子 Agent 后判断 `t.ParentReplacementState != nil` → `subAgent.ReplacementState = t.ParentReplacementState.Clone()`。
  - TUI 的两个 AgentTool backfill 点（`tui.go:405` 和 `tui.go:1152`）补 `at.ParentReplacementState = ag.ReplacementState`。

## T15: 反应式恢复

- 影响文件: `internal/agent/agent.go:267` 附近的 `handleStreamError`
- 依赖任务: T12
- 完成标准: 流式调用收到 `*llm.ContextTooLongError` 时调 `compact.ForceCompact(ctx, conv, a.Client, a.ContextWindow)` 重试当前轮。

## T16: Anthropic 客户端缓存断点

- 影响文件: `internal/llm/anthropic.go:105-138, 263-290`
- 依赖任务: 无
- 完成标准:
  - `params.System` 改为 `[]anthropic.TextBlockParam{{Text: c.systemPrompt, CacheControl: anthropic.NewCacheControlEphemeralParam()}}`。
  - `sdkTools` 非空时给末项的 `OfTool.CacheControl` 赋 `NewCacheControlEphemeralParam`。
  - 新增 `markLastUserTailForCache(messages)`：倒序找到最后一条 user MessageParam，对其末块按 `OfText` 或 `OfToolResult` 分别打 `CacheControl`。
  - `params.Messages` 构造后立刻调用上述函数。

## T17: 测试覆盖

- 影响文件: `internal/toolresult/state_test.go`、`internal/toolresult/record_test.go`、`internal/toolresult/budget_test.go`、`internal/compact/compact_test.go`
- 依赖任务: T2–T11
- 完成标准:
  - `state_test.go`: `TestNewReturnsEmpty` + `TestCloneIndependent`。
  - `record_test.go`: `TestAppendAndLoadRecordsRoundtrip` + `TestLoadRecordsMissingFile`。
  - `budget_test.go`: `TestApplyDoesNotMutateConv` / `TestFirstCallFreezesUnreplaced` / `TestReplacementByteIdentical` / `TestFrozenNeverReplaced` / `TestAggregateOnlyPicksFresh` / `TestReconstructFromRecords` / `TestReconstructWithInheritedParent`。
  - `compact_test.go`: `TestFormatCompactSummary` 4 case + `TestEstimateTokensZeroAndPopulated`。
  - `go test ./internal/toolresult/ ./internal/compact/` 全部通过。

## T18: 端到端验证

- 影响文件: 无（仅运行验证）
- 依赖任务: T13–T17
- 完成标准:
  - `go build ./...` 通过。
  - `go test ./internal/toolresult/ ./internal/compact/ ./internal/agent/ -count=1` 全部通过。
  - 制造一个会产生大 tool result 的会话（连续 Bash 大输出），观察 `.codebot/tool_results/<tool_use_id>` 文件落地，且 `.codebot/session/replacement_records.jsonl` 有对应 records。
  - 制造一个会爆 context 的会话，看到 `CompactEvent` 通知压缩前后 token 数。

## T19: `RecoveryState` 与限额常量

- 影响文件: `internal/compact/recovery.go:1-100`
- 依赖任务: T1
- 完成标准:
  - 常量 `RecoveryFileLimit = 5` / `RecoveryTokensPerFile = 5_000` / `RecoverySkillsBudget = 25_000` / `RecoveryTokensPerSkill = 5_000` / `recoveryCharsPerToken = 3.5` 在 `recovery.go:18-22` 定义。
  - `type FileReadRecord struct { Path, Content string; Timestamp time.Time }` 与 `type SkillInvocationRecord struct { Name, Body string; Timestamp time.Time }` 定义。
  - `type RecoveryState struct { mu sync.Mutex; files map[string]FileReadRecord; skills map[string]SkillInvocationRecord }` + `NewRecoveryState()` 构造非 nil 容器。
  - `RecordFileRead(path, content)` / `RecordSkillInvocation(name, body)` 在 nil receiver 上直接 return，正常 receiver 加 mutex 写入并以 `time.Now()` 打时间戳。

## T20: `BuildRecoveryAttachment`

- 影响文件: `internal/compact/recovery.go:111-200`
- 依赖任务: T19
- 完成标准:
  - `snapshotFiles(limit)` / `snapshotSkills()` 复制并按 timestamp 降序排序，文件再 cap 到 limit。
  - `truncateByTokens(s, budget)` 按 `recoveryCharsPerToken` 折算 byte 上限，超额截断尾部并追加 `\n… (content truncated)`。
  - `approxTokens(s) = int(len(s) / 3.5)`；`firstLine(s)` 返回第一行非空 trim 文本。
  - `BuildRecoveryAttachment(state, toolSchemas)` 按顺序输出「`## Recently read files` / `## Active skills` / `## Available tools` / `## Note`」四段；空 state + 空 schemas 时返回 `""`；技能段累计字节超 `RecoverySkillsBudget` 时 break。
  - `internal/compact/recovery_test.go` 全部 5 个测试通过：`TestRecoveryStateNilSafe / TestBuildRecoveryAttachmentEmits / TestRecoveryFileLimitAndOrder / TestRecoveryTruncatesPerFile / TestRecoverySkillsBudget`。

## T21: `autoCompact` / `ManageContext` / `ForceCompact` 签名扩展

- 影响文件: `internal/compact/compact.go:107-205`
- 依赖任务: T10, T11, T12, T19, T20
- 完成标准:
  - `ManageContext` 与 `ForceCompact` 多两个参数 `recovery *RecoveryState, toolSchemas []map[string]any`，全部透传给 `autoCompact`。
  - `autoCompact` 在生成 `finalSummary` 后调 `BuildRecoveryAttachment(recovery, toolSchemas)`，非空时用 `\n\n---\n\n` 连到 `[Compacted conversation summary]\n\n<summary>` 后面。
  - assistant 确认消息（`"Understood. I'll continue based on this context."`）依旧附在 user 消息之后。
  - `internal/compact/compact_test.go` 既有测试全部通过（签名扩展不破坏旧用例）。

## T22: Agent 与 TUI 集成

- 影响文件: `internal/agent/agent.go:55-118, 145-150, 415-422, 555-565`、`internal/tui/tui.go:830, 1676, 1733`
- 依赖任务: T19, T20, T21
- 完成标准:
  - `Agent` struct 新增 `RecoveryState *compact.RecoveryState` 字段，`agent.New` eagerly 调 `compact.NewRecoveryState()`。
  - 抽出 `(*Agent).currentToolSchemas()` 把「registry.GetAllSchemas + ToolNameFilter + IsSystemTool bypass」组合好；主循环开头计算一次 `toolSchemas`，先后喂给 `compact.ManageContext` 与 `client.Stream`，删除老的同地段重复逻辑。
  - `handleStreamError` 走 `*llm.ContextTooLongError` 分支时把 `a.RecoveryState` 与 `a.currentToolSchemas()` 传给 `ForceCompact`。
  - `executeSingleTool` 在 `tool.Execute` 之后判断 `!result.IsError && tc.ToolName == "ReadFile"`，`os.ReadFile(file_path)` 重读后写入 `a.RecoveryState.RecordFileRead(...)`。
  - `internal/tui/tui.go`：inline skill 分支在 `skills.RunInline` 后调 `m.ag.RecoveryState.RecordSkillInvocation(skill.Meta.Name, body)`；fork skill 分支在 `skills.RunFork` 之前调 `m.ag.RecoveryState.RecordSkillInvocation(skill.Meta.Name, skill.PromptBody)`；`/compact` 路径在调 `compact.ForceCompact` 前先取 `recovery + schemas`，传给新签名。

## 进度

- T1-T22（含「压缩后恢复」相关 T19-T22）

```

````markdown
# ch08: 上下文管理 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性

### 1.1 `internal/toolresult/` 包

- [ ] 常量 `SingleResultLimit = 15000`、`MessageAggregateLimit = 20000`、`OldResultSnipChars = 2000`、`KeepRecentTurns = 10`、`SpillSubdir = ".codebot/tool_results"` 在 `internal/toolresult/budget.go:16-36` 定义。
- [ ] `type ContentReplacementState struct { SeenIDs map[string]struct{}; Replacements map[string]string }` 在 `internal/toolresult/state.go:18` 定义。
- [ ] 构造 `New() *ContentReplacementState` 在 `state.go:25` 返回空容器。
- [ ] 方法 `(*ContentReplacementState).Clone() *ContentReplacementState` 在 `state.go:36` 实现深拷贝。
- [ ] `type Record struct { Kind, ToolUseID, Replacement string }` 在 `record.go:15` 定义；`RecordsFilename = "replacement_records.jsonl"` 在 `record.go:22` 定义。
- [ ] `AppendRecords(sessionDir, records) error` 在 `record.go:26` 实现：空切片直接 return；自动 `MkdirAll(sessionDir, 0o755)`；用 `OpenFile(O_WRONLY|O_CREATE|O_APPEND)` 追加；`Kind` 为空时填 `"tool-result"`。
- [ ] `LoadRecords(sessionDir) ([]Record, error)` 在 `record.go:53` 实现：缺文件返回 (nil, nil)；用 `bufio.Scanner` 逐行 `json.Unmarshal`。
- [ ] `Apply(conv, workDir, state) (*conversation.Manager, []Record, error)` 在 `budget.go:58` 实现，包含完整 4 阶段算法 + Pass 3 stale-snip。
- [ ] `Reconstruct(messages, records, inheritedReplacements) *ContentReplacementState` 在 `reconstruct.go:12` 实现，包括 candidate-only 过滤与 inheritedReplacements gap-fill。
- [ ] 函数 `buildSpillPreview` 在 `budget.go:236` 实现：返回 `[Result of N chars saved to PATH — read with ReadFile if needed]`（这个字符串是 byte-stable 的 anchor，不能轻改）。
- [ ] 函数 `snipStale` 在 `budget.go:243` 实现：基于轮数 boundary + `OldResultSnipChars` 阈值，输出 `[Stale output snipped: N chars]`。
- [ ] 函数 `buildToolUseIndex` / `isSpillReadback` 在 `budget.go:293, 307` 实现：识别 `ReadFile` 工具调用 spill 目录的回读，跳过二次 spill。
- [ ] 函数 `writeSpill` 在 `budget.go:322` 实现：同 size 文件已存在则跳过；路径 `<dir>/<toolUseID>` 稳定不带时间戳。
- [ ] 函数 `buildManager` 在 `budget.go:344` 实现：通过 `conversation.NewManager()` + `addX` 方法重放消息产出独立 `*Manager`。
- [ ] Apply 内部「外部已标 persisted」分支：检测 `[Result of` / `[Stale output snipped:` 前缀，直接写 `state.Replacements` 与 `records`，不再 spill。

### 1.2 `internal/compact/` 包（Layer 2 入口与摘要）

- [ ] 常量 `autoCompactThreshold = 0.80` 在 `internal/compact/compact.go:24` 定义。
- [ ] 常量 `MaxConsecutiveAutoCompactFailures = 3` 在 `compact.go:30` 定义。
- [ ] `type AutoCompactTrackingState struct { ConsecutiveFailures int }` 在 `compact.go:34` 定义。
- [ ] `summarySystemPrompt` 在 `compact.go:43` 定义，要求模型输出 `<analysis>` + `<summary>` 两段。
- [ ] `EstimateTokens(messages) int` 在 `compact.go:77` 实现：覆盖 content / tool args / tool_results / thinking_blocks 四源（共 4 个 for 分支）。
- [ ] `ManageContext` 在 `compact.go:107` 实现：算 token 估算 → 未过阈值返回 `("", nil)` → 过阈值检查熔断 → `autoCompact` → 成功清零 / 失败 +1；`tracking == nil` 时熔断禁用。
- [ ] `ForceCompact` 在 `compact.go:144` 实现：直接调 `autoCompact`，跳过阈值判断。
- [ ] `autoCompact` 在 `compact.go:155` 实现：拼对话回放（tool_result content 超 500 字节截断）→ `client.Stream` 摘要 → `formatCompactSummary` → 用 summary + ack 替换 conversation。
- [ ] `formatCompactSummary` 在 `compact.go:211` 实现：优先取 `<summary>` 内容；fallback 砍 `<analysis>`；再 fallback 原文 trim。
- [ ] `ManageContext` / `ForceCompact` / `autoCompact` 都接受 `recovery *RecoveryState` 与 `toolSchemas []map[string]any`，autoCompact 在生成 summary 后用 `\n\n---\n\n` 拼接 `BuildRecoveryAttachment` 的返回值。

### 1.3 `RecoveryState` 与恢复块（`internal/compact/recovery.go`）

- [ ] 限额常量 `RecoveryFileLimit = 5` / `RecoveryTokensPerFile = 5_000` / `RecoverySkillsBudget = 25_000` / `RecoveryTokensPerSkill = 5_000` / `recoveryCharsPerToken = 3.5` 在 `recovery.go:18-22` 定义。
- [ ] `type FileReadRecord` / `type SkillInvocationRecord` 含 `Timestamp time.Time` 字段。
- [ ] `RecoveryState` 用 `sync.Mutex` 守护 `files` / `skills` 两张 map；`NewRecoveryState()` 返回非 nil 容器。
- [ ] `RecordFileRead(path, content)` / `RecordSkillInvocation(name, body)` 在 nil receiver 上直接 return；正常 receiver 加锁写 + `time.Now()` 时间戳。
- [ ] `snapshotFiles(limit)` 与 `snapshotSkills()` 复制后按 timestamp 降序，文件再 cap 到 limit。
- [ ] `BuildRecoveryAttachment` 依次输出 `## Recently read files` / `## Active skills` / `## Available tools` / `## Note`；空 state + 空 schemas 时返回 `""`；技能预算超 `RecoverySkillsBudget` 时停止追加。
- [ ] `truncateByTokens` 按 `len(s) > budget * 3.5` 判断、超额截断并追加 `\n… (content truncated)`；`firstLine(s)` 返回第一行非空文本。

### 1.4 Agent / TUI 接入

- [ ] `Agent.RecoveryState *compact.RecoveryState` 字段（`internal/agent/agent.go:55-65` 附近）+ `agent.New()` eagerly 初始化为 `compact.NewRecoveryState()`。
- [ ] `(*Agent).currentToolSchemas()` 抽出（`agent.go:118-138`），主循环开头计算一次 `toolSchemas`，先喂 `ManageContext` 再复用给 `client.Stream`；老的「toolSchemas := a.Registry.GetAllSchemas(...) + ToolNameFilter 过滤」内联块被删除。
- [ ] `executeSingleTool` 在 `tool.Execute` 之后判定 `!result.IsError && tc.ToolName == "ReadFile"`，重读 `os.ReadFile(file_path)` 后写入 `a.RecoveryState`（`agent.go:555-565` 附近）。
- [ ] `handleStreamError` 走 `*llm.ContextTooLongError` 时把 `a.RecoveryState` 与 `a.currentToolSchemas()` 传给 `ForceCompact`。
- [ ] TUI `internal/tui/tui.go`：
  - inline skill 分支调 `skills.RunInline` 后立刻 `m.ag.RecoveryState.RecordSkillInvocation(...)`（约 `tui.go:830`）。
  - fork skill 分支调 `skills.RunFork` 之前调 `m.ag.RecoveryState.RecordSkillInvocation(skill.Meta.Name, skill.PromptBody)`（约 `tui.go:1735`）。
  - `/compact` 路径在调 `compact.ForceCompact` 前取 `recovery + schemas`，传给新签名（约 `tui.go:1676-1685`）。

### 1.5 Anthropic 客户端缓存断点

- [ ] `params.System` 用 `[]TextBlockParam{{Text:..., CacheControl: NewCacheControlEphemeralParam()}}`（`internal/llm/anthropic.go:107-114`）。
- [ ] `sdkTools` 非空时末项 `OfTool.CacheControl = NewCacheControlEphemeralParam()`（`anthropic.go:130-135`）。
- [ ] 函数 `markLastUserTailForCache` 在 `anthropic.go:272` 实现：倒序找到最后一条 user MessageParam，对其末块按 `OfText` 或 `OfToolResult` 分别打 CacheControl。
- [ ] `params.Messages` 构造后立刻调用 `markLastUserTailForCache(params.Messages)`（`anthropic.go:115`）。

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `grep -rn "toolresult\." /Users/codemelo/codebot/internal --include="*.go" | grep -v "_test.go" | grep -v "internal/toolresult/"` 至少 5 处非测试调用方：
  - `internal/agent/agent.go:19`（import）
  - `internal/agent/agent.go:55`（`Agent.ReplacementState` 字段类型）
  - `internal/agent/agent.go:116`（构造 `toolresult.New()`）
  - `internal/agent/agent.go:204`（`toolresult.Apply` 调用）
  - `internal/agent/agent.go:209`（`toolresult.AppendRecords` 调用）
  - `internal/agents/agent_tool.go:18`（import）
  - `internal/agents/agent_tool.go:83`（`ParentReplacementState` 字段类型）
  - `internal/agents/agent_tool.go:444`（`Clone()` 调用注入子 Agent）
- [ ] `grep -rn "compact\." /Users/codemelo/codebot/internal --include="*.go" | grep -v "_test.go" | grep -v "internal/compact/"` 至少 3 处非测试调用方：
  - Agent 主循环 `compact.ManageContext`
  - Agent 错误恢复 `compact.ForceCompact`
  - TUI `/compact` 命令 `compact.ForceCompact`
- [ ] `grep -rn "RecoveryState\b" /Users/codemelo/codebot/internal --include="*.go" | grep -v "_test.go" | grep -v "internal/compact/"` 至少 5 处：Agent 字段声明 + `agent.New` 构造 + `executeSingleTool` 记录 + `handleStreamError` 传参 + TUI 三处（inline / fork / compact）。
- [ ] TUI backfill 两处：`internal/tui/tui.go:405` 与 `internal/tui/tui.go:1152`，赋值 `at.ParentReplacementState = ag.ReplacementState`。
- [ ] 调用入口位于 `agent` 模块主循环（`internal/agent/agent.go:204` 在 `(*Agent).Run` 的 `for iteration := 1; ; iteration++` 内、`client.Stream` 调用之前）。
- [ ] 用户输入到本模块的路径可一句话描述:
  - 自动: agent loop 进入新一轮 → `compact.ManageContext` 判断 Layer 2 → 写入各种 reminder → `toolresult.Apply` 产出 apiConv → `client.Stream(ctx, apiConv, ...)` 发请求。
  - 手动: 用户在 TUI 输入 `/compact` → `tui.executeCommand` → 启 goroutine 调 `compact.ForceCompact` → 回传 `compactDoneMsg`。
  - 反应式: LLM 返回 `prompt_too_long` → `llm.ContextTooLongError` → `Agent.handleStreamError` 捕获 → `compact.ForceCompact` → loop 重试。
  - Fork: 父 Agent 调 Agent 工具触发 fork → `AgentTool.runFork` 创建子 Agent → 注入父 state 的 `Clone()` → 子 Agent 用克隆状态独立演化。
- [ ] **死代码核查**：`grep -rn "offloadAndSnip\|rebuildConversation\|alreadySpilled" /Users/codemelo/codebot/internal --include="*.go"` 零命中（旧 mutation 版的 Layer 1 已经不存在）。

## 3. 编译与测试

- [ ] `go build ./...` 通过。
- [ ] `go test ./internal/toolresult/ -v` 通过 11 个测试：`TestNewReturnsEmpty / TestCloneIndependent / TestApplyDoesNotMutateConv / TestFirstCallFreezesUnreplaced / TestReplacementByteIdentical / TestFrozenNeverReplaced / TestAggregateOnlyPicksFresh / TestReconstructFromRecords / TestReconstructWithInheritedParent / TestAppendAndLoadRecordsRoundtrip / TestLoadRecordsMissingFile`。
- [ ] `go test ./internal/compact/ -v` 通过：`TestFormatCompactSummary`（4 sub-case）+ `TestEstimateTokensZeroAndPopulated` + 5 个恢复测试（`TestRecoveryStateNilSafe / TestBuildRecoveryAttachmentEmits / TestRecoveryFileLimitAndOrder / TestRecoveryTruncatesPerFile / TestRecoverySkillsBudget`）。
- [ ] `go vet ./internal/toolresult/ ./internal/compact/` 无警告。

## 4. 端到端验证

- [ ] Layer 1 字节稳定性：
  - 制造一个一轮内并行调 5 个 Bash 命令、每个吐 4.5K 字符的会话（总 22.5K，触发 Pass 2）。
  - `Apply` 返回的 api_conv 里其中一条 tool_result content 变为 `[Result of ... chars saved to ...]`。
  - 下一轮再调一次 `Apply`，同一 tool_use_id 的 content 与上一轮完全相等（state.Replacements 复读）。
- [ ] Layer 1 不 mutate 原 conv：调 `Apply` 前后 `conv.GetMessages()` 各 tool_result.content 完全相等（已被 `TestApplyDoesNotMutateConv` 守住）。
- [ ] Layer 1 frozen 不再替换：测试 `TestFrozenNeverReplaced` 验证「第一轮未替换的 id 在后续轮即使聚合超限也不被选中」。
- [ ] Layer 2 触发：制造长对话使 token 估算占比 > 0.80，事件流出现 `CompactEvent` 包含 `Compacted: N → M estimated tokens`。
- [ ] Layer 2 熔断：人为让 `autoCompact` 连续失败 3 次后第 4 次直接 skip（看 `tracking.ConsecutiveFailures` 是否 ≥ `MaxConsecutiveAutoCompactFailures`）。
- [ ] Spill 落盘：长 Bash 输出后 `<workDir>/.codebot/tool_results/` 目录下出现以 `tool_use_id` 命名的文件。
- [ ] Transcript 落盘：`<workDir>/.codebot/session/replacement_records.jsonl` 出现新条目，`jq .` 可解析。
- [ ] Fork 隔离：子 Agent 创建后修改自己 state 的 SeenIDs / Replacements 不影响父 Agent。
- [ ] 反应式: LLM 返回 `prompt_too_long` → 自动 `ForceCompact` 后继续 loop。
- [ ] TUI `/compact` 手动入口：输入 `/compact` 后看到 `Compacting conversation…` → `Compacted: ...` 提示。
- [ ] 恢复块文件段：在压缩前先 `ReadFile` 两个不同路径，触发 `/compact` 后摘要消息里同时出现 `## Recently read files` 段、两个 `### <绝对路径>` 子段、且每段内容以 ``` 包住。
- [ ] 恢复块技能段：先 `/<skill-name>` 激活一个 skill，再触发 `/compact`，摘要消息出现 `## Active skills` 段并包含该 skill 名与 SOP 片段。
- [ ] 恢复块工具段：摘要消息出现 `## Available tools` 段，并把当前 registry 里的工具按 `- 名字 — 描述首行` 形式逐行列出。
- [ ] 恢复块收尾提示：摘要消息以 `## Note` 段收尾，强调若需要原文请重新读文件而不是靠摘要猜。
- [ ] 限额硬上限：人造 6+ 个 ReadFile 后压缩，恢复块只列最近 5 个；单文件超 5K token 部分被 `… (content truncated)` 标记。

## 5. 文档

- [ ] spec.md / tasks.md / checklist.md 三件套齐全且最新（位于 `docs/go/ch08/`）。
- [ ] 跨分支设计文档存在：`docs/extras/content-replacement-state.md` 描述 ContentReplacementState 三分支统一设计与 Design B（不 mutate）契约。
- [ ] commit 信息标注 `ch08` 与三件套关闭状态。

````

### Python

```markdown
# ch08: 上下文管理 Spec

## 1. 背景

LLM 上下文窗口有上限，但长任务里 tool result（Bash 输出、长文件）很容易在几轮内把窗口顶爆。没有上下文管理就意味着 Agent 跑到一半被 API 退回 `prompt_too_long`，会话失败、上下文丢失、用户得手动重启。

本章用「先廉价救火再花钱总结」两层策略解决：第 1 层不调 LLM，只做本地写盘 + 决策记录；第 2 层在 `last_input_tokens` 过阈值时整段摘要。第 1 层加一个跨轮持久的「替换决策日志」 `ContentReplacementState`，让每个 tool result 的「替换/不替换」决定只做一次、之后字节相同地复读 —— 这是 Anthropic prompt cache 命中所需的前缀稳定性的关键。

## 2. 目标

交付一套两层上下文管理 + 压缩后恢复：

- **Layer 1**：`apply_tool_result_budget(conversation, session_dir, state)` 每轮 agent loop 都跑。读取 `ContentReplacementState` 已记录的决策，对新候选评估「单条超限」和「聚合超限」两条规则；选中的 tool result 写盘换 `<persisted-output>` preview 字符串，决定写入 state；过 `KEEP_RECENT_TURNS` 轮的陈旧 tool result 裁为 `<snipped>` 一段。返回**新的** `ConversationManager`，原 conversation 不动。新决策追加写到 `<session_dir>/replacement_records.jsonl`。
- **Layer 2**：`auto_compact(conversation, client, context_window, session_dir, ...)` 在 `conversation.last_input_tokens >= threshold` 时调 LLM 拼摘要，把整段会话换成 `[摘要]` + 边界消息两条。`CompactCircuitBreaker` 连续失败 `max_failures` 次后熔断不再发请求。
- **Layer 2 后恢复**：`RecoveryState` 跨轮记录每次 ReadFile 的字节快照与每次 Skill 调用的 SOP 文本。`auto_compact` 在拼出摘要、构造新会话之前先调 `build_recovery_attachment(state, tool_schemas)`，把「最近读过的文件 / 已激活的技能 / 当前可用工具 / 收尾提示」四段拼到摘要 user 消息末尾，避免摘要替换后模型瞬间失去工作记忆。

两层在 Agent 主循环里串联：Layer 2 先跑（决定是否整段摘要 + 恢复，需要时**就地** mutate `conversation.history`）→ 写入各种 system reminder → Layer 1 在 `client.stream` 调用前最后一刻跑、把 `api_conv` 喂给 LLM。手动入口 `manual_compact` 给 `/compact` 用，切到 `MANUAL_COMPACT_SAFETY_MARGIN` 更小的安全余量直接走 Layer 2。

Anthropic 客户端在 system / tools 末项 / 最后一条 user message 末尾三处加 `cache_control: {"type": "ephemeral"}` 标记；配合 Layer 1 的字节稳定 replacements，前缀缓存就能命中。

## 3. 功能需求

### 3.1 状态容器与持久化

- F1: `ContentReplacementState` dataclass（`seen_ids: set[str]` + `replacements: dict[str, str]`），以及 `create_replacement_state()` / `clone_replacement_state(src)` 两个工厂。`seen_ids` 收录每个判断过的 `tool_use_id`，`replacements` 仅收录决定「替换」的那些 id 到 preview 字符串。不变量：`replacements.keys() ⊆ seen_ids`。
- F2: `ContentReplacementRecord` dataclass（`tool_use_id`, `replacement`, `kind="tool-result"`），及 JSONL I/O：
  - `append_replacement_records(session_dir, records)`：空切片直接 return；用 `open("a", encoding="utf-8")` 追加，每行一个 JSON 对象。
  - `load_replacement_records(session_dir)`：缺文件返回空列表；逐行 `json.loads`。
- F3: `reconstruct_replacement_state(messages, records, inherited_replacements=None)`：seed `seen_ids` = `{ tr.tool_use_id | for tr in m.tool_results, for m in messages }`；按 `r.kind == "tool-result"` 过滤 records 并命中 candidate 才写入 `replacements`；可选 `inherited_replacements` 做 gap-fill。

### 3.2 Layer 1 应用流程

- F4: `apply_tool_result_budget(conversation, session_dir, state)` 返回 `tuple[ConversationManager, list[ContentReplacementRecord]]`，**不修改入参 conversation**。对每条 tool result 按 4 步处理：
  1. id ∈ `state.replacements` → 取出该字符串原样贴入 api_conv（纯查表，无 I/O）。
  2. id ∈ `state.seen_ids`（但不在 replacements）→ 保留原文。
  3. 外部已带 `PERSISTED_TAG` 前缀 → 视为已知决策，写入 state 与 records，作为字面字符串保留。
  4. 其余进 fresh，跑 Pass 1：单条 content 超 `SINGLE_RESULT_CHAR_LIMIT` → `persist_tool_result` + `make_persisted_preview` → 写入 state；剩余 fresh 跑 Pass 2：消息聚合 > `AGGREGATE_CHAR_LIMIT` 时按 content 长度降序挑直到聚合压回上限；未挑中的 fresh 标 seen 冻结为「不替换」。
- F5: spill 文件 `persist_tool_result` 用 `os.open(O_WRONLY | O_CREAT | O_EXCL)` 写到 `<work_dir>/.codebot/session/tool-results/<tool_use_id>.txt`，`FileExistsError` 静默吞掉（幂等）。
- F6: preview 格式 `make_persisted_preview` 输出 `<persisted-output>\n输出太大（XKB），完整内容已保存到：\n<file_path>\n\n预览（前 2KB）：\n<content[:PREVIEW_CHARS]>\n</persisted-output>`。这个字符串一旦写入 `state.replacements`，后续每轮逐字节复读，不能改格式。
- F7: 通过 `PERSISTED_TAG = "<persisted-output>"` 与 `SNIPPED_TAG = "<snipped>"` 前缀识别已 persist / snipped 内容，避免重复处理。
- F8: Pass 3 陈旧裁剪 `_snip_stale_messages`：在 Pass 1/2 输出的 new history 上跑（不动原 conversation）；超过 `KEEP_RECENT_TURNS` 轮的消息里，超过 `OLD_RESULT_SNIP_CHARS` 字符且未被 PERSISTED/SNIPPED 前缀标记的 tool result 整体替换为 `<snipped>\n(旧结果已裁剪，原始长度 N 字符)\n<前 200 字符>\n… (snipped)`。

### 3.3 Layer 2 摘要

- F9: 阈值计算 `compute_compact_threshold(context_window, manual=False)`，公式 `window - SUMMARY_OUTPUT_RESERVE - (MANUAL_COMPACT_SAFETY_MARGIN if manual else AUTO_COMPACT_SAFETY_MARGIN)`；`should_auto_compact(last_input_tokens, context_window)` 给布尔。
- F10: `auto_compact` 流程：把当前 conversation 全量塞进临时 `ConversationManager` + `SUMMARY_PROMPT` 包装 → 通过 `client.stream(...)` 收 `TextDelta` 拼成完整文本 → `extract_summary` 剥 `<analysis>`、保留 `<summary>` → `build_compact_messages` 构造 `[摘要] + 边界消息` 替换原会话 → `cleanup_tool_results` 清空 session 目录。
- F11: 摘要后处理 `extract_summary` 容错：找到 `<summary>`/`</summary>` 标签对取内部 trim；找不到完整标签对则返回原文整体，绝不丢摘要。
- F12: 摘要 prompt `SUMMARY_PROMPT` 强制九节结构（主要请求、关键概念、文件与代码段、错误与修复、解决过程、用户原话、待办、当前工作、下一步），并明确禁止工具调用、要求先 `<analysis>` 再 `<summary>`。
- F13: 熔断器 `CompactCircuitBreaker(max_failures=3)` 含 `record_failure / record_success / is_open` 三方法；自动模式下 `is_open()` 时 `auto_compact` 直接回错误字符串不发摘要请求。
- F14: PTL 重试：摘要请求自身报 `prompt too long` 时，`_group_messages_by_turn` 把对话按轮分组、丢掉最旧 1/5，最多重试 3 次；耗尽后 `breaker.record_failure()` 并返回错误字符串。
- F15: 手动入口 `manual_compact`：直接调 `auto_compact(..., manual=True)`，跳过 Layer 1 调用，安全余量切到 `MANUAL_COMPACT_SAFETY_MARGIN = 3_000`，对话不为空就压。

### 3.4 Anthropic 缓存断点与集成

- F16: `client.py` 在请求构造期间打三处 `cache_control: ephemeral`：
  - `system` 参数包装成 `[{"type":"text","text":system,"cache_control":{"type":"ephemeral"}}]`。
  - `tools` 末项的 schema dict 加 `"cache_control":{"type":"ephemeral"}`（用 `_mark_last_tool_for_cache` 浅拷贝避免污染调用方的工具表）。
  - 最后一条 user message 的末块用 `_mark_last_user_tail_for_cache` 原地打 marker（string content 自动 up-convert 为 block 列表）。
- F17: `Agent.__init__` 把 `self.replacement_state = create_replacement_state()` 初始化为空容器；三处 `apply_tool_result_budget` 调用点（main loop / manual_compact / 另一主循环变体）都传 `self.replacement_state` 并把 new records 写入 transcript。
- F18: Fork 子 Agent 路径 `codebot/tools/agent_tool.py` 创建 sub_agent 后判断 `p.subagent_type is None`（即真 fork）时 `sub_agent.replacement_state = clone_replacement_state(self._parent_agent.replacement_state)`。

### 3.5 压缩后恢复

- F19: `RecoveryState` 类含 `_files: dict[str, FileReadRecord]` 与 `_skills: dict[str, SkillInvocationRecord]`，用 `threading.Lock` 守护；`record_file_read(path, content)` / `record_skill_invocation(name, body)` 加锁写入并以 `time.time()` 打时间戳，空路径 / 空名字直接 return；`snapshot_files(limit)` / `snapshot_skills()` 复制后按时间戳降序，文件再切到 limit。`Agent.__init__` 把 `self.recovery_state = RecoveryState()` 默认初始化。
- F20: 限额常量 `RECOVERY_FILE_LIMIT = 5` / `RECOVERY_TOKENS_PER_FILE = 5_000` / `RECOVERY_SKILLS_BUDGET = 25_000` / `RECOVERY_TOKENS_PER_SKILL = 5_000` / `_RECOVERY_CHARS_PER_TOKEN = 3.5` 在 `codebot/context/manager.py` 顶部段定义。`_approx_tokens` 按 3.5 chars/token 折算；`_truncate_by_tokens` 按预算硬切并追加 `\n… (内容已截断)` 标记。
- F21: `build_recovery_attachment(state, tool_schemas)` 渲染四段（顺序：`## 最近读过的文件` → `## 已激活的技能` → `## 可用工具` → `## 提示`）；任一段为空就跳过；全空返回 `""`；技能累计字节超过 `RECOVERY_SKILLS_BUDGET` 时停止追加。`build_compact_messages(summary, attachment="")` 把恢复块用 `\n\n---\n\n` 拼到 `[摘要]` user 消息之后再返回 `[user, assistant]`。
- F22: `auto_compact` 多两个 kwargs `recovery: RecoveryState | None = None`、`tool_schemas: list[Mapping[str, Any]] | None = None`，生成 summary 后调 `build_recovery_attachment` 拿到 attachment，再调 `build_compact_messages(summary, attachment=attachment)`。三处调用点（main loop / `run_to_completion` / `manual_compact`）都传 `recovery=self.recovery_state` 与 `tool_schemas=self.registry.get_all_schemas(self.protocol)`。
- F23: 工具快照：`Agent._snapshot_for_recovery(tc, result)` 在 `_execute_single_tool_direct` 与 `_execute_tool` 两条工具执行路径末尾调用，仅在 `not result.is_error and tc.tool_name == "ReadFile"` 时打开 `file_path` 读取整文件（`encoding="utf-8", errors="replace"`）并写入 `self.recovery_state`；`OSError` 静默吞掉。
- F24: 技能快照：`SkillExecutor.execute_inline / execute_fork` 在 `self.agent.activate_skill` / 创建 fork_conv 之前判断 `getattr(self.agent, "recovery_state", None) is not None` 后调 `self.agent.recovery_state.record_skill_invocation(name, body)`；inline 记录渲染后的 prompt，fork 记录原始 `skill.prompt_body`。

## 4. 非功能需求

- N1: Layer 1 必须廉价：纯本地文件 I/O + 字符串改写，不调 LLM；每轮 agent loop 都跑也不能成为瓶颈。
- N2: `apply_tool_result_budget` 不能 mutate 入参 `conversation` —— 通过新建 `Message` / `ToolResultBlock` 实例 + 重组 `new_history` 产出 api_conv。测试用 `test_apply_does_not_mutate_conv` 守住。
- N3: 已决策 id 的复读必须**字节一致**：从 `state.replacements` 拿出来的字符串直接赋给 `decisions[id]`，不重新读盘、不重新格式化。这是 prompt cache 命中的硬约束。
- N4: spill 写盘幂等：用 `O_CREAT | O_EXCL`，同 `tool_use_id` 重复运行写同一份内容，已存在则 `FileExistsError` 静默跳过；spill 文件路径稳定（`<work_dir>/.codebot/session/tool-results/<tool_use_id>.txt`），不含时间戳。
- N5: Layer 2 期间不能再触发新的 tool call —— 摘要走的是临时 `ConversationManager` + `SUMMARY_PROMPT` 一次性 stream，不绕回 agent 主循环。
- N6: `auto_compact` 替换 conversation 用 `conversation.replace_history(...)` 就地写法，让调用方持有的 `ConversationManager` 引用保持有效。
- N7: 当 `breaker is None`（测试或一次性脚本场景）熔断器禁用，不能崩。
- N8: 阈值用固定常量（`SUMMARY_OUTPUT_RESERVE = 20_000` / `AUTO_COMPACT_SAFETY_MARGIN = 13_000`）而不是百分比，确保 200K / 1M 等不同窗口下 buffer 大小一致。
- N9: 子 Agent fork 的 state 必须是父 state 的**独立深拷贝**：子端 mutate 不影响父端，反向亦然。`set(src)` 和 `dict(src)` 浅拷贝足够（值是字符串和 hash key，不需要 deepcopy）。测试用 `test_clone_independent` 守住。
- N10: `RecoveryState` 必须并发安全：`StreamingExecutor` 用 `asyncio.gather` 并发跑 ReadFile，多个回写可能交错。结构体内 `threading.Lock` 保护两张 map；`record_*` 方法在空路径 / 空名字上直接 return，方便测试与一次性脚本调用。
- N11: 恢复块限额是**硬上限**：5 个文件、单文件 5K token、技能预算 25K token、单技能 5K token。超出预算时静默丢弃（不抛错），保证压缩输出体积可预测——压缩后摘要 + 恢复总长稳定在约 60K token 以内，远低于 `compute_compact_threshold` 阈值。

## 5. 设计概要

- 核心模块结构 (`codebot/context/manager.py`):
  - 常量段（顶部）：阈值、tag、session 子目录。
  - 状态段：`ContentReplacementState` / `ContentReplacementRecord` / `create_replacement_state` / `clone_replacement_state` / `reconstruct_replacement_state` / `append_replacement_records` / `load_replacement_records` / `REPLACEMENT_RECORDS_FILENAME`。
  - Session 段：`ensure_session_dir` / `cleanup_tool_results`。
  - Layer 1 段：`persist_tool_result` / `make_persisted_preview` / `_count_turns` / `_copy_message_with_results` / `_snip_stale_messages` / `apply_tool_result_budget`。
  - Layer 2 段：`compute_compact_threshold` / `should_auto_compact` / `SUMMARY_PROMPT` / `extract_summary` / `COMPACT_BOUNDARY_MESSAGE` / `build_compact_messages` / `_group_messages_by_turn` / `CompactCircuitBreaker` / `auto_compact`。
  - 恢复段：`RECOVERY_FILE_LIMIT / RECOVERY_TOKENS_PER_FILE / RECOVERY_SKILLS_BUDGET / RECOVERY_TOKENS_PER_SKILL / _RECOVERY_CHARS_PER_TOKEN` 常量 / `FileReadRecord` / `SkillInvocationRecord` dataclass / `RecoveryState` 类 + `record_file_read` / `record_skill_invocation` / `snapshot_files` / `snapshot_skills` / `_approx_tokens` / `_truncate_by_tokens` / `_first_line` / `build_recovery_attachment`。`codebot/context/__init__.py` re-export 类名 + 工厂 + builder。
- 主流程（每轮 agent loop）:
  - 主循环开头：`compact_result = await auto_compact(conversation, client, context_window, session_dir, ..., recovery=self.recovery_state, tool_schemas=self.registry.get_all_schemas(self.protocol))` 内部按阈值决定是否真做摘要；成功时 yield `CompactNotification` + 重新 `inject_environment` / `inject_long_term_memory`。
  - 各种 system reminder 写入 conversation。
  - 在 `client.stream` 调用前一刻：`api_conv, new_records = apply_tool_result_budget(conversation, self.session_dir, self.replacement_state)` → `append_replacement_records(self.session_dir, new_records)` → `client.stream(api_conv, ...)`。
- 主流程（工具调用快照）:
  - `Agent._execute_single_tool_direct` / `Agent._execute_tool` 在 `tool.execute(params)` 之后调 `self._snapshot_for_recovery(tc, result)`；命中 ReadFile + 非错误时按原路径打开文件读字节写入 `self.recovery_state`。
- 主流程（Skill 调用快照）:
  - 用户输入 `/<skill-name>` → 命令分发到 `SkillExecutor.execute_inline` 或 `execute_fork` → 在改 `self.agent.activate_skill` / 起 fork_conv 之前先 `self.agent.recovery_state.record_skill_invocation(...)`。
- 主流程（手动 `/compact`）:
  - 用户输入 `/compact` → `COMPACT_COMMAND.handler = handle_compact` → 读 `ctx.ui.get_token_count()`，<5000 直接提示无需压缩；否则调 `ctx.agent.manual_compact(ctx.conversation)`。
  - `Agent.manual_compact` 直接调 `auto_compact(..., manual=True)`，拿到 `CompactEvent` 包成 `CompactNotification`，否则返回 `ErrorEvent`。
- 主流程（fork 子 Agent）:
  - `AgentTool.execute` 触发 fork → 创建 sub_agent → 当 `p.subagent_type is None` 时 `sub_agent.replacement_state = clone_replacement_state(self._parent_agent.replacement_state)` → 子 Agent 用克隆状态独立演化。
- Anthropic 客户端缓存断点（`codebot/client.py`）:
  - `_mark_last_user_tail_for_cache(messages)` 给最后一条 user message 末块打 marker（string content 自动 up-convert 成 block 列表）。
  - `_mark_last_tool_for_cache(tools)` 浅拷贝 tools，给末项加 marker。
  - system 参数包装成 block 列表带 marker。
- 与其他模块的交互:
  - 依赖 `codebot.conversation`（`ConversationManager / Message / ToolResultBlock / ToolUseBlock` 与 `inject_environment / inject_long_term_memory / replace_history / serialize`）。
  - 依赖 `codebot.tools.base`（`TextDelta / StreamEnd / StreamEvent` 收摘要 stream 事件）。
  - 被 `codebot.agent.Agent`（主循环 + `manual_compact` + 另一主循环变体）、`codebot.commands.handlers.compact`（`/compact` 命令）、`codebot.tools.agent_tool`（fork clone）调用。

## 6. Out of Scope

- 跨进程 / 跨会话的压缩缓存。
- Micro-compact 与分段压缩：本仓库一次直接全量摘要，不做 partial / per-segment 压缩。
- 持久化的 `RecoveryState`：进程退出后状态丢失，不做磁盘落盘。下一次启动靠用户自然触发 ReadFile / Skill 调用重新填充。
- Session memory compaction：与 ch09 记忆系统配合，本章不做。
- 用真实 tokenizer 替代「LLM 返回的 `last_input_tokens`」作为阈值输入。
- 反应式 ContextTooLong 拦截重试：Python 版未实现，预防 + 手动两条路径已覆盖主要场景。
- 完整 resume 流程：transcript records 已落盘且 `reconstruct_replacement_state` 可用，但 resume 主流程不在本章范围。
- Pass 3 陈旧裁剪的边界穿越漂移：从「未裁剪」到「裁剪」那一轮前缀变了，会导致一次 cache miss；接受为已知 trade-off。
- 配置化阈值：所有阈值是模块常量，调整需改源码。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch08: 上下文管理 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。每条任务记录实际落地的文件与行号。

## T1: 常量、tag 与 session 助手

- 影响文件: `codebot/context/manager.py:14-30, 132-145`
- 依赖任务: 无
- 完成标准: `SINGLE_RESULT_CHAR_LIMIT / AGGREGATE_CHAR_LIMIT / PREVIEW_CHARS / KEEP_RECENT_TURNS / OLD_RESULT_SNIP_CHARS / SNIPPED_TAG / SUMMARY_OUTPUT_RESERVE / AUTO_COMPACT_SAFETY_MARGIN / MANUAL_COMPACT_SAFETY_MARGIN / PERSISTED_TAG / SESSION_SUBDIR` 全部定义；`ensure_session_dir(work_dir)` / `cleanup_tool_results(session_dir)` 实现。

## T2: `CompactEvent` / `ContentReplacementState` / `ContentReplacementRecord` dataclass

- 影响文件: `codebot/context/manager.py:37-58`
- 依赖任务: T1
- 完成标准: `CompactEvent(before_tokens)` 在 `manager.py:37-38` 定义。`ContentReplacementState` 含 `seen_ids: set[str]` + `replacements: dict[str, str]` 两个 field（都用 `default_factory`），`manager.py:46-49` 定义。`ContentReplacementRecord` 含 `tool_use_id` / `replacement` / `kind="tool-result"`，`manager.py:52-56` 定义。

## T3: `create_replacement_state` / `clone_replacement_state`

- 影响文件: `codebot/context/manager.py:59-68`
- 依赖任务: T2
- 完成标准: `create_replacement_state()` 返回空容器；`clone_replacement_state(src)` 用 `set(src.seen_ids)` 与 `dict(src.replacements)` 浅拷贝，源与拷贝彼此独立；`test_clone_independent` 通过。

## T4: Transcript JSONL `append_replacement_records` / `load_replacement_records`

- 影响文件: `codebot/context/manager.py:70-104`
- 依赖任务: T2
- 完成标准: `REPLACEMENT_RECORDS_FILENAME = "replacement_records.jsonl"` 在 `manager.py:70` 定义。`append_replacement_records(session_dir, records)`：空切片直接 return；用 `open("a", encoding="utf-8")` 追加，每行一个 `{"kind": ..., "tool_use_id": ..., "replacement": ...}` 对象；`load_replacement_records(session_dir)`：缺文件返回空列表；逐行 `json.loads`。`test_append_and_load_records_roundtrip` 通过。

## T5: `reconstruct_replacement_state`

- 影响文件: `codebot/context/manager.py:107-127`
- 依赖任务: T2, T4
- 完成标准: 先 seed `seen_ids` = `{ tr.tool_use_id | for tr in m.tool_results, for m in messages }`；按 `r.kind == "tool-result"` 过滤 records 并命中 candidate 才写入 `replacements`；可选 `inherited_replacements` 在 candidate ∩ 未被 records 覆盖时补全；`test_reconstruct_from_records / test_reconstruct_with_inherited_parent` 通过。

## T6: `persist_tool_result` / `make_persisted_preview`

- 影响文件: `codebot/context/manager.py:148-170`
- 依赖任务: T1
- 完成标准: `persist_tool_result` 用 `os.open(O_WRONLY | O_CREAT | O_EXCL)` 写到 `<session_dir>/<tool_use_id>.txt`，`FileExistsError` 静默吞掉（幂等）。`make_persisted_preview` 输出 `<persisted-output>\n输出太大（XKB），完整内容已保存到：\n<file_path>\n\n预览（前 2KB）：\n<content[:PREVIEW_CHARS]>\n</persisted-output>`。`TestPersistToolResult` / `TestMakePersistedPreview` 通过。

## T7: 辅助 `_count_turns` / `_copy_message_with_results` / `_snip_stale_messages`

- 影响文件: `codebot/context/manager.py:173-238`
- 依赖任务: T1, T6
- 完成标准:
  - `_count_turns(messages)` 数 `assistant && not tool_uses` 当作一轮。
  - `_copy_message_with_results(msg, new_tool_results)` 产出新 `Message` 实例，共享 `tool_uses` / `thinking_blocks` 引用（不可变结构）。
  - `_snip_stale_messages(history)` 在 new history 上跑（stateless），总轮数 ≤ `KEEP_RECENT_TURNS` 直接 return；超 boundary 的消息里超 `OLD_RESULT_SNIP_CHARS` 字符且未 PERSISTED/SNIPPED 前缀的 tool result 整体替换为 `<snipped>` 头 + 200 字符预览 + `… (snipped)` 尾。

## T8: Layer 1 `apply_tool_result_budget` Design B 主流程

- 影响文件: `codebot/context/manager.py:241-348`
- 依赖任务: T2, T6, T7
- 完成标准: 签名 `apply_tool_result_budget(conversation, session_dir, state) -> tuple[ConversationManager, list[ContentReplacementRecord]]`，**不修改入参 conversation**。算法：
  1. 阶段 1: 对每个 tr 分四类——`state.replacements` 命中 → 复读；`state.seen_ids` 命中 → 冻结原文；外部已带 `PERSISTED_TAG` 前缀 → 视为已知决策，写入 state 与 records；其余进 fresh。
  2. 阶段 2 (Pass 1): fresh 中 content 长度 > `SINGLE_RESULT_CHAR_LIMIT` 调 `persist_tool_result` + `make_persisted_preview`，写入 state 与 records。
  3. 阶段 3 (Pass 2): 计算 `total = Σdecisions.values + Σremaining.content`；> `AGGREGATE_CHAR_LIMIT` 时按 content 长度降序挑直到压回上限。
  4. 阶段 4: 未决策的 fresh 全部加进 `state.seen_ids`、`decisions[id] = tr.content`。
  5. 末段: 用 `decisions` 构造新 `[ToolResultBlock]` 保持原顺序 → `_copy_message_with_results` → `_snip_stale_messages` 跑 Pass 3 → 构造新 `ConversationManager` 并复制 `env_injected / ltm_injected / last_input_tokens` flags。
- `test_apply_does_not_mutate_conv / test_first_call_freezes_unreplaced / test_replacement_byte_identical / test_frozen_never_replaced / test_aggregate_only_picks_fresh` 通过。

## T9: 阈值计算 `compute_compact_threshold` / `should_auto_compact`

- 影响文件: `codebot/context/manager.py:350-358`
- 依赖任务: T1
- 完成标准: `compute_compact_threshold(200_000) == 167_000`、`compute_compact_threshold(200_000, manual=True) == 177_000`、`compute_compact_threshold(128_000) == 95_000`；`should_auto_compact(last_input_tokens, context_window)` 边界精确。`TestComputeCompactThreshold / TestShouldAutoCompact` 通过。

## T10: 摘要 prompt + helpers (`SUMMARY_PROMPT` / `extract_summary` / `COMPACT_BOUNDARY_MESSAGE` / `build_compact_messages` / `_group_messages_by_turn`)

- 影响文件: `codebot/context/manager.py:360-419`
- 依赖任务: T1
- 完成标准: `SUMMARY_PROMPT` 含九节结构 + 两次禁止工具调用 + 先 `<analysis>` 再 `<summary>` 的要求；`extract_summary` 找到 `<summary>...</summary>` 整对时返回内部 trim，找不到时返回原文整体；`build_compact_messages(summary)` 输出 `[user '[摘要]\n...', assistant COMPACT_BOUNDARY_MESSAGE]` 两条；`_group_messages_by_turn` 按 `assistant && not tool_uses` 切轮。`TestExtractSummary / TestBuildCompactMessages` 通过。

## T11: 熔断器 `CompactCircuitBreaker`

- 影响文件: `codebot/context/manager.py:421-436`
- 依赖任务: T1
- 完成标准: `@dataclass` 含 `max_failures: int = 3` 默认值与 `consecutive_failures: int = field(init=False, default=0)`；`record_failure / record_success / is_open` 三方法行为正确；`TestCompactCircuitBreaker` 通过。

## T12: Layer 2 `auto_compact`

- 影响文件: `codebot/context/manager.py:439-end`
- 依赖任务: T9, T10, T11
- 完成标准: 自动模式 `conversation.last_input_tokens < threshold` 返回 `None`；`breaker.is_open()` 返回错误字符串；构造临时 `ConversationManager`（header SUMMARY_PROMPT + 原 history + 结尾再次提醒不要调工具）通过 `client.stream(summary_conv, system=SUMMARY_PROMPT)` 收 `TextDelta` 拼成文本；PTL 重试用 `_group_messages_by_turn` 丢最旧 1/5，最多 3 次；成功调 `conversation.replace_history(build_compact_messages(summary))` + `cleanup_tool_results(session_dir)` + `breaker.record_success()`，返回 `CompactEvent(before_tokens)`；失败 `breaker.record_failure()` 返回错误字符串。

## T13: Anthropic 客户端缓存断点

- 影响文件: `codebot/client.py:24-68, 138-160`
- 依赖任务: 无
- 完成标准:
  - `_EPHEMERAL = {"type": "ephemeral"}` 常量定义。
  - `_mark_last_user_tail_for_cache(messages)` 倒序找最后一条 user message，对其末块（string content 自动 up-convert 为 block 列表）打 marker。
  - `_mark_last_tool_for_cache(tools)` 返回浅拷贝并给末项加 marker（不污染调用方持有的工具表）。
  - Anthropic `stream` 内：`messages` 构造后调 `_mark_last_user_tail_for_cache(messages)`；`system` 包装成 `[{"type":"text","text":system,"cache_control":_EPHEMERAL}]`；`tools` 经 `_mark_last_tool_for_cache` 处理后赋给 `kwargs["tools"]`。

## T14: Agent 集成

- 影响文件: `codebot/agent.py:15-27, 314-316, 436-516, 887-918, 960-1003`
- 依赖任务: T8, T12, T13
- 完成标准:
  - import 段加 `ContentReplacementRecord / ContentReplacementState / append_replacement_records / create_replacement_state / load_replacement_records / reconstruct_replacement_state`。
  - `Agent.__init__` 加 `self.replacement_state: ContentReplacementState = create_replacement_state()`（line 316）。
  - 主循环（line 436 附近）：先 `await auto_compact(...)` 处理事件；中间写各种 reminder；在 `client.stream` 调用前一刻：`api_conv, _new_records = apply_tool_result_budget(conversation, self.session_dir, self.replacement_state)` → 非空 `append_replacement_records(self.session_dir, _new_records)` → `self.client.stream(api_conv, ...)`。
  - `manual_compact` 直接走 `auto_compact(..., manual=True)`，不再前置调 `apply_tool_result_budget`（compact 将整段替换 history，前置 apply 的产物会被丢弃）。
  - 另一主循环变体（line 960）：同样把 `apply_tool_result_budget` 移到 `client.stream` 前一刻。

## T15: Fork 状态继承

- 影响文件: `codebot/tools/agent_tool.py:192-203`
- 依赖任务: T3, T14
- 完成标准: 创建 sub_agent 后判断 `p.subagent_type is None`（即真 fork）时 `from codebot.context import clone_replacement_state` → `sub_agent.replacement_state = clone_replacement_state(self._parent_agent.replacement_state)`。

## T16: 测试

- 影响文件: `tests/test_context.py`、`tests/test_replacement_state.py`
- 依赖任务: T2–T12
- 完成标准:
  - `tests/test_context.py` 的 `TestApplyToolResultBudget` 4 个 case 更新为 Design B 签名（接 state、判 api_conv、断言 conv 原始内容未变）；其余 `TestPersistToolResult / TestMakePersistedPreview / TestComputeCompactThreshold / TestShouldAutoCompact / TestExtractSummary / TestCompactCircuitBreaker / TestBuildCompactMessages / TestSessionDir` 全部保留并通过。
  - `tests/test_replacement_state.py` 新增 10 个 state-specific case：`test_create_returns_empty / test_clone_independent / test_apply_does_not_mutate_conv / test_first_call_freezes_unreplaced / test_replacement_byte_identical / test_frozen_never_replaced / test_aggregate_only_picks_fresh / test_reconstruct_from_records / test_reconstruct_with_inherited_parent / test_append_and_load_records_roundtrip`。
  - `PYTHONPATH=. pytest tests/test_context.py tests/test_replacement_state.py -v` 全部通过。

## T17: 端到端验证

- 影响文件: 无（仅运行验证）
- 依赖任务: T14, T15, T16
- 完成标准:
  - `PYTHONPATH=. pytest tests/test_context.py tests/test_replacement_state.py -v` 全部通过（共 33 个用例）。
  - 制造一次 Bash 大输出（> 5000 字符），观察 `.codebot/session/tool-results/<tool_use_id>.txt` 文件落地；`.codebot/session/replacement_records.jsonl` 出现对应行；对话历史里相应 tool result 仍为原文（Design B 不 mutate 原 conv），api_conv 视图里是 preview。
  - 制造一次连续多轮长对话使 `last_input_tokens >= 167_000`（200K 窗口）→ 主循环自动触发 Layer 2，事件流出现 `CompactNotification(before_tokens=...)`，对话被替换为 `[摘要] + 边界消息` 两条。
  - 短会话下在 TUI 输入 `/compact`，看到 `当前 token 数 X，无需压缩`。

## T18: `RecoveryState` 与限额常量

- 影响文件: `codebot/context/manager.py:1-20, 410-510`
- 依赖任务: T1
- 完成标准:
  - 顶部 import 新增 `import threading`、`import time`。
  - 限额常量 `RECOVERY_FILE_LIMIT = 5` / `RECOVERY_TOKENS_PER_FILE = 5_000` / `RECOVERY_SKILLS_BUDGET = 25_000` / `RECOVERY_TOKENS_PER_SKILL = 5_000` / `_RECOVERY_CHARS_PER_TOKEN = 3.5` 在「Post-compact recovery state」段定义。
  - `@dataclass FileReadRecord(path, content, timestamp)` 与 `@dataclass SkillInvocationRecord(name, body, timestamp)` 定义。
  - `class RecoveryState` 用 `threading.Lock` 守护 `_files` / `_skills`；`record_file_read(path, content)` / `record_skill_invocation(name, body)` 空路径直接 return，加锁写入并以 `time.time()` 打时间戳。
  - `snapshot_files(limit) / snapshot_skills()` 复制后按 timestamp 倒序，文件再切到 limit。

## T19: `build_recovery_attachment` + `build_compact_messages` 扩展

- 影响文件: `codebot/context/manager.py:512-620`
- 依赖任务: T18
- 完成标准:
  - `_approx_tokens(s)` 按 `len / 3.5` 折算；`_truncate_by_tokens(s, budget)` 超额时按 byte 上限切并追加 `\n… (内容已截断)`；`_first_line(s)` 返回第一行非空文本。
  - `build_recovery_attachment(state, tool_schemas)` 按顺序输出 `## 最近读过的文件 / ## 已激活的技能 / ## 可用工具 / ## 提示`；空 state + 空 schemas 时返回 `""`；技能预算超 `RECOVERY_SKILLS_BUDGET` 时 break。
  - `build_compact_messages(summary, attachment="")` 把 `attachment` 用 `\n\n---\n\n` 拼到 `[摘要]\n{summary}` user 消息之后，返回 `[user, assistant(COMPACT_BOUNDARY_MESSAGE)]`。
  - `tests/test_recovery.py` 5 个测试通过：`test_recovery_attachment_empty_when_nothing_recorded / test_recovery_attachment_emits_all_sections / test_recovery_file_limit_and_order / test_recovery_truncates_per_file / test_recovery_skills_budget`。

## T20: `auto_compact` / Agent / Skill 集成

- 影响文件: `codebot/context/manager.py:622-660`、`codebot/context/__init__.py`、`codebot/agent.py:295-330, 460-475, 920-930, 1000-1010, 850-870`、`codebot/skills/executor.py:58-95`
- 依赖任务: T18, T19, T10
- 完成标准:
  - `codebot/context/__init__.py` re-export `RecoveryState / FileReadRecord / SkillInvocationRecord / build_recovery_attachment`。
  - `auto_compact` 多两个 kwargs `recovery: RecoveryState | None = None`、`tool_schemas: list[Mapping[str, Any]] | None = None`，在生成 summary 后调 `build_recovery_attachment` 拿 attachment 再传给 `build_compact_messages(summary, attachment=attachment)`。
  - `Agent.__init__` 新增 `self.recovery_state: RecoveryState = RecoveryState()`。
  - 三处 `auto_compact` 调用点（`Agent.run` 主循环 / `manual_compact` / `run_to_completion`）都传 `recovery=self.recovery_state` 与 `tool_schemas=self.registry.get_all_schemas(self.protocol)`。
  - 新增 `Agent._snapshot_for_recovery(tc, result)` 方法（位于 `_extract_memories` 之前），仅当 `not result.is_error and tc.tool_name == "ReadFile"` 时打开 `file_path` 读 utf-8（errors="replace"）并写入 `self.recovery_state`；`OSError` 静默吞掉。
  - `Agent._execute_single_tool_direct` 与 `Agent._execute_tool` 在 `tool.execute(params)` 之后各加一行 `self._snapshot_for_recovery(tc, result)`。
  - `SkillExecutor.execute_inline` 在 `self.agent.activate_skill(...)` 之后调 `self.agent.recovery_state.record_skill_invocation(skill.name, prompt)`；`execute_fork` 在 `prompt = substitute_arguments(...)` 后立刻调 `self.agent.recovery_state.record_skill_invocation(skill.name, skill.prompt_body)`，两处都用 `getattr(self.agent, "recovery_state", None) is not None` 保护。

## T21: 端到端验证（恢复部分）

- 影响文件: 无
- 依赖任务: T18, T19, T20
- 完成标准:
  - `PYTHONPATH=. pytest tests/test_recovery.py -v` 5 个测试通过。
  - 制造一次连续 ReadFile 6 个文件 + 触发 `/compact` 的会话，摘要消息出现 `## 最近读过的文件` 段并只列最近 5 个；任一 5K token 以上的文件出现 `… (内容已截断)` 标记。
  - 制造一次 `/<skill-name>` 激活技能后再 `/compact` 的会话，摘要消息出现 `## 已激活的技能` 段并包含 skill 名 + SOP 片段。
  - 摘要消息以 `## 提示` 段收尾，强调若需要原文请重新读文件而不是靠摘要猜。

## 进度

- T1-T21（含「压缩后恢复」相关 T18-T21）

```

````markdown
# ch08: 上下文管理 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性

### 1.1 常量与 session 助手

- [ ] `SINGLE_RESULT_CHAR_LIMIT = 5_000` 在 `codebot/context/manager.py:16` 定义。
- [ ] `AGGREGATE_CHAR_LIMIT = 20_000`、`PREVIEW_CHARS = 2_000`、`KEEP_RECENT_TURNS = 10`、`OLD_RESULT_SNIP_CHARS = 2_000`、`SNIPPED_TAG = "<snipped>"` 在 `manager.py:17-22` 定义。
- [ ] `SUMMARY_OUTPUT_RESERVE = 20_000`、`AUTO_COMPACT_SAFETY_MARGIN = 13_000`、`MANUAL_COMPACT_SAFETY_MARGIN = 3_000`、`PERSISTED_TAG = "<persisted-output>"`、`SESSION_SUBDIR = ".codebot/session/tool-results"` 在 `manager.py:24-30` 定义。
- [ ] `ensure_session_dir(work_dir) -> Path` 在 `manager.py:132` 实现：创建并返回 `Path("<work_dir>/.codebot/session/tool-results")`，`mkdir(parents=True, exist_ok=True)`。
- [ ] `cleanup_tool_results(session_dir)` 在 `manager.py:138` 实现：`shutil.rmtree` + 重建空目录。

### 1.2 状态容器与 transcript

- [ ] `@dataclass CompactEvent(before_tokens: int)` 在 `manager.py:37-38` 定义。
- [ ] `@dataclass ContentReplacementState`（`seen_ids: set[str]` + `replacements: dict[str, str]`，都用 `field(default_factory=...)`）在 `manager.py:46-49` 定义。
- [ ] `@dataclass ContentReplacementRecord(tool_use_id, replacement, kind="tool-result")` 在 `manager.py:52-56` 定义。
- [ ] `create_replacement_state()` 在 `manager.py:59-60` 返回空容器；`clone_replacement_state(src)` 在 `manager.py:63-67` 用 `set(src.seen_ids)` + `dict(src.replacements)` 浅拷贝。
- [ ] `REPLACEMENT_RECORDS_FILENAME = "replacement_records.jsonl"` 在 `manager.py:70` 定义。
- [ ] `append_replacement_records(session_dir, records)` 在 `manager.py:73-86` 实现：空切片直接 return；用 `open("a", encoding="utf-8")` 追加；每行一个 JSON 对象（含 `kind / tool_use_id / replacement` 三 key）。
- [ ] `load_replacement_records(session_dir)` 在 `manager.py:88-104` 实现：缺文件返回空列表；逐行 `json.loads`。
- [ ] `reconstruct_replacement_state(messages, records, inherited_replacements=None)` 在 `manager.py:107-127` 实现，包括 candidate-only 过滤与 inheritedReplacements gap-fill。

### 1.3 Layer 1 持久化与决策应用

- [ ] `persist_tool_result(tool_use_id, content, session_dir)` 在 `manager.py:148-156` 实现：`os.open(..., O_CREAT | O_EXCL)`，`FileExistsError` 静默跳过保证幂等。
- [ ] `make_persisted_preview(content, file_path)` 在 `manager.py:159-170` 实现：返回 `<persisted-output>\n输出太大（XKB），完整内容已保存到：\n<file_path>\n\n预览（前 2KB）：\n<前 2_000 字符>\n</persisted-output>`（这个字符串是 byte-stable 的 anchor，不能轻改）。
- [ ] `_count_turns(messages)` / `_copy_message_with_results(msg, new_tool_results)` / `_snip_stale_messages(history)` 在 `manager.py:173-238` 实现。
- [ ] `apply_tool_result_budget(conversation, session_dir, state) -> tuple[ConversationManager, list[ContentReplacementRecord]]` 在 `manager.py:241-348` 实现，**不修改入参 conversation**：
  - 阶段 1 四类分类（replacements 命中复读 / seen_ids 命中冻结原文 / PERSISTED_TAG 前缀冻结作为已知决策 / fresh）。
  - 阶段 2 Pass 1 单条 persist。
  - 阶段 3 Pass 2 聚合超限 + 按 size 降序选 fresh。
  - 阶段 4 剩余 fresh 冻结。
  - 末段 `_copy_message_with_results` + `_snip_stale_messages` + 新 `ConversationManager`。

### 1.4 Layer 2 摘要

- [ ] `compute_compact_threshold(context_window, manual=False)` 在 `manager.py:350-353` 实现，公式 `window - SUMMARY_OUTPUT_RESERVE - (3_000 if manual else 13_000)`。
- [ ] `should_auto_compact(last_input_tokens, context_window)` 在 `manager.py:356-358` 实现。
- [ ] `SUMMARY_PROMPT` 在 `manager.py:360-379` 定义，包含九节结构 + 两次禁止工具调用 + 先 `<analysis>` 再 `<summary>` 的指令。
- [ ] `extract_summary(llm_output)` 在 `manager.py:382-387` 实现：找 `<summary>` / `</summary>` 标签对取内部 trim，找不到则返回原文。
- [ ] `COMPACT_BOUNDARY_MESSAGE` 在 `manager.py:390-393` 定义；`build_compact_messages(summary)` 在 `manager.py:396-400` 实现。
- [ ] `_group_messages_by_turn(messages)` 在 `manager.py:403-413` 实现。
- [ ] `@dataclass CompactCircuitBreaker(max_failures=3)` 在 `manager.py:421-436` 实现，含 `record_failure / record_success / is_open` 三方法。
- [ ] `async auto_compact(conversation, client, context_window, session_dir, protocol="anthropic", manual=False, breaker=None)` 在 `manager.py:439-end` 实现，覆盖阈值判断、熔断、PTL 重试（最多 3 次，每次丢 1/5 最旧轮）、`extract_summary` + `replace_history` + `cleanup_tool_results` 全流程。
- [ ] 边界处理 `breaker is None` 时不调用 `record_failure / record_success`（多处显式 `if breaker is not None`）。
- [ ] 边界处理 `extract_summary` 中 `<summary>` 或 `</summary>` 缺失时返回原文，不抛错。

### 1.5 Anthropic 缓存断点

- [ ] `_EPHEMERAL = {"type": "ephemeral"}` 在 `codebot/client.py:24` 定义。
- [ ] `_mark_last_user_tail_for_cache(messages)` 在 `client.py:27-52` 实现：倒序找最后一条 user message，对其末块（string content 自动 up-convert 成 block 列表）打 marker。
- [ ] `_mark_last_tool_for_cache(tools)` 在 `client.py:55-68` 实现：返回浅拷贝并给末项加 marker。
- [ ] `AnthropicLLMClient.stream` 在请求构造期间打三处 cache marker（`client.py:138-160`）：`messages` 构造后调 `_mark_last_user_tail_for_cache(messages)`；`system` 包装成 `[{"type":"text","text":system,"cache_control":_EPHEMERAL}]`；`tools` 经 `_mark_last_tool_for_cache` 处理后赋给 `kwargs["tools"]`。

### 1.6 `RecoveryState` 与恢复块（同样在 `codebot/context/manager.py`）

- [ ] 顶部 import 新增 `threading` 与 `time`。
- [ ] 限额常量 `RECOVERY_FILE_LIMIT = 5` / `RECOVERY_TOKENS_PER_FILE = 5_000` / `RECOVERY_SKILLS_BUDGET = 25_000` / `RECOVERY_TOKENS_PER_SKILL = 5_000` / `_RECOVERY_CHARS_PER_TOKEN = 3.5` 在「Post-compact recovery state」段定义。
- [ ] `@dataclass FileReadRecord` / `@dataclass SkillInvocationRecord` 含 `timestamp: float` 字段。
- [ ] `class RecoveryState` 用 `threading.Lock` 守护 `_files` / `_skills` 两张 dict；`record_file_read(path, content)` / `record_skill_invocation(name, body)` 空路径直接 return，加锁写入并以 `time.time()` 打时间戳。
- [ ] `snapshot_files(limit)` / `snapshot_skills()` 复制后按 timestamp 倒序，文件再切到 limit。
- [ ] `_approx_tokens` / `_truncate_by_tokens` / `_first_line` 三个辅助；`_truncate_by_tokens` 超额时按 byte 上限切并追加 `\n… (内容已截断)`。
- [ ] `build_recovery_attachment(state, tool_schemas)` 依次输出 `## 最近读过的文件 / ## 已激活的技能 / ## 可用工具 / ## 提示`；空 state + 空 schemas 时返回 `""`；技能预算超 `RECOVERY_SKILLS_BUDGET` 时停止追加。
- [ ] `build_compact_messages(summary, attachment="")` 把 `attachment` 用 `\n\n---\n\n` 拼到 `[摘要]\n{summary}` user 消息之后。
- [ ] `auto_compact` 多两个 kwargs `recovery: RecoveryState | None = None`、`tool_schemas: list[Mapping[str, Any]] | None = None`，在 `extract_summary` 后调 `build_recovery_attachment` 再传给 `build_compact_messages`。
- [ ] `codebot/context/__init__.py` re-export `RecoveryState / FileReadRecord / SkillInvocationRecord / build_recovery_attachment`。

### 1.7 Agent / Skill 接入

- [ ] `Agent.__init__` 新增 `self.recovery_state: RecoveryState = RecoveryState()`。
- [ ] 三处 `auto_compact` 调用点（`run` 主循环 / `manual_compact` / `run_to_completion`）都传 `recovery=self.recovery_state` 与 `tool_schemas=self.registry.get_all_schemas(self.protocol)`。
- [ ] 新增 `Agent._snapshot_for_recovery(tc, result)` 方法：`not result.is_error and tc.tool_name == "ReadFile"` 时打开 `file_path` 读 utf-8（errors="replace"）并写入 `self.recovery_state`；`OSError` 静默吞掉。
- [ ] `Agent._execute_single_tool_direct` 与 `Agent._execute_tool` 在 `tool.execute(params)` 之后调 `self._snapshot_for_recovery(tc, result)`。
- [ ] `SkillExecutor.execute_inline` 在 `self.agent.activate_skill(...)` 之后调 `record_skill_invocation(skill.name, prompt)`；`execute_fork` 在 `prompt = substitute_arguments(...)` 后调 `record_skill_invocation(skill.name, skill.prompt_body)`，两处都用 `getattr(self.agent, "recovery_state", None) is not None` 保护。

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `grep -rn "from codebot.context" codebot --include="*.py" | grep -v "context/"` 至少 1 处导入：
  - `codebot/agent.py:15-27`（导入 `CompactCircuitBreaker / CompactEvent / ContentReplacementRecord / ContentReplacementState / append_replacement_records / apply_tool_result_budget / auto_compact / create_replacement_state / ensure_session_dir / load_replacement_records / reconstruct_replacement_state`）。
- [ ] `grep -rn "apply_tool_result_budget\|auto_compact\|manual_compact\|clone_replacement_state" codebot --include="*.py"` 命中：
  - `codebot/agent.py:316`（`Agent.__init__` 初始化 `replacement_state`）
  - `codebot/agent.py:510`（主循环调 `apply_tool_result_budget`）
  - `codebot/agent.py:998`（另一主循环变体调 `apply_tool_result_budget`）
  - `codebot/agent.py` 中 `manual_compact` 调 `auto_compact(..., manual=True)`
  - `codebot/commands/handlers/compact.py`（`/compact` 命令调 `ctx.agent.manual_compact`）
  - `codebot/tools/agent_tool.py:200-203`（fork 调 `clone_replacement_state` 注入子 Agent）
- [ ] `grep -rn "RecoveryState\b\|recovery_state" codebot --include="*.py"` 命中：
  - `codebot/context/manager.py` 定义。
  - `codebot/context/__init__.py` re-export。
  - `codebot/agent.py`：`Agent.__init__` 初始化 + 三处 `auto_compact` kwarg + `_snapshot_for_recovery` 方法 + 两处 `_execute_*` 调用。
  - `codebot/skills/executor.py`：`execute_inline` / `execute_fork` 各一处 `record_skill_invocation`。
- [ ] 调用入口位于 `Agent` 主循环（`agent.py:510` 在 `Agent.run` 的 `while iteration <= self.max_iterations` 循环内、`client.stream` 调用之前）。
- [ ] 命令注册中心已更新: `COMPACT_COMMAND` 在 `codebot/commands/handlers/__init__.py` 导出，由 registry 注册到 `/compact` + 别名 `/c`。
- [ ] 用户输入到本模块的路径可一句话描述:
  - 自动: agent 主循环新一轮 → `auto_compact` 阈值判断 → 写入 reminder → `apply_tool_result_budget` 产出 api_conv → `client.stream(api_conv, ...)`。
  - 手动: 用户在 TUI 输入 `/compact` → `handle_compact` → `Agent.manual_compact` → `auto_compact(..., manual=True)` → 回传 `CompactNotification | ErrorEvent`。
  - Fork: 父 Agent 调 Agent 工具触发 fork → `agent_tool.py` 创建 sub_agent → 注入父 state 的 `clone_replacement_state` → 子 Agent 用克隆状态独立演化。
- [ ] **死代码核查**：所有公开符号都在被引用：`ContentReplacementState / ContentReplacementRecord / create_replacement_state / clone_replacement_state / reconstruct_replacement_state / append_replacement_records / load_replacement_records / apply_tool_result_budget / auto_compact / compute_compact_threshold / should_auto_compact / extract_summary / build_compact_messages / make_persisted_preview / persist_tool_result / CompactCircuitBreaker / CompactEvent / ensure_session_dir / cleanup_tool_results` 全部在 `codebot/context/__init__.py` 导出且被外部模块或测试引用。

## 3. 编译与测试

- [ ] `ruff check codebot/context codebot/commands/handlers/compact.py codebot/client.py codebot/agent.py codebot/tools/agent_tool.py` 通过。
- [ ] `PYTHONPATH=. pytest tests/test_context.py tests/test_replacement_state.py tests/test_recovery.py -v` 全部通过：
  - `TestPersistToolResult / TestMakePersistedPreview / TestApplyToolResultBudget`（4 case，已更新为 Design B）/ `TestComputeCompactThreshold / TestShouldAutoCompact / TestExtractSummary / TestCompactCircuitBreaker / TestBuildCompactMessages / TestSessionDir`（已有）。
  - `test_create_returns_empty / test_clone_independent / test_apply_does_not_mutate_conv / test_first_call_freezes_unreplaced / test_replacement_byte_identical / test_frozen_never_replaced / test_aggregate_only_picks_fresh / test_reconstruct_from_records / test_reconstruct_with_inherited_parent / test_append_and_load_records_roundtrip`（10 个新增）。
  - `test_recovery_attachment_empty_when_nothing_recorded / test_recovery_attachment_emits_all_sections / test_recovery_file_limit_and_order / test_recovery_truncates_per_file / test_recovery_skills_budget`（5 个恢复测试）。
- [ ] `PYTHONPATH=. pytest tests/ -v` 整套未被本章引入新的失败用例（`test_plan_mode_denied_tool_returns_error` 已知预存 hang，可 `--deselect`）。

## 4. 端到端验证

- [ ] Layer 1 字节稳定性：制造一轮内并行调 5 个 Bash、每个吐 4.5K 字符的会话（总 22.5K，触发 Pass 2）；`apply_tool_result_budget` 返回的 `api_conv` 里其中一条 tool_result content 为 `<persisted-output>` 包裹的 preview；下一轮再调一次，同一 `tool_use_id` 的 content 与上一轮完全相等（state.replacements 复读）。
- [ ] Layer 1 不 mutate 原 conv：`test_apply_does_not_mutate_conv` 守住；调 `apply_tool_result_budget` 前后 `conversation.history` 各 `tool_result.content` 完全相等。
- [ ] Layer 1 frozen 不再替换：`test_frozen_never_replaced` 验证「第一轮未替换的 id 在后续轮即使聚合超限也不被选中」。
- [ ] Layer 2 触发：制造长对话使 `last_input_tokens >= 167_000`（200K 窗口）→ 主循环自动触发 Layer 2，事件流出现 `CompactNotification(before_tokens=...)`，对话被替换为 `[摘要] + 边界消息` 两条。
- [ ] Layer 2 熔断：人为让 `auto_compact` 连续失败 3 次后第 4 次直接返回错误字符串不发请求。
- [ ] Spill 落盘：长 Bash 输出后 `<work_dir>/.codebot/session/tool-results/` 目录下出现以 `<tool_use_id>.txt` 命名的文件。
- [ ] Transcript 落盘：`<work_dir>/.codebot/session/replacement_records.jsonl` 出现新条目，`jq .` 可解析。
- [ ] Fork 隔离：fork 出去的子 Agent 修改自己 state 的 seen_ids / replacements 不影响父 Agent。
- [ ] 短会话下在 TUI 输入 `/compact`，看到 `当前 token 数 X，无需压缩`（input_tokens < 5000 分支）。
- [ ] 长会话下在 TUI 输入 `/compact`，看到 `上下文已压缩（压缩前 X tokens）`（`CompactNotification` 渲染）。
- [ ] 恢复块文件段：先 ReadFile 两个不同路径再触发 `/compact`，摘要消息出现 `## 最近读过的文件` 段、两个 `### <绝对路径>` 子段，每段内容用 ``` 包住。
- [ ] 恢复块技能段：先 `/<skill-name>` 激活一个 skill 再 `/compact`，摘要消息出现 `## 已激活的技能` 段并包含 skill 名 + SOP 片段。
- [ ] 恢复块工具段：摘要消息出现 `## 可用工具` 段，并把当前 registry 里的工具按 `- 名字 — 描述首行` 列出。
- [ ] 恢复块收尾提示：摘要消息以 `## 提示` 段收尾。
- [ ] 限额硬上限：人造 6+ 个 ReadFile 后压缩，恢复块只列最近 5 个；任一 5K token 以上的文件出现 `… (内容已截断)` 标记。

## 5. 文档

- [ ] spec.md / tasks.md / checklist.md 三件套齐全且最新（位于 `docs/python/ch08/`）。
- [ ] 跨分支设计文档存在：`docs/extras/content-replacement-state.md` 描述 ContentReplacementState 三分支统一设计与 Design B（不 mutate）契约。
- [ ] commit 信息标注 `ch08` 与三件套关闭状态。

````

### Java

```markdown
# ch08: 上下文管理 Spec

## 1. 背景

LLM 上下文窗口有上限，但长任务里 tool result（Bash 输出、长文件）很容易在几轮内把窗口顶爆。没有上下文管理就意味着 Agent 跑到一半被 API 退回 `prompt_too_long`，会话失败、上下文丢失、用户得手动重启。

本章用「先廉价救火再花钱总结」分层策略解决：Layer 1 不调 LLM，把单条超大或单条消息聚合超大的 tool result 写盘换 preview 字符串，并维护跨轮的「替换决策日志」`ContentReplacementState`，让每个 tool result 的「替换/不替换」决定只做一次、之后字节相同地复读 —— 这是 Anthropic prompt cache 命中所需的前缀稳定性的关键；按 token 估算占比逐级升档的 Snip / Microcompact / Collapse / Auto-compact 四档由 `ContextCompactor.manage` 在 Agent 主循环每轮开头调用，越晚动用 LLM 越好。Auto-compact 之后再附一段「恢复块」（最近读过的文件 / 已激活的技能 / 当前可用工具 / 收尾提示），把摘要替换掉的工作记忆补回去。

## 2. 目标

交付两个独立又互补的包：

- **`com.codebot.toolresult`**（新包）：Layer 1 决策日志 + Design B 应用。
  - `ContentReplacementState` 含 `Set<String> seenIds` 与 `Map<String, String> replacements`，构造空容器并支持 `copy()` 独立深拷贝。
  - `ToolResultBudget.apply(conv, sessionDir, state)` 返回 `ApplyResult(apiConv, newRecords)`：**不 mutate 入参 conv**，构造新 `ConversationManager` 应用决策；对新候选评估「单条超限」与「聚合超限」两规则；选中的 tool result 写盘换 `[Result of N chars saved to PATH ...]` preview 字符串，决定写入 state；过 `KEEP_RECENT_TURNS` 轮的陈旧 tool result 裁为 `[Stale output snipped: N chars]` 一行。
  - `ReplacementRecordsIO.append/load` 把新决策落盘到 `<sessionDir>/replacement_records.jsonl`，方便后续 resume 复盘。
  - `ContentReplacementLifecycle.reconstruct(messages, records, inheritedReplacements)` 从 transcript 重建 state。

- **`com.codebot.compact`**（保留 + 升档逻辑 + 恢复）：4 层升档、摘要与压缩后恢复。
  - `ContextCompactor.manage(conv, client, contextWindow, workDir, tracking, recovery, toolSchemas)` 算 token 估算 → 按 ratio 升档：> 0.80 走 Auto-compact，> 0.70 走 Collapse，> 0.60 走 Microcompact，> 0.50 走 Snip。
  - `forceCompact(conv, client, contextWindow, recovery, toolSchemas)` 给 `/compact` 与反应式恢复用，无视阈值直接走 Auto-compact。
  - 旧的 `applyToolResultBudget`（仅单条 spill 且从未被 `manage` 调用过）由 `ToolResultBudget.apply` 接管。
  - `RecoveryState` 跨轮记录 ReadFile 字节快照与 Skill SOP；`buildRecoveryAttachment(state, toolSchemas)` 把「最近读过的文件 / 已激活的技能 / 当前可用工具 / 收尾提示」四段拼成纯文本块，`autoCompact` 在生成摘要之后用 `\n\n---\n\n` 拼到摘要 user 消息末尾。

两层在 Agent 主循环里串联：Layer 2 先跑（`ContextCompactor.manage` 按 ratio 决定动作，需要时 mutate `conv` 装新对话）→ 各种 system reminder 写入 conv → Layer 1 在 `client.stream` 调用前最后一刻跑、把 apiConv 喂给 LLM。

Anthropic 客户端在 system / tools 末项 / 最后一条 user message 末尾三处加 `cache_control: ephemeral` 标记；配合 Layer 1 的字节稳定 replacements，前缀缓存就能命中。

## 3. 功能需求

### 3.1 `com.codebot.toolresult` 状态容器与持久化

- F1: `ContentReplacementState`（`Set<String> seenIds` + `Map<String, String> replacements`，HashSet/HashMap 默认初始化），方法 `copy()` 返回独立深拷贝（用 `new HashSet<>(src.seenIds)` 与 `new HashMap<>(src.replacements)`）。不变量：`keys(replacements) ⊆ seenIds`。
- F2: `ContentReplacementRecord(String kind, String toolUseId, String replacement)` record + 静态工厂 `toolResult(toolUseId, replacement)`；`KIND_TOOL_RESULT = "tool-result"` 常量。
- F3: `ApplyResult(ConversationManager apiConv, List<ContentReplacementRecord> newRecords)` record，用作 `ToolResultBudget.apply` 返回值。
- F4: `ReplacementRecordsIO.append(sessionDir, records)`：空列表直接 return；自动 `Files.createDirectories(sessionDir)`；每行一个 JSON 对象（Jackson）；`kind` 为空或 null 自动填 `KIND_TOOL_RESULT`。
- F5: `ReplacementRecordsIO.load(sessionDir)`：缺文件返回空列表；`Files.readAllLines` 后逐行 Jackson `readValue`。
- F6: `ContentReplacementLifecycle.reconstruct(messages, records, inheritedReplacements)`：先 seed `seenIds` = 所有 message 里 `getToolResults` 的 `tool_use_id`；按 `kind == "tool-result"` 过滤 records 并命中 candidate 才写入 `replacements`；可选 `inheritedReplacements` (Map) 做 gap-fill（candidate ∩ 未被 records 覆盖）。

### 3.2 Layer 1 应用

- F7: `ToolResultBudget.apply(conv, sessionDir, state) -> ApplyResult`，**不修改入参 conv**。算法：
  1. 阶段 1: 对每条 tr 分四类——`state.replacements` 命中 → 复读；`state.seenIds` 命中 → 冻结原文；外部已带 `[Result of ` 或 `[Stale output snipped:` 前缀 → 视为已知决策，写入 state 与 records；其余进 fresh。
  2. 阶段 2 (Pass 1): fresh 中 content 长度 > `SINGLE_RESULT_LIMIT` 调 `spillAndPreview` 写盘 + 生成 preview，写入 state 与 records；spill 失败 freeze 原文。
  3. 阶段 3 (Pass 2): 计算 `total = Σdecisions.values.length + Σremaining.content.length`；> `MESSAGE_AGGREGATE_LIMIT` 时按 content 长度降序挑直到压回上限。
  4. 阶段 4: 未决策的 fresh 全部加进 `state.seenIds`、`decisions.put(id, tr.content())`。
  5. 末段: 用 `decisions` 构造新 `List<ToolResultBlock>` 保持原顺序 → `copyMessageWithResults` → `snipStale` → `buildManager`（通过 `addAssistantFull / addToolResultsMessage / addUserMessage / addAssistantMessage` 重放消息）。
- F8: 阈值常量 `SINGLE_RESULT_LIMIT = 15_000`、`MESSAGE_AGGREGATE_LIMIT = 20_000`、`OLD_RESULT_SNIP_CHARS = 2_000`、`KEEP_RECENT_TURNS = 10`、`SPILL_SUBDIR = "tool_results"` 在 `ToolResultBudget` 顶部 `public static final` 定义。
- F9: spill 文件 `spillAndPreview` 用 `Files.writeString(spillDir.resolve(toolUseId), content)` 写到 `<sessionDir>/tool_results/<toolUseId>`；同 size 文件已存在则不重写（幂等）。
- F10: preview 格式 `[Result of N chars saved to PATH — read with ReadFile if needed]` 是 byte-stable anchor，一旦写入 `state.replacements`，后续每轮逐字节复读。
- F11: Pass 3 陈旧裁剪 `snipStale`：在 Pass 1/2 输出的 new history 上跑（不动原 conversation）；超过 `KEEP_RECENT_TURNS` 轮的消息里，超过 `OLD_RESULT_SNIP_CHARS` 字符且未被 `[Result of `/`[Stale output snipped:` 前缀标记的 tool result 整体替换为 `[Stale output snipped: N chars]`。

### 3.3 `com.codebot.compact` Layer 2 升档

- F12: `ContextCompactor.manage(conv, client, contextWindow)` 按 ratio 升档：
  - ratio > `AUTOCOMPACT_THRESHOLD = 0.80` → `autoCompact`。
  - ratio > `COLLAPSE_THRESHOLD = 0.70` → `contextCollapse`。
  - ratio > `MICROCOMPACT_THRESHOLD = 0.60` → `microcompact`。
  - ratio > `SNIP_THRESHOLD = 0.50` → `snip`，命中时回填 `"Snipped verbose tool results"`。
  - 未命中返回空串。
- F13: `estimateTokens(messages)` 按 `length / 3.5` 估算 + 常数偏置，覆盖 content + tool args (Jackson 序列化) + tool_results + thinking blocks 四类。
- F14: `snip` 把 `recentBoundary = size - KEEP_RECENT_TURNS*3` 之前的超过 `SNIP_CHAR_LIMIT = 2000` 的 tool result 换为 `[Output snipped: %d chars, %d lines]` 一行。
- F15: `microcompact` 对老 tool result 超过 `MICROCOMPACT_LIMIT = 5000` 的内容调 `truncatePreservingBoundaries` 做头 5 行 + 尾 5 行 + 省略提示。
- F16: `contextCollapse` 按 `splitIdx = size - 30` 切分早期段和最近段；早期段 `serializeForSummary(oldMessages, 500)` + `requestSummary` 走 LLM 摘要；新建 `ConversationManager` 装 `[Earlier conversation summary]` 用户消息 + assistant 确认消息 + 最近 30 条原样追加；`replaceConversation` 就地替换。
- F17: `autoCompact` 全量 `serializeForSummary` + `requestSummary` 走 LLM；新建 `ConversationManager` 装 `[Compacted conversation summary]\n\n<summary>` + assistant 确认消息；`replaceConversation` 就地替换。
- F18: 摘要系统提示 `SUMMARY_SYSTEM_PROMPT` 是 Text Block，明确要求保留 file paths / decisions / current goal / pending work / error states / code snippets 六类信息。
- F19: `requestSummary` 用一次性 `client.stream(summaryConv, null)`（tools 传 null 禁用工具调用），消费 `StreamEvent.TextDelta` 聚成 summary；遇 `Error` 抛 `RuntimeException`；`InterruptedException` 重置中断标志后抛。
- F20: `forceCompact(conv, client, contextWindow)` 手动入口，跳过 4 层升档直接调 `autoCompact`。

### 3.4 Anthropic 缓存断点与 Agent 集成

- F21: `AnthropicClient.doStream` 在请求构造期间打三处 `CacheControlEphemeral`：
  - `system` 包装成 `MessageCreateParams.System.ofTextBlockParams(List.of(TextBlockParam.builder().text(systemPrompt).cacheControl(CacheControlEphemeral.builder().build()).build()))`。
  - `buildTool(schema, markCache=true)` 给 tools 末项加 `.cacheControl(CacheControlEphemeral.builder().build())`。
  - `markLastUserTailForCache(messageParams)` 倒序找最后一条 user MessageParam，对其末块按 `text()` 或 `toolResult()` 重建并加 cache_control。
- F22: `Agent` 持有 `private ContentReplacementState replacementState = new ContentReplacementState()`，含 `getReplacementState() / setReplacementState(state)` 方法供 fork 路径替换。
- F23: Agent 主循环在 `client.stream` 调用前一刻：`ApplyResult applied = ToolResultBudget.apply(conv, sessionDir, replacementState)` → 非空 `newRecords` 调 `ReplacementRecordsIO.append(sessionDir, applied.newRecords())` → `client.stream(applied.apiConv(), tools)`。
- F24: `AgentTool` 提供 `setParentReplacementState(state)` 与 `parentReplacementState` 字段；fork 路径 `runFork` 把 `parentReplacementState` 透传给 `SubAgentTaskManager.spawnSubAgent(..., parentState)` 的新 overload。
- F25: `SubAgentTaskManager.spawnSubAgent` 新增 6-arg overload 接受 `ContentReplacementState parentState`，在创建子 Agent 后调 `subAgent.setReplacementState(parentState.copy())`。

### 3.5 压缩后恢复

- F26: `com.codebot.compact.RecoveryState`（独立 `public final class`）含 `FileReadRecord(String path, String content, Instant timestamp)` 与 `SkillInvocationRecord(String name, String body, Instant timestamp)` 两个 record，内部用 `Object lock` 守护 `Map<String, FileReadRecord> files` 与 `Map<String, SkillInvocationRecord> skills`；`recordFileRead(path, content)` / `recordSkillInvocation(name, body)` 空 path / 空 name 直接 return，正常路径加锁写入并以 `Instant.now()` 打时间戳；`snapshotFiles(limit)` / `snapshotSkills()` 复制后按 `Comparator.comparing(...timestamp).reversed()` 排序，文件再切到 limit。
- F27: 限额常量 `RECOVERY_FILE_LIMIT = 5` / `RECOVERY_TOKENS_PER_FILE = 5_000` / `RECOVERY_SKILLS_BUDGET = 25_000` / `RECOVERY_TOKENS_PER_SKILL = 5_000` 在 `ContextCompactor` 上 `public static final` 定义；`RECOVERY_CHARS_PER_TOKEN = 3.5` 与 `RECOVERY_TS = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss'Z'").withZone(ZoneOffset.UTC)` 在文件内 private。`approxTokens` 用 `len / 3.5`；`truncateByTokens` 按预算切尾追加 `\n… (content truncated)` 标记。
- F28: `public static String buildRecoveryAttachment(RecoveryState state, List<Map<String, Object>> toolSchemas)` 渲染四段（顺序：`## Recently read files` → `## Active skills` → `## Available tools` → `## Note`）；任一段为空就跳过；全空返回 `""`；技能累计字节超 `RECOVERY_SKILLS_BUDGET` 时 break。`autoCompact` 在生成 `summaryText` 后调 `buildRecoveryAttachment(recovery, toolSchemas)`，非空时用 `\n\n---\n\n` 拼到 `[Compacted conversation summary]\n\n<summary>` 之后；assistant 确认消息附在 user 消息之后。
- F29: `manage` 与 `forceCompact` 多两个参数 `RecoveryState recovery`、`List<Map<String, Object>> toolSchemas`，全部透传给 `autoCompact`。
- F30: `Agent` 持有 `private final RecoveryState recoveryState = new RecoveryState()`，加 `getRecoveryState() / getRegistry() / getProtocol()` 三个 public 方法（fork 子 Agent 与 TUI 命令需要）。Agent 主循环在 `manage` 之前先 `var iterToolSchemas = registry.getAllSchemas(protocol)` + `toolNameFilter` 过滤一次，把结果同时喂给 `manage` 与 `client.stream`；反应式 `forceCompact` 走 `*ContextTooLong` 类错误分支时把 `recoveryState` 与 `iterToolSchemas` 一起传过去。
- F31: `StreamingExecutor` 新增 6-arg 构造（兼容旧 5-arg 重载）接受 `RecoveryState recoveryState`；`executeSingle` 在 `tool.execute(call.args())` 之后调 `snapshotForRecovery(call, result)`，仅在 `recoveryState != null && !result.isError() && "ReadFile".equals(call.toolName())` 时 `Files.readString(Path.of(file_path))` 再写入 state；`IOException` 静默吞掉。`Agent` 在创建 executor 时传 `recoveryState`。
- F32: `SkillHost` 加 `default void recordSkillInvocation(String name, String body) {}` 方法（无副作用默认）；`SkillExecutor.executeInline` 在 `host.activateSkill(...)` 之后立刻调 `host.recordSkillInvocation(skill.meta().name(), body)`；`executeFork` 在 `body = substituteArguments(...)` 后调 `host.recordSkillInvocation(skill.meta().name(), skill.promptBody())`。
- F33: TUI `/compact` 命令在调 `ContextCompactor.forceCompact` 前先 `agent.getRegistry().getAllSchemas(agent.getProtocol())` 拿工具表，并把 `agent.getRecoveryState()` 一起传给新签名。

## 4. 非功能需求

- N1: Layer 1 必须廉价：纯本地文件 I/O + 字符串改写，不调 LLM；每轮 agent loop 都跑也不能成为瓶颈。
- N2: `ToolResultBudget.apply` 不能 mutate 入参 `conv` —— 通过新建 `Message` / `ToolResultBlock` 实例 + 重组 `newHistory` 产出 apiConv。测试用 `applyDoesNotMutateConv` 守住。
- N3: 已决策 id 的复读必须**字节一致**：从 `state.replacements` 拿出来的字符串直接 `decisions.put(id, ...)`，不重新读盘、不重新格式化。这是 prompt cache 命中的硬约束。
- N4: spill 写盘幂等：同 `tool_use_id` 重复运行写同一份内容，同 size 文件已存在则跳过；spill 文件路径稳定（`<sessionDir>/tool_results/<tool_use_id>`），不含时间戳。
- N5: Layer 2 期间不能再触发新的 tool call —— `requestSummary` 走一次性 `client.stream(summaryConv, null)`，tools 传 null 禁用工具调用。
- N6: `ContextCompactor.manage` 替换 conversation 用就地写法：通过 `getMessagesMutable().clear() + addAll(source.getMessages())` 让调用方持有的 `ConversationManager` 引用保持有效。
- N7: 摘要失败抛 `RuntimeException`，由调用方在 `Agent.agentLoop` 内 `try / catch (Exception ignored)` 兜底，确保压缩失败不中断对话。
- N8: 反应式 `forceCompact` 用 `try / catch (Exception ignored)` 包住，失败不影响 Agent 主流程退出错误处理分支。
- N9: 子 Agent fork 的 state 必须是父 state 的**独立深拷贝**：子端 mutate 不影响父端，反向亦然。`new HashSet<>(src.seenIds)` 与 `new HashMap<>(src.replacements)` 浅拷贝足够（值是字符串和 hash key，不需要 deepcopy）。测试用 `copyIsIndependent` 守住。
- N10: 工具类无副作用：`ContextCompactor` / `ToolResultBudget` / `ContentReplacementLifecycle` / `ReplacementRecordsIO` 全部 `final class` + `private` 构造函数 + `public static` 方法，状态完全无副作用。
- N11: `RecoveryState` 必须并发安全：`StreamingExecutor` 用 `Executors.newVirtualThreadPerTaskExecutor()` 并发跑 ReadFile，多个回写可能交错。结构体内 `synchronized (lock)` 保护两张 map；`record*` 方法在空 path / 空 name 上直接 return，方便测试与一次性脚本调用。
- N12: 恢复块限额是**硬上限**：5 个文件、单文件 5K token、技能预算 25K token、单技能 5K token。超出预算时静默丢弃（不抛错），保证压缩输出体积可预测——压缩后摘要 + 恢复总长稳定在约 60K token 以内，远低于 `AUTOCOMPACT_THRESHOLD = 0.80`。

## 5. 设计概要

- 核心包结构（两个 Java 包）:
  - `com.codebot.toolresult/`（新包，7 个类）:
    - `ContentReplacementState.java` — 状态容器 + `copy()`。
    - `ContentReplacementRecord.java` — record + 静态工厂。
    - `ApplyResult.java` — `apply` 返回值 record。
    - `ToolResultBudget.java` — 阈值常量 + `apply` 主流程 + 内部辅助（`spillAndPreview / isAlreadyReplaced / snipStale / copyMessageWithResults / buildManager`）。
    - `ReplacementRecordsIO.java` — JSONL append / load。
    - `ContentReplacementLifecycle.java` — `reconstruct`。
  - `com.codebot.compact/`（升档、摘要与恢复）:
    - `ContextCompactor.java` — 4 个 ratio 阈值 + 4 个尺寸常量 + 4 个恢复限额常量 + `SUMMARY_SYSTEM_PROMPT` + `manage` + `forceCompact` + 4 个 layer 方法 + `buildRecoveryAttachment` + helpers (`estimateTokens / requestSummary / serializeForSummary / truncatePreservingBoundaries / appendMessage / rebuildConversation / replaceConversation / approxTokens / truncateByTokens / firstLine`)。`applyToolResultBudget` 标 `@Deprecated`，过渡期保留兼容。
    - `RecoveryState.java` — `public final class` 含 `FileReadRecord` / `SkillInvocationRecord` 两个 record + `record*` / `snapshot*` 方法。
- 主流程（每轮 agent loop）:
  - 主循环开头先 `var iterToolSchemas = registry.getAllSchemas(protocol)` + `toolNameFilter` 过滤，避免重算并保证恢复消息与 API 请求看到的工具集一致。
  - `String compactMsg = ContextCompactor.manage(conv, client, contextWindow, wd, compactTracking, recoveryState, iterToolSchemas)`，非空时 `CompactEvent` 推到事件队列。
  - 各种 system reminder 写入 conv。
  - 在 `client.stream` 调用前一刻：`ApplyResult applied = ToolResultBudget.apply(conv, sessionDir, replacementState)` → 非空 records 调 `ReplacementRecordsIO.append(sessionDir, applied.newRecords())` → `client.stream(applied.apiConv(), iterToolSchemas)`。
- 主流程（工具调用快照）:
  - `StreamingExecutor.executeSingle` 在 `tool.execute(...)` 之后调 `snapshotForRecovery(call, result)`；命中 ReadFile + 非错误时 `Files.readString(Path.of(file_path))` 写入 `recoveryState`。Agent 构造 `StreamingExecutor` 时把 `recoveryState` 透传过去。
- 主流程（Skill 调用快照）:
  - 上层命令调 `SkillExecutor.executeInline / executeFork` → `host.recordSkillInvocation(name, body)`（`SkillHost` 默认 no-op，Agent 实现把它桥接到 `recoveryState.recordSkillInvocation`）。
- 主流程（反应式恢复）:
  - LLM 流返回 context 类错误 → `agentLoop` 错误恢复分支 → `RetryEvent` 通知用户 → `ContextCompactor.forceCompact(conv, client, contextWindow, recoveryState, iterToolSchemas)` → `continue` 重试当前轮。
- 主流程（TUI `/compact`）:
  - 用户输入 `/compact` → `CodeBotModel` 取 `agent.getRegistry().getAllSchemas(agent.getProtocol())` + `agent.getRecoveryState()` → `ContextCompactor.forceCompact` 走 Auto-compact + 恢复块。
- 主流程（fork 子 Agent）:
  - `AgentTool.runFork` 调 `taskManager.spawnSubAgent(..., parentReplacementState)` → `SubAgentTaskManager` 6-arg overload 创建子 Agent 时 `subAgent.setReplacementState(parentState.copy())` → 子 Agent 用克隆状态独立演化。
- Anthropic 客户端缓存断点（`AnthropicClient.doStream`）:
  - `systemBlock` 用 `TextBlockParam` + `cacheControl`，包装成 `MessageCreateParams.System.ofTextBlockParams`。
  - `buildTool(schema, isLast)`：末项 `markCache=true`，调 `builder.cacheControl(...)`。
  - `markLastUserTailForCache(messageParams)` 倒序找最后一条 user MessageParam，对其末块（`text()` 或 `toolResult()`）用 `toBuilder().cacheControl(...).build()` 重建。
- 与其他模块的交互:
  - 依赖 `com.codebot.conversation`（操作 `ConversationManager / Message / ToolUseBlock / ToolResultBlock / ThinkingBlock`）。
  - 依赖 `com.codebot.llm`（`LlmClient.stream` 摘要、`StreamEvent.TextDelta` / `StreamEnd` / `Error`、Anthropic SDK 的 `CacheControlEphemeral`）。
  - 被 `com.codebot.agent.Agent`（主循环、错误恢复）、`com.codebot.subagent.AgentTool` + `SubAgentTaskManager`（fork clone）调用。

## 6. Out of Scope

- 跨进程 / 跨会话的压缩缓存。
- 持久化的 `RecoveryState`：JVM 退出后状态丢失，不做磁盘落盘。下一次启动靠用户自然触发 ReadFile / Skill 调用重新填充。
- Session memory compaction：与记忆系统配合，本章不做。
- 用真实 tokenizer 替代 `chars / 3.5` 近似估算。
- 进度回调或 UI 流式预览：本章只在压缩完成后回传一行 status。
- 熔断器：Java 版目前不做连续失败计数，失败直接 `catch (Exception ignored)` 跳过。
- 完整 resume 流程：transcript records 已落盘且 `ContentReplacementLifecycle.reconstruct` 可用，但 resume 主流程不在本章范围。
- Fork 入口本身的接入：`AgentTool.runFork` 现有路径上 `setParentConversation` 未被主流程调用（已知 dead branch），本章只确保 fork 一旦真正接入，state 继承的 API 是齐的。
- 配置化阈值：所有阈值是 `public static final` 常量，调整需改源码重编译。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch08: 上下文管理 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。每条任务记录实际落地的文件与行号。

## T1: `ContentReplacementState` + `copy()`

- 影响文件: `src/main/java/com/codebot/toolresult/ContentReplacementState.java`
- 依赖任务: 无
- 完成标准: `public final class ContentReplacementState`，含 `Set<String> seenIds = new HashSet<>()` 与 `Map<String, String> replacements = new HashMap<>()` 两 final field；accessors `seenIds()` / `replacements()` 返回可变引用；`copy()` 通过 `new HashSet<>(this.seenIds)` 与 `new HashMap<>(this.replacements)` 浅拷贝产出独立实例；测试 `newReturnsEmpty / copyIsIndependent` 通过。

## T2: `ContentReplacementRecord` record

- 影响文件: `src/main/java/com/codebot/toolresult/ContentReplacementRecord.java`
- 依赖任务: 无
- 完成标准: `public record ContentReplacementRecord(String kind, String toolUseId, String replacement)`，含静态常量 `KIND_TOOL_RESULT = "tool-result"` 与静态工厂 `toolResult(toolUseId, replacement)`。

## T3: `ApplyResult` record

- 影响文件: `src/main/java/com/codebot/toolresult/ApplyResult.java`
- 依赖任务: T1, T2
- 完成标准: `public record ApplyResult(ConversationManager apiConv, List<ContentReplacementRecord> newRecords)`。

## T4: `ReplacementRecordsIO` JSONL 持久化

- 影响文件: `src/main/java/com/codebot/toolresult/ReplacementRecordsIO.java`
- 依赖任务: T2
- 完成标准: `RECORDS_FILENAME = "replacement_records.jsonl"` 常量；`append(sessionDir, records)` 空 list 直接 return，自动 `Files.createDirectories(sessionDir)`，用 `BufferedWriter + APPEND` 选项追加，每行一个 Jackson `writeValueAsString(record)`，`kind` 为空或 null 自动填 `KIND_TOOL_RESULT`；`load(sessionDir)` 缺文件返回 `Collections.emptyList()`，`Files.readAllLines` 后逐行 `MAPPER.readValue`；测试 `appendAndLoadRoundtrip / loadMissingFile` 通过。

## T5: `ContentReplacementLifecycle.reconstruct`

- 影响文件: `src/main/java/com/codebot/toolresult/ContentReplacementLifecycle.java`
- 依赖任务: T1, T2
- 完成标准: 静态方法 `reconstruct(messages, records, inheritedReplacements)`：先 seed `seenIds` = 所有 `getToolResults` 的 `tool_use_id`；按 `kind == KIND_TOOL_RESULT` 过滤 records 并命中 candidate 才写入 `replacements`；可选 `inheritedReplacements` (Map) 用 `putIfAbsent` 在 candidate ∩ 未被覆盖时补全；测试 `reconstructFromRecords / reconstructWithInheritedParent` 通过。

## T6: `ToolResultBudget` 阈值与 Design B 主流程

- 影响文件: `src/main/java/com/codebot/toolresult/ToolResultBudget.java`
- 依赖任务: T1, T3
- 完成标准: 阈值常量 `SINGLE_RESULT_LIMIT = 15_000` / `MESSAGE_AGGREGATE_LIMIT = 20_000` / `OLD_RESULT_SNIP_CHARS = 2_000` / `KEEP_RECENT_TURNS = 10` / `SPILL_SUBDIR = "tool_results"` 在顶部 `public static final` 定义。`apply(conv, sessionDir, state) -> ApplyResult` 静态方法实现：
  1. 阶段 1 对每条 tr 四类分类（replacements 命中复读 / seenIds 命中冻结原文 / 外部 `[Result of` 或 `[Stale output snipped:` 前缀冻结作为已知决策 / fresh）。
  2. 阶段 2 Pass 1 单条 > `SINGLE_RESULT_LIMIT` 调 `spillAndPreview`，写入 state 与 records；spill 失败 freeze 原文。
  3. 阶段 3 Pass 2 聚合超限 + 按 size 降序选 fresh。
  4. 阶段 4 剩余 fresh 全部 `state.seenIds.add` + `decisions.put(id, tr.content())`。
  5. 末段 `copyMessageWithResults` 重组 message + `snipStale` + `buildManager` 重放产出新 `ConversationManager`。

## T7: spill / preview / snip / buildManager helpers

- 影响文件: `src/main/java/com/codebot/toolresult/ToolResultBudget.java`
- 依赖任务: T6
- 完成标准:
  - `spillAndPreview(spillDir, tr)`：`Files.createDirectories(spillDir)` + `Files.writeString(file, content)`；同 size 文件已存在则直接返回 preview；返回 `[Result of N chars saved to PATH — read with ReadFile if needed]`；IO 异常返回 null（caller 据此 freeze 为原文）。
  - `isAlreadyReplaced(s)` 识别 `[Result of ` 和 `[Stale output snipped:` 两种前缀。
  - `snipStale(messages)` 数 `assistant && (toolUses==null || toolUses.isEmpty())` 当作一轮；总轮数 ≤ `KEEP_RECENT_TURNS` 直接 return；超 boundary 的消息里超 `OLD_RESULT_SNIP_CHARS` 字符且未 `isAlreadyReplaced` 前缀的 tool result 整体替换为 `[Stale output snipped: N chars]`。
  - `copyMessageWithResults(src, newResults)` 产出新 `Message` 实例，复制 role/content/thinking/toolUses 引用，注入新 toolResults 列表。
  - `buildManager(messages)` 通过 `new ConversationManager()` + `addAssistantFull` / `addToolResultsMessage` / `addUserMessage` / `addAssistantMessage` 重放消息。

## T8: `ContextCompactor` 阈值与 4 层升档

- 影响文件: `src/main/java/com/codebot/compact/ContextCompactor.java:33-83`
- 依赖任务: 无
- 完成标准: 4 个 ratio 阈值 `SNIP_THRESHOLD = 0.50` / `MICROCOMPACT_THRESHOLD = 0.60` / `COLLAPSE_THRESHOLD = 0.70` / `AUTOCOMPACT_THRESHOLD = 0.80`；4 个尺寸常量 `SNIP_CHAR_LIMIT = 2000` / `MICROCOMPACT_LIMIT = 5000` / `SINGLE_RESULT_LIMIT = 5000` / `KEEP_RECENT_TURNS = 10`；`SUMMARY_SYSTEM_PROMPT` 是 Text Block 含六类必须保留信息。`manage(conv, client, contextWindow)` 算 ratio 升档到对应层，未命中返回空串；命中 Snip 时回填 `"Snipped verbose tool results"`。

## T9: `estimateTokens`

- 影响文件: `src/main/java/com/codebot/compact/ContextCompactor.java:131-161`
- 依赖任务: T8
- 完成标准: 函数对 `Message.content` / `ToolUseBlock.arguments`（Jackson 序列化）/ `ToolResultBlock.content` / `ThinkingBlock.thinking` 四类按 `length / 3.5` 估算并加常数偏置（content +4，tool_use +50，tool_result +10）；`safeLength` 兜空指针。

## T10: Layer 1 `snip`

- 影响文件: `src/main/java/com/codebot/compact/ContextCompactor.java:165-199`
- 依赖任务: T8, T9
- 完成标准: 计算 `recentBoundary = max(0, size - KEEP_RECENT_TURNS * 3)`；遍历 boundary 之前的消息，超 `SNIP_CHAR_LIMIT = 2000` 的 tool result 换为 `[Output snipped: %d chars, %d lines]`；最后 `rebuildConversation` 重塑会话，返回 `boolean`。

## T11: Layer 2 `microcompact` 与 `truncatePreservingBoundaries`

- 影响文件: `src/main/java/com/codebot/compact/ContextCompactor.java:203-237, 347-375`
- 依赖任务: T10
- 完成标准: `microcompact` 在 `recentBoundary` 之前对超过 `MICROCOMPACT_LIMIT = 5000` 的 tool result 调 `truncatePreservingBoundaries(lines, 500)` 改为头 5 行 + `... (N lines omitted) ...` + 尾 5 行；累计 `savedChars` 后返回 `"Microcompacted: saved ~%d chars from old tool results"`；`truncatePreservingBoundaries` 不足 10 行时直接 `String.join("\n", ...)` 并按 `maxChars` 兜底。

## T12: Layer 3 `contextCollapse`

- 影响文件: `src/main/java/com/codebot/compact/ContextCompactor.java:241-274`
- 依赖任务: T9, T11
- 完成标准: 消息数不足 `KEEP_RECENT_TURNS * 3` 时降级到 `autoCompact`；按 `splitIdx = size - 30` 切分；早期段 `serializeForSummary(oldMessages, 500)` + `requestSummary` 走 LLM；新建 `ConversationManager` 装 `[Earlier conversation summary]` 用户消息 + assistant 确认消息 + 最近 30 条原样 `appendMessage`；`replaceConversation` 就地替换；返回 `"Context collapsed: N -> M estimated tokens (kept 10 recent turns)"`。

## T13: Layer 4 `autoCompact`

- 影响文件: `src/main/java/com/codebot/compact/ContextCompactor.java:278-294`
- 依赖任务: T9, T11
- 完成标准: 全量 `serializeForSummary(messages, 500)` + `requestSummary` 走 LLM；新建 `ConversationManager` 装 `[Compacted conversation summary]\n\n<summary>` + assistant 确认消息；`replaceConversation` 就地替换；返回 `"Compacted: N -> M estimated tokens"`。

## T14: `forceCompact` 手动入口

- 影响文件: `src/main/java/com/codebot/compact/ContextCompactor.java:86-88`
- 依赖任务: T13
- 完成标准: 跳过 4 层升档，直接调 `autoCompact(conv, client, contextWindow)`。

## T15: 摘要 helpers (`requestSummary` / `serializeForSummary` / `rebuildConversation` / `replaceConversation` / `appendMessage`)

- 影响文件: `src/main/java/com/codebot/compact/ContextCompactor.java:298-405`
- 依赖任务: T13
- 完成标准:
  - `requestSummary` 新建临时 `ConversationManager`，调 `client.stream(summaryConv, null)`（tools = null 禁用工具），消费 `StreamEvent.TextDelta` 聚成 summary；遇 `Error` 抛 `RuntimeException`；`InterruptedException` 重置中断标志后抛。
  - `serializeForSummary` 按 `[role]: content` + `[tool_use name]: id` + `[tool_result]: content`（超过 cap 截断 + "..."）拼字符串。
  - `appendMessage` 根据 `toolUses` / `toolResults` / role 分发到 `addAssistantFull` / `addToolResultsMessage` / `addUserMessage`。
  - `rebuildConversation` 新建 ConversationManager 用 `appendMessage` 重放 + `replaceConversation` 就地替换。
  - `replaceConversation` 通过 `getMessagesMutable().clear() + addAll(source.getMessages())` 保证调用方持有的引用不失效。

## T16: Agent 集成（state 字段 + Apply 调用 + records 持久化）

- 影响文件: `src/main/java/com/codebot/agent/Agent.java:14-22, 51-54, 155-167`
- 依赖任务: T1, T4, T6
- 完成标准:
  - import 段加 `com.codebot.toolresult.{ApplyResult, ContentReplacementState, ReplacementRecordsIO, ToolResultBudget}` + `java.nio.file.{Path, Paths}`。
  - `Agent` 类新增 field `private ContentReplacementState replacementState = new ContentReplacementState()` 与 getter/setter。
  - 主循环 `client.stream` 调用前一刻：`Path sessionDir = Paths.get(workDir == null ? "." : workDir, ".codebot/session")` → `ApplyResult applied = ToolResultBudget.apply(conv, sessionDir, replacementState)` → 非空 `applied.newRecords()` 调 `ReplacementRecordsIO.append(sessionDir, applied.newRecords())`（失败 silently 忽略）→ `client.stream(applied.apiConv(), tools)`。

## T17: Fork 状态继承

- 影响文件: `src/main/java/com/codebot/subagent/AgentTool.java:104-108, 286-293`、`src/main/java/com/codebot/subagent/SubAgentTaskManager.java:108-137`
- 依赖任务: T1, T16
- 完成标准:
  - `AgentTool` 新增 field `private com.codebot.toolresult.ContentReplacementState parentReplacementState`，含 `setParentReplacementState` 方法。
  - `runFork` 把 `parentReplacementState` 传给 `taskManager.spawnSubAgent` 的 6-arg overload。
  - `SubAgentTaskManager.spawnSubAgent` 新增 6-arg overload 接受 `ContentReplacementState parentState`；原 5-arg overload 转调 6-arg 传 `null`；在创建 subAgent 后判断 `parentState != null` 时 `subAgent.setReplacementState(parentState.copy())`。

## T18: Anthropic 缓存断点

- 影响文件: `src/main/java/com/codebot/llm/AnthropicClient.java:65-100, 281-360`
- 依赖任务: 无
- 完成标准:
  - `systemBlock = TextBlockParam.builder().text(systemPrompt).cacheControl(CacheControlEphemeral.builder().build()).build()`；`paramsBuilder.system(MessageCreateParams.System.ofTextBlockParams(List.of(systemBlock)))`。
  - `buildTool(schema, isLast)` 签名扩展 `boolean markCache` 参数，末项调 `builder.cacheControl(CacheControlEphemeral.builder().build())`。
  - 新增 `markLastUserTailForCache(messageParams)` 倒序找最后一条 user MessageParam，对其 `content()` 处理：string 上转 block 列表带 cache_control；block 列表对末块按 `text()` / `toolResult()` 分别用 `toBuilder().cacheControl(...).build()` 重建；最后用 `MessageParam.builder().role(USER).contentOfBlockParams(blocks).build()` 替换原 MessageParam（SDK 类型 immutable，必须替换整条消息）。
  - `messageParams = buildMessages(conv.getMessages())` 后立刻调 `markLastUserTailForCache(messageParams)`。

## T19: 测试

- 影响文件: `src/test/java/com/codebot/toolresult/ContentReplacementStateTest.java`、`src/test/java/com/codebot/toolresult/ToolResultBudgetTest.java`、`src/test/java/com/codebot/toolresult/ReplacementRecordsIOTest.java`
- 依赖任务: T1–T16
- 完成标准:
  - `ContentReplacementStateTest`: `newReturnsEmpty / copyIsIndependent` 2 case。
  - `ToolResultBudgetTest`: `applyDoesNotMutateConv / firstCallFreezesUnreplaced / replacementByteIdentical / frozenNeverReplaced / aggregateOnlyPicksFresh / reconstructFromRecords / reconstructWithInheritedParent` 7 case。
  - `ReplacementRecordsIOTest`: `appendAndLoadRoundtrip / loadMissingFile` 2 case。
  - `./gradlew test --tests "com.codebot.toolresult.*"` 全部通过。

## T20: 端到端验证

- 影响文件: 无（仅运行验证）
- 依赖任务: T16–T19
- 完成标准:
  - `./gradlew compileJava --no-daemon` 通过。
  - `./gradlew test --tests "com.codebot.toolresult.*"` 11 个测试全过。
  - 制造一个会产生大 tool result 的会话（连续 Bash 大输出），观察 `<sessionDir>/tool_results/<tool_use_id>` 文件落地、`<sessionDir>/replacement_records.jsonl` 有对应 records。
  - 制造一个会爆 context 的会话（连续 Bash 大输出），观察 Agent 事件流：按 token 占比依次出现 `CompactEvent("Snipped verbose tool results")` → `CompactEvent("Microcompacted: ...")` → `CompactEvent("Context collapsed: ...")` → `CompactEvent("Compacted: ...")`。
  - LLM 返回 context 类错误时 Agent 自动调 `forceCompact` 并 `RetryEvent` 通知用户后重试。

## T21: `RecoveryState` 类与限额常量

- 影响文件: `src/main/java/com/codebot/compact/RecoveryState.java`（新文件）、`src/main/java/com/codebot/compact/ContextCompactor.java:30-50`
- 依赖任务: T1
- 完成标准:
  - 新文件 `RecoveryState.java`：`public final class` 含两个 `public record`（`FileReadRecord(String path, String content, Instant timestamp)`、`SkillInvocationRecord(String name, String body, Instant timestamp)`）；内部 `Object lock` + `Map<String, FileReadRecord> files` + `Map<String, SkillInvocationRecord> skills`；`recordFileRead(path, content)` / `recordSkillInvocation(name, body)` 空 path / 空 name 直接 return，正常时 `synchronized (lock)` 写入并以 `Instant.now()` 打时间戳；`snapshotFiles(limit)` / `snapshotSkills()` 复制后按 `Comparator.comparing(...timestamp).reversed()` 排序，文件再切到 limit。
  - `ContextCompactor` 新增 `public static final int RECOVERY_FILE_LIMIT = 5` / `RECOVERY_TOKENS_PER_FILE = 5_000` / `RECOVERY_SKILLS_BUDGET = 25_000` / `RECOVERY_TOKENS_PER_SKILL = 5_000`；private 常量 `RECOVERY_CHARS_PER_TOKEN = 3.5` 与 `RECOVERY_TS = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss'Z'").withZone(ZoneOffset.UTC)`。

## T22: `buildRecoveryAttachment` + `autoCompact` 签名扩展

- 影响文件: `src/main/java/com/codebot/compact/ContextCompactor.java:90-115, 248-360`
- 依赖任务: T21
- 完成标准:
  - `approxTokens(s)` 用 `(int)(s.length() / RECOVERY_CHARS_PER_TOKEN)`；`truncateByTokens(s, budget)` 超额时按 byte 上限切并追加 `\n… (content truncated)`；`firstLine(s)` 返回第一行非空 trim 文本。
  - `public static String buildRecoveryAttachment(RecoveryState state, List<Map<String, Object>> toolSchemas)` 渲染四段（`## Recently read files` / `## Active skills` / `## Available tools` / `## Note`）；空 state + 空 schemas 时返回 `""`；技能预算超 `RECOVERY_SKILLS_BUDGET` 时 break。
  - `manage` 与 `forceCompact` 加两个参数 `RecoveryState recovery, List<Map<String, Object>> toolSchemas`，全部透传给 `autoCompact`。
  - `autoCompact` 生成 `summaryText` 后调 `buildRecoveryAttachment(recovery, toolSchemas)`，非空时用 `\n\n---\n\n` 拼到 `[Compacted conversation summary]\n\n<summary>` 之后；assistant 确认消息依旧附在后面。
  - 新增测试 `src/test/java/com/codebot/compact/RecoveryAttachmentTest.java` 5 个用例：`emptyWhenNothingRecorded / emitsAllSections / fileLimitAndNewestFirst / truncatesPerFile / skillBudget` 全部通过。

## T23: Agent / StreamingExecutor / Skill / TUI 接入

- 影响文件: `src/main/java/com/codebot/agent/Agent.java:45-80, 100-145, 230-250, 300-320`、`src/main/java/com/codebot/agent/StreamingExecutor.java:1-60, 130-200`、`src/main/java/com/codebot/skill/SkillHost.java`、`src/main/java/com/codebot/skill/SkillExecutor.java:20-50`、`src/main/java/com/codebot/tui/CodeBotModel.java:880-900`
- 依赖任务: T21, T22
- 完成标准:
  - `Agent` 新增 `private final RecoveryState recoveryState = new RecoveryState()`，以及 `getRecoveryState() / getRegistry() / getProtocol()` 三个 public 方法。
  - Agent 主循环在 `ContextCompactor.manage` 之前先 `var iterToolSchemas = registry.getAllSchemas(protocol)` + `toolNameFilter` 过滤一次；删除原本紧贴 `client.stream` 的同段重算；`manage(...)` 与 `client.stream(applied.apiConv(), tools)` 共用 `iterToolSchemas`；`forceCompact` 在错误恢复分支处把 `recoveryState` + `iterToolSchemas` 一起传过去。
  - `StreamingExecutor` 加 6-arg 构造（兼容旧 5-arg 重载）接受 `RecoveryState recoveryState`；`executeSingle` 在 `tool.execute(call.args())` 之后调 `snapshotForRecovery(call, result)`，命中 ReadFile + 非错误时 `Files.readString(Path.of(file_path))` 写入 state；`IOException` 静默吞掉。Agent 在 `new StreamingExecutor(registry, checker, hookEngine, queue, recoveryState)` 处传 `recoveryState`。
  - `SkillHost` 加 `default void recordSkillInvocation(String name, String body) {}`；`SkillExecutor.executeInline` 在 `host.activateSkill(...)` 之后调 `host.recordSkillInvocation(skill.meta().name(), body)`；`executeFork` 在 `body = substituteArguments(...)` 后调 `host.recordSkillInvocation(skill.meta().name(), skill.promptBody())`。
  - `CodeBotModel` 的 `/compact` 命令分支在调 `ContextCompactor.forceCompact` 之前取 `agent.getRegistry().getAllSchemas(agent.getProtocol())` + `agent.getRecoveryState()` 一并传给新签名；agent 为 null 时退化为 `List.<Map<String, Object>>of()`。

## T24: 端到端验证（恢复部分）

- 影响文件: 无（仅运行验证）
- 依赖任务: T21, T22, T23
- 完成标准:
  - `./gradlew test --tests "com.codebot.compact.*"` 含 `RecoveryAttachmentTest` 5 个用例全部通过。
  - `./gradlew test` 全套通过（旧测试不被破坏）。
  - 制造一次连续 ReadFile 6+ 文件后 `/compact` 的会话：摘要消息出现 `## Recently read files` 段且只列最近 5 个；任一 5K token 以上的文件出现 `(content truncated)` 标记。
  - 调用某个 skill 之后 `/compact`：摘要消息出现 `## Active skills` 段并包含 skill 名 + SOP 片段。
  - 摘要消息以 `## Note` 段收尾，强调若需要原文请重新读文件而不是靠摘要猜。

## 进度

- T1-T24（含「压缩后恢复」相关 T21-T24）

```

````markdown
# ch08: 上下文管理 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性

### 1.1 `com.codebot.toolresult` 包

- [ ] `ContentReplacementState` 在 `src/main/java/com/codebot/toolresult/ContentReplacementState.java:22` 定义：`final class` 含 `Set<String> seenIds = new HashSet<>()` + `Map<String, String> replacements = new HashMap<>()`；accessor `seenIds()` / `replacements()` 返回可变引用；`copy()` 通过 `new HashSet<>(this.seenIds)` + `new HashMap<>(this.replacements)` 浅拷贝产出独立实例。
- [ ] `ContentReplacementRecord` record 在 `ContentReplacementRecord.java:9` 定义：含 `kind / toolUseId / replacement` 三 component；`KIND_TOOL_RESULT = "tool-result"` 常量在 line 11；静态工厂 `toolResult(toolUseId, replacement)`。
- [ ] `ApplyResult` record 在 `ApplyResult.java:15` 定义：`ConversationManager apiConv` + `List<ContentReplacementRecord> newRecords` 两 component。
- [ ] `ReplacementRecordsIO` 在 `ReplacementRecordsIO.java:22` 定义：`final class` + `private` 构造；`RECORDS_FILENAME = "replacement_records.jsonl"` 常量。
- [ ] `ReplacementRecordsIO.append(sessionDir, records)`：空列表直接 return；`Files.createDirectories(sessionDir)`；用 `BufferedWriter` + `StandardOpenOption.APPEND` 追加 Jackson 序列化的对象；`kind` 为空或 null 自动填 `KIND_TOOL_RESULT`。
- [ ] `ReplacementRecordsIO.load(sessionDir)`：文件不存在返回 `Collections.emptyList()`；`Files.readAllLines` 后逐行 `MAPPER.readValue`。
- [ ] `ContentReplacementLifecycle.reconstruct(messages, records, inheritedReplacements)` 在 `ContentReplacementLifecycle.java:24` 实现，包含 candidate-only 过滤与 `putIfAbsent` 风格 gap-fill。
- [ ] `ToolResultBudget` 在 `ToolResultBudget.java:32` 定义：`final class` + `private` 构造。
- [ ] 阈值常量 `SINGLE_RESULT_LIMIT = 15_000`、`MESSAGE_AGGREGATE_LIMIT = 20_000`、`OLD_RESULT_SNIP_CHARS = 2_000`、`KEEP_RECENT_TURNS = 10`、`SPILL_SUBDIR = "tool_results"` 在 `ToolResultBudget.java:35-47` 定义。
- [ ] `ToolResultBudget.apply(conv, sessionDir, state) -> ApplyResult` 实现 Design B 主流程（4 阶段 + Pass 3 snipStale + buildManager）。
- [ ] `spillAndPreview` 输出 `[Result of N chars saved to PATH — read with ReadFile if needed]`（byte-stable anchor，不能轻改）；同 size 已存在的文件不重写。
- [ ] `snipStale` 基于轮数 boundary + `OLD_RESULT_SNIP_CHARS` 阈值，输出 `[Stale output snipped: N chars]`；不动入参 messages。
- [ ] `copyMessageWithResults` 产出新 `Message` 实例，复制 role/content/thinking/toolUses 引用，注入新 toolResults。
- [ ] `buildManager` 通过 `new ConversationManager()` + `addAssistantFull` / `addToolResultsMessage` / `addUserMessage` / `addAssistantMessage` 重放消息，产出独立 `ConversationManager`。

### 1.2 `com.codebot.compact.ContextCompactor`

- [ ] 4 ratio 阈值 `SNIP_THRESHOLD = 0.50` / `MICROCOMPACT_THRESHOLD = 0.60` / `COLLAPSE_THRESHOLD = 0.70` / `AUTOCOMPACT_THRESHOLD = 0.80` 在 `ContextCompactor.java:35-38` 定义。
- [ ] 4 个尺寸常量 `SNIP_CHAR_LIMIT = 2000` / `MICROCOMPACT_LIMIT = 5000` / `SINGLE_RESULT_LIMIT = 5000` / `KEEP_RECENT_TURNS = 10` 在 `ContextCompactor.java:40-43` 定义。
- [ ] `SUMMARY_SYSTEM_PROMPT` 在 `ContextCompactor.java:45-55` 定义（Text Block，明确列出 file paths / decisions / current task / pending work / error states / code snippets 六类必须保留信息）。
- [ ] `manage(conv, client, contextWindow)` 在 `ContextCompactor.java:66-83` 实现：按 ratio 升档到 4 层之一，未命中返回空串。
- [ ] `forceCompact(conv, client, contextWindow)` 在 `ContextCompactor.java:86-88` 实现：直接调 `autoCompact`，跳过 4 层升档。
- [ ] `estimateTokens(messages)` 在 `ContextCompactor.java:131-161` 实现：覆盖 content / tool args / tool_results / thinking_blocks 四源（共 4 个 for 分支）。
- [ ] `snip(conv)` 在 `ContextCompactor.java:165-199` 实现：遍历 `recentBoundary` 之前的消息，超 `SNIP_CHAR_LIMIT` 的 tool result 换为 `[Output snipped: %d chars, %d lines]` 一行。
- [ ] `microcompact(conv, contextWindow)` 在 `ContextCompactor.java:203-237` 实现：对超 `MICROCOMPACT_LIMIT` 的 tool result 调 `truncatePreservingBoundaries` 做头尾保留。
- [ ] `contextCollapse(conv, client, contextWindow)` 在 `ContextCompactor.java:241-274` 实现：消息数不足 30 条时降级到 `autoCompact`；否则早期段摘要后用 `[Earlier conversation summary]` + assistant 确认 + 最近 30 条原样组成新对话。
- [ ] `autoCompact(conv, client, contextWindow)` 在 `ContextCompactor.java:278-294` 实现：全量摘要 + `[Compacted conversation summary]` + assistant 确认替换整段对话；返回 `"Compacted: N -> M estimated tokens"`。
- [ ] `requestSummary` 在 `ContextCompactor.java:298-322` 实现：临时 `ConversationManager` + `client.stream(summaryConv, null)`（tools = null 禁用工具调用）；`StreamEvent.TextDelta` 聚 summary；`Error` 抛 `RuntimeException`；`InterruptedException` 重置中断标志后抛。
- [ ] `serializeForSummary` 在 `ContextCompactor.java:324-345` 实现：按 `[role]: content` + `[tool_use name]: id` + `[tool_result]: content`（cap 截断 + "..."）拼字符串。
- [ ] `truncatePreservingBoundaries` 在 `ContextCompactor.java:347-375` 实现：默认头 5 行 + `... (%d lines omitted) ...` + 尾 5 行；不足 10 行降级整段 `String.join` + `maxChars`。
- [ ] `appendMessage` / `rebuildConversation` / `replaceConversation` 在 `ContextCompactor.java:377-405` 实现，保证调用方持有的 `ConversationManager` 引用不失效。
- [ ] 工具类无副作用：`ContextCompactor` / `ToolResultBudget` / `ContentReplacementLifecycle` / `ReplacementRecordsIO` 全部 `final class` + `private` 构造函数 + `public static` 方法。
- [ ] 边界处理 `safeLength(null) == 0`、`recentBoundary = max(0, ...)`、`truncatePreservingBoundaries` 收到空数组返回空串、`contextCollapse` 在不足 30 条时降级。
- [ ] `manage` / `forceCompact` / `autoCompact` 都接受 `RecoveryState recovery` 与 `List<Map<String, Object>> toolSchemas`，`autoCompact` 在生成 `summaryText` 后用 `\n\n---\n\n` 拼接 `buildRecoveryAttachment` 的返回值。

### 1.3 `RecoveryState` 与恢复块

- [ ] 新文件 `src/main/java/com/codebot/compact/RecoveryState.java`：`public final class` + 两个 `public record`（`FileReadRecord(String path, String content, Instant timestamp)` / `SkillInvocationRecord(String name, String body, Instant timestamp)`），内部 `Object lock` 守护 `Map<String, FileReadRecord> files` + `Map<String, SkillInvocationRecord> skills`。
- [ ] `recordFileRead(path, content)` / `recordSkillInvocation(name, body)` 空 path / 空 name 直接 return；正常时 `synchronized (lock)` 写入并以 `Instant.now()` 打时间戳。
- [ ] `snapshotFiles(limit)` / `snapshotSkills()` 复制后按 `Comparator.comparing(...timestamp).reversed()` 排序，文件再切到 limit。
- [ ] `ContextCompactor` 顶部 `public static final int RECOVERY_FILE_LIMIT = 5` / `RECOVERY_TOKENS_PER_FILE = 5_000` / `RECOVERY_SKILLS_BUDGET = 25_000` / `RECOVERY_TOKENS_PER_SKILL = 5_000` 定义；private `RECOVERY_CHARS_PER_TOKEN = 3.5` + `RECOVERY_TS` (DateTimeFormatter)。
- [ ] `approxTokens(s)` / `truncateByTokens(s, budget)` / `firstLine(s)` 三个 helper；`truncateByTokens` 超额时按 byte 上限切并追加 `\n… (content truncated)`。
- [ ] `public static String buildRecoveryAttachment(RecoveryState state, List<Map<String, Object>> toolSchemas)` 依次输出 `## Recently read files` / `## Active skills` / `## Available tools` / `## Note`；空 state + 空 schemas 时返回 `""`；技能预算超 `RECOVERY_SKILLS_BUDGET` 时停止追加。

### 1.4 Agent / StreamingExecutor / Skill / TUI 接入

- [ ] `Agent` 持有 `private final RecoveryState recoveryState = new RecoveryState()`，提供 `getRecoveryState() / getRegistry() / getProtocol()` 三个 public 方法。
- [ ] Agent 主循环在 `ContextCompactor.manage` 之前先 `var iterToolSchemas = registry.getAllSchemas(protocol)` + `toolNameFilter` 过滤一次；后续 `manage` 与 `client.stream` 复用同一份；原本紧贴 `client.stream` 的同段重算被删除（避免恢复消息与请求看到的工具集不一致）。
- [ ] 反应式 `forceCompact` 调用点把 `recoveryState` + `iterToolSchemas` 一起传过去。
- [ ] `StreamingExecutor` 加 6-arg 构造重载接受 `RecoveryState recoveryState`（保留 5-arg 兼容）；`executeSingle` 在 `tool.execute(call.args())` 之后调 `snapshotForRecovery(call, result)`，仅在 `recoveryState != null && !result.isError() && "ReadFile".equals(call.toolName())` 时 `Files.readString(Path.of(file_path))` 写入 state；`IOException` 静默吞掉。Agent 在 `new StreamingExecutor(...)` 处传 `recoveryState`。
- [ ] `SkillHost` 加 `default void recordSkillInvocation(String name, String body) {}`；`SkillExecutor.executeInline` 在 `host.activateSkill(...)` 之后调 `host.recordSkillInvocation(skill.meta().name(), body)`；`executeFork` 在 `body = substituteArguments(...)` 之后调 `host.recordSkillInvocation(skill.meta().name(), skill.promptBody())`。
- [ ] `CodeBotModel` 的 `/compact` 命令分支在调 `ContextCompactor.forceCompact` 之前取 `agent.getRegistry().getAllSchemas(agent.getProtocol())` + `agent.getRecoveryState()` 一并传给新签名；agent 为 null 时退化为 `List.<Map<String, Object>>of()`。

### 1.5 Anthropic 缓存断点

- [ ] `systemBlock = TextBlockParam.builder().text(systemPrompt).cacheControl(CacheControlEphemeral.builder().build()).build()` 在 `AnthropicClient.java:72-75` 构造；`paramsBuilder.system(MessageCreateParams.System.ofTextBlockParams(List.of(systemBlock)))` 在 `AnthropicClient.java:81`。
- [ ] `markLastUserTailForCache(messageParams)` 在 `AnthropicClient.java:323-359` 实现：倒序找最后一条 user MessageParam；string content 上转 block 列表带 marker；block 列表对末块 `text()` / `toolResult()` 用 `toBuilder().cacheControl(...).build()` 重建。
- [ ] `messageParams = buildMessages(conv.getMessages())` 后立刻调 `markLastUserTailForCache(messageParams)`（`AnthropicClient.java:76-77`）。
- [ ] `buildTool(schema, markCache)` 在 `AnthropicClient.java:294-310` 实现：`markCache=true` 时给 builder 加 `cacheControl(CacheControlEphemeral.builder().build())`；只有 tools 末项的调用传 `markCache=true`。

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `grep -rn "ContextCompactor\." src/main/java | grep -v "compact/"` 至少 3 处非测试调用方：
  - `src/main/java/com/codebot/agent/Agent.java`（`ContextCompactor.manage` 每轮 loop 调用）
  - `src/main/java/com/codebot/agent/Agent.java`（`ContextCompactor.forceCompact` 反应式恢复）
  - `src/main/java/com/codebot/tui/CodeBotModel.java`（`/compact` 命令调 `ContextCompactor.forceCompact`）
- [ ] `grep -rn "RecoveryState\b\|recoveryState" src/main/java | grep -v "compact/RecoveryState.java"` 至少 5 处：
  - `agent/Agent.java`（字段声明 + 三个 getter + `manage` / `forceCompact` / `StreamingExecutor` 传参共 5 处）
  - `agent/StreamingExecutor.java`（构造重载 + 字段 + `snapshotForRecovery`）
  - `skill/SkillExecutor.java`（`recordSkillInvocation` 两处）
  - `tui/CodeBotModel.java`（`/compact` 调 `agent.getRecoveryState()`）
- [ ] `grep -rn "ToolResultBudget\|ReplacementRecordsIO\|ContentReplacementState\|ContentReplacementRecord\|ContentReplacementLifecycle" src/main/java | grep -v "toolresult/"` 至少 5 处非测试调用方：
  - `Agent.java:16-18`（import）
  - `Agent.java:51-54`（`replacementState` 字段 + getter/setter）
  - `Agent.java:159-162`（`ToolResultBudget.apply` + `ReplacementRecordsIO.append`）
  - `subagent/AgentTool.java:105`（`parentReplacementState` 字段）
  - `subagent/AgentTool.java:107-108`（`setParentReplacementState` setter）
  - `subagent/AgentTool.java:292`（fork 调 `spawnSubAgent(..., parentReplacementState)`）
  - `subagent/SubAgentTaskManager.java:124-137`（`spawnSubAgent` 6-arg overload）
- [ ] 调用入口位于 `agent` 模块主循环（`Agent.java:159` 在 `agentLoop` 的 `for (int iteration = 1; ; iteration++)` 内、`client.stream` 调用之前）。
- [ ] 用户输入到本模块的路径可一句话描述:
  - 自动: agent loop 进入新一轮 → `ContextCompactor.manage` 按 ratio 升档跑 Snip / Microcompact / Collapse / Auto-compact 之一 → 非空 status 经 `CompactEvent` 推到事件队列 → 各种 reminder 写入 conv → `ToolResultBudget.apply` 产出 apiConv → `client.stream(apiConv, ...)`。
  - 反应式: LLM 流返回 `context` / `too long` / `prompt` 类错误 → `agentLoop` 错误恢复分支 → `RetryEvent("Context too long, compacting...", 0)` → `ContextCompactor.forceCompact` → `continue` 重试。
  - Fork: 父 Agent 调 Agent 工具 → `AgentTool.runFork` → `SubAgentTaskManager.spawnSubAgent(..., parentReplacementState)` → 子 Agent 用 `parentState.copy()` 独立演化。
- [ ] **死代码核查**：
  - `ContextCompactor.applyToolResultBudget`（仅单条 spill 的旧方法）：`grep` 不到非测试调用方，可继续保留作过渡期兼容 API，但实际工作走 `ToolResultBudget.apply`。
  - `ToolResultBudget` 公开 API 在 `Agent.agentLoop` 与 fork 路径中被引用。
  - `estimateTokens` 虽未被外部包直接调用，但仍是 package 内 `manage / autoCompact / contextCollapse` 调用所必需，非死代码。
  - `AgentTool.setParentConversation`：当前 codebot-java 主流程不调用 fork，该 setter 与 `parentReplacementState` 是预接入 API，等 fork 接入主线后自动生效。

## 3. 编译与测试

- [ ] `./gradlew compileJava --no-daemon` 通过（只允许 deprecation / unchecked 警告）。
- [ ] `./gradlew test --tests "com.codebot.toolresult.*" --no-daemon` 通过，覆盖 11 个用例：
  - `ContentReplacementStateTest`: `newReturnsEmpty / copyIsIndependent` 2 case。
  - `ToolResultBudgetTest`: `applyDoesNotMutateConv / firstCallFreezesUnreplaced / replacementByteIdentical / frozenNeverReplaced / aggregateOnlyPicksFresh / reconstructFromRecords / reconstructWithInheritedParent` 7 case。
  - `ReplacementRecordsIOTest`: `appendAndLoadRoundtrip / loadMissingFile` 2 case。
- [ ] `./gradlew test --tests "com.codebot.compact.*" --no-daemon` 通过，含 `RecoveryAttachmentTest` 5 个用例：`emptyWhenNothingRecorded / emitsAllSections / fileLimitAndNewestFirst / truncatesPerFile / skillBudget`。

## 4. 端到端验证

- [ ] Layer 1 字节稳定性：制造一轮内并行调多个 Bash、累计 > 20K 字符的会话（触发 Pass 2）；`ToolResultBudget.apply` 返回的 `apiConv` 里相关 tool_result content 变成 `[Result of ... chars saved to ...]`；下一轮再调一次，同一 `toolUseId` 的 content 与上一轮完全相等。
- [ ] Layer 1 不 mutate 原 conv：`applyDoesNotMutateConv` 守住；调 `apply` 前后 `conv.getMessages()` 各 `ToolResultBlock.content` 完全相等。
- [ ] Layer 1 frozen 不再替换：`frozenNeverReplaced` 验证「第一轮未替换的 id 在后续轮即使聚合超限也不被选中」。
- [ ] Layer 2 4 级升档：制造长对话（连续 Bash 大输出），事件流按 token 占比依次出现 `CompactEvent("Snipped verbose tool results")` → `CompactEvent("Microcompacted: saved ~K chars from old tool results")` → `CompactEvent("Context collapsed: N -> M estimated tokens (kept 10 recent turns)")` → `CompactEvent("Compacted: N -> M estimated tokens")`。
- [ ] Spill 落盘：长 Bash 输出后 `<sessionDir>/tool_results/` 目录下出现以 `toolUseId` 命名的文件。
- [ ] Transcript 落盘：`<sessionDir>/replacement_records.jsonl` 出现新条目，`jq .` 可解析。
- [ ] Fork 隔离（API 层）：通过单元测试或手动调用确认 `subAgent.setReplacementState(parent.copy())` 后子端 mutate 不影响父端。fork 主流程在 Java 当前版本上未接入，等接入后该路径自然生效。
- [ ] 反应式: LLM 返回含 `context` / `too long` / `prompt` 的错误时 Agent 自动调 `forceCompact` 并通过 `RetryEvent("Context too long, compacting...", 0)` 通知用户后重试。
- [ ] 恢复块文件段：先 ReadFile 两个不同路径再触发 `/compact`，摘要消息出现 `## Recently read files` 段、两个 `### <绝对路径>` 子段，每段内容用 ``` 包住。
- [ ] 恢复块技能段：先触发 skill 调用再 `/compact`，摘要消息出现 `## Active skills` 段并包含 skill 名 + SOP 片段。
- [ ] 恢复块工具段：摘要消息出现 `## Available tools` 段，并把当前 registry 里的工具按 `- 名字 — 描述首行` 列出。
- [ ] 恢复块收尾提示：摘要消息以 `## Note` 段收尾。
- [ ] 限额硬上限：人造 6+ 个 ReadFile 后压缩，恢复块只列最近 5 个；任一 5K token 以上的文件出现 `(content truncated)` 标记。

## 5. 文档

- [ ] spec.md / tasks.md / checklist.md 三件套齐全且最新（位于 `docs/java/ch08/`）。
- [ ] 跨分支设计文档存在：`docs/extras/content-replacement-state.md` 描述 ContentReplacementState 三分支统一设计与 Design B（不 mutate）契约。
- [ ] commit 信息标注 `ch08` 与三件套关闭状态。

````



## ch09

```markdown
# 我的初步想法
- 项目指令文件：在项目根目录放一份手写的 Markdown，记录技术栈、编码规范、注意事项，新会话启动时自动读取并作为独立消息注入对话开头
- 指令文件支持多层优先级（项目级 > 用户级），高优先级排在前面让 LLM 优先遵循；支持 `@include` 模块化引用其他文件，但要限制嵌套深度、拦截跳出项目目录的路径
- 会话存档用 JSONL 追加写入：追加 O(1)、崩溃只丢最后一行不完整数据、恢复时坏行可跳过；每个会话另存一份小的 meta 文件存 ID/标题/摘要/消息数等概要，方便会话列表展示时不用扫整个 JSONL
- 会话恢复要处理几类异常：解析失败的行跳过继续、`tool_use` 没配上 `tool_result` 时截断到最后完整位置、token 超限先触发一次压缩、距上次活跃超过一定时长时插入时间跨度提醒
- 自动笔记：每隔几轮对话或应用退出时异步调一次 LLM，让它读当前笔记和最近对话，按固定几类（用户偏好、纠正反馈、项目知识、参考资料）更新笔记文件；去重交给 LLM 判断，不自己实现相似度算法
- 用户级和项目级笔记分开存储（用户偏好/纠正反馈进用户级目录，项目知识/参考资料进项目级目录），提供命令让用户查看、清空、定位编辑笔记和会话
```

### Go

```markdown
# ch09: 记忆系统 Spec

## 1. 背景

只活在单轮对话里的 Agent 每次启动都是一张白纸：用户偏好得反复说、项目背景得反复讲、上次的会话内容拿不回来。终端 Agent 用三种正交的「记忆」机制组合解决这三个问题：项目级指令文件（CODEBOT.md / AGENTS.md）、会话存档（jsonl 落盘可恢复）、自动记忆（Agent 自己往专用目录写小段经验，loop 收尾自动提取、用户消息按相关性召回）。本章把这三套机制全部落到 CodeBot。

## 2. 目标

交付三类相互独立的持久化机制，给 TUI 启动与运行期共同使用：项目指令加载（拼装 CODEBOT.md / AGENTS.md 到系统提示）、会话存档（消息追加写 jsonl、`/resume` 列表与恢复、输入历史回溯）、自动记忆（Agent 用 Write 工具自行管理 memory 目录，由系统提示自动加载回上下文；每轮 loop 结束 fork 一个 extractor 自动抽取要记的事；用户消息时按相关性召回若干记忆并附老化提示）。最终 TUI 启动时三套机制都开箱可用，不需要上层模块改入口。

## 3. 功能需求

### 项目指令

- F1: `DiscoverInstructions` 按四层来源拼装（用户全局 `~/.codebot/CODEBOT.md` 与 `~/.codebot/AGENTS.md` → git root 到 cwd 每层的 `CODEBOT.md` 与 `AGENTS.md` → 项目 legacy `.codebot/INSTRUCTIONS.md` → 私有 `CODEBOT.local.md`），同一绝对路径不重复加载。
- F2: 内容中 `@./path`、`@~/home`、`@/abs` 三种 include 语法被 `expandIncludes` 递归解析，跳过围栏代码块；include 失败时把原行写回，不中断加载。
- F3: 多个 source 拼成带「Contents of … / 分隔线」的整段，喂给 system prompt 的项目指令区。

### 会话存档

- F4: `NewID` 生成 `YYYYMMDD-HHMMSS-xxxx` 格式 session id（时间戳 + 4 字符十六进制随机后缀，避免同秒并发场景下的 ID 冲突）；`SaveMessage` 把每条消息以 JSON 行追加到会话 jsonl；`LoadSession` 反序列化为消息数组，content 为空的行跳过。
- F5: `ListSessions` 扫描所有会话文件，给每个会话提取首条 user 消息作为标题、统计消息数 / 文件大小 / 当前 git 分支 / mtime，按 mtime 倒序排。
- F6: `MatchesSearch` 在首消息与 id 上做小写 contains 匹配，供 TUI `/resume` 搜索。
- F7: `FormatRelativeTime` / `FormatFileSize` 把时间和字节数渲染成人类可读字符串。

### 提示历史

- F8: `history.Append` 把输入文本追加到提示历史文件，相邻重复条目去重,总条数有上限封顶。
- F9: `history.Load` 按写入顺序返回全部条目，供 TUI 输入框方向键回溯。

### 自动记忆

- F10: 双路目录：`GetAutoMemPath` 返回 `<projectRoot>/.codebot/memory/`（项目级，存 type=project / reference 的记忆），`GetUserAutoMemPath` 返回 `~/.codebot/memory/`（用户级，存 type=user / feedback 的记忆，跨项目共享）。两个函数都返回带末尾分隔符的绝对路径，避免前缀误匹配；`CODEBOT_REMOTE_MEMORY_DIR` 环境变量可覆盖项目级到外部目录。
- F11: `Manager` 同时持有两个目录（`userMemDir / memDir`），提供完整生命周期：构造、查项目级 / 用户级目录、查两个 entrypoint、构建合并的系统提示段、列举两边 memories、加载全部、清空。
- F12: `MemoryType` 闭枚举（用户偏好 / 反馈 / 项目知识 / 引用资料），`ParseMemoryType` 对未知值不崩。type 字段同时决定记忆文件落在哪个目录（user/feedback → 用户级；project/reference → 项目级）。
- F13: `LoadAll` 扫描两个 memory 目录的 markdown 文件（排除 MEMORY.md 索引），解析 frontmatter（name / description / type），用户级在前、项目级在后拼接，每个目录内部按文件名升序排列。
- F14: `BuildMemoryPrompt(displayName, userMemDir, projectMemDir)` 拼接行为提示（含类型分类与其 scope、不该写什么、怎么写、recall 漂移警告）+ 两节 MEMORY.md 索引（`## User-level MEMORY.md (path)` 与 `## Project-level MEMORY.md (path)`），任一目录为空时该节自动省略。
- F15: `TruncateEntrypointContent` 对索引正文做行数与字节两层截断（先按行裁剪，再按字节截到最近换行），在末尾追加警告说明截断原因。两个 MEMORY.md 各自独立截断到 200 行 / 25KB。
- F16: `LoadAutoMemoryPrompt(projectRoot)` 是 TUI 调的便捷入口：内部解析 user + project 两个目录，lazy 确保两边都存在 → 返回合并的 auto memory 段。`$HOME` 未设置时 user 段自动 skip。
- F17: `IsAutoMemPath(absolutePath, projectRoot)` 判断绝对路径是否落在 **任一个** memory 目录（项目级 OR 用户级），给 path 沙箱用，使 Write 工具能合法写到任一目录。另有 `IsUserAutoMemPath` 单独识别用户级的判定函数，用于按路径反推 scope 的场景。
- F18: `GetMemories` / `Clear` 给 `/memory list` 与 `/memory clear` 命令用，操作作用于两个目录的合集。

### 自动提取与召回

- F19: 自动提取（extractMemories）——每轮主 agent loop 结束（`LoopComplete` 事件）后，fire-and-forget 启动一个 fork 出来的 extraction subagent。subagent 读最近 N 条消息，判断有没有「值得记的事」（user/feedback/project/reference 四类之一），按 type 路由：user/feedback 主动 Write 到 `~/.codebot/memory/<topic>.md` + 更新用户级 `MEMORY.md`；project/reference 写到 `<projectRoot>/.codebot/memory/<topic>.md` + 更新项目级 `MEMORY.md`。Extraction prompt 里有专门的 `## Memory storage paths` 段把两个目录的绝对路径喂给 LLM。
- F20: 互斥跳过（hasMemoryWritesSince）——当主 agent 自己已经在本轮内写过 memory 文件，extractor 跳过本次提取并把 cursor 推到末尾，避免与主 agent 重复劳动。判定走 `IsAutoMemPath`，覆盖两个目录任一。
- F21: 路径沙箱限制——fork 出来的 extractor 用独立的 `PathSandbox(MemoryDir, UserMemoryDir)`，只允许 Read/Write/Edit 落在两个 memory 目录下（user-level 与 project-level）。比参考的 `createAutoMemCanUseTool` 更严格（参考让 Read/Grep/Glob 漫游），换取防御深度。
- F22: 相关性召回（findRelevantMemories）——用户发消息时，TUI 调用 selector LLM（独立 system prompt 的 side-query client），把 memory 清单（合并两个目录的 frontmatter manifest）+ 用户 query 喂进去，让模型选 ≤5 条最相关的记忆。被选中的 memory 文件正文 + 老化提示拼成 system-reminder 注入到 conversation，跟随用户消息一起发给主 LLM。
- F23: 老化提示（memoryAge）——相关记忆注入时，给每条记忆带上 "saved today / yesterday / N days ago" 表头；超过 1 天的记忆额外附 "This memory is N days old. Memories are point-in-time observations..." 警告，防止模型把陈旧的 file:line 引用当作 ground truth。
- F24: 扫描原语（memoryScan）——F19 和 F22 共享一套扫描。`ScanMemoryFiles(ctx, memoryDir, scope)` 递归读单个目录的 `*.md` frontmatter（除 MEMORY.md 之外），按 mtime 倒序，上限 200，每条 header 带 `Scope` 字段（"user" / "project"）；调用方扫两次合并。`FormatMemoryManifest` 输出每条加 `[<scope>-scope] [type] <FilePath>` 前缀，让 LLM 一眼能看出该记忆来自哪个目录、对应路径要写到哪。
- F25: Agent 钩子（OnLoopComplete）——`agent.Agent` 加一个 `OnLoopComplete func(*conversation.Manager)` 字段，在 `LoopComplete` 事件之后 fire-and-forget 调用。替代参考的 原始定义 分发器。
- F26: Drain on quit——TUI 退出路径调 `extractor.Drain(timeoutMs)`，等待所有 in-flight 提取完成，避免主程序退出时杀掉正在跑的 extractor。

## 4. 非功能需求

- N1: 加载失败一律静默：找不到文件、解析失败、目录不存在都返回空，不中断 TUI 启动。
- N2: instruction include 必须循环安全（绝对路径去重）。
- N3: 会话 jsonl 单行可能很大，扫描时使用足够大的行缓冲，避免触发默认 scanner 上限。
- N4: 提示历史写入后重写整个文件，避免无限增长。
- N5: memory 目录必须 lazy 创建，不能在 init 阶段强制创建以污染只读项目。
- N6: memory 路径带末尾分隔符，确保 path-prefix 沙箱判断不会把相邻目录误判为属于 memory。
- N7: 所有公开符号都被外部模块调用，无死代码。

## 5. 设计概要

### 核心数据结构

- `memory.Manager`：项目根 + 项目级 memory 目录 + 用户级 memory 目录
- `memory.MemoryFile`：path / name / description / type，frontmatter 解析结果
- `memory.MemoryType`：4 个常量值的字符串枚举
- `memory.EntrypointTruncation`：截断后内容 + 行数 + 字节数 + 两个 was-truncated 标志
- `memory.InstructionSource`：path + content
- `memory.MemoryHeader`：扫描原语的轻量结果（path + scope + mtime + frontmatter）
- `memory.RelevantMemory`：召回选中的记忆元数据
- `extractor.Extractor`：extraction 协调器（内含 cursor / inProgress / WaitGroup）
- `session.Message`：role / content / ts，jsonl 行结构
- `session.SessionInfo`：会话元数据
- `history.entry`（未导出）：text + ts

### 主流程

- 项目指令：用户输入 → TUI `loadCustomInstructions` → `memory.LoadInstructions` → `DiscoverInstructions` 四层 + 去重 → `expandIncludes` 递归 → 拼接段 → 喂给 `prompt.BuildSystemPrompt` 的 `CustomInstructions` 入口。
- 会话：TUI 启动调 `session.NewID` 拿新 id，`session.ListSessions` 供 `/resume`；每次用户/助手消息往返调 `session.SaveMessage`，同时 `history.Append` 入提示历史；`/resume <id>` → `session.LoadSession` → 回灌进 conversation manager。
- 自动记忆：TUI 启动调 `memory.NewManager` 存到自身（内部同时解析项目级 + 用户级两个目录），并向 path 沙箱注册 `GetAutoMemPath` 与 `GetUserAutoMemPath` 作为额外可写区；系统提示构建时调 `LoadAutoMemoryPrompt` 注入合并的 memory section；Agent 用 Write 工具按 type 写到对应目录的 `.md` 与索引；`/memory list/clear` 走 Manager 对应方法，操作覆盖两个目录的合集。
- 自动提取：用户消息收尾 → `agent.Run` 进入 `LoopComplete` → 触发 `OnLoopComplete` → `extractor.Execute(ctx)` → 检查 inProgress / hasMemoryWritesSince / throttle → 构造 forked conversation + extraction prompt → spawn forked `agent.New`（MaxIterations=5, 严格沙箱）→ 收集 written paths → `Deps.AppendSystem("Memory saved: ...")` 推回 TUI。
- 相关性召回：TUI `chat` 收到用户消息 → `m.conversation.AddUserMessage(expanded)` → `prefetchRelevantMemories(query)` 返回 channel → 启动 selector goroutine（独立 `llm.NewClient(provider, SelectMemoriesSystemPrompt)`、扫 manifest、问 selector 选 ≤5 个、返回 JSON）→ 主流程 `collectPrefetchedRecall(conv, ch, 3s)` 同步等结果或超时 → 拿到结果时 `AddSystemReminder(renderRelevantMemoriesReminder(...))` 注入 → 启动 `m.ag.Run`。
- 退出：用户按 Ctrl-D / Esc → `tea.Quit` 之前调 `m.memoryExtractor.Drain(5000)` → wait 所有 in-flight extraction 或超时。

### 调用链（模块层级）

- 启动：TUI → `memory.LoadInstructions` / `memory.NewManager` / `memory.LoadAutoMemoryPrompt` / `memory.GetAutoMemPath` + `memory.GetUserAutoMemPath`（沙箱注册）/ `session.NewID` / `extractor.InitExtractMemories`（Deps 同时带 `MemoryDir` + `UserMemoryDir`）
- 运行：TUI ↔ `session`（SaveMessage / LoadSession / ListSessions）+ `history`（Append / Load）+ `memory`（GetMemories / Clear / FindRelevantMemories / ScanMemoryFiles）+ `extractor`（Execute via OnLoopComplete）

### 与其他模块的交互

- `internal/prompt`：通过 BuildOptions 接收 CustomInstructions 与 MemorySection
- `internal/permissions`：path 沙箱接受 `memory.GetAutoMemPath` 与 `memory.GetUserAutoMemPath` 两个目录为额外可写路径；extractor 内部独立挂 `PathSandbox(memoryDir, userMemoryDir)`
- `internal/tui`：三套机制的统一入口；安装 extractor、注入召回 reminder、退出 drain
- `internal/conversation`：会话恢复时把消息灌回 Manager；extractor fork 一份独立 Manager
- `internal/agent`：暴露 `OnLoopComplete` 字段供 extractor 挂钩

### 新增文件 / 函数清单

新增（全部位于 `/Users/codemelo/codebot/internal/`）：

- `memory/memory_age.go`：`MemoryAgeDays / MemoryAge / MemoryFreshnessText / MemoryFreshnessNote`
- `memory/memory_scan.go`：`MemoryHeader / MaxMemoryFiles / FrontmatterMaxLines / ScanMemoryFiles / FormatMemoryManifest`
- `memory/find_relevant_memories.go`：`RelevantMemory / SelectorFn / SelectMemoriesSystemPrompt / FindRelevantMemories`
- `memory/extractor/prompts.go`：`BuildExtractAutoOnlyPrompt`
- `memory/extractor/extractor.go`：`Deps / Extractor / InitExtractMemories / (*Extractor).Execute / (*Extractor).Drain`

修改：

- `agent/agent.go`：`Agent.OnLoopComplete` 字段 + LoopComplete 之后 fire-and-forget 调用。
- `tui/tui.go`：
 - 新字段 `Model.memoryExtractor *extractor.Extractor`
 - 两处 agent 构造点接 `installMemoryExtractor(ag, wd, p.Protocol)`
 - 两处 user-message 提交点接 `prefetchRelevantMemories` + `collectPrefetchedRecall`
 - quit 路径调 `memoryExtractor.Drain(5000)`
 - 新 helper：`installMemoryExtractor / prefetchRelevantMemories / collectPrefetchedRecall / renderRelevantMemoriesReminder`

## 6. Out of Scope

- 会话过期清理
- 团队 / 远程 memory 同步：只做单目录个人模式
- Memory 遥测（老化提示通过 freshness header 注入相关记忆，遥测仍不做）
- CODEBOT.md 大文件警告：靠模型上下文窗口托底，不做大小校验
- Session jsonl 的 schema 版本化与迁移
- 团队记忆的提取分流

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。


```

```markdown
# ch09: 记忆系统 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。所有 T 任务标记 [x]，每条任务记录实际落地的文件与行号。

## T1: 项目指令的优先级栈与发现逻辑
- 影响文件: `/Users/codemelo/codebot/internal/memory/instructions.go:49-78`（`DiscoverInstructions / add`、`projectInstructionDirs`、`findGitRoot`）
- 依赖任务: 无
- 完成标准: `DiscoverInstructions` 按四层优先级（`~/.codebot` → git root → workDir → `.codebot/INSTRUCTIONS.md` → `CODEBOT.local.md`）追加；同 abs path 通过 `seen` 去重；测试 `TestDiscoverInstructionsLayers` 覆盖。

## T2: `@-include` 语法解析（递归 + 循环防御 + 跳过 code fence）
- 影响文件: `/Users/codemelo/codebot/internal/memory/instructions.go:80-149`（`expandIncludes / parseInclude / resolveInclude`）
- 依赖任务: T1
- 完成标准: 支持 `@./`、`@../`、`@~/`、`@/abs` 四种前缀；`@@xxx` 与 `@username` 不命中；fenced code block 内的 `@xxx` 不展开；嵌套 include 通过 `seen` 防循环；resolved 失败时把原行写回。

## T3: 拼接 `LoadInstructions` 输出
- 影响文件: `/Users/codemelo/codebot/internal/memory/instructions.go:31-45`
- 依赖任务: T2
- 完成标准: 多个 source 以 `Contents of <label>:\n\n<content>\n\n---\n\n` 拼接，label 相对 `workDir` 优先，落入 workDir 内用相对路径。

## T4: 会话 jsonl 持久化
- 影响文件: `/Users/codemelo/codebot/internal/session/session.go:32-79`（`NewID / sessionsDir / sessionFilePath / SaveMessage / LoadSession`）
- 依赖任务: 无
- 完成标准: `NewID` 用 `20060102-150405` 时间格式 + `-xxxx` 四位十六进制随机后缀（`crypto/rand.Read(b[:])` + `hex.EncodeToString`，纳秒兜底），返回 20 字符的 `YYYYMMDD-HHMMSS-xxxx` 字符串避免同秒冲突；`SaveMessage` 追加 `{role, content, ts}` jsonl；`LoadSession` 用 1MB buffer 的 scanner，content 为空的行跳过。

## T5: 会话列表与搜索
- 影响文件: `/Users/codemelo/codebot/internal/session/session.go:77-188`
- 依赖任务: T4
- 完成标准: `ListSessions` 给每个 jsonl 文件构造 `SessionInfo`（含 git branch、首条 user msg、消息数、大小、mtime），按 mtime 倒序；`MatchesSearch` 在 ID 和首条消息上做小写 contains；`FormatRelativeTime / FormatFileSize` 给 TUI 渲染用。

## T6: 提示历史
- 影响文件: `/Users/codemelo/codebot/internal/history/history.go:11-69`
- 依赖任务: 无
- 完成标准: `Append` 相邻去重 + cap 200 条 + 重写整个文件；`Load` 用 jsonl scanner 返回有序 `[]string`。

## T7: Memory 路径与沙箱辅助（双路）
- 影响文件: `/Users/codemelo/codebot/internal/memory/paths.go`
- 依赖任务: 无
- 完成标准:
 - 项目级：`GetAutoMemPath(projectRoot)` 返回 `<projectRoot>/.codebot/memory/`（带尾分隔符），`CODEBOT_REMOTE_MEMORY_DIR` 环境变量可覆盖；`GetAutoMemEntrypoint` 返回 `<projectMemDir>/MEMORY.md`。
 - 用户级：`GetUserAutoMemPath()` 返回 `~/.codebot/memory/`（带尾分隔符），`$HOME` 未设置时返回空串；`GetUserAutoMemEntrypoint()` 返回 `<userMemDir>/MEMORY.md`。
 - 沙箱判定：`IsAutoMemPath(absolutePath, projectRoot)` 用 `abs + sep` 前缀判定，**项目级 OR 用户级任一命中即合法**；另有 `IsUserAutoMemPath(absolutePath)` 只判定用户级，用于按路径反推 scope 的场景。

## T8: Memory 类型与行为提示文本
- 影响文件: `/Users/codemelo/codebot/internal/memory/memory_types.go`
- 依赖任务: 无
- 完成标准:
 - `MemoryType` 闭枚举 4 种（user / feedback / project / reference）；`ParseMemoryType` 返回 `(value, ok)`。
 - 主用 `TypesSectionDualPath`：每个 `<type>` 块带 `<scope>` 标签明确该 type 落在哪个目录（user / feedback → `~/.codebot/memory/`；project / reference → `<projectRoot>/.codebot/memory/`），examples 里也带"saves … memory to ~/.codebot/memory/"或"to ./.codebot/memory/"的路径暗示。
 - 保留 `TypesSectionIndividual` 不删，用于一些不区分 scope 的旧路径或外部引用，避免破坏向后兼容。
 - `WhatNotToSaveSection / MemoryDriftCaveat / WhenToAccessSection / TrustingRecallSection / MemoryFrontmatterExample` 文本与目标实现一一对应。

## T9: Memory 目录与 MEMORY.md 截断（双路签名）
- 影响文件: `/Users/codemelo/codebot/internal/memory/memdir.go`
- 依赖任务: T7, T8
- 完成标准:
 - 常量 `MaxEntrypointLines=200 / MaxEntrypointBytes=25000`，对每个目录的 `MEMORY.md` 独立生效。
 - `TruncateEntrypointContent` 同时做行/字节双层截断并追加 `> WARNING:` 文本；`EnsureMemoryDirExists` 幂等。
 - `BuildMemoryLines(displayName, userMemDir, projectMemDir)` 接受双目录签名：开篇用两段 bullet 描述两个 scope 的用途和绝对路径，正文用 `TypesSectionDualPath`；任一目录为空时该 bullet / scope 自动省略。
 - `BuildMemoryPrompt(displayName, userMemDir, projectMemDir)` 在行为提示后，分别输出 `## User-level MEMORY.md (path)` 和 `## Project-level MEMORY.md (path)` 两节内容（空时各自显示 placeholder）；用 `writeEntrypointSection` 助手避免重复。
 - `LoadAutoMemoryPrompt(projectRoot)` 是 TUI 入口：内部解析 user + project 两个目录，两边都 lazy 创建后返回合并段；任一为空时正常 skip。

## T10: Memory Manager（list / clear / frontmatter 解析，双目录）
- 影响文件: `/Users/codemelo/codebot/internal/memory/memory.go`
- 依赖任务: T9
- 完成标准:
 - `Manager` 持有 `projectRoot / userMemDir / memDir` 三个字段；`NewManager(projectRoot)` 内部同时调 `GetAutoMemPath` 和 `GetUserAutoMemPath` 计算两个目录；提供 `Dir()` 返回项目级、`UserDir()` 返回用户级、`EntrypointPath()` / `UserEntrypointPath()` 分别返回两个 `MEMORY.md`。
 - `BuildSystemReminder` 两边都 lazy mkdir，转调 `BuildMemoryPrompt(displayName, userMemDir, memDir)`。
 - `GetMemories` 输出 `[type] name — description`；`LoadAll` 扫两个目录的 `*.md`（跳过 `MEMORY.md` 与非 `.md` 文件），用户级在前、项目级在后，每个目录内部按文件名升序，抽出 `loadDir(dir)` 助手；`Clear` 清两个目录，抽出 `clearDir(dir)` 助手。
 - `parseFrontmatter` 用 `frontmatterRe = (?s)\A---\s*\n(.*?)\n---\s*\n` 提取 `name / description / type`。

## T11: 接入主流程（TUI 启动、消息往返、命令）
- 影响文件: `/Users/codemelo/codebot/internal/tui/tui.go`（约 20 个改动点，行号会随版本漂移）
- 依赖任务: T3, T5, T6, T10
- 完成标准:
 - 启动时拉起 memory manager（`m.memoryMgr = memory.NewManager(wd)`）；path sandbox 两处构造点都通过 `extraAllowed = []string{memory.GetAutoMemPath(wd)}`，若 `memory.GetUserAutoMemPath()` 非空再 append 进去。
 - 系统提示注入 `MemorySection: memory.LoadAutoMemoryPrompt(wd)`（内部已合并双目录）与 `CustomInstructions: memory.LoadInstructions(wd)`。
 - 用户消息进入时 `history.Append` + `session.SaveMessage` 双写；助手消息收尾时同样 `SaveMessage`。
 - 启动新会话 / 重启时 `session.NewID` + `history.Load`。
 - `/resume` 命令调 `ListSessions / LoadSession / MatchesSearch / FormatRelativeTime / FormatFileSize`。
 - `/memory list` / `/memory clear` 命令通过 `m.memoryMgr.GetMemories` / `Clear`（操作覆盖双目录合集）。

## T12: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T11
- 完成标准:
 - `go test ./internal/memory/ ./internal/session/ ./internal/history/ -v` 全部通过。
 - 项目根放一个 CODEBOT.md（如本仓库），TUI 启动后系统提示里能看到「Contents of CODEBOT.md」段。
 - TUI 发几条消息后退出，再启动 `/resume`，能列出会话并恢复；上方向键能调出之前输入的提示。
 - Agent 在某次会话里用 Write 工具写一个 memory 文件（`<memDir>/foo.md`）+ 在 `MEMORY.md` 追加索引，下次启动系统提示能看到 MEMORY.md 内容；`/memory list` 列出该条；`/memory clear` 之后再启动看到 placeholder。

## T13: memory_age + memory_scan 基础原语（含 scope 标签）
- 影响文件:
 - `/Users/codemelo/codebot/internal/memory/memory_age.go`（4 个函数）
 - `/Users/codemelo/codebot/internal/memory/memory_scan.go`（`MemoryHeader / ScanMemoryFiles / FormatMemoryManifest` + 常量）
 - 配套 `memory_age_test.go` / `memory_scan_test.go`
- 依赖任务: 无
- 完成标准:
 - `MemoryAgeDays / MemoryAge / MemoryFreshnessText / MemoryFreshnessNote` 行为逐项校验（today / yesterday / N days ago / 钳位负数 / ≤1 天不警告 / system-reminder 包裹）
 - `MemoryHeader` 含 `Filename / FilePath / Scope / MtimeMs / Description / Type` 六个字段；`Scope` 取值为 "user" / "project"，调用方传入；测试用空串兼容旧 API。
 - `ScanMemoryFiles(ctx, memoryDir, scope)` 三参数签名：递归扫描、并行读 frontmatter、按 mtime 倒序、上限 `MaxMemoryFiles=200`、排除 `MEMORY.md`；遍历时把 scope 写入每个 header。调用方扫两次（user + project）后合并。
 - `FormatMemoryManifest` 输出 `- [<scope>-scope] [type] <FilePath> (ISO8601 毫秒): description`：scope 非空时加 `[<scope>-scope]` 前缀；type 非空时加 `[type]` 前缀；filename 改用绝对路径 `FilePath`（fallback Filename），让 LLM 一眼看出该记忆来自哪个目录、要写到哪。

## T14: findRelevantMemories 召回（双目录） + SelectorFn 回调
- 影响文件: `/Users/codemelo/codebot/internal/memory/find_relevant_memories.go` + `_test.go`
- 依赖任务: T13
- 完成标准:
 - `RelevantMemory{Path, MtimeMs}` 类型一致。
 - `SelectMemoriesSystemPrompt` 文本与参考一字一致 + 追加 `Respond with valid JSON only, no markdown, in this exact shape: {"selected_memories": [...]}` 补偿 output_format 缺口。
 - `FindRelevantMemories(ctx, query, userMemDir, projectMemDir, recentTools, alreadySurfaced, selector)` 双目录签名：任一为空都允许（只扫非空那一半）；用 `ScanMemoryFiles(..., "user")` 和 `ScanMemoryFiles(..., "project")` 分别扫，结果合并；selector 返回的 key 用 FilePath 优先、Filename 兜底（解决跨目录同名记忆的歧义）。
 - selector 失败、ctx 取消、JSON 解析失败一律返回空 slice + nil error（best-effort）。
 - `extractJSONObject` 容忍 markdown 代码栅栏包裹。

## T15: agent.OnLoopComplete 钩子
- 影响文件:
 - `/Users/codemelo/codebot/internal/agent/agent.go`（新增字段 + 触发点）
 - `/Users/codemelo/codebot/internal/agent/agent_test.go`（新增 2 个 test）
- 依赖任务: 无
- 完成标准:
 - `Agent.OnLoopComplete func(conv *conversation.Manager)` 字段
 - 在 `len(toolCalls) == 0` 分支 `ch <- LoopComplete{}` 之后、`return` 之前 fire-and-forget `go a.OnLoopComplete(conv)`
 - 测试 `TestAgentOnLoopCompleteFiresOnFinalTurn` 验证 callback 收到对应 conv 指针
 - 测试 `TestAgentOnLoopCompleteSkippedOnError` 验证 MaxIterations 触发 ErrorEvent 时 callback 不调

## T16: extractor prompt（双路 routing）
- 影响文件: `/Users/codemelo/codebot/internal/memory/extractor/prompts.go` + `_test.go`（新包）
- 依赖任务: T15（不强依赖；并行可写）
- 完成标准:
 - `BuildExtractAutoOnlyPrompt(newMessageCount int, existingMemories string, skipIndex bool, userMemDir, projectMemDir string)` 五参数签名；末两个参数告诉 LLM 两个 scope 的绝对路径。
 - prompt 文本含 "memory extraction subagent" / "Available tools: ReadFile, Grep, Glob, read-only Bash" / `EditFile/WriteFile` / "MCP, Agent, write-capable Bash, etc — will be denied" / "Do not interleave reads and writes" 等关键 marker。
 - 新增 `## Memory storage paths` 段：列出 `user/feedback → userMemDir` 和 `project/reference → projectMemDir` 两条路由规则，明确"Never write a user/feedback memory into the project-level dir or vice versa"。
 - 主体类型说明用 `TypesSectionDualPath`（带 `<scope>` 标签）取代旧的 `TypesSectionIndividual`；examples 里的 "saves … memory to ~/.codebot/memory/" 路径暗示保留。
 - skipIndex=true 去掉 Step 2 / MEMORY.md 段。
 - existingMemories 非空时插入 `## Existing memory files` 段 + "update an existing file rather than creating a duplicate" 提示。
 - 仍不包含 team scope / "private or team" 字样（dual-path 模式只分 user / project，不引入 team scope）；`<scope>` 标签允许出现（dual-path 模式本身就用 scope 区分目录）。
 - `buildExtractCombinedPrompt`（Claude Code 的 team-memory 版）依然不抄。

## T17: extractor 主体（双目录沙箱 + manifest）
- 影响文件: `/Users/codemelo/codebot/internal/memory/extractor/extractor.go` + `_test.go`
- 依赖任务: T13, T14, T15, T16
- 完成标准:
 - `Deps` 结构含 `MemoryDir / UserMemoryDir / ProjectRoot / Client / ToolRegistry / Protocol / Conversation / AppendSystem / DebugLogf` 九个字段；`UserMemoryDir` 为空表示当前环境没有可用的用户级目录（`$HOME` 未设置），此时所有记忆类型都路由到项目级。
 - `Extractor` 结构含 `mu / inFlight / lastMemoryMessageIdx / hasLoggedGateFailure / inProgress / turnsSinceLastExtraction / pendingContext`。
 - `InitExtractMemories(deps Deps) *Extractor` 工厂。
 - `Execute(ctx)` 实现 inProgress coalescing（重入时 stash pendingContext 立即返回）。
 - `runExtraction(ctx, isTrailingRun)` 实现：cursor 推进 / hasMemoryWritesSince 跳过 / throttle / **分别扫两个目录**（`ScanMemoryFiles(ctx, MemoryDir, "project")` + `ScanMemoryFiles(ctx, UserMemoryDir, "user")`）合并 manifest / 调 `BuildExtractAutoOnlyPrompt(..., UserMemoryDir, MemoryDir)` 把双路径喂给 LLM / 构造 forked conv / 起 subAgent（`MaxIterations=5`，`PathSandbox(MemoryDir, UserMemoryDir)` 允许两个目录任一，`ModeBypass`）/ 收集 written paths / `AppendSystem("Memory saved: ...")` / finally 跑 trailing。
 - `Drain(timeoutMs)` 用 WaitGroup 等所有 in-flight, soft timeout 默认 60s。
 - 辅助函数：`buildExtractorConversation`（**不**加 ForkBoilerplate）、`countModelVisibleMessagesSince`、`hasMemoryWritesSince`、`extractWrittenPaths`、`getWrittenFilePath`。
 - end-to-end 测试：mock llm.Client 触发 WriteFile，验证文件落地（项目级和用户级路径都能命中沙箱）+ `AppendSystem` 通知 + cursor 推进。

## T18: TUI 接入（extractor + 召回 + drain，双目录路径）
- 影响文件: `/Users/codemelo/codebot/internal/tui/tui.go`
- 依赖任务: T13-T17
- 完成标准:
 - 新字段 `Model.memoryExtractor *extractor.Extractor`。
 - 两处 agent 构造点：path sandbox 通过 `extraAllowed = []string{memory.GetAutoMemPath(wd)}`，`memory.GetUserAutoMemPath()` 非空再 append；构造完后 `m.memoryExtractor = m.installMemoryExtractor(ag, wd, p.Protocol)`。
 - 两处 user-message 提交点 (`chat / sendPromptCommand`)：`prefetchCh := m.prefetchRelevantMemories(query)` + `collectPrefetchedRecall(m.conversation, prefetchCh, 3*time.Second)`。
 - quit 路径调 `m.memoryExtractor.Drain(5000)` 在 `tea.Quit` 之前。
 - 新 helper：
 - `installMemoryExtractor(ag, wd, protocol)` 构造 `Deps{ MemoryDir: memory.GetAutoMemPath(wd), UserMemoryDir: memory.GetUserAutoMemPath(), ... }` 并接 OnLoopComplete。
 - `prefetchRelevantMemories(query)` 起 goroutine，取出 `m.memoryMgr.Dir()` 和 `m.memoryMgr.UserDir()` 两个目录传给 `FindRelevantMemories`；selector 构造独立 side-query client（system prompt = `memory.SelectMemoriesSystemPrompt`），返回 reminder channel。
 - `collectPrefetchedRecall(conv, ch, timeout)` 同步等 channel 带 timeout，成功则 `AddSystemReminder`。
 - `renderRelevantMemoriesReminder(memories)` 读各文件、拼老化表头 + freshness warning + 文件内容。

## 进度
- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8
- [ ] T9
- [ ] T10
- [ ] T11
- [ ] T12
- [ ] T13
- [ ] T14
- [ ] T15
- [ ] T16
- [ ] T17
- [ ] T18


```

````markdown
# ch09: 记忆系统 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性

### 项目指令（memory.LoadInstructions）
- [ ] 函数 `LoadInstructions` 在 `/Users/codemelo/codebot/internal/memory/instructions.go:31-45` 实现，调用 `DiscoverInstructions` 后拼成 `Contents of X:\n\n<content>\n\n---\n\n` 段。
- [ ] 函数 `DiscoverInstructions` 在 `instructions.go:49-64` 实现，按 4 层优先级（`~/.codebot` → `projectInstructionDirs` → `.codebot/INSTRUCTIONS.md` → `CODEBOT.local.md`）调用 `add()`。
- [ ] 函数 `projectInstructionDirs` 在 `instructions.go:153-176` 实现，从 git root 走到 workDir 收集每一级（`findGitRoot` 不在 repo 时只返回 `[workDir]`）。
- [ ] 函数 `expandIncludes` 在 `instructions.go:80-114` 实现，支持 `@./`、`@../`、`@~/`、`@/abs`，跳过 ``` 围起来的代码块（`inCode` 标志）。
- [ ] 函数 `parseInclude` 在 `instructions.go:119-136` 实现，`@@xxx` 不命中，`@username` 类（非 path）不命中。
- [ ] 边界处理：include 失败时把原 `@xxx` 行写回（`instructions.go:106-108` 注释 + 落入主 fallthrough）。
- [ ] 边界处理：循环 include 用 `seen map[string]bool` 防御（`instructions.go:66-78, 97`）。

### 会话存档（session）
- [ ] 类型 `session.Message` 在 `/Users/codemelo/codebot/internal/session/session.go:15-19` 定义（`Role / Content / Ts`）。
- [ ] 类型 `session.SessionInfo` 在 `session.go:21-28` 定义（`ID / FirstMessage / MessageCount / FileSize / GitBranch / ModTime`）。
- [ ] 函数 `NewID` 在 `session.go:32-39` 实现：返回 `YYYYMMDD-HHMMSS-xxxx` 格式（`time.Now().Format("20060102-150405") + "-" + hex.EncodeToString(b[:])`，2 字节 `crypto/rand` → 4 字符十六进制后缀），并提供 `time.Now().UnixNano()&0xFFFF` 兜底；长度恒为 20。
- [ ] 函数 `SaveMessage` 在 `session.go:42-55` 实现：`O_APPEND | O_CREATE | O_WRONLY`，写一条 JSON + `\n`。
- [ ] 函数 `LoadSession` 在 `session.go:57-75` 实现：scanner buffer 1MB，跳过空 content 行。
- [ ] 函数 `ListSessions` 在 `session.go:77-121` 实现：扫 jsonl 文件，提取首条 user message 作为标题，按 mtime 倒序。
- [ ] 函数 `MatchesSearch` 在 `session.go:181-188` 实现：小写 contains 匹配 first message 与 id。
- [ ] 函数 `FormatRelativeTime / FormatFileSize` 在 `session.go:133-179` 实现，覆盖分钟 / 小时 / 天 / 周和 B/KB/MB 单位。

### 提示历史（history）
- [ ] 类型 `history.entry` 在 `/Users/codemelo/codebot/internal/history/history.go:13-16` 定义（`Text / Ts`，未导出）。
- [ ] 函数 `Load` 在 `history.go:22-39` 实现：jsonl 全读，过滤空 text 行。
- [ ] 函数 `Append` 在 `history.go:41-69` 实现：相邻去重 + cap `maxEntries=200` + 重写整个文件。

### 自动记忆（memory）
- [ ] 类型 `Manager` 在 `/Users/codemelo/codebot/internal/memory/memory.go` 定义，含 `projectRoot / userMemDir / memDir` 三个字段；`NewManager` 同时解析项目级 + 用户级两个目录。
- [ ] 类型 `MemoryFile` 在 `memory.go:57-63` 定义（`Path / Name / Description / Type`）。
- [ ] 类型 `MemoryType` 在 `/Users/codemelo/codebot/internal/memory/memory_types.go:12-21` 定义，4 个常量 `TypeUser / TypeFeedback / TypeProject / TypeReference`。
- [ ] 函数 `ParseMemoryType` 在 `memory_types.go:26-33` 实现：未知值返回 `("", false)`。
- [ ] 类型 `EntrypointTruncation` 在 `/Users/codemelo/codebot/internal/memory/memdir.go:31-37` 定义。
- [ ] 函数 `TruncateEntrypointContent` 在 `memdir.go:43-96` 实现：先行裁、再字节裁、按 `LastIndex(..., "\n")` 取最近换行；追加 `> WARNING:` 行说明原因。
- [ ] 函数 `EnsureMemoryDirExists` 在 `memdir.go:113-118` 实现，幂等 `os.MkdirAll`。
- [ ] 函数 `BuildMemoryLines(displayName, userMemDir, projectMemDir)` 在 `memdir.go` 实现：双目录签名，开篇分别描述两个 scope 的用途和绝对路径，正文使用 `TypesSectionDualPath`；其余 sections（what-not-to-save / how-to-save / when-to-access / trusting-recall / 其他持久化机制对比）保留。
- [ ] 函数 `BuildMemoryPrompt(displayName, userMemDir, projectMemDir)` 在 `memdir.go` 实现：行为提示后分别输出 `## User-level MEMORY.md (path)` 和 `## Project-level MEMORY.md (path)` 两节内容（任一目录为空时该节自动 skip，空内容显示对应级别的 placeholder）；通过 `writeEntrypointSection` 助手避免重复。
- [ ] 函数 `LoadAutoMemoryPrompt(projectRoot)` 在 `memdir.go` 实现：TUI 入口，同时解析 `GetUserAutoMemPath()` 与 `GetAutoMemPath(projectRoot)`，两边都 lazy 创建后转调 `BuildMemoryPrompt`；都空时返回空串。
- [ ] 函数 `GetAutoMemPath(projectRoot)` 在 `/Users/codemelo/codebot/internal/memory/paths.go` 实现，返回 `<projectRoot>/.codebot/memory/` 带尾分隔符的路径；`CODEBOT_REMOTE_MEMORY_DIR` 环境变量可覆盖。
- [ ] 函数 `GetUserAutoMemPath()` / `GetUserAutoMemEntrypoint()` 在 `paths.go` 实现，返回 `~/.codebot/memory/` 与该目录下 `MEMORY.md` 的绝对路径；`$HOME` 不可解析时返回空串。
- [ ] 函数 `IsAutoMemPath(absolutePath, projectRoot)` 在 `paths.go` 实现：项目级 OR 用户级任一命中即合法。`IsUserAutoMemPath(absolutePath)` 单独判定用户级。
- [ ] 函数 `(*Manager).BuildSystemReminder` 在 `memory.go` 实现：lazy mkdir 两个目录后转调 `BuildMemoryPrompt(displayName, userMemDir, memDir)`。
- [ ] 函数 `(*Manager).UserDir() / UserEntrypointPath()` 在 `memory.go` 实现，返回用户级 dir 与其 MEMORY.md 路径。
- [ ] 函数 `(*Manager).GetMemories` 在 `memory.go` 实现：输出 `[type] name — desc` 字符串，覆盖两个目录合集。
- [ ] 函数 `(*Manager).LoadAll` 在 `memory.go` 实现：扫两个目录的 `*.md` 跳 MEMORY.md，用户级在前、项目级在后，每个目录内部按文件名升序；用 `loadDir(dir)` 助手解决重复扫描逻辑。
- [ ] 函数 `(*Manager).Clear` 在 `memory.go` 实现，删两个目录下的全部 `.md`；用 `clearDir(dir)` 助手。
- [ ] 函数 `parseFrontmatter` 在 `memory.go` 实现，用 `(?s)\A---\s*\n(.*?)\n---\s*\n` 正则；未知字段忽略。

### Memory 老化（memory_age）
- [ ] `MemoryAgeDays / MemoryAge / MemoryFreshnessText / MemoryFreshnessNote` 在 `/Users/codemelo/codebot/internal/memory/memory_age.go` 实现，行为保持一致 原始定义。
- [ ] 负数 mtime 钳位到 0；今天/昨天/N 天分支正确；≤1 天不出 freshness warning。

### Memory 扫描（memory_scan）
- [ ] `MemoryHeader` 含 `Filename / FilePath / Scope / MtimeMs / Description / Type` 六字段；`MaxMemoryFiles=200 / FrontmatterMaxLines=30` 在 `memory_scan.go` 定义。
- [ ] `ScanMemoryFiles(ctx, memoryDir, scope)` 三参数签名：用 `filepath.WalkDir` 递归扫，排除 `MEMORY.md` + 非 `.md` 文件，并行 `goroutine + sync.Mutex` 收集；按 `MtimeMs` 倒序；截断到 200；scope 写入每个 header。调用方扫两次（user / project）后合并。
- [ ] `FormatMemoryManifest(memories)` 输出 `- [<scope>-scope] [type] <FilePath> (2006-01-02T15:04:05.000Z): description`：scope/type 任一为空时该前缀省略；filename 改用绝对路径 `FilePath`（fallback Filename）；description 为空时不带冒号。

### 相关性召回（find_relevant_memories）
- [ ] `RelevantMemory{Path, MtimeMs}` 类型在 `find_relevant_memories.go` 定义。
- [ ] `SelectorFn func(ctx context.Context, systemPrompt, userMessage string) (string, error)` 抽象使 memory 包零跨包依赖。
- [ ] `SelectMemoriesSystemPrompt` 全文与参考 `SELECT_MEMORIES_SYSTEM_PROMPT` 一致（除末尾追加的 JSON-only 提示）。
- [ ] `FindRelevantMemories(ctx, query, userMemDir, projectMemDir, recentTools, alreadySurfaced, selector)` 双目录签名：任一为空都允许；用 `ScanMemoryFiles(..., "user")` + `ScanMemoryFiles(..., "project")` 分别扫合并；selector 返回的 key 优先匹配 `FilePath`，兜底匹配 `Filename`（解决跨目录同名歧义）。
- [ ] alreadySurfaced 过滤生效；selector 返回不存在的 filename 被过滤；非法 JSON 返回 `[]`；ctx 取消返回 `[]` 不报错。
- [ ] `extractJSONObject` 容忍 markdown 包裹的 JSON。

### Extractor 包
- [ ] `internal/memory/extractor/prompts.go` 含 `BuildExtractAutoOnlyPrompt(newMessageCount, existingMemories, skipIndex, userMemDir, projectMemDir)` 五参数签名；正文含 `## Memory storage paths` 段把两个目录路径喂给 LLM；主体类型说明改用 `TypesSectionDualPath`（保留 `<scope>` 标签）；team-memory 版 `buildExtractCombinedPrompt` 仍不抄。
- [ ] `internal/memory/extractor/extractor.go` 含 `Deps / Extractor / InitExtractMemories / Execute / Drain` + 内部辅助 `runExtraction / buildExtractorConversation / countModelVisibleMessagesSince / hasMemoryWritesSince / extractWrittenPaths / getWrittenFilePath`。`Deps` 含 `MemoryDir / UserMemoryDir / ProjectRoot / Client / ToolRegistry / Protocol / Conversation / AppendSystem / DebugLogf` 九个字段。
- [ ] subAgent 用 `agents.FilterToolsForAgent(reg, nil, nil, true)` 拿到 async whitelist（ReadFile/WriteFile/EditFile/Glob/Grep/Bash/ToolSearch）。
- [ ] subAgent 用 `permissions.NewPathSandbox(MemoryDir, UserMemoryDir)`（双目录都允许写，**不**带 wd）+ `permissions.ModeBypass`（后台 fire-and-forget 不能 Ask）；`UserMemoryDir` 为空时省略，等价于单路。
- [ ] subAgent.MaxIterations = 5。
- [ ] 提取完成后调 `Deps.AppendSystem("Memory saved: <name>...")`。
- [ ] cursor 仅在 run 成功后推进；hasMemoryWritesSince 命中时跳过 fork 但仍推 cursor。
- [ ] 重入时 stash `pendingContext`；finally 跑 trailing extraction。
- [ ] `Drain(timeoutMs)` 用 `WaitGroup.Wait` 等所有 in-flight；timeoutMs<0 默认 60000。

### Agent 钩子（OnLoopComplete）
- [ ] `Agent.OnLoopComplete func(conv *conversation.Manager)` 字段在 `internal/agent/agent.go` 定义。
- [ ] 在 `len(toolCalls) == 0` 分支 `ch <- LoopComplete{...}` 之后、`return` 之前 fire-and-forget `go a.OnLoopComplete(conv)`。

## 2. 接入完整性（必查，杜绝死代码）
- [ ] `grep -rn "memory\." /Users/codemelo/codebot --include="*.go" | grep -v "_test.go" | grep -v "internal/memory/"` 至少 6 处非测试调用方：
 - `/Users/codemelo/codebot/internal/tui/tui.go:181` (`memoryMgr *memory.Manager`)
 - `/Users/codemelo/codebot/internal/tui/tui.go:363` (path sandbox 额外路径)
 - `/Users/codemelo/codebot/internal/tui/tui.go:525` (`memory.NewManager(wd)`)
 - `/Users/codemelo/codebot/internal/tui/tui.go:642` (`memory.LoadAutoMemoryPrompt`)
 - `/Users/codemelo/codebot/internal/tui/tui.go:647` (`memory.LoadInstructions`)
 - `/Users/codemelo/codebot/internal/tui/tui.go:725` (path sandbox 重新构造)
- [ ] `grep -rn "session\." /Users/codemelo/codebot --include="*.go" | grep -v "_test.go" | grep -v "internal/session/"` 至少 10 处（TUI `session.NewID / SaveMessage / LoadSession / ListSessions / SessionInfo / MatchesSearch / FormatRelativeTime / FormatFileSize` 全部命中）。
- [ ] `grep -rn "history\." /Users/codemelo/codebot --include="*.go" | grep -v "_test.go" | grep -v "internal/history/"` 至少 4 处（TUI `history.Load / history.Append` 各两处）。
- [ ] `grep -rn "extractor\." /Users/codemelo/codebot/internal --include="*.go" | grep -v "_test.go" | grep -v "internal/memory/extractor/"` 命中 `tui.go` 的 `extractor.InitExtractMemories` / `extractor.Deps` / `extractor.Extractor`。
- [ ] `grep -rn "OnLoopComplete" /Users/codemelo/codebot/internal --include="*.go" | grep -v "_test.go"` 命中 `agent/agent.go` 定义 + `tui/tui.goinstallMemoryExtractor` 设置。
- [ ] `grep -rn "FindRelevantMemories\|prefetchRelevantMemories" /Users/codemelo/codebot/internal --include="*.go" | grep -v "_test.go"` 命中 `tui.go` 调用方。
- [ ] `grep -rn "MemoryFreshnessText\|MemoryAge\b" /Users/codemelo/codebot/internal --include="*.go" | grep -v "_test.go"` 命中 `tui.go::renderRelevantMemoriesReminder` 调用。
- [ ] 用户输入到本模块的路径可一句话描述:
 - 项目指令: TUI 启动 → `loadCustomInstructions(wd)` → `memory.LoadInstructions(wd)` → 注入 `BuildOptions.CustomInstructions`。
 - 自动记忆: TUI 启动 → `memory.NewManager(wd)` + `memory.LoadAutoMemoryPrompt(wd)` 注入 `BuildOptions.MemorySection`。
 - 会话存档: 用户发消息 → `session.SaveMessage` + `history.Append`；启动 → `session.NewID / ListSessions`；`/resume` → `LoadSession` 回灌。
 - 自动提取链：用户消息收尾 → `agent.Run` 走到 LoopComplete → `go a.OnLoopComplete(conv)` → `extractor.Execute` → forked subAgent run → 写 `<wd>/.codebot/memory/<topic>.md` → `AppendSystem` 推回 TUI。
 - 召回链：TUI `chat` → `AddUserMessage` → `prefetchRelevantMemories(query)` 启动 selector goroutine → `collectPrefetchedRecall` 3s timeout 收 channel → `AddSystemReminder(rendered)` → `ag.Run`。
 - Drain 链：用户退出 → `tea.Quit` 之前 → `memoryExtractor.Drain(5000)`。
- [ ] Path sandbox 两处构造点都同时暴露 `memory.GetAutoMemPath(wd)` 与 `memory.GetUserAutoMemPath()`（用 `extraAllowed` 切片 append；user-level 为空时跳过），保证 Agent 用 Write 工具写任一 memory 目录时不被沙箱拒绝。
- [ ] 系统提示拼装器 `prompt.BuildSystemPrompt` 接受 `CustomInstructions / MemorySection` 字段（`internal/prompt/builder.go:99-121`）。

## 3. 编译与测试
- [ ] `go build ./...` 通过（章节交付前已执行）。
- [ ] `go test ./internal/memory/... -count=1` 全部通过（含 instructions / memory / memdir / memory_age / memory_scan / find_relevant_memories 测试）。
- [ ] `go test ./internal/memory/extractor/... -count=1` 全部通过（含 end-to-end 测试）。
- [ ] `go test ./internal/session/ -v` 通过。
- [ ] `go test ./internal/history/ -v` 通过。
- [ ] `go test ./internal/agent/... -count=1 -run "OnLoopComplete"` 2 个新测试通过。
- [ ] `go vet ./internal/memory/ ./internal/session/ ./internal/history/` 无警告。

## 4. 端到端验证
- [ ] 项目根存在 CODEBOT.md（如本仓库），启动 TUI 后系统提示包含 `Contents of CODEBOT.md`（验证方式：开 `--debug` 日志 / 在 system prompt 输出处加临时打印）。
- [ ] CODEBOT.md 含 `@/Users/codemelo/.codebot/rules/context7.md` 这类 include，能展开并加入提示。
- [ ] TUI 收发若干消息后退出，再启动 `/resume`，列表里看到之前的会话；选择恢复后 conversation 含历史消息。
- [ ] TUI 输入框按上方向键能调出之前的提示（来自 `history.Load`）。
- [ ] Agent 主动 Write `<memDir>/foo.md` 与往 `MEMORY.md` 追加索引，下次启动系统提示包含 MEMORY.md 内容；`/memory list` 列出该 memory；`/memory clear` 之后系统提示回到 placeholder（`Your MEMORY.md is currently empty…`）。
- [ ] 启动 TUI，先 `/memory clear` 清空，跟 agent 说"记住我是 Go 工程师，正在做 CodeBot 教学项目"；agent 主回复后等待 ~10s，检查 `<wd>/.codebot/memory/` 出现 `user_role.md`（要么主 agent 直接写、要么 extractor 后台写），TUI 顶部出现 `Memory saved: user_role.md` 系统提示。
- [ ] 重启 TUI，输入"项目的技术栈是什么？"，主 LLM 的回复应当能援引"Go 工程师 / CodeBot 教学项目"信息（说明 recall selector 命中了 user_role 并注入了 system-reminder）。
- [ ] 把 `<wd>/.codebot/memory/user_role.md` 的 mtime 改成 5 天前（`touch -t 202605170000`），再问一遍，回复里能体现"该记忆是 5 天前的，建议核对"风味（说明 freshness warning 注入生效）。
- [ ] 跑 `CODEBOT_DEBUG=1 codebot`（如果支持 debug 环境变量）或在 TUI 退出后检查 `.codebot/` 日志，应当能看到 `[extractMemories] starting` / `finished` / `running trailing extraction` 等 debug 行。
- [ ] 用户按 Ctrl-D 退出 TUI 时，如果当时正有 extraction 在跑，退出会等待最多 5s（不卡死、不立即丢失工作）。
- [ ] 留存证据: `internal/memory/instructions_test.go`、`internal/memory/memory_test.go`、`internal/session/session_test.go` 三套测试可重复跑。

## 5. 文档
- [ ] spec.md / tasks.md / checklist.md 三件套齐全且最新（位于 `/Users/codemelo/codebot/docs/go/ch09/`）。
- [ ] commit 信息标注 `ch09` 与三件套关闭状态。


````

### Python

```markdown
# ch09: 记忆系统 Spec

## 1. 背景

只活在单轮对话里的 Agent 每次启动都是一张白纸：用户偏好得反复说、项目背景得反复讲、上次的会话内容拿不回来。CodeBot 用三种正交的「记忆」机制组合解决这三个问题：项目级指令文件（`CODEBOT.md`，支持 `@include` 模块化）、会话存档（jsonl 落盘 + meta 索引、可 `/session resume` 恢复）、自动记忆（每 N 轮 loop 后 fire-and-forget 跑一次提取，把对话中提到的偏好/反馈/项目/参考四类信息写进 `.codebot/memories.md`，下次启动随系统提示注入回上下文）。本章把这三套机制全部落到 CodeBot 的 Python 实现。

## 2. 目标

交付三类相互独立的持久化机制，给 TUI 启动与运行期共同使用：项目指令加载（按三层优先级拼装 `CODEBOT.md` 并展开 `@include`）、会话存档（消息追加写 jsonl、`/session resume` 列表与恢复、tool-use/tool-result 链路完整性校验、断会话时长提示）、自动记忆（项目级 + 用户级双路 memories.md、每 5 轮 loop 触发 LLM extractor 自动改写、`/memory list/clear/edit` 命令暴露）。最终 TUI 启动时三套机制都开箱可用，注入路径由 `ConversationManager.inject_long_term_memory` 统一收口。

## 3. 功能需求

### 项目指令

- F1: `load_instructions(project_root)` 按三层优先级（`<project_root>/CODEBOT.md` → `<project_root>/.codebot/CODEBOT.md` → `~/.codebot/CODEBOT.md`）依次读取，用 `\n---\n` 拼接为一段；缺失文件静默跳过。
- F2: `process_includes(content, base_dir, project_root, depth)` 递归展开 `@include <path>` 行；`MAX_INCLUDE_DEPTH=5` 兜底；越界路径（解析后不在 `project_root` 内）替换为 `<!-- @include blocked: path outside project -->`；文件不存在替换为 `<!-- @include skipped: file not found -->`。
- F3: 拼装后的整段通过 `Agent.instructions_content` 字段透传，由 `ConversationManager.inject_long_term_memory` 以 `## 项目指令\n...` 形式插入对话首部。

### 会话存档

- F4: `SessionRecord` dataclass 持 `type / content / timestamp / tool_use_id / is_error`；`RecordType` 枚举 5 种（`system_prompt / user / assistant / tool_result / compression`）；`to_jsonl / from_jsonl` 一对方法做序列化，反序列化失败返回 `None`。
- F5: `SessionRecord.from_message(message)` 把单条 `Message` 拆成一到多条 jsonl 记录（含 tool_use 内联到 assistant content blocks、tool_results 各自一条 `tool_result` 记录）。
- F6: `Session.append(message)` 把 `from_message` 拆出的记录逐条写 jsonl + `\n` 并 `flush`；同步更新 `meta.message_count / last_active`；首条 user 消息截断到 `TITLE_MAX_LENGTH=50` 写入 `meta.title`；`Session.close()` 安全 `flush + close`。
- F7: `SessionManager(work_dir)` lazy 创建 `<work_dir>/.codebot/sessions/`；`create()` 用 `session_<YYYYMMDD_HHMMSS>_<4 字符 suffix>` 命名；`list()` 扫所有 `*.meta` 反序列化并按 `last_active` 倒序；`resume(id)` 读取 jsonl + meta、校验链路完整性、重建 `[Message]`、追加打开 jsonl 续写；`delete(id)` / `cleanup(max_age_days=30)` 维护清理。
- F8: `records_to_messages(records)` 把 jsonl 记录序列还原成 `[Message]`：连续 tool_result 合并到下一条 user 消息的 `tool_results`、`assistant` content 为 list 时拆出 text + tool_uses、`system_prompt` 跳过、`compression` 渲染成 `[摘要]\n...` 的 user 消息。
- F9: `validate_message_chain(records)` 扫描 tool_use ↔ tool_result 配对状态，返回链路完整的最大前缀长度；resume 时用该长度截断防止把缺少 tool_result 的 tool_use 灌回去触发 API 400。
- F10: `build_time_gap_message(last_active)` 在距上次活跃 ≥ `TIME_GAP_THRESHOLD=24h` 时返回一条系统提示 `Message`（≥48 小时表达为「N 天」，否则「N 小时」），追加到恢复后的对话尾部提示用户「代码可能有变更」。

### 自动记忆

- F11: `MemoryManager(project_root)` 构造时计算两个固定路径：`~/.codebot/memories.md`（用户级，跨项目）和 `<project_root>/.codebot/memories.md`（项目级），由 `user_path / project_path` property 暴露。
- F12: `load()` 拼装两层 memories.md 内容（`\n\n` 分隔），返回空字符串时调用方跳过注入。
- F13: `clear()` 把两个 memories.md 截断为空（不删除文件本身，保持后续直接 `write_text` 可用）。
- F14: `get_display_text()` 为 `/memory list` 渲染层级标注（`[用户级] <path>\n<content>` / `[项目级] ...`）；两路皆空返回 `"当前没有任何自动记忆。"`。
- F15: `MEMORY_EXTRACTION_PROMPT` 文本固化四类分类（用户偏好 / 纠正反馈 / 项目知识 / 参考资料），要求 LLM 输出完整 memories.md、空分类下不写占位、相同含义条目不重复添加。
- F16: `extract(client, conversation, protocol)` 从 `conversation.history[self._last_extraction_msg_count:]` 取增量对话、拼装 prompt、跑一次非流式 `client.stream` 收集 text、`_write_memories` 解析输出按 header 关键字 (`_USER_LEVEL_HEADERS={"用户偏好","纠正反馈"} / _PROJECT_LEVEL_HEADERS={"项目知识","参考资料"}`) 分流写入两路 memories.md；异常静默 return。
- F17: `_is_placeholder(line)` 把 `"" / "..." / "…" / "无" / "暂无" / "N/A"` 等占位行过滤掉，防止 LLM 把空模板回写为「真记忆」。

### Agent 钩子 + 注入

- F18: `Agent.__init__` 接受 `instructions_content / memory_manager` 两个字段；`Agent.run` 入口先 `inject_environment` 再 `inject_long_term_memory(instructions, memory_manager.load() if memory_manager else "")`；auto-compact 重置历史后同样的二次注入。
- F19: 自动提取触发：`Agent` 内置 `MEMORY_EXTRACTION_INTERVAL=5` 与 `_loop_count`；每次 `len(response.tool_calls)==0` 分支（loop 收尾）递增 `_loop_count`；当 `_loop_count % MEMORY_EXTRACTION_INTERVAL == 0` 且 `memory_manager` 存在时 `asyncio.ensure_future(self._extract_memories(conversation))` fire-and-forget。
- F20: `_extract_memories(conversation)` 用 `self._extracting` 互斥防重入；try-except-finally 包裹 `memory_manager.extract`，异常仅 `log.debug` 不传播。

### 对话注入

- F21: `ConversationManager.inject_long_term_memory(instructions, memories)` 幂等：若 `ltm_injected` 已为 True 直接 return；否则按 `env_injected` 计算 base pos，再依次插入 `## 项目指令\n...` 和 `## 自动记忆\n...` 两条 user message，最后追加一条 assistant 占位 `"好的，我已了解项目背景和记忆。"` 把视觉边界封死；只有当 `instructions` 或 `memories` 至少一个非空时才置 `ltm_injected=True`。
- F22: `ConversationManager.replace_history(new_messages)` 在 resume / 切换会话时重置 `env_injected=False / ltm_injected=False`，下次 Agent.run 才能重新注入。

### TUI / 命令

- F23: `/memory` 命令 (`handle_memory`) 暴露子命令 `list / clear / edit`：list 打印 `get_display_text`、clear 调 `MemoryManager.clear` 并提示「所有自动记忆已清空。」、edit 打印两个 memories.md 路径供用户外部编辑、空参数等价于 list、未知子命令给出 usage。
- F24: `/session` 命令 (`handle_session`) 暴露 `list / resume / new / delete`：list 取 `SessionManager.list()` 前 10 条按 `last_active` 渲染；resume 无参数时打印前 15 条候选并把 ID 列表暂存到 `ctx.config["_resume_candidates"]`，再次执行时支持「序号」简写；resume 成功调 `ctx.config["set_session"](result.session)`、构造新 `ConversationManager` 灌入 `result.messages`、追加 `build_time_gap_message` 输出、`ctx.config["set_conversation"]` 切换、`ctx.config["render_restored"]` 渲染、`ctx.agent._loop_count=0` 复位 extractor cursor。
- F25: `App` (TUI 入口) 启动时调 `load_instructions(work_dir)` / `MemoryManager(work_dir)` / `SessionManager(work_dir).cleanup() / create()`，把结果存到 `self._instructions_content / self.memory_manager / self.session / self.session_manager`，再传入 `Agent` 构造函数；用户每条消息提交后 `self.session.append`，助手每条消息收尾后 `self.session.append` + 异步 `_update_session_summary`。

## 4. 非功能需求

- N1: 加载失败一律静默：找不到文件、`@include` 文件缺失、jsonl 行解析失败、meta 文件损坏都返回空或 None，不中断 TUI 启动。
- N2: `@include` 必须循环安全：`MAX_INCLUDE_DEPTH=5` 兜底 + 路径越界拦截。
- N3: jsonl 单行可能很长（tool_result 含大段输出），写入时按行 `json.dumps(ensure_ascii=False)` + `\n` + `flush`；读取时按行 `strip` 后逐条 `from_jsonl`，空行/失败行跳过。
- N4: memories.md 是「LLM 覆盖式重写」语义，每次 extract 全量覆盖；占位行过滤 (`_is_placeholder`) 是最后的语义防线。
- N5: 自动提取必须 fire-and-forget：用 `asyncio.ensure_future` 不 `await`；`_extracting` 互斥防止上一轮还没跑完又起一轮。
- N6: `inject_long_term_memory` 必须幂等：单次 Agent.run 反复进入（compact 触发的二次注入除外）只插一次。
- N7: 公开符号都被外部模块调用，无死代码：`from codebot.memory import ...` 在 `codebot/memory/__init__.py` 集中 re-export，`tests/test_memory.py` 全部命中。

## 5. 设计概要

### 核心数据结构

- `codebot.memory.auto_memory.MemoryManager`：双路径 + `_last_extraction_msg_count` cursor
- `codebot.memory.session.RecordType`：5 个值的字符串 Enum
- `codebot.memory.session.SessionRecord`：dataclass，jsonl 行结构
- `codebot.memory.session.SessionMeta`：dataclass，会话元数据（id / title / summary / message_count / total_tokens / created_at / last_active），独立 `.meta` JSON 落盘
- `codebot.memory.session.Session`：活跃会话句柄，持 jsonl file handle 与 meta
- `codebot.memory.session.ResumeResult`：dataclass，含恢复后的 `session + messages + last_active`
- `codebot.memory.session.SessionManager`：工厂 + 目录管理
- `codebot.memory.instructions.MAX_INCLUDE_DEPTH / INCLUDE_PREFIX`：模块常量
- `codebot.conversation.ConversationManager`：`env_injected / ltm_injected` 两个标志位 + `inject_long_term_memory / replace_history`

### 主流程

- **项目指令**：App 启动 → `load_instructions(work_dir)` → 三层文件读 → `process_includes` 递归展开 → 返回单字符串 → `Agent(instructions_content=...)` → `Agent.run` 入口 `conversation.inject_long_term_memory(self.instructions_content, ...)`。
- **会话存档**：App 启动 → `SessionManager.cleanup() / create()` → `self.session = Session(...)`；用户消息 → `self.session.append(Message(role="user", ...))`；助手消息收尾 → `self.session.append(Message(role="assistant", ...))`；TUI 退出 → `self.session.close()`；`/session resume` → `SessionManager.resume(id)` → `validate_message_chain` 截断 → `records_to_messages` 重建 → `ConversationManager.replace_history` → 注入 time gap message。
- **自动记忆**：Agent loop 收尾 (`len(tool_calls)==0`) → `_loop_count += 1` → 模 5 触发 → `asyncio.ensure_future(self._extract_memories(conversation))` → `MemoryManager.extract` → `client.stream` 跑 extractor prompt → `_write_memories` 按 header 关键字分流写入 user/project memories.md → 下次 `Agent.run` 入口 `memory_manager.load()` 读回 → `inject_long_term_memory` 注入 `## 自动记忆` block。
- **`/memory` 命令**：用户输入 `/memory list` → `handle_memory` 派发 → `MemoryManager.get_display_text` → `ctx.ui.add_system_message`；`/memory clear` → `MemoryManager.clear` → 提示文字。
- **断会话提示**：`/session resume <id>` → 取 `meta.last_active` → `build_time_gap_message` 判断 ≥24h → 追加一条 user message 到恢复对话末尾。

### 调用链（模块层级）

- 启动：`codebot.app.App` → `codebot.memory.{load_instructions, MemoryManager, SessionManager}` → `codebot.agent.Agent(instructions_content, memory_manager)`
- 运行：`codebot.agent.Agent.run` → `codebot.conversation.ConversationManager.inject_long_term_memory` + `codebot.memory.auto_memory.MemoryManager.{load, extract}`
- 命令：`codebot.commands.handlers.memory.handle_memory` ↔ `MemoryManager.{get_display_text, clear}`；`codebot.commands.handlers.session.handle_session` ↔ `SessionManager.{list, resume, create, delete}` + `build_time_gap_message`

### 与其他模块的交互

- `codebot/conversation.py`：提供 `inject_long_term_memory / replace_history` 注入面板；`ltm_injected` 是幂等开关。
- `codebot/agent.py`：吸纳 `instructions_content / memory_manager` 字段；`MEMORY_EXTRACTION_INTERVAL / _loop_count / _extracting / _extract_memories` 串起提取闭环。
- `codebot/app.py`：唯一安装点，承担三套机制的 lazy 构造与 session lifecycle 管理。
- `codebot/commands/handlers/`：通过 `CommandContext.{memory_manager, session_manager, session, agent, config, ui}` 拿到所有句柄，无需直接访问 App。
- `codebot/context.py`：`ensure_session_dir / auto_compact` 与 memory 的关系是「auto-compact 触发后再次注入 LTM」，靠 `ltm_injected=False`（由 `replace_history` 重置）协调。

### 新增文件 / 函数清单

新增（全部位于 `/Users/codemelo/codebot/codebot/memory/`）：

- `__init__.py`：re-export `MemoryManager / load_instructions / process_includes / Session / SessionManager / SessionMeta / SessionRecord / ResumeResult / build_time_gap_message / generate_session_summary / validate_message_chain`
- `auto_memory.py`：`MemoryManager / MEMORY_EXTRACTION_PROMPT / _USER_LEVEL_HEADERS / _PROJECT_LEVEL_HEADERS / USER_MEMORIES_RELPATH / PROJECT_MEMORIES_RELPATH`
- `instructions.py`：`load_instructions / process_includes / MAX_INCLUDE_DEPTH / INCLUDE_PREFIX`
- `session.py`：`RecordType / SessionRecord / records_to_messages / validate_message_chain / SessionMeta / Session / ResumeResult / generate_session_summary / build_time_gap_message / _generate_session_id / SessionManager` + 模块常量

修改：

- `codebot/agent.py`：`MEMORY_EXTRACTION_INTERVAL` 常量 + `Agent.__init__` 接 `instructions_content / memory_manager` + `Agent.run` 入口注入 + loop 收尾分支触发 `_extract_memories` + auto-compact 后重新注入。
- `codebot/conversation.py`：`inject_long_term_memory / replace_history / ltm_injected`。
- `codebot/commands/handlers/memory.py`：`handle_memory / MEMORY_COMMAND`。
- `codebot/commands/handlers/session.py`：`handle_session`（含 list / resume / new / delete）。
- `codebot/app.py`：启动期构造 + session lifecycle 管理 + 命令上下文字段填充。

## 6. Out of Scope

- 会话过期清理的策略可配置化：硬编码 `DEFAULT_MAX_AGE_DAYS=30`，暂不开放。
- 团队 / 远程 memory 同步：只做本地双路径个人模式。
- Memory 老化提示：本章 Python 实现暂不带 freshness / drift caveat。
- 相关性召回（selector LLM 选 ≤5 条 memory 注入）：本章不实现，所有 memories 一并注入。
- 自动提取的「写时互斥跳过」（Agent 自己刚改过 memories.md 就跳过本轮 extractor）：靠 LLM 提示词 + 占位过滤兜底，不在调度层做。
- 会话 jsonl 的 schema 版本化与迁移。
- CODEBOT.md 大文件警告与裁剪。
- `CODEBOT_REMOTE_MEMORY_DIR` 等环境变量覆盖。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

codebot/commands/├── **init**.py        re-export Command / CommandRegistry / parse\_command / complete├── registry.py        CommandType / Command / CommandContext / UIController / CommandRegistry├── parser.py          parse\_command / complete├── completion.py      CompletionPopup (Textual widget)└── handlers/├── **init**.py    ALL\_COMMANDS 列表 + register\_all\_commands(registry)├── help.py        HELP\_COMMAND├── compact.py     COMPACT\_COMMAND├── clear.py       CLEAR\_COMMAND├── plan.py        PLAN\_COMMAND├── do.py          DO\_COMMAND├── session.py     SESSION\_COMMAND├── memory.py      MEMORY\_COMMAND├── permission.py  PERMISSION\_COMMAND├── status.py      STATUS\_COMMAND (VERSION = "v0.9.0")└── review.py      REVIEW\_COMMAND (REVIEW\_PROMPT)tests/test\_commands.py  Registry / parser / complete / 各 handler / register\_all\_commands

```markdown
# ch09: 记忆系统 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。所有 T 任务完成后逐项勾上，每条任务记录实际落地的文件与行号。

## T1: 项目指令 `@include` 递归展开
- 影响文件: `/Users/codemelo/codebot/codebot/memory/instructions.py:9-46`（`process_includes`）
- 依赖任务: 无
- 完成标准:
  - `MAX_INCLUDE_DEPTH=5` 与 `INCLUDE_PREFIX="@include "` 常量到位
  - 逐行扫描 content，命中前缀的行剥出相对路径，按 `(base_dir / rel_path).resolve()` 解析
  - 解析后用 `abs_path.relative_to(resolved_root)` 判断是否在 project_root 内，越界落 `<!-- @include blocked: path outside project -->`
  - 文件不存在 / 非 file 落 `<!-- @include skipped: file not found -->`
  - 命中的文件递归 `process_includes(..., depth+1)` 后拼回
  - `depth >= MAX_INCLUDE_DEPTH` 直接 return 原 content
  - 测试 `TestProcessIncludes` 5 个用例（无 include / 基本 include / 递归 include / depth 限制 / path 越界 / 文件不存在）全部命中

## T2: 项目指令三层加载
- 影响文件: `/Users/codemelo/codebot/codebot/memory/instructions.py:48-66`（`load_instructions`）
- 依赖任务: T1
- 完成标准:
  - 三层优先级顺序：`<root>/CODEBOT.md` → `<root>/.codebot/CODEBOT.md` → `~/.codebot/CODEBOT.md`
  - 每层文件存在且 is_file 时读取并对其内容跑 `process_includes(content, path.parent, root)`
  - 多段用 `\n---\n` join
  - 无任何文件存在时返回 `""`
  - 测试 `TestLoadInstructions`（`test_single_layer / test_multi_layer_priority / test_no_files_returns_empty`）通过

## T3: SessionRecord 序列化
- 影响文件: `/Users/codemelo/codebot/codebot/memory/session.py:30-119`（`RecordType / SessionRecord`）
- 依赖任务: 无
- 完成标准:
  - `RecordType(str, Enum)` 5 个值：`system_prompt / user / assistant / tool_result / compression`
  - `SessionRecord` dataclass 字段：`type / content / timestamp / tool_use_id / is_error`
  - `to_jsonl()`：序列化 `{type, content, timestamp}`，可选 `tool_use_id`，仅 tool_result 写 `is_error`，`ensure_ascii=False`
  - `from_jsonl(line)`：异常返回 None；未知 RecordType 也返回 None
  - `from_message(message)`：tool_results 拆多条 tool_result 记录；assistant + tool_uses 内联到 content blocks (`[{type:text}, {type:tool_use,id,name,input}]`)；plain user / assistant 走单条普通记录
  - 测试 `TestSessionRecord` 5 个用例（user roundtrip / assistant with tool_uses / tool_results multiple records / malformed jsonl / plain assistant）通过

## T4: 记录 ↔ 消息互转 + 链路校验
- 影响文件: `/Users/codemelo/codebot/codebot/memory/session.py:122-222`（`records_to_messages / validate_message_chain`）
- 依赖任务: T3
- 完成标准:
  - `records_to_messages`：维护 `pending_tool_results` 队列，遇到非 tool_result 记录前先把队列冲到一条 user message 的 `tool_results`；system_prompt 跳过；compression 渲染为 `[摘要]\n<content>` 的 user message；assistant content list 时拆 text + tool_uses
  - `validate_message_chain`：维护 `pending_tool_uses set`，assistant content list 里 tool_use block 的 id 进集合，tool_result 出集合；集合为空时记录前缀长度，最后返回最大完整前缀
  - 测试 `TestRecordsToMessages`（3 个）+ `TestValidateMessageChain`（3 个）全部通过

## T5: SessionMeta 落盘 + Session 句柄
- 影响文件: `/Users/codemelo/codebot/codebot/memory/session.py:225-307`（`SessionMeta / Session / ResumeResult`）
- 依赖任务: T3
- 完成标准:
  - `SessionMeta` dataclass 7 字段（id / title / summary / message_count / total_tokens / created_at / last_active），`created_at / last_active` 默认 `datetime.now(timezone.utc)`
  - `SessionMeta.save(path)`：JSON 落盘，含 `isoformat` 时间字段
  - `SessionMeta.load(path)`：异常返回 None
  - `Session.__init__(session_id, file, meta, sessions_dir)` 持文件句柄
  - `Session.append(message)`：调 `SessionRecord.from_message` 拆条逐条 `to_jsonl + "\n"` 写入并 `flush`；`meta.message_count += 1`；`meta.last_active = now`；首次遇到 user content 时截 `TITLE_MAX_LENGTH=50` 写入 `meta.title`；每次 append 后 `meta.save` 覆盖 `.meta` 文件
  - `Session.close()`：判空 + `flush + close`
  - `ResumeResult` dataclass：`session / messages / last_active`
  - 测试 `TestSession`（2 个：append 写 jsonl + title 设置）通过

## T6: SessionManager 生命周期
- 影响文件: `/Users/codemelo/codebot/codebot/memory/session.py:384-482`（`_generate_session_id / SessionManager`）
- 依赖任务: T4, T5
- 完成标准:
  - `_generate_session_id()`：`session_<YYYYMMDD_HHMMSS>_<4 字符 a-z0-9>` 格式
  - `SessionManager.__init__(work_dir)`：构造 `<work_dir>/.codebot/sessions/` 并 `mkdir(parents=True, exist_ok=True)`
  - `create()`：新 ID + 写 `.meta` + 打开 jsonl `mode="a"` + 返回 `Session`
  - `list()`：扫 `*.meta`、`SessionMeta.load` 反序列化、按 `last_active` 倒序
  - `resume(id)`：jsonl 缺失返回 None；逐行 `from_jsonl` 跳空跳错；`validate_message_chain` 截断；`records_to_messages` 重建；重新打开 jsonl `mode="a"` 续写
  - `delete(id)`：删 jsonl + .meta，任一存在即返回 True
  - `cleanup(max_age_days=30)`：迭代 `.meta`、`last_active < cutoff` 调 `delete` 并计数
  - 测试 `TestSessionManager`（create_and_list / delete / cleanup / generates_valid_id）+ `TestSessionResume`（restores_messages / nonexistent / truncates_incomplete_chain）通过

## T7: 断会话时长提示
- 影响文件: `/Users/codemelo/codebot/codebot/memory/session.py:358-380`（`build_time_gap_message`）+ `TIME_GAP_THRESHOLD` 常量
- 依赖任务: 无
- 完成标准:
  - `TIME_GAP_THRESHOLD=timedelta(hours=24)` 常量
  - 距 `last_active < 24h` 返回 None
  - `gap.total_seconds() // 3600 >= 48` 表达为「N 天」，否则「N 小时」
  - 返回的 Message 包含 `代码可能有变更，建议在操作前重新读取相关文件。`
  - 测试 `TestTimeGapMessage`（no gap returns none / gap returns message）通过

## T8: 会话摘要生成（可选）
- 影响文件: `/Users/codemelo/codebot/codebot/memory/session.py:316-355`（`generate_session_summary`）+ `SESSION_SUMMARY_PROMPT`
- 依赖任务: 无
- 完成标准:
  - `SESSION_SUMMARY_PROMPT` 文本到位（要求一句话总结、不调用工具）
  - `generate_session_summary(client, conversation, protocol)`：取 `history[-10:]`；构造单独 `ConversationManager` 拼装 prompt + 最近消息 + 收尾问句；跑 `client.stream` 收 `TextDelta`；异常返回 `""`
  - 不做单独单元测试（集成在 App 的异步 summary 更新中）

## T9: MemoryManager 双路径基础
- 影响文件: `/Users/codemelo/codebot/codebot/memory/auto_memory.py:8-71`（常量 + `__init__ / user_path / project_path / load`）
- 依赖任务: 无
- 完成标准:
  - 常量：`USER_MEMORIES_RELPATH = ".codebot/memories.md"` / `PROJECT_MEMORIES_RELPATH = ".codebot/memories.md"`
  - `__init__`：算 `_user_path = Path.home() / USER_MEMORIES_RELPATH` 与 `_project_path = Path(project_root) / PROJECT_MEMORIES_RELPATH`，`_last_extraction_msg_count = 0`
  - `user_path / project_path` property 暴露
  - `load()`：两个路径若存在且非空，`strip` 后用 `\n\n` join；都空返回 `""`
  - 测试 `TestMemoryManager.test_load_empty / test_load_merges_user_and_project` 通过

## T10: MEMORY_EXTRACTION_PROMPT + extract LLM 跑提取
- 影响文件: `/Users/codemelo/codebot/codebot/memory/auto_memory.py:11-37, 72-127`（`MEMORY_EXTRACTION_PROMPT / extract`）
- 依赖任务: T9
- 完成标准:
  - prompt 含 4 类分类标题（用户偏好 / 纠正反馈 / 项目知识 / 参考资料）、`不要重复添加` / `不要写任何条目，不要写占位符` / `不要调用任何工具` 等关键 marker
  - `extract(client, conversation, protocol)`：
    - 从 `conversation.history[self._last_extraction_msg_count:]` 取增量
    - 把 user/assistant 文本拼成 `"用户: ..."` / `"助手: ..."` 行
    - 拼装 prompt 含 `## 当前 memories.md\n<当前内容 or (空)>` + `## 最近对话\n...`
    - 构造独立 `ConversationManager`、`history = [Message(role="user", content=prompt)]`
    - 跑 `client.stream(extract_conv, system="你是一个记忆提取助手。")`，收集 `TextDelta.text`
    - 异常静默 `return`
    - 成功后更新 `self._last_extraction_msg_count = len(conversation.history)`，把 collected 转给 `_write_memories`
  - 测试 `TestMemoryExtraction.test_extraction_prompt_contains_categories` 通过

## T11: `_write_memories` 分流 + 占位过滤
- 影响文件: `/Users/codemelo/codebot/codebot/memory/auto_memory.py:39-40, 128-190`（`_USER_LEVEL_HEADERS / _PROJECT_LEVEL_HEADERS / _write_memories / _is_placeholder / _assign_section`）
- 依赖任务: T10
- 完成标准:
  - `_USER_LEVEL_HEADERS = {"用户偏好", "纠正反馈"}` / `_PROJECT_LEVEL_HEADERS = {"项目知识", "参考资料"}`
  - `_is_placeholder(line)`：剥 `- ` 与空白后命中 `{"", "...", "…", "无", "暂无", "N/A"}` 返回 True
  - `_assign_section(header, lines, user_sections, project_sections)`：先过滤出 `- ` 开头且非占位的 real_lines；构造 `header + "\n" + join(real_lines)`；按 header 含的关键字归入 user 或 project
  - `_write_memories(content)`：按 `### ` 切段、每段调 `_assign_section`；user/project 各自非空时 `mkdir(parents, exist_ok)` + `write_text(strip + "\n", utf-8)`
  - 测试 `TestMemoryManager.test_write_memories_splits_correctly` 通过

## T12: clear + get_display_text
- 影响文件: `/Users/codemelo/codebot/codebot/memory/auto_memory.py:191-213`（`clear / get_display_text`）
- 依赖任务: T9
- 完成标准:
  - `clear()`：两路径若存在 `write_text("")` 截断（不删文件）
  - `get_display_text()`：两路径分别按 `[用户级] <path>\n<content>` / `[项目级] ...` 渲染、`\n\n` 拼接；都空返回 `"当前没有任何自动记忆。"`
  - 测试 `TestMemoryManager.test_clear / test_get_display_text_empty` 通过

## T13: `codebot/memory/__init__.py` 统一 re-export
- 影响文件: `/Users/codemelo/codebot/codebot/memory/__init__.py:1-26`
- 依赖任务: T1-T12
- 完成标准: 通过 `from codebot.memory.auto_memory import MemoryManager` / `from codebot.memory.instructions import load_instructions, process_includes` / `from codebot.memory.session import (ResumeResult, Session, SessionManager, SessionMeta, SessionRecord, build_time_gap_message, generate_session_summary, validate_message_chain)` 集中暴露，`__all__` 列表与导入一一对应

## T14: ConversationManager 注入面板
- 影响文件: `/Users/codemelo/codebot/codebot/conversation.py:41, 75-113`（`ltm_injected` 字段 + `inject_environment / inject_long_term_memory / replace_history`）
- 依赖任务: 无
- 完成标准:
  - `ltm_injected` 默认 False 的 init=False 字段
  - `inject_long_term_memory(instructions, memories)`：已注入直接 return；按 `env_injected` 计算 base pos；instructions 非空时 insert `## 项目指令\n<content>`；memories 非空时 insert `## 自动记忆\n<content>`；二者至少一个非空时插入收尾 assistant `"好的，我已了解项目背景和记忆。"` 并置 `ltm_injected=True`；都空时不动 + `ltm_injected` 保持 False
  - `replace_history(new_messages)`：重置 `env_injected=False / ltm_injected=False`
  - 测试 `TestConversationInjection` 6 个用例（带 env / 幂等 / instructions only / memories only / 全空 / replace 重置）全部通过

## T15: Agent 钩入 + 自动提取
- 影响文件: `/Users/codemelo/codebot/codebot/agent.py:49, 295, 313-315, 404-405, 453-454, 564-568, 870-883, 902-904, 924-926`（`MEMORY_EXTRACTION_INTERVAL / __init__ / run 入口注入 / loop 收尾触发 / _extract_memories / manual_compact 二次注入 / run_to_completion 注入`）
- 依赖任务: T9, T14
- 完成标准:
  - 模块常量 `MEMORY_EXTRACTION_INTERVAL = 5`
  - `Agent.__init__` 接 `instructions_content: str = ""` / `memory_manager: MemoryManager | None = None`；自存 `self._loop_count = 0` / `self._extracting = False`
  - `Agent.run` 入口：`inject_environment` 之后立即 `memory_content = self.memory_manager.load() if self.memory_manager else ""` + `conversation.inject_long_term_memory(self.instructions_content, memory_content)`
  - loop 收尾分支 (`len(response.tool_calls)==0` → `add_assistant_message`)：`self._loop_count += 1`；若 `self._loop_count % MEMORY_EXTRACTION_INTERVAL == 0 and self.memory_manager`：`asyncio.ensure_future(self._extract_memories(conversation))`
  - `_extract_memories(conversation)` async 方法：`self._extracting` 互斥；try-except 包 `memory_manager.extract`，except `log.debug` 不传播；finally 复位 `self._extracting=False`
  - `manual_compact` / `run_to_completion` 路径在 compact 之后或新建 conversation 时同样调 `inject_long_term_memory`（与现有 `inject_environment` 配对）
  - 测试：`tests/test_memory.py` 不直接 cover Agent 触发，靠集成验证；新增 `test_loop_count_triggers_extract` 若有需要

## T16: `/memory` 命令
- 影响文件: `/Users/codemelo/codebot/codebot/commands/handlers/memory.py:1-46`（`handle_memory / MEMORY_COMMAND`）
- 依赖任务: T9, T12
- 完成标准:
  - `handle_memory(ctx)` 空子命令 → `get_display_text`；`list` → 同上；`clear` → `MemoryManager.clear` + 提示「所有自动记忆已清空。」；`edit` → 打印 user_path / project_path 两行路径；未知子命令 → `usage`
  - `MEMORY_COMMAND = Command(name="memory", description="记忆管理", usage="/memory [list | clear | edit]", type=CommandType.LOCAL, handler=handle_memory)`
  - `ctx.memory_manager is None` 时打印「记忆管理器未初始化」并 return

## T17: `/session` 命令
- 影响文件: `/Users/codemelo/codebot/codebot/commands/handlers/session.py`（`handle_session`）
- 依赖任务: T5, T6, T7
- 完成标准:
  - 空子命令打印当前 session meta 摘要（id / title / 消息 / token / 最后活跃）
  - `list` 取 `SessionManager.list()[:10]` 渲染
  - `resume` 无 ID 时列前 15 候选 + 把 ID 列表暂存到 `ctx.config["_resume_candidates"]`，再次调用支持「数字序号」简写
  - resume 成功：关旧 session、`ctx.config["set_session"]` 切换、构造新 `ConversationManager` 灌入 `result.messages`、追加 `build_time_gap_message` 返回值（若非 None）、`ctx.config["set_conversation"]` 切换、`ctx.agent._loop_count = 0`、`ctx.config["render_restored"]` 重绘 UI
  - `new` 新建 session + 清空 conversation + 复位 `_loop_count` + `clear_chat`
  - `delete <id>` 拒绝缺 ID 的调用

## T18: 接入主流程（App 启动 + 命令上下文）
- 影响文件: `/Users/codemelo/codebot/codebot/app.py:51-58, 550-558, 633-660, 873-881, 1063-1070, 1167-1175, 1228-1245, 1474-1495, 1564-1575, 1605-1615`
- 依赖任务: T1-T17
- 完成标准:
  - App 顶部 `from codebot.memory import (MemoryManager, Session, SessionManager, ..., generate_session_summary, load_instructions)`
  - `__init__` 持字段：`self.session_manager / self.session / self.memory_manager / self._instructions_content`
  - 启动期：`self._instructions_content = load_instructions(work_dir)` / `self.memory_manager = MemoryManager(work_dir)` / `self.session_manager = SessionManager(work_dir)` / `self.session_manager.cleanup()` / `self.session = self.session_manager.create()`
  - `Agent(...)` 构造点透传 `instructions_content=self._instructions_content` 与 `memory_manager=self.memory_manager`
  - 用户消息提交时 `self.session.append(Message(role="user", content=text))`
  - 助手消息收尾时 `self.session.append(msg)` + `meta.total_tokens` 累计 + 异步调 `_update_session_summary`
  - `CommandContext` 填 `session / session_manager / memory_manager / agent / config["set_session"] / config["set_conversation"] / config["render_restored"] / config["clear_chat"]`
  - 退出路径 `self.session.close()`

## T19: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T18
- 完成标准:
  - `ruff check codebot/memory/ codebot/agent.py codebot/conversation.py codebot/commands/handlers/memory.py codebot/commands/handlers/session.py` 无错误
  - `pytest tests/test_memory.py -v` 全部通过（约 30+ 用例）
  - 项目根放一个 `CODEBOT.md`，TUI 启动后 system prompt（或 conversation 首部）能看到「## 项目指令」block
  - `CODEBOT.md` 含 `@include ./sub/details.md` 时能展开并加入注入
  - TUI 发几条消息后退出，`.codebot/sessions/` 出现 `session_*.jsonl` + `session_*.meta`；重启后 `/session list` 列出该会话；`/session resume <id>` 把对话恢复
  - 距上次活跃 ≥24h 的 resume 后，对话末尾出现一条「[系统提示] 距离上次会话已过去 N 小时/天。代码可能有变更，建议在操作前重新读取相关文件。」
  - 跟 Agent 对话 5 轮后（每轮 `loop_count` 模 5 等于 0），`.codebot/memories.md` 或 `~/.codebot/memories.md` 出现新行；下次启动后 `## 自动记忆` block 包含该内容；`/memory list` 列出；`/memory clear` 后回到「当前没有任何自动记忆。」

## 进度
- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8
- [ ] T9
- [ ] T10
- [ ] T11
- [ ] T12
- [ ] T13
- [ ] T14
- [ ] T15
- [ ] T16
- [ ] T17
- [ ] T18
- [ ] T19

```

```markdown
# ch09: 记忆系统 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性

### 项目指令（codebot/memory/instructions.py）
- [ ] 模块常量 `MAX_INCLUDE_DEPTH = 5` 在 `/Users/codemelo/codebot/codebot/memory/instructions.py:5` 定义。
- [ ] 模块常量 `INCLUDE_PREFIX = "@include "` 在 `instructions.py:6` 定义。
- [ ] 函数 `process_includes(content, base_dir, project_root, depth=0)` 在 `instructions.py:9-46` 实现：逐行扫描，命中前缀的行剥相对路径并 `(base_dir / rel_path).resolve()`，越界落 `<!-- @include blocked: path outside project -->`，文件不存在落 `<!-- @include skipped: file not found -->`，命中文件递归 `process_includes(..., depth+1)`；`depth >= MAX_INCLUDE_DEPTH` 直接 return 原 content。
- [ ] 函数 `load_instructions(project_root)` 在 `instructions.py:48-66` 实现：三层优先级（`<root>/CODEBOT.md` → `<root>/.codebot/CODEBOT.md` → `~/.codebot/CODEBOT.md`），每层跑 `process_includes`，多段用 `\n---\n` 拼接，无文件返回 `""`。
- [ ] 边界处理：越界 include 不抛异常（`abs_path.relative_to(resolved_root)` 在 `ValueError` 分支落注释行），测试 `TestProcessIncludes.test_path_outside_project_blocked` 验证。

### 会话存档（codebot/memory/session.py）
- [ ] 模块常量 `SESSIONS_DIR = ".codebot/sessions"` 在 `session.py:14` 定义。
- [ ] 模块常量 `TIME_GAP_THRESHOLD = timedelta(hours=24)` 在 `session.py:15` 定义。
- [ ] 模块常量 `DEFAULT_MAX_AGE_DAYS = 30` 在 `session.py:16` 定义。
- [ ] 模块常量 `TITLE_MAX_LENGTH = 50` 在 `session.py:17` 定义。
- [ ] 枚举 `RecordType(str, Enum)` 在 `session.py:30-35` 定义 5 个值（`system_prompt / user / assistant / tool_result / compression`）。
- [ ] 类 `SessionRecord` dataclass 在 `session.py:38-45` 定义：`type / content / timestamp / tool_use_id / is_error`。
- [ ] 方法 `SessionRecord.to_jsonl()` 在 `session.py:46-56` 实现：`ensure_ascii=False`、可选 `tool_use_id`、仅 tool_result 写 `is_error`。
- [ ] 方法 `SessionRecord.from_jsonl(line)` 在 `session.py:58-70` 实现：`json.JSONDecodeError / KeyError / ValueError` 三类异常都返回 None。
- [ ] 方法 `SessionRecord.from_message(message)` 在 `session.py:72-119` 实现：tool_results 拆多条；assistant + tool_uses 把 text 与 tool_use blocks 内联到 content list；plain user / assistant 走单条。
- [ ] 函数 `records_to_messages(records)` 在 `session.py:122-196` 实现：`pending_tool_results` 队列冲洗到 user message；system_prompt 跳过；compression 渲染 `[摘要]\n<content>`；assistant content list 拆 text 与 tool_uses。
- [ ] 函数 `validate_message_chain(records)` 在 `session.py:199-222` 实现：维护 `pending_tool_uses set`，集合为空时记录前缀长度，最后返回最大完整前缀。
- [ ] 类 `SessionMeta` dataclass 在 `session.py:225-233` 定义 7 字段；`save / load` 在 `session.py:235-266` 实现。
- [ ] 类 `Session` 在 `session.py:271-302` 定义：`append` 内含 `from_message + write + flush + meta` 同步更新逻辑；首条 user 消息截 `TITLE_MAX_LENGTH=50` 写 `meta.title`。
- [ ] 类 `ResumeResult` dataclass 在 `session.py:309-313` 定义（`session / messages / last_active`）。
- [ ] 函数 `_generate_session_id()` 在 `session.py:384-387` 实现：`session_<YYYYMMDD_HHMMSS>_<4 char a-z0-9>`。
- [ ] 类 `SessionManager` 在 `session.py:390-482` 定义：`__init__ / create / list / resume / delete / cleanup` 全部到位；`resume` 内部跑 `validate_message_chain + records_to_messages` 链路。
- [ ] 函数 `build_time_gap_message(last_active)` 在 `session.py:358-380` 实现：`<24h` 返 None；`>=48h` 表达「N 天」否则「N 小时」；含 `代码可能有变更` 文案。

### 自动记忆（codebot/memory/auto_memory.py）
- [ ] 模块常量 `USER_MEMORIES_RELPATH = ".codebot/memories.md"` 在 `auto_memory.py:8` 定义。
- [ ] 模块常量 `PROJECT_MEMORIES_RELPATH = ".codebot/memories.md"` 在 `auto_memory.py:9` 定义。
- [ ] 常量 `MEMORY_EXTRACTION_PROMPT` 在 `auto_memory.py:11-37` 定义：含「用户偏好 / 纠正反馈 / 项目知识 / 参考资料」四类标题、「不要重复添加」、「没有值得记忆的内容，该分类下留空（不要写任何条目，不要写占位符）」、「不要调用任何工具」。
- [ ] 常量 `_USER_LEVEL_HEADERS = {"用户偏好", "纠正反馈"}` 与 `_PROJECT_LEVEL_HEADERS = {"项目知识", "参考资料"}` 在 `auto_memory.py:39-40` 定义。
- [ ] 类 `MemoryManager` 在 `auto_memory.py:43-72` 定义；`user_path / project_path` property 暴露。
- [ ] 方法 `MemoryManager.load()` 在 `auto_memory.py:57-70` 实现：两路径若存在且 strip 非空则收集，`\n\n` 拼接。
- [ ] 方法 `MemoryManager.extract(client, conversation, protocol)` 在 `auto_memory.py:72-126` 实现：取 `history[self._last_extraction_msg_count:]` 增量、构造 prompt、跑 `client.stream` 收 `TextDelta.text`、异常静默 return、成功时推 cursor 并调 `_write_memories`。
- [ ] 方法 `MemoryManager._write_memories(content)` 在 `auto_memory.py:128-161` 实现：按 `### ` 切段、`_assign_section` 分流、写入路径前 `mkdir(parents=True, exist_ok=True)`、`write_text(strip + "\n", utf-8)`。
- [ ] 静态方法 `MemoryManager._is_placeholder(line)` 在 `auto_memory.py:163-166` 实现：剥 `- ` 与空白后命中 `{"", "...", "…", "无", "暂无", "N/A"}` 返回 True。
- [ ] 静态方法 `MemoryManager._assign_section(...)` 在 `auto_memory.py:168-189` 实现：过滤出 `- ` 开头非占位行；按 header 关键字归入 user / project sections。
- [ ] 方法 `MemoryManager.clear()` 在 `auto_memory.py:191-195` 实现：两路径若存在 `write_text("")` 截断（不删文件）。
- [ ] 方法 `MemoryManager.get_display_text()` 在 `auto_memory.py:197-213` 实现：两路径分别按 `[用户级] <path>\n<content>` / `[项目级] ...` 渲染、`\n\n` 拼接；都空返回 `"当前没有任何自动记忆。"`。

### 包级 re-export（codebot/memory/__init__.py）
- [ ] `/Users/codemelo/codebot/codebot/memory/__init__.py` 集中 `from codebot.memory.auto_memory import MemoryManager` / `from codebot.memory.instructions import load_instructions, process_includes` / `from codebot.memory.session import (ResumeResult, Session, SessionManager, SessionMeta, SessionRecord, build_time_gap_message, generate_session_summary, validate_message_chain)`。
- [ ] `__all__` 与导入名一一对应，无遗漏。

### 对话注入（codebot/conversation.py）
- [ ] 字段 `ltm_injected: bool = field(default=False, init=False)` 在 `conversation.py:41` 定义。
- [ ] 方法 `inject_long_term_memory(instructions, memories)` 在 `conversation.py:80-107` 实现：已注入 return；按 `env_injected` 计算 pos；instructions 非空 insert `## 项目指令\n<content>`；memories 非空 insert `## 自动记忆\n<content>`；二者至少一个非空时追加 assistant `"好的，我已了解项目背景和记忆。"` 并置 `ltm_injected=True`。
- [ ] 方法 `replace_history(new_messages)` 在 `conversation.py:109-112` 实现：重置 `env_injected=False / ltm_injected=False`。

### Agent 钩入（codebot/agent.py）
- [ ] 模块常量 `MEMORY_EXTRACTION_INTERVAL = 5` 在 `agent.py:49` 定义。
- [ ] `Agent.__init__` 在 `agent.py:295-329` 接收 `instructions_content: str = ""` / `memory_manager: MemoryManager | None = None`；自存 `self._loop_count = 0` / `self._extracting = False`。
- [ ] `Agent.run` 入口在 `agent.py:399-405` 调 `inject_environment` 之后立即调 `conversation.inject_long_term_memory(self.instructions_content, memory_content)`。
- [ ] loop 收尾分支在 `agent.py:564-568` 触发 `asyncio.ensure_future(self._extract_memories(conversation))`（当 `self._loop_count % MEMORY_EXTRACTION_INTERVAL == 0 and self.memory_manager`）。
- [ ] `_extract_memories(conversation)` 在 `agent.py:870-883` 实现：`self._extracting` 互斥；try-except 包 `memory_manager.extract`，except `log.debug` 不传播；finally 复位 `self._extracting=False`。
- [ ] `manual_compact` 在 `agent.py:902-904` 与 `run_to_completion` 在 `agent.py:924-926` 同样调 `inject_long_term_memory` 完成二次注入。

### 命令处理（codebot/commands/handlers/）
- [ ] `/memory` 命令处理在 `codebot/commands/handlers/memory.py:6-46` 实现：`memory_manager is None` 时提示「记忆管理器未初始化」；空 / `list` 子命令打印 `get_display_text`；`clear` 调用 `MemoryManager.clear` 并提示「所有自动记忆已清空。」；`edit` 打印 user/project 路径；未知子命令打印 usage。
- [ ] `MEMORY_COMMAND = Command(name="memory", description="记忆管理", usage="/memory [list | clear | edit]", type=CommandType.LOCAL, handler=handle_memory)` 在 `memory.py:40-46` 定义。
- [ ] `/session` 命令处理在 `codebot/commands/handlers/session.py` 实现：list / resume / new / delete 子命令；resume 后追加 `build_time_gap_message` 返回值；resume 切换 session/conversation 后复位 `ctx.agent._loop_count = 0`。

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `grep -rn "from codebot.memory" /Users/codemelo/codebot/codebot --include="*.py"` 至少 5 处非测试调用方：
  - `codebot/app.py:51-58` (导入 `MemoryManager / Session / SessionManager / SessionMeta / generate_session_summary / load_instructions`)
  - `codebot/agent.py:24` (`from codebot.memory.auto_memory import MemoryManager`)
  - `codebot/commands/handlers/memory.py` (经 ctx 调用，无直接 import 不强求)
  - `codebot/commands/handlers/session.py` (`from codebot.memory.session import build_time_gap_message`)
- [ ] `grep -rn "MemoryManager\b" /Users/codemelo/codebot/codebot --include="*.py" | grep -v "__init__.py" | grep -v "_test\|/tests/"` 至少 4 处（`app.py / agent.py / commands/handlers/memory.py / memory/auto_memory.py`）。
- [ ] `grep -rn "SessionManager\b" /Users/codemelo/codebot/codebot --include="*.py" | grep -v "__init__.py" | grep -v "/tests/"` 至少 3 处（`app.py / commands/handlers/session.py / memory/session.py`）。
- [ ] `grep -rn "inject_long_term_memory" /Users/codemelo/codebot/codebot --include="*.py" | grep -v "_test\|/tests/"` 至少 4 处（`conversation.py` 定义 + `agent.py` 3 处调用：run 入口 / 压缩后 / run_to_completion）。
- [ ] `grep -rn "MEMORY_EXTRACTION_INTERVAL\|_extract_memories\|_loop_count" /Users/codemelo/codebot/codebot --include="*.py" | grep -v "/tests/"` 命中 `agent.py` 主逻辑。
- [ ] `grep -rn "load_instructions" /Users/codemelo/codebot/codebot --include="*.py" | grep -v "/tests/"` 命中 `app.py:634` 与 `memory/instructions.py / memory/__init__.py`。
- [ ] `grep -rn "build_time_gap_message" /Users/codemelo/codebot/codebot --include="*.py" | grep -v "/tests/"` 命中 `commands/handlers/session.py` 与定义点。
- [ ] 用户输入到本模块的路径可一句话描述：
  - 项目指令：App 启动 → `load_instructions(work_dir)` → `Agent(instructions_content=...)` → `Agent.run` 入口 → `conversation.inject_long_term_memory(instructions, memory_content)` 注入 `## 项目指令` block。
  - 自动记忆：Agent loop 收尾 → `_loop_count += 1` → 模 5 → `asyncio.ensure_future(self._extract_memories)` → `MemoryManager.extract` → `client.stream` 跑 prompt → `_write_memories` 分流写两路 memories.md → 下轮 `Agent.run` 重新 `memory_manager.load()` 注入。
  - 会话存档：用户消息 → `App` → `self.session.append(Message)` → `SessionRecord.from_message` 拆条 → 写 jsonl + flush + meta 同步；`/session resume <id>` → `SessionManager.resume` → `validate_message_chain` 截断 → `records_to_messages` 重建 → `replace_history` → 追加 time gap message。
  - 命令链：`/memory list/clear/edit` → `handle_memory(ctx)` → `MemoryManager.{get_display_text, clear}`；`/session list/resume/new/delete` → `handle_session(ctx)` → `SessionManager.{list, resume, create, delete}`。

## 3. 编译与测试

- [ ] `ruff check codebot/memory/ codebot/agent.py codebot/conversation.py codebot/commands/handlers/memory.py codebot/commands/handlers/session.py` 无错误。
- [ ] `pytest tests/test_memory.py -v` 全部通过，覆盖：
  - `TestProcessIncludes`（no_includes / basic / recursive / depth_limit / path_outside_blocked / file_not_found）
  - `TestLoadInstructions`（single_layer / multi_layer_priority / no_files_returns_empty）
  - `TestSessionRecord`（user_roundtrip / assistant_with_tool_uses / tool_results_multiple_records / malformed_jsonl / plain_assistant）
  - `TestSession`（append_writes_jsonl_and_updates_meta / title_set_from_first_user_message）
  - `TestSessionManager`（create_and_list / delete / cleanup_removes_old_sessions / create_generates_valid_id）
  - `TestValidateMessageChain`（complete_chain / truncate_at_missing_tool_result / empty_records）
  - `TestRecordsToMessages`（basic_roundtrip / tool_result_grouping / system_prompt_skipped）
  - `TestSessionResume`（restores_messages / nonexistent_returns_none / truncates_incomplete_chain）
  - `TestTimeGapMessage`（no_gap_returns_none / gap_returns_message）
  - `TestSessionMeta`（save_and_load / load_invalid_returns_none）
  - `TestMemoryManager`（load_empty / load_merges_user_and_project / clear / get_display_text_empty / write_memories_splits_correctly）
  - `TestConversationInjection`（inject_long_term_memory / inject_idempotent / inject_instructions_only / inject_memories_only / inject_nothing / replace_history_resets_ltm）
  - `TestMemoryExtraction`（extraction_prompt_contains_categories）
- [ ] `python -c "from codebot.memory import MemoryManager, load_instructions, process_includes, Session, SessionManager, SessionMeta, SessionRecord, ResumeResult, build_time_gap_message, generate_session_summary, validate_message_chain; print('ok')"` 打印 `ok`。

## 4. 端到端验证

- [ ] 项目根放一份 `CODEBOT.md`，TUI 启动后通过 `--debug` 日志或临时打印 `conversation.history[0:3]` 能看到 `## 项目指令\n...` 段。
- [ ] `CODEBOT.md` 内写 `@include ./sub/style.md`（sub/style.md 在项目内）能展开；写 `@include ../../etc/passwd` 显示为 `<!-- @include blocked: path outside project -->`；写 `@include ./nonexistent.md` 显示为 `<!-- @include skipped: file not found -->`。
- [ ] TUI 收发若干消息后退出（Ctrl-D），`.codebot/sessions/` 出现一对 `session_*.jsonl` + `session_*.meta`；jsonl 每行为合法 JSON、含 `type/content/timestamp`。
- [ ] 重启 TUI，运行 `/session list` 看到该会话；`/session resume <id>` 后 `conversation.history` 含历史消息，UI 重新渲染过去对话。
- [ ] 把某 session 的 `meta.last_active` 手动改成 25 小时前再 `/session resume <id>`，对话末尾出现「[系统提示] 距离上次会话已过去 25 小时。代码可能有变更，建议在操作前重新读取相关文件。」
- [ ] 把 `last_active` 改成 31 天前，启动时 `self.session_manager.cleanup()` 应当自动删除该会话；`/session list` 不再列出。
- [ ] 跟 Agent 对话 5 轮（每轮包含一次 final assistant 无 tool_call），观察 `~/.codebot/memories.md` 或项目级 `.codebot/memories.md` 出现新行；下次启动后 `conversation.history` 的 `## 自动记忆` block 内包含该内容。
- [ ] `/memory list` 显示 `[用户级] <path>\n...` / `[项目级] ...`；`/memory clear` 后再 `/memory list` 显示「当前没有任何自动记忆。」。
- [ ] 主动构造一个含未结束 tool_use 的 session（手动改 jsonl 删掉对应 tool_result 行），`/session resume` 后 `validate_message_chain` 把 tool_use 截断，恢复出的对话不含未匹配的 tool_use（防止 API 400 invalid_request_error）。

## 5. 文档

- [ ] spec.md / tasks.md / checklist.md 三件套齐全且最新（位于 `/Users/codemelo/codebot/docs/python/ch09/`）。
- [ ] commit 信息标注 `ch09 (python)` 与三件套关闭状态。

```

### Java

```markdown
# ch09: 记忆系统 Spec

## 1. 背景

Coding Agent 在单次会话里能聊得有上下文，但会话结束 ConversationManager 一销毁，所有「用户偏好 / 项目约定 / 重要决策」全部归零，下一次启动得从零开始解释。Claude Code 的 Memory 子系统就是为了解决这个问题：每隔几轮自动从对话里抽出值得记住的事实落盘，下次会话开头再把这些记忆注入到对话最前面，让 Agent 自己「记得」上次聊过什么。本章把这条 memory 流水线落地到 CodeBot Java 版。

## 2. 目标

交付一套自动记忆系统：按 LLM 抽取的事实条目 type 双路存储——`user` / `feedback` 类记忆跟人走，写到用户级 `~/.codebot/memory/auto_memory.json`（跨项目共享）；`project` / `reference` 类记忆跟项目走，写到项目级 `<workDir>/.codebot/memory/auto_memory.json`（仅本仓库）。TUI 在 agent loop 结束时按固定轮次间隔触发后台 LLM 抽取（要求按 4 个 `### user/feedback/project/reference` 段输出，本地解析后按 type 路由到对应文件）；新会话第一条用户消息发出之前，自动把两边记忆合并后作为「Auto Memory」标题注入到 conversation 最前面（user + assistant ack 两条消息）；同时通过 system prompt 的 Memory section 把记忆同步给模型。提供 `loadInstructions` 入口读取项目根 `CODEBOT.md` 或 `.codebot/INSTRUCTIONS.md` 作为 custom instructions，与 memory section 一起拼进系统提示词。TUI 暴露清除入口让用户随时重置记忆（清两个文件）。

## 3. 功能需求

- F1: `MemoryManager(workDir)` 构造时同时计算两个文件路径——`userFilePath = ~/.codebot/memory/auto_memory.json`（取自 `System.getProperty("user.home")`）与 `projectFilePath = <workDir>/.codebot/memory/auto_memory.json`，构造尾部调 `load()` 把两边的已有记忆合并到内存 `entries`。
- F2: `MemoryEntry(content, timestamp, type)` record 作为持久化单元，含可选 `type` 字段（user / feedback / project / reference 之一，旧数据缺失时为 null）；带 `@JsonInclude(NON_NULL)` 与一个 2 元便捷构造子 `MemoryEntry(content, timestamp)`，使 Jackson 既能反序列化旧 JSON（无 type 字段）也能写新数据。Jackson 序列化为 JSON 数组，pretty-printed 写回磁盘。
- F3: `load()` 容错：调用 `loadFile(userFilePath)` 与 `loadFile(projectFilePath)` 分别读取并 append 到 `entries`；任一文件不存在或 JSON 解析失败都不抛出，单边失败不影响另一边。
- F4: `save()` 按 type 拆成 `userScoped`（user / feedback）与 `projectScoped`（project / reference）两个列表，分别通过 `writeJson(path, list)` 写到两个文件；legacy 无 type 的 entry 默认归到项目级；父目录不存在时 `Files.createDirectories` 创建；IOException 静默吞掉（best-effort），不阻塞主流程。
- F5: `getMemories()` 返回当前两个目录合并后的 `content` 字符串列表；`clear()` 把 entries 清空并对两个文件都 `writeJson(path, List.of())`。
- F6: `shouldExtract()` 每次调用自增 `turnCount`，仅在 `turnCount % EXTRACTION_INTERVAL == 0` 时返回 true，对外只暴露这一个判断接口，不让调用方自己 mod。
- F7: `extract(client, conv)` 流程：消息不足 4 条直接返回；把 conversation 序列化为 `[role]: content` 行回放；起一个临时 ConversationManager 加抽取 prompt（明确要求 LLM 按 `### user / ### feedback / ### project / ### reference` 四段输出，并指出每个 type 对应的 scope）；调 `client.stream` 收 TextDelta 串文本；调 `parseTypedSections(text)` 把输出按 `### ` 标题切成 `Map<String, String>`；遍历每个 section，type 不属于 `USER_TYPES ∪ PROJECT_TYPES` 的 silently drop（避免 LLM 幻觉造类）；其余追加 `MemoryEntry(content, now, type)` 并最后调一次 `save()`。
- F8: `injectMemories(conv)` 仅当目标 conversation 为空时生效：把所有 memory（user-level + project-level 合并）拼成 `## Auto Memory\n\n<mem>\n\n` 形式的 user 消息和一条 assistant 确认（`Understood, I'll keep this context in mind.`）写入 conversation。
- F9: `loadInstructions(workDir)` 静态方法依次尝试读 `<workDir>/CODEBOT.md` → `<workDir>/.codebot/INSTRUCTIONS.md`，命中即返回内容，全部失败返回空串。
- F10: `PromptBuilder.BuildOptions` 字段 `memorySection`：非空时以 priority 95 加入系统提示词，与 customInstructions（80）、skillSection（90）共同决定最终 system prompt 装配顺序。
- F11: TUI 主模型 `CodeBotModel.initializeProvider()` 初始化时构造 `new MemoryManager(workDir)`，调用 `loadInstructions` 拿到 custom instructions，调用 `buildMemorySection()` 拿到 memory section（内部已经合并 user / project 两边），三者一起进 `PromptBuilder.BuildOptions`，再走 `PromptBuilder.buildSystemPrompt`。
- F12: TUI 在用户首次发消息（conversation 为空）时调 `memoryManager.injectMemories(conversation)`；在 agent loop 结束（`loopDone`）时调 `triggerMemoryExtraction()` 后台抽取，不阻塞 UI。
- F13: TUI 把 `memoryManager::getMemories` 与 `memoryManager::clear` 通过 `CommandContext` 暴露给 slash 命令（清除入口，操作覆盖两个文件）。

## 4. 非功能需求

- N1: `extract` 必须在虚拟线程里跑（`Thread.startVirtualThread`），绝不能阻塞 TUI 主线程或 agent loop。
- N2: `injectMemories` 只在 conversation 为空时注入，重启同一会话或继续轮次时不能重复堆积「Auto Memory」消息。
- N3: 两个 `auto_memory.json` 都必须 pretty-printed 写回，方便人工 review / 手动编辑。
- N4: `load / save` 全部 IO 异常静默吞掉（包括按目录拆开后的单边失败），绝不向上抛出导致 MemoryManager 构造失败或 save 中断业务流程。
- N5: `MemoryEntry` 的 `timestamp` 用 ISO-8601 `Instant` 字符串（`DateTimeFormatter.ISO_INSTANT.format(Instant.now())`），方便排序与人读。
- N6: `EXTRACTION_INTERVAL = 5`、`MEMORY_DIR = ".codebot/memory"`、`MEMORY_FILE = "auto_memory.json"`、`USER_TYPES = Set.of("user", "feedback")`、`PROJECT_TYPES = Set.of("project", "reference")` 必须是模块级常量，不随工作目录变化。
- N7: 未知 type 的 `### ?` 段在 `parseTypedSections → extract` 流程里被显式 drop，不允许 silently 归入 project 或 user，避免 LLM 幻觉造出 USER_TYPES / PROJECT_TYPES 以外的分类。

## 5. 设计概要

- 核心数据结构:
 - `MemoryManager{workDir, userFilePath, projectFilePath, entries, turnCount}`：每实例绑定一个 workDir，分别持久化到 `~/.codebot/memory/auto_memory.json` 和 `<workDir>/.codebot/memory/auto_memory.json`。
 - `MemoryEntry(content, timestamp, type)` record：单条记忆 + ISO 时间戳 + 可选 type（4 类之一或 null），带 `@JsonInclude(NON_NULL)` + 2 元便捷构造子兼容旧 JSON。
 - 模块级常量：`EXTRACTION_INTERVAL = 5`、`MEMORY_DIR = ".codebot/memory"`、`MEMORY_FILE = "auto_memory.json"`、`USER_TYPES = Set.of("user", "feedback")`、`PROJECT_TYPES = Set.of("project", "reference")`、`MAPPER = new ObjectMapper()`。
- 主流程（启动加载）:
 - `CodeBotModel.initializeProvider()` → `new MemoryManager(workDir)` → 构造器内 `load()` 调 `loadFile(userFilePath) + loadFile(projectFilePath)` 把两个磁盘文件合并读进 `entries`。
 - 同步调 `MemoryManager.loadInstructions(workDir)` 拿 CODEBOT.md / INSTRUCTIONS.md 内容作 customInstructions。
 - `buildMemorySection()` 把两个目录合并后的现有记忆拼成 `# Auto Memory\n\n<mem>\n\n` 字符串。
 - 三者塞进 `PromptBuilder.BuildOptions(customInstructions, null, memorySection)` → `PromptBuilder.buildSystemPrompt` 装配。
- 主流程（首次注入）:
 - 用户在 TUI 敲下第一条消息 → `sendUserMessage()` 看 `conversation.getMessages().isEmpty() && memoryManager != null` → `memoryManager.injectMemories(conversation)` 在用户消息入栈前先放一对「Auto Memory」user + assistant 消息。
- 主流程（后台抽取）:
 - agent loop 完成（`loopDone`）→ `triggerMemoryExtraction()` → `memoryManager.shouldExtract()` 仅在第 5 / 10 / 15 ... 轮返回 true → 虚拟线程跑 `memoryManager.extract(client, conversation)` → 拼回放（含双路 routing 指令）→ `client.stream` 拿到带 `### user/feedback/project/reference` 标题的文本 → `parseTypedSections` 切成 Map → 按 USER_TYPES / PROJECT_TYPES 过滤掉未知 type → `entries.add(new MemoryEntry(content, ts, type))` × N → `save()` 按 type 拆成两个文件分别 `writeJson`。
- 主流程（清除入口）:
 - 用户 slash 命令通过 `CommandContext` 拿到 `memoryClear` Runnable → `memoryManager.clear()` → entries 清空 + 对两个文件都 `writeJson(path, List.of())`。
- 与其他模块的交互:
 - 依赖 `com.codebot.conversation`（Message / ConversationManager）。
 - 依赖 `com.codebot.llm`（LlmClient / StreamEvent）。
 - 被 `com.codebot.prompt.PromptBuilder.BuildOptions` 通过 memorySection 字段消费。
 - 被 `com.codebot.tui.CodeBotModel` 持有、初始化、调度抽取与注入。

## 6. Out of Scope

- 记忆条目的去重 / 合并 / 自动过期；本章 entries 只 append，清理交给 `clear()`。
- 记忆条目的人工编辑 UI（用户直接改 JSON 文件即可）。
- 与 ch08 上下文压缩协同：autoCompact 后是否补充 memory 由 compact 链路自行决定，本章不做。
- 抽取粒度的进一步拆分：当前 LLM 输出每个 `### type` 段的整段文本作为一条 entry 入库；如果未来需要把 bullet 列表拆成多条独立 entry，留给后续迭代。
- 向量检索 / 语义相关性挑选：当前注入是「全部 dump」，不做相关性过滤。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch09: 记忆系统 Tasks

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。本章已课程核对完成，所有 T 任务标记 [x]，每条任务记录实际落地的文件与行号。

## T1: 包结构与 `MemoryEntry` record（含 type）、模块常量
- 影响文件: `src/main/java/com/codebot/memory/MemoryManager.java`（顶部包/imports + record + 常量段）
- 依赖任务: 无
- 完成标准:
 - 包 `com.codebot.memory` 建好；imports 含 `com.fasterxml.jackson.annotation.JsonInclude`、`TypeReference`、`ObjectMapper`、`Locale` 等。
 - `MemoryEntry(String content, String timestamp, String type)` record 带 `@JsonInclude(JsonInclude.Include.NON_NULL)`；附 2 元便捷构造子 `MemoryEntry(content, timestamp)` 委托 3 元构造子并把 type 置 null，确保旧的无 type JSON 反序列化与写入兼容。
 - 模块级常量齐备：`MAPPER`（ObjectMapper）、`EXTRACTION_INTERVAL = 5`、`MEMORY_DIR = ".codebot/memory"`、`MEMORY_FILE = "auto_memory.json"`、`USER_TYPES = Set.of("user", "feedback")`、`PROJECT_TYPES = Set.of("project", "reference")`。

## T2: `MemoryManager` 字段与构造器（双 filePath）
- 影响文件: `src/main/java/com/codebot/memory/MemoryManager.java`
- 依赖任务: T1
- 完成标准: 字段 `userFilePath`、`projectFilePath`、`entries`（默认 `new ArrayList<>()`）、`turnCount` 齐备；构造器接收 `workDir`，把 `projectFilePath` 拼成 `<workDir>/.codebot/memory/auto_memory.json`、`userFilePath` 拼成 `<user.home>/.codebot/memory/auto_memory.json`，最后调 `load()` 合并加载两个文件。

## T3: 持久化 `load` / `save`（按 type 拆双路）
- 影响文件: `src/main/java/com/codebot/memory/MemoryManager.java`
- 依赖任务: T2
- 完成标准:
 - `load()` 把 `entries` 重置后调 `loadFile(userFilePath)` 与 `loadFile(projectFilePath)` 分别追加；`loadFile(path)` 文件不存在直接 return，Jackson `readValue` 失败 silently 不抛（不影响另一边继续）。
 - `save()` 遍历 `entries` 按 `e.type()` 路由：USER_TYPES → `userScoped` 列表；PROJECT_TYPES → `projectScoped`；null 或未知 type → 默认归到 `projectScoped`（向前兼容旧 entry）。最后 `writeJson(userFilePath, userScoped)` + `writeJson(projectFilePath, projectScoped)`。
 - `writeJson(path, list)` 通过 `Files.createDirectories(path.getParent())` 保证父目录；`writerWithDefaultPrettyPrinter` 写回 JSON；IOException 静默吞掉。

## T4: 访问器 `getMemories` / `shouldExtract` / `clear`（覆盖双路）
- 影响文件: `src/main/java/com/codebot/memory/MemoryManager.java`
- 依赖任务: T3
- 完成标准: `getMemories()` 返回 `entries.stream().map(MemoryEntry::content).toList()`（两路合并）；`shouldExtract()` 自增 `turnCount` 并仅在 `% EXTRACTION_INTERVAL == 0` 时返回 true；`clear()` 重置 entries 并对 `userFilePath` / `projectFilePath` 都 `writeJson(path, List.of())`，让两个文件都变成空数组。

## T5: LLM 抽取流程 `extract(client, conv)`（四段输出 + 双路写回）
- 影响文件: `src/main/java/com/codebot/memory/MemoryManager.java`
- 依赖任务: T4
- 完成标准:
 - 消息少于 4 条直接 return；构造 `[role]: content\n` 形式回放。
 - 抽取 prompt 明确要求 LLM 按 4 个 `### user / ### feedback / ### project / ### reference` 段输出，并标注每个 type 对应的 scope（user-level / project-level）；指示模型「Output nothing else」+「skip empty categories」。
 - `client.stream` 收 TextDelta 拼字符串；`StreamEnd / Error` 退出循环。
 - 结果非空时调 `parseTypedSections(text)` 切成 `LinkedHashMap<String, String>`：扫描行，遇到 `### <type>` 设当前 type、清空 buffer；其余行追加到 buffer；遇到下一个 header 或结束时把 trim 后的 body merge 到 map。
 - 遍历 map：未知 type（既不在 USER_TYPES 也不在 PROJECT_TYPES）silently drop，避免 LLM 幻觉造类；其余 append `new MemoryEntry(content, ISO_INSTANT.now(), type)` 并最后调一次 `save()`。
 - `parseTypedSections` 是 package-private static 方法，便于将来加单测。

## T6: 启动时注入 `injectMemories(conv)`
- 影响文件: `src/main/java/com/codebot/memory/MemoryManager.java:123-138`
- 依赖任务: T4
- 完成标准: 空 memory 直接 return；目标 conversation 必须为空才注入；拼接 `## Auto Memory\n\n<mem>\n\n` 文本作为 user 消息；紧跟一条 assistant 消息 `Understood, I'll keep this context in mind.`。

## T7: 静态入口 `loadInstructions(workDir)`
- 影响文件: `src/main/java/com/codebot/memory/MemoryManager.java:142-155`
- 依赖任务: 无
- 完成标准: 依次尝试 `<workDir>/CODEBOT.md` → `<workDir>/.codebot/INSTRUCTIONS.md`；命中返回文件内容；IOException 切换到下一个；全部失败返回空串。

## T8: `PromptBuilder.BuildOptions` 引入 memorySection
- 影响文件: `src/main/java/com/codebot/prompt/PromptBuilder.java:29-32, 108-134`
- 依赖任务: T6
- 完成标准: `BuildOptions(String customInstructions, String skillSection, String memorySection)` record 第 29-32 行落位；`buildSystemPrompt` 在 129-131 行把非空 memorySection 以 priority 95 加入 sections，确保比 customInstructions（80）和 skillSection（90）更靠后输出。

## T9: TUI 初始化挂载 MemoryManager
- 影响文件: `src/main/java/com/codebot/tui/CodeBotModel.java:14, 104, 380-389`
- 依赖任务: T2, T7, T8
- 完成标准: import 第 14 行有 `com.codebot.memory.MemoryManager`；字段 `private MemoryManager memoryManager`（第 104 行）；`initializeProvider()` 内 380 行 `new MemoryManager(workDir)`、383 行 `MemoryManager.loadInstructions(workDir)`、384 行 `buildMemorySection()`、385-388 行装 `BuildOptions` 并调 `PromptBuilder.buildSystemPrompt`。

## T10: TUI 首次注入与 slash 命令暴露
- 影响文件: `src/main/java/com/codebot/tui/CodeBotModel.java`
- 依赖任务: T9
- 完成标准:
 - `CommandContext` 构造把 `memoryManager::getMemories` 与 `memoryManager.clear` 暴露给 slash 命令（`clear` 操作会清两个文件）。
 - `sendUserMessage()` 在 `conversation.getMessages().isEmpty() && memoryManager != null` 时调 `memoryManager.injectMemories(conversation)`。
 - 私有方法 `buildMemorySection()` 拼 `# Auto Memory\n\n<mem>\n\n` 字符串（`mem` 取自 `memoryManager.getMemories()`，已经是双路合并后的列表）。

## T11: 后台抽取调度 `triggerMemoryExtraction`
- 影响文件: `src/main/java/com/codebot/tui/CodeBotModel.java:1137, 1165-1169`
- 依赖任务: T5, T9
- 完成标准: `loopDone` 分支（1137 行）调用 `triggerMemoryExtraction()`；该方法（1165-1169 行）守护 `memoryManager != null && client != null`，再调 `memoryManager.shouldExtract()` 决定是否启动；命中时 `Thread.startVirtualThread(() -> memoryManager.extract(client, conversation))` 不阻塞 UI。

## T12: 端到端验证（双路）
- 影响文件: 无（仅运行验证）
- 依赖任务: T9, T10, T11
- 完成标准:
 - `./gradlew build` 通过。
 - 启动 CodeBot，与 Agent 聊 5 轮以上，让对话覆盖至少一个 user 偏好和一个 project 信息（如「我喜欢函数式」+「项目用 PostgreSQL 15」）；loop 结束后 `~/.codebot/memory/auto_memory.json`（user 条目）与 `<workDir>/.codebot/memory/auto_memory.json`（project 条目）分别出现至少 1 条 `MemoryEntry`（pretty-printed JSON，含 `type` 字段）。
 - 重启 CodeBot，发出第一条消息前，对话顶端能看到 `## Auto Memory` user 消息与 assistant 确认消息各一条，且消息内容包含两个目录的记忆；模型回复体现出对上次会话内容的记忆。
 - 项目根放一份 `CODEBOT.md`，重启后 system prompt 应包含 `# Project Instructions` 段（来自 `loadInstructions`）。
 - 在 TUI 通过 slash 命令清除记忆后，两个 `auto_memory.json` 都变成空数组 `[]`。

## 进度
- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8
- [ ] T9
- [ ] T10
- [ ] T11
- [ ] T12（开发者本机已跑 `./gradlew build` 与端到端记忆抽取/注入验证）

```

```markdown
# ch09: 记忆系统 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性

- [ ] 常量 `EXTRACTION_INTERVAL = 5`、`MEMORY_DIR = ".codebot/memory"`、`MEMORY_FILE = "auto_memory.json"`、`USER_TYPES = Set.of("user", "feedback")`、`PROJECT_TYPES = Set.of("project", "reference")` 在 `src/main/java/com/codebot/memory/MemoryManager.java` 模块级定义。
- [ ] 静态字段 `MAPPER = new ObjectMapper()` 在 `MemoryManager.java` 定义，被 `loadFile / writeJson` 共用。
- [ ] record `MemoryEntry(String content, String timestamp, String type)` 在 `MemoryManager.java` 定义，带 `@JsonInclude(NON_NULL)` 与 2 元便捷构造子（委托 3 元构造，type=null），兼容旧 JSON 反序列化。
- [ ] 字段 `userFilePath / projectFilePath / entries / turnCount` 在 `MemoryManager.java` 定义；`entries` 初始为 `new ArrayList<>()`。
- [ ] 构造器 `MemoryManager(String workDir)` 实现：`userFilePath = <user.home>/.codebot/memory/auto_memory.json`、`projectFilePath = <workDir>/.codebot/memory/auto_memory.json`，调 `load()` 合并加载两边。
- [ ] `load()` 实现：把 `entries` 重置后调 `loadFile(userFilePath)` + `loadFile(projectFilePath)` 分别 append；`loadFile(path)` 文件不存在直接 return，`readValue` 失败 silently（不抛、不影响另一边）。
- [ ] `save()` 实现：遍历 `entries` 按 `type` 路由到 `userScoped`（USER_TYPES）/ `projectScoped`（PROJECT_TYPES，含 null 兼容旧数据）；`writeJson(userFilePath, userScoped)` + `writeJson(projectFilePath, projectScoped)`。
- [ ] `writeJson(path, list)` 实现：`Files.createDirectories(path.getParent())`；`writerWithDefaultPrettyPrinter` 写回；IOException 静默吞掉（best-effort）。
- [ ] `getMemories()` 实现：返回 `entries.stream().map(MemoryEntry::content).toList()`（两路合并）。
- [ ] `shouldExtract()` 实现：先自增 `turnCount`，再返回 `turnCount % EXTRACTION_INTERVAL == 0`。
- [ ] `clear()` 实现：清空 entries 并对 userFilePath / projectFilePath 都 `writeJson(path, List.of())`，让两个文件都变成空数组。
- [ ] `extract(LlmClient, ConversationManager)` 实现：消息 < 4 条 return；拼 `[role]: content\n` 回放；抽取 prompt 要求 LLM 按 `### user / ### feedback / ### project / ### reference` 四段输出（含每个 type 的 scope 说明）；`client.stream` 收 TextDelta 串到 StringBuilder；遇 StreamEnd / Error 退出；调 `parseTypedSections(text)` 切段；按 USER_TYPES / PROJECT_TYPES 过滤掉未知 type 段；每个有效段 append `MemoryEntry(content, ISO_INSTANT.now(), type)`；最后调一次 `save()`。
- [ ] `parseTypedSections(text)` 静态 package-private 方法实现：按行扫描，`### <type>` 开新 section 并把当前 buffer trim 后 merge 到 LinkedHashMap，type 小写化保证后续 set lookup 准确；非 header 行追加到当前 buffer；EOF 时再 flush 一次。
- [ ] `injectMemories(ConversationManager)` 实现：空记忆 return；目标 conversation 为空才注入；拼 `## Auto Memory\n\n<mem>\n\n` 作为 user 消息 + 一条 assistant 确认 `Understood, I'll keep this context in mind.`。
- [ ] 静态方法 `loadInstructions(String workDir)` 实现：依次尝试 `<workDir>/CODEBOT.md`、`<workDir>/.codebot/INSTRUCTIONS.md`；命中返回内容；全部失败返回 `""`。
- [ ] `PromptBuilder.BuildOptions` record 含 `memorySection` 字段。
- [ ] `PromptBuilder.buildSystemPrompt` 把非空 `memorySection` 以 priority 95 加入 builder（高于 customInstructions 的 80 与 skillSection 的 90）。

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `grep -rn "MemoryManager" src/main/java` 至少 3 处非测试调用点：
 - `src/main/java/com/codebot/tui/CodeBotModel.java:14`（import）
 - `src/main/java/com/codebot/tui/CodeBotModel.java:104`（字段声明）
 - `src/main/java/com/codebot/tui/CodeBotModel.java:380`（`initializeProvider` 内 `new MemoryManager(workDir)`）
- [ ] `MemoryManager.loadInstructions(workDir)` 在 `CodeBotModel.java:383` 被调用并塞进 `BuildOptions.customInstructions`。
- [ ] 私有方法 `buildMemorySection()` 在 `CodeBotModel.java:1154-1163` 定义；被 `initializeProvider()`（384 行）调用，结果塞进 `BuildOptions.memorySection`。
- [ ] `memoryManager.injectMemories(conversation)` 在 `CodeBotModel.java:1000-1002` 被调用，且守护条件是 `conversation.getMessages().isEmpty() && memoryManager != null`，防止重复注入。
- [ ] `memoryManager::getMemories` 和 `memoryManager.clear` 通过 `CommandContext`（`CodeBotModel.java:981-982`）暴露给 slash 命令链路。
- [ ] `triggerMemoryExtraction()` 在 `CodeBotModel.java:1165-1169` 定义，被 `loopDone` 分支（1137 行）调用；内部用 `Thread.startVirtualThread` 跑 `extract`，不阻塞 UI 线程。
- [ ] 用户输入到本模块的路径可一句话描述:
 - 启动加载: `CodeBotModel.initializeProvider()` → `new MemoryManager(workDir)` → 构造器内 `load()` 合并加载 user-level + project-level 两个 JSON → memories 进 system prompt（priority 95）。
 - 首次注入: 用户敲第一条消息 → `sendUserMessage()` 判 conversation 空 → `injectMemories(conv)` 写入 user + assistant ack（含双路合并后的记忆）。
 - 后台抽取: agent loop 结束 → `triggerMemoryExtraction()` → `shouldExtract()` 第 5/10/15... 轮 → 虚拟线程 `extract(client, conv)` → LLM 输出 4 段 ### → `parseTypedSections` 切段 → 按 type 路由后 save → 两个 `auto_memory.json` 分别新增条目。
 - 清除: slash 命令调 `memoryClear` Runnable → `MemoryManager.clear()` → 两个文件都落回 `[]`。
- [ ] **死代码核查**：`MemoryManager` 所有 public 方法（构造器 / `getMemories` / `shouldExtract` / `clear` / `extract` / `injectMemories` / `loadInstructions`）在 `CodeBotModel` 中均有调用方；`MemoryEntry`（含可选 `type` 字段）通过 Jackson 间接消费，不是死代码。

## 3. 编译与测试

- [ ] `./gradlew build` 通过。
- [ ] `./gradlew test` 通过（若有覆盖 memory 的单测）。
- [ ] IDE / `./gradlew compileJava` 无 unused import 警告（`MemoryManager` 在 `CodeBotModel.java:14` 真实被使用）。

## 4. 端到端验证

- [ ] 启动 CodeBot 与 Agent 自然聊 5 轮以上，对话至少覆盖一个 user 偏好（如「我喜欢函数式」）和一个 project 信息（如「项目用 PostgreSQL 15」）；loop 结束触发抽取后，`~/.codebot/memory/auto_memory.json`（含 type=user / feedback 的条目）与 `<workDir>/.codebot/memory/auto_memory.json`（含 type=project / reference 的条目）分别出现至少 1 条 `{"content": "...", "timestamp": "...", "type": "..."}` 条目，文件是 pretty-printed JSON。
- [ ] 重启 CodeBot（同一 workDir），发出第一条消息前，对话顶部能看到 `## Auto Memory` 起头的 user 消息 + assistant 一句 `Understood, I'll keep this context in mind.`，消息内容包含两个目录的记忆；模型回答时能引用上次会话提到过的事实。
- [ ] 换到一个新的 workDir 启动 CodeBot，对话顶部仍能看到 user-level 记忆（来自 `~/.codebot/memory/`），但不再看到旧 workDir 的 project 记忆 —— 证明 user/feedback 跨项目共享、project/reference 只在本仓库可见。
- [ ] 项目根放一份 `CODEBOT.md`，重启后 `PromptBuilder.buildSystemPrompt` 输出里能找到 `# Project Instructions` 段（验收方式：用 IDE 调试或在 `LlmClient.create` 前打日志看 systemPrompt 字符串）。
- [ ] 在 TUI 通过 slash 命令调用 `memoryClear` 后，再看两个 `auto_memory.json` 内容都变成空数组 `[]`，下次启动不再注入 Auto Memory 块。
- [ ] 当 conversation 起点已经有消息时，再调一次 `memoryManager.injectMemories(conv)` 不会重复堆叠 Auto Memory（验收：`if (conv.getMessages().isEmpty())` 守护条件）。

## 5. 文档

- [ ] spec.md / tasks.md / checklist.md 三件套齐全且最新（位于 `/Users/codemelo/codebot/docs/java/ch09/`）。
- [ ] commit 信息标注 `ch09` 与三件套关闭状态（验收阶段产物，待用户审阅后随后续 commit 一并打标）。

```



## ch10

```markdown
# 我的初步想法
- 设计一个集中式注册中心，每条命令带名称 / 别名 / 描述 / 用法 / 类型 / 参数提示 / 处理函数等元数据，注册时检测别名冲突
- 解析器识别 `/` 前缀输入，第一个空格之前是命令名、之后是参数；命令名转小写做到大小写不敏感
- 把命令按执行模式分三类：纯本地、影响 UI 状态、需要把预设提示词送进对话流让 AI 处理
- 抽一层 UI 控制接口（显示系统消息、发送用户消息、切换模式、查 token、刷状态等），让命令实现不依赖具体终端渲染框架
- 在用户回车的事件入口加一个分流器：是命令走本地分发，不是命令才送给 AI
- 支持别名和 Tab 补全：单匹配直接补全、多匹配弹列表，隐藏命令不参与
- 状态栏配合显示当前模式和高频命令提示；未知命令统一引导到帮助入口
- 内置一批高频操作：帮助、上下文压缩、清空对话、模式切换（计划/执行）、会话管理、记忆管理、权限管理、综合状态、代码审查等
```

### Go

```markdown
# ch10: Slash Command Spec

## 1. 背景

TUI 需要一种快捷方式让用户在不打扰主对话的前提下触发本地操作（查看状态、清屏、切换模式）、调用既定提示模板，以及自定义脚本。直接用文字让 LLM 转发会浪费 token 且无法即时响应。Slash Command 把这些操作收进 `/<name>` 命名空间，统一注册、补全、解析、分发；缺这个机制要么全部塞进 LLM，要么散落在 TUI 各处用魔法字符串。

## 2. 目标

交付进程内的命令注册表 `commands.Registry`，让 TUI 在用户输入 `/<name> [args]` 时按统一签名调起 handler。内置 helper / status / memory / permission / plan / do / compact / resume / session / skills / review 等核心命令；同时支持用户在多个层级的 commands 目录里放 Markdown 文件扩展私人命令，按 frontmatter 决定元信息、按 `$ARGUMENTS` 决定参数注入；TUI 在输入框响应 `/` 时实时弹补全菜单。

## 3. 功能需求

- F1: `Registry` 暴露注册、查找、列举、补全接口，并维护 alias → canonical name 投射。
- F2: 三类命令类型：本地（handler 返回字符串直接进系统消息）、本地 UI（影响 UI 状态、由 TUI 命中 type 后写专门分支处理）、提示（handler 返回提示词、转给 Agent 当成 user message 发出）。
- F3: `Parse(input)` 把 `/foo bar baz` 拆为名字与剩余参数串，名字小写化，非 `/` 前缀返回空。
- F4: `CreateDefaultRegistry` 注册全部内置命令（help / clear / compact / status / memory / plan / do / session / permission / resume / skills / review）。
- F5: 文件式命令加载器 `LoadDir` 扫描 markdown 文件，递归子目录用 `:` 拼接命名空间；frontmatter 解析 description / argument-hint / aliases；body 内 `$ARGUMENTS` 占位符被替换；缺占位符且有参数时自动追加「用户请求」段。
- F6: `LoadUserCommands(workDir)` 合并多层 commands 目录（用户全局 + 项目 codebot + 项目 claude），后来者覆盖。
- F7: TUI 实时补全：输入 `/` 后用 `Registry.Complete(prefix)` 拉匹配项，方向键选择，Tab 把选项填回输入框，Enter 直接执行。

## 4. 非功能需求

- N1: 命令注册在启动阶段强制无冲突。`Register` 检测到命令名或别名重复直接 panic，让冲突在开发期暴露而不是运行期变成行为不确定；动态来源（如 `LoadUserCommands` 加载的用户命令）必须在调用 `Register` 前通过 `HasConflict` 预检并跳过冲突项。
- N2: 文件加载器必须容忍坏文件：单个 `.md` 解析失败不阻断其他命令加载。
- N3: `Parse` 不能 panic：空串、单纯 `/`、空格分隔、连续空格都要稳定处理。
- N4: handler 签名只依赖传入的 `*Context`，commands 包不 import 内部其他模块；UI 通过闭包把状态桥接进来。

## 5. 设计概要

- 核心数据结构:
 - `commands.Command`：名字、描述、别名列表、类型、参数提示、是否隐藏、Handler
 - `commands.Registry`：命令表 + alias 索引
 - `commands.Context`：handler 唯一入口，承载 args、memory list/clear、token 计数、权限模式读写、工具计数、session 信息、skill 列表、工作目录、模型等闭包钩子
- 主流程:
 1. main 启动 → TUI 构造时调 `CreateDefaultRegistry` 装内置命令
 2. provider 就绪 → TUI 加载 skill catalog，把每个 skill 注册为提示型命令；再调 `LoadUserCommands` 加载文件式命令（已存在的同名命令跳过，让内置 / skill 优先）
 3. 用户敲 `/` → TUI 更新补全菜单（`Registry.Complete`）→ Enter / Tab → TUI `executeCommand(name, args)`
 4. `executeCommand` 按 type 分发：UI 型走 TUI 内置分支（clear / plan / do / compact / resume）；提示型调 handler 拿提示词当 user message 发；本地型调 handler 把返回值显示为系统消息
- 调用链（模块层级）:
 - 输入 `/status` → TUI handleChat → `commands.Parse` → `executeCommand` → `Registry.Find` → 内置 handler → 系统消息
 - 输入 `/git:log`（来自用户 commands 目录）→ 同样路径 → handler 是 `promptHandler(body)` → 返回提示词 → 走 user-message 通路
- 与其他模块的交互:
 - 上行依赖：TUI（注册、补全、分发）、skills（每个 skill 注册成命令）
 - 下行：无（commands 是纯数据包，不 import 任何内部模块）

## 6. Out of Scope

- 命令权限过滤
- 远程 / Bridge 模式下的命令裁剪
- 命令的链式调用 / 管道
- 命令的 fuzzy match：仅按前缀匹配
- 文件 watcher：用户改了 `.md` 不会热更新，必须重启或重新选 provider

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch10: Slash Command Tasks

## T1: 定义命令类型与注册中心
- 影响文件: `internal/commands/commands.go`
- 依赖任务: 无
- 完成标准: `Registry / Command / CommandType / Context / Handler` 在 `commands.go` 实现；`Register / Find / ListCommands / Complete` 行为与参考一致。
- 实际产出: `commands.go:48-104`（Registry 与方法）、`commands.go:9-46`（类型）

## T2: 实现 Parse 与 parseSubcommand
- 影响文件: `internal/commands/commands.go`
- 依赖任务: T1
- 完成标准: `Parse("/foo bar")` 返回 `("foo", "bar")`，`Parse("nothing")` 返回 `("", "")`，name 自动小写化。
- 实际产出: `commands.go:106-118`（Parse）、`commands.go:331-342`（parseSubcommand）

## T3: 注册 11 个内置命令
- 影响文件: `internal/commands/commands.go`
- 依赖任务: T1, T2
- 完成标准: `CreateDefaultRegistry` 返回的 Registry 包含 help、clear、compact、status、memory、plan、do、session、permission、resume、skills、review。
- 实际产出: `commands.go:120-329`（`CreateDefaultRegistry`）

## T4: 文件式命令加载（含 frontmatter / namespacing / $ARGUMENTS）
- 影响文件: `internal/commands/loader.go`
- 依赖任务: T1
- 完成标准: `LoadDir(dir)` 扫描 `.md`，子目录用 `:` 拼名字，frontmatter 解析 description / argument-hint / aliases，body 内 `$ARGUMENTS` 占位符替换；缺占位符且有参数时追加 `## User Request` 段。坏文件不阻断。
- 实际产出: `loader.go:30-54`（LoadDir）、`loader.go:94-127`（parseCommandFile）、`loader.go:133-146`（splitFrontmatter）、`loader.go:164-174`（promptHandler）

## T5: 三层目录合并加载
- 影响文件: `internal/commands/loader.go`
- 依赖任务: T4
- 完成标准: `LoadUserCommands(workDir)` 按 `~/.codebot/commands/` → `<workDir>/.codebot/commands/` → `<workDir>/.codebot/commands/` 顺序加载，后者覆盖前者；返回值稳定有序。
- 实际产出: `loader.go:62-88`

## T6: 单元测试
- 影响文件: `internal/commands/loader_test.go`
- 依赖任务: T4, T5
- 完成标准: 覆盖空目录、单文件、嵌套命名、无 frontmatter 描述回退、`$ARGUMENTS` 缺失追加、aliases 解析、三层合并优先级、跳过非 `.md` 文件。
- 实际产出: `loader_test.go:10-157`（8 个测试用例）

## T7: 接入主流程 —— TUI 注册中心 / 补全 / 分发
- 影响文件: `internal/tui/tui.go`
- 依赖任务: T1-T5
- 完成标准: TUI 持有 `cmdRegistry *commands.Registry`，构造时填默认命令，provider 就绪后追加 skill 命令与用户命令；输入 `/` 触发 `updateSlashMenu`；Enter 走 `executeCommand` 按类型分发。
- 实际产出:
 - 注册: `tui.go:223`（`commands.CreateDefaultRegistry`）、`tui.go:613-621`（skill→command）、`tui.go:625-630`（`commands.LoadUserCommands`）
 - 解析与分发: `tui.go:899`（`commands.Parse`）、`tui.go:1163-1288`（`executeCommand`）
 - 补全: `tui.go:972-1014`（`updateSlashMenu`）、`tui.go:830-869`（菜单键盘交互）
 - Context 构造器: `tui.go:1098-1162`（`buildCommandContext`）

## T8: 端到端验证
- 影响文件: 无
- 依赖任务: T7
- 完成标准: 在 TUI 启动后输入 `/status` 看到 mode/tokens/tools/memories/model/directory 行；输入 `/git:` 后弹补全（假设 `~/.codebot/commands/git/*.md` 存在）。`go build ./...` 通过。
- 实际产出: `loader_test.go` 中 `TestLoadDirSingleFile` / `TestLoadUserCommandsMergesAndOverrides` 已覆盖文件加载的端到端逻辑；TUI 流通过手动测试见 checklist 5.

## 进度
- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8

```

```markdown
# ch10: Slash Command Checklist

## 1. 实现完整性

- [ ] 类型 `Command` 在 `internal/commands/commands.go:38-46` 实现，含 Name/Description/Aliases/Type/ArgPrompt/Hidden/Handler 字段（diff 对照）
- [ ] 类型 `CommandType` 枚举 `TypeLocal / TypeLocalUI / TypePrompt` 在 `commands.go:9-15`
- [ ] 类型 `Context` 在 `commands.go:17-29` 提供 MemoryList / MemoryClear / TokenCount / PermissionMode / SetPermissionMode / ToolCount / SessionInfo / SkillList / WorkDir / Model 闭包字段
- [ ] `Registry.Register / Find / ListCommands / Complete` 在 `commands.go:60-104` 实现，alias→canonical 映射用单独 map
- [ ] `Parse(input)` 在 `commands.go:106-118`：非 `/` 前缀返回空串；name 小写化；空白拆为 (name, args)
- [ ] `CreateDefaultRegistry` 在 `commands.go:120-329` 注册 11 个命令（help/clear/compact/status/memory/plan/do/session/permission/resume/skills/review）—— 实际是 12 个，help 和 review 都各算一个
- [ ] `LoadDir` 在 `internal/commands/loader.go:30-54` 递归扫描 `.md` 文件，子目录 `:` 拼接
- [ ] `LoadUserCommands` 在 `loader.go:62-88` 按 user-global → project-codebot → project-claude 顺序合并
- [ ] `parseCommandFile` 在 `loader.go:94-127`：name 小写、空格转 `-`、frontmatter→meta、缺描述时回退到 body 第一行非标题
- [ ] `splitFrontmatter` 在 `loader.go:133-146`：YAML 解析失败优雅降级
- [ ] `promptHandler` 在 `loader.go:164-174`：`$ARGUMENTS` 占位符替换；缺占位符且 args 非空时追加 `## User Request` 段
- [ ] 边界处理：空 dir、不存在 dir、坏 yaml、单纯 `/` 输入、`SetPermissionMode == nil`（commands.go:271-273）都已覆盖

## 2. 接入完整性

- [ ] `grep -rn "commands.CreateDefaultRegistry" --include="*.go" /Users/codemelo/codebot` 命中 `internal/tui/tui.go:223` 的非测试调用
- [ ] `grep -rn "commands.LoadUserCommands" --include="*.go" /Users/codemelo/codebot` 命中 `tui.go:625`
- [ ] `grep -rn "commands.Parse" --include="*.go" /Users/codemelo/codebot` 命中 `tui.go:899`
- [ ] 注册中心 `cmdRegistry` 字段在 `tui.go:127`，构造时初始化 `tui.go:223`
- [ ] Tab 补全入口在 `tui.go:972-1014`（`updateSlashMenu`），键盘交互在 `tui.go:830-869`
- [ ] 命令分发在 `tui.go:1163-1288`（`executeCommand`），三种 CommandType 都有对应分支
- [ ] Skill→Command 转换在 `tui.go:613-621`，等价于把 skills.Catalog 注册进命令注册中心
- [ ] 用户 `.md` 命令通过 `tui.go:625-630` 后注册（避免覆盖内置 / skill 命令）
- [ ] 入口路径：用户在 TUI 输入 `/<name>` → `handleChat` (tui.go:749) → `Parse` (tui.go:899) → `executeCommand` (tui.go:905) → `Registry.Find` (tui.go:1164) → handler 或 TypeLocalUI 内置 switch

## 3. 编译与测试

- [ ] `cd /Users/codemelo/codebot && go build ./internal/commands/...` 通过
- [ ] `cd /Users/codemelo/codebot && go test ./internal/commands/...` 全部测试通过（`TestLoadDirEmptyOrMissing` / `TestLoadDirSingleFile` / `TestLoadDirNestedNamespacing` / `TestLoadDirNoFrontmatter` / `TestLoadDirHandlerNoPlaceholderWithArgs` / `TestLoadDirHandlerNoPlaceholderNoArgs` / `TestLoadDirAliases` / `TestLoadUserCommandsMergesAndOverrides` / `TestLoadDirSkipsNonMarkdown`）
- [ ] `go vet ./internal/commands/...` 无警告

## 4. 端到端验证

- [ ] 在 TUI 中输入 `/status` 后看到 Status 输出（包含 Mode / Tokens / Tools / Memories / Model / Directory 字段）—— `commands.go:175-190` 的 handler 输出
- [ ] 在 TUI 中输入 `/help` 看到 Available commands 列表 —— `commands.go:128-155` 的 handler
- [ ] 在 TUI 中输入 `/` 弹出补全菜单 —— `tui.go:1010` 设置 `slashMenuOpen=true`
- [ ] 在 TUI 中输入 `/skills` 看到已加载的 skills 列表 —— `commands.go:291-312`
- [ ] 用户在 `~/.codebot/commands/git/log.md` 放 `# Git Log\n\n$ARGUMENTS` 后输入 `/git:log main`，TUI 会把 "Git Log\n\nmain" 当 user message 发给 LLM
- [ ] 留存证据：未提供截图（手动 TUI 验证不在课程验收流程要求范围内）

## 5. 文档

- [ ] `specs/go/ch10/spec.md` 存在
- [ ] `specs/go/ch10/tasks.md` 存在
- [ ] `specs/go/ch10/checklist.md` 存在
- [ ] 已有 commit `a84e3ba feat(ch10): dynamic slash-command loading from user/project directories`

```

### Python

```markdown
# ch10: Slash Command Spec

## 1. 背景

TUI 需要一种快捷方式让用户在不打扰主对话的前提下触发本地操作（清屏、切换 Plan/Do 模式、查看 token 状态、操作 session 与记忆），以及调用既定 prompt 模板。直接把这些诉求丢给 LLM 既浪费 token，也无法即时改变 UI 状态（清屏、Plan 模式开关、permission 切换都不是 LLM 能完成的）。Slash Command 把所有以 `/` 开头的输入收编成命名空间，统一注册、补全、解析、分发；缺少这层框架，要么 if/elif 散落，要么所有动作走 Agent Loop，体验和成本都不可接受。

## 2. 目标

交付进程内的 `CommandRegistry`，让 TUI 在用户敲 `/<name> [args]` 时按统一签名调起 handler。内置 help / clear / compact / plan / do / session / memory / permission / status / skill / review / tasks / trace / worktree 等核心命令；每个命令声明 `type ∈ {LOCAL, LOCAL_UI, PROMPT}`，框架据此决定走系统消息回显、UI 状态改写，还是把 prompt 当成 user message 投回 Agent。Skill 系统在 provider 就绪后把每个 skill 注册为 `PROMPT` 类型命令；TUI 在输入框响应 `/` 时实时弹补全菜单。

## 3. 功能需求

- F1: `CommandRegistry` 暴露 `register / register_sync / find / list_commands` 四个方法，并维护 alias → canonical name 投射；冲突时抛 `ValueError`。
- F2: 三类命令类型由 `CommandType` 枚举：`LOCAL`（handler 显示系统消息）、`LOCAL_UI`（handler 改写 UI 状态，例如清屏 / 切 Plan）、`PROMPT`（handler 调用 `ui.send_user_message`，转给 Agent 当用户消息发出）。
- F3: `parse_command(text)` 把 `/foo bar baz` 拆为 `(name, args, is_command)`：非 `/` 前缀返回 `("", "", False)`；只有 `/` 返回 `("", "", True)`；name 自动小写化。
- F4: `register_all_commands(registry)` 一次性注册 10 个内置命令（help / compact / clear / plan / do / session / memory / permission / status / skill）；其余命令（review / tasks / trace / worktree）由 app 在依赖就绪后通过工厂函数注册。
- F5: `complete(registry, prefix)` 接受 `/abc` 形式前缀，遍历所有非 hidden 命令的 name 与 aliases，按字典序返回所有匹配 `"/" + name` 字符串列表。
- F6: `CommandContext` 是 handler 唯一入参，必须承载 `args / agent / conversation / session / session_manager / memory_manager / ui / config`；`config` 是 dict，托管 registry、skill_loader、skill_executor 等需要回写的闭包钩子。
- F7: `UIController` 协议固定 `add_system_message / send_user_message / set_plan_mode / get_token_count / refresh_status` 五个方法，handler 只通过它跟 UI 交互。
- F8: TUI 监听 `/` 前缀输入：`Tab` 触发 `complete` → `CompletionPopup.show`；`Enter` 走 `_dispatch_command` → `parse_command` → `registry.find` → `cmd.handler(ctx)`。

## 4. 非功能需求

- N1: `CommandRegistry.register` 默认禁止冲突，重复 name / alias 抛 `ValueError`；后续 skill 热重载需要先从 `_commands / _alias_map` 主动剔除旧条目。
- N2: `parse_command` 不能抛异常：空串、`/`、连续空格、前导空格、纯空白都要返回稳定 3 元组。
- N3: `CommandRegistry` 在异步路径使用 `asyncio.Lock` 保护并发注册（为 skill 热重载预留）。
- N4: handler 是 `async` 函数，签名只依赖 `CommandContext`；`codebot.commands` 包不 import TUI 内部模块，反向依赖通过 `config` dict 注入闭包。
- N5: handler 抛异常时由调用方（`_dispatch_command`）捕获并以系统消息形式反馈，单条命令失败不能把 TUI 拉崩。

## 5. 设计概要

- 核心数据结构：
  - `codebot.commands.registry.CommandType`：枚举 `LOCAL / LOCAL_UI / PROMPT`
  - `codebot.commands.registry.Command`：name / description / type / handler / aliases / usage / arg_prompt / hidden
  - `codebot.commands.registry.CommandRegistry`：`_commands` dict + `_alias_map` dict + `asyncio.Lock`
  - `codebot.commands.registry.CommandContext`：args / agent / conversation / session / session_manager / memory_manager / ui / config
  - `codebot.commands.registry.UIController`：`Protocol`，规定 5 个 UI 方法
- 主流程：
  1. `CodeBotApp.__init__` → `self.command_registry = CommandRegistry()` → `register_all_commands(self.command_registry)` 装 10 个内置命令
  2. provider 就绪 → 注册 worktree / tasks / trace 等带依赖的命令 → `register_skill_commands` 把 skill catalog 注册为 `PROMPT` 命令
  3. 用户敲 `/` → `on_chat_input_tab_complete` → `complete(registry, prefix)` → 单命中 inline 填回，多命中弹 `CompletionPopup`
  4. 用户回车 → `on_chat_input_submitted` → `_dispatch_command(text)` → `parse_command` → `registry.find` → 构造 `CommandContext` → `await cmd.handler(ctx)`
  5. handler 按 type 行为分化：`LOCAL` 调 `ui.add_system_message`；`LOCAL_UI` 触发 `set_plan_mode` / `clear_chat` / `set_session` 等 UI 副作用；`PROMPT` 调 `ui.send_user_message` 把构造好的 prompt 发回 Agent
- 调用链（模块层级）：
  - `/status` → `CodeBotApp._dispatch_command` → `parse_command` → `registry.find("status")` → `handle_status(ctx)` → `ctx.ui.add_system_message`
  - `/review fix race condition` → 同样路径 → `handle_review` 拼出 `REVIEW_PROMPT + 额外关注` → `ctx.ui.send_user_message` → 进入 Agent Loop
  - `/<skill_name>` → handler 由 `register_skill_commands.make_handler` 闭包构造 → 调 `SkillExecutor.execute_inline / execute_fork`
- 与其他模块的交互：
  - 上行依赖：`CodeBotApp`（注册、补全、分发）、`SkillLoader`（每个 skill 注册成命令）、`WorktreeManager` / `TaskManager` / `TraceManager`（工厂函数注入依赖）
  - 下行：纯接口包，仅 import `codebot.conversation` / `codebot.permissions` / `codebot.memory.session` 等数据模型；不 import 任何 UI / agent 实现

## 6. Out of Scope

- 命令级权限过滤（命令是否能用由调用方决定，框架不裁剪）
- 文件式 markdown 命令加载（Python 版尚未实现 `LoadDir` / 三层目录合并，由后续章节追加）
- 命令链式调用 / 管道（`/a | /b` 不支持）
- 命令的 fuzzy match（`complete` 只做前缀匹配）
- markdown 命令的文件 watcher / 热更新

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch10: Slash Command Tasks

## T1: 定义命令类型、Context、UI 协议与 Registry
- 影响文件: `codebot/commands/registry.py`
- 依赖任务: 无
- 完成标准: `CommandType / UIController / CommandContext / Command / CommandRegistry` 在 `registry.py` 实现；`register / register_sync / find / list_commands` 行为正确，alias 冲突抛 `ValueError`。
- 实际产出: `registry.py:9-13`（`CommandType`）、`registry.py:15-20`（`UIController`）、`registry.py:23-32`（`CommandContext`）、`registry.py:38-47`（`Command`）、`registry.py:50-94`（`CommandRegistry`）

## T2: 实现 parse_command 与 complete
- 影响文件: `codebot/commands/parser.py`
- 依赖任务: T1
- 完成标准: `parse_command("/foo bar")` 返回 `("foo", "bar", True)`；`parse_command("nothing")` 返回 `("", "", False)`；`parse_command("/")` 返回 `("", "", True)`；name 强制小写。`complete(registry, "/h")` 返回所有 name/alias 命中 `h` 前缀的 `"/" + name` 字典序列表。
- 实际产出: `parser.py:6-16`（`parse_command`）、`parser.py:19-29`（`complete`）

## T3: 实现 10 个内置 handler
- 影响文件: `codebot/commands/handlers/help.py`、`clear.py`、`compact.py`、`plan.py`、`do.py`、`session.py`、`memory.py`、`permission.py`、`status.py`、`skill.py`
- 依赖任务: T1
- 完成标准: 每个 handler 暴露 `async def handle_<name>(ctx)` 与 `<NAME>_COMMAND` 顶层常量；handler 类型与 spec.F4 一致；`/help` 支持 `args = ""` 列出全部、`args` 非空时打印单命令详情。
- 实际产出:
  - `help.py:12-39`（handler）、`help.py:41-48`（HELP_COMMAND，aliases `["h", "?"]`）
  - `clear.py:7-23`（handler）、`clear.py:26-32`（LOCAL_UI）
  - `compact.py:6-23`（handler）、`compact.py:26-33`（aliases `["c"]`）
  - `plan.py:6-10`（handler）、`plan.py:13-19`（LOCAL_UI，aliases `["p"]`）
  - `do.py:6-8`（handler）、`do.py:11-16`（LOCAL_UI）
  - `session.py:8-110`（handler，含 list/resume/new/delete 四个子命令）
  - `memory.py:6-39`（handler，list/clear/edit）
  - `permission.py:11-110`（handler，mode/rules/add/reset）
  - `status.py:11-43`（handler）、`status.py:45-52`（aliases `["s"]`）
  - `skill.py:11-29`（handler，list/info/reload 三个子命令）、`skill.py:84-92`（aliases `["skills"]`）

## T4: register_all_commands 聚合入口
- 影响文件: `codebot/commands/handlers/__init__.py`
- 依赖任务: T3
- 完成标准: `ALL_COMMANDS` 列出 10 个常量；`register_all_commands(registry)` 调用 `register_sync` 逐个注册，无 alias 冲突；模块顶层只 import 不副作用。
- 实际产出: `handlers/__init__.py:15-26`（ALL_COMMANDS）、`handlers/__init__.py:29-31`（register_all_commands）

## T5: 带依赖的命令工厂（review / tasks / trace / worktree / skill_register）
- 影响文件: `codebot/commands/handlers/review.py`、`tasks.py`、`trace.py`、`worktree.py`、`skill_register.py`
- 依赖任务: T1
- 完成标准: 这些命令依赖 `WorktreeManager / TaskManager / TraceManager / SkillExecutor`，必须用工厂函数（`create_*_command` / `register_skill_commands`）在 app 启动后注入；review 是无依赖 PROMPT 命令但保留在工厂层，由调用方决定何时注册。
- 实际产出:
  - `review.py:7-12`（REVIEW_PROMPT）、`review.py:15-19`（handler）、`review.py:22-28`（PROMPT）
  - `tasks.py:65-95`（create_tasks_handler）、`tasks.py:98-106`（create_tasks_command）
  - `trace.py:23-69`（create_trace_command）
  - `worktree.py:13-45`（create_worktree_command）、`worktree.py:48-167`（子命令实现）
  - `skill_register.py:18-95`（register_skill_commands，含 fork/inline 双路径）

## T6: 输入框 Tab 补全 UI 组件
- 影响文件: `codebot/commands/completion.py`
- 依赖任务: T2
- 完成标准: `CompletionPopup` 继承 `textual.containers.Vertical`，dock 在底部；`show(items) / hide()` / `is_visible` 与 `Selected` 消息齐备；点选后发 `Selected(value)` 并自动隐藏。
- 实际产出: `completion.py:9-57`（CompletionPopup）

## T7: 单元测试
- 影响文件: `tests/test_commands.py`
- 依赖任务: T1-T5
- 完成标准: 覆盖 `parse_command` 所有边界（空串、空白、纯 `/`、`/HELP` 大小写、多 args）；`CommandRegistry` 注册 / 查找 / alias / 冲突 / hidden / 异步注册；`complete` 前缀 / alias / hidden 排除 / 无命中；`register_all_commands` 数量 10、aliases 通；`handle_help / handle_plan / handle_do / handle_skill / handle_status / handle_session / handle_memory` 主路径与异常路径。
- 实际产出: `tests/test_commands.py:80-128`（parse）、`128-184`（registry）、`192-227`（complete）、`235-285`（help）、`288-313`（plan/do）、`316-348`（skill）、`352-372`（status）、`376-405`（session）、`408-441`（memory）、`447-477`（register_all）

## T8: 接入主流程 —— CodeBotApp 注册 / 补全 / 分发
- 影响文件: `codebot/app.py`
- 依赖任务: T1-T6
- 完成标准: `CodeBotApp` 持有 `self.command_registry: CommandRegistry`，构造时填默认命令；provider 就绪后追加 worktree / tasks / trace 命令与 skill 命令；输入 `/` 后 `on_chat_input_tab_complete` 触发 `complete`；`on_chat_input_submitted` 走 `_dispatch_command` → `parse_command` → `find` → `cmd.handler(ctx)`。
- 实际产出:
  - 注册: `app.py:554-555`（`self.command_registry = CommandRegistry()` / `register_all_commands`）
  - skill→command: `app.py:687-689`（`register_skill_commands`）
  - worktree 命令: `app.py:708-709`（`create_worktree_command` → `register_sync`）
  - tasks 命令: `app.py:790-791`（`create_tasks_command`）
  - trace 命令: `app.py:793-795`（`create_trace_command`）
  - context 构造器: `app.py:870-888`（`_build_command_context`）
  - 分发: `app.py:900-934`（`_dispatch_command`）
  - 补全入口: `app.py:951-961`（`on_chat_input_tab_complete`）
  - 选中回填: `app.py:970-1014`（`on_completion_popup_selected`）
  - 弹窗组件挂载: `app.py:597`（`yield CompletionPopup()`）

## T9: 端到端验证
- 影响文件: 无
- 依赖任务: T8
- 完成标准: TUI 启动后输入 `/status` 看到 `CodeBot 状态`、`模式 / 会话 / Token / 工具 / 记忆 / 工作目录 / 版本` 全部字段；输入 `/` 弹补全菜单；输入 `/review` 把 `REVIEW_PROMPT` 发回 Agent；`pytest tests/test_commands.py` 与 `ruff check codebot/commands/` 全绿。
- 实际产出: `tests/test_commands.py` 覆盖了 parse / registry / complete / 7 个 handler 与 register_all 的端到端逻辑；TUI 流程通过手动验证（见 checklist 4）。

## 进度
- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8
- [ ] T9

```

```markdown
# ch10: Slash Command Checklist

## 1. 实现完整性

- [ ] `CommandType` 枚举 `LOCAL / LOCAL_UI / PROMPT` 在 `codebot/commands/registry.py:9-13`
- [ ] `UIController` Protocol 暴露 `add_system_message / send_user_message / set_plan_mode / get_token_count / refresh_status` 在 `codebot/commands/registry.py:15-20`
- [ ] `CommandContext` dataclass 含 args/agent/conversation/session/session_manager/memory_manager/ui/config 八字段在 `codebot/commands/registry.py:23-32`
- [ ] `Command` dataclass 含 name/description/type/handler/aliases/usage/arg_prompt/hidden 在 `codebot/commands/registry.py:38-47`
- [ ] `CommandRegistry.__init__` 含 `_commands / _alias_map / _lock = asyncio.Lock()` 在 `codebot/commands/registry.py:50-54`
- [ ] `CommandRegistry.register / register_sync / find / list_commands` 在 `codebot/commands/registry.py:56-94`，alias 冲突抛 `ValueError("conflicts with...")`
- [ ] `parse_command` 在 `codebot/commands/parser.py:6-16`：非 `/` 前缀返回 `("", "", False)`；`/` 返回 `("", "", True)`；name 小写；空白拆 `(name, args)`
- [ ] `complete` 在 `codebot/commands/parser.py:19-29`：剥前导 `/`，遍历非 hidden 命令的 name + aliases，返回字典序 `["/xxx", ...]`
- [ ] 10 个内置 handler 全部存在：`codebot/commands/handlers/{help,clear,compact,plan,do,session,memory,permission,status,skill}.py`，每个文件导出 `handle_<name>` 与 `<NAME>_COMMAND`
- [ ] `register_all_commands` 在 `codebot/commands/handlers/__init__.py:29-31` 注册 10 个常量，无 alias 冲突
- [ ] 带依赖的命令工厂：`create_tasks_command` / `create_trace_command` / `create_worktree_command` / `register_skill_commands` 分别在 `handlers/tasks.py:98-106`、`handlers/trace.py:23-69`、`handlers/worktree.py:13-45`、`handlers/skill_register.py:18-95`
- [ ] `REVIEW_PROMPT` 文案在 `codebot/commands/handlers/review.py:7-12` 含「逻辑错误 / 安全问题 / 性能问题 / 代码风格」四条
- [ ] `CompletionPopup` Textual 组件在 `codebot/commands/completion.py:9-57`，含 `show / hide / Selected` 消息

## 2. 接入完整性

- [ ] `grep -rn "register_all_commands" codebot/ --include="*.py"` 命中 `codebot/app.py:46` 和 `codebot/app.py:555` 的非测试调用
- [ ] `grep -rn "self.command_registry" codebot/app.py | wc -l` 输出 ≥ 8（注册、补全、分发、_build_command_context、skill_register 都引用）
- [ ] `grep -rn "parse_command" codebot/app.py` 命中 `codebot/app.py:901`
- [ ] `grep -rn "from codebot.commands.completion" codebot/app.py` 命中 `codebot/app.py:45`
- [ ] `CodeBotApp.command_registry` 初始化在 `codebot/app.py:554-555`
- [ ] `register_skill_commands` 调用在 `codebot/app.py:687-689`
- [ ] `create_worktree_command` / `create_tasks_command` / `create_trace_command` 注册在 `codebot/app.py:708-709 / 790-791 / 793-795`
- [ ] `CompletionPopup` 挂载在 `codebot/app.py:597`（`yield CompletionPopup()`）
- [ ] Tab 补全入口 `on_chat_input_tab_complete` 在 `codebot/app.py:951-961`，调用 `complete(self.command_registry, event.text)`
- [ ] 命令分发 `_dispatch_command` 在 `codebot/app.py:900-934`，三种 `CommandType` 由 handler 内部决定行为（handler 通过 `ctx.ui` 触发不同副作用）
- [ ] `_build_command_context` 在 `codebot/app.py:870-888`，config dict 包含 `registry / set_session / set_conversation / clear_chat / render_restored / skill_loader / skill_executor`
- [ ] 入口路径：用户在 TUI 输入 `/<name>` → `on_chat_input_submitted` (`app.py:945`) → `_dispatch_command` (`app.py:900`) → `parse_command` (`app.py:901`) → `registry.find` (`app.py:921`) → `cmd.handler(ctx)` (`app.py:932`)

## 3. 编译与测试

- [ ] `cd /Users/codemelo/codebot && ruff check codebot/commands/ tests/test_commands.py` 无错误
- [ ] `cd /Users/codemelo/codebot && pytest tests/test_commands.py -v` 全部测试通过（`TestParseCommand`、`TestCommandRegistry`、`TestComplete`、`TestHelpHandler`、`TestPlanDoHandlers`、`TestSkillHandler`、`TestStatusHandler`、`TestSessionHandler`、`TestMemoryHandler`、`TestRegisterAllCommands` 等 10 个测试类）
- [ ] `pytest tests/test_commands.py::TestParseCommand -v` 8 个测试用例全过（normal / with_args / case_insensitive / only_slash / not_a_command / empty_input / whitespace_input / leading_spaces / multiple_args）
- [ ] `pytest tests/test_commands.py::TestCommandRegistry -v` 验证 alias 冲突、name 冲突、跨字段冲突、async 注册四类异常路径
- [ ] `pytest tests/test_commands.py::TestRegisterAllCommands::test_all_10_commands_registered -v` 验证 `{help, compact, clear, plan, do, session, memory, permission, status, skill}` 全部到位

## 4. 端到端验证

- [ ] 在 TUI 中输入 `/status` 后看到 `CodeBot 状态` 输出（含 `模式 / 会话 / Token / 工具 / 记忆 / 工作目录 / 版本` 字段）—— `codebot/commands/handlers/status.py:11-43` 的 handler 输出
- [ ] 在 TUI 中输入 `/help` 看到 `可用命令：` 列表 —— `codebot/commands/handlers/help.py:31-39` 的 handler
- [ ] 在 TUI 中输入 `/h`（别名）效果等同 `/help` —— `codebot/commands/handlers/help.py:43`（aliases=["h", "?"]）
- [ ] 在 TUI 中输入 `/` 后按 Tab 弹出补全菜单 —— `app.py:951-961` 触发 `popup.show(matches)`
- [ ] 在 TUI 中输入 `/skill list` 看到已加载的 Skill 列表 —— `codebot/commands/handlers/skill.py:31-41`
- [ ] 在 TUI 中输入 `/review fix race condition`，TUI 把 `REVIEW_PROMPT + "\n\n额外关注：fix race condition"` 当 user message 发给 LLM —— `codebot/commands/handlers/review.py:15-19`
- [ ] 在 TUI 中输入 `/plan 设计登录模块`，UI 进入 Plan 模式且立刻把「设计登录模块」当 user message 发出 —— `codebot/commands/handlers/plan.py:6-10`
- [ ] 留存证据：未提供截图（手动 TUI 验证不在课程验收流程要求范围内）

## 5. 文档

- [ ] `docs/python/ch10/spec.md` 存在
- [ ] `docs/python/ch10/tasks.md` 存在
- [ ] `docs/python/ch10/checklist.md` 存在
- [ ] `git log --oneline origin/python -- codebot/commands/` 至少有一条引入 Slash Command 框架的 commit

```

### Java

```markdown
# ch10: Slash 命令系统 Spec（Java 版）

## 1. 背景

TUI 在没有命令机制之前只能做一件事：把输入框里的文本当成用户消息扔给 LLM。但用户在终端里其实有大量「非对话」诉求——查看当前状态、清空会话、压缩上下文、列出记忆、切换权限模式、恢复历史会话、复用预置 Prompt。这些诉求每次都靠自然语言重复表达既费 token 又不可预测。CodeBot 用 Slash 命令解决：以 `/` 开头的输入直接被 TUI 拦截，分发到本地处理器，要么打印一段同步输出，要么直接驱动 TUI 状态切换，要么生成一段 Prompt 注入到对话里。本章把整套机制在 Java 端落地，并把 Skill 注册成动态 `PROMPT` 命令，让用户像调用 `/help` 一样调 `/lark-mail`。

## 2. 目标

交付一套 Slash 命令注册中心：内置 11 个常用命令、提供注册扩展接口、按前缀模糊搜索、TUI 输入区出现「以 `/` 开头」的字符串时自动弹出菜单（上下箭头选中、Enter/Tab 执行、Esc 退出），按命令的 `CommandType` 分别走「本地输出」「TUI 状态切换」「Prompt 注入」三条路径。同时把 SkillCatalog 中的 Skill 自动注册成 `[skill]` 后缀的 `PROMPT` 命令，让 `/lark-mail`、`/spec-prompt` 等技能可以走完全相同的菜单与执行链路。

## 3. 功能需求

### 命令模型

- F1: `Command` 是不可变 `record`，含 `name / description / aliases / type / hidden` 五个字段；提供 `matches(input)` 做精确匹配（含别名）。
- F2: `CommandType` 枚举 3 种：`LOCAL`（同步处理器，返回文本输出）、`LOCAL_UI`（TUI 副作用，无文本输出）、`PROMPT`（生成 Prompt 字符串注入会话）。
- F3: `CommandContext` 是不可变 `record`，封装命令执行所需的运行时上下文（`args / workDir / model / permissionMode / toolCount / tokenCount / memoryList / memoryClear / sessionInfo / skillList`），其中各类信息以 `Supplier` / `Runnable` / `IntSupplier` 懒求值，避免提前查询不必要的状态。

### 注册中心

- F4: `CommandRegistry` 构造时调 `registerDefaults()`，默认注册 11 个命令：`help` / `clear` / `compact` / `status` / `memory` / `plan` / `do` / `session` / `permission` / `resume` / `skills` / `review`。
- F5: `register(cmd, handler)` 同时把 handler 注册到命令名与所有别名上；handler 可为 `null`（仅 `LOCAL_UI` 类型使用）。
- F6: `search(prefix)` 在所有非 hidden 命令上做大小写不敏感前缀匹配（命令名 + 任意别名），结果按命令名升序排列，供菜单弹窗使用。
- F7: `find(name)` 通过 `Command#matches` 在命令清单中精确匹配命令名或别名，返回 `Optional<Command>`。
- F8: `execute(name, ctx)` 是 `LOCAL` / `PROMPT` 的统一执行入口：优先用 `name` 直接命中 handler；命中不到时退化到 `find` 再取规范名 handler；都没有时返回 `"Unknown command: "` 或 `"No handler registered for /…"`。
- F9: `listAll()` 返回所有命令的不可变视图；`listVisible()` 按名字升序返回所有非 hidden 命令，供 `/help` 渲染。

### 内置命令

- F10: `/help [name]`（别名 `h / ?`，`LOCAL`）。无参时输出可见命令清单 + 别名 + 简介 + 末尾「Type /help <command> for details.」；有参时输出对应命令的详情，找不到时回退「Unknown command: <name>」。
- F11: `/clear`（`LOCAL_UI`）。清空 `chatMessages` 并重置 `ConversationManager`。
- F12: `/compact`（别名 `c`，`LOCAL_UI`）。调 `ContextCompactor.forceCompact`，成功时打印 `⟳ <summary>`，失败时打印 `Compact failed: <error>`。
- F13: `/status`（别名 `s`，`LOCAL`）。打印 6 行状态卡片：`Mode / Tokens / Tools / Memories / Model / Directory`。
- F14: `/memory [list|clear]`（`LOCAL`）。无参或 `list` 列出全部 `[type] name — description`（空时输出 `"No memories stored yet."`）；`clear` 调 `ctx.memoryClear()` 后输出 `"All auto-memories cleared."`；其他子命令返回 `"Usage: /memory [list|clear]"`。
- F15: `/plan`（别名 `p`，`LOCAL_UI`）。把 `permChecker` 切到 `PermissionMode.PLAN`，记录前一个模式到 `prePlanMode`，调 `PlanFile.getOrCreatePlanPath` 拿 plan 路径并打印 banner。
- F16: `/do`（`LOCAL_UI`）。把 `permChecker` 还原到 `prePlanMode`（缺省 `DEFAULT`），重置 `PlanFile`，如果 plan 已落地则追加 plan 路径提示。
- F17: `/session [list|info]`（`LOCAL`）。当前两个子命令复用 `ctx.sessionInfo()` 返回当前会话标识；其他子命令返回 `"Usage: /session [list|info]"`。
- F18: `/permission [info|mode|rules]`（别名 `perm`，`LOCAL`）。`info` 打印当前权限模式；`mode` 打印 `"Usage: /permission mode <default|acceptEdits|plan|bypassPermissions>"`；其他子命令统一返回 `"Usage: /permission [info|mode <mode>|rules]"`。
- F19: `/resume`（别名 `r`，`LOCAL_UI`）。读 `SessionManager.listSessions(workDir)` 填 `resumeSessions / resumeFiltered`，把 `state` 切到 `AppState.RESUME` 进入会话恢复选择界面。
- F20: `/skills`（`LOCAL`）。打印 `ctx.skillList()` 内容，空时提示 `"No skills installed.\n\nAdd skills to .codebot/skills/<skill-name>/SKILL.md"`。
- F21: `/review [focus]`（`PROMPT`）。生成「review current git diff」的固定 Prompt，模板含「Logic errors / Security issues / Performance problems / Code style」四个 review 维度；用户传参时拼接 `"Additional focus: …"`。

### Skill 命令

- F22: `wireSkillsToAgent()` 在 Skill 与 Agent 接入完成后调用，把 `SkillCatalog#list()` 中的每个 Skill 注册成 `[skill]` 后缀的 `PROMPT` 命令。
- F23: `registerSkillCommand(name)` 幂等：已注册的命令不重复注册；命令描述为 `meta.description() + " [skill]"`；handler 在执行时再从 `SkillCatalog` 取最新 prompt body。

### TUI 集成

- F24: `CodeBotModel` 保留一个 `cmdRegistry` 单例字段，构造期间通过 `new CommandRegistry()` 初始化。
- F25: `updateSlashMenu()` 监听 `inputBuffer`：当文本以 `/` 开头且尚未出现空格时调 `cmdRegistry.search(prefix)` 填充 `slashMatches`，命中即弹菜单；否则关闭菜单。
- F26: 菜单弹出时上下箭头移动 `slashCursor`，Enter/Tab 选中后调 `executeSlashCommand(cmd, "")`，Esc 关闭菜单；其他字符继续追加到 `inputBuffer` 并重新刷新菜单。
- F27: Enter 提交时若输入以 `/` 开头，按 `/<cmd>[ args]` 切分后通过 `cmdRegistry.find` 命中并执行；未命中时输出 `"Unknown command: /<cmd> — type /help to see available commands"`。
- F28: `executeSlashCommand` 按 `CommandType` 分支：`LOCAL` 把 `cmdRegistry.execute` 的返回值塞进 `chatMessages` 作为 system 行；`LOCAL_UI` 直接执行 TUI 副作用（clear / compact / plan / do / resume）；`PROMPT` 把生成的 prompt + 用户参数依次 `conversation.addUserMessage`，再调 `agent.run` 进入流式回答（命令描述以 `[skill]` 结尾时额外打印一行 `skill(<name>) Successfully loaded skill`）。
- F29: `buildCommandContext(args)` 把 `args` 与当前运行时状态打包成 `CommandContext`，所有字段以 `Supplier` 形式延迟求值。

## 4. 非功能需求

- N1: `Command` / `CommandContext` 必须是 `record`（不可变值类型）。
- N2: 别名匹配在 `register` 阶段写入 `handlers` map，避免每次执行都做线性遍历。
- N3: `search` 与 `find` 不修改命令列表；菜单结果以 `Comparator.comparing(Command::name)` 稳定排序。
- N4: `LOCAL` 处理器返回 `null` 或空串时不向 `chatMessages` 追加，避免出现空 system 行。
- N5: `PROMPT` 命令必须先 `addUserMessage(prompt)` 再 `addUserMessage(args)`，确保 LLM 看到的对话顺序与用户预期一致。
- N6: Skill 注册必须幂等，重复调用 `wireSkillsToAgent` 不会产生重复命令。
- N7: `cmdRegistry` 在 `CodeBotModel` 构造后立即可用，菜单弹出不依赖 Agent / Client 已经初始化。

## 5. 设计概要

### 核心数据结构

- `Command`：`record(String name, String description, String[] aliases, CommandType type, boolean hidden)`，含 `matches` 方法。
- `Command.CommandType`：枚举 `LOCAL / LOCAL_UI / PROMPT`。
- `CommandContext`：`record(args, workDir, model, permissionMode, toolCount, tokenCount, memoryList, memoryClear, sessionInfo, skillList)`，含 `Supplier / Runnable / IntSupplier` 字段。
- `CommandRegistry`：`commands: List<Command>` + `handlers: Map<String, Function<CommandContext, String>>`，构造时调 `registerDefaults`。

### 主流程

- 启动：`CodeBotModel` 构造 → `cmdRegistry = new CommandRegistry()`（含 11 个默认命令）→ Provider 选择完成后 Skill 加载 → `wireSkillsToAgent` 把每个 Skill 注册成 `PROMPT` 命令。
- 输入监听：用户在 `inputBuffer` 输入 `/` → `updateSlashMenu` 触发 → 菜单弹出 → 上下箭头浏览 / Enter 选中。
- 命令执行：Enter 提交 `/<name> <args>` → `cmdRegistry.find(name)` 命中 → `executeSlashCommand(cmd, args)` 按 `type` 分支。
- `LOCAL` 路径：`buildCommandContext(args)` → `cmdRegistry.execute(name, ctx)` → 输出塞进 `chatMessages`。
- `LOCAL_UI` 路径：根据 `cmd.name()` 调对应 TUI 副作用（clear / compact / plan / do / resume）。
- `PROMPT` 路径：`cmdRegistry.execute` 生成 prompt → `conversation.addUserMessage(prompt)` →（如有 args）`addUserMessage(args)` → `agent.run` → 进入 streaming 状态。

### 调用链（模块层级）

- `CodeBot#main` → `CodeBotModel` 构造（`cmdRegistry` 初始化）→ TUI 主循环。
- `CodeBotModel#update` → 键盘事件分发 → `updateSlashMenu` / `executeSlashCommand`。
- `executeSlashCommand` → `CommandRegistry#execute` → 命令 handler → `chatMessages` / `conversation`。
- Skill 加载完成时：`wireSkillsToAgent` → `registerSkillCommand` → `CommandRegistry#register`。

### 与其他模块的交互

- `com.codebot.tui.CodeBotModel`：持有 `cmdRegistry`、维护 `slashMenuOpen / slashMatches / slashCursor` 菜单状态、提供 `buildCommandContext` 与 `executeSlashCommand`。
- `com.codebot.conversation.ConversationManager`：`LOCAL_UI/clear` 重置之；`PROMPT` 通过 `addUserMessage` 写入。
- `com.codebot.compact.ContextCompactor`：`/compact` 调用 `forceCompact`。
- `com.codebot.permission.PermissionChecker`：`/plan` 与 `/do` 切换权限模式。
- `com.codebot.plan.PlanFile`：`/plan` 创建 plan 文件路径、`/do` 重置。
- `com.codebot.session.SessionManager`：`/resume` 通过 `listSessions` 读取历史会话进入 RESUME 状态。
- `com.codebot.memory.MemoryManager`：`CommandContext` 通过 `Supplier` 暴露 `getMemories` / `clear`。
- `com.codebot.skill.SkillCatalog`：Skill 列表渲染（`/skills`）+ 动态注册 Skill 命令。

### 新增文件 / 类清单

新增（位于 `/Users/codemelo/codebot/src/main/java/com/codebot/command/`）：

- `Command.java`：`Command` record + `CommandType` 枚举 + `matches`。
- `CommandContext.java`：`CommandContext` record（10 个字段）。
- `CommandRegistry.java`：`commands / handlers` + `register / search / find / execute / listAll / listVisible` + `registerDefaults`（11 个内置命令）。

修改（位于 `/Users/codemelo/codebot/src/main/java/com/codebot/tui/CodeBotModel.java`）：

- 新增字段 `cmdRegistry / slashMenuOpen / slashMatches / slashCursor`。
- 构造期 `cmdRegistry = new CommandRegistry()`。
- 新增 `wireSkillsToAgent` / `registerSkillCommand` / `updateSlashMenu` / `executeSlashCommand` / `buildCommandContext`。
- `update` 键盘事件中嵌入 slash 菜单导航与 Enter 命令分发。
- `view` 渲染区追加 slash 菜单展示。

## 6. Out of Scope

- 用户自定义命令的硬盘持久化（仅运行期注册）。
- 命令执行的撤销 / 历史回放（执行即生效）。
- 命令参数的复杂解析（仅按首个空格切分 `<cmd>` 与 `args`）。
- 远程命令同步与团队共享。
- 命令权限隔离（所有命令在同一进程内权限相同）。
- Tab 自动补全到部分匹配的最长公共前缀（当前 Tab 直接执行选中命令）。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch10: Slash 命令系统 Tasks（Java 版）

> 任务粒度: 每个任务可在一次会话内完成，可独立交付。所有 T 任务标记 [x]，每条任务记录实际落地的文件与行号。

## T1: Command record 与 CommandType 枚举
- 影响文件: `/Users/codemelo/codebot/src/main/java/com/codebot/command/Command.java:13-46`
- 依赖任务: 无
- 完成标准:
 - `Command` 是 `record(String name, String description, String[] aliases, CommandType type, boolean hidden)`。
 - 内嵌 `CommandType { LOCAL, LOCAL_UI, PROMPT }`，三个值的 Javadoc 与 spec F2 描述对齐。
 - `matches(String input)` 精确比较 `name` 与每一个 `alias`，命中任一即返回 `true`。

## T2: CommandContext record
- 影响文件: `/Users/codemelo/codebot/src/main/java/com/codebot/command/CommandContext.java:11-22`
- 依赖任务: 无
- 完成标准:
 - `CommandContext` 是 `record`，含 10 个字段：`args / workDir / model / permissionMode / toolCount / tokenCount / memoryList / memoryClear / sessionInfo / skillList`。
 - `permissionMode / sessionInfo / model` 使用 `Supplier<String>`；`toolCount` 使用 `IntSupplier`；`tokenCount` 使用 `Supplier<int[]>`；`memoryList / skillList` 使用 `Supplier<List<String>>`；`memoryClear` 使用 `Runnable`。

## T3: CommandRegistry 核心
- 影响文件: `/Users/codemelo/codebot/src/main/java/com/codebot/command/CommandRegistry.java:13-107`
- 依赖任务: T1, T2
- 完成标准:
 - 字段 `commands: ArrayList<Command>` + `handlers: HashMap<String, Function<CommandContext, String>>`。
 - 构造函数调 `registerDefaults()`。
 - `register(cmd, handler)` 把 handler 同时挂到 `name` 与每个 `alias`；handler 为 `null` 时仅追加命令记录。
 - `search(prefix)` 大小写不敏感前缀匹配（命令名 + 任意别名）+ 排除 hidden + `Comparator.comparing(Command::name)` 排序。
 - `find(name)` 通过 `Command#matches` 在命令清单中精确匹配，返回 `Optional<Command>`。
 - `execute(name, ctx)` 三段：直接 `handlers.get(name)` → 失败则 `find` 取规范名 → 都失败返回错误字符串。
 - `listAll()` / `listVisible()` 提供两类视图，`listVisible` 按命令名升序。

## T4: 内置命令注册（help / clear / compact / status / memory）
- 影响文件: `/Users/codemelo/codebot/src/main/java/com/codebot/command/CommandRegistry.java:113-201`
- 依赖任务: T3
- 完成标准:
 - `/help` 别名 `h / ?`，无参时输出可见命令列表 + 末尾 `"Type /help <command> for details."`；有参时输出对应命令详情，未命中返回 `"Unknown command: <name>"`。
 - `/clear` 仅 `LOCAL_UI`，handler 为 `null`。
 - `/compact` 别名 `c`，`LOCAL_UI`，handler 为 `null`。
 - `/status` 别名 `s`，`LOCAL`，输出 `Mode / Tokens / Tools / Memories / Model / Directory` 6 行。
 - `/memory`：无参或 `list` 输出 `[type] name — desc`，空时输出 `"No memories stored yet."`；`clear` 调 `ctx.memoryClear()` 后输出 `"All auto-memories cleared."`；其他子命令输出 `"Usage: /memory [list|clear]"`。

## T5: 内置命令注册（plan / do / session / permission / resume / skills / review）
- 影响文件: `/Users/codemelo/codebot/src/main/java/com/codebot/command/CommandRegistry.java:203-281`
- 依赖任务: T3, T4
- 完成标准:
 - `/plan` 别名 `p`，`LOCAL_UI`，handler 为 `null`。
 - `/do` 仅 `LOCAL_UI`，handler 为 `null`。
 - `/session`：`info`/`list` 都调 `ctx.sessionInfo()`；其他返回 `"Usage: /session [list|info]"`。
 - `/permission` 别名 `perm`：`info` 输出 `"Current permission mode: <mode>"`；`mode` 输出 `"Usage: /permission mode <default|acceptEdits|plan|bypassPermissions>"`；其他统一 `"Usage: /permission [info|mode <mode>|rules]"`。
 - `/resume` 别名 `r`，`LOCAL_UI`，handler 为 `null`。
 - `/skills`：列出 `ctx.skillList()`，空时输出 `"No skills installed.\n\nAdd skills to .codebot/skills/<skill-name>/SKILL.md"`。
 - `/review` `PROMPT` 类型：固定模板含 `"Logic errors / Security issues / Performance problems / Code style"`；有 args 时追加 `"Additional focus: …"`。

## T6: CodeBotModel slash 菜单状态
- 影响文件: `/Users/codemelo/codebot/src/main/java/com/codebot/tui/CodeBotModel.java:106-110, 190`
- 依赖任务: T3
- 完成标准:
 - 新增字段 `cmdRegistry: CommandRegistry` / `slashMenuOpen: boolean` / `slashMatches: List<Command>` / `slashCursor: int`。
 - 构造函数中 `this.cmdRegistry = new CommandRegistry();`。

## T7: CodeBotModel slash 菜单刷新与导航
- 影响文件: `/Users/codemelo/codebot/src/main/java/com/codebot/tui/CodeBotModel.java:637-677, 825-835`
- 依赖任务: T6
- 完成标准:
 - `updateSlashMenu()`：仅当 `inputBuffer` 以 `/` 开头且不含空格时调 `cmdRegistry.search(prefix)`；命中即 `slashMenuOpen=true`。
 - `update` 内菜单导航分支：`up/down` 移动 `slashCursor`；`enter/tab` 选中命令调 `executeSlashCommand(cmd, "")`；`escape` 关闭菜单；其他可见字符追加到 `inputBuffer` 并重新 `updateSlashMenu`。
 - 文本回退（`backspace`）后同步刷新菜单。

## T8: CodeBotModel Enter 命令分发
- 影响文件: `/Users/codemelo/codebot/src/main/java/com/codebot/tui/CodeBotModel.java:712-735`
- 依赖任务: T6, T7
- 完成标准:
 - Enter 提交时若 `inputBuffer` 以 `/` 开头，按首空格切分为 `cmdName + cmdArgs`。
 - `cmdRegistry.find(cmdName)` 命中则清空 `inputBuffer` 并 `executeSlashCommand(cmd.get(), cmdArgs)`。
 - 未命中输出 `"Unknown command: /<cmd> — type /help to see available commands"`。

## T9: CodeBotModel executeSlashCommand 三分支
- 影响文件: `/Users/codemelo/codebot/src/main/java/com/codebot/tui/CodeBotModel.java:866-969`
- 依赖任务: T3, T6
- 完成标准:
 - `LOCAL`：`buildCommandContext(args)` → `cmdRegistry.execute(name, ctx)`；输出非空则塞进 `chatMessages` 作为 `system` 行。
 - `LOCAL_UI`：按 `cmd.name()` switch 到 `clear / compact / plan / do / resume` 对应 TUI 副作用；`clear` 重置 `chatMessages` 与 `ConversationManager`，`compact` 调 `ContextCompactor.forceCompact`，`plan` 切 `PermissionMode.PLAN` 并保存 `prePlanMode`，`do` 还原 `prePlanMode` 并重置 `PlanFile`，`resume` 填充 `resumeSessions` 并切到 `AppState.RESUME`。
 - `PROMPT`：调 `cmdRegistry.execute` 拿 prompt → `conversation.addUserMessage(prompt)` →（args 非空时）`addUserMessage(args)` → 进入 streaming → `agent.run(conversation)`；描述以 `[skill]` 结尾时额外 `Command.println` 一行 `skill(<name>) Successfully loaded skill`。

## T10: CodeBotModel buildCommandContext 工厂
- 影响文件: `/Users/codemelo/codebot/src/main/java/com/codebot/tui/CodeBotModel.java:971-988`
- 依赖任务: T2, T9
- 完成标准:
 - `buildCommandContext(args)` 返回 `new CommandContext(args, workDir, model, …)`。
 - `permissionMode = () -> permChecker != null ? permChecker.getMode().name().toLowerCase() : "default"`。
 - `toolCount = () -> registry != null ? registry.listTools().size() : 0`。
 - `tokenCount = () -> new int[]{totalInput, totalOutput}`。
 - `memoryList = () -> memoryManager != null ? memoryManager.getMemories() : List.of()`。
 - `memoryClear = () -> { if (memoryManager != null) memoryManager.clear(); }`。
 - `sessionInfo = () -> sessionId != null ? "Session: " + sessionId : "No active session"`。
 - `skillList = () -> skillCatalog != null ? skillCatalog.list().stream().map(s -> s.name()).toList() : List.of()`。

## T11: Skill 动态注册成 PROMPT 命令
- 影响文件: `/Users/codemelo/codebot/src/main/java/com/codebot/tui/CodeBotModel.java:500, 511-533`
- 依赖任务: T9
- 完成标准:
 - `wireSkillsToAgent()` 遍历 `skillCatalog.list()` 调 `registerSkillCommand(meta.name())`。
 - `registerSkillCommand` 幂等：`cmdRegistry.find(name).isPresent()` 时直接返回。
 - 注册描述为 `meta.description() + " [skill]"`，`CommandType.PROMPT`。
 - handler 内部按 `captured` 重新从 `skillCatalog.get` 取最新 `promptBody`，未命中返回 `"[skill error] not found: <name>"`。

## T12: CodeBotModel slash 菜单渲染
- 影响文件: `/Users/codemelo/codebot/src/main/java/com/codebot/tui/CodeBotModel.java:1739-1748`
- 依赖任务: T7
- 完成标准:
 - `view` 渲染分隔线之后追加 slash 菜单：最多 8 行，每行 `marker + "/" + cmd.name() + " — " + cmd.description()`。
 - 选中项 marker 为 ` ❯ ` + `Styles.selectedItem`；其他项 marker 为 `   ` + `Styles.normalItem`。
 - 菜单仅在 `slashMenuOpen && !slashMatches.isEmpty()` 时显示。

## T13: 接入主流程（Skill + 默认命令）
- 影响文件: `/Users/codemelo/codebot/src/main/java/com/codebot/tui/CodeBotModel.java:500, 502`
- 依赖任务: T11
- 完成标准:
 - Provider 选择完毕初始化路径中调 `wireSkillsToAgent()`，确保 Skill 命令在 Agent 启动前已注册。
 - 启动失败路径（catch 块）不阻塞 `cmdRegistry` 已经持有的内置命令——`/help`、`/clear` 等仍可使用。

## T14: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T1-T13
- 完成标准:
 - `./gradlew build` 通过。
 - 启动 TUI 输入 `/` 弹出菜单，可见命令至少 11 个（内置）+ Skill 数量。
 - `/help` 输出包含 `clear / compact / status / memory / plan / do / session / permission / resume / skills / review`。
 - `/status` 输出 6 行（Mode / Tokens / Tools / Memories / Model / Directory）。
 - `/memory list` 在 memory 为空时输出 `"No memories stored yet."`；`/memory clear` 后输出 `"All auto-memories cleared."`。
 - `/plan` 切到 plan 模式，banner 提示 plan 文件路径；`/do` 切回 default 模式。
 - 输入 `/lark-mail` 等已安装 Skill 命令，控制台先打印 `skill(lark-mail) Successfully loaded skill`，然后 agent 进入 streaming。
 - 输入 `/notexist` 命中 `"Unknown command: /notexist — type /help to see available commands"`。

## 进度
- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8
- [ ] T9
- [ ] T10
- [ ] T11
- [ ] T12
- [ ] T13
- [ ] T14

```

```markdown
# ch10: Slash 命令系统 Checklist（Java 版）

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。

## 1. 实现完整性

### Command record
- [ ] `Command` 在 `/Users/codemelo/codebot/src/main/java/com/codebot/command/Command.java:13-19` 是 `record(String name, String description, String[] aliases, CommandType type, boolean hidden)`。
- [ ] `CommandType` 在 `Command.java:22-29` 是内嵌枚举，三个值 `LOCAL / LOCAL_UI / PROMPT`，Javadoc 与 spec F2 一致。
- [ ] `matches(String input)` 在 `Command.java:35-45` 精确匹配 `name` 与所有 `alias`，命中任一返回 `true`。

### CommandContext record
- [ ] `CommandContext` 在 `/Users/codemelo/codebot/src/main/java/com/codebot/command/CommandContext.java:11-22` 是 `record`，含 10 个字段。
- [ ] `permissionMode / sessionInfo / model` 为 `Supplier<String>`；`toolCount` 为 `IntSupplier`；`tokenCount` 为 `Supplier<int[]>`；`memoryList / skillList` 为 `Supplier<List<String>>`；`memoryClear` 为 `Runnable`。

### CommandRegistry 核心
- [ ] 字段 `commands / handlers` 在 `/Users/codemelo/codebot/src/main/java/com/codebot/command/CommandRegistry.java:15-16` 定义。
- [ ] 构造函数 `CommandRegistry.java:19-21` 调 `registerDefaults()`。
- [ ] `register(cmd, handler)` 在 `CommandRegistry.java:33-41` 把 handler 同时注册到 `name` 与每个 alias。
- [ ] `search(prefix)` 在 `CommandRegistry.java:47-64` 大小写不敏感前缀匹配 + 排除 hidden + 按命令名升序。
- [ ] `find(name)` 在 `CommandRegistry.java:67-71` 通过 `Command#matches` 命中并返回 `Optional<Command>`。
- [ ] `execute(name, ctx)` 在 `CommandRegistry.java:80-94` 三段：直接 handler → `find` 取规范名 handler → 错误字符串（`"Unknown command: <name>"` / `"No handler registered for /<name>"`）。
- [ ] `listAll()` / `listVisible()` 在 `CommandRegistry.java:97-107` 提供两类视图，`listVisible` 按命令名升序。

### 内置命令注册
- [ ] `/help` 在 `CommandRegistry.java:113-146` 注册，别名 `h / ?`；无参时输出可见命令列表 + 末尾 `"Type /help <command> for details."`；有参时输出对应命令详情；未命中返回 `"Unknown command: <name>"`。
- [ ] `/clear` 在 `CommandRegistry.java:148-153` 注册（`LOCAL_UI`，handler=null）。
- [ ] `/compact` 在 `CommandRegistry.java:155-160` 注册，别名 `c`（`LOCAL_UI`，handler=null）。
- [ ] `/status` 在 `CommandRegistry.java:162-180` 注册，别名 `s`；输出 `Mode / Tokens / Tools / Memories / Model / Directory` 6 行。
- [ ] `/memory` 在 `CommandRegistry.java:182-201` 注册；无参或 `list` 列出全部记忆（空时 `"No memories stored yet."`）；`clear` 调 `ctx.memoryClear()` 后输出 `"All auto-memories cleared."`；其他子命令 `"Usage: /memory [list|clear]"`。
- [ ] `/plan` 在 `CommandRegistry.java:203-208` 注册，别名 `p`（`LOCAL_UI`，handler=null）。
- [ ] `/do` 在 `CommandRegistry.java:210-215` 注册（`LOCAL_UI`，handler=null）。
- [ ] `/session` 在 `CommandRegistry.java:217-230` 注册；`info`/`list` 都调 `ctx.sessionInfo()`；其他返回 `"Usage: /session [list|info]"`。
- [ ] `/permission` 在 `CommandRegistry.java:232-245` 注册，别名 `perm`；`info` 输出 `"Current permission mode: <mode>"`；`mode` 输出 `"Usage: /permission mode <default|acceptEdits|plan|bypassPermissions>"`；其他返回 `"Usage: /permission [info|mode <mode>|rules]"`。
- [ ] `/resume` 在 `CommandRegistry.java:247-252` 注册，别名 `r`（`LOCAL_UI`，handler=null）。
- [ ] `/skills` 在 `CommandRegistry.java:254-265` 注册；空时输出 `"No skills installed.\n\nAdd skills to .codebot/skills/<skill-name>/SKILL.md"`。
- [ ] `/review` 在 `CommandRegistry.java:267-280` 注册（`PROMPT`）；prompt 文本含 `"Logic errors"` / `"Security issues"` / `"Performance problems"` / `"Code style"`；有 args 时附加 `"Additional focus: …"`。

### CodeBotModel 状态字段
- [ ] `cmdRegistry / slashMenuOpen / slashMatches / slashCursor` 在 `/Users/codemelo/codebot/src/main/java/com/codebot/tui/CodeBotModel.java:106-110` 声明。
- [ ] 构造函数中 `CodeBotModel.java:190` 初始化 `this.cmdRegistry = new CommandRegistry();`。

### CodeBotModel 菜单刷新与导航
- [ ] `updateSlashMenu()` 在 `CodeBotModel.java:825-835` 实现：以 `/` 开头且不含空格时调 `cmdRegistry.search(prefix)`，命中弹菜单。
- [ ] 菜单导航分支 `CodeBotModel.java:637-677`：`up/down` 移动 `slashCursor`、`enter/tab` 选中后 `executeSlashCommand(cmd, "")`、`escape` 关菜单、其他字符追加并刷新菜单。
- [ ] `backspace` 路径 `CodeBotModel.java:662-664` 同步 `updateSlashMenu()`。

### CodeBotModel Enter 分发
- [ ] Enter 分支 `CodeBotModel.java:712-735`：以 `/` 开头时按首空格切分；`cmdRegistry.find(cmdName)` 命中即 `executeSlashCommand`；未命中输出 `"Unknown command: /<cmd> — type /help to see available commands"`。

### CodeBotModel executeSlashCommand
- [ ] `executeSlashCommand` 在 `CodeBotModel.java:866-969` 实现三分支。
- [ ] `LOCAL` 分支：`buildCommandContext` → `cmdRegistry.execute` → 非空输出塞进 `chatMessages`（type=`system`）。
- [ ] `LOCAL_UI` 分支：`clear` 重置 `chatMessages` 与 `conversation`；`compact` 调 `ContextCompactor.forceCompact`；`plan` 保存 `prePlanMode` 并切到 `PermissionMode.PLAN`，打印 plan 路径；`do` 还原 `prePlanMode` 并重置 `PlanFile`；`resume` 填充 `resumeSessions` 切到 `AppState.RESUME`。
- [ ] `PROMPT` 分支：`cmdRegistry.execute` 拿 prompt → `conversation.addUserMessage(prompt)` →（args 非空）`addUserMessage(args)` → streaming → `agent.run(conversation)`；描述以 `[skill]` 结尾时附加 `skill(<name>) Successfully loaded skill` 一行。

### CodeBotModel buildCommandContext
- [ ] `buildCommandContext(args)` 在 `CodeBotModel.java:971-988` 构造 `CommandContext`，所有字段使用 `Supplier / Runnable / IntSupplier`，懒求值。

### Skill 动态注册
- [ ] `wireSkillsToAgent()` 在 `CodeBotModel.java:511-516` 遍历 `skillCatalog.list()` 调 `registerSkillCommand`。
- [ ] `registerSkillCommand` 在 `CodeBotModel.java:518-533` 幂等：已注册的命令直接返回。
- [ ] Skill 命令描述为 `meta.description() + " [skill]"`，`CommandType.PROMPT`。
- [ ] Skill handler 内部按 `captured` 重新查 `skillCatalog`，未命中返回 `"[skill error] not found: <name>"`。

### CodeBotModel 渲染
- [ ] slash 菜单渲染在 `CodeBotModel.java:1739-1748`：最多 8 行；选中项 marker ` ❯ `，其他项 `   `；行模板 `marker + "/" + cmd.name() + " — " + cmd.description()`。
- [ ] 菜单仅在 `slashMenuOpen && !slashMatches.isEmpty()` 时显示。

## 2. 接入完整性（必查，杜绝死代码）
- [ ] `grep -rn "com.codebot.command" /Users/codemelo/codebot/src/main/java | grep -v "src/main/java/com/codebot/command/"` 至少命中 `CodeBotModel.java` 4 处（import + slashMatches 字段 + executeSlashCommand 签名 + 注册 Skill 命令）。
- [ ] `grep -rn "cmdRegistry\." /Users/codemelo/codebot/src/main/java | grep -v "CommandRegistry.java"` 在 `CodeBotModel.java` 命中 ≥6 处：`new CommandRegistry()` / `cmdRegistry.find` / `cmdRegistry.search` / `cmdRegistry.execute` / `cmdRegistry.register`（含 Skill 注册）。
- [ ] `grep -n "wireSkillsToAgent\|registerSkillCommand" /Users/codemelo/codebot/src/main/java/com/codebot/tui/CodeBotModel.java` 命中定义点 + 调用点。
- [ ] 用户输入到本模块的路径可一句话描述：
 - 用户输入 `/` → `updateSlashMenu` → `cmdRegistry.search` → 菜单弹出 / 上下箭头浏览 / Enter 执行。
 - 用户输入 `/<name> args` → Enter → `cmdRegistry.find` → `executeSlashCommand` → 按 `CommandType` 三分支处理。
 - Skill 注册：Provider 选择完成 → `wireSkillsToAgent` → `registerSkillCommand` → `cmdRegistry.register`。

## 3. 编译与运行
- [ ] `./gradlew build` 通过。
- [ ] 启动 TUI 输入 `/` 弹出菜单。
- [ ] 命令清单包含 11 个内置命令；已安装 Skill 显示为 `[skill]` 后缀的命令。
- [ ] `/help` 输出可见命令列表 + `"Type /help <command> for details."`。
- [ ] `/help compact` 输出 `/compact — Compress conversation context` + `Aliases: c`。

## 4. 端到端验证
- [ ] 启动 TUI，输入 `/`，菜单出现 ≥11 行命令；按下方向键 5 次后高亮变化；按 Esc 菜单消失。
- [ ] 输入 `/status` 回车，输出包含 `Mode`、`Tokens`、`Tools`、`Memories`、`Model`、`Directory` 6 个标签。
- [ ] 输入 `/memory list`，记忆为空时输出 `No memories stored yet.`；agent 写入若干记忆后再 `/memory list` 输出 `Auto-memories (<N>):` 与列表。
- [ ] 输入 `/memory clear`，输出 `All auto-memories cleared.`，再次 `/memory list` 回到 `No memories stored yet.`。
- [ ] 输入 `/plan`，status bar 中权限模式变为 `plan`，TUI 打印 `Entered Plan mode. Plan file: …`；输入 `/do` 还原。
- [ ] 输入 `/clear`，会话被清空且 `chatMessages` 中只剩 banner / 系统提示；下一条用户消息走全新会话。
- [ ] 输入 `/compact`，触发上下文压缩，TUI 顶部出现 `⟳ <summary>`。
- [ ] 输入 `/resume`，进入 `AppState.RESUME` 列表界面，可看到历史会话；Esc 返回输入界面。
- [ ] 已安装 `lark-mail` Skill 后输入 `/lark-mail` 回车，TUI 立即追加一行 `skill(lark-mail) Successfully loaded skill`，随后 agent 进入 streaming 输出 skill 引导。
- [ ] 输入 `/notexist`，输出 `Unknown command: /notexist — type /help to see available commands`。
- [ ] 输入 `/he` 不回车，菜单只剩 `/help` 一行；继续输入 `lp` 后菜单仍命中 `/help`。
- [ ] 留存证据：`docs/java/ch10/spec.md`、`tasks.md`、`checklist.md` 三件套可重复审查。

## 5. 文档
- [ ] spec.md / tasks.md / checklist.md 三件套齐全且最新（位于 `/Users/codemelo/codebot/docs/java/ch10/`）。
- [ ] commit 信息标注 `ch10` 与三件套关闭状态。

```



## ch11

```markdown
# 我的初步想法
- 单个 Skill 用「YAML frontmatter + Markdown 正文」描述：frontmatter 放元信息（唯一名字、一句话说明、可见工具白名单、执行模式、所用模型、上下文携带策略），正文是发给模型的 SOP 指令
- Skill 存放分三级：项目目录 > 用户目录 > 内置（编译进二进制），同名按优先级覆盖；解析失败的单个文件跳过并记日志，不阻断整体加载
- 两阶段加载：启动时只把所有 Skill 的名字 + 一句说明注入到对话让 Agent 看到；当 Agent 判断要用某个 Skill 时，调一个内置工具把完整指令和专属工具加载进当前会话
- 激活后的完整指令不要塞进普通消息历史，要钉在「环境上下文」里，每轮 Agent Loop 重新构建时它都在最显眼位置；同时激活多个 Skill 时各自的指令并存
- 两种执行模式：一种共享当前对话上下文，执行结果留在主对话历史里；另一种开一条独立对话执行，跑完后把结果摘要回流到主对话；独立模式还能选「全量摘要 / 最近 N 条 / 完全清空」三档来决定要不要带历史进去
- Skill 可以声明可见工具白名单收窄当前能用的工具集，提升模型选择准确率同时落实最小权限；启动时如果白名单里出现不存在的工具就立刻报错（fail-fast）
- 加载 Skill 的那个内置工具属于系统级，不受白名单约束，方便 Skill 之间嵌套触发
- 支持「目录型 Skill」：除了入口 Markdown，还能在同一目录里带自己的工具 schema 和工具实现脚本，整套作为一个可分发的能力包
- Skill 加载完自动注册成 `/<名字>` 短命令出现在帮助里；执行时重新读源文件支持热更新；提供管理子命令查看已加载 Skill、看单个 Skill 详情、强制重新扫描
- 清空对话的命令要顺带把已激活的 Skill 列表也清掉，避免新对话里残留上一次激活的 SOP
- 内置 commit / review / test 三个 Skill 样板（覆盖共享和隔离两种模式）作为生产力工具兼参考模板

# 明确不做（留给后续章节)
- Skill 的市场与分发机制
- Skill 的版本管理
```

### Go

```markdown
# ch11: Skill 系统 Spec

> 本版本按课程「第 11 章 Skill 系统」全量实现。在用户明确选择「按课程版」后，旧版（最小实现）的 Out of Scope 项目被升级为本章功能。

## 1. 背景

CodeBot 用户会反复输入一组类似的 prompt（commit message 规范、代码审查清单、跑测试的项目类型识别）。当前所有 prompt 要么写死在源码 Slash Command（`/review`）里，要么用户每次手敲，三个痛点：(1) 不能复用与分发，(2) 工具一多模型选错的概率指数级上升，(3) 没有任务级的工具白名单和上下文隔离。Skill 把可复用 SOP 装进可编辑的 Markdown 文件，配渐进式披露与执行模式，同时解决上述三个问题。

## 2. 目标

把 SKILL.md 升级为「带 frontmatter + 资源 + 专属工具」的能力包。启动时只把 `name + description` 注入对话给 Agent 看；Agent 通过 LoadSkill 工具按需把完整 SOP 钉到环境上下文，相关专属工具注册进当前会话。inline 模式 SOP 在主对话内执行，fork 模式独立子 Agent 隔离执行后把结果回流。`/<skill-name>` 显式触发与意图识别自动触发共用同一套执行器。

## 3. 功能需求

### 解析与加载
- F1: `SkillMeta` 字段：name / description / when_to_use / tags / allowed_tools / context / mode / model；`mode` 取 `inline | fork`（默认 inline），`context` 取 `full | recent | none`（仅 fork 模式生效，默认 `none`）
- F2: 单文件 `SKILL.md`（YAML frontmatter + body）与目录型（SKILL.md + tool.json + references/）两种磁盘布局
- F3: 三级搜索路径加载，优先级 `项目 .codebot/skills/` > `~/.codebot/skills/` > 内置（go:embed），同名按优先级覆盖；解析失败单条跳过并记日志
- F4: 两阶段加载：阶段 1 启动时只解析 frontmatter（不读 body），阶段 2 由 LoadSkill 工具按需读取 body 与专属工具

### 执行
- F5: `Skill.Render(args)` 把 `$ARGUMENTS` 替换为参数；缺占位符且 args 非空时在末尾追加 `## User Request` 段
- F6: inline 执行：把 SOP 通过 `Agent.ActivateSkill(name, body)` 钉到 env context，下一轮 Agent Loop 起每轮重建时 SOP 都在最显眼位置；同时按 `allowed_tools` 过滤当前会话工具集
- F7: fork 执行：在独立 `conversation.Manager` 里跑临时 Agent，按 `context` 字段决定历史携带策略（full = 主对话摘要 / recent = 最近 5 条 / none = 完全隔离），子 Agent 完成后把最终 assistant 文本作为 assistant 消息回流主对话
- F8: 工具白名单：执行 skill 前过滤 `tools.Registry`，只保留 `allowed_tools` 中声明的工具与系统工具；启动加载阶段做 fail-fast 依赖检查，白名单中出现不存在的工具立刻报错
- F9: 系统工具豁免：`LoadSkill` 标记为 system tool，工具过滤时总是可见，支持 Skill 嵌套调用

### LoadSkill 工具与意图识别
- F10: `LoadSkillTool`：read-only，输入 `{name: string}`；执行三件事——调 `Agent.ActivateSkill` 钉 SOP，注册目录型 skill 声明的专属工具到当前 registry，返回一句简短确认（不返回完整 SOP，避免 tool_result 占用空间）
- F11: 启动期 system prompt 含「可用 Skill 列表」段（只 name + description + LoadSkill 调用指引），通过 prompt builder 的 `SkillSection` 通道注入

### 命令集成
- F12: 每个 skill 自动注册为 `/<name>` 短命令，描述末尾标注 `[skill]`；inline skill 走 TypePrompt 路径，fork skill 走新增的 TypeSkillFork 路径
- F13: `/skill list | info <name> | reload` 管理子命令：list 列出已加载 skill 与来源；info 显示完整 frontmatter 与文件路径；reload 重新扫描三级目录并重建 catalog
- F14: 移除 ch10 硬编码的 `/review` handler，由 review skill 接管

### 目录型 Skill
- F15: 目录布局 `<skill>/SKILL.md` + `<skill>/tool.json` + `<skill>/references/*.go`；tool.json 声明该 skill 专属新增的工具 schema（function calling 兼容），LoadSkill 时把声明的工具注册到当前 registry（实现走 Go 预编译注入，不用 plugin）
- F16: `tool.json` 与 `allowed_tools` 职责分离：tool.json 负责「向 registry 注册新工具」，allowed_tools 负责「skill 执行期间可见工具白名单」；写法上不要重复声明已有内置工具

### 热更新与清理
- F17: 每次 skill 执行时重新读取源文件（仅 body，frontmatter 走启动期缓存），文件修改即时生效；解析失败回退到缓存版本并记日志
- F18: `/clear` 命令在清对话历史时调 `Agent.ClearActiveSkills()` 把激活 skill 列表也清空

### 内置 Skill
- F19: 三个内置 skill 通过 `go:embed` 编译进二进制：`commit`（inline）、`test`（inline）、`backend-interview`（fork, context: none，目录型自带 `parse_resume` 工具）
 - 不包含 `review`：避免与 ch10 硬编码的 `/review` slash command 名字冲突

### 远程安装
- F20: `InstallSkillTool` 让用户把 URL 发给 codebot、由 Agent 自动安装到 `~/.codebot/skills/<name>/`
 - 支持三种 URL：`https://www.skills.sh/<owner>/<repo>/<name>` / `https://github.com/<owner>/<repo>/tree/<ref>/<path>` / `https://raw.githubusercontent.com/.../SKILL.md`
 - 走 GitHub Contents API 递归拉取目录树（无需本地 git），单文件 ≤1 MiB、总大小 ≤8 MiB、文件数 ≤64、深度 ≤4
 - 暂存到兄弟 tempdir，验证含 SKILL.md 后 atomic rename 到位
 - 安装后自动 `Catalog.Reload` + 单条 `registerSkillCommand`，无需 TUI 重启即可 `/<name>` 与 `LoadSkill` 触发

## 4. 非功能需求

- N1: 单个 skill 文件解析失败不能阻断其他 skill 加载，错误走 debug log
- N2: 启动加载阶段（阶段 1）不读 body，确保 1000 个 skill 也能秒级启动
- N3: fork 模式必须隔离 conversation，主对话状态不被子 Agent 修改
- N4: 工具过滤通过 `Agent.ToolNameFilter` 钩子实现，过滤动态生效不要求重启 Agent
- N5: LoadSkill 工具调用不弹权限提示（read-only 类别）
- N6: 内置 skill 与磁盘上同名 skill 冲突时，磁盘版本优先（用户可覆盖内置）

## 5. 设计概要

### 核心数据结构
- `SkillMeta`：扩展 mode / model / context 三个字段
- `Skill`：Meta + PromptBody（懒加载）+ SourceDir + IsDirectory + ToolSchemas（来自 tool.json）
- `Catalog`：name → *Skill；新增 `GetFull(name) (*Skill, error)` 强制重读 body
- `Executor`：`RunInline(ctx, skill, args, ag, conv)` 与 `RunFork(ctx, skill, args, ag, conv) (string, error)`
- `LoadSkillTool`：实现 tools.Tool 接口；持有 *Catalog 与 *Agent 引用，标记 system tool
- Agent 新增字段与方法：`ActiveSkills map[string]string`、`ActivateSkill(name, body)`、`ClearActiveSkills()`、Agent Loop 每轮把 ActiveSkills 注入 system-reminder

### 主流程
1. 启动：TUI `loadSkillsAndBuildPrompt` → `skills.LoadCatalog(workDir)` 三级扫描，每个 skill 只读 frontmatter
2. system prompt 注入：把 catalog 的 `{name, description}` 列表 + LoadSkill 用法说明，通过 SkillSection 喂给 prompt builder
3. 命令注册：每个 skill 注册 `/<name>` 命令；LoadSkillTool 也在启动期注册进 tools.Registry
4. 主 Agent 循环每轮迭代开头：把 `agent.ActiveSkills` 字典的所有 SOP 拼成 system-reminder 注入 conv（与 ch04 的 NotificationFn / Plan Mode reminder 同一通道）
5. 显式调用 `/commit`：handler 调 `Executor.RunInline(commit, args, ag, conv)` → 内部 `ag.ActivateSkill("commit", body)` + 应用工具白名单 ToolNameFilter → 返回 rendered body 作为 user message → Agent loop
6. 意图识别：Agent 调 `LoadSkillTool({name: "commit"})` → 工具执行 `ActivateSkill` + 注册目录型工具 → 返回 `"Skill commit activated. SOP pinned to env. N specialized tools registered."`
7. fork 调用 `/review`：TUI 同步走 `Executor.RunFork` → 新 conv + 过滤 registry + 临时 Agent + Run 到完成 → 把 final text 作为 assistant 消息进主对话
8. `/clear`：清 conv → 调 `ag.ClearActiveSkills()` → 后续轮不再注入旧 SOP

### 调用链
- 启动：main → tui.New → `loadSkillsAndBuildPrompt` → `skills.LoadCatalog` + `register skill commands` + `register LoadSkillTool`
- inline 显式：用户 `/commit` → TUI executeCommand → handler → `Executor.RunInline` → ActivateSkill → user message → Agent loop（每轮 env 注入 SOP + 工具过滤）
- fork 显式：用户 `/review` → TUI executeCommand → handler → `Executor.RunFork`（同步阻塞）→ assistant 消息回流
- 意图触发：Agent 在某轮调用 `LoadSkillTool` → catalog.GetFull → ActivateSkill + register dir tools → 下一轮 SOP 钉在 env 里
- 清理：用户 `/clear` → TUI → conv reset + `ag.ClearActiveSkills`

### 与其他模块的交互
- 上行依赖：TUI（注入 system prompt、注册命令、fork 同步执行、InstallSkill OnInstalled 回调）、Agent（ActiveSkills 字段 + env 注入 + ToolNameFilter）、conversation.Manager（fork 用独立实例）、prompt.builder（SkillSection 通道）、tools.Registry（动态注册目录型工具与 InstallSkillTool）
- 下行：fork 模式调 internal `Agent.Run`，但是 skills 包不直接 import agent 包，通过接口注入（避免循环依赖）；`InstallSkill` 走标准库 `net/http` + GitHub Contents API，不依赖 `git` 二进制

## 6. Out of Scope

- Skill 版本管理 / 升级：`InstallSkill` 重复安装同名 skill 直接覆盖，不做版本号校验或回滚
- 嵌套深度限制：Skill A → LoadSkill(B) → LoadSkill(C) 不做主动限制，依赖 Agent MaxIterations 自然封顶
- fork 嵌套跨 Agent 边界的父子链路记录：留给 ch13 SubAgent
- 目录型 skill 的 Go plugin 动态加载：tool.json 声明的专属工具通过预编译 Go 文件注入而非运行时 plugin（避免 plugin 跨平台问题）；本章内置 `backend-interview` 作为目录型 skill 样板，自带 `parse_resume` 工具的 Go 实现

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch11: Skill 系统 Tasks

> 顺序执行。每完成一个任务跑 `go build ./...` 确保编译过；接入主流程的任务（T11、T12、T13、T14）做完后立刻补一次端到端验证再进下一项。

## T1: 扩展 SkillMeta 字段
- 影响文件: `internal/skills/skills.go`（修改）
- 依赖任务: 无
- 完成标准: SkillMeta 增加 `Mode string`、`Model string`、`Context` 升级为 `inline | fork`（已有），追加 `ForkContext string`（取值 `full | recent | none`）；yaml tag 全部 snake_case
- 备注: 旧 `Context` 字段值 `fork` 等同于 `Mode == "fork"`，做兼容转换

## T2: 拆分 parser 子模块
- 影响文件: `internal/skills/parser.go`（新建）、`internal/skills/skills.go`（修改）
- 依赖任务: T1
- 完成标准: 把 `loadSkill` / `parseSkillMD` 移到 parser.go；新增 `parseFrontmatterOnly(path) (SkillMeta, error)` 不读 body 的轻量解析（阶段 1 加载用）；新增 `loadSkillBody(skill *Skill) error` 强制重读 body
- 备注: parser.go 不依赖 catalog，纯函数

## T3: Catalog 改造为两阶段加载
- 影响文件: `internal/skills/catalog.go`（新建，从 skills.go 抽出）、`internal/skills/skills.go`（修改）
- 依赖任务: T2
- 完成标准: `Catalog` 在阶段 1 只装 frontmatter，每个 Skill 的 PromptBody 默认空；新增 `Catalog.GetFull(name) (*Skill, error)` 触发 loadSkillBody（含热重载逻辑：每次都重读，失败回退缓存）；保留 `Get(name) *Skill` 返回轻量版本
- 备注: `LoadFromDirectory` / `LoadSkills` / `loadInto` 也要适配两阶段

## T4: 内置 skill 嵌入（go:embed）
- 影响文件: `internal/skills/builtins.go`（新建）、`internal/skills/builtins/commit/SKILL.md`、`internal/skills/builtins/review/SKILL.md`、`internal/skills/builtins/test/SKILL.md`、`internal/skills/builtins/backend-interview/SKILL.md` + `tool.json` + `references/parse_resume.go`（新建）
- 依赖任务: T3
- 完成标准: `//go:embed builtins` 嵌入整棵目录，`LoadBuiltins []*Skill` 解析嵌入树返回内置 skill 列表；`LoadSkills(workDir)` 在最后一档加入内置（让磁盘版本覆盖）
- 内置 skill 内容：
 - `commit/SKILL.md`：mode inline / allowed_tools [Bash, ReadFile, Grep]，body 走 git status → diff → conventional commit
 - `review/SKILL.md`：mode fork / forkContext none / allowed_tools [Bash, ReadFile, Grep, Glob]，body 走 5 维度审查
 - `test/SKILL.md`：mode inline / allowed_tools [Bash, ReadFile, Grep, Glob]，body 走项目类型检测 + 跑测试 + 区分 bug
 - `backend-interview/`：目录型，自带 parse_resume 工具

## T5: tool.json 与目录型 skill 工具注册
- 影响文件: `internal/skills/directory.go`（新建）、`internal/skills/builtins/backend-interview/parse_resume.go`（实际实现，从 references/ 引用编译进二进制）
- 依赖任务: T4
- 完成标准: `parseToolJSON(dir) ([]ToolSchema, error)` 读取 tool.json 校验 function calling schema；`RegisterDirectoryTools(skill, registry) (int, error)` 把目录型 skill 声明的工具实例化并注册进 registry，返回数量；找不到对应实现时记 warning 不阻断
- 备注: parse_resume 实现走预编译 Go（在 builtins 同目录），不走 plugin

## T6: Agent ActiveSkills 字段与方法
- 影响文件: `internal/agent/agent.go`（修改）
- 依赖任务: 无（与 T1-T5 并行可做）
- 完成标准: Agent 新增 `ActiveSkills map[string]string`（name → body）；方法 `ActivateSkill(name, body string)`、`ClearActiveSkills`、`GetActiveSkills map[string]string`；Run 主循环每轮迭代开头（在 NotificationFn 注入之后），如 ActiveSkills 非空则拼成一段 system-reminder 注入 conv（标题用 `# Active Skills`）

## T7: Executor.RunInline
- 影响文件: `internal/skills/executor.go`（新建）
- 依赖任务: T3, T6
- 完成标准: `RunInline(ctx, skill, args, agentRef SkillHost) (string, error)`：调用 `skill.Render(args)` 渲染 body → `host.ActivateSkill(skill.Meta.Name, body)` → 对 allowed_tools 做 fail-fast 校验（缺工具立即返回 error）→ 把工具白名单设置到 host.SetToolFilter（封装 Agent.ToolNameFilter）→ 返回 rendered body（作为 user message 走主 loop）
- 备注: 新增 interface `SkillHost { ActivateSkill / SetToolFilter / GetTool(name) }` 实现在 Agent 上，避免 skills 包 import agent

## T8: Executor.RunFork
- 影响文件: `internal/skills/executor.go`（继续）
- 依赖任务: T7
- 完成标准: `RunFork(ctx, skill, args, host SkillForkHost) (summary string, err error)`：
 - 创建新 `conversation.Manager`
 - 按 `skill.Meta.ForkContext` 装填初始历史：`full` 取主对话 last N 条做 LLM 摘要 / `recent` 拷最近 5 条 / `none` 空
 - 把 rendered body 作为 first user message
 - 通过 `SkillForkHost.RunSubAgent(conv, allowedTools) (finalText string, err error)` 跑临时 Agent（实现在 TUI 层注入，复用 agent.Agent.Run + 收集 LoopComplete 文本）
 - 返回 finalText

## T9: LoadSkillTool（系统工具）
- 影响文件: `internal/tools/load_skill.go`（新建）
- 依赖任务: T3, T5, T6
- 完成标准: `LoadSkillTool` 实现 tools.Tool，Name = `LoadSkill`，Category = read；持有 `*skills.Catalog` + `SkillHost`；Execute：catalog.GetFull → host.ActivateSkill → 目录型 skill 调 RegisterDirectoryTools → 返回 `"Skill <name> activated. SOP pinned to env. N specialized tools registered."`；标记 SystemTool 接口让 ToolNameFilter 始终放行
- 备注: tools.Tool 接口增加可选 `SystemTool bool` 检测；Agent 的 ToolNameFilter 应用时绕过 system tool

## T10: 系统工具豁免逻辑
- 影响文件: `internal/tools/tool.go`（修改）、`internal/agent/agent.go`（修改）
- 依赖任务: T9
- 完成标准: 新增 `SystemTool` 接口（可选实现），Agent.applyToolFilter 在调 ToolNameFilter 前先 check 是否系统工具；GetAllSchemas 也要保留系统工具
- 备注: LoadSkillTool 与未来其他系统工具的统一通道

## T11: 接入 TUI —— skill 列表与命令注册
- 影响文件: `internal/tui/tui.go`（修改）、`internal/prompt/builder.go`（保留 SkillSection 通道）
- 依赖任务: T3, T4, T7, T8, T9
- 完成标准:
 - `loadSkillsAndBuildPrompt` 调用新 `skills.LoadCatalog`（两阶段），catalog 存到 m.skillCatalog
 - system prompt SkillSection 改成「Available Skills (call LoadSkill to activate)\n- /<name>: <description>\n...」+ LoadSkill 使用说明
 - 每个 skill 注册命令：inline 走 TypePrompt（handler 调 Executor.RunInline），fork 走新增 TypeSkillFork（handler 直接调 Executor.RunFork 并把返回值作为 assistant 消息插入对话）
 - 注册 LoadSkillTool 到 m.registry：`m.registry.Register(&tools.LoadSkillTool{Catalog: catalog, Host: m.ag})`

## T12: 接入 TUI —— /skill 管理命令与 /clear 集成
- 影响文件: `internal/commands/commands.go`（修改）、`internal/tui/tui.go`（修改）
- 依赖任务: T11
- 完成标准:
 - 新增 `/skill` 命令：`/skill list` → ctx.SkillCatalog.List 含来源；`/skill info <name>` → 全 frontmatter + path；`/skill reload` → catalog.Reload(workDir) + 重新注册命令
 - `/clear` handler 增加 `if m.ag != nil { m.ag.ClearActiveSkills }`
 - 删除 ch10 commands.go:314-326 的硬编码 `/review` 注册（被 review skill 接管）

## T13: 新增 TypeSkillFork 命令类型
- 影响文件: `internal/commands/commands.go`（修改）、`internal/tui/tui.go`（修改）
- 依赖任务: T8, T11
- 完成标准: `TypeSkillFork CommandType = "skill-fork"`；executeCommand 增加 case：调 handler 后把返回的 summary 作为 chatMessage（role=assistant）插入；不触发主 Agent loop

## T14: 接入主流程 —— Agent 注入 SkillHost
- 影响文件: `internal/agent/agent.go`（修改）、`internal/tui/tui.go`（修改）
- 依赖任务: T6, T7, T8
- 完成标准: Agent 实现 SkillHost 接口（ActivateSkill / ClearActiveSkills / SetToolFilter）；TUI 把 m.ag 强转为 skills.SkillHost 传给 Executor 与 LoadSkillTool；fork 路径需要 SkillForkHost.RunSubAgent，由 TUI 提供一个本地实现（开 streaming executor 跑到 LoopComplete 收集最终 assistant 文本）

## T14b: InstallSkillTool（远程安装）
- 影响文件: `internal/skills/install.go`（新建）、`internal/skills/install_tool.go`（新建）、`internal/skills/install_test.go`（新建）、`internal/tui/tui.go`（修改）
- 依赖任务: T3（Catalog.Reload）、T11（registerSkillCommand 抽出）
- 完成标准:
 - `ParseSkillURL(url) (*SkillSource, error)` 支持 skills.sh / github.com tree / raw.githubusercontent.com 三种 URL，拒绝其他 host
 - `Install(src, installRoot) (*InstallReport, error)` 走 GitHub Contents API 递归下载到 staging temp dir，验证含 `SKILL.md` 或 `skill.yaml` 后 atomic rename
 - 限额：单文件 ≤1 MiB、总大小 ≤8 MiB、文件数 ≤64、深度 ≤4
 - `InstallSkillTool` 实现 `tools.Tool`，Name = `InstallSkill`，Category = write；执行后调 `Catalog.Reload` + `OnInstalled(name)` 回调
 - TUI `wireSkillsToAgent` 把 `registerSkillCommand` 抽成可单独调用的方法，作为 OnInstalled 回调
 - SkillSection 文本告知模型「用户给 URL 要求装 skill 时调 InstallSkill」
- 备注: 不依赖本地 `git` 二进制；rate limit 命中（403）时把 GitHub 的错误文本透出给用户

## T15: 单元测试
- 影响文件: `internal/skills/skills_test.go`（修改）、`internal/skills/executor_test.go`（新建）、`internal/skills/directory_test.go`（新建）、`internal/tools/load_skill_test.go`（新建）、`internal/agent/agent_test.go`（修改）
- 依赖任务: T1-T14
- 完成标准: 覆盖
 - parser 两阶段：阶段 1 不读 body / 阶段 2 重读热更新
 - 三级覆盖：磁盘版本盖内置版本
 - Executor.RunInline 钉 SOP + 工具过滤 fail-fast
 - Executor.RunFork 隔离 conv + context: full/recent/none 三档
 - LoadSkillTool 端到端：activate + register dir tools + 简短返回
 - Agent.ActivateSkill / ClearActiveSkills / env 注入
 - 系统工具豁免：ToolNameFilter 设了 LoadSkill 也还在 schema 里
 - tool.json 解析与目录型工具注册
 - /skill list / info / reload 行为
 - /clear 触发 ClearActiveSkills

## T16: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T15
- 完成标准:
 - `go build ./...` 通过
 - `go test ./...` 全过
 - 在仓库根目录 TUI 实操：
 1. 启动看 `/help` 列出 commit / review / test [skill] / backend-interview [skill]
 2. `/skill list` 看到 4 个 skill 与来源
 3. `/skill info commit` 看到完整 frontmatter
 4. 改一处源码后 `/commit` 看到 Agent 走 git status → diff → commit
 5. `/review` 走 fork 路径，主对话不被污染，最后收到 assistant 摘要
 6. 「帮我准备一下后端面试」自然语言触发 LoadSkill("backend-interview")
 7. `/clear` 后 env-reminder 不再出现旧 SOP
 - 截图或日志留证

## 进度
- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8
- [ ] T9
- [ ] T10
- [ ] T11
- [ ] T12
- [ ] T13
- [ ] T14
- [ ] T14b
- [ ] T15
- [ ] T16

```

```markdown
# ch11: Skill 系统 Checklist

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。本版本贴合课程「全量按课程版」目标，**不**沿用旧版「验收」流程。

## 1. 实现完整性

### 1.1 解析与加载
- [ ] `SkillMeta` 在 `internal/skills/skills.go` 含字段 Name / Description / WhenToUse / Tags / AllowedTools / Context / Mode / Model / ForkContext，yaml tag 全部 snake_case
- [ ] `parseFrontmatterOnly(path) (SkillMeta, error)` 在 `internal/skills/parser.go` 实现，**不**读取 body
- [ ] `loadSkillBody(skill *Skill) error` 在 `internal/skills/parser.go` 实现，强制重读源文件（热重载）
- [ ] `Catalog.GetFull(name) (*Skill, error)` 在 `internal/skills/catalog.go` 实现，每次调用触发 `loadSkillBody`；解析失败回退缓存 + 记 debug log
- [ ] `Catalog.Reload(workDir) error` 在 `internal/skills/catalog.go` 实现
- [ ] 三层加载顺序在 `LoadCatalog(workDir)`：项目 `.codebot/skills/` > `~/.codebot/skills/` > 内置 embed；同名按优先级覆盖

### 1.2 内置 skill
- [ ] `internal/skills/builtins/commit/SKILL.md` 存在，frontmatter `mode: inline / allowed_tools: [Bash, ReadFile, Grep]`
- [ ] `internal/skills/builtins/review/SKILL.md` 存在，frontmatter `mode: fork / fork_context: none / allowed_tools: [Bash, ReadFile, Grep, Glob]`
- [ ] `internal/skills/builtins/test/SKILL.md` 存在，frontmatter `mode: inline / allowed_tools: [Bash, ReadFile, Grep, Glob]`
- [ ] `internal/skills/builtins/backend-interview/` 含 SKILL.md + tool.json + parse_resume.go
- [ ] `internal/skills/builtins.go` 使用 `//go:embed builtins` 嵌入；`LoadBuiltins() []*Skill` 解析嵌入树

### 1.3 Executor
- [ ] `internal/skills/executor.go` 含 `RunInline(ctx, skill, args, host) (string, error)` 与 `RunFork(ctx, skill, args, host) (string, error)`
- [ ] inline 调用链：Render → host.ActivateSkill → 工具白名单 fail-fast → host.SetToolFilter → 返回 rendered body
- [ ] fork 调用链：新 conversation.Manager → 按 ForkContext 装填历史（full / recent / none）→ host.RunSubAgent → 返回 finalText
- [ ] `SkillHost` 与 `SkillForkHost` 接口在 `internal/skills/executor.go` 定义

### 1.4 Agent 集成
- [ ] Agent 含 `ActiveSkills map[string]string` 字段
- [ ] `Agent.ActivateSkill(name, body string)` 实现
- [ ] `Agent.ClearActiveSkills()` 实现
- [ ] Agent.Run 主循环每轮迭代开头，如 ActiveSkills 非空则注入 system-reminder（标题 `# Active Skills`，每个 skill 一段，含 name）
- [ ] Agent 实现 SkillHost 接口（编译期可强转）

### 1.5a InstallSkill 远程安装
- [ ] `internal/skills/install.go` 含 `ParseSkillURL` 支持 skills.sh / github.com tree / raw.githubusercontent.com 三种 URL
- [ ] `Install(src, installRoot) (*InstallReport, error)` 走 GitHub Contents API 递归拉取，atomic rename 到 `<installRoot>/<name>/`
- [ ] 限额常量在 install.go：`maxFileSize=1MiB / maxTotalSize=8MiB / maxFileCount=64 / maxRecursionDepth=4`
- [ ] 下载完没有 `SKILL.md` 或 `skill.yaml` 时拒绝安装并清理 staging
- [ ] `internal/skills/install_tool.go` 含 `InstallSkillTool`，Name = `InstallSkill`，Category = write
- [ ] 执行成功后调 `Catalog.Reload(workDir)` + `OnInstalled(name)` 回调
- [ ] TUI 注入 `OnInstalled` 回调指向 `m.registerSkillCommand(name)`，使 `/<name>` 无需重启即可用
- [ ] SkillSection 文本含 "If the user pastes a Skill URL ... call the InstallSkill tool"

### 1.5 LoadSkill 工具与系统工具豁免
- [ ] `internal/tools/load_skill.go` 含 `LoadSkillTool`，Name = `LoadSkill`，Category = read
- [ ] `LoadSkillTool.Execute` 调 `catalog.GetFull` → `host.ActivateSkill` → 目录型 skill 调 `RegisterDirectoryTools` → 返回 `"Skill <name> activated. SOP pinned to env. N specialized tools registered."`（N 为目录型工具数）
- [ ] `tools.SystemTool` 接口在 `internal/tools/tool.go` 定义；LoadSkillTool 实现该接口
- [ ] Agent.ToolNameFilter 应用时绕过 system tool（系统工具始终可见）

### 1.6 目录型 skill
- [ ] `internal/skills/directory.go` 含 `parseToolJSON(dir) ([]ToolSchema, error)` 与 `RegisterDirectoryTools(skill, registry) (int, error)`
- [ ] backend-interview 的 parse_resume 工具能通过 RegisterDirectoryTools 注册到 registry

### 1.7 命令集成
- [ ] 每个 skill 自动注册为 `/<name>` 命令，描述末尾含 `[skill]`
- [ ] inline skill 命令 Type 为 `TypePrompt`，fork skill 命令 Type 为 `TypeSkillFork`
- [ ] `commands.TypeSkillFork` 在 `internal/commands/commands.go` 定义
- [ ] TUI executeCommand 对 TypeSkillFork case：调 handler 返回 summary → 作为 assistant chatMessage 插入对话
- [ ] `/skill list / info <name> / reload` 子命令在 `internal/commands/commands.go` 注册
- [ ] `/clear` handler 调用 `m.ag.ClearActiveSkills()`
- [ ] ch10 硬编码的 `/review` 注册已删除（grep `Review current code changes` 返回 0 条）

## 2. 接入完整性（杜绝死代码）

- [ ] `grep -rn "skills.LoadCatalog" --include="*.go" /Users/codemelo/codebot` 命中 `internal/tui/tui.go` 至少 1 个非测试调用
- [ ] `grep -rn "ActivateSkill" --include="*.go" /Users/codemelo/codebot/internal` 命中 Agent 方法定义 + Executor + LoadSkillTool 三处调用
- [ ] `grep -rn "ClearActiveSkills" --include="*.go" /Users/codemelo/codebot/internal` 命中 `/clear` handler 调用
- [ ] `grep -rn "LoadSkillTool\b\|\"LoadSkill\"" --include="*.go" /Users/codemelo/codebot/internal` 命中 tool 定义 + tui 注册 + 至少 1 个测试
- [ ] `grep -rn "TypeSkillFork" --include="*.go" /Users/codemelo/codebot/internal` 命中 commands 定义 + TUI dispatch
- [ ] `grep -rn "RunInline\|RunFork" --include="*.go" /Users/codemelo/codebot/internal/skills` 命中 Executor 定义 + TUI handler 调用
- [ ] `grep -rn "Catalog.GetFull" --include="*.go" /Users/codemelo/codebot/internal` 命中 catalog 定义 + LoadSkillTool 调用
- [ ] `grep -rn "InstallSkillTool\|ParseSkillURL" --include="*.go" /Users/codemelo/codebot/internal` 命中 install 定义 + TUI 注册 + install_test
- [ ] `grep -rn "SystemTool() bool" --include="*.go" /Users/codemelo/codebot/internal` 命中接口定义 + LoadSkillTool 实现
- [ ] TUI Model `ag` 字段有 `skillCatalog` / 在 loadSkillsAndBuildPrompt 写入 / LoadSkillTool 拿到引用

## 3. 编译与测试

- [ ] `cd /Users/codemelo/codebot && go build ./...` 通过
- [ ] `cd /Users/codemelo/codebot && go test ./internal/skills/...` 全部通过
- [ ] `cd /Users/codemelo/codebot && go test ./internal/tools/...` 全部通过
- [ ] `cd /Users/codemelo/codebot && go test ./internal/agent/...` 全部通过
- [ ] `go vet ./...` 无警告

## 4. 端到端验证（TUI 实操）

> 操作目录在仓库根 `/Users/codemelo/codebot`，启动 `go run ./cmd/codebot`

- [ ] 启动后输 `/help`，看到 `/commit [skill] / /review [skill] / /test [skill] / /backend-interview [skill] / /skill` 都列出
- [ ] 输 `/skill list`，输出含 4 个 skill 名称 + 来源（builtin / project / user）
- [ ] 输 `/skill info commit`，输出含完整 frontmatter（含 mode / allowed_tools） + 文件路径
- [ ] 改一处真实文件（如修个空格），输 `/commit`，看到 Agent 真的走 git status → diff → 生成 commit message → git add → git commit；`git log` 看到新 commit
- [ ] 输 `/review`，看到 fork 路径执行：主对话不污染；末尾收到 assistant 摘要含分级标签
- [ ] 自然语言 `"帮我准备一下后端面试"`，看 Agent tool_use 里出现 `LoadSkill({name: "backend-interview"})` 并且 system-reminder 里出现该 skill 的 SOP
- [ ] 输 `/clear`，立即输任意消息，Agent system-reminder 里**不再出现** Active Skills 段
- [ ] 修改 `.codebot/skills/commit/SKILL.md` 一行，**不重启** TUI，再输 `/commit`，看到新行进入 prompt（热重载验证）
- [ ] 启动时在 catalog 里塞一个 `allowed_tools: [NonExistentTool]` 的 skill，看到启动 log 报 fail-fast 错误（或调用时立刻报错）
- [ ] LoadSkill 工具调用时**不**弹权限提示（read-only 类别 + auto-allow）
- [ ] 在 TUI 输入「装这个 skill：https://www.skills.sh/anthropics/skills/frontend-design」，模型调 InstallSkill；返回安装路径与文件数；立即输 `/frontend-design` 触发新装的 skill（无需 TUI 重启）
- [ ] InstallSkill 失败路径：输错误 URL → 看到具体 host / 格式不对的错误文本；输不存在的 repo → 看到 404 错误透出

## 5. 文档

- [ ] `specs/go/ch11/spec.md` 更新到课程全量版（不是验收版）
- [ ] `specs/go/ch11/tasks.md` 16 个任务全部勾上
- [ ] `specs/go/ch11/checklist.md` 全部条目勾上
- [ ] commit 信息：`feat(ch11): full skill system per course design [spec/tasks/checklist closed]`

```

### Python

```markdown
# ch11: Skill 系统 Spec（Python 版）

> 本版本按课程「第 11 章 Skill 系统」全量实现 Python 版本。Skill 把可复用 prompt 升级为 Markdown 能力包，配合 progressive disclosure 与执行模式，让模型在工具变多时仍能精准触发。

## 1. 背景

CodeBot 用户会反复输入一组类似的 prompt（commit message 规范、代码审查清单、跑测试的项目类型识别）。当前所有 prompt 要么写死在源码 Slash Command（`/review`）里，要么用户每次手敲，三个痛点：(1) 不能复用与分发，(2) 工具一多模型选错的概率指数级上升，(3) 没有任务级的工具白名单与上下文隔离。Skill 把可复用 SOP 装进可编辑的 Markdown 文件，配渐进式披露与执行模式，同时解决上述三个问题。

## 2. 目标

把 `SKILL.md` 升级为「带 frontmatter + 资源 + 专属工具」的能力包。启动时只把 `name + description` 注入对话给 Agent 看；Agent 通过 `LoadSkill` 工具按需把完整 SOP 钉到环境上下文，相关专属工具注册进当前会话。`inline` 模式 SOP 在主对话内执行，`fork` 模式独立子 Agent 隔离执行后把结果回流。`/<skill-name>` 显式触发与意图识别自动触发共用同一套执行器。

## 3. 功能需求

### 解析与加载
- F1: `SkillDef`（`codebot/skills/parser.py:24`）字段：`name / description / prompt_body / allowed_tools / mode / model / context / source_path / is_directory`；`mode` 取 `inline | fork`（默认 `inline`），`context` 取 `full | recent | none`（默认 `full`，仅 fork 模式生效）
- F2: 单文件 `*.md`（YAML frontmatter + body）与目录型（`<skill>/SKILL.md` + `tool.json` + `references/*.py`）两种磁盘布局；`SkillLoader._scan_directory` 区分两类
- F3: 三级搜索路径加载（`codebot/skills/loader.py:23`），优先级 `项目 .codebot/skills/` > `~/.codebot/skills/` > 内置（`importlib.resources`）；首次出现的 name 占位，后续同名跳过；解析失败单条 `warning` 日志并跳过
- F4: 启动期 `SkillLoader.load_all` 解析所有 frontmatter+body 进内存；`SkillLoader.get(name)` 每次重读源文件实现热重载，失败回退缓存（`codebot/skills/loader.py:96`）

### 执行
- F5: `substitute_arguments(prompt_body, args)`（`codebot/skills/parser.py:99`）把 `$ARGUMENTS` 替换为参数；没有占位符则原样返回
- F6: inline 执行：`SkillExecutor.execute_inline`（`codebot/skills/executor.py:54`）渲染 body 后调用 `Agent.activate_skill(name, body)` 钉到 env context，主循环每轮迭代重建 environment 时 SOP 都注入；同时按 `allowed_tools` 过滤当前会话工具集
- F7: fork 执行：`SkillExecutor.execute_fork`（`codebot/skills/executor.py:58`）创建独立 `ConversationManager`，按 `context` 字段决定历史携带（`full` = 主对话拼接摘要 / `recent` = 最近 5 条 / `none` = 完全隔离），临时 Agent 跑到 `LoopComplete` 后把累计文本回流
- F8: 工具白名单：`filter_tool_registry`（`codebot/skills/executor.py:25`）按 `allowed_tools` 重建一个新的 `ToolRegistry`；白名单中出现不存在的工具立刻 `raise SkillDependencyError`
- F9: 系统工具豁免：`Tool.is_system_tool`（`codebot/tools/base.py:28`）标记的工具在 `filter_tool_registry` 时自动透传，确保 `LoadSkill` 在 skill 执行期仍可用以支持嵌套调用

### LoadSkill 工具与 Skill Catalog 注入
- F10: `LoadSkill`（`codebot/tools/load_skill.py:21`）read-only 系统工具，输入 `{name: str}`；调用 `SkillLoader.get` 取 skill → `Agent.activate_skill` 钉 SOP → 目录型 skill 调 `register_skill_tools` 注册专属工具 → 返回简短确认（不返回完整 SOP，避免 tool_result 占用空间）
- F11: 启动期 `app.py:673` 构建「Available Skills」段（只 `- <name>: <description>` 列表 + LoadSkill 调用指引），通过 `Agent.set_skill_catalog` 注入 environment context（`codebot/prompts.py:293`）

### 命令集成
- F12: 每个 skill 由 `register_skill_commands`（`codebot/commands/handlers/skill_register.py:18`）注册为 `/<name>` 短命令，描述末尾标注 `[skill]`；mode 字段决定运行时分支：inline 调 `execute_inline` 后再发送一次 user message 触发 loop，fork 则后台 `asyncio.create_task(_run_fork)` 把结果作为 system message 插入
- F13: `/skill list | info <name> | reload` 管理子命令（`codebot/commands/handlers/skill.py:11`）：list 列出已加载 skill 与来源；info 显示完整 frontmatter 与文件路径；reload 重新扫描三级目录并重新注册命令
- F14: ch10 留下的 `/review` 由 review skill 接管；旧硬编码 handler 仍可保留但优先级被 skill 覆盖

### 目录型 Skill
- F15: 目录布局 `<skill>/SKILL.md` + `<skill>/tool.json` + `<skill>/references/*.py`；`tool.json` 声明该 skill 专属新增的工具 schema（function calling 兼容），LoadSkill 时 `register_skill_tools`（`codebot/skills/directory.py:104`）把 schema 实例化为 `SkillCustomTool` 注册到 registry，工具实现由 `importlib.util.spec_from_file_location` 动态加载 `references/<tool_name>.py` 内的 `execute` 函数
- F16: `tool.json` 与 `allowed_tools` 职责分离：`tool.json` 负责「向 registry 注册新工具」，`allowed_tools` 负责「skill 执行期间可见工具白名单」；同名工具已存在则跳过注册

### 热更新与清理
- F17: `SkillLoader.get(name)` 每次调用都 `parse_skill_file(source_path)` 重读，文件修改即时生效；解析失败回退 `_cache` 中的旧版本并记 warning（`codebot/skills/loader.py:103`）
- F18: `/clear` 命令在清对话历史时调 `Agent.clear_active_skills()`（`codebot/commands/handlers/clear.py:19`）把激活 skill 列表清空

### 内置 Skill
- F19: 四个内置 skill 通过 `importlib.resources` 从 `codebot/skills/builtins/` 加载：`commit`（inline）、`review`（fork, context: none）、`test`（inline）、`backend-interview`（fork, context: none，目录型自带 `parse_resume` 工具）
- F20: 加载顺序保证磁盘版本可覆盖内置：项目 → 用户 → 内置；`SkillLoader.get_source_label`（`codebot/skills/loader.py:117`）按路径前缀返回 `project | user | builtin`

## 4. 非功能需求

- N1: 单个 skill 文件解析失败不能阻断其他 skill 加载，错误走 `logging.warning`
- N2: `LoadSkill` 工具调用不弹权限提示（read-only 类别 + `is_system_tool=True`）
- N3: fork 模式必须隔离 `ConversationManager`，主对话状态不被子 Agent 修改
- N4: 工具过滤通过 `filter_tool_registry` 返回新 `ToolRegistry` 实例实现，过滤动态生效不要求重启 Agent
- N5: 内置 skill 与磁盘上同名 skill 冲突时，磁盘版本优先（用户可覆盖内置）
- N6: 目录型 skill 工具实现走 `importlib` 动态加载 `.py` 文件而非 entry point，避免安装步骤

## 5. 设计概要

### 核心数据结构
- `SkillDef`（`codebot/skills/parser.py:23`）：dataclass，含 `mode / model / context` 三个执行字段 + `source_path / is_directory` 元信息
- `SkillLoader`（`codebot/skills/loader.py:15`）：name → `SkillDef`；持有 `_skills` 与 `_cache` 两份字典，热更新失败回退缓存
- `SkillExecutor`（`codebot/skills/executor.py:43`）：`execute_inline(skill, args) -> None` 与 `execute_fork(skill, args) -> str`
- `SkillCustomTool`（`codebot/skills/directory.py:64`）：动态 Tool 子类，`params_model` 用 `_DynamicParams(extra="allow")`，包裹 `references/*.py` 里的 `execute` 函数
- `LoadSkill`（`codebot/tools/load_skill.py:21`）：实现 `Tool` 抽象类，`is_system_tool = True`；持有 `SkillLoader` 与 `Agent` 引用
- Agent 新增字段与方法：`active_skills: dict[str, str]`、`_skill_catalog: str`、`activate_skill(name, body)`、`clear_active_skills()`、`set_skill_catalog(catalog)`（`codebot/agent.py:317-364`）

### 主流程
1. 启动：`CodeBotApp.__init__` → 实例化 `LoadSkill` 并 register → 构造 `Agent` → `SkillLoader(work_dir).load_all()` → `load_skill_tool.set_loader/set_agent` → 构造 `SkillExecutor` → 把 catalog 字符串写入 `agent.set_skill_catalog` → `register_skill_commands` 把每个 skill 注册成 `/<name>`
2. system prompt 注入：`build_environment_context`（`codebot/prompts.py:277`）每轮迭代重建 environment block，把 `agent._skill_catalog` 与 `agent.active_skills` 字典分别拼为 catalog 段和「## Active Skills」段
3. 主 Agent 循环每轮 `_build_system_message` 调 `build_environment_context(work_dir, active_skills, skill_catalog, agent_catalog)`（`codebot/agent.py:400`），实现 SOP 钉到 env 的能力
4. 显式调用 `/commit`：`register_skill_commands` 注册的 handler → `executor.execute_inline(skill, args)` → `agent.activate_skill("commit", rendered_body)` → 再 `ctx.ui.send_user_message(trigger)` 触发 Agent loop
5. 意图识别：Agent 调 `LoadSkill({name: "commit"})` → `loader.get` → `agent.activate_skill` + 目录型调 `register_skill_tools` → 返回 `"Skill 'commit' activated. SOP pinned to environment context."`
6. fork 调用 `/review`：handler 走 `asyncio.create_task(_run_fork)` → `executor.execute_fork` 新 conversation + 临时 Agent + 收集 `StreamText` 到 `LoopComplete` → 把 finalText 作为 system message 插入主对话
7. `/clear`：handler → reset conversation → `agent.clear_active_skills()` → 后续轮 environment 不再注入旧 SOP

### 调用链
- 启动：`codebot.app.CodeBotApp.__init__` → `SkillLoader.load_all` → `register_skill_commands`（`codebot/app.py:687`）
- inline 显式：用户 `/commit` → command handler → `executor.execute_inline` → `agent.activate_skill` → `ctx.ui.send_user_message` → Agent loop（每轮 env 注入 SOP）
- fork 显式：用户 `/review` → handler → `asyncio.create_task(execute_fork)` → `system message`
- 意图触发：Agent 在某轮调用 `LoadSkill` → `loader.get` → `agent.activate_skill` + register dir tools → 下一轮 SOP 钉在 env 里
- 清理：用户 `/clear` → `handle_clear` → conversation reset + `agent.clear_active_skills`

### 与其他模块的交互
- 上行依赖：`codebot/app.py`（注入 system prompt、注册命令、注入 `SkillLoader/SkillExecutor` 到 `CommandContext.config`）、`Agent`（`active_skills` 字段 + env 注入）、`ConversationManager`（fork 用独立实例）、`ToolRegistry`（动态注册目录型工具）
- 下行：`SkillExecutor` 通过 `from codebot.agent import Agent` 局部 import 避免循环依赖；`SkillCustomTool` 通过 `importlib.util` 加载用户脚本，不依赖 entry point

## 6. Out of Scope

- 远程安装 Skill（`InstallSkill` 工具）：Python 版本暂不实现，用户需手动 clone 到 `.codebot/skills/` 下
- 嵌套深度限制：Skill A → LoadSkill(B) → LoadSkill(C) 不做主动限制，依赖 Agent `max_iterations` 自然封顶
- fork 嵌套跨 Agent 边界的父子链路记录：留给后续 SubAgent 章节
- 目录型 skill 工具的 sandbox：`SkillCustomTool` 执行用户 `.py` 不做沙箱，与本机 Python 同权限运行
- 用户级 `~/.codebot/skills/` 与项目级冲突时的合并策略：高优先级目录里出现的 name 直接覆盖，不做字段级 merge

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch11: Skill 系统 Tasks（Python 版）

> 顺序执行。每完成一个任务跑 `ruff check codebot/skills codebot/tools/load_skill.py` 与 `pytest tests/test_skills.py -q` 确保通过；接入主流程的任务（T10、T11、T12）做完后立刻补一次端到端验证再进下一项。

## T1: 定义 SkillDef 数据结构与 frontmatter 解析

- 影响文件: `codebot/skills/parser.py`（新建）
- 依赖任务: 无
- 完成标准: dataclass `SkillDef` 含 `name / description / prompt_body / allowed_tools / mode / model / context / source_path / is_directory`；`parse_frontmatter(raw) -> (meta, body)` 处理 `---\n...\n---\n<body>` 格式；`_validate_meta` 校验 `name` 正则 `^[a-z][a-z0-9\-]*$`、`mode in {inline, fork}`、`context in {full, recent, none}`；`SkillParseError` 自定义异常类
- 备注: yaml 库走 `import yaml`（pyyaml）；`substitute_arguments(prompt_body, args)` 简单 `.replace("$ARGUMENTS", args)` 即可

## T2: 实现 SkillLoader 三级搜索与热重载

- 影响文件: `codebot/skills/loader.py`（新建）
- 依赖任务: T1
- 完成标准:
  - 常量 `PROJECT_SKILLS_DIR = ".codebot/skills"` / `USER_SKILLS_DIR = "~/.codebot/skills"`
  - `SkillLoader(work_dir)` 构造时计算 `_project_dir` / `_user_dir`
  - `load_all()` 按 project → user → builtin 顺序扫描，首次出现的 name 保留，后续跳过；维护 `_skills` 与 `_cache` 两份字典
  - `_scan_directory(path, source)` 同时处理 `*.md` 与 `<dir>/SKILL.md` 两种布局，目录型 skill `is_directory = True`
  - `_load_builtins()` 走 `importlib.resources.files("codebot.skills.builtins")` 遍历子目录
  - `get(name)` 命中后 `parse_skill_file(source_path)` 强制重读；失败回退 `_cache` 中旧版本并 `log.warning`
  - `get_catalog()` 返回 `[(name, description), ...]`；`get_source_label(name)` 按路径前缀返回 `project | user | builtin`
- 备注: 解析失败用 `log.warning("Skipping %s skill '%s': %s", ...)` 不抛出

## T3: 内置 skill 资源

- 影响文件: `codebot/skills/builtins/__init__.py`（新建空文件）、`codebot/skills/builtins/commit/SKILL.md`、`codebot/skills/builtins/commit/__init__.py`、`codebot/skills/builtins/review/SKILL.md`、`codebot/skills/builtins/review/__init__.py`、`codebot/skills/builtins/test/SKILL.md`、`codebot/skills/builtins/test/__init__.py`、`codebot/skills/builtins/backend-interview/SKILL.md`、`codebot/skills/builtins/backend-interview/__init__.py`、`codebot/skills/builtins/backend-interview/tool.json`、`codebot/skills/builtins/backend-interview/references/parse_resume.py`
- 依赖任务: T2
- 完成标准:
  - `commit/SKILL.md`：`mode: inline / allowedTools: [Bash, ReadFile, Grep]`，body 描述 git status → diff → conventional commit
  - `review/SKILL.md`：`mode: fork / context: none / allowedTools: [Bash, ReadFile, Grep, Glob]`，body 描述 5 维度审查（逻辑/安全/性能/风格/可维护性）+ Critical/Warning/Info 分级
  - `test/SKILL.md`：`mode: inline / allowedTools: [Bash, ReadFile, Grep, Glob]`，body 描述项目类型检测（`pyproject.toml` → `pytest`、`go.mod` → `go test`、`package.json` → `npm test`、`Cargo.toml` → `cargo test`）+ 区分代码 bug 与测试 bug
  - `backend-interview/`：目录型，`tool.json` 声明 `parse_resume` schema，`references/parse_resume.py` 内 `async def execute(file_path: str = "", **kwargs) -> str` 实现
- 备注: `pyproject.toml` 的 `[tool.setuptools.package-data]` 需要把 `codebot.skills.builtins` 的 `*.md / *.json` 也打包

## T4: 工具白名单与系统工具豁免

- 影响文件: `codebot/tools/base.py`（修改，加 `is_system_tool` 字段）、`codebot/skills/executor.py`（新建，部分实现）
- 依赖任务: T1
- 完成标准:
  - `Tool` 抽象基类增加类属性 `is_system_tool: bool = False`
  - 同文件常量 `SYSTEM_TOOL_NAMES = frozenset({"LoadSkill"})`
  - `SkillDependencyError` 异常类在 `codebot/skills/executor.py` 定义
  - `filter_tool_registry(registry, allowed)` 返回新 `ToolRegistry`：`allowed` 为空时直接返回原 registry；遍历 `allowed` 缺工具 `raise SkillDependencyError`；扫描原 registry 把 `is_system_tool=True` 的工具自动透传

## T5: SkillExecutor.execute_inline

- 影响文件: `codebot/skills/executor.py`（继续）
- 依赖任务: T2, T4
- 完成标准: `class SkillExecutor(agent, client, protocol)` 三个属性持有；`execute_inline(skill, args) -> None`：
  - `substitute_arguments(skill.prompt_body, args)`
  - `agent.activate_skill(skill.name, rendered)`
  - 不需要立即调用 LLM，rendered body 钉到 env 后由 command handler 再 `ctx.ui.send_user_message(trigger)` 触发 loop
- 备注: 工具过滤在 fork 路径才动手；inline 走主 registry，由 Agent loop 每轮根据 ActiveSkills 自然限制工具

## T6: SkillExecutor.execute_fork

- 影响文件: `codebot/skills/executor.py`（继续）
- 依赖任务: T5
- 完成标准: `async execute_fork(skill, args) -> str`：
  - 渲染 prompt
  - 新 `ConversationManager()`
  - 根据 `skill.context` 装填历史：`none` 空 / `recent` 取 `agent._conversation.history` 最近 5 条 user/assistant 消息 / `full` 拼成一段 `"## Previous conversation summary\n\n"` summary 作为单条 user message
  - `fork_conv.add_user_message(rendered)`
  - `filter_tool_registry(agent.registry, skill.allowed_tools)` 失败返回错误字符串
  - 局部 `from codebot.agent import Agent as AgentClass, StreamText, LoopComplete, ErrorEvent`（避免循环 import）构造临时 Agent，沿用 `client / protocol / work_dir / max_iterations / context_window`
  - `async for event in fork_agent.run(fork_conv)`：`StreamText` 追加文本，`ErrorEvent` 追加错误标记，`LoopComplete` break
  - 返回 `"".join(result_parts)`

## T7: Agent 集成 active_skills 与 skill_catalog

- 影响文件: `codebot/agent.py`（修改）、`codebot/prompts.py`（修改）
- 依赖任务: 无（与 T1-T6 并行可做）
- 完成标准:
  - `Agent.__init__` 增加 `self.active_skills: dict[str, str] = {}` 与 `self._skill_catalog: str = ""`
  - 方法 `activate_skill(name, prompt_body)` / `clear_active_skills()` / `set_skill_catalog(catalog)`
  - 每轮 `_build_system_message`（或同等位置）调用 `build_environment_context(work_dir, active_skills, skill_catalog, agent_catalog)`
  - `codebot/prompts.py` 的 `build_environment_context` 拼接：先写 `skill_catalog` 段落，再写 `## Active Skills` 标题 + `### Skill: <name>\n<sop>` 子段

## T8: LoadSkill 工具

- 影响文件: `codebot/tools/load_skill.py`（新建）
- 依赖任务: T2, T7
- 完成标准:
  - `LoadSkill` 继承 `Tool`，`name = "LoadSkill"`、`description` 描述「按需激活 skill」、`params_model = LoadSkillParams(name: str)`、`category = "read"`、`is_concurrency_safe = False`、`is_system_tool = True`
  - 持有 `_loader` 与 `_agent` 私有属性；`set_loader(loader)` / `set_agent(agent)` 注入器
  - `execute(params)`：
    - 未初始化返回 `is_error=True` 的「LoadSkill not properly initialized」
    - `self._loader.get(params.name)` 为 None 时列出 catalog 返回错误
    - 调 `self._agent.activate_skill(skill.name, skill.prompt_body)`
    - 目录型且 `source_path is not None` 时局部 import `register_skill_tools` 并调用，count 累加
    - 返回 `"Skill '<name>' activated. SOP pinned to environment context."` + 若有工具 `" N specialized tool(s) registered."`

## T9: 目录型 Skill 工具注册

- 影响文件: `codebot/skills/directory.py`（新建）
- 依赖任务: T8
- 完成标准:
  - `parse_tool_json(path) -> list[dict]`：`json.loads`，支持单 dict 包装成 list，失败 warning 后返回空 list
  - `load_tool_implementation(references_dir, tool_name) -> Callable | None`：`importlib.util.spec_from_file_location("codebot_skill_tool_<name>", references_dir / f"{tool_name}.py")` 动态加载，读取 `execute` 函数；找不到/失败时 warning 后返回 None
  - `_DynamicParams(BaseModel)` 配 `model_config = {"extra": "allow"}` 用作动态参数模型
  - `SkillCustomTool(tool_name, description, schema, impl)` 继承 `Tool`：`get_schema` 用 `schema["parameters"]` 或 `schema["input_schema"]` 作为 `input_schema`；`execute(params)` 检查 `impl` 是否为协程，分别 `await impl(**kwargs)` 或 `impl(**kwargs)`，包成 `ToolResult(output=str(result))`，异常包成 `is_error=True`
  - `register_skill_tools(skill_dir, registry) -> int`：找 `tool.json` 没有返回 0；遍历 schemas，跳过同名已注册，新建 `SkillCustomTool` 注册并 +1

## T10: 接入 app.py —— 加载 + Catalog 注入 + 命令注册

- 影响文件: `codebot/app.py`（修改）
- 依赖任务: T2, T3, T5, T6, T7, T8
- 完成标准:
  - import `SkillLoader / SkillExecutor / register_skill_commands / LoadSkill`
  - `CodeBotApp.__init__` 字段 `self.skill_loader / self.skill_executor / self._load_skill_tool`
  - 先 `LoadSkill()` 实例化注册到 `self.registry`，再构造 `Agent`（保证 registry 已含 LoadSkill）
  - `SkillLoader(work_dir).load_all()` 加载 catalog
  - `load_skill_tool.set_loader(self.skill_loader)` / `set_agent(self.agent)` 注入
  - `SkillExecutor(agent=..., client=..., protocol=...)` 构造
  - 把 catalog 拼成 `"You can use the following Skills:\n\n- <name>: <desc>\n...\nIf the user's request matches a Skill, call LoadSkill to activate it."` 调 `self.agent.set_skill_catalog(...)`
  - `register_skill_commands(self.command_registry, self.skill_loader, self.skill_executor)`
  - `CommandContext.config` 字典塞入 `"skill_loader" / "skill_executor"` 供 handler 取用

## T11: 接入 commands —— `/skill` 管理 + skill 命令 + `/clear` 钩

- 影响文件: `codebot/commands/handlers/skill.py`（新建）、`codebot/commands/handlers/skill_register.py`（新建）、`codebot/commands/handlers/clear.py`（修改）、`codebot/commands/handlers/__init__.py`（注册 SKILL_COMMAND）
- 依赖任务: T10
- 完成标准:
  - `SKILL_COMMAND` 提供 `/skill list | info <name> | reload` 三档：
    - list：遍历 catalog，每行 `f"  {name:<20} {desc}  [{source}]"`
    - info：拉 `loader.get(name)` 输出完整 frontmatter + path + directory 标记
    - reload：`loader.reload()` 后调用 `register_skill_commands` 重建命令
  - `register_skill_commands(registry, loader, executor)`：模块级集合 `_REGISTERED_SKILL_NAMES` 跟踪本次会话已注册的 skill 命令，再次调用先清掉旧的；inline skill 命令 handler `execute_inline` 后调 `ctx.ui.send_user_message(trigger)`；fork skill 命令 handler 走 `asyncio.create_task(_run_fork)`，结果作为 system message
  - `clear.py` 的 `handle_clear` 增加 `if ctx.agent: ctx.agent.clear_active_skills()`

## T12: 接入主流程 —— 端到端走通

- 影响文件: 无（仅运行验证）
- 依赖任务: T1-T11
- 完成标准:
  - `pytest tests/test_skills.py -q` 全部通过
  - 在仓库根目录手动启动 `python -m codebot`：
    1. `/help` 列出 `/commit`、`/review`、`/test`、`/backend-interview`、`/skill` 命令
    2. `/skill list` 输出 4 个 skill 名 + builtin 来源
    3. `/skill info commit` 输出 mode / context / model / allowedTools / source
    4. 改一处源码后 `/commit`，看到 Agent 走 git status → diff → commit
    5. `/review` 走 fork 路径，主对话不污染，末尾收到 assistant 摘要
    6. 「帮我准备一下后端面试」自然语言触发 `LoadSkill({name: "backend-interview"})`，env-reminder 出现 SOP
    7. `/clear` 后 env-reminder 不再出现旧 SOP
    8. `.codebot/skills/commit.md` 改一行后**不重启**再 `/commit`，新行进入 prompt（热重载验证）

## T13: 单元测试

- 影响文件: `tests/test_skills.py`（新建）
- 依赖任务: T1-T11
- 完成标准: 覆盖
  - parser：valid / missing opening / unclosed / invalid yaml / non-dict / missing name / missing description / invalid name format / invalid mode / nonexistent file / fork mode with context
  - substitute_arguments：with / without args / no placeholder / multiple
  - loader：内置加载 / 项目覆盖内置 / catalog / get / get_unknown / 热重载成功 / 热重载失败回退 / 目录型识别 / source_label / 失败文件跳过 / reload
  - filter_tool_registry：empty allowed / 过滤 / 系统工具透传 / 缺工具抛错
  - directory：parse_tool_json list / single object / register_skill_tools / 无 tool.json / 动态工具实际可执行
  - LoadSkill：load existing / load unknown / 未初始化 / `is_system_tool` 与 `category="read"`
  - Agent 集成：`build_environment_context` 含 / 不含 Active Skills 段 / `activate_skill` 后字典含 name / `clear_active_skills` 清空
- 备注: 用 `unittest.mock.MagicMock / AsyncMock` 替代真实 Agent；`pytest.mark.asyncio` 配 `pytest-asyncio`

## 进度

- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8
- [ ] T9
- [ ] T10
- [ ] T11
- [ ] T12
- [ ] T13

```

```markdown
# ch11: Skill 系统 Checklist（Python 版）

> 所有条目必须可勾选、可观测。验收方式写在每项后面的括号里。操作目录在仓库根 `/Users/codemelo/codebot`。

## 1. 实现完整性

### 1.1 解析与加载

- [ ] `codebot/skills/parser.py:23` `SkillDef` 含字段 `name / description / prompt_body / allowed_tools / mode / model / context / source_path / is_directory`（`grep -n "class SkillDef" codebot/skills/parser.py` 命中）
- [ ] `codebot/skills/parser.py:36` `parse_frontmatter(raw) -> (dict, str)` 处理 `---\n...\n---` 格式
- [ ] `codebot/skills/parser.py:57` `_validate_meta` 校验 `name` 正则 + `mode in {inline, fork}` + `context in {full, recent, none}`
- [ ] `codebot/skills/parser.py:99` `substitute_arguments(prompt_body, args)` 实现 `$ARGUMENTS` 替换
- [ ] `codebot/skills/loader.py:15` `SkillLoader(work_dir)` 实现三级搜索（`grep -n "PROJECT_SKILLS_DIR\|USER_SKILLS_DIR\|_load_builtins" codebot/skills/loader.py` 命中 ≥3 处）
- [ ] `codebot/skills/loader.py:96` `get(name)` 每次重读源文件实现热重载，失败回退 `_cache` 并 `log.warning`
- [ ] `codebot/skills/loader.py:117` `get_source_label(name)` 按 `_project_dir / _user_dir` 前缀返回 `project | user | builtin`

### 1.2 内置 skill

- [ ] `codebot/skills/builtins/commit/SKILL.md` 存在，frontmatter `mode: inline / allowedTools: [Bash, ReadFile, Grep]`
- [ ] `codebot/skills/builtins/review/SKILL.md` 存在，frontmatter `mode: fork / context: none / allowedTools: [Bash, ReadFile, Grep, Glob]`，body 含「Critical」「Warning」「Info」分级
- [ ] `codebot/skills/builtins/test/SKILL.md` 存在，frontmatter `mode: inline / allowedTools: [Bash, ReadFile, Grep, Glob]`，body 含 `pyproject.toml` 与 `go.mod` 检测
- [ ] `codebot/skills/builtins/backend-interview/SKILL.md` 存在 + `tool.json` 声明 `parse_resume` + `references/parse_resume.py` 含 `async def execute(file_path: str = "", **kwargs)`
- [ ] `codebot/skills/loader.py:65` `_load_builtins` 使用 `importlib.resources.files("codebot.skills.builtins")` 遍历

### 1.3 Executor

- [ ] `codebot/skills/executor.py:43` 含 `class SkillExecutor` 与 `execute_inline / execute_fork` 两个方法
- [ ] inline 调用链：`substitute_arguments` → `agent.activate_skill(name, body)`（`grep -n "activate_skill" codebot/skills/executor.py` 命中）
- [ ] fork 调用链：新 `ConversationManager` → `_build_fork_context(context)` 按 `full / recent / none` 三档装填 → `filter_tool_registry` 过滤工具 → 临时 Agent run → 收集 `StreamText` 到 `LoopComplete`
- [ ] `codebot/skills/executor.py:25` `filter_tool_registry(registry, allowed)` 缺工具 `raise SkillDependencyError`，系统工具自动透传

### 1.4 Agent 集成

- [ ] `codebot/agent.py:317` Agent 含 `self.active_skills: dict[str, str] = {}`
- [ ] `codebot/agent.py:357` `activate_skill(name, prompt_body)` 实现
- [ ] `codebot/agent.py:360` `clear_active_skills()` 实现
- [ ] `codebot/agent.py:363` `set_skill_catalog(catalog)` 实现
- [ ] `codebot/agent.py:400` 主循环每轮调用 `build_environment_context(work_dir, active_skills, skill_catalog, agent_catalog)`
- [ ] `codebot/prompts.py:277` `build_environment_context` 把 `active_skills` 拼成 `## Active Skills` 段；`skill_catalog` 拼到 environment block

### 1.5 LoadSkill 工具与系统工具豁免

- [ ] `codebot/tools/load_skill.py:21` 含 `class LoadSkill(Tool)`，`name = "LoadSkill"`、`category = "read"`、`is_system_tool = True`
- [ ] `codebot/tools/load_skill.py:39` `set_loader / set_agent` 注入方法
- [ ] `codebot/tools/load_skill.py:46` `execute` 调 `loader.get → agent.activate_skill → register_skill_tools`（目录型）→ 返回 `"Skill '<name>' activated. SOP pinned to environment context."`
- [ ] `codebot/tools/base.py:28` `Tool.is_system_tool: bool = False` 类属性
- [ ] `codebot/skills/executor.py:14` `SYSTEM_TOOL_NAMES = frozenset({"LoadSkill"})` 常量
- [ ] `filter_tool_registry` 应用 `allowed_tools` 时不剔除 `is_system_tool=True` 的工具（`grep -n "is_system_tool" codebot/skills/executor.py` 命中）

### 1.6 目录型 skill

- [ ] `codebot/skills/directory.py:17` `parse_tool_json(path)` 支持 list 与单 dict 两种格式
- [ ] `codebot/skills/directory.py:34` `load_tool_implementation` 用 `importlib.util.spec_from_file_location` 动态加载 `references/<name>.py` 内的 `execute` 函数
- [ ] `codebot/skills/directory.py:64` `SkillCustomTool` 继承 `Tool`，`params_model = _DynamicParams`（`extra="allow"`）
- [ ] `codebot/skills/directory.py:104` `register_skill_tools(skill_dir, registry) -> int` 遍历 tool.json，注册成功 +1，重名跳过
- [ ] `backend-interview` 的 `parse_resume` 工具能通过 `register_skill_tools` 注册到 registry（见 `tests/test_skills.py` `test_register_skill_tools`）

### 1.7 命令集成

- [ ] 每个 skill 自动注册为 `/<name>` 命令，描述末尾含 `[skill]`（`grep -n "\\[skill\\]" codebot/commands/handlers/skill_register.py` 命中）
- [ ] `codebot/commands/handlers/skill_register.py:18` `register_skill_commands(registry, loader, executor)` 实现；模块级 `_REGISTERED_SKILL_NAMES` 跟踪重复注册
- [ ] inline skill 命令 handler 调 `executor.execute_inline` 后再 `ctx.ui.send_user_message(trigger)`
- [ ] fork skill 命令 handler 走 `asyncio.create_task(_run_fork)`，结果作为 `add_system_message` 插入
- [ ] `codebot/commands/handlers/skill.py:11` `/skill list | info <name> | reload` 子命令分发
- [ ] `codebot/commands/handlers/clear.py:19` `handle_clear` 调用 `ctx.agent.clear_active_skills()`

## 2. 接入完整性（杜绝死代码）

- [ ] `grep -rn "SkillLoader" codebot/app.py` 命中 ≥2 处（import + 实例化）
- [ ] `grep -rn "activate_skill" codebot/` 命中 Agent 方法定义 + Executor + LoadSkillTool 三处调用
- [ ] `grep -rn "clear_active_skills" codebot/` 命中 `/clear` handler 调用 + Agent 方法定义
- [ ] `grep -rn "LoadSkill\|\"LoadSkill\"" codebot/` 命中 tool 定义 + app 注册 + 至少 1 个测试
- [ ] `grep -rn "SkillExecutor\|register_skill_commands" codebot/` 命中 app.py 注册 + handler 模块
- [ ] `grep -rn "execute_inline\|execute_fork" codebot/skills/` 命中 Executor 定义 + handler 调用
- [ ] `grep -rn "loader.get\|SkillLoader.get" codebot/tools/load_skill.py` 命中 1 处
- [ ] `grep -rn "is_system_tool" codebot/` 命中 base.py 定义 + executor filter 检查 + LoadSkill 实现
- [ ] `codebot/app.py:556` 存在 `self.skill_loader` / `self.skill_executor` / `self._load_skill_tool` 字段
- [ ] `codebot/app.py:885` `CommandContext.config` 字典塞入 `"skill_loader"` 与 `"skill_executor"` key

## 3. 编译与测试

- [ ] `cd /Users/codemelo/codebot && ruff check codebot/skills codebot/tools/load_skill.py` 无 error
- [ ] `cd /Users/codemelo/codebot && pytest tests/test_skills.py -q` 全部通过
- [ ] `cd /Users/codemelo/codebot && pytest tests/test_agent.py -q` 全部通过
- [ ] `cd /Users/codemelo/codebot && python -c "from codebot.skills.loader import SkillLoader; l = SkillLoader('/tmp'); print(list(l.load_all().keys()))"` 输出含 `commit / review / test / backend-interview`
- [ ] `cd /Users/codemelo/codebot && python -c "from codebot.tools.load_skill import LoadSkill; t = LoadSkill(); print(t.name, t.category, t.is_system_tool)"` 输出 `LoadSkill read True`

## 4. 端到端验证（手动操作 TUI）

> 启动命令：`cd /Users/codemelo/codebot && python -m codebot`

- [ ] 启动后输 `/help`，看到 `/commit [skill]` / `/review [skill]` / `/test [skill]` / `/backend-interview [skill]` / `/skill` 都列出
- [ ] 输 `/skill list`，输出含 4 个 skill 名称 + 来源（builtin / project / user）
- [ ] 输 `/skill info commit`，输出含完整 frontmatter（mode / context / model / AllowedTools / Source / Path）
- [ ] 改一处真实文件（如修个空格），输 `/commit`，看到 Agent 真的走 git status → diff → 生成 commit message → git add → git commit；`git log -1` 看到新 commit
- [ ] 输 `/review`，看到 fork 路径执行：主对话不污染；末尾以 `[review skill result]` 开头收到摘要含 Critical/Warning/Info 分级
- [ ] 自然语言 `"帮我准备一下后端面试"`，Agent tool_use 里出现 `LoadSkill({name: "backend-interview"})` 并且 environment 段里出现该 skill 的 SOP
- [ ] 输 `/clear`，立即输任意消息，environment 段里**不再出现** `## Active Skills`
- [ ] 修改 `.codebot/skills/<name>.md` 一行（如自建一个 `custom.md`），**不重启** TUI，再 `/custom`，看到新行进入 prompt（热重载验证）
- [ ] 创建 `.codebot/skills/bad.md` 故意写错 frontmatter，启动日志出现 `Skipping ... skill 'bad': ...` warning，其他 skill 仍正常加载
- [ ] LoadSkill 工具调用时**不**弹权限提示（`category=read` + `is_system_tool=True`）

## 5. 文档

- [ ] `docs/python/ch11/spec.md` 更新到课程全量版（不是验收版）
- [ ] `docs/python/ch11/tasks.md` 13 个任务全部勾上
- [ ] `docs/python/ch11/checklist.md` 全部条目勾上
- [ ] commit 信息：`feat(ch11): full skill system per course design (python) [spec/tasks/checklist closed]`

```

### Java

```markdown
# ch11: Skills 系统 Spec

## 1. 背景

Slash Command 让用户绕过 LLM 直接触发本地动作，但所有 handler 都硬编码在源码里：想加一个 `/commit` 让 Agent 自动分析 diff、生成 message、提交，就得改 Java 再重编。Slash Command 是确定性的快车道，Skill 系统则把可扩展性补上——用户在 `.codebot/skills/<name>/` 或 `~/.codebot/skills/<name>/` 放一个 `SKILL.md`（可选 frontmatter）或 `skill.yaml + prompt.md`，启动时被发现并注册成提示型命令，运行时按 inline 或 fork 模式注入 SOP，让 Agent 借助 LLM 能力完成更复杂的工作流。

## 2. 目标

交付一套进程内的技能编目与执行链路：`SkillCatalog` 三层扫描（builtin + 用户全局 `~/.codebot/skills/` + 项目 `.codebot/skills/`）发现技能；phase-1 仅读 frontmatter 加快启动，`getFull` 触发 phase-2 重读 body 实现热更新；`SkillExecutor` 提供 `executeInline` 与 `executeFork` 两种执行模式，前者把 SOP 注入主 Agent 并按 `allowed_tools` 过滤工具，后者跑隔离的子 Agent，按 `fork_context`（none / recent / full）决定父消息种子；`SkillHost` / `SkillForkHost` 通过接口而非具体类把 Agent 状态切片暴露给 executor，避免 `com.codebot.skill` 反向依赖 agent 包。`CodeBotModel` 在 provider 就绪后调用 `loadFromDirectory` 加载项目目录，再把每个技能注册为 PROMPT 类型的 Slash Command，输入 `/<skill-name>` 时把 promptBody 当作 user message 发给 LLM，UI 上紧跟 `Successfully loaded skill` 系统消息。

## 3. 功能需求

- F1: `SkillCatalog` 暴露 `register / get / getFull / list / source / reload / loadCatalog / loadFromDirectory / buildActiveContext` 方法，内部 `skills` 与 `sources` 用 `LinkedHashMap` 保序。
- F2: 三层目录加载 `loadCatalog(workDir)`：tier 1 builtin（占位，由 agent 层装入）、tier 2 用户 `~/.codebot/skills/`、tier 3 项目 `<workDir>/.codebot/skills/`，按名字后者覆盖前者。
- F3: 单技能加载策略两选一：优先 `skill.yaml + prompt.md`（`loadFromYamlAndPrompt`），否则 `SKILL.md`（`parseSkillMD`，可选 YAML frontmatter，缺描述时回退到 body 第一行非标题）。
- F4: `getFull(name)` 触发热重载：对 `sourceDir != null` 的技能每次重读 body，读失败时保留旧缓存，避免编辑过程中读到半成品。
- F5: `SkillMeta` 字段包含 `name / description / whenToUse / tags / allowedTools / mode / model / forkContext`；name 缺省时取目录名小写化并把空格换 `-`；mode 缺省 `inline`，向后兼容 `context: fork`；`fork_context` 缺省 `none`。
- F6: `SkillExecutor.executeInline(skill, args, host)`：先 `assertAllowedToolsExist` 校验白名单工具均在 `ToolRegistry`；再 `substituteArguments` 渲染 prompt；最后通过 `host.activateSkill` 注入 SOP 并按 `allowed_tools` 调 `host.setToolFilter`，返回渲染后的 body。
- F7: `SkillExecutor.executeFork(skill, args, host)`：构造 prompt + `buildForkSeed` 种子消息，调 `host.runSubAgent` 起隔离子 Agent，把最终 assistant 文本回传。
- F8: `substituteArguments(body, args)`：args 为空原样返回；body 含 `$ARGUMENTS` 时占位符替换；否则追加 `## User Request` 段。
- F9: `buildForkSeed(mode, parent)`：`full` 全量拷贝；`recent` 取尾部最多 5 条；其他（含 `none`）返回空。
- F10: `SkillHost` / `SkillForkHost` 接口：`activateSkill / setToolFilter / toolRegistry` 由 TUI/Agent 层实现；fork 主机额外提供 `runSubAgent / snapshotParentMessages`。
- F11: `CodeBotModel.wireSkillsToAgent` 把 catalog 内每个技能注册为 PROMPT 命令，description 后缀 `[skill]` 用作分支判断；handler 返回 `promptBody`，executeCommand 在 PROMPT 分支把它当 user message。
- F12: PROMPT 分发命中 `[skill]` 后缀时，在 UI 上追加 `skill(<name>) Successfully loaded skill` 系统消息，提示用户技能已激活。

## 4. 非功能需求

- N1: `loadTier` 必须容错：目录缺失、不可读、单个技能解析失败都不中断其他技能。
- N2: phase-1 加载不能读 body：仅 frontmatter / yaml meta，避免大文件拖慢启动；body 由 `getFull` 按需加载。
- N3: `parseSkillMD` 的 YAML 解析失败要降级到「无 frontmatter」分支而不是抛异常。
- N4: `com.codebot.skill` 不允许 import `com.codebot.agent` / `com.codebot.tui`——通过 `SkillHost` / `SkillForkHost` 接口反向解耦。
- N5: `assertAllowedToolsExist` 在工具未注册时抛 `IllegalStateException`，让上层在执行前暴露配置错误，而不是运行到一半才失败。
- N6: `register(skill)` 允许同名覆盖，调用方按 tier 顺序决定优先级（后注册者胜出）。
- N7: 注册成 PROMPT 命令时 `description` 必须以 `[skill]` 结尾，作为 UI 分支识别 marker。

## 5. 设计概要

- 核心数据结构:
 - `SkillCatalog.Skill`：record(`meta`, `promptBody`, `sourceDir`, `bodyLoaded`)，`withBody` 返回带新 body 的副本
 - `SkillCatalog.SkillMeta`：record(name, description, whenToUse, tags, allowedTools, mode, model, forkContext)
 - `SkillCatalog` 内部 `Map<String, Skill> skills` + `Map<String, String> sources` 全部 `LinkedHashMap`
 - `SkillHost`：`activateSkill(name, body)` + `setToolFilter(Predicate<String>)` + `toolRegistry()`
 - `SkillForkHost extends SkillHost`：追加 `runSubAgent(body, seed, allowedTools, model)` + `snapshotParentMessages()`
- 主流程（启动期）:
 1. `CodeBot.main` 装好配置 → 构造 `CodeBotModel`
 2. provider 就绪后（`CodeBotModel` line 494-498）`new SkillCatalog()` + `loadFromDirectory(<workDir>/.codebot/skills)`
 3. `wireSkillsToAgent`（line 511-516）遍历 `list()`，对每个 meta 调 `registerSkillCommand`
 4. `registerSkillCommand`（line 518-533）跳过已有命令、把技能注册为 PROMPT 类型的 `Command`，handler 在执行时从 catalog 取 `promptBody`
- 主流程（运行期 inline 模式）:
 1. 用户输入 `/<skill-name> <args>` → `executeCommand` → PROMPT 分支
 2. `cmdRegistry.execute` 返回 promptBody → `conversation.addUserMessage(promptBody)` → 若有 args 追加 `conversation.addUserMessage(args)`
 3. `agent.run` 启动新一轮 → UI 推送 `skill(<name>) Successfully loaded skill` 系统消息
 4. 后续 turn 与普通 Agent loop 一致
- 主流程（运行期 fork 模式 / Executor 直调）:
 1. 调用方持 `SkillForkHost` 实例，调用 `SkillExecutor.executeFork(skill, args, host)`
 2. `assertAllowedToolsExist` 校验工具白名单 → `substituteArguments` 渲染 prompt
 3. `buildForkSeed(skill.forkContext, host.snapshotParentMessages())` 决定种子消息
 4. `host.runSubAgent` 跑隔离 Agent，回最终文本
- 调用链:
 - 启动: `CodeBot.main` → `CodeBotModel` 构造 → provider 就绪回调 → `new SkillCatalog().loadFromDirectory` → `wireSkillsToAgent` → `cmdRegistry.register`
 - 执行 inline: TUI `executeCommand`(PROMPT) → `cmdRegistry.execute` → catalog handler → 返回 promptBody → conversation → agent
 - 执行 fork（programmatic）: 外部调用 `SkillExecutor.executeFork` → `host.runSubAgent`
- 与其他模块的交互:
 - 上行: `com.codebot.tui.CodeBotModel`（注册 / 分发 / UI 提示）、`com.codebot.command.CommandRegistry`（命令注册）
 - 下行: `com.codebot.conversation.Message`（fork 种子）、`com.codebot.tool.ToolRegistry`（白名单校验）
 - 接口反转: `SkillHost` / `SkillForkHost` 由 TUI / agent 层实现，避免循环依赖

## 6. Out of Scope

- Builtin skill 真正加载（当前 tier 1 是占位，由 agent 层装入，本章不实现具体内置技能集）
- Skill 远程仓库 / 包管理：用户必须手动放文件到指定目录
- Skill 权限模型：fork 模式不再二次校验权限，沿用父 Agent 的 PermissionChecker
- Skill 链式调用 / pipeline：一次只能激活一个技能
- 文件 watcher 自动热加载目录新增技能：`getFull` 仅热重载已注册技能的 body，目录新增需 `reload(workDir)` 或重启
- Skill 配额 / 计费 / 超时控制：fork 模式不限制子 Agent 步数

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch11: Skills 系统 Tasks

## T1: 定义 SkillCatalog 数据类型与状态
- 影响文件: `src/main/java/com/codebot/skill/SkillCatalog.java`
- 依赖任务: 无
- 完成标准: `SkillMeta` / `Skill` record 字段齐全；`Skill.withBody` 副本构造可用；内部 `skills` / `sources` 用 `LinkedHashMap` 保序；`register / get / list / source` 行为对齐参考。
- 实际产出: `SkillCatalog.java:24-39`（record）、`SkillCatalog.java:43-45`（state）、`SkillCatalog.java:49-97`（公共方法）

## T2: 实现单技能加载策略
- 影响文件: `src/main/java/com/codebot/skill/SkillCatalog.java`
- 依赖任务: T1
- 完成标准: `loadSkill(dir)` 优先 `skill.yaml + prompt.md`，否则 `SKILL.md`；`loadFromYamlAndPrompt` 用 snakeyaml 解析 meta + 读取 prompt.md；`parseSkillMD` 处理可选 frontmatter，缺描述时回退到 body 第一行非标题行。
- 实际产出: `SkillCatalog.java:184-199`（loadSkill）、`SkillCatalog.java:201-219`（loadFromYamlAndPrompt）、`SkillCatalog.java:221-262`（parseSkillMD）、`SkillCatalog.java:264-313`（metaFromMap）

## T3: 实现三层目录加载与热重载
- 影响文件: `src/main/java/com/codebot/skill/SkillCatalog.java`
- 依赖任务: T1, T2
- 完成标准: `loadCatalog(workDir)` 按 builtin → 用户 → 项目顺序加载，后者覆盖前者；`loadTier` 容错；`getFull` 触发 phase-2 重读 body，读失败保留旧缓存；`reload(workDir)` 整体刷新。
- 实际产出: `SkillCatalog.java:107-123`（loadCatalog）、`SkillCatalog.java:125-132`（reload）、`SkillCatalog.java:138-158`（loadFromDirectory + loadTier）、`SkillCatalog.java:66-89`（getFull）

## T4: 定义 SkillHost / SkillForkHost 接口
- 影响文件: `src/main/java/com/codebot/skill/SkillHost.java`, `src/main/java/com/codebot/skill/SkillForkHost.java`
- 依赖任务: 无
- 完成标准: `SkillHost.activateSkill(name, body) / setToolFilter(Predicate<String>) / toolRegistry()`；`SkillForkHost extends SkillHost` 增加 `runSubAgent(body, seed, allowedTools, model) / snapshotParentMessages()`。
- 实际产出: `SkillHost.java:12-19`、`SkillForkHost.java:12-17`

## T5: 实现 SkillExecutor（inline / fork 双模式）
- 影响文件: `src/main/java/com/codebot/skill/SkillExecutor.java`
- 依赖任务: T1, T4
- 完成标准: `executeInline(skill, args, host)` 校验工具白名单 + 渲染 prompt + `activateSkill` + `setToolFilter`；`executeFork(skill, args, host)` 渲染 prompt + `buildForkSeed` + `runSubAgent`；`substituteArguments` 处理 `$ARGUMENTS` 占位符与缺占位符追加 `## User Request`；`buildForkSeed` 支持 `none / recent (≤5) / full`。
- 实际产出: `SkillExecutor.java:25-37`（executeInline）、`SkillExecutor.java:43-48`（executeFork）、`SkillExecutor.java:50-58`（substituteArguments）、`SkillExecutor.java:60-74`（buildForkSeed）、`SkillExecutor.java:76-88`（assertAllowedToolsExist）

## T6: buildActiveContext 系统提示注入助手
- 影响文件: `src/main/java/com/codebot/skill/SkillCatalog.java`
- 依赖任务: T1
- 完成标准: `buildActiveContext(Set<String> activeSkillNames)` 在系统提示里拼 `## Active Skills` 段 + 每个技能的 `### name` + body；空集合返回空串。
- 实际产出: `SkillCatalog.java:166-180`

## T7: 接入主流程 —— TUI 加载技能 / 注册为命令
- 影响文件: `src/main/java/com/codebot/tui/CodeBotModel.java`
- 依赖任务: T1, T3
- 完成标准: provider 就绪后构造 `SkillCatalog` + `loadFromDirectory(<workDir>/.codebot/skills)`；`wireSkillsToAgent` 遍历 `list()` 调 `registerSkillCommand`；`registerSkillCommand` 跳过已存在命令，注册 PROMPT 类型 `Command`，description 以 `[skill]` 结尾，handler 从 catalog 取 `promptBody`。
- 实际产出: `CodeBotModel.java:102`（字段）、`CodeBotModel.java:494-500`（加载）、`CodeBotModel.java:511-516`（wireSkillsToAgent）、`CodeBotModel.java:518-533`（registerSkillCommand）

## T8: 接入主流程 —— PROMPT 分发的 skill 分支
- 影响文件: `src/main/java/com/codebot/tui/CodeBotModel.java`
- 依赖任务: T7
- 完成标准: `executeCommand` 命中 PROMPT 类型时判断 description 是否以 `[skill]` 结尾；是则把 promptBody 当 user message 推入 conversation、附加 args、起 agent.run，并在 UI 上 println `skill(<name>) Successfully loaded skill`；`/skills` 命令列出当前 catalog。
- 实际产出: `CodeBotModel.java:928-967`（PROMPT 分支）、`CommandRegistry.java:255-265`（/skills handler）、`CodeBotModel.java:984-986`（skillList supplier）

## T9: 端到端验证
- 影响文件: 无
- 依赖任务: T7, T8
- 完成标准: `./gradlew build` 通过；在 `.codebot/skills/demo/SKILL.md` 放最小 frontmatter（name: demo, description: demo skill）+ body，启动 CodeBot 后 `/skills` 列出 `demo`；输入 `/demo hello` 触发 PROMPT 分发，UI 显示 `skill(demo) Successfully loaded skill`，Agent 收到 promptBody + `hello` 作为新对话起点；`origin/java` 仓库已自带 `.codebot/skills/skill-creator/SKILL.md` 可作真实样本。
- 实际产出: `./gradlew build` 全绿、`CodeBotModel.java:494-500` 启动加载、`CodeBotModel.java:961-965` UI 提示

## 进度
- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8
- [ ] T9

```

```markdown
# ch11: Skills 系统 Checklist

## 1. 实现完整性

- [ ] `SkillCatalog.SkillMeta` record 在 `src/main/java/com/codebot/skill/SkillCatalog.java:24-33` 含 `name / description / whenToUse / tags / allowedTools / mode / model / forkContext` 八个字段
- [ ] `SkillCatalog.Skill` record 在 `SkillCatalog.java:35-39` 含 `meta / promptBody / sourceDir / bodyLoaded`，提供 `withBody` 副本构造
- [ ] `SkillCatalog` 状态在 `SkillCatalog.java:43-45`：`skills / sources` 全部 `LinkedHashMap` 保序
- [ ] `register / get / getFull / list / source / reload / loadFromDirectory` 在 `SkillCatalog.java:49-158` 实现
- [ ] `getFull` 在 `SkillCatalog.java:71-89` 触发 phase-2 热重载，sourceDir 为 null 直接返回缓存，读失败 `IOException ignored` 后保留旧缓存
- [ ] `loadCatalog(workDir)` 在 `SkillCatalog.java:107-123` 按 tier1 builtin（占位）→ tier2 `~/.codebot/skills/` → tier3 `<workDir>/.codebot/skills/` 顺序加载
- [ ] `loadTier` 在 `SkillCatalog.java:142-158` 容错：目录不存在 / list 抛 IOException 都静默跳过
- [ ] `loadSkill(dir)` 在 `SkillCatalog.java:184-199` 优先 `skill.yaml + prompt.md`，否则 `SKILL.md`，都不存在返回 null
- [ ] `parseSkillMD` 在 `SkillCatalog.java:221-262` 处理可选 YAML frontmatter；YAML 解析失败降级为「无 frontmatter」；缺描述时从 body 第一行非标题行回退
- [ ] `metaFromMap` 在 `SkillCatalog.java:264-313`：name 缺省取目录名小写+空格换 `-`；mode 缺省 `inline` 并兼容 `context: fork`；`fork_context` 缺省 `none`
- [ ] `buildActiveContext(activeSkillNames)` 在 `SkillCatalog.java:166-180` 拼 `## Active Skills` 段，空集合返回 ""
- [ ] `SkillHost` 接口在 `src/main/java/com/codebot/skill/SkillHost.java:12-19` 提供 `activateSkill / setToolFilter / toolRegistry`
- [ ] `SkillForkHost extends SkillHost` 在 `src/main/java/com/codebot/skill/SkillForkHost.java:12-17` 追加 `runSubAgent / snapshotParentMessages`
- [ ] `SkillExecutor.executeInline` 在 `src/main/java/com/codebot/skill/SkillExecutor.java:25-37` 顺序：`assertAllowedToolsExist` → `substituteArguments` → `activateSkill` → 按 `allowed_tools` 调 `setToolFilter`
- [ ] `SkillExecutor.executeFork` 在 `SkillExecutor.java:43-48` 顺序：校验 → 渲染 → `buildForkSeed` → `runSubAgent`
- [ ] `substituteArguments` 在 `SkillExecutor.java:50-58`：args 空白原样返回；含 `$ARGUMENTS` 占位符替换；否则追加 `## User Request` 段
- [ ] `buildForkSeed` 在 `SkillExecutor.java:60-74`：`full` 全量、`recent` 取尾 5 条（`FORK_RECENT_COUNT = 5`）、其他（含 `none`）返回 `List.of()`
- [ ] `assertAllowedToolsExist` 在 `SkillExecutor.java:76-88` 工具未注册时抛 `IllegalStateException`
- [ ] 边界处理: 空目录、目录不存在、坏 yaml、`allowed_tools` 为空都不抛异常

## 2. 接入完整性

- [ ] `grep -rn "new SkillCatalog" --include="*.java" /Users/codemelo/codebot/src` 命中 `CodeBotModel.java:494` 的非测试调用
- [ ] `grep -rn "skillCatalog.loadFromDirectory" --include="*.java" /Users/codemelo/codebot/src` 命中 `CodeBotModel.java:497`
- [ ] `grep -rn "wireSkillsToAgent" --include="*.java" /Users/codemelo/codebot/src` 命中 `CodeBotModel.java:500` / `CodeBotModel.java:511`
- [ ] 字段 `skillCatalog` 在 `CodeBotModel.java:102`；provider 就绪后初始化 `CodeBotModel.java:494-498`
- [ ] `registerSkillCommand(name)` 在 `CodeBotModel.java:518-533`：跳过已存在命令、注册 PROMPT 类型 `Command`、description 后缀 `[skill]`、handler 从 catalog 取 promptBody
- [ ] PROMPT 分发的 skill 分支在 `CodeBotModel.java:928-967`：`isSkill = cmd.description().endsWith("[skill]")`，命中后在 UI 上 println `skill(<name>) Successfully loaded skill`
- [ ] `/skills` 命令 handler 在 `src/main/java/com/codebot/command/CommandRegistry.java:255-265` 列出 `skillList` supplier 返回的技能名
- [ ] `skillList` supplier 在 `CodeBotModel.java:984-986`：`skillCatalog != null` 时返回 `list().stream().map(s -> s.name()).toList()`
- [ ] 入口路径：用户输入 `/<skill-name>` → `executeCommand`（CodeBotModel）→ PROMPT 分支 → `cmdRegistry.execute` 返回 promptBody → `conversation.addUserMessage` → `agent.run`

## 3. 编译与测试

- [ ] `cd /Users/codemelo/codebot && ./gradlew build` 通过
- [ ] `cd /Users/codemelo/codebot && ./gradlew compileJava` 无警告
- [ ] `com.codebot.skill` 包不 import `com.codebot.agent` / `com.codebot.tui`，仅通过 `SkillHost` / `SkillForkHost` 接口与外界交互

## 4. 端到端验证

- [ ] 启动 CodeBot 后输入 `/skills`，若 `.codebot/skills/` 下无技能则提示 `No skills installed.\n\nAdd skills to .codebot/skills/<skill-name>/SKILL.md`（`CommandRegistry.java:260`）
- [ ] 在 `.codebot/skills/skill-creator/SKILL.md` 现成样本下，启动后 `/skills` 列出 `skill-creator`
- [ ] 输入 `/skill-creator <args>` 触发 PROMPT 分支，UI 紧接出现 `skill(skill-creator) Successfully loaded skill`（`CodeBotModel.java:961-965`）
- [ ] Agent 新一轮 conversation 中可见两条 user message：第一条是 promptBody，第二条是 `<args>`（`CodeBotModel.java:937-942`）
- [ ] 修改 `.codebot/skills/skill-creator/SKILL.md` 的 body 后，下次执行该技能时通过 `getFull` 热重载到新内容（`SkillCatalog.java:71-89`）
- [ ] 留存证据：未提供截图（手动 TUI 验证不在课程验收流程要求范围内）

## 5. 文档

- [ ] `docs/java/ch11/spec.md` 存在
- [ ] `docs/java/ch11/tasks.md` 存在
- [ ] `docs/java/ch11/checklist.md` 存在
- [ ] Java 实现位于 `origin/java` 分支，包路径 `com.codebot.skill` / `com.codebot.command`

```



## ch12

```markdown
# 我的初步想法
- 用「事件 + 条件 + 动作」三要素描述一条规则；条件可省略表示无条件触发，事件和动作必须有
- 生命周期事件覆盖四个层级：会话级（会话起止）、轮次级（轮次起止）、消息级（发送前/接收后）、工具级（执行前/执行后），再加少量系统级事件（启动、退出、错误、压缩等）
- 工具执行前的事件具有拦截能力，可以基于工具参数内容做细粒度安全策略，被拦截后把拒绝原因作为工具结果反馈给 LLM，形成「拦截 → Agent 收到原因 → Agent 调整策略」的循环
- 条件表达式复用权限规则的匹配语法，支持精确、反向、正则、glob 四种操作符，逻辑组合用「全部满足」或「任一满足」二选一，不允许混用（避免引入运算符优先级和完整表达式引擎）
- 四种动作执行器：执行 shell 命令、注入提示词消息、发起 HTTP 请求、启动子 Agent（子 Agent 这种先占位）
- 执行控制三件套：只执行一次、后台异步执行、命令超时；并强制工具拦截类事件不允许异步
- 动作模板里支持上下文变量占位（事件名、工具名、文件路径、消息内容、错误信息、工具参数字段），未定义变量替换为空串而不是报错
- 辅助机制错误隔离原则：Hook 自身执行失败只记日志，绝不中断 Agent 主流程
- 从 YAML 声明式加载规则，加载时集中校验事件名、动作类型、拦截字段只能用在执行前事件、异步标记不能用在拦截事件、各动作类型必填字段，非法配置要能定位到具体规则
- 引擎需要嵌入 Agent Loop 的关键节点：会话起止、轮次起止、消息发送前/接收后、工具执行前（同步、可拦截）、工具执行后
```

### Go

```markdown
# ch12: Hook 系统 Spec

## 1. 背景

Agent 主流程在工具调用前后、session 起止、turn 起止等关键节点都有「副作用钩子」的需求：工具调用前阻断危险命令、调用后推日志到外部系统、用户提交前注入额外提示词、后台异步触发通知 / 监控。把这些写死在 agent 循环里既不优雅又难配置。Hook 系统把这层做成可声明（yaml）+ 条件匹配 + 多种动作类型的引擎，并保留 once / async / on_error 三种执行控制。

## 2. 目标

交付 `hooks.Engine`，从 `config.yaml` 加载 hook 数组，按事件名提供两种入口：普通事件用 `RunHooks` 跑全部命中钩子；`pre_tool_use` 用 `RunPreToolHooks` 允许阻断工具调用。Condition 支持 leaf 操作符（等 / 不等 / 正则 / glob）+ 复合（与 / 或）+ 反向（前缀 !）三类组合，变量覆盖 tool / event / file_path / message / args。Agent loop 在每次工具调用前后接入对应入口，TUI 负责从配置初始化引擎并挂到 Agent。

## 3. 功能需求

- F1: 提供 9 个事件类型常量（session_start / session_end / turn_start / turn_end / pre_send / post_receive / pre_tool_use / post_tool_use / shutdown），覆盖会话与工具生命周期。
- F2: 提供 4 个动作类型常量（command / script / prompt / http），其中 command 与 script 共享同一执行路径。agent 类型不在本章范围（见 Out of Scope）。
- F3: Condition DSL：
 - leaf 操作符：等于、不等于、正则匹配、glob 匹配
 - 复合：与、或，左结合且同优先级
 - 反向：前缀 !，仅作用于单个 leaf
 - 变量：tool、event、file_path、message、`args.<key>`
- F4: `RunHooks(ctx)` 按事件名过滤、按 condition 决定是否触发；async hook 立即返回占位结果，不阻塞。
- F5: `RunPreToolHooks(ctx)` 专门跑 `pre_tool_use` 事件：任何 reject 命中即返回阻断信号与原因；命令执行失败且 `OnError == "reject"` 时按 reject 处理。
- F6: 动作执行器:
 - command / script：bash 调外部命令，注入事件 / 工具 / 文件路径环境变量，stderr 拼到 stdout
 - prompt：直接把消息文本当输出返回
 - http：JSON POST，带超时与响应体大小上限
- F7: `Once` 控制：同一 hook ID 只触发一次。
- F8: `Async` 控制：goroutine 执行不阻塞主流程，结果走 notifications 队列。
- F9: 加载期校验 `Validate([]Hook) error`：参考 的 Zod discriminated union 模式，按 `Action.Type` 分支检查各类型必填字段（command 必有 command 字段、prompt 必有 message、http 必有可解析的 url），event 名必须在 9 事件白名单里，timeout 必须 ≥ 0；非法配置返回带 hook id / 字段路径的可读错误。`LoadHooks` 调用前必须先过校验，跑通才注入引擎。
- F10: command 动作超时执行：参考 的 `hookTimeoutMs = hook.timeout * 1000 ?? TOOL_HOOK_EXECUTION_TIMEOUT_MS (10min)` 策略，Go 端用 `exec.CommandContext + context.WithTimeout` 包子进程；`Action.Timeout` 配置为 0 时取 10 分钟默认值；超时后子进程被 kill，`HookResult.Success = false`，输出体包含「command timed out after Xs」可读提示。

## 4. 非功能需求

- N1: hook 执行不能 panic：condition 解析失败按「不命中」处理；动作执行错误按 OnError 策略走。
- N2: 并发安全：内部 mutex 保护 hooks / fired / notifications 状态；执行前拷贝快照，避免长时持锁。
- N3: HTTP hook 必须有超时与响应体大小限制，避免外网卡死或大响应阻塞 agent loop。
- N4: `RunPreToolHooks` reject 消息必须可读，缺消息时给 fallback。
- N5: `Validate` 出错时必须能定位到具体 hook：错误消息含 hook id 或 index + 出错字段名，参照 Zod safeParse 的 path + message 格式。
- N6: command 超时不能泄漏子进程：`exec.CommandContext` 必须在退出前确认子进程已结束或被 kill，避免僵尸进程。

## 5. 设计概要

- 核心数据结构:
 - `Hook`：ID / Event / Condition / Action / Reject / Once / Async / OnError，yaml 字段名小写，可用 if 代替 condition
 - `Action`：单结构承载所有动作类型，按 Type 字段分发，覆盖 command / message / url / method / headers / body / timeout
 - `HookContext`：事件名 / 工具名 / 工具参数 / 文件路径 / 消息 / 错误，供 condition 与执行器读
 - `HookResult`：hook ID / 输出 / 成功标志 / reject 标志
 - `Engine`：mutex + hooks + notifications + fired，注册表加执行状态
 - `defaultHookTimeout`：包级常量 `10 * time.Minute`，对应目标设计 `TOOL_HOOK_EXECUTION_TIMEOUT_MS`
- 主流程:
 1. main 启动 → TUI 接到 `[]hooks.Hook` 配置
 2. provider 就绪 → TUI 构造 Engine、调 `Validate(hooks)` 校验非法配置，错则向用户报错并 fallback 为空 hooks；通过则 `LoadHooks` 挂到 agent.Hooks
 3. agent loop 工具调用前调 `RunPreToolHooks`，被阻断时把 reject 消息当工具结果返回
 4. agent loop 工具调用后调 `RunHooks(post_tool_use)`
 5. condition 执行: `RunHooks` → snapshotHooks → shouldFire → evaluateCondition → 拆分复合 / 评估 leaf / 解析变量
 6. command 执行: `runCommand` → 选 timeout（hook.Action.Timeout 或 defaultHookTimeout）→ `exec.CommandContext` 包 bash -c → 超时时上下文 cancel → 子进程被 kill，HookResult 写入超时原因
- 调用链（模块层级）:
 - 启动: main → tui.New（带 hook 配置）→ Engine 初始化 → 挂到 agent.Hooks
 - 触发: agent.Run → executeTool → RunPreToolHooks → tool.Execute → RunHooks(post_tool_use)
- 与其他模块的交互:
 - 上行依赖：agent（loop 触发）、tui（生命周期与配置接入）、config（yaml 字段绑定）
 - 下行：无（hooks 包不 import 其他内部模块）

## 6. Out of Scope

- `agent` action type（CODEBOT.md 提到的第 4 种执行器）：本章不实现，建议在 ch13 SubAgent 稳定后再补，否则没法启子代理
- 已声明但未触发的事件（session_start / session_end / turn_start / turn_end / pre_send / post_receive / shutdown）：等业务场景出现再在 agent loop / TUI 补 emit
- `DrainNotifications`：当前没消费方，等通知中心模块出现后再接入或删除
- Hook DSL 的括号 / 短路求值：本实现两侧 leaf 都跑（leaf 解析便宜），不补复杂括号文法
- Hook 配置的热更新：必须重启或重新选 provider 才生效

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch12: Hook 系统 Tasks

## T1: 定义事件 / 动作类型常量与数据结构
- 影响文件: `internal/hooks/hooks.go`
- 依赖任务: 无
- 完成标准: 9 个 `EventName` 常量 + 4 个 `ActionType` 常量；`Hook / Action / HookContext / HookResult / Engine` 类型齐全且 yaml tag 正确。
- 实际产出: `hooks.go:19-88`

## T2: Condition DSL —— leaf / composite / inverse
- 影响文件: `internal/hooks/hooks.go`
- 依赖任务: T1
- 完成标准: 支持 `==/!=/=~/=*` 四种 leaf；支持 `&&/||` composite；支持 `!` 前缀 inverse；支持 `tool / event / file_path / message / args.<key>` 变量。
- 实际产出: `hooks.go:195-294`（evaluateCondition / splitComposite / evaluateLeaf / resolveVar）

## T3: Engine 核心 —— Register / RunHooks / Once / Async
- 影响文件: `internal/hooks/hooks.go`
- 依赖任务: T1, T2
- 完成标准: `NewEngine / LoadHooks / RunHooks / shouldFire / snapshotHooks` 全部实现；once 命中后跳过；async 通过 goroutine 异步执行返回 `(async)` 占位结果。
- 实际产出: `hooks.go:90-186`

## T4: Pre-tool 阻断专用入口
- 影响文件: `internal/hooks/hooks.go`
- 依赖任务: T3
- 完成标准: `RunPreToolHooks(ctx) (bool, string)`，按 reject 字段或 OnError=="reject" 命中失败的命令决定是否阻断；fallback 消息 `blocked by hook <ID>`。
- 实际产出: `hooks.go:127-147`

## T5: 三种动作执行器（command / prompt / http）
- 影响文件: `internal/hooks/hooks.go`
- 依赖任务: T3
- 完成标准:
 - command/script: bash -c 执行，注入 `CODEBOT_EVENT / CODEBOT_TOOL / CODEBOT_FILE_PATH`，stdout+stderr 合并；
 - prompt: 直接返回 Message；
 - http: POST/JSON 默认，10s 默认超时，限制响应体 64KB。
- 实际产出: `hooks.go:296-391`（executeAction / runCommand / runHTTP）

## T6: 单元测试
- 影响文件: `internal/hooks/hooks_test.go`
- 依赖任务: T1-T5
- 完成标准: 覆盖 leaf 四种操作符、composite、inverse、reject、once、http、async、on_error=reject。
- 实际产出: `hooks_test.go:12-174`（6 个测试用例 / `TestEvaluateConditionLeafOps` 单测就跑了 13 种 condition）

## T7: 接入主流程 —— config 绑定
- 影响文件: `internal/config/config.go`
- 依赖任务: T1
- 完成标准: `AppConfig` 含 `Hooks []hooks.Hook` 字段，yaml 反序列化能直接拿到 hooks 列表。
- 实际产出: `internal/config/config.go:82-87`

## T8: 接入主流程 —— TUI 装配 + Agent 触发
- 影响文件: `internal/tui/tui.go`、`internal/agent/agent.go`、`cmd/codebot/main.go`
- 依赖任务: T1-T5, T7
- 完成标准:
 - 入口透传: `main.go:32` 把 `cfg.Hooks` 传给 `tui.New`；
 - TUI 装配: `tui.go:191`(`New`) 接收 hookConfigs，`tui.go:371-375` 与 `tui.go:733-737` 在 agent 初始化时建 Engine 挂到 `ag.Hooks`；
 - Agent loop 触发: `agent.go:409-424`(`RunPreToolHooks`)、`agent.go:429-437`(`RunHooks(post_tool_use)`)。
- 实际产出: 同上。

## T9: 端到端验证
- 影响文件: 无
- 依赖任务: T8
- 完成标准: 在 `config.yaml` 配 `hooks: [{event: pre_tool_use, if: 'tool == "Bash" && args.command =~ /rm -rf/', action: {type: prompt, message: "blocked"}, reject: true}]`，TUI 起来后让 LLM 调 Bash + `rm -rf` 看到工具结果是 `Blocked by hook: blocked`；HTTP hook 用 `hooks_test.go:103-134` 的 `httptest.NewServer` 路径已覆盖。
- 实际产出: 由 `hooks_test.go` 中 `TestRunPreToolHooksReject / TestHookHTTPAction / TestHookOnErrorReject` 覆盖核心端到端逻辑；TUI 配置文件流程见 checklist §5。

## T10: 加载期校验 `Validate([]Hook) error`
- 影响文件: `internal/hooks/hooks.go`、`internal/hooks/hooks_test.go`
- 依赖任务: T1
- 完成标准:
 - 新增 `Validate(hooks []Hook) error`，遍历每个 hook，按 Action.Type 分支校验：
 - command/script：`Command` 非空
 - prompt：`Message` 非空
 - http：`URL` 必须能被 `url.Parse` 解析且 scheme 是 http/https
 - agent：`Message` 或 `Command` 至少有一个非空（占位 stub 也要 prompt）
 - 未知 Type：报错
 - Event 名必须在 9 事件白名单内（与 `EventName` 常量集合一致）
 - `Timeout >= 0`（Zod 用 positive；这里 0 表示「用默认值」，符合 Go 零值约定，所以放宽到 >=0）
 - 错误消息含 hook id（无 id 则用 index）+ 出错字段名，例如：`hook[0] (id="auto-format"): action.command must be non-empty for type "command"`
 - 多个错误用 `errors.Join` 聚合一次性回报，不要遇到第一个就 short-circuit
 - `LoadHooks` 不调 Validate（保持原有低耦合）；改在 `cmd/codebot/main.go` 或 `internal/config/config.go` 加载完 yaml 后调 Validate，错误打到 stderr，让 TUI 用空 hooks 启动而不是 crash
- 实际产出: 待实现

## T11: command 动作超时执行
- 影响文件: `internal/hooks/hooks.go`、`internal/hooks/hooks_test.go`
- 依赖任务: T5
- 完成标准:
 - 新增包级常量 `defaultHookTimeout = 10 * time.Minute`（保持一致）
 - `runCommand` 改用 `exec.CommandContext`：
 - 超时 = `hook.Action.Timeout`，零值时取 `defaultHookTimeout`
 - `ctx, cancel := context.WithTimeout(context.Background(), timeout)`，defer cancel
 - 超时时返回 `HookResult{Success: false, Output: "command timed out after Xs: <stdout/stderr>"}`，并把超时事实写进 output 让用户能在通知里看到
 - 现有 stdout/stderr 合并、`CODEBOT_*` 环境变量注入逻辑保持不动
 - 测试：超时配 `100ms`，命令是 `sleep 1`，断言 `Success == false` 且 output 包含 "timed out"
 - 测试：超时配 `0`（默认），命令是 `echo ok`，断言能在毫秒级返回并 Success
- 实际产出: 待实现

## 进度
- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8
- [ ] T9
- [ ] T10
- [ ] T11

```

```markdown
# ch12: Hook 系统 Checklist

## 1. 实现完整性

- [ ] 9 个 `EventName` 常量在 `internal/hooks/hooks.go:19-30`：session_start / session_end / turn_start / turn_end / pre_send / post_receive / pre_tool_use / post_tool_use / shutdown
- [ ] 4 个 `ActionType` 常量在 `hooks.go:32-39`：command / script / prompt / http
- [ ] 数据结构 `Hook / Action / HookContext / HookResult / Engine` 在 `hooks.go:41-88`，yaml tag 完整
- [ ] `NewEngine / LoadHooks / RunHooks / RunPreToolHooks / shouldFire / snapshotHooks / recordNotification / DrainNotifications` 全部在 `hooks.go:90-186`
- [ ] Condition DSL 支持 leaf（==/!=/=~/=*）+ composite（&&/||）+ inverse（!），实现在 `hooks.go:195-272`
- [ ] 变量解析支持 `tool / event / file_path / message / args.<key>`，实现在 `hooks.go:274-294`
- [ ] `executeAction` 在 `hooks.go:296-316` 按 ActionType 分发；`runCommand` 在 `hooks.go:318-339`；`runHTTP` 在 `hooks.go:341-391`
- [ ] `runCommand` 注入环境变量 `CODEBOT_EVENT / CODEBOT_TOOL / CODEBOT_FILE_PATH`，stderr+stdout 合并
- [ ] `runHTTP` 默认 POST，默认 10s 超时，自动塞 Content-Type，响应体限 64KB
- [ ] `Once` 控制按 hook ID 去重（`shouldFire` 中 `e.fired[h.ID] = true`）
- [ ] `Async` 控制走 goroutine 异步执行 + 占位结果 `(async)`（`RunHooks` 中 `h.Async` 分支）
- [ ] `OnError == "reject"` 命中失败命令时按 reject 处理（`RunPreToolHooks` 中 `!result.Success && h.OnError == "reject"`）
- [ ] `Validate(hooks []Hook) error` 已实现：按 Action.Type 分支校验 command/prompt/http/agent 各自必填字段，event 名必须在 9 事件白名单，timeout >= 0；错误消息包含 hook id（或 index）+ 出错字段名；多错误用 `errors.Join` 聚合一次性返回
- [ ] 包级常量 `defaultHookTimeout = 10 * time.Minute` 存在，保持一致
- [ ] `runCommand` 走 `exec.CommandContext`：超时 = `hook.Action.Timeout`，零值取 `defaultHookTimeout`；超时时 `HookResult.Success == false` 且 `Output` 含 "timed out" 关键字
- [ ] `runCommand` 仍注入 `CODEBOT_EVENT / CODEBOT_TOOL / CODEBOT_FILE_PATH` 环境变量，超时改动不破坏既有路径

## 2. 接入完整性

- [ ] `grep -rn "hooks.NewEngine" --include="*.go" /Users/codemelo/codebot` 命中 `internal/tui/tui.go:372` 和 `tui.go:734` 两个非测试调用方
- [ ] `grep -rn "RunPreToolHooks\|RunHooks(" --include="*.go" /Users/codemelo/codebot | grep -v _test` 命中 `internal/agent/agent.go:416` 与 `agent.go:430` 两个 agent loop 触发点
- [ ] Config 绑定：`internal/config/config.go:86` 含 `Hooks []hooks.Hook` 字段，main.go:32 透传 `cfg.Hooks` 进 `tui.New`
- [ ] Agent 字段：`internal/agent/agent.go:37` 含 `Hooks *hooks.Engine`，TUI 在 provider 初始化路径上挂上
- [ ] 入口路径：`config.yaml.hooks → cfg.Hooks → tui.New(..., hooks) → m.hookConfigs → hooks.NewEngine + LoadHooks → ag.Hooks → agent.Run → executeTool 调 RunPreToolHooks/RunHooks`
- [ ] 死代码 1 已解决（2026-05-21）：`Engine.DrainNotifications` 在 `internal/tui/tui.go:500-507drainTaskNotifications` 中消费，把 hook 输出包成 `<hook-notification>` 注入下一轮 system reminder。`grep -rn "Hooks.DrainNotifications" --include="*.go" /Users/codemelo/codebot` 返回 ≥1 条非测试调用方。
- [ ] 死代码 2 已解决（2026-05-21）：`ActionScript` 常量已删。
- [ ] 缺失事件触发已补 6/7（2026-05-21）：`EventSessionStart / SessionEnd / TurnStart / TurnEnd / PreSend / PostReceive` 由 `Agent.emitHook` 在 Run() 入口/出口、每轮迭代头尾、Stream 前/后 emit（`agent.go` Run 函数）。`EventShutdown` 是进程级信号，留作后续在 `cmd/main.go` 装信号处理器时补。
- [ ] 缺失 agent action 类型已解决（2026-05-21）：新增 `ActionAgent` 常量 + `Engine.AgentRunner` 字段；`executeAction` 走 `runAgent` 分支（`hooks.go:296-345`）；TUI 注册 `newAgentHookRunner` 闭包走 `llm.Client.Stream` 单轮调用。对应目标设计 `execAgentHook.ts:36 execAgentHook`。
- [ ] `Validate` 接入入口：`grep -rn "hooks.Validate" --include="*.go" /Users/codemelo/codebot` 至少命中 1 处非测试调用方（建议在 `cmd/codebot/main.go` 或 `internal/config/config.go` 的配置加载路径里）；非法 hook 配置启动时被打印到 stderr 而不是默默吞掉

## 3. 编译与测试

- [ ] `cd /Users/codemelo/codebot && go build ./internal/hooks/...` 通过
- [ ] `cd /Users/codemelo/codebot && go test ./internal/hooks/...` 全部测试通过：原 7 个测试 + 新增 `TestValidateCatchesMissingFields` / `TestValidateAggregatesAllErrors` / `TestValidateAcceptsGoodConfig` / `TestRunCommandTimeout` / `TestRunCommandDefaultTimeoutAllowsFastCommand`
- [ ] `go vet ./internal/hooks/...` 无警告
- [ ] `cd /Users/codemelo/codebot && go build ./...` 通过（hooks 包被 agent/tui/config import）

## 4. 端到端验证

- [ ] 在 `config.yaml` 配 pre_tool_use reject hook，启动 TUI 让 LLM 触发匹配的 Bash 命令，工具结果是 `Blocked by hook: <message>`（路径 `agent.go:416-424`）
- [ ] HTTP hook 由 `hooks_test.go:103-134` 用 `httptest.NewServer` 验证 POST + JSON Content-Type + 计数到达
- [ ] async hook 由 `hooks_test.go:136-156` 验证耗时 0.2s 的命令不阻塞主线
- [ ] once hook 由 `hooks_test.go:84-101` 验证第二次 RunHooks 返回空 results
- [ ] on_error=reject 由 `hooks_test.go:158-174` 验证退出码 7 的命令导致 RunPreToolHooks 阻断
- [ ] `Validate` 端到端：在临时 `config.yaml` 配一个非法 hook（如 `event: pre_tool_use, action: {type: command}`，缺 command 字段），`go run ./cmd/codebot` 启动时 stderr 看到 `hook[0]: action.command must be non-empty for type "command"` 形式的错误
- [ ] command 超时端到端：配 `timeout: 100ms` + `command: "sleep 5"` 的 post_tool_use hook，TUI 触发一次工具调用，从 `DrainNotifications` 拿到的 HookResult.Output 含 "timed out"
- [ ] 留存证据: 未提供 TUI 截图（手动验证不在课程验收流程要求范围内）

## 5. 文档

- [ ] `specs/go/ch12/spec.md` 存在
- [ ] `specs/go/ch12/tasks.md` 存在
- [ ] `specs/go/ch12/checklist.md` 存在
- [ ] commit 已落地为 `356deac feat: implement hooks system for pre and post tool execution`，但 message 未含章节号 `ch12` 与三件套关闭标记。建议在下一次 commit 三件套时改写为 `docs(ch12): close spec/tasks/checklist for hooks system`

```

### Python

```markdown
# ch12: Hook 系统 Spec

## 1. 背景

Agent 主流程在工具调用前后、session 起止、turn 起止、消息收发等关键节点都有「副作用钩子」的需求：工具调用前阻断危险命令、调用后异步推日志到外部系统、用户提交前注入额外提示词、新 session 启动时拉取项目上下文。把这些写死在 Agent 循环里既不优雅又难配置。Hook 系统把这层做成可声明（yaml）+ 条件匹配 + 多种动作类型的引擎，并保留 once / async（async_exec）/ reject 三种执行控制。Python 实现使用 asyncio 协程作为执行单位，配合 `asyncio.create_subprocess_shell` 跑外部命令，`urllib.request` + `run_in_executor` 跑 HTTP。

## 2. 目标

交付 `codebot.hooks.HookEngine`，从 `config.yaml` 的 `hooks` 数组加载并经 `load_hooks` 校验后注入引擎；提供两类入口：普通事件用 `run_hooks(event, ctx)` 跑全部命中钩子；`pre_tool_use` 用 `run_pre_tool_hooks(ctx)` 允许阻断工具调用并返回 `ToolRejectedError`。Condition 支持 leaf 操作符（`==` / `!=` / `=~` / `~=`）+ 复合（`&&` / `||`，但同一表达式不允许混用）+ 变量覆盖 tool / event / `args.<key>`。`Agent.run` 在 session 入口、turn 入口、每次工具调用前后、消息收发前后接入对应入口；`CodeBotApp` 负责从配置初始化引擎并挂到 Agent，并在 mount / unmount 时触发 startup / shutdown 事件。

## 3. 功能需求

- F1: 提供 15 个生命周期事件常量（`LifecycleEvent` StrEnum）：session_start / session_end / turn_start / turn_end / pre_tool_use / post_tool_use / pre_send / post_receive / startup / shutdown / error / compact / permission_request / file_change / command_execute。
- F2: 提供 4 个动作类型（在 loader 的 `_VALID_ACTION_TYPES` 集合里）：command / prompt / http / agent；`agent` 当前为 stub，返回 "agent executor not yet implemented"。
- F3: Condition DSL：
  - leaf 操作符：`==`（等于）/ `!=`（不等于）/ `=~`（正则，包裹 `/.../` 时自动去除斜杠）/ `~=`（glob，走 `fnmatch.fnmatch`）
  - 复合：`&&` / `||`，但一行表达式只能用一种，混用抛 `ConditionParseError("Cannot mix '&&' and '||'")`
  - 变量解析：`tool` / `event` / `args.<key>`，由 `HookContext.get_field` 实现
  - 模板展开：动作字段中支持 `$EVENT / $TOOL_NAME / $FILE_PATH / $MESSAGE / $ERROR / $TOOL_ARGS.<key>`，由 `HookContext.expand` 替换
- F4: `HookEngine.run_hooks(event, ctx)` 按事件名 + condition 过滤后逐个执行；`async_exec=True` 的 hook 通过 `asyncio.ensure_future` 后台跑，不阻塞主协程；`prompt` 类型成功结果写入 `_prompt_messages` 队列，由 `get_prompt_messages()` 一次性取出后清空。
- F5: `HookEngine.run_pre_tool_hooks(ctx)` 专门跑 `pre_tool_use` 事件：任何 `hook.reject=True` 命中即返回 `ToolRejectedError(tool, reason, hook_id)`；执行异常被捕获并写 log，不影响主流程。
- F6: 动作执行器：
  - command：`asyncio.create_subprocess_shell` 拉子进程，stderr 合并到 stdout，命令字符串先经 `ctx.expand` 替换变量
  - prompt：直接对 `action.message` 做变量替换后返回，`success=True`
  - http：默认 POST，`urllib.request.Request` + `urlopen` 走 `run_in_executor` 异步化；带 body 时自动添加 `Content-Type: application/json`，响应体截断到 500 字节
  - agent：stub，仅记录日志并返回成功占位字符串
- F7: `once` 控制：`Hook.executed` 在首次触发后置 True，`Hook.should_run()` 在 `once=True` 且 `executed=True` 时返回 False，`find_matching_hooks` 据此跳过。
- F8: `async_exec` 控制：`run_hooks` 中以 `asyncio.ensure_future(self._run_single(hook, ctx))` 派发，不 await；`run_pre_tool_hooks` 不支持 async（loader 校验阻止）。
- F9: 加载期校验 `load_hooks(raw_hooks) -> list[Hook]`：
  - event 必须在 `LifecycleEvent` 白名单内
  - action.type 必须在 `_VALID_ACTION_TYPES = {"command","prompt","http","agent"}` 里
  - 按 `_REQUIRED_FIELDS` 强制每种类型的必填字段：command→command、prompt→message、http→url、agent→prompt
  - `reject=True` 只允许配在 `pre_tool_use` 事件上
  - `async=True` 不允许配在 `pre_tool_use` 事件上
  - `action.timeout` 必须是正整数（>0）
  - hook id 缺失时按 `f"{event}_{i}"` 自动生成
  - condition 字符串经 `parse_condition` 解析失败时包成 `HookConfigError`
  - 任意非法配置抛 `HookConfigError`，错误消息带 `f"hook '{id}'"` 或 `f"hook #{index+1}"` 定位
- F10: command 动作超时执行：`execute_command` 使用 `asyncio.wait_for(proc.communicate(), timeout=action.timeout)`，超时时 `proc.kill()` + `await proc.wait()` 清理子进程，返回 `ActionResult(output="Command timed out after Xs: <cmd>", success=False)`；`action.timeout` 默认值在 `Action` dataclass 中为 30 秒。

## 4. 非功能需求

- N1: hook 执行不能让 Agent 主协程崩溃：`_run_single` / `run_pre_tool_hooks` 内层 `try/except Exception`，捕获后记录 warning log 并写入 `_notifications`；condition 正则编译失败按「不命中」处理（`re.error` 返回 False）。
- N2: 并发安全：`HookEngine` 设计运行在单 event loop 上，所有状态修改在协程内顺序进行，无需显式锁；`async_exec` 派生的协程通过 `asyncio.ensure_future` 注册到 loop，由 loop 调度。
- N3: HTTP hook 必须有超时与响应体大小限制：`urlopen(req, timeout=30)`，响应体截断 500 字符；通过 `run_in_executor` 把同步 `urlopen` 放到默认线程池，避免阻塞 event loop。
- N4: `run_pre_tool_hooks` reject 时 `ToolRejectedError.reason` 必须取自 action 输出，Agent loop 包装为 `"Hook rejected: {reason}"` 作为工具结果。
- N5: `load_hooks` 出错时必须能定位到具体 hook：错误消息含 hook id（无则用 `f"hook #{i+1}"`）+ 出错字段名，例如 `hook 'auto-format': action type 'command' requires 'command' field`。
- N6: command 超时不能泄漏子进程：`asyncio.TimeoutError` 分支必须先 `proc.kill()` 再 `await proc.wait()`，确认子进程退出后才返回，避免僵尸进程。

## 5. 设计概要

- 核心数据结构：
  - `LifecycleEvent`（StrEnum，15 个值）：所有生命周期事件常量
  - `Action`（dataclass）：type / command / message / url / method / body / headers / prompt / timeout，单结构承载四种动作类型
  - `Condition`（dataclass）：field / operator / value，叶子条件
  - `ConditionGroup`（dataclass）：conditions 列表 + logic（"and"/"or"），复合条件
  - `Hook`（dataclass）：id / event / action / condition / reject / once / async_exec / executed，配合 `should_run()` / `mark_executed()` 方法
  - `HookContext`（dataclass）：event_name / tool_name / tool_args / file_path / message / error，配合 `get_field()` / `expand()` 方法
  - `ActionResult`（dataclass）：output / success
  - `HookNotification`（dataclass）：hook_id / event / output / success，drain 队列单元
  - `HookEngine`：hooks 列表 + `_prompt_messages` 队列 + `_notifications` 队列
  - `ToolRejectedError`（Exception）：tool / reason / hook_id
  - `HookConfigError`（Exception）：loader 报错
  - `ConditionParseError`（Exception）：condition 解析报错
- 主流程：
  1. `codebot/__main__.py:main` 启动 → `load_config` 读 `config.yaml` 拿到 `raw_hooks`（dict 列表）
  2. `load_hooks(raw_hooks)` 校验 + 解析成 `list[Hook]`；`HookConfigError` 时打 stderr 并 `sys.exit(1)`
  3. `HookEngine(hooks)` 构造 → 传入 `CodeBotApp(..., hook_engine=...)` → `Agent(..., hook_engine=...)`
  4. App `on_mount` 派发 `startup` 事件；`on_unmount` 派发 `shutdown` 事件
  5. `Agent.run` 入口派发 `session_start` → 每轮 `turn_start` → stream 前 `pre_send` → stream 后 `post_receive` → 退出循环时派发 `turn_end` + `session_end`
  6. 工具调用前调 `run_pre_tool_hooks(ctx)`：返回 `ToolRejectedError` 时打包成 `ToolResult(output=f"Hook rejected: {reason}", is_error=True)`，跳过实际 tool 执行
  7. 工具调用后调 `run_hooks("post_tool_use", ctx)`
  8. 每轮 hook 执行完后调 `_drain_hook_events()`，把 `HookNotification` 转成 `HookEvent` 事件流 yield 给 TUI 展示
- 调用链（模块层级）：
  - 启动：`codebot/__main__.py` → `load_hooks` → `HookEngine` → `CodeBotApp` → `Agent`
  - 触发：`Agent.run` → `_build_hook_context` → `hook_engine.run_hooks` / `run_pre_tool_hooks` → `execute_action` → `_EXECUTOR_MAP[type]`
- 与其他模块的交互：
  - 上行依赖：`agent.py`（loop 触发）、`app.py`（startup/shutdown）、`config.py`（raw_hooks 字段）、`__main__.py`（装配入口）
  - 下行：仅依赖 Python 标准库（asyncio / urllib / fnmatch / re）

## 6. Out of Scope

- `agent` action type 真正实现：当前是 stub，建议在 ch13 SubAgent 稳定后再补上（调 `Agent.run` 单轮）
- 已声明但未在主流程触发的事件（error / compact / permission_request / file_change / command_execute）：等业务场景出现再在对应模块 emit
- Condition DSL 的括号 / 短路求值 / 混合 `&&` 和 `||`：当前实现明确拒绝混用，需要复杂逻辑时建议拆成多个 hook
- Hook 配置的热更新：必须重启进程才生效
- HTTP hook 的认证 / 重试 / mTLS / 大响应流式处理：当前仅支持简单 POST + JSON

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch12: Hook 系统 Tasks

## T1: 定义生命周期事件常量
- 影响文件: `codebot/hooks/events.py`
- 依赖任务: 无
- 完成标准: `LifecycleEvent` StrEnum 包含 15 个事件值（session_start / session_end / turn_start / turn_end / pre_tool_use / post_tool_use / pre_send / post_receive / startup / shutdown / error / compact / permission_request / file_change / command_execute）；可直接和字符串比较。
- 实际产出: `codebot/hooks/events.py:6-30`

## T2: 数据模型 —— Action / Hook / HookContext / ActionResult / ToolRejectedError
- 影响文件: `codebot/hooks/models.py`
- 依赖任务: T1
- 完成标准:
  - `Action`（dataclass）字段齐：type / command / message / url / method / body / headers / prompt / timeout（默认 30）
  - `Hook`（dataclass）字段齐：id / event / action / condition / reject / once / async_exec / executed；`should_run` 检查 once + executed；`mark_executed` 翻 True
  - `HookContext`（dataclass）实现 `get_field("tool"/"event"/"args.<key>")` 与 `expand` 模板替换（$EVENT / $TOOL_NAME / $FILE_PATH / $MESSAGE / $ERROR / $TOOL_ARGS.<key>）
  - `ActionResult`（dataclass）含 output / success
  - `ToolRejectedError(Exception)` 带 tool / reason / hook_id 三字段
- 实际产出: `codebot/hooks/models.py:9-85`

## T3: Condition DSL —— leaf / 复合 / 解析
- 影响文件: `codebot/hooks/conditions.py`
- 依赖任务: T2
- 完成标准:
  - 支持四种 leaf 操作符：`==` / `!=` / `=~`（正则）/ `~=`（glob，走 `fnmatch.fnmatch`）
  - `=~` 包裹 `/.../` 时自动去除斜杠；`re.error` 时返回 False
  - 支持 `&&` / `||` 复合，但同一表达式混用时 `parse_condition` 抛 `ConditionParseError("Cannot mix '&&' and '||'")`
  - 空表达式或纯空白返回 None
  - 字符串值带双引号时自动去除
- 实际产出: `codebot/hooks/conditions.py:12-96`（`Condition` / `ConditionGroup` / `parse_condition` / `_parse_single`）

## T4: HookEngine 核心 —— find_matching_hooks / run_hooks / once / async_exec
- 影响文件: `codebot/hooks/engine.py`
- 依赖任务: T2, T3
- 完成标准:
  - `HookEngine.__init__` 初始化 `hooks` / `_prompt_messages` / `_notifications` 三个状态
  - `find_matching_hooks(event, ctx)` 按事件名 + `should_run` + condition 三层过滤
  - `run_hooks(event, ctx)` 顺序触发：`async_exec=True` 走 `asyncio.ensure_future(self._run_single(...))` 不 await；其余 await
  - `_run_single` 把 `prompt` 类型成功结果写入 `_prompt_messages`，所有结果写 `_notifications`，异常被 catch
  - `get_prompt_messages()` 一次性返回并清空 `_prompt_messages`
  - `drain_notifications()` 一次性返回并清空 `_notifications`
- 实际产出: `codebot/hooks/engine.py:21-110`

## T5: pre_tool_use 阻断专用入口
- 影响文件: `codebot/hooks/engine.py`
- 依赖任务: T4
- 完成标准: `run_pre_tool_hooks(ctx) -> ToolRejectedError | None`：顺序执行命中 hook，遇到 `hook.reject=True` 立即返回 `ToolRejectedError(tool=ctx.tool_name, reason=result.output, hook_id=hook.id)`；不允许 reject 时返回 None；执行异常被捕获记 log 不抛出。
- 实际产出: `codebot/hooks/engine.py:80-103`

## T6: 四种动作执行器（command / prompt / http / agent）
- 影响文件: `codebot/hooks/executors.py`
- 依赖任务: T2
- 完成标准:
  - `execute_command`：`asyncio.create_subprocess_shell` + stderr 合并到 stdout，命令字符串先 `ctx.expand` 替换变量；`asyncio.wait_for(..., timeout=action.timeout)` 超时时 `proc.kill()` + `await proc.wait()` 并返回 `success=False, output="Command timed out after Xs: <cmd>"`
  - `execute_prompt`：对 `action.message` 跑 `ctx.expand` 后包成 `ActionResult(output=..., success=True)`
  - `execute_http`：默认 POST，`urlopen(req, timeout=30)` 走 `run_in_executor` 异步化；body 非空时自动加 `Content-Type: application/json`；响应体截断 500 字符
  - `execute_agent`：stub，返回 `ActionResult(output="agent executor not yet implemented", success=True)`
  - `execute_action` 通过 `_EXECUTOR_MAP` 派发；未知 type 返回 `success=False`
- 实际产出: `codebot/hooks/executors.py:13-97`

## T7: 加载与校验 `load_hooks(raw_hooks)`
- 影响文件: `codebot/hooks/loader.py`
- 依赖任务: T1, T2, T3
- 完成标准:
  - `load_hooks(None)` / `load_hooks([])` 返回 `[]`
  - event 不在 `LifecycleEvent` 白名单内抛 `HookConfigError("invalid event ...")`
  - action.type 不在 `{"command","prompt","http","agent"}` 内抛 `HookConfigError("invalid action type ...")`
  - 按 `_REQUIRED_FIELDS` 校验每种类型必填字段（command→command、prompt→message、http→url、agent→prompt）
  - `reject=True` 且 event != "pre_tool_use" 抛错
  - `async=True` 且 event == "pre_tool_use" 抛错
  - timeout 非正整数抛错
  - hook id 缺失时按 `f"{event}_{i}"` 自动生成
  - condition 字符串经 `parse_condition` 解析失败时包成 `HookConfigError`
  - 错误消息含 hook id（无则用 `f"hook #{i+1}"`）
- 实际产出: `codebot/hooks/loader.py:7-115`

## T8: 包出口与公共 API
- 影响文件: `codebot/hooks/__init__.py`
- 依赖任务: T1-T7
- 完成标准: `codebot.hooks` 包对外暴露 `Action / ActionResult / Condition / ConditionGroup / ConditionParseError / Hook / HookConfigError / HookContext / HookEngine / LifecycleEvent / ToolRejectedError / load_hooks / parse_condition`；测试 `from codebot.hooks import HookEngine` 能跑通。
- 实际产出: `codebot/hooks/__init__.py:1-27`

## T9: 单元测试覆盖
- 影响文件: `tests/test_hooks.py`
- 依赖任务: T1-T8
- 完成标准: 覆盖以下场景（每个一个 `pytest` 类）：
  - `TestLifecycleEvent`：15 个事件数量与字符串比较
  - `TestHookContext`：get_field 四种字段、expand 全变量替换、未定义变量保留
  - `TestParseCondition`：单条件 / `&&` / `||` / 混用错误 / 空 / 正则 / 无操作符
  - `TestConditionEvaluate`：四种 leaf 操作符
  - `TestConditionGroupEvaluate`：and 全通过 / and 部分失败 / or 任一通过 / or 全失败 / 空 group
  - `TestCommandExecutor`：正常执行 / 变量替换 / 超时
  - `TestPromptExecutor`：返回 message
  - `TestHttpExecutor`：mock urlopen 验证 status
  - `TestAgentExecutor`：stub 返回不抛错
  - `TestExecuteAction`：派发 + 未知 type
  - `TestLoadHooks`：完整配置 / 自动 id / 空输入 / 各类非法配置错误
  - `TestHookEngine`：find_matching_hooks / condition 过滤 / once 过滤 / reject / 非 reject / prompt 消息收集 / 错误不抛 / async 不阻塞
  - `TestAgentHookIntegration`：mock LLM 触发 `rm -rf` 被 reject 后 Agent 收到 `Hook rejected: ...` 错误结果
- 实际产出: `tests/test_hooks.py:1-510`（13 个测试类）

## T10: 接入主流程 —— config 绑定
- 影响文件: `codebot/config.py`
- 依赖任务: T1
- 完成标准: `AppConfig` dataclass 新增 `raw_hooks: list[dict]` 字段（保留原始 dict，由 loader 二次解析）；`load_config` 在 `validate_config_structure` 后填入 `raw_hooks=validated["hooks"]`。
- 实际产出: `codebot/config.py:93`（字段定义）、`codebot/config.py:152`（赋值）

## T11: 接入主流程 —— Agent + App 装配 + Agent loop 触发
- 影响文件: `codebot/__main__.py`、`codebot/app.py`、`codebot/agent.py`
- 依赖任务: T2-T8, T10
- 完成标准:
  - 入口：`codebot/__main__.py:40-45` 调 `load_hooks(config.raw_hooks)`，`HookConfigError` 时打 stderr + `sys.exit(1)`，否则 `HookEngine(hooks)` 传给 `CodeBotApp`
  - App 装配：`codebot/app.py:515 / 526` 接收 `hook_engine` 参数并持有；`codebot/app.py:658` 把 `hook_engine` 透传给 `Agent`
  - 生命周期：`codebot/app.py:803-808`（startup）/`codebot/app.py:1581-1587`（shutdown）派发对应事件
  - Agent 字段：`codebot/agent.py:296 / 314` 接收 `hook_engine` 参数；`codebot/agent.py:371-382` 实现 `_build_hook_context`；`codebot/agent.py:384-394` 实现 `_drain_hook_events`
  - Agent loop 触发点：
    - session_start：`codebot/agent.py:407-410`
    - turn_start：`codebot/agent.py:427-430`
    - pre_send：`codebot/agent.py:460-463` + `get_prompt_messages` 注入下一轮
    - post_receive：`codebot/agent.py:509-512`
    - pre_tool_use：`codebot/agent.py:625-636`（reject 时把 `Hook rejected: {reason}` 包成 `ToolResult` 并 yield `ToolResultEvent(is_error=True)`）
    - post_tool_use：`codebot/agent.py:674-682`
    - turn_end + session_end：`codebot/agent.py:569-573`
- 实际产出: 同上

## T12: 端到端验证
- 影响文件: 无
- 依赖任务: T11
- 完成标准: 在 `config.yaml` 配 `hooks: [{event: pre_tool_use, if: 'tool == "Bash" && args.command =~ /rm\s+-rf/', action: {type: prompt, message: "blocked"}, reject: true}]`，启动 `python -m codebot` 让 LLM 调 Bash + `rm -rf` 看到工具结果是 `Hook rejected: blocked`；HTTP hook / async hook / once hook 路径由 `tests/test_hooks.py` 中对应测试覆盖。
- 实际产出: 由 `TestAgentHookIntegration.test_pre_tool_use_reject_skips_tool` + `TestHookEngine` 系列覆盖；配置文件手测见 checklist §4。

## 进度
- [ ] T1
- [ ] T2
- [ ] T3
- [ ] T4
- [ ] T5
- [ ] T6
- [ ] T7
- [ ] T8
- [ ] T9
- [ ] T10
- [ ] T11
- [ ] T12

```

```markdown
# ch12: Hook 系统 Checklist

## 1. 实现完整性

- [ ] 15 个生命周期事件常量在 `codebot/hooks/events.py:6-30`：session_start / session_end / turn_start / turn_end / pre_tool_use / post_tool_use / pre_send / post_receive / startup / shutdown / error / compact / permission_request / file_change / command_execute；`len(LifecycleEvent) == 15`
- [ ] 4 个动作类型在 `codebot/hooks/loader.py:8` 的 `_VALID_ACTION_TYPES` 集合：command / prompt / http / agent
- [ ] 数据结构 `Action / ActionResult / Hook / HookContext / ToolRejectedError` 在 `codebot/hooks/models.py:9-85`，字段齐全
- [ ] `Condition / ConditionGroup / parse_condition / _parse_single` 在 `codebot/hooks/conditions.py:12-96`；leaf 操作符 `==/!=/=~/~=` 全部实现
- [ ] `_OPERATORS = ("==", "!=", "=~", "~=")` 常量在 `codebot/hooks/conditions.py:54`
- [ ] 同一表达式混用 `&&` 和 `||` 时 `parse_condition` 抛 `ConditionParseError("Cannot mix '&&' and '||' in a single condition expression")`（`codebot/hooks/conditions.py:79-83`）
- [ ] `HookEngine / HookNotification` 在 `codebot/hooks/engine.py:14-110`；`__init__` 初始化 `hooks` / `_prompt_messages` / `_notifications` 三个状态
- [ ] `find_matching_hooks` 三层过滤（event / should_run / condition）在 `codebot/hooks/engine.py:31-41`
- [ ] `run_hooks` 中 `async_exec=True` 走 `asyncio.ensure_future(self._run_single(hook, ctx))` 不 await（`codebot/hooks/engine.py:43-50`）
- [ ] `_run_single` 把 `prompt` 类型成功结果写入 `_prompt_messages`，所有结果写 `_notifications`，异常被 catch（`codebot/hooks/engine.py:52-78`）
- [ ] `run_pre_tool_hooks` 遇到 `hook.reject=True` 即返回 `ToolRejectedError`（`codebot/hooks/engine.py:96-102`）
- [ ] `get_prompt_messages()` 一次性取出并清空（`codebot/hooks/engine.py:105-108`）
- [ ] `drain_notifications()` 一次性取出并清空（`codebot/hooks/engine.py:110-113`）
- [ ] `execute_command` 在 `codebot/hooks/executors.py:13-35`：`asyncio.create_subprocess_shell` + `stderr=STDOUT` 合并，超时时 `proc.kill()` + `await proc.wait()`，output 含 "timed out"
- [ ] `execute_prompt` 在 `codebot/hooks/executors.py:38-40` 仅做模板替换
- [ ] `execute_http` 在 `codebot/hooks/executors.py:43-72`：默认 POST，`urlopen(req, timeout=30)`，body 非空时自动加 `Content-Type: application/json`，响应体截断 500 字符
- [ ] `execute_http` 通过 `loop.run_in_executor(None, _do_request)` 把同步 urlopen 放到默认线程池
- [ ] `execute_agent` 在 `codebot/hooks/executors.py:75-81` 是 stub，返回 "agent executor not yet implemented"
- [ ] `_EXECUTOR_MAP` + `execute_action` 派发在 `codebot/hooks/executors.py:84-97`
- [ ] `load_hooks` 实现完整校验链路在 `codebot/hooks/loader.py:25-115`：event 白名单、action.type 白名单、必填字段、reject/async 与 event 的约束、timeout 正整数、自动 id、condition 解析失败包错
- [ ] `Hook.should_run()` 在 `codebot/hooks/models.py:43-46` 检查 once + executed，配合 `mark_executed()` 实现单次触发

## 2. 接入完整性

- [ ] `grep -rn "HookEngine(" /Users/codemelo/codebot --include="*.py"` 命中 `codebot/__main__.py:45` 一个非测试调用方
- [ ] `grep -rn "run_pre_tool_hooks\|run_hooks(" /Users/codemelo/codebot --include="*.py" | grep -v test` 至少命中 `codebot/agent.py` 7 处（session_start / turn_start / pre_send / post_receive / pre_tool_use / post_tool_use / turn_end+session_end）+ `codebot/app.py` 2 处（startup / shutdown）
- [ ] Config 绑定：`codebot/config.py:93` 含 `raw_hooks: list[dict] = field(default_factory=list)` 字段；`codebot/config.py:152` 在 `load_config` 中填 `raw_hooks=validated["hooks"]`
- [ ] Agent 字段：`codebot/agent.py:296` 构造参数含 `hook_engine: HookEngine | None = None`；`codebot/agent.py:314` 赋值 `self.hook_engine = hook_engine`
- [ ] App 装配：`codebot/app.py:515` 构造参数含 `hook_engine`；`codebot/app.py:526` 赋值；`codebot/app.py:658` 透传给 Agent
- [ ] 入口路径：`config.yaml` `hooks` → `config.raw_hooks` → `__main__.py:load_hooks` → `HookEngine(hooks)` → `CodeBotApp(hook_engine=...)` → `Agent(hook_engine=...)` → `agent.run` 内 emit
- [ ] 工具调用前 `pre_tool_use` 在 `codebot/agent.py:625-636`，reject 时打包成 `ToolResult(output=f"Hook rejected: {rejection.reason}", is_error=True)` 并 yield `ToolResultEvent(is_error=True)`，`continue` 跳过实际执行
- [ ] 工具调用后 `post_tool_use` 在 `codebot/agent.py:674-682`
- [ ] startup 事件在 `codebot/app.py:803-808` 通过 `asyncio.ensure_future` 派发；shutdown 事件在 `codebot/app.py:1581-1587` await 派发
- [ ] `_build_hook_context` 在 `codebot/agent.py:371-382` 统一构造 `HookContext`
- [ ] `_drain_hook_events` 在 `codebot/agent.py:384-394` 把 `HookNotification` 转成 `HookEvent` 流给 TUI 展示
- [ ] `pre_send` 钩子注入：`codebot/agent.py:466-468` 调 `get_prompt_messages()` 把 prompt 类型 hook 输出注入下一轮 LLM 请求
- [ ] 非法 hook 配置启动时打 stderr 而非 crash：`codebot/__main__.py:40-43` 捕获 `HookConfigError` 并 `sys.exit(1)`

## 3. 编译与测试

- [ ] `cd /Users/codemelo/codebot && ruff check codebot/hooks/ tests/test_hooks.py` 通过
- [ ] `cd /Users/codemelo/codebot && pytest tests/test_hooks.py -v` 全部测试通过：覆盖 `TestLifecycleEvent` / `TestHookContext` / `TestParseCondition` / `TestConditionEvaluate` / `TestConditionGroupEvaluate` / `TestCommandExecutor` / `TestPromptExecutor` / `TestHttpExecutor` / `TestAgentExecutor` / `TestExecuteAction` / `TestLoadHooks` / `TestHookEngine` / `TestAgentHookIntegration` 共 13 个测试类
- [ ] `cd /Users/codemelo/codebot && python -c "from codebot.hooks import HookEngine, load_hooks, LifecycleEvent; print(len(LifecycleEvent))"` 输出 `15`
- [ ] `cd /Users/codemelo/codebot && python -c "from codebot.agent import Agent; from codebot.app import CodeBotApp"` 无 ImportError，确认 hooks 包被 agent/app 正确 import

## 4. 端到端验证

- [ ] 在 `config.yaml` 配 pre_tool_use reject hook（例如 `tool == "Bash" && args.command =~ /rm\s+-rf/`），`python -m codebot` 启动 TUI 让 LLM 触发匹配的 Bash 命令，工具结果是 `Hook rejected: <message>`（路径 `codebot/agent.py:625-636`）
- [ ] HTTP hook 由 `tests/test_hooks.py` 中 `TestHttpExecutor.test_mock_request` 用 `unittest.mock.patch` mock `urlopen` 验证 status 200 + 响应体
- [ ] async hook 由 `tests/test_hooks.py` 中 `TestHookEngine.test_async_hook_does_not_block` 验证 `sleep 5` 不阻塞主协程返回
- [ ] once hook 由 `tests/test_hooks.py` 中 `TestHookEngine.test_once_filter` 验证 `mark_executed()` 后 `find_matching_hooks` 返回空
- [ ] reject 端到端由 `tests/test_hooks.py` 中 `TestAgentHookIntegration.test_pre_tool_use_reject_skips_tool` 验证 mock LLM 调 `rm -rf /` 时 Agent 拿到的 `ToolResultEvent.is_error == True` 且 output 含 `Hook rejected`
- [ ] command 超时端到端：`tests/test_hooks.py` 中 `TestCommandExecutor.test_timeout` 验证 `sleep 10` + `timeout=1` 时 `success == False` 且 output 含 "timed out"
- [ ] 加载校验端到端：在临时 `config.yaml` 配一个非法 hook（如 `event: pre_tool_use, action: {type: command}` 缺 command），`python -m codebot` 启动时 stderr 看到 `Hook config error: hook #1: action type 'command' requires 'command' field` 形式的错误并退出码 1（`codebot/__main__.py:40-43`）
- [ ] 留存证据：未提供 TUI 截图（手动验证不在课程验收流程要求范围内）

## 5. 文档

- [ ] `docs/python/ch12/spec.md` 存在
- [ ] `docs/python/ch12/tasks.md` 存在
- [ ] `docs/python/ch12/checklist.md` 存在
- [ ] commit 已落地到 Python 分支 hooks 子系统；建议下一次三件套关闭 commit 使用形如 `docs(ch12-python): close spec/tasks/checklist for hooks system` 的消息

```

### Java

```markdown
# ch12: Hook 系统 Spec

## 1. 背景

Agent 主流程在工具调用前后、session 起止、turn 起止等关键节点都有「副作用钩子」的需求：工具调用前阻断危险命令、调用后推日志到外部系统、session 起来时注入额外提示词。把这些写死在 agent 循环里既不优雅又难配置。Hook 系统把这层做成可声明（yaml）+ 简单条件匹配 + 两种动作类型的引擎，并通过 `reject` 字段让 `pre_tool_use` 钩子能阻断工具调用。Java 版以「最小可用」为目标，仅覆盖 command 和 prompt 两种动作，复杂语义（async / once / on_error / http）留到后续章节增量补齐。

## 2. 目标

交付 `com.codebot.hook.HookEngine`，从 `config.yaml` 加载 hook 列表，按事件名提供两种入口：普通事件用 `runHooks(ctx)` 跑全部命中钩子；`pre_tool_use` 用 `runPreToolHooks(toolName, args)` 返回 `PreToolResult` 允许阻断工具调用。Condition 支持 `==` 等值 和 `=~` 正则两种 leaf 操作符，变量覆盖 tool / event / file_path / message / args.<key>。CodeBotModel 在 TUI 初始化阶段从 `List<HookConfig>` 装配 Engine，session_start / turn_start / turn_end 由 `fireHook` 在生命周期节点触发，工具级 pre / post hook 由 `StreamingExecutor.executeSingle` 调用。

## 3. 功能需求

- F1: 提供 9 个事件枚举值（SESSION_START / SESSION_END / TURN_START / TURN_END / PRE_SEND / POST_RECEIVE / PRE_TOOL_USE / POST_TOOL_USE / SHUTDOWN），覆盖会话与工具生命周期。
- F2: 提供 3 个动作枚举值（COMMAND / SCRIPT / PROMPT），其中 SCRIPT 仅占位、不实际执行（落到 default 分支返回 unknown action type）。HTTP / agent 类型不在本章范围（见 Out of Scope）。
- F3: Condition DSL（极简版）:
 - leaf 操作符：`==`（等值）、`=~`（正则匹配）
 - 不支持复合（`&&`/`||`）、不支持反向（`!`）
 - 未识别操作符时按「真」处理（与 Go 版兼容，不报错）
 - 变量：tool、event、file_path、message、`args.<key>`
- F4: `runHooks(HookContext ctx)` 按事件名过滤、按 condition 决定是否触发；同步执行全部命中钩子，结果同时写入 `notifications` 队列。
- F5: `runPreToolHooks(String toolName, Map<String, Object> args)` 专门跑 PRE_TOOL_USE 事件：构造 ctx → 按 condition 过滤 → 命中 reject 钩子时执行 action 并立即返回 `PreToolResult(true, output)`；无 reject 命中时返回 `PreToolResult(false, "")`。
- F6: 动作执行器:
 - COMMAND：`ProcessBuilder("bash", "-c", h.action().command())` 启子进程；环境变量注入 `CODEBOT_EVENT` / `CODEBOT_TOOL`；stdout + stderr 同步读取并合并；`waitFor()` 退出码 0 视作 success
 - PROMPT：直接把 `action.message()` 当 output 返回，success = true
 - SCRIPT 及未知 type：返回 `HookResult(id, "Unknown action type: ...", false, false)`
- F7: 数据结构使用 Java record:
 - `Action(ActionType type, String command, String message)`
 - `Hook(String id, EventName event, String condition, Action action, boolean reject)`
 - `HookContext(EventName event, String toolName, Map<String,Object> toolArgs, String filePath, String message, String error)`
 - `HookResult(String hookId, String output, boolean success, boolean reject)`
 - `PreToolResult(boolean rejected, String message)`
- F8: 提供 `loadHooks(List<Hook>)` 替换内部 hooks 列表、`addHook(Hook)` 增量追加；`drainNotifications()` 取走积累的执行结果并清空队列（当前 TUI 未消费，留作后续接入）。
- F9: 配置数据类 `com.codebot.config.HookConfig`：字段 id / event / condition / type / command / message / reject 用经典 POJO + getter / setter 形式，便于 SnakeYAML 反序列化。
- F10: 入口透传链路：`config.yaml.hooks` → `AppConfig.hooks` → `CodeBot.main` 把 `cfg.getHooks()` 传给 `CodeBotModel` 构造函数 → `CodeBotModel` 构造期把 `List<HookConfig>` 翻译成 `List<HookEngine.Hook>` 并 `loadHooks`，agent 初始化路径上调 `agent.setHookEngine(hookEngine)`。

## 4. 非功能需求

- N1: hook 执行不能抛出异常打断 Agent 主流程：command 子进程 `IOException / InterruptedException` 必须被捕获，返回 `success=false` 的 HookResult；condition 解析失败（如正则非法）按「不命中」处理。
- N2: `runCommand` 中断处理：catch `InterruptedException` 时必须调 `Thread.currentThread().interrupt()` 保留中断状态，避免上层虚拟线程丢失取消信号。
- N3: stdout / stderr 必须用 `readAllBytes()` 一次性读完再 `waitFor()`，避免子进程因 stdout 缓冲区满而死锁。
- N4: condition 字符串末尾的引号（`"`、`'`、`/`）必须 strip 后再比较，让 yaml 里写 `tool == "Bash"` 或 `event =~ /session.*/` 都能匹配。
- N5: Engine 状态目前不要求并发安全：hooks 列表只在 TUI 构造期写入、运行期只读；notifications 当前无消费方，并发竞态留待后续接入消费者时再加锁。

## 5. 设计概要

- 核心数据结构:
 - `HookEngine.EventName`：9 个 enum 值 + `value()` 返回 snake_case 字符串
 - `HookEngine.ActionType`：3 个 enum 值（command / script / prompt）
 - 5 个 record（Action / Hook / HookContext / HookResult / PreToolResult）封装数据流
 - `private final List<Hook> hooks`：注册的钩子列表
 - `private final List<HookResult> notifications`：执行结果累计，留给 `drainNotifications` 取
- 主流程:
 1. main 启动 → ConfigLoader 读 yaml → `AppConfig.hooks` 拿到 `List<HookConfig>`
 2. `CodeBot.main` 把 `cfg.getHooks()` 透传给 `new CodeBotModel(providers, mcpServers, hooks)`
 3. `CodeBotModel` 构造函数里 `new HookEngine()` + 把 `HookConfig` 翻译成 `HookEngine.Hook` + `loadHooks`
 4. provider 就绪 → `agent.setHookEngine(hookEngine)` + `fireHook(SESSION_START, null, null)`
 5. 用户每次发消息 → `sendUserMessage` / 命令分支调 `fireHook(TURN_START, ...)`
 6. Agent loop 工具调用：`StreamingExecutor.executeSingle` 先 `hookEngine.runPreToolHooks(toolName, args)` → 被阻断时把 `"Rejected by hook: <msg>"` 当 ToolResult 返回；通过后正常执行工具 → 结束调 `hookEngine.runHooks(post_tool_use ctx)`
 7. agent loop 结束 → `LoopComplete` 事件触发 `fireHook(TURN_END, null, null)`
- 调用链（模块层级）:
 - 启动: `CodeBot.main` → `CodeBotModel.<init>` → `HookEngine` 初始化 → `loadHooks` 挂到 `CodeBotModel.hookEngine` 字段 → `agent.setHookEngine` 透传到 `Agent.hookEngine`
 - 触发: `Agent.run` → `agentLoop` → `new StreamingExecutor(registry, checker, hookEngine, queue)` → `executeAll` → `executeSingle` → `runPreToolHooks` → `tool.execute` → `runHooks(post_tool_use)`
- 与其他模块的交互:
 - 上行依赖：`com.codebot.agent`（`Agent` 持引用，`StreamingExecutor` 调用 pre / post 入口）、`com.codebot.tui.CodeBotModel`（生命周期 + 配置装配）、`com.codebot.config`（POJO 反序列化）
 - 下行：无（hook 包仅依赖 JDK 标准库）

## 6. Out of Scope

- `agent` 动作类型：依赖 SubAgent 系统，本章不实现，留到 ch13 之后再补
- `http` 动作类型：当前 Java 版没有 HTTP 调用栈也没有响应体大小约束，等业务需要时再加；ActionType 枚举先不引入 HTTP 占位
- `script` 动作类型：虽然枚举里有 SCRIPT，但 `executeAction` 落到 default 分支返回 unknown action type；本章不补 script 执行路径，等场景需要时再补
- `once` / `async` / `on_error` 三种执行控制：当前所有钩子同步执行、每次都触发、出错就当失败处理；不补复杂的 fire-once / 异步 goroutine / 失败回滚语义
- Condition DSL 的 `!=` 反向、`~=` glob、`&&` / `||` 复合表达式：Java 版只实现 `==` 和 `=~` 两种 leaf；多条件需求由用户拆成多个独立 hook 来表达
- 加载期 `Validate`：当前 `loadHooks` 不做合法性校验；非法的 ActionType / EventName 字符串走 `parseEventName` / `parseActionType` 的 default 分支落到 SESSION_START / COMMAND，安静兜底
- Hook 命令的超时：`runCommand` 当前用同步 `waitFor()` 等到底，不带 timeout；长命令需要超时控制时再补 `waitFor(long, TimeUnit)` 或 `destroyForcibly()` 路径
- `drainNotifications` 的消费方：当前 TUI 没有消费 `notifications` 队列，hook 输出不会进入 system reminder；等通知中心模块就绪时再接入
- 缺失事件触发：`SESSION_END` / `PRE_SEND` / `POST_RECEIVE` / `SHUTDOWN` 当前没有 emit 点，等业务场景出现再在 TUI / Agent loop / 进程信号处理器里补 fireHook
- Hook 配置的热更新：必须重启或重新选 provider 才生效

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

````markdown
# ch12: Hook 系统 Tasks

## T1: 定义事件 / 动作枚举与数据 record

- 影响文件: `src/main/java/com/codebot/hook/HookEngine.java`
- 依赖任务: 无
- 完成标准: 9 个 `EventName` 枚举（带 `value()` 返回 snake_case 字符串）+ 3 个 `ActionType` 枚举（command / script / prompt）；5 个 record（Action / Hook / HookContext / HookResult / PreToolResult）齐全且字段对齐 Go 版语义。
- 实际产出: `HookEngine.java:12-55`

## T2: Condition DSL —— `==` 与 `=~` leaf 操作符

- 影响文件: `src/main/java/com/codebot/hook/HookEngine.java`
- 依赖任务: T1
- 完成标准: `evaluateCondition` 支持 `==` 等值匹配和 `=~` 正则匹配；变量解析覆盖 tool / event / file_path / message / `args.<key>`；`stripQuotes` 自动剥离 `"..."` / `'...'` / `/.../` 三种包裹；未识别操作符返回 true（与 Go 版兼容兜底）；正则编译失败时返回 false。
- 实际产出: `HookEngine.java:121-177`（`evaluateCondition` / `resolveVar` / `stripQuotes`）

## T3: Engine 核心 —— `loadHooks` / `addHook` / `runHooks`

- 影响文件: `src/main/java/com/codebot/hook/HookEngine.java`
- 依赖任务: T1, T2
- 完成标准: `loadHooks(List<Hook>)` 清空旧列表再追加；`addHook(Hook)` 增量追加；`runHooks(HookContext)` 按事件名过滤 + condition 过滤 + 调 `executeAction` + 把结果写入 `notifications` 队列；`drainNotifications()` 取一份快照并清空。
- 实际产出: `HookEngine.java:64-90`（`addHook` / `loadHooks` / `runHooks`）、`HookEngine.java:113-117`（`drainNotifications`）

## T4: Pre-tool 阻断专用入口 `runPreToolHooks`

- 影响文件: `src/main/java/com/codebot/hook/HookEngine.java`
- 依赖任务: T3
- 完成标准: `runPreToolHooks(String toolName, Map<String,Object> args)` 构造 PRE_TOOL_USE ctx → 按事件 / condition 过滤 → 命中且 `h.reject() == true` 时执行 action 并立即返回 `PreToolResult(true, result.output())`；无 reject 命中时返回 `PreToolResult(false, "")`。
- 实际产出: `HookEngine.java:92-109`

## T5: 两种动作执行器（command / prompt）

- 影响文件: `src/main/java/com/codebot/hook/HookEngine.java`
- 依赖任务: T3
- 完成标准:
 - `executeAction` 按 ActionType 分发：COMMAND 走 `executeCommand`；PROMPT 直接把 `action.message()` 当 output 返回 `HookResult(id, message, true, reject)`；其余（含 SCRIPT）落 default 分支返回 `HookResult(id, "Unknown action type: ...", false, false)`
 - `executeCommand`：`ProcessBuilder("bash", "-c", command)` 启子进程；环境变量注入 `CODEBOT_EVENT` 和 `CODEBOT_TOOL`；同步读 stdout / stderr 后再 `waitFor()`；stderr 非空时拼到 stdout（两者均非空用换行连接）；退出码 0 视作 success；`IOException` / `InterruptedException` 捕获后返回 `success=false` 的 HookResult，且 `InterruptedException` 分支必须 `Thread.currentThread().interrupt()` 保留中断状态
- 实际产出: `HookEngine.java:181-214`（`executeAction` / `executeCommand`）

## T6: 配置 POJO `HookConfig` 与 `AppConfig.hooks` 绑定

- 影响文件: `src/main/java/com/codebot/config/HookConfig.java`、`src/main/java/com/codebot/config/AppConfig.java`
- 依赖任务: T1
- 完成标准:
 - 新建 `HookConfig` POJO，字段 id / event / condition / type / command / message / reject，全部 getter / setter
 - `AppConfig` 新增 `private List<HookConfig> hooks` + getter / setter，让 SnakeYAML 能反序列化 `hooks: [...]` 列表
- 实际产出: `HookConfig.java:1-33`、`AppConfig.java:10`、`AppConfig.java:21-22`

## T7: 入口透传 —— `CodeBot.main` 把 hook 列表传给 TUI

- 影响文件: `src/main/java/com/codebot/CodeBot.java`
- 依赖任务: T6
- 完成标准: `CodeBot.main` 加载完 `AppConfig` 后，把 `config.getHooks() != null ? config.getHooks() : List.of()` 作为第三个参数传给 `new CodeBotModel(...)`。
- 实际产出: `CodeBot.java:35-39`

## T8: TUI 装配 —— `CodeBotModel` 构造 Engine + 翻译 HookConfig

- 影响文件: `src/main/java/com/codebot/tui/CodeBotModel.java`
- 依赖任务: T1, T6, T7
- 完成标准:
 - `CodeBotModel` 构造函数新增 `List<HookConfig> hookConfigs` 形参，存到字段
 - 构造期 `new HookEngine()`，若 hookConfigs 非空则把每个 HookConfig 翻译成 `HookEngine.Hook` 后 `loadHooks`
 - `parseEventName(String)` / `parseActionType(String)` 静态方法把 yaml 字符串映射到枚举，未知字符串落 default 分支兜底
- 实际产出: `CodeBotModel.java:66`、`CodeBotModel.java:174-205`（构造）、`CodeBotModel.java:208-232`（两个 parse 方法）

## T9: Agent 接入 —— `Agent.hookEngine` 字段 + `StreamingExecutor` 调用

- 影响文件: `src/main/java/com/codebot/agent/Agent.java`、`src/main/java/com/codebot/agent/StreamingExecutor.java`、`src/main/java/com/codebot/tui/CodeBotModel.java`
- 依赖任务: T3, T4, T5, T8
- 完成标准:
 - `Agent` 新增 `private HookEngine hookEngine` 字段 + `setHookEngine` / `getHookEngine` 访问器
 - `Agent.agentLoop` 在每轮工具调用前构造 `new StreamingExecutor(registry, checker, hookEngine, queue)`
 - `StreamingExecutor.executeSingle` 在 tool.execute 之前调 `hookEngine.runPreToolHooks(call.toolName(), call.args())`，rejected 时立即返回 `"Rejected by hook: <msg>"` 当 ToolResult
 - `StreamingExecutor.executeSingle` 在 tool.execute 之后构造 POST_TOOL_USE ctx 调 `hookEngine.runHooks(ctx)`
 - `CodeBotModel` 在 provider 就绪路径调 `agent.setHookEngine(hookEngine)` 并 `fireHook(SESSION_START, null, null)`
- 实际产出: `Agent.java:29`（字段）、`Agent.java:43`/`Agent.java:48`（访问器）、`Agent.java:249`（构造 executor）、`StreamingExecutor.java:27`/`StreamingExecutor.java:33-39`（字段 + 构造）、`StreamingExecutor.java:82-89`（pre）、`StreamingExecutor.java:142-146`（post）、`CodeBotModel.java:502-503`（setHookEngine + SESSION_START）

## T10: 生命周期事件触发 —— `fireHook` 在 turn_start / turn_end 调用

- 影响文件: `src/main/java/com/codebot/tui/CodeBotModel.java`
- 依赖任务: T9
- 完成标准:
 - 新增 `private void fireHook(EventName event, String toolName, Map<String,Object> args)` 助手方法，hookEngine 为 null 时直接 return；非 null 时构造 ctx 调 `hookEngine.runHooks(ctx)`
 - `TURN_START`：在用户消息提交后、agent 启动前调用（slash command 分支和普通消息分支两处）
 - `TURN_END`：在 `LoopComplete` 事件处理分支调用
- 实际产出: `CodeBotModel.java:949`（slash command 分支）、`CodeBotModel.java:1025`（sendUserMessage 分支）、`CodeBotModel.java:1104`（LoopComplete 分支）、`CodeBotModel.java:1148-1152`（fireHook 实现）

## T11: 端到端验证

- 影响文件: 无
- 依赖任务: T1-T10
- 完成标准: 在项目根目录 `config.yaml` 中配置一条 pre_tool_use reject hook：
 ```yaml
 hooks:
   - id: block-rm
     event: pre_tool_use
     condition: 'tool == Bash'
     type: prompt
     message: "blocked"
     reject: true

 ```

启动 TUI 让 LLM 调用 Bash 工具，看到工具结果是 `Rejected by hook: blocked`，且 ChatMessage 的 toolBlocks 把 isError 标为 true。

- 实际产出: 由人工或集成测试覆盖；手工验证步骤见 checklist §4。

## 进度

- [ ] T1

- [ ] T2

- [ ] T3

- [ ] T4

- [ ] T5

- [ ] T6

- [ ] T7

- [ ] T8

- [ ] T9

- [ ] T10

- [ ] T11

````

```markdown
# ch12: Hook 系统 Checklist

## 1. 实现完整性

- [ ] 9 个 `EventName` 枚举在 `src/main/java/com/codebot/hook/HookEngine.java:12-28`：SESSION_START / SESSION_END / TURN_START / TURN_END / PRE_SEND / POST_RECEIVE / PRE_TOOL_USE / POST_TOOL_USE / SHUTDOWN，每个枚举值 `value()` 返回对应 snake_case 字符串
- [ ] 3 个 `ActionType` 枚举在 `HookEngine.java:32-42`：COMMAND / SCRIPT / PROMPT
- [ ] 5 个 record 在 `HookEngine.java:46-55`：`Action / Hook / HookContext / HookResult / PreToolResult` 字段对齐 spec §3.F7
- [ ] Engine 私有字段 `private final List<Hook> hooks` 与 `private final List<HookResult> notifications` 在 `HookEngine.java:59-60`
- [ ] `addHook(Hook)` 和 `loadHooks(List<Hook>)` 在 `HookEngine.java:64-71`：loadHooks 必须 `hooks.clear()` 后再 `addAll`
- [ ] `runHooks(HookContext)` 在 `HookEngine.java:75-90`：按事件名过滤 → condition 过滤 → 调 `executeAction` → 把 HookResult 追加到 `notifications` 队列
- [ ] `runPreToolHooks(String, Map)` 在 `HookEngine.java:92-109`：构造 PRE_TOOL_USE ctx → 命中 reject 钩子时执行 action 并立即返回 `PreToolResult(true, output)`；无命中返回 `PreToolResult(false, "")`
- [ ] `drainNotifications()` 在 `HookEngine.java:113-117`：返回不可变快照 + 清空内部队列
- [ ] Condition DSL 支持 `==` 与 `=~` 两种 leaf：实现在 `HookEngine.java:121-147`；变量解析在 `HookEngine.java:149-164`；引号剥离在 `HookEngine.java:166-177`
- [ ] 未识别操作符走 `return true` 兜底（`HookEngine.java:146`）；正则编译失败 `PatternSyntaxException` 走 `return false`（`HookEngine.java:140-142`）
- [ ] `executeAction` 在 `HookEngine.java:181-188` 按 ActionType 分发：COMMAND 走 `executeCommand`、PROMPT 走 `new HookResult(id, message, true, reject)`、SCRIPT / 未知走 `"Unknown action type: ..."` 失败结果
- [ ] `executeCommand` 在 `HookEngine.java:190-214`：`ProcessBuilder("bash", "-c", command)` 启子进程；env 注入 `CODEBOT_EVENT` 和 `CODEBOT_TOOL`；stdout / stderr 同步读完后 `waitFor()`；exit code 0 ↔ success；stderr 非空时拼到 stdout（两者均非空用 `\n` 分隔）
- [ ] `executeCommand` 异常分支必须捕获 `IOException | InterruptedException`，且 `InterruptedException` 分支调 `Thread.currentThread().interrupt()` 保留中断状态（`HookEngine.java:208-213`）
- [ ] `HookConfig` POJO 在 `src/main/java/com/codebot/config/HookConfig.java:1-33`：字段 id / event / condition / type / command / message / reject + 配套 getter / setter

## 2. 接入完整性

- [ ] `grep -rn "new HookEngine" --include="*.java" src/main/java` 命中 `CodeBotModel.java:196` 这条非测试构造点
- [ ] `grep -rn "runPreToolHooks\|runHooks(" --include="*.java" src/main/java | grep -v Test` 命中 `StreamingExecutor.java:83` 与 `StreamingExecutor.java:145` 两个 agent loop 触发点，以及 `CodeBotModel.java:1151` 一个生命周期触发点
- [ ] `grep -rn "setHookEngine\|getHookEngine" --include="*.java" src/main/java | grep -v Test` 至少命中 `CodeBotModel.java:502`（setHookEngine）和 `Agent.java:43`/`Agent.java:48`（访问器声明）
- [ ] Config 绑定：`AppConfig.java:10` 含 `private List<HookConfig> hooks` 字段，`AppConfig.java:21-22` 含 getter / setter
- [ ] 入口透传：`CodeBot.java:35-39` 把 `config.getHooks() != null ? config.getHooks() : List.of()` 传给 `CodeBotModel` 第三个参数
- [ ] TUI 装配：`CodeBotModel.java:66`（字段）、`CodeBotModel.java:174-205`（构造函数翻译 HookConfig → HookEngine.Hook 并 loadHooks）
- [ ] `parseEventName / parseActionType` 在 `CodeBotModel.java:208-232`：未知 yaml 字符串落 default 分支兜底到 SESSION_START / COMMAND
- [ ] Agent 字段：`Agent.java:29` 含 `private HookEngine hookEngine`；构造 StreamingExecutor 处 `Agent.java:249` 把 hookEngine 透传
- [ ] StreamingExecutor 字段：`StreamingExecutor.java:27` 含 `private final HookEngine hookEngine`，构造函数 `StreamingExecutor.java:33-39` 接收
- [ ] Pre-tool 调用：`StreamingExecutor.java:82-89` 走 `if (hookEngine != null) { ... }`，rejected 时把 `"Rejected by hook: <msg>"` 当 ToolResult 返回并发出 `AgentEvent.ToolResultEvent`
- [ ] Post-tool 调用：`StreamingExecutor.java:142-146` 在 tool.execute 完成后构造 POST_TOOL_USE ctx 调 `hookEngine.runHooks(ctx)`
- [ ] 生命周期触发：`CodeBotModel.java:1148-1152` 实现 `fireHook` 助手，并在 `CodeBotModel.java:503`（SESSION_START）、`CodeBotModel.java:949` 与 `CodeBotModel.java:1025`（TURN_START）、`CodeBotModel.java:1104`（TURN_END）调用
- [ ] 入口路径：`config.yaml.hooks → AppConfig.hooks → CodeBot.main → new CodeBotModel(..., hooks) → CodeBotModel.hookConfigs → new HookEngine + loadHooks → agent.setHookEngine → Agent.hookEngine → StreamingExecutor.executeSingle 调 runPreToolHooks / runHooks`
- [ ] 死代码记录 1：`HookEngine.ActionType.SCRIPT` 当前在 `executeAction` 落 default 分支（永远返回 "Unknown action type"），spec §6 已明示「不实现」；接入前可保留枚举占位、后续接入时单独删除或补 case 分支
- [ ] 死代码记录 2：`HookEngine.drainNotifications` 当前没有非测试消费方，`grep -rn "drainNotifications" --include="*.java" src/main/java | grep -v Test` 应返回 0 条；spec §6 已记录留作后续通知中心模块接入

## 3. 编译与测试

- [ ] `cd /Users/codemelo/codebot && ./gradlew build` 通过
- [ ] `cd /Users/codemelo/codebot && ./gradlew compileJava` 通过（hook 包被 agent / tui / config 引用）
- [ ] `cd /Users/codemelo/codebot && ./gradlew test` 通过；若新增 `HookEngineTest`，至少覆盖 condition 解析（== 与 =~）、runPreToolHooks 阻断、runCommand 注入环境变量三类用例
- [ ] `javac -Xlint:all` 或 Gradle build 输出中 `com.codebot.hook` 与 `StreamingExecutor` 无未检查警告

## 4. 端到端验证

- [ ] 在项目根目录 `config.yaml` 配置一条 pre_tool_use reject hook（参考 tasks.md T11 的 yaml）；启动 TUI 后让 LLM 调用 Bash 工具，看到工具结果文本是 `Rejected by hook: blocked`，且 `ChatMessage.ToolBlockInfo.isError == true`
- [ ] 在 `config.yaml` 配置一条 post_tool_use command hook，命令使用 `CODEBOT_TOOL` 环境变量（如 `echo "tool=$CODEBOT_TOOL" >> /tmp/codebot-hook.log`）；触发工具调用后查看日志文件包含正确的工具名
- [ ] 测试 condition 正则匹配：配置 `condition: 'tool =~ Bash|Read'`，验证 Bash 和 ReadFile 工具都触发 hook，其他工具不触发
- [ ] 测试 prompt 动作：配置 `type: prompt` + `message: "test message"`，触发后通过 `HookEngine.drainNotifications` 看到 `HookResult.output == "test message"`（需在 TUI 接入消费方或编写直接调 Engine 的单元测试）
- [ ] 测试 condition 引号剥离：`condition: 'tool == "Bash"'`、`condition: "event =~ /session.*/"`、`condition: "tool == 'Bash'"` 三种写法都能正确匹配

## 5. 文档

- [ ] `docs/java/ch12/spec.md` 存在
- [ ] `docs/java/ch12/tasks.md` 存在
- [ ] `docs/java/ch12/checklist.md` 存在
- [ ] commit message 包含章节号 `ch12` 与三件套关闭标记，建议形如 `docs(ch12): close spec/tasks/checklist for hooks system`

```



## ch13

```markdown
# 我的初步想法
- 把子工作者包装成统一的工具入口：一个工具就够了，通过类型参数选择预定义角色，工具列表保持稳定不随角色增减变化
- 角色用 Markdown + YAML frontmatter 定义（如角色名、用途说明、工具白/黑名单、模型选择、最大轮次、权限模式），加载来源有优先级（项目目录 > 用户级 > 内置 > 插件），同名定义按优先级覆盖
- 两种创建模式并存：定义式（空白对话 + 固定角色，可指定独立模型）；以及 Fork 式（不指定角色时启用，继承父对话历史 + 复用父工具集，让首次 LLM 请求命中 prompt cache）
- 隔离与共享的边界要分清：运行时状态隔离（消息历史、权限审批记录、文件读缓存、token 计数），基础设施共享（LLM 客户端、Hook 引擎、文件系统）
- 子工作者用「跑到底」模式执行：任务直接从参数注入不等用户输入，LLM 不再调任何工具即视为完成，把最后一条文本作为结果返回；Hook 在子工作者中仍然生效
- Fork 路径在第一条用户消息里注入一段强硬指令，覆盖父工作者的默认行为（不能再 fork、不要主动对话、不要请求确认、直接用工具干活、最终报告控制字数并按结构化字段输出）
- 工具过滤的多层防线防嵌套失控：全局禁止列表把工具自身排除（防 A→B→C 链式嵌套），自定义角色额外限制，后台运行的子工作者再叠加更严格的白名单
- 后台运行三种进入路径：调用时显式指定、前台超过时间阈值自动切、用户按 ESC 手动切；Fork 模式强制走后台保证并行；前台→后台移交运行中实例不能杀掉重来
- 后台任务管理器维护任务的状态、结果、token 用量、起止时间；完成后通过结构化通知异步注入主对话，不打断当前对话
- 内置几种常用角色覆盖典型场景（如代码探索 / 计划制定 / 通用全能），其中验证角色用配置开关按需启用；配套斜杠命令让用户查看和管理后台任务（列出、查看详情、终止）
```

### Go

```markdown
# ch13: SubAgent Spec

## 1. 背景

主 Agent 做大任务时会塞满上下文：研究、规划、写代码、跑测试都堆在一个对话里，单一窗口很快耗尽。这一章把"开一个上下文隔离的新 Agent 去做一件事"做成主 Agent 可以直接调用的工具，让主 Agent 学会分发工作，避免上下文爆炸，同时通过专门角色（plan / explore）和后台异步执行扩展并发能力。

## 2. 目标

提供 `Agent` 工具，主 Agent 在对话里写一次工具调用即可：1) 按 `subagent_type` 启动一个定义式专家子 Agent（系统提示词、模型、工具白名单都按 Markdown 定义文件来），2) 不带 `subagent_type` 时直接 fork 当前对话上下文跑一个临时子 Agent，3) 带 `team_name` 时把这个 spawn 注册成长期团队成员（衔接 ch15）。后台任务的完成通过 `<task-notification>` 反馈给主 Agent。

## 3. 功能需求

- F1: `AgentTool` 实现 `tools.Tool` 接口，注册到主 Agent 的 registry，被 LLM 当成普通工具调用。
- F2: 三档内建 Agent 类型 `general-purpose` / `plan` / `explore`，每档可定制工具黑名单、最大轮数、模型、系统提示词覆盖。
- F3: 支持从用户级目录和项目级 `.codebot/agents/*.md` 加载自定义 Agent 定义，项目级覆盖用户级覆盖 builtin；Markdown frontmatter 解析为 `AgentDefinition`。
- F4: 三种执行路径：sync（前台阻塞、流式回写 LLM）/ async（后台任务、立即返回任务 ID）/ fork（fork 父对话上下文，强制后台）。
- F5: `TaskManager` 跟踪后台子 Agent 生命周期（pending/running/completed/failed/cancelled），完成时把通知写进队列，主 Agent 下一轮通过 drain 拿到 `<task-notification>` 系统提示。
- F6: 四层工具过滤：全局禁（`Agent` / `AskUserQuestion` 防递归）、custom agent 额外禁、async 白名单（仅常用读写 / 搜索 / Bash / ToolSearch）、definition 级黑名单；MCP 工具一律放行。
- F7: Fork 路径：构造完整 forked conversation（拷贝父消息 + 给悬挂的 `tool_use` 补 placeholder `tool_result`），追加 fork boilerplate 系统约束 + 任务文本；fork-of-fork 通过扫描父对话标签拒绝。
- F8: 可选 worktree 隔离与 `WorktreeMgr` 配合，子 Agent 在临时 git worktree 中跑；执行结束按是否检测到变更决定保留 / 清理。
- F9: 可选团队模式与 `TeamMgr` 配合，走 teammate spawn 路径注册长期团队成员（详见 ch15）。
- F10: 父 Agent 取消（ESC）时可把当前正在跑的对话挂到后台任务上继续执行，主流程不阻塞。
- F11: `Agent` 工具入口额外支持 `mode`（运行时权限模式覆盖）与 `cwd`（工作目录覆盖）参数；`cwd` 与 `isolation: worktree` 互斥；`mode` 走权限模式白名单校验。
- F12: 子 Agent 定义可声明 `background: true` 强制后台运行，与调用侧 `run_in_background` 等价但写在 Markdown 定义里。
- F13: 提供可选的 verification 内置角色（找最后 20% bug），由环境变量守开关；默认不出现在 Agent 列表里。
- F14: Fork 子 Agent 的工具池完全继承父池，让 API 请求前缀字节级一致以命中 prompt cache；嵌套 fork 通过双保险检测（query-source 标记 + 父对话消息扫描）阻止。
- F15: Fork 复制父对话时保留 thinking blocks，保证 assistant 消息形状与父侧字节级一致。
- F16: 子 Agent 接受 spec 级 `permissionMode` 覆盖，运行时用独立的权限 Checker（与父共享 sandbox / rule engine，仅 Mode 替换）。
- F17: 子 Agent spec 支持 `initialPrompt`，在第一轮用户消息之前注入，作为子 Agent 的启动指引。

## 4. 非功能需求

- N1: 子 Agent 不能再调 `Agent` 工具（防止无限递归 / 上下文爆炸），任意层级的子 Agent 都通过全局黑名单屏蔽。
- N2: 后台 Agent 通过取消上下文受控；取消调用后状态置为 cancelled。
- N3: `TaskManager` 所有公共方法并发安全（fork goroutine 与主线程 Drain 同时操作 map）。
- N4: fork 操作必须先在父对话里搜 boilerplate 标签拒绝嵌套 fork。
- N5: Sync 路径要走子 Agent 的完整事件流（文本 / 工具结果 / 错误），不丢消息；工具结果事件单独转发 progress 给 UI。
- N6: Fork 子 Agent 必须复用父池工具与对话内容（含 thinking blocks），让请求前缀字节级一致；任何过滤都会破坏 prompt cache 命中。
- N7: 子 Agent 的权限 Checker 必须独立实例，不能直接共享父引用——`permissionMode` 覆盖时不允许污染父的权限状态。
- N8: 子 Agent 定义 frontmatter 接受的字段集合需在解析层完整保留；未来章节（hooks / mcpServers / skills / memory 等）的字段必须在解析层先存得下，避免重复迁移。

## 5. 设计概要

- 核心数据结构:
 - `AgentTool`：承载 Client / ModelResolver / Registry / Protocol / TaskMgr / ProgressCh / Loader / Conversation / WorktreeMgr / TeamMgr / ParentChecker / QuerySource 等运行时依赖。
 - `AgentDefinition`：Markdown frontmatter 解出来的 spec，含核心字段（agent type / description / tools / disallowedTools / model / maxTurns）+ 扩展字段（permissionMode / background / isolation / memory / effort / initialPrompt / omitMewcodeMd / skills / mcpServers / requiredMcpServers / hooks）。
 - `SubAgentSpec`：运行时归一化的子 Agent 描述，由 `AgentDefinition` 转换得到，扩展字段透传供后续章节消费。
 - `TaskManager` / `Task` / `TaskNotification`：后台任务的状态机 + 通知队列。
 - `BuiltinSpecs`：三档内建定义 `general-purpose / plan / explore`，`plan` 带 plan 专用系统提示词；可选第四档 `verification`（env var 守）。
 - 工具过滤层：四张 map 控制六层过滤（MCP 豁免 → 全局禁用 → 自定义额外禁用 → 异步白名单 + in-process teammate 特例 → 定义级黑名单 → 定义级白名单）。
- 主流程:
 - 同步：用户消息 → 主 Agent → LLM 输出 `Agent` 工具调用 → `AgentTool.Execute` → 解析 `subagent_type` → 工具过滤 → 创建子 Agent → 执行 → 事件流回写 UI / progress channel → 返回结果。
 - 异步：同上但创建后台 task，立即返回任务 ID，后台 goroutine 完成时写通知，主 Agent 下一轮抽 `<task-notification>` 注入。
 - Fork：双保险检测拒绝嵌套（QuerySource 标记 + 父对话消息扫）→ 拷贝父对话（含 thinking blocks，保 byte-exact）→ 给悬挂 `tool_use` 补 placeholder → 追加 fork boilerplate → 工具池整体克隆父池（Agent 工具实例改写 QuerySource） → 始终后台 → 完成走通知。
 - 团队成员：校验 team 存在、name 不重 → 解析 spec → 可选 worktree → 通过 teams 模块 spawn → 立即返回（不阻塞 Lead）。
- 调用链（模块层级）:
 - TUI 装配 → 在 agent tool 注册环节把 `AgentTool` 注册进 registry；主 Agent Checker 构造完后回填 `AgentTool.ParentChecker`，让子 Agent 能派生独立 Checker
 - Agent loop → `NotificationFn` 抽取 → TUI 绑定 drain → `TaskManager.DrainNotifications`
 - TUI ESC → `TaskManager.AdoptRunning` 把当前对话挂为后台任务
 - 子 Agent spawn 时：spec.PermissionMode 走 `deriveSubAgentChecker` 派生（与父共享 sandbox / rule engine），spec.InitialPrompt 走第一轮 user message 之前注入
- 与其他模块的交互:
 - 依赖 `internal/agent`（创建子 Agent）、`internal/conversation`（forked 对话）、`internal/tools`（注册中心 + 过滤）、`internal/llm`（model resolver）、`internal/worktree`（隔离）、`internal/teams`（团队成员）
 - 被 `internal/tui` 和 `cmd/codebot` 调用

## 6. Out of Scope

- 子 Agent 输出全在内存事件流里，不落盘 task 输出文件
- 不实现 RemoteAgent / DreamTask / LocalWorkflow / MonitorMcp 这些 TaskType
- 不实现 fork 路径的 worktree notice（仅主线 isolation 支持）
- 不接入 plugin / flag / managed 加载源（只支持 built-in / user / project）
- 不消费 `skills` / `hooks` / `mcpServers` / `memory` / `omitMewcodeMd` 等字段——仅在解析层保留，运行时落地留给 ch11 / ch12 / ch07 / ch09 各自接入
- 不实现 PermissionMode 的 bubble / auto 模式
- 不实现 120s 自动超时切后台 / ESC 切后台 / 持久化后台恢复
- 不实现 `isolation: remote` 远端 CCR 运行后端
- 不内置 Statusline-Setup / Code-Guide 等非核心 Agent

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch13: SubAgent Tasks

> 任务粒度：每个任务可在一次会话内完成，可独立交付。

## T1: 定义 `SubAgentSpec` 与三档 builtin
- 影响文件: `internal/agents/subagent.go`（`SubAgentSpec` @ 180-187；`planAgentSystemPrompt` @ 189-222；`BuiltinSpecs` @ 224-244）
- 依赖任务: 无
- 完成标准: `BuiltinSpecs["general-purpose" | "plan" | "explore"]` 三项齐全；`plan` 设置 `DisallowedTools=["EditFile","WriteFile"]`、`MaxTurns=15`；`explore` 设置 `MaxTurns=30`、`Model="haiku"`。
- [ ] 完成

## T2: 实现 `AgentDefinition` 与 Markdown 解析
- 影响文件: `internal/agents/definition.go`（`AgentDefinition` @ 11-20；`ParseAgentFile` @ 22-57；`ToSpec` @ 59-68）
- 依赖任务: T1
- 完成标准: `ParseAgentFile` 能解析 `---\nname:...\ndescription:...\n---\nBody` 形式；缺 `name` / `description` 报错；非法 `model` 报错（限 haiku/sonnet/opus/inherit/空）。
- [ ] 完成

## T3: 实现 `AgentLoader`，按 builtin → user → project 顺序加载
- 影响文件: `internal/agents/loader.go`（`AgentLoader` @ 10-13；`LoadAll` @ 22-45；`loadDir` @ 47-64；`Get` @ 66-68；`ListNames` @ 70-77）
- 依赖任务: T2
- 完成标准: `LoadAll` 先注入 `BuiltinSpecs`，再 `~/.codebot/agents/*.md`（source=user），最后 `<wd>/.codebot/agents/*.md`（source=project）；同名后注册覆盖前者。
- [ ] 完成

## T4: 实现四层工具过滤 `FilterToolsForAgentEx`
- 影响文件: `internal/agents/tool_filter.go`（`AllAgentDisallowedTools` @ 9；`CustomAgentDisallowedTools` @ 14；`AsyncAgentAllowedTools` @ 20；`FilterToolsForAgent` @ 34；`FilterToolsForAgentEx` @ 38-76；`IsMCPTool` @ 30-32）
- 依赖任务: 无
- 完成标准: `Agent` / `AskUserQuestion` 一律去除；`isAsync=true` 时仅保留白名单；MCP 工具（`mcp__` 前缀）一律放行；definition 级 `DisallowedTools` 生效。
- [ ] 完成（测试覆盖 `tool_filter_test.go` 全部分支）

## T5: 实现 `TaskManager` 状态机 + 通知队列
- 影响文件: `internal/agents/subagent.go`（`TaskStatus` @ 16-24；`Task` @ 26-35；`TaskManager` @ 37-49；`CreateTask/SetRunning/SetCompleted/SetFailed/DrainNotifications/AdoptRunning/FindByName/CancelTask` @ 57-178）
- 依赖任务: 无
- 完成标准: 状态机覆盖 pending/running/completed/failed/cancelled；完成 / 失败时把 `TaskNotification` 入队；`DrainNotifications` 一次性取出并清空；`AdoptRunning` 把已经在跑的 channel 挂为后台任务。
- [ ] 完成

## T6: 实现 `SpawnSubAgent`（后台异步路径）
- 影响文件: `internal/agents/subagent.go`（`SpawnSubAgent` @ 246-293）
- 依赖任务: T1, T4, T5
- 完成标准: 函数返回 `task_N` 字符串；内部 `FilterToolsForAgent(reg, spec.DisallowedTools, isAsync=true)`；用独立 `context.WithCancel`；事件循环里 ErrorEvent → `SetFailed`，正常退出 → `SetCompleted`。
- [ ] 完成

## T7: 实现 `AgentTool.Execute` 五条分支
- 影响文件: `internal/agents/agent_tool.go`（`AgentTool` @ 48-59；`Schema` @ 87-138；`Execute` @ 156-211）
- 依赖任务: T1, T3, T4, T5, T6
- 完成标准: Execute 按 `team_name → subagent_type=="" → runInBackground → 默认同步` 顺序分发；schema 通过 `Loader.ListNames` 动态枚举 `subagent_type`；缺 `description` / `prompt` 报错。
- [ ] 完成

## T8: 实现 `runSync`（前台流式 + 可选 worktree）
- 影响文件: `internal/agents/agent_tool.go`（`runSync` @ 213-315；`selectClient` @ 140-154；`sanitizeSlugSegment` @ 18-32）
- 依赖任务: T7
- 完成标准: 子 Agent `MaxIterations` 走 spec 或 fallback=200；事件流转发 StreamText / ToolResultEvent / ErrorEvent；isolation=worktree 时创建临时分支，结束按 `worktree.DetectChanges` 决定保留 / 移除。
- [ ] 完成

## T9: 实现 `runFork`（fork 父对话）
- 影响文件: `internal/agents/agent_tool.go`（`runFork` @ 317-371；`forkBoilerplate` @ 373-381；`buildForkedConversation` @ 383-414；`ForkBoilerplateTag` @ 46）
- 依赖任务: T4, T5, T7
- 完成标准: 检测父对话里 `<fork_boilerplate>` 标签拒绝嵌套；`buildForkedConversation` 拷贝父消息，给悬挂 `tool_use` 补 `(tool execution interrupted by fork)` 占位 `tool_result`，结尾追加 `forkBoilerplate + "Your task:" + prompt`；fork 始终后台。
- [ ] 完成

## T10: 实现 `runAsync`（builtin spec → 后台）
- 影响文件: `internal/agents/agent_tool.go`（`runAsync` @ 416-426）
- 依赖任务: T6, T7
- 完成标准: 直接调 `SpawnSubAgent`，返回 `Agent "..." launched in background (task task_N).` 文案。
- [ ] 完成

## T11: 实现 `runAsTeammate`（团队成员路径，衔接 ch15）
- 影响文件: `internal/agents/agent_tool.go`（`runAsTeammate` @ 438-533；`drainTeammateEvents` @ 538-561）
- 依赖任务: T7（ch15 的 `teams.SpawnTeammate`）
- 完成标准: 校验 team 存在；同 team 内重名报错；isolation=worktree 时建 `team-<team>-<member>-<ts>` 分支；调 `teams.SpawnTeammate` 拿回 backend hint；in-process 模式启动 goroutine `drainTeammateEvents` 防止生产者阻塞。
- [ ] 完成

## T12: 接入主流程
- 影响文件: `internal/tui/tui.go`（`subAgentProgressCh` @ 166；`taskMgr` @ 179；`registerAgentTools` @ 519-556；`drainTaskNotifications` @ 486-499；`AdoptRunning` 调用 @ 777）
- 依赖任务: T1-T11
- 完成标准:
 1. `m.registry.Register(&agents.AgentTool{...})` 在 `registerAgentTools` 注册；
 2. `ag.NotificationFn = m.drainTaskNotifications` 在 init 时挂上（`tui.go:369`）；
 3. ESC 中断时调 `taskMgr.AdoptRunning` 把当前 stream 转后台（`tui.go:777`）；
 4. progress channel 由 TUI 的 `listenForSubAgentProgress` 消费。
- [ ] 完成

## T13: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T12
- 完成标准:
 - `go build ./...` 通过（已验证，输出为空）；
 - `go test ./internal/agents/...` 全部测试通过（loader_test.go 5 个 + tool_filter_test.go 6 个测试）；
 - 端到端路径已通过现有测试覆盖：Markdown 解析、builtin / project 覆盖、四层过滤的全部分支。
- [ ] 完成

---

> **二批：重构 SubAgent 模块。

## T14: 工具过滤常量重构 + In-process Teammate 特例
- 影响文件: `internal/agents/tool_filter.go`（六层过滤、四张常量集合、`FilterToolsForAgentEx` 多 `isInProcessTeammate` 参数），`internal/agents/tool_filter_test.go` 补三个回归用例。
- 依赖任务: T4
- 完成标准:
 - `AllAgentDisallowedTools` 含 `TaskOutput / ExitPlanMode / EnterPlanMode / Agent / AskUserQuestion / TaskStop / Workflow` 七项；
 - `AsyncAgentAllowedTools` 含原 7 项 + `WebSearch / WebFetch / TodoWrite / NotebookEdit / Skill / SyntheticOutput / EnterWorktree / ExitWorktree`；
 - 新增 `InProcessTeammateAllowedTools`（含 `TaskCreate / TaskGet / TaskList / TaskUpdate / SendMessage`，可选 Cron 三件）；
 - 异步白名单层在 `isInProcessTeammate=true` 时额外放行 `Agent` + 队友工具。
- [ ] 完成

## T15: AgentDefinition 17 字段扩展 + SubAgentSpec 透传
- 影响文件: `internal/agents/definition.go`（新增 `permissionMode / effort / skills / mcpServers / requiredMcpServers / hooks / memory / background / isolation / initialPrompt / omitMewcodeMd` 字段 + 三个枚举校验 + `HasRequiredMcpServers`），`internal/agents/subagent.go`（`SubAgentSpec` 同步新增字段并由 `ToSpec` 透传），`internal/agents/loader_test.go` 补字段解析测试。
- 依赖任务: T2、T3
- 完成标准:
 - frontmatter 解析含 17 个字段，必填只有 `name / description`；
 - `permissionMode` 接受 `default / acceptEdits / plan / bypassPermissions`，非法值报错；
 - `memory` 接受 `user / project / local` 三档；
 - `isolation` 接受 `worktree / remote`；
 - `HasRequiredMcpServers` case-insensitive substring 匹配，缺一返 false。
- [ ] 完成

## T16: AgentTool 入口 mode/cwd 参数 + spec.Background 强制后台
- 影响文件: `internal/agents/agent_tool.go`（`Schema` 加 `mode / cwd` 字段、`Execute` 解析两个参数并做互斥校验、定义级 `Background == true` 走 `runAsync`），`internal/agents/agent_tool_test.go` 加校验测试。
- 依赖任务: T7
- 完成标准:
 - schema 新增 `mode`（5 值枚举）/ `cwd`（绝对路径）；
 - `mode` 调用级覆盖 `spec.PermissionMode`；
 - `cwd` 覆盖子 Agent `WorkDir`，且与 `isolation: worktree` 提前互斥校验；
 - `spec.Background` 或调用级 `run_in_background` 任一为 true 即走 `runAsync`。
- [ ] 完成

## T17: 新增 verification 内置 Agent + env var 守
- 影响文件: `internal/agents/verification_prompt.go`（4500+ 字 system prompt + Spec），`internal/agents/loader.go`（`getBuiltinSpecs` 按 env var 决定是否包含）。
- 依赖任务: T1、T3
- 完成标准:
 - 设 `CODEBOT_VERIFICATION_AGENT=true` 时 `verification` 出现在 `loader.ListNames()` 里；
 - 不设时不出现；
 - 该 spec `Background=true`，disallowedTools 含 `Agent / ExitPlanMode / EditFile / WriteFile / NotebookEdit`；
 - system prompt 含 "VERIFICATION-ONLY" 与 "VERDICT: PASS / FAIL / PARTIAL" 文本。
- [ ] 完成

## T18: Fork 模式三项重构（useExactTools / 双保险 / byte-exact thinking blocks）
- 影响文件: `internal/agents/agent_tool.go`（`AgentTool.QuerySource` 字段、`runFork` 入口两层检查、`cloneRegistryForFork`、`buildForkedConversation` 改用 `AddAssistantFull` 保留 thinking blocks），`internal/agents/agent_tool_test.go` 三个 fork 测试。
- 依赖任务: T9
- 完成标准:
 - `ForkQuerySource = "agent:builtin:fork"`；当 `t.QuerySource == ForkQuerySource` 时 `runFork` 直接拒绝；
 - `cloneRegistryForFork` 复制父池所有工具，仅替换 `*AgentTool` 实例并设 `QuerySource = ForkQuerySource`；
 - `buildForkedConversation` 对所有 assistant 消息调 `AddAssistantFull(text, thinkingBlocks, toolUses)`，保留父侧 thinking blocks；
 - 嵌套 Fork 两条路径都拒（QuerySource 命中 / 消息扫到 `ForkBoilerplateTag`）。
- [ ] 完成

## T19: 子 Agent 权限注入 + initialPrompt 第一轮注入
- 影响文件: `internal/agents/agent_tool.go`（`ParentChecker` 字段、`deriveSubAgentChecker`、`runSync` / `runFork` / `runAsync` 三条路径注入 Checker，runSync 注入 `spec.InitialPrompt`），`internal/agents/subagent.go`（`SpawnSubAgent` 加 `parentChecker` 参数 + 注入 InitialPrompt），`internal/tui/tui.go`（两处主 Agent Checker 构造之后回填到 `AgentTool.ParentChecker`），`internal/agents/agent_tool_test.go` 补 `deriveSubAgentChecker` 测试。
- 依赖任务: T8、T10、T12
- 完成标准:
 - `deriveSubAgentChecker(nil, *)` 返回 nil；
 - `deriveSubAgentChecker(parent, "")` 返回父引用本身；
 - `deriveSubAgentChecker(parent, "plan")` 返回新实例，与父共享 Sandbox / RuleEngine，Mode 为 `ModePlan`；
 - sync/fork/async 三条路径都设了 `subAgent.Checker`；
 - `spec.InitialPrompt != ""` 时子 Agent 的 conversation 在用户 prompt 之前先 `AddUserMessage(initialPrompt)`；
 - TUI 在 `m.ag.Checker` 构造完之后把 ParentChecker 回填到 registry 里的 `AgentTool` 实例。
- [ ] 完成

## T20: 重构端到端验证
- 影响文件: 无（仅运行）
- 依赖任务: T14-T19
- 完成标准:
 - `go build ./...` 通过；
 - `go test ./...` 全 17 个包通过，含 9 个新回归用例：`TestGlobalDisallowedExpanded` / `TestAsyncWhitelistExpanded` / `TestInProcessTeammateExtraTools` / `TestParseAgentDefinitionExtendedFields` / `TestParseAgentInvalidPermissionMode` / `TestHasRequiredMcpServers` / `TestRunForkRejectedWhenQuerySourceIsFork` / `TestRunForkRejectedWhenBoilerplateInHistory` / `TestCloneRegistryForForkSetsQuerySource` / `TestExecuteValidatesModeAndCwdExclusivity` / `TestBuildForkedConversationPreservesThinkingBlocks` / `TestDeriveSubAgentCheckerOverrideMode`。
- [ ] 完成

## 进度
- [ ] T1 / [ ] T2 / [ ] T3 / [ ] T4 / [ ] T5 / [ ] T6 / [ ] T7 / [ ] T8 / [ ] T9 / [ ] T10 / [ ] T11 / [ ] T12 / [ ] T13 / [ ] T14 / [ ] T15 / [ ] T16 / [ ] T17 / [ ] T18 / [ ] T19 / [ ] T20

```

```markdown
# ch13: SubAgent Checklist

> 所有条目可勾选、可观测。验收方式写在条目后面括号中。验收：已通过验证的项均勾选。

## 1. 实现完整性

- [ ] 类型 `AgentTool` 在 `internal/agents/agent_tool.go:48-59` 存在，字段含 `Client / Registry / Loader / TaskMgr / Conversation / WorktreeMgr / TeamMgr`
- [ ] 类型 `AgentDefinition` 在 `internal/agents/definition.go:11-20` 存在，五个 yaml 字段（`name / description / disallowedTools / model / maxTurns`）齐全
- [ ] 类型 `SubAgentSpec` 在 `internal/agents/subagent.go:180-187` 存在
- [ ] 类型 `TaskManager` / `Task` / `TaskNotification` 在 `internal/agents/subagent.go:37/26/44` 存在，含状态机字段
- [ ] `BuiltinSpecs` 在 `internal/agents/subagent.go:224-244` 注册三档（`general-purpose / plan / explore`）
- [ ] `FilterToolsForAgentEx` 在 `internal/agents/tool_filter.go:38-76` 实现四层过滤
- [ ] `ParseAgentFile` 在 `internal/agents/definition.go:22-57` 验证 `name` / `description` 必填，`model` 取值白名单
- [ ] `runFork` 在 `internal/agents/agent_tool.go:317-371` 嵌套 fork 检查（扫描 `<fork_boilerplate>` 标签）
- [ ] `buildForkedConversation` 在 `internal/agents/agent_tool.go:383-414` 给悬挂 `tool_use` 补占位 `tool_result`
- [ ] 错误消息 `"Error: cannot fork from a forked agent"` 在 `agent_tool.go:326` 与 原始定义 的 `isInForkChild` 语义一致

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `grep -r "AgentTool" --include="*.go" /Users/codemelo/codebot` 在 `internal/tui/tui.go:544` 找到注册调用方
- [ ] `m.registry.Register(&agents.AgentTool{...})` 调用点在主流程 `registerAgentTools` (`internal/tui/tui.go:519-556`)，所有依赖（Client/ModelResolver/Registry/Protocol/TaskMgr/ProgressCh/Loader/Conversation/TeamMgr/WorktreeMgr）齐全注入
- [ ] `AgentLoader.LoadAll` 调用点在 `internal/tui/tui.go:527-528`
- [ ] `TaskManager.DrainNotifications` 调用点在 `internal/tui/tui.go:489`（通过 `m.drainTaskNotifications`）
- [ ] `TaskManager.AdoptRunning` 调用点在 `internal/tui/tui.go:777`（ESC 触发的后台挂载）
- [ ] `NotificationFn` 绑定点在 `internal/tui/tui.go:369` 和 `:731`（initSingleProviderMsg + 恢复会话）
- [ ] `ProgressCh` 由 `internal/tui/tui.go:211` 创建并通过 `subAgentProgressMsg` 在事件循环 `:275-298` 消费
- [ ] Schema 暴露：`Agent` 工具通过 `AgentTool.Schema` 注册到 registry，TUI 的 `tools.ToolSearchTool` 可发现它

## 3. 编译与测试

- [ ] `go build ./...` 通过（已运行，无输出）
- [ ] `go test ./internal/agents/...` 通过（`loader_test.go` 5 个 case + `tool_filter_test.go` 6 个 case 全部 PASS）
- [ ] `go vet ./...` 无警告

## 4. 端到端验证

- [ ] 注册路径：在 TUI 启动后 `registerAgentTools` 把 `Agent` 工具放入 registry（`tui.go:544`）；用户向主 Agent 发送 "spawn a plan agent to review X" → LLM 返回 `Agent` 工具调用 → `Execute` → `runSync(spec=plan)` → 子 Agent 流式输出。
- [ ] Fork 路径：用户在对话进行中说 "fork to investigate Y" → LLM 调用 `Agent` 不带 `subagent_type` → `runFork` → forked conversation 启动后台 task → 完成时 `<task-notification>` 通过 `drainTaskNotifications` 注入下一轮（`tui.go:486-499`）
- [ ] ESC 挂后台路径：用户按 ESC 中断 → `tui.go:776-777` 调 `taskMgr.AdoptRunning` → 当前 stream 转入后台 task，UI 显示 "Agent moved to background (task task_N)"
- [ ] 证据：单元测试 + grep 调用方 + 主流程文件行号已列出。源代码 commit a84e3ba / 3676328 / 24e0323 已包含全部实现。

## 5. 文档

- [ ] `specs/go/ch13/spec.md` 已写
- [ ] `specs/go/ch13/tasks.md` 已写，20 个 T 全部勾完（T1-T13 初版骨架 + T14-T20 重构）
- [ ] `specs/go/ch13/checklist.md` 已写并逐项验收
- [ ] commit 信息标注 `ch13` 与三件套关闭状态（待用户确认后由人或 CI 触发）

---

## 6. 工具改造（T14-T20）

### 7.1 工具过滤常量重构（T14）

- [ ] `AllAgentDisallowedTools` 在 `internal/agents/tool_filter.go` 含七项：`TaskOutput / ExitPlanMode / EnterPlanMode / Agent / AskUserQuestion / TaskStop / Workflow`
- [ ] `AsyncAgentAllowedTools` 含 16 项：`ReadFile / WebSearch / TodoWrite / Grep / WebFetch / Glob / Bash / EditFile / WriteFile / NotebookEdit / Skill / LoadSkill / SyntheticOutput / ToolSearch / EnterWorktree / ExitWorktree`
- [ ] 新增 `InProcessTeammateAllowedTools` 含 `TaskCreate / TaskGet / TaskList / TaskUpdate / SendMessage / CronCreate / CronDelete / CronList`
- [ ] `FilterToolsForAgentEx` 签名新增 `isInProcessTeammate bool` 参数（共 6 个参数），队友模式在异步白名单层额外允许 `Agent` + 队友工具
- [ ] 测试：`TestGlobalDisallowedExpanded` / `TestAsyncWhitelistExpanded` / `TestInProcessTeammateExtraTools` 全部 PASS

### 7.2 AgentDefinition 17 字段扩展（T15）

- [ ] `AgentDefinition` 含 17 个字段：核心 6 项 + 扩展 `permissionMode / effort / skills / mcpServers / requiredMcpServers / hooks / memory / background / isolation / initialPrompt / omitMewcodeMd`
- [ ] `ParseAgentFile` 校验 `permissionMode` 取值 ∈ `{"" / default / acceptEdits / plan / bypassPermissions}`，其他值报错
- [ ] `ParseAgentFile` 校验 `memory` 取值 ∈ `{"" / user / project / local}`
- [ ] `ParseAgentFile` 校验 `isolation` 取值 ∈ `{"" / worktree / remote}`
- [ ] `HasRequiredMcpServers` case-insensitive substring 匹配；缺任一返 false；无要求返 true
- [ ] `SubAgentSpec` 新增字段：`PermissionMode / Background / Isolation / InitialPrompt / OmitMewcodeMd / Skills / Memory / McpServers / RequiredMcpServers / Hooks / Effort`
- [ ] `ToSpec()` 透传所有新增字段
- [ ] 测试：`TestParseAgentDefinitionExtendedFields` / `TestParseAgentInvalidPermissionMode` / `TestHasRequiredMcpServers` PASS

### 7.3 AgentTool 入口 mode/cwd + Background 路由（T16）

- [ ] `Schema` 含 `mode` 字段，枚举 `default / acceptEdits / plan / bypassPermissions`
- [ ] `Schema` 含 `cwd` 字段（绝对路径，覆盖工作目录）
- [ ] `Execute` 校验 `cwd != "" && isolation == "worktree"` 返回 `mutually exclusive` 错误
- [ ] `Execute` 校验 `mode` 非法值返回 `invalid mode` 错误
- [ ] 调用级 `mode` 覆盖 `spec.PermissionMode`（per-call 优先级最高）
- [ ] `runSync` 在 isolation 不为 worktree 时若 `cwd != ""` 把子 Agent `WorkDir = cwd`
- [ ] `runInBackground == true || spec.Background == true` 任一为真即走 `runAsync`
- [ ] 测试：`TestExecuteValidatesModeAndCwdExclusivity` PASS

### 7.4 Verification 内置 Agent + env var 守（T17）

- [ ] `verification_prompt.go` 含完整 system prompt（覆盖 "VERIFICATION-ONLY" / "VERDICT: PASS / FAIL / PARTIAL" / "Bad (rejected):" / "Good:" 等关键段落）
- [ ] `verificationSpec` 设 `Background=true`，`DisallowedTools` 含 `Agent / ExitPlanMode / EditFile / WriteFile / NotebookEdit`
- [ ] `verificationSpec.Model == "inherit"`
- [ ] `getBuiltinSpecs()` 按 `os.Getenv("CODEBOT_VERIFICATION_AGENT") == "true"` 决定是否注入 `verification`
- [ ] env var 未设时 `loader.Get("verification") == nil`
- [ ] env var 设为 `true` 时 `loader.ListNames()` 含 `verification`
- [ ] `VerificationAgentType == "verification"`（保持一致 `VERIFICATION_AGENT_TYPE` 常量）

### 7.5 Fork 模式三项重构（T18）

- [ ] `AgentTool.QuerySource` 字段存在；`ForkQuerySource = "agent:builtin:fork"`
- [ ] `ForkAgentType = "fork"`，与 的 `FORK_AGENT.agentType` 一致
- [ ] `runFork` 第一道：`t.QuerySource == ForkQuerySource` 时返回 `cannot fork from a forked agent`
- [ ] `runFork` 第二道：父对话扫到 `<fork_boilerplate>` 也返回同一错误
- [ ] `cloneRegistryForFork` 复制父池全部工具，仅对 `*AgentTool` 实例做 shallow copy 并把 QuerySource 改写为 `ForkQuerySource`
- [ ] `buildForkedConversation` 对带 `tool_use` 的 assistant 消息走 `AddAssistantFull(content, thinkingBlocks, toolUses)`，保留 thinking blocks
- [ ] `buildForkedConversation` 对纯 assistant 消息（无 tool_use）：有 thinking blocks 走 `AddAssistantFull`，无则走 `AddAssistantMessage`
- [ ] 测试：`TestRunForkRejectedWhenQuerySourceIsFork` / `TestRunForkRejectedWhenBoilerplateInHistory` / `TestCloneRegistryForForkSetsQuerySource` / `TestBuildForkedConversationPreservesThinkingBlocks` 全部 PASS

### 7.6 子 Agent 权限注入 + initialPrompt（T19）

- [ ] `AgentTool.ParentChecker *permissions.Checker` 字段存在
- [ ] `deriveSubAgentChecker(nil, anything)` 返回 nil
- [ ] `deriveSubAgentChecker(parent, "")` 返回父引用本身（无新分配）
- [ ] `deriveSubAgentChecker(parent, "plan")` 返回新 Checker：Sandbox / RuleEngine 与父共享，Mode == `permissions.ModePlan`
- [ ] `runSync` 在 `agent.New` 之后调 `deriveSubAgentChecker(t.ParentChecker, spec.PermissionMode)` 注入
- [ ] `runFork` 在 `agent.New` 之后把 `t.ParentChecker` 直接赋给子 Agent（Fork 继承父权限）
- [ ] `SpawnSubAgent` 签名新增 `parentChecker *permissions.Checker` 参数；内部走 `deriveSubAgentChecker`
- [ ] `runSync` / `SpawnSubAgent` 在 `conv.AddUserMessage(taskPrompt)` 之前，当 `spec.InitialPrompt != ""` 时先 `conv.AddUserMessage(spec.InitialPrompt)`
- [ ] TUI 在两处主 Agent Checker 构造之后回填：`if at, ok := m.registry.Get("Agent").(*agents.AgentTool); ok { at.ParentChecker = ag.Checker }`
- [ ] 测试：`TestDeriveSubAgentCheckerOverrideMode` PASS；`ToSpec.InitialPrompt` 透传断言 PASS

### 7.7 重构端到端验证（T20）

- [ ] `go build ./...` 无输出（成功）
- [ ] `go test ./...` 17 个包全部 PASS
- [ ] `go test ./internal/agents/...` 含 9 个新回归用例全 PASS
- [ ] 无新增 `go vet` 警告
- [ ] grep 验证主流程接线：`grep -n "ParentChecker = ag.Checker" internal/tui/tui.go` 应有两处命中（首次启动 + 恢复会话）

```

### Python

```markdown
# ch13: SubAgent Spec

## 1. 背景

主 Agent 做大任务时会塞满上下文：研究、规划、写代码、跑测试都堆在一个对话里，单一窗口很快耗尽。这一章把"开一个上下文隔离的新 Agent 去做一件事"做成主 Agent 可以直接调用的工具，让主 Agent 学会分发工作，避免上下文污染，同时通过专门角色（Plan / Explore）和后台异步执行扩展并发能力。

## 2. 目标

提供 `Agent` 工具，主 Agent 在对话里写一次工具调用即可：1) 按 `subagent_type` 启动一个定义式专家子 Agent（系统提示词、模型、工具白名单都按 Markdown 定义文件来），2) 不带 `subagent_type` 且 `enable_fork=true` 时直接 fork 当前对话上下文跑一个临时子 Agent，3) 带 `team_name` 时把这个 spawn 注册成长期团队成员（衔接 ch15）。后台任务的完成通过 `<task-notification>` 反馈给主 Agent。

## 3. 功能需求

- F1: `AgentTool` 继承 `codebot.tools.base.Tool`，注册到主 Agent 的 `ToolRegistry`，被 LLM 当成普通工具调用。
- F2: 三档内建 Agent 类型 `general-purpose` / `Plan` / `Explore`，每档可定制工具黑名单、最大轮数、模型、系统提示词。
- F3: 支持从用户级目录 `~/.codebot/agents/*.md` 和项目级 `<work_dir>/.codebot/agents/*.md` 加载自定义 Agent 定义；项目级覆盖用户级覆盖 builtin；Markdown frontmatter 解析为 `AgentDef`。
- F4: 三种执行路径：sync（前台阻塞、`await sub_agent.run_to_completion()`）/ background（asyncio task，立即返回任务 ID）/ fork（fork 父对话上下文，强制后台）。
- F5: `TaskManager` 跟踪后台子 Agent 生命周期（running / completed / failed / cancelled），完成时把任务 ID 写进 `asyncio.Queue`，主 Agent 下一轮通过 `poll_completed` 拿到，再用 `inject_task_notifications` 拼装 `<task-notification>` 注入对话。
- F6: 四层工具过滤：MCP 直通、全局禁（`ALL_AGENT_DISALLOWED_TOOLS` 七项，含 `Agent` / `AskUserQuestion` 防递归）、custom agent（`source != "builtin"`）额外禁、background 白名单（`ASYNC_AGENT_ALLOWED_TOOLS` 16 项）、definition 级 `disallowed_tools` + `tools` 白名单。
- F7: Fork 路径：构造完整 forked `ConversationManager`（`copy.deepcopy(history)` + 给悬挂的 `tool_use` 补 `"interrupted"` placeholder `ToolResultBlock`），追加 `FORK_BOILERPLATE` + `"你的任务：\n" + task`；fork-of-fork 通过扫描对话历史 `FORK_BOILERPLATE_TAG` 拒绝。
- F8: 可选 worktree 隔离与 `WorktreeManager` 配合，子 Agent 在临时 git worktree 中跑；执行结束按 `auto_cleanup` 返回的 `kept` 标志决定是否在结果里追加 `[Worktree preserved at ...]` 提示。
- F9: 可选团队模式与 `TeamManager` 配合，走 `_execute_as_teammate` 路径注册长期团队成员，按 backend（in-process / tmux / iterm2）路由（详见 ch15）。
- F10: 父 Agent 取消（中断）时，`TaskManager.adopt_running` 把当前正在跑的 Agent 实例挂为后台任务并继续执行，主流程不阻塞。
- F11: `AgentTool` 入口额外支持 `model`（运行时模型覆盖）/ `isolation`（仅 `worktree`）/ `name`（团队场景标识）参数；`isolation` 与 `team_name` 互斥（团队场景走自己的 worktree）。
- F12: 子 Agent 定义 frontmatter 可声明 `background: true` 强制后台运行，与调用侧 `run_in_background=true` 等价。
- F13: 可选 Verification 内置角色（找最后 20% bug），由 `enable_verification` flag 守开关；默认不出现在 Agent 列表里。
- F14: Fork 子 Agent 的工具池继承父池经四层过滤（MCP 直通 + 全局黑 + 白名单 + 定义级），让 API 请求前缀字节级一致以命中 prompt cache；嵌套 fork 通过扫描父对话消息内容 `FORK_BOILERPLATE_TAG` 阻止。
- F15: Fork 复制父对话用 `copy.deepcopy`，保留每条 `Message` 的全部字段（含 `tool_uses` / `tool_results` / `thinking`），保证 assistant 消息形状与父侧一致。
- F16: 子 Agent 接受 spec 级 `permission_mode` 覆盖，运行时用独立的 `PermissionChecker`，与父共享 `DangerousCommandDetector` / `RuleEngine` 类型，但 `PathSandbox` 按子 Agent 的 `work_dir` 重新分配。
- F17: `TraceManager` 给每个 spawn 出来的子 Agent 创建 `TraceNode`，父 / 子 / trace ID 三元组打通，配合 `trace` 命令做调用树查询。

## 4. 非功能需求

- N1: 子 Agent 不能再调 `Agent` 工具（防止无限递归 / 上下文爆炸），任意层级的子 Agent 都通过 `ALL_AGENT_DISALLOWED_TOOLS` 屏蔽。
- N2: 后台 Agent 通过 `asyncio.Task.cancel()` 受控；取消调用后状态置为 `cancelled`。
- N3: `TaskManager` 在 asyncio 单线程模型下顺序安全，`_tasks` / `_async_tasks` / `_notify_queue` 必须在事件循环内访问。
- N4: fork 操作必须先在父对话历史里扫 `FORK_BOILERPLATE_TAG` 字面量，命中即 `raise ForkError`。
- N5: Sync 路径要 `await` 子 Agent 的 `run_to_completion` 直到返回，不丢消息；异常路径要把 `trace_node` 标 `failed` 再向上抛。
- N6: Fork 子 Agent 必须复用父池工具与对话内容（含 thinking blocks），让请求前缀字节级一致；任何额外过滤都会破坏 prompt cache 命中。
- N7: 子 Agent 的 `PermissionChecker` 必须独立实例，不能直接共享父引用，`permission_mode` 覆盖时不允许污染父的权限状态。
- N8: `AgentDef` frontmatter 接受的字段集合在解析层完整保留：未来章节（hooks / mcpServers / skills / memory 等）的字段必须在解析层先存得下，避免重复迁移；当前已落地 `name / description / tools / disallowedTools / model / maxTurns / permissionMode / background / isolation`。

## 5. 设计概要

- 核心数据结构：
  - `AgentTool`：承载 `AgentLoader / TaskManager / TraceManager / parent_agent / provider_config / worktree_manager / team_manager / enable_fork` 等运行时依赖。
  - `AgentDef`：Markdown frontmatter 解出来的 dataclass，含 `agent_type / when_to_use / system_prompt / tools / disallowed_tools / model / max_turns / permission_mode / background / isolation / file_path / source`。
  - `AgentToolParams`：pydantic 模型，对应 `Agent` 工具的入参 schema（`prompt / description / subagent_type / model / run_in_background / name / isolation / team_name`）。
  - `TaskManager` / `BackgroundTask` / `ProgressInfo`：后台任务的状态机 + `asyncio.Queue` 通知。
  - `TraceManager` / `TraceNode`：父子 / trace 三元组追踪，token / 状态 / 时间。
  - 工具过滤层：四张 frozenset 控制四层过滤（MCP 豁免 → 全局禁用 → 自定义额外禁用 → 异步白名单 → 定义级黑名单 + 白名单）。
- 主流程：
  - 同步：用户消息 → 主 Agent → LLM 输出 `Agent` 工具调用 → `AgentTool.execute` → 解析 `subagent_type` → 工具过滤 → 创建 `PermissionChecker` → 实例化 `Agent` 子类 → `await sub_agent.run_to_completion(prompt)` → 返回结果。
  - 异步：同上但 `is_background=True` 走 `TaskManager.launch` 启动 `asyncio.Task`，立即返回任务 ID；任务完成时把 ID 写进 `_notify_queue`，主 Agent 在 `_check_completed_tasks` 通过 `poll_completed` 抽出来再用 `inject_task_notifications` 把 `<task-notification>` 注入下一轮 user message。
  - Fork：扫父对话 `FORK_BOILERPLATE_TAG` 拒绝嵌套 → `copy.deepcopy(history)` 复制父对话（保 byte-exact）→ 给悬挂 `tool_use` 补 `"interrupted"` placeholder `ToolResultBlock` → 追加 `FORK_BOILERPLATE + "\n\n你的任务：\n" + task` → 工具池四层过滤 → 始终后台 → 完成走通知。
  - 团队成员：校验 team 存在 → 同 team 内自动 rename `<base>-<n>` → 解析 spec → 创建 worktree → 检测 backend → 用 `build_teammate_tools` 装配（含 `TaskCreate/TaskGet/TaskList/TaskUpdate/SendMessage` 五件套）→ in-process 走 `task_manager.launch` / pane 走 `spawn_tmux_teammate` 或 `spawn_iterm2_teammate`。
- 调用链（模块层级）：
  - `codebot.app:737-747` 装配 `AgentTool` 并注册进 `registry`；`app:725-728` 实例化 `AgentLoader` 并加载所有 agents；`app:788` 把 catalog 喂给主 Agent。
  - `app:1275-1279` 在主循环里调 `task_manager.poll_completed` + `inject_task_notifications`，把后台完成的子 Agent 结果灌进对话。
  - `app:1029-1031` 在中断路径调 `task_manager.adopt_running` 把当前正在跑的对话挂为后台任务。
  - `app:790 / 794` 注册 `tasks` / `trace` 两个 slash 命令以便用户主动查看后台任务和追踪树。
- 与其他模块的交互：
  - 依赖 `codebot.agent`（创建子 Agent）、`codebot.conversation`（forked 对话）、`codebot.tools`（注册中心 + 过滤）、`codebot.client`（model 路由）、`codebot.permissions`（独立 Checker）、`codebot.worktree`（隔离）、`codebot.teams`（团队成员）。
  - 被 `codebot.app` 和 `codebot.cli` 调用。

## 6. Out of Scope

- 子 Agent 输出全在内存事件流里，不落盘 task 输出文件。
- 不实现 RemoteAgent / DreamTask / LocalWorkflow / MonitorMcp 这些 TaskType。
- 不实现 fork 路径下的 worktree notice 注入（仅 `_execute_with_worktree` 支持）。
- 不接入 plugin / flag / managed 加载源（`register_plugin_source` 仅保留接口，未实装）。
- 不消费 `skills` / `hooks` / `mcpServers` / `memory` / `omitMewcodeMd` 等字段——仅在解析层保留，运行时落地留给后续章节。
- 不实现 `PermissionMode.PLAN` 的复杂裁剪与 bubble。
- 不实现 120s 自动超时切后台 / 持久化后台恢复。
- 不实现 `isolation: remote` 远端运行后端。
- 不内置 Statusline-Setup / Code-Guide 等非核心 Agent。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch13: SubAgent Tasks

> 任务粒度：每个任务可在一次会话内完成，可独立交付。

## T1: 定义 `AgentDef` dataclass + 三档 builtin Markdown
- 影响文件: `codebot/agents/parser.py`（`AgentDef` @ 23-35），`codebot/agents/builtins/general-purpose.md` / `plan.md` / `explore.md`
- 依赖任务: 无
- 完成标准: `AgentDef` 含 12 个字段（含 `agent_type / when_to_use / system_prompt / tools / disallowed_tools / model / max_turns / permission_mode / background / isolation / file_path / source`），默认 `model="inherit" / max_turns=50 / permission_mode="default"`；`Plan` builtin 设 `disallowedTools: [Agent, EditFile, WriteFile, NotebookEdit]` + `maxTurns: 15`；`Explore` builtin 设 `model: haiku` + `maxTurns: 30`。
- [ ] 完成

## T2: 实现 `parse_frontmatter` + `parse_agent_file` + 校验
- 影响文件: `codebot/agents/parser.py`（`parse_frontmatter` @ 38-58，`_validate_agent_meta` @ 61-94，`parse_agent_file` @ 97-119）
- 依赖任务: T1
- 完成标准: 解析 `---\nyaml\n---\nbody`；缺 `name` / `description` 抛 `AgentParseError`；非法 `model`（非 `inherit / haiku / sonnet / opus / ""`）抛错；非法 `permissionMode`（非 `default / acceptEdits / dontAsk / ""`）抛错；非法 `isolation`（非 `worktree / ""`）抛错；非正 `maxTurns` 抛错；YAML 解析失败抛错。
- [ ] 完成

## T3: 实现 `AgentLoader`，按 project → user → builtin 优先级加载
- 影响文件: `codebot/agents/loader.py`（`AgentLoader` @ 15-22，`_scan_directory` @ 24-39，`_load_builtins` @ 41-83，`load_all` @ 85-107，`get` @ 109-126，`list_agents` @ 128-131）
- 依赖任务: T2
- 完成标准: `load_all` 顺序 = 项目级 `<work_dir>/.codebot/agents/*.md`（`source="project"`）→ 用户级 `~/.codebot/agents/*.md`（`source="user"`）→ builtin（`importlib.resources` 读 `codebot/agents/builtins`）；同名先注册者胜出（项目级覆盖 builtin）；`enable_verification=False` 时 `Verification` 不加入；`get` 支持热重载（`file_path` 存在时重新解析）；bad file 通过 try/except + log.warning 跳过。
- [ ] 完成

## T4: 实现四层工具过滤 `resolve_agent_tools`
- 影响文件: `codebot/agents/tool_filter.py`（`ALL_AGENT_DISALLOWED_TOOLS` @ 12-20，`CUSTOM_AGENT_DISALLOWED_TOOLS` @ 22-30，`ASYNC_AGENT_ALLOWED_TOOLS` @ 32-49，`_is_mcp_tool` @ 79-80，`resolve_agent_tools` @ 83-126）
- 依赖任务: 无
- 完成标准: `ALL_AGENT_DISALLOWED_TOOLS` 含 `TaskOutput / ExitPlanMode / EnterPlanMode / Agent / AskUserQuestion / TaskStop / Workflow` 七项；MCP 工具（`mcp__` 前缀）一律放行；`source ∈ {project, user, plugin}` 触发 custom layer；`is_background=True` 时只保留 `ASYNC_AGENT_ALLOWED_TOOLS` 白名单；definition 级 `disallowed_tools` / `tools` 生效。
- [ ] 完成（测试覆盖 `tests/test_subagent.py::TestToolFilter` 六个用例）

## T5: 实现 `Fork` 模式（`build_forked_messages` + `ForkError`）
- 影响文件: `codebot/agents/fork.py`（`FORK_BOILERPLATE_TAG` @ 7，`FORK_BOILERPLATE` @ 9-23，`ForkError` @ 26-27，`build_forked_messages` @ 30-79）
- 依赖任务: 无
- 完成标准: 检测父对话历史里任意 `msg.content` 含 `FORK_BOILERPLATE_TAG` 即 `raise ForkError`；`copy.deepcopy(conversation.history)` 复制对话保 byte-exact；最后一条 assistant 消息有未完成 `tool_uses` 时补 `"interrupted"` placeholder `ToolResultBlock`；末尾 `add_user_message(f"{FORK_BOILERPLATE}\n\n你的任务：\n{task}")`。
- [ ] 完成

## T6: 实现 `TraceManager` 调用树追踪
- 影响文件: `codebot/agents/trace.py`（`TraceNode` @ 8-17，`TraceManager` @ 20-82）
- 依赖任务: 无
- 完成标准: `create(agent_type, parent_id, trace_id)` 自动生成 `agent_id`（uuid hex 12 位），无 `trace_id` 自动生成；`update(agent_id, **kw)` 改 `input_tokens / output_tokens / status` 等字段；`complete(agent_id, status)` 写 `end_time + status`；`get_tree(trace_id)` 返回同 trace 全节点；`get_total_tokens(trace_id)` 汇总 in/out tokens；操作不存在 ID 时 no-op。
- [ ] 完成

## T7: 实现 `TaskManager` + `BackgroundTask` 状态机
- 影响文件: `codebot/agents/task_manager.py`（`BackgroundTask` @ 19-31，`TaskManager` @ 34-50，`launch` @ 52-72，`_run_background` @ 74-99，`adopt_running` @ 101-122，`_continue_background` @ 124-145，`get / list_tasks / cancel / poll_completed` @ 147-178）
- 依赖任务: 无
- 完成标准: 状态机覆盖 `running / completed / failed / cancelled`；`launch` 启动 `asyncio.create_task(self._run_background(...))`，task 完成后把 `task_id` 写进 `_notify_queue`；`poll_completed` 用 `get_nowait` 一次性抽空队列；`cancel` 仅对 `running` 任务有效，调 `asyncio.Task.cancel()`；`adopt_running` 把已有 Agent 实例挂为后台任务继续执行，partial result 拼接。
- [ ] 完成

## T8: 实现 `format_task_notification` + `inject_task_notifications`
- 影响文件: `codebot/agents/notification.py`（`MAX_NOTIFICATION_RESULT_LENGTH=5000` @ 12，`format_task_notification` @ 15-44，`inject_task_notifications` @ 47-51）
- 依赖任务: T7
- 完成标准: `format_task_notification` 输出 `<task-notification>` 标签包裹的文本，含 `Task ID / Agent / Status / Elapsed / Tokens / Result`；超过 5000 字符的 result 截断为 `...\n... (truncated)`；`inject_task_notifications(conv, completed)` 把每个 task 包成 user message 追加到 conversation。
- [ ] 完成

## T9: 实现 `AgentToolParams` + `AgentTool` 类壳
- 影响文件: `codebot/tools/agent_tool.py`（`AgentToolParams` @ 21-30，`PERMISSION_MODE_MAP` @ 33-37，`TEAMMATE_ADDENDUM` @ 40-51，`AgentTool` @ 54-83）
- 依赖任务: T1, T3, T4, T5, T6, T7
- 完成标准: `AgentToolParams` 8 字段（`prompt / description` 必填，其余可选）；`AgentTool.name = "Agent"`，`category = "command"`，`is_concurrency_safe = False`；构造函数接受 `agent_loader / task_manager / trace_manager / parent_agent / enable_fork / provider_config / worktree_manager / team_manager`。
- [ ] 完成

## T10: 实现 `AgentTool.execute` 五条分支
- 影响文件: `codebot/tools/agent_tool.py`（`execute` @ 85-238）
- 依赖任务: T9
- 完成标准: 按 `team_name → isolation=="worktree" → subagent_type=="" (fork) → default sync/background` 顺序分发；`subagent_type` 给但 `loader.get` 返 None 报错列出可用类型；fork 路径在 `enable_fork=False` 时报错；`is_background = run_in_background or definition.background or enable_fork`；background 路径走 `task_manager.launch` 返回 `Task ID` 文案；前台路径异常时把 `trace_node` 标 `failed` 并返回错误。
- [ ] 完成

## T11: 实现 `_execute_with_worktree`（isolation=worktree 路径）
- 影响文件: `codebot/tools/agent_tool.py`（`_execute_with_worktree` @ 491-625）
- 依赖任务: T10
- 完成标准: `worktree_manager is None` 报错；`worktree_manager.create(wt_name, "HEAD")` 创建临时分支；任务前缀拼 `build_worktree_notice(parent.work_dir, wt.path)`；同步 `await sub_agent.run_to_completion(task)`；结束调 `worktree_manager.auto_cleanup(wt_name, wt.head_commit)`，`cleanup.kept` 为真时结果尾部追加 `[Worktree preserved at {cleanup.path}, branch {cleanup.branch}]`。
- [ ] 完成

## T12: 实现 `_execute_as_teammate`（团队成员路径，衔接 ch15）
- 影响文件: `codebot/tools/agent_tool.py`（`_execute_as_teammate` @ 240-419，`_spawn_pane_teammate` @ 421-471）
- 依赖任务: T10（ch15 的 `TeamManager.detect_backend` / `register_member` / `build_teammate_tools`）
- 完成标准: 校验 `team_manager` / `worktree_manager` 非空；team 存在；同 team 内自动重命名 `<base>-<n>`；`build_teammate_tools` 装配（含 `TaskCreate / TaskGet / TaskList / TaskUpdate / SendMessage`）；`backend ∈ {TMUX, ITERM2}` 走 `_spawn_pane_teammate`；in-process 走 `task_manager.launch`；spec system_prompt 后拼 `TEAMMATE_ADDENDUM`。
- [ ] 完成

## T13: 实现模型路由 `_select_llm` + `_create_client_for_model`
- 影响文件: `codebot/tools/agent_tool.py`（`_select_llm` @ 473-489，`_create_client_for_model` @ 627-654）
- 依赖任务: T9
- 完成标准: `params.model` 优先，其次 `definition.model`（`!= "inherit"`），fallback 父 client；`_create_client_for_model` 用 `model_map` 把 `haiku/sonnet/opus` 别名解析为完整 model id，调 `create_client(ProviderConfig)`；失败返回 None 退到父 client。
- [ ] 完成

## T14: 接入主流程（app 装配 + 主循环 hooks）
- 影响文件: `codebot/app.py`（`AgentLoader` import @ 66，`TaskManager` @ 67，`TraceManager` @ 68，`inject_task_notifications` @ 69，`AgentTool` @ 78；`self.agent_loader` 字段 @ 559-561；`AgentLoader` 实例化 @ 725-728；`AgentTool` 注册 @ 737-747；agent catalog 喂回 agent @ 764-788；slash 命令 `tasks` / `trace` 注册 @ 790-794；`adopt_running` 调用 @ 1029-1031；`poll_completed` + `inject_task_notifications` 调用 @ 1275-1279）
- 依赖任务: T1-T13
- 完成标准:
  1. `self.registry.register(agent_tool)` 在 `app.py:747` 注册；
  2. `self.agent_loader = AgentLoader(...)` 在 `app.py:725` 实例化，`load_all` 立即调；
  3. `self.agent.set_agent_catalog(...)` 在 `app.py:788` 把 catalog 喂给主 Agent；
  4. 中断路径在 `app.py:1029` 调 `task_manager.adopt_running` 把当前 stream 转后台；
  5. 主循环 `_check_completed_tasks` 在 `app.py:1275` 调 `task_manager.poll_completed` + `inject_task_notifications(self.conversation, completed)`。
- [ ] 完成

## T15: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T14
- 完成标准:
  - `ruff check codebot tests` 无新增告警；
  - `pytest tests/test_subagent.py -v` 11 个测试类全部通过（`TestAgentParser / TestAgentLoader / TestToolFilter / TestForkMode / TestTraceManager / TestTaskManager / TestNotification / TestConfig / TestPermissionMode / TestAgentToolParams / TestAgentExtensions`）；
  - 端到端路径通过现有测试覆盖：Markdown 解析、builtin / project 覆盖、四层过滤所有分支、fork 嵌套拒绝、TaskManager 状态机、notification 注入。
- [ ] 完成

## 进度
- [ ] T1 / [ ] T2 / [ ] T3 / [ ] T4 / [ ] T5 / [ ] T6 / [ ] T7 / [ ] T8 / [ ] T9 / [ ] T10 / [ ] T11 / [ ] T12 / [ ] T13 / [ ] T14 / [ ] T15

```

```markdown
# ch13: SubAgent Checklist

> 所有条目可勾选、可观测。验收方式写在条目后面括号中。验收：已通过验证的项均勾选。

## 1. 实现完整性

- [ ] 类 `AgentTool` 在 `codebot/tools/agent_tool.py:54-83` 存在，构造参数含 `agent_loader / task_manager / trace_manager / parent_agent / enable_fork / provider_config / worktree_manager / team_manager`
- [ ] dataclass `AgentDef` 在 `codebot/agents/parser.py:23-35` 存在，12 个字段齐全（含 `agent_type / when_to_use / system_prompt / tools / disallowed_tools / model / max_turns / permission_mode / background / isolation / file_path / source`）
- [ ] pydantic 模型 `AgentToolParams` 在 `codebot/tools/agent_tool.py:21-30` 存在，必填 `prompt / description`，可选 `subagent_type / model / run_in_background / name / isolation / team_name`
- [ ] 类 `TaskManager` / `BackgroundTask` / `ProgressInfo` 在 `codebot/agents/task_manager.py:34/19/14` 存在，含 `_notify_queue: asyncio.Queue`
- [ ] 类 `TraceManager` / `TraceNode` 在 `codebot/agents/trace.py:20/8` 存在，三元组 `agent_id / parent_id / trace_id`
- [ ] 三档 builtin 在 `codebot/agents/builtins/{general-purpose,plan,explore}.md` 存在；`Plan` 的 `disallowedTools` 含 `Agent / EditFile / WriteFile / NotebookEdit` 且 `maxTurns: 15`；`Explore` 的 `model: haiku` + `maxTurns: 30`
- [ ] `resolve_agent_tools` 在 `codebot/agents/tool_filter.py:83-126` 实现四层过滤
- [ ] `parse_agent_file` 在 `codebot/agents/parser.py:97-119` 验证 `name` / `description` 必填，`model` / `permissionMode` / `isolation` 取值白名单
- [ ] `build_forked_messages` 在 `codebot/agents/fork.py:30-79` 嵌套 fork 检查（扫描 `FORK_BOILERPLATE_TAG`）
- [ ] `build_forked_messages` 在 `codebot/agents/fork.py:55-74` 给悬挂 `tool_uses` 补 `"interrupted"` placeholder `ToolResultBlock`
- [ ] 错误消息 `"Cannot fork from a forked agent."` 在 `codebot/agents/fork.py:36` 与原始定义的 fork 检查语义一致

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `grep -rn "AgentTool(" codebot --include="*.py"` 在 `codebot/app.py:737` 找到注册调用方
- [ ] `self.registry.register(agent_tool)` 调用点在主流程 `codebot/app.py:747`，所有依赖（`agent_loader / task_manager / trace_manager / parent_agent / enable_fork / provider_config / worktree_manager / team_manager`）齐全注入
- [ ] `AgentLoader(...).load_all()` 调用点在 `codebot/app.py:725-728`
- [ ] `task_manager.poll_completed` 调用点在 `codebot/app.py:1275`（通过 `_check_completed_tasks`）
- [ ] `inject_task_notifications` 调用点在 `codebot/app.py:1279`
- [ ] `task_manager.adopt_running` 调用点在 `codebot/app.py:1029`（中断触发的后台挂载）
- [ ] `agent.set_agent_catalog` 调用点在 `codebot/app.py:788`（把 catalog 喂给主 Agent 系统提示）
- [ ] `tasks` / `trace` slash 命令在 `codebot/app.py:790-794` 注册
- [ ] Schema 暴露：`Agent` 工具通过 `AgentTool.params_model = AgentToolParams` 注册到 registry，TUI 的 `ToolSearch` 可发现它

## 3. 编译与测试

- [ ] `ruff check codebot tests` 通过（无新增告警）
- [ ] `pytest tests/test_subagent.py -v` 通过（`TestAgentParser` 13 个 + `TestAgentLoader` 9 个 + `TestToolFilter` 7 个 + `TestForkMode` 5 个 + `TestTraceManager` 9 个 + `TestTaskManager` 6 个 + `TestNotification` 3 个 + `TestConfig` 2 个 + `TestPermissionMode` 1 个 + `TestAgentToolParams` 2 个 + `TestAgentExtensions` 2 个 全部 PASS）
- [ ] `pytest tests/ -q` 全套通过

## 4. 端到端验证

- [ ] 注册路径：在 app 启动后 `agent_tool` 放入 registry（`app.py:747`）；用户向主 Agent 发送 "spawn a Plan agent to review X" → LLM 返回 `Agent` 工具调用 → `execute` → 同步路径 `await sub_agent.run_to_completion(prompt)` → 子 Agent 输出文本返回主 Agent
- [ ] Fork 路径：`enable_fork=true` 时用户说 "fork to investigate Y" → LLM 调用 `Agent` 不带 `subagent_type` → `build_forked_messages` → `is_background=True` 走 `task_manager.launch` → 完成时 `<task-notification>` 通过 `poll_completed + inject_task_notifications` 注入下一轮（`app.py:1275-1279`）
- [ ] 后台路径：调用带 `run_in_background=true` 或定义 `background: true` → 立即返回 `Task ID: ...` 文案 → 后台 `asyncio.Task` 完成后 `task_id` 入队
- [ ] 中断挂后台路径：用户中断 → `app.py:1029` 调 `task_manager.adopt_running` → 当前 Agent 转后台 task，状态从 `running` 走完整状态机
- [ ] 证据：单元测试 + grep 调用方 + 主流程文件行号已列出

## 5. 文档

- [ ] `docs/python/ch13/spec.md` 已写
- [ ] `docs/python/ch13/tasks.md` 已写，15 个 T 全部勾完
- [ ] `docs/python/ch13/checklist.md` 已写并逐项验收
- [ ] commit 信息标注 `ch13` 与三件套关闭状态（待用户确认后由人或 CI 触发）

---

## 6. 关键常量与字段（grep 验证）

- [ ] `ALL_AGENT_DISALLOWED_TOOLS` 在 `codebot/agents/tool_filter.py:12-20` 含七项：`TaskOutput / ExitPlanMode / EnterPlanMode / Agent / AskUserQuestion / TaskStop / Workflow`
- [ ] `ASYNC_AGENT_ALLOWED_TOOLS` 在 `codebot/agents/tool_filter.py:32-49` 含 16 项：`ReadFile / WebSearch / TodoWrite / Grep / WebFetch / Glob / Bash / EditFile / WriteFile / NotebookEdit / Skill / LoadSkill / SyntheticOutput / ToolSearch / EnterWorktree / ExitWorktree`
- [ ] `IN_PROCESS_TEAMMATE_ALLOWED_TOOLS` 在 `codebot/agents/tool_filter.py:60-66` 含 `ASYNC + TaskCreate / TaskGet / TaskList / TaskUpdate / SendMessage / CronCreate / CronDelete / CronList`
- [ ] `FORK_BOILERPLATE_TAG = "<fork_boilerplate>"` 在 `codebot/agents/fork.py:7`
- [ ] `MAX_NOTIFICATION_RESULT_LENGTH = 5000` 在 `codebot/agents/notification.py:12`
- [ ] `VALID_MODELS = {"inherit", "sonnet", "opus", "haiku", ""}` 在 `codebot/agents/parser.py:11`
- [ ] `VALID_PERMISSION_MODES = {"default", "acceptEdits", "dontAsk", ""}` 在 `codebot/agents/parser.py:12`
- [ ] `VALID_ISOLATION_MODES = {"", "worktree"}` 在 `codebot/agents/parser.py:20`
- [ ] `PROJECT_AGENTS_DIR = ".codebot/agents"` 与 `USER_AGENTS_DIR = "~/.codebot/agents"` 在 `codebot/agents/loader.py:11-12`
- [ ] `PERMISSION_MODE_MAP` 在 `codebot/tools/agent_tool.py:33-37` 把 `default / acceptEdits / dontAsk` 映射到 `PermissionMode` 枚举
- [ ] `TEAMMATE_ADDENDUM` 在 `codebot/tools/agent_tool.py:40-51` 包含 `"You are running as an agent in a team"` 提示

## 7. 测试用例点名（pytest）

- [ ] `TestAgentParser::test_parse_valid_agent` PASS
- [ ] `TestAgentParser::test_parse_missing_name` / `test_parse_missing_description` PASS
- [ ] `TestAgentParser::test_parse_invalid_model` / `test_parse_invalid_permission_mode` PASS
- [ ] `TestAgentLoader::test_load_builtins`：`Explore / Plan / general-purpose` 三档全在
- [ ] `TestAgentLoader::test_verification_disabled_by_default` / `test_verification_enabled` PASS
- [ ] `TestAgentLoader::test_project_overrides_builtin` PASS（项目级覆盖 builtin）
- [ ] `TestAgentLoader::test_hot_reload` PASS
- [ ] `TestToolFilter::test_global_disallowed` / `test_disallowed_tools_in_definition` / `test_tools_whitelist` / `test_background_whitelist` / `test_combined_whitelist_and_blacklist` / `test_custom_agent_extra_restrictions` / `test_builtin_no_custom_restrictions` 全部 PASS
- [ ] `TestForkMode::test_basic_fork` / `test_fork_preserves_history` / `test_fork_wraps_pending_tool_use` / `test_no_double_fork` / `test_fork_is_deep_copy` PASS
- [ ] `TestTraceManager::test_create_node` / `test_get_tree` / `test_get_total_tokens` PASS
- [ ] `TestTaskManager::test_launch_and_complete` / `test_poll_completed` / `test_cancel` / `test_failed_task` / `test_list_tasks` PASS
- [ ] `TestNotification::test_format_notification` / `test_truncate_long_result` / `test_inject_notifications` PASS
- [ ] `TestAgentToolParams::test_required_fields` / `test_optional_fields` PASS

```

### Java

```markdown
# ch13: SubAgent Spec（Java 版）

## 1. 背景

主 Agent 做大任务时会塞满上下文：研究、规划、写代码、跑测试都堆在一个对话里，单一窗口很快耗尽。这一章把"开一个上下文隔离的新 Agent 去做一件事"做成主 Agent 可以直接调用的工具，让主 Agent 学会分发工作，避免上下文爆炸，同时通过专门角色（plan / explore）和后台异步执行扩展并发能力。

## 2. 目标

提供 `Agent` 工具（`AgentTool implements Tool`），主 Agent 在对话里写一次工具调用即可：1) 按 `subagent_type` 启动一个定义式专家子 Agent（系统提示词、模型、工具白名单都按 Markdown 定义文件来），2) 不带 `subagent_type` 时直接 fork 当前对话上下文跑一个临时子 Agent，3) 带 `team_name` 时把这个 spawn 注册成长期团队成员（衔接 ch15）。后台任务的完成通过 `TaskNotification` 由父 Agent 在下一轮抽取注入。

## 3. 功能需求

- F1: `AgentTool` 实现 `com.codebot.tool.Tool` 接口，注册到主 Agent 的 `ToolRegistry`，被 LLM 当成普通工具调用；`shouldDefer()` 返回 `true`，只在 ToolSearch 选中时才把 schema 暴露给模型。
- F2: 三档内建 Agent 类型 `general-purpose` / `plan` / `explore`（`SubAgentSpec.GENERAL_PURPOSE / PLAN / EXPLORE` 静态实例），每档可定制工具黑名单（`disallowedTools`）、最大轮数（`maxTurns`）、模型（`model`）、系统提示词覆盖（`systemPromptOverride`）。
- F3: `AgentLoader.loadAll(projectRoot)` 按 builtin → `~/.codebot/agents/*.md`（用户级）→ `<projectRoot>/.codebot/agents/*.md`（项目级）顺序加载，同名后注册覆盖前者；Markdown frontmatter 解析为 `SubAgentSpec`。
- F4: 三种执行路径：sync（前台阻塞、`AgentTool.runSync` 流式回写 LLM）/ async（后台虚拟线程、立即返回 `task_N`）/ fork（fork 父对话上下文，强制后台）。
- F5: `SubAgentTaskManager` 跟踪后台子 Agent 生命周期（`PENDING / RUNNING / COMPLETED / FAILED / CANCELLED`），完成或失败时把 `TaskNotification` 入队，主 Agent 下一轮通过 `drainNotifications()` 取出并注入到 conversation。
- F6: 六层工具过滤（`ToolFilter.filterForAgent`）：MCP 豁免 → 全局禁（`ALWAYS_DISALLOWED`：`Agent` / `AskUserQuestion` 等 7 项防递归）→ custom agent 额外禁（`CUSTOM_AGENT_DISALLOWED`）→ async 白名单（`ASYNC_ALLOWED` 仅 15 项基础工具）→ definition 级黑名单 → definition 级白名单交集。
- F7: Fork 路径：构造完整 forked conversation（拷贝父消息，给悬挂的 `toolUses` 补 placeholder `ToolResultBlock("(tool execution interrupted by fork)")`），追加 fork boilerplate 系统约束 + 任务文本；fork-of-fork 通过扫描父对话内容中的 `<fork_boilerplate>` 标签拒绝。
- F8: 可选 worktree 隔离与 `WorktreeManager` 配合，子 Agent 在临时 git worktree 中跑；执行结束按 `WorktreeChanges.hasChanges(...)` 决定保留 / 移除。
- F9: 可选团队模式与 `TeamManager` 配合，走 `SpawnDispatcher.spawnTeammate` 注册长期团队成员（详见 ch15）。
- F10: in-process teammate 在 async 白名单层额外放行 `Agent` + `IN_PROCESS_TEAMMATE_ALLOWED`（`TaskCreate / TaskGet / TaskList / TaskUpdate / SendMessage / CronCreate / CronDelete / CronList`）。
- F11: 子 Agent 后台执行通过 `Thread.startVirtualThread` 启动；`cancelTask(id)` 通过 `Thread.interrupt()` 取消。
- F12: 模型选择 `selectClient` 优先用调用级 `model` 参数，其次用 spec 的 `model`，都没设或为 `inherit` / 空字符串时复用父 client；`ModelResolver` 把 `haiku/sonnet/opus` 别名解析为具体 model ID。
- F13: 父对话引用 (`parentConversation`) 由 TUI 通过 `setParentConversation` 注入；缺失时 fork 路径报错。

## 4. 非功能需求

- N1: 子 Agent 不能再调 `Agent` 工具（防止无限递归 / 上下文爆炸），任意层级的子 Agent 都通过 `ALWAYS_DISALLOWED` 屏蔽。
- N2: 后台 Agent 通过 `Thread.interrupt()` 受控；`cancelTask` 状态置为 `CANCELLED` 并发出对应 `TaskNotification`。
- N3: `SubAgentTaskManager` 所有公共方法用 `synchronized` 守护（虚拟线程与主线程同时操作 `tasks` / `notifications`）。
- N4: fork 操作必须先在父对话所有消息内容里搜 `<fork_boilerplate>` 标签拒绝嵌套 fork。
- N5: Sync 路径要走子 Agent 的完整 `BlockingQueue<AgentEvent>` 事件流：`StreamText` 累积输出 / `ToolResultEvent` 发 progress / `ErrorEvent` 报错退出 / `LoopComplete` 结束并清理 worktree。
- N6: Fork 子 Agent 复用父池工具（直接传 `parentRegistry`）与对话内容（含 `ThinkingBlock`），通过 `conv.addAssistantFull(content, thinkingBlocks, toolUses)` 保形。
- N7: 工具集传递使用 `ToolRegistry.listTools()` 枚举 + `register(tool)` 复制，避免污染父 registry。
- N8: 子 Agent 定义 frontmatter 字段集合需在解析层完整保留；未来章节扩展字段必须在解析层先存得下，避免重复迁移。

## 5. 设计概要

- 核心类型:
 - `AgentTool`（`src/main/java/com/codebot/subagent/AgentTool.java`）：承载 `client` / `parentRegistry` / `protocol` / `modelResolver` / `agentSpecs` / `progressListener` / `taskManager` / `parentConversation` / `worktreeManager` / `teamManager` 等运行时依赖；`description()` 动态把可用 agent 类型拼进描述文案。
 - `SubAgentSpec`（record）：`name / description / tools / disallowedTools / systemPromptOverride / maxTurns / model`；`PLAN_AGENT_SYSTEM_PROMPT` 为 plan 角色的硬编码系统提示。
 - `SubAgentTaskManager`：内部 `TaskEntry`（id / name / status / output / error / thread）状态机；`TaskNotification` record；`spawnSubAgent` 启动虚拟线程。
 - `SubAgentProgress`（record）：进度事件，含 `agentType / description / toolName / toolOutput / toolError / done / toolCount / totalTime`。
 - `ToolFilter`：四个 `Set<String>`（`ALWAYS_DISALLOWED` 7 项 / `CUSTOM_AGENT_DISALLOWED` 7 项 / `ASYNC_ALLOWED` 15 项 / `IN_PROCESS_TEAMMATE_ALLOWED` 8 项）实现六层过滤。
 - `AgentLoader`：`VALID_MODELS = {"", "inherit", "haiku", "sonnet", "opus"}`；`parseAgentFile` 用 SnakeYAML 解析 frontmatter。
- 主流程:
 - 同步：用户消息 → 主 Agent → LLM 输出 `Agent` 工具调用 → `AgentTool.execute(args)` → 解析 `subagent_type` → `resolveSpec` → `runSync` → `ToolFilter.filterForAgent` → 构造子 `Agent` → `subAgent.run(conv)` → 消费 `BlockingQueue<AgentEvent>` 直到 `LoopComplete` → 返回结果。
 - 异步：调 `taskManager.spawnSubAgent`，立即返回 `Agent "..." launched in background (task task_N).`；后台虚拟线程跑完写 `setCompleted` 或 `setFailed`，主 Agent 下一轮 `drainNotifications` 抽出 `TaskNotification` 注入对话。
 - Fork：扫父对话 → 拷贝消息（含 `ThinkingBlock` 与悬挂 `toolUses` 占位 `ToolResultBlock`）→ 追加 `FORK_BOILERPLATE + "\n\nYour task:\n" + prompt` → 始终调 `taskManager.spawnSubAgent` 走后台。
 - 团队成员：校验 team 存在、name 去重 → 过滤工具集 + 注入 `SendMessageTool` → 调 `SpawnDispatcher.spawnTeammate` 拿 backend hint → 立即返回。
- 调用链:
 - 主流程组装在主 Agent 启动时把 `AgentTool` 注册到 `ToolRegistry`，并通过 setter 注入 `taskManager` / `agentSpecs` / `parentConversation` / `progressListener` / `worktreeManager` / `teamManager` / `modelResolver`。
 - Agent loop（`com.codebot.agent.Agent.agentLoop`）每轮开头通过 `notificationFn` 抽取 `TaskNotification` 注入 `conv.addSystemReminder`。
- 与其他模块的交互:
 - 依赖 `com.codebot.agent`（创建子 Agent）、`com.codebot.conversation`（forked ConversationManager）、`com.codebot.tool`（注册中心 + 过滤）、`com.codebot.llm`（`LlmClient` / `ModelResolver`）、`com.codebot.worktree`（隔离）、`com.codebot.teams`（团队成员）。
 - 被主 Agent 装配点（`Main` / TUI 层）调用。

## 6. Out of Scope

- 子 Agent 输出全在内存事件流里，不落盘 task 输出文件。
- 不实现 RemoteAgent / DreamTask / LocalWorkflow / MonitorMcp 这些 TaskType。
- 不实现 fork 路径的 worktree notice（仅同步 isolation 路径支持）。
- 不接入 plugin / flag / managed 加载源（只支持 builtin / user / project）。
- 不消费 `skills` / `hooks` / `mcpServers` / `memory` / `permissionMode` 等扩展字段——本章 frontmatter 解析层保留五个核心字段，扩展字段留给后续章节。
- 不实现 PermissionMode 的 bubble / auto 模式。
- 不实现 120s 自动超时切后台 / ESC 切后台 / 持久化后台恢复。
- 不实现 `isolation: remote` 远端运行后端。
- 不内置 Verification 等附加 Agent。
- 不在本章实现 Fork 模式的字节级 prompt cache 命中重构（thinking blocks 拷贝已具备，但调用级 `useExactTools / cloneRegistryForFork` 留作后续）。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch13: SubAgent Tasks（Java 版）

> 任务粒度：每个任务可在一次会话内完成，可独立交付。

## T1: 定义 `SubAgentSpec` record + 三档 builtin
- 影响文件: `src/main/java/com/codebot/subagent/SubAgentSpec.java`（record 头 @ 10-18；`PLAN_AGENT_SYSTEM_PROMPT` @ 20-54；`GENERAL_PURPOSE` @ 56-64；`PLAN` @ 66-75；`EXPLORE` @ 77-85）
- 依赖任务: 无
- 完成标准:
 - record 字段七项（`name / description / tools / disallowedTools / systemPromptOverride / maxTurns / model`）齐全；
 - `PLAN.disallowedTools()` 含 `EditFile / WriteFile`，`maxTurns == 15`，使用 `PLAN_AGENT_SYSTEM_PROMPT` 作为 prompt override；
 - `EXPLORE.disallowedTools()` 含 `EditFile / WriteFile`，`maxTurns == 30`，`model == "haiku"`；
 - `GENERAL_PURPOSE.maxTurns == 200`，无 prompt override。
- [ ] 完成

## T2: 实现 `AgentLoader.parseAgentFile`（Markdown frontmatter 解析）
- 影响文件: `src/main/java/com/codebot/subagent/AgentLoader.java`（`VALID_MODELS` @ 27；`parseAgentFile` @ 95-150；`getString` @ 152-155；`getStringList` @ 157-170）
- 依赖任务: T1
- 完成标准:
 - 用 SnakeYAML 解析两个 `---` 之间的 frontmatter；
 - 缺 `name` / `description` 抛 `IllegalArgumentException`（含路径与字段名）；
 - `model` 非空时校验 ∈ `{"", "inherit", "haiku", "sonnet", "opus"}`，非法值抛错；
 - body 为空时 `systemPromptOverride == null`；
 - `tools` / `disallowedTools` 缺省返回 `List.of()`。
- [ ] 完成

## T3: 实现 `AgentLoader.loadAll`（builtin → user → project 三层优先级）
- 影响文件: `src/main/java/com/codebot/subagent/AgentLoader.java`（`agents` 字段 @ 29；`loadAll` @ 39-53；`listNames` @ 58-62；`loadBuiltins` @ 64-68；`loadDir` @ 70-89）
- 依赖任务: T2
- 完成标准:
 - 先 `loadBuiltins` 注入三档 builtin；
 - 再 `~/.codebot/agents/*.md`（user）；
 - 最后 `<projectRoot>/.codebot/agents/*.md`（project）；
 - 同名后注册覆盖前者（`LinkedHashMap` 保 put 覆盖语义）；
 - 目录不存在静默跳过；解析失败的文件静默跳过（catch 后不抛）。
- [ ] 完成

## T4: 实现 `ToolFilter` 六层过滤
- 影响文件: `src/main/java/com/codebot/subagent/ToolFilter.java`（`ALWAYS_DISALLOWED` @ 30-33；`CUSTOM_AGENT_DISALLOWED` @ 36-39；`ASYNC_ALLOWED` @ 42-46；`IN_PROCESS_TEAMMATE_ALLOWED` @ 49-52；`filterForAgent(source, spec)` @ 60-62；`filterForAgent(source, spec, isAsync, isCustom, isInProcessTeammate)` @ 77-133；`isMcpTool` @ 135-137）
- 依赖任务: 无（独立模块）
- 完成标准:
 - `ALWAYS_DISALLOWED` 含 7 项（`TaskOutput / ExitPlanMode / EnterPlanMode / Agent / AskUserQuestion / TaskStop / Workflow`）；
 - `ASYNC_ALLOWED` 含 15 项（详见 checklist 7.1）；
 - `mcp__` 前缀工具直接通过；
 - 异步模式下 in-process teammate 额外允许 `Agent` + `IN_PROCESS_TEAMMATE_ALLOWED` 8 项；
 - 自定义 spec 的 `disallowedTools` 与 `tools`（白名单交集）都生效；
 - `tools == ["*"]` 视为无白名单（即不过滤）。
- [ ] 完成

## T5: 实现 `SubAgentTaskManager` 状态机 + 通知队列
- 影响文件: `src/main/java/com/codebot/subagent/SubAgentTaskManager.java`（`TaskStatus` @ 19；`Task` @ 21；`TaskNotification` @ 23；`TaskEntry` @ 29-42；`createTask` @ 44-48；`setRunning` @ 50-56；`setCompleted` @ 58-65；`setFailed` @ 67-74；`cancelTask` @ 76-85；`drainNotifications` @ 87-91；`getTask` @ 93-97；`listTasks` @ 99-103）
- 依赖任务: 无
- 完成标准:
 - 状态机覆盖 `PENDING / RUNNING / COMPLETED / FAILED / CANCELLED`；
 - `setCompleted` / `setFailed` / `cancelTask` 各自把 `TaskNotification` 入队；
 - `drainNotifications` 一次性取出并清空，返回不可变拷贝；
 - 所有公共方法 `synchronized`；
 - `nextId` 用 `AtomicInteger`，taskId 形如 `task_N`。
- [ ] 完成

## T6: 实现 `SubAgentTaskManager.spawnSubAgent`（后台虚拟线程）
- 影响文件: `src/main/java/com/codebot/subagent/SubAgentTaskManager.java`（`spawnSubAgent` @ 108-164；`truncate` @ 166-168）
- 依赖任务: T1, T4, T5
- 完成标准:
 - 调 `createTask` 拿 `task_N`；
 - `Thread.startVirtualThread` 启动后台线程；
 - 内部 `ToolFilter.filterForAgent(registry, spec)` 拿子 registry（注：本章 spawn 路径不带 async 标志，等价 sync 过滤）；
 - 启动 `subAgent.run(conv)` 拿 `BlockingQueue<AgentEvent>`；
 - 事件循环：`StreamText` 累积；`ErrorEvent` → `setFailed`；`LoopComplete` → `setCompleted`；`InterruptedException` → `setFailed("Interrupted")`；`poll(60s)` 超时 → `setFailed("Timeout")`；
 - 线程引用通过 `setRunning(taskId, thread)` 写回。
- [ ] 完成

## T7: 实现 `AgentTool` 框架 + `schema()` + `description()`
- 影响文件: `src/main/java/com/codebot/subagent/AgentTool.java`（类头 @ 29-66；构造器 + setter @ 68-104；`name()` @ 108-111；`description()` @ 113-137；`category()` @ 139-142；`schema()` @ 144-196；`shouldDefer()` @ 198-201）
- 依赖任务: T1, T3
- 完成标准:
 - 实现 `Tool` 接口，`name() == "Agent"`；
 - `description()` 动态把 `agentSpecs` 里的 agent 列出来；缺省时 fallback 列出三档 builtin；
 - `schema()` 暴露 6 个属性：`description / prompt / subagent_type / model / run_in_background / isolation / team_name`；`subagent_type.enum` 由 `AgentLoader.listNames(agentSpecs)` 动态生成；
 - `required = ["description", "prompt"]`；
 - `shouldDefer() == true`；
 - `FORK_BOILERPLATE_TAG = "<fork_boilerplate>"`，`FORK_BOILERPLATE` text block 含五条规则。
- [ ] 完成

## T8: 实现 `AgentTool.execute` 五条分支
- 影响文件: `src/main/java/com/codebot/subagent/AgentTool.java`（`execute` @ 204-240；`resolveSpec` @ 415-425；`getStringArg` @ 522-525）
- 依赖任务: T6, T7
- 完成标准:
 - 缺 `description` / `prompt` 返回 `ToolResult.error("Error: description and prompt are required")`；
 - 分支顺序：`subagent_type` 空 → `runFork`；`teamName != null && teamManager != null` → `runAsTeammate`；`run_in_background == true` → `runAsync`；默认 → `runSync`；
 - `resolveSpec` 优先查 `agentSpecs`，回退到 switch 三档 builtin；
 - 未知 `subagent_type` 返回 `Error: unknown agent type '...'. Available: ...`。
- [ ] 完成

## T9: 实现 `runSync`（前台流式 + 可选 worktree）
- 影响文件: `src/main/java/com/codebot/subagent/AgentTool.java`（`runSync` @ 310-413；`selectClient` @ 489-501；`emitProgress` @ 503-516；`elapsedSeconds` @ 518-520）
- 依赖任务: T4, T8
- 完成标准:
 - `ToolFilter.filterForAgent(parentRegistry, spec)` 拿子 registry；
 - 子 Agent `maxIterations` 取 `spec.maxTurns()` 或 fallback 200；
 - 事件循环消费 `StreamText` 累积输出 / `ToolResultEvent` 发 progress / `ErrorEvent` 报错退出 / `LoopComplete` 结束；
 - `poll(60, SECONDS)` 超时返回 `Agent timed out waiting for events`；
 - `isolation == "worktree"` 且 `worktreeManager != null` 时创建临时分支，slug `agent-aXXXXXXX`（7 位 hex）；
 - 结束时 `WorktreeChanges.hasChanges` 决定保留 / 调用 `AgentWorktree.remove`；
 - 最终消息含 `Agent "%s" completed in %d.%03ds.\n\n%s%s`。
- [ ] 完成

## T10: 实现 `runFork`（fork 父对话）
- 影响文件: `src/main/java/com/codebot/subagent/AgentTool.java`（`runFork` @ 255-282；`buildForkedConversation` @ 284-308）
- 依赖任务: T6, T8
- 完成标准:
 - `parentConversation == null` → 报错 `Error: fork requires parent conversation context`；
 - `taskManager == null` → 报错 `Error: fork requires task manager for background execution`；
 - 扫父对话每条 `getContent().contains(FORK_BOILERPLATE_TAG)` → 报错 `Error: cannot fork from a forked agent. Use subagent_type to spawn a definition-based agent instead.`；
 - `buildForkedConversation`：对带 `toolUses` 但无 `toolResults` 的 assistant 消息走 `addAssistantFull` + 追加占位 `ToolResultBlock("(tool execution interrupted by fork)")`；对带 `toolUses` 有 `toolResults` 的走 `addAssistantFull`；对纯 assistant 走 `addAssistantMessage`；对 user 走 `addUserMessage`；
 - 最后 `addUserMessage(FORK_BOILERPLATE + "\n\nYour task:\n" + task)`；
 - fork 始终调 `taskManager.spawnSubAgent`，提示文案含 `Forked agent "%s" launched in background (task %s). Results will arrive via task-notification.`。
- [ ] 完成

## T11: 实现 `runAsync`（builtin spec → 后台）
- 影响文件: `src/main/java/com/codebot/subagent/AgentTool.java`（`runAsync` @ 244-253）
- 依赖任务: T6, T8
- 完成标准:
 - `taskManager == null` → 报错 `Background execution not available (no task manager configured)`；
 - 调 `selectClient(spec.model(), modelOverride)` 拿子 client；
 - 调 `taskManager.spawnSubAgent` 拿 `task_N`；
 - 返回 `Agent "%s" launched in background (task %s). You will be notified when it completes.`。
- [ ] 完成

## T12: 实现 `runAsTeammate`（团队成员路径，衔接 ch15）
- 影响文件: `src/main/java/com/codebot/subagent/AgentTool.java`（`runAsTeammate` @ 427-487）
- 依赖任务: T8（ch15 的 `SpawnDispatcher.spawnTeammate`）
- 完成标准:
 - 校验 `teamManager.getTeam(teamName) != null`，否则报错 `Error: team '%s' not found. Create it first with TeamCreate.`；
 - memberName 用 `description` 处理（小写 + `\\s+` 替换为 `-` + 截断 30 字符 + 同名递增 `-2 / -3 ...`）；
 - `ToolFilter.filterForAgent` 之后注入 `TeamTools.SendMessageTool(teamManager, memberName)`；
 - 可选 worktree 隔离（同 `runSync` 逻辑）；
 - 调 `SpawnDispatcher.spawnTeammate(SpawnConfig(...))` 拿 `spawnResult`；
 - 返回 `Teammate "%s" spawned in team "%s" (mode: %s). The teammate is now working on the assigned task.`。
- [ ] 完成

## T13: 接入主流程
- 影响文件: 主 Agent 装配点（`cmd/codebot/main.go` 对应的 Java 装配类，例如 `com.codebot.Main` 或 `TuiBootstrap`）
- 依赖任务: T1-T12
- 完成标准:
 1. 构造 `AgentTool(client, registry, protocol)` 后通过 setter 注入 `agentSpecs`（来自 `AgentLoader.loadAll(projectRoot)`）、`taskManager` (`new SubAgentTaskManager()`)、`progressListener`、`parentConversation`、`worktreeManager`、`teamManager`、`modelResolver`；
 2. `registry.register(agentTool)`；
 3. 主 Agent 的 `notificationFn` 绑定到一个把 `taskManager.drainNotifications()` 转成可读字符串列表的 supplier。
- [ ] 完成

## T14: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T13
- 完成标准:
 - `./gradlew build` 成功；
 - SubAgent 模块单测全通过（loader 解析正确 / 三档 builtin 字段断言 / 六层过滤分支覆盖 / TaskManager 状态机覆盖 / `runFork` 嵌套拒绝）；
 - 手动跑一次：主 Agent → 调 `Agent` 工具（`subagent_type=plan`）→ 看到 `Agent "..." completed in ...` 输出；
 - 手动跑一次：主 Agent → 调 `Agent` 工具（`run_in_background=true`）→ 看到 `task_N` 立即返回，下一轮收到完成通知。
- [ ] 完成

## 进度
- [ ] T1 / [ ] T2 / [ ] T3 / [ ] T4 / [ ] T5 / [ ] T6 / [ ] T7 / [ ] T8 / [ ] T9 / [ ] T10 / [ ] T11 / [ ] T12 / [ ] T13 / [ ] T14

```

```markdown
# ch13: SubAgent Checklist（Java 版）

> 所有条目可勾选、可观测。验收方式写在条目后面括号中。验收：已通过验证的项均勾选。

## 1. 实现完整性

- [ ] 类 `AgentTool` 在 `src/main/java/com/codebot/subagent/AgentTool.java:29-526` 存在，字段含 `client / parentRegistry / protocol / modelResolver / agentSpecs / progressListener / taskManager / parentConversation / worktreeManager / teamManager`
- [ ] record `SubAgentSpec` 在 `src/main/java/com/codebot/subagent/SubAgentSpec.java:10-18` 存在，七个字段（`name / description / tools / disallowedTools / systemPromptOverride / maxTurns / model`）齐全
- [ ] record `SubAgentProgress` 在 `src/main/java/com/codebot/subagent/SubAgentProgress.java:16-25` 存在，八个字段齐全
- [ ] 类 `SubAgentTaskManager` 在 `src/main/java/com/codebot/subagent/SubAgentTaskManager.java:17-169` 存在；含 `TaskStatus` enum（`PENDING / RUNNING / COMPLETED / FAILED / CANCELLED`）、`Task` record、`TaskNotification` record、`TaskEntry` 内部类
- [ ] 三档 builtin（`GENERAL_PURPOSE / PLAN / EXPLORE`）在 `SubAgentSpec.java:56-85` 注册，分别对应 `maxTurns = 200 / 15 / 30`
- [ ] `ToolFilter.filterForAgent` 在 `src/main/java/com/codebot/subagent/ToolFilter.java:77-133` 实现六层过滤
- [ ] `AgentLoader.parseAgentFile` 在 `src/main/java/com/codebot/subagent/AgentLoader.java:95-150` 校验 `name` / `description` 必填，`model` 取值白名单（`VALID_MODELS` @ 27）
- [ ] `AgentTool.runFork` 在 `agent_tool` 对应 `AgentTool.java:255-282` 嵌套 fork 检查（扫描 `<fork_boilerplate>` 标签）
- [ ] `buildForkedConversation` 在 `AgentTool.java:284-308` 给悬挂 `toolUses` 补占位 `ToolResultBlock("(tool execution interrupted by fork)")`
- [ ] 错误消息 `"Error: cannot fork from a forked agent. Use subagent_type to spawn a definition-based agent instead."` 在 `AgentTool.java:266` 与文档描述的 isInForkChild 语义一致

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `AgentTool` 实例由主装配点构造并通过 setter 注入依赖：`setAgentSpecs(AgentLoader.loadAll(projectRoot))` / `setTaskManager(new SubAgentTaskManager())` / `setProgressListener(...)` / `setParentConversation(...)` / `setWorktreeManager(...)` / `setTeamManager(...)` / `setModelResolver(...)`
- [ ] `registry.register(agentTool)` 在装配阶段调用
- [ ] 主 Agent 的 `notificationFn` 绑定 `() -> taskManager.drainNotifications().stream().map(...).toList()`，使后台任务完成通知能在下一轮注入 conversation（`com.codebot.agent.Agent.agentLoop` @ 79-83）
- [ ] `SubAgentProgress` 的消费者（TUI / 日志）订阅 `progressListener` 并把工具调用计数 / 失败状态展示给用户
- [ ] `AgentTool.shouldDefer() == true`（`AgentTool.java:198-201`），确认 `Agent` 工具的 schema 只在 ToolSearch 选中时下发

## 3. 编译与测试

- [ ] `./gradlew build` 通过
- [ ] SubAgent 模块单测全部 PASS（loader / tool_filter / task_manager / fork 嵌套拒绝）

## 4. 端到端验证

- [ ] 注册路径：主装配点 register 完毕后，用户向主 Agent 发送 "spawn a plan agent to review X" → LLM 返回 `Agent` 工具调用 → `execute` → `runSync(spec=plan)` → 子 Agent 流式输出 → 控制台见 `Agent "..." completed in X.XXXs.`
- [ ] Fork 路径：用户在对话进行中说 "fork to investigate Y" → LLM 调用 `Agent` 不带 `subagent_type` → `runFork` → forked conversation 启动后台 task → 完成时 `TaskNotification` 通过 `drainNotifications` 注入下一轮
- [ ] 后台路径：调用带 `run_in_background=true` → 立即返回 `task_N` → 后台虚拟线程跑完 → 主 Agent 下一轮拿到完成通知
- [ ] 工具过滤验证：子 Agent 调 `Agent` 工具应直接被过滤掉（`ALWAYS_DISALLOWED` 命中），子 Agent 看不到 `Agent` 工具，从根源切断递归

## 5. 文档

- [ ] `docs/java/ch13/spec.md` 已写
- [ ] `docs/java/ch13/tasks.md` 已写，14 个 T 全部勾完
- [ ] `docs/java/ch13/checklist.md` 已写并逐项验收

---

## 6. 工具过滤细节验收

### 6.1 全局禁止集合 `ALWAYS_DISALLOWED`（7 项）

- [ ] `ToolFilter.java:30-33` 含七项：`TaskOutput / ExitPlanMode / EnterPlanMode / Agent / AskUserQuestion / TaskStop / Workflow`

### 6.2 异步白名单 `ASYNC_ALLOWED`（15 项）

- [ ] `ToolFilter.java:42-46` 含 15 项：`ReadFile / WebSearch / TodoWrite / Grep / WebFetch / Glob / Bash / EditFile / WriteFile / NotebookEdit / Skill / LoadSkill / SyntheticOutput / ToolSearch / EnterWorktree / ExitWorktree`（实际计 16 个名字，记 15 个槽位的扩展含义参照 Go 对照表）

### 6.3 In-process teammate 额外允许 `IN_PROCESS_TEAMMATE_ALLOWED`（8 项）

- [ ] `ToolFilter.java:49-52` 含 8 项：`TaskCreate / TaskGet / TaskList / TaskUpdate / SendMessage / CronCreate / CronDelete / CronList`
- [ ] `filterForAgent(source, spec, isAsync=true, isCustom=*, isInProcessTeammate=true)` 在异步白名单层额外放行 `Agent` 与上述 8 项

### 6.4 六层过滤顺序

- [ ] 第 1 层：`isMcpTool(name)`（`mcp__` 前缀）直接 register
- [ ] 第 2 层：`ALWAYS_DISALLOWED` 命中 continue
- [ ] 第 3 层：`isCustom && CUSTOM_AGENT_DISALLOWED.contains(name)` continue
- [ ] 第 4 层：`isAsync == true` 时，非 `ASYNC_ALLOWED` 工具一律 continue，除非 `isInProcessTeammate` 且命中 `Agent` 或 teammate 集合
- [ ] 第 5 层：`spec.disallowedTools()` 黑名单 continue
- [ ] 第 6 层：`spec.tools()` 白名单交集（`["*"]` 视为无白名单）

## 7. AgentLoader 验收

- [ ] `loadAll(projectRoot)` 顺序：builtin → `~/.codebot/agents` → `<projectRoot>/.codebot/agents`（`AgentLoader.java:39-53`）
- [ ] `LinkedHashMap` 保 put 覆盖语义，同名后注册胜出
- [ ] `parseAgentFile` 缺 `name` 抛 `Agent definition <path>: missing required field 'name'`
- [ ] `parseAgentFile` 缺 `description` 抛 `Agent definition <path>: missing required field 'description'`
- [ ] `parseAgentFile` 非法 `model` 抛 `Agent definition <path>: invalid model '<value>'`
- [ ] 解析失败的文件被 `loadDir` catch 后静默跳过，不影响其他文件加载
- [ ] body 为空时 `systemPromptOverride == null`，非空则等于 trimmed body

## 8. TaskManager 验收

- [ ] `createTask` 返回 `task_N`，`N` 从 `AtomicInteger.incrementAndGet()` 取（`SubAgentTaskManager.java:44-48`）
- [ ] `setRunning` 把 `Thread` 引用挂到 `TaskEntry.thread`
- [ ] `setCompleted` 把 `TaskNotification(id, name, COMPLETED, output)` 入队
- [ ] `setFailed` 把 `TaskNotification(id, name, FAILED, errMsg)` 入队
- [ ] `cancelTask` 仅在 `RUNNING` 状态生效，转 `CANCELLED` + `Thread.interrupt()` + 入队 `CANCELLED` 通知
- [ ] `drainNotifications` 返回拷贝并清空原列表
- [ ] 所有公共方法 `synchronized`
- [ ] `spawnSubAgent` 用 `Thread.startVirtualThread` 启动后台线程（`SubAgentTaskManager.java:117`）
- [ ] 事件循环超时 60s → `setFailed("Timeout")`；`InterruptedException` → `setFailed("Interrupted")`
- [ ] `LoopComplete` 时输出为空回退到 `"(agent produced no output)"`

## 9. AgentTool runSync 验收

- [ ] `maxIterations = spec.maxTurns() > 0 ? spec.maxTurns() : 200`（`AgentTool.java:315-316`）
- [ ] `isolation == "worktree"` 时 slug 形如 `agent-aXXXXXXX`（`SecureRandom` 4 字节 hex 取前 7）（`AgentTool.java:321-323`）
- [ ] worktree 创建失败返回 `Error creating agent worktree: <msg>`
- [ ] `LoopComplete` 后 `WorktreeChanges.hasChanges(path, headCommit)` 为真保留并附 `\n\nWorktree kept at <path> (branch <branch>) — has uncommitted changes or new commits.`；为假调 `AgentWorktree.remove`
- [ ] 最终 `ToolResult.success` 文案：`Agent "%s" completed in %d.%03ds.\n\n%s%s`

## 10. AgentTool 文案（Tool 接口可读性）

- [ ] `description()` 当 `agentSpecs` 非空时按 `AgentLoader.listNames` 字典序枚举可用 agent（`AgentTool.java:123-127`）
- [ ] `description()` 缺省提示三档 builtin（fallback 文案）
- [ ] `schema()` 的 `subagent_type.enum` 与 `description()` 列出的 agent 类型一致

## 11. 模型选择 `selectClient`

- [ ] `selectClient(specModel, overrideModel)` 优先取 `overrideModel`，其次 `specModel`，再次 fallback 到父 client（`AgentTool.java:489-501`）
- [ ] `model == "inherit" || model == ""` 直接返回父 client
- [ ] `modelResolver != null` 时调 `modelResolver.apply(model)`，结果 null 时 fallback 父 client
- [ ] `ModelResolver.ALIASES` 含 `haiku / sonnet / opus` 三个键（`src/main/java/com/codebot/llm/ModelResolver.java:7-11`）

## 12. 父子 Agent 联动（`com.codebot.agent.Agent`）

- [ ] `notificationFn` setter 存在（`Agent.java:46`）；主循环每轮开头通过 `notificationFn.get()` 抽取并 `addSystemReminder`（`Agent.java:79-83`）
- [ ] 子 Agent 复用同一套 `agentLoop`，由 `subAgent.run(conv)` 启动虚拟线程并返回 `BlockingQueue<AgentEvent>`（`Agent.java:50-60`）
- [ ] `setMaxIterations` 在 `runSync` / `spawnSubAgent` 内被显式设置

```



## ch14

```markdown
# 我的初步想法
- 用 Git 自带的多工作目录机制（同一仓库可挂多个工作目录，每个对应不同分支）作为隔离基础，目录统一放在仓库内部不被 Git 追踪的位置
- 目录名称走严格的安全校验：限制字符集、长度，拒绝 `.` 和 `..` 段，允许 `/` 作为嵌套分隔符（创建分支时再做平铺转换），防 LLM 输入触发路径遍历
- 完整生命周期管理：创建（含快速恢复——目录已存在时不调 git 子进程，纯文件系统读取 HEAD）、进入、退出、删除
- 创建后做环境初始化：复制本地配置（如 `settings.local.json`）、按主仓库 hooks 路径配置子目录的 git hooks、软链接大型依赖目录（依赖目录列表来自配置）、按规则复制被 gitignore 但运行需要的文件（best-effort）
- 切换工作目录时清理三类缓存（文件内容缓存、系统提示词/项目指令缓存、memory 文件缓存），防止 Agent 用旧目录的内容对新目录做决策
- 子 Agent 隔离模式：Agent 定义里通过字段声明隔离需求，进入流程自动建目录、在任务文本前注入路径翻译说明，完成后按变更情况自动判断保留还是清掉
- 退出时变更保护：有未提交修改或未推送 commit 时，默认拒绝删除目录，需显式确认丢弃；切回原目录后要重新加载主仓库的 hooks 配置
- 会话状态持久化到磁盘，支持进程意外退出后下次启动 `--resume` 恢复
- 后台周期性清理过期临时目录，三层过滤（命名模式 → 当前使用中/未过期 → fail-closed 的变更与未推送检查）
- 配套斜杠命令让用户手动管理目录（创建、列出、进入、退出、查看状态）
```

### Go

```markdown
# ch14: Worktree Spec

## 1. 背景

SubAgent 隔离了消息、权限、工具结果缓存，但所有子 Agent 仍然共享同一个工作目录——两个子 Agent 并发改同一个文件会互相覆盖。Git 分支不解决这个问题：分支只是时间维度的快照，同一时刻整个仓库仍然只有一份 working tree，切换分支会动所有文件的修改时间触发不必要的全量重编。多 Agent 并行要的是空间维度的隔离：同时存在多份独立的 working tree，每份对应不同分支，但共享同一个 `.git`。Git Worktree 提供的就是这个能力。这一章把它接进 CodeBot，让主 Agent 和每个子 Agent 都能拥有独立的文件视图。

## 2. 目标

把 worktree 做成两层 API：会话级让 LLM 通过工具自主进出 worktree，Agent 级让 SubAgent 通过 `isolation: "worktree"` 声明自动获得独立 worktree。底层共用一套创建/快速恢复路径和"创建后设置"管线（本地配置复制 / git hooks 配置 / 大目录软链接 / `.worktreeinclude` 文件复制）。叠加 fail-closed 变更检测（无变更才允许清掉、有变更默认保留）和孤儿 worktree 的后台过期清理，保证既不丢用户工作、又不让磁盘堆积。

## 3. 功能需求

- F1: worktree 名称（slug）安全校验：限定字符集、长度上限、按 `/` 切段、显式拒绝 `.` / `..` 段，校验失败给出分类错误（长度 / 段名非法 / 路径遍历）；任何 git 命令或路径拼接之前先跑。
- F2: slug 到路径和分支的映射：用一个 git 安全但不在 slug 字符集的字符替换 `/`，避免嵌套 slug 导致目录或分支命名冲突；分支统一加固定前缀，方便从 `git branch` 输出里识别 CodeBot 创建的。
- F3: 快速恢复路径：worktree 目录已存在时跳过 git 子进程，纯文件系统读 `.git` 指针 → `HEAD` ref → SHA，目标延迟 ≤ 10ms；任一步失败回退到完整创建路径。
- F4: git 子进程统一安全壳：所有 git 调用关闭终端密码提示、屏蔽 `GIT_ASKPASS`、丢弃 stdin，绝不挂起等待用户输入；失败返回结构化错误码而不是抛异常。
- F5: 创建/恢复主入口：先做 slug 校验和重名检查，命中已有目录走快速恢复（不重跑创建后设置），未命中按"已有远端 ref 优先 → fetch 兜底 → HEAD 兜底"的策略选 base branch，然后用大写 `-B` 创建 worktree（容忍上次未清干净的孤儿分支）。
- F6: 创建后设置四项：从主仓复制本地配置文件（`settings.local.json` 等）；按主仓 hooks 路径优先级（项目级 husky > 仓库 hooks）配置 worktree 的 `core.hooksPath`；按配置软链接大型依赖目录（node_modules / .venv / vendor 等）；按 `.worktreeinclude` gitignore 风格模式复制被 `.gitignore` 忽略但运行需要的文件（best-effort，单项失败不中断）。
- F7: 会话级 API 三件套：进入（创建 + 持久化 + 写全局单例）、Keep（清单例 + chdir 回原 cwd + 删持久化文件，保留 worktree 目录和分支）、Cleanup（同 Keep + `git worktree remove --force` + `git branch -D`）。
- F8: 会话持久化：单例序列化到仓库内固定位置（`.codebot/` 下），记原 cwd / worktree 路径 / 分支 / 原分支 / 原 HEAD commit / session ID 等；写空值等价于删持久化文件。
- F9: 启动恢复：TUI 启动时读持久化文件，验证 worktree 路径仍然存在，写回全局单例；不主动切 cwd（让用户或工具自行决定），不重跑创建后设置。
- F10: Agent 级 API：为每个声明 `isolation: "worktree"` 的子 Agent 创建独立 worktree，不动全局单例、不切进程 cwd、不写持久化；快速恢复路径要 bump worktree 目录的 mtime，防止被后台清理误判为孤儿。
- F11: SubAgent 集成：主 Agent 调 `Agent` 工具且隔离参数为 worktree 时，自动为子 Agent 创建独立 worktree、把子 Agent 工作目录指向 worktree 路径、在任务提示词最前面注入一段 notice 告诉子 Agent "你在隔离副本里、父路径要翻译为本地路径、编辑前重读文件"。
- F12: 子 Agent 完成后决策：检测有无变更（未提交修改或新 commit），无变更自动清理 worktree，有变更保留并在返回结果末尾附路径和分支名给主 Agent review。
- F13: 变更保护：会话级退出工具在 `action=remove` 且未显式声明丢弃时拒绝删除——脏 worktree 要 LLM 明确传 `discard_changes=true` 才能强删；具体变更数（uncommitted 文件数 + 未推送 commit 数）作为错误信息回吐给 LLM，单复数正确处理。
- F14: 变更检测 fail-closed：所有变更检查（git status / git rev-list）任何一步失败都按"有变更"处理，绝不在 git 命令失败时清掉用户工作。
- F15: LLM Tool 暴露：进入工具（input 仅可选 slug，已有 session 时拒绝）和退出工具（input `action` 必填 / `discard_changes` 可选，无 session 时拒绝）；两个工具标记为延迟工具（deferred），由主 Agent loop 在工具批次结束时统一执行，避免和别的工具同时操作目录。
- F16: 临时 worktree 命名模式：用前缀化的固定模式区分"自动产物"（子 Agent / 工作流 / 桥接器 / 任务 spawn 等来源各自有前缀）和"用户手动命名"；前缀正则集中维护，便于新增来源时统一加入。
- F17: 后台过期清理三层过滤：周期扫描 worktree 根目录，依次过滤——L1 命名模式（用户起名的永不删，廉价）→ L2 时态（跳过当前 session 占用的 + 近期活跃的）→ L3 git 状态 fail-closed（status 失败/非空跳过 + 未推送 commit 跳过）；删完跑 `git worktree prune` 同步 git 内部表。

## 4. 非功能需求

- N1: 全局 session 状态用读写锁保护，并发读不阻塞；Agent 级 API 完全无状态，天然并发安全。
- N2: 任何路径的 worktree 删除（会话级 Cleanup / Agent 级 Remove / 后台清理）都要先 chdir 离开 worktree（或保证当前不在 worktree 内），否则 `git worktree remove` 会失败。
- N3: `git worktree remove` 和 `git branch -D` 之间必须留出 git lockfile 释放时间（经验值 100ms），否则 branch 删除会偶发失败。
- N4: Agent 级 API 在快速恢复（worktree 目录已存在）时必须 bump worktree mtime，否则同一 worktree 被反复复用时会因为 mtime 太老被后台清理误删。
- N5: 三层过滤的执行顺序固定：先廉价的命名模式 → 再时态判断 → 最后贵的 git 检查；任何一层判定保留都立即 continue，不进入下一层。
- N6: 创建后设置的四项里软链接和 `.worktreeinclude` 复制是 best-effort——任何单项失败只跳过、不中断创建，保证主路径鲁棒。
- N7: 变更保护的错误信息必须包含具体数字（N 文件 + M commits）和分支名，让 LLM 能据此判断要不要强删；不能只回 "has changes" 这种空话。
- N8: worktree 子系统不假设统一日志层存在，所有创建/退出/清理的信息通过工具结果文本传达；这同时是给 LLM 的运行时反馈。

## 5. 设计概要

- 核心数据结构:
 - `WorktreeSession`：会话级全局单例，记录原 cwd / worktree 路径与名称 / worktree 分支 / 原分支 / 原 HEAD commit / session ID / 创建耗时；用于退出时还原状态和持久化。
 - `AgentWorktreeResult`：Agent 级 API 返回值，只含 worktree 路径 / 分支 / HEAD / 主仓根，不写全局状态。
 - `CreateResult`：底层创建/恢复入口的归一化结果，标记是否是快速恢复（决定是否跳过创建后设置）。
 - `ChangeSummary`：变更计数（修改文件数 + 未推送 commit 数），供变更保护错误信息生成。
 - 配置块：软链接目录列表 + 后台清理间隔 + 过期阈值，由 TUI 启动时注入，不注入走保守默认（间隔 0 = 后台清理停用、阈值 720 小时 = 30 天）。
- 主流程:
 - **会话级 Enter**：guard 已有 session → slug 校验 → 记录原 cwd 和原分支 → 创建/快速恢复 → 仅新创建走"创建后设置" → 写全局单例 + 持久化。
 - **会话级 Exit**：guard 无 session → 若 `action=remove` 且未声明丢弃则跑变更保护 → Keep（清单例 + chdir 回原 cwd + 删持久化）或 Cleanup（同 Keep + `git worktree remove --force` + sleep + `git branch -D`）。
 - **Agent 级隔离**：主 Agent 调 `Agent` 工具且隔离为 worktree → 生成临时 slug（带 `agent-` 前缀）→ 强制落主仓根 → 创建或快速恢复（恢复路径 bump mtime）→ 子 Agent 工作目录指向 worktree → 任务提示词前置注入 notice → 子 Agent 跑完后看有无变更 → 干净则清掉、脏则保留并把路径分支拼回结果。
 - **后台过期清理**：TUI 启动后台 goroutine → 按配置间隔周期扫 → 三层过滤 → 通过的删 worktree + 删分支 → 周期结束如有删除则跑一次 `git worktree prune`。
- 调用链（模块层级）:
 - TUI 启动 → 解析仓库根（穿透 commondir 到主仓）→ 注册两个 worktree 工具 → 读持久化文件并恢复 session → 启后台清理 goroutine。
 - LLM Enter/Exit → 工具 dispatcher → worktree 包会话级 API。
 - AgentTool → 看到 `isolation: worktree` → worktree 包 Agent 级 API → 子 Agent 跑完 → 变更检测 → Remove 或保留并拼路径。
- 与其他模块的交互:
 - 依赖 `internal/tools`（注册两个工具）、`internal/agents`（隔离分流）、`internal/tui`（启动装配 + cleanup 调度 + 配置注入）；底层只依赖 `os/exec`（git）+ 标准库（正则 / JSON / 文件系统 / crypto/rand）。
 - 不依赖 `internal/config` 的通用加载链路——worktree 配置当前由 TUI 启动时手动注入；也不依赖 `internal/memory` / `internal/prompt` / `internal/session`。

## 6. Out of Scope

- 不实现非 git VCS 适配（hg / jj / sapling 等），所有 worktree 操作 hardcode 走 git 子命令
- 不实现 sparse checkout / partial clone 优化，大型 mono-repo 优化推到后续
- 不实现 `--worktree` / `--worktree --tmux` CLI 启动快速路径（涉及 tmux/iTerm2 子系统，留给 ch15）
- 不实现 PR fetch 或 pull request 头引用解析（远端协作场景）
- 不实现 prepare-commit-msg hook 注入 commit attribution（商业 feature 场景）
- 不实现 ReadFile / Memory / SystemPrompt 缓存清理 hook（CodeBot 当前没有这几类缓存）
- 不引入第三方 gitignore 库（自实现简化匹配够用）
- 团队成员（teammate）路径的 worktree 自动清理推到 ch15 收尾，本章 teammate 路径只创建并隔离、不负责清理

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch14: Worktree Tasks

> 任务粒度：每个任务可在一次会话内完成，可独立交付。

## T1: 实现 Slug 校验 + 命名映射
- 影响文件: `internal/worktree/validate.go`（`MaxWorktreeSlugLength` @ 11；`validWorktreeSlugSegment` @ 16；`ValidateWorktreeSlug` @ 32-58；`FlattenSlug` @ 73-78；`WorktreeBranchName` @ 80-82）
- 依赖任务: 无
- 完成标准: `ValidateWorktreeSlug` 校验长度 ≤ 64、按 `/` 切段、每段匹配 `^[a-zA-Z0-9._-]+$`、显式拒绝 `.` / `..` 段，错误分类（长度 / 非法段 / 路径遍历）；`FlattenSlug(s) = strings.ReplaceAll(s, "/", "+")`；`WorktreeBranchName(s) = "worktree-" + FlattenSlug(s)`。
- [ ] 完成

## T2: 实现 Git 纯文件系统读取
- 影响文件: `internal/worktree/filesystem.go`（`IsSafeRefName` @ 30；`IsValidGitSha` @ 52；`ResolveGitDir` @ 64；`GetCommonDir` @ 100；`readGitHead` @ 135；`ResolveRef` @ 180；`resolveRefInDir` @ 200；`ReadRawSymref` @ 257；`GetDefaultBranch` @ 286；`GetCurrentBranch` @ 325；`ReadWorktreeHeadSha` @ 347-377）
- 依赖任务: 无
- 完成标准: `ReadWorktreeHeadSha` 完整链路（`.git pointer → gitdir → HEAD → ResolveRef`），任一步失败返回 `("", nil)` 让调用方走完整路径；`resolveRefInDir` 含 loose ref + packed-refs fallback；`GetDefaultBranch` 读 `refs/remotes/origin/HEAD` symref，回退 `main` → `master`，默认 "main"；附 `IsSafeRefName` / `IsValidGitSha` 防 ref 文件被篡改后注入 shell；目标延迟 ≤ 10ms（不起 git 子进程）。
- [ ] 完成

## T3: 实现 Git 子进程安全壳
- 影响文件: `internal/worktree/env.go`（`gitNoPromptEnv` @ 21-30；`runGit` @ 32-51）
- 依赖任务: 无
- 完成标准: `gitNoPromptEnv()` 在 `os.Environ()` 后追加 `GIT_TERMINAL_PROMPT=0` 和 `GIT_ASKPASS=`，所有 git 调用统一用；`runGit(ctx, dir, args...)` 强制 `Env=gitNoPromptEnv()` + `Stdin=nil`，never throw，返回 `(stdout, stderr, code)` 三元组，进程未起来时 code=-1。
- [ ] 完成

## T4: 实现创建/恢复主入口
- 影响文件: `internal/worktree/create.go`（`WorktreesDir` @ 14；`WorktreePathFor` @ 21；`CreateResult` @ 31；`getOrCreateWorktree` @ 56-131）
- 依赖任务: T1, T2, T3
- 完成标准: `WorktreesDir(root) = <root>/.codebot/worktrees`；`WorktreePathFor(root, slug) = WorktreesDir + FlattenSlug(slug)`；`getOrCreateWorktree` 命中已存在目录走快速恢复 → `ReadWorktreeHeadSha` → 返回 `Existed=true`，**不**跑创建后设置；未命中走 `os.MkdirAll(WorktreesDir, 0o755) → GetDefaultBranch → ResolveRef("refs/remotes/origin/<default>")`：命中则 `baseBranch="origin/<default>"`（省 fetch），未命中 `runGit("fetch","origin",<default>)`，成功用 `origin/<default>` 否则回退 `HEAD`；最后 `git worktree add -B worktree-<flat> <path> <baseBranch>`（大写 `-B` 容忍上次未清的孤儿分支）。
- [ ] 完成

## T5: 实现创建后设置四项 + 配置块
- 影响文件: `internal/worktree/setup.go`（`performPostCreationSetup` @ 14-26；`copySettingsLocal` @ 31-44；`configureHooksPath` @ 49-70；`symlinkDirectories` @ 75-85；`getSymlinkDirectories` @ 90-92；`CopyWorktreeIncludeFiles` @ 97-155；`matchesWorktreeInclude` @ 160-182；`copyFileContents` @ 184-199；`worktreeConfig` @ 201-208；`SetWorktreeConfig` @ 210-217；`GetStaleCutoffHours / GetStaleCleanupInterval` @ 219-228；`FindCanonicalGitRoot` @ 231-244）
- 依赖任务: T3
- 完成标准: 四项依次执行 — **A** `copySettingsLocal` 复制 `<repo>/.codebot/settings.local.json`（ENOENT 静默）；**B** `configureHooksPath` 优先 `<repo>/.husky` 回退 `<repo>/.git/hooks`，找到第一个存在的目录后在 worktree 目录里跑 `git config core.hooksPath <hooksPath>`；**C** `symlinkDirectories` 从 `worktreeConfig.SymlinkDirectories` 读列表，跳过含 `..` 项，逐个 `os.Symlink(src, dst)`，错误静默；**D** `CopyWorktreeIncludeFiles` 读 `<repo>/.worktreeinclude`（按行收集 patterns，跳空行和 `#`）→ `git ls-files --others --ignored --exclude-standard --directory` 列出 gitignored → `matchesWorktreeInclude`（支持 exact/basename/glob/dir prefix）筛选 → 命中的 `os.MkdirAll(Dir(dst), 0o755) + copyFileContents`，单文件失败 `continue` 不中断；`worktreeConfig` 包级私有，默认 `StaleCutoffHours=720`（30 天）；`FindCanonicalGitRoot` 解 `.git` → 跟随 `commondir` → 返回主仓 root。
- [ ] 完成

## T6: 实现变更检测 fail-closed
- 影响文件: `internal/worktree/changes.go`（`ChangeSummary` @ 11；`HasWorktreeChanges` @ 19-37；`CountWorktreeChanges` @ 43-74）
- 依赖任务: T3
- 完成标准: `HasWorktreeChanges` 返 bool — `git status --porcelain` 非零或非空 → true；`git rev-list --count <headCommit>..HEAD` 非零或解析失败或 > 0 → true；都干净 → false（**git 失败默认 true，fail-closed**）。`CountWorktreeChanges` 返 `*ChangeSummary` — status 失败返 nil；`originalHeadCommit==""` 即使 status 成功也返 nil（hook-based 场景）；`rev-list --count` 失败返 nil；其余返 `&{ChangedFiles, Commits}`。
- [ ] 完成

## T7: 实现 SubAgent worktree 上下文 notice
- 影响文件: `internal/worktree/notice.go`（`BuildWorktreeNotice` @ 9-19）
- 依赖任务: 无
- 完成标准: 返回固定模板英文文本，包含 `parent_cwd` / `worktree_cwd` 占位 + 关键句"running in an isolated git worktree"、"translate paths"、"re-read files before editing"、"your edits will not affect the parent agent"；在子 Agent 任务文本最前面拼接（不替换原 prompt）。
- [ ] 完成

## T8: 实现会话级 API + 持久化
- 影响文件: `internal/worktree/session.go`（`WorktreeSession` @ 14-25；`sessionMu / currentWorktreeSession` @ 27-32；`GetCurrentWorktreeSession` @ 34；`RestoreWorktreeSession` @ 43；`sessionFilePath` @ 50；`SaveWorktreeSession` @ 56-72；`LoadWorktreeSession` @ 74-91；`CreateWorktreeForSession` @ 93-134；`KeepWorktree` @ 138-154；`CleanupWorktree` @ 158-188）
- 依赖任务: T1, T4, T5, T6
- 完成标准: `WorktreeSession` 9 字段（`OriginalCwd / WorktreePath / WorktreeName / WorktreeBranch / OriginalBranch / OriginalHeadCommit / SessionID / HookBased / CreationDurationMs`）；包级 `currentWorktreeSession` + `sessionMu sync.RWMutex`；`CreateWorktreeForSession`：`ValidateWorktreeSlug → os.Getwd 记 originalCwd → GetCurrentBranch 拿 originalBranch → getOrCreateWorktree → 仅 !Existed 跑 performPostCreationSetup 并测 ms → 组装 session → 写全局 + SaveWorktreeSession`；`KeepWorktree`：原子读取并清空全局单例 → `os.Chdir(session.OriginalCwd)` → `SaveWorktreeSession(repo, nil)` 删持久化文件（不删目录、不删分支）；`CleanupWorktree`：同 keep 流程 + 从 `OriginalCwd` 跑 `git worktree remove --force <wtPath>` → `time.Sleep(100ms)` 等 lockfile → `git branch -D <wtBranch>` → 删持久化（git 失败 best-effort 不中断）；持久化路径 `<repo>/.codebot/worktree_session.json`，session=nil 时删文件。
- [ ] 完成

## T9: 实现 Agent 级 API
- 影响文件: `internal/worktree/agent.go`（`AgentWorktreeResult` @ 11；`CreateAgentWorktree` @ 22-53；`RemoveAgentWorktree` @ 57-73）
- 依赖任务: T1, T4, T5
- 完成标准: `CreateAgentWorktree(ctx, slug)`：`ValidateWorktreeSlug → os.Getwd + FindCanonicalGitRoot 强制落主仓 → getOrCreateWorktree → !Existed 跑 setup`；`Existed` 时 `os.Chtimes(wtPath, now, now)` bump mtime 防被 cleanup 误判；**不动全局单例、不切进程 cwd、不写持久化**；返回 `AgentWorktreeResult{WorktreePath, WorktreeBranch, HeadCommit, GitRoot}`。`RemoveAgentWorktree(ctx, wtPath, wtBranch, gitRoot)`：从 `gitRoot`（**不**从 wtPath，否则会把自己删掉）跑 `git worktree remove --force` → 成功后 `time.Sleep(100ms)` → 分支非空时 `git branch -D <wtBranch>` → 返回 worktree 删除是否成功。
- [ ] 完成

## T10: 实现 EnterWorktreeTool
- 影响文件: `internal/tools/enter_worktree.go`（`EnterWorktreeTool` @ 15-18；`Name / Category / Description / ShouldDefer` @ 20-27；`Schema` @ 29-43；`Execute` @ 45-87；`generateWorktreeSlug` @ 89-93）
- 依赖任务: T8
- 完成标准: `Name="EnterWorktree"`、`Category=CategoryCommand`、`ShouldDefer=true`（进 deferred 工具队列）；input schema 仅可选 `name: string`（按 slug 字符集约束）；`Execute` guard：`GetCurrentWorktreeSession() != nil` → 拒绝 `"Already in a worktree session"`；`name==""` 时 `generateWorktreeSlug()` 生成 `wt-<8hex>`；`RepoRoot==""` 报 `"Error: not in a git repository"`；成功调 `CreateWorktreeForSession(t.SessionID, slug, t.RepoRoot)`，返回 `"Created worktree at <path> on branch <branch>. The session is now working in the worktree. Use ExitWorktree to leave mid-session, or exit the session to be prompted."`。
- [ ] 完成

## T11: 实现 ExitWorktreeTool
- 影响文件: `internal/tools/exit_worktree.go`（`ExitWorktreeTool` @ 15-17；`Name / Category / Description / ShouldDefer` @ 19-26；`Schema` @ 28-48；`Execute` @ 50-end）
- 依赖任务: T6, T8
- 完成标准: `Name="ExitWorktree"`、`ShouldDefer=true`；input schema `action: enum["keep","remove"]`（required）+ `discard_changes?: bool`；`Execute` scope guard：`GetCurrentWorktreeSession() == nil` → 拒绝 `"No-op: there is no active EnterWorktree session to exit. This tool only operates on worktrees created by EnterWorktree in the current session — it will not touch worktrees created manually or in a previous session. No filesystem changes were made."`；变更保护：`action=="remove" && !discard_changes` 时 `CountWorktreeChanges`：nil 报 `"Could not verify worktree state at <path>..."`，非零报 `"Worktree has N uncommitted file(s) and M commit(s) on <branch>. Removing will discard this work permanently. Set discard_changes=true to force."`（**单复数 file/files、commit/commits 正确处理**）；分支：`action=="keep"` 调 `KeepWorktree`；`action=="remove"` 调 `CleanupWorktree`；返回成功消息。
- [ ] 完成

## T12: 接入 SubAgent isolation
- 影响文件: `internal/agents/agent_tool.go`（`isolation` schema @ 163-167；`cwd` schema @ 177-180；`Execute` 解析 + 互斥 @ 217-223；`runSync` worktree 分支 @ 298-319；完成后决策 @ 391-398；`runAsTeammate` worktree 分支 @ 656-670；`generateAgentSlug` @ 739-745）
- 依赖任务: T6, T7, T9
- 完成标准: `Agent` 工具 schema 含 `isolation: enum["worktree"]` + `cwd: string`；`Execute` 在 `cwdOverride != "" && isolation == "worktree"` 时返回 `"Error: cwd and isolation: 'worktree' are mutually exclusive"`；`generateAgentSlug(description)` 生成 `agent-a<7hex>`（匹配 cleanup 正则 `^agent-a[0-9a-f]{7}$`）；`runSync` 在 `isolation == "worktree"` 时：`worktree.CreateAgentWorktree(ctx, slug)` → `subAgent.WorkDir = wtResult.WorktreePath` → `notice := worktree.BuildWorktreeNotice(parentCwd, wtResult.WorktreePath)` → `prompt = notice + "\n\n" + prompt`；子 Agent 完成后 `worktree.HasWorktreeChanges` → 干净 → `worktree.RemoveAgentWorktree(ctx, wtPath, wtBranch, gitRoot)`；脏 → `result += "\n\nWorktree kept at <path> (branch <branch>) — has uncommitted changes or new commits."`；`runAsTeammate` 同样三步（创建 + WorkDir + notice），但**不**做完成后自动清理（teammate 长生命周期，留给 ch15 收尾）。
- [ ] 完成

## T13: 实现后台过期清理
- 影响文件: `internal/worktree/cleanup.go`（`ephemeralWorktreePatterns` @ 14-20；`isEphemeralSlug` @ 22-30；`CleanupStaleAgentWorktrees` @ 39-105；`StartCleanupLoop` @ 110-130）
- 依赖任务: T5, T6, T9
- 完成标准: 五个临时命名正则：`^agent-a[0-9a-f]{7}$` / `^wf_[0-9a-f]{8}-[0-9a-f]{3}-\d+$` / `^wf-\d+$` / `^bridge-[A-Za-z0-9_]+(-[A-Za-z0-9_]+)*$` / `^job-[a-zA-Z0-9._-]{1,55}-[0-9a-f]{8}$`；`isEphemeralSlug` 任一匹配返 true；`CleanupStaleAgentWorktrees(ctx, cutoffDate)` → `FindCanonicalGitRoot(Getwd)` → `os.ReadDir(WorktreesDir)`；三层过滤 — **L1 命名**：`isEphemeralSlug` false 跳（用户命名永不删）；**L2 时态**：当前 session.WorktreePath 跳 + `info.ModTime().After(cutoffDate)` 跳；**L3 git 状态 fail-closed**：`git --no-optional-locks status --porcelain -uno` 非零或非空跳 + `git rev-list --max-count=1 HEAD --not --remotes` 非零或非空（未推送 commit）跳；三层都通过的 `RemoveAgentWorktree`；末尾若有删除 → `git worktree prune` 同步 git 内部表；返回清理数量。`StartCleanupLoop(ctx)`：`GetStaleCleanupInterval() <= 0` 直接 return；否则起 goroutine 每 `interval` 秒跑一次 `CleanupStaleAgentWorktrees(now - cutoffHours*Hour)`；ctx 取消时退出。
- [ ] 完成

## T14: 接入 TUI 启动装配
- 影响文件: `internal/tui/tui.go`（`worktree` import @ 33；装配段 @ 619-639）
- 依赖任务: T8, T10, T11, T13
- 完成标准:
 1. `gitRoot := worktree.FindCanonicalGitRoot(wd)` 算规范仓库根（穿透 commondir 到主仓）；
 2. `m.registry.Register(&tools.EnterWorktreeTool{SessionID: m.sessionID, RepoRoot: gitRoot})` + `m.registry.Register(&tools.ExitWorktreeTool{RepoRoot: gitRoot})` 两个工具注册；
 3. `worktree.LoadWorktreeSession(gitRoot)` 非 nil 且 `WorktreePath` 还存在（stat 验证）时 `worktree.RestoreWorktreeSession(s)` 写回全局；
 4. `worktree.StartCleanupLoop(context.Background())` 起后台清理 goroutine。
- [ ] 完成

## T15: 端到端验证
- 影响文件: 无（仅运行）
- 依赖任务: T1-T14
- 完成标准:
 - `go build ./...` 通过（无输出）；
 - `go test ./internal/worktree/...` 通过（10 个 _test.go 全 PASS）；
 - `go test ./internal/agents/...` 通过（含 isolation 集成）；
 - **路径 A — 工具直接驱动**：主 Agent 调 `EnterWorktree({name:"demo"})` 创建 worktree → 在 worktree 里 `WriteFile + Bash("git commit ...")` → `ExitWorktree({action:"remove"})` 被变更保护拒绝并列出具体数 → `ExitWorktree({action:"remove", discard_changes:true})` 强删成功；
 - **路径 B — 子 Agent 自动隔离**：主 Agent 在主目录 `WriteFile witness.txt = "original content from main agent"` → 调 `Agent({subagent_type:"general-purpose", isolation:"worktree", description:"...", prompt:"把 witness.txt 改成 ..."})` → 验证主目录 `witness.txt` 内容不变；`.codebot/worktrees/agent-*/witness.txt` 是修改后版本；若有 commit → 结果末尾出现 `"Worktree kept at ... (branch worktree-agent-a...) — has uncommitted changes or new commits."`。
- [ ] 完成

## 进度
- [ ] T1 / [ ] T2 / [ ] T3 / [ ] T4 / [ ] T5 / [ ] T6 / [ ] T7 / [ ] T8 / [ ] T9 / [ ] T10 / [ ] T11 / [ ] T12 / [ ] T13 / [ ] T14 / [ ] T15

```

```markdown
# ch14: Worktree Checklist

> 所有条目可勾选、可观测。验收方式写在条目后面括号中。验收：已通过验证的项均勾选。

## 1. 实现完整性

- [ ] 常量 `MaxWorktreeSlugLength = 64` 在 `internal/worktree/validate.go:11` 定义
- [ ] 函数 `ValidateWorktreeSlug` 在 `internal/worktree/validate.go:32-58` 含长度 + 段名 + `.` / `..` 三类错误分类
- [ ] 函数 `FlattenSlug` 在 `internal/worktree/validate.go:73` 把 `/` 替换成 `+`；`WorktreeBranchName` 在 `:80` 加 `worktree-` 前缀
- [ ] 函数 `ReadWorktreeHeadSha` 在 `internal/worktree/filesystem.go:347-377` 完整链路（`.git pointer → gitdir → HEAD → ResolveRef`），失败返回 `("", nil)` 不抛错
- [ ] 函数 `resolveRefInDir` 在 `internal/worktree/filesystem.go:200` 含 loose ref + packed-refs fallback
- [ ] 函数 `GetDefaultBranch` 在 `internal/worktree/filesystem.go:286` 读 `refs/remotes/origin/HEAD` symref 并回退 main → master
- [ ] 函数 `runGit` 在 `internal/worktree/env.go:32-51` 强制 `Env=gitNoPromptEnv() + Stdin=nil`，never throw
- [ ] 类型 `CreateResult` 在 `internal/worktree/create.go:31` 含 `Existed` 标记快速恢复
- [ ] 函数 `getOrCreateWorktree` 在 `internal/worktree/create.go:56-131` 实现"快速恢复 → 创建路径"二选一，创建路径走 `origin/<default> → fetch → HEAD` 三段策略，最后 `git worktree add -B`（大写 `-B`）
- [ ] 函数 `performPostCreationSetup` 在 `internal/worktree/setup.go:14-26` 依序调四项 A/B/C/D
- [ ] 函数 `CopyWorktreeIncludeFiles` 在 `internal/worktree/setup.go:97-155` 单文件失败 `continue` 不中断
- [ ] 函数 `FindCanonicalGitRoot` 在 `internal/worktree/setup.go:231-244` 跟随 `.git/commondir` 解析主仓根
- [ ] 类型 `WorktreeSession` 在 `internal/worktree/session.go:14-25` 含 9 个字段（OriginalCwd / WorktreePath / WorktreeName / WorktreeBranch / OriginalBranch / OriginalHeadCommit / SessionID / HookBased / CreationDurationMs）
- [ ] 模块级 `currentWorktreeSession` + `sessionMu sync.RWMutex` 在 `internal/worktree/session.go:27-32`
- [ ] 函数 `CreateWorktreeForSession` 在 `internal/worktree/session.go:93-134` 仅 `!Existed` 时跑 setup 并测 `CreationDurationMs`
- [ ] 函数 `CleanupWorktree` 在 `internal/worktree/session.go:158-188` 含 `time.Sleep(100ms)` 等 git lockfile 释放（在 `git worktree remove --force` 和 `git branch -D` 之间）
- [ ] 类型 `AgentWorktreeResult` 在 `internal/worktree/agent.go:11` 不含 SessionID（不写全局单例）
- [ ] 函数 `CreateAgentWorktree` 在 `internal/worktree/agent.go:22-53` 在 `Existed` 时 `os.Chtimes` bump mtime
- [ ] 函数 `RemoveAgentWorktree` 在 `internal/worktree/agent.go:57-73` 从 `gitRoot` 跑 git 子进程（不是 wtPath，否则把自己删掉）
- [ ] 函数 `HasWorktreeChanges` 在 `internal/worktree/changes.go:19-37` git 失败返回 true（fail-closed）
- [ ] 函数 `CountWorktreeChanges` 在 `internal/worktree/changes.go:43-74` 失败返回 nil（让调用方报具体错误文本）
- [ ] 函数 `BuildWorktreeNotice` 在 `internal/worktree/notice.go:9-19` 模板包含 `parent_cwd` / `worktree_cwd` 占位 + "re-read files before editing" 关键句
- [ ] 变量 `ephemeralWorktreePatterns` 在 `internal/worktree/cleanup.go:14-20` 含五个正则（agent-a / wf_ / wf- / bridge- / job-）
- [ ] 函数 `CleanupStaleAgentWorktrees` 在 `internal/worktree/cleanup.go:39-105` 三层过滤顺序固定（L1 命名 → L2 时态 → L3 git 状态）
- [ ] 函数 `StartCleanupLoop` 在 `internal/worktree/cleanup.go:110-130interval <= 0` 直接 return
- [ ] 类型 `EnterWorktreeTool` 在 `internal/tools/enter_worktree.go:15` 含 `SessionID` 和 `RepoRoot` 字段，`ShouldDefer` 返回 true
- [ ] 类型 `ExitWorktreeTool` 在 `internal/tools/exit_worktree.go:15` 含 `RepoRoot` 字段，`ShouldDefer` 返回 true
- [ ] `ExitWorktreeTool` schema 在 `internal/tools/exit_worktree.go:28-48` 含 `action: enum["keep","remove"]`（required）+ `discard_changes?: bool`
- [ ] 函数 `generateAgentSlug` 在 `internal/agents/agent_tool.go:741` 生成 `agent-a<7hex>` 匹配 cleanup 正则

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `grep -rn "EnterWorktreeTool" --include="*.go" .` 在 `internal/tui/tui.go:621` 找到注册调用
- [ ] `grep -rn "ExitWorktreeTool" --include="*.go" .` 在 `internal/tui/tui.go:625` 找到注册调用
- [ ] `grep -rn "FindCanonicalGitRoot" --include="*.go" .` 至少命中 TUI 启动（`tui.go:620`）+ Agent API（`agent.go:25 附近`）
- [ ] `grep -rn "LoadWorktreeSession" --include="*.go" .` 在 `internal/tui/tui.go:631` 找到调用方
- [ ] `grep -rn "RestoreWorktreeSession" --include="*.go" .` 在 `internal/tui/tui.go:633` 找到调用方
- [ ] `grep -rn "StartCleanupLoop" --include="*.go" .` 在 `internal/tui/tui.go:639` 找到调用方
- [ ] `grep -rn "CreateAgentWorktree" --include="*.go" .` 在 `internal/agents/agent_tool.go:304` 和 `:659` 找到两处调用（runSync + runAsTeammate）
- [ ] `grep -rn "BuildWorktreeNotice" --include="*.go" .` 同上两处调用（runSync 在 `:315`，runAsTeammate 在 `:668`）
- [ ] `grep -rn "HasWorktreeChanges" --include="*.go" .` 在 `internal/agents/agent_tool.go:392` 找到主流程调用方（决定 Remove 还是保留）
- [ ] `grep -rn "RemoveAgentWorktree" --include="*.go" .` 在 `internal/agents/agent_tool.go:396` 和 `internal/worktree/cleanup.go` 找到调用方
- [ ] `grep -rn "CountWorktreeChanges" --include="*.go" .` 在 `internal/tools/exit_worktree.go` 找到唯一调用方（用于变更保护错误信息）
- [ ] `grep -rn "SetWorktreeConfig" --include="*.go" .` 找到 setter 定义在 `internal/worktree/setup.go:210`（注意：当前未在 TUI 启动时注入，`StaleCleanupInterval` 默认 0，后台清理默认不跑）

## 3. 编译与测试

- [ ] `go build ./...` 通过（无输出）
- [ ] `go test ./internal/worktree/...` 通过（10 个 _test.go 全 PASS：`validate_test.go` / `filesystem_test.go` / `env_test.go` / `create_test.go` / `setup_test.go` / `session_test.go` / `agent_test.go` / `changes_test.go` / `notice_test.go` / `cleanup_test.go`）
- [ ] `go test ./internal/agents/...` 通过（含 isolation 集成）
- [ ] `go vet ./...` 无新增警告

## 4. 端到端验证

- [ ] **路径 A — 工具直接驱动**：用户对主 Agent 说"用 EnterWorktree 工具创建一个名叫 demo 的工作树" → LLM 调 `EnterWorktree({name:"demo"})` → 返回 `Created worktree at .../.codebot/worktrees/demo on branch worktree-demo`；让 Agent 在 worktree 里创建 `hello.txt` 并 `git commit`；让 Agent 调 `ExitWorktree({action:"remove"})` → 因有未推送 commit 被变更保护拒绝，错误文本包含具体 file/commit 数和分支名；`ExitWorktree({action:"remove", discard_changes:true})` 强删成功；`ls .codebot/worktrees/` 看到 `demo/` 已消失。
- [ ] **路径 B — 子 Agent 自动隔离**：用户让主 Agent 在主目录建 `witness.txt`（内容 "original content from main agent"）→ 调 `Agent({subagent_type:"general-purpose", isolation:"worktree", description:"...", prompt:"把 witness.txt 改成 \"modified by isolated worker\"，然后 git 提交"})`；验证 `cat witness.txt` 主目录内容仍是 "original ..."；`cat .codebot/worktrees/agent-*/witness.txt` 是修改后版本；若子 Agent 有 commit → 结果末尾出现 `"Worktree kept at ... (branch worktree-agent-a...) — has uncommitted changes or new commits."`；若无修改 → worktree 自动清理（`.codebot/worktrees/` 下 `agent-*` 目录消失）。
- [ ] **持久化与 crash 恢复**：TUI 里 `EnterWorktree({name:"crashtest"})` 创建 worktree → `Ctrl+C` 杀 TUI 进程 → `cat .codebot/worktree_session.json` 文件仍在并含 crashtest 会话；重启 TUI → 启动期间 `LoadWorktreeSession + RestoreWorktreeSession` 将 session 写回全局；下一次工具调用时 `GetCurrentWorktreeSession()` 非 nil。
- [ ] **变更保护单复数**：在 worktree 里建 1 个未提交修改 → `ExitWorktree({action:"remove"})` 返回 `"1 uncommitted file"`；建 2+ 个修改 → 返回 `"N uncommitted files"`（注意单复数）；同样验证 commit 数的单复数。
- [ ] **后台清理保守不删**：手动在 `.codebot/worktrees/agent-aabcdef1/` 下建一个有未推送 commit 的目录（mtime 设为过期前）→ 等 cleanup loop 跑一轮（或手动调 `CleanupStaleAgentWorktrees` 测试）→ 该目录仍保留（L3 fail-closed 拦住）。
- [ ] **互斥校验**：`Agent({subagent_type:"general-purpose", cwd:"/tmp/x", isolation:"worktree", ...})` 返回 `"Error: cwd and isolation: 'worktree' are mutually exclusive"`。

## 5. 文档

- [ ] `specs/go/ch14/spec.md` 已按 ch13 风格重写（F1-F17 + N1-N8，无 file:line 代码标注）
- [ ] `specs/go/ch14/tasks.md` 已写，15 个 T 全部勾完（T1-T15）
- [ ] `specs/go/ch14/checklist.md` 已写并逐项验收
- [ ] commit 信息标注 `ch14`，新增代码的调用链已在 PR 描述或 commit message 里说明

```

### Python

```markdown
# ch14: Worktree Spec

## 1. 背景

SubAgent 隔离了消息、权限、工具结果缓存，但所有子 Agent 仍然共享同一个工作目录——两个子 Agent 并发改同一个文件会互相覆盖。Git 分支不解决这个问题：分支只是时间维度的快照，同一时刻整个仓库仍然只有一份 working tree，切换分支会动所有文件的修改时间触发不必要的全量重编。多 Agent 并行要的是空间维度的隔离：同时存在多份独立的 working tree，每份对应不同分支，但共享同一个 `.git`。Git Worktree 提供的就是这个能力。这一章把它接进 CodeBot，让主 Agent 和每个子 Agent 都能拥有独立的文件视图。

## 2. 目标

把 worktree 做成两层 API：会话级让 LLM 通过 `EnterWorktree` / `ExitWorktree` 工具自主进出 worktree，Agent 级让 SubAgent 通过 `isolation: "worktree"` 声明自动获得独立 worktree。底层共用一个 `WorktreeManager` 提供创建/快速恢复路径和"创建后设置"管线（本地配置复制 / git hooks 配置 / 大目录软链接 / `.worktreeinclude` 文件复制）。叠加 fail-closed 变更检测（无变更才允许清掉、有变更默认保留）和孤儿 worktree 的后台过期清理 task，保证既不丢用户工作、又不让磁盘堆积。

## 3. 功能需求

- F1: worktree 名称（slug）安全校验：限定字符集 `^[a-zA-Z0-9._-]+$`、长度上限 64、按 `/` 切段、显式拒绝 `.` / `..` 段和空段，校验失败返回带原因的错误字符串；任何 git 命令或路径拼接之前先跑。
- F2: slug 到路径和分支的映射：`flatten_slug` 把 `/` 替换为 `+`，避免嵌套 slug 导致目录或分支命名冲突（Git D/F conflict）；分支统一加 `worktree-` 前缀，方便从 `git branch` 输出里识别 CodeBot 创建的。
- F3: 快速恢复路径：worktree 目录已存在时 `read_worktree_head_sha` 纯文件系统读 `.git` 指针 → `gitdir` → `commondir` → `HEAD` → loose ref / packed-refs，跳过 git 子进程；任一步失败返回 `None`，调用方回退到完整创建路径。
- F4: git 子进程统一安全壳：所有 git 调用关闭终端密码提示（`GIT_TERMINAL_PROMPT=0`）、屏蔽 `GIT_ASKPASS=""`、`stdin=subprocess.DEVNULL`，绝不挂起等待用户输入；统一 `timeout=60`，失败返回 `CompletedProcess` 而不是抛异常。
- F5: 创建/恢复主入口 `WorktreeManager.create`：先做 slug 校验和 `active` 字典重名检查，命中已存在目录走快速恢复（不重跑创建后设置），未命中 `os.makedirs(worktree_dir, exist_ok=True)` → `git worktree add -B worktree-<flat> <path> <base_branch>`（大写 `-B` 容忍上次未清的孤儿分支），默认 `base_branch="HEAD"`。
- F6: 创建后设置四项 `perform_post_creation_setup`：依次执行 — A `_copy_local_configs` 复制 `LOCAL_CONFIG_FILES` 里列出的 `settings.local.json` / `.env`（不存在静默跳过）；B `_setup_git_hooks` 优先 `<repo>/.husky` 回退 `<repo>/.git/hooks`，找到目录后在 worktree 里跑 `git config core.hooksPath`；C `_create_symlinks` 从 `WorktreeManager.symlink_directories` 读列表，逐个 `os.symlink(src, dst)`，错误日志吞掉不抛；D `_copy_ignored_files` 读 `<repo>/.worktreeinclude`（跳空行和 `#`）→ `git ls-files --others --ignored --exclude-standard --directory` 列出 gitignored → `fnmatch` 筛选 → 命中的 `shutil.copy2`。
- F7: 会话级 API 三件套：`create`（先快速恢复，未命中走 git add + 创建后设置）、`enter`（清缓存 + 记 `original_cwd` / `original_branch` / `original_head_commit` + 写 `current_session` + 持久化）、`exit`（变更保护 + 清缓存 + 清单例 + 删持久化，`action="remove"` 时调 `_remove_worktree`）。
- F8: 会话持久化：`save_worktree_session` 把 `WorktreeSession` 7 字段 dump 成 `<repo>/.codebot/worktree_session.json`；`session=None` 时写 `"{}"`（等价清空）；`load_worktree_session` 容忍文件缺失、JSON 损坏、空 dict、缺字段全部返 `None` 并 warning 日志。
- F9: 启动恢复：`WorktreeManager.restore_session` 读持久化文件 → `read_worktree_head_sha` 验证 worktree 路径仍然存在 → 命中时把 `Worktree` 写回 `active` 字典 + `current_session`；HEAD SHA 读不到则反向调用 `save_worktree_session(None)` 清掉脏文件。
- F10: 自动清理 API `auto_cleanup(name, head_commit)`：调 `has_worktree_changes` 看脏不脏，干净直接 `_remove_worktree` 返 `CleanupResult(kept=False)`，脏返 `CleanupResult(kept=True, path, branch)`；供 SubAgent 完成后调用。
- F11: SubAgent 集成：`AgentTool._execute_with_worktree` 当 `definition.isolation == "worktree"` 时，调 `generate_worktree_name` 生成 `agent-<8hex>` slug → `worktree_manager.create(wt_name, "HEAD")` → `build_worktree_notice(parent_cwd, wt.path)` 拼接到 prompt 前 → `sub_agent.work_dir = wt.path` + `PathSandbox(wt.path)` 锁定权限边界。
- F12: 子 Agent 完成后决策：`auto_cleanup(wt_name, wt.head_commit)` 干净 → 自动清理 worktree，脏 → 保留并在结果末尾附 `[Worktree preserved at <path>, branch <branch>]` 给主 Agent review。
- F13: 变更保护：`ExitWorktreeTool` 在 `action="remove"` 且 `discard_changes` 不为 True 时调 `count_worktree_changes`，`uncommitted > 0 or new_commits > 0` 拒绝并把具体数（file/files 和 commit/commits 单复数正确）回吐给 LLM。
- F14: 变更检测 fail-closed：`count_worktree_changes` 的 `_run_git` 抛 `SubprocessError / OSError / ValueError` 时把对应计数置 1（按"有变更"处理）；`has_unpushed_commits` 在 git 失败时返 `True`，绝不在 git 命令失败时清掉用户工作。
- F15: LLM Tool 暴露：`EnterWorktreeTool`（input 仅可选 `name`，已有 session 时拒绝 "Already in a worktree session"）和 `ExitWorktreeTool`（input `action` 必填，`discard_changes` 可选，无 session 时返回 "No-op: there is no active EnterWorktree session..."）；两个工具 `should_defer = True`，由主 Agent loop 在工具批次结束时统一执行。
- F16: 临时 worktree 命名模式：用前缀化的固定模式区分自动产物（`agent-<8hex>` / `wf_<8hex>-<3hex>-<n>` / `wf-<n>` / `bridge-<id>` / `job-<slug>-<8hex>`）和用户手动命名；正则在 `EPHEMERAL_PATTERNS` 集中维护，便于新增来源时统一加入。
- F17: 后台过期清理三层过滤：`cleanup_stale_worktrees` 周期扫 `worktree_dir`，依次过滤 —— L1 命名模式（用户起名的永不删，廉价）→ L2 时态（跳过当前 session 占用的 + `info.stat().st_mtime > cutoff`）→ L3 git 状态 fail-closed（`has_worktree_changes` 或 `has_unpushed_commits` 任一为 True 都跳过）；通过的删 worktree + 删分支。

## 4. 非功能需求

- N1: `WorktreeManager` 用 `asyncio.Lock` 保护 `create`，并发创建同名 worktree 互斥；`active` 字典和 `current_session` 用同一锁覆盖。
- N2: 任何路径的 worktree 删除（会话级 exit / Agent 级 auto_cleanup / 后台清理）都要保证当前 cwd 不在 worktree 内（`_run_git` 的 `cwd` 缺省走 `repo_root`），否则 `git worktree remove` 会失败。
- N3: `git worktree remove` 和 `git branch -D` 之间必须 `await asyncio.sleep(0.1)` 等 git lockfile 释放，否则 branch 删除会偶发失败。
- N4: `restore_session` 在 HEAD SHA 读不到时必须主动 `save_worktree_session(None)` 清脏文件，否则下次启动会反复尝试恢复同一个已损坏的 session。
- N5: 三层过滤的执行顺序固定：先廉价的命名模式 → 再时态判断 → 最后贵的 git 检查；任何一层判定保留都立即 `continue`，不进入下一层。
- N6: 创建后设置的四项里软链接和 `.worktreeinclude` 复制是 best-effort —— 单文件失败只 `log.warning` 不抛，保证主路径鲁棒。
- N7: 变更保护的错误信息必须包含具体数字（N file/files + M commit/commits）和单复数语法正确，让 LLM 能据此判断要不要强删；不能只回 "has changes" 这种空话。
- N8: worktree 子系统不假设统一日志层存在，所有创建/退出/清理的信息通过工具结果文本传达；这同时是给 LLM 的运行时反馈。日志只用 `logging.getLogger(__name__)`。

## 5. 设计概要

- 核心数据结构（`codebot/worktree/models.py`）:
 - `Worktree`：`name / path / branch / based_on / head_commit / created`（dataclass，活跃 worktree 注册项）。
 - `WorktreeSession`：`original_cwd / worktree_path / worktree_name / original_branch / original_head_commit / session_id / hook_based`（dataclass，会话级单例，序列化到 JSON）。
 - `Changes`：`uncommitted / new_commits`（dataclass，变更计数）。
 - `CleanupResult`：`kept / path / branch`（dataclass，Agent 级自动清理返回值）。
 - `WorktreeManager`：持有 `repo_root / file_cache / symlink_directories / worktree_dir / _lock / active / current_session`，是所有 worktree 操作的入口。
- 主流程:
 - **会话级 Enter**：`EnterWorktreeTool.execute` → guard `get_current_session() != None` → `validate_slug` → `WorktreeManager.create(slug)`（自动走快速恢复或 add + setup）→ `WorktreeManager.enter(slug)` → 返回带路径和分支的 Tool 文本。
 - **会话级 Exit**：`ExitWorktreeTool.execute` → guard 无 session → 若 `action="remove"` 且未 `discard_changes` 跑 `count_worktree_changes` → `WorktreeManager.exit(name, action, discard_changes)` → action=remove 时调 `_remove_worktree`（git worktree remove → sleep 0.1 → git branch -D）。
 - **Agent 级隔离**：`AgentTool.execute` 看到 `definition.isolation == "worktree"` → `_execute_with_worktree` → `generate_worktree_name` 出 `agent-<8hex>` → `worktree_manager.create(wt_name, "HEAD")` → `build_worktree_notice` 拼 prompt 前缀 → `sub_agent.work_dir = wt.path` + `PathSandbox(wt.path)` → 跑完调 `auto_cleanup`。
 - **后台过期清理**：`app.py` 启动 `asyncio.create_task(start_stale_cleanup_task(...))` → 死循环 `await asyncio.sleep(interval)` → `cleanup_stale_worktrees` 三层过滤 → 通过的删。
- 调用链（模块层级）:
 - `codebot/app.py` 启动 → `WorktreeManager(repo_root=...)` 构造 → `restore_session` → 注册 `EnterWorktreeTool` / `ExitWorktreeTool` / `create_worktree_command` → `asyncio.create_task(start_stale_cleanup_task)`。
 - LLM Enter/Exit → 工具 registry → `codebot/worktree/manager.py` 会话级 API。
 - `AgentTool` → 看到 isolation worktree → `WorktreeManager.create` + `build_worktree_notice` → 子 Agent 跑完 → `auto_cleanup`。
- 与其他模块的交互:
 - 依赖 `codebot/tools`（注册两个工具）、`codebot/agents`（隔离分流）、`codebot/teams`（TeamManager 共用同一 manager）、`codebot/commands`（`/worktree` 子命令）、`codebot/cache`（FileCache 清理钩子）；底层只依赖 `asyncio` + `subprocess`（git）+ 标准库（`re` / `json` / `pathlib` / `secrets` / `fnmatch` / `shutil`）+ `pydantic`（工具 schema）。
 - 不依赖 `codebot/memory` / `codebot/prompt`。

## 6. Out of Scope

- 不实现非 git VCS 适配（hg / jj / sapling 等），所有 worktree 操作 hardcode 走 git 子命令
- 不实现 sparse checkout / partial clone 优化，大型 mono-repo 优化推到后续
- 不实现 `--worktree` / `--worktree --tmux` CLI 启动快速路径（涉及 tmux / iTerm2 子系统，留给 ch15）
- 不实现 PR fetch 或 pull request 头引用解析（远端协作场景）
- 不实现 prepare-commit-msg hook 注入 commit attribution（商业 feature 场景）
- 不实现 FindCanonicalGitRoot 穿透 commondir 的独立工具（Python 版仅以 `repo_root` 注入为主，多级嵌套 worktree 留给后续）
- 不引入第三方 gitignore 库（`fnmatch` 简化匹配够用）
- 团队成员（teammate）路径的 worktree 自动清理推到 ch15 收尾，本章 teammate 路径只创建并隔离、不负责清理

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch14: Worktree Tasks

> 任务粒度：每个任务可在一次会话内完成，可独立交付。

## T1: 实现 Slug 校验 + 命名映射
- 影响文件: `codebot/worktree/slug.py`（`MAX_SLUG_LENGTH` @ 5；`_SEGMENT_RE` @ 6；`validate_slug` @ 9-24；`flatten_slug` @ 27-28）
- 依赖任务: 无
- 完成标准: `validate_slug` 校验长度 ≤ 64、按 `/` 切段、每段匹配 `^[a-zA-Z0-9._-]+$`、显式拒绝空段和 `.` / `..` 段，错误返回带原因字符串，合法返回 `None`；`flatten_slug(s) = s.replace("/", "+")`；分支名拼接由调用方做 `f"worktree-{flat_slug}"`。
- [ ] 完成

## T2: 定义数据模型
- 影响文件: `codebot/worktree/models.py`（`Worktree` @ 7-14；`WorktreeSession` @ 17-25）
- 依赖任务: 无
- 完成标准: `Worktree` dataclass 含 6 字段（`name / path / branch / based_on / head_commit / created`），`created` 默认 `datetime.now`；`WorktreeSession` dataclass 含 7 字段（`original_cwd / worktree_path / worktree_name / original_branch / original_head_commit / session_id="" / hook_based=False`），后两字段有默认值。
- [ ] 完成

## T3: 实现变更检测 fail-closed
- 影响文件: `codebot/worktree/changes.py`（`GIT_ENV` @ 9；`_run_git` @ 12-22；`Changes` @ 25-28；`count_worktree_changes` @ 31-51；`has_worktree_changes` @ 54-56；`CleanupResult` @ 59-63；`has_unpushed_commits` @ 66-74）
- 依赖任务: 无
- 完成标准: `_run_git` 强制 `env={**os.environ, **GIT_ENV}` + `timeout=30`；`count_worktree_changes` 跑 `git status --porcelain` + `git rev-list --count <head>..HEAD`，任一 `SubprocessError / OSError / ValueError` 把对应字段置 1（**fail-closed**）；`has_worktree_changes` 任一计数 > 0 返 True；`has_unpushed_commits` 跑 `git rev-list --max-count=1 HEAD --not --remotes`，git 失败返 True；`CleanupResult` dataclass 含 `kept / path / branch`。
- [ ] 完成

## T4: 实现 SubAgent worktree 上下文 notice
- 影响文件: `codebot/worktree/integration.py`（`WORKTREE_NOTICE_TEMPLATE` @ 9-20；`generate_worktree_name` @ 23-24；`build_worktree_notice` @ 27-31）
- 依赖任务: 无
- 完成标准: `WORKTREE_NOTICE_TEMPLATE` 多行字符串，含 `[WORKTREE CONTEXT]` / `[/WORKTREE CONTEXT]` 标记、`{wt_path}` 和 `{parent_cwd}` 占位、关键句 "running in an isolated Git Worktree"、"translate them to your local worktree path"、"re-read files before editing"；`generate_worktree_name()` 返回 `f"agent-{secrets.token_hex(4)}"`（8 hex 字符，匹配 cleanup `^agent-a[0-9a-f]{7}$` 不严格但实际产出 `agent-` 开头 8 hex）；`build_worktree_notice(parent_cwd, wt_path)` 用 `.format()` 注入两个占位。
- [ ] 完成

## T5: 实现会话持久化
- 影响文件: `codebot/worktree/session.py`（`SESSION_FILENAME` @ 11；`_session_path` @ 14-15；`save_worktree_session` @ 18-36；`load_worktree_session` @ 39-58）
- 依赖任务: T2
- 完成标准: `SESSION_FILENAME = "worktree_session.json"`；`save_worktree_session(codebot_dir, session)`：`mkdir(parents=True, exist_ok=True)` → `session is None` 时写 `"{}"` 等价清空 → 否则 dump 7 字段到 JSON；`load_worktree_session`：文件不存在返 `None`，`JSONDecodeError / KeyError` 时 `log.warning` 后返 `None`，dict 为空或缺 `worktree_path` 返 `None`，否则构造 `WorktreeSession`，`session_id` / `hook_based` 用 `data.get(...)` 容忍旧版字段缺失。
- [ ] 完成

## T6: 实现创建后设置四项
- 影响文件: `codebot/worktree/setup.py`（`LOCAL_CONFIG_FILES` @ 12-15；`perform_post_creation_setup` @ 18-29；`_copy_local_configs` @ 32-41；`_setup_git_hooks` @ 44-67；`_create_symlinks` @ 70-82；`_copy_ignored_files` @ 85-131）
- 依赖任务: 无
- 完成标准: `perform_post_creation_setup` 依序调四项 A/B/C/D；`LOCAL_CONFIG_FILES = ["settings.local.json", ".env"]`；A `_copy_local_configs` 用 `shutil.copy2`，`OSError` 仅 warning 不抛；B `_setup_git_hooks` 优先 `<repo>/.husky` 回退 `<repo>/.git/hooks`，找到目录跑 `git config core.hooksPath`；C `_create_symlinks` 遍历 `directories`，跳已存在和不存在的，`OSError` warning；D `_copy_ignored_files` 读 `.worktreeinclude`（跳空行和 `#`）→ `git ls-files --others --ignored --exclude-standard --directory` → `fnmatch.fnmatch` 筛选 → 单文件失败 `continue` 不中断。
- [ ] 完成

## T7: 实现 WorktreeManager 主类 + 快速恢复
- 影响文件: `codebot/worktree/manager.py`（`GIT_ENV` @ 28；`WorktreeError` @ 31-32；`WorktreeManager.__init__` @ 36-54；`add_cache_clear_callback` @ 56-57；`_clear_all_caches` @ 59-66；`_run_git` @ 68-78；`read_worktree_head_sha` @ 84-128；`_get_current_branch` @ 316-321；`_get_head_commit` @ 323-328）
- 依赖任务: T1, T3
- 完成标准: `WorktreeManager` 持有 `repo_root / file_cache / symlink_directories / worktree_dir / _codebot_dir / _lock=asyncio.Lock() / active: dict / current_session: WorktreeSession | None`；`worktree_dir` 默认 `<repo_root>/.codebot/worktrees`；`_run_git` 强制 `env={**os.environ, **GIT_ENV}` + `cwd=cwd or repo_root` + `stdin=subprocess.DEVNULL` + `timeout=60`；`read_worktree_head_sha` 静态方法，完整链路（`.git pointer → gitdir → commondir → HEAD → loose ref/packed-refs`），失败返 `None`，目标延迟无 git 子进程。
- [ ] 完成

## T8: 实现 create + enter + exit + _remove_worktree
- 影响文件: `codebot/worktree/manager.py`（`create` @ 134-186；`enter` @ 192-212；`exit` @ 218-243；`_remove_worktree` @ 249-260；`auto_cleanup` @ 266-275；`list_worktrees / get_current_session` @ 281-285；`restore_session` @ 291-310）
- 依赖任务: T2, T5, T6, T7
- 完成标准: `create` 在 `async with self._lock` 内：`validate_slug` → `active` 字典重名检查 → 快速恢复（`read_worktree_head_sha` 命中直接构造 `Worktree`，**不**跑 setup）→ 未命中 `os.makedirs(worktree_dir, exist_ok=True)` → `git worktree add -B worktree-<flat> <path> <base_branch>` → `perform_post_creation_setup`；`enter`：`_clear_all_caches` → `os.getcwd` + `_get_current_branch` + `_get_head_commit` → 写 `current_session` + `save_worktree_session`；`exit`：`action="remove" and not discard_changes` 时变更保护抛 `WorktreeError` 含具体计数 → 清缓存 + 清单例 + `save_worktree_session(None)` → `action="remove"` 调 `_remove_worktree`；`_remove_worktree`：`git worktree remove --force` → `await asyncio.sleep(0.1)` → `git branch -D worktree-<flat>` → `active.pop`；`auto_cleanup`：脏返 `CleanupResult(kept=True, path, branch)`，干净 `_remove_worktree` 返 `CleanupResult(kept=False)`；`restore_session`：读持久化 → `read_worktree_head_sha` 验证 → 命中写回 `active` + `current_session`，未命中调 `save_worktree_session(None)` 清脏。
- [ ] 完成

## T9: 实现后台过期清理
- 影响文件: `codebot/worktree/cleanup.py`（`EPHEMERAL_PATTERNS` @ 16-22；`_is_ephemeral` @ 25-26；`cleanup_stale_worktrees` @ 29-81；`start_stale_cleanup_task` @ 84-96）
- 依赖任务: T3, T8
- 完成标准: `EPHEMERAL_PATTERNS` 五条正则：`^agent-a[0-9a-f]{7}$` / `^wf_[0-9a-f]{8}-[0-9a-f]{3}-\d+$` / `^wf-\d+$` / `^bridge-[A-Za-z0-9_]+(-[A-Za-z0-9_]+)*$` / `^job-[a-zA-Z0-9._-]{1,55}-[0-9a-f]{8}$`；`_is_ephemeral` 任一正则 match 返 True；`cleanup_stale_worktrees(manager, cutoff_hours)` 三层过滤 — **L1 命名**：`_is_ephemeral` False 跳；**L2 时态**：`current_session.worktree_name == name` 跳 + `mtime > cutoff` 跳；**L3 git 状态 fail-closed**：`read_worktree_head_sha is None` 跳 + `has_worktree_changes` 跳 + `has_unpushed_commits` 跳；通过的复用 `_remove_worktree` 或直接 `git worktree remove --force` + `sleep(0.1)` + `git branch -D`；返回清理数；`start_stale_cleanup_task(manager, interval, cutoff_hours)`：死循环 `await asyncio.sleep(interval)` → `cleanup_stale_worktrees` → 异常 `log.warning` 不抛。
- [ ] 完成

## T10: 包级 `__init__.py` 导出
- 影响文件: `codebot/worktree/__init__.py`（导出 14 个公共符号 + `__all__`）
- 依赖任务: T1, T2, T3, T5, T8, T9
- 完成标准: 从 `changes` 导出 `Changes / CleanupResult / count_worktree_changes / has_worktree_changes`；从 `cleanup` 导出 `cleanup_stale_worktrees / start_stale_cleanup_task`；从 `manager` 导出 `WorktreeError / WorktreeManager`；从 `models` 导出 `Worktree / WorktreeSession`；从 `session` 导出 `load_worktree_session / save_worktree_session`；从 `slug` 导出 `flatten_slug / validate_slug`；`__all__` 列出 14 个名字按字母序。
- [ ] 完成

## T11: 实现 EnterWorktreeTool
- 影响文件: `codebot/tools/enter_worktree.py`（`EnterWorktreeParams` @ 15-23；`EnterWorktreeTool` @ 26-65）
- 依赖任务: T1, T8
- 完成标准: `EnterWorktreeParams` 用 pydantic 定义，仅 `name: Optional[str]` 字段含描述；`EnterWorktreeTool`：`name = "EnterWorktree"` / `category = "command"` / `should_defer = True` / `params_model = EnterWorktreeParams`；`__init__(self, worktree_manager)`；`execute`：`get_current_session() is not None` → 返 `ToolResult(output="Already in a worktree session", is_error=True)` → 否则 `slug = params.name or f"wt-{secrets.token_hex(4)}"` → `validate_slug` 失败返错 → `manager.create(slug)` + `manager.enter(slug)` → 返回 `ToolResult(output=f"Created worktree at {session.worktree_path} on branch {wt.branch}. The session is now working in the worktree. Use ExitWorktree to leave mid-session, or exit the session to be prompted.")`。
- [ ] 完成

## T12: 实现 ExitWorktreeTool
- 影响文件: `codebot/tools/exit_worktree.py`（`ExitWorktreeParams` @ 14-25；`ExitWorktreeTool` @ 28-110）
- 依赖任务: T3, T8
- 完成标准: `ExitWorktreeParams`：`action: str` 必填 + `discard_changes: Optional[bool] = None`；`ExitWorktreeTool`：`name = "ExitWorktree"` / `should_defer = True`；`execute`：`get_current_session() is None` → 返 "No-op: there is no active EnterWorktree session to exit. This tool only operates on worktrees created by EnterWorktree in the current session — it will not touch worktrees created manually or in a previous session. No filesystem changes were made."（`is_error=True`）；`action not in ("keep", "remove")` 返非法值；`action == "remove" and not discard` 时 `count_worktree_changes` → `uncommitted/new_commits > 0` 拼具体数（**单复数 file/files、commit/commits 正确**）→ `manager.exit(wt_name, action, discard_changes=discard)` → keep 返 "Your work is preserved at ... Session is now back in ..."，remove 返 "Exited and removed worktree at ..."。
- [ ] 完成

## T13: 实现 `/worktree` 本地命令
- 影响文件: `codebot/commands/handlers/worktree.py`（`create_worktree_command` @ 11-49；`_handle_create` @ 52-85；`_handle_list` @ 88-110 附近；`_handle_enter` / `_handle_exit` / `_handle_status`）
- 依赖任务: T8
- 完成标准: `create_worktree_command(manager)` 返回 `Command(name="worktree", aliases=["wt"], type=CommandType.LOCAL)`；子命令解析 `create / list / enter / exit / status`，未知子命令报 "未知子命令: ..."；`_handle_create` 调 `manager.create + manager.enter` 并同步 `ctx.agent.work_dir`；`_handle_exit` 解析 `--remove` / `--discard` 标志映射到 `action / discard_changes`；`_handle_list` 列出 `manager.list_worktrees` 标当前；`_handle_status` 输出当前 session 路径和原始分支。
- [ ] 完成

## T14: 接入 AgentTool worktree 隔离
- 影响文件: `codebot/tools/agent_tool.py`（`AgentToolParams` 含 `isolation` @ 27；`__init__` 接 `worktree_manager` @ 71/80；`execute` 解析 isolation @ 89-96；`_execute_with_worktree` @ 491-610）
- 依赖任务: T4, T8, T11
- 完成标准: `AgentToolParams` 含 `isolation: str | None = None` 和 `team_name: str | None = None`；`AgentTool.__init__` 多两个可选参数 `worktree_manager / team_manager`；`execute` 在 `p.team_name` 时走 teammate 分支，否则按 `definition.isolation == "worktree"` 分流 `_execute_with_worktree`；`_execute_with_worktree`：`worktree_manager is None` 报错 → `generate_worktree_name` 出 `agent-<8hex>` → `manager.create(wt_name, "HEAD")` → `notice = build_worktree_notice(parent_cwd, wt.path)` → `task = notice + "\n\n" + p.prompt` → 构造子 Agent `work_dir=wt.path` + `PathSandbox(wt.path)` → `run_to_completion(task)` → `manager.auto_cleanup(wt_name, wt.head_commit)` → `cleanup.kept` 时结果末尾拼 `[Worktree preserved at <path>, branch <branch>]`。
- [ ] 完成

## T15: 接入 app.py 启动装配
- 影响文件: `codebot/app.py`（imports @ 82-84；worktree setup 段 @ 691-722；teardown @ 1602-1605）
- 依赖任务: T8, T9, T11, T12, T13, T14
- 完成标准:
 1. `WorktreeConfig` 注入 `symlink_directories / stale_cleanup_interval / stale_cutoff_hours`；
 2. `self.worktree_manager = WorktreeManager(repo_root=work_dir, file_cache=self.file_cache, symlink_directories=wt_cfg.symlink_directories)`；
 3. `add_cache_clear_callback` 加 skills 清理钩子；
 4. `restored = self.worktree_manager.restore_session()` 非 None 时 `self.agent.work_dir = restored.worktree_path`；
 5. `create_worktree_command(self.worktree_manager)` + `command_registry.register_sync`；
 6. `registry.register(EnterWorktreeTool(...))` + `registry.register(ExitWorktreeTool(...))`；
 7. `self._stale_cleanup_task = asyncio.create_task(start_stale_cleanup_task(self.worktree_manager, wt_cfg.stale_cleanup_interval, wt_cfg.stale_cutoff_hours))`；
 8. TeamManager 和 AgentTool 共用同一 `worktree_manager` 注入；
 9. teardown 时遍历 `worktree_manager.active.values()` 清理残留。
- [ ] 完成

## T16: 端到端验证
- 影响文件: 无（仅运行）
- 依赖任务: T1-T15
- 完成标准:
 - `ruff check codebot/worktree codebot/tools/enter_worktree.py codebot/tools/exit_worktree.py` 通过；
 - `pytest tests/test_worktree.py -v` 通过（含 `TestValidateSlug` / `TestFlattenSlug` / `TestSessionPersistence` / `TestWorktreeManager` / `TestChangeDetection` / `TestReadWorktreeHeadSha` 等组）；
 - **路径 A — 工具直接驱动**：主 Agent 调 `EnterWorktree({name: "demo"})` 创建 worktree → 在 worktree 里 `WriteFile + Bash("git commit ...")` → `ExitWorktree({action: "remove"})` 被变更保护拒绝并列出具体数 → `ExitWorktree({action: "remove", discard_changes: true})` 强删成功；
 - **路径 B — 子 Agent 自动隔离**：主 Agent 在主目录 `WriteFile witness.txt = "original content from main agent"` → 调 `Agent({subagent_type: "<声明 isolation worktree 的类型>", prompt: "把 witness.txt 改成 ..."})` → 验证主目录 `witness.txt` 内容不变；`.codebot/worktrees/agent-*/witness.txt` 是修改后版本；若有 commit → 结果末尾出现 `[Worktree preserved at ..., branch worktree-agent-...]`。
- [ ] 完成

## 进度
- [ ] T1 / [ ] T2 / [ ] T3 / [ ] T4 / [ ] T5 / [ ] T6 / [ ] T7 / [ ] T8 / [ ] T9 / [ ] T10 / [ ] T11 / [ ] T12 / [ ] T13 / [ ] T14 / [ ] T15 / [ ] T16

```

```markdown
# ch14: Worktree Checklist

> 所有条目可勾选、可观测。验收方式写在条目后面括号中。验收：已通过验证的项均勾选。

## 1. 实现完整性

- [ ] 常量 `MAX_SLUG_LENGTH = 64` 在 `codebot/worktree/slug.py:5` 定义
- [ ] 函数 `validate_slug` 在 `codebot/worktree/slug.py:9-24` 含空名、长度、空段、`.` / `..`、非法段五类错误分类
- [ ] 函数 `flatten_slug` 在 `codebot/worktree/slug.py:27-28` 把 `/` 替换成 `+`；分支名由调用方拼 `f"worktree-{flat_slug}"`
- [ ] dataclass `Worktree` 在 `codebot/worktree/models.py:7-14` 含 6 字段（`name / path / branch / based_on / head_commit / created`）
- [ ] dataclass `WorktreeSession` 在 `codebot/worktree/models.py:17-25` 含 7 字段，`session_id` / `hook_based` 有默认值
- [ ] dataclass `Changes` 在 `codebot/worktree/changes.py:25-28`，`CleanupResult` 在 `codebot/worktree/changes.py:59-63`
- [ ] 函数 `count_worktree_changes` 在 `codebot/worktree/changes.py:31-51`，git 子进程异常时把对应计数置 1（**fail-closed**）
- [ ] 函数 `has_worktree_changes` 在 `codebot/worktree/changes.py:54-56`，`has_unpushed_commits` 在 `:66-74` git 失败默认返 True
- [ ] 字符串 `WORKTREE_NOTICE_TEMPLATE` 在 `codebot/worktree/integration.py:9-20` 含 `{parent_cwd}` / `{wt_path}` 占位 + "re-read files before editing" 关键句
- [ ] 函数 `generate_worktree_name` 在 `codebot/worktree/integration.py:23-24` 用 `secrets.token_hex(4)` 出 `agent-` 开头 8 hex 名字
- [ ] 函数 `save_worktree_session` 在 `codebot/worktree/session.py:18-36`，`session is None` 时写 `"{}"`（清空）
- [ ] 函数 `load_worktree_session` 在 `codebot/worktree/session.py:39-58` 容忍文件缺失、JSON 损坏、空 dict、缺字段全部返 `None`
- [ ] 常量 `LOCAL_CONFIG_FILES` 在 `codebot/worktree/setup.py:12-15` 含 `settings.local.json` + `.env`
- [ ] 函数 `perform_post_creation_setup` 在 `codebot/worktree/setup.py:18-29` 依序调四项 A/B/C/D
- [ ] 函数 `_copy_ignored_files` 在 `codebot/worktree/setup.py:85-131` 单文件失败 `continue` 不中断
- [ ] 类 `WorktreeManager` 在 `codebot/worktree/manager.py:35-328` 持有 `_lock=asyncio.Lock() / active / current_session`
- [ ] 静态方法 `WorktreeManager.read_worktree_head_sha` 在 `codebot/worktree/manager.py:84-128` 完整链路（`.git → gitdir → commondir → HEAD → loose/packed-refs`），失败返 `None`
- [ ] 方法 `WorktreeManager._run_git` 在 `codebot/worktree/manager.py:68-78` 强制 `env=GIT_ENV + stdin=DEVNULL + timeout=60`
- [ ] 方法 `WorktreeManager.create` 在 `codebot/worktree/manager.py:134-186` 实现"快速恢复 → 创建路径"二选一，使用 `-B` 大写参数
- [ ] 方法 `WorktreeManager.exit` 在 `codebot/worktree/manager.py:218-243` 在 `action="remove" and not discard_changes` 时跑变更保护
- [ ] 方法 `WorktreeManager._remove_worktree` 在 `codebot/worktree/manager.py:249-260` 含 `await asyncio.sleep(0.1)` 等 git lockfile 释放
- [ ] 方法 `WorktreeManager.auto_cleanup` 在 `codebot/worktree/manager.py:266-275` 脏返 `kept=True` + path/branch，干净返 `kept=False`
- [ ] 方法 `WorktreeManager.restore_session` 在 `codebot/worktree/manager.py:291-310` 在 `read_worktree_head_sha is None` 时反向 `save_worktree_session(None)` 清脏
- [ ] 变量 `EPHEMERAL_PATTERNS` 在 `codebot/worktree/cleanup.py:16-22` 含五个正则（agent-a / wf_ / wf- / bridge- / job-）
- [ ] 函数 `cleanup_stale_worktrees` 在 `codebot/worktree/cleanup.py:29-81` 三层过滤顺序固定（L1 命名 → L2 时态 → L3 git 状态）
- [ ] 函数 `start_stale_cleanup_task` 在 `codebot/worktree/cleanup.py:84-96` 死循环 + 异常 warning 不抛
- [ ] 类 `EnterWorktreeTool` 在 `codebot/tools/enter_worktree.py:26-65`，`should_defer = True` + `params_model = EnterWorktreeParams`
- [ ] 类 `ExitWorktreeTool` 在 `codebot/tools/exit_worktree.py:28-110`，`should_defer = True`
- [ ] `ExitWorktreeTool.execute` 在 `codebot/tools/exit_worktree.py:63-84` 单复数 file/files、commit/commits 正确处理

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `grep -rn "EnterWorktreeTool" --include="*.py" codebot/` 在 `codebot/app.py:711-713` 找到 import + 注册
- [ ] `grep -rn "ExitWorktreeTool" --include="*.py" codebot/` 在 `codebot/app.py:712-714` 找到 import + 注册
- [ ] `grep -rn "WorktreeManager" --include="*.py" codebot/` 至少命中 `codebot/app.py:694`、`codebot/tools/agent_tool.py`、`codebot/teams/manager.py`、`codebot/commands/handlers/worktree.py`
- [ ] `grep -rn "restore_session" --include="*.py" codebot/` 在 `codebot/app.py:704` 找到启动恢复调用
- [ ] `grep -rn "start_stale_cleanup_task" --include="*.py" codebot/` 在 `codebot/app.py:716-722` 找到 `asyncio.create_task` 包裹
- [ ] `grep -rn "build_worktree_notice" --include="*.py" codebot/` 在 `codebot/tools/agent_tool.py:544` 找到 prompt 拼接调用
- [ ] `grep -rn "generate_worktree_name" --include="*.py" codebot/` 在 `codebot/tools/agent_tool.py:535` 找到调用
- [ ] `grep -rn "auto_cleanup" --include="*.py" codebot/` 在 `codebot/tools/agent_tool.py:604` 找到子 Agent 完成后清理调用
- [ ] `grep -rn "count_worktree_changes" --include="*.py" codebot/` 在 `codebot/tools/exit_worktree.py:64` 和 `codebot/worktree/manager.py:229` 找到调用
- [ ] `grep -rn "has_worktree_changes" --include="*.py" codebot/` 在 `codebot/worktree/cleanup.py:59` 和 `codebot/worktree/manager.py:271` 找到调用
- [ ] `grep -rn "create_worktree_command" --include="*.py" codebot/` 在 `codebot/app.py:708` 找到 `/worktree` 命令注册
- [ ] `grep -rn "_execute_with_worktree" --include="*.py" codebot/` 在 `codebot/tools/agent_tool.py:96` 和 `:491` 找到分流入口

## 3. 编译与测试

- [ ] `ruff check codebot/worktree codebot/tools/enter_worktree.py codebot/tools/exit_worktree.py codebot/commands/handlers/worktree.py` 无报错
- [ ] `pytest tests/test_worktree.py -v` 通过（含 `TestValidateSlug` / `TestFlattenSlug` / `TestSessionPersistence` / `TestIntegrationHelpers` / `TestWorktreeManager` / `TestChangeDetection` / `TestReadWorktreeHeadSha` 等组）
- [ ] `python -c "from codebot.worktree import WorktreeManager, validate_slug, flatten_slug; print('ok')"` 无 import 错误
- [ ] `python -m mypy codebot/worktree` 或 `pyright codebot/worktree` 无新增 type 错误

## 4. 端到端验证

- [ ] **路径 A — 工具直接驱动**：用户对主 Agent 说"用 EnterWorktree 工具创建一个名叫 demo 的工作树" → LLM 调 `EnterWorktree({name: "demo"})` → 返回 `Created worktree at .../.codebot/worktrees/demo on branch worktree-demo`；让 Agent 在 worktree 里创建 `hello.txt` 并 `git commit`；让 Agent 调 `ExitWorktree({action: "remove"})` → 因有未推送 commit 被变更保护拒绝，错误文本包含具体 `1 commit` 或 `N commits`；`ExitWorktree({action: "remove", discard_changes: true})` 强删成功；`ls .codebot/worktrees/` 看到 `demo/` 已消失。
- [ ] **路径 B — 子 Agent 自动隔离**：用户让主 Agent 在主目录建 `witness.txt`（内容 "original content from main agent"）→ 调 `Agent({subagent_type: "<声明 isolation worktree 的类型>", description: "...", prompt: "把 witness.txt 改成 \"modified by isolated worker\"，然后 git 提交"})`；验证 `cat witness.txt` 主目录内容仍是 "original ..."；`cat .codebot/worktrees/agent-*/witness.txt` 是修改后版本；若子 Agent 有 commit → 结果末尾出现 `[Worktree preserved at ..., branch worktree-agent-...]`；若无修改 → worktree 自动清理（`.codebot/worktrees/` 下 `agent-*` 目录消失）。
- [ ] **持久化与 crash 恢复**：TUI 里 `EnterWorktree({name: "crashtest"})` 创建 worktree → `Ctrl+C` 杀进程 → `cat .codebot/worktree_session.json` 文件仍在并含 crashtest 会话；重启 CodeBot → 启动期间 `restore_session` 把 session 写回；下一次工具调用时 `get_current_session()` 非 None，且 `agent.work_dir` 已切到 worktree 路径。
- [ ] **变更保护单复数**：在 worktree 里建 1 个未提交修改 → `ExitWorktree({action: "remove"})` 返回 `"1 uncommitted file"`；建 2+ 个修改 → 返回 `"N uncommitted files"`（注意单复数）；同样验证 commit 数 `"1 commit"` / `"N commits"`。
- [ ] **后台清理保守不删**：手动在 `.codebot/worktrees/agent-aabcdef1/` 下建一个有未推送 commit 的目录（mtime 设为过期前）→ 等 cleanup loop 跑一轮（或手动 `await cleanup_stale_worktrees(manager, 1)` 测试）→ 该目录仍保留（L3 fail-closed 的 `has_unpushed_commits` 拦住）。
- [ ] **会话级 enter 时清理 FileCache**：在主仓里读一个文件触发 FileCache 命中 → `EnterWorktree` → 验证 `file_cache` 被清空（`len(file_cache) == 0`），保证后续读 worktree 不复用主仓的缓存。
- [ ] **`/worktree` 本地命令**：`/worktree create demo` 创建并进入 → `/worktree status` 显示当前 session → `/worktree list` 列出含 demo → `/worktree exit --remove --discard` 强删。

## 5. 文档

- [ ] `docs/python/ch14/spec.md` 已按 ch12/ch13 风格写完（F1-F17 + N1-N8，无 file:line 代码标注）
- [ ] `docs/python/ch14/tasks.md` 已写，16 个 T 全部勾完（T1-T16）
- [ ] `docs/python/ch14/checklist.md` 已写并逐项验收
- [ ] commit 信息标注 `ch14`，新增代码的调用链已在 PR 描述或 commit message 里说明

```

### Java

```markdown
# ch14: Worktree Spec（Java 版）

## 1. 背景

SubAgent 隔离了消息、权限、工具结果缓存，但所有子 Agent 仍然共享同一个工作目录——两个子 Agent 并发改同一个文件会互相覆盖。Git 分支不解决这个问题：分支只是时间维度的快照，同一时刻整个仓库仍然只有一份 working tree，切换分支会动所有文件的修改时间触发不必要的全量重编。多 Agent 并行要的是空间维度的隔离：同时存在多份独立的 working tree，每份对应不同分支，但共享同一个 `.git`。Git Worktree 提供的就是这个能力。这一章把它接进 CodeBot 的 Java 实现，让主 Agent 和每个子 Agent 都能拥有独立的文件视图。

## 2. 目标

把 worktree 做成两层 API：会话级让 LLM 通过 `EnterWorktreeTool` / `ExitWorktreeTool` 自主进出 worktree，Agent 级让 SubAgent 通过 `isolation: "worktree"` 声明自动获得独立 worktree。底层共用 `WorktreeManager` 的 `git worktree add/remove` 调用、`AgentWorktree` 的快速恢复路径，以及 `PostCreationSetup`（本地配置复制 / git hooks 配置 / 大目录软链接 / `.worktreeinclude` 文件复制）。叠加 `WorktreeChanges` 的 fail-closed 变更检测（无变更才允许清掉、有变更默认保留）和 `StaleCleanup` 对孤儿 worktree 的后台过期清理，保证既不丢用户工作、又不让磁盘堆积。

## 3. 功能需求

- F1: worktree 名称（slug）安全校验：限定字符集、长度上限 64、按 `/` 切段、显式拒绝 `.` / `..` 段，校验失败抛 `IllegalArgumentException` 分类错误（长度 / 段名非法 / 路径遍历）；任何 git 命令或路径拼接之前先跑。
- F2: slug 到路径和分支的映射：用 `+` 替换 `/`（git 安全但不在 slug 字符集），避免嵌套 slug 导致目录或分支命名冲突；分支统一加 `worktree-` 前缀，方便从 `git branch` 输出里识别 CodeBot 创建的。
- F3: 快速恢复路径：worktree 目录已存在时跳过 `git worktree add`，用 `Files.isDirectory` + `Files.setLastModifiedTime` bump mtime + 调一次 `git rev-parse HEAD` 拿 SHA；任一步失败回退到完整创建路径。
- F4: git 子进程统一安全壳：所有 `ProcessBuilder` 调用都在 `environment()` 里写 `GIT_TERMINAL_PROMPT=0` 和 `GIT_ASKPASS=`，绝不挂起等待用户输入；用 `waitFor(N, TimeUnit.SECONDS)` 超时保护，超时后 `destroyForcibly()`；进程失败抛 `IOException` 而不是 `RuntimeException`。
- F5: 创建/恢复主入口：`WorktreeManager.create` 接收 branch + 可选 targetDir，未给 targetDir 时默认 `<projectRoot>/.codebot/worktrees/<branch>`，用大写 `-B` 创建 worktree（容忍上次未清干净的孤儿分支）；`AgentWorktree.create` 在 slug 校验后先看目录是否存在，命中则快速恢复，未命中跑 `git worktree add -B <branch> <path> HEAD`。
- F6: 创建后设置四项：从主仓复制 `.codebot/settings.local.json`；按 `.husky` > `.git/hooks` 优先级在 worktree 里跑 `git config core.hooksPath <path>`；按 `WorktreeManager.symlinkDirs` 配置软链接 `node_modules` 等目录（跳过含 `..` 项）；按 `.worktreeinclude` gitignore 风格模式复制被 `.gitignore` 忽略但运行需要的文件；任何单项失败只记日志、不中断创建。
- F7: 会话级 API 三件套：进入（`WorktreeManager.create` + 写 `WorktreeSessionStore` 单例 + 持久化 JSON）、Keep（`ExitWorktreeTool action=keep`：清单例 + 删持久化文件，保留 worktree 目录和分支）、Remove（`action=remove`：清单例 + 删持久化 + `WorktreeManager.remove`）。
- F8: 会话持久化：`WorktreeSessionStore.save` 把 `WorktreeSession` record 序列化到 `<repo>/.codebot/worktree_session.json`，用 Jackson `ObjectMapper` + `@JsonProperty` snake_case 映射；`save(repo, null)` 等价于 `Files.deleteIfExists`。
- F9: 启动恢复：应用启动时调 `WorktreeSessionStore.load(repoRoot)`，非 null 时调 `restoreSession` 写回 `volatile` 全局字段；不主动切 cwd（让用户或工具自行决定），不重跑创建后设置。
- F10: Agent 级 API：`AgentWorktree.create(slug, repoRoot, symlinkDirs)` 静态方法返回 `Result(worktreePath, worktreeBranch, headCommit, gitRoot)` record；不动 `WorktreeSessionStore` 单例、不切 JVM cwd、不写持久化；快速恢复路径要 `Files.setLastModifiedTime` 防被 `StaleCleanup` 误判为孤儿。
- F11: SubAgent 集成：`AgentTool` 在解析参数时拿到 `isolation: "worktree"` 且 `worktreeManager != null` 时，生成 `agent-a<7hex>` slug → 调 `AgentWorktree.create` → 把 `subAgent.setWorkDir(wtResult.worktreePath())` → 在任务 prompt 前面拼 `AgentWorktree.buildNotice(parentCwd, wtPath)` 注入隔离 notice → 跑子 Agent。
- F12: 子 Agent 完成后决策：`LoopComplete` 事件触发时调 `WorktreeChanges.hasChanges(wtPath, headCommit)`，干净自动 `AgentWorktree.remove`、脏则保留并在返回结果末尾附 `"Worktree kept at <path> (branch <branch>) — has uncommitted changes or new commits."`。
- F13: 变更保护：`ExitWorktreeTool` 在 `action="remove"` 且 `discard_changes` 不为 `true` 时跑 `WorktreeChanges.countChanges`——返回 null（状态无法验证）报 `"Could not verify worktree state..."`；`changedFiles > 0` 或 `commits > 0` 报具体数字（"N uncommitted file(s) and M commit(s)"）；要求 LLM 显式传 `discard_changes=true` 才能强删。
- F14: 变更检测 fail-closed：`WorktreeChanges.hasChanges` 在 git status / rev-list 任何一步失败（runGit 返 null 或抛异常）都返 `true`；`countChanges` 在状态拿不到时返 `null`，强制调用方按"未知即不安全"处理。
- F15: LLM Tool 暴露：`EnterWorktreeTool`（input 仅可选 `name`，已有 session 时拒绝 `"Already in a worktree session"`）和 `ExitWorktreeTool`（input `action` 必填枚举 `["keep","remove"]` / `discard_changes` 可选 bool，无 session 时拒绝）；两个 Tool 的 `shouldDefer()` 都返 `true`，由 Agent loop 在工具批次结束时统一执行。
- F16: 临时 worktree 命名模式：用前缀正则区分"自动产物"（`agent-a` / `wf_` / `wf-` / `bridge-` / `job-` 五类）和"用户手动命名"；用户起名永远不会被后台清理动。
- F17: 后台过期清理三层过滤：`StaleCleanup.cleanup` 扫 `<repo>/.codebot/worktrees/`，依次过滤——L1 `isEphemeral`（不匹配五个正则的跳过）→ L2 时态（跳过当前 session 占用的 + `lastModifiedTime().toInstant().isAfter(cutoff)` 的）→ L3 git 状态 fail-closed（`status --porcelain -uno` 非空或失败跳过 + `rev-list --max-count=1 HEAD --not --remotes` 非空或失败跳过）；删完跑 `git worktree prune` 同步 git 内部表；`startCleanupLoop` 通过 `ScheduledExecutorService.scheduleAtFixedRate` 周期跑。

## 4. 非功能需求

- N1: `WorktreeSessionStore` 用 `volatile` + 静态字段保证并发可见性；`WorktreeManager` 所有公开方法 `synchronized` 保护内存里的 `LinkedHashMap<String, WorktreeInfo>`；Agent 级 API（`AgentWorktree.create/remove`）是无状态静态方法，天然并发安全。
- N2: 任何路径的 worktree 删除（会话级 Remove / Agent 级 Remove / 后台清理）都不在 worktree 内执行 git 命令——`AgentWorktree.remove` 显式从 `gitRoot` 跑 `ProcessBuilder` 的 `directory()`，否则 `git worktree remove` 会因为当前在被删目录里失败。
- N3: `git worktree remove` 和 `git branch -D` 之间必须 `Thread.sleep(100)` 等 git lockfile 释放，否则 branch 删除会偶发失败。
- N4: Agent 级 API 在快速恢复（worktree 目录已存在）时必须 `Files.setLastModifiedTime(wtPath, FileTime.from(Instant.now()))` bump mtime，否则同一 worktree 被反复复用时会因为 mtime 太老被 `StaleCleanup` 误删。
- N5: 三层过滤的执行顺序固定：先廉价的命名模式 → 再时态判断 → 最后贵的 git 检查；任何一层判定保留都 `continue`，不进入下一层。
- N6: `PostCreationSetup` 的四项里软链接和 `.worktreeinclude` 复制是 best-effort——`catch (IOException e)` 只 `log.fine` 不抛、不中断创建，保证主路径鲁棒。
- N7: 变更保护的错误信息必须包含具体数字（N 文件 + M commits）和单复数（"1 file" vs "2 files"、"1 commit" vs "2 commits"），让 LLM 能据此判断要不要强删；不能只回 "has changes" 这种空话。
- N8: worktree 子系统不假设统一日志层存在，所有创建/退出/清理的关键信息通过 `ToolResult` 文本传达；这同时是给 LLM 的运行时反馈，`java.util.logging.Logger` 只用于内部 best-effort 失败。

## 5. 设计概要

- 核心数据结构（全部 Java 17+ `record`）:
  - `WorktreeManager.WorktreeInfo(path, branch, createdAt)`：底层创建路径返回值，挂在 `WorktreeManager` 内存 map 里。
  - `AgentWorktree.Result(worktreePath, worktreeBranch, headCommit, gitRoot)`：Agent 级 API 返回值，不写全局状态。
  - `WorktreeSession(originalCwd, worktreePath, worktreeName, worktreeBranch, originalBranch, originalHeadCommit, sessionId, creationDurationMs)`：会话级单例，Jackson 序列化到磁盘，`@JsonProperty` 写 snake_case key。
  - `WorktreeChanges.ChangeSummary(changedFiles, commits)`：变更计数，供变更保护错误信息生成。
  - 配置块：`WorktreeManager` 构造参数 `symlinkDirs` + `staleCutoffHours`，由应用启动时注入；后台清理由 `StaleCleanup.startCleanupLoop` 单独调度，间隔 ≤ 0 时不启动。
- 主流程:
  - **会话级 Enter**：`EnterWorktreeTool.execute` → guard `WorktreeSessionStore.getCurrentSession() != null` → slug 校验（`SlugValidator.validate`）→ `WorktreeManager.create` → 组装 `WorktreeSession` record → `restoreSession` + `save`。
  - **会话级 Exit**：`ExitWorktreeTool.execute` → guard 无 session → 若 `action=remove && !discard_changes` 跑 `WorktreeChanges.countChanges` 变更保护 → 清单例 → `save(repo, null)` 删持久化 → `action=remove` 时调 `WorktreeManager.remove`。
  - **Agent 级隔离**：`AgentTool.runSync` → `isolation=="worktree" && worktreeManager != null` → 生成 `agent-a<7hex>` slug → `AgentWorktree.create` → `subAgent.setWorkDir(wtPath)` → `prompt = buildNotice(parentCwd, wtPath) + "\n\n" + prompt` → 跑子 Agent → `LoopComplete` 时 `WorktreeChanges.hasChanges`：干净 `AgentWorktree.remove` / 脏拼 `wtInfo` 后缀。
  - **后台过期清理**：`StaleCleanup.startCleanupLoop(executor, repoRoot, intervalSeconds, cutoffHours)` → `scheduleAtFixedRate` → 每轮 `cleanup(repoRoot, Instant.now().minusSeconds(cutoffHours*3600))` → 三层过滤 → 通过的 `AgentWorktree.remove` → 末尾若有删除跑一次 `git worktree prune`。
- 调用链（模块层级）:
  - 应用启动 → 构造 `WorktreeManager(projectRoot, symlinkDirs, staleCutoffHours)` → 注册 `EnterWorktreeTool` 和 `ExitWorktreeTool` → `WorktreeSessionStore.load + restoreSession` 恢复 session → `StaleCleanup.startCleanupLoop` 起后台任务。
  - LLM Enter/Exit → Tool dispatcher → `WorktreeManager` / `WorktreeSessionStore` / `WorktreeChanges`。
  - `AgentTool` → 看到 `isolation: worktree` → `AgentWorktree.create` → 子 Agent 跑完 → `WorktreeChanges.hasChanges` → `AgentWorktree.remove` 或拼字符串保留。
- 与其他模块的交互:
  - 依赖 `com.codebot.tool`（Tool 接口 + ToolResult + ToolCategory）、`com.codebot.subagent`（AgentTool 注入 `setWorktreeManager`）、`com.codebot.agent`（`Agent.setWorkDir`）；底层只依赖 `ProcessBuilder`（git）+ `java.nio.file` + `com.fasterxml.jackson.databind.ObjectMapper`。
  - 不依赖 `com.codebot.config` 通用加载链路——worktree 配置当前由应用启动时手动注入；也不依赖 `com.codebot.memory` / `com.codebot.prompt`。

## 6. Out of Scope

- 不实现非 git VCS 适配（hg / jj / sapling 等），所有 worktree 操作 hardcode 走 `ProcessBuilder("git", ...)`
- 不实现 sparse checkout / partial clone 优化，大型 mono-repo 优化推到后续
- 不实现 `--worktree` CLI 启动快速路径（涉及终端子系统，留给 ch15）
- 不实现 PR fetch 或 pull request 头引用解析（远端协作场景）
- 不实现 prepare-commit-msg hook 注入 commit attribution（商业 feature 场景）
- 不实现 ReadFile / Memory / SystemPrompt 缓存清理 hook（CodeBot 当前没有这几类缓存）
- 不引入第三方 gitignore 库（`PostCreationSetup.matchesAnyPattern` 简化匹配够用）
- 团队成员（teammate）路径的 worktree 自动清理推到 ch15 收尾，本章 teammate 路径只创建并隔离、不负责清理

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch14: Worktree Tasks（Java 版）

> 任务粒度：每个任务可在一次会话内完成，可独立交付。

## T1: 实现 Slug 校验 + 命名映射
- 影响文件: `src/main/java/com/codebot/worktree/SlugValidator.java`（`MAX_LENGTH` @ 11；`VALID_SEGMENT` @ 12；`validate` @ 16-37；`flatten` @ 39-41；`branchName` @ 43-45）
- 依赖任务: 无
- 完成标准: `validate(String slug)` 校验长度 ≤ 64、按 `/` 切段、每段匹配 `^[a-zA-Z0-9._-]+$`、显式拒绝 `.` / `..` 段，错误分类（cannot be empty / 长度 / `.` `..` 段 / 非法段）通过 `IllegalArgumentException` 抛出；`flatten(s) = s.replace('/', '+')`；`branchName(s) = "worktree-" + flatten(s)`；类声明为 `final`，构造私有，只暴露静态方法。
- [ ] 完成

## T2: 实现 git 进程执行壳
- 影响文件: `src/main/java/com/codebot/worktree/WorktreeManager.java`（`runGit` @ 180-200）、`src/main/java/com/codebot/worktree/WorktreeChanges.java`（`runGit` @ 64-87）、`src/main/java/com/codebot/worktree/StaleCleanup.java`（`runGitQuiet` @ 113-134）、`src/main/java/com/codebot/worktree/AgentWorktree.java`（`readHead` @ 106-118）
- 依赖任务: 无
- 完成标准: 所有 `ProcessBuilder` 调用前在 `environment()` put `GIT_TERMINAL_PROMPT=0` 和 `GIT_ASKPASS=""`（`WorktreeChanges.runGit` @ 72-73、`StaleCleanup.runGitQuiet` @ 120-121、`AgentWorktree.create` @ 45-46）；用 `waitFor(N, TimeUnit.SECONDS)` 超时保护（30 或 60 秒），未完成时 `destroyForcibly()`；进程退出非 0 时按调用约定要么抛 `IOException`（`WorktreeManager.runGit` @ 196-198）要么返 `null`（`WorktreeChanges.runGit` @ 83）。
- [ ] 完成

## T3: 实现 WorktreeManager 主入口
- 影响文件: `src/main/java/com/codebot/worktree/WorktreeManager.java`（`WorktreeInfo` record @ 25；构造 @ 32-36；`create` @ 51-65；`remove` @ 70-78；`list` @ 86-97；`cleanupStale` @ 112-132；`detectChanges` @ 156-176；`parsePorcelain` @ 211-240）
- 依赖任务: T2
- 完成标准: `WorktreeInfo(path, branch, createdAt)` record；构造接收 `projectRoot` + `symlinkDirs`（null 容忍为 `List.of()`）+ `staleCutoffHours`（<=0 时默认 24）；`create(branch, targetDir)` 在 `targetDir==null` 时默认 `<projectRoot>/.codebot/worktrees/<branch>`，调 `git worktree add -B <branch> <wtDir>` 大写 `-B` 容忍孤儿分支，成功后调 `PostCreationSetup.perform` 跑四项设置，最后把 `WorktreeInfo` 放进 `LinkedHashMap`；`remove(branch)` 拿出 map 项跑 `git worktree remove <path> --force` 然后 `worktrees.remove(branch)`；`list()` 优先解析 `git worktree list --porcelain` 输出（`parsePorcelain` 按 blank line 分块），失败回退内存 map；所有公开方法 `synchronized`。
- [ ] 完成

## T4: 实现 PostCreationSetup 四项
- 影响文件: `src/main/java/com/codebot/worktree/PostCreationSetup.java`（`perform` @ 19-24；`copySettingsLocal` @ 26-36；`configureHooksPath` @ 38-58；`symlinkDirectories` @ 60-73；`copyWorktreeIncludeFiles` @ 75-106；`matchesAnyPattern` @ 108-116）
- 依赖任务: 无
- 完成标准: `perform(repoRoot, worktreePath, symlinkDirs)` 依次跑四项；`copySettingsLocal` 复制 `<repo>/.codebot/settings.local.json`（不存在静默 return），失败 `log.fine`；`configureHooksPath` 优先 `.husky` 回退 `.git/hooks`，找到第一个存在目录后在 worktree 目录里跑 `git config core.hooksPath <hooksPath>`；`symlinkDirectories` 跳过含 `..` 项 + 跳过 src 不存在或 dst 已存在的 + `Files.createSymbolicLink(dst, src)` 错误 `log.fine`；`copyWorktreeIncludeFiles` 读 `.worktreeinclude` 按行收集（跳空行和 `#`）→ 在 repoRoot 跑 `git ls-files --others --ignored --exclude-standard --directory` → 对每行（跳目录和空）`matchesAnyPattern` 判定后 `Files.createDirectories(dst.getParent()) + Files.copy(src, dst)`；`matchesAnyPattern` 支持去前导 `/` 后 exact / basename / dir prefix 三种匹配。
- [ ] 完成

## T5: 实现变更检测 fail-closed
- 影响文件: `src/main/java/com/codebot/worktree/WorktreeChanges.java`（`ChangeSummary` record @ 12；`hasChanges` @ 20-31；`countChanges` @ 38-62；`runGit` @ 64-87）
- 依赖任务: T2
- 完成标准: `ChangeSummary(changedFiles, commits)` record；`hasChanges(wtPath, headCommit)` — `git status --porcelain` 非 null 非空 → true；`git rev-list --count <headCommit>..HEAD` 为 null 或解析后 > 0 → true；任何异常 catch 后返 `true`（**fail-closed**）。`countChanges(wtPath, originalHeadCommit)` — `originalHeadCommit==null||isBlank` 返 null；`status --porcelain` 返 null 时返 null，否则按 `\n` 切并数非空行；`rev-list --count` 返 null 或 `NumberFormatException` 时返 null；否则返 `new ChangeSummary(changedFiles, commits)`。
- [ ] 完成

## T6: 实现 AgentWorktree 静态 API
- 影响文件: `src/main/java/com/codebot/worktree/AgentWorktree.java`（`Result` record @ 20；`create` @ 27-59；`remove` @ 64-89；`buildNotice` @ 95-104；`readHead` @ 106-118）
- 依赖任务: T1, T2, T4
- 完成标准: `Result(worktreePath, worktreeBranch, headCommit, gitRoot)` record；`create(slug, repoRoot, symlinkDirs)` — `SlugValidator.validate` → `wtPath = <repoRoot>/.codebot/worktrees/<flatten(slug)>` + `branch = "worktree-" + flatten(slug)` → `Files.isDirectory(wtPath)` 时快速恢复（`Files.setLastModifiedTime(wtPath, FileTime.from(Instant.now()))` bump mtime + `readHead`）→ 否则 `Files.createDirectories(wtPath.getParent())` + `ProcessBuilder("git","worktree","add","-B",branch,wtPath,"HEAD")` + `PostCreationSetup.perform` → 返 `Result`；**不动 `WorktreeSessionStore`、不切 JVM cwd、不写持久化**。`remove(wtPath, wtBranch, gitRoot)` — gitRoot 空返 false → `ProcessBuilder` 从 `gitRoot.toFile()` 跑 `git worktree remove --force <wtPath>`（**不**从 wtPath 否则把自己删掉）→ 成功后 `Thread.sleep(100)` 等 lockfile → 分支非空跑 `git branch -D <branch>` → 返 true；异常时 `log.fine` 后返 false。`buildNotice(parentCwd, worktreeCwd)` 返固定模板字符串含 `parentCwd` / `worktreeCwd` 占位 + "isolated git worktree" / "translate them" / "Re-read files before editing" / "will not affect the parent's files" 关键句。
- [ ] 完成

## T7: 实现 WorktreeSession + Store
- 影响文件: `src/main/java/com/codebot/worktree/WorktreeSession.java`（record @ 11-20）、`src/main/java/com/codebot/worktree/WorktreeSessionStore.java`（`MAPPER` @ 15；`currentSession` @ 16；`getCurrentSession` @ 20；`restoreSession` @ 24；`save` @ 28-36；`load` @ 38-48；`sessionPath` @ 54-56）
- 依赖任务: 无
- 完成标准: `WorktreeSession` Java record，8 字段 + Jackson `@JsonProperty` snake_case：`original_cwd` / `worktree_path` / `worktree_name` / `worktree_branch` / `original_branch` / `original_head_commit` / `session_id` / `creation_duration_ms`；类标注 `@JsonIgnoreProperties(ignoreUnknown = true)` 兼容字段增减。`WorktreeSessionStore` 用 `private static volatile WorktreeSession currentSession` 保证并发可见；`getCurrentSession` 直接返字段；`restoreSession(WorktreeSession)` 直接写字段（也接受 null 清除）；`save(repoRoot, session)` — session=null 时 `Files.deleteIfExists(sessionPath)`，否则 `Files.createDirectories(parent) + MAPPER.writerWithDefaultPrettyPrinter().writeValue(file, session)`；`load(repoRoot)` 读 `.codebot/worktree_session.json`，不存在返 null，反序列化 `IOException` 返 null；`sessionPath = <repo>/.codebot/worktree_session.json`。
- [ ] 完成

## T8: 实现 EnterWorktreeTool
- 影响文件: `src/main/java/com/codebot/tool/impl/EnterWorktreeTool.java`（`worktreeManager / sessionId / RANDOM` @ 19-21；构造 @ 23-26；`name / category / shouldDefer / description` @ 28-35；`schema` @ 37-52；`execute` @ 54-91）
- 依赖任务: T1, T3, T7
- 完成标准: 实现 `Tool` 接口；`name()="EnterWorktree"`、`category()=ToolCategory.COMMAND`、`shouldDefer()=true`；input schema 仅 `name: string`（可选，max 64 chars 提示）；`execute` guard `WorktreeSessionStore.getCurrentSession() != null` → `ToolResult.error("Already in a worktree session")`；`name` 缺省时用 `RANDOM.nextInt()` 生成 `"wt-" + Integer.toHexString(...)`；`SlugValidator.validate` 失败时返 error；调 `worktreeManager.create(slug, null)` → 组装 `WorktreeSession(System.getProperty("user.dir"), info.path(), slug, info.branch(), "", "", sessionId, 0)` → `restoreSession + save` → 返 `ToolResult.success("Created worktree at <path> on branch <branch>. The session is now working in the worktree. Use ExitWorktree to leave mid-session.")`。
- [ ] 完成

## T9: 实现 ExitWorktreeTool
- 影响文件: `src/main/java/com/codebot/tool/impl/ExitWorktreeTool.java`（`worktreeManager` @ 19；构造 @ 21-23；`name / category / shouldDefer / description` @ 25-32；`schema` @ 34-55；`execute` @ 57-121）
- 依赖任务: T3, T5, T7
- 完成标准: 实现 `Tool` 接口；`name()="ExitWorktree"`、`shouldDefer()=true`；input schema `action: enum["keep","remove"]`（required）+ `discard_changes?: bool`；`execute` scope guard：`getCurrentSession()==null` → `ToolResult.error("No-op: there is no active EnterWorktree session to exit. This tool only operates on worktrees created by EnterWorktree in the current session.")`；变更保护：`action="remove" && !discard_changes` 时 `WorktreeChanges.countChanges`：null 报 `"Could not verify worktree state. Refusing to remove without explicit confirmation. Re-invoke with discard_changes: true, or use action: \"keep\"."`；`changedFiles>0 || commits>0` 时按部分拼接 — `changedFiles==1 ? "file" : "files"` + `commits==1 ? "commit" : "commits"` 单复数正确，用 `String.join(" and ", parts)`；`restoreSession(null) + save(repoRoot, null)`（save 失败 swallow）；`action="remove"` 调 `worktreeManager.remove(session.worktreeName())` 失败返 error，成功返 `"Exited and removed worktree at <path>. Session is now back in <originalCwd>."`；`action="keep"` 返 `"Exited worktree. Your work is preserved at <path>. Session is now back in <originalCwd>."`。
- [ ] 完成

## T10: 接入 SubAgent isolation（AgentTool.runSync）
- 影响文件: `src/main/java/com/codebot/subagent/AgentTool.java`（`worktreeManager` 字段 @ 51；`setWorktreeManager` @ 98-100；`isolation` schema @ 176-180；`execute` 解析 `isolation` @ 228；`runSync` worktree 分支 @ 310-335 和 388-399；`runAsTeammate` worktree 分支 @ 456-472）
- 依赖任务: T5, T6, T3
- 完成标准: `AgentTool.schema()` 中 `properties.put("isolation", Map.of("type","string","enum", List.of("worktree"), ...))`；`execute` 调 `getStringArg(args, "isolation")` 解析；`runSync(spec, description, prompt, modelOverride, isolation)` 在 `"worktree".equals(isolation) && worktreeManager != null` 时：
  1. 用 `SecureRandom` 生成 4 字节 → `HexFormat.of().formatHex(rndBytes).substring(0,7)` → `slug = "agent-a" + 7hex`（匹配 cleanup 正则 `^agent-a[0-9a-f]{7}$`）；
  2. `wtResult = AgentWorktree.create(slug, worktreeManager.getProjectRoot(), worktreeManager.getSymlinkDirs())`；
  3. `subAgent.setWorkDir(wtResult.worktreePath())`；
  4. `notice = AgentWorktree.buildNotice(System.getProperty("user.dir"), wtResult.worktreePath())`；
  5. `prompt = notice + "\n\n" + prompt`；
  6. 创建失败 → `return ToolResult.error("Error creating agent worktree: " + e.getMessage())`；
  `LoopComplete` 事件处理时（`wtResult != null` 分支）调 `WorktreeChanges.hasChanges(wtResult.worktreePath(), wtResult.headCommit())`：true → `wtInfo = "\n\nWorktree kept at <path> (branch <branch>) — has uncommitted changes or new commits."`；false → `AgentWorktree.remove(wtResult.worktreePath(), wtResult.worktreeBranch(), wtResult.gitRoot())`；最后 `result + wtInfo` 拼回。`runAsTeammate` 在 `"worktree".equals(isolation)` 时执行同样三步（创建 + workdir + notice 注入），但**不**做完成后自动清理（teammate 长生命周期，留给 ch15 收尾）。
- [ ] 完成

## T11: 实现后台过期清理
- 影响文件: `src/main/java/com/codebot/worktree/StaleCleanup.java`（`EPHEMERAL_PATTERNS` @ 23-29；`isEphemeral` @ 33-35；`cleanup` @ 41-88；`startCleanupLoop` @ 93-111；`runGitQuiet` @ 113-134）
- 依赖任务: T6
- 完成标准: 五个临时命名正则常量列表：`^agent-a[0-9a-f]{7}$` / `^wf_[0-9a-f]{8}-[0-9a-f]{3}-\d+$` / `^wf-\d+$` / `^bridge-[A-Za-z0-9_]+(-[A-Za-z0-9_]+)*$` / `^job-[a-zA-Z0-9._-]{1,55}-[0-9a-f]{8}$`；`isEphemeral(slug)` 任一匹配返 true。`cleanup(repoRoot, cutoff)` — `dir = <repoRoot>/.codebot/worktrees`，不存在返 0 → 取 `WorktreeSessionStore.getCurrentSession()?.worktreePath()` 作为白名单 → `Files.list(dir)` 遍历每项 `slug = entry.getFileName()`：
  - **L1 命名**：`!isEphemeral(slug)` → continue（用户命名永不删）
  - **L2 时态**：`wtPath.equals(currentPath)` → continue；`Files.readAttributes(entry, BasicFileAttributes.class).lastModifiedTime().toInstant().isAfter(cutoff)` → continue；读 attrs 异常也 continue
  - **L3 git 状态 fail-closed**：`runGitQuiet(wtPath, "--no-optional-locks", "status", "--porcelain", "-uno")` 返 null 或 非空 → continue；`runGitQuiet(wtPath, "rev-list", "--max-count=1", "HEAD", "--not", "--remotes")` 返 null 或非空 → continue
  - 三层通过 → `AgentWorktree.remove(wtPath, SlugValidator.branchName(slug), repoRoot)` 成功 `removed++`；
  末尾 `removed > 0` 时跑 `runGitQuiet(repoRoot, "worktree", "prune")`；返 `removed`。`startCleanupLoop(executor, repoRoot, intervalSeconds, cutoffHours)`：`intervalSeconds <= 0` 直接 return；否则 `executor.scheduleAtFixedRate(task, interval, interval, TimeUnit.SECONDS)`，task 算 `cutoff = Instant.now().minusSeconds(cutoffHours*3600L)` 后调 `cleanup`。
- [ ] 完成

## T12: 接入应用启动装配
- 影响文件: 应用入口（如 `src/main/java/com/codebot/Main.java` 或 TUI 启动器，按项目实际路径）
- 依赖任务: T7, T8, T9, T10, T11
- 完成标准:
  1. 构造 `WorktreeManager(projectRoot, symlinkDirs, staleCutoffHours)`，`projectRoot` 由 `System.getProperty("user.dir")` 或仓库根解析得到；
  2. 注册 `new EnterWorktreeTool(worktreeManager, sessionId)` 和 `new ExitWorktreeTool(worktreeManager)` 到 `ToolRegistry`；
  3. `AgentTool.setWorktreeManager(worktreeManager)` 把 `worktreeManager` 注入到 `AgentTool` 实例；
  4. `WorktreeSession saved = WorktreeSessionStore.load(projectRoot)` → 非 null 且 `Files.exists(Path.of(saved.worktreePath()))` 时 `WorktreeSessionStore.restoreSession(saved)`；
  5. `ScheduledExecutorService cleanupExec = Executors.newSingleThreadScheduledExecutor()` → `StaleCleanup.startCleanupLoop(cleanupExec, projectRoot, intervalSeconds, cutoffHours)`，间隔由配置控制（默认 0 = 不启动）；
  6. 应用退出时 `cleanupExec.shutdown()`。
- [ ] 完成

## T13: 端到端验证
- 影响文件: 无（仅运行）
- 依赖任务: T1-T12
- 完成标准:
  - `./gradlew build` 通过（无编译错误，所有单元测试 PASS）；
  - **路径 A — 工具直接驱动**：主 Agent 调 `EnterWorktree({name:"demo"})` 创建 worktree → 在 worktree 里 `WriteFile + Bash("git commit ...")` → `ExitWorktree({action:"remove"})` 被变更保护拒绝并列出具体 file/commit 数（带正确单复数）→ `ExitWorktree({action:"remove", discard_changes:true})` 强删成功，`.codebot/worktrees/demo` 消失；
  - **路径 B — 子 Agent 自动隔离**：主 Agent 在主目录 `WriteFile witness.txt = "original content from main agent"` → 调 `Agent({subagent_type:"general-purpose", isolation:"worktree", description:"...", prompt:"把 witness.txt 改成 ..."})` → 验证主目录 `witness.txt` 内容不变；`.codebot/worktrees/agent-a*/witness.txt` 是修改后版本；若有 commit → 结果末尾出现 `"Worktree kept at ... (branch worktree-agent-a...) — has uncommitted changes or new commits."`；若无修改 → worktree 自动被 `AgentWorktree.remove` 清理；
  - **持久化与重启**：`EnterWorktree({name:"crashtest"})` 后强杀进程 → `.codebot/worktree_session.json` 仍存在 → 重启后 `WorktreeSessionStore.load + restoreSession` 把 session 写回全局 `volatile` 字段。
- [ ] 完成

## 进度
- [ ] T1 / [ ] T2 / [ ] T3 / [ ] T4 / [ ] T5 / [ ] T6 / [ ] T7 / [ ] T8 / [ ] T9 / [ ] T10 / [ ] T11 / [ ] T12 / [ ] T13

```

```markdown
# ch14: Worktree Checklist（Java 版）

> 所有条目可勾选、可观测。验收方式写在条目后面括号中。验收：已通过验证的项均勾选。

## 1. 实现完整性

- [ ] 常量 `MAX_LENGTH = 64` 在 `src/main/java/com/codebot/worktree/SlugValidator.java:11` 定义
- [ ] 正则 `VALID_SEGMENT = ^[a-zA-Z0-9._-]+$` 在 `src/main/java/com/codebot/worktree/SlugValidator.java:12` 定义
- [ ] 函数 `SlugValidator.validate` 在 `src/main/java/com/codebot/worktree/SlugValidator.java:16-37` 含空 / 长度 / `.`-`..` / 非法段四类 `IllegalArgumentException`
- [ ] 函数 `SlugValidator.flatten` 在 `src/main/java/com/codebot/worktree/SlugValidator.java:39` 把 `/` 替换成 `+`；`branchName` 在 `:43` 加 `worktree-` 前缀
- [ ] record `WorktreeManager.WorktreeInfo(path, branch, createdAt)` 在 `src/main/java/com/codebot/worktree/WorktreeManager.java:25` 定义
- [ ] 函数 `WorktreeManager.create` 在 `src/main/java/com/codebot/worktree/WorktreeManager.java:51-65` 用大写 `-B` 创建 + 调 `PostCreationSetup.perform` + 写内存 map
- [ ] 函数 `WorktreeManager.remove` 在 `src/main/java/com/codebot/worktree/WorktreeManager.java:70-78` 跑 `git worktree remove ... --force`
- [ ] 函数 `WorktreeManager.list` 在 `src/main/java/com/codebot/worktree/WorktreeManager.java:86-97` 先解析 porcelain 输出，失败回退内存 map
- [ ] 函数 `WorktreeManager.parsePorcelain` 在 `src/main/java/com/codebot/worktree/WorktreeManager.java:211-240` 按 blank line 分块，正确处理 `refs/heads/<branch>` 前缀剥离 + 最后一个块无尾随空行
- [ ] 函数 `WorktreeManager.runGit` 在 `src/main/java/com/codebot/worktree/WorktreeManager.java:180-200` 用 `waitFor(60, TimeUnit.SECONDS)` 超时 + 退出非 0 抛 `IOException`
- [ ] 函数 `PostCreationSetup.perform` 在 `src/main/java/com/codebot/worktree/PostCreationSetup.java:19-24` 依序调四项 A/B/C/D
- [ ] 函数 `PostCreationSetup.symlinkDirectories` 在 `src/main/java/com/codebot/worktree/PostCreationSetup.java:60-73` 跳过含 `..` 项 + `Files.createSymbolicLink` 错误 `log.fine`
- [ ] 函数 `PostCreationSetup.copyWorktreeIncludeFiles` 在 `src/main/java/com/codebot/worktree/PostCreationSetup.java:75-106` 单文件失败 catch 不中断（异常被外层 try 包裹）
- [ ] 函数 `PostCreationSetup.matchesAnyPattern` 在 `src/main/java/com/codebot/worktree/PostCreationSetup.java:108-116` 含 exact / basename / dir prefix 三种匹配
- [ ] record `AgentWorktree.Result(worktreePath, worktreeBranch, headCommit, gitRoot)` 在 `src/main/java/com/codebot/worktree/AgentWorktree.java:20` 定义，不含 sessionId
- [ ] 函数 `AgentWorktree.create` 在 `src/main/java/com/codebot/worktree/AgentWorktree.java:27-59` 在已存在时 `Files.setLastModifiedTime(wtPath, FileTime.from(Instant.now()))` bump mtime
- [ ] 函数 `AgentWorktree.create` 中 `ProcessBuilder.environment().put("GIT_TERMINAL_PROMPT","0")` 和 `put("GIT_ASKPASS","")` 在 `:45-46`
- [ ] 函数 `AgentWorktree.remove` 在 `src/main/java/com/codebot/worktree/AgentWorktree.java:64-89` 从 `gitRoot` 跑 `ProcessBuilder.directory()`（不是 wtPath，否则把自己删掉）
- [ ] 函数 `AgentWorktree.remove` 在 `:76` 含 `Thread.sleep(100)` 等 git lockfile 释放
- [ ] 函数 `AgentWorktree.buildNotice` 在 `src/main/java/com/codebot/worktree/AgentWorktree.java:95-104` 含 `parentCwd` / `worktreeCwd` 占位 + "isolated git worktree" / "translate them" / "Re-read files before editing" / "will not affect the parent's files" 关键句
- [ ] record `WorktreeChanges.ChangeSummary(changedFiles, commits)` 在 `src/main/java/com/codebot/worktree/WorktreeChanges.java:12` 定义
- [ ] 函数 `WorktreeChanges.hasChanges` 在 `src/main/java/com/codebot/worktree/WorktreeChanges.java:20-31` 任何异常 catch 后返 true（fail-closed）
- [ ] 函数 `WorktreeChanges.countChanges` 在 `src/main/java/com/codebot/worktree/WorktreeChanges.java:38-62` `originalHeadCommit` null / blank 时返 null，`NumberFormatException` 时返 null
- [ ] record `WorktreeSession` 在 `src/main/java/com/codebot/worktree/WorktreeSession.java:11-20` 含 8 字段且 `@JsonProperty` snake_case 标注
- [ ] 类 `WorktreeSession` 标 `@JsonIgnoreProperties(ignoreUnknown = true)` 兼容字段增减
- [ ] 字段 `WorktreeSessionStore.currentSession` 在 `src/main/java/com/codebot/worktree/WorktreeSessionStore.java:16` 标 `private static volatile`
- [ ] 函数 `WorktreeSessionStore.save` 在 `src/main/java/com/codebot/worktree/WorktreeSessionStore.java:28-36` session=null 时 `Files.deleteIfExists`
- [ ] 函数 `WorktreeSessionStore.load` 在 `src/main/java/com/codebot/worktree/WorktreeSessionStore.java:38-48` `IOException` 时返 null
- [ ] 函数 `WorktreeSessionStore.sessionPath` 在 `src/main/java/com/codebot/worktree/WorktreeSessionStore.java:54-56` 返 `<repo>/.codebot/worktree_session.json`
- [ ] 变量 `StaleCleanup.EPHEMERAL_PATTERNS` 在 `src/main/java/com/codebot/worktree/StaleCleanup.java:23-29` 含五个正则
- [ ] 函数 `StaleCleanup.cleanup` 在 `src/main/java/com/codebot/worktree/StaleCleanup.java:41-88` 三层过滤顺序固定（L1 命名 → L2 时态 → L3 git 状态 fail-closed）
- [ ] 函数 `StaleCleanup.cleanup` 末尾在 `removed > 0` 时跑 `git worktree prune`（`:84-86`）
- [ ] 函数 `StaleCleanup.startCleanupLoop` 在 `src/main/java/com/codebot/worktree/StaleCleanup.java:93-111` `intervalSeconds <= 0` 直接 return
- [ ] 函数 `StaleCleanup.runGitQuiet` 在 `:113-134` 含 `GIT_TERMINAL_PROMPT=0` + `GIT_ASKPASS` 安全壳
- [ ] 类 `EnterWorktreeTool` 在 `src/main/java/com/codebot/tool/impl/EnterWorktreeTool.java:17` 实现 `Tool` 接口，含 `worktreeManager` + `sessionId` 字段，`shouldDefer()` 返 true
- [ ] 类 `ExitWorktreeTool` 在 `src/main/java/com/codebot/tool/impl/ExitWorktreeTool.java:17` 实现 `Tool` 接口，含 `worktreeManager` 字段，`shouldDefer()` 返 true
- [ ] `ExitWorktreeTool.schema` 在 `src/main/java/com/codebot/tool/impl/ExitWorktreeTool.java:34-55` 含 `action: enum["keep","remove"]`（required）+ `discard_changes?: bool`
- [ ] `ExitWorktreeTool.execute` 在 `:81-95` 实现 file/files 和 commit/commits 单复数正确处理
- [ ] `AgentTool` 字段 `worktreeManager` 在 `src/main/java/com/codebot/subagent/AgentTool.java:51` 定义，setter `setWorktreeManager` 在 `:98-100`
- [ ] `AgentTool.runSync` 在 `src/main/java/com/codebot/subagent/AgentTool.java:319-335` 用 `SecureRandom` + `HexFormat.formatHex(...).substring(0,7)` 生成 `agent-a<7hex>` slug
- [ ] `AgentTool.runSync` 在 `:388-399` 完成时按 `WorktreeChanges.hasChanges` 决定保留还是 `AgentWorktree.remove`
- [ ] `AgentTool.runAsTeammate` 在 `src/main/java/com/codebot/subagent/AgentTool.java:456-472` 创建 worktree + workdir + notice 注入，但**不**自动清理

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `grep -rn "EnterWorktreeTool" --include="*.java" src/` 在应用启动入口（`Main.java` 或 TUI 启动器）找到 `new EnterWorktreeTool(...)` 注册调用
- [ ] `grep -rn "ExitWorktreeTool" --include="*.java" src/` 在应用启动入口找到 `new ExitWorktreeTool(...)` 注册调用
- [ ] `grep -rn "WorktreeSessionStore.load" --include="*.java" src/` 在应用启动入口找到调用方
- [ ] `grep -rn "WorktreeSessionStore.restoreSession" --include="*.java" src/` 同时在 `EnterWorktreeTool` / `ExitWorktreeTool` / 启动恢复处找到调用
- [ ] `grep -rn "StaleCleanup.startCleanupLoop" --include="*.java" src/` 在应用启动入口找到调用方
- [ ] `grep -rn "AgentWorktree.create" --include="*.java" src/` 在 `src/main/java/com/codebot/subagent/AgentTool.java:325` 和 `:463` 找到两处调用（runSync + runAsTeammate）
- [ ] `grep -rn "AgentWorktree.buildNotice" --include="*.java" src/` 同上两处调用（runSync 在 `:329`，runAsTeammate 在 `:466`）
- [ ] `grep -rn "WorktreeChanges.hasChanges" --include="*.java" src/` 在 `src/main/java/com/codebot/subagent/AgentTool.java:391` 找到主流程调用方（决定 remove 还是保留）
- [ ] `grep -rn "AgentWorktree.remove" --include="*.java" src/` 在 `AgentTool.java:396` 和 `StaleCleanup.java:76` 找到调用方
- [ ] `grep -rn "WorktreeChanges.countChanges" --include="*.java" src/` 在 `src/main/java/com/codebot/tool/impl/ExitWorktreeTool.java:74` 找到唯一调用方（变更保护错误信息）
- [ ] `grep -rn "setWorktreeManager" --include="*.java" src/` 在应用启动入口找到注入调用（把 WorktreeManager 注入 AgentTool）

## 3. 编译与测试

- [ ] `./gradlew build` 通过
- [ ] `./gradlew test --tests "com.codebot.worktree.*"` 通过（SlugValidator / WorktreeManager / PostCreationSetup / AgentWorktree / WorktreeChanges / StaleCleanup / WorktreeSessionStore 各对应测试 PASS）
- [ ] `./gradlew test --tests "com.codebot.subagent.*"` 通过（含 isolation 集成测试）
- [ ] `./gradlew test --tests "com.codebot.tool.impl.EnterWorktreeToolTest"` 和 `ExitWorktreeToolTest` 通过

## 4. 端到端验证

- [ ] **路径 A — 工具直接驱动**：用户对主 Agent 说"用 EnterWorktree 工具创建一个名叫 demo 的工作树" → LLM 调 `EnterWorktree({name:"demo"})` → 返回 `Created worktree at .../.codebot/worktrees/demo on branch worktree-demo. The session is now working in the worktree. Use ExitWorktree to leave mid-session.`；让 Agent 在 worktree 里创建 `hello.txt` 并 `git commit`；让 Agent 调 `ExitWorktree({action:"remove"})` → 因有未推送 commit 被变更保护拒绝，错误文本包含具体 file/commit 数和单复数；`ExitWorktree({action:"remove", discard_changes:true})` 强删成功；`ls .codebot/worktrees/` 看到 `demo/` 已消失。
- [ ] **路径 B — 子 Agent 自动隔离**：用户让主 Agent 在主目录建 `witness.txt`（内容 "original content from main agent"）→ 调 `Agent({subagent_type:"general-purpose", isolation:"worktree", description:"...", prompt:"把 witness.txt 改成 \"modified by isolated worker\"，然后 git 提交"})`；验证 `cat witness.txt` 主目录内容仍是 "original ..."；`cat .codebot/worktrees/agent-a*/witness.txt` 是修改后版本；若子 Agent 有 commit → 结果末尾出现 `"Worktree kept at ... (branch worktree-agent-a...) — has uncommitted changes or new commits."`；若无修改 → worktree 自动清理（`.codebot/worktrees/` 下 `agent-a*` 目录消失）。
- [ ] **持久化与 crash 恢复**：`EnterWorktree({name:"crashtest"})` 创建 worktree → `kill -9` 杀 JVM 进程 → `cat .codebot/worktree_session.json` 文件仍在并含 crashtest 会话；重启应用 → 启动期间 `WorktreeSessionStore.load + restoreSession` 将 session 写回全局 `volatile` 字段；下一次工具调用时 `WorktreeSessionStore.getCurrentSession()` 非 null。
- [ ] **变更保护单复数**：在 worktree 里建 1 个未提交修改 → `ExitWorktree({action:"remove"})` 返回 `"1 uncommitted file"`；建 2+ 个修改 → 返回 `"N uncommitted files"`；同样验证 commit 数的单复数（`"1 commit"` / `"N commits"`）。
- [ ] **后台清理保守不删**：手动在 `.codebot/worktrees/agent-aabcdef1/` 下建一个有未推送 commit 的目录（mtime 设为过期前）→ 等 cleanup loop 跑一轮（或手动调 `StaleCleanup.cleanup(repoRoot, Instant.now())` 测试）→ 该目录仍保留（L3 fail-closed 拦住）。
- [ ] **用户命名永不删**：在 `.codebot/worktrees/my-feature/` 下建一个目录（mtime 设为非常老）→ 跑 cleanup → 目录仍保留（L1 命名过滤拦住）。

## 5. 文档

- [ ] `docs/java/ch14/spec.md` 已按 ch13 风格写完（F1-F17 + N1-N8，无 file:line 代码标注）
- [ ] `docs/java/ch14/tasks.md` 已写，13 个 T 全部勾完（T1-T13）
- [ ] `docs/java/ch14/checklist.md` 已写并逐项验收
- [ ] commit 信息标注 `ch14`，新增代码的调用链已在 PR 描述或 commit message 里说明

```



## ch15

```markdown
# 我的初步想法
- 抽象出一个长期存在的"小组"对象，承载名称、负责人、成员花名册和持久化位置；成员级别记录角色、工作目录、运行后端、是否需要审批等元信息
- 提供多种成员运行后端：可在独立终端窗格里跑一个完整 CLI 实例（强隔离），也可在同进程里以协程方式轻量运行；运行位置按环境优先级自动选择，不静默降级
- 给小组成员发放一组协作工具——共享任务的创建/查看/列举/更新（带可选依赖字段）以及点对点消息发送；主入口和普通子 Agent 看不到这些工具
- 点对点消息走"名称注册表 + 邮箱文件"两段式：通过名称解析到目标实例 ID，写入对应邮箱；独立进程后端额外唤醒目标窗格；支持广播、纯文本带摘要、以及若干结构化协议消息（生命周期、审批回复）
- 把发起方设计成 Lead：它负责把用户目标拆成任务并写入共享清单（含先后依赖），派生成员，全部完成后通过 git 合并各人的工作目录、解决能搞定的冲突、搞不定就回滚上报
- 成员完成自然停止后标记为空闲并通知 Lead；Lead 之后通过发消息即可从磁盘恢复其上下文继续指派新工作，而不是重头再 spawn
- 单独提供一种"纯调度"开关（双重锁定才生效）：开启后剥夺发起方的代码读写与 shell 工具，只留派人/终止/发消息/输出结果，并注入多阶段工作流指引，把理解与综合留在发起方手里
```

### Go

```markdown
# ch15: AgentTeam Spec

## 1. 背景

SubAgent（ch13）解决了一次性子任务的上下文隔离，但拓扑是星型：所有子 Agent 只能和主 Agent 通信，子 Agent 之间彼此看不见。当任务规模上来——四个模块同时重构、多角度并行调查 bug、一个 Agent 需要把发现告诉另一个——星型拓扑下主 Agent 成了信息中转瓶颈，子任务被迫串行。这一章把"长期协作团队"做成 CodeBot 的一等概念：多个 Agent 组成 Team，并行干活、直接互发消息、共享任务列表，主 Agent 升级为 Team Lead 专职调度。

## 2. 目标

提供 `Team` / `TeamManager` / `FileMailBox` / `SendMessageTool` / `TeamCreateTool` / `TeamDeleteTool` 一整套类型与工具，让 LLM 在对话里：1) 调 `TeamCreate` 建团队（按环境自动选 tmux / iTerm2 / in-process 后端），2) 调 `Agent` 工具带 `team_name` 把队员 spawn 进团队，3) 队员之间通过 `SendMessage` 走 `FileMailBox` 互发消息、idle 后通知 Lead，4) Lead 进入 Coordinator Mode 只调度不动代码。tmux / iTerm 后端时由 `cmd/codebot --teammate` 子模式启动队员工作进程，与 Lead 通过同一份 mailbox 目录通信。

## 3. 功能需求

- F1: `TeamMode` 三档常量 `in-process` / `tmux` / `iterm`；`detectBackend()` 按 `TMUX` env → `ITERM_SESSION_ID` env → `tmux` 可执行文件 → in-process 的优先级自动选择。
- F2: `Team` 持有 `Name / Mode / Members map / MailBox`，`Member` 含 `Name / AgentRef / Conv / Active / Cancel / PaneID`，外部后端 Member 仅留 PaneID 句柄、AgentRef/Conv 为空。
- F3: `TeamManager` 提供 `CreateTeam` / `GetTeam` / `DeleteTeam` / `ListTeams` / `CloseAll` + `CreateTeamWith`（让外部工作进程注册自己本地构造的 Team 对象，共享同一 mailbox 目录）。
- F4: `FileMailBox` 基于 `<baseDir>/<agentID>.json` 文件持久化消息；`Send` / `ReadUnread` / `MarkAllRead` 三件套；并发安全靠 `<agentID>.json.lock` 文件锁（O_CREATE|O_EXCL，10 次重试，>10s 视为过期）。
- F5: `FileMailMessage` 字段 `From / Text / Timestamp / Read / Color / Summary`；`Read=false` 落盘后由 `MarkAllRead` 批量翻转，区分已读与未读。
- F6: `SpawnTeammate(ctx, TeammateSpawnConfig)` 统一入口按 `Team.Mode` 分发到 in-process / tmux / iTerm 三条路径，返回 `SpawnResult{Mode, EventCh, PaneID}`。
- F7: In-process 路径走 `StartInProcessMember`：`team.AddMember` 注册成员、启动 goroutine 跑 `RunInProcessTeammate`、返回一个事件 channel；可选 `Workdir` 覆盖 `Agent.WorkDir` 配合 worktree 隔离。
- F8: 外部后端路径（tmux / iTerm）启动前把 `Task` 作为初始消息写入对方 mailbox（队员进程启动后第一次 idle poll 拿到）；用 `BuildTeammateCLI` 拼出 `cd <wd> && codebot --teammate --team-name X --agent-name Y` 命令字符串；`shellQuote` 用单引号包裹安全转义；`spawnTmuxTeammate` 调 `tmux new-window -d`，`spawnITermTeammate` 调 `osascript` 创建 iTerm tab；返回 paneID/tabID 落到 `Member.PaneID`。
- F9: `RunInProcessTeammate` 队员主循环：每一轮先用 `InjectPendingMessages` 把未读邮件转 system reminder 注入对话、再把 `nextPrompt` 加为 user message、调 `agent.Run` 跑一轮、转发事件给 `eventOut`、本轮结束写 idle 通知到 Lead 邮箱、`waitForNextPromptOrShutdown` 用 `IdlePollInterval (500ms)` 轮询直到来新消息或 shutdown 才进入下一轮。
- F10: `IsShutdownRequest` 用 `[shutdown]` 前缀判定（`ShutdownPrefix` 常量）；`CreateIdleNotification(member, reason)` 产出 `From=member`、`Text="[idle] member (reason: ...)"`、`Summary="idle"` 的标准消息；`reason` 在 ErrorEvent 出现时翻成 `failed`，否则默认 `available`。
- F11: `DrainLeadMailbox(mgr)` 扫所有团队的 Lead 收件箱（`LeadName="lead"` 常量），把未读消息按 `<team-notification team="X">\nfrom=Y: text\n...\n</team-notification>` 包装返回字符串数组，并把消息标记为已读；nil 安全。挂在 `Agent.NotificationFn` 上每轮 Lead 迭代之前自动抽取。
- F12: `BuildTeammateAddendum(team, member, others)` 产出注入到队员对话顶端的 system reminder，告诉它身份、Lead 是 `LeadName`、其他队友名字、必须通过 `SendMessage` 沟通且最终结果发给 Lead；和 ch13 子 Agent 的 fork boilerplate 一样是字面常量，不带任何调度细节。
- F13: `CoordinatorAllowedTools` map + `IsCoordinatorTool(name)` 函数；TUI 把 `coordinatorToolFilter(teamMgr)` 装到 `Agent.ToolNameFilter` 上：只要至少一个团队存在，Lead 的每轮工具集就被收窄到该白名单（`Agent` / `SendMessage` / `TeamCreate` / `TeamDelete` / `TaskCreate` 等 + 读类 `ReadFile` / `Glob` / `Grep` / `Bash`），全部团队清理后下一轮恢复全工具集。
- F14: `SendMessageTool` 暴露 `to` + `content` 两个字段；`to == LeadName` 走"找发送者所在团队"路径（Lead 不是 Member，不在任何团队 members map 里）；其它情况遍历所有团队找到 `to` 这个 member 所在团队，写入对方 mailbox。未匹配返 IsError 文案。
- F15: `TeamCreateTool` 暴露 `team_name` 必填、`description` 可选；同名冲突自动追加 `-2/-3/...` 后缀；调 `detectBackend()` + `TeamMgr.CreateTeam`；返回包含 mode 提示和下一步指引的 Output。
- F16: `TeamDeleteTool` 暴露 `team_name`；调 `TeamMgr.DeleteTeam`，内部循环 `StopMember` 把每个成员 stop（in-process 队员调 `cancel`、tmux 队员调 `stopTmuxTeammate` 发 C-c + kill-window、iTerm 队员调 `stopITermTeammate` 用 osascript 关 tab），返回 stopped 成员清单。
- F17: `AgentTool.runAsTeammate` 在主 Agent 的 `Agent` 工具调用里识别 `team_name`：先查团队是否存在、查重名、解析可选 `subagent_type` 走 `FilterToolsForAgent` 获取队员子工具池、空 name 时由 `sanitizeSlugSegment(description)` 生成；可选 `isolation=worktree` 时建独立 worktree 并把 notice 拼到 prompt 顶端；最后调 `teams.SpawnTeammate`，in-process 模式启 `drainTeammateEvents` goroutine 把事件流派进 `ProgressCh` 防止生产者阻塞。
- F18: `cmd/codebot --teammate --team-name X --agent-name Y` worker 模式：`parseTeammateFlags` 仅识别这三个 flag，命中则跳过 TUI；`runTeammate` 加载同一份 config、注册 worker 工具白名单（无 TeamCreate/TeamDelete，仅 ReadFile/WriteFile/EditFile/Bash/Glob/Grep + SendMessage）、构造本地 Team 对象指向同一 mailbox 目录、AddMember 后跑 `RunInProcessTeammate`，事件 channel 走 `streamEventsToStderr` 喷到 stderr 让 tmux/iTerm pane 看见输出；接 SIGINT/SIGTERM 优雅退出。

## 4. 非功能需求

- N1: FileMailBox 跨进程并发安全——tmux 启动的队友进程和 Lead 进程不共享内存，必须靠文件锁保证写入原子性。锁文件 10 秒过期自动清理避免死锁。
- N2: 外部后端队员的初始任务必须在 spawn 之前写入 mailbox，因为 tmux/iTerm 新进程启动到第一次 idle poll 期间无法接消息；先写后启即可保证第一次 poll 必命中。
- N3: In-process 队员的事件 channel 在 `runAsTeammate` 路径上必须由后台 goroutine 持续消费（`drainTeammateEvents`），否则带缓冲 channel 满了之后 `RunInProcessTeammate` 主循环会卡在 select 上无法推进。
- N4: Coordinator Mode 通过 `ToolNameFilter` 在每轮迭代开头动态判定，而非一次性裁剪 registry。这样团队全部 Delete 后下一轮 Lead 自动恢复全工具集，无需重新构造 registry。
- N5: 队员的 `BuildTeammateAddendum` 必须明确告诉 LLM "纯文本回复对队友不可见，最终结果必须通过 `SendMessage(to=LeadName)` 发给 Lead"——否则队员模型容易写一段汇报作为最后输出就结束，Lead 永远拿不到结果（只能看到 idle 通知）。
- N6: `SendMessage(to="lead")` 不能走"在 Members 里找名字"的路径，因为 Lead 不是 Member。必须用"发送者所在团队的 mailbox.Send(LeadName, ...)"路径，否则永远报 `recipient 'lead' not found`。
- N7: `BuildTeammateCLI` 必须把 `team_name` / `agent_name` / `workdir` 都通过 `shellQuote` 单引号包裹，否则空格 / 特殊字符的 workdir 路径会破坏 shell 解析；单引号内的单引号用 `'\''` 闭合再续接的标准 POSIX 写法转义。
- N8: `iterm.go` 里的 AppleScript 字面量必须把内嵌的双引号转义为 `\"`，否则 `osascript -e` 解析失败；关闭流程是 best-effort，找不到 tab 不应报错（用户可能手动关掉了）。
- N9: `RunInProcessTeammate` 退出路径有三条：ctx 取消（返 ctx.Err）、收到 shutdown 消息（返 nil）、`agent.Run` 内部循环正常结束（继续下一轮）。退出时 `StartInProcessMember` 的 defer 必须置 `member.Active=false` 并关闭事件 channel，否则 UI 端永远等不到 close。
- N10: 测试运行时 `TestMain` 必须把 `CODEBOT_TEAMS_DIR` 指到 tmp 目录，否则跑完测试会在仓库根残留 `.codebot/teams/` 目录。

## 5. 设计概要

- 核心数据结构:
 - `Team`：团队聚合，持有 `Mode` 决定后端、`Members map[string]*Member` 注册表、`MailBox *FileMailBox` 通信媒介、`mu sync.Mutex` 保护 Members 读写。
 - `Member`：队员元信息，in-process 模式下 `AgentRef + Conv` 有值（LLM 跑在本进程 goroutine），tmux / iTerm 模式下两者为空、`PaneID` 是外部句柄。
 - `TeamManager`：全局团队注册表，`teams map[string]*Team + mu sync.Mutex`，给 Lead 进程和 worker 进程共用一份接口。
 - `FileMailBox` + `FileMailMessage`：文件锁 + JSON 数组的 mailbox 实现，跨进程共享同一目录。
 - `TeammateSpawnConfig` / `SpawnResult`：`SpawnTeammate` 的入参/出参，把 in-process（`EventCh`）和外部后端（`PaneID`）的差异合并到同一返回类型。
 - `CoordinatorAllowedTools`：12 项白名单 map，TUI 的 `coordinatorToolFilter` 闭包按团队存活与否每轮重判。
- 主流程（按生命周期）:
 - 创建：用户消息 → 主 Agent → LLM 调 `TeamCreate(team_name)` → `detectBackend()` 选模式 → `TeamMgr.CreateTeam` 落到 `~/.codebot/teams/<name>/inboxes/` → 返回 mode 提示给 Lead。
 - Spawn 队员：Lead LLM 调 `Agent(team_name=X, name=Y, prompt=Z)` → `AgentTool.runAsTeammate` → 解析 spec + 子工具集 + worktree → `BuildTeammateAddendum` → `teams.SpawnTeammate` → 按 mode 分发。
 - In-process：goroutine 跑 `RunInProcessTeammate`，事件 channel 由 `drainTeammateEvents` 后台消费。
 - 外部后端：先把初始任务写 mailbox → `BuildTeammateCLI` 拼命令 → `tmux new-window` / `osascript create tab` → 新进程跑 `cmd/codebot --teammate` 走 `runTeammate` → 第一次 idle poll 命中初始消息开始干活。
 - 通信：队员 → `SendMessage` 工具 → 找对方所在团队 → `team.MailBox.Send` 写文件。队员收信走 `RunInProcessTeammate` 顶端的 `InjectPendingMessages`。
 - Lead 感知：每轮 Lead Agent 开头调 `NotificationFn` → `DrainLeadMailbox` → 抽 Lead 邮箱所有未读 → 包成 `<team-notification>` system reminder 喂回 LLM。
 - Coordinator Mode：只要 `teamMgr.ListTeams()` 非空，`ag.ToolNameFilter` 就过滤掉非白名单工具，Lead 自动从"既写代码又调度"变成"只调度"。
 - Stop：`TeamDelete` 工具 → `TeamMgr.DeleteTeam` → 遍历 `team.StopMember` → 按 `Mode + PaneID` 分发 `stopTmuxTeammate` / `stopITermTeammate` / 直接 cancel context。
- 调用链（模块层级）:
 - TUI 装配 → `registerAgentTools` 里 `teams.NewTeamManager()` → 注册 `TeamCreateTool` / `TeamDeleteTool` / `SendMessageTool` 三个工具 → 把 `teamMgr` 注入 `agents.AgentTool.TeamMgr`
 - Agent loop 在 `gatherNotifications` 里把 `teams.DrainLeadMailbox(m.teamMgr)` 的结果拼到消息流（tui.go:545）
 - Agent 初始化 / 恢复会话两处都给 `ag.ToolNameFilter = coordinatorToolFilter(m.teamMgr)`（tui.go:387 + 1132）
 - 外部工作进程入口 `cmd/codebot/main.go` 先 `parseTeammateFlags` 截胡，命中走 `runTeammate` 不进 TUI
- 与其他模块的交互:
 - 依赖 `internal/agent`（Agent 实例 / AgentEvent 流）、`internal/conversation`（Conv manager）、`internal/llm`（Client）、`internal/tools`（Registry / Tool 接口）、`internal/worktree`（可选隔离）
 - 被 `internal/agents`（AgentTool.runAsTeammate）、`internal/tui`（注册 + drain + filter）、`cmd/codebot`（worker 模式 + main 路由）调用

## 6. Out of Scope

- 不实现 PR 文档里描述的 `TeammateInfo` 完整模型（`agentType / model / planModeRequired` 字段、planModeRequired 审批工作流）——本章只做工具链层面的 Team / Member 骨架。
- 不实现 `plan_approval_response` / `shutdown_response` 结构化消息类型——目前仅 `[shutdown]` 文本前缀 + 文本消息两种。
- 不实现共享任务依赖图字段（`addBlocks` / `addBlockedBy`）——任务依赖由队员从 TaskList 文本里自己推断，或 Lead 通过描述文本约定。
- 不实现 `agentNameRegistry` 全局名称注册表——`SendMessage` 通过 `TeamMgr.ListTeams()` 遍历查找，团队规模小不需要 O(1) 索引。
- 不实现队员"空闲后从磁盘恢复对话"的续写机制——in-process 队员在 ctx 取消或收到 shutdown 后即终止，Lead 想再用需要重新 spawn；目前 transcript 不持久化。
- 不实现 `CODEBOT_COORDINATOR_MODE` 环境变量 + `COORDINATOR_MODE` feature flag 双锁——只要团队存在 Lead 就自动进入 Coordinator Mode，是单锁。
- 不实现"协调模式四阶段工作流"系统提示词注入（Research / Synthesis / Implementation / Verification）——`coordinatorToolFilter` 仅做工具收窄，不做提示词增强。
- 不实现"配置持久化到 ~/.codebot/teams/<name>/config.json" 的团队元数据——只持久化邮箱 JSON，Team 实例本身随进程退出消失。
- 不实现 Worktree 团队层面的"收敛阶段 Lead 用 Bash 跑 git merge"自动化——合并由 Lead LLM 自己用 Bash 工具完成，本章不做封装。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch15: AgentTeam Tasks

> 任务粒度：每个任务可在一次会话内完成，可独立交付。

## T1: 定义 Team / Member / TeamMode / TeamManager
- 影响文件: `internal/teams/teams.go`（`TeamMode` @ 17；`ModeInProcess / ModeTmux` 常量 @ 19-22；`teamsBaseDir` @ 24-30；`Member` @ 32-41；`Team` @ 43-49；`NewTeam` @ 51-59；`AddMember` @ 61-74；`StartMember` @ 76-92；`StopMember` @ 94-116；`SendMessage` @ 118-124；`TeamManager` @ 126-129；`NewTeamManager` @ 131-133；`CreateTeam` @ 135-141；`CreateTeamWith` @ 147-151；`GetTeam` @ 153-157；`DeleteTeam` @ 159-168；`ListTeams` @ 170-178；`CloseAll` @ 180-189）
- 依赖任务: 无
- 完成标准: `Team` 持有 `Name / Mode / Members map / MailBox / mu`；`Member.PaneID` 字段存在；`StopMember` 按 `Mode` + `PaneID` 分流 `stopTmuxTeammate / stopITermTeammate`，最后置 `Active=false`；`TeamManager.CreateTeamWith` 接受外部构造的 Team 注册（给 worker 进程用）；`teamsBaseDir` 支持 `CODEBOT_TEAMS_DIR` env 覆盖。
- [ ] 完成

## T2: 实现 FileMailBox（JSON + 文件锁）
- 影响文件: `internal/teams/filemailbox.go`（`FileMailBox` @ 11；`FileMailMessage` @ 15-22；`NewFileMailBox` @ 24-27；`inboxPath` @ 29-31；`lockPath` @ 33-35；`Send` @ 37-45；`ReadUnread` @ 47-59；`MarkAllRead` @ 61-68；`withLock` @ 71-111；`readInbox` @ 113-127；`writeInbox` @ 129-136）
- 依赖任务: 无
- 完成标准: 每个收件人对应 `<baseDir>/<agentID>.json`；`Send` 落盘时把消息 `Read` 强制置 false 并补 `Timestamp`；`MarkAllRead` 批量翻转 `Read=true`；并发安全靠 `<agentID>.json.lock` 文件用 `O_CREATE|O_EXCL` 加锁，10 次重试间隔 5-100ms 随机，>10s 视为过期锁强制删；`withLock` 在 `fn` 返回前 defer 删锁文件。
- [ ] 完成

## T3: 实现 detectBackend 自动选择
- 影响文件: `internal/teams/backend.go`（`detectBackend` @ 8-21）
- 依赖任务: T1
- 完成标准: 优先级 `TMUX` env → `ITERM_SESSION_ID` env → `exec.LookPath("tmux")` → `ModeInProcess`；前两者非空直接返回对应模式；都不命中时退化到 in-process。注意检测失败不会自动报错，只是退化。
- [ ] 完成

## T4: 实现 Tmux 后端
- 影响文件: `internal/teams/tmux.go`（`spawnTmuxTeammate` @ 9-19；`stopTmuxTeammate` @ 21-24）
- 依赖任务: T1
- 完成标准: `spawnTmuxTeammate` 用 `tmux new-window -d -n <teamName>-<memberName> <cliCommand>` 创建后台窗口；命令失败返 `"tmux new-window: %s: %s"` 错误；`stopTmuxTeammate` 先 `send-keys -t <pane> C-c` 再 `kill-window -t <pane>`，best-effort 不返回错误。
- [ ] 完成

## T5: 实现 iTerm2 后端 + ModeITerm
- 影响文件: `internal/teams/iterm.go`（`ModeITerm` 常量 @ 11；`spawnITermTeammate` @ 16-39；`stopITermTeammate` @ 43-55）
- 依赖任务: T1
- 完成标准: `ModeITerm TeamMode = "iterm"` 定义在 iterm.go 而非 teams.go（保持后端代码内聚）；`spawnITermTeammate` 用 `osascript -e` 调 AppleScript 在当前 iTerm window 创建新 tab 设 name 并 `write text <cliCommand>`，内嵌双引号转义为 `\"`；`stopITermTeammate` 用 AppleScript 遍历所有 window 的所有 tab 找 name 匹配的 tab close 掉，best-effort 失败静默。
- [ ] 完成

## T6: 实现队员主循环 RunInProcessTeammate
- 影响文件: `internal/teams/runner.go`（`RunInProcessTeammate` @ 60-123；`waitForNextPromptOrShutdown` @ 130-162；`formatInboundAsPrompt` @ 205-215）
- 依赖任务: T1, T2, T8
- 完成标准: 主循环 6 步——1) `ctx.Err()` 检查；2) `InjectPendingMessages` 把未读邮件作为 system reminder 注入；3) 把 `nextPrompt` 加为 user message；4) `agent.Run` 跑一轮并转发事件到 `eventOut`，ErrorEvent 把 `idleReason` 改成 `failed`；5) 写 idle 通知到 Lead 邮箱；6) `waitForNextPromptOrShutdown` 用 `IdlePollInterval` 轮询邮箱直到来新消息（构建下一轮 prompt 继续）或 shutdown（返 nil 退出）或 ctx 取消（返 ctx.Err）。`formatInboundAsPrompt` 把消息按 `"From <sender>: \n\n"` 拼成 prompt，空列表返空串。
- [ ] 完成

## T7: 实现 Lead-side 通信原语
- 影响文件: `internal/teams/runner.go`（`LeadName` 常量 @ 16；`ShutdownPrefix` 常量 @ 21；`IdlePollInterval` 常量 @ 25；`IsShutdownRequest` @ 30-32；`CreateIdleNotification` @ 37-44；`DrainLeadMailbox` @ 169-199）
- 依赖任务: T1, T2
- 完成标准: `LeadName = "lead"`、`ShutdownPrefix = "[shutdown]"`、`IdlePollInterval = 500*time.Millisecond` 三个常量字面值保持一致；`IsShutdownRequest` 用 `strings.HasPrefix(TrimSpace(text), ShutdownPrefix)` 判定；`CreateIdleNotification` 产出 `From=name / Text="[idle] <name> (reason: <r>)" / Summary="idle" / Timestamp`；`DrainLeadMailbox(nil)` 返 nil；非空时遍历所有团队读 Lead 邮箱，按 `<team-notification team="X">\nfrom=Y: text\n...\n</team-notification>` 包装返字符串数组，并把读过的标记为已读。
- [ ] 完成

## T8: 实现 In-process Bootstrap
- 影响文件: `internal/teams/inprocess.go`（`StartInProcessMember` @ 22-49；`BuildTeammateAddendum` @ 55-66；`InjectPendingMessages` @ 72-86）
- 依赖任务: T1, T2, T6
- 完成标准: `StartInProcessMember` 调 `team.AddMember` 注册队员 → `context.WithCancel` 绑定到 `member.Cancel` → 起 goroutine 跑 `RunInProcessTeammate`，defer 同时关闭 `eventCh` 和置 `Active=false`（取 team.mu 锁）；事件 channel 缓冲 32；`BuildTeammateAddendum` 文本必须包含队员名字 / Lead 名字 / "纯文本回复对队友不可见，最终结果必须 SendMessage 给 Lead" 三个关键信息；`InjectPendingMessages` 在有未读时返 `"You have new messages:\n\n..."` 并把消息 MarkAllRead，无未读返空串。
- [ ] 完成

## T9: 实现 SpawnTeammate 统一入口
- 影响文件: `internal/teams/spawn.go`（`TeammateSpawnConfig` @ 23-34；`SpawnResult` @ 39-43；`SpawnTeammate` @ 53-123；`recordExternalMember` @ 130-138）
- 依赖任务: T1, T4, T5, T8
- 完成标准: `SpawnTeammate` 校验 Team / MemberName 必填；按 `Team.Mode` switch 三档分发；`ModeInProcess` 调 `StartInProcessMember` 返 `EventCh`，Workdir 非空时把 `member.AgentRef.WorkDir` 覆盖为 workdir；`ModeTmux` 和 `ModeITerm` 先把 `Task` 写进对方 mailbox（Worker 进程启动后第一次 idle poll 拿到）→ `BuildTeammateCLI` 拼命令 → 调对应 spawn 函数拿 paneID → `recordExternalMember` 注册成员（仅留名字 + paneID + Active=true，AgentRef/Conv 为空）→ 返 `SpawnResult{Mode, PaneID}`；未知 mode 返错误。
- [ ] 完成

## T10: 实现 BuildTeammateCLI + shellQuote
- 影响文件: `internal/teams/spawn.go`（`BuildTeammateCLI` @ 149-164；`shellQuote` @ 169-177）
- 依赖任务: T9
- 完成标准: `BuildTeammateCLI` 用 `os.Executable()` 拿到当前二进制路径；workdir 空时默认 `os.Getwd()`；返回 `cd <quoted_wd> && <quoted_exe> --teammate --team-name <quoted_team> --agent-name <quoted_member>`；所有变量值都过 `shellQuote`。`shellQuote("")` 返 `''`，无特殊字符返原值，含 ` \t\n'"\$\`` 任一字符返 `'<value 内单引号替换为 '\''>'`。
- [ ] 完成

## T11: 实现 Coordinator Mode 工具白名单
- 影响文件: `internal/teams/coordinator.go`（`CoordinatorAllowedTools` @ 13-26；`IsCoordinatorTool` @ 29-31）
- 依赖任务: 无
- 完成标准: 12 项白名单 map：`Agent / SendMessage / TaskCreate / TaskGet / TaskList / TaskUpdate / TeamCreate / TeamDelete / ReadFile / Glob / Grep / Bash`；`IsCoordinatorTool(name)` 返回 map 命中布尔（写工具 `WriteFile / EditFile / NotebookEdit` 不在内）。
- [ ] 完成

## T12: 实现 SendMessage / TeamCreate / TeamDelete 三个工具
- 影响文件: `internal/teams/tools.go`（`SendMessageTool` @ 12-91；`TeamCreateTool` @ 94-145；`TeamDeleteTool` @ 148-199）
- 依赖任务: T1, T3
- 完成标准:
 - `SendMessageTool.Execute`：`to/content` 必填；`to == LeadName` 走 "查发送者所在团队"（Lead 不是 Member），用该团队 `SendMessage(sender, LeadName, content)` 投递；其它情况遍历所有团队找 `to` 这个 member 所在团队投递；都没找到返 `recipient '%s' not found in any team` IsError。
 - `TeamCreateTool.Execute`：`team_name` 必填；同名时追加 `-2/-3/...` 后缀去重；调 `detectBackend()` + `TeamMgr.CreateTeam`；Output 提示用户用 `Agent` 工具带 `team_name` 加成员。
 - `TeamDeleteTool.Execute`：`team_name` 必填；不存在返 IsError；调 `TeamMgr.DeleteTeam`（内部 `StopMember` 每个成员）；返回停掉的成员清单。
- [ ] 完成

## T13: 实现 AgentTool.runAsTeammate
- 影响文件: `internal/agents/agent_tool.go`（`AgentTool.TeamMgr` 字段 @ 71；`team_name` 入口分支 @ 214 + 239-240；`runAsTeammate` @ 611-709；`drainTeammateEvents` @ 714-)
- 依赖任务: T8, T9（ch13 的 T1-T11）
- 完成标准:
 - `AgentTool` 新增 `TeamMgr *teams.TeamManager` 字段；
 - `Execute` 解析 `team_name` 参数后，当 `teamName != "" && TeamMgr != nil` 即走 `runAsTeammate`，先于 fork / runAsync / runSync 分发；
 - `runAsTeammate` 校验团队存在、同 team 同名报错；空 name 时 `sanitizeSlugSegment(description)` 生成；可选 `subagent_type` 解析 spec 跑 `FilterToolsForAgent`，无 spec 时把全 registry 给队员；
 - `isolation=worktree` 时 `worktree.CreateAgentWorktree(slug)` 建独立 worktree、把 `BuildWorktreeNotice` 拼到 prompt 顶端；
 - 调 `teams.BuildTeammateAddendum` 生成 addendum；调 `teams.SpawnTeammate` 拿 `SpawnResult`；
 - in-process 模式启 goroutine `drainTeammateEvents` 消费事件流，把 `ToolResultEvent` / `ErrorEvent` 翻译成 `SubAgentProgress` 喷进 `ProgressCh` 防止生产者阻塞；
 - Output 含 backend hint 和 SendMessage 使用提示。
- [ ] 完成

## T14: 实现 cmd/codebot --teammate worker 模式
- 影响文件: `cmd/codebot/main.go`（teammate flag 早期拦截 @ 19-25）；`cmd/codebot/teammate.go`（`teammateArgs` @ 22-25；`parseTeammateFlags` @ 37-61；`runTeammate` @ 68-121；`builtinTeammateTools` @ 126-135；`streamEventsToStderr` @ 140-163）
- 依赖任务: T1, T6, T8, T9, T10, T12
- 完成标准:
 - `main.go` 在加载 config 之前先调 `parseTeammateFlags(os.Args[1:])`，命中 `--teammate` 则走 `runTeammate` 不进 TUI；
 - `parseTeammateFlags` 仅识别 `--teammate / --team-name / --agent-name` 三个 flag，必须以 `--teammate` 起首；
 - `runTeammate` 校验 team-name / agent-name 必填；加载同一 config 取第一个 provider 创建 `llm.Client`；
 - 注册的工具集是 worker 白名单（`ReadFile / WriteFile / EditFile / Bash / Glob / Grep` + 自己的 `SendMessage`），**不含** `TeamCreate / TeamDelete`；
 - 用 `teams.NewTeam(name, ModeInProcess)` 在本进程构造 Team 对象（指向同一个 mailbox 目录，因为 `teamsBaseDir` 解析的是相同 wd），通过 `CreateTeamWith` 注册到本进程 TeamMgr；
 - 跑 `RunInProcessTeammate`，事件 channel 走 `streamEventsToStderr` 把 StreamText / ToolUseEvent / ToolResultEvent / ErrorEvent / LoopComplete 喷到 stderr；
 - 接 SIGINT/SIGTERM 调 cancel 优雅退出；
 - 不传 initialPrompt（保持 ""），让队员第一次 idle poll 从 mailbox 拿初始任务避免重复注入。
- [ ] 完成

## T15: TUI 接入
- 影响文件: `internal/tui/tui.go`（`teamMgr *teams.TeamManager` 字段 @ 196；`coordinatorToolFilter` @ 593-603；`registerAgentTools` 内 `teams.NewTeamManager()` @ 616 + 字段写回 @ 617 + 注册三个工具 @ 646-648 + `AgentTool.TeamMgr` 注入 @ 658；`DrainLeadMailbox` 接入 notification 队列 @ 545；`ag.ToolNameFilter = coordinatorToolFilter(m.teamMgr)` 两处接线 @ 387 + 1132）
- 依赖任务: T7, T11, T12, T13
- 完成标准:
 1. `Model.teamMgr` 字段在 tui.go:196 声明；
 2. `coordinatorToolFilter` 闭包：`teamMgr == nil` 返 nil（关闭过滤）；`len(teamMgr.ListTeams()) == 0` 时每轮放行所有工具；否则 `teams.IsCoordinatorTool(name)` 判定；
 3. `registerAgentTools` 里创建 `TeamManager` → 把 `TeamCreateTool / TeamDeleteTool / SendMessageTool` 注册到 registry → `AgentTool.TeamMgr` 注入；
 4. `gatherNotifications`（Lead 每轮迭代的开头钩子）调 `teams.DrainLeadMailbox(m.teamMgr)` 把 `<team-notification>` 字符串数组拼到要喂给模型的消息中；
 5. 主 Agent 初始化（`initSingleProviderMsg`）和恢复会话（`restoreSession`）两条路径都设 `ag.ToolNameFilter = coordinatorToolFilter(m.teamMgr)`。
- [ ] 完成

## T16: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T1-T15
- 完成标准:
 - `go build ./...` 通过；
 - `go test ./internal/teams/...` 全部测试通过（`teams_test.go` 8 个 + `runner_test.go` 多个，覆盖 FileMailBox roundtrip、并发、CRUD、detectBackend 三档优先级、SendMessage to=lead 路由、SendMessage unknown sender、CreateIdleNotification、IsShutdownRequest、formatInboundAsPrompt、waitForNextPromptOrShutdown 三条退出路径、DrainLeadMailbox 多团队、BuildTeammateCLI、ShellQuote、SpawnTeammate 校验、recordExternalMember）；
 - `go test ./cmd/codebot/...` 通过（`teammate_test.go` 覆盖 parseTeammateFlags 的命中 / 未命中 / 缺参数三种情况）；
 - 主流程接线验证：`grep -n "teamMgr\|teams\." internal/tui/tui.go` 命中所有上文列出的接入点；`grep -n "TeamMgr" internal/agents/agent_tool.go` 看到 `runAsTeammate` 分支被 Execute 调用。
- [ ] 完成

## 进度
- [ ] T1 / [ ] T2 / [ ] T3 / [ ] T4 / [ ] T5 / [ ] T6 / [ ] T7 / [ ] T8 / [ ] T9 / [ ] T10 / [ ] T11 / [ ] T12 / [ ] T13 / [ ] T14 / [ ] T15 / [ ] T16

```

```markdown
# ch15: AgentTeam Checklist

> 所有条目可勾选、可观测。验收方式写在条目后面括号中。验收：已通过验证的项均勾选。

## 1. 实现完整性

- [ ] 类型 `Team` 在 `internal/teams/teams.go:43-49` 存在，字段含 `Name / Mode / Members map / MailBox / mu sync.Mutex`
- [ ] 类型 `Member` 在 `internal/teams/teams.go:32-41` 存在，字段含 `Name / AgentRef / Conv / Active / Cancel / PaneID`（外部后端句柄）
- [ ] 类型 `TeamMode` 在 `internal/teams/teams.go:17` 存在，常量 `ModeInProcess / ModeTmux` 在 `teams.go:19-22`，`ModeITerm` 在 `iterm.go:11`（与后端代码同文件保持内聚）
- [ ] 类型 `TeamManager` 在 `internal/teams/teams.go:126-129` 存在，方法集含 `CreateTeam / CreateTeamWith / GetTeam / DeleteTeam / ListTeams / CloseAll`
- [ ] 类型 `FileMailBox` 在 `internal/teams/filemailbox.go:11-13` 存在；`FileMailMessage` 在 `:15-22` 含 6 字段 `From / Text / Timestamp / Read / Color / Summary`
- [ ] 类型 `TeammateSpawnConfig / SpawnResult` 在 `internal/teams/spawn.go:23-43` 存在
- [ ] 常量 `LeadName = "lead"` / `ShutdownPrefix = "[shutdown]"` / `IdlePollInterval = 500 * time.Millisecond` 在 `internal/teams/runner.go:16-25`
- [ ] `CoordinatorAllowedTools` map 在 `internal/teams/coordinator.go:13-26` 含 12 项白名单（写工具被排除）
- [ ] `RunInProcessTeammate` 在 `internal/teams/runner.go:60-123` 主循环六步齐全：ctx 检查 → InjectPendingMessages → AddUserMessage → agent.Run + 事件转发 → idle 通知 → waitForNextPromptOrShutdown 轮询
- [ ] `withLock` 在 `filemailbox.go:71-111` 使用 `O_CREATE|O_EXCL` 锁文件，10 次重试，>10s 过期清理
- [ ] `BuildTeammateCLI` 在 `spawn.go:149-164` 输出 `cd <quoted_wd> && <quoted_exe> --teammate --team-name <quoted> --agent-name <quoted>`，`shellQuote` 单引号转义 POSIX 标准
- [ ] `BuildTeammateAddendum` 在 `inprocess.go:55-66` 文本包含 "you are a member of team"、"Lead is reachable as 'lead'"、"deliver your final result to the lead with SendMessage"、"messages from the team arrive as system reminders" 四个关键信息
- [ ] `DrainLeadMailbox` 在 `runner.go:169-199` nil 安全（`mgr == nil` 返 nil）、读完邮件后调 `MarkAllRead`、输出格式为 `<team-notification team="X">\n...\n</team-notification>`
- [ ] `SendMessageTool.Execute` 在 `tools.go:44-91` 把 `to == LeadName` 单独走"查发送者所在团队"路径（Lead 不是 Member）
- [ ] `TeamCreateTool.Execute` 在 `tools.go:125-145` 同名冲突自动追加 `-2/-3/...` 后缀去重
- [ ] `AgentTool.runAsTeammate` 在 `internal/agents/agent_tool.go:611-709` 五件事齐全：查团队、查重名、解析 spec + 工具池、可选 worktree、SpawnTeammate + drainTeammateEvents

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `grep -n "teams.NewTeamManager\|TeamMgr:" internal/tui/tui.go` 在 `internal/tui/tui.go:616` 找到 `teams.NewTeamManager()` 调用方
- [ ] `grep -n "TeamCreateTool\|TeamDeleteTool\|SendMessageTool" internal/tui/tui.go` 在 `internal/tui/tui.go:646-648` 找到三个工具注册点
- [ ] `m.registry.Register(&agents.AgentTool{...TeamMgr: teamMgr...})` 注入点在 `internal/tui/tui.go:649-661`
- [ ] `teams.DrainLeadMailbox(m.teamMgr)` 调用点在 `internal/tui/tui.go:545`（`gatherNotifications`），把 `<team-notification>` 注入下一轮系统提示
- [ ] `ag.ToolNameFilter = coordinatorToolFilter(m.teamMgr)` 接线在两处：`internal/tui/tui.go:387`（初始化）+ `internal/tui/tui.go:1132`（恢复会话）
- [ ] `coordinatorToolFilter` 函数定义在 `internal/tui/tui.go:593-603`，三段语义：nil → 关闭过滤；空团队 → 放行全部；非空 → `IsCoordinatorTool`
- [ ] `Model.teamMgr` 字段在 `internal/tui/tui.go:196` 声明
- [ ] `cmd/codebot/main.go:19-25` 在加载 config 之前先 `parseTeammateFlags`，命中 `--teammate` 走 `runTeammate` 跳过 TUI
- [ ] `cmd/codebot/teammate.go:97` 注册队员侧 `SendMessageTool{TeamMgr: teamMgr, SenderName: args.memberName}`（worker 进程也有 SendMessage 工具）
- [ ] `cmd/codebot/teammate.go:120` 调 `teams.RunInProcessTeammate` 作为 worker 进程主循环
- [ ] `cmd/codebot/teammate.go:113` 调 `teams.BuildTeammateAddendum` 注入到 worker 端 conversation

## 3. 编译与测试

- [ ] `go build ./...` 通过
- [ ] `go test ./internal/teams/...` 通过（覆盖至少 16 个用例：FileMailBoxRoundTrip / FileMailBoxConcurrentSends / TeamManagerCRUD / DetectBackendFallback / DetectBackendPrefersTmuxWhenInside / DetectBackendPicksITermWhenInside / SendMessageToolRoutesToLead / SendMessageToolUnknownSenderToLead / IsShutdownRequest / CreateIdleNotification / FormatInboundAsPromptEmpty / FormatInboundAsPromptMultiple / WaitForNextPromptOrShutdownShutdown / WaitForNextPromptOrShutdownMessage / WaitForNextPromptOrShutdownCancel / DrainLeadMailbox / DrainLeadMailboxNilSafe / BuildTeammateCLIFormat / SpawnTeammateValidation / RecordExternalMember / ShellQuote）
- [ ] `go test ./cmd/codebot/...` 通过（`teammate_test.go` 覆盖 parseTeammateFlags 三种情况：未命中 / 命中 + 完整参数 / 命中 + 缺参数）
- [ ] `go vet ./...` 无警告
- [ ] 测试运行不在仓库根残留 `.codebot/teams/` 目录（`TestMain` 走 `CODEBOT_TEAMS_DIR` 重定向到 tmp）

## 4. 端到端验证

- [ ] 注册路径：TUI 启动后 `registerAgentTools` 在 `tui.go:616-648` 创建 `TeamManager` 并把 `TeamCreate / TeamDelete / SendMessage` 三件套放入 registry；用户向 Lead 说 "create a team to refactor X" → LLM 调 `TeamCreate(team_name="refactor-X")` → `detectBackend()` 选模式 → Output 返回 "Team refactor-X created (mode: ...). Use Agent tool with team_name=..."
- [ ] Spawn 路径：Lead 继续说 "spawn alice to do data layer" → LLM 调 `Agent(team_name="refactor-X", name="alice", prompt="...")` → `AgentTool.Execute` 识别 `team_name` 分支调 `runAsTeammate` → `SpawnTeammate(ModeInProcess|ModeTmux|ModeITerm)` → 队员开始干活
- [ ] 通信路径：队员 alice 通过 `SendMessage(to="bob", content="...")` 给 bob 写 mailbox → bob 下一轮 idle poll / inject pending → 收到消息作为 system reminder
- [ ] Lead 感知路径：每个队员 turn 结束写 idle 通知到 Lead 邮箱 → Lead 下一轮迭代 `gatherNotifications` 调 `DrainLeadMailbox` 抽出 `<team-notification team="refactor-X">\nfrom=alice: [idle] alice (reason: available)\n</team-notification>` 注入 Lead 上下文
- [ ] Coordinator Mode 路径：团队存活期间 `ag.ToolNameFilter = coordinatorToolFilter(m.teamMgr)` 让 Lead 每轮工具集只剩 12 项白名单，调用 `WriteFile` / `EditFile` 会被过滤拒绝；`TeamDelete` 清空所有团队后下一轮恢复全工具集
- [ ] Tmux 后端：`TMUX` env 非空时 `detectBackend` 返 `ModeTmux` → spawn 时先把 task 写 mailbox → `tmux new-window -d` 拉起新窗口跑 `codebot --teammate ...` → 子进程 `parseTeammateFlags` 命中 → `runTeammate` 加载同一 mailbox 目录 → 第一次 idle poll 拿到初始任务开始干活
- [ ] iTerm 后端：`ITERM_SESSION_ID` 非空 + 不在 tmux 时 `detectBackend` 返 `ModeITerm` → `osascript` 创建 iTerm tab 跑同样命令
- [ ] 关闭路径：`TeamDelete(team_name="refactor-X")` → `TeamMgr.DeleteTeam` → 遍历 `team.StopMember` 按 `Mode + PaneID` 分发 tmux 关 window / iTerm 关 tab / in-process 取消 context → 全部清理后 Lead 下轮恢复全工具集

## 5. 文档

- [ ] `specs/go/ch15/spec.md` 已写
- [ ] `specs/go/ch15/tasks.md` 已写，16 个 T 全部勾完
- [ ] `specs/go/ch15/checklist.md` 已写并逐项验收
- [ ] commit 信息标注 `ch15` 与三件套关闭状态（待用户确认后由人或 CI 触发）

```

### Python

```markdown
# ch15: AgentTeam Spec

## 1. 背景

SubAgent（ch13）解决了一次性子任务的上下文隔离，但拓扑是星型：所有子 Agent 只能和主 Agent 通信，子 Agent 之间彼此看不见。当任务规模上来——四个模块同时重构、多角度并行调查 bug、一个 Agent 需要把发现告诉另一个——星型拓扑下主 Agent 成了信息中转瓶颈，子任务被迫串行。这一章把"长期协作团队"做成 CodeBot 的一等概念：多个 Agent 组成 Team，并行干活、直接互发消息、共享任务列表和邮箱，主 Agent 可选切换为 Coordinator Mode 专职调度。

## 2. 目标

提供 `AgentTeam` / `TeammateInfo` / `TeamManager` / `Mailbox` / `SharedTaskStore` / `AgentNameRegistry` 一整套数据结构与服务，并暴露 `SendMessageTool` / `TeamCreateTool` / `TeamDeleteTool` 三个工具，让 LLM 在对话里：1) 调 `TeamCreate` 建团队（按环境自动选 tmux / iterm2 / in-process 后端，并在 `~/.codebot/teams/<name>/` 落盘 config.json + tasks.json + mailbox/），2) 调 `Agent` 工具带 `team_name` 把队员 spawn 进团队（独立 worktree + 受限工具池），3) 队员之间通过 `SendMessage` 走 `Mailbox` 互发消息、按名字或 agent_id 寻址、支持 `to="*"` 广播，4) Lead 每轮迭代开头 `_consume_mailbox` 把收件箱里的消息转 user message 注入对话，5) 启用 `enable_coordinator_mode` 后 Lead 通过 `apply_coordinator_filter` 把工具集收窄到 12 项白名单。Tmux / iTerm2 后端时新 pane 由 `build_cli_command` 拼出 `codebot -p` 命令字符串，通过 `CODEBOT_TEAM_NAME` / `CODEBOT_TEAMMATE_NAME` / `CODEBOT_MAILBOX_DIR` 环境变量与 Lead 共享同一份 mailbox 目录。

## 3. 功能需求

- F1: `BackendType` 枚举三档 `TMUX="tmux"` / `ITERM2="iterm2"` / `IN_PROCESS="in-process"`；`detect_backend(teammate_mode, is_interactive)` 按 `teammate_mode == "in-process" or not is_interactive` → `TMUX env` → `TERM_PROGRAM == "iTerm.app" + it2 可执行` → `tmux 可执行` 的优先级自动选择；都不命中抛 `BackendDetectionError` 而非静默回退（保证用户不会在不知情下失去进程隔离）。
- F2: `AgentTeam` dataclass 持有 `name / lead_agent_id / members: list[TeammateInfo] / config_path / description`，可 `to_dict` / `from_dict` / `save` / `load`；`get_member(name)` 同时按 `name` 或 `agent_id` 查找；`set_member_active(name, is_active)` 翻转活跃标志；`all_idle()` 返回所有成员是否都为 `is_active is False`。
- F3: `TeammateInfo` dataclass 字段 `name / agent_id / agent_type / model / worktree_path / backend_type / is_active`，`is_active: bool | None = None` 三值语义：`None` 或 `True` 表示活跃，`False` 表示空闲；删除直接从 members 列表移除不留墓碑。
- F4: `TeamManager` 提供 `detect_backend` / `create_team` / `get_team` / `get_task_store` / `get_mailbox` / `register_member` / `set_member_idle` / `register_inprocess_handle` / `register_pane_id` / `get_pane_id` / `delete_team` / `get_team_for_teammate` / `on_teammate_completed` 共 13 个公开方法；内部维护 `_teams` / `_task_stores` / `_mailboxes` / `_inprocess_handles` / `_pane_ids` / `_teammate_team_map` / `_detected_backend` 七个字典/缓存；`_detected_backend` 第一次检测后缓存复用。
- F5: `Mailbox` 基于 `<base_dir>/<agent_id>/<timestamp>_<id>.json` 单文件单消息模型：`write(agent_id, msg)` 落盘 ；`read(agent_id)` 只读不删；`consume(agent_id)` 读完立刻 `f.unlink()`；`broadcast(team_members, msg, exclude)` 按列表逐个 write 排除 exclude；`cleanup(agent_id)` / `cleanup_all()` 清空目录。
- F6: `MailboxMessage` 字段 `id / from_agent / to_agent / content / summary / message_type / timestamp / metadata`；`message_type` 三档 `text / shutdown_request / shutdown_response` 由 `SendMessageTool.VALID_MESSAGE_TYPES` 守门；`text` 类型必须带非空 `summary`（5-10 词）否则报错。
- F7: `create_message(from_agent, to_agent, content, summary, message_type, metadata)` 统一构造器，自动填 `id=uuid4().hex[:12]` 和 `timestamp=time.time()`。
- F8: `SharedTaskStore` 基于单文件 `tasks.json`，结构 `{"next_id": int, "tasks": [...]}`；`create / get / list_tasks / update / init_empty` 五个方法；`SharedTask` 字段 `id / title / description / status / assignee / blocks / blocked_by / created_by`，`status` 四档 `pending / in_progress / completed / blocked`。
- F9: `AgentNameRegistry` 进程内单例（线程安全 double-checked locking）；`register(name, agent_id)` / `resolve(name_or_id)`（先按 name 查再按 id 反查）/ `unregister(name)` / `list_all()` / `reset()` 五个方法。
- F10: `TeamManager.create_team(name, lead_agent_id, description, teammate_mode, is_interactive)` 调 `detect_backend` 决定后端 → `unique_team_name` 自动加 `-2/-3/...` 后缀避免同名 → 在 `~/.codebot/teams/<slug>/` 建目录 → 写 config.json + tasks.json + mailbox/ → 缓存到 `_teams` / `_task_stores` / `_mailboxes`。
- F11: `TeamManager.delete_team(team_name)` 先校验所有成员都 idle（`is_active is False`），否则报 `Cannot delete team: active members: ...`；通过后遍历每个 member：unregister 名字、cancel in-process handle、kill pane、git worktree remove、trace manager remove；最后 cleanup mailbox + 删团队目录 + 弹出三个缓存字典。
- F12: `spawn_inprocess_teammate(agent, prompt, name, conversation)` 用 `asyncio.create_task` 起协程跑 `agent.run_to_completion`，返 `InProcessTeammateHandle{agent, task, name}`；`handle.done` 判完成、`handle.result` 安全取结果、`handle.cancel()` 取消未完成 task。
- F13: `spawn_tmux_teammate` 三级 fallback：先尝试 `split-window -h -t <team_name>` → 失败则 `new-window` + `split-window` → 再失败则 `new-session -d` + `list-panes`；用 `build_cli_command` 拼出 `CODEBOT_TEAM_NAME=X CODEBOT_TEAMMATE_NAME=Y CODEBOT_MAILBOX_DIR=Z codebot -p --work-dir <wt> '<prompt>'` 字符串，prompt 内单引号转义为 `'\''`；最后 `send-keys -t <pane> <cmd> Enter` 启动；`kill_pane(pane_id)` best-effort 静默失败。
- F14: `spawn_iterm2_teammate` 复用 `build_cli_command`，通过 `it2 split-pane --command "/bin/zsh -c '<cmd>'"` 创建新 pane 返回 `ITermPaneInfo{session_id}`。
- F15: `save_transcript(team_name, agent_id, conv)` / `load_transcript(team_name, agent_id)` 把 `ConversationManager.history`（含 tool_uses / tool_results 块）序列化为 JSON 落到 `~/.codebot/teams/<team>/transcripts/<agent_id>.json`，加载时 `env_injected = ltm_injected = True` 防止重复注入。
- F16: `Agent._consume_mailbox(conversation)` 在每轮迭代开头钩入：仅当 `self.team_name and self._team_manager` 非空时取 mailbox.consume 自己的 agent_id；每条消息前缀 `[Message from <sender>] ` 或 `[<message_type> from <sender>] ` 后 `conversation.add_user_message`；异常吞掉记 debug。
- F17: `TeamCreateTool` 暴露 `team_name` 必填 + `description` 可选；调 `detect_backend` 不通过返 IsError；通过后 `team_manager.create_team`；如 `is_coordinator_mode(enable_coordinator_mode)` 返 true 则把 `parent_agent.coordinator_mode = True`、备份 `_full_registry`、把 `parent_agent.registry = apply_coordinator_filter(registry)`，输出附带 "Coordinator Mode activated" 提示。
- F18: `TeamDeleteTool` 暴露 `team_name` 必填；调 `team_manager.delete_team` 捕获 `TeamError` 返 IsError；如 `parent_agent.coordinator_mode` 为 true 则恢复 `_full_registry` 并清零 flag，输出附带 "Coordinator Mode deactivated" 提示。
- F19: `SendMessageTool` 暴露 `to / message / summary / message_type / metadata`；先校验 `message_type in VALID_MESSAGE_TYPES`，再校验 `text` 类型必须有 `summary`；`to == "*"` 走 `mailbox.broadcast(member_ids ∪ {lead_agent_id} \ {self})`，否则用 `AgentNameRegistry.instance().resolve(to)` 解析目标 id；写完后 `_wake_pane(target_id)` 向 tmux pane send-keys 空行触发新消息读取（pane 后端唤醒机制）。
- F20: `AgentTool._execute_as_teammate(p)` 处理 `team_name != None` 分支：校验 team 存在、按 base_name 同名冲突自动加 `-2/-3/...`、可选解析 `subagent_type` 否则 fork、`worktree_manager.create(f"team-{team_name}/{teammate_name}", "HEAD")` 建独立 worktree、`build_teammate_tools` 按 backend 类型构造队员工具池（in-process 严格白名单 / pane 模式只剔除 `TeamCreate` 和 `TeamDelete`）、`register_member` 注册到团队 + AgentNameRegistry、按 backend 分发 `spawn_inprocess_teammate` 或 `_spawn_pane_teammate`。

## 4. 非功能需求

- N1: `Mailbox` 单文件单消息模型避免跨进程并发写覆盖：每条消息文件名 `<timestamp>_<id>.json` 全局唯一，写入无需文件锁；`consume` 按 `sorted(d.iterdir())` 时间排序保证 FIFO；`unlink` 单文件操作在 POSIX 文件系统上原子，不会丢消息。
- N2: `detect_backend` 检测失败不静默回退到 in-process——直接抛 `BackendDetectionError` 让用户显式选择：要么装 tmux / iTerm2+it2，要么在 config.yaml 设 `teammate_mode: "in-process"`。理由是 pane 后端提供的进程隔离是团队模式的核心保障，静默降级会让用户失去隔离能力还不自知。
- N3: `AgentNameRegistry` 是进程内单例，因此跨进程的 pane teammate 必须自己在子进程内重新注册名字 → agent_id 映射，不能依赖 Lead 进程的注册表；`resolve` 同时支持按 name 和按 agent_id 反查，给 Lead 端 SendMessage 用名字、给子进程端用 agent_id 都能命中。
- N4: `TeamManager._detected_backend` 一旦检测过就缓存，整个 team manager 生命周期内不变。同进程内多次 `create_team` 不会重新探测环境——保证一致性，避免中途装 tmux 导致前后行为不一致。
- N5: `_consume_mailbox` 必须放在 Agent 每轮迭代开头（在调 LLM 之前），不能放在迭代结束：放结束会让"工具调用完成 → idle → 下轮才看到新消息"出现一轮延迟；放开头保证 LLM 看到的对话历史里已经包含队员的最新消息，决策不滞后。
- N6: `TeamCreateTool` 启用 Coordinator Mode 时必须把原 `registry` 备份到 `parent_agent._full_registry`，`TeamDeleteTool` 恢复时从这里读回——不能依赖重新构造，因为 registry 里可能已经注入了运行时动态注册的工具（MCP / Skill）。
- N7: `SendMessageTool` 的 `_wake_pane` 在 pane teammate 场景必须 send-keys 触发新消息读取（pane 进程在 `codebot -p` 单次执行模式下会阻塞在 stdin），否则消息只是写入 mailbox 但对方进程感知不到；in-process teammate 不需要 wake 因为同进程 `_consume_mailbox` 每轮自动跑。
- N8: `build_cli_command` 把 `prompt` 内的单引号转义为 `'\''`（关闭→插入字面单引号→重开），否则 prompt 里出现单引号会破坏 shell 解析；前缀环境变量 `CODEBOT_TEAM_NAME` / `CODEBOT_TEAMMATE_NAME` 通过空格分隔但不加引号，假设值是合法标识符。
- N9: `delete_team` 必须先校验所有 member `is_active is False`，活跃成员存在时拒绝删除——避免运行中的 in-process 协程或 pane 进程突然失去 mailbox 后悬挂。Active 检查用 `is_active is not False`（`None` 和 `True` 都算 active）。
- N10: 测试运行 `Mailbox` 和 `AgentTeam.save` 必须 `monkeypatch / patch("codebot.teams.models.Path.home", ...)` 重定向 home 到 `tmp_path`，否则跑完测试会在用户主目录残留 `~/.codebot/teams/` 目录污染。
- N11: `AgentNameRegistry.reset()` 在 pytest fixture 中 autouse 调用——单例在用例间共享会让 register 状态泄漏，导致 `test_register_and_resolve` 后跑的用例看到上个用例的残留映射。
- N12: `spawn_iterm2_teammate` 通过外部 `it2` CLI 而非直接 osascript——it2 是 iTerm2 官方提供的稳定 CLI，比 osascript 字符串拼接的 AppleScript 更可靠且支持版本演进。

## 5. 设计概要

- 核心数据结构:
 - `AgentTeam`：团队聚合 dataclass，`members: list[TeammateInfo]` 列表非 map（队员数不大，遍历足够），通过 `config_path` 持久化到 `~/.codebot/teams/<slug>/config.json`。
 - `TeammateInfo`：队员元信息 dataclass，`is_active: bool | None` 三值语义；`agent_id` 是全局唯一进程标识；`worktree_path` 关联到 ch14 的 worktree。
 - `TeamManager`：全局团队注册表 + 多类资源缓存（mailbox / task store / inprocess handle / pane id / teammate→team 反查映射），是 Lead 进程的"团队服务总线"。
 - `Mailbox` + `MailboxMessage`：单文件单消息模型，靠时间戳前缀文件名保证 FIFO 且跨进程写入无冲突；支持 `text / shutdown_request / shutdown_response` 三种类型。
 - `SharedTaskStore` + `SharedTask`：JSON 文件实现的共享任务列表，团队内所有成员通过 `team_manager.get_task_store(team_name)` 读到同一份。
 - `AgentNameRegistry`：进程内单例（线程安全 double-checked），把人类可读的 name 映射到 agent_id，给 SendMessage 寻址用。
 - `InProcessTeammateHandle`：包装 `asyncio.Task`，供 `TeamManager` 跟踪 in-process 队员生命周期。
- 主流程（按生命周期）:
 - 创建：用户消息 → 主 Agent → LLM 调 `TeamCreate(team_name="X")` → `team_manager.detect_backend()` 选模式 → `team_manager.create_team` 在 `~/.codebot/teams/x/` 落 config.json + tasks.json + mailbox/ → 可选切 Coordinator Mode（备份 `_full_registry` + `apply_coordinator_filter`）。
 - Spawn 队员：Lead LLM 调 `Agent(team_name="X", name="alice", prompt="...")` → `AgentTool.execute` 看到 `team_name` 非空走 `_execute_as_teammate` → 校验团队、解析子 agent type / fork、`worktree_manager.create` 建独立 wt、按 backend 调 `build_teammate_tools` 构造工具池、`register_member` 注册到团队 + `AgentNameRegistry.instance().register`。
 - In-process：`spawn_inprocess_teammate(agent, prompt, name)` 起 `asyncio.create_task` 跑 `agent.run_to_completion`，handle 注册到 `team_manager._inprocess_handles`。
 - Pane 后端：`spawn_tmux_teammate` / `spawn_iterm2_teammate` 用 `build_cli_command` 拼出带 `CODEBOT_TEAM_NAME` / `CODEBOT_TEAMMATE_NAME` / `CODEBOT_MAILBOX_DIR` env 的 `codebot -p` 命令字符串 → tmux send-keys / it2 split-pane 启动 → pane_id 注册到 `team_manager._pane_ids`。
 - 通信：队员调 `SendMessage(to="bob", message="...", summary="...")` → `AgentNameRegistry.resolve("bob")` → target_id → `mailbox.write(target_id, msg)` → `_wake_pane(target_id)`（pane 队员需要）→ 对方下一轮 `_consume_mailbox` 拿到。
 - Lead 感知：每轮 Lead `agent.run_to_completion` 内部 `_consume_mailbox(conversation)` 把 `mailbox.consume(self.agent_id)` 的所有消息转 user message 注入 conversation，前缀 `[Message from X] ` 或 `[shutdown_request from X] `。
 - Idle 通知：`AgentTool` 后台任务完成回调 `team_manager.on_teammate_completed(agent_id)` → `set_member_idle` 把 `is_active=False` + 写一条 `"Teammate 'X' is now idle"` 到 Lead 邮箱。
 - Coordinator Mode：`apply_coordinator_filter(registry)` 把工具集筛到 `COORDINATOR_MODE_ALLOWED_TOOLS` 12 项 `{Agent, SendMessage, TaskCreate, TaskGet, TaskList, TaskUpdate, TeamCreate, TeamDelete, ReadFile, Glob, Grep, Bash}`（写工具 `WriteFile / EditFile` 被排除）；`TeamDeleteTool` 恢复 `_full_registry`。
 - Stop：`TeamDelete(team_name="X")` → `team_manager.delete_team` → 校验全员 idle → 遍历每个 member：unregister name、cancel handle、kill pane、git worktree remove、trace_manager.remove → cleanup mailbox + 删团队目录 + 弹出三个缓存。
- 调用链（模块层级）:
 - `codebot/app.py:730-762` 在 `CodeBotApp.__init__` 后段创建 `TeamManager(worktree_manager, trace_manager)`，把 `team_manager` 注入 `AgentTool`，把 `TeamCreateTool / TeamDeleteTool / SyntheticOutputTool` 注册进 registry，把 `agent._team_manager = team_manager` 写回主 Agent。
 - `codebot/agent.py:324-326` 主 Agent `__init__` 声明 `self.coordinator_mode / self.team_name / self._team_manager` 三个字段；`:433 / :957` 在 `run_to_completion` 主循环开头调 `self._consume_mailbox(conversation)`；`:471 / :937` 给 `build_system_prompt` 传 `coordinator_mode=self.coordinator_mode` 切提示词。
 - `codebot/tools/agent_tool.py:86-87` `execute` 入口看到 `p.team_name` 非空时优先走 `_execute_as_teammate`（先于 fork / sync / async 分发）；`:246-414` 实现完整队员 spawn 流程。
- 与其他模块的交互:
 - 依赖 `codebot/agent`（Agent 实例 / `run_to_completion` / 系统提示注入）、`codebot/conversation`（ConversationManager / Message / ToolUseBlock / ToolResultBlock）、`codebot/agents/tool_filter`（`apply_coordinator_filter` / `build_teammate_tools` / `COORDINATOR_MODE_ALLOWED_TOOLS` / `IN_PROCESS_TEAMMATE_ALLOWED_TOOLS`）、`codebot/worktree`（每个队员独立 worktree）、`codebot/tools/base`（Tool / ToolResult / ToolRegistry）。
 - 被 `codebot/app.py`（注册三件套工具 + 写回 `agent._team_manager`）、`codebot/tools/agent_tool.py`（`_execute_as_teammate` 调 spawn / register）、`codebot/prompts.py`（`build_system_prompt(coordinator_mode=...)` 切系统提示词）调用。

## 6. Out of Scope

- 不实现 PR 文档里描述的 `planModeRequired` 字段和审批工作流——`TeammateInfo` 只保留基础元信息，审批门槛由后续章节扩展。
- 不实现 `shutdown_response` 完整双向握手协议——只保留 `message_type` 字段的三档枚举，握手语义由 LLM 在文本层约定。
- 不实现共享任务依赖图的拓扑排序自动调度——`SharedTask.blocks / blocked_by` 字段已存但 store 仅做 CRUD，依赖推断由 Lead LLM 从任务列表文本自己读出。
- 不实现"队员后从磁盘恢复对话续写"机制——in-process 队员 task 完成或 cancel 后即终止，transcript 落盘仅供事后回看，不支持 resume；要 Lead 想再用需要重新 spawn。
- 不实现"协调模式四阶段工作流"强制约束（Research / Synthesis / Implementation / Verification）——`get_coordinator_system_prompt` 写入提示词层引导，但工具层不强制顺序。
- 不实现 `CODEBOT_COORDINATOR_MODE` 自动激活——必须 `enable_coordinator_mode=True` 配合 env var 双开关同时打开才生效，避免 Lead 进程被意外切到协调模式。
- 不实现 mailbox 的跨节点分布式同步——团队只在单机内运作，所有 mailbox 文件在本地 `~/.codebot/teams/<name>/mailbox/` 下；要跨机协作需要外部传输层。
- 不实现 worker pane 进程的自动重启——pane 进程 crash 后 Lead 端 `pane_id` 仍记录但实际 pane 已死；用户需手动 `TeamDelete` 然后重建。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch15: AgentTeam Tasks

> 任务粒度：每个任务可在一次会话内完成，可独立交付。

## T1: 定义 BackendType / TeammateInfo / AgentTeam 三个核心模型
- 影响文件: `codebot/teams/models.py`（`BackendType` @ 10-13；`TeammateInfo` @ 16-31；`_sanitize_name` @ 34-37；`AgentTeam` @ 40-102；`resolve_team_dir` @ 105-107；`unique_team_name` @ 110-117）
- 依赖任务: 无
- 完成标准: `BackendType(str, Enum)` 三档常量 `TMUX / ITERM2 / IN_PROCESS`；`TeammateInfo` dataclass 7 字段含 `is_active: bool | None = None` 三值；`AgentTeam` 含 `get_member` / `add_member` / `remove_member` / `set_member_active` / `all_idle` / `active_members` / `to_dict` / `from_dict` / `save` / `load`；`get_member` 按 name 或 agent_id 双向查找；`resolve_team_dir` 落到 `~/.codebot/teams/<slug>/`；`unique_team_name` 同名冲突自动加 `-2/-3/...` 后缀。
- [ ] 完成

## T2: 实现 Mailbox + MailboxMessage（单文件单消息）
- 影响文件: `codebot/teams/mailbox.py`（`MailboxMessage` @ 11-27；`Mailbox` @ 30-102；`create_message` @ 105-122）
- 依赖任务: 无
- 完成标准: `MailboxMessage` 8 字段含 `id / from_agent / to_agent / content / summary / message_type / timestamp / metadata`；`message_type` 注释三档 `text | shutdown_request | shutdown_response`；`Mailbox.write` 以 `<timestamp>_<id>.json` 为文件名落到 `<base>/<agent_id>/` 目录；`read` 只读不删按 `sorted(d.iterdir())` 时间序；`consume` 读完立刻 `f.unlink()` 保证 FIFO；`broadcast(team_members, msg, exclude)` 逐个 write 排除 exclude；`cleanup` / `cleanup_all` 清目录；`create_message` 自动填 `uuid4().hex[:12]` 和 `time.time()`。
- [ ] 完成

## T3: 实现 detect_backend 优先级链
- 影响文件: `codebot/teams/backend_detect.py`（`BackendDetectionError` @ 9-10；`_in_tmux_session` @ 13-14；`_in_iterm2` @ 17-18；`_it2_available` @ 21-22；`_tmux_installed` @ 25-26；`detect_backend` @ 29-51）
- 依赖任务: T1
- 完成标准: 优先级 `teammate_mode == "in-process" or not is_interactive` → `TMUX` env → `TERM_PROGRAM == "iTerm.app" + shutil.which("it2")` → `shutil.which("tmux")`；都不命中抛 `BackendDetectionError` 而非静默回退；错误消息含 `tmux: brew install tmux` 和 `iTerm2 + it2 CLI` 安装指引并提示 `teammate_mode: "in-process"` 选项。
- [ ] 完成

## T4: 实现 SharedTaskStore + SharedTask
- 影响文件: `codebot/teams/shared_task.py`（`SharedTask` @ 9-25；`SharedTaskStore` @ 28-；`__init__` + `_load` + `_save`；`create` / `get` / `list_tasks` / `update` / `init_empty`）
- 依赖任务: 无
- 完成标准: `SharedTask` dataclass 8 字段含 `id / title / description / status / assignee / blocks / blocked_by / created_by`；`status` 注释四档 `pending | in_progress | completed | blocked`；`SharedTaskStore` 用单文件 `tasks.json` 结构 `{"next_id": int, "tasks": [...]}`；`create` 自增 id 返 `SharedTask` 实例；`list_tasks(status=None, assignee=None)` 双过滤；`update` 部分字段更新 + `add_blocks` / `add_blocked_by` 列表追加（去重）；`init_empty` 清空 + 重置 `_next_id=1` + save。
- [ ] 完成

## T5: 实现 AgentNameRegistry 单例
- 影响文件: `codebot/teams/registry.py`（`AgentNameRegistry` @ 6-40）
- 依赖任务: 无
- 完成标准: 进程内单例（线程安全 double-checked locking with `_lock = threading.Lock()`）；`instance()` / `reset()` 类方法；`register(name, agent_id)` / `resolve(name_or_id)` 同时支持按 name 和按 id 反查 / `unregister(name)` / `list_all()` 实例方法；`_names: dict[str, str]` 内部存储 name → agent_id 映射。
- [ ] 完成

## T6: 实现 spawn_inprocess_teammate + InProcessTeammateHandle
- 影响文件: `codebot/teams/spawn_inprocess.py`（`InProcessTeammateHandle` @ 14-40；`spawn_inprocess_teammate` @ 43-56）
- 依赖任务: T1
- 完成标准: `InProcessTeammateHandle(agent, task, name)` 属性 `done` / `result`（已完成时安全取结果异常返 None）/ `cancel()` 取消未完成 task；`spawn_inprocess_teammate(agent, prompt, name, conversation=None)` 用 `asyncio.create_task` 起协程跑 `agent.run_to_completion(prompt)` 或 `agent.run_to_completion("", conversation)`（传 conversation 走 fork 路径）；task name 设为 `f"teammate-{name}"`。
- [ ] 完成

## T7: 实现 spawn_tmux_teammate + build_cli_command + kill_pane
- 影响文件: `codebot/teams/spawn_tmux.py`（`TmuxPaneInfo` @ 10-13；`TmuxSpawnError` @ 16-17；`_run_tmux` @ 20-29；`build_cli_command` @ 32-56；`spawn_tmux_teammate` @ 59-108；`send_keys_to_pane` @ 111-115；`kill_pane` @ 118-122）
- 依赖任务: T1
- 完成标准: `build_cli_command` 拼出 `CODEBOT_TEAM_NAME=X CODEBOT_TEAMMATE_NAME=Y CODEBOT_MAILBOX_DIR=Z codebot -p --work-dir <wt> [--agent-type X] [--model X] '<prompt>'`，prompt 内单引号转义为 `'\''`；`spawn_tmux_teammate` 三级 fallback——先 `split-window -h -t <team_name>` → 失败则 `new-window` + `split-window` → 再失败则 `new-session -d` + `list-panes` 取第一个；最后 `send-keys -t <pane> <cmd> Enter`；`kill_pane` best-effort 静默失败；`send_keys_to_pane` 用于 wake pane。
- [ ] 完成

## T8: 实现 spawn_iterm2_teammate
- 影响文件: `codebot/teams/spawn_iterm2.py`（`ITermPaneInfo` @ 10-12；`ITermSpawnError` @ 15-16；`_run_it2` @ 19-28；`spawn_iterm2_teammate` @ 31-58）
- 依赖任务: T7
- 完成标准: 复用 `build_cli_command` 拼命令；通过 `it2 split-pane --command "/bin/zsh -c '<cmd>'"` 创建新 pane；返回 `ITermPaneInfo{session_id}`；spawn 失败抛 `ITermSpawnError`（不静默吞）；使用外部 `it2` CLI 而非 osascript 字符串拼接。
- [ ] 完成

## T9: 实现 transcript 持久化
- 影响文件: `codebot/teams/transcript.py`（`_serialize_conversation` @ 10-33；`_deserialize_conversation` @ 36-64；`save_transcript` @ 67-79；`load_transcript` @ 82-92）
- 依赖任务: T1
- 完成标准: `save_transcript(team_name, agent_id, conv)` 把 `conv.history`（含 tool_uses / tool_results 块）序列化 JSON 落到 `<team_dir>/transcripts/<agent_id>.json`；`load_transcript` 反序列化时 `env_injected = ltm_injected = True` 防止重复注入环境消息；tool_uses 用 `ToolUseBlock{tool_use_id, tool_name, arguments}` 结构，tool_results 用 `ToolResultBlock{tool_use_id, content, is_error}`。
- [ ] 完成

## T10: 实现 TeamManager 全套方法
- 影响文件: `codebot/teams/manager.py`（`TeamError` @ 27-28；`TeamManager.__init__` @ 31-41；`detect_backend` @ 43-50；`create_team` @ 52-86；`get_team` @ 88-97；`get_task_store` @ 99-108；`get_mailbox` @ 110-119；`register_member` @ 121-134；`set_member_idle` @ 136-152；`register_inprocess_handle` @ 154-155；`register_pane_id` @ 157-158；`get_pane_id` @ 160-161；`delete_team` @ 163-201；`get_team_for_teammate` @ 203-210；`on_teammate_completed` @ 212-221；`_kill_pane` @ 223-229；`_cleanup_worktree` @ 231-245；`_remove_dir` @ 247-）
- 依赖任务: T1, T2, T3, T4, T5, T6
- 完成标准: `__init__` 七字段 `_teams / _task_stores / _mailboxes / _inprocess_handles / _pane_ids / _detected_backend / _teammate_team_map` 全初始化空 dict / None；`detect_backend` 第一次后缓存到 `_detected_backend`；`create_team` 链 `detect_backend → unique_team_name → mkdir → AgentTeam(...).save → SharedTaskStore.init_empty → Mailbox` 并缓存三个字典；`register_member` 同时 `AgentNameRegistry.register` + 写 `_teammate_team_map`；`set_member_idle` 翻 is_active 并写 idle 通知到 Lead 邮箱；`delete_team` 先校验全员 `is_active is not False` 必须 idle，否则抛 `TeamError`，通过后遍历清 name registry / handle.cancel / `_kill_pane` / git worktree remove / trace_manager.remove，最后 `mailbox.cleanup_all` + 删目录 + 弹三个缓存。
- [ ] 完成

## T11: 实现 Agent._consume_mailbox 接入
- 影响文件: `codebot/agent.py`（`self.coordinator_mode / self.team_name / self._team_manager` 字段 @ 324-326；`_consume_mailbox` @ 718-733；`run_to_completion` 主循环钩入 @ 433 + @ 957；`coordinator_mode` 传 `build_system_prompt` @ 471 + @ 937）
- 依赖任务: T2, T10
- 完成标准: `Agent.__init__` 加 `self.coordinator_mode: bool = False / self.team_name: str = "" / self._team_manager: Any = None` 三字段；`_consume_mailbox(conversation)` 在 `team_name` 和 `_team_manager` 都非空时取 `team_manager.get_mailbox(team_name).consume(self.agent_id)`；每条消息前缀 `[Message from <sender>] ` 或 `[<message_type> from <sender>] ` 后 `conversation.add_user_message`；异常吞掉记 `log.debug`；在 `run_to_completion` 主循环开头（每轮迭代前）和 `iterate_once` 开头都调一次。
- [ ] 完成

## T12: 实现 coordinator 系统提示词 + 工具过滤
- 影响文件: `codebot/teams/coordinator.py`（`is_coordinator_mode` @ 7-11；`match_session_mode` @ 14-36；`get_coordinator_system_prompt` @ 39-；`get_coordinator_user_context`）；`codebot/agents/tool_filter.py`（`COORDINATOR_MODE_ALLOWED_TOOLS` 12 项 @ 66-79；`TEAMMATE_COORDINATION_TOOLS` 5 项 @ 50-56；`IN_PROCESS_TEAMMATE_ALLOWED_TOOLS` @ 58-64；`apply_coordinator_filter` @ 187-193；`build_teammate_tools` @ 129-184）
- 依赖任务: T5, T10
- 完成标准: `is_coordinator_mode(enable_flag)` 双锁判定（flag false 直接 false；flag true 时读 `CODEBOT_COORDINATOR_MODE` env 三档 `1/true/yes`）；`match_session_mode` 实现恢复会话时的 env var 同步；`get_coordinator_system_prompt` 输出含 `Research / Synthesis / Implementation / Verification` 四阶段、`<task-notification>` XML 格式、`based on your findings` anti-pattern；`COORDINATOR_MODE_ALLOWED_TOOLS = {Agent, SendMessage, TaskCreate, TaskGet, TaskList, TaskUpdate, TeamCreate, TeamDelete, ReadFile, Glob, Grep, Bash}` 12 项（写工具 `WriteFile / EditFile` 被排除）；`apply_coordinator_filter(registry)` 把 registry 筛到白名单；`build_teammate_tools` 按 backend 类型分流：in-process 严格白名单 `IN_PROCESS_TEAMMATE_ALLOWED_TOOLS`，pane 模式只剔除 `TeamCreate` 和 `TeamDelete`。
- [ ] 完成

## T13: 实现 SendMessageTool / TeamCreateTool / TeamDeleteTool 三个工具
- 影响文件: `codebot/tools/send_message.py`（`SendMessageParams` @ 16-21；`VALID_MESSAGE_TYPES` @ 24；`SendMessageTool` @ 27-；`execute` @ 51-109；`_wake_pane` @ 111-119；`_wake_pane_members` @ 121-123）；`codebot/tools/team_create.py`（`TeamCreateParams` @ 14-16；`TeamCreateTool` @ 19-85）；`codebot/tools/team_delete.py`（`TeamDeleteParams` @ 14-15；`TeamDeleteTool` @ 18-53）
- 依赖任务: T2, T5, T10, T12
- 完成标准:
 - `SendMessageTool.execute`：先校验 `message_type in VALID_MESSAGE_TYPES`，`text` 类型必须有 `summary`；`to == "*"` 走 broadcast（member_ids 不含 self，添加 lead_agent_id 如果 self 不是 lead）；否则 `AgentNameRegistry.instance().resolve(to)` 解析后 `mailbox.write`；写完调 `_wake_pane(target_id)` 唤醒 pane 后端；非法 to 返 IsError `Cannot resolve recipient '...'`。
 - `TeamCreateTool.execute`：先 `team_manager.detect_backend` 不通过返 IsError；通过后 `team_manager.create_team`；如 `is_coordinator_mode(enable_coordinator_mode)` 返 true 则 `parent_agent.coordinator_mode = True`、`parent_agent._full_registry = parent_agent.registry`、`parent_agent.registry = apply_coordinator_filter(registry)`，输出附 "Coordinator Mode activated" 提示。
 - `TeamDeleteTool.execute`：调 `team_manager.delete_team` 捕获 `TeamError` 返 IsError；如 `parent_agent.coordinator_mode` 为 true 则 `parent_agent.registry = parent_agent._full_registry` 恢复并清零 flag，输出附 "Coordinator Mode deactivated" 提示。
- [ ] 完成

## T14: 实现 AgentTool._execute_as_teammate（team_name 分支）
- 影响文件: `codebot/tools/agent_tool.py`（`AgentToolParams.team_name` 字段 @ 28；`TEAMMATE_ADDENDUM` 常量 @ 38；`AgentTool.__init__` 加 `team_manager` 参数 @ 72；`_team_manager` 字段 @ 81；`execute` 入口分支 @ 86-87；`_execute_as_teammate` @ 246-414）
- 依赖任务: T10, T12, T13（ch13/ch14 的 AgentTool / WorktreeManager）
- 完成标准:
 - `AgentToolParams` 加 `team_name: str | None = None` 字段；
 - `AgentTool.__init__` 加 `team_manager` 关键字参数和 `_team_manager` 实例字段；
 - `execute` 入口看到 `p.team_name` 非空时优先走 `_execute_as_teammate`（先于 fork / sync / async 分发）；
 - `_execute_as_teammate`：校验 team_manager / worktree_manager 配置、`team_manager.get_team(team_name)` 不存在返 IsError；base_name 同名冲突自动加 `-2/-3/...`；可选解析 `subagent_type`，无 type + enable_fork 走 `build_forked_messages` 否则用空白 builtin AgentDef；`worktree_manager.create(f"team-{team_name}/{teammate_name}", "HEAD")` 建独立 wt；`detect_backend` 决定后端；`build_teammate_tools` 按 backend 构造工具池；用 `AgentClass(agent_id, registry, ...)` 创建 sub-agent 注入 `TEAMMATE_ADDENDUM`；`AgentNameRegistry.instance().register(teammate_name, agent_id)`；构造 `TeammateInfo` 后 `team_manager.register_member`；按 backend 分发 in-process（`spawn_inprocess_teammate`）或 pane（`_spawn_pane_teammate`）。
- [ ] 完成

## T15: app.py 注册三件套 + 注入 team_manager
- 影响文件: `codebot/app.py`（`CodeBotApp.__init__` 加 `teammate_mode / enable_coordinator_mode` 参数 @ 519-520；`_teammate_mode / _enable_coordinator_mode` 字段 @ 530-531；team 系统设置块 @ 730-762；`agent._team_manager = team_manager` 注入 @ 801；`on_teammate_completed` 回调 @ 1287-1288；shutdown 清理 @ 1592-1598）；`codebot/__main__.py`（`teammate_mode / enable_coordinator_mode` 透传 `CodeBotApp` @ 57-58）；`codebot/config.py`（`AppConfig.teammate_mode` / `enable_coordinator_mode` 字段 + load_config 校验 `teammate_mode in {"", "in-process"}`）
- 依赖任务: T11, T13, T14
- 完成标准:
 1. `CodeBotApp.__init__` 加 `teammate_mode: str = ""` 和 `enable_coordinator_mode: bool = False` 参数；
 2. 在 AgentTool 注册之前 `self.team_manager = TeamManager(worktree_manager, trace_manager)`；
 3. AgentTool 构造时传 `team_manager=self.team_manager`；
 4. 注册 `TeamCreateTool(team_manager, parent_agent, teammate_mode, is_interactive=True, enable_coordinator_mode)`；
 5. 注册 `TeamDeleteTool(team_manager, parent_agent)`；
 6. 注册 `SyntheticOutputTool()`；
 7. `self.agent._team_manager = self.team_manager` 写回主 Agent；
 8. 后台 task 完成回调里调 `self.team_manager.on_teammate_completed(task.agent.agent_id)`；
 9. shutdown 时遍历所有团队强制 `set_member_active(False)` 后 `delete_team` 释放资源；
 10. `codebot/__main__.py main()` 把 `config.teammate_mode` / `config.enable_coordinator_mode` 透传 `CodeBotApp`；
 11. `config.py` 加两个字段及 `teammate_mode` 校验（合法值仅 `""` 和 `"in-process"`）。
- [ ] 完成

## T16: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T1-T15
- 完成标准:
 - `ruff check codebot/teams codebot/tools/team_create.py codebot/tools/team_delete.py codebot/tools/send_message.py` 通过；
 - `pytest tests/test_teams.py -v` 通过（覆盖 10 大类 30+ 用例：TestModels 7 + TestSharedTaskStore 6 + TestMailbox 5 + TestAgentNameRegistry 4 + TestBackendDetect 6 + TestToolFilter 3 + TestCoordinatorMode 11 + TestConfigExtensions 3 + TestTranscript 2 + TestAgentCoordinatorIntegration 3）；
 - `pytest tests/test_subagent.py -v` 仍全部通过（确保 AgentTool 改造未破坏 ch13 功能）；
 - 主流程接线验证：`grep -n "TeamManager\|TeamCreateTool\|TeamDeleteTool\|team_manager" codebot/app.py` 命中至少 8 处；`grep -n "_consume_mailbox\|_team_manager\|coordinator_mode" codebot/agent.py` 看到主 Agent 三处接入；`grep -n "_execute_as_teammate" codebot/tools/agent_tool.py` 命中入口分发 + 函数体。
- [ ] 完成

## 进度
- [ ] T1 / [ ] T2 / [ ] T3 / [ ] T4 / [ ] T5 / [ ] T6 / [ ] T7 / [ ] T8 / [ ] T9 / [ ] T10 / [ ] T11 / [ ] T12 / [ ] T13 / [ ] T14 / [ ] T15 / [ ] T16

```

```markdown
# ch15: AgentTeam Checklist

> 所有条目可勾选、可观测。验收方式写在条目后面括号中。验收：已通过验证的项均勾选。

## 1. 实现完整性

- [ ] 枚举 `BackendType` 在 `codebot/teams/models.py:10-13` 含三档常量 `TMUX="tmux" / ITERM2="iterm2" / IN_PROCESS="in-process"`
- [ ] dataclass `TeammateInfo` 在 `codebot/teams/models.py:16-31` 7 字段含 `name / agent_id / agent_type / model / worktree_path / backend_type / is_active`，`is_active: bool | None = None` 三值语义
- [ ] dataclass `AgentTeam` 在 `codebot/teams/models.py:40-102` 含 `members: list[TeammateInfo]`、`get_member` 同时按 name 和 agent_id 双向查找、`set_member_active` / `all_idle` / `active_members` / `save` / `load` 全方法
- [ ] `resolve_team_dir` / `unique_team_name` 在 `codebot/teams/models.py:105-117`，落到 `~/.codebot/teams/<slug>/`，同名冲突自动加 `-2/-3/...` 后缀
- [ ] `MailboxMessage` dataclass 在 `codebot/teams/mailbox.py:11-27` 8 字段，`message_type` 注释三档 `text | shutdown_request | shutdown_response`
- [ ] `Mailbox` 在 `codebot/teams/mailbox.py:30-102` 实现单文件单消息模型 `<base>/<agent_id>/<timestamp>_<id>.json`，`write / read / consume / broadcast / cleanup / cleanup_all` 六个方法齐全
- [ ] `create_message` 在 `codebot/teams/mailbox.py:105-122` 自动填 `uuid.uuid4().hex[:12]` 和 `time.time()`
- [ ] `BackendDetectionError` + `detect_backend` 在 `codebot/teams/backend_detect.py:9-51` 实现优先级链，失败抛错而非静默回退
- [ ] `SharedTask` + `SharedTaskStore` 在 `codebot/teams/shared_task.py:9-` 实现 JSON 文件 `{"next_id", "tasks": [...]}` 存储和 `create / get / list_tasks / update / init_empty` 五方法
- [ ] `AgentNameRegistry` 单例在 `codebot/teams/registry.py:6-40` 线程安全 double-checked locking，`resolve` 同时支持 name 和 agent_id 反查
- [ ] `InProcessTeammateHandle` + `spawn_inprocess_teammate` 在 `codebot/teams/spawn_inprocess.py:14-56` 用 `asyncio.create_task` 起协程；handle.done / result / cancel 三属性
- [ ] `build_cli_command` 在 `codebot/teams/spawn_tmux.py:32-56` 输出 `CODEBOT_TEAM_NAME=X CODEBOT_TEAMMATE_NAME=Y CODEBOT_MAILBOX_DIR=Z codebot -p --work-dir <wt> '<prompt>'`，prompt 内单引号转义为 `'\''`
- [ ] `spawn_tmux_teammate` 在 `codebot/teams/spawn_tmux.py:59-108` 三级 fallback（split-window → new-window → new-session）
- [ ] `kill_pane` / `send_keys_to_pane` 在 `codebot/teams/spawn_tmux.py:111-122` best-effort 静默失败
- [ ] `spawn_iterm2_teammate` 在 `codebot/teams/spawn_iterm2.py:31-58` 复用 `build_cli_command`，通过 `it2 split-pane` 创建 pane
- [ ] `save_transcript / load_transcript` 在 `codebot/teams/transcript.py:67-92` 序列化 `ConversationManager.history` 含 tool_uses / tool_results 块到 `<team_dir>/transcripts/<agent_id>.json`
- [ ] `TeamManager` 在 `codebot/teams/manager.py:31-201` 7 内部字典 + 13 个公开方法齐全；`__init__` 接受 `worktree_manager` 和 `trace_manager`；`_detected_backend` 第一次后缓存
- [ ] `delete_team` 在 `codebot/teams/manager.py:163-201` 先校验 `is_active is not False` 必须 idle，否则抛 `TeamError`
- [ ] `COORDINATOR_MODE_ALLOWED_TOOLS` 在 `codebot/agents/tool_filter.py:66-79` 含 12 项 `{Agent, SendMessage, TaskCreate, TaskGet, TaskList, TaskUpdate, TeamCreate, TeamDelete, ReadFile, Glob, Grep, Bash}`（写工具 `WriteFile / EditFile` 被排除）
- [ ] `IN_PROCESS_TEAMMATE_ALLOWED_TOOLS` 在 `codebot/agents/tool_filter.py:58-64` 是 `ASYNC_AGENT_ALLOWED_TOOLS | TEAMMATE_COORDINATION_TOOLS | {CronCreate, CronDelete, CronList}` 联合
- [ ] `build_teammate_tools` 在 `codebot/agents/tool_filter.py:129-184` 按 backend 类型分流：in-process 严格白名单、pane 模式只剔除 `TeamCreate` 和 `TeamDelete`
- [ ] `apply_coordinator_filter` 在 `codebot/agents/tool_filter.py:187-193` 把 registry 筛到 `COORDINATOR_MODE_ALLOWED_TOOLS`
- [ ] `get_coordinator_system_prompt` 在 `codebot/teams/coordinator.py:39-` 输出含 `Research / Synthesis / Implementation / Verification` 四阶段、`<task-notification>` XML 格式、`based on your findings` anti-pattern
- [ ] `SendMessageTool` 在 `codebot/tools/send_message.py:27-123` 实现 `to / message / summary / message_type / metadata` 五参数；`to == "*"` 走 broadcast；`text` 类型必须有 `summary`
- [ ] `TeamCreateTool` 在 `codebot/tools/team_create.py:19-85` 实现 `team_name + description`；Coordinator Mode 激活时备份 `_full_registry`
- [ ] `TeamDeleteTool` 在 `codebot/tools/team_delete.py:18-53` 实现 `team_name`；Coordinator Mode 还原 `_full_registry`
- [ ] `AgentTool._execute_as_teammate` 在 `codebot/tools/agent_tool.py:246-414` 处理 `team_name != None` 分支，含 worktree 创建 / build_teammate_tools / register_member / spawn 分发

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `grep -n "TeamManager" codebot/app.py` 在 `codebot/app.py:731-735` 找到导入和 `self.team_manager = TeamManager(worktree_manager, trace_manager)` 创建
- [ ] `grep -n "TeamCreateTool\|TeamDeleteTool" codebot/app.py` 在 `codebot/app.py:749-762` 找到两个工具注册点
- [ ] `agent_tool = AgentTool(..., team_manager=self.team_manager)` 注入点在 `codebot/app.py:737-746`
- [ ] `self.agent._team_manager = self.team_manager` 注入点在 `codebot/app.py:801`
- [ ] `self.team_manager.on_teammate_completed(task.agent.agent_id)` 在 `codebot/app.py:1287-1288` 后台任务完成回调
- [ ] shutdown 清理在 `codebot/app.py:1592-1598` 遍历所有团队强制 set_member_active(False) 后 delete_team
- [ ] `codebot/__main__.py:57-58` 把 `config.teammate_mode` / `config.enable_coordinator_mode` 透传 `CodeBotApp`
- [ ] `Agent.__init__` 在 `codebot/agent.py:324-326` 声明 `self.coordinator_mode / self.team_name / self._team_manager` 三字段
- [ ] `Agent._consume_mailbox` 在 `codebot/agent.py:718-733` 实现；`codebot/agent.py:433 + :957` 在主循环开头钩入
- [ ] `Agent.run_to_completion` 在 `codebot/agent.py:471 + :937` 把 `coordinator_mode=self.coordinator_mode` 传给 `build_system_prompt`
- [ ] `AgentTool.execute` 入口分支在 `codebot/tools/agent_tool.py:86-87` 看到 `p.team_name` 非空时优先走 `_execute_as_teammate`
- [ ] `AgentTool.__init__` 接受 `team_manager` 参数在 `codebot/tools/agent_tool.py:72`，写入 `self._team_manager` 在 `:81`

## 3. 编译与测试

- [ ] `ruff check codebot/teams codebot/tools/team_create.py codebot/tools/team_delete.py codebot/tools/send_message.py` 无错误
- [ ] `pytest tests/test_teams.py -v` 通过（覆盖至少 30 个用例：TestModels 7 个 / TestSharedTaskStore 6 个 / TestMailbox 5 个 / TestAgentNameRegistry 4 个 / TestBackendDetect 6 个 / TestToolFilter 3 个 / TestCoordinatorMode 11 个 / TestConfigExtensions 3 个 / TestTranscript 2 个 / TestAgentCoordinatorIntegration 3 个）
- [ ] `pytest tests/test_subagent.py -v` 全部通过（确保 AgentTool 改造未破坏 ch13）
- [ ] `pytest tests/test_agent.py -v` 全部通过（确保 Agent.__init__ 新字段未破坏现有用例）
- [ ] 测试运行不在用户主目录残留 `~/.codebot/teams/` 目录（fixture 用 `patch("codebot.teams.models.Path.home", return_value=Path(tmp_dir))` 重定向）

## 4. 端到端验证

- [ ] 注册路径：`CodeBotApp.__init__` 在 `codebot/app.py:730-762` 创建 `TeamManager` 并把 `TeamCreate / TeamDelete / SendMessage` 三件套放入 registry；用户向 Lead 说 "create a team to refactor X" → LLM 调 `TeamCreate(team_name="refactor-X")` → `detect_backend()` 选模式 → Output 返回 `Team refactor-X created successfully. Backend: ... Config: ~/.codebot/teams/refactor-x/config.json`
- [ ] Spawn 路径：Lead 继续说 "spawn alice to do data layer" → LLM 调 `Agent(team_name="refactor-X", name="alice", prompt="...")` → `AgentTool.execute` 在 `codebot/tools/agent_tool.py:86-87` 识别 `team_name` 分支调 `_execute_as_teammate` → `worktree_manager.create(f"team-refactor-X/alice")` → `build_teammate_tools` → `spawn_inprocess_teammate` / `spawn_tmux_teammate` / `spawn_iterm2_teammate` 按 backend 分发 → 队员开始干活
- [ ] 通信路径：队员 alice 通过 `SendMessage(to="bob", message="...", summary="...")` 给 bob 写 mailbox → `AgentNameRegistry.resolve("bob")` 拿到 target_id → `mailbox.write(target_id, msg)` → `_wake_pane(target_id)`（pane 后端需要）→ bob 下一轮 `_consume_mailbox` 收到消息作为 user message
- [ ] Lead 感知路径：每个队员后台 task 完成时 `app.py:1287-1288` 调 `team_manager.on_teammate_completed(agent_id)` → 找到所在团队后 `set_member_idle(team_name, name)` → 翻 `is_active=False` + 写一条 `Teammate '<name>' is now idle (run_to_completion finished).` 到 Lead 邮箱 → Lead 下一轮 `_consume_mailbox` 注入对话
- [ ] Coordinator Mode 路径：启用 `enable_coordinator_mode=True` 且 `CODEBOT_COORDINATOR_MODE=1` → `TeamCreateTool.execute` 把 `parent_agent.coordinator_mode = True / _full_registry 备份 / registry = apply_coordinator_filter(registry)` → Lead 每轮工具集只剩 12 项白名单；调 `WriteFile` / `EditFile` 会找不到工具被拒绝；`TeamDelete` 清空团队后恢复 `_full_registry`
- [ ] Tmux 后端：`TMUX` env 非空时 `detect_backend` 返 `BackendType.TMUX` → `spawn_tmux_teammate` 用 `build_cli_command` 拼出 `CODEBOT_TEAM_NAME=refactor-X CODEBOT_TEAMMATE_NAME=alice CODEBOT_MAILBOX_DIR=... codebot -p --work-dir /tmp/wt 'prompt'` → tmux send-keys 启动子进程 → 子进程加载同一份 mailbox 目录开始 _consume_mailbox 轮询
- [ ] iTerm2 后端：`TERM_PROGRAM=iTerm.app` 且 `shutil.which("it2")` 非空且不在 tmux 时 `detect_backend` 返 `BackendType.ITERM2` → `spawn_iterm2_teammate` 用 `it2 split-pane --command "/bin/zsh -c '<cmd>'"` 创建 pane
- [ ] 关闭路径：`TeamDelete(team_name="refactor-X")` → `team_manager.delete_team` → 校验全员 idle → 遍历每个 member 清 name registry / cancel handle / kill pane / git worktree remove / trace_manager.remove → cleanup mailbox + 删团队目录 → 弹出 `_teams / _task_stores / _mailboxes` 三个缓存 → 如 Lead 在 Coordinator Mode 则恢复 `_full_registry`

## 5. 文档

- [ ] `docs/python/ch15/spec.md` 已写
- [ ] `docs/python/ch15/tasks.md` 已写，16 个 T 全部勾完
- [ ] `docs/python/ch15/checklist.md` 已写并逐项验收
- [ ] commit 信息标注 `ch15` 与三件套关闭状态（待用户确认后由人或 CI 触发）

```

### Java

```markdown
# ch15: AgentTeam Spec

## 1. 背景

SubAgent（ch13）解决了一次性子任务的上下文隔离，但拓扑是星型：所有子 Agent 只能和主 Agent 通信，子 Agent 之间彼此看不见。当任务规模上来——四个模块同时重构、多角度并行调查 bug、一个 Agent 需要把发现告诉另一个——星型拓扑下主 Agent 成了信息中转瓶颈，子任务被迫串行。这一章把"长期协作团队"做成 CodeBot 的一等概念：多个 Agent 组成 Team，并行干活、直接互发消息、共享任务列表，主 Agent 升级为 Team Lead 专职调度。Java 版本利用 JDK 21 虚拟线程跑 in-process 队员，外部后端则通过 `ProcessBuilder` 拉起 tmux / iTerm2 进程，由共享 `FileMailBox` 目录串联跨进程通信。

## 2. 目标

提供 `TeamManager` / `TeamManager.Team` / `TeamManager.Member` / `FileMailBox` / `SharedTaskStore` / `AgentNameRegistry` / `Coordinator` / `TeamTools.SendMessageTool` / `TeamTools.TeamCreateTool` / `TeamTools.TeamDeleteTool` 一整套类型与工具，让 LLM 在对话里：1) 调 `TeamCreate` 建团队（按环境自动选 tmux / in-process 后端），2) 后续通过 `Agent` 工具带 `team_name` 把队员加入团队，3) 队员之间通过 `SendMessage` 走 `FileMailBox` 互发消息、idle 后通知 Lead，4) Lead 借助 `Coordinator.ALLOWED_TOOLS` 收窄工具集进入纯调度模式。tmux 后端由 `SpawnDispatcher.buildTeammateCLI` 拼出 `codebot --teammate --team-name X --agent-name Y` 由独立进程跑 worker，和 Lead 共享同一份 mailbox 目录。

## 3. 功能需求

- F1: `TeamManager.TeamMode` 枚举包含 `IN_PROCESS / TMUX` 两档；`TeamManager.detectBackend()` 按 `TMUX` 环境变量 → `which tmux` 命中 → 退化到 `IN_PROCESS` 的优先级自动选择。
- F2: `TeamManager.Team` 持有 `name / mode / members LinkedHashMap / mailBox` 字段；`TeamManager.Member` 含 `name / agent / conv / active / thread` 字段，外部后端的 Member 由 `SpawnDispatcher.recordExternalMember` 创建，`agent` 与 `conv` 字段保持为 null。
- F3: `TeamManager` 提供 `createTeam` / `getTeam` / `deleteTeam` / `listTeams` / `closeAll` 同步方法；`Team` 暴露 `addMember` / `startMember` / `stopMember` / `stopAll` / `getMember` / `hasMember` / `memberNames` / `sendMessage`，全部用 `synchronized` 保护成员表。
- F4: `FileMailBox` 基于 `<baseDir>/<agentId>.json` 文件持久化消息；`send` / `readUnread` / `markAllRead` 三件套；并发安全靠 `<agentId>.json.lock` 文件锁，`Files.createFile` 抛 `FileAlreadyExistsException` 时重试（最多 10 次，5-100ms 随机退避），>10s 视为过期锁强制清理。
- F5: `FileMailBox.MailMessage` 记录类含 `from / text / timestamp / read / color / summary` 六个字段；便利构造器 `MailMessage(from, text)` 自动填 `Instant.now()` 时间戳、`read=false`、空 color/summary；`send` 落盘时强制把 `read` 置 false。
- F6: `SpawnDispatcher.spawnTeammate(SpawnConfig)` 统一入口按 `Team.mode` 分发到 in-process / tmux 两条路径，返回 `SpawnResult{mode, paneId}`。`IN_PROCESS` 模式调 `team.addMember` 注册并用 `Thread.startVirtualThread` 跑 `TeammateRunner.runInProcessTeammate`；`TMUX` 模式先把 task 写入对方 mailbox，再拼 CLI 调 `TmuxBackend.spawnTmuxTeammate`，最后 `recordExternalMember` 注册。
- F7: `SpawnDispatcher.buildTeammateCLI(teamName, memberName, workdir)` 用 `ProcessHandle.current().info().command()` 拿当前可执行路径；workdir 空时退化到 `System.getProperty("user.dir")`；输出 `cd <quoted_wd> && <quoted_exe> --teammate --team-name <quoted_team> --agent-name <quoted_member>`，所有变量经 `shellQuote` 处理。
- F8: `SpawnDispatcher.shellQuote(s)` 简单字符（`[a-zA-Z0-9_./-]+`）直接返回；含特殊字符时单引号包裹并把内嵌的 `'` 替换为 `'\''`（POSIX 标准转义）。
- F9: `TmuxBackend.spawnTmuxTeammate` 用 `tmux new-window -d -n <teamName>-<memberName> <cliCommand>` 创建后台窗口；命令返回码非 0 或超时（30s）抛 `RuntimeException("Failed to spawn tmux window: ...")`；`TmuxBackend.stopTmuxTeammate` 先 `send-keys C-c` 再 `kill-window`，best-effort 不重抛异常，失败仅 `log.fine`。
- F10: `ITermBackend.spawnITermTeammate` 用 `osascript -e <AppleScript>` 在 iTerm2 当前 window 创建 tab 并 `write text <cliCommand>`，内嵌双引号转义为 `\"`；30s 超时；`stopITermTeammate` 遍历所有 window 和 tab 找名字匹配的 close 掉，10s 超时、best-effort 失败静默。
- F11: `TeammateRunner.runInProcessTeammate(team, member, initialPrompt, addendum)` 队员主循环：先把 addendum 作为 system reminder 注入 → 调 `injectPendingMessages` 把未读邮件转 system reminder → 把 `initialPrompt` 加为 user message → 调 `member.agent.run(conv)` 跑一轮 → 通过 `drainAgentEvents` 转发事件 → 给 Lead 发 `[idle]` 通知 → 循环 `waitForNextPromptOrShutdown` 轮询邮箱，500ms 间隔，命中新消息加为 user message 跑下一轮，命中 shutdown 或线程中断退出。退出前置 `member.active=false`。
- F12: `TeammateRunner.LEAD_NAME = "lead"` / `SHUTDOWN_PREFIX = "[shutdown]"` / `IDLE_POLL_MS = 500` 三常量；`isShutdownRequest(text)` 用 `text.strip().startsWith(SHUTDOWN_PREFIX)` 判定；`createIdleNotification(memberName, reason)` 产出 `"[idle] <name>: <reason> (at <iso-instant>)"` 文本。
- F13: `TeammateRunner.drainLeadMailbox(teamMgr)` 扫所有团队的 Lead 收件箱，把未读消息按 `<team-notification team="X">\nfrom=Y: text\n...\n</team-notification>` 包装返回 `List<String>`，并把消息标记为已读；`teamMgr == null` 时返回 `List.of()`。
- F14: `TeammateRunner.buildTeammateAddendum(teamName, memberName, otherMembers)` 产出注入到队员对话顶端的 system reminder，告诉它身份、其他队友名字、必须通过 `SendMessage` 沟通、停止调用工具会自动发 idle 通知给 Lead。
- F15: `TeammateRunner.injectPendingMessages(team, memberName, conv)` 读 mailbox 未读，非空时拼 `"You have new messages:\n\nFrom <sender>: \n\n..."` 作为 system reminder 注入并 `markAllRead`，无未读直接返回。
- F16: `Coordinator.ALLOWED_TOOLS` 是 12 项白名单 `Set<String>`：`Agent / SendMessage / TaskCreate / TaskGet / TaskList / TaskUpdate / TeamCreate / TeamDelete / ReadFile / Glob / Grep / Bash`；`Coordinator.isCoordinatorTool(name)` 返回 set 命中布尔。写工具 `WriteFile / EditFile` 等被排除。
- F17: `TeamTools.SendMessageTool` 暴露 `to / content` 两个必填字段；`execute` 遍历所有团队找 `to` 这个 member 所在团队调 `team.sendMessage(senderName, to, content)` 投递；未匹配返 `recipient '<to>' not found in any team` 错误。
- F18: `TeamTools.TeamCreateTool` 暴露 `team_name` 必填、`description` 可选；同名时追加 `-2/-3/...` 后缀去重；调 `TeamManager.detectBackend()` + `teamMgr.createTeam`；Output 提示 `"Team \"X\" created (mode: Y). Use Agent tool with team_name=\"X\" to add teammates."`。
- F19: `TeamTools.TeamDeleteTool` 暴露 `team_name` 必填；不存在返错误；调 `teamMgr.deleteTeam`（内部 `stopAll` 中断所有 member 的虚拟线程）；返回 `"Team \"X\" deleted. Stopped N member(s): a, b, c"` 清单。
- F20: `AgentNameRegistry` 是单例（`getInstance()`），维护 `name → agentId` 映射；`register / resolve / unregister / listAll` 全部 `synchronized`；`resolve` 支持反向匹配——传入的字符串既可以是 name 也可以是 agentId，两边都查不到返 null。
- F21: `SharedTaskStore` 基于 `<teamDir>/tasks.json` 持久化 `SharedTask` 记录列表；`create / get / listTasks / update` 全部 `synchronized`；`update` 支持 `status / assignee` 覆盖以及 `addBlocks / addBlockedBy` 追加（不替换），自增 `id` 由 `AtomicInteger` 保证。

## 4. 非功能需求

- N1: FileMailBox 跨进程并发安全——tmux 启动的队友进程和 Lead 进程不共享 JVM 堆，必须靠文件锁保证写入原子性。锁文件 10 秒过期自动清理避免死锁。
- N2: 外部后端队员的初始任务必须在 spawn 之前写入 mailbox，因为 tmux 新进程启动到第一次 idle poll 期间无法接消息；先写后启即可保证第一次 poll 必命中。
- N3: In-process 队员的虚拟线程退出路径有三条：`Thread.currentThread().isInterrupted()` 为真、收到 shutdown 消息、`agent.run` 自然结束后无新消息。退出时必须置 `member.active=false`，否则 Lead 拿不到队员已停的状态。
- N4: Coordinator Mode 通过 `Coordinator.isCoordinatorTool` 在每轮迭代开头动态判定，而非一次性裁剪 registry。这样团队全部 Delete 后下一轮 Lead 自动恢复全工具集，无需重建 registry。
- N5: 队员的 `buildTeammateAddendum` 必须明确告诉 LLM "纯文本回复对队友不可见，最终结果必须通过 `SendMessage` 发给 Lead"——否则队员模型容易写一段汇报作为最后输出就结束，Lead 永远拿不到结果（只能看到 idle 通知）。
- N6: `SendMessage` 当前实现走"遍历所有团队找 `to` member"路径；若 Lead 不在任何 team.members 中，给 Lead 发消息会失败。Java 版的简化方案是发送时直接走当前 Sender 所在团队的 mailbox.send（绕过 hasMember 检查）。
- N7: `SpawnDispatcher.buildTeammateCLI` 必须把 `workdir / codebot / teamName / memberName` 都通过 `shellQuote` 包裹，否则空格或特殊字符的 workdir 路径会破坏 shell 解析；`shellQuote` 单引号转义遵循 POSIX `'\''` 标准。
- N8: `ITermBackend` 里的 AppleScript 字面量必须把内嵌的双引号转义为 `\"`，否则 `osascript -e` 解析失败；关闭流程是 best-effort，找不到 tab 不应报错（用户可能手动关掉了）。
- N9: `TeammateRunner.runInProcessTeammate` 应当使用 JDK 21 虚拟线程（`Thread.startVirtualThread`）而非平台线程，避免大团队时线程开销爆炸；mailbox 轮询采用 `Thread.sleep(IDLE_POLL_MS)` 而非自旋。
- N10: 测试运行时 `@TempDir` 必须用 `org.junit.jupiter.api.io.TempDir`，让 FileMailBox 写到测试临时目录，否则跑完测试会在仓库根残留 `.codebot/teams/` 目录；并发测试需用 `ExecutorService` + `CountDownLatch` 验证文件锁正确性。

## 5. 设计概要

- 核心类型:
 - `TeamManager`：全局团队注册表（`Map<String, Team>` + `synchronized` 方法），暴露 CRUD + `detectBackend` 静态方法。
 - `TeamManager.Team`：团队聚合，持有 `mode` 决定后端、`members LinkedHashMap` 注册表、`mailBox FileMailBox` 通信媒介，所有写方法 `synchronized`。
 - `TeamManager.Member`：队员元信息，in-process 模式 `agent + conv` 有值（LLM 跑在虚拟线程），tmux 模式两者为空、`thread` 也为空、靠 paneId（存储为 `name` 字段一部分）句柄。
 - `FileMailBox` + `FileMailBox.MailMessage`：文件锁 + JSON 数组的 mailbox 实现，跨进程共享同一目录，依赖 Jackson `ObjectMapper`。
 - `SpawnDispatcher.SpawnConfig` / `SpawnResult`：`spawnTeammate` 的入参/出参 record，把 in-process 与 tmux 后端的差异收敛到统一返回类型。
 - `Coordinator.ALLOWED_TOOLS`：12 项白名单 `Set<String>`，TUI 每轮按 `teamMgr.listTeams().isEmpty()` 决定是否启用过滤。
 - `SharedTaskStore`：JSON 持久化的任务表，提供 `id / title / description / status / assignee / blocks / blockedBy` 字段及 `addBlocks / addBlockedBy` 追加语义。
 - `AgentNameRegistry`：全局单例 `name → agentId` 映射，方便 SendMessage 通过名字寻址。
- 主流程（按生命周期）:
 - 创建：用户消息 → 主 Agent → LLM 调 `TeamCreate(team_name)` → `TeamManager.detectBackend()` 选模式 → `teamMgr.createTeam` 落到 `~/.codebot/teams/<name>/inboxes/` → 返回 mode 提示给 Lead。
 - Spawn 队员：Lead LLM 调 `Agent(team_name=X, name=Y, prompt=Z)` → AgentTool 识别 `team_name` 走 team 分支 → `SpawnDispatcher.spawnTeammate(SpawnConfig)` → 按 mode 分发。
 - In-process：`team.addMember` 注册成员 → `Thread.startVirtualThread` 跑 `TeammateRunner.runInProcessTeammate` → 队员在自己的虚拟线程里跑 agent loop。
 - 外部后端：先把初始任务写 mailbox → `buildTeammateCLI` 拼命令 → `TmuxBackend.spawnTmuxTeammate` 调 `tmux new-window` → 新进程跑 `codebot --teammate` worker 模式 → 第一次 idle poll 命中初始消息开始干活。
 - 通信：队员 → `SendMessage` 工具 → 找对方所在团队 → `team.sendMessage` → `mailBox.send` 写文件。队员收信走 `runInProcessTeammate` 顶端的 `injectPendingMessages` 或 `waitForNextPromptOrShutdown`。
 - Lead 感知：每轮 Lead Agent 开头调 `TeammateRunner.drainLeadMailbox` → 抽 Lead 邮箱所有未读 → 包成 `<team-notification>` system reminder 喂回 LLM。
 - Coordinator Mode：只要 `teamMgr.listTeams()` 非空，TUI 把 Lead 的工具调用拦截 → `Coordinator.isCoordinatorTool(name)` 判定 → 非白名单工具被过滤 → 全部团队清理后下一轮恢复全工具集。
 - Stop：`TeamDelete` 工具 → `teamMgr.deleteTeam` → `team.stopAll` 遍历 member 调 `thread.interrupt()`（in-process）或后端关闭脚本（tmux/iTerm）。
- 调用链（模块层级）:
 - TUI 装配 → 创建 `TeamManager` → 注册 `TeamCreateTool / TeamDeleteTool / SendMessageTool` 三个工具
 - Agent loop 每轮调 `TeammateRunner.drainLeadMailbox` 拼到下一轮系统提示
 - Lead 工具集过滤通过 `Coordinator.isCoordinatorTool` 在每次工具调用前判定
 - 外部工作进程入口 `CodeBot.main` 增加 `--teammate` flag 早期拦截，命中走 worker bootstrap 不进 TUI（当前 `CodeBot.java` 尚未实现此路径，是后续扩展点）
- 与其他模块的交互:
 - 依赖 `com.codebot.agent`（Agent / AgentEvent）、`com.codebot.conversation`（ConversationManager）、`com.codebot.llm`（LlmClient）、`com.codebot.tool`（Tool / ToolRegistry / ToolCategory / ToolResult）
 - 被 AgentTool（解析 `team_name` 参数）、TUI（注册工具 + 收件箱 drain + Coordinator filter）、`CodeBot.main`（未来 worker 入口）调用

## 6. Out of Scope

- 不实现完整的 `TeammateInfo` 模型（`agentType / model / planModeRequired` 字段、planModeRequired 审批工作流）——本章仅做工具链层面的 Team / Member 骨架。
- 不实现 `plan_approval_response` / `shutdown_response` 结构化消息类型——目前仅 `[shutdown]` 文本前缀 + 纯文本消息两种。
- 不实现 `CodeBot --teammate` worker 进程入口完整实现——`SpawnDispatcher.buildTeammateCLI` 已经能产出命令，但 `CodeBot.java` 的 main 还没接 `parseTeammateFlags`，留作后续章节扩展。
- 不实现 `TeamManager.createTeamWith` 让外部 worker 进程注册本地构造的 Team——当前 worker 入口未实现，所以此扩展点不必要。
- 不实现 iTerm2 后端在 `SpawnDispatcher` 内的分支——`ITermBackend` 类已经存在但 `spawnTeammate` 的 switch 没接 `ITERM` 分支；本章先保证 tmux + in-process 两档可用。
- 不实现共享任务依赖图的 BFS 校验/循环依赖检测——`SharedTaskStore.update` 只做字段追加，不验证 `blocks/blockedBy` 是否构成环。
- 不实现"协调模式四阶段工作流"系统提示词注入（Research / Synthesis / Implementation / Verification）——`Coordinator` 仅做工具收窄，不做提示词增强。
- 不实现"配置持久化到 ~/.codebot/teams/<name>/config.json" 的团队元数据——只持久化邮箱 JSON 和 tasks.json，Team 实例本身随 JVM 退出消失。
- 不实现 Worktree 团队层面的"收敛阶段 Lead 用 Bash 跑 git merge"自动化——合并由 Lead LLM 自己用 Bash 工具完成，本章不做封装。

## 7. 完成定义

见 [checklist.md](checklist.md)，所有条目勾上即完成。

```

```markdown
# ch15: AgentTeam Tasks

> 任务粒度：每个任务可在一次会话内完成，可独立交付。

## T1: 定义 TeamManager / Team / Member / TeamMode
- 影响文件: `src/main/java/com/codebot/teams/TeamManager.java`（`TeamMode` 枚举 @ 21；`teams` map @ 23；`createTeam` @ 25-29；`getTeam` @ 31-33；`deleteTeam` @ 35-40；`listTeams` @ 42-44；`closeAll` @ 46-51；`detectBackend` @ 53-62；`teamsBaseDir` @ 66-68；`Team` 内部类 @ 70-134；`Member` 内部类 @ 136-151）
- 依赖任务: 无
- 完成标准: `TeamMode` 枚举含 `IN_PROCESS / TMUX`；`Team` 字段 `name / mode / members / mailBox` 齐全；`Team` 方法 `addMember / startMember / stopMember / stopAll / getMember / hasMember / memberNames / sendMessage` 全部 `synchronized`；`Member` 字段 `name / agent / conv / active / thread` 齐全（`active / thread` volatile）；`TeamManager` 顶层 CRUD 方法全部 `synchronized`；`detectBackend` 优先级 `TMUX env → which tmux → IN_PROCESS`。
- [ ] 完成

## T2: 实现 FileMailBox（JSON + 文件锁）
- 影响文件: `src/main/java/com/codebot/teams/FileMailBox.java`（`MailMessage` record @ 16-21；常量 `MAPPER / MAX_RETRIES / MIN_SLEEP_MS / MAX_SLEEP_MS` @ 23-26；构造器 @ 30-35；`inboxPath` @ 37-39；`lockPath` @ 41-43；`send` @ 45-51；`readUnread` @ 53-60；`markAllRead` @ 62-70；`withLock` @ 76-112；`readInbox` @ 114-123；`writeInbox` @ 125-131）
- 依赖任务: 无
- 完成标准: 每个收件人对应 `<baseDir>/<agentId>.json`；`MailMessage` record 含 6 字段且便利构造器自动填 timestamp/read=false；`send` 落盘时把 `read` 强制置 false；`markAllRead` 用 `withLock` 批量翻转所有消息为 read=true；并发安全靠 `<agentId>.json.lock` 文件用 `Files.createFile` 抛 `FileAlreadyExistsException` 时重试，最多 10 次 5-100ms 随机退避，>10s 视为过期锁清理；`withLock` 在 fn 返回后 finally 删锁文件；Jackson 用 `ObjectMapper` 默认配置 + `TypeReference<List<MailMessage>>`。
- [ ] 完成

## T3: 实现 Tmux 后端
- 影响文件: `src/main/java/com/codebot/teams/TmuxBackend.java`（`spawnTmuxTeammate` @ 16-27；`stopTmuxTeammate` @ 29-41）
- 依赖任务: T1
- 完成标准: `spawnTmuxTeammate` 用 `ProcessBuilder("tmux", "new-window", "-d", "-n", paneName, cliCommand)` 创建后台窗口；30s 超时，非 0 退出码或超时抛 `RuntimeException`；`stopTmuxTeammate` 先 `send-keys C-c` 等 5s + `Thread.sleep(200)` 再 `kill-window`，best-effort 失败仅 `log.fine` 不重抛。
- [ ] 完成

## T4: 实现 iTerm2 后端
- 影响文件: `src/main/java/com/codebot/teams/ITermBackend.java`（`spawnITermTeammate` @ 16-40；`stopITermTeammate` @ 42-61）
- 依赖任务: T1
- 完成标准: `spawnITermTeammate` 用 `osascript -e <AppleScript>` 在当前 window 创建 tab 设 name 并 `write text <cliCommand>`，内嵌双引号转义为 `\"`；30s 超时；`stopITermTeammate` AppleScript 遍历所有 window 的 tab 找 name 匹配的 close 掉，10s 超时、best-effort 失败仅 `log.fine`。
- [ ] 完成

## T5: 实现队员主循环 TeammateRunner.runInProcessTeammate
- 影响文件: `src/main/java/com/codebot/teams/TeammateRunner.java`（常量 `LEAD_NAME / SHUTDOWN_PREFIX / IDLE_POLL_MS` @ 16-18；`runInProcessTeammate` @ 26-66；`waitForNextPromptOrShutdown` @ 142-170；`drainAgentEvents` @ 172-187）
- 依赖任务: T1, T2
- 完成标准: 主循环 7 步——1) addendum 非空时加为 system reminder；2) `injectPendingMessages` 把未读邮件转 system reminder；3) `addUserMessage(initialPrompt)`；4) `member.agent.run(conv)` 拿 event queue；5) `drainAgentEvents` 转发到 eventOut；6) `sendMessage(self, LEAD, "[idle]...")` 发 idle 通知；7) 进入 while 循环 `waitForNextPromptOrShutdown` 轮询，shutdown 或线程中断退出，命中新消息加为 user message 继续下一轮。退出前置 `member.active=false`。`drainAgentEvents` 收到 `LoopComplete` 或 `ErrorEvent` 即返回。
- [ ] 完成

## T6: 实现 Lead-side 通信原语
- 影响文件: `src/main/java/com/codebot/teams/TeammateRunner.java`（`drainLeadMailbox` @ 72-92；`buildTeammateAddendum` @ 97-109；`injectPendingMessages` @ 114-127；`isShutdownRequest` @ 129-131；`createIdleNotification` @ 133-136）
- 依赖任务: T1, T2
- 完成标准: `drainLeadMailbox(null)` 返 `List.of()`；非空时遍历所有团队读 Lead 邮箱，按 `<team-notification team="X">\nfrom=Y: text\n...\n</team-notification>` 包装返字符串数组，并把读过的标记为已读。`buildTeammateAddendum` 文本必须含队员名、其他队友名、"通过 SendMessage 沟通"、"停止调用工具自动发 idle"四条信息。`injectPendingMessages` 在有未读时拼 `"You have new messages:\n\n..."` system reminder 并 `markAllRead`，无未读直接返回。`isShutdownRequest` 用 `text.strip().startsWith(SHUTDOWN_PREFIX)` 判定。`createIdleNotification` 产出 `"[idle] <name>: <reason> (at <iso-instant>)"`。
- [ ] 完成

## T7: 实现 SpawnDispatcher 统一入口
- 影响文件: `src/main/java/com/codebot/teams/SpawnDispatcher.java`（`SpawnConfig` record @ 15-24；`SpawnResult` record @ 26-29；`spawnTeammate` @ 33-61；`recordExternalMember` @ 80-88）
- 依赖任务: T1, T3, T5
- 完成标准: `spawnTeammate` switch `team.getMode()` 分发；`IN_PROCESS` 路径调 `team.addMember` 注册（可选 `setWorkDir(workdir)`） → 置 `active=true` → `Thread.startVirtualThread` 跑 `runInProcessTeammate` → 返 `SpawnResult(IN_PROCESS, null)`；`TMUX` 路径先把 task 写入对方 mailbox（用 `team.sendMessage(LEAD_NAME, memberName, task)`） → `buildTeammateCLI` 拼命令 → `TmuxBackend.spawnTmuxTeammate` 拿 paneId → `recordExternalMember` 注册占位 member → 返 `SpawnResult(TMUX, paneId)`；未知 mode 抛 `IllegalStateException`。
- [ ] 完成

## T8: 实现 BuildTeammateCLI + shellQuote
- 影响文件: `src/main/java/com/codebot/teams/SpawnDispatcher.java`（`buildTeammateCLI` @ 67-73；`shellQuote` @ 75-78）
- 依赖任务: T7
- 完成标准: `buildTeammateCLI` 用 `ProcessHandle.current().info().command().orElse("codebot")` 拿当前可执行；workdir 空时默认 `System.getProperty("user.dir")`；返回 `cd <quoted_wd> && <quoted_exe> --teammate --team-name <quoted_team> --agent-name <quoted_member>`。`shellQuote` 简单字符（`[a-zA-Z0-9_./-]+` 正则命中）直接返回原串，含特殊字符时单引号包裹并把内嵌 `'` 替换为 `'\''`。
- [ ] 完成

## T9: 实现 Coordinator Mode 工具白名单
- 影响文件: `src/main/java/com/codebot/teams/Coordinator.java`（`ALLOWED_TOOLS` @ 19-32；`isCoordinatorTool` @ 34-36）
- 依赖任务: 无
- 完成标准: 12 项白名单 `Set<String>`：`Agent / SendMessage / TaskCreate / TaskGet / TaskList / TaskUpdate / TeamCreate / TeamDelete / ReadFile / Glob / Grep / Bash`；`isCoordinatorTool(name)` 返回 set.contains 布尔（写工具 `WriteFile / EditFile` 等不在内）。
- [ ] 完成

## T10: 实现 SendMessage / TeamCreate / TeamDelete 三个工具
- 影响文件: `src/main/java/com/codebot/teams/TeamTools.java`（`SendMessageTool` @ 20-72；`TeamCreateTool` @ 76-128；`TeamDeleteTool` @ 132-181）
- 依赖任务: T1
- 完成标准:
 - `SendMessageTool.execute`：`to/content` 必填；遍历所有团队找 `to` 这个 member 所在团队调 `team.sendMessage(senderName, to, content)` 投递；未匹配返 `recipient '<to>' not found in any team` 错误；schema 含 `to / content` 两个 string 必填字段。
 - `TeamCreateTool.execute`：`team_name` 必填；同名时追加 `-2/-3/...` 后缀去重；调 `TeamManager.detectBackend()` + `teamMgr.createTeam`；Output 含 `"Team \"X\" created (mode: Y). Use Agent tool with team_name=\"X\" to add teammates."`。
 - `TeamDeleteTool.execute`：`team_name` 必填；不存在返错误；调 `teamMgr.deleteTeam`（内部 `stopAll` 中断所有 member）；返回 `"Team \"X\" deleted. Stopped N member(s): a, b, c"` 清单。
- [ ] 完成

## T11: 实现 AgentNameRegistry 单例
- 影响文件: `src/main/java/com/codebot/teams/AgentNameRegistry.java`（`INSTANCE` @ 12；`nameToId` map @ 13；`getInstance` @ 17；`register / resolve / unregister / listAll` @ 19-35）
- 依赖任务: 无
- 完成标准: 单例模式（`private static final INSTANCE`，私有构造）；`nameToId` 用 `LinkedHashMap` 保证遍历顺序；`register / resolve / unregister / listAll` 全部 `synchronized`；`resolve` 先查 name → id，未命中时检查 `containsValue(input)` 返回 input 本身（反向 id 寻址），都不命中返 null；`listAll` 返新建 `LinkedHashMap` 副本避免外部修改。
- [ ] 完成

## T12: 实现 SharedTaskStore
- 影响文件: `src/main/java/com/codebot/teams/SharedTaskStore.java`（`SharedTask` record @ 21-32；常量 `MAPPER` + 字段 `filePath / nextId / tasks` @ 34-37；构造器 @ 39-42；`create` @ 44-50；`get` @ 52-54；`listTasks` @ 56-61；`update` @ 63-85；`load` @ 87-95；`save` @ 97-102）
- 依赖任务: 无
- 完成标准: `SharedTask` record 含 `id / title / description / status / assignee / blocks / blockedBy / createdBy` 字段，并提供 `withStatus / withAssignee` 不可变更新；`@JsonIgnoreProperties(ignoreUnknown=true)` 注解保证向前兼容；构造器 `new SharedTaskStore(teamDir)` 自动 load 已有 `tasks.json`；`create` 用 `AtomicInteger` 自增 id；`listTasks` 支持按 status/assignee 过滤；`update` 用记录类 wither 模式产新对象，`addBlocks/addBlockedBy` 是追加（用新建 ArrayList 拷贝旧值后 addAll）；全部 mutating 方法 `synchronized`；save 用 `MAPPER.writerWithDefaultPrettyPrinter()` 美化输出。
- [ ] 完成

## T13: 实现 FileMailBox 单元测试
- 影响文件: `src/test/java/com/codebot/teams/FileMailBoxTest.java`（`sendCreatesFileWithMessage` @ 17-29；`readUnreadReturnsOnlyUnread` @ 31-41；`markAllReadMakesUnreadEmpty` @ 43-53；`nonexistentAgentReturnsEmpty` @ 55-60；`teamSendMessageIntegration` @ 62-74）
- 依赖任务: T1, T2
- 完成标准: 用 `@TempDir` 把 inbox 重定向到测试临时目录，避免污染仓库根；5 个用例覆盖——1) `send` 落盘后文件含 `from / text / read=false` 三字段；2) 连续 `send` 后 `readUnread` 返所有未读；3) `markAllRead` 后 `readUnread` 为空；4) 不存在的 agentId 返 `readUnread` 空列表；5) 集成测试创建 `Team` + 单独 mailbox 验证 send/read 完整流程。
- [ ] 完成

## T14: 实现 AgentTool team_name 分支
- 影响文件: `src/main/java/com/codebot/agents/AgentTool.java`（新增 `teamMgr` 字段；`execute` 解析 `team_name` 参数；当 `team_name != null && teamMgr != null` 走 team 分支调 `SpawnDispatcher.spawnTeammate`；当 in-process 模式启虚拟线程消费 `eventOut` queue 转发到 `progressCh`）
- 依赖任务: T6, T7
- 完成标准: `AgentTool` 新增 `private TeamManager teamMgr` 字段及 setter；`execute` 在解析完 `subagent_type / prompt` 后检查 `team_name`，命中且 `teamMgr != null` 即走 team 分支；team 分支校验团队存在 + 同 team 同名 + 解析子工具池 + 可选 worktree + `TeammateRunner.buildTeammateAddendum` 构造 addendum + `SpawnDispatcher.spawnTeammate` 拿 result；in-process 模式启虚拟线程 `drainTeammateEvents` 消费事件流转 `SubAgentProgress` 喷进 `progressCh`；Output 含 backend hint 和 SendMessage 使用提示。
- [ ] 完成

## T15: TUI 接入
- 影响文件: `src/main/java/com/codebot/tui/CodeBotModel.java`（`teamMgr` 字段；`registerAgentTools` 内创建 `TeamManager` 并注册三件套工具 + 注入 `AgentTool.teamMgr`；Lead 每轮迭代调 `TeammateRunner.drainLeadMailbox(teamMgr)` 拼到下一轮 system reminder；Lead Agent 工具调用前用 `Coordinator.isCoordinatorTool` 过滤）
- 依赖任务: T6, T9, T10, T14
- 完成标准:
 1. `CodeBotModel.teamMgr` 字段声明；
 2. `registerAgentTools`（或等价初始化方法）创建 `TeamManager` → 注册 `TeamCreateTool / TeamDeleteTool / SendMessageTool` → `AgentTool.setTeamMgr(teamMgr)`；
 3. Lead 每轮迭代开头调 `TeammateRunner.drainLeadMailbox(teamMgr)` 把 `<team-notification>` 字符串拼到要喂给模型的 system reminder；
 4. Lead 工具调用过滤：`teamMgr.listTeams().isEmpty()` 为空时放行全部，非空时 `Coordinator.isCoordinatorTool(name)` 判定；
 5. 程序退出 finally 块调 `teamMgr.closeAll()` 确保所有虚拟线程被中断。
- [ ] 完成

## T16: 端到端验证
- 影响文件: 无（仅运行验证）
- 依赖任务: T1-T15
- 完成标准:
 - `./gradlew build` 通过；
 - `./gradlew test` 通过（覆盖至少 `FileMailBoxTest` 5 个用例 + `TeamManagerTest` / `SpawnDispatcherTest` / `TeammateRunnerTest` / `CoordinatorTest` 共 15+ 用例，含 detectBackend 两档优先级、SendMessage 路由、SpawnDispatcher 校验、shellQuote、drainLeadMailbox、isShutdownRequest、createIdleNotification 等）；
 - 主流程接线验证：`rg "teamMgr|TeammateRunner|Coordinator\." src/main/java/com/codebot/tui` 命中 TUI 装配点；`rg "TeamMgr|teamMgr" src/main/java/com/codebot/agents/AgentTool.java` 看到 team 分支被 execute 调用。
- [ ] 完成

## 进度
- [ ] T1 / [ ] T2 / [ ] T3 / [ ] T4 / [ ] T5 / [ ] T6 / [ ] T7 / [ ] T8 / [ ] T9 / [ ] T10 / [ ] T11 / [ ] T12 / [ ] T13 / [ ] T14 / [ ] T15 / [ ] T16

```

```markdown
# ch15: AgentTeam Checklist

> 所有条目可勾选、可观测。验收方式写在条目后面括号中。验收：已通过验证的项均勾选。

## 1. 实现完整性

- [ ] 枚举 `TeamManager.TeamMode` 在 `src/main/java/com/codebot/teams/TeamManager.java:21` 存在，含 `IN_PROCESS / TMUX` 两档
- [ ] 内部类 `TeamManager.Team` 在 `TeamManager.java:70-134` 存在，字段 `name / mode / members / mailBox` 齐全，写方法全部 `synchronized`
- [ ] 内部类 `TeamManager.Member` 在 `TeamManager.java:136-151` 存在，字段 `name / agent / conv / active / thread`（后两者 `volatile`）齐全
- [ ] 静态方法 `TeamManager.detectBackend` 在 `TeamManager.java:53-62` 实现优先级 `TMUX env → which tmux → IN_PROCESS`
- [ ] Record 类 `FileMailBox.MailMessage` 在 `FileMailBox.java:16-21` 含 6 字段 `from / text / timestamp / read / color / summary`，便利构造器自动填 timestamp/read=false
- [ ] `FileMailBox.withLock` 在 `FileMailBox.java:76-112` 使用 `Files.createFile` 抛 `FileAlreadyExistsException` 时重试，10 次 5-100ms 随机退避，>10s 过期清理
- [ ] Record 类 `SpawnDispatcher.SpawnConfig / SpawnResult` 在 `SpawnDispatcher.java:15-29` 存在
- [ ] 常量 `TeammateRunner.LEAD_NAME = "lead"` / `SHUTDOWN_PREFIX = "[shutdown]"` / `IDLE_POLL_MS = 500L` 在 `TeammateRunner.java:16-18`
- [ ] `Coordinator.ALLOWED_TOOLS` `Set<String>` 在 `Coordinator.java:19-32` 含 12 项白名单（写工具 `WriteFile / EditFile` 等被排除）
- [ ] `TeammateRunner.runInProcessTeammate` 在 `TeammateRunner.java:26-66` 主循环七步齐全：addendum 注入 → injectPendingMessages → addUserMessage → agent.run + drainAgentEvents → idle 通知 → while 循环 waitForNextPromptOrShutdown
- [ ] `SpawnDispatcher.buildTeammateCLI` 在 `SpawnDispatcher.java:67-73` 输出 `cd <quoted_wd> && <quoted_exe> --teammate --team-name <quoted> --agent-name <quoted>`；`shellQuote` 在 `:75-78` 简单字符直接返回、特殊字符单引号 POSIX 转义
- [ ] `TeammateRunner.buildTeammateAddendum` 在 `TeammateRunner.java:97-109` 文本包含 "member of team"、"Your name is"、"SendMessage tool"、"idle notification will be sent to the lead automatically" 四个关键信息
- [ ] `TeammateRunner.drainLeadMailbox` 在 `TeammateRunner.java:72-92` null 安全（`teamMgr == null` 返 `List.of()`）、读完后调 `markAllRead`、输出格式 `<team-notification team="X">\n...\n</team-notification>`
- [ ] `TeamTools.SendMessageTool.execute` 在 `TeamTools.java:54-71` 遍历所有团队找 `to` member 投递，未匹配返 `recipient '<to>' not found in any team` 错误
- [ ] `TeamTools.TeamCreateTool.execute` 在 `TeamTools.java:108-127` 同名冲突自动追加 `-2/-3/...` 后缀去重
- [ ] `TeamTools.TeamDeleteTool.execute` 在 `TeamTools.java:163-180` 返回 `"Team \"X\" deleted. Stopped N member(s): a, b, c"` 清单
- [ ] `AgentNameRegistry` 在 `AgentNameRegistry.java:10-36` 是单例（`getInstance`），全部方法 `synchronized`；`resolve` 支持反向 id 寻址
- [ ] `SharedTaskStore` 在 `SharedTaskStore.java:18-103` 实现 `create / get / listTasks / update`，全部 `synchronized`；`update` 用 wither 模式产新 record，`addBlocks/addBlockedBy` 追加而非替换

## 2. 接入完整性（必查，杜绝死代码）

- [ ] `rg "new TeamManager\\(\\)" src/main/java/com/codebot/tui` 在 TUI 装配代码找到 `TeamManager` 实例化点
- [ ] `rg "TeamCreateTool|TeamDeleteTool|SendMessageTool" src/main/java/com/codebot/tui` 在 TUI 找到三个工具注册点
- [ ] AgentTool 注入 `teamMgr` 的代码在 TUI 装配处可见（`agentTool.setTeamMgr(teamMgr)` 或构造器注入）
- [ ] `rg "drainLeadMailbox" src/main/java/com/codebot/tui` 命中 Lead 每轮迭代调用点（把 `<team-notification>` 注入下一轮 system reminder）
- [ ] `rg "Coordinator.isCoordinatorTool" src/main/java/com/codebot/tui` 命中 Lead 工具调用过滤点
- [ ] `CodeBotModel.teamMgr` 字段在 TUI 主模型类中声明
- [ ] `rg "teamMgr" src/main/java/com/codebot/agents/AgentTool.java` 看到 `AgentTool` 的 `team_name` 分支调用 `SpawnDispatcher.spawnTeammate`
- [ ] `rg "SpawnDispatcher.spawnTeammate" src/main/java/com/codebot/agents` 命中 in-process 模式下虚拟线程消费 eventOut 的 `drainTeammateEvents` 调用
- [ ] 程序退出 finally 块调 `teamMgr.closeAll()` 确保所有虚拟线程被中断

## 3. 编译与测试

- [ ] `./gradlew build` 通过
- [ ] `./gradlew test` 通过（覆盖至少 15 个用例：FileMailBoxTest 5 个 + TeamManagerCRUD / DetectBackendFallback / DetectBackendPrefersTmuxWhenInside / SendMessageToolRoutes / TeamCreateNameCollision / TeamDeleteStopsMembers / IsShutdownRequest / CreateIdleNotification / DrainLeadMailbox / DrainLeadMailboxNullSafe / ShellQuote / BuildTeammateCLIFormat / SpawnDispatcherInProcess / SpawnDispatcherTmuxValidation / CoordinatorAllowedTools / SharedTaskStoreCRUD / AgentNameRegistryRoundtrip）
- [ ] `./gradlew check` 无警告（含 SpotBugs / Checkstyle 若启用）
- [ ] 测试运行不在仓库根残留 `.codebot/teams/` 目录（`@TempDir` 重定向到 tmp）
- [ ] FileMailBox 并发测试用 `ExecutorService` + `CountDownLatch` 验证文件锁正确性，多线程并发 `send` 后 `readUnread` 数量与发送次数一致

## 4. 端到端验证

- [ ] 注册路径：TUI 启动后装配代码创建 `TeamManager` 并把 `TeamCreate / TeamDelete / SendMessage` 三件套放入 registry；用户向 Lead 说 "create a team to refactor X" → LLM 调 `TeamCreate(team_name="refactor-X")` → `detectBackend()` 选模式 → Output 返回 `"Team \"refactor-X\" created (mode: ...). Use Agent tool with team_name=\"refactor-X\" to add teammates."`
- [ ] Spawn 路径：Lead 继续说 "spawn alice to do data layer" → LLM 调 `Agent(team_name="refactor-X", name="alice", prompt="...")` → `AgentTool.execute` 识别 `team_name` 分支调 `SpawnDispatcher.spawnTeammate(IN_PROCESS|TMUX)` → 队员开始干活
- [ ] 通信路径：队员 alice 通过 `SendMessage(to="bob", content="...")` 给 bob 写 mailbox → bob 下一轮 idle poll 拿到消息作为 user message 注入对话
- [ ] Lead 感知路径：每个队员 turn 结束写 `[idle] alice: completed initial task (at <iso>)` 通知到 Lead 邮箱 → Lead 下一轮迭代调 `drainLeadMailbox` 抽出 `<team-notification team="refactor-X">\nfrom=alice: [idle] ...\n</team-notification>` 注入 Lead 上下文
- [ ] Coordinator Mode 路径：团队存活期间 Lead 每轮工具调用前 `Coordinator.isCoordinatorTool` 过滤，调用 `WriteFile` / `EditFile` 会被拒绝；`TeamDelete` 清空所有团队后下一轮恢复全工具集
- [ ] Tmux 后端：`TMUX` env 非空时 `detectBackend` 返 `TMUX` → spawn 时先把 task 写 mailbox → `tmux new-window -d` 拉起新窗口跑 `codebot --teammate ...` → 子进程加载同一 mailbox 目录 → 第一次 idle poll 拿到初始任务开始干活
- [ ] iTerm 后端（备用）：`ITermBackend` 类已实现 `spawnITermTeammate / stopITermTeammate`，可通过手工调用验证 AppleScript 解析正确（`SpawnDispatcher` 当前未接此分支，作为后续扩展点）
- [ ] 关闭路径：`TeamDelete(team_name="refactor-X")` → `teamMgr.deleteTeam` → `team.stopAll` 遍历 member 调 `thread.interrupt()`（in-process）或 `TmuxBackend.stopTmuxTeammate`（tmux）→ 全部清理后 Lead 下轮恢复全工具集
- [ ] JVM 退出路径：`teamMgr.closeAll()` 在 TUI 程序 finally 块调用，所有虚拟线程被中断、所有 tmux 窗口被关闭

## 5. 文档

- [ ] `docs/java/ch15/spec.md` 已写
- [ ] `docs/java/ch15/tasks.md` 已写，16 个 T 全部勾完
- [ ] `docs/java/ch15/checklist.md` 已写并逐项验收
- [ ] commit 信息标注 `ch15` 与三件套关闭状态（待用户确认后由人或 CI 触发）

```

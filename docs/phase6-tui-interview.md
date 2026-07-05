# 阶段6：TUI 交互层与面试专题

> **学习目标**：理解终端 UI 的架构、Worktree 隔离机制、项目架构精华总结、以及面试准备。
> **预计时间**：3-4 天
> **前置要求**：完成阶段1-5

---

## 目录

1. [TUI 交互层：如何用 CSS 写终端应用](#1-tui-交互层如何用-css-写终端应用)
2. [Worktree：在隔离环境中安全操作](#2-worktree在隔离环境中安全操作)
3. [项目架构精华：一张图装下全部知识](#3-项目架构精华一张图装下全部知识)
4. [面试准备：系统设计题](#4-面试准备系统设计题)
5. [面试准备：源码阅读题](#5-面试准备源码阅读题)
6. [面试准备：行为问题](#6-面试准备行为问题)
7. [速查手册：15 个核心类型 + 6 条核心流程](#7-速查手册)

---

## 1. TUI 交互层：如何用 CSS 写终端应用

### 1.1 Textual 是什么？

Textual 让你用**写 Web 应用的方式**写终端应用：

- **组件**：Button、TextArea、Markdown、OptionList... 像 HTML 标签一样
- **CSS**：用 TCSS 文件写样式（颜色、边框、布局），语法几乎和 CSS 一样
- **消息系统**：组件间通过消息通信（类似 DOM 事件）
- **响应式布局**：Vertical、Horizontal 等容器自动排列子组件

### 1.2 CodeBot 的 UI 结构

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

### 1.3 输入框的贴心设计

- **@文件引用**：输入 `@src/main.py` 会自动读取文件内容并嵌入消息，LLM 直接看到代码
- **/命令补全**：输入 `/com` 按 Tab → 自动补全为 `/compact`
- **历史回溯**：按 ↑ 翻之前的输入
- **Shift+Enter** 换行，**Enter** 发送（符合聊天习惯）
- **Shift+Tab** 切换权限模式（default → acceptEdits → plan → bypass 循环）

### 1.4 流式渲染：打字机效果

Agent 不是一次性返回全部内容。每生成一个字，TUI 就在消息气泡末尾追加一个字——就像 AI 在跟你打字聊天。工具调用也实时显示："正在读 main.py..." → 完成后显示 ✅ 和耗时。

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

### 2.3 Worktree 怎么创建的？

1. `git worktree add` 创建新工作目录
2. 复制本地配置文件（.env 等）
3. 设置 Git hooks
4. 软链接大目录（.venv、node_modules），避免重复安装依赖

### 2.4 自动清理

有一个后台任务每小时检查所有 worktree。超过 24 小时没用的自动删除——防止积累一堆废弃工作树。

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
  ReadFile              安全白名单
  EditFile              危险黑名单
  Bash                  路径沙箱
  Grep/Glob             规则引擎
  Agent/Team            模式矩阵
  LoadSkill             人工确认
    ↓                   ↑
记忆层（Memory）: 长期记忆注入 + 上下文压缩 + 会话持久化
```

### 3.2 关键数字

| 指标 | 值 |
|------|-----|
| 总代码量 | ~15,000 行 Python |
| 核心文件 | agent.py (1300行), app.py (1900行), client.py (800行) |
| 内置工具 | 20+ |
| 内置命令 | 14 个斜杠命令 |
| 内置 Skill | 4 个（commit, review, test, backend-interview） |
| 内置子 Agent | 4 个（explore, plan, verification, general-purpose） |
| 安全层数 | 5 层（+ Hook 可追加自定义层） |
| 支持协议 | 3 种（Anthropic Messages / OpenAI Responses / OpenAI Chat Completions） |
| 测试文件 | 17 个 |

### 3.3 七个关键技术决策

| 决策 | 理由 |
|------|------|
| 分层而非扁平 | 每层可独立开发测试，修改不影响其他层 |
| 策略模式做多协议 | 新增模型只需加一个客户端类 |
| Pydantic 做参数校验 | 一套代码同时生成 Schema（给 LLM）+ 校验参数（给自己） |
| 注册表模式做扩展 | 工具/Skill/Agent/命令都可以运行时注册和发现 |
| Future + await 做权限确认 | Agent 代码自然流动，不阻塞 TUI |
| 文件系统做团队通信 | 本地 Agent 无需网络开销，消息不丢失 |
| 双层压缩做长对话 | Layer1 裁剪结果 + Layer2 摘要历史 |

---

## 4. 面试准备：系统设计题

### 4.1 "设计一个 AI Coding Agent"

**回答框架**：

**第一层：需求**
- 终端运行，接受自然语言任务
- 自主推理 + 执行工具 + 观察结果 + 循环迭代
- 安全第一（不能 rm -rf /）
- 多模型支持，不被单一厂商锁定
- 处理长对话（context window 只有 20 万 token）

**第二层：架构（画五层图）**
- 交互层：TUI（Textual — CSS 样式终端框架）
- 引擎层：ReAct 循环（Think → Act → Observe → Loop）+ Plan 模式（高风险任务先规划）
- 工具层：统一 Tool 接口 + 注册表 + 延迟加载
- 记忆层：自动记忆提取 + 上下文压缩 + 会话持久化
- 安全层：五道防线（白名单 → 黑名单 → 沙箱 → 规则 → 模式） + 人工确认

**第三层：核心流程**
```
用户输入 → 注入环境上下文和长期记忆 → ReAct 循环
  → LLM 思考和输出 → 安全检查和工具执行 → 结果返回 → 循环
  → 长对话自动压缩 → 任务完成
```

**第四层：关键挑战**
- Context Window 管理 → 双层压缩（tool result budget + 摘要）
- 工具安全性 → 纵深防御
- 多协议适配 → 策略模式
- 扩展性 → 五种正交扩展机制

### 4.2 "怎么处理 LLM 的幻觉问题？"

- **强制执行工具**：System prompt 强调 "不要假设代码内容，先读文件"
- **唯一性约束**：EditFile 的 old_string 必须唯一匹配
- **文件状态检查**：编辑前验证文件未被外部修改
- **幻觉保护**：连续 3 次调用不存在的工具 → 强制终止
- **人工确认**：高风险操作需要用户审核

### 4.3 "怎么保证系统安全？"

**纵深防御**：五层各有职责，任一层说 "不" 就停止。
1. 安全命令白名单 + 危险命令黑名单
2. 路径沙箱（只能在项目目录和临时目录操作）
3. 用户自定义规则
4. 权限模式控制（日常/信任编辑/纯规划/完全信任）
5. 人工确认（HITL）

---

## 5. 面试准备：源码阅读题

### 5.1 "ReAct 循环是怎么实现的？"

Agent.run() 是一个异步生成器，主循环是 `while True`：
- 循环头：注入提醒 → 自动压缩检查 → 构建 system prompt → 裁剪工具结果
- LLM 调用：序列化消息 → 流式调用 LLM → StreamCollector 收集事件 → 逐个 yield 给 TUI
- 响应处理：max_tokens 恢复 → 无工具调用则结束 → 有则执行
- 工具执行：分区 → 安全检查 → 可能等待用户确认 → 执行 → 结果加入历史
- 循环尾：turn_end → 回到头

四种终止条件：无工具调用 / 超 50 轮 / 连续 3 次幻觉 / 退出 Plan 模式

### 5.2 "三种 LLM 客户端怎么统一？"

定义 LLMClient 抽象基类，只有一个 stream() 方法。三个实现类各自负责：
- 把内部 Message 格式转成自己 API 的请求格式
- 把自己 API 的流式事件映射为内部统一的 7 种 StreamEvent
- 把原生异常映射为内部统一的 4 种异常

Agent 只调 stream()，完全不知道底层是谁。

### 5.3 "长对话怎么压缩？"

触发条件：token 数 ≥ context_window - 1.3 万
步骤：分割（prefix 待压缩 + keep 保留原文约 1 万 token）→ LLM 生成结构化摘要 → 附件恢复（文件快照 + 激活技能）→ 替换历史
保护的侧写：断路器防频繁压缩

---

## 6. 面试准备：行为问题

### 6.1 "项目中最大的技术挑战？"

Context Window 管理。对话越来越长，20 万 token 很快就满。解决方案是两层压缩，但引入了新问题：压缩后 LLM 忘记刚读的文件 → 设计了 RecoveryState。压缩太频繁影响效率 → 设计了断路器。压缩质量影响后续推理 → 设计了 9 段结构化摘要 Prompt。

### 6.2 "怎么保证代码质量？"

- 全项目类型标注（PEP 604 X | Y 语法）
- Pydantic 自动参数校验
- 17 个测试文件覆盖核心模块
- LLM 异常全映射为内部异常（AuthenticationError/RateLimitError/NetworkError）
- Hook 自动跑 linter

### 6.3 "从项目中学到了什么？"

1. 分层架构让每层独立开发和测试
2. 注册表模式让扩展变得极其简单
3. 异步编程在 Agent 场景不可或缺（流式 LLM + 并发工具 + 异步权限确认）
4. 安全是 Agent 系统的第一优先级，不能事后打补丁
5. 多协议适配的关键是"自己定义接口，让三方适配"

---

## 7. 速查手册

### 7.1 15 个核心类型

| 类型 | 文件 | 一句话 |
|------|------|--------|
| Agent | agent.py | 核心循环 |
| AgentEvent | agent.py | 10 种事件的联合类型 |
| LLMClient | client.py | LLM 的抽象接口 |
| StreamEvent | tools/base.py | 7 种流式事件的联合类型 |
| Tool | tools/base.py | 工具模板 |
| ToolRegistry | tools/__init__.py | 工具注册表 |
| ConversationManager | conversation.py | 消息历史管理 |
| Message | conversation.py | 单条消息 |
| PermissionChecker | permissions/checker.py | 五层安全检查 |
| PermissionMode | permissions/modes.py | 四种权限模式 |
| SkillLoader | skills/loader.py | 技能加载与热重载 |
| HookEngine | hooks/engine.py | 生命周期钩子 |
| MCPManager | mcp/manager.py | MCP 连接管理 |
| TeamManager | teams/manager.py | 团队管理 |
| WorktreeManager | worktree/manager.py | 工作树隔离 |

### 7.2 6 条核心流程

| 流程 | 入口 | 关键步骤 |
|------|------|---------|
| 用户请求 | app._on_chat_submitted() | @展开 → /命令 → Agent.run() |
| Agent 循环 | Agent.run() | 注入 → LLM 调用 → 工具执行 → 循环 |
| LLM 调用 | client.stream() | 序列化 → API 调用 → 事件映射 |
| 工具执行 | Agent._execute_tool() | 安全检查 → 确认 → 执行 → Hook |
| 上下文压缩 | auto_compact() | 判断 → 分割 → 摘要 → 附件 → 替换 |
| 记忆提取 | MemoryManager.extract() | 格式化 → LLM 分析 → 写入 |

### 7.3 学习完成检查清单

完成全部 6 个阶段后，你应该能够：

- [ ] 画出五层架构图并解释每层职责
- [ ] 讲清楚 ReAct 循环的四个阶段
- [ ] 说明多协议适配的设计思路
- [ ] 描述一个 Tool 的完整创建流程
- [ ] 说出五层安全检查的顺序和职责
- [ ] 解释双层压缩的触发条件和执行步骤
- [ ] 说明 Token 混合估算的原理
- [ ] 区分三种子 Agent 模式的适用场景
- [ ] 解释 Skill 的加载和激活流程
- [ ] 说明 MCP 协议在项目中的角色
- [ ] 回答"设计一个 AI Coding Agent"系统设计题
- [ ] 讲出项目中最复杂的技术挑战和解决思路

---

> 🎉 **恭喜！** 六个阶段全部完成。下一步：打开源码配合文档一起阅读，每读完一个模块尝试写一段总结。
> **面试时记住：先讲架构，再讲细节，始终和实际场景关联。**

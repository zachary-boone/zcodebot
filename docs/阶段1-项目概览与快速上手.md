# 阶段1：项目概览与快速上手

> **学习目标**：理解 CodeBot 是什么、为什么这样设计、五层架构如何协作、配置系统如何工作、完整的启动流程，并能向面试官清晰介绍项目定位与技术选型。
> **预计时间**：2-3 天（每天 2-3 小时）
> **前置要求**：Python 基础（async/await、dataclass、类型注解）、了解命令行、了解 LLM 基本概念（Token、Context Window、Function Calling）
> **面试导向**：读完本章你能回答"介绍一下你的项目""为什么选这个技术栈""项目架构怎么设计的"这三个开场必问题。

---

## 目录

1. [项目定位与设计哲学](#1-项目定位与设计哲学)
2. [市场对标：CodeBot 在 AI Coding 工具谱系中的位置](#2-市场对标codebot-在-ai-coding-工具谱系中的位置)
3. [五层分层架构](#3-五层分层架构)
4. [配置系统](#4-配置系统)
5. [启动流程详解](#5-启动流程详解)
6. [项目目录结构](#6-项目目录结构)
7. [技术栈全景图与选型理由](#7-技术栈全景图与选型理由)
8. [关键设计模式](#8-关键设计模式)
9. [本章自测题](#9-本章自测题)
10. [面试高频题与回答模板](#10-面试高频题与回答模板)

---

## 1. 项目定位与设计哲学

### 1.1 CodeBot 是什么？

CodeBot 是一个**终端 AI 编程助手**——你可以把它理解成"住在终端里的 AI 程序员"。你告诉它"帮我修复这个 bug"或者"给这个模块写单元测试",它会自主完成：

- 读文件理解代码结构
- 搜索相关代码片段（Grep/Glob）
- 精确编辑文件（search/replace）
- 运行命令验证（跑测试、编译）
- 在循环中迭代直到任务完成

**关键区别：自主 Agent vs 补全工具**

| 类型 | 代表 | 工作方式 | 自主性 |
|------|------|---------|--------|
| **补全工具** | GitHub Copilot | 你写一行，它补一行 | ❌ 完全被动 |
| **对话助手** | ChatGPT | 你问它答，不碰你的代码 | ⚠️ 只建议 |
| **自主 Agent** | **CodeBot**、Claude Code | 自己决定做什么、调什么工具、何时完成 | ✅ 完全自主 |

CodeBot 属于第三类——它自己决定要做什么、调用什么工具、什么时候算完成。这是 LLM 应用的"最高形态"：从"被使用的工具"变成"使用工具的 Agent"。

### 1.2 四大设计原则

**原则一：两种工作模式，应对不同风险**

- **日常模式（ReAct）**：直接开工，边想边做。说"修复 bug"就直接读文件、改代码、跑测试。适合低风险、明确的任务。
- **Plan 模式**：高风险任务先规划。Agent 先探索代码、写出详细计划保存为 Markdown 文件，你审核通过后才开始动手。在 Plan 模式中，Agent **只能**：读代码、派发子 Agent 探索、写计划文件、退出 Plan 模式——其他写操作全被拦截。

**为什么需要两种模式？** 这是工程实践中的"信任校准"问题。AI Agent 越强大，越容易"自信地犯错"——你让它重构核心模块，它可能直接删了重写。Plan 模式就是给用户一个"刹车踏板"：先看方案再放行。

**原则二：分层架构，职责清晰**

交互层 → 引擎层 → 工具层 → 记忆层 → 安全层。每层只管自己的事，通过明确接口通信。这就像工厂流水线，每个工位只做一件事，换个工位不影响其他工位。

**原则三：厂商中立，不绑定模型**

同时支持 Anthropic（Claude）、OpenAI（GPT）、DeepSeek、Qwen、Ollama 以及任何兼容 OpenAI API 的服务。Agent 核心代码完全不知道底层用的是哪个模型——通过统一接口屏蔽差异。

**为什么不绑定单一厂商？** 这是工程上的"反脆弱"设计：模型厂商会涨价、会限流、会下线（如 OpenAI 曾下线 codex）。多协议支持让你随时切换，不被单一供应商绑架。

**原则四：安全优先**

默认情况下，读文件自动放行，写文件和执行命令需要你确认。安全层有 **六层纵深防御**（Plan 模式例外 → 安全白名单 → 危险黑名单 → 路径沙箱 → 规则引擎 → 模式矩阵 → 人工确认），任一层拦截就能阻止危险操作。详见阶段3。

**为什么安全是第一优先级？** AI Agent 有"行动力"——它能删文件、能跑命令、能改代码。一个没有安全设计的 Agent 就像一辆没有刹车的跑车，再快也是灾难。Anthropic 的 Claude Code、OpenAI 的 Codex CLI 都把安全作为核心设计点。

### 1.3 设计哲学的演进背景

CodeBot 的设计参考了 AI Agent 领域的几个里程碑：

- **2022 年 ReAct 论文**（Yao et al.）：提出 Reasoning + Acting 交替的范式，让 LLM 边推理边调用工具。CodeBot 的默认模式就是 ReAct。
- **2023 年 AutoGPT/BabyAGI**：展示了"自主 Agent"的可行性，但也暴露了"无控制循环"的危险——AI 会无限执行下去。CodeBot 的 `max_iterations=50` 就是借鉴这个教训。
- **2024 年 Claude Code/Codex CLI**：证明了"终端 + Agent + 安全确认"的产品形态。CodeBot 的五层架构、权限模式都参考了它们。
- **2024 年 MCP 协议**：Anthropic 提出的开放标准，让 Agent 能接入任何外部工具。CodeBot 是 MCP 客户端。

---

## 2. 市场对标：CodeBot 在 AI Coding 工具谱系中的位置

### 2.1 AI Coding 工具谱系图

```
                    自主性 →
    ┌──────────────────────────────────────────────┐
    │  补全     │  对话    │  半自主    │  全自主   │
    ├──────────────────────────────────────────────┤
IDE │ Copilot  │ Cursor  │ Cursor   │  CodeBot  │
    │ Tab      │ Chat    │ Agent    │  Claude   │
    │          │         │          │  Code     │
    └──────────────────────────────────────────────┘
CLI │          │         │ Aider    │  CodeBot  │
    │          │         │          │  Codex    │
    │          │         │          │  CLI      │
    └──────────────────────────────────────────────┘
```

### 2.2 与主流工具对比

| 工具 | 形态 | 自主性 | 安全机制 | 多模型 | 开源 | 扩展机制 |
|------|------|--------|---------|--------|------|---------|
| **GitHub Copilot** | IDE 插件 | 补全 | 无 | ❌ | ❌ | ❌ |
| **Cursor** | IDE | 半自主 | 询问 | 部分 | ❌ | MCP |
| **Claude Code** | CLI | 全自主 | 权限模式 | ❌ | ❌ | MCP/Skill |
| **Codex CLI** | CLI | 全自主 | 沙箱 | ❌ | ✅ | ❌ |
| **Aider** | CLI | 半自主 | git 兜底 | ✅ | ✅ | ❌ |
| **CodeBot** | CLI | 全自主 | **五层纵深防御** | ✅ | ✅ | **五大正交扩展** |

### 2.3 CodeBot 的差异化竞争力

1. **五层纵深安全防御**——比 Claude Code 的"模式矩阵"更细粒度，比 Codex CLI 的"沙箱"更分层
2. **五大正交扩展机制**（Tool/Skill/MCP/Hook/SubAgent）——这是同类项目中扩展性最强的设计
3. **三协议统一适配**——不依赖 litellm 等中间层，直接集成官方 SDK，享受最新特性
4. **跨会话自动记忆**——同类工具大多只有单会话记忆，CodeBot 能跨对话记住用户偏好
5. **Plan Mode + ReAct 双模式**——兼顾"快速执行"和"安全规划"

---

## 3. 五层分层架构

### 3.1 架构全景图

把 CodeBot 想象成五层楼的建筑，从上到下每层各司其职：

```
┌──────────────────────────────────────────────────┐
│  1F 交互层 (TUI)   你看到和操作的终端界面            │
│  负责：显示消息、接收输入、弹出确认框                 │
│  比如：你输入"修复bug" → 这一层把文字送下去            │
│  关键文件：app.py (1900行)、styles.tcss              │
├──────────────────────────────────────────────────┤
│  2F 引擎层 (Agent)  大脑——决定做什么、怎么做          │
│  负责：循环思考→行动→观察，直到任务完成                │
│  比如：收到"修复bug"后，决定"先读文件理解代码"          │
│  关键文件：agent.py (1300行)、prompts.py             │
├──────────────────────────────────────────────────┤
│  3F 工具层 (Tools)  手脚——真正干活的地方              │
│  负责：读文件、写文件、搜代码、跑命令、派发子Agent      │
│  比如：引擎层说"读main.py"，这一层去读并返回内容        │
│  关键文件：tools/base.py、tools/read_file.py 等       │
├──────────────────────────────────────────────────┤
│  4F 记忆层 (Memory) 记事本——跨对话记住信息            │
│  负责：记录用户偏好、项目知识，对话太长时压缩            │
│  比如：你上次说"不要用class"，下次对话Agent还记得       │
│  关键文件：memory/auto_memory.py、context/manager.py  │
├──────────────────────────────────────────────────┤
│  5F 安全层 (Security) 门禁系统——防止Agent搞破坏        │
│  负责：检查每个操作是否安全，拦截危险命令                │
│  比如：Agent想执行rm -rf /→ 6层防线全部拦截            │
│  关键文件：permissions/checker.py、dangerous.py       │
└──────────────────────────────────────────────────┘
```

### 3.2 一次请求的完整旅程（时序图）

你说"帮我分析这段代码的问题"，这一路上发生了什么：

```
用户         交互层         引擎层         工具层         安全层         记忆层
 │             │             │             │             │             │
 │ "分析代码"  │             │             │             │             │
 │────────────>│             │             │             │             │
 │             │ 展开@引用    │             │             │             │
 │             │ 识别/命令    │             │             │             │
 │             │────────────>│             │             │             │
 │             │             │ 注入环境上下文 │             │             │
 │             │             │ 注入长期记忆  │             │             │
 │             │             │─────────────────────────────────────────>│
 │             │             │             │             │             │
 │             │             │ Think: 调LLM │             │             │
 │             │             │─────────────>│ (LLM API)   │             │
 │             │             │<─────────────│             │             │
 │             │             │ 决定"读文件"  │             │             │
 │             │             │             │             │             │
 │             │             │ Act: 调ReadFile│            │             │
 │             │             │─────────────>│             │             │
 │             │             │             │ 安全检查      │             │
 │             │             │             │────────────>│             │
 │             │             │             │<────────────│             │
 │             │             │             │ 放行(只读)    │             │
 │             │             │             │             │             │
 │             │             │             │ 执行读文件    │             │
 │             │             │             │─────────────>│             │
 │             │             │             │<─────────────│             │
 │             │             │<─────────────│             │             │
 │             │             │ Observe: 看到代码│           │             │
 │             │             │             │             │             │
 │             │             │ Think: 发现bug│             │             │
 │             │             │ Act: 调EditFile│            │             │
 │             │             │─────────────>│             │             │
 │             │             │             │ 安全检查      │             │
 │             │             │             │────────────>│             │
 │             │             │             │ 需确认!       │             │
 │             │             │<─────────────│             │             │
 │             │<────────────│ 弹确认框     │             │             │
 │<────────────│             │             │             │             │
 │ 用户点允许   │             │             │             │             │
 │────────────>│             │             │             │             │
 │             │────────────>│             │             │             │
 │             │             │─────────────>│             │             │
 │             │             │             │ 执行写文件    │             │
 │             │             │<─────────────│             │             │
 │             │             │ Loop: 继续   │             │             │
 │             │             │ ...          │             │             │
 │             │             │ 任务完成     │             │             │
 │             │<────────────│             │             │             │
 │ 显示结果     │             │             │             │             │
 │<────────────│             │             │             │             │
```

这就是 ReAct（推理+行动）循环的核心——Think → Act → Observe → Loop。

### 3.3 为什么是五层？（分层 vs 扁平的对比）

这分层不是拍脑袋决定的，而是参考了业界顶级方案的实践。如果不分层会怎样？

| 层次 | 解决的问题 | 如果不分层 |
|------|---------|----------|
| 交互层 | 用户怎么舒服地使用？ | UI 代码散落在业务逻辑里，想换个界面（比如改成 Web 界面）就得重写整个项目 |
| 引擎层 | LLM 怎么自主完成任务？ | 核心循环和 UI、工具逻辑混在一起，改一点牵动全身，测试困难 |
| 工具层 | 怎么给 Agent 扩展能力？ | 加一个工具要改十几处代码，还容易引入 bug |
| 记忆层 | Agent 怎么记住跨对话的信息？ | 每次开新对话都是"失忆"状态，用户体验极差，长对话会爆 token |
| 安全层 | 怎么防止 AI 误操作？ | 安全检查东一处西一处，总有漏洞，审计困难 |

**分层的本质是"关注点分离"**——每层只关心自己的问题。改 UI 不影响引擎，加工具不影响安全，调模型不影响记忆。这是软件工程的核心原则（SOC, Separation of Concerns）在 AI Agent 领域的应用。

### 3.4 层间通信协议

每层之间通过明确的数据结构通信，不直接调用对方内部方法：

| 层间通信 | 数据载体 | 方向 |
|---------|---------|------|
| 交互层 ↔ 引擎层 | `AgentEvent`（12 种事件联合类型） | 双向（yield 事件 / 接收用户输入） |
| 引擎层 ↔ 工具层 | `ToolResult`（output + is_error） | 引擎调 tool.execute()，返回 ToolResult |
| 引擎层 ↔ 安全层 | `Decision`（effect + reason） | 引擎调 checker.check()，返回 Decision |
| 引擎层 ↔ 记忆层 | `Message` 列表 | 引擎读写 conversation_manager |
| 工具层 ↔ 记忆层 | 文件状态缓存 | 工具读写 file_state_cache |

这种"数据驱动"的通信方式让每层都可以独立测试——给引擎层喂 mock 事件，不需要真的启动 TUI。

---

## 4. 配置系统

### 4.1 三个配置文件的优先级

CodeBot 启动时按顺序读三个文件，**后读的覆盖先读的**：

1. **`~/.codebot/config.yaml`**（你家目录下）——全局默认值，影响你所有项目
2. **`项目/.codebot/config.yaml`**（项目目录下）——项目级配置，团队共享，提交到 Git
3. **`项目/.codebot/config.local.yaml`**（项目目录下）——你的私人配置，被 .gitignore 忽略

**举例**：团队项目配置用 DeepSeek，但你想本地测试用 Ollama，只需在 `config.local.yaml` 里写你的 Ollama 配置，它会覆盖项目级配置——完全不影响其他人。

### 4.2 完整配置示例（带注释）

```yaml
# LLM 提供商列表（可以配多个，第一个为默认）
providers:
  - name: claude                    # 自定义名称
    protocol: anthropic              # 协议：anthropic / openai / openai-compat
    base_url: https://api.anthropic.com
    model: claude-sonnet-4-5-20250929
    api_key: ${ANTHROPIC_API_KEY}    # 支持环境变量引用
    thinking: true                   # 开启 Extended Thinking
    context_window: 200000           # 可选，手动指定 context window

  - name: deepseek
    protocol: openai-compat          # OpenAI 兼容协议
    base_url: https://api.deepseek.com/v1
    model: deepseek-chat
    api_key: ${OPENAI_API_KEY}       # DeepSeek 用 OPENAI_API_KEY 环境变量

# 权限模式：default / acceptEdits / plan / bypass
permission_mode: default

# MCP 外部工具服务器
mcp_servers:
  - name: filesystem
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/allowed/dir"]
  - name: github
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: ${GITHUB_TOKEN}

# 功能开关
enable_fork: false                    # 允许 Fork 子 Agent
enable_verification_agent: false      # 启用验证 Agent
teammate_mode: ""                     # 队友模式
enable_coordinator_mode: false        # 启用 Coordinator 模式

# Hook 钩子
hooks:
  - id: lint-on-write
    event: post_tool_use
    tool_name: WriteFile
    command: ruff check $FILE_PATH
  - id: format-on-write
    event: post_tool_use
    tool_name: WriteFile
    command: ruff format $FILE_PATH

# Worktree 隔离配置
worktree:
  symlink_directories:                # 软链接这些目录避免重复安装
    - node_modules
    - .venv
  stale_cleanup_interval: 3600        # 清理检查间隔（秒）
  stale_cutoff_hours: 24              # 超过多少小时不用就清理
```

### 4.3 不同配置项的合并规则

合并不是简单的"后面全盖前面"，不同项有不同策略：

| 配置项 | 合并方式 | 为什么 |
|--------|---------|--------|
| LLM 提供商 (providers) | **全部覆盖** | 要么用团队的模型，要么用自己的，不能"混用" |
| 权限模式 (permission_mode) | **条件覆盖**：设了非 default 才覆盖 | 团队配了 plan 模式，你用 default 不会被覆盖 |
| MCP 外部工具 (mcp_servers) | **按名称合并**：同名覆盖，不同名追加 | 允许团队加一个、你自己再加一个 |
| 钩子 (hooks) | **全部追加** | 每层的钩子都有价值，不应被覆盖 |
| 开关类 (enable_fork 等) | **或运算**：任一层设为 true 就生效 | 开关类的，更宽松的策略更合理 |

**这些合并规则不是随便定的**，而是基于"配置项的语义"：
- providers 是"互斥选择"——你只能用一个模型
- mcp_servers 是"集合"——多个工具可以共存
- hooks 是"事件流"——多个监听者都能收到
- 开关是"布尔"——or 语义符合"谁开了就算"

### 4.4 API Key 的智能解析

配置里写 `api_key: ${ANTHROPIC_API_KEY}`，CodeBot 会自动去环境变量里找 `ANTHROPIC_API_KEY` 的值。不写也行——每种协议有默认的环境变量名：

- `anthropic` 协议 → 找 `ANTHROPIC_API_KEY`
- `openai` / `openai-compat` 协议 → 找 `OPENAI_API_KEY`

所以你的 DeepSeek 配置不需要写 api_key，只要设了环境变量 `OPENAI_API_KEY=你的DeepSeek密钥`，系统就会自动读取。

**设计巧思**：`${VAR_NAME}` 这种语法借鉴于 docker-compose 和 shell。它的好处是"配置文件可以提交到 Git，但密钥不进 Git"——这是 12-Factor App 的最佳实践。

### 4.5 Context Window 的四层自动检测

Context Window（上下文窗口）决定 LLM 能"一次看到多少字"。知道这个值很重要——超了就触发压缩。CodeBot 用四层 fallback 来获取：

```
第1层：配置文件里写了 context_window: 128000？
  ├─ 是 → 直接用
  └─ 否 ↓

第2层：调用 API 端点查询（GET /v1/models/模型名）
  ├─ 成功 → 用 API 返回的精确值
  └─ 失败（网络/超时/不支持）↓

第3层：查内置映射表（代码里维护常见模型的窗口大小）
  ├─ 找到 → 用映射表的值
  └─ 找不到 ↓

第4层：给保守默认值
  - 模型名含 "claude" → 20万
  - 其他 → 12.8万
```

**为什么宁可低估也不能高估？**
- 低估：提前触发压缩，损失一些上下文细节，但不会报错
- 高估：以为还有空间继续加内容，结果 API 返回 413 错误，对话中断

这是工程上的"安全失败"原则——失败时也要 fail safe，不能 fail dangerous。

---

## 5. 启动流程详解

### 5.1 你在终端敲下 `codebot` 后发生了什么？

整个启动可以分成两段：**准备阶段**（离用户输入还远）和**组装阶段**（把所有零件拼成能用的 Agent）。

**准备阶段**（`main()` 函数，`__main__.py`）：

1. 创建 `.codebot/` 目录、初始化日志文件
2. 解析命令行参数（比如 `--mode plan` 强制启动 Plan 模式、`-p "xxx"` 非交互模式）
3. 加载三份配置文件并合并成一个 `AppConfig`
4. 如果有 Hook 配置，创建 Hook 引擎

**分支**：
- 如果你用了 `-p "分析代码"` → 非交互模式：直接执行任务，打完收工
- 如果只是 `codebot` → 交互模式：启动 TUI，进入组装阶段

**组装阶段**（`CodeBotApp.on_mount()`）：

```
CodeBotApp.on_mount()
  │
  ├── 1. 读取 CODEBOT.md → 获取项目专属指令
  ├── 2. 加载 Skills → 找到所有可用技能包
  ├── 3. 加载 Agent 定义 → 找到 explore/plan/verification 等子Agent定义
  ├── 4. 创建 LLM 客户端 → 根据配置选 Anthropic/OpenAI/OpenAICompat
  ├── 5. 创建工具注册表 → 注册所有内置工具（ReadFile/Bash/Grep...）
  ├── 6. 创建权限检查器 → 六层安全防线
  ├── 7. 创建 Agent 实例 → 把上面的零件组装在一起
  ├── 8. 启动 MCP 连接 → 连上外部工具服务器
  ├── 9. 启动 Worktree 清理任务 → 每小时检查一次过期的工作树
  └── 10. 恢复上次会话（如果有的话）
```

**把第 4~7 步想象成组装一辆车**：
- LLM 客户端是**发动机**（提供推理能力）
- 工具注册表是**轮胎和方向盘**（提供行动力）
- 权限检查器是**刹车**（提供安全保障）
- Agent 实例就是把它们装在一起的**底盘**

### 5.2 启动失败的处理

每个步骤都有失败处理，保证启动鲁棒：

| 步骤 | 失败情况 | 处理方式 |
|------|---------|---------|
| 加载配置 | 配置文件语法错误 | 报错退出，提示具体行号 |
| 创建 LLM 客户端 | API Key 缺失 | 抛 AuthenticationError，提示如何设置 |
| 创建工具注册表 | 某个工具初始化失败 | 跳过该工具，记录日志，不阻塞启动 |
| 启动 MCP 连接 | 某个 MCP Server 连不上 | 跳过该 Server，其他工具正常可用 |
| 启动 Worktree 清理 | 后台任务启动失败 | 记录日志，不影响主流程 |

**设计哲学：渐进式降级**——能跑就跑，跑不了的功能降级，而不是"一个组件挂全盘挂"。这是云原生应用的核心原则。

---

## 6. 项目目录结构

### 6.1 五个核心文件（🔥标记为必看）

| 文件 | 作用 | 行数 | 学习优先级 |
|------|------|------|----------|
| `agent.py` | **Agent 主循环**——ReAct 循环、Plan 模式、max_tokens 恢复 | ~1300 | 🔥🔥🔥 |
| `app.py` | **TUI 应用**——终端界面的全部逻辑 | ~1900 | 🔥🔥 |
| `client.py` | **LLM 客户端**——Anthropic/OpenAI/DeepSeek三合一适配器 | ~800 | 🔥🔥🔥 |
| `tools/base.py` | **工具基类**——所有工具的模板，7种流式事件 | ~100 | 🔥🔥🔥 |
| `permissions/checker.py` | **权限检查器**——六层安全检查的核心 | ~100 | 🔥🔥🔥 |

### 6.2 完整目录结构

```
terminal-codebot/
├── codebot/
│   ├── agent.py              # 🔥 Agent 主循环（ReAct / Plan Mode）
│   ├── app.py                # Textual TUI 应用
│   ├── client.py             # 🔥 LLM 客户端（Anthropic / OpenAI / OpenAICompat）
│   ├── config.py             # 配置加载与合并
│   ├── conversation.py       # 对话管理
│   ├── prompts.py            # 系统提示词构建
│   ├── serialization.py      # 消息序列化（统一格式 → 各协议格式）
│   ├── validator.py          # 验证器
│   ├── driver.py             # 驱动器
│   ├── cache.py              # 缓存
│   ├── serialization.py      # 序列化
│   ├── agents/               # 子 Agent 系统
│   │   ├── builtins/         # 内置 Agent 定义（explore/plan/verification）
│   │   ├── loader.py         # Agent 加载器
│   │   ├── task_manager.py   # 任务管理器
│   │   ├── fork.py           # Fork 子 Agent
│   │   ├── tool_filter.py    # 工具过滤
│   │   └── notification.py   # 通知系统
│   ├── commands/             # 斜杠命令系统
│   │   ├── handlers/         # 内置命令处理器（/clear /compact /status 等）
│   │   ├── registry.py       # 命令注册表
│   │   └── completion.py     # 命令补全
│   ├── context/              # 🔥 上下文窗口管理（compact）
│   │   └── manager.py        # 压缩管理器
│   ├── hooks/                # Hook 钩子系统
│   │   ├── engine.py         # 钩子引擎
│   │   ├── events.py         # 7种生命周期事件
│   │   ├── conditions.py     # 触发条件
│   │   └── executors.py      # 命令执行器
│   ├── mcp/                  # MCP 协议支持
│   │   ├── client.py         # MCP 客户端
│   │   ├── manager.py        # 连接管理
│   │   └── tool_wrapper.py   # 工具包装器
│   ├── memory/               # 🔥 记忆系统
│   │   ├── auto_memory.py    # 自动记忆提取
│   │   ├── session.py        # 会话管理
│   │   ├── recall.py         # 记忆召回（LLM 选择器 + 语义检索回退）
│   │   ├── semantic_recall.py # 🔥 语义记忆检索（RAG 第一期，embedding 替代 LLM 选择器）
│   │   └── instructions.py   # 指令持久化
│   ├── permissions/          # 🔥 权限系统
│   │   ├── checker.py        # 五层权限检查器
│   │   ├── dangerous.py      # 危险命令检测（正则黑名单 + 安全白名单）
│   │   ├── modes.py          # 四种权限模式矩阵
│   │   ├── rules.py          # 规则引擎
│   │   └── sandbox.py        # 路径沙箱
│   ├── skills/               # Skill 技能包系统
│   │   ├── builtins/         # 内置技能（commit/review/test）
│   │   ├── loader.py         # 技能加载器（热重载）
│   │   ├── executor.py       # 技能执行器
│   │   └── parser.py         # Markdown 解析
│   ├── teams/                # 多 Agent 团队协作
│   │   ├── coordinator.py    # Coordinator 模式
│   │   ├── mailbox.py        # 文件系统消息队列
│   │   ├── manager.py        # 团队管理
│   │   ├── spawn_inprocess.py # 进程内启动
│   │   ├── spawn_tmux.py     # tmux 启动
│   │   └── spawn_iterm2.py   # iTerm2 启动
│   ├── tools/                # 🔥 工具注册与实现
│   │   ├── base.py           # 工具基类 + 7种流式事件
│   │   ├── read_file.py      # 读文件
│   │   ├── write_file.py     # 写文件
│   │   ├── edit_file.py      # 精确编辑（search/replace）
│   │   ├── bash.py           # 执行命令
│   │   ├── glob.py           # 文件搜索
│   │   ├── grep.py           # 内容搜索
│   │   ├── code_search.py    # 🔥 语义代码搜索（RAG 第二期，向量+BM25+RRF 混合检索）
│   │   ├── agent_tool.py     # 子 Agent 派发
│   │   ├── load_skill.py     # 加载技能
│   │   ├── task_*.py         # 任务管理（4个）
│   │   ├── team_*.py         # 团队管理（2个）
│   │   └── *_worktree.py     # Worktree（2个）
│   ├── worktree/             # Git worktree 隔离
│   │   ├── manager.py        # 工作树管理
│   │   ├── setup.py          # 创建工作树
│   │   ├── cleanup.py        # 自动清理
│   │   └── changes.py        # 变更追踪
│   ├── rag/                  # 🔥 RAG 检索增强生成子系统（详见阶段7）
│   │   ├── embedding.py      # EmbeddingProvider 抽象 + cosine_similarity
│   │   ├── chunker.py        # AST 代码分块（Python 按函数/类切）
│   │   ├── qdrant_store.py   # Qdrant 向量库（嵌入式默认 + Server 可选）
│   │   ├── indexer.py        # 增量索引（mtime+hash 两级判断）
│   │   ├── bm25.py           # 自实现 BM25 关键词召回
│   │   ├── fusion.py         # RRF 倒数排名融合
│   │   └── reranker.py       # embedding 轻量重排
│   └── styles.tcss           # TUI 样式文件（Textual CSS）
├── tests/                    # 测试文件（20个：17 核心 + 3 RAG）
├── docs/                     # 项目文档（7 个 phase + RAG 方案 + 简历描述 + Vibe Coding）
├── pyproject.toml            # 项目配置（含 [optional-dependencies] rag 组）
└── README.md
```

### 6.3 目录结构口诀

记住这个口诀：

> **app 管界面，agent 管循环，client 管模型，tools 管能力，permissions 管安全，memory 管记忆，context 管窗口，rag 管检索**

每个目录都是一个独立子系统，对应五层架构中的一层或多层。`rag/` 是横切"工具层 + 记忆层"的检索增强子系统——给 CodeSearch 工具提供语义搜索能力，给记忆召回提供 embedding 相似度能力（详见阶段7）。

---

## 7. 技术栈全景图与选型理由

### 7.1 技术栈一览

| 技术 | 做什么 | 选它的原因 | 替代方案 |
|------|------|----------|---------|
| **Textual** | 终端 UI 框架 | 支持 CSS 样式、响应式布局、组件化——能做复杂终端应用 | Rich（无交互）、urwid（老旧） |
| **Anthropic SDK** | 调 Claude 模型 | 官方 SDK，Prompt Caching 节省费用，Extended Thinking | 通用 SDK（如 litellm） |
| **OpenAI SDK** | 调 GPT 和 DeepSeek | 官方 SDK，生态最大，兼容服务最多 | - |
| **Pydantic v2** | 数据校验 + Schema 生成 | 自动生成 JSON Schema（LLM function calling 刚需），Rust 内核快 | dataclass（无 Schema）、attrs |
| **mcp** | 外部工具扩展 | 标准化协议，任何语言实现的 MCP Server 都能接入 | 自定义协议 |
| **Qdrant** | 向量库（RAG） | Rust 内核 + 原生 payload 过滤 + 嵌入式免起服务 | faiss（无元数据过滤）、chromadb |
| **PyYAML** | 读配置文件 | Python 生态标配，支持 `${VAR}` 解析 | TOML（无环境变量）、JSON（无注释） |
| **HTTPX** | HTTP 客户端 | 支持异步，用于 MCP SSE 连接 | requests（同步）、aiohttp |
| **uv** | 包管理 | Rust 实现，比 pip 快 10-100 倍 | pip、poetry |
| **pytest** | 测试 | Python 测试标准，支持 asyncio | unittest（语法繁琐） |

### 7.2 一个关键决策：为什么直接用两个 SDK 而不是用 litellm？

这是面试官最爱问的"技术选型"问题之一。答案是：

**用 litellm 的代价**：
- ❌ 放弃 Anthropic 独有功能：Prompt Caching（节省 90% 费用）、Extended Thinking（深度思考）
- ❌ 等待 litellm 适配新特性（滞后 1-3 个月）
- ❌ 多一层依赖，多一层 bug 风险
- ❌ 调试时多一层黑盒

**自己实现抽象层的代价**：
- ❌ 要写 3 个客户端类
- ❌ 要维护 3 套消息序列化逻辑

**权衡结果**：自己实现抽象层。因为：
1. Anthropic 的 Prompt Caching 能省 90% 费用——长对话下这是巨大的成本优势
2. Extended Thinking 是 Claude 的差异化能力，放弃太可惜
3. 抽象层只有 1 个方法（`stream()`），维护成本可控
4. 直接用官方 SDK，bug 修复更快，新特性当天可用

**这是一个典型的"自建 vs 买现成"权衡**——当现成方案牺牲了核心特性时，自建是值得的。

### 7.3 为什么选 Python 而不是 Go/Rust？

| 维度 | Python | Go | Rust |
|------|--------|----|------|
| LLM SDK 生态 | ✅ 最丰富 | ⚠️ 较少 | ❌ 少 |
| 异步编程 | ✅ async/await | ✅ goroutine | ⚠️ 复杂 |
| 开发速度 | ✅ 快 | 中 | 慢 |
| 运行性能 | ⚠️ 慢 | ✅ 快 | ✅ 最快 |
| 类型安全 | ⚠️ Pydantic 补足 | ✅ 原生 | ✅ 原生 |

Agent 应用的瓶颈是 **LLM API 延迟**（秒级），不是语言性能（毫秒级）。Python 的生态优势远超性能劣势，这是正确的取舍。

---

## 8. 关键设计模式

### 8.1 策略模式 — 切换模型不换代码

你想从 DeepSeek 换到 Claude，只改一行配置。这就是策略模式的威力：

- **抽象策略**：`LLMClient` 接口（只要求实现一个 `stream()` 方法）
- **具体策略**：`AnthropicClient`、`OpenAIClient`、`OpenAICompatClient`
- **工厂函数**：读取配置中的 `protocol` 字段，自动选对应的客户端

Agent 代码里只调用 `client.stream()`，完全不知道底层是哪个模型。

**RAG 子系统复用了同样的思路**：`EmbeddingProvider`（`rag/embedding.py`）也是策略模式——抽象接口只有一个 `embed()` 方法，`OpenAIEmbedding` / `NullEmbedding` 是具体策略，`create_embedding_provider` 是工厂函数。embedding 不可用时降级为 `NullEmbedding`，上层 `is_available()` 判断后走回退路径。**同一套设计模式在不同场景复用，是架构一致性的体现。**

```python
# client.py 的核心抽象
class LLMClient(ABC):
    @abstractmethod
    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]: ...
```

**为什么用策略模式而不是适配器模式？**
- 适配器模式是"事后补丁"——已有类接口不兼容，写个适配器转换
- 策略模式是"前置设计"——先定义接口，再写多个实现
- 我们是新系统，从零设计接口，所以用策略模式更合适

### 8.2 管道模式 — 六层安全检查逐个过关

每个工具调用都要走一条"管道"，经过六层关卡：

```
工具调用 → [Plan模式例外?] → [安全命令白名单?] → [危险命令黑名单?]
               → [路径沙箱?] → [用户规则?] → [模式矩阵?] → [人工确认?] → 决定
```

每道关卡返回"允许"、"拒绝"或"问用户"。任一关说"拒绝"就立即终止——就像机场安检，有一项不合格就过不去。

```python
# permissions/checker.py 的核心逻辑
def check(self, tool: Tool, arguments: dict) -> Decision:
    # Layer 0: Plan 模式例外
    if self.mode == PermissionMode.PLAN:
        if tool.name in _PLAN_MODE_ALLOWED_TOOLS:
            return Decision(effect="allow", reason="Plan mode: allowed tool")
        # ... 仅允许写计划文件

    # Layer 1: 安全命令白名单（自动放行）
    if tool.category == "command" and is_safe_command(content):
        return Decision(effect="allow", reason="Safe read-only command")

    # Layer 1b: 危险命令黑名单（直接拒绝）
    if tool.category == "command":
        hit, reason = self.detector.detect(content)
        if hit:
            return Decision(effect="deny", reason=f"危险命令拦截: {reason}")

    # Layer 2: 路径沙箱
    if tool.category in ("read", "write") and content:
        ok, reason = self.sandbox.check(content)
        if not ok:
            return Decision(effect="deny", reason=f"路径沙箱拦截: {reason}")

    # Layer 3: 规则引擎
    rule_result = self.rule_engine.evaluate(tool.name, content)
    if rule_result == "allow": return Decision(effect="allow", ...)
    if rule_result == "deny":  return Decision(effect="deny", ...)

    # Layer 4: 权限模式兜底
    effect = mode_decide(self.mode, tool.category)
    if effect == "allow": return Decision(effect="allow", ...)
    if effect == "deny":  return Decision(effect="deny", ...)

    # Layer 5: 人工确认
    return Decision(effect="ask", reason="需要用户确认")
```

**为什么用管道模式？**
- **纵深防御**：即使某一层有漏洞，后续层仍能拦截
- **可扩展**：加一层不影响其他层
- **可测试**：每层独立测试
- **短路优化**：任一层做出决定就立即返回，不浪费后续计算

### 8.3 观察者模式 — Hook 自动响应

Agent 在七个关键时刻（会话开始/结束、每轮开始/结束、工具执行前后、LLM 调用前后）广播通知，你配置的 Hook 收到通知后自动执行。这就像 Git Hook——每次 commit 前自动跑 lint。

```yaml
# 配置示例：每次写文件后自动跑 ruff
hooks:
  - id: lint-on-write
    event: post_tool_use
    tool_name: WriteFile
    command: ruff check $FILE_PATH
```

**为什么用观察者模式？**
- **解耦**：Hook 配置和 Agent 核心完全分离
- **可插拔**：想加就加，想删就删，不影响核心
- **多播**：一个事件可以触发多个 Hook

### 8.4 异步生成器模式 — 流式输出

Agent 不是一次性返回结果的——它用 `yield` 把事件逐个推送给 TUI：
- yield 一个字 → TUI 显示一个字（打字机效果）
- yield 一个工具调用 → TUI 显示工具卡片
- yield 一个权限请求 → TUI 弹确认框

```python
# agent.py 的核心循环就是异步生成器
async def run(self, user_input: str) -> AsyncIterator[AgentEvent]:
    async for event in self._react_loop():
        yield event  # 逐个推送
```

这就像视频流——服务端边生成边推，客户端边收边播。

**为什么用异步生成器？**
- **实时反馈**：用户看到打字机效果，不用等全部生成完
- **低内存**：不需要把全部响应缓存住
- **可中断**：用户按 ESC 可以随时取消

### 8.5 注册表模式 — 所有东西都是"注册-查找"

工具、技能、子Agent、命令——全部用注册表管理。想加一个新工具？

```python
# 1. 继承 Tool 基类
class MyTool(Tool):
    name = "MyTool"
    description = "做某件事"
    params_model = MyToolParams
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: BaseModel) -> ToolResult:
        # 实现
        return ToolResult(output="...")

# 2. 注册
registry.register(MyTool())

# 3. 不需要改任何核心代码
```

所有可扩展系统都是这个套路：**定义接口 → 实现接口 → 注册 → 运行时查找**。

### 8.6 Future 模式 — 异步权限确认

这是整个项目最精妙的设计之一。当 Agent 需要用户确认时，不能阻塞整个程序——还要让 TUI 能响应点击。解法是 `asyncio.Future`：

```python
# Agent 创建一个"空盒子"（Future）
future = asyncio.Future()

# yield 给 TUI
yield PermissionRequest(tool_name="EditFile", description="...", future=future)

# await 等待 TUI 把盒子填满
response = await future  # 此时 Agent 协程挂起，TUI 继续运行
```

TUI 侧：
```python
# 用户点击 Allow
future.set_result(PermissionResponse.ALLOW)  # 把盒子填满
```

**为什么用 Future 而不是回调函数？**
用 Future 的话，Agent 代码从上到下自然流动——确认前的代码和确认后的代码在同一个函数里，变量都能直接访问。用回调的话，确认后的逻辑要拆出来单独写，状态要打包传递，复杂很多。

---

## 9. 本章自测题

先尝试自己回答，再对照后续章节验证：

1. **CodeBot 的五层分别是什么？各解决什么问题？如果合并交互层和引擎层会有什么问题？**

2. **三个配置文件分别在哪？它们的优先级和合并策略是怎样的？为什么 providers 是"全部覆盖"而 hooks 是"全部追加"？**

3. **Context Window 的四层 fallback 是什么？为什么宁愿低估也不能高估？**

4. **从敲下 `codebot` 到 Agent 可以接受指令，经过了哪些关键步骤？哪一步失败会导致整个启动失败？**

5. **ReAct 循环的四个阶段（Think → Act → Observe → Loop）分别在做什么？和 Plan Mode 有什么本质区别？**

6. **Plan 模式中 Agent 只能做哪几件事？为什么不能完全禁止所有写操作？**

7. **为什么用策略模式而不是适配器模式来做多协议支持？如果用 litellm 会有什么问题？**

8. **`pyproject.toml` 里的 `[project.scripts]` 是什么作用？它和直接 `python -m codebot` 有什么区别？**

9. **管道模式在安全层是如何应用的？如果某一层的判断有 bug，会发生什么？**

10. **如果让你从零设计一个 AI Coding Agent，你会怎么分层？CodeBot 的五层是否合理？**

---

## 10. 面试高频题与回答模板

### 10.1 "介绍一下你的项目"（必问！）

**回答模板（30 秒电梯演讲版）**：

> "CodeBot 是一个轻量级终端 AI Coding Agent，基于 ReAct 和 Plan Mode 双模式驱动 LLM 自主完成编程任务。它采用交互、引擎、工具、记忆、安全五层分层架构，兼容 Anthropic、OpenAI 双协议，支持 MCP 工具扩展、Skill 技能包、跨会话记忆、多 Agent 并行协作。
>
> 核心特性包括：五层纵深安全防御、五大正交扩展机制、双层上下文压缩、跨会话自动记忆。整个项目约 15000 行 Python，覆盖了从 LLM 调用到终端 UI 的全栈实现。
>
> 我主要负责 [你的模块]，解决了 [具体问题]，通过 [技术方案] 实现了 [效果]。"

**关键点**：先讲"是什么"，再讲"怎么做"，最后讲"我做了什么"。控制在一分钟内。

### 10.2 "为什么选这个技术栈？"

**回答框架**：

| 技术 | 选型理由 | 替代方案与不选的原因 |
|------|---------|-------------------|
| Python | LLM SDK 生态最丰富，开发快，性能瓶颈在 LLM API 不在语言 | Go（生态少）、Rust（开发慢） |
| Textual | 支持 CSS、组件化、响应式——能做复杂终端 UI | Rich（无交互）、urwid（老旧） |
| Anthropic SDK | 享受 Prompt Caching（省 90% 费用）、Extended Thinking | litellm（滞后新特性、多一层依赖） |
| Pydantic v2 | 一套代码同时生成 JSON Schema 给 LLM 和校验参数给自己 | dataclass（无 Schema）、attrs |
| async/await | LLM 流式响应、工具并发、权限确认都需要异步 | 同步（TUI 卡死） |

**追问"为什么不用 litellm"**：

> "litellm 是统一层，但会牺牲 Anthropic 的独有功能：Prompt Caching 能省 90% 费用，Extended Thinking 是 Claude 的差异化能力。自己实现抽象层只需定义一个 `stream()` 接口，维护成本可控，但能享受最新特性。这是典型的'自建 vs 买现成'权衡——当现成方案牺牲核心特性时，自建是值得的。"

### 10.3 "项目架构怎么设计的？"

**回答框架**：

1. **先画五层图**：交互层 → 引擎层 → 工具层 → 记忆层 → 安全层
2. **解释每层职责**（一句话一层）
3. **解释层间通信**：通过明确数据结构（AgentEvent/ToolResult/Decision/Message）
4. **解释为什么分层**：关注点分离、独立测试、可扩展
5. **举一个"不分层会怎样"的反例**

**追问"如果让你重新设计会改什么"**：

> "目前的分层已经比较成熟，但有几个可以优化的点：
> 1. 工具层可以拆成'工具注册'和'工具执行'两个子层，执行器支持远程调用
> 2. 记忆层可以引入向量数据库做语义检索，目前是关键词匹配
> 3. 安全层可以做成插件化，让用户自定义检查器
> 不过这些是优化方向，当前架构已经能很好支撑功能。"

### 10.4 "项目最大的挑战是什么？"

（这个问题在 phase6 有详细回答，这里先给框架）

**STAR 法则**：
- **S (Situation)**：Context Window 只有 20 万 token，长对话会爆
- **T (Task)**：让 Agent 能处理长对话不中断
- **A (Action)**：设计双层压缩（工具结果裁剪 + 对话摘要）+ RecoveryState（文件快照恢复）+ 断路器（防频繁压缩）
- **R (Result)**：单次对话能处理上千轮交互，压缩后 LLM 仍保有工作上下文

---

> **下一步**：完成阶段1后，进入**阶段2：核心引擎深入**，逐段理解 Agent 主循环的真实代码实现。

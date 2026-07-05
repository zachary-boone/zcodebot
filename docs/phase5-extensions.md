# 阶段5：扩展与协作系统

> **学习目标**：理解 Skill 技能包、子 Agent、Team 团队协作、MCP 协议、Hook 系统这五大扩展机制的各自定位和协同方式。
> **预计时间**：3-4 天
> **前置要求**：完成阶段2（Agent 主循环）、阶段3（Tool 系统）

---

## 目录

1. [Skill 技能包：改变 Agent 的行为模式](#1-skill-技能包改变-agent-的行为模式)
2. [子 Agent：把大任务拆成小任务](#2-子-agent把大任务拆成小任务)
3. [Team 团队协作：多 Agent 并行工作](#3-team-团队协作多-agent-并行工作)
4. [MCP 协议：接入外部工具服务](#4-mcp-协议接入外部工具服务)
5. [Hook 系统：在关键时刻自动执行脚本](#5-hook-系统在关键时刻自动执行脚本)
6. [五大扩展机制对比](#6-五大扩展机制对比)
7. [面试高频点](#7-面试高频点)

---

## 1. Skill 技能包：改变 Agent 的行为模式

### 1.1 什么是 Skill？

Skill 是一段预写的 SOP（标准操作流程）。加载后会把这段 SOP 注入到 Agent 的系统提示词中，改变 Agent 的行为。

比如 "commit" Skill 的 SOP 是：① 跑 `git diff` 看改了啥 → ② 按规范写 commit message → ③ 写入 COMMIT_MSG 文件 → ④ 执行 `git commit`。

Agent 加载这个 Skill 后，就会按这个流程生成 commit，而不是随便写一行 "update files"。

### 1.2 Skill 文件长什么样？

每个 Skill 就是一个 Markdown 文件，开头有 YAML 元数据（名称、描述、允许的工具、运行模式），正文就是 SOP。

**文件位置（三层优先级）**：
- 内置 Skills（项目自带）
- 用户级 Skills（`~/.codebot/skills/`）
- 项目级 Skills（`项目/.codebot/skills/`）← 优先级最高

**关键设计：热重载**。每次用到某个 Skill 时，系统会重新读取磁盘上的文件——所以改 Skill 文件后不用重启，下次调用自动生效。

### 1.3 激活流程

用户说 "commit"→ Agent 调用 `LoadSkill("commit")` → 系统做三件事：
1. 把 SOP 记录到 Agent 的 active_skills 列表
2. 如果 Skill 自带工具，注册到工具表
3. 下一轮循环，环境上下文中会出现 "## Active Skills\n\n### commit\n\n[SOP 内容]"

LLM 看到这段 SOP 后，就会严格按流程执行。

### 1.4 运行模式

- **inline**：SOP 注入到主 Agent 的提示词，由主 Agent 执行（适合改变行为模式的场景）
- **subagent**：生成一个独立子 Agent 来执行（适合需要隔离上下文的复杂任务）

---

## 2. 子 Agent：把大任务拆成小任务

### 2.1 三种模式

当主 Agent 想让别人帮忙干活时，调用 Agent 工具。根据参数不同有三种模式：

**SubAgent（指定 agent_type）**
- 创建**全新**的 Agent，空对话历史，只给特定的工具集
- 适合独立子任务："探索所有 TODO 注释"、"检查安全问题"
- 子 Agent 完成后返回结果，自身销毁

**Fork（不指定 agent_type）**
- **复制**父 Agent 的对话历史到子 Agent
- 适合需要上下文的子任务："基于刚才的分析，继续深挖这个 bug"
- 共享上下文但独立执行

**Teammate（指定 team_name）**
- 创建**长期运行**的队友，加入团队
- 非阻塞：调完立即返回，队友在后台干活
- 通过 SendMessage 通信

### 2.2 工具过滤

子 Agent 不能随便用主 Agent 的全部工具。比如 "Verification Agent"（验证代码质量）不应该有 WriteFile 权限——它只能读和报告。

Agent 定义文件里写着 "允许的工具列表"，创建子 Agent 时只注册那些工具。

### 2.3 内置 Agent 定义

- **general-purpose**：通用 Agent，拥有全部工具
- **explore**：代码探索专家，只读工具，用于搜索和分析代码库
- **plan**：规划 Agent，专注制定详细执行计划
- **verification**：验证 Agent，只读工具，审查代码质量和安全问题

---

## 3. Team 团队协作：多 Agent 并行工作

### 3.1 架构

```
         Coordinator (你直接对话的 Agent)
              │
    创建团队，分配任务
              │
    ┌─────────┼─────────┐
    │         │         │
  Worker1  Worker2  Worker3
  (写代码)  (测试)   (审查)
    │         │         │
    └─────────┼─────────┘
              │
         Mailbox (文件系统消息队列)
```

### 3.2 Coordinator 模式

当开启 Coordinator 模式后，Agent 的角色从"干活的工人"变成"工头"：
- 和用户沟通需求
- 创建 Worker 去干活
- 收结果、合并、和用户汇报

### 3.3 Mailbox：团队通信

没有用网络协议，直接用**文件系统**做消息队列：

- 发送消息 → 往 mailbox 目录写一个 JSON 文件
- 接收消息 → 读目录下的 JSON 文件，读完就删

**为什么用文件系统？** 同一台机器上的 Agent 不需要网络开销，消息不丢失（写磁盘），简单可靠。

### 3.4 三种启动后端

| 后端 | 适用场景 |
|------|---------|
| in-process | 同一进程内创建 Agent 实例（最快） |
| tmux | 在 tmux 窗格中独立运行（你能看到每个 Agent 在干什么） |
| iterm2 | 在 iTerm2 标签中独立运行（macOS） |

---

## 4. MCP 协议：接入外部工具服务

### 4.1 核心思路

MCP 是 Anthropic 提出的开放标准。CodeBot 是 MCP 客户端，可以连接任何 MCP Server。

比如连一个 GitHub MCP Server，Agent 就能搜 Issues、读 PR、创建 Pull Request——就和内置工具一样自然。

### 4.2 架构

```
CodeBot 内部:
  MCPManager → 管理多个 MCP 连接
    ├── GitHub 连接（stdio：启动 npx 子进程）
    └── 数据库连接（HTTP：连远程服务器）

  每个 MCP Server 的工具被包装成 MCPToolWrapper，
  注册到 ToolRegistry，和内置工具地位完全平等。
```

### 4.3 为什么需要 MCP？

内置工具只能用 Python 写。但你可能想接入：
- GitHub API（已有 MCP Server，一行命令就能连）
- 公司内部数据库（用 Go 实现 MCP Server）
- 第三方 SaaS 工具

用 MCP 的话，任何语言实现的 MCP Server 都能接入，不需要改 CodeBot 代码。

---

## 5. Hook 系统：在关键时刻自动执行脚本

### 5.1 Agent 的七个生命周期钩子

```
会话开始 → 每轮开始 → LLM 调用前 → LLM 返回后 → 工具执行前 → 工具执行后 → 每轮结束 → 会话结束
```

你在配置里指定哪个时刻触发什么命令。

### 5.2 两种动作类型

- **command**：执行 Shell 命令，结果只记日志（如"每次写完文件后跑 ruff format"）
- **prompt**：执行命令，结果注入 LLM 的 system prompt（如"每次 LLM 调用前注入最新的项目状态"）

### 5.3 关键能力：pre_tool_use 可以拒绝执行

工具执行前的钩子可以**拒绝执行**。比如：
- 检测到 `Bash` 的命令包含 `curl` → 拒绝 + 提示 "curl 命令需要手动确认"
- 检测到 `WriteFile` 的目标是 `.env` → 拒绝 + 提示 "敏感文件受保护"

这是给用户的**自定义安全网**，在五层安全模型之外再追加你自己的规则。

### 5.4 变量替换

Hook 命令可以使用变量：$FILE_PATH（操作的文件）、$TOOL_NAME（工具名）、$ERROR（错误信息）。你不需要写死路径。

---

## 6. 五大扩展机制对比

| 扩展方式 | 一句话 | 适合谁 | 复杂度 |
|---------|--------|--------|--------|
| **内置工具** | 继承 Tool 类，实现 execute | 需要新能力（如调 API） | 中（写代码） |
| **Skill** | 写 Markdown SOP | 改变 Agent 的行为模式 | 低（写文档） |
| **MCP** | 连外部 MCP Server | 接入第三方工具 | 中（配连接），高（写 Server） |
| **Hook** | 配置 Shell 命令 | 自动检查/格式化 | 低（写 Shell） |
| **子 Agent** | Fork 独立 Agent | 并行处理子任务 | 中（写 Agent 定义） |

**正交设计**：这五种机制解决完全不同的问题，互不冲突。你可以同时用 Skill 改变 Agent 风格、用 Hook 做自动格式化、用 MCP 连 GitHub、用子 Agent 做并行验证——它们各自独立工作。

---

## 7. 面试高频点

### 7.1 "如何设计一个可扩展的 Agent 平台？"

五个正交的扩展维度：
1. **工具扩展**：Tool 基类 + 注册表，加能力
2. **行为扩展**（Skill）：SOP 注入，改风格
3. **外部扩展**（MCP）：标准协议，连生态
4. **自动扩展**（Hook）：生命周期脚本，自动化
5. **协作扩展**（Team）：多 Agent 并行，分治

### 7.2 "子 Agent 怎么确保不搞坏父 Agent 的状态？"

- 对话隔离：SubAgent 完全空历史
- 工具过滤：只给允许的工具
- 权限独立：自己的 PermissionChecker
- 结果限制：只返回最终输出文本，内部状态丢弃

### 7.3 "MCP 工具和内置工具有什么区别？"

内置工具是 Python 代码写死在项目里的。MCP 工具来自外部进程，可以是用任何语言实现的。MCP 提供协议标准化和进程隔离，内置工具提供低延迟和强耦合。

### 7.4 "Hook 会不会带来安全风险？"

Hook 是用户自己配置的，命令来自你的配置文件，不是 LLM 生成的。pre_tool_use 钩子还可以主动拒绝危险操作，反而是**加强安全**。异步 Hook 挂死也不阻塞 Agent。

---

> **下一步**：阶段6深入 TUI 交互层、Worktree 隔离、架构总结与面试综合准备。

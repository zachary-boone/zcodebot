# 阶段5：扩展与协作系统

> **学习目标**：理解 Skill 技能包、子 Agent、Team 团队协作、MCP 协议、Hook 系统这五大扩展机制的各自定位、源码实现和协同方式。读完本章你能回答"怎么设计可扩展的 Agent 平台"这个系统设计题。
> **预计时间**：3-4 天
> **前置要求**：完成阶段2（Agent 主循环）、阶段3（Tool 系统）；理解进程间通信、文件锁、Markdown frontmatter
> **面试导向**：扩展性是工程系统的核心评价指标。读完本章你能讲清楚"五种正交的扩展维度"——这是面试加分点。

---

## 目录

1. [Skill 技能包：改变 Agent 的行为模式](#1-skill-技能包改变-agent-的行为模式)
2. [子 Agent：把大任务拆成小任务](#2-子-agent把大任务拆成小任务)
3. [Team 团队协作：多 Agent 并行工作](#3-team-团队协作多-agent-并行工作)
4. [MCP 协议：接入外部工具服务](#4-mcp-协议接入外部工具服务)
5. [Hook 系统：在关键时刻自动执行脚本](#5-hook-系统在关键时刻自动执行脚本)
6. [五大扩展机制对比](#6-五大扩展机制对比)
7. [面试高频点与追问](#7-面试高频点与追问)

---

## 1. Skill 技能包：改变 Agent 的行为模式

### 1.1 什么是 Skill？

Skill 是一段预写的 **SOP**（Standard Operating Procedure，标准操作流程）。加载后会把这段 SOP 注入到 Agent 的系统提示词中，**改变 Agent 的行为模式**。

**例子**：commit Skill 的 SOP 是：
1. 跑 `git diff` 看改了啥
2. 按规范写 commit message（type: subject 格式）
3. 写入 COMMIT_MSG 文件
4. 执行 `git commit`

Agent 加载这个 Skill 后，就会按这个流程生成 commit，而不是随便写一行 "update files"。

### 1.2 Skill 文件长什么样？

每个 Skill 就是一个 Markdown 文件，开头有 YAML frontmatter（元数据），正文就是 SOP。

**示例：commit skill**

```markdown
---
name: commit
description: 分析 git diff 并生成规范 commit
allowed_tools:
  - Bash
  - ReadFile
  - WriteFile
mode: inline
---

# Commit Skill

执行步骤：

1. 运行 `git diff --staged` 查看暂存区改动
2. 运行 `git diff` 查看未暂存改动
3. 分析改动，按以下规范生成 commit message：
   - 格式：`<type>(<scope>): <subject>`
   - type: feat/fix/docs/style/refactor/test/chore
   - subject: 简短描述，不超过 50 字符
4. 把 commit message 写入 .git/COMMIT_MSG
5. 执行 `git commit`

注意：
- 不要 commit .env 等敏感文件
- 一个 commit 只做一件事
```

### 1.3 文件位置（三层优先级）

| 层级 | 位置 | 说明 |
|------|------|------|
| 内置 | `codebot/skills/builtins/` | 项目自带，不可删除 |
| 用户级 | `~/.codebot/skills/` | 用户全局 Skill，跨所有项目 |
| 项目级 | `项目/.codebot/skills/` | 项目专属，团队共享，提交 Git |

优先级：项目级 > 用户级 > 内置（同名时高优先级覆盖低优先级）。

### 1.4 关键设计：热重载

每次用到某个 Skill 时，系统会重新读取磁盘上的文件——所以改 Skill 文件后**不用重启**，下次调用自动生效。

```python
class SkillLoader:
    def load(self, name: str) -> Skill:
        # 每次都从磁盘读取，不缓存
        path = self._find_skill_file(name)
        content = path.read_text(encoding="utf-8")
        return self._parse(content)
```

**为什么热重载？** 因为 Skill 是用户经常调整的——写着写着发现 SOP 不合理，改一下就想立刻测试。如果每次都要重启，体验很差。

**为什么不缓存？** 因为 Skill 文件很小（几 KB），磁盘 I/O 开销可忽略。缓存反而引入"缓存一致性"问题（改了文件但缓存还是旧的）。

### 1.5 激活流程

用户说 "commit" → Agent 调用 `LoadSkill("commit")` → 系统做三件事：

```python
async def execute(self, params: LoadSkillParams) -> ToolResult:
    skill = self.loader.load(params.name)
    # 1. 把 SOP 记录到 Agent 的 active_skills 列表
    self.agent.activate_skill(skill.name, skill.prompt_body)
    # 2. 如果 Skill 自带工具，注册到工具表
    for tool in skill.tools:
        self.registry.register(tool)
    # 3. 下一轮循环，环境上下文中会出现 SOP
    return ToolResult(output=f"Skill '{skill.name}' activated")
```

下一轮循环，环境上下文中会出现：

```
## Active Skills

### commit

[完整的 SOP 内容]
```

LLM 看到这段 SOP 后，就会严格按流程执行。

### 1.6 两种运行模式

| 模式 | 行为 | 适用场景 |
|------|------|---------|
| `inline` | SOP 注入到主 Agent 的提示词，由主 Agent 执行 | 改变行为模式的场景（如 commit、review） |
| `subagent` | 生成一个独立子 Agent 来执行 | 需要隔离上下文的复杂任务 |

**为什么有两种模式？**

- `inline` 简单直接，主 Agent 保有完整上下文，能灵活应对
- `subagent` 隔离上下文，避免 SOP 的细节污染主 Agent 的对话历史

大多数 Skill 用 `inline` 就够。只有当 SOP 很长（如 backend-interview 有几千字）或者需要独立工具集时，才用 `subagent`。

### 1.7 内置 Skill 一览

| Skill | 作用 | 模式 |
|-------|------|------|
| `commit` | 分析 git diff 并生成规范 commit | inline |
| `review` | 多维度代码审查（逻辑/安全/性能/风格/可维护性） | inline |
| `test` | 自动生成测试用例 | inline |
| `backend-interview` | 后端面试知识问答 | inline |

---

## 2. 子 Agent：把大任务拆成小任务

### 2.1 三种模式

当主 Agent 想让别人帮忙干活时，调用 Agent 工具。根据参数不同有三种模式：

#### SubAgent（指定 agent_type）

```python
Agent(
    subagent_type="explore",  # 指定 Agent 类型
    prompt="探索所有 TODO 注释"
)
```

- 创建**全新**的 Agent，空对话历史，只给特定的工具集
- 适合独立子任务："探索所有 TODO 注释"、"检查安全问题"
- 子 Agent 完成后返回结果，自身销毁

#### Fork（不指定 agent_type）

```python
Agent(
    prompt="基于刚才的分析，继续深挖这个 bug"
    # 不指定 subagent_type
)
```

- **复制**父 Agent 的对话历史到子 Agent
- 适合需要上下文的子任务："基于刚才的分析，继续深挖这个 bug"
- 共享上下文但独立执行

#### Teammate（指定 team_name）

```python
Agent(
    name="worker-1",
    team_name="my-team",
    prompt="负责写测试"
)
```

- 创建**长期运行**的队友，加入团队
- 非阻塞：调完立即返回，队友在后台干活
- 通过 SendMessage 通信

### 2.2 三种模式对比

| 模式 | 对话历史 | 工具集 | 阻塞性 | 通信方式 | 适用场景 |
|------|---------|--------|--------|---------|---------|
| SubAgent | 空（全新） | 受限 | 阻塞（等结果） | 返回值 | 独立子任务 |
| Fork | 复制父 Agent | 受限 | 阻塞（等结果） | 返回值 | 需要上下文的子任务 |
| Teammate | 空（全新） | 受限 | 非阻塞 | SendMessage | 长期协作 |

### 2.3 工具过滤

子 Agent 不能随便用主 Agent 的全部工具。比如 "Verification Agent"（验证代码质量）不应该有 WriteFile 权限——它只能读和报告。

```python
# agents/builtins/verification.md
---
name: verification
description: 验证 Agent，专注审查和验证代码
allowed_tools:
  - ReadFile
  - Grep
  - Glob
  - Bash
---
```

Agent 定义文件里写着 "允许的工具列表"，创建子 Agent 时只注册那些工具。

**为什么需要工具过滤？**
1. **安全**：限制子 Agent 的能力，防止误操作
2. **聚焦**：让子 Agent 专注于自己的任务，不被其他工具干扰
3. **节省 token**：工具 Schema 占 context window，少注册工具省空间

### 2.4 内置 Agent 定义

| Agent | 文件 | 工具 | 用途 |
|-------|------|------|------|
| `general-purpose` | `general-purpose.md` | 全部工具 | 通用子任务 |
| `explore` | `explore.md` | 只读工具 | 代码探索（搜索、分析） |
| `plan` | `plan.md` | 只读 + WriteFile（计划文件） | 制定执行计划 |
| `verification` | `verification.md` | 只读工具 | 审查代码质量 |

### 2.5 Fork 的实现

Fork 是最复杂的模式——要复制父 Agent 的状态，但又要独立执行：

```python
def fork(self) -> Agent:
    """创建一个子 Agent，复制当前对话历史。"""
    child = Agent(
        client=self.client,           # 共享 LLM 客户端
        registry=self.registry,        # 共享工具注册表
        protocol=self.protocol,
        work_dir=self.work_dir,
        permission_checker=self.permission_checker,  # 共享权限检查器
        context_window=self.context_window,
        memory_manager=self.memory_manager,
        parent_id=self.agent_id,       # 记录父 Agent ID
    )
    # 复制对话历史（深拷贝，避免互相影响）
    child._current_conversation = clone_conversation(self._current_conversation)
    return child
```

**关键设计**：
- 共享 LLM 客户端、工具注册表、权限检查器（这些是无状态的）
- 深拷贝对话历史（有状态，必须独立）
- 记录 parent_id 用于追踪

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

```python
# Coordinator 的工作流程
1. 用户："帮我实现用户登录功能"
2. Coordinator 分析任务，拆分成子任务：
   - Worker1: 实现后端 API
   - Worker2: 实现前端页面
   - Worker3: 写测试
3. 创建团队，派发任务
4. 等待 Worker 完成（通过 Mailbox 收消息）
5. 合并结果，向用户汇报
```

### 3.3 Mailbox：文件系统消息队列

没有用网络协议，直接用**文件系统**做消息队列（`teams/mailbox.py`）：

```python
@dataclass
class MailboxMessage:
    id: str
    from_agent: str
    to_agent: str
    content: str
    summary: str = ""
    message_type: str = "text"  # text | shutdown_request | shutdown_response
    timestamp: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

class Mailbox:
    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir)

    def _agent_dir(self, agent_id: str) -> Path:
        return self._base_dir / agent_id

    def write(self, agent_id: str, message: MailboxMessage) -> None:
        """发送消息：往收件人的目录写一个 JSON 文件。"""
        d = self._agent_dir(agent_id)
        d.mkdir(parents=True, exist_ok=True)
        # 文件名：时间戳_ID.json（时间戳保证排序）
        filename = f"{message.timestamp:.6f}_{message.id}.json"
        (d / filename).write_text(
            json.dumps(message.to_dict(), ensure_ascii=False),
            encoding="utf-8",
        )

    def read(self, agent_id: str) -> list[MailboxMessage]:
        """接收消息：读目录下的 JSON 文件，读完删除。"""
        d = self._agent_dir(agent_id)
        if not d.exists():
            return []
        messages = []
        for f in sorted(d.glob("*.json")):
            data = json.loads(f.read_text(encoding="utf-8"))
            messages.append(MailboxMessage.from_dict(data))
            f.unlink()  # 读完删除
        return messages
```

### 3.4 为什么用文件系统做消息队列？

| 方案 | 优点 | 缺点 |
|------|------|------|
| 文件系统 | 简单、无依赖、消息不丢失、跨进程 | 慢（磁盘 I/O） |
| 网络（Socket） | 快 | 需要端口管理、复杂 |
| 内存队列 | 最快 | 进程崩溃消息丢失 |
| 消息中间件（Redis/RabbitMQ） | 可靠、快 | 重依赖 |

**选择文件系统的原因**：
1. 同一台机器上的 Agent 不需要网络开销
2. 消息不丢失（写磁盘）——进程崩溃重启后还能恢复
3. 简单可靠，无额外依赖
4. 跨进程：不同 tmux/iTerm2 窗格的 Agent 也能通信

### 3.5 消息类型

```python
message_type: str = "text"  # text | shutdown_request | shutdown_response
```

| 类型 | 含义 | 用途 |
|------|------|------|
| `text` | 普通文本消息 | 日常通信 |
| `shutdown_request` | 关闭请求 | Coordinator 让 Worker 停止 |
| `shutdown_response` | 关闭响应 | Worker 确认收到关闭请求 |

### 3.6 三种启动后端

| 后端 | 适用场景 | 特点 |
|------|---------|------|
| `in-process` | 同一进程内创建 Agent 实例 | 最快，但 Agent 间不隔离 |
| `tmux` | 在 tmux 窗格中独立运行 | 你能看到每个 Agent 在干什么 |
| `iterm2` | 在 iTerm2 标签中独立运行 | macOS 专用 |

**为什么要多种后端？** 不同场景需求不同：
- 调试时想看到每个 Agent 的实时输出 → tmux/iterm2
- 生产环境追求速度 → in-process
- macOS 用户 → iterm2

---

## 4. MCP 协议：接入外部工具服务

### 4.1 核心思路

MCP（Model Context Protocol）是 Anthropic 提出的开放标准。CodeBot 是 MCP 客户端，可以连接任何 MCP Server。

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

### 4.3 MCPManager 的实现

```python
class MCPManager:
    def __init__(self) -> None:
        self._configs: dict[str, MCPServerConfig] = {}
        self._clients: dict[str, MCPClient] = {}

    async def register_all_tools(self, registry: ToolRegistry) -> list[str]:
        """连接所有 MCP Server，把它们的工具注册到 ToolRegistry。"""
        errors: list[str] = []
        for name, config in self._configs.items():
            try:
                client = MCPClient(config)
                await client.connect()
                self._clients[name] = client

                # 列出该 Server 提供的所有工具
                tools = await client.list_tools()
                for tool_def in tools:
                    # 包装成 MCPToolWrapper，注册到 ToolRegistry
                    wrapper = MCPToolWrapper(name, tool_def, client)
                    registry.register(wrapper)
                    logger.info("Registered MCP tool: %s", wrapper.name)

            except Exception as e:
                msg = f"MCP server '{name}': {e}"
                logger.warning(msg)
                errors.append(msg)  # 一个 Server 挂了不影响其他

        return errors
```

**关键设计**：
- **错误隔离**：一个 MCP Server 连不上不影响其他 Server（errors 收集，不抛异常）
- **统一注册**：MCP 工具包装成 `MCPToolWrapper` 后，和内置工具地位完全平等——LLM 不知道某个工具是内置的还是 MCP 的

### 4.4 两种传输方式

| 传输方式 | 适用场景 | 例子 |
|---------|---------|------|
| `stdio` | 启动本地子进程 | `npx @modelcontextprotocol/server-filesystem` |
| `HTTP` | 连接远程服务器 | `https://mcp.example.com/sse` |

### 4.5 stdio 传输的工作原理

```python
# 启动 MCP Server 子进程
process = subprocess.Popen(
    ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/allowed/dir"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)

# 通过 stdin/stdout 用 JSON-RPC 通信
# 客户端写 JSON 到 process.stdin
# 服务端写 JSON 到 process.stdout
```

**为什么用 stdio？**
1. 简单：不需要端口
2. 隔离：每个 MCP Server 是独立进程，崩溃不影响 CodeBot
3. 通用：任何语言实现的 MCP Server 都能用（Node、Python、Go...）

### 4.6 MCPToolWrapper：统一工具接口

```python
class MCPToolWrapper(Tool):
    """把 MCP 工具包装成 CodeBot 的 Tool 接口。"""

    def __init__(self, server_name: str, tool_def: dict, client: MCPClient):
        self.name = tool_def["name"]
        self.description = tool_def["description"]
        self.params_model = self._build_params_model(tool_def["input_schema"])
        self.category = "command"  # MCP 工具默认 command 类别
        self.is_concurrency_safe = False
        self._client = client

    async def execute(self, params: BaseModel) -> ToolResult:
        # 调用 MCP Server 执行工具
        result = await self._client.call_tool(self.name, params.model_dump())
        return ToolResult(output=result)
```

LLM 调用 MCP 工具和调用内置工具完全一样——`registry.get("mcp__github__search_issues")`，LLM 不知道这是 MCP 工具。

### 4.7 为什么需要 MCP？

内置工具只能用 Python 写。但你可能想接入：
- GitHub API（已有 MCP Server，一行命令就能连）
- 公司内部数据库（用 Go 实现 MCP Server）
- 第三方 SaaS 工具

用 MCP 的话，**任何语言实现的 MCP Server 都能接入，不需要改 CodeBot 代码**。

这是"生态扩展"的标准做法——类似 VSCode 的 Language Server Protocol、Chrome 的扩展机制。

---

## 5. Hook 系统：在关键时刻自动执行脚本

### 5.1 Agent 的生命周期事件

CodeBot 定义了 12 种生命周期事件（`hooks/events.py`）：

```python
class LifecycleEvent(StrEnum):
    # 会话（Session）级别
    SESSION_START = "session_start"      # 会话开始
    SESSION_END = "session_end"          # 会话结束

    # 轮次（Turn）级别
    TURN_START = "turn_start"            # 每轮开始
    TURN_END = "turn_end"                # 每轮结束

    # 工具（Tool）级别
    PRE_TOOL_USE = "pre_tool_use"        # 工具执行前
    POST_TOOL_USE = "post_tool_use"      # 工具执行后

    # 消息（Message）级别
    PRE_SEND = "pre_send"                # 发送给 LLM 前
    POST_RECEIVE = "post_receive"        # 收到 LLM 响应后

    # 系统（System）级别
    STARTUP = "startup"                  # 程序启动
    SHUTDOWN = "shutdown"                # 程序关闭
    ERROR = "error"                      # 发生错误
    COMPACT = "compact"                  # 上下文压缩
    PERMISSION_REQUEST = "permission_request"  # 权限请求
    FILE_CHANGE = "file_change"          # 文件变更
    COMMAND_EXECUTE = "command_execute"  # 命令执行
```

你在配置里指定哪个时刻触发什么命令。

### 5.2 配置示例

```yaml
hooks:
  - id: lint-on-write
    event: post_tool_use
    tool_name: WriteFile           # 仅在 WriteFile 工具后触发
    command: ruff check $FILE_PATH

  - id: format-on-write
    event: post_tool_use
    tool_name: WriteFile
    command: ruff format $FILE_PATH

  - id: notify-on-error
    event: error
    command: echo "Error occurred" >> error.log

  - id: session-summary
    event: session_end
    command: python generate_summary.py
```

### 5.3 两种动作类型

| 动作类型 | 行为 | 例子 |
|---------|------|------|
| `command` | 执行 Shell 命令，结果只记日志 | "每次写完文件后跑 ruff format" |
| `prompt` | 执行命令，结果注入 LLM 的 system prompt | "每次 LLM 调用前注入最新的项目状态" |

### 5.4 关键能力：pre_tool_use 可以拒绝执行

工具执行前的钩子可以**拒绝执行**。这是 Hook 系统最强大的能力——给用户一个**自定义安全网**。

```python
class HookEngine:
    async def run_hooks(self, event: str, ctx: HookContext) -> None:
        for hook in self._hooks_for_event(event):
            result = await self._execute(hook, ctx)
            if event == "pre_tool_use" and result.rejected:
                raise ToolRejectedError(result.reason)
```

例子：
- 检测到 `Bash` 的命令包含 `curl` → 拒绝 + 提示 "curl 命令需要手动确认"
- 检测到 `WriteFile` 的目标是 `.env` → 拒绝 + 提示 "敏感文件受保护"

这是在六层安全模型之外再追加你自己的规则。

### 5.5 变量替换

Hook 命令可以使用变量：

| 变量 | 含义 | 例子 |
|------|------|------|
| `$FILE_PATH` | 操作的文件路径 | `/project/src/main.py` |
| `$TOOL_NAME` | 工具名 | `WriteFile` |
| `$ERROR` | 错误信息 | `File not found` |
| `$WORK_DIR` | 工作目录 | `/project` |

你不需要写死路径。

### 5.6 Hook 与六层安全的关系

Hook 不是六层安全的替代，而是**补充**：

| 机制 | 定位 | 灵活性 |
|------|------|--------|
| 六层安全 | 内置的、通用的安全防线 | 固定，改代码才能改 |
| Hook | 用户自定义的、特定场景的规则 | 灵活，改配置就能改 |

六层安全是"出厂设置"，Hook 是"用户自定义"。

### 5.7 异步非阻塞

Hook 执行是异步的，挂死不阻塞 Agent：

```python
async def _execute(self, hook: Hook, ctx: HookContext) -> HookResult:
    try:
        proc = await asyncio.create_subprocess_shell(
            hook.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=hook.timeout
        )
        return HookResult(success=True, output=stdout.decode())
    except asyncio.TimeoutError:
        return HookResult(success=False, output="Hook timeout")
```

**为什么异步？** 因为 Hook 是用户写的脚本，可能挂死（如等待网络）。如果同步执行，整个 Agent 会卡住。异步 + 超时保证 Agent 始终能继续。

---

## 6. 五大扩展机制对比

| 扩展方式 | 一句话 | 适合谁 | 复杂度 | 改变什么 |
|---------|--------|--------|--------|---------|
| **内置工具** | 继承 Tool 类，实现 execute | 需要新能力（如调 API） | 中（写代码） | 增加工具 |
| **Skill** | 写 Markdown SOP | 改变 Agent 的行为模式 | 低（写文档） | 改变行为 |
| **MCP** | 连外部 MCP Server | 接入第三方工具 | 中（配连接），高（写 Server） | 增加工具 |
| **Hook** | 配置 Shell 命令 | 自动检查/格式化 | 低（写 Shell） | 自动化 |
| **子 Agent** | Fork 独立 Agent | 并行处理子任务 | 中（写 Agent 定义） | 分治 |

### 6.1 正交设计

这五种机制解决**完全不同的问题，互不冲突**：

- **工具**解决"Agent 能做什么"
- **Skill**解决"Agent 怎么做"
- **MCP**解决"接入外部生态"
- **Hook**解决"在关键时刻自动做什么"
- **子 Agent**解决"怎么拆分任务"

你可以同时用：
- Skill 改变 Agent 风格
- Hook 做自动格式化
- MCP 连 GitHub
- 子 Agent 做并行验证

它们各自独立工作，互不干扰。

### 6.2 这是"正交扩展"的典范

**正交性**（Orthogonality）是软件设计的核心原则——不同维度的变化互相独立。就像数学里的正交向量，可以单独改变一个而不影响其他。

CodeBot 的五大扩展机制是正交设计的典范：
- 加新工具不影响 Skill
- 改 Skill 不影响 Hook
- 配 Hook 不影响子 Agent
- 用子 Agent 不影响 MCP

这让系统可以**独立演进**——某个维度的改进不需要其他维度配合。

---

## 7. 面试高频点与追问

### 7.1 "如何设计一个可扩展的 Agent 平台？"

五个正交的扩展维度：

1. **工具扩展**：Tool 基类 + 注册表，加能力
2. **行为扩展**（Skill）：SOP 注入，改风格
3. **外部扩展**（MCP）：标准协议，连生态
4. **自动扩展**（Hook）：生命周期脚本，自动化
5. **协作扩展**（Team）：多 Agent 并行，分治

**关键设计原则**：正交性。五个维度互相独立，可以单独演进。

**追问：为什么是五个不是三个？**

因为它们解决不同问题：
- 工具解决"能做什么"
- Skill 解决"怎么做"
- MCP 解决"接入外部"
- Hook 解决"自动触发"
- 子 Agent 解决"任务拆分"

合并任意两个都会丧失灵活性。比如把 Skill 和 Hook 合并（都是"自动行为"），就分不清"改变 LLM 行为"和"执行外部脚本"了。

### 7.2 "子 Agent 怎么确保不搞坏父 Agent 的状态？"

四道隔离：

1. **对话隔离**：SubAgent 完全空历史（Fork 是复制，也是深拷贝）
2. **工具过滤**：只给允许的工具
3. **权限独立**：自己的 PermissionChecker
4. **结果限制**：只返回最终输出文本，内部状态丢弃

**追问：Fork 模式不是共享历史吗？**

Fork 是**复制**历史（深拷贝），不是共享。子 Agent 修改自己的副本不影响父 Agent。这是"写时复制"的思想——只在修改时才真正独立。

### 7.3 "MCP 工具和内置工具有什么区别？"

| 维度 | 内置工具 | MCP 工具 |
|------|---------|---------|
| 实现 | Python 代码写死在项目里 | 来自外部进程，任何语言 |
| 性能 | 低延迟（同进程调用） | 较高延迟（IPC 通信） |
| 隔离 | 工具崩了影响 Agent | MCP Server 崩了不影响 Agent |
| 扩展 | 改代码 + 重启 | 配置 + 连接 |
| 生态 | 仅限项目作者 | 整个 MCP 生态 |

**关键点**：对 LLM 来说两者地位完全平等——LLM 不知道某个工具是内置的还是 MCP 的。这是"统一抽象"的胜利。

### 7.4 "Hook 会不会带来安全风险？"

Hook 是用户自己配置的，命令来自你的配置文件，**不是 LLM 生成的**。所以：
- Hook 命令是可信的（用户自己写的）
- LLM 不能触发任意 Hook（只能触发已配置的）
- pre_tool_use 钩子还可以主动拒绝危险操作，反而是**加强安全**

**追问：如果 Hook 命令本身有 bug 怎么办？**

异步 + 超时保证 Hook 挂死不阻塞 Agent。Hook 失败只记日志，不影响主流程。这是"故障隔离"——Hook 是辅助功能，不应该拖垮主流程。

### 7.5 "Skill 为什么用 Markdown 而不是 JSON/YAML？"

三个原因：

1. **可读性**：Skill 是给人读的 SOP，Markdown 的可读性远超 JSON/YAML
2. **表达力**：Markdown 支持标题、列表、代码块，适合写流程
3. **frontmatter 兼容**：YAML frontmatter 做元数据，正文做 SOP，两全其美

**追问：为什么不用 Python 代码写 Skill？**

因为 Skill 是**行为指导**，不是**逻辑实现**。Skill 告诉 LLM "按这个流程做"，具体怎么做由 LLM 决定。如果用代码，就变成硬编码逻辑了，失去灵活性。

### 7.6 "Mailbox 为什么用文件系统而不是 Redis？"

| 方案 | 优点 | 缺点 |
|------|------|------|
| 文件系统 | 无依赖、消息不丢失、跨进程 | 慢 |
| Redis | 快、支持发布订阅 | 重依赖、内存数据可能丢 |

选择文件系统的原因：
1. **零依赖**：CodeBot 是轻量级工具，不想引入 Redis
2. **跨进程**：tmux/iTerm2 的 Agent 是独立进程，文件系统天然跨进程
3. **消息不丢失**：进程崩溃重启后还能恢复
4. **够用**：Agent 间消息量不大，磁盘 I/O 不是瓶颈

### 7.7 "如果让你加一个新的扩展机制，会加什么？"

参考答案：

1. **插件市场**：类似 VSCode Marketplace，一键安装第三方扩展
2. **Webhook**：接收外部事件（如 GitHub Push），触发 Agent 执行
3. **定时任务**：cron 风格的定时 Agent 执行
4. **权限模板**：预设的权限配置，一键应用（如"只读模式""全权限模式"）
5. **多模态扩展**：支持图片、音频输入（目前只支持文本）

---

> **下一步**：阶段6深入 TUI 交互层、Worktree 隔离、架构总结与面试综合准备。
